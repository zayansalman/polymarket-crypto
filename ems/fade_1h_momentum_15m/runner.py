"""The Fade 1h Momentum on 15m loop: bookkeeping, inputs, the model hook, sizing and orders.

``run_forever(stop_event)`` runs for the dashboard's lifetime (started in the app lifespan,
after the market-data hub). Paper only: live trading is not authorised for any market, and
this strategy has no live order path. Every order is a scaled passive limit order: one parent
order split into child orders resting at several price levels on the passive side of the touch
(a buy at or under the best bid, a sell at or over the best ask). Nothing crosses the spread.
The strategy never holds both sides of a window: a position is cut by a resting sell of the
shares held, never by buying the other side.

One pass, in the order that keeps the record honest
---------------------------------------------------
1. Setup, once per process: seed the dials (version 0, the prior, and version 1, the starting
   dials fitted on the Sep 17-20 tape: ``learner.seed_starting_dials``), and stop every order a
   previous run left resting (their tape is still read, so fills made before the restart are
   kept). Until this has worked, no new order is placed; bookkeeping runs regardless.
2. Bookkeeping, every pass whatever the switch or the mode says: bring fills up to date from
   the trade tape, then settle every ended window the venue has resolved, traded or not, and
   let the learner take one step on every window just settled (``learner.learn_from_settled``,
   off the event loop; a new dials version per step). Each step is guarded on its own.
3. The executor for this pass, from the operator's PAPER/LIVE selection and the kill switch.
   Anything but PAPER places nothing: LIVE shows "not built / not authorised" on the card,
   resting paper orders are cancelled, and the paper orders already filled keep settling.
4. The strategy switch. Off: cancel resting orders, release the market data, open nothing new.
5. On: read Settings, then the clock again (bookkeeping can take a while, and the inputs, the
   time left and the price ages are measured from this reading), and gather every coin's
   inputs. Before the model runs, the ledger gives each coin's position in its current window
   (the net shares held) and the bankroll the sizing works from.
6. The model (``decide.decide``, off the event loop: about 2,000 simulated paths a coin) prices
   a parent buy order on each side and, with shares held, a resting sell of them, sized in the
   fractional-Kelly account with the shares held; it returns the one that adds the most
   expected log growth, or none.
7. For the coins with an order: buys are shrunk by the joint Kelly of all the coins buying in
   this pass (bets on opposite sides hedge each other and are not shrunk), each buy child order
   is capped at the largest single order, sizes go to the venue's share step and 5-share
   minimum, and the buys as a whole are fitted into the free cash. Then the resting orders are
   brought in line with the plan by ``executor.reconcile``: an order at the same price is kept,
   with its place in the queue, while the plan still wants at least its size; the rest are
   cancelled and the difference placed, in one transaction.
8. Record one ``fade_decisions`` row per coin (its inputs, what the maths said and what was
   done, or why nothing was), and this pass's state for the card (``status()`` and the
   ``STATUS_KEY`` config row; the keys are listed under "The card's view" below). A failure
   that did not stop a coin (its inputs' ``warnings``) is an error of the pass.
9. Sleep for the poll interval from Settings.

The bankroll
------------
The free cash is ``ledger.free_cash_usd``: the starting paper bankroll plus settled P&L, minus
the cash in unsettled windows (what bought shares cost minus what sold shares brought in), minus
the unfilled part of every buy order that could still turn out to have filled (the tape runs
minutes behind), except the resting buy orders of the windows this pass re-plans. Each coin's
model sizes from that cash plus what the positions in the other coins' current windows are
worth at their books' mids (its own window's position is in the Kelly account already). Buys
only ever spend the free cash.

The card's view (``status()``, also saved under ``STATUS_KEY``)
-------------------------------------------------------------
state (running, setting_up, switched_off, paper/live/kill-switch executor states,
no_executor, pass_failed, stopped, stopped_on_error), strategy, last_pass_ts, passes, errors
(every distinct error of the latest pass), last_error, last_error_ts, executor {state,
requested_mode, message, can_place}, bankroll {start_usd, free_usd}, dials_version, fills,
shares_filled, settled, learned, learn_note, settle_waiting {resolution, tape, retry_later,
no_market_id, backlog}, cancelled, and assets {asset: entry}. Each coin's entry: action (one of
the decision row actions below, or switched_off), reason, window_slug, decision_id, notes and
warnings (its inputs'); with a Problem, codes; with a Decision, order_action (buy, sell or
none), side, p, p_model, explanation, orders (each planned child order's record: level, kind,
order_side, side, price, shares, usd, depth_ahead, p_fill, q_fill, optimal_shares), position
(held_side, held_shares, and the sell or a note; None when nothing is held) and sizing
(cash_usd, bankroll_usd, account_cash_usd, joint_scale, fit_scale, buy_usd, sell_shares,
optimal_shares, growth); once the orders were brought in line, kept, placed and cancelled
(counts) and held_back (plain-English reasons an order was held back); error, when the coin
failed. A loop that fails before its first pass in this process keeps its own state and error
and borrows only the last pass time of the run before (``from_earlier_run``), so the card never
shows an earlier run's clean status over a loop that died.

Never raises (a cancel still propagates, which the app's teardown expects): every step is
guarded, and every failure is logged and shown on the card.
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

from ems import db as _db  # type: ignore[import-untyped]
from ems.logging_setup import get_logger
from ems import runtime_knobs as _knobs
from ems import strategies as _strategies
from ems.fade_1h_momentum_15m import decide as _decide
from ems.fade_1h_momentum_15m import executor as _executor
from ems.fade_1h_momentum_15m import inputs as _inputs
from ems.fade_1h_momentum_15m import learner as _learner
from ems.fade_1h_momentum_15m import ledger as _ledger
from ems.fade_1h_momentum_15m import sizing as _sizing
from ems.fade_1h_momentum_15m.decide import (
    ChildOrder, Decision, DecideSettings, NoDecision, ParentOrder,
)
from ems.fade_1h_momentum_15m.inputs import Inputs, Problem

log = get_logger("fade_1h.runner")

STRATEGY = "fade_1h_momentum_15m"
LABEL = "Fade 1h (15m)"
OWNER = _inputs.OWNER
ASSETS = _inputs.ASSETS
SIDES = _decide.SIDES
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
NO_ORDER = "no_order"  # a Decision, but no order pays (or none survives the venue's minimum)
ORDERS = "orders"  # child orders planned (kept or placed)
NOT_PLACED = "not_placed"  # a plan, but this pass may not place orders (LIVE, kill switch...)
REFUSED = "refused"  # follow-up row: the executor refused the plan

# Runner states beyond the executor's, shown on the card.
PASS_FAILED = "pass_failed"  # a pass raised past its own guards
STOPPED = "stopped"
STOPPED_ON_ERROR = "stopped_on_error"

NO_MODEL_REASON = "The model gave no decision for this window: inputs recorded, no orders."
TRADING_STEP_FAILED = "trading_step_failed"  # cancel reason when the trading step fails

MIN_SHARES = _executor.MIN_ORDER_SHARES  # the venue's minimum order on these markets
SHARE_STEP = _executor.SHARE_STEP  # the venue takes sizes in hundredths of a share
_SHARES_EPS = 1e-9
_MAX_NAMED = 5

# The card's view of the runner, replaced whole at the end of every pass.
_STATUS: dict[str, Any] = {"state": "not_started", "last_pass_ts": None, "last_error": None}


def status() -> dict[str, Any]:
    """A copy of the runner's latest state for the card (``STATUS_KEY`` holds the same)."""
    return copy.deepcopy(_STATUS)


