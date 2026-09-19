"""Daily Up/Down market discovery and per-asset price/spot resolution.

Market discovery constructs today's slug directly
(``{gamma_name}-up-or-down-on-{month}-{day}-{year}``) and queries Gamma by
exact slug. Tried the "list everything, then classify" approach first
(``tools.venue_recorder.discover()``/``classify()``) since it doesn't
require knowing each asset's exact slug spelling in advance — but a live
run found it unreliable in practice: ``discover()``'s "up or down" question
substring match is broad enough to be swamped by unrelated categories
platform-wide, and it silently missed 4 of 5 tracked assets' markets that a
direct ``?slug=`` lookup found instantly. The asset-name mapping below
(``bnb`` works, ``binancecoin`` 404s) was itself confirmed live before
relying on it, so a direct construct-and-fetch is the reliable path;
``discover()``/``classify()`` remains the fallback for an asset whose direct
slug 404s (e.g. the naming template changes), not the primary path.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx

import config as _config
from polymarket_bot.daily.types import DailyMarketView
from polymarket_bot.pairarb.market_index import parse_market
from polymarket_exec.connectors.updown_quote import daily_reference_instant
from tools.venue_recorder import GAMMA_API, SPOT_SYMBOL, classify, discover

# Short config-facing asset key -> the full name Gamma's slug spells out.
# xrp/bnb already agree with their short form; bnb is confirmed correct
# where the more obvious "binancecoin" 404s.
_GAMMA_ASSET_NAME = {
    "sol": "solana",
    "doge": "dogecoin",
    "xrp": "xrp",
    "bnb": "bnb",
    "eth": "ethereum",
    "btc": "bitcoin",
}
_SLUG_ASSET_TO_SHORT = {v: k for k, v in _GAMMA_ASSET_NAME.items()}

# Distinguishes this family from the old 5m/15m/1h clock-floor family
# (slug shape "{asset}-updown-{rung}-{ts}") by slug shape alone — NOT by the
# startDate..endDate span, which is the TRADING window and can be ~2 days
# wide even though the actual resolution always compares two specific
# noon-ET closes on consecutive days (see fetch_close_at).
_DAILY_SLUG_MARKER = "-up-or-down-on-"


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _epoch(value: Any) -> int | None:
    dt = _parse_dt(value)
    return int(dt.timestamp()) if dt is not None else None


def _todays_slug(gamma_name: str, now: datetime) -> str:
    return f"{gamma_name}-up-or-down-on-{now.strftime('%B').lower()}-{now.day}-{now.year}"


async def _fetch_by_slug(client: httpx.AsyncClient, slug: str) -> dict[str, Any] | None:
    try:
        resp = await client.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=15.0)
        resp.raise_for_status()
        rows = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    return rows[0] if rows else None


async def _fallback_via_discover(
    client: httpx.AsyncClient, short_asset: str
) -> dict[str, Any] | None:
    """Last resort if the direct slug 404s: sweep + classify, one asset only."""
    markets = await discover(client)
    candidates = []
    for m in markets:
        slug = (m.get("slug") or "").lower()
        if _DAILY_SLUG_MARKER not in slug:
            continue
        asset_raw, _family, _rung, _evidence = classify(m)
        if _SLUG_ASSET_TO_SHORT.get(asset_raw) != short_asset:
            continue
        if _epoch(m.get("endDate")) is not None:
            candidates.append(m)
    if not candidates:
        return None
    return min(candidates, key=lambda m: _epoch(m.get("endDate")))


async def discover_daily_markets(
    client: httpx.AsyncClient, tracked_assets: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Return ``{short_asset: raw_gamma_market_dict}`` for today's daily family.

    Only assets in ``tracked_assets`` (default: ``config.DAILY_ASSETS``) are
    kept; an asset whose market can't be found (direct slug 404 AND the
    discover() fallback empty) is silently skipped for this tick — logged by
    the caller, not fatal to the other assets' scan.
    """
    tracked = tracked_assets if tracked_assets is not None else _config.DAILY_ASSETS
    now = datetime.now(UTC)
    by_asset: dict[str, dict[str, Any]] = {}
    for short in tracked:
        gamma_name = _GAMMA_ASSET_NAME.get(short)
        if gamma_name is None:
            continue
        market = await _fetch_by_slug(client, _todays_slug(gamma_name, now))
        if market is None:
            market = await _fallback_via_discover(client, short)
        if market is not None:
            by_asset[short] = market
    return by_asset


async def fetch_close_at(
    client: httpx.AsyncClient, symbol: str, ts: int
) -> float | None:
    """The 1-minute Binance close price at ``ts`` — used for BOTH the window's
    reference print (at record time) and its settlement print (at
    ``resolves_at``, once the window has closed): same operation, two
    different instants.

    NOT the market's ``startDate`` — checked a live market's own resolution
    text (Gamma ``description``) and it resolves on the Binance 1-minute
    close at *noon ET on the calendar day before ``endDate``* (e.g. Solana's
    Aug-30 market: "Up if the Aug 29 noon-ET close is lower than the Aug 30
    noon-ET close"), not at ``startDate`` — the trading window opens up to
    ~2 days before settlement, well before the actual comparison period
    starts. Every market in this family shares one question template (only
    the asset/date change), so the caller derives ``reference_ts`` from that
    prior noon ET (``updown_quote.daily_reference_instant``) rather than
    trusting ``startDate``. Noon ET is wall-clock: subtracting a flat 86400s
    is right 363 days a year and an hour wrong on the two DST switch days.
    """
    try:
        resp = await client.get(
            f"{_config.BINANCE_API_BASE}/api/v3/klines",
            params={
                "symbol": symbol,
                "interval": "1m",
                "startTime": ts * 1000,
                "limit": 1,
            },
        )
        resp.raise_for_status()
        rows = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not rows:
        return None
    return float(rows[0][4])


