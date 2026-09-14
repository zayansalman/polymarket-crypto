"""Decision/reference/settlement instants for a daily noon-ET Up/Down market.

The market for date D resolves Up if the Binance 1-minute close labeled 12:00 ET
on D is above the one labeled 12:00 ET on D-1. The decision is taken a few
minutes before that reference candle so the model never sees the reference print.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DECISION_LEAD_S = 300


def noon_et(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, 12, tzinfo=ET).timestamp())


@dataclass(frozen=True)
class MarketTimes:
    t_dec: int
    t_ref: int
    t_settle: int


def market_times(market_date: date) -> MarketTimes:
    t_ref = noon_et(market_date - timedelta(days=1))
    return MarketTimes(t_dec=t_ref - DECISION_LEAD_S, t_ref=t_ref, t_settle=noon_et(market_date))
