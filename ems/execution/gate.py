"""The pre-trade risk gate every strategy's orders pass: one leg per mode, paper and live.

Before a venue sees an order, :meth:`RiskGate.check` looks at it against the leg's limits, in
this order, and names the first one it breaks:

1. ``kill_switch``: the kill switch file exists (or cannot be checked).
2. ``bad_limit``: a limit is not a number in its knob's range (fails closed).
3. ``loss_halt``: the day's settled P&L on this leg is at or below minus the loss halt.
4. ``bad_notional``: the order is worth nothing, or not a number.
5. ``max_trade``: the order's notional (price x shares) is above the per-trade cap.
6. ``daily_cap``: today's net notional placed on this leg, plus this order, is above the cap.

The limits are runtime knobs (``ems/runtime_knobs.py``, group "Risk"), read on every check, so
a change on the SETTINGS card applies to the next order. A loss halt or daily cap of 0 is off.
The loss halt is a fixed floor for the UTC day (minus the halt), not a trailing one.

Hold the leg's ``lock`` from ``check`` through placing the order to ``commit``: strategies run
as separate tasks, and two orders checked at once would otherwise both fit under a cap that
has room for one.

What the gate counts lives in ``risk_events``, one row per event: the notional of every order
placed (``commit``), the unfilled part given back once the order can fill no more
(``credit``), and each settled order's P&L (``realize``). Totals are sums over the UTC day, so a
restart cannot reset a loss halt, and each order's credit and P&L are counted once however
often they are reported: report them once they are final (a credit once the order can fill no
more, P&L once it is settled). A later report with a different amount is ignored and logged,
and a credit is never more than the order's commit. Paper and live never share a total.
"""

from __future__ import annotations

import asyncio
import math
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import structlog

from ems import db as _db  # type: ignore[import-untyped]
from ems import runtime_knobs as _knobs
from ems.execution import controls as _controls

log = structlog.get_logger(__name__)

PAPER = "paper"
LIVE = "live"
MODES = (PAPER, LIVE)

KILL = "kill_switch"
BAD_LIMIT = "bad_limit"
LOSS_HALT = "loss_halt"
BAD_NOTIONAL = "bad_notional"
MAX_TRADE = "max_trade"
DAILY_CAP = "daily_cap"
REASONS = (KILL, BAD_LIMIT, LOSS_HALT, BAD_NOTIONAL, MAX_TRADE, DAILY_CAP)
_TOLERANCE = 1e-9

DAY_S = 86_400

# The knobs behind each leg's limits.
LIMIT_KNOBS: Mapping[str, Mapping[str, str]] = MappingProxyType({
    PAPER: MappingProxyType({
        "max_trade_usd": "paper_max_trade_usd",
        "daily_notional_cap_usd": "paper_daily_notional_cap_usd",
        "daily_loss_halt_usd": "paper_daily_loss_halt_usd",
    }),
    LIVE: MappingProxyType({
        "max_trade_usd": "live_max_trade_usd",
        "daily_notional_cap_usd": "live_daily_notional_cap_usd",
        "daily_loss_halt_usd": "live_daily_loss_halt_usd",
    }),
})


@dataclass(frozen=True)
class Limits:
    max_trade_usd: float
    daily_notional_cap_usd: float  # 0: off
    daily_loss_halt_usd: float  # 0: off


@dataclass(frozen=True)
class DayTotals:
    """This leg's UTC day so far: net notional placed (commits less credits) and settled P&L."""

    notional_usd: float
    pnl_usd: float


@dataclass(frozen=True)
class Verdict:
    """What the gate said about one order. ``reason`` is None when it may go."""

    mode: str
    allowed: bool
    reason: str | None
    message: str
    limits: Limits
    today: DayTotals


def _check_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    return mode


def day_start(now: float) -> int:
    """The UTC midnight at or before ``now``."""
    return int(math.floor(now / DAY_S) * DAY_S)


async def read_limits(mode: str) -> Limits:
    names = LIMIT_KNOBS[_check_mode(mode)]
    return Limits(
        max_trade_usd=float(await _knobs.get(names["max_trade_usd"])),
        daily_notional_cap_usd=float(await _knobs.get(names["daily_notional_cap_usd"])),
        daily_loss_halt_usd=float(await _knobs.get(names["daily_loss_halt_usd"])),
    )


def limit_problems(mode: str, limits: Limits) -> list[str]:
    """Every limit that is not a number inside its knob's range: the gate then blocks."""
    problems = []
    for field_name, knob_name in LIMIT_KNOBS[_check_mode(mode)].items():
        value = getattr(limits, field_name)
        knob = _knobs.KNOBS[knob_name]
        low = knob.min_value if knob.min_value is not None else -math.inf
        high = knob.max_value if knob.max_value is not None else math.inf
        if not (math.isfinite(value) and low <= value <= high):
            problems.append(f"{knob.label} is {value!r}, outside {low:g} to {high:g}")
    return problems


_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
    weakref.WeakKeyDictionary())


