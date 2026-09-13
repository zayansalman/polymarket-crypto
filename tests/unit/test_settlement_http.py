"""Settlement/reference HTTP client must use HTTP/2 (#120).

Polymarket's ``crypto-price`` reference endpoint sits behind Cloudflare bot
management, which now 403s ("Just a moment...") a Chrome-spoofed request made
over HTTP/1.1 — the UA-vs-protocol mismatch reads as a bot. Real Chrome uses
HTTP/2, and the same request over HTTP/2 passes (measured: 0/5 vs 5/5). Without
a reference price the bot books ``reference_price=0`` and SKIPs every window, so
this client config is load-bearing for trading at all.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from polymarket_bot import paper


def test_settlement_client_enables_http2(monkeypatch) -> None:
    captured: dict = {}

    def fake_async_client(*args, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(paper.httpx, "AsyncClient", fake_async_client)
    paper._make_settlement_client()
    assert captured.get("http2") is True
    assert captured.get("follow_redirects") is True
