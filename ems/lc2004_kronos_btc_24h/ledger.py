"""lc2004-Kronos BTC 24h ledger: every read and write of the ``lc2004_forecasts`` table.

One row per hourly forecast, unique on (window, last closed 1h candle), so an hour is forecast
once however often the caller asks. The table is in ``ems/db.py`` SCHEMA (created by
``db.init_db``), so it exists before the first forecast and the dashboard can read it from a
fresh database. A failed forecast keeps its error text and leaves the numbers empty. The
market's best bid and ask on both outcomes at forecast time are stored with it when the caller
has them, so the card can show what the book was when the model spoke.

Rows come back as plain dicts with ``final_closes`` decoded to a list of floats (None when the
run failed). Timestamps are integer epoch seconds; ``last_candle_open_ms`` is milliseconds.

There are no order tables: the forecast is display-only and the operator trades by hand
(Zayan (operator), 2026-09-29).

Claude, 2026-09-22 (the forecasts part of the old ledger); trimmed and moved onto ``ems.db``
by Claude, 2026-09-29.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from ems import db as _db  # type: ignore[import-untyped]
from ems.logging_setup import redact_secrets

TABLE = "lc2004_forecasts"
FORECAST_COLS = (
    "window_slug", "window_start_ts", "window_end_ts", "last_candle_open_ms", "horizon_hours",
    "strike", "last_close", "paths", "paths_above", "p_raw", "q_up", "sampling_se",
    "final_closes", "seconds", "error", "torch_version", "up_bid", "up_ask", "down_bid",
    "down_ask", "created_ts",
)


def _decode(row: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    raw = out.get("final_closes")
    if isinstance(raw, str):
        try:
            closes = json.loads(raw)
        except json.JSONDecodeError:
            closes = None
        out["final_closes"] = ([float(c) for c in closes]
                               if isinstance(closes, list) else None)
    return out


async def _rows(sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    async with _db.connect() as conn:
        async with conn.execute(sql, params) as cur:
            return [_decode(r) for r in await cur.fetchall()]


async def _one(sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
    rows = await _rows(sql, params)
    return rows[0] if rows else None


async def insert_forecast(**row: Any) -> int | None:
    """Store one forecast. Returns its id, or None when (window, candle) is already stored.

    ``final_closes`` may be a sequence of floats (stored as JSON, to the cent). The error text
    is scrubbed of registered secrets before it is stored, since the card shows it.
    """
    unknown = set(row) - set(FORECAST_COLS)
    if unknown:
        raise ValueError(f"unknown forecast fields: {sorted(unknown)}")
    closes = row.get("final_closes")
    if closes is not None and not isinstance(closes, str):
        row["final_closes"] = json.dumps([round(float(c), 2) for c in closes])
    if row.get("error") is not None:
        row["error"] = redact_secrets(str(row["error"]))
    names = [c for c in FORECAST_COLS if c in row]
    async with _db.connect() as conn:
        cur = await conn.execute(
            f"INSERT OR IGNORE INTO {TABLE} ({', '.join(names)}) "
            f"VALUES ({', '.join('?' * len(names))})",
            [row[n] for n in names],
        )
        await conn.commit()
        return int(cur.lastrowid) if cur.rowcount > 0 and cur.lastrowid is not None else None


async def forecast_exists(window_slug: str, last_candle_open_ms: int) -> bool:
    """Whether this window already has a forecast (failed or not) from this candle."""
    row = await _one(
        f"SELECT 1 AS x FROM {TABLE} WHERE window_slug = ? AND last_candle_open_ms = ?",
        (window_slug, int(last_candle_open_ms)),
    )
    return row is not None


async def latest_forecast(window_slug: str) -> dict[str, Any] | None:
    """The newest forecast for the window (the latest candle), failed or not."""
    return await _one(
        f"SELECT * FROM {TABLE} WHERE window_slug = ? "
        "ORDER BY last_candle_open_ms DESC, id DESC LIMIT 1",
        (window_slug,),
    )


async def window_forecasts(window_slug: str) -> list[dict[str, Any]]:
    """Every forecast for the window, oldest candle first: the card's hour-by-hour history."""
    return await _rows(
        f"SELECT * FROM {TABLE} WHERE window_slug = ? ORDER BY last_candle_open_ms, id",
        (window_slug,),
    )

