"""One token's order book, rebuilt from CLOB snapshots and absolute level changes.

Every ``book`` event is a full reset of that token's levels; ``price_change`` sets the
new absolute size at one price (0 removes the level). The best bid and ask are kept up
to date on each change, so ``top()`` never scans the book. ``top()`` returns an
immutable :class:`TopOfBook` that callers may hand to other threads.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from polymarket_exec.marketdata.clob_messages import BUY, SELL

Level = tuple[float, float]  # (price, size)


@dataclass(frozen=True, slots=True)
class TopOfBook:
    token_id: str
    best_bid: float | None
    best_ask: float | None
    bid_size: float | None
    ask_size: float | None
    tick_size: float | None
    last_trade_price: float | None
    server_ts_ms: int | None  # newest server timestamp applied to this book
    received_ms: int | None  # when that update arrived, on our clock

    @property
    def crossed(self) -> bool:
        """Same rule as ``polymarket_bot.paper.BookTop``: a bid above the ask."""
        return (
            self.best_bid is not None
            and self.best_ask is not None
            and self.best_bid > self.best_ask
        )


@dataclass(frozen=True, slots=True)
class LastTrade:
    price: float
    size: float
    side: str  # the taker's side
    ts_ms: int | None


def _items(levels: dict[float, float]) -> list[Level]:
    """A copy that is safe to take from another thread while the loop keeps writing."""
    for _ in range(10):
        try:
            return list(levels.items())
        except RuntimeError:  # the dict changed size mid-copy
            continue
    return []


class OrderBook:
    __slots__ = (
        "token_id", "bids", "asks", "tick_size", "last_trade_price", "last_trade",
        "server_ts_ms", "events_at_ts", "received_ms", "_best_bid", "_best_ask",
    )

    def __init__(self, token_id: str) -> None:
        self.token_id = token_id
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.tick_size: float | None = None
        self.last_trade_price: float | None = None
        self.last_trade: LastTrade | None = None
        self.server_ts_ms: int | None = None
        self.events_at_ts = 0  # events applied with exactly ``server_ts_ms``
        self.received_ms: int | None = None
        self._best_bid: float | None = None
        self._best_ask: float | None = None

    def reset(
        self,
        bids: Iterable[Level],
        asks: Iterable[Level],
        tick_size: float | None = None,
        last_trade_price: float | None = None,
        ts_ms: int | None = None,
        received_ms: int | None = None,
    ) -> None:
        """Replace every level. Levels may come in any order; empty ones are skipped.

        ``tick_size`` / ``last_trade_price`` are kept when not given: only snapshot books
        carry them.
        """
        new_bids = {price: size for price, size in bids if size > 0}
        new_asks = {price: size for price, size in asks if size > 0}
        self.bids, self.asks = new_bids, new_asks  # readers see the old or the new dict
        self._best_bid = max(new_bids) if new_bids else None
        self._best_ask = min(new_asks) if new_asks else None
        if tick_size is not None:
            self.tick_size = tick_size
        if last_trade_price is not None:
            self.last_trade_price = last_trade_price
        self._stamp(ts_ms, received_ms)

    def apply(
        self,
        side: str,
        price: float,
        size: float,
        ts_ms: int | None = None,
        received_ms: int | None = None,
    ) -> None:
        """Set the absolute ``size`` at ``price`` (``side`` BUY = bids, SELL = asks)."""
        if side == BUY:
            levels = self.bids
        elif side == SELL:
            levels = self.asks
        else:
            raise ValueError(f"unknown side {side!r}")
        if size > 0:
            levels[price] = size
            if side == BUY:
                if self._best_bid is None or price > self._best_bid:
                    self._best_bid = price
            elif self._best_ask is None or price < self._best_ask:
                self._best_ask = price
        elif levels.pop(price, None) is not None:
            if side == BUY and price == self._best_bid:
                self._best_bid = max(levels) if levels else None
            elif side == SELL and price == self._best_ask:
                self._best_ask = min(levels) if levels else None
        self._stamp(ts_ms, received_ms)

    def record_trade(
        self,
        price: float,
        size: float,
        side: str,
        ts_ms: int | None = None,
        received_ms: int | None = None,
    ) -> None:
        self.last_trade = LastTrade(price, size, side, ts_ms)
        self.last_trade_price = price
        self._stamp(ts_ms, received_ms)

    def set_tick_size(self, tick_size: float) -> bool:
        """True when the tick size actually changed (the channel repeats each change)."""
        if tick_size == self.tick_size:
            return False
        self.tick_size = tick_size
        return True

    @property
    def freshness(self) -> tuple[int, int]:
        """(newest server time, events applied at that millisecond): how far along the
        event stream this book is. Connections receive the same events in the same
        order, so this orders their books even when several events share a millisecond."""
        return (-1 if self.server_ts_ms is None else self.server_ts_ms, self.events_at_ts)

    def top(self) -> TopOfBook:
        bid, ask = self._best_bid, self._best_ask
        return TopOfBook(
            token_id=self.token_id,
            best_bid=bid,
            best_ask=ask,
            bid_size=None if bid is None else self.bids.get(bid),
            ask_size=None if ask is None else self.asks.get(ask),
            tick_size=self.tick_size,
            last_trade_price=self.last_trade_price,
            server_ts_ms=self.server_ts_ms,
            received_ms=self.received_ms,
        )

    def levels(self, side: str, n: int) -> tuple[Level, ...]:
        """The best ``n`` levels of ``side`` ("bid" | "ask"), best first. Thread-safe."""
        if side == "bid":
            ordered = sorted(_items(self.bids), reverse=True)
        elif side == "ask":
            ordered = sorted(_items(self.asks))
        else:
            raise ValueError(f"unknown side {side!r}")
        return tuple(ordered[:n]) if n > 0 else ()

    def _stamp(self, ts_ms: int | None, received_ms: int | None) -> None:
        # Server stamps step back across event types; keep the newest of each.
        if ts_ms is not None:
            if self.server_ts_ms is None or ts_ms > self.server_ts_ms:
                self.server_ts_ms = ts_ms
                self.events_at_ts = 1
            elif ts_ms == self.server_ts_ms:
                self.events_at_ts += 1
        if received_ms is not None and (self.received_ms is None or received_ms > self.received_ms):
            self.received_ms = received_ms
