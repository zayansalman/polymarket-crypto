"""Regime feature math (polymarket_bot/regime/features.py): pure functions over bars.

Covers the volatility estimators and their per-second scaling, the jump
diagnostics, seasonal same-hour baselines, the move z-score, and the
``None``-on-insufficient-data contract (never a fabricated zero).
"""
from __future__ import annotations

import math
import random
from datetime import UTC, datetime

import pytest

from polymarket_bot import strategy as _strategy
from polymarket_bot.regime import features as F
from polymarket_bot.regime.types import Bar, BookState, VenueMarket

_T0 = 1_700_000_000_000  # ms


def _bar(i: int, secs: int, o: float, h: float, lo: float, c: float, qv: float = 1000.0,
         trades: int = 10, tbq: float = 500.0, t0: int = _T0) -> Bar:
    return Bar(t0 + i * secs * 1000, o, h, lo, c, 1.0, qv, trades, tbq)


def _walk(n: int, secs: int, vol: float, seed: int = 7, base: float = 100.0,
          qv: float = 1000.0, t0: int = _T0) -> list[Bar]:
    """Geometric random walk with a symmetric intra-bar range."""
    rng = random.Random(seed)
    out: list[Bar] = []
    px = base
    for i in range(n):
        o = px
        c = o * math.exp(rng.gauss(0.0, vol))
        h = max(o, c) * (1.0 + abs(rng.gauss(0.0, vol / 2)))
        low = min(o, c) * (1.0 - abs(rng.gauss(0.0, vol / 2)))
        out.append(_bar(i, secs, o, h, low, c, qv=qv, t0=t0))
        px = c
    return out


# --- log returns / close-to-close ---------------------------------------------


def test_log_returns_skips_non_positive_prices() -> None:
    bars = [_bar(0, 60, 1, 1, 1, 100.0), _bar(1, 60, 1, 1, 1, 0.0), _bar(2, 60, 1, 1, 1, 110.0)]
    assert F.log_returns(bars) == []


def test_realized_vol_matches_strategy_estimator_scaled() -> None:
    """Same stdev-of-log-returns math as strategy.sigma_per_second, divided by sqrt(bar s)."""
    bars = _walk(60, 60, 0.001)
    closes = [b.close for b in bars]
    expected = _strategy.sigma_per_second(closes) / math.sqrt(60.0)
    assert F.realized_vol_per_second(bars, 60.0) == pytest.approx(expected)


def test_realized_vol_none_when_too_few_returns() -> None:
    assert F.realized_vol_per_second(_walk(3, 60, 0.001), 60.0) is None
    assert F.realized_vol_per_second([], 60.0) is None
    assert F.realized_vol_per_second(_walk(10, 60, 0.001), 0.0) is None


def test_realized_vol_has_no_floor_unlike_the_loop() -> None:
    """A flat series reads 0.0 here (information), not the loop's 2e-5 floor."""
    flat = [_bar(i, 60, 100.0, 100.0, 100.0, 100.0) for i in range(10)]
    assert F.realized_vol_per_second(flat, 60.0) == 0.0
    assert _strategy.sigma_per_second([100.0] * 10) == pytest.approx(2e-5)


# --- Garman–Klass --------------------------------------------------------------


def test_garman_klass_variance_formula() -> None:
    bar = _bar(0, 60, 100.0, 102.0, 99.0, 101.0)
    hl = math.log(102.0 / 99.0)
    co = math.log(101.0 / 100.0)
    expected = 0.5 * hl * hl - (2 * math.log(2) - 1) * co * co
    assert F.garman_klass_variance(bar) == pytest.approx(expected)


def test_garman_klass_rejects_bad_range() -> None:
    assert F.garman_klass_variance(_bar(0, 60, 100.0, 98.0, 99.0, 101.0)) is None  # high < low
    assert F.garman_klass_variance(_bar(0, 60, 100.0, 102.0, 0.0, 101.0)) is None   # low <= 0


def test_garman_klass_vol_tracks_the_generating_vol() -> None:
    """On a long random walk, GK per-second vol ≈ the generating per-bar vol / sqrt(bar s)."""
    bars = _walk(2000, 60, 0.002, seed=3)
    gk = F.garman_klass_vol_per_second(bars, 60.0)
    cc = F.realized_vol_per_second(bars, 60.0)
    assert gk is not None and cc is not None
    # Both estimate the same diffusion; the range-based one is within ~35% here
    # (our synthetic range is a crude proxy for a true high/low path).
    assert gk == pytest.approx(cc, rel=0.35)


