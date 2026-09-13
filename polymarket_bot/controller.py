"""Start/stop controller for the BTC 5-minute trader (paper default, live opt-in)."""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import config as _config
from polymarket_exec.execution.live import LiveBootRefused, assert_live_boot_allowed
from polymarket_bot import paper as _paper
from polymarket_bot.paper import (
    count_open_positions,
    force_close_open_positions,
    run_paper_loop,
)
from config import BOT_MODE, PAPER_MAX_TRADE_USD, PAPER_MIN_TRADE_USD
from db import get_config, notify, set_config
from logging_setup import get_logger

log = get_logger("controller")

PAPER_ONLY_DETAIL = (
    "BTC 5-minute paper mode is ready. No live orders are placed in paper mode."
)
LIVE_MODE_DETAIL = (
    "BTC 5-minute LIVE mode is configured — Start will place REAL orders on the "
    "Polymarket CLOB, risk-gated and journaled to live_orders."
)

_runner_thread: threading.Thread | None = None
_stop_event: threading.Event | None = None
_thread_lock = threading.Lock()

# --- Loop watchdog (#147) --------------------------------------------------
# The 2026-07-05 incident: the paper loop wedged on an unbounded await —
# process alive, dashboard serving, zero ticks for ~14h, and Start could not
# recover it (the alive-thread short-circuit). The watchdog stamps out the
# whole wedge CLASS: a stalled heartbeat while the bot should be running is
# abandoned and respawned in paper mode; live mode only notifies — the
# watchdog must never auto-restart a real-money path.
WATCHDOG_STALL_SECONDS = 180.0
WATCHDOG_POLL_SECONDS = 20.0
_watchdog_thread: threading.Thread | None = None
_desired_running = False
_mode_cache = "paper"
_live_stall_notified = False

# --- Silent-stop detector (#138) -------------------------------------------
# Complements the #147 watchdog. The watchdog catches a WEDGED loop (thread
# alive, heartbeat stale) and respawns/notifies. It cannot catch a loop or
# whole process that DIES: a dead in-process watchdog notifies no one, and the
# persisted state is left reading "running" with no stop event ever written
# (the exact 06-24 incident in this issue). get_status() already self-heals
# that stale "running" row to "stopped" — but did so SILENTLY. This flag makes
# it emit exactly one notification per silent death so the operator learns the
# bot went down instead of staring at a frozen dashboard.
_silent_stop_notified = False


def watchdog_verdict(
    desired_running: bool,
    mode: str,
    heartbeat_age: float,
    threshold: float = WATCHDOG_STALL_SECONDS,
) -> str:
    """'restart' | 'notify' | 'ok' for one watchdog poll (pure decision)."""
    if not desired_running or heartbeat_age <= threshold:
        return "ok"
    return "notify" if mode == "live" else "restart"


def is_silent_stop(prior_state: str, runner_alive: bool) -> bool:
    """The silent-death signature (#138): the persisted state says the loop is
    running, yet no runner thread is alive in THIS process.

    True means the loop (or a prior process hosting it) died without an
    operator stop, so no 'stopped' event was ever journaled. Distinct from the
    #147 watchdog's wedge case, where the thread is still alive but not
    heartbeating.
    """
    return prior_state == "running" and not runner_alive


@dataclass
class BtcBotStatus:
    state: str
    mode: str
    updated_at: str | None
    detail: str


def _default_detail() -> str:
    if _config.BOT_MODE == "live":
        cap = _config.TRADE_BANKROLL_CAP_USD
        cap_str = f"${cap:.2f}" if cap is not None else "disabled"
        return (
            f"{LIVE_MODE_DETAIL}\n\n"
            f"Per-trade cap ${_config.TRADE_MAX_USD:.2f}, daily loss halt "
            f"${_config.TRADE_DAILY_LOSS_HALT_USD:.2f}, bankroll cap "
            f"{cap_str}. "
            f"Kill switch: touch {_config.KILL_SWITCH_PATH}."
        )
    return (
        f"{PAPER_ONLY_DETAIL}\n\n"
        f"Paper sizing range: ${PAPER_MIN_TRADE_USD:.0f}-"
        f"${PAPER_MAX_TRADE_USD:.0f} by confidence."
    )


def _is_runner_alive() -> bool:
    return _runner_thread is not None and _runner_thread.is_alive()


