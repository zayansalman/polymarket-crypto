"""Unit tests for PaperExecutionManager — order lifecycle, exits, and queries."""

from __future__ import annotations

import random
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio

from polymarket_exec.core.types import (
    ExitReason,
    MarketWindow,
    OrderState,
    PaperOrder,
    Side,
    Signal,
    SignalAction,
    Tick,
)
from polymarket_exec.execution.paper import PaperExecutionManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _AlwaysFillRNG(random.Random):
    """Deterministic RNG: always rolls high so every order fully fills.

    Pins the fill simulation in PaperExecutionManager — without this, the
    1% PARTIAL_FILL / 0.1% REJECTED rolls make FILLED-assuming tests flaky.
    PARTIAL_FILL/REJECTED paths are covered explicitly by monkey-patching
    _determine_fill in their dedicated tests.
    """

    def random(self) -> float:
        return 0.999


@pytest_asyncio.fixture
async def em(tmp_path: Path) -> PaperExecutionManager:
    """An initialised PaperExecutionManager backed by a temp SQLite file."""
    db_path = tmp_path / "test_execution.db"
    manager = PaperExecutionManager(
        str(db_path), latency_sim_ms=0.0, rng=_AlwaysFillRNG()
    )
    await manager.init()
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def em_with_latency(tmp_path: Path) -> PaperExecutionManager:
    """PaperExecutionManager with 10 ms simulated latency."""
    db_path = tmp_path / "test_latency.db"
    manager = PaperExecutionManager(
        str(db_path), latency_sim_ms=10.0, rng=_AlwaysFillRNG()
    )
    await manager.init()
    yield manager
    await manager.close()


@pytest.fixture
def window() -> MarketWindow:
    return MarketWindow(
        slug="btc-updown-5m-1700000000",
        question="Bitcoin Up or Down?",
        start_ts=1700000000,
        end_ts=1700000300,
        up_price=0.52,
        down_price=0.48,
    )


@pytest.fixture
def signal_enter_up() -> Signal:
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
def signal_enter_down() -> Signal:
    return Signal(
        action=SignalAction.ENTER_DOWN,
        side=Side.DOWN,
        confidence=0.70,
        notional_usd=2.0,
        edge=-0.07,
        fair_up_prob=0.40,
        reason="enter Down: edge -0.070",
    )


@pytest.fixture
def signal_skip() -> Signal:
    return Signal(
        action=SignalAction.SKIP,
        side=None,
        confidence=0.55,
        notional_usd=0.0,
        edge=0.02,
        fair_up_prob=0.54,
        reason="skip: edge below threshold",
    )


# ---------------------------------------------------------------------------
# Order creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_order_full_lifecycle(em: PaperExecutionManager, window: MarketWindow, signal_enter_up: Signal) -> None:
    """An order should go PENDING → ACKNOWLEDGED → FILLED."""
    order = await em.submit_order(signal_enter_up, window)

    assert isinstance(order, PaperOrder)
    assert order.order_id >= 1
    assert order.window_slug == window.slug
    assert order.side is Side.UP
    assert order.state is OrderState.FILLED
    assert order.requested_notional == 3.0
    assert order.filled_notional == 3.0
    assert order.entry_price == pytest.approx(0.52)

    # Check transition history
    history = await em.get_order_transition_history(order.order_id)
    states = [h["to_state"] for h in history]
    assert "PENDING" in states
    assert "ACKNOWLEDGED" in states
    assert "FILLED" in states


@pytest.mark.asyncio
async def test_submit_order_multiple_orders(em: PaperExecutionManager, window: MarketWindow, signal_enter_up: Signal, signal_enter_down: Signal) -> None:
    """Multiple orders should get independent IDs and be queryable."""
    order1 = await em.submit_order(signal_enter_up, window)
    order2 = await em.submit_order(signal_enter_down, window)

    assert order1.order_id != order2.order_id
    assert order1.side is Side.UP
    assert order2.side is Side.DOWN

    assert await em.get_order_count() == 2


# ---------------------------------------------------------------------------
# SKIP signal → CANCELLED order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_signal_creates_cancelled_order(em: PaperExecutionManager, window: MarketWindow, signal_skip: Signal) -> None:
    """A SKIP signal should create a CANCELLED order (no position)."""
    order = await em.submit_order(signal_skip, window)

    assert order.state is OrderState.CANCELLED
    assert order.filled_notional == 0.0

    # No position should be created
    open_positions = await em.get_open_positions()
    assert len(open_positions) == 0

    # Transition history should show CANCELLED
    history = await em.get_order_transition_history(order.order_id)
    assert any(h["to_state"] == "CANCELLED" for h in history)


