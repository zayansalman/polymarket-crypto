"""Hourly strategy decision record: one row per (hour, strategy), actions, and settlement."""
from __future__ import annotations

import json
from typing import Any

import db as _db
from polymarket_bot.hourly.market import HOUR_S, up_won


async def record_decision(
    *,
    strategy_id: str,
    window_slug: str,
    window_start_ts: int,
    side: str | None,
    reason: str,
    signal: dict[str, Any],
    factors: dict[str, Any],
    up_bid: float | None,
    up_ask: float | None,
    down_bid: float | None,
    down_ask: float | None,
    hour_open: float | None,
    mode: str,
    late: bool,
) -> bool:
    """INSERT OR IGNORE the hour's decision; True iff this call wrote the row."""
    if side is None:
        action = "NO_SIGNAL"
    else:
        action = "MISSED" if late else "PENDING"
    async with _db.connect() as conn:
        cur = await conn.execute(
            """
            INSERT OR IGNORE INTO hourly_strategy_context(
              created_at, strategy_id, window_slug, window_start_ts, decision_side,
              decision_reason, signal_json, factors_json, up_bid, up_ask, down_bid,
              down_ask, hour_open, action, mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _db.utc_now_iso(), strategy_id, window_slug, window_start_ts, side, reason,
                json.dumps(signal, default=str), json.dumps(factors, default=str),
                up_bid, up_ask, down_bid, down_ask, hour_open, action, mode,
            ),
        )
        await conn.commit()
        return cur.rowcount == 1


async def get_decision(window_slug: str, strategy_id: str) -> dict[str, Any] | None:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM hourly_strategy_context WHERE window_slug = ? AND strategy_id = ?",
            (window_slug, strategy_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_action(
    window_slug: str, strategy_id: str, action: str, position_id: int | None = None
) -> None:
    async with _db.connect() as conn:
        await conn.execute(
            "UPDATE hourly_strategy_context SET action = ?, "
            "position_id = COALESCE(?, position_id) "
            "WHERE window_slug = ? AND strategy_id = ?",
            (action[:240], position_id, window_slug, strategy_id),
        )
        await conn.commit()


async def finalize_pending_past_deadline(now: int, deadline_s: int) -> int:
    """Resolve every PENDING row whose hour's entry deadline has passed; return rows changed.

    Claude, 2026-09-15, branch-review finding pending-row-never-finalized: a row left
    PENDING when no tick ran between its deadline and the hour's end (Stop, crash,
    sleep, failing ticks, strategy disabled) otherwise stays PENDING forever. A row
    whose hour already has this strategy's hourly position (crash after the live
    submit, before the ENTERED write) becomes ENTERED with that position; every
    other row becomes MISSED. Uses the same test as the engine's current-hour check
    (now - window_start_ts > deadline), for every strategy, enabled or not.
    """
    position_for_row = (
        "SELECT p.position_id FROM paper_positions p "
        "WHERE p.window_slug = hourly_strategy_context.window_slug "
        "AND p.strategy_id = hourly_strategy_context.strategy_id "
        "AND p.market_timeframe = '1h'"
    )
    async with _db.connect() as conn:
        entered = await conn.execute(
            "UPDATE hourly_strategy_context SET action = 'ENTERED', "
            f"position_id = ({position_for_row} ORDER BY p.position_id LIMIT 1) "
            f"WHERE action = 'PENDING' AND window_start_ts + ? < ? AND EXISTS ({position_for_row})",
            (deadline_s, now),
        )
        missed = await conn.execute(
            "UPDATE hourly_strategy_context SET action = 'MISSED' "
            "WHERE action = 'PENDING' AND window_start_ts + ? < ?",
            (deadline_s, now),
        )
        await conn.commit()
        return max(entered.rowcount, 0) + max(missed.rowcount, 0)


async def unsettled_windows(now: int) -> list[int]:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT window_start_ts FROM hourly_strategy_context "
            "WHERE settled_at IS NULL AND window_start_ts + ? <= ? ORDER BY window_start_ts",
            (HOUR_S, now),
        )
        return [int(r["window_start_ts"]) for r in await cur.fetchall()]


async def settle_window(window_start_ts: int, hour_open: float, hour_close: float) -> None:
    outcome = "Up" if up_won(hour_open, hour_close) else "Down"
    async with _db.connect() as conn:
        await conn.execute(
            "UPDATE hourly_strategy_context SET hour_open = COALESCE(hour_open, ?), "
            "hour_close = ?, outcome_side = ?, settled_at = ? "
            "WHERE window_start_ts = ? AND settled_at IS NULL",
            (hour_open, hour_close, outcome, _db.utc_now_iso(), window_start_ts),
        )
        await conn.commit()
