"""Order execution for Fade 1h Momentum on 15m: paper resting bids, their fills, settlement.

Paper and live share one pipeline: the strategy asks an :class:`Executor` to rest bids, cancel
them, bring fills up to date and settle, and never branches on the mode anywhere else. Only
the paper implementation exists. There is no live order path for this strategy: it is not
built, and live trading is not authorised for any market (AGENTS.md). :func:`choose_executor`
reads the operator's requested mode each pass and hands back ``None`` for LIVE, with a plain
message for the card, while fills and settlement of the paper bids already made keep running.

Every order is a resting limit bid. Nothing crosses the spread: a bid at or above the ask is
refused before anything is written, and there is no taker path (a hedge is a resting bid on the
other side). Every fill is therefore a resting fill at our own price, with no fee.

How a paper bid fills
---------------------
The authority is the venue's public trade tape (``data-api /trades?market=<id>&takerOnly=
true``): one record per taker order. Checked live on 2026-09-22:

- A taker order that sweeps several price levels is ONE record at its average price. So a
  record at or below our bid has already eaten everything bid above us, and the queue in front
  of a new bid is all the size resting at our price or better (:func:`queue_ahead`). A sweep
  whose average stays above our bid is not counted even if its last shares reached it, so if
  anything fills are under-counted.
- The venue's book for one outcome already contains the mirror of the other (an Up bid at 0.14
  is also shown as a Down ask at 0.86). A taker BUYING the other outcome at q is therefore a
  sale into our bids at 1 - q, and a taker SELLING our outcome at p is a sale at p (the rule of
  ``polymarket_bot.maker.filler.crossed_volume``). Every record is a sale into the bids of
  exactly one outcome, so one record can never fill both an entry and a hedge.
- The tape runs minutes behind (2-5 minutes seen, arriving in batches), and replies can come
  from copies at different points in time.

So, for each order, the tape is read from where its last read stopped (its cursor, first its
placement) up to when it stopped resting (its cancel, else its window end), and never past what
the tape can vouch for (see "How far the tape is trusted" below). Volume that reaches the
order's price first works through the queue that was in front of it when it was placed; only
the volume after that is ours, up to the order's size. When several of our own bids rest on one
outcome, a record reaches the highest bid first and a lower bid sees only what the higher ones
did not take: our paper bids are not in the real book, so one record must not fill two of them
beyond its size (:func:`allocate_fills`). The fill is stamped with the time of the record that
reached it, not the time we noticed.

How far the tape is trusted
---------------------------
The newest record in a reply marks how far that reply is complete: everything strictly older
is assumed to be in it (the tape is indexed in time order). Where a market is quiet, anything
older than ``max_tape_lag_s`` is assumed complete too. A read that fails, or whose pages
shifted while being read, moves no cursor, so nothing is ever skipped; the next pass reads the
same stretch again. Because the tape lags, a fill can come to light minutes after it happened,
including for a bid already cancelled by a requote: callers sizing a new bid must allow for
fills of recent bids that the tape has not shown yet.

Settlement
----------
Every ended window in the ledger is settled from the venue's order-book service
(``clob /markets/<id>``: ``closed`` plus the per-token ``winner`` flag, never Gamma), whether
or not anything was placed in it: the learner needs every window's result. The winner flag is
the venue's own call (for a 15m window: the Chainlink TWAP-60s print at the close against the
print at the open), so nothing here re-derives it from prices. A window waits while any of its
orders still has tape to read. After ``force_settle_after_s`` past the window end it is settled
with whatever tape was read, and the report says so. Lookups are capped per pass and go round
the due windows least recently tried first, so windows that never resolve cannot hold up the
rest.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import structlog

import config as _config  # type: ignore[import-untyped]
import db as _db  # type: ignore[import-untyped]
from polymarket_bot.fade_1h_momentum_15m import ledger as _ledger
from polymarket_bot.fade_1h_momentum_15m.ledger import (
    SIDES,
    FlowUpdate,
    NewOrder,
    PendingFlow,
    Settlement,
)
from polymarket_bot.maker.quoter import UA as BROWSER_HEADERS

log = structlog.get_logger(__name__)

DATA_API = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
# The operator's PAPER/LIVE choice, written by the dashboard (controller.set_mode). Read
# directly: importing the controller would pull in the BTC loop and the live executor.
MODE_KEY = "polymarket_bot.requested_mode"

TAPE_PAGE = 500
# Consecutive pages overlap by this many records. A page that shares none of them with the
# pages before it means the tape shifted under us (a reply from an older copy), so the read is
# thrown away rather than risk a gap.
TAPE_PAGE_OVERLAP = 50
TAPE_MAX_OFFSET = 5000
DEFAULT_MAX_TAPE_LAG_S = 900
DEFAULT_FORCE_SETTLE_AFTER_S = 3600
DEFAULT_MAX_SETTLE_PER_PASS = 40
# After a result lookup fails, that window waits this long before its next try, doubling on
# each further failure up to the ceiling. A window the venue has simply not resolved yet is
# not a failure and is tried again on the next pass.
SETTLE_RETRY_BASE_S = 60.0
SETTLE_RETRY_MAX_S = 1800.0
# Plain-English lists on the card name at most this many windows.
MAX_SLUGS_NAMED = 3
HTTP_TIMEOUT_S = 10.0

# Executor states, shown on the card.
PAPER_STATE = "paper"
LIVE_STATE = "live_not_authorised"
KILL_STATE = "kill_switch"
UNKNOWN_MODE_STATE = "mode_unknown"
RESTART_REASON = "restart"

_PRICE_EPS = 1e-9
_SHARES_EPS = 1e-9


# ---------------------------------------------------------------------------
# Errors and reports
# ---------------------------------------------------------------------------


class PlacementRefused(RuntimeError):
    """A bid was not placed, and nothing was written. ``reason`` is a short code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class TapeUnavailable(RuntimeError):
    """The trade tape could not be read in full this time. Nothing was moved."""


