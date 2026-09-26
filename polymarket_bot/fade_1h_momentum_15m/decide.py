"""The model hook for Fade 1h Momentum on 15m: one coin's inputs in, a :class:`Decision` out.

``decide(inputs, dials, settings, held=..., bankroll=...)`` prices the coin's current 15m
window with the TWAP-settled model (``model.py``: a window settles Up iff the Chainlink TWAP-60s
print at its close is at least the print at its open), follows the market with the dials'
anchor weights, and chooses what to rest: a scaled passive limit order (one parent order split
into child orders resting at several price levels) that buys Up, one that buys Down, a resting
sell of shares already held, or nothing. Every candidate is priced by a Monte Carlo of the
model's own paths and sized by the Kelly maths; the one that adds the most expected log growth
wins. Pure and standard library only (no numpy or scipy); the runner calls it off the event
loop.

The steps, with the maths each one uses
---------------------------------------
1. The model's chance of Up, ``p_model`` (``model.prob_up_twap`` with the 60 s average). The
   price now, ``X(t)``, is the live Chainlink price (or Binance, per the operator's spot feed),
   measured against the start reference ``x0`` (the TWAP-60s print at the open, Gamma's
   ``priceToBeat``). The TWAP-60s prints are only the settlement's two ends: that start
   reference, and in the last 60 s the part of the closing average already printed (the mean
   of the Chainlink prints since ``window_end - 60``). The TWAP-60s print now is not the price
   now: it is a 60 s average that runs about 30 s behind. On top of the leg so far come the
   snap-back (the stretch of the last twelve 15m candles, pulled back at speed
   ``kappa0 e^{-lam t}``) and the momentum (``theta`` times the blend of the 1h market's
   implied drift and the spot's own trailing hour), over the noise left.
2. The chance traded on, ``p = sizing.anchor(p_model, m, w_M, w_S)``: probit-space weights on
   the 15m market's own Up mid ``m`` and on the model (section 1 of
   ``tasks/2026-09-22-fade-1h-sizing.md``).
3. The candidate orders. With nothing held in the window: a parent buy order on each side,
   its child orders at the price levels of :func:`buy_price_levels` (the operator's band under
   that side's best bid, so a buy never crosses the spread and never sits on the far side of
   the mid). With shares held on one side: more of that side, or a resting sell of them at a
   price level of :func:`sell_price_levels` (the band over that side's best ask). The other
   side is never bought while one side is held: the log-optimal position with cash available
   is one side plus cash, so a position is cut by selling it.
4. The fill simulation (:func:`simulate_fills`): ``MC_PATHS`` paths of the fitted process
   (seeded per window, so a pass is reproducible and passes in one window share their noise):
   - each path draws its drift from N(mu, v) and runs section 1's exact transition on a grid
     (every 10 s, every 5 s inside the closing minute), tracking the closing average;
   - the market's price along the path is the crowd's view (a drift and Brownian noise, with
     the TWAP settlement) calibrated to equal the market's mid now, as in section 6 of the
     research doc. A resting buy at ``b`` fills when that price for its side comes down to
     ``b``; a resting sell at ``s`` when it comes up to ``s``. Between grid points the crossing
     is caught by the Brownian-bridge extreme of the step (sampled exactly), not only at the
     grid points, so a path that dips through a level and comes back inside a step fills it;
   - at the fill the chance is re-evaluated there, with the crowd exactly at the level and the
     model's state at the crossing: ``Phi(w_M z_market + w_S z_model)``. Being filled means the
     price moved against the order, so that adverse selection is inside ``q_fill``, not a
     haircut. With the market-only dials (``w_M = 1``, ``w_S = 0``) every level's ``q_fill`` is
     its own price, so nothing pays. Nothing fills at once: every level is on the passive side
     of the mid, and it fills only when the market moves to it;
   - where a deeper level comes out filling more often than the win chances at the fills
     allow, its fill chance is trimmed to fit (:func:`buy_quotes`); no win chance is changed.
5. Sizing (``sizing.py``), in a fractional-Kelly account (``sizing.kelly_cash``: the Kelly
   multiplier times the wealth, holding the shares already bought):
   - a buy: ``sizing.scaled_limits`` on the side's levels, counting the shares of that side
     already held (so a position already at its size gets nothing more);
   - a sell: ``sizing.reduce_position`` at each sell level with the chance the held side still
     wins given that sale fills; the price is the level where the fill chance times the gain
     (``sizing.sale_log_growth``) is largest.
   The candidate that adds the most expected log growth to the account is the order, and
   nothing rests when none adds anything. Each candidate also reports what it adds to the
   operator's own money (the bankroll's cash with the shares held, as they settle): that is
   the figure the card and the records show. The account's own figure only chooses: when the
   position is worth more than the multiplier's share of the wealth, the account's cash sits
   at its floor, near zero, and a sale's growth there is the log of that near-zero cash. A
   sale that only brings the position back to the multiplier's size gives up a little of the
   operator's growth for less risk, so its reported growth is below 0.
6. The factor waterfall for the card, and one sentence in a trader's words.

What the runner does with it
----------------------------
The Decision carries the chosen parent order with the log-optimal shares at each price level
(for this coin alone, in the Kelly account). The runner shrinks buys by the joint Kelly of the
coins decided in the same pass (``ParentOrder.bet`` gives the one-bet summary with its side,
for ``sizing.joint_kelly``), caps each child order at the largest single order, rounds to the
venue's share step and minimum, and fits the whole plan into the free cash. There is no price
rule or threshold: the side, the prices and the sizes all come out of the maths, and each is
zero when it does not pay.

Contract: pure, standard library only; raises :class:`NoDecision` (with a plain-English reason)
when the inputs cannot be priced, and ValueError on dials, settings or holdings that are not
usable. ``p_model`` and ``p`` are chances the window settles Up; a child order's ``q_fill`` is
the chance its own side wins given that it fills.
"""

from __future__ import annotations

import math
import random
import time
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from polymarket_bot.fade_1h_momentum_15m import model as _model
from polymarket_bot.fade_1h_momentum_15m import sizing as _sizing

if TYPE_CHECKING:  # the hook only reads Inputs; importing it at runtime is not needed
    from polymarket_bot.fade_1h_momentum_15m.inputs import Inputs

SIDES = ("Up", "Down")
ACTIONS = ("buy", "sell")
# The price now for the model: the live Chainlink price, or Binance to compare.
SPOT_FEEDS = ("chainlink", "binance")
# Earlier setting names, read as the feed they meant to be. The TWAP-60s print is the
# settlement's reference, never the price now, so an old "chainlink_twap60" reads Chainlink.
LEGACY_SPOT_FEEDS: Mapping[str, str] = MappingProxyType({"chainlink_twap60": "chainlink"})

# The price levels' step: whole cents, or the book's tick when that is coarser. The band is
# set in cents, and a cent step keeps a 0-15c band at 16 levels even where the venue's tick is
# a tenth of a cent near 0 and 1 (a whole-cent step from the touch stays on a finer tick grid).
LEVEL_STEP = 0.01
_PRICE_EPS = 1e-9

# The fill simulation: paths per decision, and the grid (seconds) outside and inside the
# closing minute, whose average settles the window.
MC_PATHS = 2000
MC_STEP_S = 10.0
MC_CLOSE_STEP_S = 5.0
AVG_S = 60.0
HOUR_S = 3600.0

_P_EPS = 1e-12  # probabilities handed on stay strictly inside (0, 1)
_Z_CAP = 7.0  # |z| beyond this is certainty to 1e-12
_SHARE_EPS = 1e-9  # shares below this are none

# The dials the model reads, and the keys of the blend moments (per sigma^2) with their
# defaults (model.BLEND_*), which the prior dials do not carry.
DIAL_KEYS = ("w_M", "w_S", "theta", "kappa0", "lam", "alpha", "c", "rho")
BLEND_KEYS = {"blend_HH": _model.BLEND_HH, "blend_LL": _model.BLEND_LL,
              "blend_HL": _model.BLEND_HL}


