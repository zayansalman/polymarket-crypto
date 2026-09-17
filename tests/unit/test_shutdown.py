"""SIGTERM must stop the dashboard even while a browser tab is open.

An open tab holds the live-update stream (/api/stream) open forever. uvicorn
waits for every connection to close before running lifespan teardown, so
without a bounded grace period the process never exits and keeps holding
data/bot.lock — a restarted main.py then refuses to start.
"""
from __future__ import annotations

import asyncio
import socket

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

import main


async def _endless_stream(request):
    async def events():
        while True:
            yield "data: {}\n\n"
            await asyncio.sleep(0.05)

    return StreamingResponse(events(), media_type="text/event-stream")


@pytest.mark.asyncio
async def test_shutdown_finishes_with_an_open_event_stream() -> None:
    options = main.dashboard_server_options()
    grace = options.get("timeout_graceful_shutdown")
    config = uvicorn.Config(
        Starlette(routes=[Route("/stream", _endless_stream)]),
        log_level="warning",
        lifespan="off",
        timeout_graceful_shutdown=grace,
    )
    server = uvicorn.Server(config)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    writer = None
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /stream HTTP/1.1\r\nHost: test\r\n\r\n")
        await writer.drain()
        assert b"200" in await reader.readline()  # stream is open

        server.should_exit = True  # what uvicorn's SIGTERM handler does
        await asyncio.wait_for(asyncio.shield(serving), timeout=(grace or 0) + 3)
    finally:
        server.force_exit = True
        if writer is not None:
            writer.close()
        await serving
        sock.close()
