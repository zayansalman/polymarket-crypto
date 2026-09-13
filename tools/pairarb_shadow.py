"""Live shadow runner for two-sided maker quoting on 5m Up/Down markets (#182).

**Places no orders.** Reads public books and the public trade tape, simulates
what resting bids on both legs would have done, and settles each window on its
real outcome. Nothing here can touch money — there is no signer, no key, and no
write path to the CLOB.

What it does each cycle:

1. Construct the current and next window slugs from the clock. The 5m slug is a
   pure function of time (``{asset}-updown-5m-{floor(now/300)*300}``), so it is
   built, never discovered — ``venue_recorder.discover()`` pages ``endDate``
   ascending over ``closed=false`` and surfaces zombie Dec-2025 windows instead
   of live ones (#182).
2. On first sight of a window, read both legs' books and ask
   :func:`polymarket_bot.pairarb.quoter.plan_quote` where it would rest. Record the
   depth already queued at those prices — we join the **back**.
3. Each cycle, pull the market's public trade tape and advance the simulated
   fills (:mod:`polymarket_bot.pairarb.fills`).
4. Once resolved, settle into hedged pairs plus any stranded leg. Stranded legs
   settle on the realized outcome, never at par.

Usage::

    python tools/pairarb_shadow.py --assets doge,btc --size 10
    python tools/pairarb_shadow.py --assets doge --once
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_bot.pairarb import ledger
from polymarket_bot.pairarb.fills import settle_window, simulate_fill, vwap
from polymarket_bot.pairarb.quoter import DEFAULT_MIN_EDGE, plan_quote
from polymarket_bot.pairarb.types import BookSide, PairOutcome, QuotePlan, RestingOrder

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"

WINDOW_SECONDS = 300
# Grace period after a window closes before we look for its resolution — the
# venue needs a moment to publish outcomePrices.
SETTLE_DELAY = 45


def window_slug(asset: str, start_ts: int) -> str:
    """Build the 5m window slug for ``asset`` starting at ``start_ts``."""
    return f"{asset}-updown-5m-{start_ts}"


def current_window_start(now: int | None = None) -> int:
    """Unix seconds at which the in-progress 5-minute window began."""
    now = int(time.time()) if now is None else now
    return (now // WINDOW_SECONDS) * WINDOW_SECONDS


@dataclass
class TrackedWindow:
    """One window we are shadow-quoting."""

    slug: str
    asset: str
    start_ts: int
    condition_id: str
    up_token: str
    down_token: str
    plan: QuotePlan | None = None
    up_order: RestingOrder | None = None
    down_order: RestingOrder | None = None
    outcome: PairOutcome | None = None
    note: str = ""
    # Banked executions as (price, size). A quote re-posted as the book moves
    # fills at several prices, so a single entry price cannot settle the leg.
    up_execs: list[tuple[float, float]] = field(default_factory=list)
    down_execs: list[tuple[float, float]] = field(default_factory=list)
    exec_rows: list[dict[str, Any]] = field(default_factory=list)
    requotes: int = 0
    _tape: dict[str, dict[str, Any]] = field(default_factory=dict)

    def banked(self, outcome: str) -> float:
        """Shares already banked on one leg across all previous quotes."""
        execs = self.up_execs if outcome == "Up" else self.down_execs
        return sum(size for _, size in execs)

    @property
    def end_ts(self) -> int:
        return self.start_ts + WINDOW_SECONDS

    @property
    def settled(self) -> bool:
        return self.outcome is not None


async def _get(client: httpx.AsyncClient, url: str, **params: Any) -> Any:
    r = await client.get(url, params=params or None, timeout=20.0)
    r.raise_for_status()
    return r.json()


async def fetch_market(client: httpx.AsyncClient, slug: str) -> dict[str, Any] | None:
    """Fetch one market by slug, or ``None`` when the venue does not list it."""
    try:
        rows = await _get(client, f"{GAMMA}/markets", slug=slug)
    except Exception:  # noqa: BLE001 — a missing window is normal, not fatal
        return None
    return rows[0] if isinstance(rows, list) and rows else None


async def fetch_side(
    client: httpx.AsyncClient, token_id: str, outcome: str
) -> BookSide | None:
    """Read one leg's top-of-book and the size resting at its best bid."""
    try:
        book = await _get(client, f"{CLOB}/book", token_id=token_id)
    except Exception:  # noqa: BLE001
        return None
    bids = sorted(
        ((float(x["price"]), float(x["size"])) for x in book.get("bids", [])),
        reverse=True,
    )
    asks = sorted((float(x["price"]), float(x["size"])) for x in book.get("asks", []))
    return BookSide(
        token_id=token_id,
        outcome=outcome,
        bids=tuple(bids),
        best_ask=asks[0][0] if asks else None,
    )


