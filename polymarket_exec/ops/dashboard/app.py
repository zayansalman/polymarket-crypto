"""FastAPI dashboard for the local Polymarket crypto trading lab.

Replaces the 150MB+ Gradio dashboard with a lightweight FastAPI + Jinja2
implementation. All visual design is preserved via extracted CSS. Also
starts the daily altcoin scanner (#185) as a background task for its
lifetime — see ``_lifespan`` — a paper-only strategy independent of the
BTC 5m loop the rest of this module's endpoints control.

Endpoints:
    GET  /              — Main dashboard page (HTML)
    POST /api/start     — Start the trading bot (paper by default; LIVE when
                          BOT_MODE=live and the boot gates pass)
    POST /api/stop      — Stop the trading bot (live mode flattens first)
    GET  /api/data      — Full dashboard data as JSON
    GET  /api/stream    — Server-Sent Events for live updates
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path so top-level modules (config, db, …)
# can be imported when this package is run directly.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import (  # type: ignore[import-untyped]
    BOT_MODE,
    CHAINLINK_STREAM_URL,
    TRADE_MAX_USD,
    KILL_SWITCH_PATH,
    HISTORY_CSV_PATH,
    DASHBOARD_SERVER_NAME,
    DASHBOARD_SERVER_PORT,
    DATA_DIR,
    DB_PATH,
)
from db import connect, init_db  # type: ignore[import-untyped]
from polymarket_bot import runtime_knobs as _knobs
from logging_setup import get_logger  # type: ignore[import-untyped]
from polymarket_exec.ops.dashboard.execution_view import (  # type: ignore[import-untyped]
    execution_view_html,
    market_selector_html,
)
from polymarket_exec.ops.dashboard.panels import _data as _panel_data

log = get_logger("dashboard")

_IS_LIVE = BOT_MODE == "live"
_MODE_BANNER = (
    "LIVE — orders are real. Risk-gated CLOB orders are placed on Polymarket."
    if _IS_LIVE
    else "Paper — no live orders are placed in this mode."
)

# Lazily import polymarket_bot modules (may not be available in test environments)
try:
    from polymarket_bot.controller import (  # type: ignore[import-untyped]
        current_mode,
        get_status,
        live_consented,
        request_start,
        request_stop,
        set_mode,
    )
    from polymarket_bot.history import load_btc_history_stats  # type: ignore[import-untyped]
    from polymarket_bot.paper import load_paper_summary  # type: ignore[import-untyped]
    from polymarket_bot.backtest import format_report  # type: ignore[import-untyped]
    from polymarket_exec.execution.live import (  # type: ignore[import-untyped]
        live_boot_problems,
    )

    _BTC_BOT_AVAILABLE = True
except Exception:
    _BTC_BOT_AVAILABLE = False
    log.warning("polymarket_bot modules not available; dashboard running in mock mode")

# ---------------------------------------------------------------------------
# Lifespan — init DB tables on startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    await init_db()
    # One-shot (#76): clear a stale paper-era loss-halt bypass so live starts
    # halt-ON now that the bypass applies to real money. Idempotent via sentinel.
    from polymarket_exec.execution.gate import migrate_clear_stale_bypass_v76
    await migrate_clear_stale_bypass_v76()

    # Daily altcoin scanner (#185): auto-runs in-process for the dashboard's
    # lifetime — paper-only, no live gate, so no Start/Stop control needed
    # (unlike the BTC loop, which controller.py starts/stops explicitly).
    from polymarket_bot.daily.scanner import run_forever as _run_daily_scanner

    daily_stop_event = asyncio.Event()
    daily_task = asyncio.create_task(_run_daily_scanner(daily_stop_event))

    # Order-size ticket quotes: polls the selected market's book only while a
    # dashboard is open (demand-driven), independent of the trading loop.
    from polymarket_exec.ops.dashboard import quote_feed

    quote_stop_event = asyncio.Event()
    quote_task = asyncio.create_task(quote_feed.run_forever(quote_stop_event))

    yield

    for stop_event, task in (
        (daily_stop_event, daily_task),
        (quote_stop_event, quote_task),
    ):
        stop_event.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

dashboard_dir = Path(__file__).parent

app = FastAPI(title="Polymarket Crypto Trading Lab", lifespan=_lifespan)

# Same-origin dashboard: no wildcard CORS. With "*" any web page could read the
# page (and its LIVE token) and drive real-money endpoints cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        f"http://127.0.0.1:{DASHBOARD_SERVER_PORT}",
        f"http://localhost:{DASHBOARD_SERVER_PORT}",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Dashboard-Token"],
)

# Per-process token rendered into the page. Selecting LIVE and starting a LIVE
# loop require it, so real-money consent is the dashboard click — not any HTTP
# client that can reach the port.
_DASHBOARD_TOKEN = secrets.token_urlsafe(32)


def _has_dashboard_token(request: Request) -> bool:
    return secrets.compare_digest(
        request.headers.get("x-dashboard-token", ""), _DASHBOARD_TOKEN
    )

app.mount("/static", StaticFiles(directory=str(dashboard_dir / "static")), name="static")

templates = Jinja2Templates(directory=str(dashboard_dir / "templates"))

# Cache-bust static JS by its mtime so a code change is always picked up — the
# browser otherwise caches /static/dashboard.js across server restarts, leaving
# new functions (e.g. setActiveModel) undefined on a stale page.
try:
    _STATIC_VERSION = str(int(max(
        (dashboard_dir / "static" / name).stat().st_mtime
        for name in ("dashboard.js", "style.css")
    )))
except OSError:
    _STATIC_VERSION = "1"

# ---------------------------------------------------------------------------
# Formatting helpers (ported from original dashboard.py)
# ---------------------------------------------------------------------------


def _money(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}${value:,.2f}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _fmt_cap(cap: float | None) -> str:
    """Render an optional risk-limit cap: dollar amount when set, 'disabled' when None."""
    return f"${cap:.2f}" if cap is not None else "disabled"


def _fmt_relative(ts: str | None) -> str:
    if not ts:
        return "never"
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
    except ValueError:
        return ts
    age = max(0, int((datetime.now(UTC) - parsed).total_seconds()))
    if age < 60:
        return f"{age}s ago"
    if age < 3600:
        return f"{age // 60}m ago"
    return f"{age // 3600}h ago"


def _kpi_card(label: str, value: str, hint: str = "") -> str:
    return (
        "<div class='metric'>"
        f"<div class='label'>{escape(label)}</div>"
        f"<div class='value'>{escape(value)}</div>"
        f"<div class='hint'>{escape(hint)}</div>"
        "</div>"
    )


def _state_badge(state: str, risk_state: str) -> str:
    state_l = (state or "").lower()
    risk_l = (risk_state or "").lower()
    if state_l == "running" and risk_l.startswith("ok"):
        cls = "ok"
    elif state_l == "running":
        cls = "warn"
    elif "breach" in risk_l or "stale" in risk_l:
        cls = "stop"
    else:
        cls = "warn"
    return f"<span class='badge {cls}'>{escape(state.upper())}</span>"


def _pnl_class(value: float | None) -> str:
    if value is None or value == 0:
        return ""
    return "positive" if value > 0 else "negative"


# ---------------------------------------------------------------------------
# Async data loaders
# ---------------------------------------------------------------------------


async def _load_feed(limit: int = 18) -> list[dict[str, Any]]:
    """Notification feed merged with today's BLOCKED order intents.

    BLOCKED entries used to get their own "last 5" column on the RISK
    GUARDRAILS card; they now flow through the same activity feed so
    silent-stop conditions show up alongside everything else, in order.
    """
    async with connect() as db:
        async with db.execute(
            """
            SELECT created_at, event_type, message, details_json
            FROM notification_feed
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = [dict(row) for row in await cur.fetchall()]
    for r in await _panel_data.recent_blocked(limit=5):
        reason = (r.get("error") or "").strip() or "risk gate"
        mode_tag = f"[{(r.get('mode') or 'live').lower()}] "
        rows.append(
            {
                "created_at": r.get("created_at"),
                "event_type": "blocked",
                "message": f"{mode_tag}{r.get('intent') or 'entry'} blocked — {reason}",
                "details_json": None,
            }
        )
    rows.sort(key=lambda r: r["created_at"] or "", reverse=True)
    return rows[:limit]