async def get_status() -> BtcBotStatus:
    """Return current BTC controller status."""
    global _silent_stop_notified
    state = await get_config("polymarket_bot.state", "stopped")
    mode = await get_config("polymarket_bot.mode", BOT_MODE)
    updated_at = await get_config("polymarket_bot.updated_at")
    detail = await get_config("polymarket_bot.detail", _default_detail())

    # State derives from the actual runner thread, not the stored row alone
    # (issue #23): a display that can read STOPPED while the loop places
    # orders makes the operator's kill decision unreliable — and vice versa.
    runner_alive = _is_runner_alive()
    if is_silent_stop(state, runner_alive):
        # #138: self-heal the stale row AND alert once — a silent death must
        # not look identical to an idle dashboard.
        if not _silent_stop_notified:
            _silent_stop_notified = True
            log.error("btc.silent_stop_detected", last_heartbeat=updated_at)
            await notify(
                "silent_stop",
                "Detected silent bot stop: the loop is not running but the "
                f"saved state was 'running' (last heartbeat {updated_at or 'unknown'}). "
                "No operator stop was recorded — the loop or process died "
                "unexpectedly. Press Start to resume (#138).",
                {"last_heartbeat": updated_at, "mode": mode},
            )
        state = "stopped"
        detail = "BTC bot loop is not running in this process. Press Start to restart."
        await set_config("polymarket_bot.state", state)
        await set_config("polymarket_bot.detail", detail)
    elif state != "running" and runner_alive:
        state = "running"
        await set_config("polymarket_bot.state", state)
    if runner_alive:
        # A confirmed-healthy loop re-arms the detector for the next death.
        _silent_stop_notified = False

    return BtcBotStatus(
        state=state or "stopped",
        mode=mode or BOT_MODE,
        updated_at=updated_at,
        detail=detail or _default_detail(),
    )


async def current_mode() -> str:
    """The active execution mode: runtime selector overrides the env default."""
    return await get_config("polymarket_bot.requested_mode", _config.BOT_MODE) or "paper"


async def set_mode(mode: str) -> BtcBotStatus:
    """Switch execution mode from the dashboard.

    The bot is stopped (live → flatten, paper → force-close) and the new mode
    is persisted. The runner is NOT auto-started — the operator must press
    Start. This keeps mode-switching free of surprise live-order side effects.

    The switch itself is never blocked: LIVE is always selectable. The live
    boot gate guards real orders at Start (``request_start``) and again at
    executor build (``build_live_executor``) — an unarmed LIVE selection
    simply refuses to start, with the reason in the status detail.
    """
    if mode not in ("paper", "live"):
        raise ValueError(f"unknown mode {mode!r}")
    await request_stop()
    await set_config("polymarket_bot.requested_mode", mode)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    detail = (
        f"Mode set to {mode.upper()}. Bot is stopped — press Start to begin."
    )
    if mode == "live":
        try:
            assert_live_boot_allowed()
        except LiveBootRefused as e:
            detail = (
                f"Mode set to LIVE — not armed: {e} "
                "Start will refuse until this is fixed."
            )
    await set_config("polymarket_bot.mode", mode)
    await set_config("polymarket_bot.updated_at", now)
    await set_config("polymarket_bot.detail", detail)
    return await get_status()


