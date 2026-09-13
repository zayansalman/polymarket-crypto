"""Reconnecting WS loop: subscribes, feeds frames, reports every disconnected stretch as a gap."""
from __future__ import annotations

import asyncio
import json

import pytest

from polymarket_exec.connectors.ws_runner import WsStatus, run_ws_forever


class _FakeWs:
    def __init__(self, frames: list, fail_after: bool, stop_event: asyncio.Event) -> None:
        self.frames = list(frames)
        self.sent: list[str] = []
        self.fail_after = fail_after
        self.stop_event = stop_event

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def recv(self) -> str:
        if self.frames:
            return self.frames.pop(0)
        if self.fail_after:
            raise ConnectionError("socket closed")
        self.stop_event.set()
        await asyncio.sleep(0)
        return "not json"


@pytest.mark.asyncio
async def test_reconnects_reports_gaps_and_delivers_frames() -> None:
    stop = asyncio.Event()
    clock = iter(range(1000, 10_000, 10))
    connections = [
        _FakeWs([json.dumps({"n": 1})], fail_after=True, stop_event=stop),
        _FakeWs([json.dumps({"n": 2}), "[1,2]"], fail_after=False, stop_event=stop),
    ]
    made: list[_FakeWs] = []

    def connect(url: str) -> _FakeWs:
        assert url == "wss://example.test/ws"
        ws = connections.pop(0)
        made.append(ws)
        return ws

    messages: list[dict] = []
    gaps: list[tuple[int, int]] = []
    status = WsStatus()

    def on_message(msg: dict) -> bool:
        messages.append(msg)
        return msg.get("n") == 1  # only the first frame counts as data

    await asyncio.wait_for(
        run_ws_forever(
            name="t", url="wss://example.test/ws", subscribe={"op": "sub"},
            on_message=on_message, on_gap=lambda a, b: gaps.append((a, b)),
            stop_event=stop, status=status, connect=connect,
            time_fn=lambda: float(next(clock)), recv_timeout_s=1.0, initial_backoff_s=0.001,
        ),
        timeout=5,
    )
    assert messages == [{"n": 1}, {"n": 2}]  # the list frame is ignored
    assert [ws.sent for ws in made] == [['{"op": "sub"}'], ['{"op": "sub"}']]
    assert len(gaps) == 2
    assert gaps[1][0] > gaps[0][1]  # second gap starts after the first connection was up
    assert status.connected is False and status.last_error == "ConnectionError: socket closed"
    # Acks and non-data frames (here {"n": 2}) never refresh the data timestamp.
    assert status.last_data_at == 1020.0


@pytest.mark.asyncio
async def test_no_subscribe_message_when_none() -> None:
    stop = asyncio.Event()
    ws = _FakeWs([], fail_after=False, stop_event=stop)
    await asyncio.wait_for(
        run_ws_forever(
            name="t", url="wss://x", subscribe=None, on_message=lambda m: False,
            on_gap=lambda a, b: None, stop_event=stop, status=WsStatus(),
            connect=lambda url: ws, recv_timeout_s=1.0, initial_backoff_s=0.001,
        ),
        timeout=5,
    )
    assert ws.sent == []
