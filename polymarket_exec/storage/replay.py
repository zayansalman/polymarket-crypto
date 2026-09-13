"""Deterministic replay — feed recorded market data through a signal generator.

The replay engine reads ticks from :class:`MarketDataRecorder` storage and
passes them one-by-one through an :class:`AbstractSignalGenerator`.  An
optional *callback* can be invoked for each ``(tick, signal)`` pair — this
is used by backtest harnesses to simulate execution.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from polymarket_exec.core.interfaces import AbstractSignalGenerator
from polymarket_exec.core.types import MarketWindow, Signal, Tick

from .recorder import MarketDataRecorder


class DeterministicReplay:
    """Replay recorded market data deterministically through a signal generator.

    Parameters:
        recorder: The :class:`MarketDataRecorder` that holds the data.
        signal_generator: An implementation of :class:`AbstractSignalGenerator`.
        rng_seed: Optional seed for any stochastic replay behaviour
            (e.g. fill-probability sampling).  When provided, repeated replays
            of the same data produce identical results.
    """

    def __init__(
        self,
        recorder: MarketDataRecorder,
        signal_generator: AbstractSignalGenerator,
        rng_seed: int | None = None,
    ) -> None:
        self.recorder = recorder
        self.signal_generator = signal_generator
        self._rng_seed = rng_seed
        self._stats: dict[str, Any] = {
            "total_windows": 0,
            "replayed_windows": 0,
            "total_ticks": 0,
            "signals_generated": 0,
            "windows_without_ticks": 0,
        }

    # ------------------------------------------------------------------
    # Replay API
    # ------------------------------------------------------------------

    async def replay_window(
        self,
        window_slug: str,
        callback: Callable[[Tick, Signal], Awaitable[None]] | None = None,
    ) -> list[Signal]:
        """Replay every tick for *window_slug* through the signal generator.

        Parameters:
            window_slug: The window identifier stored in the recorder.
            callback: Optional async callable invoked for each ``(tick, signal)``
                pair after generation.

        Returns:
            A list of the generated signals in tick order.
        """
        ticks = await self.recorder.get_ticks_for_window(window_slug)
        if not ticks:
            self._stats["windows_without_ticks"] += 1
            return []

        # Fetch the window to compute remaining_seconds
        windows = await self.recorder.get_recorded_windows(0, 2_000_000_000)
        window: MarketWindow | None = None
        for w in windows:
            if w.slug == window_slug:
                window = w
                break

        signals: list[Signal] = []
        for tick in ticks:
            remaining = max(0, window.end_ts - int(tick.ts.timestamp())) if window else 0
            signal = self.signal_generator.generate(
                spot=tick.spot_price,
                reference=tick.reference_price,
                sigma=tick.sigma_per_second,
                remaining_seconds=remaining,
                market_window=tick.window,
            )
            signals.append(signal)
            self._stats["total_ticks"] += 1
            self._stats["signals_generated"] += 1
            if callback is not None:
                await callback(tick, signal)

        self._stats["replayed_windows"] += 1
        return signals

    async def replay_range(
        self,
        start_ts: int,
        end_ts: int,
        callback: Callable[[Tick, Signal], Awaitable[None]] | None = None,
    ) -> dict[str, list[Signal]]:
        """Replay all ticks for all windows whose *start_ts* falls in range.

        Parameters:
            start_ts: Inclusive start (Unix seconds).
            end_ts: Inclusive end (Unix seconds).
            callback: Optional async callable invoked for each ``(tick, signal)``.

        Returns:
            A dict mapping ``window_slug -> [Signal, ...]``.
        """
        windows = await self.recorder.get_recorded_windows(start_ts, end_ts)
        self._stats["total_windows"] += len(windows)

        results: dict[str, list[Signal]] = {}
        for window in windows:
            sigs = await self.replay_window(window.slug, callback=callback)
            results[window.slug] = sigs

        return results

    async def get_coverage_report(self) -> dict[str, Any]:
        """Return statistics about replay coverage.

        Keys:
            - ``total_windows``: Total windows found in the requested range.
            - ``replayed_windows``: Windows that had at least one tick replayed.
            - ``total_ticks``: Total ticks processed.
            - ``signals_generated``: Total signals produced.
            - ``windows_without_ticks``: Windows with no recorded ticks.
            - ``time_range``: ``{start_ts, end_ts}`` of recorded data.
        """
        storage_report = await self.recorder.get_coverage_report()
        return {
            **self._stats,
            "time_range": storage_report.get("time_range", {}),
        }

    def reset_stats(self) -> None:
        """Reset accumulated replay statistics."""
        self._stats = {
            "total_windows": 0,
            "replayed_windows": 0,
            "total_ticks": 0,
            "signals_generated": 0,
            "windows_without_ticks": 0,
        }
