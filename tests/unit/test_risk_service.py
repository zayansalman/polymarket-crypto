"""Unit tests for RiskService — pre/post-trade checks, drawdown, and state transitions."""

from __future__ import annotations

import pytest

from polymarket_exec.core.types import (
    OrderState,
    PaperOrder,
    PaperPosition,
    Side,
    Signal,
    SignalAction,
)
from polymarket_exec.execution.risk import RiskService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def risk_service() -> RiskService:
    return RiskService(
        max_open_positions=1,
        max_exposure_usd=5.0,
        max_drawdown_usd=50.0,
        entry_min_remaining=60,
    )


@pytest.fixture
def risk_service_multi() -> RiskService:
    """Risk service allowing multiple positions."""
    return RiskService(
        max_open_positions=3,
        max_exposure_usd=15.0,
        max_drawdown_usd=100.0,
        entry_min_remaining=60,
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
def signal_excessive_notional() -> Signal:
    """Signal with notional exceeding max_exposure_usd=5.0."""
    return Signal(
        action=SignalAction.ENTER_UP,
        side=Side.UP,
        confidence=0.90,
        notional_usd=10.0,
        edge=0.15,
        fair_up_prob=0.70,
        reason="enter Up: edge +0.150",
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


@pytest.fixture
def filled_order() -> PaperOrder:
    return PaperOrder(
        order_id=1,
        created_at=__import__("datetime").datetime(2024, 1, 1, 12, 0, 0),
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


# ---------------------------------------------------------------------------
# pre_trade_check — basic pass/fail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_trade_check_passes(risk_service: RiskService, signal_enter_up: Signal) -> None:
    """pre_trade_check should pass with zero open positions and valid signal."""
    assert await risk_service.pre_trade_check(signal_enter_up, []) is True


@pytest.mark.asyncio
async def test_pre_trade_check_fails_on_max_positions(risk_service: RiskService, signal_enter_up: Signal) -> None:
    """pre_trade_check should fail when open_positions >= max_open_positions."""
    # Create a mock open position
    mock_order = PaperOrder(
        order_id=1,
        created_at=__import__("datetime").datetime(2024, 1, 1, 12, 0, 0),
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
    mock_pos = PaperPosition(
        position_id=1,
        order=mock_order,
        opened_at=__import__("datetime").datetime(2024, 1, 1, 12, 0, 0),
    )

    # max_open_positions=1, so 1 open position should block new entries
    assert await risk_service.pre_trade_check(signal_enter_up, [mock_pos]) is False


@pytest.mark.asyncio
async def test_pre_trade_check_fails_on_zero_notional(risk_service: RiskService) -> None:
    """pre_trade_check should fail when notional is zero or negative."""
    signal_zero = Signal(
        action=SignalAction.ENTER_UP,
        side=Side.UP,
        confidence=0.75,
        notional_usd=0.0,
        edge=0.08,
        fair_up_prob=0.60,
        reason="enter Up: edge +0.080",
    )
    assert await risk_service.pre_trade_check(signal_zero, []) is False


@pytest.mark.asyncio
async def test_pre_trade_check_fails_on_negative_notional(risk_service: RiskService) -> None:
    """pre_trade_check should fail when notional is negative."""
    signal_neg = Signal(
        action=SignalAction.ENTER_UP,
        side=Side.UP,
        confidence=0.75,
        notional_usd=-1.0,
        edge=0.08,
        fair_up_prob=0.60,
        reason="enter Up: edge +0.080",
    )
    assert await risk_service.pre_trade_check(signal_neg, []) is False


@pytest.mark.asyncio
async def test_pre_trade_check_fails_on_max_exposure(risk_service: RiskService, signal_excessive_notional: Signal) -> None:
    """pre_trade_check should fail when notional exceeds max_exposure_usd."""
    # max_exposure_usd = 5.0, signal notional = 10.0
    assert await risk_service.pre_trade_check(signal_excessive_notional, []) is False


@pytest.mark.asyncio
async def test_pre_trade_check_fails_on_skip_signal(risk_service: RiskService, signal_skip: Signal) -> None:
    """pre_trade_check should fail for SKIP signals."""
    assert await risk_service.pre_trade_check(signal_skip, []) is False


@pytest.mark.asyncio
async def test_pre_trade_check_passes_with_room(risk_service_multi: RiskService, signal_enter_up: Signal) -> None:
    """pre_trade_check should pass when below max_open_positions limit."""
    # max_open_positions=3, only 1 position open
    mock_order = PaperOrder(
        order_id=1,
        created_at=__import__("datetime").datetime(2024, 1, 1, 12, 0, 0),
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
    mock_pos = PaperPosition(
        position_id=1,
        order=mock_order,
        opened_at=__import__("datetime").datetime(2024, 1, 1, 12, 0, 0),
    )

    assert await risk_service_multi.pre_trade_check(signal_enter_up, [mock_pos]) is True


@pytest.mark.asyncio
async def test_pre_trade_check_at_exact_limit_fails(risk_service: RiskService, signal_enter_up: Signal) -> None:
    """pre_trade_check should fail at exactly max_open_positions."""
    mock_order = PaperOrder(
        order_id=1,
        created_at=__import__("datetime").datetime(2024, 1, 1, 12, 0, 0),
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
    mock_pos = PaperPosition(
        position_id=1,
        order=mock_order,
        opened_at=__import__("datetime").datetime(2024, 1, 1, 12, 0, 0),
    )

    # >= max_open_positions (1) should fail
    assert await risk_service.pre_trade_check(signal_enter_up, [mock_pos]) is False


# ---------------------------------------------------------------------------
# get_risk_state transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_risk_state_ok_initially(risk_service: RiskService) -> None:
    """Fresh RiskService should report OK."""
    assert await risk_service.get_risk_state() == "OK"


@pytest.mark.asyncio
async def test_risk_state_warning_on_drawdown(risk_service: RiskService) -> None:
    """Drawdown at WARNING threshold → WARNING state."""
    # WARNING threshold = max_drawdown * _WARNING_DRAWDOWN_RATIO = 50 * 0.5 = 25
    # Simulate: peak at $100, total_pnl drops to $75 -> drawdown = $25
    risk_service.on_position_opened(5.0)
    # Close a winning position to set peak
    risk_service.on_position_closed(5.0, 100.0)  # peak_pnl = 100, total_pnl = 100
    # Now close a losing position
    risk_service.on_position_closed(5.0, -25.0)  # total_pnl = 75, drawdown = 25

    state = await risk_service.get_risk_state()
    assert state == "WARNING"


@pytest.mark.asyncio
async def test_risk_state_breach_on_drawdown(risk_service: RiskService) -> None:
    """Drawdown at BREACH threshold → BREACH state."""
    # BREACH threshold = max_drawdown * _BREACH_DRAWDOWN_RATIO = 50 * 0.9 = 45
    risk_service.on_position_opened(5.0)
    risk_service.on_position_closed(5.0, 100.0)  # peak = 100, total = 100
    risk_service.on_position_closed(5.0, -46.0)  # total = 54, drawdown = 46

    state = await risk_service.get_risk_state()
    assert state == "BREACH"


@pytest.mark.asyncio
async def test_risk_state_breach_on_exposure(risk_service: RiskService) -> None:
    """Exposure exceeding max_exposure_usd → BREACH."""
    risk_service.on_position_opened(6.0)  # max_exposure_usd = 5.0

    state = await risk_service.get_risk_state()
    assert state == "BREACH"


# ---------------------------------------------------------------------------
# Drawdown calculation
# ---------------------------------------------------------------------------


def test_drawdown_tracks_peak(risk_service: RiskService) -> None:
    """Drawdown should be measured from the peak PnL."""
    risk_service.on_position_opened(5.0)
    risk_service.on_position_closed(5.0, 10.0)  # peak=10, total=10, dd=0
    assert risk_service._peak_pnl == 10.0
    assert risk_service._current_drawdown == 0.0

    risk_service.on_position_closed(5.0, -3.0)  # total=7, dd=3
    assert risk_service._current_drawdown == 3.0

    risk_service.on_position_closed(5.0, 8.0)  # total=15, peak=15, dd=0
    assert risk_service._peak_pnl == 15.0
    assert risk_service._current_drawdown == 0.0

    risk_service.on_position_closed(5.0, -20.0)  # total=-5, dd=20
    assert risk_service._current_drawdown == 20.0


def test_drawdown_never_negative(risk_service: RiskService) -> None:
    """Drawdown should never be negative."""
    risk_service.on_position_opened(5.0)
    risk_service.on_position_closed(5.0, 10.0)
    assert risk_service._current_drawdown == 0.0

    # Winning above peak
    risk_service.on_position_closed(5.0, 5.0)
    assert risk_service._current_drawdown == 0.0


# ---------------------------------------------------------------------------
# Win/loss tracking
# ---------------------------------------------------------------------------


def test_win_loss_tracking(risk_service: RiskService) -> None:
    """Win and loss counts should be tracked correctly."""
    risk_service.on_position_opened(5.0)
    risk_service.on_position_closed(5.0, 1.0)  # win
    assert risk_service._win_count == 1
    assert risk_service._loss_count == 0

    risk_service.on_position_opened(5.0)
    risk_service.on_position_closed(5.0, -0.5)  # loss
    assert risk_service._win_count == 1
    assert risk_service._loss_count == 1

    risk_service.on_position_opened(5.0)
    risk_service.on_position_closed(5.0, 0.0)  # breakeven — neither win nor loss
    assert risk_service._win_count == 1
    assert risk_service._loss_count == 1


def test_win_loss_with_multiple_trades(risk_service: RiskService) -> None:
    """Multiple sequential trades accumulate correctly."""
    for _ in range(5):
        risk_service.on_position_opened(5.0)
        risk_service.on_position_closed(5.0, 2.0)  # 5 wins

    for _ in range(3):
        risk_service.on_position_opened(5.0)
        risk_service.on_position_closed(5.0, -1.0)  # 3 losses

    assert risk_service._win_count == 5
    assert risk_service._loss_count == 3
    assert risk_service._total_pnl == 5 * 2.0 + 3 * (-1.0)  # 10 - 3 = 7


# ---------------------------------------------------------------------------
# Exposure tracking
# ---------------------------------------------------------------------------


def test_exposure_tracks_open_positions(risk_service: RiskService) -> None:
    """Exposure should increase on open and decrease on close."""
    assert risk_service._exposure == 0.0

    risk_service.on_position_opened(3.0)
    assert risk_service._exposure == 3.0

    risk_service.on_position_opened(2.0)
    assert risk_service._exposure == 5.0

    risk_service.on_position_closed(3.0, 0.5)
    assert risk_service._exposure == 2.0

    risk_service.on_position_closed(2.0, -0.3)
    assert risk_service._exposure == 0.0


def test_exposure_never_negative(risk_service: RiskService) -> None:
    """Exposure should never go below zero."""
    risk_service.on_position_opened(3.0)
    risk_service.on_position_closed(5.0, 0.0)  # closing more than opened
    assert risk_service._exposure == 0.0


# ---------------------------------------------------------------------------
# post_trade_report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_trade_report_structure(risk_service: RiskService, filled_order: PaperOrder) -> None:
    """post_trade_report should return expected keys and values."""
    risk_service.on_position_opened(filled_order.filled_notional)
    report = await risk_service.post_trade_report(filled_order)

    assert "exposure" in report
    assert "open_position_count" in report
    assert "total_pnl" in report
    assert "current_drawdown" in report
    assert "peak_pnl" in report
    assert "win_count" in report
    assert "loss_count" in report
    assert "risk_state" in report

    assert report["exposure"] == filled_order.filled_notional
    assert report["risk_state"] == "OK"


@pytest.mark.asyncio
async def test_post_trade_report_after_close(risk_service: RiskService, filled_order: PaperOrder) -> None:
    """post_trade_report after closing a profitable position."""
    risk_service.on_position_opened(filled_order.filled_notional)
    risk_service.on_position_closed(filled_order.filled_notional, 1.5)

    report = await risk_service.post_trade_report(filled_order)
    assert report["total_pnl"] == 1.5
    assert report["win_count"] == 1
    assert report["loss_count"] == 0
    assert report["current_drawdown"] == 0.0


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def test_reset_clears_all(risk_service: RiskService) -> None:
    """reset should zero all internal counters."""
    risk_service.on_position_opened(5.0)
    risk_service.on_position_closed(5.0, 10.0)
    risk_service.on_position_closed(5.0, -3.0)

    assert risk_service._exposure == 0.0
    assert risk_service._win_count == 1
    assert risk_service._total_pnl == 7.0

    risk_service.reset()

    assert risk_service._exposure == 0.0
    assert risk_service._peak_pnl == 0.0
    assert risk_service._current_drawdown == 0.0
    assert risk_service._win_count == 0
    assert risk_service._loss_count == 0
    assert risk_service._total_pnl == 0.0
    assert risk_service._last_update is None
