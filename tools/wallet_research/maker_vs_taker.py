"""Measure adverse selection: do MAKER fills realise worse than TAKER fills?

The trade-off a follower faces is a fee against adverse selection. Crossing costs
`shares * 0.07 * p * (1-p)` - 1.75c/share at 50c. Resting costs nothing but only
fills when someone wants the other side, which is disproportionately when they
know something.

`/trades?takerOnly=true` returns only taker-side records; `takerOnly=false`
returns every fill. A fill present in the full feed but absent from the taker
feed was passive. Classifying both and comparing realised outcome against price
paid gives adverse selection in cents per share, directly comparable to the fee.
"""
from __future__ import annotations
import argparse, asyncio, sqlite3, sys
from pathlib import Path
import httpx

DB = Path(__file__).resolve().parents[2] / "data" / "wallet_research" / "wallets.db"
DATA = "https://data-api.polymarket.com"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
      "Accept": "application/json"}
FEE_RATE = 0.07


async def taker_fills(cl, cid):
    out, off = [], 0
    while True:
        for attempt in range(4):
            try:
                r = await cl.get(f"{DATA}/trades", params={
                    "market": cid, "limit": 500, "offset": off,
                    "takerOnly": "true"}, timeout=40.0)
                if r.status_code == 429:
                    await asyncio.sleep(2 + 3 * attempt); continue
                r.raise_for_status()
                page = r.json(); break
            except Exception:
                if attempt == 3:
                    return out
                await asyncio.sleep(1.5 * (attempt + 1))
        if not page:
            break
        out.extend(page)
        if len(page) < 500:
            break
        off += 500
    return out


def key(txh, size, price):
    return (txh, round(float(size), 4), round(float(price), 4))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", type=int, default=400)
    ap.add_argument("--family", default="1h")
    ap.add_argument("--conc", type=int, default=6)
    a = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cids = [r[0] for r in con.execute(
        "SELECT cid FROM markets WHERE fetched=1 AND family=? ORDER BY RANDOM() LIMIT ?",
        (a.family, a.markets))]
    print(f"sampling {len(cids)} {a.family} markets", flush=True)

    # price bucket -> [shares, cost, won, fee] for maker and taker separately
    agg = {"maker": {}, "taker": {}}
    sem = asyncio.Semaphore(a.conc)
    done = 0

    async def one(cl, cid):
        nonlocal done
        async with sem:
            feed = await taker_fills(cl, cid)
        tk = {key(t.get("transactionHash"), t["size"], t["price"])
              for t in feed if t.get("side") == "BUY"}
        winner = con.execute("SELECT winner FROM markets WHERE cid=?", (cid,)).fetchone()[0]
        for txh, size, price, oidx in con.execute(
                "SELECT txh,size,price,oidx FROM trades WHERE cid=? AND side='BUY'", (cid,)):
            side = "taker" if key(txh, size, price) in tk else "maker"
            b = int(price * 20)
            row = agg[side].setdefault(b, [0.0, 0.0, 0.0, 0.0])
            row[0] += size
            row[1] += size * price
            row[2] += size if oidx == winner else 0.0
            row[3] += size * FEE_RATE * price * (1 - price)
        done += 1
        if done % 50 == 0:
            print(f"  {done}/{len(cids)}", flush=True)

    async with httpx.AsyncClient(headers=UA) as cl:
        await asyncio.gather(*(one(cl, c) for c in cids))

    print(f"\n{'price':>11}{'maker sh':>12}{'maker real-imp':>16}"
          f"{'taker sh':>12}{'taker real-imp':>16}{'taker fee':>11}{'adv sel':>10}")
    tot = {"maker": [0.0, 0.0], "taker": [0.0, 0.0, 0.0]}
    for b in sorted(set(agg["maker"]) | set(agg["taker"])):
        mk = agg["maker"].get(b, [0, 0, 0, 0])
        tk = agg["taker"].get(b, [0, 0, 0, 0])
        if mk[0] < 2000 or tk[0] < 2000:
            continue
        me = 100 * (mk[2] - mk[1]) / mk[0]
        te = 100 * (tk[2] - tk[1]) / tk[0]
        fee = 100 * tk[3] / tk[0]
        tot["maker"][0] += mk[0]; tot["maker"][1] += mk[2] - mk[1]
        tot["taker"][0] += tk[0]; tot["taker"][1] += tk[2] - tk[1]; tot["taker"][2] += tk[3]
        print(f"  {b/20:.2f}-{(b+1)/20:.2f}{mk[0]:>12,.0f}{me:>+16.2f}"
              f"{tk[0]:>12,.0f}{te:>+16.2f}{fee:>11.2f}{me - te:>+10.2f}")
    m = 100 * tot["maker"][1] / tot["maker"][0]
    t = 100 * tot["taker"][1] / tot["taker"][0]
    f = 100 * tot["taker"][2] / tot["taker"][0]
    print(f"\n  MAKER  realised - implied: {m:+.2f} c/share, fee 0.00  -> net {m:+.2f} c/share")
    print(f"  TAKER  realised - implied: {t:+.2f} c/share, fee {f:.2f}  -> net {t - f:+.2f} c/share")
    print(f"\n  adverse selection borne by makers: {m - t:+.2f} c/share")
    print(f"  taker fee paid instead:             {-f:+.2f} c/share")
    print(f"  => {'MAKER' if m > t - f else 'TAKER'} is the better side by "
          f"{abs(m - (t - f)):.2f} c/share")

asyncio.run(main())