async def _get_status_safe() -> Any:
    """Get controller status, returning a mock if polymarket_bot is unavailable."""
    if _BTC_BOT_AVAILABLE:
        return await get_status()
    # Mock status for testing / when polymarket_bot is not available
    class _MockStatus:
        state = "stopped"
        mode = BOT_MODE
        updated_at = None
        detail = f"BTC 5-minute bot is ready. Mode: {_MODE_BANNER}"
    return _MockStatus()


async def _get_paper_safe() -> Any:
    """Get paper summary, returning a mock if polymarket_bot is unavailable."""
    if _BTC_BOT_AVAILABLE:
        return await load_paper_summary()
    # Mock paper summary for testing
    class _MockPaper:
        risk_state = "IDLE: no ticks yet"
        open_positions = 0
        closed_positions = 0
        total_pnl_usd = 0.0
        open_exposure_usd = 0.0
        closed_notional_usd = 0.0
        win_rate = None
        avg_pnl_usd = None
        avg_hold_seconds = None
        last_signal = "none"
        last_tick_at = None
        last_window_slug = None
        last_spot_price = None
        last_fair_up_prob = None
        last_up_price = None
        last_edge = None
        last_feed_source = None
        recent_positions: list[dict[str, Any]] = []
    return _MockPaper()


