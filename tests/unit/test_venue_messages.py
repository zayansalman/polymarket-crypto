"""Venue message parsers, pinned to frames captured live on 2026-09-13."""
from __future__ import annotations

from polymarket_exec.connectors.venue_messages import (
    binance_liquidation,
    binance_perp_snapshot,
    kraken_futures_snapshot,
    kraken_futures_trades,
    kraken_spot_trades,
)

KRAKEN_SPOT_UPDATE = {
    "channel": "trade",
    "type": "update",
    "data": [
        {"symbol": "BTC/USD", "side": "buy", "price": 77313.3, "qty": 0.0084064,
         "ord_type": "limit", "trade_id": 107612614, "timestamp": "2026-09-13T20:52:59.976634Z"},
        {"symbol": "BTC/USD", "side": "sell", "price": 77313.2, "qty": 0.5,
         "ord_type": "market", "trade_id": 107612615, "timestamp": "2026-09-13T20:53:00.000000Z"},
    ],
}


def test_kraken_spot_update_yields_taker_side_trades() -> None:
    trades = kraken_spot_trades(KRAKEN_SPOT_UPDATE)
    assert trades == [
        (1789332779976, 77313.3, 0.0084064, "buy"),
        (1789332780000, 77313.2, 0.5, "sell"),
    ]


def test_kraken_spot_ignores_status_heartbeat_snapshot_and_other_symbols() -> None:
    assert kraken_spot_trades({"channel": "heartbeat"}) == []
    assert kraken_spot_trades({"channel": "status", "type": "update", "data": []}) == []
    assert kraken_spot_trades({**KRAKEN_SPOT_UPDATE, "type": "snapshot"}) == []
    eth = {"channel": "trade", "type": "update", "data": [
        {**KRAKEN_SPOT_UPDATE["data"][0], "symbol": "ETH/USD"}]}
    assert kraken_spot_trades(eth) == []


def test_kraken_futures_fill_and_skipped_frames() -> None:
    fill = {"product_id": "PF_XBTUSD", "feed": "trade", "uid": "12d69b78", "side": "buy",
            "type": "fill", "time": 1789332835217, "qty": 0.019, "price": 77320.0, "seq": 447666}
    assert kraken_futures_trades(fill) == [(1789332835217, 77320.0, 0.019, "buy")]
    snapshot = {"feed": "trade_snapshot", "product_id": "PF_XBTUSD", "trades": [fill]}
    assert kraken_futures_trades(snapshot) == []
    subscribed = {"event": "subscribed", "feed": "trade", "product_ids": ["PF_XBTUSD"]}
    assert kraken_futures_trades(subscribed) == []
    assert kraken_futures_trades({**fill, "type": "termination"}) == []


def test_binance_liquidation_parses_btc_and_skips_others() -> None:
    msg = {"e": "forceOrder", "E": 1789332900000, "o": {
        "s": "BTCUSDT", "S": "SELL", "q": "0.014", "p": "77000", "ap": "77010.5",
        "X": "FILLED", "l": "0.014", "z": "0.014", "T": 1789332899999}}
    assert binance_liquidation(msg) == (1789332899999, 77010.5, 0.014, "sell")
    assert binance_liquidation({**msg, "o": {**msg["o"], "s": "ETHUSDT"}}) is None
    assert binance_liquidation({"result": None, "id": 1}) is None


def test_binance_perp_snapshot() -> None:
    premium = {"symbol": "BTCUSDT", "markPrice": "77318.00847826", "indexPrice": "77346.00152174",
               "lastFundingRate": "0.00009166", "nextFundingTime": 1789344000000,
               "time": 1789332813000}
    snap = binance_perp_snapshot(premium, {"symbol": "BTCUSDT", "openInterest": "104994.803"},
                                 symbol="BTCUSDT")
    assert snap.venue == "binance_perp" and snap.symbol == "BTCUSDT"
    assert snap.taken_at_ms == 1789332813000 and snap.next_funding_ms == 1789344000000
    assert snap.funding_rate == 0.00009166 and snap.open_interest == 104994.803


def test_kraken_futures_snapshot_stores_relative_funding() -> None:
    tickers = {"tickers": [
        {"symbol": "PI_XBTUSD", "markPrice": 1.0},
        {"symbol": "PF_XBTUSD", "markPrice": 77322.77, "indexPrice": 77314.89,
         "fundingRate": 0.8274803189537242, "openInterest": 1907.2899},
    ]}
    snap = kraken_futures_snapshot(tickers, symbol="PF_XBTUSD", now_ms=1789332900000)
    assert snap is not None and snap.venue == "kraken_futures"
    assert abs(snap.funding_rate - 0.8274803189537242 / 77314.89) < 1e-15
    assert snap.open_interest == 1907.2899 and snap.taken_at_ms == 1789332900000
    assert kraken_futures_snapshot({"tickers": []}, symbol="PF_XBTUSD", now_ms=0) is None