def test_garman_klass_none_when_too_few_bars() -> None:
    assert F.garman_klass_vol_per_second(_walk(2, 60, 0.001), 60.0) is None


def test_garman_klass_clamps_negative_mean_variance_to_zero() -> None:
    """A close-to-open move that dominates the range gives a negative GK term; the
    mean is clamped at zero before the root rather than raising."""
    bars = [_bar(i, 60, 100.0, 100.0001, 99.9999, 100.5) for i in range(5)]
    assert F.garman_klass_vol_per_second(bars, 60.0) == 0.0


# --- jump diagnostics -----------------------------------------------------------


def test_rv_bv_ratio_near_one_for_diffusion_and_high_with_a_jump() -> None:
    smooth = _walk(500, 60, 0.001, seed=11)
    ratio_smooth = F.rv_bv_ratio(smooth)
    assert ratio_smooth is not None and 0.7 < ratio_smooth < 1.4
    # A persistent 3% level shift at the midpoint: ONE discontinuous return
    # (a bump-and-revert would be two adjacent jumps that bipower variation
    # also sees, which is exactly what this diagnostic must not count).
    mid = 250
    jumped = [
        b if i < mid else Bar(b.open_time_ms, b.open * 1.03, b.high * 1.03, b.low * 1.03,
                              b.close * 1.03, b.volume, b.quote_volume, b.trades, b.taker_buy_quote)
        for i, b in enumerate(smooth)
    ]
    ratio_jump = F.rv_bv_ratio(jumped)
    assert ratio_jump is not None and ratio_jump > 1.5


def test_max_return_z_flags_a_single_outlier() -> None:
    bars = _walk(60, 60, 0.001, seed=5)
    assert (F.max_return_z(bars) or 0.0) < 4.0
    b = bars[30]
    bars[30] = Bar(b.open_time_ms, b.open, b.high, b.low, b.close * 1.02,
                   b.volume, b.quote_volume, b.trades, b.taker_buy_quote)
    assert (F.max_return_z(bars) or 0.0) > 4.0


def test_jump_diagnostics_none_on_short_or_flat_input() -> None:
    assert F.rv_bv_ratio(_walk(3, 60, 0.001)) is None
    flat = [_bar(i, 60, 100.0, 100.0, 100.0, 100.0) for i in range(10)]
    assert F.rv_bv_ratio(flat) is None
    assert F.max_return_z(flat) is None


# --- volume / activity -----------------------------------------------------------


def test_volume_sums_and_ratios() -> None:
    bars = [_bar(i, 60, 1, 1, 1, 1, qv=100.0, trades=5, tbq=60.0) for i in range(60)]
    assert F.quote_volume_sum(bars) == 6000.0
    assert F.trades_sum(bars) == 300.0
    assert F.taker_buy_ratio(bars) == pytest.approx(0.6)
    assert F.quote_volume_sum([]) is None
    assert F.taker_buy_ratio([]) is None


def test_hourly_average_normalises_by_covered_span() -> None:
    bars = [_bar(i, 300, 1, 1, 1, 1, qv=10.0) for i in range(144)]  # 12h
    assert F.hourly_average_quote_volume(bars, 300.0) == pytest.approx(1440.0 / 12.0)


# --- move ------------------------------------------------------------------------


def test_window_log_return_uses_first_open_and_last_close() -> None:
    bars = [_bar(0, 60, 100.0, 101, 99, 100.5), _bar(1, 60, 100.5, 102, 100, 102.0)]
    assert F.window_log_return(bars) == pytest.approx(math.log(102.0 / 100.0))
    assert F.window_log_return([]) is None


def test_move_z_is_return_over_sigma_root_time() -> None:
    assert F.move_z(0.01, 1e-4, 3600.0) == pytest.approx(0.01 / (1e-4 * 60.0))
    assert F.move_z(None, 1e-4, 3600.0) is None
    assert F.move_z(0.01, 0.0, 3600.0) is None


def test_range_position_bounds() -> None:
    bars = [_bar(0, 300, 100, 110, 90, 95), _bar(1, 300, 95, 105, 92, 110)]
    assert F.range_position(bars) == pytest.approx(1.0)
    flat = [_bar(0, 300, 100, 100, 100, 100)]
    assert F.range_position(flat) is None


def test_ratio_guards_zero_and_none() -> None:
    assert F.ratio(1.0, 0.0) is None
    assert F.ratio(None, 2.0) is None
    assert F.ratio(3.0, 2.0) == 1.5


# --- seasonal baselines ---------------------------------------------------------


