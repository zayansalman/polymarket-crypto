"""Reconnecting WebSocket loop shared by the venue trade feeds (Kraken, Binance liquidations)."""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Callable

from logging_setup import get_logger

log = get_logger("ws_runner")


@dataclass
class WsStatus:
    connected: bool = False
    connected_since: float | None = None
    last_data_at: float | None = None  # last frame the handler reported as live data
    last_error: str | None = None


def _default_connect(url: str) -> Any:
    import websockets

    return websockets.connect(url, open_timeout=15, ping_interval=20)


async def run_ws_forever(
    *,
    name: str,
    url: str,
    subscribe: dict[str, Any] | None,
    on_message: Callable[[dict[str, Any]], bool],
    on_gap: Callable[[int, int], None],
    stop_event: asyncio.Event,
    status: WsStatus,
    connect: Callable[[str], Any] | None = None,
    time_fn: Callable[[], float] = time.time,
    recv_timeout_s: float = 30.0,
    initial_backoff_s: float = 1.0,
    max_backoff_s: float = 60.0,
) -> None:
    """Connect, subscribe and feed JSON object frames to ``on_message`` until ``stop_event``.

    ``on_message`` returns True for a frame carrying live data; only those refresh
    ``status.last_data_at``, so subscription acks and heartbeats can't make a silent
    stream look healthy. Each disconnected stretch (including the one before the
    first connect) is reported once the socket is up again as ``on_gap(from_ms, to_ms)``.
    """
    connect = connect or _default_connect
    backoff = initial_backoff_s
    down_since_ms = int(time_fn() * 1000)
    while not stop_event.is_set():
        try:
            async with connect(url) as ws:
                if subscribe is not None:
                    await ws.send(json.dumps(subscribe))
                now = time_fn()
                on_gap(down_since_ms, int(now * 1000))
                # last_error is kept: the card shows it only while the feed is down.
                status.connected, status.connected_since = True, now
                backoff = initial_backoff_s
                while not stop_event.is_set():
                    try:
                        frame = await asyncio.wait_for(ws.recv(), timeout=recv_timeout_s)
                    except asyncio.TimeoutError:
                        continue  # quiet feeds are normal; the library's pings detect dead sockets
                    try:
                        msg = json.loads(frame)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(msg, dict) and on_message(msg):
                        status.last_data_at = time_fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — any socket error means reconnect
            status.last_error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("venue_ws.disconnected", feed=name, error=status.last_error,
                        retry_in_s=backoff)
        finally:
            if status.connected:
                down_since_ms = int(time_fn() * 1000)
            status.connected = False
        if stop_event.is_set():
            break
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
        backoff = min(backoff * 2, max_backoff_s)
