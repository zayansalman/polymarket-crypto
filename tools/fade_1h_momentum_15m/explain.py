"""Fade 1h Momentum on 15m: one decision explained factor by factor, in a trader's words.

The maths is tasks/2026-09-21-fade-1h-momentum-on-15m.md, sections 1-6, as implemented in
model.py. Nothing here re-derives it: every probability, drift, break-even, fill chance and
Kelly size below is a call into model.py.

The probability the 15m window closes Up is p = Phi(z), z = (y - R + Mo) / s, with
- y: the window's leg so far,
- R = (1 - e^-K) M_t: the snap-back pull on the stretch of the last 12 15m candles,
- Mo = theta * mu_hat * G: the momentum push (the blended 1h drift, faded or followed),
- s = sqrt(V + theta^2 v_hat G^2): one typical move for the time left.

The waterfall splits p - 1/2 over those three parts by exact Shapley values on Phi: the
value of a set of parts is prob_up with the other parts set to zero (s unchanged, since s
does not depend on y, M or mu), so the three contributions sum to p - 1/2 exactly and do not
depend on the order they are added in.

The resting bid b* is model.maker_best_bid, step 2's search. That search is a bounded Brent
search, which is local: where J(b) is negative at the bottom of the range and positive near the
top it can stop at 1c. So explain() also evaluates J on every 1c bid of the same range (one
vectorised model.maker_fill call) and flags a row where that beats the search. The flag is
reported beside step 2's result, in its own note (bid_search_note); it does not change what
step 2's paper book did.

Wording rules the text keeps: the stretch is a weighted average of the candles, not a sum;
sigma is the size of a typical swing, never written as '% an hour' (that unit is kept for
drifts); a bid 1c under the last trade is the top of the book's bid range, not a price rule;
where the row carries Binance's own direction, a split from the settlement is said out loud.

    from tools.fade_1h_momentum_15m.explain import explain
    ex = explain(row, params)          # row: see ``explain``; params: theta, kappa0, lam, alpha, c
    print("\\n".join(ex.summary()))
    print(ex.story())
"""
from __future__ import annotations

import math
import sys
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import combinations, permutations
from pathlib import Path

import numpy as np
from scipy.special import ndtr

try:  # imported as tools.fade_1h_momentum_15m.explain
    from . import model as fm
except ImportError:  # run or imported from this folder, as the step scripts are
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import model as fm  # type: ignore[no-redef]

PARTS = ("leg", "snap_back", "momentum")
PART_NAMES = {"leg": "the 15m leg so far", "snap_back": "the snap-back pull",
              "momentum": "the momentum push"}
STORY_GROUPS = ("Leg", "Snap-back", "Momentum", "Trade")  # the four story lines; "Result" is apart
TICK = 0.01  # step 2's bid range: [1c, price - 1c]
TICK_TOL = 1e-3  # in ticks, as step 2: a price this close under a tick is on it (tape float noise)
ON_BOUND_TOL = 1e-4  # b* this close to price - 1c sits on the search's upper end (step 2)
MISS_TOL = 1e-4  # a 1c bid beats the search when its J is higher by more than 0.01c a share bid
BARELY = 0.005  # wording only: a part moving Up by less than 0.5 points "barely matters"
FLAT_HOUR = 5e-5  # wording only: an hour's move under 0.005% reads as flat
ORD = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}


# --------------------------------------------------------------------------- Shapley


def shapley(value: Callable[[frozenset], object], players: Sequence[Hashable]) -> dict:
    """Exact Shapley values: phi_i = sum over S not holding i of |S|!(n-|S|-1)!/n! [v(S+i) - v(S)].

    value(frozenset of players) may return a float or an array (then each phi is an array).
    The values sum to value(all) - value(none).
    """
    n = len(players)
    memo: dict[frozenset, object] = {}

    def v(s) -> object:
        key = frozenset(s)
        if key not in memo:
            memo[key] = value(key)
        return memo[key]

    out = {}
    for i in players:
        others = [q for q in players if q != i]
        tot = 0.0
        for r in range(n):
            wgt = math.factorial(r) * math.factorial(n - r - 1) / math.factorial(n)
            for s in combinations(others, r):
                tot = tot + wgt * (v(set(s) | {i}) - v(s))
        out[i] = tot
    return out


def shapley_by_orders(value: Callable[[frozenset], object], players: Sequence[Hashable]) -> dict:
    """The same values the long way: each player's marginal effect averaged over all n! orders."""
    out = {i: 0.0 for i in players}
    orders = list(permutations(players))
    for order in orders:
        have: set = set()
        for i in order:
            out[i] = out[i] + (value(frozenset(have | {i})) - value(frozenset(have)))
            have.add(i)
    return {i: out[i] / len(orders) for i in players}


