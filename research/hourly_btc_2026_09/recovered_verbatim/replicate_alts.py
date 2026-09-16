import math
from pathlib import Path
import numpy as np, pandas as pd

B = Path(__file__).parent
FILES = {
    "BTC": ("btc_1h_flow.csv", "btc_perp_1h_flow.csv", "bybit_oi_1h.csv"),
    "ETH": ("eth_1h_flow.csv", "eth_perp_1h_flow.csv", "bybit_oi_eth_1h.csv"),
    "SOL": ("sol_1h_flow.csv", "sol_perp_1h_flow.csv", "bybit_oi_sol_1h.csv"),
    "XRP": ("xrp_1h_flow.csv", "xrp_perp_1h_flow.csv", "bybit_oi_xrp_1h.csv"),
}

def hourly(name):
    d = pd.read_csv(B / name, parse_dates=["ts"])
    imb = 2 * d.taker_buy_base / d.volume - 1
    d["z"] = (imb - imb.rolling(168).mean()) / imb.rolling(168).std()
    d["dir"] = np.sign(d.close - d.open)
    d["fz"] = d.z * d.dir
    return d

def w(x):
    n = len(x)
    if n == 0:
        return float("nan"), float("nan"), 0, "n=0"
    k = int(x.sum()); ph = k / n; z = 1.96; d = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / d; h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
    return ph, c - h, n, f"{ph:5.1%} [{c-h:4.1%},{c+h:4.1%}] n={n}"

def run(asset):
    spot, perp, oif = FILES[asset]
    s = hourly(spot)
    p = hourly(perp)[["ts", "fz"]].rename(columns={"fz": "p_fz"})
    oi = pd.read_csv(B / oif, parse_dates=["ts"])[["ts", "oi"]]
    df = s.merge(p, on="ts").merge(oi, on="ts", how="left")
    df["up"] = (df.close > df.open).astype(int)
    rng = (df.high - df.low).replace(0, np.nan)
    df["clv"] = (2 * df.close - df.high - df.low) / rng
    lr = np.log(df.oi / df.oi.shift(1))
    df["doi_now"] = (lr - lr.rolling(168).mean()) / lr.rolling(168).std()
    for c in ["fz", "p_fz", "dir", "clv"]:
        df[c + "_1"] = df[c].shift(1)
    df = df.iloc[400:].dropna(subset=["fz_1", "p_fz_1", "dir_1", "clv_1", "doi_now"])
    df = df[df.dir_1 != 0].copy()
    df["win"] = (df.up == (df.dir_1 < 0).astype(int)).astype(int)
    df["month"] = df.ts.dt.to_period("M")
    months = [m for m in sorted(df.month.unique()) if pd.Period("2024-04") <= m <= pd.Period("2026-08")]
    sel = {"A": [], "A_oi_notup": [], "A_oi_up": []}
    for m in months:
        hist, cur = df[df.month < m], df[df.month == m]
        q80 = hist.fz_1.quantile(0.8); pq80 = hist.p_fz_1.quantile(0.8)
        clv = ((cur.dir_1 > 0) & (cur.clv_1 > 0.8)) | ((cur.dir_1 < 0) & (cur.clv_1 < -0.8))
        A = (cur.fz_1 > q80) & (cur.p_fz_1 <= pq80) & clv
        sel["A"].append(cur[A]); sel["A_oi_notup"].append(cur[A & (cur.doi_now <= 0)]); sel["A_oi_up"].append(cur[A & (cur.doi_now > 0)])
    base = df[df.month.isin(months)].win.mean()
    return base, {k: pd.concat(v) for k, v in sel.items()}

pooled = {"A": [], "A_oi_notup": [], "A_oi_up": []}
pooled_base = []
print("asset | all-hours reversal | Tier A | Tier A + OI not up (rule) | Tier A + OI up | replicates?")
for asset in ["BTC", "ETH", "SOL", "XRP"]:
    base, r = run(asset)
    a, notup, up = w(r["A"].win), w(r["A_oi_notup"].win), w(r["A_oi_up"].win)
    rep = notup[0] >= 0.57 and notup[1] > base and notup[0] > up[0]
    tag = "(original)" if asset == "BTC" else ("YES" if rep else "no")
    print(f"{asset} | {base:.1%} | {a[3]} | {notup[3]} | {up[3]} | {tag}")
    if asset != "BTC":
        for k in pooled:
            pooled[k].append(r[k].assign(asset=asset))
        pooled_base.append(base)
P = {k: pd.concat(v) for k, v in pooled.items()}
pb = float(np.mean(pooled_base))
notup, up = w(P["A_oi_notup"].win), w(P["A_oi_up"].win)
rep = notup[0] >= 0.57 and notup[1] > pb and notup[0] > up[0]
print(f"POOLED ETH+SOL+XRP | {pb:.1%} | {w(P['A'].win)[3]} | {notup[3]} | {up[3]} | {'YES' if rep else 'no'}")
X = P["A_oi_notup"]
print("pooled rule by year:", " ".join(f"{y}:{g.win.mean():.0%}/{len(g)}" for y, g in X.groupby(X.ts.dt.year)))
print("pooled rule latest 11 months (>= 2025-10-18):", w(X[X.ts >= pd.Timestamp('2025-10-18 14:00')].win)[3])
