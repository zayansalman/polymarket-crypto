"""The loop trades BTC 1d when started on it, in the selected mode, and settles 1d rows
from any run — same pattern as tests/unit/test_hourly_loop.py for the 1h engine."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot import controller, market_selection, paper
from polymarket_bot.daily_btc import engine as daily_engine
from polymarket_bot.daily_btc import market as dbm
from polymarket_bot.hourly import engine as hourly_engine

WINDOW = dbm.window_for(date(2026, 9, 17))
SLUG = "bitcoin-up-or-down-on-september-17-2026"


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    monkeypatch.setattr(paper, "_timeframe", "5m")
    monkeypatch.setattr(paper, "_risk_gate", None)
    monkeypatch.setattr(paper, "_live_executor", None)
    return _db


async def _insert_daily_row(mode: str = "paper", start: int = WINDOW.reference_ts) -> int:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, strategy_id, market_timeframe, window_start_ts, mode)"
            " VALUES ('x', ?, 'Up', 'open', 0.52, 2.6, 5, 'tsinghua_kronos_btc_24h', '1d', ?, ?)",
            (SLUG, start, mode),
        )
        await conn.commit()
        return int(cur.lastrowid)


def test_btc_1d_is_loop_supported() -> None:
    assert ("btc", "1d") in market_selection.LOOP_SUPPORTED


def test_daily_engine_is_in_the_strategy_registry() -> None:
    assert paper._STRATEGY_ENGINES["1d"] is daily_engine
    assert paper._STRATEGY_ENGINES["1h"] is hourly_engine


@pytest.mark.asyncio
async def test_tick_routes_to_the_daily_engine_and_settles_other_timeframes(
    test_db, monkeypatch
) -> None:
    snap = MagicMock()
    daily_tick = AsyncMock(return_value=snap)
    hourly_settle = AsyncMock()
    monkeypatch.setattr(daily_engine, "tick", daily_tick)
    monkeypatch.setattr(hourly_engine, "settle_due", hourly_settle)
    monkeypatch.setattr(paper, "_timeframe", "1d")
    assert await paper.paper_tick_once() is snap
    assert daily_tick.await_args.kwargs == {"allow_entries": True}
    hourly_settle.assert_awaited_once()
    assert hourly_settle.await_args.args[1] is snap


@pytest.mark.asyncio
async def test_a_5m_tick_settles_due_daily_and_hourly_rows(test_db, monkeypatch) -> None:
    snap = MagicMock(window_slug="btc-updown-5m-1")
    monkeypatch.setattr(paper, "_build_snapshot", AsyncMock(return_value=snap))
    monkeypatch.setattr(paper, "_log_tick", AsyncMock())
    monkeypatch.setattr(paper, "_close_due_positions", AsyncMock())
    monkeypatch.setattr(paper, "_maybe_open_position", AsyncMock())
    monkeypatch.setattr(paper, "_record_and_settle_shadow", AsyncMock())
    hourly_settle = AsyncMock()
    daily_settle = AsyncMock()
    monkeypatch.setattr(hourly_engine, "settle_due", hourly_settle)
    monkeypatch.setattr(daily_engine, "settle_due", daily_settle)
    await paper.paper_tick_once()
    hourly_settle.assert_awaited_once()
    daily_settle.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_1h_tick_settles_due_daily_rows_but_not_its_own(test_db, monkeypatch) -> None:
    """``_settle_other_engines`` never re-settles the engine that is already running."""
    snap = MagicMock()
    monkeypatch.setattr(hourly_engine, "tick", AsyncMock(return_value=snap))
    hourly_settle = AsyncMock()
    daily_settle = AsyncMock()
    monkeypatch.setattr(hourly_engine, "settle_due", hourly_settle)
    monkeypatch.setattr(daily_engine, "settle_due", daily_settle)
    monkeypatch.setattr(paper, "_timeframe", "1h")
    await paper.paper_tick_once()
    hourly_settle.assert_not_awaited()
    daily_settle.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_settlement_of_one_engine_never_blocks_another(
    test_db, monkeypatch
) -> None:
    snap = MagicMock()
    monkeypatch.setattr(daily_engine, "tick", AsyncMock(return_value=snap))
    monkeypatch.setattr(hourly_engine, "settle_due", AsyncMock(side_effect=RuntimeError("boom")))
    daily_settle = AsyncMock()
    monkeypatch.setattr(daily_engine, "settle_due", daily_settle)
    monkeypatch.setattr(paper, "_timeframe", "1d")
    result = await paper.paper_tick_once()
    assert result is snap  # the tick still returns normally


@pytest.mark.asyncio
async def test_five_minute_paths_ignore_daily_rows(test_db) -> None:
    await _insert_daily_row()
    assert await paper._open_legacy_position_exists() is False


@pytest.mark.asyncio
async def test_stop_sells_current_window_daily_rows_regardless_of_pinned_timeframe(
    test_db, monkeypatch
) -> None:
    """Mirrors test_hourly_loop.py's Stop test: matched by window_start_ts to the CURRENT
    window, not gated on the pinned ``_timeframe`` (Claude, 2026-09-15/17, branch-review
    finding 5m-stop-depends-on-hourly-discovery, generalized to every strategy engine).
    """
    position_id = await _insert_daily_row()
    snap = SimpleNamespace(window_slug=SLUG, created_at="y", spot_price=1.0,
                           up_best_bid=0.50, down_best_bid=0.49)
    build = AsyncMock(return_value=snap)
    monkeypatch.setattr(daily_engine, "build_snapshot", build)
    closed = AsyncMock(return_value=True)
    monkeypatch.setattr(paper, "_close_position", closed)
    # The loop is pinned to 5m, but an open live-style daily row for the CURRENT window
    # must still be sellable at Stop.
    assert await paper.force_close_open_positions("STOP_REQUEST") == 1
    build.assert_awaited_once()
    assert closed.await_args.args[0]["position_id"] == position_id
    assert closed.await_args.args[2] == 0.50


@pytest.mark.asyncio
async def test_stop_leaves_a_past_window_daily_row_for_settlement(test_db, monkeypatch) -> None:
    past_start = WINDOW.reference_ts - 3 * 86_400
    position_id = await _insert_daily_row(start=past_start)
    build = AsyncMock(return_value=SimpleNamespace(
        window_slug=SLUG, created_at="y", spot_price=1.0, up_best_bid=0.50, down_best_bid=0.49,
    ))
    monkeypatch.setattr(daily_engine, "build_snapshot", build)
    closed = AsyncMock(return_value=True)
    monkeypatch.setattr(paper, "_close_position", closed)
    assert await paper.force_close_open_positions("STOP_REQUEST") == 0
    build.assert_not_awaited()
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT state FROM paper_positions WHERE position_id = ?",
                                 (position_id,))
        assert (await cur.fetchone())["state"] == "open"


@pytest.mark.asyncio
async def test_start_pins_btc_1d(test_db, monkeypatch) -> None:
    await market_selection.set_selection("btc", "1d")
    started: list[tuple] = []
    monkeypatch.setattr(controller, "_ensure_runner_started", lambda force=False: started.append(
        (controller._mode_cache, controller._timeframe_cache)))
    monkeypatch.setattr(controller, "_ensure_watchdog_started", lambda: None)
    await controller.request_start()
    assert started == [("paper", "1d")]
    controller._desired_running = False


@pytest.mark.asyncio
async def test_start_refuses_an_unsupported_selection_names_all_three_timeframes(
    test_db, monkeypatch
) -> None:
    await market_selection.set_selection("eth", "1d")
    spawn = MagicMock()
    monkeypatch.setattr(controller, "_ensure_runner_started", spawn)
    status = await controller.request_start()
    spawn.assert_not_called()
    assert status.state == "stopped"
    assert "BTC 1d" in status.detail
