"""The Fade 1h Momentum on 15m model, standard library only: the chance a 15m window settles Up.

A port of the research model (``tools/fade_1h_momentum_15m/model.py`` on the research branch,
sections 1, 1b, 2 and 4 of ``tasks/2026-09-21-fade-1h-momentum-on-15m.md``) for the settlement
the market actually uses: a window settles Up iff the Chainlink TWAP-60s print at its close
(the average of the price over the window's last 60 s) is at least the TWAP-60s print at its
open. That is the research's ``window_prob_up(..., settlement="twap", avg_len=AVG_60S)``. The
app has no numpy or scipy, so every special function is written out here; the unit tests
check this port against the research code to 1e-9 on about 200 cases.

Units: time in hours, sigma per sqrt(hour), drift per hour, log returns, probabilities in
(0, 1). Every function takes and returns plain floats.

The process (section 1). ``dX = [theta mu - kappa(s) (X - a)] ds + sigma dW`` with a pull
``kappa(s) = kappa0 exp(-lam s)`` (s = hour-time; lam of either sign) toward the level ``a``
set at the decision so that ``X(t) - a = M``, the stretch of the last twelve 15m candles
(:func:`stretch`). ``mu`` is the blend of the 1h market's implied drift and the spot's own
trailing hour (:func:`mu_hat_H`, :func:`blend`).

How it is evaluated (the research's choices, ported one to one):

- ``G(t, h) = int_t^{t+h} exp(-K(u, t+h)) du`` (:func:`reversion_G`). Where ``lam h <= 0.1``
  the integral is taken in the reversion-clock variable by 48-point Gauss-Legendre (exact to
  ~1e-15 there; kappa0 = 0, lam = 0 and every lam < 0 take this path). Otherwise the
  exponential-integral closed form ``G = [s(a2) - e^-K s(a1)] / lam`` with ``s(x) = e^x E1(x)``.
- The forward integral ``L(u) = int_u^end exp(-K(u, s)) ds`` is ``G`` on the reversed clock
  (``lam -> -lam``); ``B``, ``Gbar``, ``Psi`` (section 1b) are 64-point Gauss-Legendre
  integrals of the closed-form ``L`` (:func:`twap_unit_moments`).
- ``E1`` (:func:`exp1`): power series up to 1, continued fraction (modified Lentz) above.
  ``Ei`` (:func:`expi`), either sign: ``-E1(-x)`` below 0, power series up to 40, asymptotic
  series above. :func:`forward_L_ei` is the document's Ei form of ``L``, valid for either sign
  of lam, kept as an independent check of the quadrature.
- Gauss-Legendre nodes by Newton's method on the Legendre polynomials (:func:`gauss_legendre`).
- The normal distribution: ``erfc`` for the CDF (accurate in both tails), the standard
  library's ``NormalDist.inv_cdf`` for its inverse.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from functools import lru_cache
from statistics import NormalDist
from typing import NamedTuple

N_LAGS = 12
M_H_CLIP = (0.005, 0.995)  # the 1h price is clipped here before it is inverted (section 2)
PARAM_NAMES = ("theta", "kappa0", "lam", "alpha", "c")
Q = 0.25  # hours in a 15m window
AVG_60S = 1.0 / 60.0  # the TWAP-60s print averages the last 60 s
SETTLEMENT = "twap"

EULER_GAMMA = 0.5772156649015329
_LAM_H_SPLIT = 0.1  # lam * h at or below this: quadrature form; above: exponential integral
_K_TRUNC = 40.0  # the quadrature form drops z > 40 (weight e^-40 ~ 4e-18 of the integral)
_EPS_GONE = 1e-12  # hours; less than this of the average gone counts as none
_SQRT2 = math.sqrt(2.0)
_LOG_SQRT_2PI = 0.5 * math.log(2.0 * math.pi)
_NORMAL = NormalDist()

# Forecast-error moments of the two drift estimates, per sigma^2 (section 4): the Sep 17-20
# tape, all four days (step 2, ``blend.all_four_days_descriptive``). Used when the dials do
# not carry their own.
BLEND_HH = 1.4424106551197178
BLEND_LL = 1.7749869124606796
BLEND_HL = 1.0744598336803688


# --------------------------------------------------------------------------- the normal law


def ndtr(z: float) -> float:
    """Phi(z), the standard normal CDF (``erfc`` form: accurate far into either tail)."""
    return 0.5 * math.erfc(-z / _SQRT2)


def norm_pdf(z: float) -> float:
    return math.exp(-0.5 * z * z - _LOG_SQRT_2PI)


def log_ndtr(z: float) -> float:
    """ln Phi(z), finite for any finite z (asymptotic series far in the lower tail)."""
    if z > -20.0:
        return math.log(ndtr(z))
    # ln Phi(z) = -z^2/2 - ln(-z) - ln sqrt(2 pi) + ln(1 - 1/z^2 + 3/z^4 - 15/z^6 + 105/z^8)
    x = 1.0 / (z * z)
    series = 1.0 - x * (1.0 - x * (3.0 - x * (15.0 - 105.0 * x)))
    return -0.5 * z * z - math.log(-z) - _LOG_SQRT_2PI + math.log(series)


def ndtri(p: float) -> float:
    """Phi^-1(p) for 0 < p < 1."""
    return _NORMAL.inv_cdf(p)


# --------------------------------------------------------------------------- small helpers


def exprel(x: float) -> float:
    """(e^x - 1) / x, 1 at x = 0."""
    if abs(x) < 1e-5:
        return 1.0 + x * (0.5 + x * (1.0 / 6.0 + x / 24.0))
    return math.expm1(x) / x


def exprel_prime(x: float) -> float:
    """d/dx of (e^x - 1)/x, stable at 0."""
    if abs(x) < 0.5:
        acc = 0.0
        for n in range(18, 0, -1):  # sum_{n>=1} n x^(n-1) / (n+1)!, Horner from the top
            acc = acc * x + n / math.factorial(n + 1)
        return acc
    return (math.exp(x) * (x - 1.0) + 1.0) / (x * x)


@lru_cache(maxsize=8)
def gauss_legendre(n: int) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Nodes (ascending) and weights of n-point Gauss-Legendre quadrature on [-1, 1].

    Newton's method on the three-term Legendre recurrence from the asymptotic guess
    cos(pi (i - 1/4) / (n + 1/2)) (Numerical Recipes, gauleg)."""
    if n < 1:
        raise ValueError("n must be at least 1")
    nodes = [0.0] * n
    weights = [0.0] * n
    for i in range(1, (n + 1) // 2 + 1):
        z = math.cos(math.pi * (i - 0.25) / (n + 0.5))
        pp = 1.0
        for _ in range(100):
            p1, p2 = 1.0, 0.0
            for j in range(1, n + 1):
                p1, p2 = ((2.0 * j - 1.0) * z * p1 - (j - 1.0) * p2) / j, p1
            pp = n * (z * p1 - p2) / (z * z - 1.0)
            dz = p1 / pp
            z -= dz
            if abs(dz) < 1e-16:
                break
        p1, p2 = 1.0, 0.0
        for j in range(1, n + 1):
            p1, p2 = ((2.0 * j - 1.0) * z * p1 - (j - 1.0) * p2) / j, p1
        pp = n * (z * p1 - p2) / (z * z - 1.0)
        w = 2.0 / ((1.0 - z * z) * pp * pp)
        nodes[i - 1], nodes[n - i] = -z, z
        weights[i - 1] = weights[n - i] = w
    return tuple(nodes), tuple(weights)


def _unit_rule(n: int) -> tuple[tuple[float, float], ...]:
    """(node, weight) pairs of n-point Gauss-Legendre on [0, 1]."""
    xs, ws = gauss_legendre(n)
    return tuple((0.5 * (x + 1.0), 0.5 * w) for x, w in zip(xs, ws))


_GL48 = _unit_rule(48)  # the inner integral (G on the reversion clock)
_GL64 = _unit_rule(64)  # the outer integrals over the averaging window


# --------------------------------------------------------------------------- E1 and Ei


def _e1_series(x: float) -> float:
    """E1(x) = -gamma - ln x - sum_{k>=1} (-x)^k / (k k!), for 0 < x <= 1."""
    total, term = 0.0, 1.0
    for k in range(1, 60):
        term *= -x / k
        add = term / k
        total += add
        if abs(add) < 1e-18 * max(1.0, abs(total)):
            break
    return -EULER_GAMMA - math.log(x) - total


def _exp_e1_cf(x: float) -> float:
    """e^x E1(x) for x > 1: the continued fraction by the modified Lentz method (Numerical
    Recipes, expint with n = 1). Converges in under 40 steps at x = 1, fewer above."""
    tiny = 1e-300
    b = x + 1.0
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 500):
        an = -float(i * i)
        b += 2.0
        d = 1.0 / (an * d + b)
        c = b + an / c
        delta = c * d
        h *= delta
        if abs(delta - 1.0) < 1e-16:
            break
    return h


