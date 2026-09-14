"""Fade the 3-day direction: bet the daily BTC market against BTC's last 3 days.

Decided shortly before the market's noon-ET reference candle. If BTC is higher than it
was exactly 72 hours earlier, buy Down; if lower, buy Up; no change or a missing price
means no trade. Mode-agnostic: the same decision feeds paper and live.
"""
from __future__ import annotations

from dataclasses import dataclass

STRATEGY_ID = "fade_three_day"
LOOKBACK_S = 3 * 86_400
DECISION_LEAD_S = 300


@dataclass(frozen=True)
class Decision:
    side: str | None
    reason: str
    return_3d: float | None


def decide(price_now: float | None, price_3d_ago: float | None) -> Decision:
    if price_now is None or price_3d_ago is None or price_3d_ago <= 0:
        return Decision(None, "skip: BTC price missing", None)
    change = price_now / price_3d_ago - 1
    if change > 0:
        return Decision("Down", f"BTC up {change:+.2%} over 3 days: buy Down", change)
    if change < 0:
        return Decision("Up", f"BTC down {change:+.2%} over 3 days: buy Up", change)
    return Decision(None, "skip: BTC unchanged over 3 days", 0.0)
