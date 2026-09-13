"""LIVE copy-trade executor — mirrors a target wallet with real funds (#182).

**This places real orders with real money.** It is off by default and refuses to
start unless every gate below is armed by the operator. No agent arms these; the
operator does (``AGENTS.md``).

Gates, all mandatory:

1. ``assert_live_boot_allowed()`` — the repo's existing live gate: a private key,
   a coherent signature type, and a funder address for proxy wallets.
2. ``COPY_LIVE_CONFIRM=YES_I_UNDERSTAND`` — a **separate** phrase, so arming the
   BTC strategy bot never silently arms the copier. They are different risks and
   must be consented to independently.
3. ``--live`` on the command line. Without it this runs dry and prints the orders
   it *would* send.
4. ``--bankroll`` — a hard ceiling on capital outstanding. Refuses to open a
   position that would breach it.

The private key is read by the CLOB client and is never logged, printed, or
persisted here.

**Capital note.** Measured from the target's own tape, copying its full flow at
the venue minimum needs a mean of $67 and peaks at $317 outstanding. Running a
smaller bankroll than the peak does not scale the strategy down — it makes
participation depend on whether cash happened to be free, which selects trades by
liquidity accident rather than by rule. Use ``--assets`` to pick a deterministic
subset that genuinely fits instead.

Usage::

    python tools/copytrade_live.py --preflight
    python tools/copytrade_live.py --bankroll 25 --assets doge          # dry run
    python tools/copytrade_live.py --bankroll 25 --assets doge --live   # REAL
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_bot.pairarb.feed import FeedUnavailable, open_feed
from polymarket_bot.pairarb.mirror import MIN_ORDER_SHARES, price_the_copy

CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"

COPY_CONFIRM_PHRASE = "YES_I_UNDERSTAND"
DEFAULT_DB = Path("data/copytrade_live.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_copies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tx_key TEXT NOT NULL,
  placed_at INTEGER,
  window_slug TEXT,
  token_id TEXT,
  outcome TEXT,
  their_price REAL,
  limit_price REAL,
  size REAL,
  status TEXT,
  order_id TEXT,
  response TEXT,
  cost_usd REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_live_copies_tx ON live_copies(tx_key);
"""


class CopyBootRefused(RuntimeError):
    """Raised when live copying is requested but a gate is not armed."""


def assert_copy_live_allowed(live_flag: bool) -> None:
    """Refuse live copying unless every gate is armed.

    Layered on top of the repo's own live gate rather than replacing it, and
    adds a copier-specific confirmation so the two live paths cannot be armed by
    accident from one another's env.
    """
    if not live_flag:
        raise CopyBootRefused("--live was not passed (this is the safe default)")

    problems: list[str] = []
    try:
        from polymarket_exec.execution.live import assert_live_boot_allowed

        assert_live_boot_allowed()
    except ImportError as exc:  # noqa: BLE001
        problems.append(f"cannot import the repo live gate ({exc})")
    except Exception as exc:  # noqa: BLE001 — LiveBootRefused and friends
        problems.append(str(exc))

    if os.getenv("COPY_LIVE_CONFIRM", "") != COPY_CONFIRM_PHRASE:
        problems.append(
            f"COPY_LIVE_CONFIRM is not '{COPY_CONFIRM_PHRASE}' — arming the BTC "
            "strategy bot does not arm the copier; they are separate risks"
        )
    try:
        import py_clob_client_v2  # noqa: F401
    except ImportError:
        problems.append("py_clob_client_v2 is not installed")

    if not os.getenv("POLYGON_RPC_WSS", "").strip():
        # Refusing rather than degrading. The data-api feed is ~20s stale and
        # median slippage there is 9.56c against a ~1c/share edge, so a live
        # copier on it is structurally negative. This is a correctness gate,
        # not a performance preference.
        problems.append(
            "POLYGON_RPC_WSS is not set — live copying on the ~20s-stale "
            "data-api feed loses by construction (9.56c median slippage vs "
            "2.82c at 0-2s); get a free Polygon WSS endpoint first"
        )

    if problems:
        raise CopyBootRefused(
            "LIVE COPY REFUSED: " + " and ".join(problems)
            + ". Every gate is mandatory; there is no fallback to a smaller size."
        )


