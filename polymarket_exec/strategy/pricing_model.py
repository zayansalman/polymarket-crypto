"""Pricing-model probability and volatility estimation."""

from __future__ import annotations

import math
import statistics


def sigma_per_second(closes: list[float]) -> float:
    """Estimate one-second log-return volatility from *closes*.

    Parameters:
        closes: Ordered list of recent close prices (oldest first).

    Returns:
        The standard deviation of log-returns between consecutive closes,
        floored at ``0.00002`` to prevent false certainty during quiet periods.
        If fewer than two valid returns are available, the floor is returned.
    """
    returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i] > 0 and closes[i - 1] > 0
    ]
    if len(returns) < 2:
        return 0.00002
    # Floor prevents a quiet sample from producing false certainty.
    return max(statistics.stdev(returns), 0.00002)


def fair_up_probability(
    spot: float,
    reference: float,
    sigma: float,
    remaining_seconds: int,
) -> float:
    """Volatility-band probability that BTC finishes above the reference price.

    This uses a Gaussian approximation: the probability is the CDF of a
    normal distribution centred at ``log(spot/reference)`` with standard
    deviation ``sigma * sqrt(remaining_seconds)``.

    Parameters:
        spot: Current BTC spot price.
        reference: Window reference (opening) price.
        sigma: One-second volatility estimate (from :func:`sigma_per_second`).
        remaining_seconds: Seconds left in the current window.

    Returns:
        A probability in ``[0.005, 0.995]``.  Edge cases (non-positive
        inputs or zero volatility) return ``0.5``.
    """
    if spot <= 0 or reference <= 0:
        return 0.5
    denom = sigma * math.sqrt(max(remaining_seconds, 1))
    if denom <= 0:
        return 0.5
    z = math.log(spot / reference) / denom
    return min(0.995, max(0.005, 0.5 * (1 + math.erf(z / math.sqrt(2)))))
