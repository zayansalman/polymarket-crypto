"""Hourly decision record: one row per (hour, strategy), actions, and settlement of every hour."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot.hourly import ledger

H = 1_789_326_000
SLUG = "bitcoin-up-or-down-september-13-2026-3pm-et"


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    return _db


async def _record(strategy_id: str = "hourly_mean_reversion", side: str | None = "Down",
                  late: bool = False, start: int = H, slug: str = SLUG) -> bool:
    return await ledger.record_decision(
        strategy_id=strategy_id, window_slug=slug, window_start_ts=start, side=side,
        reason="enter Down: test", signal={"spot_fz": 2.5}, factors={"weekend": False},
        up_bid=0.49, up_ask=0.50, down_bid=0.50, down_ask=0.51, hour_open=77000.0,
        mode="paper", late=late,
    )


@pytest.mark.asyncio
async def test_record_is_idempotent_per_hour_and_strategy(test_db) -> None:
    assert await _record() is True
    assert await _record() is False
    assert await _record(strategy_id="kronos_btc_finetune", side=None) is True
    row = await ledger.get_decision(SLUG, "hourly_mean_reversion")
    assert row["action"] == "PENDING" and row["decision_side"] == "Down"
    assert json.loads(row["signal_json"]) == {"spot_fz": 2.5}
    assert (await ledger.get_decision(SLUG, "kronos_btc_finetune"))["action"] == "NO_SIGNAL"


@pytest.mark.asyncio
async def test_late_signal_is_recorded_missed(test_db) -> None:
    await _record(late=True)
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"] == "MISSED"


@pytest.mark.asyncio
async def test_set_action_and_settle_every_strategy_row(test_db) -> None:
    await _record()
    await _record(strategy_id="kronos_btc_finetune", side=None)
    await ledger.set_action(SLUG, "hourly_mean_reversion", "ENTERED", position_id=7)
    assert await ledger.unsettled_windows(H + 3599) == []
    assert await ledger.unsettled_windows(H + 3600) == [H]
    await ledger.settle_window(H, 77000.0, 76900.0)
    for sid in ("hourly_mean_reversion", "kronos_btc_finetune"):
        row = await ledger.get_decision(SLUG, sid)
        assert row["outcome_side"] == "Down" and row["hour_close"] == 76900.0
        assert row["settled_at"] is not None
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["position_id"] == 7
    assert await ledger.unsettled_windows(H + 7200) == []


@pytest.mark.asyncio
async def test_positions_table_has_strategy_columns(test_db) -> None:
    async with _db.connect() as conn:
        cur = await conn.execute("PRAGMA table_info(paper_positions)")
        cols = {r["name"] for r in await cur.fetchall()}
    assert {"strategy_id", "market_timeframe", "window_start_ts"} <= cols


# Claude, 2026-09-15, branch-review finding pending-row-never-finalized
@pytest.mark.asyncio
async def test_pending_rows_past_the_deadline_are_finalized_for_every_hour(test_db) -> None:
    prev_slug = "bitcoin-up-or-down-september-13-2026-2pm-et"
    await _record(start=H - 3600, slug=prev_slug)
    await _record(strategy_id="kronos_btc_finetune", start=H - 3600, slug=prev_slug)
    await _record()
    await _record(strategy_id="kronos_btc_finetune", side=None)
    async with _db.connect() as conn:  # this strategy's position for the previous hour
        cur = await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, strategy_id, market_timeframe, window_start_ts, mode)"
            " VALUES ('x', ?, 'Down', 'open', 0.5, 2.5, 5, 'kronos_btc_finetune', '1h', ?, 'live')",
            (prev_slug, H - 3600),
        )
        position_id = cur.lastrowid
        await conn.commit()

    assert await ledger.finalize_pending_past_deadline(H + 120, 120) == 2  # H itself: not yet
    prev = await ledger.get_decision(prev_slug, "hourly_mean_reversion")
    assert prev["action"] == "MISSED" and prev["position_id"] is None
    kronos = await ledger.get_decision(prev_slug, "kronos_btc_finetune")
    assert (kronos["action"], kronos["position_id"]) == ("ENTERED", position_id)
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"] == "PENDING"

    assert await ledger.finalize_pending_past_deadline(H + 121, 120) == 1
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"] == "MISSED"
    assert (await ledger.get_decision(SLUG, "kronos_btc_finetune"))["action"] == "NO_SIGNAL"
    assert await ledger.finalize_pending_past_deadline(H + 7200, 120) == 0
