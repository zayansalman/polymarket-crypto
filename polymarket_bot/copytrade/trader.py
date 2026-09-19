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

FEE_RATE = 0.07

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
    """Price and record one copy. True iff a new paper position was opened.

    Every fill examined is written to ``copy_decisions``, including the ones
    declined and why. A copier that skips silently is indistinguishable from
    one with nothing to do, and the skips are where the cost hides.
    """
    target = _targets.get(target_address)
    label = target.label if target else target_address[:10]
    now_i = int(time.time())

    async def note(decision: str, reason: str, our_price=None, our_size=None):
        await _ledger.log_decision(
            ts=now_i, target=target_address, target_label=label, tx=fill.tx,
            condition_id=fill.condition_id, title=fill.title,
            outcome=fill.outcome, their_size=fill.size, their_price=fill.price,
            decision=decision, reason=reason, our_price=our_price,
            our_size=our_size, lag_seconds=round(fill.lag_seconds, 1))

    if fill.side != "BUY":
        await note("skipped", "target sold; we mirror entries only")
        return False
    if not fill.followed:
        await note("skipped", "market outside the family this target was measured on")
        return False

    scale = float(await _knobs.get("copy_scale"))
    max_shares = float(await _knobs.get("copy_max_shares"))
    max_slippage = float(await _knobs.get("copy_max_slippage_cents")) / 100.0

    try:
        asks = await _asks(client, fill.token_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("copytrade.book_failed", token=fill.token_id[:16], error=str(exc))
        await note("skipped", f"book unavailable ({type(exc).__name__})")
        return False
    if not asks:
        await note("skipped", "empty book — market already settled or halted")
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
        best = asks[0][0]
        if fill.size * scale < MIN_ORDER_SHARES:
            why = (f"their clip {fill.size:.1f}sh scales below the "
                   f"{MIN_ORDER_SHARES:.0f}sh venue floor")
        elif max_slippage > 0 and (best - fill.price) > max_slippage:
            why = (f"book moved {100*(best-fill.price):+.1f}c against us, over the "
                   f"{100*max_slippage:.0f}c cap")
        else:
            why = f"no fillable depth (best ask {best:.3f})"
        await note("skipped", why, our_price=best)
        log.info("copytrade.skipped", target=label, reason=why)
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
    await note("copied" if opened else "duplicate",
               "mirrored at the live ask" if opened else "already recorded",
               our_price=copy.our_price, our_size=copy.size)
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


async def requote_due(client: httpx.AsyncClient, delay_s: int = 25) -> int:
    """Re-price open copies against the book a real order would have met.

    The decision-time price assumes the displayed ladder survives until our
    order lands, which is the optimism the operator called out. This re-walks
    the SAME order against the book ``delay_s`` later and stores both, so the
    difference between an optimal paper fill and a realistic one is measured
    rather than assumed.
    """
    rows = await _ledger.needs_requote(delay_s, int(time.time()))
    done = 0
    for r in rows[:25]:
        try:
            asks = await _asks(client, r["token_id"])
        except Exception:  # noqa: BLE001
            asks = []
        now = int(time.time())
        if not asks:
            # Nothing to cross: a real order would have rested unfilled.
            await _ledger.set_requote(r["id"], price=None, fee=0.0, cost=0.0,
                                      slippage=None, now=now)
            done += 1
            continue
        want = r["size"] or 0.0
        got = cost = 0.0
        for px, depth in asks:
            take = min(depth, want - got)
            if take <= 0:
                break
            got += take
            cost += take * px
        if got <= 0:
            await _ledger.set_requote(r["id"], price=None, fee=0.0, cost=0.0,
                                      slippage=None, now=now)
            done += 1
            continue
        px = cost / got
        fee = got * FEE_RATE * px * (1 - px)
        slip = (px + fee / got) - (r["their_price"] or px)
        await _ledger.set_requote(r["id"], price=px, fee=fee, cost=cost + fee,
                                  slippage=slip, now=now)
        done += 1
        log.info("copytrade.requoted", target=r["target_label"],
                 decision_px=round(r["our_price"] or 0, 3), real_px=round(px, 3),
                 filled=round(got, 1), wanted=round(want, 1))
    return done


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
