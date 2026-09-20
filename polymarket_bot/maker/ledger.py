"""Paper ledger for resting maker quotes.

A taker order either fills or errors, so a copy ledger only needs the fill. A
maker order has a third outcome that matters more than either: it rests and
nothing happens. Most quotes end that way, and a ledger that only stored fills
would report the edge of the orders that filled while hiding how rarely they do.

So a quote is a row from the moment it is placed. It carries the queue it joined
(`depth_ahead`: shares already resting at our price or better), because that
number is the whole difference between a paper fill and a real one. We do not
fill until the tape has traded through that queue. Assuming otherwise is exactly
the optimism that makes paper results useless.

States: resting -> filled -> settled, or resting -> expired.
"""

from __future__ import annotations

from typing import Any

import db as _db  # type: ignore[import-untyped]

SCHEMA = """
CREATE TABLE IF NOT EXISTS maker_quotes (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  quoted_ts     INTEGER NOT NULL,
  condition_id  TEXT NOT NULL,
  token_id      TEXT NOT NULL,
  window_slug   TEXT,
  title         TEXT,
  outcome       TEXT,
  -- What we posted.
  quote_price   REAL NOT NULL,
  quote_size    REAL NOT NULL,
  -- The book we posted into, kept so a bad fill can be traced to a bad quote.
  best_bid      REAL,
  best_ask      REAL,
  mid           REAL,
  spread        REAL,
  -- Shares resting at our price or better when we joined. Volume must trade
  -- through this before any of it is ours.
  depth_ahead   REAL NOT NULL DEFAULT 0,
  -- Volume seen crossing to our level since, whether or not it reached us.
  crossed       REAL NOT NULL DEFAULT 0,
  state         TEXT NOT NULL DEFAULT 'resting',
  filled_ts     INTEGER,
  filled_size   REAL,
  resolves_at   INTEGER,
  expired_ts    INTEGER,
  won           INTEGER,
  pnl           REAL,
  settled_at    INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_maker_quotes_key
  ON maker_quotes(condition_id, token_id, quote_price, quoted_ts);
CREATE INDEX IF NOT EXISTS idx_maker_quotes_state ON maker_quotes(state);

CREATE TABLE IF NOT EXISTS maker_decisions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           INTEGER NOT NULL,
  condition_id TEXT,
  token_id     TEXT,
  title        TEXT,
  outcome      TEXT,
  best_bid     REAL,
  best_ask     REAL,
  decision     TEXT NOT NULL,
  reason       TEXT,
  quote_price  REAL,
  quote_size   REAL
);
CREATE INDEX IF NOT EXISTS idx_maker_decisions_ts ON maker_decisions(ts);
CREATE INDEX IF NOT EXISTS idx_maker_decisions_d ON maker_decisions(decision);
"""

QUOTE_COLS = (
    "quoted_ts", "condition_id", "token_id", "window_slug", "title", "outcome",
    "quote_price", "quote_size", "best_bid", "best_ask", "mid", "spread",
    "depth_ahead", "resolves_at",
)
DECISION_COLS = (
    "ts", "condition_id", "token_id", "title", "outcome", "best_bid",
    "best_ask", "decision", "reason", "quote_price", "quote_size",
)


async def init() -> None:
    async with _db.connect() as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()


async def log_decision(**row: Any) -> None:
    """Record every market looked at, quoted or not, and why."""
    async with _db.connect() as conn:
        await conn.execute(
            f"INSERT INTO maker_decisions ({', '.join(DECISION_COLS)}) "
            f"VALUES ({', '.join('?' for _ in DECISION_COLS)})",
            tuple(row.get(c) for c in DECISION_COLS))
        await conn.commit()


async def place(**row: Any) -> int | None:
    """Insert one resting quote. Returns its id, or None if already present."""
    async with _db.connect() as conn:
        cur = await conn.execute(
            f"INSERT OR IGNORE INTO maker_quotes ({', '.join(QUOTE_COLS)}) "
            f"VALUES ({', '.join('?' for _ in QUOTE_COLS)})",
            tuple(row.get(c) for c in QUOTE_COLS))
        await conn.commit()
        return cur.lastrowid if cur.rowcount > 0 else None


async def resting() -> list[dict]:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM maker_quotes WHERE state='resting' ORDER BY quoted_ts")
        return [dict(r) for r in await cur.fetchall()]


async def filled_unsettled() -> list[dict]:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM maker_quotes WHERE state='filled' ORDER BY filled_ts")
        return [dict(r) for r in await cur.fetchall()]


async def note_crossed(row_id: int, crossed: float) -> None:
    """Record volume seen at our level even when it did not reach us.

    A quote that watched 400 shares trade through and got none of them is a
    different failure from one nothing ever came near, and the fill model is
    only checkable if both are written down.
    """
    async with _db.connect() as conn:
        await conn.execute(
            "UPDATE maker_quotes SET crossed=? WHERE id=?", (crossed, row_id))
        await conn.commit()


async def fill(row_id: int, *, ts: int, size: float, crossed: float) -> None:
    async with _db.connect() as conn:
        await conn.execute(
            "UPDATE maker_quotes SET state='filled', filled_ts=?, filled_size=?, "
            "crossed=? WHERE id=? AND state='resting'",
            (ts, size, crossed, row_id))
        await conn.commit()


async def expire(row_id: int, *, ts: int, crossed: float) -> None:
    async with _db.connect() as conn:
        await conn.execute(
            "UPDATE maker_quotes SET state='expired', expired_ts=?, crossed=? "
            "WHERE id=? AND state='resting'", (ts, crossed, row_id))
        await conn.commit()


