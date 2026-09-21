"""End-to-end tests for the EMS dashboard (#37 redesign).

Verifies the full page-load flow, EMS panels, controls, API round-trips,
static assets, and the trading-terminal visual contract.
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


class TestFullPageLoad:
    def test_page_loads_200(self, client: TestClient):
        assert client.get("/").status_code == 200

    def test_page_has_doctype_structure(self, client: TestClient):
        text = client.get("/").text
        for tag in ("<!DOCTYPE html>", "<html", "</html>", "<head>", "<body>"):
            assert tag in text

    def test_page_has_meta_viewport(self, client: TestClient):
        assert "width=device-width" in client.get("/").text

    def test_ems_panels_present(self, client: TestClient):
        text = client.get("/").text
        for panel in ("DECISION ENGINE", "LIVE MARKET", "PERFORMANCE / ALPHA",
                      "TCA", "TRADE BLOTTER", "execution-grid", "ribbon"):
            assert panel in text

    def test_ems_content_container(self, client: TestClient):
        text = client.get("/").text
        assert "execution-content" in text
        assert "activity-content" in text

    def test_strategy_panel_is_gone(self, client: TestClient):
        # The v0 Strategy card was archived (2026-09-13).
        text = client.get("/").text
        assert "Edge band" not in text
        assert "AUTO-PAUSE" not in text.upper().replace("AUTO-PAUSED", "")


class TestButtonInteractivity:
    def test_start_button(self, client: TestClient):
        text = client.get("/").text
        assert "handleStart()" in text
        assert "Start" in text

    def test_stop_button(self, client: TestClient):
        text = client.get("/").text
        assert "handleStop()" in text

    def test_refresh_button(self, client: TestClient):
        assert "handleRefresh()" in client.get("/").text


class TestApiRoundTrip:
    def test_start_returns_status(self, client: TestClient):
        r = client.post("/api/start")
        assert r.status_code == 200 and "status" in r.json()

    def test_stop_returns_status(self, client: TestClient):
        r = client.post("/api/stop")
        assert r.status_code == 200 and "status" in r.json()

    def test_data_after_start_stop_cycle(self, client: TestClient):
        client.post("/api/start")
        client.post("/api/stop")
        data = client.get("/api/data").json()
        assert "execution_view" in data
        assert "activity" in data


class TestStaticAssets:
    def test_css_complete(self, client: TestClient):
        css = client.get("/static/style.css").text
        selectors = [
            ":root", "body", ".topbar", ".ribbon", ".execution-grid", ".card",
            ".card-h", ".stat", ".pill", ".gauge", ".book", ".spark",
            ".calib", ".blotter", ".tag", ".btn", ".sse-indicator", ".toast",
        ]
        for sel in selectors:
            assert sel in css, f"missing CSS selector: {sel}"

    def test_js_has_core_functions(self, client: TestClient):
        js = client.get("/static/dashboard.js").text
        for fn in ("showToast", "handleStart", "handleStop", "handleRefresh",
                   "updateDashboard", "connectSSE", "updateSseIndicator"):
            assert fn in js, f"missing JS function: {fn}"


class TestVisualContract:
    """Institutional light theme — hairline grids, color reserved for signal."""

    def test_light_palette(self, client: TestClient):
        css = client.get("/static/style.css").text
        assert "#ffffff" in css      # --bg
        assert "#1b7a43" in css      # --pos
        assert "#b3261e" in css      # --neg
        assert "#ffa53c" not in css  # old Bloomberg amber accent must be gone

    def test_pnl_color_classes(self, client: TestClient):
        css = client.get("/static/style.css").text
        assert ".up" in css and ".down" in css
        assert "--pos:" in css and "--neg:" in css

    def test_no_rounded_corners_or_shadows(self, client: TestClient):
        css = client.get("/static/style.css").text
        import re
        radii = re.findall(r"border-radius:\s*([^;]+);", css)
        assert all(r.strip() in ("0", "0px", "0 0 0 0") for r in radii), radii
        # The only shadow allowed is the header market selector's open-position
        # glow (and its pulse keyframes) — a deliberate status signal.
        shadow_lines = [ln for ln in css.splitlines() if "box-shadow" in ln]
        assert all(
            ln.startswith((".mkt-btn.glow", "@keyframes mkt-pulse")) for ln in shadow_lines
        ), shadow_lines

    def test_monospace_numbers(self, client: TestClient):
        assert "--font-mono:" in client.get("/static/style.css").text

    def test_pill_and_tag_variants(self, client: TestClient):
        css = client.get("/static/style.css").text
        assert ".pill.live" in css
        assert ".tag.up" in css and ".tag.down" in css
