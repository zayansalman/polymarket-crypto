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

from polymarket_exec.ops.dashboard.app import app


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


class TestAppCreation:
    def test_app_has_title(self):
        assert app.title == "Polymarket Crypto Trading Lab"

    def test_app_has_routes(self):
        paths = {r.path for r in app.routes}
        assert {"/", "/api/data", "/api/stream", "/api/runtime-config"} <= paths
        # The BTC loop's controls went with it (2026-09-26).
        assert not {"/api/start", "/api/stop", "/api/mode"} & paths


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
                      "SETTINGS"):
            assert panel in text, f"missing card: {panel}"

    def test_no_loop_controls(self, client: TestClient):
        text = client.get("/").text
        for gone in ("handleStart()", "handleStop()", "setMode(", "ORDER SIZE",
                     "TRADE BLOTTER", "DECISION ENGINE"):
            assert gone not in text, gone

    def test_has_activity_log(self, client: TestClient):
        text = client.get("/").text
        assert "ACTIVITY LOG" in text


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
        for gone in ("handleStart", "handleStop", "setMode", "setTradeShares", "updateTicket"):
            assert gone not in js, gone

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
