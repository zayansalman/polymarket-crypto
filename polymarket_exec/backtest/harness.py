"""Full-market backtest harness.

Unlike the conditional backtest (which only evaluates historical buys),
:class:`FullMarketBacktestHarness` runs the strategy on *every* recorded
tick in *every* recorded window, simulates entry/exit with configurable
market friction, and produces a complete :class:`BacktestResult`.
"""

from __future__ import annotations

import random
from typing import Any

from polymarket_exec.backtest.metrics import BacktestResult, FrictionModel
from polymarket_exec.core.interfaces import AbstractSignalGenerator
from polymarket_exec.core.types import (
    MarketWindow,
    Signal,
    SignalAction,
    StrategyParams,
    Tick,
)
from polymarket_exec.storage.recorder import MarketDataRecorder


class FullMarketBacktestHarness:
    """Run a strategy across all recorded ticks in a time range.

    The harness:
        1. Reads all recorded windows in *[start_ts, end_ts]*.
        2. For each window, reads every recorded tick.
        3. Generates a signal for each tick via *signal_gen*.
        4. Applies a :class:`FrictionModel` to decide if the signal fills.
        5. Simulates entry → hold → exit lifecycle per position.
        6. Aggregates PnL, drawdown, win rate, and exit attribution.

    Parameters:
        recorder: SQLite-backed :class:`MarketDataRecorder`.
        signal_gen: Strategy signal generator implementation.
        risk_params: Optional dict of risk limits (``max_open_positions``, etc.).
    """

    def __init__(
        self,
        recorder: MarketDataRecorder,
        signal_gen: AbstractSignalGenerator,
        risk_params: dict | None = None,
    ) -> None:
        self.recorder = recorder
        self.signal_gen = signal_gen
        self.risk_params = risk_params or {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        start_ts: int,
        end_ts: int,
        strategy_params: StrategyParams,
        friction: FrictionModel | None = None,
        rng_seed: int = 42,
        run_name: str = "full_market_backtest",
    ) -> BacktestResult:
        """Execute the full-market backtest.

        Parameters:
            start_ts: Inclusive start of the backtest period (Unix seconds).
            end_ts: Inclusive end of the backtest period (Unix seconds).
            strategy_params: Strategy parameters (edge thresholds, sizing).
            friction: Market friction model.  Uses defaults if *None*.
            rng_seed: Seed for deterministic friction sampling.
            run_name: Identifier for this run.

        Returns:
            A :class:`BacktestResult` with full metrics.
        """
        friction = friction or FrictionModel()
        rng = random.Random(rng_seed)

        windows = await self.recorder.get_recorded_windows(start_ts, end_ts)
        total_signals = 0
        signals_taken = 0
        windows_traded = 0

        # Track positions and equity
        open_position: dict[str, Any] | None = None
        trades: list[dict[str, Any]] = []  # closed trade records
        exit_reasons: dict[str, int] = {
            "TARGET": 0,
            "STOP": 0,
            "TIME": 0,
            "WINDOW_ROLL": 0,
            "BAND_REENTRY": 0,
            "OTHER": 0,
        }

        for window in windows:
            ticks = await self.recorder.get_ticks_for_window(window.slug)
            if not ticks:
                continue

            window_had_trade = False

            for tick in ticks:
                remaining = max(0, window.end_ts - int(tick.ts.timestamp()))

                # Check exit for open position before processing new signal
                if open_position is not None:
                    should_exit, exit_reason, exit_pnl = self._simulate_exit(
                        open_position, tick, strategy_params, remaining, window
                    )
                    if should_exit:
                        open_position["closed"] = True
                        open_position["exit_price"] = tick.spot_price
                        open_position["exit_reason"] = exit_reason
                        open_position["pnl"] = exit_pnl
                        trades.append(open_position)
                        exit_reasons[exit_reason] = exit_reasons.get(exit_reason, 0) + 1
                        open_position = None
                        continue  # Don't enter on the same tick we exit

                # Generate signal
                signal = self.signal_gen.generate(
                    spot=tick.spot_price,
                    reference=tick.reference_price,
                    sigma=tick.sigma_per_second,
                    remaining_seconds=remaining,
                    market_window=window,
                )
                total_signals += 1

                # Apply friction
                friction_signal = self._apply_friction(signal, friction, rng)
                if friction_signal is None:
                    continue  # Didn't pass friction check

                # Take the signal (enter a position)
                if (
                    friction_signal.action == SignalAction.ENTER_UP
                    or friction_signal.action == SignalAction.ENTER_DOWN
                ) and open_position is None:
                    side = (
                        "Up"
                        if friction_signal.action == SignalAction.ENTER_UP
                        else "Down"
                    )
                    open_position = {
                        "window_slug": window.slug,
                        "side": side,
                        "entry_price": tick.spot_price,
                        "entry_ts": int(tick.ts.timestamp()),
                        "notional": friction_signal.notional_usd,
                        "signal_edge": friction_signal.edge,
                        "confidence": friction_signal.confidence,
                        "fair_up_prob": friction_signal.fair_up_prob,
                        "closed": False,
                        "pnl": 0.0,
                        "exit_reason": None,
                        "exit_price": None,
                    }
                    signals_taken += 1
                    window_had_trade = True

            # Close any remaining open position at window end
            if open_position is not None:
                last_tick = ticks[-1]
                exit_pnl = self._calculate_pnl(
                    open_position["entry_price"],
                    last_tick.spot_price,
                    open_position["side"],
                    open_position["notional"],
                )
                open_position["closed"] = True
                open_position["exit_price"] = last_tick.spot_price
                open_position["exit_reason"] = "TIME"
                open_position["pnl"] = exit_pnl
                trades.append(open_position)
                exit_reasons["TIME"] += 1
                open_position = None

            if window_had_trade:
                windows_traded += 1

        # Build result
        wins = sum(1 for t in trades if t["pnl"] > 0)
        losses = sum(1 for t in trades if t["pnl"] <= 0)
        total_pnl = sum(t["pnl"] for t in trades)
        total_notional = sum(t["notional"] for t in trades)

        # Max drawdown from equity curve
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            equity += t["pnl"]
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)

        trade_count = wins + losses
        roi = total_pnl / total_notional if total_notional else 0.0
        win_rate = wins / trade_count if trade_count else 0.0
        avg_pnl = total_pnl / trade_count if trade_count else 0.0

        return BacktestResult(
            name=run_name,
            params={
                "min_trade_usd": strategy_params.min_trade_usd,
                "max_trade_usd": strategy_params.max_trade_usd,
                "entry_edge_min": strategy_params.entry_edge_min,
                "min_confidence": strategy_params.min_confidence,
                "entry_min_remaining_seconds": strategy_params.entry_min_remaining_seconds,
                "max_entry_price": strategy_params.max_entry_price,
                "min_entry_price": strategy_params.min_entry_price,
            },
            start_ts=start_ts,
            end_ts=end_ts,
            total_windows=len(windows),
            windows_traded=windows_traded,
            total_signals=total_signals,
            signals_taken=signals_taken,
            wins=wins,
            losses=losses,
            total_pnl_usd=round(total_pnl, 4),
            total_notional_usd=round(total_notional, 4),
            roi=round(roi, 6),
            win_rate=round(win_rate, 6),
            avg_pnl_usd=round(avg_pnl, 6),
            max_drawdown_usd=round(max_dd, 4),
            friction_model=friction.to_dict(),
            exit_reasons=exit_reasons,
        )

    # ------------------------------------------------------------------
    # Friction
    # ------------------------------------------------------------------

    def _apply_friction(
        self, signal: Signal, friction: FrictionModel, rng: random.Random
    ) -> Signal | None:
        """Apply market friction to a signal.

        Returns the (possibly modified) signal if it should be "filled",
        or ``None`` if the quote is too stale or the fill failed.
        """
        # Skip signals have no execution relevance
        if signal.action == SignalAction.SKIP:
            return signal

        # Fill probability check
        roll = rng.random()
        if roll > friction.total_fill_probability:
            return None  # No fill at all

        # Apply spread cost: reduce notional proportionally
        spread_cost = friction.spread_bps / 10_000
        adjusted_notional = signal.notional_usd * (1.0 - spread_cost)

        if adjusted_notional <= 0:
            return None

        # Return a modified signal with reduced notional
        from dataclasses import replace

        return replace(signal, notional_usd=round(adjusted_notional, 4))

    # ------------------------------------------------------------------
    # Exit simulation
    # ------------------------------------------------------------------

    def _simulate_exit(
        self,
        position: dict[str, Any],
        tick: Tick,
        params: StrategyParams,
        remaining_seconds: int,
        window: MarketWindow,
    ) -> tuple[bool, str, float]:
        """Check if an open position should be exited.

        Returns:
            ``(should_exit: bool, exit_reason: str, pnl: float)``
        """
        entry = position["entry_price"]
        side = position["side"]
        notional = position["notional"]
        current = tick.spot_price

        up_prob = tick.fair_up_prob

        # For Up positions: take profit if spot rises, stop if it falls
        # For Down positions: take profit if spot falls, stop if it rises
        # Use the fair_up_prob to estimate direction
        edge = up_prob - window.up_price if side == "Up" else (1 - up_prob) - window.down_price

        # Time-based exit (near window end)
        if remaining_seconds <= 10:
            pnl = self._calculate_pnl(entry, current, side, notional)
            return True, "TIME", pnl

        # Target hit: edge has decayed (moved against us)
        # For Up: if up_prob drops significantly below entry, exit
        # For Down: if up_prob rises significantly, exit
        if side == "Up":
            # Target: spot moved up enough that we're comfortable taking profit
            if current > entry * 1.002:  # Small profit target
                pnl = self._calculate_pnl(entry, current, side, notional)
                if pnl > 0:
                    return True, "TARGET", pnl
            # Stop: spot moved down significantly
            if current < entry * 0.998:
                pnl = self._calculate_pnl(entry, current, side, notional)
                return True, "STOP", pnl
        else:  # Down
            # Target: spot moved down
            if current < entry * 0.998:
                pnl = self._calculate_pnl(entry, current, side, notional)
                if pnl > 0:
                    return True, "TARGET", pnl
            # Stop: spot moved up
            if current > entry * 1.002:
                pnl = self._calculate_pnl(entry, current, side, notional)
                return True, "STOP", pnl

        # Band reentry: edge has completely reversed
        if edge is not None and edge * position.get("signal_edge", 0) < 0:
            # Edge has flipped sign - exit the position
            pnl = self._calculate_pnl(entry, current, side, notional)
            if pnl > 0 or remaining_seconds < 60:
                return True, "BAND_REENTRY", pnl

        return False, "", 0.0

    # ------------------------------------------------------------------
    # PnL calculation
    # ------------------------------------------------------------------

    def _calculate_pnl(
        self, entry: float, exit: float, side: str, notional: float
    ) -> float:
        """Calculate simulated PnL for a binary outcome position.

        For binary markets:
        - Up side pays 1.0 if spot >= reference at expiry, 0 otherwise
        - Down side pays 1.0 if spot < reference at expiry, 0 otherwise

        We approximate the running PnL as the change in probability
        valued at the current spot level, scaled by notional.

        Parameters:
            entry: Entry probability (price).
            exit: Exit probability (implied by current spot).
            side: "Up" or "Down".
            notional: USD notional of the position.

        Returns:
            PnL in USD.
        """
        # For simulation, treat entry/exit as probability-like prices
        # PnL = notional * (exit_value - entry_cost) / entry_cost
        # where exit_value is the implied pricing-model value at exit
        if side == "Up":
            # Up wins if price goes up from entry
            if exit > entry:
                return notional * ((exit - entry) / entry) if entry > 0 else 0.0
            else:
                return -notional * ((entry - exit) / entry) if entry > 0 else 0.0
        else:  # Down
            # Down wins if price goes down from entry
            if exit < entry:
                return notional * ((entry - exit) / entry) if entry > 0 else 0.0
            else:
                return -notional * ((exit - entry) / entry) if entry > 0 else 0.0
