"""Does buying (or fading) the 1h-momentum side of a 15m market pay, by entry price?

The bulk check behind Fade 1h Momentum on 15m, run 2026-09-21 on 1,136 BTC/ETH/
SOL/XRP 15m markets (m15.db, Sep 17-20). For each market:

- momentum side = sign of the Binance spot return from one hour before the
  window opens to the open (Up if positive)
- reference price = size-weighted price wallets paid for that side in the
  first 3 minutes of the window (30 s grace before the open)
- one row per market (the market is the independent trial), net of the taker
  fee ``0.07 * p * (1 - p)`` per share

Binance 1-minute klines are fetched once into ``--spot-cache``.

    python3 tools/fade_1h_momentum_15m/threshold_scan.py --m15 data/wallet_research/m15.db
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
import urllib.request
from pathlib import Path

FEE = 0.07
SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}
BANDS = [(0.0, 0.30), (0.30, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 1.01)]


def load_spot(cache: Path, start: int, end: int) -> dict[str, dict[int, float]]:
    db = sqlite3.connect(cache)
    db.execute("create table if not exists k(sym text, t integer, o real, primary key(sym, t))")
    for sym in SYMBOLS.values():
        have = db.execute("select count(*) from k where sym=? and t between ? and ?", (sym, start, end)).fetchone()[0]
        if have >= (end - start) // 60:
            continue
        s = start * 1000
        while s < end * 1000:
            url = (f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1m"
                   f"&startTime={s}&endTime={end * 1000 - 1}&limit=1000")
            rows = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20))
            if not rows:
                break
            db.executemany("insert or replace into k values (?,?,?)", [(sym, r[0] // 1000, float(r[1])) for r in rows])
            s = rows[-1][0] + 60_000
            time.sleep(0.1)
        db.commit()
    out: dict[str, dict[int, float]] = {s: {} for s in SYMBOLS.values()}
    for sym, t, o in db.execute("select sym, t, o from k"):
        out[sym][t] = o
    return out


def price_at(series: dict[int, float], ts: int) -> float | None:
    for back in range(6):
        t = (ts // 60 - back) * 60
        if t in series:
            return series[t]
    return None


def stats(payoffs: list[float]) -> tuple[float, float]:
    n = len(payoffs)
    mean = sum(payoffs) / n
    var = sum((x - mean) ** 2 for x in payoffs) / (n - 1)
    return mean, mean / math.sqrt(var / n) if var > 0 else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--m15", default="data/wallet_research/m15.db")
    ap.add_argument("--spot-cache", default="data/fade_1h_momentum_15m/spot.db")
    args = ap.parse_args()

    Path(args.spot_cache).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(f"file:{args.m15}?mode=ro", uri=True)
    markets = db.execute("select cid, slug, end_ts, winner from markets where fetched=1").fetchall()
    lo = min(m[2] for m in markets) - 900 - 3600 - 600
    hi = max(m[2] for m in markets) + 60
    spot = load_spot(Path(args.spot_cache), lo - lo % 60, hi)

    rows = []  # (price of momentum side, momentum side won, symbol)
    for cid, slug, end_ts, winner in markets:
        sym = SYMBOLS.get(slug.split("-")[0])
        if sym is None:
            continue
        start = end_ts - 900
        now, hour_ago = price_at(spot[sym], start), price_at(spot[sym], start - 3600)
        if not now or not hour_ago or now == hour_ago:
            continue
        side = 0 if now > hour_ago else 1
        fills = db.execute(
            "select price, size from trades where cid=? and oidx=? and ts between ? and ?",
            (cid, side, start - 30, start + 180),
        ).fetchall()
        if not fills:
            continue
        p = sum(pr * sz for pr, sz in fills) / sum(sz for _, sz in fills)
        rows.append((p, int(side == winner), sym))

    print(f"{len(rows)} of {len(markets)} markets had a momentum side and an early fill on it\n")
    print(f"{'price band':>11} {'n':>4} {'won':>6} {'avg p':>6} {'follow c/sh':>12} {'t':>6}")
    for a, b in BANDS:
        sel = [r for r in rows if a <= r[0] < b]
        if len(sel) < 5:
            continue
        follow = [100 * ((1 - p) if w else -p) - 100 * FEE * p * (1 - p) for p, w, _ in sel]
        mean, t = stats(follow)
        print(f"{a:.2f}-{b:.2f} {len(sel):4d} {sum(w for _, w, _ in sel) / len(sel):6.3f} "
              f"{sum(p for p, _, _ in sel) / len(sel):6.3f} {mean:12.2f} {t:6.2f}")

    print("\nprice >= c   n   follow c/sh  t     fade c/sh  t")
    for c in (0.40, 0.50, 0.55, 0.60, 0.65, 0.70):
        sel = [r for r in rows if r[0] >= c]
        follow = [100 * ((1 - p) if w else -p) - 100 * FEE * p * (1 - p) for p, w, _ in sel]
        fade = [100 * (-(1 - p) if w else p) - 100 * FEE * p * (1 - p) for p, w, _ in sel]
        (fm, ft), (dm, dt) = stats(follow), stats(fade)
        print(f"  {c:.2f}   {len(sel):4d}  {fm:8.2f} {ft:6.2f}  {dm:8.2f} {dt:6.2f}")


if __name__ == "__main__":
    main()