# ---------------------------------------------------------------------------
# HTML generators (ported from original dashboard.py)
# ---------------------------------------------------------------------------


def _position_cards(positions: list[dict[str, Any]]) -> str:
    if not positions:
        return "<div class='note'>No paper positions yet. Start the bot to let it observe a BTC 5m window.</div>"
    cards: list[str] = []
    for pos in positions:
        pnl = pos.get("realized_pnl_usd")
        pnl_text = "open" if pnl is None else _money(float(pnl), signed=True)
        pnl_cls = _pnl_class(float(pnl)) if pnl is not None else ""
        closed = _fmt_relative(pos.get("closed_at")) if pos.get("closed_at") else "open"
        cards.append(
            "<div class='position-card'>"
            f"<b>{escape(pos['side'])}</b> "
            f"<span class='mono'>{escape(pos['window_slug'])}</span><br>"
            f"State: {escape(pos['state'])} | Opened: {_fmt_relative(pos.get('opened_at'))} | Closed: {closed}<br>"
            f"Entry: {float(pos['entry_price']):.3f} | Exit: {pos.get('exit_price') or 'n/a'} | "
            f"Notional: {_money(float(pos['notional_usd']))}<br>"
            f"PnL: <span class='{pnl_cls}'>{pnl_text}</span> | Reason: {escape(str(pos.get('exit_reason') or 'holding'))}"
            "</div>"
        )
    return "<div class='positions'>" + "\n".join(cards) + "</div>"


async def _overview_html() -> str:
    status = await _get_status_safe()
    paper = await _get_paper_safe()
    badge = _state_badge(status.state, paper.risk_state)
    pnl_cls = _pnl_class(paper.total_pnl_usd)
    return (
        "<div class='grid'>"
        f"{_kpi_card('Bot state', status.state.upper(), f'mode: {status.mode}')}"
        f"{_kpi_card('Risk state', paper.risk_state, _fmt_relative(paper.last_tick_at))}"
        f"{_kpi_card('Open exposure', _money(paper.open_exposure_usd), f'{paper.open_positions} open position(s)')}"
        f"{_kpi_card('Closed trades', str(paper.closed_positions), f'win rate {_pct(paper.win_rate)}')}"
        f"<div class='metric'><div class='label'>Paper PnL</div>"
        f"<div class='value {pnl_cls}'>{_money(paper.total_pnl_usd, signed=True)}</div>"
        f"<div class='hint'>closed notional {_money(paper.closed_notional_usd)}</div></div>"
        f"{_kpi_card('Last signal', paper.last_signal[:90], paper.last_window_slug or 'no market tick yet')}"
        "</div>"
        "<div class='panel'>"
        f"{badge} <span class='mono'>DB {escape(str(DB_PATH))}</span>"
        f"<p class='status-detail'>{escape(status.detail)}</p>"
        "</div>"
    )


async def _status_markdown() -> str:
    status = await _get_status_safe()
    paper = await _get_paper_safe()
    return (
        "<h3>Controller</h3>\n"
        "<ul>\n"
        f"<li>State: <strong>{status.state.upper()}</strong></li>\n"
        f"<li>Mode: <strong>{status.mode}</strong></li>\n"
        f"<li>Updated: <code>{status.updated_at or 'not yet'}</code></li>\n"
        f"<li>Risk: <strong>{paper.risk_state}</strong></li>\n"
        "</ul>\n"
        f"<p>{escape(status.detail)}</p>\n"
        + (
            "<p><em><strong>LIVE — orders are real.</strong> Start places risk-gated "
            "orders on the Polymarket CLOB. Stop cancels resting orders and "
            f"flattens the open position. Kill switch: <code>{escape(str(KILL_SWITCH_PATH))}</code>."
            "</em></p>"
            if _IS_LIVE
            else "<p><em>Start runs the paper loop only — no live orders in paper mode. "
            "Stop prevents new entries immediately and force-closes any open "
            "simulated position.</em></p>"
        )
    )


