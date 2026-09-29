"""The lc2004-Kronos BTC 24h forecast loop (``ems/lc2004_kronos_btc_24h/runner.py``).

No network, no model: Gamma discovery, the strike, the books, the weights check and
``forecast.run_and_store`` are fakes, the HTTP client refuses every request, and the database
is a temporary file. Pins: one background forecast per (window, candle), never awaited inside
a pass; switched off in Settings starts nothing and cancels a running one; waiting for the
noon candle; the books read into the status; errors in the status, and a failed market
lookup or strike read shown as an error rather than a wait; retries timed from the failure,
and never a second model run for one hour; the loop waking for a retry; its startup delay
and shutdown. Display-only: nothing here places an order (Zayan (operator), 2026-09-29).
Claude, 2026-09-29.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from ems import db as _db
from ems import runtime_knobs as _knobs
from ems.lc2004_kronos_btc_24h import forecast as fc
from ems.lc2004_kronos_btc_24h import ledger
from ems.lc2004_kronos_btc_24h import runner as rn
from ems.lc2004_kronos_btc_24h.market import BtcDailyMarket, Book, MarketDataError, window_at

def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp())


# 12:05 EDT on 2026-09-22: the window resolving on September 23 has just started, and the
# noon 1m candle has closed.
NOW = _ts("2026-09-22T16:05:00")
WINDOW = window_at(NOW)
CANDLE = fc.target_candle_open_ms(NOW)
STRIKE = 100_050.0
MARKET = BtcDailyMarket(
    slug=WINDOW.slug, condition_id="0xcid", question="Bitcoin Up or Down on September 23?",
    start_ts=WINDOW.start_ts, end_ts=WINDOW.end_ts, up_token="UP", down_token="DOWN")
BOOKS = {"UP": Book(bids=((0.51, 10.0),), asks=((0.53, 12.0),)),
         "DOWN": Book(bids=((0.46, 8.0),), asks=((0.48, 9.0),))}


class Fakes:
    """Every outside call the loop makes, recorded."""

    def __init__(self) -> None:
        self.market: BtcDailyMarket | None | Exception = MARKET
        self.strike: float | None | Exception = STRIKE
        self.books: dict[str, Book | None] = dict(BOOKS)
        self.missing: str | None = None
        self.discovered = 0
        self.strike_reads = 0
        self.book_reads: list[str] = []
        self.runs: list[dict[str, Any]] = []
        self.gate = asyncio.Event()  # a run finishes only once this is set
        self.started = asyncio.Event()
        self.cancelled = 0
        self.raise_: Exception | None = None
        self.store = True
        # How long each run takes on the job's clock (the model takes minutes).
        self.run_seconds = 0.0
        self.clock = 0.0

    def monotonic(self) -> float:
        return self.clock

    async def discover(self, client, window):  # noqa: ANN001
        self.discovered += 1
        if isinstance(self.market, Exception):
            raise self.market
        return self.market

    async def fetch_minute_close(self, client, minute_ts, now_ts):  # noqa: ANN001
        self.strike_reads += 1
        if isinstance(self.strike, Exception):
            raise self.strike
        return self.strike

    async def fetch_book(self, client, token_id):  # noqa: ANN001
        self.book_reads.append(token_id)
        return self.books.get(token_id)

    def missing_weights(self) -> str | None:
        return self.missing

    async def run_and_store(self, client, *, window, strike, last_candle_open_ms, now_ts,  # noqa: ANN001
                            book_tops=None):
        self.runs.append({"window": window, "strike": strike, "candle": last_candle_open_ms,
                          "now": now_ts, "book_tops": book_tops})
        self.started.set()
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        self.clock += self.run_seconds
        if self.raise_ is not None:
            raise self.raise_
        if not self.store:
            return None
        return await ledger.insert_forecast(
            window_slug=window.slug, window_start_ts=window.start_ts,
            window_end_ts=window.end_ts, last_candle_open_ms=last_candle_open_ms,
            horizon_hours=fc.horizon_hours(window.end_ts, last_candle_open_ms), strike=strike,
            last_close=100_000.0, paths=30, paths_above=24, p_raw=0.8, q_up=25 / 32,
            sampling_se=0.073, final_closes=[100_100.0] * 30, seconds=300.0,
            created_ts=int(now_ts))


@pytest_asyncio.fixture
async def lc_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "lc2004_runner.db")
    await _db.init_db()
    for name, knob in _knobs.KNOBS.items():
        _knobs._cache[name] = knob.default
    return tmp_path


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Fakes:
    f = Fakes()
    monkeypatch.setattr(rn._market, "discover", f.discover)
    monkeypatch.setattr(rn._market, "fetch_minute_close", f.fetch_minute_close)
    monkeypatch.setattr(rn._market, "fetch_book", f.fetch_book)
    monkeypatch.setattr(rn._kronos, "missing_weights", f.missing_weights)
    monkeypatch.setattr(rn._forecast, "run_and_store", f.run_and_store)
    monkeypatch.setattr(rn, "_monotonic", f.monotonic)
    return f


def _no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"no request may leave a test: {request.url}")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(_no_network))


async def _settle(state: rn.RunnerState) -> None:
    """Let a finished job's task complete."""
    if state.job is not None:
        await asyncio.wait({state.job}, timeout=2)


