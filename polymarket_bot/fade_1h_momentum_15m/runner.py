"""The Fade 1h Momentum on 15m loop: bookkeeping, inputs, the model hook, sizing and bids.

``run_forever(stop_event)`` runs for the dashboard's lifetime (started in the app lifespan,
after the market-data hub). Paper only: live trading is not authorised for any market, and
this strategy has no live order path. Every order is a resting limit bid; nothing crosses the
spread, and a hedge is a resting bid on the other side.

One pass, in the order that keeps the record honest
---------------------------------------------------
1. Setup, once per process: seed the dials (version 0, the prior, and version 1, the starting
   dials fitted on the Sep 17-20 tape: ``learner.seed_starting_dials``), and stop every bid a
   previous run left resting (their tape is still read, so fills made before the restart are
   kept). Until this has worked, no new bid is placed; bookkeeping runs regardless.
2. Bookkeeping, every pass whatever the switch or the mode says: bring fills up to date from
   the trade tape, then settle every ended window the venue has resolved, traded or not, and
   let the learner take one step on every window just settled (``learner.learn_from_settled``,
   off the event loop; a new dials version per step). Each step is guarded on its own.
3. The executor for this pass, from the operator's PAPER/LIVE selection and the kill switch.
   Anything but PAPER places nothing: LIVE shows "not built / not authorised" on the card,
   resting paper bids are cancelled, and the paper bids already filled keep settling.
4. The strategy switch. Off: cancel resting bids, release the market data, open nothing new.
5. On: read Settings, gather every coin's inputs, and ask the model (``decide.decide``, off
   the event loop: about 2,000 simulated paths a coin) for a Decision per coin: the side, the
   chance traded on, every ladder rung's fill and win chances, and hedge quotes.
6. For the coins with a Decision: size the ladders against the free bankroll, shrink them by
   the joint Kelly of all the coins decided in this pass, size any hedge of a position held in
   the window, then bring the resting bids in line with that plan (unchanged bids keep their
   place in the queue; the rest are cancelled and replaced in one transaction).
7. Record one ``fade_decisions`` row per coin (its inputs, and what was done or why nothing
   was), and this pass's state for the card (``status()`` and the ``STATUS_KEY`` config row).
8. Sleep for the poll interval from Settings.

The free bankroll (``W``) is the starting paper bankroll plus settled P&L, minus what filled
but unsettled shares cost, minus the unfilled part of every bid that could still turn out to
have filled (the tape runs minutes behind) except the resting bids this pass is about to
re-plan. A bid cancelled this pass counts against ``W`` from the next pass until its tape is
read. The entry ladder is sized from ``W`` alone and the hedge from ``W`` and the held shares,
as sections 3 and 4 of ``tasks/2026-09-22-fade-1h-sizing-hedging.md`` define them.

Never raises (a cancel still propagates, which the app's teardown expects): every step is
guarded, and every failure is logged and shown on the card as ``last_error``.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

import db as _db  # type: ignore[import-untyped]
from logging_setup import get_logger
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot import strategies as _strategies
from polymarket_bot.fade_1h_momentum_15m import decide as _decide
from polymarket_bot.fade_1h_momentum_15m import executor as _executor
from polymarket_bot.fade_1h_momentum_15m import inputs as _inputs
from polymarket_bot.fade_1h_momentum_15m import learner as _learner
from polymarket_bot.fade_1h_momentum_15m import ledger as _ledger
from polymarket_bot.fade_1h_momentum_15m import sizing as _sizing
from polymarket_bot.fade_1h_momentum_15m.decide import (
    Decision, DecideSettings, NoDecision, RungQuote,
)
from polymarket_bot.fade_1h_momentum_15m.inputs import Inputs, Problem

log = get_logger("fade_1h.runner")

STRATEGY = "fade_1h_momentum_15m"
LABEL = "Fade 1h (15m)"
OWNER = _inputs.OWNER
ASSETS = _inputs.ASSETS
STATUS_KEY = "fade_1h.runner_status"
DEFAULT_POLL_S = 60.0
MIN_SLEEP_S = 1.0
HTTP_TIMEOUT_S = 25.0

# Event types written to the activity feed (colours in the dashboard's _FEED_KIND).
FILL_EVENT = "fade1h_fill"
SETTLED_EVENT = "fade1h_settled"

# Decision row actions.
NO_INPUTS = "no_inputs"  # a coin's inputs were missing, stale or inconsistent
COIN_OFF = "coin_off"  # the coin is switched off in Settings
NO_MODEL = "no_model"  # the model gave no Decision (it cannot price this window)
MODEL_ERROR = "model_error"  # the model hook raised or returned something unusable
NO_BID = "no_bid"  # a Decision, but the maths gives no stake anywhere
BID = "bid"  # bids planned (kept or placed)
NOT_PLACED = "not_placed"  # a plan, but this pass may not place bids (LIVE, kill switch...)
REFUSED = "refused"  # follow-up row: the executor refused the plan

NO_MODEL_REASON = "The model gave no decision for this window: inputs recorded, no bids."


_SHARE_STEP = 0.01  # the venue takes sizes in hundredths of a share
_MIN_SHARES = 5.0  # the venue's minimum order size on these markets
_PRICE_EPS = 1e-9
_MAX_NAMED = 5

# The card's view of the runner, replaced whole at the end of every pass.
_STATUS: dict[str, Any] = {"state": "not_started", "last_pass_ts": None, "last_error": None}


def status() -> dict[str, Any]:
    """A copy of the runner's latest state for the card (``STATUS_KEY`` holds the same)."""
    return copy.deepcopy(_STATUS)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """The operator's Settings for this strategy, read fresh every pass."""

    poll_s: float
    bankroll_usd: float
    kelly_multiplier: float
    max_order_usd: float
    band_lo: float  # dollars under the side's best ask, nearest rung
    band_hi: float  # deepest rung
    hedge: bool
    spot_feed: str
    coins: Mapping[str, bool]

    @property
    def decide(self) -> DecideSettings:
        return DecideSettings(spot_feed=self.spot_feed, band_lo=self.band_lo,
                              band_hi=self.band_hi)

    def as_record(self) -> dict[str, Any]:
        return {
            "bankroll_usd": self.bankroll_usd, "kelly_multiplier": self.kelly_multiplier,
            "max_order_usd": self.max_order_usd, "band_lo": self.band_lo,
            "band_hi": self.band_hi, "hedge": self.hedge, "spot_feed": self.spot_feed,
            "coins": dict(self.coins),
        }