# ---------------------------------------------------------------------------
# Partial fill path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_fill_state(em: PaperExecutionManager, window: MarketWindow, signal_enter_up: Signal) -> None:
    """Forcing a partial fill should produce PARTIAL_FILL state."""
    # Monkey-patch _determine_fill to force partial fill
    original_determine = em._determine_fill
    em._determine_fill = lambda signal: (OrderState.PARTIAL_FILL, 1.0)

    try:
        order = await em.submit_order(signal_enter_up, window)
        assert order.state is OrderState.PARTIAL_FILL
        assert order.filled_notional == 1.0
        assert order.filled_notional < order.requested_notional

        # No position created for partial fill
        open_positions = await em.get_open_positions()
        assert len(open_positions) == 0
    finally:
        em._determine_fill = original_determine


# ---------------------------------------------------------------------------
# Rejected fill path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejected_fill_state(em: PaperExecutionManager, window: MarketWindow, signal_enter_up: Signal) -> None:
    """Forcing a rejection should produce REJECTED state."""
    original_determine = em._determine_fill
    em._determine_fill = lambda signal: (OrderState.REJECTED, 0.0)

    try:
        order = await em.submit_order(signal_enter_up, window)
        assert order.state is OrderState.REJECTED
        assert order.filled_notional == 0.0
    finally:
        em._determine_fill = original_determine


# ---------------------------------------------------------------------------
# Position creation on FILLED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filled_order_creates_position(em: PaperExecutionManager, window: MarketWindow, signal_enter_up: Signal) -> None:
    """A fully filled order should create an open PaperPosition."""
    order = await em.submit_order(signal_enter_up, window)
    assert order.state is OrderState.FILLED

    open_positions = await em.get_open_positions()
    assert len(open_positions) == 1

    pos = open_positions[0]
    assert pos.position_id >= 1
    assert pos.order.order_id == order.order_id
    assert pos.closed_at is None
    assert pos.exit_reason is None
    assert pos.realized_pnl_usd is None


@pytest.mark.asyncio
async def test_get_position_by_id(em: PaperExecutionManager, window: MarketWindow, signal_enter_up: Signal) -> None:
    """get_position should fetch a single position by ID."""
    order = await em.submit_order(signal_enter_up, window)
    open_positions = await em.get_open_positions()
    pos = open_positions[0]

    fetched = await em.get_position(pos.position_id)
    assert fetched is not None
    assert fetched.position_id == pos.position_id
    assert fetched.order.order_id == order.order_id

    # Non-existent position
    assert await em.get_position(99999) is None


# ---------------------------------------------------------------------------
# Exit checking — WINDOW_ROLL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_exits_window_roll(em: PaperExecutionManager, window: MarketWindow, signal_enter_up: Signal) -> None:
    """WINDOW_ROLL: position from different window slug."""
    await em.submit_order(signal_enter_up, window)
    open_positions = await em.get_open_positions()
    pos = open_positions[0]

    # New window with different slug
    new_window = MarketWindow(
        slug="btc-updown-5m-1700000300",  # different slug
        question="Bitcoin Up or Down?",
        start_ts=1700000300,
        end_ts=1700000600,
        up_price=0.55,
        down_price=0.45,
    )
    tick = _make_tick(new_window, remaining_seconds=120, edge=0.06)

    reason = await em.check_exits(pos, tick)
    assert reason is ExitReason.WINDOW_ROLL


# ---------------------------------------------------------------------------
# Exit checking — TARGET
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_exits_target_hit(em: PaperExecutionManager, window: MarketWindow, signal_enter_up: Signal) -> None:
    """TARGET: pnl >= notional * target_return."""
    # Set target_return to 0.10 (10%). With $3 notional, target is $0.30 profit.
    # Entry price = 0.52 (up_price), need exit to produce $0.30 PnL.
    # shares = 3.0 / 0.52 = 5.769. Need exit_price where 5.769 * (exit - 0.52) >= 0.30
    # => exit >= 0.52 + 0.30/5.769 = 0.572
    await em.submit_order(signal_enter_up, window)
    open_positions = await em.get_open_positions()
    pos = open_positions[0]

    # Set up_price to 0.58 which should trigger target
    tick_window = MarketWindow(
        slug=window.slug,
        question=window.question,
        start_ts=window.start_ts,
        end_ts=window.end_ts,
        up_price=0.58,  # high enough for target
        down_price=0.42,
    )
    tick = _make_tick(tick_window, remaining_seconds=120, edge=0.06)

    reason = await em.check_exits(pos, tick)
    assert reason is ExitReason.TARGET


