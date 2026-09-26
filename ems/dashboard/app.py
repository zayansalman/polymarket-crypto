"""FastAPI dashboard for the local Polymarket crypto trading lab.

Starts the WebSocket market-data hub and the strategies (Fade 1h Momentum on
15m, Kelly horse-race) as background tasks for its lifetime — see ``_lifespan``.

Endpoints:
    GET  /                   — the dashboard page (HTML)
    POST /api/runtime-config — a strategy switch or a SETTINGS knob
    POST /api/mode           — the PAPER/LIVE selection (LIVE needs the page's token)
    GET  /api/data           — the page's fragments as JSON
    GET  /api/stream         — Server-Sent Events for live updates
    GET  /strategy-docs      — the strategy docs (``docs_view``)
"""

from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


from ems.config import (  # type: ignore[import-untyped]
    DASHBOARD_ALLOWED_HOSTS,
    DASHBOARD_SERVER_NAME,
    DASHBOARD_SERVER_PORT,
)
from ems.db import connect, init_db, notify  # type: ignore[import-untyped]
from ems.logging_setup import get_logger  # type: ignore[import-untyped]
from ems import runtime_knobs as _knobs
from ems import strategies as _strategies
from ems.execution import controls as _controls
from ems.execution import live_control as _live_control
from ems.dashboard.execution_view import execution_view_html

log = get_logger("dashboard")

# ---------------------------------------------------------------------------
# Lifespan — init DB tables on startup, run the hub and the strategy
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await init_db()

    # Market-data hub: live Up/Down books and trades (CLOB market WS) and the
    # Chainlink / TWAP / Binance reference prices (RTDS WS).
    from ems.marketdata import hub as _marketdata_hub

    market_data = _marketdata_hub.MarketDataHub()
    marketdata_stop_event = asyncio.Event()
    marketdata_task = asyncio.create_task(market_data.run(marketdata_stop_event))
    _marketdata_hub.set_current(market_data)

    # Fade 1h Momentum on 15m: scaled passive limit orders on paper on the
    # BTC/ETH/SOL/XRP 15m Up/Down windows, read from the hub above — so it
    # starts after the hub is current. Paper only: it has no live order path.
    # It settles and checks fills every pass whatever its switch says, records
    # every coin's inputs, and rests child orders only where the maths says
    # they pay.
    from ems.fade_1h_momentum_15m.runner import run_forever as _run_fade

    fade_stop_event = asyncio.Event()
    fade_task = asyncio.create_task(_run_fade(fade_stop_event))

    # Kelly horse-race: one randomised passive buy per BTC 15m window, read from
    # the hub above. Paper always; live only once built and armed. Its fills and
    # settlement run every pass whatever its switch says.
    from ems.kelly_horse_race.runner import run_forever as _run_kelly

    kelly_stop_event = asyncio.Event()
    kelly_task = asyncio.create_task(_run_kelly(kelly_stop_event))

    yield

    _marketdata_hub.set_current(None)
    for stop_event, task in (
        (fade_stop_event, fade_task),
        (kelly_stop_event, kelly_task),
        (marketdata_stop_event, marketdata_task),
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

# Same-origin dashboard: no wildcard CORS.
_LOCAL_ORIGINS = frozenset({
    f"http://127.0.0.1:{DASHBOARD_SERVER_PORT}",
    f"http://localhost:{DASHBOARD_SERVER_PORT}",
})
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(_LOCAL_ORIGINS),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Dashboard-Token"],
)
# Answer only to local host names: a page on another name that rebinds its DNS to 127.0.0.1
# must not be served the dashboard (or its token).
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=sorted({"127.0.0.1", "localhost", *DASHBOARD_ALLOWED_HOSTS}
                         | ({DASHBOARD_SERVER_NAME} - {"", "0.0.0.0", "::"})),
)

# A new token each process, put in the page it serves. Only a click on that page carries it,
# so only the operator's click in this dashboard session can select LIVE (AGENTS.md).
_DASHBOARD_TOKEN = secrets.token_urlsafe(32)


def _has_dashboard_token(request: Request) -> bool:
    return secrets.compare_digest(request.headers.get("x-dashboard-token", ""),
                                  _DASHBOARD_TOKEN)


def _refusal(request: Request) -> str | None:
    """Why a state-changing request is refused, or None. Every one must come from this
    process's page: a local origin (when the browser names one), a JSON body (a cross-site
    page cannot send one without a CORS check, which fails), and the page's token. The switch,
    the knobs (the live risk caps among them) and PAPER/LIVE all go through here."""
    origin = request.headers.get("origin")
    if origin is not None and origin not in _LOCAL_ORIGINS:
        return "Refused: the request did not come from this dashboard."
    kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if kind != "application/json":
        return "Refused: the request must be JSON."
    if not _has_dashboard_token(request):
        return "Refused: the dashboard token is missing or old. Reload the page and try again."
    return None


async def _mode_context() -> dict[str, Any]:
    """The PAPER/LIVE control's state for the page."""
    try:
        mode = await _controls.requested_mode()
    except Exception:  # noqa: BLE001 — shown as PAPER; the strategies fail closed anyway
        mode = "paper"
    status = await _live_control.live_status()
    return {"mode": mode, "live_armed": status.armed, "live_hint": status.message}