def preflight(bankroll: float, assets: list[str]) -> int:
    """Report whether a live run could start, without starting one."""
    print("=" * 72)
    print("COPY-TRADE LIVE PREFLIGHT")
    print("=" * 72)
    checks: list[tuple[str, bool, str]] = []

    try:
        import py_clob_client_v2  # noqa: F401

        checks.append(("py_clob_client_v2 installed", True, ""))
    except ImportError:
        checks.append(
            ("py_clob_client_v2 installed", False, "pip install py-clob-client-v2")
        )
    for var, need in (
        ("POLYGON_RPC_WSS", "wss://polygon-mainnet.g.alchemy.com/v2/KEY (free)"),
        ("POLYMARKET_PRIVATE_KEY", "your signer key (never shown)"),
        ("POLYMARKET_FUNDER", "proxy wallet address"),
        ("COPY_LIVE_CONFIRM", COPY_CONFIRM_PHRASE),
    ):
        val = os.getenv(var, "")
        ok = (
            bool(val)
            if ("KEY" in var or "FUNDER" in var or "WSS" in var)
            else val == COPY_CONFIRM_PHRASE
        )
        checks.append((var, ok, f"set to {need}" if not ok else ""))

    for name, ok, fix in checks:
        print(f"  [{'OK ' if ok else 'XX'}] {name}{'' if ok else '  -> ' + fix}")

    print("\n  capital plan")
    print(f"    bankroll            ${bankroll:.2f}")
    print(f"    assets              {','.join(assets) if assets else 'ALL (6)'}")
    per = MIN_ORDER_SHARES * 0.45
    print(f"    typical clip        {MIN_ORDER_SHARES:.0f} shares ~ ${per:.2f}")
    print(f"    concurrent capacity ~{bankroll / per:.0f} open fills")
    scale = (len(assets) / 6.0) if assets else 1.0
    print(f"    measured need at this scope: mean ${67 * scale:.0f}, peak ${317 * scale:.0f}")
    if bankroll < 317 * scale:
        print(
            f"    !! bankroll is below the measured PEAK (${317 * scale:.0f}). You will be"
            "\n       cash-blocked during bursts, and the trades you catch will be"
            "\n       selected by whether cash was free, not by any rule."
        )
    print("=" * 72)
    return 0 if all(ok for _, ok, _ in checks) else 1


