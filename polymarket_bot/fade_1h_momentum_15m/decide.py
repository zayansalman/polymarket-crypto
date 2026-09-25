"""The model hook for Fade 1h Momentum on 15m: one coin's inputs in, a :class:`Decision` out.

``decide(inputs, dials, settings)`` prices the coin's current 15m window with the TWAP-settled
model (``model.py``: a window settles Up iff the Chainlink TWAP-60s print at its close is at
least the print at its open), follows the market with the dials' anchor weights, picks the
side, and prices every rung of that side's ladder and a hedge bid on each side by a capped
Monte Carlo of the model's own paths. Pure and standard library only (no numpy or scipy); the
runner calls it off the event loop.

The steps, with the maths each one uses
---------------------------------------
1. The model's chance of Up, ``p_model`` (``model.prob_up_twap`` with the 60 s average):
   the leg so far (the spot feed against the start reference, plus the part of the closing
   average already printed), the snap-back (the stretch of the last twelve 15m candles,
   pulled back at speed ``kappa0 e^{-lam t}``) and the momentum (``theta`` times the blend of
   the 1h market's implied drift and the spot's own trailing hour), over the noise left.
2. The chance traded on, ``p = sizing.anchor(p_model, m, w_M, w_S)``: probit-space weights on
   the 15m market's own Up mid ``m`` and on the model (section 1 of
   ``tasks/2026-09-22-fade-1h-sizing-hedging.md``).
3. The side: Up when ``p >= 1/2``, else Down.
4. The ladder: every price of :func:`ladder_prices` (the operator's band under that side's
   best ask; never at or above the ask). For each rung, ``p_fill`` (it fills before the window
   ends) and ``q_fill`` (the side wins given that it fills) come from ``MC_PATHS`` simulated
   paths of the fitted process (seeded per decision, so a pass is reproducible):
   - each path draws its drift from N(mu, v) and runs section 1's exact transition on a grid
     (every 10 s, every 5 s inside the closing minute), tracking the closing average;
   - the market's price along the path is the crowd's view (a drift and Brownian noise, with
     the TWAP settlement) calibrated to equal the market's mid now, as in section 6 of the
     research doc: a bid at ``b`` fills when that price for its side comes down to ``b``;
   - at the fill the chance is re-evaluated there: ``Phi(w_M z_market + w_S z_model)`` with the
     path's state and drift, so being filled deep (the price moved against us) is inside
     ``q_fill``, not a haircut. The per-rung estimates are made consistent across the ladder
     (a deeper rung fills only on paths that filled the shallower ones).
5. Hedge quotes, one per held side: a resting bid at the other side's best bid, with the chance
   the held side still wins given that bid fills, from the same paths.
6. The factor waterfall for the card, and one sentence in a trader's words.

What the runner does with it
----------------------------
A Decision carries probabilities, never dollar amounts. The runner owns sizing, because sizing
needs what the model does not see: the bankroll, the positions already held, the Kelly
multiplier and the other coins decided in the same pass. For every coin with a Decision the
runner (``runner.py``) keeps the rungs on its ladder grid, sizes them with ``sizing.ladder``
against the free bankroll, shrinks each coin's ladder by the joint Kelly of all the coins
decided this pass (``sizing.joint_kelly`` with the dials' ``rho``), applies the Kelly
multiplier, sizes a hedge of a held position with ``sizing.hedge_shares_given_fill``, and turns
each stake into shares with ``sizing.size_to_order`` (the largest single bid caps each one). A
rung or hedge the maths gives nothing is not placed. There is no price rule or threshold: the
side is where p leans, the prices are the operator's grid and every size is zero when it does
not pay.

Contract: pure, standard library only; raises :class:`NoDecision` (with a plain-English reason)
when the inputs cannot be priced, and ValueError on dials or inputs that are not numbers.
``p_model`` and ``p`` are chances the window settles Up; ``q_fill`` and ``p_held_given_fill``
are for the side named.
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
SPOT_FEEDS = ("chainlink_twap60", "chainlink", "binance")

# The ladder grid's step: whole cents, or the book's tick when that is coarser. The band is set
# in cents, and a cent step keeps a 1-15c band at 15 rungs even where the venue's tick is a
# tenth of a cent near 0 and 1 (a whole-cent step is always on a finer tick's grid).
LADDER_STEP = 0.01
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


@dataclass(frozen=True)
class RungQuote:
    """One candidate rung of the entry ladder, on the Decision's side.

    ``price``: the bid, a price from :func:`ladder_prices` for that side's best ask.
    ``p_fill``: the chance this rung fills before the window ends. Cumulative: a deeper rung
    fills only after every shallower one, so ``p_fill`` falls with depth.
    ``q_fill``: the chance the side wins given that this rung fills (being filled deep means
    the price moved against us, so it is usually lower for deeper rungs). ``p_fill`` and
    ``q_fill`` come from the same simulated paths for every rung, so they are consistent.
    """

    price: float
    p_fill: float
    q_fill: float

    def __post_init__(self) -> None:
        _probability("price", self.price)
        _probability("p_fill", self.p_fill)
        _probability("q_fill", self.q_fill)


@dataclass(frozen=True)
class HedgeQuote:
    """A resting hedge bid for a position held in this window.

    If we hold ``held`` (net), bid on the other side at ``price``. ``p_held_given_fill`` is
    the chance the held side still wins given that this hedge bid fills (usually lower than
    the plain chance: the other side's bid fills when the price moves toward it). One per held
    side; the runner uses the one matching what is actually held, and places nothing when
    nothing is held.
    """

    held: str
    price: float
    p_held_given_fill: float

    def __post_init__(self) -> None:
        _side("held", self.held)
        _probability("price", self.price)
        _probability("p_held_given_fill", self.p_held_given_fill)

    @property
    def side(self) -> str:
        """The side the hedge bid buys: the other side of what is held."""
        return "Down" if self.held == "Up" else "Up"


@dataclass(frozen=True)
class Decision:
    """What the model says about one coin's current 15m window.

    - ``side``: the side the entry ladder bids on ("Up" or "Down").
    - ``p_model``: the model's own chance that the window settles Up (TWAP-60s settlement).
    - ``p``: the chance traded on: the market anchor of ``p_model`` and the 15m market's price,
      ``sizing.anchor(p_model, market_up, w_M, w_S)`` with the dials' weights.
    - ``rungs``: ladder rungs on ``side`` with their fill and win chances. The runner sizes
      them into bids; rungs that do not pay get nothing.
    - ``hedges``: resting hedge quotes, at most one per held side (see :class:`HedgeQuote`).
    - ``factors``: numbers for the card: the waterfall (leg so far, snap-back, momentum, market
      anchor) in z units and in points of probability, and the model's internals.
    - ``explanation``: one plain-English sentence for the card.
    """

    side: str
    p_model: float
    p: float
    rungs: tuple[RungQuote, ...] = ()
    hedges: tuple[HedgeQuote, ...] = ()
    factors: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))
    explanation: str = ""

    def __post_init__(self) -> None:
        _side("side", self.side)
        _probability("p_model", self.p_model)
        _probability("p", self.p)
        rungs = tuple(self.rungs)
        if not all(isinstance(r, RungQuote) for r in rungs):
            raise ValueError("rungs must be RungQuote values")
        hedges = tuple(self.hedges)
        if not all(isinstance(h, HedgeQuote) for h in hedges):
            raise ValueError("hedges must be HedgeQuote values")
        held = [h.held for h in hedges]
        if len(set(held)) != len(held):
            raise ValueError("at most one hedge quote per held side")
        factors: dict[str, float] = {}
        for name, value in dict(self.factors).items():
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"factor {name!r} is not a finite number")
            factors[str(name)] = number
        object.__setattr__(self, "rungs", rungs)
        object.__setattr__(self, "hedges", hedges)
        object.__setattr__(self, "factors", MappingProxyType(factors))
        object.__setattr__(self, "explanation", str(self.explanation or ""))

    def hedge_for(self, held: str) -> HedgeQuote | None:
        """The hedge quote for a position held on ``held``, if the model gave one."""
        return next((h for h in self.hedges if h.held == held), None)


@dataclass(frozen=True)
class DecideSettings:
    """The operator's settings the model reads (the runner fills these from Settings).

    ``spot_feed``: which reference series gives the price now for the leg so far, one of
    ``SPOT_FEEDS`` (the 15m market settles on ``chainlink_twap60``). ``band_lo`` and
    ``band_hi``: the ladder band, in dollars under the side's best ask (nearest and deepest
    rung). ``paths``: simulated paths per decision.
    """

    spot_feed: str = "chainlink_twap60"
    band_lo: float = 0.01
    band_hi: float = 0.15
    paths: int = MC_PATHS

    def __post_init__(self) -> None:
        if self.spot_feed not in SPOT_FEEDS:
            raise ValueError(f"spot_feed must be one of {SPOT_FEEDS}, got {self.spot_feed!r}")
        for name in ("band_lo", "band_hi"):
            value = float(getattr(self, name))
            if not (math.isfinite(value) and value >= 0.0):
                raise ValueError(f"{name} must be zero or more, got {value!r}")
        if int(self.paths) < 1:
            raise ValueError(f"paths must be at least 1, got {self.paths!r}")


DEFAULT_SETTINGS = DecideSettings()


def ladder_prices(best_ask: float, tick: float, band_lo: float,
                  band_hi: float) -> tuple[float, ...]:
    """The ladder grid for one side: bids from ``band_lo`` to ``band_hi`` dollars under
    ``best_ask``, nearest first, strictly below the ask and above zero.

    The step is ``LADDER_STEP`` (a cent) or ``tick`` when that is coarser. Prices are rounded
    to 6 decimals so they compare equal to the same price computed elsewhere. Empty when the
    band is empty (``band_hi < band_lo``) or the ask is not a price.
    """
    ask, step = float(best_ask), max(float(tick), LADDER_STEP)
    lo, hi = float(band_lo), float(band_hi)
    if not (math.isfinite(ask) and 0.0 < ask <= 1.0) or not math.isfinite(step) or step <= 0:
        return ()
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi < lo:
        return ()
    first = max(1, math.ceil(lo / step - _PRICE_EPS))
    last = math.floor(hi / step + _PRICE_EPS)
    out = []
    for j in range(first, last + 1):
        price = round(ask - j * step, 6)
        if price <= _PRICE_EPS:
            break
        out.append(price)
    return tuple(out)


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
    spot_feed: str = "chainlink_twap60"

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


def state_from_inputs(inputs: Inputs, spot_feed: str = "chainlink_twap60") -> ModelState:
    """The model's view of live :class:`~polymarket_bot.fade_1h_momentum_15m.inputs.Inputs`."""
    if spot_feed not in SPOT_FEEDS:
        raise ValueError(f"spot_feed must be one of {SPOT_FEEDS}, got {spot_feed!r}")
    now = {"chainlink_twap60": inputs.twap60, "chainlink": inputs.chainlink,
           "binance": inputs.binance}[spot_feed]
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
        sigma=sigma, mu_l=mu_l, r15=tuple(float(r) for r in inputs.r15), spot_feed=spot_feed,
    )


