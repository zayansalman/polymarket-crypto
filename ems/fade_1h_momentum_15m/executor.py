"""Order execution for Fade 1h Momentum on 15m: paper child orders, their fills, settlement.

Paper and live share one pipeline: the strategy asks an :class:`Executor` to rest orders,
cancel them, bring fills up to date and settle, and never branches on the mode anywhere else.
Only the paper implementation exists. There is no live order path for this strategy: it is not
built, and live trading is not authorised for any market (AGENTS.md). :func:`choose_executor`
reads the operator's requested mode each pass and hands back ``None`` for LIVE, with a plain
message for the card, while fills and settlement of the paper orders already made keep
running.

Every order is a passive limit order. Nothing crosses the spread: a buy at or above the best
ask, or a sell at or below the best bid, is refused before anything is written, and there is
no taker path. A parent order is split into child orders, one per price level; each is a row
of its own. An entry buys one outcome's token. A hedge SELLS shares of that token already held
(never more than are held and not already offered), so the strategy never holds both outcomes.
Every fill is a passive fill at our own price, with no fee.

How a paper order fills
-----------------------
By the venue's public taker trade tape, through the depth that was ahead of the order price
level by level. That fill model, the tape reader, the result lookup, the never-cross check and
the kill switch are shared by every strategy and live in ``ems/execution/`` (``queue.py``,
``tape.py``, ``controls.py``); this module keeps each order's cursor and settles the windows.

For each order the tape is read from where its last read stopped (its cursor, first its
placement) up to when it stopped resting (its cancel, else its window end), and never past what
the tape can vouch for (see "How far the tape is trusted" below).

Time
----
An order rests from the second after it is written (``ledger.place_orders``), and a cancel stops
it at the second it is written, so no trade already on the tape can fill an order that did not
exist yet, and no trade is credited to both a cancelled order and its replacement. The executor
reads its clock inside the ledger's write; pass ``now`` only to fix the time in a test.

How far the tape is trusted
---------------------------
The newest record in a reply marks how far that reply is complete: everything strictly older
is assumed to be in it (the tape is indexed in time order). A market with no newer record may
have had no trades, or its copy of the tape may be running behind; the clock alone never tells
the two apart. So a stretch after a market's newest record is taken as complete only when it
is at least ``max_tape_lag_s`` old AND some market's tape read in the same pass already holds a
record at least that new (the indexer has got past it). When no market read has one, the
newest window's market is read once for it. A read that fails, or whose pages shifted while
being read, moves no cursor, so nothing is ever skipped; the next pass reads the same stretch
again. Because the tape lags, a fill can come to light minutes after it happened, including for
an order already cancelled: callers sizing a new order must allow for fills of recent orders
that the tape has not shown yet.

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

import dataclasses
import math
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import structlog

from ems.execution import controls as _controls
from ems.execution.controls import PlacementRefused, requested_mode
from ems.execution.queue import QueuedOrder, allocate_fills, book_terms, own_terms
from ems.execution.tape import (
    HttpClient,
    MarketUnavailable,
    TapeRead,
    TapeUnavailable,
    market_outcome,
    read_taker_tape,
    tape_newest_ts,
)
# Moved to ems/execution/ (shared by every strategy); still importable from here.
from ems.execution.controls import MODE_KEY as MODE_KEY
from ems.execution.queue import TapePrint as TapePrint
from ems.execution.queue import queue_ahead as queue_ahead
from ems.execution.tape import CLOB as CLOB
from ems.execution.tape import DATA_API as DATA_API
from ems.execution.tape import FRESHNESS_PAGE as FRESHNESS_PAGE
from ems.execution.tape import TAPE_PAGE as TAPE_PAGE
from ems.execution.tape import TAPE_PAGE_OVERLAP as TAPE_PAGE_OVERLAP
from ems.fade_1h_momentum_15m import ledger as _ledger
from ems.fade_1h_momentum_15m.ledger import (
    FlowUpdate,
    NewOrder,
    PendingFlow,
    PlaceResult,
    Settlement,
)

log = structlog.get_logger(__name__)

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
# The venue takes sizes in hundredths of a share, and no order under 5 shares on these markets.
SHARE_STEP = 0.01
MIN_ORDER_SHARES = 5.0

# Executor states, shown on the card.
PAPER_STATE = "paper"
LIVE_STATE = "live_not_authorised"
KILL_STATE = "kill_switch"
UNKNOWN_MODE_STATE = "mode_unknown"
RESTART_REASON = "restart"

_SHARES_EPS = 1e-9

Clock = Callable[[], float]


# ---------------------------------------------------------------------------
# Errors and reports
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FillEvent:
    """Shares an order gained in one read. ``side`` is the order's outcome and ``price`` its
    own price; ``order_side`` says whether shares were bought or sold."""

    order_id: int
    window_slug: str
    side: str
    kind: str
    price: float
    shares: float
    ts: int
    order_side: str = "BUY"


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


@dataclass(frozen=True)
class OrderChanges:
    """What it takes to bring the resting child orders in line with the plan: the ids to
    keep (with their place in the queue), the ids to cancel, and the child orders to add."""

    keep: tuple[int, ...]
    cancel: tuple[int, ...]
    place: tuple[NewOrder, ...]


@dataclass(frozen=True)
class Reconciled:
    changes: OrderChanges
    result: PlaceResult


# ---------------------------------------------------------------------------
# The executor seam
# ---------------------------------------------------------------------------


@runtime_checkable
class Executor(Protocol):
    """Where the strategy's orders go. Paper is the only implementation.

    Every method is safe to call on every pass. Orders only ever rest: ``place_bid``,
    ``place_sell``, ``requote`` and ``reconcile`` refuse (``PlacementRefused``) a buy that would
    meet the ask or a sell that would meet the bid.
    """

    mode: str

    async def place_bid(
        self, order: NewOrder, *, best_ask: float | None, now: float | None = None
    ) -> int | None: ...

    async def place_sell(
        self, order: NewOrder, *, best_bid: float | None, now: float | None = None
    ) -> int | None: ...

    async def requote(
        self,
        orders: Sequence[NewOrder],
        *,
        best_asks: Mapping[str, float | None] | None = None,
        best_bids: Mapping[str, float | None] | None = None,
        now: float | None = None,
        cancel_ids: Iterable[int] = (),
        cancel_reason: str = "requote",
    ) -> PlaceResult: ...

    async def reconcile(
        self,
        wanted: Sequence[NewOrder],
        resting: Sequence[Mapping[str, Any]],
        *,
        best_asks: Mapping[str, float | None] | None = None,
        best_bids: Mapping[str, float | None] | None = None,
        now: float | None = None,
        cancel_reason: str = "requote",
    ) -> Reconciled: ...

    async def cancel(
        self, order_ids: Iterable[int], *, reason: str, now: float | None = None
    ) -> int: ...

    async def sync_fills(self, *, now: float | None = None) -> FillReport: ...

    async def settle(self, *, now: float | None = None) -> SettleReport: ...


# ---------------------------------------------------------------------------
# Bringing resting orders in line with the plan (pure)
# ---------------------------------------------------------------------------


def reconcile_orders(
    wanted: Sequence[NewOrder],
    resting: Sequence[Mapping[str, Any]],
    *,
    min_shares: float = MIN_ORDER_SHARES,
    share_step: float = SHARE_STEP,
) -> OrderChanges:
    """Bring resting child orders in line with the plan, keeping every place in the queue the
    plan allows (pure).

    ``wanted``: the child orders the maths wants resting now, with their unfilled sizes.
    ``resting``: ``ledger.open_orders()`` rows. Orders match on window, token, kind and price
    (the same tick).

    - A price the plan no longer wants: every resting order there is cancelled.
    - A price it wants: the resting orders there are kept oldest first (they are furthest up
      the queue) while their unfilled shares fit the plan's size; the rest are cancelled. If
      the kept orders fall short, one new child order adds the difference, rounded down to
      ``share_step``, unless that is under the venue's minimum order (``min_shares``).

    The venue has no way to shrink an order in place, so a cut that lands inside one order
    cancels that order and adds back the part still wanted as a new child order. No size or
    price tolerance: an order is kept only when the plan wants at least its size at its price.
    """
    targets: dict[tuple, list[NewOrder]] = {}
    for order in wanted:
        targets.setdefault(_match_key(order.window_slug, order.token_id, order.kind,
                                      order.price), []).append(order)
    rows_at: dict[tuple, list[Mapping[str, Any]]] = {}
    for row in resting:
        key = _match_key(row["window_slug"], row["token_id"], row["kind"], row["price"])
        rows_at.setdefault(key, []).append(row)

    keep: list[int] = []
    cancel: list[int] = []
    kept_at: dict[tuple, float] = {}
    for key, rows in rows_at.items():
        want = sum(float(o.shares) for o in targets.get(key, ()))
        kept = 0.0
        for row in sorted(rows, key=lambda r: (int(r["placed_ts"]), int(r["id"]))):
            left = max(0.0, float(row["shares"]) - float(row.get("filled_shares") or 0.0))
            if kept + left <= want + _SHARES_EPS:
                keep.append(int(row["id"]))
                kept += left
            else:
                cancel.append(int(row["id"]))
        kept_at[key] = kept

    place: list[NewOrder] = []
    for key, orders in targets.items():
        want = sum(float(o.shares) for o in orders)
        extra = _round_down(want - kept_at.get(key, 0.0), share_step)
        if extra > _SHARES_EPS and extra >= min_shares - _SHARES_EPS:
            place.append(dataclasses.replace(orders[0], shares=extra))
    return OrderChanges(keep=tuple(keep), cancel=tuple(cancel), place=tuple(place))


def _match_key(window_slug: Any, token_id: Any, kind: Any, price: Any) -> tuple:
    return (str(window_slug), str(token_id), str(kind), round(float(price), 6))


def _round_down(shares: float, step: float) -> float:
    if shares <= 0:
        return 0.0
    if step <= 0:
        return shares
    return round(math.floor(shares / step + 1e-9) * step, 6)


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
# Paper book-keeping: runs in every mode
# ---------------------------------------------------------------------------


class PaperBookkeeper:
    """Brings paper fills up to date and settles windows. Mode-independent: paper orders
    already placed keep filling and settling whatever the operator selects, as the strategy
    switch's contract requires ("off means open nothing new, never abandon").

    Every cursor lives in the ledger, so a restart loses nothing. The only memory kept here is
    when each unsettled window's result was last looked up, so that the per-pass cap on
    lookups goes round every due window instead of being used up by the same old ones. Keep
    one bookkeeper for the life of the runner (pass it to :func:`choose_executor`).
    ``clock`` gives the time whenever a call is not handed ``now``.
    """

    def __init__(
        self,
        client: HttpClient,
        *,
        max_tape_lag_s: float = DEFAULT_MAX_TAPE_LAG_S,
        force_settle_after_s: float = DEFAULT_FORCE_SETTLE_AFTER_S,
        max_settle_per_pass: int = DEFAULT_MAX_SETTLE_PER_PASS,
        clock: Clock = time.time,
    ) -> None:
        if max_tape_lag_s < 0 or force_settle_after_s < 0 or max_settle_per_pass < 1:
            raise ValueError("tape lag and force delay must be >= 0, settle cap >= 1")
        self._client = client
        self.clock = clock
        self.max_tape_lag_s = float(max_tape_lag_s)
        self.force_settle_after_s = float(force_settle_after_s)
        self.max_settle_per_pass = int(max_settle_per_pass)
        # window slug -> when its result was last looked up / how many lookups failed in a row
        self._looked_up: dict[str, float] = {}
        self._failures: dict[str, int] = {}

    def _time(self, now: float | None) -> float:
        return float(self.clock()) if now is None else float(now)

    async def recover_after_restart(self, *, now: float | None = None) -> int:
        """Stop every order left resting by a previous run. Their tape up to now is still read
        (by ``sync_fills``), so fills made before the restart are kept."""
        cancelled = await _ledger.cancel_all_open(
            ts=self.clock if now is None else now, reason=RESTART_REASON)
        if cancelled:
            log.info("fade1h.restart_cancelled", orders=cancelled)
        return cancelled

    async def sync_fills(self, *, now: float | None = None) -> FillReport:
        """Expire orders in ended windows, then read each market's tape and record fills."""
        t = self._time(now)
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

        reads: dict[str, TapeRead] = {}
        for cid, group in markets.items():
            try:
                reads[cid] = await self._read(cid, group, report)
            except Exception as exc:  # noqa: BLE001 - one market must not stop the others
                self._report_failure(group, exc, report)

        clock = int(math.floor(t))
        fresh = await self._freshness(reads, markets, clock)
        for cid, tape in reads.items():
            group = markets[cid]
            try:
                await self._apply(cid, group, tape, clock, fresh, report)
            except Exception as exc:  # noqa: BLE001 - one market must not stop the others
                self._report_failure(group, exc, report)
        return report

    @staticmethod
    def _report_failure(group: list[dict], exc: BaseException, report: FillReport) -> None:
        slug = group[0]["window_slug"]
        reason = (
            str(exc) if isinstance(exc, TapeUnavailable)
            else f"the fill check failed ({type(exc).__name__}: {exc})"
        )
        report.errors.append(f"{slug}: {reason}. Its fills are checked again next pass.")
        log.warning("fade1h.fill_check_failed", window=slug, error=str(exc))

    async def _read(self, cid: str, rows: list[dict], report: FillReport) -> TapeRead:
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
        return tape

    async def _freshness(self, reads: Mapping[str, TapeRead],
                         markets: Mapping[str, list[dict]], clock: int) -> int | None:
        """The newest record seen on any market's tape this pass: the indexer has got at least
        that far. If some market needs the clock to vouch for a stretch past its own newest
        record and nothing read is that new, the newest window's market is read for it."""
        fresh = max((r.newest_ts for r in reads.values() if r.newest_ts is not None),
                    default=None)
        vouch = clock - int(self.max_tape_lag_s)
        need: int | None = None
        for cid, tape in reads.items():
            rows = markets[cid]
            target = min(vouch, max(int(r["flow_until"]) for r in rows))
            covered = min(int(r["flow_from"]) for r in rows)
            if tape.newest_ts is not None:
                covered = max(covered, tape.newest_ts)
            if target > covered:
                need = target if need is None else max(need, target)
        if need is None or (fresh is not None and fresh >= need):
            return fresh
        try:
            market = await _ledger.latest_market(clock, exclude=reads)
            probed = (
                await tape_newest_ts(self._client, str(market["condition_id"]))
                if market is not None else None
            )
        except Exception as exc:  # noqa: BLE001 - without it the stretch just waits
            log.warning("fade1h.tape_freshness_unread", error=str(exc))
            return fresh
        if probed is None:
            return fresh
        return probed if fresh is None else max(fresh, probed)

    async def _apply(self, cid: str, rows: list[dict], tape: TapeRead, clock: int,
                     fresh: int | None, report: FillReport) -> None:
        # Complete up to the newest record in this reply; beyond it only once the stretch is
        # max_tape_lag_s old and some tape read this pass already holds a newer record.
        reached = [tape.newest_ts] if tape.newest_ts is not None else []
        if fresh is not None:
            reached.append(min(clock - int(self.max_tape_lag_s), fresh))
        if not reached:
            return
        horizon = min(max(reached), clock)

        queued: list[QueuedOrder] = []
        by_id: dict[int, dict] = {}
        for r in rows:
            order_side = str(r.get("order_side") or "BUY")
            side, price, levels = book_terms(order_side, str(r["side"]), float(r["price"]),
                                              _ledger.load_levels(r))
            by_id[int(r["id"])] = r
            queued.append(QueuedOrder(
                order_id=int(r["id"]), side=side, price=price, shares=float(r["shares"]),
                flow_from=int(r["flow_from"]), flow_to=min(horizon, int(r["flow_until"])),
                levels=levels, crossed=float(r["crossed"] or 0.0),
                filled=float(r["filled_shares"] or 0.0), placed_ts=int(r["placed_ts"]),
            ))
        moving = [q for q in queued if q.flow_to > q.flow_from]
        if not moving:
            return
        flows = allocate_fills(tape.prints, moving)
        updates = []
        for q in moving:
            flow = flows[q.order_id]
            order_side = str(by_id[q.order_id].get("order_side") or "BUY")
            updates.append(FlowUpdate(
                order_id=q.order_id, cursor_ts=q.flow_to, crossed=flow.crossed,
                add_shares=flow.added, fill_ts=flow.fill_ts,
                levels_ahead=own_terms(order_side, flow.levels),
            ))
        result = await _ledger.record_flow(updates)
        report.orders_updated += result.updated
        for q in moving:
            flow = flows[q.order_id]
            if flow.added <= _SHARES_EPS or flow.fill_ts is None:
                continue
            row = by_id[q.order_id]
            event = FillEvent(
                order_id=q.order_id, window_slug=str(row["window_slug"]), side=str(row["side"]),
                kind=str(row["kind"]), price=float(row["price"]), shares=flow.added,
                ts=flow.fill_ts, order_side=str(row.get("order_side") or "BUY"),
            )
            report.fills.append(event)
            log.info(
                "fade1h.filled", window=event.window_slug, side=event.side, kind=event.kind,
                order_side=event.order_side, price=event.price, shares=round(event.shares, 2),
                at=event.ts, depth_ahead=round(float(row.get("depth_ahead") or 0.0), 1),
                crossed=round(flow.crossed, 1),
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
        t = self._time(now)
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
                f"{slug}: settled with {settlement.forced_pending} order(s) whose trade tape "
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


class PaperExecutor:
    """Paper child orders: written to the ledger, filled only by the real trade tape."""

    mode = "paper"

    def __init__(
        self,
        client: HttpClient,
        *,
        bookkeeper: PaperBookkeeper | None = None,
        kill_switch_path: Path | str | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.bookkeeper = bookkeeper or PaperBookkeeper(client, clock=clock or time.time)
        self._clock: Clock = clock or self.bookkeeper.clock
        self._kill_switch_path = kill_switch_path

    def _kill_active(self) -> bool:
        try:
            return _controls.kill_switch_path(self._kill_switch_path).exists()
        except OSError:
            return True  # cannot tell: place nothing

    def _write_time(self, now: float | None) -> _ledger.WriteTime:
        """The clock itself, read by the ledger inside its write; ``now`` only when given."""
        return self._clock if now is None else float(now)

    async def place_bid(
        self, order: NewOrder, *, best_ask: float | None, now: float | None = None
    ) -> int | None:
        """Rest one buy. ``best_ask`` is the lowest ask on the order's own token (None when
        nobody is offering it). Returns the order id, or None if it was already placed.
        Raises ``PlacementRefused`` if it would cross or is held back."""
        if not isinstance(order, NewOrder) or order.order_side != "BUY":
            raise PlacementRefused("bad_order", "place_bid only places buy orders.")
        return self._single(await self.requote([order], best_asks={order.token_id: best_ask},
                                               now=now))

    async def place_sell(
        self, order: NewOrder, *, best_bid: float | None, now: float | None = None
    ) -> int | None:
        """Rest one sale of shares held. ``best_bid`` is the highest bid on the order's own
        token (None when nobody is bidding). A sale bigger than the free shares is cut to them
        (the row says how many). Returns the order id, or None if it was already placed.
        Raises ``PlacementRefused`` if it would cross or no shares are free to sell."""
        if not isinstance(order, NewOrder) or order.order_side != "SELL":
            raise PlacementRefused("bad_order", "place_sell only places sell orders.")
        return self._single(await self.requote([order], best_bids={order.token_id: best_bid},
                                               now=now))

    @staticmethod
    def _single(result: PlaceResult) -> int | None:
        (order_id,) = result.ids
        if order_id is None and 0 in result.held_back:
            reason, message = result.held_back[0]
            raise PlacementRefused(reason, message)
        return order_id

    async def requote(
        self,
        orders: Sequence[NewOrder],
        *,
        best_asks: Mapping[str, float | None] | None = None,
        best_bids: Mapping[str, float | None] | None = None,
        now: float | None = None,
        cancel_ids: Iterable[int] = (),
        cancel_reason: str = "requote",
    ) -> PlaceResult:
        """Cancel ``cancel_ids`` and rest ``orders`` in one transaction.

        ``best_asks`` holds the lowest ask for every token bought, ``best_bids`` the highest
        bid for every token sold (None: that side of the book is empty). Every buy must rest
        strictly below the ask and every sell strictly above the bid.

        Refuses the whole batch with ``PlacementRefused``, writing nothing (the cancels
        included), if any order would cross, if an order is not a ``NewOrder``, if the kill
        switch file is present, if an order's window has no market id (its fills could never
        be read from the tape), or if the ledger refuses it (window unknown or over, or the
        token is not that side's token). Orders the one-side rule holds back (a sale of shares
        not free, a buy while the other outcome is held) are listed in the result's
        ``held_back``; the cancels and the other orders still go through.
        """
        batch = list(orders)
        asks = dict(best_asks or {})
        bids = dict(best_bids or {})
        if batch:
            if self._kill_active():
                raise PlacementRefused(
                    KILL_STATE, "The kill switch is on, so no new orders are placed."
                )
            for order in batch:
                if not isinstance(order, NewOrder):
                    raise PlacementRefused("bad_order", "Only NewOrder orders can be placed.")
                _controls.check_passive(order, asks, bids)
            for slug in dict.fromkeys(order.window_slug for order in batch):
                window = await _ledger.get_window(slug)
                # A window's market id is only ever added, never removed, so this check
                # cannot go stale before the placement below.
                if window is not None and not window.get("condition_id"):
                    raise PlacementRefused(
                        "no_market_id", f"{slug}: no market id recorded, so an order there "
                        "could never be filled from the trade tape."
                    )
        try:
            return await _ledger.place_orders(
                batch, ts=self._write_time(now), cancel_ids=cancel_ids,
                cancel_reason=cancel_reason, min_shares=MIN_ORDER_SHARES, share_step=SHARE_STEP,
            )
        except ValueError as exc:  # the ledger wrote nothing
            raise PlacementRefused("ledger_refused", str(exc)) from exc

    async def reconcile(
        self,
        wanted: Sequence[NewOrder],
        resting: Sequence[Mapping[str, Any]],
        *,
        best_asks: Mapping[str, float | None] | None = None,
        best_bids: Mapping[str, float | None] | None = None,
        now: float | None = None,
        cancel_reason: str = "requote",
    ) -> Reconciled:
        """Bring ``resting`` (``ledger.open_orders()`` rows) in line with ``wanted`` by
        :func:`reconcile_orders`, then cancel and place in one transaction (:meth:`requote`).
        Nothing is written when nothing changes."""
        changes = reconcile_orders(wanted, resting)
        if not changes.cancel and not changes.place:
            return Reconciled(changes, PlaceResult(ids=[]))
        result = await self.requote(
            changes.place, best_asks=best_asks, best_bids=best_bids, now=now,
            cancel_ids=changes.cancel, cancel_reason=cancel_reason,
        )
        return Reconciled(changes, result)

    async def cancel(
        self, order_ids: Iterable[int], *, reason: str, now: float | None = None
    ) -> int:
        return await _ledger.cancel_orders(order_ids, ts=self._write_time(now), reason=reason)

    async def sync_fills(self, *, now: float | None = None) -> FillReport:
        return await self.bookkeeper.sync_fills(now=now)

    async def settle(self, *, now: float | None = None) -> SettleReport:
        return await self.bookkeeper.settle(now=now)


# ---------------------------------------------------------------------------
# Choosing the executor from the requested mode
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutorChoice:
    """This pass's executor. ``executor`` is None when no new orders may be placed; the
    bookkeeper keeps filling and settling the paper orders already made in every state."""

    requested_mode: str
    state: str
    message: str
    executor: Executor | None
    bookkeeper: PaperBookkeeper

    @property
    def can_place(self) -> bool:
        return self.executor is not None


async def choose_executor(
    client: HttpClient,
    *,
    bookkeeper: PaperBookkeeper | None = None,
    kill_switch_path: Path | str | None = None,
    clock: Clock | None = None,
) -> ExecutorChoice:
    """Pick this pass's executor from the requested mode and the kill switch.

    Pass the runner's one long-lived ``bookkeeper`` on every call: it remembers which
    windows' results were looked up lately, and a fresh one each pass would forget that.
    ``clock`` stamps the executor's writes (default: the bookkeeper's clock).

    - kill switch file present: none (no new orders).
    - paper: ``PaperExecutor``.
    - live: none. No live order path exists for this strategy and live trading is not
      authorised, so the card says so and nothing new is placed.
    - anything else, or the mode cannot be read: none.
    """
    keeper = bookkeeper or PaperBookkeeper(client, clock=clock or time.time)
    try:
        mode = await requested_mode()
    except Exception as exc:  # noqa: BLE001 - fail closed, and say why
        log.warning("fade1h.mode_read_failed", error=str(exc))
        return ExecutorChoice(
            "unknown", UNKNOWN_MODE_STATE,
            f"Could not read the PAPER/LIVE selection ({type(exc).__name__}), so no new orders "
            "are placed this pass. Orders already filled keep settling.", None, keeper,
        )
    path = _controls.kill_switch_path(kill_switch_path)
    try:
        killed = path.exists()
    except OSError:
        killed = True
    if killed:
        return ExecutorChoice(
            mode, KILL_STATE,
            f"The kill switch file is present ({path}). No new orders, and resting orders are "
            "cancelled. Orders already filled keep settling.", None, keeper,
        )
    if mode == "paper":
        return ExecutorChoice(
            mode, PAPER_STATE,
            "Paper trading: orders rest only in this app's ledger and fill only when the real "
            "trade tape reaches them.",
            PaperExecutor(client, bookkeeper=keeper, kill_switch_path=kill_switch_path,
                          clock=clock),
            keeper,
        )
    if mode == "live":
        return ExecutorChoice(
            mode, LIVE_STATE,
            "LIVE is selected, but this strategy has no live order path: it is not built, and "
            "live trading is not authorised for any market. No new orders are placed; paper "
            "orders already filled keep settling.", None, keeper,
        )
    return ExecutorChoice(
        mode, UNKNOWN_MODE_STATE,
        f"The selected mode {mode!r} is not PAPER or LIVE, so no new orders are placed. "
        "Orders already filled keep settling.", None, keeper,
    )


async def stand_down(choice: ExecutorChoice, *, now: float | None = None) -> int:
    """When this pass may not place orders, cancel every paper order still resting (a resting
    order that filled later would change the position). Returns how many were cancelled."""
    if choice.can_place:
        return 0
    cancelled = await _ledger.cancel_all_open(
        ts=choice.bookkeeper.clock if now is None else now, reason=choice.state)
    if cancelled:
        log.info("fade1h.stood_down", state=choice.state, orders=cancelled)
    return cancelled
