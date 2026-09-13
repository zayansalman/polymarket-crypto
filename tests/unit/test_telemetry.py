"""Unit tests for FeedHealthTracker and LatencyTracker."""

from __future__ import annotations

from pathlib import Path

import pytest

from polymarket_exec.ops.telemetry import FeedHealthTracker, LatencyTracker


# ---------------------------------------------------------------------------
# FeedHealthTracker tests
# ---------------------------------------------------------------------------


class TestFeedHealthTracker:
    """Test suite for the feed health monitoring subsystem."""

    @pytest.fixture
    async def tracker(self, tmp_db_path: Path) -> FeedHealthTracker:
        t = FeedHealthTracker(window_seconds=3600)
        await t.init_db(tmp_db_path)
        return t

    @pytest.mark.asyncio
    async def test_record_tick_latency_increments(self, tmp_db_path: Path) -> None:
        """Recording latencies should increase the internal buffer."""
        tracker = FeedHealthTracker(window_seconds=3600)
        await tracker.init_db(tmp_db_path)

        for i in range(5):
            await tracker.record_tick_latency("binance", 10.0 + i)

        health = await tracker.get_health("binance")
        assert health.source == "binance"
        assert health.last_latency_ms == 14.0  # last value
        assert health.status == "healthy"

    @pytest.mark.asyncio
    async def test_p50_p95_p99_calculations(self, tmp_db_path: Path) -> None:
        """Percentiles should be computed correctly from recorded latencies."""
        tracker = FeedHealthTracker(window_seconds=3600)
        await tracker.init_db(tmp_db_path)

        # 100 latencies: 1..100 ms
        for i in range(1, 101):
            await tracker.record_tick_latency("binance", float(i))

        health = await tracker.get_health("binance")
        assert health.p50_ms == 50.5  # median of 1..100
        # Linear interpolation: p95 idx = 0.95 * 99 = 94.05 → 95 + 0.05*(96-95) = 95.05
        assert health.p95_ms == 95.05
        # p99 idx = 0.99 * 99 = 98.01 → 99 + 0.01*(100-99) = 99.01
        assert health.p99_ms == 99.01
        assert health.last_latency_ms == 100.0

    @pytest.mark.asyncio
    async def test_p50_p95_p99_single_sample(self, tmp_db_path: Path) -> None:
        """Percentiles with a single sample should equal that sample."""
        tracker = FeedHealthTracker(window_seconds=3600)
        await tracker.init_db(tmp_db_path)
        await tracker.record_tick_latency("binance", 42.0)

        health = await tracker.get_health("binance")
        assert health.p50_ms == 42.0
        assert health.p95_ms == 42.0
        assert health.p99_ms == 42.0

    @pytest.mark.asyncio
    async def test_p50_p95_p99_empty(self, tmp_db_path: Path) -> None:
        """Percentiles with no samples should all be 0.0."""
        tracker = FeedHealthTracker(window_seconds=3600)
        await tracker.init_db(tmp_db_path)

        health = await tracker.get_health("binance")
        assert health.p50_ms == 0.0
        assert health.p95_ms == 0.0
        assert health.p99_ms == 0.0

    @pytest.mark.asyncio
    async def test_error_count_tracking(self, tmp_db_path: Path) -> None:
        """Errors should be counted and queryable."""
        tracker = FeedHealthTracker(window_seconds=3600)
        await tracker.init_db(tmp_db_path)

        await tracker.record_error("binance", "timeout: connection refused")
        await tracker.record_error("binance", "timeout: read timed out")
        await tracker.record_error("binance", "HTTP 429: rate limited")

        health = await tracker.get_health("binance")
        assert health.error_count_1h == 3

    @pytest.mark.asyncio
    async def test_health_status_transitions(self, tmp_db_path: Path) -> None:
        """Status should reflect latency and heartbeat state."""
        tracker = FeedHealthTracker(window_seconds=3600)
        await tracker.init_db(tmp_db_path)

        # Initially down — no data
        health = await tracker.get_health("binance")
        assert health.status == "down"

        # After heartbeat, healthy
        await tracker.record_feed_heartbeat("binance")
        health = await tracker.get_health("binance")
        assert health.status == "healthy"

        # With moderate latency, still healthy
        for _ in range(10):
            await tracker.record_tick_latency("binance", 100.0)
        health = await tracker.get_health("binance")
        assert health.status == "healthy"

    @pytest.mark.asyncio
    async def test_high_latency_degraded(self, tmp_db_path: Path) -> None:
        """p95 > 5000ms should trigger 'degraded' status."""
        tracker = FeedHealthTracker(window_seconds=3600)
        await tracker.init_db(tmp_db_path)

        # 100 samples: most are moderate but enough high values to push p95 > 5000
        for i in range(100):
            # Samples 90-99 are all > 5000ms → p95 will definitely be > 5000
            lat = 6000.0 if i >= 90 else 100.0 + i
            await tracker.record_tick_latency("binance", lat)

        health = await tracker.get_health("binance")
        assert health.status == "degraded"

    @pytest.mark.asyncio
    async def test_get_all_health(self, tmp_db_path: Path) -> None:
        """get_all_health should return health for all seen sources."""
        tracker = FeedHealthTracker(window_seconds=3600)
        await tracker.init_db(tmp_db_path)

        await tracker.record_tick_latency("binance", 50.0)
        await tracker.record_tick_latency("chainlink", 75.0)
        await tracker.record_tick_latency("kraken", 120.0)

        all_health = await tracker.get_all_health()
        assert set(all_health.keys()) == {"binance", "chainlink", "kraken"}
        assert all_health["binance"].last_latency_ms == 50.0
        assert all_health["chainlink"].last_latency_ms == 75.0
        assert all_health["kraken"].last_latency_ms == 120.0

    @pytest.mark.asyncio
    async def test_health_report_markdown(self, tmp_db_path: Path) -> None:
        """get_health_report should return a Markdown string."""
        tracker = FeedHealthTracker(window_seconds=3600)
        await tracker.init_db(tmp_db_path)

        await tracker.record_tick_latency("binance", 50.0)
        await tracker.record_feed_heartbeat("binance")

        report = await tracker.get_health_report()
        assert "## Feed Health Report" in report
        assert "binance" in report
        assert "healthy" in report

    @pytest.mark.asyncio
    async def test_health_report_empty(self, tmp_db_path: Path) -> None:
        """Empty health report should be informative."""
        tracker = FeedHealthTracker(window_seconds=3600)
        await tracker.init_db(tmp_db_path)

        report = await tracker.get_health_report()
        assert "No feeds monitored yet" in report


