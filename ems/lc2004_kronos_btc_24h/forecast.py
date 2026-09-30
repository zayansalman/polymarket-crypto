"""lc2004-Kronos BTC 24h forecast: one hourly Kronos run, scored against the window's strike.

Once the 1h candle that ends at hour H has closed, the model gets the 512 closed Binance BTCUSDT
1h candles up to H and samples ``PATHS`` paths of the next K hourly closes, where K is the number
of whole hours from H to the window's end (24 at noon ET, 1 at 11:00 ET, 23 or 25 across a clock
change). k paths finish strictly above the strike, and q_up = (k + 1) / (PATHS + 2)
(``maths.probability_up``). The row is stored whether the run worked or not: a failed run keeps
its error text (missing weights, a missing library in the worker's Python, a timeout) so the
dashboard can say why there is no forecast.

The sampling recipe is the research pre-registration's
(research/kronos_lc2004_btcusdt_1h_finetune_24h_horizon/PREREG.md, Claude, 2026-09-17): T 1.0,
top_p 0.9, top_k 0, 30 independent paths in chunks of 15, 4 CPU threads, 512 candles of context.
The seed is the last closed candle's hour number since 1970, so a rerun of the same hour draws
the same paths. The model runs in an isolated worker process (``ems.kronos_forecast.client``);
this process never imports torch.

Display-only: nothing here places, cancels or simulates an order. Zayan (operator), 2026-09-29:
the forecast is shown on the dashboard and he trades by hand.

Claude, 2026-09-22, for docs/superpowers/specs/2026-09-22-lc2004-kronos-btc-24h-design.md
("The maths", steps 1-3). Moved onto ``ems`` with the book tops at forecast time by Claude,
2026-09-29.
"""
from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any

import httpx

from ems.kronos_forecast import client as _kronos
from ems.lc2004_kronos_btc_24h import ledger as _ledger
from ems.lc2004_kronos_btc_24h import market as _market
from ems.lc2004_kronos_btc_24h.maths import PATHS, probability_up
from ems.logging_setup import get_logger

log = get_logger("lc2004_kronos_btc_24h.forecast")

# PREREG.md sampling settings (Claude, 2026-09-17; lc2004 fine-tune author's forecast script).
TEMPERATURE = 1.0
TOP_P = 0.9
TOP_K = 0
MAX_CONTEXT = 512
CHUNK = 15
THREADS = 4
HOUR_S = 3600
HOUR_MS = 3_600_000
BOOK_TOP_KEYS = ("up_bid", "up_ask", "down_bid", "down_ask")


