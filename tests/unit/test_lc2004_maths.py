"""lc2004-Kronos BTC 24h maths (ems/lc2004_kronos_btc_24h/maths.py).

Pins the Laplace probability with a strict count above the strike (design spec step 3,
docs/superpowers/specs/2026-09-22-lc2004-kronos-btc-24h-design.md) and the spread of the
paths' final closes against the strike. The forecast is display-only (Zayan (operator),
2026-09-29), so there is no sizing to test. Claude, 2026-09-22; trimmed by Claude, 2026-09-29.
"""
from __future__ import annotations

import math
import random

import pytest

from ems.lc2004_kronos_btc_24h.maths import PATHS, Probability, path_spread, probability_up

# --- probability_up ---------------------------------------------------------------------------


def test_paths_constant_is_thirty() -> None:
    assert PATHS == 30


def test_count_is_strictly_above_the_strike() -> None:
    got = probability_up([100.0, 101.0, 99.0, 100.0], strike=100.0)
    assert got == Probability(k=1, n=4, p_raw=0.25, q_up=2 / 6,
                              sampling_se=math.sqrt(0.25 * 0.75 / 4))


def test_laplace_keeps_the_extremes_off_certainty() -> None:
    none_up = probability_up([99.0] * PATHS, strike=100.0)
    assert (none_up.k, none_up.q_up, none_up.p_raw, none_up.sampling_se) == (0, 1 / 32, 0.0, 0.0)
    all_up = probability_up([101.0] * PATHS, strike=100.0)
    assert (all_up.k, all_up.q_up, all_up.p_raw, all_up.sampling_se) == (30, 31 / 32, 1.0, 0.0)


def test_eighteen_of_thirty() -> None:
    closes = [110.0] * 18 + [90.0] * 12
    got = probability_up(closes, strike=100.0)
    assert got.k == 18 and got.n == 30
    assert got.q_up == 19 / 32
    assert got.p_raw == 0.6
    assert got.sampling_se == pytest.approx(math.sqrt(0.6 * 0.4 / 30))


def test_every_count_maps_to_k_plus_one_over_thirty_two() -> None:
    for k in range(PATHS + 1):
        closes = [101.0] * k + [100.0] * (PATHS - k)  # a close AT the strike is not Up
        got = probability_up(closes, strike=100.0)
        assert (got.k, got.n) == (k, PATHS)
        assert got.q_up == pytest.approx((k + 1) / 32)
        assert got.p_raw == pytest.approx(k / 30)
        assert got.sampling_se == pytest.approx(math.sqrt((k / 30) * (1 - k / 30) / 30))


def test_order_of_the_paths_does_not_matter() -> None:
    closes = [100.0 + i for i in range(-15, 15)]
    shuffled = closes[:]
    random.Random(4).shuffle(shuffled)
    assert probability_up(closes, 100.0) == probability_up(shuffled, 100.0)


@pytest.mark.parametrize(
    ("closes", "strike"),
    [([], 100.0), ([100.0, float("nan")], 100.0), ([100.0], float("inf"))],
)
def test_probability_rejects_bad_input(closes: list[float], strike: float) -> None:
    with pytest.raises(ValueError):
        probability_up(closes, strike)


# --- path_spread ------------------------------------------------------------------------------


def test_spread_of_evenly_spaced_paths() -> None:
    # 30 closes 100_000, 100_010, ..., 100_290: position q*(n-1) = 2.9, 14.5 and 26.1.
    closes = [100_000.0 + 10 * i for i in range(PATHS)]
    got = path_spread(closes, strike=100_100.0)
    assert set(got) == {"p10", "p50", "p90",
                        "p10_minus_strike", "p50_minus_strike", "p90_minus_strike"}
    assert got["p10"] == pytest.approx(100_029.0)
    assert got["p50"] == pytest.approx(100_145.0)
    assert got["p90"] == pytest.approx(100_261.0)
    assert got["p10_minus_strike"] == pytest.approx(-71.0)
    assert got["p50_minus_strike"] == pytest.approx(45.0)
    assert got["p90_minus_strike"] == pytest.approx(161.0)


def test_spread_ignores_the_order_of_the_paths() -> None:
    closes = [100_000.0 + 10 * i for i in range(PATHS)]
    shuffled = closes[:]
    random.Random(9).shuffle(shuffled)
    assert path_spread(shuffled, 100_100.0) == path_spread(closes, 100_100.0)


def test_spread_matches_the_standard_linear_percentile() -> None:
    # The textbook linear definition on an odd-sized, uneven sample.
    closes = [3.0, 1.0, 10.0, 7.0, 4.0]  # sorted: 1, 3, 4, 7, 10
    got = path_spread(closes, strike=5.0)
    assert got["p10"] == pytest.approx(1.0 + 0.4 * (3.0 - 1.0))   # position 0.4
    assert got["p50"] == pytest.approx(4.0)                      # position 2
    assert got["p90"] == pytest.approx(7.0 + 0.6 * (10.0 - 7.0))  # position 3.6


def test_spread_of_one_path_is_that_path() -> None:
    got = path_spread([101.5], strike=100.0)
    assert (got["p10"], got["p50"], got["p90"]) == (101.5, 101.5, 101.5)
    assert got["p50_minus_strike"] == pytest.approx(1.5)


def test_percentiles_are_in_order_and_inside_the_paths() -> None:
    rng = random.Random(11)
    for _ in range(200):
        closes = [rng.gauss(100_000.0, 1_500.0) for _ in range(PATHS)]
        got = path_spread(closes, strike=100_000.0)
        assert min(closes) <= got["p10"] <= got["p50"] <= got["p90"] <= max(closes)
        for pct in (10, 50, 90):
            assert got[f"p{pct}_minus_strike"] == pytest.approx(got[f"p{pct}"] - 100_000.0)


@pytest.mark.parametrize(
    ("closes", "strike"),
    [([], 100.0), ([100.0, float("inf")], 100.0), ([100.0], float("nan"))],
)
def test_spread_rejects_bad_input(closes: list[float], strike: float) -> None:
    with pytest.raises(ValueError):
        path_spread(closes, strike)
