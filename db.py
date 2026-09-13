"""SQLite storage for the local Polymarket crypto trading lab."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, AsyncIterator

import aiosqlite

from config import DB_PATH
from logging_setup import redact_secrets


SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_feed (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  event_type TEXT NOT NULL,
  message TEXT NOT NULL,
  details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_notification_feed_created
  ON notification_feed(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notification_feed_event
  ON notification_feed(event_type);

CREATE TABLE IF NOT EXISTS config (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_ticks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  window_slug TEXT NOT NULL,
  market_question TEXT,
  remaining_seconds INTEGER,
  spot_price REAL,
  reference_price REAL,
  sigma_per_second REAL,
  market_up_price REAL,
  market_down_price REAL,
  fair_up_prob REAL,
  edge REAL,
  signal_side TEXT,
  confidence REAL,
  notional_usd REAL,
  reason TEXT,
  feed_source TEXT,
  up_best_bid REAL,
  up_best_ask REAL,
  up_bid_size REAL,
  up_ask_size REAL,
  down_best_bid REAL,
  down_best_ask REAL,
  down_bid_size REAL,
  down_ask_size REAL,
  quote_source TEXT,
  gamma_up_price REAL
);
CREATE INDEX IF NOT EXISTS idx_paper_ticks_created
  ON paper_ticks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_paper_ticks_window
  ON paper_ticks(window_slug);

CREATE TABLE IF NOT EXISTS paper_positions (
  position_id INTEGER PRIMARY KEY AUTOINCREMENT,
  opened_at TEXT NOT NULL,
  closed_at TEXT,
  window_slug TEXT NOT NULL,
  market_question TEXT,
  side TEXT NOT NULL,
  state TEXT NOT NULL,
  entry_price REAL NOT NULL,
  exit_price REAL,
  notional_usd REAL NOT NULL,
  shares REAL NOT NULL,
  opened_spot REAL,
  closed_spot REAL,
  confidence REAL,
  edge REAL,
  entry_reason TEXT,
  exit_reason TEXT,
  realized_pnl_usd REAL,
  feed_source TEXT,
  quote_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_paper_positions_state
  ON paper_positions(state);
CREATE INDEX IF NOT EXISTS idx_paper_positions_opened
  ON paper_positions(opened_at DESC);

CREATE TABLE IF NOT EXISTS live_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  window_slug TEXT,
  token_id TEXT,
  intent TEXT NOT NULL,
  side TEXT NOT NULL,
  price REAL,
  size REAL,
  notional_usd REAL,
  order_type TEXT,
  status TEXT NOT NULL,
  clob_order_id TEXT,
  error TEXT,
  details_json TEXT,
  -- 'live' rows are real CLOB attempts. 'paper' rows record what the live
  -- gate WOULD have done for the same signal, so paper is a faithful preview
  -- of live (issue #64). All rows that predate the migration are 'live'.
  mode TEXT
);
CREATE INDEX IF NOT EXISTS idx_live_orders_created
  ON live_orders(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_live_orders_status
  ON live_orders(status);

CREATE TABLE IF NOT EXISTS model_shadow_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT,
  window_slug TEXT,
  model_id TEXT,
  side TEXT,
  entry_price REAL,
  notional_usd REAL,
  shares REAL,
  fair_prob REAL,
  edge REAL,
  confidence REAL,
  reason TEXT,
  state TEXT,
  outcome TEXT,
  settlement_price REAL,
  resolved_at TEXT,
  realized_pnl_usd REAL,
  quote_source TEXT,
  feed_source TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_model_shadow_positions_window_model
  ON model_shadow_positions(window_slug, model_id);

-- Issue #185: daily (24h-window) altcoin Up/Down shadow scanner. A separate
-- table from model_shadow_positions on purpose: it tracks ONE asset-scan
-- decision per day-window (not several competing models per window), and a
-- window_slug already uniquely identifies one (asset, day) pair for this
-- market family, so the idempotency key is window_slug alone.
CREATE TABLE IF NOT EXISTS daily_shadow_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  window_slug TEXT NOT NULL,
  asset TEXT NOT NULL,
  side TEXT NOT NULL,
  entry_price REAL NOT NULL,
  notional_usd REAL NOT NULL,
  shares REAL NOT NULL,
  fair_prob REAL,
  edge REAL,
  confidence REAL,
  reason TEXT,
  state TEXT NOT NULL,
  outcome TEXT,
  settlement_price REAL,
  resolved_at TEXT,
  realized_pnl_usd REAL,
  sigma_per_second REAL,
  drift_per_second REAL,
  -- Settlement is self-contained (recomputed from Binance, not from
  -- Polymarket's own resolution status — a live check found a resolved
  -- market in this family stops being returned by the same discovery query
  -- used to find it while open). Stamped at record time so a later tick
  -- never needs to re-resolve the market to settle it.
  reference_price REAL,
  resolves_at TEXT,
  binance_symbol TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_shadow_positions_window
  ON daily_shadow_positions(window_slug);
CREATE INDEX IF NOT EXISTS idx_daily_shadow_positions_asset
  ON daily_shadow_positions(asset);
"""

