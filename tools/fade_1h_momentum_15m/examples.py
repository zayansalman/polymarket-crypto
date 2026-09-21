"""Fade 1h Momentum on 15m: real worked examples from the step-2 tape, explained factor by factor.

Rebuilds step 2's rows (step2_polymarket.build_rows, ~7 s), prices them with the parameters
step 2 is frozen on (or --params), and picks one real decision per scenario at the headline
decision minute by a fixed criterion: the first qualifying row in time order that is not
already shown. Most filters use only what is known at the decision; the header says which ones
look at what happened after it (a won or lost taker buy, a filled or unfilled bid, a missed
limit). Each pick is explained by explain.py and written as a card to
data/fade_1h_momentum_15m/worked_examples.md.

Step 2's resting bid on every headline row (to count and pick the bid scenarios) is read from
step 2's own b* cache, whose keys are the exact inputs, so a changed parameter file simply
misses it. A row the cache does not hold is worked out here, in time order, up to
MAKER_LIVE_MAX rows (~0.5 s each); the counts say how many rows were left unknown. This script
never writes the cache.

Each card also checks step 2's bid search against every 1c bid (explain.bid_grid, ~1 s a card)
and reports a row where the search missed a better bid in its own note.

    PYTHON_DOTENV_DISABLED=1 python3 tools/fade_1h_momentum_15m/examples.py
    PYTHON_DOTENV_DISABLED=1 python3 tools/fade_1h_momentum_15m/examples.py --params path/to/params.json

Reads only; writes only the markdown. Single process.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data  # noqa: E402
import model as fm  # noqa: E402
import step2_polymarket as s2  # noqa: E402
from explain import (ORD, PARTS, Explanation, cents, contributions, explain, feeds_text, pct,  # noqa: E402
                     snap_back_by_quarter)

OUT = data.OUT_DIR / "worked_examples.md"
HEADLINE = s2.HEADLINE
MAKER_LIVE_MAX = 40  # b* searches (~0.5 s each) allowed for headline rows step 2's cache does not hold

# Selection criteria (fixed before looking at outcomes; used to pick examples, not to trade).
AGREE_POINTS = 0.02  # (a) maths' Up probability within 2 points of the 15m market's Up price
FAST_LEG_MOVES = 1.0  # (b) last completed 15m candle up by at least one typical 15m swing
CROWD_GAP = 0.05  # (c) 1h Up price at least 5c from what the hour's move alone prices
VISIBLE_PUSH = 0.005  # (c) momentum push of at least 0.5 points on Up

ROW_FLOATS = ("y", "x", "sigma", "mu_L", "m_H", "market_price_up", "quote_up", "quote_dn",
              "ask_up", "ask_dn", "later_min_up", "later_max_up", "up", "binance_up")
LEAD = {"leg": "the leg", "snap_back": "the snap-back", "momentum": "the momentum"}


# --------------------------------------------------------------------------- rows


def load_params(path: Path) -> tuple[dict, str]:
    doc = json.loads(path.read_text())
    prm = doc["params"] if "params" in doc else doc
    return {k: float(prm[k]) for k in fm.PARAM_NAMES}, str(path.relative_to(data.REPO)) \
        if path.is_relative_to(data.REPO) else str(path)


def build(prm: dict) -> dict:
    """Step 2's rows, blend and probabilities, exactly as step2_polymarket.main builds them."""
    with contextlib.redirect_stdout(io.StringIO()):
        R, _ = s2.build_rows()
    R["mu_H"] = fm.mu_hat_H(R["m_H"], R["x"], R["sigma"], R["t_hour"])
    used = np.isfinite(R["market_price_up"]) & R["ok_model"]
    days = np.unique(R["day"])
    lodo = {int(d): s2.blend_moments(R, R["day"] != d) for d in days}
    X = s2.model_inputs(R, prm, lodo)
    mu, v = X["fitted"]
    n = R["mi"].size
    vH, vL, cHL = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    for d, mom in lodo.items():
        s = R["day"] == d
        e = mom["error_moments_per_sigma2"]
        vH[s], vL[s], cHL[s] = R["sig2"][s] * e["HH"], R["sig2"][s] * e["LL"], R["sig2"][s] * e["HL"]
    R.update({"M": X["M"], "mu": mu, "v": v, "vH": vH, "vL": vL, "cHL": cHL, "used": used,
              "p": s2.prob(R, X["M"], mu, v, prm), "tape_days": [int(d) for d in days]})
    return R


def row_at(R: dict, i: int) -> dict:
    day = int(R["day"][i])
    return {"coin": s2.COINS[int(R["coin"][i])], "t0": int(R["t0"][i]), "minute": int(R["minute"][i]),
            "prev": R["prev"][i].copy(), "vH": float(R["vH"][i]), "vL": float(R["vL"][i]),
            "cHL": float(R["cHL"][i]), "slug": R["slugs"][int(R["mi"][i])], "day": day,
            "blend_days": [d for d in R["tape_days"] if d != day],  # step 2's leave-one-day-out blend
            **{k: float(R[k][i]) for k in ROW_FLOATS}}


def headline_rows(R: dict) -> np.ndarray:
    """Step 2's headline rows in time order, then BTC, ETH, SOL, XRP."""
    head = np.flatnonzero(R["used"] & (R["minute"] == HEADLINE))
    return head[np.lexsort((R["coin"][head], R["t"][head]))]


