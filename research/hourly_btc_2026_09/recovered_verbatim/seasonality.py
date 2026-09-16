import math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.cluster.vq import kmeans2
from scipy.stats import binomtest, spearmanr

BASE = Path(__file__).parent
SPLIT = pd.Timestamp("2025-10-18 14:00")

df = pd.read_csv(BASE / "btc_1h_full.csv", parse_dates=["ts"])
df["up"] = (df.close > df.open).astype(int)
df["last_up"] = df.up.shift(1)
df["rev_win"] = (df.up != df.last_up).astype(int)
df["move"] = (df.close - df.open).abs() / df.open
df["ret"] = df.close / df.open - 1
df["vol_share"] = df.volume / df.volume.rolling(24 * 7, min_periods=24).mean()
df = df.iloc[1:].copy()
df["hour_utc"] = df.ts.dt.hour
df["hour_et"] = df.ts.dt.tz_localize("UTC").dt.tz_convert("America/New_York").dt.hour
df["weekday"] = df.ts.dt.weekday
df["weekend"] = (df.weekday >= 5).astype(int)
df["how"] = df.weekday * 24 + df.hour_utc
df["funding"] = df.hour_utc.isin([0, 8, 16]).astype(int)
disc, val = df[df.ts < SPLIT], df[df.ts >= SPLIT]


def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def bh(pvals, q=0.10):
    p = np.asarray(pvals)
    order = np.argsort(p)
    m = len(p)
    thresh = q * (np.arange(1, m + 1)) / m
    passed = p[order] <= thresh
    k = np.max(np.where(passed)[0]) + 1 if passed.any() else 0
    out = np.zeros(m, bool)
    out[order[:k]] = True
    return out


def fmt(k, n):
    lo, hi = wilson(k, n)
    return f"{k / n:5.1%} [{lo:4.1%},{hi:4.1%}] n={n}"


base_up, base_rev = disc.up.mean(), disc.rev_win.mean()
print(f"discovery {disc.ts.iloc[0]} -> {disc.ts.iloc[-1]} n={len(disc)} | up {base_up:.1%} | reversal {base_rev:.1%}")
print(f"validation {val.ts.iloc[0]} -> {val.ts.iloc[-1]} n={len(val)} | up {val.up.mean():.1%} | reversal {val.rev_win.mean():.1%}")

print("\n=== 1+2) SLICES: FDR in discovery, then validation; stability across all cells")
for fam in ["hour_utc", "hour_et", "weekday", "weekend", "funding", "how"]:
    gd, gv = disc.groupby(fam), val.groupby(fam)
    cells = sorted(set(gd.groups) & set(gv.groups))
    for metric, base in [("up", base_up), ("rev_win", base_rev)]:
        pv = [binomtest(int(gd.get_group(c)[metric].sum()), len(gd.get_group(c)), base).pvalue for c in cells]
        passed = bh(pv)
        rd = [gd.get_group(c)[metric].mean() for c in cells]
        rv = [gv.get_group(c)[metric].mean() for c in cells]
        rho, prho = spearmanr(rd, rv)
        print(f"\n[{fam} / {metric}] cells={len(cells)} | FDR survivors in discovery: {int(passed.sum())} | discovery-vs-validation rank corr {rho:+.2f} (p={prho:.2f})")
        for i in np.where(passed)[0]:
            c = cells[i]
            a, b = gd.get_group(c), gv.get_group(c)
            vbase = val[metric].mean()
            lo, hi = wilson(int(b[metric].sum()), len(b))
            same_dir = (rd[i] - base) * (rv[i] - vbase) > 0
            holds = same_dir and (lo > vbase or hi < vbase)
            print(f"   cell {c}: discovery {fmt(int(a[metric].sum()), len(a))} p={pv[i]:.4f} | validation {fmt(int(b[metric].sum()), len(b))} | {'HOLDS' if holds else ('same direction, not significant' if same_dir else 'FLIPPED')}")
    md = [gd.get_group(c).move.mean() for c in cells]
    mv = [gv.get_group(c).move.mean() for c in cells]
    print(f"[{fam} / move size, positive control] rank corr {spearmanr(md, mv)[0]:+.2f}")

print("\n=== 3) CLUSTERING (profiles from discovery only)")


