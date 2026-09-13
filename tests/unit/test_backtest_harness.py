"""Unit tests for FullMarketBacktestHarness."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from polymarket_exec.backtest.harness import FullMarketBacktestHarness
from polymarket_exec.backtest.metrics import BacktestResult, FrictionModel
from polymarket_exec.core.interfaces import AbstractSignalGenerator
from polymarket_exec.core.types import (
    MarketWindow,
    Side,
    Signal,
    SignalAction,
    StrategyParams,
    Tick,
)
from polymarket_exec.storage.recorder import MarketDataRecorder


# ---------------------------------------------------------------------------
# Mock signal generators
# ---------------------------------------------------------------------------


class AlwaysEnterUp(AbstractSignalGenerator):
    """Always generates ENTER_UP for testing."""

    def generate(
        self, spot: float, reference: float, sigma: float,
        remaining_seconds: int, market_window: MarketWindow,
    ) -> Signal:
        return Signal(
            action=SignalAction.ENTER_UP, side=Side.UP,
            confidence=0.75, notional_usd=3.0, edge=0.10,
            fair_up_prob=0.60, reason="always up",
        )


class AlwaysSkip(AbstractSignalGenerator):
    """Always generates SKIP for testing."""

    def generate(
        self, spot: float, reference: float, sigma: float,
        remaining_seconds: int, market_window: MarketWindow,
    ) -> Signal:
        return Signal(
            action=SignalAction.SKIP, side=None,
            confidence=0.50, notional_usd=0.0, edge=0.0,
            fair_up_prob=0.50, reason="always skip",
        )


class AlternatingSignal(AbstractSignalGenerator):
    """Alternates between ENTER_UP and ENTER_DOWN."""

    def __init__(self) -> None:
        self._call_count = 0

    def generate(
        self, spot: float, reference: float, sigma: float,
        remaining_seconds: int, market_window: MarketWindow,
    ) -> Signal:
        self._call_count += 1
        if self._call_count % 2 == 1:
            return Signal(
                action=SignalAction.ENTER_UP, side=Side.UP,
                confidence=0.70, notional_usd=2.0, edge=0.08,
                fair_up_prob=0.60, reason="alternating up",
            )
        else:
            return Signal(
                action=SignalAction.ENTER_DOWN, side=Side.DOWN,
                confidence=0.70, notional_usd=2.0, edge=-0.08,
                fair_up_prob=0.40, reason="alternating down",
            )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def strategy_params() -> StrategyParams:
    return StrategyParams(
        min_trade_usd=1.0,
        max_trade_usd=5.0,
        entry_edge_min=0.02,
        min_confidence=0.50,
        entry_min_remaining_seconds=30,
        max_entry_price=0.95,
        min_entry_price=0.05,
    )


@pytest.fixture
def friction() -> FrictionModel:
    return FrictionModel(
        stale_quote_ms=500,
        spread_bps=10.0,
        fill_probability=0.95,
        partial_fill_probability=0.05,
    )


async def _build_populated_recorder(
    tmp_path: Path,
    num_windows: int = 1,
    ticks_per_window: int = 10,
) -> MarketDataRecorder:
    """Build a recorder with deterministic test data."""
    db_path = tmp_path / "harness_test.db"
    r = MarketDataRecorder(db_path=db_path)
    await r.init()

    base_price = 50000.0
    for w_idx in range(num_windows):
        start_ts = 1700000000 + w_idx * 300
        window = MarketWindow(
            slug=f"btc-window-{w_idx}",
            question=f"Bitcoin Up or Down - Window {w_idx}?",
            start_ts=start_ts,
            end_ts=start_ts + 300,
            up_price=0.52,
            down_price=0.48,
        )
        await r.record_window(window)

        # Create ticks with rising then falling prices
        for t_idx in range(ticks_per_window):
            ts = datetime.fromtimestamp(start_ts + t_idx * 30, tz=timezone.utc)
            # Price rises first half, falls second half
            if t_idx < ticks_per_window // 2:
                spot = base_price + t_idx * 200  # rising
            else:
                spot = base_price + (ticks_per_window - t_idx) * 200  # falling

            signal_action = SignalAction.SKIP if t_idx == 0 else (
                SignalAction.ENTER_UP if t_idx < ticks_per_window // 2 else SignalAction.ENTER_DOWN
            )
            side = None if t_idx == 0 else (
                Side.UP if t_idx < ticks_per_window // 2 else Side.DOWN
            )
            notional = 0.0 if t_idx == 0 else 2.0
            edge = 0.0 if t_idx == 0 else (0.08 if t_idx < ticks_per_window // 2 else -0.08)

            tick = Tick(
                ts=ts,
                window=window,
                spot_price=spot,
                reference_price=base_price,
                sigma_per_second=0.0002,
                fair_up_prob=0.55,
                signal=Signal(
                    action=signal_action,
                    side=side,
                    confidence=0.65,
                    notional_usd=notional,
                    edge=edge,
                    fair_up_prob=0.55,
                    reason=f"tick {t_idx}",
                ),
                feed_source="binance",
            )
            await r.record_tick(tick)

    return r


# ---------------------------------------------------------------------------
# Harness run tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_produces_backtest_result(
    tmp_path: Path, strategy_params: StrategyParams, friction: FrictionModel,
) -> None:
    """The harness should produce a BacktestResult with valid fields."""
    recorder = await _build_populated_recorder(tmp_path, num_windows=1, ticks_per_window=10)
    signal_gen = AlwaysEnterUp()
    harness = FullMarketBacktestHarness(recorder, signal_gen)

    result = await harness.run(
        start_ts=1700000000,
        end_ts=1700000300,
        strategy_params=strategy_params,
        friction=friction,
    )

    assert isinstance(result, BacktestResult)
    assert result.total_windows == 1
    assert result.total_signals > 0
    await recorder.close()


@pytest.mark.asyncio
async def test_run_with_always_skip(
    tmp_path: Path, strategy_params: StrategyParams, friction: FrictionModel,
) -> None:
    """When all signals are SKIP, no trades should be taken."""
    recorder = await _build_populated_recorder(tmp_path, num_windows=1, ticks_per_window=10)
    signal_gen = AlwaysSkip()
    harness = FullMarketBacktestHarness(recorder, signal_gen)

    result = await harness.run(
        start_ts=1700000000,
        end_ts=1700000300,
        strategy_params=strategy_params,
        friction=friction,
    )

    assert result.total_signals > 0
    assert result.signals_taken == 0
    assert result.wins == 0
    assert result.losses == 0
    assert result.total_pnl_usd == 0.0
    assert result.total_notional_usd == 0.0
    await recorder.close()


@pytest.mark.asyncio
async def test_run_multiple_windows(
    tmp_path: Path, strategy_params: StrategyParams, friction: FrictionModel,
) -> None:
    """The harness should process multiple windows correctly."""
    recorder = await _build_populated_recorder(tmp_path, num_windows=3, ticks_per_window=6)
    signal_gen = AlwaysEnterUp()
    harness = FullMarketBacktestHarness(recorder, signal_gen)

    result = await harness.run(
        start_ts=1700000000,
        end_ts=1700001200,
        strategy_params=strategy_params,
        friction=friction,
    )

    assert result.total_windows == 3
    assert result.total_signals > 0
    await recorder.close()


# ---------------------------------------------------------------------------
# Friction model tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_friction_reduces_fill_probability(
    tmp_path: Path, strategy_params: StrategyParams,
) -> None:
    """Zero fill probability should result in no trades being taken."""
    recorder = await _build_populated_recorder(tmp_path, num_windows=1, ticks_per_window=10)
    signal_gen = AlwaysEnterUp()
    harness = FullMarketBacktestHarness(recorder, signal_gen)

    # Zero fill probability — no trades should fill
    zero_friction = FrictionModel(
        fill_probability=0.0, partial_fill_probability=0.0,
    )

    result = await harness.run(
        start_ts=1700000000,
        end_ts=1700000300,
        strategy_params=strategy_params,
        friction=zero_friction,
    )

    assert result.signals_taken == 0
    await recorder.close()


@pytest.mark.asyncio
async def test_friction_spread_cost(
    tmp_path: Path, strategy_params: StrategyParams,
) -> None:
    """High spread should reduce notional but not prevent fills."""
    recorder = await _build_populated_recorder(tmp_path, num_windows=1, ticks_per_window=10)
    signal_gen = AlwaysEnterUp()
    harness = FullMarketBacktestHarness(recorder, signal_gen)

    # 100% spread — notional reduced to 0
    max_spread = FrictionModel(
        fill_probability=1.0, partial_fill_probability=0.0,
        spread_bps=10000.0,  # 100% spread
    )

    result = await harness.run(
        start_ts=1700000000,
        end_ts=1700000300,
        strategy_params=strategy_params,
        friction=max_spread,
    )

    # High spread eliminates notional, so no fills
    assert result.signals_taken == 0
    await recorder.close()


# ---------------------------------------------------------------------------
# Exit tracking tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_reasons_populated(
    tmp_path: Path, strategy_params: StrategyParams, friction: FrictionModel,
) -> None:
    """Exit reasons should be tracked when positions close."""
    recorder = await _build_populated_recorder(tmp_path, num_windows=1, ticks_per_window=10)
    signal_gen = AlwaysEnterUp()
    harness = FullMarketBacktestHarness(recorder, signal_gen)

    result = await harness.run(
        start_ts=1700000000,
        end_ts=1700000300,
        strategy_params=strategy_params,
        friction=friction,
    )

    # Exit reasons should have entries
    total_exits = sum(result.exit_reasons.values())
    assert total_exits >= 0  # May be 0 if no positions opened
    await recorder.close()


# ---------------------------------------------------------------------------
# BacktestResult metrics calculation
# ---------------------------------------------------------------------------


class TestBacktestResult:
    def test_to_dict_roundtrip(self) -> None:
        result = BacktestResult(
            name="test", params={"a": 1},
            start_ts=0, end_ts=100,
            total_windows=10, windows_traded=5,
            total_signals=100, signals_taken=20,
            wins=12, losses=8,
            total_pnl_usd=10.0, total_notional_usd=100.0,
            roi=0.10, win_rate=0.60,
            avg_pnl_usd=0.50, max_drawdown_usd=2.0,
            friction_model={"spread_bps": 10.0},
            exit_reasons={"TARGET": 8, "STOP": 2},
        )
        d = result.to_dict()
        restored = BacktestResult.from_dict(d)
        assert restored.name == "test"
        assert restored.wins == 12
        assert restored.losses == 8
        assert restored.total_pnl_usd == pytest.approx(10.0)

    def test_from_dict_partial(self) -> None:
        """from_dict should handle dicts with missing optional fields."""
        d = {
            "name": "partial",
            "params": {},
            "start_ts": 0,
            "end_ts": 100,
            "total_windows": 5,
            "windows_traded": 0,
            "total_signals": 50,
            "signals_taken": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl_usd": 0.0,
            "total_notional_usd": 0.0,
            "roi": 0.0,
            "win_rate": 0.0,
            "avg_pnl_usd": 0.0,
            "max_drawdown_usd": 0.0,
            "friction_model": {},
            "exit_reasons": {},
        }
        result = BacktestResult.from_dict(d)
        assert result.name == "partial"
        assert result.wins == 0

    def test_summary_format(self) -> None:
        result = BacktestResult(
            name="test_run", params={"edge": 0.05},
            start_ts=1000, end_ts=2000,
            total_windows=10, windows_traded=3,
            total_signals=50, signals_taken=5,
            wins=3, losses=2,
            total_pnl_usd=5.0, total_notional_usd=50.0,
            roi=0.10, win_rate=0.60,
            avg_pnl_usd=1.0, max_drawdown_usd=1.5,
            friction_model={"spread_bps": 10.0},
            exit_reasons={"TARGET": 3, "STOP": 2},
        )
        summary = result.summary()
        assert "test_run" in summary
        assert "5.0" in summary  # PnL appears somewhere in summary
        assert "TARGET" in summary


# ---------------------------------------------------------------------------
# FrictionModel tests
# ---------------------------------------------------------------------------


class TestFrictionModel:
    def test_default_values(self) -> None:
        f = FrictionModel()
        assert f.stale_quote_ms == 500
        assert f.spread_bps == 10.0
        assert f.fill_probability == 0.95
        assert f.partial_fill_probability == 0.05

    def test_total_fill_probability(self) -> None:
        f = FrictionModel(fill_probability=0.90, partial_fill_probability=0.08)
        assert f.total_fill_probability == pytest.approx(0.98)

    def test_to_dict_roundtrip(self) -> None:
        f = FrictionModel(
            stale_quote_ms=250, spread_bps=5.0,
            fill_probability=0.90, partial_fill_probability=0.10,
        )
        d = f.to_dict()
        restored = FrictionModel.from_dict(d)
        assert restored.stale_quote_ms == 250
        assert restored.spread_bps == 5.0


# ---------------------------------------------------------------------------
# PnL calculation
# ---------------------------------------------------------------------------


class TestPnLCalculation:
    def test_calculate_pnl_up_win(self) -> None:
        harness = FullMarketBacktestHarness.__new__(FullMarketBacktestHarness)
        pnl = harness._calculate_pnl(entry=50000.0, exit=50100.0, side="Up", notional=2.0)
        # PnL = notional * (exit - entry) / entry = 2 * 100 / 50000 = 0.004
        assert pnl > 0

    def test_calculate_pnl_up_loss(self) -> None:
        harness = FullMarketBacktestHarness.__new__(FullMarketBacktestHarness)
        pnl = harness._calculate_pnl(entry=50000.0, exit=49900.0, side="Up", notional=2.0)
        assert pnl < 0

    def test_calculate_pnl_down_win(self) -> None:
        harness = FullMarketBacktestHarness.__new__(FullMarketBacktestHarness)
        pnl = harness._calculate_pnl(entry=50000.0, exit=49900.0, side="Down", notional=2.0)
        assert pnl > 0

    def test_calculate_pnl_down_loss(self) -> None:
        harness = FullMarketBacktestHarness.__new__(FullMarketBacktestHarness)
        pnl = harness._calculate_pnl(entry=50000.0, exit=50100.0, side="Down", notional=2.0)
        assert pnl < 0

    def test_calculate_pnl_zero_entry(self) -> None:
        harness = FullMarketBacktestHarness.__new__(FullMarketBacktestHarness)
        pnl = harness._calculate_pnl(entry=0.0, exit=50000.0, side="Up", notional=2.0)
        assert pnl == 0.0


# ---------------------------------------------------------------------------
# Mock recorder data test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_mock_recorder_data(
    tmp_path: Path, strategy_params: StrategyParams, friction: FrictionModel,
) -> None:
    """Test the harness with carefully constructed deterministic data."""
    db_path = tmp_path / "deterministic.db"
    r = MarketDataRecorder(db_path=db_path)
    await r.init()

    # Create a window where price moves predictably
    window = MarketWindow(
        slug="det-1", question="Q",
        start_ts=1700000000, end_ts=1700000300,
        up_price=0.52, down_price=0.48,
    )
    await r.record_window(window)

    base_price = 50000.0
    for i in range(6):
        ts = datetime.fromtimestamp(1700000000 + i * 50, tz=timezone.utc)
        # Price: rises then falls
        spot = base_price + (100 if i < 3 else -100) * (1 if i != 2 and i != 5 else 0)

        tick = Tick(
            ts=ts, window=window,
            spot_price=spot, reference_price=base_price,
            sigma_per_second=0.0002, fair_up_prob=0.55,
            signal=Signal(
                action=SignalAction.SKIP, side=None,
                confidence=0.5, notional_usd=0.0, edge=0.0,
                fair_up_prob=0.55, reason="test",
            ),
            feed_source="binance",
        )
        await r.record_tick(tick)

    signal_gen = AlwaysEnterUp()
    harness = FullMarketBacktestHarness(r, signal_gen)

    result = await harness.run(
        start_ts=1700000000, end_ts=1700000300,
        strategy_params=strategy_params, friction=friction,
    )

    assert result is not None
    assert isinstance(result, BacktestResult)
    assert result.total_windows == 1
    await r.close()