def maker_table(R: dict, head: np.ndarray, prm: dict, live_max: int = MAKER_LIVE_MAX) -> dict:
    """Step 2's headline resting bid on each headline row (b* on the maths' side, anchor reset at the
    fill), classified by step 2's own maker_entries: quoted, filled, on the search's upper end.

    Read from step 2's b* cache with step 2's key (maker_quotes); a row the cache does not hold is
    searched here in time order, up to live_max rows. Rows beyond that stay unknown (known False)."""
    cache = s2.MakerCache(s2.MAKER_CACHE)  # read only
    mu_c = fm.implied_drift(R["market_price_up"], R["y"], R["sigma"], R["h"])  # as step2.maker_quotes
    Q = {side: {k: np.full(head.size, np.nan) for k in s2.MAKER_KEYS} for side in ("up", "down")}
    known = np.zeros(head.size, bool)
    live = 0
    for n, i in enumerate(head):
        side = "up" if s2.favoured_up(R["p"][i]) else "down"
        price = R["market_price_up"][i] if side == "up" else 1.0 - R["market_price_up"][i]
        x = np.array([price, R["y"][i], R["t_hour"][i], R["h"][i], R["sigma"][i], mu_c[i], R["M"][i],
                      R["mu"][i], R["v"][i], prm["theta"], prm["kappa0"], prm["lam"],
                      1.0 if side == "up" else -1.0, s2.FILL_CODE["reset"]])
        y = cache.get(x)
        if y is None:
            if live >= live_max:
                continue
            res = fm.maker_best_bid(*x[:12], side=side, fill_stretch="reset")
            y = np.array([float(res[k]) for k in s2.MAKER_KEYS])
            live += 1
        for k, val in zip(s2.MAKER_KEYS, y):
            Q[side][k][n] = val
        known[n] = True
    me = s2.maker_entries(s2.take(R, head), Q, R["p"][head])
    quoted, filled, on_bound = (np.zeros(head.size, bool) for _ in range(3))
    quoted[me["row"]] = True
    filled[me["row"]] = me["filled"]
    on_bound[me["row"]] = me["on_bound"]
    return {"known": known, "quoted": quoted, "filled": filled, "on_bound": on_bound, "live": live}


# --------------------------------------------------------------------------- scenarios