# --- the forecast ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pass_starts_one_background_forecast_when_due_and_not_again(lc_db,
                                                                             fakes) -> None:
    state = rn.RunnerState()
    async with _client() as client:
        report = await rn.pass_once(state, client, NOW)
        assert report == {"state": rn.RUNNING, "forecast_started": True, "errors": []}
        await asyncio.wait_for(fakes.started.wait(), 1)
        # The pass returned while the model runs: it is a background task.
        assert state.running() and len(fakes.runs) == 1
        run = fakes.runs[0]
        assert (run["window"], run["strike"], run["candle"], run["now"]) == (
            WINDOW, STRIKE, CANDLE, NOW)
        assert run["book_tops"] == {"up_bid": 0.51, "up_ask": 0.53,
                                    "down_bid": 0.46, "down_ask": 0.48}
        status = rn.status()
        assert status["forecast_running"] == {"window": WINDOW.slug, "candle_open_ms": CANDLE,
                                              "started_ts": NOW}

        # While it runs, a later pass starts nothing more.
        assert (await rn.pass_once(state, client, NOW + 60))["forecast_started"] is False
        assert len(fakes.runs) == 1

        # Once it has stored its row, the same hour is not forecast again.
        fakes.gate.set()
        await _settle(state)
        assert rn.status()["forecast_running"] is None
        assert (await rn.pass_once(state, client, NOW + 120))["forecast_started"] is False
        assert len(fakes.runs) == 1
        assert (await ledger.latest_forecast(WINDOW.slug))["q_up"] == pytest.approx(25 / 32)

        # The next hour's candle is forecast after the hourly close.
        report = await rn.pass_once(state, client, NOW + 3600)
        assert report["forecast_started"] is True
        await _settle(state)
        assert fakes.runs[-1]["candle"] == CANDLE + 3_600_000


@pytest.mark.asyncio
async def test_switched_off_starts_nothing_and_cancels_a_running_forecast(lc_db,
                                                                          fakes) -> None:
    state = rn.RunnerState()
    async with _client() as client:
        await rn.pass_once(state, client, NOW)
        await asyncio.wait_for(fakes.started.wait(), 1)
        job = state.job
        assert job is not None and not job.done()
        looked_up = (fakes.discovered, fakes.strike_reads, len(fakes.book_reads))

        await _knobs.set(rn.KNOB, False)
        report = await rn.pass_once(state, client, NOW + 60)
        assert report == {"state": rn.SWITCHED_OFF, "forecast_started": False, "errors": []}
        assert job.cancelled() and fakes.cancelled == 1
        assert state.job is None
        # Nothing else happens while it is off: no market, strike or book reads.
        assert (fakes.discovered, fakes.strike_reads, len(fakes.book_reads)) == looked_up
        status = rn.status()
        assert status["state"] == rn.SWITCHED_OFF and status["forecast_running"] is None

        await rn.pass_once(state, client, NOW + 120)
        assert len(fakes.runs) == 1  # still off: nothing new

        await _knobs.set(rn.KNOB, True)
        assert (await rn.pass_once(state, client, NOW + 180))["forecast_started"] is True
        fakes.gate.set()
        await _settle(state)


