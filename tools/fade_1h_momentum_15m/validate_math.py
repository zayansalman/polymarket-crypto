"""Monte Carlo check of every closed form in tasks/2026-09-21-fade-1h-momentum-on-15m.md.

Each line prints the closed form beside a simulation of the process it claims to
describe. Run: python3 tools/fade_1h_momentum_15m/validate_math.py
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import minimize_scalar
from scipy.special import exp1
from scipy.stats import norm

Phi, Phinv = norm.cdf, norm.ppf
rng = np.random.default_rng(20260921)
SIGMA = 0.005  # per sqrt(hour)
FEE = 0.07


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


def main() -> None:
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


if __name__ == "__main__":
    main()
