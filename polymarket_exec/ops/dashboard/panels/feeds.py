"""FEEDS card: one row per live upstream feed — what it feeds, source, delay, status.

Rows come from the always-on feed monitor (``polymarket_exec/ops/feed_monitor.py``),
which checks every feed directly — so the card is live whether or not the bot
loop is running. Rows are plain data (``FeedRow``) so new venues are one more row.

Delay is the age of the latest print for the Chainlink WS stream, and the
round-trip time of the latest check for each REST feed.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape

import config as _config
from polymarket_exec.ops import feed_monitor as fm
from polymarket_exec.ops import flow_recorder as fr

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
# A connected trade socket with no frame for this long is flagged (or QUIET if normal).
WS_STALE_S = 120.0
# A socket that has not connected yet this soon after start is "connecting", not down.
WS_CONNECT_GRACE_S = 30.0


def _secs(v: float) -> str:
    if v < 10:
        return f"{v:.1f}s"
    if v < 60:
        return f"{int(v)}s"
    return f"{int(v) // 60}m{int(v) % 60:02d}s"


def _ms(v: float) -> str:
    return f"{v:.0f}ms" if v < 1000 else _secs(v / 1000)


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


def build_rows(
    snap: fm.FeedsSnapshot | None, flow: fr.FlowSnapshot | None = None
) -> list[FeedRow]:
    if snap is None:
        # Feed monitor not running (only outside the dashboard app).
        names = [("Chainlink BTC/USD", "spot · vol", "Polymarket WS")] + [
            (n, r, s) for _k, n, r, s in _REST_FEEDS
        ]
        rows = [FeedRow(n, r, s, "—", "OFF", "idle") for n, r, s in names]
    else:
        rows = [_ws_row(snap)] + [_rest_row(snap, *feed) for feed in _REST_FEEDS]
    if flow is not None:
        rows += [_flow_row(flow, *feed) for feed in _FLOW_FEEDS]
    return rows


def render(snap: fm.FeedsSnapshot | None, flow: fr.FlowSnapshot | None = None) -> str:
    rows = build_rows(snap, flow)
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
