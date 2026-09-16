import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).parent
SPLIT = pd.Timestamp("2025-10-18 14:00")


def load(name):
    d = pd.read_csv(BASE / name, parse_dates=["ts"])
    imb = 2 * d.taker_buy_base / d.volume - 1
    d["z"] = (imb - imb.rolling(168).mean()) / imb.rolling(168).std()
    d["dir"] = np.sign(d.close - d.open)
    d["fz"] = d.z * d.dir
    d["rng"] = (d.high - d.low) / d.open
    return d


btc, eth = load("btc_1h_flow.csv"), load("eth_1h_flow.csv")
df = btc.merge(eth[["ts", "z", "fz", "dir"]].rename(columns={"z": "eth_z", "fz": "eth_fz", "dir": "eth_dir"}), on="ts", how="left")
df["up"] = (df.close > df.open).astype(int)
df["move"] = (df.close - df.open).abs() / df.open
for c in ["fz", "z", "dir", "eth_fz", "eth_z", "eth_dir", "rng", "volume"]:
    df[c + "_1"] = df[c].shift(1)
df["rng_p95"] = df.rng.rolling(168).quantile(0.95).shift(1)
df["vol_p90"] = df.volume.rolling(168).quantile(0.90).shift(1)
df = df.iloc[200:].dropna(subset=["fz_1", "z_1", "dir_1", "eth_fz_1"]).copy()
df = df[df.dir_1 != 0]

disc = df[df.ts < SPLIT]
q_fz = disc.fz_1.quantile(0.8)
q_z = disc.z_1.abs().quantile(0.8)
q_efz = disc.eth_fz_1.quantile(0.8)
df["sig"] = df.fz_1 > q_fz
df["sig_win"] = (df.up == (df.dir_1 < 0).astype(int)).astype(int)
df["alt"] = df.z_1.abs() > q_z
df["alt_win"] = (df.up == (df.z_1 < 0).astype(int)).astype(int)

ts_ny = df.ts.dt.tz_localize("UTC").dt.tz_convert("America/New_York")
hour = df.ts.dt.hour
last_fri = (df.ts.dt.weekday == 4) & ((df.ts + pd.Timedelta(days=7)).dt.month != df.ts.dt.month)
factors = {
    "funding hour (H or H-1 at 00/08/16 UTC)": hour.isin([0, 8, 16]) | hour.isin([1, 9, 17]),
    "options expiry hour (08 UTC)": hour == 8,
    "  monthly expiry (last Fri 08 UTC)": (hour == 8) & last_fri,
    "US open hour (contains 09:30 NY)": ts_ny.dt.hour == 9,
    "US regular session (09:30-16:00 NY, weekdays)": (ts_ny.dt.weekday < 5) & (((ts_ny.dt.hour == 9)) | ((ts_ny.dt.hour >= 10) & (ts_ny.dt.hour < 16))),
    "weekend (UTC)": df.ts.dt.weekday >= 5,
    "big/liquidation-like H-1": (df.rng_1 > df.rng_p95) & (df.volume_1 > df.vol_p90),
    "ETH also flow-pushed same way": (df.eth_fz_1 > q_efz) & (df.eth_dir_1 == df.dir_1),
}

cal_path = BASE / "macro_calendar.csv"
if cal_path.exists():
    cal = pd.read_csv(cal_path, parse_dates=["utc"])
    cal["hour_start"] = cal.utc.dt.tz_localize(None).dt.floor("h")
    for ev, label in [("CPI|JOBS", "CPI or jobs report hour"), ("FOMC_STATEMENT", "FOMC statement hour"), ("CPI|JOBS|FOMC_STATEMENT|PCE", "any scheduled macro hour")]:
        hs = set(cal[cal.event.str.fullmatch(ev)].hour_start)
        factors[label] = df.ts.isin(hs)
    print(f"macro calendar loaded: {len(cal)} rows")


def w(s):
    n = len(s)
    if n == 0:
        return "      n=0          "
    k = int(s.sum())
    p = k / n
    z = 1.96
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return f"{p:5.1%} [{c - h:4.1%},{c + h:4.1%}] n={n:<5}"


print(f"cutoffs from discovery: fz>{q_fz:.2f} |z|>{q_z:.2f} eth_fz>{q_efz:.2f}")
for part_name, part in [("DISCOVERY 2023-25", df.ts < SPLIT), ("VALIDATION 2025-26", df.ts >= SPLIT)]:
    g = df[part]
    print(f"\n==== {part_name} | SIG all {w(g[g.sig].sig_win)} | ALT all {w(g[g.alt].alt_win)} | P(up) {g.up.mean():.1%} | move {g.move.mean():.3%}")
    for name, mask in factors.items():
        m = mask[part]
        for tag, mm in [("on ", m), ("off", ~m)]:
            h = g[mm]
            print(f"  {name[:46]:46s} {tag} | SIG {w(h[h.sig].sig_win)} | ALT {w(h[h.alt].alt_win)} | P(up) {h.up.mean():5.1%} | move {h.move.mean():.3%}")

print("\nETH lead-lag (all hours): P(BTC up at H | ETH H-1 up / down)")
for part_name, part in [("discovery", df.ts < SPLIT), ("validation", df.ts >= SPLIT)]:
    g = df[part]
    print(f"  {part_name}: ETH up {w(g[g.eth_dir_1 > 0].up)} | ETH down {w(g[g.eth_dir_1 < 0].up)}")
    print(f"  {part_name}: BTC & ETH H-1 same direction -> reversal {w(g[g.eth_dir_1 == g.dir_1].sig_win)} | different -> reversal {w(g[g.eth_dir_1 != g.dir_1].sig_win)}")