# ---------------------------------------------------------------------------
# Exit checking — STOP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_exits_stop_hit(em: PaperExecutionManager, window: MarketWindow, signal_enter_up: Signal) -> None:
    """STOP: pnl <= notional * stop_return."""
    # stop_return = -0.08. With $3 notional, stop triggers at PnL <= -0.24.
    # shares = 3.0 / 0.52 = 5.769. Need exit where 5.769 * (exit - 0.52) <= -0.24
    # => exit <= 0.52 - 0.24/5.769 = 0.478
    await em.submit_order(signal_enter_up, window)
    open_positions = await em.get_open_positions()
    pos = open_positions[0]

    tick_window = MarketWindow(
        slug=window.slug,
        question=window.question,
        start_ts=window.start_ts,
        end_ts=window.end_ts,
        up_price=0.45,  # low enough for stop
        down_price=0.55,
    )
    tick = _make_tick(tick_window, remaining_seconds=120, edge=0.06)

    reason = await em.check_exits(pos, tick)
    assert reason is ExitReason.STOP


# ---------------------------------------------------------------------------
# Exit checking — TIME
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_exits_time_hit(em: PaperExecutionManager, window: MarketWindow, signal_enter_up: Signal) -> None:
    """TIME: remaining_seconds <= time_exit_seconds (default 45)."""
    await em.submit_order(signal_enter_up, window)
    open_positions = await em.get_open_positions()
    pos = open_positions[0]

    # Tick with remaining seconds = 30 (below 45 threshold)
    # Window end is 1700000300, so ts must be 1700000270 or later
    tick_window = MarketWindow(
        slug=window.slug,
        question=window.question,
        start_ts=window.start_ts,
        end_ts=window.end_ts,
        up_price=0.52,
        down_price=0.48,
    )
    tick = _make_tick(tick_window, remaining_seconds=30, edge=0.06)

    reason = await em.check_exits(pos, tick)
    assert reason is ExitReason.TIME


@pytest.mark.asyncio
async def test_check_exits_time_not_hit(em: PaperExecutionManager, window: MarketWindow, signal_enter_up: Signal) -> None:
    """TIME should NOT trigger when remaining_seconds > time_exit_seconds."""
    await em.submit_order(signal_enter_up, window)
    open_positions = await em.get_open_positions()
    pos = open_positions[0]

    tick = _make_tick(window, remaining_seconds=120, edge=0.06)
    # Set up price so no target/stop triggers

    reason = await em.check_exits(pos, tick)
    # Should not be TIME since 120 > 45
    assert reason is not ExitReason.TIME


# ---------------------------------------------------------------------------
# Exit checking — BAND_REENTRY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_exits_band_reentry(em: PaperExecutionManager, window: MarketWindow, signal_enter_up: Signal) -> None:
    """BAND_REENTRY: abs(edge) < entry_edge_min / 2."""
    # entry_edge_min default = 0.045, half = 0.0225
    await em.submit_order(signal_enter_up, window)
    open_positions = await em.get_open_positions()
    pos = open_positions[0]

    # Edge = 0.01 which is below 0.0225 threshold
    tick = _make_tick(window, remaining_seconds=120, edge=0.01)

    reason = await em.check_exits(pos, tick)
    assert reason is ExitReason.BAND_REENTRY


@pytest.mark.asyncio
async def test_check_exits_no_exit(em: PaperExecutionManager, window: MarketWindow, signal_enter_up: Signal) -> None:
    """No exit conditions met → None."""
    await em.submit_order(signal_enter_up, window)
    open_positions = await em.get_open_positions()
    pos = open_positions[0]

    # Normal conditions: same window, plenty of time, moderate edge, no target/stop
    tick = _make_tick(window, remaining_seconds=120, edge=0.06)

    reason = await em.check_exits(pos, tick)
    assert reason is None


# ---------------------------------------------------------------------------
# Priority ordering: WINDOW_ROLL before TIME
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_priority_window_roll_before_time(em: PaperExecutionManager, window: MarketWindow, signal_enter_up: Signal) -> None:
    """WINDOW_ROLL should trigger before TIME even when both conditions are met."""
    await em.submit_order(signal_enter_up, window)
    open_positions = await em.get_open_positions()
    pos = open_positions[0]

    new_window = MarketWindow(
        slug="btc-updown-5m-1700000300",
        question=window.question,
        start_ts=1700000300,
        end_ts=1700000600,
        up_price=0.52,
        down_price=0.48,
    )
    # remaining_seconds=30 would trigger TIME, but WINDOW_ROLL should win
    tick = _make_tick(new_window, remaining_seconds=30, edge=0.06)

    reason = await em.check_exits(pos, tick)
    assert reason is ExitReason.WINDOW_ROLL


