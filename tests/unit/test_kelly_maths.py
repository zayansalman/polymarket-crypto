"""Kelly horse-race maths (``ems/kelly_horse_race/maths.py``): P(Up), the die, the size."""

from __future__ import annotations

import math
import random
from collections import Counter
from statistics import NormalDist

import pytest

from ems.kelly_horse_race import maths

K = 100_000.0


def closed_form(x: float, k: float, r60: float, sigma_h: float, tau: float) -> float:
    return NormalDist().cdf((math.log(x / k) + r60 * tau) / (sigma_h * math.sqrt(tau)))


@pytest.mark.parametrize("x, r60, sigma_h, tau, expected", [
    (K, 0.0, 0.004, 0.25, 0.5),
    (K, 0.002, 0.004, 0.25, 0.5987063256829237),
    (K, -0.002, 0.004, 0.25, 0.4012936743170763),
    (K, 0.003366484934291658, 0.004, 0.25, 0.6630533107760167),  # 80%/h -> 66%/15m
    (K, 0.003366484934291658, 0.004, 1.0, 0.8),
    (100_050.0, 0.002, 0.004, 10 / 60, 0.6950561771819139),
    (99_950.0, 0.002, 0.004, 10 / 60, 0.459323313682623),
    (100_010.0, -0.003, 0.004, 1 / 60, 0.5385633052150096),
])
def test_p_up_matches_the_closed_form(x, r60, sigma_h, tau, expected) -> None:
    chance = maths.chance_of_up(x, K, r60, sigma_h, tau)
    assert chance.p_up == pytest.approx(expected, abs=1e-12)
    assert chance.p_up == pytest.approx(closed_form(x, K, r60, sigma_h, tau), abs=1e-12)


def test_at_the_open_it_is_phi_of_half_r60_over_sigma() -> None:
    r60, sigma_h = 0.003, 0.005
    assert maths.chance_of_up(K, K, r60, sigma_h, 0.25).p_up == pytest.approx(
        NormalDist().cdf(0.5 * r60 / sigma_h))


@pytest.mark.parametrize("x, r60, expected", [
    (100_010.0, 0.0, 1.0), (99_990.0, 0.0, 0.0), (K, 0.0, 0.5), (K, 0.001, 1.0),
    (K, -0.001, 0.0),
])
def test_with_no_volatility_it_is_the_sign_of_the_move(x, r60, expected) -> None:
    chance = maths.chance_of_up(x, K, r60, 0.0, 0.1)
    assert chance.p_up == expected and chance.z is None


def test_with_no_time_left_it_is_the_sign_of_the_price() -> None:
    assert maths.chance_of_up(100_001.0, K, -0.5, 0.01, 0.0).p_up == 1.0
    assert maths.chance_of_up(K, K, 0.5, 0.01, 0.0).p_up == 0.5


def test_more_volatility_pulls_toward_one_half() -> None:
    ps = [maths.chance_of_up(K, K, 0.002, s, 0.25).p_up for s in (0.004, 0.008, 0.016, 0.1)]
    assert ps == sorted(ps, reverse=True) and all(p > 0.5 for p in ps)
    qs = [maths.chance_of_up(K, K, -0.002, s, 0.25).p_up for s in (0.004, 0.008, 0.016)]
    assert qs == sorted(qs) and all(q < 0.5 for q in qs)


@pytest.mark.parametrize("args", [
    (0.0, K, 0.0, 0.1, 0.1), (K, -1.0, 0.0, 0.1, 0.1), (K, K, 0.0, -0.1, 0.1),
    (K, K, 0.0, 0.1, -0.1), (K, K, float("nan"), 0.1, 0.1),
])
def test_bad_inputs_are_refused(args) -> None:
    with pytest.raises(ValueError):
        maths.chance_of_up(*args)


def test_hour_moves_from_sixty_returns() -> None:
    steps = [0.002 if i % 2 == 0 else -0.001 for i in range(60)]
    r60, sigma_h = maths.hour_moves(steps)
    assert r60 == pytest.approx(0.03) and sigma_h == pytest.approx(0.012247448713915983)
    with pytest.raises(ValueError):
        maths.hour_moves(steps[:59])


def test_the_die_follows_p_up() -> None:
    assert maths.pick_side(0.7, 0.69) == "Up" and maths.pick_side(0.7, 0.7) == "Down"
    assert maths.pick_side(1.0, 0.999999) == "Up" and maths.pick_side(0.0, 0.0) == "Down"
    for bad in (1.0, -0.1):
        with pytest.raises(ValueError):
            maths.pick_side(0.5, bad)


def test_dice_frequencies_with_a_seeded_generator() -> None:
    rng = random.Random(20260926)
    n, p = 40_000, 0.66
    ups = sum(maths.pick_side(p, rng.random()) == "Up" for _ in range(n))
    assert abs(ups / n - p) < 4 * math.sqrt(p * (1 - p) / n)


@pytest.mark.parametrize("min_size, price, cap, low, high", [
    (5, 0.53, 5, 5.0, 9.43), (5, 0.50, 5, 5.0, 10.0), (5, 0.37, 5, 5.0, 13.51),
    (5, 0.99, 5, 5.0, 5.05), (5, 0.01, 5, 5.0, 500.0),
])
def test_share_bounds(min_size, price, cap, low, high) -> None:
    first = maths.draw_size(price, min_size, cap, 0.0)
    last = maths.draw_size(price, min_size, cap, 0.999999999)
    assert (first.shares, last.shares) == (low, high)
    assert first.min_shares == low and first.max_shares == high
    assert last.notional_usd <= cap + 1e-9


@pytest.mark.parametrize("min_size, price, cap", [(5, 0.53, 2), (15, 0.50, 5)])
def test_no_order_when_the_minimum_costs_more_than_the_cap(min_size, price, cap) -> None:
    assert maths.draw_size(price, min_size, cap, 0.5) is None


def test_the_size_is_uniform_over_every_hundredth_of_a_share() -> None:
    counts = Counter(maths.draw_size(0.53, 5, 5, (i + 0.5) / 44_400).shares
                     for i in range(44_400))
    assert len(counts) == 444 and set(counts.values()) == {100}
    assert all(abs(s * 100 - round(s * 100)) < 1e-6 for s in counts)
