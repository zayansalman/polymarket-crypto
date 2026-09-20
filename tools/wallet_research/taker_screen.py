"""Find wallets that made money on crypto Up/Down as TAKERS, recently.

Ranking by profit finds market makers: their edge is the spread, they are on the
other side of the trade from a copier, and copying them converts their profit
into your cost. This screen inverts that. It classifies every fill as maker or
taker FIRST, discards the maker flow, and ranks on what is left — the part a
copier who crosses the spread can actually reproduce.

Classification is exact, not inferred. ``/trades?takerOnly=true`` returns only
taker-side records; a fill present in the full feed but absent there was passive.

Pass 1 is local and cheap: one-sided, still active, profitable after the taker
fee. Pass 2 spends API calls only on those survivors.
"""
from __future__ import annotations
import argparse
import asyncio
import csv
import math
import sqlite3
import statistics
import time
from pathlib import Path
import httpx

DATA = "https://data-api.polymarket.com"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
      "Accept": "application/json"}
FEE = 0.07
NOW = int(time.time())


def norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def candidates(con, *, days, families, min_markets, max_two_sided, min_hold,
               active_days, max_stake):
    since = NOW - days * 86400
    mk = {cid: (fam, ets, win) for cid, fam, ets, win in con.execute(
        "SELECT cid,family,end_ts,winner FROM markets WHERE fetched=1 AND end_ts>=?",
        (since,)) if fam in families}
    out: list[dict] = []
    cw = None
    per: dict = {}

    def flush(w, per):
        if len(per) < min_markets:
            return
        pnls, sh, cost, fee, two, held, acq, last = [], 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0
        for cid, d in per.items():
            net = [d["b"][i] - d["s"][i] for i in (0, 1)]
            sp = max(0.0, -min(net))
            eff = [net[i] + sp for i in (0, 1)]
            bought = d["b"][0] + d["b"][1] + 2 * sp
            if bought <= 0:
                continue
            _fam, _ets, win = mk[cid]
            pnls.append(d["proc"] - d["cost"] - sp + eff[win] - d["tfee"])
            sh += bought
            cost += d["cost"] + sp
            fee += d["tfee"]
            acq += bought
            held += min(eff[0] + eff[1], bought)
            last = max(last, d["last"])
            lo, hi = min(d["b"]), max(d["b"])
            if hi > 0 and lo > 0.05 * hi:
                two += 1
        n = len(pnls)
        if n < min_markets or sh <= 0 or cost <= 0:
            return
        if two / n > max_two_sided:          # quoting, not taking
            return
        if held / acq < min_hold:            # not holding to resolution
            return
        if last < NOW - active_days * 86400:  # gone
            return
        if cost / n > max_stake:             # not a "normal" wallet
            return
        total = sum(pnls)
        if total <= 0:
            return
        sd = statistics.stdev(pnls) if n > 1 else 0.0
        t = (total / n) / (sd / math.sqrt(n)) if sd > 0 else 0.0
        out.append({"wallet": w, "mkts": n, "pnl": total, "cost": cost,
                    "shares": sh, "fee": fee, "edge_c": 100 * total / sh,
                    "t": t, "stake": cost / n, "two": two / n,
                    "entry": cost / sh, "last": last})

    for w, cid, oidx, b, s, cost, proc, tfee, last_ts in con.execute(
            "SELECT wallet,cid,oidx,b,s,cost,proc,tfee,last_ts FROM wm ORDER BY wallet"):
        if w != cw:
            if cw is not None:
                flush(cw, per)
            cw = w
            per = {}
        if cid not in mk:
            continue
        d = per.setdefault(cid, {"b": [0.0, 0.0], "s": [0.0, 0.0], "cost": 0.0,
                                 "proc": 0.0, "tfee": 0.0, "last": 0})
        i = 1 if oidx else 0
        d["b"][i] += b
        d["s"][i] += s
        d["cost"] += cost
        d["proc"] += proc
        d["tfee"] += tfee
        d["last"] = max(d["last"], last_ts)
    if cw is not None:
        flush(cw, per)
    return out, mk


