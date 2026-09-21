"""Unit tests for the Fade 1h Momentum on 15m model (tools/fade_1h_momentum_15m/model.py).

Every number the task doc quotes (tasks/2026-09-21-fade-1h-momentum-on-15m.md, sections 1b,
5, 6 and 9) is checked, plus the closed forms against direct quadrature, the maker fill and the
average-price (TWAP) settlement against seeded Monte Carlo of the same dynamics.
"""
from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from scipy.integrate import dblquad, quad  # noqa: E402
from scipy.optimize import minimize_scalar  # noqa: E402
from scipy.special import ndtr, ndtri  # noqa: E402

from tools.fade_1h_momentum_15m import model as fm  # noqa: E402
from tools.fade_1h_momentum_15m import validate_math as vm  # noqa: E402
from tools.fade_1h_momentum_15m.validate_math import ou_moments as reference_ou  # noqa: E402

SIGMA = 0.005


def _g_by_quadrature(t, h, kappa0, lam, power=1):
    """int_t^{t+h} exp(-power * int_u^{t+h} kappa) du by adaptive quadrature.

    Integrated in w = t + h - u, with breakpoints on the boundary layer of width
    1/kappa(t+h) that a fast reversion puts at the end of the window.
    """
    end = t + h

    def big_k(w):
        if lam == 0:
            return kappa0 * w
        return kappa0 * np.exp(-lam * end) * np.expm1(lam * w) / lam

    k_end = kappa0 * np.exp(-lam * end)
    points = [x / k_end for x in (1, 10, 100) if k_end > 0 and x / k_end < h] or None
    return quad(lambda w: np.exp(-power * big_k(w)), 0.0, h, points=points, epsabs=0,
                epsrel=1e-12, limit=400)[0]


# ------------------------------------------------------------------ section 5 examples


def test_sixty_cent_hour_at_the_open_prices_the_first_quarter_at_0_5504():
    mu = fm.mu_hat_H(0.60, 0.0, SIGMA, 0.0)
    p = fm.prob_up(0.0, 0.0, mu, 0.0, 0.0, 0.25, SIGMA, 1.0, 0.0, 0.0)
    assert float(p) == pytest.approx(0.5504, abs=5e-5)


def test_sixty_cent_hour_at_45_after_a_0_3pct_rise_prices_the_last_quarter_at_0_1719():
    mu = fm.mu_hat_H(0.60, 0.003, SIGMA, 0.75)
    assert float(mu * 0.25) == pytest.approx(-0.0024, abs=5e-5)  # "implied drift -0.24%"
    p = fm.prob_up(0.0, 0.0, mu, 0.0, 0.75, 0.25, SIGMA, 1.0, 0.0, 0.0)
    assert float(p) == pytest.approx(0.1719, abs=5e-5)


@pytest.mark.parametrize("t, weight", [(0.0, 0.50), (0.25, 0.58), (0.5, 0.71), (0.75, 1.00)])
def test_weight_on_the_hour_price_grows_through_the_hour(t, weight):
    def z(z_hour):
        m_hour = fm.martingale_prob_up(z_hour, 1.0, 1.0)
        mu = fm.mu_hat_H(m_hour, 0.001, SIGMA, t)
        return ndtri(fm.prob_up(0.0, 0.0, mu, 0.0, t, 0.25, SIGMA, 1.0, 0.0, 0.0))

    slope = (z(0.3 + 1e-6) - z(0.3 - 1e-6)) / 2e-6
    assert float(slope) == pytest.approx(0.5 / np.sqrt(1 - t), rel=1e-6)
    assert float(slope) == pytest.approx(weight, abs=0.005)


def test_implied_drift_inverts_the_hour_binary():
    mu, t, x = 0.004, 0.5, 0.002
    m = fm.martingale_prob_up(x + mu * (1 - t), 1 - t, SIGMA)
    assert float(fm.mu_hat_H(m, x, SIGMA, t)) == pytest.approx(mu, rel=1e-12)
    # prices are clipped to [0.005, 0.995] before inverting
    assert fm.mu_hat_H(1.0, 0.0, SIGMA, 0.0) == fm.mu_hat_H(0.995, 0.0, SIGMA, 0.0)


def test_quote_noise_floor_is_the_delta_method_variance():
    m, s_m, t = 0.62, 0.01, 0.3
    eps = 1e-7
    slope = (fm.mu_hat_H(m + eps, 0.0, SIGMA, t) - fm.mu_hat_H(m - eps, 0.0, SIGMA, t)) / (2 * eps)
    assert float(fm.mu_hat_H_quote_var(m, s_m, SIGMA, t)) == pytest.approx(
        float(slope) ** 2 * s_m**2, rel=1e-6)


def test_late_in_the_quarter_the_quarter_move_takes_over():
    y, h = 0.0004, 1e-4
    p = fm.prob_up(y, 0.001, -0.02, 1e-4, 0.7, h, SIGMA, 1.0, 3.0, 1.0)
    assert float(p) == pytest.approx(float(fm.martingale_prob_up(y, h, SIGMA)), abs=2e-3)
    settled = fm.prob_up(np.array([-1e-4, 0.0, 1e-4]), 0.0, 0.0, 0.0, 0.75, 0.0, SIGMA,
                         1.0, 0.0, 0.0)
    assert settled.tolist() == [0.0, 1.0, 1.0]  # ties go Up


# ------------------------------------------------------------------ section 1: OU moments


@pytest.mark.parametrize(
    "M, kappa0, lam, t, h, mean, var",
    [
        (0.004, 3.0, 4.0, 0.0, 0.25, -0.0006825139407771245, 4.356661776169525e-06),
        (-0.003, 3.0, 4.0, 0.5, 0.25, 0.0011601380321359137, 5.927554651871889e-06),
        (0.006, 3.0, 4.0, 0.25, 0.2, -9.608147588356587e-05, 4.39875477766277e-06),
    ],
)
def test_ou_moments_match_validate_math_exact_numbers(M, kappa0, lam, t, h, mean, var):
    one_minus, G, V = fm.ou_moments(t, h, kappa0, lam, SIGMA)
    got_mean = -one_minus * M + 0.004 * G
    ref_mean, ref_var, ref_g = reference_ou(M, 0.004, kappa0, lam, t, h)
    assert float(got_mean) == pytest.approx(ref_mean, rel=1e-12)
    assert float(V) == pytest.approx(ref_var, rel=1e-12)
    assert float(G) == pytest.approx(ref_g, rel=1e-12)
    assert float(got_mean) == pytest.approx(mean, rel=1e-12)
    assert float(V) == pytest.approx(var, rel=1e-12)


def test_first_case_matches_the_doc_table():
    one_minus, G, V = fm.ou_moments(0.0, 0.25, 3.0, 4.0, SIGMA)
    assert float(-one_minus * 0.004 + 0.004 * G) == pytest.approx(-0.000683, abs=5e-7)
    assert float(V) == pytest.approx(4.357e-6, abs=5e-10)


@pytest.mark.parametrize("lam", [0.0, 1e-9, 0.3, 4.0, 60.0])
@pytest.mark.parametrize("kappa0", [0.0, 1e-12])
def test_no_reversion_is_brownian_motion(kappa0, lam):
    h = 0.2
    one_minus, G, V = fm.ou_moments(0.3, h, kappa0, lam, SIGMA)
    assert float(one_minus) == pytest.approx(0.0, abs=1e-11)
    assert float(G) == pytest.approx(h, rel=1e-10)
    assert float(V) == pytest.approx(SIGMA**2 * h, rel=1e-10)


