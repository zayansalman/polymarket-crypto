"""Unit tests for core domain types and enums."""

from __future__ import annotations

import pytest
from dataclasses import FrozenInstanceError

from polymarket_exec.core.exceptions import (
    BtcBotError,
    ConfigurationError,
    FeedError,
    MarketDiscoveryError,
    RiskBreach,
)
from polymarket_exec.core.types import (
    BacktestMetrics,
    BacktestParams,
    BtcBotStatus,
    BtcHistoryStats,
    BuyOpportunity,
    ExitReason,
    MarketWindow,
    OrderState,
    PaperPosition,
    PaperSnapshot,
    PaperSummary,
    Side,
    Signal,
    SignalAction,
    StrategyParams,
    Tick,
)


# ---------------------------------------------------------------------------
# Enum value tests
# ---------------------------------------------------------------------------


class TestSide:
    def test_up_value(self) -> None:
        assert Side.UP == "Up"
        assert Side.UP.value == "Up"

    def test_down_value(self) -> None:
        assert Side.DOWN == "Down"
        assert Side.DOWN.value == "Down"

    def test_members(self) -> None:
        assert set(Side) == {Side.UP, Side.DOWN}


class TestSignalAction:
    def test_members_exist(self) -> None:
        assert SignalAction.SKIP is not None
        assert SignalAction.ENTER_UP is not None
        assert SignalAction.ENTER_DOWN is not None

    def test_auto_values_are_unique(self) -> None:
        values = {a.value for a in SignalAction}
        assert len(values) == 3


class TestOrderState:
    def test_all_members(self) -> None:
        expected = {
            "PENDING",
            "ACKNOWLEDGED",
            "FILLED",
            "PARTIAL_FILL",
            "CANCELLED",
            "REJECTED",
        }
        actual = {s.name for s in OrderState}
        assert actual == expected

    def test_auto_values_are_unique(self) -> None:
        values = {s.value for s in OrderState}
        assert len(values) == 6


class TestExitReason:
    def test_string_values(self) -> None:
        assert ExitReason.TARGET == "TARGET"
        assert ExitReason.STOP == "STOP"
        assert ExitReason.TIME == "TIME"
        assert ExitReason.WINDOW_ROLL == "WINDOW_ROLL"
        assert ExitReason.BAND_REENTRY == "BAND_REENTRY"
        assert ExitReason.STOP_REQUEST == "STOP_REQUEST"

    def test_all_members(self) -> None:
        assert len(set(ExitReason)) == 6


# ---------------------------------------------------------------------------
# Frozen dataclass immutability
# ---------------------------------------------------------------------------


class TestFrozenDataclasses:
    def test_market_window_is_frozen(self) -> None:
        w = MarketWindow(
            slug="test", question="Q", start_ts=0, end_ts=300,
            up_price=0.5, down_price=0.5,
        )
        with pytest.raises(FrozenInstanceError):
            w.slug = "changed"

    def test_signal_is_frozen(self) -> None:
        s = Signal(
            action=SignalAction.SKIP, side=None, confidence=0.5,
            notional_usd=0.0, edge=0.0, fair_up_prob=0.5, reason="test",
        )
        with pytest.raises(FrozenInstanceError):
            s.confidence = 0.9

    def test_tick_is_frozen(self, sample_window: MarketWindow) -> None:
        from datetime import datetime, timezone
        sig = Signal(
            action=SignalAction.SKIP, side=None, confidence=0.5,
            notional_usd=0.0, edge=0.0, fair_up_prob=0.5, reason="t",
        )
        t = Tick(
            ts=datetime.now(timezone.utc), window=sample_window,
            spot_price=50000.0, reference_price=49900.0,
            sigma_per_second=0.0002, fair_up_prob=0.55,
            signal=sig, feed_source="test",
        )
        with pytest.raises(FrozenInstanceError):
            t.spot_price = 60000.0

    def test_strategy_params_is_frozen(self) -> None:
        p = StrategyParams(
            min_trade_usd=1.0, max_trade_usd=5.0,
            entry_edge_min=0.05, min_confidence=0.60,
        )
        with pytest.raises(FrozenInstanceError):
            p.entry_edge_min = 0.10

    def test_backtest_params_is_frozen(self) -> None:
        p = BacktestParams(
            entry_edge_min=0.05, min_confidence=0.60,
            min_remaining_seconds=90, max_entry_price=0.95,
        )
        with pytest.raises(FrozenInstanceError):
            p.max_entry_price = 0.80

    def test_frozen_dataclasses_are_hashable(self) -> None:
        """Frozen dataclasses can be used as dict keys / in sets."""
        w = MarketWindow(
            slug="a", question="Q", start_ts=0, end_ts=300,
            up_price=0.5, down_price=0.5,
        )
        d = {w: 42}
        assert d[w] == 42