# ---------------------------------------------------------------------------
# LatencyTracker tests
# ---------------------------------------------------------------------------


class TestLatencyTracker:
    """Test suite for the loop-duration / HTTP latency tracker."""

    @pytest.mark.asyncio
    async def test_record_loop_duration(self, tmp_db_path: Path) -> None:
        """Loop durations should be recorded and summarised."""
        tracker = LatencyTracker(max_samples=100)
        await tracker.init_db(tmp_db_path)

        for i in range(10):
            await tracker.record_loop_duration(100.0 + i * 10)

        summary = await tracker.get_summary()
        assert summary["loop_count"] == 10
        assert summary["loop_min_ms"] == 100.0
        assert summary["loop_max_ms"] == 190.0

    @pytest.mark.asyncio
    async def test_record_http_latency(self, tmp_db_path: Path) -> None:
        """HTTP latencies should be tracked per-endpoint."""
        tracker = LatencyTracker(max_samples=100)
        await tracker.init_db(tmp_db_path)

        for i in range(5):
            await tracker.record_http_latency("/api/v1/ticker", 20.0 + i * 5)
        for i in range(5):
            await tracker.record_http_latency("/api/v1/order", 30.0 + i * 10)

        summary = await tracker.get_summary()
        assert "/api/v1/ticker" in summary["http"]
        assert "/api/v1/order" in summary["http"]
        assert summary["http"]["/api/v1/ticker"]["count"] == 5
        assert summary["http"]["/api/v1/order"]["count"] == 5

    @pytest.mark.asyncio
    async def test_p50_p95_p99_loop(self, tmp_db_path: Path) -> None:
        """Loop percentiles should be accurate."""
        tracker = LatencyTracker(max_samples=100)
        await tracker.init_db(tmp_db_path)

        for i in range(1, 101):
            await tracker.record_loop_duration(float(i))

        summary = await tracker.get_summary()
        assert summary["loop_p50_ms"] == 50.5
        # Linear interpolation: p95 idx = 0.95 * 99 = 94.05 → 95 + 0.05*(96-95) = 95.05
        assert summary["loop_p95_ms"] == 95.05
        # p99 idx = 0.99 * 99 = 98.01 → 99 + 0.01*(100-99) = 99.01
        assert summary["loop_p99_ms"] == 99.01

    @pytest.mark.asyncio
    async def test_empty_summary(self, tmp_db_path: Path) -> None:
        """Empty tracker should return zeroed summary."""
        tracker = LatencyTracker(max_samples=100)
        await tracker.init_db(tmp_db_path)

        summary = await tracker.get_summary()
        assert summary["loop_count"] == 0
        assert summary["loop_p50_ms"] == 0.0
        assert summary["loop_p95_ms"] == 0.0
        assert summary["loop_p99_ms"] == 0.0
        assert summary["loop_min_ms"] == 0.0
        assert summary["loop_max_ms"] == 0.0

    @pytest.mark.asyncio
    async def test_max_samples_ring_buffer(self, tmp_db_path: Path) -> None:
        """The internal deque should respect max_samples."""
        tracker = LatencyTracker(max_samples=10)
        await tracker.init_db(tmp_db_path)

        for i in range(20):
            await tracker.record_loop_duration(float(i))

        summary = await tracker.get_summary()
        assert summary["loop_count"] == 10
        assert summary["loop_min_ms"] == 10.0  # oldest retained
        assert summary["loop_max_ms"] == 19.0  # newest

    @pytest.mark.asyncio
    async def test_http_percentiles(self, tmp_db_path: Path) -> None:
        """HTTP latency percentiles should be per-endpoint."""
        tracker = LatencyTracker(max_samples=100)
        await tracker.init_db(tmp_db_path)

        for i in range(1, 51):
            await tracker.record_http_latency("/test", float(i))

        summary = await tracker.get_summary()
        http = summary["http"]["/test"]
        assert http["p50_ms"] == 25.5
        assert http["count"] == 50
