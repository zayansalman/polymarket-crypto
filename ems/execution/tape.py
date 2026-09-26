"""Reads from the venue that every strategy's fills and results rest on.

- The taker trade tape (``data-api /trades?market=<id>&takerOnly=true``): :func:`read_taker_tape`
  reads every record from a second on, and :func:`tape_newest_ts` how far the tape has got. How
  the records fill resting orders is ``ems.execution.queue``.
- A market's result from the venue's order-book service (``clob /markets/<id>``: ``closed``
  plus the per-token ``winner`` flag, never Gamma, which drops ended 15m markets):
  :func:`market_outcome`.

A read that could be partial or wrong raises (``TapeUnavailable``, ``MarketUnavailable``)
rather than return something that would skip trades or call a result early.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ems.execution.queue import SIDES, TapePrint

# Browser-like headers for Polymarket's public REST endpoints, which answer a
# bare client with a Cloudflare 403.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Accept": "application/json",
}

DATA_API = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

TAPE_PAGE = 500
# Consecutive pages overlap by this many records. A page that shares none of them with the
# pages before it means the tape shifted under us (a reply from an older copy), so the read is
# thrown away rather than risk a gap.
TAPE_PAGE_OVERLAP = 50
TAPE_MAX_OFFSET = 5000
# Records asked for when a market's tape is read only to see how far the tape has got.
FRESHNESS_PAGE = 20
HTTP_TIMEOUT_S = 10.0


class TapeUnavailable(RuntimeError):
    """The trade tape could not be read in full this time. Nothing was moved."""


class MarketUnavailable(RuntimeError):
    """The venue's order-book service could not say how a market resolved."""


class HttpClient(Protocol):
    """The slice of ``httpx.AsyncClient`` used here."""

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class TapeRead:
    """The tape for one market from ``since`` on, oldest first."""

    prints: tuple[TapePrint, ...]
    newest_ts: int | None  # the newest record in the reply, however old
    skipped: int  # records that could not be read or were not for this market


def as_float(value: Any) -> float | None:
    """``value`` as a finite float, or None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _record_key(rec: Any) -> tuple | None:
    """A record's identity, from parsed numbers (the tape writes 10 and 10.0 alike)."""
    if not isinstance(rec, Mapping):
        return None
    ts = as_float(rec.get("timestamp"))
    size = as_float(rec.get("size"))
    price = as_float(rec.get("price"))
    if ts is None or size is None or price is None:
        return None
    return (
        int(ts), str(rec.get("transactionHash") or ""), str(rec.get("proxyWallet") or ""),
        str(rec.get("asset") or ""), str(rec.get("outcomeIndex")), str(rec.get("side") or ""),
        size, price,
    )


def _outcome_of(
    rec: Mapping[str, Any], up_token: str | None, down_token: str | None
) -> str | None:
    """The outcome whose token a record traded: by token id when the window's tokens are
    known, else by the record's own outcome label, else by its outcome index."""
    asset = str(rec.get("asset") or "")
    if asset and up_token and down_token:
        return "Up" if asset == up_token else "Down" if asset == down_token else None
    label = rec.get("outcome")
    if label in SIDES:
        return str(label)
    index = rec.get("outcomeIndex")
    if index in (0, 1) and not isinstance(index, bool):
        return SIDES[int(index)]  # Up/Down markets list their outcomes as ["Up", "Down"]
    return None


def _same_market(rec: Mapping[str, Any], condition_id: str) -> bool:
    cid = rec.get("conditionId")
    return not cid or str(cid).lower() == condition_id.lower()


def _print_of(
    rec: Mapping[str, Any], key: tuple, *, up_token: str | None, down_token: str | None
) -> TapePrint | None:
    side = str(rec.get("side") or "").upper()
    outcome = _outcome_of(rec, up_token, down_token)
    ts, size, price = key[0], key[6], key[7]
    if side not in ("BUY", "SELL") or outcome is None or size <= 0 or not 0.0 <= price <= 1.0:
        return None
    return TapePrint(ts=ts, outcome=outcome, side=side, size=size, price=price)


def http_status(exc: BaseException) -> str:
    """A failed request in a few words: its HTTP status, else the exception's type."""
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return f"HTTP {code}" if code else type(exc).__name__