def pick(R: dict, prm: dict) -> tuple[list[dict], dict]:
    """One row per scenario: the first qualifying headline-minute row in time order not already shown.
    Also returns the tape-wide numbers the intro quotes (all from the same headline rows)."""
    head = headline_rows(R)
    th, k0, lam = prm["theta"], prm["kappa0"], prm["lam"]
    y, M, mu, v = R["y"][head], R["M"][head], R["mu"][head], R["v"][head]
    t, h, sig = R["t_hour"][head], R["h"][head], R["sigma"][head]
    C = contributions(y, M, mu, v, t, h, sig, th, k0, lam)
    p = R["p"][head]
    price = R["market_price_up"][head]
    k = R["k"][head]
    lead = np.argmax(np.abs(np.stack([C[q] for q in PARTS])), 0)  # 0 leg, 1 snap-back, 2 momentum

    # step 2's headline taker, on the same rows: decided, entered (filled), limit miss, and did the side win
    S = s2.take(R, head)
    tr = s2.taker_trades(S, p, "model")
    D = tr["decided"]
    decided, entered, won, missed = (np.zeros(head.size, bool) for _ in range(4))
    decided[D["row"]] = True
    entered[tr["row"]] = True
    won[tr["row"]] = tr["win"] > 0.5
    missed[D["row"][D["priced"] & ~D["filled"]]] = True
    not_decided = ~decided

    typ15 = sig * 0.5  # one typical 15-minute swing: sigma x sqrt(1/4 hour)
    last = R["prev"][head][:, 0]
    alone = fm.martingale_prob_up(R["x"][head], 1.0 - t, sig)
    gap = R["m_H"][head] - alone
    mt = maker_table(R, head, prm)
    down = ~s2.favoured_up(p)

    def is_filled_taker(ex: Explanation) -> bool:
        return ex.decision["taker"]["filled"]

    specs = [
        {"key": "a", "title": "Top of the hour: the maths and the market agree", "uses": "decision",
         "rule": f"1st quarter of the hour; the maths' Up probability within {100 * AGREE_POINTS:.0f} points of the "
                 "15m market's last Up price; no taker buy (the ask on the maths' side is not under its break-even)",
         "mask": (k == 1) & (np.abs(p - price) <= AGREE_POINTS) & not_decided},
        {"key": "b", "title": "Late in the hour after a fast 15m up leg: the snap-back leans against it",
         "uses": "decision", "focus": "snap_back",
         "rule": "4th quarter of the hour; the last completed 15m candle up by at least one typical 15-minute "
                 "swing; the stretch above zero, so the snap-back works against Up",
         "mask": (k == 4) & (last >= FAST_LEG_MOVES * typ15) & (M > 0)},
        {"key": "c", "title": "The 1h market disagrees with the hour's move", "uses": "decision", "focus": "momentum",
         "rule": f"a 1h market trade in the last minute, its Up price at least {100 * CROWD_GAP:.0f}c away from "
                 "what the hour's move so far would price on its own; the momentum push worth at least "
                 f"{100 * VISIBLE_PUSH:.1f} points on Up",
         "mask": np.isfinite(gap) & (np.abs(gap) >= CROWD_GAP) & (np.abs(C["momentum"]) >= VISIBLE_PUSH)},
        {"key": "d", "title": "A taker entry that won", "uses": "settled",
         "rule": "the maths' taker buy filled (the ask before the decision under the break-even, then a buy "
                 "limited at the break-even) and the side won",
         "mask": entered & won, "check": lambda ex: is_filled_taker(ex) and ex.outcome["side_won"]},
        {"key": "e", "title": "A taker entry that lost", "uses": "settled",
         "rule": "the maths' taker buy filled (same execution) and the side lost",
         "mask": entered & ~won, "check": lambda ex: is_filled_taker(ex) and not ex.outcome["side_won"]},
        {"key": "f", "title": "A resting bid that filled", "uses": "after", "maker": True,
         "rule": "a bid rests on the maths' side (a positive expected profit) and a later trade on that side is "
                 "below it",
         "lean": "a bid fills only when the market moves against it, so this pick leans toward a loss",
         "after": "on a later fill, which happens only when the market moves against the bid, so it leans toward "
                  "a loss",
         "mask": mt["quoted"] & mt["filled"],
         "check": lambda ex: ex.decision["maker"]["quoted"] and ex.decision["maker"]["filled"]},
        {"key": "g", "title": "The snap-back or the momentum leads", "uses": "decision",
         "rule": "the snap-back or the momentum moves Up more than the 15m leg does",
         "mask": lead != 0, "check": lambda ex: max(PARTS, key=lambda q: abs(ex.contributions[q])) != "leg"},
        {"key": "h", "title": "A taker buy that missed its limit", "uses": "after",
         "rule": "the ask before the decision is under the break-even, but the first taker buy in the 30 s after "
                 "it is over the break-even, so the limited buy does not fill",
         "after": "on the first taker buy in the 30 s after the decision",
         "mask": missed, "check": lambda ex: ex.decision["taker"]["limit_miss"]},
        {"key": "i", "title": "A resting bid that did not fill", "uses": "after", "maker": True,
         "rule": "a bid rests on the maths' side and no later trade on that side is below it",
         "after": "on no later trade below the bid",
         "mask": mt["quoted"] & ~mt["filled"],
         "check": lambda ex: ex.decision["maker"]["quoted"] and not ex.decision["maker"]["filled"]},
        {"key": "j", "title": "A Down-side entry", "uses": "decision", "maker": True,
         "rule": "the maths makes Down more likely and acts on it: it sends a taker buy of Down (the ask under the "
                 "break-even) or rests a bid on Down",
         "mask": down & (decided | mt["quoted"]),
         "check": lambda ex: ex.decision["side"] == "Down" and (
             ex.decision["taker"]["decided"] or ex.decision["maker"]["quoted"])},
    ]
    taken: set[int] = set()
    n_unknown = int((~mt["known"]).sum())
    for sc in specs:
        mask = sc.pop("mask")  # a row whose bid is unknown is never 'quoted', so it cannot qualify through a bid
        if sc.get("maker"):
            sc["n_unknown"], sc["n_rows"] = n_unknown, int(head.size)
        sc["n_qualifying"] = int(mask.sum())
        q = [int(j) for j in np.flatnonzero(mask) if int(head[j]) not in taken]
        sc["row"] = int(head[q[0]]) if q else None
        if q:
            taken.add(sc["row"])
            if sc["key"] == "g":
                sc["focus"] = PARTS[int(lead[q[0]])]

    p_no_mom = fm.prob_up(y, M, 0.0, v, t, h, sig, th, k0, lam)  # momentum off, as in the waterfall
    split = R["binance_up"][head] != (R["up"][head] > 0.5)
    stats = {"n": int(head.size), "mom_mean": float(np.abs(C["momentum"]).mean()),
             "mom_max": float(np.abs(C["momentum"]).max()),
             "mom_flips": int((s2.favoured_up(p) != s2.favoured_up(p_no_mom)).sum()),
             "lead": [int((lead == j).sum()) for j in range(3)],
             "feed_split": int(split.sum()),
             "quotes": int((mt["quoted"] & mt["known"]).sum()),
             "quotes_on_bound": int((mt["quoted"] & mt["on_bound"] & mt["known"]).sum()),
             "maker_unknown": n_unknown, "maker_live": mt["live"]}
    return specs, stats


# --------------------------------------------------------------------------- cards


def usd(x: float) -> str:
    return f"{'-' if x < 0 else '+'}${abs(x):.2f}"


def _signed_pts(x: float) -> str:
    return f"{100 * x:+.1f}"


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def days_text(days) -> str:
    """'Sep 18, 19 and 20' within one month, else 'Aug 31 and Sep 1'."""
    ds = [datetime.fromtimestamp(int(d) * 86400, UTC) for d in sorted(days)]
    if not ds:
        return "none"
    if len({(d.year, d.month) for d in ds}) == 1:
        return f"{ds[0]:%b} " + _and([str(d.day) for d in ds])
    return _and([f"{d:%b} {d.day}" for d in ds])


def span_text(days) -> str:
    """'Sep 17-20' for the tape's first and last day."""
    ds = [datetime.fromtimestamp(int(d) * 86400, UTC) for d in sorted(days)]
    if ds[0] == ds[-1]:
        return f"{ds[0]:%b} {ds[0].day}"
    if (ds[0].year, ds[0].month) == (ds[-1].year, ds[-1].month):
        return f"{ds[0]:%b} {ds[0].day}-{ds[-1].day}"
    return f"{ds[0]:%b} {ds[0].day} to {ds[-1]:%b} {ds[-1].day}"


