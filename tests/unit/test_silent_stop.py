"""Unit tests for the silent-stop detector (#138).

The #147 watchdog handles a WEDGED loop (thread alive, heartbeat stale). It
cannot catch a loop or whole process that DIES — a dead in-process watchdog
notifies no one, and the persisted ``polymarket_bot.state`` is left reading "running"
with no stop event in the feed (the 06-24 incident). ``get_status`` already
self-heals that stale row to "stopped"; #138 makes it emit exactly one
notification per silent death so the operator is told, not left staring at a
frozen dashboard.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

import polymarket_bot.controller as controller
import db as _db
from polymarket_bot.controller import get_status, is_silent_stop


class TestIsSilentStop:
    def test_true_when_running_but_no_runner(self) -> None:
        """Persisted 'running' + dead thread == silent death."""
        assert is_silent_stop("running", runner_alive=False) is True

    def test_false_when_running_and_alive(self) -> None:
        assert is_silent_stop("running", runner_alive=True) is False

    def test_false_when_cleanly_stopped(self) -> None:
        """An operator stop leaves state 'stopped' — never a silent death."""
        assert is_silent_stop("stopped", runner_alive=False) is False

    def test_false_when_stopped_and_alive(self) -> None:
        assert is_silent_stop("stopped", runner_alive=True) is False


@pytest_asyncio.fixture
async def bot_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "test_silent_stop.db")
    await _db.init_db()
    # Isolate the module-level detector flag and runner thread per test.
    controller._silent_stop_notified = False
    controller._runner_thread = None
    yield _db
    controller._silent_stop_notified = False
    controller._runner_thread = None


async def _notifications(db, event_type: str) -> list[dict]:
    async with db.connect() as conn:
        async with conn.execute(
            "SELECT * FROM notification_feed WHERE event_type = ?", (event_type,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_silent_stop_notifies_once_and_heals(bot_db, monkeypatch) -> None:
    """state='running' with no live runner → one notification + heal to stopped."""
    monkeypatch.setattr(controller, "_is_runner_alive", lambda: False)
    await _db.set_config("polymarket_bot.state", "running")
    await _db.set_config("polymarket_bot.updated_at", "2026-07-07T06:40:00+00:00")

    status = await get_status()

    assert status.state == "stopped"  # self-healed
    notifs = await _notifications(bot_db, "silent_stop")
    assert len(notifs) == 1
    assert "silent bot stop" in notifs[0]["message"].lower()
    # The last heartbeat is surfaced so the operator knows when it died.
    assert "2026-07-07T06:40:00+00:00" in notifs[0]["message"]


@pytest.mark.asyncio
async def test_silent_stop_does_not_spam_on_repeated_polls(bot_db, monkeypatch) -> None:
    """The dashboard polls get_status constantly — only ONE alert per death."""
    monkeypatch.setattr(controller, "_is_runner_alive", lambda: False)
    await _db.set_config("polymarket_bot.state", "running")

    for _ in range(5):
        await get_status()

    notifs = await _notifications(bot_db, "silent_stop")
    assert len(notifs) == 1


@pytest.mark.asyncio
async def test_healthy_running_never_notifies(bot_db, monkeypatch) -> None:
    """A live runner thread is not a silent stop — no alert."""
    monkeypatch.setattr(controller, "_is_runner_alive", lambda: True)
    await _db.set_config("polymarket_bot.state", "running")

    status = await get_status()

    assert status.state == "running"
    assert await _notifications(bot_db, "silent_stop") == []


@pytest.mark.asyncio
async def test_clean_stop_never_notifies(bot_db, monkeypatch) -> None:
    """Operator-stopped bot (state 'stopped') is not a silent death."""
    monkeypatch.setattr(controller, "_is_runner_alive", lambda: False)
    await _db.set_config("polymarket_bot.state", "stopped")

    await get_status()

    assert await _notifications(bot_db, "silent_stop") == []


@pytest.mark.asyncio
async def test_detector_rearms_after_runner_recovers(bot_db, monkeypatch) -> None:
    """After a healthy poll re-arms the flag, a SECOND silent death alerts again."""
    alive = {"v": False}
    monkeypatch.setattr(controller, "_is_runner_alive", lambda: alive["v"])

    # First death → 1 alert.
    await _db.set_config("polymarket_bot.state", "running")
    await get_status()
    assert len(await _notifications(bot_db, "silent_stop")) == 1

    # Operator restarts: runner alive + state running → re-arms detector.
    alive["v"] = True
    await _db.set_config("polymarket_bot.state", "running")
    await get_status()
    assert controller._silent_stop_notified is False

    # Second death → a fresh alert (total 2).
    alive["v"] = False
    await _db.set_config("polymarket_bot.state", "running")
    await get_status()
    assert len(await _notifications(bot_db, "silent_stop")) == 2
