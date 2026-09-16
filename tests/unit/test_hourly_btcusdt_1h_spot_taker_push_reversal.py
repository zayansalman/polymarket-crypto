"""Binance BTCUSDT 1h reversal after a spot taker-buy/sell push that perps didn't match, with the hour closing at its high or low: flow push, close location and the frozen rule."""
from __future__ import annotations

import statistics

import pytest

from polymarket_bot.hourly import btcusdt_1h_spot_taker_push_reversal as rule
from polymarket_bot.hourly.market import Candle


def _series(last_tb_share: float, *, o: float = 100.0, c: float = 110.0,
            hi: float = 110.0, lo: float = 99.0, n: int = 170) -> list[Candle]:
    """n hourly candles: baseline taker-buy share alternating 0.45/0.55, then the last one."""
    out = []
    for i in range(n - 1):
        share = 0.55 if i % 2 else 0.45
        out.append(Candle(i * 3_600_000, 100.0, 101.0, 99.0, 100.5, 10.0, 1000.0, 10.0 * share))
    out.append(Candle((n - 1) * 3_600_000, o, hi, lo, c, 10.0, 1000.0, 10.0 * last_tb_share))
    return out


def test_flow_push_matches_sample_stdev_over_window_including_last() -> None:
    candles = _series(0.9)
    fp = rule.flow_push(candles)
    imbs = [2 * c.taker_buy_volume / c.volume - 1 for c in candles[-rule.WINDOW:]]
    expected_z = (imbs[-1] - statistics.mean(imbs)) / statistics.stdev(imbs)
    assert fp.imbalance == pytest.approx(0.8)
    assert fp.z == pytest.approx(expected_z)
    assert fp.direction == 1 and fp.fz == pytest.approx(expected_z)
    assert fp.clv == pytest.approx(1.0)  # (2*110 - 110 - 99) / 11


def test_flow_push_needs_a_full_window() -> None:
    with pytest.raises(ValueError):
        rule.flow_push(_series(0.9, n=rule.WINDOW - 1))


def test_rule_fires_down_after_pushed_up_hour_closing_at_high() -> None:
    d = rule.decide(_series(0.9), _series(0.5))  # perps show no push
    assert d.side == "Down"
    assert d.signal["spot_fz"] > rule.SPOT_FZ_MIN and d.signal["perp_fz"] <= rule.PERP_FZ_MAX
    assert d.signal["wider_rule_fired"] is True
    assert d.reason.startswith("enter Down")


def test_rule_fires_up_after_pushed_down_hour_closing_at_low() -> None:
    spot = _series(0.1, o=110.0, c=100.0, hi=111.0, lo=100.0)  # sellers pushed it down
    perp = _series(0.5, o=110.0, c=100.0, hi=111.0, lo=100.0)
    assert rule.decide(spot, perp).side == "Up"


def test_no_bet_when_perps_confirm_weak_push_mid_close_or_flat() -> None:
    assert rule.decide(_series(0.9), _series(0.9)).side is None  # perps confirmed
    weak = rule.decide(_series(0.56), _series(0.5))
    assert weak.side is None and weak.signal["wider_rule_fired"] is False
    mid = rule.decide(_series(0.9, c=104.5, hi=110.0, lo=99.0), _series(0.5))
    assert mid.side is None and "extreme" in mid.reason
    flat = rule.decide(_series(0.9, o=100.0, c=100.0, hi=101.0, lo=99.0), _series(0.5))
    assert flat.side is None and "flat" in flat.reason
