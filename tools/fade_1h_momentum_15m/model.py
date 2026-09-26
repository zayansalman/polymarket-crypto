"""Fade 1h Momentum on 15m: the model of tasks/2026-09-21-fade-1h-momentum-on-15m.md, sections 1-6 and 1b.

Pure numpy/scipy, vectorised over arrays of observations, no I/O. Units: time in hours,
sigma per sqrt(hour), drift per hour, prices as probabilities in (0, 1), returns as log
returns. Every function broadcasts its array arguments against each other.

Numerical choices (the maths is the document's; these are how it is evaluated):

- OU moments (section 1). ``G`` and ``V`` use the exponential-integral closed forms written
  as ``G = [s(A w2) - e^-K s(A w1)] / lam`` with ``s(x) = e^x E1(x)`` (never overflows; an
  asymptotic series above x = 50, and ``-gamma - ln x`` from logs when x underflows).
  Where ``lam * h <= 0.1`` that difference cancels, so the same integral is evaluated in
  the reversion-clock variable z = K(u, t+h), ``G = int_0^K e^-z / (kappa(t+h) + lam z) dz``,
  by 48-point Gauss-Legendre (exact to ~1e-15 there; kappa0 = 0 and lam = 0 are ordinary
  points of this form, and every lam < 0, reversion growing through the hour, takes this
  path). ``V`` is ``sigma^2 * G`` evaluated at ``2 * kappa0``.
- Maker fill (section 6). The Wang-Poetzelberger piecewise-linear crossing formula is used
  as the doc says, with its Gaussian expectation evaluated by recursive quadrature on a grid
  aligned with the boundary instead of by Monte Carlo; see ``maker_fill``.
- Average-price settlement (section 1b, 2026-09-22). The 15m market settles on a Chainlink
  TWAP, not on the close. ``prob_up_twap`` prices P(average of the log price over the last
  ``avg_len`` hours of the window >= the start reference). Its moments need the forward
  integral L(u) = int_u^end exp(-K(u,s)) ds, which is section 1's G on the reversed clock
  (kappa0 e^{+lam s}), so ``_G_and_grad`` evaluates it exactly; the outer integrals
  int L and int L^2 are 64-point Gauss-Legendre (relative error ~1e-12 against adaptive
  quadrature). ``window_prob_up`` is the one switch: settlement="twap" | "close". The old
  endpoint functions (``prob_up``, ``martingale_prob_up``) are unchanged, for comparison.
"""
from __future__ import annotations

from collections.abc import Mapping

import numpy as np
from scipy.optimize import OptimizeResult, minimize, minimize_scalar
from scipy.special import exp1, exprel, log_ndtr, ndtr, ndtri, roots_hermite, roots_legendre

FEE = 0.07
N_LAGS = 12
M_H_CLIP = (0.005, 0.995)
PARAM_NAMES = ("theta", "kappa0", "lam", "alpha", "c")
# lam is free in sign (section 8 asks whether lam > 0; lam < 0 = reversion growing through the
# hour). Its lower limit -10 is only a numerical guard for the optimiser's line search: kappa may
# then grow e^10-fold over the hour, and every fit so far sits far inside it (report at_bound).
LAM_GUARD = -10.0
PARAM_BOUNDS = ((None, None), (0.0, None), (LAM_GUARD, None), (0.0, 4.0), (1e-9, None))
PARAM_BOUNDS_LAM_GE_0 = ((None, None), (0.0, None), (0.0, None), (0.0, 4.0), (1e-9, None))  # sensitivity

_EULER_GAMMA = 0.5772156649015329
_LAM_H_SPLIT = 0.1  # lam * h at or below this: quadrature form; above: exponential integral
_K_TRUNC = 40.0  # quadrature form drops z > 40 (weight e^-40 ~ 4e-18 of the integral)
_GL_X, _GL_W = roots_legendre(48)
_GL_S = 0.5 * (_GL_X + 1.0)
_GL_W = 0.5 * _GL_W
_SAFE_SD = 8.5  # mass this many remaining sd above the fill boundary never fills (~1e-17)

# Section 1b: average-price settlement.
Q = 0.25  # hours in a 15m window
AVG_60S = 1.0 / 60.0  # a trailing 60-second average (the Chainlink TWAP-60s stream's own window)
SETTLEMENTS = ("twap", "close")
_TW_X, _TW_W = roots_legendre(64)
_TW_S = 0.5 * (_TW_X + 1.0)  # outer nodes on [0, 1]
_TW_W = 0.5 * _TW_W
_TW_CHUNK = 256  # unique (t, h, kappa0, lam, avg_len) rows per block: 256 * 64 * 48 floats per temporary


def _arr(x) -> np.ndarray:
    return np.asarray(x, dtype=float)


# --------------------------------------------------------------------------- section 1


def stretch(prev_returns, alpha, c):
    """M_t = sum_j w_j c tanh(r_-j / c), w_j ~ j^-alpha normalised; column 0 = latest candle."""
    return _stretch_and_grad(prev_returns, alpha, c)[0]


def _stretch_and_grad(prev_returns, alpha, c):
    """Stretch M and its derivatives dM/dalpha, dM/dc."""
    r = _arr(prev_returns)
    alpha = _arr(alpha)[..., None]
    c = _arr(c)[..., None]
    logj = np.log(np.arange(1, r.shape[-1] + 1, dtype=float))
    raw = np.exp(-alpha * logj)
    w = raw / raw.sum(-1, keepdims=True)
    u = r / c
    th = np.tanh(u)
    g = c * th
    m = (w * g).sum(-1)
    lbar = (w * logj).sum(-1, keepdims=True)
    dm_da = (w * (lbar - logj) * g).sum(-1)
    dm_dc = (w * (th - u * (1.0 - th * th))).sum(-1)
    return m, dm_da, dm_dc


def _exprel_prime(x):
    """d/dx of (e^x - 1)/x, stable at 0."""
    x = _arr(x)
    out = np.empty_like(x)
    small = np.abs(x) < 0.5
    xs = x[small]
    acc = np.zeros_like(xs)
    for n in range(18, 0, -1):  # sum_{n>=1} n x^(n-1) / (n+1)!, Horner from the top
        acc = acc * xs + n / _factorial(n + 1)
    out[small] = acc
    xl = x[~small]
    out[~small] = (np.exp(xl) * (xl - 1.0) + 1.0) / (xl * xl)
    return out


def _factorial(n: int) -> float:
    return float(np.prod(np.arange(1, n + 1, dtype=float)))


