"""Copy-trade mirror — what following a target wallet would actually cost (#182).

Runs alongside the pair quoter so the two approaches are measured on the same
windows, with the same settlement, in the same ledger. **Places no orders.**

The argument this settles: the target is a maker. It gets filled because someone
crossed the spread to hit its resting bid, which means a copier — who can only
react *after* the fill is public — must cross the spread themselves and pay the
taker fee the target avoided. So the copy is priced at **the ask we would face
now**, never at the price the target got.

Recording both prices on every row makes the cost empirical rather than
asserted: ``their_price`` is what the target paid, ``our_price`` is what the
copy costs, and the gap is the answer. If the gap turns out not to matter, this
ledger will show it.

Fee note: the copy always crosses, so it always pays ``0.07·p·(1−p)``. Maker
fills are fee-free, which is exactly the asymmetry being measured.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from polymarket_bot.shadow.fees import taker_fee_per_share

# Polymarket rejects orders below 5 shares (venue-confirmed: gamma
# ``orderMinSize`` = 5, CLOB ``min_order_size`` = 5). Note ``rewardsMinSize``
# is 50, so a copier at the floor earns no maker rebate — irrelevant here
# since a copy always crosses, but it is why the resting-quote strategy loses
# its subsidy at small capital.
MIN_ORDER_SHARES = 5.0


@dataclass(frozen=True)
class CopyFill:
    """One would-be copy of a target's trade.

    Attributes:
        window_slug: Window the target traded.
        condition_id: Market id, used for settlement.
        outcome: ``'Up'`` or ``'Down'`` — the side the target bought.
        their_price: What the target actually paid.
        our_price: The ask we would have to cross to follow them.
        size: Shares we would take, capped by displayed depth.
        fee: Taker fee we pay and they did not.
        their_ts: Target's fill timestamp.
        our_ts: When we could first have acted.
    """

    window_slug: str
    condition_id: str
    outcome: str
    their_price: float
    our_price: float
    size: float
    fee: float
    their_ts: int
    our_ts: int

    @property
    def cost_per_share(self) -> float:
        """All-in cost of the copy, including the fee they avoided."""
        return self.our_price + self.fee

    @property
    def slippage_per_share(self) -> float:
        """How far behind the target we start, per share.

        Spread paid plus fee paid. This is the number that decides whether
        copying can work at all — it is incurred before the market moves.
        """
        return self.cost_per_share - self.their_price

    def pnl(self, resolved_up: bool) -> float:
        """Realized dollars once the window resolves."""
        won = (self.outcome == "Up") == resolved_up
        payout = 1.0 if won else 0.0
        return self.size * (payout - self.cost_per_share)


def price_the_copy(
    trade: dict[str, Any],
    asks: list[tuple[float, float]],
    scale: float = 1.0,
    max_shares: float = 50.0,
    fee_rate: float = 0.07,
    min_shares: float = MIN_ORDER_SHARES,
    skip_below_min: bool = False,
    max_their_size: float | None = None,
    max_slippage: float | None = None,
) -> CopyFill | None:
    """Price a copy of ``trade`` against the book we would actually face.

    ``asks`` is the current ask ladder for the same outcome, cheapest first. We
    walk it to fill ``scale`` times the target's size, capped by ``max_shares``
    and by displayed depth — a copier cannot fill on liquidity that is not
    there, and pretending otherwise is how copy backtests invent profit.

    **The venue's 5-share floor cuts both ways at small capital.** On the 87% of
    the target's trades that are 5 shares or larger, the floor is harmless and
    we simply take less than they did. On the 13% below it, we cannot match
    their size and are forced to take *more* — amplifying whatever their
    smallest trades are. ``skip_below_min`` declines those instead, which is the
    honest choice if their small clips turn out to be probes or hedge scraps
    rather than conviction.

    Returns ``None`` when the trade is unusable, the book is empty, or the
    target's size is below the floor and ``skip_below_min`` is set.
    """
    try:
        their_price = float(trade["price"])
        their_size = float(trade["size"])
        their_ts = int(trade["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None
    outcome = str(trade.get("outcome") or "")
    if outcome not in ("Up", "Down") or their_size <= 0:
        return None
    if not asks:
        return None
    if skip_below_min and their_size < min_shares:
        return None
    if max_their_size is not None and their_size > max_their_size:
        # Decline the target's largest clips. NOTE: the size/PnL analysis that
        # motivated this filter was WITHDRAWN — it inferred outcomes from the
        # target's redemptions, and redemption is lazy (winners sit unredeemed
        # for days), so it could not distinguish a loss from an un-cashed win.
        # This ceiling is an untested operator hypothesis, not a finding.
        return None

    # Clamp to the venue floor: an order below it is unplaceable, not merely
    # small (lessons.md #85 — a sub-minimum cap silently blocked 100% of
    # entries for a full session).
    want = max(min_shares, min(their_size * scale, max_shares))
    taken = 0.0
    cost = 0.0
    for price, size in sorted(asks):
        if taken >= want:
            break
        n = min(size, want - taken)
        taken += n
        cost += n * price
    if taken < min_shares - 1e-9:
        # Displayed depth could not even cover the venue minimum, so this order
        # could not have been placed at all. Recording it as a small fill would
        # invent liquidity that was not there.
        return None

    our_price = cost / taken
    if max_slippage is not None:
        # Decline when the price has already run past the target's fill.
        #
        # CAUTION, measured 2026-08-14: tightening this guard makes the TARGET's
        # PnL on the surviving subset monotonically WORSE (+$6 -> -$52 across 124
        # fills; -$6 -> -$76 across 44). High slippage is the evidence the target
        # was RIGHT — price ran because their call was correct — so the guard
        # preferentially discards their winners and our win rate falls from 45%
        # to 16%. This is a real trade-off, not a free safety.
        if (our_price + taker_fee_per_share(our_price, fee_rate)) - their_price > max_slippage:
            return None
    return CopyFill(
        window_slug=str(trade.get("slug") or ""),
        condition_id=str(trade.get("conditionId") or ""),
        outcome=outcome,
        their_price=their_price,
        our_price=our_price,
        size=taken,
        fee=taker_fee_per_share(our_price, fee_rate),
        their_ts=their_ts,
        our_ts=their_ts,
    )


def trade_dict_from_fast_fill(
    price: float,
    shares: float,
    tx_hash: str,
    resolved: tuple[str, str, str],
    now: int,
) -> dict[str, Any]:
    """Adapt a fast-feed fill into the dict shape :func:`price_the_copy` expects.

    The activity-API trade dict and the fast feed (``feed.py``'s ``FeedFill``,
    from either onchain transport) carry different fields — this is the seam
    between them so ``price_the_copy`` stays feed-agnostic. ``resolved`` is
    ``(slug, outcome, condition_id)`` from a
    :class:`~polymarket_bot.pairarb.market_index.TokenIndex` lookup.

    Neither onchain transport carries a settlement timestamp (see ``feed.py``'s
    ``observed_lag`` note — block timestamp is not on the log), so ``now`` — the
    detection time — stands in for it. On this transport the two are seconds
    apart by construction, unlike the api feed where they can differ by tens of
    seconds; this is not false precision, it is the honest floor of what the
    transport can report.
    """
    slug, outcome, condition_id = resolved
    return {
        "price": price,
        "size": shares,
        "timestamp": now,
        "outcome": outcome,
        "slug": slug,
        "conditionId": condition_id,
        "transactionHash": tx_hash,
    }
