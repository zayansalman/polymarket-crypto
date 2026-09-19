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
  their_size    REAL,
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
  settled_at    INTEGER,
  -- Decision-time price is optimistic: it assumes the displayed ladder is still
  -- there when our order lands. These columns hold the SAME order re-priced
  -- against the book ~25s later, which is when a real order would arrive.
  real_price    REAL,
  real_fee      REAL,
  real_cost_usd REAL,
  -- Shares actually obtainable at re-quote. Less than `size` when the book was
  -- too thin: the realistic payout must be on THIS, not the intended size.
  real_size     REAL,
  real_slippage REAL,
  real_pnl      REAL,
  requoted_at   INTEGER,
  -- Set when the target exited a market we still hold. From that point our
  -- copy is no longer tracking them, and the result is ours, not theirs.
  target_exited INTEGER
);
-- Every fill considered, including the ones we declined and why. A copier that
-- silently skips looks identical to one with nothing to do.
CREATE TABLE IF NOT EXISTS copy_decisions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           INTEGER NOT NULL,
  target       TEXT NOT NULL,
  target_label TEXT,
  tx           TEXT,
  condition_id TEXT,
  title        TEXT,
  outcome      TEXT,
  their_size   REAL,
  their_price  REAL,
  decision     TEXT NOT NULL,
  reason       TEXT,
  our_price    REAL,
  our_size     REAL,
  lag_seconds  REAL,
  -- What declining actually cost or saved. A skip is a decision, and a
  -- decision with no measured outcome is just an opinion.
  token_id     TEXT,
  shadow_pnl   REAL,
  shadow_won   INTEGER,
  settled_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_copy_decisions_ts ON copy_decisions(ts);
CREATE INDEX IF NOT EXISTS idx_copy_decisions_d ON copy_decisions(decision);
CREATE UNIQUE INDEX IF NOT EXISTS idx_copy_trades_key
  ON copy_trades(target, condition_id, tx, outcome);