def _reversion_K(t, h, kappa0, lam):
    """K = int_t^{t+h} kappa0 e^{-lam s} ds and its derivatives in kappa0, lam."""
    t, h, k0, lam = np.broadcast_arrays(_arr(t), _arr(h), _arr(kappa0), _arr(lam))
    phi1 = h * exprel(-lam * h)  # (1 - e^{-lam h}) / lam, -> h as lam -> 0
    e_t = np.exp(-lam * t)
    dk_dk0 = e_t * phi1
    dk_dlam = k0 * e_t * (-t * phi1 - h * h * _exprel_prime(-lam * h))
    return k0 * dk_dk0, dk_dk0, dk_dlam


def _exp_e1(a, log_a):
    """s(a) = e^a E1(a) for a >= 0, from a and its log (so an underflowed a is still exact)."""
    out = np.empty_like(a)
    big = a > 50.0
    tiny = a < 1e-100
    mid = ~big & ~tiny
    out[mid] = np.exp(a[mid]) * exp1(a[mid])
    out[tiny] = -_EULER_GAMMA - log_a[tiny]  # E1(a) = -gamma - ln a + O(a)
    # a > 50: asymptotic (1/a) sum_{n<=24} (-1)^n n! a^-n by Horner; error < 1e-17.
    inv = 1.0 / a[big]
    acc = np.ones_like(inv)
    for k in range(23, -1, -1):
        acc = 1.0 - (k + 1) * inv * acc
    out[big] = acc * inv
    return out


def _G_and_grad(t, h, kappa0, lam, grad: bool = True):
    """G = int_t^{t+h} exp(-int_u^{t+h} kappa) du and, if grad, dG/dkappa0, dG/dlam."""
    t, h, k0, lam = np.broadcast_arrays(_arr(t), _arr(h), _arr(kappa0), _arr(lam))
    shape = t.shape
    t, h, k0, lam = (a.ravel() for a in (t, h, k0, lam))
    G = np.empty_like(t)
    gk = np.empty_like(t)
    gl = np.empty_like(t)
    quad = lam * h <= _LAM_H_SPLIT

    # Quadrature form: G = D int_0^1 e^{-K s} / (1 + beta s) ds, D = (e^{lam h} - 1) / lam.
    if quad.any():
        tq, hq, kq, lq = t[quad], h[quad], k0[quad], lam[quad]
        big_t = tq + hq
        x = lq * hq
        d = hq * exprel(x)
        d_l = hq * hq * _exprel_prime(x)
        beta = np.expm1(x)
        beta_l = hq * np.exp(x)
        e_T = np.exp(-lq * big_t)
        k2 = kq * e_T  # kappa at the end of the window
        big_k = k2 * d
        trunc = big_k > _K_TRUNC
        k2_safe = np.where(trunc, k2, 1.0)
        kk = np.where(trunc, _K_TRUNC, big_k)
        bb = np.where(trunc, _K_TRUNC * lq / k2_safe, beta)
        s = _GL_S
        den = 1.0 + bb[:, None] * s
        e = np.exp(-kk[:, None] * s) / den
        i0 = e @ _GL_W
        i1 = (e * s) @ _GL_W
        i2 = (e * s / den) @ _GL_W
        pref = np.where(trunc, _K_TRUNC / k2_safe, d)
        G[quad] = pref * i0
        if grad:
            gk_n = -d * d * e_T * i1
            gl_n = d_l * i0 - d * i1 * (kq * e_T * (d_l - big_t * d)) - d * i2 * beta_l
            k0_safe = np.where(trunc, kq, 1.0)
            gk_t = (-pref * i0 + pref * i2 * bb) / k0_safe
            gl_t = big_t * pref * i0 - pref * pref * (1.0 + lq * big_t) * i2
            gk[quad] = np.where(trunc, gk_t, gk_n)
            gl[quad] = np.where(trunc, gl_t, gl_n)

    # Exponential-integral form: G = [s(a2) - e^{-K} s(a1)] / lam, a_i = kappa(t_i) / lam.
    ex = ~quad
    if ex.any():
        te, he, ke, le = t[ex], h[ex], k0[ex], lam[ex]
        big_t = te + he
        big_k, _, _ = _reversion_K(te, he, ke, le)
        phi1 = he * exprel(-le * he)
        e_t, e_T = np.exp(-le * te), np.exp(-le * big_t)
        zero = big_k < 1e-150  # kappa effectively 0 over the window: Brownian limit
        with np.errstate(divide="ignore"):
            log_a1 = np.log(ke) - np.log(le) - le * te
        log_a1 = np.where(zero, 0.0, log_a1)
        log_a2 = log_a1 - le * he
        s1 = _exp_e1(np.exp(log_a1), log_a1)
        s2 = _exp_e1(np.exp(log_a2), log_a2)
        e_mk = np.exp(-big_k)
        g_e = np.where(zero, he, (s2 - e_mk * s1) / le)
        G[ex] = g_e
        if grad:
            k2 = ke * e_T
            gk[ex] = (g_e * e_T - exprel(-big_k) * e_t * phi1) / le
            gl_e = ((1.0 - k2 * g_e) * (1.0 / le + big_t) - e_mk * (1.0 / le + te) - g_e) / le
            gl[ex] = np.where(zero, 0.0, gl_e)

    G = G.reshape(shape)
    if not grad:
        return G, None, None
    return G, gk.reshape(shape), gl.reshape(shape)


def ou_moments(t, h, kappa0, lam, sigma):
    """(1 - e^-K, G, V) of section 1 for the return over [t, t+h] (exact, E1 closed forms)."""
    big_k, _, _ = _reversion_K(t, h, kappa0, lam)
    G, _, _ = _G_and_grad(t, h, kappa0, lam, grad=False)
    v1, _, _ = _G_and_grad(t, h, 2.0 * _arr(kappa0), lam, grad=False)
    return -np.expm1(-big_k), G, _arr(sigma) ** 2 * v1


def _ou_unit_moments_and_grad(t, h, kappa0, lam):
    """A = 1 - e^-K, G, V/sigma^2 and their derivatives in (kappa0, lam)."""
    big_k, k_k, k_l = _reversion_K(t, h, kappa0, lam)
    G, g_k, g_l = _G_and_grad(t, h, kappa0, lam)
    v1, v_k2, v_l = _G_and_grad(t, h, 2.0 * _arr(kappa0), lam)
    e_mk = np.exp(-big_k)
    return {
        "K": big_k, "A": -np.expm1(-big_k), "A_k": e_mk * k_k, "A_l": e_mk * k_l,
        "G": G, "G_k": g_k, "G_l": g_l, "V1": v1, "V1_k": 2.0 * v_k2, "V1_l": v_l,
    }


# --------------------------------------------------------------------------- section 1b


