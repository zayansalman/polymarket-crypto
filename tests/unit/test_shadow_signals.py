"""Unit tests for the shadow forward-tester candidate signals.

Covers the candidate strategies in :mod:`polymarket_bot.shadow.signals`
(post-#142 roster: v2 champion + v7 challenger; v3/v4/v5/v6 retired):

* ``cushion_favorite_v2`` — None when v0 declines, None when the cushion is
  below the floor, a signal when both pass.
* ``cushion_fresh_v7`` — v2 restricted to the first 60s of the window with
  claimed edges capped at 0.065.

The signals are pure, so every case is a hand-built
:class:`~polymarket_bot.shadow.types.SnapshotView` plus a small local
:class:`~polymarket_bot.strategy.StrategyParams`. We deliberately build our own
params (not the ``polymarket_exec`` conftest fixture) because the candidates reuse
``polymarket_bot.strategy.signal_from_executable_edges``, which takes the
``polymarket_bot.strategy`` flavour of ``StrategyParams``.
"""

from __future__ import annotations

import pytest

from polymarket_bot import strategy
from polymarket_bot.shadow.signals import (
    cushion_favorite_v2,
    cushion_fresh_v7,
    cushion_fresh_v7_f45,
    cushion_fresh_v7_f45_spread,
    pricing_fresh_v8,
)
from polymarket_bot.shadow.types import ShadowSignal, SnapshotView


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def params() -> strategy.StrategyParams:
    """Loose params so v0 fires on a modest edge in the happy-path cases."""
    return strategy.StrategyParams(
        min_trade_usd=1.0,
        max_trade_usd=5.0,
        entry_edge_min=0.05,
        min_confidence=0.55,
        entry_min_remaining_seconds=60,
        max_entry_price=0.95,
        min_entry_price=0.05,
        entry_edge_max=1.0,
    )


