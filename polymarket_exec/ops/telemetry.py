"""Feed health telemetry and latency tracking for the BTC 5m pricing-model system.

Usage::

    tracker = FeedHealthTracker(window_seconds=3600)
    await tracker.record_tick_latency("binance", 45.2)
    await tracker.record_feed_heartbeat("binance")
    await tracker.record_error("binance", "timeout")
    health = await tracker.get_health("binance")
    report = await tracker.get_health_report()

    lat_tracker = LatencyTracker(max_samples=1000)
    await lat_tracker.record_loop_duration(120.5)
    await lat_tracker.record_http_latency("/api/v1/ticker", 67.3)
    summary = await lat_tracker.get_summary()
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

# ---------------------------------------------------------------------------
# Telemetry schema ( additive to existing DB )
# ---------------------------------------------------------------------------

TELEMETRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS feed_health_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    source TEXT NOT NULL,
    latency_ms REAL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feed_health_source
    ON feed_health_log(source, timestamp DESC);

CREATE TABLE IF NOT EXISTS loop_duration_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    duration_ms REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_loop_duration_ts
    ON loop_duration_log(timestamp DESC);

CREATE TABLE IF NOT EXISTS error_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    source TEXT NOT NULL,
    error_type TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_error_log_source
    ON error_log(source, timestamp DESC);
"""

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FeedHealth:
    """Aggregated health snapshot for a single feed source."""

    source: str
    status: str  # "healthy", "degraded", "down"
    last_heartbeat: datetime | None
    last_latency_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    error_count_1h: int
    detail: str


# ---------------------------------------------------------------------------
# Percentile helper
# ---------------------------------------------------------------------------


def _percentile(sorted_values: list[float], p: float) -> float:
    """Return the *p*-th percentile of a sorted list using linear interpolation."""
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    # Use nearest-rank method with linear interpolation
    idx = (p / 100.0) * (n - 1)
    lower = int(idx)
    upper = lower + 1
    if upper >= n:
        return sorted_values[-1]
    frac = idx - lower
    return sorted_values[lower] + frac * (sorted_values[upper] - sorted_values[lower])


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# FeedHealthTracker
# ---------------------------------------------------------------------------


