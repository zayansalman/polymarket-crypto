"""Integration tests for the connector layer.

These tests exercise the full flow: registry -> get connector -> call method
with mocked HTTP responses.  No real network calls are made.
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
from polymarket_exec.core.exceptions import FeedError, MarketDiscoveryError


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
# Full-flow integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_flow_registry_to_polymarket_discovery():
    """End-to-end: registry -> PolymarketConnector -> discover_current_window."""
    market_payload = {
        "slug": "btc-updown-5m-1700000000",
        "question": "Bitcoin Up or Down - Nov 14, 2023?",
        "outcomes": '["Up", "Down"]',
        "outcomePrices": '["0.52", "0.48"]',
    }
    mock_resp = _mock_response(json_data=[market_payload])
    mock_resp.raise_for_status = MagicMock()
    client = _make_async_client(mock_resp)

    # Build the system under test via the registry
    registry = ConnectorRegistry()
    poly = PolymarketConnector(client)
    registry.register_market("polymarket", poly)

    # Resolve connector through registry and exercise it
    resolved = registry.get_market("polymarket")
    window = await resolved.discover_current_window()

    assert window.slug.startswith("btc-updown-5m-")
    assert window.up_price == 0.52
    assert window.down_price == 0.48
    assert resolved is poly  # Identity check


@pytest.mark.asyncio
async def test_full_flow_registry_to_binance_spot():
    """End-to-end: registry -> BinanceConnector -> get_spot_and_recent_closes."""
    klines = [
        [i, "100", "101", "99", str(50000.0 + i), "1"]
        for i in range(90)
    ]
    mock_resp = _mock_response(json_data=klines)
    mock_resp.raise_for_status = MagicMock()
    client = _make_async_client(mock_resp)

    registry = ConnectorRegistry()
    binance = BinanceConnector(client)
    registry.register_price("binance", binance)

    resolved = registry.get_price("binance")
    latest, closes = await resolved.get_spot_and_recent_closes()

    assert latest == 50000.0 + 89
    assert len(closes) == 90
    assert resolved is binance


@pytest.mark.asyncio
async def test_full_flow_registry_to_binance_reference():
    """End-to-end: registry -> BinanceConnector -> get_reference_price."""
    klines = [
        [1700000000000, "50000", "50100", "49900", "50050", "100"]
    ]
    mock_resp = _mock_response(json_data=klines)
    mock_resp.raise_for_status = MagicMock()
    client = _make_async_client(mock_resp)

    registry = ConnectorRegistry()
    binance = BinanceConnector(client)
    registry.register_price("primary", binance)

    resolved = registry.get_primary_price()
    ref = await resolved.get_reference_price(1700000000)

    assert ref == 50050.0
    assert resolved is binance


@pytest.mark.asyncio
async def test_full_flow_multiple_price_connectors():
    """Registry with multiple price connectors; primary resolves correctly."""
    klines = [[0, "100", "101", "99", "50000", "1"]]
    mock_resp = _mock_response(json_data=klines)
    mock_resp.raise_for_status = MagicMock()
    client = _make_async_client(mock_resp)

    registry = ConnectorRegistry()
    registry.register_price("binance", BinanceConnector(client))
    registry.register_price("chainlink", ChainlinkConnectorStub())

    # primary should be binance (first registered)
    primary = registry.get_primary_price()
    assert isinstance(primary, BinanceConnector)

    # But chainlink should still be retrievable by name
    stub = registry.get_price("chainlink")
    assert isinstance(stub, ChainlinkConnectorStub)


# ---------------------------------------------------------------------------
# Health check integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_history_accumulation():
    """Multiple health_check_all calls accumulate history correctly."""
    registry = ConnectorRegistry()

    binance_mock = MagicMock()
    binance_mock.health_check = AsyncMock(
        side_effect=[
            {"status": "ok", "latency_ms": 10.0},
            {"status": "ok", "latency_ms": 12.0},
            {"status": "degraded", "latency_ms": 2500.0},
        ]
    )
    poly_mock = MagicMock()
    poly_mock.health_check = AsyncMock(
        side_effect=[
            {"status": "ok", "latency_ms": 20.0},
            {"status": "ok", "latency_ms": 22.0},
            {"status": "ok", "latency_ms": 21.0},
        ]
    )

    registry.register_price("binance", binance_mock)
    registry.register_market("polymarket", poly_mock)

    # Run three rounds of health checks
    r1 = await registry.health_check_all()
    await registry.health_check_all()
    r3 = await registry.health_check_all()

    # Verify aggregated results
    assert r1["binance"]["status"] == "ok"
    assert r1["polymarket"]["status"] == "ok"
    assert r3["binance"]["status"] == "degraded"
    assert r3["polymarket"]["status"] == "ok"

    # Verify history accumulation
    binance_history = registry.get_health_history("binance", limit=10)
    assert len(binance_history) == 3
    assert binance_history[0]["latency_ms"] == 10.0
    assert binance_history[1]["latency_ms"] == 12.0
    assert binance_history[2]["latency_ms"] == 2500.0

    poly_history = registry.get_health_history("polymarket", limit=10)
    assert len(poly_history) == 3
    assert poly_history[2]["latency_ms"] == 21.0


@pytest.mark.asyncio
async def test_health_check_history_limit():
    """History respects per-connector limit."""
    registry = ConnectorRegistry()

    mock = MagicMock()
    mock.health_check = AsyncMock(return_value={"status": "ok"})
    registry.register_price("repeat", mock)

    for _ in range(60):
        await registry.health_check_all()

    # Default history limit is 50, so we should get at most 50 back
    history = registry.get_health_history("repeat", limit=100)
    assert len(history) == 50

    history_limited = registry.get_health_history("repeat", limit=10)
    assert len(history_limited) == 10


@pytest.mark.asyncio
async def test_health_check_all_with_exception_connector():
    """One failing connector should not prevent others from being checked."""
    registry = ConnectorRegistry()

    good_mock = MagicMock()
    good_mock.health_check = AsyncMock(return_value={"status": "ok"})
    bad_mock = MagicMock()
    bad_mock.health_check = AsyncMock(side_effect=ConnectionError("network down"))

    registry.register_price("good", good_mock)
    registry.register_price("bad", bad_mock)

    results = await registry.health_check_all()

    assert results["good"]["status"] == "ok"
    assert results["bad"]["status"] == "error"
    assert "network down" in results["bad"]["detail"]


# ---------------------------------------------------------------------------
# Error propagation integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_error_propagates_from_binance():
    """FeedError from Binance connector propagates to the caller."""
    error_resp = _mock_response(status_code=418, text="I'm a teapot")
    error_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "Rate limited",
            request=MagicMock(spec=httpx.Request),
            response=error_resp,
        )
    )
    client = _make_async_client(error_resp)

    registry = ConnectorRegistry()
    registry.register_price("binance", BinanceConnector(client))

    resolved = registry.get_price("binance")
    with pytest.raises(FeedError) as exc_info:
        await resolved.get_spot_and_recent_closes()
    assert "418" in str(exc_info.value)


@pytest.mark.asyncio
async def test_market_discovery_error_propagates():
    """MarketDiscoveryError from Polymarket propagates to the caller."""
    empty_resp = _mock_response(json_data=[])
    empty_resp.raise_for_status = MagicMock()
    client = _make_async_client(empty_resp)

    registry = ConnectorRegistry()
    registry.register_market("polymarket", PolymarketConnector(client))

    resolved = registry.get_market("polymarket")
    with pytest.raises(MarketDiscoveryError) as exc_info:
        await resolved.discover_current_window()
    assert "Could not discover" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Chainlink stub integration test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chainlink_stub_in_registry():
    """Chainlink stub can be registered and its health checked."""
    registry = ConnectorRegistry()
    stub = ChainlinkConnectorStub()
    registry.register_price("chainlink", stub)

    results = await registry.health_check_all()
    assert results["chainlink"]["status"] == "not_configured"
    assert "pending" in results["chainlink"]["detail"]

    with pytest.raises(NotImplementedError):
        await registry.get_price("chainlink").get_spot_and_recent_closes()
