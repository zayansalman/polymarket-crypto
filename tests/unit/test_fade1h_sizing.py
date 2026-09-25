"""Sizing maths for Fade 1h Momentum on 15m (polymarket_bot/fade_1h_momentum_15m/sizing.py).

Reproduces every number in the check table of tasks/2026-09-22-fade-1h-sizing-hedging.md and
pins the properties the spec relies on: the market anchor's limits, Kelly for one bet and for
several bets that move together, fractional Kelly, the bid ladder and the resting hedge.
"""

from __future__ import annotations

import math
import random
from statistics import NormalDist

import pytest

from polymarket_bot.fade_1h_momentum_15m import sizing as s

N = NormalDist()


def _argmax(fn, lo: float, hi: float, iters: int = 200) -> float:
    """Golden-section search for the maximum of a concave function on [lo, hi]."""
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    a, b = lo, hi
    c, d = b - ratio * (b - a), a + ratio * (b - a)
    fc, fd = fn(c), fn(d)
    for _ in range(iters):
        if fc < fd:
            a, c, fc = c, d, fd
            d = a + ratio * (b - a)
            fd = fn(d)
        else:
            b, d, fd = d, c, fc
            c = b - ratio * (b - a)
            fc = fn(c)
    return (a + b) / 2.0


def _single_growth(f: float, q: float, b: float) -> float:
    """Section 2's G(f) for one resting bid at b that wins with chance q."""
    return q * math.log1p(f * (1.0 - b) / b) + (1.0 - q) * math.log1p(-f)


# ---------------------------------------------------------------------------------------------
# Section 1: the market anchor
# ---------------------------------------------------------------------------------------------


class TestAnchor:
    @pytest.mark.parametrize("m", [0.03, 0.37, 0.5, 0.81, 0.999])
    @pytest.mark.parametrize("p_model", [0.0, 0.2, 0.9, 1.0])
    def test_no_model_weight_follows_the_market(self, m: float, p_model: float) -> None:
        # w_S = 0: the maths is the market price, whatever the model says.
        assert s.anchor(p_model, m, 1.0, 0.0) == pytest.approx(m, abs=1e-12)

    def test_no_market_weight_is_the_model(self) -> None:
        assert s.anchor(0.73, 0.4, 0.0, 1.0) == pytest.approx(0.73, abs=1e-12)

    def test_probit_combination(self) -> None:
        p_model, m, w_m, w_s = 0.62, 0.55, 0.7, 0.4
        expected = N.cdf(w_m * N.inv_cdf(m) + w_s * N.inv_cdf(p_model))
        assert s.anchor(p_model, m, w_m, w_s) == pytest.approx(expected, abs=1e-15)

    def test_up_and_down_are_mirror_images(self) -> None:
        up = s.anchor(0.62, 0.55, 0.7, 0.4)
        down = s.anchor(0.38, 0.45, 0.7, 0.4)
        assert up + down == pytest.approx(1.0, abs=1e-12)

    def test_more_model_weight_moves_toward_the_model(self) -> None:
        ps = [s.anchor(0.7, 0.5, 1.0, w) for w in (0.0, 0.25, 0.5, 1.0)]
        assert ps == sorted(ps)
        assert ps[0] == pytest.approx(0.5) and ps[-1] == pytest.approx(0.7)

    @pytest.mark.parametrize("m", [1.2, -0.1, float("nan")])
    def test_rejects_a_mid_that_is_not_a_probability(self, m: float) -> None:
        with pytest.raises(ValueError):
            s.anchor(0.5, m, 1.0, 0.5)


# ---------------------------------------------------------------------------------------------
# Section 2: one bet
# ---------------------------------------------------------------------------------------------


