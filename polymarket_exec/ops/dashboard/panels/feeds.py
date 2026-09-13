"""FEEDS card: one row per upstream feed — what it feeds, source, delay, status.

Replaces the ribbon's TICK/SPOT/REF/VOL/BOOK/EXEC chips. Rows are plain data
(``FeedRow``) so new venues (Binance spot, Kraken, …) are one more row.

Per-feed status comes from the last journaled tick's ``feed_source``; when that
tick is stale or missing, tick-derived rows go grey instead of showing an old OK.
Delays shown are the ones the app measures today: loop tick age, Chainlink WS
print age, and time since the last live order. Other rows show "—".
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Any

import config as _config

from . import _shared as s

# Live mode: no order action for this long while running is worth a look —
# a bot that lost CLOB write access often keeps reading and journaling skips.
ORDER_QUIET_AFTER_S = 300


@dataclass(frozen=True)
class FeedRow:
    name: str
    role: str
    source: str
    delay: str
    status: str
    level: str  # on | warn | down | idle
    delay_warn: bool = False


def _secs(v: float) -> str:
    if v < 10:
        return f"{v:.1f}s"
    if v < 60:
        return f"{int(v)}s"
    return f"{int(v) // 60}m{int(v) % 60:02d}s"


def build_rows(
    *,
    tick: dict[str, Any] | None,
    is_live: bool,
    last_live_at: str | None,
    chainlink_age_s: float | None,
    tick_seconds: float,
) -> list[FeedRow]:
    tick_age = s.tick_age_seconds(tick.get("created_at") if tick else None)
    # Same cutoff as the loop's own check (paper._connectivity_from_tick): the
    # runtime tick-interval knob, not the env default.
    stale_after = int(max(tick_seconds * 3, 20))
    fresh = tick_age is not None and tick_age <= stale_after
    parts = s.parse_feed_source(tick.get("feed_source") if tick else None)

    if tick_age is None:
        loop = FeedRow("Bot loop", "decision tick", "journal", "—", "NO DATA", "warn")
    elif fresh:
        loop = FeedRow("Bot loop", "decision tick", "journal", f"{tick_age}s", "OK", "on")
    else:
        loop = FeedRow(
            "Bot loop", "decision tick", "journal", _secs(tick_age), "STALE", "warn", True
        )
    rows = [loop]

    def from_tick(row: FeedRow) -> FeedRow:
        # No fresh tick → the recorded source status is history, not health.
        if fresh:
            return row
        return FeedRow(row.name, row.role, row.source, row.delay, "—", "idle", row.delay_warn)

    spot = parts.get("spot") or ""
    if spot == "chainlink_ws":
        spot_src, spot_status, spot_level = "Polymarket WS", "OK", "on"
    elif spot.startswith("chainlink"):
        spot_src, spot_status, spot_level = "REST fallback", "FALLBACK", "warn"
    else:
        spot_src, spot_status, spot_level = spot or "—", "DOWN", "down"
    cl_delay, cl_warn = "—", False
    if chainlink_age_s is not None:
        cl_delay = _secs(chainlink_age_s)
        cl_warn = chainlink_age_s > _config.CHAINLINK_STALE_SECONDS
    rows.append(from_tick(FeedRow(
        "Chainlink BTC/USD", "spot · vol", spot_src, cl_delay, spot_status, spot_level, cl_warn
    )))

    ref_ok = (parts.get("ref") or "").startswith("chainlink")
    rows.append(from_tick(FeedRow(
        "Chainlink BTC/USD", "window open", "REST", "—",
        "OK" if ref_ok else "DOWN", "on" if ref_ok else "down",
    )))

    book_ok = bool(tick) and any(
        tick.get(k) is not None
        for k in ("up_best_ask", "down_best_ask", "up_best_bid", "down_best_bid")
    )
    rows.append(from_tick(FeedRow(
        "Polymarket book", "UP/DOWN quotes", "CLOB REST", "—",
        "OK" if book_ok else "EMPTY", "on" if book_ok else "warn",
    )))

    # A tick is only journaled after Gamma answered, so a fresh tick means OK.
    rows.append(from_tick(FeedRow("Polymarket Gamma", "market lookup", "REST", "—", "OK", "on")))

    vol = parts.get("vol") or ""
    if vol == "chainlink_ws":
        binance = FeedRow("Binance BTCUSDT", "vol backup", "REST klines", "—", "STANDBY", "idle")
    elif vol.startswith("binance"):
        binance = FeedRow("Binance BTCUSDT", "vol backup", "REST klines", "—", "IN USE", "warn")
    else:
        binance = FeedRow("Binance BTCUSDT", "vol backup", "REST klines", "—", "DOWN", "down")
    rows.append(from_tick(binance))

    if is_live:
        live_age = s.tick_age_seconds(last_live_at)
        if live_age is None:
            rows.append(FeedRow("Polymarket orders", "order entry", "CLOB", "—", "NONE", "warn"))
        else:
            quiet = live_age > ORDER_QUIET_AFTER_S
            rows.append(FeedRow(
                "Polymarket orders", "order entry", "CLOB", f"{_secs(live_age)} ago",
                "QUIET" if quiet else "OK", "warn" if quiet else "on", quiet,
            ))
    return rows


def render(
    *,
    tick: dict[str, Any] | None,
    is_live: bool,
    last_live_at: str | None,
    chainlink_age_s: float | None,
    tick_seconds: float,
) -> str:
    rows = build_rows(
        tick=tick,
        is_live=is_live,
        last_live_at=last_live_at,
        chainlink_age_s=chainlink_age_s,
        tick_seconds=tick_seconds,
    )
    issues = sum(r.level in ("warn", "down") for r in rows)
    note = "all OK" if not issues else f"{issues} issue{'s' if issues != 1 else ''}"
    body = "".join(
        "<tr>"
        f"<td>{escape(r.name)}</td>"
        f"<td class='feeds-role'>{escape(r.role)}</td>"
        f"<td class='feeds-role'>{escape(r.source)}</td>"
        f"<td class='feeds-delay{' warn' if r.delay_warn else ''}'>{escape(r.delay)}</td>"
        f"<td><span class='feed {r.level}'>{escape(r.status)}</span></td>"
        "</tr>"
        for r in rows
    )
    return (
        "<section class='card feeds-card'>"
        f"<div class='card-h'>FEEDS<span class='win{' warn' if issues else ''}'>{note}</span></div>"
        "<div class='feeds-scroll'><table class='de-tail-tbl feeds-tbl'>"
        "<thead><tr><th>Feed</th><th>Used for</th><th>Source</th>"
        "<th class='feeds-delay'>Delay</th><th>Status</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
        "</section>"
    )
