"""Binance BTCUSDT 1h reversal after a spot taker-buy/sell push that perps didn't match, with the hour closing at its high or low.

Calculated from hour H-1's Binance spot and USD-M perp BTCUSDT 1h candles, at the open of hour H:
- imbalance = 2 * taker_buy_volume / volume - 1
- z = (imbalance - mean) / sample stdev, over the 168 hours ending at and including H-1
- flow push = z * sign(close - open), for spot and perp separately
- close location = (2 * close - high - low) / (high - low), on spot

Bet against H-1 (Down after an up hour, Up after a down hour) when spot flow push > 1.20,
perp flow push <= 1.24 and close location * direction > 0.80.

Sources (full table: docs/strategies/hourly-btc-strategies.md; evidence:
research/hourly_btc_2026_09/):
- skip hours without an extra signal: Zayan (operator), 2026-09-14;
- taker order flow as that signal: proposed by Claude, 2026-09-14, citing Kitron & Wengrowicz,
  arXiv 2608.21888; approved by Zayan (operator), 2026-09-14;
- the formulas, the perp and close-location conditions and the thresholds: Claude,
  2026-09-14, pre-registered in PREREG_orderflow.md, PREREG_perp_selective.md and
  PREREG_minute.md (rule R5); close location as in Marc Chaikin's Accumulation/Distribution;
- name: Zayan (operator), 2026-09-15.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from polymarket_bot.hourly.market import Candle

STRATEGY_ID = "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal"
# Frozen values (Claude, 2026-09-14): 168-hour window from PREREG_orderflow.md; 1.20 and 1.24
# are the 80th percentiles of spot and perp flow push on the discovery data (Binance
# 2023-10-12 to 2025-10-18 14:00 UTC); 0.80 was fixed in PREREG_minute.md before its test ran.
WINDOW = 168
SPOT_FZ_MIN = 1.20
PERP_FZ_MAX = 1.24
CLV_MIN = 0.80


@dataclass(frozen=True)
class FlowPush:
    imbalance: float | None
    z: float | None
    direction: int
    fz: float | None
    clv: float | None
    # Window statistics behind z, saved so the candle audit can recheck the decision
    # (candle audit approved by Zayan (operator), 2026-09-15).
    window_n: int = WINDOW
    window_mean: float | None = None
    window_sd: float | None = None


@dataclass(frozen=True)
class Decision:
    side: str | None
    reason: str
    signal: dict[str, Any]


def _imbalance(c: Candle) -> float | None:
    return 2 * c.taker_buy_volume / c.volume - 1 if c.volume > 0 else None


def flow_push(candles: list[Candle]) -> FlowPush:
    """Flow push of the LAST candle against the trailing WINDOW candles (including it)."""
    if len(candles) < WINDOW:
        raise ValueError(f"need {WINDOW} closed candles, got {len(candles)}")
    window = candles[-WINDOW:]
    last = window[-1]
    imbs = [_imbalance(c) for c in window]
    z: float | None = None
    mean: float | None = None
    sd: float | None = None
    if all(i is not None for i in imbs):
        mean = statistics.mean(imbs)
        sd = statistics.stdev(imbs)
        if sd > 0:
            z = (imbs[-1] - mean) / sd
    direction = 1 if last.close > last.open else (-1 if last.close < last.open else 0)
    rng = last.high - last.low
    clv = (2 * last.close - last.high - last.low) / rng if rng > 0 else None
    return FlowPush(
        imbalance=imbs[-1],
        z=z,
        direction=direction,
        fz=z * direction if z is not None else None,
        clv=clv,
        window_n=len(window),
        window_mean=mean,
        window_sd=sd,
    )


def _candle_fields(c: Candle) -> dict[str, Any]:
    return {
        "open_time_ms": c.open_time_ms, "open": c.open, "high": c.high, "low": c.low,
        "close": c.close, "volume": c.volume, "quote_volume": c.quote_volume,
        "taker_buy_volume": c.taker_buy_volume,
    }


def decide(spot: list[Candle], perp: list[Candle]) -> Decision:
    s = flow_push(spot)
    p = flow_push(perp)
    signal: dict[str, Any] = {
        "spot_imbalance": s.imbalance,
        "spot_z": s.z,
        "spot_fz": s.fz,
        "perp_z": p.z,
        "perp_fz": p.fz,
        "clv": s.clv,
        "direction": s.direction,
        "wider_rule_fired": s.fz is not None and s.fz > SPOT_FZ_MIN,
        "hour_open_time_ms": spot[-1].open_time_ms,
        # The exact inputs, for the candle audit against Binance's archive.
        "spot_h1": _candle_fields(spot[-1]),
        "perp_h1": _candle_fields(perp[-1]),
        "spot_window": {"n": s.window_n, "mean": s.window_mean, "sd": s.window_sd},
        "perp_window": {"n": p.window_n, "mean": p.window_mean, "sd": p.window_sd},
    }
    if s.direction == 0:
        return Decision(None, "no signal: last hour was flat", signal)
    if s.fz is None or s.fz <= SPOT_FZ_MIN:
        return Decision(None, f"no signal: spot push {s.fz} not above {SPOT_FZ_MIN}", signal)
    if p.fz is None or p.fz > PERP_FZ_MAX:
        return Decision(None, f"no signal: perps confirmed the push ({p.fz})", signal)
    if s.clv is None or s.clv * s.direction <= CLV_MIN:
        return Decision(None, f"no signal: did not close at the extreme (CLV {s.clv})", signal)
    side = "Down" if s.direction > 0 else "Up"
    return Decision(
        side,
        f"enter {side}: spot push {s.fz:.2f}, perp push {p.fz:.2f}, close location {s.clv:+.2f}",
        signal,
    )
