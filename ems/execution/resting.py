"""Resting orders for any strategy: one request shape, a paper venue, and the same calls live.

A strategy never talks to the exchange. It hands a venue "buy this token, at this price, this
many shares, until this time" (:class:`PlaceRequest`), and later asks what became of it
(:meth:`RestingVenue.fills`) or stops it (:meth:`RestingVenue.cancel`). The paper venue here and
the live venue (``ems/execution/clob.py``) take the same calls, so the strategy's code never
branches on the mode and paper and live cannot drift apart.

Every order is a passive limit BUY that only rests: one that would meet the best ask is refused
before anything is written, and while the kill switch file exists nothing is placed. An order
is good till a date (GTD): its expiry is the end of its window, and the venue stops it
``GTD_STOP_S`` (60 s) before that, as Polymarket does.

How a paper order fills
-----------------------
From the venue's public taker trade tape, through the depth that was displayed at its price or
better when it was placed (``queue_ahead``), by the fill model every strategy shares
(``ems/execution/queue.py:allocate_fills``). For a lone buy that is
``filled = min(size, max(0, crossed - queue_ahead))``, where ``crossed`` is the taker volume
that reached its price after it was placed and before it stopped resting. Orders of different
strategies on the same outcome are run through the tape together, so one trade never fills two
paper orders beyond its size. Each fill is at the order's own price, with no fee.

An order rests from the second after it is written and stops at the second its cancel is
written, or at its stop. Its stretch of tape is read from where the last read ended, never past
what the tape can vouch for: the newest record in a reply marks how far that reply is
complete, and a quiet stretch after it counts as read only once it is ``max_tape_lag_s`` old
and some market's tape already holds a newer record. An order is ``final`` once its whole
resting stretch has been read (nothing more can fill it), or ``force_final_after_s`` after it
stopped, with ``forced`` set, if the tape never caught up.

Storage: ``paper_resting_orders`` (created by ``db.init_db``), read and written only here.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import structlog

from ems import db as _db  # type: ignore[import-untyped]
from ems.execution import controls as _controls
from ems.execution.controls import PlacementRefused
from ems.execution.queue import SIDES, QueuedOrder, allocate_fills
from ems.execution.tape import HttpClient, TapeRead, read_taker_tape, tape_newest_ts

log = structlog.get_logger(__name__)

PAPER = "paper"
LIVE = "live"
MODES = (PAPER, LIVE)

# Polymarket stops a GTD order this long before the expiry it was given.
GTD_STOP_S = 60
SHARE_STEP = 0.01
DEFAULT_MAX_TAPE_LAG_S = 900
DEFAULT_FORCE_FINAL_AFTER_S = 3600
PAPER_ID_PREFIX = "paper-"

# Order states, as a venue reports them.
RESTING = "resting"
FILLED = "filled"
CANCELLED = "cancelled"
EXPIRED = "expired"
REJECTED = "rejected"
STATES = (RESTING, FILLED, CANCELLED, EXPIRED, REJECTED)
CLOSED_STATES = (FILLED, CANCELLED, EXPIRED, REJECTED)

_EPS = 1e-9

Clock = Callable[[], float]


@dataclass(frozen=True)
class PlaceRequest:
    """Buy ``size`` shares of ``token_id`` at ``price``, resting until ``expires_ts``.

    ``outcome`` is the side the token pays on ("Up" or "Down") and ``up_token`` /
    ``down_token`` the window's two tokens (the paper venue matches tape records by them).
    ``queue_ahead`` is the size displayed at ``price`` or better when the order is placed: the
    depth it joins behind. ``best_ask`` is the lowest ask on the token (None when nobody is
    offering it): the order must rest strictly below it. ``tick_size`` is the market's price
    step. ``strategy`` tags the order on the venue's record.
    """

    strategy: str
    condition_id: str
    token_id: str
    outcome: str
    up_token: str
    down_token: str
    price: float
    size: float
    expires_ts: int
    tick_size: float
    queue_ahead: float
    best_ask: float | None

    @property
    def order_side(self) -> str:
        return "BUY"

    @property
    def notional_usd(self) -> float:
        return round(self.price * self.size, 6)


@dataclass(frozen=True)
class Placed:
    """An order the venue accepted: its id on that venue and the second it started resting."""

    order_id: str
    placed_ts: int


@dataclass(frozen=True)
class OrderView:
    """Where an order stands on its venue.

    ``state`` is one of ``STATES``. ``closed_ts`` is when it stopped resting (None while it
    rests). ``final`` means nothing more can fill it: closed, and (paper) its tape read through
    the close or (live) the venue's own terminal state. ``forced`` marks a paper order made
    final with tape still unread, so its fills may be under-counted.
    """

    order_id: str
    state: str
    size: float
    filled_size: float
    closed_ts: int | None
    final: bool
    forced: bool = False

    @property
    def closed(self) -> bool:
        return self.state in CLOSED_STATES

    @property
    def unfilled_size(self) -> float:
        return max(0.0, self.size - self.filled_size)


@runtime_checkable
class RestingVenue(Protocol):
    """Where resting orders go: the paper venue or the live one. Same calls on both."""

    mode: str

    async def place(self, request: PlaceRequest, *, now: float | None = None) -> Placed: ...

    async def cancel(
        self, order_ids: Iterable[str], *, reason: str, now: float | None = None
    ) -> int: ...

    async def fills(
        self, order_ids: Iterable[str], *, now: float | None = None
    ) -> dict[str, OrderView]: ...


def validate_request(request: PlaceRequest, now: float) -> None:
    """The checks every venue makes before an order goes anywhere. Raises PlacementRefused."""
    if request.outcome not in SIDES:
        raise PlacementRefused("bad_order", f"Outcome {request.outcome!r} is not Up or Down.")
    if request.token_id not in (request.up_token, request.down_token):
        raise PlacementRefused("bad_order", "The token is not one of the window's two tokens.")
    tick = request.tick_size
    if not (math.isfinite(tick) and 0.0 < tick < 1.0):
        raise PlacementRefused("bad_order", f"Tick size {tick!r} is not a price step.")
    price = request.price
    if not (math.isfinite(price) and tick - _EPS <= price <= 1.0 - tick + _EPS):
        raise PlacementRefused("bad_order", f"Price {price!r} is not a tradable price.")
    if abs(price / tick - round(price / tick)) > 1e-6:
        raise PlacementRefused("bad_order", f"Price {price} is not on the {tick} tick.")
    size = request.size
    if not (math.isfinite(size) and size > 0.0):
        raise PlacementRefused("bad_order", f"Size {size!r} is not a number of shares.")
    if abs(size / SHARE_STEP - round(size / SHARE_STEP)) > 1e-6:
        raise PlacementRefused("bad_order", f"Size {size} is not in hundredths of a share.")
    if not (math.isfinite(request.queue_ahead) and request.queue_ahead >= 0.0):
        raise PlacementRefused("bad_order", "The depth ahead must be a number of shares.")
    if int(request.expires_ts) - GTD_STOP_S <= math.floor(now) + 1:
        raise PlacementRefused(
            "too_late", "The venue stops an order 60 s before its expiry, so this one would "
            "never rest."
        )
    _controls.check_passive(request, {request.token_id: request.best_ask}, {})


# ---------------------------------------------------------------------------
# The paper venue
# ---------------------------------------------------------------------------


def paper_order_id(row_id: int) -> str:
    return f"{PAPER_ID_PREFIX}{int(row_id)}"


def _row_id(order_id: str) -> int | None:
    if not isinstance(order_id, str) or not order_id.startswith(PAPER_ID_PREFIX):
        return None
    try:
        return int(order_id[len(PAPER_ID_PREFIX):])
    except ValueError:
        return None


def _levels(row: Mapping[str, Any]) -> tuple[tuple[float, float], ...]:
    raw = row.get("levels_ahead_json")
    if raw:
        return tuple((float(px), float(size)) for px, size in json.loads(raw))
    return ((float(row["price"]), float(row["queue_ahead"])),)


def _end_ts(row: Mapping[str, Any]) -> int:
    """The second the order stopped resting (exclusive): its cancel, else its stop."""
    stop = int(row["stop_ts"])
    cancelled = row.get("cancelled_ts")
    return min(stop, int(cancelled)) if cancelled is not None else stop


def view_of(row: Mapping[str, Any], now: float) -> OrderView:
    """A paper order's row as the venue reports it."""
    size = float(row["size"])
    filled = float(row["filled_size"] or 0.0)
    end = _end_ts(row)
    if filled >= size - _EPS:
        state, closed_ts = FILLED, int(row["filled_ts"] or end)
    elif row.get("cancelled_ts") is not None and int(row["cancelled_ts"]) < int(row["stop_ts"]):
        state, closed_ts = CANCELLED, int(row["cancelled_ts"])
    elif now >= int(row["stop_ts"]):
        state, closed_ts = EXPIRED, int(row["stop_ts"])
    else:
        state, closed_ts = RESTING, None
    final = row.get("final_ts") is not None or state == FILLED
    return OrderView(
        order_id=paper_order_id(int(row["id"])), state=state, size=size, filled_size=filled,
        closed_ts=closed_ts, final=final, forced=bool(row.get("forced")),
    )


