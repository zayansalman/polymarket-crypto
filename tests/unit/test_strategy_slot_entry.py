"""Shared strategy-slot entry: the hourly entry steps work for a daily window and any decision record."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot import paper
from polymarket_bot import strategy_slot_entry as entry
from polymarket_exec.execution.live import LiveOrderResult

START = 1_789_574_400  # 2026-09-16 16:00 UTC: noon ET, the start of the Sep 17 daily window
SID = "tsinghua_kronos_btc_24h"
SLUG = "bitcoin-up-or-down-on-september-17-2026"
WINDOW = entry.SlotWindow(SID, "1d", START)
UNFINISHED = "UNCERTAIN:entry attempt did not finish"


class _Decision:
    """A decision record that keeps every action write as (action, position_id, expected_action)."""

    def __init__(self) -> None:
        self.actions: list[tuple[str, int | None, str | None]] = []

    async def reason(self) -> str | None:
        return "enter Up: test"

    async def set_action(self, action, position_id=None, *, expected_action=None) -> bool:
        self.actions.append((action, position_id, expected_action))
        return True


def _snapshot() -> paper.PaperSnapshot:
    return paper.PaperSnapshot(
        created_at="2026-09-16T16:00:10+00:00", window_slug=SLUG,
        market_question="q", remaining_seconds=86_000, spot_price=100.0, reference_price=100.0,
        sigma_per_second=0.0, market_up_price=0.52, market_down_price=0.49, fair_up_prob=0.5,
        edge=0.0, signal_side=None, confidence=0.0, notional_usd=0.0, reason="",
        feed_source="quotes=clob", up_token_id="U", down_token_id="D",
        up_best_bid=0.51, up_best_ask=0.52, up_bid_size=100.0, up_ask_size=100.0,
        down_best_bid=0.48, down_best_ask=0.49, down_bid_size=100.0, down_ask_size=100.0,
    )


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    monkeypatch.setattr(paper, "_live_executor", None)
    monkeypatch.setattr(paper, "_risk_gate", None)
    monkeypatch.setattr(entry, "notify", AsyncMock())
    return _db


def _live_account(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    slot = MagicMock()
    slot.tracks_position = False
    slot.resync_flat = AsyncMock(return_value=False)
    slot.submit_entry = AsyncMock(return_value=LiveOrderResult(
        ok=True, status="SUBMITTED", order_id="0xE", price=0.52, size=5.0, notional_usd=2.6))
    account = MagicMock()
    account.slot_executor = MagicMock(return_value=slot)
    monkeypatch.setattr(paper, "_live_executor", account)
    return account, slot


async def _advance(action: str, *, now: int, decision: _Decision, mode: str = "paper",
                   allow_entries: bool = True) -> None:
    await entry.advance(_snapshot(), WINDOW, action=action, side="Up", now=now, deadline_s=300,
                        allow_entries=allow_entries, mode=mode, decision=decision)


async def _insert_row(*, mode: str = "paper", timeframe: str = "1d", start: int = START) -> int:
    return await entry.insert_row(_snapshot(), entry.SlotWindow(SID, timeframe, start),
                                  side="Up", price=0.52, notional=2.6, shares=5.0, reason="r",
                                  mode=mode)


@pytest.mark.asyncio
async def test_paper_entry_for_a_daily_window_records_the_attempt_then_entered(test_db) -> None:
    decision = _Decision()
    await _advance(entry.PENDING, now=START + 10, decision=decision)
    row = await entry.open_row_for(SID, "paper")
    assert decision.actions == [(entry.SUBMITTING, None, None),
                                (entry.ENTERED, row["position_id"], None)]
    assert (row["market_timeframe"], row["window_start_ts"], row["window_slug"], row["side"],
            row["entry_price"], row["shares"], row["entry_reason"]) == (
        "1d", START, SLUG, "Up", 0.52, 5.0, "enter Up: test")


@pytest.mark.asyncio
async def test_past_deadline_is_missed_and_submitting_never_retries(test_db) -> None:
    late = _Decision()
    await _advance(entry.PENDING, now=START + 301, decision=late)
    assert late.actions == [(entry.MISSED, None, None)]
    stuck = _Decision()
    await _advance(entry.SUBMITTING, now=START + 20, decision=stuck)
    assert stuck.actions == [(UNFINISHED, None, entry.SUBMITTING)]
    assert entry.UNFINISHED_ATTEMPT == UNFINISHED
    entry.notify.assert_awaited_once()
    assert entry.notify.await_args.args[0] == "entry_attempt_unfinished"
    assert await entry.open_row_for(SID, "paper") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["PENDING", "SUBMITTING"])
async def test_still_open_same_window_position_is_linked_not_missed(test_db, action) -> None:
    position_id = await _insert_row()
    decision = _Decision()
    await _advance(action, now=START + 400, decision=decision)
    assert decision.actions == [(entry.ENTERED, position_id, action)]
    entry.notify.assert_not_awaited()


# Claude, 2026-09-15, branch-review finding crash-after-entry-marks-missed: live links the
# window only while the strategy's slot tracks the entry.
@pytest.mark.asyncio
@pytest.mark.parametrize("tracks", [True, False])
async def test_live_link_needs_the_strategy_slot_to_track_the_entry(
    test_db, monkeypatch, tracks
) -> None:
    account, slot = _live_account(monkeypatch)
    slot.tracks_position = tracks
    position_id = await _insert_row(mode="live")
    decision = _Decision()
    await _advance(entry.SUBMITTING, now=START + 20, decision=decision, mode="live")
    account.slot_executor.assert_called_with(SID)
    if tracks:
        assert decision.actions == [(entry.ENTERED, position_id, entry.SUBMITTING)]
    else:
        assert decision.actions == [(UNFINISHED, None, entry.SUBMITTING)]
    slot.submit_entry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("timeframe", "start"), [("1d", START - 86_400), ("1h", START)])
async def test_open_row_of_another_window_holds_the_slot_and_records_nothing(
    test_db, timeframe, start
) -> None:
    await _insert_row(timeframe=timeframe, start=start)
    decision = _Decision()
    await _advance(entry.PENDING, now=START + 10, decision=decision)
    assert decision.actions == []
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT COUNT(*) AS n FROM paper_positions")
        assert (await cur.fetchone())["n"] == 1


# Same record as the hourly kill-switch tests: entries held leave the window PENDING, and
# the deadline then records MISSED.
@pytest.mark.asyncio
async def test_entries_held_leave_the_window_pending_until_the_deadline(test_db) -> None:
    held = _Decision()
    await _advance(entry.PENDING, now=START + 10, decision=held, allow_entries=False)
    assert held.actions == []
    await _advance(entry.PENDING, now=START + 301, decision=held, allow_entries=False)
    assert held.actions == [(entry.MISSED, None, None)]
    assert await entry.open_row_for(SID, "paper") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [
    "NO_SIGNAL", "UNAVAILABLE", "MISSED", "ENTERED", "BLOCKED:x", "UNCERTAIN:x"])
async def test_finished_actions_are_left_alone(test_db, action) -> None:
    decision = _Decision()
    await _advance(action, now=START + 10, decision=decision)
    assert decision.actions == []
    assert await entry.open_row_for(SID, "paper") is None


@pytest.mark.asyncio
async def test_live_entry_for_a_daily_window_goes_through_the_strategy_slot(
    test_db, monkeypatch
) -> None:
    _, slot = _live_account(monkeypatch)
    decision = _Decision()
    await _advance(entry.PENDING, now=START + 10, decision=decision, mode="live")
    slot.resync_flat.assert_awaited_once()
    slot.submit_entry.assert_awaited_once_with(
        token_id="U", side_price=0.52, notional_usd=pytest.approx(2.6), window_slug=SLUG)
    row = await entry.open_row_for(SID, "live")
    assert (row["market_timeframe"], row["window_start_ts"], row["mode"]) == ("1d", START, "live")
    assert decision.actions == [(entry.SUBMITTING, None, None),
                                (entry.ENTERED, row["position_id"], None)]


# Claude, 2026-09-17, review of the strategy_slot_entry extraction: a runner can resume a tick
# after the operator switched mode (an abandoned paper runner once LIVE is running). The
# tick's mode then differs from the live executor's, and the step does nothing: no order, no
# position row, no decision write, no notice. The loop in the new mode handles the window.
async def _position_count() -> int:
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT COUNT(*) AS n FROM paper_positions")
        return int((await cur.fetchone())["n"])


def _mode_changed_warning(tick_mode: str, executor_mode: str) -> tuple[str, dict]:
    return ("strategy_slot_entry.mode_changed_mid_tick",
            {"strategy_id": SID, "timeframe": "1d", "tick_mode": tick_mode,
             "executor_mode": executor_mode, "window_slug": SLUG})


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["PENDING", "SUBMITTING"])
@pytest.mark.parametrize("same_window_row", [False, True])
async def test_paper_tick_with_a_live_executor_present_posts_nothing_and_records_nothing(
    test_db, monkeypatch, action, same_window_row
) -> None:
    account, slot = _live_account(monkeypatch)
    slot.tracks_position = True
    if same_window_row:
        await _insert_row(mode="paper")
    log = MagicMock()
    monkeypatch.setattr(entry, "log", log)
    decision = _Decision()
    await _advance(action, now=START + 10, decision=decision, mode="paper")
    account.slot_executor.assert_not_called()
    slot.resync_flat.assert_not_awaited()
    slot.submit_entry.assert_not_awaited()
    assert decision.actions == []
    assert await _position_count() == (1 if same_window_row else 0)
    entry.notify.assert_not_awaited()
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT COUNT(*) AS n FROM live_orders")
        assert (await cur.fetchone())["n"] == 0
    assert [(c.args[0], c.kwargs) for c in log.warning.call_args_list] == [
        _mode_changed_warning("paper", "live")]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["PENDING", "SUBMITTING"])
@pytest.mark.parametrize("same_window_row", [False, True])
async def test_live_tick_without_a_live_executor_enters_nothing_and_records_nothing(
    test_db, monkeypatch, action, same_window_row
) -> None:
    assert paper._live_executor is None  # the live loop stopped while this tick ran
    if same_window_row:
        await _insert_row(mode="live")
    log = MagicMock()
    monkeypatch.setattr(entry, "log", log)
    decision = _Decision()
    await _advance(action, now=START + 10, decision=decision, mode="live")
    assert decision.actions == []
    assert await _position_count() == (1 if same_window_row else 0)
    assert await entry.open_row_for(SID, "paper") is None
    entry.notify.assert_not_awaited()
    assert [(c.args[0], c.kwargs) for c in log.warning.call_args_list] == [
        _mode_changed_warning("live", "paper")]
