"""Chainlink Data Streams connector stub.

Implements ``AbstractPriceConnector`` interface but defers all operations to a
future integration (GitHub issue #9).  This stub preserves the architecture
contract so downstream components (registry, strategy, health checks) can treat
Chainlink as a first-class price source even before the integration is live.

What Chainlink Data Streams would provide
-----------------------------------------
`Chainlink Data Streams <https://docs.chain.link/data-streams/>`_ deliver
verifiable, low-latency, settlement-aware BTC/USD price updates together with a
cryptographic proof of correctness (Merkle-root attestation).  Unlike a
CEX REST feed, Data Streams are designed for on-chain settlement:

* **Cryptographic proof** — each report carries a signed merkle root that can
  be verified on-chain, eliminating the trust assumption in a single exchange.
* **Decentralised oracle network** — prices are aggregated from multiple
  premium data providers rather than a single venue.
* **Settlement trigger** — a resolved report can be pushed on-chain to trigger
  payout logic, which is relevant if the bot ever moves from paper trading to
  on-chain binary options.
* **Stream vs REST** — the production implementation would likely use the
  WebSocket stream for real-time ticks and the REST API for historical /
  reference-price lookups.

When this stub is replaced, the real implementation will:
1. Open a WebSocket connection to ``wss://<router>.ws.chain.link``.
2. Subscribe to the BTC/USD CEX Price Streams feed (stream ID TBD).
3. Decode each ``FullReport`` or ``BenchmarkPrice`` payload.
4. Verify the on-chain report signature (optional, depending on trust model).
5. Return the decoded ``benchmarkPrice`` as ``float``.

Refs: #9
"""

from __future__ import annotations

from polymarket_exec.core.interfaces import AbstractPriceConnector


class ChainlinkConnectorStub(AbstractPriceConnector):
    """Placeholder connector for Chainlink Data Streams integration (issue #9).

    All data-fetching methods raise :class:`NotImplementedError` with a
    helpful message.  ``health_check`` returns a static "not_configured"
    response so the health-aggregator can report the pending status.
    """

    def __init__(
        self,
        stream_url: str = "https://data.chain.link/streams/btc-usd-cexprice-streams",
    ) -> None:
        self._stream_url = stream_url

    async def get_spot_and_recent_closes(self) -> tuple[float, list[float]]:
        """Not implemented — Chainlink Data Streams integration is pending (#9)."""
        raise NotImplementedError(
            "ChainlinkConnectorStub.get_spot_and_recent_closes() is not implemented. "
            "Chainlink Data Streams integration is pending (issue #9). "
            "This method would decode a FullReport or BenchmarkPrice payload from the "
            "BTC/USD CEX Price Streams feed and return the benchmark price together "
            "with a recent history buffer reconstructed from stream messages."
        )

    async def get_reference_price(self, window_start_ts: int) -> float:
        """Not implemented — Chainlink Data Streams integration is pending (#9)."""
        raise NotImplementedError(
            "ChainlinkConnectorStub.get_reference_price() is not implemented. "
            "Chainlink Data Streams integration is pending (issue #9). "
            "This method would query the Data Streams REST API for the "
            "cryptographically-verified BTC/USD price at the requested window "
            "start time (window_start_ts)."
        )

    async def health_check(self) -> dict:
        """Return a static status indicating the integration is pending."""
        return {
            "status": "not_configured",
            "latency_ms": 0.0,
            "detail": (
                "Chainlink Data Streams integration pending (issue #9). "
                f"Configured stream URL: {self._stream_url}"
            ),
        }
