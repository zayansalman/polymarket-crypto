"""The hourly lc2004-Kronos forecast and its table (ems/lc2004_kronos_btc_24h/forecast.py, ledger.py).

No network and no model: Binance runs behind ``httpx.MockTransport`` and the Kronos client's
``run_forecast`` is replaced by a fake, except in the missing-weights test, which points the
real client at an empty Hugging Face cache so it returns before starting any worker. Pins:
the stored row on success (k, q_up, the book tops, the request's recipe and seed), the stored
row on each kind of failure (missing weights, a missing library in the worker, a client that
raises, unusable paths), one forecast per (window, candle), and the target candle and horizon
across the noon-ET boundary and both 2026 clock changes. The forecast is display-only (Zayan
(operator), 2026-09-29). Claude, 2026-09-29.
"""
from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from ems import db as _db
from ems.kronos_forecast import client as kronos_client
from ems.kronos_forecast import worker as kronos_worker
from ems.lc2004_kronos_btc_24h import forecast as fc
from ems.lc2004_kronos_btc_24h import ledger
from ems.lc2004_kronos_btc_24h.market import DayWindow, MarketDataError, window_at
from ems.lc2004_kronos_btc_24h.maths import PATHS

HOUR_MS = 3_600_000
HOUR_S = 3600


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp())


# 12:05 EDT on 2026-09-22: the window resolving on September 23 has just started.
NOW = _ts("2026-09-22T16:05:00")
WINDOW = window_at(NOW)
CANDLE = fc.target_candle_open_ms(NOW)  # the 11:00-12:00 EDT candle, closed at noon
STRIKE = 100_050.0
TOPS = {"up_bid": 0.51, "up_ask": 0.53, "down_bid": 0.47, "down_ask": 0.49}


# --- fakes ------------------------------------------------------------------------------------


def _kline(open_ms: int) -> list[Any]:
    px = 100_000.0 + open_ms / HOUR_MS % 97
    return [open_ms, str(px - 5), str(px + 20), str(px - 30), str(px), "12.5",
            open_ms + HOUR_MS - 1, str(px * 12.5), 1000, "6.0", str(px * 6.0), "0"]


class Binance:
    """513 hourly rows ending with the hour forming at ``now``: 512 closed, one forming."""

    def __init__(self, now_ts: int) -> None:
        forming = (now_ts * 1000 // HOUR_MS) * HOUR_MS
        self.rows = [_kline(forming - (512 - i) * HOUR_MS) for i in range(513)]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.path == "/api/v3/klines"
        assert request.url.params["interval"] == "1h"
        return httpx.Response(200, json=self.rows)

    @property
    def last_closed_close(self) -> float:
        return float(self.rows[-2][4])


class FakeKronos:
    """Stands in for ``ems.kronos_forecast.client.run_forecast`` and records each request."""

    def __init__(self, result: kronos_client.ForecastResult | Exception) -> None:
        self.result = result
        self.requests: list[kronos_client.ForecastRequest] = []

    async def __call__(self, request: kronos_client.ForecastRequest, **_: Any
                       ) -> kronos_client.ForecastResult:
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _ok(closes: tuple[float, ...]) -> kronos_client.ForecastResult:
    return kronos_client.ForecastResult(
        ok=True, last_close=100_000.0, final_closes=closes, seconds=412.5,
        torch_version="2.4.1")


EIGHTEEN_UP = tuple([STRIKE + 250.0 + i for i in range(18)]
                    + [STRIKE - 300.0 - i for i in range(11)] + [STRIKE])  # at-strike is not Up


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture
async def lc_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "lc2004.db")
    await _db.init_db()
    return tmp_path


def _use(monkeypatch: pytest.MonkeyPatch, fake: FakeKronos) -> None:
    monkeypatch.setattr(fc._kronos, "run_forecast", fake)


async def _run(binance: Binance, *, window: DayWindow = WINDOW, candle: int = CANDLE,
               now: int = NOW, book_tops: dict[str, Any] | None = None) -> int | None:
    async with _client(binance) as client:
        return await fc.run_and_store(client, window=window, strike=STRIKE,
                                      last_candle_open_ms=candle, now_ts=now,
                                      book_tops=book_tops)


