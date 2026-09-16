import math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import binom

BASE = Path(__file__).parent
SPLIT = pd.Timestamp("2025-10-18 14:00")


def flow(name, px):
    d = pd.read_csv(BASE / name, parse_dates=["ts"])
    imb = 2 * d.taker_buy_base / d.volume - 1
    out = pd.DataFrame({"ts": d.ts})
    out[px + "_z"] = (imb - imb.rolling(168).mean()) / imb.rolling(168).std()
    out[px + "_dir"] = np.sign(d.close - d.open)
    out[px + "_close"] = d.close
    out[px + "_open"] = d.open
    return out


df = flow("btc_1h_flow.csv", "s").merge(flow("btc_perp_1h_flow.csv", "p"), on="ts").merge(flow("eth_1h_flow.csv", "e")[["ts", "e_dir"]], on="ts")
df["up"] = (df.s_close > df.s_open).astype(int)
mv = (df.s_close - df.s_open).abs()
df["size_raw"] = np.log1p(mv / mv.rolling(24).mean())
b = df.p_close / df.s_close - 1
df["basis_z"] = (b - b.rolling(168).mean()) / b.rolling(168).std()

fund = pd.read_csv(BASE / "btc_funding.csv", parse_dates=["ts"]).sort_values("ts")
fund["ts"] = fund.ts.dt.floor("h")
fund["fz"] = (fund.rate - fund.rate.rolling(90).mean()) / fund.rate.rolling(90).std()
df = pd.merge_asof(df.sort_values("ts"), fund[["ts", "fz"]].rename(columns={"fz": "fund_z"}), on="ts", direction="backward", allow_exact_matches=True)

sgn = df.s_dir.shift(1)
X = pd.DataFrame({
    "ts": df.ts,
    "s_fz": (df.s_z * df.s_dir).shift(1),
    "p_fz": (df.p_z * df.p_dir).shift(1),
    "div": ((df.p_z - df.s_z).shift(1)) * sgn,
    "basis": df.basis_z.shift(1) * sgn,
    "fund": df.fund_z * sgn,
    "eth_same": (df.e_dir.shift(1) == sgn).astype(float),
    "us_open": (df.ts.dt.tz_localize("UTC").dt.tz_convert("America/New_York").dt.hour == 9).astype(float),
    "exp08": (df.ts.dt.hour == 8).astype(float),
    "weekend": (df.ts.dt.weekday >= 5).astype(float),
    "size": df.size_raw.shift(1),
    "sgn": sgn,
    "up": df.up,
})
X = X.iloc[300:].dropna()
X = X[X.sgn != 0].reset_index(drop=True)
X["rev"] = (X.up == (X.sgn < 0).astype(int)).astype(int)
FEATS = ["s_fz", "p_fz", "div", "basis", "fund", "eth_same", "us_open", "exp08", "weekend", "size"]
disc_m = X.ts < SPLIT


def wil(k, n, z=1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def fmt(s):
    k, n = int(np.sum(s)), len(s)
    if n == 0:
        return "n=0"
    lo, hi = wil(k, n)
    return f"{k / n:5.1%} [{lo:4.1%},{hi:4.1%}] n={n}"


print(f"rows {len(X)} | discovery {int(disc_m.sum())} | validation {int((~disc_m).sum())}")
print(f"all-hours reversal: discovery {fmt(X[disc_m].rev)} | validation {fmt(X[~disc_m].rev)}")

D = X[disc_m]
q_s, q_p, q_f = D.s_fz.quantile(0.8), D.p_fz.quantile(0.8), D.fund.quantile(0.8)
print("\n== univariate (discovery | validation)")
qs = D.p_fz.quantile([0.2, 0.4, 0.6, 0.8]).values
edges = [-np.inf, *qs, np.inf]
for i in range(5):
    m = (X.p_fz > edges[i]) & (X.p_fz <= edges[i + 1])
    print(f"  perp fz Q{i + 1}: {fmt(X[m & disc_m].rev)} | {fmt(X[m & ~disc_m].rev)}")
for name, m in [
    ("confluence spot&perp top quintile", (X.s_fz > q_s) & (X.p_fz > q_p)),
    ("spot-only push", (X.s_fz > q_s) & (X.p_fz <= q_p)),
    ("perp-only push", (X.s_fz <= q_s) & (X.p_fz > q_p)),
    ("crowded funding (fund top quintile)", X.fund > q_f),
]:
    print(f"  {name:36s}: {fmt(X[m & disc_m].rev)} | {fmt(X[m & ~disc_m].rev)}")


def fit(frame, lam=1.0, iters=400, lr=0.5):
    mu, sd = frame[FEATS].mean(), frame[FEATS].std().replace(0, 1)
    A = ((frame[FEATS] - mu) / sd).values
    A = np.column_stack([np.ones(len(A)), A])
    y = frame.rev.values.astype(float)
    w = np.zeros(A.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-A @ w))
        g = A.T @ (p - y) / len(y)
        g[1:] += lam * w[1:] / len(y)
        w -= lr * g
    return w, mu, sd


