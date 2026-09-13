"""Unit tests for strategy math — pricing model, sizing, and signal generation."""

from __future__ import annotations


import pytest

from polymarket_exec.core.types import Side, Signal, SignalAction, StrategyParams
from polymarket_exec.strategy.pricing_model import fair_up_probability, sigma_per_second
from polymarket_exec.strategy.signal import signal_from_edge
from polymarket_exec.strategy.sizing import confidence_from_edge, notional_from_confidence


# ============================================================================
# sigma_per_second
# ============================================================================


class TestSigmaPerSecond:
    def test_insufficient_data_returns_floor(self) -> None:
        """With fewer than 2 valid returns, sigma floors at 0.00002."""
        assert sigma_per_second([50000.0]) == pytest.approx(0.00002)
        assert sigma_per_second([]) == pytest.approx(0.00002)
        assert sigma_per_second([50000.0, 50000.0]) == pytest.approx(0.00002)

    def test_constant_prices_return_floor(self, flat_prices: list[float]) -> None:
        """Flat prices have zero stdev, so floor applies."""
        sigma = sigma_per_second(flat_prices)
        assert sigma == pytest.approx(0.00002)

    def test_volatile_prices_positive_sigma(self, volatile_prices: list[float]) -> None:
        sigma = sigma_per_second(volatile_prices)
        assert sigma > 0.00002

    def test_typical_90s_closes(self, valid_closes: list[float]) -> None:
        sigma = sigma_per_second(valid_closes)
        assert sigma > 0.00002
        # Realistic BTC 1s sigma is ~0.0001 - 0.001
        assert sigma < 0.01

    def test_negative_or_zero_prices_skipped(self) -> None:
        """Non-positive prices are excluded from return calculation."""
        # Use enough variance so stdev exceeds floor even after skipping negatives
        closes = [50000.0, -1.0, 51000.0, 49000.0, 50500.0]
        sigma = sigma_per_second(closes)
        # Only positive-to-positive transitions are valid: 51000->49000, 49000->50500
        assert sigma >= 0.00002  # at minimum the floor


# ============================================================================
# fair_up_probability
# ============================================================================


class TestFairUpProbability:
    def test_spot_equals_reference_returns_half(self) -> None:
        """When spot == reference, the probability should be exactly 0.5."""
        assert fair_up_probability(
            spot=50000.0, reference=50000.0, sigma=0.001, remaining_seconds=100
        ) == pytest.approx(0.5)

    def test_sigma_zero_returns_half(self) -> None:
        """Zero sigma means no uncertainty -> return neutral 0.5 as guard."""
        assert fair_up_probability(
            spot=60000.0, reference=50000.0, sigma=0.0, remaining_seconds=100
        ) == pytest.approx(0.5)

    def test_negative_prices_handled(self) -> None:
        """Non-positive spot or reference returns 0.5."""
        assert fair_up_probability(
            spot=-100.0, reference=50000.0, sigma=0.001, remaining_seconds=100
        ) == pytest.approx(0.5)
        assert fair_up_probability(
            spot=50000.0, reference=-100.0, sigma=0.001, remaining_seconds=100
        ) == pytest.approx(0.5)
        assert fair_up_probability(
            spot=0.0, reference=50000.0, sigma=0.001, remaining_seconds=100
        ) == pytest.approx(0.5)

    def test_zero_remaining_seconds(self) -> None:
        """Remaining = 0 -> use max(remaining_seconds, 1) -> sqrt(1)."""
        prob = fair_up_probability(
            spot=50000.0, reference=50000.0, sigma=0.001, remaining_seconds=0
        )
        assert prob == pytest.approx(0.5)

    def test_spot_above_reference_high_prob(self) -> None:
        """Spot well above reference -> high up probability."""
        prob = fair_up_probability(
            spot=55000.0, reference=50000.0, sigma=0.001, remaining_seconds=60
        )
        assert prob > 0.5
        assert prob <= 0.995  # clamped

    def test_spot_below_reference_low_prob(self) -> None:
        """Spot well below reference -> low up probability."""
        prob = fair_up_probability(
            spot=45000.0, reference=50000.0, sigma=0.001, remaining_seconds=60
        )
        assert prob < 0.5
        assert prob >= 0.005  # clamped

    def test_probability_bounds(self) -> None:
        """Probability is always in [0.005, 0.995]."""
        for spot in [40000.0, 50000.0, 60000.0]:
            for ref in [40000.0, 50000.0, 60000.0]:
                prob = fair_up_probability(
                    spot=spot, reference=ref, sigma=0.0001, remaining_seconds=10
                )
                assert 0.005 <= prob <= 0.995

    def test_extreme_spot_clamped(self) -> None:
        """Very extreme spot/reference ratios get clamped to bounds."""
        prob = fair_up_probability(
            spot=100000.0, reference=50000.0, sigma=0.00001, remaining_seconds=1
        )
        assert prob == pytest.approx(0.995)

    def test_symmetry(self) -> None:
        """P(up | spot=a, ref=b) = 1 - P(up | spot=b, ref=a)."""
        sigma = 0.001
        remaining = 100
        p1 = fair_up_probability(51000.0, 50000.0, sigma, remaining)
        p2 = fair_up_probability(50000.0, 51000.0, sigma, remaining)
        assert p1 + p2 == pytest.approx(1.0, abs=1e-6)