def headline_title(ex: Explanation, focus: str | None = None) -> str:
    """A short title in a trader's voice, from the numbers. focus puts the card's scenario first."""
    i, d, c = ex.inputs, ex.decision, ex.contributions
    tk, mk, side = d["taker"], d.get("maker"), d["side"]
    big = max(PARTS, key=lambda q: abs(c[q]))
    if focus == "momentum" and math.isfinite(i["m_H"]):
        driver = (f"the 1h market pays {cents(i['m_H'])} against {cents(i['hour_alone_price_up'])} for the hour's "
                  f"move alone")
    elif focus in PARTS and focus != big:
        n = f"{100 * abs(c[focus]):.1f} points"
        driver = f"{LEAD[focus]} {'adds' if c[focus] > 0 else 'takes'} {n} {'to' if c[focus] > 0 else 'off'} Up"
    else:
        driver = f"{LEAD[big]} leads"
    acts = []
    if tk["filled"]:
        acts.append(f"buys {side} at {cents(tk['fill_price'])}")
    elif tk["limit_miss"]:
        acts.append(f"sends a buy of {side} limited at {cents(tk['break_even'])} that misses")
    if mk is not None and mk["quoted"]:
        acts.append(f"bids {side} at {cents(mk['bid'])}")
    act = ("the maths " + " and ".join(acts)) if acts else "no trade"
    return (f"{i['coin']} {i['minutes_gone']} min into the {datetime.fromtimestamp(i['window_start_ts'], UTC):%H:%M}"
            f" window: {driver}, Up {100 * d['p_up']:.1f}%, {act}")


def _taker_line(d: dict, hm: str) -> str:
    tk, side = d["taker"], d["side"]
    be = cents(tk["break_even"])
    why = (f"{be} plus the {100 * tk['fee_at_break_even']:.2f}c fee at that price adds up to the maths' "
           f"{100 * d['p_side']:.1f}%")
    if not math.isfinite(tk["ask"]):
        s = f"no taker bought {side} in the minute before {hm}, so there is no ask to act on."
        other = "Down" if side == "Up" else "Up"
        q_other, last = d["ask_at_t"][other], d["market_price"][side]
        if math.isfinite(q_other) and abs((1.0 - q_other) - last) < 0.005:
            s += (f" The {cents(last)} last trade matches a taker buy of {other} at {cents(q_other)}, which is the same "
                  f"as someone selling {side} at {cents(last)}: it hit a bid on {side}, so it is not a price anyone "
                  f"could buy {side} at.")
        return s
    if not tk["decided"]:
        return f"{side}'s ask of {cents(tk['ask'])} is at or over the {be} break-even ({why}): no taker buy."
    head = f"{side}'s ask of {cents(tk['ask'])} is under the {be} break-even ({why}). It buys with a limit at {be}"
    if tk["filled"]:
        src = (f"the first taker buy after {hm}" if tk["fill_from_print"]
               else "no taker buy within 30 s, so at the ask before the decision")
        return (f"{head}: filled at {cents(tk['fill_price'])} ({src}), fee {100 * tk['fee']:.2f}c, expected profit "
                f"{100 * tk['ev_per_share']:+.2f}c a share, full-Kelly size {100 * tk['kelly']:.1f}% of bankroll.")
    return f"{head}, but the first taker buy after {hm} was {cents(tk['fill_price'])}, over the limit: no position."


def _maker_line(d: dict) -> str:
    mk, side = d["maker"], d["side"]
    g = mk["grid"]
    if mk["quoted"]:
        if mk["on_upper_bound"]:
            s = (f"rests a bid on {side} at {cents(mk['bid'])}, 1c under the {cents(mk['price'])} last trade. Expected "
                 f"profit per share bid is still rising there, at the top of the paper book's bid range, so the "
                 f"book's range sets this level, not a peak in the maths.")
        else:
            s = f"rests a bid on {side} at {cents(mk['bid'])}, the bid where fill chance times edge peaks."
        s += (f" Fill chance {100 * mk['P_fill']:.1f}%: the maths' own estimate that {side} trades down to "
              f"{cents(mk['bid'])} before the close. A fill only happens if the price falls to the bid, which means "
              f"the market has moved against {side}, so the maths marks {side} "
              f"{'down' if mk['p_fill'] < d['p_side'] else 'up'} from {100 * d['p_side']:.1f}% to "
              f"{100 * mk['p_fill']:.1f}% for a filled bid. Expected profit {100 * mk['J']:+.2f}c per share bid; "
              f"full-Kelly size {100 * mk['kelly']:.1f}% of bankroll.")
        return s + (" See the note on the bid search below." if mk["search_missed"] else "")
    if not math.isfinite(mk["bid"]):
        return f"no room to rest a bid under {side}'s {cents(mk['price'])} price."
    if mk["search_missed"]:
        return "the paper book rests no bid (see the note on its bid search below)."
    if g is not None:
        return (f"no bid is worth resting. A bid fills only after {side} has fallen to it, and at every bid from 1c "
                f"to {cents(g['top'])} the maths then values {side} at or below the bid (the best, {cents(g['b'])}, "
                f"gives {100 * g['J']:+.2f}c per share bid).")
    return (f"the bid search finds no bid on {side} with a positive expected profit (its best gives "
            f"{100 * mk['J']:+.2f}c per share bid).")