def coalition_value(y, M, mu, v, t, h, sigma, theta, kappa0, lam) -> Callable[[frozenset], object]:
    """value(S) = prob_up with the parts outside S switched off, minus 1/2.

    Leg off: y = 0. Snap-back off: M = 0. Momentum off: mu = 0 with v kept, so the typical move
    s = sqrt(V + theta^2 v G^2) is the same in every coalition and only the numerator changes.
    value(none) = Phi(0) - 1/2 = 0 and value(all) = p - 1/2.
    """
    y, M, mu = (np.asarray(a, float) for a in (y, M, mu))

    def value(s: frozenset):
        p = fm.prob_up(np.where("leg" in s, y, 0.0), np.where("snap_back" in s, M, 0.0),
                       np.where("momentum" in s, mu, 0.0), v, t, h, sigma, theta, kappa0, lam)
        return p - 0.5

    return value


def contributions(y, M, mu, v, t, h, sigma, theta, kappa0, lam) -> dict:
    """Shapley contribution of each part to p - 1/2 (vectorised over rows)."""
    return shapley(coalition_value(y, M, mu, v, t, h, sigma, theta, kappa0, lam), PARTS)


def snap_back_by_quarter(minute: int, kappa0: float, lam: float) -> list[float]:
    """Share of the stretch pulled back before the close, decided `minute` minutes into a window
    in each quarter of the hour (1st..4th). Does not depend on sigma."""
    h = (15 - minute) / 60.0
    return [float(fm.ou_moments((k - 1) / 4 + minute / 60.0, h, kappa0, lam, 1.0)[0]) for k in (1, 2, 3, 4)]


def bid_grid(price_j, y, t, h, sigma, mu_crowd, M, mu, v, theta, kappa0, lam, side: str) -> dict | None:
    """J(b) = P_fill(b) (p_fill(b) - b) on every 1c bid of step 2's range [1c, price_j - 1c].

    One vectorised model.maker_fill call (~1 s at a 70c price). Returns the best bid on that grid
    (b, P_fill, p_fill, J, kelly) and J one tick under the price (J_top), or None with no room.
    """
    n = int(math.floor((price_j - TICK) / TICK + TICK_TOL))
    if n < 1:
        return None
    bids = np.round(np.arange(1, n + 1) * TICK, 10)
    P, pf = fm.maker_fill(bids, y, t, h, sigma, mu_crowd, M, mu, v, theta, kappa0, lam, side=side)
    J = P * (pf - bids)
    if not np.isfinite(J).any():
        return None
    k = int(np.nanargmax(J))
    return {"b": float(bids[k]), "P_fill": float(P[k]), "p_fill": float(pf[k]), "J": float(J[k]),
            "kelly": float(fm.kelly_maker(pf[k], bids[k])), "top": float(bids[-1]), "J_top": float(J[-1])}


# --------------------------------------------------------------------------- formatting


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _ok(x) -> bool:
    return math.isfinite(_f(x))


def pct(x, nd: int = 3) -> str:
    """Log return (or drift) as a signed percent, to 3 decimals by default (BTC legs are a few
    hundredths of a percent, so 2 decimals hides the number that drives the card)."""
    return f"{100 * x:+.{nd}f}%"


def cents(p) -> str:
    """A price in cents: whole cents plain, anything else to 0.1c."""
    v = 100 * _f(p)
    if not math.isfinite(v):
        return "none"
    return f"{v:.0f}c" if abs(v - round(v)) < 0.05 else f"{v:.1f}c"


def points(x) -> str:
    return f"{100 * abs(x):.1f} points"


def _moves_up(x: float) -> str:
    """What one part does to Up, with its direction always stated."""
    if abs(x) < BARELY:
        if 100 * abs(x) < 0.05:
            return "barely moves Up (under 0.1 points)"
        return (f"adds only {points(x)} to Up, so it barely matters" if x > 0
                else f"takes only {points(x)} off Up, so it barely matters")
    return f"adds {points(x)} to Up" if x > 0 else f"takes {points(x)} off Up"


def _hhmm(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), UTC).strftime("%H:%M")


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def swing(coin: str, sigma: float) -> str:
    """sigma as a size, never as a speed or direction: '% an hour' is kept for drifts."""
    return f"{coin}'s typical one-hour swing of {100 * sigma:.3f}% (up or down, realised over the last 60 min)"


