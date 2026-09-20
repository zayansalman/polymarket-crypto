"""Rank wallets by copier edge, not by how much money they made.

What changed from analyze.py, and why:

  * The unit is **net edge per share in cents**, after the crypto taker fee a
    follower pays on every entry (shares * 0.07 * p * (1-p)). Total PnL rewards
    size; edge per share rewards being right.
  * A wallet that buys BOTH outcomes in most of its markets is quoting, not
    forecasting. Its profit is the spread a copier pays. Measured and excluded.
  * Significance is computed on per-market copier PnL, then corrected across
    every wallet tested (Benjamini-Hochberg FDR). Screening 10k wallets and
    keeping the best t-stat finds the top of a noise distribution; FDR is what
    stops that.
  * Robustness: the edge must survive deleting the wallet's 3 best markets.
"""
from __future__ import annotations
import argparse
import csv
import math
import sqlite3
import statistics
import time
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "data" / "wallet_research" / "wallets.db"
NOW = int(time.time())
FEE_RATE = 0.07  # crypto category; makers pay 0


def norm_sf(z: float) -> float:
    """Upper-tail probability of the standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def load_markets(con):
    return {cid: (fam, end_ts, winner)
            for cid, fam, end_ts, winner in
            con.execute("SELECT cid,family,end_ts,winner FROM markets WHERE fetched=1")}


def wallet_stream(con, mk, family):
    """Yield (wallet, name, {cid: per-market dict}) one wallet at a time."""
    names = dict(con.execute(
        "SELECT wallet, name FROM trades WHERE name IS NOT NULL GROUP BY wallet"))
    cur = con.execute("SELECT wallet,cid,oidx,b,s,cost,proc,n,last_ts,first_buy,tfee"
                      " FROM wm ORDER BY wallet")
    cw = None
    per = {}
    for w, cid, oidx, b, s, cost, proc, n, last_ts, first_buy, tfee in cur:
        if w != cw:
            if cw is not None:
                yield cw, names.get(cw, ""), per
            cw, per = w, {}
        if cid not in mk or (family and mk[cid][0] != family):
            continue
        d = per.setdefault(cid, {"b": [0.0, 0.0], "s": [0.0, 0.0], "cost": 0.0,
                                 "proc": 0.0, "tfee": 0.0, "last": 0, "fb": None})
        i = 1 if oidx else 0
        d["b"][i] += b
        d["s"][i] += s
        d["cost"] += cost
        d["proc"] += proc
        d["tfee"] += tfee
        d["last"] = max(d["last"], last_ts)
        if first_buy is not None:
            d["fb"] = first_buy if d["fb"] is None else min(d["fb"], first_buy)
    if cw is not None:
        yield cw, names.get(cw, ""), per


def measure(w, name, per, mk):
    pnls, shares_l, leads = [], [], []
    two_sided = 0
    tot_shares = tot_cost = tot_fee = held = acquired = 0.0
    buckets = [0.0, 0.0, 0.0]
    f1h = f24h = 0
    last = 0
    for cid, d in per.items():
        fam, end_ts, winner = mk[cid]
        net = [d["b"][i] - d["s"][i] for i in (0, 1)]
        sp = max(0.0, -min(net))
        eff = [net[i] + sp for i in (0, 1)]
        bought = d["b"][0] + d["b"][1] + sp * 2
        if bought <= 0:
            continue
        # copier pays the taker fee on every entry
        pnl = d["proc"] - d["cost"] - sp + eff[winner] - d["tfee"]
        pnls.append(pnl)
        shares_l.append(bought)
        tot_shares += bought
        tot_cost += d["cost"] + sp
        tot_fee += d["tfee"]
        acquired += bought
        held += min(eff[0] + eff[1], bought)
        lo, hi = min(d["b"]), max(d["b"])
        if hi > 0 and lo > 0.05 * hi:
            two_sided += 1
        age = (NOW - end_ts) / 86400.0
        buckets[0 if age <= 30 else (1 if age <= 60 else 2)] += pnl
        if d["fb"]:
            leads.append((end_ts - d["fb"]) / 60.0)
        last = max(last, d["last"])
        if fam == "1h":
            f1h += 1
        else:
            f24h += 1

    n = len(pnls)
    if n < 2 or tot_shares <= 0:
        return None
    mean = sum(pnls) / n
    sd = statistics.stdev(pnls) if n > 1 else 0.0
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0

    order = sorted(range(n), key=lambda i: -pnls[i])
    drop = set(order[:3])
    rest = [pnls[i] for i in range(n) if i not in drop]
    rest_sh = sum(shares_l[i] for i in range(n) if i not in drop)
    rest_pnl = sum(rest)
    rest_edge = 100.0 * rest_pnl / rest_sh if rest_sh > 0 else 0.0
    if len(rest) > 1 and statistics.stdev(rest) > 0:
        rest_t = (rest_pnl / len(rest)) / (statistics.stdev(rest) / math.sqrt(len(rest)))
    else:
        rest_t = 0.0

    leads.sort()
    return {
        "wallet": w, "name": name, "mkts": n,
        "net_pnl": sum(pnls),
        "edge_c": 100.0 * sum(pnls) / tot_shares,      # cents/share, after fee
        "t": t, "p": norm_sf(t) if t > 0 else 1.0,
        "rest_edge_c": rest_edge, "rest_t": rest_t,
        "top3_share": (sum(pnls[i] for i in drop) / sum(pnls)) if sum(pnls) > 0 else 9.9,
        "two_sided": two_sided / n,
        "hold": held / acquired if acquired else 0.0,
        "shares": tot_shares, "cost": tot_cost, "fee": tot_fee,
        "stake": tot_cost / n,
        "b30": buckets[0], "b60": buckets[1], "b90": buckets[2],
        "lead_med": statistics.median(leads) if leads else 0.0,
        "lead_p10": leads[int(len(leads) * .1)] if leads else 0.0,
        "f1h": f1h, "f24h": f24h, "last": last,
    }


def bh_fdr(rows, q):
    """Benjamini-Hochberg: largest k with p_(k) <= k/m * q survives."""
    m = len(rows)
    ordered = sorted(rows, key=lambda r: r["p"])
    cutoff = 0
    for k, r in enumerate(ordered, 1):
        if r["p"] <= k / m * q:
            cutoff = k
    for k, r in enumerate(ordered, 1):
        r["fdr_pass"] = k <= cutoff
        r["bh_rank"] = k
    return ordered[:cutoff], m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", choices=["1h", "24h"])
    ap.add_argument("--min-markets", type=int, default=30)
    ap.add_argument("--q", type=float, default=0.10, help="FDR level")
    ap.add_argument("--max-two-sided", type=float, default=0.25)
    ap.add_argument("--min-hold", type=float, default=0.8)
    ap.add_argument("--min-lead", type=float, default=2.0)
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--csv", default="ranked.csv")
    ap.add_argument("--db", help="override the sqlite path")
    a = ap.parse_args()

    global DB
    if a.db:
        DB = Path(a.db)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    mk = load_markets(con)
    rows = []
    for w, name, per in wallet_stream(con, mk, a.family):
        if len(per) < a.min_markets:
            continue
        r = measure(w, name, per, mk)
        if r:
            rows.append(r)
    print(f"wallets with >={a.min_markets} markets: {len(rows)}")

    # Structural gates first — these are not statistical claims, they are
    # statements about whether the trade can be followed at all.
    elig = [r for r in rows
            if r["two_sided"] <= a.max_two_sided
            and r["hold"] >= a.min_hold
            and r["lead_p10"] >= a.min_lead
            and r["last"] > NOW - 14 * 86400]
    print(f"structurally copyable (one-sided, holds to resolution, "
          f"{a.min_lead}+ min runway at p10, active <14d): {len(elig)}")

    survivors, m = bh_fdr(elig, a.q)
    print(f"survive Benjamini-Hochberg FDR q={a.q} across {m} tested: {len(survivors)}")

    survivors.sort(key=lambda r: -r["edge_c"])
    hdr = (f"{'wallet':44}{'name':16}{'edge¢':>7}{'t':>6}{'p':>9}{'exTop3¢':>9}"
           f"{'exT3_t':>7}{'net$':>9}{'mkts':>6}{'fam':>5}{'stake$':>8}"
           f"{'2side%':>7}{'lead10':>7}{'30d$':>8}{'60d$':>8}{'90d$':>8}")
    print()
    print(hdr)
    print("-" * len(hdr))
    for r in survivors[:a.top]:
        fam = "24h" if r["f24h"] > r["f1h"] else "1h"
        print(f"{r['wallet']:44}{(r['name'] or '-')[:15]:16}{r['edge_c']:7.2f}{r['t']:6.2f}"
              f"{r['p']:9.5f}{r['rest_edge_c']:9.2f}{r['rest_t']:7.2f}{r['net_pnl']:9.0f}"
              f"{r['mkts']:6d}{fam:>5}{r['stake']:8.0f}{r['two_sided']*100:7.0f}"
              f"{r['lead_p10']:7.1f}{r['b30']:8.0f}{r['b60']:8.0f}{r['b90']:8.0f}")
    if survivors:
        out = DB.parent / a.csv
        with open(out, "w", newline="") as fh:
            wtr = csv.DictWriter(fh, fieldnames=list(survivors[0].keys()))
            wtr.writeheader()
            wtr.writerows(survivors)
        print("\nwrote", out)

main()
