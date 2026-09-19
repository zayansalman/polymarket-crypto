"""Operator runtime config endpoint + persistence (#50).

The dashboard POSTs to /api/runtime-config to set the unified max trade size;
the value is persisted to the config table and read by the loop every tick.
Each test runs against its own throwaway SQLite so the real journal is untouched.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import db as _db
from polymarket_exec.execution.gate import (
    get_runtime_max_trade_usd,
    get_runtime_trade_shares,
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "rt_config.db")
    from polymarket_exec.ops.dashboard.app import app

    with TestClient(app) as c:
        yield c


class TestRuntimeConfigEndpoint:
    def test_set_max_trade_size_ok_and_persists(self, client: TestClient) -> None:
        r = client.post(
            "/api/runtime-config", json={"key": "max_trade_usd", "value": 3.5}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["value"] == 3.5
        # Persisted so the loop's next tick enforces it.
        assert asyncio.run(get_runtime_max_trade_usd()) == 3.5

    def test_rounds_to_cents(self, client: TestClient) -> None:
        r = client.post(
            "/api/runtime-config", json={"key": "max_trade_usd", "value": 4.567}
        )
        assert r.json()["value"] == 4.57

    def test_rejects_non_numeric(self, client: TestClient) -> None:
        r = client.post(
            "/api/runtime-config", json={"key": "max_trade_usd", "value": "abc"}
        )
        assert r.json()["status"] == "error"
        assert "number" in r.json()["detail"]

    def test_rejects_zero_and_negative(self, client: TestClient) -> None:
        for bad in (0, -1.0):
            r = client.post(
                "/api/runtime-config", json={"key": "max_trade_usd", "value": bad}
            )
            assert r.json()["status"] == "error"

    def test_rejects_out_of_range(self, client: TestClient) -> None:
        r = client.post(
            "/api/runtime-config", json={"key": "max_trade_usd", "value": 99999}
        )
        assert r.json()["status"] == "error"

    def test_unknown_key_rejected(self, client: TestClient) -> None:
        r = client.post(
            "/api/runtime-config", json={"key": "nonsense", "value": 1}
        )
        assert r.json()["status"] == "error"
        assert "unknown runtime key" in r.json()["detail"]

    def test_set_trade_shares_ok_and_persists(self, client: TestClient) -> None:
        r = client.post(
            "/api/runtime-config", json={"key": "trade_shares", "value": 8}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["value"] == 8.0
        assert asyncio.run(get_runtime_trade_shares()) == 8.0

    def test_trade_shares_rejects_below_minimum(self, client: TestClient) -> None:
        # 4 shares < Polymarket's 5-share minimum.
        r = client.post(
            "/api/runtime-config", json={"key": "trade_shares", "value": 4}
        )
        body = r.json()
        assert body["status"] == "error"
        assert "minimum" in body["detail"].lower()
        assert asyncio.run(get_runtime_trade_shares()) is None

    def test_trade_shares_rejects_non_numeric(self, client: TestClient) -> None:
        r = client.post(
            "/api/runtime-config", json={"key": "trade_shares", "value": "x"}
        )
        assert r.json()["status"] == "error"


class TestMarketSelection:
    def test_default_is_btc_1h_and_no_combo_is_loop_wired(
        self, client: TestClient
    ) -> None:
        """5m was removed 2026-09-19; no family is wired into the loop."""
        from polymarket_bot import market_selection as ms

        sel = asyncio.run(ms.get_selection())
        assert (sel.asset, sel.timeframe) == ("btc", "1h")
        assert not sel.loop_supported
        assert "5m" not in ms.TIMEFRAMES

    def test_set_market_persists(self, client: TestClient) -> None:
        from polymarket_bot import market_selection as ms

        r = client.post(
            "/api/runtime-config",
            json={"key": "market", "value": {"asset": "eth", "timeframe": "1h"}},
        )
        body = r.json()
        assert body["status"] == "ok"
        assert body["value"] == {"asset": "eth", "timeframe": "1h"}
        assert body["loop_supported"] is False
        sel = asyncio.run(ms.get_selection())
        assert (sel.asset, sel.timeframe) == ("eth", "1h")

    def test_rejects_unknown_market(self, client: TestClient) -> None:
        for bad in ({"asset": "ltc", "timeframe": "5m"}, {"asset": "btc", "timeframe": "2m"}, "btc"):
            r = client.post("/api/runtime-config", json={"key": "market", "value": bad})
            assert r.json()["status"] == "error"

    def test_page_renders_selector(self, client: TestClient) -> None:
        html = client.get("/").text
        assert "id=\"market-selector\"" in html
        assert "data-asset='eth'" in html
        assert "data-timeframe='1h'" in html


class TestMarketSelectorGlow:
    def test_glow_by_open_position_pnl(self) -> None:
        from polymarket_bot.market_selection import MarketSelection
        from polymarket_exec.ops.dashboard.panels import market_selector as mks

        slug = "btc-updown-1h-1757750400"
        tick = {"window_slug": slug, "up_best_bid": 0.60, "up_best_ask": 0.62}
        pos = [{"window_slug": slug, "side": "UP", "entry_price": 0.50, "shares": 5}]
        pnl = mks.open_market_pnl(open_pos=pos, daily_open=[{"asset": "doge"}], tick=tick)
        assert pnl[("btc", "1h")] == pytest.approx(0.55)
        assert pnl[("doge", "1d")] is None

        html = mks.render(selection=MarketSelection("btc", "1h"), open_pnl=pnl)
        assert "active glow-pos' data-asset='btc'" in html
        assert "active glow-pos' data-timeframe='1h'" in html
        assert "glow-flat' data-asset='doge'" in html

        pos[0]["entry_price"] = 0.70
        pnl = mks.open_market_pnl(open_pos=pos, daily_open=[], tick=tick)
        assert "glow-neg' data-asset='btc'" in mks.render(
            selection=MarketSelection("btc", "5m"), open_pnl=pnl
        )