class TestSingleKelly:
    def test_check_table_single_kelly_0_1200(self) -> None:
        assert s.kelly_maker(0.56, 0.50) == pytest.approx(0.1200, abs=1e-12)

    def test_uncertain_q_does_not_change_the_stake(self) -> None:
        # Check table: q ~ N(0.56, 0.05^2), b = 0.50. G is linear in q, so averaging G over
        # q's spread is G at the mean: the best stake stays 0.1200 exactly (the spec's
        # 0.1198 was Monte Carlo noise).
        zs, ps = s.normal_quadrature(20)
        qs = [0.56 + 0.05 * z for z in zs]
        assert min(qs) > 0.0 and max(qs) < 1.0

        def mixed(f: float) -> float:
            return math.fsum(p * _single_growth(f, q, 0.50) for p, q in zip(ps, qs))

        assert _argmax(mixed, 0.0, 0.9) == pytest.approx(0.1200, abs=1e-8)
        assert s.joint_kelly([(0.56, 0.50)], 0.0, 1.0)[0] == pytest.approx(0.1200, abs=1e-9)

    @pytest.mark.parametrize("q,b", [(0.5, 0.5), (0.4, 0.5), (0.0, 0.3)])
    def test_no_edge_stakes_nothing(self, q: float, b: float) -> None:
        assert s.kelly_maker(q, b) == 0.0

    def test_taker_formula_includes_the_fee(self) -> None:
        q, a = 0.62, 0.55
        c = 0.07 * a * (1 - a)
        assert s.kelly_taker(q, a) == pytest.approx((q - a - c) / (1 - a - c), abs=1e-15)
        # ...and it is the log-optimal stake for a bet that costs a + c per share.
        assert s.joint_kelly([(q, a + c)], 0.0, 1.0)[0] == pytest.approx(
            s.kelly_taker(q, a), abs=1e-9
        )

    def test_taker_fee_can_remove_the_edge(self) -> None:
        assert s.kelly_maker(0.56, 0.55) > 0.0
        assert s.kelly_taker(0.56, 0.55) == 0.0

    @pytest.mark.parametrize("q,b", [(0.56, 0.5), (0.8, 0.65), (0.3, 0.12), (0.97, 0.9)])
    def test_solver_matches_closed_form(self, q: float, b: float) -> None:
        assert s.joint_kelly([(q, b)], 0.4, 1.0)[0] == pytest.approx(
            s.kelly_maker(q, b), abs=1e-9
        )

    def test_rejects_bad_prices(self) -> None:
        for b in (0.0, 1.0, 1.5):
            with pytest.raises(ValueError):
                s.kelly_maker(0.5, b)


# ---------------------------------------------------------------------------------------------
# Section 2: the risk dial (fractional Kelly)
# ---------------------------------------------------------------------------------------------


class TestFractionalKelly:
    def test_half_kelly_keeps_three_quarters_of_the_growth(self) -> None:
        full = _single_growth(0.12, 0.56, 0.5)
        half = s.joint_log_growth([(0.56, 0.5)], 0.0, [0.06])
        quarter = s.joint_log_growth([(0.56, 0.5)], 0.0, [0.03])
        assert half / full == pytest.approx(0.749, abs=5e-4)  # spec: 75%
        assert round(half / full, 2) == 0.75
        assert quarter / full == pytest.approx(0.437, abs=5e-4)  # spec: 44%
        assert round(quarter / full, 2) == 0.44

    def test_half_kelly_halves_the_swing(self) -> None:
        def swing(f: float) -> float:  # spread of log wealth for one bet at q = 0.56, b = 0.5
            return math.sqrt(0.56 * 0.44) * (math.log1p(f) - math.log1p(-f))

        assert swing(0.06) / swing(0.12) == pytest.approx(0.5, abs=0.005)

    def test_multiplier_scales_every_stake(self) -> None:
        bets = [(0.56, 0.5), (0.58, 0.52), (0.6, 0.55)]
        full = s.joint_kelly(bets, 0.6, 1.0)
        half = s.joint_kelly(bets, 0.6, 0.5)
        assert half == pytest.approx([0.5 * f for f in full], abs=1e-15)
        assert s.joint_kelly(bets, 0.6, 0.0) == [0.0, 0.0, 0.0]
        growth_full = s.joint_log_growth(bets, 0.6, full)
        growth_half = s.joint_log_growth(bets, 0.6, half)
        assert growth_half / growth_full == pytest.approx(0.75, abs=0.01)

    @pytest.mark.parametrize("k", [-0.1, 1.5, float("nan")])
    def test_rejects_a_multiplier_outside_0_to_1(self, k: float) -> None:
        with pytest.raises(ValueError):
            s.joint_kelly([(0.56, 0.5)], 0.6, k)
        with pytest.raises(ValueError):
            s.ladder([(0.5, 0.5, 0.56)], 100.0, k)