def feeds_text(outcome: Mapping | None) -> str:
    """Which way the settlement feed and the maths' feed closed the window, with a split said out loud.
    Empty when the row does not carry Binance's direction."""
    if not outcome or outcome.get("binance_winner") is None:
        return ""
    w, bw = outcome["winner"], outcome["binance_winner"]
    if outcome["feeds_split"]:
        return (f"The feeds split: Binance, which the maths reads, closed {bw}; Chainlink, which settles the "
                f"market, closed {w}.")
    return f"Binance, which the maths reads, and Chainlink, which settles the market, both closed {w}."


def crowd_reading(x: float, m_H: float, hour_alone: float) -> str:
    """What the 1h price says against the hour's move so far, as a clause starting 'so the crowd'.

    'part of the rise/drop' only when m_H sits between the hour-alone price and 50c; on the other
    side of 50c from the hour's move the crowd expects the move more than reversed.
    """
    gap = m_H - hour_alone
    if abs(gap) < 0.01:
        return "so the crowd prices about no further move"
    if abs(x) < FLAT_HOUR:
        return f"so the crowd expects the hour to go {'up' if gap > 0 else 'down'}"
    move = "rise" if x > 0 else "drop"
    if (gap > 0) == (x > 0):
        return f"so the crowd expects the {move} to extend"
    back = "given back" if x > 0 else "won back"
    if abs(m_H - 0.5) < 0.005:
        return f"so the crowd expects all of the {move} to be {back}"
    if (m_H > 0.5) == (x > 0):
        return f"so the crowd expects part of the {move} to be {back}"
    return (f"so the crowd expects the {move} to be more than {back}, with the hour finishing "
            f"{'Down' if x > 0 else 'Up'}")


# --------------------------------------------------------------------------- explanation