@pytest.mark.asyncio
async def test_switched_off_from_the_start_reads_nothing(lc_db, fakes) -> None:
    await _knobs.set(rn.KNOB, False)
    async with _client() as client:
        report = await rn.pass_once(rn.RunnerState(), client, NOW)
    assert report["state"] == rn.SWITCHED_OFF
    assert (fakes.discovered, fakes.strike_reads, fakes.book_reads, fakes.runs) == (0, 0, [], [])
    assert rn.status()["window"]["slug"] == WINDOW.slug


@pytest.mark.asyncio
async def test_waiting_for_the_noon_candle_starts_nothing(lc_db, fakes) -> None:
    fakes.strike = None
    async with _client() as client:
        report = await rn.pass_once(rn.RunnerState(), client, WINDOW.start_ts + 30)
    assert report["forecast_started"] is False and fakes.runs == []
    status = rn.status()
    assert status["strike"] is None
    assert status["waiting"] == [rn.WAITING_FOR_STRIKE]
    assert status["next_forecast_after"] == WINDOW.start_ts + 60 + rn.FORECAST_LAG_S
    # The books are still read for the card.
    assert status["books"]["up"] == {"bid": 0.51, "ask": 0.53}


@pytest.mark.asyncio
async def test_missing_weights_start_nothing_and_say_so(lc_db, fakes) -> None:
    fakes.missing = "Kronos weights for lc2004/x@eb51 are not in /c; run python3 tools/f.py"
    async with _client() as client:
        report = await rn.pass_once(rn.RunnerState(), client, NOW)
    assert report["forecast_started"] is False and fakes.runs == []
    assert rn.status()["missing_weights"] == fakes.missing
    assert rn.status()["strike"] == STRIKE


@pytest.mark.asyncio
async def test_the_books_are_read_into_the_status_every_pass(lc_db, fakes) -> None:
    state = rn.RunnerState()
    async with _client() as client:
        await rn.pass_once(state, client, NOW)
        fakes.books["DOWN"] = None  # a book that cannot be read
        await rn.pass_once(state, client, NOW + 60)
        fakes.gate.set()
        await _settle(state)
    assert fakes.book_reads == ["UP", "DOWN", "UP", "DOWN"]
    status = rn.status()
    assert status["books"] == {"ts": NOW + 60, "up": {"bid": 0.51, "ask": 0.53}, "down": None}
    assert status["market"] == {"slug": WINDOW.slug, "question": MARKET.question}
    assert status["strike"] == STRIKE and status["waiting"] == []


@pytest.mark.asyncio
async def test_the_market_is_looked_up_once_per_window_and_again_a_minute_after_a_miss(
        lc_db, fakes) -> None:
    fakes.market = None
    state = rn.RunnerState()
    async with _client() as client:
        await rn.pass_once(state, client, NOW)
        assert rn.status()["market"] is None
        assert rn.WAITING_FOR_MARKET in rn.status()["waiting"]
        await rn.pass_once(state, client, NOW + 30)
        assert fakes.discovered == 1  # not again inside the minute
        fakes.market = MARKET
        await rn.pass_once(state, client, NOW + 61)
        await rn.pass_once(state, client, NOW + 120)
        assert fakes.discovered == 2  # found, and kept for the window
        assert rn.status()["market"]["slug"] == WINDOW.slug
        fakes.gate.set()
        await _settle(state)


# --- errors ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_step_lands_in_the_status_and_the_pass_goes_on(lc_db, fakes) -> None:
    fakes.market = RuntimeError("gamma down")
    state = rn.RunnerState()
    async with _client() as client:
        report = await rn.pass_once(state, client, NOW)
        fakes.gate.set()
        await _settle(state)
    assert report["state"] == rn.PASS_FAILED
    assert report["errors"] == ["Looking up the market failed: RuntimeError: gamma down"]
    status = rn.status()
    assert status["state"] == rn.PASS_FAILED
    assert status["last_error"] == report["errors"][0] and status["last_error_ts"] == NOW
    # The strike was still read, and the forecast still started (without book tops).
    assert status["strike"] == STRIKE
    assert report["forecast_started"] is True and fakes.runs[0]["book_tops"] is None
    assert status["waiting"] == []  # a failed lookup is not "not listed yet"


