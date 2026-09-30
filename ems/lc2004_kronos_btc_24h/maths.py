"""lc2004-Kronos BTC 24h maths: the chance of Up from the sampled paths, and their spread.

Pure functions, with no I/O and no state. Nothing here sizes, plans or places an order: the
forecast is shown on the dashboard and the operator trades by hand (Zayan (operator),
2026-09-29). There are no price rules or thresholds here.

The chance of Up: the Laplace posterior mean
--------------------------------------------
The model samples n independent paths (``PATHS`` = 30). k of them close strictly above the
strike at the window's end. The count is strict because the market resolves Up only when
settle > strike. Treat each path as a Bernoulli draw with unknown success rate p and put a
uniform prior on p (Beta(1, 1)). The posterior is then Beta(k + 1, n - k + 1), with mean

    q_up = (k + 1) / (n + 2)            (Laplace's rule of succession)

This keeps 0/30 and 30/30 from reading as certainty (1/32 and 31/32 instead). The raw share
p_raw = k/n and its sampling error sqrt(p_raw*(1 - p_raw)/n) are recorded as well. The Laplace
mean covers only the sampling noise of 30 draws, not any error in the model itself.

The spread of the paths
-----------------------
``path_spread`` gives the 10th, 50th and 90th percentiles of the paths' final closes, and each
one minus the strike, so the card can say where the middle 80% of the paths finished against
the price the window has to beat. Percentiles interpolate linearly between the sorted closes
(the usual "linear" definition: position q*(n - 1) in the sorted list).

Source: research/kronos_lc2004_btcusdt_1h_finetune_24h_horizon/PREREG.md (Claude, 2026-09-17)
and docs/superpowers/specs/2026-09-22-lc2004-kronos-btc-24h-design.md, "The maths", steps 2
and 3 (Claude, 2026-09-22). Trimmed to the forecast alone by Claude, 2026-09-29.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# Sampled paths per forecast (research pre-registration, PREREG.md; design spec, "The model").
PATHS = 30
SPREAD_PERCENTILES = (10, 50, 90)


@dataclass(frozen=True)
class Probability:
    """What the sampled paths say about Up.

    ``k`` of ``n`` paths closed strictly above the strike. ``p_raw = k/n``,
    ``q_up = (k + 1)/(n + 2)`` (Laplace), and ``sampling_se = sqrt(p_raw*(1 - p_raw)/n)``.
    """

    k: int
    n: int
    p_raw: float
    q_up: float
    sampling_se: float


def _finite_closes(final_closes: Sequence[float], strike: float) -> list[float]:
    if not math.isfinite(strike):
        raise ValueError(f"strike must be finite, got {strike!r}")
    closes = [float(c) for c in final_closes]
    if not closes:
        raise ValueError("final_closes is empty")
    if not all(math.isfinite(c) for c in closes):
        raise ValueError("final_closes contains a non-finite value")
    return closes


def probability_up(final_closes: Sequence[float], strike: float) -> Probability:
    """Laplace probability that the window settles Up, from each path's final close.

    Counts closes strictly above ``strike``: a close equal to the strike is not Up.
    """
    closes = _finite_closes(final_closes, strike)
    n = len(closes)
    k = sum(1 for c in closes if c > strike)
    p_raw = k / n
    return Probability(
        k=k,
        n=n,
        p_raw=p_raw,
        q_up=(k + 1) / (n + 2),
        sampling_se=math.sqrt(p_raw * (1.0 - p_raw) / n),
    )


def _percentile(ordered: list[float], pct: float) -> float:
    """Linear interpolation at position ``pct/100 * (n - 1)`` of an ascending list."""
    pos = pct / 100.0 * (len(ordered) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def path_spread(final_closes: Sequence[float], strike: float) -> dict[str, float]:
    """The 10th, 50th and 90th percentiles of the final closes, and each minus the strike.

    Keys: ``p10``, ``p50``, ``p90`` (prices) and ``p10_minus_strike``, ``p50_minus_strike``,
    ``p90_minus_strike`` (dollars above the strike; negative is below it).
    """
    ordered = sorted(_finite_closes(final_closes, strike))
    out: dict[str, float] = {}
    for pct in SPREAD_PERCENTILES:
        value = _percentile(ordered, pct)
        out[f"p{pct}"] = value
        out[f"p{pct}_minus_strike"] = value - float(strike)
    return out
