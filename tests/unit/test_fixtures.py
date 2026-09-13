"""Tests verifying that all deterministic fixtures produce valid, reproducible data."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from polymarket_exec.core.types import (
    BacktestParams,
    MarketWindow,
    Signal,
    SignalAction,
    StrategyParams,
    Tick,
)


# ---------------------------------------------------------------------------
# Determinism tests
# ---------------------------------------------------------------------------


class TestFixtureDeterminism:
    """Every fixture must produce identical output across test runs."""

    def test_market_window_deterministic(self, fixture_market_window: MarketWindow) -> None:
        """fixture_market_window must have fixed, known values."""
        assert fixture_market_window.slug == "btc-updown-5m-1704067200"
        assert fixture_market_window.start_ts == 1704067200
        assert fixture_market_window.end_ts == 1704067500
        assert fixture_market_window.up_price == 0.52
        assert fixture_market_window.down_price == 0.48

    def test_tick_series_length_and_types(
        self, fixture_tick_series: list[Tick]
    ) -> None:
        """fixture_tick_series must contain 10 Tick objects."""
        assert len(fixture_tick_series) == 10
        for tick in fixture_tick_series:
            assert isinstance(tick, Tick)
            assert isinstance(tick.ts, datetime)
            assert isinstance(tick.window, MarketWindow)
            assert isinstance(tick.signal, Signal)

    def test_tick_series_prices_progressive(
        self, fixture_tick_series: list[Tick]
    ) -> None:
        """Prices should increase by exactly 10.0 per tick."""
        for i, tick in enumerate(fixture_tick_series):
            expected_price = 42_000.0 + i * 10.0
            assert tick.spot_price == expected_price
            assert tick.reference_price == 42_000.0

    def test_tick_series_signal_at_index_4(
        self, fixture_tick_series: list[Tick]
    ) -> None:
        """The 5th tick (index 4) should have ENTER_UP; all others SKIP."""
        for i, tick in enumerate(fixture_tick_series):
            if i == 4:
                assert tick.signal.action is SignalAction.ENTER_UP
                assert tick.signal.side is not None
                assert tick.signal.confidence == 0.75
                assert tick.signal.notional_usd == 3.0
            else:
                assert tick.signal.action is SignalAction.SKIP

    def test_tick_series_timestamps(
        self, fixture_tick_series: list[Tick]
    ) -> None:
        """Timestamps should increment by 1 second."""
        for i, tick in enumerate(fixture_tick_series):
            expected = datetime(2024, 1, 1, 0, 1, i, tzinfo=timezone.utc)
            assert tick.ts == expected

    def test_price_series_length(self, fixture_price_series: list[float]) -> None:
        """fixture_price_series must have exactly 50 points."""
        assert len(fixture_price_series) == 50

    def test_price_series_first_value(self, fixture_price_series: list[float]) -> None:
        """First price must be 42_000.0."""
        assert fixture_price_series[0] == 42_000.0

    def test_price_series_positive(self, fixture_price_series: list[float]) -> None:
        """All prices must be positive."""
        assert all(p > 0 for p in fixture_price_series)

    def test_price_series_no_nans(self, fixture_price_series: list[float]) -> None:
        """No NaN values in the price series."""
        import math

        assert not any(math.isnan(p) for p in fixture_price_series)

    def test_price_series_reproducible(self, fixture_price_series: list[float]) -> None:
        """The same deterministic series must be produced every time.

        We verify this by checking known values at specific indices.
        """
        # Index 1: 42_000 * (1 + 0.0002 * 1) since 1 % 4 in (0,1)
        expected_1 = 42_000.0 * (1 + 0.0002 * 1.0)
        assert fixture_price_series[1] == pytest.approx(expected_1, rel=1e-12)

        # Index 3: 42_000 * (1 + 0.0002 * 1) * (1 + 0.0002 * 1) * (1 - 0.0002 * 1)
        # since 3 % 4 = 3 not in (0,1) -> -1.0
        assert fixture_price_series[3] > 0

    def test_signal_params(self, fixture_signal_params: StrategyParams) -> None:
        """fixture_signal_params must have known test values."""
        assert fixture_signal_params.min_trade_usd == 1.0
        assert fixture_signal_params.max_trade_usd == 5.0
        assert fixture_signal_params.entry_edge_min == 0.045
        assert fixture_signal_params.min_confidence == 0.50
        assert fixture_signal_params.entry_min_remaining_seconds == 60

    def test_backtest_params(self, fixture_backtest_params: BacktestParams) -> None:
        """fixture_backtest_params must have known test values."""
        assert fixture_backtest_params.entry_edge_min == 0.04
        assert fixture_backtest_params.min_confidence == 0.50
        assert fixture_backtest_params.min_remaining_seconds == 60
        assert fixture_backtest_params.max_entry_price == 0.95
        assert fixture_backtest_params.min_trade_usd == 1.0
        assert fixture_backtest_params.max_trade_usd == 5.0


# ---------------------------------------------------------------------------
# Mock fixture tests
# ---------------------------------------------------------------------------


class TestMockFixtures:
    """Mock fixtures should have properly configured async methods."""

    @pytest.mark.asyncio
    async def test_mock_registry_price_connector(
        self, mock_connector_registry: object
    ) -> None:
        """Registry mock should return a working price connector."""
        price = mock_connector_registry.get_primary_price()
        spot, closes = await price.get_spot_and_recent_closes()
        assert spot == 42_000.0
        assert len(closes) == 3

    @pytest.mark.asyncio
    async def test_mock_registry_market_connector(
        self, mock_connector_registry: object
    ) -> None:
        """Registry mock should return a working market connector."""
        market = mock_connector_registry.get_primary_market()
        window = await market.discover_current_window()
        assert isinstance(window, MarketWindow)
        assert window.slug == "btc-updown-5m-1704067200"

    @pytest.mark.asyncio
    async def test_mock_registry_health_check(
        self, mock_connector_registry: object
    ) -> None:
        """Registry mock health check should return results for all connectors."""
        results = await mock_connector_registry.health_check_all()
        assert "primary" in results
        assert "polymarket" in results
        assert results["primary"]["ok"] is True

    @pytest.mark.asyncio
    async def test_mock_execution_manager_submit(
        self, mock_execution_manager: object
    ) -> None:
        """Execution manager mock should return a PaperOrder on submit."""
        from polymarket_exec.core.types import Signal, Side, SignalAction, MarketWindow

        signal = Signal(
            action=SignalAction.ENTER_UP,
            side=Side.UP,
            confidence=0.75,
            notional_usd=3.0,
            edge=0.08,
            fair_up_prob=0.60,
            reason="test",
        )
        window = MarketWindow(
            slug="test", question="test?", start_ts=0, end_ts=300,
            up_price=0.52, down_price=0.48,
        )
        order = await mock_execution_manager.submit_order(signal, window)
        assert order.order_id == 1
        assert order.side is Side.UP

    @pytest.mark.asyncio
    async def test_mock_execution_manager_check_exits(
        self, mock_execution_manager: object
    ) -> None:
        """Execution manager check_exits should return None by default."""
        from polymarket_exec.core.types import PaperPosition, PaperOrder

        mock_pos = PaperPosition(
            position_id=1,
            order=PaperOrder(
                order_id=1,
                created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                window_slug="test",
                side=None,
                state=None,  # type: ignore[arg-type]
                requested_notional=0.0,
                filled_notional=0.0,
                entry_price=0.0,
                confidence=0.0,
                edge=0.0,
                feed_source="test",
            ),
            opened_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        mock_tick = None  # not used by the mock
        result = await mock_execution_manager.check_exits(mock_pos, mock_tick)  # type: ignore[arg-type]
        assert result is None


# ---------------------------------------------------------------------------
# Infrastructure fixture tests
# ---------------------------------------------------------------------------


class TestInfrastructureFixtures:
    """Tests for tmp_db_path and other infrastructure fixtures."""

    def test_tmp_db_path_exists(self, tmp_db_path: Path) -> None:
        """tmp_db_path should be a valid Path under pytest tmp_path."""
        assert isinstance(tmp_db_path, Path)
        assert tmp_db_path.name == "test_telemetry.db"
        assert "tmp" in str(tmp_db_path) or "pytest" in str(tmp_db_path)

    @pytest.mark.asyncio
    async def test_feed_tracker_fixture(self, tmp_db_path: Path) -> None:
        """feed_tracker fixture should be initialised and usable."""
        from polymarket_exec.ops.telemetry import FeedHealthTracker

        tracker = FeedHealthTracker(window_seconds=3600)
        await tracker.init_db(tmp_db_path)
        assert isinstance(tracker, FeedHealthTracker)

    @pytest.mark.asyncio
    async def test_latency_tracker_fixture(self, tmp_db_path: Path) -> None:
        """latency_tracker fixture should be initialised and usable."""
        from polymarket_exec.ops.telemetry import LatencyTracker

        tracker = LatencyTracker(max_samples=100)
        await tracker.init_db(tmp_db_path)
        assert isinstance(tracker, LatencyTracker)
