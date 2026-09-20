"""Live copy-trade shadow — mirror a target wallet, priced honestly (#182).

**Places no orders.** Polls a target wallet's public activity, and for every new
trade records what copying it *would* cost against the book we would actually
face at that moment: we cross the ask and pay the taker fee, because the target
is a maker and its fill is only visible to us after the fact.

Settles each window on its real outcome and reports both the copy's PnL and the
target's, so the difference is measured rather than argued.

Usage::

    python tools/copytrade_shadow.py --target 0xf8af...54c6
    python tools/copytrade_shadow.py --target 0xf8af...54c6 --scale 0.5 --once
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_bot.pairarb.mirror import MIN_ORDER_SHARES, price_the_copy

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"

DEFAULT_DB = Path("data/copytrade_shadow.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS copy_fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tx_key TEXT NOT NULL,
  window_slug TEXT,
  condition_id TEXT,
  outcome TEXT,
  their_price REAL,
  our_price REAL,
  fee REAL,
  size REAL,
  slippage REAL,
  their_ts INTEGER,
  seen_ts INTEGER,
  resolved_up INTEGER,
  our_pnl REAL,
  their_pnl REAL,
  settled_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_copy_fills_tx ON copy_fills(tx_key);
"""


def _key(t: dict[str, Any]) -> str:
    return "|".join(
        str(t.get(k, ""))
        for k in ("transactionHash", "asset", "price", "size", "timestamp")
    )


async def _get(client: httpx.AsyncClient, url: str, **params: Any) -> Any:
    r = await client.get(url, params=params or None, timeout=20.0)
    r.raise_for_status()
    return r.json()


async def fetch_asks(client: httpx.AsyncClient, token_id: str) -> list[tuple[float, float]]:
    try:
        book = await _get(client, f"{CLOB}/book", token_id=token_id)
    except Exception:  # noqa: BLE001
        return []
    return sorted((float(x["price"]), float(x["size"])) for x in book.get("asks", []))


async def resolve_window(client: httpx.AsyncClient, slug: str) -> bool | None:
    """Return whether the window resolved Up, or ``None`` if not yet resolved."""
    try:
        rows = await _get(client, f"{GAMMA}/markets", slug=slug)
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    import json as _json

    try:
        prices = [float(x) for x in _json.loads(rows[0].get("outcomePrices") or "[]")]
        outcomes = [str(x).lower() for x in _json.loads(rows[0].get("outcomes") or "[]")]
    except (ValueError, TypeError):
        return None
    if len(prices) != 2 or max(prices) < 0.99:
        return None
    idx_up = 0 if outcomes and outcomes[0] == "up" else 1
    return prices[idx_up] >= 0.99


def report(db_path: Path) -> None:
    if not db_path.exists():
        print("no ledger yet")
        return
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    rows = list(db.execute("SELECT * FROM copy_fills WHERE settled_at IS NOT NULL"))
    pending = db.execute(
        "SELECT COUNT(*) FROM copy_fills WHERE settled_at IS NULL"
    ).fetchone()[0]
    if not rows:
        print(f"copy shadow: 0 settled, {pending} pending")
        return
    n = len(rows)
    ours = sum(r["our_pnl"] or 0 for r in rows)
    theirs = sum(r["their_pnl"] or 0 for r in rows)
    slip = sum((r["slippage"] or 0) * (r["size"] or 0) for r in rows)
    shares = sum(r["size"] or 0 for r in rows)
    print("=" * 74)
    print(f"COPY-TRADE SHADOW — {n} settled fills, {pending} pending")
    print("=" * 74)
    print(f"  target PnL on the same fills : ${theirs:+10.2f}")
    print(f"  our PnL copying them         : ${ours:+10.2f}")
    print(f"  difference                   : ${ours - theirs:+10.2f}")
    if shares:
        print(
            f"\n  slippage paid (spread+fee)   : ${slip:10.2f}"
            f"  = {100 * slip / shares:.2f}c/share over {shares:,.0f} shares"
        )
        print(f"  their avg fill : {sum((r['their_price'] or 0) * (r['size'] or 0) for r in rows) / shares:.4f}")
        print(f"  our avg cost   : {sum(((r['our_price'] or 0) + (r['fee'] or 0)) * (r['size'] or 0) for r in rows) / shares:.4f}")
    print("=" * 74)