async def _save_status() -> None:
    try:
        await _db.set_config(STATUS_KEY, json.dumps(_STATUS, default=str))
    except Exception as exc:  # noqa: BLE001 - the in-memory copy still serves the card
        log.warning("fade1h.status_not_saved", error=f"{type(exc).__name__}: {exc}")


async def _record_failure(message: str, ts: float, state: str) -> None:
    """Put a failure that escaped a pass's own guards on the card (memory and STATUS_KEY).

    Before this process's first pass, the run before's last pass time is borrowed (marked
    ``from_earlier_run``) so the card has it for context, while the state and the error are
    this process's own."""
    global _STATUS
    status = {**_STATUS, "state": state, "last_error": message, "last_error_ts": ts,
              "errors": [message]}
    if not status.get("last_pass_ts"):
        try:
            saved = json.loads(await _db.get_config(STATUS_KEY) or "null")
        except Exception:  # noqa: BLE001 - only the earlier pass time is lost
            saved = None
        if isinstance(saved, dict) and saved.get("last_pass_ts"):
            status.update(last_pass_ts=saved["last_pass_ts"], passes=saved.get("passes"),
                          from_earlier_run=True)
    _STATUS = status
    await _save_status()


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
    band_lo: float  # dollars from the touch to the nearest price level (0 joins it)
    band_hi: float  # ... to the deepest price level
    reduce_positions: bool
    spot_feed: str
    coins: Mapping[str, bool]

    @property
    def decide(self) -> DecideSettings:
        return DecideSettings(spot_feed=self.spot_feed, band_lo=self.band_lo,
                              band_hi=self.band_hi, kelly_multiplier=self.kelly_multiplier,
                              reduce_positions=self.reduce_positions)

    def as_record(self) -> dict[str, Any]:
        return {
            "bankroll_usd": self.bankroll_usd, "kelly_multiplier": self.kelly_multiplier,
            "max_order_usd": self.max_order_usd, "band_lo": self.band_lo,
            "band_hi": self.band_hi, "reduce_positions": self.reduce_positions,
            "spot_feed": self.spot_feed, "coins": dict(self.coins),
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
        band_lo=float(await get("fade1h_levels_near_cents")) / 100.0,
        band_hi=float(await get("fade1h_levels_far_cents")) / 100.0,
        reduce_positions=bool(await get("fade1h_reduce_positions")),
        spot_feed=str(await get("fade1h_spot_feed")),
        coins={a: bool(await get(f"fade1h_trade_{a}")) for a in ASSETS},
    )


