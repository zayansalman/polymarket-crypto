"""Connector registry — manages the lifecycle and discovery of all connectors.

The registry provides a central lookup for price and market connectors,
supports health-check aggregation, and maintains a rolling history of health
results for observability.
"""

from __future__ import annotations

from collections import deque

from polymarket_exec.core.interfaces import AbstractMarketConnector, AbstractPriceConnector

# Each connector keeps up to this many historical health records.
_DEFAULT_HISTORY_LIMIT = 50


class ConnectorRegistry:
    """Central registry for price and market connectors.

    Typical usage::

        registry = ConnectorRegistry()
        registry.register_price("primary", BinanceConnector(client))
        registry.register_price("fallback", ChainlinkConnectorStub())
        registry.register_market("polymarket", PolymarketConnector(client))

        primary = registry.get_primary_price()
        market = registry.get_primary_market()
        health = await registry.health_check_all()
    """

    def __init__(self) -> None:
        self._connectors: dict[str, AbstractPriceConnector] = {}
        self._market_connectors: dict[str, AbstractMarketConnector] = {}
        self._health_history: dict[str, deque[dict]] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_price(
        self, name: str, connector: AbstractPriceConnector
    ) -> None:
        """Register a price connector under *name*."""
        self._connectors[name] = connector
        if name not in self._health_history:
            self._health_history[name] = deque(
                maxlen=_DEFAULT_HISTORY_LIMIT
            )

    def register_market(
        self, name: str, connector: AbstractMarketConnector
    ) -> None:
        """Register a market connector under *name*."""
        self._market_connectors[name] = connector
        if name not in self._health_history:
            self._health_history[name] = deque(
                maxlen=_DEFAULT_HISTORY_LIMIT
            )

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def get_price(self, name: str) -> AbstractPriceConnector:
        """Return the price connector registered under *name*.

        Raises:
            KeyError: if *name* is not registered.
        """
        try:
            return self._connectors[name]
        except KeyError as exc:
            raise KeyError(
                f"No price connector registered under '{name}'. "
                f"Available: {list(self._connectors.keys())}"
            ) from exc

    def get_market(self, name: str) -> AbstractMarketConnector:
        """Return the market connector registered under *name*.

        Raises:
            KeyError: if *name* is not registered.
        """
        try:
            return self._market_connectors[name]
        except KeyError as exc:
            raise KeyError(
                f"No market connector registered under '{name}'. "
                f"Available: {list(self._market_connectors.keys())}"
            ) from exc

    def get_primary_price(self) -> AbstractPriceConnector:
        """Return the primary (first-registered or named ``"primary"``) price
        connector.

        The lookup order is:
        1. Explicitly registered as ``"primary"``.
        2. First connector in registration order.

        Raises:
            KeyError: if no price connector is registered.
        """
        if "primary" in self._connectors:
            return self._connectors["primary"]
        if self._connectors:
            # dict preserves insertion order (Python 3.7+)
            return next(iter(self._connectors.values()))
        raise KeyError("No price connectors registered.")

    def get_primary_market(self) -> AbstractMarketConnector:
        """Return the primary market connector.

        Lookup order mirrors :meth:`get_primary_price`.

        Raises:
            KeyError: if no market connector is registered.
        """
        if "primary" in self._market_connectors:
            return self._market_connectors["primary"]
        if self._market_connectors:
            return next(iter(self._market_connectors.values()))
        raise KeyError("No market connectors registered.")

    def list_price_connectors(self) -> list[str]:
        """Return a list of registered price connector names."""
        return list(self._connectors.keys())

    def list_market_connectors(self) -> list[str]:
        """Return a list of registered market connector names."""
        return list(self._market_connectors.keys())

    # ------------------------------------------------------------------
    # Health checks
    # ------------------------------------------------------------------

    async def health_check_all(self) -> dict[str, dict]:
        """Run ``health_check()`` on every registered connector and store
        the results in history.

        Returns a dict mapping connector name to health result dict.
        """
        results: dict[str, dict] = {}

        for name, connector in self._connectors.items():
            try:
                result = await connector.health_check()
            except Exception as exc:  # noqa: BLE001
                result = {
                    "status": "error",
                    "latency_ms": 0.0,
                    "detail": f"Exception during health check: {exc.__class__.__name__}: {exc}",
                }
            self._health_history.setdefault(
                name, deque(maxlen=_DEFAULT_HISTORY_LIMIT)
            ).append(result)
            results[name] = result

        for name, connector in self._market_connectors.items():
            try:
                result = await connector.health_check()
            except Exception as exc:  # noqa: BLE001
                result = {
                    "status": "error",
                    "latency_ms": 0.0,
                    "detail": f"Exception during health check: {exc.__class__.__name__}: {exc}",
                }
            self._health_history.setdefault(
                name, deque(maxlen=_DEFAULT_HISTORY_LIMIT)
            ).append(result)
            results[name] = result

        return results

    def get_health_history(self, name: str, limit: int = 10) -> list[dict]:
        """Return the last *limit* health-check records for connector *name*.

        Returns:
            A list of health dicts, newest-last (i.e. chronological order).
        """
        history = self._health_history.get(name, deque())
        return list(history)[-limit:]