async def fetch_tape(
    client: httpx.AsyncClient, condition_id: str, limit: int = 500
) -> list[dict[str, Any]]:
    """Public trade tape for one market."""
    try:
        rows = await _get(client, f"{DATA}/trades", market=condition_id, limit=limit)
    except Exception:  # noqa: BLE001
        return []
    return rows if isinstance(rows, list) else []


def _tape_key(t: dict[str, Any]) -> str:
    """Stable-ish identity for a tape row, so repeated polls don't double-count."""
    return "|".join(
        str(t.get(k, "")) for k in ("transactionHash", "asset", "price", "size", "timestamp")
    )


async def discover_window(
    client: httpx.AsyncClient, asset: str, start_ts: int
) -> TrackedWindow | None:
    """Resolve one asset/window into tracked form, or ``None`` if not listed."""
    slug = window_slug(asset, start_ts)
    market = await fetch_market(client, slug)
    if not market:
        return None
    try:
        tokens = json.loads(market["clobTokenIds"])
        outcomes = json.loads(market["outcomes"])
    except (KeyError, ValueError, TypeError):
        return None
    if len(tokens) != 2 or len(outcomes) != 2:
        return None
    idx_up = 0 if str(outcomes[0]).lower() == "up" else 1
    return TrackedWindow(
        slug=slug,
        asset=asset,
        start_ts=start_ts,
        condition_id=str(market.get("conditionId", "")),
        up_token=str(tokens[idx_up]),
        down_token=str(tokens[1 - idx_up]),
    )


def _bank(w: TrackedWindow, order: RestingOrder | None) -> None:
    """Move an order's filled shares into the window's execution record."""
    if order is None or order.filled <= 0:
        return
    execs = w.up_execs if order.outcome == "Up" else w.down_execs
    execs.append((order.price, order.filled))
    w.exec_rows.append(
        {
            "outcome": order.outcome,
            "price": order.price,
            "size": order.filled,
            "depth_ahead": order.depth_ahead,
            "posted_ts": order.posted_ts,
            "filled_ts": int(time.time()),
        }
    )


async def maintain_quotes(
    client: httpx.AsyncClient,
    w: TrackedWindow,
    size: float,
    min_edge: float,
    max_shares: float,
    requote_secs: int,
    offset: float,
) -> None:
    """Post, hold, or re-post our two resting bids against the current book.

    A real maker re-quotes as the book moves; a bid left at its opening price
    is stranded far from the market within seconds on a 5-minute window. So each
    cycle we re-read both books and:

    * **hold** the existing order when our price is still the best bid — moving
      would forfeit queue position we have already earned, and standing still
      does not, so holding is both realistic and conservative;
    * **re-post** at the new best bid when the book has moved away, banking
      whatever filled and re-joining the **back** of the new queue
      (``depth_ahead`` resets — a cancel/replace never keeps priority);
    * **stand aside** when the two best bids no longer sum below 1.00.

    Inventory is capped per leg by ``max_shares``: a real maker does not
    accumulate unbounded one-sided risk, and without a cap a persistently
    one-sided window would report a stranded position no operator would hold.
    """
    up = await fetch_side(client, w.up_token, "Up")
    down = await fetch_side(client, w.down_token, "Down")
    if up is None or down is None:
        w.note = "no book"
        return

    plan = plan_quote(
        w.slug, up, down, size=size, min_edge=min_edge, offset=offset
    )
    if plan is None:
        # Pull both quotes. Bank anything filled so it still settles.
        _bank(w, w.up_order)
        _bank(w, w.down_order)
        w.up_order = None
        w.down_order = None
        bid_sum = (
            f"{up.best_bid + down.best_bid:.3f}"
            if up.best_bid is not None and down.best_bid is not None
            else "n/a"
        )
        w.note = f"stood aside (bid sum {bid_sum})"
        return

    w.plan = plan
    now = int(time.time())
    moved = False
    for outcome, price, depth in (
        ("Up", plan.up_price, plan.up_depth_ahead),
        ("Down", plan.down_price, plan.down_depth_ahead),
    ):
        current = w.up_order if outcome == "Up" else w.down_order
        if current is not None:
            if abs(current.price - price) < 1e-9:
                continue  # still at the best bid — keep our place in the queue
            if now - current.posted_ts < requote_secs:
                # Deliberately do NOT chase. Cancel/replace forfeits queue
                # position, so an order that moves on every 1-tick tick never
                # accumulates enough through-volume to fill — it resets its own
                # clock forever. Holding a bid the market has walked away from
                # is what a real maker does, and the fills it eventually gets
                # when price comes back ARE the adverse selection we are here
                # to measure. Suppressing them would flatter the result.
                continue
        _bank(w, current)
        moved = moved or current is not None
        remaining = max(0.0, max_shares - w.banked(outcome))
        order = (
            RestingOrder(outcome, price, min(size, remaining), depth, now)
            if remaining > 0
            else None
        )
        if outcome == "Up":
            w.up_order = order
        else:
            w.down_order = order

    if moved:
        w.requotes += 1
    w.note = f"quoting {plan.up_price:.3f}/{plan.down_price:.3f} (rq {w.requotes})"


