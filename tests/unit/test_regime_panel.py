"""MARKET REGIME dashboard card (panels/regime.py) and its presence on the EMS page."""
from __future__ import annotations

from html import escape
from typing import Any

import pytest

from polymarket_bot.regime.classify import RULES
from polymarket_exec.ops.dashboard.panels import regime as panel


def _snapshot(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "created_at": "2026-09-27T12:00:00+00:00",
        "asset": "btc", "symbol": "BTCUSDT", "timeframe": "5m", "grade": "partial",
        "headline": "VOL 41% ann (MID) · VOLUME 1.3× (NORMAL) · FLAT · BOOK CHEAP · DAY UTC",
        "recommendation": "feasible: pairarb_maker · degraded: btc_5m_taker, daily_altcoin",
        "bands": {"volatility": "mid", "vol_trend": "stable", "volume": "normal", "move": "flat",
                  "jumps": "continuous", "book": "cheap", "session": "day", "weekday": "weekday"},
        "features": {"vol_1h_gk": 0.41 / 5615.69, "vol_24h_gk": 0.38 / 5615.69, "vol_ratio_seasonal": 1.02,
                     "volume_ratio_seasonal": 1.31, "volume_1h_usd": 45_000_000.0, "move_z_1h": -0.4,
                     "overround": 0.012, "maker_capture": 0.008},
        "fits": [
            {"strategy_id": "btc_5m_taker", "label": "5m pricing loop (taker, hold-to-settle)", "fit": "degraded",
             "reasons": ["m.taker_cost_over_gate"], "metrics": {"taker_round_trip_cost": 0.0235, "edge_gate": 0.045}},
            {"strategy_id": "pairarb_maker", "label": "5m two-sided maker quoting (shadow)", "fit": "feasible",
             "reasons": ["m.capture_positive"], "metrics": {"maker_capture": 0.008}},
            {"strategy_id": "daily_altcoin", "label": "Daily altcoin Up/Down scanner (shadow)", "fit": "blocked",
             "reasons": ["m.no_24h_bars"], "metrics": {}},
        ],
        "quality": [{"code": "daily_family_proxy", "detail": "btc is not in DAILY_ASSETS"}],
        "sources": {"bars": "binance_spot_klines"},
    }
    base.update(overrides)
    return base


def test_render_without_snapshot_names_the_asset() -> None:
    html = panel.render(None, asset="eth")
    assert "MARKET REGIME" in html and "no regime scan yet for ETH" in html
    assert "no regime scan yet —" in panel.render(None)


def test_render_shows_headline_chips_numbers_and_fits() -> None:
    html = panel.render(_snapshot(), asset="btc")
    assert "MARKET REGIME" in html and "BTC 5m" in html and "BTCUSDT" in html
    assert "VOL 41% ann (MID)" in html
    for chip in ("volatility: mid", "vol trend: stable", "volume: normal", "move: flat",
                 "jumps: continuous", "book: cheap", "session: day"):
        assert chip in html, chip
    assert "41% ann" in html and "38% ann" in html and "1.02×" in html and "1.31×" in html
    assert "$45.0M" in html and "1.2¢" in html and "0.8¢" in html
    assert "STRATEGY FEASIBILITY" in html
    assert "DEGRADED" in html and "FEASIBLE" in html and "BLOCKED" in html
    assert "feasible: pairarb_maker" in html
    assert "data grade partial: daily_family_proxy (btc is not in DAILY_ASSETS)" in html
    assert "not edge" in html and "FINDINGS.md" in html


def test_render_resolves_rule_ids_to_prose_and_shows_metrics() -> None:
    html = panel.render(_snapshot(), asset="btc")
    assert escape(RULES["m.taker_cost_over_gate"]) in html
    assert "taker round trip cost 0.0235" in html and "edge gate 0.045" in html
    unknown = _snapshot(fits=[{"strategy_id": "x", "label": "x", "fit": "feasible", "reasons": ["m.unknown"], "metrics": {}}])
    assert "m.unknown" in panel.render(unknown)


def test_render_neutral_tones_for_fits_and_red_only_for_blocked() -> None:
    html = panel.render(_snapshot(), asset="btc")
    assert "class='mono down'>BLOCKED" in html
    assert "class='mono '>FEASIBLE" in html  # no green: not a PnL signal
    assert "class='mono dim'>DEGRADED" in html


def test_render_handles_missing_features_and_string_quality() -> None:
    html = panel.render(_snapshot(features={}, quality=["legacy string"], grade="full"))
    assert html.count("—") >= 8 and "legacy string" in html and "data grade full" in html


def test_render_escapes_untrusted_text() -> None:
    html = panel.render(_snapshot(headline="<script>alert(1)</script>"))
    assert "<script>" not in html and "&lt;script&gt;" in html


class TestDashboardPresence:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from polymarket_exec.ops.dashboard.app import app

        with TestClient(app) as c:
            yield c

    def test_regime_card_is_on_the_ems_page(self, client) -> None:
        text = client.get("/").text
        assert "MARKET REGIME" in text
        assert "class='card wide'><div class='card-h'>MARKET REGIME" in text

    def test_regime_knobs_are_in_settings(self, client) -> None:
        text = client.get("/").text
        assert "Regime monitor" in text
