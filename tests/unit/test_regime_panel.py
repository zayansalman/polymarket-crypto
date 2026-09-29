"""MARKET REGIME dashboard card (panels/regime.py) and its presence on the EMS page."""
from __future__ import annotations

from html import escape
from typing import Any

import pytest
import pytest_asyncio

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
             "reasons": ["m.book_expensive_taker"], "metrics": {"taker_entry_fee": 0.0175, "edge_gate": 0.045}},
            {"strategy_id": "pairarb_maker", "label": "5m two-sided maker quoting (shadow)", "fit": "feasible",
             "reasons": ["m.capture_positive"], "metrics": {"maker_capture": 0.008}},
            {"strategy_id": "daily_altcoin", "label": "Daily altcoin Up/Down scanner (shadow)", "fit": "blocked",
             "reasons": ["m.no_24h_bars"], "metrics": {}},
        ],
        "quality": [{"code": "book_absent", "detail": "no in-phase book read"}],
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
    assert "data grade partial: book_absent (no in-phase book read)" in html
    assert "not edge" in html and "FINDINGS.md" in html


def test_render_resolves_rule_ids_to_prose_and_shows_metrics() -> None:
    html = panel.render(_snapshot(), asset="btc")
    assert escape(RULES["m.book_expensive_taker"]) in html
    assert "taker entry fee 0.0175" in html and "edge gate 0.045" in html
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


def test_render_tolerates_non_string_bands_and_non_dict_fits() -> None:
    """Regression: `_chip` called .replace on whatever bands_json held and `_fit_row` called
    .get on whatever fits held, so one odd row could 500 the whole EMS page."""
    html = panel.render(_snapshot(bands={"volatility": None, "book": 3, "session": "day"},
                                  fits=["junk", None, {"strategy_id": "x", "fit": "feasible"}]))
    assert "volatility: unknown" in html and "book: 3" in html and "MARKET REGIME" in html
    assert html.count("FEASIBLE") == 1


def test_annualization_is_the_features_constant() -> None:
    from polymarket_bot.regime.features import ANNUALIZE

    assert panel._ANNUALIZE is ANNUALIZE


def test_no_favour_language_on_the_card() -> None:
    assert "favour" not in panel.render(_snapshot(), asset="btc").lower()


def test_unavailable_card_is_distinct_from_no_scan_yet() -> None:
    down = panel.render_unavailable()
    assert "MARKET REGIME" in down and "regime.card_failed" in down and "no regime scan yet" not in down


@pytest_asyncio.fixture
async def _tmp_db(tmp_path, monkeypatch):
    import db as _db

    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()


@pytest.mark.asyncio
async def test_a_failing_regime_card_never_breaks_the_execution_view(_tmp_db, monkeypatch) -> None:
    """The card is advisory: a fault loading or rendering it is logged and replaced by the
    'unavailable' placeholder instead of 500ing the operator's whole EMS page."""
    from polymarket_exec.ops.dashboard import execution_view as ev

    async def boom(asset=None):
        raise RuntimeError("corrupt row")

    monkeypatch.setattr(ev.data, "latest_regime", boom)
    html = await ev._regime_card_html()
    assert "regime card unavailable" in html
    monkeypatch.setattr(ev.data, "latest_regime", lambda asset=None: _none())
    assert "no regime scan yet for BTC" in await ev._regime_card_html()


async def _none():
    return None


def test_dead_background_task_is_logged_but_cancellation_is_not(monkeypatch) -> None:
    import importlib

    # The package re-exports the FastAPI object as `app`, shadowing the module: import by path.
    app_mod = importlib.import_module("polymarket_exec.ops.dashboard.app")

    events: list[tuple[str, dict]] = []

    class _Log:
        def error(self, event, **kw):
            events.append((event, kw))

    monkeypatch.setattr(app_mod, "log", _Log())

    class _Task:
        def __init__(self, exc=None, cancelled=False):
            self._exc, self._cancelled = exc, cancelled

        def cancelled(self):
            return self._cancelled

        def exception(self):
            return self._exc

    cb = app_mod._log_background_task_death("regime_monitor")
    cb(_Task(cancelled=True))
    cb(_Task(exc=None))
    assert events == []
    cb(_Task(exc=RuntimeError("boom")))
    assert events[0][0] == "background_task_died" and events[0][1]["task"] == "regime_monitor"


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
