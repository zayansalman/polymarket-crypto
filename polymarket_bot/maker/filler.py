"""Decide whether a resting quote would really have filled, and settle it.

This is the part paper trading usually gets wrong. The easy version marks a
quote filled as soon as the tape prints at our price — which silently assumes
we were first in the queue, on every order, forever. Real passive orders sit
behind whatever was already there.

So a fill requires volume to trade THROUGH the queue we joined:

    crossed  = taker volume that sold our token at or below our bid since we quoted
    ours     = max(0, crossed - depth_ahead)
    filled   = min(quote_size, ours)

``crossed`` is recorded even when it never reaches us, because "400 shares
traded at my price and I got none" and "nothing ever came" are different
failures and only one of them is fixed by quoting more aggressively.

Flow is read from taker records only. The full feed carries both sides of every
match, so counting it whole would double every trade and halve the queue.
"""

from __future__ import annotations

import time

import httpx
import structlog

from polymarket_bot.maker import ledger as _ledger

DATA = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

log = structlog.get_logger(__name__)


async def _taker_flow(
    client: httpx.AsyncClient, condition_id: str, since_ts: int
) -> list[tuple[int, int, str, float, float]]:
    """(ts, outcome_index, side, size, price) for aggressive trades since ``since_ts``."""
    out: list[tuple[int, int, str, float, float]] = []
    offset = 0
    while True:
        try:
            r = await client.get(
                f"{DATA}/trades",
                params={"market": condition_id, "limit": 500, "offset": offset,
                        "takerOnly": "true"},
                timeout=25.0)
            r.raise_for_status()
            feed = r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("maker.flow_failed", cid=condition_id[:12], error=str(exc))
            return out
        if not feed:
            break
        stop = False
        for t in feed:
            try:
                ts = int(t["timestamp"])
            except (KeyError, TypeError, ValueError):
                continue
            if ts < since_ts:
                stop = True          # the feed is newest-first
                continue
            try:
                out.append((ts, int(t.get("outcomeIndex", -1)), str(t.get("side")),
                            float(t["size"]), float(t["price"])))
            except (TypeError, ValueError):
                continue
        if stop or len(feed) < 500:
            break
        offset += 500
        if offset > 5000:
            break
    return out


def crossed_volume(
    flow: list[tuple[int, int, str, float, float]],
    *, our_index: int, our_price: float,
) -> float:
    """Aggressive volume that sold our token at or below our bid.

    The two outcomes share one book: a taker BUYING the other token at q is the
    same event as a taker SELLING ours at 1-q. Counting only the rows stamped
    with our own token id would miss half the flow that hit our bid.
    """
    total = 0.0
    for _ts, idx, side, size, price in flow:
        if idx == our_index:
            sells, px = side == "SELL", price
        elif idx == 1 - our_index:
            sells, px = side == "BUY", 1.0 - price
        else:
            continue
        if sells and px <= our_price + 1e-9:
            total += size
    return total


async def check_fills(client: httpx.AsyncClient, token_index: dict[str, int]) -> int:
    """Walk every resting quote. Fill, expire, or just record what it saw."""
    rows = await _ledger.resting()
    if not rows:
        return 0
    now = int(time.time())
    by_cid: dict[str, list[dict]] = {}
    for r in rows:
        by_cid.setdefault(r["condition_id"], []).append(r)

    filled = 0
    for cid, group in by_cid.items():
        since = min(int(r["quoted_ts"]) for r in group)
        flow = await _taker_flow(client, cid, since)
        for r in group:
            idx = token_index.get(r["token_id"])
            if idx is None:
                continue
            mine = [f for f in flow if f[0] >= int(r["quoted_ts"])]
            crossed = crossed_volume(mine, our_index=idx,
                                     our_price=float(r["quote_price"]))
            ours = crossed - float(r["depth_ahead"] or 0.0)
            if ours > 0:
                size = min(float(r["quote_size"]), ours)
                await _ledger.fill(r["id"], ts=now, size=size, crossed=crossed)
                filled += 1
                log.info("maker.filled", market=r["window_slug"],
                         outcome=r["outcome"], price=r["quote_price"],
                         size=round(size, 1), crossed=round(crossed, 1),
                         queue=round(float(r["depth_ahead"] or 0), 1))
            else:
                await _ledger.note_crossed(r["id"], crossed)
                resolves = int(r["resolves_at"] or 0)
                if resolves and now > resolves:
                    await _ledger.expire(r["id"], ts=now, crossed=crossed)
                    log.info("maker.expired", market=r["window_slug"],
                             price=r["quote_price"], crossed=round(crossed, 1),
                             queue=round(float(r["depth_ahead"] or 0), 1))
    return filled


async def settle_due(client: httpx.AsyncClient) -> int:
    """Settle filled quotes whose market has resolved.

    Resolution comes from the CLOB's per-token ``winner`` flag, not Gamma: Gamma
    drops short-dated markets after they end and leaves the hourly ones at
    ``closed: false`` long past the outcome.

    A maker pays NO fee, so the payout is the whole of it.
    """
    rows = await _ledger.filled_unsettled()
    if not rows:
        return 0
    by_cid: dict[str, list[dict]] = {}
    for r in rows:
        by_cid.setdefault(r["condition_id"], []).append(r)

    now = int(time.time())
    settled = 0
    for cid, group in by_cid.items():
        try:
            resp = await client.get(f"{CLOB}/markets/{cid}", timeout=20.0)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            market = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("maker.settle_failed", cid=cid[:12], error=str(exc))
            continue
        if not market.get("closed"):
            continue
        winners = [str(t.get("outcome")) for t in (market.get("tokens") or [])
                   if t.get("winner")]
        if not winners:
            continue
        for r in group:
            size = float(r["filled_size"] or 0.0)
            px = float(r["quote_price"] or 0.0)
            if size <= 0 or px <= 0:
                continue
            won = str(r["outcome"]) == winners[0]
            pnl = (size if won else 0.0) - size * px
            await _ledger.settle(r["id"], won=won, pnl=pnl, ts=now)
            settled += 1
            log.info("maker.settled", market=r["window_slug"],
                     outcome=r["outcome"], won=won, pnl=round(pnl, 2),
                     cps=round(100 * pnl / size, 2))
    return settled