CREATE INDEX IF NOT EXISTS idx_copy_trades_state ON copy_trades(state);
"""


async def init() -> None:
    async with _db.connect() as conn:
        await conn.executescript(SCHEMA)
        # Columns added after the table first shipped.
        for col, decl in (
            ("their_size", "REAL"), ("real_pnl", "REAL"),
            ("real_size", "REAL"),
            ("target_exited", "INTEGER"),
            ("real_price", "REAL"), ("real_fee", "REAL"),
            ("real_cost_usd", "REAL"), ("real_slippage", "REAL"),
            ("requoted_at", "INTEGER"),
        ):
            try:
                await conn.execute(f"ALTER TABLE copy_trades ADD COLUMN {col} {decl}")
            except Exception:  # noqa: BLE001 — already present
                pass
        await conn.commit()


async def log_decision(**row: object) -> None:
    """Record every fill we looked at, copied or not."""
    cols = ("ts", "target", "target_label", "tx", "condition_id", "title",
            "outcome", "their_size", "their_price", "decision", "reason",
            "our_price", "our_size", "lag_seconds", "token_id")
    async with _db.connect() as conn:
        await conn.execute(
            f"INSERT INTO copy_decisions ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            tuple(row.get(c) for c in cols))
        await conn.commit()


async def decisions(limit: int = 100) -> list[dict]:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM copy_decisions ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def decision_counts(since: int) -> list[dict]:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT decision, reason, COUNT(*) n FROM copy_decisions "
            "WHERE ts>=? GROUP BY decision, reason ORDER BY n DESC", (since,))
        return [dict(r) for r in await cur.fetchall()]


async def reachability(since: int) -> list[dict]:
    """Per target: how often we could actually get in.

    A wallet that enters in the last seconds of a window is uncopyable however
    good its edge, because the market has closed before a follower sees the
    fill. This is the screen criterion the offline research could not measure —
    only live polling reveals it.
    """
    async with _db.connect() as conn:
        cur = await conn.execute(
            """SELECT target_label,
                      COUNT(*) AS seen,
                      SUM(CASE WHEN decision='copied' THEN 1 ELSE 0 END) AS copied,
                      SUM(CASE WHEN reason LIKE 'market already closed%'
                               THEN 1 ELSE 0 END) AS too_late,
                      AVG(lag_seconds) AS lag
               FROM copy_decisions
               WHERE ts>=? AND decision IN ('copied','skipped')
               GROUP BY target_label
               HAVING seen >= 3
               ORDER BY copied DESC, seen DESC""", (since,))
        return [dict(r) for r in await cur.fetchall()]


async def unsettled_skips(older_than_ts: int) -> list[dict]:
    """Declined fills old enough that their market should have resolved."""
    async with _db.connect() as conn:
        for col, decl in (("token_id", "TEXT"), ("shadow_pnl", "REAL"),
                          ("shadow_won", "INTEGER"), ("settled_at", "INTEGER")):
            try:
                await conn.execute(
                    f"ALTER TABLE copy_decisions ADD COLUMN {col} {decl}")
            except Exception:  # noqa: BLE001
                pass
        await conn.commit()
        cur = await conn.execute(
            "SELECT * FROM copy_decisions WHERE decision='skipped' "
            "AND settled_at IS NULL AND our_price IS NOT NULL "
            "AND condition_id IS NOT NULL AND ts <= ? LIMIT 40",
            (older_than_ts,))
        return [dict(r) for r in await cur.fetchall()]


async def settle_skip(row_id: int, *, won: bool, pnl: float, now: int) -> None:
    async with _db.connect() as conn:
        await conn.execute(
            "UPDATE copy_decisions SET shadow_won=?, shadow_pnl=?, settled_at=? "
            "WHERE id=?", (1 if won else 0, pnl, now, row_id))
        await conn.commit()


async def skip_scoreboard(since: int) -> list[dict]:
    """Was declining right? Per reason, what the skipped trades would have done."""
    async with _db.connect() as conn:
        cur = await conn.execute(
            """SELECT reason,
                      COUNT(*) AS n,
                      SUM(shadow_pnl) AS pnl,
                      SUM(CASE WHEN shadow_won=1 THEN 1 ELSE 0 END) AS wins
               FROM copy_decisions
               WHERE decision='skipped' AND settled_at IS NOT NULL AND ts>=?
               GROUP BY reason ORDER BY n DESC""", (since,))
        return [dict(r) for r in await cur.fetchall()]


async def open_on_market(target: str, condition_id: str, outcome: str) -> list[dict]:
    """Open copies of one target on one outcome — used to spot their exit."""
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM copy_trades WHERE state='open' AND target=? "
            "AND condition_id=? AND outcome=?", (target, condition_id, outcome))
        return [dict(r) for r in await cur.fetchall()]


async def mark_target_exited(row_id: int, now: int) -> None:
    async with _db.connect() as conn:
        await conn.execute(
            "UPDATE copy_trades SET target_exited=? WHERE id=? AND target_exited IS NULL",
            (now, row_id))
        await conn.commit()


async def needs_requote(older_than: int, now: int) -> list[dict]:
    """Open copies old enough that a real order would already have landed."""
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM copy_trades WHERE requoted_at IS NULL "
            "AND state='open' AND our_ts <= ? ORDER BY our_ts", (now - older_than,))
        return [dict(r) for r in await cur.fetchall()]


async def set_requote(row_id: int, *, price: float | None, fee: float,
                      cost: float, slippage: float | None, size: float,
                      now: int) -> None:
    async with _db.connect() as conn:
        await conn.execute(
            "UPDATE copy_trades SET real_price=?, real_fee=?, real_cost_usd=?, "
            "real_slippage=?, real_size=?, requoted_at=? WHERE id=?",
            (price, fee, cost, slippage, size, now, row_id))
        await conn.commit()


async def record(**row: Any) -> bool:
    """Insert one open copy. True iff a new row was actually created."""
    cols = (
        "target", "target_label", "tx", "condition_id", "token_id", "window_slug",
        "title", "outcome", "their_size", "their_price", "our_price", "size",
        "fee", "cost_usd",
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


async def settle(row_id: int, *, won: bool, pnl: float, real_pnl: float | None,
                 now: int) -> None:
    """Close a copy with BOTH results.

    ``pnl`` is the optimistic paper fill. ``real_pnl`` is the same position at
    the price a real order would have got, and is exactly 0 when the book was
    gone — an order that never filled neither wins nor loses, and counting it as
    a win is the single biggest way a paper ledger lies.
    """
    async with _db.connect() as conn:
        await conn.execute(
            "UPDATE copy_trades SET state='settled', won=?, pnl=?, real_pnl=?, "
            "settled_at=? WHERE id=? AND state='open'",
            (1 if won else 0, pnl, real_pnl, now, row_id),
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
                      SUM(real_pnl)                              AS real_pnl,
                      SUM(CASE WHEN real_price IS NULL THEN 1 ELSE 0 END)
                                                                 AS never_filled,
                      SUM(cost_usd)                              AS staked,
                      SUM(real_cost_usd)                         AS real_staked,
                      AVG(real_slippage)                         AS real_slip,
                      SUM(size)                                  AS shares,
                      AVG(slippage)                              AS slip
               FROM copy_trades WHERE state='settled'
               GROUP BY target_label"""
        )
        per_target = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) AS wins,
                      SUM(pnl) AS pnl, SUM(real_pnl) AS real_pnl,
                      SUM(cost_usd) AS staked, SUM(size) AS shares,
                      SUM(CASE WHEN real_price IS NULL THEN 1 ELSE 0 END) AS never_filled,
                      SUM(real_cost_usd) AS real_staked,
                      AVG(real_slippage) AS real_slip
               FROM copy_trades WHERE state='settled'"""
        )
        total = dict(await cur.fetchone() or {})
        cur = await conn.execute(
            "SELECT COUNT(*) AS n, SUM(cost_usd) AS staked "
            "FROM copy_trades WHERE state='open'"
        )
        openp = dict(await cur.fetchone() or {})
        cur = await conn.execute(
            """SELECT COUNT(*) AS n,
                      SUM(cost_usd) AS staked,
                      SUM(real_cost_usd) AS real_staked,
                      AVG(real_slippage) AS real_slip,
                      SUM(CASE WHEN real_price IS NULL THEN 1 ELSE 0 END) AS unfilled,
                      -- On the rows that DID fill, how the price itself moved.
                      -- Mixing these with the dead orders hides both.
                      SUM(CASE WHEN real_price IS NOT NULL THEN cost_usd ELSE 0 END)
                          AS filled_decision,
                      SUM(CASE WHEN real_price IS NOT NULL THEN real_cost_usd ELSE 0 END)
                          AS filled_real,
                      SUM(CASE WHEN real_price IS NULL THEN cost_usd ELSE 0 END)
                          AS lost_to_unfilled,
                      SUM(CASE WHEN real_price IS NOT NULL AND real_size < size * 0.99
                               THEN 1 ELSE 0 END) AS partial,
                      SUM(CASE WHEN their_size IS NOT NULL AND size > their_size * 1.05
                               THEN 1 ELSE 0 END) AS upsized,
                      (SELECT COUNT(*) FROM copy_trades
                        WHERE target_exited IS NOT NULL) AS diverged
               FROM copy_trades WHERE requoted_at IS NOT NULL"""
        )
        execq = dict(await cur.fetchone() or {})
    return {"per_target": per_target, "total": total, "open": openp,
            "execution": execq}
