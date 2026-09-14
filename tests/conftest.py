"""Shared pytest fixtures for the polymarket_exec test suite."""

from __future__ import annotations

import math
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from polymarket_exec.core.interfaces import (
    AbstractExecutionManager,
    AbstractMarketConnector,
    AbstractPriceConnector,
)
from polymarket_exec.core.types import (
    BacktestParams,
    MarketWindow,
    OrderState,
    PaperOrder,
    PaperPosition,
    Side,
    Signal,
    SignalAction,
    StrategyParams,
    Tick,
)
from polymarket_exec.ops.telemetry import FeedHealthTracker, LatencyTracker


# ---------------------------------------------------------------------------
# Real-money isolation (autouse)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_live_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may inherit the operator's .env private key or LIVE consent.

    config.py loads .env at import, and several dashboard tests hit /api/start
    on the real app — without this a local run could build a real
    LiveExecutor. Tests that need an armed gate set a dummy key explicitly.
    """
    import config as _config

    monkeypatch.setattr(_config, "POLYMARKET_PRIVATE_KEY", "")
    # No wallet either: keeps dashboard renders off the network (wallet stat).
    monkeypatch.setattr(_config, "POLYMARKET_FUNDER", "")
    try:
        from polymarket_bot import controller as _controller
    except Exception:  # noqa: BLE001 — bot package optional in some envs
        return
    monkeypatch.setattr(_controller, "_live_consent", False)


@pytest.fixture(autouse=True)
def _no_quote_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard app tests must not poll the venue for the order-size ticket.

    The app lifespan starts ``quote_feed.run_forever``; swap it for a no-op that
    just waits to be stopped. Tests of the poller itself call the real one.
    """
    try:
        from polymarket_exec.ops.dashboard import quote_feed as _quote_feed
    except Exception:  # noqa: BLE001 — dashboard package optional in some envs
        return

    async def _idle(stop_event) -> None:  # type: ignore[no-untyped-def]
        await stop_event.wait()

    monkeypatch.setattr(_quote_feed, "run_forever", _idle)


@pytest.fixture(autouse=True)
def _no_feed_monitor_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard tests boot the app lifespan; keep its feed monitor offline.

    The monitor's WS connection and REST checks are unit-tested with fakes in
    test_feed_monitor.py.
    """
    from polymarket_exec.ops.feed_monitor import FeedMonitor

    async def _idle_run(self, stop_event):  # noqa: ANN001
        await stop_event.wait()

    monkeypatch.setattr(FeedMonitor, "run", _idle_run)


@pytest.fixture(autouse=True)
def _no_flow_recorder_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard tests boot the app lifespan; keep the venue flow recorder offline.

    The recorder's REST and WS paths are unit-tested with fakes in test_flow_recorder.py.
    """
    from polymarket_exec.ops.flow_recorder import FlowRecorder

    async def _idle_run(self, stop_event):  # noqa: ANN001
        await stop_event.wait()

    monkeypatch.setattr(FlowRecorder, "run", _idle_run)


# ---------------------------------------------------------------------------
# Strategy parameter fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def default_params() -> StrategyParams:
    """Default strategy parameters matching typical paper trading config."""
    return StrategyParams(
        min_trade_usd=1.0,
        max_trade_usd=5.0,
        entry_edge_min=0.05,
        min_confidence=0.60,
        entry_min_remaining_seconds=90,
        max_entry_price=0.95,
        min_entry_price=0.05,
    )


@pytest.fixture
def loose_params() -> StrategyParams:
    """Loose parameters that make it easy to generate entry signals."""
    return StrategyParams(
        min_trade_usd=1.0,
        max_trade_usd=5.0,
        entry_edge_min=0.01,
        min_confidence=0.50,
        entry_min_remaining_seconds=10,
        max_entry_price=0.99,
        min_entry_price=0.01,
    )


@pytest.fixture
def strict_params() -> StrategyParams:
    """Strict parameters that make it hard to generate entry signals."""
    return StrategyParams(
        min_trade_usd=5.0,
        max_trade_usd=10.0,
        entry_edge_min=0.20,
        min_confidence=0.85,
        entry_min_remaining_seconds=180,
        max_entry_price=0.70,
        min_entry_price=0.30,
    )


@pytest.fixture
def backtest_params() -> BacktestParams:
    """Typical backtest parameter set."""
    return BacktestParams(
        entry_edge_min=0.05,
        min_confidence=0.60,
        min_remaining_seconds=90,
        max_entry_price=0.95,
        min_trade_usd=1.0,
        max_trade_usd=5.0,
        min_entry_price=0.05,
    )


# ---------------------------------------------------------------------------
# Deterministic domain object fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_market_window() -> MarketWindow:
    """Deterministic MarketWindow for reproducible tests.

    Uses a fixed epoch timestamp (2024-01-01 00:00:00 UTC) so that
    all derived calculations are bit-for-bit identical across runs.
    """
    return MarketWindow(
        slug="btc-updown-5m-1704067200",
        question="Bitcoin Up or Down - Jan 01, 2024?",
        start_ts=1704067200,
        end_ts=1704067500,
        up_price=0.52,
        down_price=0.48,
    )


