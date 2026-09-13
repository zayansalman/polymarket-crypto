"""Unit tests for the connector layer.

Uses :mod:`unittest.mock` to mock HTTP responses — no external network calls.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from polymarket_exec.connectors import (
    BinanceConnector,
    ChainlinkConnectorStub,
    ConnectorRegistry,
    PolymarketConnector,
)
from polymarket_exec.connectors.polymarket import _json_list, _outcome_prices
from polymarket_exec.core.exceptions import FeedError, MarketDiscoveryError
from polymarket_exec.core.types import MarketWindow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(json_data=None, status_code=200, text=""):
    """Build a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = text
    return resp


def _make_async_client(mock_get_response):
    """Create a mock httpx.AsyncClient whose ``get`` coroutine returns
    *mock_get_response*.
    """
    client = MagicMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(return_value=mock_get_response)
    return client


# ---------------------------------------------------------------------------
# PolymarketConnector tests
# ---------------------------------------------------------------------------


class TestPolymarketConnector:

    @pytest.mark.asyncio
    async def test_discover_current_window_happy_path(self):
        """Successful market discovery via the markets endpoint."""
        market_payload = {
            "slug": "btc-updown-5m-1700000000",
            "question": "Bitcoin Up or Down - Nov 14, 2023?",
            "outcomes": '["Up", "Down"]',
            "outcomePrices": '["0.52", "0.48"]',
        }
        mock_resp = _mock_response(json_data=[market_payload])
        mock_resp.raise_for_status = MagicMock()
        client = _make_async_client(mock_resp)

        connector = PolymarketConnector(client)
        window = await connector.discover_current_window()

        assert isinstance(window, MarketWindow)
        assert window.slug.startswith("btc-updown-5m-")
        assert window.up_price == 0.52
        assert window.down_price == 0.48
        assert window.end_ts == window.start_ts + 300
        # Verify the client was called (at least once for markets endpoint)
        assert client.get.called

    @pytest.mark.asyncio
    async def test_discover_current_window_via_events_endpoint(self):
        """Market found via events endpoint fallback."""
        # First call (markets) returns empty list; second call (events) returns
        # an event with embedded markets list.
        event_payload = {
            "markets": [
                {
                    "slug": "btc-updown-5m-1700000000",
                    "question": "Bitcoin Up or Down?",
                    "outcomes": '["Up", "Down"]',
                    "outcomePrices": '["0.55", "0.45"]',
                }
            ]
        }
        empty_resp = _mock_response(json_data=[])
        empty_resp.raise_for_status = MagicMock()
        event_resp = _mock_response(json_data=[event_payload])
        event_resp.raise_for_status = MagicMock()

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=[empty_resp, event_resp])

        connector = PolymarketConnector(client)
        window = await connector.discover_current_window()

        assert isinstance(window, MarketWindow)
        assert window.up_price == 0.55
        assert window.down_price == 0.45

    @pytest.mark.asyncio
    async def test_discover_current_window_not_found_raises(self):
        """When no market is found, :class:`MarketDiscoveryError` is raised."""
        empty_resp = _mock_response(json_data=[])
        empty_resp.raise_for_status = MagicMock()

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=empty_resp)

        connector = PolymarketConnector(client)
        with pytest.raises(MarketDiscoveryError) as exc_info:
            await connector.discover_current_window()
        assert "Could not discover" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_discover_current_window_http_error_raises_feed_error(self):
        """HTTP errors from the Gamma API are translated to :class:`FeedError`."""
        error_resp = _mock_response(status_code=500, text="Internal Server Error")
        error_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Server error",
                request=MagicMock(spec=httpx.Request),
                response=error_resp,
            )
        )

        client = _make_async_client(error_resp)
        connector = PolymarketConnector(client)
        with pytest.raises(FeedError) as exc_info:
            await connector.discover_current_window()
        assert "500" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_health_check_ok(self):
        """Health check returns 'ok' when API is responsive."""
        mock_resp = _mock_response(json_data=[])
        mock_resp.raise_for_status = MagicMock()
        client = _make_async_client(mock_resp)

        connector = PolymarketConnector(client)
        result = await connector.health_check()

        assert result["status"] == "ok"
        assert "latency_ms" in result
        assert result["latency_ms"] >= 0

    @pytest.mark.asyncio
    async def test_health_check_down(self):
        """Health check returns 'down' on request errors."""
        error_resp = MagicMock(spec=httpx.Response)
        error_resp.raise_for_status = MagicMock(
            side_effect=httpx.ConnectError("Connection refused")
        )
        client = _make_async_client(error_resp)

        connector = PolymarketConnector(client)
        result = await connector.health_check()

        assert result["status"] == "down"
        assert "ConnectError" in result["detail"] or "Request error" in result["detail"]

    @pytest.mark.asyncio
    async def test_discover_with_list_outcome_prices(self):
        """Market where outcomePrices is already a list (not JSON string)."""
        market_payload = {
            "slug": "btc-updown-5m-1700000000",
            "question": "Bitcoin Up or Down?",
            "outcomes": ["Up", "Down"],
            "outcomePrices": ["0.61", "0.39"],
        }
        mock_resp = _mock_response(json_data=[market_payload])
        mock_resp.raise_for_status = MagicMock()
        client = _make_async_client(mock_resp)

        connector = PolymarketConnector(client)
        window = await connector.discover_current_window()

        assert window.up_price == 0.61
        assert window.down_price == 0.39