@pytest.mark.parametrize("lam", [0.0, 1e-12, 1e-6])
def test_no_decay_is_constant_speed_ou(lam):
    k, h = 3.0, 0.2
    one_minus, G, V = fm.ou_moments(0.4, h, k, lam, SIGMA)
    assert float(one_minus) == pytest.approx(1 - np.exp(-k * h), rel=1e-5)
    assert float(G) == pytest.approx((1 - np.exp(-k * h)) / k, rel=1e-5)
    assert float(V) == pytest.approx(SIGMA**2 * (1 - np.exp(-2 * k * h)) / (2 * k), rel=1e-5)


@pytest.mark.parametrize("kappa0, lam", [(1e6, 1e-6), (1e8, 3.0), (5e3, 0.05), (40.0, 300.0)])
def test_large_A_does_not_overflow(kappa0, lam):
    t, h = 0.1, 0.25
    one_minus, G, V = fm.ou_moments(t, h, kappa0, lam, SIGMA)
    assert np.isfinite([one_minus, G, V]).all()
    assert float(G) == pytest.approx(_g_by_quadrature(t, h, kappa0, lam), rel=1e-8)
    assert float(V) == pytest.approx(SIGMA**2 * _g_by_quadrature(t, h, kappa0, lam, 2), rel=1e-8)


def test_G_and_V_match_direct_quadrature_on_both_evaluation_paths():
    rng = np.random.default_rng(20260921)
    for _ in range(60):
        t, h = rng.uniform(0, 0.9), rng.uniform(1e-3, 0.25)
        kappa0, lam = 10 ** rng.uniform(-3, 2), 10 ** rng.uniform(-4, 2)
        _, G, V = fm.ou_moments(t, h, kappa0, lam, 1.0)
        assert float(G) == pytest.approx(_g_by_quadrature(t, h, kappa0, lam), rel=1e-9)
        assert float(V) == pytest.approx(_g_by_quadrature(t, h, kappa0, lam, 2), rel=1e-9)


def test_G_V_and_derivatives_hold_for_reversion_growing_through_the_hour():
    rng = np.random.default_rng(11)
    for _ in range(40):
        t, h = rng.uniform(0, 0.9), rng.uniform(1e-2, 0.25)
        kappa0, lam = 10 ** rng.uniform(-2, 1.5), -(10 ** rng.uniform(-3, 1))
        _, G, V = fm.ou_moments(t, h, kappa0, lam, 1.0)
        assert float(G) == pytest.approx(_g_by_quadrature(t, h, kappa0, lam), rel=1e-9)
        assert float(V) == pytest.approx(_g_by_quadrature(t, h, kappa0, lam, 2), rel=1e-9)
        mom = fm._ou_unit_moments_and_grad(t, h, kappa0, lam)
        for name, idx in (("k", 0), ("l", 1)):
            x = [kappa0, lam]
            e = 1e-6 * abs(x[idx])
            up, dn = list(x), list(x)
            up[idx] += e
            dn[idx] -= e
            hi = fm._ou_unit_moments_and_grad(t, h, *up)
            lo = fm._ou_unit_moments_and_grad(t, h, *dn)
            for key in ("A", "G", "V1"):
                fd = (hi[key] - lo[key]) / (2 * e)
                rounding = 1e-13 * float(abs(hi[key])) / e
                assert float(mom[f"{key}_{name}"]) == pytest.approx(float(fd), rel=1e-5,
                                                                    abs=rounding)


def test_lam_is_free_in_sign_in_the_default_fit():
    assert fm.PARAM_BOUNDS[2][0] == fm.LAM_GUARD < 0
    assert fm.PARAM_BOUNDS_LAM_GE_0[2][0] == 0.0


def test_the_two_evaluation_paths_agree_at_the_switch():
    h = 0.2
    lam = np.array([0.1 / h * (1 - 1e-9), 0.1 / h * (1 + 1e-9)])
    G, gk, gl = fm._G_and_grad(0.3, h, 2.5, lam)
    assert G[0] == pytest.approx(G[1], rel=1e-8)
    assert gk[0] == pytest.approx(gk[1], rel=1e-6)
    assert gl[0] == pytest.approx(gl[1], rel=1e-6)


def test_moment_derivatives_match_finite_differences():
    rng = np.random.default_rng(7)
    for _ in range(40):
        t, h = rng.uniform(0, 0.9), rng.uniform(1e-2, 0.25)
        kappa0, lam = 10 ** rng.uniform(-2, 1.5), 10 ** rng.uniform(-3, 1.5)
        mom = fm._ou_unit_moments_and_grad(t, h, kappa0, lam)
        for name, idx in (("k", 0), ("l", 1)):
            x = [kappa0, lam]
            e = 1e-6 * x[idx]
            up, dn = list(x), list(x)
            up[idx] += e
            dn[idx] -= e
            hi = fm._ou_unit_moments_and_grad(t, h, *up)
            lo = fm._ou_unit_moments_and_grad(t, h, *dn)
            for key in ("A", "G", "V1"):
                fd = (hi[key] - lo[key]) / (2 * e)
                rounding = 1e-13 * float(abs(hi[key])) / e  # finite-difference noise floor
                assert float(mom[f"{key}_{name}"]) == pytest.approx(float(fd), rel=1e-5,
                                                                    abs=rounding)


# ------------------------------------------------------------------ section 1b: average-price settlement


def _K(u, s, kappa0, lam):
    if lam == 0:
        return kappa0 * (s - u)
    return kappa0 * (np.exp(-lam * u) - np.exp(-lam * s)) / lam


def _twap_by_quadrature(t, h, kappa0, lam, avg):
    """(B, Gbar, Psi) straight from their definitions by nested adaptive quadrature.

    B = int (1 - e^{-K(t,s)}) ds and Gbar = int G(t,s) ds over the unknown part of the
    average; Psi = int l(u)^2 du, l(u) = int_{max(u,a')}^{end} e^{-K(u,s)} ds (section 1b).
    """
    end = t + h
    a1 = end - min(h, avg)
    kw = dict(epsabs=0, epsrel=1e-12, limit=400)

    def ell_u(u):
        return quad(lambda s: np.exp(-_K(u, s, kappa0, lam)), max(u, a1), end, **kw)[0]

    def g_ts(s):
        return quad(lambda u: np.exp(-_K(u, s, kappa0, lam)), t, s, **kw)[0]

    B = quad(lambda s: -np.expm1(-_K(t, s, kappa0, lam)), a1, end, **kw)[0]
    Gb = quad(g_ts, a1, end, **kw)[0]
    Psi = quad(lambda u: ell_u(u) ** 2, t, end, points=[a1] if a1 > t else None, **kw)[0]
    return B, Gb, Psi


def test_twap_brownian_closed_forms():
    for t, lam in ((0.0, 0.0), (0.3, 2.0), (0.8, -1.6)):
        h = 0.25 - (t % 0.25)
        B, Gb, var = fm.twap_moments(t, h, 0.0, lam, SIGMA)  # whole window
        assert float(B) == pytest.approx(0.0, abs=1e-15)
        assert float(Gb) == pytest.approx(h**2 / 2, rel=1e-12)
        assert float(var) == pytest.approx(SIGMA**2 * h**3 / 3, rel=1e-12)
        ell = min(h, fm.AVG_60S)
        gap = h - ell
        B, Gb, var = fm.twap_moments(t, h, 0.0, lam, SIGMA, fm.AVG_60S)  # trailing minute
        assert float(Gb) == pytest.approx(gap * ell + ell**2 / 2, rel=1e-12)
        assert float(var) == pytest.approx(SIGMA**2 * (gap * ell**2 + ell**3 / 3), rel=1e-12)