def exp1(x: float) -> float:
    """E1(x) = int_x^inf e^-t / t dt, for x > 0."""
    if not x > 0.0:
        raise ValueError(f"exp1 needs x > 0, got {x!r}")
    if x <= 1.0:
        return _e1_series(x)
    return math.exp(-x) * _exp_e1_cf(x)


def exp_e1(a: float, log_a: float | None = None) -> float:
    """s(a) = e^a E1(a) for a >= 0, from a and its log (an underflowed a is still exact).

    As the research: -gamma - ln a below 1e-100 (E1(a) = -gamma - ln a + O(a)); the asymptotic
    series (1/a) sum_{n<=24} (-1)^n n! a^-n above 50 (error < 1e-17); exact in between."""
    if log_a is None:
        log_a = math.log(a) if a > 0.0 else -math.inf
    if a < 1e-100:
        return -EULER_GAMMA - log_a
    if a > 50.0:
        inv = 1.0 / a
        acc = 1.0
        for k in range(23, -1, -1):
            acc = 1.0 - (k + 1) * inv * acc
        return acc * inv
    if a <= 1.0:
        return math.exp(a) * _e1_series(a)
    return _exp_e1_cf(a)


def expi(x: float) -> float:
    """The exponential integral Ei(x) = -PV int_{-x}^inf e^-t / t dt, for x != 0, either sign.

    x < 0: -E1(-x). 0 < x <= 40: gamma + ln x + sum x^k / (k k!) (all terms positive, so no
    cancellation). x > 40: the asymptotic series (e^x / x) sum k! / x^k, stopped at its
    smallest term (below 1e-16 relative there)."""
    if x == 0.0:
        return -math.inf
    if x < 0.0:
        return -exp1(-x)
    if x <= 40.0:
        total, term = 0.0, 1.0
        for k in range(1, 400):
            term *= x / k
            add = term / k
            total += add
            if add < 1e-17 * total:
                break
        return EULER_GAMMA + math.log(x) + total
    total, term = 1.0, 1.0
    for k in range(1, 200):
        nxt = term * k / x
        if nxt >= term:
            break
        term = nxt
        total += term
        if term < 1e-17 * total:
            break
    return math.exp(x) / x * total


