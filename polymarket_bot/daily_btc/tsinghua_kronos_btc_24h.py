"""Tsinghua-Kronos BTC 24h: the Kronos team's 24-hour BTCUSDT forecast, bet when it beats the price.

Name: Zayan (operator), 2026-09-16. Forecast recipe: github.com/shiyu-coder/Kronos-demo
update_predictions.py (commit eba16695), Kronos (Shi et al., arXiv 2508.02739, Tsinghua).
Bet only when the forecast beats the market price: Zayan (operator), 2026-09-13.
Default edge threshold 0.05: Claude, 2026-09-16. Full sources:
docs/strategies/tsinghua-kronos-btc-24h.md.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from polymarket_bot.daily_btc.forecast_input import INPUT_CANDLES
from polymarket_bot.hourly.market import Candle
from polymarket_bot.kronos_forecast.client import (
    KRONOS_CODE_COMMIT,
    KRONOS_MINI_WITH_TOKENIZER_2K,
    ForecastRequest,
    ForecastResult,
)

STRATEGY_ID = "tsinghua_kronos_btc_24h"
DISPLAY_NAME = "Tsinghua-Kronos BTC 24h"
# Recipe of the Kronos team's published forecast (research branch
# research/kronos-mini-official-btcusdt-24h-demo-recreation, README). Fixed so live calls
# stay comparable with that scored record.
HORIZON_HOURS = 24
PATHS = 30
TEMPERATURE = 1.0
TOP_P = 0.95
TOP_K = 0
MAX_CONTEXT = 512
NOT_A_PROBABILITY = "forecast probability was not a number between 0 and 1"
__all__ = ["INPUT_CANDLES", "STRATEGY_ID", "DISPLAY_NAME", "Decision", "request_for", "decide"]


@dataclass(frozen=True)
class Decision:
    side: str | None
    reason: str
    signal: dict[str, Any]
    available: bool


def request_for(candles: list[Candle], reference_ts: int) -> ForecastRequest:
    return ForecastRequest(
        candles=[[c.open_time_ms, c.open, c.high, c.low, c.close, c.volume, c.quote_volume]
                 for c in candles],
        horizon=HORIZON_HOURS, paths=PATHS, temperature=TEMPERATURE, top_p=TOP_P, top_k=TOP_K,
        seed=reference_ts // 3600, max_context=MAX_CONTEXT,
    )


def _price(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "none"


def _is_probability(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0.0 <= value <= 1.0)


def decide(
    result: ForecastResult,
    *,
    candles: list[Candle],
    reference_ts: int,
    up_ask: float | None,
    down_ask: float | None,
    edge_threshold: float,
) -> Decision:
    spec = KRONOS_MINI_WITH_TOKENIZER_2K
    signal: dict[str, Any] = {
        "strategy": DISPLAY_NAME,
        "model": f"{spec.model_repo}@{spec.model_revision}",
        "tokenizer": f"{spec.tokenizer_repo}@{spec.tokenizer_revision}",
        "code": f"shiyu-coder/Kronos@{KRONOS_CODE_COMMIT}",
        "recipe": "shiyu-coder/Kronos-demo update_predictions.py@eba16695",
        "paths": PATHS, "horizon_hours": HORIZON_HOURS, "temperature": TEMPERATURE,
        "top_p": TOP_P, "top_k": TOP_K, "seed": reference_ts // 3600,
        "input_candles": len(candles),
        "input_first_open_ms": candles[0].open_time_ms if candles else None,
        "input_last_open_ms": candles[-1].open_time_ms if candles else None,
        "up_ask": up_ask, "down_ask": down_ask, "edge_threshold": edge_threshold,
        "up_edge": None, "down_edge": None,
    }
    if not result.ok or result.upside_prob is None:
        signal["error"] = result.error
        return Decision(None, f"unavailable: {result.error}", signal, available=False)
    if not _is_probability(result.upside_prob):
        signal["error"] = NOT_A_PROBABILITY
        return Decision(None, f"unavailable: {NOT_A_PROBABILITY}", signal, available=False)
    p = float(result.upside_prob)
    se = math.sqrt(p * (1 - p) / PATHS)
    up_edge = p - up_ask if up_ask is not None and up_ask > 0 else None
    down_edge = (1 - p) - down_ask if down_ask is not None and down_ask > 0 else None
    signal.update({
        "p_up": p, "sampling_se": se, "last_close": result.last_close,
        "final_closes": list(result.final_closes), "worker_seconds": result.seconds,
        "up_edge": up_edge, "down_edge": down_edge,
        # The side the Kronos team's published record was scored on (P above/below 50%).
        "published_record_side": "Up" if p > 0.5 else ("Down" if p < 0.5 else None),
    })
    best: tuple[str, float] | None = None
    if up_edge is not None and up_edge >= edge_threshold:
        best = ("Up", up_edge)
    if down_edge is not None and down_edge >= edge_threshold and (best is None or down_edge > best[1]):
        best = ("Down", down_edge)
    if best is None:
        return Decision(
            None,
            f"no bet: P(up) {p:.2f} vs Up ask {_price(up_ask)} and Down ask {_price(down_ask)}; "
            f"no edge of {edge_threshold:.2f} or more",
            signal, available=True,
        )
    side, edge = best
    return Decision(
        side,
        f"enter {side}: P(up) {p:.2f} (sampling error {se:.2f}), edge {edge:+.2f} "
        f"at threshold {edge_threshold:.2f}",
        signal, available=True,
    )