@pytest.fixture
def fixture_tick_series(fixture_market_window: MarketWindow) -> list[Tick]:
    """List of 10 deterministic Ticks for testing tick-loop logic.

    Prices drift upward from 42_000 to 42_090 over 10 ticks (1 tick/second).
    Each tick has a SKIP signal except the 5th which has a synthetic ENTER_UP.
    """
    base_ts = datetime(2024, 1, 1, 0, 1, 0, tzinfo=timezone.utc)
    ticks: list[Tick] = []
    for i in range(10):
        price = 42_000.0 + i * 10.0
        action = SignalAction.ENTER_UP if i == 4 else SignalAction.SKIP
        side = Side.UP if i == 4 else None
        confidence = 0.75 if i == 4 else 0.40
        notional = 3.0 if i == 4 else 0.0
        edge = 0.08 if i == 4 else 0.02
        signal = Signal(
            action=action,
            side=side,
            confidence=confidence,
            notional_usd=notional,
            edge=edge,
            fair_up_prob=0.55 + i * 0.005,
            reason=f"tick {i}: {action.name}",
        )
        ticks.append(
            Tick(
                ts=base_ts.replace(second=i),
                window=fixture_market_window,
                spot_price=price,
                reference_price=42_000.0,
                sigma_per_second=0.00015 + i * 0.00001,
                fair_up_prob=0.55 + i * 0.005,
                signal=signal,
                feed_source="binance",
            )
        )
    return ticks


@pytest.fixture
def fixture_price_series() -> list[float]:
    """50-point deterministic BTC price series.

    Synthetic prices oscillating around 42_000 with controlled volatility
    (~0.02% per step).  The series is fully deterministic — same seed
    produces identical values.
    """
    base = 42_000.0
    prices: list[float] = [base]
    for i in range(1, 50):
        # Oscillate between positive and negative moves
        direction = 1.0 if i % 4 in (0, 1) else -1.0
        prices.append(base * (1 + 0.0002 * direction))
        base = prices[-1]
    return prices


@pytest.fixture
def fixture_signal_params() -> StrategyParams:
    """StrategyParams with well-known test values for signal validation."""
    return StrategyParams(
        min_trade_usd=1.0,
        max_trade_usd=5.0,
        entry_edge_min=0.045,
        min_confidence=0.50,
        entry_min_remaining_seconds=60,
        max_entry_price=0.95,
        min_entry_price=0.05,
    )


@pytest.fixture
def fixture_backtest_params() -> BacktestParams:
    """BacktestParams with test values for backtest harness tests."""
    return BacktestParams(
        entry_edge_min=0.04,
        min_confidence=0.50,
        min_remaining_seconds=60,
        max_entry_price=0.95,
        min_trade_usd=1.0,
        max_trade_usd=5.0,
        min_entry_price=0.05,
    )


# ---------------------------------------------------------------------------
# Mock component fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_connector_registry() -> MagicMock:
    """Registry with mocked price and market connectors.

    Returns a MagicMock that behaves like a ConnectorRegistry but
    uses AsyncMock for all async health check methods.
    """
    mock_price = MagicMock(spec=AbstractPriceConnector)
    mock_price.get_spot_and_recent_closes = AsyncMock(
        return_value=(42_000.0, [41_900.0, 41_950.0, 42_000.0])
    )
    mock_price.get_reference_price = AsyncMock(return_value=42_000.0)
    mock_price.health_check = AsyncMock(
        return_value={"ok": True, "latency_ms": 45.0, "detail": "nominal"}
    )

    mock_market = MagicMock(spec=AbstractMarketConnector)
    mock_market.discover_current_window = AsyncMock(
        return_value=MarketWindow(
            slug="btc-updown-5m-1704067200",
            question="Bitcoin Up or Down?",
            start_ts=1704067200,
            end_ts=1704067500,
            up_price=0.52,
            down_price=0.48,
        )
    )
    mock_market.health_check = AsyncMock(
        return_value={"ok": True, "latency_ms": 30.0, "detail": "nominal"}
    )

    registry = MagicMock()
    registry.get_primary_price = MagicMock(return_value=mock_price)
    registry.get_primary_market = MagicMock(return_value=mock_market)
    registry.list_price_connectors = MagicMock(return_value=["primary"])
    registry.list_market_connectors = MagicMock(return_value=["polymarket"])
    registry.health_check_all = AsyncMock(
        return_value={
            "primary": {"ok": True, "latency_ms": 45.0, "detail": "nominal"},
            "polymarket": {"ok": True, "latency_ms": 30.0, "detail": "nominal"},
        }
    )
    registry._mock_price = mock_price
    registry._mock_market = mock_market
    return registry