# --------------------------------------------------------------------------- section 1


def reversion_K(t: float, h: float, kappa0: float, lam: float) -> float:
    """K = int_t^{t+h} kappa0 e^{-lam s} ds."""
    return kappa0 * math.exp(-lam * t) * h * exprel(-lam * h)


def reversion_G(t: float, h: float, kappa0: float, lam: float) -> float:
    """G = int_t^{t+h} exp(-int_u^{t+h} kappa) du: the momentum's route into the move over
    [t, t+h] (section 1). kappa0 = 0 gives h."""
    if lam * h <= _LAM_H_SPLIT:
        # Quadrature form: G = D int_0^1 e^{-K s} / (1 + beta s) ds, D = (e^{lam h} - 1) / lam.
        big_t = t + h
        x = lam * h
        d = h * exprel(x)
        beta = math.expm1(x)
        k2 = kappa0 * math.exp(-lam * big_t)  # kappa at the end of the interval
        big_k = k2 * d
        if big_k > _K_TRUNC:
            kk, bb, pref = _K_TRUNC, _K_TRUNC * lam / k2, _K_TRUNC / k2
        else:
            kk, bb, pref = big_k, beta, d
        return pref * sum([w * math.exp(-kk * s) / (1.0 + bb * s) for s, w in _GL48])
    # Exponential-integral form: G = [s(a2) - e^{-K} s(a1)] / lam, a_i = kappa(t_i) / lam.
    big_k = reversion_K(t, h, kappa0, lam)
    if big_k < 1e-150:  # kappa effectively 0 over the interval: the Brownian limit
        return h
    log_a1 = math.log(kappa0) - math.log(lam) - lam * t
    log_a2 = log_a1 - lam * h
    s1 = exp_e1(math.exp(log_a1), log_a1)
    s2 = exp_e1(math.exp(log_a2), log_a2)
    return (s2 - math.exp(-big_k) * s1) / lam


