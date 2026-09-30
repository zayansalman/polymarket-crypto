"""Unit tests for the FastAPI EMS dashboard.

Covers app creation, the page structure, static assets, and the /api endpoints.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi.testclient import TestClient

from ems.dashboard.app import app


def page_token(client: TestClient) -> str:
    """The token the served page carries (the dashboard's POSTs need it)."""
    text = client.get("/").text
    marker = 'name="dashboard-token" content="'
    start = text.index(marker) + len(marker)
    return text[start:text.index('"', start)]


@pytest.fixture
def client() -> TestClient:
    """Like the page: a local host name, and the page's token on every POST."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.headers["X-Dashboard-Token"] = page_token(c)
        yield c


class TestAppCreation:
    def test_app_has_title(self):
        assert app.title == "Polymarket Crypto Trading Lab"

    def test_app_has_routes(self):
        paths = {r.path for r in app.routes}
        assert {"/", "/api/data", "/api/stream", "/api/runtime-config", "/api/mode"} <= paths
        # The BTC loop's Start/Stop went with it (2026-09-26); PAPER/LIVE came back for
        # Kelly horse-race's live leg (AGENTS.md, "Live trading").
        assert not {"/api/start", "/api/stop"} & paths


class TestDashboardPage:
    def test_get_root_returns_html(self, client: TestClient):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_html_links_assets(self, client: TestClient):
        text = client.get("/").text
        assert "/static/style.css" in text
        assert "/static/dashboard.js" in text

    def test_has_the_cards(self, client: TestClient):
        text = client.get("/").text
        for panel in ("execution-grid", "FEEDS", "MY STRATEGIES", "FADE 1H MOMENTUM ON 15M",
                      "KELLY HORSE-RACE", "lc2004-Kronos BTC 24h forecast", "SETTINGS"):
            assert panel in text, f"missing card: {panel}"

    def test_no_loop_controls(self, client: TestClient):
        text = client.get("/").text
        for gone in ("handleStart()", "handleStop()", "ORDER SIZE",
                     "TRADE BLOTTER", "DECISION ENGINE"):
            assert gone not in text, gone

    def test_has_activity_log(self, client: TestClient):
        text = client.get("/").text
        assert "ACTIVITY LOG" in text

    def test_every_card_folds_and_remembers_its_state(self, client: TestClient):
        # Each card is a <details> fold; dashboard.js stores open/closed by
        # data-fold and re-applies it after every refresh swaps the HTML.
        text = client.get("/").text
        for key in ("feeds", "strategies", "strategy", "fade-1h", "kelly-horse-race",
                    "lc2004-kronos-btc-24h", "settings"):
            assert f"data-fold='{key}'" in text, key
        assert "<section class='card" not in text
        assert text.count("<summary class='card-h'>") == 7


class TestStaticFiles:
    def test_css_served(self, client: TestClient):
        r = client.get("/static/style.css")
        assert r.status_code == 200
        assert "text/css" in r.headers["content-type"]

    def test_css_has_theme_variables(self, client: TestClient):
        css = client.get("/static/style.css").text
        for var in ("--bg:", "--pos:", "--neg:", "--font-mono:"):
            assert var in css, f"missing var {var}"

    def test_js_served(self, client: TestClient):
        r = client.get("/static/dashboard.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]

    def test_js_has_handlers_and_sse(self, client: TestClient):
        js = client.get("/static/dashboard.js").text
        for fn in ("handleRefresh", "updateDashboard", "EventSource", "setKnob", "setStrategy"):
            assert fn in js, f"missing {fn}"
        for gone in ("handleStart", "handleStop", "setTradeShares", "updateTicket"):
            assert gone not in js, gone
        assert "setMode" in js and "X-Dashboard-Token" in js

    def test_js_swaps_ems_content(self, client: TestClient):
        assert "execution-content" in client.get("/static/dashboard.js").text

    def test_js_remembers_folds_from_the_document(self, client: TestClient):
        # A document-level capture listener, not an inline ontoggle: the inline
        # one fired before this script loaded and threw on every page load.
        js = client.get("/static/dashboard.js").text
        assert "document.addEventListener('toggle'" in js
        assert "details[data-fold]" in js

    def test_css_has_control_input(self, client: TestClient):
        assert ".ctl-input" in client.get("/static/style.css").text


class TestApiData:
    def test_api_data_returns_json(self, client: TestClient):
        r = client.get("/api/data")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/json"

    def test_api_data_has_expected_keys(self, client: TestClient):
        data = client.get("/api/data").json()
        assert set(data) == {"execution_view", "activity"}

    def test_api_data_execution_view_is_rendered_html(self, client: TestClient):
        execution_view = client.get("/api/data").json()["execution_view"]
        assert isinstance(execution_view, str) and len(execution_view) > 200
        assert "execution-grid" in execution_view


class TestApiRuntimeConfig:
    def test_unknown_key_is_an_error(self, client: TestClient):
        r = client.post("/api/runtime-config", json={"key": "max_trade_usd", "value": 3})
        assert r.status_code == 200 and r.json()["status"] == "error"

    def test_a_knob_round_trips(self, client: TestClient):
        r = client.post("/api/runtime-config",
                        json={"key": "fade1h_kelly_multiplier", "value": 0.25})
        assert r.json() == {"status": "ok", "key": "fade1h_kelly_multiplier", "value": 0.25}

    def test_a_strategy_switch_round_trips(self, client: TestClient):
        body = {"key": "strategy", "value": {"name": "fade_1h_momentum_15m", "enabled": True}}
        r = client.post("/api/runtime-config", json=body)
        assert r.json()["status"] == "ok" and r.json()["value"]["enabled"] is True


class TestApiStream:
    def test_stream_route_exists(self):
        assert any(r.path == "/api/stream" for r in app.routes)



class TestPaperLive:
    """PAPER/LIVE: LIVE only by the operator's click on this process's page (AGENTS.md)."""

    @staticmethod
    def _token(client: TestClient) -> str:
        return page_token(client)

    @pytest.fixture
    def client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        """Its own database: these tests write the PAPER/LIVE selection."""
        from ems import db as _db
        from ems.execution import live_control

        monkeypatch.setattr(_db, "DB_PATH", tmp_path / "mode.db")
        with TestClient(app, base_url="http://127.0.0.1") as c:
            c.headers["X-Dashboard-Token"] = page_token(c)
            yield c
        live_control.record_click("paper")

    def test_the_page_has_the_control_and_a_token(self, client: TestClient):
        text = client.get("/").text
        assert "setMode('paper')" in text and "setMode('live')" in text
        assert len(self._token(client)) > 30

    def test_live_without_the_token_is_refused(self, client: TestClient):
        from ems.execution import live_control
        r = client.post("/api/mode", json={"mode": "live"},
                        headers={"X-Dashboard-Token": ""}).json()
        assert r["status"] == "error" and "from the dashboard page" in r["detail"]
        assert not live_control.clicked_live()
        r = client.post("/api/mode", json={"mode": "live"},
                        headers={"X-Dashboard-Token": "not-the-token"}).json()
        assert r["status"] == "error"

    def test_a_click_with_the_token_selects_live(self, client: TestClient):
        from ems.execution import live_control
        r = client.post("/api/mode", json={"mode": "live"},
                        headers={"X-Dashboard-Token": self._token(client)}).json()
        assert r["status"] == "ok" and r["mode"] == "live" and live_control.clicked_live()
        assert "Operator selected LIVE" in client.get("/api/data").json()["activity"]
        r = client.post("/api/mode", json={"mode": "paper"}).json()
        assert r["status"] == "ok" and not live_control.clicked_live()

    def test_a_bad_mode_is_refused(self, client: TestClient):
        assert client.post("/api/mode", json={"mode": "shadow"}).json()["status"] == "error"

    def test_cors_stays_local(self, client: TestClient):
        r = client.options("/api/mode", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        assert r.headers.get("access-control-allow-origin") != "*"
        assert "evil.example" not in (r.headers.get("access-control-allow-origin") or "")

    def test_no_browser_dialogs(self, client: TestClient):
        js = client.get("/static/dashboard.js").text
        for dialog in ("confirm(", "alert(", "prompt("):
            assert dialog not in js, dialog


class TestNoForeignWrites:
    """Only this page changes state: a cross-site form, a missing token or another host name
    is refused for the switch, the knobs (live risk caps among them) and PAPER/LIVE."""

    @pytest.fixture
    def client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        from ems import db as _db

        monkeypatch.setattr(_db, "DB_PATH", tmp_path / "csrf.db")
        with TestClient(app, base_url="http://127.0.0.1") as c:
            yield c

    def test_a_cross_site_text_form_cannot_lift_a_live_cap(self, client: TestClient):
        from ems import runtime_knobs as _knobs

        token = page_token(client)
        body = '{"key": "live_max_trade_usd", "value": 1000, "pad": "="}'
        for headers in ({"Content-Type": "text/plain"},
                        {"Content-Type": "text/plain", "X-Dashboard-Token": token},
                        {"Content-Type": "application/json", "Origin": "https://evil.example",
                         "X-Dashboard-Token": token},
                        {"Content-Type": "application/json"}):
            r = client.post("/api/runtime-config", content=body, headers=headers).json()
            assert r["status"] == "error" and r["detail"].startswith("Refused"), headers
        import asyncio
        assert asyncio.run(_knobs.get("live_max_trade_usd")) == 3.0

    def test_the_page_itself_can_write(self, client: TestClient):
        r = client.post("/api/runtime-config", json={"key": "kelly_horse_race_max_notional_usd",
                                                     "value": 4.0},
                        headers={"X-Dashboard-Token": page_token(client),
                                 "Origin": "http://127.0.0.1:7860"}).json()
        assert r["status"] == "ok"

    def test_another_host_name_is_not_served(self):
        with TestClient(app, base_url="http://rebind.evil.example:7860") as c:
            r = c.get("/")
            assert r.status_code == 400 and "dashboard-token" not in r.text
            assert c.post("/api/mode", json={"mode": "live"}).status_code == 400
