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


def _decide(p: float, up_ask: float | None, down_ask: float | None) -> rule.Decision:
    return rule.decide(_result(p), candles=CANDLES, reference_ts=REF, up_ask=up_ask,
                       down_ask=down_ask, edge_threshold=0.05)


def test_an_edge_exactly_at_the_threshold_bets() -> None:
    d = _decide(0.6, up_ask=0.55, down_ask=0.5)  # 0.6 - 0.55 is 0.04999999999999993 unrounded
    assert d.side == "Up" and d.signal["up_edge"] == 0.05


def test_the_larger_edge_wins_and_equal_edges_go_to_up() -> None:
    assert _decide(0.6, up_ask=0.50, down_ask=0.20).side == "Down"  # Up 0.10, Down 0.20
    assert _decide(0.6, up_ask=0.40, down_ask=0.30).side == "Up"    # Up 0.20, Down 0.10
    tie = _decide(0.6, up_ask=0.45, down_ask=0.25)  # both 0.15 once rounded
    assert tie.signal["up_edge"] == tie.signal["down_edge"] == 0.15
    assert tie.side == "Up"


def test_zero_or_negative_asks_are_ignored() -> None:
    down_only = _decide(0.1, up_ask=0.0, down_ask=0.5)
    assert down_only.side == "Down" and down_only.signal["up_edge"] is None
    up_only = _decide(0.9, up_ask=0.5, down_ask=-0.1)
    assert up_only.side == "Up" and up_only.signal["down_edge"] is None


@pytest.mark.parametrize("result", [
    ForecastResult(ok=False), ForecastResult(ok=False, error=""),
    ForecastResult(ok=True, upside_prob=None),
], ids=["failed_without_an_error", "failed_with_a_blank_error", "ok_without_a_probability"])
def test_an_unavailable_reason_never_says_none(result: ForecastResult) -> None:
    d = rule.decide(result, candles=CANDLES, reference_ts=REF, up_ask=0.5, down_ask=0.5,
                    edge_threshold=0.05)
    assert (d.side, d.available) == (None, False)
    assert d.reason == "unavailable: the forecast returned no probability"


@pytest.mark.parametrize(("up_ask", "down_ask"), [(None, None), (0.0, None), (None, -0.1),
                                                  (0.0, 0.0)])
def test_no_usable_ask_on_either_side_says_there_are_no_prices(
    up_ask: float | None, down_ask: float | None
) -> None:
    d = _decide(0.9, up_ask=up_ask, down_ask=down_ask)
    assert (d.side, d.available) == (None, True)
    assert d.reason == "no bet: no order book prices for this window"


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


def test_the_record_keeps_the_input_close_and_window_length_when_the_forecast_fails() -> None:
    failed = rule.decide(ForecastResult(ok=False, error="timed out"), candles=CANDLES,
                         reference_ts=REF, up_ask=0.5, down_ask=0.5, edge_threshold=0.05,
                         window_hours=23.0)
    assert failed.signal["input_close"] == failed.signal["last_close"] == 482.0
    assert failed.signal["window_hours"] == 23.0
    assert (failed.signal["worker_seconds"], failed.signal["torch_version"]) == (None, None)


def test_the_record_takes_the_close_from_the_input_and_run_details_from_the_worker() -> None:
    result = ForecastResult(ok=True, upside_prob=0.6, last_close=999.0,
                            final_closes=(483.0, 481.0), seconds=8.8, torch_version="2.10.0")
    d = rule.decide(result, candles=CANDLES, reference_ts=REF, up_ask=0.52, down_ask=0.49,
                    edge_threshold=0.05, window_hours=24.0)
    assert d.signal["input_close"] == d.signal["last_close"] == 482.0  # not the worker's 999
    assert (d.signal["worker_seconds"], d.signal["torch_version"], d.signal["window_hours"]) == (
        8.8, "2.10.0", 24.0)
    unset = rule.decide(result, candles=CANDLES, reference_ts=REF, up_ask=0.52, down_ask=0.49,
                        edge_threshold=0.05)
    assert unset.signal["window_hours"] is None


def test_a_published_record_side_is_none_at_exactly_half() -> None:
    d = rule.decide(_result(0.5), candles=CANDLES, reference_ts=REF, up_ask=0.5, down_ask=0.5,
                    edge_threshold=0.05)
    assert d.signal["published_record_side"] is None