def card(sc: dict, ex: Explanation, row: dict, p_step2: float) -> str:
    i, d, o = ex.inputs, ex.decision, ex.outcome
    tk, mk = d["taker"], d.get("maker")
    side = d["side"]
    ws = datetime.fromtimestamp(i["window_start_ts"], UTC)
    we = datetime.fromtimestamp(i["window_end_ts"], UTC)
    dt = datetime.fromtimestamp(i["decision_ts"], UTC)
    hm = f"{dt:%H:%M}"
    q = ORD[i["quarter"]]
    focus = sc.get("focus")
    L = [f"## ({sc['key']}) {sc['title']}", "", f"**{headline_title(ex, focus)}**", ""]
    for n, line in enumerate(ex.summary(focus)):
        L += ["> " + line] if n == 0 else [">", "> " + line]
    L += ["", "**The story**", ""] + [f"- **{g}.** {t}" for g, t in ex.story_lines()] + [""]

    byq = i["snap_back_by_quarter"]
    trend = "stronger" if byq[3] > byq[0] else "weaker" if byq[3] < byq[0] else "no different"
    g_h, left = i["G_hours"], i["minutes_left"]
    g_min = 60 * g_h
    counts = (f"all {left} minutes left = {g_h:.3f} h" if g_min >= left - 0.05 else
              f"{g_min:.1f} min = {g_h:.3f} h. A drift that builds up during the window is itself partly pulled "
              f"back by the snap-back, so the {left} minutes left count as {g_min:.1f}")
    if math.isfinite(i["m_H"]):
        weights = (f"1h market {100 * i['w_H']:.0f}%, spot {100 * i['w_L']:.0f}%, fitted on the other tape days' "
                   f"forecast errors ({days_text(row.get('blend_days', []))})")
    else:
        weights = "spot 100% (no 1h market trade in the last minute)"
    L += ["**Inputs**", "", "| input | value |", "|---|---|",
          f"| coin | {i['coin']} |",
          f"| window | {ws:%Y-%m-%d %H:%M}-{we:%H:%M} UTC, {q} quarter of the hour |",
          f"| decision time | {hm} UTC: {i['minutes_gone']} min gone, {left} min left |",
          f"| 15m leg so far | {pct(i['leg'])} = {i['leg_in_typical_moves']:+.2f} typical moves for the time left |",
          f"| typical move for the time left | {100 * i['typical_move']:.3f}% (without the snap-back or momentum "
          f"adjustments, sigma × √time left: {100 * i['brownian_move_for_time_left']:.3f}%) |",
          f"| typical one-hour swing, up or down (sigma, realised over the last 60 min) | "
          f"{100 * i['sigma_per_hour']:.3f}% |",
          "| last 12 15m candles, newest first | " + " ".join(f"{100 * r:+.3f}" for r in i["candles"]) + " (%) |",
          "| weight of each candle, newest first | " + " ".join(f"{100 * w:.1f}" for w in i["candle_weights"])
          + " (%) |",
          "| each candle's part of the stretch (weight × capped candle) | "
          + " ".join(f"{100 * r:+.3f}" for r in i["candle_stretch_parts"]) + " (%) |",
          f"| stretch (weighted average = sum of each candle's part above) | {pct(i['stretch'])} |",
          f"| share pulled back before the close | {100 * i['snap_back_fraction']:.1f}% in a {q}-quarter window, so "
          f"the snap-back pull is {pct(i['snap_back_pull'], 4)}. The snap-back is {trend} later in the hour: "
          + ", ".join(f"{ORD[n + 1]} {100 * b:.1f}%" for n, b in enumerate(byq)) + " at this minute |",
          f"| hour's move so far | {pct(i['hour_leg'])} (on its own it prices the hour Up at "
          f"{cents(i['hour_alone_price_up'])}) |",
          f"| 1h market Up price | {cents(i['m_H']) if math.isfinite(i['m_H']) else 'no trade in the last minute'} |",
          "| drift implied by the 1h price | "
          + (f"{pct(i['mu_H'])} an hour for the rest of the hour |" if math.isfinite(i["mu_H"]) else "none |"),
          f"| trailing 1h spot trend | {pct(i['mu_L'])} an hour |",
          f"| blend weights | {weights} |",
          f"| blended momentum | {pct(i['mu'])} an hour (how far off this estimate has been: "
          f"{100 * i['mu_sd']:.3f}% an hour, one sd) |",
          f"| momentum weight (theta) | {i['theta']:+.4f} ({'negative: fades' if i['theta'] < 0 else 'positive: follows' if i['theta'] > 0 else 'zero: ignores'} the momentum) |",
          f"| time the momentum counts for (G) | {counts} |",
          f"| momentum push = theta × blended momentum × G | {i['theta']:+.4f} × {pct(i['mu'])} an hour × "
          f"{g_h:.3f} h = {pct(i['momentum_push'], 4)} |",
          "", "**Probability waterfall**: each factor's fair share of the move from 50% (the same total whichever "
          "order you add them).", "",
          "| step | points on Up | Up after | in typical moves for the time left |", "|---|---|---|---|"]
    for w in ex.waterfall:
        zpart = i["z_parts"].get(w.get("part"), None)
        zs = f"{zpart:+.3f}" if zpart is not None else (f"{i['z']:+.3f}" if w["step"] == "net" else "")
        L.append(f"| {w['step']} | {'' if w['step'] == 'start' else _signed_pts(w['points'])} | "
                 f"{100 * w['p_up']:.1f}% | {zs} |")
    qa, fa, be = d["ask_at_t"], d["first_print_after_t"], d["break_even"]
    L += ["", "**Decision**", "", "| | Up | Down |", "|---|---|---|",
          f"| the maths' probability | {100 * d['p_up']:.1f}% | {100 * d['p_down']:.1f}% |",
          f"| 15m market price (last trade, a buy or a sell) | {cents(d['market_price']['Up'])} | "
          f"{cents(d['market_price']['Down'])} |",
          f"| ask at the decision (last price a taker paid in the minute before {hm}) | {cents(qa['Up'])} | "
          f"{cents(qa['Down'])} |",
          f"| first taker buy in the 30 s after {hm} | {cents(fa['Up'])} | {cents(fa['Down'])} |",
          f"| taker break-even (a*) | {cents(be['Up'])} | {cents(be['Down'])} |", "",
          f"- The side the maths makes more likely: **{side}** ({100 * d['p_side']:.1f}%).",
          f"- Taker: {_taker_line(d, hm)}"]
    if mk is not None:
        L.append(f"- Maker: {_maker_line(d)}")
    L += ["", "**Outcome**", ""]
    if o is None:
        L.append("- Not settled.")
    else:
        feeds = feeds_text(o).replace("The feeds split:", "**The feeds split:**")
        L.append(f"- {o['winner']} won: the maths' side {'won' if o['side_won'] else 'lost'}."
                 + (f" {feeds}" if feeds else ""))
        if tk["filled"]:
            L.append(f"- Taker: {100 * o['taker_pnl']:+.1f}c a share after the fee; at full-Kelly size "
                     f"{usd(o['taker_pnl_per_100_at_kelly'])} per $100 of bankroll.")
        if mk is not None:
            low = mk["lowest_later_print"]
            low_s = cents(low) if math.isfinite(low) else "none"
            if mk["quoted"] and mk["filled"]:
                L.append(f"- Maker: filled (lowest later {side} price: {low_s}); {100 * o['maker_pnl']:+.1f}c a share, "
                         f"no fee; at full-Kelly size {usd(o['maker_pnl_per_100_at_kelly'])} per $100 of bankroll.")
            elif mk["quoted"]:
                L.append(f"- Maker: not filled. The lowest later {side} price was {low_s}, and the paper book counts a "
                         f"fill only on a later trade below the bid, since its place in the queue at the bid is "
                         f"unknown.")
    note = ex.bid_search_note()
    if note:
        L += ["", f"**Note.** {note}"]
    L += ["", f"<sub>Market: `{row['slug']}`. Check: explain() p = {ex.decision['p_up']:.12f}, step 2 p = "
              f"{p_step2:.12f}.</sub>", ""]
    return "\n".join(L)


