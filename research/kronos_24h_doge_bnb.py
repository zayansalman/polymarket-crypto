"""Kronos 24h directional accuracy on DOGE and BNB, scored the Polymarket way.

Source: Zayan (operator), 2026-09-19 — test the 24h Kronos forecast on the
Polymarket DOGE and BNB daily Up/Down books, direction only, not price.

Recipe matches Tsinghua-Kronos BTC 24h: base Kronos-mini (not the BTC
fine-tune, which is bent toward one instrument), 383 closed 1h candles in, 30
sampled paths, T=1.0, top_p=0.95, top_k=0, 24-step horizon. Upside is the share
of paths whose final close is strictly above the last input close.

Scored against the Polymarket daily rule: noon ET to noon ET, up when the later
close is at or above the earlier one. The venue settles on 1-minute closes; this
uses the 1h close at noon ET, which can disagree only on near-ties.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HOUR_MS = 3_600_000
INPUT_CANDLES = 383
HORIZON = 24
PATHS = 30
TEMPERATURE = 1.0
TOP_P = 0.95
TOP_K = 0
SEED = 7
ET = ZoneInfo("America/New_York")
CACHE = Path("data/kronos_24h_alt")
CODE_DIR = Path("third_party/kronos_67b630e")
MODELS = Path("data/kronos_models")


def fetch_klines(symbol: str, start_ms: int, end_ms: int) -> list[list[float]]:
    """Binance 1h klines, oldest first, paginated. Cached on disk per symbol."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE / f"{symbol}_1h_{start_ms}_{end_ms}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())

    rows: list[list[float]] = []
    cursor = start_ms
    while cursor < end_ms:
        url = (
            "https://api.binance.com/api/v3/klines"
            f"?symbol={symbol}&interval=1h&startTime={cursor}&limit=1000"
        )
        with urllib.request.urlopen(url, timeout=30) as resp:
            batch = json.loads(resp.read())
        if not batch:
            break
        for row in batch:
            open_ms = int(row[0])
            if open_ms >= end_ms:
                break
            rows.append(
                [open_ms, float(row[1]), float(row[2]), float(row[3]),
                 float(row[4]), float(row[5]), float(row[7])]
            )
        cursor = int(batch[-1][0]) + HOUR_MS
        time.sleep(0.12)
    cache_file.write_text(json.dumps(rows))
    return rows


def noon_et_open_ms(day: datetime) -> int:
    """Unix ms of the 1h candle that opens at noon ET on *day*."""
    noon = datetime(day.year, day.month, day.day, 12, tzinfo=ET)
    return int(noon.astimezone(UTC).timestamp() * 1000)


def build_windows(rows: list[list[float]]) -> dict[int, int]:
    return {int(r[0]): i for i, r in enumerate(rows)}


def load_predictor():
    sys.path.insert(0, str(CODE_DIR))
    import torch
    from model import Kronos, KronosPredictor, KronosTokenizer

    torch.set_num_threads(4)
    torch.manual_seed(SEED)
    tok_dir = next(
        (MODELS / "models--NeoQuasar--Kronos-Tokenizer-2k" / "snapshots").iterdir()
    )
    model_dir = next(
        (MODELS / "models--NeoQuasar--Kronos-mini" / "snapshots").iterdir()
    )
    tokenizer = KronosTokenizer.from_pretrained(str(tok_dir))
    model = Kronos.from_pretrained(str(model_dir))
    tokenizer.eval()
    model.eval()
    return torch, KronosPredictor(model, tokenizer, device="cpu", max_context=512)


def forecast(torch, predictor, candles: list[list[float]]) -> float:
    import pandas as pd

    cols = ["open", "high", "low", "close", "volume", "amount"]
    df = pd.DataFrame([r[1:] for r in candles], columns=cols, dtype="float64")
    x_ts = pd.Series(pd.to_datetime([int(r[0]) for r in candles], unit="ms"))
    first_future = pd.Timestamp(int(candles[-1][0]) + HOUR_MS, unit="ms")
    y_ts = pd.Series(pd.date_range(first_future, periods=HORIZON, freq="h"))
    with torch.no_grad():
        preds = predictor.predict_batch(
            df_list=[df] * PATHS, x_timestamp_list=[x_ts] * PATHS,
            y_timestamp_list=[y_ts] * PATHS, pred_len=HORIZON,
            T=TEMPERATURE, top_k=TOP_K, top_p=TOP_P, sample_count=1, verbose=False,
        )
    last_close = float(candles[-1][4])
    finals = [float(f["close"].iloc[-1]) for f in preds]
    return sum(1 for v in finals if v > last_close) / len(finals)


def score(results: list[dict]) -> None:
    n = len(results)
    if not n:
        print("  no windows scored")
        return
    hits = sum(1 for r in results if r["correct"])
    up_rate = sum(1 for r in results if r["actual_up"]) / n
    print(f"  n={n}  up-rate={up_rate:.3f}  hit={hits / n:.3f}")

    print("  calibration:")
    buckets = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.8), (0.8, 1.01)]
    for lo, hi in buckets:
        rows = [r for r in results if lo <= r["upside"] < hi]
        if not rows:
            continue
        actual = sum(1 for r in rows if r["actual_up"]) / len(rows)
        mean_p = sum(r["upside"] for r in rows) / len(rows)
        print(f"    [{lo:.1f},{hi:.1f})  n={len(rows):4d}  mean_p={mean_p:.3f}  actual={actual:.3f}")

    print("  by conviction (distance from 0.5):")
    for floor in (0.0, 0.1, 0.2, 0.3):
        rows = [r for r in results if abs(r["upside"] - 0.5) >= floor]
        if not rows:
            continue
        hit = sum(1 for r in rows if r["correct"]) / len(rows)
        print(f"    |edge|>={floor:.1f}  n={len(rows):4d}  hit={hit:.3f}")


def run_symbol(symbol: str, days: int, torch, predictor) -> list[dict]:
    end = datetime.now(UTC)
    start = end - timedelta(days=days + 30)
    rows = fetch_klines(
        symbol, int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    )
    index = build_windows(rows)
    print(f"{symbol}: {len(rows)} hourly candles")

    results: list[dict] = []
    day = (end - timedelta(days=days)).astimezone(ET)
    while day < end.astimezone(ET) - timedelta(days=1):
        open_ms = noon_et_open_ms(day)
        nxt_ms = noon_et_open_ms(day + timedelta(days=1))
        i, j = index.get(open_ms), index.get(nxt_ms)
        day += timedelta(days=1)
        if i is None or j is None or i < INPUT_CANDLES:
            continue
        # Input ends with the last candle CLOSED before noon ET.
        window = rows[i - INPUT_CANDLES:i]
        ref_close = float(rows[i][4])
        settle_close = float(rows[j][4])
        upside = forecast(torch, predictor, window)
        actual_up = settle_close >= ref_close
        predicted_up = upside >= 0.5
        results.append({
            "upside": upside,
            "actual_up": actual_up,
            "correct": predicted_up == actual_up,
        })
        if len(results) % 25 == 0:
            print(f"  {symbol}: {len(results)} windows", flush=True)
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="DOGEUSDT,BNBUSDT")
    ap.add_argument("--days", type=int, default=365)
    args = ap.parse_args()

    torch, predictor = load_predictor()
    for symbol in args.symbols.split(","):
        started = time.monotonic()
        results = run_symbol(symbol.strip(), args.days, torch, predictor)
        print(f"\n== {symbol.strip()} 24h direction, noon ET to noon ET ==")
        score(results)
        print(f"  ({time.monotonic() - started:.0f}s)\n", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