async def _paper_html() -> str:
    paper = await _get_paper_safe()
    avg_hold = "n/a" if paper.avg_hold_seconds is None else f"{paper.avg_hold_seconds:.0f}s"
    last_edge = "n/a" if paper.last_edge is None else f"{paper.last_edge:+.3f}"
    last_fair = "n/a" if paper.last_fair_up_prob is None else f"{paper.last_fair_up_prob:.1%}"
    last_up = "n/a" if paper.last_up_price is None else f"{paper.last_up_price:.3f}"
    min_trade = await _knobs.get("paper_min_trade_usd")
    max_trade = await _knobs.get("paper_max_trade_usd")
    return (
        "<div class='grid'>"
        f"{_kpi_card('Last tick', _fmt_relative(paper.last_tick_at), paper.last_feed_source or 'no feed yet')}"
        f"{_kpi_card('Spot', 'n/a' if paper.last_spot_price is None else f'${paper.last_spot_price:,.2f}', paper.last_window_slug or 'no window yet')}"
        f"{_kpi_card('Fair Up', last_fair, f'market up {last_up}')}"
        f"{_kpi_card('Edge', last_edge, 'no strategy loaded')}"
        f"{_kpi_card('Avg PnL', _money(paper.avg_pnl_usd, signed=True), f'avg hold {avg_hold}')}"
        f"{_kpi_card('Sizing', f'${min_trade:.0f}-${max_trade:.0f}', 'paper clip range')}"
        "</div>"
        "<div class='panel'><h3>Recent Paper Positions</h3>"
        f"{_position_cards(paper.recent_positions)}"
        "</div>"
    )


# Activity-log colour groups. Anything unlisted renders as "other" (neutral).
_FEED_KIND = {
    "system_start": "system",
    "paper_started": "system",
    "paper_stopped": "system",
    "live_started": "system",
    "paper_entry": "trade",
    "live_entry": "trade",
    "paper_exit": "trade",
    "live_exit": "trade",
    "runtime_config": "config",
    "loss_halt_bypass": "config",
    "loss_halt_reset": "config",
    "paper_halt_pause": "warn",
    "loss_halt_stop": "warn",
    "loop_watchdog_restart": "warn",
    "live_reconciled": "warn",
    "live_positions_left_open": "warn",
    "blocked": "alert",
    "silent_stop": "alert",
    "live_boot_refused": "alert",
    "loop_watchdog_stall_live": "alert",
    "live_kill_switch": "alert",
}


async def _activity_html() -> str:
    try:
        rows = await _load_feed()
    except Exception:
        rows = []
    if not rows:
        return "<p><em>No BTC bot activity yet. Press Start to begin paper trading.</em></p>"
    lines = ['<ul class="feed-list">']
    for row in rows:
        stamp = _fmt_relative(row["created_at"])
        kind = _FEED_KIND.get(row["event_type"], "other")
        event = row["event_type"].replace("_", " ")
        lines.append(
            f"<li class='k-{kind}'><code>{escape(stamp)}</code> "
            f"<strong>{escape(event)}</strong> — {escape(row['message'])}</li>"
        )
    lines.append("</ul>")
    return "\n".join(lines)


def _history_html() -> str:
    if _BTC_BOT_AVAILABLE:
        stats = load_btc_history_stats()
    else:
        class _MockStats:
            found = False
            path = str(HISTORY_CSV_PATH)
        stats = _MockStats()

    if not stats.found:
        return (
            "<h3>Historical Trade Baseline</h3>\n"
            f"<p>Optional CSV not found at <code>{HISTORY_CSV_PATH}</code>. The bot still runs; "
            "the CSV only helps explain why the lab sizes paper trades at $1-$5.</p>"
        )
    return (
        "<h3>Historical Trade Baseline</h3>\n"
        "<ul>\n"
        f"<li>Source: <code>{stats.path}</code></li>\n"
        f"<li>BTC rows: <strong>{stats.btc_rows}</strong> of {stats.total_rows}</li>\n"
        f"<li>Buys / sells / redeems: <strong>{stats.buys} / {stats.sells} / {stats.redeems}</strong></li>\n"
        f"<li>Average buy size: <strong>${stats.buy_usdc_avg:.2f}</strong></li>\n"
        f"<li>Median buy size: <strong>${stats.buy_usdc_median:.2f}</strong></li>\n"
        f"<li>Share of buys sized $1-$5: <strong>{stats.one_to_five_buy_share:.0%}</strong></li>\n"
        "</ul>"
    )


def _brief_html() -> str:
    return (
        "<h3>System Brief</h3>\n"
        "<p>This is a local BTC 5-minute binary pricing-model strategy lab. It is useful "
        "as a personal paper bot and as a compact example of trading-system "
        "discipline: market discovery, feed labeling, confidence-based sizing, "
        "one-position risk control, structured event logs, and a dashboard kill "
        "switch.</p>\n"
        + (
            "<p><strong>Mode: LIVE — orders are real.</strong> Start places risk-gated "
            "limit orders on the Polymarket CLOB (per-trade cap "
            f"${TRADE_MAX_USD:.2f}, daily loss halt ${_knobs.cached('live_daily_loss_halt_usd'):.2f}, "
            f"bankroll cap {_fmt_cap(_knobs.cached('live_bankroll_cap_usd') or None)}); Stop cancels and flattens. "
            f"Kill switch file: <code>{escape(str(KILL_SWITCH_PATH))}</code>.</p>"
            if _IS_LIVE
            else "<p>Mode: paper — this mode does not sign or submit live orders. The active "
            "workflow is simple: <strong>Start</strong> begins simulated BTC 5m trading, and "
            "<strong>Stop</strong> halts new entries and closes any open simulated position.</p>"
        )
    )


