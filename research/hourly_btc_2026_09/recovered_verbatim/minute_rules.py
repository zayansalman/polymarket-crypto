import math
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).parent
SPLIT = pd.Timestamp("2025-10-18 14:00")

q = pd.read_csv(BASE / "btc_15m_spot.csv", parse_dates=["ts"])
q["hour"] = q.ts.dt.floor("h")
q["minute"] = q.ts.dt.minute
last = q[q.minute == 45].set_index("hour")[["open", "close", "volume", "taker_buy_base"]].rename(
    columns={"open": "o45", "close": "c59", "volume": "v15", "taker_buy_base": "tb15"}
)
full = q.groupby("hour").agg(ho=("open", "first"), hc=("close", "last"), cnt=("minute", "size"))
last = last.join(full)
last = last[last.cnt == 4]
last.index.name = "ts"


def hourly(name):
    d = pd.read_csv(BASE / name, parse_dates=["ts"])
    imb = 2 * d.taker_buy_base / d.volume - 1
    d["z"] = (imb - imb.rolling(168).mean()) / imb.rolling(168).std()
    d["dir"] = np.sign(d.close - d.open)
    d["fz"] = d.z * d.dir
    return d


s = hourly("btc_1h_flow.csv")
p = hourly("btc_perp_1h_flow.csv")[["ts", "fz"]].rename(columns={"fz": "p_fz"})
df = s.merge(p, on="ts").merge(last, left_on="ts", right_index=True, how="left")
chk = df.dropna(subset=["ho"])
print(f"sanity: 1m-built hour open/close match hourly klines: {np.mean(np.isclose(chk.ho, chk.open) & np.isclose(chk.hc, chk.close)):.4%} of {len(chk)} hours")

df["up"] = (df.close > df.open).astype(int)
df["m15"] = df.c59 / df.o45 - 1
imb15 = 2 * df.tb15 / df.v15 - 1
df["z15"] = (imb15 - imb15.rolling(168, min_periods=120).mean()) / imb15.rolling(168, min_periods=120).std()
df["fz15"] = df.z15 * np.sign(df.m15)
rng = (df.high - df.low).replace(0, np.nan)
df["clv"] = (2 * df.close - df.high - df.low) / rng

for c in ["fz", "p_fz", "dir", "m15", "fz15", "clv"]:
    df[c + "_1"] = df[c].shift(1)
df = df.iloc[300:].dropna(subset=["fz_1", "p_fz_1", "dir_1", "m15_1", "fz15_1", "clv_1"])
df = df[(df.dir_1 != 0) & (df.m15_1 != 0)].copy()
disc = df.ts < SPLIT
q_s, q_p, q15 = df[disc].fz_1.quantile(0.8), df[disc].p_fz_1.quantile(0.8), df[disc].fz15_1.quantile(0.8)
print(f"cutoffs (discovery): spot fz>{q_s:.2f} perp fz>{q_p:.2f} fz15>{q15:.2f} | rows {len(df)}")

sig = df.fz_1 > q_s
spot_only = sig & (df.p_fz_1 <= q_p)
against_hour_up = (df.dir_1 < 0).astype(int)
against_15_up = (df.m15_1 < 0).astype(int)
clv_ext = ((df.dir_1 > 0) & (df.clv_1 > 0.8)) | ((df.dir_1 < 0) & (df.clv_1 < -0.8))
r2 = df.fz15_1 > q15
rules = {
    "R0 existing SIG (reference)": (sig, against_hour_up),
    "R1 close at extreme": (clv_ext, against_hour_up),
    "R2 last-15m flow-pushed": (r2, against_15_up),
    "R3 SIG + last15 continued with flow": (sig & (np.sign(df.m15_1) == df.dir_1) & (df.fz15_1 > 0), against_hour_up),
    "R4 SIG + last15 already reversing": (sig & (np.sign(df.m15_1) != df.dir_1), against_hour_up),
    "R5 SPOT_ONLY + close at extreme": (spot_only & clv_ext, against_hour_up),
    "R6 R2 + SIG same direction": (r2 & sig & (np.sign(df.m15_1) == df.dir_1), against_hour_up),
    "R7 R1 + SPOT_ONLY": (clv_ext & spot_only, against_hour_up),
    "   SPOT_ONLY (reference)": (spot_only, against_hour_up),
}


def fmt(x):
    n = len(x)
    if n == 0:
        return "n=0".ljust(30)
    k = int(x.sum())
    ph = k / n
    z = 1.96
    d = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / d
    h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
    return f"{ph:5.1%} [{c - h:4.1%},{c + h:4.1%}] n={n:<5}"


print(f"\n{'rule':40s} | {'discovery 2023-25':34s} | {'validation 2025-26':34s} | share of val hours | 60% bar")
for name, (mask, bet_up) in rules.items():
    win = (df.up == bet_up).astype(int)
    dv, vv = win[mask & disc], win[mask & ~disc]
    k, n = int(vv.sum()), len(vv)
    ok = False
    if n >= 150:
        ph = k / n
        z = 1.96
        d = 1 + z * z / n
        lo = (ph + z * z / (2 * n)) / d - z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
        ok = ph >= 0.60 and lo >= 0.55
    print(f"{name:40s} | {fmt(dv):34s} | {fmt(vv):34s} | {n / max(int((~disc).sum()), 1):6.1%} | {ok}")
