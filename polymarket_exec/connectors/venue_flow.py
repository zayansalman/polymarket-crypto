"""Closed-hour trade-flow bars and venue state snapshots for the Binance and Kraken feeds."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

HOUR_MS = 3_600_000

Side = Literal["buy", "sell"]


def hour_floor_ms(ts_ms: int) -> int:
    return ts_ms - ts_ms % HOUR_MS


@dataclass(frozen=True)
class HourBar:
    venue: str
    symbol: str
    hour_start_ms: int
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float
    taker_buy_volume: float
    trades: int
    complete: bool
    source: str

    @property
    def imbalance(self) -> float | None:
        """Aggressive-buy share mapped to [-1, 1]; None for an hour with no volume."""
        if self.volume <= 0:
            return None
        return 2 * self.taker_buy_volume / self.volume - 1


@dataclass(frozen=True)
class VenueSnapshot:
    venue: str
    symbol: str
    taken_at_ms: int
    mark_price: float | None
    index_price: float | None
    funding_rate: float | None
    next_funding_ms: int | None
    open_interest: float | None
    source: str


def parse_binance_klines(
    rows: list[list[Any]], *, venue: str, symbol: str, now_ms: int
) -> list[HourBar]:
    """Closed 1h klines only: Binance always returns the forming candle last."""
    bars: list[HourBar] = []
    for row in rows:
        if int(row[6]) >= now_ms:
            continue
        bars.append(
            HourBar(
                venue=venue,
                symbol=symbol,
                hour_start_ms=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                taker_buy_volume=float(row[9]),
                trades=int(row[8]),
                complete=True,
                source="rest_klines",
            )
        )
    return bars


@dataclass
class _Bucket:
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    taker_buy_volume: float = 0.0
    trades: int = 0


class HourAggregator:
    """Folds live trades into per-hour bars; any hour touched by a feed gap is incomplete."""

    def __init__(self, *, venue: str, symbol: str, source: str, started_at_ms: int) -> None:
        self.venue = venue
        self.symbol = symbol
        self.source = source
        self._buckets: dict[int, _Bucket] = {}
        self._gap_hours: set[int] = set()
        self._next_hour_ms = hour_floor_ms(started_at_ms)
        # Trades before the recorder started were never seen.
        self.mark_gap(started_at_ms, started_at_ms)

    def add(self, ts_ms: int, price: float, qty: float, taker_side: Side) -> None:
        hour = hour_floor_ms(ts_ms)
        if hour < self._next_hour_ms:
            return
        bucket = self._buckets.get(hour)
        if bucket is None:
            bucket = self._buckets[hour] = _Bucket(open=price, high=price, low=price, close=price)
        bucket.high = max(bucket.high, price)
        bucket.low = min(bucket.low, price)
        bucket.close = price
        bucket.volume += qty
        if taker_side == "buy":
            bucket.taker_buy_volume += qty
        bucket.trades += 1

    def mark_gap(self, from_ms: int, to_ms: int) -> None:
        hour = hour_floor_ms(from_ms)
        while hour <= to_ms:
            self._gap_hours.add(hour)
            hour += HOUR_MS

    def roll(self, now_ms: int) -> list[HourBar]:
        """Emit one bar for every hour that has fully ended, in order."""
        bars: list[HourBar] = []
        while self._next_hour_ms + HOUR_MS <= now_ms:
            hour = self._next_hour_ms
            bucket = self._buckets.pop(hour, None)
            complete = hour not in self._gap_hours
            self._gap_hours.discard(hour)
            bars.append(
                HourBar(
                    venue=self.venue,
                    symbol=self.symbol,
                    hour_start_ms=hour,
                    open=bucket.open if bucket else None,
                    high=bucket.high if bucket else None,
                    low=bucket.low if bucket else None,
                    close=bucket.close if bucket else None,
                    volume=bucket.volume if bucket else 0.0,
                    taker_buy_volume=bucket.taker_buy_volume if bucket else 0.0,
                    trades=bucket.trades if bucket else 0,
                    complete=complete,
                    source=self.source,
                )
            )
            self._next_hour_ms += HOUR_MS
        return bars
