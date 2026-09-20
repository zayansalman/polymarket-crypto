"""Scan crypto STRIKE markets ("will X be above $Y on <date>").

Two reasons this universe is worth measuring. Seven of nine wallets that were
screened on Up-or-Down had voluntarily migrated here, and the Up-or-Down markets
turned out to be untradeable from either side — takers pay a 1.127c fee against
a +0.213c gross edge, makers pay ~3.5c in adverse selection.

The question asked here is deliberately about the MARKET, not about wallets.
Ranking wallets by past edge failed a holdout test, so repeating it in a new
universe would repeat a known mistake. Market-level calibration has been the
reliable measurement all along.

Gamma cannot enumerate these — it drops resolved short-dated markets entirely —
so the CLOB's own paginated listing is the source, which also carries an explicit
per-token `winner` flag.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
      "Accept": "application/json"}
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"

# "will bitcoin be above 84000 on ...", "eth-above-2300-on-...", etc.
STRIKE_MARKERS = ("-above-", "-be-between-")
ASSETS = ("bitcoin", "btc", "eth", "ethereum", "sol", "solana", "xrp", "doge")

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
  cid TEXT PRIMARY KEY, slug TEXT, family TEXT, end_ts INTEGER,
  winner INTEGER, volume REAL, fetched INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trades (
  cid TEXT, wallet TEXT, name TEXT, side TEXT, oidx INTEGER,
  size REAL, price REAL, ts INTEGER, txh TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_cid ON trades(cid);
CREATE INDEX IF NOT EXISTS idx_mkt_fetched ON markets(fetched);
"""


def get(url, params=None, tries=4):
    q = "?" + urllib.parse.urlencode(params, doseq=True) if params else ""
    for a in range(tries):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url + q, headers=UA), timeout=40) as r:
                return json.load(r)
        except Exception:
            if a == tries - 1:
                return None
            time.sleep(1.5 * (a + 1))
    return None


def is_strike(slug: str) -> bool:
    """A crypto PRICE strike, not a token-launch FDV market.

    `startswith` matched "solstice-fdv-above-50m" against the asset "sol" and
    filled the first run with token launches. Asset names must match on a
    hyphen boundary, and FDV/launch markets are excluded outright.
    """
    s = slug.lower()
    if not any(m in s for m in STRIKE_MARKERS):
        return False
    if "fdv" in s or "after-launch" in s or "launch-a-token" in s:
        return False
    parts = set(s.split("-"))
    return bool(parts & set(ASSETS))


def harvest(con, max_pages: int, since_ts: int) -> int:
    cursor, pages, kept = "", 0, 0
    while pages < max_pages:
        d = get(f"{CLOB}/markets", {"next_cursor": cursor} if cursor else None)
        if not isinstance(d, dict) or not d.get("data"):
            break
        for m in d["data"]:
            slug = m.get("market_slug") or ""
            if not m.get("closed") or not is_strike(slug):
                continue
            winner = None
            for i, t in enumerate(m.get("tokens") or []):
                if t.get("winner"):
                    winner = i
            if winner is None:
                continue
            end = m.get("end_date_iso") or ""
            try:
                ts = int(time.mktime(time.strptime(end[:19], "%Y-%m-%dT%H:%M:%S")))
            except Exception:
                ts = 0
            if since_ts and ts and ts < since_ts:
                continue
            con.execute(
                "INSERT OR IGNORE INTO markets(cid,slug,family,end_ts,winner,volume)"
                " VALUES(?,?,?,?,?,?)",
                (m.get("condition_id"), slug, "strike", ts, winner, 0.0))
            kept += 1
        con.commit()
        cursor = d.get("next_cursor") or ""
        pages += 1
        if not cursor or cursor == "LTE=":
            break
        if pages % 20 == 0:
            print(f"  page {pages}, {kept} strike markets kept", flush=True)
    return kept


def pull_trades(con, limit_markets: int, lo: int = 0, hi: int = 0) -> int:
    """Pull trades, optionally restricted to a date window.

    The CLOB listing runs oldest-first, so an unrestricted "newest N" picks the
    newest of whatever slice was harvested — which silently produced an 8-day
    sample masquerading as five months. The window makes the period explicit.
    """
    q = "SELECT cid FROM markets WHERE fetched=0"
    params: list = []
    if lo:
        q += " AND end_ts >= ?"
        params.append(lo)
    if hi:
        q += " AND end_ts < ?"
        params.append(hi)
    q += " ORDER BY end_ts DESC LIMIT ?"
    params.append(limit_markets)
    rows = con.execute(q, params).fetchall()
    done = 0
    for (cid,) in rows:
        out, off = [], 0
        while True:
            page = get(f"{DATA}/trades", {"market": cid, "limit": 500,
                                          "offset": off, "takerOnly": "false"})
            if not page:
                break
            out.extend(page)
            if len(page) < 500 or off > 5000:
                break
            off += 500
        con.executemany(
            "INSERT INTO trades(cid,wallet,name,side,oidx,size,price,ts,txh)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            [(cid, t["proxyWallet"], t.get("name"), t.get("side"),
              int(t.get("outcomeIndex") or 0), float(t.get("size") or 0),
              float(t.get("price") or 0), int(t.get("timestamp") or 0),
              t.get("transactionHash")) for t in out])
        con.execute("UPDATE markets SET fetched=1 WHERE cid=?", (cid,))
        done += 1
        if done % 50 == 0:
            con.commit()
            print(f"  {done}/{len(rows)} markets, trades so far", flush=True)
    con.commit()
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/wallet_research/strike.db")
    ap.add_argument("--pages", type=int, default=400)
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--markets", type=int, default=1500)
    ap.add_argument("--harvest-only", action="store_true")
    ap.add_argument("--from-date", default="", help="YYYY-MM-DD window start")
    ap.add_argument("--to-date", default="", help="YYYY-MM-DD window end")
    a = ap.parse_args()

    Path(a.db).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(a.db)
    con.executescript(SCHEMA)
    since = int(time.time()) - a.days * 86400

    print("harvesting strike markets from the CLOB listing")
    kept = harvest(con, a.pages, since)
    total = con.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    print(f"  {kept} added this run, {total} in db")
    if a.harvest_only:
        return
    def stamp(d):
        return int(time.mktime(time.strptime(d, "%Y-%m-%d"))) if d else 0

    print("pulling trades")
    pull_trades(con, a.markets, stamp(a.from_date), stamp(a.to_date))
    n = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    print(f"  {n} trades")


main()