def target_candle_open_ms(now_ts: float) -> int:
    """Open time (ms) of the last 1h candle that has closed at ``now_ts``."""
    return (int(now_ts) // HOUR_S) * HOUR_S * 1000 - HOUR_MS


def horizon_hours(window_end_ts: int, last_candle_open_ms: int) -> int:
    """Whole hourly steps from the last closed candle's close to the window's end."""
    return (int(window_end_ts) - (int(last_candle_open_ms) // 1000 + HOUR_S)) // HOUR_S


def seed_for(last_candle_open_ms: int) -> int:
    """The sampling seed: the last closed candle's hour number since 1970."""
    return int(last_candle_open_ms) // HOUR_MS


def build_request(candles: list[_market.Candle], *, horizon: int,
                  seed: int) -> _kronos.ForecastRequest:
    """The worker's request: each candle as [open_ms, o, h, l, c, volume, quote volume]."""
    rows = [[float(c.open_time_ms), c.open, c.high, c.low, c.close, c.volume, c.quote_volume]
            for c in candles]
    return _kronos.ForecastRequest(
        candles=rows, horizon=int(horizon), paths=PATHS, temperature=TEMPERATURE,
        top_p=TOP_P, top_k=TOP_K, seed=int(seed), max_context=MAX_CONTEXT, threads=THREADS,
        chunk=CHUNK,
    )


def _book_tops(book_tops: Mapping[str, Any] | None) -> dict[str, float | None]:
    """The four best prices to store, each a finite float or None. Unknown keys are refused."""
    if book_tops is None:
        return {}
    unknown = set(book_tops) - set(BOOK_TOP_KEYS)
    if unknown:
        raise ValueError(f"unknown book_tops keys: {sorted(unknown)}")
    out: dict[str, float | None] = {}
    for key in BOOK_TOP_KEYS:
        value = book_tops.get(key)
        try:
            price = float(value) if value is not None else None
        except (TypeError, ValueError):
            price = None
        out[key] = price if price is not None and math.isfinite(price) else None
    return out


class ForecastNotStoredError(Exception):
    """The model ran but its row could not be stored (a full disk, a locked database).

    The runner does not run the model again for that hour: a run takes minutes of CPU and
    about a gigabyte of memory. The message carries what the model said, so the card still
    shows it (Claude, 2026-09-29, for a review finding).
    """


async def _run(request: _kronos.ForecastRequest) -> _kronos.ForecastResult:
    """The client's result. It reports worker failures itself; anything it raises is one too."""
    try:
        return await _kronos.run_forecast(request)
    except Exception as exc:  # stored and shown on the card, never swallowed
        log.exception("lc2004.forecast_client_raised")
        return _kronos.ForecastResult(
            ok=False, error=f"the Kronos client raised {type(exc).__name__}: {exc}")


async def run_and_store(client: httpx.AsyncClient, *, window: _market.DayWindow, strike: float,
                        last_candle_open_ms: int, now_ts: float,
                        book_tops: Mapping[str, Any] | None = None) -> int | None:
    """Fetch the candles, run the model, score the paths against ``strike`` and store the row.

    ``book_tops`` is the market's best prices at forecast time (keys ``up_bid``, ``up_ask``,
    ``down_bid``, ``down_ask``; any may be None), stored with the row when given.

    Returns the forecast id, or None when there is nothing to forecast (K < 1) or this window
    already has a forecast from this candle. Missing or stale candles raise
    ``market.MarketDataError`` and store nothing, so the caller retries on a later pass. A model
    failure is stored with its error text. A row that cannot be stored after the model has run
    raises ``ForecastNotStoredError``.
    """
    tops = _book_tops(book_tops)
    horizon = horizon_hours(window.end_ts, last_candle_open_ms)
    if horizon < 1:
        return None
    if await _ledger.forecast_exists(window.slug, last_candle_open_ms):
        return None
    candles = await _market.fetch_closed_1h_candles(
        client, now_ms=int(now_ts * 1000), count=MAX_CONTEXT)
    if candles[-1].open_time_ms != int(last_candle_open_ms):
        raise _market.MarketDataError(
            f"the last closed 1h candle opens at {candles[-1].open_time_ms}, "
            f"not {last_candle_open_ms}: the hour turned while the forecast was starting")
    request = build_request(candles, horizon=horizon, seed=seed_for(last_candle_open_ms))
    log.info("lc2004.forecast_started", window=window.slug, horizon=horizon,
             candle_open_ms=last_candle_open_ms)
    result = await _run(request)
    row: dict[str, Any] = {
        "window_slug": window.slug,
        "window_start_ts": window.start_ts,
        "window_end_ts": window.end_ts,
        "last_candle_open_ms": int(last_candle_open_ms),
        "horizon_hours": horizon,
        "strike": float(strike),
        "last_close": candles[-1].close,
        "paths": PATHS,
        "seconds": result.seconds,
        "torch_version": result.torch_version,
        "created_ts": int(time.time()),
        **tops,
    }
    error = None if result.ok else (
        result.error or "the Kronos worker failed without an error message")
    if error is None:
        try:
            prob = probability_up(result.final_closes, strike)
        except ValueError as exc:
            error = f"the Kronos worker returned paths that cannot be scored: {exc}"
        else:
            row.update(paths=prob.n, paths_above=prob.k, p_raw=prob.p_raw, q_up=prob.q_up,
                       sampling_se=prob.sampling_se, final_closes=list(result.final_closes))
            log.info("lc2004.forecast_done", window=window.slug, horizon=horizon, k=prob.k,
                     n=prob.n, q_up=round(prob.q_up, 4), seconds=result.seconds)
    if error is not None:
        row["error"] = error
        log.warning("lc2004.forecast_failed", window=window.slug, horizon=horizon, error=error)
    try:
        return await _ledger.insert_forecast(**row)
    except Exception as exc:
        log.exception("lc2004.forecast_not_stored", window=window.slug,
                      candle_open_ms=last_candle_open_ms, q_up=row.get("q_up"),
                      paths_above=row.get("paths_above"), error=error)
        raise ForecastNotStoredError(
            f"The model ran ({_what_it_said(row, error)}) but the forecast could not be "
            f"stored: {type(exc).__name__}: {exc}") from exc


def _what_it_said(row: Mapping[str, Any], error: str | None) -> str:
    if error is not None:
        return "and failed"
    return (f"Up {100 * row['q_up']:.0f}%, {row['paths_above']} of {row['paths']} paths end "
            "above the strike")
