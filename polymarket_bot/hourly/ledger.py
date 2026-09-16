"""Hourly decision record: one row per (UTC hour start, strategy, mode), actions, settlement."""
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


# Rows are looked up by the hour's UTC start, never its ET slug: on the November fall-back day
# two hours share one slug (Claude, 2026-09-15, branch-review finding
# dst-fallback-slug-collision).
# Rows are also looked up by mode, so a paper run and a live run in the same hour never read
# or overwrite each other's decision (Claude, 2026-09-15, branch-review finding
# decision-row-shared-across-modes).
async def get_decision(
    window_start_ts: int, strategy_id: str, *, mode: str
) -> dict[str, Any] | None:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM hourly_strategy_context "
            "WHERE window_start_ts = ? AND strategy_id = ? AND mode = ?",
            (window_start_ts, strategy_id, mode),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_action(
    window_start_ts: int,
    strategy_id: str,
    action: str,
    position_id: int | None = None,
    *,
    mode: str,
    expected_action: str | None = None,
) -> None:
    # Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried:
    # expected_action makes the update apply only while the row still holds that action,
    # so closing out an unfinished attempt never overwrites a result written meanwhile.
    async with _db.connect() as conn:
        await conn.execute(
            "UPDATE hourly_strategy_context SET action = ?, "
            "position_id = COALESCE(?, position_id) "
            "WHERE window_start_ts = ? AND strategy_id = ? AND mode = ? "
            "AND (? IS NULL OR action = ?)",
            (action[:240], position_id, window_start_ts, strategy_id, mode,
             expected_action, expected_action),
        )
        await conn.commit()


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