@pytest.fixture
def mock_execution_manager() -> MagicMock:
    """ExecutionManager with mocked order/position lifecycle methods.

    All async methods return sensible defaults so that tick-loop tests
    can run without a real database or exchange connection.
    """
    mgr = MagicMock(spec=AbstractExecutionManager)

    mgr.submit_order = AsyncMock(
        return_value=PaperOrder(
            order_id=1,
            created_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            window_slug="btc-updown-5m-1704067200",
            side=Side.UP,
            state=OrderState.FILLED,
            requested_notional=3.0,
            filled_notional=3.0,
            entry_price=0.52,
            confidence=0.75,
            edge=0.08,
            feed_source="binance",
        )
    )
    mgr.check_exits = AsyncMock(return_value=None)
    mgr.force_close_all = AsyncMock(return_value=[])
    return mgr


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Temporary SQLite database file for isolated tests.

    The file is created under pytest's ``tmp_path`` fixture so it is
    automatically cleaned up after each test.
    """
    return tmp_path / "test_telemetry.db"


# ---------------------------------------------------------------------------
# Telemetry fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def feed_tracker(tmp_db_path: Path) -> AsyncGenerator[FeedHealthTracker, None]:
    """Initialised :class:`FeedHealthTracker` pointing at a temp database."""
    tracker = FeedHealthTracker(window_seconds=3600)
    await tracker.init_db(tmp_db_path)
    yield tracker


@pytest.fixture
async def latency_tracker(tmp_db_path: Path) -> AsyncGenerator[LatencyTracker, None]:
    """Initialised :class:`LatencyTracker` pointing at a temp database."""
    tracker = LatencyTracker(max_samples=1000)
    await tracker.init_db(tmp_db_path)
    yield tracker


# ---------------------------------------------------------------------------
# Domain object fixtures (legacy — kept for backward compat)
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_window() -> MarketWindow:
    """A single BTC 5-minute market window."""
    return MarketWindow(
        slug="btc-updown-5m-1700000000",
        question="Bitcoin Up or Down - Dec 14, 2023?",
        start_ts=1700000000,
        end_ts=1700000300,
        up_price=0.52,
        down_price=0.48,
    )


@pytest.fixture
def sample_signal_enter_up() -> Signal:
    """A sample ENTER_UP signal."""
    return Signal(
        action=SignalAction.ENTER_UP,
        side=Side.UP,
        confidence=0.75,
        notional_usd=3.0,
        edge=0.08,
        fair_up_prob=0.60,
        reason="enter Up: edge +0.080",
    )


@pytest.fixture
def sample_signal_skip() -> Signal:
    """A sample SKIP signal."""
    return Signal(
        action=SignalAction.SKIP,
        side=None,
        confidence=0.55,
        notional_usd=0.0,
        edge=0.02,
        fair_up_prob=0.54,
        reason="skip: edge/confidence below threshold",
    )


@pytest.fixture
def sample_order() -> PaperOrder:
    """A filled paper order."""
    return PaperOrder(
        order_id=1,
        created_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        window_slug="btc-updown-5m-1700000000",
        side=Side.UP,
        state=OrderState.FILLED,
        requested_notional=3.0,
        filled_notional=3.0,
        entry_price=0.52,
        confidence=0.75,
        edge=0.08,
        feed_source="binance",
    )


@pytest.fixture
def sample_position(sample_order: PaperOrder) -> PaperPosition:
    """An open paper position wrapping *sample_order*."""
    return PaperPosition(
        position_id=1,
        order=sample_order,
        opened_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def closed_position(sample_order: PaperOrder) -> PaperPosition:
    """A closed paper position with realized PnL."""
    return PaperPosition(
        position_id=2,
        order=sample_order,
        opened_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        closed_at=datetime(2024, 1, 1, 12, 2, 30, tzinfo=timezone.utc),
        exit_price=0.95,
        exit_reason="TARGET",
        realized_pnl_usd=1.24,
    )


# ---------------------------------------------------------------------------
# Price series fixtures for pricing-model tests
# ---------------------------------------------------------------------------


@pytest.fixture
def flat_prices() -> list[float]:
    """Constant prices — should yield sigma floor."""
    return [50000.0] * 20


@pytest.fixture
def volatile_prices() -> list[float]:
    """Prices with a clear upward drift and volatility."""
    base = 50000.0
    prices = [base]
    for i in range(1, 30):
        prices.append(base * (1 + 0.001 * i + 0.0005 * math.sin(i)))
    return prices


@pytest.fixture
def valid_closes() -> list[float]:
    """90 seconds of realistic 1-second BTC closes."""
    base = 50000.0
    closes: list[float] = [base]
    for i in range(1, 90):
        # ~0.02% per-step volatility => sigma_per_second ~0.0002
        closes.append(base * (1 + 0.0002 * (1 if i % 3 == 0 else -1)))
        base = closes[-1]
    return closes
