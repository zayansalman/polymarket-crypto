"""Reads from the venue that every strategy's fills and results rest on.

- The trade tape (``data-api /trades?market=<id>``). :func:`read_fill_tape` reads every trade
  from a second on, price level by price level where the venue lists the levels (its maker
  records), and is what fills resting orders (``ems.execution.queue``). :func:`read_taker_tape`
  reads the taker records alone (``takerOnly=true``), and :func:`tape_newest_ts` how far the
  tape has got.
- A market's result from the venue's order-book service (``clob /markets/<id>``: ``closed``
  plus the per-token ``winner`` flag, never Gamma, which drops ended 15m markets):
  :func:`market_outcome`.

A read that could be partial or wrong raises (``TapeUnavailable``, ``MarketUnavailable``)
rather than return something that would skip trades or call a result early.
"""

from __future__ import annotations

import dataclasses
import math
from collections import Counter
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

# The venue's limits: 1,000 records a page, offsets up to 10,000 (10,500 is refused). That
# reaches about 10,500 records back; the busiest BTC 15m window seen had 7,017 taker records.
# The combined list (takerOnly=false) holds about 2.8 records per trade, so it reaches only
# about 3,700 trades back.
TAPE_PAGE = 1000
# Consecutive pages overlap by this many records. A page that shares none of them with the
# pages before it means the tape shifted under us (a reply from an older copy), so the read is
# thrown away rather than risk a gap.
TAPE_PAGE_OVERLAP = 50
TAPE_MAX_OFFSET = 10_000
# Records asked for when a market's tape is read only to see how far the tape has got.
FRESHNESS_PAGE = 20
HTTP_TIMEOUT_S = 10.0
# A trade's maker records add up to its size; the venue writes sizes to 1e-6 of a share.
LEG_SIZE_TOLERANCE = 1e-6


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
    # Trades older than ``newest_ts`` read at their average price: their levels were not read.
    # (A newer one can fill only if the quiet-stretch rule vouches past ``newest_ts``, which
    # takes a combined copy more than ``max_tape_lag_s`` behind; it is not counted.) Every
    # such trade, counted or not, is marked on its print (``TapePrint.averaged``).
    averaged: int = 0


@dataclass(frozen=True)
class _Feed:
    """One list of the tape from ``since`` on, as (key, record) in the venue's order (newest
    first), with how far it reaches and the records that could not be read."""

    records: tuple[tuple[tuple, Mapping[str, Any]], ...]
    newest: int | None
    skipped: int


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


def _maker_print(
    rec: Mapping[str, Any], key: tuple, *, up_token: str | None, down_token: str | None
) -> TapePrint | None:
    """A maker record as the print of the sale it stands for, by flipping its side: a maker
    BUY of O at p is a sale into O's bids at p (a taker SELL of O at p), and a maker SELL of O
    at p a sale into the other outcome's bids at 1 - p (a taker BUY of O at p)."""
    maker = _print_of(rec, key, up_token=up_token, down_token=down_token)
    if maker is None:
        return None
    return dataclasses.replace(maker, side="SELL" if maker.side == "BUY" else "BUY")


def _legs_add_up(taker: TapePrint, legs: list[TapePrint]) -> bool:
    """True when these are all of a trade's maker records: each in the trade's second, selling
    into the same outcome's bids, and together the trade's size."""
    outcome = taker.hits()[0]
    if not legs or any(leg.ts != taker.ts or leg.hits()[0] != outcome for leg in legs):
        return False
    return abs(sum(leg.size for leg in legs) - taker.size) <= LEG_SIZE_TOLERANCE


def http_status(exc: BaseException) -> str:
    """A failed request in a few words: its HTTP status, else the exception's type."""
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return f"HTTP {code}" if code else type(exc).__name__


async def _read_feed(
    client: HttpClient, condition_id: str, *, since: int, taker_only: bool,
    cut_short: bool = False,
) -> _Feed:
    """Every record of one list of a market's tape from ``since`` on (newest-first pages,
    overlapping).

    Raises ``TapeUnavailable`` if any page fails, the pages shift under us, or a reply holds
    another market's trades. A list deeper than the venue pages raises too, unless
    ``cut_short``: then the records that could be paged are returned, newest first, and the
    older ones are left out. One maker can have several identical records in one trade (the
    same price and size), so a record is taken as already read only as many times as a
    page before showed it.
    """
    seen: dict[tuple, int] = {}
    records: list[tuple[tuple, Mapping[str, Any]]] = []
    newest: int | None = None
    skipped = 0
    offset = 0
    while True:
        try:
            resp = await client.get(
                f"{DATA_API}/trades",
                params={"market": condition_id, "limit": TAPE_PAGE, "offset": offset,
                        "takerOnly": "true" if taker_only else "false"},
                headers=BROWSER_HEADERS, timeout=HTTP_TIMEOUT_S,
            )
            resp.raise_for_status()
            feed = resp.json()
        except Exception as exc:  # noqa: BLE001 - any failure means the read is incomplete
            raise TapeUnavailable(f"could not read the trade tape ({http_status(exc)})") from exc
        if not isinstance(feed, list):
            raise TapeUnavailable("the trade tape answered in an unexpected shape")
        overlapped = reached = False
        copies: dict[tuple, int] = {}
        for rec in feed:
            key = _record_key(rec)
            if key is None:
                skipped += 1
                continue
            copies[key] = copies.get(key, 0) + 1
            if copies[key] <= seen.get(key, 0):
                overlapped = True  # read on an earlier page
                continue
            seen[key] = copies[key]
            if not _same_market(rec, condition_id):
                raise TapeUnavailable("the trade tape answered with another market's trades")
            ts = key[0]
            newest = ts if newest is None else max(newest, ts)
            if ts < since:
                reached = True
                continue
            records.append((key, rec))
        if offset > 0 and not overlapped:
            raise TapeUnavailable("the trade tape shifted while it was being read")
        if reached or len(feed) < TAPE_PAGE:
            break
        offset += TAPE_PAGE - TAPE_PAGE_OVERLAP
        if offset > TAPE_MAX_OFFSET:
            if cut_short:
                break
            raise TapeUnavailable("the trade tape is too long to read back that far")
    return _Feed(records=tuple(records), newest=newest, skipped=skipped)


