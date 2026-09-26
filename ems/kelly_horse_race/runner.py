"""The Kelly horse-race loop: bookkeeping, then one randomised passive buy per BTC 15m window.

``run_forever(stop_event)`` runs for the dashboard's lifetime (started in the app lifespan,
after the market-data hub). Every pass, in the order that keeps the record honest:

1. Setup, once per process: give the risk gate today's settled P&L again (counted once), in
   case the last run stopped between settling a window and telling the gate.
2. Bookkeeping, whatever the switch or the mode says, each step guarded on its own:
   - bring every open order's fills up to date from its venue (``venue.fills``) and mirror
     what the venue reports; an order the venue can fill no more gives its unfilled notional
     back to its gate leg;
   - from ``CANCEL_LEAD_S`` (60 s) before the window end, cancel what still rests (the venue
     stops a GTD order then anyway); filled shares are kept;
   - settle every ended window whose orders are all final, once the venue's order-book
     service calls it: P&L per filled share is ``1[won] - price``, with no fee.
3. The endpoints for this pass (``ems/execution/endpoints.py``): paper is always on unless the
   kill switch file exists. Whatever rests on an endpoint that is off is cancelled.
4. The strategy switch. Off: cancel every resting order, give up the market data, decide
   nothing new.
5. On: one decision per window (``maths``), from this window's inputs (``inputs``). The two
   draws are made once per window from ``random.SystemRandom`` and kept, so a pass that has
   to wait for an input never rolls the die again. The same order goes to every active
   endpoint, through that endpoint's risk gate leg; a block or a refusal is recorded against
   that mode. An endpoint that comes on after the window's decision starts at the next window.
   Inputs that cannot arrive in time (``NotReady.final``, or still missing at the cutoff
   ``DECISION_CUTOFF_S`` before the end) are recorded as the window's reason for no order.
6. Record this pass's state for the card (``status()`` and the ``STATUS_KEY`` config row).
7. Sleep for the poll interval from Settings.

Never raises (a cancel still propagates, which the app's teardown expects): every step is
guarded, and every failure is logged and shown on the card.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from ems import db as _db  # type: ignore[import-untyped]
from ems import runtime_knobs as _knobs
from ems import strategies as _strategies
from ems.execution import endpoints as _endpoints
from ems.execution.controls import PlacementRefused
from ems.execution.gate import MODES, RiskGate
from ems.execution.resting import PaperRestingVenue, PlaceRequest, RestingVenue
from ems.execution.tape import MarketUnavailable, market_outcome
from ems.kelly_horse_race import inputs as _inputs
from ems.kelly_horse_race import ledger as _ledger
from ems.kelly_horse_race import maths as _maths
from ems.logging_setup import get_logger

log = get_logger("kelly_horse_race.runner")

STRATEGY = "kelly_horse_race"
LABEL = "Kelly horse-race"
OWNER = _inputs.OWNER
STATUS_KEY = "kelly_horse_race.runner_status"
DEFAULT_POLL_S = 5.0
DEFAULT_MAX_NOTIONAL_USD = 5.0
MIN_SLEEP_S = 1.0
HTTP_TIMEOUT_S = 25.0
CANCEL_LEAD_S = 60
DECISION_CUTOFF_S = 120
SETTLE_RETRY_S = 30.0
MAX_SETTLE_PER_PASS = 20
FILL_EVENT = "kelly_fill"
SETTLED_EVENT = "kelly_settled"

# Runner states, shown on the card.
RUNNING = "running"
SWITCHED_OFF = "switched_off"
NO_ENDPOINT = "no_endpoint"  # the kill switch, or every endpoint off: nothing new is decided
PASS_FAILED = "pass_failed"
STOPPED = "stopped"
STOPPED_ON_ERROR = "stopped_on_error"

_STATUS: dict[str, Any] = {"state": "not_started", "last_pass_ts": None, "last_error": None}


def status() -> dict[str, Any]:
    """This process's latest pass, for the card."""
    return copy.deepcopy(_STATUS)


async def _save_status() -> None:
    try:
        await _db.set_config(STATUS_KEY, json.dumps(_STATUS, default=str))
    except Exception as exc:  # noqa: BLE001 - the card still has the in-memory copy
        log.warning("kelly.status_save_failed", error=str(exc))


