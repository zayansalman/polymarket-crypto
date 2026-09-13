"""Persistence for the two-sided pair shadow tester (#182).

Writes to its **own** SQLite file (default ``data/pairarb_shadow.db``), not the
trading ledger. Same separation the venue recorder uses: this is research
output, and the money-path DB and its migrations should not carry a schema that
no live code reads. It also means a week-long shadow run cannot corrupt or lock
the database the live loop depends on.

Two tables:

* ``pair_windows`` — one settled row per window. ``window_slug`` is UNIQUE, so
  re-settling a window is idempotent and a crash-restart cannot double-count.
* ``pair_execs`` — every simulated fill. Kept separate from the settled row
  because the adverse-selection question ("did this leg fill *because* it was
  about to lose?") needs per-fill price and timing, which a settled aggregate
  has already averaged away.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite

DEFAULT_DB = Path("data/pairarb_shadow.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pair_windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_slug TEXT NOT NULL,
  asset TEXT,
  start_ts INTEGER,
  settled_at INTEGER,
  up_filled REAL,
  down_filled REAL,
  up_vwap REAL,
  down_vwap REAL,
  resolved_up INTEGER,
  pairs REAL,
  stranded REAL,
  pnl REAL,
  requotes INTEGER,
  quoted_up REAL,
  quoted_down REAL,
  up_depth_ahead REAL,
  down_depth_ahead REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pair_windows_slug
  ON pair_windows(window_slug);

CREATE TABLE IF NOT EXISTS pair_execs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_slug TEXT NOT NULL,
  asset TEXT,
  outcome TEXT,
  price REAL,
  size REAL,
  depth_ahead REAL,
  posted_ts INTEGER,
  filled_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pair_execs_slug ON pair_execs(window_slug);
"""


async def init(db_path: Path = DEFAULT_DB) -> None:
    """Create the schema if absent. Safe to call on every start."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def record_window(
    row: dict[str, Any], db_path: Path = DEFAULT_DB
) -> bool:
    """Persist one settled window. Returns False if it was already recorded.

    ``INSERT OR IGNORE`` against the UNIQUE slug index makes this idempotent, so
    a restart that re-settles a window it already saw is a no-op rather than a
    double-count.
    """
    cols = (
        "window_slug", "asset", "start_ts", "settled_at",
        "up_filled", "down_filled", "up_vwap", "down_vwap",
        "resolved_up", "pairs", "stranded", "pnl", "requotes",
        "quoted_up", "quoted_down", "up_depth_ahead", "down_depth_ahead",
    )
    placeholders = ",".join("?" for _ in cols)
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            f"INSERT OR IGNORE INTO pair_windows ({','.join(cols)}) "
            f"VALUES ({placeholders})",
            tuple(row.get(c) for c in cols),
        )
        await db.commit()
        return cur.rowcount > 0


async def record_execs(
    window_slug: str,
    asset: str,
    execs: list[dict[str, Any]],
    db_path: Path = DEFAULT_DB,
) -> None:
    """Persist the simulated fills for one window."""
    if not execs:
        return
    async with aiosqlite.connect(db_path) as db:
        await db.executemany(
            "INSERT INTO pair_execs "
            "(window_slug, asset, outcome, price, size, depth_ahead, posted_ts, filled_ts) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    window_slug,
                    asset,
                    e.get("outcome"),
                    e.get("price"),
                    e.get("size"),
                    e.get("depth_ahead"),
                    e.get("posted_ts"),
                    e.get("filled_ts"),
                )
                for e in execs
            ],
        )
        await db.commit()


async def already_settled(
    window_slug: str, db_path: Path = DEFAULT_DB
) -> bool:
    """Whether this window has already been recorded."""
    if not db_path.exists():
        return False
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT 1 FROM pair_windows WHERE window_slug = ? LIMIT 1", (window_slug,)
        ) as cur:
            return await cur.fetchone() is not None


async def summary(db_path: Path = DEFAULT_DB) -> dict[str, Any]:
    """Aggregate the settled record for reporting."""
    if not db_path.exists():
        return {}
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT COUNT(*)                                   AS windows,
                   SUM(CASE WHEN up_filled > 0 OR down_filled > 0 THEN 1 ELSE 0 END)
                                                              AS with_fills,
                   SUM(CASE WHEN up_filled > 0 AND down_filled > 0 THEN 1 ELSE 0 END)
                                                              AS both_legs,
                   COALESCE(SUM(pairs), 0)                    AS pairs,
                   COALESCE(SUM(stranded), 0)                 AS stranded,
                   COALESCE(SUM(pnl), 0)                      AS pnl
            FROM pair_windows
            """
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else {}
