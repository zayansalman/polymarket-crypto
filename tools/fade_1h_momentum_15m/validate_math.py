"""Monte Carlo check of every closed form in tasks/2026-09-21-fade-1h-momentum-on-15m.md.

Each line prints the closed form beside a simulation of the process it claims to
describe. Section 9 (added 2026-09-22) checks section 1b, average-price settlement, with its
own seed (20260922) so sections 1-8 print exactly what they printed before. Run:
python3 tools/fade_1h_momentum_15m/validate_math.py [--only-twap]
"""
from __future__ import annotations

import sys

import numpy as np
from scipy.optimize import minimize_scalar
from scipy.special import exp1, expi, exprel, roots_legendre
from scipy.stats import norm

Phi, Phinv = norm.cdf, norm.ppf
rng = np.random.default_rng(20260921)
SIGMA = 0.005  # per sqrt(hour)
FEE = 0.07
Q = 0.25  # hours in a 15m window
AVG_60S = 1.0 / 60.0


def ou_moments(M, mu, k0, lam, t, h, sigma=SIGMA):
    """Exact mean and variance of R over [t, t+h] for dX = [mu - k0 e^{-lam s}(X - a)]ds + sigma dW."""
    a = k0 / lam
    w1, w2 = np.exp(-lam * t), np.exp(-lam * (t + h))
    e_minus_K = np.exp(-a * (w1 - w2))
    G = np.exp(a * w2) * (exp1(a * w2) - exp1(a * w1)) / lam
    V = sigma**2 * np.exp(2 * a * w2) * (exp1(2 * a * w2) - exp1(2 * a * w1)) / lam
    return -(1 - e_minus_K) * M + mu * G, V, G


def simulate_ou(M, mu, k0, lam, t, h, n=200_000, steps=2000):
    dt = h / steps
    X = np.full(n, M)
    s = t
    for _ in range(steps):
        X += (mu - k0 * np.exp(-lam * s) * X) * dt + SIGMA * np.sqrt(dt) * rng.standard_normal(n)
        s += dt
    return X - M


def first_passage(delta, nu, h, sigma=SIGMA):
    """P(min over [0,h] of nu*s + sigma*W_s <= -delta), delta > 0."""
    sh = sigma * np.sqrt(h)
    return Phi((-delta - nu * h) / sh) + np.exp(-2 * nu * delta / sigma**2) * Phi((-delta + nu * h) / sh)


# --------------------------------------------------------------------------- section 1b reference
# Written from the document's formulas, independently of model.py (the unit tests compare the two).

_GLX, _GLW = roots_legendre(64)


def _section1_unit(k0, lam, t, g):
    """(e^-K, G, V/sigma^2) of section 1 over [t, t+g] (exponential-integral closed forms)."""
    if g <= 0:
        return 1.0, 0.0, 0.0
    if k0 == 0:
        return 1.0, g, g
    if lam == 0:
        return np.exp(-k0 * g), -np.expm1(-k0 * g) / k0, -np.expm1(-2 * k0 * g) / (2 * k0)
    a = k0 / lam
    w1, w2 = np.exp(-lam * t), np.exp(-lam * (t + g))

    def e1(x):  # E1(x) = -Ei(-x); real for either sign of lam (the i*pi of E1 at x < 0 cancels in the difference)
        return -expi(-x)

    G = np.exp(a * w2) * (e1(a * w2) - e1(a * w1)) / lam
    V1 = np.exp(2 * a * w2) * (e1(2 * a * w2) - e1(2 * a * w1)) / lam
    return np.exp(-a * (w1 - w2)), G, V1


def forward_L(u, end, k0, lam):
    """L(u) = int_u^end exp(-K(u,s)) ds = e^{-A w_u} [Ei(A w_u) - Ei(A w_end)] / lam, A = k0/lam, w = e^{-lam s}."""
    u = np.asarray(u, float)
    if k0 == 0:
        return end - u
    if lam == 0:
        return -np.expm1(-k0 * (end - u)) / k0
    a = k0 / lam
    wu, we = np.exp(-lam * u), np.exp(-lam * end)
    return np.exp(-a * wu) * (expi(a * wu) - expi(a * we)) / lam


