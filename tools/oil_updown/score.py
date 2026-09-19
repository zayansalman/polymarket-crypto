"""Score every preds/*.parquet on the same days.

Windows
  A  settle target, 2012-01 .. 2026-09-11 (long history)
  B  5pm target (Polymarket-matched), 2024-04-23 .. 2026-09-11
  C  Polymarket window: the 116 resolved WTI Up/Down markets, real outcome
"""
import glob
import json
import sys

import numpy as np
import pandas as pd
from statsmodels.stats.proportion import proportion_confint

d = pd.read_parquet("dataset.parquet")
d = d[d.index < "2026-09-14"]
pm = pd.read_json("pm_oil_events.json")
pm = pm[pm.closed].copy()
pm["date"] = pd.to_datetime(pd.to_datetime(pm.end).dt.tz_convert("America/New_York").dt.date)
pm["y_pm"] = pm.prices.apply(lambda p: json.loads(p)[0] if isinstance(p, str) else p[0]).astype(float)
y_pm = pm.set_index("date")["y_pm"]


def stats(p, y):
    ok = p.notna() & y.notna()
    p, y = p[ok], y[ok]
    n = len(y)
    if n == 0:
        return {}
    call = (p > 0.5).astype(float)
    hit = (call == y).mean()
    lo, hi = proportion_confint(int((call == y).sum()), n, method="wilson")
    conf = (p - 0.5).abs()
    top = conf >= conf.quantile(0.7)
    hit_top = (call[top] == y[top]).mean() if top.sum() else np.nan
    brier = ((p.clip(0.01, 0.99) - y) ** 2).mean()
    return dict(n=n, hit=round(hit * 100, 1), ci=f"{lo*100:.1f}-{hi*100:.1f}", hit_top30=round(hit_top * 100, 1),
                up_calls=round(call.mean() * 100, 0), brier=round(brier, 4))


rows = []
for f in sorted(glob.glob("preds/*.parquet")):
    name = f.split("/")[-1][:-8]
    p = pd.read_parquet(f)["p_up"]
    p.index = pd.to_datetime(p.index)
    a = stats(p.reindex(d.index)[(d.index >= "2012-01-01")], d["y_settle"][(d.index >= "2012-01-01")])
    b = stats(p.reindex(d.index)[d.index >= "2024-04-23"], d["y_5pm"][d.index >= "2024-04-23"])
    c = stats(p.reindex(y_pm.index), y_pm)
    rows.append(dict(model=name, A_n=a.get("n"), A_hit=a.get("hit"), A_ci=a.get("ci"), A_top30=a.get("hit_top30"),
                     B_n=b.get("n"), B_hit=b.get("hit"), B_ci=b.get("ci"), B_top30=b.get("hit_top30"), B_brier=b.get("brier"),
                     C_n=c.get("n"), C_hit=c.get("hit"), C_ci=c.get("ci")))
res = pd.DataFrame(rows).sort_values("B_hit", ascending=False)
pd.set_option("display.width", 250)
print(res.to_string(index=False))
res.to_csv("scoreboard.csv", index=False)