def _scorecard_html() -> str:
    return (
        "<h3>Trading Systems Scorecard</h3>\n"
        "<ul>\n"
        "<li>Scope: BTC 5-minute Up/Down markets only.</li>\n"
        "<li>Operator control: Start, Stop, Refresh, and activity feed.</li>\n"
        "<li>Risk: one open paper position, bounded $1-$5 sizing, target/stop/time exits.</li>\n"
        "<li>Feed discipline: public BTC fallback is labeled; Chainlink Streams is the intended reference.</li>\n"
        "<li>Auditability: ticks, simulated positions, exits, config state, and notifications persist to SQLite.</li>\n"
        "<li>Failure visibility: market/feed/loop errors surface in logs and dashboard state.</li>\n"
        "</ul>"
    )


def _settings_html() -> str:
    return (
        "<h3>BTC 5m Paper Rules</h3>\n"
        "<p class='dim'>Every value below is a live, dashboard-editable knob — see the "
        "SETTINGS card on the BTC 5m tab to change one. This list just reflects the "
        "current values.</p>\n"
        "<ul>\n"
        f"<li>Market scope: BTC Up/Down 5-minute windows only.</li>\n"
        "<li>Strategy: <strong>none loaded</strong> (v0 archived 2026-09-13).</li>\n"
        f"<li>Paper sizing: <strong>${_knobs.cached('paper_min_trade_usd'):.0f}-${_knobs.cached('paper_max_trade_usd'):.0f}</strong>.</li>\n"
        f"<li>Tick cadence: <strong>{_knobs.cached('paper_tick_seconds'):.0f}s</strong>.</li>\n"
        f"<li>Target / stop return: <strong>{_knobs.cached('paper_target_return'):.0%} / {_knobs.cached('paper_stop_return'):.0%}</strong>.</li>\n"
        f"<li>Time exit: <strong>{_knobs.cached('paper_time_exit_seconds')}s</strong>.</li>\n"
        f"<li>Settlement-aware reference target: {CHAINLINK_STREAM_URL}</li>\n"
        "</ul>\n"
        + (
            "<p><strong>Mode: LIVE — orders are real.</strong> Live limits: per-trade cap "
            f"<strong>${TRADE_MAX_USD:.2f}</strong>, daily loss halt "
            f"<strong>${_knobs.cached('live_daily_loss_halt_usd'):.2f}</strong>, session bankroll cap "
            f"<strong>{_fmt_cap(_knobs.cached('live_bankroll_cap_usd') or None)}</strong>, max 1 open position, "
            f"kill switch <code>{escape(str(KILL_SWITCH_PATH))}</code>.</p>"
            if _IS_LIVE
            else "<p>Required local env vars are optional for paper mode except path overrides. "
            "No private key is used in paper mode.</p>"
        )
    )


def _backtest_html() -> str:
    report_path = DATA_DIR / "backtests" / "latest.json"
    if not report_path.exists():
        return (
            "<h3>BTC 5m Binary Pricing Model Backtest</h3>\n"
            "<p>No local report yet. Run:</p>\n"
            '<pre><code>./.venv/bin/python tools/backtest_btc_strategy.py</code></pre>'
        )
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if _BTC_BOT_AVAILABLE:
            return format_report(report)
        # Fallback rendering when polymarket_bot.backtest is unavailable
        baseline = report.get("baseline", {})
        current = report.get("current", {})
        best = report.get("best", {})
        lines = [
            "<h2>BTC 5m Binary Pricing Model Backtest</h2>",
            "<ul>",
            f"<li>Opportunities: {report.get('opportunities', 'N/A')}</li>",
            f"<li>Method: {report.get('method', 'N/A')}</li>",
            "</ul>",
            "<h3>Results</h3>",
            "<ul>",
            f"<li>All historical buys: trades={baseline.get('trades', 'N/A')}, pnl=${baseline.get('total_pnl_usd', 0):+.2f}, roi={baseline.get('roi', 0):.1%}</li>",
            f"<li>Current defaults: trades={current.get('trades', 'N/A')}, pnl=${current.get('total_pnl_usd', 0):+.2f}, roi={current.get('roi', 0):.1%}</li>",
            f"<li>Optimized filter: trades={best.get('trades', 'N/A')}, pnl=${best.get('total_pnl_usd', 0):+.2f}, roi={best.get('roi', 0):.1%}</li>",
            "</ul>",
            "<h3>Optimized Parameters</h3>",
            "<ul>",
        ]
        for key, value in best.get("params", {}).items():
            lines.append(f"<li>{key}: {value}</li>")
        lines.append("</ul>")
        return "\n".join(lines)
    except Exception as e:
        return f"<h3>Backtest Error</h3><p>Failed to load report: {escape(str(e))}</p>"


