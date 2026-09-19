"""Chainlink, Chainlink 60 s TWAP and Binance prices from Polymarket's RTDS WebSocket.

Settlement references: 5m/15m Up/Down markets resolve on the Chainlink 60 s TWAP (since
2026-08-14); 1h/1d markets on Binance BTC_USDT. Wire facts (live-checked 2026-09-16):

* ``wss://ws-live-data.polymarket.com``, no auth, permessage-deflate. Each subscribe or
  unsubscribe is acknowledged with an empty text frame. RTDS never answers ``PING``
  (sent every 5 s as the docs ask); liveness is the 1 Hz per-symbol update flow.
* Topics: ``crypto_prices`` (Binance, ``btcusdt``...), ``crypto_prices_chainlink`` and
  ``crypto_prices_twap_sixty`` (``btc/usd``...). ``filters`` must be COMPACT JSON, and
  only ONE filter per topic+type is honoured for updates, so each topic is subscribed
  unfiltered (updates for every symbol, filtered here) plus one filtered entry per
  tracked asset, which only adds a history snapshot.
* Snapshots are ``type: "subscribe"`` frames with ``payload.data``. The Chainlink one
  arrives under topic ``crypto_prices`` (not ``_chainlink``), so the source is read from
  the symbol form: ``/usd`` is Chainlink, ``usdt`` is Binance, a twap topic is the TWAP.
* Observation stamps are whole seconds, so missing seconds show up as gaps.
* Errors come back as ``{"body": {"message": ...}, "statusCode": 4xx}`` or
  ``{"message": "Invalid request body", ...}``.
"""
from __future__ import annotations

import asyncio
import bisect
import json
import random
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from logging_setup import get_logger
from polymarket_exec.marketdata.clob_stream import (
    Backoff,
    StreamSilent,
    Throughput,
    copy_deque,
    describe_error,
    first_exit,
    is_rate_limited,
    pause,
    percentile,
    run_until_stopped,
    tls_options,
)

log = get_logger("marketdata.rtds")

RTDS_WS = "wss://ws-live-data.polymarket.com"

BINANCE = "binance"
CHAINLINK = "chainlink"
CHAINLINK_TWAP60 = "chainlink_twap60"
SOURCES = (CHAINLINK, CHAINLINK_TWAP60, BINANCE)
TOPICS = {
    BINANCE: "crypto_prices",
    CHAINLINK: "crypto_prices_chainlink",
    CHAINLINK_TWAP60: "crypto_prices_twap_sixty",
}
_SOURCE_BY_UPDATE_TOPIC = {topic: source for source, topic in TOPICS.items()}
TWAP_WINDOW_S = 60

DEFAULT_ASSETS = ("btc", "eth", "sol", "xrp", "doge", "bnb")
PING_INTERVAL_S = 5.0
SILENCE_RECONNECT_S = 30.0  # every tracked symbol updates once a second
HISTORY_POINTS = 900
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0
TICK_S = 0.5
LATENCY_SAMPLES = 512

# Frame kinds.
UPDATE = "update"
SNAPSHOT = "snapshot"
ACK = "ack"
ERROR = "error"
OTHER = "other"


@dataclass(frozen=True, slots=True)
class PricePoint:
    source: str  # binance | chainlink | chainlink_twap60
    asset: str  # btc, eth, ...
    value: float
    obs_ms: int  # observation second
    publish_ms: int | None  # when RTDS published it (live updates only)
    received_ms: int  # our clock


@dataclass(frozen=True)
class RtdsFrame:
    kind: str  # UPDATE | SNAPSHOT | ACK | ERROR | OTHER
    points: tuple[PricePoint, ...] = ()  # tracked assets only
    error: str | None = None


@dataclass(frozen=True)
class SourceStatus:
    connected: bool
    last_update_age_s: float | None  # since the newest live update arrived
    newest_obs_ms: int | None  # newest observation held, any asset
    points: int  # points held, all assets
    updates: int  # live updates accepted
    snapshots: int  # history snapshots merged
    latency_ms_p50: float | None  # receive time minus observation time
    gaps: int  # missing whole seconds inside the held history
    assets: int  # assets with at least one point
    reconnects: int
    handler_errors: int
    last_error: str | None


def symbol(source: str, asset: str) -> str:
    return f"{asset}usdt" if source == BINANCE else f"{asset}/usd"


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def subscribe_message(assets: Iterable[str]) -> str:
    """Unfiltered updates per topic, plus one compact-filtered entry per asset (history)."""
    assets = tuple(assets)
    subscriptions: list[dict[str, str]] = []
    for source in (BINANCE, CHAINLINK, CHAINLINK_TWAP60):
        topic = TOPICS[source]
        subscriptions.append({"topic": topic, "type": "update"})
        subscriptions.extend(
            {"topic": topic, "type": "update",
             "filters": _compact({"symbol": symbol(source, asset)})}
            for asset in assets
        )
    return _compact({"action": "subscribe", "subscriptions": subscriptions})


