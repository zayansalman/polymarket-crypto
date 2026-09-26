"""Sizing maths for Fade 1h Momentum on 15m: market anchor, Kelly stakes, scaled passive limit
orders and position reduction.

Pure functions, standard library only (``math``, ``statistics.NormalDist``); no I/O. This is
sections 1-4 of ``tasks/2026-09-22-fade-1h-sizing.md``. One objective drives every
function: maximise the expected log of the bankroll (Kelly 1956), the choice that grows money
fastest over repeated bets. Nothing here is a price rule or a threshold: every output comes
out of that objective and is zero when it does not pay.

- :func:`anchor` blends the model with the market's own price in probit space (section 1).
- :func:`kelly_maker` and :func:`kelly_taker` size one bet (section 2).
- :func:`joint_kelly` sizes simultaneous bets whose outcomes move together, through a
  one-factor Gaussian copula integrated by Gauss-Hermite quadrature (section 2). A bet on Down
  loads on the shared factor with the opposite sign of a bet on Up.
- :func:`scaled_limits` sizes one parent buy order split into child orders resting at several
  price levels on one side of one market, counting the shares of that side already held
  (section 3).
- :func:`reduce_position` sizes a resting sell of shares already held (section 4: we never
  hold both sides, so a position is cut by selling it, not by buying the other side).
- :func:`kelly_cash` is the cash of the fractional-Kelly account those two work in.
- :func:`size_to_order` turns a dollar stake into a share count the venue accepts.

Every order here is a resting limit order; nothing crosses the spread. The taker formula is
kept only so a card can show what taking would have paid.

The Kelly multiplier ``k`` (the operator's risk dial; section 2 of the spec, default one half)
---------------------------------------------------------------------------------------------
With nothing held, fractional Kelly is full Kelly times ``k``. With shares already held,
multiplying the full-Kelly stake by ``k`` every pass would re-bet the same outcome pass after
pass and creep to full Kelly. So the multiplier is applied to wealth instead: the sizing works
in a Kelly account worth ``k`` times the total wealth (cash plus the held shares at their
market mark) that holds all the shares already bought, and its cash is
``C = k (W + n mark) - n mark`` (:func:`kelly_cash`). Full-Kelly sizing on ``(C, n)`` then
stakes exactly ``k`` times full Kelly when nothing is held, adds nothing after a fill at
unchanged odds, and sells when the position has grown past ``k`` of the wealth or the odds
have turned. With ``k = 1`` the account is the whole cash ``W``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from functools import lru_cache
from statistics import NormalDist
from typing import NamedTuple

from ems.fees import taker_fee_per_share

__all__ = [
    "Bet",
    "DEFAULT_QUADRATURE_NODES",
    "KELLY_CASH_FLOOR",
    "MAX_JOINT_BETS",
    "PriceLevel",
    "SIDES",
    "STAKE_CAP",
    "anchor",
    "gauss_hermite",
    "joint_kelly",
    "joint_log_growth",
    "kelly_cash",
    "kelly_maker",
    "kelly_taker",
    "normal_quadrature",
    "outcome_probabilities",
    "reduce_position",
    "sale_log_growth",
    "scaled_limits",
    "scaled_limits_log_growth",
    "size_to_order",
]

_NORMAL = NormalDist()

SIDES = ("Up", "Down")

# Probabilities are held inside (eps, 1 - eps) before an inverse normal: a market mid of
# exactly 0 or 1 would otherwise map to an infinite z.
_PROBIT_EPS = 1e-12

# Upper bound on the total stake as a fraction of the bankroll. Losing every bet leaves
# 1 - total, whose log must exist; this keeps every candidate the solver tries finite.
STAKE_CAP = 1.0 - 1e-9

# The Kelly account's cash never goes below this fraction of the total wealth (or 1e-12
# dollars), so its log exists. An account at the floor holds more than k of the wealth in
# one position: nothing more is bought, and a sale is worth almost any fill.
KELLY_CASH_FLOOR = 1e-9

# The joint table has 2**n outcomes. The strategy uses four coins; eight is headroom.
MAX_JOINT_BETS = 8

# 128 Gauss-Hermite nodes give every joint outcome probability to about 1e-8 for a copula
# correlation up to 0.9 and about 1e-5 at 0.95 (checked against Sheppard's exact formula).
# A correlation of exactly 1 is handled exactly, without quadrature.
DEFAULT_QUADRATURE_NODES = 128

# Supplied probabilities may disagree with each other by float rounding, never by more.
_CONSISTENCY_TOL = 1e-9

# Stop when the projected-gradient step (the first-order optimality residual) is this small.
_KKT_TOL = 1e-14
_MAX_NEWTON_ITER = 100


class Bet(NamedTuple):
    """One binary bet. ``q``: chance it wins (for a resting order, given that it fills).
    ``price``: cost per share, fee included. ``payoff``: paid per share if it wins.
    ``side``: the outcome it is on, "Up" or "Down"; bets on opposite sides of coins that move
    together hedge each other."""

    q: float
    price: float
    payoff: float = 1.0
    side: str = "Up"


class PriceLevel(NamedTuple):
    """One price level of a scaled parent order. ``price``: where its child order rests.
    ``p_fill``: chance that child order fills (cumulative: a deeper level fills only after
    every nearer one). ``q_fill``: chance the side wins given that it fills."""

    price: float
    p_fill: float
    q_fill: float


# ---------------------------------------------------------------------------------------------
# Input checks
# ---------------------------------------------------------------------------------------------


def _finite(name: str, x: float) -> float:
    x = float(x)
    if not math.isfinite(x):
        raise ValueError(f"{name} must be a finite number, got {x!r}")
    return x


def _prob(name: str, p: float) -> float:
    p = _finite(name, p)
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"{name} must be a probability between 0 and 1, got {p!r}")
    return p


def _price(name: str, b: float) -> float:
    b = _finite(name, b)
    if not 0.0 < b < 1.0:
        raise ValueError(f"{name} must be a price strictly between 0 and 1, got {b!r}")
    return b


def _multiplier(k: float) -> float:
    k = _finite("kelly_multiplier", k)
    if not 0.0 <= k <= 1.0:
        raise ValueError(f"kelly_multiplier must be between 0 and 1, got {k!r}")
    return k


def _shares(name: str, n: float) -> float:
    n = _finite(name, n)
    if n < 0.0:
        raise ValueError(f"{name} (shares held) must not be negative, got {n!r}")
    return n


def _side(name: str, side: str) -> str:
    if side not in SIDES:
        raise ValueError(f"{name} must be one of {SIDES}, got {side!r}")
    return side


def _probit(p: float) -> float:
    return _NORMAL.inv_cdf(min(max(p, _PROBIT_EPS), 1.0 - _PROBIT_EPS))


# ---------------------------------------------------------------------------------------------
# Section 1: follow the market
# ---------------------------------------------------------------------------------------------


def anchor(p_model: float, market_mid: float, w_M: float, w_S: float) -> float:
    """The probability we trade on: ``Phi(w_M * Phi^-1(m) + w_S * Phi^-1(p_model))``.

    ``market_mid`` is the market's own Up mid, ``p_model`` the model's Up probability, and
    ``w_M``, ``w_S`` the weights learned on settled windows. With ``w_S = 0`` and ``w_M = 1``
    the answer is the market price itself, so nothing trades until the model has shown it
    adds information beyond the price (Bates and Granger 1969; Ranjan and Gneiting 2010).
    ``p_model`` is not read when ``w_S`` is 0.
    """
    m = _prob("market_mid", market_mid)
    w_M = _finite("w_M", w_M)
    w_S = _finite("w_S", w_S)
    z = w_M * _probit(m)
    if w_S != 0.0:
        z += w_S * _probit(_prob("p_model", p_model))
    return _NORMAL.cdf(z)


# ---------------------------------------------------------------------------------------------
# Section 2: one bet
# ---------------------------------------------------------------------------------------------


def kelly_maker(q: float, b: float) -> float:
    """Full-Kelly bankroll fraction for a resting buy at ``b`` that wins with chance ``q`` if
    it fills: ``(q - b) / (1 - b)``, and 0 when that is negative. No fee on a resting fill."""
    q = _prob("q", q)
    b = _price("b", b)
    return max(0.0, (q - b) / (1.0 - b))


def kelly_taker(q: float, a: float, fee_rate: float = 0.07) -> float:
    """Full-Kelly fraction for taking at the ask ``a`` with the venue's taker fee
    ``c = fee_rate * a * (1 - a)``: ``(q - a - c) / (1 - a - c)``, 0 when negative.

    For comparison only: the strategy never crosses the spread.
    """
    q = _prob("q", q)
    a = _price("a", a)
    cost = a + taker_fee_per_share(a, _finite("fee_rate", fee_rate))
    if cost >= 1.0:
        return 0.0
    return max(0.0, (q - cost) / (1.0 - cost))


# ---------------------------------------------------------------------------------------------
# Section 2: several bets whose outcomes move together
# ---------------------------------------------------------------------------------------------


@lru_cache(maxsize=16)
def gauss_hermite(n: int) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Nodes and weights of ``n``-point Gauss-Hermite quadrature, in ascending node order:
    ``integral of exp(-x^2) g(x) dx`` is ``sum(w_i * g(x_i))``, exact for polynomials ``g``
    of degree up to ``2n - 1``.

    Newton's method on the orthonormal Hermite recurrence, started from the asymptotic root
    guesses of Press et al., Numerical Recipes, section 4.5 (routine gauher).
    """
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError(f"the number of nodes must be a positive integer, got {n!r}")
    pim4 = math.pi ** -0.25
    nodes = [0.0] * n
    weights = [0.0] * n
    z = 0.0
    for i in range((n + 1) // 2):
        if i == 0:
            z = math.sqrt(2.0 * n + 1.0) - 1.85575 * (2.0 * n + 1.0) ** -0.16667
        elif i == 1:
            z -= 1.14 * n ** 0.426 / z
        elif i == 2:
            z = 1.86 * z - 0.86 * nodes[0]
        elif i == 3:
            z = 1.91 * z - 0.91 * nodes[1]
        else:
            z = 2.0 * z - nodes[i - 2]
        derivative = 0.0
        for _ in range(100):
            p1, p2 = pim4, 0.0
            for j in range(1, n + 1):
                p3, p2 = p2, p1
                p1 = z * math.sqrt(2.0 / j) * p2 - math.sqrt((j - 1.0) / j) * p3
            derivative = math.sqrt(2.0 * n) * p2
            step = p1 / derivative
            z -= step
            if abs(step) <= 1e-14 * max(1.0, abs(z)):
                break
        else:
            raise ArithmeticError(f"Gauss-Hermite root {i} of {n} did not converge")
        nodes[i], nodes[n - 1 - i] = z, -z
        weights[i] = weights[n - 1 - i] = 2.0 / (derivative * derivative)
    if n % 2:
        nodes[n // 2] = 0.0
    return tuple(reversed(nodes)), tuple(reversed(weights))


@lru_cache(maxsize=16)
def normal_quadrature(n: int) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Gauss-Hermite rescaled for a standard normal ``Z``: points ``z_i`` and probabilities
    ``p_i`` (summing to 1) with ``E[g(Z)]`` approximately ``sum(p_i * g(z_i))``."""
    xs, ws = gauss_hermite(n)
    root_pi = math.sqrt(math.pi)
    return (tuple(math.sqrt(2.0) * x for x in xs), tuple(w / root_pi for w in ws))


def outcome_probabilities(
    qs: Sequence[float],
    rho: float,
    n_nodes: int = DEFAULT_QUADRATURE_NODES,
    sides: Sequence[str] | None = None,
) -> dict[tuple[bool, ...], float]:
    """The chance of every win/lose combination of bets that win with chances ``qs`` and move
    together through a one-factor Gaussian copula with correlation ``rho``.

    Each coin's Up result is ``sqrt(rho) Z + sqrt(1 - rho) e_i < Phi^-1(chance of Up)``, with
    one shared normal ``Z`` and independent normals ``e_i``, so every pair of hidden normals
    has correlation ``rho`` (the copula correlation; the correlation of the win/lose results
    themselves is lower). ``rho`` is learned on the coins' Up results. A bet on Up wins with
    that event; a bet on Down wins with its complement, which is the same model with the
    shared factor's sign flipped: ``-sqrt(rho) Z - sqrt(1 - rho) e_i < Phi^-1(q_i)``. So two
    bets on the same side move together and two bets on opposite sides move against each
    other. ``sides`` gives each bet's side ("Up" or "Down"; all Up when omitted). The shared
    ``Z`` is integrated out by Gauss-Hermite quadrature. A one-factor copula covers
    ``0 <= rho <= 1``; a negative estimate is treated as 0 (independent). Keys are tuples of
    booleans, True meaning that bet wins.
    """
    probs = [_prob(f"q[{i}]", q) for i, q in enumerate(qs)]
    n = len(probs)
    signs = [1.0] * n if sides is None else [
        1.0 if _side(f"sides[{i}]", s) == "Up" else -1.0 for i, s in enumerate(sides)]
    if len(signs) != n:
        raise ValueError(f"expected {n} sides, got {len(signs)}")
    rho = min(max(_finite("rho", rho), 0.0), 1.0)
    if n == 0:
        return {(): 1.0}
    if n > MAX_JOINT_BETS:
        raise ValueError(f"at most {MAX_JOINT_BETS} simultaneous bets, got {n}")

    if rho >= 1.0:
        # Perfectly together: one uniform U; Up wins on coin i when U < chance of Up. A bet on
        # Up wins when U < q_i, a bet on Down when U > 1 - q_i.
        cuts = sorted({0.0, 1.0, *(q if s > 0 else 1.0 - q for q, s in zip(probs, signs))})
        table: dict[tuple[bool, ...], float] = {}
        for lo, hi in zip(cuts, cuts[1:]):
            mid = 0.5 * (lo + hi)
            key = tuple((q > mid) if s > 0 else (mid > 1.0 - q) for q, s in zip(probs, signs))
            table[key] = table.get(key, 0.0) + (hi - lo)
        return table

    if rho == 0.0 or n == 1:
        points: tuple[float, ...] = (0.0,)
        mass: tuple[float, ...] = (1.0,)
        win_given_z = [[q] for q in probs]
        lose_given_z = [[1.0 - q] for q in probs]
    else:
        points, mass = normal_quadrature(n_nodes)
        load, spread = math.sqrt(rho), math.sqrt(1.0 - rho)
        win_given_z, lose_given_z = [], []
        for q, sign in zip(probs, signs):
            if q <= 0.0 or q >= 1.0:
                win_given_z.append([q] * len(points))
                lose_given_z.append([1.0 - q] * len(points))
                continue
            t = _NORMAL.inv_cdf(q)
            args = [(t - sign * load * z) / spread for z in points]
            win_given_z.append([_NORMAL.cdf(a) for a in args])
            lose_given_z.append([_NORMAL.cdf(-a) for a in args])

    # Build the joint table one bet at a time, carrying the product at every node.
    partial: list[tuple[tuple[bool, ...], list[float]]] = [((), list(mass))]
    for i in range(n):
        grown = []
        for key, weights in partial:
            grown.append((key + (True,), [w * c for w, c in zip(weights, win_given_z[i])]))
            grown.append((key + (False,), [w * c for w, c in zip(weights, lose_given_z[i])]))
        partial = grown
    return {key: math.fsum(weights) for key, weights in partial}


def _as_bet(i: int, bet: Bet | Sequence[float]) -> Bet:
    b = Bet(*bet)
    q = _prob(f"bets[{i}].q", b.q)
    price = _finite(f"bets[{i}].price", b.price)
    payoff = _finite(f"bets[{i}].payoff", b.payoff)
    if price <= 0.0:
        raise ValueError(f"bets[{i}].price must be positive, got {price!r}")
    if payoff < 0.0:
        raise ValueError(f"bets[{i}].payoff must not be negative, got {payoff!r}")
    return Bet(q, price, payoff, _side(f"bets[{i}].side", b.side))


# A scenario is (probability, base wealth, return per unit staked on each variable). Wealth in
# a scenario is base + sum(R_i * f_i) per unit of bankroll. The base is 1 unless shares are
# already held: then it is 1 + n / W where those shares pay. Every R_i is at least -1.
_Scenario = tuple[float, float, tuple[float, ...]]


def _joint_scenarios(bets: list[Bet], rho: float, n_nodes: int) -> list[_Scenario]:
    table = outcome_probabilities([b.q for b in bets], rho, n_nodes, [b.side for b in bets])
    win_return = [b.payoff / b.price - 1.0 for b in bets]
    scenarios = []
    for key, p in table.items():
        if p > 0.0:
            scenarios.append(
                (p, 1.0, tuple(r if won else -1.0 for r, won in zip(win_return, key))))
    return scenarios


def joint_kelly(
    bets: Sequence[Bet | Sequence[float]],
    rho: float,
    kelly_multiplier: float,
    n_nodes: int = DEFAULT_QUADRATURE_NODES,
) -> list[float]:
    """Bankroll fractions for simultaneous bets whose outcomes move together.

    Maximises ``E[ln(1 + sum(f_i R_i))]`` over the joint win/lose table from
    :func:`outcome_probabilities` (each bet's side sets the sign of its load on the shared
    factor), where ``R_i`` is ``payoff/price - 1`` if bet ``i`` wins and ``-1`` if it loses,
    subject to ``f_i >= 0`` and ``sum(f_i) < 1``, by projected Newton. The objective is
    concave, so the optimum is unique; a bet that adds nothing gets 0. Bets on the same side
    share the move and get less than alone; bets on opposite sides hedge each other and can
    get more. The full-Kelly stakes are then multiplied by ``kelly_multiplier`` (0 to 1).
    Each bet is a :class:`Bet` or a ``(q, price[, payoff[, side]])`` tuple; for a resting
    order, ``q`` is the chance of winning given that it fills. Returns fractions in the order
    given.
    """
    k = _multiplier(kelly_multiplier)
    clean = [_as_bet(i, b) for i, b in enumerate(bets)]
    if not clean:
        return []
    if len(clean) > MAX_JOINT_BETS:
        raise ValueError(f"at most {MAX_JOINT_BETS} simultaneous bets, got {len(clean)}")
    best = _maximise_log_growth(_joint_scenarios(clean, rho, n_nodes), len(clean))
    return [k * f for f in best]


def joint_log_growth(
    bets: Sequence[Bet | Sequence[float]],
    rho: float,
    stakes: Sequence[float],
    n_nodes: int = DEFAULT_QUADRATURE_NODES,
) -> float:
    """Expected log growth of the bankroll, ``E[ln(1 + sum(f_i R_i))]``, for bankroll
    fractions ``stakes`` on ``bets`` (same model as :func:`joint_kelly`)."""
    clean = [_as_bet(i, b) for i, b in enumerate(bets)]
    f = _stakes(stakes, len(clean))
    return _log_growth(_joint_scenarios(clean, rho, n_nodes), f)


def _stakes(stakes: Sequence[float], n: int) -> list[float]:
    f = [_finite(f"stakes[{i}]", s) for i, s in enumerate(stakes)]
    if len(f) != n:
        raise ValueError(f"expected {n} stakes, got {len(f)}")
    if any(s < 0.0 for s in f):
        raise ValueError("stakes must not be negative")
    if math.fsum(f) >= 1.0:
        raise ValueError("stakes must total less than the bankroll")
    return f


# ---------------------------------------------------------------------------------------------
# The fractional-Kelly account
# ---------------------------------------------------------------------------------------------


def kelly_cash(W: float, n: float, mark: float | None, kelly_multiplier: float) -> float:
    """The cash of the Kelly account: ``k (W + n mark) - n mark`` (module docstring).

    ``W``: cash. ``n``: shares of one side already held, valued at ``mark`` (that side's
    market price; not read when ``n`` is 0). ``k``: the Kelly multiplier. With nothing held
    this is ``k W``. Never below ``KELLY_CASH_FLOOR`` of the total wealth (so its log
    exists): a position worth more than ``k`` of the wealth leaves the account at that floor,
    where only a sale pays. The account is for sizing and choosing only: a growth figure
    worked out in an account at its floor is the log of that near-zero cash, so what an order
    adds is reported on the operator's own cash (``decide.py`` does both).
    """
    W = _finite("W", W)
    n = _shares("n", n)
    k = _multiplier(kelly_multiplier)
    value = 0.0
    if n > 0.0:
        if mark is None:
            raise ValueError("mark is needed when shares are held")
        value = n * _price("mark", mark)
    total = W + value
    return max(k * total - value, KELLY_CASH_FLOOR * max(total, 1e-3))


# ---------------------------------------------------------------------------------------------
# Section 3: one parent order split into child orders at several price levels
# ---------------------------------------------------------------------------------------------


def _level_scenarios(
    levels: Sequence[PriceLevel | Sequence[float]], held_fraction: float = 0.0,
) -> tuple[list[int], list[_Scenario]]:
    """Levels sorted nearest (highest price) first, and the scenarios "exactly the first k
    child orders filled, then win / lose" in that order. ``held_fraction``: shares already
    held on this side per unit of bankroll (they pay in every win scenario). The no-fill
    scenario changes nothing and is left out."""
    clean = []
    for i, r in enumerate(levels):
        level = PriceLevel(*r)
        clean.append(PriceLevel(_price(f"levels[{i}].price", level.price),
                                _prob(f"levels[{i}].p_fill", level.p_fill),
                                _prob(f"levels[{i}].q_fill", level.q_fill)))
    order = sorted(range(len(clean)), key=lambda i: -clean[i].price)
    ordered = [clean[i] for i in order]
    for near, deep in zip(ordered, ordered[1:]):
        if deep.p_fill > near.p_fill + _CONSISTENCY_TOL:
            raise ValueError(
                f"a deeper price level ({deep.price}) cannot fill more often than a nearer "
                f"one ({near.price}): {deep.p_fill} > {near.p_fill}"
            )
    n = len(ordered)
    odds = [(1.0 - r.price) / r.price for r in ordered]
    win_base = 1.0 + held_fraction
    scenarios: list[_Scenario] = []
    for k in range(1, n + 1):
        here, nxt = ordered[k - 1], (ordered[k] if k < n else None)
        p_next = nxt.p_fill if nxt else 0.0
        win_next = nxt.p_fill * nxt.q_fill if nxt else 0.0
        exactly = here.p_fill - p_next  # chance exactly the first k child orders fill
        win = here.p_fill * here.q_fill - win_next  # ... and the side wins
        if not -_CONSISTENCY_TOL <= win <= max(exactly, 0.0) + _CONSISTENCY_TOL:
            raise ValueError(
                f"the price levels' win chances are inconsistent: when exactly the first {k} "
                f"child orders fill (chance {exactly:.6g}) the implied chance of filling and "
                f"winning is {win:.6g}"
            )
        if exactly <= 0.0:
            continue
        q_bar = min(max(win / exactly, 0.0), 1.0)
        pad = (0.0,) * (n - k)
        if q_bar > 0.0:
            scenarios.append((exactly * q_bar, win_base, tuple(odds[:k]) + pad))
        if q_bar < 1.0:
            scenarios.append((exactly * (1.0 - q_bar), 1.0, (-1.0,) * k + pad))
    return order, scenarios


def scaled_limits(
    levels: Sequence[PriceLevel | Sequence[float]],
    W: float,
    kelly_multiplier: float,
    held: float = 0.0,
    mark: float | None = None,
) -> list[float]:
    """Dollar stakes for one parent buy order split into child orders resting at several
    price levels on one side, in the order the levels are given.

    Each level is a :class:`PriceLevel` or ``(price, p_fill, q_fill)``. A deeper child order
    fills only after every nearer one, so ``p_fill`` falls with depth; ``q_fill`` is the chance
    of winning given that level fills (lower for deeper levels when being filled deep means
    the price moved against us). ``held`` shares of the same side are already held: they pay
    ``held`` dollars in every win scenario, so the stakes count them (a position already at
    its log-optimal size gets nothing more). With the Kelly account's cash
    ``C = kelly_cash(W, held, mark, kelly_multiplier)`` the stakes ``x_i >= 0`` maximise

        sum_k (P_k - P_k+1) [ qbar_k ln(C + held + sum_{i<=k} x_i (1 - b_i)/b_i)
                              + (1 - qbar_k) ln(C - sum_{i<=k} x_i) ]

    where ``qbar_k = (P_k q_k - P_k+1 q_k+1) / (P_k - P_k+1)`` is the chance of winning
    when exactly the first ``k`` child orders fill. With nothing held that is ``k`` times the
    full-Kelly stakes on ``W``. The objective is concave, so the optimum is unique; it is found
    by projected Newton. A level whose ``q_fill`` does not beat its price always gets 0.

    ``p_fill`` and ``q_fill`` must be estimated from the same simulated paths for every
    level; then they are consistent by construction. Inconsistent inputs raise ValueError.
    """
    k = _multiplier(kelly_multiplier)
    W = _finite("W", W)
    held = _shares("held", held)
    C = kelly_cash(W, held, mark, k)
    order, scenarios = _level_scenarios(levels, held / C)
    zeros = [0.0] * len(order)
    wealth = W + (held * float(mark) if held > 0.0 else 0.0)  # mark checked by kelly_cash
    if not order or k <= 0.0 or wealth <= 0.0:
        return zeros
    best = _maximise_log_growth(scenarios, len(order))
    stakes = list(zeros)
    for pos, i in enumerate(order):
        stakes[i] = best[pos] * C
    return stakes


def scaled_limits_log_growth(
    levels: Sequence[PriceLevel | Sequence[float]], W: float, stakes: Sequence[float],
    held: float = 0.0,
) -> float:
    """How much dollar ``stakes`` on the child orders add to the expected log of an account
    with cash ``W`` holding ``held`` shares of the side: ``E[ln W_end] - E[ln W_end with no
    orders]`` (same model as :func:`scaled_limits`; stakes in the order the levels are given).
    For the fractional-Kelly account pass its cash, :func:`kelly_cash`."""
    W = _finite("W", W)
    if W <= 0.0:
        raise ValueError(f"W must be positive, got {W!r}")
    held = _shares("held", held)
    order, scenarios = _level_scenarios(levels, held / W)
    fractions = _stakes([s / W for s in stakes], len(order))
    return _log_growth(scenarios, [fractions[i] for i in order])


# ---------------------------------------------------------------------------------------------
# Section 4: reduce a held position with a resting sell
# ---------------------------------------------------------------------------------------------


def reduce_position(p_given_fill: float, s: float, W: float, n: float) -> float:
    """Shares to sell, with a resting sell at ``s``, out of ``n`` shares held of a side that
    wins with chance ``p_given_fill`` if that sell fills, with ``W`` dollars of cash.

    Selling ``x`` shares at ``s`` leaves ``W + n - x (1 - s)`` if the side wins and
    ``W + x s`` if it loses. Maximising ``p ln(W + n - x(1 - s)) + (1 - p) ln(W + x s)``:

        x* = [(1 - p) s (W + n) - p (1 - s) W] / [s (1 - s)]

    clipped to ``[0, n]``: never more than is held (selling more would open the other side,
    and we never hold both sides). It is section 4's hedge formula with ``b_o = 1 - s`` (a
    sale at ``s`` pays like a buy of the other side at ``1 - s``, without holding both). It
    sells more the more we hold and the further the odds have turned, and it is 0 when
    keeping the shares pays more. A resting sell may never fill; if it does not, nothing
    changes whatever ``x`` is, so the best ``x`` uses the chance given the fill. For the
    fractional-Kelly account pass its cash (:func:`kelly_cash`) as ``W``.
    """
    p = _prob("p_given_fill", p_given_fill)
    s = _price("s", s)
    W = _finite("W", W)
    n = _shares("n", n)
    if n <= 0.0:
        return 0.0
    x = ((1.0 - p) * s * (W + n) - p * (1.0 - s) * W) / (s * (1.0 - s))
    return min(max(0.0, x), n)


def sale_log_growth(p_given_fill: float, s: float, W: float, n: float, x: float) -> float:
    """How much selling ``x`` of ``n`` held shares at ``s`` adds to the expected log of an
    account with cash ``W``, given that the sale fills:
    ``p ln((W + n - x(1 - s)) / (W + n)) + (1 - p) ln((W + x s) / W)``. Times the chance the
    sale fills, it is the expected gain of resting it (no fill changes nothing)."""
    p = _prob("p_given_fill", p_given_fill)
    s = _price("s", s)
    W = _finite("W", W)
    n = _shares("n", n)
    x = _finite("x", x)
    if W <= 0.0:
        raise ValueError(f"W must be positive, got {W!r}")
    if not 0.0 <= x <= n:
        raise ValueError(f"x must be between 0 and the {n!r} shares held, got {x!r}")
    return p * math.log1p(-x * (1.0 - s) / (W + n)) + (1.0 - p) * math.log1p(x * s / W)


# ---------------------------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------------------------


def size_to_order(
    stake_usd: float, price: float, min_shares: float = 5.0, tick: float = 0.01
) -> float:
    """Shares to buy with ``stake_usd`` dollars at ``price``: rounded down to the share
    step ``tick`` (the venue takes sizes in hundredths of a share), and 0 when that is below
    the venue minimum ``min_shares``. Rounding down never stakes more than the maths asked
    for; rounding a small stake up to the minimum would."""
    stake = _finite("stake_usd", stake_usd)
    b = _price("price", price)
    step = _finite("tick", tick)
    floor = _finite("min_shares", min_shares)
    if step <= 0.0:
        raise ValueError(f"tick must be positive, got {tick!r}")
    if floor < 0.0:
        raise ValueError(f"min_shares must not be negative, got {min_shares!r}")
    if stake <= 0.0:
        return 0.0
    # The small nudge keeps float noise (4.5 / 0.45 = 9.999...) from losing a whole step.
    steps = math.floor(stake / b / step + 1e-9)
    shares = round(steps * step, 10)
    return shares if shares >= floor - 1e-12 else 0.0


# ---------------------------------------------------------------------------------------------
# Solver: maximise sum_s p_s ln(base_s + R_s . f) over f >= 0, sum(f) <= STAKE_CAP
# ---------------------------------------------------------------------------------------------


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return math.fsum(x * y for x, y in zip(a, b))


def _log_growth(scenarios: list[_Scenario], f: Sequence[float]) -> float:
    """``sum p ln((base + R . f) / base)``: the change against staking nothing."""
    total = []
    for p, base, r in scenarios:
        ratio = _dot(r, f) / base
        if ratio <= -1.0:
            return -math.inf
        total.append(p * math.log1p(ratio))
    return math.fsum(total)


def _growth_change(scenarios: list[_Scenario], f: Sequence[float], g: Sequence[float]) -> float:
    """``growth(g) - growth(f)``, computed as ``sum p ln1p(delta / wealth)`` so that tiny
    improvements near the optimum are not lost to rounding."""
    delta = [b - a for a, b in zip(f, g)]
    total = []
    for p, base, r in scenarios:
        wealth = base + _dot(r, f)
        ratio = _dot(r, delta) / wealth
        if ratio <= -1.0:
            return -math.inf
        total.append(p * math.log1p(ratio))
    return math.fsum(total)


def _gradient_hessian(
    scenarios: list[_Scenario], f: Sequence[float]
) -> tuple[list[float], list[list[float]]]:
    n = len(f)
    grad = [0.0] * n
    hess = [[0.0] * n for _ in range(n)]
    for p, base, r in scenarios:
        wealth = base + _dot(r, f)
        first = p / wealth
        second = first / wealth
        for i in range(n):
            ri = r[i]
            if ri == 0.0:
                continue
            grad[i] += first * ri
            row = hess[i]
            for j in range(i + 1):
                if r[j] != 0.0:
                    row[j] -= second * ri * r[j]
    for i in range(n):
        for j in range(i):
            hess[j][i] = hess[i][j]
    return grad, hess


def _project(v: Sequence[float], cap: float = STAKE_CAP) -> list[float]:
    """Euclidean projection onto ``{x >= 0, sum(x) <= cap}``."""
    clipped = [max(0.0, x) for x in v]
    if math.fsum(clipped) <= cap:
        return clipped
    # Onto the face sum(x) = cap (Duchi et al. 2008): shift by theta, then clip.
    theta, running = 0.0, 0.0
    for j, u in enumerate(sorted(v, reverse=True), start=1):
        running += u
        t = (running - cap) / j
        if u > t:
            theta = t
    return [max(0.0, x - theta) for x in v]


def _solve_positive_definite(a: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Solve ``a x = rhs`` by Cholesky; None when ``a`` is not safely positive definite (a
    pivot that is only rounding noise relative to its diagonal counts as singular)."""
    n = len(rhs)
    low = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = a[i][j] - math.fsum(low[i][m] * low[j][m] for m in range(j))
            if i == j:
                if not s > 1e-8 * a[i][i]:
                    return None
                low[i][i] = math.sqrt(s)
            else:
                low[i][j] = s / low[j][j]
    y = [0.0] * n
    for i in range(n):
        y[i] = (rhs[i] - math.fsum(low[i][m] * y[m] for m in range(i))) / low[i][i]
    x = [0.0] * n
    for i in reversed(range(n)):
        x[i] = (y[i] - math.fsum(low[m][i] * x[m] for m in range(i + 1, n))) / low[i][i]
    if not all(math.isfinite(v) for v in x):
        return None
    return x


def _newton_direction(hess: list[list[float]], grad: list[float], free: list[int]) -> list[float]:
    """Solve ``(-H_FF) d = g_F``, adding a small ridge if ``-H_FF`` is singular (two levels at
    one price, or identical bets that always settle together); falls back to the gradient."""
    a = [[-hess[i][j] for j in free] for i in free]
    rhs = [grad[i] for i in free]
    scale = max(1.0, max(a[i][i] for i in range(len(free))))
    ridge = 0.0
    for _ in range(12):
        m = [[a[i][j] + (ridge if i == j else 0.0) for j in range(len(free))]
             for i in range(len(free))]
        d = _solve_positive_definite(m, rhs)
        if d is not None:
            return d
        ridge = 1e-10 * scale if ridge == 0.0 else ridge * 100.0
    return rhs


def _arc_search(
    scenarios: list[_Scenario], f: list[float], grad: list[float], direction: list[float]
) -> list[float] | None:
    """Armijo backtracking along the projected arc ``P(f + t d)``; None if no step gains."""
    t = 1.0
    for _ in range(80):
        cand = _project([x + t * d for x, d in zip(f, direction)])
        predicted = _dot(grad, [c - x for c, x in zip(cand, f)])
        if predicted > 0.0 and _growth_change(scenarios, f, cand) >= 1e-4 * predicted:
            return cand
        t *= 0.5
    return None


def _maximise_log_growth(scenarios: list[_Scenario], n: int) -> list[float]:
    """Projected Newton (Bertsekas 1982) for ``max sum_s p_s ln(base_s + R_s . f)`` over
    ``f >= 0, sum(f) <= STAKE_CAP``, starting from no stake. Coordinates held at 0 whose
    gradient points below 0 are fixed for the step; the rest take a Newton step; the result
    is projected back onto the feasible set with an Armijo line search along the arc, falling
    back to a projected-gradient step if the Newton step does not gain."""
    f = [0.0] * n
    if not scenarios:
        return f
    for _ in range(_MAX_NEWTON_ITER):
        grad, hess = _gradient_hessian(scenarios, f)
        residual = max(abs(p - x) for p, x in zip(_project([x + g for x, g in zip(f, grad)]), f))
        if residual <= _KKT_TOL:
            break
        near_zero = min(1e-6, residual)
        free = [i for i in range(n) if not (f[i] <= near_zero and grad[i] <= 0.0)]
        direction = list(grad)  # held coordinates move down with the gradient: projected to 0
        if free:
            for i, d in zip(free, _newton_direction(hess, grad, free)):
                direction[i] = d
        step = _arc_search(scenarios, f, grad, direction)
        if step is None:
            step = _arc_search(scenarios, f, grad, list(grad))
        if step is None:
            break  # no ascent left at float precision
        f = step
    return f