class MarketUnavailable(RuntimeError):
    """The venue's order-book service could not say how a market resolved."""


@dataclass(frozen=True)
class FillEvent:
    """Shares an order gained in one read."""

    order_id: int
    window_slug: str
    side: str
    kind: str
    price: float
    shares: float
    ts: int


@dataclass
class FillReport:
    """What one fill check did. ``errors`` are plain English, for the card."""

    markets_read: int = 0
    orders_updated: int = 0
    expired: int = 0
    fills: list[FillEvent] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def shares_filled(self) -> float:
        return sum(f.shares for f in self.fills)


@dataclass
class SettleReport:
    """What one settlement check did. ``errors`` are plain English, for the card."""

    settled: list[Settlement] = field(default_factory=list)
    waiting_resolution: int = 0  # ended, but the venue has not called a winner yet
    waiting_tape: int = 0  # orders still have trade tape to read; no lookup made
    retry_later: int = 0  # the last lookup failed; tried again after a pause
    no_market_id: int = 0  # ended windows whose market id was never recorded
    backlog: int = 0  # windows ready for a lookup but left for the next pass (per-pass cap)
    forced: list[str] = field(default_factory=list)  # settled with tape still unread
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The executor seam
# ---------------------------------------------------------------------------


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


@runtime_checkable
class Executor(Protocol):
    """Where the strategy's orders go. Paper is the only implementation.

    Every method is safe to call on every pass. ``place_bid`` and ``requote`` only ever rest
    bids, and refuse (``PlacementRefused``) a bid that would cross the ask.
    """

    mode: str

    async def place_bid(
        self, order: NewOrder, *, best_ask: float | None, now: float | None = None
    ) -> int | None: ...

    async def requote(
        self,
        orders: Sequence[NewOrder],
        *,
        best_asks: Mapping[str, float | None],
        now: float | None = None,
        cancel_ids: Iterable[int] = (),
        cancel_reason: str = "requote",
    ) -> list[int | None]: ...

    async def cancel(
        self, order_ids: Iterable[int], *, reason: str, now: float | None = None
    ) -> int: ...

    async def sync_fills(self, *, now: float | None = None) -> FillReport: ...

    async def settle(self, *, now: float | None = None) -> SettleReport: ...


