"""FEEDS card: every live upstream feed — how it connects, what it is for, who uses it, health.

Rows come from the market-data hub (``polymarket_exec/marketdata/hub.py``), which checks
every feed directly. Rows are plain data (``FeedRow``) so new sources are one more row.

Columns: Feed | Connection (how the data arrives and how often; the endpoint on hover)
| Used for | Used by (the components that consume it today) | Delay | Status.

Delay is the age of the latest print for the RTDS price rows and the served event
latency (p50, the slowest market in use) for the Polymarket books.

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

from polymarket_exec.marketdata import hub as md_hub
from polymarket_exec.marketdata import rtds_stream as rs

# A Gamma lookup error older than this is history, not the reason a market has no tokens.
GAMMA_ERROR_FRESH_S = 120.0

# A REST round trip slower than this is flagged (status stays OK).
SLOW_MS = 2000.0
COLUMNS = 6

# Who consumes a feed today: components, not files.
FADE_1H = "Fade 1h Momentum on 15m"
# The hub keeps price history for strategies. Fade 1h Momentum on 15m reads all three: the
# Chainlink price as the price now, the TWAP-60s prints for the price to beat and the closing
# minute, and Binance to compare.
PRICES_USED_BY = FADE_1H
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


def _described(row: FeedRow, connection: str, used_by: str) -> FeedRow:
    return replace(row, connection=connection, used_by=used_by)


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



def build_rows(marketdata: md_hub.MarketDataSnapshot | None) -> list[FeedRow]:
    if marketdata is None:
        # Hub not running (only outside the dashboard app).
        return [_described(FeedRow(BOOKS_NAME, "Up/Down books · trades", BOOKS_SOURCE, "—",
                                   "OFF", "idle"), BOOKS_CONNECTION, NO_OWNER)]
    rows = [_books_row(marketdata)]
    rows += [_described(_price_row(marketdata, key, name, role), _RTDS_CONNECTION,
                        PRICES_USED_BY)
             for key, name, role in _PRICE_FEEDS]
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


def render(marketdata: md_hub.MarketDataSnapshot | None) -> str:
    rows = build_rows(marketdata)
    issues = sum(r.level in ("warn", "down") for r in rows)
    note = "all OK" if not issues else f"{issues} issue{'s' if issues != 1 else ''}"
    # A <details> fold: dashboard.js stores open/closed by ``data-fold`` and
    # re-applies it after every refresh swaps this HTML out.
    return (
        "<details class='card feeds-card fold' data-fold='feeds' open>"
        "<summary class='card-h'><span class='fold-title'>FEEDS</span>"
        f"<span class='win{' warn' if issues else ''}'>{note}</span></summary>"
        "<div class='feeds-scroll'><table class='de-tail-tbl feeds-tbl'>"
        "<thead><tr><th>Feed</th><th>Connection</th><th>Used for</th><th>Used by</th>"
        "<th class='feeds-delay'>Delay</th><th>Status</th></tr></thead>"
        f"<tbody>{''.join(_row_html(r) for r in rows)}</tbody></table></div>"
        "</details>"
    )
