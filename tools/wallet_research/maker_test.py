"""Could a resting bid have filled, and would it have paid?

The taker side loses: a real +0.21c/share gross edge against a 1.127c fee.
Makers pay no fee, so the maker side is the only configuration where the
arithmetic can clear. Its unknown is FILL RATE, because only filled orders are
recorded and every order that rested unfilled is invisible.

That unknown can be BOUNDED rather than guessed. For a bid resting at price P on
some outcome, any trade in the window at a price <= P is volume that crossed
down to our level — an upper bound on what we could have taken, since we cannot
see queue position ahead of us. Reporting it as a bound, not a fill, is the
whole point: assuming we take all of it is exactly the optimism this project
exists to avoid.

Scored honestly: a bid that fills is held to resolution and pays NO fee.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/wallet_research/wallets.db")
    ap.add_argument("--family", default="1h")
    ap.add_argument("--offset-cents", type=float, default=5.0,
                    help="how far under the prevailing price to rest")
    ap.add_argument("--markets", type=int, default=1200)
    ap.add_argument("--min-price", type=float, default=0.15)
    ap.add_argument("--max-price", type=float, default=0.85)
    a = ap.parse_args()

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    cids = [r[0] for r in con.execute(
        "SELECT cid FROM markets WHERE fetched=1 AND family=? "
        "ORDER BY END_TS DESC LIMIT ?".replace("END_TS", "end_ts"),
        (a.family, a.markets))]
    off = a.offset_cents / 100.0
    print(f"{len(cids)} {a.family} markets, resting {a.offset_cents:.0f}c under "
          f"the prevailing price, entries between {a.min_price} and {a.max_price}")

    quoted = filled = 0
    rets: list[float] = []
    taker_rets: list[float] = []

    for k, cid in enumerate(cids):
        winner = con.execute(
            "SELECT winner FROM markets WHERE cid=?", (cid,)).fetchone()[0]
        rows = con.execute(
            "SELECT oidx, ts, size, price FROM trades "
            "WHERE cid=? AND side='BUY' ORDER BY ts", (cid,)).fetchall()
        if len(rows) < 10:
            continue
        by_out: dict[int, list[tuple[int, float, float]]] = {}
        for o, t, s, p in rows:
            by_out.setdefault(o, []).append((t, s, p))

        for o, seq in by_out.items():
            if len(seq) < 6:
                continue
            # Quote off an early reference print, then watch the rest of the tape.
            ref_t, _ref_s, ref_p = seq[len(seq) // 4]
            if not (a.min_price <= ref_p <= a.max_price):
                continue
            bid = round(ref_p - off, 3)
            if bid <= 0.01:
                continue
            quoted += 1
            # Volume that crossed down to our level afterwards. Upper bound on a
            # fill: we cannot see the queue ahead of us.
            down = [(s, p) for t, s, p in seq if t > ref_t and p <= bid]
            if not down:
                continue
            filled += 1
            payout = 1.0 if o == winner else 0.0
            rets.append(payout - bid)                       # maker: no fee
            fee = 0.07 * ref_p * (1 - ref_p)
            taker_rets.append(payout - ref_p - fee)         # crossing instead
        if k % 300 == 0 and k:
            print(f"  {k}/{len(cids)}", flush=True)

    print(f"\nquotes placed {quoted:,}   touched by the tape {filled:,} "
          f"({100*filled/max(quoted,1):.1f}%)  <- UPPER BOUND on fill rate")
    if len(rets) > 2:
        m = statistics.mean(rets)
        se = statistics.stdev(rets) / math.sqrt(len(rets))
        print(f"  resting fill, no fee, held to resolution: "
              f"{100*m:+7.3f}c/share  t={m/se:+6.2f}")
        mt = statistics.mean(taker_rets)
        st = statistics.stdev(taker_rets) / math.sqrt(len(taker_rets))
        print(f"  crossing instead, same markets, with fee: "
              f"{100*mt:+7.3f}c/share  t={mt/st:+6.2f}")
        print(f"\n  resting is {100*(m-mt):+.3f}c/share better than crossing")
        print("  " + ("RESTING CLEARS THE FEE" if m > 0 else
                      "resting still loses, even fee-free"))


main()