def _forward_L(u, end, kappa0, lam, grad: bool = True):
    """L(u) = int_u^end exp(-K(u, s)) ds and, if grad, dL/dkappa0, dL/dlam.

    Section 1's G integrates over the START of the interval with its end fixed; L integrates
    over the END with its start fixed. Reversing time (s -> -s) swaps the two and turns
    kappa0 e^{-lam s} into kappa0 e^{+lam s}, so L(u) = G(-end, end - u; kappa0, -lam),
    evaluated by the same exact routine (closed form or clock-variable quadrature).
    """
    end = _arr(end)
    G, gk, gl = _G_and_grad(-end, end - _arr(u), kappa0, -_arr(lam), grad)
    return G, gk, (None if gl is None else -gl)


def _twap_unit_moments_and_grad(t, h, kappa0, lam, avg_len=Q, grad: bool = True):
    """Section 1b moments of I = int_{a'}^{t+h} (X(s) - X(t)) ds at sigma = 1.

    The average is over the last avg_len hours of the window, [t + h - avg_len, t + h];
    a' = max(t, t + h - avg_len) is where the unknown part of it starts. With gap =
    a' - t, ell = t + h - a', L the forward integral (``_forward_L``) and K, G, V1
    section 1's over [t, a']:

        B    = ell - e^{-K} L(a')                       (E[I] = -B M + mu Gbar)
        Gbar = G L(a') + int_{a'}^{t+h} L(u) du
        Psi  = V1 L(a')^2 + int_{a'}^{t+h} L(u)^2 du     (Var[I] = sigma^2 Psi)

    Returns a dict with the keys of ``_ou_unit_moments_and_grad`` ("A" = B, "G" = Gbar,
    "V1" = Psi, each with _k and _l derivatives when grad) plus "ell" and "L": broadcast
    over its arguments, NaN where an input is not finite.
    """
    args = np.broadcast_arrays(*(_arr(a) for a in (t, h, kappa0, lam, avg_len)))
    shape = args[0].shape
    flat = np.stack([a.ravel() for a in args], 1) if args[0].size else np.zeros((0, 5))
    ok = np.isfinite(flat).all(1)
    safe = np.where(ok[:, None], flat, np.array([[0.0, Q, 0.0, 0.0, Q]]))
    uniq, inv = np.unique(safe, axis=0, return_inverse=True)
    inv = inv.ravel()
    keys = ("A", "G", "V1", "ell", "L") + (("A_k", "A_l", "G_k", "G_l", "V1_k", "V1_l") if grad else ())
    res = {k: np.empty(len(uniq)) for k in keys}
    for lo in range(0, len(uniq), _TW_CHUNK):
        tt, hh, k0, lm, av = uniq[lo:lo + _TW_CHUNK].T
        end = tt + hh
        ell = np.minimum(hh, av)
        gap = hh - ell
        sec1 = _ou_unit_moments_and_grad(tt, gap, k0, lm)  # section 1 over [t, a']
        la, la_k, la_l = _forward_L(end - ell, end, k0, lm, grad)
        nodes = end[:, None] - ell[:, None] * (1.0 - _TW_S[None, :])
        ln, ln_k, ln_l = _forward_L(nodes, end[:, None], k0[:, None], lm[:, None], grad)
        il = ell * (ln @ _TW_W)
        il2 = ell * ((ln * ln) @ _TW_W)
        stay = 1.0 - sec1["A"]  # e^{-K} over [t, a']
        # B = ell - e^{-K} L(a') cancels when the pull is weak; there B = int (1 - e^{-K(t,s)}) ds
        # is taken directly on the same nodes (smooth, so exact to rounding at K < 1/2).
        k_ts, k_ts_k, k_ts_l = _reversion_K(tt[:, None], nodes - tt[:, None], k0[:, None], lm[:, None])
        weak = _reversion_K(tt, hh, k0, lm)[0] < 0.5  # K over the whole of [t, t+h]
        b_int = ell * (-np.expm1(-k_ts) @ _TW_W)
        sl = slice(lo, lo + _TW_CHUNK)
        res["A"][sl] = np.where(weak, b_int, ell - stay * la)
        res["G"][sl] = sec1["G"] * la + il
        res["V1"][sl] = sec1["V1"] * la * la + il2
        res["ell"][sl] = ell
        res["L"][sl] = la
        if grad:
            for name, l_d, n_d, k_d in (("k", la_k, ln_k, k_ts_k), ("l", la_l, ln_l, k_ts_l)):
                b_int_d = ell * ((np.exp(-k_ts) * k_d) @ _TW_W)
                res[f"A_{name}"][sl] = np.where(weak, b_int_d, sec1[f"A_{name}"] * la - stay * l_d)
                res[f"G_{name}"][sl] = sec1[f"G_{name}"] * la + sec1["G"] * l_d + ell * (n_d @ _TW_W)
                res[f"V1_{name}"][sl] = (sec1[f"V1_{name}"] * la * la + 2.0 * sec1["V1"] * la * l_d
                                         + 2.0 * ell * ((ln * n_d) @ _TW_W))
    return {k: np.where(ok, v[inv], np.nan).reshape(shape) for k, v in res.items()}


def twap_moments(t, h, kappa0, lam, sigma, avg_len=Q):
    """(B, Gbar, Var[I]) of section 1b, the average-price counterpart of ``ou_moments``.

    I = int_{a'}^{t+h} (X(s) - X(t)) ds over the unknown part of the averaging window (the
    last avg_len hours of the window; a' = max(t, t + h - avg_len)); E[I] = -B M + mu Gbar.
    Brownian limit (kappa0 = 0, avg_len = Q): B = 0, Gbar = h^2 / 2, Var[I] = sigma^2 h^3 / 3.
    """
    m = _twap_unit_moments_and_grad(t, h, kappa0, lam, avg_len, grad=False)
    return m["A"], m["G"], _arr(sigma) ** 2 * m["V1"]


# --------------------------------------------------------------------------- sections 2-4


def implied_drift(m, x, sigma, time_left):
    """Drift per hour at which a binary priced m, move so far x, is fair: (s sqrt(T) z - x)/T."""
    z = ndtri(np.clip(_arr(m), *M_H_CLIP))
    time_left = _arr(time_left)
    return (_arr(sigma) * np.sqrt(time_left) * z - _arr(x)) / time_left


def mu_hat_H(m_H, x_t, sigma, t):
    """Section 2: the drift the 1h market price implies for the rest of the hour."""
    return implied_drift(m_H, x_t, sigma, 1.0 - _arr(t))


def mu_hat_H_quote_var(m_H, half_spread, sigma, t):
    """Section 2 quote-noise floor on var(mu_hat_H): (sigma / (sqrt(1-t) phi(z)))^2 s_m^2."""
    z = ndtri(np.clip(_arr(m_H), *M_H_CLIP))
    phi = np.exp(-0.5 * z * z) / np.sqrt(2.0 * np.pi)
    return (_arr(sigma) / (np.sqrt(1.0 - _arr(t)) * phi)) ** 2 * _arr(half_spread) ** 2


