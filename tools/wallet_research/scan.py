"""Scan resolved 1h/24h crypto Up-or-Down markets and build per-wallet ledgers.

Market-side pull: every trade on every market in scope, so each wallet's record
on those markets is complete rather than a sample. Resumable via SQLite.
"""
from __future__ import annotations
import argparse, asyncio, calendar, json, sqlite3, sys, time
from pathlib import Path
import httpx

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
      "Accept": "application/json"}

# Market universes. Add new assets here rather than in the code below.
UNIVERSES = {
    "crypto": {
        "1h": ["btc-up-or-down-hourly", "eth-up-or-down-hourly", "xrp-up-or-down-hourly"],
        "24h": ["btc-up-or-down-daily", "eth-up-or-down-daily", "solana-up-or-down-daily",
                "xrp-up-or-down-daily", "bnb-up-or-down-daily", "hype-up-or-down-daily"],
    },
    # 15-minute crypto. Distinct from the deleted 5m family: these are the
    # markets the surviving taker wallets actually trade.
    "crypto15m": {
        "15m": ["btc-up-or-down-15m", "eth-up-or-down-15m",
                "sol-up-or-down-15m", "xrp-up-or-down-15m"],
    },
    # Gold/silver/oil/SPY exist as dailies only - no hourly equivalents exist.
    "macro": {
        "24h": ["gold-daily-up-or-down", "silver-daily-up-or-down", "oil-daily-up-or-down",
                "spy-daily-up-or-down", "spy-open-daily-up-or-down"],
    },
}
_FAMILY_OF: dict[str, str] = {}
HOURLY: list[str] = []
DAILY: list[str] = []

