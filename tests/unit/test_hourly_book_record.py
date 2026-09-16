"""Hourly book record: the book at 0/10/30/60/120 s after the hour opens, with a digital fair value."""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

import config as _config
import db as _db
from polymarket_bot.hourly import book_record
from polymarket_bot.hourly import market as hm

H = 1_789_326_000  # 2026-09-13 19:00 UTC
SLUG = hm.slug_for(H)
MARKET = hm.HourMarket(SLUG, SLUG, H, "up-token", "down-token")


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    book_record.reset_caches()
    return _db


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def test_fair_up_is_the_digital_option_value() -> None:
    sigma, tau = 0.005, 0.5
    expected = _phi(math.log(101.0 / 100.0) / (sigma * math.sqrt(tau)) - sigma * math.sqrt(tau) / 2)
    assert book_record.fair_up(101.0, 100.0, sigma, 1800) == pytest.approx(expected)
    assert book_record.fair_up(100.0, 100.0, sigma, 3600) < 0.5  # drift term only
    assert book_record.fair_up(100.2, 100.0, sigma, 3600) > 0.5
    assert book_record.fair_up(100.0, 100.0, sigma, 0) == 1.0  # tie resolves Up
    assert book_record.fair_up(99.9, 100.0, sigma, 0) == 0.0
    assert book_record.fair_up(101.0, 100.0, None, 1800) is None
    assert book_record.fair_up(101.0, 0.0, sigma, 1800) is None


@pytest.mark.parametrize(("elapsed", "offset"), [
    (0, 0), (9, 0), (10, 10), (29, 10), (30, 30), (59, 30), (60, 60), (119, 60),
    (120, 120), (179, 120), (180, None), (-1, None),
])
def test_offset_windows(elapsed: int, offset: int | None) -> None:
    assert book_record.offset_for(elapsed) == offset


def _book(bids: list[tuple[str, str]], asks: list[tuple[str, str]], ts: str | None = "1789326002123"):
    body = {"bids": [{"price": p, "size": q} for p, q in bids],
            "asks": [{"price": p, "size": q} for p, q in asks]}
    if ts is not None:
        body["timestamp"] = ts
    return body


@pytest.mark.asyncio
async def test_fetch_levels_returns_best_first_top_levels() -> None:
    body = _book(bids=[("0.44", "5"), ("0.45", "6"), ("0.46", "7"), ("0.47", "8")],  # worst -> best
                 asks=[("0.53", "9"), ("0.52", "10"), ("0.51", "11"), ("0.50", "12")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=body))) as client:
        levels = await book_record.fetch_levels(client, "up-token")
    assert levels == book_record.BookLevels(
        bids=[(0.47, 8.0), (0.46, 7.0), (0.45, 6.0)],
        asks=[(0.50, 12.0), (0.51, 11.0), (0.52, 10.0)],
        book_ts_ms=1789326002123,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(500))) as client:
        assert await book_record.fetch_levels(client, "up-token") is None


def _candle(open_s: int, o: float, c: float) -> hm.Candle:
    return hm.Candle(open_s * 1000, o, max(o, c), min(o, c), c, 10.0, 1000.0, 5.0)


def test_hour_sigma_is_the_sample_stdev_of_log_returns() -> None:
    candles = [_candle(H - k * 3600, 100.0, 100.0 + (k % 3)) for k in range(170, 0, -1)]
    returns = [math.log(c.close / c.open) for c in candles[-168:]]
    assert book_record.hour_sigma(candles) == pytest.approx(statistics.stdev(returns))
    assert book_record.hour_sigma(candles[-1:]) is None


class _Venue:
    """CLOB books for both tokens and Binance spot 1h klines (H-1 closed up 1%)."""

    def __init__(self) -> None:
        self.book_requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(f"{_config.POLYMARKET_CLOB_API}/book"):
            self.book_requests += 1
            if request.url.params["token_id"] == "up-token":
                return httpx.Response(200, json=_book([("0.47", "20")], [("0.49", "30")]))
            return httpx.Response(200, json=_book([("0.51", "25")], [("0.54", "15")]))
        if url.startswith(f"{_config.BINANCE_API_BASE}/api/v3/klines"):
            rows = []
            for k in range(170, 0, -1):
                o = 100.0
                c = 101.0 if k == 1 else 100.0 + (k % 3) * 0.1
                rows.append([(H - k * 3600) * 1000, str(o), str(max(o, c)), str(min(o, c)), str(c),
                             "10", (H - k * 3600) * 1000 + 3_599_999, "1000", 5, "5", "0", "0"])
            rows.append([H * 1000, "101", "101", "101", "101", "1", H * 1000 + 3_599_999,
                         "1", 1, "0.5", "0", "0"])  # forming candle, dropped
            return httpx.Response(200, json=rows)
        return httpx.Response(404)


def _snapshot(spot: float = 101.2, hour_open: float = 101.0) -> SimpleNamespace:
    return SimpleNamespace(spot_price=spot, reference_price=hour_open)


