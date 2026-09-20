"""Forecast the 24 hours after each noon-ET anchor with the lc2004 Kronos BTCUSDT 1h fine-tune.

Question from Zayan (operator), 2026-09-17: the fine-tune was trained and tested on hourly data;
how does it do on 24-hour predictions? Design: PREREG.md (written before any forecast ran).

For every New York date D from 2025-10-18 (the first noon-ET anchor after the fine-tune's
training data ends, 2025-10-18 13:00 UTC) the script:
1. takes the 512 closed Binance spot BTCUSDT 1h candles before 12:00 ET on D;
2. samples 30 paths of the next K hourly candles, K = hours from noon ET on D to noon ET on D+1
   (24, or 23/25 across a clock change);
3. writes, per anchor, every path's closes and the share of paths above the last close.

Anchors are processed in a spread-out order (golden-ratio sequence), so a partial run is an
even sample of the whole period. Re-running skips dates already written.

Model settings follow the fine-tune author's own forecast script (github.com/Liucong-JunZi/
Kronos-Btc-finetune @ 2eef54e, btc_1h_prediction.py: 512 input candles, T=1.0, top_p=0.9) and
its training log (lookback 512, predict window 48). Independent paths as in the hourly test
(research/hourly_btc_2026_09/recovered_verbatim/backtest.py): predict_batch over N identical
inputs with sample_count=1. 30 paths, the count the Kronos team's live BTCUSDT 24h demo uses
(research/kronos_mini_official_btcusdt_demo on branch
research/kronos-mini-official-btcusdt-24h-demo-recreation). Claude, 2026-09-17.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
DATA = HERE / "data"
HOUR_MS = 3_600_000
LOOKBACK = 512
TRAINING_DATA_LAST_OPEN_MS = int(datetime(2025, 10, 18, 13, tzinfo=UTC).timestamp() * 1000)
SAMPLING = {"T": 1.0, "top_k": 0, "top_p": 0.9, "sample_count": 1}
SEED = 20260917
COLS = ["open", "high", "low", "close", "volume", "amount"]

ap = argparse.ArgumentParser()
ap.add_argument("--kronos-code", required=True,
                help="folder holding model/ from github.com/shiyu-coder/Kronos @ 67b630e")
ap.add_argument("--weights", default=str(DATA / "weights"),
                help="folder with model/ and tokenizer/ (config.json + model.safetensors)")
ap.add_argument("--paths", type=int, default=30)
ap.add_argument("--chunk", type=int, default=15)
ap.add_argument("--threads", type=int, default=4)
ap.add_argument("--limit", type=int, default=0, help="stop after this many new anchors (0 = all)")
ap.add_argument("--out", default=str(DATA / "forecasts.jsonl"))
args = ap.parse_args()

sys.path.insert(0, args.kronos_code)
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import torch  # noqa: E402
from model import Kronos, KronosPredictor, KronosTokenizer  # noqa: E402

hourly = pd.read_csv(DATA / "btcusdt_1h_spot.csv").rename(columns={"quote_volume": "amount"})
row_by_open = {int(t): i for i, t in enumerate(hourly["open_time_ms"])}
noon = pd.read_csv(DATA / "noon_et_1m_closes.csv")
noon_by_date = {d: (int(ms), float(c)) for d, ms, c in
                zip(noon["et_date"], noon["noon_open_time_ms"], noon["close"])}

anchors = []
for d, (ms, close_d) in noon_by_date.items():
    nxt = (date.fromisoformat(d) + timedelta(days=1)).isoformat()
    if nxt not in noon_by_date:
        continue  # D+1 noon has not happened yet
    ms_next, close_d1 = noon_by_date[nxt]
    k = (ms_next - ms) // HOUR_MS
    i = row_by_open.get(ms)
    if i is None or i < LOOKBACK or row_by_open.get(ms + (k - 1) * HOUR_MS) is None:
        continue
    if int(hourly["open_time_ms"].iloc[i - LOOKBACK]) != ms - LOOKBACK * HOUR_MS:
        continue  # gap inside the 512-hour input
    assert ms > TRAINING_DATA_LAST_OPEN_MS, "forecast hours must be after the training data"
    anchors.append({"et_date": d, "anchor_ms": ms, "k": int(k), "row": i,
                    "noon_close_d": close_d, "noon_close_d1": close_d1})

order = sorted(range(len(anchors)), key=lambda j: (j * 0.6180339887498949) % 1.0)
out_path = Path(args.out)
done = set()
if out_path.exists():
    done = {json.loads(line)["et_date"] for line in out_path.open()}
todo = [anchors[j] for j in order if anchors[j]["et_date"] not in done]
if args.limit:
    todo = todo[:args.limit]
print(f"anchors={len(anchors)} ({anchors[0]['et_date']} to {anchors[-1]['et_date']}) "
      f"done={len(done)} this run={len(todo)}", flush=True)

torch.set_num_threads(args.threads)
weights = Path(args.weights)
tok = KronosTokenizer.from_pretrained(str(weights / "tokenizer")).eval()
mdl = Kronos.from_pretrained(str(weights / "model")).eval()
predictor = KronosPredictor(mdl, tok, device="cpu", max_context=LOOKBACK)

t_run = time.perf_counter()
with out_path.open("a") as out:
    for n, a in enumerate(todo, 1):
        i, k = a["row"], a["k"]
        ctx = hourly.iloc[i - LOOKBACK:i]
        x_df = ctx[COLS].reset_index(drop=True)
        x_ts = pd.Series(pd.to_datetime(ctx["open_time_ms"].to_numpy(), unit="ms"))
        y_ts = pd.Series(pd.date_range(x_ts.iloc[-1] + pd.Timedelta(hours=1), periods=k, freq="h"))
        seed = SEED + int(a["et_date"].replace("-", ""))
        torch.manual_seed(seed)

        t0 = time.perf_counter()
        paths: list[np.ndarray] = []
        while len(paths) < args.paths:
            b = min(args.chunk, args.paths - len(paths))
            outs = predictor.predict_batch([x_df] * b, [x_ts] * b, [y_ts] * b, pred_len=k,
                                           verbose=False, **SAMPLING)
            paths += [o["close"].to_numpy(dtype=float) for o in outs]
        seconds = time.perf_counter() - t0

        closes = np.vstack(paths)  # paths x k
        last_close = float(ctx["close"].iloc[-1])
        p_by_step = (closes > last_close).mean(axis=0)
        actual = hourly["close"].iloc[i:i + k].to_numpy(dtype=float)
        row = {
            "et_date": a["et_date"],
            "anchor_utc": datetime.fromtimestamp(a["anchor_ms"] / 1000, UTC).isoformat(),
            "k": k,
            "last_close": last_close,
            "noon_close_d": a["noon_close_d"],
            "noon_close_d1": a["noon_close_d1"],
            "p_up": float(p_by_step[-1]),
            "p_up_by_step": [float(v) for v in p_by_step],
            "actual_close_by_step": [float(v) for v in actual],
            "median_close_by_step": [float(v) for v in np.median(closes, axis=0)],
            "path_closes": np.round(closes, 2).tolist(),
            "paths": args.paths,
            "sampling": SAMPLING,
            "lookback": LOOKBACK,
            "seed": seed,
            "seconds": round(seconds, 1),
        }
        out.write(json.dumps(row) + "\n")
        out.flush()
        per = (time.perf_counter() - t_run) / n
        print(f"{len(done) + n}/{len(anchors)} {a['et_date']} p_up={row['p_up']:.2f} "
              f"{seconds:.0f}s | ~{(len(todo) - n) * per / 3600:.1f} h left", flush=True)

print("FINISHED", flush=True)
