"""Abstract base classes for all pluggable system components."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from .types import (
    ExitReason,
    MarketWindow,
    PaperOrder,
    PaperPosition,
    Signal,
    Tick,
)


class AbstractMarketConnector(ABC):
    """Responsible for discovering the current trading window on a prediction market."""

    @abstractmethod
    async def discover_current_window(self) -> MarketWindow:
        """Return the active :class:`MarketWindow` for the current time period.

        Raises:
            MarketDiscoveryError: when no active market can be found.
        """
        ...

    @abstractmethod
    async def health_check(self) -> dict:
        """Return a health-status dict (keys: ``ok: bool``, ``latency_ms: float``,
        ``detail: str``, etc.)."""
        ...


class AbstractPriceConnector(ABC):
    """Provides BTC spot prices and recent close history for volatility estimation."""

    @abstractmethod
    async def get_spot_and_recent_closes(self) -> tuple[float, list[float]]:
        """Return ``(latest_spot_price, recent_closes)``.

        ``recent_closes`` is ordered oldest-first and is used by
        :func:`polymarket_exec.strategy.pricing_model.sigma_per_second`.
        """
        ...

    @abstractmethod
    async def get_reference_price(self, window_start_ts: int) -> float:
        """Return the reference (opening) price for the window starting at
        *window_start_ts* (Unix seconds)."""
        ...

    @abstractmethod
    async def health_check(self) -> dict:
        """Return a health-status dict."""
        ...


class AbstractSignalGenerator(ABC):
    """Given market data, produce a :class:`~polymarket_exec.core.types.Signal`."""

    @abstractmethod
    def generate(
        self,
        spot: float,
        reference: float,
        sigma: float,
        remaining_seconds: int,
        market_window: MarketWindow,
    ) -> Signal:
        """Generate a signal for the current tick.

        Parameters:
            spot: Current BTC spot price.
            reference: Window reference (opening) price.
            sigma: One-second volatility estimate.
            remaining_seconds: Seconds until the window closes.
            market_window: The current :class:`MarketWindow` (carries up/down prices).

        Returns:
            A fully populated :class:`~polymarket_exec.core.types.Signal`.
        """
        ...


class AbstractExecutionManager(ABC):
    """Manages order submission, exit checking, and emergency close."""

    @abstractmethod
    async def submit_order(
        self,
        signal: Signal,
        window: MarketWindow,
    ) -> PaperOrder:
        """Submit an order derived from *signal* for *window*.

        Returns:
            A :class:`PaperOrder` in ``PENDING`` (or later) state.
        """
        ...

    @abstractmethod
    async def check_exits(
        self,
        position: PaperPosition,
        tick: Tick,
    ) -> Optional[ExitReason]:
        """Evaluate whether *position* should be exited given the latest *tick*.

        Returns:
            The :class:`ExitReason` if the position should close, else ``None``.
        """
        ...

    @abstractmethod
    async def force_close_all(
        self,
        reason: ExitReason,
    ) -> list[PaperPosition]:
        """Forcibly close every open position.

        Returns:
            The list of positions that were closed.
        """
        ...


class AbstractRiskService(ABC):
    """Venue-independent pre-trade and post-trade risk controls."""

    @abstractmethod
    async def pre_trade_check(
        self,
        signal: Signal,
        open_positions: list[PaperPosition],
    ) -> bool:
        """Return ``True`` iff the proposed trade passes all risk gates.

        Typical checks:
        * max open positions (default 1)
        * max exposure per window
        * late-window entry filter
        * edge threshold validation
        """
        ...

    @abstractmethod
    async def post_trade_report(self, order: PaperOrder) -> dict:
        """Return a post-trade risk report (PnL tracking, drawdown, etc.)."""
        ...

    @abstractmethod
    async def get_risk_state(self) -> str:
        """Return a short human-readable risk state string, e.g. ``"OK"`` or
        ``"BREACH: ..."``."""
        ...
