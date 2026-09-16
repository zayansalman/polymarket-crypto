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


async def _record(strategy_id: str = "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", side: str | None = "Down",
                  late: bool = False, start: int = H, slug: str = SLUG,
                  mode: str = "paper") -> bool:
    return await ledger.record_decision(
        strategy_id=strategy_id, window_slug=slug, window_start_ts=start, side=side,
        reason="enter Down: test", signal={"spot_fz": 2.5}, factors={"weekend": False},
        up_bid=0.49, up_ask=0.50, down_bid=0.50, down_ask=0.51, hour_open=77000.0,
        mode=mode, late=late,
    )


@pytest.mark.asyncio
async def test_record_is_idempotent_per_hour_and_strategy(test_db) -> None:
    assert await _record() is True
    assert await _record() is False
    assert await _record(strategy_id="kronos_lc2004_btcusdt_1h_finetune_up_chance_vs_polymarket_price", side=None) is True
    row = await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper")
    assert row["action"] == "PENDING" and row["decision_side"] == "Down"
    assert json.loads(row["signal_json"]) == {"spot_fz": 2.5}
    assert (await ledger.get_decision(H, "kronos_lc2004_btcusdt_1h_finetune_up_chance_vs_polymarket_price", mode="paper"))["action"] == "NO_SIGNAL"