async def _settle_due(con: sqlite3.Connection, client: httpx.AsyncClient) -> list[Any]:
    """Settle every fill old enough to have resolved. Shared by both feeds."""
    unsettled = list(
        con.execute(
            "SELECT id, window_slug, outcome, our_price, fee, size, their_price"
            " FROM copy_fills WHERE settled_at IS NULL AND their_ts < ?",
            (int(time.time()) - 360,),
        )
    )
    for rid, slug, outcome, our_price, fee, size, their_price in unsettled:
        up = await resolve_window(client, slug)
        if up is None:
            continue
        won = (outcome == "Up") == up
        payout = 1.0 if won else 0.0
        our_pnl = size * (payout - (our_price + fee))
        their_pnl = size * (payout - their_price)
        con.execute(
            "UPDATE copy_fills SET resolved_up=?, our_pnl=?, their_pnl=?,"
            " settled_at=? WHERE id=?",
            (int(up), our_pnl, their_pnl, int(time.time()), rid),
        )
    con.commit()
    return unsettled


async def run(
    target: str, scale: float, max_shares: float, min_shares: float,
    skip_small: bool, max_their: float | None, max_slip: float | None,
    assets: list[str], once: bool, db_path: Path,
) -> int:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    con.commit()
    seen: set[str] = {
        r[0] for r in con.execute("SELECT tx_key FROM copy_fills")
    }
    print(f"copy shadow | target={target} scale={scale} "
          f"size={min_shares}-{max_shares}sh skip_small={skip_small} "
          f"max_their={max_their}")
    print("SHADOW ONLY — no orders are placed.\n")

    async with httpx.AsyncClient(headers={"User-Agent": "copy-shadow/0.1"}) as client:
        while True:
            try:
                acts = await _get(client, f"{DATA}/activity", user=target, limit=100)
            except Exception:  # noqa: BLE001
                acts = []
            new = 0
            for t in acts if isinstance(acts, list) else []:
                if t.get("type") != "TRADE":
                    continue
                slug_t = str(t.get("slug") or "")
                if assets and not any(slug_t.startswith(a + "-") for a in assets):
                    continue
                k = _key(t)
                if k in seen:
                    continue
                asset = str(t.get("asset") or "")
                asks = await fetch_asks(client, asset) if asset else []
                fill = price_the_copy(
                    t, asks, scale=scale, max_shares=max_shares,
                    min_shares=min_shares, skip_below_min=skip_small,
                    max_their_size=max_their,
                    max_slippage=max_slip,
                )
                seen.add(k)
                if fill is None:
                    continue
                con.execute(
                    "INSERT OR IGNORE INTO copy_fills (tx_key, window_slug, condition_id,"
                    " outcome, their_price, our_price, fee, size, slippage, their_ts, seen_ts)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        k, fill.window_slug, fill.condition_id, fill.outcome,
                        fill.their_price, fill.our_price, fill.fee, fill.size,
                        fill.slippage_per_share, fill.their_ts, int(time.time()),
                    ),
                )
                new += 1
                print(
                    f"  COPY {fill.window_slug:<28} {fill.outcome:<5} "
                    f"them {fill.their_price:.3f}  us {fill.cost_per_share:.3f}  "
                    f"slip {100 * fill.slippage_per_share:+.2f}c  x{fill.size:.0f}sh"
                )
            con.commit()

            unsettled = await _settle_due(con, client)
            if new or unsettled:
                report(db_path)
            if once:
                return 0
            await asyncio.sleep(4)