def _count(sc: dict) -> str:
    n = sc["n_qualifying"]
    base = "no row qualifies" if n == 0 else f"{n} row{'s' if n != 1 else ''} qualif{'y' if n != 1 else 'ies'}"
    if sc.get("n_unknown"):
        base += (f"; bids on {sc['n_unknown']:,} of the {sc['n_rows']:,} rows are not counted: step 2's bid cache "
                 f"does not hold them yet")
    return base


def picked(specs: list[dict]) -> str:
    """How the examples were picked: one line per card, with how many rows fit."""
    out = ["## How the examples were picked", "",
           "Each card is the first row in time order that fits its filter and is not already shown on an earlier "
           "card. The filters only choose which real rows to show; they are not part of the strategy.", ""]
    for sc in specs:
        lean = f" This depends on what traded after the decision: {sc['lean']}." if sc.get("lean") else ""
        out.append(f"- **({sc['key']}) {sc['title']}**: {sc['rule']} ({_count(sc)}).{lean}")
    return "\n".join(out)


def _keys(specs: list[dict], uses: str) -> list[str]:
    return [f"({sc['key']})" for sc in specs if sc["uses"] == uses]


def intro(prm: dict, stats: dict, cards_quoted: int, cards_on_bound: int) -> str:
    byq = snap_back_by_quarter(HEADLINE, prm["kappa0"], prm["lam"])
    trend = "stronger" if byq[3] > byq[0] else "weaker" if byq[3] < byq[0] else "no stronger or weaker"
    fee50 = 100 * float(fm.taker_fee(0.5))
    th, al, c = prm["theta"], prm["alpha"], prm["c"]
    # the stretch's weights and soft cap, from the model: a lone tiny move gives its weight, a lone move of
    # half the cap shows how much the cap trims it
    eye = np.eye(fm.N_LAGS)
    w = np.asarray(fm.stretch(eye * 1e-12, al, c), float) / 1e-12
    trim = 1.0 - float(fm.stretch(eye[0] * c / 2, al, c)) / (w[0] * c / 2)
    if th == 0:
        weight_words = "With theta at 0 the maths ignores the momentum."
    else:
        weight_words = (f"With theta {th:+.4f}, the maths expects the rest of the window to go "
                        f"{'against' if th < 0 else 'with'} the momentum by {100 * abs(th):.1f}% of what the "
                        f"momentum alone would carry.")
    n, lead = stats["n"], stats["lead"]
    if cards_quoted == 0:
        cards_bid = ""
    elif cards_on_bound == cards_quoted:
        cards_bid = " Every resting bid in these cards sits there."
    else:
        cards_bid = (f" {cards_on_bound} of the {cards_quoted} resting bids in these cards sit there; the others sit "
                     f"where fill chance times edge peaks.")
    nq, nb = stats["quotes"], stats["quotes_on_bound"]
    tape_bid = ""
    if nq:
        tape_bid = (f" On the whole minute-{HEADLINE} tape, {'all ' if nb == nq else ''}{nb} of step 2's {nq} resting "
                    f"bids sit there{' too' if nb == nq else ''}"
                    + (f", counting only the {n - stats['maker_unknown']:,} rows whose bid is known"
                       if stats["maker_unknown"] else "") + ".")
    return "\n".join([
        "## How to read a card", "",
        "Each card opens with a short summary and the story in four lines: the leg, the snap-back, the momentum "
        "and the trade. The tables under them hold every number behind the story.", "",
        "What the maths looks at, in the order a card shows it. The symbol in brackets is only for readers who "
        "want the formula.", "",
        "- **15m leg so far** (y): how far spot has moved since the window opened, in % and in typical moves for "
        "the time left.",
        "- **Time left** (h): minutes until the window closes. The less time left, the more the leg so far decides "
        "the window.",
        "- **Typical swing** (sigma): the size of a typical one-hour move in spot, up or down, from its realised "
        "volatility over the last 60 minutes. It is a size, not a direction or a trend. One typical move for the "
        "time left (s) is this scaled to the minutes left, trimmed by the snap-back and widened by doubt about the "
        "momentum.",
        f"- **Snap-back from the last 12 candles** (stretch M): a weighted average of the last twelve 15m candles, "
        f"not their sum. The newest candle carries about {100 * w[0]:.0f}% of the weight, the 2nd {100 * w[1]:.0f}%, "
        f"the 12th {100 * w[-1]:.0f}%, so the newest candle often sets the stretch's sign. Each candle is also "
        f"soft-capped at c ({100 * c:.2f}% with these parameters), which only bites on candles near that size: a "
        f"candle of {100 * c / 2:.2f}% is trimmed by about {100 * trim:.0f}%, a smaller one by less. The maths "
        f"expects part of the stretch to come back before the close.",
        f"- **Where in the hour**: the snap-back gets {trend} as the hour goes on. With these parameters about "
        f"{100 * byq[0]:.0f}% of the stretch comes back in a 1st-quarter window and about {100 * byq[3]:.0f}% in a "
        f"4th-quarter window (decided {HEADLINE} minutes in).",
        "- **The hour's move so far** (x): how far spot has moved since the hour opened. On its own it gives a "
        "price for the hour.",
        "- **The 1h market price** (m_H): the crowd's price for the hour closing Up. Its gap from the hour-alone "
        "price is the drift the crowd expects for the rest of the hour.",
        "- **Trailing 1h spot trend** (mu_L): spot's move over the last 60 minutes. It is blended with the 1h "
        "market's drift into one **blended momentum** (mu), by weights fitted on the other tape days' forecast "
        "errors (leave one day out; there is no earlier tape to fit on).",
        f"- **Momentum weight** (theta): negative, the maths fades the momentum; positive, it follows it. "
        f"{weight_words} The push is theta × blended momentum (% an hour) × the time it counts for (G, in hours). "
        f"A drift that builds up during the window is itself partly pulled back by the snap-back, so G is a little "
        f"less than the time left.",
        f"- **How much the momentum moves Up**: at minute {HEADLINE} on the tape ({n:,} rows) the momentum push "
        f"moves Up by {100 * stats['mom_mean']:.2f} points on average and {100 * stats['mom_max']:.1f} points at "
        f"most. Setting it to zero would change the maths' side on {stats['mom_flips']} rows. The biggest factor is "
        f"the leg on {lead[0]:,} rows, the snap-back on {lead[1]:,} and the momentum on {lead[2]:,}.",
        f"- **Spot feed and settlement feed**: the maths reads Binance 1-minute candles. The markets settle on "
        f"Chainlink. At minute {HEADLINE} the two closed the window in different directions on "
        f"{stats['feed_split']} of {n:,} rows ({100 * stats['feed_split'] / n:.1f}%). A split can turn a right read "
        f"of Binance into a loss, or the other way round. Each card's Outcome says which way each feed closed.",
        "- **The 15m market price**: the last trade on the window's own market. A trade can be a buy or a sell, "
        "so the last trade may have hit a bid. The **ask** is the last price a taker actually paid for that side "
        "in the minute before the decision. A taker buy fills at the first price a taker pays in the 30 s after.",
        "- **Break-even** (a*): the most a taker share is worth, the price where price plus fee equals the maths' "
        f"probability. The fee is 7% × price × (1 − price): {fee50:.2f}c a share at 50c, less further from 50c.",
        "- **Resting bid and fill chance** (b*): the maths looks for the bid with the most expected profit per share "
        "bid (fill chance × edge), from 1c up to 1c under the last trade. The paper book keeps a bid at least 1c "
        "under the last trade so that it rests rather than buys at once, and it rests one bid per window, with no "
        "ladder. When expected profit is still rising at the top of that range, the bid sits 1c under the last "
        f"trade: the book sets that level, not a peak in the maths.{cards_bid}{tape_bid} The fill chance is the "
        "maths' own estimate that the price trades down to the bid before the close. A fill only happens when the "
        "price falls to the bid, so the maths marks its side lower for a filled bid.",
        "- **Size**: full Kelly, the bankroll share that grows money fastest for that edge, from the maths' "
        "probability and the entry price. Taker and maker are two separate paper books compared side by side, "
        "not stacked on the same window. Each card gives the size in its Decision bullets.", "",
        "The waterfall shows each factor's fair share of the move from 50% (the same total whichever order you "
        "add them).", "",
        "The strategy has no price rules: side, entry price and size all come out of the one calculation.", "",
        "The formula and the fitted parameters are in the footnote at the end.",
    ])


