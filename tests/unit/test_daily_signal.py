"""Daily altcoin scanner signal math: sigma/drift scaling, the 50-50 tie
fair-value split, executable-edge scoring, and cross-asset ranking."""

from __future__ import annotations

import math

import pytest

from polymarket_bot import strategy as _strategy
from polymarket_bot.daily.signal import (
    daily_sigma_and_drift_per_second,
    fair_up_probability,
    rank,
    score,
)
from polymarket_bot.daily.types import DailyMarketView, DailySignal


def _closes(n: int = 30, start: float = 100.0, step: float = 0.0) -> list[float]:
    return [start + i * step for i in range(n)]


def test_sigma_per_second_is_daily_sigma_scaled_by_sqrt_time():
    closes = [100.0, 102.0, 99.0, 103.0, 101.0, 104.0, 98.0]
    sigma_s, _drift_s = daily_sigma_and_drift_per_second(closes)
    sigma_day = _strategy.sigma_per_second(closes)  # same stdev-of-log-returns math
    assert sigma_s == pytest.approx(sigma_day / math.sqrt(86400.0))


def test_drift_per_second_is_daily_drift_scaled_linearly():
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    _sigma_s, drift_s = daily_sigma_and_drift_per_second(closes)
    drift_day = _strategy.drift_per_second(closes)
    assert drift_s == pytest.approx(drift_day / 86400.0)


def test_flat_closes_still_floor_sigma():
    """A perfectly flat history must not produce a zero/negative sigma."""
    sigma_s, drift_s = daily_sigma_and_drift_per_second(_closes(30, 100.0, 0.0))
    assert sigma_s > 0
    assert drift_s == pytest.approx(0.0)


def test_tie_is_exactly_fifty_fifty_not_credited_to_up():
    """At spot == reference, this family's fair_up must be EXACTLY 0.5 (tie
    is 50-50), unlike strategy.fair_up_probability's structural Up bias for
    the SAME inputs (that family credits an exact tie entirely to Up)."""
    spot = reference = 100.0
    sigma = 0.0001
    remaining = 3600
    daily_p = fair_up_probability(spot, reference, sigma, remaining)
    btc_p = _strategy.fair_up_probability(spot, reference, sigma, remaining)
    assert daily_p == pytest.approx(0.5, abs=1e-12)
    assert btc_p > 0.5  # the BTC family's documented structural Up bias
    assert daily_p < btc_p


def test_fair_up_probability_rises_with_spot_above_reference():
    sigma = 0.0001
    remaining = 3600
    p_flat = fair_up_probability(100.0, 100.0, sigma, remaining)
    p_up = fair_up_probability(101.0, 100.0, sigma, remaining)
    assert p_up > p_flat


def _view(
    *,
    asset: str = "sol",
    up_ask: float | None = 0.50,
    down_ask: float | None = 0.50,
    fair_up: float = 0.65,
    remaining_seconds: int = 7200,
) -> DailyMarketView:
    return DailyMarketView(
        asset=asset,
        window_slug=f"{asset}-up-or-down-on-august-30-2026",
        condition_id="0xabc",
        up_token="1",
        down_token="2",
        binance_symbol="SOLUSDT",
        resolves_at="2026-08-30T16:00:00Z",
        remaining_seconds=remaining_seconds,
        spot=101.0,
        reference=100.0,
        up_ask=up_ask,
        down_ask=down_ask,
        market_up_price=0.50,
        fair_up=fair_up,
        sigma_per_second=0.0001,
        drift_per_second=0.0,
        liquidity_usd=5000.0,
        order_min_size=5.0,
    )


def _params(**overrides) -> _strategy.StrategyParams:
    base = dict(
        min_trade_usd=10.0,
        max_trade_usd=10.0,
        entry_edge_min=0.045,
        min_confidence=0.0,
        entry_min_remaining_seconds=3600,
    )
    base.update(overrides)
    return _strategy.StrategyParams(**base)


def test_score_enters_the_side_with_qualifying_edge():
    view = _view(up_ask=0.50, down_ask=0.50, fair_up=0.65)  # edge_up = 0.15
    sig = score(view, _params())
    assert sig is not None
    assert sig.asset == "sol"
    assert sig.side == "Up"
    assert sig.entry_price == pytest.approx(0.50)
    assert sig.edge == pytest.approx(0.15)


def test_score_skips_below_threshold():
    view = _view(up_ask=0.63, down_ask=0.40, fair_up=0.65)  # edge_up = 0.02 < 0.045
    assert score(view, _params()) is None


def test_score_skips_too_close_to_resolution():
    view = _view(up_ask=0.50, down_ask=0.50, fair_up=0.65, remaining_seconds=1800)
    assert score(view, _params(entry_min_remaining_seconds=3600)) is None


def test_rank_picks_the_largest_edge_across_assets():
    weak = DailySignal(
        asset="doge", side="Up", entry_price=0.5, fair_prob=0.55, edge=0.05,
        confidence=0.6, reason="enter Up",
    )
    strong = DailySignal(
        asset="eth", side="Down", entry_price=0.3, fair_prob=0.55, edge=0.20,
        confidence=0.9, reason="enter Down",
    )
    assert rank([weak, strong]) is strong
    assert rank([strong, weak]) is strong


def test_rank_of_empty_list_is_none():
    assert rank([]) is None
