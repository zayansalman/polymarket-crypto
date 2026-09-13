"""Market data recorder — persists raw market snapshots to SQLite for deterministic replay.

Usage::

    recorder = MarketDataRecorder(db_path=Path("/data/market.db"))
    await recorder.init()
    await recorder.record_window(window)
    await recorder.record_tick(tick)
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from polymarket_exec.core.types import MarketWindow, SignalAction, Side, Tick


class MarketDataRecorder:
    """Records and retrieves market windows, ticks, and CLOB snapshots.

    All data is stored in SQLite so that a :class:`DeterministicReplay`
    can later read the *exact* same sequence of ticks and reproduce
    signals bit-for-bit.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS recorded_windows (
        slug TEXT PRIMARY KEY,
        question TEXT,
        start_ts INTEGER,
        end_ts INTEGER,
        up_price REAL,
        down_price REAL,
        recorded_at TEXT
    );
    CREATE TABLE IF NOT EXISTS recorded_ticks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        window_slug TEXT,
        ts TEXT,
        spot_price REAL,
        reference_price REAL,
        sigma_per_second REAL,
        fair_up_prob REAL,
        signal_action TEXT,
        signal_side TEXT,
        signal_confidence REAL,
        signal_notional REAL,
        signal_edge REAL,
        feed_source TEXT,
        FOREIGN KEY (window_slug) REFERENCES recorded_windows(slug)
    );
    CREATE TABLE IF NOT EXISTS clob_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        window_slug TEXT,
        bid REAL,
        ask REAL,
        spread REAL,
        quote_ts TEXT,
        freshness_ms INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_ticks_window ON recorded_ticks(window_slug);
    CREATE INDEX IF NOT EXISTS idx_ticks_ts ON recorded_ticks(ts);
    CREATE INDEX IF NOT EXISTS idx_clob_window ON clob_snapshots(window_slug);
    """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        """Create tables and indices. Safe to call multiple times."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(self.SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        """Close the underlying database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    async def record_window(self, window: MarketWindow) -> None:
        """Persist a :class:`MarketWindow`."""
        if self._db is None:
            raise RuntimeError("Recorder not initialized — call init() first")
        await self._db.execute(
            """
            INSERT OR REPLACE INTO recorded_windows
            (slug, question, start_ts, end_ts, up_price, down_price, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                window.slug,
                window.question,
                window.start_ts,
                window.end_ts,
                window.up_price,
                window.down_price,
                _now_iso(),
            ),
        )
        await self._db.commit()

    async def record_tick(self, tick: Tick) -> None:
        """Persist a :class:`Tick` (including its embedded signal)."""
        if self._db is None:
            raise RuntimeError("Recorder not initialized — call init() first")
        sig = tick.signal
        await self._db.execute(
            """
            INSERT INTO recorded_ticks
            (window_slug, ts, spot_price, reference_price, sigma_per_second,
             fair_up_prob, signal_action, signal_side, signal_confidence,
             signal_notional, signal_edge, feed_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tick.window.slug,
                tick.ts.isoformat(),
                tick.spot_price,
                tick.reference_price,
                tick.sigma_per_second,
                tick.fair_up_prob,
                sig.action.name,
                sig.side.value if sig.side is not None else None,
                sig.confidence,
                sig.notional_usd,
                sig.edge,
                tick.feed_source,
            ),
        )
        await self._db.commit()

    async def record_clob_snapshot(
        self,
        window_slug: str,
        bid: float,
        ask: float,
        spread: float,
        timestamp: int,
        freshness_ms: int = 0,
    ) -> None:
        """Persist a CLOB (order-book) snapshot.

        Parameters:
            window_slug: Identifies the market window this book belongs to.
            bid: Best bid price.
            ask: Best ask price.
            spread: Bid-ask spread (in price terms or bps).
            timestamp: Unix seconds.
            freshness_ms: Age of the quote in milliseconds.
        """
        if self._db is None:
            raise RuntimeError("Recorder not initialized — call init() first")
        await self._db.execute(
            """
            INSERT INTO clob_snapshots
            (window_slug, bid, ask, spread, quote_ts, freshness_ms)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                window_slug,
                bid,
                ask,
                spread,
                datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(),
                freshness_ms,
            ),
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    async def get_recorded_windows(
        self, start_ts: int, end_ts: int
    ) -> list[MarketWindow]:
        """Return all windows whose *start_ts* falls in [*start_ts*, *end_ts*]."""
        if self._db is None:
            raise RuntimeError("Recorder not initialized — call init() first")
        cursor = await self._db.execute(
            """
            SELECT slug, question, start_ts, end_ts, up_price, down_price
            FROM recorded_windows
            WHERE start_ts >= ? AND start_ts <= ?
            ORDER BY start_ts ASC
            """,
            (start_ts, end_ts),
        )
        rows = await cursor.fetchall()
        return [_row_to_window(row) for row in rows]

    async def get_ticks_for_window(self, window_slug: str) -> list[Tick]:
        """Return every tick recorded for *window_slug*, ordered by ts."""
        if self._db is None:
            raise RuntimeError("Recorder not initialized — call init() first")
        # First fetch the window
        window = await self._get_window_by_slug(window_slug)
        if window is None:
            return []

        cursor = await self._db.execute(
            """
            SELECT ts, spot_price, reference_price, sigma_per_second,
                   fair_up_prob, signal_action, signal_side, signal_confidence,
                   signal_notional, signal_edge, feed_source
            FROM recorded_ticks
            WHERE window_slug = ?
            ORDER BY ts ASC
            """,
            (window_slug,),
        )
        rows = await cursor.fetchall()
        return [_row_to_tick(row, window) for row in rows]

    async def get_clob_for_window(self, window_slug: str) -> list[dict]:
        """Return all CLOB snapshots for *window_slug*, ordered by quote_ts."""
        if self._db is None:
            raise RuntimeError("Recorder not initialized — call init() first")
        cursor = await self._db.execute(
            """
            SELECT bid, ask, spread, quote_ts, freshness_ms
            FROM clob_snapshots
            WHERE window_slug = ?
            ORDER BY quote_ts ASC
            """,
            (window_slug,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def _get_window_by_slug(self, slug: str) -> Optional[MarketWindow]:
        """Fetch a single window by its slug."""
        if self._db is None:
            return None
        cursor = await self._db.execute(
            "SELECT * FROM recorded_windows WHERE slug = ?", (slug,)
        )
        row = await cursor.fetchone()
        return _row_to_window(row) if row else None

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    async def get_window_count(self) -> int:
        """Total number of recorded windows."""
        if self._db is None:
            raise RuntimeError("Recorder not initialized — call init() first")
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM recorded_windows"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_tick_count(self) -> int:
        """Total number of recorded ticks."""
        if self._db is None:
            raise RuntimeError("Recorder not initialized — call init() first")
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM recorded_ticks"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_coverage_report(self) -> dict[str, Any]:
        """Return a summary of what has been recorded."""
        total_windows = await self.get_window_count()
        total_ticks = await self.get_tick_count()

        windows_without_ticks = 0
        if self._db is not None:
            cursor = await self._db.execute(
                """
                SELECT w.slug FROM recorded_windows w
                LEFT JOIN recorded_ticks t ON w.slug = t.window_slug
                WHERE t.id IS NULL
                """
            )
            rows = await cursor.fetchall()
            windows_without_ticks = len(rows)

        time_range: dict[str, Any] = {"start_ts": None, "end_ts": None}
        if self._db is not None:
            cursor = await self._db.execute(
                "SELECT MIN(start_ts), MAX(end_ts) FROM recorded_windows"
            )
            row = await cursor.fetchone()
            if row and row[0] is not None:
                time_range = {
                    "start_ts": row[0],
                    "end_ts": row[1],
                }

        return {
            "total_windows": total_windows,
            "total_ticks": total_ticks,
            "windows_without_ticks": windows_without_ticks,
            "time_range": time_range,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_window(row: aiosqlite.Row) -> MarketWindow:
    return MarketWindow(
        slug=row["slug"],
        question=row["question"],
        start_ts=row["start_ts"],
        end_ts=row["end_ts"],
        up_price=row["up_price"],
        down_price=row["down_price"],
    )


def _row_to_tick(row: aiosqlite.Row, window: MarketWindow) -> Tick:
    from polymarket_exec.core.types import Signal

    # Parse the ISO timestamp
    ts_raw = row["ts"]
    if isinstance(ts_raw, str):
        ts = datetime.fromisoformat(ts_raw)
    else:
        ts = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)

    # Reconstruct the signal
    side: Optional[Side] = None
    side_str = row["signal_side"]
    if side_str is not None:
        side = Side.UP if side_str == "Up" else Side.DOWN

    action = SignalAction[row["signal_action"]]

    signal = Signal(
        action=action,
        side=side,
        confidence=row["signal_confidence"],
        notional_usd=row["signal_notional"],
        edge=row["signal_edge"],
        fair_up_prob=row["fair_up_prob"],
        reason=f"replay: {action.name} {side.value if side else '-'} edge={row['signal_edge']:+.4f}",
    )

    return Tick(
        ts=ts,
        window=window,
        spot_price=row["spot_price"],
        reference_price=row["reference_price"],
        sigma_per_second=row["sigma_per_second"],
        fair_up_prob=row["fair_up_prob"],
        signal=signal,
        feed_source=row["feed_source"],
    )
