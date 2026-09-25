"""Fade 1h Momentum on 15m: the standard-library port of the research model.

The fixtures (``tests/fixtures/fade1h_model_cases.json``) were made in the research worktree
from ``tools/fade_1h_momentum_15m/model.py`` (numpy/scipy) and ``validate_math.py``: about 200
cases over both signs of lam, kappa0 = 0, strong pull, early and late in the window and the
hour, extreme 1h prices, and the last 60 s with part of the closing average known.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from polymarket_bot.fade_1h_momentum_15m import model as m

FIXTURES = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "fade1h_model_cases.json").read_text()
)
TOL = 1e-9


def _nan(x):  # noqa: ANN001, ANN202
    return math.nan if x is None else x


def _rel(a: float, b: float) -> float:
    return abs(a - b) / max(abs(b), 1e-300)


def test_the_fixtures_cover_what_the_port_must_handle() -> None:
    cases = FIXTURES["cases"]
    assert len(cases) >= 190
    lams = {c["lam"] for c in cases}
    assert any(x < 0 for x in lams) and any(x > 0 for x in lams) and 0.0 in lams
    assert any(c["kappa0"] == 0.0 for c in cases) and any(c["kappa0"] >= 12 for c in cases)
    assert any(c["lam"] * c["h"] > 0.1 for c in cases)  # the exponential-integral path
    last_minute = [c for c in cases if c["h"] < m.AVG_60S and c["abar"] is not None]
    assert len(last_minute) >= 10  # part of the closing average known
    assert any(c["t"] < 0.05 for c in cases) and any(c["t"] > 0.95 for c in cases)
    assert {x["m_H"] for x in FIXTURES["drift"]} >= {0.001, 0.999}


@pytest.mark.parametrize("case", FIXTURES["cases"], ids=lambda c: f"{c['tag']}-t{c['t']:.4f}")
def test_the_window_probability_matches_the_research_code(case) -> None:  # noqa: ANN001
    c = case
    p = m.window_prob_up(c["d"], c["M"], c["mu"], c["v"], c["t"], c["h"], c["sigma"],
                         c["theta"], c["kappa0"], c["lam"], settlement="twap",
                         abar=_nan(c["abar"]), avg_len=c["avg_len"])
    assert p == pytest.approx(c["p"], abs=TOL)
    B, Gbar, var = m.twap_moments(c["t"], c["h"], c["kappa0"], c["lam"], c["sigma"],
                                  c["avg_len"])
    for got, want in ((B, c["B"]), (Gbar, c["Gbar"]), (var, c["var_I"])):
        assert got == pytest.approx(want, rel=TOL, abs=1e-300)
    A, G, V = m.ou_moments(c["t"], c["h"], c["kappa0"], c["lam"], c["sigma"])
    for got, want in ((A, c["A"]), (G, c["G"]), (V, c["V"])):
        assert got == pytest.approx(want, rel=TOL, abs=1e-300)
    base = m.martingale_prob_up_twap(_nan(c["abar"]), c["d"], c["h"], c["sigma"], c["avg_len"])
    assert base == pytest.approx(c["p_martingale"], abs=TOL)


def test_the_stretch_and_its_derivatives_match() -> None:
    for s in FIXTURES["stretch"]:
        got = m.stretch_and_grad(s["r"], s["alpha"], s["c"])
        assert got.M == pytest.approx(s["M"], abs=1e-12)
        assert got.dM_dalpha == pytest.approx(s["dM_dalpha"], abs=1e-12)
        assert got.dM_dc == pytest.approx(s["dM_dc"], abs=1e-9)


def test_the_1h_inversion_and_the_blend_match() -> None:
    for x in FIXTURES["drift"]:
        mu_h = math.nan if x["mu_H"] is None else m.mu_hat_H(x["m_H"], x["x"], x["sigma"], x["t"])
        if x["mu_H"] is not None:
            assert mu_h == pytest.approx(x["mu_H"], rel=TOL)
        mu, v = m.blend(mu_h, x["mu_L"], x["vH"], x["vL"], x["cHL"])
        assert mu == pytest.approx(x["mu"], rel=TOL, abs=1e-15)
        assert v == pytest.approx(x["v"], rel=TOL)


def test_the_special_functions_match_scipy() -> None:
    for x, want in FIXTURES["special"]["exp1"]:
        assert m.exp1(x) == pytest.approx(want, rel=1e-12)
    for x, want in FIXTURES["special"]["expi"]:
        assert m.expi(x) == pytest.approx(want, rel=1e-12)
    for n in (48, 64):
        nodes, weights = m.gauss_legendre(n)
        want_x, want_w = FIXTURES["special"][f"legendre{n}"]
        assert nodes == pytest.approx(want_x, abs=1e-14)
        assert weights == pytest.approx(want_w, abs=1e-13)
    with pytest.raises(ValueError):
        m.exp1(0.0)
    assert m.expi(-1.0) == pytest.approx(-m.exp1(1.0), rel=1e-15)


def test_the_forward_integral_agrees_with_its_ei_form_for_either_sign_of_lam() -> None:
    for c in FIXTURES["forward_L"]:
        quad = m.forward_L(c["u"], c["end"], c["kappa0"], c["lam"])
        ei = m.forward_L_ei(c["u"], c["end"], c["kappa0"], c["lam"])
        assert quad == pytest.approx(c["L_model"], rel=TOL)
        assert ei == pytest.approx(c["L_ei"], rel=1e-9)
        assert quad == pytest.approx(ei, rel=1e-9)
    assert any(c["lam"] < 0 for c in FIXTURES["forward_L"])
    assert any(c["lam"] > 0 for c in FIXTURES["forward_L"])


def test_validate_math_reference_and_its_printed_numbers() -> None:
    # validate_math.py section 9's independent implementation, and the formula column of the
    # research doc's table 1b.10 (four digits).
    printed = [0.6862, 0.5518, 0.3387, 0.8019, 0.9733, 0.5142, 0.5337, 0.4621]
    for c, shown in zip(FIXTURES["validate_math"], printed, strict=True):
        p = m.prob_up_twap(_nan(c["abar"]), c["d"], c["M"], c["mu"], c["v"], c["t"], c["h"],
                           c["sigma"], 1.0, c["kappa0"], c["lam"], c["avg_len"])
        assert p == pytest.approx(c["p"], abs=TOL), c["name"]
        assert p == pytest.approx(shown, abs=5e-5), c["name"]
        mom = m.twap_unit_moments(c["t"], c["h"], c["kappa0"], c["lam"], c["avg_len"])
        assert mom.Gbar == pytest.approx(c["Gbar"], rel=1e-9)
        assert mom.Psi == pytest.approx(c["Psi"], rel=1e-9)
        assert mom.B == pytest.approx(c["B"], rel=1e-9, abs=1e-15)


def test_the_research_docs_worked_cases_for_the_trailing_minute() -> None:
    # Section 1b.9: momentum isolated (theta 1, kappa0 0), sigma 0.5%, start = the open price.
    sigma = 0.005
    mu = m.mu_hat_H(0.60, 0.0, sigma, 0.0)
    assert m.window_prob_up(0.0, 0.0, mu, 0.0, 0.0, 0.25, sigma, 1.0, 0.0, 0.0) == \
        pytest.approx(0.5498, abs=5e-5)
    mu = m.mu_hat_H(0.60, 0.003, sigma, 0.75)
    assert m.window_prob_up(0.0, 0.0, mu, 0.0, 0.75, 0.25, sigma, 1.0, 0.0, 0.0) == \
        pytest.approx(0.1746, abs=5e-5)


def test_the_limits_in_closed_form() -> None:
    t, h = 0.3, 0.2
    # Brownian, whole window: Gbar = h^2/2, Psi = h^3/3, B = 0.
    b0 = m.twap_unit_moments(t, m.Q, 0.0, 0.0, m.Q)
    assert (b0.B, b0.Gbar, b0.Psi) == pytest.approx((0.0, m.Q ** 2 / 2, m.Q ** 3 / 3), rel=1e-12)
    # Constant pull: B = h - (1 - e^-kh)/k, Gbar = B/k, Psi closed form.
    k = 3.0
    bk = m.twap_unit_moments(t, h, k, 0.0, m.Q)
    one = -math.expm1(-k * h) / k
    assert bk.B == pytest.approx(h - one, rel=1e-10)
    assert bk.Gbar == pytest.approx((h - one) / k, rel=1e-10)
    assert bk.Psi == pytest.approx((h - 2 * one - math.expm1(-2 * k * h) / (2 * k)) / k ** 2,
                                   rel=1e-9)
    # An averaging window shrinking to nothing recovers section 1's moments of the close.
    small = 1e-7
    bs = m.twap_unit_moments(t, h, 3.0, -1.2, small)
    sec1 = m.ou_unit_moments(t, h, 3.0, -1.2)
    assert bs.B / small == pytest.approx(sec1.A, rel=1e-5)
    assert bs.Gbar / small == pytest.approx(sec1.G, rel=1e-5)
    assert bs.Psi / small ** 2 == pytest.approx(sec1.V1, rel=1e-5)
    # Settled at the close: the closing average decides, ties go Up.
    assert m.prob_up_twap(0.0, -0.01, 0.0, 0.0, 0.0, 0.5, 0.0, 0.005, 0.0, 0.0, 0.0) == 1.0
    assert m.prob_up_twap(-1e-6, 0.01, 0.0, 0.0, 0.0, 0.5, 0.0, 0.005, 0.0, 0.0, 0.0) == 0.0


def test_the_normal_law_helpers() -> None:
    assert m.ndtr(0.0) == 0.5
    assert m.ndtr(-10.0) == pytest.approx(7.619853024160527e-24, rel=1e-12)
    assert m.ndtri(m.ndtr(1.2345)) == pytest.approx(1.2345, abs=1e-12)
    assert m.log_ndtr(-40.0) == pytest.approx(-804.6084420137538, rel=1e-12)
    assert m.log_ndtr(-5.0) == pytest.approx(math.log(m.ndtr(-5.0)), rel=1e-14)
    assert m.exprel(0.0) == 1.0 and m.exprel(1e-9) == pytest.approx(1.0 + 5e-10, rel=1e-15)
    assert m.exprel_prime(0.0) == pytest.approx(0.5)
    assert m.exprel_prime(1.0) == pytest.approx(1.0, rel=1e-12)  # (e (1-1) + 1) / 1


def test_the_moments_are_cached_and_quick() -> None:
    import time

    m.twap_unit_moments.cache_clear()
    started = time.perf_counter()
    for i in range(100):
        m.twap_unit_moments(0.3 + i * 1e-5, 0.2, 0.345, -1.62, m.AVG_60S)
    fresh = time.perf_counter() - started
    assert fresh < 2.0  # about 0.3 ms each on the Mac
    m.twap_unit_moments(0.3, 0.2, 0.345, -1.62, m.AVG_60S)
    assert m.twap_unit_moments.cache_info().hits >= 1
    with pytest.raises(ValueError):
        m.window_prob_up(0.0, 0.0, 0.0, 0.0, 0.0, 0.25, 0.005, 0.0, 0.0, 0.0, settlement="close")