# --- success ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_forecast_is_stored_with_k_q_up_and_the_book_tops(lc_db, monkeypatch) -> None:
    fake = FakeKronos(_ok(EIGHTEEN_UP))
    _use(monkeypatch, fake)
    binance = Binance(NOW)
    forecast_id = await _run(binance, book_tops=TOPS)
    assert isinstance(forecast_id, int)

    row = await ledger.latest_forecast(WINDOW.slug)
    assert row is not None and row["id"] == forecast_id
    assert (row["window_slug"], row["window_start_ts"], row["window_end_ts"]) == (
        "bitcoin-up-or-down-on-september-23-2026", _ts("2026-09-22T16:00:00"),
        _ts("2026-09-23T16:00:00"))
    assert row["last_candle_open_ms"] == _ts("2026-09-22T15:00:00") * 1000
    assert row["horizon_hours"] == 24
    assert (row["paths"], row["paths_above"]) == (30, 18)
    assert row["q_up"] == pytest.approx(19 / 32)
    assert row["p_raw"] == pytest.approx(0.6)
    assert row["sampling_se"] == pytest.approx(math.sqrt(0.6 * 0.4 / 30))
    assert row["strike"] == STRIKE
    assert row["last_close"] == binance.last_closed_close
    assert row["final_closes"] == [round(c, 2) for c in EIGHTEEN_UP]
    assert (row["seconds"], row["torch_version"], row["error"]) == (412.5, "2.4.1", None)
    assert {k: row[k] for k in TOPS} == TOPS
    assert row["created_ts"] > 0


@pytest.mark.asyncio
async def test_the_request_is_the_preregistered_recipe(lc_db, monkeypatch) -> None:
    fake = FakeKronos(_ok(EIGHTEEN_UP))
    _use(monkeypatch, fake)
    binance = Binance(NOW)
    await _run(binance)
    assert binance.requests[0].url.params["limit"] == "513"
    [req] = fake.requests
    assert (req.horizon, req.paths, req.temperature, req.top_p, req.top_k) == (
        24, PATHS, 1.0, 0.9, 0)
    assert (req.max_context, req.threads, req.chunk) == (512, 4, 15)
    assert req.seed == CANDLE // HOUR_MS  # the candle's hour number: reruns draw the same paths
    assert len(req.candles) == 512 and all(len(r) == 7 for r in req.candles)
    assert req.candles[-1][0] == float(CANDLE)
    assert req.candles[-1][4] == binance.last_closed_close
    assert req.candles[0][0] == float(CANDLE - 511 * HOUR_MS)


@pytest.mark.asyncio
async def test_missing_or_partial_book_tops_are_stored_as_empty(lc_db, monkeypatch) -> None:
    _use(monkeypatch, FakeKronos(_ok(EIGHTEEN_UP)))
    await _run(Binance(NOW), book_tops={"up_bid": 0.5, "up_ask": None, "down_bid": "nan"})
    row = await ledger.latest_forecast(WINDOW.slug)
    assert row is not None
    assert (row["up_bid"], row["up_ask"], row["down_bid"], row["down_ask"]) == (
        0.5, None, None, None)

    later = NOW + HOUR_S
    _use(monkeypatch, FakeKronos(_ok(EIGHTEEN_UP)))
    await _run(Binance(later), candle=fc.target_candle_open_ms(later), now=later)
    row = await ledger.latest_forecast(WINDOW.slug)
    assert row is not None and row["horizon_hours"] == 23
    assert all(row[k] is None for k in TOPS)


@pytest.mark.asyncio
async def test_an_unknown_book_top_key_is_refused_before_any_request(lc_db, monkeypatch) -> None:
    fake = FakeKronos(_ok(EIGHTEEN_UP))
    _use(monkeypatch, fake)
    binance = Binance(NOW)
    with pytest.raises(ValueError, match="book_tops"):
        await _run(binance, book_tops={"up_mid": 0.52})
    assert binance.requests == [] and fake.requests == []


# --- failures ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_weights_are_stored_as_a_readable_error(lc_db, monkeypatch,
                                                              tmp_path) -> None:
    # The REAL client, pointed at an empty cache: it answers before starting any worker.
    empty = tmp_path / "empty_hf_cache"
    empty.mkdir()
    monkeypatch.setenv("HF_HUB_CACHE", str(empty))
    assert kronos_client.missing_weights() is not None
    forecast_id = await _run(Binance(NOW), book_tops=TOPS)
    assert isinstance(forecast_id, int)
    row = await ledger.latest_forecast(WINDOW.slug)
    assert row is not None
    assert "Kronos weights for lc2004/kronos_base_model_BTCUSDT_1h_finetune" in row["error"]
    assert "fetch_lc2004_kronos_weights.py" in row["error"]
    assert all(row[k] is None for k in ("paths_above", "p_raw", "q_up", "sampling_se",
                                        "final_closes", "torch_version"))
    assert (row["paths"], row["horizon_hours"], row["strike"]) == (PATHS, 24, STRIKE)
    assert {k: row[k] for k in TOPS} == TOPS


