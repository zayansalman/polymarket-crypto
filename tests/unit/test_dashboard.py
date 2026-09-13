"""Unit tests for the FastAPI EMS dashboard (#37 redesign).

Covers app creation, the EMS page structure, static assets, and the
/api endpoints. Visual contract is the trading-terminal theme.
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
        assert "/" in paths
        assert "/api/data" in paths
        assert "/api/start" in paths
        assert "/api/stop" in paths
        assert "/api/stream" in paths


class TestDashboardPage:
    def test_get_root_returns_html(self, client: TestClient):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_html_links_assets(self, client: TestClient):
        text = client.get("/").text
        assert "/static/style.css" in text
        assert "/static/dashboard.js" in text

    def test_has_ems_panels(self, client: TestClient):
        text = client.get("/").text
        for panel in ("ribbon", "ems-grid", "STRATEGY", "LIVE MARKET",
                      "PERFORMANCE / ALPHA", "TCA", "TRADE BLOTTER"):
            assert panel in text, f"missing EMS panel: {panel}"

    def test_has_controls(self, client: TestClient):
        text = client.get("/").text
        assert "handleStart()" in text
        assert "handleStop()" in text

    def test_has_runtime_controls_card(self, client: TestClient):
        text = client.get("/").text
        assert "CONTROLS" in text
        assert "ctl-shares" in text
        assert "setTradeShares()" in text
        assert "Polymarket minimum order" in text

    def test_has_secondary_panels(self, client: TestClient):
        text = client.get("/").text
        assert "ACTIVITY LOG" in text
        assert "BACKTEST" in text


class TestStaticFiles:
    def test_css_served(self, client: TestClient):
        r = client.get("/static/style.css")
        assert r.status_code == 200
        assert "text/css" in r.headers["content-type"]

    def test_css_has_theme_variables(self, client: TestClient):
        css = client.get("/static/style.css").text
        for var in ("--bg:", "--pos:", "--neg:", "--font-mono:"):
            assert var in css, f"missing var {var}"

    def test_css_has_ems_components(self, client: TestClient):
        css = client.get("/static/style.css").text
        for sel in (".ribbon", ".card", ".ems-grid", ".blotter", ".stat", ".pill", ".tag"):
            assert sel in css, f"missing {sel}"

    def test_js_served(self, client: TestClient):
        r = client.get("/static/dashboard.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]

    def test_js_has_handlers_and_sse(self, client: TestClient):
        js = client.get("/static/dashboard.js").text
        for fn in ("handleStart", "handleStop", "handleRefresh",
                   "updateDashboard", "EventSource"):
            assert fn in js, f"missing {fn}"

    def test_js_swaps_ems_content(self, client: TestClient):
        assert "ems-content" in client.get("/static/dashboard.js").text

    def test_js_has_runtime_control_handler(self, client: TestClient):
        js = client.get("/static/dashboard.js").text
        assert "setTradeShares" in js
        assert "updateShareValue" in js

    def test_css_has_control_input(self, client: TestClient):
        assert ".ctl-input" in client.get("/static/style.css").text


class TestApiData:
    def test_api_data_returns_json(self, client: TestClient):
        r = client.get("/api/data")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/json"

    def test_api_data_has_expected_keys(self, client: TestClient):
        data = client.get("/api/data").json()
        assert "execution_view" in data
        assert "activity" in data
        assert "backtest" in data

    def test_api_data_execution_view_is_rendered_html(self, client: TestClient):
        execution_view = client.get("/api/data").json()["execution_view"]
        assert isinstance(execution_view, str) and len(execution_view) > 200
        assert "ribbon" in execution_view


class TestApiStart:
    def test_start_returns_json(self, client: TestClient):
        r = client.post("/api/start")
        assert r.status_code == 200
        assert "status" in r.json()


class TestApiStop:
    def test_stop_returns_json(self, client: TestClient):
        r = client.post("/api/stop")
        assert r.status_code == 200
        assert "status" in r.json()


class TestApiStream:
    def test_stream_route_exists(self):
        assert any(r.path == "/api/stream" for r in app.routes)


class TestPerformanceReconLine:
    """The 'Reconciled vs Polymarket' footer surfaces real account truth (#102)."""

    _PERF = dict(
        n=5, wins=3, losses=2, pnl=1.2, roi=0.05, win_rate=0.6,
        expectancy=0.24, profit_factor=1.5, max_dd=-0.5, equity=[0.0, 1.0, 2.0],
    )
    _RECON = {
        "real_btc_pnl_lifetime": "-14.5139",
        "real_account_pnl_lifetime": "-34.096",
        "open_positions_value": "6.2578",
        "asof": "2026-06-22T05:03:56Z",
        "source": "polymarket-data-api",
    }

    def test_recon_line_renders_real_numbers(self) -> None:
        from polymarket_exec.ops.dashboard.panels import performance

        html = performance.render(
            style="settle", perf=self._PERF, perf_live={"n": 0}, perf_paper={"n": 0},
            recon=self._RECON,
        )
        assert "Reconciled vs Polymarket" in html
        assert "$-14.51" in html  # BTC bot real
        assert "$-34.10" in html  # account real
        assert "2026-06-22T05:03:56" in html

    def test_recon_line_absent_without_data(self) -> None:
        from polymarket_exec.ops.dashboard.panels import performance

        html = performance.render(
            style="settle", perf=self._PERF, perf_live={"n": 0}, perf_paper={"n": 0},
            recon=None,
        )
        assert "Reconciled vs Polymarket" not in html

    def test_account_footer_flags_non_bot_inclusion(self) -> None:
        # #113: the account-wide figure bundles the operator's non-bot trades —
        # the label must say so, so it can't be misread as the bot's number.
        from polymarket_exec.ops.dashboard.panels import performance

        html = performance.render(
            style="settle", perf=self._PERF, perf_live={"n": 0}, perf_paper={"n": 0},
            recon=self._RECON,
        )
        assert "incl. non-bot" in html

    def test_freshness_badge_warns_when_unreconciled(self) -> None:
        # #113: with no reconciliation snapshot the headline metrics are pure
        # assumed-fill — the operator must see that, not a silent number.
        from polymarket_exec.ops.dashboard.panels import performance

        html = performance.render(
            style="settle", perf=self._PERF, perf_live={"n": 0}, perf_paper={"n": 0},
            recon=None,
        )
        assert "assumed-fill" in html

    def test_freshness_badge_shows_reconciled_date(self) -> None:
        from polymarket_exec.ops.dashboard.panels import performance

        html = performance.render(
            style="settle", perf=self._PERF, perf_live={"n": 0}, perf_paper={"n": 0},
            recon=self._RECON,
        )
        assert "perf-fresh" in html  # the freshness badge, distinct from the footer
        assert "reconciled 2026-06-22" in html
class TestBlotterOpenUnrealized:
    """Issue #113: an open blotter row shows live unrealized P&L (marked at the
    current side mid) instead of a static 'OPEN' when the position is in the
    live window and a tick is available."""

    _TICK = {
        "window_slug": "btc-updown-5m-100",
        "up_best_bid": 0.60, "up_best_ask": 0.62,
        "down_best_bid": 0.38, "down_best_ask": 0.42,
        "market_up_price": 0.61, "market_down_price": 0.40,
    }

    def _pos(self, **over):
        p = dict(side="UP", entry_price=0.50, shares=10.0, notional_usd=5.0,
                 window_slug="btc-updown-5m-100", mode="live")
        p.update(over)
        return p

    def test_open_row_shows_unrealized_for_current_window(self) -> None:
        from polymarket_exec.ops.dashboard.panels import blotter

        html = blotter.render(closed=[], open_pos=[self._pos()], tick=self._TICK)
        assert "+$1.10" in html  # (0.61-0.50)*10

    def test_open_row_static_open_without_tick(self) -> None:
        from polymarket_exec.ops.dashboard.panels import blotter

        html = blotter.render(closed=[], open_pos=[self._pos()], tick=None)
        assert "OPEN" in html

    def test_open_row_static_open_for_other_window(self) -> None:
        from polymarket_exec.ops.dashboard.panels import blotter

        html = blotter.render(
            closed=[], open_pos=[self._pos(window_slug="btc-updown-5m-999")],
            tick=self._TICK,
        )
        assert "OPEN" in html  # no live mark for a stale window


class TestMarketOpenPosition:
    """Issue #113: the LIVE MARKET card shows live unrealized P&L for an open
    position, marked at the current side mid. Positions in a window other than
    the latest tick's are shown without a fabricated mark ('—')."""

    _TICK = {
        "window_slug": "btc-updown-5m-100", "spot_price": 65000.0,
        "reference_price": 64990.0, "remaining_seconds": 120, "edge": 0.03,
        "fair_up_prob": 0.55,
        "up_best_bid": 0.60, "up_best_ask": 0.62,
        "down_best_bid": 0.38, "down_best_ask": 0.42,
        "market_up_price": 0.61, "market_down_price": 0.40, "reason": "idle",
    }

    def _pos(self, **over):
        p = dict(side="UP", entry_price=0.50, shares=10.0, notional_usd=5.0,
                 window_slug="btc-updown-5m-100", mode="live")
        p.update(over)
        return p

    def test_no_open_block_when_flat(self) -> None:
        from polymarket_exec.ops.dashboard.panels import market

        html = market.render(self._TICK, [])
        assert "OPEN POSITION" not in html

    def test_up_position_marks_to_side_mid(self) -> None:
        from polymarket_exec.ops.dashboard.panels import market

        # UP mid = (0.60+0.62)/2 = 0.61; unrealized = (0.61-0.50)*10 = +1.10.
        html = market.render(self._TICK, [self._pos()])
        assert "OPEN POSITION" in html
        assert "+$1.10" in html

    def test_down_position_marks_to_down_mid(self) -> None:
        from polymarket_exec.ops.dashboard.panels import market

        # DOWN mid = (0.38+0.42)/2 = 0.40; unrealized = (0.40-0.55)*10 = -1.50.
        html = market.render(self._TICK, [self._pos(side="DOWN", entry_price=0.55)])
        assert "$-1.50" in html

    def test_other_window_position_not_marked(self) -> None:
        from polymarket_exec.ops.dashboard.panels import market

        html = market.render(self._TICK, [self._pos(window_slug="btc-updown-5m-999")])
        # No fabricated unrealized from a stale window; entry still shown.
        assert "OPEN POSITION" in html
        assert "+$1.10" not in html

    def test_render_back_compat_without_open_pos(self) -> None:
        from polymarket_exec.ops.dashboard.panels import market

        # Existing single-arg call site must still work.
        html = market.render(self._TICK)
        assert "LIVE MARKET" in html

    def test_render_is_full_width_card(self) -> None:
        # The LIVE MARKET card must span both grid columns. The ems-grid has an
        # odd number of single-width cards, so a half-width LIVE MARKET orphans
        # the neighbouring cell (empty band under STRATEGY). Both the populated
        # and the no-tick states must carry `card wide`.
        from polymarket_exec.ops.dashboard.panels import market

        assert "class='card wide'" in market.render(self._TICK)
        assert "class='card wide'" in market.render(None)


class TestGuardrailsTrailingHalt:
    """Issue #112: the LOSS HALT panel reflects a trailing high-water-mark stop —
    halt floor = peak - limit, headroom = pnl - floor. A positive PnL that has
    drawn down past the floor shows HALTED; a never-profitable leg behaves like
    the old fixed -limit floor."""

    def _render(self, **over):
        from polymarket_exec.ops.dashboard.panels import guardrails

        kw = dict(
            day_spend=0.0, bankroll_cap=None, submitted_count=0, submitted_notional=0.0,
            day_pnl=0.0, live_pnl=0.0, paper_pnl=0.0, live_peak=0.0, paper_peak=0.0,
            loss_halt_usd=10.0, state="running", bot_detail="", session_start=None,
            blocked=[], mode="live", bypass_loss_halt=False,
        )
        kw.update(over)
        return guardrails.render(**kw)

    def test_headroom_is_full_at_peak(self) -> None:
        # pnl == peak == 0 → full headroom, not halted.
        html = self._render(live_pnl=0.0, live_peak=0.0)
        assert "$10.00" in html  # headroom
        assert ">OK<" in html

    def test_headroom_shrinks_after_drawdown_from_peak(self) -> None:
        # Banked +30, gave back 5 → headroom 5, floor +20, still OK.
        html = self._render(live_pnl=25.0, live_peak=30.0)
        assert "$5.00" in html  # headroom = 25 - (30-10)
        assert ">OK<" in html

    def test_positive_pnl_can_be_halted_by_trailing_stop(self) -> None:
        # +18 after a +30 peak → floor +20, 18 <= 20 → HALTED despite positive PnL.
        html = self._render(live_pnl=18.0, live_peak=30.0)
        assert ">HALTED<" in html

    def test_never_profitable_matches_fixed_floor(self) -> None:
        # peak 0 → floor -10; -10 → HALTED, exactly like the pre-#112 behaviour.
        html = self._render(live_pnl=-10.0, live_peak=0.0)
        assert ">HALTED<" in html

    def test_panel_shows_peak_and_floor(self) -> None:
        html = self._render(live_pnl=25.0, live_peak=30.0)
        assert "Peak (live)" in html
        assert "Halt floor" in html

    def test_paper_mode_uses_paper_leg(self) -> None:
        # In paper mode the paper leg + paper peak drive the display.
        html = self._render(mode="paper", paper_pnl=4.0, paper_peak=12.0,
                            live_pnl=0.0, live_peak=0.0)
        # floor = 12 - 10 = 2; headroom = 4 - 2 = 2.
        assert "$2.00" in html


class TestV0StrategyArchived:
    """The v0 strategy's dashboard surfaces are gone (archived 2026-09-13)."""

    def test_controls_has_no_model_picker(self) -> None:
        from polymarket_exec.ops.dashboard.panels import controls

        html = controls.render(trade_shares_current=None, current_price=None)
        assert "ctl-model" not in html
        assert "STRATEGY MODEL" not in html

    def test_active_model_key_is_rejected(self, client: TestClient) -> None:
        r = client.post(
            "/api/runtime-config",
            json={"key": "active_model", "value": "pricing_v0"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "error"

    def test_decision_engine_has_no_gates_column(self) -> None:
        from polymarket_exec.ops.dashboard.panels import decision_engine

        tick = {"spot_price": 1.0, "reference_price": 1.0, "remaining_seconds": 90,
                "reason": "skip: no strategy loaded"}
        html = decision_engine.render(tick, [])
        assert "GATES" not in html
        assert "no strategy loaded" in html


def test_stat_helper_emits_data_flash_attribute():
    from polymarket_exec.ops.dashboard.panels._shared import stat
    html_with_flash = stat("Session P&L", "+$1,284.50", flash="pnl")
    assert "data-flash='pnl'" in html_with_flash
    html_without_flash = stat("Win rate", "61.0%")
    assert "data-flash" not in html_without_flash
