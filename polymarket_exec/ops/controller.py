"""Unified bot controller — tick loop using execution manager + risk service.

The :class:`BotController` orchestrates one complete tick cycle:

1. Discover the current market window.
2. Fetch spot + reference prices.
3. Generate a signal from pricing-model edge.
4. Check open positions for exits.
5. Maybe submit a new order (after pre-trade risk check).
6. Record the tick for replay.

All heavy lifting is delegated to injected collaborators so the controller
remains a thin coordination layer.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime
from typing import Optional

from polymarket_exec.connectors.registry import ConnectorRegistry
from polymarket_exec.core.interfaces import (
    AbstractExecutionManager,
    AbstractRiskService,
    AbstractSignalGenerator,
)
from polymarket_exec.core.types import (
    BtcBotStatus,
    ExitReason,
    OrderState,
    PaperPosition,
    SignalAction,
    StrategyParams,
    Tick,
)
from polymarket_exec.execution.risk import RiskService
from polymarket_exec.storage.recorder import MarketDataRecorder
from polymarket_exec.strategy.pricing_model import fair_up_probability, sigma_per_second

log = logging.getLogger("polymarket_exec.controller")

# Default tick interval in seconds
_DEFAULT_TICK_INTERVAL = 5.0


class BotController:
    """Unified tick-based controller for the paper trading loop.

    Parameters
    ----------
    execution_manager:
        Handles order submission, exit checking, and forced close.
    risk_service:
        Provides pre-trade and post-trade risk controls.
    connector_registry:
        Registry of market and price connectors.
    signal_generator:
        Strategy that produces :class:`Signal` from market data.
    recorder:
        Persists ticks and windows for deterministic replay.
    stop_event:
        ``threading.Event`` that signals the loop to stop.
    params:
        Strategy parameters (thresholds, sizing, timing).
    tick_interval:
        Seconds to sleep between ticks (default 5.0).
    """

    def __init__(
        self,
        execution_manager: AbstractExecutionManager,
        risk_service: AbstractRiskService,
        connector_registry: ConnectorRegistry,
        signal_generator: AbstractSignalGenerator,
        recorder: MarketDataRecorder,
        stop_event: threading.Event,
        params: StrategyParams,
        tick_interval: float = _DEFAULT_TICK_INTERVAL,
    ) -> None:
        self.execution_manager = execution_manager
        self.risk_service = risk_service
        self.registry = connector_registry
        self.signal_generator = signal_generator
        self.recorder = recorder
        self.stop_event = stop_event
        self.params = params
        self.tick_interval = tick_interval

        self._last_tick: Optional[Tick] = None
        self._running: bool = False

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main loop until *stop_event* is set.

        Each iteration calls :meth:`tick` and then sleeps for
        *tick_interval* seconds.
        """
        self._running = True
        log.info("controller.run_start")

        while not self.stop_event.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("controller.tick_error")

            # Sleep in small increments so we respond quickly to stop_event
            for _ in range(int(self.tick_interval * 2)):
                if self.stop_event.is_set():
                    break
                await asyncio.sleep(0.5)

        self._running = False
        log.info("controller.run_stop")

    # ------------------------------------------------------------------
    # Single tick
    # ------------------------------------------------------------------

    async def tick(self) -> Optional[Tick]:
        """Execute one full tick cycle.

        Returns:
            The :class:`Tick` produced this cycle, or ``None`` if the
            tick could not be completed (e.g. market discovery failed).
        """
        # 1. Discover current window
        try:
            market_connector = self.registry.get_primary_market()
            window = await market_connector.discover_current_window()
        except Exception:
            log.exception("controller.market_discovery_failed")
            return None

        # 2. Get prices
        try:
            price_connector = self.registry.get_primary_price()
            spot, recent_closes = await price_connector.get_spot_and_recent_closes()
            reference = await price_connector.get_reference_price(window.start_ts)
        except Exception:
            log.exception("controller.price_fetch_failed")
            return None

        # 3. Compute pricing-model probability
        sigma = sigma_per_second(recent_closes)
        remaining_seconds = max(0, int(window.end_ts - datetime.now(UTC).timestamp()))
        fair_prob = fair_up_probability(spot, reference, sigma, remaining_seconds)

        # 4. Generate signal
        signal = self.signal_generator.generate(
            spot=spot,
            reference=reference,
            sigma=sigma,
            remaining_seconds=remaining_seconds,
            market_window=window,
        )

        # 5. Build the Tick
        tick = Tick(
            ts=datetime.now(UTC),
            window=window,
            spot_price=spot,
            reference_price=reference,
            sigma_per_second=sigma,
            fair_up_prob=fair_prob,
            signal=signal,
            feed_source="binance",  # primary price source
        )
        self._last_tick = tick

        # 6. Persist window + tick for replay
        try:
            await self.recorder.record_window(window)
            await self.recorder.record_tick(tick)
        except Exception:
            log.exception("controller.recorder_failed")

        # 7. Check open positions for exits
        open_positions = await self.execution_manager.get_open_positions()
        for position in open_positions:
            exit_reason = await self.execution_manager.check_exits(position, tick)
            if exit_reason is not None:
                await self._close_position(position, tick, exit_reason)

        # 8. Maybe enter a new position (post-exit check, so we can roll)
        # Re-fetch open positions after exits
        open_positions = await self.execution_manager.get_open_positions()
        if signal.action in (SignalAction.ENTER_UP, SignalAction.ENTER_DOWN):
            if await self.risk_service.pre_trade_check(signal, open_positions):
                try:
                    order = await self.execution_manager.submit_order(signal, window)
                    if order.state is OrderState.FILLED:
                        if isinstance(self.risk_service, RiskService):
                            self.risk_service.on_position_opened(order.filled_notional)
                    await self.risk_service.post_trade_report(order)
                except Exception:
                    log.exception("controller.order_submit_failed")

        return tick

    # ------------------------------------------------------------------
    # Position close helper
    # ------------------------------------------------------------------

    async def _close_position(
        self, position: PaperPosition, tick: Tick, reason: ExitReason
    ) -> None:
        """Close a single position and update risk tracking."""
        # Use the execution manager to close by updating the DB directly
        # For the paper execution manager, we update via the DB
        em = self.execution_manager

        # We need to compute PnL for the risk service
        from polymarket_exec.execution.paper import _pnl_for_position, _current_price_for_tick

        exit_price = _current_price_for_tick(tick, position.order.side)
        pnl = _pnl_for_position(position, exit_price)

        # Update position in DB
        db_conn = await em._ensure_db()
        now = datetime.now(UTC).isoformat(timespec="seconds")
        await db_conn.execute(
            """
            UPDATE paper_positions
            SET state = 'closed', closed_at = ?,
                exit_price = ?, exit_reason = ?, realized_pnl_usd = ?
            WHERE position_id = ?
            """,
            (now, exit_price, reason.value, pnl, position.position_id),
        )
        await db_conn.commit()

        # Update risk service
        if isinstance(self.risk_service, RiskService):
            self.risk_service.on_position_closed(
                position.order.filled_notional, pnl
            )

        log.info(
            "controller.position_closed",
            position_id=position.position_id,
            reason=reason.value,
            pnl=round(pnl, 4),
        )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def status(self) -> BtcBotStatus:
        """Return current controller status."""
        state = "running" if self._running else "stopped"
        mode = "paper"
        updated_at = datetime.now(UTC).isoformat(timespec="seconds")

        risk_state = await self.risk_service.get_risk_state()
        open_count = len(await self.execution_manager.get_open_positions())

        detail = (
            f"BTC paper loop {state}.\n"
            f"Open positions: {open_count}\n"
            f"Risk state: {risk_state}\n"
            f"Last tick: {self._last_tick.ts.isoformat() if self._last_tick else 'never'}"
        )

        return BtcBotStatus(
            state=state,
            mode=mode,
            updated_at=updated_at,
            detail=detail,
        )

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------

    async def shutdown(self, reason: ExitReason = ExitReason.STOP_REQUEST) -> None:
        """Gracefully shut down: close all open positions and stop."""
        self.stop_event.set()
        closed = await self.execution_manager.force_close_all(reason)
        log.info("controller.shutdown", closed_positions=len(closed))

        # Update risk service for each closed position
        if isinstance(self.risk_service, RiskService):
            for pos in closed:
                self.risk_service.on_position_closed(
                    pos.order.filled_notional, 0.0
                )