# ---------------------------------------------------------------------------
# Queue maths (pure)
# ---------------------------------------------------------------------------


def queue_ahead(bid_levels: Iterable[tuple[float, float]], price: float) -> float:
    """Shares resting at ``price`` or better on our outcome's bid side: the queue in front of a
    new bid there. ``bid_levels`` are (price, size) pairs from that outcome's book."""
    return sum(float(size) for px, size in bid_levels if float(px) >= price - _PRICE_EPS)


@dataclass(frozen=True)
class TapePrint:
    """One taker trade: the outcome whose token was traded, and the taker's side."""

    ts: int
    outcome: str
    side: str
    size: float
    price: float

    def hits(self) -> tuple[str, float]:
        """The outcome whose bids this trade sold into, and the price for that outcome.

        A taker selling an outcome at p sells into its bids at p. A taker buying the other
        outcome at q is the same sale at 1 - q: the two outcomes share one book.
        """
        if self.side == "SELL":
            return self.outcome, self.price
        return _other(self.outcome), 1.0 - self.price


@dataclass(frozen=True)
class QueuedBid:
    """One resting bid as the fill allocation sees it.

    ``crossed`` is the volume that has reached this bid's price since it was placed (after our
    own higher bids took their fills) and ``filled`` its shares so far. The stretch of tape it
    reads now is [flow_from, flow_to).
    """

    order_id: int
    side: str
    price: float
    shares: float
    depth_ahead: float
    flow_from: int
    flow_to: int
    crossed: float = 0.0
    filled: float = 0.0
    placed_ts: int = 0


@dataclass(frozen=True)
class BidFlow:
    """Where one bid stands after a read. ``added`` shares came from this read, the first of
    them at ``fill_ts``."""

    order_id: int
    crossed: float
    filled: float
    added: float
    fill_ts: int | None


def allocate_fills(prints: Sequence[TapePrint], bids: Sequence[QueuedBid]) -> dict[int, BidFlow]:
    """Run the tape through our resting bids, oldest record first.

    Each record sells into one outcome's bids. It reaches our highest bid on that outcome
    first; whatever that bid takes is gone before the next one down sees it. A bid counts a
    record only if the record's price for its outcome is at or below the bid, and only inside
    the bid's own stretch of tape. The queue in front of a bid is worked through before any of
    the volume is its own.
    """
    crossed = {b.order_id: float(b.crossed) for b in bids}
    filled = {b.order_id: float(b.filled) for b in bids}
    fill_ts: dict[int, int] = {}
    ladders: dict[str, list[QueuedBid]] = {side: [] for side in SIDES}
    for b in bids:
        if b.side not in ladders:
            raise ValueError(f"order {b.order_id}: side must be one of {SIDES}")
        ladders[b.side].append(b)
    for ladder in ladders.values():
        ladder.sort(key=lambda b: (-b.price, b.placed_ts, b.order_id))

    for p in sorted(prints, key=lambda t: t.ts):
        outcome, px = p.hits()
        available = float(p.size)
        for b in ladders.get(outcome, ()):
            if available <= _SHARES_EPS:
                break
            if not b.flow_from <= p.ts < b.flow_to or px > b.price + _PRICE_EPS:
                continue
            reached = crossed[b.order_id] + available
            ours = min(float(b.shares), max(0.0, reached - float(b.depth_ahead)))
            take = min(available, max(0.0, ours - filled[b.order_id]))
            crossed[b.order_id] = reached
            if take > _SHARES_EPS:
                filled[b.order_id] += take
                fill_ts.setdefault(b.order_id, p.ts)
                available -= take

    return {
        b.order_id: BidFlow(
            order_id=b.order_id,
            crossed=crossed[b.order_id],
            filled=filled[b.order_id],
            added=max(0.0, filled[b.order_id] - float(b.filled)),
            fill_ts=fill_ts.get(b.order_id),
        )
        for b in bids
    }