def _asset(source: str, sym: Any) -> str | None:
    if not isinstance(sym, str):
        return None
    suffix = "usdt" if source == BINANCE else "/usd"
    return sym[: -len(suffix)] if sym.endswith(suffix) else None


def _snapshot_source(topic: Any, payload: dict[str, Any]) -> str | None:
    if topic == TOPICS[CHAINLINK_TWAP60]:
        return CHAINLINK_TWAP60 if payload.get("window_s", TWAP_WINDOW_S) == TWAP_WINDOW_S \
            else None
    if not isinstance(topic, str) or "twap" in topic:
        return None  # another TWAP window
    sym = payload.get("symbol")
    if isinstance(sym, str) and sym.endswith("/usd"):
        return CHAINLINK  # arrives under crypto_prices
    if isinstance(sym, str) and sym.endswith("usdt"):
        return BINANCE
    return None


def _error_text(msg: dict[str, Any]) -> str:
    body = msg.get("body")
    detail = body.get("message") if isinstance(body, dict) else msg.get("message")
    if detail is None:
        detail = _compact(body if body is not None else msg)
    status = msg.get("statusCode")
    return (f"{status}: {detail}" if status is not None else str(detail))[:200]


def parse_rtds_frame(text: str | bytes, received_ms: int, assets: frozenset[str]) -> RtdsFrame:
    """Classify one RTDS frame and normalise its prices (pure)."""
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", "replace")
    if not text.strip():
        return RtdsFrame(ACK)
    try:
        msg = json.loads(text)
    except ValueError:
        return RtdsFrame(OTHER)
    if not isinstance(msg, dict):
        return RtdsFrame(OTHER)
    if "statusCode" in msg or ("message" in msg and "topic" not in msg):
        return RtdsFrame(ERROR, error=_error_text(msg))
    payload = msg.get("payload")
    if not isinstance(payload, dict):
        return RtdsFrame(OTHER)
    try:
        if msg.get("type") == "update":
            return _update(msg, payload, received_ms, assets)
        if msg.get("type") == "subscribe":
            return _snapshot(msg, payload, received_ms, assets)
    except (KeyError, TypeError, ValueError):
        return RtdsFrame(OTHER)
    return RtdsFrame(OTHER)


def _update(msg: dict[str, Any], payload: dict[str, Any], received_ms: int,
            assets: frozenset[str]) -> RtdsFrame:
    source = _SOURCE_BY_UPDATE_TOPIC.get(msg.get("topic"))
    if source is None:
        return RtdsFrame(OTHER)
    if source == CHAINLINK_TWAP60 and payload.get("window_s", TWAP_WINDOW_S) != TWAP_WINDOW_S:
        return RtdsFrame(OTHER)
    value = float(payload["value"])
    obs_ms = int(payload["timestamp"])
    publish = msg.get("timestamp")
    asset = _asset(source, payload.get("symbol"))
    if asset is None or asset not in assets:
        return RtdsFrame(UPDATE)  # still proves the stream flows
    point = PricePoint(source, asset, value, obs_ms,
                       None if publish is None else int(publish), received_ms)
    return RtdsFrame(UPDATE, (point,))


def _snapshot(msg: dict[str, Any], payload: dict[str, Any], received_ms: int,
              assets: frozenset[str]) -> RtdsFrame:
    source = _snapshot_source(msg.get("topic"), payload)
    data = payload.get("data")
    if source is None or not isinstance(data, list):
        return RtdsFrame(OTHER)
    asset = _asset(source, payload.get("symbol"))
    if asset is None or asset not in assets:
        return RtdsFrame(SNAPSHOT)
    points = []
    for item in data:
        try:
            points.append(PricePoint(source, asset, float(item["value"]),
                                     int(item["timestamp"]), None, received_ms))
        except (KeyError, TypeError, ValueError):
            continue
    return RtdsFrame(SNAPSHOT, tuple(points))


def _obs(point: PricePoint) -> int:
    return point.obs_ms