@pytest.mark.asyncio
async def test_a_failed_market_lookup_is_an_error_not_waiting(lc_db, fakes) -> None:
    """Review finding (Claude, 2026-09-29): a Gamma 403 read as "not listed yet" all day."""
    fakes.market = MarketDataError("Gamma: the slug lookup failed (HTTP 403); the series "
                                   "lookup failed (HTTP 403)")
    state = rn.RunnerState()
    async with _client() as client:
        report = await rn.pass_once(state, client, NOW)
        assert report["errors"] == [
            "Looking up the market failed: MarketDataError: Gamma: the slug lookup failed "
            "(HTTP 403); the series lookup failed (HTTP 403)"]
        assert rn.WAITING_FOR_MARKET not in rn.status()["waiting"]
        await rn.pass_once(state, client, NOW + 60)
        assert fakes.discovered == 2  # tried again on the next pass
        fakes.gate.set()
        await _settle(state)


@pytest.mark.asyncio
async def test_a_failed_strike_read_is_an_error_not_waiting(lc_db, fakes) -> None:
    """Review finding (Claude, 2026-09-29): a failed read looked like waiting for noon."""
    fakes.strike = MarketDataError("binance returned the 1m candle opening at "
                                   f"{(WINDOW.start_ts + 60) * 1000}, not "
                                   f"{WINDOW.start_ts * 1000}")
    at_three = WINDOW.start_ts + 3 * 3600 + 300  # 15:05 ET
    state = rn.RunnerState()
    async with _client() as client:
        report = await rn.pass_once(state, client, at_three)
        assert report["state"] == rn.PASS_FAILED and report["forecast_started"] is False
        assert report["errors"][0].startswith(
            "Reading the strike failed: MarketDataError: binance returned the 1m candle")
        status = rn.status()
        assert status["waiting"] == [] and status["strike"] is None
        assert status["strike_failed"] is True
        assert status["next_forecast_after"] is None  # never a time already gone
        # Read again next pass; once it works the forecast starts.
        fakes.strike = STRIKE
        report = await rn.pass_once(state, client, at_three + 60)
        assert report["forecast_started"] is True and rn.status()["strike_failed"] is False
        fakes.gate.set()
        await _settle(state)


@pytest.mark.asyncio
async def test_a_forecast_that_raises_is_shown_and_tried_again_a_minute_after_it_failed(
        lc_db, fakes) -> None:
    fakes.raise_ = RuntimeError("database is locked")
    fakes.run_seconds = 30.0  # it failed 30 s after the pass that started it
    fakes.gate.set()
    state = rn.RunnerState()
    async with _client() as client:
        await rn.pass_once(state, client, NOW)
        await _settle(state)
        status = rn.status()
        assert "The forecast run failed: RuntimeError: database is locked" in status["last_error"]
        assert status["last_error_ts"] == NOW + 30
        assert state.retry_at == {(WINDOW.slug, CANDLE): NOW + 30 + rn.POLL_S}
        await rn.pass_once(state, client, NOW + 60)
        assert len(fakes.runs) == 1  # inside the retry wait, timed from the failure
        assert "database is locked" in rn.status()["last_error"]  # still shown on the next pass
        fakes.raise_ = None
        await rn.pass_once(state, client, NOW + 30 + rn.POLL_S)
        await _settle(state)
        assert len(fakes.runs) == 2


@pytest.mark.asyncio
async def test_a_forecast_that_ran_but_was_not_stored_is_not_run_again_that_hour(
        lc_db, fakes) -> None:
    """Review finding (Claude, 2026-09-29): a retry timed from the pass that started a
    3-minute run was already past, so every pass reran the model all hour."""
    fakes.raise_ = fc.ForecastNotStoredError(
        "The model ran (Up 78%, 24 of 30 paths end above the strike) but the forecast could "
        "not be stored: OperationalError: database or disk is full")
    fakes.run_seconds = 180.0
    fakes.gate.set()
    state = rn.RunnerState()
    async with _client() as client:
        await rn.pass_once(state, client, NOW)
        await _settle(state)
        status = rn.status()
        assert status["last_error"].startswith("The model ran (Up 78%, 24 of 30 paths")
        assert status["last_error_ts"] == NOW + 180
        next_hour = rn.next_hourly_close(NOW)
        assert state.retry_at == {(WINDOW.slug, CANDLE): next_hour}
        for later in (240, 480, 1800, next_hour - NOW - 1):
            assert (await rn.pass_once(state, client, NOW + later))["forecast_started"] is False
        assert len(fakes.runs) == 1  # the model ran once for this hour
        assert "database or disk is full" in rn.status()["last_error"]  # still on the card
        # The next hour is a new forecast.
        fakes.raise_ = None
        report = await rn.pass_once(state, client, next_hour + rn.FORECAST_LAG_S)
        assert report["forecast_started"] is True
        await _settle(state)
        assert [run["candle"] for run in fakes.runs] == [CANDLE, CANDLE + 3_600_000]


