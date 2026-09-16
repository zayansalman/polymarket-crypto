"""Rerun the Tsinghua-Kronos BTC 24h forecast on hours the Kronos team published; compare.

Inputs (produced on branch research/kronos-mini-official-btcusdt-24h-demo-recreation):
  --published  research/kronos_mini_official_btcusdt_demo/data/published_forecasts_scored.csv
  --candles    research/kronos_mini_official_btcusdt_demo/data/btcusdt_1h_spot.csv
Prints, per hour, our upside probability vs the published one, then the mean absolute gap,
the correlation and how many gaps exceed 2 sampling standard errors. With 30 paths the gap
from sampling alone is about sqrt(2 * p(1-p)/30), roughly 13 points near 50%.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from polymarket_bot.daily_btc import tsinghua_kronos_btc_24h as rule  # noqa: E402
from polymarket_bot.hourly.market import Candle  # noqa: E402
from polymarket_bot.kronos_forecast import client as kc  # noqa: E402

HOUR_MS = 3_600_000


def load_candles(path: Path) -> dict[int, Candle]:
    out = {}
    with path.open() as f:
        for r in csv.DictReader(f):
            t = int(r["open_time_ms"])
            out[t] = Candle(t, float(r["open"]), float(r["high"]), float(r["low"]),
                            float(r["close"]), float(r["volume"]), float(r["quote_volume"]),
                            float(r["taker_buy_base"]))
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--published", type=Path, required=True)
    ap.add_argument("--candles", type=Path, required=True)
    ap.add_argument("--hours", type=int, default=40)
    args = ap.parse_args()
    candles = load_candles(args.candles)
    rows = []
    with args.published.open() as f:
        for r in csv.DictReader(f):
            if r["era"] == "mini":
                rows.append(r)
    step = max(1, len(rows) // args.hours)
    pairs = []
    for r in rows[::step][: args.hours]:
        start_ms = int(float(__import__("pandas").Timestamp(r["anchor"]).timestamp()) * 1000)
        window = [candles.get(start_ms - k * HOUR_MS) for k in range(rule.INPUT_CANDLES, 0, -1)]
        if any(c is None for c in window):
            continue
        result = await kc.run_forecast(rule.request_for(window, start_ms // 1000))
        if not result.ok:
            print("worker failed:", result.error)
            return 1
        published = float(r["p"])
        pairs.append((result.upside_prob, published))
        print(f"{r['anchor']}  ours={result.upside_prob:.3f}  published={published:.3f}  "
              f"({result.seconds:.1f} s)")
    ours = [a for a, _ in pairs]
    pub = [b for _, b in pairs]
    n = len(pairs)
    if n < 2:
        print(f"hours={n}: need at least 2 compared hours for a correlation")
        return 1
    mae = sum(abs(a - b) for a, b in pairs) / n
    ma, mb = sum(ours) / n, sum(pub) / n
    cov = sum((a - ma) * (b - mb) for a, b in pairs)
    spread = math.sqrt(sum((a - ma) ** 2 for a in ours) * sum((b - mb) ** 2 for b in pub))
    corr = cov / spread if spread > 0 else float("nan")
    wide = sum(1 for a, b in pairs
               if abs(a - b) > 2 * math.sqrt(max(b * (1 - b), 1 / 30) * 2 / rule.PATHS))
    print(f"hours={n} mean_abs_gap={mae:.3f} correlation={corr:.3f} gaps_beyond_2se={wide}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