def twap_unit_moments(k0, lam, t, h, avg=Q):
    """(B, Gbar, Psi) of section 1b: E[I] = -B M + mu Gbar, Var[I] = sigma^2 Psi.

    I = int_{a'}^{t+h} (X(s) - X(t)) ds, a' = max(t, t + h - avg). 64-point Gauss-Legendre
    over u in [a', t+h] of the closed-form L(u) and L(u)^2.
    """
    end = t + h
    ell = min(h, avg)
    a1 = end - ell
    stay, G, V1 = _section1_unit(k0, lam, t, a1 - t)
    la = float(forward_L(a1, end, k0, lam))
    u = a1 + 0.5 * ell * (_GLX + 1)
    Lu = forward_L(u, end, k0, lam)
    w = 0.5 * ell * _GLW
    return ell - stay * la, G * la + w @ Lu, V1 * la**2 + w @ Lu**2


def twap_prob(abar, d, M, mu, v_mu, k0, lam, t, h, avg=Q, sigma=SIGMA):
    """Section 1b: P(average of X over the last avg hours of the window >= x0), drift ~ N(mu, v_mu)."""
    B, Gb, Psi = twap_unit_moments(k0, lam, t, h, avg)
    ell = min(h, avg)
    eps = avg - ell
    known = (eps * abar if eps > 0 else 0.0) + ell * d
    return Phi((known - B * M + mu * Gb) / np.sqrt(sigma**2 * Psi + v_mu * Gb**2))


def simulate_future_average(M, mu, v_mu, k0, lam, t, h, avg, n, gen, n_gap=200, n_avg=400,
                            sigma=SIGMA):
    """I = int_{a'}^{t+h} D ds over simulated paths of D(s) = X(s) - X(t).

    Section 1's process with the anchor set at t (X(t) - a = M) and drift mu + sqrt(v_mu) Z
    per path. Each step is the exact constant-speed OU transition with kappa at the step's
    midpoint (error O(dt^2)); I is the trapezoid rule on n_avg steps of the averaging window.
    """
    ell = min(h, avg)
    gap = h - ell
    mus = mu + np.sqrt(v_mu) * gen.standard_normal(n)
    y = np.full(n, float(M))  # X - a

    def step(y, s, dt):
        kap = k0 * np.exp(-lam * (s + dt / 2))
        return (y * np.exp(-kap * dt) + mus * dt * exprel(-kap * dt)
                + sigma * np.sqrt(dt * exprel(-2 * kap * dt)) * gen.standard_normal(n))

    s = t
    if gap > 0:
        dt = gap / n_gap
        for _ in range(n_gap):
            y = step(y, s, dt)
            s += dt
    dt = ell / n_avg
    integral = 0.5 * (y - M) * dt
    for i in range(n_avg):
        y = step(y, s, dt)
        s += dt
        integral += (y - M) * dt * (0.5 if i == n_avg - 1 else 1.0)
    return integral


# Cases: tau = fraction of the window gone, k = quarter of the hour, drift ~ N(mu, v_mu)
# (theta mu_hat and theta^2 v_hat of section 5), abar and d from the start reference x0.
TWAP_CASES = [
    dict(name="Brownian, whole window, open", tau=0, k=1, M=0.0, mu=0.004, v_mu=0.0, k0=0.0, lam=0.0,
         avg=Q, abar=np.nan, d=0.0002),
    dict(name="constant-speed OU, whole window, minute 2", tau=2 / 15, k=2, M=0.002, mu=0.004, v_mu=0.0,
         k0=3.0, lam=0.0, avg=Q, abar=0.0001, d=0.0003),
    dict(name="decaying pull (lam 4), whole window, open", tau=0, k=1, M=0.004, mu=0.004, v_mu=0.0,
         k0=3.0, lam=4.0, avg=Q, abar=np.nan, d=0.0),
    dict(name="growing pull (lam -1.6), whole window, minute 10", tau=10 / 15, k=4, M=-0.003, mu=-0.002,
         v_mu=0.002**2, k0=0.8, lam=-1.6, avg=Q, abar=0.0002, d=-0.0001),
    dict(name="strong pull (kappa0 12), whole window, minute 1", tau=1 / 15, k=1, M=-0.0015, mu=0.003,
         v_mu=0.002**2, k0=12.0, lam=0.5, avg=Q, abar=0.0, d=0.0001),
    dict(name="decaying pull, last 60 s, minute 2", tau=2 / 15, k=2, M=0.004, mu=0.004, v_mu=0.0,
         k0=3.0, lam=4.0, avg=AVG_60S, abar=np.nan, d=-0.0002),
    dict(name="growing pull, last 60 s, 30 s left", tau=14.5 / 15, k=4, M=0.001, mu=0.002, v_mu=0.003**2,
         k0=0.8, lam=-1.6, avg=AVG_60S, abar=-0.00005, d=0.00008),
    dict(name="step-1 fit (kappa0 0.345, lam -1.62), last 60 s, open", tau=0, k=3, M=0.0015, mu=-0.001,
         v_mu=0.004**2, k0=0.345, lam=-1.62, avg=AVG_60S, abar=np.nan, d=0.0003),
]


