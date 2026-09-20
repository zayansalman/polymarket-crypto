"""Does the taker screen predict, or does it fit noise?

The screen ranks wallets on a window of history. The only honest test is
whether the wallets it picks go on to make money in a period it never saw.

Screens on [train_start, train_end), then measures exactly those wallets over
[train_end, test_end) — a strict holdout. The benchmark is every OTHER wallet
active in the same test window, so the question is not "did they profit" but
"did they beat the population the screen chose them out of".

Run on the same db the live registry came from, so the answer applies to the
registry rather than to a different universe.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import time


def window_stats(con, lo: int, hi: int, family: str | None):
    """Per wallet over one window: net P&L after the taker fee, and shares."""
    q = """
      SELECT t.wallet, t.cid, t.oidx,
             SUM(CASE WHEN t.side='BUY'  THEN t.size ELSE 0 END) b,
             SUM(CASE WHEN t.side='SELL' THEN t.size ELSE 0 END) s,
             SUM(CASE WHEN t.side='BUY'  THEN t.size*t.price ELSE 0 END) cost,
             SUM(CASE WHEN t.side='SELL' THEN t.size*t.price ELSE 0 END) proc,
             SUM(CASE WHEN t.side='BUY' THEN t.size*0.07*t.price*(1-t.price)
                      ELSE 0 END) fee,
             m.winner
      FROM trades t JOIN markets m ON m.cid=t.cid AND m.fetched=1
      WHERE m.end_ts >= ? AND m.end_ts < ?
    """
    params = [lo, hi]
    if family:
        q += " AND m.family=?"
        params.append(family)
    q += " GROUP BY t.wallet, t.cid, t.oidx"

    per: dict[str, dict] = {}
    cur = con.execute(q, params)
    rows: dict[tuple[str, str], dict] = {}
    for w, cid, oidx, b, s, cost, proc, fee, winner in cur:
        d = rows.setdefault((w, cid), {"b": [0.0, 0.0], "s": [0.0, 0.0],
                                       "cost": 0.0, "proc": 0.0, "fee": 0.0,
                                       "win": winner})
        i = 1 if oidx else 0
        d["b"][i] += b
        d["s"][i] += s
        d["cost"] += cost
        d["proc"] += proc
        d["fee"] += fee
    for (w, _cid), d in rows.items():
        net = [d["b"][i] - d["s"][i] for i in (0, 1)]
        sp = max(0.0, -min(net))
        eff = [net[i] + sp for i in (0, 1)]
        bought = d["b"][0] + d["b"][1] + 2 * sp
        if bought <= 0:
            continue
        pnl = d["proc"] - d["cost"] - sp + eff[d["win"]] - d["fee"]
        a = per.setdefault(w, {"pnl": 0.0, "sh": 0.0, "n": 0,
                               "cost": 0.0, "two": 0, "pnls": []})
        a["pnl"] += pnl
        a["sh"] += bought
        a["cost"] += d["cost"] + sp
        a["n"] += 1
        a["pnls"].append(pnl)
        lo_s, hi_s = min(d["b"]), max(d["b"])
        if hi_s > 0 and lo_s > 0.05 * hi_s:
            a["two"] += 1
    return per


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/wallet_research/wallets.db")
    ap.add_argument("--family", default="1h")
    ap.add_argument("--train-days", type=int, default=30)
    ap.add_argument("--test-days", type=int, default=30)
    ap.add_argument("--gap-days", type=int, default=0,
                    help="days between train and test windows")
    ap.add_argument("--min-markets", type=int, default=15)
    ap.add_argument("--top", type=int, default=40)
    a = ap.parse_args()

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    newest = con.execute(
        "SELECT MAX(end_ts) FROM markets WHERE fetched=1").fetchone()[0]
    test_hi = newest
    test_lo = test_hi - a.test_days * 86400
    train_hi = test_lo - a.gap_days * 86400
    train_lo = train_hi - a.train_days * 86400
    fmt = lambda t: time.strftime("%Y-%m-%d", time.gmtime(t))
    print(f"train {fmt(train_lo)} -> {fmt(train_hi)}   "
          f"test {fmt(test_lo)} -> {fmt(test_hi)}   family={a.family}")

    train = window_stats(con, train_lo, train_hi, a.family)
    test = window_stats(con, test_lo, test_hi, a.family)
    print(f"wallets active in train {len(train)}, in test {len(test)}")

    # The screen: one-sided, enough markets, positive net edge per share.
    picked = []
    for w, d in train.items():
        if d["n"] < a.min_markets or d["sh"] <= 0 or d["cost"] <= 0:
            continue
        if d["two"] / d["n"] > 0.25:      # quoting, not taking
            continue
        edge = 100 * d["pnl"] / d["sh"]
        if edge <= 0:
            continue
        picked.append((edge, w))
    picked.sort(reverse=True)
    chosen = [w for _e, w in picked[: a.top]]
    print(f"screen picked {len(chosen)} wallets (top {a.top} by edge/share)")

    def summarise(ws, label):
        rows = [test[w] for w in ws if w in test and test[w]["sh"] > 0]
        if not rows:
            print(f"  {label}: none active in the test window")
            return None
        sh = sum(r["sh"] for r in rows)
        pnl = sum(r["pnl"] for r in rows)
        edges = [100 * r["pnl"] / r["sh"] for r in rows]
        pos = sum(1 for e in edges if e > 0)
        print(f"  {label:26} {len(rows):5d} wallets  "
              f"edge {100*pnl/sh:+7.3f}c/share  "
              f"median {statistics.median(edges):+7.2f}c  "
              f"{100*pos/len(rows):3.0f}% profitable")
        return edges

    print("\nOUT OF SAMPLE:")
    a_edges = summarise(chosen, "screened wallets")
    others = [w for w in test if w not in set(chosen)]
    b_edges = summarise(others, "everyone else")

    if a_edges and b_edges and len(a_edges) > 1:
        ma, mb = statistics.mean(a_edges), statistics.mean(b_edges)
        sa = statistics.stdev(a_edges) / math.sqrt(len(a_edges))
        sb = statistics.stdev(b_edges) / math.sqrt(len(b_edges))
        se = math.sqrt(sa * sa + sb * sb)
        t = (ma - mb) / se if se > 0 else 0.0
        print(f"\n  difference {ma-mb:+.2f}c/share, t={t:+.2f}")
        print("  " + ("screen has predictive value" if t > 2
                      else "NOT distinguishable from chance"))


main()