# ---------------------------------------------------------------------------
# Sizing: from Decisions to a plan of child orders (pure)
# ---------------------------------------------------------------------------


def _book(inputs: Inputs, side: str) -> Any:
    return inputs.up_book if side == "Up" else inputs.down_book


def _token(inputs: Inputs, side: str) -> str:
    return inputs.up_token if side == "Up" else inputs.down_token


def _round_down(shares: float) -> float:
    """Shares rounded down to the venue's step, and zero below its minimum order."""
    if not (math.isfinite(shares) and shares > 0.0):
        return 0.0
    steps = math.floor(shares / SHARE_STEP + 1e-9)
    n = round(steps * SHARE_STEP, 6)
    return n if n >= MIN_SHARES - _SHARES_EPS else 0.0


@dataclass(frozen=True)
class PlannedOrder:
    """One child order the plan wants resting.

    ``kind`` "entry" buys ``side``'s token; "hedge" sells shares of it already held.
    ``level``: the price level's place in its parent order, 0 nearest the touch.
    ``levels_ahead``: the displayed book on the side it rests on (bids for a buy, asks for a
    sell), as the ledger keeps it: the levels at our price or better. ``optimal_shares``: what
    the maths gave this level before the joint shrink, the caps and the venue's rounding.
    """

    kind: str
    side: str
    token_id: str
    price: float
    shares: float
    level: int
    levels_ahead: tuple[tuple[float, float], ...]
    p_fill: float
    q_fill: float
    optimal_shares: float

    @property
    def order_side(self) -> str:
        return _ledger.ORDER_SIDE_OF_KIND[self.kind]

    @property
    def usd(self) -> float:
        """What the child order costs (a buy) or brings in (a sell) if it all fills."""
        return self.price * self.shares

    @property
    def depth_ahead(self) -> float:
        order_side = self.order_side
        return math.fsum(size for px, size in self.levels_ahead
                         if (px >= self.price - 1e-9 if order_side == "BUY"
                             else px <= self.price + 1e-9))

    def new_order(self, window_slug: str, decision_id: int | None) -> _ledger.NewOrder:
        return _ledger.NewOrder(
            window_slug=window_slug, token_id=self.token_id, side=self.side, kind=self.kind,
            price=self.price, shares=self.shares, level=self.level,
            levels_ahead=self.levels_ahead, decision_id=decision_id,
        )

    def as_record(self) -> dict[str, Any]:
        return {
            "level": self.level, "kind": self.kind, "order_side": self.order_side,
            "side": self.side, "price": self.price, "shares": self.shares,
            "usd": round(self.usd, 6), "depth_ahead": self.depth_ahead,
            "p_fill": self.p_fill, "q_fill": self.q_fill,
            "optimal_shares": self.optimal_shares,
        }


@dataclass
class CoinPlan:
    """What the maths wants resting for one coin this pass, and how it got there."""

    asset: str
    window_slug: str
    action: str = "none"  # buy | sell | none: the Decision's order
    side: str | None = None
    orders: list[PlannedOrder] = field(default_factory=list)
    bankroll_usd: float = 0.0  # the wealth this coin's model sized from
    joint_scale: float = 1.0  # joint Kelly over the coin's own Kelly (buys), 0..1
    fit_scale: float = 1.0  # the share of the buys that fits the free cash, 0..1
    held_side: str | None = None
    held_shares: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def buys(self) -> list[PlannedOrder]:
        return [o for o in self.orders if o.kind == "entry"]

    @property
    def sells(self) -> list[PlannedOrder]:
        return [o for o in self.orders if o.kind == "hedge"]

    @property
    def buy_usd(self) -> float:
        return math.fsum(o.usd for o in self.buys)

    def sizing_record(self, dec: Decision, cash_usd: float) -> dict[str, Any]:
        order = dec.order
        return {
            "cash_usd": cash_usd, "bankroll_usd": self.bankroll_usd,
            "account_cash_usd": dec.account_cash, "joint_scale": self.joint_scale,
            "fit_scale": self.fit_scale, "buy_usd": self.buy_usd,
            "sell_shares": math.fsum(o.shares for o in self.sells),
            "optimal_shares": order.shares if order is not None else 0.0,
            "growth": order.growth if order is not None else 0.0,
        }

    def position_record(self, dec: Decision) -> dict[str, Any] | None:
        """The position held in the window and what the maths does with it, for the card."""
        if self.held_side is None:
            return None
        out: dict[str, Any] = {"held_side": self.held_side, "held_shares": self.held_shares}
        sale = dec.option("sell", self.held_side)
        chosen = self.sells[0] if self.sells else None
        if chosen is not None:
            out["sell"] = chosen.as_record()
        elif sale is not None and sale.paying and dec.order is sale:
            out["note"] = ("The maths would sell "
                           f"{sale.paying[0].shares:.2f} at {sale.paying[0].price:g}, below "
                           f"the venue's minimum order of {MIN_SHARES:g} shares.")
        elif sale is None:
            out["note"] = "Reducing a held position is switched off in Settings."
        elif dec.order is not None and dec.order.action == "buy":
            out["note"] = "Adding to the position pays more than selling any of it."
        else:
            out["note"] = "Keeping the shares pays more than selling them at any price level."
        return out


