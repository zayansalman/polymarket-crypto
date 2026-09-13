"""Entrypoint for the BTC 5-minute paper trading system."""
from __future__ import annotations

import asyncio

from config import DASHBOARD_SERVER_PORT, DB_PATH
from db import init_db, notify
from logging_setup import get_logger, setup_logging

# New architecture entrypoint (v0.2+)
try:
    from polymarket_exec.ops.dashboard.app import app as dashboard_app
    HAS_NEW_DASHBOARD = True
except ImportError:
    HAS_NEW_DASHBOARD = False

# Legacy entrypoint (v0.1)
if not HAS_NEW_DASHBOARD:
    from config import DASHBOARD_SERVER_NAME, DASHBOARD_SERVER_PORT
    from dashboard import launch

log = get_logger("main")


async def startup_tasks() -> None:
    await init_db()
    await notify(
        "system_start",
        "BTC 5-minute paper trading system started",
        {
            "db_path": str(DB_PATH),
            "version": "0.2.0",
            "dashboard": "fastapi" if HAS_NEW_DASHBOARD else "gradio",
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


def main() -> None:
    setup_logging("INFO")
    _LOCK = _acquire_singleton_lock()  # noqa: F841 — held for process lifetime
    log.info(
        "app.boot",
        db_path=str(DB_PATH),
        version="0.2.0",
        has_new_dashboard=HAS_NEW_DASHBOARD,
    )
    # Deferred from config.py import-time (logging wasn't ready then).
    import config as _config
    for note in getattr(_config, "CONFIG_DEPRECATIONS", []):
        log.warning("config.deprecation", message=note)
    asyncio.run(startup_tasks())

    if HAS_NEW_DASHBOARD:
        import uvicorn
        log.info("dashboard.start_fastapi", port=DASHBOARD_SERVER_PORT)
        uvicorn.run(
            "polymarket_exec.ops.dashboard.app:app",
            host="127.0.0.1",
            port=DASHBOARD_SERVER_PORT,
            log_level="info",
        )
    else:
        log.info("dashboard.start_gradio", server="127.0.0.1", port=DASHBOARD_SERVER_PORT)
        launch()


if __name__ == "__main__":
    main()
