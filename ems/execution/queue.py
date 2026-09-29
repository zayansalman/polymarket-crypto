"""Queue maths for resting orders: the depth ahead of an order, and how the trade tape fills it.

Pure: no I/O. Used by every strategy that rests paper orders, so one fill model serves them all.

How a paper order fills
-----------------------
The authority is the venue's public trade tape (``data-api /trades?market=<id>``, read by
``ems.execution.tape.read_fill_tape``). Checked live on 2026-09-22, 2026-09-27 and 2026-09-29:

- ``takerOnly=true`` lists one record per taker order, at the size-weighted average price of
  every level it swept. ``takerOnly=false`` lists the same records plus one record per maker
  order each one traded with, at that maker's own price, so only it shows the levels a sweep
  reached. No field tells the two kinds apart: a maker record is one that is not in the taker
  list, and a trade's maker records add up to its size (1,216 of 1,216 checked).
- So each trade reaches this model as prints: its maker records, one per price level, best
  level first, at the taker's second; or, where they could not all be read (the combined list
  is about 2.8 times as long and pages back only about 10,500 records), the taker record alone
  at its average price. Never both.
- A record's price is its notional over its size, with the size rounded up to 1e-6 of a share,
  so a record at one price level can land up to 1e-6 off it (0.2399999981 for 0.24), maker
  records too. Prices are rounded to 5 decimals first: a real sweep's average sits at least
  1e-5 off the 0.001 tick grid.
- The venue's book for one outcome already contains the mirror of the other (an Up bid at 0.14
  is also shown as a Down ask at 0.86). Every print is a sale into the bids of exactly one
  outcome: a taker SELLING an outcome at p, or a maker BUYING it at p, sells into its bids at
  p; a taker BUYING the other outcome at q, or a maker SELLING it at q, is a sale into our
  outcome's bids at 1 - q. By the same mirror, our resting SELL of a token at s is a bid for
  the other outcome at 1 - s: it fills when a taker buys our token at s or more, or sells the
  other token at 1 - s or less.
- The tape runs minutes behind (2-5 minutes seen, arriving in batches), and replies can come
  from copies at different points in time.

The depth ahead of an order is kept price level by price level: what was displayed at its
price or better when it was placed (bids for a buy, asks for a sell). A print at q, in the
terms of the bids it sold into:

- At any price, it proves nothing was bid above q when it traded (price priority): the depth
  still displayed above q traded or was pulled, and is gone.
- Above our price, it then uses up the depth displayed at q, and never fills us: trades above
  our price move us up the queue.
- At our price, it uses up the depth at our price, and what is left of it is ours, up to the
  order's size: ``filled = min(size, max(0, crossed - queue_ahead))``, where ``queue_ahead``
  is the depth displayed at our own price.
- Below our price, it proves our level was empty when it traded, whether the depth displayed
  ahead of us traded or was pulled: a real order there has filled. It fills the rest of the
  order, up to what is left of the print, and the depth ahead is gone. These are the fills
  made as the price runs through the bid, the ones that lose money; waiting for the displayed
  depth to trade would miss them.
- A whole trade read at its average price (``TapePrint.averaged``) is a sweep whose split
  across levels is unknown. Its lowest level is at or below its average, so the rules above
  still clear the depth; but below our price it fills nothing, since how much of it reached
  our price is unknown, and part of it may have traded above us. At our price it counts in
  full, which is right for a trade at one level (a sweep's average sits off the tick grid) and
  can over-count only a sweep whose average lands exactly on our price. Fills from such
  prints may be over- or under-counted, and the card says so.

When several of our own orders sit on one outcome's bids, a print reaches the highest first and
a lower one sees only what the higher ones did not take: our paper orders are not in the real
book, so one print must not fill two of them beyond its size (:func:`allocate_fills`). The fill
is stamped with the time of the print that reached it, not the time we noticed.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

SIDES = ("Up", "Down")

Level = tuple[float, float]  # (price, shares)

_PRICE_EPS = 1e-9
_SHARES_EPS = 1e-9
# A record's price is rounded to this many decimals before it is compared: that puts a price
# a hair off its level back on it, and keeps a sweep's average on the same side of every level.
_TAPE_PRICE_DECIMALS = 5


def other_side(side: str) -> str:
    return "Down" if side == "Up" else "Up"


def _better_or_equal(order_side: str, px: float, price: float) -> bool:
    """True if a displayed level at ``px`` is at our price or better, so it is ahead of us:
    a bid at or above our buy, an ask at or below our sell."""
    if order_side == "BUY":
        return px >= price - _PRICE_EPS
    return px <= price + _PRICE_EPS


def ahead_of(order_side: str, price: float, levels: Iterable[Sequence[float]]) -> tuple[Level, ...]:
    """The displayed price levels ahead of a new order at ``price``, best first.

    ``levels`` are (price, shares) pairs from the side of the book the order rests on (bids
    for a BUY, asks for a SELL), in any order; levels worse than ours are behind us and are
    dropped. Raises ValueError for a level that is not a price and a size.
    """
    out: list[Level] = []
    for level in levels:
        px, size = float(level[0]), float(level[1])
        if not (math.isfinite(px) and 0.0 <= px <= 1.0 and math.isfinite(size) and size >= 0.0):
            raise ValueError(f"a price level must be a price and a size, got {tuple(level)!r}")
        if size > 0.0 and _better_or_equal(order_side, px, price):
            out.append((px, size))
    out.sort(key=lambda lv: -lv[0] if order_side == "BUY" else lv[0])
    return tuple(out)


def queue_ahead(levels: Iterable[tuple[float, float]], price: float,
                order_side: str = "BUY") -> float:
    """Displayed shares at ``price`` or better: the depth a new order there joins behind.
    ``levels`` are (price, shares) pairs from the side it rests on (bids for a buy, asks for a
    sell)."""
    return sum(size for _, size in ahead_of(order_side, price, levels))


@dataclass(frozen=True)
class TapePrint:
    """One trade at one price: the outcome whose token was traded, and the taker's side (for a
    maker record, the side of the taker it met). ``averaged`` marks a whole trade read at its
    average price because its price levels could not be read."""

    ts: int
    outcome: str
    side: str
    size: float
    price: float
    averaged: bool = False

    def hits(self) -> tuple[str, float]:
        """The outcome whose bids this trade sold into, and the price for that outcome.

        A taker selling an outcome at p sells into its bids at p. A taker buying the other
        outcome at q is the same sale at 1 - q: the two outcomes share one book. The price is
        rounded to 5 decimals, so a record a hair off its price level counts at that level.
        """
        if self.side == "SELL":
            return self.outcome, round(self.price, _TAPE_PRICE_DECIMALS)
        return other_side(self.outcome), round(1.0 - self.price, _TAPE_PRICE_DECIMALS)


@dataclass(frozen=True)
class QueuedOrder:
    """One resting order as the fill allocation sees it, in the terms of the book it sits in.

    A buy of an outcome sits among that outcome's bids at its own price. A sell of an outcome
    at s sits among the OTHER outcome's bids at 1 - s (the mirror). ``levels`` is the displayed
    depth still ahead of it in those terms, best (highest) first, the last usually at its own
    price. ``crossed`` is the volume that has reached its price level, or traded below it, since
    it was placed and ``filled`` its shares so far. The stretch of tape it reads now is
    [flow_from, flow_to).
    """

    order_id: int
    side: str
    price: float
    shares: float
    flow_from: int
    flow_to: int
    levels: tuple[tuple[float, float], ...] = ()
    crossed: float = 0.0
    filled: float = 0.0
    placed_ts: int = 0


@dataclass(frozen=True)
class OrderFlow:
    """Where one order stands after a read. ``added`` shares came from this read, the first
    of them at ``fill_ts``; ``levels`` is the depth still ahead (book terms). ``done_ts`` is
    the time of the record that took the order to its full size in this read, if one did."""

    order_id: int
    crossed: float
    filled: float
    added: float
    fill_ts: int | None
    levels: tuple[tuple[float, float], ...]
    done_ts: int | None = None


def allocate_fills(prints: Sequence[TapePrint],
                   orders: Sequence[QueuedOrder]) -> dict[int, OrderFlow]:
    """Run the tape through our resting orders, oldest print first.

    Each print sells into one outcome's bids at price q. For each of our orders there, best
    price first: if q is below the order's price, the depth ahead of it is gone and the print
    fills it, up to what is left of the print (a whole trade at its average fills nothing).
    Otherwise the depth displayed above q is gone, and the print uses up the depth at q; if q
    is the order's price, what is left after the depth there is the order's, up to its size.
    What an order takes is gone before our next order down sees the print. A print counts only
    inside an order's own stretch of tape.
    """
    levels = {o.order_id: [[float(px), float(size)] for px, size in o.levels] for o in orders}
    crossed = {o.order_id: float(o.crossed) for o in orders}
    filled = {o.order_id: float(o.filled) for o in orders}
    fill_ts: dict[int, int] = {}
    done_ts: dict[int, int] = {}
    books: dict[str, list[QueuedOrder]] = {side: [] for side in SIDES}
    for o in orders:
        if o.side not in books:
            raise ValueError(f"order {o.order_id}: side must be one of {SIDES}")
        books[o.side].append(o)
    for book in books.values():
        book.sort(key=lambda o: (-o.price, o.placed_ts, o.order_id))

    for p in sorted(prints, key=lambda t: t.ts):
        outcome, px = p.hits()
        available = float(p.size)
        for o in books.get(outcome, ()):
            if available <= _SHARES_EPS:
                break
            if not o.flow_from <= p.ts < o.flow_to:
                continue
            ahead = levels[o.order_id]
            reach = available
            if px < o.price - _PRICE_EPS:
                # It traded below our price, so by price priority nothing was left at our price
                # or above: the depth shown ahead of us traded or was pulled, and a real order
                # here has filled. All that is left of the print reaches us.
                for level in ahead:
                    level[1] = 0.0
                if p.averaged:
                    # A whole sweep at its average: how much of it reached our price is
                    # unknown, and part of it may have traded above us, so none of it is ours.
                    continue
                crossed[o.order_id] += reach
            else:
                # By price priority nothing was bid above the print's price when it traded.
                for level in ahead:
                    if level[0] > px + _PRICE_EPS:
                        level[1] = 0.0
                # The level above our price that the print traded at.
                for level in ahead:
                    if level[0] <= o.price + _PRICE_EPS or level[0] < px - _PRICE_EPS:
                        break
                    used = min(reach, level[1])
                    level[1] -= used
                    reach -= used
                if px > o.price + _PRICE_EPS:
                    continue  # it never got down to our price
                crossed[o.order_id] += reach
                # The depth at our own price, then us.
                for level in ahead:
                    if level[0] > o.price + _PRICE_EPS:
                        continue
                    used = min(reach, level[1])
                    level[1] -= used
                    reach -= used
            take = min(reach, max(0.0, float(o.shares) - filled[o.order_id]))
            if take > _SHARES_EPS:
                filled[o.order_id] += take
                fill_ts.setdefault(o.order_id, p.ts)
                available -= take
                if filled[o.order_id] >= float(o.shares) - _SHARES_EPS:
                    done_ts.setdefault(o.order_id, p.ts)

    return {
        o.order_id: OrderFlow(
            order_id=o.order_id,
            crossed=crossed[o.order_id],
            filled=filled[o.order_id],
            added=max(0.0, filled[o.order_id] - float(o.filled)),
            fill_ts=fill_ts.get(o.order_id),
            levels=tuple((px, max(0.0, size)) for px, size in levels[o.order_id]),
            done_ts=done_ts.get(o.order_id),
        )
        for o in orders
    }


def book_terms(order_side: str, side: str, price: float,
               levels: Iterable[tuple[float, float]]) -> tuple[str, float, tuple]:
    """An order's outcome, price and depth ahead in the terms of the bids it sits among."""
    if order_side == "SELL":
        return (other_side(side), 1.0 - price,
                tuple((1.0 - float(px), float(size)) for px, size in levels))
    return side, price, tuple((float(px), float(size)) for px, size in levels)


def own_terms(order_side: str, levels: Iterable[tuple[float, float]]) -> tuple:
    """Depth ahead in book terms, back in the order's own terms (the inverse of the mirror)."""
    if order_side == "SELL":
        return tuple((round(1.0 - px, 6), size) for px, size in levels)
    return tuple((round(px, 6), size) for px, size in levels)
