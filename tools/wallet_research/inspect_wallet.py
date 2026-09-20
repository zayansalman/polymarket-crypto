"""Look hard at one wallet before copying it.

A three-day total can be one good afternoon. These are the checks that decide
whether a number is a strategy or a streak:

  - **Day by day.** Three days up beats one day up thirty and two down.
  - **Maker or taker, per day.** A copier can only reproduce the taker part.
  - **Does it hold to resolution?** If it sells before the market settles, its
    result depends on exit timing a follower 20-60s behind cannot reproduce.
  - **Entry price distribution.** The favourite/underdog split changes what the
    trade even is, and the fee is worst at 0.50.
  - **Concentration.** A total carried by two markets is not a strategy.
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
    ap.add_argument("wallet")
    ap.add_argument("--db", default="data/wallet_research/m15.db")
    ap.add_argument("--labels", default="data/wallet_research/maker15.db")
    ap.add_argument("--days", type=float, default=3.0)
    a = ap.parse_args()

    w = a.wallet.lower()
    now = int(time.time())
    since = now - int(a.days * 86400)
    src = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    lab = sqlite3.connect(f"file:{a.labels}?mode=ro", uri=True)

    meta = {c: (e, win) for c, e, win in src.execute(
        "SELECT cid,end_ts,winner FROM markets WHERE fetched=1 AND end_ts>=? "
        "AND winner IS NOT NULL", (since,))}
    labelled = {c for (c,) in lab.execute("SELECT cid FROM done")}

    # Only this wallet's markets, found via the wallet index rather than a scan.
    cids = {c for (c,) in src.execute(
        "SELECT DISTINCT cid FROM trades WHERE wallet=?", (w,))} & set(meta) & labelled
    print(f"{PROFILE}{w}")
    print(f"{len(cids)} resolved+labelled 15m markets in the last {a.days:g} days\n")

    day: dict[str, list] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0])
    per_mkt: list[tuple[str, float]] = []
    px_buckets: dict[str, list] = defaultdict(lambda: [0.0, 0.0])
    sells = buys = 0
    sell_sh = buy_sh = 0.0

    for cid in cids:
        end_ts, winner = meta[cid]
        d = time.strftime("%Y-%m-%d", time.gmtime(end_ts))
        keys = Counter((t, ww, s, p) for t, ww, s, p in lab.execute(
            "SELECT txh,wallet,size_r,price_r FROM tk WHERE cid=?", (cid,)))
        mk_pnl = 0.0
        for txh, side, oidx, size, price in src.execute(
                "SELECT txh,side,oidx,size,price FROM trades "
                "WHERE cid=? AND wallet=?", (cid, w)):
            taker = keys.get((txh, w, round(size, 4), round(price, 4)), 0) > 0
            payout = 1.0 if oidx == winner else 0.0
            ps = (payout - price) if side == "BUY" else (price - payout)
            if side == "BUY":
                buys += 1
                buy_sh += size
            else:
                sells += 1
                sell_sh += size
            rec = day[d]
            rec[4] += 1
            if taker:
                fee = FEE * price * (1 - price)
                rec[0] += size * (ps - fee)
                rec[1] += size
                mk_pnl += size * (ps - fee)
            else:
                rec[2] += size * ps
                rec[3] += size
                mk_pnl += size * ps
            bp = price if side == "BUY" else 1.0 - price
            b = ("<0.35" if bp < 0.35 else "0.35-0.50" if bp < 0.50
                 else "0.50-0.65" if bp < 0.65 else "0.65-0.80" if bp < 0.80
                 else "0.80+")
            px_buckets[b][0] += size
            px_buckets[b][1] += size * ps
        per_mkt.append((cid, mk_pnl))

    print(f"{'day':<12}{'taker $':>10}{'tk sh':>9}{'tk c/sh':>9}"
          f"{'maker $':>10}{'mk sh':>9}{'fills':>7}")
    for d, v in sorted(day.items()):
        tk_cps = (100 * v[0] / v[1]) if v[1] else 0.0
        print(f"{d:<12}{v[0]:>10,.0f}{v[1]:>9,.0f}{tk_cps:>+9.2f}"
              f"{v[2]:>10,.0f}{v[3]:>9,.0f}{v[4]:>7}")
    tk_tot = sum(v[0] for v in day.values())
    tk_sh = sum(v[1] for v in day.values())
    mk_tot = sum(v[2] for v in day.values())
    up = sum(1 for v in day.values() if v[0] + v[2] > 0)
    print(f"{'TOTAL':<12}{tk_tot:>10,.0f}{tk_sh:>9,.0f}"
          f"{(100*tk_tot/tk_sh if tk_sh else 0):>+9.2f}{mk_tot:>10,.0f}")
    print(f"  up on {up} of {len(day)} days")

    print(f"\nholds to resolution?  {buys} buys / {sells} sells "
          f"({buy_sh:,.0f}sh vs {sell_sh:,.0f}sh sold back)")
    print("  a wallet that sells before settlement is timing exits, which a "
          "follower minutes behind cannot copy")

    print(f"\n{'entry price':<12}{'shares':>10}{'gross c/sh':>12}")
    for b in ("<0.35", "0.35-0.50", "0.50-0.65", "0.65-0.80", "0.80+"):
        v = px_buckets.get(b)
        if v and v[0]:
            print(f"{b:<12}{v[0]:>10,.0f}{100*v[1]/v[0]:>+12.2f}")

    per_mkt.sort(key=lambda r: r[1], reverse=True)
    tot = sum(p for _c, p in per_mkt)
    if per_mkt and tot:
        n = max(1, len(per_mkt) // 10)
        print(f"\nconcentration: best {n} of {len(per_mkt)} markets hold "
              f"{100*sum(p for _c, p in per_mkt[:n])/tot:.0f}% of the total")
        pos = sum(1 for _c, p in per_mkt if p > 0)
        print(f"  {pos}/{len(per_mkt)} markets positive "
              f"({100*pos/len(per_mkt):.0f}%)")


main()