async def advance_fills(client: httpx.AsyncClient, w: TrackedWindow) -> None:
    """Pull the tape and advance both simulated resting orders."""
    if w.up_order is None or w.down_order is None:
        return
    for row in await fetch_tape(client, w.condition_id):
        w._tape.setdefault(_tape_key(row), row)
    tape = list(w._tape.values())
    w.up_order = simulate_fill(w.up_order, tape)
    w.down_order = simulate_fill(w.down_order, tape)


async def try_settle(client: httpx.AsyncClient, w: TrackedWindow) -> bool:
    """Settle the window if the venue has published its outcome. True when done."""
    market = await fetch_market(client, w.slug)
    if not market:
        return False
    try:
        prices = [float(x) for x in json.loads(market.get("outcomePrices") or "[]")]
        outcomes = [str(x).lower() for x in json.loads(market.get("outcomes") or "[]")]
    except (ValueError, TypeError):
        return False
    if len(prices) != 2 or max(prices) < 0.99:
        return False  # not resolved yet
    idx_up = 0 if outcomes and outcomes[0] == "up" else 1
    resolved_up = prices[idx_up] >= 0.99

    # Bank whatever the still-resting orders filled, then settle on the
    # volume-weighted price of every execution on each leg.
    _bank(w, w.up_order)
    _bank(w, w.down_order)
    w.up_order = None
    w.down_order = None
    up_px, up_f = vwap(w.up_execs)
    dn_px, dn_f = vwap(w.down_execs)

    w.outcome = settle_window(
        window_slug=w.slug,
        up_filled=up_f,
        down_filled=dn_f,
        up_price=up_px,
        down_price=dn_px,
        resolved_up=resolved_up,
    )
    return True


def render(tracked: dict[str, TrackedWindow], settled: list[PairOutcome]) -> str:
    """Compact console view: live windows on top, running totals below."""
    lines = ["", "=" * 96]
    lines.append(
        f"{'window':<30} {'quote':<14} {'depth ahead':<13} {'filled U/D':<13} {'status'}"
    )
    lines.append("-" * 96)
    for w in sorted(tracked.values(), key=lambda x: x.start_ts):
        if w.settled:
            continue
        quote = f"{w.plan.up_price:.3f}/{w.plan.down_price:.3f}" if w.plan else "-"
        depth = (
            f"{w.plan.up_depth_ahead:.0f}/{w.plan.down_depth_ahead:.0f}" if w.plan else "-"
        )
        up_live = w.up_order.filled if w.up_order else 0.0
        dn_live = w.down_order.filled if w.down_order else 0.0
        fills = f"{w.banked('Up') + up_live:.1f}/{w.banked('Down') + dn_live:.1f}"
        left = w.end_ts - int(time.time())
        status = f"{left:+d}s  {w.note}"
        lines.append(f"{w.slug:<30} {quote:<14} {depth:<13} {fills:<13} {status}")

    if settled:
        pnl = sum(o.pnl for o in settled)
        pairs = sum(o.pairs for o in settled)
        stranded = sum(o.stranded for o in settled)
        filled = [o for o in settled if o.up_filled or o.down_filled]
        both = [o for o in settled if o.up_filled > 0 and o.down_filled > 0]
        lines.append("-" * 96)
        lines.append(
            f"SETTLED {len(settled):>4}  |  any-fill {len(filled):>3}  both-legs {len(both):>3}"
            f"  |  hedged pairs {pairs:>8.1f}  stranded {stranded:>8.1f}"
            f"  |  PnL ${pnl:+.2f}"
        )
        if pairs + stranded > 0:
            lines.append(
                f"{'':8}  hedged share of filled volume: "
                f"{100 * 2 * pairs / (2 * pairs + stranded):.0f}%"
                f"   (stranded legs settle on outcome, never at par)"
            )
    lines.append("=" * 96)
    return "\n".join(lines)


