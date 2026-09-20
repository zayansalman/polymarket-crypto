"""Does the slippage filter have an edge, independent of whose fill it is?

Wallet selection did not survive a holdout test. The filter did not lose money:
declining when the book moved against us netted +$48.68 live while taking every
observed fill lost $54.52. That is a different and much narrower claim — about
a rule, not about a wallet — and it is testable on history.

The rule: after someone buys at price P, look at what the same token trades at
~30s later. If it has moved no more than `cap` cents ABOVE P, take it; otherwise
decline. This asks whether entries that survive that test settle better than the
ones that fail it, across every wallet in the dataset rather than a chosen few.

Both arms are scored the same way, holding to resolution and paying the taker
fee, so the only difference is the condition.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics

FEE = 0.07


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/wallet_research/wallets.db")
    ap.add_argument("--family", default="1h")
    ap.add_argument("--lag-lo", type=int, default=20, help="seconds after the fill")
    ap.add_argument("--lag-hi", type=int, default=60)
    ap.add_argument("--cap-cents", type=float, default=3.0)
    ap.add_argument("--markets", type=int, default=1500)
    a = ap.parse_args()

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    cids = [r[0] for r in con.execute(
        "SELECT cid FROM markets WHERE fetched=1 AND family=? "
        "ORDER BY end_ts DESC LIMIT ?", (a.family, a.markets))]
    print(f"scanning {len(cids)} {a.family} markets")

    cap = a.cap_cents / 100.0
    took: list[float] = []      # per-share net return when the rule accepts
    declined: list[float] = []  # ... and when it declines
    n_seen = 0

    for k, cid in enumerate(cids):
        winner = con.execute(
            "SELECT winner FROM markets WHERE cid=?", (cid,)).fetchone()[0]
        rows = con.execute(
            "SELECT oidx, ts, size, price, side FROM trades WHERE cid=? ORDER BY ts",
            (cid,)).fetchall()
        buys = [(o, t, s, p) for o, t, s, p, side in rows if side == "BUY"]
        if len(buys) < 4:
            continue
        # tape per outcome for the look-ahead
        tape: dict[int, list[tuple[int, float, float]]] = {}
        for o, t, s, p in buys:
            tape.setdefault(o, []).append((t, s, p))

        for o, t, s, p in buys:
            later = [(sz, px) for tt, sz, px in tape.get(o, ())
                     if t + a.lag_lo <= tt <= t + a.lag_hi]
            if not later:
                continue
            n_seen += 1
            vw = sum(sz * px for sz, px in later) / sum(sz for sz, px in later)
            entry = vw                      # we pay the later price, not theirs
            fee = FEE * entry * (1 - entry)
            payout = 1.0 if o == winner else 0.0
            ret = payout - entry - fee      # per share
            (took if (vw - p) <= cap else declined).append(ret)
        if k % 300 == 0 and k:
            print(f"  {k}/{len(cids)} markets, {n_seen:,} entries", flush=True)

    def show(xs, label):
        if len(xs) < 2:
            print(f"  {label}: too few")
            return None
        m = statistics.mean(xs)
        se = statistics.stdev(xs) / math.sqrt(len(xs))
        print(f"  {label:34} {len(xs):8,} entries  "
              f"{100*m:+7.3f}c/share  t={m/se:+6.2f}")
        return (m, se, len(xs))

    print(f"\nRULE: take when the tape {a.lag_lo}-{a.lag_hi}s later is within "
          f"{a.cap_cents:.0f}c above the original fill")
    A = show(took, "ACCEPTED by the rule")
    B = show(declined, "DECLINED by the rule")
    if A and B:
        diff = A[0] - B[0]
        se = math.sqrt(A[1] ** 2 + B[1] ** 2)
        t = diff / se if se else 0
        print(f"\n  accepted minus declined: {100*diff:+.3f}c/share, t={t:+.2f}")
        print("  " + ("the rule separates good entries from bad" if t > 3
                      else "NOT a real separation"))
        print(f"  accepted alone is {'PROFITABLE' if A[0] > 0 else 'unprofitable'} "
              f"after fees ({100*A[0]:+.3f}c/share, t={A[0]/A[1]:+.2f})")


main()
