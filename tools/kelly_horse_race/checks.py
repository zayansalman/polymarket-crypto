"""Numerical checks behind tasks/2026-09-22-kelly-horse-race-research.md."""
from __future__ import annotations

import math
from statistics import NormalDist

N = NormalDist()


def fee(price: float) -> float:
    """Polymarket taker fee per share: 0.07 * p * (1 - p)."""
    return 0.07 * price * (1 - price)


def main() -> None:
    p, q = 0.80, 0.60  # our P(Up), Up price; Down price 1 - q (prices sum to $1)
    split = (p / q, (1 - p) / (1 - q))          # wealth if Up wins, if Down wins
    f = (p - q) / (1 - q)                        # binary Kelly fraction on Up
    kelly_cash = ((1 - f) + f / q, 1 - f)
    print(f"80/20 split wealth {split[0]:.4f}/{split[1]:.4f}; "
          f"Kelly {f:.2f} on Up + cash {kelly_cash[0]:.4f}/{kelly_cash[1]:.4f}")
    print(f"weighted die picks the winner {p * p + (1 - p) ** 2:.0%} vs always-Up {p:.0%}")
    print(f"Up+Down pair at 0.50/0.51 as taker costs {0.50 + 0.51 + fee(0.50) + fee(0.51):.4f}")
    print(f"80% for the next hour -> {N.cdf(N.inv_cdf(0.80) / 2):.1%} for the next 15m "
          "(same drift and vol)")
    kl = 0.55 * math.log(0.55 / 0.53) + 0.45 * math.log(0.45 / 0.47)
    print(f"learning burn-in at P=0.55 vs price 0.53: {1 / (2 * kl):.0f} windows")


if __name__ == "__main__":
    main()