app.mount("/static", StaticFiles(directory=str(dashboard_dir / "static")), name="static")

templates = Jinja2Templates(directory=str(dashboard_dir / "templates"))

from ems.dashboard import docs_view as _docs_view  # noqa: E402

_docs_view.register(app)

# Cache-bust static JS by its mtime so a code change is always picked up — the
# browser otherwise caches /static/dashboard.js across server restarts.
try:
    _STATIC_VERSION = str(int(max(
        (dashboard_dir / "static" / name).stat().st_mtime
        for name in ("dashboard.js", "style.css")
    )))
except OSError:
    _STATIC_VERSION = "1"


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------


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


async def _load_feed(limit: int = 18) -> list[dict[str, Any]]:
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
            return [dict(row) for row in await cur.fetchall()]


# Activity-log colour groups. Anything unlisted renders as "other" (neutral).
_FEED_KIND = {
    "system_start": "system",
    "fade1h_fill": "trade",
    "fade1h_settled": "trade",
    "kelly_fill": "trade",
    "kelly_settled": "trade",
    "runtime_config": "config",
    "mode": "config",
}


async def _activity_html() -> str:
    try:
        rows = await _load_feed()
    except Exception:
        rows = []
    if not rows:
        return "<p><em>No activity yet.</em></p>"
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


async def _execution_view_safe() -> str:
    """Render the page body; a render error must never touch the strategy."""
    try:
        return await execution_view_html()
    except Exception as e:  # noqa: BLE001
        log.warning("execution_view_render_failed", error=str(e))
        return f"<div class='execution-view'><div class='card'>Execution view error: {escape(str(e))}</div></div>"


async def _page_data() -> dict[str, Any]:
    return {
        "execution_view": await _execution_view_safe(),
        "activity": await _activity_html(),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> Any:
    """Main dashboard page."""
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {**await _page_data(), **await _mode_context(), "static_version": _STATIC_VERSION,
         "dashboard_token": _DASHBOARD_TOKEN},
    )


@app.post("/api/mode")
async def api_mode(request: Request) -> dict[str, Any]:
    """Select PAPER or LIVE, only from this process's page (``_refusal``: its token, a JSON
    body, a local origin), so LIVE is selected only by the operator's click here. Audited to
    the activity feed."""
    refused = _refusal(request)
    if refused:
        log.warning("mode_refused", reason=refused)
        return {"status": "error",
                "detail": f"{refused} LIVE can only be selected by clicking LIVE in the dashboard."}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    mode = str((body or {}).get("mode", "")).strip().lower()
    if mode not in ("paper", "live"):
        return {"status": "error", "detail": "mode must be paper or live"}
    await _live_control.select_mode(mode, clicked=True)
    status = await _live_control.live_status()
    await notify("mode", f"Operator selected {mode.upper()}. {status.message}",
                 {"mode": mode, "live_armed": status.armed})
    log.info("mode_selected", mode=mode, live_armed=status.armed, state=status.state)
    return {"status": "ok", "mode": mode, "live_armed": status.armed,
            "detail": status.message}


@app.post("/api/runtime-config")
async def api_runtime_config(request: Request) -> dict[str, Any]:
    """Set a strategy switch (``key="strategy"``) or a SETTINGS knob (any name in
    ``runtime_knobs.KNOBS``). Validated, persisted, read by the strategy on its
    next pass, and audited to ``notification_feed``. Only from this page (``_refusal``)."""
    refused = _refusal(request)
    if refused:
        log.warning("runtime_config_refused", reason=refused)
        return {"status": "error", "detail": refused}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    key = (body or {}).get("key", "")
    if key == "strategy":
        value = (body or {}).get("value") or {}
        if not isinstance(value, dict):
            return {"status": "error", "detail": "value must be {name, enabled}"}
        name = str(value.get("name", ""))
        try:
            is_on = await _strategies.set_enabled(name, bool(value.get("enabled")))
        except ValueError as e:
            return {"status": "error", "detail": str(e)}
        label = _strategies.STRATEGIES[name].label
        await notify(
            "runtime_config",
            f"Operator turned {label} {'ON' if is_on else 'OFF'} "
            f"({'may open new positions' if is_on else 'no new entries; open positions still settle'})",
            {"key": key, "value": {"name": name, "enabled": is_on}},
        )
        log.info("runtime_config_set", key=key, strategy=name, enabled=is_on)
        return {"status": "ok", "key": key, "value": {"name": name, "enabled": is_on}}
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
        log.info("runtime_config_set", key=key, value=value)
        return {"status": "ok", "key": key, "value": value}
    return {"status": "error", "detail": f"unknown runtime key {key!r}"}


@app.get("/api/data")
async def api_data() -> dict[str, Any]:
    """The page's fragments as JSON."""
    return await _page_data()


@app.get("/api/stream")
async def api_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events for real-time updates (5-second interval)."""
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            try:
                yield f"data: {json.dumps(await _page_data())}\n\n"
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