async def read_poll_interval() -> float:
    """The pass interval from Settings; the default when it cannot be read."""
    try:
        value = float(await _knobs.get("fade1h_poll_interval_seconds"))
    except Exception as exc:  # noqa: BLE001 - a knob read must not stop the loop
        log.warning("fade1h.poll_interval_unreadable", error=f"{type(exc).__name__}: {exc}")
        return DEFAULT_POLL_S
    return value if math.isfinite(value) and value > 0 else DEFAULT_POLL_S


async def read_settings() -> Settings:
    """Every Setting this strategy reads. Raises if one cannot be read."""
    get = _knobs.get
    return Settings(
        poll_s=float(await get("fade1h_poll_interval_seconds")),
        bankroll_usd=float(await get("fade1h_bankroll_usd")),
        kelly_multiplier=float(await get("fade1h_kelly_multiplier")),
        max_order_usd=float(await get("fade1h_max_order_usd")),
        band_lo=float(await get("fade1h_ladder_lo_cents")) / 100.0,
        band_hi=float(await get("fade1h_ladder_hi_cents")) / 100.0,
        hedge=bool(await get("fade1h_hedge_enabled")),
        spot_feed=str(await get("fade1h_spot_feed")),
        coins={a: bool(await get(f"fade1h_trade_{a}")) for a in ASSETS},
    )


# ---------------------------------------------------------------------------
# Sizing: from Decisions to a plan of resting bids (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedBid:
    """One resting bid the plan wants in the book."""

    kind: str  # entry | hedge
    side: str
    token_id: str
    price: float
    shares: float
    rung: int
    depth_ahead: float
    p_fill: float | None = None
    q_fill: float | None = None

    @property
    def cost_usd(self) -> float:
        return self.price * self.shares

    def as_record(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "rung": self.rung, "kind": self.kind, "side": self.side, "price": self.price,
            "shares": self.shares, "stake_usd": round(self.cost_usd, 6),
            "depth_ahead": self.depth_ahead,
        }
        if self.p_fill is not None:
            out["p_fill"], out["q_fill"] = self.p_fill, self.q_fill
        return out


@dataclass
class CoinPlan:
    """What the maths wants resting for one coin this pass, and how it got there."""

    asset: str
    window_slug: str
    side: str
    bids: list[PlannedBid] = field(default_factory=list)
    kelly_f: float = 0.0  # bankroll fraction of the ladder after joint sizing and multiplier
    full_kelly_usd: float = 0.0  # the ladder alone at full Kelly, dollars
    joint_scale: float = 0.0  # joint Kelly over the coin's own Kelly, 0..1
    q_bar: float | None = None  # the ladder's share-weighted win chance given a fill
    price_bar: float | None = None  # its average price per share
    hedge: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def entries(self) -> list[PlannedBid]:
        return [b for b in self.bids if b.kind == "entry"]

    @property
    def entry_cost_usd(self) -> float:
        return sum(b.cost_usd for b in self.entries)

    @property
    def cost_usd(self) -> float:
        return sum(b.cost_usd for b in self.bids)

    def sizing_record(self, bankroll_usd: float) -> dict[str, Any]:
        return {
            "bankroll_usd": bankroll_usd, "full_kelly_usd": self.full_kelly_usd,
            "joint_scale": self.joint_scale, "kelly_f": self.kelly_f, "q_bar": self.q_bar,
            "price_bar": self.price_bar, "entry_cost_usd": self.entry_cost_usd,
            "hedge_cost_usd": self.cost_usd - self.entry_cost_usd,
        }


def _book(inputs: Inputs, side: str) -> Any:
    return inputs.up_book if side == "Up" else inputs.down_book


def _token(inputs: Inputs, side: str) -> str:
    return inputs.up_token if side == "Up" else inputs.down_token


def _on_tick(price: float, tick: float) -> float:
    """``price`` rounded down onto the tick grid (a lower bid never crosses)."""
    step = tick if math.isfinite(tick) and tick > 0 else _inputs.DEFAULT_TICK
    return round(math.floor(price / step + 1e-6) * step, 6)


