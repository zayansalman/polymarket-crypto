"""Re-download the three files the frozen BTCUSDT 1h rule was tested on, pinned to the original range.

Same endpoints, columns and ranges as the 2026-09-13 downloads (session_commands 037, 042, 049):
- btc_1h_flow.csv: Binance spot BTCUSDT 1h klines, 2023-10-12 00:00 .. 2026-09-13 18:00 UTC (open time)
- btc_perp_1h_flow.csv: Binance USD-M perp BTCUSDT 1h klines, 2023-10-12 00:00 .. 2026-09-13 19:00 UTC
- bybit_oi_1h.csv: Bybit linear BTCUSDT open interest, hourly, 2023-10-12 00:00 .. 2026-09-13 22:00 UTC

Usage: python3 fetch_btcusdt_1h_spot_perp_klines_and_bybit_oi.py [DATA_DIR]
Written by Claude, 2026-09-16, to reproduce the research after the scratch folder was wiped.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import pandas as pd

DATA = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "data"
START = pd.Timestamp("2023-10-12 00:00")
KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "amount",
              "trades", "taker_buy_base", "taker_buy_quote", "ignore"]


def _ms(ts: pd.Timestamp) -> int:
    return int(ts.tz_localize("UTC").timestamp() * 1000)


def _get(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except Exception:  # noqa: BLE001 - retried, then raised
            if attempt == 3:
                raise
            time.sleep(2 + 2 * attempt)
    raise AssertionError("unreachable")


def klines(url_base: str, last_open: pd.Timestamp, limit: int) -> pd.DataFrame:
    start, end, rows = _ms(START), _ms(last_open), []
    while start <= end:
        batch = _get(f"{url_base}?symbol=BTCUSDT&interval=1h&startTime={start}&limit={limit}")
        if not batch:
            break
        rows += batch
        start = batch[-1][0] + 3_600_000
        time.sleep(0.2)
    df = pd.DataFrame(rows, columns=KLINE_COLS).drop_duplicates("open_time").sort_values("open_time")
    df = df[df.open_time <= end]
    for c in ["open", "high", "low", "close", "volume", "amount", "taker_buy_base", "taker_buy_quote"]:
        df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df.open_time, unit="ms")
    cols = ["ts", "open", "high", "low", "close", "volume", "amount", "trades", "taker_buy_base",
            "taker_buy_quote"]
    return df[cols].reset_index(drop=True)


def bybit_oi(last_ts: pd.Timestamp) -> pd.DataFrame:
    s, end, step, rows = _ms(START), _ms(last_ts), 200 * 3_600_000, []
    while s <= end:
        e = min(s + step - 1, end)
        d = _get("https://api.bybit.com/v5/market/open-interest?category=linear&symbol=BTCUSDT"
                 f"&intervalTime=1h&startTime={s}&endTime={e}&limit=200")
        rows += [(int(x["timestamp"]), float(x["openInterest"]))
                 for x in d.get("result", {}).get("list", [])]
        s = e + 1
        time.sleep(0.15)
    df = pd.DataFrame(rows, columns=["ts_ms", "oi"]).drop_duplicates("ts_ms").sort_values("ts_ms")
    df["ts"] = pd.to_datetime(df.ts_ms, unit="ms")
    return df.reset_index(drop=True)


def _report(name: str, df: pd.DataFrame) -> None:
    gaps = int(df.ts.diff().dropna().ne(pd.Timedelta(hours=1)).sum())
    print(f"{name}: rows {len(df)} {df.ts.iloc[0]} -> {df.ts.iloc[-1]} | gaps {gaps}")


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    spot = klines("https://api.binance.com/api/v3/klines", pd.Timestamp("2026-09-13 18:00"), 1000)
    spot.to_csv(DATA / "btc_1h_flow.csv", index=False)
    _report("btc_1h_flow.csv", spot)
    perp = klines("https://fapi.binance.com/fapi/v1/klines", pd.Timestamp("2026-09-13 19:00"), 1500)
    perp.to_csv(DATA / "btc_perp_1h_flow.csv", index=False)
    _report("btc_perp_1h_flow.csv", perp)
    oi = bybit_oi(pd.Timestamp("2026-09-13 22:00"))
    oi.to_csv(DATA / "bybit_oi_1h.csv", index=False)
    _report("bybit_oi_1h.csv", oi)


if __name__ == "__main__":
    main()
