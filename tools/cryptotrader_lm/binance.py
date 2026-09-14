"""Binance spot 5-minute klines: pre-decision price, momentum and aggressive flow.

Only candles that CLOSE before the decision instant are used. Aggressive flow is
the taker-buy share of traded volume: imbalance = (2 * taker_buy - volume) / volume,
+1 when every trade lifted the offer, -1 when every trade hit the bid.
"""
from __future__ import annotations

import bisect
import time

import httpx

KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
SYMBOL = {"btc": "BTCUSDT", "eth": "ETHUSDT"}
INTERVAL_S = 300
_PAGE = 1000
DAY_S = 86_400


def fetch_klines(client: httpx.Client, symbol: str, start_s: int, end_s: int) -> list[list]:
    rows: list[list] = []
    cursor_ms = start_s * 1000
    end_ms = end_s * 1000
    while cursor_ms < end_ms:
        resp = client.get(KLINES_URL, params={
            "symbol": symbol, "interval": "5m", "startTime": cursor_ms,
            "endTime": end_ms, "limit": _PAGE,
        })
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        rows.extend(page)
        cursor_ms = int(page[-1][0]) + INTERVAL_S * 1000
        time.sleep(0.1)
    return rows


class Candles:
    """5m candles indexed by open time (seconds)."""

    def __init__(self, rows: list[list]):
        dedup = {int(r[0]) // 1000: r for r in rows}
        self.open_s = sorted(dedup)
        self.close = [float(dedup[t][4]) for t in self.open_s]
        self.volume = [float(dedup[t][5]) for t in self.open_s]
        self.taker_buy = [float(dedup[t][9]) for t in self.open_s]
        self.quote_volume = [float(dedup[t][7]) for t in self.open_s]

    def _last_closed_index(self, t: int) -> int | None:
        """Index of the last candle whose close is at or before `t`."""
        i = bisect.bisect_right(self.open_s, t - INTERVAL_S) - 1
        if i < 0 or t - (self.open_s[i] + INTERVAL_S) >= INTERVAL_S:
            return None
        return i

    def price_at(self, t: int) -> float | None:
        i = self._last_closed_index(t)
        return None if i is None else self.close[i]

    def taker_imbalance(self, t: int, window_s: int) -> float | None:
        lo = bisect.bisect_left(self.open_s, t - window_s)
        hi = bisect.bisect_right(self.open_s, t - INTERVAL_S)
        expected = window_s // INTERVAL_S
        if hi - lo < expected * 0.9:
            return None
        vol = sum(self.volume[lo:hi])
        if vol <= 0:
            return None
        return (2 * sum(self.taker_buy[lo:hi]) - vol) / vol

    def quote_volume_sum(self, t: int, window_s: int) -> float:
        lo = bisect.bisect_left(self.open_s, t - window_s)
        hi = bisect.bisect_right(self.open_s, t - INTERVAL_S)
        return sum(self.quote_volume[lo:hi])


def price_features(candles: Candles, t_dec: int) -> dict:
    """Decision price, 3-day momentum sign, 30 daily % changes, taker imbalance."""
    price = candles.price_at(t_dec)
    daily = [candles.price_at(t_dec - k * DAY_S) for k in range(31)]
    daily_returns = None
    if all(p is not None for p in daily):
        chron = list(reversed(daily))
        daily_returns = [round(100 * (b / a - 1), 2) for a, b in zip(chron, chron[1:])]
    ret_3d = None
    if price is not None and daily[3] is not None:
        ret_3d = price / daily[3] - 1
    momentum = None if ret_3d is None else (ret_3d > 0) - (ret_3d < 0)
    return {
        "price": price,
        "ret_3d": ret_3d,
        "momentum_3d": momentum,
        "daily_returns_30d": daily_returns,
        "bn_imbalance_1h": candles.taker_imbalance(t_dec, 3_600),
        "bn_imbalance_4h": candles.taker_imbalance(t_dec, 14_400),
        "bn_imbalance_24h": candles.taker_imbalance(t_dec, DAY_S),
        "bn_quote_volume_24h": candles.quote_volume_sum(t_dec, DAY_S),
    }