@pytest.mark.asyncio
async def test_candles_not_ready_say_so_and_retry(lc_db, fakes) -> None:
    fakes.raise_ = MarketDataError("last closed 1h candle opens at 1, expected 2")
    fakes.gate.set()
    state = rn.RunnerState()
    async with _client() as client:
        await rn.pass_once(state, client, NOW)
        await _settle(state)
        note = rn.status()["forecast_note"]
        assert note.startswith("Waiting for Binance's hourly candles: last closed 1h candle")
        await rn.pass_once(state, client, NOW + 5)
        assert rn.status()["forecast_note"] == note and len(fakes.runs) == 1
        fakes.raise_ = None
        await rn.pass_once(state, client, NOW + rn.DATA_RETRY_S)
        await _settle(state)
        assert len(fakes.runs) == 2 and rn.status()["forecast_note"] is None


# --- timing ------------------------------------------------------------------------------------


def test_the_next_forecast_is_a_few_seconds_after_the_next_hourly_close() -> None:
    lag = rn.FORECAST_LAG_S
    assert rn.next_forecast_after(WINDOW, NOW, True) == _ts("2026-09-22T17:00:00") + lag
    before_noon_read = WINDOW.start_ts + 30
    assert rn.next_forecast_after(WINDOW, before_noon_read, False) == WINDOW.start_ts + 60 + lag
    # Past the noon candle's read without a strike (the read failed): no time already gone.
    assert rn.next_forecast_after(WINDOW, WINDOW.start_ts + 60 + lag, False) is None
    assert rn.next_forecast_after(WINDOW, NOW, False) is None
    # The window's last hour: the next forecast is the next window's, after its noon candle.
    last_hour = WINDOW.end_ts - 1800
    assert rn.next_forecast_after(WINDOW, last_hour, True) == WINDOW.end_ts + 60 + lag


def test_the_loop_wakes_just_after_each_hourly_close() -> None:
    assert rn.sleep_seconds(NOW) == rn.POLL_S
    assert rn.sleep_seconds(_ts("2026-09-22T16:59:30")) == pytest.approx(35.0)
    assert rn.sleep_seconds(_ts("2026-09-22T16:59:58")) == pytest.approx(7.0)
    assert rn.sleep_seconds(_ts("2026-09-22T17:00:05")) == rn.POLL_S


def test_the_loop_wakes_for_a_pending_retry_not_a_whole_minute_later() -> None:
    """Review finding (Claude, 2026-09-29): DATA_RETRY_S never took effect."""
    assert rn.sleep_seconds(NOW, [NOW + rn.DATA_RETRY_S]) == pytest.approx(rn.DATA_RETRY_S)
    assert rn.sleep_seconds(NOW, [NOW - 5, NOW + 45, NOW + 30]) == pytest.approx(30.0)
    assert rn.sleep_seconds(NOW, [NOW + 0.2]) == rn.MIN_SLEEP_S
    assert rn.sleep_seconds(NOW, [NOW + 600]) == rn.POLL_S
    state = rn.RunnerState(retry_at={(WINDOW.slug, CANDLE): NOW + 20.0})
    # No strike yet for the window: also wake when the noon candle can be read.
    assert rn.wake_times(state, NOW) == [NOW + 20.0, WINDOW.start_ts + 60 + rn.FORECAST_LAG_S]
    state.strikes = {WINDOW.slug: STRIKE}
    assert rn.wake_times(state, NOW) == [NOW + 20.0]


def test_the_settings_switch_is_registered_and_on_by_default() -> None:
    knob = _knobs.KNOBS[rn.KNOB]
    assert (knob.kind, knob.default) == ("bool", True)
    assert knob.label == "Run the lc2004-Kronos BTC 24h forecast"
    assert knob.group == "lc2004-Kronos BTC 24h forecast"


# --- the loop ----------------------------------------------------------------------------------