@dataclass
class Explanation:
    """What explain() returns: inputs, waterfall, decision, outcome, and the story."""

    inputs: dict
    waterfall: list[dict]
    decision: dict
    outcome: dict | None
    contributions: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"inputs": self.inputs, "waterfall": self.waterfall, "decision": self.decision,
                "outcome": self.outcome, "contributions": self.contributions}

    # ---------------------------------------------------------------- story

    def story(self) -> str:
        """The story as one paragraph, generated from the numbers only."""
        return " ".join(self.sentences())

    def sentences(self) -> list[str]:
        """5-8 short items in a trader's voice: leg, snap-back, momentum (1-2), trade (2), result."""
        return [text for _, text in self._parts()]

    def story_lines(self) -> list[tuple[str, str]]:
        """The story as four short lines: (Leg, Snap-back, Momentum, Trade). The result is in summary()."""
        parts = self._parts()
        return [(g, " ".join(t for gg, t in parts if gg == g)) for g in STORY_GROUPS]

    def summary(self, focus: str | None = None) -> list[str]:
        """2-3 lines for the top of a card: what pushed Up or Down, the maths against the market
        price and what it did, and what happened. focus (a part name) puts that part first, with
        why it moved Up, for a card picked to show that part."""
        i, d, c = self.inputs, self.decision, self.contributions
        side, mk = d["side"], d.get("maker")
        names = {"leg": f"the {pct(i['leg'])} 15m leg", "snap_back": "the snap-back",
                 "momentum": "the momentum"}
        big = [k for k in sorted(PARTS, key=lambda q: -abs(c[q])) if 100 * abs(c[k]) >= 0.05]
        if focus in PARTS:
            big = [focus] + [k for k in big if k != focus]

        def verb(k: str, full: bool = False) -> str:
            n = f"{100 * abs(c[k]):.1f}"
            if full:
                return f"adds {n} points to Up" if c[k] > 0 else f"takes {n} points off Up"
            return f"adds {n}" if c[k] > 0 else f"takes {n} off"

        if not big:
            first = "Nothing moves Up off 50%."
        elif focus in PARTS:
            first = f"{_cap(names[focus])} {verb(focus, True)}: {self._why(focus)}."
            if big[1:]:
                first += " " + _cap(" and ".join(f"{names[k]} {verb(k)}" for k in big[1:])) + "."
        else:
            k0 = big[0]
            first = f"{_cap(names[k0])} {verb(k0, True)}"
            rest = [f"{names[k]} {verb(k)}" for k in big[1:]]
            first += (", " + " and ".join(rest) if rest else "") + "."
        price_j = d["market_price"][side]
        acts = [self._taker_short()]
        if mk is not None:
            acts.append(self._maker_short())
        second = (f"The maths makes {side} {100 * d['p_side']:.1f}% against {side}'s {cents(price_j)} market "
                  f"price: " + ", and ".join(a for a in acts if a) + ".")
        out = [first, second]
        if self.outcome is not None:
            out.append(self._result_short())
        return out

    def _why(self, k: str) -> str:
        """Why one part moved Up, in a clause (for a card that focuses on that part)."""
        i = self.inputs
        if k == "leg":
            way = "up" if i["leg"] > 0 else "down" if i["leg"] < 0 else "flat"
            return (f"{i['coin']} is {way} {pct(abs(i['leg']))[1:]} with {i['minutes_left']} minutes left, "
                    f"{abs(i['leg_in_typical_moves']):.2f} typical moves for the time left")
        if k == "snap_back":
            return (f"weighted toward the newest, the last twelve 15m candles average a stretch of "
                    f"{pct(i['stretch'])}, and in a {ORD[i['quarter']]}-quarter window the maths expects "
                    f"{100 * i['snap_back_fraction']:.0f}% of it back before the close")
        how = "fades" if i["theta"] < 0 else "follows" if i["theta"] > 0 else "ignores"
        if _ok(i["m_H"]):
            return (f"the 1h market pays {cents(i['m_H'])} for Up against {cents(i['hour_alone_price_up'])} from "
                    f"the hour's move alone, which pulls the blended momentum to {pct(i['mu'])} an hour, and the "
                    f"maths {how} it")
        return f"the trailing hour on spot is {pct(i['mu_L'])} an hour and the maths {how} it"

    def _taker_short(self) -> str:
        d = self.decision
        side, tk = d["side"], d["taker"]
        if not _ok(tk["ask"]):
            return f"no one bought {side} in the minute before, so there is no ask to take"
        if not tk["decided"]:
            return f"{side}'s {cents(tk['ask'])} ask is at or over its {cents(tk['break_even'])} break-even, so no taker buy"
        if tk["filled"]:
            return f"it buys {side} at {cents(tk['fill_price'])} as a taker"
        return f"its taker buy limited at {cents(tk['break_even'])} does not fill"

    def _maker_short(self) -> str:
        mk = self.decision["maker"]
        side = self.decision["side"]
        if mk["quoted"]:
            return f"it rests a bid on {side} at {cents(mk['bid'])}"
        if not _ok(mk["bid"]):
            return f"there is no room for a bid under {side}'s price"
        if mk["search_missed"]:
            return "the paper book rests no bid"
        return "no bid is worth resting"

    def _result_short(self) -> str:
        o, d = self.outcome, self.decision
        tk, mk = d["taker"], d.get("maker")
        bits = []
        if tk["filled"]:
            v = o["taker_pnl"]
            bits.append(f"the taker buy {'made' if v >= 0 else 'lost'} {100 * abs(v):.1f}c a share")
        if mk is not None and mk["quoted"]:
            if mk["filled"]:
                v = o["maker_pnl"]
                bits.append(f"the filled bid {'made' if v >= 0 else 'lost'} {100 * abs(v):.1f}c a share")
            else:
                bits.append("the resting bid did not fill")
        s = f"{o['winner']} won: " + (" and ".join(bits) if bits else "the maths had no position") + "."
        return s + (" " + feeds_text(o) if o.get("feeds_split") else "")

    def bid_search_note(self) -> str | None:
        """The known gap in step 2's bid search on this row, as its own note, or None.

        Step 2's paper book rests nothing where its local search misses; a check of every 1c bid
        finds a bid with a positive expected profit. Reported beside the book, never as its trade."""
        mk = self.decision.get("maker")
        if mk is None or not mk["search_missed"] or mk["grid"] is None:
            return None
        g, side = mk["grid"], self.decision["side"]
        s = (f"Known gap in the paper book's bid search: a check of every 1c bid finds a {cents(g['b'])} {side} "
             f"bid with a {100 * g['P_fill']:.1f}% fill chance, {side} at {100 * g['p_fill']:.1f}% if filled, and "
             f"{100 * g['J']:+.2f}c expected per share bid. The search missed it")
        s += (f", and the book rested only its own {cents(mk['bid'])} bid." if mk["quoted"]
              else ", and the book rested nothing.")
        o = self.outcome
        if o is not None:
            low = mk["lowest_later_print"]
            low_s = cents(low) if _ok(low) else "none"
            s += (f" In hindsight it would have filled (lowest later {side} price: {low_s}) and made "
                  f"{100 * o['missed_bid_pnl']:+.1f}c a share." if mk["grid_filled"]
                  else f" In hindsight it would not have filled (lowest later {side} price: {low_s}).")
        return s

    def _parts(self) -> list[tuple[str, str]]:
        """(story group, text) in reading order."""
        i, d, c = self.inputs, self.decision, self.contributions
        coin, y = i["coin"], i["leg"]
        out: list[tuple[str, str]] = []

        # 1. the leg (sigma is a size: '% an hour' is kept for drifts)
        hh, q = _hhmm(i["window_start_ts"]), ORD[i["quarter"]]
        if i["minutes_gone"] == 0:
            out.append(("Leg", f"{coin}'s {hh} window ({q} quarter of the hour) has just opened, so there is "
                               f"no 15m leg yet. A typical move for the time left is {100 * i['typical_move']:.3f}%, "
                               f"from {swing(coin, i['sigma_per_hour'])}."))
        else:
            way = f"up {pct(y)[1:]}" if y > 0 else f"down {pct(y)[1:]}" if y < 0 else "flat"
            out.append(("Leg", f"{coin} is {way} since the {hh} window opened, with {i['minutes_left']} of 15 "
                               f"minutes left ({q} quarter of the hour). A typical move for the time left is "
                               f"{100 * i['typical_move']:.3f}%, from {swing(coin, i['sigma_per_hour'])}, so the "
                               f"leg is {abs(i['leg_in_typical_moves']):.2f} typical moves, which "
                               f"{_moves_up(c['leg'])}."))

        # 2. the snap-back: the stretch is a weighted average, and the newest candle carries the most
        r0, w0, part0 = i["candles"][0], i["candle_weights"][0], i["candle_stretch_parts"][0]
        typ15 = i["sigma_per_hour"] * math.sqrt(0.25)
        fast = abs(r0) / typ15 if typ15 > 0 else 0.0
        size = f" ({fast:.1f} typical 15m moves)" if fast >= 1.0 else ""
        byq = i["snap_back_by_quarter"]
        share = i["snap_back_fraction"]
        where = ""
        if max(byq) - min(byq) > 0.005:
            if abs(share - max(byq)) < 1e-12:
                where = ", where the snap-back is strongest"
            elif abs(share - min(byq)) < 1e-12:
                where = ", where the snap-back is weakest"
        out.append(("Snap-back", f"Weighted toward the newest, the last twelve 15m candles average a stretch of "
                                 f"{pct(i['stretch'])}. The latest candle, {pct(r0)}{size}, carries "
                                 f"{100 * w0:.0f}% of the weight and alone gives {pct(part0)}. In a {q}-quarter "
                                 f"window{where}, the maths expects {100 * share:.0f}% of the stretch back before "
                                 f"the close, which {_moves_up(c['snap_back'])}."))

        # 3. the momentum push
        th = i["theta"]
        how = (f"the maths fades it (weight {th:+.3f})" if th < 0 else
               f"the maths follows it (weight {th:+.3f})" if th > 0 else "the maths gives it no weight")
        push = f"{how}, which {_moves_up(c['momentum'])}."
        if _ok(i["m_H"]):
            x = i["hour_leg"]
            hour = "The hour is flat so far" if abs(x) < FLAT_HOUR else \
                f"The hour is {'up' if x > 0 else 'down'} {pct(abs(x))[1:]} so far"
            crowd = crowd_reading(x, i["m_H"], i["hour_alone_price_up"])
            out.append(("Momentum", f"{hour}, which on its own prices Up at {cents(i['hour_alone_price_up'])}; "
                                    f"the 1h market pays {cents(i['m_H'])}, {crowd} ({pct(i['mu_H'])} an hour "
                                    f"for the rest of the hour)."))
            out.append(("Momentum", f"Blended {100 * i['w_H']:.0f}/{100 * i['w_L']:.0f} with the trailing hour on "
                                    f"spot ({pct(i['mu_L'])} an hour), momentum is {pct(i['mu'])} an hour; {push}"))
        else:
            out.append(("Momentum", f"There is no 1h market trade in the last minute, so momentum is the trailing "
                                    f"hour on spot alone, {pct(i['mu_L'])} an hour; {push}"))

        # 4. net and the taker order: one short sentence (the details are in the decision table)
        side, tk = d["side"], d["taker"]
        net = f"Net: Up {100 * d['p_up']:.1f}%, Down {100 * d['p_down']:.1f}%."
        a_star = cents(tk["break_even"])
        if not _ok(tk["ask"]):
            act = f"No one bought {side} in the minute before: no ask to take."
        elif not tk["decided"]:
            act = f"{side}'s {cents(tk['ask'])} ask is at or over its {a_star} break-even: no taker buy."
        elif tk["filled"]:
            act = f"Buys {side} at {cents(tk['fill_price'])} against a {a_star} break-even."
        else:
            act = (f"Sends a buy of {side} limited at its {a_star} break-even; the next taker buy is "
                   f"{cents(tk['fill_price'])}, so it does not fill.")
        out.append(("Trade", f"{net} {act}"))

        # 5. the resting bid: one short sentence
        mk = d.get("maker")
        if mk is not None:
            g = mk["grid"]
            if mk["quoted"]:
                where_b = ("1c under the last trade" if mk["on_upper_bound"]
                           else "where fill chance times edge peaks")
                out.append(("Trade", f"Rests a bid on {side} at {cents(mk['bid'])}, {where_b}."))
            elif not _ok(mk["bid"]):
                out.append(("Trade", f"No room to rest a bid under {side}'s {cents(mk['price'])} price."))
            elif mk["search_missed"]:
                out.append(("Trade", "The paper book rests no bid (see the note on its bid search)."))
            elif g is not None:
                out.append(("Trade", f"Rests no bid: a bid fills only after {side} has fallen to it, and then the "
                                     f"maths values {side} at or below the bid, at every bid from 1c to "
                                     f"{cents(g['top'])}."))
            else:
                out.append(("Trade", f"Rests no bid: the bid search finds none on {side} with a positive expected "
                                     f"profit."))

        # 6. the result
        o = self.outcome
        if o is not None:
            parts = []
            if tk["filled"]:
                parts.append(f"the taker buy at {cents(tk['fill_price'])} made {100 * o['taker_pnl']:+.1f}c a "
                             f"share after the {100 * tk['fee']:.2f}c fee")
            if mk is not None and mk["quoted"]:
                parts.append(f"the resting bid filled and made {100 * o['maker_pnl']:+.1f}c a share"
                             if mk["filled"] else "the resting bid did not fill")
            tail = ("; " + ", and ".join(parts)) if parts else "; the maths had no position"
            feeds = feeds_text(o) if o.get("feeds_split") else ""
            out.append(("Result", f"Result: {o['winner']} won{tail}." + (f" {feeds}" if feeds else "")))
        return out

