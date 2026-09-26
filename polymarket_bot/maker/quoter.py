"""Decide what to rest, and where in the queue it lands.

The measurement this implements: across 2,600 resolved crypto Up/Down markets,
maker fills between 0.55 and 0.92 returned +2 to +6.6c/share held to resolution,
fee-free, while maker fills under 0.50 lost. The sign flips at the midpoint. So
the rule is to rest a bid on the FAVOURITE and never on the underdog.

Two things separate this from the simulation that wrongly said makers bleed:

  - We never cross. A quote at or below the best bid is passive by construction,
    pays no fee, and is the only side of this market where the arithmetic can
    clear at all.
  - We write down the queue we joined. ``depth_ahead`` is the size already
    resting at our price or better; until the tape trades through it, none of
    the flow is ours. Paper that skips this reports the edge of a front-of-queue
    order nobody actually had.

A quote is placed once per market per window and left alone. Chasing the mid is
how a passive order becomes an aggressive one by accident.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Accept": "application/json",
}
TICK = 0.01


@dataclass(frozen=True)
class Book:
    bids: list[tuple[float, float]]     # best first
    asks: list[tuple[float, float]]     # best first

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    def depth_at_or_better(self, price: float) -> float:
        """Bid size resting at ``price`` or above — the queue in front of us."""
        return sum(sz for px, sz in self.bids if px >= price - 1e-9)


@dataclass(frozen=True)
class LiveMarket:
    condition_id: str
    slug: str
    title: str
    end_ts: int
    tokens: tuple[str, str]
    outcomes: tuple[str, str]


def _parse(market: dict[str, Any], end_ts: int) -> LiveMarket | None:
    try:
        toks = json.loads(market.get("clobTokenIds") or "[]")
        outs = json.loads(market.get("outcomes") or "[]")
    except (TypeError, ValueError):
        return None
    if len(toks) != 2 or len(outs) != 2 or not market.get("conditionId"):
        return None
    return LiveMarket(
        condition_id=market["conditionId"],
        slug=market.get("slug") or "",
        title=market.get("question") or market.get("slug") or "",
        end_ts=end_ts,
        tokens=(str(toks[0]), str(toks[1])),
        outcomes=(str(outs[0]), str(outs[1])),
    )


async def live_markets(
    client: httpx.AsyncClient, series: list[str]
) -> list[LiveMarket]:
    """Open markets in the given Gamma series, soonest to resolve first.

    Gamma leaves long-resolved Up/Down markets at ``closed: false`` — the same
    quirk that made a Gamma-based settler never fire. Markets whose end has
    already passed are dropped here rather than each costing two book requests
    before being refused downstream.
    """
    import calendar

    now = int(time.time())
    out: list[LiveMarket] = []
    for s in series:
        try:
            r = await client.get(
                f"{GAMMA}/events",
                params={"series_slug": s, "closed": "false", "limit": 40,
                        "order": "endDate", "ascending": "true"},
                timeout=20.0)
            r.raise_for_status()
            evs = r.json()
        except Exception:  # noqa: BLE001 — one bad series must not stop the rest
            continue
        for e in evs:
            for m in e.get("markets", []):
                end = m.get("endDate") or e.get("endDate") or ""
                try:
                    ts = calendar.timegm(time.strptime(end[:19], "%Y-%m-%dT%H:%M:%S"))
                except Exception:  # noqa: BLE001
                    continue
                if ts <= now:
                    continue
                lm = _parse(m, ts)
                if lm is not None:
                    out.append(lm)
    out.sort(key=lambda m: m.end_ts)
    return out


async def book(client: httpx.AsyncClient, token_id: str) -> Book | None:
    try:
        r = await client.get(f"{CLOB}/book", params={"token_id": token_id},
                             timeout=15.0)
        r.raise_for_status()
        raw = r.json()
    except Exception:  # noqa: BLE001
        return None
    def levels(key: str, reverse: bool) -> list[tuple[float, float]]:
        vals = [(float(x["price"]), float(x["size"]))
                for x in (raw.get(key) or []) if float(x.get("size") or 0) > 0]
        return sorted(vals, key=lambda r: r[0], reverse=reverse)
    return Book(bids=levels("bids", True), asks=levels("asks", False))


@dataclass(frozen=True)
class Quote:
    token_id: str
    outcome: str
    price: float
    size: float
    depth_ahead: float
    best_bid: float
    best_ask: float
    mid: float
    spread: float


def decide(
    m: LiveMarket,
    books: dict[str, Book],
    *,
    band_lo: float,
    band_hi: float,
    size: float,
    improve: bool,
    max_spread: float,
) -> tuple[Quote | None, str]:
    """Pick the favourite and price a passive bid on it, or say why not.

    Returns ``(quote, reason)``; ``quote`` is None when we decline, and the
    reason is written to the ledger either way. A refusal with no recorded
    reason is indistinguishable from never having looked.
    """
    scored: list[tuple[float, str, str, Book]] = []
    for tok, out in zip(m.tokens, m.outcomes):
        b = books.get(tok)
        if b is None or b.mid is None:
            continue
        scored.append((b.mid, tok, out, b))
    if len(scored) < 2:
        return None, "book unavailable on one or both outcomes"

    scored.sort(reverse=True)
    mid, tok, out, b = scored[0]          # the favourite
    assert b.best_bid is not None and b.best_ask is not None

    spread = b.best_ask - b.best_bid
    if spread > max_spread:
        return None, (f"spread {100*spread:.1f}c wider than the "
                      f"{100*max_spread:.0f}c cap")

    # Improving by a tick puts us at the front of the queue; joining leaves us
    # behind everything already there. Never cross — that would make us a taker.
    price = round(b.best_bid + TICK, 3) if improve else b.best_bid
    if price >= b.best_ask:
        price = b.best_bid
    if not (band_lo <= price < band_hi):
        return None, (f"favourite bid {price:.2f} outside the "
                      f"{band_lo:.2f}-{band_hi:.2f} band where makers profit")

    depth = b.depth_at_or_better(price) if price <= b.best_bid + 1e-9 else 0.0
    return Quote(
        token_id=tok, outcome=out, price=price, size=size, depth_ahead=depth,
        best_bid=b.best_bid, best_ask=b.best_ask, mid=mid, spread=spread,
    ), ("improved a tick to the front of the queue" if depth == 0
        else f"joined behind {depth:.0f}sh")