def mu_hat_L(r_L, L=1.0):
    """Section 3: spot momentum, trailing log return over L hours divided by L."""
    return _arr(r_L) / _arr(L)


def mu_hat_L_var(sigma, L=1.0):
    """Section 3: sampling variance of mu_hat_L, sigma^2 / L."""
    return _arr(sigma) ** 2 / _arr(L)


def blend(mu_H, mu_L, vH, vL, cHL):
    """Section 4 minimum-variance blend -> (mu, v); NaN mu_H (no 1h market) -> spot alone."""
    mu_H, mu_L, vH, vL, cHL = np.broadcast_arrays(*(_arr(a) for a in (mu_H, mu_L, vH, vL, cHL)))
    den = vH + vL - 2.0 * cHL
    ok = den > 1e-300
    den_safe = np.where(ok, den, 1.0)
    w = np.where(ok, (vL - cHL) / den_safe, 0.5)
    v = np.where(ok, (vH * vL - cHL * cHL) / den_safe, vH)  # den = 0: identical errors
    mu = w * mu_H + (1.0 - w) * mu_L
    no_h = np.isnan(mu_H)
    return np.where(no_h, mu_L, mu), np.where(no_h, vL, v)


def blend_equal(mu_H, mu_L, vH, vL, cHL):
    """Section 4 equal-weight comparator -> (mu, v); NaN mu_H -> spot alone."""
    mu_H, mu_L, vH, vL, cHL = np.broadcast_arrays(*(_arr(a) for a in (mu_H, mu_L, vH, vL, cHL)))
    mu = 0.5 * (mu_H + mu_L)
    v = 0.25 * (vH + vL + 2.0 * cHL)
    no_h = np.isnan(mu_H)
    return np.where(no_h, mu_L, mu), np.where(no_h, vL, v)


# --------------------------------------------------------------------------- section 5


def prob_up(y, M, mu, v, t, h, sigma, theta, kappa0, lam):
    """Section 5: P(Up) = Phi((y - (1-e^-K) M + theta mu G) / sqrt(V + theta^2 v G^2))."""
    a, G, V = ou_moments(t, h, kappa0, lam, sigma)
    theta = _arr(theta)
    num = _arr(y) - a * _arr(M) + theta * _arr(mu) * G
    den = np.sqrt(V + theta * theta * _arr(v) * G * G)
    with np.errstate(divide="ignore", invalid="ignore"):
        p = ndtr(num / den)
    return np.where(den > 0, p, (num >= 0).astype(float))  # h = 0: settled, ties go Up


def martingale_prob_up(y, h, sigma):
    """Step 1 baseline: Phi(y / (sigma sqrt(h)))."""
    return ndtr(_arr(y) / (_arr(sigma) * np.sqrt(_arr(h))))


def _avg_split(h, avg_len):
    """(eps, ell): hours of the averaging window already gone, and still to come."""
    h, avg_len = _arr(h), _arr(avg_len)
    ell = np.minimum(h, avg_len)
    return avg_len - ell, ell


_EPS_GONE = 1e-12  # hours; less than this of the average gone counts as none (float noise in h)


def _known_part(abar, d, h, avg_len):
    """eps * abar + ell * d: what the decision already knows of avg_len * (average - x0)."""
    eps, ell = _avg_split(h, avg_len)
    with np.errstate(invalid="ignore"):
        past = np.where(eps > _EPS_GONE, eps * _arr(abar), 0.0)  # abar unused (may be NaN) when none gone
    return past + ell * _arr(d)


def _settle_prob(num, den):
    """Phi(num / den); den = 0 is settled (1 if num >= 0, ties Up); NaN in, NaN out."""
    with np.errstate(divide="ignore", invalid="ignore"):
        p = ndtr(num / den)
    settled = np.where(num >= 0, 1.0, 0.0)
    return np.where(np.isnan(num) | np.isnan(den), np.nan, np.where(den > 0, p, settled))


def prob_up_twap(abar, d, M, mu, v, t, h, sigma, theta, kappa0, lam, avg_len=Q):
    """Section 1b: P(average log price over the last avg_len hours of the window >= x0).

    x0 = log of the start reference (the Chainlink TWAP-60s value at the window open). d =
    X(t) - x0, the current log price measured from the start reference. abar = realised
    average of X over the part of the averaging window already gone, minus x0 (ignored while
    that part is empty, i.e. while h >= avg_len). t = hour-time of the decision, h = hours
    left in the window; M, mu, v, sigma, theta, kappa0, lam as ``prob_up``.

        p = Phi((eps abar + ell d - B M + theta mu Gbar) / sqrt(sigma^2 Psi + theta^2 v Gbar^2))

    eps = avg_len - ell, ell = min(h, avg_len). avg_len = Q averages the whole window (the
    resolution text read literally); AVG_60S the trailing minute (the TWAP-60s stream's value
    at the close); avg_len -> 0 recovers ``prob_up`` with d = y. At h = 0 it is settled:
    1 if abar >= 0 (ties go Up), else 0.
    """
    m = _twap_unit_moments_and_grad(t, h, kappa0, lam, avg_len, grad=False)
    theta, Gb = _arr(theta), m["G"]
    num = _known_part(abar, d, h, avg_len) - m["A"] * _arr(M) + theta * _arr(mu) * Gb
    den = np.sqrt(_arr(sigma) ** 2 * m["V1"] + theta * theta * _arr(v) * Gb * Gb)
    return _settle_prob(num, den)


def martingale_prob_up_twap(abar, d, h, sigma, avg_len=Q):
    """The no-edge baseline under average-price settlement (no drift, no reversion).

    Phi((eps abar + ell d) / (sigma sqrt(gap ell^2 + ell^3 / 3))), gap = h - ell: the
    Brownian case of ``prob_up_twap`` in closed form.
    """
    eps, ell = _avg_split(h, avg_len)
    gap = _arr(h) - ell
    den = _arr(sigma) * np.sqrt(gap * ell * ell + ell ** 3 / 3.0)
    return _settle_prob(_known_part(abar, d, h, avg_len), den)


def _check_settlement(settlement):
    if settlement not in SETTLEMENTS:
        raise ValueError(f"settlement must be one of {SETTLEMENTS}")


def window_prob_up(y, M, mu, v, t, h, sigma, theta, kappa0, lam, *, settlement, abar=np.nan,
                   avg_len=Q):
    """P(the 15m window resolves Up) under either settlement rule: the one switch.

    settlement="twap": ``prob_up_twap`` (the market's rule since the 2026-09-22 correction),
    with y = X(t) - x0 measured from the start reference and abar the realised average so far
    minus x0. settlement="close": ``prob_up`` (the old endpoint target, kept for comparison),
    with y = X(t) - X(window open); abar and avg_len are ignored.
    """
    _check_settlement(settlement)
    if settlement == "close":
        return prob_up(y, M, mu, v, t, h, sigma, theta, kappa0, lam)
    return prob_up_twap(abar, y, M, mu, v, t, h, sigma, theta, kappa0, lam, avg_len)