async def _rows() -> list[dict]:
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT * FROM hourly_book_snapshots ORDER BY offset_s")
        return [dict(r) for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_records_each_offset_once_with_fair_value_and_previous_hour(test_db) -> None:
    venue = _Venue()
    async with httpx.AsyncClient(transport=httpx.MockTransport(venue)) as client:
        assert await book_record.maybe_record(client, snapshot=_snapshot(), market=MARKET, now=H + 2)
        requests_after_first = venue.book_requests
        assert not await book_record.maybe_record(client, snapshot=_snapshot(), market=MARKET,
                                                  now=H + 5)
        assert venue.book_requests == requests_after_first  # no refetch inside the same window
        assert await book_record.maybe_record(client, snapshot=_snapshot(), market=MARKET, now=H + 12)
        assert not await book_record.maybe_record(client, snapshot=_snapshot(), market=MARKET,
                                                  now=H + 200)
    first, second = await _rows()
    assert (first["offset_s"], first["elapsed_s"], second["offset_s"]) == (0, 2, 10)
    assert (first["window_slug"], first["window_start_ts"]) == (SLUG, H)
    assert (first["up_best_bid"], first["up_best_ask"]) == (0.47, 0.49)
    assert (first["down_best_bid"], first["down_best_ask"]) == (0.51, 0.54)
    assert json.loads(first["up_asks_json"]) == [[0.49, 30.0]]
    assert first["up_book_ts_ms"] == 1789326002123
    assert first["mirror_gap_ask"] == pytest.approx(0.49 - (1 - 0.51))
    assert first["mirror_gap_bid"] == pytest.approx(0.47 - (1 - 0.54))
    assert first["prev_hour_return"] == pytest.approx(math.log(101.0 / 100.0))
    assert first["sigma_1h"] > 0
    assert first["prev_hour_vol_units"] == pytest.approx(
        first["prev_hour_return"] / first["sigma_1h"])
    assert first["seconds_left"] == 3598
    assert first["fair_up"] == pytest.approx(book_record.fair_up(
        101.2, 101.0, first["sigma_1h"], 3598))


@pytest.mark.asyncio
async def test_a_failing_venue_never_raises_and_writes_nothing(test_db) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
        assert not await book_record.maybe_record(client, snapshot=_snapshot(), market=MARKET,
                                                  now=H + 2)
    assert await _rows() == []


class _PreviousHourStillForming(_Venue):
    """Binance has not closed hour H-1 yet on the first call (Claude, 2026-09-16, review)."""

    def __init__(self) -> None:
        super().__init__()
        self.kline_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(f"{_config.BINANCE_API_BASE}/api/v3/klines"):
            self.kline_calls += 1
            if self.kline_calls == 1:
                rows = super().__call__(request).json()[:-1]  # H-1 is still the forming row
                return httpx.Response(200, json=rows)
        return super().__call__(request)


@pytest.mark.asyncio
async def test_candles_are_cached_only_once_the_previous_hour_has_closed(test_db) -> None:
    venue = _PreviousHourStillForming()
    async with httpx.AsyncClient(transport=httpx.MockTransport(venue)) as client:
        assert await book_record.maybe_record(client, snapshot=_snapshot(), market=MARKET, now=H + 1)
        assert await book_record.maybe_record(client, snapshot=_snapshot(), market=MARKET, now=H + 11)
    first, second = await _rows()
    assert first["prev_hour_return"] is None
    assert second["prev_hour_return"] == pytest.approx(math.log(101.0 / 100.0))


@pytest.mark.asyncio
async def test_rows_after_our_own_live_entry_are_flagged(test_db) -> None:
    from polymarket_bot.hourly import ledger

    spot_push = "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal"
    async with httpx.AsyncClient(transport=httpx.MockTransport(_Venue())) as client:
        await book_record.maybe_record(client, snapshot=_snapshot(), market=MARKET, now=H + 2)
        await ledger.record_decision(
            strategy_id=spot_push, window_slug=SLUG, window_start_ts=H, side="Down", reason="r",
            signal={}, factors={}, up_bid=None, up_ask=None, down_bid=None, down_ask=None,
            hour_open=101.0, mode="live", late=False)
        await ledger.set_action(H, spot_push, "ENTERED", mode="live")
        await book_record.maybe_record(client, snapshot=_snapshot(), market=MARKET, now=H + 12)
    first, second = await _rows()
    assert (first["own_live_order_this_hour"], second["own_live_order_this_hour"]) == (0, 1)


@pytest.mark.asyncio
async def test_a_table_from_an_earlier_build_is_upgraded(tmp_path, monkeypatch) -> None:
    """Claude, 2026-09-16, paper smoke run: the table first shipped without
    own_live_order_this_hour, and CREATE TABLE IF NOT EXISTS keeps the old table."""
    import aiosqlite

    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            "CREATE TABLE hourly_book_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " created_at TEXT NOT NULL, window_slug TEXT NOT NULL, window_start_ts INTEGER NOT NULL,"
            " offset_s INTEGER NOT NULL, elapsed_s INTEGER NOT NULL, fetched_at_ms INTEGER NOT NULL,"
            " up_best_bid REAL, up_best_ask REAL, down_best_bid REAL, down_best_ask REAL,"
            " up_bids_json TEXT, up_asks_json TEXT, down_bids_json TEXT, down_asks_json TEXT,"
            " up_book_ts_ms INTEGER, down_book_ts_ms INTEGER, mirror_gap_ask REAL,"
            " mirror_gap_bid REAL, spot REAL, hour_open REAL, sigma_1h REAL,"
            " seconds_left INTEGER, fair_up REAL, prev_hour_return REAL, prev_hour_vol_units REAL)")
        await conn.commit()
    monkeypatch.setattr(_db, "DB_PATH", path)
    await _db.init_db()
    book_record.reset_caches()
    async with httpx.AsyncClient(transport=httpx.MockTransport(_Venue())) as client:
        assert await book_record.maybe_record(client, snapshot=_snapshot(), market=MARKET, now=H + 2)
    assert (await _rows())[0]["own_live_order_this_hour"] == 0