# ---------------------------------------------------------------------------
# Signal creation
# ---------------------------------------------------------------------------


class TestSignal:
    def test_skip_signal(self) -> None:
        s = Signal(
            action=SignalAction.SKIP,
            side=None,
            confidence=0.55,
            notional_usd=0.0,
            edge=0.02,
            fair_up_prob=0.54,
            reason="skip: edge/confidence below threshold",
        )
        assert s.action is SignalAction.SKIP
        assert s.side is None
        assert s.notional_usd == 0.0

    def test_enter_up_signal(self) -> None:
        s = Signal(
            action=SignalAction.ENTER_UP,
            side=Side.UP,
            confidence=0.80,
            notional_usd=5.0,
            edge=0.10,
            fair_up_prob=0.62,
            reason="enter Up: edge +0.100",
        )
        assert s.action is SignalAction.ENTER_UP
        assert s.side is Side.UP
        assert s.notional_usd == 5.0

    def test_enter_down_signal(self) -> None:
        s = Signal(
            action=SignalAction.ENTER_DOWN,
            side=Side.DOWN,
            confidence=0.80,
            notional_usd=5.0,
            edge=-0.10,
            fair_up_prob=0.38,
            reason="enter Down: edge -0.100",
        )
        assert s.action is SignalAction.ENTER_DOWN
        assert s.side is Side.DOWN


# ---------------------------------------------------------------------------
# PaperPosition with optional fields
# ---------------------------------------------------------------------------


class TestPaperPosition:
    def test_open_position(self, sample_position: PaperPosition) -> None:
        assert sample_position.closed_at is None
        assert sample_position.exit_price is None
        assert sample_position.exit_reason is None
        assert sample_position.realized_pnl_usd is None

    def test_closed_position(self, closed_position: PaperPosition) -> None:
        assert closed_position.closed_at is not None
        assert closed_position.exit_price == 0.95
        assert closed_position.exit_reason is not None
        assert closed_position.realized_pnl_usd == 1.24

    def test_mutable_fields(self, sample_position: PaperPosition) -> None:
        """PaperPosition is mutable — fields can be updated as it evolves."""
        from datetime import datetime, timezone
        sample_position.closed_at = datetime.now(timezone.utc)
        sample_position.exit_price = 0.90
        sample_position.exit_reason = "TARGET"
        sample_position.realized_pnl_usd = 0.50
        assert sample_position.exit_price == 0.90


# ---------------------------------------------------------------------------
# PaperSnapshot
# ---------------------------------------------------------------------------


class TestPaperSnapshot:
    def test_creation(self) -> None:
        snap = PaperSnapshot(
            created_at="2024-01-01T12:00:00+00:00",
            window_slug="btc-updown-5m-1700000000",
            market_question="Bitcoin Up or Down?",
            remaining_seconds=180,
            spot_price=50000.0,
            reference_price=49900.0,
            sigma_per_second=0.0002,
            market_up_price=0.52,
            market_down_price=0.48,
            fair_up_prob=0.55,
            edge=0.03,
            signal_side="Up",
            confidence=0.65,
            notional_usd=2.0,
            reason="enter Up: edge +0.030",
            feed_source="binance",
        )
        assert snap.window_slug == "btc-updown-5m-1700000000"
        assert snap.signal_side == "Up"
        assert snap.notional_usd == 2.0

    def test_null_signal_side(self) -> None:
        snap = PaperSnapshot(
            created_at="2024-01-01T12:00:00+00:00",
            window_slug="btc-updown-5m-1700000000",
            market_question="Bitcoin Up or Down?",
            remaining_seconds=180,
            spot_price=50000.0,
            reference_price=49900.0,
            sigma_per_second=0.0002,
            market_up_price=0.52,
            market_down_price=0.48,
            fair_up_prob=0.55,
            edge=0.01,
            signal_side=None,
            confidence=0.52,
            notional_usd=0.0,
            reason="skip: edge/confidence below threshold",
            feed_source="binance",
        )
        assert snap.signal_side is None
        assert snap.notional_usd == 0.0


# ---------------------------------------------------------------------------
# PaperSummary
# ---------------------------------------------------------------------------


