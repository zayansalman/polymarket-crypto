"""Fill feed for the copier — one interface, two transports (#182).

The copier's dominant cost is feed latency, not trading logic. Measured over 357
fills, the ``data-api`` activity endpoint runs **~20 seconds stale** (p25 12s,
median 20s, p90 47s) because it exposes trades only after batching on-chain
settlement. Slippage tracks that lag almost linearly:

===============  =====================
lag bucket       median |slippage|
===============  =====================
0-2s             2.82c
3-5s             4.14c
6-10s            3.99c
11-30s           9.56c   <- the API feed
30s+             12.14c
===============  =====================

So the API transport is not a slower version of the right feed — at a ~1c-per-
share edge it inverts the trade. It survives here only as an explicitly-selected
fallback that announces itself, never as a silent default.

``ONCHAIN`` subscribes to Polygon ``OrderFilled`` logs and delivers fills at
block time (~2s) with maker/taker identity and the fee field. It needs a
``POLYGON_RPC_WSS`` endpoint (a free key from e.g. alchemy.com).

For a key-free option, :func:`http_poll_fills` polls ``eth_getLogs`` against a
public Polygon RPC every ``poll_seconds``. Public endpoints rate-limit hard, so
this is not as fast as the WSS push, but it reads the same on-chain event and is
still an order of magnitude closer to block time than the ~20s-stale API poll —
worth reaching for before ever falling back to the API transport.

That ~2s is a floor, not a tuning target: a fill becomes attributable only once
settled, so a copier always races the confirmation of something the target has
already done. Beating it requires not reacting at all — i.e. resting orders of
your own.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from polymarket_bot.pairarb.onchain import decode_order_filled, subscription_params


class FeedUnavailable(RuntimeError):
    """Raised when no low-latency transport can be established."""


@dataclass(frozen=True)
class FeedFill:
    """One observed fill by the target, normalised across transports.

    Attributes:
        token_id: Outcome token the target traded.
        price: Their fill price.
        shares: Their size.
        observed_lag: Seconds between their fill and our observation. This is
            the number that decides whether a copy is worth placing, so it
            travels with every fill rather than being inferred later.
        source: ``'onchain'`` or ``'api'``.
        is_maker: Whether they rested (fee-free). ``None`` when the transport
            cannot tell — the API feed cannot.
        tx_hash: Settlement transaction, when known.
    """

    token_id: str
    price: float
    shares: float
    observed_lag: float
    source: str
    is_maker: bool | None = None
    tx_hash: str = ""


async def onchain_fills(
    target: str, wss_url: str, roles: tuple[bool, ...] = (True, False)
) -> AsyncIterator[FeedFill]:
    """Yield the target's fills from Polygon logs as blocks land.

    Subscribes once per role because ``maker`` and ``taker`` occupy different
    indexed topic slots. The target rests for ~40% of its fills and crosses for
    ~35%, so watching only one role drops a third of the flow.

    Reconnects on drop: a copier that silently stops receiving looks identical
    to a quiet market, which is the most dangerous failure mode available.
    """
    import websockets

    while True:
        try:
            async with websockets.connect(wss_url, ping_interval=20) as ws:
                for i, as_maker in enumerate(roles):
                    await ws.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": i,
                                "method": "eth_subscribe",
                                "params": ["logs", subscription_params(target, as_maker)],
                            }
                        )
                    )
                while True:
                    raw = await ws.recv()
                    msg = json.loads(raw)
                    log = (msg.get("params") or {}).get("result")
                    if not log:
                        continue
                    fill = decode_order_filled(log)
                    if fill is None or not fill.shares:
                        continue
                    mine_is_maker = fill.maker.lower() == target.lower()
                    yield FeedFill(
                        token_id=fill.token_id,
                        price=fill.price,
                        shares=fill.shares,
                        # Block timestamp is not on the log, so this is the
                        # delivery latency of the subscription itself. It
                        # excludes block time (~2s), which is the real floor.
                        observed_lag=0.0,
                        source="onchain",
                        is_maker=mine_is_maker and fill.is_maker_fill,
                        tx_hash=fill.tx_hash,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — reconnect rather than die silently
            await asyncio.sleep(2)


# Free, read-only Polygon RPCs — no key needed. Order is a latency/rate-limit
# preference, not a correctness one; a dead endpoint is skipped, not fatal.
PUBLIC_HTTP_RPCS = (
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.llamarpc.com",
    "https://polygon.drpc.org",
    "https://rpc.ankr.com/polygon",
)


async def _rpc(client: Any, url: str, method: str, params: Any) -> Any:
    r = await client.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json().get("result")


async def _pick_public_rpc(client: Any) -> str | None:
    """First public RPC that answers ``eth_blockNumber``, or ``None``."""
    for url in PUBLIC_HTTP_RPCS:
        try:
            if await _rpc(client, url, "eth_blockNumber", []):
                return url
        except Exception:  # noqa: BLE001
            continue
    return None


async def http_poll_fills(
    target: str, poll_seconds: float = 2.0, roles: tuple[bool, ...] = (True, False)
) -> AsyncIterator[FeedFill]:
    """Yield the target's fills via ``eth_getLogs`` polling — no RPC key needed.

    Same decoded event as :func:`onchain_fills`, delivered by HTTP polling
    instead of a push subscription. Latency is ``poll_seconds`` plus block time,
    not block time alone, but it is reading the chain directly rather than
    waiting on the data-api's batched ingestion — the dominant cost this feed
    module exists to avoid.

    Raises :class:`FeedUnavailable` if no public endpoint answers at all: at
    that point neither this nor :func:`onchain_fills` can run, and the caller's
    only real option is the explicit, stale API fallback.
    """
    import httpx

    async with httpx.AsyncClient(headers={"User-Agent": "copy-feed-http/0.1"}) as client:
        rpc = await _pick_public_rpc(client)
        if rpc is None:
            raise FeedUnavailable(
                "no reachable public Polygon RPC for HTTP polling "
                "(all of PUBLIC_HTTP_RPCS failed eth_blockNumber)"
            )
        last = int(await _rpc(client, rpc, "eth_blockNumber", []) or "0x0", 16)
        while True:
            try:
                head = int(await _rpc(client, rpc, "eth_blockNumber", []) or "0x0", 16)
                if head > last:
                    for as_maker in roles:
                        params = dict(subscription_params(target, as_maker))
                        params["fromBlock"] = hex(last + 1)
                        params["toBlock"] = hex(head)
                        logs = await _rpc(client, rpc, "eth_getLogs", [params]) or []
                        for log in logs:
                            fill = decode_order_filled(log)
                            if fill is None or not fill.shares:
                                continue
                            mine_is_maker = fill.maker.lower() == target.lower()
                            yield FeedFill(
                                token_id=fill.token_id,
                                price=fill.price,
                                shares=fill.shares,
                                # Same rationale as onchain_fills: this excludes
                                # block time, which is the real floor.
                                observed_lag=0.0,
                                source="rpc_poll",
                                is_maker=mine_is_maker and fill.is_maker_fill,
                                tx_hash=fill.tx_hash,
                            )
                    last = head
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — try another endpoint, don't die
                rpc = await _pick_public_rpc(client) or rpc
            await asyncio.sleep(poll_seconds)


async def api_fills(
    target: str, poll_seconds: float = 3.0
) -> AsyncIterator[FeedFill]:
    """Yield fills from the ``data-api`` activity endpoint. **~20s stale.**

    Kept only as an explicit fallback. Every fill carries its measured
    ``observed_lag`` so a consumer can drop stale ones rather than trade on
    them blind.
    """
    import httpx

    seen: set[str] = set()
    async with httpx.AsyncClient(headers={"User-Agent": "copy-feed/0.1"}) as c:
        while True:
            try:
                r = await c.get(
                    "https://data-api.polymarket.com/activity",
                    params={"user": target, "limit": 100},
                    timeout=20.0,
                )
                rows = r.json()
            except Exception:  # noqa: BLE001
                rows = []
            now = time.time()
            for t in rows if isinstance(rows, list) else []:
                if t.get("type") != "TRADE":
                    continue
                key = "|".join(
                    str(t.get(k, ""))
                    for k in ("transactionHash", "asset", "price", "size", "timestamp")
                )
                if key in seen:
                    continue
                seen.add(key)
                try:
                    yield FeedFill(
                        token_id=str(t.get("asset") or ""),
                        price=float(t["price"]),
                        shares=float(t["size"]),
                        observed_lag=now - float(t["timestamp"]),
                        source="api",
                        is_maker=None,
                        tx_hash=str(t.get("transactionHash") or ""),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            await asyncio.sleep(poll_seconds)


def open_feed(target: str, allow_api_fallback: bool = False) -> AsyncIterator[FeedFill]:
    """Return the best available fill feed for ``target``.

    Prefers ``POLYGON_RPC_WSS``. Raises :class:`FeedUnavailable` when it is
    unset unless the caller has explicitly opted into the stale API transport,
    because degrading from a 2s feed to a 20s one without saying so would hand
    the consumer numbers that look fine and are not.
    """
    wss = os.getenv("POLYGON_RPC_WSS", "").strip()
    if wss:
        return onchain_fills(target, wss)
    if not allow_api_fallback:
        raise FeedUnavailable(
            "POLYGON_RPC_WSS is not set. The data-api fallback is ~20s stale, "
            "which at a ~1c/share edge inverts the trade (median slippage 9.56c "
            "at 11-30s vs 2.82c at 0-2s). Get a free Polygon WSS endpoint "
            "(alchemy.com -> Polygon Mainnet -> WSS), or pass "
            "allow_api_fallback=True to accept the stale feed knowingly."
        )
    return api_fills(target)
