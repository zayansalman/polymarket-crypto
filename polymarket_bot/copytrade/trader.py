"""Turn an observed fill into a paper copy, and settle it when the market does.

Pricing is deliberately pessimistic and honest: we do NOT record the target's
price. We fetch the live ask ladder at the moment we see their fill — already
~20s behind — and walk it with ``pairarb.mirror.price_the_copy``, paying the
taker fee they may not have paid. The recorded cost is what following them would
actually have cost, spread and fee included.

That gap is the experiment. A copy can lose two different ways — the target was
wrong, or following them cost more than their edge — and only recording both
prices tells those apart.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import structlog

import config as _config
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot.copytrade import ledger as _ledger
from polymarket_bot.copytrade import targets as _targets
from polymarket_bot.pairarb.mirror import MIN_ORDER_SHARES, price_the_copy

log = structlog.get_logger(__name__)

CLOB = "https://clob.polymarket.com"


async def _asks(client: httpx.AsyncClient, token_id: str) -> list[tuple[float, float]]:
    """Live ask ladder for one outcome token, cheapest first."""
    r = await client.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=15.0)
    r.raise_for_status()
    book = r.json()
    rungs = [
        (float(a["price"]), float(a["size"]))
        for a in (book.get("asks") or [])
        if float(a.get("size") or 0) > 0
    ]
    # The CLOB returns asks worst-first; price_the_copy walks cheapest-first.
    return sorted(rungs, key=lambda r: r[0])


async def consider(
    client: httpx.AsyncClient, fill: Any, target_address: str
) -> bool:
    """Price and record one copy. True iff a new paper position was opened."""
    if fill.side != "BUY" or not fill.followed:
        return False

    target = _targets.get(target_address)
    label = target.label if target else target_address[:10]

    scale = float(await _knobs.get("copy_scale"))
    max_shares = float(await _knobs.get("copy_max_shares"))
    max_slippage = float(await _knobs.get("copy_max_slippage_cents")) / 100.0

    try:
        asks = await _asks(client, fill.token_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("copytrade.book_failed", token=fill.token_id[:16], error=str(exc))
        return False
    if not asks:
        return False

    copy = price_the_copy(
        {
            "outcome": fill.outcome,
            "size": fill.size,
            "price": fill.price,
            "timestamp": fill.ts,
            "slug": fill.slug,
            "conditionId": fill.condition_id,
        },
        asks,
        scale=scale,
        max_shares=max_shares,
        min_shares=MIN_ORDER_SHARES,
        skip_below_min=True,
        max_slippage=max_slippage if max_slippage > 0 else None,
    )
    if copy is None:
        return False

    opened = await _ledger.record(
        target=target_address,
        target_label=label,
        tx=fill.tx,
        condition_id=fill.condition_id,
        token_id=fill.token_id,
        window_slug=fill.slug,
        title=fill.title,
        outcome=fill.outcome,
        their_price=copy.their_price,
        our_price=copy.our_price,
        size=copy.size,
        fee=copy.fee,
        cost_usd=copy.size * copy.cost_per_share,
        slippage=copy.slippage_per_share,
        their_ts=fill.ts,
        our_ts=int(time.time()),
        resolves_at=fill.resolves_at,
    )
    if opened:
        log.info(
            "copytrade.copied",
            target=label,
            outcome=fill.outcome,
            theirs=round(copy.their_price, 3),
            ours=round(copy.our_price, 3),
            size=round(copy.size, 2),
            slip_c=round(100 * copy.slippage_per_share, 2),
        )
    return opened


async def settle_due(client: httpx.AsyncClient) -> int:
    """Settle every open copy whose market has resolved. Returns rows settled."""
    rows = await _ledger.open_rows()
    if not rows:
        return 0
    by_cid: dict[str, list[dict]] = {}
    for r in rows:
        by_cid.setdefault(r["condition_id"], []).append(r)

    settled = 0
    cids = list(by_cid)
    for i in range(0, len(cids), 20):
        chunk = cids[i : i + 20]
        try:
            resp = await client.get(
                f"{_config.POLYMARKET_GAMMA_API}/markets",
                params=[("condition_ids", c) for c in chunk],
                timeout=20.0,
            )
            resp.raise_for_status()
            markets = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("copytrade.settle_lookup_failed", error=str(exc))
            continue

        for m in markets:
            if not m.get("closed"):
                continue
            try:
                prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
                outcomes = json.loads(m.get("outcomes") or "[]")
            except Exception:  # noqa: BLE001
                continue
            if not prices or max(prices) < 0.99:
                continue
            winner = outcomes[prices.index(max(prices))]
            now = int(time.time())
            for r in by_cid.get(m.get("conditionId"), []):
                won = str(r["outcome"]) == str(winner)
                payout = r["size"] if won else 0.0
                pnl = payout - (r["cost_usd"] or 0.0)
                await _ledger.settle(r["id"], won=won, pnl=pnl, now=now)
                settled += 1
                log.info(
                    "copytrade.settled",
                    target=r["target_label"],
                    outcome=r["outcome"],
                    winner=winner,
                    pnl=round(pnl, 2),
                )
    return settled
