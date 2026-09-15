"""The loop trades BTC 1h when started on that selection, in whichever mode Start picked."""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot import controller, market_selection, paper
from polymarket_bot.hourly import engine
from polymarket_bot.hourly import market as hm
from polymarket_exec.execution.live import LiveOrderResult

H = 1_789_326_000
SLUG = hm.slug_for(H)


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    monkeypatch.setattr(paper, "_timeframe", "5m")  # restored after each test
    return _db


def test_btc_1h_is_loop_supported() -> None:
    assert ("btc", "1h") in market_selection.LOOP_SUPPORTED


@pytest.mark.asyncio
async def test_live_on_1h_starts_like_any_live_run_and_stop_cancels_every_slot(
    test_db, monkeypatch
) -> None:
    executor = MagicMock()
    executor.start = AsyncMock()
    executor.cancel_open_all = AsyncMock(return_value=[])
    monkeypatch.setattr(paper, "build_live_executor", MagicMock(return_value=executor))
    feed = MagicMock()
    feed.run = AsyncMock()
    monkeypatch.setattr(paper, "ChainlinkWsFeed", MagicMock(return_value=feed))
    stop = threading.Event()
    stop.set()  # start, then stop straight away

    await paper.run_paper_loop(stop, mode="live", timeframe="1h")

    executor.start.assert_awaited_once()
    executor.cancel_open_all.assert_awaited_once_with(reason="LOOP_STOP")
    assert paper._timeframe == "1h"
    assert await _db.get_config("polymarket_bot.mode") == "live"
    assert await _db.get_config("polymarket_bot.state") == "stopped"


@pytest.mark.asyncio
async def test_stop_in_live_sells_current_hour_rows_through_their_own_slot(
    test_db, monkeypatch
) -> None:
    async with _db.connect() as conn:
        await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, strategy_id, market_timeframe, window_start_ts, mode)"
            " VALUES ('x', ?, 'Down', 'open', 0.52, 2.6, 5, 'hourly_mean_reversion', '1h', ?,"
            " 'live')",
            (SLUG, H),
        )
        await conn.commit()
    slot = MagicMock()
    slot.submit_exit = AsyncMock(return_value=LiveOrderResult(
        ok=True, status="SUBMITTED", order_id="0xX", price=0.50, size=5.0, notional_usd=2.5))
    account = MagicMock()
    account.slot_executor = MagicMock(return_value=slot)
    monkeypatch.setattr(paper, "_live_executor", account)
    monkeypatch.setattr(paper, "_now", lambda: H + 30)  # Stop matches the hour by start time
    snap = SimpleNamespace(window_slug=SLUG, created_at="y", spot_price=1.0,
                           up_best_bid=0.49, down_best_bid=0.50)
    monkeypatch.setattr(engine, "build_snapshot", AsyncMock(return_value=snap))

    assert await paper.force_close_open_positions("STOP_REQUEST") == 1

    account.slot_executor.assert_called_with("hourly_mean_reversion")
    slot.submit_exit.assert_awaited_once_with(side_price=0.50, size=5.0, window_slug=SLUG)
    account.submit_exit.assert_not_called()


@pytest.mark.asyncio
async def test_paper_tick_routes_to_engine_on_1h(test_db, monkeypatch) -> None:
    snap = MagicMock()
    tick = AsyncMock(return_value=snap)
    monkeypatch.setattr(engine, "tick", tick)
    monkeypatch.setattr(paper, "_timeframe", "1h")
    monkeypatch.setattr(paper, "_risk_gate", None)
    monkeypatch.setattr(paper, "_live_executor", None)
    assert await paper.paper_tick_once() is snap
    tick.assert_awaited_once()
    assert tick.await_args.kwargs == {"allow_entries": True}


@pytest.mark.asyncio
async def test_start_pins_the_selected_timeframe(test_db, monkeypatch) -> None:
    # Start writes these globals; monkeypatch restores them so no later test inherits 1h.
    monkeypatch.setattr(controller, "_timeframe_cache", "5m")
    monkeypatch.setattr(controller, "_mode_cache", "paper")
    monkeypatch.setattr(controller, "_desired_running", False)
    await market_selection.set_selection("btc", "1h")
    started: list[tuple] = []
    monkeypatch.setattr(controller, "_ensure_runner_started", lambda force=False: started.append(
        (controller._mode_cache, controller._timeframe_cache)))
    monkeypatch.setattr(controller, "_ensure_watchdog_started", lambda: None)
    await controller.request_start()
    assert started == [("paper", "1h")]


@pytest.mark.asyncio
async def test_start_refuses_an_unsupported_selection(test_db, monkeypatch) -> None:
    await market_selection.set_selection("eth", "1h")
    spawn = MagicMock()
    monkeypatch.setattr(controller, "_ensure_runner_started", spawn)
    status = await controller.request_start()
    spawn.assert_not_called()
    assert status.state == "stopped" and "not wired" in status.detail