def martingale_window_prob_up(y, h, sigma, *, settlement, abar=np.nan, avg_len=Q):
    """The martingale (no-edge) baseline under either settlement rule; arguments as
    ``window_prob_up``."""
    _check_settlement(settlement)
    if settlement == "close":
        return martingale_prob_up(y, h, sigma)
    return martingale_prob_up_twap(abar, y, h, sigma, avg_len)


# --------------------------------------------------------------------------- section 6


def taker_fee(a, f=FEE):
    """Fee per share when taking at price a: f a (1 - a)."""
    a = _arr(a)
    return f * a * (1.0 - a)


def taker_ev(p, a, f=FEE):
    """Expected value per share of taking at a: p - a - f a (1 - a)."""
    return _arr(p) - _arr(a) - taker_fee(a, f)


def taker_break_even(p, f=FEE):
    """Highest taker price with EV >= 0: a* = ((1+f) - sqrt((1+f)^2 - 4 f p)) / (2 f)."""
    p = _arr(p)
    return 2.0 * p / ((1.0 + f) + np.sqrt((1.0 + f) ** 2 - 4.0 * f * p))  # rationalised form


def kelly_taker(p, a, f=FEE):
    """Kelly fraction taking at a with fee: (p - a - c) / (1 - a - c), floored at 0."""
    c = taker_fee(a, f)
    a = _arr(a)
    return np.maximum(0.0, (_arr(p) - a - c) / (1.0 - a - c))


def kelly_maker(p_fill, b):
    """Kelly fraction for a maker fill at b (no fee): (p_fill - b) / (1 - b), floored at 0."""
    b = _arr(b)
    return np.maximum(0.0, (_arr(p_fill) - b) / (1.0 - b))


def crowd_boundary(b, h_left, sigma, mu_H):
    """Quarter move at which the crowd's Up price equals b: sigma sqrt(h') Phi^-1(b) - mu_H h'."""
    h_left = _arr(h_left)
    return _arr(sigma) * np.sqrt(h_left) * ndtri(_arr(b)) - _arr(mu_H) * h_left


def _clock_fractions(ratio, start, tail):
    """Grid on the Brownian clock, as fractions of its total: geometric from both ends."""
    head, g = [], start
    while g < 0.5:
        head.append(g)
        g /= ratio
    back, r = [], 0.5
    while True:
        back.append(1.0 - r)
        if r <= tail:
            break
        r *= ratio
    return np.array([0.0] + head + back)


def _gregory_weights(n):
    """Trapezoid weights on n + 1 points of [0, 1] with Gregory end corrections."""
    w = np.ones(n + 1)
    w[:3] = w[-3:][::-1] = (3.0 / 8.0, 7.0 / 6.0, 23.0 / 24.0)
    return w / n


def _path_moments(t0, el, sigma, kappa0, lam):
    """K, 1 - e^-K, G and the Brownian clock Gamma = e^{2K} V, el hours after t0."""
    big_k, _, _ = _reversion_K(t0, el, kappa0, lam)
    a, g, v = ou_moments(t0, el, kappa0, lam, sigma)
    return big_k, a, g, np.exp(2.0 * big_k) * v


# Elapsed fractions of the window where the clock is tabulated to place the grid times.
_CLOCK_TABLE = np.unique(np.concatenate([
    [0.0, 1.0], 0.5 * np.geomspace(1e-9, 1.0, 90), 1.0 - 0.5 * np.geomspace(1e-10, 1.0, 100)]))


FILL_STRETCH = ("reset", "held")