LIVE_ORDERS_COLUMN_MIGRATIONS = {
    "mode": "TEXT",
    # Issue #137: the CLOB placement response's status, promoted from
    # details_json so maker/taker attribution is queryable without JSON
    # parsing. 'matched' = crossed at placement (taker, fee paid on the
    # crossed portion); 'live' = rested on the book (maker if later filled,
    # no fee); 'delayed' = venue-throttled. Backfilled from details_json.
    "placement_status": "TEXT",
}

# Issue #122: capture the market state at decision time on each shadow row so
# the regime-attribution instrument (tools/regime_attribution.py) can stratify
# by volatility and basis, not just time-of-day and edge. Additive + nullable —
# rows recorded before this migration stay NULL and are skipped for those axes.
SHADOW_COLUMN_MIGRATIONS = {
    "spot_at_decision": "REAL",
    "reference_at_decision": "REAL",
    "sigma_per_second": "REAL",
    "drift_per_second": "REAL",
}

POSITION_COLUMN_MIGRATIONS = {
    "market_question": "TEXT",
    "exit_price": "REAL",
    "shares": "REAL",
    "opened_spot": "REAL",
    "closed_spot": "REAL",
    "confidence": "REAL",
    "edge": "REAL",
    "entry_reason": "TEXT",
    "exit_reason": "TEXT",
    "realized_pnl_usd": "REAL",
    "feed_source": "TEXT",
    # 'clob' since v0.3.1; NULL rows predate executable CLOB quotes (issue #22)
    # and are excluded from all KPI aggregates (re-baseline).
    "quote_source": "TEXT",
    # 'settle' | 'scalp' since v0.3.2 (issue #28); KPIs aggregate only the
    # active style so baselines from different trade shapes never blend.
    "strategy_style": "TEXT",
    # 'live' | 'paper' — recorded at insert time from whether the live
    # executor was attached. Legacy rows are backfilled by joining
    # live_orders on window_slug.
    "mode": "TEXT",
}

# Issue #22: executable top-of-book quotes journaled per tick. Rows without
# quote_source were priced off stale Gamma outcomePrices and are excluded
# from dashboard KPIs and the paper summary.
TICK_COLUMN_MIGRATIONS = {
    "up_best_bid": "REAL",
    "up_best_ask": "REAL",
    "up_bid_size": "REAL",
    "up_ask_size": "REAL",
    "down_best_bid": "REAL",
    "down_best_ask": "REAL",
    "down_bid_size": "REAL",
    "down_ask_size": "REAL",
    "quote_source": "TEXT",
    "gamma_up_price": "REAL",
}


# --- Issue #185 rebrand migration -------------------------------------------
# The BTC-5m-era table names and config-key namespace are renamed below. Both
# migrations are idempotent (safe to run on every boot) and additive: they
# only touch a table/row that still carries the OLD name, so a DB that has
# already been migrated — or one that never had the old names at all — is a
# no-op. This carries an operator's existing accumulated history (paper/live
# rows, risk-gate counters) forward under the new names instead of silently
# starting fresh.
_TABLE_RENAMES = {
    "btc_paper_ticks": "paper_ticks",
    "btc_paper_positions": "paper_positions",
    "btc_live_orders": "live_orders",
    "btc_model_shadow_positions": "model_shadow_positions",
}

# Config-key namespace prefixes renamed under #185. Deliberately excludes the
# already-orphaned ``btc_bot.*`` prefix (superseded by ``polymarket_bot.*``
# during the #169/#184 package rename, nothing reads it any more) and the
# ``btc_live.*`` prefix, which polymarket_exec.execution.gate's legacy
# fallback intentionally keeps reading under its original literal name.
_CONFIG_KEY_PREFIX_RENAMES = {
    "btc_risk.": "risk.",
    "btc_runtime.": "runtime.",
    "btc_model.": "model.",
    "btc_recon.": "recon.",
}


async def _rename_legacy_tables(db: aiosqlite.Connection) -> None:
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ) as cur:
        existing = {row["name"] for row in await cur.fetchall()}
    for old, new in _TABLE_RENAMES.items():
        if old in existing and new not in existing:
            await db.execute(f"ALTER TABLE {old} RENAME TO {new}")


async def _rename_legacy_config_keys(db: aiosqlite.Connection) -> None:
    async with db.execute("SELECT key FROM config") as cur:
        keys = [row["key"] for row in await cur.fetchall()]
    for old_prefix, new_prefix in _CONFIG_KEY_PREFIX_RENAMES.items():
        for key in keys:
            if not key.startswith(old_prefix):
                continue
            new_key = new_prefix + key[len(old_prefix):]
            await db.execute(
                """
                UPDATE config SET key = ?
                 WHERE key = ?
                   AND NOT EXISTS (SELECT 1 FROM config WHERE key = ?)
                """,
                (new_key, key, new_key),
            )


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@asynccontextmanager
async def connect() -> AsyncIterator[aiosqlite.Connection]:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()


