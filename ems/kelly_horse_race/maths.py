"""The maths of Kelly horse-race: the chance of Up, the two draws, and the size. Pure: no I/O.

The chance of Up. A 15m window settles Up when the Chainlink TWAP-60s print at the close is at
least the print at the open, ``K``. Read the window as a binary option on that print with the
last hour's move carried forward as the drift (research note section 5; arXiv 2606.19517 with
a forecast drift in place of the risk-neutral one)::

    P(Up) = Phi( (ln(X / K) + r60 * tau) / (sigma_h * sqrt(tau)) )

``X`` is the TWAP-60s print now, ``r60`` the last hour's log return and ``sigma_h`` its
realised volatility per square-root hour (both from sixty 1-minute Binance returns), ``tau``
the hours left. With no volatility (or no time) left, P(Up) is 1 or 0 by the sign of the
numerator, and 0.5 when that is zero too. More volatility pulls P(Up) toward 0.5.

The side: the die. Draw ``u1`` uniform on [0, 1) and buy Up if ``u1 < P(Up)``, else Down. Over
many windows the stake lands on each side in proportion to its chance: Kelly's horse-race
split ``b = p`` (Kelly 1956; arXiv 1901.06278 Prop. 2), one window at a time.

The size: a random notional. Draw ``u2`` uniform on [0, 1); the shares are one of the counts,
in hundredths of a share, from the venue's minimum order up to the most the notional cap buys at
the price (``floor(max_notional / price)``), each equally likely. At a fixed price that is a
uniform notional between ``min_order_size * price`` and the cap. When the minimum order costs
more than the cap there is no order.

Both draws are passed in, so the maths is testable with fixed numbers; the runner draws them
from ``random.SystemRandom`` and stores them with the decision.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

UP = "Up"
DOWN = "Down"
SHARE_STEP = 0.01
MINUTE_RETURNS = 60

_NORMAL = NormalDist()
_EPS = 1e-9


@dataclass(frozen=True)
class Chance:
    """P(Up) and the pieces it came from, for the record and the card."""

    p_up: float
    log_moneyness: float  # ln(X / K)
    drift: float  # r60 * tau
    stdev: float  # sigma_h * sqrt(tau)
    z: float | None  # (log_moneyness + drift) / stdev; None with no volatility left


@dataclass(frozen=True)
class Size:
    shares: float
    notional_usd: float
    min_shares: float
    max_shares: float


def _finite(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return number


def hour_moves(minute_returns: tuple[float, ...] | list[float]) -> tuple[float, float]:
    """``(r60, sigma_h)`` from the last hour's sixty 1-minute log returns (any order).

    ``r60`` is their sum, ``ln(c60 / c0)``; ``sigma_h`` the square root of the sum of their
    squares, the realised volatility per square-root hour.
    """
    returns = [_finite("a minute return", r) for r in minute_returns]
    if len(returns) != MINUTE_RETURNS:
        raise ValueError(f"need {MINUTE_RETURNS} minute returns, got {len(returns)}")
    return math.fsum(returns), math.sqrt(math.fsum(r * r for r in returns))


def chance_of_up(x: float, k: float, r60: float, sigma_h: float, tau: float) -> Chance:
    """P(Up) for a window whose opening print is ``k``, with ``tau`` hours left."""
    x, k = _finite("X", x), _finite("K", k)
    r60, sigma_h, tau = _finite("r60", r60), _finite("sigma_h", sigma_h), _finite("tau", tau)
    if x <= 0 or k <= 0:
        raise ValueError(f"prices must be positive, got X={x!r} K={k!r}")
    if sigma_h < 0 or tau < 0:
        raise ValueError(f"volatility and time left must be >= 0, got {sigma_h!r}, {tau!r}")
    moneyness = math.log(x / k)
    drift = r60 * tau
    stdev = sigma_h * math.sqrt(tau)
    numerator = moneyness + drift
    if stdev <= 0.0:
        p = 1.0 if numerator > 0 else 0.0 if numerator < 0 else 0.5
        return Chance(p, moneyness, drift, 0.0, None)
    z = numerator / stdev
    return Chance(_NORMAL.cdf(z), moneyness, drift, stdev, z)


def pick_side(p_up: float, u1: float) -> str:
    """The die: Up if ``u1 < p_up``, else Down."""
    p_up, u1 = _finite("P(Up)", p_up), _finite("u1", u1)
    if not 0.0 <= p_up <= 1.0:
        raise ValueError(f"P(Up) must be in [0, 1], got {p_up!r}")
    if not 0.0 <= u1 < 1.0:
        raise ValueError(f"u1 must be in [0, 1), got {u1!r}")
    return UP if u1 < p_up else DOWN


def draw_size(price: float, min_order_size: float, max_notional_usd: float,
              u2: float) -> Size | None:
    """Shares for one order, drawn uniformly between the minimum order and the cap.

    None when the minimum order already costs more than ``max_notional_usd``.
    """
    price = _finite("price", price)
    min_shares = _finite("min_order_size", min_order_size)
    cap = _finite("max_notional_usd", max_notional_usd)
    u2 = _finite("u2", u2)
    if not 0.0 < price < 1.0:
        raise ValueError(f"price must be in (0, 1), got {price!r}")
    if min_shares <= 0 or cap <= 0:
        raise ValueError("the minimum order and the cap must be positive")
    if not 0.0 <= u2 < 1.0:
        raise ValueError(f"u2 must be in [0, 1), got {u2!r}")
    low = math.ceil(min_shares / SHARE_STEP - _EPS)  # in hundredths of a share
    high = math.floor(cap / price / SHARE_STEP + _EPS)
    if high < low:
        return None
    # Every hundredth of a share from the minimum to the cap, both included, equally likely.
    steps = low + min(int(u2 * (high - low + 1)), high - low)
    shares = round(steps * SHARE_STEP, 6)
    return Size(shares=shares, notional_usd=round(shares * price, 6),
                min_shares=round(low * SHARE_STEP, 6), max_shares=round(high * SHARE_STEP, 6))
