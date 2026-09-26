"""CLOB market-channel parsers: every live event type, the snapshot array and the text frames."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ems.marketdata import clob_messages as cm

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "marketdata"
UP = "48347891018346333616777312811599713638650075692149435329595052184345334860240"
DOWN = "43094655420909802468126769661333866266512911139677430764258029764666702009377"
MARKET = "0x607e1f5bb3d34a6588dae7410afd16ab9e25e84cbe22bf1747f5bbb01570d026"


def _frame(name: str) -> str:
    return (FIXTURES / name).read_text()


def _one(name: str):
    events = cm.parse_frame(_frame(name))
    assert len(events) == 1
    return events[0]


def test_snapshot_array_is_one_book_per_token_with_tick_size_and_last_trade() -> None:
    events = cm.parse_frame(_frame("clob_book_snapshot_array.json"))
    assert [type(e) for e in events] == [cm.BookEvent, cm.BookEvent]
    up, down = events
    assert (up.asset_id, down.asset_id) == (UP, DOWN)
    assert up.market == MARKET and up.snapshot is True
    assert up.ts_ms == 1_789_554_460_858 and isinstance(up.ts_ms, int)
    assert up.tick_size == 0.01 and up.last_trade_price == 0.81
    assert up.hash == "a8720b85e39b868685d24c437c97a4caf5504b09"
    # Levels keep the wire order: bids ascending, asks descending (best = last).
    assert up.bids[-1] == (0.8, 282.0) and up.bids[0] == (0.01, 2655.36)
    assert up.asks[-1] == (0.82, 20.0) and up.asks[0] == (0.99, 1487.9)
    assert isinstance(up.bids, tuple) and isinstance(up.bids[0], tuple)


def test_later_book_event_has_no_tick_size_or_last_trade() -> None:
    book = _one("clob_book.json")
    assert isinstance(book, cm.BookEvent)
    assert (book.asset_id, book.snapshot) == (UP, False)
    assert book.tick_size is None and book.last_trade_price is None
    assert book.bids[-1] == (0.79, 1571.16) and book.asks[-1] == (0.8, 370.0)
    assert book.ts_ms == 1_789_554_461_089


def test_price_change_has_both_mirrored_entries() -> None:
    ev = _one("clob_price_change.json")
    assert isinstance(ev, cm.PriceChangeEvent)
    assert (ev.market, ev.ts_ms) == (MARKET, 1_789_554_460_854)
    first, second = ev.changes
    assert first == cm.LevelChange(
        asset_id=DOWN, price=0.16, size=100.0, side="BUY", best_bid=0.18, best_ask=0.19,
        hash="1ddb10f381011c887b7b74a580c3cc33caba39f3",
    )
    assert (second.asset_id, second.price, second.side) == (UP, 0.84, "SELL")
    assert (second.best_bid, second.best_ask) == (0.81, 0.82)


def test_best_bid_ask_last_trade_and_tick_size() -> None:
    bba = _one("clob_best_bid_ask.json")
    assert bba == cm.BestBidAskEvent(MARKET, UP, 0.8, 0.82, 0.02, 1_789_554_460_858)
    trade = _one("clob_last_trade_price.json")
    assert isinstance(trade, cm.LastTradeEvent)
    assert (trade.asset_id, trade.price, trade.size, trade.side, trade.ts_ms) == (
        DOWN, 0.19, 10.526316, "BUY", 1_789_554_460_868)
    assert trade.transaction_hash.startswith("0x")
    tick = _one("clob_tick_size_change.json")
    assert isinstance(tick, cm.TickSizeEvent)
    assert (tick.old_tick_size, tick.new_tick_size, tick.ts_ms) == (0.01, 0.001, 1_789_554_926_589)


def test_new_market_keeps_slug_tokens_in_outcome_order() -> None:
    ev = _one("clob_new_market.json")
    assert isinstance(ev, cm.NewMarketEvent)
    assert ev.slug == "doge-updown-5m-1789640400"
    assert ev.outcomes == ("Up", "Down")
    assert ev.token_ids == (
        "928418278557132166815334280270897925187552104377290215935342525292646419327",
        "74034782249289360232835014759878476192308275296302561409923515513263342883075",
    )
    assert ev.condition_id == "0x44c5dcfb5c17e7e1367d62ba50dc6e581489371674ec1229ba5d3ffca9e4ab60"
    assert ev.active is False and ev.tick_size == 0.01
    assert ev.fee_rate == 0.07 and ev.ts_ms == 1_789_554_484_390
    assert ev.question.startswith("Dogecoin Up or Down")


def test_new_market_falls_back_to_assets_ids() -> None:
    raw = json.loads(_frame("clob_new_market.json"))
    del raw["clob_token_ids"]
    ev = cm.parse_frame(json.dumps(raw))[0]
    assert isinstance(ev, cm.NewMarketEvent) and len(ev.token_ids) == 2


def test_market_resolved() -> None:
    ev = _one("clob_market_resolved.json")
    assert isinstance(ev, cm.MarketResolvedEvent)
    assert ev.winning_outcome == "Up"
    assert ev.winning_asset_id == ev.token_ids[0]
    assert len(ev.token_ids) == 2 and "5M" in ev.tags
    assert ev.ts_ms == 1_789_555_023_725 and ev.id == "4582819"


def test_text_frames_are_typed() -> None:
    frames = json.loads(_frame("clob_text_frames.json"))
    kinds = [cm.parse_frame(f) for f in frames]
    assert [k[0].kind for k in kinds] == [
        cm.PONG, cm.NO_NEW_ASSETS, cm.INVALID_MESSAGE, cm.INVALID_OPERATION,
        cm.EMPTY_SNAPSHOT, cm.EMPTY_SNAPSHOT,
    ]
    assert all(len(k) == 1 and isinstance(k[0], cm.TextFrame) for k in kinds)
    other = cm.parse_frame("SOMETHING NEW")
    assert other == [cm.TextFrame(cm.OTHER_TEXT, "SOMETHING NEW")]


@pytest.mark.parametrize(
    "text",
    [
        '{"event_type": "brand_new_thing", "market": "0x1"}',  # unknown type
        '{"market": "0x1"}',  # no event_type
        '{"event_type": "price_change", "market": "0x1"}',  # malformed known type
        '{"event_type": "book", "asset_id": "1", "bids": [{"price": "x"}], "asks": []}',
        '{"not json',
        "[1]",  # an array item that is not an object
    ],
)
def test_unknown_or_malformed_frames_never_raise(text: str) -> None:
    events = cm.parse_frame(text)
    assert len(events) == 1 and isinstance(events[0], cm.Unknown)


def test_price_change_side_must_be_buy_or_sell() -> None:
    raw = json.loads(_frame("clob_price_change.json"))
    raw["price_changes"][0]["side"] = "HOLD"
    assert isinstance(cm.parse_frame(json.dumps(raw))[0], cm.Unknown)


def test_array_frame_parses_each_book_and_skips_bad_ones() -> None:
    books = json.loads(_frame("clob_book_snapshot_array.json"))
    events = cm.parse_frame(json.dumps([books[0], {"event_type": "book"}, books[1]]))
    assert [type(e) for e in events] == [cm.BookEvent, cm.Unknown, cm.BookEvent]
    assert all(e.snapshot for e in events if isinstance(e, cm.BookEvent))


def test_empty_best_side_reads_zero_or_one_as_sent() -> None:
    raw = json.loads(_frame("clob_price_change.json"))
    raw["price_changes"][0].update(best_bid="0", best_ask="1", size="0")
    change = cm.parse_frame(json.dumps(raw))[0].changes[0]
    assert (change.best_bid, change.best_ask, change.size) == (0.0, 1.0, 0.0)
