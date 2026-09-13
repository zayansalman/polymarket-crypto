"""Maker/taker placement telemetry (#137): derivation + backfill.

The CLOB placement response has always been journaled verbatim in
``live_orders.details_json``; its ``response.status`` is the maker/taker
signal ('matched' = crossed at placement → taker fee paid; 'live' = rested on
the book → maker if later filled, fee-free). #137 promotes it to a queryable
``placement_status`` column: derived at insert time in ``journal_live_order``
and backfilled from the JSON for every historical row by ``init_db``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

import db as _db


@pytest_asyncio.fixture
async def journal_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "test_placement.db")
    await _db.init_db()
    return _db


async def _rows(db) -> list[dict]:
    async with db.connect() as conn:
        async with conn.execute(
            "SELECT * FROM live_orders ORDER BY id"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_matched_placement_recorded_as_taker(journal_db) -> None:
    """A crossed-at-placement response ('matched') lands in the column."""
    await _db.journal_live_order(
        intent="ENTRY",
        side="Up",
        status="SUBMITTED",
        details={"response": {"status": "matched", "success": True}},
    )
    (row,) = await _rows(journal_db)
    assert row["placement_status"] == "matched"


@pytest.mark.asyncio
async def test_resting_placement_recorded_as_maker_eligible(journal_db) -> None:
    await _db.journal_live_order(
        intent="ENTRY",
        side="Down",
        status="SUBMITTED",
        details={"response": {"status": "live", "success": True}},
    )
    (row,) = await _rows(journal_db)
    assert row["placement_status"] == "live"


@pytest.mark.asyncio
async def test_no_response_status_stays_null(journal_db) -> None:
    """BLOCKED/paper rows and responses without a status must not invent one."""
    await _db.journal_live_order(intent="ENTRY", side="Up", status="BLOCKED")
    await _db.journal_live_order(
        intent="ENTRY", side="Up", status="ERROR",
        details={"error": "timeout"},
    )
    rows = await _rows(journal_db)
    assert all(r["placement_status"] is None for r in rows)


@pytest.mark.asyncio
async def test_backfill_lifts_status_from_legacy_json(journal_db) -> None:
    """Rows journaled before the migration get placement_status on init_db."""
    legacy = json.dumps({"response": {"status": "matched", "success": True}})
    async with journal_db.connect() as conn:
        await conn.execute(
            "INSERT INTO live_orders "
            "(created_at, intent, side, status, details_json, mode, placement_status) "
            "VALUES ('2026-06-20T00:00:00+00:00','ENTRY','Up','SUBMITTED',?, 'live', NULL)",
            (legacy,),
        )
        # A row with junk JSON must survive the backfill untouched.
        await conn.execute(
            "INSERT INTO live_orders "
            "(created_at, intent, side, status, details_json, mode, placement_status) "
            "VALUES ('2026-06-20T00:01:00+00:00','ENTRY','Up','SUBMITTED','not json', 'live', NULL)",
        )
        await conn.commit()

    await _db.init_db()  # re-running init runs the backfill

    rows = await _rows(journal_db)
    assert rows[0]["placement_status"] == "matched"
    assert rows[1]["placement_status"] is None


@pytest.mark.asyncio
async def test_backfill_does_not_overwrite_existing_status(journal_db) -> None:
    """Idempotence: an already-populated placement_status is never rewritten."""
    async with journal_db.connect() as conn:
        await conn.execute(
            "INSERT INTO live_orders "
            "(created_at, intent, side, status, details_json, mode, placement_status) "
            "VALUES ('2026-06-20T00:00:00+00:00','ENTRY','Up','SUBMITTED',?, 'live','live')",
            (json.dumps({"response": {"status": "matched"}}),),
        )
        await conn.commit()

    await _db.init_db()

    (row,) = await _rows(journal_db)
    assert row["placement_status"] == "live"  # kept, not re-derived
