"""Who actually made money on the 15m markets in the last few days.

Deliberately a short window. A three-day screen cannot prove skill — it is a
few hundred trades and the noise is large — so this does not pretend to rank
by significance. It answers a narrower question: which wallets are up right
now, how they got there, and whether any of it is reproducible by a copier.

Three things decide that last part and all three are printed:

  - **Maker or taker.** A maker's profit IS the spread a copier pays. You
    cannot copy a resting order: by the time you see the fill it has happened.
    Only the taker share of a wallet's edge is reachable by following it.
  - **Fee.** Takers pay ``size * 0.07 * p * (1-p)``. Copying means paying it
    again, so the taker column here is already net of it.
  - **Entry price.** The same fill is a different trade at 0.55 and at 0.95.

Per-fill contribution held to resolution is exact and additive:
``BUY at p -> size*(payout-p)``, ``SELL at p -> size*(p-payout)``. Summed over a
wallet it equals its realised PnL, so no position tracking is needed.
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from collections import Counter, defaultdict

FEE = 0.07
PROFILE = "https://polymarket.com/profile/"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/wallet_research/m15.db")
    ap.add_argument("--labels", default="data/wallet_research/maker15.db")
    ap.add_argument("--days", type=float, default=3.0)
    ap.add_argument("--min-markets", type=int, default=20)
    ap.add_argument("--min-shares", type=float, default=500)
    ap.add_argument("--active-hours", type=float, default=18.0,
                    help="must have traded this recently to be listed")
    ap.add_argument("--top", type=int, default=20)
    a = ap.parse_args()

    now = int(time.time())
    since = now - int(a.days * 86400)
    src = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    lab = sqlite3.connect(f"file:{a.labels}?mode=ro", uri=True)

    meta = {c: w for c, w in src.execute(
        "SELECT cid,winner FROM markets WHERE fetched=1 AND end_ts>=? "
        "AND winner IS NOT NULL", (since,))}
    labelled = {c for (c,) in lab.execute("SELECT cid FROM done")}
    cids = [c for c in meta if c in labelled]
    print(f"{len(cids)} labelled 15m markets in the last {a.days:g} days "
          f"({len(meta)} resolved, {len(meta) - len(cids)} not yet labelled)")

    # wallet -> dict of running totals
    W: dict[str, dict] = defaultdict(lambda: {
        "tk_pnl": 0.0, "tk_sh": 0.0, "tk_fee": 0.0, "tk_n": 0,
        "mk_pnl": 0.0, "mk_sh": 0.0, "mk_n": 0,
        "px_sh": 0.0, "last": 0, "mkts": set(), "wins": 0, "n": 0,
    })

    for cid in cids:
        winner = meta[cid]
        keys = Counter((t, w, s, p) for t, w, s, p in lab.execute(
            "SELECT txh,wallet,size_r,price_r FROM tk WHERE cid=?", (cid,)))
        for txh, wallet, side, oidx, size, price, ts in src.execute(
                "SELECT txh,wallet,side,oidx,size,price,ts FROM trades WHERE cid=?",
                (cid,)):
            w = (wallet or "").lower()
            key = (txh, w, round(size, 4), round(price, 4))
            taker = keys.get(key, 0) > 0
            if taker:
                keys[key] -= 1
            payout = 1.0 if oidx == winner else 0.0
            ps = (payout - price) if side == "BUY" else (price - payout)
            r = W[w]
            r["mkts"].add(cid)
            r["last"] = max(r["last"], ts or 0)
            r["n"] += 1
            r["wins"] += 1 if ps > 0 else 0
            # Entry price, normalised to a buy, weighted by size.
            r["px_sh"] += size * (price if side == "BUY" else 1.0 - price)
            if taker:
                fee = FEE * price * (1 - price)
                r["tk_pnl"] += size * (ps - fee)
                r["tk_fee"] += size * fee
                r["tk_sh"] += size
                r["tk_n"] += 1
            else:
                r["mk_pnl"] += size * ps
                r["mk_sh"] += size
                r["mk_n"] += 1

    rows = []
    for w, r in W.items():
        sh = r["tk_sh"] + r["mk_sh"]
        if sh < a.min_shares or len(r["mkts"]) < a.min_markets:
            continue
        if (now - r["last"]) > a.active_hours * 3600:
            continue
        rows.append((w, r, sh, r["tk_pnl"] + r["mk_pnl"]))

    rows.sort(key=lambda x: x[3], reverse=True)
    print(f"\n{len(rows)} wallets with >= {a.min_shares:,.0f} shares across "
          f">= {a.min_markets} markets, active in the last {a.active_hours:g}h")

    print(f"\n{'wallet':<44}{'net $':>9}{'shares':>9}{'mkts':>6}{'taker%':>8}"
          f"{'taker $':>9}{'tk c/sh':>9}{'avg px':>8}{'last':>7}")
    for w, r, sh, net in rows[:a.top]:
        tk_pct = 100 * r["tk_sh"] / sh
        tk_cps = (100 * r["tk_pnl"] / r["tk_sh"]) if r["tk_sh"] else 0.0
        avg_px = r["px_sh"] / sh
        age = (now - r["last"]) / 3600
        print(f"{w:<44}{net:>9,.0f}{sh:>9,.0f}{len(r['mkts']):>6}{tk_pct:>7.0f}%"
              f"{r['tk_pnl']:>9,.0f}{tk_cps:>+9.2f}{avg_px:>8.2f}{age:>6.1f}h")

    print("\n--- copyable candidates: taker-heavy AND taker-profitable ---")
    print("(a maker's profit is the spread a copier pays, so it is not copyable)")
    cands = [(w, r, sh, net) for w, r, sh, net in rows
             if r["tk_sh"] > 0.6 * sh and r["tk_pnl"] > 0]
    cands.sort(key=lambda x: x[1]["tk_pnl"] / max(x[1]["tk_sh"], 1), reverse=True)
    if not cands:
        print("  none")
    for w, r, sh, net in cands[:a.top]:
        print(f"  {PROFILE}{w}")
        print(f"      taker {r['tk_pnl']:+,.0f} on {r['tk_sh']:,.0f}sh "
              f"({100 * r['tk_pnl'] / r['tk_sh']:+.2f}c/share, fee "
              f"{r['tk_fee']:,.0f} already paid) &middot; "
              f"{len(r['mkts'])} markets &middot; avg entry "
              f"{r['px_sh'] / sh:.2f} &middot; {r['n']} fills")


main()