class UnitMoments(NamedTuple):
    """Section 1 over [t, t+h] at sigma = 1: E[R] = -A M + mu G, Var[R] = sigma^2 V1."""

    K: float
    A: float  # 1 - e^-K
    G: float
    V1: float


def ou_unit_moments(t: float, h: float, kappa0: float, lam: float) -> UnitMoments:
    """(K, 1 - e^-K, G, V/sigma^2) of section 1; V1 is G at twice the pull."""
    big_k = reversion_K(t, h, kappa0, lam)
    return UnitMoments(big_k, -math.expm1(-big_k), reversion_G(t, h, kappa0, lam),
                       reversion_G(t, h, 2.0 * kappa0, lam))


def ou_moments(t: float, h: float, kappa0: float, lam: float,
               sigma: float) -> tuple[float, float, float]:
    """(1 - e^-K, G, V) of section 1 for the return over [t, t+h]."""
    m = ou_unit_moments(t, h, kappa0, lam)
    return m.A, m.G, sigma * sigma * m.V1


# --------------------------------------------------------------------------- section 1b


def forward_L(u: float, end: float, kappa0: float, lam: float) -> float:
    """L(u) = int_u^end exp(-K(u, s)) ds: section 1's G on the reversed clock (lam -> -lam)."""
    return reversion_G(-end, end - u, kappa0, -lam)


def forward_L_ei(u: float, end: float, kappa0: float, lam: float) -> float:
    """The document's closed form of L, e^{-A w_u} [Ei(A w_u) - Ei(A w_end)] / lam with
    A = kappa0 / lam and w = e^{-lam s}; valid for either sign of lam (Ei of a negative
    argument is -E1). An independent check of :func:`forward_L`, not used on the hot path
    (the difference cancels when the pull is weak)."""
    if kappa0 == 0.0:
        return end - u
    if lam == 0.0:
        return -math.expm1(-kappa0 * (end - u)) / kappa0
    a = kappa0 / lam
    wu, we = math.exp(-lam * u), math.exp(-lam * end)
    return math.exp(-a * wu) * (expi(a * wu) - expi(a * we)) / lam


class TwapMoments(NamedTuple):
    """Section 1b at sigma = 1 for I = int_{a'}^{t+h} (X(s) - X(t)) ds, the unknown part of
    the averaging window: E[I] = -B M + mu Gbar, Var[I] = sigma^2 Psi."""

    B: float
    Gbar: float
    Psi: float
    ell: float  # hours of the average still to come
    L: float  # L(a')


def _twap_unit_moments(t: float, h: float, kappa0: float, lam: float,
                       avg_len: float) -> TwapMoments:
    end = t + h
    ell = min(h, avg_len)
    gap = h - ell
    sec1 = ou_unit_moments(t, gap, kappa0, lam)  # section 1 over [t, a']
    la = forward_L(end - ell, end, kappa0, lam)
    il = il2 = 0.0
    lnodes = []
    for s, w in _GL64:
        u = end - ell * (1.0 - s)
        ln = forward_L(u, end, kappa0, lam)
        lnodes.append((u, w))
        il += w * ln
        il2 += w * ln * ln
    il *= ell
    il2 *= ell
    if reversion_K(t, h, kappa0, lam) < 0.5:
        # B = ell - e^{-K} L(a') cancels when the pull is weak; there B = int (1 - e^{-K(t,s)}) ds
        # is taken directly on the same nodes (smooth, so exact to rounding at K < 1/2).
        b = ell * sum([w * -math.expm1(-reversion_K(t, u - t, kappa0, lam))
                       for u, w in lnodes])
    else:
        b = ell - (1.0 - sec1.A) * la
    return TwapMoments(b, sec1.G * la + il, sec1.V1 * la * la + il2, ell, la)


@lru_cache(maxsize=4096)
def twap_unit_moments(t: float, h: float, kappa0: float, lam: float,
                      avg_len: float = AVG_60S) -> TwapMoments:
    """(B, Gbar, Psi, ell, L) of section 1b; the average is over the last ``avg_len`` hours of
    the window [t, t+h] (a' = max(t, t + h - avg_len) is where its unknown part starts).

        B    = ell - e^{-K} L(a')                       (E[I] = -B M + mu Gbar)
        Gbar = G L(a') + int_{a'}^{t+h} L(u) du
        Psi  = V1 L(a')^2 + int_{a'}^{t+h} L(u)^2 du     (Var[I] = sigma^2 Psi)

    Cached: the moments depend only on the clock and the pull, so every coin in the same
    window and every pass on the same grid share them."""
    return _twap_unit_moments(float(t), float(h), float(kappa0), float(lam), float(avg_len))


