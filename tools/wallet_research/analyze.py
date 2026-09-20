"""Score wallets on the scanned 1h/24h Up-or-Down universe.

PnL is reconstructed from each wallet's own fills plus the market's settlement:

    pnl = sell proceeds - buy cost - $1 per implied split pair
          + $1 per winning share still held at the close

Selling more of a token than was bought means those shares came from a split, so
the $1 pair cost is charged back rather than counted as free proceeds.

Aggregation runs in SQL and streams one wallet at a time, so memory stays flat
regardless of how many trades are in the db.
"""
from __future__ import annotations
import argparse
import sqlite3
import time
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "data" / "wallet_research" / "wallets.db"
NOW = int(time.time())


def build_summary(con: sqlite3.Connection, rebuild: bool) -> None:
    if rebuild:
        con.execute("DROP TABLE IF EXISTS wm")
    exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='wm'").fetchone()
    if exists:
        return
    con.execute("""
        CREATE TABLE wm AS
        SELECT t.wallet AS wallet, t.cid AS cid, t.oidx AS oidx,
               SUM(CASE WHEN t.side='BUY'  THEN t.size ELSE 0 END) AS b,
               SUM(CASE WHEN t.side='SELL' THEN t.size ELSE 0 END) AS s,
               SUM(CASE WHEN t.side='BUY'  THEN t.size*t.price ELSE 0 END) AS cost,
               SUM(CASE WHEN t.side='BUY' THEN t.size*0.07*t.price*(1-t.price) ELSE 0 END) AS tfee,
               SUM(CASE WHEN t.side='SELL' THEN t.size*t.price ELSE 0 END) AS proc,
               COUNT(*) AS n, MAX(t.ts) AS last_ts, MIN(t.ts) AS first_ts,
               MIN(CASE WHEN t.side='BUY' THEN t.ts END) AS first_buy
        FROM trades t JOIN markets m ON m.cid=t.cid AND m.fetched=1
        GROUP BY t.wallet, t.cid, t.oidx
    """)
    con.execute("CREATE INDEX idx_wm_wallet ON wm(wallet)")
    con.commit()


def wallet_rows(con: sqlite3.Connection, family: str | None):
    """Yield (wallet, [per-market dict, ...]) streaming in wallet order."""
    mk = {cid: (fam, end_ts, winner)
          for cid, fam, end_ts, winner in
          con.execute("SELECT cid,family,end_ts,winner FROM markets WHERE fetched=1")}
    names = {}
    for w, nm in con.execute(
            "SELECT wallet, name FROM trades WHERE name IS NOT NULL GROUP BY wallet"):
        names[w] = nm
    cur = con.execute(
        "SELECT wallet,cid,oidx,b,s,cost,proc,n,last_ts,first_buy,tfee FROM wm ORDER BY wallet, cid")
    cur_w = None
    per: dict = {}
    for w, cid, oidx, b, s, cost, proc, n, last_ts, first_buy, tfee in cur:
        if w != cur_w:
            if cur_w is not None:
                yield cur_w, names.get(cur_w, ""), per
            cur_w, per = w, {}
        if cid not in mk:
            continue
        if family and mk[cid][0] != family:
            continue
        d = per.setdefault(cid, {"b": [0.0, 0.0], "s": [0.0, 0.0], "cost": 0.0,
                                 "proc": 0.0, "n": 0, "last": 0, "fb": None, "tfee": 0.0})
        i = 1 if oidx else 0
        d["b"][i] += b
        d["s"][i] += s
        d["cost"] += cost
        d["proc"] += proc
        d["tfee"] += tfee
        d["n"] += n
        d["last"] = max(d["last"], last_ts)
        if first_buy is not None:
            d["fb"] = first_buy if d["fb"] is None else min(d["fb"], first_buy)
    if cur_w is not None:
        yield cur_w, names.get(cur_w, ""), per


