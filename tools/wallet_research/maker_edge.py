"""What did real resting orders actually earn, fee-free?

Every fill is labelled maker or taker by ``maker_label.py``. Held to resolution,
one fill's contribution is exact and additive:

    BUY  at p:  size * (payout_per_share - p)
    SELL at p:  size * (p - payout_per_share)

Summed over a wallet's fills in a market this equals its realised PnL, so no
position tracking is needed and nothing is double counted. Takers pay
``size * 0.07 * p * (1-p)``; makers pay nothing.

Significance is clustered by MARKET, not by fill. Every fill in one market shares
a single resolution, so the independent unit is the market and a per-fill t-stat
would count one coin flip thousands of times.

Self-check: maker and taker are opposite sides of the same matches, so their
gross dollars must cancel. The run prints that residual; a large one means the
labels or the tape are wrong and the rest of the output should not be believed.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import time
from collections import Counter, defaultdict

FEE = 0.07


def tstat(xs: list[float]) -> tuple[float, float]:
    """Mean and t of a sample, (0, 0) when it is too small to say anything."""
    if len(xs) < 3:
        return 0.0, 0.0
    m = statistics.mean(xs)
    sd = statistics.stdev(xs)
    if sd == 0:
        return m, 0.0
    return m, m / (sd / math.sqrt(len(xs)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/wallet_research/wallets.db")
    ap.add_argument("--labels", default="data/wallet_research/maker.db")
    ap.add_argument("--family", default="", help="blank = all labelled")
    ap.add_argument("--min-price", type=float, default=0.0)
    ap.add_argument("--max-price", type=float, default=1.0)
    ap.add_argument("--min-shares", type=float, default=5000,
                    help="per-wallet maker shares needed to be ranked")
    ap.add_argument("--min-markets", type=int, default=10)
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()

    src = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    lab = sqlite3.connect(f"file:{a.labels}?mode=ro", uri=True)
    meta = {c: (f, e, w) for c, f, e, w in src.execute(
        "SELECT cid,family,end_ts,winner FROM markets WHERE fetched=1")}
    cids = [c for (c,) in lab.execute("SELECT cid FROM done")
            if c in meta and meta[c][2] is not None
            and (not a.family or meta[c][0] == a.family)]
    print(f"{len(cids)} labelled markets"
          f"{' (' + a.family + ')' if a.family else ''}, "
          f"prices {a.min_price:g}-{a.max_price:g}")

    mk_shares = tk_shares = 0.0
    mk_gross = tk_gross = tk_fees = 0.0
    per_market_mk: list[float] = []     # one per-share return per market
    per_market_tk: list[float] = []
    orphan_taker = 0

    w_mk: dict[str, list] = defaultdict(lambda: [0.0, 0.0, 0, set()])
    w_month: dict[str, dict[str, list]] = defaultdict(
        lambda: defaultdict(lambda: [0.0, 0.0]))

    for k, cid in enumerate(cids):
        _fam, ets, winner = meta[cid]
        month = time.strftime("%Y-%m", time.gmtime(ets))
        keys = Counter((t, w, s, p) for t, w, s, p in lab.execute(
            "SELECT txh,wallet,size_r,price_r FROM tk WHERE cid=?", (cid,)))
        m_sh = m_gr = t_sh = t_gr = t_fe = 0.0

        for txh, wallet, side, oidx, size, price in src.execute(
                "SELECT txh,wallet,side,oidx,size,price FROM trades WHERE cid=?",
                (cid,)):
            if not (a.min_price <= price <= a.max_price):
                continue
            w = (wallet or "").lower()
            key = (txh, w, round(size, 4), round(price, 4))
            if keys.get(key, 0) > 0:
                keys[key] -= 1
                is_taker = True
            else:
                is_taker = False
            payout = 1.0 if oidx == winner else 0.0
            ps = (payout - price) if side == "BUY" else (price - payout)
            if is_taker:
                fee = FEE * price * (1 - price)
                t_sh += size
                t_gr += size * ps
                t_fe += size * fee
            else:
                m_sh += size
                m_gr += size * ps
                w_mk[w][0] += size
                w_mk[w][1] += size * ps
                w_mk[w][2] += 1
                w_mk[w][3].add(cid)
                mm = w_month[w][month]
                mm[0] += size
                mm[1] += size * ps

        orphan_taker += sum(keys.values())
        mk_shares += m_sh
        mk_gross += m_gr
        tk_shares += t_sh
        tk_gross += t_gr
        tk_fees += t_fe
        if m_sh > 0:
            per_market_mk.append(m_gr / m_sh)
        if t_sh > 0:
            per_market_tk.append((t_gr - t_fe) / t_sh)
        if k % 200 == 0 and k:
            print(f"  {k}/{len(cids)}", flush=True)

    resid = mk_gross + tk_gross
    print(f"\nshares  maker {mk_shares:,.0f}   taker {tk_shares:,.0f}")
    print(f"zero-sum residual {resid:+,.0f} on {mk_gross:+,.0f} maker gross "
          f"({100*abs(resid)/max(abs(mk_gross), 1):.1f}% of it)")
    if orphan_taker:
        print(f"{orphan_taker:,} taker records matched no stored fill")

    mm, mt = tstat(per_market_mk)
    tm, tt = tstat(per_market_tk)
    print("\nheld to resolution, per share of that side's own volume:")
    print(f"  MAKER, no fee            {100*mk_gross/mk_shares:+7.3f}c/share"
          f"   per-market mean {100*mm:+7.3f}c  t={mt:+5.2f}  "
          f"({len(per_market_mk)} markets)")
    print(f"  TAKER, before fee        {100*tk_gross/tk_shares:+7.3f}c/share")
    print(f"  TAKER, after fee         {100*(tk_gross-tk_fees)/tk_shares:+7.3f}"
          f"c/share   per-market mean {100*tm:+7.3f}c  t={tt:+5.2f}")
    print(f"  fee to the house         {100*tk_fees/tk_shares:7.3f}c/taker share"
          f"   (${tk_fees:,.0f})")
    print(f"\n  resting instead of crossing is worth "
          f"{100*(mk_gross/mk_shares - (tk_gross-tk_fees)/tk_shares):+.3f}c/share")

    ranked = [(w, sh, p, n, cs) for w, (sh, p, n, cs) in w_mk.items()
              if sh >= a.min_shares and len(cs) >= a.min_markets]
    ranked.sort(key=lambda r: r[2] / r[1], reverse=True)
    print(f"\n{len(ranked)} wallets with >= {a.min_shares:,.0f} maker shares "
          f"across >= {a.min_markets} markets")
    print(f"  {'wallet':<44}{'c/share':>9}{'shares':>11}{'pnl $':>10}"
          f"{'mkts':>6}  months (c/share)")
    for w, sh, p, _n, cs in ranked[:a.top]:
        ms = " ".join(
            f"{mo[-2:]}:{100*v[1]/v[0]:+.1f}"
            for mo, v in sorted(w_month[w].items()) if v[0] > 0)
        print(f"  {w:<44}{100*p/sh:+9.2f}{sh:>11,.0f}{p:>10,.0f}{len(cs):>6}  {ms}")

    pos = sum(1 for _w, _sh, p, _n, _c in ranked if p > 0)
    print(f"\n{pos}/{len(ranked)} ranked wallets positive as makers "
          f"({100*pos/max(len(ranked),1):.0f}%)")


main()