# ---------------------------------------------------------------------------------------------
# Section 2: four coins at once
# ---------------------------------------------------------------------------------------------


class TestJointKelly:
    FOUR = [(0.56, 0.50)] * 4

    def test_check_table_alone_0_48(self) -> None:
        assert 4 * s.kelly_maker(0.56, 0.50) == pytest.approx(0.48, abs=1e-12)

    def test_check_table_jointly_at_rho_0_6(self) -> None:
        stakes = s.joint_kelly(self.FOUR, 0.6, 1.0)
        # Four identical bets get identical stakes, each 0.05-0.06 as the spec says.
        assert max(stakes) - min(stakes) < 1e-12
        assert all(0.05 <= f <= 0.06 for f in stakes)
        # The quadrature's exact figure is 0.2135 in total (0.0534 each). The spec's "0.22"
        # came from a Monte Carlo check (it scatters 0.048-0.061 per coin at 20k samples).
        assert sum(stakes) == pytest.approx(0.21348, abs=1e-4)
        assert sum(stakes) == pytest.approx(0.22, abs=0.01)
        # Sizing each coin alone would overbet by more than double.
        assert 4 * s.kelly_maker(0.56, 0.50) / sum(stakes) > 2.0

    @staticmethod
    def _assert_optimal(bets: list[tuple[float, float]], rho: float, stakes: list[float]) -> None:
        """First-order conditions of a concave maximum: a staked bet's slope is 0, an unstaked
        bet's slope does not point up."""
        h = 1e-6
        for i, f in enumerate(stakes):
            up = list(stakes)
            up[i] += h
            if f > 0.0:
                down = list(stakes)
                down[i] -= h
                slope = s.joint_log_growth(bets, rho, up) - s.joint_log_growth(bets, rho, down)
                assert slope / (2 * h) == pytest.approx(0.0, abs=1e-7)
            else:
                slope = s.joint_log_growth(bets, rho, up) - s.joint_log_growth(bets, rho, stakes)
                assert slope / h < 0.0

    def test_joint_stakes_satisfy_first_order_conditions(self) -> None:
        bets = [(0.56, 0.50), (0.58, 0.52), (0.55, 0.49), (0.57, 0.51)]
        stakes = s.joint_kelly(bets, 0.6, 1.0)
        assert all(f > 0.0 for f in stakes)
        self._assert_optimal(bets, 0.6, stakes)

    def test_a_thin_edge_that_moves_with_stronger_bets_gets_zero(self) -> None:
        # 0.58 at 55c has an edge on its own, but at rho 0.6 the three stronger bets already
        # carry the shared move; adding it lowers growth, so the maths stakes exactly 0.
        bets = [(0.56, 0.50), (0.60, 0.52), (0.53, 0.47), (0.58, 0.55)]
        assert s.kelly_maker(0.58, 0.55) > 0.0
        stakes = s.joint_kelly(bets, 0.6, 1.0)
        assert stakes[3] == 0.0
        assert all(f > 0.0 for f in stakes[:3])
        self._assert_optimal(bets, 0.6, stakes)

    def test_simultaneous_independent_bets_stake_less_than_one_at_a_time(self) -> None:
        total = sum(s.joint_kelly(self.FOUR, 0.0, 1.0))
        assert total == pytest.approx(0.459, abs=1e-3)
        assert total < 0.48

    def test_perfectly_together_is_one_bet(self) -> None:
        assert sum(s.joint_kelly(self.FOUR, 1.0, 1.0)) == pytest.approx(0.12, abs=1e-8)

    def test_more_correlation_means_less_in_total(self) -> None:
        totals = [sum(s.joint_kelly(self.FOUR, rho, 1.0)) for rho in (0.0, 0.3, 0.6, 0.9, 1.0)]
        assert totals == sorted(totals, reverse=True)

    def test_negative_rho_counts_as_independent(self) -> None:
        assert s.joint_kelly(self.FOUR, -0.3, 1.0) == s.joint_kelly(self.FOUR, 0.0, 1.0)

    def test_bet_without_edge_gets_exactly_zero(self) -> None:
        stakes = s.joint_kelly([(0.56, 0.50), (0.45, 0.50), (0.50, 0.50)], 0.6, 1.0)
        assert stakes[0] > 0.0
        assert stakes[1] == 0.0 and stakes[2] == 0.0
        assert stakes[0] == pytest.approx(0.12, abs=1e-9)

    def test_stakes_follow_the_order_given(self) -> None:
        bets = [(0.55, 0.5), (0.60, 0.5), (0.57, 0.5)]
        stakes = s.joint_kelly(bets, 0.3, 1.0)
        assert stakes[1] > stakes[2] > stakes[0] > 0.0
        flipped = s.joint_kelly(list(reversed(bets)), 0.3, 1.0)
        assert flipped == pytest.approx(list(reversed(stakes)), abs=1e-12)

    def test_total_stays_below_the_bankroll(self) -> None:
        stakes = s.joint_kelly([(0.99, 0.5)] * 4, 0.3, 1.0)
        assert sum(stakes) < 1.0
        assert all(f >= 0.0 for f in stakes)

    def test_accepts_bet_tuples_and_named_bets(self) -> None:
        named = s.joint_kelly([s.Bet(0.56, 0.5), s.Bet(0.58, 0.5, 1.0)], 0.6, 1.0)
        plain = s.joint_kelly([(0.56, 0.5), (0.58, 0.5, 1.0)], 0.6, 1.0)
        assert named == plain

    def test_empty_and_too_many(self) -> None:
        assert s.joint_kelly([], 0.6, 0.5) == []
        with pytest.raises(ValueError):
            s.joint_kelly([(0.56, 0.5)] * (s.MAX_JOINT_BETS + 1), 0.6, 0.5)

    def test_growth_rejects_stakes_at_or_above_the_bankroll(self) -> None:
        with pytest.raises(ValueError):
            s.joint_log_growth([(0.56, 0.5)] * 2, 0.6, [0.5, 0.5])
        with pytest.raises(ValueError):
            s.joint_log_growth([(0.56, 0.5)], 0.6, [-0.1])


