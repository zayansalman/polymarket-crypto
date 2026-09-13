"""Always-on recorder for venue trade-flow feeds: Binance spot/perp/liquidations and Kraken.

Started from the dashboard lifespan (next to the feed monitor), so hour bars accrue
whether or not the bot loop runs. Each pass (``interval_s``):

* fetches the last closed 1h klines for Binance spot BTCUSDT/ETHUSDT and perp BTCUSDT;
* once per hour, samples perp state (Binance premiumIndex + openInterest, Kraken
  Futures ticker);
* rolls the live WS trade aggregators (Kraken spot, Kraken Futures, Binance
  liquidations) into bars for every hour that has ended.

Pure observation data for the hourly BTC strategy; nothing here decides or gates.
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
from polymarket_exec.connectors import venue_messages as vm
from polymarket_exec.connectors.venue_flow import (
    HourAggregator,
    hour_floor_ms,
    parse_binance_klines,
)
from polymarket_exec.connectors.ws_runner import WsStatus, run_ws_forever
from polymarket_exec.storage import venue_flow_store as store

log = get_logger("flow_recorder")

BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_FAPI_WS = "wss://fstream.binance.com/ws"
KRAKEN_SPOT_WS = "wss://ws.kraken.com/v2"
KRAKEN_FUTURES_WS = "wss://futures.kraken.com/ws/v1"
KRAKEN_FUTURES_API = "https://futures.kraken.com/derivatives/api/v3"

DEFAULT_INTERVAL_S = 60.0
# Venue trade clocks can run slightly behind ours; wait this long past an hour
# boundary before closing that hour's WS bar.
ROLL_GRACE_MS = 10_000

BINANCE_SPOT_BTC = "binance_spot:BTCUSDT"
BINANCE_SPOT_ETH = "binance_spot:ETHUSDT"
BINANCE_PERP_BTC = "binance_perp:BTCUSDT"
BINANCE_PERP_STATE = "binance_perp_state:BTCUSDT"
BINANCE_LIQ = "binance_liq:BTCUSDT"
KRAKEN_SPOT = "kraken_spot:BTC/USD"
KRAKEN_FUTURES = "kraken_futures:PF_XBTUSD"
KRAKEN_FUTURES_STATE = "kraken_futures_state:PF_XBTUSD"

# (feed key, market, symbol) for closed-kline REST bars.
_REST_BARS = (
    (BINANCE_SPOT_BTC, "spot", "BTCUSDT"),
    (BINANCE_SPOT_ETH, "spot", "ETHUSDT"),
    (BINANCE_PERP_BTC, "perp", "BTCUSDT"),
)
REST_KEYS = (BINANCE_SPOT_BTC, BINANCE_SPOT_ETH, BINANCE_PERP_BTC, BINANCE_PERP_STATE,
             KRAKEN_FUTURES_STATE)
WS_KEYS = (KRAKEN_SPOT, KRAKEN_FUTURES, BINANCE_LIQ)


@dataclass(frozen=True)
class FeedStatus:
    kind: str  # "rest" | "ws"
    ok: bool | None  # rest: last check (None = not checked yet); ws: None
    connected: bool
    connected_since: float | None
    last_event_at: float | None  # rest: last success; ws: last frame
    last_hour_ms: int | None  # newest bar written for this feed
    detail: str | None


@dataclass(frozen=True)
class FlowSnapshot:
    taken_at: float
    started_at: float
    interval_s: float
    feeds: dict[str, FeedStatus]


@dataclass(frozen=True)
class _RestState:
    ok: bool
    last_ok_at: float | None
    detail: str | None


class _Unusable(Exception):
    """The venue answered, but not with anything recordable."""


def _default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15.0)


def _liquidations(msg: dict[str, Any]) -> list[vm.Trade]:
    trade = vm.binance_liquidation(msg)
    return [trade] if trade else []


class FlowRecorder:
    def __init__(
        self,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        connect: Callable[[str], Any] | None = None,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self.interval_s = interval_s
        self._client_factory = client_factory or _default_client
        self._connect = connect
        self._time_fn = time_fn
        self._started_at = time_fn()
        self._aggs = self._new_aggregators(int(self._started_at * 1000))
        self._ws_status = {key: WsStatus() for key in WS_KEYS}
        self._rest: dict[str, _RestState] = {}
        self._last_hour: dict[str, int] = {}
        self._snapshot_hour: dict[str, int] = {}

    @staticmethod
    def _new_aggregators(started_at_ms: int) -> dict[str, HourAggregator]:
        return {
            KRAKEN_SPOT: HourAggregator(venue="kraken_spot", symbol="BTC/USD",
                                        source="ws_v2_trade", started_at_ms=started_at_ms),
            KRAKEN_FUTURES: HourAggregator(venue="kraken_futures", symbol="PF_XBTUSD",
                                           source="ws_v1_trade", started_at_ms=started_at_ms),
            BINANCE_LIQ: HourAggregator(venue="binance_liq", symbol="BTCUSDT",
                                        source="ws_forceOrder", started_at_ms=started_at_ms),
        }

    def aggregator(self, key: str) -> HourAggregator:
        return self._aggs[key]

    def snapshot(self) -> FlowSnapshot:
        feeds: dict[str, FeedStatus] = {}
        for key in REST_KEYS:
            st = self._rest.get(key)
            feeds[key] = FeedStatus(
                kind="rest",
                ok=None if st is None else st.ok,
                connected=False,
                connected_since=None,
                last_event_at=None if st is None else st.last_ok_at,
                last_hour_ms=self._last_hour.get(key),
                detail=None if st is None else st.detail,
            )
        for key in WS_KEYS:
            ws = self._ws_status[key]
            feeds[key] = FeedStatus(
                kind="ws",
                ok=None,
                connected=ws.connected,
                connected_since=ws.connected_since,
                last_event_at=ws.last_message_at,
                last_hour_ms=self._last_hour.get(key),
                detail=ws.last_error,
            )
        return FlowSnapshot(self._time_fn(), self._started_at, self.interval_s, feeds)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Hold the WS trade feeds and record every ``interval_s`` until ``stop_event``."""
        self._started_at = self._time_fn()
        self._aggs = self._new_aggregators(int(self._started_at * 1000))
        specs: list[tuple[str, str, dict[str, Any], Callable[[dict[str, Any]], list]]] = [
            (KRAKEN_SPOT, KRAKEN_SPOT_WS,
             {"method": "subscribe",
              "params": {"channel": "trade", "symbol": ["BTC/USD"], "snapshot": False}},
             vm.kraken_spot_trades),
            (KRAKEN_FUTURES, KRAKEN_FUTURES_WS,
             {"event": "subscribe", "feed": "trade", "product_ids": ["PF_XBTUSD"]},
             vm.kraken_futures_trades),
            (BINANCE_LIQ, BINANCE_FAPI_WS,
             {"method": "SUBSCRIBE", "params": ["!forceOrder@arr"], "id": 1},
             _liquidations),
        ]
        tasks = [
            asyncio.create_task(
                run_ws_forever(
                    name=key,
                    url=url,
                    subscribe=subscribe,
                    on_message=self._trade_handler(key, parse),
                    on_gap=self._aggs[key].mark_gap,
                    stop_event=stop_event,
                    status=self._ws_status[key],
                    connect=self._connect,
                    time_fn=self._time_fn,
                )
            )
            for key, url, subscribe, parse in specs
        ]
        try:
            async with self._client_factory() as client:
                while not stop_event.is_set():
                    await self.record_once(client, int(self._time_fn() * 1000))
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(stop_event.wait(), timeout=self.interval_s)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError, Exception):
                    await task

    def _trade_handler(
        self, key: str, parse: Callable[[dict[str, Any]], list]
    ) -> Callable[[dict[str, Any]], None]:
        def handle(msg: dict[str, Any]) -> None:
            agg = self._aggs[key]
            try:
                for ts_ms, price, qty, side in parse(msg):
                    agg.add(ts_ms, price, qty, side)
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("flow_recorder.bad_frame", feed=key, error=str(exc)[:200])

        return handle

    async def record_once(self, client: httpx.AsyncClient, now_ms: int) -> None:
        """One recording pass. Never raises; each feed's failure is kept for the FEEDS card."""
        for key, market, symbol in _REST_BARS:
            await self._guard(
                key,
                lambda k=key, m=market, s=symbol: self._rest_bars(client, k, m, s, now_ms),
            )
        hour = hour_floor_ms(now_ms)
        for key, take in (
            (BINANCE_PERP_STATE, self._binance_state),
            (KRAKEN_FUTURES_STATE, self._kraken_state),
        ):
            if self._snapshot_hour.get(key) != hour and await self._guard(
                key, lambda t=take: t(client, now_ms)
            ):
                self._snapshot_hour[key] = hour
        for key, agg in self._aggs.items():
            bars = agg.roll(now_ms - ROLL_GRACE_MS)
            if not bars:
                continue
            try:
                await store.upsert_hour_bars(bars)
            except Exception as exc:  # noqa: BLE001 — a DB hiccup must not stop recording
                log.warning("flow_recorder.store_failed", feed=key, error=str(exc)[:200])
            else:
                self._last_hour[key] = bars[-1].hour_start_ms

    async def _guard(self, key: str, check: Callable[[], Awaitable[None]]) -> bool:
        previous = self._rest.get(key)
        try:
            await check()
        except Exception as exc:  # noqa: BLE001 — every failure is a feed status
            detail = str(exc) if isinstance(exc, _Unusable) else f"{type(exc).__name__}: {exc}"
            if previous is None or previous.ok:
                log.warning("flow_recorder.feed_down", feed=key, error=detail[:200])
            self._rest[key] = _RestState(
                False, previous.last_ok_at if previous else None, detail[:200]
            )
            return False
        if previous is not None and not previous.ok:
            log.info("flow_recorder.feed_recovered", feed=key)
        self._rest[key] = _RestState(True, self._time_fn(), None)
        return True

    async def _rest_bars(
        self, client: httpx.AsyncClient, key: str, market: str, symbol: str, now_ms: int
    ) -> None:
        if market == "spot":
            url, venue = f"{_config.BINANCE_API_BASE}/api/v3/klines", "binance_spot"
        else:
            url, venue = f"{BINANCE_FAPI}/fapi/v1/klines", "binance_perp"
        resp = await client.get(url, params={"symbol": symbol, "interval": "1h", "limit": 3})
        resp.raise_for_status()
        bars = parse_binance_klines(resp.json(), venue=venue, symbol=symbol, now_ms=now_ms)
        if not bars:
            raise _Unusable("no closed candle returned")
        await store.upsert_hour_bars(bars)
        self._last_hour[key] = bars[-1].hour_start_ms

    async def _binance_state(self, client: httpx.AsyncClient, now_ms: int) -> None:
        premium = await client.get(
            f"{BINANCE_FAPI}/fapi/v1/premiumIndex", params={"symbol": "BTCUSDT"}
        )
        premium.raise_for_status()
        oi = await client.get(f"{BINANCE_FAPI}/fapi/v1/openInterest", params={"symbol": "BTCUSDT"})
        oi.raise_for_status()
        await store.insert_snapshot(
            vm.binance_perp_snapshot(premium.json(), oi.json(), symbol="BTCUSDT")
        )

    async def _kraken_state(self, client: httpx.AsyncClient, now_ms: int) -> None:
        resp = await client.get(f"{KRAKEN_FUTURES_API}/tickers")
        resp.raise_for_status()
        snap = vm.kraken_futures_snapshot(resp.json(), symbol="PF_XBTUSD", now_ms=now_ms)
        if snap is None:
            raise _Unusable("PF_XBTUSD missing from tickers")
        await store.insert_snapshot(snap)


# Process-wide recorder, set by the dashboard lifespan. None outside the app.
_current: FlowRecorder | None = None


def set_current(recorder: FlowRecorder | None) -> None:
    global _current
    _current = recorder


def current() -> FlowRecorder | None:
    return _current