@pytest.mark.parametrize("lam", [0.0, 1e-9])
def test_twap_constant_speed_is_the_integrated_ou(lam):
    k, h = 3.0, 0.2
    one = -np.expm1(-k * h) / k
    B, Gb, var = fm.twap_moments(0.3, h, k, lam, SIGMA)
    assert float(B) == pytest.approx(h - one, rel=1e-8)
    assert float(Gb) == pytest.approx((h - one) / k, rel=1e-8)
    assert float(var) == pytest.approx(
        SIGMA**2 * (h - 2 * one - np.expm1(-2 * k * h) / (2 * k)) / k**2, rel=1e-8)


def test_twap_moments_match_nested_adaptive_quadrature():
    rng = np.random.default_rng(20260922)
    for _ in range(14):
        t, h = rng.uniform(0, 0.9), rng.uniform(1e-3, 0.25)
        kappa0 = 10 ** rng.uniform(-2, 1.5)
        lam = rng.choice([-1.0, 1.0]) * 10 ** rng.uniform(-3, 1)
        avg = rng.choice([fm.Q, fm.AVG_60S, 0.1])
        got = fm.twap_moments(t, h, kappa0, lam, 1.0, avg)
        ref = _twap_by_quadrature(t, h, kappa0, lam, avg)
        for g, r in zip(got, ref):
            assert float(g) == pytest.approx(r, rel=1e-8)


def test_twap_moments_match_validate_math_exponential_integral_reference():
    rng = np.random.default_rng(3)
    for _ in range(60):
        t, h = rng.uniform(0, 0.9), rng.uniform(1e-3, 0.25)
        kappa0 = 10 ** rng.uniform(-3, 1.3)
        lam = rng.choice([-1.0, 1.0]) * 10 ** rng.uniform(-2, 1)
        avg = rng.choice([fm.Q, fm.AVG_60S])
        got = fm.twap_moments(t, h, kappa0, lam, 1.0, avg)
        ref = vm.twap_unit_moments(kappa0, lam, t, h, avg)
        # the reference forms B = ell - e^{-K} L(a') as written, which cancels under a weak pull
        # (model.py integrates it directly there): B is compared on the scale of ell it multiplies
        assert float(got[0]) == pytest.approx(ref[0], rel=1e-9, abs=1e-12 * min(h, avg))
        for g, r in zip(got[1:], ref[1:]):
            assert float(g) == pytest.approx(r, rel=1e-9, abs=1e-18)


@pytest.mark.parametrize("avg", [fm.Q, fm.AVG_60S])
def test_twap_variance_is_the_double_integral_of_the_covariance(avg):
    # Var[I] = int int Cov(D(s1), D(s2)), Cov = e^{-K(s1,s2)} V(t, s1) for s1 <= s2: the form
    # before the stochastic-Fubini reduction to sigma^2 int l(u)^2 du.
    t, h, kappa0, lam = 0.78, 0.2, 0.9, -1.6
    a1 = t + h - min(h, avg)

    def cov(s2, s1):  # s1 <= s2
        v = float(fm.ou_moments(t, s1 - t, kappa0, lam, SIGMA)[2])
        return np.exp(-_K(s1, s2, kappa0, lam)) * v

    tri = dblquad(cov, a1, t + h, lambda s1: s1, lambda s1: t + h, epsabs=0, epsrel=1e-10)[0]
    assert 2 * tri == pytest.approx(float(fm.twap_moments(t, h, kappa0, lam, SIGMA, avg)[2]),
                                    rel=1e-7)


def test_twap_moment_derivatives_match_finite_differences():
    rng = np.random.default_rng(12)
    for _ in range(24):
        t, h = rng.uniform(0, 0.9), rng.uniform(1e-2, 0.25)
        kappa0 = 10 ** rng.uniform(-2, 1.3)
        lam = rng.choice([-1.0, 1.0]) * 10 ** rng.uniform(-2, 1)
        avg = rng.choice([fm.Q, fm.AVG_60S])
        mom = fm._twap_unit_moments_and_grad(t, h, kappa0, lam, avg)
        for name, idx in (("k", 0), ("l", 1)):
            x = [kappa0, lam]
            e = 1e-6 * abs(x[idx])
            up, dn = list(x), list(x)
            up[idx] += e
            dn[idx] -= e
            hi = fm._twap_unit_moments_and_grad(t, h, *up, avg)
            lo = fm._twap_unit_moments_and_grad(t, h, *dn, avg)
            for key in ("A", "G", "V1"):
                fd = (hi[key] - lo[key]) / (2 * e)
                rounding = 1e-13 * float(abs(hi[key])) / e
                assert float(mom[f"{key}_{name}"]) == pytest.approx(float(fd), rel=1e-5, abs=rounding)


def test_a_vanishing_average_is_the_close():
    args = (0.001, -0.02, 1e-5, 0.3, 0.2, SIGMA, -0.7, 3.0, -1.2)
    close = float(fm.prob_up(0.0004, *args))
    twap = float(fm.prob_up_twap(np.nan, 0.0004, *args, avg_len=1e-7))
    assert twap == pytest.approx(close, abs=1e-7)


def test_window_prob_up_is_one_switch_over_the_two_rules():
    y, M, mu, v, t, h = 0.0004, 0.001, -0.02, 1e-5, 0.3 + 2 / 60, 0.2
    params = (SIGMA, -0.7, 3.0, -1.2)
    close = fm.window_prob_up(y, M, mu, v, t, h, *params, settlement="close", abar=0.5)
    assert float(close) == float(fm.prob_up(y, M, mu, v, t, h, *params))
    twap = fm.window_prob_up(y, M, mu, v, t, h, *params, settlement="twap", abar=0.0002)
    assert float(twap) == float(fm.prob_up_twap(0.0002, y, M, mu, v, t, h, *params))
    sixty = fm.window_prob_up(y, M, mu, v, t, h, *params, settlement="twap", avg_len=fm.AVG_60S)
    assert np.isfinite(sixty) and float(sixty) != float(twap)
    with pytest.raises(ValueError):
        fm.window_prob_up(y, M, mu, v, t, h, *params, settlement="average")
    assert float(fm.martingale_window_prob_up(y, h, SIGMA, settlement="close")) == float(
        fm.martingale_prob_up(y, h, SIGMA))
    for avg in (fm.Q, fm.AVG_60S):
        mart = fm.martingale_window_prob_up(y, h, SIGMA, settlement="twap", abar=0.0002, avg_len=avg)
        assert float(mart) == pytest.approx(
            float(fm.prob_up_twap(0.0002, y, M, mu, v, t, h, SIGMA, 0.0, 0.0, 0.0, avg)), rel=1e-12)


def test_twap_worked_examples():
    mu = fm.mu_hat_H(0.60, 0.0, SIGMA, 0.0)  # 60c hour at :00
    p = fm.prob_up_twap(np.nan, 0.0, 0.0, mu, 0.0, 0.0, 0.25, SIGMA, 1.0, 0.0, 0.0)
    assert float(p) == pytest.approx(0.5437, abs=5e-5)  # Phi(Phi^-1(0.6) sqrt(3)/4)
    assert float(p) == pytest.approx(float(ndtr(ndtri(0.6) * np.sqrt(3) / 4)), rel=1e-12)
    p60 = fm.prob_up_twap(np.nan, 0.0, 0.0, mu, 0.0, 0.0, 0.25, SIGMA, 1.0, 0.0, 0.0, fm.AVG_60S)
    assert float(p60) == pytest.approx(0.5498, abs=5e-5)
    mu = fm.mu_hat_H(0.60, 0.003, SIGMA, 0.75)  # :45, hour up 0.3%, 1h still 60c
    p = fm.prob_up_twap(np.nan, 0.0, 0.0, mu, 0.0, 0.75, 0.25, SIGMA, 1.0, 0.0, 0.0)
    assert float(p) == pytest.approx(0.2062, abs=5e-5)
    p60 = fm.prob_up_twap(np.nan, 0.0, 0.0, mu, 0.0, 0.75, 0.25, SIGMA, 1.0, 0.0, 0.0, fm.AVG_60S)
    assert float(p60) == pytest.approx(0.1746, abs=5e-5)


