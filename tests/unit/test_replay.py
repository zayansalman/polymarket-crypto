"""Unit tests for DeterministicReplay — replay engine."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from polymarket_exec.core.interfaces import AbstractSignalGenerator
from polymarket_exec.core.types import (
    MarketWindow,
    Side,
    Signal,
    SignalAction,
    Tick,
)
from polymarket_exec.storage.recorder import MarketDataRecorder
from polymarket_exec.storage.replay import DeterministicReplay


# ---------------------------------------------------------------------------
# Mock signal generator
# ---------------------------------------------------------------------------


class MockSignalGenerator(AbstractSignalGenerator):
    """A deterministic signal generator for testing.

    Generates ENTER_UP when remaining_seconds > 120,
    ENTER_DOWN when remaining_seconds is between 60 and 120,
    SKIP otherwise.
    """

    def generate(
        self,
        spot: float,
        reference: float,
        sigma: float,
        remaining_seconds: int,
        market_window: MarketWindow,
    ) -> Signal:
        if remaining_seconds > 120:
            return Signal(
                action=SignalAction.ENTER_UP,
                side=Side.UP,
                confidence=0.70,
                notional_usd=3.0,
                edge=0.10,
                fair_up_prob=0.60,
                reason=f"mock: remaining={remaining_seconds}",
            )
        elif remaining_seconds > 60:
            return Signal(
                action=SignalAction.ENTER_DOWN,
                side=Side.DOWN,
                confidence=0.65,
                notional_usd=2.5,
                edge=-0.08,
                fair_up_prob=0.40,
                reason=f"mock: remaining={remaining_seconds}",
            )
        else:
            return Signal(
                action=SignalAction.SKIP,
                side=None,
                confidence=0.50,
                notional_usd=0.0,
                edge=0.0,
                fair_up_prob=0.50,
                reason=f"mock: remaining={remaining_seconds}",
            )


class EchoSignalGenerator(AbstractSignalGenerator):
    """Echoes back the embedded signal from the tick itself."""

    def generate(
        self,
        spot: float,
        reference: float,
        sigma: float,
        remaining_seconds: int,
        market_window: MarketWindow,
    ) -> Signal:
        # We return a fixed predictable signal based on spot price
        if spot > reference:
            return Signal(
                action=SignalAction.ENTER_UP,
                side=Side.UP,
                confidence=0.75,
                notional_usd=3.0,
                edge=0.08,
                fair_up_prob=0.60,
                reason="echo: above reference",
            )
        elif spot < reference:
            return Signal(
                action=SignalAction.ENTER_DOWN,
                side=Side.DOWN,
                confidence=0.75,
                notional_usd=3.0,
                edge=-0.08,
                fair_up_prob=0.40,
                reason="echo: below reference",
            )
        else:
            return Signal(
                action=SignalAction.SKIP,
                side=None,
                confidence=0.50,
                notional_usd=0.0,
                edge=0.0,
                fair_up_prob=0.50,
                reason="echo: at reference",
            )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def populated_recorder(tmp_path: Path) -> MarketDataRecorder:
    """A recorder with one window and 10 ticks pre-loaded."""
    db_path = tmp_path / "replay_test.db"
    r = MarketDataRecorder(db_path=db_path)
    await r.init()

    window = MarketWindow(
        slug="btc-updown-5m-1700000000",
        question="Bitcoin Up or Down?",
        start_ts=1700000000,
        end_ts=1700000300,
        up_price=0.52,
        down_price=0.48,
    )
    await r.record_window(window)

    # Create 10 ticks spanning the window (ts 0..270 seconds into window)
    for i in range(10):
        signal_action = SignalAction.SKIP if i % 3 == 0 else (
            SignalAction.ENTER_UP if i % 2 == 0 else SignalAction.ENTER_DOWN
        )
        side = None if i % 3 == 0 else (Side.UP if i % 2 == 0 else Side.DOWN)
        tick = Tick(
            ts=datetime(2023, 11, 14, 22, 13, 20 + i, tzinfo=timezone.utc),
            window=window,
            spot_price=50000.0 + i * 100,
            reference_price=49900.0,
            sigma_per_second=0.0002,
            fair_up_prob=0.5 + i * 0.02,
            signal=Signal(
                action=signal_action,
                side=side,
                confidence=0.5 + i * 0.05,
                notional_usd=float(i) if i % 3 != 0 else 0.0,
                edge=0.01 * i,
                fair_up_prob=0.5 + i * 0.02,
                reason=f"test tick {i}",
            ),
            feed_source="binance",
        )
        await r.record_tick(tick)

    yield r
    await r.close()


@pytest.fixture
def mock_signal_gen() -> MockSignalGenerator:
    return MockSignalGenerator()


# ---------------------------------------------------------------------------
# replay_window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_window_generates_signals(
    populated_recorder: MarketDataRecorder,
    mock_signal_gen: MockSignalGenerator,
) -> None:
    replay = DeterministicReplay(populated_recorder, mock_signal_gen)
    signals = await replay.replay_window("btc-updown-5m-1700000000")

    # 10 ticks should produce 10 signals
    assert len(signals) == 10

    # All should be Signal instances
    for sig in signals:
        assert isinstance(sig, Signal)


@pytest.mark.asyncio
async def test_replay_window_empty_window(
    tmp_path: Path,
    mock_signal_gen: MockSignalGenerator,
) -> None:
    """Replaying a window with no ticks returns empty list."""
    db_path = tmp_path / "empty.db"
    r = MarketDataRecorder(db_path=db_path)
    await r.init()

    window = MarketWindow(
        slug="empty-window",
        question="Empty",
        start_ts=1700000000,
        end_ts=1700000300,
        up_price=0.5,
        down_price=0.5,
    )
    await r.record_window(window)

    replay = DeterministicReplay(r, mock_signal_gen)
    signals = await replay.replay_window("empty-window")
    assert signals == []
    await r.close()


@pytest.mark.asyncio
async def test_replay_window_nonexistent_window(
    populated_recorder: MarketDataRecorder,
    mock_signal_gen: MockSignalGenerator,
) -> None:
    """Replaying a non-existent window returns empty list."""
    replay = DeterministicReplay(populated_recorder, mock_signal_gen)
    signals = await replay.replay_window("nonexistent-slug")
    assert signals == []


# ---------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_window_callback_called(
    populated_recorder: MarketDataRecorder,
    mock_signal_gen: MockSignalGenerator,
) -> None:
    """The callback should be called once for each (tick, signal) pair."""
    replay = DeterministicReplay(populated_recorder, mock_signal_gen)
    call_count = 0
    received_pairs: list[tuple[Tick, Signal]] = []

    async def callback(tick: Tick, signal: Signal) -> None:
        nonlocal call_count
        call_count += 1
        received_pairs.append((tick, signal))

    signals = await replay.replay_window(
        "btc-updown-5m-1700000000", callback=callback
    )

    assert call_count == 10
    assert len(received_pairs) == 10
    assert len(signals) == 10

    # Verify each pair
    for tick, signal in received_pairs:
        assert isinstance(tick, Tick)
        assert isinstance(signal, Signal)
        assert tick.window.slug == "btc-updown-5m-1700000000"


# ---------------------------------------------------------------------------
# replay_range
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_range_multiple_windows(
    tmp_path: Path,
    mock_signal_gen: MockSignalGenerator,
) -> None:
    """replay_range should handle multiple windows."""
    db_path = tmp_path / "multi.db"
    r = MarketDataRecorder(db_path=db_path)
    await r.init()

    for w_idx in range(3):
        window = MarketWindow(
            slug=f"btc-window-{w_idx}",
            question=f"Q{w_idx}",
            start_ts=1700000000 + w_idx * 300,
            end_ts=1700000300 + w_idx * 300,
            up_price=0.52,
            down_price=0.48,
        )
        await r.record_window(window)

        for t_idx in range(5):
            tick = Tick(
                ts=datetime(2023, 11, 14, 22, 13, 20 + t_idx, tzinfo=timezone.utc),
                window=window,
                spot_price=50000.0 + t_idx * 10,
                reference_price=49900.0,
                sigma_per_second=0.0002,
                fair_up_prob=0.5,
                signal=Signal(
                    action=SignalAction.SKIP,
                    side=None,
                    confidence=0.5,
                    notional_usd=0.0,
                    edge=0.0,
                    fair_up_prob=0.5,
                    reason="test",
                ),
                feed_source="binance",
            )
            await r.record_tick(tick)

    replay = DeterministicReplay(r, mock_signal_gen)
    results = await replay.replay_range(1700000000, 1700001200)

    # Should have 3 windows with signals
    assert len(results) == 3
    for slug, signals in results.items():
        assert len(signals) == 5

    await r.close()


@pytest.mark.asyncio
async def test_replay_range_with_callback(
    tmp_path: Path,
    mock_signal_gen: MockSignalGenerator,
) -> None:
    """Callback should be invoked for every tick across all windows."""
    db_path = tmp_path / "multi_cb.db"
    r = MarketDataRecorder(db_path=db_path)
    await r.init()

    for w_idx in range(2):
        window = MarketWindow(
            slug=f"btc-window-{w_idx}",
            question=f"Q{w_idx}",
            start_ts=1700000000 + w_idx * 300,
            end_ts=1700000300 + w_idx * 300,
            up_price=0.52,
            down_price=0.48,
        )
        await r.record_window(window)
        for t_idx in range(3):
            tick = Tick(
                ts=datetime(2023, 11, 14, 22, 13, 20 + t_idx, tzinfo=timezone.utc),
                window=window,
                spot_price=50000.0,
                reference_price=49900.0,
                sigma_per_second=0.0002,
                fair_up_prob=0.5,
                signal=Signal(
                    action=SignalAction.SKIP,
                    side=None,
                    confidence=0.5,
                    notional_usd=0.0,
                    edge=0.0,
                    fair_up_prob=0.5,
                    reason="test",
                ),
                feed_source="binance",
            )
            await r.record_tick(tick)

    replay = DeterministicReplay(r, mock_signal_gen)
    call_count = 0

    async def callback(tick: Tick, signal: Signal) -> None:
        nonlocal call_count
        call_count += 1

    await replay.replay_range(1700000000, 1700001200, callback=callback)
    assert call_count == 6  # 2 windows * 3 ticks each
    await r.close()


# ---------------------------------------------------------------------------
# Empty replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_range_empty(
    tmp_path: Path,
    mock_signal_gen: MockSignalGenerator,
) -> None:
    """Replaying a range with no windows returns empty dict."""
    db_path = tmp_path / "empty_range.db"
    r = MarketDataRecorder(db_path=db_path)
    await r.init()

    replay = DeterministicReplay(r, mock_signal_gen)
    results = await replay.replay_range(1, 100)
    assert results == {}
    await r.close()


# ---------------------------------------------------------------------------
# Coverage report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coverage_report(
    populated_recorder: MarketDataRecorder,
    mock_signal_gen: MockSignalGenerator,
) -> None:
    replay = DeterministicReplay(populated_recorder, mock_signal_gen)
    await replay.replay_range(1700000000, 1700000300)

    report = await replay.get_coverage_report()
    assert report["total_windows"] == 1
    assert report["replayed_windows"] == 1
    assert report["total_ticks"] == 10
    assert report["signals_generated"] == 10
    assert report["windows_without_ticks"] == 0


@pytest.mark.asyncio
async def test_coverage_report_with_empty_window(
    tmp_path: Path,
    mock_signal_gen: MockSignalGenerator,
) -> None:
    """A window without ticks should be counted in windows_without_ticks."""
    db_path = tmp_path / "mixed.db"
    r = MarketDataRecorder(db_path=db_path)
    await r.init()

    # Window with ticks
    w1 = MarketWindow(
        slug="w1", question="Q1", start_ts=1700000000,
        end_ts=1700000300, up_price=0.52, down_price=0.48,
    )
    await r.record_window(w1)
    tick = Tick(
        ts=datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc),
        window=w1, spot_price=50000.0, reference_price=49900.0,
        sigma_per_second=0.0002, fair_up_prob=0.5,
        signal=Signal(
            action=SignalAction.SKIP, side=None, confidence=0.5,
            notional_usd=0.0, edge=0.0, fair_up_prob=0.5, reason="test",
        ),
        feed_source="binance",
    )
    await r.record_tick(tick)

    # Window without ticks
    w2 = MarketWindow(
        slug="w2", question="Q2", start_ts=1700000300,
        end_ts=1700000600, up_price=0.50, down_price=0.50,
    )
    await r.record_window(w2)

    replay = DeterministicReplay(r, mock_signal_gen)
    await replay.replay_range(1700000000, 1700000600)

    report = await replay.get_coverage_report()
    assert report["windows_without_ticks"] == 1
    await r.close()


# ---------------------------------------------------------------------------
# Stats reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_stats(
    populated_recorder: MarketDataRecorder,
    mock_signal_gen: MockSignalGenerator,
) -> None:
    replay = DeterministicReplay(populated_recorder, mock_signal_gen)
    await replay.replay_window("btc-updown-5m-1700000000")
    assert replay._stats["total_ticks"] == 10

    replay.reset_stats()
    assert replay._stats["total_ticks"] == 0
    assert replay._stats["signals_generated"] == 0
    assert replay._stats["replayed_windows"] == 0
