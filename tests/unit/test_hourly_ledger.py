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


# Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried
@pytest.mark.asyncio
async def test_set_action_with_expected_action_only_replaces_that_action(test_db) -> None:
    await _record()
    await ledger.set_action(SLUG, "hourly_mean_reversion", "ENTERED", position_id=7)
    await ledger.set_action(SLUG, "hourly_mean_reversion", "UNCERTAIN:x", expected_action="SUBMITTING")
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"] == "ENTERED"
    await ledger.set_action(SLUG, "hourly_mean_reversion", "UNCERTAIN:x", expected_action="ENTERED")
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"] == "UNCERTAIN:x"


@pytest.mark.asyncio
async def test_positions_table_has_strategy_columns(test_db) -> None:
    async with _db.connect() as conn:
        cur = await conn.execute("PRAGMA table_info(paper_positions)")
        cols = {r["name"] for r in await cur.fetchall()}
    assert {"strategy_id", "market_timeframe", "window_start_ts"} <= cols