async def _record_failure(message: str, ts: float, state: str) -> None:
    global _STATUS
    _STATUS = {**_STATUS, "state": state, "last_error": message, "last_error_ts": ts,
               "errors": [message]}
    await _save_status()


async def _knob(name: str, default: float) -> float:
    try:
        value = float(await _knobs.get(name))
    except Exception as exc:  # noqa: BLE001 - the default applies, and the log says so
        log.warning("kelly.knob_read_failed", knob=name, error=str(exc))
        return default
    return value if math.isfinite(value) and value > 0 else default


async def read_poll_interval() -> float:
    return await _knob("kelly_horse_race_poll_interval_seconds", DEFAULT_POLL_S)


async def read_max_notional() -> float:
    return await _knob("kelly_horse_race_max_notional_usd", DEFAULT_MAX_NOTIONAL_USD)


def order_ref(order_id: int) -> str:
    """How the risk gate names one of this strategy's orders."""
    return f"{STRATEGY}:{int(order_id)}"


@dataclass
class PassReport:
    ts: float
    state: str = RUNNING
    errors: list[str] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    settled: list[_ledger.SettledWindow] = field(default_factory=list)
    cancelled: int = 0
    endpoints: dict[str, dict[str, Any]] = field(default_factory=dict)
    window: dict[str, Any] = field(default_factory=dict)
    step_failed: bool = False

    def fail(self, step: str, exc: BaseException | str) -> str:
        text = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
        message = f"{step} failed: {text}"
        self.errors.append(message)
        self.step_failed = True
        log.warning("kelly.step_failed", step=step, error=text)
        return message


@dataclass
class _Draws:
    u1: float
    u2: float