class PaperRestingVenue:
    """Paper orders: written to ``paper_resting_orders``, filled only by the real trade tape."""

    mode = PAPER

    def __init__(
        self,
        client: HttpClient,
        *,
        clock: Clock = time.time,
        kill_switch_path: Path | str | None = None,
        max_tape_lag_s: float = DEFAULT_MAX_TAPE_LAG_S,
        force_final_after_s: float = DEFAULT_FORCE_FINAL_AFTER_S,
    ) -> None:
        if max_tape_lag_s < 0 or force_final_after_s < 0:
            raise ValueError("tape lag and force delay must be >= 0")
        self._client = client
        self.clock = clock
        self._kill_switch_path = kill_switch_path
        self.max_tape_lag_s = float(max_tape_lag_s)
        self.force_final_after_s = float(force_final_after_s)
        self.last_errors: list[str] = []

    def _time(self, now: float | None) -> float:
        return float(self.clock()) if now is None else float(now)

    async def place(self, request: PlaceRequest, *, now: float | None = None) -> Placed:
        """Rest one paper buy. Raises ``PlacementRefused`` (nothing written) when the kill
        switch is on or the request fails :func:`validate_request`."""
        t = self._time(now)
        if _controls.kill_switch_active(self._kill_switch_path):
            raise PlacementRefused("kill_switch", "The kill switch is on, so no new orders are "
                                   "placed.")
        validate_request(request, t)
        placed_ts = int(math.floor(t)) + 1
        async with _db.connect() as conn:
            cur = await conn.execute(
                """
                INSERT INTO paper_resting_orders (
                  strategy, condition_id, token_id, outcome, up_token, down_token, price, size,
                  queue_ahead, levels_ahead_json, placed_ts, stop_ts, expires_ts, flow_cursor_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.strategy, request.condition_id, request.token_id, request.outcome,
                    request.up_token, request.down_token, float(request.price),
                    float(request.size), float(request.queue_ahead),
                    json.dumps([[float(request.price), float(request.queue_ahead)]]),
                    placed_ts, int(request.expires_ts) - GTD_STOP_S, int(request.expires_ts),
                    placed_ts,
                ),
            )
            await conn.commit()
            row_id = int(cur.lastrowid)
        log.info("paper_venue.placed", strategy=request.strategy, order=row_id,
                 outcome=request.outcome, price=request.price, size=request.size,
                 queue_ahead=request.queue_ahead)
        return Placed(order_id=paper_order_id(row_id), placed_ts=placed_ts)

    async def cancel(
        self, order_ids: Iterable[str], *, reason: str, now: float | None = None
    ) -> int:
        """Stop resting orders at this second. Fills already made, and fills the tape shows
        later for the time they rested, are kept. Returns how many were stopped."""
        ids = [i for i in (_row_id(o) for o in order_ids) if i is not None]
        if not ids:
            return 0
        stop_at = int(math.floor(self._time(now)))
        marks = ",".join("?" * len(ids))
        async with _db.connect() as conn:
            cur = await conn.execute(
                f"""
                UPDATE paper_resting_orders
                SET cancelled_ts = ?, cancel_reason = ?
                WHERE id IN ({marks}) AND cancelled_ts IS NULL AND final_ts IS NULL
                  AND filled_size < size AND stop_ts > ?
                """,
                (stop_at, str(reason), *ids, stop_at),
            )
            await conn.commit()
            count = int(cur.rowcount or 0)
        if count:
            log.info("paper_venue.cancelled", orders=count, reason=reason)
        return count

    async def fills(
        self, order_ids: Iterable[str], *, now: float | None = None
    ) -> dict[str, OrderView]:
        """Bring the given orders' fills up to date from the tape and report each one.

        Unknown ids are left out. A tape that cannot be read moves nothing: those orders are
        reported as they stood, and ``last_errors`` says why (plain English, for a card).
        """
        t = self._time(now)
        clock = int(math.floor(t))
        self.last_errors = []
        ids = [i for i in (_row_id(o) for o in order_ids) if i is not None]
        rows = await self._load(ids)
        open_rows = [r for r in rows if r.get("final_ts") is None
                     and float(r["filled_size"] or 0.0) < float(r["size"]) - _EPS]
        if open_rows:
            await self._advance(open_rows, t, clock)
            rows = await self._load(ids)
        return {paper_order_id(int(r["id"])): view_of(r, t) for r in rows}

    async def _load(self, ids: list[int]) -> list[dict[str, Any]]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        async with _db.connect() as conn:
            async with conn.execute(
                f"SELECT * FROM paper_resting_orders WHERE id IN ({marks}) ORDER BY id", ids
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def _advance(self, rows: list[dict[str, Any]], t: float, clock: int) -> None:
        # Read through the end already (a cancel written in the cursor's own second): final.
        through = [int(r["id"]) for r in rows if int(r["flow_cursor_ts"]) >= _end_ts(r)]
        if through:
            marks = ",".join("?" * len(through))
            async with _db.connect() as conn:
                await conn.execute(
                    f"UPDATE paper_resting_orders SET final_ts = ? WHERE id IN ({marks}) "
                    "AND final_ts IS NULL",
                    (clock, *through),
                )
                await conn.commit()
        markets: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if int(row["id"]) not in through:
                markets.setdefault(str(row["condition_id"]), []).append(row)
        # Every order on a market is run through its tape together, whichever strategy placed
        # it, so one trade never fills two paper orders beyond its size.
        for cid in markets:
            markets[cid] = await self._market_rows(cid)
        reads: dict[str, TapeRead] = {}
        for cid, group in markets.items():
            try:
                reads[cid] = await read_taker_tape(
                    self._client, cid, since=min(int(r["flow_cursor_ts"]) for r in group),
                    up_token=group[0]["up_token"], down_token=group[0]["down_token"],
                )
            except Exception as exc:  # noqa: BLE001 - one market must not stop the others
                self.last_errors.append(f"{cid[:10]}: {exc}. Fills are checked again next pass.")
                log.warning("paper_venue.tape_unread", market=cid, error=str(exc))
        fresh = await self._freshness(reads, markets, clock)
        for cid, tape in reads.items():
            await self._apply(markets[cid], tape, clock, fresh)
        await self._force_final(rows, clock)

    async def _market_rows(self, condition_id: str) -> list[dict[str, Any]]:
        async with _db.connect() as conn:
            async with conn.execute(
                """
                SELECT * FROM paper_resting_orders
                WHERE condition_id = ? AND final_ts IS NULL AND filled_size < size
                ORDER BY id
                """,
                (condition_id,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def _freshness(self, reads: Mapping[str, TapeRead],
                         markets: Mapping[str, list[dict[str, Any]]], clock: int) -> int | None:
        """The newest record seen on any tape this pass: the tape has got at least that far.
        If a market needs a quiet stretch vouched for past that, the newest paper market's tape
        is read for how far it has got."""
        fresh = max((r.newest_ts for r in reads.values() if r.newest_ts is not None),
                    default=None)
        vouch = clock - int(self.max_tape_lag_s)
        need: int | None = None
        for cid, tape in reads.items():
            group = markets[cid]
            target = min(vouch, max(_end_ts(r) for r in group))
            covered = min(int(r["flow_cursor_ts"]) for r in group)
            if tape.newest_ts is not None:
                covered = max(covered, tape.newest_ts)
            if target > covered:
                need = target if need is None else max(need, target)
        if need is None or (fresh is not None and fresh >= need):
            return fresh
        try:
            async with _db.connect() as conn:
                async with conn.execute(
                    """
                    SELECT condition_id FROM paper_resting_orders
                    WHERE placed_ts <= ? ORDER BY placed_ts DESC, id DESC LIMIT 1
                    """,
                    (clock,),
                ) as cur:
                    row = await cur.fetchone()
            probed = await tape_newest_ts(self._client, str(row["condition_id"])) if row else None
        except Exception as exc:  # noqa: BLE001 - without it the stretch just waits
            log.warning("paper_venue.tape_freshness_unread", error=str(exc))
            return fresh
        if probed is None:
            return fresh
        return probed if fresh is None else max(fresh, probed)

    async def _apply(self, group: list[dict[str, Any]], tape: TapeRead, clock: int,
                     fresh: int | None) -> None:
        reached = [tape.newest_ts] if tape.newest_ts is not None else []
        if fresh is not None:
            reached.append(min(clock - int(self.max_tape_lag_s), fresh))
        if not reached:
            return
        horizon = min(max(reached), clock)
        queued = [
            QueuedOrder(
                order_id=int(r["id"]), side=str(r["outcome"]), price=float(r["price"]),
                shares=float(r["size"]), flow_from=int(r["flow_cursor_ts"]),
                flow_to=min(horizon, _end_ts(r)), levels=_levels(r),
                crossed=float(r["crossed"] or 0.0), filled=float(r["filled_size"] or 0.0),
                placed_ts=int(r["placed_ts"]),
            )
            for r in group
        ]
        ends = {int(r["id"]): _end_ts(r) for r in group}
        moving = [q for q in queued if q.flow_to > q.flow_from]
        if not moving:
            return
        flows = allocate_fills(tape.prints, moving)
        async with _db.connect() as conn:
            for q in moving:
                flow = flows[q.order_id]
                done = q.flow_to >= ends[q.order_id] or flow.filled >= q.shares - _EPS
                await conn.execute(
                    """
                    UPDATE paper_resting_orders
                    SET flow_cursor_ts = ?, crossed = ?, filled_size = ?,
                        filled_ts = COALESCE(filled_ts, ?), levels_ahead_json = ?,
                        final_ts = CASE WHEN ? THEN ? ELSE final_ts END
                    WHERE id = ?
                    """,
                    (
                        q.flow_to, flow.crossed, flow.filled,
                        flow.fill_ts if flow.added > _EPS else None,
                        json.dumps([[px, size] for px, size in flow.levels]),
                        1 if done else 0, clock, q.order_id,
                    ),
                )
                if flow.added > _EPS:
                    log.info("paper_venue.filled", order=q.order_id, outcome=q.side,
                             price=q.price, shares=round(flow.added, 2), at=flow.fill_ts)
            await conn.commit()

    async def _force_final(self, rows: list[dict[str, Any]], clock: int) -> None:
        """Orders whose tape never caught up are made final ``force_final_after_s`` after they
        stopped, flagged ``forced``."""
        late = [int(r["id"]) for r in rows
                if clock >= _end_ts(r) + self.force_final_after_s]
        if not late:
            return
        marks = ",".join("?" * len(late))
        async with _db.connect() as conn:
            cur = await conn.execute(
                f"""
                UPDATE paper_resting_orders SET final_ts = ?, forced = 1
                WHERE id IN ({marks}) AND final_ts IS NULL
                """,
                (clock, *late),
            )
            await conn.commit()
        if cur.rowcount:
            self.last_errors.append(
                f"{cur.rowcount} paper order(s) closed with trade tape still unread, so their "
                "fills may be under-counted."
            )
            log.warning("paper_venue.forced_final", orders=cur.rowcount)