def _order_shares(stake_usd: float, price: float, max_order_usd: float) -> float:
    """Shares for a dollar stake: capped at the largest single bid, rounded down to the
    venue's share step, and zero below its minimum order."""
    stake = min(stake_usd, max_order_usd)
    if not (math.isfinite(stake) and stake > 0):
        return 0.0
    return _sizing.size_to_order(stake, price, min_shares=_MIN_SHARES, tick=_SHARE_STEP)


def _grid_rungs(decision: Decision, inputs: Inputs, settings: Settings,
                notes: list[str]) -> list[RungQuote]:
    """The Decision's rungs that sit on this pass's ladder grid, nearest first."""
    book = _book(inputs, decision.side)
    grid = _decide.ladder_prices(book.best_ask, inputs.tick_size, settings.band_lo,
                                 settings.band_hi)
    on_grid = {round(p, 6) for p in grid}
    kept: dict[float, RungQuote] = {}
    off = 0
    for rung in decision.rungs:
        key = round(rung.price, 6)
        if key not in on_grid or key in kept:
            off += 1
            continue
        kept[key] = rung
    if off:
        notes.append(f"{off} rung(s) were not on the ladder grid ({settings.band_lo * 100:g}-"
                     f"{settings.band_hi * 100:g}c under the {decision.side} ask of "
                     f"{book.best_ask:g}) and were left out.")
    return sorted(kept.values(), key=lambda r: -r.price)


def plan_orders(
    cases: Mapping[str, tuple[Inputs, Decision]],
    held: Mapping[str, Mapping[str, float]],
    *,
    bankroll_usd: float,
    rho: float,
    settings: Settings,
) -> dict[str, CoinPlan]:
    """Size every coin's Decision into resting bids (pure; run it off the event loop).

    ``held``: shares already filled in each coin's current window, ``{asset: {side: n}}``.

    1. Each coin's ladder alone at full Kelly, ``sizing.ladder`` against ``bankroll_usd``.
    2. Each ladder summarised as one bet (its average price per share, and its share-weighted
       win chance given a fill); ``sizing.joint_kelly`` sizes those bets together with the
       copula correlation ``rho``. A coin's ladder is scaled by its joint stake over its own
       Kelly stake (joint sizing only ever shrinks), then by the Kelly multiplier.
    3. A hedge for a net position held in the window, from the matching HedgeQuote, by
       ``sizing.hedge_shares_given_fill`` with the paired shares added to the cash (they pay
       $1 whichever side wins) and only the unpaired shares at risk.
    4. Stakes become shares with ``size_to_order`` (the largest single bid caps each one), and
       if the plan as a whole would cost more than the free bankroll it is scaled down to fit.
    """
    W = float(bankroll_usd)
    k = float(settings.kelly_multiplier)
    plans: dict[str, CoinPlan] = {}
    ladders: dict[str, tuple[list[RungQuote], list[float]]] = {}

    for asset, (inp, dec) in cases.items():
        plan = CoinPlan(asset=asset, window_slug=inp.window_slug, side=dec.side)
        plans[asset] = plan
        rungs = _grid_rungs(dec, inp, settings, plan.notes)
        if not rungs:
            if not dec.rungs:
                plan.notes.append("The model gave no ladder rungs.")
            continue
        if W <= 0:
            plan.notes.append(f"No free bankroll (${W:.2f}), so no new entry bids.")
            continue
        try:
            full = _sizing.ladder([(r.price, r.p_fill, r.q_fill) for r in rungs], W, 1.0)
        except ValueError as exc:
            plan.notes.append(f"The model's rung chances cannot be sized: {exc}.")
            continue
        total = sum(full)
        plan.full_kelly_usd = total
        if total <= 0:
            plan.notes.append("No rung's chance of winning beats its price, so the ladder "
                              "gets no stake.")
            continue
        shares = sum(x / r.price for x, r in zip(full, rungs))
        plan.price_bar = total / shares
        plan.q_bar = min(1.0, max(0.0, sum(x / r.price * r.q_fill
                                           for x, r in zip(full, rungs)) / shares))
        ladders[asset] = (rungs, full)

    if ladders:
        names = list(ladders)
        bets = [_sizing.Bet(plans[a].q_bar, plans[a].price_bar) for a in names]
        joint = _sizing.joint_kelly(bets, rho, 1.0)
        for asset, f_joint in zip(names, joint):
            plan = plans[asset]
            alone = _sizing.kelly_maker(plan.q_bar, plan.price_bar)
            plan.joint_scale = min(1.0, max(0.0, f_joint / alone)) if alone > 0 else 0.0
            plan.kelly_f = k * f_joint
            inp, dec = cases[asset]
            book = _book(inp, dec.side)
            rungs, full = ladders[asset]
            for i, (rung, x) in enumerate(zip(rungs, full)):
                n = _order_shares(k * plan.joint_scale * x, rung.price, settings.max_order_usd)
                if n > 0:
                    plan.bids.append(PlannedBid(
                        kind="entry", side=dec.side, token_id=_token(inp, dec.side),
                        price=rung.price, shares=n, rung=i,
                        depth_ahead=book.depth_ahead(rung.price),
                        p_fill=rung.p_fill, q_fill=rung.q_fill,
                    ))
            if not plan.entries:
                plan.notes.append("The ladder's stakes are all below the venue's minimum "
                                  f"order of {_MIN_SHARES:g} shares.")

    if settings.hedge:
        for asset, (inp, dec) in cases.items():
            _plan_hedge(plans[asset], inp, dec, held.get(asset) or {}, W, settings)

    total_cost = sum(p.cost_usd for p in plans.values())
    if total_cost > max(W, 0.0) + 1e-9:
        factor = max(W, 0.0) / total_cost
        for plan in plans.values():
            resized = []
            for b in plan.bids:
                n = _order_shares(b.cost_usd * factor, b.price, settings.max_order_usd)
                if n > 0:
                    resized.append(dataclasses.replace(b, shares=min(n, b.shares)))
            if plan.bids:
                plan.notes.append(f"Scaled down to {factor:.0%} so the plan fits the free "
                                  f"bankroll of ${max(W, 0.0):.2f}.")
            plan.bids = resized
    return plans


