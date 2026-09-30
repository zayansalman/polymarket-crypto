"""The lc2004-Kronos BTC 24h forecast loop: one hourly model run for the dashboard card.

``run_forever(stop_event)`` runs for the dashboard's lifetime (started in the app lifespan).
It waits ``STARTUP_DELAY_S`` on the stop event first, so an app that is started and stopped
at once (the test suite, a quick restart) never starts a forecast. Then one pass a minute,
waking a few seconds after each hourly close so a new hour's forecast starts at once, when a
running forecast ends, and when a pending retry is due:

1. The ``lc2004_forecast_enabled`` setting. Off: any running forecast is cancelled (its worker
   is killed) and nothing else happens; the card says it is switched off in Settings.
2. The window (12:00 ET to 12:00 ET) and its Polymarket market, looked up once per window
   (again after ``MARKET_RETRY_S`` while Gamma does not list it). A failed lookup is an error
   on the card, not "not listed yet".
3. The strike: the Binance BTCUSDT 1m close at 12:00 ET on the window's first day, once that
   candle has closed. Until then the card says it is waiting for the noon candle; a failed
   read after that is an error on the card.
4. Both outcomes' books, read every pass for the card.
5. The forecast: when the weights are in place, the strike is known, no forecast is stored
   for (window, last closed 1h candle) and none is running, ONE background task runs
   ``forecast.run_and_store`` with the book tops of this pass. The model takes minutes, so a
   pass never waits for it. It is cancelled on shutdown or when switched off, and the Kronos
   client then kills the worker's process group. Retries are timed from when the job failed:
   missing candles ``DATA_RETRY_S`` later, other failures before the model a minute later, and
   a row that could not be stored after the model ran not until the next hour, so the model
   never runs twice for one hour.

Every step is guarded: a failure is logged and shown on the card (``status()``), never raised
out of the loop. Display-only: nothing here places, cancels or simulates an order. Zayan
(operator), 2026-09-29: "this doesnt need to trade for me actually just show its prediction
in the regime overview or somewhere in the dashboard and ill go manaulla place a trade".

Claude, 2026-09-29. The forecast part of last week's runner
(``polymarket_bot/lc2004_kronos_btc_24h/runner.py``, Claude, 2026-09-22), without its orders.
"""
from __future__ import annotations

import asyncio
import copy
import time
from contextlib import suppress
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

from ems import runtime_knobs as _knobs
from ems.kronos_forecast import client as _kronos
from ems.lc2004_kronos_btc_24h import forecast as _forecast
from ems.lc2004_kronos_btc_24h import ledger as _ledger
from ems.lc2004_kronos_btc_24h import market as _market
from ems.logging_setup import get_logger, redact_secrets

log = get_logger("lc2004_kronos_btc_24h.runner")

KNOB = "lc2004_forecast_enabled"
# Wait this long after boot before the first pass, on the stop event, so shutdown is instant.
STARTUP_DELAY_S = 15.0
POLL_S = 60.0
MIN_SLEEP_S = 1.0
HTTP_TIMEOUT_S = 25.0
# Gamma lists the daily market about two days ahead; while it does not, look again this late.
MARKET_RETRY_S = 60.0
# After Binance's hourly candles were missing or stale, try that hour again this much later.
DATA_RETRY_S = 20.0
# Wake this long after each hourly close: Binance opens the new hour on its own clock.
FORECAST_LAG_S = 5
# How long a shutdown waits for a cancelled forecast's worker to be killed and reaped.
CANCEL_WAIT_S = 5.0
HOUR_S = 3600
MINUTE_S = 60
ERROR_CHARS = 300
# The job's own clock: how long it ran before it failed (a test can stand in for it).
_monotonic = time.monotonic

# Loop states, shown on the card.
NOT_STARTED = "not_started"
RUNNING = "running"
SWITCHED_OFF = "switched_off"
PASS_FAILED = "pass_failed"
STOPPED = "stopped"
STOPPED_ON_ERROR = "stopped_on_error"