# ---------------------------------------------------------------------------
# _outcome_prices helper tests
# ---------------------------------------------------------------------------


class TestOutcomePricesHelpers:
    def test_json_list_with_string(self):
        assert _json_list('["a", "b"]') == ["a", "b"]

    def test_json_list_with_list(self):
        assert _json_list(["a", "b"]) == ["a", "b"]

    def test_json_list_with_none(self):
        assert _json_list(None) == []

    def test_json_list_with_bad_json(self):
        assert _json_list("not json") == []

    def test_outcome_prices_up_first(self):
        market = {"outcomes": ["Up", "Down"], "outcomePrices": ["0.6", "0.4"]}
        up, down = _outcome_prices(market)
        assert up == 0.6
        assert down == 0.4

    def test_outcome_prices_down_first(self):
        market = {"outcomes": ["Down", "Up"], "outcomePrices": ["0.4", "0.6"]}
        up, down = _outcome_prices(market)
        assert up == 0.6
        assert down == 0.4

    def test_outcome_prices_bad_count_raises(self):
        with pytest.raises(MarketDiscoveryError):
            _outcome_prices({"outcomes": ["Up"], "outcomePrices": ["0.5"]})


# ---------------------------------------------------------------------------
# BinanceConnector tests
# ---------------------------------------------------------------------------


class TestBinanceConnector:

    @pytest.mark.asyncio
    async def test_get_spot_and_recent_closes(self):
        """Happy path — 90 1-second klines returned."""
        klines = [[i, "100", "101", "99", str(50000.0 + i), "1"] for i in range(90)]
        mock_resp = _mock_response(json_data=klines)
        mock_resp.raise_for_status = MagicMock()
        client = _make_async_client(mock_resp)

        connector = BinanceConnector(client)
        latest, closes = await connector.get_spot_and_recent_closes()

        assert latest == 50000.0 + 89
        assert len(closes) == 90
        assert closes[0] == 50000.0
        assert closes[-1] == 50000.0 + 89

    @pytest.mark.asyncio
    async def test_get_spot_and_recent_closes_empty_raises(self):
        """Empty klines response raises FeedError."""
        mock_resp = _mock_response(json_data=[])
        mock_resp.raise_for_status = MagicMock()
        client = _make_async_client(mock_resp)

        connector = BinanceConnector(client)
        with pytest.raises(FeedError) as exc_info:
            await connector.get_spot_and_recent_closes()
        assert "no BTC closes" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_reference_price(self):
        """Fetch reference price at window start."""
        klines = [[1700000000000, "50000", "50100", "49900", "50050", "100"]]
        mock_resp = _mock_response(json_data=klines)
        mock_resp.raise_for_status = MagicMock()
        client = _make_async_client(mock_resp)

        connector = BinanceConnector(client)
        ref = await connector.get_reference_price(1700000000)

        assert ref == 50050.0

    @pytest.mark.asyncio
    async def test_get_reference_price_empty_raises(self):
        """Empty reference candle raises FeedError."""
        mock_resp = _mock_response(json_data=[])
        mock_resp.raise_for_status = MagicMock()
        client = _make_async_client(mock_resp)

        connector = BinanceConnector(client)
        with pytest.raises(FeedError) as exc_info:
            await connector.get_reference_price(1700000000)
        assert "no BTC window reference candle" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_http_error_translated_to_feed_error(self):
        """HTTP errors from Binance are translated to :class:`FeedError`."""
        error_resp = _mock_response(status_code=418, text="I'm a teapot")
        error_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Rate limited",
                request=MagicMock(spec=httpx.Request),
                response=error_resp,
            )
        )
        client = _make_async_client(error_resp)

        connector = BinanceConnector(client)
        with pytest.raises(FeedError) as exc_info:
            await connector.get_spot_and_recent_closes()
        assert "418" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_health_check_ok(self):
        """Health check returns ok when Binance is responsive."""
        mock_resp = _mock_response(json_data={"symbols": []})
        mock_resp.raise_for_status = MagicMock()
        client = _make_async_client(mock_resp)

        connector = BinanceConnector(client)
        result = await connector.health_check()

        assert result["status"] == "ok"
        assert "latency_ms" in result

    @pytest.mark.asyncio
    async def test_health_check_down(self):
        """Health check returns down on request errors."""
        error_resp = MagicMock(spec=httpx.Response)
        error_resp.raise_for_status = MagicMock(
            side_effect=httpx.TimeoutException("Timed out")
        )
        client = _make_async_client(error_resp)

        connector = BinanceConnector(client)
        result = await connector.health_check()

        assert result["status"] == "down"

    @pytest.mark.asyncio
    async def test_rate_limit_enforcement(self):
        """Excessive requests trigger rate-limit protection."""
        mock_resp = _mock_response(json_data=[])
        mock_resp.raise_for_status = MagicMock()
        client = _make_async_client(mock_resp)

        connector = BinanceConnector(client)
        # Fill the rate-limit buffer
        connector._request_timestamps.extend([float("inf")] * 60)

        with pytest.raises(FeedError) as exc_info:
            await connector.get_spot_and_recent_closes()
        assert "rate-limit" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# ChainlinkConnectorStub tests