def _plan_hedge(plan: CoinPlan, inp: Inputs, dec: Decision, held: Mapping[str, float],
                W: float, settings: Settings) -> None:
    up, down = float(held.get("Up") or 0.0), float(held.get("Down") or 0.0)
    if abs(up - down) <= _PRICE_EPS:
        return
    side_held = "Up" if up > down else "Down"
    n, paired = abs(up - down), min(up, down)
    quote = dec.hedge_for(side_held)
    record: dict[str, Any] = {"held": side_held, "held_shares": n, "paired_shares": paired}
    plan.hedge = record
    if quote is None:
        record["note"] = f"The model gave no hedge quote for the {side_held} position."
        return
    other = quote.side
    book = _book(inp, other)
    price = _on_tick(quote.price, inp.tick_size)
    record.update(side=other, price=price, p_held_given_fill=quote.p_held_given_fill)
    if not 0.0 < price < book.best_ask - _PRICE_EPS:
        record["note"] = (f"The hedge quote {quote.price:g} is not below the {other} ask of "
                          f"{book.best_ask:g}; a hedge only ever rests.")
        return
    h = _sizing.hedge_shares_given_fill(quote.p_held_given_fill, price, W + paired, n)
    shares = _order_shares(h * price, price, settings.max_order_usd)
    record.update(optimal_shares=h, shares=shares)
    if shares > 0:
        plan.bids.append(PlannedBid(
            kind="hedge", side=other, token_id=_token(inp, other), price=price,
            shares=shares, rung=0, depth_ahead=book.depth_ahead(price),
        ))


def diff_orders(planned: Sequence[PlannedBid], resting: Sequence[Mapping[str, Any]]
                ) -> tuple[list[int], list[int], list[PlannedBid]]:
    """Match the plan against the bids resting now: ``(keep_ids, cancel_ids, new_bids)``.

    A resting bid is kept, with its place in the queue, when the plan wants the same kind,
    side, token and price with the same shares still unfilled. Every other resting bid is
    cancelled and every unmatched planned bid is placed.
    """
    keep: list[int] = []
    new: list[PlannedBid] = []
    left = list(resting)
    for bid in planned:
        match = None
        for row in left:
            remaining = float(row["shares"]) - float(row.get("filled_shares") or 0.0)
            if (row["kind"] == bid.kind and row["side"] == bid.side
                    and str(row["token_id"]) == bid.token_id
                    and abs(float(row["price"]) - bid.price) <= _PRICE_EPS
                    and abs(remaining - bid.shares) < _SHARE_STEP / 2):
                match = row
                break
        if match is None:
            new.append(bid)
        else:
            keep.append(int(match["id"]))
            left.remove(match)
    return keep, [int(r["id"]) for r in left], new


def free_bankroll(start_usd: float, summary: Mapping[str, Any],
                  pending: Sequence[Mapping[str, Any]], replanned: set[str]) -> float:
    """The bankroll free for this pass's plan (module docstring).

    ``pending``: ``ledger.orders_needing_flow()`` rows. ``replanned``: the window slugs whose
    resting bids this pass re-plans (their cost is the plan's, not a deduction).
    """
    unread = 0.0
    for row in pending:
        if row.get("state") in _ledger.RESTING_STATES and row.get("window_slug") in replanned:
            continue
        remaining = float(row["shares"]) - float(row.get("filled_shares") or 0.0)
        unread += max(0.0, remaining) * float(row["price"])
    return (float(start_usd) + float(summary.get("net_pnl_usd") or 0.0)
            - float(summary.get("open_exposure_usd") or 0.0) - unread)


def _decide_all(ready: Mapping[str, Inputs], params: Mapping[str, Any],
                settings: DecideSettings) -> dict[str, Any]:
    """Every ready coin's Decision (or None, or the exception it raised). Runs in a worker
    thread; one coin failing never stops the others."""
    out: dict[str, Any] = {}
    for asset, inputs in ready.items():
        try:
            out[asset] = _decide.decide(inputs, params, settings)
        except Exception as exc:  # noqa: BLE001 - reported per coin by the caller
            out[asset] = exc
    return out


# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------


