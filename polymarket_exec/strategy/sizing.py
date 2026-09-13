"""Position sizing derived from signal confidence and strategy parameters."""

from __future__ import annotations

from polymarket_exec.core.types import StrategyParams


def confidence_from_edge(edge: float) -> float:
    """Map a pricing-model edge to a confidence score in ``[0.0, 0.99]``.

    The mapping is linear: ``confidence = 0.50 + |edge| * 2.8``,
    clamped to the valid range.
    """
    return min(0.99, max(0.0, 0.50 + abs(edge) * 2.8))


def notional_from_confidence(confidence: float, params: StrategyParams) -> float:
    """Scale notional exposure from *confidence* according to *params*.

    Returns:
        A USD notional value in ``[min_trade_usd, max_trade_usd]``.
        If *confidence* is below ``params.min_confidence``, returns ``0.0``.
    """
    if confidence < params.min_confidence:
        return 0.0
    span = max(params.max_trade_usd - params.min_trade_usd, 0)
    scaled = (confidence - params.min_confidence) / max(
        0.99 - params.min_confidence, 0.01
    )
    raw = params.min_trade_usd + span * min(max(scaled, 0.0), 1.0)
    return float(round(min(max(raw, params.min_trade_usd), params.max_trade_usd)))
