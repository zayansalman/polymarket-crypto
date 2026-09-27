"""Persistence for regime snapshots (``regime_snapshots``) and threshold versions.

Append-only: every scan inserts one row, nothing is ever updated, so the
table is a faithful time series a future router can be backtested against
(join on ``window_slug`` for the clock-derived families, on ``created_ts``
for the daily family). Features, bands, fits, quality and sources are
stored as JSON columns — adding a feature is a code change, never a schema
migration. The threshold values behind each ``thresholds_version`` are
journaled once (``INSERT OR IGNORE``) so history can be re-banded
reproducibly.
"""
from __future__ import annotations

import json
from typing import Any

import db as _db
from polymarket_bot.regime.types import RegimeSnapshot


async def record_snapshot(snapshot: RegimeSnapshot) -> int:
    """Insert ``snapshot``; returns the new row id."""
    async with _db.connect() as conn:
        cur = await conn.execute(
            """
            INSERT INTO regime_snapshots(
              created_at, created_ts, run_id, scan_seq, asset, symbol, timeframe,
              window_slug, grade, headline, recommendation, thresholds_version,
              bands_json, features_json, fits_json, quality_json, sources_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.created_at,
                snapshot.created_ts,
                snapshot.run_id,
                snapshot.scan_seq,
                snapshot.asset,
                snapshot.symbol,
                snapshot.timeframe,
                snapshot.window_slug,
                snapshot.grade,
                snapshot.headline,
                snapshot.recommendation,
                snapshot.thresholds_version,
                json.dumps(snapshot.bands, sort_keys=True),
                json.dumps(snapshot.features.as_dict(), sort_keys=True),
                json.dumps([f.as_dict() for f in snapshot.fits]),
                json.dumps([q.as_dict() for q in snapshot.quality]),
                json.dumps(dict(snapshot.sources), sort_keys=True),
            ),
        )
        row_id = int(cur.lastrowid or 0)
        await conn.commit()
    return row_id


async def record_thresholds(version: str, thresholds: dict[str, Any], seen_at: str) -> bool:
    """Journal the values behind ``version`` once; ``True`` iff newly inserted."""
    async with _db.connect() as conn:
        await conn.execute(
            """
            INSERT OR IGNORE INTO regime_threshold_versions(
              version, thresholds_json, first_seen_at
            ) VALUES (?, ?, ?)
            """,
            (version, json.dumps(thresholds, sort_keys=True), seen_at),
        )
        inserted = conn.total_changes > 0
        await conn.commit()
    return inserted


def _decode(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["bands"] = json.loads(d.pop("bands_json") or "{}")
    d["features"] = json.loads(d.pop("features_json") or "{}")
    d["fits"] = json.loads(d.pop("fits_json") or "[]")
    d["quality"] = json.loads(d.pop("quality_json") or "[]")
    d["sources"] = json.loads(d.pop("sources_json") or "{}")
    d["usable_for_router"] = d.get("grade") == "full"
    return d


async def latest_snapshot(asset: str | None = None) -> dict[str, Any] | None:
    """Most recent snapshot (optionally for one asset) as a plain dict."""
    query = "SELECT * FROM regime_snapshots"
    params: list[Any] = []
    if asset is not None:
        query += " WHERE asset = ?"
        params.append(asset)
    query += " ORDER BY id DESC LIMIT 1"
    async with _db.connect() as conn:
        async with conn.execute(query, params) as cur:
            row = await cur.fetchone()
    return _decode(row) if row else None


async def recent_snapshots(
    limit: int = 60, asset: str | None = None
) -> list[dict[str, Any]]:
    """Last ``limit`` snapshots, oldest-first, as plain dicts."""
    query = "SELECT * FROM regime_snapshots"
    params: list[Any] = []
    if asset is not None:
        query += " WHERE asset = ?"
        params.append(asset)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    async with _db.connect() as conn:
        async with conn.execute(query, params) as cur:
            rows = [_decode(r) for r in await cur.fetchall()]
    return list(reversed(rows))