def footnote(prm: dict, prm_label: str) -> str:
    th, k0, lam, al, c = (prm[k] for k in fm.PARAM_NAMES)
    grow = math.exp(-lam)
    if lam < 0:
        change = f"a negative lam makes it grow: {grow:.1f}× stronger at the end of the hour than at the start"
    elif lam > 0:
        change = f"a positive lam makes it fade: {1 / grow:.1f}× weaker at the end of the hour than at the start"
    else:
        change = "lam = 0 keeps it the same all hour"
    w = np.arange(1, fm.N_LAGS + 1, dtype=float) ** -al
    w /= w.sum()
    return "\n".join([
        "## Footnote: the formula and the parameters", "",
        "p = Φ((y − R + Mo) / s). y is the 15m leg so far. R = share pulled back × stretch is the snap-back pull. "
        "Mo = theta × blended momentum × G is the momentum push. s = √(V + theta² × v × G²) is one typical move "
        "for the time left: V is the variance of spot's move over the time left after the snap-back, v the error "
        "variance of the blended momentum. Φ turns a number of typical moves into a probability. The waterfall "
        "splits p − 50% over y, R and Mo by exact Shapley values: each factor's effect averaged over every order "
        "of adding them.", "",
        f"Parameters, from `{prm_label}`:", "",
        f"- theta {th:+.4f}: the momentum weight. Negative fades the momentum, positive follows it.",
        f"- kappa0 {k0:.4f}: how hard the snap-back pulls at the top of the hour (a speed per hour).",
        f"- lam {lam:+.4f}: how that pull changes through the hour. The pull is kappa0 × e^(−lam × hours into "
        f"the hour), so {change}.",
        f"- alpha {al:.4f}: how fast older candles lose weight in the stretch. The latest candle carries "
        f"{100 * w[0]:.0f}% of the weight, the 2nd back {100 * w[1]:.0f}%, the 12th back {100 * w[-1]:.0f}%.",
        f"- c {c:.5f}: the cap on one candle. Each candle enters as c × tanh(move / c), so a move well under "
        f"{100 * c:.2f}% counts in full and no candle counts for more than {100 * c:.2f}%.",
        "",
    ])