async def settle(row_id: int, *, won: bool, pnl: float, ts: int) -> None:
    async with _db.connect() as conn:
        await conn.execute(
            "UPDATE maker_quotes SET state='settled', won=?, pnl=?, settled_at=? "
            "WHERE id=? AND state='filled'", (1 if won else 0, pnl, ts, row_id))
        await conn.commit()


async def summary() -> dict:
    """Headline numbers, each over the population it actually applies to.

    Counts and sums are reported with the row count they were computed over,
    because a per-share figure divided by the wrong denominator has been the
    most expensive kind of mistake in this project.
    """
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT state, COUNT(*) n, SUM(quote_size) sz, SUM(crossed) cr "
            "FROM maker_quotes GROUP BY state")
        by_state = {r["state"]: dict(r) for r in await cur.fetchall()}

        cur = await conn.execute(
            "SELECT COUNT(*) n, SUM(filled_size) shares, SUM(pnl) pnl, "
            "SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) wins, "
            "SUM(filled_size * quote_price) staked "
            "FROM maker_quotes WHERE state='settled' AND pnl IS NOT NULL")
        s = dict((await cur.fetchone()) or {})

    placed = sum(v["n"] for v in by_state.values())
    filled = (by_state.get("filled", {}).get("n", 0)
              + by_state.get("settled", {}).get("n", 0))
    n = s.get("n") or 0
    shares = s.get("shares") or 0.0
    return {
        "placed": placed,
        "resting": by_state.get("resting", {}).get("n", 0),
        "expired": by_state.get("expired", {}).get("n", 0),
        "filled": filled,
        "fill_rate": (filled / placed) if placed else 0.0,
        "settled_n": n,
        "settled_shares": shares,
        "wins": s.get("wins") or 0,
        "pnl": s.get("pnl") or 0.0,
        "staked": s.get("staked") or 0.0,
        "cents_per_share": (100.0 * (s.get("pnl") or 0.0) / shares) if shares else 0.0,
    }


async def decision_counts(since: int) -> list[dict]:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT decision, reason, COUNT(*) n FROM maker_decisions "
            "WHERE ts>=? GROUP BY decision, reason ORDER BY n DESC", (since,))
        return [dict(r) for r in await cur.fetchall()]


async def recent(limit: int = 40) -> list[dict]:
    """Newest quotes first, in any state — including the ones that never filled.

    Showing only fills would make the card a highlight reel; the unfilled rows
    are the fill rate, which is the number that decides whether the edge is
    reachable at all.
    """
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM maker_quotes ORDER BY quoted_ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def by_band() -> list[dict]:
    """Settled results grouped by the price we actually filled at.

    The research said the edge lives between 0.55 and 0.92 and inverts below
    the midpoint. This is the same cut applied to our own fills, so the live
    record can disagree with the study rather than inherit its conclusion.
    """
    async with _db.connect() as conn:
        cur = await conn.execute(
            """
            SELECT CASE
                     WHEN quote_price < 0.55 THEN '<0.55'
                     WHEN quote_price < 0.65 THEN '0.55-0.65'
                     WHEN quote_price < 0.75 THEN '0.65-0.75'
                     WHEN quote_price < 0.85 THEN '0.75-0.85'
                     ELSE '0.85+'
                   END band,
                   COUNT(*) n,
                   SUM(filled_size) shares,
                   SUM(pnl) pnl,
                   SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) wins
            FROM maker_quotes
            WHERE state='settled' AND pnl IS NOT NULL AND filled_size > 0
            GROUP BY band ORDER BY band
            """)
        return [dict(r) for r in await cur.fetchall()]


async def queue_report() -> dict:
    """How the queue actually behaved, over the rows each figure covers.

    Two different failures hide behind one fill rate: quotes nothing came near,
    and quotes that watched volume trade past them. They are counted apart.
    """
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) n, SUM(crossed) crossed, SUM(depth_ahead) queue "
            "FROM maker_quotes WHERE state IN ('expired','resting')")
        un = dict((await cur.fetchone()) or {})
        cur = await conn.execute(
            "SELECT COUNT(*) n FROM maker_quotes "
            "WHERE state IN ('expired','resting') AND crossed > 0")
        touched = dict((await cur.fetchone()) or {})
        cur = await conn.execute(
            "SELECT COUNT(*) n, AVG(crossed) crossed, AVG(depth_ahead) queue "
            "FROM maker_quotes WHERE state IN ('filled','settled')")
        fl = dict((await cur.fetchone()) or {})
    return {
        "unfilled_n": un.get("n") or 0,
        "unfilled_touched": touched.get("n") or 0,
        "unfilled_crossed": un.get("crossed") or 0.0,
        "filled_n": fl.get("n") or 0,
        "filled_avg_crossed": fl.get("crossed") or 0.0,
        "filled_avg_queue": fl.get("queue") or 0.0,
    }


async def quoted_markets() -> set[str]:
    """Every condition_id we have ever quoted, in any state.

    One fixed clip per market, and no second bite after the first fills. The
    edge measured at a fixed clip per market is +3.8c/share; the same band
    weighted by how much volume each market drew is NEGATIVE. Re-quoting a busy
    market is how a fixed clip quietly turns into a volume-weighted one.
    """
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT DISTINCT condition_id FROM maker_quotes")
        return {r[0] for r in await cur.fetchall()}