def score(con: sqlite3.Connection, family: str | None):
    mk = {cid: (fam, end_ts, winner)
          for cid, fam, end_ts, winner in
          con.execute("SELECT cid,family,end_ts,winner FROM markets WHERE fetched=1")}
    out = []
    for w, name, per in wallet_rows(con, family):
        if len(per) < 5:
            continue
        pnl = cost = bought = held = splits = fee = 0.0
        wins = mkts = trades = f1h = f24h = 0
        buckets = [0.0, 0.0, 0.0]
        per_mkt = []
        leads: list[float] = []
        entry_num = entry_den = 0.0
        last = 0
        for cid, d in per.items():
            fam, end_ts, winner = mk[cid]
            net = [d["b"][i] - d["s"][i] for i in (0, 1)]
            sp = max(0.0, -min(net))
            eff = [net[i] + sp for i in (0, 1)]
            acquired = d["b"][0] + d["b"][1] + sp * 2
            if acquired <= 0:
                continue
            p = d["proc"] - d["cost"] - sp + eff[winner]
            pnl += p
            mkts += 1
            wins += 1 if p > 0 else 0
            fee += d["tfee"]
            cost += d["cost"] + sp
            bought += acquired
            held += min(eff[0] + eff[1], acquired)
            splits += sp
            trades += d["n"]
            last = max(last, d["last"])
            if fam == "1h":
                f1h += 1
            else:
                f24h += 1
            age = (NOW - end_ts) / 86400.0
            buckets[0 if age <= 30 else (1 if age <= 60 else 2)] += p
            per_mkt.append(p)
            entry_num += d["cost"]
            entry_den += d["b"][0] + d["b"][1]
            if d["fb"]:
                leads.append((end_ts - d["fb"]) / 60.0)
        if mkts < 5 or bought <= 0:
            continue
        top = max(per_mkt) if per_mkt else 0.0
        out.append({
            "wallet": w, "name": name, "pnl": pnl, "mkts": mkts,
            "winrate": wins / mkts, "hold": held / bought,
            "roi": pnl / cost if cost > 0 else 0.0, "cost": cost,
            "avg_stake": cost / mkts, "trades": trades, "f1h": f1h, "f24h": f24h,
            "b30": buckets[0], "b60": buckets[1], "b90": buckets[2],
            "top_share": (top / pnl) if pnl > 0 else 9.9,
            "avg_entry": entry_num / entry_den if entry_den else 0.0,
            "last": last, "splits": splits, "fee": fee,
            "pnl_net": pnl - fee,
            "roi_net": (pnl - fee) / cost if cost > 0 else 0.0,
            "lead_min": (sorted(leads)[len(leads)//2] if leads else 0.0),
            "lead_p10": (sorted(leads)[max(0,int(len(leads)*0.1))] if leads else 0.0),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", choices=["1h", "24h"])
    ap.add_argument("--min-markets", type=int, default=20)
    ap.add_argument("--min-hold", type=float, default=0.8)
    ap.add_argument("--min-lead", type=float, default=5.0,
                    help="median minutes between entry and settlement; filters out\n                          last-second snipers a delayed copy can never follow")
    ap.add_argument("--min-net", type=float, default=0.0,
                    help="minimum PnL after we pay the taker fee to get in")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--sort", default="pnl")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--csv")
    a = ap.parse_args()
    con = sqlite3.connect(DB)
    build_summary(con, a.rebuild)
    n_mk = con.execute("SELECT COUNT(*) FROM markets WHERE fetched=1").fetchone()[0]
    rows = score(con, a.family)
    print(f"markets scored: {n_mk}   wallets with >=5 markets: {len(rows)}")
    if not a.all:
        rows = [r for r in rows
                if r["mkts"] >= a.min_markets and r["hold"] >= a.min_hold
                and r["b30"] > 0 and (r["b60"] + r["b90"]) > 0
                and r["top_share"] < 0.5
                and r["lead_min"] >= a.min_lead
                and r["pnl_net"] > a.min_net
                and r["last"] > NOW - 10 * 86400]
        print(f"after filters: {len(rows)}")
    rows.sort(key=lambda r: r[a.sort], reverse=True)
    hdr = (f"{'wallet':44}{'name':18}{'pnl$':>9}{'mkts':>6}{'win%':>6}{'hold%':>7}"
           f"{'net$':>9}{'roi%':>7}{'stake$':>8}{'30d$':>8}{'60d$':>8}{'90d$':>8}{'1h':>5}{'24h':>5}{'entry':>7}{'lead_m':>8}{'lead10':>8}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows[:a.top]:
        print(f"{r['wallet']:44}{(r['name'] or '')[:17]:18}{r['pnl']:9.0f}{r['mkts']:6d}"
              f"{r['winrate']*100:6.1f}{r['hold']*100:7.1f}{r['pnl_net']:9.0f}{r['roi']*100:7.1f}"
              f"{r['avg_stake']:8.0f}{r['b30']:8.0f}{r['b60']:8.0f}{r['b90']:8.0f}"
              f"{r['f1h']:5d}{r['f24h']:5d}{r['avg_entry']:7.3f}"
              f"{r['lead_min']:8.1f}{r['lead_p10']:8.1f}")
    if a.csv:
        import csv
        with open(a.csv, "w", newline="") as fh:
            wtr = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            wtr.writeheader()
            wtr.writerows(rows)
        print("wrote", a.csv)

main()
