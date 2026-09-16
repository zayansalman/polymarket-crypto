"""Download Binance spot BTCUSDT 1h klines (with taker-buy volume) from data.binance.vision.

The Kronos demo fetched the same candles from Binance's REST API (python-binance
Client.get_klines, spot). Monthly archives are used for full months and daily archives
for the current month. Spot archives from 2025 onward use microsecond timestamps; they
are normalised to milliseconds. Output: data/btcusdt_1h_spot.csv (UTC open time).
"""
from __future__ import annotations

import csv
import io
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "data" / "btcusdt_1h_spot.csv"
BASE = "https://data.binance.vision/data/spot"
COLS = ["open_time_ms", "open", "high", "low", "close", "volume", "close_time_ms",
        "quote_volume", "trades", "taker_buy_base", "taker_buy_quote"]


def _rows(url: str) -> list[list[str]]:
    try:
        with urlopen(url, timeout=60) as r:
            payload = r.read()
    except HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        text = z.read(z.namelist()[0]).decode()
    out = []
    for rec in csv.reader(io.StringIO(text)):
        if not rec or not rec[0].isdigit():
            continue
        ot, ct = int(rec[0]), int(rec[6])
        if ot >= 10**14:  # microseconds
            ot, ct = ot // 1000, ct // 1000
        out.append([str(ot), *rec[1:6], str(ct), *rec[7:11]])
    return out


def main(start: str = "2025-06-01", end: str | None = None) -> int:
    first = date.fromisoformat(start)
    last = date.fromisoformat(end) if end else date.today() - timedelta(days=1)
    rows: dict[int, list[str]] = {}
    month = date(first.year, first.month, 1)
    while month <= last:
        nxt = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
        tag = f"{month:%Y-%m}"
        got = _rows(f"{BASE}/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-{tag}.zip")
        if not got:  # month not archived yet: fall back to daily files
            day = month
            while day < nxt and day <= last:
                got += _rows(f"{BASE}/daily/klines/BTCUSDT/1h/BTCUSDT-1h-{day:%Y-%m-%d}.zip")
                day += timedelta(days=1)
        for r in got:
            rows[int(r[0])] = r
        print(tag, len(got), flush=True)
        month = nxt
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        for k in sorted(rows):
            w.writerow(rows[k])
    keys = sorted(rows)
    gaps = sum(1 for a, b in zip(keys, keys[1:]) if b - a != 3_600_000)
    print(f"{len(keys)} candles, gaps={gaps} -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
