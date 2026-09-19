"""FEEDS card: every live upstream feed — how it connects, what it is for, who uses it, health.

Rows come from the always-on feed monitor (``polymarket_exec/ops/feed_monitor.py``),
the market-data hub (``polymarket_exec/marketdata/hub.py``), the venue flow recorder
and the macro recorder, which check every feed directly — so the card is live whether
or not the bot loop is running. Rows are plain data (``FeedRow``) so new venues are one
more row.

Columns: Feed | Connection (how the data arrives and how often; the endpoint on hover)
| Used for | Used by (the components that consume it today) | Delay | Status.

Delay is the age of the latest print for the Chainlink WS stream and the RTDS price
rows, the round-trip time of the latest check for each monitored REST feed, the served
event latency (p50, the slowest market in use) for the Polymarket books, and the age of
the last successful pull for recorder feeds.

Polymarket books are streamed on demand. Their summary row takes its status and delay
from the markets in use only, and lists their owners. Under it, a grid shows every
available market (assets x timeframes): the served latency of a market in use,
"lingering" for one nobody uses any more (its sockets stop soon) and "available" for the
rest; the hover says who uses it, connections up/total, p50/p90 and KiB/s. Lingering and
available markets never count as issues. ``render`` is pure: no network, no awaits.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from html import escape

import config as _config
from polymarket_exec.marketdata import hub as md_hub
from polymarket_exec.marketdata import rtds_stream as rs
from polymarket_exec.ops import feed_monitor as fm
from polymarket_exec.ops import flow_recorder as fr
from polymarket_exec.ops import macro_recorder as mr

# A Gamma lookup error older than this is history, not the reason a market has no tokens.
GAMMA_ERROR_FRESH_S = 120.0

# A REST round trip slower than this is flagged (status stays OK).
SLOW_MS = 2000.0
COLUMNS = 6

# Who consumes a feed today: components, not files.
BOT_LOOP = "bot loop"
TICKET = "order ticket"
SCANNER = "daily scanner"
HUB = "market hub"
LIVE_EXECUTOR = "live executor"
FEEDS_CHECK = "FEEDS check"  # the feed monitor itself: connected and checked, loop or not
FLOW_RECORDER = "flow recorder"
MACRO_RECORDER = "macro recorder"
PRICES_USED_BY = "none yet (kept warm)"  # the hub keeps price history for strategies
NO_OWNER = "none"


def _by(*names: str) -> str:
    return " · ".join(names)


@dataclass(frozen=True)
class GridCell:
    market: str  # e.g. "btc-5m"
    state: str  # md_hub.STREAMING | LINGERING | AVAILABLE
    text: str  # served latency | ok | stale | connecting | down | lingering | available
    level: str  # on | warn | down | idle | linger
    title: str  # hover: who uses it, connections up/total, p50/p90, KiB/s


@dataclass(frozen=True)
class MarketGrid:
    timeframes: tuple[str, ...]
    rows: tuple[tuple[str, tuple[GridCell | None, ...]], ...]  # (asset, cell per timeframe)


@dataclass(frozen=True)
class FeedRow:
    name: str
    role: str  # used for
    source: str  # the endpoint behind the connection (hover)
    delay: str
    status: str
    level: str  # on | warn | down | idle
    delay_warn: bool = False
    detail: str | None = None  # hover text: the error behind a bad status
    connection: str = ""  # how the data arrives and how often
    used_by: str = ""  # the components that consume it today
    grid: MarketGrid | None = None  # Polymarket books: every available market


# The feed monitor's WebSocket row.
_WS_NAME, _WS_ROLE, _WS_SOURCE = "Chainlink BTC/USD", "spot · vol", "RTDS · crypto_prices_chainlink"
_WS_CONNECTION = "WebSocket · RTDS"
_WS_USED_BY = _by(BOT_LOOP, FEEDS_CHECK)

# (probe key, name, used for, endpoint, used by) — order is the card's row order.
# The monitor checks each one every interval_s, as the loop would call it.
_REST_FEEDS = (
    (fm.CHAINLINK_REST, "Chainlink BTC/USD", "window open", "crypto-price API",
     _by(BOT_LOOP, FEEDS_CHECK)),
    (fm.GAMMA, "Polymarket Gamma", "market lookup", "Gamma /markets",
     _by(BOT_LOOP, TICKET, SCANNER, HUB, FEEDS_CHECK)),
    (fm.CLOB_BOOK, "Polymarket book", "UP/DOWN quotes", "CLOB /book",
     _by(BOT_LOOP, TICKET, LIVE_EXECUTOR, FEEDS_CHECK)),
    (fm.BINANCE, "Binance BTCUSDT 1s", "vol backup", "Binance /api/v3/klines 1s",
     _by(BOT_LOOP, FEEDS_CHECK)),
)

# How a flow feed arrives: REST on every recorder pass, REST once an hour, or a WebSocket.
EACH_PASS = "each pass"
ONCE_AN_HOUR = "once an hour"

# (flow feed key, name, used for, endpoint, connection, quiet_ok) — venue flow rows, in
# card order. quiet_ok: silence on a connected socket is normal (sparse fills, liquidations).
_FLOW_FEEDS = (
    (fr.BINANCE_SPOT_BTC, "Binance BTCUSDT 1h", "hourly flow", "Binance /api/v3/klines 1h",
     EACH_PASS, False),
    (fr.BINANCE_SPOT_ETH, "Binance ETHUSDT", "hourly flow", "Binance /api/v3/klines 1h",
     EACH_PASS, False),
    (fr.BINANCE_PERP_BTC, "Binance perp BTCUSDT", "hourly flow", "Binance /fapi/v1/klines 1h",
     EACH_PASS, False),
    (fr.BINANCE_PERP_STATE, "Binance perp BTCUSDT", "funding · OI",
     "Binance premiumIndex · openInterest", ONCE_AN_HOUR, False),
    (fr.BINANCE_LIQ, "Binance liquidations", "liquidation flow", "Binance !forceOrder@arr",
     "WebSocket · Binance futures", True),
    (fr.KRAKEN_SPOT, "Kraken BTC/USD", "hourly flow", "Kraken v2 trade",
     "WebSocket · Kraken", False),
    (fr.KRAKEN_FUTURES, "Kraken PF_XBTUSD", "hourly flow", "Kraken Futures v1 trade",
     "WebSocket · Kraken Futures", True),
    (fr.KRAKEN_FUTURES_STATE, "Kraken PF_XBTUSD", "funding · OI", "Kraken Futures /tickers",
     ONCE_AN_HOUR, False),
)

# (macro source key, name, used for, endpoint) — macro calendar rows, in card order.
_MACRO_FEEDS = (
    (mr.BLS_SCHEDULE, "BLS schedule", "CPI · jobs · PPI times", "bls.gov ICS"),
    (mr.BEA_SCHEDULE, "BEA schedule", "GDP · PCE times", "bea.gov ICS"),
    (mr.CENSUS_SCHEDULE, "Census schedule", "retail sales times", "census.gov calendar"),
    (mr.FED_CALENDAR, "Fed calendar", "FOMC · speeches", "federalreserve.gov JSON"),
    (mr.FF_WEEK, "ForexFactory week", "forecasts · claims", "faireconomy JSON"),
)
_MACRO_CADENCE_S = {source.key: source.cadence_s for source in mr.default_sources()}

# (price source, name, used for) — RTDS reference price rows, in card order.
_PRICE_FEEDS = (
    (rs.CHAINLINK, "Chainlink prices", "spot · vol"),
    (rs.CHAINLINK_TWAP60, "Chainlink 60s TWAP", "5m·15m settle ref"),
    (rs.BINANCE, "Binance prices", "1h·1d settle ref"),
)
_RTDS_CONNECTION = "WebSocket · RTDS"

BOOKS_NAME = "Polymarket books"
BOOKS_SOURCE = "CLOB market channel"
BOOKS_CONNECTION = "WebSocket · CLOB · on demand"
# A market in use whose sockets are connected with no data frame for this long is flagged.
BOOKS_STALE_S = 45.0
# A market in use whose median served latency is above this is serving stale books.
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


def _every(seconds: float) -> str:
    """A cadence in words: 'every 10 s', 'every 2 min', 'hourly', 'every 6 h'."""
    s = int(round(seconds))
    if s == 3600:
        return "hourly"
    if s > 3600 and s % 3600 == 0:
        return f"every {s // 3600} h"
    if s >= 120 and s % 60 == 0:
        return f"every {s // 60} min"
    return f"every {s} s"


def _described(row: FeedRow, connection: str, used_by: str) -> FeedRow:
    return replace(row, connection=connection, used_by=used_by)


def _ws_row(snap: fm.FeedsSnapshot) -> FeedRow:
    name, role, source = _WS_NAME, _WS_ROLE, _WS_SOURCE
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


def _flow_connection(flow: fr.FlowSnapshot, how: str) -> str:
    if how == EACH_PASS:
        return f"REST · {_every(flow.interval_s)}"
    if how == ONCE_AN_HOUR:
        return f"REST · {_every(3600)}"
    return how


# --- Polymarket books: markets in use, and the grid of every available market ----------

@dataclass(frozen=True)
class _Health:
    status: str  # OK | CONNECTING | STALE | DOWN
    level: str  # on | idle | warn | down
    detail: str | None  # what is wrong or notable (without the market's name)


def _market_name(market: md_hub.GridMarket) -> str:
    return md_hub.shard_name(market.asset, market.timeframe)


def _market_health(md: md_hub.MarketDataSnapshot, market: md_hub.GridMarket) -> _Health:
    """How a market in use is served (its own grace period starts when it was wanted)."""
    shard = md.clob_shards.get(_market_name(market))
    since = market.since if market.since is not None else md.started_at
    booting = md.taken_at - since <= WS_CONNECT_GRACE_S
    if market.tokens == 0 or shard is None:
        if booting:
            return _Health("CONNECTING", "idle", "looking up its windows")
        detail = "no market tokens yet"
        recent = (md.gamma_last_error_at is not None
                  and md.taken_at - md.gamma_last_error_at <= GAMMA_ERROR_FRESH_S)
        if md.gamma_errors and recent:
            detail += f" (Gamma lookups failing: {md.gamma_last_error or 'error'})"
        return _Health("DOWN", "down", detail)
    conns = shard.connections
    if market.connections_up == 0:
        if booting:
            return _Health("CONNECTING", "idle", "connecting")
        error = next((c.last_error for c in conns if c.last_error), None)
        return _Health("DOWN", "down", error or "not connected (reconnecting)")
    frames = [c.last_frame_at for c in conns if c.last_frame_at is not None]
    opened = [c.connected_since for c in conns if c.connected and c.connected_since is not None]
    seen = ([max(frames)] if frames else []) + ([min(opened)] if opened else [])
    quiet = md.taken_at - max(seen) if seen else 0.0
    if quiet > BOOKS_STALE_S:
        return _Health("STALE", "warn", f"connected, but no data for {_secs(quiet)}")
    p50 = market.served_latency_ms_p50
    if p50 is not None and p50 > BOOKS_LAG_S * 1000:
        return _Health("STALE", "warn", f"{_secs(p50 / 1000)} behind (served latency)")
    reconnecting = sum(1 for c in conns if not c.connected)
    if reconnecting:
        return _Health("OK", "on", f"{reconnecting} of {len(conns)} connections reconnecting; "
                                   "still served")
    return _Health("OK", "on", None)


def _named(problems: list[tuple[str, _Health]]) -> str:
    name, health = problems[0]
    more = f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""
    return f"{name}: {health.detail}{more}"


def _poll_note(md: md_hub.MarketDataSnapshot) -> str:
    """The fresh REST poll racing the sockets, for the Connection hover."""
    poll = md.rest_poll
    if poll is None or not poll.tokens:
        return ""
    judged = poll.ahead + poll.behind + poll.same
    note = f" · fresh REST poll on {poll.tokens} tokens, {poll.rate_hz:g}/s"
    if judged:
        note += f", ahead of the sockets {100 * poll.ahead / judged:.0f}% of reads"
    if poll.ahead_ms_p50 is not None:
        note += f" (by {_ms(poll.ahead_ms_p50)})"
    if poll.rtt_ms_p50 is not None:
        note += f" · round trip {_ms(poll.rtt_ms_p50)}"
    if poll.errors:
        note += f" · {poll.errors} failed"
    return note


def _books_row(md: md_hub.MarketDataSnapshot) -> FeedRow:
    markets = list(md.grid.values())
    in_use = [m for m in markets if m.state == md_hub.STREAMING]
    lingering = sum(1 for m in markets if m.state == md_hub.LINGERING)
    role = f"Up/Down books · trades ({len(in_use)} of {len(markets)} in use)"
    owners = sorted({owner for m in in_use for owner in m.owners})
    used_by = _by(*owners) if owners else NO_OWNER
    grid = _market_grid(md)
    source = BOOKS_SOURCE + _poll_note(md)

    def row(delay: str, status: str, level: str, warn: bool = False,
            detail: str | None = None) -> FeedRow:
        return FeedRow(BOOKS_NAME, role, source, delay, status, level, warn,
                       detail[:200] if detail else None, BOOKS_CONNECTION, used_by, grid)

    if not in_use:
        detail = f"no market in use; {len(markets) - lingering} available"
        if lingering:
            detail += f", {lingering} lingering"
        return row("—", "IDLE", "idle", detail=detail)
    timed = [(m.served_latency_ms_p50, _market_name(m)) for m in in_use
             if m.served_latency_ms_p50 is not None]
    worst = max(timed) if timed else None
    delay = _ms(worst[0]) if worst is not None else "—"
    slow = worst is not None and worst[0] > SLOW_MS
    health = [(_market_name(m), _market_health(md, m)) for m in in_use]
    down = [item for item in health if item[1].status == "DOWN"]
    if down:
        detail = _named(down)
        if len(down) < len(in_use):
            detail = f"{len(down)} of {len(in_use)} markets in use down; {detail}"
        return row(delay, "DOWN", "down", slow, detail)
    stale = [item for item in health if item[1].status == "STALE"]
    if stale:
        return row(delay, "STALE", "warn", True, _named(stale))
    connecting = [item for item in health if item[1].status == "CONNECTING"]
    if len(connecting) == len(in_use):
        return row(delay, "CONNECTING", "idle", slow, _named(connecting))
    if slow:
        return row(delay, "OK", "on", True, f"slowest: {worst[1]}")
    notes = [item for item in health if item[1].detail]
    return row(delay, "OK", "on", False, _named(notes) if notes else None)


def _opt_ms(v: float | None) -> str:
    return _ms(v) if v is not None else "—"


def _grid_cell(md: md_hub.MarketDataSnapshot, market: md_hub.GridMarket) -> GridCell:
    name = _market_name(market)
    label = f"{market.asset} {market.timeframe}"
    if market.state == md_hub.AVAILABLE:
        return GridCell(name, market.state, "available", "idle",
                        f"{label} · available: not streaming (nobody uses it)")
    stats = (f"connections {market.connections_up}/{market.connections} · "
             f"p50 {_opt_ms(market.served_latency_ms_p50)} · "
             f"p90 {_opt_ms(market.served_latency_ms_p90)} · "
             f"{market.bytes_per_s / 1024:.1f} KiB/s")
    if market.hot:
        stats += " · fresh REST poll"
    if market.state == md_hub.LINGERING:
        left = _secs(market.linger_left_s) if market.linger_left_s is not None else "a moment"
        return GridCell(name, market.state, "lingering", "linger",
                        f"{label} · lingering: nobody uses it; its sockets stop in {left} · "
                        f"{stats}")
    health = _market_health(md, market)
    p50 = market.served_latency_ms_p50
    if health.status == "DOWN":
        text = "down"
    elif health.status == "CONNECTING":
        text = "connecting"
    elif p50 is not None:
        text = _ms(p50)
    else:
        text = "ok" if health.status == "OK" else "stale"
    title = f"{label} · used by {', '.join(market.owners)} · {stats}"
    if health.detail and health.detail != text:
        title += f" · {health.detail}"
    return GridCell(name, market.state, text, health.level, title)


def _market_grid(md: md_hub.MarketDataSnapshot) -> MarketGrid | None:
    if not md.assets or not md.timeframes:
        return None
    rows = []
    for asset in md.assets:
        cells = []
        for timeframe in md.timeframes:
            market = md.grid.get(md_hub.shard_name(asset, timeframe))
            cells.append(None if market is None else _grid_cell(md, market))
        rows.append((asset, tuple(cells)))
    return MarketGrid(tuple(md.timeframes), tuple(rows))


def _price_row(md: md_hub.MarketDataSnapshot, key: str, name: str, role: str) -> FeedRow:
    source = f"RTDS · {rs.TOPICS[key]}"
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


def _macro_connection(macro: mr.MacroSnapshot, key: str) -> str:
    st = macro.feeds.get(key)
    cadence = st.cadence_s if st is not None else _MACRO_CADENCE_S.get(key)
    return f"REST · {_every(cadence)}" if cadence else "REST"


def build_rows(
    snap: fm.FeedsSnapshot | None,
    flow: fr.FlowSnapshot | None = None,
    macro: mr.MacroSnapshot | None = None,
    marketdata: md_hub.MarketDataSnapshot | None = None,
) -> list[FeedRow]:
    interval_s = snap.interval_s if snap is not None else fm.DEFAULT_INTERVAL_S
    checked = f"REST · {_every(interval_s)}"
    if snap is None:
        # Feed monitor not running (only outside the dashboard app).
        rows = [_described(FeedRow(_WS_NAME, _WS_ROLE, _WS_SOURCE, "—", "OFF", "idle"),
                           _WS_CONNECTION, _WS_USED_BY)]
        rows += [_described(FeedRow(name, role, source, "—", "OFF", "idle"), checked, used_by)
                 for _key, name, role, source, used_by in _REST_FEEDS]
    else:
        rows = [_described(_ws_row(snap), _WS_CONNECTION, _WS_USED_BY)]
        rows += [_described(_rest_row(snap, key, name, role, source), checked, used_by)
                 for key, name, role, source, used_by in _REST_FEEDS]
    if marketdata is not None:
        rows.append(_books_row(marketdata))
        rows += [_described(_price_row(marketdata, key, name, role), _RTDS_CONNECTION,
                            PRICES_USED_BY)
                 for key, name, role in _PRICE_FEEDS]
    if flow is not None:
        rows += [_described(_flow_row(flow, key, name, role, source, quiet_ok),
                            _flow_connection(flow, how), FLOW_RECORDER)
                 for key, name, role, source, how, quiet_ok in _FLOW_FEEDS]
    if macro is not None:
        rows += [_described(_macro_row(macro, key, name, role, source),
                            _macro_connection(macro, key), MACRO_RECORDER)
                 for key, name, role, source in _MACRO_FEEDS]
    return rows


def _cell_html(cell: GridCell | None) -> str:
    if cell is None:
        return "<td class='feeds-grid-none'>—</td>"
    return (f"<td><span class='feed {cell.level}' title='{escape(cell.title, quote=True)}'>"
            f"{escape(cell.text)}</span></td>")


def _grid_html(grid: MarketGrid) -> str:
    head = "".join(f"<th>{escape(timeframe)}</th>" for timeframe in grid.timeframes)
    body = "".join(
        f"<tr><th>{escape(asset.upper())}</th>{''.join(_cell_html(c) for c in cells)}</tr>"
        for asset, cells in grid.rows
    )
    return (
        f"<tr class='feeds-grid-row'><td colspan='{COLUMNS}'><table class='feeds-grid'>"
        f"<thead><tr><th></th>{head}</tr></thead><tbody>{body}</tbody></table></td></tr>"
    )


def _row_html(r: FeedRow) -> str:
    source = f" title='{escape(r.source, quote=True)}'" if r.source else ""
    detail = f" title='{escape(r.detail, quote=True)}'" if r.detail else ""
    html = (
        "<tr>"
        f"<td>{escape(r.name)}</td>"
        f"<td class='feeds-conn'{source}>{escape(r.connection or '—')}</td>"
        f"<td class='feeds-role'>{escape(r.role)}</td>"
        f"<td class='feeds-role'>{escape(r.used_by or '—')}</td>"
        f"<td class='feeds-delay{' warn' if r.delay_warn else ''}'>{escape(r.delay)}</td>"
        f"<td><span class='feed {r.level}'{detail}>{escape(r.status)}</span></td>"
        "</tr>"
    )
    return html + (_grid_html(r.grid) if r.grid is not None else "")


def render(
    snap: fm.FeedsSnapshot | None,
    flow: fr.FlowSnapshot | None = None,
    macro: mr.MacroSnapshot | None = None,
    marketdata: md_hub.MarketDataSnapshot | None = None,
) -> str:
    rows = build_rows(snap, flow, macro, marketdata)
    issues = sum(r.level in ("warn", "down") for r in rows)
    note = "all OK" if not issues else f"{issues} issue{'s' if issues != 1 else ''}"
    return (
        "<section class='card feeds-card'>"
        f"<div class='card-h'>FEEDS<span class='win{' warn' if issues else ''}'>{note}</span></div>"
        "<div class='feeds-scroll'><table class='de-tail-tbl feeds-tbl'>"
        "<thead><tr><th>Feed</th><th>Connection</th><th>Used for</th><th>Used by</th>"
        "<th class='feeds-delay'>Delay</th><th>Status</th></tr></thead>"
        f"<tbody>{''.join(_row_html(r) for r in rows)}</tbody></table></div>"
        "</section>"
    )
