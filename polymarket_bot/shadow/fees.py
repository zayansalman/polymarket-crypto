"""Polymarket taker-fee math for the shadow forward-tester.

Pure functions, stdlib only. The shadow tester settles each candidate
strategy's would-be trade net of the Polymarket taker fee, so every
PnL number flows through :func:`net_pnl_per_share`. The fee is charged
on entry (per share bought) regardless of how the market resolves.

Fee model (Polymarket): ``fee_per_share(p) = fee_rate * p * (1 - p)``,
a symmetric parabola peaking at ``p = 0.5`` and vanishing at the
certain prices ``p = 0`` and ``p = 1``. With the default 7% rate the
worst-case fee is ``0.07 * 0.25 = 0.0175`` (1.75 cents) per share.
"""

from __future__ import annotations


def taker_fee_per_share(price: float, fee_rate: float = 0.07) -> float:
    """Polymarket taker fee paid per share at entry price ``price``.

    The fee is ``fee_rate * price * (1 - price)``: zero at the certain
    prices (0 and 1) and maximal at 0.5. It is charged on entry and does
    not depend on the eventual outcome.
    """
    return fee_rate * price * (1.0 - price)


def net_pnl_per_share(entry_price: float, won: bool, fee_rate: float = 0.07) -> float:
    """Net PnL per share, after the entry taker fee.

    A winning share pays out 1.0 against the ``entry_price`` cost, so the
    gross is ``1 - entry`` on a win and ``-entry`` on a loss. The taker
    fee is subtracted in both cases because it is charged at entry.
    """
    gross = (1.0 - entry_price) if won else -entry_price
    return gross - taker_fee_per_share(entry_price, fee_rate)


def breakeven_winrate(entry_price: float, fee_rate: float = 0.07) -> float:
    """Win-rate at which expected net PnL per share is exactly zero.

    Let ``p = entry_price`` and ``fee = taker_fee_per_share(p)``. With a
    win-rate ``w`` the expected net PnL per share is

        EV(w) = w * net_pnl(won=True)  + (1 - w) * net_pnl(won=False)
              = w * ((1 - p) - fee)    + (1 - w) * (-p - fee)
              = w * (1 - p) - (1 - w) * p - fee      # the ±fee terms sum to -fee
              = w - p - fee.

    Setting ``EV(w) = 0`` gives the breakeven win-rate

        w* = p + fee = p + fee_rate * p * (1 - p).

    Because ``fee > 0`` for any ``0 < p < 1``, ``w*`` is strictly greater
    than the entry price ``p``: the fee raises the win-rate you must clear
    to break even above the naive "win more often than you pay" bar.
    """
    return entry_price + taker_fee_per_share(entry_price, fee_rate)