def _fill_core(b, y0, t0, h0, sigma, mu_H, M, mu, theta, kappa0, lam,
               n_max, ppsd, ratio, start, tail, fill_stretch="reset"):
    """P(fill) and P(fill and Up) for flat 1-D problems with a known drift mu (v = 0)."""
    nb = b.size
    col = (lambda a: a[:, None])  # noqa: E731
    zb = ndtri(b)

    # Our model's path of the quarter move Y (section 1 with drift theta mu), mapped to a
    # Brownian motion: Z(s) = e^{K_s} (Y(s) - E Y(s)) runs on the clock Gamma = e^{2K} V.
    # Grid times are placed geometrically on that clock from both ends, so every step is a
    # fixed fraction of the clock already run or still to run, whatever the reversion.
    frac = _clock_fractions(ratio, start, tail)
    n = frac.size - 1
    _, _, _, tab = _path_moments(col(t0), h0[:, None] * _CLOCK_TABLE, col(sigma),
                                 col(kappa0), col(lam))
    tab = tab / tab[:, -1:]
    idx = np.clip((tab[:, None, :] <= frac[None, :, None]).sum(-1) - 1, 0, tab.shape[1] - 2)
    f0, f1 = np.take_along_axis(tab, idx, 1), np.take_along_axis(tab, idx + 1, 1)
    w = np.clip((frac[None, :] - f0) / np.maximum(f1 - f0, 1e-300), 0.0, 1.0)
    el = h0[:, None] * (_CLOCK_TABLE[idx] + w * (_CLOCK_TABLE[idx + 1] - _CLOCK_TABLE[idx]))
    el[:, 0] = 0.0
    hl = h0[:, None] - el  # time left at each grid point
    bnd = crowd_boundary(col(b), hl, col(sigma), col(mu_H))
    big_k, a_s, g_s, gam = _path_moments(col(t0), el, col(sigma), col(kappa0), col(lam))
    mean = col(y0) - a_s * col(M) + col(theta) * col(mu) * g_s
    c = np.exp(big_k) * (bnd - mean)  # the fill boundary in Z coordinates
    k_e, a_e, g_e, gam_e = _path_moments(t0, h0, sigma, kappa0, lam)
    c_e = np.exp(k_e) * (0.0 - (y0 - a_e * M + theta * mu * g_e))

    # Where mass can be, relative to the boundary: within 8.5 sd of Z's spread so far, and
    # not so far above that the boundary cannot reach it in the clock left (never fills).
    later_max = np.maximum.accumulate(np.concatenate([c, c_e[:, None]], 1)[:, ::-1], 1)[:, ::-1]
    rise = np.maximum(later_max[:, 1:] - c, 0.0)
    spread = np.sqrt(gam)
    lo = np.maximum(0.0, -_SAFE_SD * spread - c)
    left = np.sqrt(np.maximum(col(gam_e) - gam, 0.0))
    top = np.maximum(np.minimum(_SAFE_SD * spread - c, rise + _SAFE_SD * left), lo)
    step_sd = np.sqrt(np.maximum(np.diff(gam, axis=1), 1e-300))  # step_sd[:, i-1]: step i
    scale = np.minimum(step_sd, np.concatenate([step_sd[:, 1:], step_sd[:, -1:]], 1))
    need = np.ceil(ppsd * ((top[:, 1:] - lo[:, 1:]) / scale).max(0))
    n_pts = np.clip(need, 8, n_max).astype(int)  # grid intervals at each step, batch-wide

    # Up-probability at the fill state, evaluated on the boundary at each grid time. "reset":
    # the model re-evaluated there as a new decision would be (section 6), so the stretch is M
    # (no 15m candle completes inside the quarter; the anchor moves with the price, as in the
    # step 1 fit, where the in-quarter move y is never pulled back). "held": the anchor stays
    # where it was set at the quote, so the move from the quote to the fill is pulled back too
    # (the posterior of this path model's own SDE; a sensitivity).
    stretch_fill = col(M) if fill_stretch == "reset" else col(M) + bnd - col(y0)
    p_bnd = prob_up(bnd, stretch_fill, col(mu), 0.0, col(t0) + el, hl,
                    col(sigma), col(theta), col(kappa0), col(lam))

    xi0 = -c[:, 0]
    immediate = xi0 <= 0.0
    kill = np.zeros(nb)
    fill_up = np.zeros(nb)
    grid_prev = np.where(immediate, 1.0, xi0)[:, None]
    mass_prev = np.ones((nb, 1))
    for i in range(1, n + 1):
        delta = np.maximum(gam[:, i] - gam[:, i - 1], 1e-300)[:, None]
        sd = np.sqrt(delta)
        dc = (c[:, i] - c[:, i - 1])[:, None]
        # Cross during the step (boundary linear in the clock), from x above it:
        # P(tau <= delta) = Phi(-(x-dc)/sd) + e^{2x dc/delta} Phi(-(x+dc)/sd), and the mean
        # crossing time E[tau; tau <= delta] / delta = (x/dc) [first - second], which places
        # each fill inside the step so p is interpolated linearly between the step's ends.
        xp = grid_prev
        first = ndtr(-(xp - dc) / sd)
        second = np.exp(2.0 * xp * dc / delta + log_ndtr(-(xp + dc) / sd))
        r = xp / sd
        flat = np.abs(dc) < 1e-6 * sd
        dc_safe = np.where(flat, 1.0, dc)
        at_zero = 2.0 * (r * np.exp(-0.5 * r * r) / np.sqrt(2.0 * np.pi) - r * r * ndtr(-r))
        when = np.where(flat, at_zero, xp / dc_safe * (first - second))
        killed = (mass_prev * (first + second)).sum(1)
        kill += killed
        fill_up += (killed * p_bnd[:, i - 1]
                    + (mass_prev * when).sum(1) * (p_bnd[:, i] - p_bnd[:, i - 1]))
        # Transition density onto the new grid, with the Brownian-bridge non-crossing factor.
        u = np.linspace(0.0, 1.0, n_pts[i - 1] + 1)
        grid = col(lo[:, i]) + col(top[:, i] - lo[:, i]) * u[None, :]
        diff = grid[:, None, :] - xp[:, :, None] + dc[:, :, None]
        kern = np.exp(-0.5 * diff * diff / delta[:, :, None])
        kern /= np.sqrt(2.0 * np.pi) * sd[:, :, None]
        kern *= -np.expm1(-2.0 * xp[:, :, None] * grid[:, None, :] / delta[:, :, None])
        dens = np.einsum("bj,bjk->bk", mass_prev, kern)
        grid_prev = grid
        mass_prev = dens * col(top[:, i] - lo[:, i]) * _gregory_weights(n_pts[i - 1])[None, :]
    # Last sliver of the window: the crowd's price is then a martingale under our model to
    # O(sqrt(time left)), so P(fill later | Y) = (1 - price) / (1 - b), and a fill there
    # settles Up with the boundary probability.
    xi_y = grid_prev * np.exp(-big_k[:, n])[:, None]
    price = ndtr(zb[:, None] + xi_y / (sigma[:, None] * np.sqrt(hl[:, n])[:, None]))
    tail_fill = (mass_prev * (1.0 - price)).sum(1) / (1.0 - b)
    kill += tail_fill
    fill_up += tail_fill * p_bnd[:, n]
    p_now = prob_up(y0, M, mu, 0.0, t0, h0, sigma, theta, kappa0, lam)
    kill = np.where(immediate, 1.0, kill)
    fill_up = np.where(immediate, p_now, fill_up)
    return kill, fill_up


