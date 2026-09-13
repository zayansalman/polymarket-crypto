"""Signal composition — bridge raw edge into a fully typed :class:`Signal`."""

from __future__ import annotations


from polymarket_exec.core.types import Side, Signal, SignalAction, StrategyParams

from .sizing import confidence_from_edge, notional_from_confidence


def signal_from_edge(
    edge: float,
    remaining_seconds: int,
    up_price: float,
    down_price: float,
    params: StrategyParams,
) -> Signal:
    """Convert an *edge* value into a fully populated :class:`Signal`.

    Parameters:
        edge: ``fair_up_prob - market_up_price`` — positive means Up is
            undervalued, negative means Down is undervalued.
        remaining_seconds: Seconds until the window closes.
        up_price: Current market price for the ``Up`` outcome.
        down_price: Current market price for the ``Down`` outcome.
        params: Strategy parameters governing thresholds and sizing.

    Returns:
        A :class:`Signal` with ``action`` set to :attr:`SignalAction.SKIP`,
        :attr:`SignalAction.ENTER_UP`, or :attr:`SignalAction.ENTER_DOWN`
        depending on the edge and filter logic.
    """
    confidence = confidence_from_edge(edge)

    if remaining_seconds <= params.entry_min_remaining_seconds:
        return _skip_signal(confidence, edge, "skip: too close to window end")

    if abs(edge) < params.entry_edge_min or confidence < params.min_confidence:
        return _skip_signal(
            confidence, edge, "skip: edge/confidence below threshold"
        )

    side: Side = Side.UP if edge > 0 else Side.DOWN
    entry_price = up_price if side is Side.UP else down_price

    if entry_price < params.min_entry_price or entry_price > params.max_entry_price:
        return _skip_signal(
            confidence,
            edge,
            "skip: entry price too extreme for paper fill model",
        )

    notional = notional_from_confidence(confidence, params)

    if notional <= 0:
        return _skip_signal(confidence, edge, "skip: zero notional")

    return Signal(
        action=SignalAction.ENTER_UP if side is Side.UP else SignalAction.ENTER_DOWN,
        side=side,
        confidence=confidence,
        notional_usd=notional,
        edge=edge,
        fair_up_prob=0.0,  # populated by caller from fair_up_probability()
        reason=f"enter {side.value}: edge {edge:+.3f}",
    )


def _skip_signal(confidence: float, edge: float, reason: str) -> Signal:
    """Build a SKIP signal with the given diagnostic *reason*."""
    return Signal(
        action=SignalAction.SKIP,
        side=None,
        confidence=confidence,
        notional_usd=0.0,
        edge=edge,
        fair_up_prob=0.0,
        reason=reason,
    )