# ---------------------------------------------------------------------------
# Aggregated data helpers
# ---------------------------------------------------------------------------


async def _get_overview_data() -> dict[str, str]:
    """Return overview data as a dict with 'html' and 'status' keys."""
    return {
        "html": await _overview_html(),
        "status": await _status_markdown(),
    }


async def _get_paper_data() -> dict[str, str]:
    return {"html": await _paper_html()}


async def _execution_view_safe() -> str:
    """Render the execution view; never let a dashboard error touch the trading loop."""
    try:
        return await execution_view_html()
    except Exception as e:  # noqa: BLE001
        log.warning("execution_view_render_failed", error=str(e))
        return f"<div class='execution-view'><div class='card'>Execution view error: {escape(str(e))}</div></div>"


async def _market_selector_safe() -> str:
    """Render the topbar market selector; a render error must not break the page."""
    try:
        return await market_selector_html()
    except Exception as e:  # noqa: BLE001
        log.warning("market_selector_render_failed", error=str(e))
        return ""


async def _get_activity_data() -> str:
    return await _activity_html()


def _get_history_data() -> str:
    return _history_html()


def _get_backtest_data() -> str:
    return _backtest_html()


def _get_settings_data() -> str:
    return _settings_html()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> Any:
    """Main dashboard page."""
    mode, live_armed, live_hint = await _mode_context()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "execution_view": await _execution_view_safe(),
            "market_selector": await _market_selector_safe(),
            "activity": await _get_activity_data(),
            "backtest": _get_backtest_data(),
            "mode": mode,
            "live_armed": live_armed,
            "live_hint": live_hint,
            "dashboard_token": _DASHBOARD_TOKEN,
            "static_version": _STATIC_VERSION,
        },
    )


def _live_armed() -> tuple[bool, str]:
    """(armed, hint) — whether the live boot gate passes right now."""
    if not _BTC_BOT_AVAILABLE:
        return False, "polymarket_bot unavailable"
    problems = live_boot_problems()
    if not problems:
        return True, "LIVE — real CLOB orders with real funds"
    return False, "not armed: " + "; ".join(problems)


async def _mode_context() -> tuple[str, bool, str]:
    """(active mode, live armed, LIVE button hint) for the mode toggle.

    LIVE is never disabled — armed only changes the hint and confirm text.
    The boot gate blocks real orders at Start, not the mode switch.
    """
    armed, hint = _live_armed()
    mode = await current_mode() if _BTC_BOT_AVAILABLE else BOT_MODE
    if mode == "live" and armed and not live_consented():
        hint = "LIVE selected earlier — click LIVE again before Start"
    return mode, armed, hint


@app.post("/api/mode")
async def api_mode(request: Request) -> dict[str, Any]:
    """Switch execution mode (paper/live) and restart the loop cleanly."""
    if not _BTC_BOT_AVAILABLE:
        return {"status": "error", "detail": "polymarket_bot not available"}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    mode = (body or {}).get("mode", "")
    if mode not in ("paper", "live"):
        return {"status": "error", "detail": f"invalid mode {mode!r}"}
    if mode == "live" and not _has_dashboard_token(request):
        return {
            "status": "error",
            "detail": "LIVE can only be selected by clicking LIVE in the dashboard.",
        }
    try:
        status = await set_mode(mode)
        armed, hint = _live_armed()
        return {
            "status": status.state,
            "mode": status.mode,
            "detail": status.detail,
            "live_armed": armed,
            "live_hint": hint,
        }
    except Exception as e:  # noqa: BLE001
        log.exception("btc.set_mode_failed", error=str(e))
        return {"status": "error", "detail": f"Mode switch failed: {e}"}


@app.post("/api/start")
async def api_start(request: Request) -> dict[str, str]:
    """Start the trading bot — paper by default, LIVE (real orders) only after
    the operator clicked LIVE in this dashboard session and every boot gate
    passes. A LIVE start must come from the dashboard page (token)."""
    try:
        if _BTC_BOT_AVAILABLE:
            if await current_mode() == "live" and not _has_dashboard_token(request):
                return {
                    "status": "error",
                    "detail": "LIVE Start must be pressed in the dashboard.",
                }
            status = await request_start()
            return {"status": status.state, "detail": status.detail}
        return {"status": "mock_running", "detail": "Mock start — polymarket_bot not available"}
    except Exception as e:
        log.exception("btc.start_failed", error=str(e))
        return {"status": "error", "detail": f"Start failed: {e}"}


