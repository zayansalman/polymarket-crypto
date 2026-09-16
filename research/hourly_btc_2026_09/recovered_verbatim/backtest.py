import argparse
import json
import random
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
import torch

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE / "pylibs"))
sys.path.insert(0, str(BASE / "Kronos-upstream"))
from model import Kronos, KronosPredictor, KronosTokenizer  # noqa: E402

HOUR_MS = 3_600_000
OOS_START = pd.Timestamp("2025-10-18 14:00")  # first candle never used in fine-tuning or checkpoint selection
LOOKBACK = 512
COLS = ["open", "high", "low", "close", "volume", "amount"]

p = argparse.ArgumentParser()
p.add_argument("--hours", type=int, default=500)
p.add_argument("--paths", type=int, default=50)
p.add_argument("--chunk", type=int, default=25)
p.add_argument("--top_p", type=float, default=1.0)
p.add_argument("--seed", type=int, default=7)
p.add_argument("--out", default=str(BASE / "backtest_results.jsonl"))
args = p.parse_args()


def fetch_klines():
    cache = BASE / "btc_1h_oos.csv"
    if cache.exists():
        return pd.read_csv(cache, parse_dates=["ts"])
    start = int((OOS_START - pd.Timedelta(hours=LOOKBACK + 24)).timestamp() * 1000)
    now_ms = int(time.time() * 1000)
    rows = []
    while start < now_ms:
        url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&startTime={start}&limit=1000"
        batch = json.load(urllib.request.urlopen(url, timeout=30))
        if not batch:
            break
        rows.extend(batch)
        start = batch[-1][0] + HOUR_MS
        time.sleep(0.2)
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "close_time", "amount", "trades", "tbb", "tbq", "ignore"])
    df = df[df["close_time"] < now_ms].drop_duplicates("open_time").sort_values("open_time")
    for c in COLS:
        df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms")
    df = df[["open_time", "ts"] + COLS].reset_index(drop=True)
    df.to_csv(cache, index=False)
    return df


df = fetch_klines()
ts_to_idx = {t: i for i, t in enumerate(df["open_time"])}

eligible = []
for i in range(LOOKBACK, len(df)):
    if df["ts"].iloc[i] < OOS_START:
        continue
    if df["open_time"].iloc[i] - df["open_time"].iloc[i - LOOKBACK] != LOOKBACK * HOUR_MS:
        continue  # gap in the 512-hour context
    eligible.append(i)
targets = sorted(random.Random(args.seed).sample(eligible, min(args.hours, len(eligible))))

done = set()
out_path = Path(args.out)
if out_path.exists():
    done = {json.loads(line)["target_open_time"] for line in out_path.open()}

print(f"candles={len(df)} {df['ts'].iloc[0]} -> {df['ts'].iloc[-1]} | eligible OOS hours={len(eligible)} | sampled={len(targets)} | already done={len(done)}", flush=True)

tok = KronosTokenizer.from_pretrained(str(BASE / "weights/tokenizer")).eval()
mdl = Kronos.from_pretrained(str(BASE / "weights/model")).eval()
pred = KronosPredictor(mdl, tok, device="cpu", max_context=LOOKBACK)

t_start = time.perf_counter()
n_new = 0
with out_path.open("a") as out:
    for k, i in enumerate(targets):
        tgt = df.iloc[i]
        if int(tgt["open_time"]) in done:
            continue
        torch.manual_seed(args.seed * 100_000 + i)
        hist = df.iloc[i - LOOKBACK:i].reset_index(drop=True)
        x_df, x_ts, y_ts = hist[COLS], hist["ts"], pd.Series([tgt["ts"]])

        t0 = time.perf_counter()
        closes, opens = [], []
        left = args.paths
        while left > 0:
            b = min(args.chunk, left)
            outs = pred.predict_batch([x_df] * b, [x_ts] * b, [y_ts] * b, pred_len=1, T=1.0, top_k=0, top_p=args.top_p, sample_count=1, verbose=False)
            closes += [float(o["close"].iloc[0]) for o in outs]
            opens += [float(o["open"].iloc[0]) for o in outs]
            left -= b
        closes, opens = np.array(closes), np.array(opens)
        fc_s = time.perf_counter() - t0

        # tokenizer round-trip on the real context: how blurry are decoded prices?
        x = hist[COLS].values.astype(np.float32)
        mu, sd = x.mean(0), x.std(0)
        xn = torch.from_numpy(np.clip((x - mu) / (sd + 1e-5), -5, 5))[None]
        with torch.no_grad():
            toks = tok.encode(xn, half=True)
            rec = tok.decode([toks[0], toks[1]], half=True)[0].numpy() * (sd + 1e-5) + mu
        rec_err_close = rec[:, 3] - x[:, 3]
        rec_err_move = (rec[:, 3] - rec[:, 0]) - (x[:, 3] - x[:, 0])

        real_open, real_close = float(tgt["open"]), float(tgt["close"])
        prev = hist.iloc[-1]
        row = {
            "target_open_time": int(tgt["open_time"]),
            "target_ts": str(tgt["ts"]),
            "real_open": real_open,
            "real_close": real_close,
            "up": int(real_close > real_open),
            "prev_up": int(prev["close"] > prev["open"]),
            "p_up_vs_real_open": float((closes > real_open).mean()),
            "p_up_within_path": float((closes > opens).mean()),
            "pred_close_median": float(np.median(closes)),
            "pred_open_median": float(np.median(opens)),
            "pred_close_p10_p90": [float(v) for v in np.percentile(closes, [10, 90])],
            "context_std_close": float(sd[3]),
            "rec_close_mae": float(np.abs(rec_err_close).mean()),
            "rec_close_bias": float(rec_err_close.mean()),
            "rec_move_mae": float(np.abs(rec_err_move).mean()),
            "rec_move_bias": float(rec_err_move.mean()),
            "real_move_mae": float(np.abs(x[:, 3] - x[:, 0]).mean()),
            "paths": args.paths,
            "top_p": args.top_p,
            "forecast_s": round(fc_s, 2),
        }
        out.write(json.dumps(row) + "\n")
        out.flush()
        n_new += 1
        if n_new % 10 == 0 or n_new == 1:
            el = time.perf_counter() - t_start
            remaining = len(targets) - len(done) - n_new
            print(f"{len(done) + n_new}/{len(targets)} done | {el / n_new:.1f}s/hour | ~{remaining * el / n_new / 60:.0f} min left", flush=True)

print("FINISHED", flush=True)
