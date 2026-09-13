"""Two-sided quote placement for the 5m Up/Down pair strategy (#182).

Pure decision logic: given both legs' books, decide whether to rest a bid on
each and at what price. No I/O.

**Rest below the market, not at it.** Joining the touch competes for a ~1c edge
against hundreds of shares of queue, because the venue's best-bid sum sits at
roughly 0.99. Resting deep competes for a much larger edge against almost no
queue, and it is what the accounts running this strategy actually do: observed
on a live DOGE window, the reference account held Up at avg 28.3c and Down at
avg 31.1c — a pair assembled for **59.4c** that redeems at $1.00.

The mechanism is that the two legs fill at *different moments*. Over a
5-minute window price swings enough that a deep Up bid is hit on a dip and a
deep Down bid is hit later on the rally. At no single instant were both bids
simultaneously that cheap — which is why a quoter that demands
``up_bid + down_bid < 1.00`` *right now* sees far less opportunity than one that
rests deep on both legs and lets volatility come to it.

So the edge test here is applied to the prices we would **post at**, not to the
current touch, and the caller is expected to hold those orders rather than
chase the book.

Sizing note: Polymarket enforces a **5-share minimum per order**, and this repo
has been bitten by that before — a dollar cap below the share floor silently
blocked 100% of entries (#85/#87 in ``tasks/lessons.md``). So the floor here is
denominated in *shares*, not dollars.
"""

from __future__ import annotations

from polymarket_bot.pairarb.types import BookSide, QuotePlan

# Polymarket rejects orders below 5 shares. Denominated in shares deliberately —
# a dollar-denominated floor inverts against price and produces unplaceable
# orders at favourites (lessons.md, #85).
MIN_ORDER_SHARES = 5.0

# Dollars below the touch to rest each leg. 0.05 is a starting point, not a
# tuned value: deep enough to clear the ~1c-spread crowd and earn real edge,
# shallow enough that a 5-minute window's volatility can still reach it. The
# shadow ledger exists to calibrate this.
DEFAULT_OFFSET = 0.05

# Minimum dollars per completed pair before quoting is worth it.
DEFAULT_MIN_EDGE = 0.005

# Never post below this. A bid at 1c is nearly free and technically shows huge
# edge, but it fills only when the outcome is already decided against us — the
# purest possible adverse selection.
MIN_QUOTE_PRICE = 0.02


def quote_price(book: BookSide, offset: float) -> float | None:
    """Price to rest at on one leg: ``offset`` below the touch, floored.

    Returns ``None`` when the leg has no bid to reference or the offset would
    push us below :data:`MIN_QUOTE_PRICE`.
    """
    if book.best_bid is None:
        return None
    price = round(book.best_bid - offset, 4)
    if price < MIN_QUOTE_PRICE:
        return None
    return price


def plan_quote(
    window_slug: str,
    up: BookSide,
    down: BookSide,
    size: float,
    min_edge: float = DEFAULT_MIN_EDGE,
    offset: float = 0.0,
) -> QuotePlan | None:
    """Decide where to rest bids on both legs, or ``None`` to stand aside.

    With ``offset = 0`` this joins the touch (the conservative baseline). With a
    positive ``offset`` it rests that far below on each leg, which is the
    strategy the reference accounts actually run.

    Returns ``None`` when either leg has no usable bid, the size is below the
    venue's share minimum, or the two posted prices do not sum to meaningfully
    less than the 1.00 a completed pair redeems for.

    There is no fee term: maker fills are fee-free on Polymarket. That asymmetry
    against the 0.07·p·(1−p) taker fee is why this is tradeable at all — the
    same pair crossed as a taker costs 1.0449.
    """
    if size < MIN_ORDER_SHARES:
        return None
    up_price = quote_price(up, offset)
    down_price = quote_price(down, offset)
    if up_price is None or down_price is None:
        return None

    edge = 1.0 - (up_price + down_price)
    if edge < min_edge:
        return None

    return QuotePlan(
        window_slug=window_slug,
        up_price=up_price,
        down_price=down_price,
        size=size,
        edge_per_pair=edge,
        up_depth_ahead=up.depth_ahead_of(up_price),
        down_depth_ahead=down.depth_ahead_of(down_price),
    )