async def read_taker_tape(
    client: HttpClient,
    condition_id: str,
    *,
    since: int,
    up_token: str | None = None,
    down_token: str | None = None,
) -> TapeRead:
    """Every taker record for a market from ``since`` on (newest-first pages, overlapping).

    Raises ``TapeUnavailable`` if any page fails, the pages shift under us, a reply holds
    another market's trades, or ``since`` is deeper than the tape can be paged: a partial or
    wrong read would skip trades.
    """
    keys: set[tuple] = set()
    prints: list[TapePrint] = []
    newest: int | None = None
    skipped = 0
    offset = 0
    while True:
        try:
            resp = await client.get(
                f"{DATA_API}/trades",
                params={"market": condition_id, "limit": TAPE_PAGE, "offset": offset,
                        "takerOnly": "true"},
                headers=BROWSER_HEADERS, timeout=HTTP_TIMEOUT_S,
            )
            resp.raise_for_status()
            feed = resp.json()
        except Exception as exc:  # noqa: BLE001 - any failure means the read is incomplete
            raise TapeUnavailable(f"could not read the trade tape ({http_status(exc)})") from exc
        if not isinstance(feed, list):
            raise TapeUnavailable("the trade tape answered in an unexpected shape")
        overlapped = reached = False
        for rec in feed:
            key = _record_key(rec)
            if key is None:
                skipped += 1
                continue
            if key in keys:
                overlapped = True
                continue
            keys.add(key)
            if not _same_market(rec, condition_id):
                raise TapeUnavailable("the trade tape answered with another market's trades")
            ts = key[0]
            newest = ts if newest is None else max(newest, ts)
            if ts < since:
                reached = True
                continue
            tape_print = _print_of(rec, key, up_token=up_token, down_token=down_token)
            if tape_print is None:
                skipped += 1
            else:
                prints.append(tape_print)
        if offset > 0 and not overlapped:
            raise TapeUnavailable("the trade tape shifted while it was being read")
        if reached or len(feed) < TAPE_PAGE:
            break
        offset += TAPE_PAGE - TAPE_PAGE_OVERLAP
        if offset > TAPE_MAX_OFFSET:
            raise TapeUnavailable("the trade tape is too long to read back that far")
    prints.reverse()  # pages come newest first; keep the feed's order within one second
    prints.sort(key=lambda t: t.ts)
    return TapeRead(prints=tuple(prints), newest_ts=newest, skipped=skipped)


async def market_outcome(
    client: HttpClient,
    condition_id: str,
    *,
    up_token: str | None = None,
    down_token: str | None = None,
) -> str | None:
    """How a market resolved, from the venue's order-book service: "Up", "Down", or None while
    it has not. Never Gamma: Gamma drops ended 15m markets. Raises ``MarketUnavailable``."""
    try:
        resp = await client.get(
            f"{CLOB}/markets/{condition_id}", headers=BROWSER_HEADERS, timeout=HTTP_TIMEOUT_S
        )
        if getattr(resp, "status_code", 200) == 404:
            raise MarketUnavailable("the order-book service does not know this market")
        resp.raise_for_status()
        market = resp.json()
    except MarketUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MarketUnavailable(f"could not look up the result ({http_status(exc)})") from exc
    if not isinstance(market, Mapping):
        raise MarketUnavailable("the order-book service answered in an unexpected shape")
    if not market.get("closed"):
        return None
    winners: set[str] = set()
    for token in market.get("tokens") or []:
        if not isinstance(token, Mapping) or token.get("winner") is not True:
            continue
        tid = str(token.get("token_id") or "")
        if up_token and tid == up_token:
            winners.add("Up")
        elif down_token and tid == down_token:
            winners.add("Down")
        elif token.get("outcome") in SIDES:
            winners.add(str(token["outcome"]))
    if len(winners) > 1:
        raise MarketUnavailable("the order-book service marks both outcomes as winners")
    return winners.pop() if winners else None


async def tape_newest_ts(client: HttpClient, condition_id: str) -> int | None:
    """The time of the newest taker record on a market's tape (None if it has none). Raises
    ``TapeUnavailable`` if the tape cannot be read."""
    try:
        resp = await client.get(
            f"{DATA_API}/trades",
            params={"market": condition_id, "limit": FRESHNESS_PAGE, "offset": 0,
                    "takerOnly": "true"},
            headers=BROWSER_HEADERS, timeout=HTTP_TIMEOUT_S,
        )
        resp.raise_for_status()
        feed = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise TapeUnavailable(f"could not read the trade tape ({http_status(exc)})") from exc
    if not isinstance(feed, list):
        raise TapeUnavailable("the trade tape answered in an unexpected shape")
    newest: int | None = None
    for rec in feed:
        key = _record_key(rec)
        if key is None or not _same_market(rec, condition_id):
            continue
        newest = key[0] if newest is None else max(newest, key[0])
    return newest
