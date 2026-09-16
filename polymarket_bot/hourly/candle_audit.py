"""Candle audit: check the Binance candles each hourly decision used against Binance's archive.

The spot taker-push reversal rule has sharp cut-offs, so a slightly different taker-buy volume
on a just-closed candle can flip a bet. Once Binance publishes the day's archive
(data.binance.vision), each decision's saved hour-H-1 spot and perp candles are compared with
the archive, and the rule is re-evaluated with the archive values. Observation only: the
result is written to the decision record and a flipped decision notifies the operator.

Statuses: MATCH (same candles), MISMATCH (a field differs, same decision), FLIPPED (the
decision would change), UNAVAILABLE (no archive after 7 days), NO_CANDLES (the decision saved
no candles).

Sources: approved by Zayan (operator), 2026-09-15, from the alphaXiv sweep by Claude,
2026-09-15 (point-in-time audit, arXiv 2608.25348). Archive formats checked on 2026-09-15:
the spot daily 1h CSV has no header and microsecond timestamps; the USD-M futures CSV has a
header row and millisecond timestamps. The recheck swaps only hour H-1 into the saved window
statistics (earlier hours were closed long before the decision).
"""
from __future__ import annotations

import csv
import io
import json
import math
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

import db as _db
from logging_setup import get_logger
from polymarket_bot.hourly import btcusdt_1h_spot_taker_push_reversal as rule
from polymarket_bot.hourly.market import HOUR_S, Candle

log = get_logger("hourly_candle_audit")

ARCHIVE_BASE = "https://data.binance.vision/data"
AUDIT_AFTER_S = 6 * HOUR_S  # after the end of the UTC day that holds hour H-1
GIVE_UP_AFTER_S = 7 * 24 * HOUR_S  # after that same day end
RETRY_EVERY_S = HOUR_S
_FIELDS = ("open", "high", "low", "close", "volume", "quote_volume", "taker_buy_volume")
_REL_TOL = 1e-9

_last_attempt: dict[str, int] = {}


def reset_state() -> None:
    _last_attempt.clear()


def archive_url(venue: str, day: str) -> str:
    kind = "spot" if venue == "spot" else "futures/um"
    return f"{ARCHIVE_BASE}/{kind}/daily/klines/BTCUSDT/1h/BTCUSDT-1h-{day}.zip"


def parse_archive(zip_bytes: bytes) -> dict[int, Candle]:
    """Archive rows keyed by open time in milliseconds (header rows are skipped)."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        text = z.read(z.namelist()[0]).decode()
    out: dict[int, Candle] = {}
    for row in csv.reader(io.StringIO(text)):
        if not row or not row[0].strip().isdigit():
            continue
        open_time = int(row[0])
        if open_time >= 10**14:  # microseconds (spot archive)
            open_time //= 1000
        out[open_time] = Candle(
            open_time_ms=open_time, open=float(row[1]), high=float(row[2]), low=float(row[3]),
            close=float(row[4]), volume=float(row[5]), quote_volume=float(row[7]),
            taker_buy_volume=float(row[9]),
        )
    return out


def swap_last(stats: dict[str, Any], old: float, new: float) -> tuple[float, float]:
    """(mean, sample stdev) of the saved window with its last value replaced."""
    n, mean, sd = int(stats["n"]), float(stats["mean"]), float(stats["sd"])
    total = n * mean - old + new
    squares = (n - 1) * sd * sd + n * mean * mean - old * old + new * new
    new_mean = total / n
    variance = max((squares - n * new_mean * new_mean) / (n - 1), 0.0)
    return new_mean, math.sqrt(variance)


def _imbalance(c: Candle) -> float | None:
    return 2 * c.taker_buy_volume / c.volume - 1 if c.volume > 0 else None


def _direction(c: Candle) -> int:
    return 1 if c.close > c.open else (-1 if c.close < c.open else 0)


def _flow_push(saved: dict[str, Any], stats: dict[str, Any], archived: Candle) -> float | None:
    old, new = _imbalance(_candle(saved)), _imbalance(archived)
    if old is None or new is None or stats.get("mean") is None or stats.get("sd") is None:
        return None
    mean, sd = swap_last(stats, old, new)
    if sd <= 0:
        return None
    return (new - mean) / sd * _direction(archived)


def _candle(fields: dict[str, Any]) -> Candle:
    return Candle(**{k: fields[k] for k in ("open_time_ms", *_FIELDS)})


def _differs(saved: dict[str, Any], archived: Candle, venue: str) -> list[str]:
    return [f"{venue}.{f}" for f in _FIELDS
            if not math.isclose(float(saved[f]), getattr(archived, f), rel_tol=_REL_TOL)]


def recheck(
    signal: dict[str, Any], spot: Candle, perp: Candle, recorded_side: str | None
) -> dict[str, Any]:
    """Re-evaluate the frozen rule with the archive's hour-H-1 candles."""
    spot_fz = _flow_push(signal["spot_h1"], signal["spot_window"], spot)
    perp_fz = _flow_push(signal["perp_h1"], signal["perp_window"], perp)
    direction = _direction(spot)
    rng = spot.high - spot.low
    clv = (2 * spot.close - spot.high - spot.low) / rng if rng > 0 else None
    side: str | None = None
    if (direction != 0 and spot_fz is not None and spot_fz > rule.SPOT_FZ_MIN
            and perp_fz is not None and perp_fz <= rule.PERP_FZ_MAX
            and clv is not None and clv * direction > rule.CLV_MIN):
        side = "Down" if direction > 0 else "Up"
    return {
        "fields_differ": _differs(signal["spot_h1"], spot, "spot")
        + _differs(signal["perp_h1"], perp, "perp"),
        "spot_fz": spot_fz, "perp_fz": perp_fz, "clv": clv,
        "side": side, "recorded_side": recorded_side, "flipped": side != recorded_side,
    }


