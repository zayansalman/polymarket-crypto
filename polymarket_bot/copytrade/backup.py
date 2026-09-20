"""Periodic snapshots of the copy-trade record.

The paper history is the whole product of this experiment and it lives in one
SQLite file alongside everything else. That file has already been cleared once —
by a test with no database isolation — and there was nothing to restore from.

So the ledger is snapshotted to its own timestamped file on a schedule. Snapshots
are plain SQLite, readable without the app, and pruned to a fixed count so they
cannot grow without bound. This copies ONLY the copy-trade tables: restoring a
whole-app backup over a live database would be a far bigger hazard than the one
it guards against.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import structlog

import config as _config

log = structlog.get_logger(__name__)

TABLES = ("copy_trades", "copy_decisions")
KEEP = 48


def snapshot_dir() -> Path:
    return Path(_config.DB_PATH).resolve().parent / "copytrade_snapshots"


def write_snapshot(now: int | None = None) -> Path | None:
    """Copy the copy-trade tables into a standalone timestamped database."""
    now = int(time.time()) if now is None else now
    out_dir = snapshot_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
    out = out_dir / f"copytrade-{stamp}.db"

    src = sqlite3.connect(f"file:{_config.DB_PATH}?mode=ro", uri=True)
    try:
        have = {r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        present = [t for t in TABLES if t in have]
        if not present:
            return None
        rows = 0
        dst = sqlite3.connect(out)
        try:
            for table in present:
                schema = src.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)).fetchone()
                if not schema or not schema[0]:
                    continue
                dst.execute(schema[0])
                cur = src.execute(f"SELECT * FROM {table}")
                cols = len(cur.description)
                placeholders = ",".join("?" for _ in range(cols))
                batch = cur.fetchall()
                if batch:
                    dst.executemany(
                        f"INSERT INTO {table} VALUES ({placeholders})", batch)
                    rows += len(batch)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()

    if rows == 0:
        out.unlink(missing_ok=True)
        return None
    _prune(out_dir)
    log.info("copytrade.snapshot", path=str(out), rows=rows)
    return out


def _prune(out_dir: Path, keep: int = KEEP) -> None:
    snaps = sorted(out_dir.glob("copytrade-*.db"))
    for old in snaps[:-keep]:
        old.unlink(missing_ok=True)


def latest() -> Path | None:
    snaps = sorted(snapshot_dir().glob("copytrade-*.db"))
    return snaps[-1] if snaps else None