# ---------------------------------------------------------------------------------------------
# Quadrature and the copula table
# ---------------------------------------------------------------------------------------------


class TestQuadrature:
    def test_known_small_rules(self) -> None:
        rp = math.sqrt(math.pi)
        x, w = s.gauss_hermite(1)
        assert x == pytest.approx((0.0,)) and w == pytest.approx((rp,))
        x, w = s.gauss_hermite(2)
        assert x == pytest.approx((-1 / math.sqrt(2), 1 / math.sqrt(2)), abs=1e-14)
        assert w == pytest.approx((rp / 2, rp / 2), abs=1e-14)
        x, w = s.gauss_hermite(3)
        r = math.sqrt(1.5)
        assert x == pytest.approx((-r, 0.0, r), abs=1e-14)
        assert w == pytest.approx((rp / 6, 2 * rp / 3, rp / 6), abs=1e-14)

    @pytest.mark.parametrize("n", [5, 20, 64, 128])
    def test_normal_moments_are_exact(self, n: int) -> None:
        zs, ps = s.normal_quadrature(n)
        assert list(zs) == sorted(zs)
        assert math.fsum(ps) == pytest.approx(1.0, abs=1e-13)
        for power, moment in ((1, 0.0), (2, 1.0), (3, 0.0), (4, 3.0), (6, 15.0)):
            if power <= 2 * n - 1:
                assert math.fsum(p * z**power for z, p in zip(zs, ps)) == pytest.approx(
                    moment, abs=1e-10
                )

    def test_rejects_a_bad_node_count(self) -> None:
        for n in (0, -3, 2.5, True):
            with pytest.raises(ValueError):
                s.gauss_hermite(n)

    @pytest.mark.parametrize("rho", [0.3, 0.6, 0.9])
    def test_matches_sheppards_formula(self, rho: float) -> None:
        # Two bets at q = 1/2: P(both win) = 1/4 + asin(rho) / (2 pi), exactly.
        table = s.outcome_probabilities([0.5, 0.5], rho)
        exact = 0.25 + math.asin(rho) / (2 * math.pi)
        assert table[(True, True)] == pytest.approx(exact, abs=1e-7)
        assert table[(False, False)] == pytest.approx(exact, abs=1e-7)

    def test_table_keeps_each_bets_own_chance(self) -> None:
        qs = [0.56, 0.52, 0.60, 0.45]
        table = s.outcome_probabilities(qs, 0.6)
        assert len(table) == 16
        assert math.fsum(table.values()) == pytest.approx(1.0, abs=1e-12)
        for i, q in enumerate(qs):
            assert math.fsum(p for k, p in table.items() if k[i]) == pytest.approx(q, abs=1e-10)

    def test_independent_table_is_a_product(self) -> None:
        table = s.outcome_probabilities([0.3, 0.8], 0.0)
        assert table[(True, False)] == pytest.approx(0.3 * 0.2, abs=1e-15)

    def test_perfectly_together_table(self) -> None:
        table = s.outcome_probabilities([0.3, 0.6], 1.0)
        assert table[(True, True)] == pytest.approx(0.3)
        assert table[(False, True)] == pytest.approx(0.3)
        assert table[(False, False)] == pytest.approx(0.4)
        assert (True, False) not in table

    def test_four_coins_settle_the_same_way_more_often_when_correlated(self) -> None:
        def same(rho: float) -> float:
            t = s.outcome_probabilities([0.5] * 4, rho)
            return t[(True,) * 4] + t[(False,) * 4]

        assert same(0.0) == pytest.approx(0.125, abs=1e-15)
        assert same(0.3) < same(0.6) < same(0.9)


