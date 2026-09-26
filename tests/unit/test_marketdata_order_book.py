"""Order book rebuilt from snapshots and absolute level changes; top of book stays O(1)."""
from __future__ import annotations

import dataclasses
import random

import pytest

from polymarket_exec.marketdata.order_book import LastTrade, OrderBook, TopOfBook

WIRE_BIDS = ((0.01, 2655.36), (0.02, 884.92), (0.79, 1631.88), (0.8, 282.0))  # ascending
WIRE_ASKS = ((0.99, 1487.9), (0.98, 3561.3), (0.83, 100.0), (0.82, 20.0))  # descending


def _book(**kw) -> OrderBook:
    book = OrderBook("tok")
    book.reset(WIRE_BIDS, WIRE_ASKS, tick_size=0.01, last_trade_price=0.81, ts_ms=1_000,
               received_ms=1_050, **kw)
    return book


def test_reset_from_wire_order_gives_best_levels() -> None:
    top = _book().top()
    assert top == TopOfBook("tok", 0.8, 0.82, 282.0, 20.0, 0.01, 0.81, 1_000, 1_050)
    assert top.crossed is False


def test_reset_accepts_any_level_order() -> None:
    book = OrderBook("tok")
    book.reset(reversed(WIRE_BIDS), reversed(WIRE_ASKS), ts_ms=1)
    top = book.top()
    assert (top.best_bid, top.best_ask, top.bid_size, top.ask_size) == (0.8, 0.82, 282.0, 20.0)
    assert top.tick_size is None and top.last_trade_price is None


def test_sizes_are_absolute_and_zero_removes_the_level() -> None:
    book = _book()
    book.apply("BUY", 0.8, 100.0, ts_ms=1_001)
    assert book.top().bid_size == 100.0  # replaced, not added
    book.apply("BUY", 0.8, 0.0, ts_ms=1_002)
    top = book.top()
    assert (top.best_bid, top.bid_size) == (0.79, 1631.88)
    book.apply("SELL", 0.82, 0.0, ts_ms=1_003)
    assert (book.top().best_ask, book.top().ask_size) == (0.83, 100.0)
    book.apply("SELL", 0.5, 0.0, ts_ms=1_004)  # removing a missing level is harmless
    assert book.levels("ask", 10)[0] == (0.83, 100.0)


def test_better_prices_become_the_top() -> None:
    book = _book()
    book.apply("BUY", 0.81, 5.0)
    book.apply("SELL", 0.815, 7.0)
    top = book.top()
    assert (top.best_bid, top.bid_size, top.best_ask, top.ask_size) == (0.81, 5.0, 0.815, 7.0)


def test_best_is_maintained_through_random_changes() -> None:
    rng = random.Random(7)
    book = OrderBook("tok")
    book.reset([], [])
    bids: dict[float, float] = {}
    asks: dict[float, float] = {}
    for _ in range(3000):
        side = rng.choice(("BUY", "SELL"))
        price = round(rng.randint(1, 99) / 100, 2)
        size = rng.choice((0.0, 0.0, float(rng.randint(1, 500))))
        book.apply(side, price, size)
        levels = bids if side == "BUY" else asks
        if size > 0:
            levels[price] = size
        else:
            levels.pop(price, None)
        top = book.top()
        assert top.best_bid == (max(bids) if bids else None)
        assert top.best_ask == (min(asks) if asks else None)
        assert top.bid_size == (bids[max(bids)] if bids else None)
        assert top.ask_size == (asks[min(asks)] if asks else None)


def test_a_later_book_resets_levels_but_keeps_tick_size_and_last_trade() -> None:
    book = _book()
    book.apply("BUY", 0.81, 5.0)
    book.reset([(0.5, 1.0)], [(0.6, 2.0)], ts_ms=2_000, received_ms=2_040)
    top = book.top()
    assert (top.best_bid, top.best_ask, top.tick_size, top.last_trade_price) == (
        0.5, 0.6, 0.01, 0.81)
    assert book.levels("bid", 5) == ((0.5, 1.0),)
    assert (top.server_ts_ms, top.received_ms) == (2_000, 2_040)


