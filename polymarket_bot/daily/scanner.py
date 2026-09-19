"""The daily altcoin scanner's tick loop (issue #185).

Every ``config.DAILY_SCAN_INTERVAL_SECONDS`` (default 60s — this market
resolves on a ~24h cadence, so it has no need of the BTC 5m loop's 5s
tick): scores every tracked asset's daily Up/Down market, opens ONE $10
paper (shadow-only) position on the single strongest qualifying signal, and
settles any window whose resolution instant has passed. No live gate exists
for this strategy — it is paper-only, full stop.
"""
from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime

import httpx

import config as _config
from logging_setup import get_logger
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot import strategies as _strategies
from polymarket_bot import strategy as _strategy
from polymarket_bot.daily import ledger as _ledger
from polymarket_bot.daily import market as _market
from polymarket_bot.daily import signal as _signal
from polymarket_bot.daily.types import DailyMarketView, DailySignal

log = get_logger("daily_scanner")


async def _params() -> _strategy.StrategyParams:
    # min == max: a fixed $ size, not confidence-scaled (operator asked for
    # flat $10 positions, unlike the BTC loop's confidence-weighted $1-$5).
    trade_usd = await _knobs.get("daily_trade_usd")
    return _strategy.StrategyParams(
        min_trade_usd=trade_usd,
        max_trade_usd=trade_usd,
        entry_edge_min=await _knobs.get("daily_entry_edge_min"),
        min_confidence=0.0,
        entry_min_remaining_seconds=_config.DAILY_ENTRY_MIN_REMAINING_SECONDS,
    )


async def _scored_view(
    client: httpx.AsyncClient, short_asset: str, market: dict
) -> tuple[DailyMarketView, DailySignal] | None:
    view = await _market.build_market_view(client, short_asset, market)
    if view is None:
        log.warning("daily_scan.no_view", asset=short_asset)
        return None
    closes = await _market.fetch_daily_closes(
        client, view.binance_symbol, days=await _knobs.get("daily_vol_lookback_days")
    )
    if len(closes) < 5:
        log.warning("daily_scan.insufficient_history", asset=short_asset, n=len(closes))
        return None
    sigma, drift = _signal.daily_sigma_and_drift_per_second(closes)
    fair_up = _signal.fair_up_probability(view.spot, view.reference, sigma, view.remaining_seconds)
    view = dataclasses.replace(view, fair_up=fair_up, sigma_per_second=sigma, drift_per_second=drift)
    sig = _signal.score(view, await _params())
    if sig is None:
        return None
    return view, sig


async def _shares_for(sig: DailySignal, view: DailyMarketView) -> float:
    """Shares for a flat $ notional, floored to the venue's order minimum.

    DOGE/BNB carry materially thinner books than SOL/XRP/ETH (confirmed
    live: ~$110-140 liquidity vs. $8k-39k) — a paper fill still shouldn't
    claim more size than the book could actually absorb, so this caps
    shares at the reported liquidity too (same "don't silently oversize"
    principle as the BTC loop's #85/#87 share-floor fix, applied here to a
    liquidity CEILING instead of a minimum-order FLOOR).
    """
    raw_shares = await _knobs.get("daily_trade_usd") / sig.entry_price
    shares = max(raw_shares, view.order_min_size)
    if view.liquidity_usd is not None and view.liquidity_usd > 0:
        liquidity_shares = view.liquidity_usd / sig.entry_price
        shares = min(shares, max(liquidity_shares, view.order_min_size))
    return shares


async def scan_once(client: httpx.AsyncClient) -> None:
    """One tick: settle whatever is due, then look for a new entry.

    The two halves are INDEPENDENT and neither may swallow the other.
    Settlement runs first and unconditionally because it needs nothing from
    discovery — it settles off the ``reference_price``/``resolves_at``/
    ``binance_symbol`` stamped on each open row (see :func:`_settle_due`).
    This used to sit behind an early ``return`` on empty discovery, so any
    tick that found no markets also skipped settlement: a live run left an
    already-resolved doge window ``state='open'`` for over a day that way,
    through a stretch where the UTC-date slug lookup fixed in #239 returned
    nothing from noon ET to UTC midnight. A Gamma outage would do the same.

    Each half logs its own failure rather than aborting the other, per
    AGENTS.md's no-silent-failures rule.
    """
    try:
        await _settle_due(client)
    except Exception:  # noqa: BLE001 — a stuck settlement must not stop entries
        log.exception("daily_scan.settle_failed")
    try:
        await _enter_best(client)
    except Exception:  # noqa: BLE001 — a broken entry pass must not strand settlements
        log.exception("daily_scan.entry_pass_failed")


