import argparse
import json
import resource
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE / "pylibs"))
sys.path.insert(0, str(BASE / "Kronos-upstream"))
from model import Kronos, KronosPredictor, KronosTokenizer  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("--device", default="cpu")
p.add_argument("--n", type=int, default=1, help="independent forecast paths")
p.add_argument("--chunk", type=int, default=100, help="paths per model call")
p.add_argument("--lookback", type=int, default=512)
p.add_argument("--repeats", type=int, default=2)
args = p.parse_args()

torch.manual_seed(0)

k = json.load(open(BASE / "btc_1h_latest.json"))
cols = ["open_time", "open", "high", "low", "close", "volume", "close_time", "amount", "trades", "tb_base", "tb_quote", "ignore"]
df = pd.DataFrame(k, columns=cols)
for c in ["open", "high", "low", "close", "volume", "amount"]:
    df[c] = df[c].astype(float)
df["ts"] = pd.to_datetime(df["open_time"], unit="ms")

target = df.iloc[-1]
hist = df.iloc[:-1].tail(args.lookback).reset_index(drop=True)
x_df = hist[["open", "high", "low", "close", "volume", "amount"]]
x_ts = hist["ts"]
y_ts = pd.Series([target["ts"]])

t0 = time.perf_counter()
tok = KronosTokenizer.from_pretrained(str(BASE / "weights/tokenizer"))
mdl = Kronos.from_pretrained(str(BASE / "weights/model"))
was_training = (tok.training, mdl.training)
tok.eval()
mdl.eval()
pred = KronosPredictor(mdl, tok, device=args.device, max_context=512)
load_s = time.perf_counter() - t0


def sync():
    if args.device == "mps":
        torch.mps.synchronize()


def run_paths():
    closes = []
    left = args.n
    while left > 0:
        b = min(args.chunk, left)
        out = pred.predict_batch([x_df] * b, [x_ts] * b, [y_ts] * b, pred_len=1, T=1.0, top_k=0, top_p=0.9, sample_count=1, verbose=False)
        closes.extend(float(o["close"].iloc[0]) for o in out)
        left -= b
    sync()
    return np.array(closes)


timings = []
closes = None
for _ in range(args.repeats):
    t = time.perf_counter()
    closes = run_paths()
    timings.append(time.perf_counter() - t)

open_ = float(target["open"])
res = {
    "device": args.device,
    "n": args.n,
    "chunk": args.chunk,
    "lookback": len(hist),
    "load_s": round(load_s, 2),
    "first_call_s": round(timings[0], 2),
    "steady_call_s": round(min(timings[1:]) if len(timings) > 1 else timings[0], 2),
    "ru_maxrss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1048576, 0),
    "mps_driver_mb": round(torch.mps.driver_allocated_memory() / 1048576, 0) if args.device == "mps" else None,
    "loaded_in_training_mode": was_training,
    "forecast_hour_utc": str(target["ts"]),
    "context_ends_utc": str(hist["ts"].iloc[-1]),
    "hour_open": open_,
    "unique_paths": int(len(np.unique(closes))),
    "p_up": round(float((closes > open_).mean()), 3),
    "close_p5_p50_p95": [round(float(v), 1) for v in np.percentile(closes, [5, 50, 95])],
}
print("RESULT " + json.dumps(res))