@pytest.mark.parametrize("t", [0.0, 0.25, 0.5, 0.75])
def test_twap_weight_on_the_hour_price_is_sqrt3_over_4_of_the_close(t):
    def z(z_hour):
        mu = fm.mu_hat_H(fm.martingale_prob_up(z_hour, 1.0, 1.0), 0.001, SIGMA, t)
        return ndtri(fm.prob_up_twap(np.nan, 0.0, 0.0, mu, 0.0, t, 0.25, SIGMA, 1.0, 0.0, 0.0))

    slope = (z(0.3 + 1e-6) - z(0.3 - 1e-6)) / 2e-6
    assert float(slope) == pytest.approx(np.sqrt(3) / 4 / np.sqrt(1 - t), rel=1e-6)


def test_twap_weights_on_what_is_known():
    # z's numerator is eps * abar + ell * d: the average so far counts for the time it covers,
    # the current displacement for the time still to come.
    h, avg = 0.15, fm.Q
    base = dict(M=0.0, mu=0.0, v=0.0, t=0.1, h=h, sigma=SIGMA, theta=0.0, kappa0=0.0, lam=0.0)
    den = SIGMA * np.sqrt(h**3 / 3)
    for abar, d in ((0.0003, 0.0), (0.0, 0.0003), (0.0002, -0.0001)):
        p = fm.prob_up_twap(abar, d, avg_len=avg, **base)
        assert float(ndtri(p)) == pytest.approx(((avg - h) * abar + h * d) / den, rel=1e-9)


def test_twap_settles_at_the_end_and_ignores_an_unused_average():
    p = fm.prob_up_twap(np.array([-1e-4, 0.0, 1e-4]), 5.0, 0.0, 0.0, 0.0, 0.5, 0.0, SIGMA, 1.0, 0.3, 1.0)
    assert p.tolist() == [0.0, 1.0, 1.0]  # h = 0: only the realised average counts, ties go Up
    at_open = fm.prob_up_twap(np.nan, 0.0003, 0.0, 0.0, 0.0, 0.0, 0.25, SIGMA, 1.0, 0.0, 0.0)
    assert np.isfinite(at_open)  # nothing of the average has passed, abar is not read
    early_60s = fm.prob_up_twap(np.nan, 0.0003, 0.0, 0.0, 0.0, 0.1, 0.1, SIGMA, 1.0, 0.0, 0.0, fm.AVG_60S)
    assert np.isfinite(early_60s)
    late = fm.prob_up_twap(np.nan, 0.0003, 0.0, 0.0, 0.0, 0.1, 0.1, SIGMA, 1.0, 0.0, 0.0)
    assert np.isnan(late)  # part of the whole-window average has passed and was not given
    bad = fm.twap_moments(np.array([0.1, np.nan]), 0.2, 1.0, 1.0, SIGMA)
    assert np.isfinite(bad[1][0]) and np.isnan(bad[1][1])
    no_sigma = fm.prob_up_twap(0.0, 0.0003, 0.0, 0.0, 0.0, 0.1, 0.1, np.nan, 1.0, 0.0, 0.0)
    assert np.isnan(no_sigma)
    open_h = 0.25 - 1e-17 * 3  # float noise in h at the open: none of the average has gone
    assert np.isfinite(fm.prob_up_twap(np.nan, 0.0003, 0.0, 0.0, 0.0, 0.5, open_h, SIGMA, 1.0, 0.0, 0.0))


def test_twap_probability_matches_monte_carlo():
    gen = np.random.default_rng(20260922)
    n = 60_000
    for c in (vm.TWAP_CASES[3], vm.TWAP_CASES[6], vm.TWAP_CASES[7]):
        t, h = (c["k"] - 1) / 4 + c["tau"] / 4, (1 - c["tau"]) / 4
        B, Gb, var = fm.twap_moments(t, h, c["k0"], c["lam"], SIGMA, c["avg"])
        integ = vm.simulate_future_average(c["M"], c["mu"], c["v_mu"], c["k0"], c["lam"], t, h, c["avg"],
                                       n, gen, n_gap=100, n_avg=200)
        # theta = 1 carries the drift and its uncertainty: mu ~ N(mu, v_mu)
        p = float(fm.prob_up_twap(c["abar"], c["d"], c["M"], c["mu"], c["v_mu"], t, h, SIGMA, 1.0,
                                  c["k0"], c["lam"], c["avg"]))
        ell = min(h, c["avg"])
        eps = c["avg"] - ell
        known = (eps * c["abar"] if eps > 0 else 0.0) + ell * c["d"]
        total_var = float(var) + c["v_mu"] * float(Gb) ** 2
        assert integ.mean() == pytest.approx(float(-B * c["M"] + c["mu"] * Gb), abs=4 * integ.std() / np.sqrt(n))
        assert integ.var() == pytest.approx(total_var, rel=4 * np.sqrt(2 / n))
        assert np.mean(known + integ >= 0) == pytest.approx(p, abs=4 * np.sqrt(p * (1 - p) / n))


# ------------------------------------------------------------------ stretch and blend


def test_stretch_weights_and_soft_clip():
    r = np.array([0.001, -0.002, 0.0005] + [0.0] * 9)
    assert float(fm.stretch(r, 0.0, 1e9)) == pytest.approx(r.sum() / 12, rel=1e-9)
    w = 1.0 / np.arange(1, 13) ** 2
    w /= w.sum()
    assert float(fm.stretch(r, 2.0, 1e9)) == pytest.approx((w * r).sum(), rel=1e-9)
    big = np.array([[0.05] + [0.0] * 11, [-0.05] + [0.0] * 11])
    m = fm.stretch(big, 4.0, 0.002)  # saturates at +-c times the lag-1 weight
    w1 = 1.0 / (1.0 / np.arange(1, 13) ** 4).sum()
    assert m == pytest.approx([0.002 * w1, -0.002 * w1], rel=1e-9)


def test_blend_minimum_variance_and_no_hour_market():
    mu, v = fm.blend(0.01, 0.002, 4e-6, 9e-6, 1e-6)
    w = (9e-6 - 1e-6) / (4e-6 + 9e-6 - 2e-6)
    assert float(mu) == pytest.approx(w * 0.01 + (1 - w) * 0.002, rel=1e-12)
    assert float(v) == pytest.approx((4e-6 * 9e-6 - 1e-12) / (4e-6 + 9e-6 - 2e-6), rel=1e-12)
    # the blended error variance is below either input's
    assert float(v) < 4e-6
    mu, v = fm.blend(np.array([np.nan, 0.01]), 0.002, 4e-6, 9e-6, 1e-6)
    assert mu[0] == 0.002 and v[0] == 9e-6
    mu_eq, v_eq = fm.blend_equal(0.01, 0.002, 4e-6, 9e-6, 1e-6)
    assert float(mu_eq) == pytest.approx(0.006) and float(v_eq) == pytest.approx(3.75e-6)


# ------------------------------------------------------------------ section 6: taker


