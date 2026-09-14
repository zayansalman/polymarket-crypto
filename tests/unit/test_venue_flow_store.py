"""Venue flow store: bars upsert idempotently and never downgrade a complete row."""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

import db as _db
from polymarket_exec.connectors.venue_flow import HOUR_MS, HourBar, VenueSnapshot
from polymarket_exec.storage import venue_flow_store as store

H0 = 1_789_326_000_000


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    return _db


def _bar(hour: int, volume: float, complete: bool) -> HourBar:
    return HourBar("kraken_spot", "BTC/USD", hour, 1.0, 2.0, 0.5, 1.5, volume, volume / 2,
                   3, complete, "ws_v2_trade")


@pytest.mark.asyncio
async def test_upsert_is_idempotent_and_keeps_the_more_complete_row(test_db) -> None:
    assert await store.upsert_hour_bars([_bar(H0, 10.0, True), _bar(H0 + HOUR_MS, 4.0, False)]) == 2
    await store.upsert_hour_bars([_bar(H0, 1.0, False)])  # must not replace the complete row
    await store.upsert_hour_bars([_bar(H0 + HOUR_MS, 5.0, True)])  # may upgrade an incomplete one
    rows = await store.recent_hours("kraken_spot", "BTC/USD", 10)
    assert [(r["hour_start_ms"], r["volume"], r["complete"]) for r in rows] == [
        (H0 + HOUR_MS, 5.0, 1),
        (H0, 10.0, 1),
    ]
    assert await store.upsert_hour_bars([]) == 0


@pytest.mark.asyncio
async def test_snapshots_latest_first(test_db) -> None:
    for ts, oi in ((H0, 100.0), (H0 + HOUR_MS, 110.0)):
        await store.insert_snapshot(VenueSnapshot("binance_perp", "BTCUSDT", ts, 1.0, 1.0,
                                                  0.0001, ts + 8 * HOUR_MS, oi, "fapi"))
    latest = await store.latest_snapshot("binance_perp", "BTCUSDT")
    assert latest is not None and latest["open_interest"] == 110.0
    assert await store.latest_snapshot("kraken_futures", "PF_XBTUSD") is None