# ---------------------------------------------------------------------------


class TestChainlinkConnectorStub:

    @pytest.mark.asyncio
    async def test_get_spot_and_recent_closes_raises(self):
        stub = ChainlinkConnectorStub()
        with pytest.raises(NotImplementedError) as exc_info:
            await stub.get_spot_and_recent_closes()
        assert "Chainlink" in str(exc_info.value)
        assert "issue #9" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_reference_price_raises(self):
        stub = ChainlinkConnectorStub()
        with pytest.raises(NotImplementedError) as exc_info:
            await stub.get_reference_price(1700000000)
        assert "Chainlink" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_health_check_returns_not_configured(self):
        stub = ChainlinkConnectorStub()
        result = await stub.health_check()
        assert result["status"] == "not_configured"
        assert "pending" in result["detail"]


# ---------------------------------------------------------------------------
# ConnectorRegistry tests
# ---------------------------------------------------------------------------


class TestConnectorRegistry:

    @pytest.mark.asyncio
    async def test_register_and_retrieve_price_connector(self):
        registry = ConnectorRegistry()
        mock_connector = MagicMock(spec=BinanceConnector)
        registry.register_price("binance", mock_connector)

        retrieved = registry.get_price("binance")
        assert retrieved is mock_connector

    def test_register_and_retrieve_market_connector(self):
        registry = ConnectorRegistry()
        mock_connector = MagicMock(spec=PolymarketConnector)
        registry.register_market("polymarket", mock_connector)

        retrieved = registry.get_market("polymarket")
        assert retrieved is mock_connector

    def test_get_price_missing_raises(self):
        registry = ConnectorRegistry()
        with pytest.raises(KeyError) as exc_info:
            registry.get_price("nonexistent")
        assert "nonexistent" in str(exc_info.value)

    def test_get_market_missing_raises(self):
        registry = ConnectorRegistry()
        with pytest.raises(KeyError) as exc_info:
            registry.get_market("nonexistent")
        assert "nonexistent" in str(exc_info.value)

    def test_list_connectors_empty(self):
        registry = ConnectorRegistry()
        assert registry.list_price_connectors() == []
        assert registry.list_market_connectors() == []

    def test_list_connectors_populated(self):
        registry = ConnectorRegistry()
        registry.register_price("a", MagicMock())
        registry.register_price("b", MagicMock())
        registry.register_market("poly", MagicMock())

        assert registry.list_price_connectors() == ["a", "b"]
        assert registry.list_market_connectors() == ["poly"]

    def test_get_primary_price_first_registered(self):
        """When no 'primary' named connector, first registered is returned."""
        registry = ConnectorRegistry()
        first = MagicMock()
        second = MagicMock()
        registry.register_price("binance", first)
        registry.register_price("chainlink", second)

        primary = registry.get_primary_price()
        assert primary is first

    def test_get_primary_price_explicit_name(self):
        """Connector named 'primary' takes precedence."""
        registry = ConnectorRegistry()
        first = MagicMock()
        primary_mock = MagicMock()
        registry.register_price("binance", first)
        registry.register_price("primary", primary_mock)

        primary = registry.get_primary_price()
        assert primary is primary_mock

    def test_get_primary_price_no_connectors_raises(self):
        registry = ConnectorRegistry()
        with pytest.raises(KeyError):
            registry.get_primary_price()

    def test_get_primary_market(self):
        registry = ConnectorRegistry()
        mock = MagicMock(spec=PolymarketConnector)
        registry.register_market("polymarket", mock)

        primary = registry.get_primary_market()
        assert primary is mock

    def test_get_primary_market_no_connectors_raises(self):
        registry = ConnectorRegistry()
        with pytest.raises(KeyError):
            registry.get_primary_market()

    @pytest.mark.asyncio
    async def test_health_check_all(self):
        """health_check_all aggregates results from all connectors."""
        registry = ConnectorRegistry()

        price_mock = MagicMock()
        price_mock.health_check = AsyncMock(return_value={"status": "ok"})
        market_mock = MagicMock()
        market_mock.health_check = AsyncMock(return_value={"status": "ok"})

        registry.register_price("binance", price_mock)
        registry.register_market("polymarket", market_mock)

        results = await registry.health_check_all()

        assert "binance" in results
        assert "polymarket" in results
        assert results["binance"]["status"] == "ok"
        assert results["polymarket"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_health_check_all_exception_handling(self):
        """Exceptions during health_check are captured gracefully."""
        registry = ConnectorRegistry()

        bad_mock = MagicMock()
        bad_mock.health_check = AsyncMock(side_effect=RuntimeError("boom"))

        registry.register_price("bad", bad_mock)
        results = await registry.health_check_all()

        assert results["bad"]["status"] == "error"
        assert "boom" in results["bad"]["detail"]

    def test_get_health_history(self):
        """Health history accumulates across health_check_all calls."""
        registry = ConnectorRegistry()

        mock_connector = MagicMock()
        mock_connector.health_check = AsyncMock(
            side_effect=[
                {"status": "ok", "seq": 1},
                {"status": "ok", "seq": 2},
                {"status": "degraded", "seq": 3},
            ]
        )
        registry.register_price("test", mock_connector)

        # History should be empty before any health check
        assert registry.get_health_history("test") == []

    @pytest.mark.asyncio
    async def test_health_history_accumulation(self):
        """Health history accumulates after health_check_all calls."""
        registry = ConnectorRegistry()

        mock_connector = MagicMock()
        mock_connector.health_check = AsyncMock(
            side_effect=[
                {"status": "ok", "seq": 1},
                {"status": "ok", "seq": 2},
                {"status": "degraded", "seq": 3},
            ]
        )
        registry.register_price("test", mock_connector)

        await registry.health_check_all()
        await registry.health_check_all()
        await registry.health_check_all()

        history = registry.get_health_history("test", limit=10)
        assert len(history) == 3
        assert history[0]["seq"] == 1
        assert history[1]["seq"] == 2
        assert history[2]["seq"] == 3

    def test_get_health_history_limit(self):
        """get_health_history respects the limit parameter."""
        registry = ConnectorRegistry()

        # Pre-seed history directly
        mock_connector = MagicMock()
        registry.register_price("test", mock_connector)
        await_result = AsyncMock(return_value={"status": "ok"})
        mock_connector.health_check = await_result

        # Manually add history items
        for i in range(15):
            registry._health_history["test"].append({"seq": i + 1})

        history = registry.get_health_history("test", limit=5)
        assert len(history) == 5
        assert history[-1]["seq"] == 15

    def test_get_health_history_unknown_connector(self):
        """Requesting history for an unregistered connector returns empty list."""
        registry = ConnectorRegistry()
        assert registry.get_health_history("nobody") == []