# Where each spot feed sits in ``Inputs.as_record()``.
_RECORD_KEYS = {"chainlink_twap60": "twap60", "chainlink": "chainlink", "binance": "binance"}


def state_from_record(record: Mapping[str, Any], spot_feed: str | None = None) -> ModelState:
    """The model's view of a recorded ``Inputs.as_record()`` (a ``fade_decisions`` row's
    inputs), so the learner can re-price a past decision under new dials. The spot feed is
    the one the row was decided with (its ``settings``), unless given."""
    if record.get("status") != "ok":
        raise NoDecision("The row has no complete inputs.")
    feed = spot_feed or str((record.get("settings") or {}).get("spot_feed")
                            or "chainlink_twap60")
    if feed not in SPOT_FEEDS:
        raise NoDecision(f"Unknown spot feed {feed!r}.")
    feed_key = _RECORD_KEYS[feed]
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
                     (record.get(feed_key) or {}).get("value"), start_ref),
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
    """Per bid price on each token: paths that filled it, and the sum over those paths of the
    anchored Up chance at the fill. ``n_paths`` in all."""

    n_paths: int
    up: dict[float, tuple[int, float]]
    down: dict[float, tuple[int, float]]
    steps: int = 0


@lru_cache(maxsize=8192)
def _step(t: float, dt: float, kappa0: float, lam: float) -> tuple[float, float, float]:
    """(e^-K, G, sqrt(V1)) of section 1's exact transition over [t, t + dt] (hours)."""
    m = _model.ou_unit_moments(t, dt, kappa0, lam)
    return 1.0 - m.A, m.G, math.sqrt(max(m.V1, 0.0))