class NoDecision(Exception):
    """The inputs cannot be priced (the message says why, in plain English)."""


def _probability(name: str, value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc
    if not (math.isfinite(v) and 0.0 < v < 1.0):
        raise ValueError(f"{name} must be strictly between 0 and 1, got {value!r}")
    return v


def _side(name: str, value: Any) -> str:
    if value not in SIDES:
        raise ValueError(f"{name} must be one of {SIDES}, got {value!r}")
    return str(value)


def _other(side: str) -> str:
    return "Down" if side == "Up" else "Up"


def spot_feed_name(feed: str) -> str:
    """The spot feed a setting names (``SPOT_FEEDS``), reading an earlier name as the feed it
    meant (``LEGACY_SPOT_FEEDS``). ValueError for anything else."""
    name = LEGACY_SPOT_FEEDS.get(feed, feed)
    if name not in SPOT_FEEDS:
        raise ValueError(f"spot_feed must be one of {SPOT_FEEDS}, got {feed!r}")
    return name


@dataclass(frozen=True)
class ChildOrder:
    """One child order of a parent order: a resting limit order at one price level.

    ``price``: where it rests. ``p_fill``: the chance it fills before the window ends (for a
    buy, cumulative: a deeper level fills only after every nearer one, so ``p_fill`` falls with
    depth). ``q_fill``: the chance the side it trades wins given that it fills (for a buy the
    side bought, for a sell the side sold; being filled means the price moved against the
    order, so it is usually worse than the plain chance). ``shares``: the log-optimal shares at
    this level for this coin alone, before the venue's rounding and the runner's caps; 0 where
    the maths gives the level nothing.
    """

    price: float
    p_fill: float
    q_fill: float
    shares: float = 0.0

    def __post_init__(self) -> None:
        _probability("price", self.price)
        _probability("p_fill", self.p_fill)
        _probability("q_fill", self.q_fill)
        shares = float(self.shares)
        if not (math.isfinite(shares) and shares >= 0.0):
            raise ValueError(f"shares must be zero or more, got {self.shares!r}")
        object.__setattr__(self, "shares", shares)

    @property
    def usd(self) -> float:
        """What the child order costs (a buy) or brings in (a sell) if it all fills."""
        return self.price * self.shares

    def as_record(self) -> dict[str, float]:
        return {"price": self.price, "p_fill": self.p_fill, "q_fill": self.q_fill,
                "shares": self.shares}


@dataclass(frozen=True)
class ParentOrder:
    """One parent order: a buy or a sell of one side, split into child orders at price levels.

    ``action``: "buy" or "sell". ``side``: the token bought or sold. ``child_orders``: every
    price level priced, nearest to the touch first, each with its log-optimal ``shares`` (0
    where it does not pay). A sell carries shares at one level at most: the level where the
    fill chance times the gain is largest.

    Two growth figures, fills and no fills weighted by their chances, both 0 for an order with
    no shares:

    - ``account_growth``: the expected log growth the order adds to the Kelly account
      (``sizing.kelly_cash``), the objective the sizes maximise. The order rests only when it
      is above 0, and of several candidates the largest is the order. It is never shown: when
      the position is worth more than the Kelly multiplier's share of the wealth, the account's
      cash sits at its floor, near zero, and this is the log of that near-zero cash. None
      (the default) means the same as ``growth``, which it is when the multiplier is 1.
    - ``growth``: the expected log growth the order adds to the operator's own money: the
      bankroll's cash with the shares already held, paid out as they settle. This is the
      figure for the card and the records. A buy adds growth. A sale adds growth when the odds
      have turned; a sale that only brings the position back to the Kelly multiplier's size
      gives up a little growth for less risk, and its ``growth`` is below 0.
    """

    action: str
    side: str
    child_orders: tuple[ChildOrder, ...] = ()
    growth: float = 0.0
    account_growth: float | None = None

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"action must be one of {ACTIONS}, got {self.action!r}")
        _side("side", self.side)
        children = tuple(self.child_orders)
        if not all(isinstance(c, ChildOrder) for c in children):
            raise ValueError("child_orders must be ChildOrder values")
        growth = float(self.growth)
        if not math.isfinite(growth):
            raise ValueError(f"growth must be a finite number, got {growth!r}")
        account = growth if self.account_growth is None else float(self.account_growth)
        if not (math.isfinite(account) and account >= 0.0):
            raise ValueError(
                f"account_growth must be a finite number, zero or more, got {account!r}")
        object.__setattr__(self, "child_orders", children)
        object.__setattr__(self, "growth", growth)
        object.__setattr__(self, "account_growth", account)

    @property
    def paying(self) -> tuple[ChildOrder, ...]:
        """The child orders the maths gives shares, nearest first."""
        return tuple(c for c in self.child_orders if c.shares > _SHARE_EPS)

    @property
    def shares(self) -> float:
        return math.fsum(c.shares for c in self.child_orders)

    @property
    def usd(self) -> float:
        """What the parent order costs (a buy) or brings in (a sell) if it all fills."""
        return math.fsum(c.usd for c in self.child_orders)

    @property
    def average_price(self) -> float | None:
        shares = self.shares
        return self.usd / shares if shares > _SHARE_EPS else None

    @property
    def q_bar(self) -> float | None:
        """The share-weighted chance the side wins given a fill."""
        shares = self.shares
        if shares <= _SHARE_EPS:
            return None
        return min(1.0, max(0.0, math.fsum(c.shares * c.q_fill for c in self.child_orders)
                            / shares))

    @property
    def bet(self) -> _sizing.Bet | None:
        """The parent buy order as one bet for ``sizing.joint_kelly`` (its share-weighted win
        chance, its average price and its side); None for a sell (a sale only lowers the
        exposure) and for an order with no shares."""
        if self.action != "buy":
            return None
        price, q = self.average_price, self.q_bar
        if price is None or q is None:
            return None
        return _sizing.Bet(q, price, 1.0, self.side)

    def as_record(self) -> dict[str, Any]:
        return {"action": self.action, "side": self.side, "growth": self.growth,
                "child_orders": [c.as_record() for c in self.child_orders]}