WAITING_FOR_STRIKE = ("Waiting for the noon candle: the strike is the Binance BTCUSDT 1m close "
                      "at 12:00 ET, known once that minute has closed.")
WAITING_FOR_MARKET = ("Polymarket does not list this window's market yet; looking again in a "
                      "minute.")

# What the last pass saw, for the card (this process only). Never read for decisions.
_STATUS: dict[str, Any] = {"state": NOT_STARTED, "last_pass_ts": None, "last_error": None}


def status() -> dict[str, Any]:
    """This process's latest pass, for the card."""
    return copy.deepcopy(_STATUS)


def _set_status(**fields: Any) -> None:
    global _STATUS
    _STATUS = {**_STATUS, **fields}


def _error_text(message: str) -> str:
    return redact_secrets(message)[:ERROR_CHARS]


@dataclass
class RunnerState:
    """What the loop keeps between passes. The ``lc2004_forecasts`` table is the record."""

    markets: dict[str, _market.BtcDailyMarket] = field(default_factory=dict)
    market_retry_at: dict[str, float] = field(default_factory=dict)
    strikes: dict[str, float] = field(default_factory=dict)
    job: asyncio.Task[Any] | None = None
    job_key: tuple[str, int] | None = None
    job_started_ts: float | None = None
    retry_at: dict[tuple[str, int], float] = field(default_factory=dict)
    # Why the last forecast job stored no row (Binance's candles not ready), or None.
    note: str | None = None

    def running(self) -> bool:
        return self.job is not None and not self.job.done()


