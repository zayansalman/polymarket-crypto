"""Back-of-queue maker fill simulation and window settlement (#182).

This module is where a maker backtest either stays honest or manufactures fake
profit. A taker simulation can be truthful cheaply — the liquidity was displayed,
we would have crossed it. A *maker* simulation has to answer "would our resting
order have been hit?", which depends on queue position behind orders we cannot
observe. Every choice here is deliberately pessimistic:

* We always join the **back** of the queue at our price. ``depth_ahead`` is the
  size already resting when we posted, and it must be fully cleared by observed
  through-volume before a single share of ours fills.
* Only **displayed** size counts. Hidden or subsequently-added liquidity never
  helps us.
* Volume is attributed from the **public trade tape**, not inferred from book
  deltas, so a book that merely *moves* never counts as a fill.

If edge survives these assumptions it is more likely to be real.

Tape semantics (Polymarket): every trade prints as a BUY of some outcome, because
buying Down at ``q`` *is* selling Up at ``1 - q``. So a resting bid on outcome X at
price ``p`` is hit either by an explicit SELL of X at ``<= p`` or — the common
case — by a BUY of the complementary outcome at ``>= 1 - p``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from polymarket_bot.pairarb.types import PairOutcome, RestingOrder

# Float slop for price/size comparisons. Polymarket ticks are 0.01/0.001, so
# 1e-9 is far below any real distinction while absorbing float noise.
_EPS = 1e-9


def hits_resting_bid(trade: Mapping[str, Any], outcome: str, price: float) -> bool:
    """Whether ``trade`` would have lifted a resting bid on ``outcome`` at ``price``.

    Two representations both count:

    * an explicit ``SELL`` of ``outcome`` at or below our price, and
    * a ``BUY`` of the complementary outcome at or above ``1 - price`` — which is
      the same economic event, and how Polymarket's tape actually prints it.

    A trade strictly *better* than our price (a seller who stopped at a higher
    bid) still counts as through-volume: it consumed queue at levels ahead of
    ours, and callers attribute it against ``depth_ahead``.
    """
    t_outcome = str(trade.get("outcome") or "")
    side = str(trade.get("side") or "BUY").upper()
    try:
        t_price = float(trade["price"])
    except (KeyError, TypeError, ValueError):
        return False

    if t_outcome == outcome:
        # Explicit sell of our own outcome: hits us if it reached our price.
        return side == "SELL" and t_price <= price + _EPS
    if t_outcome and t_outcome != outcome:
        # Complementary buy == a sell of our outcome at (1 - t_price).
        return side == "BUY" and (1.0 - t_price) <= price + _EPS
    return False


def simulate_fill(
    order: RestingOrder,
    trades: Iterable[Mapping[str, Any]],
) -> RestingOrder:
    """Return ``order`` advanced by whatever the tape would have filled.

    Through-volume is accumulated from trades that (a) occurred at or after the
    order was posted and (b) reached our price. The queue ahead of us absorbs
    that volume first; only the excess fills us, capped at our own size.

    Trades before ``posted_ts`` are ignored — we were not in the book yet.
    """
    through = 0.0
    for trade in trades:
        try:
            ts = int(trade.get("timestamp", 0))
        except (TypeError, ValueError):
            continue
        if ts < order.posted_ts:
            continue
        if not hits_resting_bid(trade, order.outcome, order.price):
            continue
        try:
            through += float(trade.get("size", 0.0))
        except (TypeError, ValueError):
            continue

    ours = max(0.0, through - order.depth_ahead)
    filled = min(order.size, ours)
    if filled <= order.filled + _EPS:
        return order
    return RestingOrder(
        outcome=order.outcome,
        price=order.price,
        size=order.size,
        depth_ahead=order.depth_ahead,
        posted_ts=order.posted_ts,
        filled=filled,
    )


def vwap(executions: Iterable[tuple[float, float]]) -> tuple[float, float]:
    """Volume-weighted average price and total size over ``(price, size)`` fills.

    A quote that is re-posted as the book moves fills at several prices, so a
    single entry price is not enough to settle it. Returns ``(0.0, 0.0)`` for an
    empty or zero-size run rather than dividing by zero.
    """
    total_size = 0.0
    total_cost = 0.0
    for price, size in executions:
        if size <= 0:
            continue
        total_size += size
        total_cost += price * size
    if total_size <= _EPS:
        return 0.0, 0.0
    return total_cost / total_size, total_size


def settle_window(
    window_slug: str,
    up_filled: float,
    down_filled: float,
    up_price: float,
    down_price: float,
    resolved_up: bool,
) -> PairOutcome:
    """Settle one window into hedged pairs plus any stranded leg.

    Hedged pairs are deterministic: each costs ``up_price + down_price`` and
    redeems at exactly 1.00 regardless of outcome, with no fee because maker
    fills are not charged.

    The stranded remainder is **naked directional risk** and is settled on the
    realized outcome — never marked at par. A strategy that only shows profit
    when stranded legs are marked at par is not a real strategy, so this is the
    single most important line in the module.
    """
    pairs = min(up_filled, down_filled)
    pair_cost = up_price + down_price
    pnl = pairs * (1.0 - pair_cost)

    stranded = abs(up_filled - down_filled)
    if stranded > _EPS:
        if up_filled > down_filled:
            payout = 1.0 if resolved_up else 0.0
            pnl += stranded * (payout - up_price)
        else:
            payout = 0.0 if resolved_up else 1.0
            pnl += stranded * (payout - down_price)

    return PairOutcome(
        window_slug=window_slug,
        up_filled=up_filled,
        down_filled=down_filled,
        up_price=up_price,
        down_price=down_price,
        resolved_up=resolved_up,
        pairs=pairs,
        stranded=stranded,
        pnl=pnl,
    )