def twap_moments(t: float, h: float, kappa0: float, lam: float, sigma: float,
                 avg_len: float = AVG_60S) -> tuple[float, float, float]:
    """(B, Gbar, Var[I]) of section 1b, as the research's ``twap_moments``."""
    m = twap_unit_moments(t, h, kappa0, lam, avg_len)
    return m.B, m.Gbar, sigma * sigma * m.Psi


def avg_split(h: float, avg_len: float = AVG_60S) -> tuple[float, float]:
    """(eps, ell): hours of the averaging window already gone, and still to come."""
    ell = min(h, avg_len)
    return avg_len - ell, ell


def known_part(abar: float, d: float, h: float, avg_len: float = AVG_60S) -> float:
    """eps * abar + ell * d: what the decision already knows of avg_len * (average - x0).
    abar is not read while none of the average has gone."""
    eps, ell = avg_split(h, avg_len)
    past = eps * abar if eps > _EPS_GONE else 0.0
    return past + ell * d


def settle_prob(num: float, den: float) -> float:
    """Phi(num / den); den = 0 means settled (1 if num >= 0, ties Up); NaN in, NaN out."""
    if math.isnan(num) or math.isnan(den):
        return math.nan
    if den > 0.0:
        return ndtr(num / den)
    return 1.0 if num >= 0.0 else 0.0


class ZParts(NamedTuple):
    """The numerator of section 1b's probability, split into its causes, and its scale.

    p = Phi((known + snapback + momentum) / sd):
    - known: eps abar + ell d, the leg so far (where the settlement stream is against the start
      reference, and the part of the closing average already printed);
    - snapback: -B M, the stretch of the last candles pulled back;
    - momentum: theta mu Gbar, the blended hour drift;
    - sd: sqrt(sigma^2 Psi + theta^2 v Gbar^2).
    """

    known: float
    snapback: float
    momentum: float
    sd: float
    moments: TwapMoments

    @property
    def num(self) -> float:
        return self.known + self.snapback + self.momentum

    @property
    def z(self) -> float:
        return self.num / self.sd if self.sd > 0.0 else math.copysign(math.inf, self.num)

    @property
    def p(self) -> float:
        return settle_prob(self.num, self.sd)


def twap_parts(abar: float, d: float, M: float, mu: float, v: float, t: float, h: float,
               sigma: float, theta: float, kappa0: float, lam: float,
               avg_len: float = AVG_60S) -> ZParts:
    """The parts of :func:`prob_up_twap` (see :class:`ZParts`)."""
    m = twap_unit_moments(t, h, kappa0, lam, avg_len)
    known = known_part(abar, d, h, avg_len)
    sd = math.sqrt(sigma * sigma * m.Psi + theta * theta * v * m.Gbar * m.Gbar)
    return ZParts(known, -m.B * M, theta * mu * m.Gbar, sd, m)


def prob_up_twap(abar: float, d: float, M: float, mu: float, v: float, t: float, h: float,
                 sigma: float, theta: float, kappa0: float, lam: float,
                 avg_len: float = AVG_60S) -> float:
    """Section 1b: P(average log price over the last avg_len hours of the window >= x0).

    x0 = log of the start reference (the TWAP-60s print at the window open). d = X(t) - x0, the
    price now from the start reference. abar = realised average of X over the part of the
    averaging window already gone, minus x0 (not read while that part is empty, h >= avg_len).
    t = hour-time of the decision, h = hours left, M the stretch, (mu, v) the blended drift and
    its error variance, sigma the volatility, (theta, kappa0, lam) the process dials.

        p = Phi((eps abar + ell d - B M + theta mu Gbar) / sqrt(sigma^2 Psi + theta^2 v Gbar^2))

    At h = 0 it is settled: 1 if abar >= 0 (ties Up), else 0."""
    parts = twap_parts(abar, d, M, mu, v, t, h, sigma, theta, kappa0, lam, avg_len)
    return settle_prob(parts.num, parts.sd)


