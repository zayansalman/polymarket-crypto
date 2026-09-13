"""Unit tests for the shadow forward-tester's Polymarket taker-fee math."""

from __future__ import annotations

import pytest

from polymarket_bot.shadow.fees import (
    breakeven_winrate,
    net_pnl_per_share,
    taker_fee_per_share,
)


# ============================================================================
# taker_fee_per_share
# ============================================================================


class TestTakerFeePerShare:
    def test_zero_at_certain_prices(self) -> None:
        """Fee vanishes at the certain prices p=0 and p=1 (no variance to tax)."""
        assert taker_fee_per_share(0.0) == pytest.approx(0.0)
        assert taker_fee_per_share(1.0) == pytest.approx(0.0)

    def test_max_at_one_half(self) -> None:
        """Parabola peaks at p=0.5: 0.07 * 0.25 = 0.0175 (1.75 cents)."""
        assert taker_fee_per_share(0.5) == pytest.approx(0.0175)

    def test_peak_is_the_global_maximum(self) -> None:
        """No price beats the p=0.5 fee — confirms the peak location."""
        peak = taker_fee_per_share(0.5)
        for p in (0.01, 0.1, 0.25, 0.4, 0.49, 0.51, 0.6, 0.75, 0.9, 0.99):
            assert taker_fee_per_share(p) <= peak

    def test_symmetry_about_one_half(self) -> None:
        """fee(p) == fee(1 - p): the parabola is symmetric about 0.5."""
        for p in (0.05, 0.2, 0.37, 0.48):
            assert taker_fee_per_share(p) == pytest.approx(taker_fee_per_share(1.0 - p))

    def test_explicit_formula_value(self) -> None:
        """fee(0.3) = 0.07 * 0.3 * 0.7 = 0.0147."""
        assert taker_fee_per_share(0.3) == pytest.approx(0.07 * 0.3 * 0.7)

    def test_custom_fee_rate_scales_linearly(self) -> None:
        """Doubling the rate doubles the fee at a fixed price."""
        assert taker_fee_per_share(0.4, fee_rate=0.14) == pytest.approx(
            2.0 * taker_fee_per_share(0.4, fee_rate=0.07)
        )

    def test_zero_rate_is_free(self) -> None:
        assert taker_fee_per_share(0.5, fee_rate=0.0) == pytest.approx(0.0)


# ============================================================================
# net_pnl_per_share
# ============================================================================


class TestNetPnlPerShare:
    def test_win_is_positive_payoff_minus_fee(self) -> None:
        """A winning share at 0.40 grosses 0.60, less the 0.0168 fee."""
        fee = taker_fee_per_share(0.40)
        assert net_pnl_per_share(0.40, won=True) == pytest.approx(0.60 - fee)
        # Still net-positive here: the win payoff dwarfs the fee.
        assert net_pnl_per_share(0.40, won=True) > 0.0

    def test_loss_is_negative_entry_minus_fee(self) -> None:
        """A losing share forfeits the entry cost AND still pays the fee."""
        fee = taker_fee_per_share(0.40)
        assert net_pnl_per_share(0.40, won=False) == pytest.approx(-0.40 - fee)
        assert net_pnl_per_share(0.40, won=False) < 0.0

    def test_loss_is_always_more_negative_than_raw_entry(self) -> None:
        """The fee makes a loss strictly worse than just forfeiting entry."""
        for p in (0.1, 0.3, 0.5, 0.7, 0.9):
            assert net_pnl_per_share(p, won=False) < -p

    def test_win_is_strictly_below_raw_gross(self) -> None:
        """The fee shaves the winning payoff below the naive 1 - entry."""
        for p in (0.1, 0.3, 0.5, 0.7, 0.9):
            assert net_pnl_per_share(p, won=True) < (1.0 - p)

    def test_fee_charged_on_both_outcomes(self) -> None:
        """Win-minus-loss equals exactly 1.0: the per-share fee cancels.

        gross_win - gross_loss = (1 - p) - (-p) = 1, and the fee is the
        same subtraction on both legs, so it drops out of the difference.
        """
        for p in (0.2, 0.5, 0.8):
            won = net_pnl_per_share(p, won=True)
            lost = net_pnl_per_share(p, won=False)
            assert (won - lost) == pytest.approx(1.0)

    def test_zero_fee_recovers_raw_binary_payoff(self) -> None:
        assert net_pnl_per_share(0.4, won=True, fee_rate=0.0) == pytest.approx(0.6)
        assert net_pnl_per_share(0.4, won=False, fee_rate=0.0) == pytest.approx(-0.4)


# ============================================================================
# breakeven_winrate
# ============================================================================


class TestBreakevenWinrate:
    def test_equals_entry_plus_fee(self) -> None:
        """Closed form: w* = p + fee(p)."""
        for p in (0.1, 0.25, 0.5, 0.75, 0.9):
            assert breakeven_winrate(p) == pytest.approx(p + taker_fee_per_share(p))

    def test_strictly_above_entry_price_when_fee_positive(self) -> None:
        """The fee raises the bar: required win-rate exceeds the entry price."""
        for p in (0.1, 0.3, 0.5, 0.7, 0.9):
            assert breakeven_winrate(p) > p

    def test_makes_expected_net_pnl_zero(self) -> None:
        """At w = breakeven_winrate, expected net PnL per share is exactly 0.

        This is the defining property: it directly ties the derivation back
        to net_pnl_per_share rather than re-asserting the closed form.
        """
        for p in (0.15, 0.35, 0.5, 0.65, 0.85):
            w = breakeven_winrate(p)
            ev = w * net_pnl_per_share(p, won=True) + (1.0 - w) * net_pnl_per_share(p, won=False)
            assert ev == pytest.approx(0.0, abs=1e-12)

    def test_above_breakeven_is_profitable(self) -> None:
        """A win-rate one point above breakeven yields positive expected PnL."""
        for p in (0.2, 0.5, 0.8):
            w = breakeven_winrate(p) + 0.01
            ev = w * net_pnl_per_share(p, won=True) + (1.0 - w) * net_pnl_per_share(p, won=False)
            assert ev > 0.0

    def test_below_breakeven_is_unprofitable(self) -> None:
        """A win-rate one point below breakeven yields negative expected PnL."""
        for p in (0.2, 0.5, 0.8):
            w = breakeven_winrate(p) - 0.01
            ev = w * net_pnl_per_share(p, won=True) + (1.0 - w) * net_pnl_per_share(p, won=False)
            assert ev < 0.0

    def test_zero_fee_breakeven_is_entry_price(self) -> None:
        """With no fee the bar collapses to the fair binary one: w* = p."""
        for p in (0.1, 0.5, 0.9):
            assert breakeven_winrate(p, fee_rate=0.0) == pytest.approx(p)