def _oldest_first(units: list[list[TapePrint]]) -> tuple[TapePrint, ...]:
    """Prints read newest first, oldest first: pages come newest first, and the feed's order
    within one second is kept."""
    units.reverse()
    units.sort(key=lambda unit: unit[0].ts)
    return tuple(p for unit in units for p in unit)


async def read_taker_tape(
    client: HttpClient,
    condition_id: str,
    *,
    since: int,
    up_token: str | None = None,
    down_token: str | None = None,
) -> TapeRead:
    """Every taker record for a market from ``since`` on (``takerOnly=true``), each at its
    average price.

    Raises ``TapeUnavailable`` if any page fails, the pages shift under us, a reply holds
    another market's trades, or ``since`` is deeper than the tape can be paged: a partial or
    wrong read would skip trades.
    """
    feed = await _read_feed(client, condition_id, since=since, taker_only=True)
    units: list[list[TapePrint]] = []
    skipped = feed.skipped
    for key, rec in feed.records:
        tape_print = _print_of(rec, key, up_token=up_token, down_token=down_token)
        if tape_print is None:
            skipped += 1
        else:
            units.append([tape_print])
    return TapeRead(prints=_oldest_first(units), newest_ts=feed.newest, skipped=skipped)


async def read_fill_tape(
    client: HttpClient,
    condition_id: str,
    *,
    since: int,
    up_token: str | None = None,
    down_token: str | None = None,
) -> TapeRead:
    """Every trade on a market from ``since`` on, as the prints that fill resting orders: one
    per price level a trade reached, where the venue's lists show it.

    Two reads of the same endpoint. The taker list (``takerOnly=true``) says which trades
    happened and who took; it is read in full or not at all, as :func:`read_taker_tape`. The
    combined list (``takerOnly=false``) holds those records again plus the maker records each
    trade met, each at its maker's own price; a record in it that is not in the taker list is
    a maker record. It is read only when there is a trade to fill from.

    - A trade whose maker records were all read (they add up to its size) becomes those
      records, best level first, at the trade's second: :func:`_maker_print`.
    - Any other trade stays its taker record, at its average price, marked ``averaged`` so the
      fill model never takes its average for a price level. That happens when its trade is
      newer than the combined copy, or older than the combined list can be paged back (about
      3,700 trades): a stretch that deep, after a restart or a long gap in the tape, is read
      this way rather than refused. ``averaged`` counts such trades older than ``newest_ts``,
      the ones that can fill orders from this read.

    A trade counts once, as its maker records or as itself, never both. The two lists are
    separate copies, so the reply is complete only up to the older of their newest records: a
    trade the combined copy has not reached yet waits for the next read. Raises
    ``TapeUnavailable`` as :func:`read_taker_tape` does, and if the combined list cannot be
    read.
    """
    taker = await _read_feed(client, condition_id, since=since, taker_only=True)
    trades: list[tuple[tuple, TapePrint]] = []
    skipped = taker.skipped
    for key, rec in taker.records:
        tape_print = _print_of(rec, key, up_token=up_token, down_token=down_token)
        if tape_print is None:
            skipped += 1
        else:
            trades.append((key, tape_print))
    if not trades:
        return TapeRead(prints=(), newest_ts=taker.newest, skipped=skipped)

    combined = await _read_feed(client, condition_id, since=since, taker_only=False,
                                cut_short=True)
    takers = Counter(key for key, _ in taker.records)
    per_trade = Counter(key[1] for key, _ in taker.records)
    legs: dict[str, list[TapePrint]] = {}
    unreadable: set[str] = set()  # trades with a maker record that could not be read
    for key, rec in combined.records:
        if takers[key] > 0:
            takers[key] -= 1  # the taker record itself
            continue
        leg = _maker_print(rec, key, up_token=up_token, down_token=down_token)
        if leg is None:
            unreadable.add(key[1])
        else:
            legs.setdefault(key[1], []).append(leg)
    newest = (min(taker.newest, combined.newest)
              if taker.newest is not None and combined.newest is not None else None)

    units: list[list[TapePrint]] = []
    averaged = 0
    for key, tape_print in trades:
        tx = key[1]
        # A transaction with two taker records cannot say which maker met which: both stay at
        # their average (not seen on the venue).
        own = legs.get(tx, []) if tx and per_trade[tx] == 1 and tx not in unreadable else []
        if _legs_add_up(tape_print, own):
            units.append(sorted(own, key=lambda leg: -leg.hits()[1]))  # best level first
        else:
            units.append([dataclasses.replace(tape_print, averaged=True)])
            if newest is not None and tape_print.ts < newest:
                averaged += 1
    return TapeRead(prints=_oldest_first(units), newest_ts=newest, skipped=skipped,
                    averaged=averaged)


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
