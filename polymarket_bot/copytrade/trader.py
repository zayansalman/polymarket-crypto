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

import time
from typing import Any

import httpx
import structlog

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
    skip_small = bool(await _knobs.get("copy_skip_below_min"))

    try:
        asks = await _asks(client, fill.token_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            # The window had already closed by the time we saw the fill. The
            # target entered in the last seconds; a follower cannot be there.
            await note("skipped", "market already closed when we saw the fill "
                                  f"({fill.lag_seconds:.0f}s behind)")
        else:
            await note("skipped", f"book request failed (HTTP {exc.response.status_code})")
        return False
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
        skip_below_min=skip_small,
        max_slippage=max_slippage if max_slippage > 0 else None,
    )
    if copy is None:
        best = asks[0][0]
        depth = sum(sz for _px, sz in asks)
        want = max(MIN_ORDER_SHARES, fill.size * scale)
        # Report the reason that actually applied. The floor only rejects when
        # skip_below_min is on; blaming it otherwise is a wrong log, which is
        # worse than none.
        if skip_small and fill.size < MIN_ORDER_SHARES:
            why = (f"their clip {fill.size:.1f}sh is under the "
                   f"{MIN_ORDER_SHARES:.0f}sh venue floor")
        elif max_slippage > 0 and (best - fill.price) > max_slippage:
            why = (f"book moved {100*(best-fill.price):+.1f}c against us, over the "
                   f"{100*max_slippage:.0f}c cap")
        elif depth < want:
            why = (f"only {depth:.1f}sh on the whole ask side, need {want:.1f}sh")
        else:
            why = (f"declined at best ask {best:.3f} vs their {fill.price:.3f} "
                   f"({depth:.0f}sh depth)")
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
        their_size=fill.size,
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
    if opened and copy.size > fill.size * 1.05:
        # Their clip was under the venue floor, so the mirror is larger than
        # the bet they made. Per-share edge is unchanged, which is what we are
        # measuring, but the exposure is not a 1:1 mirror and must say so.
        log.info("copytrade.upsized", target=label,
                 theirs=round(fill.size, 2), ours=round(copy.size, 2))
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
    """Settle every open copy whose market has resolved. Returns rows settled.

    Resolution comes from the CLOB, not Gamma. Gamma drops the 15-minute markets
    entirely once they end — they are returned by neither condition_ids nor slug
    — and it leaves the hourly ones at ``closed: false`` long after the outcome
    is decided, so a Gamma-based settler silently never fires for either. The
    CLOB's market record carries ``closed`` plus an explicit ``winner`` flag per
    token, which is the authoritative answer for both families.
    """
    rows = await _ledger.open_rows()
    if not rows:
        return 0
    by_cid: dict[str, list[dict]] = {}
    for r in rows:
        by_cid.setdefault(r["condition_id"], []).append(r)

    settled = 0
    for cid, group in by_cid.items():
        try:
            resp = await client.get(f"{CLOB}/markets/{cid}", timeout=20.0)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            market = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("copytrade.settle_lookup_failed", cid=cid[:12], error=str(exc))
            continue
        if not market.get("closed"):
            continue
        winners = [
            str(t.get("outcome"))
            for t in (market.get("tokens") or [])
            if t.get("winner")
        ]
        if not winners:
            continue  # closed but not yet resolved — leave it open
        winner = winners[0]
        now = int(time.time())
        for r in group:
            won = str(r["outcome"]) == winner
            payout = r["size"] if won else 0.0
            pnl = payout - (r["cost_usd"] or 0.0)
            # The realistic result. A row whose book was gone at re-quote never
            # filled, so it is flat — not a winner. Rows not yet re-quoted have
            # no realistic price to settle against and stay null rather than
            # silently inheriting the optimistic one.
            if r.get("requoted_at") is None:
                real_pnl = None
            elif r.get("real_price") is None:
                real_pnl = 0.0
            else:
                real_pnl = payout - (r["real_cost_usd"] or 0.0)
            await _ledger.settle(r["id"], won=won, pnl=pnl,
                                 real_pnl=real_pnl, now=now)
            settled += 1
            log.info(
                "copytrade.settled", target=r["target_label"],
                market=r["window_slug"], ours=r["outcome"], winner=winner,
                pnl=round(pnl, 2),
            )
    return settled
