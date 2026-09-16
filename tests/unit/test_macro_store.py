"""Macro store: schedule sync marks vanished future slots removed; consensus keeps first-seen times."""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

import db as _db
from polymarket_exec.connectors.macro_calendar import ConsensusRow, MacroEvent
from polymarket_exec.storage import macro_store as store

DAY = 86_400_000
NOW = 1_789_400_000_000  # 2026-09-14


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    return _db


def _ev(title: str, at: int, source: str = "bls", category: str = "cpi",
        period: str | None = None) -> MacroEvent:
    return MacroEvent(source, category, title, at, period)


async def _rows(source: str | None = None) -> list[dict]:
    rows = await store.events_between(0, 2**62, include_removed=True)
    return [r for r in rows if source is None or r["source"] == source]


@pytest.mark.asyncio
async def test_sync_upserts_idempotently(test_db) -> None:
    events = [_ev("Consumer Price Index", NOW + 2 * DAY), _ev("Employment Situation", NOW + DAY,
                                                              category="nfp")]
    first = await store.sync_events("bls", events, NOW)
    assert (first.seen, first.removed) == (2, 0)
    await store.sync_events("bls", [_ev("Consumer Price Index", NOW + 2 * DAY, period="Aug 2026"),
                                    events[1]], NOW + 60_000)
    rows = await _rows()
    assert [(r["title"], r["status"]) for r in rows] == [
        ("Employment Situation", "scheduled"), ("Consumer Price Index", "scheduled")]
    cpi = rows[1]
    assert (cpi["first_seen_ms"], cpi["last_seen_ms"], cpi["reference_period"]) == (
        NOW, NOW + 60_000, "Aug 2026")
    assert cpi["recorded_at"]
    assert (await store.sync_events("bls", [], NOW)).seen == 0


@pytest.mark.asyncio
async def test_reschedule_removes_only_future_in_range_rows_of_that_source(test_db) -> None:
    await store.sync_events("bls", [
        _ev("Past release", NOW - DAY),
        _ev("Consumer Price Index", NOW + 2 * DAY),
        _ev("Producer Price Index", NOW + 3 * DAY, category="ppi"),
        _ev("Far future", NOW + 90 * DAY),
    ], NOW - 2 * DAY)
    await store.sync_events("bea", [_ev("GDP (Advance Estimate)", NOW + 2 * DAY, source="bea",
                                        category="gdp")], NOW - 2 * DAY)
    # CPI moved a day later; the pull no longer carries the past row or the far-future row.
    later = NOW + 60_000
    result = await store.sync_events("bls", [
        _ev("Consumer Price Index", NOW + 3 * DAY),
        _ev("Producer Price Index", NOW + 3 * DAY, category="ppi"),
        _ev("Next month", NOW + 30 * DAY),
    ], later)
    assert (result.seen, result.removed) == (3, 0)  # old CPI is before this pull's range
    result = await store.sync_events("bls", [
        _ev("Consumer Price Index", NOW + 3 * DAY),
        _ev("Producer Price Index", NOW + 3 * DAY, category="ppi"),
        _ev("Next month", NOW + 30 * DAY),
        _ev("Just ahead", NOW + DAY),
        _ev("Older past", NOW - 2 * DAY),  # stretches the range over the past row below
    ], later + 1)
    assert (result.seen, result.removed) == (5, 1)
    status = {(r["title"], r["scheduled_at_ms"]): r["status"] for r in await _rows("bls")}
    assert status == {
        ("Older past", NOW - 2 * DAY): "scheduled",
        ("Past release", NOW - DAY): "scheduled",  # in range and missing, but past: untouched
        ("Just ahead", NOW + DAY): "scheduled",
        ("Consumer Price Index", NOW + 2 * DAY): "removed",
        ("Consumer Price Index", NOW + 3 * DAY): "scheduled",
        ("Producer Price Index", NOW + 3 * DAY): "scheduled",
        ("Next month", NOW + 30 * DAY): "scheduled",
        ("Far future", NOW + 90 * DAY): "scheduled",  # outside this pull's range
    }
    assert (await _rows("bea"))[0]["status"] == "scheduled"  # other sources untouched
    # Seen again: back to scheduled.
    await store.sync_events("bls", [_ev("Consumer Price Index", NOW + 2 * DAY),
                                     _ev("Consumer Price Index", NOW + 3 * DAY)], later + 2)
    again = {(r["title"], r["scheduled_at_ms"]): r["status"] for r in await _rows("bls")}
    assert again[("Consumer Price Index", NOW + 2 * DAY)] == "scheduled"


