"""Shared data contracts for the shadow forward-tester.

Two frozen value objects flow between the loop wiring (built separately by
the integrator) and the candidate-strategy signal functions in
:mod:`polymarket_bot.shadow.signals`:

* :class:`SnapshotView` — the immutable, per-tick market read a candidate
  strategy sees. It carries the spot/reference feed, the executable Up/Down
  asks, the model's fair Up probability, and provenance for both feed and
  quote sources. The signal functions are pure: everything they need to
  decide a would-be trade is on this object.
* :class:`ShadowSignal` — a candidate's would-be trade for this tick. The
  loop persists it to ``model_shadow_positions`` and settles it later,
  net of the Polymarket taker fee. ``None`` from a signal function means
  "no trade this tick".

Both are frozen so a snapshot cannot be mutated after a strategy inspects
it and a logged signal cannot drift before it is written to the ledger.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SnapshotView:
    """Immutable per-tick market view handed to a candidate strategy.

    Attributes:
        window_slug: The Polymarket 5-minute window identifier.
        remaining_seconds: Seconds left until the window resolves.
        spot: Current BTC spot from the settlement-aligned feed.
        reference: The window's open/reference print (resolves Up on >=).
        up_ask: Executable best ask to BUY Up, or ``None`` if no usable quote.
        down_ask: Executable best ask to BUY Down, or ``None`` if no quote.
        market_up_price: The book's mid/last Up price (used for "favored").
        fair_up: Model fair probability the window resolves Up.
        sigma_per_second: Estimated one-second volatility, or ``None``.
        drift_per_second: Estimated one-second directional drift (mean 1s
            log-return), or ``None`` when the feed cannot supply it. Paired
            with ``sigma_per_second`` to form a standardised-momentum regime.
        feed_source: Provenance label for the spot/reference feed.
        quote_source: Provenance label for the Up/Down asks.
    """

    window_slug: str
    remaining_seconds: int
    spot: float
    reference: float
    up_ask: float | None
    down_ask: float | None
    market_up_price: float
    fair_up: float
    sigma_per_second: float | None
    feed_source: str
    quote_source: str
    drift_per_second: float | None = None
    # Optional bid quotes — populated by the tick-replay harness (tools/replay_race.py)
    # for spread-guard signal evaluation; None in all live/paper/shadow paths, which
    # avoids any production-behaviour change while enabling replay-only gate testing.
    up_bid: float | None = None
    down_bid: float | None = None


@dataclass(frozen=True)
class ShadowSignal:
    """A candidate strategy's would-be trade for one tick.

    Attributes:
        side: ``'Up'`` or ``'Down'`` — the side the strategy would buy.
        entry_price: The executable ask the strategy would pay per share.
        fair_prob: Model fair probability for the chosen side.
        edge: Fair-minus-ask edge for the chosen side at entry.
        confidence: Strategy confidence in the would-be trade.
        reason: Human-readable explanation of why the trade was taken.
    """

    side: str
    entry_price: float
    fair_prob: float
    edge: float
    confidence: float
    reason: str