def predict(model, frame):
    w, mu, sd = model
    A = np.column_stack([np.ones(len(frame)), ((frame[FEATS] - mu) / sd).values])
    return 1 / (1 + np.exp(-A @ w))


def select(p, rev, t):
    bet = (p >= t) | (p <= 1 - t)
    hit = np.where(p >= t, rev, 1 - rev)
    return bet, hit


cut = D.ts.iloc[int(len(D) * 0.8)]
inner_fit, inner_val = D[D.ts < cut], D[D.ts >= cut]
m_in = fit(inner_fit)
p_in = predict(m_in, inner_val)
best, chosen = None, None
for t in np.round(np.arange(0.52, 0.705, 0.01), 2):
    bet, hit = select(p_in, inner_val.rev.values, t)
    n = int(bet.sum())
    if n < 150:
        continue
    hr = hit[bet].mean()
    if best is None or hr > best[1]:
        best = (t, hr, n)
    if hr >= 0.60 and chosen is None:
        chosen = (t, hr, n)
t = (chosen or best)[0]
print(f"\n== threshold from inner discovery: {'reached 60%' if chosen else 'did NOT reach 60% in-sample'}; best t={best[0]} hit {best[1]:.1%} n={best[2]} | using t={t}")
w, _, _ = m_in
print("   inner-fit coefficients (standardized):", {f: round(float(c), 3) for f, c in zip(["const", *FEATS], w)})

V = X[~disc_m].copy()
mA = fit(D)
V["pA"] = predict(mA, V)
print("   full-discovery coefficients:", {f: round(float(c), 3) for f, c in zip(["const", *FEATS], mA[0])})

V["pB"] = np.nan
for month, g in V.groupby(V.ts.dt.to_period("M")):
    hist = X[X.ts < g.ts.min()]
    V.loc[g.index, "pB"] = predict(fit(hist), g)


def report(label, p):
    bet, hit = select(p, V.rev.values, t)
    h = hit[bet]
    n, k = len(h), int(h.sum())
    lo, hi = wil(k, n)
    luck50 = 1 - binom.cdf(k - 1, n, 0.5)
    luck523 = 1 - binom.cdf(k - 1, n, 0.523)
    print(f"\n== {label}: bets {n} of {len(V)} hours ({n / len(V):.1%}) | hit {k / n:.1%} [{lo:.1%},{hi:.1%}] | P(luck | 50%)={luck50:.4f} | P(luck | 52.3%)={luck523:.4f}")
    half = V.ts.dt.year.astype(str) + np.where(V.ts.dt.month <= 6, "-H1", "-H2")
    for hname in sorted(half.unique()):
        mm = (half == hname).values & bet
        print(f"   {hname}: {fmt(hit[mm])}")
    for tt in [0.55, 0.58, 0.60, 0.62, 0.65]:
        b2, h2 = select(p, V.rev.values, tt)
        print(f"   sensitivity t={tt}: {fmt(h2[b2])}")
    met = n >= 150 and k / n >= 0.60 and lo >= 0.55
    print(f"   60% goal bar met: {met}")


report("Scheme A static", V.pA.values)
report("Scheme B rolling monthly refit", V.pB.values)
