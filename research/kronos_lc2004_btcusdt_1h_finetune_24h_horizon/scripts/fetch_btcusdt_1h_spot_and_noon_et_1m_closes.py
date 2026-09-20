"""Fetch the Binance spot BTCUSDT candles the 24-hour test needs.

Writes (git-ignored):
- data/btcusdt_1h_spot.csv: closed 1h candles from 2025-09-20 00:00 UTC (512 hours before the
  first noon-ET anchor, plus margin) up to now.
- data/noon_et_1m_closes.csv: for every New York date from 2025-10-18 on, the 1-minute candle
  that opens at 12:00 America/New_York. Its close is the price Polymarket's daily BTC Up/Down
  market settles on (polymarket_bot/daily/market.py, fetch_close_at, on branch
  feature/tsinghua-kronos-btc-24h-daily-btc-market).

Source: Binance public REST API, GET /api/v3/klines (no key). Claude, 2026-09-17.
"""
from __future__ import annotations

import csv
import json
import time
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent.parent
DATA = HERE / "data"
KLINES = "https://api.binance.com/api/v3/klines"
ET = ZoneInfo("America/New_York")
HOUR_MS = 3_600_000
HOURLY_FROM = datetime(2025, 9, 20, tzinfo=UTC)
FIRST_NOON_DATE = date(2025, 10, 18)


def get(params: dict) -> list:
    url = f"{KLINES}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def noon_et_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, 12, tzinfo=ET).timestamp() * 1000)


def fetch_hourly(now_ms: int) -> int:
    start = int(HOURLY_FROM.timestamp() * 1000)
    rows: dict[int, list] = {}
    while start < now_ms:
        batch = get({"symbol": "BTCUSDT", "interval": "1h", "startTime": start, "limit": 1000})
        if not batch:
            break
        for k in batch:
            if k[6] < now_ms:  # closed candles only
                rows[k[0]] = k
        start = batch[-1][0] + HOUR_MS
        time.sleep(0.2)
    times = sorted(rows)
    gaps = [t for a, t in zip(times, times[1:]) if t - a != HOUR_MS]
    if gaps:
        raise SystemExit(f"gaps in hourly candles after: {gaps[:5]}")
    with (DATA / "btcusdt_1h_spot.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["open_time_ms", "open", "high", "low", "close", "volume", "quote_volume"])
        for t in times:
            k = rows[t]
            w.writerow([k[0], k[1], k[2], k[3], k[4], k[5], k[7]])
    print(f"hourly: {len(times)} candles, {datetime.fromtimestamp(times[0] / 1000, UTC)} to "
          f"{datetime.fromtimestamp(times[-1] / 1000, UTC)}")
    return len(times)


def fetch_noon_closes(now_ms: int) -> int:
    out = []
    d = FIRST_NOON_DATE
    while noon_et_ms(d) + 60_000 <= now_ms:
        ms = noon_et_ms(d)
        batch = get({"symbol": "BTCUSDT", "interval": "1m", "startTime": ms, "limit": 1})
        if not batch or batch[0][0] != ms:
            raise SystemExit(f"no 1m candle opening at noon ET on {d}")
        out.append((d.isoformat(), ms, batch[0][4]))
        d += timedelta(days=1)
        time.sleep(0.1)
    with (DATA / "noon_et_1m_closes.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["et_date", "noon_open_time_ms", "close"])
        w.writerows(out)
    print(f"noon-ET 1m closes: {len(out)} dates, {out[0][0]} to {out[-1][0]}")
    return len(out)


if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    now = int(time.time() * 1000)
    fetch_hourly(now)
    fetch_noon_closes(now)