@app.post("/api/stop")
async def api_stop() -> dict[str, str]:
    """Stop the trading bot. In live mode this waits for the runner to cancel
    resting orders and flatten open positions before reporting stopped."""
    try:
        if _BTC_BOT_AVAILABLE:
            status = await request_stop()
            return {"status": status.state, "detail": status.detail}
        return {"status": "mock_stopped", "detail": "Mock stop — polymarket_bot not available"}
    except Exception as e:
        log.exception("btc.stop_failed", error=str(e))
        return {"status": "error", "detail": f"Stop failed: {e}"}


@app.post("/api/loss_halt/bypass")
async def api_loss_halt_bypass(request: Request) -> dict[str, Any]:
    """Toggle the daily realized-loss halt bypass (#76).

    Applies to BOTH paper and live — the old "live can never disable a hard
    money limit from the UI" invariant was removed at the operator's request.
    Persisted under ``risk.paper_bypass_loss_halt`` so the choice survives
    Stop/Start, and re-read by the gate every tick so it takes effect without a
    restart. Audited to ``notification_feed``.
    """
    from db import notify  # type: ignore[import-untyped]
    from polymarket_exec.execution.gate import set_loss_halt_bypass
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    enabled = bool((body or {}).get("enabled", False))
    await set_loss_halt_bypass(enabled)
    await notify(
        "loss_halt_bypass",
        f"Operator {'ENABLED' if enabled else 'disabled'} loss-halt bypass "
        "(paper+live, runtime — affects real money in live)",
        {"enabled": enabled},
    )
    log.info("btc.loss_halt_bypass", enabled=enabled)
    return {"status": "ok", "bypass_loss_halt": enabled}


@app.post("/api/loss_halt/reset")
async def api_loss_halt_reset() -> dict[str, Any]:
    """Operator "let me trade again": when stopped, reset the loss-halt tally +
    peaks so entries resume (#76).

    The loss-halt daily counters are held in memory by the running loop and
    re-persisted on every close, so they can only be reset when STOPPED — a
    loss-halt breach auto-stops the bot, so the operator is already stopped
    when one fires. Bankroll-cap notional is left untouched.
    Audited to ``notification_feed``.
    """
    from db import get_config, notify  # type: ignore[import-untyped]
    from polymarket_exec.execution.gate import reset_daily_loss_halt
    state = (await get_config("polymarket_bot.state", "stopped")) or "stopped"
    halt_reset = state != "running"
    if halt_reset:
        await reset_daily_loss_halt()
    await notify(
        "loss_halt_reset",
        "Operator reset the loss-halt tally + peaks to $0.00 (live + paper)"
        if halt_reset
        else "Operator pressed reset while running — loss-halt tally left to the loop",
    )
    log.info("btc.loss_halt_reset", halt_reset=halt_reset, state=state)
    return {
        "status": "ok",
        "reset": True,
        "halt_reset": halt_reset,
    }


# Hard sanity bound on the operator runtime per-trade cap. Generous enough for
# any realistic clip on this bankroll, low enough to catch a fat-fingered entry.
_MAX_TRADE_USD_CEILING = 1000.0
# Hard sanity bound on the operator runtime trade size in shares (#89). Far above
# anything this bankroll supports; the gate/bankroll caps do the real limiting.
_MAX_TRADE_SHARES_CEILING = 1000.0


