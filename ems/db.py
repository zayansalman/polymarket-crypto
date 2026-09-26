"""SQLite storage for the local Polymarket crypto trading lab."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, AsyncIterator

import aiosqlite

from ems.config import DB_PATH
from ems.logging_setup import redact_secrets


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

-- Fade 1h Momentum on 15m: a paper-only strategy (ems/fade_1h_momentum_15m/,
-- read and written only through its ledger.py). Its own tables, never paper_positions:
-- Stop and the PAPER/LIVE toggle force-close every open paper_positions row at the BTC 5m
-- price. Timestamps are integer epoch seconds, like the window bounds they are compared with.

-- One row per coin per 15m window the strategy saw, traded or not: the learner needs the
-- outcome of every window, not only the traded ones.
CREATE TABLE IF NOT EXISTS fade_windows (
  window_slug        TEXT PRIMARY KEY,
  asset              TEXT NOT NULL,
  window_start       INTEGER NOT NULL,
  window_end         INTEGER NOT NULL,
  hour_start         INTEGER NOT NULL,
  condition_id       TEXT,
  up_token           TEXT,
  down_token         TEXT,
  -- The Chainlink TWAP-60s print at the window start (Gamma's priceToBeat). A 15m window
  -- settles Up iff the TWAP-60s print at the window's close (the average of its last 60 s)
  -- is >= this opening print.
  start_ref_price    REAL,
  start_ref_source   TEXT,
  -- The 1h market this window sits in (settles on the Binance 1h candle, close >= open).
  hour_slug          TEXT,
  hour_condition_id  TEXT,
  outcome            TEXT CHECK (outcome IN ('Up', 'Down')),
  settled_ts         INTEGER,
  -- Sum of this window's order P&L: a hedged pair is one bet.
  net_pnl            REAL,
  created_ts         INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fade_windows_due
  ON fade_windows(outcome, window_end);

CREATE INDEX IF NOT EXISTS idx_fade_windows_asset
  ON fade_windows(asset, window_start);

-- One row per coin per pass: every input the maths saw (so p can be recomputed under new
-- dials), what it said, and why nothing was placed when nothing was.
CREATE TABLE IF NOT EXISTS fade_decisions (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ts             INTEGER NOT NULL,
  asset          TEXT NOT NULL,
  window_slug    TEXT,
  mode           TEXT,
  dials_version  INTEGER,
  inputs_json    TEXT NOT NULL,
  p_model        REAL,
  p              REAL,
  side           TEXT CHECK (side IN ('Up', 'Down')),
  kelly_f        REAL,
  stake_usd      REAL,
  -- The parent order's child orders: one per price level, as planned this pass.
  child_orders_json TEXT,
  hedge_json     TEXT,
  factors_json   TEXT,
  action         TEXT NOT NULL,
  reason         TEXT
);

CREATE INDEX IF NOT EXISTS idx_fade_decisions_asset
  ON fade_decisions(asset, id);

CREATE INDEX IF NOT EXISTS idx_fade_decisions_window
  ON fade_decisions(window_slug);

-- One row per paper child order: a passive limit order resting at one price level. A BUY
-- (kind 'entry') buys the token; a SELL (kind 'hedge') sells shares of it already held, never
-- more, so the strategy never holds both outcomes. Size changes cancel or add whole child
-- orders, so each row keeps its own place in the queue and its own stretch of the tape.
-- Life: resting -> partial -> filled, or it stops resting as cancelled / expired with
-- whatever it filled. Settlement is separate: won, pnl and settled_ts are set when the
-- window resolves. Every fill is a passive fill at our own price, so there is no fee.
CREATE TABLE IF NOT EXISTS fade_orders (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  window_slug     TEXT NOT NULL REFERENCES fade_windows(window_slug),
  token_id        TEXT NOT NULL,
  -- The outcome the token pays on.
  side            TEXT NOT NULL CHECK (side IN ('Up', 'Down')),
  kind            TEXT NOT NULL CHECK (kind IN ('entry', 'hedge')),
  -- The price level's place in its parent order: 0 is the level nearest the touch.
  level           INTEGER NOT NULL DEFAULT 0,
  price           REAL NOT NULL,
  shares          REAL NOT NULL,
  -- BUY for an entry, SELL for a hedge (a sale of shares held).
  order_side      TEXT NOT NULL DEFAULT 'BUY' CHECK (order_side IN ('BUY', 'SELL')),
  -- Displayed shares at our price or better when placed (bids for a BUY, asks for a SELL):
  -- the depth ahead of us.
  depth_ahead     REAL NOT NULL DEFAULT 0,
  -- The same depth price level by price level, best first, as [price, shares] pairs; each
  -- level is used up as the tape trades through it, so this holds what is still ahead.
  levels_ahead_json TEXT,
  -- Tape volume that has reached our price level since placement.
  crossed         REAL NOT NULL DEFAULT 0,
  state           TEXT NOT NULL DEFAULT 'resting'
                  CHECK (state IN ('resting', 'partial', 'filled', 'cancelled', 'expired')),
  -- Rests from here (inclusive): the second after the write, so no trade already on the
  -- tape can fill it. It stops resting at cancelled_ts (exclusive).
  placed_ts       INTEGER NOT NULL,
  cancelled_ts    INTEGER,
  cancel_reason   TEXT,
  filled_ts       INTEGER,
  filled_shares   REAL NOT NULL DEFAULT 0,
  fill_price      REAL,
  -- The tape has been read for this order up to here (exclusive).
  flow_cursor_ts  INTEGER,
  decision_id     INTEGER,
  mode            TEXT NOT NULL DEFAULT 'paper',
  -- 1 if the order's token paid $1 at settlement.
  won             INTEGER,
  -- BUY: filled x (payout - price). SELL: filled x (price - payout).
  pnl             REAL,
  settled_ts      INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_fade_orders_key
  ON fade_orders(window_slug, token_id, kind, level, placed_ts);

CREATE INDEX IF NOT EXISTS idx_fade_orders_state
  ON fade_orders(state);

CREATE INDEX IF NOT EXISTS idx_fade_orders_window
  ON fade_orders(window_slug);

-- Versioned parameter sets: version 0 is the prior, each learner step adds one.
CREATE TABLE IF NOT EXISTS fade_dials (
  version      INTEGER PRIMARY KEY,
  ts           INTEGER NOT NULL,
  source       TEXT NOT NULL,
  params_json  TEXT NOT NULL,
  n_windows    INTEGER NOT NULL DEFAULT 0,
  loglik       REAL,
  note         TEXT
);
"""

