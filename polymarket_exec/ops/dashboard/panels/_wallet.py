"""Polymarket wallet size for the EMS ribbon: cash + open positions value.

Read-only and keyless — only the public funder address is used:

* cash      — on-chain collateral (pUSD) ``balanceOf(funder)`` on Polygon
* positions — Polymarket Data API ``/value?user=funder``

Matches Polymarket's own "Portfolio" (= cash + positions) and "Cash". Cached
for ``TTL_SECONDS`` so the dashboard's frequent refreshes never hammer the
RPC; on a failed refresh the last good snapshot is kept and marked stale.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

import config as _config

COLLATERAL_TOKEN = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"  # pUSD, Polygon
COLLATERAL_DECIMALS = 6
POLYGON_RPCS = (
    "https://polygon-bor-rpc.publicnode.com",
    "https://1rpc.io/matic",
    "https://polygon.drpc.org",
)
DATA_API_VALUE = "https://data-api.polymarket.com/value"
TTL_SECONDS = 30.0

_lock = asyncio.Lock()
_cache: dict[str, Any] = {"funder": None, "at": 0.0, "snapshot": None}


async def _cash(client: httpx.AsyncClient, funder: str) -> float:
    data = "0x70a08231" + funder[2:].lower().rjust(64, "0")
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": COLLATERAL_TOKEN, "data": data}, "latest"],
    }
    last: Exception | None = None
    for rpc in POLYGON_RPCS:
        try:
            r = await client.post(rpc, json=payload)
            r.raise_for_status()
            result = r.json().get("result")
            if result is None:
                raise ValueError(r.json())
            return int(result, 16) / 10**COLLATERAL_DECIMALS
        except Exception as e:  # noqa: BLE001 — try the next RPC
            last = e
    raise RuntimeError(f"all Polygon RPCs failed: {last}")


async def _positions(client: httpx.AsyncClient, funder: str) -> float:
    r = await client.get(DATA_API_VALUE, params={"user": funder})
    r.raise_for_status()
    rows = r.json()
    return float(sum(float(row.get("value") or 0) for row in rows or []))


async def wallet_snapshot() -> dict[str, Any] | None:
    """``{cash, positions, total, stale}`` for the configured funder, or None
    when no wallet is configured or it has never been read successfully."""
    funder = (_config.POLYMARKET_FUNDER or "").strip()
    if not funder.startswith("0x") or len(funder) != 42:
        return None
    async with _lock:
        fresh = (
            _cache["funder"] == funder
            and _cache["snapshot"] is not None
            and time.monotonic() - _cache["at"] < TTL_SECONDS
        )
        if fresh:
            return _cache["snapshot"]
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                cash, positions = await asyncio.gather(
                    _cash(client, funder), _positions(client, funder)
                )
        except Exception:  # noqa: BLE001 — keep the last good number, flag it
            prior = _cache["snapshot"] if _cache["funder"] == funder else None
            if prior is None:
                return None
            _cache["at"] = time.monotonic()  # back off for one TTL
            return {**prior, "stale": True}
        snapshot = {
            "cash": cash,
            "positions": positions,
            "total": cash + positions,
            "stale": False,
        }
        _cache.update(funder=funder, at=time.monotonic(), snapshot=snapshot)
        return snapshot