@pytest.mark.asyncio
async def test_sync_window_covers_edge_rows_that_move_out_or_vanish(test_db) -> None:
    def ff(title: str, at: int) -> MacroEvent:
        return _ev(title, at, source="forexfactory", category="other")

    week = (NOW - DAY, NOW + 6 * DAY - 1)  # the span each pull is complete for
    first, middle, last = ff("ADP Weekly", NOW + DAY), ff("Speech", NOW + 2 * DAY), \
        ff("Late speech", NOW + 4 * DAY)
    beyond = ff("Next week", NOW + 6 * DAY)  # just past the window
    await store.sync_events("forexfactory", [first, middle, last, beyond], NOW, window=week)
    # The first row moves 15 minutes later and the last one is dropped: both lie outside
    # the new pull's own [earliest, latest] span but inside the window.
    moved = ff("ADP Weekly", NOW + DAY + 900_000)
    result = await store.sync_events("forexfactory", [moved, middle], NOW + 60_000, window=week)
    assert (result.seen, result.removed) == (2, 2)
    status = {(r["title"], r["scheduled_at_ms"]): r["status"]
              for r in await _rows("forexfactory")}
    assert status == {
        ("ADP Weekly", NOW + DAY): "removed",
        ("ADP Weekly", NOW + DAY + 900_000): "scheduled",
        ("Speech", NOW + 2 * DAY): "scheduled",
        ("Late speech", NOW + 4 * DAY): "removed",
        ("Next week", NOW + 6 * DAY): "scheduled",  # outside the window: untouched
    }


@pytest.mark.asyncio
async def test_sync_rejects_events_from_another_source(test_db) -> None:
    with pytest.raises(ValueError):
        await store.sync_events("bls", [_ev("GDP", NOW, source="bea")], NOW)


@pytest.mark.asyncio
async def test_events_between_filters_and_orders(test_db) -> None:
    await store.sync_events("bls", [
        _ev("Consumer Price Index", NOW + 2 * DAY),
        _ev("Employment Situation", NOW + DAY, category="nfp"),
        _ev("Real Earnings", NOW + 2 * DAY, category="other"),
    ], NOW)
    await store.sync_events("fed", [_ev("FOMC Meeting", NOW + 5 * DAY, source="fed",
                                        category="fomc_decision")], NOW)
    await store.sync_events("bls", [_ev("Employment Situation", NOW + DAY, category="nfp"),
                                    _ev("Real Earnings", NOW + 2 * DAY, category="other")],
                            NOW + 1)  # CPI removed
    rows = await store.events_between(NOW, NOW + 10 * DAY)
    assert [r["title"] for r in rows] == ["Employment Situation", "Real Earnings", "FOMC Meeting"]
    picked = await store.events_between(NOW, NOW + 10 * DAY, categories=["cpi", "fomc_decision"])
    assert [r["title"] for r in picked] == ["FOMC Meeting"]
    with_removed = await store.events_between(NOW, NOW + 10 * DAY, categories=["cpi"],
                                              include_removed=True)
    assert [(r["title"], r["status"]) for r in with_removed] == [
        ("Consumer Price Index", "removed")]
    assert await store.events_between(NOW + DAY + 1, NOW + 2 * DAY) == []  # end exclusive
    assert await store.events_between(NOW, NOW + 10 * DAY, categories=[]) == []


def _cons(forecast: str | None, previous: str | None = "206K",
          impact: str | None = "Medium") -> ConsensusRow:
    return ConsensusRow("forexfactory", "USD", "Unemployment Claims", "jobless_claims",
                        NOW + 3 * DAY, impact, forecast, previous)


@pytest.mark.asyncio
async def test_consensus_inserts_only_changes_and_reads_as_of(test_db) -> None:
    assert await store.record_consensus([_cons("209K")], NOW) == 1
    assert await store.record_consensus([_cons("209K")], NOW + 3_600_000) == 0
    assert await store.record_consensus([_cons("211K")], NOW + 7_200_000) == 1
    assert await store.record_consensus([_cons("211K", impact="High")], NOW + 9_000_000) == 1
    assert await store.record_consensus([_cons(None)], NOW + 10_000_000) == 1
    assert await store.record_consensus([_cons(None), _cons(None)], NOW + 11_000_000) == 0
    assert await store.record_consensus([], NOW) == 0
    key = ("forexfactory", "Unemployment Claims", NOW + 3 * DAY)
    assert await store.consensus_as_of(*key, NOW - 1) is None
    first = await store.consensus_as_of(*key, NOW + 3_600_000)
    assert first is not None and (first["forecast"], first["taken_at_ms"]) == ("209K", NOW)
    assert first["country"] == "USD" and first["category"] == "jobless_claims"
    assert (await store.consensus_as_of(*key, NOW + 7_200_000))["forecast"] == "211K"
    assert (await store.consensus_as_of(*key, NOW + 9_000_000))["impact"] == "High"
    assert (await store.consensus_as_of(*key, NOW + 99 * DAY))["forecast"] is None
    assert await store.consensus_as_of("forexfactory", "CPI m/m", NOW + 3 * DAY, NOW + DAY) is None


@pytest.mark.asyncio
async def test_consensus_same_slot_twice_in_one_pull_keeps_the_last_without_flip_flop(
    test_db,
) -> None:
    # One pull carries two rows for the same slot with different values: the last one wins
    # (as in sync_events), so repeated pulls insert nothing and first-seen stays put.
    pull = [_cons("1", previous="2"), _cons("3", previous="4")]
    assert await store.record_consensus(pull, NOW) == 1
    assert await store.record_consensus(pull, NOW + 3_600_000) == 0
    assert await store.record_consensus(pull, NOW + 7_200_000) == 0
    key = ("forexfactory", "Unemployment Claims", NOW + 3 * DAY)
    latest = await store.consensus_as_of(*key, NOW + 99 * DAY)
    assert latest is not None
    assert (latest["forecast"], latest["previous"], latest["taken_at_ms"]) == ("3", "4", NOW)