def test_reset_skips_empty_levels() -> None:
    book = OrderBook("tok")
    book.reset([(0.4, 0.0), (0.3, 2.0)], [(0.6, 0.0)])
    assert book.levels("bid", 5) == ((0.3, 2.0),)
    assert book.top().best_ask is None


def test_levels_are_best_first_and_limited() -> None:
    book = _book()
    assert book.levels("bid", 2) == ((0.8, 282.0), (0.79, 1631.88))
    assert book.levels("ask", 3) == ((0.82, 20.0), (0.83, 100.0), (0.98, 3561.3))
    assert len(book.levels("bid", 100)) == 4
    assert book.levels("ask", 0) == ()
    with pytest.raises(ValueError):
        book.levels("middle", 1)


def test_trades_update_last_trade_and_its_price() -> None:
    book = _book()
    book.record_trade(0.82, 12.5, "BUY", ts_ms=1_500, received_ms=1_540)
    assert book.last_trade == LastTrade(0.82, 12.5, "BUY", 1_500)
    top = book.top()
    assert (top.last_trade_price, top.server_ts_ms, top.received_ms) == (0.82, 1_500, 1_540)


def test_server_time_never_steps_back() -> None:
    book = _book()
    book.apply("BUY", 0.81, 1.0, ts_ms=900, received_ms=1_100)  # stamps are not monotonic
    assert (book.top().server_ts_ms, book.top().received_ms) == (1_000, 1_100)


def test_tick_size_change_reports_only_real_changes() -> None:
    book = _book()
    assert book.set_tick_size(0.001) is True
    assert book.set_tick_size(0.001) is False  # the channel sends each change twice
    assert book.top().tick_size == 0.001


def test_apply_rejects_an_unknown_side() -> None:
    with pytest.raises(ValueError):
        _book().apply("HOLD", 0.5, 1.0)


def test_top_is_an_immutable_snapshot() -> None:
    book = _book()
    top = book.top()
    book.apply("BUY", 0.81, 5.0)
    assert top.best_bid == 0.8
    with pytest.raises(dataclasses.FrozenInstanceError):
        top.best_bid = 0.9  # type: ignore[misc]


def test_empty_book() -> None:
    book = OrderBook("tok")
    top = book.top()
    assert (top.best_bid, top.best_ask, top.bid_size, top.ask_size) == (None, None, None, None)
    assert top.crossed is False and book.levels("bid", 3) == ()


@pytest.mark.parametrize(
    ("bid", "ask"),
    [(0.5, 0.6), (0.6, 0.5), (0.5, 0.5), (None, 0.5), (0.5, None), (None, None)],
)
def test_crossed_means_a_bid_above_the_ask(bid, ask) -> None:
    ours = TopOfBook("tok", bid, ask, None, None, None, None, None, None)
    assert ours.crossed is (bid is not None and ask is not None and bid > ask)


def test_freshness_counts_events_at_the_newest_server_time() -> None:
    book = OrderBook("tok")
    assert book.freshness == (-1, 0)
    book.reset([(0.5, 1.0)], [(0.6, 1.0)], ts_ms=100)
    assert book.freshness == (100, 1)
    book.apply("BUY", 0.5, 2.0, ts_ms=100)  # several events can share one millisecond
    book.apply("BUY", 0.5, 3.0, ts_ms=99)  # an older stamp moves nothing
    assert book.freshness == (100, 2)
    book.record_trade(0.6, 1.0, "BUY", ts_ms=101)
    assert book.freshness == (101, 1)
    book.set_tick_size(0.001)
    book.apply("SELL", 0.7, 1.0)  # no stamp
    assert book.freshness == (101, 1)
