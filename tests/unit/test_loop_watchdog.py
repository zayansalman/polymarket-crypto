"""Unit tests for the in-process loop watchdog (#147).

The 2026-07-05 incident: the paper loop wedged on an unbounded await
mid-window — process alive, dashboard serving, zero ticks for ~14h. The
watchdog converts every wedge-class into a short blip: a stalled heartbeat
while the bot should be running triggers an abandon-and-respawn in paper
mode (notify-only in live — never auto-restart a real-money path).
"""

from __future__ import annotations

import threading
import time

import pytest

import polymarket_bot.controller as controller
import polymarket_bot.paper as paper
from polymarket_bot.controller import watchdog_verdict


class TestWatchdogVerdict:
    def test_restart_when_paper_stalls(self) -> None:
        assert watchdog_verdict(True, "paper", 300.0, threshold=180.0) == "restart"

    def test_notify_only_when_live_stalls(self) -> None:
        """Live is real money: the watchdog must NEVER auto-restart it."""
        assert watchdog_verdict(True, "live", 300.0, threshold=180.0) == "notify"

    def test_ok_when_fresh(self) -> None:
        assert watchdog_verdict(True, "paper", 5.0, threshold=180.0) == "ok"

    def test_ok_when_not_desired_running(self) -> None:
        """A stopped bot has no heartbeat by design — never a stall."""
        assert watchdog_verdict(False, "paper", 1e9, threshold=180.0) == "ok"

    def test_never_beaten_counts_as_stalled(self) -> None:
        assert watchdog_verdict(True, "paper", float("inf"), threshold=180.0) == "restart"


class TestHeartbeat:
    def test_infinite_age_before_first_beat(self) -> None:
        paper._heartbeat_monotonic = None
        assert paper.heartbeat_age_seconds() == float("inf")

    def test_age_measured_from_last_beat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        t = {"now": 1000.0}
        monkeypatch.setattr(paper.time, "monotonic", lambda: t["now"])
        paper._beat()
        t["now"] = 1042.5
        assert paper.heartbeat_age_seconds() == pytest.approx(42.5)


class TestGenerationGuard:
    def test_superseded_generation_may_not_finalize(self) -> None:
        """An abandoned (wedged, later un-wedged) loop must not clear the NEW
        loop's globals or write state=stopped."""
        paper._loop_generation = 7
        assert paper._is_current_generation(7) is True
        paper._loop_generation = 8  # a newer loop was spawned
        assert paper._is_current_generation(7) is False


class TestForceRespawn:
    def test_force_spawns_fresh_runner_even_while_old_thread_alive(self) -> None:
        """The 07-05 wedge: _runner_thread alive but stuck. force=True must
        abandon it (its stop_event set) and start a NEW thread + event."""
        release = threading.Event()
        wedged = threading.Thread(target=release.wait, daemon=True)
        wedged.start()
        old_event = threading.Event()
        controller._runner_thread = wedged
        controller._stop_event = old_event
        spawned: list[threading.Event] = []

        def fake_run(stop_event: threading.Event, mode: str | None = None) -> None:
            spawned.append(stop_event)
            stop_event.wait(5.0)

        orig = controller._run_loop_in_thread
        controller._run_loop_in_thread = fake_run  # type: ignore[assignment]
        try:
            controller._ensure_runner_started(force=True)
            deadline = time.monotonic() + 2.0
            while not spawned and time.monotonic() < deadline:
                time.sleep(0.01)
            assert spawned, "force respawn never started a new runner"
            assert controller._runner_thread is not wedged
            assert controller._stop_event is not old_event
            assert old_event.is_set(), "abandoned loop's stop_event must be set"
        finally:
            controller._run_loop_in_thread = orig  # type: ignore[assignment]
            if controller._stop_event is not None:
                controller._stop_event.set()
            release.set()
            controller._runner_thread = None
            controller._stop_event = None

    def test_without_force_alive_thread_short_circuits(self) -> None:
        release = threading.Event()
        alive = threading.Thread(target=release.wait, daemon=True)
        alive.start()
        controller._runner_thread = alive
        controller._stop_event = threading.Event()
        try:
            controller._ensure_runner_started()
            assert controller._runner_thread is alive  # unchanged
        finally:
            release.set()
            controller._runner_thread = None
            controller._stop_event = None