class RiskGate:
    """One leg of the gate. Holds no counts of its own: every total is read from the table."""

    def __init__(self, mode: str, *, kill_switch_path: Path | str | None = None) -> None:
        self.mode = _check_mode(mode)
        self._kill_switch_path = kill_switch_path

    @property
    def lock(self) -> asyncio.Lock:
        """This leg's lock, shared by every gate instance on the leg in this event loop."""
        loop = asyncio.get_running_loop()
        legs = _LOCKS.get(loop)
        if legs is None:
            legs = _LOCKS[loop] = {}
        if self.mode not in legs:
            legs[self.mode] = asyncio.Lock()
        return legs[self.mode]

    async def day_totals(self, now: float) -> DayTotals:
        start = day_start(now)
        async with _db.connect() as conn:
            async with conn.execute(
                """
                SELECT
                  COALESCE((
                    SELECT SUM(c.amount_usd - COALESCE(r.amount_usd, 0))
                    FROM risk_events c
                    LEFT JOIN risk_events r
                      ON r.mode = c.mode AND r.order_ref = c.order_ref AND r.kind = 'credit'
                    WHERE c.mode = ? AND c.kind = 'commit' AND c.ts >= ? AND c.ts < ?
                  ), 0) AS notional,
                  COALESCE((
                    SELECT SUM(amount_usd) FROM risk_events
                    WHERE mode = ? AND kind = 'realize' AND ts >= ? AND ts < ?
                  ), 0) AS pnl
                """,
                (self.mode, start, start + DAY_S, self.mode, start, start + DAY_S),
            ) as cur:
                row = await cur.fetchone()
        return DayTotals(notional_usd=float(row["notional"]), pnl_usd=float(row["pnl"]))

    async def check(self, notional_usd: float, *, now: float) -> Verdict:
        """Whether an order worth ``notional_usd`` may be placed on this leg now."""
        limits = await read_limits(self.mode)
        today = await self.day_totals(now)

        def verdict(reason: str | None, message: str) -> Verdict:
            return Verdict(self.mode, reason is None, reason, message, limits, today)

        if _controls.kill_switch_active(self._kill_switch_path):
            return verdict(KILL, "The kill switch is on, so no new orders are placed.")
        problems = limit_problems(self.mode, limits)
        if problems:
            return verdict(BAD_LIMIT, "A risk limit is not usable, so nothing is placed: "
                           + "; ".join(problems) + ".")
        halt = limits.daily_loss_halt_usd
        if halt > 0 and today.pnl_usd <= -halt + _TOLERANCE:
            return verdict(LOSS_HALT, f"Daily loss halt: {self.mode} P&L today is "
                           f"${today.pnl_usd:+,.2f}, at or past the ${halt:,.2f} halt.")
        if not (math.isfinite(notional_usd) and notional_usd > 0):
            return verdict(BAD_NOTIONAL, f"The order is worth {notional_usd!r} USD.")
        if notional_usd > limits.max_trade_usd + _TOLERANCE:
            return verdict(MAX_TRADE, f"Per-trade cap: ${notional_usd:,.2f} is above the "
                           f"{self.mode} cap of ${limits.max_trade_usd:,.2f}.")
        cap = limits.daily_notional_cap_usd
        if cap > 0 and today.notional_usd + notional_usd > cap + _TOLERANCE:
            return verdict(DAILY_CAP, f"Daily cap: ${today.notional_usd:,.2f} placed today plus "
                           f"${notional_usd:,.2f} is above the {self.mode} cap of ${cap:,.2f}.")
        return verdict(None, "Within every limit.")

    async def commit(self, *, strategy: str, order_ref: str, notional_usd: float,
                     now: float) -> None:
        """Count an order just placed against today's notional."""
        await self._record("commit", strategy, order_ref, notional_usd, now)

    async def credit(self, *, strategy: str, order_ref: str, unfilled_usd: float,
                     now: float) -> None:
        """Give back the unfilled part of an order that can fill no more. Counted once."""
        await self._record("credit", strategy, order_ref, max(0.0, unfilled_usd), now)

    async def realize(self, *, strategy: str, order_ref: str, pnl_usd: float,
                      now: float) -> None:
        """Count a settled order's P&L toward the loss halt. Counted once."""
        await self._record("realize", strategy, order_ref, pnl_usd, now)

    async def _record(self, kind: str, strategy: str, order_ref: str, amount: float,
                      now: float) -> None:
        if not math.isfinite(amount):
            raise ValueError(f"{kind} amount must be a number, got {amount!r}")
        async with _db.connect() as conn:
            if kind == "credit":
                async with conn.execute(
                    "SELECT amount_usd FROM risk_events WHERE mode = ? AND kind = 'commit' "
                    "AND order_ref = ?", (self.mode, str(order_ref)),
                ) as cur:
                    committed = await cur.fetchone()
                if committed is not None and amount > float(committed["amount_usd"]):
                    log.warning("risk_gate.credit_capped", mode=self.mode, order=order_ref,
                                credit=amount, committed=float(committed["amount_usd"]))
                    amount = float(committed["amount_usd"])
            cur = await conn.execute(
                """
                INSERT OR IGNORE INTO risk_events (ts, mode, strategy, kind, order_ref, amount_usd)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (int(math.floor(now)), self.mode, str(strategy), kind, str(order_ref),
                 float(amount)),
            )
            if not cur.rowcount:
                async with conn.execute(
                    "SELECT amount_usd FROM risk_events WHERE mode = ? AND kind = ? "
                    "AND order_ref = ?", (self.mode, kind, str(order_ref)),
                ) as held:
                    first = await held.fetchone()
                if first is not None and abs(float(first["amount_usd"]) - amount) > _TOLERANCE:
                    log.warning("risk_gate.second_report_ignored", mode=self.mode, kind=kind,
                                order=order_ref, kept=float(first["amount_usd"]), ignored=amount)
            await conn.commit()