@pytest.mark.parametrize("p, a_star", [(0.55, 0.5326), (0.60, 0.5830), (0.70, 0.6849)])
def test_taker_break_even(p, a_star):
    a = fm.taker_break_even(p)
    assert float(a) == pytest.approx(a_star, abs=5e-5)
    assert float(fm.taker_ev(p, a)) == pytest.approx(0.0, abs=1e-15)
    assert float(fm.taker_break_even(p, f=0.0)) == pytest.approx(p)


def test_kelly_with_fee_is_0_1217_and_maximises_log_growth():
    p, a = 0.62, 0.55
    f = fm.kelly_taker(p, a)
    assert float(f) == pytest.approx(0.1217, abs=5e-5)
    c = fm.FEE * a * (1 - a)
    growth = minimize_scalar(
        lambda x: -(p * np.log(1 + x * (1 - a - c) / (a + c)) + (1 - p) * np.log(1 - x)),
        bounds=(0, 0.99), method="bounded", options={"xatol": 1e-10}).x
    assert float(f) == pytest.approx(growth, abs=1e-6)
    assert float(fm.kelly_taker(0.5, 0.55)) == 0.0  # no edge, no size
    assert float(fm.kelly_maker(0.6, 0.5)) == pytest.approx(0.2)


# ------------------------------------------------------------------ section 6: maker


def _mc_fill(bids, y0, t0, h0, mu_H, *, theta=0.0, mu=0.0, v=0.0, kappa0=0.0, lam=0.0, M=0.0,
             side=1.0, n=150_000, steps=400, seed=20260921, reset=False):
    """Seeded path simulation of the quarter move; a fill is a crossing of the crowd boundary.

    Time steps are dense near expiry (where the boundary is sqrt-shaped) and crossings
    between steps are caught with the Brownian-bridge probability. The paths revert toward
    the anchor set at the quote, so the realised win rate on filled paths is the "held"
    p_fill. With reset=True the third value is the "reset" p_fill: the mean over filled
    paths of the model's p re-evaluated at the fill (spot on the boundary at the end of the
    crossing step, stretch M, that path's drift).
    """
    rng = np.random.default_rng(seed)
    left = h0 * (1 - np.arange(steps + 1) / steps) ** 2
    mus = mu + np.sqrt(v) * rng.standard_normal(n)
    y = np.full(n, y0)
    zb = ndtri(np.asarray(bids))[:, None]
    hit = np.zeros((len(bids), n), bool)
    p_hit = np.full((len(bids), n), np.nan)
    for k in range(steps):
        dt = left[k] - left[k + 1]
        kap = kappa0 * np.exp(-lam * (t0 + h0 - left[k] + dt / 2))
        y_next = (y + (theta * mus - kap * (y - y0 + M)) * dt
                  + SIGMA * np.sqrt(dt) * rng.standard_normal(n))
        d0 = side * y - (SIGMA * np.sqrt(left[k]) * zb - side * mu_H * left[k])
        d1 = side * y_next - (SIGMA * np.sqrt(left[k + 1]) * zb - side * mu_H * left[k + 1])
        bridge = np.exp(-2 * np.maximum(d0, 0) * np.maximum(d1, 0) / (SIGMA**2 * dt))
        new = ~hit & ((d1 <= 0) | (rng.random(n) < bridge))
        if reset and new.any() and left[k + 1] > 0:
            y_b = side * SIGMA * np.sqrt(left[k + 1]) * zb[:, 0] - mu_H * left[k + 1]
            for j in range(len(bids)):
                sel = new[j]
                p_up = fm.prob_up(y_b[j], M, mus[sel], 0.0, t0 + h0 - left[k + 1], left[k + 1],
                                  SIGMA, theta, kappa0, lam)
                p_hit[j, sel] = p_up if side > 0 else 1.0 - p_up
        hit |= new
        y = y_next
    wins = (y >= 0) if side > 0 else (y < 0)
    out = hit.mean(1), np.array([wins[h].mean() for h in hit])
    if reset:
        last = hit & np.isnan(p_hit)  # filled in the final step: settles as it stands
        p_hit[last] = np.broadcast_to(wins, hit.shape)[last]
        out = out + (np.array([p_hit[j, hit[j]].mean() for j in range(len(bids))]),)
    return out


# validate_math.py section 6: two minutes into a quarter, y = +0.05%, 1h drift 0.2%/h.
H0 = (1 - 2 / 15) / 4
T0 = 0.25 + (2 / 15) / 4
Y0, MU_H = 0.0005, 0.002
BIDS = np.array([0.60, 0.55, 0.45])