async def run(
    assets: list[str],
    size: float,
    min_edge: float,
    max_shares: float,
    requote_secs: int,
    offset: float,
    once: bool,
    db_path: Path = ledger.DEFAULT_DB,
) -> int:
    tracked: dict[str, TrackedWindow] = {}
    settled: list[PairOutcome] = []
    await ledger.init(db_path)
    prior = await ledger.summary(db_path)
    if prior.get("windows"):
        print(
            f"resuming — {prior['windows']} windows already recorded, "
            f"running PnL ${prior['pnl']:+.2f}"
        )
    print(
        f"pairarb shadow | assets={','.join(assets)} size={size} "
        f"min_edge={min_edge} max_shares={max_shares} "
        f"offset={offset} requote={requote_secs}s"
        f"\nSHADOW ONLY — no orders are placed.\n"
    )
    async with httpx.AsyncClient(headers={"User-Agent": "pairarb-shadow/0.1"}) as client:
        while True:
            now = int(time.time())
            cur = current_window_start(now)
            # Track the in-progress window and the next one.
            for asset in assets:
                for start in (cur, cur + WINDOW_SECONDS):
                    slug = window_slug(asset, start)
                    if slug in tracked:
                        continue
                    if await ledger.already_settled(slug, db_path):
                        continue
                    w = await discover_window(client, asset, start)
                    if w is None:
                        continue
                    tracked[slug] = w

            for w in list(tracked.values()):
                if w.settled:
                    continue
                if now < w.end_ts:
                    # Advance fills against the standing quote BEFORE re-quoting,
                    # so a fill that happened at the old price is banked at that
                    # price rather than silently re-priced.
                    await advance_fills(client, w)
                    await maintain_quotes(
                        client, w, size, min_edge, max_shares, requote_secs, offset
                    )
                elif now >= w.end_ts + SETTLE_DELAY:
                    await advance_fills(client, w)
                    if await try_settle(client, w):
                        settled.append(w.outcome)  # type: ignore[arg-type]
                        o = w.outcome
                        assert o is not None
                        fresh = await ledger.record_window(
                            {
                                "window_slug": o.window_slug,
                                "asset": w.asset,
                                "start_ts": w.start_ts,
                                "settled_at": int(time.time()),
                                "up_filled": o.up_filled,
                                "down_filled": o.down_filled,
                                "up_vwap": o.up_price,
                                "down_vwap": o.down_price,
                                "resolved_up": int(o.resolved_up),
                                "pairs": o.pairs,
                                "stranded": o.stranded,
                                "pnl": o.pnl,
                                "requotes": w.requotes,
                                "quoted_up": w.plan.up_price if w.plan else None,
                                "quoted_down": w.plan.down_price if w.plan else None,
                                "up_depth_ahead": w.plan.up_depth_ahead if w.plan else None,
                                "down_depth_ahead": (
                                    w.plan.down_depth_ahead if w.plan else None
                                ),
                            },
                            db_path,
                        )
                        if fresh:
                            await ledger.record_execs(
                                o.window_slug, w.asset, w.exec_rows, db_path
                            )
                        print(
                            f"  SETTLED {o.window_slug:<28} "
                            f"{'Up' if o.resolved_up else 'Down':<5} "
                            f"pairs={o.pairs:.1f} stranded={o.stranded:.1f} "
                            f"pnl=${o.pnl:+.2f}"
                        )

            print(render(tracked, settled), flush=True)

            # Drop settled windows older than 30 min so memory stays flat.
            cutoff = now - 1800
            for slug, w in list(tracked.items()):
                if w.settled and w.end_ts < cutoff:
                    del tracked[slug]

            if once:
                return 0
            await asyncio.sleep(5)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--assets", default="doge,btc", help="comma-separated 5m assets")
    p.add_argument("--size", type=float, default=10.0, help="shares quoted per leg")
    p.add_argument(
        "--min-edge",
        type=float,
        default=DEFAULT_MIN_EDGE,
        help="minimum dollars per completed pair before quoting",
    )
    p.add_argument(
        "--max-shares",
        type=float,
        default=50.0,
        help="inventory cap per leg per window",
    )
    p.add_argument(
        "--offset",
        type=float,
        default=0.05,
        help="dollars below the touch to rest each leg; 0 joins the touch",
    )
    p.add_argument(
        "--requote-secs",
        type=int,
        default=99999,
        help="minimum seconds an order must rest before it may be moved; "
             "chasing every tick forfeits queue position and never fills",
    )
    p.add_argument(
        "--db", default=str(ledger.DEFAULT_DB), help="shadow ledger SQLite path"
    )
    p.add_argument("--once", action="store_true", help="single cycle then exit")
    a = p.parse_args()
    assets = [x.strip().lower() for x in a.assets.split(",") if x.strip()]
    try:
        return asyncio.run(
            run(
                assets, a.size, a.min_edge, a.max_shares,
                a.requote_secs, a.offset, a.once, Path(a.db),
            )
        )
    except KeyboardInterrupt:
        print("\nstopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
