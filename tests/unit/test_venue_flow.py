"""Venue flow bars: closed-candle kline parsing and live-trade hour aggregation."""
from __future__ import annotations

from polymarket_exec.connectors.venue_flow import (
    HOUR_MS,
    HourAggregator,
    HourBar,
    hour_floor_ms,
    parse_binance_klines,
)

H0 = 1_789_326_000_000  # an hour boundary (2026-09-13 19:00 UTC)


def _kline(open_ms: int, o: float, h: float, lo: float, c: float, v: float, tb: float) -> list:
    return [open_ms, str(o), str(h), str(lo), str(c), str(v), open_ms + HOUR_MS - 1,
            "0", 42, str(tb), "0", "0"]


def test_hour_floor() -> None:
    assert hour_floor_ms(H0 + 59 * 60_000) == H0
    assert hour_floor_ms(H0) == H0


def test_parse_binance_klines_drops_the_forming_candle() -> None:
    rows = [_kline(H0, 1, 2, 0.5, 1.5, 10, 7), _kline(H0 + HOUR_MS, 1.5, 2, 1, 1.2, 4, 1)]
    now = H0 + HOUR_MS + 5_000  # second candle still forming
    bars = parse_binance_klines(rows, venue="binance_spot", symbol="BTCUSDT", now_ms=now)
    assert len(bars) == 1
    bar = bars[0]
    assert (bar.hour_start_ms, bar.close, bar.volume, bar.taker_buy_volume, bar.trades) == (
        H0, 1.5, 10.0, 7.0, 42
    )
    assert bar.complete and bar.source == "rest_klines"
    assert abs(bar.imbalance - 0.4) < 1e-12  # 2*7/10 - 1


def test_imbalance_is_none_without_volume() -> None:
    bar = HourBar("kraken_spot", "BTC/USD", H0, None, None, None, None, 0.0, 0.0, 0, False, "x")
    assert bar.imbalance is None


def test_aggregator_builds_bar_and_marks_start_hour_incomplete() -> None:
    agg = HourAggregator(venue="kraken_spot", symbol="BTC/USD", source="ws_v2_trade",
                         started_at_ms=H0 + 10 * 60_000)
    agg.add(H0 + 11 * 60_000, 100.0, 2.0, "buy")
    agg.add(H0 + 12 * 60_000, 101.0, 1.0, "sell")
    agg.add(H0 + HOUR_MS + 1_000, 102.0, 3.0, "buy")
    assert agg.roll(H0 + HOUR_MS - 1) == []  # hour not over yet
    first = agg.roll(H0 + HOUR_MS)
    assert len(first) == 1
    b = first[0]
    assert (b.open, b.high, b.low, b.close) == (100.0, 101.0, 100.0, 101.0)
    assert (b.volume, b.taker_buy_volume, b.trades) == (3.0, 2.0, 2)
    assert b.complete is False  # recorder started 10 minutes into the hour
    second = agg.roll(H0 + 2 * HOUR_MS)
    assert len(second) == 1 and second[0].complete is True and second[0].volume == 3.0


def test_aggregator_gap_marks_every_hour_it_spans_and_emits_empty_hours() -> None:
    agg = HourAggregator(venue="kraken_spot", symbol="BTC/USD", source="ws_v2_trade",
                         started_at_ms=H0)
    agg.roll(H0 + HOUR_MS)  # flush the (incomplete) start hour
    agg.mark_gap(H0 + HOUR_MS + 50 * 60_000, H0 + 2 * HOUR_MS + 5 * 60_000)
    bars = agg.roll(H0 + 4 * HOUR_MS)
    assert [b.hour_start_ms for b in bars] == [H0 + HOUR_MS, H0 + 2 * HOUR_MS, H0 + 3 * HOUR_MS]
    assert [b.complete for b in bars] == [False, False, True]
    assert all(b.volume == 0.0 and b.trades == 0 and b.open is None for b in bars)


def test_late_print_for_a_rolled_hour_is_ignored() -> None:
    agg = HourAggregator(venue="kraken_spot", symbol="BTC/USD", source="s", started_at_ms=H0)
    agg.roll(H0 + HOUR_MS)
    agg.add(H0 + 30 * 60_000, 99.0, 1.0, "buy")  # belongs to an already-written hour
    bars = agg.roll(H0 + 2 * HOUR_MS)
    assert bars[0].trades == 0
