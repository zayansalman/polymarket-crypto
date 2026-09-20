"""Cross-check a wallet's reconstructed PnL and measure how copyable it is.

Three questions the ranking table cannot answer on its own:
  1. Does the PnL we computed from fills agree with Polymarket's own numbers?
  2. Are their fills taker-side (copyable by crossing) or maker-side (their edge
     IS the spread, so a copier crossing to get in pays it away)?
  3. How much runway is there between their entry and settlement?
"""
from __future__ import annotations
import argparse
import json
import sqlite3
import statistics
import time
import urllib.parse
import urllib.request
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "data" / "wallet_research" / "wallets.db"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
      "Accept": "application/json"}


def get(url, params=None, tries=4):
    q = "?" + urllib.parse.urlencode(params, doseq=True) if params else ""
    for a in range(tries):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url + q, headers=UA), timeout=40) as r:
                return json.load(r)
        except Exception:
            if a == tries - 1:
                raise
            time.sleep(1.5 * (a + 1))


def one_market_audit(con, wallet, cid):
    fam, end_ts, winner, slug = con.execute(
        "SELECT family,end_ts,winner,slug FROM markets WHERE cid=?", (cid,)).fetchone()
    rows = con.execute(
        "SELECT side,oidx,size,price,ts FROM trades WHERE cid=? AND wallet=? ORDER BY ts",
        (cid, wallet)).fetchall()
    print(f"\n  audit {slug}  (winner outcome index {winner})")
    b = [0.0, 0.0]
    s = [0.0, 0.0]
    cost = proc = 0.0
    for side, oidx, size, price, ts in rows:
        lead = (end_ts - ts) / 60.0
        print(f"    {side:4} idx{oidx} {size:9.2f} @ {price:.3f}  "
              f"{lead:7.1f} min before close")
        if side == "BUY":
            b[oidx] += size
            cost += size * price
        else:
            s[oidx] += size
            proc += size * price
    net = [b[i] - s[i] for i in (0, 1)]
    sp = max(0.0, -min(net))
    eff = [net[i] + sp for i in (0, 1)]
    pnl = proc - cost - sp + eff[winner]
    print(f"    cost ${cost:.2f}  proceeds ${proc:.2f}  splits {sp:.2f}  "
          f"held at close {eff}  payout ${eff[winner]:.2f}  => PnL ${pnl:.2f}")
    return pnl


def taker_fraction(con, wallet, sample=25):
    """What share of their fills also appear in the taker-only feed."""
    cids = [r[0] for r in con.execute(
        "SELECT DISTINCT cid FROM trades WHERE wallet=? ORDER BY RANDOM() LIMIT ?",
        (wallet, sample))]
    mine = taker = 0
    for cid in cids:
        ours = {(r[0], round(r[1], 4), round(r[2], 4))
                for r in con.execute(
                    "SELECT txh,size,price FROM trades WHERE cid=? AND wallet=?",
                    (cid, wallet))}
        if not ours:
            continue
        try:
            feed = get("https://data-api.polymarket.com/trades",
                       {"market": cid, "limit": 500, "takerOnly": "true"})
        except Exception:
            continue
        theirs = {(t.get("transactionHash"), round(float(t["size"]), 4),
                   round(float(t["price"]), 4))
                  for t in feed if t["proxyWallet"].lower() == wallet.lower()}
        mine += len(ours)
        taker += len(ours & theirs)
    return taker, mine


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wallet")
    ap.add_argument("--audits", type=int, default=2)
    a = ap.parse_args()
    w = a.wallet.lower()
    con = sqlite3.connect(DB)

    n_mkts, n_tr = con.execute(
        "SELECT COUNT(DISTINCT cid), COUNT(*) FROM trades WHERE wallet=?", (w,)).fetchone()
    print(f"wallet {w}\n  in-scope markets {n_mkts}, fills {n_tr}")

    print("\n== Polymarket's own numbers ==")
    try:
        val = get("https://data-api.polymarket.com/value", {"user": w})
        print("  portfolio value:", val)
    except Exception as e:
        print("  value:", e)
    try:
        pnl = get("https://user-pnl-api.polymarket.com/user-pnl",
                  {"user_address": w, "interval": "1m", "fidelity": "1d"})
        if pnl:
            print(f"  cumulative PnL curve: {len(pnl)} pts, "
                  f"start ${pnl[0]['p']:.0f} -> end ${pnl[-1]['p']:.0f} "
                  f"(delta ${pnl[-1]['p']-pnl[0]['p']:+.0f} over the window)")
    except Exception as e:
        print("  user-pnl:", e)

    print("\n== per-market audits (arithmetic shown in full) ==")
    cids = [r[0] for r in con.execute(
        "SELECT cid FROM trades WHERE wallet=? GROUP BY cid ORDER BY MAX(ts) DESC LIMIT ?",
        (w, a.audits))]
    for cid in cids:
        one_market_audit(con, w, cid)

    print("\n== copyability ==")
    taker, mine = taker_fraction(con, w)
    if mine:
        print(f"  taker-side fills: {taker}/{mine} = {100*taker/mine:.0f}% "
              f"(low % = they are the maker; a copier crossing the spread pays "
              f"away the edge they collect)")
    leads = []
    for cid, first_buy in con.execute(
            "SELECT cid, MIN(ts) FROM trades WHERE wallet=? AND side='BUY' GROUP BY cid", (w,)):
        r = con.execute("SELECT end_ts FROM markets WHERE cid=?", (cid,)).fetchone()
        if r:
            leads.append((r[0] - first_buy) / 60.0)
    if leads:
        leads.sort()
        print(f"  entry lead time before settlement (min): "
              f"p10 {leads[int(len(leads)*.1)]:.1f}  median {statistics.median(leads):.1f}  "
              f"p90 {leads[int(len(leads)*.9)]:.1f}")
        tight = sum(1 for x in leads if x < 2) / len(leads)
        print(f"  entries with under 2 min of runway: {100*tight:.0f}%")

main()