def twap_section(n=200_000) -> list[float]:
    """Section 9 of the printout; returns every z-score it printed."""
    gen = np.random.default_rng(20260922)
    zs: list[float] = []
    print("9. Average-price settlement (section 1b): closed form / quadrature vs simulation")
    print(f"   {n:,} paths per case, exact OU steps on a fine grid (200 before the averaging window, 400 in it)")
    for c in TWAP_CASES:
        t, h = (c["k"] - 1) / 4 + c["tau"] / 4, (1 - c["tau"]) / 4
        B, Gb, Psi = twap_unit_moments(c["k0"], c["lam"], t, h, c["avg"])
        mean = -B * c["M"] + c["mu"] * Gb
        var = SIGMA**2 * Psi + c["v_mu"] * Gb**2
        integ = simulate_future_average(c["M"], c["mu"], c["v_mu"], c["k0"], c["lam"], t, h, c["avg"], n, gen)
        ell = min(h, c["avg"])
        eps = c["avg"] - ell
        known = (eps * c["abar"] if eps > 0 else 0.0) + ell * c["d"]
        p = twap_prob(c["abar"], c["d"], c["M"], c["mu"], c["v_mu"], c["k0"], c["lam"], t, h, c["avg"])
        p_sim = np.mean(known + integ >= 0)
        z_m = (integ.mean() - mean) / (integ.std() / np.sqrt(n))
        z_v = (integ.var() - var) / (var * np.sqrt(2 / (n - 1)))
        z_p = (p_sim - p) / np.sqrt(p * (1 - p) / n)
        zs += [z_m, z_v, z_p]
        print(f"   {c['name']}")
        print(f"      E[I]   {mean:+.4e} sim {integ.mean():+.4e} (z {z_m:+.2f})   Var {var:.4e} sim {integ.var():.4e} (z {z_v:+.2f})"
              f"   P(Up) {p:.4f} sim {p_sim:.4f} (z {z_p:+.2f})")

    print("   Start reference: the TWAP-60s value at the open, the average over the minute before it")
    delta, mu0, steps = AVG_60S, 0.004, 120
    dt = delta / steps
    x = np.zeros(n)
    area = 0.5 * x * dt
    for i in range(steps):
        x = x + mu0 * dt + SIGMA * np.sqrt(dt) * gen.standard_normal(n)
        area += x * dt * (0.5 if i == steps - 1 else 1.0)
    offset = x - area / delta  # X(open) - x0
    m_cf, v_cf = mu0 * delta / 2, SIGMA**2 * delta / 3
    z_m = (offset.mean() - m_cf) / (offset.std() / np.sqrt(n))
    z_v = (offset.var() - v_cf) / (v_cf * np.sqrt(2 / (n - 1)))
    zs += [z_m, z_v]
    print(f"      X(open) - x0: mean {m_cf:+.3e} sim {offset.mean():+.3e} (z {z_m:+.2f}), sd {np.sqrt(v_cf):.3e}"
          f" sim {offset.std():.3e} (z of var {z_v:+.2f})")
    for avg, label in ((Q, "whole window"), (AVG_60S, "last 60 s")):
        M, k0, lam, mu = 0.0015, 0.345, -1.62, 0.002
        integ = simulate_future_average(M, mu, 0.0, k0, lam, 0.5, Q, avg, n, gen)
        ell = min(Q, avg)
        up = ell * offset + integ >= 0
        B, Gb, Psi = twap_unit_moments(k0, lam, 0.5, Q, avg)
        p = Phi((ell * offset - B * M + mu * Gb) / (SIGMA * np.sqrt(Psi)))
        p_no = Phi((-B * M + mu * Gb) / (SIGMA * np.sqrt(Psi)))  # the offset ignored: x0 taken as X(open)
        z_t = (up.mean() - p.mean()) / ((up - p).std() / np.sqrt(n))
        edges = np.quantile(p, np.linspace(0, 1, 11))
        b = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, 9)
        zb = [(up[b == j].mean() - p[b == j].mean()) / np.sqrt(np.mean(p[b == j] * (1 - p[b == j])) / (b == j).sum())
              for j in range(10)]
        zs += [z_t] + zb
        lo, hi = np.quantile(p, [0.05, 0.95])
        print(f"      {label}, open: P(Up) sim {up.mean():.4f}, mean of p given the offset {p.mean():.4f} (z {z_t:+.2f});"
              f" 10 bins of p, max |z| {max(abs(z) for z in zb):.2f}; p runs {lo:.3f}..{hi:.3f} (5-95%),"
              f" {p_no:.3f} if the offset is ignored")

    print("   Log approximation: average of the log price vs arithmetic average of the price, same paths")
    for sig in (0.005, 0.02):
        for avg, label in ((Q, "whole window"), (AVG_60S, "last 60 s")):
            steps_gap, steps_avg = (0, 600) if avg == Q else (300, 300)
            x = np.zeros(n)
            if steps_gap:
                dt = (Q - avg) / steps_gap
                for _ in range(steps_gap):
                    x = x + sig * np.sqrt(dt) * gen.standard_normal(n)
            dt = avg / steps_avg
            lx, ls = 0.5 * x * dt, 0.5 * np.exp(x) * dt
            for i in range(steps_avg):
                x = x + sig * np.sqrt(dt) * gen.standard_normal(n)
                wgt = dt * (0.5 if i == steps_avg - 1 else 1.0)
                lx += x * wgt
                ls += np.exp(x) * wgt
            up_log, up_arith = lx >= 0, ls >= avg
            k = int((up_log != up_arith).sum())
            gap = Q - avg
            var_avg = gap + avg / 3  # / sig^2
            # Jensen gap at the boundary: half the time-variance of X over the average, given the average = 0
            c = 0.5 * sig**2 * avg * (1 / 6 - avg / (45 * var_avg))
            pred = c / (sig * np.sqrt(var_avg) * np.sqrt(2 * np.pi))
            z = (k - n * pred) / np.sqrt(max(n * pred, 1.0))
            zs.append(z)
            print(f"      sigma {sig:.1%}/sqrt(h), {label}: P(Up) log {up_log.mean():.5f} arithmetic {up_arith.mean():.5f};"
                  f" differ on {k} of {n:,} paths, first-order prediction {n * pred:.1f} (z {z:+.2f})")

    print("   Limits")
    t, h = 0.3, 0.2
    Bs, Gs, Ps = twap_unit_moments(3.0, -1.2, t, h, 1e-7)
    stay, G, V1 = _section1_unit(3.0, -1.2, t, h)
    print(f"      averaging window -> 0 (1e-7 h), lam -1.2: B/ell {Bs / 1e-7:.8f} vs 1-e^-K {1 - stay:.8f};"
          f" Gbar/ell {Gs / 1e-7:.8f} vs G {G:.8f}; Psi/ell^2 {Ps / 1e-14:.8f} vs V/sigma^2 {V1:.8f}")
    B0, G0, P0 = twap_unit_moments(0.0, 0.0, t, Q, Q)
    print(f"      Brownian, whole window: Gbar {G0:.8f} = h^2/2 {Q**2 / 2:.8f}; Psi {P0:.8f} = h^3/3 {Q**3 / 3:.8f}")
    k = 3.0
    Bk, Gk, Pk = twap_unit_moments(k, 0.0, t, h, Q)
    one = -np.expm1(-k * h) / k
    print(f"      constant kappa: B {Bk:.10f} = h - (1-e^-kh)/k {h - one:.10f}; Gbar {Gk:.10f} = B/k {(h - one) / k:.10f};"
          f" Psi {Pk:.10f} = {(h - 2 * one - np.expm1(-2 * k * h) / (2 * k)) / k**2:.10f}")
    print(f"   max |z| over {len(zs)} simulation checks: {max(abs(z) for z in zs):.2f}")
    return zs