@pytest.mark.asyncio
async def test_late_signal_is_recorded_missed(test_db) -> None:
    await _record(late=True)
    assert (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper"))["action"] == "MISSED"


@pytest.mark.asyncio
async def test_set_action_and_settle_every_strategy_row(test_db) -> None:
    await _record()
    await _record(strategy_id="kronos_lc2004_btcusdt_1h_finetune_up_chance_vs_polymarket_price", side=None)
    await ledger.set_action(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", "ENTERED", position_id=7, mode="paper")
    assert await ledger.unsettled_windows(H + 3599) == []
    assert await ledger.unsettled_windows(H + 3600) == [H]
    await ledger.settle_window(H, 77000.0, 76900.0)
    for sid in ("btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", "kronos_lc2004_btcusdt_1h_finetune_up_chance_vs_polymarket_price"):
        row = await ledger.get_decision(H, sid, mode="paper")
        assert row["outcome_side"] == "Down" and row["hour_close"] == 76900.0
        assert row["settled_at"] is not None
    assert (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper"))["position_id"] == 7
    assert await ledger.unsettled_windows(H + 7200) == []


# Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried
@pytest.mark.asyncio
async def test_set_action_with_expected_action_only_replaces_that_action(test_db) -> None:
    await _record()
    sid = "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal"
    await ledger.set_action(H, sid, "ENTERED", position_id=7, mode="paper")
    await ledger.set_action(H, sid, "UNCERTAIN:x", mode="paper", expected_action="SUBMITTING")
    assert (await ledger.get_decision(H, sid, mode="paper"))["action"] == "ENTERED"
    await ledger.set_action(H, sid, "UNCERTAIN:x", mode="paper", expected_action="ENTERED")
    assert (await ledger.get_decision(H, sid, mode="paper"))["action"] == "UNCERTAIN:x"


@pytest.mark.asyncio
async def test_positions_table_has_strategy_columns(test_db) -> None:
    async with _db.connect() as conn:
        cur = await conn.execute("PRAGMA table_info(paper_positions)")
        cols = {r["name"] for r in await cur.fetchall()}
    assert {"strategy_id", "market_timeframe", "window_start_ts"} <= cols


@pytest.mark.asyncio
async def test_two_hours_with_the_same_slug_keep_their_own_rows(test_db) -> None:
    # Claude, 2026-09-15, branch-review finding dst-fallback-slug-collision: on 2026-11-01 the
    # 05:00Z and 06:00Z hours are both "1am ET", so rows are keyed by the UTC hour start.
    first, second = 1_793_509_200, 1_793_512_800
    shared = "bitcoin-up-or-down-november-1-2026-1am-et"
    assert await _record(start=first, slug=shared) is True
    assert await _record(start=second, slug=shared, side="Up") is True
    await ledger.set_action(second, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", "ENTERED", position_id=9, mode="paper")
    first_row = await ledger.get_decision(first, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper")
    second_row = await ledger.get_decision(second, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper")
    assert (first_row["decision_side"], first_row["action"], first_row["position_id"]) == (
        "Down", "PENDING", None)
    assert (second_row["decision_side"], second_row["action"], second_row["position_id"]) == (
        "Up", "ENTERED", 9)


# Claude, 2026-09-15, branch-review finding decision-row-shared-across-modes: a paper run and a
# live run in the same hour each record and act on their own row.
@pytest.mark.asyncio
async def test_paper_and_live_keep_their_own_row_for_the_same_hour(test_db) -> None:
    assert await _record(mode="paper") is True
    assert await _record(mode="live") is True
    assert await _record(mode="live") is False
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT mode, action FROM hourly_strategy_context ORDER BY mode")
        assert [tuple(r) for r in await cur.fetchall()] == [
            ("live", "PENDING"), ("paper", "PENDING")]
    await ledger.set_action(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal",
                            "BLOCKED:daily loss halt: paper realized -50", mode="paper")
    await ledger.set_action(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", "ENTERED", position_id=4, mode="live")
    paper_row = await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper")
    live_row = await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="live")
    assert (paper_row["mode"], paper_row["action"], paper_row["position_id"]) == (
        "paper", "BLOCKED:daily loss halt: paper realized -50", None)
    assert (live_row["mode"], live_row["action"], live_row["position_id"]) == (
        "live", "ENTERED", 4)
    await ledger.settle_window(H, 77000.0, 76900.0)
    for mode in ("paper", "live"):
        row = await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode=mode)
        assert row["outcome_side"] == "Down" and row["settled_at"] is not None


@pytest.mark.asyncio
async def test_db_made_with_the_older_unique_indexes_accepts_a_row_per_mode(test_db) -> None:
    # Claude, 2026-09-15, branch-review finding decision-row-shared-across-modes: a DB created by
    # an earlier build of this branch still has a two-column unique index; init_db replaces it.
    async with _db.connect() as conn:
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_hourly_context_window_strategy "
            "ON hourly_strategy_context(window_slug, strategy_id)")
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_hourly_context_start_strategy "
            "ON hourly_strategy_context(window_start_ts, strategy_id)")
        await conn.commit()
    await _db.init_db()
    assert await _record(mode="paper") is True
    assert await _record(mode="live") is True


# Claude, 2026-09-15, branch-review finding pending-row-never-finalized
@pytest.mark.asyncio
async def test_open_decisions_of_ended_hours_are_finalized_per_strategy_and_mode(test_db) -> None:
    prev_slug = "bitcoin-up-or-down-september-13-2026-2pm-et"
    kronos = "kronos_lc2004_btcusdt_1h_finetune_up_chance_vs_polymarket_price"
    await _record(start=H - 3600, slug=prev_slug)
    await _record(strategy_id=kronos, start=H - 3600, slug=prev_slug)
    await _record(strategy_id=kronos, start=H - 3600, slug=prev_slug, mode="live")
    await _record()
    await _record(strategy_id=kronos, side=None)
    async with _db.connect() as conn:  # the Kronos strategy's live position for the previous hour
        cur = await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, strategy_id, market_timeframe, window_start_ts, mode)"
            " VALUES ('x', ?, 'Down', 'open', 0.5, 2.5, 5, ?, '1h', ?, 'live')",
            (prev_slug, kronos, H - 3600),
        )
        position_id = cur.lastrowid
        await conn.commit()
    # A live position only counts with its order in the journal (Claude, 2026-09-16, review
    # of the merged branch).
    await _db.journal_live_order(
        intent="ENTRY", side="BUY", status="SUBMITTED", window_slug=prev_slug, token_id="t",
        price=0.5, size=5.0, clob_order_id="0xFILLED", strategy_id=kronos)
    spot_push = "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal"
    await ledger.set_action(H - 3600, spot_push, "SUBMITTING", mode="paper")

    counts = await ledger.finalize_ended_hours(H, "UNCERTAIN:entry attempt did not finish")
    assert counts == {"entered": 1, "unfinished": 1, "missed": 1}
    prev = await ledger.get_decision(H - 3600, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper")
    assert (prev["action"], prev["position_id"]) == ("UNCERTAIN:entry attempt did not finish", None)
    live = await ledger.get_decision(H - 3600, kronos, mode="live")
    assert (live["action"], live["position_id"]) == ("ENTERED", position_id)
    paper_row = await ledger.get_decision(H - 3600, kronos, mode="paper")  # live position: not its own
    assert (paper_row["action"], paper_row["position_id"]) == ("MISSED", None)
    # The current hour is left alone.
    assert (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper"))["action"] == "PENDING"
    assert (await ledger.get_decision(H, kronos, mode="paper"))["action"] == "NO_SIGNAL"
    assert await ledger.finalize_ended_hours(H, "x") == {"entered": 0, "unfinished": 0, "missed": 0}


@pytest.mark.asyncio
async def test_ended_hour_ignores_closed_rows_and_live_rows_without_a_journaled_order(test_db) -> None:
    """Claude, 2026-09-16, review of the merged branch: only a still-open position counts, and a
    live one only with its order in the journal."""
    spot_push = "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal"
    await _record(start=H - 3600, mode="live")
    await _record(start=H - 3600, mode="paper")
    await ledger.set_action(H - 3600, spot_push, "SUBMITTING", mode="live")
    await ledger.set_action(H - 3600, spot_push, "SUBMITTING", mode="paper")
    async with _db.connect() as conn:
        for mode, state in (("live", "open"), ("paper", "closed")):  # untraced live; sold paper
            await conn.execute(
                "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
                " notional_usd, shares, strategy_id, market_timeframe, window_start_ts, mode)"
                " VALUES ('x', ?, 'Down', ?, 0.5, 2.5, 5, ?, '1h', ?, ?)",
                (SLUG, state, spot_push, H - 3600, mode),
            )
        await conn.commit()
    counts = await ledger.finalize_ended_hours(H, "UNCERTAIN:entry attempt did not finish")
    assert counts == {"entered": 0, "unfinished": 2, "missed": 0}
