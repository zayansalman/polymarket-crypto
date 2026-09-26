"""Step 2 of the pre-registered test: the frozen model on the real Polymarket 15m tape.

Pre-registration: tasks/2026-09-21-fade-1h-momentum-on-15m.md, section 8, step 2.
Model: tools/fade_1h_momentum_15m/model.py. Parameters frozen before the tape starts:
data/fade_1h_momentum_15m/params_pre_sep17.json (step 1, windows opening before 2026-09-17):
"params" (lam free in sign, the pre-registered fit) and "params_lam_ge_0" (sensitivity).

Rows. Every settled BTC/ETH/SOL/XRP 15m market in m15.db (1,136, windows 2026-09-17 20:45 ..
2026-09-20 19:30 UTC), decided at minute m in {0, 1, 2, 3, 5} of the window (tau = m / 15);
headline m = 2. Outcome: the real resolution (winner oidx 0 = Up).

Inputs at the decision instant t = window open + 60 m, exactly as step 1 (Binance spot, the
price at t = close of the minute that opened at t - 60; y = 0 at m = 0), plus:
- m_H (BTC/ETH/XRP): Up-equivalent price of the last print on that hour's market (wallets.db,
  family 1h) with t - 60 <= ts <= t. None -> NaN -> spot momentum only. SOL has no hourly tape.
- market price of the 15m Up token: Up-equivalent price of the last print (either token, maker
  or taker row) with t - 60 <= ts <= t. None -> the row is skipped and counted.
Tape order: both tapes were stored newest first (ts never increases with rowid inside a market),
so chronological order is (ts ascending, rowid descending).

Blend (section 4). Forecast target: the rest of the hour's drift (X(hour close) - X(t)) / (1 - t)
on Binance, which is what the hourly market settles on. Errors are standardised by sigma and
weighted by (1 - t) so each row's target noise has the same size. The minimum-variance weight
only depends on differences of the moments, where the target noise cancels; for v the target
noise (measured by the forward realised variance) is subtracted. Moments are estimated
leave-one-day-out over the four UTC days of the tape (deviation: there is one tape, so no
walk-forward) and applied per row as sigma^2 * moment. Equal weights (w_H = 0.5) beside.

Execution, all on tape prints. Side (section 6): j = Up where p >= 1/2, else Down; one entry per
market at a decision minute (the market is the unit).
- taker. Decision on what is known at t: the quote of side j = price of the LAST taker print that
  is an Up-equivalent buy of side j with t - 60 <= ts < t (a BUY of token j, or a SELL of the other
  token, which hits a bid on it = an ask on j). Taker = its (txh, wallet) is in maker15.db tk.
  Decided when quote < a*(p_j). Order: a buy limited at a*(p_j), section 6's most we would ever
  pay. The ask at t is the price of the FIRST such print with t <= ts <= t + 30: the order fills
  there if it is <= a*, else it is a limit miss (no position, counted). A decided row with no
  such print is kept, since whether another trader bought within 30 s is not known at t, and
  fills at its pre-t quote. P&L per share = 1{win} - fill - 0.07 fill (1 - fill). Kelly f* =
  (p - a - c) / (1 - a - c) at the fill price. Sensitivities, on the same decided rows: a market
  order (pays the post-t print even above a*); no-print rows at quote + mean slippage, at the
  first later taker buy, or dropped; the previous headline (market order, no-print rows dropped);
  pay the quote itself; the first run's rule (decide and pay on the post-t print, which uses
  information after t); both sides tested independently (the first run's side rule); direct BUY
  prints only. Every taker table also gives the same entries paying the pre-t quote (matched n).
- maker: b* = argmax P_fill(b) (p_fill(b) - b) on [0.01, price_j - 0.01] (model.maker_best_bid),
  p_fill re-evaluated at the fill state (stretch M, fill_stretch="reset"); quoted when J(b*) > 0
  (maker Kelly > 0). Filled only if a print with t < ts < window end has Up-equivalent price (as a
  price of token j) strictly below b*. P&L per filled share = 1{win} - b*, no fee. Queue position
  is unknown: a print strictly below b* is taken as proof the bid would have filled. Reported
  split by where b* sits: on the search's upper end price_j - 1c (the last print sets the bid) or
  interior (J'(b*) = 0). Sensitivities at the headline minute: equal weights; both sides quoted
  wherever J > 0 (reported per market and per (market, side)); and a chain back to the first
  run's rule, one change at a time: lam >= 0 params; then the anchor held at the quote
  (fill_stretch="held"); then both sides (= the first run's maker).
- baselines: Zayan's rule (>= 0.40) and the 2026-09-21 finding (>= 0.55): buy the 1h side as the
  finding defines it (threshold_scan.py: sign of the Binance return from 1 h before the window
  open to the open) when its pre-t quote is >= the threshold; a market order (no a*), priced as
  the taker. Sensitivity: the side from the trailing hour at t (mu_L).
- maker tick sensitivity: the highest 1c tick at or below b*, with b* on the search's upper end
  taken as exactly price_j - 1c.

Every comparison is on the same rows; t is clustered by window start (the four coins and the
decision minutes of one window are one cluster). Intervals: Wilson (rows treated as independent,
as pre-registered) and, beside them, mean +- 1.96 window-clustered standard errors. The model is
fed Binance while the 15m markets settle on Chainlink; every headline score and P&L is also
given against Binance's own direction (vs_binance_direction), and model - martingale (both fed
Binance) is the comparison that isolates the model's terms. Tables by quarter and asset are
exploratory; multiple_comparisons counts every t computed and adjusts each table (Holm, BH).

    PYTHON_DOTENV_DISABLED=1 python3 tools/fade_1h_momentum_15m/step2_polymarket.py
    PYTHON_DOTENV_DISABLED=1 python3 tools/fade_1h_momentum_15m/step2_polymarket.py --no-maker
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from scipy.optimize import minimize_scalar
from scipy.special import log_ndtr

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data  # noqa: E402  (sibling module: tools/fade_1h_momentum_15m/data.py)
import model as fm  # noqa: E402  (sibling module: tools/fade_1h_momentum_15m/model.py)
from step1_walkforward import cluster_t  # noqa: E402  (CR1 t of a mean, clustered)

OUT = data.OUT_DIR / "step2.json"
PARAMS = data.OUT_DIR / "params_pre_sep17.json"
MAKER_CACHE = data.OUT_DIR / "step2_maker_cache.npz"

COINS = ["btc", "eth", "sol", "xrp"]
MINUTES = np.array([0, 1, 2, 3, 5])
HEADLINE = 2
NLAG = fm.N_LAGS
QUOTE_BACK = 60  # market price and taker quote: prints with t - 60 <= ts <= t (quote: < t)
ASK_AHEAD = 30  # taker fill: first taker buy with t <= ts <= t + 30
P_CLIP = 1e-3  # every probability clipped to [P_CLIP, 1 - P_CLIP] for log-loss
TICK = 0.01
TICK_TOL = 1e-3  # in ticks (1e-5 in price): a price this close under a tick is on it (tape float noise)
ZAYAN_THRESHOLDS = (0.40, 0.55)
BINS = np.linspace(0.0, 1.0, 11)
Z95 = 1.959963984540054
ON_BOUND_TOL = 1e-4  # b* within this of price_j - 1c sits on the search's upper end

QUOTE = ("quote_up", "quote_dn")  # decision: last taker buy of side j in [t - 60, t)
FILL = ("ask_up", "ask_dn")  # execution: first taker buy of side j in [t, t + 30]
QUOTE_DIRECT = ("quote_up_direct", "quote_dn_direct")
FILL_DIRECT = ("ask_up_direct", "ask_dn_direct")
LATER = ("later_up", "later_dn")  # first taker buy of side j in [t, window end)


def _r(v, nd: int = 2):
    return None if v is None or not np.isfinite(v) else round(float(v), nd)


def _iso(ts) -> str:
    return data._iso(int(ts))


# --------------------------------------------------------------------------- tape


def _tape(db: str, cid: str, lo: int, hi: int) -> dict:
    """Prints of one market with lo <= ts < hi in chronological order (ts asc, rowid desc)."""
    cur = data._pm(db).execute(
        "select ts, oidx, side, price, txh, wallet from trades where cid=? and ts>=? and ts<? "
        "order by ts, rowid desc", (cid, lo, hi))
    rows = cur.fetchall()
    ts = np.array([r[0] for r in rows], np.int64)
    oidx = np.array([r[1] for r in rows], np.int64)
    buy = np.array([r[2] == "BUY" for r in rows], bool)
    price = np.array([r[3] for r in rows], float)
    keys = [((r[4] or "").lower(), (r[5] or "").lower()) for r in rows]
    return {"ts": ts, "oidx": oidx, "buy": buy, "price": price, "keys": keys,
            "up": np.where(oidx == 0, price, 1.0 - price)}


def _last_in(tp: dict, lo: int, hi: int) -> tuple[float, int]:
    """Up-equivalent price of the last print with lo <= ts <= hi, and how many prints there are."""
    a = np.searchsorted(tp["ts"], lo, "left")
    b = np.searchsorted(tp["ts"], hi, "right")
    return (float(tp["up"][b - 1]) if b > a else np.nan), int(b - a)


def market_rows(mk: data.Market, h1: dict) -> list[dict]:
    """Tape quantities of one 15m market at each decision minute."""
    start, end = mk.start_ts, mk.end_ts
    tp = _tape("m15", mk.cid, start - QUOTE_BACK, end)
    tk = data.taker_keys(mk.cid)
    taker = np.array([k in tk for k in tp["keys"]], bool)
    # Direction on Up of each print: BUY Up or SELL Down = +1 (an Up buy); BUY Down or SELL Up = -1.
    up_dir = np.where((tp["oidx"] == 0) == tp["buy"], 1, -1)
    hour = start - start % 3600
    ht = h1.get((mk.asset, hour))
    out = []

    def first(mask, vals):
        i = np.flatnonzero(mask)
        return float(vals[i[0]]) if i.size else np.nan

    def last(mask, vals):
        i = np.flatnonzero(mask)
        return float(vals[i[-1]]) if i.size else np.nan

    for m in MINUTES:
        t = start + 60 * int(m)
        price, n_quote = _last_in(tp, t - QUOTE_BACK, t)
        m_h = np.nan
        if ht is not None:
            m_h, _ = _last_in(ht, t - QUOTE_BACK, t)
        a0 = np.searchsorted(tp["ts"], t - QUOTE_BACK, "left")
        a = np.searchsorted(tp["ts"], t, "left")
        b = np.searchsorted(tp["ts"], t + ASK_AHEAD, "right")
        cols = {}
        for tag, sl, pick in (("quote", slice(a0, a), last), ("ask", slice(a, b), first),
                              ("later", slice(a, tp["ts"].size), first)):  # later: t <= ts < window end
            tk_w, dir_w, up_w = taker[sl], up_dir[sl], tp["up"][sl]
            oid_w, buy_w = tp["oidx"][sl], tp["buy"][sl]
            cols[f"{tag}_up"] = pick(tk_w & (dir_w == 1), up_w)
            cols[f"{tag}_dn"] = pick(tk_w & (dir_w == -1), 1.0 - up_w)
            if tag == "later":
                cols["later_up_delay_s"] = first(tk_w & (dir_w == 1), (tp["ts"][sl] - t).astype(float))
                cols["later_dn_delay_s"] = first(tk_w & (dir_w == -1), (tp["ts"][sl] - t).astype(float))
                continue
            cols[f"{tag}_up_direct"] = pick(tk_w & buy_w & (oid_w == 0), up_w)
            cols[f"{tag}_dn_direct"] = pick(tk_w & buy_w & (oid_w == 1), 1.0 - up_w)
            if tag == "ask":
                cols["ask_up_delay_s"] = first(tk_w & (dir_w == 1), (tp["ts"][sl] - t).astype(float))
                cols["ask_dn_delay_s"] = first(tk_w & (dir_w == -1), (tp["ts"][sl] - t).astype(float))
        later = tp["ts"] > t  # the maker quote rests from t (exclusive) to the window end
        fu = tp["up"][later]
        f0 = tp["price"][later & (tp["oidx"] == 0)]
        f1 = tp["price"][later & (tp["oidx"] == 1)]
        out.append({
            "market_price_up": price, "n_prints_quote_window": n_quote, "m_H": m_h,
            "has_h1_market": ht is not None, **cols,
            "later_min_up": float(fu.min()) if fu.size else np.nan,  # Up bid fills if < b
            "later_max_up": float(fu.max()) if fu.size else np.nan,  # Down bid fills if 1 - this < b
            "later_min_tok0": float(f0.min()) if f0.size else np.nan,
            "later_min_tok1": float(f1.min()) if f1.size else np.nan,
            "n_prints_total": int(tp["ts"].size), "n_taker_prints": int(taker.sum()),
        })
    return out


def load_h1() -> dict:
    """(asset, hour open) -> chronological tape of that hourly market, restricted to its hour."""
    out = {}
    for mk in data.markets_1h():
        out[(mk.asset, mk.start_ts)] = _tape("wallets", mk.cid, mk.start_ts - QUOTE_BACK, mk.end_ts)
    return out


# --------------------------------------------------------------------------- spot features


def spot_features(coin: str, t0: np.ndarray) -> dict:
    """Step 1's inputs for windows opening at t0 (unique, sorted) at MINUTES, plus the blend target."""
    lo_t = int(t0.min()) - 60 * (15 * NLAG + 120)
    hi_t = (int(t0.max()) // 3600 + 1) * 3600 + 60
    b = data.load_1m(coin, lo_t, hi_t)
    n_min = (hi_t - lo_t) // 60
    o = np.full(n_min, np.nan)
    c = np.full(n_min, np.nan)
    k = (b.t - lo_t) // 60
    o[k], c[k] = b.o, b.c
    lo, lc = np.log(o), np.log(c)
    bad = np.isnan(o) | np.isnan(c)
    miss = np.concatenate([[0], np.cumsum(bad)])
    r2 = np.zeros(n_min)
    r2[1:] = (lc[1:] - lc[:-1]) ** 2
    cs = np.concatenate([[0.0], np.cumsum(np.nan_to_num(r2))])  # cs[i] = sum_{j < i} r2[j]

    i0 = (t0 - lo_t) // 60
    it = i0[:, None] + MINUTES[None, :]
    s_t = lc[it - 1]  # ln S(t): close of the minute before t
    y = np.where(MINUTES[None, :] > 0, s_t - lo[i0][:, None], 0.0)
    ih = i0 - (t0 % 3600) // 60
    x = np.where(it > ih[:, None], s_t - lo[ih][:, None], 0.0)
    sig2 = cs[it] - cs[it - 60]
    mu_l = s_t - lc[it - 61]
    # the 2026-09-21 finding's (threshold_scan.py) 1h side: the return from 1 h before the window
    # open to the open, ln S(T0) - ln S(T0 - 3600); known from the open on, equal to mu_L at minute 0
    mom_open = lc[i0 - 1] - lc[i0 - 61]
    prev = np.stack([lc[i0 - 15 * (j - 1) - 1] - lo[i0 - 15 * j] for j in range(1, NLAG + 1)], 1)
    t_hour = ((t0 % 3600)[:, None] + 60 * MINUTES[None, :]) / 3600.0
    hour_move = lc[ih + 59] - lo[ih]  # the Binance 1h candle the hourly market settles on
    target = (hour_move[:, None] - x) / (1.0 - t_hour)
    rv_fwd = cs[ih + 60][:, None] - cs[it]  # realised variance of the rest of the hour
    ok_model = (miss[i0 + 6] - miss[i0 - 15 * NLAG - 2]) == 0  # every minute a decision up to m=5 uses
    ok_target = (miss[ih + 60] - miss[i0 - 15 * NLAG - 2]) == 0
    nm = MINUTES.size
    return {
        "y": y, "x": x, "sigma": np.sqrt(np.maximum(sig2, 0.0)), "sig2": sig2, "mu_L": mu_l,
        "mom_1h_to_open": np.repeat(mom_open[:, None], nm, 1),
        "prev": np.repeat(prev[:, None, :], nm, 1), "t_hour": t_hour,
        "h": np.broadcast_to((15 - MINUTES) / 60.0, (t0.size, nm)).copy(),
        "target": target, "rv_fwd": rv_fwd,
        "ok_model": np.repeat(ok_model[:, None], nm, 1) & (sig2 > 0),
        "ok_target": np.repeat(ok_target[:, None], nm, 1),
        "binance_up": np.repeat((c[i0 + 14] >= o[i0])[:, None], nm, 1),
    }


def build_rows() -> tuple[dict, dict]:
    t_start = time.time()
    mkts = data.markets_15m()
    h1 = load_h1()
    print(f"markets 15m {len(mkts)}, hourly {len(h1)} ({time.time() - t_start:.1f}s)", flush=True)
    tape = []
    for i, mk in enumerate(mkts):
        tape.append(market_rows(mk, h1))
        if (i + 1) % 200 == 0:
            print(f"  tape {i + 1}/{len(mkts)} ({time.time() - t_start:.0f}s)", flush=True)
    nm = MINUTES.size
    R: dict = {
        "mi": np.repeat(np.arange(len(mkts)), nm),
        "coin": np.repeat([COINS.index(m.asset) for m in mkts], nm).astype(np.int8),
        "t0": np.repeat([m.start_ts for m in mkts], nm).astype(np.int64),
        "end": np.repeat([m.end_ts for m in mkts], nm).astype(np.int64),
        "minute": np.tile(MINUTES, len(mkts)).astype(np.int8),
        "up": np.repeat([1.0 if m.winner == 0 else 0.0 for m in mkts], nm),
    }
    R["t"] = R["t0"] + 60 * R["minute"].astype(np.int64)  # int8 minute * 60 would overflow
    R["k"] = ((R["t0"] % 3600) // 900 + 1).astype(np.int8)
    R["day"] = R["t0"] // 86400
    for key in tape[0][0]:
        R[key] = np.array([row[key] for mrows in tape for row in mrows])
    # Spot features, per coin, mapped back onto the market rows.
    n = R["mi"].size
    feats: dict = {}
    for ci, coin in enumerate(COINS):
        sel = np.flatnonzero(R["coin"] == ci)
        if not sel.size:
            continue
        t0u, inv = np.unique(R["t0"][sel], return_inverse=True)
        f = spot_features(coin, t0u)
        mpos = np.searchsorted(MINUTES, R["minute"][sel])
        for key, arr in f.items():
            if key not in feats:
                shape = (n,) + arr.shape[2:]
                feats[key] = np.full(shape, np.nan) if arr.dtype.kind == "f" else np.zeros(shape, arr.dtype)
            feats[key][sel] = arr[inv.ravel(), mpos]
    R.update(feats)
    R["market_ids"] = [m.cid for m in mkts]
    R["slugs"] = [m.slug for m in mkts]
    meta = {"n_markets": len(mkts), "n_hourly_markets": len(h1), "seconds": round(time.time() - t_start, 1)}
    return R, meta


def take(R: dict, sel: np.ndarray) -> dict:
    return {k: (v[sel] if isinstance(v, np.ndarray) else v) for k, v in R.items()}


# --------------------------------------------------------------------------- blend


def blend_moments(R: dict, sel: np.ndarray) -> dict:
    """Standardised forecast-error moments of mu_hat_H and mu_hat_L over rows sel (section 4).

    e_X = (mu_hat_X - target) / sigma with target the rest of the hour's drift; weights 1 - t so
    the target noise is the same size on every row. S_XY are the raw weighted moments; the
    target noise (forward realised variance, standardised the same way) is subtracted to get the
    moments of the estimates' own errors, m_XY. w_H depends only on differences, where it cancels.
    """
    s = sel & np.isfinite(R["mu_H"]) & R["ok_target"] & R["ok_model"]
    w = 1.0 - R["t_hour"][s]
    sig = R["sigma"][s]
    eh = (R["mu_H"][s] - R["target"][s]) / sig
    el = (R["mu_L"][s] - R["target"][s]) / sig
    sw = w.sum()
    S = {"HH": float((w * eh * eh).sum() / sw), "LL": float((w * el * el).sum() / sw),
         "HL": float((w * eh * el).sum() / sw)}
    noise = float((R["rv_fwd"][s] / (w * sig * sig)).sum() / sw)
    m = {k: v - noise for k, v in S.items()}
    den = S["HH"] + S["LL"] - 2 * S["HL"]
    w_h = (S["LL"] - S["HL"]) / den
    v_blend = (m["HH"] * m["LL"] - m["HL"] ** 2) / den
    return {"n_rows": int(s.sum()), "n_windows": int(np.unique(R["t0"][s]).size),
            "raw_moments_per_sigma2": S, "target_noise_per_sigma2": noise,
            "error_moments_per_sigma2": m, "w_H": float(w_h), "w_L": float(1 - w_h),
            "v_blend_per_sigma2": float(v_blend),
            "mean_error_H": float((w * eh).sum() / sw), "mean_error_L": float((w * el).sum() / sw)}


def model_inputs(R: dict, prm: dict, lodo: dict) -> dict:
    """mu, v for the three variants (fitted blend, equal blend, spot only), and the stretch M."""
    n = R["mi"].size
    mhh, mll, mhl = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    for d, mom in lodo.items():
        s = R["day"] == d
        e = mom["error_moments_per_sigma2"]
        mhh[s], mll[s], mhl[s] = e["HH"], e["LL"], e["HL"]
    s2 = R["sig2"]
    vH, vL, cHL = s2 * mhh, s2 * mll, s2 * mhl
    mu_f, v_f = fm.blend(R["mu_H"], R["mu_L"], vH, vL, cHL)
    mu_e, v_e = fm.blend_equal(R["mu_H"], R["mu_L"], vH, vL, cHL)
    M = fm.stretch(R["prev"], prm["alpha"], prm["c"])
    return {"M": M, "fitted": (mu_f, np.maximum(v_f, 0.0)), "equal": (mu_e, np.maximum(v_e, 0.0)),
            "spot": (R["mu_L"], s2)}


def prob(R: dict, M, mu, v, prm: dict, theta=None) -> np.ndarray:
    th = prm["theta"] if theta is None else theta
    return fm.prob_up(R["y"], M, mu, v, R["t_hour"], R["h"], R["sigma"], th, prm["kappa0"], prm["lam"])


# --------------------------------------------------------------------------- statistics


def wilson(k: int, n: int) -> list:
    lo, hi = data._wilson(k, n)
    return [_r(lo, 4), _r(hi, 4)]


def cluster_ci(x: np.ndarray, g: np.ndarray) -> list | None:
    """Mean +- 1.96 CR1 standard errors clustered by g (the four coins of a window move together)."""
    x = np.asarray(x, float)
    if x.size < 2:
        return None
    _, inv = np.unique(g, return_inverse=True)
    s = np.bincount(inv.ravel(), weights=x - x.mean())
    ng = s.size
    if ng < 2:
        return None
    se = np.sqrt(ng / (ng - 1) * float((s * s).sum())) / x.size
    return [_r(x.mean() - Z95 * se, 4), _r(x.mean() + Z95 * se, 4)]


def calibration(p: np.ndarray, up: np.ndarray, g: np.ndarray) -> list[dict]:
    idx = np.clip(np.digitize(p, BINS) - 1, 0, BINS.size - 2)
    out = []
    for b in range(BINS.size - 1):
        s = idx == b
        n = int(s.sum())
        if n == 0:
            continue
        k = int(up[s].sum())
        out.append({"bin": f"{BINS[b]:.1f}-{BINS[b + 1]:.1f}", "n": n, "n_windows": int(np.unique(g[s]).size),
                    "mean_p": _r(p[s].mean(), 4), "win_rate": _r(k / n, 4), "wilson95": wilson(k, n),
                    "ci95_cluster_window": cluster_ci(up[s], g[s])})
    return out


def losses(p: np.ndarray, up: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    q = np.clip(p, P_CLIP, 1 - P_CLIP)
    ll = -(up * np.log(q) + (1 - up) * np.log1p(-q))
    return ll, (p - up) ** 2


def paired(a: np.ndarray, b: np.ndarray, g: np.ndarray) -> dict:
    d = a - b
    return {"mean": float(d.mean()), "n": int(d.size), "n_windows": int(np.unique(g).size),
            "t_window": _r(cluster_t(d, g))}


PROB_MODELS = ["model", "model_equal_w", "model_spot_only", "model_lam_ge_0", "market", "martingale"]
PAIRS = [("model", "market"), ("model", "martingale"), ("market", "martingale"),
         ("model_equal_w", "market"), ("model_equal_w", "model"), ("model_spot_only", "model"),
         ("model_spot_only", "market"), ("model_lam_ge_0", "model")]
PAIRS_SHORT = [("model", "market"), ("model", "martingale"), ("market", "martingale")]


def score_block(S: dict, P: dict, up=None, models=PROB_MODELS, pairs=PAIRS) -> dict:
    """Log-loss / Brier of every probability on the same rows, paired differences (a - b < 0: a better)."""
    up = S["up"] if up is None else up
    g = S["t0"]
    L = {m: losses(P[m], up) for m in models}
    return {"n": int(up.size), "n_windows": int(np.unique(g).size), "up_rate": _r(up.mean(), 4),
            "scores": {m: {"logloss": float(L[m][0].mean()), "brier": float(L[m][1].mean()),
                           "n_clipped": int(((P[m] < P_CLIP) | (P[m] > 1 - P_CLIP)).sum())}
                       for m in models},
            "logloss_diff": {f"{a}_minus_{b}": paired(L[a][0], L[b][0], g) for a, b in pairs},
            "brier_diff": {f"{a}_minus_{b}": paired(L[a][1], L[b][1], g) for a, b in pairs}}


def per_market(S: dict, rows: np.ndarray, values: np.ndarray) -> dict:
    """Aggregate entry values to one unit per market: total, mean, count and window of each market."""
    mk = S["mi"][rows]
    u, first, inv = np.unique(mk, return_index=True, return_inverse=True)
    inv = inv.ravel()
    cnt = np.bincount(inv)
    tot = np.bincount(inv, weights=values)
    return {"total": tot, "mean": tot / cnt, "count": cnt, "t0": S["t0"][rows][first], "inv": inv}


# --------------------------------------------------------------------------- taker


def favoured_up(p_up: np.ndarray) -> np.ndarray:
    """Section 6: side from sign(p - 1/2); p exactly 1/2 goes Up, as ties settle Up."""
    return p_up >= 0.5


NO_PRINT = ("quote", "quote_plus_slippage", "later", "drop")
SIDE_DEF = {"open": "mom_1h_to_open", "t": "mu_L"}  # Zayan's 1h side: finding's (headline) / at t
SRC_PRINT, SRC_NO_PRINT = 0, 1  # where an entry's price came from


def taker_trades(S: dict, p_up: np.ndarray | None, rule: str, threshold: float = 0.0,
                 side_rule: str = "favoured", decide=QUOTE, pay=FILL, order: str | None = None,
                 no_print: str = "quote", side_def: str = "open") -> dict:
    """Every decided (row, side) under one rule, and its execution. Arrays over entries (filled).

    Decision, on what is known at t. rule "model": side j (side_rule "favoured": from sign(p - 1/2);
    "both": each side tested independently, the first run's rule), decided when decide_j < a*(p_j).
    rule "zayan": the 1h side (side_def "open": sign of ln S(open) - ln S(open - 1 h), the
    2026-09-21 finding's; "t": sign of mu_L, the trailing hour at t), decided when decide_j >= threshold.

    Execution. The ask at t is the first pay_j print (first taker buy of side j in [t, t + 30]).
    A decided row with no such print is kept (whether another trader bought within 30 s is not known
    at t) and priced by no_print: "quote" its decide_j price (the ask as last seen at t); "quote_plus_
    slippage" decide_j + the mean pay_j - decide_j over this rule's decided rows that have a print;
    "later" the first taker buy of side j before the window end (none: unpriced); "drop" unpriced
    (the first run's rule: the population then depends on prints after t). order "limit" (default for
    the model): a buy limited at a*(p_j), section 6's most we would ever pay; it fills only when the
    price is <= a*, else a limit miss (no position). "market" (default for Zayan, who has no a*):
    fills at the price whatever it is. Unpriced decided rows are counted, never entered.
    """
    if no_print not in NO_PRINT:
        raise ValueError(f"no_print must be one of {NO_PRINT}")
    order = order or ("limit" if rule == "model" else "market")
    parts = []
    for j in (0, 1):
        d, a = S[decide[j]], S[pay[j]]
        if rule == "model":
            pp = p_up if j == 0 else 1.0 - p_up
            if side_rule == "both":
                side_ok = np.ones(d.size, bool)
            else:
                side_ok = favoured_up(p_up) if j == 0 else ~favoured_up(p_up)
            lim = fm.taker_break_even(pp)
            want = side_ok & np.isfinite(d) & (d < lim)
        else:
            mom = S[SIDE_DEF[side_def]]
            side_ok = mom > 0 if j == 0 else mom < 0
            want = side_ok & np.isfinite(d) & (d >= threshold)
            pp = np.full(d.size, np.nan) if p_up is None else (p_up if j == 0 else 1.0 - p_up)
            lim = np.full(d.size, np.inf)
        i = np.flatnonzero(want)
        later = S[LATER[j]][i] if no_print == "later" else np.full(i.size, np.nan)
        parts.append((i, np.full(i.size, j), d[i], a[i], pp[i], lim[i], later))
    rows, sides, quote, a, pj, lim, later = (np.concatenate(v) for v in zip(*parts))
    has = np.isfinite(a)
    slip = float((a[has] - quote[has]).mean()) if has.any() else 0.0
    imputed = {"quote": quote, "quote_plus_slippage": quote + slip, "later": later,
               "drop": np.full(rows.size, np.nan)}[no_print]
    price = np.where(has, a, imputed)
    priced = np.isfinite(price)
    fill = priced & (price <= lim + 1e-12) if order == "limit" else priced
    win_all = np.where(sides == 0, S["up"][rows], 1.0 - S["up"][rows])
    fee_all = fm.taker_fee(np.where(fill, price, 0.0))
    pnl_all = np.where(fill, win_all - np.where(fill, price, 0.0) - fee_all, 0.0)
    src = np.where(has, SRC_PRINT, SRC_NO_PRINT)
    f = fill
    return {"row": rows[f], "side": sides[f], "ask": price[f], "p": pj[f], "win": win_all[f],
            "fee": fee_all[f], "pnl": pnl_all[f], "quote": quote[f], "src": src[f],
            "decided": {"row": rows, "side": sides, "win": win_all, "quote": quote, "priced": priced,
                        "filled": fill, "pnl": pnl_all, "has_print": has},
            "rule": {"order": order, "no_print": no_print, "decide": decide[0], "pay": pay[0],
                     "side_def": side_def if rule == "zayan" else None,
                     "mean_slippage_first_post_t_print_minus_quote": slip if has.any() else None}}


def _subset(tr: dict, keep: np.ndarray) -> dict:
    n = tr["row"].size
    return {k: (v[keep] if isinstance(v, np.ndarray) and v.shape[:1] == (n,) else v) for k, v in tr.items()}


def first_per_market(S: dict, tr: dict) -> dict:
    """Keep the earliest decision minute's entry per market: one entry per market.

    An order that missed its limit at one minute is known by t + 30, before the next decision
    minute, so the market can enter later; no-print rows are priced, not dropped, so which minute
    enters no longer depends on whether someone else traded after t."""
    D = tr["decided"]
    first_dec = {"markets_decided_at_some_minute": int(np.unique(S["mi"][D["row"]]).size),
                 "markets_entered_at_some_minute": int(np.unique(S["mi"][tr["row"]]).size),
                 "decided_rows_all_minutes": int(D["row"].size),
                 "limit_miss_rows_all_minutes": int((D["priced"] & ~D["filled"]).sum()),
                 "unpriced_rows_all_minutes": int((~D["priced"]).sum())}
    if tr["row"].size == 0:
        return {**tr, "decided": None, "first_entry": first_dec}
    key = S["mi"][tr["row"]]
    order = np.lexsort((tr["side"], S["minute"][tr["row"]], key))
    _, first = np.unique(key[order], return_index=True)
    return {**_subset(tr, order[first]), "decided": None, "first_entry": first_dec}


def _pnl_cell(S: dict, rows: np.ndarray, pnl: np.ndarray, win: np.ndarray | None = None) -> dict:
    """n, c/share and window-clustered t of one set of entries (and their win rate)."""
    out = {"n": int(pnl.size)}
    if pnl.size:
        g = S["t0"][rows]
        out.update({"c_per_share": _r(100 * pnl.mean(), 3),
                    "t_window": _r(cluster_t(pnl, g)) if pnl.size > 1 else None,
                    "n_windows": int(np.unique(g).size)})
        if win is not None:
            out["win_rate"] = _r(win.mean(), 4)
    return out


def _execution_counts(S: dict, tr: dict) -> dict:
    """Decided, entered, limit-missed and unpriced counts, per decided row P&L, and the same entries
    paying the pre-t quote (matched n) - so no execution figure is read over a different population."""
    D = tr["decided"]
    if D is None:  # pooled first entry per market
        out = {"execution": tr["rule"], **tr["first_entry"]}
    else:
        has, priced, filled = D["has_print"], D["priced"], D["filled"]
        out = {"execution": tr["rule"], "decided": int(D["row"].size),
               "decided_with_first_post_t_print_within_30s": int(has.sum()),
               "decided_no_post_t_print_within_30s": int((~has).sum()),
               "entered": int(filled.sum()),
               "entered_at_post_t_print": int((filled & has).sum()),
               "entered_no_post_t_print_priced_by_rule": int((filled & ~has).sum()),
               "limit_miss_price_above_a_star": int((priced & ~filled).sum()),
               "limit_miss_of_which_post_t_print": int((priced & ~filled & has).sum()),
               "unpriced_not_entered": int((~priced).sum())}
        if D["row"].size:  # per decided row: a limit miss earns 0; unpriced rows left out (counted above)
            out["per_decided_row_limit_miss_0"] = _pnl_cell(S, D["row"][priced], D["pnl"][priced])
    if tr["row"].size and tr["rule"]["decide"].startswith("quote"):  # decided on a pre-t quote
        q = tr["quote"]
        e_q = tr["win"] - q - fm.taker_fee(q)
        out["same_entries_paying_the_pre_t_quote"] = {
            **_pnl_cell(S, tr["row"], e_q),
            "mean_price_minus_quote_c": _r(100 * (tr["ask"] - q).mean(), 3),
            "t_window_price_minus_quote": _r(cluster_t(tr["ask"] - q, S["t0"][tr["row"]])) if q.size > 1 else None}
        out["entries_by_price_source"] = {
            "first_post_t_print_within_30s": _pnl_cell(S, tr["row"][tr["src"] == SRC_PRINT],
                                                       tr["pnl"][tr["src"] == SRC_PRINT], tr["win"][tr["src"] == SRC_PRINT]),
            f"no_post_t_print_within_30s_priced_{tr['rule']['no_print']}": _pnl_cell(
                S, tr["row"][tr["src"] == SRC_NO_PRINT], tr["pnl"][tr["src"] == SRC_NO_PRINT],
                tr["win"][tr["src"] == SRC_NO_PRINT])}
    return out


def taker_stats(S: dict, tr: dict) -> dict:
    n_rows = int(S["up"].size)
    e = tr["pnl"]
    g = S["t0"][tr["row"]]
    out = {"n_rows": n_rows, "n_windows": int(np.unique(S["t0"]).size), "fills": int(e.size),
           "fills_up": int((tr["side"] == 0).sum()), "fills_down": int((tr["side"] == 1).sum()),
           **_execution_counts(S, tr)}
    if e.size == 0:
        return out
    pm = per_market(S, tr["row"], e)
    k = int(tr["win"].sum())
    out.update({
        "n_markets": int(pm["count"].size), "markets_with_2_entries": int((pm["count"] > 1).sum()),
        "c_per_share": _r(100 * e.mean(), 3), "t_window": _r(cluster_t(e, g)) if e.size > 1 else None,
        "n_fill_windows": int(np.unique(g).size),
        "win_rate": _r(k / e.size, 4), "wilson95": wilson(k, e.size),
        "win_rate_ci95_cluster_window": cluster_ci(tr["win"], g),
        "mean_ask": _r(tr["ask"].mean(), 4), "mean_fee_c": _r(100 * tr["fee"].mean(), 3),
        "total_c": _r(100 * e.sum(), 1)})
    if (pm["count"] > 1).any():  # two sides of one market: the market is the unit
        out["per_market"] = {
            "n_markets": int(pm["count"].size),
            "total_c_per_market": _r(100 * pm["total"].mean(), 3), "t_window_total": _r(cluster_t(pm["total"], pm["t0"])),
            "mean_c_per_share_per_market": _r(100 * pm["mean"].mean(), 3),
            "t_window_mean": _r(cluster_t(pm["mean"], pm["t0"]))}
    if np.isfinite(tr["p"]).all():
        f = fm.kelly_taker(tr["p"], tr["ask"])
        r = e / (tr["ask"] + tr["fee"])  # return per dollar spent
        out.update({
            "mean_p_side": _r(tr["p"].mean(), 4),
            "model_ev_c_per_share_at_fill_price": _r(100 * fm.taker_ev(tr["p"], tr["ask"]).mean(), 3),
            "kelly": {"mean_f": _r(f.mean(), 5),
                      "kelly_weighted_c_per_share": _r(100 * (f * e).sum() / f.sum(), 3) if f.sum() > 0 else None,
                      "sum_f_times_return": _r((f * r).sum(), 5),
                      "sum_log_growth": _r(np.log1p(f * r).sum(), 5),
                      "t_window_f_times_return": _r(cluster_t(f * r, g)) if e.size > 1 else None}})
    return out


# --------------------------------------------------------------------------- maker


MAKER_KEYS = ("b", "P_fill", "p_fill", "J", "kelly")
FILL_CODE = {"held": 0.0, "reset": 1.0}  # last element of a cache key


class MakerCache:
    """b* results keyed by their exact inputs, saved to MAKER_CACHE as the run goes.

    Key: (price_j, y, t, h, sigma, mu_crowd, M, mu, v, theta, kappa0, lam, +-1 side, fill code).
    Entries written before the fill convention existed (13 inputs) were computed with the anchor
    held at the quote, so they load with fill code 0 ("held").
    """

    def __init__(self, path: Path):
        self.path = path
        self.index: dict[bytes, int] = {}
        self.inp: list[np.ndarray] = []
        self.out: list[np.ndarray] = []
        if path.exists():
            z = np.load(path)
            inputs = z["inputs"]
            if inputs.shape[1] == 13:
                inputs = np.column_stack([inputs, np.full(inputs.shape[0], FILL_CODE["held"])])
            for x, y in zip(inputs, z["outputs"]):
                self._add(x, y)
        self.dirty = 0

    def _add(self, x, y):
        self.index[np.asarray(x, float).tobytes()] = len(self.inp)
        self.inp.append(np.asarray(x, float))
        self.out.append(np.asarray(y, float))

    def get(self, x):
        i = self.index.get(np.asarray(x, float).tobytes())
        return None if i is None else self.out[i]

    def put(self, x, y):
        self._add(x, y)
        self.dirty += 1
        if self.dirty >= 100:
            self.save()

    def save(self):
        if not self.inp or not self.dirty:
            return
        tmp = self.path.with_suffix(".tmp.npz")
        np.savez(tmp, inputs=np.stack(self.inp), outputs=np.stack(self.out))
        os.replace(tmp, self.path)
        self.dirty = 0


def maker_quotes(R: dict, idx: np.ndarray, sides: np.ndarray | None, mu: np.ndarray, v: np.ndarray,
                 M: np.ndarray, prm: dict, cache: MakerCache, label: str, fill_stretch: str = "reset") -> dict:
    """b* at rows idx, on side sides[i] (0 Up, 1 Down; None = both sides). The crowd drift that moves
    the fill boundary is the one that reproduces the observed 15m price at t (deviation: section 6
    writes mu_hat_H)."""
    out = {side: {k: np.full(R["mi"].size, np.nan) for k in MAKER_KEYS} for side in ("up", "down")}
    mu_c = fm.implied_drift(R["market_price_up"], R["y"], R["sigma"], R["h"])
    t_start, done, hits = time.time(), 0, 0
    for n_i, i in enumerate(idx):
        todo = ("up", "down") if sides is None else (("up", "down")[int(sides[i])],)
        for side in todo:
            price = R["market_price_up"][i] if side == "up" else 1.0 - R["market_price_up"][i]
            x = np.array([price, R["y"][i], R["t_hour"][i], R["h"][i], R["sigma"][i], mu_c[i], M[i],
                          mu[i], v[i], prm["theta"], prm["kappa0"], prm["lam"], 1.0 if side == "up" else -1.0,
                          FILL_CODE[fill_stretch]])
            y = cache.get(x)
            if y is None:
                res = fm.maker_best_bid(*x[:12], side=side, fill_stretch=fill_stretch)
                y = np.array([float(res[k]) for k in MAKER_KEYS])
                cache.put(x, y)
                done += 1
            else:
                hits += 1
            for kk, val in zip(MAKER_KEYS, y):
                out[side][kk][i] = val
        if (n_i + 1) % 250 == 0:
            el = time.time() - t_start
            print(f"  maker[{label}] {n_i + 1}/{idx.size} rows, {done} computed, {hits} cached "
                  f"({el:.0f}s, eta {el / (n_i + 1) * (idx.size - n_i - 1):.0f}s)", flush=True)
    cache.save()
    print(f"  maker[{label}] done: {done} computed, {hits} cached ({time.time() - t_start:.0f}s)", flush=True)
    return out


def maker_entries(S: dict, Q: dict, p_up: np.ndarray, side_rule: str = "favoured",
                  fill_rule: str = "up_equivalent", tick: bool = False) -> dict:
    """Quotes with J(b*) > 0 on the allowed side(s), their fill (a later print strictly below the bid)
    and P&L. side_rule "favoured": only the side of sign(p - 1/2); "both": either side where J > 0."""
    rows, sides, bid, filled, win, Pf, pf, J, bound = [], [], [], [], [], [], [], [], []
    for j, side in enumerate(("up", "down")):
        b_raw = Q[side]["b"]
        price_j = S["market_price_up"] if j == 0 else 1.0 - S["market_price_up"]
        on_bound = np.abs(b_raw - (price_j - TICK)) < ON_BOUND_TOL
        if tick:  # the highest 1c tick at or below b*; b* on the search's upper end is price_j - 1c
            # exactly (Brent stops up to xatol short of it, which a plain floor turned into 2c under).
            # Tolerance 1e-5 in price: tape prices carry float noise (0.6099997255 for 0.61).
            b_snap = np.where(on_bound, price_j - TICK, b_raw)
            b = np.floor(b_snap / TICK + TICK_TOL) * TICK
        else:
            b = b_raw.copy()
        if side_rule == "both":
            allowed = np.ones(b.size, bool)
        else:
            allowed = favoured_up(p_up) if j == 0 else ~favoured_up(p_up)
        quote = allowed & np.isfinite(b) & (Q[side]["J"] > 0) & (b >= TICK - 1e-12)
        if fill_rule == "up_equivalent":
            later = S["later_min_up"] if j == 0 else 1.0 - S["later_max_up"]
        else:  # prints on token j only
            later = S["later_min_tok0"] if j == 0 else S["later_min_tok1"]
        # strictly below the bid by more than the tape's float noise (a print stored as 0.5599997 is 0.56:
        # not below a bid on the 0.56 tick)
        f = np.isfinite(later) & (later < b - TICK_TOL * TICK)
        i = np.flatnonzero(quote)
        rows.append(i)
        sides.append(np.full(i.size, j))
        bid.append(b[i])
        filled.append(f[i])
        win.append(S["up"][i] if j == 0 else 1.0 - S["up"][i])
        Pf.append(Q[side]["P_fill"][i])
        pf.append(Q[side]["p_fill"][i])
        J.append(Q[side]["J"][i])
        bound.append(on_bound[i])
    rows, sides, bid, filled, win, Pf, pf, J, bound = (
        np.concatenate(x) for x in (rows, sides, bid, filled, win, Pf, pf, J, bound))
    return {"row": rows, "side": sides, "bid": bid, "filled": filled, "win": win, "P_fill": Pf,
            "p_fill": pf, "J": J, "on_bound": bound, "pnl": np.where(filled, win - bid, 0.0)}


def first_quote_per_market(S: dict, me: dict) -> dict:
    """Earliest quoting minute per market; the bid then rests to the window end."""
    if me["row"].size == 0:
        return me
    key = S["mi"][me["row"]]
    order = np.lexsort((me["side"], S["minute"][me["row"]], key))
    _, first = np.unique(key[order], return_index=True)
    return _subset(me, order[first])


def _maker_core(S: dict, me: dict) -> dict:
    out = {"quotes": int(me["row"].size), "quotes_up": int((me["side"] == 0).sum()),
           "quotes_down": int((me["side"] == 1).sum())}
    if me["row"].size == 0:
        return out
    g = S["t0"][me["row"]]
    f = me["filled"]
    nf = int(f.sum())
    ev = me["pnl"]
    out.update({
        "fills": nf, "fill_rate": _r(nf / me["row"].size, 4), "wilson95_fill_rate": wilson(nf, me["row"].size),
        "fill_rate_ci95_cluster_window": cluster_ci(f.astype(float), g),
        "model_mean_P_fill": _r(me["P_fill"].mean(), 4), "mean_bid": _r(me["bid"].mean(), 4),
        "ev_c_per_quote": _r(100 * ev.mean(), 3), "t_window_ev_per_quote": _r(cluster_t(ev, g)) if ev.size > 1 else None,
        "model_J_c_per_quote": _r(100 * me["J"].mean(), 3)})
    if nf:
        e = me["win"][f] - me["bid"][f]
        k = int(me["win"][f].sum())
        out.update({
            "c_per_share_per_fill": _r(100 * e.mean(), 3),
            "t_window_per_fill": _r(cluster_t(e, g[f])) if nf > 1 else None,
            "n_fill_windows": int(np.unique(g[f]).size),
            "win_rate_on_fills": _r(k / nf, 4), "wilson95_win_on_fills": wilson(k, nf),
            "win_on_fills_ci95_cluster_window": cluster_ci(me["win"][f], g[f]),
            "mean_bid_on_fills": _r(me["bid"][f].mean(), 4),
            "model_mean_p_fill_on_fills": _r(me["p_fill"][f].mean(), 4)})
    return out


def maker_stats(S: dict, me: dict, split: bool = True) -> dict:
    """Maker results with the market as the unit, split by where b* sits (review finding 11) and, when
    a market quotes both sides, per market with one-sided and two-sided fills apart (finding 5)."""
    out = {"n_rows": int(S["up"].size), "n_windows": int(np.unique(S["t0"]).size), **_maker_core(S, me)}
    if me["row"].size == 0:
        return out
    pm = per_market(S, me["row"], me["pnl"])
    out["n_markets_quoted"] = int(pm["count"].size)
    out["markets_quoting_both_sides"] = int((pm["count"] > 1).sum())
    out["share_quotes_b_star_on_upper_bound"] = _r(me["on_bound"].mean(), 4)
    if split:
        out["by_b_star_position"] = {
            "on_upper_bound_last_print_minus_1c": _maker_core(S, _subset(me, me["on_bound"])),
            "interior_J_prime_zero": _maker_core(S, _subset(me, ~me["on_bound"]))}
    if (pm["count"] > 1).any():
        inv = pm["inv"]
        nfill = np.bincount(inv, weights=me["filled"].astype(float))
        fill_e = np.where(me["filled"], me["win"] - me["bid"], 0.0)
        has_fill = nfill > 0
        per_fill = np.bincount(inv, weights=fill_e)[has_fill] / nfill[has_fill]
        one = me["filled"] & (nfill[inv] == 1)
        two_mk = np.flatnonzero(nfill == 2)
        pair = np.array([1.0 - me["bid"][(inv == k) & me["filled"]].sum() for k in two_mk])
        e1 = me["win"][one] - me["bid"][one]
        g1 = S["t0"][me["row"][one]]
        out["per_market"] = {
            "n_markets": int(pm["count"].size),
            "total_c_per_market": _r(100 * pm["total"].mean(), 3), "t_window_total": _r(cluster_t(pm["total"], pm["t0"])),
            "mean_c_per_quote_per_market": _r(100 * pm["mean"].mean(), 3),
            "t_window_mean_per_quote": _r(cluster_t(pm["mean"], pm["t0"])),
            "markets_with_a_fill": int(has_fill.sum()),
            "c_per_share_per_fill_per_market": _r(100 * per_fill.mean(), 3),
            "t_window_per_fill_per_market": _r(cluster_t(per_fill, pm["t0"][has_fill])),
            "one_sided_fills": {"n": int(one.sum()), "c_per_share": _r(100 * e1.mean(), 3) if one.any() else None,
                                "t_window": _r(cluster_t(e1, g1)) if one.sum() > 1 else None,
                                "win_rate": _r(me["win"][one].mean(), 4) if one.any() else None},
            "two_sided_fills": {"n_markets": int(two_mk.size),
                                "c_per_pair_1_minus_bUp_minus_bDown": _r(100 * pair.mean(), 3) if two_mk.size else None,
                                "t_window": _r(cluster_t(pair, pm["t0"][two_mk])) if two_mk.size > 1 else None}}
    return out


# --------------------------------------------------------------------------- theta


def theta_refit(S: dict, M: np.ndarray, mu: np.ndarray, v: np.ndarray, prm: dict) -> dict:
    """Diagnostic, not pre-registered: theta by maximum likelihood on the tape with the rest frozen."""
    a, G, V1 = fm.ou_moments(S["t_hour"], S["h"], prm["kappa0"], prm["lam"], 1.0)
    num0 = S["y"] - a * M
    sgn = 2.0 * S["up"] - 1.0

    def ll(th):
        z = (num0 + th * mu * G) / np.sqrt(S["sig2"] * V1 + th * th * v * G * G)
        return log_ndtr(sgn * z)

    res = minimize_scalar(lambda th: -float(ll(th).sum()), bounds=(-50.0, 50.0), method="bounded",
                          options={"xatol": 1e-9})
    th = float(res.x)
    eps = 1e-3 * max(1.0, abs(th))
    lp, l0, lm = ll(th + eps), ll(th), ll(th - eps)
    hess = -float((lp - 2 * l0 + lm).sum()) / eps ** 2
    score = (lp - lm) / (2 * eps)
    _, inv = np.unique(S["t0"], return_inverse=True)
    sg = np.bincount(inv.ravel(), weights=score)
    ng = sg.size
    se = float(np.sqrt(ng / (ng - 1) * (sg * sg).sum()) / hess) if hess > 0 else float("nan")
    frozen = float(-ll(prm["theta"]).sum())
    return {"theta_hat": th, "se_cluster_window": se, "z": _r(th / se) if se > 0 else None,
            "n": int(S["up"].size), "n_windows": int(ng),
            "nll_per_row_at_theta_hat": float(-l0.mean()), "nll_per_row_at_frozen_theta": frozen / S["up"].size,
            "on_bound": bool(abs(abs(th) - 50.0) < 1e-3)}


def side_vs_momentum(S: dict, p: np.ndarray, p_theta0: np.ndarray, tr: dict) -> dict:
    """Does the model's favoured side go with or against the trailing 1h spot return?"""
    mom = np.sign(S["mu_L"])
    nz = (mom != 0) & (p != 0.5)
    fav = np.sign(p - 0.5)
    fav0 = np.sign(p_theta0 - 0.5)
    ent = tr["row"]
    ent_side = np.where(tr["side"] == 0, 1.0, -1.0)
    ent_ok = mom[ent] != 0
    return {
        "n_rows": int(nz.sum()),
        "share_rows_model_favours_against_1h_move": _r((fav[nz] == -mom[nz]).mean(), 4),
        "share_rows_with_theta_0_against_1h_move": _r((fav0[nz] == -mom[nz]).mean(), 4),
        "rows_where_theta_flips_favoured_side": int((fav != fav0).sum()),
        "mean_abs_p_change_from_theta": _r(np.abs(p - p_theta0).mean(), 6),
        "max_abs_p_change_from_theta": _r(np.abs(p - p_theta0).max(), 6),
        "taker_entries": int(ent_ok.sum()),
        "share_taker_entries_against_1h_move": _r((ent_side[ent_ok] == -mom[ent][ent_ok]).mean(), 4)
        if ent_ok.any() else None,
    }


def crowd_prices_reversion(S: dict, P: dict) -> dict:
    """Diagnostic, not pre-registered: split rows by the sign of the previous 15m candle and compare
    the crowd's Up price, the model's p and the real Up rate. If the crowd prices the reversion, its
    price is lower after an up candle than after a down candle, by about as much as the model's."""
    prev = S["prev"][:, 0]
    out = {}
    for label, s in (("previous_candle_up", prev > 0), ("previous_candle_down", prev < 0)):
        k, n = int(S["up"][s].sum()), int(s.sum())
        out[label] = {"n": n, "mean_market_up": _r(P["market"][s].mean(), 4), "mean_model_p": _r(P["model"][s].mean(), 4),
                      "mean_martingale_p": _r(P["martingale"][s].mean(), 4),
                      "up_rate": _r(k / n, 4) if n else None, "wilson95": wilson(k, n),
                      "ci95_cluster_window": cluster_ci(S["up"][s], S["t0"][s])}
    both = prev != 0
    x = np.sign(prev[both])  # +1 after an up candle
    g = S["t0"][both]
    hit = (S["up"][both] > 0) == (x < 0)  # the outcome went against the previous candle
    k, n = int(hit.sum()), int(hit.size)
    out["outcome_against_previous_candle"] = {"n": n, "rate": _r(k / n, 4) if n else None, "wilson95": wilson(k, n),
                                              "ci95_cluster_window": cluster_ci(hit.astype(float), g),
                                              "t_window": _r(cluster_t(hit - 0.5, g))}
    for key in ("market", "model"):
        lean = -x * (P[key][both] - 0.5)  # > 0: the price leans against the previous candle
        out[f"{key}_lean_against_previous_candle_c"] = {"mean": _r(100 * lean.mean(), 3), "n": n,
                                                        "t_window": _r(cluster_t(lean, g))}
    return out


# --------------------------------------------------------------------------- report


ZAYAN = {f"zayan_ge_{int(100 * th)}c": th for th in ZAYAN_THRESHOLDS}


def maker_block(S: dict, spec: dict, full: bool = True) -> dict:
    """Maker results of one quote set: spec = {Q, p_up, side_rule, what}."""
    Q, p_up, side_rule = spec["Q"], spec["p_up"], spec["side_rule"]
    out = {"what": spec["what"],
           "bid_b_star_up_equivalent_prints": maker_stats(S, maker_entries(S, Q, p_up, side_rule))}
    if full:
        out["bid_floored_to_1c_tick"] = maker_stats(S, maker_entries(S, Q, p_up, side_rule, tick=True), split=False)
        out["fill_from_prints_on_token_j_only"] = maker_stats(
            S, maker_entries(S, Q, p_up, side_rule, fill_rule="token"), split=False)
    return out


def _has(Q: dict) -> bool:
    return bool(np.isfinite(Q["up"]["J"]).any() or np.isfinite(Q["down"]["J"]).any())


def block(S: dict, P: dict, Qs: dict, sensitivities: bool = True) -> dict:
    """Everything for one set of rows: calibration, scores, taker, baselines, maker.

    sensitivities=False (the exploratory quarter / asset tables): the pre-registered rules only."""
    out = {}
    g = S["t0"]
    if sensitivities:
        out["calibration"] = {m: calibration(P[m], S["up"], g) for m in PROB_MODELS}
        out.update(score_block(S, P))
    else:
        out.update(score_block(S, P, models=["model", "market", "martingale"], pairs=PAIRS_SHORT))
    taker = {"model": taker_stats(S, taker_trades(S, P["model"], "model")),
             **{k: taker_stats(S, taker_trades(S, None, "zayan", th)) for k, th in ZAYAN.items()}}
    if sensitivities:
        pm = P["model"]
        taker["sensitivities"] = {
            "note": "every execution variant below is on the headline rule's decided rows (same decision); "
                    "each gives decided / entered / limit-miss / unpriced counts beside its P&L",
            "model_equal_w": taker_stats(S, taker_trades(S, P["model_equal_w"], "model")),
            "model_spot_only": taker_stats(S, taker_trades(S, P["model_spot_only"], "model")),
            "model_lam_ge_0": taker_stats(S, taker_trades(S, P["model_lam_ge_0"], "model")),
            "model_market_order_pays_above_a_star": taker_stats(S, taker_trades(S, pm, "model", order="market")),
            "model_no_print_rows_at_quote_plus_mean_slippage": taker_stats(
                S, taker_trades(S, pm, "model", no_print="quote_plus_slippage")),
            "model_no_print_rows_at_first_later_taker_buy": taker_stats(
                S, taker_trades(S, pm, "model", no_print="later")),
            "model_no_print_rows_dropped_conditions_on_prints_after_t": taker_stats(
                S, taker_trades(S, pm, "model", no_print="drop")),
            "previous_headline_market_order_no_print_rows_dropped": taker_stats(
                S, taker_trades(S, pm, "model", order="market", no_print="drop")),
            "model_pay_the_pre_t_quote_every_decided_row": taker_stats(S, taker_trades(S, pm, "model", pay=QUOTE)),
            "model_both_sides_independently": taker_stats(S, taker_trades(S, pm, "model", side_rule="both")),
            "model_direct_buy_prints_only": taker_stats(
                S, taker_trades(S, pm, "model", decide=QUOTE_DIRECT, pay=FILL_DIRECT)),
            "first_run_rule_decide_and_pay_post_t_print_uses_information_after_t": {
                "model_both_sides_market_order": taker_stats(
                    S, taker_trades(S, pm, "model", side_rule="both", decide=FILL, pay=FILL, order="market")),
                **{k: taker_stats(S, taker_trades(S, None, "zayan", th, decide=FILL, pay=FILL, side_def="t"))
                   for k, th in ZAYAN.items()}},
            **{f"{k}_side_from_trailing_hour_at_t_mu_L": taker_stats(
                S, taker_trades(S, None, "zayan", th, side_def="t")) for k, th in ZAYAN.items()},
            **{f"{k}_no_print_rows_dropped": taker_stats(S, taker_trades(S, None, "zayan", th, no_print="drop"))
               for k, th in ZAYAN.items()},
            **{f"{k}_pay_the_pre_t_quote_every_decided_row": taker_stats(
                S, taker_trades(S, None, "zayan", th, pay=QUOTE)) for k, th in ZAYAN.items()}}
    out["taker"] = taker
    out["maker"] = {}
    for label, spec in Qs.items():
        if label != "model" and not sensitivities:
            continue
        if _has(spec["Q"]):
            out["maker"][label] = maker_block(S, spec, full=sensitivities)
    return out


def vs_binance(S: dict, P: dict, Qs: dict) -> dict:
    """Review finding 10: the model is fed Binance, the market settles on Chainlink. The same rows scored
    against Binance's own direction, and the real-resolution gap split by whether Binance agrees."""
    bu = S["binance_up"].astype(float)
    Sb = {**S, "up": bu}
    out = {"note": "target = Binance close >= open over the window (the model's own feed) instead of the real "
                   "Chainlink resolution; the agree/disagree split conditions on the outcome, and scoring against "
                   "Binance handicaps the market as the real target handicaps the model, so neither is the truth. "
                   "model_minus_martingale (both fed Binance) isolates the model's own terms under either target.",
           **score_block(Sb, P, models=["model", "market", "martingale"], pairs=PAIRS_SHORT),
           "taker": {"model": taker_stats(Sb, taker_trades(Sb, P["model"], "model")),
                     **{k: taker_stats(Sb, taker_trades(Sb, None, "zayan", th)) for k, th in ZAYAN.items()}}}
    if _has(Qs["model"]["Q"]):
        out["maker"] = {"model": maker_stats(Sb, maker_entries(Sb, Qs["model"]["Q"], Qs["model"]["p_up"]), split=False)}
    dis = bu != S["up"]
    g = S["t0"]
    lm, lk = losses(P["model"], S["up"])[0], losses(P["market"], S["up"])[0]
    d = lm - lk
    split = {"n_rows": int(d.size), "n_binance_disagrees_with_resolution": int(dis.sum()),
             "real_target_model_minus_market_logloss": paired(lm, lk, g)}
    for lab, s in (("rows_binance_disagrees", dis), ("rows_binance_agrees", ~dis)):
        cell = {"n": int(s.sum()), "contribution_to_mean_gap": float(d[s].sum() / d.size)}
        if s.sum() > 1:
            cell["model_minus_market_logloss_per_row"] = paired(lm[s], lk[s], g[s])
            Ss = take(S, s)
            cell["taker_model_real_target"] = taker_stats(Ss, taker_trades(Ss, P["model"][s], "model"))
            if _has(Qs["model"]["Q"]):
                Qm = {side: {k: a[s] for k, a in Qs["model"]["Q"][side].items()} for side in ("up", "down")}
                cell["maker_model_real_target"] = maker_stats(Ss, maker_entries(Ss, Qm, Qs["model"]["p_up"][s]),
                                                              split=False)
        split[lab] = cell
    out["real_resolution_gap_by_binance_agreement"] = split
    return out


def slice_Qs(Qs: dict, sel: np.ndarray) -> dict:
    return {lab: {**spec, "Q": {side: {k: a[sel] for k, a in spec["Q"][side].items()} for side in ("up", "down")},
                  "p_up": spec["p_up"][sel]} for lab, spec in Qs.items()}


def sub_blocks(S: dict, P: dict, Qs: dict, key: str, values, names=None) -> list[dict]:
    rows = []
    for v in values:
        sel = S[key] == v
        if not sel.any():
            continue
        Ss = take(S, sel)
        Ps = {m: P[m][sel] for m in P}
        rows.append({key: names[v] if names else int(v), **block(Ss, Ps, slice_Qs(Qs, sel), sensitivities=False)})
    return rows


def pooled_first_entry(S: dict, P: dict, Qs: dict) -> dict:
    """All decision minutes pooled, the earliest qualifying minute per market (one entry per market)."""
    out = {"taker": {
        "model": taker_stats(S, first_per_market(S, taker_trades(S, P["model"], "model"))),
        "model_equal_w": taker_stats(S, first_per_market(S, taker_trades(S, P["model_equal_w"], "model"))),
        **{k: taker_stats(S, first_per_market(S, taker_trades(S, None, "zayan", th))) for k, th in ZAYAN.items()},
        "sensitivity_model_market_order": taker_stats(
            S, first_per_market(S, taker_trades(S, P["model"], "model", order="market"))),
        "sensitivity_previous_rule_market_order_no_print_rows_dropped": taker_stats(
            S, first_per_market(S, taker_trades(S, P["model"], "model", order="market", no_print="drop")))}}
    spec = Qs.get("model")
    if spec is not None and _has(spec["Q"]):
        out["maker"] = {"model": maker_stats(S, first_quote_per_market(S, maker_entries(S, spec["Q"], spec["p_up"])))}
    return out


def headline_block(b: dict) -> dict:
    """The pre-registered headline, separate from every sensitivity and exploratory table."""
    mk = b["maker"].get("model", {}).get("bid_b_star_up_equivalent_prints")
    return {
        "what": "minute 2, fitted blend, frozen lam-free params; side from sign(p - 1/2), one entry per market; "
                "taker decides on the last pre-t taker quote (quote < a*) and sends a buy limited at a*: it fills "
                "at the first post-t taker print (within 30 s) when that is <= a* (else a limit miss), and a "
                "decided row with no such print is kept and filled at its pre-t quote; Zayan baselines take the "
                "1h side of the 2026-09-21 finding (window open - 1 h -> open) as market orders with the same "
                "pricing; maker rests b* with p_fill re-evaluated at the fill state; t clustered by window",
        "n_rows": b["n"], "n_windows": b["n_windows"],
        "calibration": {m: b["calibration"][m] for m in ("model", "market")},
        "logloss_diff": {k: b["logloss_diff"][k] for k in ("model_minus_market", "model_minus_martingale",
                                                           "market_minus_martingale")},
        "brier_diff": {k: b["brier_diff"][k] for k in ("model_minus_market", "model_minus_martingale",
                                                       "market_minus_martingale")},
        "taker": {k: b["taker"][k] for k in ("model", *ZAYAN)},
        "taker_execution_sensitivities_same_decided_rows": {
            k: {kk: b["taker"]["sensitivities"][k].get(kk) for kk in (
                "decided", "entered", "limit_miss_price_above_a_star", "unpriced_not_entered", "fills",
                "c_per_share", "t_window", "win_rate", "per_decided_row_limit_miss_0",
                "same_entries_paying_the_pre_t_quote")}
            for k in ("model_market_order_pays_above_a_star", "model_no_print_rows_at_quote_plus_mean_slippage",
                      "model_no_print_rows_at_first_later_taker_buy",
                      "model_no_print_rows_dropped_conditions_on_prints_after_t",
                      "previous_headline_market_order_no_print_rows_dropped",
                      "model_pay_the_pre_t_quote_every_decided_row",
                      *[f"{z}_side_from_trailing_hour_at_t_mu_L" for z in ZAYAN],
                      *[f"{z}_no_print_rows_dropped" for z in ZAYAN])},
        "maker": mk,
    }


def multiple_comparisons(report: dict) -> dict:
    """Review finding 9: every t computed in this report, and a Holm / BH adjustment within each table."""
    found: list[tuple[str, float]] = []

    def walk(x, path):
        if isinstance(x, dict):
            for k, v in x.items():
                k = str(k)
                if (k.startswith("t_") or k in ("t", "z")) and isinstance(v, (int, float)) and not isinstance(v, bool):
                    found.append((f"{path}/{k}", float(v)))
                else:
                    walk(v, f"{path}/{k}")
        elif isinstance(x, list):
            for i, v in enumerate(x):
                walk(v, f"{path}[{i}]")

    walk({k: v for k, v in report.items() if k not in ("multiple_comparisons", "headline")}, "")
    fams: dict[str, list[tuple[str, float]]] = {}
    for path, t in found:
        parts = path.split("/")  # a table = one section of one minute's block, or one top-level section
        fam = "/".join(parts[:4]) if parts[1] == "by_minute" else "/".join(parts[:3])
        for tag in ("by_quarter_k_exploratory", "by_asset_exploratory", "by_quarter_k_first_entry",
                    "by_asset_first_entry"):
            if f"/{tag}" in path:
                fam = path.split(f"/{tag}")[0] + f"/{tag}"
        fams.setdefault(fam, []).append((path, t))
    families = {}
    for fam, cells in fams.items():
        p = [data.p_two_sided(t) for _, t in cells]
        ph, pb = data.holm(p), data.bh(p)
        families[fam] = {"n_t": len(cells), "n_abs_t_ge_2": int(sum(abs(t) >= 2 for _, t in cells)),
                         "holm_p_below_0.05": [c for (c, _), h in zip(cells, ph) if h < 0.05],
                         "bh_p_below_0.05": [c for (c, _), q in zip(cells, pb) if q < 0.05]}
    a = np.array([t for _, t in found])
    return {"n_t_statistics": int(a.size), "n_abs_t_ge_2": int((np.abs(a) >= 2).sum()),
            "expected_abs_t_ge_2_if_all_null_and_independent": round(a.size * data.p_two_sided(2.0), 1),
            "note": "t-statistics from many overlapping cells (minutes, rules, sensitivities, quarters, assets) are "
                    "strongly dependent; the adjustment is within each table (family). The pre-registered headline "
                    "is 'headline'; quarter and asset tables are exploratory.",
            "families": families}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--no-maker", action="store_true", help="skip the maker b* search (slow part)")
    ap.add_argument("--maker-equal-all-minutes", action="store_true",
                    help="also run the equal-weight maker at every minute (default: headline minute only)")
    ap.add_argument("--out", default=str(OUT), help="report path")
    args = ap.parse_args()
    t_start = time.time()

    prm_file = json.loads(PARAMS.read_text())
    prm = prm_file["params"]
    prm0 = prm_file["params_lam_ge_0"]
    R, meta = build_rows()
    n = R["mi"].size
    has_price = np.isfinite(R["market_price_up"])
    used = has_price & R["ok_model"]
    R["mu_H"] = fm.mu_hat_H(R["m_H"], R["x"], R["sigma"], R["t_hour"])

    # Coverage.
    cov = {"markets": meta["n_markets"], "hourly_markets": meta["n_hourly_markets"],
           "markets_with_no_prints_from_open_minus_60s_to_end": int((R["n_prints_total"][R["minute"] == 0] == 0).sum()),
           "rows_total": int(n), "rows_used": int(used.sum()),
           "rows_skipped_no_15m_print_in_last_60s": int((~has_price).sum()),
           "rows_skipped_missing_spot_minutes": int((has_price & ~R["ok_model"]).sum()),
           "by_minute": []}
    for m in MINUTES:
        s = R["minute"] == m
        h1c = s & (R["coin"] != COINS.index("sol"))
        su = s & used
        delays = np.concatenate([R["ask_up_delay_s"][su], R["ask_dn_delay_s"][su]])
        delays = delays[np.isfinite(delays)]
        cov["by_minute"].append({
            "minute": int(m), "rows": int(s.sum()), "used": int(su.sum()),
            "skipped_no_15m_print": int((s & ~has_price).sum()),
            "btc_eth_xrp_rows_used": int((h1c & used).sum()),
            "with_m_H": int((h1c & used & np.isfinite(R["m_H"])).sum()),
            "btc_eth_xrp_rows_with_no_hourly_market": int((h1c & used & ~R["has_h1_market"]).sum()),
            "with_pre_t_taker_quote_up": int((su & np.isfinite(R["quote_up"])).sum()),
            "with_pre_t_taker_quote_down": int((su & np.isfinite(R["quote_dn"])).sum()),
            "with_post_t_taker_fill_up": int((su & np.isfinite(R["ask_up"])).sum()),
            "with_post_t_taker_fill_down": int((su & np.isfinite(R["ask_dn"])).sum()),
            "with_taker_buy_t_to_window_end_up": int((su & np.isfinite(R["later_up"])).sum()),
            "with_taker_buy_t_to_window_end_down": int((su & np.isfinite(R["later_dn"])).sum()),
            "post_t_fill_delay_s_median_p90": [_r(np.median(delays), 1), _r(np.percentile(delays, 90), 1)]
            if delays.size else None})
    used_rows = np.flatnonzero(used)
    agree = (R["binance_up"][used] == (R["up"][used] > 0)).mean()
    cov["binance_direction_agrees_with_resolution_on_used_rows"] = _r(agree, 4)
    print(f"rows {n}, used {used.sum()} ({cov['rows_skipped_no_15m_print_in_last_60s']} without a 15m print)",
          flush=True)

    # Blend moments, leave one day out.
    days = np.unique(R["day"])
    lodo = {int(d): blend_moments(R, R["day"] != d) for d in days}
    blend_report = {"per_heldout_day": {datetime.fromtimestamp(int(d) * 86400, UTC).strftime("%Y-%m-%d"): {
        **lodo[int(d)], "heldout_rows_used": int((used & (R["day"] == d)).sum())} for d in days},
        "all_four_days_descriptive": blend_moments(R, np.ones(n, bool)),
        "by_quarter_k_all_days_descriptive_not_used": {
            int(k): blend_moments(R, R["k"] == k) for k in range(1, 5)}}
    for d in days:
        print(f"  blend held-out {datetime.fromtimestamp(int(d) * 86400, UTC):%m-%d}: "
              f"w_H {lodo[int(d)]['w_H']:.3f} (n {lodo[int(d)]['n_rows']})", flush=True)

    X = model_inputs(R, prm, lodo)
    X0 = model_inputs(R, prm0, lodo)  # lam >= 0 params: same blend, own stretch (alpha, c)
    M, M0 = X["M"], X0["M"]
    P_all = {"model": prob(R, M, *X["fitted"], prm), "model_equal_w": prob(R, M, *X["equal"], prm),
             "model_spot_only": prob(R, M, *X["spot"], prm),
             "model_lam_ge_0": prob(R, M0, *X0["fitted"], prm0),
             "market": R["market_price_up"].copy(),
             "martingale": fm.martingale_prob_up(R["y"], R["h"], R["sigma"])}
    p_theta0 = prob(R, M, *X["fitted"], prm, theta=0.0)

    # Maker b*. Headline: lam-free params, fitted blend, reset, favoured side, every minute. At the
    # headline minute, a chain of sensitivities from the first run's rule, one change at a time.
    def side_of(p):
        return np.where(favoured_up(p), 0, 1)

    nanQ = {side: {k: np.full(n, np.nan) for k in MAKER_KEYS} for side in ("up", "down")}
    maker_specs = {
        "model": ("headline: lam-free params, fitted blend, p_fill reset at the fill, favoured side", "favoured",
                  P_all["model"]),
        "model_equal_w": ("equal-weight blend; otherwise the headline", "favoured", P_all["model_equal_w"]),
        "model_both_sides": ("both sides quoted wherever J > 0 (the first run's side rule); otherwise the headline. "
                             "Per market in per_market, per (market, side) at the top level", "both", P_all["model"]),
        "model_lam_ge_0": ("lam >= 0 params; otherwise the headline", "favoured", P_all["model_lam_ge_0"]),
        "model_lam_ge_0_anchor_held": ("lam >= 0 params, p_fill with the anchor held at the quote; favoured side",
                                       "favoured", P_all["model_lam_ge_0"]),
        "first_run_rule_lam_ge_0_anchor_held_both_sides": (
            "the first run's maker: lam >= 0, anchor held, both sides quoted wherever J > 0; results per market "
            "(per_market) and per (market, side) (top level, as first reported)", "both", P_all["model_lam_ge_0"]),
    }
    Qs = {lab: {"Q": {s: {k: a.copy() for k, a in d.items()} for s, d in nanQ.items()}, "p_up": p,
                "side_rule": rule, "what": what} for lab, (what, rule, p) in maker_specs.items()}
    if not args.no_maker:
        cache = MakerCache(MAKER_CACHE)
        head = used_rows[R["minute"][used_rows] == HEADLINE]
        mu_f, v_f = X["fitted"]
        # the cached first-run sets first (no computation), then the new ones
        Qs["first_run_rule_lam_ge_0_anchor_held_both_sides"]["Q"] = maker_quotes(
            R, head, None, mu_f, v_f, M0, prm0, cache, "lam>=0 held both", "held")
        Qs["model_lam_ge_0_anchor_held"]["Q"] = maker_quotes(
            R, head, side_of(P_all["model_lam_ge_0"]), mu_f, v_f, M0, prm0, cache, "lam>=0 held", "held")
        Qs["model"]["Q"] = maker_quotes(R, used_rows, side_of(P_all["model"]), mu_f, v_f, M, prm, cache,
                                        "headline", "reset")
        Qs["model_both_sides"]["Q"] = maker_quotes(R, head, None, mu_f, v_f, M, prm, cache, "both sides", "reset")
        Qs["model_lam_ge_0"]["Q"] = maker_quotes(
            R, head, side_of(P_all["model_lam_ge_0"]), mu_f, v_f, M0, prm0, cache, "lam>=0", "reset")
        mu_e, v_e = X["equal"]
        eq_rows = used_rows if args.maker_equal_all_minutes else head
        Qs["model_equal_w"]["Q"] = maker_quotes(R, eq_rows, side_of(P_all["model_equal_w"]), mu_e, v_e, M, prm,
                                                cache, "equal w", "reset")

    S_all = take(R, used)
    P_used = {m: P_all[m][used] for m in P_all}
    Q_used = slice_Qs(Qs, used)

    by_minute = {}
    for m in MINUTES:
        s = S_all["minute"] == m
        Ss = take(S_all, s)
        Ps = {k: v[s] for k, v in P_used.items()}
        Qss = slice_Qs(Q_used, s)
        b = block(Ss, Ps, Qss)
        b["vs_binance_direction"] = vs_binance(Ss, Ps, Qss)
        b["by_quarter_k_exploratory"] = sub_blocks(Ss, Ps, Qss, "k", range(1, 5))
        b["by_asset_exploratory"] = sub_blocks(Ss, Ps, Qss, "coin", range(4), names=COINS)
        tr = taker_trades(Ss, Ps["model"], "model")
        b["model_side_vs_1h_momentum"] = side_vs_momentum(Ss, Ps["model"], p_theta0[used][s], tr)
        b["does_the_crowd_price_15m_reversion_diagnostic"] = {
            "all": crowd_prices_reversion(Ss, Ps),
            "by_quarter_k": {int(k): crowd_prices_reversion(take(Ss, Ss["k"] == k), {q: v[Ss["k"] == k] for q, v in Ps.items()})
                             for k in range(1, 5)}}
        by_minute[str(int(m))] = b
        tk = b["taker"]
        mk = b["maker"].get("model", {}).get("bid_b_star_up_equivalent_prints", {})
        print(f"  minute {m}: n {b['n']}  dLL model-market {b['logloss_diff']['model_minus_market']['mean']:+.4f} "
              f"(t {b['logloss_diff']['model_minus_market']['t_window']})  taker model {tk['model'].get('fills')} "
              f"{tk['model'].get('c_per_share')}c (t {tk['model'].get('t_window')}); zayan40 "
              f"{tk['zayan_ge_40c'].get('fills')} {tk['zayan_ge_40c'].get('c_per_share')}c "
              f"(t {tk['zayan_ge_40c'].get('t_window')}); zayan55 {tk['zayan_ge_55c'].get('fills')} "
              f"{tk['zayan_ge_55c'].get('c_per_share')}c; maker {mk.get('quotes')} quotes "
              f"{mk.get('ev_c_per_quote')}c/quote (t {mk.get('t_window_ev_per_quote')})", flush=True)

    pooled = {"scores_all_minutes": score_block(S_all, P_used),
              "calibration_all_minutes": {m: calibration(P_used[m], S_all["up"], S_all["t0"]) for m in PROB_MODELS},
              "first_entry_per_market": pooled_first_entry(S_all, P_used, Q_used),
              "by_quarter_k_first_entry": [], "by_asset_first_entry": []}
    for key, vals, names in (("k", range(1, 5), None), ("coin", range(4), COINS)):
        for v in vals:
            sel = S_all[key] == v
            Ss = take(S_all, sel)
            Ps = {m: P_used[m][sel] for m in P_used}
            pooled["by_quarter_k_first_entry" if key == "k" else "by_asset_first_entry"].append(
                {key: names[v] if names else int(v), **pooled_first_entry(Ss, Ps, slice_Qs(Q_used, sel))})

    # Theta: the frozen sign, and a tape refit of theta alone (diagnostic).
    theta = {
        "frozen_theta": prm["theta"], "frozen_theta_z_cluster_window": prm_file["robust"]["z_cluster_window"].get("theta"),
        "frozen_theta_lam_ge_0_sensitivity": prm0["theta"],
        "frozen_meaning": ("theta < 0: the model fades the momentum estimate (the fitted blend of the 1h market's "
                           "implied drift and the trailing 1h spot return)" if prm["theta"] < 0 else
                           "theta > 0: the model follows the momentum estimate"),
        "tape_refit_theta_only_diagnostic": {}}
    for label, (mu, v) in (("fitted_w", X["fitted"]), ("equal_w", X["equal"]), ("spot_only", X["spot"])):
        hm = S_all["minute"] == HEADLINE
        theta["tape_refit_theta_only_diagnostic"][label] = {
            "all_minutes": theta_refit(S_all, M[used], mu[used], v[used], prm),
            "headline_minute": theta_refit(take(S_all, hm), M[used][hm], mu[used][hm], v[used][hm], prm)}

    rusage_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20  # macOS: bytes
    report = {
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),
        "headline": headline_block(by_minute[str(HEADLINE)]),
        "protocol": {
            "pre_registration": "tasks/2026-09-21-fade-1h-momentum-on-15m.md section 8 step 2",
            "params": str(PARAMS.relative_to(data.REPO)), "params_frozen": prm,
            "params_frozen_lam_ge_0_sensitivity": prm0,
            "tape": "m15.db (15m, both sides of every match), wallets.db family 1h, maker15.db tk (taker side)",
            "windows_utc": [_iso(R["t0"].min()), _iso(R["t0"].max())],
            "decision_minutes": MINUTES.tolist(), "headline_minute": HEADLINE,
            "spot_conventions": prm_file["conventions"],
            "market_price": "Up-equivalent price of the last print (either token, maker or taker row) with "
                            "t - 60 <= ts <= t; none -> row skipped",
            "m_H": "Up-equivalent price of the last print on that hour's market with t - 60 <= ts <= t; none -> "
                   "NaN -> spot momentum only (model.blend); SOL has no hourly tape",
            "mu_H": "model.mu_hat_H(m_H, x_t, sigma, t) with x_t = ln S(t) - ln(open of the hour's first minute)",
            "blend": "section 4 with moments of standardised forecast errors (target: rest-of-hour drift on "
                     "Binance, weights 1 - t, target noise removed via forward realised variance), estimated "
                     "leave-one-day-out over the four UTC days; per row vH, vL, cHL = sigma^2 * moment; v floored at 0",
            "tape_order": "chronological = (ts asc, rowid desc): both tapes were stored newest first",
            "side": "section 6: side j = Up where p >= 1/2, else Down; one entry per market at a decision minute",
            "taker": "decide on the quote known at t: the last taker print that is an Up-equivalent buy of side j "
                     "(BUY of token j or SELL of the other token) with t - 60 <= ts < t; decided when quote < "
                     "a*(p_j). Order: a buy limited at a*(p_j) (section 6: the most we would ever pay). The ask at "
                     "t is the first such print with t <= ts <= t + 30; the order fills there when it is <= a*, "
                     "else a limit miss (no position, counted). A decided row with no such print is kept (whether "
                     "someone else bought in the 30 s after t is not known at t) and filled at its pre-t quote "
                     "(< a* by the decision); quote + mean slippage, the first later taker buy, and dropping them "
                     "are sensitivities on the same decided rows. pnl = win - fill - 0.07 fill (1 - fill). Each "
                     "taker table gives decided / entered / limit-miss / unpriced counts, P&L per decided row "
                     "(limit miss = 0), the same entries paying the pre-t quote (matched n), and entries split by "
                     "price source (post-t print vs no print)",
            "maker": "b* = model.maker_best_bid on [0.01, price_j - 0.01] for the favoured side, p_fill re-evaluated "
                     "at the fill (stretch M), quoted when J(b*) > 0; filled when a print with t < ts < window end "
                     "has price (as token j, Up-equivalent) strictly below b*; pnl per filled share = win - b*, no "
                     "fee; queue position unknown: a print strictly below b* is taken as a fill",
            "baselines": "Zayan's rule (>= 0.40) and the 2026-09-21 finding (>= 0.55): the 1h side as the finding "
                         "defines it (threshold_scan.py), the sign of ln S(window open) - ln S(open - 1 h) on Binance "
                         "(known from the open; at minute 0 equal to mu_L), entered when the pre-t quote of that side "
                         "is >= the threshold, as a market order (no a*) priced like the taker (first post-t print "
                         "within 30 s, else the pre-t quote); same rows. The side from the trailing hour at t "
                         "(sign of mu_L, which at minute m includes the window's own first m minutes) is a "
                         "sensitivity",
            "units": "the market: one entry per market per decision minute (pooled: the earliest minute per "
                     "market); where a sensitivity allows both sides, per_market gives the market-level numbers",
            "log_loss": f"every probability clipped to [{P_CLIP}, {1 - P_CLIP}]",
            "t_statistics": "mean / CR1 standard error clustered by window start (the four coins and the decision "
                            "minutes of one window are one cluster)",
            "intervals": "wilson95: Wilson, rows treated as independent (pre-registered), too narrow because the "
                         "four coins of a window move together; ci95_cluster_window: mean +- 1.96 window-clustered "
                         "standard errors",
            "feed": "the model's y, x, sigma and momentum come from Binance spot; the 15m markets settle on "
                    "Chainlink. vs_binance_direction repeats the headline scores and P&L against Binance's own "
                    "direction; model_minus_martingale isolates the model's terms (both fed Binance). A model fed "
                    "the Chainlink price at t is not in this test.",
            "sign_convention": "logloss_diff and brier_diff are a - b per row: negative = a better",
        },
        "deviations": DEVIATIONS,
        "coverage": cov,
        "blend": blend_report,
        "theta": theta,
        "by_minute": by_minute,
        "pooled_all_minutes": pooled,
        "seconds": round(time.time() - t_start, 1),
        "peak_rss_mb": round(rusage_mb, 1),
    }
    report["multiple_comparisons"] = multiple_comparisons(report)
    Path(args.out).write_text(json.dumps(report, indent=1, default=float))
    print(f"wrote {args.out}\n{report['seconds']}s, peak RSS {report['peak_rss_mb']} MB")


DEVIATIONS = [
    "Blend moments are estimated leave-one-day-out over the tape's four UTC days, not walk-forward: there "
    "is one tape, so earlier days use weights estimated partly on later days.",
    "Forecast-error target and scaling are not specified in section 4; chosen: the rest-of-hour drift on "
    "Binance (what the hourly market settles on), errors standardised by sigma and weighted by 1 - t, "
    "target noise removed with the forward realised variance (only v changes; the weight does not).",
    "Maker fill boundary: section 6 moves the crowd's price with mu_hat_H. Here the crowd drift is the one "
    "that reproduces the observed 15m price at t (implied_drift of the 15m price), so the bid range below "
    "the current price is coherent with the fill model; mu_hat_H does not exist for SOL or missing prints.",
    "Maker: only b* is quoted (no scaled child orders across 5-15c), and only where J(b*) > 0 (maker Kelly > 0); the fill "
    "test uses Up-equivalent prints on both tokens (the book is shared), with prints on token j only and a "
    "bid on the 1c tick (the highest tick at or below b*) as sensitivities.",
    "Maker sensitivities (equal weights, both sides, lam >= 0, anchor held, the first run's rule) are computed at "
    "the headline minute only (runtime); the headline maker at every minute.",
    "Taker quotes and fills include taker SELLs of the other token (Up-equivalent buys of side j); a "
    "direct-BUY-only sensitivity is reported beside.",
    "Market price and m_H use prints with ts == t (the decision second) as the spec's [t - 60, t] says; the "
    "taker quote uses ts < t because the fill window starts at t.",
    "Log-loss clips every probability to [0.001, 0.999] (the market's own prices reach 0.99+).",
    "Theta refit on the tape is a diagnostic, not pre-registered; the frozen theta is what every score uses.",
    "Added diagnostics, not pre-registered: the model's favoured side against the trailing 1h move (with theta "
    "and with theta = 0), and whether the crowd's price leans against the previous 15m candle.",
    # review fixes
    "Review findings 1 and 6 (taker look-ahead): the first run decided AND priced on the first taker print in "
    "[t, t + 30], so which entries were taken depended on price moves after t. The decision now uses only the "
    "last taker print in [t - 60, t) (known at t); the first post-t print is only the fill price. The first "
    "run's rule and a pay-the-quote variant are reported as sensitivities.",
    "Review finding 2 (side): the first run tested both sides against a*/b* independently, buying the side the "
    "model rates below 1/2 and sometimes both sides of one market. Now the side is sign(p - 1/2) as section 6 "
    "says; both sides independently is a sensitivity.",
    "Review finding 3 (p_fill): the first run re-evaluated p at the fill with the anchor held at the quote "
    "(stretch M + B - y), which also pulls back the move from the quote to the fill; section 6 re-evaluates "
    "the model at the fill state, where a new decision uses stretch M. Now 'reset' (M); 'held' is a "
    "sensitivity at the headline minute.",
    "Review finding 4 (lam): the frozen params are the lam-free fit (step 1); the first run's lam >= 0 bound "
    "is a sensitivity (params_lam_ge_0). model.maker_fill no longer returns NaN for lam < 0.",
    "Review finding 5 (units): results are per market (section 8: equal-weighted per market); the pooled "
    "first entry keeps one entry per market (the first run kept one per market and side). Where a sensitivity "
    "quotes both sides, per_market gives per-market totals, per-fill means per market, and one-sided and "
    "two-sided fills apart.",
    "Review finding 8: Wilson intervals (pre-registered) treat rows as independent; window-clustered intervals "
    "are given beside them.",
    "Review finding 9: many t-statistics are computed; quarter and asset tables are exploratory and "
    "multiple_comparisons counts every t and adjusts within each table (Holm, BH). The pre-registered "
    "headline is 'headline'.",
    "Review finding 10: the model is fed Binance, the market settles on Chainlink; vs_binance_direction "
    "reports the same scores and P&L against Binance's own direction and splits the real-target gap by "
    "whether Binance agrees with the resolution. A Chainlink-fed model is not part of this test.",
    "Review finding 11: b* on the search's upper end (last print - 1c, J still rising) is reported apart "
    "from interior b* (J'(b*) = 0).",
    # second review (execution and baselines)
    "Second review, taker population (findings 1, 3, 7): the previous headline kept a decided row only if "
    "another taker bought side j in [t, t + 30], so the set of trades depended on information after t (at "
    "minute 2, 81 of 447 decided rows dropped). Every decided row is now kept; one with no such print fills at "
    "its pre-t quote (the ask as last seen at t). Priced at quote + the rule's mean slippage, at the first later "
    "taker buy, or dropped (the old population) are sensitivities on the same decided rows. Each taker table "
    "states decided / entered / limit-miss / unpriced counts, P&L per decided row, the entries split by price "
    "source, and the same entries paying the pre-t quote (matched n) - the earlier pay-the-quote sensitivity "
    "was read beside the headline over a different population. The pooled first entry uses the same rule.",
    "Second review, taker order (finding 6): section 6 makes a* the most we would ever pay and section 8 buys "
    "where the ask is below a*, but the previous headline was a market order that paid the first post-t print "
    "whatever its price (25% of minute-2 entries above a*). The headline is now a buy limited at a*: decided on "
    "the pre-t quote, filled at the first post-t print only when it is <= a*, limit misses counted apart from "
    "no-print rows. The market order is a labelled sensitivity. A missed order is known by t + 30, before the "
    "next decision minute, so the pooled first entry may enter a market at a later minute.",
    "Second review, baseline side (finding 4): the baselines now take the 1h side as the 2026-09-21 finding "
    "defines it (threshold_scan.py: Binance return from 1 h before the window open to the open), which Zayan's "
    ">= 40c rule shares; the previous code used the trailing hour at t (mu_L), which at minute m includes the "
    "window's own first m minutes (disagrees on 128 of 1,071 minute-2 rows). mu_L's side is a sensitivity. "
    "The hourly market's favoured side (m_H vs 1/2), a third reading, is not tested.",
    "Second review, maker tick (finding 5): 'bid_floored_to_1c_tick' floored b*, which Brent leaves 1.003-1.007c "
    "under the last print, to 2c under it. Now the highest tick at or below b* with b* on the upper end taken as "
    "exactly last print - 1c.",
]


if __name__ == "__main__":
    main()