def next_hourly_close(ts: float) -> int:
    return (int(ts) // HOUR_S + 1) * HOUR_S


def next_forecast_after(window: _market.DayWindow, now_ts: float,
                        strike_known: bool) -> int | None:
    """When the next forecast can start: a few seconds after the next hourly close.

    Before the strike is known, that is when the noon candle closes; once that time has
    passed without a strike (its read failed), None: it starts as soon as the strike is read,
    and the card shows the failure instead of a time already gone. The window's last hourly
    close is the next window's start, whose forecast waits for that window's noon candle.
    """
    if not strike_known:
        noon_read = int(window.start_ts) + MINUTE_S + FORECAST_LAG_S
        return noon_read if now_ts < noon_read else None
    next_close = next_hourly_close(now_ts)
    if next_close >= window.end_ts:
        return int(window.end_ts) + MINUTE_S + FORECAST_LAG_S
    return next_close + FORECAST_LAG_S


def sleep_seconds(now_ts: float, wake_at: Iterable[float] = ()) -> float:
    """Until the next pass: ``POLL_S``, or less so a pass lands just after the hourly close or
    at the earliest of ``wake_at`` (epoch seconds: a pending retry, the noon candle's close).
    Times already past are ignored; never less than ``MIN_SLEEP_S``.
    """
    until_close = next_hourly_close(now_ts) + FORECAST_LAG_S - now_ts
    soonest = min([POLL_S, until_close, *(t - now_ts for t in wake_at if t > now_ts)])
    return max(MIN_SLEEP_S, soonest)


def wake_times(state: RunnerState, now_ts: float) -> list[float]:
    """When the loop should look again besides its minute: pending retries, the noon candle."""
    times = [*state.retry_at.values(), *state.market_retry_at.values()]
    window = _market.window_at(now_ts)
    if window.slug not in state.strikes:
        times.append(float(window.start_ts + MINUTE_S + FORECAST_LAG_S))
    return times


async def forecast_enabled() -> bool:
    """The operator's Settings switch for the hourly model run."""
    return bool(await _knobs.get(KNOB))


# ---------------------------------------------------------------------------------------------
# The window's market, strike and books
# ---------------------------------------------------------------------------------------------


async def _get_market(state: RunnerState, client: httpx.AsyncClient, window: _market.DayWindow,
                      now_ts: float) -> _market.BtcDailyMarket | None:
    if window.slug in state.markets:
        return state.markets[window.slug]
    if now_ts < state.market_retry_at.get(window.slug, 0.0):
        return None
    found = await _market.discover(client, window)
    if found is None:
        state.market_retry_at = {window.slug: now_ts + MARKET_RETRY_S}
        return None
    state.markets = {window.slug: found}
    state.market_retry_at = {}
    return found


async def _get_strike(state: RunnerState, client: httpx.AsyncClient, window: _market.DayWindow,
                      now_ts: float) -> float | None:
    if window.slug in state.strikes:
        return state.strikes[window.slug]
    strike = await _market.fetch_minute_close(client, window.start_ts, now_ts)
    if strike is not None:
        state.strikes = {window.slug: strike}
    return strike


def _top(book: _market.Book | None) -> dict[str, float | None] | None:
    return None if book is None else {"bid": book.best_bid, "ask": book.best_ask}


async def _read_books(client: httpx.AsyncClient, market: _market.BtcDailyMarket,
                      now_ts: float) -> dict[str, Any]:
    """Both outcomes' best bid and ask; an outcome whose book could not be read is None."""
    return {
        "ts": now_ts,
        "up": _top(await _market.fetch_book(client, market.up_token)),
        "down": _top(await _market.fetch_book(client, market.down_token)),
    }


def book_tops(books: dict[str, Any] | None) -> dict[str, float | None] | None:
    """The four prices ``forecast.run_and_store`` stores with the row."""
    if not books:
        return None
    up, down = books.get("up") or {}, books.get("down") or {}
    return {"up_bid": up.get("bid"), "up_ask": up.get("ask"),
            "down_bid": down.get("bid"), "down_ask": down.get("ask")}


# ---------------------------------------------------------------------------------------------
# The background forecast
# ---------------------------------------------------------------------------------------------


async def _forecast_job(state: RunnerState, client: httpx.AsyncClient,
                        window: _market.DayWindow, strike: float, candle_open_ms: int,
                        now_ts: float, tops: dict[str, float | None] | None) -> int | None:
    """One ``run_and_store``; a failure is shown and its retry is timed from the failure.

    ``now_ts`` is the pass that started the job, and the model takes minutes, so the failure
    time is ``now_ts`` plus how long the job ran. Missing candles: ``DATA_RETRY_S`` later.
    The model ran but its row was not stored: not before the next hourly close, when the hour
    to forecast is a new one, so the model never runs twice for one hour. Anything else
    failed before the model ran: a minute later (Claude, 2026-09-29, for a review finding).
    """
    key = (window.slug, candle_open_ms)
    started = _monotonic()
    try:
        forecast_id = await _forecast.run_and_store(
            client, window=window, strike=strike, last_candle_open_ms=candle_open_ms,
            now_ts=now_ts, book_tops=tops)
    except asyncio.CancelledError:
        raise
    except _market.MarketDataError as exc:
        state.retry_at = {key: now_ts + (_monotonic() - started) + DATA_RETRY_S}
        state.note = _error_text(f"Waiting for Binance's hourly candles: {exc}")
        log.warning("lc2004.forecast_data_not_ready", window=window.slug, error=str(exc))
        _set_status(forecast_note=state.note, forecast_running=None)
        return None
    except _forecast.ForecastNotStoredError as exc:
        failed_at = now_ts + (_monotonic() - started)
        state.retry_at = {key: float(next_hourly_close(failed_at))}
        log.error("lc2004.forecast_not_stored", window=window.slug, error=str(exc))
        _set_status(forecast_running=None, last_error_ts=failed_at,
                    last_error=_error_text(str(exc)))
        return None
    except Exception as exc:  # noqa: BLE001 - shown on the card, tried again later
        failed_at = now_ts + (_monotonic() - started)
        state.retry_at = {key: failed_at + POLL_S}
        log.exception("lc2004.forecast_job_failed", window=window.slug)
        _set_status(forecast_running=None, last_error_ts=failed_at, last_error=_error_text(
            f"The forecast run failed: {type(exc).__name__}: {exc}"))
        return None
    state.note = None
    _set_status(forecast_note=None, forecast_running=None)
    return forecast_id


def _start_forecast(state: RunnerState, client: httpx.AsyncClient, window: _market.DayWindow,
                    strike: float, candle_open_ms: int, now_ts: float,
                    tops: dict[str, float | None] | None) -> None:
    state.job = asyncio.create_task(
        _forecast_job(state, client, window, strike, candle_open_ms, now_ts, tops),
        name=f"lc2004-forecast-{window.slug}-{candle_open_ms}")
    state.job_key = (window.slug, candle_open_ms)
    state.job_started_ts = now_ts
    log.info("lc2004.forecast_job_started", window=window.slug, candle_open_ms=candle_open_ms)


async def _maybe_start_forecast(state: RunnerState, client: httpx.AsyncClient,
                                window: _market.DayWindow, strike: float, now_ts: float,
                                tops: dict[str, float | None] | None) -> bool:
    """Start the forecast for the last closed 1h candle if none is stored, due or running."""
    if state.running():
        return False
    if state.job is not None:  # finished: forget it
        state.job, state.job_key, state.job_started_ts = None, None, None
    candle_open_ms = _forecast.target_candle_open_ms(now_ts)
    if _forecast.horizon_hours(window.end_ts, candle_open_ms) < 1:
        return False
    if now_ts < state.retry_at.get((window.slug, candle_open_ms), 0.0):
        return False
    if await _ledger.forecast_exists(window.slug, candle_open_ms):
        return False
    _start_forecast(state, client, window, strike, candle_open_ms, now_ts, tops)
    return True


async def stop_forecast(state: RunnerState) -> None:
    """Cancel a running forecast and wait for it; the Kronos client kills the worker's group."""
    job, state.job, state.job_key, state.job_started_ts = state.job, None, None, None
    if job is None:
        return
    if not job.done():
        job.cancel()
        await asyncio.wait({job}, timeout=CANCEL_WAIT_S)
        if not job.done():
            log.warning("lc2004.forecast_cancel_slow", wait_s=CANCEL_WAIT_S)
            return
        log.info("lc2004.forecast_cancelled")


def _running(state: RunnerState) -> dict[str, Any] | None:
    if not state.running() or state.job_key is None:
        return None
    return {"window": state.job_key[0], "candle_open_ms": state.job_key[1],
            "started_ts": state.job_started_ts}


# ---------------------------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------------------------


async def pass_once(state: RunnerState, client: httpx.AsyncClient,
                    now_ts: float) -> dict[str, Any]:
    """One pass: the switch, the window, its market, strike and books, and the forecast.

    Returns ``{"state", "forecast_started", "errors"}``. Each step is guarded, so a failure is
    recorded in the status and the rest of the pass still runs.
    """
    errors: list[str] = []

    def failed(step: str, exc: Exception) -> None:
        log.exception("lc2004.step_failed", step=step)
        errors.append(_error_text(f"{step} failed: {type(exc).__name__}: {exc}"))

    try:
        enabled = await forecast_enabled()
    except Exception as exc:  # noqa: BLE001 - the setting's default (on) applies
        failed("Reading the Settings switch", exc)
        enabled = bool(_knobs.KNOBS[KNOB].default)
    window = _market.window_at(now_ts)
    base = {
        "last_pass_ts": now_ts,
        "window": {"slug": window.slug, "start_ts": window.start_ts, "end_ts": window.end_ts},
    }
    if not enabled:
        await stop_forecast(state)
        _finish(state, base, SWITCHED_OFF, errors, now_ts)
        return {"state": SWITCHED_OFF, "forecast_started": False, "errors": errors}

    missing: str | None = None
    try:
        missing = _kronos.missing_weights()
    except Exception as exc:  # noqa: BLE001
        failed("Checking the model weights", exc)

    # A failed lookup or read is an error on the card, never shown as waiting.
    market, market_failed = None, False
    try:
        market = await _get_market(state, client, window, now_ts)
    except Exception as exc:  # noqa: BLE001
        market_failed = True
        failed("Looking up the market", exc)

    strike, strike_failed = None, False
    try:
        strike = await _get_strike(state, client, window, now_ts)
    except Exception as exc:  # noqa: BLE001
        strike_failed = True
        failed("Reading the strike", exc)

    books = None
    if market is not None:
        try:
            books = await _read_books(client, market, now_ts)
        except Exception as exc:  # noqa: BLE001
            failed("Reading the books", exc)

    started = False
    if strike is not None and missing is None:
        try:
            started = await _maybe_start_forecast(state, client, window, strike, now_ts,
                                                  book_tops(books))
        except Exception as exc:  # noqa: BLE001
            failed("Starting the forecast", exc)

    waiting = [text for text, show in (
        (WAITING_FOR_MARKET, market is None and not market_failed),
        (WAITING_FOR_STRIKE, strike is None and not strike_failed)) if show]
    base.update(
        market=({"slug": market.slug, "question": market.question}
                if market is not None else None),
        strike=strike,
        strike_failed=strike_failed,
        books=books,
        missing_weights=missing,
        waiting=waiting,
        forecast_note=state.note,
        next_forecast_after=next_forecast_after(window, now_ts, strike is not None),
    )
    _finish(state, base, PASS_FAILED if errors else RUNNING, errors, now_ts)
    return {"state": PASS_FAILED if errors else RUNNING, "forecast_started": started,
            "errors": errors}


def _finish(state: RunnerState, base: dict[str, Any], loop_state: str, errors: list[str],
            now_ts: float) -> None:
    """Replace the card's status with this pass's, keeping the last error seen."""
    global _STATUS
    last_error, last_error_ts = _STATUS.get("last_error"), _STATUS.get("last_error_ts")
    if errors:
        last_error, last_error_ts = errors[-1], now_ts
    _STATUS = {
        "state": loop_state,
        "last_error": last_error,
        "last_error_ts": last_error_ts,
        "errors": errors,
        "forecast_running": _running(state),
        **base,
    }


# ---------------------------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------------------------


async def _wait(stop: asyncio.Event, timeout: float,
                job: asyncio.Task[Any] | None = None) -> bool:
    """Wait up to ``timeout`` seconds, or until a running ``job`` ends; True once ``stop`` is set.

    Waking when the forecast ends lets the next pass time its retry from the failure (a
    ``DATA_RETRY_S`` wait would otherwise sit behind a whole poll). The job is only watched,
    never awaited, so its outcome stays with ``_forecast_job``.
    """
    if job is None or job.done():
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout)
        return stop.is_set()
    stopped = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({stopped, job}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopped.cancel()
    return stop.is_set()


async def run_forever(stop_event: asyncio.Event | None = None) -> None:
    """Pass after pass until ``stop_event`` is set; the running forecast is cancelled on exit."""
    stop = stop_event if stop_event is not None else asyncio.Event()
    if await _wait(stop, STARTUP_DELAY_S):
        _set_status(state=STOPPED)
        return
    failed = False
    state = RunnerState()
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            try:
                while not stop.is_set():
                    try:
                        await pass_once(state, client, time.time())
                    except Exception as exc:  # noqa: BLE001 - the loop keeps its cadence
                        log.exception("lc2004.pass_failed")
                        _set_status(state=PASS_FAILED, last_error=_error_text(
                            f"A pass failed before it finished: {type(exc).__name__}: {exc}"),
                            last_error_ts=time.time())
                    now = time.time()
                    if await _wait(stop, sleep_seconds(now, wake_times(state, now)), state.job):
                        break
            finally:
                await stop_forecast(state)
    except Exception as exc:  # noqa: BLE001
        failed = True
        log.exception("lc2004.loop_died")
        _set_status(last_error=_error_text(f"The loop stopped: {type(exc).__name__}: {exc}"),
                    last_error_ts=time.time())
    finally:
        _set_status(state=STOPPED_ON_ERROR if failed else STOPPED, forecast_running=None)
