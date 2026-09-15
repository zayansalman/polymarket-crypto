"""The loop trades BTC 1h when started on that selection, in whichever mode Start picked."""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio

import config as _config
import db as _db
from polymarket_bot import controller, market_selection, paper
from polymarket_bot.hourly import engine
from polymarket_bot.hourly import market as hm
from polymarket_exec.execution.live import LiveExecutor, LiveOrderResult

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
    current_hour = SimpleNamespace(window_slug=SLUG, created_at="y", spot_price=1.0,
                                   up_best_bid=0.49, down_best_bid=0.50)
    monkeypatch.setattr(engine, "build_snapshot", AsyncMock(return_value=current_hour))

    assert await paper.force_close_open_positions("STOP_REQUEST") == 0
    assert await paper.count_open_positions() == 1

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
    snap = SimpleNamespace(window_slug=SLUG, created_at="y", spot_price=1.0,
                           up_best_bid=0.49, down_best_bid=0.50)
    monkeypatch.setattr(engine, "build_snapshot", AsyncMock(return_value=snap))

    assert await paper.force_close_open_positions("STOP_REQUEST") == 0

    slot.submit_exit.assert_not_awaited()
    account.submit_exit.assert_not_called()
    assert await paper.count_open_positions(mode="live") == 1


# Claude, 2026-09-15, branch-review finding other-timeframe-live-rows-never-settled:
# boot adopts open live rows of either timeframe, so each run settles both kinds.
MR = "hourly_mean_reversion"
FIVE_MIN_SLUG = f"btc-updown-5m-{H}"


def _binance_closed_hour(request: httpx.Request) -> httpx.Response:
    """Binance spot 1h klines: the requested hour closed Up (105 over 100), next hour open."""
    params = request.url.params
    if str(request.url).startswith(f"{_config.BINANCE_API_BASE}/api/v3/klines"):
        start = int(params["startTime"]) // 1000

        def kline(open_s: int, o: float, c: float) -> list:
            return [open_s * 1000, str(o), str(max(o, c)), str(min(o, c)), str(c), "10",
                    open_s * 1000 + 3_599_999, "1000", 5, "5", "0", "0"]

        return httpx.Response(200, json=[kline(start, 100.0, 105.0), kline(start + 3600, 105, 105)])
    return httpx.Response(404)


def _clob_client_mock() -> MagicMock:
    client = MagicMock()
    client.create_or_derive_api_key.return_value = SimpleNamespace(
        api_key="k", api_secret="s", api_passphrase="p")
    client.get_ok.return_value = "OK"
    client.cancel_all.return_value = {"canceled": [], "not_canceled": {}}
    client.get_order.return_value = {"size_matched": "5.26", "price": "0.57"}
    return client


@pytest.mark.asyncio
async def test_5m_tick_settles_an_adopted_past_hour_live_hourly_row_into_the_loss_halt(
    test_db, tmp_path, monkeypatch
) -> None:
    past = H - 3600
    await _insert_open_hourly_row(past, "live")
    await _db.journal_live_order(
        intent="ENTRY", side="BUY", status="SUBMITTED", window_slug=hm.slug_for(past),
        token_id="1234567890", price=0.57, size=5.26, clob_order_id="0xMR", strategy_id=MR,
    )
    account = LiveExecutor(
        private_key="0x" + "1" * 64, funder="0xF", signature_type=2, max_trade_usd=3.0,
        daily_loss_halt_usd=10.0, bankroll_cap_usd=30.0, max_entry_slippage=0.5,
        exit_fill_timeout_seconds=5.0, kill_switch_path=tmp_path / "KILL",
        client=_clob_client_mock(),
    )
    await account.start()
    assert account.slot_executor(MR)._position_open is True  # boot adopted it

    # The operator then started BTC 5m live.
    monkeypatch.setattr(paper, "_live_executor", account)
    monkeypatch.setattr(paper, "_risk_gate", account.gate)
    monkeypatch.setattr(paper, "_now", lambda: H + 600)
    monkeypatch.setattr(paper, "_make_settlement_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_binance_closed_hour)))
    five_min = SimpleNamespace(window_slug=FIVE_MIN_SLUG, signal_side=None, notional_usd=0.0,
                               created_at="y", spot_price=1.0)
    monkeypatch.setattr(paper, "_build_snapshot", AsyncMock(return_value=five_min))
    monkeypatch.setattr(paper, "_log_tick", AsyncMock())
    monkeypatch.setattr(paper, "_record_and_settle_shadow", AsyncMock())

    await paper.paper_tick_once()

    assert await paper.count_open_positions(mode="live") == 0
    assert account.slot_executor(MR)._position_open is False
    assert account.gate.halt_pnl < -2.9  # the held Down side lost: about -3.09 booked


@pytest.mark.asyncio
async def test_1h_tick_settles_an_adopted_rolled_live_5m_row_before_the_hourly_tick(
    test_db, monkeypatch
) -> None:
    rolled_slug = f"btc-updown-5m-{H - 300}"
    async with _db.connect() as conn:
        await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, mode) VALUES ('x', ?, 'Up', 'open', 0.5, 2.5, 5, 'live')",
            (rolled_slug,),
        )
        await conn.commit()
    order: list[str] = []
    account = MagicMock()
    account.enforce_kill_switch = AsyncMock(return_value=False)

    async def record_settlement(won: bool, window_slug: str) -> LiveOrderResult:
        order.append("5m settlement")
        return LiveOrderResult(ok=True, status="SETTLED", price=1.0, size=5.0,
                               notional_usd=2.4125)

    account.record_settlement = AsyncMock(side_effect=record_settlement)
    monkeypatch.setattr(paper, "_live_executor", account)
    monkeypatch.setattr(paper, "_risk_gate", None)
    monkeypatch.setattr(paper, "_timeframe", "1h")
    monkeypatch.setattr(paper, "_now", lambda: H + 600)
    monkeypatch.setattr(paper, "_make_settlement_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(
                            lambda r: httpx.Response(404))))
    monkeypatch.setattr(paper, "_build_snapshot", AsyncMock(return_value=SimpleNamespace(
        window_slug=FIVE_MIN_SLUG, created_at="y", spot_price=1.0,
        up_best_bid=0.5, down_best_bid=0.5)))
    connector = MagicMock()
    connector.settle_window = AsyncMock(return_value=True)  # Up won
    monkeypatch.setattr(paper, "_make_settlement_connector", MagicMock(return_value=connector))
    hourly_snap = MagicMock()

    async def hourly_tick(client, *, allow_entries: bool):
        order.append("hourly tick")
        return hourly_snap

    monkeypatch.setattr(engine, "tick", hourly_tick)

    assert await paper.paper_tick_once() is hourly_snap

    account.record_settlement.assert_awaited_once_with(True, rolled_slug)
    assert order == ["5m settlement", "hourly tick"]
    assert await paper.count_open_positions(mode="live") == 0


