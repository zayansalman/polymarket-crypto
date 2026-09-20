"""Does a wallet's maker edge carry into the NEXT month, or is it last month's luck?

The taker screen failed exactly here: picked wallets were no likelier to profit
out of sample than anyone else. This asks the same question of the maker side,
where fills are real resting orders and there is no fee to clear.

For each adjacent pair of months: rank on the earlier one, then measure the same
wallets on the later one against every other wallet that made markets in it. The
baseline is the comparison that matters — a positive test number means nothing
if everybody was positive that month.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import time
from collections import Counter, defaultdict


def edges(src, lab, cids, meta) -> dict[str, dict[str, list]]:
    """{month: {wallet: [shares, pnl, markets]}} over maker fills only."""
    out: dict[str, dict[str, list]] = defaultdict(
        lambda: defaultdict(lambda: [0.0, 0.0, set()]))
    for cid in cids:
        _fam, ets, winner = meta[cid]
        month = time.strftime("%Y-%m", time.gmtime(ets))
        keys = Counter((t, w, s, p) for t, w, s, p in lab.execute(
            "SELECT txh,wallet,size_r,price_r FROM tk WHERE cid=?", (cid,)))
        for txh, wallet, side, oidx, size, price in src.execute(
                "SELECT txh,wallet,side,oidx,size,price FROM trades WHERE cid=?",
                (cid,)):
            w = (wallet or "").lower()
            key = (txh, w, round(size, 4), round(price, 4))
            if keys.get(key, 0) > 0:
                keys[key] -= 1
                continue                       # taker fill, not our subject
            payout = 1.0 if oidx == winner else 0.0
            ps = (payout - price) if side == "BUY" else (price - payout)
            rec = out[month][w]
            rec[0] += size
            rec[1] += size * ps
            rec[2].add(cid)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/wallet_research/wallets.db")
    ap.add_argument("--labels", default="data/wallet_research/maker.db")
    ap.add_argument("--min-shares", type=float, default=5000)
    ap.add_argument("--min-markets", type=int, default=10)
    ap.add_argument("--top", type=int, default=20)
    a = ap.parse_args()

    src = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    lab = sqlite3.connect(f"file:{a.labels}?mode=ro", uri=True)
    meta = {c: (f, e, w) for c, f, e, w in src.execute(
        "SELECT cid,family,end_ts,winner FROM markets WHERE fetched=1")}
    cids = [c for (c,) in lab.execute("SELECT cid FROM done")
            if c in meta and meta[c][2] is not None]
    print(f"{len(cids)} labelled markets")

    by_month = edges(src, lab, cids, meta)
    months = sorted(by_month)
    print(f"months: {', '.join(months)}\n")

    def qualified(m):
        return {w: v for w, v in by_month[m].items()
                if v[0] >= a.min_shares and len(v[2]) >= a.min_markets}

    print(f"{'train':>8} {'test':>8} {'picked':>7} {'their test':>12} "
          f"{'everyone else':>14} {'gap':>8}")
    gaps = []
    for tr, te in zip(months, months[1:]):
        a_q, b_q = qualified(tr), qualified(te)
        if len(a_q) < 5 or len(b_q) < 5:
            print(f"{tr:>8} {te:>8}   too few qualifying wallets")
            continue
        picks = [w for w, _ in sorted(
            a_q.items(), key=lambda kv: kv[1][1] / kv[1][0], reverse=True)][:a.top]
        picked = [b_q[w] for w in picks if w in b_q]
        if len(picked) < 3:
            print(f"{tr:>8} {te:>8}   only {len(picked)} picks traded in {te}")
            continue
        rest = [v for w, v in b_q.items() if w not in set(picks)]
        p_edge = sum(v[1] for v in picked) / sum(v[0] for v in picked)
        r_edge = sum(v[1] for v in rest) / sum(v[0] for v in rest)
        gaps.append(p_edge - r_edge)
        print(f"{tr:>8} {te:>8} {len(picked):>7} {100*p_edge:>11.3f}c "
              f"{100*r_edge:>13.3f}c {100*(p_edge-r_edge):>+7.3f}c")

    if gaps:
        m = statistics.mean(gaps)
        print(f"\nmean gap {100*m:+.3f}c/share over {len(gaps)} splits; "
              f"positive in {sum(1 for g in gaps if g > 0)}/{len(gaps)}")
        if len(gaps) > 2:
            se = statistics.stdev(gaps) / math.sqrt(len(gaps))
            print(f"t = {m/se:+.2f}")
        print("\n" + ("picking makers CARRIES out of sample"
                      if m > 0 else "picking makers does NOT carry"))


main()