def _day(window_start_ts: int) -> tuple[str, int]:
    """UTC date of hour H-1 and the end of that day, in seconds."""
    prev = datetime.fromtimestamp(window_start_ts - HOUR_S, UTC)
    day_start = prev.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start.strftime("%Y-%m-%d"), int((day_start + timedelta(days=1)).timestamp())


async def _write(row_id: int, status: str, detail: dict[str, Any] | None) -> None:
    async with _db.connect() as conn:
        await conn.execute(
            "UPDATE hourly_strategy_context SET candle_audit = ?, candle_audit_json = ?, "
            "candle_audited_at = ? WHERE id = ?",
            (status, json.dumps(detail) if detail is not None else None, _db.utc_now_iso(),
             row_id),
        )
        await conn.commit()


async def _fetch(client: httpx.AsyncClient, venue: str, day: str) -> dict[int, Candle] | None:
    resp = await client.get(archive_url(venue, day))
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return parse_archive(resp.content)


async def audit_due(client: httpx.AsyncClient, now: int) -> int:
    """Audit decisions whose archive day is due, at most one day per call. Never raises."""
    try:
        return await _audit_due(client, now)
    except Exception as exc:  # noqa: BLE001 - an observation must never break the tick
        log.warning("hourly_candle_audit.failed", error=f"{type(exc).__name__}: {exc}")
        return 0


async def _audit_due(client: httpx.AsyncClient, now: int) -> int:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT id, window_start_ts, window_slug, mode, decision_side, signal_json "
            "FROM hourly_strategy_context WHERE strategy_id = ? AND candle_audit IS NULL "
            "ORDER BY window_start_ts",
            (rule.STRATEGY_ID,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    written = 0
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        signal = json.loads(row["signal_json"] or "{}")
        if not all(k in signal for k in ("spot_h1", "perp_h1", "spot_window", "perp_window")):
            await _write(row["id"], "NO_CANDLES", None)
            written += 1
            continue
        row["signal"] = signal
        by_day.setdefault(_day(row["window_start_ts"])[0], []).append(row)
    for day, day_rows in sorted(by_day.items()):
        day_end = _day(day_rows[0]["window_start_ts"])[1]
        not_published_yet = now < day_end + AUDIT_AFTER_S
        tried_recently = now - _last_attempt.get(day, -RETRY_EVERY_S) < RETRY_EVERY_S
        if not_published_yet or tried_recently:
            continue
        _last_attempt[day] = now
        try:
            spot, perp = await _fetch(client, "spot", day), await _fetch(client, "perp", day)
        except httpx.HTTPError as exc:
            log.warning("hourly_candle_audit.archive_read_failed", day=day, error=str(exc))
            return written
        if spot is None or perp is None:
            if now >= day_end + GIVE_UP_AFTER_S:
                for row in day_rows:
                    await _write(row["id"], "UNAVAILABLE", None)
                    written += 1
            return written
        for row in day_rows:
            open_ms = (row["window_start_ts"] - HOUR_S) * 1000
            if open_ms not in spot or open_ms not in perp:
                await _write(row["id"], "UNAVAILABLE", None)
                written += 1
                continue
            detail = recheck(row["signal"], spot[open_ms], perp[open_ms], row["decision_side"])
            status = "FLIPPED" if detail["flipped"] else (
                "MISMATCH" if detail["fields_differ"] else "MATCH")
            await _write(row["id"], status, detail)
            written += 1
            if status == "FLIPPED":
                log.warning("hourly_candle_audit.flipped", window_slug=row["window_slug"],
                            mode=row["mode"], recorded=row["decision_side"], archive=detail["side"])
                await _db.notify(
                    "hourly_candle_audit",
                    f"Candle audit: the {row['mode']} decision for {row['window_slug']} would have "
                    f"been {detail['side'] or 'no bet'} with Binance's archived candles, not "
                    f"{row['decision_side'] or 'no bet'}. Fields that differ: "
                    f"{', '.join(detail['fields_differ']) or 'none'}.",
                    {"window_start_ts": row["window_start_ts"], "mode": row["mode"]},
                )
        return written  # one archive day per call
    return written