def header(specs: list[dict], prm_label: str, R: dict) -> str:
    settled = _keys(specs, "settled")
    after = [f"({sc['key']}) {sc['after']}" for sc in specs if sc["uses"] == "after"]
    how = []
    if settled:
        how.append(f"{_and(settled)} {'are' if len(settled) > 1 else 'is'} picked on how the window settled.")
    if after:
        how.append("Picked on what traded after the decision: " + "; ".join(after) + ".")
    how.append("The other filters use only what is known at the decision.")
    return (f"# Fade 1h Momentum on 15m: worked examples\n\nGenerated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC by "
            f"`tools/fade_1h_momentum_15m/examples.py` from step 2's rows (`step2_polymarket.build_rows`) and "
            f"`{prm_label}`. {len(specs)} real decisions on the {span_text(R['tape_days'])} Polymarket tape, each "
            f"{HEADLINE} minutes into a 15-minute Up/Down window. " + " ".join(how) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--params", default=str(s2.PARAMS), help="parameter JSON ('params' key or flat)")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    t_start = time.time()
    prm, prm_label = load_params(Path(args.params))
    R = build(prm)
    specs, stats = pick(R, prm)
    cards, first = [], None
    cards_quoted = cards_on_bound = 0
    for sc in specs:
        if sc["row"] is None:
            cards.append(f"## ({sc['key']}) {sc['title']}\n\nNo row qualifies on this tape with these parameters.\n")
            continue
        row = row_at(R, sc["row"])
        ex = explain(row, prm)
        p_step2 = float(R["p"][sc["row"]])
        if abs(ex.decision["p_up"] - p_step2) > 1e-9:
            raise RuntimeError(f"explain p {ex.decision['p_up']} != step 2 p {p_step2} at row {sc['row']}")
        check: Callable[[Explanation], bool] | None = sc.get("check")
        if check is not None and not check(ex):
            raise RuntimeError(f"row {sc['row']} was picked for ({sc['key']}) but explain() does not agree")
        mk = ex.decision.get("maker")
        if mk is not None and mk["quoted"]:
            cards_quoted += 1
            cards_on_bound += int(mk["on_upper_bound"])
        cards.append(card(sc, ex, row, p_step2))
        if first is None:
            first = cards[-1]
    text = (header(specs, prm_label, R) + "\n" + intro(prm, stats, cards_quoted, cards_on_bound) + "\n\n"
            + picked(specs) + "\n\n" + "\n".join(cards) + "\n" + footnote(prm, prm_label))
    Path(args.out).write_text(text)
    print(f"wrote {args.out} ({time.time() - t_start:.1f}s; {stats['maker_live']} bids searched live, "
          f"{stats['maker_unknown']} headline rows without a bid)")
    if first is not None:
        print("\n" + first)


if __name__ == "__main__":
    main()