async def init_db() -> None:
    async with connect() as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await _rename_legacy_tables(db)
        await db.executescript(SCHEMA)
        await _rename_legacy_config_keys(db)
        await _migrate_columns(db, "paper_positions", POSITION_COLUMN_MIGRATIONS)
        await _migrate_columns(db, "paper_ticks", TICK_COLUMN_MIGRATIONS)
        await _migrate_columns(db, "live_orders", LIVE_ORDERS_COLUMN_MIGRATIONS)
        await _migrate_columns(
            db, "model_shadow_positions", SHADOW_COLUMN_MIGRATIONS
        )
        await _backfill_position_mode(db)
        await _backfill_live_order_mode(db)
        await _backfill_placement_status(db)
        await db.commit()


async def _backfill_position_mode(db: aiosqlite.Connection) -> None:
    """Fill mode on legacy rows: 'live' iff a SUBMITTED ENTRY exists for the slug.

    A position was real iff the live executor placed a corresponding entry on
    that same window — the journal proves that. All other rows are paper.
    Rows that already carry a mode are left alone.
    """
    await db.execute(
        """
        UPDATE paper_positions
           SET mode = 'live'
         WHERE mode IS NULL
           AND window_slug IN (
               SELECT window_slug FROM live_orders
                WHERE intent = 'ENTRY' AND status = 'SUBMITTED'
           )
        """
    )
    await db.execute(
        "UPDATE paper_positions SET mode = 'paper' WHERE mode IS NULL"
    )


async def _backfill_live_order_mode(db: aiosqlite.Connection) -> None:
    """Every pre-migration row in live_orders is real CLOB activity → 'live'."""
    await db.execute(
        "UPDATE live_orders SET mode = 'live' WHERE mode IS NULL"
    )


async def _backfill_placement_status(db: aiosqlite.Connection) -> None:
    """Promote details_json→response.status into placement_status (#137).

    The CLOB placement response was journaled verbatim from day one, so the
    maker/taker signal already exists for every historical order — this lifts
    it into the queryable column for rows that predate the migration.
    """
    await db.execute(
        """
        UPDATE live_orders
           SET placement_status = json_extract(details_json, '$.response.status')
         WHERE placement_status IS NULL
           AND details_json IS NOT NULL
           AND json_valid(details_json)
        """
    )


async def _migrate_columns(
    db: aiosqlite.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        existing = {row["name"] for row in await cur.fetchall()}
    for column, column_type in columns.items():
        if column not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


async def get_config(key: str, default: str | None = None) -> str | None:
    async with connect() as db:
        async with db.execute("SELECT value FROM config WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
    return row["value"] if row else default


async def set_config(key: str, value: str | None) -> None:
    # The dashboard "detail" line is stored here and rendered in the UI;
    # scrub any secret that a stringified exception might have carried in.
    value = redact_secrets(value)
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO config(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at
            """,
            (key, value, utc_now_iso()),
        )
        await db.commit()


async def journal_live_order(
    *,
    intent: str,
    side: str,
    status: str,
    window_slug: str | None = None,
    token_id: str | None = None,
    price: float | None = None,
    size: float | None = None,
    notional_usd: float | None = None,
    order_type: str | None = None,
    clob_order_id: str | None = None,
    error: str | None = None,
    details: dict[str, Any] | None = None,
    mode: str = "live",
) -> None:
    """Append one order/fill/cancel attempt to the live_orders journal.

    ``mode`` is 'live' for real CLOB activity and 'paper' for paper-side
    BLOCKED rows surfaced by the shared RiskGate (issue #64).

    ``placement_status`` (#137) is derived here from the CLOB placement
    response carried in ``details`` ('matched' = crossed at placement/taker,
    'live' = rested/maker-eligible) so maker/taker attribution is queryable
    without JSON parsing; NULL when the details carry no response status.
    """
    error = redact_secrets(error)
    placement_status: str | None = None
    if isinstance(details, dict):
        response = details.get("response")
        if isinstance(response, dict):
            raw = response.get("status")
            if isinstance(raw, str) and raw:
                placement_status = raw
    payload = redact_secrets(json.dumps(details or {}, sort_keys=True, default=str))
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO live_orders(
              created_at, window_slug, token_id, intent, side, price, size,
              notional_usd, order_type, status, clob_order_id, error,
              details_json, mode, placement_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now_iso(),
                window_slug,
                token_id,
                intent,
                side,
                price,
                size,
                notional_usd,
                order_type,
                status,
                clob_order_id,
                error,
                payload,
                mode,
                placement_status,
            ),
        )
        await db.commit()


async def notify(
    event_type: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    # Scrub secrets at the sink: any caller-supplied string is persisted here.
    message = redact_secrets(message)
    payload = redact_secrets(json.dumps(details or {}, sort_keys=True, default=str))
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO notification_feed(created_at, event_type, message, details_json)
            VALUES (?, ?, ?, ?)
            """,
            (utc_now_iso(), event_type, message, payload),
        )
        await db.commit()
