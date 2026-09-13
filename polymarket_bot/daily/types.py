"""Shared data contracts for the daily altcoin scanner.

Mirrors :mod:`polymarket_bot.shadow.types` (``SnapshotView`` / ``ShadowSignal``)
but adds an ``asset`` field, since a single scan tick evaluates several
assets at once rather than one fixed market — the daily scanner has to
compare candidates ACROSS assets, not just decide whether to trade the one
market it was handed.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DailyMarketView:
    """Immutable per-asset market read for one scan tick.

    Attributes:
        asset: Tracked asset key (e.g. ``"sol"``), matching ``config.DAILY_ASSETS``.
        window_slug: The Polymarket daily market's slug for today.
        condition_id: The market's CTF condition id.
        up_token: Outcome-token id for "Up".
        down_token: Outcome-token id for "Down".
        binance_symbol: The Binance ticker this window resolves against
            (e.g. ``"SOLUSDT"``) — stored so settlement can recompute the
            outcome later without re-resolving the market.
        resolves_at: ISO8601 UTC timestamp of the window's actual settlement
            instant (NOT ``endDate`` necessarily coincident with it for this
            family, but is here — see ``market.py``).
        remaining_seconds: Seconds left until the window resolves.
        spot: Current spot price from Binance.
        reference: The window's open/reference price (resolves Up on >=).
        up_ask: Executable best ask to BUY Up, or ``None`` if no usable quote.
        down_ask: Executable best ask to BUY Down, or ``None`` if no quote.
        market_up_price: The book's mid/last Up price.
        fair_up: Model fair probability the window resolves Up.
        sigma_per_second: Estimated one-second volatility (diffused from a
            multi-day look-back, not a 90-second sample — see
            :mod:`polymarket_bot.daily.signal`).
        drift_per_second: Estimated one-second directional drift.
        liquidity_usd: The book's reported liquidity, used to cap paper fill
            size on thin books (DOGE/BNB are materially thinner than
            SOL/XRP/ETH — confirmed live, not assumed).
        order_min_size: The venue's minimum order size in shares for this
            market.
    """

    asset: str
    window_slug: str
    condition_id: str
    up_token: str
    down_token: str
    binance_symbol: str
    resolves_at: str
    remaining_seconds: int
    spot: float
    reference: float
    up_ask: float | None
    down_ask: float | None
    market_up_price: float
    fair_up: float
    sigma_per_second: float | None
    drift_per_second: float | None
    liquidity_usd: float | None
    order_min_size: float


@dataclass(frozen=True)
class DailySignal:
    """A would-be trade for one asset, scored for cross-asset ranking.

    Attributes:
        asset: The asset this signal was scored for.
        side: ``'Up'`` or ``'Down'``.
        entry_price: The executable ask that would be paid per share.
        fair_prob: Model fair probability for the chosen side.
        edge: Fair-minus-ask edge for the chosen side at entry.
        confidence: Strategy confidence in the would-be trade.
        reason: Human-readable explanation.
    """

    asset: str
    side: str
    entry_price: float
    fair_prob: float
    edge: float
    confidence: float
    reason: str
