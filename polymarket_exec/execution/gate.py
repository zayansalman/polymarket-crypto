"""Venue-independent pre-trade risk gate (issue #64).

Both paper and live entries go through the SAME ``RiskGate`` so paper is a
faithful preview of live: what paper blocks, live would have blocked; what
paper opens, live would have opened (modulo the actual CLOB order placement,
which is the only live-only concern).

The gate owns the persisted daily counters (date, realized PnL, cumulative
BUY notional) in SQLite. They are venue-independent — both paper closes and
live closes feed them — so every gate that consults a counter (the daily
realized-loss halt, the optional bankroll cap) advances identically in both
modes.

State lives under the ``risk.*`` config keys (renamed from the legacy
``btc_risk.*`` namespace under issue #185; existing rows are migrated by
``db.init_db()``). Legacy ``btc_live.*`` keys written before issue #64 are
read once at boot and migrated in place; after the first persist they are no
longer referenced. Those three keys keep their original literal names — they
point at historical data, not branding, so they are not part of the #185
rename.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

from db import get_config, set_config  # type: ignore[import-untyped]
from polymarket_bot import runtime_knobs as _knobs

# ---------------------------------------------------------------------------
# Persisted-state keys
# ---------------------------------------------------------------------------

# Current keys (issue #64). Generic — fed by paper closes and live closes.
_RISK_DATE_KEY = "risk.date"
_RISK_NOTIONAL_KEY = "risk.daily_buy_notional"
# Split counters (issue #67; halt scope changed in #76). Live and paper
# realized PnL are tracked separately. The halt now fires on the MODE'S OWN
# leg — live halts on real-money PnL, paper on study PnL — so paper-study
# losses no longer halt live trading. ``daily_realized_pnl`` keeps the combined
# sum as a reporting/back-compat surface only.
_RISK_LIVE_PNL_KEY = "risk.live_realized_pnl"
_RISK_PAPER_PNL_KEY = "risk.paper_realized_pnl"
# Session high-water marks (issue #112). The loss halt trails the session PEAK
# realized PnL — floor = peak - daily_loss_halt_usd — so banked profit can't be
# bled back beyond the limit. Peaks ratchet up only and reset with the PnL
# counters at the UTC day boundary. Absent (pre-#112 state) → derived on load as
# max(0, leg_pnl), which keeps a never-profitable session identical to the old
# fixed -limit floor.
_RISK_LIVE_PEAK_KEY = "risk.live_peak_pnl"
_RISK_PAPER_PEAK_KEY = "risk.paper_peak_pnl"
# Pre-split combined key (issue #64). Read once on migration into the live
# bucket — by far the most common pre-split scenario was a live-only counter.
_RISK_COMBINED_PNL_KEY = "risk.daily_realized_pnl"

# Operator loss-halt bypass (#65, generalised #76). Originally paper-only; now
# an operator runtime knob honoured in BOTH modes — the old "live can never
# disable a hard money limit from the UI" invariant was removed at the
# operator's explicit request. Re-read every tick via ``refresh_overrides``.
# Renamed from ``btc_risk.paper_bypass_loss_halt`` under issue #185 — the
# rebrand's DB migration (``db.init_db()``) carries the persisted value
# forward, so this is not the silent reset the old comment here warned about.
_BYPASS_LOSS_HALT_KEY = "risk.paper_bypass_loss_halt"
# One-shot migration sentinel (#76): set once the stale paper-era bypass flag
# has been cleared so live starts halt-ON; presence of the sentinel guarantees
# a later deliberate bypass is never wiped.
_BYPASS_MIGRATED_KEY = "risk.bypass_migrated_v76"

# Operator runtime risk knobs (#50). Unlike the paper-only bypass above, these
# are tuning controls that apply in BOTH modes (the operator wants to resize
# the clip mid-session without a restart). Persisted in the config table and
# re-read every tick via ``refresh_runtime_limits``. Unset → fall back to the
# env/config default, so absence is fully backward-compatible.
_RUNTIME_MAX_TRADE_KEY = "runtime.max_trade_usd"
# Operator runtime trade size in SHARES (#89). When set it takes precedence over
# the dollar cap above: the bot sizes each clip to this many shares and the
# per-trade dollar cap derives from it (N shares cost ≤ ~$N since prices < 1).
_RUNTIME_TRADE_SHARES_KEY = "runtime.trade_shares"
# Trade size when the operator hasn't set one: the Polymarket venue minimum.
DEFAULT_TRADE_SHARES = 5.0

# Legacy keys, written by the live-only counter before issue #64. Read once at
# boot if the new keys are absent, then never touched again — the next persist
# writes the new keys exclusively. These 3 literal names are NOT part of the
# #185 rebrand rename: they must keep pointing at whatever a pre-#64 database
# actually wrote under the old ``btc_live.*`` prefix, so renaming the string
# here (rather than migrating the historical data) would just break the
# fallback they exist for.
_LEGACY_DATE_KEY = "btc_live.risk_date"
_LEGACY_PNL_KEY = "btc_live.daily_realized_pnl"
_LEGACY_NOTIONAL_KEY = "btc_live.daily_buy_notional"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateConfig:
    """Static gate configuration. Identical instance shared by paper and live."""

    max_trade_usd: float
    daily_loss_halt_usd: float
    bankroll_cap_usd: Optional[float]  # None / ≤0 → cap disabled
    max_entry_slippage: float
    kill_switch_path: Path


@dataclass(frozen=True)
class EntryRequest:
    """What the caller is asking permission to do.

    ``best_ask`` and ``side_price`` together drive the slippage guard. When
    either is ``None`` (e.g. the book is unavailable), the guard does not
    contribute a block reason — other gates still run.
    """

    notional_usd: float
    position_open: bool  # any open ledger / live position
    entry_order_resting: bool  # live: unfilled entry order resting in the book
    side_price: Optional[float]  # the price the signal was computed against
    best_ask: Optional[float]  # the live ask AT FILL TIME


# ---------------------------------------------------------------------------
# RiskGate
# ---------------------------------------------------------------------------


class RiskGate:
    """Pre-trade gate + persisted daily counters, shared by paper and live.

    All gate logic lives here so paper and live cannot diverge by accident.
    Counters are persisted to SQLite (``risk.*``) and rebuilt at boot so
    Stop/Start or a process restart never resets the daily-loss halt or
    silently grants a fresh bankroll when the cap is enabled.
    """

    def __init__(self, cfg: GateConfig, *, is_live: bool = False) -> None:
        self.cfg = cfg
        # Which leg drives THIS gate's loss halt (#76): the live gate halts on
        # real-money PnL, the paper gate on study PnL. Set True by the live
        # executor; the paper loop leaves it False.
        self.is_live = is_live
        self._date = self._today()
        # Split PnL counters (issue #67). Gate halts on the SUM; dashboard
        # shows each leg separately so the operator can tell real-money PnL
        # apart from paper study runs.
        self._live_pnl: float = 0.0
        self._paper_pnl: float = 0.0
        # Session high-water marks per leg (#112) — the trailing loss halt is
        # measured as drawdown from these. Ratchet up only; reset with the PnL
        # counters at the UTC day boundary.
        self._live_peak: float = 0.0
        self._paper_peak: float = 0.0
        self._daily_buy_notional: float = 0.0
        self._kill_handled = False
        self._loaded = False
        # Cached loss-halt bypass (#76) — refreshed by ``refresh_overrides`` on
        # each tick so the dashboard toggle takes effect immediately without a
        # restart. Applies in BOTH modes.
        self._bypass_loss_halt = False
        # Operator runtime per-trade cap override (#50). None → use the env
        # default (cfg.max_trade_usd). Refreshed every tick by
        # ``refresh_runtime_limits`` so the dashboard control applies without a
        # restart, in BOTH paper and live (this is a tuning knob, not a
        # safety-loosening override like the loss-halt bypass).
        self._runtime_max_trade_usd: float | None = None
        # Operator runtime trade size in SHARES (#89). None → fall back to the
        # dollar cap above. Refreshed every tick by ``refresh_runtime_limits``.
        self._runtime_trade_shares: float | None = None
        # Dashboard-editable live-risk knobs (#206) — same refresh cadence and
        # override-wins-else-cfg precedence as ``_runtime_max_trade_usd`` above,
        # backed by the shared ``runtime_knobs`` registry. None → this gate's
        # own ``cfg.X`` (so a GateConfig built with custom values, e.g. in
        # tests, is respected until an operator override is actually set).
        self._runtime_daily_loss_halt_usd: float | None = None
        self._runtime_max_entry_slippage: float | None = None
        self._runtime_bankroll_cap_usd: float | None = None
        self._runtime_exit_fill_timeout_seconds: float | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).date().isoformat()

    def _roll_daily_window(self) -> None:
        today = self._today()
        if today != self._date:
            self._date = today
            self._live_pnl = 0.0
            self._paper_pnl = 0.0
            self._live_peak = 0.0
            self._paper_peak = 0.0
            self._daily_buy_notional = 0.0

    async def load(self) -> None:
        """Rebuild counters from SQLite.

        Reads the split ``risk.{live,paper}_realized_pnl`` keys first.
        If either is absent, falls back through:
          1. The pre-split combined ``risk.daily_realized_pnl`` (issue #64)
             — its value is migrated INTO the live bucket on the assumption
             that pre-split state was almost always live-only PnL.
          2. The legacy ``btc_live.*`` keys (pre-#64).
        After ``persist()`` runs, only the new split keys are written; the
        older keys are ignored on subsequent boots.
        """
        date = await get_config(_RISK_DATE_KEY)
        live_pnl_raw = await get_config(_RISK_LIVE_PNL_KEY)
        paper_pnl_raw = await get_config(_RISK_PAPER_PNL_KEY)
        live_peak_raw = await get_config(_RISK_LIVE_PEAK_KEY)
        paper_peak_raw = await get_config(_RISK_PAPER_PEAK_KEY)
        notional_raw = await get_config(_RISK_NOTIONAL_KEY)
        # Migration order: split keys → combined #64 key → legacy #20 keys.
        if live_pnl_raw is None and paper_pnl_raw is None:
            combined_raw = await get_config(_RISK_COMBINED_PNL_KEY)
            if combined_raw is not None:
                live_pnl_raw = combined_raw
            elif date is None and notional_raw is None:
                date = await get_config(_LEGACY_DATE_KEY)
                live_pnl_raw = await get_config(_LEGACY_PNL_KEY)
                notional_raw = await get_config(_LEGACY_NOTIONAL_KEY)
        if date == self._today():
            try:
                self._live_pnl = float(live_pnl_raw or 0)
                self._paper_pnl = float(paper_pnl_raw or 0)
                self._daily_buy_notional = float(notional_raw or 0)
            except ValueError:
                self._live_pnl = 0.0
                self._paper_pnl = 0.0
                self._daily_buy_notional = 0.0
            # Peaks (#112): use the stored mark, but never below the current leg
            # PnL or 0. A pre-#112 session has no peak key → this derives
            # max(0, leg_pnl), so a never-profitable session keeps peak 0 and the
            # halt stays identical to the old fixed -limit floor.
            try:
                self._live_peak = max(0.0, self._live_pnl, float(live_peak_raw or 0))
                self._paper_peak = max(0.0, self._paper_pnl, float(paper_peak_raw or 0))
            except ValueError:
                self._live_peak = max(0.0, self._live_pnl)
                self._paper_peak = max(0.0, self._paper_pnl)
        self._date = self._today()
        self._loaded = True
        await self.persist()

    async def persist(self) -> None:
        await set_config(_RISK_DATE_KEY, self._date)
        await set_config(_RISK_LIVE_PNL_KEY, repr(self._live_pnl))
        await set_config(_RISK_PAPER_PNL_KEY, repr(self._paper_pnl))
        await set_config(_RISK_LIVE_PEAK_KEY, repr(self._live_peak))
        await set_config(_RISK_PAPER_PEAK_KEY, repr(self._paper_peak))
        await set_config(_RISK_NOTIONAL_KEY, repr(self._daily_buy_notional))

    async def refresh_overrides(self) -> None:
        """Re-read the operator loss-halt bypass from SQLite (BOTH modes, #76)."""
        self._bypass_loss_halt = (await get_config(_BYPASS_LOSS_HALT_KEY)) == "1"

    @property
    def bypass_loss_halt(self) -> bool:
        """Operator loss-halt bypass (#76) — applies to paper AND live."""
        return self._bypass_loss_halt

    @property
    def halt_pnl(self) -> float:
        """The realized-loss leg that drives THIS gate's halt (#76): live money
        in live mode, study PnL in paper mode."""
        self._roll_daily_window()
        return self._live_pnl if self.is_live else self._paper_pnl

    @property
    def halt_peak(self) -> float:
        """Session high-water mark of THIS gate's leg (#112). The trailing loss
        halt is measured as drawdown from this peak."""
        self._roll_daily_window()
        return self._live_peak if self.is_live else self._paper_peak

    @property
    def loss_halt_floor(self) -> float:
        """The PnL level at/below which the halt fires (#112): peak - limit.
        With peak 0 (never profitable) this is the old fixed -limit floor."""
        return self.halt_peak - self.effective_daily_loss_halt_usd

    @property
    def loss_halt_headroom(self) -> float:
        """USD this leg can still lose before the trailing halt fires (#112):
        current PnL minus the floor. Equals the full limit at the peak; shrinks
        as PnL falls below the peak; restored when a new peak is set."""
        return self.halt_pnl - self.loss_halt_floor

    def loss_halt_breached(self) -> bool:
        """True when this mode's own realized PnL has drawn down to/through the
        trailing floor (peak - limit) and the bypass is off (#76, #112). The
        loop uses this to STOP the bot."""
        if self._bypass_loss_halt:
            return False
        return self.halt_pnl <= self.loss_halt_floor

    async def refresh_runtime_limits(self) -> None:
        """Re-read operator runtime risk knobs from SQLite (BOTH paper and live).

        Distinct from ``refresh_overrides``: that reads the loss-halt bypass
        (a safety-loosening operator toggle, #76). The per-trade cap is a
        tuning knob the operator expects to apply everywhere, so this runs
        regardless of mode. A blank / unset / non-numeric / ≤0 value clears the
        override and the gate falls back to the env default.
        """
        self._runtime_max_trade_usd = await _read_positive(_RUNTIME_MAX_TRADE_KEY)
        self._runtime_trade_shares = await _read_positive(_RUNTIME_TRADE_SHARES_KEY)
        self._runtime_daily_loss_halt_usd = await _knobs.get_override("live_daily_loss_halt_usd")
        self._runtime_max_entry_slippage = await _knobs.get_override("live_max_entry_slippage")
        self._runtime_bankroll_cap_usd = await _knobs.get_override("live_bankroll_cap_usd")
        self._runtime_exit_fill_timeout_seconds = await _knobs.get_override("live_exit_fill_timeout_seconds")

    @property
    def runtime_max_trade_usd(self) -> float | None:
        """The operator-set per-trade cap override, or None when unset (raw)."""
        return self._runtime_max_trade_usd

    @property
    def runtime_trade_shares(self) -> float | None:
        """The operator-set trade size in shares (#89), or None when unset."""
        return self._runtime_trade_shares

    @property
    def trade_shares(self) -> float:
        """Shares per clip: the operator's size, else ``DEFAULT_TRADE_SHARES``."""
        if self._runtime_trade_shares is not None:
            return self._runtime_trade_shares
        return DEFAULT_TRADE_SHARES

    @property
    def effective_max_trade_usd(self) -> float:
        """The per-trade dollar cap in force.

        Precedence: a share-denominated trade size (#89) wins — N shares cost at
        most ~$N (binary prices are < 1), so the dollar cap is N and never blocks
        the bot's own N-share clip. Else the dollar override, else the env default.
        """
        if self._runtime_trade_shares is not None:
            return self._runtime_trade_shares
        if self._runtime_max_trade_usd is not None:
            return self._runtime_max_trade_usd
        return self.cfg.max_trade_usd

    @property
    def effective_daily_loss_halt_usd(self) -> float:
        """Dashboard-editable daily loss-halt limit (#206). Falls back to this
        gate's own ``cfg.daily_loss_halt_usd`` until an operator sets
        ``live_daily_loss_halt_usd`` from the Settings tab."""
        if self._runtime_daily_loss_halt_usd is not None:
            return self._runtime_daily_loss_halt_usd
        return self.cfg.daily_loss_halt_usd

    @property
    def effective_max_entry_slippage(self) -> float:
        """Dashboard-editable entry-slippage guard (#206)."""
        if self._runtime_max_entry_slippage is not None:
            return self._runtime_max_entry_slippage
        return self.cfg.max_entry_slippage

    @property
    def effective_bankroll_cap_usd(self) -> float | None:
        """Dashboard-editable daily bankroll cap (#206). 0/≤0 means disabled,
        matching the existing ``GateConfig.bankroll_cap_usd`` convention."""
        if self._runtime_bankroll_cap_usd is not None:
            return self._runtime_bankroll_cap_usd
        return self.cfg.bankroll_cap_usd

    @property
    def effective_exit_fill_timeout_seconds(self) -> float:
        """Dashboard-editable exit-fill timeout (#206), consumed by the live
        executor's exit-order retry loop."""
        if self._runtime_exit_fill_timeout_seconds is not None:
            return self._runtime_exit_fill_timeout_seconds
        return _knobs.KNOBS["live_exit_fill_timeout_seconds"].default

    # ------------------------------------------------------------------
    # Counters — fed by BOTH paper closes and live closes
    # ------------------------------------------------------------------

    async def record_realized_pnl(self, pnl_usd: float, *, is_live: bool) -> None:
        """Add realized PnL to the right bucket. Both feed the halt sum.

        ``is_live`` is the only call-site distinction — paper closes pass
        False so their PnL stays separated. The halt decision uses the gate's
        OWN leg (live or paper) per its ``is_live`` (#76), so paper losses no
        longer drive the live halt.
        """
        self._roll_daily_window()
        if is_live:
            self._live_pnl += pnl_usd
            self._live_peak = max(self._live_peak, self._live_pnl)
        else:
            self._paper_pnl += pnl_usd
            self._paper_peak = max(self._paper_peak, self._paper_pnl)
        await self.persist()

    async def record_buy_notional(self, notional_usd: float) -> None:
        self._roll_daily_window()
        self._daily_buy_notional += notional_usd
        await self.persist()

    @property
    def live_pnl(self) -> float:
        self._roll_daily_window()
        return self._live_pnl

    @property
    def paper_pnl(self) -> float:
        self._roll_daily_window()
        return self._paper_pnl

    @property
    def daily_realized_pnl(self) -> float:
        """Combined live+paper PnL. Reporting/back-compat surface only — the
        halt decision uses the per-mode leg (``halt_pnl``) since #76."""
        self._roll_daily_window()
        return self._live_pnl + self._paper_pnl

    @property
    def daily_buy_notional(self) -> float:
        self._roll_daily_window()
        return self._daily_buy_notional

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------

    def kill_switch_active(self) -> bool:
        return self.cfg.kill_switch_path.exists()

    def mark_kill_handled(self) -> None:
        self._kill_handled = True

    def kill_already_handled(self) -> bool:
        return self._kill_handled

    def rearm_kill(self) -> None:
        self._kill_handled = False

    # ------------------------------------------------------------------
    # The gate
    # ------------------------------------------------------------------

    def block_reason(self, req: EntryRequest) -> str | None:
        """Return why this entry is blocked, or ``None`` if all gates pass.

        Order matters only for the error message — the FIRST tripped gate is
        reported. The set of gates is canonical: every entry, paper or live,
        gets the same answer for the same inputs.
        """
        if self.kill_switch_active():
            return f"KILL switch active at {self.cfg.kill_switch_path}"
        self._roll_daily_window()
        if self.loss_halt_breached():
            leg = "live" if self.is_live else "paper"
            return (
                f"daily loss halt: {leg} realized {self.halt_pnl:+.2f} USD at/below "
                f"trailing floor {self.loss_halt_floor:+.2f} "
                f"(peak {self.halt_peak:+.2f} − {self.effective_daily_loss_halt_usd:.2f} limit)"
            )
        if req.position_open or req.entry_order_resting:
            return "an open position/order already exists (max 1)"
        if req.notional_usd <= 0:
            return "notional must be positive"
        cap = self.effective_max_trade_usd
        if req.notional_usd > cap:
            return (
                f"per-trade cap: {req.notional_usd:.2f} USD exceeds "
                f"{cap:.2f} USD"
            )
        cap_usd = self.effective_bankroll_cap_usd
        if (
            cap_usd is not None
            and cap_usd > 0
            and self._daily_buy_notional + req.notional_usd > cap_usd
        ):
            return (
                f"daily bankroll cap: {self._daily_buy_notional:.2f} + "
                f"{req.notional_usd:.2f} USD exceeds "
                f"{cap_usd:.2f} USD"
            )
        slippage_cap = self.effective_max_entry_slippage
        if (
            req.best_ask is not None
            and req.side_price is not None
            and req.side_price > 0
            and req.best_ask - req.side_price > slippage_cap
        ):
            return (
                f"entry slippage guard: book ask {req.best_ask:.3f} is "
                f"{req.best_ask - req.side_price:+.3f} above the signal price "
                f"{req.side_price:.3f} (max {slippage_cap:.3f}); "
                "the edge that justified this trade no longer exists"
            )
        return None


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------


def build_gate_from_config(*, is_live: bool = False) -> RiskGate:
    """Build a RiskGate from the global ``config`` module.

    The single source of truth: paper and live both use this so their gate
    configs cannot drift. The persisted state is NOT loaded here — callers
    must ``await gate.load()`` once before the first ``block_reason()``.

    ``is_live`` selects which realized-loss leg drives the halt (#76): the live
    gate halts on real-money PnL, the paper gate on study PnL.
    """
    import config as _config  # type: ignore[import-untyped]

    cfg = GateConfig(
        max_trade_usd=_config.TRADE_MAX_USD,
        daily_loss_halt_usd=_config.TRADE_DAILY_LOSS_HALT_USD,
        bankroll_cap_usd=_config.TRADE_BANKROLL_CAP_USD,
        max_entry_slippage=_config.TRADE_MAX_ENTRY_SLIPPAGE,
        kill_switch_path=Path(_config.KILL_SWITCH_PATH),
    )
    return RiskGate(cfg, is_live=is_live)


async def set_loss_halt_bypass(enabled: bool) -> None:
    """Persist the operator loss-halt bypass (#76; applies to paper AND live)."""
    await set_config(_BYPASS_LOSS_HALT_KEY, "1" if enabled else "0")


async def get_loss_halt_bypass() -> bool:
    return (await get_config(_BYPASS_LOSS_HALT_KEY)) == "1"


async def reset_daily_loss_halt() -> None:
    """Operator action: zero today's realized-loss tally AND the session peaks so
    the trailing halt clears (#112). Zeroing PnL alone is not enough — the floor
    is ``peak - limit``, so a banked peak would hold the halt breached even at
    PnL 0 (a +$30 day reset to PnL 0 still floors at +$20). Resetting the peaks
    drops the floor back to ``-limit``. The date and the bankroll-cap notional
    are intentionally left untouched. Caller enforces stopped-only — a running
    loop holds these counters in memory and would re-persist over the reset."""
    await set_config(_RISK_LIVE_PNL_KEY, "0.0")
    await set_config(_RISK_PAPER_PNL_KEY, "0.0")
    await set_config(_RISK_LIVE_PEAK_KEY, "0.0")
    await set_config(_RISK_PAPER_PEAK_KEY, "0.0")


async def migrate_clear_stale_bypass_v76() -> None:
    """One-shot (#76): the loss-halt bypass used to be paper-only and was
    structurally ignored in live. It now applies to live too, so a flag left ON
    from a paper study would silently disable the real-money halt. Clear it once
    so live starts halt-ON; the sentinel makes this idempotent and guarantees a
    later deliberate operator bypass is never wiped."""
    if (await get_config(_BYPASS_MIGRATED_KEY)) == "1":
        return
    await set_config(_BYPASS_LOSS_HALT_KEY, "0")
    await set_config(_BYPASS_MIGRATED_KEY, "1")


async def set_runtime_max_trade_usd(value: float | None) -> None:
    """Persist the operator runtime per-trade cap (#50).

    ``None`` (or ≤0) clears the override so the gate falls back to the env
    default. The live loop re-reads this every tick, so the change takes effect
    without a restart, in both paper and live.
    """
    if value is None or value <= 0:
        await set_config(_RUNTIME_MAX_TRADE_KEY, "")
    else:
        await set_config(_RUNTIME_MAX_TRADE_KEY, repr(float(value)))


async def get_runtime_max_trade_usd() -> float | None:
    """The persisted runtime per-trade cap, or None when unset/invalid."""
    return await _read_positive(_RUNTIME_MAX_TRADE_KEY)


async def set_runtime_trade_shares(value: float | None) -> None:
    """Persist the operator runtime trade size in shares (#89).

    ``None`` (or ≤0) clears it so the gate falls back to the dollar cap. Re-read
    every tick, so it takes effect without a restart, in both paper and live.
    """
    if value is None or value <= 0:
        await set_config(_RUNTIME_TRADE_SHARES_KEY, "")
    else:
        await set_config(_RUNTIME_TRADE_SHARES_KEY, repr(float(value)))


async def get_runtime_trade_shares() -> float | None:
    """The persisted runtime trade size in shares, or None when unset/invalid."""
    return await _read_positive(_RUNTIME_TRADE_SHARES_KEY)


async def _read_positive(key: str) -> float | None:
    """Read a config key as a positive float, or None when unset / invalid / ≤0."""
    raw = await get_config(key)
    if raw is None or raw.strip() == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