async def classify(con, cl, wallet, mk, sample, sem):
    """Split one wallet's fills into maker and taker, and score the taker half."""
    cids = [r[0] for r in con.execute(
        "SELECT DISTINCT cid FROM trades WHERE wallet=? ORDER BY RANDOM() LIMIT ?",
        (wallet, sample))]
    cids = [c for c in cids if c in mk]
    tk_notional = mk_notional = 0.0
    tk_pnl = tk_shares = tk_fee = 0.0
    seen = 0

    async def one(cid):
        nonlocal tk_notional, mk_notional, tk_pnl, tk_shares, tk_fee, seen
        async with sem:
            try:
                r = await cl.get(f"{DATA}/trades", params={
                    "market": cid, "limit": 500, "takerOnly": "true"}, timeout=30.0)
                r.raise_for_status()
                feed = r.json()
            except Exception:
                return
        taker_keys = {
            (t.get("transactionHash"), round(float(t["size"]), 4),
             round(float(t["price"]), 4))
            for t in feed if t["proxyWallet"].lower() == wallet.lower()
        }
        _fam, _ets, winner = mk[cid]
        for txh, size, price, oidx, side in con.execute(
                "SELECT txh,size,price,oidx,side FROM trades WHERE cid=? AND wallet=?",
                (cid, wallet)):
            key = (txh, round(size, 4), round(price, 4))
            is_taker = key in taker_keys
            notional = size * price
            if is_taker:
                tk_notional += notional
            else:
                mk_notional += notional
            if side == "BUY" and is_taker:
                fee = size * FEE * price * (1 - price)
                payout = size if oidx == winner else 0.0
                tk_pnl += payout - notional - fee
                tk_shares += size
                tk_fee += fee
        seen += 1

    await asyncio.gather(*(one(c) for c in cids))
    tot = tk_notional + mk_notional
    return {
        "taker_share": (tk_notional / tot) if tot else 0.0,
        "taker_pnl": tk_pnl, "taker_shares": tk_shares, "taker_fee": tk_fee,
        "taker_edge_c": (100 * tk_pnl / tk_shares) if tk_shares else 0.0,
        "markets_checked": seen,
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/wallet_research/wallets.db")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--families", default="1h")
    ap.add_argument("--min-markets", type=int, default=15)
    ap.add_argument("--max-two-sided", type=float, default=0.25)
    ap.add_argument("--min-hold", type=float, default=0.7)
    ap.add_argument("--active-days", type=int, default=7)
    ap.add_argument("--max-stake", type=float, default=2000.0)
    ap.add_argument("--min-taker-share", type=float, default=0.6)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--conc", type=int, default=8)
    ap.add_argument("--top", type=int, default=40)
    a = ap.parse_args()

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    fams = set(a.families.split(","))
    cands, mk = candidates(
        con, days=a.days, families=fams, min_markets=a.min_markets,
        max_two_sided=a.max_two_sided, min_hold=a.min_hold,
        active_days=a.active_days, max_stake=a.max_stake)
    print(f"pass 1 — one-sided, holds to resolution, active <{a.active_days}d, "
          f"profitable after fee, <=${a.max_stake:,.0f}/market, last {a.days}d: "
          f"{len(cands)} wallets")
    if not cands:
        return
    cands.sort(key=lambda r: -r["edge_c"])
    cands = cands[: a.top * 4]
    print(f"pass 2 — classifying maker vs taker for the top {len(cands)} "
          f"(sampling {a.sample} markets each)")

    sem = asyncio.Semaphore(a.conc)
    async with httpx.AsyncClient(headers=UA) as cl:
        results = await asyncio.gather(
            *(classify(con, cl, c["wallet"], mk, a.sample, sem) for c in cands))
    for c, r in zip(cands, results):
        c.update(r)

    keep = [c for c in cands
            if c["taker_share"] >= a.min_taker_share and c["taker_pnl"] > 0]
    print(f"        {len(keep)} are >={a.min_taker_share:.0%} taker by notional "
          f"AND profitable on their taker fills\n")
    keep.sort(key=lambda r: -r["taker_edge_c"])
    hdr = (f"{'wallet':44}{'taker%':>8}{'takerEdge¢':>12}{'takerPnL$':>11}"
           f"{'allPnL$':>10}{'mkts':>6}{'t':>6}{'stake$':>9}{'entry':>7}{'lastSeen':>10}")
    print(hdr)
    print("-" * len(hdr))
    for c in keep[: a.top]:
        print(f"{c['wallet']:44}{100*c['taker_share']:8.0f}{c['taker_edge_c']:12.2f}"
              f"{c['taker_pnl']:11.0f}{c['pnl']:10.0f}{c['mkts']:6d}{c['t']:6.2f}"
              f"{c['stake']:9.0f}{c['entry']:7.3f}"
              f"{(NOW-c['last'])/86400:9.1f}d")
    out = Path(a.db).parent / "taker_ranked.csv"
    with open(out, "w", newline="") as fh:
        wtr = csv.DictWriter(fh, fieldnames=list(keep[0].keys()) if keep
                             else list(cands[0].keys()))
        wtr.writeheader()
        wtr.writerows(keep or cands)
    print("\nwrote", out)

asyncio.run(main())