def _other(side: str) -> str:
    return "Down" if side == "Up" else "Up"


def _count_windows(slugs: Sequence[str]) -> str:
    return "1 window" if len(slugs) == 1 else f"{len(slugs)} windows"


def _name_some(slugs: Sequence[str]) -> str:
    """The first few window names, and how many more, for a line on the card."""
    shown = ", ".join(slugs[:MAX_SLUGS_NAMED])
    rest = len(slugs) - MAX_SLUGS_NAMED
    return f"{shown} and {rest} more" if rest > 0 else shown


def _duration(seconds: float) -> str:
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, rest = divmod(seconds, 60)
    return f"{minutes} min" if not rest else f"{minutes} min {rest} s"


# ---------------------------------------------------------------------------
# Reading the venue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TapeRead:
    """The tape for one market from ``since`` on, oldest first."""

    prints: tuple[TapePrint, ...]
    newest_ts: int | None  # the newest record in the reply, however old
    skipped: int  # records that could not be read or were not for this market


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _record_key(rec: Any) -> tuple | None:
    """A record's identity, from parsed numbers (the tape writes 10 and 10.0 alike)."""
    if not isinstance(rec, Mapping):
        return None
    ts = _float(rec.get("timestamp"))
    size = _float(rec.get("size"))
    price = _float(rec.get("price"))
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


def _status(exc: BaseException) -> str:
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
            raise TapeUnavailable(f"could not read the trade tape ({_status(exc)})") from exc
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
        raise MarketUnavailable(f"could not look up the result ({_status(exc)})") from exc
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


# ---------------------------------------------------------------------------
# Paper book-keeping: runs in every mode
# ---------------------------------------------------------------------------


def _now(now: float | None) -> float:
    return time.time() if now is None else float(now)


