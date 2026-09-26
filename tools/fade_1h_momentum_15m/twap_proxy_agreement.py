"""Which Binance proxy of the 15m settlement agrees with the real resolutions? (section 1b)

The 15m markets resolve on the Chainlink BTC/USD TWAP-60s data stream: Up if the "TWAP of the
time range" is >= "the price at the beginning of that range". Two readings of that text:

- whole window: the average price over the 15 minutes vs the stream's value at the open;
- trailing minute: the stream's value at the close (the average over the last 60 s) vs its
  value at the open (the average over the 60 s before the open).

Every settled 15m market in m15.db (read-only, winner column, Sep 17-20) is compared with
Binance 1-minute candles (data/fade_1h_momentum_15m/spot1m.db). A kline opening at t has o =
price at t and c = price at t + 60, so (o + c) / 2 stands for the average over [t, t + 60].
Proxies, each "Up iff end >= start", ties Up:

- close vs open: c of the window's last minute >= o of its first (what steps 0-2 used);
- whole-window average: mean of (o + c) / 2 over the window's 15 minutes >= (o + c) / 2 of
  the minute before the open (the proxy of the 2026-09-22 six-month quick check);
- trailing minute: (o + c) / 2 of the window's last minute >= (o + c) / 2 of the minute
  before the open.

Sensitivities: the average over the last k = 2, 3, 5, 10 minutes; (o + h + l + c) / 4 in place
of (o + c) / 2; the start taken as the open price o. Paired comparisons against close vs open
on the same markets: McNemar counts and a t of the per-market difference clustered by window
(the four coins of one window move together). Every table here is descriptive; no threshold.

    PYTHON_DOTENV_DISABLED=1 python3 tools/fade_1h_momentum_15m/twap_proxy_agreement.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.fade_1h_momentum_15m import data  # noqa: E402

OUT = data.OUT_DIR / "twap_proxy_agreement.json"
PRE = 11  # minutes kept before the open (the start reference and the longest trailing average)


def _grid() -> dict:
    """Per market: winner (0 Up, 1 Down), asset, window start, and (n, PRE + 15) o/h/l/c arrays."""
    markets = data.markets_15m()
    by: dict[str, list] = {}
    for m in markets:
        by.setdefault(m.asset, []).append(m)
    out = {k: [] for k in ("winner", "asset", "start", "o", "h", "l", "c")}
    skipped = []
    for asset, ms in sorted(by.items()):
        lo = min(m.start_ts for m in ms) - 60 * PRE
        hi = max(m.end_ts for m in ms)
        b = data.load_1m(asset, lo, hi)
        idx = {int(t): i for i, t in enumerate(b.t)}
        for m in ms:
            need = [m.start_ts + 60 * j for j in range(-PRE, 15)]
            if not all(t in idx for t in need):
                skipped.append(m.slug)
                continue
            ii = np.array([idx[t] for t in need])
            out["winner"].append(m.winner)
            out["asset"].append(asset)
            out["start"].append(m.start_ts)
            for f in "ohlc":
                out[f].append(getattr(b, f)[ii])
    g = {k: np.asarray(v) for k, v in out.items()}
    g["n_markets"], g["skipped"] = len(markets), skipped
    return g


def _col(j: int) -> int:
    """Column of the minute opening at window start + 60 j (j = -1 is the minute before the open)."""
    return PRE + j


def proxies(g: dict) -> dict[str, np.ndarray]:
    """Binance side for each proxy: True = Up."""
    o, h, lo, c = g["o"], g["h"], g["l"], g["c"]
    mid = (o + c) / 2
    ohlc = (o + h + lo + c) / 4
    start = mid[:, _col(-1)]
    win = slice(_col(0), _col(15))
    p = {
        "close_vs_open": c[:, _col(14)] >= o[:, _col(0)],
        "whole_window_average": mid[:, win].mean(1) >= start,
        "trailing_minute": mid[:, _col(14)] >= start,
    }
    for k in (2, 3, 5, 10):
        p[f"last_{k}_minutes_average"] = mid[:, _col(15 - k):_col(15)].mean(1) >= start
    p["whole_window_average_ohlc4"] = ohlc[:, win].mean(1) >= ohlc[:, _col(-1)]
    p["trailing_minute_ohlc4"] = ohlc[:, _col(14)] >= ohlc[:, _col(-1)]
    p["whole_window_average_vs_open_price"] = mid[:, win].mean(1) >= o[:, _col(0)]
    p["trailing_minute_vs_open_price"] = mid[:, _col(14)] >= o[:, _col(0)]
    return p


def _cluster_t(x: np.ndarray, cluster: np.ndarray) -> float:
    """Mean of x over its clustered standard error (sum of within-cluster deviations)."""
    d = x - x.mean()
    _, inv = np.unique(cluster, return_inverse=True)
    s = np.bincount(inv, weights=d)
    se = math.sqrt(float((s * s).sum())) / x.size
    return float(x.mean() / se) if se > 0 else float("nan")


def agreement(g: dict) -> dict:
    up_real = g["winner"] == 0
    base = None
    res: dict = {"n_markets_in_tape": g["n_markets"], "n_compared": int(up_real.size),
                 "skipped_missing_minutes": g["skipped"], "proxies": {}}
    for name, up in proxies(g).items():
        ok = up == up_real
        k, n = int(ok.sum()), int(ok.size)
        row = {"agree": k, "n": n, "rate": k / n, "wilson95": [round(x, 4) for x in data._wilson(k, n)],
               "by_asset": {}}
        for a in sorted(set(g["asset"])):
            s = g["asset"] == a
            row["by_asset"][a] = {"agree": int(ok[s].sum()), "n": int(s.sum()), "rate": float(ok[s].mean())}
        if base is None:
            base = ok
        else:
            b_only, p_only = int((base & ~ok).sum()), int((ok & ~base).sum())
            diff = ok.astype(float) - base.astype(float)
            row["vs_close_vs_open"] = {
                "proxy_right_close_wrong": p_only, "close_right_proxy_wrong": b_only,
                "difference_in_rate": float(diff.mean()),
                "mcnemar_z": (p_only - b_only) / math.sqrt(p_only + b_only) if p_only + b_only else 0.0,
                "t_clustered_by_window": _cluster_t(diff, g["start"]),
                "n_windows": int(np.unique(g["start"]).size)}
        res["proxies"][name] = row
    return res


def main() -> None:
    g = _grid()
    res = agreement(g)
    res["definitions"] = {
        "kline": "a Binance 1m kline opening at t has o = price at t and c = price at t + 60; (o + c) / 2 stands "
                 "for the average over [t, t + 60]",
        "start_reference": "(o + c) / 2 of the minute before the open, for the Chainlink TWAP-60s value at the open; "
                           "the *_vs_open_price rows use o of the first minute instead",
        "close_vs_open": "c of the window's last minute >= o of its first minute",
        "whole_window_average": "mean over the window's 15 minutes of (o + c) / 2 >= the start reference",
        "trailing_minute": "(o + c) / 2 of the window's last minute >= the start reference",
        "last_k_minutes_average": "mean of (o + c) / 2 over the window's last k minutes >= the start reference",
        "ohlc4": "(o + h + l + c) / 4 in place of (o + c) / 2, start reference included",
        "ties": "Up",
        "t_clustered_by_window": "per-market (proxy agrees) - (close vs open agrees), clustered by window start",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=1))
    print(f"{res['n_compared']} of {res['n_markets_in_tape']} settled 15m markets compared "
          f"({len(res['skipped_missing_minutes'])} skipped for missing Binance minutes)")
    print(f"{'proxy':40} {'agree':>11} {'rate':>7} {'Wilson 95%':>16}   vs close vs open")
    for name, r in res["proxies"].items():
        v = r.get("vs_close_vs_open")
        tail = "" if v is None else (f"   +{v['proxy_right_close_wrong']} / -{v['close_right_proxy_wrong']}  "
                                     f"diff {v['difference_in_rate']:+.4f}  t {v['t_clustered_by_window']:+.2f}")
        print(f"{name:40} {r['agree']:5d}/{r['n']:<5d} {r['rate']:7.4f} {str(r['wilson95']):>16}{tail}")
    for name in ("close_vs_open", "whole_window_average", "trailing_minute"):
        print(f"  {name} by asset: " + ", ".join(
            f"{a} {v['agree']}/{v['n']} ({v['rate']:.3f})" for a, v in res["proxies"][name]["by_asset"].items()))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
