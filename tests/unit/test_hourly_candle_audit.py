"""Candle audit: the candles each hourly decision used, checked against Binance's daily archive."""
from __future__ import annotations

import io
import json
import statistics
import zipfile
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

import db as _db
from polymarket_bot.hourly import btcusdt_1h_spot_taker_push_reversal as rule
from polymarket_bot.hourly import candle_audit, ledger
from polymarket_bot.hourly.market import Candle

H = 1_789_326_000  # 2026-09-13 19:00 UTC; the rule reads H-1 = 18:00 on 2026-09-13
DAY = "2026-09-13"
DAY_END = 1_789_344_000  # 2026-09-14 00:00 UTC
SID = rule.STRATEGY_ID


def _series(last_tb_share: float, *, n: int = 170, c: float = 110.0) -> list[Candle]:
    out = []
    for i in range(n - 1):
        share = 0.55 if i % 2 else 0.45
        out.append(Candle((H - (n - i) * 3600) * 1000, 100.0, 101.0, 99.0, 100.5, 10.0, 1000.0,
                          10.0 * share))
    out.append(Candle((H - 3600) * 1000, 100.0, 110.0, 99.0, c, 10.0, 1000.0, 10.0 * last_tb_share))
    return out


SPOT, PERP = _series(0.9), _series(0.5)


def _archive_row(c: Candle, *, micro: bool) -> str:
    scale = 1000 if micro else 1
    close_time = (c.open_time_ms + 3_599_999) * scale
    return ",".join(str(x) for x in [
        c.open_time_ms * scale, c.open, c.high, c.low, c.close, c.volume, close_time,
        c.quote_volume, 7, c.taker_buy_volume, 0, 0])


def _zip(candles: list[Candle], *, spot: bool) -> bytes:
    lines = [_archive_row(c, micro=spot) for c in candles]
    if not spot:  # the USD-M futures archive has a header row, spot does not
        lines.insert(0, "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
                        "taker_buy_volume,taker_buy_quote_volume,ignore")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"BTCUSDT-1h-{DAY}.csv", "\n".join(lines) + "\n")
    return buf.getvalue()


def test_archive_urls() -> None:
    assert candle_audit.archive_url("spot", DAY) == (
        "https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1h/BTCUSDT-1h-2026-09-13.zip")
    assert candle_audit.archive_url("perp", DAY) == (
        "https://data.binance.vision/data/futures/um/daily/klines/BTCUSDT/1h/"
        "BTCUSDT-1h-2026-09-13.zip")


def test_parse_archive_reads_both_timestamp_formats() -> None:
    spot = candle_audit.parse_archive(_zip(SPOT[-3:], spot=True))
    perp = candle_audit.parse_archive(_zip(PERP[-3:], spot=False))
    assert sorted(spot) == sorted(perp) == [c.open_time_ms for c in SPOT[-3:]]
    assert spot[(H - 3600) * 1000] == SPOT[-1]
    assert perp[(H - 3600) * 1000].taker_buy_volume == pytest.approx(5.0)


def test_signal_records_the_candles_and_window_stats_it_used() -> None:
    signal = rule.decide(SPOT, PERP).signal
    assert signal["spot_h1"] == {
        "open_time_ms": SPOT[-1].open_time_ms, "open": 100.0, "high": 110.0, "low": 99.0,
        "close": 110.0, "volume": 10.0, "quote_volume": 1000.0, "taker_buy_volume": 9.0}
    imbs = [2 * c.taker_buy_volume / c.volume - 1 for c in SPOT[-rule.WINDOW:]]
    assert signal["spot_window"]["n"] == rule.WINDOW
    assert signal["spot_window"]["mean"] == pytest.approx(statistics.mean(imbs))
    assert signal["spot_window"]["sd"] == pytest.approx(statistics.stdev(imbs))
    assert signal["perp_h1"]["taker_buy_volume"] == 5.0


def test_window_swap_reproduces_the_modified_window_exactly() -> None:
    imbs = [2 * c.taker_buy_volume / c.volume - 1 for c in SPOT[-rule.WINDOW:]]
    stats = {"n": len(imbs), "mean": statistics.mean(imbs), "sd": statistics.stdev(imbs)}
    swapped = imbs[:-1] + [0.1]
    mean, sd = candle_audit.swap_last(stats, imbs[-1], 0.1)
    assert mean == pytest.approx(statistics.mean(swapped), abs=1e-12)
    assert sd == pytest.approx(statistics.stdev(swapped), abs=1e-12)


