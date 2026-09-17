"""Always-on feed monitor: keeps the live feeds connected and checks each one.

Runs for the dashboard process's lifetime (started in ``app.py``'s lifespan),
so the FEEDS card shows real feed health whether or not the bot loop is running.

* **Chainlink BTC/USD WS** — one :class:`ChainlinkWsFeed` connection. The bot
  loop reads this same feed (``paper.set_shared_chainlink_feed``), so the card
  shows the exact stream the loop trades on, not a second copy.
* **REST feeds** — every ``interval_s`` the monitor makes the same calls the
  loop makes (Gamma market lookup, CLOB book, Chainlink crypto-price window
  open, Binance klines) and records round-trip latency, success, and the error.

Failures are logged once per OK→DOWN transition (no per-probe log spam) and
shown on the card with the error text.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

import config as _config
from logging_setup import get_logger
from polymarket_exec.connectors.chainlink_settlement import (
    FIVE_MINUTES,
    ChainlinkSettlementConnector,
    ChainlinkWsFeed,
)

log = get_logger("feed_monitor")

DEFAULT_INTERVAL_S = 10.0

GAMMA = "gamma"
CLOB_BOOK = "clob_book"
CHAINLINK_REST = "chainlink_rest"
BINANCE = "binance"


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    latency_ms: float
    checked_at: float  # wall clock, unix seconds
    detail: str | None = None  # error text, or why an answer was unusable


@dataclass(frozen=True)
class FeedsSnapshot:
    """Point-in-time view of every monitored feed, for the FEEDS card."""

    taken_at: float
    started_at: float
    interval_s: float
    ws_connected: bool
    ws_fresh: bool
    ws_print_age_s: float | None
    probes: dict[str, ProbeResult]


class FeedMonitor:
    def __init__(
        self,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        chainlink_ws: ChainlinkWsFeed | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self.interval_s = interval_s
        self.chainlink_ws = chainlink_ws or ChainlinkWsFeed(
            url=_config.POLYMARKET_LIVE_DATA_WS,
            stale_after_s=_config.CHAINLINK_STALE_SECONDS,
        )
        self._client_factory = client_factory or _default_client
        self._time_fn = time_fn
        self._started_at = time_fn()
        self._probes: dict[str, ProbeResult] = {}

    def snapshot(self) -> FeedsSnapshot:
        latest = self.chainlink_ws.latest()
        now = self._time_fn()
        return FeedsSnapshot(
            taken_at=now,
            started_at=self._started_at,
            interval_s=self.interval_s,
            ws_connected=self.chainlink_ws.is_connected(),
            ws_fresh=self.chainlink_ws.is_fresh(),
            ws_print_age_s=max(0.0, now - latest[0]) if latest else None,
            probes=dict(self._probes),
        )

    async def run(self, stop_event: asyncio.Event) -> None:
        """Hold the WS connection and probe REST feeds until ``stop_event``."""
        self._started_at = self._time_fn()
        ws_task = asyncio.create_task(self.chainlink_ws.run())
        try:
            async with self._client_factory() as client:
                while not stop_event.is_set():
                    await self.probe_once(client)
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(stop_event.wait(), timeout=self.interval_s)
        finally:
            self.chainlink_ws.stop()
            ws_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await ws_task

    async def probe_once(self, client: httpx.AsyncClient) -> None:
        """One round of REST checks. Never raises."""
        # Lazy: polymarket_bot.paper imports polymarket_exec at module load.
        from polymarket_bot import paper as _paper

        now = int(self._time_fn())
        market: dict[str, Any] | None = None

        async def gamma() -> None:
            nonlocal market
            market = await _paper._fetch_current_market(client, now)

        await self._probe(GAMMA, gamma)

        async def clob_book() -> None:
            if market is None:
                raise RuntimeError("no current market (Gamma lookup failed)")
            up_token, _down = _paper._outcome_token_ids(market)
            if not up_token:
                raise RuntimeError("market has no CLOB token ids")
            r = await client.get(
                f"{_config.POLYMARKET_CLOB_API}/book", params={"token_id": up_token}
            )
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict) or not (data.get("bids") or data.get("asks")):
                raise _Unusable("empty book")

        async def chainlink_rest() -> None:
            connector = ChainlinkSettlementConnector(
                client, api_base=_config.POLYMARKET_CRYPTO_PRICE_API
            )
            window = await connector.fetch_window(now - now % FIVE_MINUTES)
            if window.open_price is None:
                raise _Unusable("no open price for the current window")

        async def binance() -> None:
            await _paper._fetch_spot_and_recent_closes(client)

        await asyncio.gather(
            self._probe(CLOB_BOOK, clob_book),
            self._probe(CHAINLINK_REST, chainlink_rest),
            self._probe(BINANCE, binance),
        )

    async def _probe(self, key: str, check: Callable[[], Awaitable[None]]) -> None:
        t0 = time.perf_counter()
        try:
            await check()
        except Exception as exc:  # noqa: BLE001 — every failure is a feed status
            detail = str(exc) if isinstance(exc, _Unusable) else f"{type(exc).__name__}: {exc}"
            result = ProbeResult(False, _ms_since(t0), self._time_fn(), detail[:200])
        else:
            result = ProbeResult(True, _ms_since(t0), self._time_fn())
        previous = self._probes.get(key)
        if not result.ok and (previous is None or previous.ok):
            log.warning("feed_monitor.feed_down", feed=key, error=result.detail)
        elif result.ok and previous is not None and not previous.ok:
            log.info("feed_monitor.feed_recovered", feed=key)
        self._probes[key] = result


class _Unusable(Exception):
    """The feed answered, but not with anything the loop could use."""


def _ms_since(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000


def _default_client() -> httpx.AsyncClient:
    # Same client the loop uses: crypto-price 403s without HTTP/2 (#120).
    from polymarket_bot import paper as _paper

    return _paper._make_settlement_client()


# Process-wide monitor, set by the dashboard lifespan. None outside the app.
_current: FeedMonitor | None = None


def set_current(monitor: FeedMonitor | None) -> None:
    global _current
    _current = monitor


def current() -> FeedMonitor | None:
    return _current
