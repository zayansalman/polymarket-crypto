"""Tests for the fast-feed -> price_the_copy adapter (#182)."""

from __future__ import annotations

from polymarket_bot.pairarb.mirror import price_the_copy, trade_dict_from_fast_fill


def test_trade_dict_from_fast_fill_maps_resolved_tuple_to_expected_keys():
    d = trade_dict_from_fast_fill(
        price=0.42,
        shares=7.5,
        tx_hash="0xdead",
        resolved=("btc-updown-5m-1786705500", "Up", "0xcondition"),
        now=1786705520,
    )
    assert d == {
        "price": 0.42,
        "size": 7.5,
        "timestamp": 1786705520,
        "outcome": "Up",
        "slug": "btc-updown-5m-1786705500",
        "conditionId": "0xcondition",
        "transactionHash": "0xdead",
    }


def test_trade_dict_from_fast_fill_feeds_price_the_copy_directly():
    """The adapter's whole point: its output must be consumable as-is."""
    d = trade_dict_from_fast_fill(
        price=0.30,
        shares=10.0,
        tx_hash="0xabc",
        resolved=("doge-updown-5m-1786705500", "Down", "0xcond"),
        now=1786705522,
    )
    asks = [(0.32, 20.0)]
    fill = price_the_copy(d, asks, max_shares=10.0, min_shares=5.0)
    assert fill is not None
    assert fill.window_slug == "doge-updown-5m-1786705500"
    assert fill.outcome == "Down"
    assert fill.condition_id == "0xcond"
    assert fill.their_price == 0.30
    assert fill.their_ts == 1786705522
