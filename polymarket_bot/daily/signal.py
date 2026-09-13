"""Fair-value scoring for the daily altcoin scanner.

Reuses :func:`polymarket_bot.strategy.signal_from_executable_edges` verbatim
(already asset/timeframe-agnostic — every 5m shadow model in
:mod:`polymarket_bot.shadow.signals` reuses it too). Two pieces do NOT carry
over from :mod:`polymarket_bot.strategy`, both because this market family's
own rules differ from the BTC 5m family's:

* The volatility/drift *estimator* — ``sigma_per_second``/``drift_per_second``
  fit off ~90 seconds of 1-second klines, appropriate to a 5-minute window's
  microstructure noise, not the diffusive volatility that should project
  over 24h. This module estimates sigma/drift from daily closes instead.
* The tie convention — checked this family's own resolution text (Gamma
  ``description``): "If the final Close price for both candles is exactly
  equal, this market will resolve 50-50." The BTC 5m family instead credits
  an exact tie entirely to Up, which is WHY ``strategy.fair_up_probability``
  adds a discretized tie-mass term on top of the plain log-normal CDF — it
  has to shift probability mass away from the natural continuous split,
  toward Up. A 50-50 tie rule needs no such correction: since the caller
  always derives the other side as ``1 - fair_up`` (never scores Down
  independently), a plain symmetric CDF ALREADY gives both sides their
  natural 50/50 share of the tie at ``z=0`` with no adjustment — adding
  half the BTC formula's tie term here would actually double-book it
  (``fair_up`` gains it, and ``1 - fair_up`` loses it, a net full-``p_tie``
  distortion, not the intended even split). So :func:`fair_up_probability`
  below is the plain log-normal CDF, deliberately without a tie-mass term.
"""
from __future__ import annotations

import math

from polymarket_bot import strategy as _strategy
from polymarket_bot.daily.types import DailyMarketView, DailySignal

_SECONDS_PER_DAY = 86400.0


def fair_up_probability(
    spot: float, reference: float, sigma: float, remaining_seconds: int
) -> float:
    """Probability this window resolves Up: plain log-normal CDF, no tie term.

    Same model as ``strategy.fair_up_probability`` (Gaussian in log-price,
    std ``sigma * sqrt(remaining_seconds)``) minus its discretized tie-mass
    addition — see the module docstring for why that term doesn't apply
    under this family's 50-50 (not credit-to-Up) tie rule.
    """
    if spot <= 0 or reference <= 0:
        return 0.5
    denom = sigma * math.sqrt(max(remaining_seconds, 1))
    if denom <= 0:
        return 0.5
    z = math.log(spot / reference) / denom
    p_above = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    return min(0.995, max(0.005, p_above))


def daily_sigma_and_drift_per_second(closes: list[float]) -> tuple[float, float]:
    """Estimate per-second sigma/drift from a list of daily closes.

    ``strategy.sigma_per_second``/``drift_per_second`` compute the
    stdev/mean of consecutive log-returns — reused here on DAILY (not
    1-second) returns, then time-scaled to per-second units so the result
    is dimensionally correct for ``fair_up_probability``'s
    ``sigma * sqrt(remaining_seconds)`` denominator:

    * Volatility scales with sqrt(time) under a random walk, so
      ``sigma_per_second = sigma_per_day / sqrt(seconds_per_day)``.
    * Drift is a mean (linear in time, not sqrt), so
      ``drift_per_second = drift_per_day / seconds_per_day``.
    """
    sigma_per_day = _strategy.sigma_per_second(closes)
    drift_per_day = _strategy.drift_per_second(closes)
    sigma_per_second = sigma_per_day / math.sqrt(_SECONDS_PER_DAY)
    drift_per_second = drift_per_day / _SECONDS_PER_DAY
    return sigma_per_second, drift_per_second


def score(view: DailyMarketView, params: _strategy.StrategyParams) -> DailySignal | None:
    """Score one asset's market view; ``None`` means no qualifying trade.

    ``fair_up`` must already be set on ``view`` (by the caller, after
    computing sigma from that asset's closes) — this function only turns a
    fair probability into an executable-edge decision, mirroring the shape
    of every 5m shadow model in :mod:`polymarket_bot.shadow.signals`.
    """
    edge_up = view.fair_up - view.up_ask if view.up_ask is not None else None
    edge_down = (1.0 - view.fair_up) - view.down_ask if view.down_ask is not None else None
    side, confidence, _notional, reason = _strategy.signal_from_executable_edges(
        edge_up, edge_down, view.remaining_seconds, view.up_ask, view.down_ask, params
    )
    if side is None:
        return None
    entry_price = view.up_ask if side == "Up" else view.down_ask
    fair_prob = view.fair_up if side == "Up" else (1.0 - view.fair_up)
    edge = edge_up if side == "Up" else edge_down
    return DailySignal(
        asset=view.asset,
        side=side,
        entry_price=float(entry_price),
        fair_prob=fair_prob,
        edge=float(edge),
        confidence=confidence,
        reason=reason,
    )


def rank(signals: list[DailySignal]) -> DailySignal | None:
    """Return the single strongest qualifying signal by edge, or ``None``."""
    if not signals:
        return None
    return max(signals, key=lambda s: s.edge)