# ---------------------------------------------------------------------------------------------
# Section 3: the ladder
# ---------------------------------------------------------------------------------------------


class TestLadder:
    RUNGS = [(0.50, 0.60, 0.58), (0.48, 0.40, 0.47), (0.45, 0.20, 0.52)]

    def test_single_rung_is_plain_kelly_whatever_its_fill_chance(self) -> None:
        for p_fill in (1.0, 0.3, 0.01):
            assert s.ladder([(0.5, p_fill, 0.56)], 100.0, 1.0) == pytest.approx(
                [12.0], abs=1e-7
            )

    def test_rung_without_edge_gets_zero(self) -> None:
        stakes = s.ladder(self.RUNGS, 100.0, 1.0)
        assert stakes[1] == 0.0  # q 0.47 does not beat the 48c bid
        assert stakes[0] > 0.0

    def test_shallow_rung_without_edge_leaves_room_for_a_deeper_one(self) -> None:
        stakes = s.ladder([(0.50, 0.6, 0.49), (0.45, 0.3, 0.55)], 100.0, 1.0)
        assert stakes[0] == 0.0
        assert stakes[1] > 0.0

    def test_no_edge_anywhere_stakes_nothing(self) -> None:
        rungs = [(0.50, 0.6, 0.50), (0.48, 0.4, 0.46), (0.45, 0.2, 0.44)]
        assert s.ladder(rungs, 100.0, 1.0) == [0.0, 0.0, 0.0]

    def test_ladder_optimum_satisfies_first_order_conditions(self) -> None:
        rungs = [(0.52, 0.7, 0.57), (0.50, 0.5, 0.56), (0.47, 0.3, 0.55), (0.44, 0.1, 0.50)]
        stakes = s.ladder(rungs, 100.0, 1.0)
        assert sum(1 for x in stakes if x > 0.0) >= 2
        h = 1e-4
        for i, x in enumerate(stakes):
            up = list(stakes)
            up[i] += h
            if x > 0.0:
                down = list(stakes)
                down[i] -= h
                slope = s.ladder_log_growth(rungs, 100.0, up) - s.ladder_log_growth(
                    rungs, 100.0, down
                )
                assert slope / (2 * h) == pytest.approx(0.0, abs=1e-8)
            else:
                slope = s.ladder_log_growth(rungs, 100.0, up) - s.ladder_log_growth(
                    rungs, 100.0, stakes
                )
                assert slope / h <= 1e-8

    def test_beats_random_nearby_stakes(self) -> None:
        stakes = s.ladder(self.RUNGS, 100.0, 1.0)
        best = s.ladder_log_growth(self.RUNGS, 100.0, stakes)
        rng = random.Random(7)
        for _ in range(200):
            other = [max(0.0, x + rng.uniform(-3.0, 3.0)) for x in stakes]
            assert s.ladder_log_growth(self.RUNGS, 100.0, other) <= best + 1e-15

    def test_multiplier_and_bankroll_scale_the_stakes(self) -> None:
        full = s.ladder(self.RUNGS, 100.0, 1.0)
        assert s.ladder(self.RUNGS, 100.0, 0.5) == pytest.approx([x / 2 for x in full])
        assert s.ladder(self.RUNGS, 250.0, 1.0) == pytest.approx([x * 2.5 for x in full])

    def test_input_order_does_not_matter(self) -> None:
        rungs = [(0.52, 0.7, 0.57), (0.50, 0.5, 0.56), (0.47, 0.3, 0.55)]
        stakes = s.ladder(rungs, 100.0, 1.0)
        shuffled = [rungs[2], rungs[0], rungs[1]]
        again = s.ladder(shuffled, 100.0, 1.0)
        assert again == pytest.approx([stakes[2], stakes[0], stakes[1]], abs=1e-9)

    def test_total_stays_below_the_bankroll(self) -> None:
        rungs = [(0.30, 1.0, 0.99), (0.20, 0.9, 0.99)]
        stakes = s.ladder(rungs, 100.0, 1.0)
        assert 0.0 < sum(stakes) < 100.0

    def test_no_bankroll_no_stakes(self) -> None:
        assert s.ladder(self.RUNGS, 0.0, 0.5) == [0.0, 0.0, 0.0]
        assert s.ladder(self.RUNGS, -5.0, 0.5) == [0.0, 0.0, 0.0]
        assert s.ladder([], 100.0, 0.5) == []

    def test_deeper_rung_filling_more_often_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="deeper rung"):
            s.ladder([(0.50, 0.3, 0.56), (0.45, 0.5, 0.56)], 100.0, 0.5)

    def test_inconsistent_win_chances_are_rejected(self) -> None:
        # Both rungs fill together (P 0.5 each), so they must share one win chance.
        with pytest.raises(ValueError, match="inconsistent"):
            s.ladder([(0.50, 0.5, 0.40), (0.45, 0.5, 0.60)], 100.0, 0.5)