@pytest.mark.asyncio
async def test_start_while_running_keeps_the_pinned_timeframe(test_db, monkeypatch) -> None:
    release = threading.Event()
    runner = threading.Thread(target=release.wait, daemon=True)
    runner.start()
    try:
        monkeypatch.setattr(controller, "_timeframe_cache", "5m")
        monkeypatch.setattr(controller, "_mode_cache", "paper")
        monkeypatch.setattr(controller, "_runner_thread", runner)
        monkeypatch.setattr(controller, "_stop_event", threading.Event())
        spawn = MagicMock()
        monkeypatch.setattr(controller, "_ensure_runner_started", spawn)
        await market_selection.set_selection("btc", "1h")

        status = await controller.request_start()

        assert controller._timeframe_cache == "5m"
        spawn.assert_not_called()
        assert "Press Stop" in status.detail
    finally:
        release.set()
        runner.join(5)


def test_loop_thread_hands_the_timeframe_to_the_loop(monkeypatch) -> None:
    seen: dict[str, str] = {}

    async def fake(stop_event, mode=None, timeframe="5m"):
        seen["tf"] = timeframe

    monkeypatch.setattr(controller, "run_paper_loop", fake)
    controller._run_loop_in_thread(threading.Event(), "paper", "1h")
    assert seen == {"tf": "1h"}


def test_runner_spawn_passes_the_pinned_mode_and_timeframe(monkeypatch) -> None:
    monkeypatch.setattr(controller, "_runner_thread", None)
    monkeypatch.setattr(controller, "_stop_event", None)
    monkeypatch.setattr(controller, "_mode_cache", "paper")
    monkeypatch.setattr(controller, "_timeframe_cache", "1h")
    recorder = MagicMock()
    monkeypatch.setattr(controller.threading, "Thread", recorder)

    controller._ensure_runner_started()

    assert recorder.call_args.kwargs["args"][1:] == ("paper", "1h")


async def _insert_open_hourly_row(start_ts: int, mode: str) -> None:
    async with _db.connect() as conn:
        await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, strategy_id, market_timeframe, window_start_ts, mode)"
            " VALUES ('x', ?, 'Down', 'open', 0.52, 2.6, 5, 'hourly_mean_reversion', '1h', ?, ?)",
            (hm.slug_for(start_ts), start_ts, mode),
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_paper_stop_leaves_past_hour_and_no_bid_rows_open(test_db, monkeypatch) -> None:
    await _insert_open_hourly_row(H - 3600, "paper")
    monkeypatch.setattr(paper, "_live_executor", None)
    monkeypatch.setattr(paper, "_build_snapshot", AsyncMock(side_effect=AssertionError))
    monkeypatch.setattr(paper, "_now", lambda: H + 30)
    current_hour = SimpleNamespace(window_slug=SLUG, created_at="y", spot_price=1.0,
                                   up_best_bid=0.49, down_best_bid=0.50)
    monkeypatch.setattr(engine, "build_snapshot", AsyncMock(return_value=current_hour))

    assert await paper.force_close_open_positions("STOP_REQUEST") == 0
    assert await paper.count_open_positions() == 1

    monkeypatch.setattr(paper, "_now", lambda: H - 3600 + 30)
    own_hour_no_bid = SimpleNamespace(window_slug=hm.slug_for(H - 3600), created_at="y",
                                      spot_price=1.0, up_best_bid=0.49, down_best_bid=None)
    monkeypatch.setattr(engine, "build_snapshot", AsyncMock(return_value=own_hour_no_bid))

    assert await paper.force_close_open_positions("STOP_REQUEST") == 0
    assert await paper.count_open_positions() == 1


@pytest.mark.asyncio
async def test_live_stop_never_sells_a_past_hour_row(test_db, monkeypatch) -> None:
    await _insert_open_hourly_row(H - 3600, "live")
    slot = MagicMock()
    slot.submit_exit = AsyncMock()
    account = MagicMock()
    account.slot_executor = MagicMock(return_value=slot)
    monkeypatch.setattr(paper, "_live_executor", account)
    monkeypatch.setattr(paper, "_now", lambda: H + 30)
    snap = SimpleNamespace(window_slug=SLUG, created_at="y", spot_price=1.0,
                           up_best_bid=0.49, down_best_bid=0.50)
    monkeypatch.setattr(engine, "build_snapshot", AsyncMock(return_value=snap))

    assert await paper.force_close_open_positions("STOP_REQUEST") == 0

    slot.submit_exit.assert_not_awaited()
    account.submit_exit.assert_not_called()
    assert await paper.count_open_positions(mode="live") == 1


@pytest.mark.asyncio
async def test_stop_matches_the_current_hour_by_start_time_not_slug(test_db, monkeypatch) -> None:
    # Claude, 2026-09-15, branch-review finding dst-fallback-slug-collision: on 2026-11-01 the
    # 05:00Z and 06:00Z hours share one ET slug; a 05:00Z row is past once 06:00Z has begun.
    first, second = 1_793_509_200, 1_793_512_800
    await _insert_open_hourly_row(first, "paper")
    monkeypatch.setattr(paper, "_live_executor", None)
    monkeypatch.setattr(paper, "_now", lambda: second + 30)
    same_slug_next_hour = SimpleNamespace(window_slug=hm.slug_for(second), created_at="y",
                                          spot_price=1.0, up_best_bid=0.49, down_best_bid=0.50)
    build = AsyncMock(return_value=same_slug_next_hour)
    monkeypatch.setattr(engine, "build_snapshot", build)

    assert await paper.force_close_open_positions("STOP_REQUEST") == 0

    assert await paper.count_open_positions() == 1
    assert build.await_args.args[1:] == (second + 30,)