class TestPaperSummary:
    def test_creation(self) -> None:
        summary = PaperSummary(
            running_state="paper",
            open_positions=1,
            closed_positions=10,
            total_pnl_usd=5.50,
            open_exposure_usd=3.0,
            closed_notional_usd=30.0,
            win_rate=0.60,
            avg_pnl_usd=0.55,
            avg_hold_seconds=150.0,
            risk_state="OK",
            last_signal="Up conf 0.75 $3: enter Up: edge +0.080",
            last_tick_at="2024-01-01T12:00:00+00:00",
            last_window_slug="btc-updown-5m-1700000000",
            last_spot_price=50000.0,
            last_fair_up_prob=0.60,
            last_up_price=0.52,
            last_edge=0.08,
            last_feed_source="binance",
        )
        assert summary.running_state == "paper"
        assert summary.win_rate == 0.60
        assert summary.recent_positions == []

    def test_with_recent_positions(self) -> None:
        summary = PaperSummary(
            running_state="paper",
            open_positions=0,
            closed_positions=2,
            total_pnl_usd=1.20,
            open_exposure_usd=0.0,
            closed_notional_usd=6.0,
            win_rate=1.0,
            avg_pnl_usd=0.60,
            avg_hold_seconds=120.0,
            risk_state="OK",
            last_signal="none",
            last_tick_at=None,
            last_window_slug=None,
            last_spot_price=None,
            last_fair_up_prob=None,
            last_up_price=None,
            last_edge=None,
            last_feed_source=None,
            recent_positions=[{"position_id": 1, "side": "Up", "pnl": 0.80}],
        )
        assert len(summary.recent_positions) == 1


# ---------------------------------------------------------------------------
# BtcBotStatus
# ---------------------------------------------------------------------------


class TestBtcBotStatus:
    def test_creation(self) -> None:
        status = BtcBotStatus(
            state="running",
            mode="paper",
            updated_at="2024-01-01T12:00:00+00:00",
            detail="BTC paper loop running.",
        )
        assert status.state == "running"
        assert status.mode == "paper"


# ---------------------------------------------------------------------------
# BtcHistoryStats
# ---------------------------------------------------------------------------


class TestBtcHistoryStats:
    def test_not_found(self) -> None:
        stats = BtcHistoryStats(path="/tmp/nonexistent.csv", found=False)
        assert stats.found is False
        assert stats.total_rows == 0

    def test_found_with_data(self) -> None:
        stats = BtcHistoryStats(
            path="/tmp/history.csv",
            found=True,
            total_rows=150,
            btc_rows=50,
            buys=30,
            sells=10,
            redeems=5,
            buy_usdc_total=100.0,
            buy_usdc_avg=3.33,
            buy_usdc_median=3.0,
            buy_usdc_min=1.0,
            buy_usdc_max=10.0,
            one_to_five_buy_share=0.80,
        )
        assert stats.found is True
        assert stats.buy_usdc_total == 100.0


# ---------------------------------------------------------------------------
# BuyOpportunity
# ---------------------------------------------------------------------------


class TestBuyOpportunity:
    def test_creation(self) -> None:
        opp = BuyOpportunity(
            market_name="Bitcoin Up or Down - Jan 1",
            side="Up",
            trade_ts=1700000100,
            window_start_ts=1700000000,
            window_end_ts=1700000300,
            remaining_seconds=200,
            entry_price=0.52,
            actual_notional_usd=5.0,
            actual_shares=9.615,
            reference_price=50000.0,
            trade_spot_price=50100.0,
            settlement_price=50200.0,
            outcome="Up",
            fair_side_prob=0.60,
            edge=0.08,
            confidence=0.72,
            settlement_pnl_usd=0.80,
        )
        assert opp.side == "Up"
        assert opp.settlement_pnl_usd == 0.80


# ---------------------------------------------------------------------------
# BacktestMetrics
# ---------------------------------------------------------------------------


class TestBacktestMetrics:
    def test_creation(self) -> None:
        m = BacktestMetrics(
            name="test_run",
            params={"edge": 0.05, "conf": 0.60},
            opportunities=100,
            trades=50,
            wins=30,
            losses=20,
            skipped=50,
            total_notional_usd=150.0,
            total_pnl_usd=10.0,
            roi=0.0667,
            win_rate=0.60,
            avg_pnl_usd=0.20,
            max_drawdown_usd=2.5,
            score=8.20,
        )
        assert m.name == "test_run"
        assert m.trades == 50
        assert m.wins == 30


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class TestExceptions:
    def test_polymarket_bot_error_is_base(self) -> None:
        with pytest.raises(BtcBotError):
            raise BtcBotError("base error")

    def test_feed_error_is_subclass(self) -> None:
        assert issubclass(FeedError, BtcBotError)
        with pytest.raises(BtcBotError):
            raise FeedError("feed failed")

    def test_market_discovery_error_is_subclass(self) -> None:
        assert issubclass(MarketDiscoveryError, BtcBotError)
        with pytest.raises(BtcBotError):
            raise MarketDiscoveryError("no market")

    def test_risk_breach_is_subclass(self) -> None:
        assert issubclass(RiskBreach, BtcBotError)
        with pytest.raises(BtcBotError):
            raise RiskBreach("limit exceeded")

    def test_configuration_error_is_subclass(self) -> None:
        assert issubclass(ConfigurationError, BtcBotError)
        with pytest.raises(BtcBotError):
            raise ConfigurationError("bad config")