# ---------------------------------------------------------------------------------------------
# Section 4: the hedge
# ---------------------------------------------------------------------------------------------


def _hedge_growth(h: float, p: float, b: float, W: float, n: float) -> float:
    """Section 4's E ln W with h hedge shares, minus its value with no hedge (a constant), in
    log1p form so the numerical maximiser is not limited by rounding in ln(W + n)."""
    return p * math.log1p(-h * b / (W + n)) + (1 - p) * math.log1p(h * (1 - b) / W)


class TestHedge:
    @pytest.mark.parametrize(
        "n,W,p,b,expected",
        [
            (20, 100, 0.30, 0.65, 43.52),
            (20, 100, 0.45, 0.52, 33.17),
            (20, 100, 0.60, 0.38, 29.54),
            (40, 50, 0.20, 0.75, 56.00),
        ],
    )
    def test_check_table(self, n: float, W: float, p: float, b: float, expected: float) -> None:
        h = s.hedge_shares(p, b, W, n)
        assert round(h, 2) == expected
        numeric = _argmax(lambda x: _hedge_growth(x, p, b, W, n), 0.0, W / b)
        assert h == pytest.approx(numeric, abs=5e-6)

    def test_keeps_the_upside_when_hedging_does_not_pay(self) -> None:
        # Table row: 20 Up + $100, Up 70%, Down bid 35c -> no hedge.
        assert s.hedge_shares(0.70, 0.35, 100, 20) == 0.0

    def test_hedges_more_as_the_odds_turn(self) -> None:
        hs = [s.hedge_shares(p, 0.5, 100, 20) for p in (0.6, 0.5, 0.4, 0.3)]
        assert hs == sorted(hs)

    def test_never_bids_more_than_the_cash_buys(self) -> None:
        h = s.hedge_shares(0.02, 0.5, 10.0, 100.0)
        assert h == pytest.approx(20.0)  # $10 at 50c; the unconstrained answer is 215.2
        numeric = _argmax(lambda x: _hedge_growth(x, 0.02, 0.5, 10.0, 100.0), 0.0, 20.0)
        assert numeric == pytest.approx(20.0, abs=1e-6)

    def test_with_nothing_held_it_is_kelly_on_the_other_side(self) -> None:
        h = s.hedge_shares(0.35, 0.5, 100.0, 0.0)
        assert h * 0.5 / 100.0 == pytest.approx(s.kelly_maker(0.65, 0.5), abs=1e-12)

    def test_no_cash_no_hedge(self) -> None:
        assert s.hedge_shares(0.1, 0.5, 0.0, 50.0) == 0.0

    def test_fill_conditional_variant(self) -> None:
        # A resting hedge may not fill; the no-fill branch does not depend on h, so the best
        # h uses the chance our side wins GIVEN the hedge fills.
        p_fill, p_if_fill, p_if_not = 0.4, 0.35, 0.75
        W, n, b = 100.0, 20.0, 0.55

        def growth(h: float) -> float:
            filled = _hedge_growth(h, p_if_fill, b, W, n)
            unfilled = _hedge_growth(0.0, p_if_not, b, W, n)  # no fill: h changes nothing
            return p_fill * filled + (1 - p_fill) * unfilled

        h = s.hedge_shares_given_fill(p_if_fill, b, W, n)
        assert h == pytest.approx(s.hedge_shares(p_if_fill, b, W, n))
        assert h == pytest.approx(_argmax(growth, 0.0, W / b), abs=5e-6)
        assert h > 0.0
        # Using the unconditional chance instead would size a different (here: no) hedge.
        p_unconditional = p_fill * p_if_fill + (1 - p_fill) * p_if_not
        assert s.hedge_shares(p_unconditional, b, W, n) != pytest.approx(h, abs=1.0)

    def test_rejects_bad_inputs(self) -> None:
        with pytest.raises(ValueError):
            s.hedge_shares(0.3, 1.0, 100, 20)
        with pytest.raises(ValueError):
            s.hedge_shares(0.3, 0.5, 100, -1)
        with pytest.raises(ValueError):
            s.hedge_shares(1.3, 0.5, 100, 20)