def profiles(frame, key):
    g = frame.groupby(key)
    return pd.DataFrame({"up": g.up.mean(), "rev_win": g.rev_win.mean(), "move": g.move.mean(), "vol_share": g.vol_share.mean(), "ret": g.ret.mean(), "n": g.size()})


def silhouette(X, lab):
    D = np.sqrt(((X[:, None, :] - X[None, :, :]) ** 2).sum(-1))
    s = []
    for i in range(len(X)):
        same = lab == lab[i]
        if same.sum() <= 1:
            s.append(0.0)
            continue
        a = D[i, same].sum() / (same.sum() - 1)
        b = min(D[i, lab == c].mean() for c in set(lab) if c != lab[i])
        s.append((b - a) / max(a, b))
    return float(np.mean(s))


for key, label in [("hour_utc", "24 UTC hours"), ("how", "168 hour x weekday cells")]:
    P = profiles(disc, key)
    feats = ["up", "rev_win", "move", "vol_share", "ret"]
    X = ((P[feats] - P[feats].mean()) / P[feats].std()).values
    best = None
    for k in range(2, 6):
        runs = []
        for seed in range(50):
            cent, lab = kmeans2(X, k, minit="++", seed=seed)
            inertia = sum(((X[lab == j] - cent[j]) ** 2).sum() for j in range(k))
            runs.append((inertia, lab))
        lab = min(runs, key=lambda r: r[0])[1]
        if len(set(lab)) < 2:
            continue
        sil = silhouette(X, lab)
        if best is None or sil > best[0]:
            best = (sil, k, lab)
    sil, k, lab = best
    ward = fcluster(linkage(X, "ward"), k, "maxclust")
    agree = np.mean([(lab[i] == lab[j]) == (ward[i] == ward[j]) for i in range(len(lab)) for j in range(i + 1, len(lab))])
    print(f"\n[{label}] k-means best k={k} (silhouette {sil:.2f}); pairwise agreement with Ward clustering {agree:.0%}")
    P["cluster"] = lab
    cmap = dict(zip(P.index, lab))
    for c in sorted(set(lab)):
        members = [m for m in P.index if cmap[m] == c]
        d_ = disc[disc[key].isin(members)]
        v_ = val[val[key].isin(members)]
        mem = members if key == "hour_utc" else f"{len(members)} cells"
        print(f"   cluster {c}: {mem}")
        print(f"      up      disc {fmt(int(d_.up.sum()), len(d_))} | val {fmt(int(v_.up.sum()), len(v_))}")
        print(f"      reversal disc {fmt(int(d_.rev_win.sum()), len(d_))} | val {fmt(int(v_.rev_win.sum()), len(v_))}")
        print(f"      avg move disc {d_.move.mean():.3%} | val {v_.move.mean():.3%}")

print("\n=== 4) HONEST SKIP-HOURS TEST (rules picked on discovery, applied to validation)")
for key in ["hour_utc", "hour_et", "how"]:
    P = profiles(disc, key)
    P["side_edge"] = (P.up - 0.5).abs()
    P["side"] = (P.up > 0.5).astype(int)
    top_side = P[P.side_edge >= P.side_edge.quantile(0.75)]
    v = val[val[key].isin(top_side.index)].copy()
    v["hit"] = (v.up == v[key].map(top_side.side)).astype(int)
    d = disc[disc[key].isin(top_side.index)].copy()
    d["hit"] = (d.up == d[key].map(top_side.side)).astype(int)
    print(f"[{key}] bet the discovery-favored side in top-quartile cells ({len(top_side)} cells): discovery {fmt(int(d.hit.sum()), len(d))} | validation {fmt(int(v.hit.sum()), len(v))}")
    top_rev = P[P.rev_win >= P.rev_win.quantile(0.75)]
    d2, v2 = disc[disc[key].isin(top_rev.index)], val[val[key].isin(top_rev.index)]
    print(f"[{key}] reversal bet only in top-quartile reversal cells ({len(top_rev)} cells): discovery {fmt(int(d2.rev_win.sum()), len(d2))} | validation {fmt(int(v2.rev_win.sum()), len(v2))} | vs reversal every hour in validation {fmt(int(val.rev_win.sum()), len(val))}")
