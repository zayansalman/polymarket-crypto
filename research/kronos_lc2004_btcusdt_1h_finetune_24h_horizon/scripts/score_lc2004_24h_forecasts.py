"""Score the lc2004 24-hour forecasts written by run_lc2004_24h_forecasts_at_noon_et.py.

Primary (PREREG.md): hit rate of the 30-path majority call against Polymarket's daily BTC
Up/Down rule, the Binance 1-minute close at noon ET on D+1 vs noon ET on D (exact ties settle
50-50, so they are left out). 95% ranges are Wilson intervals; noon-to-noon windows do not
overlap, so each day counts once.

Also: Brier score, AUC, calibration, confident calls only, simple baselines on the same days,
what the forecast leans on (last 24 h move, last hour, RSI), hit rate by forecast hour 1-24, and
a same-day comparison with the Kronos team's published Kronos-mini forecasts (Tsinghua-Kronos
BTC 24h, data/published_forecasts_scored.csv of research/kronos_mini_official_btcusdt_demo on
branch research/kronos-mini-official-btcusdt-24h-demo-recreation @ 13894f0).

Writes outputs/score.txt and outputs/forecasts_summary.csv. Claude, 2026-09-17.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent.parent
DATA, OUT = HERE / "data", HERE / "outputs"

ap = argparse.ArgumentParser()
ap.add_argument("--tsinghua-scored", default=None,
                help="published_forecasts_scored.csv from the Tsinghua-Kronos BTC 24h research")
args = ap.parse_args()

lines: list[str] = []


def say(text: str = "") -> None:
    print(text)
    lines.append(text)


def wilson(hits: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return math.nan, math.nan
    p = hits / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return mid - half, mid + half


def auc(p: pd.Series, y: pd.Series) -> float:
    n1, n0 = int(y.sum()), int(len(y) - y.sum())
    if not n1 or not n0:
        return math.nan
    ranks = p.rank()
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def hit_line(label: str, call_up: pd.Series, y: pd.Series) -> str:
    n = len(y)
    hits = int((call_up.astype(int) == y.astype(int)).sum())
    lo, hi = wilson(hits, n)
    return f"  {label:<34} {hits / n:6.1%}  [{lo:.1%}, {hi:.1%}]  n={n}" if n else f"  {label:<34} n=0"


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    diff = close.diff()
    gain = diff.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-diff.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)


rows = [json.loads(line) for line in (DATA / "forecasts.jsonl").open()]
d = pd.DataFrame([{k: r[k] for k in ("et_date", "anchor_utc", "k", "last_close", "noon_close_d",
                                     "noon_close_d1", "p_up", "seconds")} for r in rows])
d["hourly_close_target"] = [r["actual_close_by_step"][-1] for r in rows]
d = d.sort_values("et_date").reset_index(drop=True)
rows_by_date = {r["et_date"]: r for r in rows}

hourly = pd.read_csv(DATA / "btcusdt_1h_spot.csv")
hourly["rsi14"] = rsi(hourly["close"])
by_open = hourly.set_index("open_time_ms")
anchor_ms = pd.to_datetime(d["anchor_utc"]).astype("int64") // 10**6
d["ret_24h"] = d["last_close"] / by_open.loc[anchor_ms - 25 * 3_600_000, "close"].to_numpy() - 1
d["ret_1h"] = d["last_close"] / by_open.loc[anchor_ms - 3_600_000, "open"].to_numpy() - 1
d["rsi14"] = by_open.loc[anchor_ms - 3_600_000, "rsi14"].to_numpy()

d["up_market"] = np.select([d["noon_close_d1"] > d["noon_close_d"],
                            d["noon_close_d1"] < d["noon_close_d"]], [1.0, 0.0], np.nan)
d["up_hourly"] = (d["hourly_close_target"] > d["last_close"]).astype(float)
OUT.mkdir(exist_ok=True)
d.round(6).to_csv(OUT / "forecasts_summary.csv", index=False)

say(f"lc2004 Kronos BTCUSDT 1h fine-tune, 24-hour forecasts at noon ET: {len(d)} days, "
    f"{d['et_date'].min()} to {d['et_date'].max()}")
say(f"median forecast time {d['seconds'].median():.0f}s; market ties left out: "
    f"{int(d['up_market'].isna().sum())}; calls exactly 50%: {int((d['p_up'] == 0.5).sum())}")

for target, label in (("up_market", "Polymarket daily rule (1m noon-ET closes)"),
                      ("up_hourly", "model's own target (hourly close K hours ahead)")):
    s = d.dropna(subset=[target])
    y = s[target].astype(int)
    dec = s[s["p_up"] != 0.5]
    say()
    say(f"== {label}")
    say(f"  up rate {y.mean():.1%} (n={len(s)})")
    say(hit_line("lc2004 majority call (p != 0.5)", dec["p_up"] > 0.5, dec[target]))
    for cut in (0.2, 0.3, 0.4):
        c = dec[(dec["p_up"] - 0.5).abs() >= cut]
        say(hit_line(f"  only |p - 0.5| >= {cut}", c["p_up"] > 0.5, c[target]))
    brier = float(((s["p_up"] - y) ** 2).mean())
    base = float(((y.mean() - y) ** 2).mean())
    say(f"  Brier {brier:.4f} vs 0.2500 always-50% vs {base:.4f} base rate | AUC {auc(s['p_up'], y):.3f}")
    say("  baselines, same days:")
    say(hit_line("always Up", pd.Series(True, index=s.index), y))
    say(hit_line("follow last 24h move", s["ret_24h"] > 0, y))
    say(hit_line("bet against last 24h move", s["ret_24h"] <= 0, y))
    say("  calibration:")
    for lo, hi in ((0, .2), (.2, .4), (.4, .5), (.5, .6), (.6, .8), (.8, 1.01)):
        b = s[(s["p_up"] >= lo) & (s["p_up"] < hi)]
        if len(b):
            say(f"    p in [{lo:.1f}, {min(hi, 1):.1f}): n={len(b):3d} mean p={b['p_up'].mean():.2f} "
                f"actual up={b[target].mean():.2f}")

say()
say("== what the forecast leans on (Spearman correlation with p_up)")
for col, label in (("ret_24h", "last 24h move"), ("ret_1h", "last hour move"), ("rsi14", "RSI(14), hourly")):
    say(f"  {label:<18} {d['p_up'].corr(d[col], method='spearman'):+.2f}")
dec = d[d["p_up"] != 0.5]
say(f"  calls that follow the last 24h move: {((dec['p_up'] > 0.5) == (dec['ret_24h'] > 0)).mean():.1%}")
say(f"  mean p_up {d['p_up'].mean():.2f}; share of Up calls {(dec['p_up'] > 0.5).mean():.1%}")

say()
say("== hit rate by forecast hour (all anchors; hour k close vs the last close before noon ET)")
for step in range(1, int(d["k"].max()) + 1):
    ps, ys = [], []
    for r in rows:
        if r["k"] >= step:
            ps.append(r["p_up_by_step"][step - 1])
            ys.append(int(r["actual_close_by_step"][step - 1] > r["last_close"]))
    s = pd.DataFrame({"p": ps, "y": ys})
    s = s[s["p"] != 0.5]
    hits = int(((s["p"] > 0.5).astype(int) == s["y"]).sum())
    lo, hi = wilson(hits, len(s))
    say(f"  hour {step:2d}: {hits / len(s):6.1%} [{lo:.1%}, {hi:.1%}] n={len(s)} "
        f"AUC {auc(s['p'].reset_index(drop=True), s['y'].reset_index(drop=True)):.3f}")

if args.tsinghua_scored:
    t = pd.read_csv(args.tsinghua_scored, parse_dates=["anchor"])
    t = t[t["era"] == "mini"]
    et = t["anchor"].dt.tz_localize("UTC").dt.tz_convert("America/New_York")
    t = t[et.dt.hour == 12].assign(et_date=et[et.dt.hour == 12].dt.date.astype(str))
    m = d.merge(t[["et_date", "p"]], on="et_date").dropna(subset=["up_market"])
    m = m[(m["p_up"] != 0.5) & (m["p"] != 0.5)]
    say()
    say("== same days as the Kronos team's published Kronos-mini 24h forecasts (Polymarket daily rule)")
    if len(m):
        y = m["up_market"].astype(int)
        ours, theirs = (m["p_up"] > 0.5).astype(int) == y, (m["p"] > 0.5).astype(int) == y
        say(hit_line("lc2004 fine-tune (Kronos-base)", m["p_up"] > 0.5, y))
        say(hit_line("Tsinghua-Kronos BTC 24h (Kronos-mini)", m["p"] > 0.5, y))
        say(f"  same call {((m['p_up'] > 0.5) == (m['p'] > 0.5)).mean():.1%}; both right {int((ours & theirs).sum())}, "
            f"only lc2004 {int((ours & ~theirs).sum())}, only Kronos-mini {int((~ours & theirs).sum())}, "
            f"both wrong {int((~ours & ~theirs).sum())}; p correlation {m['p_up'].corr(m['p']):+.2f}")
    else:
        say("  no overlapping days yet")

(OUT / "score.txt").write_text("\n".join(lines) + "\n")