def window_prob_up(y: float, M: float, mu: float, v: float, t: float, h: float, sigma: float,
                   theta: float, kappa0: float, lam: float, *, settlement: str = SETTLEMENT,
                   abar: float = math.nan, avg_len: float = AVG_60S) -> float:
    """The research's switch, TWAP settlement only: y = X(t) - x0 from the start reference."""
    if settlement != SETTLEMENT:
        raise ValueError("only the TWAP settlement is ported (the 15m market's rule)")
    return prob_up_twap(abar, y, M, mu, v, t, h, sigma, theta, kappa0, lam, avg_len)


def martingale_prob_up_twap(abar: float, d: float, h: float, sigma: float,
                            avg_len: float = AVG_60S) -> float:
    """The no-edge baseline under average-price settlement (no drift, no pull):
    Phi((eps abar + ell d) / (sigma sqrt(gap ell^2 + ell^3 / 3))), gap = h - ell."""
    eps, ell = avg_split(h, avg_len)
    gap = h - ell
    den = sigma * math.sqrt(gap * ell * ell + ell ** 3 / 3.0)
    return settle_prob(known_part(abar, d, h, avg_len), den)


def brownian_twap(h: float, avg_len: float = AVG_60S) -> tuple[float, float]:
    """(Gbar, Psi) of section 1b with no pull (kappa0 = 0): g ell + ell^2/2, g ell^2 + ell^3/3.
    The crowd's view in the fill model: a drift and Brownian noise."""
    ell = min(h, avg_len)
    gap = h - ell
    return gap * ell + 0.5 * ell * ell, gap * ell * ell + ell ** 3 / 3.0


# --------------------------------------------------------------------------- stretch


class Stretch(NamedTuple):
    M: float
    dM_dalpha: float
    dM_dc: float


def stretch_and_grad(prev_returns: Sequence[float], alpha: float, c: float) -> Stretch:
    """M = sum_j w_j c tanh(r_-j / c), w_j ~ j^-alpha normalised (element 0 = the candle just
    before the window), and its derivatives in alpha and c."""
    r = [float(x) for x in prev_returns]
    if not r:
        raise ValueError("prev_returns is empty")
    logj = [math.log(j) for j in range(1, len(r) + 1)]
    raw = [math.exp(-alpha * lj) for lj in logj]
    total = math.fsum(raw)
    w = [x / total for x in raw]
    lbar = math.fsum(wi * lj for wi, lj in zip(w, logj))
    m = da = dc = 0.0
    for wi, lj, ri in zip(w, logj, r):
        u = ri / c
        th = math.tanh(u)
        g = c * th
        m += wi * g
        da += wi * (lbar - lj) * g
        dc += wi * (th - u * (1.0 - th * th))
    return Stretch(m, da, dc)


def stretch(prev_returns: Sequence[float], alpha: float, c: float) -> float:
    """The stretch M (section 1)."""
    return stretch_and_grad(prev_returns, alpha, c).M


# --------------------------------------------------------------------------- sections 2 and 4


def implied_drift(m: float, x: float, sigma: float, time_left: float) -> float:
    """Drift per hour at which a binary priced m, move so far x, is fair: (s sqrt(T) z - x)/T."""
    z = ndtri(min(max(m, M_H_CLIP[0]), M_H_CLIP[1]))
    return (sigma * math.sqrt(time_left) * z - x) / time_left


def mu_hat_H(m_H: float, x_t: float, sigma: float, t: float) -> float:
    """Section 2: the drift the 1h market price implies for the rest of the hour."""
    return implied_drift(m_H, x_t, sigma, 1.0 - t)


def blend(mu_H: float, mu_L: float, vH: float, vL: float, cHL: float) -> tuple[float, float]:
    """Section 4's minimum-variance blend -> (mu, v); NaN mu_H (no 1h market) -> spot alone."""
    if math.isnan(mu_H):
        return mu_L, vL
    den = vH + vL - 2.0 * cHL
    if den > 1e-300:
        w = (vL - cHL) / den
        v = (vH * vL - cHL * cHL) / den
    else:  # identical errors
        w, v = 0.5, vH
    return w * mu_H + (1.0 - w) * mu_L, v


def blend_weight(vH: float, vL: float, cHL: float) -> float:
    """w_H of section 4: the weight on the 1h market's drift."""
    den = vH + vL - 2.0 * cHL
    return (vL - cHL) / den if den > 1e-300 else 0.5
