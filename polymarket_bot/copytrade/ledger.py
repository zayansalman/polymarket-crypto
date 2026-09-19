"""Paper ledger for copy trades: one row per copy, settled on resolution.

Every row records BOTH prices — what the target paid and what we would have had
to pay arriving late — because the gap between them is the whole question. A
ledger that stored only our price would hide whether a losing copy lost because
the target was wrong or because following cost too much.

Rows are keyed on (target, condition_id, their tx) so a restart or an overlapping
poll cannot double-open the same copy.
"""

from __future__ import annotations

from typing import Any

import db as _db  # type: ignore[import-untyped]

SCHEMA = """
CREATE TABLE IF NOT EXISTS copy_trades (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  target        TEXT NOT NULL,
  target_label  TEXT,
  tx            TEXT NOT NULL,
  condition_id  TEXT NOT NULL,
  token_id      TEXT,
  window_slug   TEXT,
  title         TEXT,
  outcome       TEXT,
  their_price   REAL,
  our_price     REAL,
  size          REAL,
  fee           REAL,
  cost_usd      REAL,
  slippage      REAL,
  their_ts      INTEGER,
  our_ts        INTEGER,
  resolves_at   INTEGER,
  state         TEXT NOT NULL DEFAULT 'open',
  won           INTEGER,
  pnl           REAL,
  settled_at    INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_copy_trades_key
  ON copy_trades(target, condition_id, tx, outcome);
CREATE INDEX IF NOT EXISTS idx_copy_trades_state ON copy_trades(state);
"""


async def init() -> None:
    async with _db.connect() as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()


async def record(**row: Any) -> bool:
    """Insert one open copy. True iff a new row was actually created."""
    cols = (
        "target", "target_label", "tx", "condition_id", "token_id", "window_slug",
        "title", "outcome", "their_price", "our_price", "size", "fee", "cost_usd",
        "slippage", "their_ts", "our_ts", "resolves_at",
    )
    placeholders = ", ".join("?" for _ in cols)
    async with _db.connect() as conn:
        cur = await conn.execute(
            f"INSERT OR IGNORE INTO copy_trades ({', '.join(cols)}) "
            f"VALUES ({placeholders})",
            tuple(row.get(c) for c in cols),
        )
        await conn.commit()
        return cur.rowcount > 0


async def open_rows() -> list[dict]:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM copy_trades WHERE state='open' ORDER BY their_ts DESC"
        )
        return [dict(r) for r in await cur.fetchall()]


async def settled_rows(limit: int = 200) -> list[dict]:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM copy_trades WHERE state='settled' "
            "ORDER BY settled_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def settle(row_id: int, *, won: bool, pnl: float, now: int) -> None:
    async with _db.connect() as conn:
        await conn.execute(
            "UPDATE copy_trades SET state='settled', won=?, pnl=?, settled_at=? "
            "WHERE id=? AND state='open'",
            (1 if won else 0, pnl, now, row_id),
        )
        await conn.commit()


async def summary() -> dict:
    """Headline numbers for the dashboard, per target and overall."""
    async with _db.connect() as conn:
        cur = await conn.execute(
            """SELECT target_label,
                      COUNT(*)                                   AS n,
                      SUM(CASE WHEN won=1 THEN 1 ELSE 0 END)     AS wins,
                      SUM(pnl)                                   AS pnl,
                      SUM(cost_usd)                              AS staked,
                      SUM(size)                                  AS shares,
                      AVG(slippage)                              AS slip
               FROM copy_trades WHERE state='settled'
               GROUP BY target_label"""
        )
        per_target = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) AS wins,
                      SUM(pnl) AS pnl, SUM(cost_usd) AS staked, SUM(size) AS shares
               FROM copy_trades WHERE state='settled'"""
        )
        total = dict(await cur.fetchone() or {})
        cur = await conn.execute(
            "SELECT COUNT(*) AS n, SUM(cost_usd) AS staked "
            "FROM copy_trades WHERE state='open'"
        )
        openp = dict(await cur.fetchone() or {})
    return {"per_target": per_target, "total": total, "open": openp}
