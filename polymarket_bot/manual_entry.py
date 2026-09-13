"""Thread-safe one-slot handoff of a Market-mode Buy click from the dashboard to the runner."""
from __future__ import annotations

import threading
import time
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass, field

# A click older than this when the runner picks it up is no longer the
# operator's intent at that price — it is refused, never executed late.
INTENT_TTL_SECONDS = 15.0


@dataclass(frozen=True)
class EntryOutcome:
    """What happened to one entry attempt, in words the dashboard can show.

    status: "filled" (paper fill, or live order fully matched at placement) |
    "placed" (live order accepted but (partly) resting) | "blocked" (refused by
    a check/gate) | "pending" (the dashboard stopped waiting but the runner
    already started it) | "error" (unexpected exception).
    """

    status: str
    detail: str
    mode: str | None = None
    side: str | None = None
    price: float | None = None
    shares: float | None = None
    notional_usd: float | None = None
    position_id: int | None = None


@dataclass
class ManualEntryIntent:
    """One operator Buy click, waiting for the runner thread to act on it."""

    side: str  # "Up" | "Down"
    window_slug: str  # the window the operator saw when clicking
    seen_ask: float | None  # the ask the operator saw (slippage reference)
    mode: str  # "paper" | "live" — the runner must be in this mode
    requested_at: float = field(default_factory=time.monotonic)
    future: Future[EntryOutcome] = field(default_factory=Future)


# Set when a click is waiting so the runner's inter-tick sleep returns at once.
wake = threading.Event()

# The dashboard (uvicorn loop) submits, the runner thread takes — one slot, so
# a second click while one is in flight is refused instead of queued.
_lock = threading.Lock()
_slot: ManualEntryIntent | None = None


def submit(intent: ManualEntryIntent) -> bool:
    """Queue ``intent`` and wake the runner; False when a click is already waiting."""
    global _slot
    with _lock:
        if _slot is not None:
            return False
        _slot = intent
        wake.set()
    return True


def take() -> ManualEntryIntent | None:
    """Claim the waiting click (runner side); clears the slot and the wake flag."""
    global _slot
    with _lock:
        intent, _slot = _slot, None
        wake.clear()
    return intent


def withdraw(intent: ManualEntryIntent) -> bool:
    """Remove ``intent`` if it is still the waiting click (dashboard gave up)."""
    global _slot
    with _lock:
        if _slot is not intent:
            return False
        _slot = None
        wake.clear()
    return True


def has_pending() -> bool:
    """True while a click waits in the slot (not yet taken by the runner)."""
    with _lock:
        return _slot is not None


def resolve(intent: ManualEntryIntent, outcome: EntryOutcome) -> None:
    """Deliver ``outcome`` to the waiting dashboard; a no-op if it already resolved."""
    if intent.future.done():
        return
    try:
        intent.future.set_result(outcome)
    except InvalidStateError:
        pass  # cancelled or resolved concurrently — the dashboard already moved on


def cancel_pending(reason: str, *, status: str = "blocked") -> None:
    """Refuse the waiting click (loop stop/start, failed tick) with ``reason``."""
    intent = take()
    if intent is None:
        return
    resolve(intent, EntryOutcome(status, reason, mode=intent.mode, side=intent.side))


def reset_for_tests() -> None:
    """Drop any waiting click and the wake flag (test isolation)."""
    global _slot
    with _lock:
        _slot = None
        wake.clear()
