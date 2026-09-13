"""Background quote poller for the dashboard's order-size ticket.

Keeps one in-memory :class:`UpDownQuote` fresh for whichever market the panel
last asked about, so renders never block on the network and the ticket tracks
the operator's selection even while the trading loop is stopped.

Demand-driven: the poller only hits the venue while a dashboard is open (a
render asked within ``IDLE_AFTER_SECONDS``), and a selection change wakes it
immediately instead of waiting out the poll interval. Failed reads back off
and keep the last good prices, which the ticket then flags as stale.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from logging_setup import get_logger  # type: ignore[import-untyped]
from polymarket_exec.connectors.updown_quote import UpDownQuote, UpDownQuoteClient

POLL_SECONDS = 2.0
IDLE_AFTER_SECONDS = 30.0
MAX_BACKOFF_SECONDS = 30.0

log = get_logger("dashboard.quote_feed")

_wanted: tuple[str, str] | None = None
_last_demand = 0.0
_latest: UpDownQuote | None = None
_wake: asyncio.Event | None = None  # created inside the running loop


def snapshot(asset: str, timeframe: str) -> UpDownQuote | None:
    """Latest quote for this selection, or ``None`` until the first read lands.

    Also registers demand: the poller follows the most recent caller.
    """
    global _wanted, _last_demand
    _last_demand = time.monotonic()
    if _wanted != (asset, timeframe):
        _wanted = (asset, timeframe)
        if _wake is not None:
            _wake.set()
    if _latest is not None and (_latest.asset, _latest.timeframe) == (asset, timeframe):
        return _latest
    return None


async def run_forever(stop_event: asyncio.Event) -> None:
    global _latest, _wake
    wake = _wake = asyncio.Event()
    fails = 0
    async with httpx.AsyncClient(timeout=5.0) as http:
        quotes = UpDownQuoteClient(http)
        while not stop_event.is_set():
            wake.clear()  # before the fetch, so a selection change mid-fetch re-polls
            wanted = _wanted
            if wanted is not None and time.monotonic() - _last_demand < IDLE_AFTER_SECONDS:
                quote = await _poll(quotes, *wanted)
                fails = fails + 1 if quote.error else 0
                _latest = merge(_latest, quote)
            try:
                await asyncio.wait_for(wake.wait(), timeout=backoff_seconds(fails))
            except TimeoutError:
                pass


def merge(previous: UpDownQuote | None, quote: UpDownQuote) -> UpDownQuote:
    """A failed read keeps the last good prices for the same market.

    They age into "stale" on the ticket instead of blanking on one blip (e.g. a
    venue 429); ``no quote`` shows only when there is nothing good to fall back on.
    """
    if (
        quote.error
        and previous is not None
        and not previous.error
        and (previous.asset, previous.timeframe) == (quote.asset, quote.timeframe)
    ):
        return previous
    return quote


def backoff_seconds(consecutive_failures: int) -> float:
    """Poll interval: steady when healthy, doubling to 30s while reads fail."""
    if consecutive_failures <= 0:
        return POLL_SECONDS
    return min(MAX_BACKOFF_SECONDS, POLL_SECONDS * 2**consecutive_failures)


async def _poll(quotes: UpDownQuoteClient, asset: str, timeframe: str) -> UpDownQuote:
    try:
        quote = await quotes.fetch(asset, timeframe)
    except Exception as e:  # noqa: BLE001 — the poller must outlive any bad read
        quote = UpDownQuote(asset, timeframe, time.time(), error=f"{type(e).__name__}: {e}")
    if quote.error:
        log.warning("quote_feed.fetch_failed", asset=asset, timeframe=timeframe, error=quote.error)
    return quote