def _view(**overrides: object) -> SnapshotView:
    """Build a SnapshotView from sane defaults overridden per test."""
    base: dict[str, object] = dict(
        window_slug="btc-5m-2026-06-18T16:00",
        remaining_seconds=120,
        spot=50000.0,
        reference=50000.0,
        up_ask=0.55,
        down_ask=0.46,
        market_up_price=0.55,
        fair_up=0.70,
        sigma_per_second=0.0003,
        feed_source="chainlink",
        quote_source="clob",
    )
    base.update(overrides)
    return SnapshotView(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# cushion_favorite_v2
# ---------------------------------------------------------------------------


class TestCushionFavoriteV2:
    def test_none_when_v0_declines(self, params: strategy.StrategyParams) -> None:
        """No executable quotes -> v0 returns side None -> wrapper returns None."""
        view = _view(up_ask=None, down_ask=None, reference=49980.0)
        assert cushion_favorite_v2(view, params) is None

    def test_none_when_v0_declines_thin_edge(self, params: strategy.StrategyParams) -> None:
        """Edge below v0's threshold -> v0 declines even with a big cushion."""
        # fair_up 0.70 vs up_ask 0.69 -> edge 0.01 < entry_edge_min 0.05.
        view = _view(up_ask=0.69, down_ask=0.60, fair_up=0.70, reference=49900.0)
        assert cushion_favorite_v2(view, params) is None

    def test_none_when_cushion_below_threshold(
        self, params: strategy.StrategyParams
    ) -> None:
        """v0 wants Up, but spot leads reference by only ~0.2 bps -> None."""
        # edge_up = 0.70 - 0.55 = 0.15 (v0 enters Up), but cushion is tiny.
        view = _view(spot=50000.0, reference=49999.0, up_ask=0.55, fair_up=0.70)
        cushion_bps = (50000.0 - 49999.0) / 50000.0 * 1e4
        assert cushion_bps < 1.5  # sanity: this is the gate we are exercising
        assert cushion_favorite_v2(view, params) is None

    def test_signal_when_both_pass_up(self, params: strategy.StrategyParams) -> None:
        """v0 enters Up and the cushion clears the floor -> Up signal."""
        view = _view(spot=50000.0, reference=49980.0, up_ask=0.55, fair_up=0.70)
        sig = cushion_favorite_v2(view, params)
        assert isinstance(sig, ShadowSignal)
        assert sig.side == "Up"
        assert sig.entry_price == pytest.approx(0.55)
        assert sig.fair_prob == pytest.approx(0.70)
        assert sig.edge == pytest.approx(0.70 - 0.55)
        # cushion = (50000 - 49980) / 50000 * 1e4 = 4.0 bps
        assert sig.reason.startswith("cushion 4.0bps;")

    def test_v7_passes_through_v2_when_fresh_and_capped(
        self, params: strategy.StrategyParams
    ) -> None:
        """Fresh window (50s in) + modest edge claim (0.06 <= cap) -> v2's trade."""
        view = _view(
            remaining_seconds=250, spot=50000.0, reference=49980.0,
            up_ask=0.55, fair_up=0.61,
        )
        sig = cushion_fresh_v7(view, params)
        assert isinstance(sig, ShadowSignal)
        assert sig.side == "Up"
        assert sig.entry_price == pytest.approx(0.55)
        assert sig.edge == pytest.approx(0.06)
        assert sig.reason.startswith("fresh 50s;")

    def test_v7_none_when_window_stale(self, params: strategy.StrategyParams) -> None:
        """180s into the window (> 60s freshness gate) -> None even though v2 fires."""
        view = _view(
            remaining_seconds=120, spot=50000.0, reference=49980.0,
            up_ask=0.55, fair_up=0.61,
        )
        assert cushion_favorite_v2(view, params) is not None  # v2 would trade
        assert cushion_fresh_v7(view, params) is None

    def test_v7_none_when_edge_claim_above_cap(
        self, params: strategy.StrategyParams
    ) -> None:
        """A 0.07 claimed edge (> 0.065 cap) is distrusted -> None."""
        view = _view(
            remaining_seconds=250, spot=50000.0, reference=49980.0,
            up_ask=0.55, fair_up=0.62,
        )
        assert cushion_favorite_v2(view, params) is not None  # v2 would trade
        assert cushion_fresh_v7(view, params) is None

    def test_v7_edge_exactly_at_cap_passes(
        self, params: strategy.StrategyParams
    ) -> None:
        view = _view(
            remaining_seconds=250, spot=50000.0, reference=49980.0,
            up_ask=0.55, fair_up=0.615,
        )
        sig = cushion_fresh_v7(view, params)
        assert isinstance(sig, ShadowSignal)
        assert sig.edge == pytest.approx(0.065)

    def test_v7_none_when_v2_declines(self, params: strategy.StrategyParams) -> None:
        """Cushion too thin -> v2 declines -> v7 declines (pure wrapper)."""
        view = _view(
            remaining_seconds=250, spot=50000.0, reference=49999.0,
            up_ask=0.55, fair_up=0.61,
        )
        assert cushion_fresh_v7(view, params) is None

    def test_v8_fires_fresh_without_cushion_or_cap(
        self, params: strategy.StrategyParams
    ) -> None:
        """v8 = freshness ONLY: takes a fresh v0 signal v2 would reject for a
        thin cushion, and does not cap the edge claim."""
        view = _view(
            remaining_seconds=250, spot=50000.0, reference=49999.0,  # 0.2bps cushion
            up_ask=0.55, fair_up=0.70,  # edge 0.15 (> v7's cap)
        )
        assert cushion_favorite_v2(view, params) is None  # cushion too thin
        sig = pricing_fresh_v8(view, params)
        assert isinstance(sig, ShadowSignal)
        assert sig.side == "Up"
        assert sig.edge == pytest.approx(0.15)
        assert sig.reason.startswith("fresh 50s;")

    def test_v8_none_when_window_stale(self, params: strategy.StrategyParams) -> None:
        view = _view(
            remaining_seconds=120, spot=50000.0, reference=49980.0,
            up_ask=0.55, fair_up=0.70,
        )
        assert pricing_fresh_v8(view, params) is None

    def test_v8_none_when_v0_declines(self, params: strategy.StrategyParams) -> None:
        """Thin edge -> v0 declines -> v8 declines (freshness adds no trades)."""
        view = _view(remaining_seconds=250, up_ask=0.69, fair_up=0.70)
        assert pricing_fresh_v8(view, params) is None

    def test_signal_when_both_pass_down(self, params: strategy.StrategyParams) -> None:
        """v0 enters Down (spot below reference) and the cushion clears -> Down."""
        # fair_up 0.30 -> fair_down 0.70 vs down_ask 0.55 -> edge_down 0.15.
        view = _view(
            spot=50000.0,
            reference=50020.0,
            up_ask=0.72,
            down_ask=0.55,
            market_up_price=0.30,
            fair_up=0.30,
        )
        sig = cushion_favorite_v2(view, params)
        assert isinstance(sig, ShadowSignal)
        assert sig.side == "Down"
        assert sig.entry_price == pytest.approx(0.55)
        assert sig.fair_prob == pytest.approx(0.70)
        assert sig.edge == pytest.approx(0.70 - 0.55)
        # cushion = (50020 - 50000) / 50000 * 1e4 = 4.0 bps
        assert sig.reason.startswith("cushion 4.0bps;")

    def test_cushion_uses_correct_side_sign(
        self, params: strategy.StrategyParams
    ) -> None:
        """A long spot lead is the WRONG sign for a Down pick -> None.

        v0 enters Down, but spot is far ABOVE reference, so the favourable
        gap (reference - spot) is negative and cannot clear the floor.
        """
        view = _view(
            spot=50050.0,
            reference=50000.0,
            up_ask=0.72,
            down_ask=0.55,
            market_up_price=0.30,
            fair_up=0.30,
        )
        assert cushion_favorite_v2(view, params) is None


# ---------------------------------------------------------------------------
# drift_per_second  (directional twin of sigma_per_second)
# ---------------------------------------------------------------------------


class TestDriftPerSecond:
    def test_insufficient_data_returns_zero(self) -> None:
        """Fewer than 2 valid returns -> no drift estimate -> 0.0."""
        assert strategy.drift_per_second([]) == pytest.approx(0.0)
        assert strategy.drift_per_second([50000.0]) == pytest.approx(0.0)

    def test_flat_prices_zero_drift(self) -> None:
        """Constant prices have zero log-returns -> zero mean drift."""
        assert strategy.drift_per_second([50000.0] * 10) == pytest.approx(0.0)

    def test_monotone_up_positive_drift(self) -> None:
        """A rising series has positive mean log-return."""
        assert strategy.drift_per_second([100.0, 101.0, 102.0, 103.0]) > 0.0

    def test_monotone_down_negative_drift(self) -> None:
        """A falling series has negative mean log-return."""
        assert strategy.drift_per_second([103.0, 102.0, 101.0, 100.0]) < 0.0

    def test_value_is_mean_log_return(self) -> None:
        """Drift equals the arithmetic mean of consecutive 1s log-returns."""
        import math

        closes = [100.0, 102.0, 101.0]
        expected = (math.log(102.0 / 100.0) + math.log(101.0 / 102.0)) / 2
        assert strategy.drift_per_second(closes) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# cushion_fresh_v7_f45  (#149 — replay-only, freshness ≤ 45s)
# ---------------------------------------------------------------------------


def _view_with_bids(**overrides: object) -> SnapshotView:
    """Base view with bid quotes populated (replay context).

    Default bids produce a half-tick spread (ask − bid = 0.005) to avoid
    floating-point boundary issues in the ≤ 0.01 spread guard. Tests that
    want a wider spread pass a lower bid explicitly.
    """
    base: dict[str, object] = dict(
        window_slug="btc-5m-2026-07-08T10:00",
        remaining_seconds=260,   # 40s into window → fresh for ≤45s gate
        spot=50000.0,
        reference=49980.0,       # 4 bps cushion → Up signal
        up_ask=0.55,
        down_ask=0.46,
        market_up_price=0.55,
        fair_up=0.61,            # edge 0.06, inside edge_cap 0.065
        sigma_per_second=0.0003,
        feed_source="replay",
        quote_source="replay",
        up_bid=0.545,            # 0.005 spread on Up — clearly ≤ 1 tick (0.01)
        down_bid=0.455,          # 0.005 spread on Down
    )
    base.update(overrides)
    return SnapshotView(**base)  # type: ignore[arg-type]


class TestCushionFreshV7F45:
    """Pre-registered freshness=45s variant (#149 — H1)."""

    def test_fires_when_40s_into_window(self, params: strategy.StrategyParams) -> None:
        """40s elapsed (≤ 45s gate) → v7_f45 fires."""
        view = _view_with_bids(remaining_seconds=260)
        sig = cushion_fresh_v7_f45(view, params)
        assert isinstance(sig, ShadowSignal)
        assert sig.side == "Up"
        assert "fresh 40s" in sig.reason

    def test_none_when_46s_into_window(self, params: strategy.StrategyParams) -> None:
        """46s elapsed (> 45s gate, still ≤ 60s v7 spec) → None."""
        view = _view_with_bids(remaining_seconds=254)  # 300-254=46
        assert cushion_fresh_v7(view, params) is not None   # original v7 still fires
        assert cushion_fresh_v7_f45(view, params) is None   # tighter gate blocks it

    def test_none_when_180s_into_window(self, params: strategy.StrategyParams) -> None:
        """Well beyond the gate → None."""
        view = _view_with_bids(remaining_seconds=120)
        assert cushion_fresh_v7_f45(view, params) is None

    def test_none_when_cushion_too_thin(self, params: strategy.StrategyParams) -> None:
        """Inherits v2's cushion guard — thin gap is rejected."""
        view = _view_with_bids(remaining_seconds=260, reference=49999.0)
        assert cushion_fresh_v7_f45(view, params) is None

    def test_none_when_edge_above_cap(self, params: strategy.StrategyParams) -> None:
        """Edge 0.07 > 0.065 cap → rejected, same as v7."""
        view = _view_with_bids(remaining_seconds=260, fair_up=0.62)
        assert cushion_fresh_v7_f45(view, params) is None


# ---------------------------------------------------------------------------
# cushion_fresh_v7_f45_spread  (#149 — replay-only, f45 + spread ≤ 1 tick)
# ---------------------------------------------------------------------------


class TestCushionFreshV7F45Spread:
    """Pre-registered spread-guard variant (#149 — H2)."""

    def test_fires_when_spread_tight(
        self, params: strategy.StrategyParams
    ) -> None:
        """Spread 0.005 (half-tick) ≤ max_spread 0.01 → fires."""
        view = _view_with_bids()   # default: up_bid=0.545, spread=0.005
        sig = cushion_fresh_v7_f45_spread(view, params)
        assert isinstance(sig, ShadowSignal)
        assert sig.side == "Up"
        assert "spread" in sig.reason

    def test_fires_when_spread_exactly_one_tick(
        self, params: strategy.StrategyParams
    ) -> None:
        """Spread exactly at the 0.01 boundary → fires (≤ not <)."""
        # Use bid = ask − 0.01 via carefully chosen values to avoid fp error.
        # up_ask=0.55, up_bid=0.54 → 0.55−0.54 = 0.010000...009 in raw float,
        # but the signal rounds to 6dp so it sees 0.01 ≤ 0.01 → passes.
        view = _view_with_bids(up_bid=0.54)
        sig = cushion_fresh_v7_f45_spread(view, params)
        assert isinstance(sig, ShadowSignal)

    def test_none_when_spread_is_two_ticks(
        self, params: strategy.StrategyParams
    ) -> None:
        """2-tick spread (0.02) > max_spread=0.01 → None."""
        view = _view_with_bids(up_bid=0.53)   # spread = 0.55 - 0.53 = 0.02
        assert cushion_fresh_v7_f45(view, params) is not None   # f45 still fires
        assert cushion_fresh_v7_f45_spread(view, params) is None

    def test_none_when_bid_absent(self, params: strategy.StrategyParams) -> None:
        """No bid quote (live/paper/shadow path) → None (safe fallback)."""
        view = _view_with_bids(up_bid=None)
        assert cushion_fresh_v7_f45_spread(view, params) is None

    def test_none_when_stale_window(self, params: strategy.StrategyParams) -> None:
        """Spread guard doesn't rescue a stale window (f45 blocks first)."""
        view = _view_with_bids(remaining_seconds=254, up_bid=0.54)  # 46s elapsed
        assert cushion_fresh_v7_f45_spread(view, params) is None

    def test_none_when_cushion_too_thin(self, params: strategy.StrategyParams) -> None:
        """Inherits v2 cushion guard — thin gap still blocked."""
        view = _view_with_bids(reference=49999.0, up_bid=0.54)
        assert cushion_fresh_v7_f45_spread(view, params) is None

    def test_reason_chain_present(self, params: strategy.StrategyParams) -> None:
        """Reason string chains: spread → fresh → cushion → v0 entry."""
        view = _view_with_bids()
        sig = cushion_fresh_v7_f45_spread(view, params)
        assert sig is not None
        # All three guard layers must be mentioned in order.
        assert sig.reason.index("spread") < sig.reason.index("fresh")
        assert sig.reason.index("fresh") < sig.reason.index("cushion")