def _grid(now: float, window_start: float, window_end: float) -> list[float]:
    """Seconds from now to just before the window end: every MC_STEP_S on the window's own
    clock, every MC_CLOSE_STEP_S inside the closing minute (its start is a grid point)."""
    close = window_end - AVG_S
    points: set[float] = set()
    s = window_start + (math.floor((now - window_start) / MC_STEP_S) + 1) * MC_STEP_S
    while s < close - 1e-6:
        points.add(s)
        s += MC_STEP_S
    if close > now:
        points.add(close)
    k = max(0, math.floor((now - close) / MC_CLOSE_STEP_S) + 1)
    s = close + k * MC_CLOSE_STEP_S
    while s < window_end - 1e-6:
        points.add(s)
        s += MC_CLOSE_STEP_S
    return [now] + sorted(p for p in points if p > now + 1e-6)


def _seed(state: ModelState) -> int:
    return zlib.crc32(f"{state.asset}|{state.window_slug}|{state.ts:.3f}".encode())


def simulate_fills(state: ModelState, dials: Mapping[str, float], view: View,
                   up_prices: Sequence[float], down_prices: Sequence[float], *,
                   paths: int = MC_PATHS, seed: int | None = None) -> FillTable:
    """Step 4 of the module docstring: which bids fill on which simulated paths, and the
    anchored Up chance at each fill.

    A bid at ``b`` on the Up token fills when the crowd's Up price ``Phi(z_c)`` comes down to
    ``b``; on the Down token when ``1 - Phi(z_c)`` does. The crowd prices the window with a
    drift ``mu_c`` and Brownian noise under the same TWAP settlement, ``z_c = (known + mu_c
    Gbar_B) / (sigma sqrt(Psi_B))``, with ``mu_c`` set so that ``z_c`` now is the market's mid.
    Bids at or above the market's price now fill at once, at today's p."""
    n_paths = int(paths)
    theta, kappa0, lam = dials["theta"], dials["kappa0"], dials["lam"]
    w_M, w_S = dials["w_M"], dials["w_S"]
    sigma, M, d0 = state.sigma, view.M, state.d
    h0 = state.h
    eps0, ell0 = _model.avg_split(h0)
    gb0, psib0 = _model.brownian_twap(h0)
    known0 = view.parts.known
    z_mkt = view.z_market
    mu_c = (sigma * math.sqrt(psib0) * z_mkt - known0) / gb0 if gb0 > 0.0 else 0.0

    ups = sorted({round(float(b), 6) for b in up_prices if 0.0 < b < 1.0}, reverse=True)
    dns = sorted({round(float(b), 6) for b in down_prices if 0.0 < b < 1.0}, reverse=True)
    up_n = [0] * len(ups)
    up_s = [0.0] * len(ups)
    dn_n = [0] * len(dns)
    dn_s = [0.0] * len(dns)
    # Fill thresholds on z_c: Up bid b fills when z_c <= probit(b); Down when z_c >= -probit(b).
    up_thr = [_probit(b) for b in ups]
    dn_thr = [-_probit(b) for b in dns]
    # Bids the market's price already reaches fill now, on every path, at today's p.
    iu0 = sum(1 for thr in up_thr if z_mkt <= thr)
    id0 = sum(1 for thr in dn_thr if z_mkt >= thr)
    for i in range(iu0):
        up_n[i], up_s[i] = n_paths, n_paths * view.p
    for i in range(id0):
        dn_n[i], dn_s[i] = n_paths, n_paths * view.p

    grid = _grid(state.ts, state.window_start, state.window_end)
    close_start = state.window_end - AVG_S
    rows = []
    for i in range(1, len(grid)):
        s_prev, s = grid[i - 1], grid[i]
        dt = (s - s_prev) / HOUR_S
        t_prev = (s_prev - state.hour_start) / HOUR_S
        t_i = (s - state.hour_start) / HOUR_S
        h_i = (state.window_end - s) / HOUR_S
        decay, g_step, sd_unit = _step(t_prev, dt, kappa0, lam)
        mom = _model.twap_unit_moments(t_i, h_i, kappa0, lam, _model.AVG_60S)
        gb, psib = _model.brownian_twap(h_i)
        rows.append((
            decay, g_step, sigma * sd_unit,
            s_prev >= close_start - 1e-6, dt,  # this step is inside the closing minute
            mom.ell, gb, 1.0 / (sigma * math.sqrt(psib)),
            mom.B, mom.Gbar, 1.0 / (sigma * math.sqrt(mom.Psi)),
        ))
    table = FillTable(n_paths=n_paths, up={}, down={}, steps=len(rows))
    n_up, n_dn = len(ups), len(dns)
    if rows and (iu0 < n_up or id0 < n_dn):
        rng = random.Random(_seed(state) if seed is None else seed)
        gauss = rng.gauss
        sq_v = math.sqrt(view.v) if view.v > 0.0 else 0.0
        integ0 = eps0 * view.abar if eps0 > 0.0 else 0.0
        inf = math.inf
        ndtr = _model.ndtr
        for _ in range(n_paths):
            drift = theta * (view.mu + sq_v * gauss(0.0, 1.0) if sq_v else view.mu)
            z_dev = M  # X - a, the stretch still to be pulled back
            d_prev = d0
            integ = integ0
            iu, idn = iu0, id0
            thr_u = up_thr[iu] if iu < n_up else -inf
            thr_d = dn_thr[idn] if idn < n_dn else inf
            for (decay, g_step, sd_step, in_close, dt, ell, gb, inv_csd, b_m, gbar,
                 inv_msd) in rows:
                z_dev = decay * z_dev + drift * g_step + sd_step * gauss(0.0, 1.0)
                d_now = d0 + (z_dev - M)
                if in_close:
                    integ += 0.5 * (d_prev + d_now) * dt
                d_prev = d_now
                known = integ + ell * d_now
                zc = (known + mu_c * gb) * inv_csd
                if zc <= thr_u or zc >= thr_d:
                    zm = (known - b_m * M + drift * gbar) * inv_msd
                    q_up = ndtr(max(-40.0, min(40.0, w_M * zc + w_S * zm)))
                    while iu < n_up and zc <= up_thr[iu]:
                        up_n[iu] += 1
                        up_s[iu] += q_up
                        iu += 1
                    while idn < n_dn and zc >= dn_thr[idn]:
                        dn_n[idn] += 1
                        dn_s[idn] += q_up
                        idn += 1
                    thr_u = up_thr[iu] if iu < n_up else -inf
                    thr_d = dn_thr[idn] if idn < n_dn else inf
                    if iu >= n_up and idn >= n_dn:
                        break
    table.up = {b: (n, s) for b, n, s in zip(ups, up_n, up_s)}
    table.down = {b: (n, s) for b, n, s in zip(dns, dn_n, dn_s)}
    return table