async def _enter_best(client: httpx.AsyncClient) -> None:
    """Score every tracked asset and open at most one position this tick."""
    # Operator switch (STRATEGIES card): off stops NEW entries only. It
    # deliberately gates this half alone — _settle_due above runs whatever
    # the switch says, so turning the scanner off can never strand an open
    # window (the #245 failure, reached by a different route).
    if not await _strategies.enabled("daily_altcoin"):
        return

    markets = await _market.discover_daily_markets(client)
    if not markets:
        log.warning("daily_scan.no_markets_found")
        return
    scored: list[tuple[DailyMarketView, DailySignal]] = []
    for short_asset, market in markets.items():
        try:
            result = await _scored_view(client, short_asset, market)
        except Exception:  # noqa: BLE001
            log.exception("daily_scan.asset_failed", asset=short_asset)
            continue
        if result is not None:
            scored.append(result)

    best = _signal.rank([sig for _view, sig in scored])
    if best is not None:
        view = next(v for v, s in scored if s is best)
        shares = await _shares_for(best, view)
        inserted = await _ledger.record_signal(
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            window_slug=view.window_slug,
            asset=best.asset,
            side=best.side,
            entry_price=best.entry_price,
            fair_prob=best.fair_prob,
            edge=best.edge,
            confidence=best.confidence,
            reason=best.reason,
            notional_usd=shares * best.entry_price,
            shares=shares,
            reference_price=view.reference,
            resolves_at=view.resolves_at,
            binance_symbol=view.binance_symbol,
            sigma_per_second=view.sigma_per_second,
            drift_per_second=view.drift_per_second,
        )
        if inserted:
            log.info(
                "daily_scan.entry",
                asset=best.asset,
                side=best.side,
                edge=round(best.edge, 4),
                entry_price=best.entry_price,
            )


async def _settle_due(client: httpx.AsyncClient) -> None:
    """Settle any open window whose resolution instant has passed.

    Self-contained: computes the outcome from the ``reference_price``/
    ``binance_symbol``/``resolves_at`` stamped on each row at record time
    (see :func:`polymarket_bot.daily.ledger.record_signal`), never by
    re-querying Polymarket — a live check found an already-resolved market
    in this family stops being returned by the same ``?slug=`` lookup used
    to discover it while open, so depending on Gamma for the outcome would
    silently strand every settlement. This mirrors the existing BTC-family
    convention of settling from the same settlement-aligned feed used at
    entry, not from the venue's resolution API.
    """
    now = int(datetime.now(UTC).timestamp())
    for row in await _ledger.open_settlement_candidates():
        # Per row, mirroring the entry half's per-asset isolation: one row
        # whose price lookup misbehaves must not abort settlement for every
        # other due window, on every tick, forever.
        try:
            await _settle_row(client, row, now)
        except Exception:  # noqa: BLE001 — one poison row must not strand the rest
            log.exception("daily_scan.settle_row_failed", window_slug=row["window_slug"])


async def _settle_row(client: httpx.AsyncClient, row: dict, now: int) -> None:
    """Settle one open row, if its resolution instant has passed."""
    end = _epoch_or_none(row["resolves_at"])
    if end is None:
        log.warning("daily_scan.settle_bad_resolves_at", window_slug=row["window_slug"])
        return
    if now < end:
        return  # still trading: not a failure, and the common case
    settlement_price = await _market.fetch_close_at(client, row["binance_symbol"], end)
    if settlement_price is None:
        # fetch_close_at turns every HTTP error into None, so without this
        # the row strands exactly like the bug scan_once fixes, but silently
        # (AGENTS.md: no silent failures).
        log.warning(
            "daily_scan.settle_price_unavailable",
            window_slug=row["window_slug"],
            symbol=row["binance_symbol"],
        )
        return
    reference = row["reference_price"]
    if settlement_price == reference:
        outcome_side = None  # exact tie -> resolves 50-50, see ledger.settle
    else:
        outcome_side = "Up" if settlement_price > reference else "Down"
    n = await _ledger.settle(
        window_slug=row["window_slug"],
        outcome_side=outcome_side,
        settlement_price=settlement_price,
        resolved_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    if n:
        log.info(
            "daily_scan.settled",
            window_slug=row["window_slug"],
            outcome=outcome_side or "tie",
        )


def _epoch_or_none(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


async def run_forever(stop_event: asyncio.Event | None = None) -> None:
    """Run the scan loop until ``stop_event`` is set (or forever if ``None``)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        while stop_event is None or not stop_event.is_set():
            # Backstop: both halves of scan_once catch their own failures, so
            # this fires only for a step added later without its own handler.
            # The interval read is INSIDE it on purpose — it is a SQLite read
            # that can raise, and it is the one statement whose failure would
            # end the loop for the process's lifetime. Nothing supervises this
            # task (app.py starts it with a bare create_task), so an escape
            # here stops the scanner with nothing in the logs.
            try:
                await scan_once(client)
                interval = await _knobs.get("daily_scan_interval_seconds")
            except Exception:  # noqa: BLE001
                log.exception("daily_scan.tick_failed")
                interval = _config.DAILY_SCAN_INTERVAL_SECONDS
            await asyncio.sleep(interval)