class PaperBookkeeper:
    """Brings paper fills up to date and settles windows. Mode-independent: paper bids already
    placed keep filling and settling whatever the operator selects, as the strategy switch's
    contract requires ("off means open nothing new, never abandon").

    Every cursor lives in the ledger, so a restart loses nothing. The only memory kept here is
    when each unsettled window's result was last looked up, so that the per-pass cap on
    lookups goes round every due window instead of being used up by the same old ones. Keep
    one bookkeeper for the life of the runner (pass it to :func:`choose_executor`).
    """

    def __init__(
        self,
        client: HttpClient,
        *,
        max_tape_lag_s: float = DEFAULT_MAX_TAPE_LAG_S,
        force_settle_after_s: float = DEFAULT_FORCE_SETTLE_AFTER_S,
        max_settle_per_pass: int = DEFAULT_MAX_SETTLE_PER_PASS,
    ) -> None:
        if max_tape_lag_s < 0 or force_settle_after_s < 0 or max_settle_per_pass < 1:
            raise ValueError("tape lag and force delay must be >= 0, settle cap >= 1")
        self._client = client
        self.max_tape_lag_s = float(max_tape_lag_s)
        self.force_settle_after_s = float(force_settle_after_s)
        self.max_settle_per_pass = int(max_settle_per_pass)
        # window slug -> when its result was last looked up / how many lookups failed in a row
        self._looked_up: dict[str, float] = {}
        self._failures: dict[str, int] = {}

    async def recover_after_restart(self, *, now: float | None = None) -> int:
        """Stop every bid left resting by a previous run. Their tape up to now is still read
        (by ``sync_fills``), so fills made before the restart are kept."""
        cancelled = await _ledger.cancel_all_open(ts=_now(now), reason=RESTART_REASON)
        if cancelled:
            log.info("fade1h.restart_cancelled", orders=cancelled)
        return cancelled

    async def sync_fills(self, *, now: float | None = None) -> FillReport:
        """Expire bids in ended windows, then read each market's tape and record fills."""
        t = _now(now)
        report = FillReport()
        report.expired = await _ledger.expire_due(t)
        rows = await _ledger.orders_needing_flow()
        markets: dict[str, list[dict]] = {}
        for row in rows:
            cid = row.get("condition_id")
            if not cid:
                slug = row["window_slug"]
                message = f"{slug}: no market id recorded, so its trade tape cannot be read."
                if message not in report.errors:
                    report.errors.append(message)
                continue
            markets.setdefault(str(cid), []).append(row)
        for cid, group in markets.items():
            try:
                await self._sync_market(cid, group, t, report)
            except Exception as exc:  # noqa: BLE001 - one market must not stop the others
                slug = group[0]["window_slug"]
                reason = (
                    str(exc) if isinstance(exc, TapeUnavailable)
                    else f"the fill check failed ({type(exc).__name__}: {exc})"
                )
                report.errors.append(f"{slug}: {reason}. Its fills are checked again next pass.")
                log.warning("fade1h.fill_check_failed", window=slug, error=str(exc))
        return report

    async def _sync_market(
        self, cid: str, rows: list[dict], now: float, report: FillReport
    ) -> None:
        first = rows[0]
        tape = await read_taker_tape(
            self._client, cid, since=min(int(r["flow_from"]) for r in rows),
            up_token=first.get("up_token"), down_token=first.get("down_token"),
        )
        report.markets_read += 1
        if tape.skipped:
            report.errors.append(
                f"{first['window_slug']}: {tape.skipped} trade record(s) could not be read and "
                "were left out, so fills there may be under-counted."
            )
            log.warning("fade1h.tape_records_skipped", window=first["window_slug"],
                        skipped=tape.skipped)
        clock = int(math.floor(now))
        horizon = clock - int(self.max_tape_lag_s)
        if tape.newest_ts is not None:
            horizon = max(horizon, tape.newest_ts)
        horizon = min(horizon, clock)

        bids = [
            QueuedBid(
                order_id=int(r["id"]), side=str(r["side"]), price=float(r["price"]),
                shares=float(r["shares"]), depth_ahead=float(r["depth_ahead"] or 0.0),
                flow_from=int(r["flow_from"]), flow_to=min(horizon, int(r["flow_until"])),
                crossed=float(r["crossed"] or 0.0), filled=float(r["filled_shares"] or 0.0),
                placed_ts=int(r["placed_ts"]),
            )
            for r in rows
        ]
        moving = [b for b in bids if b.flow_to > b.flow_from]
        if not moving:
            return
        flows = allocate_fills(tape.prints, moving)
        updates = [
            FlowUpdate(
                order_id=b.order_id, cursor_ts=b.flow_to, crossed=flows[b.order_id].crossed,
                add_shares=flows[b.order_id].added, fill_ts=flows[b.order_id].fill_ts,
            )
            for b in moving
        ]
        result = await _ledger.record_flow(updates)
        report.orders_updated += result.updated
        by_id = {int(r["id"]): r for r in rows}
        for b in moving:
            flow = flows[b.order_id]
            if flow.added <= _SHARES_EPS or flow.fill_ts is None:
                continue
            row = by_id[b.order_id]
            event = FillEvent(
                order_id=b.order_id, window_slug=str(row["window_slug"]), side=b.side,
                kind=str(row["kind"]), price=b.price, shares=flow.added, ts=flow.fill_ts,
            )
            report.fills.append(event)
            log.info(
                "fade1h.filled", window=event.window_slug, side=event.side, kind=event.kind,
                price=event.price, shares=round(event.shares, 2), at=event.ts,
                queue=round(b.depth_ahead, 1), crossed=round(flow.crossed, 1),
            )

    def _retry_at(self, slug: str) -> float:
        """When a window whose lookups failed may be looked up again (-inf: any time)."""
        failures = self._failures.get(slug, 0)
        if not failures:
            return -math.inf
        pause = min(SETTLE_RETRY_MAX_S, SETTLE_RETRY_BASE_S * 2.0 ** (failures - 1))
        return self._looked_up.get(slug, -math.inf) + pause

    def _forget(self, slug: str) -> None:
        self._looked_up.pop(slug, None)
        self._failures.pop(slug, None)

    async def settle(self, *, now: float | None = None) -> SettleReport:
        """Settle every ended window the venue has resolved, traded or not.

        Windows still waiting for trade tape, or with no market id, cost no lookup and do not
        count against the per-pass cap. The rest are looked up least recently tried first
        (never tried first of all), so windows that stay unresolved or keep failing cannot
        crowd out newer ones; a window whose lookup failed waits a growing pause first.
        """
        t = _now(now)
        report = SettleReport()
        due = await _ledger.settlement_due(t)
        due_slugs = {str(w["window_slug"]) for w in due}
        for slug in [s for s in self._looked_up if s not in due_slugs]:
            self._forget(slug)  # settled, or gone from the ledger

        no_market: list[str] = []
        paused: list[str] = []
        ready: list[dict] = []
        for window in due:
            slug = str(window["window_slug"])
            if not window.get("condition_id"):
                no_market.append(slug)
            elif int(window.get("pending_flow") or 0) and not self._give_up(window, t):
                report.waiting_tape += 1
            elif t < self._retry_at(slug):
                paused.append(slug)
            else:
                ready.append(window)
        if no_market:
            report.no_market_id = len(no_market)
            report.errors.append(
                f"{_count_windows(no_market)} ended with no market id recorded "
                f"({_name_some(no_market)}), so the result cannot be looked up and "
                f"{'it stays' if len(no_market) == 1 else 'they stay'} unsettled."
            )
        if paused:
            report.retry_later = len(paused)
            report.errors.append(
                f"{_count_windows(paused)} could not be looked up last time "
                f"({_name_some(paused)}) and {'is' if len(paused) == 1 else 'are'} tried "
                "again after a pause."
            )

        ready.sort(key=lambda w: (
            self._looked_up.get(str(w["window_slug"]), -math.inf),
            float(w["window_end"]), str(w["window_slug"]),
        ))
        report.backlog = max(0, len(ready) - self.max_settle_per_pass)
        for window in ready[: self.max_settle_per_pass]:
            slug = str(window["window_slug"])
            try:
                await self._settle_one(window, t, report)
            except Exception as exc:  # noqa: BLE001 - one window must not stop the others
                report.errors.append(
                    f"{slug}: settling failed ({type(exc).__name__}: {exc}). "
                    "Tried again next pass."
                )
                log.warning("fade1h.settle_failed", window=slug, error=str(exc))
        return report

    def _give_up(self, window: Mapping[str, Any], now: float) -> bool:
        """True once a window's unread tape has waited long enough to settle without it."""
        return now >= float(window["window_end"]) + self.force_settle_after_s

    async def _settle_one(self, window: dict, now: float, report: SettleReport) -> None:
        slug = str(window["window_slug"])
        pending = int(window.get("pending_flow") or 0)
        self._looked_up[slug] = now
        try:
            outcome = await market_outcome(
                self._client, str(window["condition_id"]), up_token=window.get("up_token"),
                down_token=window.get("down_token"),
            )
        except MarketUnavailable as exc:
            self._failures[slug] = self._failures.get(slug, 0) + 1
            pause = self._retry_at(slug) - now
            report.errors.append(f"{slug}: {exc}. Tried again in {_duration(pause)}.")
            log.warning("fade1h.settle_lookup_failed", window=slug, error=str(exc),
                        failures=self._failures[slug])
            return
        self._failures.pop(slug, None)
        if outcome is None:
            report.waiting_resolution += 1
            return
        try:
            settlement = await _ledger.settle_window(
                slug, outcome=outcome, ts=now, force=bool(pending) and self._give_up(window, now)
            )
        except PendingFlow:
            report.waiting_tape += 1
            return
        self._forget(slug)
        if settlement is None:
            return
        report.settled.append(settlement)
        if settlement.forced_pending:
            report.forced.append(slug)
            report.errors.append(
                f"{slug}: settled with {settlement.forced_pending} bid(s) whose trade tape "
                "could not be read in full, so their fills may be under-counted."
            )
            log.warning("fade1h.settled_forced", window=slug, pending=settlement.forced_pending)
        log.info(
            "fade1h.settled", window=slug, outcome=outcome, pnl=round(settlement.net_pnl, 4),
            orders=settlement.orders, shares=round(settlement.filled_shares, 2),
        )


