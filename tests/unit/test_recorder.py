"""Unit tests for MarketDataRecorder — SQLite storage and retrieval."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from polymarket_exec.core.types import (
    MarketWindow,
    Side,
    Signal,
    SignalAction,
    Tick,
)
from polymarket_exec.storage.recorder import MarketDataRecorder


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def recorder(tmp_path: Path) -> MarketDataRecorder:
    """An initialised MarketDataRecorder backed by a temp SQLite file."""
    db_path = tmp_path / "test_market.db"
    r = MarketDataRecorder(db_path=db_path)
    await r.init()
    yield r
    await r.close()


@pytest.fixture
def sample_window() -> MarketWindow:
    return MarketWindow(
        slug="btc-updown-5m-1700000000",
        question="Bitcoin Up or Down - Dec 14, 2023?",
        start_ts=1700000000,
        end_ts=1700000300,
        up_price=0.52,
        down_price=0.48,
    )


@pytest.fixture
def sample_tick(sample_window: MarketWindow) -> Tick:
    return Tick(
        ts=datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc),
        window=sample_window,
        spot_price=50000.0,
        reference_price=49900.0,
        sigma_per_second=0.0002,
        fair_up_prob=0.55,
        signal=Signal(
            action=SignalAction.ENTER_UP,
            side=Side.UP,
            confidence=0.65,
            notional_usd=2.5,
            edge=0.05,
            fair_up_prob=0.55,
            reason="enter Up: edge +0.050",
        ),
        feed_source="binance",
    )


# ---------------------------------------------------------------------------
# Table initialisation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_creates_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    r = MarketDataRecorder(db_path=db_path)
    await r.init()

    # Verify tables exist by running a query
    assert r._db is not None
    cursor = await r._db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    rows = await cursor.fetchall()
    table_names = {row[0] for row in rows}
    assert "recorded_windows" in table_names
    assert "recorded_ticks" in table_names
    assert "clob_snapshots" in table_names
    await r.close()


@pytest.mark.asyncio
async def test_init_creates_indices(tmp_path: Path) -> None:
    db_path = tmp_path / "idx.db"
    r = MarketDataRecorder(db_path=db_path)
    await r.init()
    assert r._db is not None
    cursor = await r._db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )
    rows = await cursor.fetchall()
    index_names = {row[0] for row in rows}
    assert "idx_ticks_window" in index_names
    assert "idx_ticks_ts" in index_names
    assert "idx_clob_window" in index_names
    await r.close()


# ---------------------------------------------------------------------------
# Record / retrieve windows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_and_retrieve_window(
    recorder: MarketDataRecorder, sample_window: MarketWindow
) -> None:
    await recorder.record_window(sample_window)
    windows = await recorder.get_recorded_windows(1700000000, 1700000000)
    assert len(windows) == 1
    assert windows[0].slug == sample_window.slug
    assert windows[0].question == sample_window.question
    assert windows[0].up_price == pytest.approx(0.52)


@pytest.mark.asyncio
async def test_record_window_upsert(
    recorder: MarketDataRecorder, sample_window: MarketWindow
) -> None:
    """Recording the same window twice should update (upsert) it."""
    await recorder.record_window(sample_window)
    modified = MarketWindow(
        slug=sample_window.slug,
        question=sample_window.question,
        start_ts=sample_window.start_ts,
        end_ts=sample_window.end_ts,
        up_price=0.99,  # changed
        down_price=0.01,
    )
    await recorder.record_window(modified)
    windows = await recorder.get_recorded_windows(1700000000, 1700000000)
    assert len(windows) == 1
    assert windows[0].up_price == pytest.approx(0.99)


@pytest.mark.asyncio
async def test_get_windows_by_date_range(
    recorder: MarketDataRecorder,
) -> None:
    for i in range(5):
        w = MarketWindow(
            slug=f"btc-{1700000000 + i * 300}",
            question=f"Q{i}",
            start_ts=1700000000 + i * 300,
            end_ts=1700000300 + i * 300,
            up_price=0.5,
            down_price=0.5,
        )
        await recorder.record_window(w)

    # Range that captures windows 0-2
    windows = await recorder.get_recorded_windows(1700000000, 1700000600)
    assert len(windows) == 3
    slugs = [w.slug for w in windows]
    assert slugs == ["btc-1700000000", "btc-1700000300", "btc-1700000600"]


@pytest.mark.asyncio
async def test_get_windows_empty_range(
    recorder: MarketDataRecorder,
) -> None:
    windows = await recorder.get_recorded_windows(1, 100)
    assert windows == []


@pytest.mark.asyncio
async def test_window_count(
    recorder: MarketDataRecorder, sample_window: MarketWindow
) -> None:
    assert await recorder.get_window_count() == 0
    await recorder.record_window(sample_window)
    assert await recorder.get_window_count() == 1


# ---------------------------------------------------------------------------
# Record / retrieve ticks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_and_retrieve_tick(
    recorder: MarketDataRecorder,
    sample_window: MarketWindow,
    sample_tick: Tick,
) -> None:
    await recorder.record_window(sample_window)
    await recorder.record_tick(sample_tick)
    ticks = await recorder.get_ticks_for_window(sample_window.slug)
    assert len(ticks) == 1
    assert ticks[0].spot_price == pytest.approx(50000.0)
    assert ticks[0].signal.action == SignalAction.ENTER_UP
    assert ticks[0].signal.side == Side.UP


@pytest.mark.asyncio
async def test_record_multiple_ticks(
    recorder: MarketDataRecorder, sample_window: MarketWindow
) -> None:
    await recorder.record_window(sample_window)
    for i in range(5):
        tick = Tick(
            ts=datetime(2023, 11, 14, 22, 13, 20 + i, tzinfo=timezone.utc),
            window=sample_window,
            spot_price=50000.0 + i * 100,
            reference_price=49900.0,
            sigma_per_second=0.0002,
            fair_up_prob=0.5 + i * 0.01,
            signal=Signal(
                action=SignalAction.SKIP,
                side=None,
                confidence=0.5,
                notional_usd=0.0,
                edge=0.0,
                fair_up_prob=0.5,
                reason="test",
            ),
            feed_source="binance",
        )
        await recorder.record_tick(tick)

    ticks = await recorder.get_ticks_for_window(sample_window.slug)
    assert len(ticks) == 5
    # Verify ordering
    prices = [t.spot_price for t in ticks]
    assert prices == [50000.0, 50100.0, 50200.0, 50300.0, 50400.0]


@pytest.mark.asyncio
async def test_get_ticks_for_missing_window(
    recorder: MarketDataRecorder,
) -> None:
    ticks = await recorder.get_ticks_for_window("nonexistent")
    assert ticks == []


@pytest.mark.asyncio
async def test_tick_count(
    recorder: MarketDataRecorder,
    sample_window: MarketWindow,
    sample_tick: Tick,
) -> None:
    assert await recorder.get_tick_count() == 0
    await recorder.record_window(sample_window)
    await recorder.record_tick(sample_tick)
    assert await recorder.get_tick_count() == 1


# ---------------------------------------------------------------------------
# CLOB snapshots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_and_retrieve_clob(
    recorder: MarketDataRecorder, sample_window: MarketWindow
) -> None:
    await recorder.record_window(sample_window)
    await recorder.record_clob_snapshot(
        window_slug=sample_window.slug,
        bid=0.51,
        ask=0.53,
        spread=0.02,
        timestamp=1700000010,
        freshness_ms=100,
    )
    clobs = await recorder.get_clob_for_window(sample_window.slug)
    assert len(clobs) == 1
    assert clobs[0]["bid"] == pytest.approx(0.51)
    assert clobs[0]["ask"] == pytest.approx(0.53)
    assert clobs[0]["spread"] == pytest.approx(0.02)
    assert clobs[0]["freshness_ms"] == 100


@pytest.mark.asyncio
async def test_record_multiple_clobs(
    recorder: MarketDataRecorder, sample_window: MarketWindow
) -> None:
    await recorder.record_window(sample_window)
    for i in range(3):
        await recorder.record_clob_snapshot(
            window_slug=sample_window.slug,
            bid=0.50 + i * 0.01,
            ask=0.54 - i * 0.01,
            spread=0.04 - i * 0.02,
            timestamp=1700000010 + i,
            freshness_ms=50 + i * 10,
        )
    clobs = await recorder.get_clob_for_window(sample_window.slug)
    assert len(clobs) == 3


# ---------------------------------------------------------------------------
# Coverage report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coverage_report_empty(recorder: MarketDataRecorder) -> None:
    report = await recorder.get_coverage_report()
    assert report["total_windows"] == 0
    assert report["total_ticks"] == 0
    assert report["windows_without_ticks"] == 0


@pytest.mark.asyncio
async def test_coverage_report_with_data(
    recorder: MarketDataRecorder,
    sample_window: MarketWindow,
    sample_tick: Tick,
) -> None:
    await recorder.record_window(sample_window)
    await recorder.record_tick(sample_tick)
    report = await recorder.get_coverage_report()
    assert report["total_windows"] == 1
    assert report["total_ticks"] == 1
    assert report["windows_without_ticks"] == 0
    assert report["time_range"]["start_ts"] == 1700000000
    assert report["time_range"]["end_ts"] == 1700000300


@pytest.mark.asyncio
async def test_coverage_report_window_without_ticks(
    recorder: MarketDataRecorder,
    sample_window: MarketWindow,
) -> None:
    await recorder.record_window(sample_window)
    report = await recorder.get_coverage_report()
    assert report["total_windows"] == 1
    assert report["total_ticks"] == 0
    assert report["windows_without_ticks"] == 1


# ---------------------------------------------------------------------------
# Round-trip via JSON fixtures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_roundtrip_with_fixture_data(
    recorder: MarketDataRecorder, tmp_path: Path
) -> None:
    """Load fixtures and verify round-trip storage."""
    fixture_dir = Path(__file__).parent.parent / "fixtures"

    # Load window
    window_data = json.loads((fixture_dir / "market_window.json").read_text())
    window = MarketWindow(**window_data)
    await recorder.record_window(window)

    # Load ticks
    ticks_data = json.loads((fixture_dir / "tick_series.json").read_text())
    for td in ticks_data:
        tick = _tick_from_dict(td, window)
        await recorder.record_tick(tick)

    # Verify
    stored_windows = await recorder.get_recorded_windows(1700000000, 1700000300)
    assert len(stored_windows) == 1
    stored_ticks = await recorder.get_ticks_for_window(window.slug)
    assert len(stored_ticks) == len(ticks_data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tick_from_dict(d: dict, window: MarketWindow) -> Tick:
    side = Side.UP if d["signal_side"] == "Up" else Side.DOWN if d["signal_side"] == "Down" else None
    action = SignalAction[d["signal_action"]]
    signal = Signal(
        action=action,
        side=side,
        confidence=d["signal_confidence"],
        notional_usd=d["signal_notional"],
        edge=d["signal_edge"],
        fair_up_prob=d["fair_up_prob"],
        reason=f"fixture: {action.name}",
    )
    return Tick(
        ts=datetime.fromisoformat(d["ts"]),
        window=window,
        spot_price=d["spot_price"],
        reference_price=d["reference_price"],
        sigma_per_second=d["sigma_per_second"],
        fair_up_prob=d["fair_up_prob"],
        signal=signal,
        feed_source=d["feed_source"],
    )
