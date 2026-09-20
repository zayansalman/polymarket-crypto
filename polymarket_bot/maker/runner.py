"""The maker loop: quote the favourite, watch the queue, settle on resolution.

One pass does three things, in the order that keeps the ledger honest:

  1. settle anything that resolved, so the record is current before new claims
     are added to it;
  2. check resting quotes against the tape, filling only what traded through
     the queue;
  3. quote markets we are not already in.

We never cancel and requote. A passive order that chases the mid stops being
passive — it ends up crossing, paying the fee, and becoming the taker strategy
this one exists to replace.
"""

from __future__ import annotations

import time

import httpx
import structlog

from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot import strategies as _strategies
from polymarket_bot.maker import filler as _filler
from polymarket_bot.maker import ledger as _ledger
from polymarket_bot.maker import quoter as _quoter

log = structlog.get_logger(__name__)

SERIES = [
    "btc-up-or-down-hourly", "eth-up-or-down-hourly", "xrp-up-or-down-hourly",
    "btc-up-or-down-daily", "eth-up-or-down-daily", "solana-up-or-down-daily",
    "xrp-up-or-down-daily",
]

DEFAULTS = {
    # The band where real maker fills were profitable. Below 0.55 the sign
    # flips: passive buyers of underdogs lost 5-7c/share on the same tape.
    "band_lo": 0.55,
    "band_hi": 0.92,
    "size": 25.0,
    "improve": True,
    "max_spread": 0.06,
    # Do not quote into a market about to resolve: a fill in the last seconds
    # is a coin flip on a stale price, not the edge we measured.
    "min_seconds_left": 120,
}


async def pass_once(
    client: httpx.AsyncClient, cfg: dict | None = None, *, quote: bool = True
) -> dict:
    """Settle, check fills, then quote. Returns a count of each.

    ``quote=False`` runs the bookkeeping half only: resting quotes are still
    filled and settled, but no new one is placed.
    """
    c = {**DEFAULTS, **(cfg or {})}
    now = int(time.time())

    markets = await _quoter.live_markets(client, SERIES)
    token_index: dict[str, int] = {}
    for m in markets:
        for i, tok in enumerate(m.tokens):
            token_index[tok] = i

    settled = await _filler.settle_due(client)
    filled = await _filler.check_fills(client, token_index)

    # Any market we have already quoted is done, filled or not: one fixed clip
    # each. See the ledger note — repeat quotes in the markets that draw the
    # most flow are a volume-weighted book wearing a fixed-clip costume, and the
    # same band measured that way loses money.
    already = await _ledger.quoted_markets()
    placed = skipped = 0

    if not quote:
        return {"quoted": 0, "skipped": 0, "filled": filled, "settled": settled}

    for m in markets:
        left = m.end_ts - now
        if left < c["min_seconds_left"]:
            await _ledger.log_decision(
                ts=now, condition_id=m.condition_id, title=m.title,
                decision="skipped",
                reason=f"only {left}s left, under the {c['min_seconds_left']}s floor")
            skipped += 1
            continue
        if m.condition_id in already:
            continue

        books = {}
        for tok in m.tokens:
            b = await _quoter.book(client, tok)
            if b is not None:
                books[tok] = b
        quote, reason = _quoter.decide(
            m, books, band_lo=c["band_lo"], band_hi=c["band_hi"],
            size=c["size"], improve=c["improve"], max_spread=c["max_spread"])

        if quote is None:
            await _ledger.log_decision(
                ts=now, condition_id=m.condition_id, title=m.title,
                decision="skipped", reason=reason)
            skipped += 1
            continue

        row_id = await _ledger.place(
            quoted_ts=now, condition_id=m.condition_id, token_id=quote.token_id,
            window_slug=m.slug, title=m.title, outcome=quote.outcome,
            quote_price=quote.price, quote_size=quote.size,
            best_bid=quote.best_bid, best_ask=quote.best_ask, mid=quote.mid,
            spread=quote.spread, depth_ahead=quote.depth_ahead,
            resolves_at=m.end_ts)
        await _ledger.log_decision(
            ts=now, condition_id=m.condition_id, token_id=quote.token_id,
            title=m.title, outcome=quote.outcome, best_bid=quote.best_bid,
            best_ask=quote.best_ask,
            decision="quoted" if row_id else "duplicate", reason=reason,
            quote_price=quote.price, quote_size=quote.size)
        if row_id:
            placed += 1
            log.info("maker.quoted", market=m.slug, outcome=quote.outcome,
                     price=quote.price, size=quote.size,
                     queue=round(quote.depth_ahead, 1),
                     spread_c=round(100 * quote.spread, 1))

    return {"markets": len(markets), "quoted": placed, "skipped": skipped,
            "filled": filled, "settled": settled}


async def config_from_knobs() -> dict:
    """Current operator settings, read fresh so a change applies next pass."""
    return {
        "band_lo": float(await _knobs.get("maker_band_lo")),
        "band_hi": float(await _knobs.get("maker_band_hi")),
        "size": float(await _knobs.get("maker_size")),
        "improve": bool(await _knobs.get("maker_improve_tick")),
        "max_spread": float(await _knobs.get("maker_max_spread_cents")) / 100.0,
        "min_seconds_left": int(await _knobs.get("maker_min_seconds_left")),
    }


async def run_forever(stop_event: "object | None" = None) -> None:
    """Poll until ``stop_event`` is set (or forever if ``None``).

    Nothing supervises this task, so a raise here would stop the maker silently
    for the life of the process. Every pass is guarded, the knob reads included
    — they are SQLite reads that can raise — and a failure is logged rather than
    allowed to escape.
    """
    import asyncio

    await _ledger.init()
    async with httpx.AsyncClient(headers=_quoter.UA, timeout=25.0) as client:
        while stop_event is None or not stop_event.is_set():
            interval = 45.0
            try:
                interval = float(await _knobs.get("maker_poll_interval_seconds"))
                # Off stops NEW quotes only. Settlement and fill checks run
                # either way, so flipping the switch can never strand a quote
                # that is already resting — the contract every switch in
                # ``polymarket_bot.strategies`` promises.
                quote = await _strategies.enabled("maker")
                stats = await pass_once(
                    client, await config_from_knobs(), quote=quote)
                if stats["quoted"] or stats["filled"] or stats["settled"]:
                    log.info("maker.pass", **stats)
            except Exception:  # noqa: BLE001
                log.exception("maker.tick_failed")
            await asyncio.sleep(interval)