@pytest.mark.asyncio
async def test_a_missing_library_in_the_worker_is_stored_as_a_readable_error(
        lc_db, monkeypatch) -> None:
    error = kronos_worker.missing_library_error(
        ImportError("No module named 'einops'", name="einops"))
    _use(monkeypatch, FakeKronos(kronos_client.ForecastResult(ok=False, error=error,
                                                              seconds=1.5)))
    await _run(Binance(NOW))
    row = await ledger.latest_forecast(WINDOW.slug)
    assert row is not None
    assert row["error"] == error and "missing einops" in row["error"]
    assert (row["q_up"], row["paths_above"], row["seconds"]) == (None, None, 1.5)


@pytest.mark.asyncio
async def test_a_failure_without_text_still_says_what_failed(lc_db, monkeypatch) -> None:
    _use(monkeypatch, FakeKronos(kronos_client.ForecastResult(ok=False)))
    await _run(Binance(NOW))
    row = await ledger.latest_forecast(WINDOW.slug)
    assert row is not None and "failed without an error message" in row["error"]


@pytest.mark.asyncio
async def test_a_client_that_raises_is_stored_not_swallowed(lc_db, monkeypatch) -> None:
    _use(monkeypatch, FakeKronos(RuntimeError("pipe closed")))
    forecast_id = await _run(Binance(NOW))
    assert isinstance(forecast_id, int)
    row = await ledger.latest_forecast(WINDOW.slug)
    assert row is not None
    assert row["error"] == "the Kronos client raised RuntimeError: pipe closed"
    assert row["q_up"] is None


@pytest.mark.asyncio
async def test_paths_that_cannot_be_scored_are_stored_as_an_error(lc_db, monkeypatch) -> None:
    _use(monkeypatch, FakeKronos(_ok(())))
    await _run(Binance(NOW))
    row = await ledger.latest_forecast(WINDOW.slug)
    assert row is not None
    assert row["error"].startswith("the Kronos worker returned paths that cannot be scored")
    assert (row["q_up"], row["final_closes"], row["torch_version"]) == (None, None, "2.4.1")


@pytest.mark.asyncio
async def test_stale_candles_store_nothing_and_raise(lc_db, monkeypatch) -> None:
    fake = FakeKronos(_ok(EIGHTEEN_UP))
    _use(monkeypatch, fake)
    stale = Binance(NOW - 2 * HOUR_S)  # Binance data stops two hours back
    with pytest.raises(MarketDataError):
        await _run(stale)
    assert fake.requests == []
    assert await ledger.latest_forecast(WINDOW.slug) is None


@pytest.mark.asyncio
async def test_the_hour_turning_during_the_start_stores_nothing(lc_db, monkeypatch) -> None:
    fake = FakeKronos(_ok(EIGHTEEN_UP))
    _use(monkeypatch, fake)
    with pytest.raises(MarketDataError, match="the hour turned"):
        await _run(Binance(NOW), candle=CANDLE - HOUR_MS)
    assert fake.requests == []
    assert await ledger.latest_forecast(WINDOW.slug) is None


# --- a row that cannot be stored after the model ran -----------------------------------------


@pytest.mark.asyncio
async def test_a_row_that_cannot_be_stored_says_what_the_model_said(lc_db, monkeypatch) -> None:
    """The runner must not rerun the model for it (review finding, Claude, 2026-09-29)."""
    fake = FakeKronos(_ok(EIGHTEEN_UP))
    _use(monkeypatch, fake)

    async def disk_full(**row: Any) -> int | None:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(fc._ledger, "insert_forecast", disk_full)
    with pytest.raises(fc.ForecastNotStoredError) as caught:
        await _run(Binance(NOW))
    assert str(caught.value) == (
        "The model ran (Up 59%, 18 of 30 paths end above the strike) but the forecast could "
        "not be stored: OperationalError: database or disk is full")
    assert len(fake.requests) == 1

    _use(monkeypatch, FakeKronos(kronos_client.ForecastResult(ok=False, error="timed out")))
    with pytest.raises(fc.ForecastNotStoredError, match=r"^The model ran \(and failed\) but"):
        await _run(Binance(NOW))


