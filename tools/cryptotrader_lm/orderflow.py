"""Aggressive (taker) order flow on the Polymarket daily market itself.

Buying Up or selling Down pushes toward Up; buying Down or selling Up pushes
toward Down. Only price, size, side, outcome and timestamp are kept — wallet
and profile fields from the public trades feed are dropped.
"""
from __future__ import annotations

import time

import httpx

TRADES_URL = "https://data-api.polymarket.com/trades"
_PAGE = 500
_MAX_OFFSET = 10_000
_KEEP = ("timestamp", "side", "outcome", "price", "size")


def fetch_taker_trades(client: httpx.Client, condition_id: str, start_s: int,
                       end_s: int) -> list[dict]:
    trades: list[dict] = []
    offset = 0
    while offset <= _MAX_OFFSET:
        for attempt in range(6):
            resp = client.get(TRADES_URL, params={
                "market": condition_id, "takerOnly": "true", "start": start_s,
                "end": end_s, "limit": _PAGE, "offset": offset,
            })
            if resp.status_code != 429:
                break
            time.sleep(5 * (attempt + 1))
        resp.raise_for_status()
        page = resp.json()
        trades.extend({k: row[k] for k in _KEEP} for row in page)
        if len(page) < _PAGE:
            break
        offset += _PAGE
    return trades


def up_pressure(trades: list[dict], start_s: int, end_s: int) -> dict:
    """USD notional of taker flow toward Up vs Down in [start_s, end_s)."""
    up_usd = down_usd = 0.0
    n = 0
    for tr in trades:
        if not start_s <= int(tr["timestamp"]) < end_s:
            continue
        notional = float(tr["price"]) * float(tr["size"])
        toward_up = (tr["side"] == "BUY") == (tr["outcome"] == "Up")
        if toward_up:
            up_usd += notional
        else:
            down_usd += notional
        n += 1
    total = up_usd + down_usd
    return {
        "up_usd": round(up_usd, 2),
        "down_usd": round(down_usd, 2),
        "trades": n,
        "imbalance": None if total <= 0 else (up_usd - down_usd) / total,
    }