async def request_start() -> BtcBotStatus:
    """Start the trading runner (paper by default, live only when fully gated)."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    mode = await current_mode()
    if mode == "live":
        try:
            # Boot gate is checked HERE, before any thread starts. Refusal
            # means nothing runs — live never silently falls back to paper.
            assert_live_boot_allowed()
        except LiveBootRefused as e:
            detail = str(e)
            await set_config("polymarket_bot.state", "stopped")
            await set_config("polymarket_bot.mode", "live")
            await set_config("polymarket_bot.updated_at", now)
            await set_config("polymarket_bot.detail", detail)
            log.error("btc.live_start_refused", error=detail)
            return await get_status()
    global _desired_running, _mode_cache, _silent_stop_notified
    _desired_running = True
    _mode_cache = mode
    _silent_stop_notified = False  # #138: re-arm on every legitimate start
    _paper._beat()  # startup grace: the watchdog measures from Start
    _ensure_runner_started()
    _ensure_watchdog_started()
    await set_config("polymarket_bot.state", "running")
    await set_config("polymarket_bot.mode", mode)
    await set_config("polymarket_bot.updated_at", now)
    if mode == "live":
        detail = (
            "BTC LIVE loop starting — orders are REAL. It will discover the "
            "current BTC 5m market and place risk-gated CLOB orders."
        )
    else:
        detail = (
            "BTC paper loop starting. It will discover the current BTC 5m "
            "market and log simulated trades only."
        )
    await set_config("polymarket_bot.detail", detail)
    return await get_status()


async def request_stop() -> BtcBotStatus:
    """Stop the runner and disable new entries (live orders are flattened).

    Ordering matters: the stop event is set FIRST, then we WAIT for the
    runner thread to exit. The runner's own shutdown sequence (on its own
    event loop, the only thread that ever drives the LiveExecutor) cancels
    resting orders and flattens live positions before dropping the executor,
    so no entry can fire after the flatten and no live position can ever be
    paper-closed by this controller. In live mode, any ledger row that is
    still open afterwards means the live exit FAILED — it is left open for
    the operator instead of being closed with fictional paper prices.
    """
    now = datetime.now(UTC).isoformat(timespec="seconds")
    mode = _config.BOT_MODE
    global _desired_running
    _desired_running = False  # an operator stop is never a stall (#147)
    if _stop_event is not None:
        _stop_event.set()
    runner = _runner_thread
    if runner is not None and runner.is_alive():
        await asyncio.to_thread(runner.join, 90.0)

    if mode == "live":
        remaining = await count_open_positions()
        if runner is not None and runner.is_alive():
            detail = (
                "BTC live loop stop requested but the runner has not finished its "
                "shutdown flatten yet. Do NOT restart until it exits; check logs "
                "and live_orders."
            )
        elif remaining:
            detail = (
                f"BTC live loop stopped, but {remaining} live position(s) could NOT "
                "be flattened and remain OPEN in the ledger. Flatten manually on "
                "Polymarket and check the live_orders journal."
            )
        else:
            detail = (
                "BTC live loop stopped. Resting orders cancelled and open live "
                "positions flattened."
            )
    else:
        closed_count, close_error = await _safe_force_close()
        detail = (
            "BTC paper loop stop requested. New entries are disabled. "
            f"Force-closed {closed_count} open paper position(s)."
        )
        if close_error:
            detail = f"{detail} Force-close check failed: {close_error}"
    await set_config("polymarket_bot.state", "stopped")
    await set_config("polymarket_bot.mode", mode)
    await set_config("polymarket_bot.updated_at", now)
    await set_config("polymarket_bot.detail", detail)
    return await get_status()


def _ensure_runner_started(force: bool = False) -> None:
    """Spawn the runner thread; ``force`` abandons a wedged-but-alive one.

    The abandoned thread's stop_event is set first, so if it ever un-wedges
    it exits immediately — and the loop's generation guard (#147) stops it
    from clearing the successor's globals on the way out.
    """
    global _runner_thread, _stop_event
    with _thread_lock:
        if _runner_thread is not None and _runner_thread.is_alive():
            if not force:
                return
            if _stop_event is not None:
                _stop_event.set()
            log.warning(
                "watchdog.abandoning_wedged_runner",
                thread=_runner_thread.name,
            )
        _stop_event = threading.Event()
        _runner_thread = threading.Thread(
            target=_run_loop_in_thread,
            args=(_stop_event,),
            name="btc-paper-runner",
            daemon=True,
        )
        _runner_thread.start()


def _run_loop_in_thread(stop_event: threading.Event) -> None:
    asyncio.run(run_paper_loop(stop_event))


def _ensure_watchdog_started() -> None:
    global _watchdog_thread
    with _thread_lock:
        if _watchdog_thread is not None and _watchdog_thread.is_alive():
            return
        _watchdog_thread = threading.Thread(
            target=_watchdog_loop, name="btc-loop-watchdog", daemon=True
        )
        _watchdog_thread.start()


def _watchdog_loop() -> None:
    global _live_stall_notified
    while True:
        time.sleep(WATCHDOG_POLL_SECONDS)
        try:
            verdict = watchdog_verdict(
                _desired_running, _mode_cache, _paper.heartbeat_age_seconds()
            )
            if verdict == "ok":
                _live_stall_notified = False
                continue
            age = _paper.heartbeat_age_seconds()
            if verdict == "restart":
                log.warning("watchdog.loop_stalled_restarting", age_seconds=age)
                asyncio.run(
                    notify(
                        "loop_watchdog_restart",
                        f"Loop watchdog: no heartbeat for {age:.0f}s while the "
                        "paper bot should be running — abandoned the wedged "
                        "loop and respawned it (#147).",
                        {"age_seconds": age},
                    )
                )
                _paper._beat()  # grace period for the fresh loop's startup
                _ensure_runner_started(force=True)
            elif not _live_stall_notified:
                _live_stall_notified = True
                log.error("watchdog.live_loop_stalled", age_seconds=age)
                asyncio.run(
                    notify(
                        "loop_watchdog_stall_live",
                        f"Loop watchdog: no heartbeat for {age:.0f}s in LIVE "
                        "mode. NOT auto-restarting a real-money path — check "
                        "the process and restart manually (#147).",
                        {"age_seconds": age},
                    )
                )
        except Exception as e:  # noqa: BLE001 — the watchdog must never die
            log.warning("watchdog.poll_failed", error=f"{type(e).__name__}: {e}")


async def _safe_force_close() -> tuple[int, str | None]:
    try:
        return await force_close_open_positions("STOP_REQUEST"), None
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
        log.warning("btc.stop_force_close_failed", error=error)
        return 0, error