def _missing_s(older: PricePoint, newer: PricePoint) -> int:
    return max(0, (newer.obs_ms - older.obs_ms) // 1000 - 1)


class _Series:
    """One (source, asset) history, oldest first, at most ``maxlen`` points."""

    __slots__ = ("points", "gap_s", "_maxlen")

    def __init__(self, maxlen: int) -> None:
        self._maxlen = maxlen
        self.points: deque[PricePoint] = deque(maxlen=maxlen)
        self.gap_s = 0  # missing seconds between held points

    def newest(self) -> PricePoint | None:
        try:
            return self.points[-1]
        except IndexError:
            return None

    def add(self, point: PricePoint) -> bool:
        """True when ``point`` is new and held."""
        points = self.points
        if points and point.obs_ms <= points[-1].obs_ms:
            if point.obs_ms == points[-1].obs_ms:
                return False
            return self.merge((point,)) > 0
        if points and self._maxlen > 1:
            if len(points) == self._maxlen:  # the oldest point falls out
                self.gap_s -= _missing_s(points[0], points[1])
            self.gap_s += _missing_s(points[-1], point)
        points.append(point)
        return True

    def merge(self, new: Iterable[PricePoint]) -> int:
        """Merge points by observation time (held points win). Returns how many were kept."""
        by_obs = {p.obs_ms: p for p in self.points}
        fresh: set[int] = set()
        for p in new:
            if p.obs_ms not in by_obs:
                by_obs[p.obs_ms] = p
                fresh.add(p.obs_ms)
        if not fresh:
            return 0
        kept = sorted(by_obs.values(), key=_obs)[-self._maxlen:]
        self.points = deque(kept, maxlen=self._maxlen)  # readers see the old or new deque
        self.gap_s = sum(_missing_s(a, b) for a, b in zip(kept, kept[1:]))
        return sum(1 for p in kept if p.obs_ms in fresh)


@dataclass
class _SourceStats:
    updates: int = 0
    snapshots: int = 0
    handler_errors: int = 0
    last_update_at: float | None = None
    latency: deque[int] = field(default_factory=lambda: deque(maxlen=LATENCY_SAMPLES))


class RtdsPriceStream:
    """One RTDS connection holding the three reference prices for the tracked assets.

    Reads (``latest``, ``history``, ``status``) are safe from other threads.
    """

    def __init__(
        self,
        assets: Iterable[str] = DEFAULT_ASSETS,
        *,
        on_point: Callable[[PricePoint], None] | None = None,
        url: str = RTDS_WS,
        connect: Callable[[str], Any] | None = None,
        time_fn: Callable[[], float] = time.time,
        ping_interval_s: float = PING_INTERVAL_S,
        silence_reconnect_s: float = SILENCE_RECONNECT_S,
        history_points: int = HISTORY_POINTS,
        initial_backoff_s: float = INITIAL_BACKOFF_S,
        max_backoff_s: float = MAX_BACKOFF_S,
        tick_s: float = TICK_S,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.assets = tuple(dict.fromkeys(assets))
        self.url = url
        self._assets = frozenset(self.assets)
        self._on_point = on_point
        self._connect = connect or _default_connect
        self._time_fn = time_fn
        self._ping_interval_s = ping_interval_s
        self._silence_s = silence_reconnect_s
        self._backoff = Backoff(initial_backoff_s, max_backoff_s, rng)
        self._tick_s = tick_s
        maxlen = max(1, int(history_points))
        self._series = {(s, a): _Series(maxlen) for s in SOURCES for a in self.assets}
        self._latest: dict[tuple[str, str], PricePoint] = {}
        self._stats = {s: _SourceStats() for s in SOURCES}
        self._connected = False
        self._quiet_since = 0.0
        self._last_ping_at = 0.0
        self._session_had_data = False
        self._reconnects = 0
        self._acks = 0
        self._other = 0
        self._bytes_total = 0
        self._throughput = Throughput()
        self._last_error: str | None = None

    @property
    def acks(self) -> int:
        return self._acks

    @property
    def bytes_total(self) -> int:
        """Characters received (the frames are ASCII JSON, after decompression)."""
        return self._bytes_total

    def bytes_per_s(self) -> float:
        """Characters received per second over the last 10 whole seconds."""
        return self._throughput.per_second(self._time_fn())[1]

    def latest(self, source: str, asset: str) -> PricePoint | None:
        return self._latest.get((source, asset))

    def history(self, source: str, asset: str, seconds: float | None = None
                ) -> tuple[PricePoint, ...]:
        """Held points, oldest first; with ``seconds``, only those observed that recently."""
        series = self._series.get((source, asset))
        if series is None:
            return ()
        points = copy_deque(series.points)
        if seconds is None:
            return points
        cutoff = int(self._time_fn() * 1000) - int(seconds * 1000)
        return points[bisect.bisect_left(points, cutoff, key=_obs):]

    def status(self) -> dict[str, SourceStatus]:
        now = self._time_fn()
        out: dict[str, SourceStatus] = {}
        for source in SOURCES:
            stats = self._stats[source]
            held = [self._series[(source, a)] for a in self.assets]
            newest = [p.obs_ms for p in (s.newest() for s in held) if p is not None]
            out[source] = SourceStatus(
                connected=self._connected,
                last_update_age_s=(None if stats.last_update_at is None
                                   else max(0.0, now - stats.last_update_at)),
                newest_obs_ms=max(newest) if newest else None,
                points=sum(len(s.points) for s in held),
                updates=stats.updates,
                snapshots=stats.snapshots,
                latency_ms_p50=percentile(sorted(copy_deque(stats.latency)), 0.5),
                gaps=sum(s.gap_s for s in held),
                assets=len(newest),
                reconnects=self._reconnects,
                handler_errors=stats.handler_errors,
                last_error=self._last_error,
            )
        return out

    def handle_frame(self, text: str | bytes) -> None:
        """Apply one received frame (synchronous; also used by tests and replays)."""
        now = self._time_fn()
        self._bytes_total += len(text)
        self._throughput.add(now, 1, len(text))
        frame = parse_rtds_frame(text, int(now * 1000), self._assets)
        if frame.kind == UPDATE:
            self._quiet_since = now
            self._session_had_data = True
            for point in frame.points:
                self._accept(point, now)
        elif frame.kind == SNAPSHOT:
            self._merge(frame.points)
        elif frame.kind == ACK:
            self._acks += 1
        elif frame.kind == ERROR:
            self._last_error = frame.error
            log.warning("marketdata.rtds_error", error=frame.error)
        else:
            self._other += 1

    def _accept(self, point: PricePoint, now: float) -> None:
        key = (point.source, point.asset)
        series = self._series[key]
        if not series.add(point):
            return
        stats = self._stats[point.source]
        stats.updates += 1
        stats.last_update_at = now
        stats.latency.append(point.received_ms - point.obs_ms)
        newest = series.newest()
        if newest is not None:
            self._latest[key] = newest
        if self._on_point is None:
            return
        try:
            self._on_point(point)
        except Exception as exc:  # noqa: BLE001 — a consumer bug must not drop prices
            stats.handler_errors += 1
            if stats.handler_errors <= 5:
                log.warning("marketdata.rtds_handler_failed", error=describe_error(exc))

    def _merge(self, points: tuple[PricePoint, ...]) -> None:
        groups: dict[tuple[str, str], list[PricePoint]] = {}
        for point in points:
            groups.setdefault((point.source, point.asset), []).append(point)
        for key, group in groups.items():
            series = self._series[key]
            series.merge(group)
            self._stats[key[0]].snapshots += 1
            newest = series.newest()
            if newest is not None:
                self._latest[key] = newest

    async def run(self, stop_event: asyncio.Event) -> None:
        """Hold the connection until ``stop_event``; never raises (except cancellation)."""
        while not stop_event.is_set():
            self._session_had_data = False
            rate_limited = False
            try:
                await run_until_stopped(self._serve(), stop_event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — any socket error means reconnect
                self._last_error = describe_error(exc)
                rate_limited = is_rate_limited(exc)
                log.warning("marketdata.rtds_disconnected", error=self._last_error)
            finally:
                self._connected = False
            if stop_event.is_set():
                break
            self._reconnects += 1
            delay = self._backoff.delay(had_data=self._session_had_data,
                                        rate_limited=rate_limited)
            await pause(stop_event, delay)

    async def _serve(self) -> None:
        async with self._connect(self.url) as ws:
            now = self._time_fn()
            self._quiet_since = self._last_ping_at = now
            await ws.send(subscribe_message(self.assets))
            self._connected = True
            log.info("marketdata.rtds_connected", assets=len(self.assets))
            await first_exit(self._read(ws), self._housekeep(ws))

    async def _read(self, ws: Any) -> None:
        handle = self.handle_frame
        while True:
            handle(await ws.recv())

    async def _housekeep(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(self._tick_s)
            now = self._time_fn()
            if now - self._last_ping_at >= self._ping_interval_s:
                self._last_ping_at = now
                await ws.send("PING")  # never answered; the docs ask for it
            silent_for = now - self._quiet_since
            if silent_for >= self._silence_s:
                raise StreamSilent(f"no price update for {silent_for:.0f}s")


def _default_connect(url: str) -> Any:
    import websockets

    # RTDS negotiates permessage-deflate, so compression stays on (the default). The TLS
    # context is the one the market-channel sockets share.
    return websockets.connect(url, open_timeout=15, ping_interval=None, max_queue=1024,
                              close_timeout=1, **tls_options(url))
