import math
from pathlib import Path
import numpy as np, pandas as pd
from scipy.stats import binomtest

B = Path(__file__).parent
VAL = pd.Timestamp("2025-10-18 14:00")

def hourly(name):
    d = pd.read_csv(B / name, parse_dates=["ts"])
    imb = 2 * d.taker_buy_base / d.volume - 1
    d["z"] = (imb - imb.rolling(168).mean()) / imb.rolling(168).std()
    d["dir"] = np.sign(d.close - d.open)
    d["fz"] = d.z * d.dir
    return d

s = hourly("btc_1h_flow.csv")
p = hourly("btc_perp_1h_flow.csv")[["ts", "fz"]].rename(columns={"fz": "p_fz"})
oi = pd.read_csv(B / "bybit_oi_1h.csv", parse_dates=["ts"])[["ts", "oi"]]
df = s.merge(p, on="ts").merge(oi, on="ts", how="left")
df["up"] = (df.close > df.open).astype(int)
rng = (df.high - df.low).replace(0, np.nan)
df["clv"] = (2 * df.close - df.high - df.low) / rng
lr = np.log(df.oi / df.oi.shift(1))          # at row H: change from OI[H-1] to OI[H] = during H-1
lrz = (lr - lr.rolling(168).mean()) / lr.rolling(168).std()
df["doi_now"] = lrz                          # known ~1-2 min after H
df["doi_lag"] = lrz.shift(1)                 # change during H-2, known at H
for c in ["fz", "p_fz", "dir", "clv"]:
    df[c + "_1"] = df[c].shift(1)
df = df.iloc[400:].dropna(subset=["fz_1", "p_fz_1", "dir_1", "clv_1", "doi_now", "doi_lag"])
df = df[df.dir_1 != 0].copy()
df["win"] = (df.up == (df.dir_1 < 0).astype(int)).astype(int)
df["month"] = df.ts.dt.to_period("M")

months = [m for m in sorted(df.month.unique()) if pd.Period("2024-04") <= m <= pd.Period("2026-08")]
masks = {f"R{i}": [] for i in range(1, 9)}
for m in months:
    hist, cur = df[df.month < m], df[df.month == m]
    q80, q90, q95 = hist.fz_1.quantile([0.8, 0.9, 0.95])
    pq80 = hist.p_fz_1.quantile(0.8)
    def clv_ext(t):
        return ((cur.dir_1 > 0) & (cur.clv_1 > t)) | ((cur.dir_1 < 0) & (cur.clv_1 < -t))
    A = (cur.fz_1 > q80) & (cur.p_fz_1 <= pq80) & clv_ext(0.8)
    rules = {
        "R1": A,
        "R2": (cur.fz_1 > q90) & (cur.p_fz_1 <= pq80) & clv_ext(0.9),
        "R3": (cur.fz_1 > q95) & (cur.p_fz_1 <= pq80) & clv_ext(0.9),
        "R4": A & (cur.doi_now > 0), "R5": A & (cur.doi_now < 0),
        "R6": A & (cur.doi_lag > 0), "R7": A & (cur.doi_lag < 0),
        "R8": (cur.fz_1 > q80) & (cur.doi_now < 0),
    }
    for k, v in rules.items():
        masks[k].append(cur.index[v])

names = {"R1": "Tier A", "R2": "Strict A90", "R3": "Strict A95", "R4": "A & OI rose (H-1, delayed)",
         "R5": "A & OI fell (H-1, delayed)", "R6": "A & OI rose (H-2)", "R7": "A & OI fell (H-2)",
         "R8": "Flow push & OI fell (H-1, delayed)"}

def w(x):
    n = len(x)
    if n == 0:
        return (float("nan"), float("nan"), float("nan"), 0, "n=0")
    k = int(x.sum()); ph = k / n; z = 1.96; d = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / d; h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
    return ph, c - h, c + h, n, f"{ph:5.1%} [{c-h:4.1%},{c+h:4.1%}] n={n}"

oos_all = df[df.month.isin(months)]
print(f"OOS months {months[0]}..{months[-1]} | all-hours reversal {w(oos_all.win)[4]}")
pvals, rows = [], []
for k in masks:
    idx = masks[k][0].append(masks[k][1:]) if len(masks[k]) > 1 else masks[k][0]
    sel = df.loc[idx]
    ph, lo, hi, n, txt = w(sel.win)
    val = sel[sel.ts >= VAL]
    vph, vlo, vhi, vn, vtxt = w(val.win)
    years = " ".join(f"{y}:{g.win.mean():.0%}/{len(g)}" for y, g in sel.groupby(sel.ts.dt.year))
    pv = binomtest(int(sel.win.sum()), n, 0.523, alternative="greater").pvalue if n else 1.0
    pvals.append(pv)
    met = n >= 150 and ph >= 0.60 and lo >= 0.55 and vph >= 0.58
    rows.append((k, names[k], txt, vtxt, years, pv, met))
order = np.argsort(pvals); m_ = len(pvals); passed = np.zeros(m_, bool)
ok = np.array(pvals)[order] <= 0.10 * np.arange(1, m_ + 1) / m_
if ok.any():
    passed[order[: np.max(np.where(ok)[0]) + 1]] = True
for (k, nm, txt, vtxt, years, pv, met), fdr in zip(rows, passed):
    print(f"{k} {nm:36s} | OOS {txt:28s} | val {vtxt:28s} | FDR {fdr} | 60% bar {met}\n     years {years}")