# ---------------------------------------------------------------------------
# Force close all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_close_all(em: PaperExecutionManager, window: MarketWindow, signal_enter_up: Signal, signal_enter_down: Signal) -> None:
    """force_close_all should close every open position."""
    await em.submit_order(signal_enter_up, window)
    # Need a fresh window to enter the second position (since same window)
    # But we can use the same window since we allow multiple positions conceptually
    # Actually, we need to patch max_open_positions. Let's just enter one for now
    # and verify it's closed.

    open_before = await em.get_open_positions()
    assert len(open_before) == 1

    closed = await em.force_close_all(ExitReason.STOP_REQUEST)
    assert len(closed) == 1
    assert closed[0].position_id == open_before[0].position_id
    assert closed[0].exit_reason is ExitReason.STOP_REQUEST
    assert closed[0].realized_pnl_usd == 0.0

    open_after = await em.get_open_positions()
    assert len(open_after) == 0

    # Verify in DB via closed positions
    closed_positions = await em.get_closed_positions()
    assert len(closed_positions) == 1


@pytest.mark.asyncio
async def test_force_close_all_empty(em: PaperExecutionManager) -> None:
    """force_close_all with no open positions returns empty list."""
    closed = await em.force_close_all(ExitReason.STOP_REQUEST)
    assert closed == []


# ---------------------------------------------------------------------------
# Latency simulation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_latency_simulation(em_with_latency: PaperExecutionManager, window: MarketWindow, signal_enter_up: Signal) -> None:
    """Order with latency should still complete successfully."""
    order = await em_with_latency.submit_order(signal_enter_up, window)
    assert order.state is OrderState.FILLED

    # Transition history should include all states
    history = await em_with_latency.get_order_transition_history(order.order_id)
    states = [h["to_state"] for h in history]
    assert "PENDING" in states
    assert "ACKNOWLEDGED" in states
    assert "FILLED" in states


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_open_positions_returns_only_open(em: PaperExecutionManager, window: MarketWindow, signal_enter_up: Signal) -> None:
    """get_open_positions should exclude closed positions."""
    await em.submit_order(signal_enter_up, window)
    open_before = await em.get_open_positions()
    assert len(open_before) == 1

    await em.force_close_all(ExitReason.TARGET)
    open_after = await em.get_open_positions()
    assert len(open_after) == 0


@pytest.mark.asyncio
async def test_get_closed_positions_limit(em: PaperExecutionManager, window: MarketWindow, signal_enter_up: Signal) -> None:
    """get_closed_positions should respect the limit parameter."""
    # Enter and close 3 positions
    for i in range(3):
        w = MarketWindow(
            slug=f"btc-updown-5m-{1700000000 + i * 300}",
            question="Bitcoin Up or Down?",
            start_ts=1700000000 + i * 300,
            end_ts=1700000300 + i * 300,
            up_price=0.52,
            down_price=0.48,
        )
        await em.submit_order(signal_enter_up, w)
        await em.get_open_positions()
        # Close each position individually via force_close_all
        # Since force_close_all closes ALL open positions, we need to be careful
        # Actually it closes all at once. So we close after each one.
        await em.force_close_all(ExitReason.TARGET)

    # Now we should have 3 closed positions
    all_closed = await em.get_closed_positions(limit=50)
    assert len(all_closed) == 3

    # Limit to 2
    limited = await em.get_closed_positions(limit=2)
    assert len(limited) == 2


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_and_position_counts(em: PaperExecutionManager, window: MarketWindow, signal_enter_up: Signal, signal_skip: Signal) -> None:
    """get_order_count and get_position_count should be accurate."""
    assert await em.get_order_count() == 0
    assert await em.get_position_count() == 0

    await em.submit_order(signal_enter_up, window)
    assert await em.get_order_count() == 1
    assert await em.get_position_count() == 1

    await em.submit_order(signal_skip, window)
    assert await em.get_order_count() == 2
    assert await em.get_position_count() == 1  # SKIP doesn't create position


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tick(window: MarketWindow, remaining_seconds: int, edge: float) -> Tick:
    """Build a minimal Tick for exit checking tests."""
    # Compute ts so that remaining_seconds = window.end_ts - ts.timestamp()
    ts = datetime.fromtimestamp(window.end_ts - remaining_seconds, tz=UTC)

    return Tick(
        ts=ts,
        window=window,
        spot_price=50000.0,
        reference_price=49900.0,
        sigma_per_second=0.0002,
        fair_up_prob=0.55,
        signal=Signal(
            action=SignalAction.SKIP,
            side=None,
            confidence=0.5,
            notional_usd=0.0,
            edge=edge,
            fair_up_prob=0.55,
            reason="test tick",
        ),
        feed_source="binance",
    )
