"""Flow recorder: REST bars each pass, perp snapshots once an hour, WS aggregators rolled to bars."""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

import config as _config
import db as _db
from polymarket_exec.connectors.venue_flow import HOUR_MS
from polymarket_exec.ops import flow_recorder as fr
from polymarket_exec.storage import venue_flow_store as store

# Captured before the autouse conftest fixture swaps ``run`` for an offline stub.
_REAL_RUN = fr.FlowRecorder.run

H0 = 1_789_326_000_000
NOW_MS = H0 + 2 * HOUR_MS + 30_000  # 30 s into hour H0+2h


def _klines(symbol_close: float) -> list:
    rows = []
    for i in range(3):  # H0, H0+1h closed; H0+2h forming
        open_ms = H0 + i * HOUR_MS
        rows.append([open_ms, "1", "2", "0.5", str(symbol_close), "10", open_ms + HOUR_MS - 1,
                     "0", 5, "6", "0", "0"])
    return rows


def _handler(*, perp_status: int = 200, calls: list[str] | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if calls is not None:
            calls.append(url)
        if url.startswith(f"{_config.BINANCE_API_BASE}/api/v3/klines"):
            close = 77_000.0 if request.url.params["symbol"] == "BTCUSDT" else 3_000.0
            return httpx.Response(200, json=_klines(close))
        if url.startswith(f"{fr.BINANCE_FAPI}/fapi/v1/klines"):
            if perp_status != 200:
                return httpx.Response(perp_status, json={"code": -1})
            return httpx.Response(200, json=_klines(77_010.0))
        if url.startswith(f"{fr.BINANCE_FAPI}/fapi/v1/premiumIndex"):
            return httpx.Response(200, json={"symbol": "BTCUSDT", "markPrice": "77318.0",
                                             "indexPrice": "77346.0", "lastFundingRate": "0.0001",
                                             "nextFundingTime": H0 + 8 * HOUR_MS,
                                             "time": NOW_MS})
        if url.startswith(f"{fr.BINANCE_FAPI}/fapi/v1/openInterest"):
            return httpx.Response(200, json={"symbol": "BTCUSDT", "openInterest": "104994.8"})
        if url.startswith(f"{fr.KRAKEN_FUTURES_API}/tickers"):
            return httpx.Response(200, json={"tickers": [
                {"symbol": "PF_XBTUSD", "markPrice": 77322.0, "indexPrice": 77314.0,
                 "fundingRate": 0.8, "openInterest": 1907.3}]})
        return httpx.Response(404)

    return handle


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    return _db


def _recorder(started_ms: int = H0 + 10 * 60_000) -> fr.FlowRecorder:
    return fr.FlowRecorder(time_fn=lambda: started_ms / 1000)


@pytest.mark.asyncio
async def test_record_once_writes_closed_rest_bars_and_hourly_snapshots(test_db) -> None:
    rec = _recorder()
    calls: list[str] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler(calls=calls))) as client:
        await rec.record_once(client, NOW_MS)
        n_first = len(calls)
        await rec.record_once(client, NOW_MS + 60_000)  # same hour: no second snapshot
    btc = await store.recent_hours("binance_spot", "BTCUSDT", 10)
    assert [r["hour_start_ms"] for r in btc] == [H0 + HOUR_MS, H0]  # forming candle dropped
    assert (await store.recent_hours("binance_spot", "ETHUSDT", 10))[0]["close"] == 3_000.0
    assert (await store.recent_hours("binance_perp", "BTCUSDT", 10))[0]["close"] == 77_010.0
    assert (await store.latest_snapshot("binance_perp", "BTCUSDT"))["open_interest"] == 104994.8
    kraken = await store.latest_snapshot("kraken_futures", "PF_XBTUSD")
    assert kraken is not None and abs(kraken["funding_rate"] - 0.8 / 77314.0) < 1e-12
    assert len(calls) - n_first == 3  # second pass: only the three kline fetches
    snap = rec.snapshot()
    assert snap.feeds[fr.BINANCE_SPOT_BTC].ok is True
    assert snap.feeds[fr.BINANCE_SPOT_BTC].last_hour_ms == H0 + HOUR_MS
    assert snap.feeds[fr.BINANCE_PERP_STATE].ok is True


@pytest.mark.asyncio
async def test_a_failing_feed_is_reported_and_others_still_record(test_db) -> None:
    rec = _recorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler(perp_status=503))) as c:
        await rec.record_once(c, NOW_MS)
    feeds = rec.snapshot().feeds
    assert feeds[fr.BINANCE_PERP_BTC].ok is False
    assert "503" in (feeds[fr.BINANCE_PERP_BTC].detail or "")
    assert feeds[fr.BINANCE_SPOT_BTC].ok is True
    assert await store.recent_hours("binance_perp", "BTCUSDT", 10) == []


@pytest.mark.asyncio
async def test_ws_aggregators_roll_into_stored_bars(test_db) -> None:
    rec = _recorder(started_ms=H0 + 10 * 60_000)
    rec.aggregator(fr.KRAKEN_SPOT).add(H0 + 20 * 60_000, 77_000.0, 2.0, "buy")
    rec.aggregator(fr.BINANCE_LIQ).add(H0 + 21 * 60_000, 76_900.0, 0.5, "sell")
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler())) as client:
        await rec.record_once(client, H0 + HOUR_MS + fr.ROLL_GRACE_MS)
    kraken = await store.recent_hours("kraken_spot", "BTC/USD", 5)
    assert len(kraken) == 1 and kraken[0]["volume"] == 2.0 and kraken[0]["complete"] == 0
    liq = await store.recent_hours("binance_liq", "BTCUSDT", 5)
    assert liq[0]["volume"] == 0.5 and liq[0]["taker_buy_volume"] == 0.0
    assert rec.snapshot().feeds[fr.KRAKEN_SPOT].last_hour_ms == H0


def test_snapshot_lists_every_feed_before_any_check() -> None:
    rec = _recorder()
    feeds = rec.snapshot().feeds
    assert set(feeds) == set(fr.REST_KEYS) | set(fr.WS_KEYS)
    assert all(feeds[k].ok is None and feeds[k].kind == "rest" for k in fr.REST_KEYS)
    assert all(feeds[k].connected is False and feeds[k].kind == "ws" for k in fr.WS_KEYS)


@pytest.mark.asyncio
async def test_run_starts_ws_feeds_and_stops_cleanly(test_db) -> None:
    urls: list[str] = []

    class _Idle:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def send(self, text: str) -> None:
            pass

        async def recv(self) -> str:
            await asyncio.sleep(3600)
            return "{}"

    def connect(url: str) -> _Idle:
        urls.append(url)
        return _Idle()

    rec = fr.FlowRecorder(
        interval_s=3600, connect=connect,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler())),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(_REAL_RUN(rec, stop))
    for _ in range(50):
        if len(urls) == 3:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert sorted(urls) == sorted([fr.KRAKEN_SPOT_WS, fr.KRAKEN_FUTURES_WS, fr.BINANCE_FAPI_WS])


def test_current_registry() -> None:
    rec = _recorder()
    fr.set_current(rec)
    try:
        assert fr.current() is rec
    finally:
        fr.set_current(None)
    assert fr.current() is None
