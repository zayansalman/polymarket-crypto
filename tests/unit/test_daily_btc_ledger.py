"""Daily BTC decision record: one row per (window, strategy, mode), actions, finalization, settlement."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot.daily_btc import ledger
from polymarket_bot.daily_btc.market import DayMarket, window_for

WINDOW = window_for(date(2026, 9, 17))  # Sep 16 noon ET -> Sep 17 noon ET
MARKET = DayMarket("bitcoin-up-or-down-on-september-17-2026", "Bitcoin Up or Down on September 17?",
                   WINDOW, "111", "222", 0.07, 1.0)
PREV_WINDOW = window_for(date(2026, 9, 16))  # Sep 15 noon ET -> Sep 16 noon ET
PREV_MARKET = DayMarket("bitcoin-up-or-down-on-september-16-2026",
                        "Bitcoin Up or Down on September 16?", PREV_WINDOW, "333", "444", 0.07, 1.0)
SID = "tsinghua_kronos_btc_24h"
SECOND_SID = "second_daily_btc_strategy_in_this_test"
UNFINISHED = "UNCERTAIN:entry attempt did not finish"


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    return _db


async def _record(mode: str = "paper", side: str | None = "Up", *, late: bool = False,
                  available: bool = True, market: DayMarket = MARKET,
                  strategy_id: str = SID) -> bool:
    return await ledger.record_decision(
        strategy_id=strategy_id, mode=mode, market=market, side=side, reason="enter Up: test",
        signal={"p_up": 0.6}, up_bid=0.5, up_ask=0.52, down_bid=0.48, down_ask=0.5,
        late=late, available=available,
    )


async def _insert_position(*, strategy_id: str, mode: str, reference_ts: int,
                           timeframe: str = "1d", state: str = "open",
                           exit_reason: str | None = None) -> int:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, strategy_id, market_timeframe, window_start_ts, mode,"
            " exit_reason) VALUES ('x', 'slug', 'Up', ?, 0.52, 2.6, 5, ?, ?, ?, ?, ?)",
            (state, strategy_id, timeframe, reference_ts, mode, exit_reason),
        )
        await conn.commit()
        return int(cur.lastrowid)


@pytest.mark.asyncio
async def test_one_row_per_window_strategy_and_mode(test_db) -> None:
    assert await _record() is True
    assert await _record() is False
    assert await _record(mode="live") is True
    paper = await ledger.get_decision(WINDOW.reference_ts, SID, mode="paper")
    assert paper["action"] == ledger.PENDING and paper["decision_side"] == "Up"
    assert paper["window_slug"] == MARKET.slug and paper["settle_ts"] == WINDOW.settle_ts
    assert json.loads(paper["signal_json"]) == {"p_up": 0.6}
    assert (paper["up_bid"], paper["up_ask"], paper["down_bid"], paper["down_ask"]) == (
        0.5, 0.52, 0.48, 0.5)
    assert (await ledger.get_decision(WINDOW.reference_ts, SID, mode="live"))["mode"] == "live"
    assert await ledger.get_decision(PREV_WINDOW.reference_ts, SID, mode="paper") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("side", "late", "available", "action"), [
    ("Up", False, True, "PENDING"), ("Up", True, True, "MISSED"),
    (None, False, True, "NO_SIGNAL"), (None, True, True, "MISSED"),
    (None, False, False, "UNAVAILABLE"),
])
async def test_action_mapping(test_db, side, late, available, action) -> None:
    await _record(side=side, late=late, available=available)
    assert (await ledger.get_decision(WINDOW.reference_ts, SID, mode="paper"))["action"] == action


@pytest.mark.asyncio
async def test_set_action_with_expected_action_only_replaces_that_action(test_db) -> None:
    await _record()
    await _record(mode="live")
    ref = WINDOW.reference_ts
    assert await ledger.set_action(ref, SID, "SUBMITTING", mode="paper",
                                   expected_action="PENDING") is True
    assert await ledger.set_action(ref, SID, "MISSED", mode="paper",
                                   expected_action="PENDING") is False
    assert await ledger.set_action(ref, SID, "ENTERED", 7, mode="paper") is True
    row = await ledger.get_decision(ref, SID, mode="paper")
    assert (row["action"], row["position_id"]) == ("ENTERED", 7)
    assert (await ledger.get_decision(ref, SID, mode="live"))["action"] == "PENDING"
    assert await ledger.set_action(PREV_WINDOW.reference_ts, SID, "MISSED", mode="paper") is False


@pytest.mark.asyncio
async def test_settlement_of_every_row_in_the_window(test_db) -> None:
    await _record()
    await _record(mode="live", side=None)
    assert await ledger.unsettled_windows(WINDOW.settle_ts + 119, 120) == []
    assert await ledger.unsettled_windows(WINDOW.settle_ts + 120, 120) == [
        (WINDOW.reference_ts, WINDOW.settle_ts)]
    await ledger.settle_window(WINDOW.reference_ts, 100.0, 100.0, "tie")
    for mode in ("paper", "live"):
        row = await ledger.get_decision(WINDOW.reference_ts, SID, mode=mode)
        assert (row["reference_close"], row["settle_close"], row["outcome"]) == (
            100.0, 100.0, "tie")
        assert row["settled_at"] is not None
    assert await ledger.unsettled_windows(WINDOW.settle_ts + 999, 120) == []


# Same rules as the hourly record's finalize_ended_hours (Claude, 2026-09-15, branch-review
# finding pending-row-never-finalized), for noon-ET windows (Claude, 2026-09-16).
@pytest.mark.asyncio
async def test_open_decisions_of_ended_windows_are_finalized_per_strategy_and_mode(
    test_db,
) -> None:
    prev = PREV_WINDOW.reference_ts
    await _record(market=PREV_MARKET)
    await _record(market=PREV_MARKET, strategy_id=SECOND_SID)
    await _record(market=PREV_MARKET, strategy_id=SECOND_SID, mode="live")
    await _record()
    await _record(strategy_id=SECOND_SID, side=None)
    position_id = await _insert_position(strategy_id=SECOND_SID, mode="live", reference_ts=prev)
    await ledger.set_action(prev, SID, "SUBMITTING", mode="paper")

    counts = await ledger.finalize_ended_windows(WINDOW.reference_ts, UNFINISHED)

    assert counts == {"entered": 1, "unfinished": 1, "missed": 1}
    row = await ledger.get_decision(prev, SID, mode="paper")
    assert (row["action"], row["position_id"]) == (UNFINISHED, None)
    row = await ledger.get_decision(prev, SECOND_SID, mode="live")
    assert (row["action"], row["position_id"]) == ("ENTERED", position_id)
    row = await ledger.get_decision(prev, SECOND_SID, mode="paper")  # live position: not its own
    assert (row["action"], row["position_id"]) == ("MISSED", None)
    # The current window is left to the engine's entry step.
    assert (await ledger.get_decision(WINDOW.reference_ts, SID, mode="paper"))["action"] == (
        "PENDING")
    assert (await ledger.get_decision(WINDOW.reference_ts, SECOND_SID, mode="paper"))[
        "action"] == "NO_SIGNAL"
    assert await ledger.finalize_ended_windows(WINDOW.reference_ts, "x") == {
        "entered": 0, "unfinished": 0, "missed": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize(("timeframe", "state", "exit_reason", "action"), [
    ("1d", "closed", "STOP_REQUEST", "ENTERED"),  # sold at Stop: the entry went through
    ("1d", "closed", "RECONCILED_NO_LIVE_TRACE", "MISSED"),  # boot found no order placed
    ("1d", "closed", "RECONCILED_UNFILLED", "MISSED"),  # boot found the order never filled
    ("1h", "open", None, "MISSED"),  # an hourly position is not this window's
])
async def test_ended_window_links_only_its_own_daily_position(
    test_db, timeframe, state, exit_reason, action
) -> None:
    prev = PREV_WINDOW.reference_ts
    await _record(market=PREV_MARKET)
    position_id = await _insert_position(strategy_id=SID, mode="paper", reference_ts=prev,
                                         timeframe=timeframe, state=state,
                                         exit_reason=exit_reason)
    await ledger.finalize_ended_windows(WINDOW.reference_ts, UNFINISHED)
    row = await ledger.get_decision(prev, SID, mode="paper")
    linked = position_id if action == "ENTERED" else None
    assert (row["action"], row["position_id"]) == (action, linked)