@app.post("/api/runtime-config")
async def api_runtime_config(request: Request) -> dict[str, Any]:
    """Set an operator runtime risk knob, persisted and read by the bot each tick.

    Currently supports ``key="max_trade_usd"`` — the unified per-trade cap that
    governs both the sizing ceiling and the gate cap, in paper AND live, taking
    effect on the next tick without a restart (#50). Validated and audited to
    ``notification_feed``. The shape is generic so position-mode / max-positions
    controls can register here later.
    """
    from db import notify  # type: ignore[import-untyped]
    from polymarket_exec.execution.gate import set_runtime_max_trade_usd

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    key = (body or {}).get("key", "")
    if key == "max_trade_usd":
        try:
            value = float((body or {}).get("value"))
        except (TypeError, ValueError):
            return {"status": "error", "detail": "value must be a number"}
        if not (0 < value <= _MAX_TRADE_USD_CEILING):
            return {
                "status": "error",
                "detail": f"max trade size must be between $0 and ${_MAX_TRADE_USD_CEILING:.0f}",
            }
        value = round(value, 2)
        await set_runtime_max_trade_usd(value)
        await notify(
            "runtime_config",
            f"Operator set max trade size to ${value:.2f} (paper+live, runtime — no restart)",
            {"key": key, "value": value},
        )
        log.info("btc.runtime_config_set", key=key, value=value)
        return {"status": "ok", "key": key, "value": value}
    if key == "trade_shares":
        from polymarket_exec.execution.gate import set_runtime_trade_shares
        from polymarket_exec.execution.live import DEFAULT_MIN_ORDER_SIZE

        try:
            value = float((body or {}).get("value"))
        except (TypeError, ValueError):
            return {"status": "error", "detail": "value must be a number"}
        if not (DEFAULT_MIN_ORDER_SIZE <= value <= _MAX_TRADE_SHARES_CEILING):
            return {
                "status": "error",
                "detail": (
                    f"shares must be between {DEFAULT_MIN_ORDER_SIZE:.0f} "
                    f"(Polymarket minimum) and {_MAX_TRADE_SHARES_CEILING:.0f}"
                ),
            }
        value = round(value, 2)
        await set_runtime_trade_shares(value)
        await notify(
            "runtime_config",
            f"Operator set trade size to {value:g} shares (paper+live, runtime — no restart)",
            {"key": key, "value": value},
        )
        log.info("btc.runtime_config_set", key=key, value=value)
        return {"status": "ok", "key": key, "value": value}
    if key == "market":
        from polymarket_bot import market_selection

        value = (body or {}).get("value") or {}
        if not isinstance(value, dict):
            return {"status": "error", "detail": "value must be {asset, timeframe}"}
        asset = str(value.get("asset", ""))
        timeframe = str(value.get("timeframe", ""))
        try:
            sel = await market_selection.set_selection(asset, timeframe)
        except ValueError as e:
            return {"status": "error", "detail": str(e)}
        await notify(
            "runtime_config",
            f"Operator selected market {sel.asset.upper()} {sel.timeframe} (paper+live)",
            {"key": key, "value": {"asset": sel.asset, "timeframe": sel.timeframe}},
        )
        log.info("btc.runtime_config_set", key=key, asset=sel.asset, timeframe=sel.timeframe)
        return {
            "status": "ok",
            "key": key,
            "value": {"asset": sel.asset, "timeframe": sel.timeframe},
            "loop_supported": sel.loop_supported,
        }
    # Generic dashboard-editable knobs (#206) — everything registered in
    # ``runtime_knobs.KNOBS`` (paper strategy, live risk limits, auto-pause,
    # daily scanner) is validated and persisted through one shared path
    # instead of a bespoke branch per knob.
    if key in _knobs.KNOBS:
        try:
            value = await _knobs.set(key, (body or {}).get("value"))
        except ValueError as e:
            return {"status": "error", "detail": str(e)}
        knob = _knobs.KNOBS[key]
        await notify(
            "runtime_config",
            f"Operator set {knob.label} to {value} (runtime — no restart)",
            {"key": key, "value": value},
        )
        log.info("btc.runtime_config_set", key=key, value=value)
        return {"status": "ok", "key": key, "value": value}
    return {"status": "error", "detail": f"unknown runtime key {key!r}"}




async def _runtime_state() -> dict[str, str]:
    """Lightweight snapshot of bot state + mode for the topbar buttons."""
    from db import get_config

    return {
        "state": (await get_config("polymarket_bot.state", "stopped")) or "stopped",
        "mode": (await get_config("polymarket_bot.requested_mode", "paper")) or "paper",
    }


@app.get("/api/data")
async def api_data() -> dict[str, Any]:
    """Get current dashboard data as JSON."""
    return {
        "execution_view": await _execution_view_safe(),
        "market_selector": await _market_selector_safe(),
        "activity": await _get_activity_data(),
        "backtest": _get_backtest_data(),
        "runtime": await _runtime_state(),
    }


@app.get("/api/stream")
async def api_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events for real-time updates (5-second interval)."""
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            try:
                data = {
                    "execution_view": await _execution_view_safe(),
                    "market_selector": await _market_selector_safe(),
                    "activity": await _get_activity_data(),
                    "backtest": _get_backtest_data(),
                    "runtime": await _runtime_state(),
                }
                yield f"data: {json.dumps(data)}\n\n"
            except Exception as e:
                log.warning("sse_error", error=str(e))
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            await asyncio.sleep(5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Launch helper
# ---------------------------------------------------------------------------

def launch() -> None:
    """Run the dashboard with uvicorn."""
    import uvicorn
    uvicorn.run(
        "polymarket_exec.ops.dashboard.app:app",
        host=DASHBOARD_SERVER_NAME,
        port=DASHBOARD_SERVER_PORT,
        log_level="info",
    )