class Runner:
    """One Kelly horse-race loop. ``live`` is the live venue factory once one is built."""

    def __init__(
        self,
        client: httpx.AsyncClient | Any,
        *,
        hub_fn: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.time,
        kill_switch_path: Any = None,
        rng: Callable[[], float] | None = None,
        paper: RestingVenue | None = None,
        live: _endpoints.LiveVenueFactory | None = None,
    ) -> None:
        self._client = client
        self._hub_fn = hub_fn or _current_hub
        self._clock = clock
        self._kill_switch_path = kill_switch_path
        self._rng = rng or random.SystemRandom().random
        self._latest_cid: str | None = None
        self.paper = paper or PaperRestingVenue(client, clock=clock,
                                                kill_switch_path=kill_switch_path,
                                                latest_market=lambda: self._latest_cid)
        self._live_factory = live
        self.venues: dict[str, RestingVenue] = {"paper": self.paper}
        self.gates = {mode: RiskGate(mode, kill_switch_path=kill_switch_path) for mode in MODES}
        self.memory = _inputs.Memory()
        self._draws: dict[str, _Draws] = {}
        self._waiting: dict[str, _inputs.NotReady] = {}
        self._settle_retry_at: dict[int, float] = {}
        self._setup_done = False
        self._hub: Any = None

    def _now(self) -> float:
        return float(self._clock())

    async def pass_once(self) -> PassReport:
        """One guarded pass. Never raises (a cancel propagates)."""
        report = PassReport(ts=self._now())
        if not self._setup_done:
            await self._setup(report)
        await self._bookkeeping(report)
        points = await self._choose(report)
        try:
            on = await _strategies.enabled(STRATEGY)
        except Exception as exc:  # noqa: BLE001 - fail closed
            report.fail("Reading the strategy switch", exc)
            on = False
        if not on:
            await self._cancel_resting(report, modes=MODES, reason=SWITCHED_OFF)
            self.release()
            report.state = SWITCHED_OFF
        elif points is None or not any(p.active for p in points.values()):
            report.state = NO_ENDPOINT
        else:
            try:
                await self._decide(points, report)
            except Exception as exc:  # noqa: BLE001 - the pass goes on to record itself
                report.fail("The decision", exc)
        await self._finish(report)
        return report

    # -- setup and bookkeeping --------------------------------------------------------

    async def _setup(self, report: PassReport) -> None:
        try:
            now = self._now()
            for window in await _ledger.recent(limit=200):
                for order in window["orders"].values():
                    if order.get("pnl_usd") is not None and order.get("settled_ts"):
                        await self.gates[order["mode"]].realize(
                            strategy=STRATEGY, order_ref=order_ref(order["id"]),
                            pnl_usd=float(order["pnl_usd"]), now=float(order["settled_ts"]))
            self._setup_done = True
            log.info("kelly.setup_done", at=now)
        except Exception as exc:  # noqa: BLE001 - tried again next pass
            report.fail("Setup", exc)

    async def _bookkeeping(self, report: PassReport) -> None:
        for step, fn in (("Checking fills", self._sync_fills),
                         ("Giving unfilled notional back to the gate", self._credit),
                         ("Cancelling at the window end", self._cancel_ending),
                         ("Settling", self._settle)):
            try:
                await fn(report)
            except Exception as exc:  # noqa: BLE001 - each step on its own
                report.fail(step, exc)

    async def _sync_fills(self, report: PassReport) -> None:
        for mode in list(self.venues):
            rows = [r for r in await _ledger.open_orders(mode) if r["venue_order_id"]]
            await self._refresh(mode, rows, report)

    async def _refresh(self, mode: str, rows: list[dict[str, Any]], report: PassReport) -> None:
        """Ask ``mode``'s venue how ``rows`` stand and mirror it; report new fills."""
        venue = self.venues.get(mode)
        if venue is None or not rows:
            return
        views = await venue.fills([r["venue_order_id"] for r in rows], now=self._now())
        for error in getattr(venue, "last_errors", []) or []:
            report.errors.append(f"{mode}: {error}")
        for row in rows:
            view = views.get(row["venue_order_id"])
            if view is None:
                report.errors.append(f"{mode}: order {row['venue_order_id']} is not on the "
                                     "venue's record.")
                continue
            added = await _ledger.apply_view(int(row["id"]), view)
            if added > 0:
                fill = {"mode": mode, "order_id": int(row["id"]),
                        "window_slug": row["window_slug"], "outcome": row["outcome"],
                        "price": float(row["price"]), "shares": round(added, 2)}
                report.fills.append(fill)
                await self._notify(FILL_EVENT, (
                    f"Kelly horse-race {mode}: {added:.2f} {row['outcome']} shares filled at "
                    f"{float(row['price']):.2f} ({row['window_slug']})"), fill)

    async def _credit(self, report: PassReport) -> None:
        now = self._now()
        for row in await _ledger.to_credit():
            unfilled = max(0.0, float(row["size"]) - float(row["filled_size"] or 0.0))
            await self.gates[row["mode"]].credit(
                strategy=STRATEGY, order_ref=order_ref(row["id"]),
                unfilled_usd=round(unfilled * float(row["price"]), 6), now=now)
            await _ledger.mark_credited(int(row["id"]))

    async def _cancel_ending(self, report: PassReport) -> None:
        now = self._now()
        rows = [r for r in await _ledger.resting_orders()
                if now >= int(r["window_end_ts"]) - CANCEL_LEAD_S]
        await self._cancel_rows(rows, reason="window_end", report=report)

    async def _cancel_rows(self, rows: list[dict[str, Any]], *, reason: str,
                           report: PassReport) -> None:
        """Cancel these resting orders on their venues, then mirror where they stand."""
        by_mode: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row["venue_order_id"]:
                by_mode.setdefault(row["mode"], []).append(row)
        for mode, group in by_mode.items():
            venue = self.venues.get(mode)
            if venue is None:
                continue
            report.cancelled += await venue.cancel([r["venue_order_id"] for r in group],
                                                   reason=reason, now=self._now())
            await self._refresh(mode, group, report)

    async def _settle(self, report: PassReport) -> None:
        now = self._now()
        for decision in (await _ledger.settlement_due(now))[:MAX_SETTLE_PER_PASS]:
            key = int(decision["id"])
            if self._settle_retry_at.get(key, -math.inf) > now:
                continue
            try:
                outcome = await market_outcome(
                    self._client, str(decision["condition_id"]),
                    up_token=decision["up_token"], down_token=decision["down_token"])
            except MarketUnavailable as exc:
                self._settle_retry_at[key] = now + SETTLE_RETRY_S
                report.errors.append(f"{decision['window_slug']}: {exc}. Tried again shortly.")
                continue
            if outcome is None:
                self._settle_retry_at[key] = now + SETTLE_RETRY_S
                continue
            settled = await _ledger.settle(key, outcome=outcome, ts=now)
            self._settle_retry_at.pop(key, None)
            if settled is None:
                continue
            for order in settled.orders:
                await self.gates[order.mode].realize(
                    strategy=STRATEGY, order_ref=order_ref(order.order_id),
                    pnl_usd=order.pnl_usd, now=now)
            report.settled.append(settled)
            pnl = {m: round(sum(o.pnl_usd for o in settled.orders if o.mode == m), 4)
                   for m in {o.mode for o in settled.orders}}
            await self._notify(SETTLED_EVENT, (
                f"Kelly horse-race: {settled.window_slug} settled {outcome}"
                + "".join(f"; {m} P&L ${v:+.2f}" for m, v in sorted(pnl.items()))),
                {"window_slug": settled.window_slug, "outcome": outcome, "pnl": pnl})

    # -- endpoints and the switch -----------------------------------------------------

    async def _choose(self, report: PassReport) -> dict[str, _endpoints.Endpoint] | None:
        try:
            points = await _endpoints.endpoints(
                paper=self.paper, gates=self.gates, live=self._live_factory,
                kill_switch_path=self._kill_switch_path)
        except Exception as exc:  # noqa: BLE001 - fail closed: nothing new anywhere
            report.fail("Choosing the endpoints", exc)
            await self._cancel_resting(report, modes=MODES, reason="endpoints_unknown")
            return None
        for mode, point in points.items():
            report.endpoints[mode] = {"state": point.state, "message": point.message,
                                      "active": point.active}
            if point.active and point.venue is not None:
                self.venues[mode] = point.venue
        off = [mode for mode, point in points.items() if not point.active]
        if off:
            await self._cancel_resting(report, modes=off, reason="endpoint_off")
        return points

    async def _cancel_resting(self, report: PassReport, *, modes: Any, reason: str) -> None:
        try:
            rows = [r for r in await _ledger.resting_orders() if r["mode"] in modes]
            await self._cancel_rows(rows, reason=reason, report=report)
        except Exception as exc:  # noqa: BLE001
            report.fail("Cancelling resting orders", exc)

    # -- the decision -------------------------------------------------------------------

    def _draws_for(self, slug: str) -> _Draws:
        if slug not in self._draws:
            self._draws[slug] = _Draws(u1=float(self._rng()), u2=float(self._rng()))
        return self._draws[slug]

    async def _decide(self, points: Mapping[str, _endpoints.Endpoint],
                      report: PassReport) -> None:
        now = self._now()
        hub = self._hub = self._hub_fn()
        try:
            win = await _inputs.window(hub, self._client, now, self.memory)
        except _inputs.NotReady as exc:
            report.window = {"waiting": exc.code, "message": exc.message}
            return
        self._forget_before(win.start)
        self._latest_cid = win.condition_id  # a market trading now, for the tape's freshness
        report.window = {"slug": win.slug, "start": win.start, "end": win.end}
        existing = await _ledger.decision_for(win.slug)
        if existing is not None:
            report.window.update(decision_id=existing["id"], reason=existing["reason"])
            return
        cutoff = win.end - DECISION_CUTOFF_S
        if now >= cutoff:
            waited = self._waiting.pop(win.slug, None)
            if waited is not None:
                await self._record_no_order(win, now, f"{waited.code}: {waited.message} "
                                            "Still missing at the cutoff.", report)
            else:
                report.window.update(waiting="too_late", message=(
                    "This window was already too far gone when this run reached it."))
            return
        draws = self._draws_for(win.slug)
        try:
            k, k_source = await _inputs.price_to_beat(hub, self._client, win, now,
                                                      self.memory, cutoff=cutoff)
            x = _inputs.price_now(hub, now)
            r60, sigma_h = _maths.hour_moves(await _inputs.minute_returns(self._client, now))
        except _inputs.NotReady as exc:
            await self._not_ready(win, now, exc, report)
            return
        tau = (win.end - now) / 3600.0
        chance = _maths.chance_of_up(x.value, k, r60, sigma_h, tau)
        side = _maths.pick_side(chance.p_up, draws.u1)
        token = win.token(side)
        try:
            book = await _inputs.read_book(self._client, token)
        except _inputs.NotReady as exc:
            await self._not_ready(win, now, exc, report)
            return
        fields: dict[str, Any] = {
            "window_slug": win.slug, "condition_id": win.condition_id,
            "up_token": win.up_token, "down_token": win.down_token,
            "window_start": win.start, "window_end": win.end, "ts": int(math.floor(now)),
            "k_price": k, "k_source": k_source, "x_price": x.value, "x_obs_ts": x.obs_ts,
            "r60": r60, "sigma_h": sigma_h, "tau_h": tau, "z": chance.z, "p_up": chance.p_up,
            "u1": draws.u1, "side": side, "u2": draws.u2, "token_id": token,
            "best_bid": book.best_bid, "best_ask": book.best_ask, "bid_size": book.bid_size,
            "tick_size": book.tick_size, "min_order_size": book.min_order_size,
        }
        max_notional = await read_max_notional()
        fields["max_notional_usd"] = max_notional
        size = None
        if book.best_bid is None:
            fields["reason"] = "no_bid: nobody is bidding for that side, so there is no price."
        elif book.best_ask is not None and book.best_bid >= book.best_ask - 1e-9:
            fields["reason"] = (f"book_locked: the best bid {book.best_bid:.2f} meets the best "
                                f"ask {book.best_ask:.2f}.")
        else:
            size = _maths.draw_size(book.best_bid, book.min_order_size, max_notional, draws.u2)
            if size is None:
                fields["reason"] = (
                    f"min_order_over_cap: the minimum order ({book.min_order_size:g} shares at "
                    f"{book.best_bid:.2f}) costs more than the ${max_notional:.2f} cap.")
            else:
                fields.update(price=book.best_bid, shares=size.shares,
                              notional_usd=size.notional_usd)
        try:
            decision_id = await _ledger.record_decision(fields)
        except _ledger.AlreadyDecided:
            return
        self._draws.pop(win.slug, None)
        self._waiting.pop(win.slug, None)
        report.window.update(decision_id=decision_id, reason=fields.get("reason"))
        log.info("kelly.decided", window=win.slug, p_up=round(chance.p_up, 4), u1=draws.u1,
                 side=side, price=fields.get("price"), shares=fields.get("shares"),
                 reason=fields.get("reason"))
        if size is None:
            return
        request = PlaceRequest(
            strategy=STRATEGY, condition_id=win.condition_id, token_id=token, outcome=side,
            up_token=win.up_token, down_token=win.down_token, price=float(book.best_bid),
            size=size.shares, expires_ts=win.end, tick_size=book.tick_size,
            queue_ahead=float(book.bid_size or 0.0), best_ask=book.best_ask,
        )
        for point in points.values():
            if point.active:
                await self._send(point, request, decision_id, win, now, report)

    async def _send(self, point: _endpoints.Endpoint, request: PlaceRequest, decision_id: int,
                    win: _inputs.Window, now: float, report: PassReport) -> None:
        common = dict(decision_id=decision_id, window_slug=win.slug, mode=point.mode,
                      token_id=request.token_id, outcome=request.outcome, price=request.price,
                      size=request.size, queue_ahead=request.queue_ahead,
                      window_end_ts=win.end)
        # The leg's lock spans check, place and commit, so another strategy's order checked at
        # the same moment cannot also fit under a cap that has room for one.
        async with point.gate.lock:
            verdict = await point.gate.check(request.notional_usd, now=now)
            if not verdict.allowed:
                await _ledger.record_order(**common, state="blocked",
                                           reason=f"{verdict.reason}: {verdict.message}")
                log.info("kelly.blocked", mode=point.mode, reason=verdict.reason)
                return
            try:
                placed = await point.venue.place(request, now=now)  # type: ignore[union-attr]
            except PlacementRefused as exc:
                await _ledger.record_order(**common, state="rejected",
                                           reason=f"{exc.reason}: {exc}")
                log.info("kelly.refused", mode=point.mode, reason=exc.reason)
                return
            except Exception as exc:  # noqa: BLE001 - recorded; the other endpoint still goes
                await _ledger.record_order(**common, state="rejected",
                                           reason=f"error: {type(exc).__name__}: {exc}")
                report.fail(f"Placing on {point.mode}", exc)
                return
            row_id = await _ledger.record_order(**common, state="resting",
                                                venue_order_id=placed.order_id,
                                                placed_ts=placed.placed_ts)
            await point.gate.commit(strategy=STRATEGY, order_ref=order_ref(row_id),
                                    notional_usd=request.notional_usd, now=now)

    async def _not_ready(self, win: _inputs.Window, now: float, exc: _inputs.NotReady,
                         report: PassReport) -> None:
        if exc.final:
            await self._record_no_order(win, now, f"{exc.code}: {exc.message}", report)
            return
        self._waiting[win.slug] = exc
        report.window.update(waiting=exc.code, message=exc.message)

    async def _record_no_order(self, win: _inputs.Window, now: float, reason: str,
                               report: PassReport) -> None:
        try:
            decision_id = await _ledger.record_decision({
                "window_slug": win.slug, "condition_id": win.condition_id,
                "up_token": win.up_token, "down_token": win.down_token,
                "window_start": win.start, "window_end": win.end, "ts": int(math.floor(now)),
                "reason": reason,
            })
        except _ledger.AlreadyDecided:
            return
        self._draws.pop(win.slug, None)
        self._waiting.pop(win.slug, None)
        report.window.update(decision_id=decision_id, reason=reason)

    def _forget_before(self, start: int) -> None:
        for book in (self._draws, self._waiting):
            for slug in [s for s in book if _inputs._slug_start(s) < start]:
                book.pop(slug, None)
        self.memory.forget_before(start)

    # -- the card -----------------------------------------------------------------------

    async def _finish(self, report: PassReport) -> None:
        global _STATUS
        errors = list(dict.fromkeys(report.errors))
        state = PASS_FAILED if report.step_failed and report.state == RUNNING else report.state
        _STATUS = {
            "state": state,
            "strategy": STRATEGY,
            "last_pass_ts": report.ts,
            "passes": int(_STATUS.get("passes") or 0) + 1,
            "errors": errors,
            "last_error": errors[-1] if errors else _STATUS.get("last_error"),
            "last_error_ts": report.ts if errors else _STATUS.get("last_error_ts"),
            "endpoints": report.endpoints,
            "window": report.window,
            "fills": report.fills,
            "settled": len(report.settled),
            "cancelled": report.cancelled,
        }
        await _save_status()

    async def _notify(self, event: str, message: str, details: Mapping[str, Any]) -> None:
        try:
            await _db.notify(event, message, dict(details))
        except Exception as exc:  # noqa: BLE001 - the card still shows it
            log.warning("kelly.notify_failed", event=event, error=str(exc))

    def release(self) -> None:
        """Give up the market data this strategy asked for."""
        for hub in {id(h): h for h in (self._hub, _current_hub()) if h is not None}.values():
            try:
                hub.release(OWNER)
            except Exception as exc:  # noqa: BLE001
                log.warning("kelly.release_failed", error=str(exc))


