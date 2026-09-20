"""Market execution strategy — dashboard side: POST /api/market-order + EXECUTION card.

The route only validates the click and hands it to the controller; the
controller is stubbed here (the runner-side handoff is covered in
test_market_execution.py), so nothing can place an order. Each test runs
against its own throwaway SQLite and a private knob cache.
"""
from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from html import escape
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import config as _config
import db as _db
from polymarket_bot import controller, manual_entry, paper
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot.manual_entry import EntryOutcome
from polymarket_exec.ops.dashboard.panels import blotter, decision_engine, execution

SLUG = "btc-updown-5m-1770000000"
TICK: dict[str, Any] = {
    "window_slug": SLUG,
    "remaining_seconds": 180,
    "up_best_ask": 0.531,
    "down_best_ask": 0.472,
    "market_up_price": 0.531,
    "market_down_price": 0.472,
}


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stopped controller, empty click slot, and a knob cache no test can leak."""
    monkeypatch.setattr(controller, "_runner_thread", None)
    monkeypatch.setattr(controller, "_stop_event", None)
    monkeypatch.setattr(controller, "_desired_running", False)
    monkeypatch.setattr(controller, "_mode_cache", "paper")
    monkeypatch.setattr(paper, "_live_executor", None)
    monkeypatch.setattr(_knobs, "_cache", dict(_knobs._cache))
    monkeypatch.setattr(controller, "_ensure_runner_started", lambda force=False: None)
    monkeypatch.setattr(controller, "_ensure_watchdog_started", lambda: None)
    manual_entry.reset_for_tests()
    yield
    manual_entry.reset_for_tests()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "test_market_order.db")
    from polymarket_exec.ops.dashboard.app import app

    with TestClient(app) as c:
        yield c


def _token(client: TestClient) -> dict[str, str]:
    """The dashboard page's token, as the page's own JS sends it."""
    html = client.get("/").text
    marker = '<meta name="dashboard-token" content="'
    start = html.index(marker) + len(marker)
    return {"X-Dashboard-Token": html[start: html.index('"', start)]}


def _set_market(client: TestClient) -> None:
    r = client.post("/api/runtime-config", json={"key": "execution_strategy", "value": "market"})
    assert r.json()["status"] == "ok"


def _order(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "side": "Up", "window_slug": SLUG, "ask": 0.531, "asset": "btc", "timeframe": "5m",
    }
    body.update(overrides)
    return body


def _post(client: TestClient, body: dict[str, Any], headers: dict[str, str] | None) -> dict:
    r = client.post("/api/market-order", json=body, headers=headers or {})
    assert r.status_code == 200  # refusals are JSON statuses, never HTTP errors
    reply = r.json()
    assert set(reply) == {"status", "detail", "mode", "side", "price", "shares", "notional_usd"}
    return reply


# --------------------------------------------------------------------------
# POST /api/market-order
# --------------------------------------------------------------------------


def test_order_without_token_is_refused(client: TestClient, monkeypatch) -> None:
    _set_market(client)
    calls: list[Any] = []
    monkeypatch.setattr(controller, "request_manual_entry", lambda *a, **k: calls.append(a))
    reply = _post(client, _order(), headers=None)
    assert reply["status"] == "error"
    assert "token" in reply["detail"]
    assert calls == []


def test_order_with_bad_side_is_refused(client: TestClient) -> None:
    _set_market(client)
    reply = _post(client, _order(side="Sideways"), _token(client))
    assert reply["status"] == "error"
    assert "Up or Down" in reply["detail"]


def test_order_refused_while_strategy_is_model(client: TestClient) -> None:
    reply = _post(client, _order(), _token(client))
    assert reply["status"] == "error"
    assert "Switch execution strategy to Market" in reply["detail"]


def test_order_refused_on_unwired_market(client: TestClient) -> None:
    _set_market(client)
    sel = {"key": "market", "value": {"asset": "eth", "timeframe": "15m"}}
    assert client.post("/api/runtime-config", json=sel).json()["loop_supported"] is False
    reply = _post(client, _order(asset="eth", timeframe="15m"), _token(client))
    assert reply["status"] == "error"
    assert "isn't wired for ETH 15m" in reply["detail"]


def test_order_refused_when_market_changed_since_render(client: TestClient) -> None:
    _set_market(client)
    reply = _post(client, _order(asset="eth"), _token(client))
    assert reply["status"] == "error"
    assert "Market changed" in reply["detail"]


def test_order_refused_without_window(client: TestClient) -> None:
    _set_market(client)
    reply = _post(client, _order(window_slug=""), _token(client))
    assert reply["status"] == "error"
    assert "window" in reply["detail"]


def test_order_while_bot_stopped_is_blocked(client: TestClient) -> None:
    _set_market(client)
    reply = _post(client, _order(), _token(client))
    assert reply["status"] == "blocked"
    assert "Start" in reply["detail"]
    assert not manual_entry.has_pending()  # never queued for a later Start


def test_order_returns_the_runner_outcome(client: TestClient, monkeypatch) -> None:
    _set_market(client)
    seen: dict[str, Any] = {}

    async def fake_request(side: str, **kwargs: Any) -> EntryOutcome:
        seen.update(side=side, **kwargs)
        return EntryOutcome(
            "filled", "Paper BUY Up 5.00 sh @ 0.531 ($2.66)", mode="paper", side="Up",
            price=0.531, shares=5.0, notional_usd=2.655, position_id=7,
        )

    monkeypatch.setattr(controller, "request_manual_entry", fake_request)
    reply = _post(client, _order(), _token(client))
    assert seen == {"side": "Up", "window_slug": SLUG, "seen_ask": pytest.approx(0.531)}
    assert reply == {
        "status": "filled", "detail": "Paper BUY Up 5.00 sh @ 0.531 ($2.66)",
        "mode": "paper", "side": "Up", "price": 0.531, "shares": 5.0, "notional_usd": 2.655,
    }


@pytest.mark.parametrize("ask", [None, "junk", 0, -0.2, 1.7])
def test_unusable_seen_ask_is_sent_as_none(client: TestClient, monkeypatch, ask: Any) -> None:
    _set_market(client)
    seen: dict[str, Any] = {}

    async def fake_request(side: str, **kwargs: Any) -> EntryOutcome:
        seen.update(kwargs)
        return EntryOutcome("blocked", "No ask on the Down book", mode="paper", side=side)

    monkeypatch.setattr(controller, "request_manual_entry", fake_request)
    reply = _post(client, _order(side="Down", ask=ask), _token(client))
    assert seen["seen_ask"] is None
    assert reply["status"] == "blocked"


def test_controller_failure_is_an_error_status(client: TestClient, monkeypatch) -> None:
    _set_market(client)

    async def boom(side: str, **kwargs: Any) -> EntryOutcome:
        raise RuntimeError("db locked")

    monkeypatch.setattr(controller, "request_manual_entry", boom)
    reply = _post(client, _order(), _token(client))
    assert reply["status"] == "error"
    assert "db locked" in reply["detail"]


def test_execution_strategy_knob_toggles_and_persists(client: TestClient) -> None:
    import asyncio

    _set_market(client)
    assert asyncio.run(_knobs.get("execution_strategy")) == "market"
    r = client.post("/api/runtime-config", json={"key": "execution_strategy", "value": "yolo"})
    assert r.json()["status"] == "error"
    assert asyncio.run(_knobs.get("execution_strategy")) == "market"
    client.post("/api/runtime-config", json={"key": "execution_strategy", "value": "model"})
    assert asyncio.run(_knobs.get("execution_strategy")) == "model"


def test_dashboard_renders_execution_card(client: TestClient) -> None:
    view = client.get("/api/data").json()["execution_view"]
    assert "id='exec-card'" in view
    assert view.index("id='exec-card'") < view.index("CONTROLS")
    assert "mo-btn" not in view  # Model is the default: no Buy buttons
    _set_market(client)
    view = client.get("/api/data").json()["execution_view"]
    assert "buyMarket('Up')" in view and "buyMarket('Down')" in view
    assert "Press ▶ Start first" in view  # nothing runs in this test process
    assert "MARKET — auto entries off" in view or "no ticks yet" in view


@pytest.mark.asyncio
async def test_open_position_count_ignores_style_and_mode(tmp_path: Path, monkeypatch) -> None:
    from polymarket_exec.ops.dashboard.panels import _data

    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "count.db")
    await _db.init_db()
    async with _db.connect() as db:
        for style, mode, state in (
            ("scalp", "live", "open"), ("settle", "paper", "open"), ("settle", "paper", "closed"),
        ):
            await db.execute(
                "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price, "
                "notional_usd, shares, strategy_style, mode) "
                "VALUES ('2026-09-13T00:00:00+00:00', ?, 'Up', ?, 0.5, 2.5, 5, ?, ?)",
                (SLUG, state, style, mode),
            )
        await db.commit()
    assert await _data.open_position_count() == 2


def test_is_running_follows_the_runner_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    assert controller.is_running() is False
    alive = threading.Event()
    thread = threading.Thread(target=alive.wait, daemon=True)
    thread.start()
    stop = threading.Event()
    try:
        monkeypatch.setattr(controller, "_runner_thread", thread)
        monkeypatch.setattr(controller, "_stop_event", stop)
        monkeypatch.setattr(controller, "_desired_running", True)
        assert controller.is_running() is True
        stop.set()  # Stop pressed: still alive while it flattens, but not running
        assert controller.is_running() is False
    finally:
        alive.set()
        thread.join(2)


# --------------------------------------------------------------------------
# EXECUTION panel (pure render)
# --------------------------------------------------------------------------


def _panel(**overrides: Any) -> str:
    kwargs: dict[str, Any] = dict(
        strategy="market", running=True, mode="paper", asset="btc", timeframe="5m",
        loop_supported=True, tick=TICK, trade_shares=None, open_position_count=0,
        kill_armed=False,
    )
    kwargs.update(overrides)
    return execution.render(**kwargs)


def _button(html: str, side: str) -> str:
    start = html.index(f"data-side='{side}'")
    return html[html.rfind("<button", 0, start): html.index("</button>", start)]


def _reason(**overrides: Any) -> str | None:
    kwargs: dict[str, Any] = dict(
        running=True, loop_supported=True, selection="BTC 5m", tick=TICK,
        open_position_count=0, kill_armed=False,
    )
    kwargs.update(overrides)
    return execution.disabled_reason(**kwargs)


def test_model_panel_has_toggle_but_no_buy_buttons() -> None:
    html = _panel(strategy="model")
    assert "EXECUTION" in html
    assert "mo-btn" not in html and "buyMarket" not in html
    assert "class='mode-opt active' data-strategy='model'" in html
    assert "setExecutionStrategy('market')" in html
    assert "enters automatically" in html


def test_unknown_strategy_renders_as_model() -> None:
    assert "mo-btn" not in _panel(strategy="yolo")


def test_market_panel_shows_both_buttons_with_ask_and_size() -> None:
    html = _panel()
    assert "class='mode-opt active' data-strategy='market'" in html
    up, down = _button(html, "Up"), _button(html, "Down")
    assert "BUY UP" in up and "0.531" in up and "≈ $2.66 · 5 sh" in up
    assert "BUY DOWN" in down and "0.472" in down and "≈ $2.36 · 5 sh" in down
    assert "disabled" not in up and "disabled" not in down
    assert "data-ask='0.5310'" in up
    assert "<span class='pill paper'>PAPER</span>" in up
    assert "Click to buy at the current ask" in html
    assert f"data-window='{SLUG}'" in html
    assert "data-asset='btc'" in html and "data-timeframe='5m'" in html
    assert "data-mode='paper'" in html
    assert html.startswith("<section class='card wide' id='exec-card'")


def test_market_panel_sizes_to_operator_trade_shares() -> None:
    assert "≈ $5.31 · 10 sh" in _button(_panel(trade_shares=10.0), "Up")


def test_live_panel_says_real_order() -> None:
    html = _panel(mode="live")
    assert "LIVE — real order" in _button(html, "Up")
    assert "data-mode='live'" in html


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"running": False}, "Press ▶ Start first"),
        ({"asset": "eth", "timeframe": "1h", "loop_supported": False},
         "Loop isn't wired for ETH 1h yet"),
        ({"open_position_count": 1}, "Position open — wait for it to exit (max 1)"),
        ({"kill_armed": True}, "Kill switch armed"),
        ({"tick": None}, "Waiting for market data"),
        ({"tick": {**TICK, "remaining_seconds": 0}}, "Window ended — waiting for the next one"),
    ],
)
def test_disabled_reason_turns_both_buttons_off(overrides: dict, expected: str) -> None:
    html = _panel(**overrides)
    assert "disabled" in _button(html, "Up") and "disabled" in _button(html, "Down")
    assert f"<div class='mo-status off'>{escape(expected)}</div>" in html


def test_disabled_reason_order_and_all_clear() -> None:
    assert _reason() is None
    everything_wrong = dict(
        running=False, loop_supported=False, tick=None, open_position_count=1, kill_armed=True,
    )
    assert _reason(**everything_wrong) == "Press ▶ Start first"
    assert _reason(**{**everything_wrong, "running": True}) == "Loop isn't wired for BTC 5m yet"
    assert _reason(open_position_count=1, kill_armed=True).startswith("Position open")


def test_missing_ask_disables_only_that_side() -> None:
    html = _panel(tick={**TICK, "down_best_ask": None, "market_down_price": None})
    assert "disabled" not in _button(html, "Up")
    down = _button(html, "Down")
    assert "disabled" in down and "No ask on the Down book" in down
    assert "Click to buy at the current ask" in html


def test_panel_escapes_interpolated_text() -> None:
    html = _panel(tick={**TICK, "window_slug": "x'><script>1</script>"})
    assert "<script>" not in html


# --------------------------------------------------------------------------
# Decision banner, blotter chip, static assets
# --------------------------------------------------------------------------


class _Params:
    entry_edge_min = 0.045
    entry_edge_max = 0.07
    min_confidence = 0.5
    entry_min_remaining_seconds = 60
    min_entry_price = 0.2
    max_entry_price = 0.8


def _banner(html: str) -> str:
    start = html.index("<div class='de-decision'>")
    return html[start: html.index("</div>", start)]


def test_decision_banner_in_market_mode_never_says_enter() -> None:
    tick = {**TICK, "signal_side": "Up", "reason": "enter: edge 0.05", "fair_up_prob": 0.6}
    model = _banner(decision_engine.render(tick, []))
    assert "ENTER UP" in model
    market = _banner(
        decision_engine.render(tick, [], execution_strategy="market")
    )
    assert "MARKET — auto entries off" in market
    assert "model signal shown for reference" in market
    assert "ENTER" not in market


def test_blotter_marks_market_rows() -> None:
    row = {
        "side": "Up", "entry_price": 0.53, "exit_price": 1.0, "notional_usd": 2.65,
        "realized_pnl_usd": 2.35, "exit_reason": "SETTLED", "closed_at": None, "mode": "paper",
        "shares": 5.0, "window_slug": SLUG,
    }
    html = blotter.render(
        closed=[{**row, "entry_source": "market"}, {**row, "entry_source": "model"}],
        open_pos=[{**row, "entry_source": "market"}],
    )
    assert html.count("class='pill mkt'") == 2
    assert "class='pill mkt'" not in blotter.render(closed=[row], open_pos=[])


def test_dashboard_js_wires_market_buttons(client: TestClient) -> None:
    js = client.get("/static/dashboard.js").text
    for name in ("function buyMarket", "function setExecutionStrategy", "function postKnob",
                 "function applyPendingMarketOrder"):
        assert name in js
    buy = js[js.index("function buyMarket"): js.index("function applyPendingMarketOrder")]
    assert "'/api/market-order'" in buy
    assert "headers: dashboardHeaders()" in buy
    assert "window.pendingMarketOrder" in buy
    swap = js.index("swapKeepingInputs(execEl")
    assert "applyPendingMarketOrder()" in js[swap: swap + 120]
    assert not re.search(r"\b(confirm|alert|prompt)\(", js)


def test_css_styles_market_buttons(client: TestClient) -> None:
    css = client.get("/static/style.css").text
    for sel in (".mo-grid", ".mo-btn.up", ".mo-btn.down", ".mo-btn[disabled]",
                ".mo-btn.pending", ".exec-toggle .mode-opt.active", ".pill.mkt"):
        assert sel in css
    block = css[css.index(".exec-row"): css.index(".pill.mkt")]
    assert "box-shadow" not in block
    assert all(r.strip() == "0" for r in re.findall(r"border-radius:\s*([^;]+);", block))


def test_kill_switch_helper_reads_the_kill_file(tmp_path: Path, monkeypatch) -> None:
    from polymarket_exec.ops.dashboard.panels import ribbon

    kill = tmp_path / "KILL"
    monkeypatch.setattr(_config, "KILL_SWITCH_PATH", kill)
    assert ribbon.kill_switch_armed() is False
    kill.touch()
    assert ribbon.kill_switch_armed() is True
