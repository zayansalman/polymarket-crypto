"""SQLite read/write for venue flow hour bars and perp venue snapshots."""
from __future__ import annotations

from typing import Any

import db as _db
from polymarket_exec.connectors.venue_flow import HourBar, VenueSnapshot


async def upsert_hour_bars(bars: list[HourBar]) -> int:
    """Write bars; an existing row is replaced only by one that is at least as complete."""
    if not bars:
        return 0
    recorded_at = _db.utc_now_iso()
    async with _db.connect() as conn:
        await conn.executemany(
            """
            INSERT INTO venue_flow_hourly(
              venue, symbol, hour_start_ms, open, high, low, close, volume,
              taker_buy_volume, trades, complete, source, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(venue, symbol, hour_start_ms) DO UPDATE SET
              open = excluded.open, high = excluded.high, low = excluded.low,
              close = excluded.close, volume = excluded.volume,
              taker_buy_volume = excluded.taker_buy_volume, trades = excluded.trades,
              complete = excluded.complete, source = excluded.source,
              recorded_at = excluded.recorded_at
            WHERE excluded.complete >= venue_flow_hourly.complete
            """,
            [
                (
                    b.venue, b.symbol, b.hour_start_ms, b.open, b.high, b.low, b.close,
                    b.volume, b.taker_buy_volume, b.trades, int(b.complete), b.source,
                    recorded_at,
                )
                for b in bars
            ],
        )
        await conn.commit()
    return len(bars)


async def insert_snapshot(snap: VenueSnapshot) -> None:
    async with _db.connect() as conn:
        await conn.execute(
            """
            INSERT INTO venue_snapshot(
              venue, symbol, taken_at_ms, mark_price, index_price, funding_rate,
              next_funding_ms, open_interest, source, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snap.venue, snap.symbol, snap.taken_at_ms, snap.mark_price, snap.index_price,
                snap.funding_rate, snap.next_funding_ms, snap.open_interest, snap.source,
                _db.utc_now_iso(),
            ),
        )
        await conn.commit()


async def recent_hours(venue: str, symbol: str, limit: int) -> list[dict[str, Any]]:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM venue_flow_hourly WHERE venue = ? AND symbol = ? "
            "ORDER BY hour_start_ms DESC LIMIT ?",
            (venue, symbol, limit),
        )
        return [dict(r) for r in await cur.fetchall()]


async def latest_snapshot(venue: str, symbol: str) -> dict[str, Any] | None:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM venue_snapshot WHERE venue = ? AND symbol = ? "
            "ORDER BY taken_at_ms DESC LIMIT 1",
            (venue, symbol),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