class FeedHealthTracker:
    """Tracks per-feed latency, heartbeats, and errors.

    Data is persisted to SQLite so that historical analysis and dashboards
    can query it directly.  An in-memory deque buffers recent latencies for
    fast percentile calculations.
    """

    def __init__(self, window_seconds: int = 3600) -> None:
        self._window_seconds = window_seconds
        self._latencies: dict[str, deque[float]] = {}
        self._heartbeats: dict[str, datetime] = {}
        self._db_path: Path | None = None

    async def init_db(self, db_path: Path) -> None:
        """Create telemetry tables in the SQLite database at *db_path*.

        Safe to call multiple times — tables are created with
        ``IF NOT EXISTS``.
        """
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(db_path)) as db:
            await db.executescript(TELEMETRY_SCHEMA)
            await db.commit()

    # -- Recording ----------------------------------------------------------

    async def record_tick_latency(self, source: str, latency_ms: float) -> None:
        """Record the latency (in ms) of a single tick from *source*."""
        self._latencies.setdefault(source, deque(maxlen=10_000)).append(latency_ms)
        self._heartbeats[source] = datetime.now(UTC)
        if self._db_path is not None:
            async with aiosqlite.connect(str(self._db_path)) as db:
                await db.execute(
                    """
                    INSERT INTO feed_health_log (timestamp, source, latency_ms, status)
                    VALUES (?, ?, ?, ?)
                    """,
                    (_now_iso(), source, latency_ms, "healthy"),
                )
                await db.commit()

    async def record_feed_heartbeat(self, source: str) -> None:
        """Record a heartbeat (successful feed update) from *source*."""
        self._heartbeats[source] = datetime.now(UTC)
        if self._db_path is not None:
            async with aiosqlite.connect(str(self._db_path)) as db:
                await db.execute(
                    """
                    INSERT INTO feed_health_log (timestamp, source, latency_ms, status)
                    VALUES (?, ?, ?, ?)
                    """,
                    (_now_iso(), source, 0.0, "heartbeat"),
                )
                await db.commit()

    async def record_error(self, source: str, error: str) -> None:
        """Record an error from *source*.  The error string is stored as-is."""
        if self._db_path is not None:
            error_type = error.split(":")[0] if ":" in error else error
            async with aiosqlite.connect(str(self._db_path)) as db:
                await db.execute(
                    """
                    INSERT INTO error_log (timestamp, source, error_type, message)
                    VALUES (?, ?, ?, ?)
                    """,
                    (_now_iso(), source, error_type, error),
                )
                await db.commit()

    # -- Queries ------------------------------------------------------------

    async def get_health(self, source: str) -> FeedHealth:
        """Return a :class:`FeedHealth` snapshot for *source*."""
        latencies = list(self._latencies.get(source, deque()))
        sorted_lat = sorted(latencies) if latencies else []

        p50 = _percentile(sorted_lat, 50.0)
        p95 = _percentile(sorted_lat, 95.0)
        p99 = _percentile(sorted_lat, 99.0)
        last_lat = latencies[-1] if latencies else 0.0

        last_hb = self._heartbeats.get(source)
        status = self._determine_status(last_hb, sorted_lat)
        error_count = await self._error_count_last_hour(source)

        detail = self._build_detail(status, last_hb, last_lat, error_count)

        return FeedHealth(
            source=source,
            status=status,
            last_heartbeat=last_hb,
            last_latency_ms=last_lat,
            p50_ms=p50,
            p95_ms=p95,
            p99_ms=p99,
            error_count_1h=error_count,
            detail=detail,
        )

    async def get_all_health(self) -> dict[str, FeedHealth]:
        """Return health snapshots for every source seen so far."""
        sources = set(self._latencies.keys()) | set(self._heartbeats.keys())
        return {source: await self.get_health(source) for source in sources}

    async def get_health_report(self) -> str:
        """Return a human-readable Markdown health report."""
        all_health = await self.get_all_health()
        if not all_health:
            return "## Feed Health Report\n\nNo feeds monitored yet.\n"

        lines: list[str] = [
            "## Feed Health Report",
            f"\n_Generated at {datetime.now(UTC).isoformat()}_\n",
            "| Source | Status | p50 (ms) | p95 (ms) | p99 (ms) | Errors (1h) | Detail |",
            "|--------|--------|----------|----------|----------|-------------|--------|",
        ]
        for source, h in sorted(all_health.items()):
            status_emoji = "🟢" if h.status == "healthy" else "🟡" if h.status == "degraded" else "🔴"
            lines.append(
                f"| {source} | {status_emoji} {h.status} | {h.p50_ms:.1f} | "
                f"{h.p95_ms:.1f} | {h.p99_ms:.1f} | {h.error_count_1h} | {h.detail} |"
            )
        return "\n".join(lines) + "\n"

    # -- Internal helpers ---------------------------------------------------

    def _determine_status(
        self, last_hb: datetime | None, sorted_lat: list[float]
    ) -> str:
        """Classify feed status based on heartbeat recency and latency."""
        now = datetime.now(UTC)
        if last_hb is not None and (now - last_hb).total_seconds() > 60:
            return "down"
        if sorted_lat:
            p95 = _percentile(sorted_lat, 95.0)
            if p95 > 5_000:  # > 5 seconds
                return "degraded"
        if last_hb is None and not sorted_lat:
            return "down"
        return "healthy"

    def _build_detail(
        self,
        status: str,
        last_hb: datetime | None,
        last_lat: float,
        error_count: int,
    ) -> str:
        """Build a human-readable detail string."""
        if status == "down":
            if last_hb is None:
                return "No heartbeat received yet"
            ago = (datetime.now(UTC) - last_hb).total_seconds()
            return f"Last heartbeat {ago:.0f}s ago"
        if status == "degraded":
            return f"p95 latency high ({last_lat:.1f}ms), errors={error_count}"
        return f"Nominal — last latency {last_lat:.1f}ms"

    async def _error_count_last_hour(self, source: str) -> int:
        """Count errors for *source* in the last hour from SQLite."""
        if self._db_path is None:
            return 0
        from_iso = (
            datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
            - __import__("datetime").timedelta(hours=1)
        ).isoformat()
        # Simpler: just count all errors for this source when no DB
        async with aiosqlite.connect(str(self._db_path)) as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM error_log
                WHERE source = ? AND timestamp >= ?
                """,
                (source, from_iso),
            )
            row = await cursor.fetchone()
            return row[0] if row else 0


# ---------------------------------------------------------------------------
# LatencyTracker
# ---------------------------------------------------------------------------


class LatencyTracker:
    """Tracks loop-duration and HTTP-request latency percentiles.

    Stores samples in memory for fast queries; optionally persists to
    the same SQLite database used by :class:`FeedHealthTracker`.
    """

    def __init__(self, max_samples: int = 1000) -> None:
        self._max_samples = max_samples
        self._loop_durations: deque[float] = deque(maxlen=max_samples)
        self._http_latencies: dict[str, deque[float]] = {}
        self._db_path: Path | None = None

    async def init_db(self, db_path: Path) -> None:
        """Create telemetry tables (shared schema with FeedHealthTracker)."""
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(db_path)) as db:
            await db.executescript(TELEMETRY_SCHEMA)
            await db.commit()

    # -- Recording ----------------------------------------------------------

    async def record_loop_duration(self, duration_ms: float) -> None:
        """Record one tick-loop duration in milliseconds."""
        self._loop_durations.append(duration_ms)
        if self._db_path is not None:
            async with aiosqlite.connect(str(self._db_path)) as db:
                await db.execute(
                    """
                    INSERT INTO loop_duration_log (timestamp, duration_ms)
                    VALUES (?, ?)
                    """,
                    (_now_iso(), duration_ms),
                )
                await db.commit()

    async def record_http_latency(self, endpoint: str, latency_ms: float) -> None:
        """Record an HTTP request latency for *endpoint*."""
        self._http_latencies.setdefault(
            endpoint, deque(maxlen=self._max_samples)
        ).append(latency_ms)

    # -- Queries ------------------------------------------------------------

    async def get_summary(self) -> dict:
        """Return aggregated latency statistics.

        Keys:
            - ``loop_p50_ms``, ``loop_p95_ms``, ``loop_p99_ms`` — loop durations
            - ``loop_min_ms``, ``loop_max_ms`` — loop extremes
            - ``loop_count`` — number of loop samples
            - ``http`` — dict mapping endpoint -> ``{"p50": .., "p95": .., "p99": .., "count": ..}``
        """
        loop_sorted = sorted(self._loop_durations)
        result: dict = {
            "loop_p50_ms": _percentile(loop_sorted, 50.0),
            "loop_p95_ms": _percentile(loop_sorted, 95.0),
            "loop_p99_ms": _percentile(loop_sorted, 99.0),
            "loop_min_ms": loop_sorted[0] if loop_sorted else 0.0,
            "loop_max_ms": loop_sorted[-1] if loop_sorted else 0.0,
            "loop_count": len(loop_sorted),
            "http": {},
        }
        for endpoint, samples in self._http_latencies.items():
            s = sorted(samples)
            result["http"][endpoint] = {
                "p50_ms": _percentile(s, 50.0),
                "p95_ms": _percentile(s, 95.0),
                "p99_ms": _percentile(s, 99.0),
                "min_ms": s[0] if s else 0.0,
                "max_ms": s[-1] if s else 0.0,
                "count": len(s),
            }
        return result