DB = Path(__file__).resolve().parents[2] / "data" / "wallet_research" / "wallets.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
  cid TEXT PRIMARY KEY, slug TEXT, series TEXT, family TEXT,
  end_ts INTEGER, winner INTEGER, volume REAL, fetched INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trades (
  cid TEXT, wallet TEXT, name TEXT, side TEXT, oidx INTEGER,
  size REAL, price REAL, ts INTEGER, txh TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_cid ON trades(cid);
CREATE INDEX IF NOT EXISTS idx_trades_wallet ON trades(wallet);
CREATE INDEX IF NOT EXISTS idx_mkt_fetched ON markets(fetched);
"""


def db_open() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    con.execute("PRAGMA journal_mode=WAL")
    return con


async def jget(cl: httpx.AsyncClient, url: str, params: dict, tries: int = 5):
    for a in range(tries):
        try:
            r = await cl.get(url, params=params, timeout=40.0)
            if r.status_code == 429:
                await asyncio.sleep(2 + 3 * a); continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if a == tries - 1:
                raise
            await asyncio.sleep(1.5 * (a + 1))
    return None


def winner_of(m: dict) -> int | None:
    """Index of the outcome that resolved to 1, or None if unresolved."""
    try:
        prices = json.loads(m.get("outcomePrices") or "[]")
    except Exception:
        return None
    vals = [float(p) for p in prices]
    if not vals or not m.get("closed"):
        return None
    hi = max(vals)
    if hi < 0.99:
        return None
    return vals.index(hi)


async def phase_markets(cl: httpx.AsyncClient, con: sqlite3.Connection, since_ts: int) -> None:
    """Enumerate resolved markets series by series.

    Gamma refuses offsets past ~2000, so the window is walked in 7-day slices
    with end_date_min/max and only shallow paging inside each slice.
    """
    now = int(time.time())
    for series in HOURLY + DAILY:
        family = _FAMILY_OF.get(series, "1h" if series in HOURLY else "24h")
        kept = 0
        lo = since_ts
        while lo < now:
            hi = min(lo + 7 * 86400, now)
            fmt = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
            offset = 0
            while True:
                evs = await jget(cl, f"{GAMMA}/events", {
                    "series_slug": series, "closed": "true", "limit": 100,
                    "offset": offset, "order": "endDate", "ascending": "true",
                    "end_date_min": fmt(lo), "end_date_max": fmt(hi)})
                if not evs:
                    break
                for e in evs:
                    for m in e.get("markets", []):
                        end = m.get("endDate") or e.get("endDate") or ""
                        try:
                            # Gamma stamps are UTC; mktime would read them as local time.
                            ts = calendar.timegm(time.strptime(end[:19], "%Y-%m-%dT%H:%M:%S"))
                        except Exception:
                            continue
                        w = winner_of(m)
                        if w is None:
                            continue
                        con.execute(
                            "INSERT OR IGNORE INTO markets(cid,slug,series,family,end_ts,winner,volume)"
                            " VALUES(?,?,?,?,?,?,?)",
                            (m["conditionId"], m.get("slug"), series, family, ts, w,
                             float(m.get("volumeNum") or 0)))
                        kept += 1
                con.commit()
                if len(evs) < 100:
                    break
                offset += 100
                if offset >= 2000:
                    break
            lo = hi
        print(f"  {series}: {kept} resolved markets in window", flush=True)


async def fetch_trades(cl: httpx.AsyncClient, cid: str) -> list[dict]:
    out, offset = [], 0
    while True:
        page = await jget(cl, f"{DATA}/trades", {
            "market": cid, "limit": 500, "offset": offset, "takerOnly": "false"})
        if not page:
            break
        out.extend(page)
        if len(page) < 500 or offset > 20000:
            break
        offset += 500
    return out


async def phase_trades(cl: httpx.AsyncClient, con: sqlite3.Connection, conc: int) -> None:
    rows = con.execute("SELECT cid FROM markets WHERE fetched=0 ORDER BY end_ts DESC").fetchall()
    total = len(rows)
    print(f"  {total} markets to pull", flush=True)
    sem = asyncio.Semaphore(conc)
    done = 0
    lock = asyncio.Lock()

    async def one(cid: str):
        nonlocal done
        async with sem:
            try:
                tr = await fetch_trades(cl, cid)
            except Exception as exc:
                print(f"  !! {cid[:12]} {exc}", flush=True)
                return
        async with lock:
            con.executemany(
                "INSERT INTO trades(cid,wallet,name,side,oidx,size,price,ts,txh)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                [(cid, t["proxyWallet"], t.get("name"), t.get("side"),
                  int(t.get("outcomeIndex") or 0), float(t.get("size") or 0),
                  float(t.get("price") or 0), int(t.get("timestamp") or 0),
                  t.get("transactionHash")) for t in tr])
            con.execute("UPDATE markets SET fetched=1 WHERE cid=?", (cid,))
            done += 1
            if done % 100 == 0:
                con.commit()
                print(f"  {done}/{total} markets", flush=True)

    await asyncio.gather(*(one(r[0]) for r in rows))
    con.commit()
    print(f"  done {done}/{total}", flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--conc", type=int, default=6)
    ap.add_argument("--markets-only", action="store_true")
    ap.add_argument("--universe", default="crypto", choices=sorted(UNIVERSES))
    ap.add_argument("--db", help="override the sqlite path")
    a = ap.parse_args()
    since = int(time.time()) - a.days * 86400
    global HOURLY, DAILY, DB
    uni = UNIVERSES[a.universe]
    HOURLY = uni.get("1h", []) + uni.get("15m", [])
    DAILY = uni.get("24h", [])
    global _FAMILY_OF
    _FAMILY_OF = {s: fam for fam, names in uni.items() for s in names}
    if a.db:
        DB = Path(a.db)
    print(f"universe {a.universe}: {len(HOURLY)} hourly + {len(DAILY)} daily series", flush=True)
    con = db_open()
    async with httpx.AsyncClient(headers=UA) as cl:
        print("phase 1: markets", flush=True)
        await phase_markets(cl, con, since)
        n = con.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
        print(f"  total {n} markets in db", flush=True)
        if a.markets_only:
            return
        print("phase 2: trades", flush=True)
        await phase_trades(cl, con, a.conc)
    con.close()

asyncio.run(main())