def rung_quotes(side: str, prices: Sequence[float], table: FillTable) -> list[RungQuote]:
    """The side's ladder from the simulation: nearest first, p_fill and q_fill for the side,
    made consistent (a deeper rung's fill-and-win paths are a subset of a shallower one's).

    The simulation re-evaluates the chance at each rung's own fill, which is unbiased rung by
    rung but can leave "exactly the first k rungs filled, then won" slightly outside
    [0, P_k - P_k+1] from noise. Those increments are clipped into range from the deepest rung
    up and the win chances rebuilt from them."""
    levels = table.up if side == "Up" else table.down
    n_paths = table.n_paths
    rows = []
    for b in sorted({round(float(p), 6) for p in prices}, reverse=True):
        n, s_up = levels.get(b, (0, 0.0))
        if n <= 0:
            break  # deeper rungs never fill either
        wins = s_up if side == "Up" else n - s_up
        rows.append([b, n, min(max(wins, 0.0), float(n))])
    for k in range(len(rows) - 1, -1, -1):
        n_k, raw = rows[k][1], rows[k][2]
        n_next, w_next = (rows[k + 1][1], rows[k + 1][2]) if k + 1 < len(rows) else (0, 0.0)
        rows[k][2] = w_next + min(max(raw - w_next, 0.0), float(n_k - n_next))
    return [RungQuote(b, min(n / n_paths, 1.0 - _P_EPS), _inside(w / n)) for b, n, w in rows]