# ---------------------------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------------------------


class TestSizeToOrder:
    def test_whole_shares(self) -> None:
        assert s.size_to_order(3.0, 0.5) == 6.0

    def test_rounds_down_to_the_share_step(self) -> None:
        assert s.size_to_order(4.0, 0.33) == 12.12  # 12.1212...

    def test_float_noise_does_not_lose_a_step(self) -> None:
        assert s.size_to_order(4.5, 0.45) == 10.0

    def test_below_the_venue_minimum_is_zero(self) -> None:
        assert s.size_to_order(2.0, 0.5) == 0.0  # 4 shares < 5
        assert s.size_to_order(2.5, 0.5) == 5.0
        assert s.size_to_order(2.0, 0.5, min_shares=1.0) == 4.0

    def test_no_stake_no_order(self) -> None:
        assert s.size_to_order(0.0, 0.5) == 0.0
        assert s.size_to_order(-3.0, 0.5) == 0.0

    def test_coarser_step(self) -> None:
        assert s.size_to_order(4.0, 0.33, tick=1.0) == 12.0

    def test_rejects_bad_inputs(self) -> None:
        with pytest.raises(ValueError):
            s.size_to_order(3.0, 0.0)
        with pytest.raises(ValueError):
            s.size_to_order(3.0, 0.5, tick=0.0)
        with pytest.raises(ValueError):
            s.size_to_order(float("nan"), 0.5)
