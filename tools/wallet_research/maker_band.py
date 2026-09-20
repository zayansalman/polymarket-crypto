"""Is the maker edge on favourites real, and what shape is it?

Every maker fill is normalised to a BUY: selling outcome o at p is buying the
other outcome at 1-p, which pays identically, so one calibration curve covers
the whole tape.

Then the only question worth asking: at a fill price of p, how often did that
side actually win? Price IS the market's forecast, so realised-minus-implied is
the edge, in probability, before any trading assumption. If favourites win more
often than their price says, resting on them pays; if not, the band result was
a fluke of two months.

Three fragility checks, because a positive mean is not the same as a tradeable
one. Significance is clustered by market -- fills in one market share a single
resolution. Months are shown separately, not pooled. And the share of profit
coming from the best few markets is printed, because an edge carried by rare
large winners is not an edge you can size.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import time
from collections import Counter, defaultdict

BUCKETS = [(0.0, 0.05), (0.05, 0.15), (0.15, 0.25), (0.25, 0.35), (0.35, 0.45),
           (0.45, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 0.85),
           (0.85, 0.92), (0.92, 0.97), (0.97, 1.01)]


def clustered_t(per_market: list[tuple[float, float]]) -> tuple[float, float, int]:
    """Mean per-share edge and its t, one observation per market.

    Shares are NOT independent observations: every share bought in a market
    settles on that market's single outcome, so a share-count interval claims
    thousands of trials where there was one. The market is the trial.
    """
    rets = [p / s for s, p in per_market if s > 0]
    if len(rets) < 3:
        return 0.0, 0.0, len(rets)
    m = statistics.mean(rets)
    sd = statistics.stdev(rets)
    return m, (m / (sd / math.sqrt(len(rets))) if sd else 0.0), len(rets)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/wallet_research/wallets.db")
    ap.add_argument("--labels", default="data/wallet_research/maker.db")
    ap.add_argument("--lo", type=float, default=0.55)
    ap.add_argument("--hi", type=float, default=0.95)
    ap.add_argument("--family", default="")
    a = ap.parse_args()

    src = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    lab = sqlite3.connect(f"file:{a.labels}?mode=ro", uri=True)
    meta = {c: (f, e, w) for c, f, e, w in src.execute(
        "SELECT cid,family,end_ts,winner FROM markets WHERE fetched=1")}
    cids = [c for (c,) in lab.execute("SELECT cid FROM done")
            if c in meta and meta[c][2] is not None
            and (not a.family or meta[c][0] == a.family)]

    # bucket -> [shares, won_shares, implied_shares, pnl]
    cal: dict[tuple, list] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    cal_mkt: dict[tuple, list] = defaultdict(list)   # bucket -> [(shares, pnl)]
    month: dict[str, list] = defaultdict(lambda: [0.0, 0.0])
    month_rets: dict[str, list[float]] = defaultdict(list)
    per_market: list[tuple[str, float, float]] = []   # cid, shares, pnl (in band)

    for cid in cids:
        _fam, ets, winner = meta[cid]
        mo = time.strftime("%Y-%m", time.gmtime(ets))
        keys = Counter((t, w, s, p) for t, w, s, p in lab.execute(
            "SELECT txh,wallet,size_r,price_r FROM tk WHERE cid=?", (cid,)))
        m_sh = m_pnl = 0.0
        here: dict[tuple, list] = defaultdict(lambda: [0.0, 0.0])
        for txh, wallet, side, oidx, size, price in src.execute(
                "SELECT txh,wallet,side,oidx,size,price FROM trades WHERE cid=?",
                (cid,)):
            k = (txh, (wallet or "").lower(), round(size, 4), round(price, 4))
            if keys.get(k, 0) > 0:
                keys[k] -= 1
                continue                      # taker fill
            # Normalise to a buy.
            if side == "BUY":
                buy_o, buy_p = oidx, price
            else:
                buy_o, buy_p = 1 - oidx, 1.0 - price
            won = 1.0 if buy_o == winner else 0.0
            for lo, hi in BUCKETS:
                if lo <= buy_p < hi:
                    c = cal[(lo, hi)]
                    c[0] += size
                    c[1] += size * won
                    c[2] += size * buy_p
                    c[3] += size * (won - buy_p)
                    h = here[(lo, hi)]
                    h[0] += size
                    h[1] += size * (won - buy_p)
                    break
            if a.lo <= buy_p < a.hi:
                m_sh += size
                m_pnl += size * (won - buy_p)
                month[mo][0] += size
                month[mo][1] += size * (won - buy_p)
        for b, h in here.items():
            cal_mkt[b].append((h[0], h[1]))
        if m_sh > 0:
            per_market.append((cid, m_sh, m_pnl))
            month_rets[mo].append(m_pnl / m_sh)

    print(f"{len(cids)} markets, maker fills normalised to buys\n")
    print("CALIBRATION — does the fill price forecast the outcome?")
    print(f"  {'price band':<14}{'shares':>12}{'implied':>9}{'realised':>10}"
          f"{'edge c/share':>14}{'per-mkt':>10}{'t':>8}{'mkts':>7}")
    for (lo, hi), v in sorted(cal.items()):
        if v[0] < 1000:
            continue
        imp, real = v[2] / v[0], v[1] / v[0]
        m, t, n = clustered_t(cal_mkt[(lo, hi)])
        flag = "  <-" if abs(t) >= 2 else ""
        print(f"  {lo:.2f}-{hi:.2f}     {v[0]:>12,.0f}{imp:>9.3f}{real:>10.3f}"
              f"{100*v[3]/v[0]:>+14.3f}{100*m:>+10.3f}{t:>+8.2f}{n:>7}{flag}")
    print("  edge = volume weighted; per-mkt = equal weighted across markets, "
          "which is the independent unit")
    print("  '<-' = clustered |t| >= 2")

    sh = sum(s for _c, s, _p in per_market)
    pnl = sum(p for _c, _s, p in per_market)
    if not sh:
        print("\nno fills in band")
        return
    rets = [p / s for _c, s, p in per_market]
    m = statistics.mean(rets)
    t = m / (statistics.stdev(rets) / math.sqrt(len(rets))) if len(rets) > 2 else 0
    print(f"\nBAND {a.lo:.2f}-{a.hi:.2f}   {sh:,.0f} shares   ${pnl:,.0f}")
    print(f"  volume weighted   {100*pnl/sh:+.3f}c/share")
    print(f"  per market mean   {100*m:+.3f}c/share  t={t:+.2f}  "
          f"({len(rets)} markets, {100*sum(1 for r in rets if r>0)/len(rets):.0f}% positive)")

    print("\n  by month — the two weightings answer different questions:")
    print("    volume weighted is what the whole maker population earned, so a")
    print("    single enormous position can carry the month. Equal weighted is")
    print("    what a fixed clip in every market would have earned, which is us.")
    print(f"    {'month':<9}{'vol wtd':>10}{'equal wtd':>11}{'t':>7}"
          f"{'mkts':>7}{'% pos':>7}{'shares':>14}")
    for mo, v in sorted(month.items()):
        r = month_rets[mo]
        em, et, en = clustered_t([(1.0, x) for x in r])
        pos = 100 * sum(1 for x in r if x > 0) / max(len(r), 1)
        print(f"    {mo:<9}{100*v[1]/v[0]:>+10.3f}{100*em:>+11.3f}{et:>+7.2f}"
              f"{en:>7}{pos:>6.0f}%{v[0]:>14,.0f}")

    caps = [c for c in (250, 1000, 5000) if c]
    print("\n  with a per-market size cap (what we could actually rest):")
    for cap in caps:
        csh = sum(min(s, cap) for _c, s, _p in per_market)
        cpnl = sum((p / s) * min(s, cap) for _c, s, p in per_market if s > 0)
        print(f"    cap {cap:>5,}sh/market   {100*cpnl/csh:+7.3f}c/share   "
              f"{csh:>10,.0f} shares   ${cpnl:>9,.0f}")

    print("\n  concentration — is this carried by a few markets?")
    print("    (dollars are dominated by whichever market happened to be huge,")
    print("     so the same question is asked of the per-share return too)")
    ordered = sorted(per_market, key=lambda r: r[2], reverse=True)
    for frac in (0.01, 0.05, 0.10):
        n = max(1, int(len(ordered) * frac))
        top = sum(r[2] for r in ordered[:n])
        print(f"    $  best {frac:>4.0%} ({n:>4} markets) hold "
              f"{100*top/pnl:>7.1f}% of the profit")
    worst = sum(r[2] for r in ordered[-max(1, len(ordered) // 100):])
    print(f"    $  worst 1% of markets cost ${worst:,.0f}")

    eq = sorted(rets, reverse=True)
    tot = sum(eq)
    for frac in (0.01, 0.05, 0.10):
        n = max(1, int(len(eq) * frac))
        print(f"    c/ best {frac:>4.0%} ({n:>4} markets) hold "
              f"{100*sum(eq[:n])/tot:>7.1f}% of the mean")
    print(f"    c/ median market  {100*statistics.median(eq):+.3f}c/share")
    trimmed = eq[max(1, len(eq)//20):-max(1, len(eq)//20)]
    print(f"    c/ trimmed 5% each tail  {100*statistics.mean(trimmed):+.3f}"
          f"c/share  <- survives without the tails?")


main()