def explain(row: Mapping, params: Mapping, *, maker: bool | Mapping = True,
            bid_check: bool = True) -> Explanation:
    """One decision on one coin's 15m window, explained.

    row (one step-2 row; NaN or missing where the tape has nothing):
      coin, t0 (window open, unix s), minute (minutes into the window at the decision),
      y (window's log move so far), x (hour's log move so far), sigma (per sqrt hour),
      prev (12 previous 15m log returns, newest first), mu_L (trailing 1h log return),
      m_H (1h market's Up price), vH, vL, cHL (blend error moments, section 4),
      market_price_up (15m market's last Up-equivalent print),
      quote_up / quote_dn (ask at t: last taker buy of that side in [t - 60, t)),
      ask_up / ask_dn (first taker buy of that side in [t, t + 30]),
      later_min_up / later_max_up (lowest / highest Up-equivalent print after t, to the end),
      up (1 Up won, 0 Down won, on the settlement feed, Chainlink; optional),
      binance_up (1 where Binance, the maths' feed, closed the window Up; optional).
    params: theta, kappa0, lam, alpha, c.
    maker: True computes b* (model.maker_best_bid, ~0.5 s), False skips it, or a mapping with
      b, P_fill, p_fill, J, kelly already computed for this row's favoured side.
    bid_check: with maker=True, also evaluate J on every 1c bid of the search's range (~1 s) and
      flag the row where the best of those beats the search (decision["maker"]["search_missed"]).

    Conventions are step 2's (step2_polymarket.py): t and h from the window and minute, v floored
    at 0, side from sign(p - 1/2) (ties Up), a taker buy limited at a*, the maker's crowd drift
    implied by the 15m price, a bid filled by a later print strictly below it.
    """
    prm = {k: float(params[k]) for k in fm.PARAM_NAMES}
    theta, kappa0, lam, alpha, cc = (prm[k] for k in fm.PARAM_NAMES)
    t0, minute = int(row["t0"]), int(row["minute"])
    t = ((t0 % 3600) + 60 * minute) / 3600.0
    h = (15 - minute) / 60.0
    quarter = (t0 % 3600) // 900 + 1
    y, x, sigma = _f(row["y"]), _f(row["x"]), _f(row["sigma"])
    prev = np.asarray(row["prev"], float)
    m_H = _f(row.get("m_H"))

    M = float(fm.stretch(prev, alpha, cc))
    per_candle = np.asarray(fm.stretch(np.diag(prev), alpha, cc), float)  # candle j alone
    # each candle's weight, from the model: a tiny move is not capped, so its part / its size = w_j
    eps = 1e-12
    weights = np.asarray(fm.stretch(np.eye(prev.size) * eps, alpha, cc), float) / eps
    snap_frac, G, V = (float(q) for q in fm.ou_moments(t, h, kappa0, lam, sigma))
    mu_H = float(fm.mu_hat_H(m_H, x, sigma, t))  # NaN without a 1h print
    mu_L = float(fm.mu_hat_L(_f(row["mu_L"])))
    vH, vL, cHL = _f(row["vH"]), _f(row["vL"]), _f(row["cHL"])
    mu, v = (float(q) for q in fm.blend(mu_H, mu_L, vH, vL, cHL))
    v = max(v, 0.0)  # step 2 floors the blended error variance at 0
    w_H = float(fm.blend(1.0, 0.0, vH, vL, cHL)[0]) if math.isfinite(mu_H) else 0.0
    p = float(fm.prob_up(y, M, mu, v, t, h, sigma, theta, kappa0, lam))

    pull = snap_frac * M
    push = theta * mu * G
    s = math.sqrt(V + theta * theta * v * G * G)  # typical move for the time left (reporting)
    z = (y - pull + push) / s
    contrib = {k: float(val) for k, val in contributions(y, M, mu, v, t, h, sigma, theta, kappa0, lam).items()}

    inputs = {
        "coin": str(row["coin"]).upper(), "window_start_ts": t0, "window_end_ts": t0 + 900,
        "decision_ts": t0 + 60 * minute, "quarter": int(quarter), "minutes_gone": minute,
        "minutes_left": 15 - minute, "t_hour": t, "h_hours": h,
        "leg": y, "typical_move": s, "leg_in_typical_moves": y / s,
        "sigma_per_hour": sigma, "brownian_move_for_time_left": sigma * math.sqrt(h),
        "candles": prev.tolist(), "candle_weights": weights.tolist(),
        "candle_stretch_parts": per_candle.tolist(), "stretch": M,
        "last3_sum": float(prev[:3].sum()),
        "K": float(-math.log1p(-snap_frac)) if snap_frac < 1 else float("inf"),
        "snap_back_fraction": snap_frac, "snap_back_pull": pull,
        "snap_back_by_quarter": snap_back_by_quarter(minute, kappa0, lam),
        "hour_leg": x, "hour_alone_price_up": float(fm.martingale_prob_up(x, 1.0 - t, sigma)),
        "m_H": m_H, "mu_H": mu_H, "mu_L": mu_L, "w_H": w_H, "w_L": 1.0 - w_H,
        "mu": mu, "v": v, "mu_sd": math.sqrt(v), "vH": vH, "vL": vL, "cHL": cHL,
        "theta": theta, "theta_reads": "fades" if theta < 0 else "follows" if theta > 0 else "off",
        "G_hours": G, "momentum_push": push, "V": V,
        "z": z, "z_parts": {"leg": y / s, "snap_back": -pull / s, "momentum": push / s},
        "params": prm,
    }
    waterfall, run = [{"step": "start", "points": 0.0, "p_up": 0.5}], 0.5
    for k in PARTS:
        run += contrib[k]
        waterfall.append({"step": PART_NAMES[k], "part": k, "points": contrib[k], "p_up": run})
    waterfall.append({"step": "net", "points": p - 0.5, "p_up": p})

    # ---- decision (section 6, step 2's execution)
    up_side = p >= 0.5
    side = "Up" if up_side else "Down"
    p_side = p if up_side else 1.0 - p
    price_up = _f(row["market_price_up"])
    price_j = price_up if up_side else 1.0 - price_up
    quote = {"Up": _f(row.get("quote_up")), "Down": _f(row.get("quote_dn"))}
    first = {"Up": _f(row.get("ask_up")), "Down": _f(row.get("ask_dn"))}
    be = {"Up": float(fm.taker_break_even(p)), "Down": float(fm.taker_break_even(1.0 - p))}
    ask = quote[side]
    a_star = be[side]
    decided = _ok(ask) and ask < a_star
    from_print = _ok(first[side])
    fill_price = first[side] if from_print else ask
    filled = decided and _ok(fill_price) and fill_price <= a_star + 1e-12
    at = fill_price if filled else ask
    taker = {
        "ask": ask, "break_even": a_star, "fee_at_break_even": float(fm.taker_fee(a_star)),
        "decided": bool(decided), "fill_price": fill_price if decided else float("nan"),
        "fill_from_print": bool(from_print), "filled": bool(filled), "limit_miss": bool(decided and not filled),
        "fee": float(fm.taker_fee(at)) if _ok(at) else float("nan"),
        "ev_per_share": float(fm.taker_ev(p_side, at)) if _ok(at) else float("nan"),
        "kelly": float(fm.kelly_taker(p_side, at)) if _ok(at) else 0.0,
    }
    decision = {"p_up": p, "p_down": 1.0 - p, "side": side, "p_side": p_side,
                "market_price": {"Up": price_up, "Down": 1.0 - price_up},
                "ask_at_t": quote, "first_print_after_t": first, "break_even": be, "taker": taker}

    if maker is not False:
        grid = None
        mk_side = "up" if up_side else "down"
        if isinstance(maker, Mapping):
            res = {k: _f(maker[k]) for k in ("b", "P_fill", "p_fill", "J", "kelly")}
        else:
            mu_crowd = float(fm.implied_drift(price_up, y, sigma, h))  # step 2: drift of the 15m price
            r = fm.maker_best_bid(price_j, y, t, h, sigma, mu_crowd, M, mu, v, theta, kappa0, lam, side=mk_side)
            res = {k: float(r[k]) for k in ("b", "P_fill", "p_fill", "J", "kelly")}
            if bid_check and _ok(price_j):
                grid = bid_grid(price_j, y, t, h, sigma, mu_crowd, M, mu, v, theta, kappa0, lam, mk_side)
        b = res["b"]
        quoted = _ok(b) and res["J"] > 0 and b >= TICK - 1e-12
        later = _f(row.get("later_min_up")) if up_side else 1.0 - _f(row.get("later_max_up"))
        search_j = res["J"] if quoted else 0.0
        missed = grid is not None and grid["J"] > MISS_TOL and grid["J"] > search_j + MISS_TOL
        decision["maker"] = {
            "price": price_j, "bid": b, "P_fill": res["P_fill"], "p_fill": res["p_fill"],
            "J": res["J"], "kelly": res["kelly"], "quoted": bool(quoted),
            "on_upper_bound": bool(_ok(b) and abs(b - (price_j - TICK)) < ON_BOUND_TOL),
            "lowest_later_print": later,
            "filled": bool(quoted and _ok(later) and later < b - 1e-12),
            "grid": grid, "search_missed": bool(missed),
            # a 1c bid fills on a print below it by more than the tape's float noise (step 2's rule)
            "grid_filled": bool(missed and _ok(later) and later < grid["b"] - TICK_TOL * TICK)}

    outcome = None
    up = _f(row.get("up"))
    if math.isfinite(up):
        winner = "Up" if up >= 0.5 else "Down"
        win = 1.0 if winner == side else 0.0
        outcome = {"winner": winner, "side_won": bool(win), "taker_pnl": float("nan"),
                   "maker_pnl": float("nan"), "taker_pnl_per_100_at_kelly": float("nan"),
                   "maker_pnl_per_100_at_kelly": float("nan"), "missed_bid_pnl": float("nan"),
                   "binance_winner": None, "feeds_split": False}
        bup = _f(row.get("binance_up"))
        if math.isfinite(bup):  # the maths reads Binance; the market settles on Chainlink
            outcome["binance_winner"] = "Up" if bup >= 0.5 else "Down"
            outcome["feeds_split"] = outcome["binance_winner"] != winner
        if filled:
            pnl = win - fill_price - taker["fee"]
            outcome["taker_pnl"] = pnl
            outcome["taker_pnl_per_100_at_kelly"] = 100 * taker["kelly"] * pnl / (fill_price + taker["fee"])
        mk = decision.get("maker")
        if mk is not None and mk["filled"]:
            outcome["maker_pnl"] = win - mk["bid"]
            outcome["maker_pnl_per_100_at_kelly"] = 100 * mk["kelly"] * (win - mk["bid"]) / mk["bid"]
        if mk is not None and mk["grid_filled"]:
            outcome["missed_bid_pnl"] = win - mk["grid"]["b"]

    # the waterfall ends on the model's p, s is the model's own denominator, the candles' parts add
    # up to M (a weighted average: the weights add up to 1)
    if abs(sum(contrib.values()) - (p - 0.5)) > 1e-9 or abs(float(ndtr(z)) - p) > 1e-9 \
            or abs(float(per_candle.sum()) - M) > 1e-12 or abs(float(weights.sum()) - 1.0) > 1e-9:
        raise RuntimeError("explanation does not reproduce model.prob_up")
    return Explanation(inputs=inputs, waterfall=waterfall, decision=decision, outcome=outcome,
                       contributions=contrib)
