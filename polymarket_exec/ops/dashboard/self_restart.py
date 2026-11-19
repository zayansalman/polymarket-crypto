"""Restart the local paper app by itself after its code changes on disk.

Luke asked (2026-11-18) not to have to end Python and reopen the app after
every update. When the app was started by the Windows launcher
(``tools/windows_paper_startup.py`` sets ``PAPER_SELF_RESTART=1``), this task
watches the bot's Python files. After a change has settled, every changed file
compiles, and the bot is in paper mode with NO open trade, it hands off to
``windows_paper_startup.py restart`` and exits. That is the same as ending
Python in Task Manager, only timed for a moment with nothing open, and the
launcher starts the app again (paper mode, no window) once this process is
gone. Real-money mode is never restarted.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from db import connect, notify  # type: ignore[import-untyped]
from logging_setup import get_logger  # type: ignore[import-untyped]

log = get_logger("self_restart")

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
# Code the running app imports. Tests, tools and docs don't need a restart.
WATCHED = ("polymarket_bot", "polymarket_exec", "main.py", "config.py", "db.py",
           "logging_setup.py")
CHECK_SECONDS = 15.0
SETTLE_SECONDS = 20.0  # an update copies several files; wait for the last one


def enabled() -> bool:
    return os.name == "nt" and os.environ.get("PAPER_SELF_RESTART") == "1"


def snapshot(root: Path = REPOSITORY_ROOT) -> dict[str, float]:
    """mtime of every watched ``.py`` file."""
    out: dict[str, float] = {}
    for name in WATCHED:
        path = root / name
        files = [path] if path.is_file() else path.rglob("*.py") if path.is_dir() else []
        for f in files:
            try:
                out[str(f)] = f.stat().st_mtime
            except OSError:
                continue
    return out


def changed_files(before: dict[str, float], now: dict[str, float]) -> list[str]:
    return sorted(p for p, m in now.items() if before.get(p) != m)


def compile_errors(paths: list[str]) -> list[str]:
    """Files that would fail to load (syntax), so a broken copy never restarts the bot."""
    bad: list[str] = []
    for p in paths:
        try:
            compile(Path(p).read_bytes(), p, "exec")
        except (OSError, SyntaxError, ValueError) as e:
            bad.append(f"{Path(p).name}: {e}")
    return bad


async def _open_trades() -> int:
    async with connect() as db:
        async with db.execute(
            "SELECT COUNT(*) AS n FROM paper_positions WHERE state = 'open'"
        ) as cur:
            row = await cur.fetchone()
    return int(row["n"]) if row else 0


def _hand_off() -> None:
    """Start the launcher's restart step, detached, then end this process."""
    launcher = REPOSITORY_ROOT / "tools" / "windows_paper_startup.py"
    log_path = Path(os.environ.get("DATA_DIR", ".")).parent / "logs" / "dashboard.log"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    with open(log_path, "a", encoding="utf-8") as out:
        subprocess.Popen(
            [sys.executable, str(launcher), "restart", "--after-pid", str(os.getpid())],
            cwd=REPOSITORY_ROOT, env=os.environ.copy(), stdin=subprocess.DEVNULL,
            stdout=out, stderr=subprocess.STDOUT, creationflags=flags,
        )
    log.warning("self_restart.exiting", pid=os.getpid())
    os._exit(0)  # like End task: SQLite keeps every committed write


async def run_forever(
    stop_event: asyncio.Event, mode: Callable[[], Awaitable[str]]
) -> None:
    if not enabled():
        return
    base = snapshot()
    reported: set[tuple[str, ...]] = set()
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
        try:
            now = await asyncio.to_thread(snapshot)
            changed = changed_files(base, now)
            if not changed:
                continue
            if time.time() - max(now.get(p, 0.0) for p in changed) < SETTLE_SECONDS:
                continue  # still copying
            bad = await asyncio.to_thread(compile_errors, changed)
            if bad:
                key = tuple(bad)
                if key not in reported:
                    reported.add(key)
                    await notify("self_restart_blocked",
                                 "An update didn't load because a file has an error, so the "
                                 "app kept running the old version: " + "; ".join(bad))
                continue
            if await mode() != "paper":
                continue  # never restart real-money mode
            if await _open_trades():
                continue  # wait until the open trade settles
            await notify("self_restart", "Restarting to load an update (no trade open). "
                         "Back in about 15 seconds.",
                         {"files": [Path(p).name for p in changed][:20]})
            _hand_off()
        except Exception as e:  # noqa: BLE001 — never take the dashboard down
            log.warning("self_restart.check_failed", error=str(e))
