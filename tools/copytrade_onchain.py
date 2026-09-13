"""Real-time on-chain fill listener for a target wallet (#182).

Replaces the ``data-api`` activity poll, which is **~20s stale** (median over
357 measured fills), with Polygon ``OrderFilled`` logs at block time — ~2s.
Measured slippage by lag bucket: 9.56c median at 11-30s versus 2.82c at 0-2s,
so this is worth roughly 3x on the dominant cost.

Transport only; decoding lives in :mod:`polymarket_bot.pairarb.onchain`.

Needs a Polygon RPC. Reading logs costs no gas and fits any free tier — set
``POLYGON_RPC_WSS`` (preferred, pushed) or ``POLYGON_RPC_HTTP`` (polled).
Public endpoints work for a smoke test but rate-limit hard.

Usage::

    export POLYGON_RPC_WSS=wss://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY
    python tools/copytrade_onchain.py --validate     # prove the decoder first
    python tools/copytrade_onchain.py                # live listen
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_bot.pairarb.onchain import (
    decode_order_filled,
    subscription_params,
)

DEFAULT_TARGET = "0xf8af03f1e68ee7162db8983f0d6dd0dc869854c6"
DATA = "https://data-api.polymarket.com"

PUBLIC_HTTP = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.llamarpc.com",
    "https://polygon.drpc.org",
    "https://rpc.ankr.com/polygon",
]


def _post(url: str, method: str, params: Any, timeout: int = 25) -> Any:
    req = urllib.request.Request(
        url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def pick_http_rpc() -> str | None:
    """First reachable HTTP RPC — env override wins."""
    env = os.getenv("POLYGON_RPC_HTTP", "")
    for url in ([env] if env else []) + PUBLIC_HTTP:
        try:
            if _post(url, "eth_blockNumber", [], timeout=10).get("result"):
                return url
        except Exception:  # noqa: BLE001
            continue
    return None


def _get(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def validate(target: str) -> int:
    """Prove the decoder against trades the data-api already reported.

    Takes recent activity rows, pulls the same transactions' receipts from
    Polygon, decodes their ``OrderFilled`` logs, and checks that price and size
    agree. A decoder that silently mis-scales amounts would otherwise produce
    confident, wrong fills.
    """
    rpc = pick_http_rpc()
    if not rpc:
        print("no reachable Polygon RPC — set POLYGON_RPC_HTTP")
        return 1
    print(f"RPC: {rpc}")
    acts = _get(f"{DATA}/activity?user={target}&limit=40")
    trades = [a for a in acts if a.get("type") == "TRADE" and a.get("transactionHash")][:6]
    if not trades:
        print("no recent trades to validate against")
        return 1

    print(f"\n{'tx':<14} {'api price':>10} {'chain price':>12} {'api sz':>9} {'chain sz':>10} {'ok':>4}")
    print("-" * 66)
    ok_count = 0
    for t in trades:
        tx = t["transactionHash"]
        try:
            rcpt = _post(rpc, "eth_getTransactionReceipt", [tx]).get("result")
        except Exception as exc:  # noqa: BLE001
            print(f"{tx[:12]}.. receipt failed: {str(exc)[:40]}")
            continue
        if not rcpt:
            continue
        fills = [
            f
            for f in (decode_order_filled(lg) for lg in rcpt.get("logs", []))
            if f is not None
            and target.lower() in (f.maker.lower(), f.taker.lower())
        ]
        if not fills:
            print(f"{tx[:12]}.. no OrderFilled log matched this wallet")
            continue
        api_p, api_s = float(t["price"]), float(t["size"])
        best = min(fills, key=lambda f: abs(f.price - api_p))
        good = abs(best.price - api_p) < 0.02 and abs(best.shares - api_s) / max(api_s, 1) < 0.05
        ok_count += good
        print(
            f"{tx[:12]}.. {api_p:>10.4f} {best.price:>12.4f} "
            f"{api_s:>9.2f} {best.shares:>10.2f} {'OK' if good else 'XX':>4}"
        )
    print("-" * 66)
    print(f"{ok_count}/{len(trades)} decoded correctly")
    return 0 if ok_count else 1


async def listen(target: str) -> int:
    """Subscribe to the target's fills and print them as blocks land."""
    wss = os.getenv("POLYGON_RPC_WSS", "")
    if not wss:
        print(
            "POLYGON_RPC_WSS is not set — falling back to HTTP polling, which "
            "gives up most of the latency win.\n"
            "  Free key: https://alchemy.com -> Polygon Mainnet -> WSS URL"
        )
        return await poll(target)
    import websockets

    print(f"listening for {target} via {wss.split('/v2/')[0]}/... (block time ~2s)")
    async with websockets.connect(wss, ping_interval=20) as ws:
        for as_maker in (True, False):
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1 if as_maker else 2,
                        "method": "eth_subscribe",
                        "params": ["logs", subscription_params(target, as_maker)],
                    }
                )
            )
        while True:
            msg = json.loads(await ws.recv())
            log = (msg.get("params") or {}).get("result")
            if not log:
                continue
            fill = decode_order_filled(log)
            if not fill:
                continue
            role = "MAKER" if fill.maker.lower() == target.lower() else "taker"
            side = "BUY " if fill.maker_bought else "SELL"
            print(
                f"  {time.strftime('%H:%M:%S')} {role:<5} {side} "
                f"{fill.shares:>8.2f}sh @ {fill.price:.4f}  "
                f"blk {fill.block_number}  {fill.tx_hash[:14]}.."
            )


async def poll(target: str) -> int:
    """HTTP fallback: poll eth_getLogs each block-ish, measuring real lag."""
    rpc = pick_http_rpc()
    if not rpc:
        print("no reachable Polygon RPC")
        return 1
    print(f"polling {rpc} (HTTP fallback)")
    last = int(_post(rpc, "eth_blockNumber", [])["result"], 16)
    while True:
        try:
            head = int(_post(rpc, "eth_blockNumber", [])["result"], 16)
            if head > last:
                for as_maker in (True, False):
                    params = subscription_params(target, as_maker)
                    params.update({"fromBlock": hex(last + 1), "toBlock": hex(head)})
                    res = _post(rpc, "eth_getLogs", [params]).get("result") or []
                    for lg in res:
                        f = decode_order_filled(lg)
                        if f:
                            print(
                                f"  {time.strftime('%H:%M:%S')} "
                                f"{'MAKER' if as_maker else 'taker':<5} "
                                f"{'BUY ' if f.maker_bought else 'SELL'} "
                                f"{f.shares:>8.2f}sh @ {f.price:.4f} blk {f.block_number}"
                            )
                last = head
        except Exception as exc:  # noqa: BLE001
            print(f"  rpc error: {str(exc)[:70]}")
        await asyncio.sleep(2)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target", default=DEFAULT_TARGET)
    p.add_argument("--validate", action="store_true", help="prove the decoder and exit")
    a = p.parse_args()
    if a.validate:
        return validate(a.target.lower())
    try:
        return asyncio.run(listen(a.target.lower()))
    except KeyboardInterrupt:
        print("\nstopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