# The table as the 2026-09-22 branch created it (before torch_version and the book tops).
OLD_TABLE = """
CREATE TABLE lc2004_forecasts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, window_slug TEXT NOT NULL,
  window_start_ts INTEGER NOT NULL, window_end_ts INTEGER NOT NULL,
  last_candle_open_ms INTEGER NOT NULL, horizon_hours INTEGER NOT NULL, strike REAL,
  last_close REAL, paths INTEGER NOT NULL, paths_above INTEGER, p_raw REAL, q_up REAL,
  sampling_se REAL, final_closes TEXT, seconds REAL, error TEXT, created_ts INTEGER NOT NULL,
  UNIQUE(window_slug, last_candle_open_ms))
"""
OLD_COLUMNS = {"window_slug", "window_start_ts", "window_end_ts", "last_candle_open_ms",
               "horizon_hours", "strike", "last_close", "paths", "paths_above", "p_raw",
               "q_up", "sampling_se", "final_closes", "seconds", "error", "created_ts"}


def test_the_migration_adds_every_column_the_old_table_lacks() -> None:
    assert set(_db.LC2004_FORECAST_COLUMN_MIGRATIONS) == set(ledger.FORECAST_COLS) - OLD_COLUMNS


@pytest.mark.asyncio
async def test_an_older_forecast_table_is_migrated_so_rows_can_be_stored(
        tmp_path: Path, monkeypatch) -> None:
    """Review finding (Claude, 2026-09-29): an older table made every insert fail."""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(OLD_TABLE)
    monkeypatch.setattr(_db, "DB_PATH", path)
    await _db.init_db()
    await _db.init_db()  # idempotent
    _use(monkeypatch, FakeKronos(_ok(EIGHTEEN_UP)))
    assert isinstance(await _run(Binance(NOW), book_tops=TOPS), int)
    row = await ledger.latest_forecast(WINDOW.slug)
    assert row is not None and row["torch_version"] == "2.4.1"
    assert {k: row[k] for k in TOPS} == TOPS


# --- once per (window, candle) ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_hour_is_forecast_once(lc_db, monkeypatch) -> None:
    fake = FakeKronos(_ok(EIGHTEEN_UP))
    _use(monkeypatch, fake)
    binance = Binance(NOW)
    first = await _run(binance)
    assert isinstance(first, int)
    assert await ledger.forecast_exists(WINDOW.slug, CANDLE)
    assert await _run(binance) is None
    assert len(fake.requests) == 1 and len(binance.requests) == 1  # no refetch, no second run
    assert len(await ledger.window_forecasts(WINDOW.slug)) == 1


@pytest.mark.asyncio
async def test_a_failed_hour_is_not_retried_in_the_same_hour(lc_db, monkeypatch) -> None:
    fake = FakeKronos(kronos_client.ForecastResult(ok=False, error="timed out"))
    _use(monkeypatch, fake)
    await _run(Binance(NOW))
    assert await _run(Binance(NOW)) is None
    assert len(fake.requests) == 1


@pytest.mark.asyncio
async def test_nothing_to_forecast_once_the_window_has_ended(lc_db, monkeypatch) -> None:
    fake = FakeKronos(_ok(EIGHTEEN_UP))
    _use(monkeypatch, fake)
    binance = Binance(NOW)
    ended = DayWindow(slug="bitcoin-up-or-down-on-september-22-2026",
                      start_ts=_ts("2026-09-21T16:00:00"), end_ts=_ts("2026-09-22T16:00:00"))
    assert fc.horizon_hours(ended.end_ts, CANDLE) == 0
    assert await _run(binance, window=ended) is None
    assert binance.requests == [] and fake.requests == []


# --- the ledger's reads -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_window_history_is_oldest_first_and_latest_is_the_newest_candle(lc_db) -> None:
    for i, hour in enumerate((3, 1, 2)):  # stored out of order on purpose
        await ledger.insert_forecast(
            window_slug=WINDOW.slug, window_start_ts=WINDOW.start_ts,
            window_end_ts=WINDOW.end_ts, last_candle_open_ms=CANDLE + hour * HOUR_MS,
            horizon_hours=24 - hour, strike=STRIKE + i, paths=PATHS, q_up=hour / 32,
            final_closes=[1.234, 2.0], created_ts=1_000 + i)
    await ledger.insert_forecast(
        window_slug="other", window_start_ts=0, window_end_ts=1, last_candle_open_ms=0,
        horizon_hours=1, paths=PATHS, error="x", created_ts=2_000)

    history = await ledger.window_forecasts(WINDOW.slug)
    assert [r["horizon_hours"] for r in history] == [23, 22, 21]
    assert history[0]["final_closes"] == [1.23, 2.0]
    latest = await ledger.latest_forecast(WINDOW.slug)
    assert latest is not None and latest["horizon_hours"] == 21
    other = await ledger.latest_forecast("other")
    assert other is not None and other["final_closes"] is None  # a failed run has no paths
    assert await ledger.latest_forecast("nowhere") is None


