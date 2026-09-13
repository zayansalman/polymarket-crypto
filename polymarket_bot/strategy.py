"""Shared BTC 5-minute binary strategy math."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyParams:
    min_trade_usd: float
    max_trade_usd: float
    entry_edge_min: float
    min_confidence: float
    entry_min_remaining_seconds: int = 90
    max_entry_price: float = 0.95
    min_entry_price: float = 0.05
    # Stale-model guard (issue #29): an apparent edge ABOVE this cap means
    # the model is lagging a fast market, not that the market is wrong —
    # soaked PnL by claimed edge was monotonically decreasing (4.5-7%:
    # +7% ROI; >15%: -36% to -57% ROI). Default 1.0 disables the cap.
    entry_edge_max: float = 1.0


def sigma_per_second(closes: list[float]) -> float:
    """Estimate one-second volatility from recent closes with a safety floor."""
    returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i] > 0 and closes[i - 1] > 0
    ]
    if len(returns) < 2:
        return 0.00002
    # Floor prevents a quiet sample from producing false certainty.
    return max(statistics.stdev(returns), 0.00002)


def drift_per_second(closes: list[float]) -> float:
    """Estimate one-second directional drift as the MEAN of 1s log-returns.

    The directional twin of :func:`sigma_per_second` (which is the *stdev* of
    the same returns): a positive value means spot has been rising, negative
    falling. Returns ``0.0`` when there are fewer than 2 valid returns — no
    drift estimate rather than a fabricated one — so a caller normalising by
    sigma sees a neutral (zero) regime. Unlike sigma there is no floor: zero
    drift is a meaningful, unbiased reading.
    """
    returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i] > 0 and closes[i - 1] > 0
    ]
    if len(returns) < 2:
        return 0.0
    return statistics.fmean(returns)


def fair_up_probability(
    spot: float,
    reference: float,
    sigma: float,
    remaining_seconds: int,
    print_granularity: float = 0.01,
) -> float:
    """Probability the market resolves Up: P(close > open) + P(close == open).

    Polymarket resolves Up when the Chainlink close print is **greater than
    or equal to** the open print (ties credit Up; verified from the market
    rules page, issue #21). The Gaussian CDF alone treats P(tie) = 0, but
    Chainlink prints are discrete (~2dp at $61k), so when |spot - reference|
    is small late in the window the tie mass is material and belongs to Up.

    Tie-mass approximation: the end-print distribution is treated as
    Gaussian in log-price with std ``sigma * sqrt(remaining)``; the
    probability of landing exactly on the reference print is estimated as
    the Gaussian *density* at the reference times the log-width of one
    print step (``print_granularity / reference``), capped at 0.45 so a
    degenerate sigma can never claim a near-certain tie. This is a
    first-order discretization of the continuous density — adequate
    because the width is tiny relative to the band; it is NOT an exact
    lattice-walk tie probability.

    Consequence: fair_up is strictly > 0.5 when spot == reference — a
    structural Up bias near pinned prices that the market may not price.
    """
    if spot <= 0 or reference <= 0:
        return 0.5
    denom = sigma * math.sqrt(max(remaining_seconds, 1))
    if denom <= 0:
        return 0.5
    z = math.log(spot / reference) / denom
    p_above = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    # One print step expressed in z units, then tie mass = pdf(z) * width.
    width = (print_granularity / reference) / denom
    pdf = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    p_tie = min(pdf * width, 0.45)
    return min(0.995, max(0.005, p_above + p_tie))


def confidence_from_edge(edge: float) -> float:
    return min(0.99, max(0.0, 0.50 + abs(edge) * 2.8))


def notional_from_confidence(confidence: float, params: StrategyParams) -> float:
    if confidence < params.min_confidence:
        return 0.0
    span = max(params.max_trade_usd - params.min_trade_usd, 0)
    scaled = (confidence - params.min_confidence) / max(0.99 - params.min_confidence, 0.01)
    raw = params.min_trade_usd + span * min(max(scaled, 0.0), 1.0)
    return float(round(min(max(raw, params.min_trade_usd), params.max_trade_usd)))


def signal_from_executable_edges(
    edge_up: float | None,
    edge_down: float | None,
    remaining_seconds: int,
    up_ask: float | None,
    down_ask: float | None,
    params: StrategyParams,
) -> tuple[str | None, float, float, str]:
    """Signal from edges computed against EXECUTABLE prices (issue #22).

    For a candidate BUY of side X the relevant market price is X's best
    ask, so ``edge_up = fair_up - up_best_ask`` and
    ``edge_down = (1 - fair_up) - down_best_ask``. A side without a usable
    ask (empty or crossed book) is passed as ``None`` and is not a
    candidate. Note both edges can be negative simultaneously — buying
    either side pays its half of the spread — so the candidate is the side
    with the LARGER edge, and only a positive edge above the threshold
    trades. Confidence derives from the positive part of the edge (a very
    negative edge is conviction to do nothing, not to trade).
    """
    candidates: list[tuple[str, float, float]] = []
    if edge_up is not None and up_ask is not None:
        candidates.append(("Up", edge_up, up_ask))
    if edge_down is not None and down_ask is not None:
        candidates.append(("Down", edge_down, down_ask))
    if not candidates:
        return None, 0.0, 0.0, "skip: no executable quote (book empty or crossed)"
    side, edge, entry_price = max(candidates, key=lambda c: c[1])
    confidence = min(0.99, max(0.0, 0.50 + max(edge, 0.0) * 2.8))
    if remaining_seconds <= params.entry_min_remaining_seconds:
        return None, confidence, 0.0, "skip: too close to window end"
    if edge < params.entry_edge_min or confidence < params.min_confidence:
        return None, confidence, 0.0, "skip: edge/confidence below threshold"
    if edge > params.entry_edge_max:
        # "Too good" is a warning, not an opportunity: the market has moved
        # and the model hasn't caught up. Taking these is adverse selection.
        return None, confidence, 0.0, "skip: edge above cap (stale-model guard)"
    if entry_price < params.min_entry_price or entry_price > params.max_entry_price:
        return None, confidence, 0.0, "skip: entry price too extreme for paper fill model"
    notional = notional_from_confidence(confidence, params)
    return (
        side,
        confidence,
        notional,
        f"enter {side}: executable edge {edge:+.3f} @ ask {entry_price:.3f}",
    )


def signal_from_edge(
    edge: float,
    remaining_seconds: int,
    up_price: float,
    down_price: float,
    params: StrategyParams,
) -> tuple[str | None, float, float, str]:
    """Return side, confidence, paper notional, and reason for a current tick.

    Legacy single-edge form (assumes up/down prices sum to ~1); kept for
    backtest tooling. The live signal path uses
    :func:`signal_from_executable_edges` against CLOB best asks.
    """
    confidence = confidence_from_edge(edge)
    if remaining_seconds <= params.entry_min_remaining_seconds:
        return None, confidence, 0.0, "skip: too close to window end"
    if abs(edge) < params.entry_edge_min or confidence < params.min_confidence:
        return None, confidence, 0.0, "skip: edge/confidence below threshold"
    side = "Up" if edge > 0 else "Down"
    entry_price = up_price if side == "Up" else down_price
    if entry_price < params.min_entry_price or entry_price > params.max_entry_price:
        return None, confidence, 0.0, "skip: entry price too extreme for paper fill model"
    notional = notional_from_confidence(confidence, params)
    return side, confidence, notional, f"enter {side}: edge {edge:+.3f}"
