"""The live resting-order venue: post-only good-till-date limit BUYs on the Polymarket CLOB.

The same calls as the paper venue (``ems/execution/resting.py``), so a strategy sends one
``PlaceRequest`` to either. Authorised only as AGENTS.md "Live trading" records: BTC 15m
Up/Down, Kelly horse-race, armed by the operator. Nothing here decides whether LIVE is armed
(``ems/execution/live_control.py`` does); this is only how an order reaches the exchange.

- ``place``: one limit BUY, ``OrderType.GTD`` with ``post_only=True`` (the exchange refuses an
  order that would cross, so it can only ever rest) and ``expiration`` the request's expiry
  (the window end; the exchange stops it 60 s before). The kill switch file blocks it, and
  :func:`validate_request` checks it, before anything is sent. When the send fails with no
  reply, the order may still have reached the exchange: its open orders are searched for it
  (:meth:`ClobRestingVenue.find_order`); found, it is followed as placed; not searchable,
  ``OutcomeUnknown`` says so and the strategy keeps looking.
- ``fills``: ``get_order`` per order: ``size_matched`` is the shares filled and ``status``
  says whether it still rests (``live``) or is done (``matched``, ``canceled``). An empty
  reply is a miss, not an end: the order is left out until it has been gone long enough.
- ``cancel``: by order id (``cancel_orders``), never "cancel all": the account may hold
  orders this app did not place. A cancel that fails or is refused goes to ``last_errors``.
- ``last_errors``: what went wrong since the caller last read it; the caller clears it.
- Every placement and cancel, refused and failed ones included, is journaled to
  ``live_orders`` (``journal.py``) with the strategy in its details.

The private key is read from ``ems.config`` only to build the client and is never logged or
journaled; the API credentials the client derives from it are added to the log scrubber.
The client library (``py-clob-client-v2``, the ``live`` extra) is imported only here, and
only when a live venue is opened.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import math
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import structlog

from ems import config as _config  # type: ignore[import-untyped]
from ems import logging_setup as _logging
from ems.execution import controls as _controls
from ems.execution import journal as _journal
from ems.execution.controls import OutcomeUnknown as OutcomeUnknown
from ems.execution.controls import PlacementRefused
from ems.execution.resting import (
    CANCELLED,
    EXPIRED,
    FILLED,
    GTD_STOP_S,
    LIVE,
    RESTING,
    OrderView,
    Placed,
    PlaceRequest,
    validate_request,
)

log = structlog.get_logger(__name__)

LIBRARY = "py_clob_client_v2"
SAFE_SIGNATURE_TYPE = 2  # the Polymarket Safe (MetaMask) setup, the only one accepted
ORDER_TYPE = "GTD"
# get_order statuses after which an order can never trade again.
TERMINAL = {"matched": FILLED, "canceled": CANCELLED, "cancelled": CANCELLED,
            "expired": EXPIRED, "invalid": CANCELLED}
# An order the exchange still reports open this long after its GTD stop is taken as closed
# (flagged forced), so its window can settle; the log says so.
STUCK_AFTER_STOP_S = 120
# An order whose status reply comes back empty is taken as gone only after its stop plus
# STUCK_AFTER_STOP_S, or, when its expiry is not known, after this many empty replies in a row.
MISSES_BEFORE_GONE = 60
# After a send with no reply: how many times the open orders are searched, and the wait
# between searches (the exchange can take a moment to list a new order).
LOOKUPS = 3
LOOKUP_WAIT_S = 1.0

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[Any]]


class LiveUnavailable(RuntimeError):
    """The live venue cannot open: its config, its library or the exchange said no."""


def wallet_problems() -> list[str]:
    """Every reason a live venue cannot open now (empty: it can). Read at call time."""
    problems: list[str] = []
    if getattr(_config, "CONFIG_PARSE_ERRORS", None):
        problems.append("these env values did not parse: " + "; ".join(_config.CONFIG_PARSE_ERRORS))
    if not _config.POLYMARKET_PRIVATE_KEY:
        problems.append("POLYMARKET_PRIVATE_KEY is not set")
    if _config.POLYMARKET_SIGNATURE_TYPE != SAFE_SIGNATURE_TYPE:
        problems.append(f"POLYMARKET_SIGNATURE_TYPE is {_config.POLYMARKET_SIGNATURE_TYPE}, not "
                        f"{SAFE_SIGNATURE_TYPE} (the Polymarket Safe)")
    if not _config.POLYMARKET_FUNDER:
        problems.append("POLYMARKET_FUNDER (your Polymarket wallet address) is not set")
    if importlib.util.find_spec(LIBRARY) is None:
        problems.append('the live library is not installed (pip install -e ".[live]")')
    return problems


def _library() -> Any:
    return importlib.import_module(LIBRARY)


def build_client() -> Any:
    """A signed-in CLOB client from the wallet config. Blocking: run it in a thread.

    Raises ``LiveUnavailable`` with plain-English reasons; never with the key in them.
    """
    problems = wallet_problems()
    if problems:
        raise LiveUnavailable("; ".join(problems))
    lib = _library()
    try:
        client = lib.ClobClient(
            _config.POLYMARKET_CLOB_API, chain_id=_config.POLYMARKET_CHAIN_ID,
            key=_config.POLYMARKET_PRIVATE_KEY, signature_type=_config.POLYMARKET_SIGNATURE_TYPE,
            funder=_config.POLYMARKET_FUNDER,
        )
        creds = client.create_or_derive_api_key()
        if creds is None:
            raise LiveUnavailable("the exchange gave no API credentials for this wallet")
        for secret in (getattr(creds, "api_secret", None), getattr(creds, "api_passphrase", None)):
            _logging.register_secret(secret)
        client.set_api_creds(creds)
        client.get_ok()
    except LiveUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - reported in plain words, scrubbed
        raise LiveUnavailable(_logging.redact_secrets(
            f"the exchange sign-in failed ({type(exc).__name__}: {exc})")) from exc
    return client


def _tick_text(tick: float) -> str:
    return format(float(tick), "g")


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class ClobRestingVenue:
    """Live orders on the CLOB, through a signed-in client (``build_client``, or a test
    double with the same methods). ``api`` gives the order types (the library by default)."""

    mode = LIVE

    def __init__(self, client: Any, *, clock: Clock = time.time,
                 kill_switch_path: Path | str | None = None, api: Any = None,
                 sleep: Sleep = asyncio.sleep) -> None:
        self._client = client
        self.clock = clock
        self._kill_switch_path = kill_switch_path
        self._api = api
        self._sleep = sleep
        self._cancelled: dict[str, int] = {}  # our own cancels: id -> the second written
        self._expiry: dict[str, int] = {}  # id -> the expiry it was sent with
        self._last: dict[str, OrderView] = {}  # the last view of each order
        self._misses: dict[str, int] = {}  # id -> empty status replies in a row
        self.last_errors: list[str] = []

    def _time(self, now: float | None) -> float:
        return float(self.clock()) if now is None else float(now)

    def _types(self) -> Any:
        if self._api is None:
            lib = _library()
            self._api = SimpleNamespace(OrderArgs=lib.OrderArgs, OrderType=lib.OrderType,
                                        PartialCreateOrderOptions=lib.PartialCreateOrderOptions,
                                        OpenOrderParams=lib.OpenOrderParams)
        return self._api

    def _error(self, message: str) -> None:
        self.last_errors.append(_logging.redact_secrets(message))

    async def place(self, request: PlaceRequest, *, now: float | None = None) -> Placed:
        """Send one post-only GTD limit BUY. Raises ``PlacementRefused`` when it is not sent
        or the exchange refuses it, and ``OutcomeUnknown`` when the send failed and the open
        orders could not be searched; every outcome is journaled."""
        t = self._time(now)
        where = dict(window_slug=None, token_id=request.token_id, price=request.price,
                     size=request.size, order_type=ORDER_TYPE)
        details = {"strategy": request.strategy, "post_only": True,
                   "expiration": int(request.expires_ts), "condition_id": request.condition_id,
                   "outcome": request.outcome}
        try:
            if _controls.kill_switch_active(self._kill_switch_path):
                raise PlacementRefused("kill_switch", "The kill switch is on, so no new orders "
                                       "are placed.")
            validate_request(request, t)
        except PlacementRefused as exc:
            await _journal.journal_live_order(intent=_journal.ENTRY, side="BUY",
                                              status=_journal.BLOCKED, error=f"{exc.reason}: {exc}",
                                              details=details, **where)
            raise
        api = self._types()
        args = api.OrderArgs(token_id=request.token_id, price=float(request.price),
                             size=float(request.size), side="BUY",
                             expiration=int(request.expires_ts))
        options = api.PartialCreateOrderOptions(tick_size=_tick_text(request.tick_size))
        try:
            raw = await asyncio.to_thread(self._client.create_and_post_order, args, options,
                                          api.OrderType.GTD, True)
        except Exception as exc:  # noqa: BLE001 - never retried blind: searched for instead
            return await self._after_lost_reply(request, exc, details, where, t)
        response = raw if isinstance(raw, Mapping) else {}
        order_id = str(response.get("orderID") or response.get("orderId") or "")
        ok = bool(response.get("success", bool(order_id))) and bool(order_id)
        if not ok:
            error = _logging.redact_secrets(str(response.get("errorMsg") or response or raw))
            await _journal.journal_live_order(intent=_journal.ENTRY, side="BUY",
                                              status=_journal.REJECTED, error=error,
                                              details={**details, "response": response}, **where)
            log.warning("clob_venue.rejected", error=error)
            raise PlacementRefused("venue_rejected", f"The exchange refused the order: {error}")
        self._expiry[order_id] = int(request.expires_ts)
        await self._journal_submitted(order_id, {**details, "response": response}, where)
        log.info("clob_venue.placed", strategy=request.strategy, order=order_id,
                 outcome=request.outcome, price=request.price, size=request.size,
                 status=response.get("status"))
        return Placed(order_id=order_id, placed_ts=int(math.floor(t)))

    async def _journal_submitted(self, order_id: str, details: Mapping[str, Any],
                                 where: Mapping[str, Any]) -> None:
        """Journal an order the exchange took. A journal failure is logged and kept in
        ``last_errors``, never turned into a refusal: the order rests either way."""
        try:
            await _journal.journal_live_order(intent=_journal.ENTRY, side="BUY",
                                              status=_journal.SUBMITTED, clob_order_id=order_id,
                                              details=dict(details), **where)
        except Exception as exc:  # noqa: BLE001 - the order is placed; say the journal missed it
            error = _logging.redact_secrets(f"{type(exc).__name__}: {exc}")
            self._error(f"order {order_id[:12]} was placed but not journaled: {error}")
            log.error("clob_venue.journal_failed", order=order_id, error=error)

    async def _after_lost_reply(self, request: PlaceRequest, exc: Exception,
                                details: Mapping[str, Any], where: Mapping[str, Any],
                                t: float) -> Placed:
        """The send raised, so the exchange may or may not have the order. Search its open
        orders for it; never send it again blind."""
        error = _logging.redact_secrets(f"{type(exc).__name__}: {exc}")
        log.warning("clob_venue.place_failed", error=error)
        asked = False
        for attempt in range(LOOKUPS):
            if attempt:
                await self._sleep(LOOKUP_WAIT_S)
            try:
                found = await self.find_order(token_id=request.token_id, price=request.price,
                                              size=request.size, expires_ts=request.expires_ts)
            except Exception as lookup_exc:  # noqa: BLE001 - tried again, then reported
                log.warning("clob_venue.lookup_failed",
                            error=_logging.redact_secrets(f"{type(lookup_exc).__name__}"))
                continue
            asked = True
            if found is not None:
                self._expiry[found] = int(request.expires_ts)
                await self._journal_submitted(
                    found, {**details, "recovered": f"the send failed ({error}) but the order "
                            "was found among the open orders"}, where)
                log.warning("clob_venue.recovered", order=found)
                return Placed(order_id=found, placed_ts=int(math.floor(t)))
        if asked:
            await _journal.journal_live_order(
                intent=_journal.ENTRY, side="BUY", status=_journal.ERROR,
                error=f"{error}; not among the open orders", details=dict(details), **where)
            raise PlacementRefused("venue_error", f"The order could not be sent ({error}) and "
                                   "is not among the exchange's open orders.") from exc
        await _journal.journal_live_order(
            intent=_journal.ENTRY, side="BUY", status=_journal.ERROR,
            error=f"{error}; outcome unknown: the open orders could not be read",
            details=dict(details), **where)
        raise OutcomeUnknown(f"The send failed ({error}) and the exchange's open orders could "
                             "not be read, so whether the order rests there is not known "
                             "yet.") from exc

    async def find_order(self, *, token_id: str, price: float, size: float,
                         expires_ts: int) -> str | None:
        """The id of an open BUY on ``token_id`` with this price, size and expiry that this
        venue does not already follow, or None. Raises when the open orders cannot be read."""
        api = self._types()
        params_type = getattr(api, "OpenOrderParams", None) or SimpleNamespace
        raw = await asyncio.to_thread(self._client.get_open_orders,
                                      params_type(asset_id=str(token_id)))
        known = set(self._expiry) | set(self._last)
        for order in raw if isinstance(raw, list) else []:
            if not isinstance(order, Mapping):
                continue
            order_id = str(order.get("id") or "")
            if (not order_id or order_id in known
                    or str(order.get("side") or "").upper() != "BUY"
                    or str(order.get("asset_id") or token_id) != str(token_id)):
                continue
            if (abs((_float(order.get("price")) or -1.0) - float(price)) <= 1e-9
                    and abs((_float(order.get("original_size")) or -1.0) - float(size)) <= 1e-6
                    and int(_float(order.get("expiration")) or 0) == int(expires_ts)):
                return order_id
        return None

    async def cancel(self, order_ids: Iterable[str], *, reason: str,
                     now: float | None = None) -> int:
        """Cancel by id. Returns how many the exchange confirmed cancelled."""
        ids = [str(i) for i in order_ids if i]
        if not ids:
            return 0
        stamp = int(math.floor(self._time(now)))
        try:
            raw = await asyncio.to_thread(self._client.cancel_orders, ids)
        except Exception as exc:  # noqa: BLE001 - journaled; tried again next pass
            error = _logging.redact_secrets(f"{type(exc).__name__}: {exc}")
            for order_id in ids:
                await _journal.journal_live_order(intent=_journal.CANCEL, side="-",
                                                  status=_journal.ERROR, clob_order_id=order_id,
                                                  error=error, details={"reason": reason})
            self.last_errors.append(f"the cancel of {len(ids)} order(s) failed: {error}")
            log.warning("clob_venue.cancel_failed", orders=len(ids), error=error)
            return 0
        response = raw if isinstance(raw, Mapping) else {}
        done = {str(i) for i in response.get("canceled") or []}
        refused = response.get("not_canceled") or {}
        count = 0
        for order_id in ids:
            if order_id in done:
                self._cancelled[order_id] = stamp
                count += 1
                await _journal.journal_live_order(intent=_journal.CANCEL, side="-",
                                                  status=_journal.CANCELLED,
                                                  clob_order_id=order_id,
                                                  details={"reason": reason})
            else:
                why = refused.get(order_id) if isinstance(refused, Mapping) else None
                error = str(why or "not confirmed cancelled")
                await _journal.journal_live_order(intent=_journal.CANCEL, side="-",
                                                  status=_journal.ERROR, clob_order_id=order_id,
                                                  error=error, details={"reason": reason})
                self._error(f"order {order_id[:12]}: the cancel was refused ({error})")
                log.warning("clob_venue.cancel_refused", order=order_id, error=error)
        return count

    async def fills(self, order_ids: Iterable[str], *,
                    now: float | None = None) -> dict[str, OrderView]:
        """Each order as the exchange reports it. An order whose read fails, or whose reply is
        empty, is left out (``last_errors`` says so) and asked again next pass; one the
        exchange has stopped returning for long enough (pruned once done) keeps its last
        view, made final."""
        t = self._time(now)
        out: dict[str, OrderView] = {}
        for order_id in [str(i) for i in order_ids if i]:
            try:
                raw = await asyncio.to_thread(self._client.get_order, order_id)
            except Exception as exc:  # noqa: BLE001 - asked again next pass
                self._error(f"order {order_id[:12]}: the status read failed "
                            f"({type(exc).__name__})")
                continue
            view = self._view(order_id, raw, t)
            if view is None:
                self._error(f"order {order_id[:12]}: the exchange returned no status; asked "
                            "again next pass")
                continue
            self._last[order_id] = view
            out[order_id] = view
        return out

    def _gone(self, order_id: str, t: float) -> bool:
        """Whether an order with an empty status reply has been missing long enough to be
        taken as gone: past its stop plus STUCK_AFTER_STOP_S, or, with no known expiry, after
        MISSES_BEFORE_GONE empty replies in a row."""
        misses = self._misses[order_id] = self._misses.get(order_id, 0) + 1
        expiry = self._expiry.get(order_id)
        if expiry:
            return t >= expiry - GTD_STOP_S + STUCK_AFTER_STOP_S
        return misses >= MISSES_BEFORE_GONE

    def _view(self, order_id: str, raw: Any, t: float) -> OrderView | None:
        if not isinstance(raw, Mapping) or not raw:
            if not self._gone(order_id, t):
                return None
            log.warning("clob_venue.order_gone", order=order_id)
            last = self._last.get(order_id)
            if last is not None:
                return OrderView(order_id, last.state if last.closed else EXPIRED, last.size,
                                 last.filled_size, last.closed_ts or int(t), True,
                                 forced=not last.closed)
            return OrderView(order_id, EXPIRED, 0.0, 0.0, int(t), True, forced=True)
        self._misses.pop(order_id, None)
        size = _float(raw.get("original_size")) or _float(raw.get("size")) or 0.0
        matched = min(size, _float(raw.get("size_matched")) or 0.0) if size else (
            _float(raw.get("size_matched")) or 0.0)
        status = str(raw.get("status") or "").lower()
        expiry = self._expiry.get(order_id) or int(_float(raw.get("expiration")) or 0) or None
        if expiry:
            self._expiry[order_id] = expiry
        stop = expiry - GTD_STOP_S if expiry else None
        if status in TERMINAL:
            state = TERMINAL[status]
            if state == CANCELLED and order_id not in self._cancelled and stop and t >= stop:
                state = EXPIRED
            if size and matched >= size - 1e-9:
                state = FILLED
            closed = self._cancelled.get(order_id) if state == CANCELLED else (
                stop if state == EXPIRED else None)
            return OrderView(order_id, state, size, matched, int(closed or t), True)
        if stop is not None and t >= stop + STUCK_AFTER_STOP_S:
            log.warning("clob_venue.open_past_its_stop", order=order_id, status=status)
            return OrderView(order_id, EXPIRED, size, matched, int(stop), True, forced=True)
        return OrderView(order_id, RESTING, size, matched, None, False)


async def open_live_venue(*, clock: Clock = time.time,
                          kill_switch_path: Path | str | None = None) -> ClobRestingVenue:
    """Sign in and return the live venue. Raises ``LiveUnavailable`` (reasons in plain words)."""
    client = await asyncio.to_thread(build_client)
    return ClobRestingVenue(client, clock=clock, kill_switch_path=kill_switch_path)