@dataclass(frozen=True)
class Decision:
    """What the model says about one coin's current 15m window.

    - ``p_model``: the model's own chance that the window settles Up (TWAP-60s settlement).
    - ``p``: the chance traded on: the market anchor of ``p_model`` and the 15m market's price,
      ``sizing.anchor(p_model, market_up, w_M, w_S)`` with the dials' weights.
    - ``order``: the parent order to rest (the candidate that adds the most expected log
      growth to the Kelly account, ``ParentOrder.account_growth``), or None when none adds
      anything.
    - ``options``: every candidate parent order priced, with the growth it adds to the
      operator's own money (``ParentOrder.growth``), for the card and the record (why this
      side and not the other).
    - ``held_side``, ``held_shares``: the net position held in the window when deciding.
    - ``account_cash``: the Kelly account's cash the sizes were worked out in
      (``sizing.kelly_cash``). It sits at its floor, near zero, when the position is worth
      more than the Kelly multiplier's share of the wealth (factors ``held_usd`` and
      ``kelly_account_usd``).
    - ``factors``: numbers for the card: the waterfall (leg so far, snap-back, momentum, market
      anchor) in z units and in points of probability, and the model's internals.
    - ``explanation``: one plain-English sentence for the card.
    """

    p_model: float
    p: float
    order: ParentOrder | None = None
    options: tuple[ParentOrder, ...] = ()
    held_side: str | None = None
    held_shares: float = 0.0
    account_cash: float | None = None
    factors: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))
    explanation: str = ""

    def __post_init__(self) -> None:
        _probability("p_model", self.p_model)
        _probability("p", self.p)
        if self.order is not None and not isinstance(self.order, ParentOrder):
            raise ValueError("order must be a ParentOrder or None")
        options = tuple(self.options)
        if not all(isinstance(o, ParentOrder) for o in options):
            raise ValueError("options must be ParentOrder values")
        if self.held_side is not None:
            _side("held_side", self.held_side)
        held = float(self.held_shares)
        if not (math.isfinite(held) and held >= 0.0):
            raise ValueError(f"held_shares must be zero or more, got {self.held_shares!r}")
        if self.held_side is None and held > 0.0:
            raise ValueError("held_shares without a held_side")
        order = self.order
        if order is not None and self.held_side is not None:
            if order.side != self.held_side:
                raise ValueError("while one side is held the order must trade that side")
        if order is not None and order.action == "sell" and self.held_side is None:
            raise ValueError("a sell needs shares held")
        factors: dict[str, float] = {}
        for name, value in dict(self.factors).items():
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"factor {name!r} is not a finite number")
            factors[str(name)] = number
        object.__setattr__(self, "options", options)
        object.__setattr__(self, "held_shares", held)
        object.__setattr__(self, "factors", MappingProxyType(factors))
        object.__setattr__(self, "explanation", str(self.explanation or ""))

    @property
    def side(self) -> str | None:
        """The side the order trades, or None when nothing rests."""
        return self.order.side if self.order is not None else None

    @property
    def action(self) -> str:
        """"buy", "sell" or "none"."""
        return self.order.action if self.order is not None else "none"

    def option(self, action: str, side: str) -> ParentOrder | None:
        """The candidate parent order for ``action`` on ``side``, if it was priced."""
        return next((o for o in self.options if o.action == action and o.side == side), None)

    def as_record(self) -> dict[str, Any]:
        """A JSON-safe dict of the choice: the order, every candidate and the position."""
        return {
            "p_model": self.p_model, "p": self.p, "action": self.action, "side": self.side,
            "held_side": self.held_side, "held_shares": self.held_shares,
            "account_cash": self.account_cash,
            "order": self.order.as_record() if self.order is not None else None,
            "options": [o.as_record() for o in self.options],
        }


@dataclass(frozen=True)
class DecideSettings:
    """The operator's settings the model reads (the runner fills these from Settings).

    ``spot_feed``: the price now for the model, one of ``SPOT_FEEDS`` (an earlier name is read
    as the feed it meant, ``LEGACY_SPOT_FEEDS``). ``band_lo`` and ``band_hi``: the band of
    price levels, in dollars from the touch: buys from ``band_lo`` to ``band_hi`` under the
    side's best bid (0 joins the best bid), sells the same distance over its best ask.
    ``paths``: simulated paths per decision. ``kelly_multiplier``: the operator's risk dial
    (``sizing.kelly_cash``). ``reduce_positions``: whether a held position may be cut by a
    resting sell.
    """

    spot_feed: str = "chainlink"
    band_lo: float = 0.01
    band_hi: float = 0.15
    paths: int = MC_PATHS
    kelly_multiplier: float = 0.5
    reduce_positions: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "spot_feed", spot_feed_name(self.spot_feed))
        for name in ("band_lo", "band_hi"):
            value = float(getattr(self, name))
            if not (math.isfinite(value) and value >= 0.0):
                raise ValueError(f"{name} must be zero or more, got {value!r}")
        if int(self.paths) < 1:
            raise ValueError(f"paths must be at least 1, got {self.paths!r}")
        k = float(self.kelly_multiplier)
        if not (math.isfinite(k) and 0.0 <= k <= 1.0):
            raise ValueError(f"kelly_multiplier must be between 0 and 1, got {k!r}")


DEFAULT_SETTINGS = DecideSettings()


def _band_offsets(tick: float, band_lo: float, band_hi: float) -> tuple[float, range] | None:
    step = max(float(tick), LEVEL_STEP)
    lo, hi = float(band_lo), float(band_hi)
    if not (math.isfinite(step) and step > 0.0 and math.isfinite(lo) and math.isfinite(hi)):
        return None
    if hi < lo or lo < 0.0:
        return None
    first = max(0, math.ceil(lo / step - _PRICE_EPS))
    last = math.floor(hi / step + _PRICE_EPS)
    return step, range(first, last + 1)


def buy_price_levels(best_bid: float, tick: float, band_lo: float,
                     band_hi: float, *, best_ask: float | None = None) -> tuple[float, ...]:
    """The price levels of a parent buy order on one side: from ``band_lo`` to ``band_hi``
    dollars under that side's ``best_bid``, nearest first, above zero. A buy at or under the
    best bid rests in the book (it never crosses the spread) and sits on the passive side of
    the mid, so it fills only when the market comes down to it.

    ``best_ask``: that side's own best ask. A level at or above it would meet the ask (a
    locked book, bid = ask, puts the best bid there), so it is not a resting order and is
    left out.

    The step is ``LEVEL_STEP`` (a cent) or ``tick`` when that is coarser. Prices are rounded to
    6 decimals so they compare equal to the same price computed elsewhere. Empty when the band
    is empty (``band_hi < band_lo``) or the best bid is not a price.
    """
    bid = float(best_bid)
    grid = _band_offsets(tick, band_lo, band_hi)
    if grid is None or not (math.isfinite(bid) and 0.0 < bid < 1.0):
        return ()
    ask = _touch(best_ask)
    step, offsets = grid
    out = []
    for j in offsets:
        price = round(bid - j * step, 6)
        if price <= _PRICE_EPS:
            break
        if ask is not None and price >= ask - _PRICE_EPS:
            continue
        out.append(price)
    return tuple(out)


def sell_price_levels(best_ask: float, tick: float, band_lo: float,
                      band_hi: float, *, best_bid: float | None = None) -> tuple[float, ...]:
    """The price levels of a resting sell of one side: from ``band_lo`` to ``band_hi`` dollars
    over that side's ``best_ask``, nearest first, below one. The mirror of
    :func:`buy_price_levels` (a sale at ``s`` pays like a buy of the other side at ``1 - s``):
    it never crosses the spread and fills only when the market comes up to it. ``best_bid``:
    that side's own best bid; a level at or below it would meet the bid and is left out."""
    ask = float(best_ask)
    grid = _band_offsets(tick, band_lo, band_hi)
    if grid is None or not (math.isfinite(ask) and 0.0 < ask < 1.0):
        return ()
    bid = _touch(best_bid)
    step, offsets = grid
    out = []
    for j in offsets:
        price = round(ask + j * step, 6)
        if price >= 1.0 - _PRICE_EPS:
            break
        if bid is not None and price <= bid + _PRICE_EPS:
            continue
        out.append(price)
    return tuple(out)


def _touch(price: float | None) -> float | None:
    """The other side of the book as a price, or None when there is none to meet."""
    if price is None:
        return None
    value = float(price)
    return value if math.isfinite(value) else None


# ---------------------------------------------------------------------------
# What the model reads: from live Inputs, or from a recorded decision row
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelState:
    """One coin's window as the model sees it (hours, log returns; see ``inputs.Inputs``)."""

    asset: str
    ts: float  # decision time, epoch seconds
    window_slug: str
    window_start: float
    window_end: float
    hour_start: float
    d: float  # ln(price now on the spot feed / start reference)
    close_abar: float | None  # mean ln price over the closing minute so far minus ln(start ref)
    m: float  # the 15m market's Up mid
    m_H: float  # the 1h market's Up mid
    x: float  # the hour's move so far on Binance (the 1h market's settlement basis)
    sigma: float  # per sqrt(hour), from the last 60 one-minute returns
    mu_l: float  # the trailing hour's return
    r15: tuple[float, ...]  # the 12 completed 15m candles before the window, newest first
    spot_feed: str = "chainlink"

    @property
    def t(self) -> float:
        """Hour-time of the decision, 0..1."""
        return (self.ts - self.hour_start) / HOUR_S

    @property
    def h(self) -> float:
        """Hours left in the window."""
        return (self.window_end - self.ts) / HOUR_S


