"""Adaptive risk controller tests (#36)."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

import config as _config
import db as _db
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot.adaptive import (
    evaluate_and_maybe_pause,
    is_paused,
    rolling_performance,
    should_pause,
    clear_auto_pause,
)


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    return _db


async def _add(db, *, pnl, notional=5.0, edge=0.06, entry=0.55, style="settle",
               quote="clob", state="closed", opened_at="t"):
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state,"
            " entry_price, notional_usd, shares, edge, realized_pnl_usd,"
            " quote_source, strategy_style)"
            " VALUES (?,'w','Up',?,?,?,1,?,?,?,?)",
            (opened_at, state, entry, notional, edge, pnl, quote, style),
        )
        await conn.commit()


# --- pure decision ---------------------------------------------------------


def test_should_pause_warmup_never_pauses():
    paused, reason = should_pause({"n": 5, "roi": -0.9, "pnl": -9}, min_trades=10, min_roi=-0.15)
    assert paused is False and "warming up" in reason


def test_should_pause_trips_below_floor():
    paused, reason = should_pause({"n": 20, "roi": -0.30, "pnl": -30}, min_trades=10, min_roi=-0.15)
    assert paused is True and "below floor" in reason


def test_should_pause_ok_above_floor():
    paused, _ = should_pause({"n": 20, "roi": 0.10, "pnl": 10}, min_trades=10, min_roi=-0.15)
    assert paused is False


# --- rolling metrics -------------------------------------------------------


@pytest.mark.asyncio
async def test_rolling_performance_and_calibration(test_db):
    # 6 wins @ +4.5 (entry .55 -> payout 1.0 on 5 notional ~ +4.09), 4 losses @ -5
    for _ in range(6):
        await _add(test_db, pnl=4.09, edge=0.06, entry=0.55)   # model_prob .61, won
    for _ in range(4):
        await _add(test_db, pnl=-5.0, edge=0.06, entry=0.55)   # model_prob .61, lost
    perf = await rolling_performance(window=20, style="settle")
    assert perf["n"] == 10
    assert perf["win_rate"] == pytest.approx(0.6)
    assert perf["brier"] is not None and 0 < perf["brier"] < 1


@pytest.mark.asyncio
async def test_rolling_excludes_other_style_and_nonclob(test_db):
    await _add(test_db, pnl=4.0, style="scalp")
    await _add(test_db, pnl=4.0, quote="gamma")
    await _add(test_db, pnl=4.0, state="open")
    perf = await rolling_performance(window=20, style="settle")
    assert perf["n"] == 0


# --- sticky auto-pause integration ----------------------------------------


@pytest.mark.asyncio
async def test_evaluate_trips_and_is_sticky(test_db, monkeypatch):
    await _knobs.set("auto_pause_enabled", True)
    await _knobs.set("auto_pause_window", 20)
    await _knobs.set("auto_pause_min_trades", 10)
    await _knobs.set("auto_pause_min_roi", -0.15)
    await _knobs.set("exit_style", "settle")
    for _ in range(12):
        await _add(test_db, pnl=-5.0)  # all losses -> ROI -100%
    paused, reason = await evaluate_and_maybe_pause()
    assert paused is True and "below floor" in reason
    # sticky: stays paused even after we add winners (no auto-resume)
    for _ in range(20):
        await _add(test_db, pnl=4.0)
    paused2, _ = await is_paused()
    assert paused2 is True
    # operator clears -> resumes
    await clear_auto_pause()
    assert (await is_paused())[0] is False


@pytest.mark.asyncio
async def test_clear_records_cleared_at(test_db):
    await clear_auto_pause()
    assert await _db.get_config("polymarket_bot.auto_pause_cleared_at") is not None


@pytest.mark.asyncio
async def test_clear_prevents_immediate_repause(test_db, monkeypatch):
    # The operator's "resume" must actually stick: trades that predate the clear
    # are excluded from the edge window, so it doesn't re-pause on the next tick.
    await _knobs.set("auto_pause_enabled", True)
    await _knobs.set("auto_pause_window", 20)
    await _knobs.set("auto_pause_min_trades", 10)
    await _knobs.set("auto_pause_min_roi", -0.15)
    await _knobs.set("exit_style", "settle")
    await _db.set_config("polymarket_bot.session_start", "2020-01-01T00:00:00+00:00")
    for _ in range(12):
        await _add(test_db, pnl=-5.0, opened_at="2020-01-02T00:00:00+00:00")
    assert (await evaluate_and_maybe_pause())[0] is True

    await clear_auto_pause()  # records cleared_at = now (after the 2020 trades)
    assert (await is_paused())[0] is False
    paused2, reason2 = await evaluate_and_maybe_pause()
    assert paused2 is False  # post-clear window is empty → warming up, not re-paused
    assert "warming up" in reason2


@pytest.mark.asyncio
async def test_repauses_on_fresh_losses_after_clear(test_db, monkeypatch):
    # The adaptive guard still re-protects: losses booked AFTER the clear count.
    await _knobs.set("auto_pause_enabled", True)
    await _knobs.set("auto_pause_window", 20)
    await _knobs.set("auto_pause_min_trades", 10)
    await _knobs.set("auto_pause_min_roi", -0.15)
    await _knobs.set("exit_style", "settle")
    await _db.set_config("polymarket_bot.session_start", "2020-01-01T00:00:00+00:00")
    await clear_auto_pause()  # cleared_at = now
    # 12 fresh losses dated in the future, after the clear timestamp.
    for _ in range(12):
        await _add(test_db, pnl=-5.0, opened_at="2099-01-01T00:00:00+00:00")
    paused, reason = await evaluate_and_maybe_pause()
    assert paused is True and "below floor" in reason


@pytest.mark.asyncio
async def test_evaluate_warmup_does_not_pause(test_db, monkeypatch):
    await _knobs.set("auto_pause_enabled", True)
    await _knobs.set("auto_pause_min_trades", 10)
    await _knobs.set("exit_style", "settle")
    for _ in range(5):
        await _add(test_db, pnl=-5.0)
    paused, _ = await evaluate_and_maybe_pause()
    assert paused is False


@pytest.mark.asyncio
async def test_since_scopes_to_session(test_db, monkeypatch):
    # Old losing trades before the session start must be excluded.
    async with test_db.connect() as conn:
        await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state,"
            " entry_price, notional_usd, shares, edge, realized_pnl_usd,"
            " quote_source, strategy_style)"
            " VALUES ('2026-06-13T00:00:00+00:00','w','Up','closed',0.55,5,1,0.06,-5,'clob','settle')",
        )
        await conn.commit()
    perf_all = await rolling_performance(20, "settle")
    perf_session = await rolling_performance(20, "settle", since="2026-06-14T00:00:00+00:00")
    assert perf_all["n"] == 1
    assert perf_session["n"] == 0  # the old trade is before the session start


@pytest.mark.asyncio
async def test_disabled_never_pauses(test_db, monkeypatch):
    await _knobs.set("auto_pause_enabled", False)
    await _knobs.set("exit_style", "settle")
    for _ in range(20):
        await _add(test_db, pnl=-5.0)
    paused, _ = await evaluate_and_maybe_pause()
    assert paused is False
