"""Compare recreated Tsinghua-Kronos BTC 24h forecasts with the Kronos team's published ones.

Reads outputs/recreated_every_25h.csv (recreate_tsinghua_kronos_btc_24h_forecast.py) and
data/published_forecasts_scored.csv (score_published_kronos_demo_forecasts.py), keeps the
era "mini" hours present in both, and reports: whether the input's last close is identical,
mean and mean-absolute gap, correlation, and how many gaps exceed two standard errors of the
difference of two independent 30-path estimates, sqrt(2 * p(1-p) / 30) (floor p(1-p) at 1/30
so 0% and 100% readings are not treated as exact). About 5% beyond 2 SE is expected from
sampling alone.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent.parent

r = pd.read_csv(HERE / "outputs" / "recreated_every_25h.csv", parse_dates=["anchor"])
p = pd.read_csv(HERE / "data" / "published_forecasts_scored.csv", parse_dates=["anchor"])
m = r.merge(p[["anchor", "p", "last_close", "up_24h", "era"]], on="anchor",
            suffixes=("_ours", "_published"))
m = m[m["era"] == "mini"]
gap = m["upside_prob"] - m["p"]
se = np.sqrt(2 * np.maximum(m["p"] * (1 - m["p"]), 1 / 30) / 30)
print(f"hours compared: {len(m)}")
print(f"inputs identical (last close): {np.isclose(m['last_close_ours'], m['last_close_published']).mean():.1%}")
print(f"mean gap {gap.mean():+.3f}, mean absolute gap {gap.abs().mean():.3f}, "
      f"correlation {np.corrcoef(m['upside_prob'], m['p'])[0, 1]:.3f}")
print(f"gaps beyond 2 SE: {(gap.abs() > 2 * se).sum()} of {len(m)} "
      f"({(gap.abs() > 2 * se).mean():.1%}; about 5% expected from sampling alone)")
print(f"mean ours {m['upside_prob'].mean():.3f} vs published {m['p'].mean():.3f}; "
      f"sd ours {m['upside_prob'].std():.3f} vs published {m['p'].std():.3f}")
print(f"median seconds per recreated forecast: {r['seconds'].median():.1f}")
