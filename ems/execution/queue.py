"""Queue maths for resting orders: the depth ahead of an order, and how the taker tape fills it.

Pure: no I/O. Used by every strategy that rests paper orders, so one fill model serves them all.

How a paper order fills
-----------------------
The authority is the venue's public trade tape (``data-api /trades?market=<id>&takerOnly=
true``, read by ``ems.execution.tape``): one record per taker order. Checked live on
2026-09-22:

- A taker order that sweeps several price levels is ONE record at its average price.
- The venue's book for one outcome already contains the mirror of the other (an Up bid at 0.14
  is also shown as a Down ask at 0.86). A taker BUYING the other outcome at q is therefore a
  sale into our outcome's bids at 1 - q, and a taker SELLING our outcome at p is a sale at p
  (the crossed-volume rule). Every record is a sale into the
  bids of exactly one outcome. By the same mirror, our resting SELL of a token at s is a bid
  for the other outcome at 1 - s: it fills when a taker buys our token at s or more, or sells
  the other token at 1 - s or less.
- The tape runs minutes behind (2-5 minutes seen, arriving in batches), and replies can come
  from copies at different points in time.

The depth ahead of an order is kept price level by price level: what was displayed at its
price or better when it was placed (bids for a buy, asks for a sell). A record at price q
first uses up the depth still displayed at q or better, best level first. Only a record that
gets down to our price reaches our own level; what is left of it after the depth there is
ours, up to the order's size. So trades above our price move us up the queue by using up the
levels above us, and never fill us. When several of our own orders sit on one outcome's bids,
a record reaches the highest first and a lower one sees only what the higher ones did not
take: our paper orders are not in the real book, so one record must not fill two of them
beyond its size (:func:`allocate_fills`). The fill is stamped with the time of the record that
reached it, not the time we noticed.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

SIDES = ("Up", "Down")

Level = tuple[float, float]  # (price, shares)

_PRICE_EPS = 1e-9
_SHARES_EPS = 1e-9


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
    """One taker trade: the outcome whose token was traded, and the taker's side."""

    ts: int
    outcome: str
    side: str
    size: float
    price: float

    def hits(self) -> tuple[str, float]:
        """The outcome whose bids this trade sold into, and the price for that outcome.

        A taker selling an outcome at p sells into its bids at p. A taker buying the other
        outcome at q is the same sale at 1 - q: the two outcomes share one book.
        """
        if self.side == "SELL":
            return self.outcome, self.price
        return other_side(self.outcome), 1.0 - self.price


@dataclass(frozen=True)
class QueuedOrder:
    """One resting order as the fill allocation sees it, in the terms of the book it sits in.

    A buy of an outcome sits among that outcome's bids at its own price. A sell of an outcome
    at s sits among the OTHER outcome's bids at 1 - s (the mirror). ``levels`` is the displayed
    depth still ahead of it in those terms, best (highest) first, the last usually at its own
    price. ``crossed`` is the volume that has reached its price level since it was placed and
    ``filled`` its shares so far. The stretch of tape it reads now is [flow_from, flow_to).
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
    """Run the tape through our resting orders, oldest record first.

    Each record sells into one outcome's bids at price q. For each of our orders there, best
    price first: the record uses up the depth still displayed ahead of the order at q or
    better, best level first. If q is at or below the order's price, what is left reaches its
    level, works through the depth there, and the rest is the order's, up to its size; what it
    takes is gone before our next order down sees the record. A record counts only inside an
    order's own stretch of tape.
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
            # The levels above our price that the record traded at or through.
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