def _children(order: ParentOrder) -> list[tuple[int, ChildOrder]]:
    """The child orders the maths gives shares, with their level (0 nearest the touch)."""
    return [(i, c) for i, c in enumerate(order.child_orders) if c.shares > _SHARES_EPS]


def plan_orders(
    cases: Mapping[str, tuple[Inputs, Decision]],
    *,
    cash_usd: float,
    bankrolls: Mapping[str, float],
    held: Mapping[str, Mapping[str, float]],
    rho: float,
    settings: Settings,
) -> dict[str, CoinPlan]:
    """Turn every coin's Decision into child orders (pure; run it off the event loop).

    ``cash_usd``: the free cash buys may spend. ``bankrolls``: the wealth each coin's model sized
    from. ``held``: net shares held in each coin's current window, ``{asset: {side: n}}``.

    1. Buys: each coin's parent buy order summarised as one bet (its share-weighted chance of
       winning given a fill, its average price, its side); ``sizing.joint_kelly`` sizes the bets
       together with the copula correlation ``rho`` (a bet on Down loads on the shared factor
       the other way). A coin's buys are scaled by its joint stake over its own Kelly stake;
       joint sizing only ever shrinks. Each child order is capped at the largest single order.
    2. Sells: a resting sell of shares held is sized by ``sizing.reduce_position`` inside the
       model; it only lowers the exposure, so it is neither joint-shrunk nor capped.
    3. Every size is rounded down to the venue's share step, and zero under its minimum order.
    4. If the buys as a whole cost more than the free cash, every buy is scaled down to fit.
    """
    plans: dict[str, CoinPlan] = {}
    buyers: list[str] = []
    for asset, (inp, dec) in cases.items():
        side_held, n_held = None, 0.0
        for side, shares in (held.get(asset) or {}).items():
            if float(shares or 0.0) > _SHARES_EPS:
                side_held, n_held = side, float(shares)
        plan = CoinPlan(asset=asset, window_slug=inp.window_slug, action=dec.action,
                        side=dec.side, bankroll_usd=float(bankrolls.get(asset, cash_usd)),
                        held_side=side_held, held_shares=n_held)
        plans[asset] = plan
        if dec.order is None and settings.band_lo > settings.band_hi + 1e-12:
            plan.notes.append(
                f"The nearest price level ({100 * settings.band_lo:g}c from the touch) is set "
                f"further out than the deepest ({100 * settings.band_hi:g}c), so there is no "
                "price level to rest a child order at. Change the price range in Settings.")
        elif dec.order is None:
            plan.notes.append(
                f"Neither adding to the {n_held:.2f} {side_held} shares held nor selling them "
                "adds expected growth, so no order rests." if side_held is not None else
                "Neither side adds expected growth at any price level in the band, so no "
                "order rests.")
        elif dec.order.action == "buy" and dec.order.bet is not None:
            buyers.append(asset)

    if buyers:
        bets = [cases[a][1].order.bet for a in buyers]  # type: ignore[union-attr]
        joint = _sizing.joint_kelly(bets, rho, 1.0)
        for asset, bet, f_joint in zip(buyers, bets, joint):
            alone = _sizing.kelly_maker(bet.q, bet.price)
            plans[asset].joint_scale = min(1.0, max(0.0, f_joint / alone)) if alone > 0 else 1.0

    for asset, (inp, dec) in cases.items():
        plan, order = plans[asset], dec.order
        if order is None:
            continue
        book = _book(inp, order.side)
        token = _token(inp, order.side)
        if order.action == "buy":
            for level, child in _children(order):
                n = child.shares * plan.joint_scale
                if settings.max_order_usd > 0:
                    n = min(n, settings.max_order_usd / child.price)
                n = _round_down(n)
                if n > 0:
                    plan.orders.append(PlannedOrder(
                        kind="entry", side=order.side, token_id=token, price=child.price,
                        shares=n, level=level, levels_ahead=tuple(book.bids),
                        p_fill=child.p_fill, q_fill=child.q_fill,
                        optimal_shares=child.shares))
        else:
            for level, child in _children(order):
                n = _round_down(min(child.shares, plan.held_shares))
                if n > 0:
                    plan.orders.append(PlannedOrder(
                        kind="hedge", side=order.side, token_id=token, price=child.price,
                        shares=n, level=level, levels_ahead=tuple(book.asks),
                        p_fill=child.p_fill, q_fill=child.q_fill,
                        optimal_shares=child.shares))
        if not plan.orders:
            plan.notes.append(
                f"The maths' order is below the venue's minimum of {MIN_SHARES:g} shares a "
                "child order once the joint sizing and the caps are applied, so nothing rests.")

    total = math.fsum(p.buy_usd for p in plans.values())
    room = max(float(cash_usd), 0.0)
    if total > room + 1e-9:
        factor = room / total
        for plan in plans.values():
            if not plan.buys:
                continue
            plan.fit_scale = factor
            kept = []
            for o in plan.orders:
                if o.kind == "entry":
                    n = _round_down(o.shares * factor)
                    if n <= 0:
                        continue
                    o = dataclasses.replace(o, shares=min(n, o.shares))
                kept.append(o)
            plan.orders = kept
            plan.notes.append(f"The buys were scaled down to {factor:.0%} so they fit the free "
                              f"cash of ${room:.2f}.")
    return plans


