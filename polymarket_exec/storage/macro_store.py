"""SQLite read/write for the macro calendar: scheduled events and first-seen consensus values."""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import db as _db
from polymarket_exec.connectors.macro_calendar import ConsensusRow, MacroEvent

# config-table keys holding each recorder source's last schedule state (one JSON object each).
FEED_STATE_KEY_PREFIX = "macro.feed_state."


@dataclass(frozen=True)
class SyncResult:
    seen: int  # distinct (title, time) slots in the pull
    removed: int  # future slots of this source that vanished from the pull


async def sync_events(
    source: str,
    events: Sequence[MacroEvent],
    now_ms: int,
    window: tuple[int, int] | None = None,
) -> SyncResult:
    """Upsert one full pull of ``source``'s schedule, then mark vanished future slots removed.

    Only rows of ``source`` that are still scheduled, lie in the future, and fall inside
    ``window`` can be removed — past rows are never touched and a pull says nothing about
    slots outside the span it covers. ``window`` is the inclusive [start, end] ms span the
    pull is complete for (a source that lists a fixed week passes that week); by default
    it is the pull's own [earliest, latest] time. One transaction.
    """
    if any(e.source != source for e in events):
        raise ValueError(f"sync_events({source!r}) got events from another source")
    slots = {(e.title, e.scheduled_at_ms): e for e in events}  # last duplicate wins
    if not slots:
        return SyncResult(0, 0)
    recorded_at = _db.utc_now_iso()
    times = [ms for _title, ms in slots]
    lo, hi = window if window is not None else (min(times), max(times))
    async with _db.connect() as conn:
        await conn.executemany(
            """
            INSERT INTO macro_events(
              source, category, title, scheduled_at_ms, reference_period, status,
              first_seen_ms, last_seen_ms, recorded_at
            ) VALUES (?, ?, ?, ?, ?, 'scheduled', ?, ?, ?)
            ON CONFLICT(source, title, scheduled_at_ms) DO UPDATE SET
              category = excluded.category,
              reference_period = excluded.reference_period,
              status = 'scheduled',
              last_seen_ms = excluded.last_seen_ms,
              recorded_at = excluded.recorded_at
            """,
            [
                (source, e.category, e.title, e.scheduled_at_ms, e.reference_period,
                 now_ms, now_ms, recorded_at)
                for e in slots.values()
            ],
        )
        cur = await conn.execute(
            "SELECT id, title, scheduled_at_ms FROM macro_events "
            "WHERE source = ? AND status = 'scheduled' AND scheduled_at_ms > ? "
            "AND scheduled_at_ms BETWEEN ? AND ?",
            (source, now_ms, lo, hi),
        )
        gone = [(recorded_at, r["id"]) for r in await cur.fetchall()
                if (r["title"], r["scheduled_at_ms"]) not in slots]
        await conn.executemany(
            "UPDATE macro_events SET status = 'removed', recorded_at = ? WHERE id = ?", gone
        )
        await conn.commit()
    return SyncResult(len(slots), len(gone))


def _values(row: Any) -> tuple[str | None, str | None, str | None]:
    return (row["impact"], row["forecast"], row["previous"])


async def record_consensus(rows: Iterable[ConsensusRow], now_ms: int) -> int:
    """Insert a consensus row only when (impact, forecast, previous) changed since the latest.

    A pull holding the same (source, title, time) slot twice keeps the last row, as
    ``sync_events`` does, so one value per slot is compared and stored per pull.
    """
    rows = list({(r.source, r.title, r.scheduled_at_ms): r for r in rows}.values())
    if not rows:
        return 0
    recorded_at = _db.utc_now_iso()
    inserted = 0
    latest: dict[tuple[str, str, int], tuple[str | None, str | None, str | None]] = {}
    async with _db.connect() as conn:
        for row in rows:
            key = (row.source, row.title, row.scheduled_at_ms)
            if key not in latest:
                cur = await conn.execute(
                    "SELECT impact, forecast, previous FROM macro_consensus "
                    "WHERE source = ? AND title = ? AND scheduled_at_ms = ? "
                    "ORDER BY taken_at_ms DESC, id DESC LIMIT 1",
                    key,
                )
                prior = await cur.fetchone()
                if prior is not None:
                    latest[key] = _values(prior)
            values = (row.impact, row.forecast, row.previous)
            if key in latest and latest[key] == values:
                continue
            await conn.execute(
                """
                INSERT INTO macro_consensus(
                  source, title, country, category, scheduled_at_ms, impact, forecast,
                  previous, taken_at_ms, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (row.source, row.title, row.country, row.category, row.scheduled_at_ms,
                 *values, now_ms, recorded_at),
            )
            latest[key] = values
            inserted += 1
        await conn.commit()
    return inserted


async def save_feed_state(key: str, state: Mapping[str, Any]) -> None:
    """Keep one recorder source's schedule state (last attempt, next attempt, ...) across restarts."""
    await _db.set_config(FEED_STATE_KEY_PREFIX + key, json.dumps(dict(state), sort_keys=True))


async def load_feed_states(keys: Iterable[str]) -> dict[str, Any]:
    """Saved state per source key, as decoded JSON; keys never saved or not JSON are left out."""
    wanted = {FEED_STATE_KEY_PREFIX + k: k for k in keys}
    if not wanted:
        return {}
    async with _db.connect() as conn:
        cur = await conn.execute(
            f"SELECT key, value FROM config WHERE key IN ({', '.join('?' for _ in wanted)})",
            list(wanted),
        )
        rows = await cur.fetchall()
    states: dict[str, Any] = {}
    for row in rows:
        try:
            states[wanted[row["key"]]] = json.loads(row["value"])
        except (TypeError, ValueError):  # NULL or not JSON: as if never saved
            continue
    return states


async def events_between(
    start_ms: int,
    end_ms: int,
    categories: Iterable[str] | None = None,
    include_removed: bool = False,
) -> list[dict[str, Any]]:
    """Events with ``start_ms <= scheduled_at_ms < end_ms``, earliest first."""
    sql = "SELECT * FROM macro_events WHERE scheduled_at_ms >= ? AND scheduled_at_ms < ?"
    params: list[Any] = [start_ms, end_ms]
    if categories is not None:
        cats = list(categories)
        if not cats:
            return []
        sql += f" AND category IN ({', '.join('?' for _ in cats)})"
        params += cats
    if not include_removed:
        sql += " AND status = 'scheduled'"
    sql += " ORDER BY scheduled_at_ms, id"
    async with _db.connect() as conn:
        cur = await conn.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]


async def consensus_as_of(
    source: str, title: str, scheduled_at_ms: int, as_of_ms: int
) -> dict[str, Any] | None:
    """The consensus row that was current at ``as_of_ms`` (latest taken at or before it)."""
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM macro_consensus WHERE source = ? AND title = ? "
            "AND scheduled_at_ms = ? AND taken_at_ms <= ? "
            "ORDER BY taken_at_ms DESC, id DESC LIMIT 1",
            (source, title, scheduled_at_ms, as_of_ms),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