def _hourly(days: int, value_by_hour, t0_hour_utc: int = 0) -> list[Bar]:
    """Hourly bars starting at a UTC midnight, ``days`` days long."""
    start = datetime(2026, 9, 1, t0_hour_utc, tzinfo=UTC)
    out = []
    for i in range(days * 24):
        ts = int(start.timestamp() * 1000) + i * 3600 * 1000
        hour = (t0_hour_utc + i) % 24
        qv = value_by_hour(hour)
        out.append(Bar(ts, 100.0, 100.5, 99.5, 100.0, 1.0, qv, 10, qv / 2))
    return out


def test_same_hour_median_excludes_forming_bar_and_needs_three_points() -> None:
    bars = _hourly(7, lambda h: 1000.0 + h)
    # 7 days → 7 bars at hour 5, the last one of the series is hour 23 (excluded anyway).
    assert F.same_hour_median(bars, 5, lambda b: b.quote_volume) == 1005.0
    assert F.same_hour_median(bars[:48], 5, lambda b: b.quote_volume) is None  # only 2 points


def test_blended_hour_baseline_weights_by_minute_of_hour() -> None:
    bars = _hourly(7, lambda h: 1000.0 if h == 13 else 2000.0)
    at = datetime(2026, 9, 8, 13, 15, tzinfo=UTC)  # 15 min into hour 13
    # w = 0.25 on hour 13 (1000), 0.75 on hour 12 (2000)
    assert F.blended_hour_baseline(bars, at, lambda b: b.quote_volume) == pytest.approx(1750.0)


def test_blended_hour_baseline_falls_back_to_the_available_hour() -> None:
    bars = _hourly(7, lambda h: 500.0)
    # Keep only hour-13 bars so hour 12 has no median.
    only13 = [b for b in bars if datetime.fromtimestamp(b.open_time_ms / 1000, tz=UTC).hour == 13]
    at = datetime(2026, 9, 8, 13, 30, tzinfo=UTC)
    assert F.blended_hour_baseline(only13, at, lambda b: b.quote_volume) == 500.0
    assert F.blended_hour_baseline([], at, lambda b: b.quote_volume) is None


# --- assembly --------------------------------------------------------------------


def test_compute_features_with_everything_absent_is_all_none() -> None:
    at = datetime(2026, 9, 8, 13, 30, tzinfo=UTC)
    feats = F.compute_features(bars_1m=[], bars_5m=[], bars_1h=[], at=at, book=None,
                               venue_current=None, venue_completed=())
    assert all(v is None for v in feats.as_dict().values())


def test_compute_features_populates_from_inputs() -> None:
    at = datetime(2026, 9, 8, 13, 30, tzinfo=UTC)
    b1 = _walk(60, 60, 0.001, seed=1, qv=1000.0)
    b5 = _walk(288, 300, 0.002, seed=2, qv=5000.0)
    bh = _hourly(7, lambda h: 60_000.0)
    book = BookState(overround=0.012, maker_capture=0.008, executable_depth_usd=150.0,
                     ticks_used=12, newest_age_seconds=5, sigma_per_second=4e-5,
                     vol_source="chainlink_ws")
    venue = VenueMarket("btc-updown-5m-1", 1234.0, 300.0)
    completed = [VenueMarket("a", 100.0, 10.0), VenueMarket("b", 300.0, 30.0), VenueMarket("c", 200.0, None)]
    feats = F.compute_features(bars_1m=b1, bars_5m=b5, bars_1h=bh, at=at, book=book,
                               venue_current=venue, venue_completed=completed)
    assert feats.vol_1h_gk is not None and feats.vol_24h_gk is not None
    assert feats.volume_1h_usd == pytest.approx(60_000.0)
    assert feats.volume_ratio_seasonal == pytest.approx(1.0)
    assert feats.vol_1s == 4e-5
    assert feats.vol_variance_ratio == pytest.approx(feats.vol_1h_gk ** 2 / (4e-5) ** 2)
    assert feats.overround == 0.012 and feats.maker_capture == 0.008
    assert feats.book_ticks_used == 12.0 and feats.book_age_seconds == 5.0
    assert feats.venue_liquidity_usd == 300.0
    assert feats.venue_volume_per_window_median_usd == 200.0
    assert feats.venue_liquidity_median_usd == 20.0
    assert feats.venue_windows_used == 3.0
    assert feats.move_z_1h == pytest.approx(F.move_z(feats.return_1h, feats.vol_1h_gk, 3600.0))


def test_annualize_constant_is_root_seconds_per_year() -> None:
    assert F.ANNUALIZE == pytest.approx(math.sqrt(365 * 86400))