# ---------------------------------------------------------------------------
# The paper executor
# ---------------------------------------------------------------------------


def _kill_path(path: Path | str | None) -> Path:
    return Path(path) if path is not None else Path(_config.KILL_SWITCH_PATH)


class PaperExecutor:
    """Paper resting bids: written to the ledger, filled only by the real trade tape."""

    mode = "paper"

    def __init__(
        self,
        client: HttpClient,
        *,
        bookkeeper: PaperBookkeeper | None = None,
        kill_switch_path: Path | str | None = None,
    ) -> None:
        self.bookkeeper = bookkeeper or PaperBookkeeper(client)
        self._kill_switch_path = kill_switch_path

    def _kill_active(self) -> bool:
        try:
            return _kill_path(self._kill_switch_path).exists()
        except OSError:
            return True  # cannot tell: place nothing

    async def place_bid(
        self, order: NewOrder, *, best_ask: float | None, now: float | None = None
    ) -> int | None:
        """Rest one bid. ``best_ask`` is the lowest ask on the order's own token (None when
        nobody is offering it). Returns the order id, or None if it was already placed."""
        (order_id,) = await self.requote([order], best_asks={order.token_id: best_ask}, now=now)
        return order_id

    async def requote(
        self,
        orders: Sequence[NewOrder],
        *,
        best_asks: Mapping[str, float | None],
        now: float | None = None,
        cancel_ids: Iterable[int] = (),
        cancel_reason: str = "requote",
    ) -> list[int | None]:
        """Cancel ``cancel_ids`` and rest ``orders`` in one transaction.

        ``best_asks`` holds the lowest ask for every token bid on (None: no asks). Every bid
        must rest strictly below it. Refuses the whole batch with ``PlacementRefused``,
        writing nothing (the cancels included), if any bid would cross, if an order is not a
        ``NewOrder``, if the kill switch file is present, if a bid's window has no market id
        (its fills could never be read from the tape), or if the ledger refuses it (window
        unknown or over, or the token is not that side's token).
        """
        batch = list(orders)
        if batch:
            if self._kill_active():
                raise PlacementRefused(
                    KILL_STATE, "The kill switch is on, so no new bids are placed."
                )
            for order in batch:
                if not isinstance(order, NewOrder):
                    raise PlacementRefused("bad_order", "Only NewOrder bids can be placed.")
                if order.token_id not in best_asks:
                    raise PlacementRefused(
                        "no_ask", f"No best ask was given for token {order.token_id}, so the "
                        "bid cannot be checked against the spread."
                    )
                ask = best_asks[order.token_id]
                if ask is None:
                    continue
                ask_value = _float(ask)
                if ask_value is None or not 0.0 < ask_value <= 1.0:
                    raise PlacementRefused(
                        "bad_ask", f"The best ask {ask!r} for token {order.token_id} is not a "
                        "price."
                    )
                if order.price >= ask_value - _PRICE_EPS:
                    raise PlacementRefused(
                        "would_cross",
                        f"A bid at {order.price:.3f} would meet the ask at {ask_value:.3f}; "
                        "bids only ever rest below the ask.",
                    )
            for slug in dict.fromkeys(order.window_slug for order in batch):
                window = await _ledger.get_window(slug)
                # A window's market id is only ever added, never removed, so this check
                # cannot go stale before the placement below.
                if window is not None and not window.get("condition_id"):
                    raise PlacementRefused(
                        "no_market_id", f"{slug}: no market id recorded, so a bid there could "
                        "never be filled from the trade tape."
                    )
        try:
            return await _ledger.place_orders(
                batch, ts=_now(now), cancel_ids=cancel_ids, cancel_reason=cancel_reason
            )
        except ValueError as exc:  # the ledger wrote nothing
            raise PlacementRefused("ledger_refused", str(exc)) from exc

    async def cancel(
        self, order_ids: Iterable[int], *, reason: str, now: float | None = None
    ) -> int:
        return await _ledger.cancel_orders(order_ids, ts=_now(now), reason=reason)

    async def sync_fills(self, *, now: float | None = None) -> FillReport:
        return await self.bookkeeper.sync_fills(now=now)

    async def settle(self, *, now: float | None = None) -> SettleReport:
        return await self.bookkeeper.settle(now=now)


