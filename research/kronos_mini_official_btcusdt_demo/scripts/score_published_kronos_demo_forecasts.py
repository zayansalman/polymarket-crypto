"""Score the Kronos team's published BTCUSDT forecasts (shiyu-coder/Kronos-demo) against Binance.

Published number: "upside probability" = share of 30 sampled Kronos paths whose close
24 hours ahead is ABOVE the last closed hourly close (update_predictions.py,
calculate_metrics). Each run starts at HH:00:05 UTC and drops the still-forming candle,
so for a forecast published during hour A (A = update time floored to the hour):
  last_close = close of the candle that opened at A-1h (price at A)
  24h outcome = close of the candle that opened at A+23h (price at A+24h) > last_close
Also scored against hour A's own candle (close >= open), which is how Polymarket's
hourly BTC Up/Down market resolves, since the forecast is published ~25 s into hour A.

Eras (from the demo repo's history of update_predictions.py and model/module.py):
  small_T0.6   2025-07-11 .. 2025-08-20 07:33 UTC  Kronos-small, T=0.6, top_p=0.9
  small_T1.0   2025-08-20 07:33 .. 2025-08-30 10:17 UTC  Kronos-small, T=1.0, top_p=0.95
  mini_prefix  2025-08-30 10:17 .. 2025-09-16 02:33 UTC  Kronos-mini, attention dropout left on
  mini         2025-09-16 02:33 UTC .. 2026-07-04  Kronos-mini, dropout bug fixed (commit eba16695)
The era boundaries are code-push times; when the running process picked each change up
is unknown.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent.parent
PUB = HERE / "data" / "published_forecasts.csv"
KL = HERE / "data" / "btcusdt_1h_spot.csv"
OUT = HERE / "data" / "published_forecasts_scored.csv"

ERAS = [
    ("small_T0.6", "2025-07-11", "2025-08-20 07:33"),
    ("small_T1.0", "2025-08-20 07:33", "2025-08-30 10:17"),
    ("mini_prefix", "2025-08-30 10:17", "2025-09-16 02:33"),
    ("mini", "2025-09-16 02:33", "2026-12-31"),
]


def auc(p: np.ndarray, y: np.ndarray) -> float:
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty(len(p))
    sp = p[order]
    i = 0
    while i < len(sp):  # average ranks for ties
        j = i
        while j + 1 < len(sp) and sp[j + 1] == sp[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    n1 = y.sum()
    n0 = len(y) - n1
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def block_bootstrap(df: pd.DataFrame, fn, block_hours: int = 24, reps: int = 2000,
                    seed: int = 7) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    blocks = [g for _, g in df.groupby(df["anchor"].dt.floor(f"{block_hours}h"))]
    stats = []
    for _ in range(reps):
        pick = rng.integers(0, len(blocks), len(blocks))
        stats.append(fn(pd.concat([blocks[k] for k in pick])))
    return float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5))


def summarise(name: str, d: pd.DataFrame, target: str) -> None:
    d = d.dropna(subset=[target])
    y = d[target].to_numpy().astype(int)
    p = d["p"].to_numpy()
    decided = d[d["p"] != 0.5]
    hit = float(((decided["p"] > 0.5).astype(int) == decided[target]).mean())
    base = y.mean()
    brier = float(np.mean((p - y) ** 2))
    brier_half = float(np.mean((0.5 - y) ** 2))
    brier_base = float(np.mean((base - y) ** 2))
    hit_ci = block_bootstrap(decided, lambda g: float(((g["p"] > 0.5).astype(int) == g[target]).mean()))
    skill_ci = block_bootstrap(d, lambda g: float(np.mean((g[target].mean() - g[target]) ** 2)
                                                  - np.mean((g["p"] - g[target]) ** 2)))
    print(f"  {name:<12} n={len(d):5d} up-rate={base:.3f} | hit={hit:.3f} "
          f"[{hit_ci[0]:.3f},{hit_ci[1]:.3f}] (n={len(decided)}) | AUC={auc(p, y):.3f} | "
          f"Brier={brier:.4f} vs always-0.5 {brier_half:.4f} vs base-rate {brier_base:.4f} "
          f"| skill vs base-rate={brier_base - brier:+.4f} [{skill_ci[0]:+.4f},{skill_ci[1]:+.4f}]")


def calibration(d: pd.DataFrame, target: str) -> None:
    d = d.dropna(subset=[target])
    bins = [0, 0.2, 0.4, 0.5, 0.6, 0.8, 1.0001]
    d = d.assign(bucket=pd.cut(d["p"], bins, right=False))
    t = d.groupby("bucket", observed=True).agg(n=(target, "size"), mean_p=("p", "mean"),
                                               actual=(target, "mean"))
    print(t.to_string(float_format=lambda v: f"{v:.3f}"))


def main() -> int:
    pub = pd.read_csv(PUB)
    pub = pub[pub["subject"].str.startswith("Auto-update", na=False)].copy()
    pub = pub[pub["upside_prob"].str.endswith("%", na=False)]
    pub["update_time"] = pd.to_datetime(pub["update_time"])
    pub["p"] = pub["upside_prob"].str.rstrip("%").astype(float) / 100
    pub["anchor"] = pub["update_time"].dt.floor("h")
    pub = pub.drop_duplicates("anchor", keep="first")

    kl = pd.read_csv(KL)
    kl["open_time"] = pd.to_datetime(kl["open_time_ms"], unit="ms")
    kl = kl.set_index("open_time")
    close = kl["close"]
    openp = kl["open"]

    a = pub["anchor"]
    pub["last_close"] = close.reindex(a - pd.Timedelta(hours=1)).to_numpy()
    pub["close_24h"] = close.reindex(a + pd.Timedelta(hours=23)).to_numpy()
    pub["up_24h"] = np.where(pub["close_24h"].notna(),
                             (pub["close_24h"] > pub["last_close"]).astype(float), np.nan)
    h_open = openp.reindex(a).to_numpy()
    h_close = close.reindex(a).to_numpy()
    pub["up_hour_A"] = np.where(~np.isnan(h_close), (h_close >= h_open).astype(float), np.nan)

    pub["era"] = None
    for name, lo, hi in ERAS:
        m = (pub["update_time"] >= pd.Timestamp(lo)) & (pub["update_time"] < pd.Timestamp(hi))
        pub.loc[m, "era"] = name
    OUT.parent.mkdir(exist_ok=True)
    (HERE / "outputs").mkdir(exist_ok=True)
    pub.to_csv(OUT, index=False)

    for target, label in [("up_24h", "24-hour direction (what the demo forecasts)"),
                          ("up_hour_A", "hour A direction (Polymarket hourly market rule)")]:
        print(f"\n== {label}; block bootstrap 95% ranges use 24h blocks")
        for name, _, _ in ERAS:
            summarise(name, pub[pub["era"] == name], target)
        summarise("all", pub, target)
        print(f"\n  calibration, era 'mini' ({target}):")
        calibration(pub[pub["era"] == "mini"], target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