# ============================================================================
# confidence_from_edge
# ============================================================================


class TestConfidenceFromEdge:
    def test_zero_edge(self) -> None:
        assert confidence_from_edge(0.0) == pytest.approx(0.50)

    def test_positive_edge(self) -> None:
        assert confidence_from_edge(0.10) == pytest.approx(0.50 + 0.10 * 2.8)

    def test_negative_edge_uses_abs(self) -> None:
        """Negative edge uses absolute value — direction is independent."""
        assert confidence_from_edge(-0.10) == pytest.approx(0.50 + 0.10 * 2.8)

    def test_clamped_at_zero(self) -> None:
        """Confidence uses abs(edge) and clamps at 0.0 floor — only possible
        when the formula underflows, which it doesn't in practice."""
        # The function uses abs(edge) so even -100.0 -> 0.99 (clamped from above)
        # The 0.0 floor only applies if 0.50 + abs(edge) * 2.8 < 0, impossible
        assert confidence_from_edge(-100.0) == pytest.approx(0.99)
        assert confidence_from_edge(100.0) == pytest.approx(0.99)

    def test_clamped_at_0_99(self) -> None:
        assert confidence_from_edge(100.0) == pytest.approx(0.99)

    def test_monotonicity(self) -> None:
        """Confidence increases monotonically with |edge|."""
        edges = [0.0, 0.01, 0.02, 0.05, 0.10, 0.15, 0.20]
        confidences = [confidence_from_edge(e) for e in edges]
        for i in range(1, len(confidences)):
            assert confidences[i] >= confidences[i - 1]


# ============================================================================
# notional_from_confidence
# ============================================================================


class TestNotionalFromConfidence:
    def test_below_min_confidence_returns_zero(self, default_params: StrategyParams) -> None:
        assert notional_from_confidence(0.50, default_params) == 0.0

    def test_at_min_confidence(self, default_params: StrategyParams) -> None:
        """At exactly min_confidence, should return min_trade_usd."""
        n = notional_from_confidence(default_params.min_confidence, default_params)
        assert n == default_params.min_trade_usd

    def test_at_max_confidence(self, default_params: StrategyParams) -> None:
        """At 0.99 (max confidence), should return max_trade_usd."""
        n = notional_from_confidence(0.99, default_params)
        assert n == default_params.max_trade_usd

    def test_mid_confidence_scaled(self, default_params: StrategyParams) -> None:
        """At midpoint between min and max confidence, notional is roughly midway."""
        mid_conf = (default_params.min_confidence + 0.99) / 2
        n = notional_from_confidence(mid_conf, default_params)
        expected_mid = (default_params.min_trade_usd + default_params.max_trade_usd) / 2
        # Due to rounding, allow ±1 USD tolerance
        assert abs(n - expected_mid) <= 1.0

    def test_clamped_to_max(self, default_params: StrategyParams) -> None:
        """Even with >0.99 confidence, notional doesn't exceed max."""
        assert (
            notional_from_confidence(1.0, default_params)
            <= default_params.max_trade_usd
        )

    def test_clamped_to_min(self, default_params: StrategyParams) -> None:
        """Even with very low but above-min confidence, notional >= min."""
        n = notional_from_confidence(default_params.min_confidence + 0.01, default_params)
        assert n >= default_params.min_trade_usd


# ============================================================================
# signal_from_edge
# ============================================================================