def _current_hub() -> Any:
    from ems.marketdata import hub as _hub

    return _hub.current()


async def _sleep(stop_event: asyncio.Event | None, seconds: float) -> None:
    seconds = max(MIN_SLEEP_S, seconds)
    if stop_event is None:
        await asyncio.sleep(seconds)
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


def _stopped(stop_event: asyncio.Event | None) -> bool:
    return stop_event is not None and stop_event.is_set()


async def run_forever(stop_event: asyncio.Event | None = None) -> None:
    """Pass after pass until ``stop_event`` is set. Paper always; live once built and armed."""
    global _STATUS
    failed = False
    runner: Runner | None = None
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            runner = Runner(client)
            while not _stopped(stop_event):
                started = time.monotonic()
                try:
                    await runner.pass_once()
                except Exception as exc:  # noqa: BLE001 - the loop keeps its cadence
                    log.exception("kelly.pass_failed")
                    await _record_failure(f"A pass failed before it finished: "
                                          f"{type(exc).__name__}: {exc}", time.time(),
                                          PASS_FAILED)
                interval = await read_poll_interval()
                await _sleep(stop_event, interval - (time.monotonic() - started))
    except Exception as exc:  # noqa: BLE001
        failed = True
        log.exception("kelly.loop_died")
        await _record_failure(f"The loop stopped: {type(exc).__name__}: {exc}", time.time(),
                              STOPPED_ON_ERROR)
    finally:
        if runner is not None:
            runner.release()
        _STATUS = {**_STATUS, "state": STOPPED_ON_ERROR if failed else STOPPED}