@pytest.fixture
def real_loop():
    """The real run_forever (conftest idles it for the dashboard tests)."""
    return getattr(rn, "real_run_forever", rn.run_forever)


@pytest.mark.asyncio
async def test_run_forever_returns_at_once_when_stopped_during_the_startup_delay(
        real_loop, fakes) -> None:
    assert rn.STARTUP_DELAY_S == 15.0
    stop = asyncio.Event()
    task = asyncio.create_task(real_loop(stop))
    await asyncio.sleep(0.05)
    assert not task.done()  # waiting out the delay
    started = asyncio.get_running_loop().time()
    stop.set()
    await asyncio.wait_for(task, 1)
    assert asyncio.get_running_loop().time() - started < 0.5
    assert rn.status()["state"] == rn.STOPPED
    assert fakes.discovered == 0 and fakes.runs == []

    stopped = asyncio.Event()
    stopped.set()
    await asyncio.wait_for(real_loop(stopped), 0.5)  # already stopped: returns at once


@pytest.mark.asyncio
async def test_shutdown_cancels_the_running_forecast(real_loop, lc_db, fakes,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rn, "STARTUP_DELAY_S", 0.0)
    stop = asyncio.Event()
    task = asyncio.create_task(real_loop(stop))
    await asyncio.wait_for(fakes.started.wait(), 2)
    assert rn.status()["forecast_running"] is not None  # the real clock's window
    stop.set()
    await asyncio.wait_for(task, 2)
    assert fakes.cancelled == 1
    status = rn.status()
    assert status["state"] == rn.STOPPED and status["forecast_running"] is None


@pytest.mark.asyncio
async def test_a_pass_that_fails_past_its_guards_is_shown_and_the_loop_goes_on(
        real_loop, lc_db, fakes, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rn, "STARTUP_DELAY_S", 0.0)
    monkeypatch.setattr(rn, "sleep_seconds", lambda now, wake_at=(): 0.01)
    calls: list[float] = []

    async def broken(state, client, now_ts):  # noqa: ANN001
        calls.append(now_ts)
        raise RuntimeError("boom")

    monkeypatch.setattr(rn, "pass_once", broken)
    stop = asyncio.Event()
    task = asyncio.create_task(real_loop(stop))
    for _ in range(100):
        if len(calls) >= 2:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert len(calls) >= 2
    status = rn.status()
    assert status["last_error"] == "A pass failed before it finished: RuntimeError: boom"
    assert status["state"] == rn.STOPPED


@pytest.mark.asyncio
async def test_the_loop_retries_missing_candles_after_data_retry_s(
        real_loop, lc_db, fakes, monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop wakes when the forecast ends and again when its retry is due, not a whole
    poll later (review finding, Claude, 2026-09-29)."""
    monkeypatch.setattr(rn, "STARTUP_DELAY_S", 0.0)
    monkeypatch.setattr(rn, "DATA_RETRY_S", 0.3)
    monkeypatch.setattr(rn, "MIN_SLEEP_S", 0.05)
    monkeypatch.setattr(rn, "POLL_S", 60.0)
    fakes.raise_ = MarketDataError("last closed 1h candle opens at 1, expected 2")
    fakes.gate.set()
    stop = asyncio.Event()
    task = asyncio.create_task(real_loop(stop))
    try:
        for _ in range(300):
            if len(fakes.runs) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(fakes.runs) >= 2, "the retry waited for the next poll"
    finally:
        stop.set()
        await asyncio.wait_for(task, 2)


def test_nothing_here_can_place_an_order() -> None:
    root = Path(__file__).resolve().parents[2]
    for rel in ("ems/lc2004_kronos_btc_24h", "ems/kronos_forecast"):
        for path in (root / rel).glob("*.py"):
            text = path.read_text()
            assert "ems.execution" not in text, path
            assert "ems import strategies" not in text, path
    from ems import inventory, strategies

    assert "lc2004_kronos_btc_24h" not in strategies.STRATEGIES
    assert all("lc2004" not in fam.key for fam in inventory.FAMILIES)


def test_the_app_starts_the_loop_and_stops_it() -> None:
    text = (Path(__file__).resolve().parents[2] / "ems/dashboard/app.py").read_text()
    assert "from ems.lc2004_kronos_btc_24h.runner import run_forever as _run_lc2004" in text
    assert "(lc2004_stop_event, lc2004_task)" in text