def maker_fill(b, y, t, h, sigma, mu_H, M, mu, v, theta, kappa0, lam, side="up", *,
               fill_stretch="reset", n_max=256, ppsd=2.5, ratio=0.8, start=1e-3, tail=1e-4,
               n_gh=5, chunk=64):
    """Section 6 maker fill: (P_fill(b), p_fill(b)) for a bid resting at b until the window ends.

    A bid on the Up token at b fills when the crowd's Up price, Phi((Y + mu_H h')/(sigma
    sqrt(h'))), comes down to b, i.e. when the quarter move Y first reaches the moving
    boundary B(h') = sigma sqrt(h') Phi^-1(b) - mu_H h' (h' = time left at the fill). Y
    follows this model's own dynamics (section 1: drift theta*mu, reversion of the stretch M
    at speed kappa0 e^{-lam s}, lam of either sign). p_fill = P(Up | fill) = E[p(fill state)]
    / P_fill, where p(fill state) is ``prob_up`` re-evaluated with the spot on the boundary
    at the fill time; adverse selection is inside it, not a haircut.

    fill_stretch sets the stretch used for that re-evaluation. "reset" (default, section 6's
    "re-evaluated at the fill state"): M, what a new decision at that instant would use, since
    no 15m candle completes inside the quarter. "held": M + (B - y), the anchor kept where it
    was set at the quote, so the move from the quote to the fill is also pulled back; this is
    the exact posterior of the path model below, a sensitivity.

    Arguments are those of ``prob_up`` (y = quarter move so far, t = hour-time now, h = hours
    left in the quarter) plus b and mu_H (the crowd's drift that sets the boundary). ``side``
    "down" prices a bid on the Down token by mirror symmetry (y, M, mu, mu_H change sign;
    p_fill is then P(Down | fill)).

    Approximations (at the defaults, over 14 random states and 4 bids each, P_fill is within
    8e-4 and p_fill within 5e-4 of the same computation refined to ppsd=5, ratio=0.9,
    start=1e-4, tail=1e-6, n_gh=9, and within Monte Carlo error of simulated paths):
    1. Wang-Poetzelberger: between grid times the boundary is linear in the Brownian clock
       Gamma of Z = e^K (Y - E Y); the crossing probability of that piecewise-linear
       boundary is exact. Grid times are geometric on Gamma from both ends (steps of
       1 - ratio of the clock run or still to run, from start * Gamma to tail * Gamma left),
       so the sqrt(h') end of the boundary and strong reversion are both resolved.
    2. Its Gaussian expectation is evaluated by recursive quadrature (trapezoid with Gregory
       end corrections), not by Monte Carlo, on a grid aligned with the boundary spanning
       where mass can be (8.5 sd of Z's spread) and can still fill (8.5 sd of the clock
       left above the boundary's future high), with ppsd points per step sd (<= n_max).
       Grid sizes are shared by the rows computed together (chunk), so a row's value can
       move at quadrature-error level (<1e-4) with what it is batched with.
    3. The last part of the window (tail * Gamma of clock left) uses P(fill later) =
       (1 - price)/(1 - b), exact for a martingale price; drift and reversion over that
       sliver are neglected.
    4. Inside a grid step, p at the fill is interpolated linearly in the clock at the exact
       mean first-passage time of that step's linear boundary.
    5. Drift uncertainty v enters by Gauss-Hermite over mu ~ N(mu, v) with n_gh nodes (one
       node when v = 0), so the posterior on mu given a fill is exact up to the rule.
    6. The crowd's drift mu_H is held fixed through the window.
    If y is already at or below the boundary the bid fills now: P_fill = 1, p_fill = p now.
    """
    if side not in ("up", "down"):
        raise ValueError("side must be 'up' or 'down'")
    if fill_stretch not in FILL_STRETCH:
        raise ValueError(f"fill_stretch must be one of {FILL_STRETCH}")
    sgn = 1.0 if side == "up" else -1.0
    args = np.broadcast_arrays(*(_arr(a) for a in (b, y, t, h, sigma, mu_H, M, mu, v,
                                                   theta, kappa0, lam)))
    shape = args[0].shape
    flat = np.stack([a.ravel() for a in args]) if args[0].size else np.zeros((12, 0))
    # lam may take either sign (ou_moments is exact for lam < 0 as well).
    valid = (np.isfinite(flat).all(0) & (flat[0] > 0) & (flat[0] < 1) & (flat[3] > 0)
             & (flat[4] > 0) & (flat[8] >= 0) & (flat[10] >= 0))
    benign = np.array([0.5, 0.0, 0.0, 0.25, 0.005, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    flat = np.where(valid[None, :], flat, benign[:, None])  # invalid rows come back NaN
    b, y, t, h, sigma, mu_H, M, mu, v, theta, kappa0, lam = flat
    y, mu_H, M, mu = sgn * y, sgn * mu_H, sgn * M, sgn * mu
    xg, wg = roots_hermite(n_gh)
    wg = wg / np.sqrt(np.pi)
    has_v = v > 0
    reps = np.where(has_v, n_gh, 1)
    row = np.repeat(np.arange(b.size), reps)
    node = np.concatenate([np.arange(r) for r in reps]) if b.size else np.zeros(0, int)
    one = ~has_v[row]
    shift = np.where(one, 0.0, np.sqrt(2.0 * v[row]) * xg[node])
    weight = np.where(one, 1.0, wg[node])
    mu_nodes = mu[row] + shift
    pf = np.zeros(b.size)
    pu = np.zeros(b.size)
    for lo in range(0, row.size, chunk):
        sl = slice(lo, lo + chunk)
        r = row[sl]
        kill, up = _fill_core(b[r], y[r], t[r], h[r], sigma[r], mu_H[r], M[r], mu_nodes[sl],
                              theta[r], kappa0[r], lam[r], n_max, ppsd, ratio, start, tail,
                              fill_stretch)
        np.add.at(pf, r, weight[sl] * kill)
        np.add.at(pu, r, weight[sl] * up)
    with np.errstate(invalid="ignore", divide="ignore"):
        p_fill = np.clip(pu / pf, 0.0, 1.0)
    pf = np.where(valid, np.clip(pf, 0.0, 1.0), np.nan)  # quadrature can overshoot by ~1e-6
    p_fill = np.where(valid, p_fill, np.nan)
    return pf.reshape(shape), p_fill.reshape(shape)


def maker_best_bid(price_now, y, t, h, sigma, mu_H, M, mu, v, theta, kappa0, lam, side="up", *,
                   tick=0.01, xatol=1e-4, **fill_kw):
    """b* = argmax P_fill(b) (p_fill(b) - b) on [tick, price_now - tick] by bounded Brent.

    price_now is the current price of the token bid for (the Down token when side="down").
    Returns a dict of arrays: b, P_fill, p_fill, J (expected profit per share bid) and kelly
    (maker Kelly at p_fill). One Brent search per observation; NaN where the book leaves no
    room below price_now. Where J is still rising at price_now - tick (the model rates the
    token above its price), b* sits on that upper end and J'(b*) = 0 does not hold there: the
    last print, not the fill model, sets the bid. fill_kw go to ``maker_fill`` (fill_stretch,
    grid options).
    """
    args = np.broadcast_arrays(*(_arr(a) for a in (price_now, y, t, h, sigma, mu_H, M, mu, v,
                                                   theta, kappa0, lam)))
    shape = args[0].shape
    flat = [a.ravel() for a in args]
    out = {k: np.full(flat[0].size, np.nan) for k in ("b", "P_fill", "p_fill", "J", "kelly")}
    for i in range(flat[0].size):
        price, *rest = (a[i] for a in flat)
        lo, hi = tick, price - tick
        if not np.isfinite([price, *rest]).all() or hi <= lo:
            continue

        def neg_j(bid, rest=rest):
            P, pf = maker_fill(bid, *rest, side=side, **fill_kw)
            return -float(P * (pf - bid))

        res = minimize_scalar(neg_j, bounds=(lo, hi), method="bounded",
                              options={"xatol": xatol})
        bid = float(res.x)
        P, pf = maker_fill(bid, *rest, side=side, **fill_kw)
        out["b"][i], out["P_fill"][i], out["p_fill"][i] = bid, float(P), float(pf)
        out["J"][i] = float(P * (pf - bid))
        out["kelly"][i] = float(kelly_maker(pf, bid))
    return {k: a.reshape(shape) for k, a in out.items()}


# --------------------------------------------------------------------------- section 7


def neg_log_lik(params, obs: Mapping, grad: bool = True, *, settlement: str = "close",
                avg_len=Q):
    """-sum[o ln p + (1-o) ln(1-p)] over quarter outcomes, and its analytic gradient.

    params = (theta, kappa0, lam, alpha, c). obs maps: 'up' (1 if the quarter closed Up),
    'y' (quarter move at the decision), 'prev_returns' (n, 12; column 0 the candle just
    before the window), 'mu' and 'v' (blended drift and its error variance, section 4),
    't' (hour-time of the decision), 'h' (hours left), 'sigma' (per sqrt hour).

    settlement="close" (default, what every fit so far used) scores ``prob_up``;
    settlement="twap" scores ``prob_up_twap`` over the last avg_len hours of the window,
    with 'y' = X(t) - x0 from the start reference and 'up' the average-price outcome, and
    needs 'abar' (realised average minus x0; ignored where none of the average has passed).
    """
    nll, g, _ = _neg_log_lik_core(params, obs, None, grad, settlement, avg_len)
    return (nll, g) if grad else nll


N_QUARTERS = 4


def neg_log_lik_vol_by_quarter(params, obs: Mapping, grad: bool = True, *,
                               settlement: str = "close", avg_len=Q):
    """Sensitivity, not in the document: ``neg_log_lik`` with the diffusion variance of each
    quarter of the hour scaled by its own factor, V = s_k^2 sigma^2 V1(kappa0, lam).

    params = (theta, kappa0, lam, alpha, c, ln s_1, .., ln s_4); obs as ``neg_log_lik`` plus
    'k' (quarter of the hour, 1..4). sigma is the trailing 60-minute realised volatility, which
    is not a forecast of the coming quarter's: forward volatility differs by quarter of the
    hour, and in the document's model kappa sets both the mean pull (1 - e^-K) and the variance
    shrink V < sigma^2 h, so an intra-hour volatility pattern can load on lam. Freeing s_k
    separates the two. At ln s_k = 0 this is ``neg_log_lik``. Gradient over all nine.
    settlement and avg_len as ``neg_log_lik``.
    """
    params = _arr(params)
    k = np.asarray(obs["k"], dtype=int) - 1
    q = np.exp(2.0 * params[5:5 + N_QUARTERS])[k]
    nll, g, row = _neg_log_lik_core(params[:5], obs, q, grad, settlement, avg_len)
    if not grad:
        return nll
    g_s = -np.bincount(k, weights=row, minlength=N_QUARTERS)
    return nll, np.concatenate([g, g_s])


def _neg_log_lik_core(params, obs: Mapping, var_mult, grad: bool, settlement: str = "close",
                      avg_len=Q):
    """(nll, gradient over the five parameters, per-row d ln L / d ln s) with the diffusion
    variance sigma^2 V1 multiplied per row by var_mult (None = 1; the last output is then None).

    Both settlement rules share one form, z = (known - A M + theta mu G) / sqrt(sigma^2 V1 +
    theta^2 v G^2): "close" has known = y and section 1's (A, G, V1); "twap" has known =
    eps abar + ell y and section 1b's (B, Gbar, Psi) in their place."""
    _check_settlement(settlement)
    theta, kappa0, lam, alpha, c = (float(x) for x in params)
    up = _arr(obs["up"])
    y, mu, v = _arr(obs["y"]), _arr(obs["mu"]), _arr(obs["v"])
    sig2 = _arr(obs["sigma"]) ** 2
    if var_mult is not None:
        sig2 = sig2 * _arr(var_mult)
    m, dm_da, dm_dc = _stretch_and_grad(obs["prev_returns"], alpha, c)
    th = np.stack(np.broadcast_arrays(_arr(obs["t"]), _arr(obs["h"])), 1)
    uniq, inv = np.unique(th, axis=0, return_inverse=True)
    inv = inv.ravel()
    if settlement == "close":
        known = y
        unit = _ou_unit_moments_and_grad(uniq[:, 0], uniq[:, 1], kappa0, lam)
    else:
        known = _known_part(obs.get("abar", np.nan), y, obs["h"], avg_len)  # NaN only where used
        unit = _twap_unit_moments_and_grad(uniq[:, 0], uniq[:, 1], kappa0, lam, avg_len)
    mom = {k: x[inv] for k, x in unit.items()}
    a, G = mom["A"], mom["G"]
    num = known - a * m + theta * mu * G
    s2 = sig2 * mom["V1"] + theta * theta * v * G * G
    s = np.sqrt(s2)
    z = num / s
    sign = 2.0 * up - 1.0
    ll = log_ndtr(sign * z)
    nll = -float(ll.sum())
    if not grad:
        return nll, None, None
    # d ln Phi(sign z) / dz = sign phi(z) / Phi(sign z)
    dll = sign * np.exp(-0.5 * z * z - 0.5 * np.log(2.0 * np.pi) - ll)

    def dz(dnum, ds2):
        return (dnum - 0.5 * z * ds2 / s) / s

    g = np.array([
        dz(mu * G, 2.0 * theta * v * G * G),
        dz(-mom["A_k"] * m + theta * mu * mom["G_k"],
           sig2 * mom["V1_k"] + 2.0 * theta * theta * v * G * mom["G_k"]),
        dz(-mom["A_l"] * m + theta * mu * mom["G_l"],
           sig2 * mom["V1_l"] + 2.0 * theta * theta * v * G * mom["G_l"]),
        dz(-a * dm_da, 0.0),
        dz(-a * dm_dc, 0.0),
    ])
    row = None if var_mult is None else dll * dz(0.0, 2.0 * sig2 * mom["V1"])  # d ln L_i / d ln s
    return nll, -(g * dll).sum(1), row


def fit_mle(obs: Mapping, x0=(0.0, 1.0, 1.0, 1.0, 0.003), c_unit=1e-3,
            param_bounds=PARAM_BOUNDS, *, settlement: str = "close", avg_len=Q,
            **kw) -> OptimizeResult:
    """Maximum likelihood of (theta, kappa0, lam, alpha, c) by L-BFGS-B within param_bounds.

    c is optimised in units of c_unit (1e-3 = 0.1% log return) so all five parameters are
    of order one for the optimiser; the result's x is in natural units. The default bounds
    leave lam free in sign; PARAM_BOUNDS_LAM_GE_0 is the lam >= 0 sensitivity. settlement
    and avg_len as ``neg_log_lik`` (default "close", what every fit so far used).
    """
    _check_settlement(settlement)
    scale = np.array([1.0, 1.0, 1.0, 1.0, c_unit])
    n = max(len(_arr(obs["up"])), 1)

    def f(x):
        nll, g = neg_log_lik(x * scale, obs, settlement=settlement, avg_len=avg_len)
        return nll / n, g * scale / n

    bounds = [(lo / s if lo is not None else None, hi / s if hi is not None else None)
              for (lo, hi), s in zip(param_bounds, scale)]
    res = minimize(f, np.asarray(x0, float) / scale, jac=True, method="L-BFGS-B",
                   bounds=bounds, **kw)
    res.x = res.x * scale
    return res