def _num(name: str, value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise NoDecision(f"The {name} is not a number ({value!r}).") from exc
    if not math.isfinite(v):
        raise NoDecision(f"The {name} is not a finite number ({value!r}).")
    return v


def _log_ratio(name: str, a: Any, b: Any) -> float:
    a, b = _num(name, a), _num(name, b)
    if a <= 0.0 or b <= 0.0:
        raise NoDecision(f"The {name} needs two positive prices, got {a!r} and {b!r}.")
    return math.log(a / b)


def _vol(returns: Sequence[Any]) -> tuple[float, float]:
    rs = [_num("minute return", r) for r in returns]
    if not rs:
        raise NoDecision("No one-minute returns, so the volatility is not known.")
    return math.sqrt(math.fsum(r * r for r in rs)), math.fsum(rs)


def state_from_inputs(inputs: Inputs, spot_feed: str = "chainlink") -> ModelState:
    """The model's view of live :class:`~polymarket_bot.fade_1h_momentum_15m.inputs.Inputs`.

    ``d`` is the price now on ``spot_feed`` (the live Chainlink price, or Binance) against the
    start reference; the TWAP-60s print now is never read (module docstring, step 1)."""
    feed = spot_feed_name(spot_feed)
    now = inputs.chainlink if feed == "chainlink" else inputs.binance
    sigma, mu_l = _vol(inputs.minute_returns)
    close = None if inputs.close_avg is None else (
        inputs.close_avg.log_value - math.log(inputs.start_ref))
    return ModelState(
        asset=inputs.asset, ts=float(inputs.ts), window_slug=inputs.window_slug,
        window_start=float(inputs.window_start), window_end=float(inputs.window_end),
        hour_start=float(inputs.hour_start),
        d=_log_ratio("price against the start reference", now.value, inputs.start_ref),
        close_abar=close, m=float(inputs.up_book.mid),
        m_H=0.5 * (float(inputs.hour_up_bid) + float(inputs.hour_up_ask)),
        x=_log_ratio("hour's move", inputs.binance.value, inputs.hour_open),
        sigma=sigma, mu_l=mu_l, r15=tuple(float(r) for r in inputs.r15), spot_feed=feed,
    )


def state_from_record(record: Mapping[str, Any], spot_feed: str | None = None) -> ModelState:
    """The model's view of a recorded ``Inputs.as_record()`` (a ``fade_decisions`` row's
    inputs), so the learner can re-price a past decision under new dials. The spot feed is
    the one the row was decided with (its ``settings``), unless given; a row decided with the
    TWAP-60s print as the price now is re-priced on the Chainlink price it also recorded."""
    if record.get("status") != "ok":
        raise NoDecision("The row has no complete inputs.")
    feed = spot_feed or str((record.get("settings") or {}).get("spot_feed") or "chainlink")
    try:
        feed = spot_feed_name(feed)
    except ValueError as exc:
        raise NoDecision(f"Unknown spot feed {feed!r}.") from exc
    start_ref = _num("start reference", record.get("start_ref"))
    close = record.get("close_avg")
    close_abar = None
    if isinstance(close, Mapping) and close.get("log_value") is not None:
        close_abar = _num("closing average", close["log_value"]) - math.log(start_ref)
    up = record.get("up_book") or {}
    sigma, mu_l = _vol(record.get("minute_returns") or ())
    return ModelState(
        asset=str(record.get("asset")), ts=_num("decision time", record.get("ts")),
        window_slug=str(record.get("window_slug")),
        window_start=_num("window start", record.get("window_start")),
        window_end=_num("window end", record.get("window_end")),
        hour_start=_num("hour start", record.get("hour_start")),
        d=_log_ratio("price against the start reference",
                     (record.get(feed) or {}).get("value"), start_ref),
        close_abar=close_abar,
        m=0.5 * (_num("Up bid", up.get("best_bid")) + _num("Up ask", up.get("best_ask"))),
        m_H=0.5 * (_num("1h Up bid", record.get("hour_up_bid"))
                   + _num("1h Up ask", record.get("hour_up_ask"))),
        x=_log_ratio("hour's move", (record.get("binance") or {}).get("value"),
                     record.get("hour_open")),
        sigma=sigma, mu_l=mu_l,
        r15=tuple(_num("15m candle return", r) for r in (record.get("r15") or ())),
        spot_feed=feed,
    )


def resolve_dials(dials: Mapping[str, Any]) -> dict[str, float]:
    """The dials the model reads, as floats. The blend moments fall back to the research's
    Sep 17-20 values when a dial set does not carry them (the prior does not)."""
    out: dict[str, float] = {}
    for key in DIAL_KEYS:
        if key not in dials:
            raise ValueError(f"the dials have no {key!r}")
        value = float(dials[key])
        if not math.isfinite(value):
            raise ValueError(f"dial {key!r} is not a finite number")
        out[key] = value
    for key, default in BLEND_KEYS.items():
        value = float(dials.get(key, default))
        out[key] = value if math.isfinite(value) else default
    if out["c"] <= 0.0:
        raise ValueError("dial 'c' must be positive")
    if out["kappa0"] < 0.0:
        raise ValueError("dial 'kappa0' must not be negative")
    return out


# ---------------------------------------------------------------------------
# The model's view: p_model, p and the waterfall
# ---------------------------------------------------------------------------


def _clip_z(z: float) -> float:
    return max(-_Z_CAP, min(_Z_CAP, z))


def _probit(p: float) -> float:
    return _model.ndtri(min(max(p, _P_EPS), 1.0 - _P_EPS))


def _inside(p: float) -> float:
    return min(max(p, _P_EPS), 1.0 - _P_EPS)


@dataclass(frozen=True)
class View:
    """The model's numbers for one state under one dial set."""

    p_model: float
    p: float
    z_model: float  # (known + snapback + momentum) / sd, capped at +-7
    z_market: float  # probit of the market's Up mid
    parts: _model.ZParts
    M: float
    dM_dalpha: float
    dM_dc: float
    mu: float
    v: float
    mu_H: float
    w_H: float
    abar: float  # the closing average so far used (d when it is not known)
    abar_known: bool

    @property
    def z(self) -> float:
        """The anchored z: w_M z_market + w_S z_model (``p = Phi(z)``)."""
        return _model.ndtri(self.p)

    def waterfall(self) -> dict[str, float]:
        """Points of Up probability from each cause, in order: leg so far (from 1/2), then
        the snap-back, the momentum and the market anchor. They add up to ``p - 1/2``."""
        sd = self.parts.sd
        if sd <= 0.0:
            p0 = p1 = self.p_model
        else:
            p0 = _model.ndtr(_clip_z(self.parts.known / sd))
            p1 = _model.ndtr(_clip_z((self.parts.known + self.parts.snapback) / sd))
        return {"leg_pts": 100.0 * (p0 - 0.5), "snapback_pts": 100.0 * (p1 - p0),
                "momentum_pts": 100.0 * (self.p_model - p1),
                "anchor_pts": 100.0 * (self.p - self.p_model)}


def evaluate(state: ModelState, dials: Mapping[str, float]) -> View:
    """p_model and p for one state (steps 1 and 2 of the module docstring)."""
    if not (math.isfinite(state.sigma) and state.sigma > 0.0):
        raise NoDecision("The price did not move in the last hour, so its volatility is zero "
                         "and the window cannot be priced.")
    h, t = state.h, state.t
    if not h > 0.0:
        raise NoDecision("The window has ended.")
    if not 0.0 <= t < 1.0:
        raise NoDecision("The decision time is outside the window's hour.")
    if len(state.r15) != _model.N_LAGS:
        raise NoDecision(f"{len(state.r15)} completed 15m candles, {_model.N_LAGS} needed.")
    if not 0.0 < state.m < 1.0:
        raise NoDecision("The 15m market's mid is not a price.")
    st = _model.stretch_and_grad(state.r15, dials["alpha"], dials["c"])
    s2 = state.sigma * state.sigma
    vH, vL, cHL = s2 * dials["blend_HH"], s2 * dials["blend_LL"], s2 * dials["blend_HL"]
    mu_H = (_model.mu_hat_H(state.m_H, state.x, state.sigma, t)
            if 0.0 < state.m_H < 1.0 else math.nan)
    mu, v = _model.blend(mu_H, state.mu_l, vH, vL, cHL)
    v = max(v, 0.0)
    eps, _ = _model.avg_split(h)
    known = state.close_abar is not None
    abar = state.close_abar if known else state.d  # the closing minute's prints not held
    parts = _model.twap_parts(abar, state.d, st.M, mu, v, t, h, state.sigma, dials["theta"],
                              dials["kappa0"], dials["lam"], _model.AVG_60S)
    z_model = _clip_z(parts.z) if parts.sd > 0.0 else math.copysign(_Z_CAP, parts.num + 0.0)
    p_model = _inside(_model.ndtr(z_model))
    p = _inside(_sizing.anchor(p_model, state.m, dials["w_M"], dials["w_S"]))
    return View(p_model=p_model, p=p, z_model=z_model, z_market=_probit(state.m), parts=parts,
                M=st.M, dM_dalpha=st.dM_dalpha, dM_dc=st.dM_dc, mu=mu, v=v, mu_H=mu_H,
                w_H=(_model.blend_weight(vH, vL, cHL) if math.isfinite(mu_H) else 0.0),
                abar=abar, abar_known=known or eps <= 0.0)


# ---------------------------------------------------------------------------
# The fill simulation
# ---------------------------------------------------------------------------


@dataclass
class FillTable:
    """Per level, keyed by the market's Up price at which it fills: the paths that filled it,
    and the sum over those paths of the anchored Up chance at the fill. ``falls``: levels that
    fill when the market's Up price comes down to the key (Up buys at the key, Down sells at
    one minus it). ``rises``: levels that fill when it comes up to the key (Down buys at one
    minus the key, Up sells at the key). ``n_paths`` in all; a level the market's price
    already reaches now is not a resting order and is left out."""

    n_paths: int
    falls: dict[float, tuple[int, float]]
    rises: dict[float, tuple[int, float]]
    steps: int = 0


@lru_cache(maxsize=8192)
def _step(t: float, dt: float, kappa0: float, lam: float) -> tuple[float, float, float]:
    """(e^-K, G, sqrt(V1)) of section 1's exact transition over [t, t + dt] (hours)."""
    m = _model.ou_unit_moments(t, dt, kappa0, lam)
    return 1.0 - m.A, m.G, math.sqrt(max(m.V1, 0.0))


def _grid(now: float, window_start: float, window_end: float, step_s: float = MC_STEP_S,
          close_step_s: float = MC_CLOSE_STEP_S) -> list[float]:
    """Seconds from now to just before the window end: every ``step_s`` on the window's own
    clock, every ``close_step_s`` inside the closing minute (its start is a grid point)."""
    close = window_end - AVG_S
    points: set[float] = set()
    s = window_start + (math.floor((now - window_start) / step_s) + 1) * step_s
    while s < close - 1e-6:
        points.add(s)
        s += step_s
    if close > now:
        points.add(close)
    k = max(0, math.floor((now - close) / close_step_s) + 1)
    s = close + k * close_step_s
    while s < window_end - 1e-6:
        points.add(s)
        s += close_step_s
    return [now] + sorted(p for p in points if p > now + 1e-6)


def _seed(state: ModelState) -> int:
    """One seed per coin and window: passes in the same window draw the same noise."""
    return zlib.crc32(f"{state.asset}|{state.window_slug}".encode())


def simulate_fills(state: ModelState, dials: Mapping[str, float], view: View,
                   falls: Sequence[float], rises: Sequence[float], *,
                   paths: int = MC_PATHS, seed: int | None = None,
                   step_s: float = MC_STEP_S,
                   close_step_s: float = MC_CLOSE_STEP_S) -> FillTable:
    """Step 4 of the module docstring: which resting orders fill on which simulated paths,
    and the anchored Up chance at each fill.

    ``falls``: the market Up prices at which a level fills on the way down; ``rises``: on the
    way up (see :class:`FillTable`). The crowd prices the window with a drift ``mu_c`` and
    Brownian noise under the same TWAP settlement, ``z_c = (known + mu_c Gbar_B) / (sigma
    sqrt(Psi_B))``, with ``mu_c`` set so that ``z_c`` now is the market's mid. A level at
    ``x`` fills in a step when the step's Brownian-bridge minimum (maximum, for a rise) of
    ``z_c`` reaches ``Phi^-1(x)``: the bridge extreme between the step's two ends is drawn
    exactly by inversion, ``min = (a + b - sqrt((a - b)^2 - 2 var ln U)) / 2``. At the fill
    the crowd is at ``Phi^-1(x)`` and the model's state is interpolated to the crossing, so
    the chance is evaluated where the order fills, not at the grid point after it."""
    n_paths = int(paths)
    theta, kappa0, lam = dials["theta"], dials["kappa0"], dials["lam"]
    w_M, w_S = dials["w_M"], dials["w_S"]
    sigma, M, d0 = state.sigma, view.M, state.d
    h0 = state.h
    eps0, _ = _model.avg_split(h0)
    gb0, psib0 = _model.brownian_twap(h0)
    known0 = view.parts.known
    z_mkt = view.z_market
    mu_c = (sigma * math.sqrt(psib0) * z_mkt - known0) / gb0 if gb0 > 0.0 else 0.0

    # Nearest first: falls from the highest price down, rises from the lowest up. A level the
    # market's price reaches already is not a resting order; it is left out.
    mid = state.m
    down_x = sorted({round(float(x), 6) for x in falls
                     if 0.0 < x < 1.0 and x < mid - _PRICE_EPS}, reverse=True)
    up_x = sorted({round(float(x), 6) for x in rises
                   if 0.0 < x < 1.0 and x > mid + _PRICE_EPS})
    down_thr = [_probit(x) for x in down_x]
    up_thr = [_probit(x) for x in up_x]
    n_dn, n_up = len(down_x), len(up_x)
    dn_n, dn_s = [0] * n_dn, [0.0] * n_dn
    up_n, up_s = [0] * n_up, [0.0] * n_up

    grid = _grid(state.ts, state.window_start, state.window_end, step_s, close_step_s)
    close_start = state.window_end - AVG_S

    def point(s: float) -> tuple[float, float, float, float, float, float]:
        """(ell, A, A C, alpha, beta, gamma) at grid time s: z_c = (integ + ell d) A + A C,
        and the model's z at the fill with the crowd at thr is alpha thr + beta + gamma drift."""
        t_s = (s - state.hour_start) / HOUR_S
        h_s = (state.window_end - s) / HOUR_S
        mom = _model.twap_unit_moments(t_s, h_s, kappa0, lam, _model.AVG_60S)
        gb, psib = _model.brownian_twap(h_s)
        a = 1.0 / (sigma * math.sqrt(psib))
        c = mu_c * gb
        inv_msd = 1.0 / (sigma * math.sqrt(mom.Psi))
        return (mom.ell, a, a * c, inv_msd / a, (-c - mom.B * M) * inv_msd,
                mom.Gbar * inv_msd)

    first = point(state.ts)
    rows = []
    for i in range(1, len(grid)):
        s_prev, s = grid[i - 1], grid[i]
        dt = (s - s_prev) / HOUR_S
        t_prev = (s_prev - state.hour_start) / HOUR_S
        decay, g_step, sd_unit = _step(t_prev, dt, kappa0, lam)
        in_close = s_prev >= close_start - 1e-6  # this step is inside the closing minute
        ell, a, ac, alpha, beta, gamma = point(s)
        sd_d = sigma * sd_unit
        # Variance of z_c's move over the step (d's noise, through the known part).
        sd_z = sd_d * a * (ell + (0.5 * dt if in_close else 0.0))
        rows.append((decay, g_step, sd_d, in_close, dt, ell, a, ac, 2.0 * sd_z * sd_z,
                     alpha, beta, gamma))
    table = FillTable(n_paths=n_paths, falls={}, rises={}, steps=len(rows))
    if rows and (n_dn or n_up):
        rng = random.Random(_seed(state) if seed is None else seed)
        gauss, uniform = rng.gauss, rng.random
        log, sqrt, ndtr = math.log, math.sqrt, _model.ndtr
        sq_v = math.sqrt(view.v) if view.v > 0.0 else 0.0
        integ0 = eps0 * view.abar if eps0 > 0.0 else 0.0
        inf = math.inf
        _, _, _, alpha0, beta0, gamma0 = first
        for _ in range(n_paths):
            drift = theta * (view.mu + sq_v * gauss(0.0, 1.0) if sq_v else view.mu)
            z_dev = M  # X - a, the stretch still to be pulled back
            d_prev = d0
            integ = integ0
            zp = z_mkt
            ap, bp, gp = alpha0, beta0, gamma0
            idn, iup = 0, 0
            thr_d = down_thr[0] if n_dn else -inf
            thr_u = up_thr[0] if n_up else inf
            for (decay, g_step, sd_d, in_close, dt, ell, a, ac, kv, alpha, beta,
                 gamma) in rows:
                z_dev = decay * z_dev + drift * g_step + sd_d * gauss(0.0, 1.0)
                d_now = d0 + (z_dev - M)
                if in_close:
                    integ += 0.5 * (d_prev + d_now) * dt
                d_prev = d_now
                zc = (integ + ell * d_now) * a + ac
                # One uniform per step gives the bridge's minimum and maximum; each is exact
                # on its own, and every candidate order reads only one of them.
                spread = zp - zc
                reach = sqrt(spread * spread - kv * log(1.0 - uniform()))
                low = 0.5 * (zp + zc - reach)
                if low <= thr_d:
                    while idn < n_dn and low <= down_thr[idn]:
                        thr = down_thr[idn]
                        dp, dn = zp - thr, zc - thr
                        u = dp / (dp - dn) if dn <= 0.0 else dp / (dp + dn)
                        zm = ((1.0 - u) * (ap * thr + bp + gp * drift)
                              + u * (alpha * thr + beta + gamma * drift))
                        dn_n[idn] += 1
                        dn_s[idn] += ndtr(w_M * thr + w_S * zm)
                        idn += 1
                    thr_d = down_thr[idn] if idn < n_dn else -inf
                high = low + reach
                if high >= thr_u:
                    while iup < n_up and high >= up_thr[iup]:
                        thr = up_thr[iup]
                        dp, dn = thr - zp, thr - zc
                        u = dp / (dp - dn) if dn <= 0.0 else dp / (dp + dn)
                        zm = ((1.0 - u) * (ap * thr + bp + gp * drift)
                              + u * (alpha * thr + beta + gamma * drift))
                        up_n[iup] += 1
                        up_s[iup] += ndtr(w_M * thr + w_S * zm)
                        iup += 1
                    thr_u = up_thr[iup] if iup < n_up else inf
                if idn >= n_dn and iup >= n_up:
                    break
                zp, ap, bp, gp = zc, alpha, beta, gamma
    table.falls = {x: (n, s) for x, n, s in zip(down_x, dn_n, dn_s)}
    table.rises = {x: (n, s) for x, n, s in zip(up_x, up_n, up_s)}
    return table


def _market_key(action: str, side: str, price: float) -> tuple[str, float]:
    """Where a level sits in the FillTable: (direction, the market's Up price at its fill)."""
    buy_up_or_sell_down = (action == "buy") == (side == "Up")
    x = price if side == "Up" else 1.0 - price
    return ("falls" if buy_up_or_sell_down else "rises"), round(x, 6)


def buy_quotes(side: str, prices: Sequence[float], table: FillTable) -> list[ChildOrder]:
    """The price levels of a parent buy order on ``side`` from the simulation: nearest first,
    ``p_fill`` and ``q_fill`` (the side wins given the fill), made consistent with each other.
    ``shares`` are 0: sizing comes after.

    ``q_fill`` is the anchored chance at each level's own fill, averaged over the paths that
    fill it: the calibrated chance at the moment that child order fills. The sizing also needs
    "exactly the first k child orders filled, then won", ``P_k q_k - P_k+1 q_k+1``, to lie in
    ``[0, P_k - P_k+1]``. The anchored chance mixes the market's view with the model's, so it
    does not move exactly as the model's paths do, and a deeper level can come out filling
    more often than those chances allow. Where it does, the deeper level's fill chance is
    trimmed to the most they allow (``P_k+1 <= P_k (1 - q_k) / (1 - q_k+1)`` and
    ``P_k+1 <= P_k q_k / q_k+1``), nearest level first; no level's win chance is changed, so
    no level gains or loses edge from it. A level the simulation left out (the market's price
    already reaches it) and every level past the first that never fills are dropped."""
    n_paths = table.n_paths
    rows: list[tuple[float, float, float]] = []
    for b in sorted({round(float(p), 6) for p in prices}, reverse=True):
        where, x = _market_key("buy", side, b)
        got = getattr(table, where).get(x)
        if got is None:
            continue
        n, s_up = got
        if n <= 0:
            break  # deeper levels never fill either
        wins = s_up if side == "Up" else n - s_up
        q = _inside(min(max(wins, 0.0), float(n)) / n)
        p_fill = min(n / n_paths, 1.0 - _P_EPS)
        if rows:
            p_near, q_near = rows[-1][1], rows[-1][2]
            p_fill = min(p_fill, p_near * (1.0 - q_near) / (1.0 - q), p_near * q_near / q)
        if p_fill <= _P_EPS:
            break
        rows.append((b, p_fill, q))
    return [ChildOrder(b, p_fill, q) for b, p_fill, q in rows]


def sell_quotes(side: str, prices: Sequence[float], table: FillTable) -> list[ChildOrder]:
    """The price levels of a resting sell of ``side`` from the simulation: nearest first, the
    chance each fills and the chance ``side`` still wins given that it fills. ``shares`` are
    0: sizing comes after. Levels that never fill are dropped."""
    out = []
    for price in sorted({round(float(p), 6) for p in prices}):
        where, x = _market_key("sell", side, price)
        got = getattr(table, where).get(x)
        if got is None or got[0] <= 0:
            continue
        n, s_up = got
        q_up = s_up / n
        out.append(ChildOrder(price, min(n / table.n_paths, 1.0 - _P_EPS),
                              _inside(q_up if side == "Up" else 1.0 - q_up)))
    return out


# ---------------------------------------------------------------------------
# Sizing each candidate, and the choice
# ---------------------------------------------------------------------------


def _position(held: Mapping[str, Any] | None) -> tuple[str | None, float, float]:
    """(net side held or None, net shares, paired shares). Shares of both sides held together
    pay $1 a pair whatever happens, so they count as cash; only the net shares are at risk."""
    shares = {}
    for side in SIDES:
        value = float((held or {}).get(side) or 0.0)
        if not (math.isfinite(value) and value >= 0.0):
            raise ValueError(f"shares held on {side} must be zero or more, got {value!r}")
        shares[side] = value
    up, down = shares["Up"], shares["Down"]
    net = up - down
    if abs(net) <= _SHARE_EPS:
        return None, 0.0, min(up, down)
    return ("Up" if net > 0 else "Down"), abs(net), min(up, down)


def _own_cash(cash: float, bankroll: float | None) -> float:
    """The cash an order's reported growth is measured on: the operator's own (``bankroll``),
    or the Kelly account's when it is not given (the same thing with a multiplier of 1)."""
    if bankroll is None:
        return cash
    own = float(bankroll)
    if not (math.isfinite(own) and own > 0.0):
        raise ValueError(f"bankroll must be a positive finite number, got {bankroll!r}")
    return own


def buy_option(side: str, prices: Sequence[float], table: FillTable, cash: float,
               held: float = 0.0, mark: float | None = None, *,
               bankroll: float | None = None) -> ParentOrder:
    """A parent buy order on ``side`` sized in a Kelly account with ``cash`` holding ``held``
    shares of ``side`` (marked at ``mark``): ``sizing.scaled_limits`` at full Kelly on that
    account (the multiplier is already in the account's cash). Its ``account_growth`` is what
    those stakes add to the account; its ``growth`` is what they add to the operator's own
    ``bankroll`` of cash holding the same shares (``sizing.scaled_limits_log_growth`` on each)."""
    quotes = buy_quotes(side, prices, table)
    if not quotes or not cash > 0.0:
        return ParentOrder("buy", side, tuple(quotes), 0.0, 0.0)
    levels = [(c.price, c.p_fill, c.q_fill) for c in quotes]
    stakes = _sizing.scaled_limits(levels, cash, 1.0, held=held, mark=mark)
    # A level whose q_fill is its own price up to float rounding comes out with a stake of
    # float noise; below a share it is no order, and its growth is none either.
    stakes = [x if x / c.price > _SHARE_EPS else 0.0 for c, x in zip(quotes, stakes)]
    children = tuple(ChildOrder(c.price, c.p_fill, c.q_fill, x / c.price)
                     for c, x in zip(quotes, stakes))
    if not any(x > 0.0 for x in stakes):
        return ParentOrder("buy", side, children, 0.0, 0.0)
    account = _sizing.scaled_limits_log_growth(levels, cash, stakes, held=held)
    growth = _sizing.scaled_limits_log_growth(levels, _own_cash(cash, bankroll), stakes,
                                              held=held)
    return ParentOrder("buy", side, children, growth, max(account, 0.0))


def sell_option(side: str, prices: Sequence[float], table: FillTable, cash: float,
                held: float, *, bankroll: float | None = None) -> ParentOrder:
    """A resting sell of ``held`` shares of ``side``: at each sell level the log-optimal
    shares in the Kelly account with ``cash`` (``sizing.reduce_position`` with the chance
    ``side`` wins given the sale fills), and the level whose fill chance times the account's
    gain (``sizing.sale_log_growth``) is largest. Its ``account_growth`` is that fill chance
    times gain; its ``growth`` is the same sale's fill chance times what it adds to the
    operator's own ``bankroll`` of cash holding the ``held`` shares."""
    quotes = sell_quotes(side, prices, table)
    best, best_gain, best_x = -1, 0.0, 0.0
    for i, c in enumerate(quotes):
        x = _sizing.reduce_position(c.q_fill, c.price, cash, held)
        if x <= _SHARE_EPS:
            continue
        gain = c.p_fill * _sizing.sale_log_growth(c.q_fill, c.price, cash, held, x)
        if gain > best_gain:
            best, best_gain, best_x = i, gain, x
    children = tuple(ChildOrder(c.price, c.p_fill, c.q_fill, best_x if i == best else 0.0)
                     for i, c in enumerate(quotes))
    if best < 0:
        return ParentOrder("sell", side, children, 0.0, 0.0)
    sale = quotes[best]
    growth = sale.p_fill * _sizing.sale_log_growth(sale.q_fill, sale.price,
                                                   _own_cash(cash, bankroll), held, best_x)
    return ParentOrder("sell", side, children, growth, best_gain)


# ---------------------------------------------------------------------------
# The card: factors and one sentence
# ---------------------------------------------------------------------------


def _cents(price: float) -> str:
    c = 100.0 * price
    return f"{c:.0f}c" if abs(c - round(c)) < 1e-6 else f"{c:.1f}c"


def _left(h_hours: float) -> str:
    seconds = h_hours * HOUR_S
    if seconds < 90:
        return f"{max(1, round(seconds))} s left"
    return f"{round(seconds / 60)} min left"


def _pts(x: float) -> str:
    n = abs(x)
    return f"{n:.0f} point{'s' if round(n) != 1 else ''}" if n >= 0.95 else f"{n:.1f} points"


def _shares_text(n: float) -> str:
    return f"{n:.0f}" if abs(n - round(n)) < 0.05 else f"{n:.1f}"


def _prices_text(prices: Sequence[str]) -> str:
    if len(prices) == 1:
        return prices[0]
    if len(prices) <= 4:
        return ", ".join(prices[:-1]) + f" and {prices[-1]}"
    return f"{prices[0]} down to {prices[-1]} ({len(prices)} price levels)"


def _action_text(order: ParentOrder | None, options: Sequence[ParentOrder],
                 held_side: str | None, held: float, held_usd: float = 0.0,
                 kelly_usd: float | None = None) -> str:
    holding = (f"we hold {_shares_text(held)} {held_side} shares" if held_side else "")
    if order is None:
        priced = [o for o in options if o.child_orders]
        if held_side:
            return f"{holding}; neither adding to them nor selling them pays, so nothing rests"
        if priced:
            return ("no price level under either side's best bid wins often enough when it "
                    "fills to pay, so nothing rests")
        return "no price level in the band gets filled in the simulation, so nothing rests"
    paying = order.paying
    near = paying[0]
    odds = f"filled at {_cents(near.price)}, {order.side} wins {100 * near.q_fill:.0f}% of the time"
    if order.action == "sell":
        still = (f"{order.side} still wins {100 * near.q_fill:.0f}% of the time if that sale "
                 f"fills")
        offer = (f"the maths offers {_shares_text(near.shares)} of them at "
                 f"{_cents(near.price)} ({still})")
        if kelly_usd is not None and held_usd > kelly_usd:
            return (f"{holding}, worth ${held_usd:,.2f} at the mid, more than the Kelly "
                    f"multiplier's share of the bankroll (${kelly_usd:,.2f}), so {offer}")
        if order.growth < 0.0:
            return (f"{holding}, more than the Kelly multiplier's size at these odds, so "
                    f"{offer}: the sale gives up a little expected growth for less risk")
        return (f"{holding}, and selling {_shares_text(near.shares)} of them at "
                f"{_cents(near.price)} pays more than keeping them, so the maths offers them "
                f"there ({still})")
    prices = _prices_text([_cents(c.price) for c in paying])
    if len(paying) == 1:
        article = "an" if order.side == "Up" else "a"
        what = f"{article} {order.side} buy order at {prices}"
    else:
        what = f"a scaled {order.side} buy order, child orders at {prices}"
    lead = f"{holding}, and the maths adds" if held_side else "the maths rests"
    return f"{lead} {what} ({odds})"


def story(state: ModelState, view: View, order: ParentOrder | None,
          options: Sequence[ParentOrder] = (), held_side: str | None = None,
          held_shares: float = 0.0, *, held_usd: float = 0.0,
          kelly_usd: float | None = None) -> str:
    """One sentence for the card, in a trader's words. ``held_usd``: the shares held at their
    side's mid. ``kelly_usd``: the Kelly multiplier's share of the wealth (cash plus that)."""
    coin = state.asset.upper()
    leg = 100.0 * state.d
    if abs(leg) < 0.005:
        part_leg = f"{coin}'s 15m leg is flat against its start with {_left(state.h)}"
    else:
        part_leg = (f"{coin}'s 15m leg is {'up' if leg > 0 else 'down'} {abs(leg):.2f}% "
                    f"with {_left(state.h)}")
    w = view.waterfall()
    snap = w["snapback_pts"]
    if abs(snap) < 0.5:
        part_snap = "the snap-back from the last candles barely moves it"
    else:
        ran = "ran up" if view.M > 0 else "sold off"
        part_snap = (f"the last candles {ran} so the snap-back "
                     f"{'takes' if snap < 0 else 'adds'} {_pts(snap)} "
                     f"{'off' if snap < 0 else 'to'} Up")
    mom = w["momentum_pts"]
    if abs(mom) < 0.5:
        part_mom = "the hour's momentum adds little"
    else:
        part_mom = (f"the hour's momentum {'adds' if mom > 0 else 'takes'} {_pts(mom)} "
                    f"{'to' if mom > 0 else 'off'} Up")
    pm, m = view.p_model, state.m
    if abs(pm - m) < 0.01:
        part_mkt = f"the market has Up at {_cents(m)}, where the model has it too"
    elif (pm - 0.5) * (m - 0.5) > 0 and abs(m - 0.5) >= 0.6 * abs(pm - 0.5):
        part_mkt = (f"the market already prices most of it (Up at {_cents(m)} against the "
                    f"model's {100 * pm:.0f}%), so the chance traded on is {100 * view.p:.0f}%")
    else:
        part_mkt = (f"the market has Up at {_cents(m)} against the model's {100 * pm:.0f}%, "
                    f"so the chance traded on is {100 * view.p:.0f}%")
    part_act = _action_text(order, options, held_side, held_shares, held_usd, kelly_usd)
    return "; ".join((part_leg, part_snap, part_mom, part_mkt, part_act)) + "."


def _factors(state: ModelState, dials: Mapping[str, float], view: View, paths: int, steps: int,
             ms: float, options: Sequence[ParentOrder], account_cash: float,
             held_shares: float, held_usd: float = 0.0,
             kelly_usd: float | None = None) -> dict[str, float]:
    parts = view.parts
    sd = parts.sd if parts.sd > 0.0 else math.nan
    out = {
        "p_model": view.p_model, "p": view.p, "market_up": state.m,
        "leg_z": parts.known / sd, "snapback_z": parts.snapback / sd,
        "momentum_z": parts.momentum / sd, "model_z": view.z_model,
        "anchor_z": view.z - view.z_model,
        **view.waterfall(),
        "leg_pct": 100.0 * state.d, "minutes_left": 60.0 * state.h, "sigma": state.sigma,
        "stretch_M": view.M, "mu": view.mu, "v": view.v, "mu_L": state.mu_l, "w_H": view.w_H,
        "B": parts.moments.B, "Gbar": parts.moments.Gbar, "Psi": parts.moments.Psi,
        "w_M": dials["w_M"], "w_S": dials["w_S"], "theta": dials["theta"],
        "kappa0": dials["kappa0"], "lam": dials["lam"],
        "paths": float(paths), "steps": float(steps), "sim_ms": ms,
        "closing_average_known": 1.0 if view.abar_known else 0.0,
        "account_cash": account_cash, "held_shares": held_shares, "held_usd": held_usd,
    }
    if kelly_usd is not None:
        out["kelly_account_usd"] = kelly_usd
    for o in options:
        out[f"growth_{o.action}_{o.side.lower()}"] = o.growth
    if math.isfinite(view.mu_H):
        out["mu_H"] = view.mu_H
    return {k: v for k, v in out.items() if isinstance(v, (int, float)) and math.isfinite(v)}


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------


def decide(
    inputs: Inputs, dials: Mapping[str, Any], settings: DecideSettings = DEFAULT_SETTINGS, *,
    held: Mapping[str, float] | None = None, bankroll: float | None = None,
) -> Decision:
    """The model's view of one coin's window and the parent order it rests
    (module docstring).

    ``inputs``: this pass's :class:`~polymarket_bot.fade_1h_momentum_15m.inputs.Inputs` for
    the coin. ``dials``: the newest dial set's parameters (``ledger.dials()["params"]``).
    ``settings``: the operator's model settings. ``held``: shares already filled in this
    window, ``{"Up": n, "Down": n}`` (either may be missing). ``bankroll``: the wealth the
    sizing works from, apart from this window's own position (free cash plus what open
    positions in other windows are worth); needed when shares are held, and 1 (sizes per
    dollar) when omitted. Raises :class:`NoDecision` when the window cannot be priced (no
    volatility, the window over, a missing candle).
    """
    params = resolve_dials(dials)
    state = state_from_inputs(inputs, settings.spot_feed)
    view = evaluate(state, params)
    held_side, n_held, paired = _position(held)
    if bankroll is None:
        if held_side is not None or paired > 0.0:
            raise ValueError("a bankroll is needed to size against shares already held")
        W = 1.0
    else:
        W = float(bankroll)
        if not math.isfinite(W):
            raise ValueError(f"bankroll must be a finite number, got {bankroll!r}")

    books = {"Up": inputs.up_book, "Down": inputs.down_book}
    tick = float(inputs.tick_size)
    lo, hi = settings.band_lo, settings.band_hi
    buy_sides = SIDES if held_side is None else (held_side,)
    buys = {sd: buy_price_levels(books[sd].best_bid, tick, lo, hi,
                                 best_ask=books[sd].best_ask) for sd in buy_sides}
    sells: dict[str, tuple[float, ...]] = {}
    if held_side is not None and settings.reduce_positions:
        sells[held_side] = sell_price_levels(books[held_side].best_ask, tick, lo, hi,
                                             best_bid=books[held_side].best_bid)
    falls = [*buys.get("Up", ()), *(1.0 - s for s in sells.get("Down", ()))]
    rises = [*(1.0 - b for b in buys.get("Down", ())), *sells.get("Up", ())]

    started = time.perf_counter()
    table = simulate_fills(state, params, view, falls, rises, paths=settings.paths)
    ms = 1000.0 * (time.perf_counter() - started)

    # The Kelly account: the multiplier times the wealth, holding the shares already bought.
    cash = W + paired
    mark = float(books[held_side].mid) if held_side is not None else None
    k = float(settings.kelly_multiplier)
    exposure = n_held * mark if mark is not None else 0.0
    kelly_usd = k * (cash + exposure)  # the Kelly multiplier's share of the wealth
    account = _sizing.kelly_cash(cash, n_held, mark, k)
    can_buy = kelly_usd - exposure > 0.0 and cash + exposure > 0.0
    # The account sizes the orders and picks one. What each adds is reported on the
    # operator's own cash holding the same shares: when the position is worth more than
    # ``kelly_usd`` the account's cash sits at its floor, near zero, and a sale's growth in
    # the account is the log of that near-zero cash. The own cash is never taken below the
    # account's, whose floor keeps every log finite.
    own = max(cash, account)

    options: list[ParentOrder] = []
    for sd in buy_sides:
        mine = n_held if sd == held_side else 0.0
        options.append(buy_option(sd, buys[sd], table, account if can_buy else 0.0, mine,
                                  mark if mine > 0.0 else None, bankroll=own))
    for sd, prices in sells.items():
        options.append(sell_option(sd, prices, table, account, n_held, bankroll=own))
    paying = [o for o in options if o.account_growth > 0.0 and o.paying]
    order = max(paying, key=lambda o: o.account_growth) if paying else None
    return Decision(
        p_model=view.p_model, p=view.p, order=order, options=tuple(options),
        held_side=held_side, held_shares=n_held, account_cash=account,
        factors=_factors(state, params, view, settings.paths, table.steps, ms, options,
                         account, n_held, exposure, kelly_usd),
        explanation=story(state, view, order, options, held_side, n_held,
                          held_usd=exposure, kelly_usd=kelly_usd),
    )