@dataclass
class PassReport:
    """What one pass did, for the card."""

    ts: float
    state: str = "running"
    errors: list[str] = field(default_factory=list)
    assets: dict[str, dict[str, Any]] = field(default_factory=dict)
    fills: int = 0
    shares_filled: float = 0.0
    settled: int = 0
    settle_waiting: dict[str, int] = field(default_factory=dict)
    executor: dict[str, Any] = field(default_factory=dict)
    bankroll: dict[str, Any] = field(default_factory=dict)
    dials_version: int | None = None
    cancelled: int = 0
    learned: int = 0  # settled windows the learner took a step on this pass
    learn_note: str | None = None

    def fail(self, step: str, exc: BaseException | str) -> str:
        text = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
        message = f"{step} failed: {text}"
        self.errors.append(message)
        log.warning("fade1h.step_failed", step=step, error=text)
        return message


def _coin(asset: str) -> str:
    return asset.upper()


class Runner:
    """One runner per process: keeps the HTTP client, the input memory and the bookkeeper
    (which remembers when each window's result was last looked up) for its lifetime."""

    def __init__(
        self,
        client: Any,
        *,
        hub_fn: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.time,
        kill_switch_path: Any = None,
        memory: _inputs.InputMemory | None = None,
        bookkeeper: _executor.PaperBookkeeper | None = None,
    ) -> None:
        self._client = client
        self._hub_fn = hub_fn or _current_hub
        self._clock = clock
        self._kill_switch_path = kill_switch_path
        self._memory = memory or _inputs.InputMemory()
        self.bookkeeper = bookkeeper or _executor.PaperBookkeeper(client)
        self._ready = False
        self._last_hub: Any = None
        self._passes = 0
        self._last_error: str | None = None
        self._last_error_ts: float | None = None

    # -- the pass -----------------------------------------------------------

    async def pass_once(self) -> PassReport:
        """One guarded pass. Never raises (a cancel propagates)."""
        now = float(self._clock())
        report = PassReport(ts=now)
        await self._setup(now, report)
        await self._bookkeeping(now, report)
        choice = await self._choose(now, report)
        try:
            on = await _strategies.enabled(STRATEGY)
        except Exception as exc:  # noqa: BLE001 - fail closed
            report.fail("Reading the strategy switch", exc)
            on = False
        if not on:
            await self._switched_off(now, report)
        elif not self._ready:
            report.state = "setting_up"
            report.errors.append("Setup has not finished, so no new bids are placed yet.")
        else:
            if choice is None or not choice.can_place:
                # Inputs are still read and recorded; the card shows why nothing is placed.
                report.state = choice.state if choice is not None else "no_executor"
            try:
                await self._trade(now, choice, report)
            except Exception as exc:  # noqa: BLE001 - shown on the card
                report.fail("The trading step", exc)
        await self._finish(report)
        return report

    async def _setup(self, now: float, report: PassReport) -> None:
        if self._ready:
            return
        try:
            dials = await _learner.seed_starting_dials(ts=now)
            report.dials_version = int(dials["version"])
            await self.bookkeeper.recover_after_restart(now=now)
        except Exception as exc:  # noqa: BLE001 - retried next pass
            report.fail("Setup (dials and restart recovery)", exc)
            return
        self._ready = True

    async def _bookkeeping(self, now: float, report: PassReport) -> None:
        try:
            fills = await self.bookkeeper.sync_fills(now=now)
        except Exception as exc:  # noqa: BLE001
            report.fail("Checking fills", exc)
        else:
            report.fills = len(fills.fills)
            report.shares_filled = fills.shares_filled
            report.errors.extend(fills.errors)
            await self._notify_fills(fills.fills)
        try:
            settled = await self.bookkeeper.settle(now=now)
        except Exception as exc:  # noqa: BLE001
            report.fail("Settling", exc)
        else:
            report.settled = len(settled.settled)
            report.settle_waiting = {
                "resolution": settled.waiting_resolution, "tape": settled.waiting_tape,
                "retry_later": settled.retry_later, "no_market_id": settled.no_market_id,
                "backlog": settled.backlog,
            }
            report.errors.extend(settled.errors)
            await self._notify_settled(settled.settled)
            if settled.settled:
                await self._learn(now, settled.settled, report)

    async def _learn(self, now: float, settled: Sequence[_ledger.Settlement],
                     report: PassReport) -> None:
        """One learning step on the windows just settled (the learner never raises)."""
        try:
            learned = await _learner.learn_from_settled(settled, ts=now)
        except Exception as exc:  # noqa: BLE001 - belt and braces
            report.fail("Learning", exc)
            return
        report.learned += learned.windows
        report.errors.extend(learned.errors)
        if learned.version is not None:
            report.dials_version = learned.version
            report.learn_note = learned.note

    async def _choose(self, now: float, report: PassReport) -> _executor.ExecutorChoice | None:
        try:
            choice = await _executor.choose_executor(
                self._client, bookkeeper=self.bookkeeper, kill_switch_path=self._kill_switch_path
            )
        except Exception as exc:  # noqa: BLE001 - no executor: place nothing
            message = report.fail("Choosing the executor", exc)
            report.executor = {"state": _executor.UNKNOWN_MODE_STATE, "requested_mode": None,
                               "message": f"{message}. No new bids this pass.",
                               "can_place": False}
            try:
                report.cancelled += await _ledger.cancel_all_open(
                    ts=now, reason=_executor.UNKNOWN_MODE_STATE)
            except Exception as exc2:  # noqa: BLE001
                report.fail("Cancelling resting bids", exc2)
            return None
        report.executor = {"state": choice.state, "requested_mode": choice.requested_mode,
                           "message": choice.message, "can_place": choice.can_place}
        if not choice.can_place:
            try:
                report.cancelled += await _executor.stand_down(choice, now=now)
            except Exception as exc:  # noqa: BLE001
                report.fail("Cancelling resting bids", exc)
        return choice

    async def _switched_off(self, now: float, report: PassReport) -> None:
        report.state = "switched_off"
        try:
            report.cancelled += await _ledger.cancel_all_open(ts=now, reason="switched_off")
        except Exception as exc:  # noqa: BLE001
            report.fail("Cancelling resting bids", exc)
        self.release()
        for asset in ASSETS:
            report.assets[asset] = {
                "action": "switched_off",
                "reason": "The strategy is switched off: no new bids. Fills and settlement "
                          "keep running.",
            }

    async def _trade(self, now: float, choice: _executor.ExecutorChoice | None,
                     report: PassReport) -> None:
        settings = await read_settings()
        dials = await _ledger.dials() or await _ledger.seed_dials(ts=now)
        params = dict(dials.get("params") or {})
        version = int(dials["version"])
        report.dials_version = version
        hub = self._hub_fn()
        if hub is not None:
            self._last_hub = hub
        gathered = await _inputs.gather(hub, self._client, now, memory=self._memory)
        state = choice.state if choice is not None else _executor.UNKNOWN_MODE_STATE
        can_place = choice is not None and choice.can_place

        cases: dict[str, tuple[Inputs, Decision]] = {}
        simple: dict[str, tuple[str, str, Inputs | Problem]] = {}
        ready: dict[str, Inputs] = {}
        for asset in ASSETS:
            got = gathered.get(asset)
            if not isinstance(got, (Inputs, Problem)):
                got = Problem(asset=asset, code="internal_error", ts=now,
                              message="No inputs were returned for this coin.")
            if isinstance(got, Problem):
                simple[asset] = (NO_INPUTS, got.message, got)
            elif not settings.coins.get(asset, True):
                simple[asset] = (COIN_OFF, f"{_coin(asset)} is switched off in Settings: "
                                           "inputs recorded, no bids.", got)
            else:
                ready[asset] = got
        # The model runs off the event loop (a capped simulation per coin).
        answers = await asyncio.to_thread(_decide_all, ready, params, settings.decide)
        for asset, got in ready.items():
            decision = answers.get(asset)
            if isinstance(decision, NoDecision):
                simple[asset] = (NO_MODEL, f"The model cannot price this window: {decision} "
                                           "No bids for this coin.", got)
                continue
            if isinstance(decision, BaseException):
                exc = decision
                simple[asset] = (MODEL_ERROR, f"The model failed: {type(exc).__name__}: "
                                              f"{exc}. No bids for this coin.", got)
                log.warning("fade1h.model_failed", asset=asset, error=str(exc))
                continue
            if decision is None:
                simple[asset] = (NO_MODEL, NO_MODEL_REASON, got)
            elif not isinstance(decision, Decision):
                simple[asset] = (MODEL_ERROR, "The model returned something other than a "
                                              "Decision. No bids for this coin.", got)
            else:
                cases[asset] = (got, decision)

        resting = await _ledger.open_orders()
        plans: dict[str, CoinPlan] = {}
        W: float | None = None
        if cases:
            try:
                W, plans = await self._plan(cases, params, settings)
            except Exception as exc:  # noqa: BLE001 - record the inputs anyway, bid nothing
                message = report.fail("Sizing", exc)
                plans = {
                    asset: CoinPlan(asset=asset, window_slug=inp.window_slug, side=dec.side,
                                    notes=[f"{message}. No bids this pass."])
                    for asset, (inp, dec) in cases.items()
                }
        else:
            try:
                W = await _ledger.free_cash_usd(settings.bankroll_usd)
            except Exception as exc:  # noqa: BLE001 - only the card's figure is lost
                report.fail("Reading the free bankroll", exc)
        report.bankroll = {"start_usd": settings.bankroll_usd, "free_usd": W}

        for asset in ASSETS:
            mine = [o for o in resting if o.get("asset") == asset]
            try:
                if asset in simple:
                    action, reason, got = simple[asset]
                    await self._record_simple(now, asset, action, reason, got, settings,
                                              state, version, report)
                    await self._cancel(choice, [int(o["id"]) for o in mine], action, now,
                                       report)
                else:
                    inp, dec = cases[asset]
                    await self._act(now, asset, inp, dec, plans[asset], mine, settings, W,
                                    choice, state, can_place, version, report)
            except Exception as exc:  # noqa: BLE001 - one coin must not stop the others
                message = report.fail(f"{_coin(asset)}", exc)
                report.assets.setdefault(asset, {})["error"] = message

    async def _plan(self, cases: Mapping[str, tuple[Inputs, Decision]],
                    params: Mapping[str, Any], settings: Settings
                    ) -> tuple[float, dict[str, CoinPlan]]:
        """The free bankroll and every decided coin's plan."""
        summary = await _ledger.summary()
        pending = await _ledger.orders_needing_flow()
        positions = await _ledger.open_positions()
        replanned = {inp.window_slug for inp, _ in cases.values()}
        W = free_bankroll(settings.bankroll_usd, summary, pending, replanned)
        held: dict[str, dict[str, float]] = {}
        for row in positions:
            for asset, (inp, _) in cases.items():
                if row["window_slug"] == inp.window_slug:
                    held.setdefault(asset, {})[row["side"]] = float(row["shares"] or 0.0)
        rho = float(params.get("rho", _ledger.PRIOR_DIALS["rho"]))
        plans = await asyncio.to_thread(
            plan_orders, cases, held, bankroll_usd=W, rho=rho, settings=settings)
        return W, plans

    async def _record_simple(self, now: float, asset: str, action: str, reason: str,
                             got: Inputs | Problem, settings: Settings, state: str,
                             version: int, report: PassReport) -> None:
        record = got.as_record()
        record["settings"] = settings.as_record()
        decision_id = await _ledger.record_decision(
            ts=now, asset=asset, inputs=record, action=action,
            window_slug=got.window_slug, mode=state, dials_version=version, reason=reason,
        )
        entry: dict[str, Any] = {"action": action, "reason": reason,
                                 "window_slug": got.window_slug, "decision_id": decision_id}
        if isinstance(got, Problem):
            entry["codes"] = list(got.codes)
        report.assets[asset] = entry

    async def _cancel(self, choice: _executor.ExecutorChoice | None, ids: list[int],
                      reason: str, now: float, report: PassReport) -> None:
        """Cancel a coin's resting bids. When this pass may not place bids they were all
        stood down already."""
        if not ids or choice is None or choice.executor is None:
            return
        report.cancelled += await choice.executor.cancel(ids, reason=reason, now=now)

    async def _act(self, now: float, asset: str, inp: Inputs, dec: Decision, plan: CoinPlan,
                   resting: list[dict], settings: Settings, W: float | None,
                   choice: _executor.ExecutorChoice | None, state: str, can_place: bool,
                   version: int, report: PassReport) -> None:
        if not plan.bids:
            action = NO_BID
            reason = " ".join(plan.notes) or "The maths gives no stake at any rung."
        elif not can_place:
            action = NOT_PLACED
            reason = (choice.message if choice is not None
                      else "No executor this pass, so no new bids.")
        else:
            action, reason = BID, (" ".join(plan.notes) or None)
        record = inp.as_record()
        record["settings"] = settings.as_record()
        entries = plan.entries
        hedge_bid = next((b for b in plan.bids if b.kind == "hedge"), None)
        hedge = dict(plan.hedge or {})
        if hedge_bid is not None:
            hedge["bid"] = hedge_bid.as_record()
        decision_id = await _ledger.record_decision(
            ts=now, asset=asset, inputs=record, action=action, window_slug=inp.window_slug,
            mode=state, dials_version=version, p_model=dec.p_model, p=dec.p, side=dec.side,
            kelly_f=plan.kelly_f, stake_usd=plan.entry_cost_usd,
            ladder=[b.as_record() for b in entries], hedge=hedge or None,
            factors={"model": dict(dec.factors),
                     "sizing": plan.sizing_record(W if W is not None else 0.0),
                     "explanation": dec.explanation},
            reason=reason,
        )
        entry: dict[str, Any] = {
            "action": action, "reason": reason, "window_slug": inp.window_slug,
            "decision_id": decision_id, "side": dec.side, "p": dec.p, "p_model": dec.p_model,
            "explanation": dec.explanation, "bids": [b.as_record() for b in plan.bids],
        }
        report.assets[asset] = entry
        if not can_place or choice is None or choice.executor is None:
            return
        in_window = [o for o in resting if o["window_slug"] == inp.window_slug]
        elsewhere = [int(o["id"]) for o in resting if o["window_slug"] != inp.window_slug]
        keep, cancel, new = diff_orders(plan.bids, in_window)
        cancel += elsewhere
        entry.update(kept=len(keep), cancelled=len(cancel), placed=0)
        if not cancel and not new:
            return
        orders = [
            _ledger.NewOrder(
                window_slug=inp.window_slug, token_id=b.token_id, side=b.side, kind=b.kind,
                price=b.price, shares=b.shares, rung=b.rung, depth_ahead=b.depth_ahead,
                decision_id=decision_id,
            )
            for b in new
        ]
        try:
            ids = await choice.executor.requote(
                orders, best_asks={inp.up_token: inp.up_book.best_ask,
                                   inp.down_token: inp.down_book.best_ask},
                now=now, cancel_ids=cancel, cancel_reason="requote",
            )
        except _executor.PlacementRefused as exc:
            message = f"The bids were refused ({exc.reason}): {exc}"
            entry.update(action=REFUSED, reason=message, cancelled=0)
            report.errors.append(f"{_coin(asset)}: {message}")
            await _ledger.record_decision(
                ts=now, asset=asset, window_slug=inp.window_slug, mode=state,
                dials_version=version, action=REFUSED, reason=message,
                inputs={"status": "refused", "decision_id": decision_id},
            )
            return
        report.cancelled += len(cancel)
        entry["placed"] = sum(1 for i in ids if i is not None)

    # -- the card -----------------------------------------------------------

    async def _finish(self, report: PassReport) -> None:
        global _STATUS
        self._passes += 1
        if report.errors:
            self._last_error = report.errors[-1]
            self._last_error_ts = report.ts
        _STATUS = {
            "state": report.state,
            "strategy": STRATEGY,
            "last_pass_ts": report.ts,
            "passes": self._passes,
            "errors": list(report.errors),
            "last_error": self._last_error,
            "last_error_ts": self._last_error_ts,
            "executor": dict(report.executor),
            "bankroll": dict(report.bankroll),
            "dials_version": report.dials_version,
            "fills": report.fills,
            "shares_filled": report.shares_filled,
            "settled": report.settled,
            "learned": report.learned,
            "learn_note": report.learn_note,
            "settle_waiting": dict(report.settle_waiting),
            "cancelled": report.cancelled,
            "assets": copy.deepcopy(report.assets),
        }
        try:
            await _db.set_config(STATUS_KEY, json.dumps(_STATUS, default=str))
        except Exception as exc:  # noqa: BLE001 - the in-memory copy still serves the card
            log.warning("fade1h.status_not_saved", error=f"{type(exc).__name__}: {exc}")

    async def _notify_fills(self, fills: Sequence[_executor.FillEvent]) -> None:
        if not fills:
            return
        parts = [f"{f.window_slug.split('-')[0].upper()} {f.side} {f.shares:.2f} sh at "
                 f"{f.price:g} ({f.kind})" for f in fills[:_MAX_NAMED]]
        more = len(fills) - _MAX_NAMED
        text = "; ".join(parts) + (f"; and {more} more" if more > 0 else "")
        try:
            await _db.notify(FILL_EVENT, f"{LABEL}: paper bids filled: {text}.",
                             {"fills": [dataclasses.asdict(f) for f in fills]})
        except Exception as exc:  # noqa: BLE001 - the ledger has the fills either way
            log.warning("fade1h.notify_failed", error=f"{type(exc).__name__}: {exc}")

    async def _notify_settled(self, settled: Sequence[_ledger.Settlement]) -> None:
        for s in settled:
            if s.filled_shares <= 0:
                continue  # a window nothing filled in: recorded, but not news
            sign = "+" if s.net_pnl >= 0 else "-"
            try:
                await _db.notify(
                    SETTLED_EVENT,
                    f"{LABEL}: {s.window_slug} settled {s.outcome}: net {sign}"
                    f"${abs(s.net_pnl):.2f} on {s.filled_shares:.2f} filled shares "
                    f"(${s.staked_usd:.2f} staked).",
                    {"window_slug": s.window_slug, "outcome": s.outcome,
                     "net_pnl": s.net_pnl, "filled_shares": s.filled_shares},
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("fade1h.notify_failed", error=f"{type(exc).__name__}: {exc}")

    # -- market data --------------------------------------------------------

    def release(self) -> None:
        """Give up this strategy's market-data demand on every hub it used (the app clears
        the current hub before its teardown stops this task, so the last one seen counts)."""
        try:
            current = self._hub_fn()
        except Exception:  # noqa: BLE001
            current = None
        hubs = [h for h in (self._last_hub, current) if h is not None]
        for hub in {id(h): h for h in hubs}.values():
            try:
                hub.release(OWNER)
            except Exception as exc:  # noqa: BLE001
                log.warning("fade1h.release_failed", error=f"{type(exc).__name__}: {exc}")


def _current_hub() -> Any:
    """The process's market-data hub, looked up on every call (None outside the dashboard)."""
    from polymarket_exec.marketdata import hub as md_hub

    return md_hub.current()


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


async def _sleep(stop_event: Any, seconds: float) -> None:
    seconds = max(MIN_SLEEP_S, seconds)
    if stop_event is None:
        await asyncio.sleep(seconds)
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except (TimeoutError, asyncio.TimeoutError):
        pass


def _stopped(stop_event: Any) -> bool:
    return stop_event is not None and stop_event.is_set()


async def run_forever(stop_event: asyncio.Event | None = None) -> None:
    """Run passes until ``stop_event`` is set (or forever if None). Never raises: a failure
    anywhere is logged and shown on the card, and the next pass tries again. A cancel (the
    app's teardown) propagates after the market data is released."""
    global _STATUS
    runner: Runner | None = None
    failed = False
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            runner = Runner(client)
            while not _stopped(stop_event):
                started = time.monotonic()
                try:
                    await runner.pass_once()
                except Exception:  # noqa: BLE001 - pass_once guards itself; belt and braces
                    log.exception("fade1h.pass_failed")
                interval = await read_poll_interval()
                await _sleep(stop_event, interval - (time.monotonic() - started))
    except Exception as exc:  # noqa: BLE001 - never raise out of the task
        failed = True
        log.exception("fade1h.loop_failed")
        _STATUS = {**_STATUS, "last_error": f"The loop stopped: {type(exc).__name__}: {exc}",
                   "last_error_ts": time.time()}
    finally:
        if runner is not None:
            runner.release()
        else:
            try:
                from polymarket_exec.marketdata import hub as md_hub

                md_hub.release(OWNER)
            except Exception:  # noqa: BLE001
                pass
        _STATUS = {**_STATUS, "state": "stopped_on_error" if failed else "stopped"}