async def run_rpc(
    target: str, scale: float, max_shares: float, min_shares: float,
    skip_small: bool, max_their: float | None, max_slip: float | None,
    assets: list[str], once: bool, db_path: Path,
) -> int:
    """Same ledger and pricing as :func:`run`, sourced from the fast feed.

    No ``POLYGON_RPC_WSS`` key required — polls ``eth_getLogs`` on a public
    Polygon RPC (:func:`~polymarket_bot.pairarb.feed.http_poll_fills`), which reads
    the same on-chain event as the WSS transport at poll-interval-plus-block-
    time latency instead of the ``data-api``'s ~20s-stale batching. Detection
    time stands in for the fill timestamp (see
    ``mirror.trade_dict_from_fast_fill``), so ``their_ts`` and ``seen_ts`` are
    seconds apart here, not the tens of seconds the api feed measures.
    """
    from polymarket_bot.pairarb.feed import FeedUnavailable, http_poll_fills
    from polymarket_bot.pairarb.market_index import TokenIndex
    from polymarket_bot.pairarb.mirror import trade_dict_from_fast_fill

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    con.commit()
    seen: set[str] = {r[0] for r in con.execute("SELECT tx_key FROM copy_fills")}
    index = TokenIndex(assets or ["btc", "eth", "sol", "xrp", "doge", "bnb"])
    print(f"copy shadow (rpc feed) | target={target} scale={scale} "
          f"size={min_shares}-{max_shares}sh skip_small={skip_small} "
          f"max_their={max_their}")
    print("SHADOW ONLY — no orders are placed.\n")

    async with httpx.AsyncClient(headers={"User-Agent": "copy-shadow/0.1"}) as index_client:
        await index.refresh(index_client)

        async def _settle_loop() -> None:
            async with httpx.AsyncClient(headers={"User-Agent": "copy-shadow/0.1"}) as c:
                while True:
                    await index.refresh(c)
                    await _settle_due(con, c)
                    if once:
                        return
                    await asyncio.sleep(30)

        settle_task = asyncio.ensure_future(_settle_loop())
        try:
            async for fill in http_poll_fills(target):
                token = fill.token_id
                resolved = index.resolve(token)
                if resolved is None:
                    await index.refresh(index_client)
                    resolved = index.resolve(token)
                if resolved is None:
                    continue  # not a tracked 5m window — not our market
                slug, outcome, _condition_id = resolved
                if assets and not any(slug.startswith(a + "-") for a in assets):
                    continue
                k = "|".join((fill.tx_hash, token, f"{fill.price}", f"{fill.shares}"))
                if k in seen:
                    continue
                seen.add(k)
                now = int(time.time())
                t = trade_dict_from_fast_fill(fill.price, fill.shares, fill.tx_hash, resolved, now)
                asks = await fetch_asks(index_client, token) if token else []
                priced = price_the_copy(
                    t, asks, scale=scale, max_shares=max_shares,
                    min_shares=min_shares, skip_below_min=skip_small,
                    max_their_size=max_their, max_slippage=max_slip,
                )
                if priced is None:
                    continue
                con.execute(
                    "INSERT OR IGNORE INTO copy_fills (tx_key, window_slug, condition_id,"
                    " outcome, their_price, our_price, fee, size, slippage, their_ts, seen_ts)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        k, priced.window_slug, priced.condition_id, priced.outcome,
                        priced.their_price, priced.our_price, priced.fee, priced.size,
                        priced.slippage_per_share, priced.their_ts, now,
                    ),
                )
                con.commit()
                print(
                    f"  COPY {priced.window_slug:<28} {priced.outcome:<5} "
                    f"them {priced.their_price:.3f}  us {priced.cost_per_share:.3f}  "
                    f"slip {100 * priced.slippage_per_share:+.2f}c  x{priced.size:.0f}sh"
                )
                if once:
                    return 0
        except FeedUnavailable as exc:
            print(f"rpc feed unavailable: {exc}")
            return 1
        finally:
            settle_task.cancel()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--target",
        default="0xf8af03f1e68ee7162db8983f0d6dd0dc869854c6",
        help="wallet to mirror",
    )
    p.add_argument("--scale", type=float, default=1.0, help="fraction of their size")
    p.add_argument("--max-shares", type=float, default=50.0)
    p.add_argument(
        "--min-shares", type=float, default=MIN_ORDER_SHARES,
        help="venue floor is 5 shares; orders below it are unplaceable",
    )
    p.add_argument(
        "--max-slippage", type=float, default=None,
        help="skip a copy if price ran this far past their fill; NOTE this "
             "preferentially discards their winning trades (see mirror.py)",
    )
    p.add_argument(
        "--max-their-size", type=float, default=None,
        help="skip their trades above this many shares (UNVALIDATED filter)",
    )
    p.add_argument(
        "--assets", default="", help="comma-separated asset filter, e.g. doge",
    )
    p.add_argument(
        "--skip-small", action="store_true",
        help="decline their sub-5-share trades instead of oversizing them",
    )
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--once", action="store_true")
    p.add_argument("--report", action="store_true", help="print ledger and exit")
    p.add_argument(
        "--feed", choices=("api", "rpc"), default="api",
        help="'api' polls data-api (~20s stale, the historical default); "
             "'rpc' reads on-chain OrderFilled logs via a public Polygon RPC "
             "(~poll-interval + block time, no API key needed)",
    )
    a = p.parse_args()
    if a.report:
        report(Path(a.db))
        return 0
    runner = run_rpc if a.feed == "rpc" else run
    try:
        return asyncio.run(
            runner(
                a.target.lower(), a.scale, a.max_shares, a.min_shares,
                a.skip_small, a.max_their_size, a.max_slippage,
                [x.strip().lower() for x in a.assets.split(',') if x.strip()],
                a.once, Path(a.db),
            )
        )
    except KeyboardInterrupt:
        print("\nstopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