@pytest.mark.asyncio
async def test_a_duplicate_row_is_ignored_and_unknown_fields_are_refused(lc_db) -> None:
    row = dict(window_slug=WINDOW.slug, window_start_ts=WINDOW.start_ts,
               window_end_ts=WINDOW.end_ts, last_candle_open_ms=CANDLE, horizon_hours=24,
               paths=PATHS, created_ts=1)
    assert isinstance(await ledger.insert_forecast(**row), int)
    assert await ledger.insert_forecast(**row) is None
    with pytest.raises(ValueError, match="unknown forecast fields"):
        await ledger.insert_forecast(**row, kelly_size=5.0)


# --- target candle and horizon across noon ET and the clock changes ---------------------------


@pytest.mark.parametrize(
    ("now_iso", "slug", "candle_iso", "horizon"),
    [
        # A normal day (EDT, noon = 16:00Z).
        ("2026-09-22T15:05:00", "bitcoin-up-or-down-on-september-22-2026",
         "2026-09-22T14:00:00", 1),
        ("2026-09-22T15:59:59", "bitcoin-up-or-down-on-september-22-2026",
         "2026-09-22T14:00:00", 1),
        ("2026-09-22T16:00:00", "bitcoin-up-or-down-on-september-23-2026",
         "2026-09-22T15:00:00", 24),
        ("2026-09-22T16:05:00", "bitcoin-up-or-down-on-september-23-2026",
         "2026-09-22T15:00:00", 24),
        # Fall-back window: noon EDT Oct 31 (16:00Z) to noon EST Nov 1 (17:00Z), 25 hours.
        ("2026-10-31T16:05:00", "bitcoin-up-or-down-on-november-1-2026",
         "2026-10-31T15:00:00", 25),
        ("2026-11-01T06:30:00", "bitcoin-up-or-down-on-november-1-2026",
         "2026-11-01T05:00:00", 11),
        ("2026-11-01T16:05:00", "bitcoin-up-or-down-on-november-1-2026",
         "2026-11-01T15:00:00", 1),
        ("2026-11-01T17:05:00", "bitcoin-up-or-down-on-november-2-2026",
         "2026-11-01T16:00:00", 24),
        # Spring-forward window: noon EST Mar 7 (17:00Z) to noon EDT Mar 8 (16:00Z), 23 hours.
        ("2026-03-07T17:05:00", "bitcoin-up-or-down-on-march-8-2026",
         "2026-03-07T16:00:00", 23),
        ("2026-03-08T15:05:00", "bitcoin-up-or-down-on-march-8-2026",
         "2026-03-08T14:00:00", 1),
        ("2026-03-08T16:05:00", "bitcoin-up-or-down-on-march-9-2026",
         "2026-03-08T15:00:00", 24),
    ],
)
def test_target_candle_and_horizon(now_iso: str, slug: str, candle_iso: str,
                                   horizon: int) -> None:
    now = _ts(now_iso)
    window = window_at(now)
    candle = fc.target_candle_open_ms(now)
    assert window.slug == slug
    assert candle == _ts(candle_iso) * 1000
    assert candle + HOUR_MS <= now * 1000  # the candle has closed
    assert fc.horizon_hours(window.end_ts, candle) == horizon


@pytest.mark.parametrize(("start_iso", "hours"), [
    ("2026-09-22T16:00:00", 24), ("2026-10-31T16:00:00", 25), ("2026-03-07T17:00:00", 23),
])
def test_every_hour_of_a_window_counts_down_to_one(start_iso: str, hours: int) -> None:
    start = _ts(start_iso)
    window = window_at(start)
    assert window.end_ts - window.start_ts == hours * HOUR_S
    seen = []
    for h in range(hours):
        now = start + h * HOUR_S + 90  # 90 s past each hour, once the candle has closed
        assert window_at(now) == window
        seen.append(fc.horizon_hours(window.end_ts, fc.target_candle_open_ms(now)))
    assert seen == list(range(hours, 0, -1))


def test_the_seed_is_the_candle_hour() -> None:
    assert fc.seed_for(CANDLE) == _ts("2026-09-22T15:00:00") // HOUR_S
    assert fc.seed_for(CANDLE + HOUR_MS) == fc.seed_for(CANDLE) + 1
