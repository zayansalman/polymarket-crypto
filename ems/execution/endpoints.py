"""Which venues may take new orders this pass: one endpoint per mode, each with its gate leg.

A strategy decides once and sends the same order to every active endpoint (paper and live
then hold the same position; only the gate leg and the fills can differ). It never branches on
the mode: it asks :func:`endpoints` each pass and calls the same methods on each active one.

- paper: always on, unless the kill switch file exists.
- live: off until a live venue is built; the endpoint says so.

An endpoint that is off keeps its orders' bookkeeping running (fills, settlement); the
strategy cancels whatever still rests there.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from ems.execution import controls as _controls
from ems.execution.gate import LIVE, PAPER, RiskGate
from ems.execution.resting import RestingVenue

ON = "on"
KILL = "kill_switch"
NOT_BUILT = "not_built"

LiveVenueFactory = Callable[[], Awaitable[RestingVenue]]


@dataclass(frozen=True)
class Endpoint:
    """One mode this pass. ``venue`` is None when it may not take new orders."""

    mode: str
    state: str
    message: str
    venue: RestingVenue | None
    gate: RiskGate

    @property
    def active(self) -> bool:
        return self.venue is not None


async def endpoints(
    *,
    paper: RestingVenue,
    gates: dict[str, RiskGate],
    live: LiveVenueFactory | None = None,
    kill_switch_path: Path | str | None = None,
) -> dict[str, Endpoint]:
    """Both endpoints for this pass, paper first."""
    killed = _controls.kill_switch_active(kill_switch_path)
    path = _controls.kill_switch_path(kill_switch_path)
    kill_message = (f"The kill switch file is present ({path}). No new orders; resting ones "
                    "are cancelled; fills and settlement go on.")
    if killed:
        paper_ep = Endpoint(PAPER, KILL, kill_message, None, gates[PAPER])
        live_ep = Endpoint(LIVE, KILL, kill_message, None, gates[LIVE])
        return {PAPER: paper_ep, LIVE: live_ep}
    paper_ep = Endpoint(PAPER, ON, "Paper: orders rest in this app's ledger and fill only when "
                        "the real trade tape reaches them.", paper, gates[PAPER])
    if live is None:
        live_ep = Endpoint(LIVE, NOT_BUILT, "Live: no live venue is built, so nothing is sent "
                           "to the exchange.", None, gates[LIVE])
    else:
        live_ep = Endpoint(LIVE, ON, "Live: post-only orders on the exchange.", await live(),
                           gates[LIVE])
    return {PAPER: paper_ep, LIVE: live_ep}