def test_recheck_matches_when_the_archive_agrees() -> None:
    d = rule.decide(SPOT, PERP)
    out = candle_audit.recheck(d.signal, SPOT[-1], PERP[-1], recorded_side=d.side)
    assert (out["fields_differ"], out["side"], out["flipped"]) == ([], "Down", False)


def test_recheck_flags_a_flip_when_archive_taker_volume_differs() -> None:
    d = rule.decide(SPOT, PERP)
    revised = replace(SPOT[-1], taker_buy_volume=5.5)  # spot push drops below 1.20
    out = candle_audit.recheck(d.signal, revised, PERP[-1], recorded_side=d.side)
    assert out["fields_differ"] == ["spot.taker_buy_volume"]
    assert out["side"] is None and out["flipped"] is True
    assert out["spot_fz"] < rule.SPOT_FZ_MIN


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    candle_audit.reset_state()
    return _db


async def _record(start: int = H, signal: dict | None = None, mode: str = "paper") -> None:
    d = rule.decide(SPOT, PERP)
    await ledger.record_decision(
        strategy_id=SID, window_slug="slug", window_start_ts=start, side=d.side,
        reason=d.reason, signal=d.signal if signal is None else signal, factors={},
        up_bid=None, up_ask=None, down_bid=None, down_ask=None, hour_open=110.0,
        mode=mode, late=False)


async def _audit_row(mode: str = "paper") -> dict:
    return await ledger.get_decision(H, SID, mode=mode)


class _Archive:
    def __init__(self, spot: list[Candle] | None = SPOT, perp: list[Candle] | None = PERP,
                 fail: bool = False) -> None:
        self.spot, self.perp, self.fail, self.requests = spot, perp, fail, 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.fail:
            raise httpx.ConnectError("down")
        url = str(request.url)
        candles = self.spot if "/spot/" in url else self.perp
        if candles is None:
            return httpx.Response(404)
        return httpx.Response(200, content=_zip(candles[-24:], spot="/spot/" in url))


async def _run(archive: _Archive, now: int) -> int:
    async with httpx.AsyncClient(transport=httpx.MockTransport(archive)) as client:
        return await candle_audit.audit_due(client, now)


@pytest.mark.asyncio
async def test_audit_matches_and_waits_until_the_archive_is_due(test_db) -> None:
    await _record()
    await _record(mode="live")
    archive = _Archive()
    assert await _run(archive, DAY_END + 3600) == 0 and archive.requests == 0  # not due yet
    assert await _run(archive, DAY_END + 7 * 3600) == 2
    row = await _audit_row()
    assert row["candle_audit"] == "MATCH" and row["candle_audited_at"]
    assert json.loads(row["candle_audit_json"])["flipped"] is False
    assert (await _audit_row("live"))["candle_audit"] == "MATCH"


@pytest.mark.asyncio
async def test_audit_flags_a_flip_and_notifies(test_db) -> None:
    await _record()
    revised = SPOT[:-1] + [replace(SPOT[-1], taker_buy_volume=5.5)]
    assert await _run(_Archive(spot=revised), DAY_END + 7 * 3600) == 1
    assert (await _audit_row())["candle_audit"] == "FLIPPED"
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM notification_feed WHERE event_type = 'hourly_candle_audit'")
        assert (await cur.fetchone())["n"] == 1


@pytest.mark.asyncio
async def test_missing_archive_retries_hourly_then_gives_up(test_db) -> None:
    await _record()
    archive = _Archive(spot=None)
    assert await _run(archive, DAY_END + 7 * 3600) == 0
    assert (await _audit_row())["candle_audit"] is None
    first = archive.requests
    assert await _run(archive, DAY_END + 7 * 3600 + 600) == 0
    assert archive.requests == first  # no second request inside the hour
    assert await _run(archive, DAY_END + 8 * 86400) == 1
    assert (await _audit_row())["candle_audit"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_rows_without_saved_candles_and_a_failing_network(test_db) -> None:
    await _record(signal={"spot_fz": 2.0})
    assert await _run(_Archive(fail=True), DAY_END + 7 * 3600) == 1
    assert (await _audit_row())["candle_audit"] == "NO_CANDLES"


@pytest.mark.asyncio
async def test_a_failing_network_never_raises(test_db) -> None:
    await _record()
    assert await _run(_Archive(fail=True), DAY_END + 7 * 3600) == 0
    assert (await _audit_row())["candle_audit"] is None
