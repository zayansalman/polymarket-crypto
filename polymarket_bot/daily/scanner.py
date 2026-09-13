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

    await _settle_due(client)


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
        end = _epoch_or_none(row["resolves_at"])
        if end is None or now < end:
            continue
        settlement_price = await _market.fetch_close_at(client, row["binance_symbol"], end)
        if settlement_price is None:
            continue
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
            try:
                await scan_once(client)
            except Exception:  # noqa: BLE001
                log.exception("daily_scan.tick_failed")
            await asyncio.sleep(await _knobs.get("daily_scan_interval_seconds"))