def hedge_quotes(up_best_bid: float, down_best_bid: float, table: FillTable) -> list[HedgeQuote]:
    """Step 5: a hedge bid at the other side's best bid for each side that could be held."""
    out = []
    n, s_up = table.down.get(round(float(down_best_bid), 6), (0, 0.0))
    if n > 0:  # holding Up: bid Down at its best bid
        out.append(HedgeQuote("Up", round(float(down_best_bid), 6), _inside(s_up / n)))
    n, s_up = table.up.get(round(float(up_best_bid), 6), (0, 0.0))
    if n > 0:  # holding Down: bid Up at its best bid
        out.append(HedgeQuote("Down", round(float(up_best_bid), 6), _inside(1.0 - s_up / n)))
    return out


# ---------------------------------------------------------------------------
# The card: factors and one sentence
# ---------------------------------------------------------------------------


def _paying(rungs: Sequence[RungQuote]) -> list[float]:
    """The rung prices full Kelly stakes anything on (scale-free: W = 1)."""
    if not rungs:
        return []
    try:
        stakes = _sizing.ladder([(r.price, r.p_fill, r.q_fill) for r in rungs], 1.0, 1.0)
    except ValueError:
        return []
    return [r.price for r, x in zip(rungs, stakes) if x > 1e-9]


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


