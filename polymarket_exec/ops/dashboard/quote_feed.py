"""Background quote poller for the dashboard's order-size ticket.

Keeps one in-memory :class:`UpDownQuote` fresh for whichever market the panel
last asked about, so renders never block on the network and the ticket tracks
the operator's selection even while the trading loop is stopped.

Prices come from the market-data hub's live books: while a dashboard is open the
ticket *wants* the selected market (``owner="order ticket"``), and lets it go when
the selection changes or the dashboard closes. The REST poll stays as the
fallback — before the hub's first snapshot, while its sockets are down, and when
there is no hub in the process at all. It also stays the only way to learn a
window's ``min_order_size``, which the market channel does not carry: one read per
window, then the value is carried and the hub serves the prices.

Demand-driven: the ticket only costs anything while a dashboard is open (a
render asked within ``IDLE_AFTER_SECONDS``), and a selection change wakes the
poller immediately instead of waiting out the poll interval. Failed reads back
off and keep the last good prices, which the ticket then flags as stale.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from logging_setup import get_logger  # type: ignore[import-untyped]
from polymarket_exec.connectors.updown_quote import UpDownQuote, UpDownQuoteClient
from polymarket_exec.marketdata import hub as md_hub

POLL_SECONDS = 2.0
IDLE_AFTER_SECONDS = 30.0
MAX_BACKOFF_SECONDS = 30.0
OWNER = "order ticket"  # this consumer's name on the hub (shown on the FEEDS card)

log = get_logger("dashboard.quote_feed")

_wanted: tuple[str, str] | None = None
_last_demand = 0.0
_latest: UpDownQuote | None = None
_wake: asyncio.Event | None = None  # created inside the running loop
_demand: tuple[str, str] | None = None  # the market we hold on the hub
_min_sizes: dict[str, float] = {}  # slug -> its last REST min_order_size (one window)


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
        try:
            while not stop_event.is_set():
                wake.clear()  # before the fetch, so a selection change mid-fetch re-polls
                wanted = _wanted
                if wanted is not None and time.monotonic() - _last_demand < IDLE_AFTER_SECONDS:
                    _follow(wanted)
                    quote = from_hub(*wanted) or await _poll(quotes, *wanted)
                    fails = fails + 1 if quote.error else 0
                    _latest = merge(_latest, quote)
                else:
                    _follow(None)  # no dashboard open: stop streaming for us
                try:
                    await asyncio.wait_for(wake.wait(), timeout=backoff_seconds(fails))
                except TimeoutError:
                    pass
        finally:
            _follow(None)


def _follow(market: tuple[str, str] | None) -> None:
    """Hold exactly this market on the hub for the ticket (``None`` holds nothing)."""
    global _demand
    if market == _demand:
        return
    if _demand is not None:
        md_hub.release(OWNER)
    _demand = market
    if market is None:
        return
    try:
        md_hub.want(market[0], market[1], OWNER)
    except ValueError as e:  # a selection the hub does not carry: REST still prices it
        log.warning("quote_feed.market_not_streamed", asset=market[0],
                    timeframe=market[1], error=str(e))


def from_hub(asset: str, timeframe: str) -> UpDownQuote | None:
    """The hub's live books as a quote, or None when the REST poll has to do it.

    ``min_order_size`` is not on the market channel, so a window we have not read
    over REST yet still needs one read; after that the hub serves the prices.
    """
    hub = md_hub.current()
    if hub is None:
        return None
    quote = hub.quote(asset, timeframe)
    if quote is None or not quote.live:
        return None
    min_order_size = _min_sizes.get(quote.market.slug)
    if min_order_size is None:
        return None
    return UpDownQuote(
        asset=asset,
        timeframe=timeframe,
        fetched_at=time.time(),
        slug=quote.market.slug,
        up_ask=quote.up.best_ask,
        up_bid=quote.up.best_bid,
        down_ask=quote.down.best_ask,
        down_bid=quote.down.best_bid,
        min_order_size=min_order_size,
    )


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
    global _min_sizes
    try:
        quote = await quotes.fetch(asset, timeframe)
    except Exception as e:  # noqa: BLE001 — the poller must outlive any bad read
        quote = UpDownQuote(asset, timeframe, time.time(), error=f"{type(e).__name__}: {e}")
    if quote.error:
        log.warning("quote_feed.fetch_failed", asset=asset, timeframe=timeframe, error=quote.error)
    elif quote.slug and quote.min_order_size is not None:
        _min_sizes = {quote.slug: quote.min_order_size}  # only the live window is needed
    return quote
