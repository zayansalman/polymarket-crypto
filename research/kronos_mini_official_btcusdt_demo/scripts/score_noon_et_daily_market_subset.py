"""Score the published Kronos-mini 24h forecasts only at 12:00 America/New_York.

Polymarket's daily BTC Up/Down market ("bitcoin-up-or-down-on-<date>") resolves on the
Binance BTCUSDT 1-minute close at noon ET against the previous day's noon-ET close
(polymarket_bot/daily/market.py; exact ties settle 50-50). A 24h forecast published during
the noon-ET hour covers that same window: last_close = price at 12:00 ET, target = price
24 hours later. Reads data/published_forecasts_scored.csv (score_published_kronos_demo_forecasts.py).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent.parent

d = pd.read_csv(HERE / "data" / "published_forecasts_scored.csv", parse_dates=["anchor"])
d = d[d["era"] == "mini"].dropna(subset=["up_24h"])
et = d["anchor"].dt.tz_localize("UTC").dt.tz_convert("America/New_York")
noon = d[et.dt.hour == 12]
dec = noon[noon["p"] != 0.5]
hit = ((dec["p"] > 0.5).astype(int) == dec["up_24h"]).mean()
se = (hit * (1 - hit) / len(dec)) ** 0.5
print(f"noon-ET anchors (era mini): n={len(noon)} decided={len(dec)} hit={hit:.3f} "
      f"[{hit - 1.96 * se:.3f},{hit + 1.96 * se:.3f}] up-rate={noon['up_24h'].mean():.3f}")
for lo, hi in [(0, .2), (.2, .4), (.4, .5), (.5, .6), (.6, .8), (.8, 1.01)]:
    s = noon[(noon["p"] >= lo) & (noon["p"] < hi)]
    print(f"  p in [{lo},{hi}): n={len(s):3d} mean_p={s['p'].mean():.2f} actual_up={s['up_24h'].mean():.2f}")
