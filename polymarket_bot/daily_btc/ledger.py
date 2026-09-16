"""Daily BTC decision record: one row per (noon-ET window, strategy, mode), actions, settlement.

The operations and their signatures match polymarket_bot.hourly.ledger, so both engines
read and write their records the same way (Claude, 2026-09-16).
"""
from __future__ import annotations

import json
from typing import Any

import db as _db
from polymarket_bot.daily_btc.market import DayMarket

PENDING = "PENDING"
NO_SIGNAL = "NO_SIGNAL"
MISSED = "MISSED"
UNAVAILABLE = "UNAVAILABLE"
ENTERED = "ENTERED"


async def record_decision(
    *,
    strategy_id: str,
    mode: str,
    market: DayMarket,
    side: str | None,
    reason: str,
    signal: dict[str, Any],
    up_bid: float | None,
    up_ask: float | None,
    down_bid: float | None,
    down_ask: float | None,
    late: bool,
    available: bool,
) -> bool:
    """INSERT OR IGNORE the window's decision; True iff this call wrote the row.

    The action is UNAVAILABLE when the forecast could not run; else MISSED when the decision
    came after the entry deadline (the engine then skips the model, so there is no side);
    else NO_SIGNAL without a side; else PENDING (Claude, 2026-09-16).
    """
    if not available:
        action = UNAVAILABLE
    elif late:
        action = MISSED
    elif side is None:
        action = NO_SIGNAL
    else:
        action = PENDING
    async with _db.connect() as conn:
        cur = await conn.execute(
            """
            INSERT OR IGNORE INTO btc_daily_market_decisions(
              created_at, strategy_id, mode, window_slug, reference_ts, settle_ts,
              decision_side, decision_reason, signal_json, up_bid, up_ask, down_bid,
              down_ask, action
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (_db.utc_now_iso(), strategy_id, mode, market.slug, market.window.reference_ts,
             market.window.settle_ts, side, reason, json.dumps(signal, default=str),
             up_bid, up_ask, down_bid, down_ask, action),
        )
        await conn.commit()
        return cur.rowcount == 1


# Rows are looked up by the window's reference time and by mode, as in the hourly record
# (Claude, 2026-09-15, branch-review findings dst-fallback-slug-collision and
# decision-row-shared-across-modes).
async def get_decision(
    reference_ts: int, strategy_id: str, *, mode: str
) -> dict[str, Any] | None:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM btc_daily_market_decisions "
            "WHERE reference_ts = ? AND strategy_id = ? AND mode = ?",
            (reference_ts, strategy_id, mode),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_action(
    reference_ts: int,
    strategy_id: str,
    action: str,
    position_id: int | None = None,
    *,
    mode: str,
    expected_action: str | None = None,
) -> bool:
    """Set the row's action (and position, when given); True iff a row changed.

    ``expected_action`` applies the update only while the row still holds that action, so
    closing out an unfinished attempt never overwrites a result written meanwhile (Claude,
    2026-09-15, branch-review finding hourly-ambiguous-post-error-retried).
    """
    async with _db.connect() as conn:
        cur = await conn.execute(
            "UPDATE btc_daily_market_decisions SET action = ?, "
            "position_id = COALESCE(?, position_id) "
            "WHERE reference_ts = ? AND strategy_id = ? AND mode = ? "
            "AND (? IS NULL OR action = ?)",
            (action[:240], position_id, reference_ts, strategy_id, mode,
             expected_action, expected_action),
        )
        await conn.commit()
        return cur.rowcount > 0


async def finalize_ended_windows(
    current_reference_ts: int, unfinished_action: str
) -> dict[str, int]:
    """Close out decisions still PENDING or SUBMITTING in windows that already ended.

    The same rules as polymarket_bot.hourly.ledger.finalize_ended_hours (Claude, 2026-09-15,
    branch-review finding pending-row-never-finalized), applied to noon-ET windows (Claude,
    2026-09-16). A row left open when no tick ran before its window ended (Stop, crash,
    sleep, failing ticks, strategy switched off) otherwise stays open forever. The current
    window is left to the engine's entry step. For each ended window, per strategy and mode:
    - ENTERED with the position, when this strategy has a 1d position for that window in
      that mode that boot reconciliation did not close as never placed
      (RECONCILED_NO_LIVE_TRACE) or never filled (RECONCILED_UNFILLED);
    - otherwise an attempt that was started (SUBMITTING) becomes ``unfinished_action``
      (branch-review finding hourly-ambiguous-post-error-retried: never assume it failed);
    - otherwise MISSED.
    """
    position_for_row = (
        "SELECT p.position_id FROM paper_positions p "
        "WHERE p.window_start_ts = btc_daily_market_decisions.reference_ts "
        "AND p.strategy_id = btc_daily_market_decisions.strategy_id "
        "AND p.mode = btc_daily_market_decisions.mode "
        "AND p.market_timeframe = '1d' "
        "AND COALESCE(p.exit_reason, '') "
        "NOT IN ('RECONCILED_NO_LIVE_TRACE', 'RECONCILED_UNFILLED')"
    )
    async with _db.connect() as conn:
        entered = await conn.execute(
            "UPDATE btc_daily_market_decisions SET action = 'ENTERED', "
            f"position_id = ({position_for_row} ORDER BY p.position_id LIMIT 1) "
            "WHERE action IN ('PENDING', 'SUBMITTING') AND reference_ts < ? "
            f"AND EXISTS ({position_for_row})",
            (current_reference_ts,),
        )
        unfinished = await conn.execute(
            "UPDATE btc_daily_market_decisions SET action = ? "
            "WHERE action = 'SUBMITTING' AND reference_ts < ?",
            (unfinished_action[:240], current_reference_ts),
        )
        missed = await conn.execute(
            "UPDATE btc_daily_market_decisions SET action = 'MISSED' "
            "WHERE action = 'PENDING' AND reference_ts < ?",
            (current_reference_ts,),
        )
        await conn.commit()
        return {
            "entered": max(entered.rowcount, 0),
            "unfinished": max(unfinished.rowcount, 0),
            "missed": max(missed.rowcount, 0),
        }


async def unsettled_windows(now_ts: int, grace_s: int) -> list[tuple[int, int]]:
    """Distinct (reference_ts, settle_ts) of unsettled rows whose settlement minute + grace passed."""
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT reference_ts, settle_ts FROM btc_daily_market_decisions "
            "WHERE settled_at IS NULL AND settle_ts + ? <= ? ORDER BY reference_ts",
            (grace_s, now_ts),
        )
        return [(int(r["reference_ts"]), int(r["settle_ts"])) for r in await cur.fetchall()]


async def settle_window(
    reference_ts: int, reference_close: float, settle_close: float, result: str
) -> None:
    """Settle every row of one window; ``result`` is "Up", "Down" or "tie"."""
    async with _db.connect() as conn:
        await conn.execute(
            "UPDATE btc_daily_market_decisions SET reference_close = ?, settle_close = ?, "
            "outcome = ?, settled_at = ? WHERE reference_ts = ? AND settled_at IS NULL",
            (reference_close, settle_close, result, _db.utc_now_iso(), reference_ts),
        )
        await conn.commit()
