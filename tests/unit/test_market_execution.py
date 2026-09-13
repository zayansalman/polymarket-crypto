"""Market execution strategy — operator Buy Up / Buy Down through the shared pipeline.

No network, no real ClobClient: the live executor is a mock and the loop's
snapshot is built by hand. Verifies that a Market click is consumed by the
runner through the SAME entry pipeline the model uses (paper fill or
LiveExecutor.submit_entry), that safety gates still apply while model-quality
filters don't, that the model path is untouched, and that the dashboard ↔
runner handoff always resolves the click.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

import config as _config
import db as _db
import polymarket_bot.controller as controller
import polymarket_bot.paper as paper
from polymarket_bot import manual_entry
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot.adaptive import rolling_performance
from polymarket_bot.manual_entry import EntryOutcome, ManualEntryIntent
from polymarket_exec.execution.gate import EntryRequest, GateConfig, RiskGate
from polymarket_exec.execution.live import LiveOrderResult

SLUG = "btc-updown-5m-1770000000"


@pytest_asyncio.fixture
async def bot_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "test_market.db")
    await _db.init_db()
    return _db


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch):
    """Fresh handoff slot, knob cache and loop globals for every test."""
    manual_entry.reset_for_tests()
    # A private cache copy: refresh_cache()/set() in one test can't leak a
    # "market" strategy into the rest of the suite.
    monkeypatch.setattr(_knobs, "_cache", dict(_knobs._cache))
    monkeypatch.setitem(_knobs._cache, "execution_strategy", "market")
    monkeypatch.setitem(_knobs._cache, "exit_style", "settle")
    monkeypatch.setattr(paper, "_live_executor", None)
    monkeypatch.setattr(paper, "_risk_gate", None)
    yield
    manual_entry.reset_for_tests()


def _snapshot(side: str | None = None, **overrides) -> paper.PaperSnapshot:
    base = dict(
        created_at="2026-06-10T12:00:00+00:00",
        window_slug=SLUG,
        market_question="Bitcoin Up or Down?",
        remaining_seconds=200,
        spot_price=100000.0,
        reference_price=99950.0,
        sigma_per_second=0.5,
        market_up_price=0.57,
        market_down_price=0.43,
        fair_up_prob=0.65,
        edge=0.08,
        signal_side=side,
        confidence=0.8 if side else 0.0,
        notional_usd=3.0 if side else 0.0,
        reason="edge above minimum" if side else "skip: no edge",
        feed_source="test",
        up_token_id="UP_TOKEN",
        down_token_id="DOWN_TOKEN",
    )
    base.update(overrides)
    return paper.PaperSnapshot(**base)


def _intent(side: str = "Up", **overrides) -> ManualEntryIntent:
    base = dict(side=side, window_slug=SLUG, seen_ask=None, mode="paper")
    base.update(overrides)
    return ManualEntryIntent(**base)


def _mock_executor(*, fully_matched: bool = True) -> MagicMock:
    executor = MagicMock()
    executor.submit_entry = AsyncMock(
        return_value=LiveOrderResult(
            ok=True, status="SUBMITTED", order_id="0xE1",
            price=0.57, size=5.0, notional_usd=2.85,
        )
    )
    executor.enforce_kill_switch = AsyncMock(return_value=False)
    executor.resync_flat = AsyncMock(return_value=False)
    executor.cancel_open = AsyncMock(return_value=[])
    # submit_entry drops its resting-order id when the placement fully matched.
    executor._entry_order_id = None if fully_matched else "0xE1"
    return executor


def _gate(tmp_path: Path, *, slippage: float = 0.02) -> RiskGate:
    return RiskGate(
        GateConfig(
            max_trade_usd=5.0,
            daily_loss_halt_usd=10.0,
            bankroll_cap_usd=None,
            max_entry_slippage=slippage,
            kill_switch_path=tmp_path / "KILL",
        ),
        is_live=False,
    )


async def _consume(intent: ManualEntryIntent, snapshot: paper.PaperSnapshot) -> EntryOutcome:
    await paper._consume_manual_intent(intent, snapshot)
    return intent.future.result(timeout=0)


async def _rows(bot_db, table: str = "paper_positions") -> list[dict]:
    async with bot_db.connect() as conn:
        async with conn.execute(f"SELECT * FROM {table}") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def _insert_row(bot_db, *, state: str, entry_source: str | None = None, **cols) -> None:
    values = dict(
        opened_at="2026-06-10T11:00:00+00:00", window_slug=SLUG, side="Up",
        state=state, entry_price=0.5, notional_usd=2.5, shares=5.0,
        quote_source="clob", strategy_style="settle", mode="paper",
        entry_source=entry_source,
    )
    values.update(cols)
    names = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    async with bot_db.connect() as conn:
        await conn.execute(
            f"INSERT INTO paper_positions({names}) VALUES ({marks})", tuple(values.values())
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Paper Market buys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("side,ask", [("Up", 0.57), ("Down", 0.43)])
async def test_paper_market_buy_opens_one_market_row(bot_db, side: str, ask: float) -> None:
    outcome = await _consume(_intent(side, seen_ask=ask), _snapshot())

    assert outcome.status == "filled"
    assert outcome.mode == "paper" and outcome.side == side
    assert outcome.price == pytest.approx(ask)
    assert outcome.shares == pytest.approx(5.0)
    rows = await _rows(bot_db)
    assert len(rows) == 1
    row = rows[0]
    assert row["side"] == side
    assert row["entry_price"] == pytest.approx(ask)
    assert row["shares"] >= 5.0 - 1e-9
    assert row["mode"] == "paper"
    assert row["entry_source"] == "market"
    assert row["confidence"] is None and row["edge"] is None
    assert row["entry_reason"] == f"market: operator Buy {side} @ {ask:.3f}"
    assert row["strategy_style"] == "settle"
    assert paper._live_executor is None
    assert await _rows(bot_db, "live_orders") == []
    feed = await _rows(bot_db, "notification_feed")
    assert any(
        f["event_type"] == "paper_entry" and f["message"].startswith("[market] Paper BUY")
        for f in feed
    )


@pytest.mark.asyncio
async def test_market_buy_sizes_to_operator_trade_shares(
    bot_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = _gate(tmp_path)
    gate._runtime_trade_shares = 7.0
    monkeypatch.setattr(paper, "_risk_gate", gate)

    outcome = await _consume(_intent("Up"), _snapshot())

    assert outcome.status == "filled"
    assert outcome.shares == pytest.approx(7.0)
    assert outcome.notional_usd == pytest.approx(7.0 * 0.57)


@pytest.mark.asyncio
async def test_model_entry_row_is_tagged_model(bot_db) -> None:
    await paper._maybe_open_position(_snapshot("Up"))
    rows = await _rows(bot_db)
    assert len(rows) == 1
    assert rows[0]["entry_source"] == "model"
    assert rows[0]["confidence"] == pytest.approx(0.8)
    assert rows[0]["edge"] == pytest.approx(0.08)
    assert rows[0]["entry_reason"] == "edge above minimum"


# ---------------------------------------------------------------------------
# Live Market buys (mock executor — one shared pipeline)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("side,token", [("Up", "UP_TOKEN"), ("Down", "DOWN_TOKEN")])
async def test_live_market_buy_routes_through_executor(
    bot_db, monkeypatch: pytest.MonkeyPatch, side: str, token: str
) -> None:
    executor = _mock_executor()
    monkeypatch.setattr(paper, "_live_executor", executor)

    outcome = await _consume(_intent(side, seen_ask=0.55, mode="live"), _snapshot())

    executor.resync_flat.assert_awaited_once()
    kwargs = executor.submit_entry.await_args.kwargs
    assert kwargs["token_id"] == token
    assert kwargs["side_price"] == 0.55  # the ask the operator saw
    assert kwargs["window_slug"] == SLUG
    assert outcome.status == "filled"
    assert outcome.mode == "live"
    rows = await _rows(bot_db)
    assert len(rows) == 1
    assert rows[0]["mode"] == "live" and rows[0]["entry_source"] == "market"
    assert rows[0]["shares"] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_live_market_buy_partly_resting_reports_placed(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paper, "_live_executor", _mock_executor(fully_matched=False))

    outcome = await _consume(_intent("Up", mode="live"), _snapshot())

    assert outcome.status == "placed"
    assert "rests on the book" in outcome.detail
    assert len(await _rows(bot_db)) == 1


@pytest.mark.asyncio
async def test_blocked_live_market_buy_writes_no_row(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _mock_executor()
    executor.submit_entry = AsyncMock(
        return_value=LiveOrderResult(ok=False, status="BLOCKED", reason="per-trade cap: 5.70 USD")
    )
    monkeypatch.setattr(paper, "_live_executor", executor)

    outcome = await _consume(_intent("Up", mode="live"), _snapshot())

    assert outcome.status == "blocked"
    assert "per-trade cap" in outcome.detail
    assert await _rows(bot_db) == []


@pytest.mark.asyncio
async def test_model_live_entry_side_price_is_still_the_tick_ask(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _mock_executor()
    monkeypatch.setattr(paper, "_live_executor", executor)

    await paper._maybe_open_position(_snapshot("Down"))

    kwargs = executor.submit_entry.await_args.kwargs
    assert kwargs["token_id"] == "DOWN_TOKEN"
    assert kwargs["side_price"] == 0.43
    assert kwargs["notional_usd"] == 3.0


# ---------------------------------------------------------------------------
# Refusals — every one carries a human-readable detail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_click_while_position_open_is_blocked(bot_db) -> None:
    assert (await _consume(_intent("Up"), _snapshot())).status == "filled"

    outcome = await _consume(_intent("Down"), _snapshot())

    assert outcome.status == "blocked"
    assert "max 1" in outcome.detail
    assert len(await _rows(bot_db)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intent_kw,snap_kw,expected",
    [
        ({}, {"market_up_price": None}, "No ask on the Up book"),
        ({"window_slug": "btc-updown-5m-1769999700"}, {}, "Window rolled"),
        ({"requested_at": time.monotonic() - 60}, {}, "expired"),
        ({"mode": "live"}, {}, "Mode changed"),
        ({}, {"remaining_seconds": 0}, "Window already ended"),
        ({}, {"up_best_bid": 0.60}, "crossed"),
        ({"side": "Sideways"}, {}, "Unknown side"),
    ],
)
async def test_mismatched_or_unexecutable_click_is_blocked(
    bot_db, intent_kw: dict, snap_kw: dict, expected: str
) -> None:
    outcome = await _consume(_intent(**intent_kw), _snapshot(**snap_kw))

    assert outcome.status == "blocked"
    assert expected in outcome.detail
    assert await _rows(bot_db) == []


@pytest.mark.asyncio
async def test_click_refused_when_strategy_is_model(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(_knobs._cache, "execution_strategy", "model")

    outcome = await _consume(_intent("Up"), _snapshot())

    assert outcome.status == "blocked"
    assert "switch to Market" in outcome.detail
    assert await _rows(bot_db) == []


@pytest.mark.asyncio
async def test_unexpected_failure_resolves_as_error(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paper, "_execute_entry", AsyncMock(side_effect=RuntimeError("boom")))

    outcome = await _consume(_intent("Up"), _snapshot())

    assert outcome.status == "error"
    assert outcome.detail == "Market order failed: boom"


@pytest.mark.asyncio
async def test_abandoned_click_is_skipped(bot_db) -> None:
    intent = _intent("Up")
    assert intent.future.cancel()  # the dashboard gave up before pickup

    await paper._consume_manual_intent(intent, _snapshot())

    assert await _rows(bot_db) == []


# ---------------------------------------------------------------------------
# Safety gates still apply (paper parity)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_market_buy_blocked_by_breached_loss_halt(
    bot_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = _gate(tmp_path)
    await gate.record_realized_pnl(-12.0, is_live=False)
    monkeypatch.setattr(paper, "_risk_gate", gate)

    outcome = await _consume(_intent("Up"), _snapshot())

    assert outcome.status == "blocked"
    assert "daily loss halt" in outcome.detail
    assert await _rows(bot_db) == []
    journal = await _rows(bot_db, "live_orders")
    assert len(journal) == 1
    assert journal[0]["status"] == "BLOCKED" and journal[0]["mode"] == "paper"
    assert journal[0]["token_id"] == "UP_TOKEN"


@pytest.mark.asyncio
async def test_paper_market_buy_blocked_by_kill_switch(
    bot_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = _gate(tmp_path)
    (tmp_path / "KILL").touch()
    monkeypatch.setattr(paper, "_risk_gate", gate)

    outcome = await _consume(_intent("Down"), _snapshot())

    assert outcome.status == "blocked"
    assert "KILL switch" in outcome.detail
    journal = await _rows(bot_db, "live_orders")
    assert [(j["status"], j["mode"], j["token_id"]) for j in journal] == [
        ("BLOCKED", "paper", "DOWN_TOKEN")
    ]


@pytest.mark.asyncio
async def test_paper_market_buy_slippage_guard_uses_seen_ask(
    bot_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paper, "_risk_gate", _gate(tmp_path, slippage=0.02))

    outcome = await _consume(
        _intent("Up", seen_ask=0.50), _snapshot(market_up_price=0.55)
    )

    assert outcome.status == "blocked"
    assert "slippage" in outcome.detail
    assert "expected price 0.500" in outcome.detail
    assert await _rows(bot_db) == []


def test_slippage_message_is_neutral(tmp_path: Path) -> None:
    msg = _gate(tmp_path).block_reason(
        EntryRequest(
            notional_usd=1.0, position_open=False, entry_order_resting=False,
            side_price=0.52, best_ask=0.55,
        )
    )
    assert msg == (
        "entry slippage guard: best ask 0.550 is 0.030 above the expected price 0.520 "
        "(cap 0.020)"
    )


# ---------------------------------------------------------------------------
# Model-quality filters do NOT apply to Market (safety ones do)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_pause_blocks_model_but_not_market(bot_db) -> None:
    await _db.set_config("polymarket_bot.auto_paused", "1")

    await paper._maybe_open_position(_snapshot("Up"))
    assert await _rows(bot_db) == []

    outcome = await _consume(_intent("Up"), _snapshot())
    assert outcome.status == "filled"
    assert len(await _rows(bot_db)) == 1


@pytest.mark.asyncio
async def test_settle_one_entry_per_window_does_not_block_market(bot_db) -> None:
    await _insert_row(bot_db, state="closed", entry_source="model")

    await paper._maybe_open_position(_snapshot("Up"))
    assert len(await _rows(bot_db)) == 1  # model: window already traded

    outcome = await _consume(_intent("Up"), _snapshot())
    assert outcome.status == "filled"
    assert len(await _rows(bot_db)) == 2

    blocked = await _consume(_intent("Down"), _snapshot())
    assert blocked.status == "blocked" and "max 1" in blocked.detail


@pytest.mark.asyncio
async def test_market_ignores_entry_price_band_and_min_remaining(bot_db) -> None:
    # 0.43 is below the model's 0.50 min entry price; 20s is below its 60s floor.
    outcome = await _consume(_intent("Down"), _snapshot(remaining_seconds=20))
    assert outcome.status == "filled"
    assert outcome.price == pytest.approx(0.43)


# ---------------------------------------------------------------------------
# Tick wiring
# ---------------------------------------------------------------------------


def _stub_tick(monkeypatch: pytest.MonkeyPatch, snapshot: paper.PaperSnapshot) -> None:
    monkeypatch.setattr(paper, "_build_snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(paper, "_record_and_settle_shadow", AsyncMock())


@pytest.mark.asyncio
async def test_market_strategy_tick_does_not_auto_enter(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _knobs.set("execution_strategy", "market")
    _stub_tick(monkeypatch, _snapshot("Up"))

    await paper.paper_tick_once()

    assert await _rows(bot_db) == []


@pytest.mark.asyncio
async def test_model_strategy_tick_still_auto_enters(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _knobs.set("execution_strategy", "model")
    _stub_tick(monkeypatch, _snapshot("Up"))

    await paper.paper_tick_once()

    rows = await _rows(bot_db)
    assert len(rows) == 1 and rows[0]["entry_source"] == "model"


@pytest.mark.asyncio
async def test_invalid_persisted_strategy_reads_as_model(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _db.set_config("runtime.execution.strategy", "yolo")
    _stub_tick(monkeypatch, _snapshot("Up"))

    await paper.paper_tick_once()

    assert paper._execution_strategy() == "model"
    assert len(await _rows(bot_db)) == 1


@pytest.mark.asyncio
async def test_tick_consumes_pending_click_instead_of_model(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _knobs.set("execution_strategy", "market")
    _stub_tick(monkeypatch, _snapshot("Up"))
    intent = _intent("Down")
    assert manual_entry.submit(intent)
    assert manual_entry.wake.is_set()

    await paper.paper_tick_once()

    outcome = intent.future.result(timeout=0)
    assert outcome.status == "filled" and outcome.side == "Down"
    rows = await _rows(bot_db)
    assert [(r["side"], r["entry_source"]) for r in rows] == [("Down", "market")]
    assert not manual_entry.has_pending() and not manual_entry.wake.is_set()


@pytest.mark.asyncio
async def test_live_kill_switch_refuses_pending_click(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _knobs.set("execution_strategy", "market")
    executor = _mock_executor()
    executor.enforce_kill_switch = AsyncMock(return_value=True)
    monkeypatch.setattr(paper, "_live_executor", executor)
    _stub_tick(monkeypatch, _snapshot())
    intent = _intent("Up", mode="live")
    manual_entry.submit(intent)

    await paper.paper_tick_once()

    outcome = intent.future.result(timeout=0)
    assert outcome.status == "blocked" and "Kill switch" in outcome.detail
    executor.submit_entry.assert_not_awaited()
    assert await _rows(bot_db) == []


@pytest.mark.asyncio
async def test_sleep_wakes_early_for_pending_click() -> None:
    stop = threading.Event()
    manual_entry.submit(_intent("Up"))
    started = time.monotonic()

    await paper._sleep_interruptible(stop, 5.0)

    assert time.monotonic() - started < 1.0


class _QuietFeed:
    """Chainlink WS feed stand-in: never connects, just waits to be cancelled."""

    def __init__(self, **_kwargs) -> None:
        pass

    async def run(self) -> None:
        await asyncio.Event().wait()

    def stop(self) -> None:
        pass


@pytest.mark.asyncio
async def test_failed_tick_refuses_waiting_click(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paper, "ChainlinkWsFeed", _QuietFeed)
    stop = threading.Event()
    intent = _intent("Up")
    manual_entry.submit(intent)

    async def failing_tick() -> paper.PaperSnapshot:
        stop.set()
        raise RuntimeError("gamma down")

    monkeypatch.setattr(paper, "paper_tick_once", failing_tick)

    await paper.run_paper_loop(stop, mode="paper")

    outcome = intent.future.result(timeout=0)
    assert outcome.status == "error"
    assert "gamma down" in outcome.detail
    assert not manual_entry.wake.is_set()


@pytest.mark.asyncio
async def test_loop_stop_refuses_click_left_in_slot(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paper, "ChainlinkWsFeed", _QuietFeed)
    stop = threading.Event()
    late = _intent("Down")

    async def tick() -> paper.PaperSnapshot:
        manual_entry.submit(late)  # arrives after this tick's entry step
        stop.set()
        return _snapshot()

    monkeypatch.setattr(paper, "paper_tick_once", tick)

    await paper.run_paper_loop(stop, mode="paper")

    outcome = late.future.result(timeout=0)
    assert outcome.status == "blocked"
    assert outcome.detail == "Bot stopped — order not placed"
    assert not manual_entry.has_pending()


@pytest.mark.asyncio
async def test_live_boot_refusal_refuses_waiting_click(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_config, "POLYMARKET_PRIVATE_KEY", "")
    intent = _intent("Up", mode="live")
    manual_entry.submit(intent)

    await paper.run_paper_loop(threading.Event(), mode="live")

    outcome = intent.future.result(timeout=0)
    assert outcome.status == "blocked" and "LIVE mode refused" in outcome.detail


def test_detail_line_says_market_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    snap = _snapshot("Up")
    assert "Market mode — model auto-entries off" in paper._detail_from_snapshot(snap)
    monkeypatch.setitem(_knobs._cache, "execution_strategy", "model")
    assert "Market mode" not in paper._detail_from_snapshot(snap)


# ---------------------------------------------------------------------------
# Exits: BAND_REENTRY is a model exit, TIME/TARGET/STOP are not
# ---------------------------------------------------------------------------


def _pos(entry_source: str | None) -> dict:
    return {
        "position_id": 1, "side": "Up", "entry_price": 0.50, "shares": 6.0,
        "notional_usd": 3.0, "window_slug": SLUG, "realized_pnl_usd": None,
        "entry_source": entry_source,
    }


def test_band_reentry_skipped_for_market_rows_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(_knobs._cache, "exit_style", "scalp")
    snap = _snapshot(edge=0.0, up_best_ask=0.51, up_best_bid=0.50)

    assert paper._exit_reason(snap, _pos("model"), exit_price=0.50) == "BAND_REENTRY"
    assert paper._exit_reason(snap, _pos(None), exit_price=0.50) == "BAND_REENTRY"
    assert paper._exit_reason(snap, _pos("market"), exit_price=0.50) is None


def test_time_exit_still_applies_to_market_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(_knobs._cache, "exit_style", "scalp")
    snap = _snapshot(edge=0.0, up_best_ask=0.51, remaining_seconds=30)

    assert paper._exit_reason(snap, _pos("market"), exit_price=0.50) == "TIME"
    assert paper._exit_reason(snap, _pos("market"), exit_price=0.60) == "TIME"


@pytest.mark.asyncio
async def test_close_due_positions_selects_entry_source(bot_db, monkeypatch) -> None:
    monkeypatch.setitem(_knobs._cache, "exit_style", "scalp")
    await _consume(_intent("Up"), _snapshot())
    snap = _snapshot(edge=0.0, up_best_ask=0.58, up_best_bid=0.57)

    await paper._close_due_positions(snap, MagicMock())

    rows = await _rows(bot_db)
    assert rows[0]["state"] == "open"  # BAND_REENTRY skipped for the market row


# ---------------------------------------------------------------------------
# Adaptive auto-pause only judges the model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rolling_performance_excludes_market_rows(bot_db) -> None:
    await _insert_row(bot_db, state="closed", entry_source="market", realized_pnl_usd=-2.5)
    await _insert_row(bot_db, state="closed", entry_source="model", realized_pnl_usd=1.0)
    await _insert_row(bot_db, state="closed", entry_source=None, realized_pnl_usd=1.0)

    perf = await rolling_performance(20, "settle")

    assert perf["n"] == 2
    assert perf["pnl"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Handoff module
# ---------------------------------------------------------------------------


def test_slot_holds_one_click() -> None:
    first, second = _intent("Up"), _intent("Down")
    assert manual_entry.submit(first)
    assert not manual_entry.submit(second)
    assert manual_entry.has_pending()
    assert manual_entry.take() is first
    assert manual_entry.take() is None
    assert not manual_entry.wake.is_set()


def test_withdraw_only_removes_its_own_click() -> None:
    mine, other = _intent("Up"), _intent("Down")
    manual_entry.submit(other)
    assert not manual_entry.withdraw(mine)
    assert manual_entry.withdraw(other)
    assert not manual_entry.has_pending() and not manual_entry.wake.is_set()


def test_cancel_pending_resolves_waiting_future() -> None:
    intent = _intent("Up")
    manual_entry.submit(intent)

    manual_entry.cancel_pending("Bot stopped — order not placed")

    outcome = intent.future.result(timeout=0)
    assert outcome == EntryOutcome(
        "blocked", "Bot stopped — order not placed", mode="paper", side="Up"
    )
    assert not manual_entry.has_pending()


def test_cancel_pending_tolerates_a_cancelled_future() -> None:
    intent = _intent("Up")
    manual_entry.submit(intent)
    intent.future.cancel()
    manual_entry.cancel_pending("Bot stopped — order not placed")  # must not raise
    assert not manual_entry.has_pending()


# ---------------------------------------------------------------------------
# Controller: request_manual_entry
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_runner(monkeypatch: pytest.MonkeyPatch):
    """An alive runner thread + open stop event, without a real loop."""
    release = threading.Event()
    runner = threading.Thread(target=release.wait, daemon=True)
    runner.start()
    monkeypatch.setattr(controller, "_runner_thread", runner)
    monkeypatch.setattr(controller, "_stop_event", threading.Event())
    monkeypatch.setattr(controller, "_desired_running", True)
    monkeypatch.setattr(controller, "_mode_cache", "paper")
    yield
    release.set()
    runner.join(timeout=2)


def _consumer(resolve: bool = True) -> threading.Thread:
    """A stand-in runner: take the click and answer it from another thread."""

    def run() -> None:
        if not manual_entry.wake.wait(5):
            return
        intent = manual_entry.take()
        if intent is None or not intent.future.set_running_or_notify_cancel():
            return
        if resolve:
            manual_entry.resolve(
                intent,
                EntryOutcome(
                    "filled", "Paper BUY Up 5.00 sh @ 0.570 ($2.85)", mode=intent.mode,
                    side=intent.side, price=0.57, shares=5.0, notional_usd=2.85,
                ),
            )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


@pytest.mark.asyncio
async def test_request_refused_when_bot_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(controller, "_runner_thread", None)
    monkeypatch.setattr(controller, "_stop_event", None)
    monkeypatch.setattr(controller, "_desired_running", False)

    outcome = await controller.request_manual_entry("Up", window_slug=SLUG, seen_ask=0.57)

    assert outcome.status == "blocked"
    assert "Start" in outcome.detail
    assert not manual_entry.has_pending()


@pytest.mark.asyncio
async def test_request_refused_when_stop_requested(
    fake_runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller._stop_event.set()

    outcome = await controller.request_manual_entry("Up", window_slug=SLUG, seen_ask=0.57)

    assert outcome.status == "blocked" and "Start" in outcome.detail


@pytest.mark.asyncio
async def test_request_rejects_unknown_side(fake_runner) -> None:
    outcome = await controller.request_manual_entry("Sideways", window_slug=SLUG, seen_ask=None)
    assert outcome.status == "blocked" and "Up or Down" in outcome.detail
    assert not manual_entry.has_pending()


@pytest.mark.asyncio
async def test_request_returns_runner_outcome(fake_runner) -> None:
    consumer = _consumer()

    outcome = await controller.request_manual_entry(
        "Up", window_slug=SLUG, seen_ask=0.57, timeout=5.0
    )

    consumer.join(timeout=2)
    assert outcome.status == "filled"
    assert outcome.side == "Up" and outcome.mode == "paper"
    assert outcome.shares == 5.0


@pytest.mark.asyncio
async def test_request_refused_while_another_click_waits(fake_runner) -> None:
    manual_entry.submit(_intent("Down"))

    outcome = await controller.request_manual_entry("Up", window_slug=SLUG, seen_ask=0.57)

    assert outcome.status == "blocked" and "already in progress" in outcome.detail


@pytest.mark.asyncio
async def test_request_timeout_clears_slot(fake_runner) -> None:
    outcome = await controller.request_manual_entry(
        "Up", window_slug=SLUG, seen_ask=0.57, timeout=0.2
    )

    assert outcome.status == "blocked"
    assert "in time" in outcome.detail
    assert not manual_entry.has_pending()
    assert not manual_entry.wake.is_set()


@pytest.mark.asyncio
async def test_request_reports_pending_when_runner_already_started(fake_runner) -> None:
    consumer = _consumer(resolve=False)

    outcome = await controller.request_manual_entry(
        "Up", window_slug=SLUG, seen_ask=0.57, timeout=0.5
    )

    consumer.join(timeout=2)
    assert outcome.status == "pending"
    assert "activity feed" in outcome.detail


@pytest.mark.asyncio
async def test_cancel_pending_answers_a_waiting_request(fake_runner) -> None:
    def stopper() -> None:
        if manual_entry.wake.wait(5):
            manual_entry.cancel_pending("Bot stopped — order not placed")

    thread = threading.Thread(target=stopper, daemon=True)
    thread.start()

    outcome = await controller.request_manual_entry(
        "Up", window_slug=SLUG, seen_ask=0.57, timeout=5.0
    )

    thread.join(timeout=2)
    assert outcome.status == "blocked"
    assert outcome.detail == "Bot stopped — order not placed"


@pytest.mark.asyncio
async def test_request_stop_cancels_pending_click(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(controller, "_runner_thread", None)
    monkeypatch.setattr(controller, "_stop_event", None)
    monkeypatch.setattr(controller, "_desired_running", True)
    intent = _intent("Up")
    manual_entry.submit(intent)

    await controller.request_stop()

    outcome = intent.future.result(timeout=0)
    assert outcome.status == "blocked" and "Bot stopped" in outcome.detail
    assert not manual_entry.has_pending()


@pytest.mark.asyncio
async def test_end_to_end_click_through_tick(
    bot_db, fake_runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dashboard request on one loop, runner tick on another thread's loop."""
    await _knobs.set("execution_strategy", "market")
    _stub_tick(monkeypatch, _snapshot())

    def runner_tick() -> None:
        if manual_entry.wake.wait(5):
            asyncio.run(paper.paper_tick_once())

    thread = threading.Thread(target=runner_tick, daemon=True)
    thread.start()

    outcome = await controller.request_manual_entry(
        "Down", window_slug=SLUG, seen_ask=0.43, timeout=10.0
    )

    thread.join(timeout=5)
    assert outcome.status == "filled"
    assert outcome.price == pytest.approx(0.43)
    rows = await _rows(bot_db)
    assert [(r["side"], r["entry_source"], r["mode"]) for r in rows] == [
        ("Down", "market", "paper")
    ]


# ---------------------------------------------------------------------------
# Knob
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execution_strategy_knob_defaults_and_validates(bot_db) -> None:
    assert _knobs.KNOBS["execution_strategy"].default == "model"
    assert await _knobs.get("execution_strategy") == "model"
    with pytest.raises(ValueError):
        await _knobs.set("execution_strategy", "yolo")
    assert await _knobs.set("execution_strategy", "market") == "market"
    assert await _knobs.get("execution_strategy") == "market"
