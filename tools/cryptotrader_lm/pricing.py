"""Entry pricing, taker fees and settlement for a $-stake daily Up/Down position.

`prices-history` returns the CLOB midpoint; the order book trades on a 1-cent
tick, so the touch is the nearest cent strictly on each side of the mid.
Fees follow each market's own Gamma `feeSchedule`:
fee/share = rate * (p * (1 - p)) ** exponent (zero when fees are disabled).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

TICK_CENTS = 1
MAX_MID_AGE_S = 600


def entry_mid(history: list[dict], t_dec: int, max_age_s: int = MAX_MID_AGE_S) -> float | None:
    """Last midpoint at or before `t_dec`, or None if missing or stale."""
    points = [pt for pt in history if pt["t"] <= t_dec]
    if not points:
        return None
    last = max(points, key=lambda pt: pt["t"])
    if t_dec - last["t"] > max_age_s:
        return None
    return float(last["p"])


def touch_from_mid(mid: float) -> tuple[float, float]:
    """(bid, ask) as the nearest whole cents strictly below/above the mid."""
    cents = mid * 100
    ask = math.floor(cents + 1e-6) + TICK_CENTS
    bid = math.ceil(cents - 1e-6) - TICK_CENTS
    bid = min(max(bid, 1), 98)
    ask = min(max(ask, 2), 99)
    return bid / 100, ask / 100


def entry_price(side: str, up_mid: float) -> float:
    """Taker price paid for one share of `side` ('up' or 'down')."""
    bid, ask = touch_from_mid(up_mid)
    if side == "up":
        return ask
    if side == "down":
        return round(1.0 - bid, 2)
    raise ValueError(f"unknown side {side!r}")


def fee_per_share(price: float, rate: float, exponent: float) -> float:
    if rate <= 0:
        return 0.0
    return rate * (price * (1.0 - price)) ** exponent


@dataclass(frozen=True)
class Settlement:
    side: str
    entry: float
    shares: float
    fee_usd: float
    pnl_usd: float
    won: bool | None


def settle(side: str, up_mid: float, outcome: str, rate: float, exponent: float,
           stake_usd: float = 10.0) -> Settlement:
    """PnL of buying `stake_usd` of `side` at the taker price; a tie pays 0.50."""
    entry = entry_price(side, up_mid)
    shares = stake_usd / entry
    fee = fee_per_share(entry, rate, exponent)
    if outcome == "tie":
        payout, won = 0.5, None
    else:
        won = outcome == side
        payout = 1.0 if won else 0.0
    pnl = shares * (payout - entry - fee)
    return Settlement(side=side, entry=entry, shares=shares, fee_usd=shares * fee,
                      pnl_usd=pnl, won=won)
