import math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import binomtest

BASE = Path(__file__).parent
SPLIT = pd.Timestamp("2025-10-18 14:00")

df = pd.read_csv(BASE / "btc_1h_flow.csv", parse_dates=["ts"])
df["up"] = (df.close > df.open).astype(int)
imb = 2 * df.taker_buy_base / df.volume - 1
z = (imb - imb.rolling(168).mean()) / imb.rolling(168).std()
move = df.close - df.open
size = move.abs() / move.abs().rolling(24).mean()

df["fz"] = (z * np.sign(move)).shift(1)
df["z_prev"] = z.shift(1)
df["size_prev"] = size.shift(1)
df["last_up"] = df.up.shift(1)
df["rev_win"] = (df.up != df.last_up).astype(int)
df = df.dropna(subset=["fz", "z_prev", "size_prev", "last_up"])
df = df[np.sign(df.fz) != 0]
disc, val = df[df.ts < SPLIT], df[df.ts >= SPLIT]
print(f"discovery n={len(disc)} {disc.ts.iloc[0]} -> {disc.ts.iloc[-1]} | validation n={len(val)} {val.ts.iloc[0]} -> {val.ts.iloc[-1]}")


def wilson(k, n, zc=1.96):
    p = k / n
    d = 1 + zc * zc / n
    c = (p + zc * zc / (2 * n)) / d
    h = zc * math.sqrt(p * (1 - p) / n + zc * zc / (4 * n * n)) / d
    return c - h, c + h


def fmt(s):
    k, n = int(s.sum()), len(s)
    lo, hi = wilson(k, n)
    return f"{k / n:5.1%} [{lo:4.1%},{hi:4.1%}] n={n:>5} ({n / len_ref[0]:4.0%} of hours)"


q_fz = disc.fz.quantile([0.2, 0.4, 0.6, 0.8]).values
q_absz = disc.z_prev.abs().quantile(0.8)
q_size = disc.size_prev.quantile(0.8)
print(f"cutoffs (discovery): fz quintiles {np.round(q_fz, 2)} | |z| top quintile >= {q_absz:.2f} | move size top quintile >= {q_size:.2f}x")

len_ref = [0]
for name, part in [("discovery", disc), ("validation", val)]:
    len_ref[0] = len(part)
    print(f"\n=== {name}: reversal bet, all hours {fmt(part.rev_win)}")
    print("  1) dose-response by flow-in-move-direction quintile (1 = move fought by flow, 5 = move pushed by flow)")
    edges = [-np.inf, *q_fz, np.inf]
    for i in range(5):
        s = part[(part.fz > edges[i]) & (part.fz <= edges[i + 1])]
        print(f"     Q{i + 1}: {fmt(s.rev_win)}")

tests = {}
for name, part in [("discovery", disc), ("validation", val)]:
    len_ref[0] = len(part)
    h1 = part.fz > q_fz[3]
    h2 = h1 & (part.size_prev > q_size)
    h3 = part.z_prev.abs() > q_absz
    h3_win = (part.up != (part.z_prev > 0).astype(int)).astype(int)
    h4 = part.fz <= q_fz[0]
    base = part.rev_win.mean()
    print(f"\n=== {name}")
    for label, mask, wins in [
        ("H1 flow-driven move -> bet reversal", h1, part.rev_win),
        ("   ...all other hours", ~h1, part.rev_win),
        ("H2 flow-driven BIG move -> bet reversal", h2, part.rev_win),
        ("H3 strong flow alone -> bet against flow", h3, h3_win),
        ("H4 move fought by flow -> bet reversal", h4, part.rev_win),
    ]:
        w = wins[mask]
        line = f"  {label:42s} {fmt(w)}"
        if name == "discovery" and not label.startswith("   "):
            ref = 0.5 if label.startswith("H3") else base
            p = binomtest(int(w.sum()), len(w), ref).pvalue
            tests[label] = p
            line += f" p={p:.4f} vs {ref:.1%}"
        print(line)

labels = list(tests)
p = np.array([tests[l] for l in labels])
order = np.argsort(p)
m = len(p)
passed = np.zeros(m, bool)
ok = p[order] <= 0.10 * np.arange(1, m + 1) / m
if ok.any():
    passed[order[: np.max(np.where(ok)[0]) + 1]] = True
print("\nFDR (q=0.10) in discovery:", {l.split()[0]: bool(x) for l, x in zip(labels, passed)})
print("reference: taker break-even ~52.25% at 50c; maker 50%")
