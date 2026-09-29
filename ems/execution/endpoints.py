"""Which venues may take new orders this pass: one endpoint per mode, each with its gate leg.

A strategy decides once and sends the same order to every active endpoint (paper and live
then hold the same position; only the gate leg and the fills can differ). It never branches on
the mode: it asks :func:`endpoints` each pass and calls the same methods on each active one.

- paper: always on, unless the kill switch file exists.
- live: on only while LIVE is armed (``live_control.live_status``: selected, clicked in this
  process, a wallet that passes) and the live venue opened. Without a live holder (a runner
  built for paper only) there is no live endpoint, and it says so.

An endpoint that is off still hands back its venue when there is one, so fills and settlement
of its orders go on; the strategy cancels whatever still rests there. The live venue is opened
for that even when LIVE is off, but only if the strategy says it has live orders to follow.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ems.execution import controls as _controls
from ems.execution import live_control as _live
from ems.execution.gate import LIVE, PAPER, RiskGate
from ems.execution.resting import RestingVenue

ON = "on"
KILL = "kill_switch"
NOT_BUILT = "not_built"
BOOT_FAILED = "boot_failed"


@dataclass(frozen=True)
class Endpoint:
    """One mode this pass. ``venue`` follows its orders (None when there is none);
    ``active`` says whether it may take new ones."""

    mode: str
    state: str
    message: str
    venue: RestingVenue | None
    gate: RiskGate
    active: bool = False


async def endpoints(
    *,
    paper: RestingVenue,
    gates: dict[str, RiskGate],
    live: _live.LiveVenueHolder | None = None,
    live_orders_open: bool = False,
    kill_switch_path: Path | str | None = None,
) -> dict[str, Endpoint]:
    """Both endpoints for this pass, paper first."""
    killed = _controls.kill_switch_active(kill_switch_path)
    path = _controls.kill_switch_path(kill_switch_path)
    kill_message = (f"The kill switch file is present ({path}). No new orders; resting ones "
                    "are cancelled; fills and settlement go on.")
    if killed:
        paper_ep = Endpoint(PAPER, KILL, kill_message, paper, gates[PAPER])
    else:
        paper_ep = Endpoint(PAPER, ON, "Paper: orders rest in this app's ledger and fill only "
                            "when the real trade tape reaches them.", paper, gates[PAPER],
                            active=True)
    return {PAPER: paper_ep, LIVE: await _live_endpoint(gates[LIVE], live, live_orders_open,
                                                         killed, kill_message)}


async def _live_endpoint(gate: RiskGate, holder: _live.LiveVenueHolder | None, orders_open: bool,
                         killed: bool, kill_message: str) -> Endpoint:
    if holder is None:
        return Endpoint(LIVE, NOT_BUILT, "Live: this strategy's loop has no live venue, so "
                        "nothing is sent to the exchange.", None, gate)
    status = await _live.live_status()
    venue = holder.venue
    if (status.armed and not killed) or orders_open:
        venue = await holder.open()
    if killed:
        return Endpoint(LIVE, KILL, kill_message, venue, gate)
    if not status.armed:
        return Endpoint(LIVE, status.state, status.message, venue, gate)
    if venue is None:
        return Endpoint(LIVE, BOOT_FAILED, f"LIVE is armed but the exchange sign-in failed: "
                        f"{holder.error}. Tried again in a minute.", None, gate)
    return Endpoint(LIVE, ON, status.message, venue, gate, active=True)


async def still_active(point: Endpoint, *, kill_switch_path: Path | str | None = None
                       ) -> str | None:
    """Re-check an endpoint just before an order is sent: None if it may still take it, else
    why not. A decision can take tens of seconds of reads, and the operator may have clicked
    PAPER, or touched the kill switch, in the meantime."""
    if not point.active or point.venue is None:
        return f"{point.mode} is off"
    if _controls.kill_switch_active(kill_switch_path):
        return "kill_switch: the kill switch file appeared"
    if point.mode == LIVE:
        status = await _live.live_status()
        if not status.armed:
            return f"live_disarmed: {status.message}"
    return None