# ---------------------------------------------------------------------------
# Choosing the executor from the requested mode
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutorChoice:
    """This pass's executor. ``executor`` is None when no new bids may be placed; the
    bookkeeper keeps filling and settling the paper bids already made in every state."""

    requested_mode: str
    state: str
    message: str
    executor: Executor | None
    bookkeeper: PaperBookkeeper

    @property
    def can_place(self) -> bool:
        return self.executor is not None


async def requested_mode() -> str:
    """The operator's PAPER/LIVE selection (the dashboard's toggle), lower case."""
    raw = await _db.get_config(MODE_KEY, _config.BOT_MODE)
    return str(raw or "paper").strip().lower()


async def choose_executor(
    client: HttpClient,
    *,
    bookkeeper: PaperBookkeeper | None = None,
    kill_switch_path: Path | str | None = None,
) -> ExecutorChoice:
    """Pick this pass's executor from the requested mode and the kill switch.

    Pass the runner's one long-lived ``bookkeeper`` on every call: it remembers which
    windows' results were looked up lately, and a fresh one each pass would forget that.

    - kill switch file present: none (no new bids).
    - paper: ``PaperExecutor``.
    - live: none. No live order path exists for this strategy and live trading is not
      authorised, so the card says so and nothing new is placed.
    - anything else, or the mode cannot be read: none.
    """
    keeper = bookkeeper or PaperBookkeeper(client)
    try:
        mode = await requested_mode()
    except Exception as exc:  # noqa: BLE001 - fail closed, and say why
        log.warning("fade1h.mode_read_failed", error=str(exc))
        return ExecutorChoice(
            "unknown", UNKNOWN_MODE_STATE,
            f"Could not read the PAPER/LIVE selection ({type(exc).__name__}), so no new bids "
            "are placed this pass. Bids already filled keep settling.", None, keeper,
        )
    path = _kill_path(kill_switch_path)
    try:
        killed = path.exists()
    except OSError:
        killed = True
    if killed:
        return ExecutorChoice(
            mode, KILL_STATE,
            f"The kill switch file is present ({path}). No new bids, and resting bids are "
            "cancelled. Bids already filled keep settling.", None, keeper,
        )
    if mode == "paper":
        return ExecutorChoice(
            mode, PAPER_STATE,
            "Paper trading: bids rest only in this app's ledger and fill only when the real "
            "trade tape reaches them.",
            PaperExecutor(client, bookkeeper=keeper, kill_switch_path=kill_switch_path),
            keeper,
        )
    if mode == "live":
        return ExecutorChoice(
            mode, LIVE_STATE,
            "LIVE is selected, but this strategy has no live order path: it is not built, and "
            "live trading is not authorised for any market. No new bids are placed; paper "
            "bids already filled keep settling.", None, keeper,
        )
    return ExecutorChoice(
        mode, UNKNOWN_MODE_STATE,
        f"The selected mode {mode!r} is not PAPER or LIVE, so no new bids are placed. Bids "
        "already filled keep settling.", None, keeper,
    )


async def stand_down(choice: ExecutorChoice, *, now: float | None = None) -> int:
    """When this pass may not place bids, cancel every paper bid still resting (a resting bid
    that filled later would open a position). Returns how many were cancelled."""
    if choice.can_place:
        return 0
    cancelled = await _ledger.cancel_all_open(ts=_now(now), reason=choice.state)
    if cancelled:
        log.info("fade1h.stood_down", state=choice.state, orders=cancelled)
    return cancelled