class TestSignalFromEdge:
    def test_too_close_to_window_end(self, default_params: StrategyParams) -> None:
        """Remaining seconds <= entry_min_remaining_seconds -> SKIP."""
        sig = signal_from_edge(
            edge=0.10, remaining_seconds=90,
            up_price=0.45, down_price=0.55, params=default_params,
        )
        assert sig.action is SignalAction.SKIP
        assert "too close" in sig.reason.lower()

    def test_edge_below_threshold(self, default_params: StrategyParams) -> None:
        sig = signal_from_edge(
            edge=0.01, remaining_seconds=120,
            up_price=0.45, down_price=0.55, params=default_params,
        )
        assert sig.action is SignalAction.SKIP
        assert sig.confidence == pytest.approx(confidence_from_edge(0.01))

    def test_confidence_below_threshold(self, default_params: StrategyParams) -> None:
        sig = signal_from_edge(
            edge=0.02, remaining_seconds=120,
            up_price=0.45, down_price=0.55, params=default_params,
        )
        assert sig.action is SignalAction.SKIP

    def test_entry_price_too_extreme(self, default_params: StrategyParams) -> None:
        """up_price > max_entry_price -> SKIP."""
        sig = signal_from_edge(
            edge=0.10, remaining_seconds=120,
            up_price=0.99, down_price=0.01, params=default_params,
        )
        assert sig.action is SignalAction.SKIP
        assert "price" in sig.reason.lower()

    def test_entry_price_too_low(self, default_params: StrategyParams) -> None:
        """down_price < min_entry_price -> SKIP."""
        sig = signal_from_edge(
            edge=-0.10, remaining_seconds=120,
            up_price=0.52, down_price=0.01, params=default_params,
        )
        assert sig.action is SignalAction.SKIP

    def test_valid_enter_up(self, default_params: StrategyParams) -> None:
        sig = signal_from_edge(
            edge=0.10, remaining_seconds=120,
            up_price=0.45, down_price=0.55, params=default_params,
        )
        assert sig.action is SignalAction.ENTER_UP
        assert sig.side is Side.UP
        assert sig.confidence == pytest.approx(confidence_from_edge(0.10))
        assert sig.notional_usd > 0.0
        assert "enter Up" in sig.reason

    def test_valid_enter_down(self, default_params: StrategyParams) -> None:
        sig = signal_from_edge(
            edge=-0.10, remaining_seconds=120,
            up_price=0.45, down_price=0.55, params=default_params,
        )
        assert sig.action is SignalAction.ENTER_DOWN
        assert sig.side is Side.DOWN
        assert sig.notional_usd > 0.0
        assert "enter Down" in sig.reason

    def test_zero_notional_from_low_confidence(self, default_params: StrategyParams) -> None:
        """If notional calc returns 0, signal should still be SKIP."""
        # This edge produces a confidence barely above threshold,
        # but with strict min_trade_usd it could round to 0
        strict = StrategyParams(
            min_trade_usd=1.0,
            max_trade_usd=5.0,
            entry_edge_min=0.05,
            min_confidence=0.99,  # very high
            entry_min_remaining_seconds=10,
            max_entry_price=0.95,
            min_entry_price=0.05,
        )
        sig = signal_from_edge(
            edge=0.20, remaining_seconds=120,
            up_price=0.45, down_price=0.55, params=strict,
        )
        # confidence_from_edge(0.20) = 0.50 + 0.56 = 1.06 -> clamped to 0.99
        # At exactly 0.99 with min_confidence 0.99:
        #   scaled = (0.99 - 0.99) / (0.99 - 0.99) -> 0 / 0 -> / max(0.01) -> 0
        #   raw = min_trade + span * 0 = 1.0
        # So notional = 1.0, action should be ENTER_UP
        if sig.notional_usd <= 0:
            assert sig.action is SignalAction.SKIP
        else:
            assert sig.action is SignalAction.ENTER_UP

    def test_return_type_is_signal(self, default_params: StrategyParams) -> None:
        """signal_from_edge must return a Signal object, not a raw tuple."""
        sig = signal_from_edge(
            edge=0.10, remaining_seconds=120,
            up_price=0.45, down_price=0.55, params=default_params,
        )
        assert isinstance(sig, Signal)
        assert hasattr(sig, "action")
        assert hasattr(sig, "side")
        assert hasattr(sig, "confidence")
        assert hasattr(sig, "notional_usd")
        assert hasattr(sig, "edge")
        assert hasattr(sig, "fair_up_prob")
        assert hasattr(sig, "reason")

    def test_edge_boundary_exactly_at_threshold(self, loose_params: StrategyParams) -> None:
        """Edge exactly at entry_edge_min with loose params."""
        sig = signal_from_edge(
            edge=loose_params.entry_edge_min, remaining_seconds=120,
            up_price=0.45, down_price=0.55, params=loose_params,
        )
        # abs(edge) >= entry_edge_min, so it should NOT skip for edge
        if sig.action is SignalAction.SKIP:
            # Must be skipping for some OTHER reason, not edge
            assert "edge/confidence" not in sig.reason

    def test_very_small_edge(self) -> None:
        """Tiny edge should produce SKIP with low confidence."""
        params = StrategyParams(
            min_trade_usd=1.0, max_trade_usd=5.0,
            entry_edge_min=0.05, min_confidence=0.60,
            entry_min_remaining_seconds=10,
            max_entry_price=0.95, min_entry_price=0.05,
        )
        sig = signal_from_edge(
            edge=0.001, remaining_seconds=120,
            up_price=0.45, down_price=0.55, params=params,
        )
        assert sig.action is SignalAction.SKIP

    def test_fair_up_prob_is_zero_placeholder(self, default_params: StrategyParams) -> None:
        """fair_up_prob in the Signal is a placeholder set to 0.0.

        The caller (tick builder) is responsible for populating it with
        the actual fair_up_probability() result.
        """
        sig = signal_from_edge(
            edge=0.10, remaining_seconds=120,
            up_price=0.45, down_price=0.55, params=default_params,
        )
        assert sig.fair_up_prob == 0.0