@pytest.mark.asyncio
async def test_1h_tick_still_runs_when_the_5m_snapshot_for_an_open_5m_row_fails(
    test_db, monkeypatch
) -> None:
    async with _db.connect() as conn:
        await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, mode) VALUES ('x', ?, 'Up', 'open', 0.5, 2.5, 5, 'paper')",
            (f"btc-updown-5m-{H - 300}",),
        )
        await conn.commit()
    monkeypatch.setattr(paper, "_live_executor", None)
    monkeypatch.setattr(paper, "_risk_gate", None)
    monkeypatch.setattr(paper, "_timeframe", "1h")
    monkeypatch.setattr(paper, "_build_snapshot",
                        AsyncMock(side_effect=RuntimeError("Could not discover BTC 5m market")))
    hourly_snap = MagicMock()
    tick = AsyncMock(return_value=hourly_snap)
    monkeypatch.setattr(engine, "tick", tick)

    assert await paper.paper_tick_once() is hourly_snap

    tick.assert_awaited_once()
    assert await paper.count_open_positions() == 1  # retried on the next tick


@pytest.mark.asyncio
async def test_stop_still_sells_the_current_hour_row_when_the_5m_snapshot_fails(
    test_db, monkeypatch
) -> None:
    async with _db.connect() as conn:
        await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, mode) VALUES ('a', ?, 'Up', 'open', 0.5, 2.5, 5, 'live')",
            (FIVE_MIN_SLUG,),
        )
        await conn.commit()
    await _insert_open_hourly_row(H, "live")
    slot = MagicMock()
    slot.submit_exit = AsyncMock(return_value=LiveOrderResult(
        ok=True, status="SUBMITTED", order_id="0xX", price=0.50, size=5.0, notional_usd=2.5))
    account = MagicMock()
    account.slot_executor = MagicMock(return_value=slot)
    monkeypatch.setattr(paper, "_live_executor", account)
    monkeypatch.setattr(paper, "_timeframe", "1h")
    monkeypatch.setattr(paper, "_now", lambda: H + 600)
    monkeypatch.setattr(paper, "_build_snapshot",
                        AsyncMock(side_effect=RuntimeError("Could not discover BTC 5m market")))
    monkeypatch.setattr(engine, "build_snapshot", AsyncMock(return_value=SimpleNamespace(
        window_slug=SLUG, created_at="y", spot_price=1.0, up_best_bid=0.49, down_best_bid=0.50)))

    with pytest.raises(RuntimeError, match="5m market"):
        await paper.force_close_open_positions("STOP_REQUEST")

    slot.submit_exit.assert_awaited_once_with(side_price=0.50, size=5.0, window_slug=SLUG)
    assert await paper.count_open_positions(mode="live") == 1  # only the 5m row is left