def main() -> None:
    if "--only-twap" in sys.argv:
        twap_section()
        return
    N = 400_000

    print("1. Hour binary on Brownian motion, and inverting the price back to the drift")
    mu, t, x = 0.004, 0.5, 0.002
    closed = Phi((x + mu * (1 - t)) / (SIGMA * np.sqrt(1 - t)))
    sim = np.mean(x + rng.normal(mu * (1 - t), SIGMA * np.sqrt(1 - t), N) >= 0)
    mu_back = (SIGMA * np.sqrt(1 - t) * Phinv(closed) - x) / (1 - t)
    print(f"   P(Up) closed {closed:.4f}  sim {sim:.4f}   drift recovered {mu_back:.6f} (true {mu})")

    print("2. 60c hour at :00 -> value of the first quarter")
    mu60 = SIGMA * Phinv(0.60)
    print(f"   closed {Phi(Phinv(0.60) / 2):.4f}  sim {np.mean(rng.normal(mu60 / 4, SIGMA / 2, N) >= 0):.4f}")

    print("3. Hour up 0.3% at :45, 1h still 60c -> value of the last quarter")
    x, t = 0.003, 0.75
    mu_last = (SIGMA * np.sqrt(1 - t) * Phinv(0.60) - x) / (1 - t)
    R = rng.normal(mu_last / 4, SIGMA / 2, N)
    print(f"   closed {Phi((mu_last / 4) / (SIGMA / 2)):.4f}  sim {np.mean(R >= 0):.4f}"
          f"   hour re-priced from same draws {np.mean(x + R >= 0):.4f} (must be 0.60)")

    print("4. Decaying mean reversion: exact moments (exponential integral) vs simulation")
    for M, k0, lam, t, h in [(0.004, 3.0, 4.0, 0.0, 0.25), (-0.003, 3.0, 4.0, 0.5, 0.25), (0.006, 3.0, 4.0, 0.25, 0.2)]:
        ER, V, _ = ou_moments(M, 0.004, k0, lam, t, h)
        R = simulate_ou(M, 0.004, k0, lam, t, h)
        K = k0 / lam * (np.exp(-lam * t) - np.exp(-lam * (t + h)))
        first_order = 0.004 * h - M * K
        print(f"   t={t} h={h}: mean exact {ER:+.6f} sim {R.mean():+.6f} (first-order {first_order:+.6f})"
              f" | var exact {V:.3e} sim {R.var():.3e} (Brownian {SIGMA**2 * h:.3e})")

    print("5. First passage of drifted Brownian motion (fill of a resting bid, fixed level)")
    nu, delta, h, steps, n = -0.006, 0.0015, 0.25, 3000, 200_000
    dt = h / steps
    path, hit = np.zeros(n), np.zeros(n, bool)
    for _ in range(steps):
        path += nu * dt + SIGMA * np.sqrt(dt) * rng.standard_normal(n)
        hit |= path <= -delta
    bgk = first_passage(delta + 0.5826 * SIGMA * np.sqrt(dt), nu, h)
    print(f"   continuous {first_passage(delta, nu, h):.4f}  discrete sim {hit.mean():.4f}"
          f"  continuous with Broadie-Glasserman-Kou shift {bgk:.4f}")

    print("6. Fill level moves as the window runs down: moving boundary vs fixed-level formula")
    h0, y0, muH = (1 - 2 / 15) / 4, 0.0005, 0.002
    for b in (0.60, 0.55, 0.45):
        boundary = lambda hp: SIGMA * np.sqrt(hp) * Phinv(b) - muH * hp  # noqa: E731
        steps = 3000
        dt = h0 / steps
        y, hit, hp = np.full(n, y0), np.zeros(n, bool), h0
        for _ in range(steps):
            y += SIGMA * np.sqrt(dt) * rng.standard_normal(n)
            hp -= dt
            hit |= y <= boundary(max(hp, 1e-9))
        fixed = first_passage(y0 - boundary(h0), 0.0, h0)
        now = Phi((y0 + muH * h0) / (SIGMA * np.sqrt(h0)))
        print(f"   bid {b:.2f} (market {now:.3f}): moving boundary sim {hit.mean():.4f}  fixed-level formula {fixed:.4f}")

    print("7. Taker break-even price (quadratic)")
    for p in (0.55, 0.60, 0.70):
        a = ((1 + FEE) - np.sqrt((1 + FEE) ** 2 - 4 * FEE * p)) / (2 * FEE)
        print(f"   p={p}: a*={a:.4f}  EV at a* = {p - a - FEE * a * (1 - a):+.1e}")

    print("8. Kelly fraction with the taker fee")
    p, a = 0.62, 0.55
    c = FEE * a * (1 - a)
    analytic = (p - a - c) / (1 - a - c)
    growth = lambda f: -(p * np.log(1 + f * (1 - a - c) / (a + c)) + (1 - p) * np.log(1 - f))  # noqa: E731
    numeric = minimize_scalar(growth, bounds=(0, 0.99), method="bounded").x
    print(f"   (p-a-c)/(1-a-c) = {analytic:.4f}  argmax E[log wealth] = {numeric:.4f}"
          f"  (old (p-a-c)/(1-a) = {(p - a - c) / (1 - a):.4f})")

    twap_section()


if __name__ == "__main__":
    main()