def story(state: ModelState, view: View, side: str, rungs: Sequence[RungQuote]) -> str:
    """One sentence for the card, in a trader's words."""
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
    paying = _paying(rungs)
    if len(paying) == 1:
        article = "an" if side == "Up" else "a"
        part_act = f"the maths rests {article} {side} bid at {_cents(paying[0])}"
    elif paying:
        prices = [_cents(b) for b in paying]
        listed = (", ".join(prices[:-1]) + f" and {prices[-1]}" if len(prices) <= 4
                  else f"{prices[0]} down to {prices[-1]} ({len(prices)} rungs)")
        part_act = f"the maths rests {side} bids at {listed}"
    elif rungs:
        part_act = f"no {side} bid under the ask wins often enough to pay, so nothing rests"
    else:
        part_act = f"no {side} bid in the band gets filled in the simulation, so nothing rests"
    return "; ".join((part_leg, part_snap, part_mom, part_mkt, part_act)) + "."


def _factors(state: ModelState, dials: Mapping[str, float], view: View, paths: int, steps: int,
             ms: float) -> dict[str, float]:
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
    }
    if math.isfinite(view.mu_H):
        out["mu_H"] = view.mu_H
    return {k: v for k, v in out.items() if isinstance(v, (int, float)) and math.isfinite(v)}


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------


def decide(
    inputs: Inputs, dials: Mapping[str, Any], settings: DecideSettings = DEFAULT_SETTINGS
) -> Decision:
    """The model's view of one coin's window: a :class:`Decision` (module docstring).

    ``inputs``: this pass's :class:`~polymarket_bot.fade_1h_momentum_15m.inputs.Inputs` for
    the coin. ``dials``: the newest dial set's parameters (``ledger.dials()["params"]``).
    ``settings``: the operator's model settings. Raises :class:`NoDecision` when the window
    cannot be priced (no volatility, the window over, a missing candle).
    """
    params = resolve_dials(dials)
    state = state_from_inputs(inputs, settings.spot_feed)
    view = evaluate(state, params)
    side = "Up" if view.p >= 0.5 else "Down"
    book = inputs.up_book if side == "Up" else inputs.down_book
    grid = ladder_prices(book.best_ask, inputs.tick_size, settings.band_lo, settings.band_hi)
    up_bb, dn_bb = float(inputs.up_book.best_bid), float(inputs.down_book.best_bid)
    up_prices = list(grid if side == "Up" else ()) + [up_bb]
    dn_prices = list(grid if side == "Down" else ()) + [dn_bb]
    started = time.perf_counter()
    table = simulate_fills(state, params, view, up_prices, dn_prices, paths=settings.paths)
    ms = 1000.0 * (time.perf_counter() - started)
    rungs = rung_quotes(side, grid, table)
    hedges = hedge_quotes(up_bb, dn_bb, table)
    return Decision(
        side=side, p_model=view.p_model, p=view.p, rungs=tuple(rungs), hedges=tuple(hedges),
        factors=_factors(state, params, view, settings.paths, table.steps, ms),
        explanation=story(state, view, side, rungs),
    )