async def run(
    target: str,
    bankroll: float,
    assets: list[str],
    max_shares: float,
    live: bool,
    db_path: Path,
) -> int:
    assert_copy_live_allowed(live)  # raises unless every gate is armed

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    con.commit()
    seen: set[str] = {r[0] for r in con.execute("SELECT tx_key FROM live_copies")}
    outstanding = 0.0
    opened: list[tuple[int, float]] = []  # (expiry_ts, cost)

    client_obj: Any = None
    if live:
        from py_clob_client_v2 import ClobClient

        import config as _config

        client_obj = ClobClient(
            host=CLOB,
            key=_config.POLYMARKET_PRIVATE_KEY,
            chain_id=137,
            signature_type=_config.POLYMARKET_SIGNATURE_TYPE,
            funder=_config.POLYMARKET_FUNDER or None,
        )
        await asyncio.to_thread(client_obj.create_or_derive_api_key)
        print("CLOB client ready (key never logged)")

    mode = "LIVE — REAL ORDERS" if live else "DRY RUN — no orders sent"
    print(f"copy live | target={target} bankroll=${bankroll} assets={assets or 'all'}")
    print(f"MODE: {mode}\n")

    async with httpx.AsyncClient(headers={"User-Agent": "copy-live/0.1"}) as http:
        while True:
            now = int(time.time())
            # release capital from windows that have resolved
            still = [(e, c) for e, c in opened if e > now]
            outstanding = sum(c for _, c in still)
            opened = still

            try:
                r = await http.get(
                    f"{DATA}/activity", params={"user": target, "limit": 50}, timeout=20
                )
                acts = r.json()
            except Exception:  # noqa: BLE001
                acts = []

            for t in acts if isinstance(acts, list) else []:
                if t.get("type") != "TRADE":
                    continue
                slug = str(t.get("slug") or "")
                if assets and not any(slug.startswith(a + "-") for a in assets):
                    continue
                key = "|".join(
                    str(t.get(k, ""))
                    for k in ("transactionHash", "asset", "price", "size", "timestamp")
                )
                if key in seen:
                    continue
                seen.add(key)

                token = str(t.get("asset") or "")
                if not token:
                    continue
                try:
                    b = await http.get(
                        f"{CLOB}/book", params={"token_id": token}, timeout=20
                    )
                    asks = sorted(
                        (float(x["price"]), float(x["size"]))
                        for x in b.json().get("asks", [])
                    )
                except Exception:  # noqa: BLE001
                    continue

                fill = price_the_copy(t, asks, max_shares=max_shares)
                if fill is None:
                    continue
                cost = fill.size * fill.cost_per_share
                if outstanding + cost > bankroll:
                    print(
                        f"  SKIP {slug:<28} would breach bankroll "
                        f"(${outstanding:.2f}+${cost:.2f} > ${bankroll:.2f})"
                    )
                    continue

                # Marketable limit at the ask we priced against — never a market
                # order. A market order on a thin 5m book can fill arbitrarily far
                # through the ladder.
                limit = round(min(0.99, fill.our_price + 0.01), 2)
                status, oid, resp = "DRY_RUN", "", ""
                if live:
                    from py_clob_client_v2 import OrderArgs

                    args = OrderArgs(
                        token_id=token,
                        price=limit,
                        size=fill.size,
                        side="BUY",
                    )
                    try:
                        raw = await asyncio.to_thread(
                            client_obj.create_and_post_order, args
                        )
                        status = str(raw.get("status", "sent"))
                        oid = str(raw.get("orderID", ""))
                        resp = str(raw)[:400]
                    except Exception as exc:  # noqa: BLE001
                        status, resp = "ERROR", str(exc)[:400]

                con.execute(
                    "INSERT OR IGNORE INTO live_copies (tx_key, placed_at, window_slug,"
                    " token_id, outcome, their_price, limit_price, size, status,"
                    " order_id, response, cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        key, now, slug, token, fill.outcome, fill.their_price,
                        limit, fill.size, status, oid, resp, cost,
                    ),
                )
                con.commit()
                if status != "ERROR":
                    outstanding += cost
                    end = int(slug.split("-")[-1]) + 300 if slug[-1].isdigit() else now + 300
                    opened.append((end, cost))
                print(
                    f"  {'ORDER' if live else 'WOULD'} {slug:<28} {fill.outcome:<5} "
                    f"them {fill.their_price:.3f} lim {limit:.2f} x{fill.size:.0f}sh "
                    f"${cost:.2f} | out ${outstanding:.2f}/{bankroll:.0f} [{status}]"
                )

            await asyncio.sleep(3)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target", default="0xf8af03f1e68ee7162db8983f0d6dd0dc869854c6")
    p.add_argument("--bankroll", type=float, default=25.0)
    p.add_argument("--assets", default="", help="comma-separated, e.g. doge")
    p.add_argument("--max-shares", type=float, default=MIN_ORDER_SHARES)
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--preflight", action="store_true")
    p.add_argument(
        "--live",
        action="store_true",
        help="ACTUALLY PLACE ORDERS. Without this the run is a dry run.",
    )
    a = p.parse_args()
    assets = [x.strip().lower() for x in a.assets.split(",") if x.strip()]
    if a.preflight:
        return preflight(a.bankroll, assets)
    try:
        return asyncio.run(
            run(
                a.target.lower(), a.bankroll, assets, a.max_shares, a.live, Path(a.db)
            )
        )
    except CopyBootRefused as exc:
        print(f"\n{exc}\n")
        return 2
    except KeyboardInterrupt:
        print("\nstopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
