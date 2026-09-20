"""Label every stored fill maker or taker, one API call per market.

The trade table was pulled with ``takerOnly=false``, so it holds BOTH sides of
every match: the aggressor and the resting order it ran into. Re-pulling the
same market with ``takerOnly=true`` returns only the aggressive records, so a
fill present in the full feed but absent there was PASSIVE — a real resting
order that really filled, at a real price, fee-free.

That is the measurement the maker question needs. No simulation, no fill-rate
assumption, no queue-position guess: these orders filled.

Labels are cached in their own database so the analysis can be re-run without
re-spending the API calls.
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import time

import httpx

DATA = "https://data-api.polymarket.com"
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Accept": "application/json",
}
PAGE = 500


def open_out(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS tk(
          cid TEXT, txh TEXT, wallet TEXT, size_r REAL, price_r REAL);
        CREATE INDEX IF NOT EXISTS tk_cid ON tk(cid);
        CREATE TABLE IF NOT EXISTS done(cid TEXT PRIMARY KEY, n_taker INTEGER);
        """
    )
    return con


async def one_market(client: httpx.AsyncClient, cid: str) -> list[tuple] | None:
    """Every taker-side record for one market, paged to exhaustion."""
    rows: list[tuple] = []
    offset = 0
    while True:
        for attempt in range(4):
            try:
                r = await client.get(
                    f"{DATA}/trades",
                    params={"market": cid, "limit": PAGE, "offset": offset,
                            "takerOnly": "true"},
                    timeout=30.0,
                )
                r.raise_for_status()
                feed = r.json()
                break
            except Exception:
                if attempt == 3:
                    return None
                await asyncio.sleep(1.5 * (attempt + 1))
        if not feed:
            break
        for t in feed:
            rows.append((
                cid,
                t.get("transactionHash"),
                (t.get("proxyWallet") or "").lower(),
                round(float(t["size"]), 4),
                round(float(t["price"]), 4),
            ))
        if len(feed) < PAGE:
            break
        offset += PAGE
        if offset > 20000:      # the offset cap; say so rather than truncate quietly
            print(f"  {cid[:10]} hit the offset cap — partial", flush=True)
            break
    return rows


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/wallet_research/wallets.db")
    ap.add_argument("--out", default="data/wallet_research/maker.db")
    ap.add_argument("--families", default="1h,24h")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--markets", type=int, default=800)
    ap.add_argument("--concurrency", type=int, default=6)
    a = ap.parse_args()

    fams = tuple(a.families.split(","))
    src = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    out = open_out(a.out)
    since = int(time.time()) - a.days * 86400

    q = (
        "SELECT cid FROM markets WHERE fetched=1 AND winner IS NOT NULL "
        f"AND end_ts>=? AND family IN ({','.join('?' * len(fams))}) "
        "ORDER BY end_ts DESC LIMIT ?"
    )
    cids = [r[0] for r in src.execute(q, (since, *fams, a.markets))]
    have = {r[0] for r in out.execute("SELECT cid FROM done")}
    todo = [c for c in cids if c not in have]
    print(f"{len(cids)} markets in scope, {len(have)} already labelled, "
          f"{len(todo)} to pull", flush=True)

    sem = asyncio.Semaphore(a.concurrency)
    done_n = 0

    async with httpx.AsyncClient(headers=UA) as client:
        async def work(cid: str):
            nonlocal done_n
            async with sem:
                rows = await one_market(client, cid)
            if rows is None:
                return
            out.execute("BEGIN")
            out.executemany("INSERT INTO tk VALUES(?,?,?,?,?)", rows)
            out.execute("INSERT OR REPLACE INTO done VALUES(?,?)", (cid, len(rows)))
            out.commit()
            done_n += 1
            if done_n % 25 == 0:
                print(f"  {done_n}/{len(todo)} markets", flush=True)

        for i in range(0, len(todo), 200):
            await asyncio.gather(*(work(c) for c in todo[i:i + 200]))

    print(f"labelled {done_n} markets, "
          f"{out.execute('SELECT count(*) FROM tk').fetchone()[0]:,} taker records")


asyncio.run(main())