def test_fill_probability_matches_monte_carlo_for_validate_math_bids():
    P, p_fill = fm.maker_fill(BIDS, Y0, T0, H0, SIGMA, MU_H, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    P_mc, p_mc = _mc_fill(BIDS, Y0, T0, H0, MU_H)
    assert P == pytest.approx(P_mc, abs=0.01)
    assert p_fill == pytest.approx(p_mc, abs=0.01)
    # the doc's moving-boundary simulation: 0.797 at 10c under, 0.676 at 20c under
    assert P[1:] == pytest.approx([0.797, 0.676], abs=0.01)


def _both_conventions(bids, y0, t0, h0, mu_H, kw, side, seed):
    args = (bids, y0, t0, h0, SIGMA, mu_H, kw["M"], kw["mu"], kw["v"], kw["theta"],
            kw["kappa0"], kw["lam"])
    P, p_held = fm.maker_fill(*args, side=side, fill_stretch="held")
    P_r, p_reset = fm.maker_fill(*args, side=side)  # default: reset
    P_mc, p_mc, p_mc_reset = _mc_fill(bids, y0, t0, h0, mu_H, **kw,
                                      side=1.0 if side == "up" else -1.0, seed=seed, reset=True)
    assert P_r == pytest.approx(P, abs=1e-12)  # the convention only changes p at the fill
    assert P == pytest.approx(P_mc, abs=0.01)
    assert p_held == pytest.approx(p_mc, abs=0.01)  # held = the path model's own posterior
    assert p_reset == pytest.approx(p_mc_reset, abs=0.01)
    return p_held, p_reset


def test_fill_with_fade_reversion_and_drift_uncertainty_matches_monte_carlo():
    kw = dict(theta=-0.8, mu=0.006, v=0.003**2, kappa0=3.0, lam=2.0, M=0.002)
    p_held, p_reset = _both_conventions(BIDS, Y0, T0, H0, MU_H, kw, "up", seed=1)
    # the price fell to the bid: a held anchor pulls that fall back up, a reset one does not
    assert (p_held > p_reset).all()
    _both_conventions(np.array([0.30, 0.20]), Y0, T0, H0, MU_H, kw, "down", seed=2)


def test_fill_under_strong_reversion_matches_monte_carlo():
    # kappa0 = 12/h: the clock of the Brownian-motion transform is very uneven over the
    # window, which a grid placed on calendar time does not resolve.
    kw = dict(theta=-1.0, mu=0.003, v=0.002**2, kappa0=12.0, lam=0.5, M=-0.0015)
    _both_conventions(np.array([0.48, 0.40, 0.25]), 0.0, 0.05, 0.2, 0.0, kw, "up", seed=3)


def test_fill_with_reversion_growing_through_the_hour_matches_monte_carlo():
    # lam < 0: kappa rises through the hour (the sign the data pull toward); not a NaN
    kw = dict(theta=-0.5, mu=0.004, v=0.002**2, kappa0=0.4, lam=-1.6, M=0.0015)
    t0, h0 = 0.75 + 2 / 60, 13 / 60
    P, p_fill = fm.maker_fill(BIDS, Y0, t0, h0, SIGMA, MU_H, kw["M"], kw["mu"], kw["v"],
                              kw["theta"], kw["kappa0"], kw["lam"])
    assert np.isfinite(P).all() and np.isfinite(p_fill).all()
    _both_conventions(BIDS, Y0, t0, h0, MU_H, kw, "up", seed=4)


def test_fill_conventions_agree_without_reversion():
    args = (BIDS, Y0, T0, H0, SIGMA, MU_H, 0.002, 0.006, 0.0, -0.8, 0.0, 0.0)
    _, p_held = fm.maker_fill(*args, fill_stretch="held")
    _, p_reset = fm.maker_fill(*args, fill_stretch="reset")
    assert p_held == pytest.approx(p_reset, abs=1e-12)
    with pytest.raises(ValueError):
        fm.maker_fill(*args, fill_stretch="anchor")


def test_fill_is_certain_at_or_above_the_crowd_price():
    crowd_now = float(fm.prob_up(Y0, 0.0, MU_H, 0.0, T0, H0, SIGMA, 1.0, 0.0, 0.0))
    P, p_fill = fm.maker_fill(crowd_now + 0.01, Y0, T0, H0, SIGMA, MU_H, 0.0, 0.0, 0.0,
                              0.0, 0.0, 0.0)
    assert float(P) == 1.0
    assert float(p_fill) == pytest.approx(float(fm.martingale_prob_up(Y0, H0, SIGMA)))
    P_below, _ = fm.maker_fill(crowd_now - 1e-4, Y0, T0, H0, SIGMA, MU_H, 0.0, 0.0, 0.0,
                               0.0, 0.0, 0.0)
    assert float(P_below) > 0.99  # continuous as the bid reaches the price


def test_unusable_inputs_come_back_nan_without_touching_the_rest():
    b = np.array([0.5, 0.0, 1.0, 0.5, 0.5, 0.5])
    mu = np.array([0.0, 0.0, 0.0, np.nan, 0.0, 0.0])
    sigma = np.array([SIGMA, SIGMA, SIGMA, SIGMA, 0.0, SIGMA])
    P, p_fill = fm.maker_fill(b, Y0, T0, H0, sigma, MU_H, 0.0, mu, 0.0, 0.0, 0.0, 0.0)
    assert np.isnan(P[1:5]).all() and np.isnan(p_fill[1:5]).all()
    one, one_p = fm.maker_fill(0.5, Y0, T0, H0, SIGMA, MU_H, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    # grid sizes are shared across a batch, so a row moves only at quadrature-error level
    assert P[[0, 5]] == pytest.approx([float(one)] * 2, abs=1e-4)
    assert p_fill[[0, 5]] == pytest.approx([float(one_p)] * 2, abs=1e-4)


def test_best_bid_maximises_expected_profit_and_never_crosses():
    args = (Y0, T0, H0, SIGMA, MU_H, 0.002, 0.006, 0.0, 0.8, 3.0, 2.0)
    best = fm.maker_best_bid(np.array([0.66, 0.40, 0.015]), *args)
    assert np.isnan(best["b"][2])  # no room below the price
    assert best["b"][1] <= 0.40 - 0.01 + 1e-9
    grid = np.linspace(0.02, 0.65, 64)
    P, pf = fm.maker_fill(grid, *args)
    assert best["J"][0] >= (P * (pf - grid)).max() - 1e-5
    assert best["kelly"][0] == pytest.approx(
        (best["p_fill"][0] - best["b"][0]) / (1 - best["b"][0]))


# ------------------------------------------------------------------ likelihood


def _synthetic(n, params, seed):
    rng = np.random.default_rng(seed)
    tau = rng.choice([0, 1 / 15, 2 / 15, 3 / 15, 5 / 15], n)
    t = rng.integers(0, 4, n) / 4 + tau / 4
    sigma = SIGMA * np.exp(0.3 * rng.standard_normal(n))
    obs = {
        "t": t, "h": (1 - tau) / 4, "sigma": sigma,
        "prev_returns": 0.5 * sigma[:, None] * rng.standard_normal((n, 12)),
        "mu": 0.004 * rng.standard_normal(n), "v": np.full(n, 0.002**2),
        "y": sigma * np.sqrt(tau / 4) * rng.standard_normal(n),
    }
    theta, kappa0, lam, alpha, c = params
    m = fm.stretch(obs["prev_returns"], alpha, c)
    p = fm.prob_up(obs["y"], m, obs["mu"], obs["v"], obs["t"], obs["h"], sigma, theta,
                   kappa0, lam)
    obs["up"] = (rng.random(n) < p).astype(float)
    return obs


@pytest.mark.parametrize("x", [(-0.5, 4.0, 2.0, 1.0, 0.002), (0.3, 20.0, 0.05, 3.5, 0.01),
                               (1.0, 0.7, 40.0, 0.2, 0.001), (-0.2, 0.4, -1.6, 1.0, 0.009)])
def test_likelihood_gradient_matches_finite_differences(x):
    obs = _synthetic(2000, (-0.5, 4.0, 2.0, 1.0, 0.002), seed=3)
    x = np.array(x)
    _, grad = fm.neg_log_lik(x, obs)
    for i in range(5):
        e = 1e-6 * abs(x[i])
        hi, lo = x.copy(), x.copy()
        hi[i] += e
        lo[i] -= e
        fd = (fm.neg_log_lik(hi, obs, grad=False) - fm.neg_log_lik(lo, obs, grad=False)) / (2 * e)
        assert grad[i] == pytest.approx(fd, rel=1e-4, abs=1e-4)


def test_likelihood_matches_prob_up():
    params = (-0.5, 4.0, 2.0, 1.0, 0.002)
    obs = _synthetic(500, params, seed=4)
    m = fm.stretch(obs["prev_returns"], 1.0, 0.002)
    p = fm.prob_up(obs["y"], m, obs["mu"], obs["v"], obs["t"], obs["h"], obs["sigma"],
                   -0.5, 4.0, 2.0)
    ll = np.where(obs["up"] == 1, np.log(p), np.log(1 - p)).sum()
    assert fm.neg_log_lik(params, obs, grad=False) == pytest.approx(-ll, rel=1e-10)


def test_vol_by_quarter_likelihood_reduces_to_the_document_and_scales_the_variance():
    params = (-0.5, 4.0, -1.5, 1.0, 0.002)
    obs = _synthetic(800, params, seed=7)
    obs["k"] = (np.floor(obs["t"] * 4) + 1).astype(int)
    x0 = np.concatenate([params, np.zeros(4)])
    nll0, g0 = fm.neg_log_lik_vol_by_quarter(x0, obs)
    nll, g = fm.neg_log_lik(params, obs)
    assert nll0 == pytest.approx(nll, rel=1e-13)
    assert g0[:5] == pytest.approx(g, rel=1e-10)
    ln_s = np.array([0.2, -0.1, 0.05, -0.3])
    s_row = np.exp(ln_s)[obs["k"] - 1]
    m = fm.stretch(obs["prev_returns"], 1.0, 0.002)
    p = fm.prob_up(obs["y"], m, obs["mu"], obs["v"], obs["t"], obs["h"], s_row * obs["sigma"], -0.5, 4.0, -1.5)
    ll = np.where(obs["up"] == 1, np.log(p), np.log(1 - p)).sum()
    assert fm.neg_log_lik_vol_by_quarter(np.concatenate([params, ln_s]), obs, grad=False) == pytest.approx(-ll, rel=1e-10)


def test_vol_by_quarter_gradient_matches_finite_differences():
    obs = _synthetic(2000, (-0.5, 4.0, 2.0, 1.0, 0.002), seed=8)
    obs["k"] = (np.floor(obs["t"] * 4) + 1).astype(int)
    x = np.array([-0.3, 3.0, -1.2, 1.1, 0.003, 0.15, -0.2, 0.1, -0.05])
    _, grad = fm.neg_log_lik_vol_by_quarter(x, obs)
    for i in range(9):
        e = 1e-6 * max(abs(x[i]), 0.1)
        hi, lo = x.copy(), x.copy()
        hi[i] += e
        lo[i] -= e
        fd = (fm.neg_log_lik_vol_by_quarter(hi, obs, grad=False)
              - fm.neg_log_lik_vol_by_quarter(lo, obs, grad=False)) / (2 * e)
        assert grad[i] == pytest.approx(fd, rel=1e-4, abs=1e-4)


def test_mle_recovers_planted_parameters():
    true = (-0.5, 4.0, 2.0, 1.0, 0.002)
    obs = _synthetic(60_000, true, seed=5)
    res = fm.fit_mle(obs)
    assert res.success
    theta, kappa0, lam, alpha, c = res.x
    assert theta == pytest.approx(-0.5, abs=0.1)
    assert kappa0 == pytest.approx(4.0, abs=1.0)
    assert lam == pytest.approx(2.0, abs=1.0)
    assert alpha == pytest.approx(1.0, abs=0.3)
    assert c == pytest.approx(0.002, abs=0.0006)
    assert fm.neg_log_lik(res.x, obs, grad=False) <= fm.neg_log_lik(true, obs, grad=False)


def test_mle_recovers_a_planted_negative_lam_and_the_lam_ge_0_fit_sits_on_its_bound():
    true = (-0.5, 2.0, -2.0, 1.0, 0.002)
    obs = _synthetic(60_000, true, seed=6)
    res = fm.fit_mle(obs)
    assert res.success
    assert res.x[2] == pytest.approx(-2.0, abs=1.0)
    assert res.x[2] > fm.LAM_GUARD + 1.0  # the guard does not bind
    ge0 = fm.fit_mle(obs, param_bounds=fm.PARAM_BOUNDS_LAM_GE_0)
    assert ge0.x[2] == pytest.approx(0.0, abs=1e-8)
    assert fm.neg_log_lik(res.x, obs, grad=False) < fm.neg_log_lik(ge0.x, obs, grad=False)


def _synthetic_twap(n, params, seed, avg):
    """Rows scored on average-price settlement: y = X(t) - x0, abar = realised average - x0."""
    obs = _synthetic(n, params, seed)
    rng = np.random.default_rng(seed + 100)
    tau = 1 - 4 * obs["h"]
    offset = obs["sigma"] * np.sqrt(fm.AVG_60S / 3) * rng.standard_normal(n)  # X(open) - x0
    obs["y"] = obs["y"] + offset
    obs["abar"] = np.where(tau > 0, offset + 0.5 * obs["y"] + 0.3 * obs["sigma"] * np.sqrt(tau / 12)
                           * rng.standard_normal(n), np.nan)
    theta, kappa0, lam, alpha, c = params
    m = fm.stretch(obs["prev_returns"], alpha, c)
    p = fm.prob_up_twap(obs["abar"], obs["y"], m, obs["mu"], obs["v"], obs["t"], obs["h"],
                        obs["sigma"], theta, kappa0, lam, avg)
    obs["up"] = (rng.random(n) < p).astype(float)
    return obs


@pytest.mark.parametrize("avg", [fm.Q, fm.AVG_60S])
def test_twap_likelihood_matches_prob_up_twap_and_its_gradient(avg):
    params = (-0.5, 4.0, -1.5, 1.0, 0.002)
    obs = _synthetic_twap(1500, params, 9, avg)
    m = fm.stretch(obs["prev_returns"], 1.0, 0.002)
    p = fm.window_prob_up(obs["y"], m, obs["mu"], obs["v"], obs["t"], obs["h"], obs["sigma"],
                          -0.5, 4.0, -1.5, settlement="twap", abar=obs["abar"], avg_len=avg)
    ll = np.where(obs["up"] == 1, np.log(p), np.log(1 - p)).sum()
    kw = dict(settlement="twap", avg_len=avg)
    assert fm.neg_log_lik(params, obs, grad=False, **kw) == pytest.approx(-ll, rel=1e-10)
    assert fm.neg_log_lik(params, obs, grad=False) != pytest.approx(-ll, rel=1e-6)  # "close" differs
    x = np.array([-0.3, 3.0, -1.2, 1.1, 0.003])
    _, grad = fm.neg_log_lik(x, obs, **kw)
    for i in range(5):
        e = 1e-6 * abs(x[i])
        hi, lo = x.copy(), x.copy()
        hi[i] += e
        lo[i] -= e
        fd = (fm.neg_log_lik(hi, obs, grad=False, **kw) - fm.neg_log_lik(lo, obs, grad=False, **kw)) / (2 * e)
        assert grad[i] == pytest.approx(fd, rel=1e-4, abs=1e-4)
    obs["k"] = (np.floor(obs["t"] * 4) + 1).astype(int)
    x9 = np.concatenate([x, [0.15, -0.2, 0.1, -0.05]])
    _, g9 = fm.neg_log_lik_vol_by_quarter(x9, obs, **kw)
    for i in (1, 2, 5, 8):
        e = 1e-6 * max(abs(x9[i]), 0.1)
        hi, lo = x9.copy(), x9.copy()
        hi[i] += e
        lo[i] -= e
        fd = (fm.neg_log_lik_vol_by_quarter(hi, obs, grad=False, **kw)
              - fm.neg_log_lik_vol_by_quarter(lo, obs, grad=False, **kw)) / (2 * e)
        assert g9[i] == pytest.approx(fd, rel=1e-4, abs=1e-4)


def test_mle_recovers_planted_parameters_under_twap_settlement():
    true = (-0.5, 4.0, 2.0, 1.0, 0.002)
    obs = _synthetic_twap(40_000, true, 5, fm.Q)
    res = fm.fit_mle(obs, settlement="twap")
    assert res.success
    theta, kappa0, lam, alpha, c = res.x
    assert theta == pytest.approx(-0.5, abs=0.15)
    assert kappa0 == pytest.approx(4.0, abs=1.5)
    assert lam == pytest.approx(2.0, abs=1.5)
    kw = dict(grad=False, settlement="twap")
    assert fm.neg_log_lik(res.x, obs, **kw) <= fm.neg_log_lik(true, obs, **kw)
    with pytest.raises(ValueError):
        fm.fit_mle(obs, settlement="average")


# ------------------------------------------------------------------ step 2 decision rules


def _step2():
    from tools.fade_1h_momentum_15m import step2_polymarket as s2
    return s2


def _rows(**cols):
    n = len(next(iter(cols.values())))
    base = {"mi": np.arange(n), "t0": np.arange(n) * 900, "minute": np.full(n, 2),
            "mu_L": np.full(n, 0.001), "mom_1h_to_open": np.full(n, 0.001), "up": np.ones(n),
            "later_up": np.full(n, np.nan), "later_dn": np.full(n, np.nan)}
    base.update({k: np.asarray(v, float) for k, v in cols.items()})
    return base


def _taker_case():
    nan = np.nan
    p = np.array([0.60, 0.60, 0.60, 0.40])
    S = _rows(quote_up=[0.50, 0.70, 0.50, 0.30], ask_up=[0.62, 0.50, nan, 0.30], later_up=[0.62, 0.50, 0.66, 0.30],
              quote_dn=[0.30, 0.30, 0.30, 0.55], ask_dn=[0.30, 0.30, 0.30, 0.52], later_dn=[0.30, 0.30, 0.30, 0.52],
              up=[1, 0, 1, 0])
    return S, p


def test_taker_decides_on_the_pre_t_quote_and_buys_limited_at_a_star():
    s2 = _step2()
    S, p = _taker_case()
    a_star = float(fm.taker_break_even(0.60))
    assert 0.50 < a_star < 0.62
    tr = s2.taker_trades(S, p, "model")
    # row 0: quote 0.50 < a* -> decided; the ask after t is 0.62 > a* -> limit miss, no position
    # row 1: only the post-t print is cheap -> not decided (no look-ahead)
    # row 2: decided, nobody bought Up in the 30 s after t -> kept, filled at its pre-t quote
    # row 3: p = 0.40 favours Down; the cheap Up quote is not the model's side; Down fills at 0.52 <= a*
    assert tr["row"].tolist() == [2, 3] and tr["side"].tolist() == [0, 1]
    assert tr["ask"].tolist() == [0.50, 0.52]
    assert tr["src"].tolist() == [s2.SRC_NO_PRINT, s2.SRC_PRINT]
    st = s2.taker_stats(S, tr)
    assert (st["decided"], st["entered"], st["limit_miss_price_above_a_star"], st["unpriced_not_entered"]) == (3, 2, 1, 0)
    assert st["decided_no_post_t_print_within_30s"] == 1
    assert st["per_decided_row_limit_miss_0"]["n"] == 3  # the miss counts, at 0
    assert st["same_entries_paying_the_pre_t_quote"]["n"] == 2  # matched rows
    both = s2.taker_trades(S, p, "model", side_rule="both")
    assert {(int(r), int(s)) for r, s in zip(both["row"], both["side"])} >= {(0, 1), (3, 0)}


def test_taker_execution_variants_keep_the_same_decided_rows():
    s2 = _step2()
    S, p = _taker_case()
    market = s2.taker_trades(S, p, "model", order="market")
    assert market["row"].tolist() == [0, 2, 3] and market["ask"].tolist() == [0.62, 0.50, 0.52]
    old = s2.taker_trades(S, p, "model", order="market", no_print="drop")  # the previous headline
    assert old["row"].tolist() == [0, 3] and int((~old["decided"]["priced"]).sum()) == 1
    later = s2.taker_trades(S, p, "model", no_print="later")  # row 2 priced at 0.66 > a*: a miss
    assert later["row"].tolist() == [3]
    slip = s2.taker_trades(S, p, "model", no_print="quote_plus_slippage")  # mean of 0.62-0.50, 0.52-0.55
    assert slip["ask"][0] == pytest.approx(0.50 + 0.045)
    for tr in (market, old, later, slip):
        assert tr["decided"]["row"].tolist() == [0, 2, 3]


def test_baseline_side_is_the_findings_open_minus_1h_to_open():
    s2 = _step2()
    S = _rows(quote_up=[0.45, 0.45], ask_up=[0.45, 0.45], quote_dn=[0.45, 0.45], ask_dn=[0.45, 0.45],
              mu_L=[0.001, 0.001], mom_1h_to_open=[0.001, -0.001])
    tr = s2.taker_trades(S, None, "zayan", 0.40)
    assert tr["side"].tolist() == [0, 1] and tr["row"].tolist() == [0, 1]  # side 1 = Down on row 1
    at_t = s2.taker_trades(S, None, "zayan", 0.40, side_def="t")
    assert at_t["side"].tolist() == [0, 0]


def test_tick_sensitivity_bids_1c_under_the_last_print_on_the_upper_end():
    s2 = _step2()
    n = 2
    S = _rows(market_price_up=[0.3899997255, 0.60], later_min_up=[0.375, 0.415], later_max_up=[0.60, 0.60],
              later_min_tok0=[0.375, 0.415], later_min_tok1=[0.40, 0.40])  # 0.39 as the tape stores it
    Q = {"up": {"b": np.array([0.39 - 0.01 - 0.00005, 0.4235]), "P_fill": np.full(n, 0.8),
                "p_fill": np.full(n, 0.6), "J": np.full(n, 0.05), "kelly": np.full(n, 0.1)},
         "down": {k: np.full(n, np.nan) for k in ("b", "P_fill", "p_fill", "J", "kelly")}}
    me = s2.maker_entries(S, Q, np.array([0.6, 0.6]), tick=True)
    assert me["bid"] == pytest.approx([0.38, 0.42])  # not 0.37: Brent's xatol no longer costs a tick
    assert me["on_bound"].tolist() == [True, False]
    assert me["filled"].tolist() == [True, True]


def test_pooled_first_entry_keeps_one_entry_per_market():
    s2 = _step2()
    S = _rows(quote_up=[0.40] * 4, ask_up=[0.40] * 4, quote_dn=[0.40] * 4, ask_dn=[0.40] * 4)
    S["mi"] = np.array([7, 7, 7, 8])
    S["minute"] = np.array([3, 0, 2, 5])
    tr = s2.taker_trades(S, np.array([0.7, 0.3, 0.7, 0.7]), "model")
    first = s2.first_per_market(S, tr)
    assert sorted(zip(S["mi"][first["row"]].tolist(), S["minute"][first["row"]].tolist())) == [(7, 0), (8, 5)]
    # a limit miss at minute 0 (known by t + 30) lets market 7 enter at its next decided minute, 2
    S["ask_dn"] = np.array([0.40, 0.99, 0.40, 0.40])
    first = s2.first_per_market(S, s2.taker_trades(S, np.array([0.7, 0.3, 0.7, 0.7]), "model"))
    assert sorted(zip(S["mi"][first["row"]].tolist(), S["minute"][first["row"]].tolist())) == [(7, 2), (8, 5)]
    assert s2.taker_stats(S, first)["markets_entered_at_some_minute"] == 2


def test_maker_entries_quote_only_the_favoured_side_and_flag_the_upper_bound():
    s2 = _step2()
    n = 2
    S = _rows(market_price_up=[0.55, 0.55], later_min_up=[0.40, 0.60], later_max_up=[0.60, 0.60],
              later_min_tok0=[0.40, 0.60], later_min_tok1=[0.40, 0.40])
    Q = {"up": {"b": np.array([0.54, 0.45]), "P_fill": np.full(n, 0.8), "p_fill": np.full(n, 0.6),
                "J": np.full(n, 0.05), "kelly": np.full(n, 0.1)},
         "down": {"b": np.array([0.40, 0.40]), "P_fill": np.full(n, 0.8), "p_fill": np.full(n, 0.5),
                  "J": np.full(n, 0.05), "kelly": np.full(n, 0.1)}}
    me = s2.maker_entries(S, Q, np.array([0.6, 0.6]))
    assert me["side"].tolist() == [0, 0]
    assert me["on_bound"].tolist() == [True, False]  # 0.54 = last print 0.55 - 1c
    assert me["filled"].tolist() == [True, False]
    assert s2.maker_entries(S, Q, np.array([0.6, 0.6]), side_rule="both")["row"].size == 4


def test_multiplicity_adjustments_and_clustered_interval():
    from tools.fade_1h_momentum_15m import data as fdata
    p = [0.01, 0.04, 0.03, 0.005]
    assert fdata.holm(p) == pytest.approx([0.03, 0.06, 0.06, 0.02])
    assert fdata.bh(p) == pytest.approx([0.02, 0.04, 0.04, 0.02])
    assert fdata.p_two_sided(1.959963984540054) == pytest.approx(0.05)
    s2 = _step2()
    rng = np.random.default_rng(0)
    x = (rng.random(400) < 0.5).astype(float)
    lo, hi = s2.cluster_ci(x, np.arange(400))
    lo2, hi2 = s2.cluster_ci(np.repeat(x[:100], 4), np.repeat(np.arange(100), 4))  # 4 identical rows per window
    assert hi2 - lo2 > 1.5 * (hi - lo)
