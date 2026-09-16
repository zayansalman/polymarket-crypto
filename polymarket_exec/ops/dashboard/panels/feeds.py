"""FEEDS card: one row per live upstream feed — what it feeds, source, delay, status.

Rows come from the always-on feed monitor (``polymarket_exec/ops/feed_monitor.py``),
the market-data hub (``polymarket_exec/marketdata/hub.py``), the venue flow recorder
and the macro recorder, which check every feed directly — so the card is live whether
or not the bot loop is running. Rows are plain data (``FeedRow``) so new venues are one
more row.

Delay is the age of the latest print for the Chainlink WS stream and the RTDS price
rows, the round-trip time of the latest check for each monitored REST feed, the event
latency (p50 of the slowest of the hub's CLOB market sockets), and the age of the last
successful pull for recorder feeds.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape

import config as _config
from polymarket_exec.marketdata import hub as md_hub
from polymarket_exec.marketdata import rtds_stream as rs
from polymarket_exec.ops import feed_monitor as fm
from polymarket_exec.ops import flow_recorder as fr
from polymarket_exec.ops import macro_recorder as mr

# A REST round trip slower than this is flagged (status stays OK).
SLOW_MS = 2000.0


@dataclass(frozen=True)
class FeedRow:
    name: str
    role: str
    source: str
    delay: str
    status: str
    level: str  # on | warn | down | idle
    delay_warn: bool = False
    detail: str | None = None  # hover text: the error behind a bad status


# (probe key, name, used for, source) — order is the card's row order.
_REST_FEEDS = (
    (fm.CHAINLINK_REST, "Chainlink BTC/USD", "window open", "crypto-price REST"),
    (fm.GAMMA, "Polymarket Gamma", "market lookup", "REST"),
    (fm.CLOB_BOOK, "Polymarket book", "UP/DOWN quotes", "CLOB REST"),
    (fm.BINANCE, "Binance BTCUSDT", "vol backup", "REST klines"),
)

# (flow feed key, name, used for, source, quiet_ok) — venue flow rows, in card order.
# quiet_ok: silence on a connected socket is normal (sparse futures fills, liquidations).
_FLOW_FEEDS = (
    (fr.BINANCE_SPOT_BTC, "Binance BTCUSDT", "hourly flow", "REST klines", False),
    (fr.BINANCE_SPOT_ETH, "Binance ETHUSDT", "hourly flow", "REST klines", False),
    (fr.BINANCE_PERP_BTC, "Binance perp BTCUSDT", "hourly flow", "fapi REST", False),
    (fr.BINANCE_PERP_STATE, "Binance perp BTCUSDT", "funding · OI", "fapi REST", False),
    (fr.BINANCE_LIQ, "Binance liquidations", "liquidation flow", "fstream WS", True),
    (fr.KRAKEN_SPOT, "Kraken BTC/USD", "hourly flow", "WS v2 trades", False),
    (fr.KRAKEN_FUTURES, "Kraken PF_XBTUSD", "hourly flow", "WS v1 trades", True),
    (fr.KRAKEN_FUTURES_STATE, "Kraken PF_XBTUSD", "funding · OI", "tickers REST", False),
)

# (macro source key, name, used for, source) — macro calendar rows, in card order.
_MACRO_FEEDS = (
    (mr.BLS_SCHEDULE, "BLS schedule", "CPI · jobs · PPI times", "bls.gov ICS"),
    (mr.BEA_SCHEDULE, "BEA schedule", "GDP · PCE times", "bea.gov ICS"),
    (mr.CENSUS_SCHEDULE, "Census schedule", "retail sales times", "census.gov calendar"),
    (mr.FED_CALENDAR, "Fed calendar", "FOMC · speeches", "federalreserve.gov JSON"),
    (mr.FF_WEEK, "ForexFactory week", "forecasts · claims", "faireconomy JSON"),
)

# (price source, name, used for) — RTDS reference price rows, in card order.
_PRICE_FEEDS = (
    (rs.CHAINLINK, "Chainlink prices", "spot · vol"),
    (rs.CHAINLINK_TWAP60, "Chainlink 60s TWAP", "5m·15m settle ref"),
    (rs.BINANCE, "Binance prices", "1h·1d settle ref"),
)
# The CLOB market socket, connected, with no data frame for this long is flagged.
BOOKS_STALE_S = 45.0
# A market socket whose median event latency is above this is serving stale books.
BOOKS_LAG_S = 5.0
# A reference price whose newest print is older than this is flagged.
PRICE_STALE_S = 10.0

# A connected trade socket with no frame for this long is flagged (or QUIET if normal).
WS_STALE_S = 120.0
# A socket that has not connected yet this soon after start is "connecting", not down.
WS_CONNECT_GRACE_S = 30.0
# A rate-limited macro source is retried on the recorder's first tick after its wait ends,
# behind any sources ahead of it in that pass: its row stays WAIT for a tick plus this.
MACRO_WAIT_GRACE_S = 30.0


def _secs(v: float) -> str:
    if v < 10:
        return f"{v:.1f}s"
    if v < 60:
        return f"{int(v)}s"
    return f"{int(v) // 60}m{int(v) % 60:02d}s"


def _ms(v: float) -> str:
    return f"{v:.0f}ms" if v < 1000 else _secs(v / 1000)


def _age(v: float) -> str:
    """Long ages, coarse: '42s', '17m', '5h03m', '2d04h'."""
    s = max(0, int(v))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86_400:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 86_400}d{s % 86_400 // 3600:02d}h"


def _ws_row(snap: fm.FeedsSnapshot) -> FeedRow:
    name, role, source = "Chainlink BTC/USD", "spot · vol", "Polymarket WS"
    age = snap.ws_print_age_s
    delay = _secs(age) if age is not None else "—"
    delay_warn = age is not None and age > _config.CHAINLINK_STALE_SECONDS
    if snap.ws_fresh:
        return FeedRow(name, role, source, delay, "OK", "on", delay_warn)
    if snap.ws_connected:
        return FeedRow(name, role, source, delay, "STALE", "warn", delay_warn,
                       "connected, but no recent prints")
    if snap.taken_at - snap.started_at <= _config.CHAINLINK_STALE_SECONDS:
        return FeedRow(name, role, source, delay, "CONNECTING", "idle")
    return FeedRow(name, role, source, delay, "DOWN", "down", delay_warn,
                   "not connected (reconnecting)")


def _rest_row(
    snap: fm.FeedsSnapshot, key: str, name: str, role: str, source: str
) -> FeedRow:
    probe = snap.probes.get(key)
    if probe is None:
        return FeedRow(name, role, source, "—", "CHECKING", "idle")
    delay = _ms(probe.latency_ms)
    slow = probe.latency_ms > SLOW_MS
    if snap.taken_at - probe.checked_at > snap.interval_s * 3:
        return FeedRow(name, role, source, delay, "STALE", "warn", slow,
                       f"last checked {_secs(snap.taken_at - probe.checked_at)} ago")
    if probe.ok:
        return FeedRow(name, role, source, delay, "OK", "on", slow)
    if probe.detail == "empty book":
        return FeedRow(name, role, source, delay, "EMPTY", "warn", slow, probe.detail)
    return FeedRow(name, role, source, delay, "DOWN", "down", slow, probe.detail)


def _flow_row(
    flow: fr.FlowSnapshot, key: str, name: str, role: str, source: str, quiet_ok: bool
) -> FeedRow:
    st = flow.feeds.get(key)
    if st is None or (st.kind == "rest" and st.ok is None):
        return FeedRow(name, role, source, "—", "CHECKING", "idle")
    if st.kind == "rest":
        age = flow.taken_at - st.last_event_at if st.last_event_at is not None else None
        delay = _secs(age) if age is not None else "—"
        if not st.ok:
            return FeedRow(name, role, source, delay, "DOWN", "down", False, st.detail)
        if age is not None and age > flow.interval_s * 3:
            return FeedRow(name, role, source, delay, "STALE", "warn", True,
                           f"last success {delay} ago")
        return FeedRow(name, role, source, delay, "OK", "on")
    last = st.last_event_at if st.last_event_at is not None else st.connected_since
    age = flow.taken_at - last if last is not None else None
    delay = _secs(flow.taken_at - st.last_event_at) if st.last_event_at is not None else "—"
    if not st.connected:
        if flow.taken_at - flow.started_at <= WS_CONNECT_GRACE_S:
            return FeedRow(name, role, source, delay, "CONNECTING", "idle")
        return FeedRow(name, role, source, delay, "DOWN", "down", False,
                       st.detail or "not connected (reconnecting)")
    if age is not None and age > WS_STALE_S:
        if quiet_ok:
            return FeedRow(name, role, source, delay, "QUIET", "idle", False,
                           "connected; no events lately (normal for this feed)")
        return FeedRow(name, role, source, delay, "STALE", "warn", True,
                       "connected, but no recent trades")
    return FeedRow(name, role, source, delay, "OK", "on")


def _books_row(md: md_hub.MarketDataSnapshot) -> FeedRow:
    name, source = "Polymarket books", "CLOB market WS"
    role = f"Up/Down books · trades ({md.markets} markets)"
    st = md.clob
    p50 = st.latency_ms_p50
    delay = _ms(p50) if p50 is not None else "—"
    slow = p50 is not None and p50 > SLOW_MS
    if st.connected:
        if st.subscribed == 0:
            return FeedRow(name, role, source, delay, "IDLE", "idle", False,
                           "connected; no market tokens to follow yet")
        seen = [t for t in (st.last_frame_at, st.connected_since) if t is not None]
        quiet = md.taken_at - max(seen) if seen else 0.0
        if quiet > BOOKS_STALE_S:
            return FeedRow(name, role, source, delay, "STALE", "warn", True,
                           f"connected, but no data for {_secs(quiet)}")
        slowest = md.slowest_socket or "a socket"
        if p50 is not None and p50 > BOOKS_LAG_S * 1000:
            return FeedRow(name, role, source, delay, "STALE", "warn", True,
                           f"{slowest} is {_secs(p50 / 1000)} behind (median event latency)")
        return FeedRow(name, role, source, delay, "OK", "on", slow,
                       f"slowest socket: {slowest}" if slow else None)
    if md.taken_at - md.started_at <= WS_CONNECT_GRACE_S:
        return FeedRow(name, role, source, delay, "CONNECTING", "idle")
    if md.tokens == 0:
        detail = "no market tokens yet"
        if md.gamma_errors:
            detail += f" (Gamma lookups failing: {md.gamma_last_error or 'error'})"
    else:
        detail = st.last_error or "not connected (reconnecting)"
        wanted = [s for s in md.clob_shards.values() if s.desired > 0]
        down = sum(1 for s in wanted if not s.connected)
        if 0 < down < len(wanted):  # one socket per asset x timeframe
            detail = f"{down} of {len(wanted)} sockets down; {detail}"
    return FeedRow(name, role, source, delay, "DOWN", "down", slow, detail[:200])


def _price_row(md: md_hub.MarketDataSnapshot, key: str, name: str, role: str) -> FeedRow:
    source = "RTDS WS"
    st = md.prices.get(key)
    age = md.price_ages.get(key)
    delay = _secs(age) if age is not None else "—"
    stale = age is not None and age > PRICE_STALE_S
    booting = md.taken_at - md.started_at <= WS_CONNECT_GRACE_S
    if st is not None and st.connected:
        if age is None:
            if booting:
                return FeedRow(name, role, source, delay, "CONNECTING", "idle")
            return FeedRow(name, role, source, delay, "STALE", "warn", False,
                           "connected, but no prints yet")
        if stale:
            return FeedRow(name, role, source, delay, "STALE", "warn", True,
                           "connected, but no recent prints")
        return FeedRow(name, role, source, delay, "OK", "on")
    if booting:
        return FeedRow(name, role, source, delay, "CONNECTING", "idle", stale)
    detail = (st.last_error if st is not None else None) or "not connected (reconnecting)"
    return FeedRow(name, role, source, delay, "DOWN", "down", stale, detail)


def _macro_row(macro: mr.MacroSnapshot, key: str, name: str, role: str, source: str) -> FeedRow:
    st = macro.feeds.get(key)
    if st is None or st.last_attempt_at is None:
        return FeedRow(name, role, source, "—", "CHECKING", "idle")
    age = macro.taken_at - st.last_ok_at if st.last_ok_at is not None else None
    delay = _age(age) if age is not None else "—"
    stale_after = 2 * st.cadence_s + macro.tick_s
    if not st.ok:
        waiting = (
            st.retry_after
            and st.next_attempt_at is not None
            and macro.taken_at < st.next_attempt_at + macro.tick_s + MACRO_WAIT_GRACE_S
        )
        if not waiting:
            return FeedRow(name, role, source, delay, "DOWN", "down", False, st.detail)
        # A rate limit that never lifts is an outage: no data for too long (or, never had
        # any, the recorder running that long) is STALE, not a quiet WAIT.
        no_data_for = age if age is not None else macro.taken_at - macro.started_at
        if no_data_for > stale_after:
            return FeedRow(name, role, source, delay, "STALE", "warn", True, st.detail)
        return FeedRow(name, role, source, delay, "WAIT", "idle", False, st.detail)
    if age is not None and age > stale_after:
        return FeedRow(name, role, source, delay, "STALE", "warn", True,
                       f"last success {delay} ago")
    return FeedRow(name, role, source, delay, "OK", "on")


def build_rows(
    snap: fm.FeedsSnapshot | None,
    flow: fr.FlowSnapshot | None = None,
    macro: mr.MacroSnapshot | None = None,
    marketdata: md_hub.MarketDataSnapshot | None = None,
) -> list[FeedRow]:
    if snap is None:
        # Feed monitor not running (only outside the dashboard app).
        names = [("Chainlink BTC/USD", "spot · vol", "Polymarket WS")] + [
            (n, r, s) for _k, n, r, s in _REST_FEEDS
        ]
        rows = [FeedRow(n, r, s, "—", "OFF", "idle") for n, r, s in names]
    else:
        rows = [_ws_row(snap)] + [_rest_row(snap, *feed) for feed in _REST_FEEDS]
    if marketdata is not None:
        rows.append(_books_row(marketdata))
        rows += [_price_row(marketdata, *feed) for feed in _PRICE_FEEDS]
    if flow is not None:
        rows += [_flow_row(flow, *feed) for feed in _FLOW_FEEDS]
    if macro is not None:
        rows += [_macro_row(macro, *feed) for feed in _MACRO_FEEDS]
    return rows


def render(
    snap: fm.FeedsSnapshot | None,
    flow: fr.FlowSnapshot | None = None,
    macro: mr.MacroSnapshot | None = None,
    marketdata: md_hub.MarketDataSnapshot | None = None,
) -> str:
    rows = build_rows(snap, flow, macro, marketdata)
    issues = sum(r.level in ("warn", "down") for r in rows)
    note = "all OK" if not issues else f"{issues} issue{'s' if issues != 1 else ''}"
    body = "".join(
        "<tr>"
        f"<td>{escape(r.name)}</td>"
        f"<td class='feeds-role'>{escape(r.role)}</td>"
        f"<td class='feeds-role'>{escape(r.source)}</td>"
        f"<td class='feeds-delay{' warn' if r.delay_warn else ''}'>{escape(r.delay)}</td>"
        f"<td><span class='feed {r.level}'"
        + (f" title='{escape(r.detail, quote=True)}'" if r.detail else "")
        + f">{escape(r.status)}</span></td>"
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