async def fetch_daily_closes(
    client: httpx.AsyncClient, symbol: str, days: int = 30
) -> list[float]:
    """Recent daily closes for the volatility/drift estimator, oldest-first."""
    try:
        resp = await client.get(
            f"{_config.BINANCE_API_BASE}/api/v3/klines",
            params={"symbol": symbol, "interval": "1d", "limit": days},
        )
        resp.raise_for_status()
        rows = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    return [float(r[4]) for r in rows if isinstance(r, (list, tuple)) and len(r) > 4]


async def fetch_spot(client: httpx.AsyncClient, symbol: str) -> float | None:
    """Latest spot price via REST — a 60s scan cadence has no need for a
    push-latency websocket feed."""
    try:
        resp = await client.get(
            f"{_config.BINANCE_API_BASE}/api/v3/ticker/price", params={"symbol": symbol}
        )
        resp.raise_for_status()
        return float(resp.json()["price"])
    except (httpx.HTTPError, ValueError, KeyError):
        return None


def market_prices(market: dict[str, Any]) -> tuple[float | None, float | None, float | None, float | None]:
    """Return ``(up_bid, up_ask, down_bid, down_ask)`` from a raw Gamma market row.

    Up/Down are complementary outcomes of one book: Down's ask/bid derive
    from Up's bid/ask (``down_ask = 1 - up_bid``, ``down_bid = 1 - up_ask``).
    ``bestBid``/``bestAsk`` on the Gamma row are for the first-listed outcome,
    which is confirmed "Up" for this family (``outcomes = ["Up", "Down"]``).
    """
    up_bid = market.get("bestBid")
    up_ask = market.get("bestAsk")
    up_bid = float(up_bid) if isinstance(up_bid, (int, float)) else None
    up_ask = float(up_ask) if isinstance(up_ask, (int, float)) else None
    down_bid = (1.0 - up_ask) if up_ask is not None else None
    down_ask = (1.0 - up_bid) if up_bid is not None else None
    return up_bid, up_ask, down_bid, down_ask


async def build_market_view(
    client: httpx.AsyncClient, short_asset: str, market: dict[str, Any]
) -> DailyMarketView | None:
    """Resolve one asset's raw Gamma market row into a full ``DailyMarketView``.

    Returns ``None`` if the spot feed or reference price is unavailable —
    the caller should skip this asset for the tick rather than score it on
    a fabricated read.
    """
    mt = parse_market(market)
    if mt is None:
        return None
    end_dt = _parse_dt(market.get("endDate"))
    if end_dt is None:
        return None
    end = int(end_dt.timestamp())
    remaining = max(0, end - int(datetime.now(UTC).timestamp()))
    # See fetch_close_at: the comparison runs between two noon-ET closes, not
    # [startDate, endDate] (startDate is when trading OPENED, which can be up
    # to ~2 days earlier). Noon ET is wall-clock, so on the two DST switch
    # days a year the two noons are 23h or 25h apart, never 86400s.
    reference_ts = int(daily_reference_instant(end_dt).timestamp())

    slug_asset, _family, _rung, _evidence = classify(market)
    symbol = SPOT_SYMBOL.get(slug_asset) or SPOT_SYMBOL.get(short_asset)
    if symbol is None:
        return None

    spot = await fetch_spot(client, symbol)
    reference = await fetch_close_at(client, symbol, reference_ts)
    if spot is None or reference is None:
        return None

    up_bid, up_ask, down_bid, down_ask = market_prices(market)
    market_up_price = (
        (up_bid + up_ask) / 2.0
        if up_bid is not None and up_ask is not None
        else float(json.loads(market.get("outcomePrices") or "[0.5, 0.5]")[0])
    )
    liquidity = market.get("liquidity") or market.get("liquidityNum")
    try:
        liquidity_usd = float(liquidity) if liquidity is not None else None
    except (TypeError, ValueError):
        liquidity_usd = None
    order_min_size = float(market.get("orderMinSize") or 5.0)

    return DailyMarketView(
        asset=short_asset,
        window_slug=mt.slug,
        condition_id=mt.condition_id,
        up_token=mt.up_token,
        down_token=mt.down_token,
        binance_symbol=symbol,
        resolves_at=str(market.get("endDate")),
        remaining_seconds=remaining,
        spot=spot,
        reference=reference,
        up_ask=up_ask,
        down_ask=down_ask,
        market_up_price=market_up_price,
        fair_up=0.5,  # filled in by polymarket_bot.daily.signal
        sigma_per_second=None,
        drift_per_second=None,
        liquidity_usd=liquidity_usd,
        order_min_size=order_min_size,
    )
