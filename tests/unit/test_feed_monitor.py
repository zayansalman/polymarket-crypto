"""Feed monitor: checks every live feed directly and shares its WS feed with the loop."""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import config as _config
import db as _db
from polymarket_bot import paper
from polymarket_exec.ops import feed_monitor as fm

# Captured before the autouse conftest fixture swaps ``run`` for an offline stub.
_REAL_RUN = fm.FeedMonitor.run

NOW = 1_800_000_000  # a 5-minute boundary


class _FakeWs:
    def __init__(self) -> None:
        self.ran = False
        self.stopped = False

    async def run(self) -> None:
        self.ran = True
        await asyncio.Event().wait()

    def stop(self) -> None:
        self.stopped = True

    def latest(self):
        return (NOW - 2.0, 77_000.0)

    def is_connected(self) -> bool:
        return True

    def is_fresh(self) -> bool:
        return True


def _market() -> dict:
    return {
        "slug": f"btc-updown-1h-{NOW}",
        "outcomes": json.dumps(["Up", "Down"]),
        "outcomePrices": json.dumps(["0.5", "0.5"]),
        "clobTokenIds": json.dumps(["111", "222"]),
    }


def _handler(*, gamma_status: int = 200, book: dict | None = None, open_price=77_000.0):
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(_config.POLYMARKET_GAMMA_API):
            if gamma_status != 200:
                return httpx.Response(gamma_status, json={"error": "down"})
            return httpx.Response(200, json=[_market()])
        if url.startswith(f"{_config.POLYMARKET_CLOB_API}/book"):
            assert request.url.params["token_id"] == "111"
            return httpx.Response(200, json=book if book is not None else {
                "bids": [{"price": "0.48", "size": "10"}], "asks": [{"price": "0.52", "size": "10"}],
            })
        if url.startswith(_config.POLYMARKET_CRYPTO_PRICE_API):
            assert request.url.params["eventStartTime"] == str(NOW)
            return httpx.Response(200, json={"openPrice": open_price, "closePrice": None, "completed": False})
        if url.startswith(paper.BINANCE_API):
            return httpx.Response(200, json=[[0, 0, 0, 0, "77010.5"]] * 5)
        return httpx.Response(404)

    return handle


def _monitor(**kw) -> fm.FeedMonitor:
    return fm.FeedMonitor(chainlink_ws=_FakeWs(), time_fn=lambda: float(NOW), **kw)


async def _probe(handler) -> dict[str, fm.ProbeResult]:
    monitor = _monitor()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await monitor.probe_once(client)
    return monitor.snapshot().probes


@pytest.mark.asyncio
async def test_all_feeds_ok() -> None:
    probes = await _probe(_handler())
    assert set(probes) == {fm.GAMMA, fm.CLOB_BOOK, fm.CHAINLINK_REST, fm.BINANCE}
    assert all(p.ok and p.latency_ms >= 0 for p in probes.values())


@pytest.mark.asyncio
async def test_gamma_down_takes_book_check_with_it() -> None:
    probes = await _probe(_handler(gamma_status=503))
    assert not probes[fm.GAMMA].ok and "503" in probes[fm.GAMMA].detail
    assert not probes[fm.CLOB_BOOK].ok and "Gamma" in probes[fm.CLOB_BOOK].detail
    assert probes[fm.CHAINLINK_REST].ok and probes[fm.BINANCE].ok


@pytest.mark.asyncio
async def test_unusable_answers_are_not_ok() -> None:
    probes = await _probe(_handler(book={"bids": [], "asks": []}, open_price=None))
    assert probes[fm.CLOB_BOOK].detail == "empty book"
    assert not probes[fm.CHAINLINK_REST].ok


@pytest.mark.asyncio
async def test_down_is_logged_once_per_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(fm, "log", log)
    monitor = _monitor()
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler(gamma_status=503))) as client:
        await monitor.probe_once(client)
        await monitor.probe_once(client)
    downs = [c for c in log.warning.call_args_list if c.kwargs.get("feed") == fm.GAMMA]
    assert len(downs) == 1


def test_snapshot_reports_ws_print_age() -> None:
    snap = _monitor().snapshot()
    assert (snap.ws_connected, snap.ws_fresh, snap.ws_print_age_s) == (True, True, 2.0)


@pytest.mark.asyncio
async def test_run_holds_ws_and_stops_it_on_exit() -> None:
    ws = _FakeWs()
    stop = asyncio.Event()
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler()))
    monitor = fm.FeedMonitor(
        chainlink_ws=ws, client_factory=lambda: client, time_fn=lambda: float(NOW)
    )

    async def probe_then_stop(_client) -> None:
        await asyncio.sleep(0)  # let the WS task start
        stop.set()

    monitor.probe_once = probe_then_stop  # type: ignore[method-assign]
    await asyncio.wait_for(_REAL_RUN(monitor, stop), timeout=5)
    assert ws.ran and ws.stopped


@pytest.mark.asyncio
async def test_loop_reads_shared_feed_and_leaves_it_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "shared_feed.db")
    await _db.init_db()
    shared = _FakeWs()
    monkeypatch.setattr(paper, "_shared_chainlink_feed", shared)
    monkeypatch.setattr(paper, "ChainlinkWsFeed", MagicMock(side_effect=AssertionError("second WS")))
    stop = threading.Event()
    seen = []

    async def tick():
        seen.append(paper._chainlink_feed)
        stop.set()
        return MagicMock()

    monkeypatch.setattr(paper, "paper_tick_once", tick)
    monkeypatch.setattr(paper, "_detail_from_snapshot", lambda _s: "tick")
    monkeypatch.setattr(paper, "notify", AsyncMock())

    await paper.run_paper_loop(stop, mode="paper")

    assert seen == [shared]
    assert not shared.stopped
    assert paper._chainlink_feed is None
