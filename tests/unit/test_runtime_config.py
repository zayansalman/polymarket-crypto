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


def _stored_market() -> tuple[str | None, str | None]:
    from polymarket_bot import market_selection as ms

    return (
        asyncio.run(_db.get_config(ms.ASSET_KEY, None)),
        asyncio.run(_db.get_config(ms.TIMEFRAME_KEY, None)),
    )


@pytest.fixture
def no_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    """No market has a strategy (independent of the real registry)."""
    from polymarket_bot import market_selection as ms

    monkeypatch.setattr(ms, "STRATEGY_MARKETS", frozenset())


@pytest.fixture
def btc_1h_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only BTC 1h has a strategy."""
    from polymarket_bot import market_selection as ms

    monkeypatch.setattr(ms, "STRATEGY_MARKETS", frozenset({("btc", "1h")}))


@pytest.fixture
def two_strategies(monkeypatch: pytest.MonkeyPatch) -> None:
    """BTC 1h (the default) and ETH 5m have strategies."""
    from polymarket_bot import market_selection as ms

    monkeypatch.setattr(ms, "STRATEGY_MARKETS", frozenset({("btc", "1h"), ("eth", "5m")}))


class TestMarketSelection:
    def test_no_strategy_defaults_to_loop_market(
        self, client: TestClient, no_strategy: None
    ) -> None:
        from polymarket_bot import market_selection as ms

        sel = asyncio.run(ms.get_selection())
        assert (sel.asset, sel.timeframe) == ("btc", "5m")
        assert sel.loop_supported

    def test_default_is_first_strategy_market(
        self, client: TestClient, btc_1h_strategy: None
    ) -> None:
        from polymarket_bot import market_selection as ms

        sel = asyncio.run(ms.get_selection())
        assert (sel.asset, sel.timeframe) == ("btc", "1h")

    def test_set_market_persists(self, client: TestClient, two_strategies: None) -> None:
        from polymarket_bot import market_selection as ms

        r = client.post(
            "/api/runtime-config",
            json={"key": "market", "value": {"asset": "eth", "timeframe": "5m"}},
        )
        body = r.json()
        assert body["status"] == "ok"
        assert body["value"] == {"asset": "eth", "timeframe": "5m"}
        assert body["loop_supported"] is False
        assert _stored_market() == ("eth", "5m")
        sel = asyncio.run(ms.get_selection())
        assert (sel.asset, sel.timeframe) == ("eth", "5m")

    def test_rejects_unknown_market(self, client: TestClient) -> None:
        for bad in ({"asset": "ltc", "timeframe": "5m"}, {"asset": "btc", "timeframe": "2m"}, "btc"):
            r = client.post("/api/runtime-config", json={"key": "market", "value": bad})
            assert r.json()["status"] == "error"

    def test_rejects_market_without_strategy(
        self, client: TestClient, btc_1h_strategy: None
    ) -> None:
        from polymarket_bot import market_selection as ms

        ok = client.post(
            "/api/runtime-config",
            json={"key": "market", "value": {"asset": "btc", "timeframe": "1h"}},
        )
        assert ok.json()["status"] == "ok"
        for asset, tf in (("eth", "1h"), ("btc", "5m"), ("doge", "1d")):
            r = client.post(
                "/api/runtime-config",
                json={"key": "market", "value": {"asset": asset, "timeframe": tf}},
            )
            body = r.json()
            assert body["status"] == "error"
            assert "no strategy" in body["detail"]
        assert _stored_market() == ("btc", "1h")
        sel = asyncio.run(ms.get_selection())
        assert (sel.asset, sel.timeframe) == ("btc", "1h")

    def test_rejects_everything_while_no_strategy_exists(
        self, client: TestClient, no_strategy: None
    ) -> None:
        r = client.post(
            "/api/runtime-config",
            json={"key": "market", "value": {"asset": "btc", "timeframe": "5m"}},
        )
        assert r.json()["status"] == "error"
        assert "no strategy for BTC 5m" in r.json()["detail"]
        assert _stored_market() == (None, None)

    def test_stale_selection_without_strategy_falls_back(
        self, client: TestClient, btc_1h_strategy: None
    ) -> None:
        from db import set_config
        from polymarket_bot import market_selection as ms

        asyncio.run(set_config(ms.ASSET_KEY, "eth"))
        asyncio.run(set_config(ms.TIMEFRAME_KEY, "5m"))
        sel = asyncio.run(ms.get_selection())
        assert (sel.asset, sel.timeframe) == ("btc", "1h")

    def test_timeframe_for_snaps_to_a_strategy(self, btc_1h_strategy: None) -> None:
        from polymarket_bot import market_selection as ms

        assert ms.timeframe_for("btc", "1h") == "1h"
        assert ms.timeframe_for("btc", "5m") == "1h"
        assert ms.timeframe_for("eth", "1h") is None

    def test_page_renders_selector(self, client: TestClient) -> None:
        html = client.get("/").text
        assert "id=\"market-selector\"" in html
        assert "data-asset='eth'" in html
        assert "data-timeframe='1h'" in html

    def test_everything_greyed_while_no_strategy_exists(self, no_strategy: None) -> None:
        from polymarket_bot.market_selection import MarketSelection
        from polymarket_exec.ops.dashboard.panels import market_selector as mks

        html = mks.render(selection=MarketSelection("btc", "5m"), open_pnl={})
        assert html.count(" disabled ") == 10
        assert "onclick=" not in html
        assert "active' data-asset='btc' disabled title='No strategy for BTC yet'" in html
        assert "active' data-timeframe='5m' disabled title='No strategy for BTC 5m yet'" in html

    def test_only_strategy_markets_are_clickable(self, btc_1h_strategy: None) -> None:
        from polymarket_bot.market_selection import MarketSelection
        from polymarket_exec.ops.dashboard.panels import market_selector as mks

        html = mks.render(selection=MarketSelection("btc", "1h"), open_pnl={})
        assert "data-asset='btc' title='BTC' onclick=\"setMarket('btc','1h')\"" in html
        assert "data-timeframe='1h' title='BTC 1h' onclick=\"setMarket('btc','1h')\"" in html
        for asset in ("eth", "sol", "xrp", "doge", "bnb"):
            assert f"data-asset='{asset}' disabled" in html
        for tf in ("5m", "15m", "1d"):
            assert f"data-timeframe='{tf}' disabled title='No strategy for BTC {tf} yet'" in html
        assert html.count(" disabled ") == 8
        assert html.count("onclick=") == 2


    def test_asset_button_snaps_to_a_timeframe_with_strategy(
        self, two_strategies: None
    ) -> None:
        from polymarket_bot.market_selection import MarketSelection
        from polymarket_exec.ops.dashboard.panels import market_selector as mks

        html = mks.render(selection=MarketSelection("btc", "1h"), open_pnl={})
        assert "data-asset='eth' title='ETH' onclick=\"setMarket('eth','5m')\"" in html
        assert "data-asset='sol' disabled title='No strategy for SOL yet'" in html


class TestMarketSelectorGlow:
    def test_glow_by_open_position_pnl(self) -> None:
        from polymarket_bot.market_selection import MarketSelection
        from polymarket_exec.ops.dashboard.panels import market_selector as mks

        slug = "btc-updown-5m-1757750400"
        tick = {"window_slug": slug, "up_best_bid": 0.60, "up_best_ask": 0.62}
        pos = [{"window_slug": slug, "side": "UP", "entry_price": 0.50, "shares": 5}]
        pnl = mks.open_market_pnl(open_pos=pos, daily_open=[{"asset": "doge"}], tick=tick)
        assert pnl[("btc", "5m")] == pytest.approx(0.55)
        assert pnl[("doge", "1d")] is None

        html = mks.render(selection=MarketSelection("btc", "5m"), open_pnl=pnl)
        assert "active glow-pos' data-asset='btc'" in html
        assert "active glow-pos' data-timeframe='5m'" in html
        assert "glow-flat' data-asset='doge'" in html

        pos[0]["entry_price"] = 0.70
        pnl = mks.open_market_pnl(open_pos=pos, daily_open=[], tick=tick)
        assert "glow-neg' data-asset='btc'" in mks.render(
            selection=MarketSelection("btc", "5m"), open_pnl=pnl
        )
