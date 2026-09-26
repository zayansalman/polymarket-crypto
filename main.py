"""Entrypoint: boots the FastAPI operator dashboard (uvicorn)."""
from __future__ import annotations

import asyncio

from config import DASHBOARD_SERVER_PORT, DB_PATH
from db import init_db, notify
from logging_setup import get_logger, setup_logging

log = get_logger("main")


async def startup_tasks() -> None:
    await init_db()
    await notify(
        "system_start",
        "Polymarket crypto EMS started",
        {
            "db_path": str(DB_PATH),
            "version": "0.2.0",
            "dashboard": "fastapi",
        },
    )


def _acquire_singleton_lock() -> object:
    """Refuse to start if another instance is already running (#36).

    Multiple concurrent processes writing the shared SQLite journal each ran
    their own loop with independent live-executor state — the root cause of
    entries silently taking the paper path in 'live' mode. An advisory lock
    on data/bot.lock makes a second instance fail fast and loud.
    """
    import fcntl

    lock_path = DB_PATH.parent / "bot.lock"
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(
            f"REFUSED: another bot instance already holds {lock_path}. "
            "Only one process may run the loop (it would otherwise trade the "
            "same account from two uncoordinated loops). Stop the other first."
        )
        raise SystemExit(1)
    fh.write(str(__import__("os").getpid()))
    fh.flush()
    return fh  # keep the handle alive for the process lifetime


# Seconds uvicorn waits for open requests on shutdown before cancelling them.
# An open dashboard tab keeps /api/stream open forever, and without a bound
# SIGTERM never got past "Waiting for connections to close" — the process
# stayed alive holding data/bot.lock, so a restart was refused.
SHUTDOWN_GRACE_S = 3


def dashboard_server_options() -> dict[str, object]:
    """uvicorn settings for the FastAPI dashboard."""
    return {
        "host": "127.0.0.1",
        "port": DASHBOARD_SERVER_PORT,
        "log_level": "info",
        "timeout_graceful_shutdown": SHUTDOWN_GRACE_S,
    }


def main() -> None:
    setup_logging("INFO")
    _LOCK = _acquire_singleton_lock()  # noqa: F841 — held for process lifetime
    log.info(
        "app.boot",
        db_path=str(DB_PATH),
        version="0.2.0",
    )
    asyncio.run(startup_tasks())

    import uvicorn
    log.info("dashboard.start_fastapi", port=DASHBOARD_SERVER_PORT)
    uvicorn.run(
        "polymarket_exec.ops.dashboard.app:app", **dashboard_server_options()
    )


if __name__ == "__main__":
    main()
