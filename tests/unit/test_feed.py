"""Tests for the copier's fill feed (#182).

The feed is where the copier's edge is won or lost: the data-api transport is
~20s stale and median slippage there is 9.56c against a ~1c/share edge, so
silently falling back to it produces numbers that look plausible and are
inverted. These tests pin the refusal.
"""

from __future__ import annotations

import pytest

from polymarket_bot.pairarb.feed import (
    PUBLIC_HTTP_RPCS,
    FeedFill,
    FeedUnavailable,
    _pick_public_rpc,
    _rpc,
    http_poll_fills,
    open_feed,
)


def test_refuses_to_open_a_stale_feed_by_default(monkeypatch):
    monkeypatch.delenv("POLYGON_RPC_WSS", raising=False)
    with pytest.raises(FeedUnavailable) as exc:
        open_feed("0xabc")
    assert "20s stale" in str(exc.value)


def test_stale_feed_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("POLYGON_RPC_WSS", raising=False)
    gen = open_feed("0xabc", allow_api_fallback=True)
    assert gen is not None
    gen.aclose()


def test_prefers_onchain_when_configured(monkeypatch):
    monkeypatch.setenv("POLYGON_RPC_WSS", "wss://example.invalid/v2/key")
    gen = open_feed("0xabc")
    assert gen is not None
    gen.aclose()


def test_every_fill_carries_its_own_observed_lag():
    """Lag must travel with the fill so a consumer can drop stale ones."""
    f = FeedFill(token_id="t", price=0.5, shares=5.0, observed_lag=21.4, source="api")
    assert f.observed_lag == 21.4
    assert f.is_maker is None  # the API transport cannot tell


def test_onchain_fill_can_report_maker_status():
    f = FeedFill(
        token_id="t", price=0.5, shares=5.0, observed_lag=0.0,
        source="onchain", is_maker=True,
    )
    assert f.is_maker is True


class _FakeRpcResponse:
    def __init__(self, result=None, ok=True):
        self._result = result
        self._ok = ok

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("http error")

    def json(self):
        return {"result": self._result}


class _FakeRpcClient:
    """Answers eth_blockNumber for a fixed set of URLs, fails everyone else."""

    def __init__(self, live_urls: dict[str, str]):
        self._live = live_urls
        self.posted: list[str] = []

    async def post(self, url, json, timeout):  # noqa: A002 - matches httpx's signature
        self.posted.append(url)
        if url not in self._live:
            raise RuntimeError(f"unreachable: {url}")
        return _FakeRpcResponse(result=self._live[url])


@pytest.mark.asyncio
async def test_rpc_returns_the_result_field():
    client = _FakeRpcClient({"https://good": "0x2a"})
    result = await _rpc(client, "https://good", "eth_blockNumber", [])
    assert result == "0x2a"


@pytest.mark.asyncio
async def test_rpc_raises_on_http_error():
    class _FailingClient:
        async def post(self, url, json, timeout):
            return _FakeRpcResponse(ok=False)

    with pytest.raises(RuntimeError):
        await _rpc(_FailingClient(), "https://x", "eth_blockNumber", [])


@pytest.mark.asyncio
async def test_pick_public_rpc_skips_dead_endpoints():
    """Every endpoint but the last is unreachable — must still find it."""
    last = PUBLIC_HTTP_RPCS[-1]
    client = _FakeRpcClient({last: "0x1"})
    picked = await _pick_public_rpc(client)
    assert picked == last
    # Tried the dead ones before succeeding, in declared order.
    assert client.posted[: len(PUBLIC_HTTP_RPCS) - 1] == list(PUBLIC_HTTP_RPCS[:-1])


@pytest.mark.asyncio
async def test_pick_public_rpc_returns_none_when_all_unreachable():
    client = _FakeRpcClient({})
    assert await _pick_public_rpc(client) is None


def test_http_poll_fills_returns_an_async_generator():
    """Construction alone must not perform I/O (lazy, like the other transports)."""
    gen = http_poll_fills("0xabc")
    assert gen is not None
    gen.aclose()
