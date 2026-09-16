"""Recreate the Kronos team's published BTCUSDT 24-hour forecast for given hours.

Faithful to shiyu-coder/Kronos-demo update_predictions.py at commit eba16695 (2025-09-16):
model NeoQuasar/Kronos-mini (weights unchanged since 2025-07-01) with
NeoQuasar/Kronos-Tokenizer-2k, both in eval mode on CPU, max_context 512; input = the
383 closed Binance spot BTCUSDT 1h candles before hour A (the demo fetched 384 and dropped
the forming one), columns open/high/low/close/volume/amount(quote volume), naive UTC
open-time timestamps; 24 predicted hours; T=1.0, top_p=0.95, top_k=0, 30 sampled paths.
upside = share of paths whose hour A+23 close is above the last closed close (strictly).
Model code = the demo repo's own model/ package at eba16695 (data/kronos_demo_code).
The demo seeded nothing; here each hour is seeded with its epoch hour so reruns repeat.

Usage: recreate_tsinghua_kronos_btc_24h_forecast.py OUT.csv ANCHOR_ISO [ANCHOR_ISO ...]
   or: recreate_tsinghua_kronos_btc_24h_forecast.py OUT.csv --every N [--start ISO --end ISO]
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "data" / "kronos_demo_code"))
sys.path.insert(0, str(HERE / "data" / "pylibs"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import torch  # noqa: E402

from model import Kronos, KronosPredictor, KronosTokenizer  # noqa: E402

CACHE = HERE / "data" / "hf_cache"
MODEL = CACHE / "models--NeoQuasar--Kronos-mini/snapshots/f4e68697d9d5aed55cef5c96aabc3376bcad9f81"
TOKENIZER = CACHE / "models--NeoQuasar--Kronos-Tokenizer-2k/snapshots/26966d0035065a0cae0ebad7af8ece35bc1fb51c"
INPUT_CANDLES = 383
HORIZON = 24
PATHS = 30


def load_candles() -> pd.DataFrame:
    kl = pd.read_csv(HERE / "data" / "btcusdt_1h_spot.csv")
    kl["timestamps"] = pd.to_datetime(kl["open_time_ms"], unit="ms")
    kl = kl.rename(columns={"quote_volume": "amount"})
    return kl.set_index("timestamps", drop=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("anchors", nargs="*")
    ap.add_argument("--every", type=int, default=0)
    ap.add_argument("--start", default="2025-09-16 03:00")
    ap.add_argument("--end", default="2026-07-04 12:00")
    args = ap.parse_args()
    torch.set_num_threads(4)

    kl = load_candles()
    if args.every:
        anchors = pd.date_range(args.start, args.end, freq=f"{args.every}h")
    else:
        anchors = pd.to_datetime(args.anchors)

    tokenizer = KronosTokenizer.from_pretrained(str(TOKENIZER))
    model = Kronos.from_pretrained(str(MODEL))
    tokenizer.eval()
    model.eval()
    predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=512)

    out = Path(args.out)
    done = set()
    if out.exists():
        done = set(pd.read_csv(out)["anchor"])
    new_file = not out.exists()
    with out.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["anchor", "last_close", "upside_prob", "mean_final_close", "seconds"])
        for a in anchors:
            key = a.strftime("%Y-%m-%d %H:%M:%S")
            if key in done:
                continue
            hist = kl.loc[a - pd.Timedelta(hours=INPUT_CANDLES): a - pd.Timedelta(hours=1)]
            if len(hist) != INPUT_CANDLES:
                continue
            y_ts = pd.Series(pd.date_range(a, periods=HORIZON, freq="h"), name="y_timestamp")
            torch.manual_seed(int(a.timestamp()) // 3600)
            t0 = time.time()
            with torch.no_grad():
                close_df, _ = predictor.predict(
                    df=hist[["open", "high", "low", "close", "volume", "amount"]].reset_index(drop=True),
                    x_timestamp=hist["timestamps"].reset_index(drop=True),
                    y_timestamp=y_ts, pred_len=HORIZON, T=1.0, top_p=0.95,
                    sample_count=PATHS, verbose=False,
                )
            last_close = float(hist["close"].iloc[-1])
            final = close_df.iloc[-1]
            w.writerow([key, last_close, float((final > last_close).mean()),
                        float(final.mean()), round(time.time() - t0, 2)])
            f.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
