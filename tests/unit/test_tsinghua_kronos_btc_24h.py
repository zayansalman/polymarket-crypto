"""Tsinghua-Kronos BTC 24h: recipe request, price-relative bet rule, recorded signal."""
from __future__ import annotations

import math

import pytest

from polymarket_bot.daily_btc import tsinghua_kronos_btc_24h as rule
from polymarket_bot.hourly.market import Candle
from polymarket_bot.kronos_forecast.client import ForecastResult

REF = 1_789_574_400  # 2026-09-16 16:00 UTC (noon EDT)
CANDLES = [Candle((REF - (383 - i) * 3600) * 1000, 100.0, 101.0, 99.0, 100.0 + i, 10.0, 1000.0, 5.0)
           for i in range(383)]


def _result(p: float) -> ForecastResult:
    return ForecastResult(ok=True, upside_prob=p, last_close=482.0, final_closes=(483.0, 481.0),
                          seconds=8.8)


def test_request_follows_the_kronos_demo_recipe() -> None:
    req = rule.request_for(CANDLES, REF)
    assert (req.horizon, req.paths, req.temperature, req.top_p, req.top_k, req.max_context) == (
        24, 30, 1.0, 0.95, 0, 512)
    assert req.seed == REF // 3600
    assert req.candles[-1] == [(REF - 3600) * 1000, 100.0, 101.0, 99.0, 482.0, 10.0, 1000.0]
    assert len(req.candles) == rule.INPUT_CANDLES == 383


def test_buys_up_when_the_forecast_beats_the_up_price_by_the_threshold() -> None:
    d = rule.decide(_result(0.60), candles=CANDLES, reference_ts=REF, up_ask=0.52,
                    down_ask=0.49, edge_threshold=0.05)
    assert d.side == "Up" and d.available
    assert d.signal["up_edge"] == pytest.approx(0.08)
    assert d.signal["down_edge"] == pytest.approx(-0.09)
    assert d.signal["sampling_se"] == pytest.approx(math.sqrt(0.6 * 0.4 / 30))
    assert d.signal["published_record_side"] == "Up"
    assert d.signal["model"] == "NeoQuasar/Kronos-mini@f4e68697d9d5aed55cef5c96aabc3376bcad9f81"
    assert d.signal["input_last_open_ms"] == (REF - 3600) * 1000
    assert d.reason.startswith("enter Up")


def test_buys_down_and_skips_below_the_threshold() -> None:
    down = rule.decide(_result(0.30), candles=CANDLES, reference_ts=REF, up_ask=0.52,
                       down_ask=0.49, edge_threshold=0.05)
    assert down.side == "Down" and down.signal["down_edge"] == pytest.approx(0.21)
    skip = rule.decide(_result(0.55), candles=CANDLES, reference_ts=REF, up_ask=0.52,
                       down_ask=0.49, edge_threshold=0.05)
    assert skip.side is None and skip.available and skip.reason.startswith("no bet")


def test_missing_asks_and_unavailable_forecasts() -> None:
    no_book = rule.decide(_result(0.9), candles=CANDLES, reference_ts=REF, up_ask=None,
                          down_ask=None, edge_threshold=0.05)
    assert no_book.side is None and no_book.signal["up_edge"] is None
    failed = rule.decide(ForecastResult(ok=False, error="timed out"), candles=CANDLES,
                         reference_ts=REF, up_ask=0.5, down_ask=0.5, edge_threshold=0.05)
    assert failed.side is None and failed.available is False
    assert failed.reason == "unavailable: timed out"


@pytest.mark.parametrize("p", [math.nan, math.inf, 1.5, -0.2, True],
                         ids=["nan", "infinite", "above_one", "below_zero", "a_bool"])
def test_a_probability_that_is_not_a_number_between_0_and_1_is_unavailable(p: float) -> None:
    d = rule.decide(_result(p), candles=CANDLES, reference_ts=REF, up_ask=0.5, down_ask=0.5,
                    edge_threshold=0.05)
    assert (d.side, d.available) == (None, False)
    assert d.reason == "unavailable: forecast probability was not a number between 0 and 1"
    assert "p_up" not in d.signal


def test_a_published_record_side_is_none_at_exactly_half() -> None:
    d = rule.decide(_result(0.5), candles=CANDLES, reference_ts=REF, up_ask=0.5, down_ask=0.5,
                    edge_threshold=0.05)
    assert d.signal["published_record_side"] is None
