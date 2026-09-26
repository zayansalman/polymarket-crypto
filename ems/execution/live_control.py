"""Whether LIVE is armed, and the one live venue the process opens.

LIVE is armed only while all of these hold (AGENTS.md, "Live trading"):

1. LIVE is the selected mode (``controls.requested_mode``, the ``polymarket_bot.requested_mode``
   config row the dashboard's PAPER/LIVE control writes);
2. the operator clicked LIVE in the dashboard in this process (:func:`record_click`, called only
   by that route). It is held in memory and never saved: a LIVE saved by an earlier process, or
   ``BOT_MODE=live`` in the environment, is never consent;
3. the wallet config passes (``clob.wallet_problems``).

A strategy adds its own switch on top (Kelly horse-race's runner checks it every pass).

The live venue itself is opened (signed in) only when it is needed: when LIVE is armed, or
when a strategy still has live orders to follow after LIVE was turned off or the app restarted.
A failed sign-in is tried again after ``RETRY_S``.
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from ems import db as _db  # type: ignore[import-untyped]
from ems.execution import clob as _clob
from ems.execution import controls as _controls
from ems.execution.resting import RestingVenue

log = structlog.get_logger(__name__)

PAPER = "paper"
LIVE = "live"
RETRY_S = 60.0

ARMED = "on"
NOT_SELECTED = "not_selected"
NOT_CLICKED = "not_clicked"
NO_WALLET = "no_wallet"
MODE_UNKNOWN = "mode_unknown"

_clicked_live = False  # this process only; set by the dashboard route, never saved


def record_click(mode: str) -> None:
    """The operator clicked ``mode`` in the dashboard (the route calls this, nothing else)."""
    global _clicked_live
    _clicked_live = mode == LIVE


def clicked_live() -> bool:
    return _clicked_live


async def select_mode(mode: str, *, clicked: bool) -> str:
    """Save the PAPER/LIVE selection. ``clicked``: it came from the operator's click in the
    dashboard (the only way LIVE is consented to)."""
    mode = str(mode or "").strip().lower()
    if mode not in (PAPER, LIVE):
        raise ValueError("mode must be paper or live")
    await _db.set_config(_controls.MODE_KEY, mode)
    record_click(mode if clicked else PAPER)
    return mode


@dataclass(frozen=True)
class LiveStatus:
    state: str
    message: str
    problems: tuple[str, ...] = ()

    @property
    def armed(self) -> bool:
        return self.state == ARMED


async def live_status() -> LiveStatus:
    """Whether LIVE is armed now, and in plain words why not."""
    try:
        mode = await _controls.requested_mode()
    except Exception as exc:  # noqa: BLE001 - fail closed
        return LiveStatus(MODE_UNKNOWN, f"The PAPER/LIVE selection could not be read "
                          f"({type(exc).__name__}), so nothing is sent live.")
    if mode != LIVE:
        return LiveStatus(NOT_SELECTED, "PAPER is selected: nothing is sent to the exchange.")
    if not _clicked_live:
        return LiveStatus(NOT_CLICKED, "LIVE is selected but was not clicked in this dashboard "
                          "session (a saved LIVE or BOT_MODE=live is never consent): click LIVE "
                          "to arm it.")
    problems = tuple(_clob.wallet_problems())
    if problems:
        return LiveStatus(NO_WALLET, "LIVE is not armed: " + "; ".join(problems) + ".",
                          problems)
    return LiveStatus(ARMED, "LIVE is armed: post-only orders go to the exchange.")


Opener = Callable[[], Awaitable[RestingVenue]]


@dataclass
class LiveVenueHolder:
    """Opens the live venue once and keeps it; after a failed sign-in, waits ``RETRY_S``."""

    kill_switch_path: Path | str | None = None
    clock: Callable[[], float] = time.time
    opener: Opener | None = None
    venue: RestingVenue | None = None
    error: str | None = None
    _retry_at: float = field(default=-math.inf)

    async def open(self) -> RestingVenue | None:
        """The live venue, opening it if needed. None (with ``error``) if it cannot open."""
        if self.venue is not None:
            return self.venue
        now = float(self.clock())
        if now < self._retry_at:
            return None
        try:
            opener = self.opener or (lambda: _clob.open_live_venue(
                clock=self.clock, kill_switch_path=self.kill_switch_path))
            self.venue = await opener()
            self.error = None
            log.info("live_venue.opened")
        except Exception as exc:  # noqa: BLE001 - shown on the card, tried again later
            self.error = str(exc) or type(exc).__name__
            self._retry_at = now + RETRY_S
            log.warning("live_venue.open_failed", error=self.error)
            return None
        return self.venue