def _decide_all(ready: Mapping[str, Inputs], params: Mapping[str, Any],
                settings: DecideSettings, held: Mapping[str, Mapping[str, float]],
                bankrolls: Mapping[str, float]) -> dict[str, Any]:
    """Every ready coin's Decision (or None, or the exception it raised). Runs in a worker
    thread; one coin failing never stops the others."""
    out: dict[str, Any] = {}
    for asset, inputs in ready.items():
        try:
            out[asset] = _decide.decide(inputs, params, settings, held=held.get(asset) or {},
                                        bankroll=bankrolls[asset])
        except Exception as exc:  # noqa: BLE001 - reported per coin by the caller
            out[asset] = exc
    return out


def _held_by_window(positions: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    """Net shares held per window and side, from ``ledger.open_positions()``."""
    out: dict[str, dict[str, float]] = {}
    for row in positions:
        shares = float(row.get("shares") or 0.0)
        if shares > _SHARES_EPS:
            out.setdefault(str(row["window_slug"]), {})[str(row["side"])] = shares
    return out


def bankrolls_for(coins: Mapping[str, Inputs], by_window: Mapping[str, Mapping[str, float]],
                  cash_usd: float) -> dict[str, float]:
    """The wealth each coin's model sizes from: the free cash plus what the positions in the
    OTHER coins' current windows are worth at their side's book mid (the coin's own position
    is in its Kelly account). ``by_window``: net shares held per window and side. Positions in
    ended windows awaiting their result count at zero."""
    marks: dict[str, float] = {}
    for asset, inp in coins.items():
        value = 0.0
        for side, shares in (by_window.get(inp.window_slug) or {}).items():
            value += shares * _book(inp, side).mid
        marks[asset] = value
    total = math.fsum(marks.values())
    return {asset: float(cash_usd) + total - marks[asset] for asset in coins}


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


def _input_notes(asset: str, got: Inputs | Problem, report: PassReport) -> dict[str, Any]:
    """A coin's input notes and warnings for its card entry. A warning is a failure that did
    not stop the coin (a database read or write), so it is also an error of the pass."""
    warnings = [str(w) for w in getattr(got, "warnings", ()) or ()]
    for text in warnings:
        report.errors.append(f"{_coin(asset)}: {text}")
    return {"notes": [str(n) for n in getattr(got, "notes", ()) or ()], "warnings": warnings}


class Runner:
    """One runner per process: keeps the HTTP client, the input memory and the bookkeeper
    (which remembers when each window's result was last looked up) for its lifetime.

    ``clock`` is read at the moment each thing is measured or written: the ledger reads it
    inside its own writes, so an order rests from the second after it is written."""

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
        self.bookkeeper = bookkeeper or _executor.PaperBookkeeper(client, clock=clock)
        self._ready = False
        self._last_hub: Any = None
        self._passes = 0
        self._last_error: str | None = None
        self._last_error_ts: float | None = None

    def _now(self) -> float:
        return float(self._clock())

    # -- the pass -----------------------------------------------------------

    async def pass_once(self) -> PassReport:
        """One guarded pass. Never raises (a cancel propagates)."""
        report = PassReport(ts=self._now())
        await self._setup(report)
        await self._bookkeeping(report)
        choice = await self._choose(report)
        try:
            on = await _strategies.enabled(STRATEGY)
        except Exception as exc:  # noqa: BLE001 - fail closed
            report.fail("Reading the strategy switch", exc)
            on = False
        if not on:
            await self._switched_off(report)
        elif not self._ready:
            report.state = "setting_up"
            report.errors.append("Setup has not finished, so no new orders are placed yet.")
        else:
            if choice is None or not choice.can_place:
                # Inputs are still read and recorded; the card shows why nothing is placed.
                report.state = choice.state if choice is not None else "no_executor"
            try:
                await self._trade(choice, report)
            except Exception as exc:  # noqa: BLE001 - shown on the card
                report.fail("The trading step", exc)
                await self._stand_down_after_failure(report)
        await self._finish(report)
        return report

    async def _stand_down_after_failure(self, report: PassReport) -> None:
        """The trading step failed before it could bring the orders in line: stop them all,
        so none keeps resting at a price the maths no longer stands behind."""
        try:
            report.cancelled += await _ledger.cancel_all_open(
                ts=self._clock, reason=TRADING_STEP_FAILED)
        except Exception as exc:  # noqa: BLE001
            report.fail("Cancelling resting orders", exc)

    async def _setup(self, report: PassReport) -> None:
        if self._ready:
            return
        try:
            dials = await _learner.seed_starting_dials(ts=self._now())
            report.dials_version = int(dials["version"])
            await self.bookkeeper.recover_after_restart()
        except Exception as exc:  # noqa: BLE001 - retried next pass
            report.fail("Setup (dials and restart recovery)", exc)
            return
        self._ready = True

    async def _bookkeeping(self, report: PassReport) -> None:
        try:
            fills = await self.bookkeeper.sync_fills()
        except Exception as exc:  # noqa: BLE001
            report.fail("Checking fills", exc)
        else:
            report.fills = len(fills.fills)
            report.shares_filled = fills.shares_filled
            report.errors.extend(fills.errors)
            await self._notify_fills(fills.fills)
        try:
            settled = await self.bookkeeper.settle()
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
                await self._learn(settled.settled, report)

    async def _learn(self, settled: Sequence[_ledger.Settlement], report: PassReport) -> None:
        """One learning step on the windows just settled (the learner never raises)."""
        try:
            learned = await _learner.learn_from_settled(settled, ts=self._now())
        except Exception as exc:  # noqa: BLE001 - belt and braces
            report.fail("Learning", exc)
            return
        report.learned += learned.windows
        report.errors.extend(learned.errors)
        if learned.version is not None:
            report.dials_version = learned.version
            report.learn_note = learned.note

    async def _choose(self, report: PassReport) -> _executor.ExecutorChoice | None:
        try:
            choice = await _executor.choose_executor(
                self._client, bookkeeper=self.bookkeeper,
                kill_switch_path=self._kill_switch_path, clock=self._clock,
            )
        except Exception as exc:  # noqa: BLE001 - no executor: place nothing
            message = report.fail("Choosing the executor", exc)
            report.executor = {"state": _executor.UNKNOWN_MODE_STATE, "requested_mode": None,
                               "message": f"{message}. No new orders this pass.",
                               "can_place": False}
            try:
                report.cancelled += await _ledger.cancel_all_open(
                    ts=self._clock, reason=_executor.UNKNOWN_MODE_STATE)
            except Exception as exc2:  # noqa: BLE001
                report.fail("Cancelling resting orders", exc2)
            return None
        report.executor = {"state": choice.state, "requested_mode": choice.requested_mode,
                           "message": choice.message, "can_place": choice.can_place}
        if not choice.can_place:
            try:
                report.cancelled += await _executor.stand_down(choice)
            except Exception as exc:  # noqa: BLE001
                report.fail("Cancelling resting orders", exc)
        return choice

    async def _switched_off(self, report: PassReport) -> None:
        report.state = "switched_off"
        try:
            report.cancelled += await _ledger.cancel_all_open(ts=self._clock,
                                                              reason="switched_off")
        except Exception as exc:  # noqa: BLE001
            report.fail("Cancelling resting orders", exc)
        self.release()
        for asset in ASSETS:
            report.assets[asset] = {
                "action": "switched_off",
                "reason": "The strategy is switched off: no new orders. Fills and settlement "
                          "keep running.",
            }

    async def _trade(self, choice: _executor.ExecutorChoice | None,
                     report: PassReport) -> None:
        settings = await read_settings()
        dials = await _ledger.dials() or await _ledger.seed_dials(ts=self._now())
        params = dict(dials.get("params") or {})
        version = int(dials["version"])
        report.dials_version = version
        hub = self._hub_fn()
        if hub is not None:
            self._last_hub = hub
        # The time the inputs, the prices' ages and the time left are measured from: read
        # after the bookkeeping, which can take a while.
        now = self._now()
        gathered = await _inputs.gather(hub, self._client, now, memory=self._memory)
        state = choice.state if choice is not None else _executor.UNKNOWN_MODE_STATE
        can_place = choice is not None and choice.can_place

        simple: dict[str, tuple[str, str, Inputs | Problem]] = {}
        ready: dict[str, Inputs] = {}
        priced: dict[str, Inputs] = {}  # coins with inputs, switched on or not (for marks)
        for asset in ASSETS:
            got = gathered.get(asset)
            if not isinstance(got, (Inputs, Problem)):
                got = Problem(asset=asset, code="internal_error", ts=now,
                              message="No inputs were returned for this coin.")
            if isinstance(got, Problem):
                simple[asset] = (NO_INPUTS, got.message, got)
                continue
            priced[asset] = got
            if not settings.coins.get(asset, True):
                simple[asset] = (COIN_OFF, f"{_coin(asset)} is switched off in Settings: "
                                           "inputs recorded, no orders.", got)
            else:
                ready[asset] = got

        # The position and the bankroll the model sizes from, before it runs.
        resting = await _ledger.open_orders()
        cash: float | None = None
        held: dict[str, dict[str, float]] = {}
        bankrolls: dict[str, float] = {}
        if ready:
            positions = await _ledger.open_positions()
            by_window = _held_by_window(positions)
            held = {a: dict(by_window.get(inp.window_slug) or {}) for a, inp in ready.items()}
            cash = await _ledger.free_cash_usd(
                settings.bankroll_usd, replanned={inp.window_slug for inp in ready.values()})
            bankrolls = bankrolls_for(priced, by_window, cash)
        else:
            try:
                cash = await _ledger.free_cash_usd(settings.bankroll_usd)
            except Exception as exc:  # noqa: BLE001 - only the card's figure is lost
                report.fail("Reading the free cash", exc)
        report.bankroll = {"start_usd": settings.bankroll_usd, "free_usd": cash}

        # The model runs off the event loop (a capped simulation per coin).
        answers = (await asyncio.to_thread(_decide_all, ready, params, settings.decide, held,
                                           bankrolls) if ready else {})
        cases: dict[str, tuple[Inputs, Decision]] = {}
        for asset, got in ready.items():
            decision = answers.get(asset)
            if isinstance(decision, NoDecision):
                simple[asset] = (NO_MODEL, f"The model cannot price this window: {decision} "
                                           "No orders for this coin.", got)
            elif isinstance(decision, BaseException):
                simple[asset] = (MODEL_ERROR, f"The model failed: {type(decision).__name__}: "
                                              f"{decision}. No orders for this coin.", got)
                log.warning("fade1h.model_failed", asset=asset, error=str(decision))
            elif decision is None:
                simple[asset] = (NO_MODEL, NO_MODEL_REASON, got)
            elif not isinstance(decision, Decision):
                simple[asset] = (MODEL_ERROR, "The model returned something other than a "
                                              "Decision. No orders for this coin.", got)
            else:
                cases[asset] = (got, decision)

        plans: dict[str, CoinPlan] = {}
        if cases:
            # Buy orders of ready coins the model gave nothing are cancelled this pass, but
            # their tape is not read yet: they may still turn out to have filled.
            unsure = math.fsum(
                max(0.0, float(o["shares"]) - float(o.get("filled_shares") or 0.0))
                * float(o["price"])
                for o in resting
                if o.get("order_side") == "BUY" and o.get("asset") in ready
                and o.get("asset") not in cases and o.get("window_slug") in
                {inp.window_slug for inp in ready.values()})
            rho = float(params.get("rho", _ledger.PRIOR_DIALS["rho"]))
            try:
                plans = await asyncio.to_thread(
                    plan_orders, cases, cash_usd=(cash or 0.0) - unsure, bankrolls=bankrolls,
                    held=held, rho=rho, settings=settings)
            except Exception as exc:  # noqa: BLE001 - record the inputs anyway, place nothing
                message = report.fail("Sizing", exc)
                plans = {asset: CoinPlan(asset=asset, window_slug=inp.window_slug,
                                         notes=[f"{message}. No orders this pass."])
                         for asset, (inp, _) in cases.items()}

        for asset in ASSETS:
            mine = [o for o in resting if o.get("asset") == asset]
            try:
                if asset in simple:
                    action, reason, got = simple[asset]
                    await self._record_simple(now, asset, action, reason, got, settings,
                                              state, version, report)
                    await self._cancel(choice, [int(o["id"]) for o in mine], action, report)
                else:
                    inp, dec = cases[asset]
                    await self._act(now, asset, inp, dec, plans[asset], mine, settings,
                                    cash or 0.0, choice, state, can_place, version, report)
            except Exception as exc:  # noqa: BLE001 - one coin must not stop the others
                message = report.fail(f"{_coin(asset)}", exc)
                report.assets.setdefault(asset, {})["error"] = message

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
                                 "window_slug": got.window_slug, "decision_id": decision_id,
                                 **_input_notes(asset, got, report)}
        if isinstance(got, Problem):
            entry["codes"] = list(got.codes)
        report.assets[asset] = entry

    async def _cancel(self, choice: _executor.ExecutorChoice | None, ids: list[int],
                      reason: str, report: PassReport) -> None:
        """Cancel a coin's resting orders. When this pass may not place orders they were all
        stood down already."""
        if not ids or choice is None or choice.executor is None:
            return
        report.cancelled += await choice.executor.cancel(ids, reason=reason)

    async def _act(self, now: float, asset: str, inp: Inputs, dec: Decision, plan: CoinPlan,
                   resting: list[dict], settings: Settings, cash: float,
                   choice: _executor.ExecutorChoice | None, state: str, can_place: bool,
                   version: int, report: PassReport) -> None:
        if not plan.orders:
            action = NO_ORDER
            reason = " ".join(plan.notes) or "The maths gives no order at any price level."
        elif not can_place:
            action = NOT_PLACED
            reason = (choice.message if choice is not None
                      else "No executor this pass, so no new orders.")
        else:
            action, reason = ORDERS, (" ".join(plan.notes) or None)
        record = inp.as_record()
        record["settings"] = settings.as_record()
        position = plan.position_record(dec)
        wealth = plan.bankroll_usd
        decision_id = await _ledger.record_decision(
            ts=now, asset=asset, inputs=record, action=action, window_slug=inp.window_slug,
            mode=state, dials_version=version, p_model=dec.p_model, p=dec.p,
            side=dec.side,
            kelly_f=plan.buy_usd / wealth if wealth > 0 else None,
            stake_usd=plan.buy_usd,
            child_orders=[o.as_record() for o in plan.orders], hedge=position,
            factors={"model": dict(dec.factors), "sizing": plan.sizing_record(dec, cash),
                     "order": {"action": dec.action, "side": dec.side},
                     "explanation": dec.explanation},
            reason=reason,
        )
        entry: dict[str, Any] = {
            "action": action, "reason": reason, "window_slug": inp.window_slug,
            "decision_id": decision_id, "order_action": dec.action, "side": dec.side,
            "p": dec.p, "p_model": dec.p_model, "explanation": dec.explanation,
            "orders": [o.as_record() for o in plan.orders], "position": position,
            "sizing": plan.sizing_record(dec, cash), **_input_notes(asset, inp, report),
        }
        report.assets[asset] = entry
        if not can_place or choice is None or choice.executor is None:
            return
        wanted = [o.new_order(inp.window_slug, decision_id) for o in plan.orders]
        # Every resting order of this coin: the ones in another window are not wanted and are
        # cancelled in the same transaction.
        try:
            done = await choice.executor.reconcile(
                wanted, resting,
                best_asks={inp.up_token: inp.up_book.best_ask,
                           inp.down_token: inp.down_book.best_ask},
                best_bids={inp.up_token: inp.up_book.best_bid,
                           inp.down_token: inp.down_book.best_bid},
                cancel_reason="requote",
            )
        except _executor.PlacementRefused as exc:
            message = f"The orders were refused ({exc.reason}): {exc}"
            entry.update(action=REFUSED, reason=message, kept=0, placed=0, cancelled=0)
            report.errors.append(f"{_coin(asset)}: {message}")
            await _ledger.record_decision(
                ts=now, asset=asset, window_slug=inp.window_slug, mode=state,
                dials_version=version, action=REFUSED, reason=message,
                inputs={"status": "refused", "decision_id": decision_id},
            )
            return
        result = done.result
        placed = sum(1 for i in result.ids if i is not None)
        report.cancelled += result.cancelled
        entry.update(kept=len(done.changes.keep), placed=placed, cancelled=result.cancelled)
        if result.held_back:
            entry["held_back"] = [text for _, text in
                                  (result.held_back[i] for i in sorted(result.held_back))]

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
            "errors": list(dict.fromkeys(report.errors)),
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
        await _save_status()

    async def _notify_fills(self, fills: Sequence[_executor.FillEvent]) -> None:
        if not fills:
            return
        parts = [f"{f.window_slug.split('-')[0].upper()} "
                 f"{'sold' if f.order_side == 'SELL' else 'bought'} {f.shares:.2f} {f.side} at "
                 f"{f.price:g}" for f in fills[:_MAX_NAMED]]
        more = len(fills) - _MAX_NAMED
        text = "; ".join(parts) + (f"; and {more} more" if more > 0 else "")
        try:
            await _db.notify(FILL_EVENT, f"{LABEL}: paper orders filled: {text}.",
                             {"fills": [dataclasses.asdict(f) for f in fills]})
        except Exception as exc:  # noqa: BLE001 - the ledger has the fills either way
            log.warning("fade1h.notify_failed", error=f"{type(exc).__name__}: {exc}")

    async def _notify_settled(self, settled: Sequence[_ledger.Settlement]) -> None:
        for s in settled:
            if s.filled_shares <= 0:
                continue  # a window nothing filled in: recorded, but not news
            sign = "+" if s.net_pnl >= 0 else "-"
            sold = (f", {s.sold_shares:.2f} sold for ${s.sale_proceeds_usd:.2f}"
                    if s.sold_shares > 0 else "")
            try:
                await _db.notify(
                    SETTLED_EVENT,
                    f"{LABEL}: {s.window_slug} settled {s.outcome}: net {sign}"
                    f"${abs(s.net_pnl):.2f} on {s.filled_shares:.2f} shares bought "
                    f"(${s.staked_usd:.2f}){sold}.",
                    {"window_slug": s.window_slug, "outcome": s.outcome,
                     "net_pnl": s.net_pnl, "filled_shares": s.filled_shares,
                     "sold_shares": s.sold_shares},
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
    from ems.marketdata import hub as md_hub

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
    anywhere is logged and shown on the card (a pass that fails past its own guards, and a
    loop that cannot start or dies, both land in ``status()`` and ``STATUS_KEY``), and the next
    pass tries again. A cancel (the app's teardown) propagates after the market data is
    released."""
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
                except Exception as exc:  # noqa: BLE001 - pass_once guards itself; belt and braces
                    log.exception("fade1h.pass_failed")
                    await _record_failure(
                        f"A pass failed before it finished: {type(exc).__name__}: {exc}",
                        time.time(), PASS_FAILED)
                interval = await read_poll_interval()
                await _sleep(stop_event, interval - (time.monotonic() - started))
    except Exception as exc:  # noqa: BLE001 - never raise out of the task
        failed = True
        log.exception("fade1h.loop_failed")
        await _record_failure(f"The loop stopped: {type(exc).__name__}: {exc}", time.time(),
                              STOPPED_ON_ERROR)
    finally:
        if runner is not None:
            runner.release()
        _STATUS = {**_STATUS, "state": STOPPED_ON_ERROR if failed else STOPPED}