# Fade 1h Momentum on 15m tables. Each dict lists every column an older copy of the table
# can gain (nullable, or NOT NULL with a default). A new column goes in the CREATE above AND
# here; test_fade1h_ledger checks the two agree. A column an index in SCHEMA uses cannot be
# added this way (SCHEMA's CREATE INDEX runs before the migrations), so those stay in the
# CREATE only.
FADE_WINDOW_COLUMN_MIGRATIONS = {
    "condition_id": "TEXT",
    "up_token": "TEXT",
    "down_token": "TEXT",
    "start_ref_price": "REAL",
    "start_ref_source": "TEXT",
    "hour_slug": "TEXT",
    "hour_condition_id": "TEXT",
    "settled_ts": "INTEGER",
    "net_pnl": "REAL",
}

FADE_DECISION_COLUMN_MIGRATIONS = {
    "mode": "TEXT",
    "dials_version": "INTEGER",
    "p_model": "REAL",
    "p": "REAL",
    "side": "TEXT",
    "kelly_f": "REAL",
    "stake_usd": "REAL",
    "child_orders_json": "TEXT",
    "hedge_json": "TEXT",
    "factors_json": "TEXT",
    "reason": "TEXT",
}

FADE_ORDER_COLUMN_MIGRATIONS = {
    "order_side": "TEXT NOT NULL DEFAULT 'BUY' CHECK (order_side IN ('BUY', 'SELL'))",
    "depth_ahead": "REAL NOT NULL DEFAULT 0",
    "levels_ahead_json": "TEXT",
    "crossed": "REAL NOT NULL DEFAULT 0",
    "cancelled_ts": "INTEGER",
    "cancel_reason": "TEXT",
    "filled_ts": "INTEGER",
    "filled_shares": "REAL NOT NULL DEFAULT 0",
    "fill_price": "REAL",
    "flow_cursor_ts": "INTEGER",
    "decision_id": "INTEGER",
    "mode": "TEXT NOT NULL DEFAULT 'paper'",
    "won": "INTEGER",
    "pnl": "REAL",
    "settled_ts": "INTEGER",
}

FADE_DIALS_COLUMN_MIGRATIONS = {
    "n_windows": "INTEGER NOT NULL DEFAULT 0",
    "loglik": "REAL",
    "note": "TEXT",
}


# --- Issue #185 rebrand migration -------------------------------------------
# The BTC-5m-era table names and config-key namespace are renamed below. Both
# migrations are idempotent (safe to run on every boot) and additive: they
# only touch a table/row that still carries the OLD name, so a DB that has
# already been migrated — or one that never had the old names at all — is a
# no-op. This carries an operator's existing accumulated history (paper/live
# rows, risk-gate counters) forward under the new names instead of silently


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
        await db.executescript(SCHEMA)
        await _migrate_columns(db, "fade_windows", FADE_WINDOW_COLUMN_MIGRATIONS)
        await _migrate_columns(db, "fade_decisions", FADE_DECISION_COLUMN_MIGRATIONS)
        await _migrate_columns(db, "fade_orders", FADE_ORDER_COLUMN_MIGRATIONS)
        await _migrate_columns(db, "fade_dials", FADE_DIALS_COLUMN_MIGRATIONS)
        await db.commit()


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
