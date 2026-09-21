"""Step 1 of the pre-registered test: walk-forward maximum likelihood on Binance spot only.

Pre-registration: tasks/2026-09-21-fade-1h-momentum-on-15m.md, section 8, step 1.
Model: tools/fade_1h_momentum_15m/model.py (sections 1-5), fitted with its analytic-gradient
likelihood ``neg_log_lik``.

Observations. Every 15m window of BTC/ETH/SOL/XRP opening 2026-03-01 00:00 .. 2026-09-20 23:45
UTC, decided at minutes m in {0, 1, 2, 3, 5} of the window (tau = m / 15). Price rule (strict,
no look-ahead): the price at instant T is S(T) = close of the minute that opened at T - 60, the
last trade before T. Known at the decision t = T0 + 60 m:

- y = ln S(t) - ln K, K = the window's strike = open of its first minute (the outcome's
  reference, known for m >= 1). At m = 0 the strike is being set at that instant and is not
  yet known, so y = 0 there.
- x = ln S(t) - ln (open of the hour's first minute); 0 at the hour's first instant. Stored
  only: step 1 has no hourly-market tape, so mu_H (the only user of x) does not exist.
- prev_returns: ln(close / open) of the 12 previous completed 15m candles (column 0 = the
  candle ending at T0), the intra-bar convention of step 0 and the paper.
- sigma^2 per hour = sum of the 60 squared close-to-close 1-minute log returns of S ending at t.
- mu = mu_L = ln S(t) - ln S(t - 1h) (L = 1, per hour); v = sigma^2 / L = sigma^2.
- h = (15 - m) / 60 hours left; hour-time t_hour = ((T0 mod 3600) + 60 m) / 3600.
Outcome: up = 1[close of the window's last minute >= K] (ties Up, as Polymarket).

Models, one parameter set for all four coins, fitted on every row with window opening before
month M and scored on month M (M = 2026-04 .. 2026-09; 2026-09 is 1-20 only):
  (0) martingale Phi(y / (sigma sqrt h)), nothing fitted
  (1) momentum:  theta free, kappa0 = 0
  (2) reversion: kappa0, lam, alpha, c free, theta = 0
  (3) full:      theta, kappa0, lam, alpha, c free
  lam is free in sign, as section 7 fits it ("maximum likelihood"; section 8 asks whether
  lam > 0): model.PARAM_BOUNDS, whose lam >= -10 is a numerical guard only (at_bound says if
  it binds). Sensitivity, scored the same way: (2) and (3) with lam >= 0, the bound the first
  run used (model.PARAM_BOUNDS_LAM_GE_0).
  (0s) diagnostic, not pre-registered: Phi(y / (s sigma sqrt h)) with one fitted scale s. Any
       kappa0 > 0 also shrinks the model's variance V below sigma^2 h, so part of a gain of (2)
       or (3) at m >= 1 can be a recalibration of the trailing volatility rather than reversion.
       (0s) measures how much a pure variance rescale buys; at m = 0 (y = 0) it equals (0).
  (0k) diagnostic, not pre-registered: (0s) with one scale per quarter of the hour k.
  Sensitivity added after the pre-registration (second review): (2) and (3) with the diffusion
  variance of quarter k scaled, V = s_k^2 sigma^2 V1(kappa0, lam) (reversion_vol_by_k,
  full_vol_by_k; model.neg_log_lik_vol_by_quarter). kappa sets both the mean pull and the
  variance shrink, and the trailing sigma's ratio to the coming quarter's volatility differs
  by quarter, so lam can pick up that pattern; lam_identification_sensitivity compares lam,
  its clustered z and the reversion fraction by quarter, as coded and with s_k free, on the
  same rows (walk-forward and pre-Sep-17).
Every comparison is on the same rows; t-statistics are clustered by window start (the four
coins and five decision times of one window are one cluster), with a by-day clustering beside
it as a robustness check.

Then (3) is refit on every window opening before 2026-09-17 00:00 UTC (the Polymarket tape
starts on 2026-09-17 20:45), lam free and lam >= 0, and saved to
data/fade_1h_momentum_15m/params_pre_sep17.json for step 2 ("params" = lam free, the
pre-registered fit; "params_lam_ge_0" = the sensitivity).

    PYTHON_DOTENV_DISABLED=1 python3 tools/fade_1h_momentum_15m/step1_walkforward.py
"""
from __future__ import annotations

import json
import resource
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from scipy.optimize import minimize, minimize_scalar
from scipy.special import log_ndtr, ndtr

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data  # noqa: E402  (sibling module: tools/fade_1h_momentum_15m/data.py)
import model as fm  # noqa: E402  (sibling module: tools/fade_1h_momentum_15m/model.py)
from step0_reversal_auc import auc  # noqa: E402  (Mann-Whitney AUC with midranks)

OUT = data.OUT_DIR / "step1.json"
PARAMS_OUT = data.OUT_DIR / "params_pre_sep17.json"
COINS = ["btc", "eth", "sol", "xrp"]


def _ts(y: int, m: int, d: int = 1) -> int:
    return int(datetime(y, m, d, tzinfo=UTC).timestamp())


START = _ts(2026, 3, 1)  # first window
END = _ts(2026, 9, 21)  # exclusive: last window opens 2026-09-20 23:45
HIST = _ts(2026, 2, 28)  # lookback only
CUTOFF = _ts(2026, 9, 17)  # final fit: windows opening before this
FOLDS = [(2026, m) for m in range(4, 10)]  # test months; each trains on every earlier month
MINUTES = np.array([0, 1, 2, 3, 5])  # decision minutes into the window (tau = m / 15)
NLAG = fm.N_LAGS
LOOKBACK = 15 * NLAG  # minutes before the window open the features reach (12 quarters >= 61)

NQ = fm.N_QUARTERS
NAMES = fm.PARAM_NAMES + tuple(f"ln_s{k}" for k in range(1, NQ + 1))  # ln s_k: vol_by_k models only
SCALE = np.array([1.0, 1.0, 1.0, 1.0, 1e-3] + [1.0] * NQ)  # c optimised in units of 0.1%, as fm.fit_mle
TYPICAL = np.array([0.1, 0.1, 0.1, 0.1, 1e-3] + [0.1] * NQ)  # finite-difference step reference per parameter
LN_S_BOUND = 1.5  # |ln s_k| <= 1.5 (s_k in [0.22, 4.5]): a numerical guard only
LAM_FREE = fm.LAM_GUARD  # lam's lower limit in the pre-registered fits: a numerical guard only
_BASE5 = np.array([0.0, 1.0, 1.0, 1.0, 0.003])
_BASE9 = np.concatenate([_BASE5, np.zeros(NQ)])
MODELS = {
    "momentum": {"free": [0], "fixed": np.array([0.0, 0.0, 0.0, 1.0, 0.003]), "lam_lower": LAM_FREE},
    "reversion": {"free": [1, 2, 3, 4], "fixed": _BASE5, "lam_lower": LAM_FREE},
    "full": {"free": [0, 1, 2, 3, 4], "fixed": _BASE5, "lam_lower": LAM_FREE},
    # sensitivity: the lam >= 0 bound the first run used (not in the pre-registration)
    "reversion_lam_ge_0": {"free": [1, 2, 3, 4], "fixed": _BASE5, "lam_lower": 0.0},
    "full_lam_ge_0": {"free": [0, 1, 2, 3, 4], "fixed": _BASE5, "lam_lower": 0.0},
    # sensitivity added after the pre-registration (review of 2026-09-21): the diffusion variance of
    # quarter k gets its own scale, V = s_k^2 sigma^2 V1(kappa) (model.neg_log_lik_vol_by_quarter), so
    # an intra-hour volatility pattern cannot load on lam through the variance term
    "reversion_vol_by_k": {"free": [1, 2, 3, 4, 5, 6, 7, 8], "fixed": _BASE9, "lam_lower": LAM_FREE},
    "full_vol_by_k": {"free": [0, 1, 2, 3, 4, 5, 6, 7, 8], "fixed": _BASE9, "lam_lower": LAM_FREE},
}
# final (pre-Sep-17) fits only: the in-sample cost of lam >= 0 once the variance has its own scales
FINAL_ONLY = {"reversion_vol_by_k_lam_ge_0": {"free": [1, 2, 3, 4, 5, 6, 7, 8], "fixed": _BASE9, "lam_lower": 0.0}}
_VOL_BASE = {"reversion_vol_by_k": "reversion", "full_vol_by_k": "full",
             "reversion_vol_by_k_lam_ge_0": "reversion_lam_ge_0"}
STARTS = [np.array(s) for s in ((0.0, 1.0, 1.0, 1.0, 0.003), (-0.3, 3.0, 0.2, 0.5, 0.001),
                                (0.3, 0.5, 3.0, 2.0, 0.01))]
START_LAM_NEG = np.array([0.0, 0.5, -1.5, 1.0, 0.008])  # extra start where lam may be negative
OPT = {"maxiter": 2000, "ftol": 1e-14, "gtol": 1e-10, "maxcor": 20}


def _spec(name: str) -> dict:
    return MODELS[name] if name in MODELS else FINAL_ONLY[name]


def _pad(x, dim: int) -> np.ndarray:
    """A start of another model resized to dim parameters (ln s_k = 0 where it has none)."""
    x = np.asarray(x, float)
    return np.concatenate([x, np.zeros(dim - x.size)]) if x.size < dim else x[:dim].copy()


def nll_and_grad(x, obs: dict) -> tuple[float, np.ndarray]:
    """model.neg_log_lik (5 parameters) or model.neg_log_lik_vol_by_quarter (9)."""
    x = np.asarray(x, float)
    return fm.neg_log_lik(x, obs) if x.size == 5 else fm.neg_log_lik_vol_by_quarter(x, obs)


# --------------------------------------------------------------------------- observations


def coin_rows(coin: str) -> tuple[dict, dict]:
    """Decision rows of one coin and the skip report."""
    b = data.load_1m(coin, HIST, END)
    n_min = (END - HIST) // 60
    o = np.full(n_min, np.nan)
    c = np.full(n_min, np.nan)
    k = (b.t - HIST) // 60
    o[k], c[k] = b.o, b.c
    lo, lc = np.log(o), np.log(c)
    miss = np.concatenate([[0], np.cumsum(np.isnan(o) | np.isnan(c))])
    r2 = np.zeros(n_min)
    r2[1:] = (lc[1:] - lc[:-1]) ** 2
    cs = np.concatenate([[0.0], np.cumsum(np.nan_to_num(r2))])  # cs[i] = sum_{j < i} r2[j]

    t0 = START + 900 * np.arange((END - START) // 900, dtype=np.int64)
    i0 = (t0 - HIST) // 60
    ok = miss[i0 + 15] - miss[i0 - LOOKBACK - 1] == 0  # the window and every minute its features use
    n_windows, n_missing = int(t0.size), int((~ok).sum())
    t0, i0 = t0[ok], i0[ok]

    nw, nm = t0.size, MINUTES.size
    it = i0[:, None] + MINUTES[None, :]  # minute index of the decision instant
    s_t = lc[it - 1]  # ln S(t): close of the minute before t
    y = np.where(MINUTES[None, :] > 0, s_t - lo[i0][:, None], 0.0)
    ih = i0 - (t0 % 3600) // 60  # the hour's first minute
    x = np.where(it > ih[:, None], s_t - lo[ih][:, None], 0.0)
    sig2 = cs[it] - cs[it - 60]
    mu_l = s_t - lc[it - 61]
    prev = np.stack([lc[i0 - 15 * (j - 1) - 1] - lo[i0 - 15 * j] for j in range(1, NLAG + 1)], 1)
    up = (c[i0 + 14] >= o[i0]).astype(float)
    bad_sig = sig2 <= 0

    rows = {
        "coin": np.full(nw * nm, COINS.index(coin), dtype=np.int8),
        "t0": np.repeat(t0, nm),
        "minute": np.tile(MINUTES, nw).astype(np.int8),
        "k": np.repeat((t0 % 3600) // 900 + 1, nm).astype(np.int8),
        "up": np.repeat(up, nm),
        "y": y.ravel(), "x": x.ravel(),
        "prev_returns": np.repeat(prev, nm, axis=0),
        "sigma": np.sqrt(np.maximum(sig2, 0.0)).ravel(),
        "mu": mu_l.ravel(), "v": sig2.ravel(),
        "t": (((t0 % 3600)[:, None] + 60 * MINUTES[None, :]) / 3600.0).ravel(),
        "h": np.tile((15 - MINUTES) / 60.0, nw),
    }
    keep = ~bad_sig.ravel()
    rows = {kk: a[keep] for kk, a in rows.items()}
    report = {"windows": n_windows, "skipped_missing_minutes": n_missing,
              "rows_dropped_zero_sigma": int(bad_sig.sum()), "rows": int(keep.sum()),
              "up_rate": round(float(up.mean()), 5), "binance_exact_ties": int((c[i0 + 14] == o[i0]).sum())}
    return rows, report


def load_all() -> tuple[dict, dict]:
    parts, report = [], {}
    for coin in COINS:
        r, rep = coin_rows(coin)
        parts.append(r)
        report[coin] = rep
    rows = {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
    rows["day"] = rows["t0"] // 86400
    return rows, report


def take(rows: dict, sel: np.ndarray) -> dict:
    return {k: a[sel] for k, a in rows.items()}


# --------------------------------------------------------------------------- scoring


def predict(x, obs: dict) -> tuple[np.ndarray, np.ndarray]:
    """(p, per-row log-likelihood) of model.prob_up at full parameter vector x.

    x has 5 parameters, or 9 for the vol_by_k sensitivity (V = s_k^2 sigma^2 V1, s_k = e^x[4+k]).
    OU moments are evaluated on the unique (t, h) pairs (20 here) and mapped back.
    """
    theta, kappa0, lam, alpha, c = (float(v) for v in x[:5])
    m = fm.stretch(obs["prev_returns"], alpha, c)
    th = np.stack([obs["t"], obs["h"]], 1)
    uniq, inv = np.unique(th, axis=0, return_inverse=True)
    a, G, V1 = fm.ou_moments(uniq[:, 0], uniq[:, 1], kappa0, lam, 1.0)
    inv = inv.ravel()
    a, G, V1 = a[inv], G[inv], V1[inv]
    sig2 = obs["sigma"] ** 2
    if len(x) > 5:
        sig2 = sig2 * np.exp(2.0 * np.asarray(x[5:5 + NQ], float))[obs["k"].astype(int) - 1]
    num = obs["y"] - a * m + theta * obs["mu"] * G
    den = np.sqrt(sig2 * V1 + theta * theta * obs["v"] * G * G)
    z = num / den
    return ndtr(z), log_ndtr((2.0 * obs["up"] - 1.0) * z)


def predict_martingale(obs: dict, scale=1.0) -> tuple[np.ndarray, np.ndarray]:
    """Phi(y / (s sigma sqrt h)); scale is one number, or one per row (the by-quarter diagnostic)."""
    z = obs["y"] / (scale * obs["sigma"] * np.sqrt(obs["h"]))
    return ndtr(z), log_ndtr((2.0 * obs["up"] - 1.0) * z)


def cluster_t(d: np.ndarray, g: np.ndarray) -> float | None:
    """Mean of d over its standard error clustered by g (CR1)."""
    n = d.size
    _, inv = np.unique(g, return_inverse=True)
    s = np.bincount(inv.ravel(), weights=d - d.mean())
    ng = s.size
    if ng < 2:
        return None
    var = ng / (ng - 1) * float((s * s).sum()) / (n * n)
    return float(d.mean() / np.sqrt(var)) if var > 0 else None


def score(p: np.ndarray, ll: np.ndarray, up: np.ndarray) -> dict:
    return {"logloss": float(-ll.mean()), "brier": float(np.mean((p - up) ** 2))}


def diff(loss_a: np.ndarray, loss_b: np.ndarray, obs: dict) -> dict:
    """Paired a - b per row (negative = a better), n, t clustered by window and by day."""
    d = loss_a - loss_b
    return {"mean": float(d.mean()), "n": int(d.size), "n_windows": int(np.unique(obs["t0"]).size),
            "t_window": _r(cluster_t(d, obs["t0"])), "t_day": _r(cluster_t(d, obs["day"]))}


def _r(v, nd: int = 2):
    return None if v is None else round(float(v), nd)


# --------------------------------------------------------------------------- fitting


def param_bounds(lam_lower: float = LAM_FREE) -> list:
    """model.PARAM_BOUNDS with lam's lower limit set: LAM_FREE (numerical guard) or 0 (sensitivity),
    then |ln s_k| <= LN_S_BOUND for the vol_by_k sensitivity (numerical guard)."""
    b = list(fm.PARAM_BOUNDS)
    b[2] = (lam_lower, b[2][1])
    return b + [(-LN_S_BOUND, LN_S_BOUND)] * NQ


def _bounds(free, lam_lower: float = LAM_FREE):
    pb = param_bounds(lam_lower)
    return [(None if pb[j][0] is None else pb[j][0] / SCALE[j], None if pb[j][1] is None else pb[j][1] / SCALE[j])
            for j in free]


def fit(obs: dict, name: str, extra_starts=(), starts=None) -> dict:
    """Maximum likelihood of one model over its free parameters, best of several starts.

    starts (optional) replaces the default start list; extra_starts are added to it either way."""
    spec = _spec(name)
    lam_lower = spec["lam_lower"]
    free = np.array(spec["free"])
    dim = spec["fixed"].size
    n = obs["up"].size
    if starts is None:
        starts = [s.copy() for s in STARTS]
        if 2 in spec["free"] and lam_lower < 0:
            extra_starts = list(extra_starts) + [START_LAM_NEG.copy()]
    starts = [_pad(s, dim) for s in starts] + [_pad(s, dim) for s in extra_starts if s is not None]
    if name == "momentum":
        starts = [np.array([0.0, 0, 0, 1, 0.003])] + [np.array([s[0], 0, 0, 1, 0.003]) for s in extra_starts
                                                      if s is not None]
    best, runs = None, []
    for s in starts:
        base = spec["fixed"].copy()
        base[free] = s[free]
        if lam_lower >= 0 and base[2] < 0:  # a warm start from a lam-free fit, moved inside the bound
            base[2] = 0.0

        def f(z, base=base):
            xx = base.copy()
            xx[free] = z * SCALE[free]
            nll, g = nll_and_grad(xx, obs)
            return nll / n, g[free] * SCALE[free] / n

        res = minimize(f, base[free] / SCALE[free], jac=True, method="L-BFGS-B",
                       bounds=_bounds(free, lam_lower), options=OPT)
        xx = base.copy()
        xx[free] = res.x * SCALE[free]
        runs.append({"start": [float(v) for v in s[free]], "nll_per_row": float(res.fun),
                     "x": [float(v) for v in xx[free]], "success": bool(res.success), "nit": int(res.nit)})
        if best is None or res.fun < best[0]:
            best = (float(res.fun), xx, res)
    nll, xx, res = best
    spread = max(r["nll_per_row"] for r in runs) - nll
    grad = nll_and_grad(xx, obs)[1] / n  # at a bound or on a fixed parameter: the way the data pull it
    return {"x": xx, "free": [NAMES[j] for j in free], "lam_lower_bound": lam_lower,
            "nll_per_row": nll, "n": int(n),
            "grad_nll_per_row_at_fit": {k: float(v) for k, v in zip(NAMES, grad)},
            "success": bool(res.success), "message": str(res.message), "nit": int(res.nit),
            "n_starts": len(runs), "worst_start_minus_best_nll_per_row": spread, "runs": runs}


def fit_scale(obs: dict, sel=None) -> float:
    """(0s): scale s of the martingale by maximum likelihood (rows with y != 0 carry it)."""
    sel = obs["minute"] > 0 if sel is None else sel & (obs["minute"] > 0)
    y, sig, h, up = obs["y"][sel], obs["sigma"][sel], obs["h"][sel], obs["up"][sel]
    zb = (2 * up - 1) * y / (sig * np.sqrt(h))
    res = minimize_scalar(lambda ls: -float(log_ndtr(zb * np.exp(-ls)).sum()), bounds=(-2, 2),
                          method="bounded", options={"xatol": 1e-9})
    return float(np.exp(res.x))


def fit_scale_by_k(obs: dict) -> np.ndarray:
    """(0k): one martingale scale per quarter of the hour k = 1..4 (the by-quarter version of (0s))."""
    return np.array([fit_scale(obs, obs["k"] == k) for k in range(1, NQ + 1)])


def robust_se(x, obs: dict, name: str) -> dict:
    """Cluster-robust (sandwich) standard errors of the free parameters at the fit x.

    Hessian by central differences of the analytic gradient; per-row scores by differences of the
    per-row log-likelihood. Parameters on a bound are left out (their SE is not defined there).
    """
    free = _spec(name)["free"]
    lam_lower = _spec(name)["lam_lower"]
    lower = [b[0] for b in param_bounds(lam_lower)]
    upper = [b[1] for b in param_bounds(lam_lower)]
    at_bound = {NAMES[j]: bool((lower[j] is not None and x[j] - lower[j] < 1e-6 * max(TYPICAL[j], abs(x[j]) + TYPICAL[j]))
                               or (upper[j] is not None and upper[j] - x[j] < 1e-6))
                for j in free}
    inner = [j for j in free if not at_bound[NAMES[j]]]
    if not inner:
        return {"at_bound": at_bound}
    eps = {j: 1e-4 * max(abs(x[j]), TYPICAL[j]) for j in inner}
    H = np.empty((len(inner), len(inner)))
    S = np.empty((obs["up"].size, len(inner)))
    for a, j in enumerate(inner):
        hi, lo_ = x.copy(), x.copy()
        hi[j] += eps[j]
        lo_[j] -= eps[j]
        gh = nll_and_grad(hi, obs)[1]
        gl = nll_and_grad(lo_, obs)[1]
        H[a] = (gh - gl)[inner] / (2 * eps[j])
        S[:, a] = (predict(hi, obs)[1] - predict(lo_, obs)[1]) / (2 * eps[j])
    H = 0.5 * (H + H.T)
    Hi = np.linalg.pinv(H)
    out = {"at_bound": at_bound, "hessian_cond": float(np.linalg.cond(H))}
    for label, g in (("window", obs["t0"]), ("day", obs["day"])):
        _, inv = np.unique(g, return_inverse=True)
        Sg = np.stack([np.bincount(inv.ravel(), weights=S[:, a]) for a in range(len(inner))], 1)
        ng = Sg.shape[0]
        B = ng / (ng - 1) * Sg.T @ Sg
        cov = Hi @ B @ Hi
        se = np.sqrt(np.maximum(np.diag(cov), 0.0))
        out[f"se_cluster_{label}"] = {NAMES[j]: float(se[a]) for a, j in enumerate(inner)}
        out[f"z_cluster_{label}"] = {NAMES[j]: _r(x[j] / se[a]) if se[a] > 0 else None
                                     for a, j in enumerate(inner)}
    return out


def params_dict(x) -> dict:
    return {k: float(v) for k, v in zip(NAMES, x)}


def hour_profile(x) -> list[dict]:
    """What the fitted process implies at each (quarter k, minute m) decision point.

    For a vol_by_k fit (9 parameters) the sd ratio includes s_k, and s_k is given beside it."""
    theta, kappa0, lam, alpha, c = x[:5]
    out = []
    for k in range(1, 5):
        s_k = float(np.exp(x[4 + k])) if len(x) > 5 else 1.0
        for m in MINUTES:
            t, h = (k - 1) / 4 + m / 60, (15 - m) / 60
            a, G, V1 = fm.ou_moments(t, h, kappa0, lam, 1.0)
            cell = {"k": k, "minute": int(m), "t_hour": round(t, 5), "h": round(h, 5),
                    "reversion_fraction_1_minus_e^-K": float(a), "G_over_h": float(G / h),
                    "sd_ratio_sqrtV_over_sigma_sqrt_h": float(s_k * np.sqrt(V1 / h))}
            if len(x) > 5:
                cell["vol_scale_s_k"] = s_k
            out.append(cell)
    return out


# --------------------------------------------------------------------------- reporting


LOSS_MODELS = ["martingale", "martingale_scaled", "martingale_scaled_by_k", "momentum", "reversion", "full",
               "reversion_lam_ge_0", "full_lam_ge_0", "reversion_vol_by_k", "full_vol_by_k"]
COMPARE = [("momentum", "martingale"), ("reversion", "martingale"), ("full", "martingale"),
           ("martingale_scaled", "martingale"), ("full", "reversion"), ("full", "momentum"),
           ("reversion", "martingale_scaled"), ("full", "martingale_scaled"),
           # lam >= 0 sensitivity, and the effect of the bound on the same rows
           ("full_lam_ge_0", "martingale"), ("reversion_lam_ge_0", "martingale"),
           ("full", "full_lam_ge_0"), ("reversion", "reversion_lam_ge_0"),
           # second review: one variance scale per quarter (diagnostic (0k), sensitivity vol_by_k)
           ("martingale_scaled_by_k", "martingale"), ("martingale_scaled_by_k", "martingale_scaled"),
           ("reversion", "martingale_scaled_by_k"), ("full", "martingale_scaled_by_k"),
           ("reversion_vol_by_k", "martingale_scaled_by_k"), ("full_vol_by_k", "martingale_scaled_by_k"),
           ("reversion_vol_by_k", "reversion"), ("full_vol_by_k", "full"),
           ("reversion_vol_by_k", "martingale"), ("full_vol_by_k", "martingale")]


def evaluate(obs: dict, P: dict, LL: dict) -> dict:
    """Scores and paired differences on one set of rows (every model on the same rows)."""
    up = obs["up"]
    out = {"n": int(up.size), "n_windows": int(np.unique(obs["t0"]).size), "up_rate": float(up.mean()),
           "scores": {m: score(P[m], LL[m], up) for m in LOSS_MODELS}, "logloss_diff": {}, "brier_diff": {}}
    for a, b in COMPARE:
        out["logloss_diff"][f"{a}_minus_{b}"] = diff(-LL[a], -LL[b], obs)
        out["brier_diff"][f"{a}_minus_{b}"] = diff((P[a] - up) ** 2, (P[b] - up) ** 2, obs)
    return out


def breakdown(obs: dict, P: dict, LL: dict, key: str, values) -> list[dict]:
    rows = []
    for v in values:
        sel = obs[key] == v
        o = take(obs, sel)
        e = evaluate(o, {m: P[m][sel] for m in P}, {m: LL[m][sel] for m in LL})
        rows.append({key: int(v), **e})
    return rows


def breakdown_grid(obs: dict, P: dict, LL: dict) -> list[dict]:
    rows = []
    for k in range(1, 5):
        for m in MINUTES:
            sel = (obs["k"] == k) & (obs["minute"] == m)
            o = take(obs, sel)
            up = o["up"]
            cell = {"k": k, "minute": int(m), "n": int(sel.sum())}
            for a, b in (("full", "martingale"), ("reversion", "martingale"), ("momentum", "martingale")):
                cell[f"{a}_minus_{b}"] = diff(-LL[a][sel], -LL[b][sel], o)
                cell[f"{a}_minus_{b}_brier"] = diff((P[a][sel] - up) ** 2, (P[b][sel] - up) ** 2, o)
            rows.append(cell)
    return rows


def sign_reversal_by_k(obs: dict) -> list[dict]:
    """Model-free check (not pre-registered): -sign(previous 15m candle) against the outcome, minute-0
    rows, by quarter of the hour. No parameters, so it reads the same on any rows."""
    o = take(obs, obs["minute"] == 0)
    score = -np.sign(o["prev_returns"][:, 0])
    out = []
    for k in (0, 1, 2, 3, 4):
        sel = np.ones(o["up"].size, bool) if k == 0 else o["k"] == k
        sc, y, t0 = score[sel], o["up"][sel], o["t0"][sel]
        nz = sc != 0
        hit = ((sc[nz] > 0) == (y[nz] > 0)).astype(float)
        out.append({"k": "all" if k == 0 else k, "n": int(sel.sum()), "sign_auc": round(auc(sc, y), 5),
                    "hit_rate_prev_nonflat": round(float(hit.mean()), 5), "n_prev_nonflat": int(nz.sum()),
                    "t_window": _r(cluster_t(hit - 0.5, t0[nz])),
                    "sign_auc_by_coin": {c: round(auc(sc[o["coin"][sel] == i], y[o["coin"][sel] == i]), 5)
                                         for i, c in enumerate(COINS)}})
    return out


def _iso(ts) -> str:
    return data._iso(int(ts))


# Extra warm starts: a full model also starts from its reversion-only fit (same lam bound).
_REVERSION_WARM = {"full": ("reversion",), "full_lam_ge_0": ("reversion_lam_ge_0",)}


def vol_starts(name: str, base_x, own_x, scales_k) -> list:
    """Starts of a vol_by_k model: its as-coded fit on the same rows with s_k = 1 and with s_k = the
    martingale's by-quarter scales, its own previous fit, and two generic starts (lam > 0, lam < 0)."""
    out = []
    if base_x is not None:
        out += [_pad(base_x, 9), np.concatenate([_pad(base_x, 5), np.log(scales_k)])]
    if own_x is not None:
        out.append(np.asarray(own_x, float))
    out.append(_pad(STARTS[0], 9))
    if _spec(name)["lam_lower"] < 0:
        out.append(_pad(START_LAM_NEG, 9))
    return out


def final_fit(opre: dict, name: str, warm: dict, starts=None) -> dict:
    """A model refit on every window before the tape: (3) lam free, the params step 2 uses; the rest
    are sensitivities (lam >= 0, reversion only, one variance scale per quarter)."""
    t1 = time.time()
    if starts is None:
        ff = fit(opre, name, extra_starts=[warm.get(name)] + [warm[r] for r in _REVERSION_WARM.get(name, ())])
    else:
        ff = fit(opre, name, starts=starts)
    spec = _spec(name)
    return {
        "model": f"{name}: {', '.join(NAMES[j] for j in spec['free'])} free; lam >= {spec['lam_lower']}"
                 + (" (numerical guard only)" if spec["lam_lower"] < 0 else " (sensitivity bound)"),
        "params": params_dict(ff["x"]), "robust": robust_se(ff["x"], opre, name),
        **{k: v for k, v in ff.items() if k != "x"},
        "train": {"first_window_utc": _iso(opre["t0"].min()), "last_window_utc": _iso(opre["t0"].max()),
                  "n_rows": int(opre["up"].size), "n_windows": int(np.unique(opre["t0"]).size),
                  "rule": "window open < 2026-09-17 00:00 UTC (last window ends 2026-09-17 00:00; "
                          "the Polymarket 15m tape starts 2026-09-17 20:45)"},
        "hour_profile": hour_profile(ff["x"]),
        "seconds": round(time.time() - t1, 1),
    }


def _lam_cell(f: dict) -> dict:
    rb = f.get("robust", {})
    out = {"lam": f["params"]["lam"], "se_cluster_window": rb.get("se_cluster_window", {}).get("lam"),
           "z_cluster_window": rb.get("z_cluster_window", {}).get("lam"),
           "z_cluster_day": rb.get("z_cluster_day", {}).get("lam"),
           "lam_at_bound": rb.get("at_bound", {}).get("lam")}
    if "theta" in rb.get("z_cluster_window", {}):  # theta free (full models): the other step 1 question
        out.update({"theta": f["params"]["theta"], "theta_se_cluster_window": rb["se_cluster_window"]["theta"],
                    "theta_z_cluster_window": rb["z_cluster_window"]["theta"]})
    return out


def _minute0_profile(f: dict) -> dict:
    hp = [r for r in f["hour_profile"] if r["minute"] == 0]
    out = {"reversion_fraction_minute0_k1_to_k4": [round(r["reversion_fraction_1_minus_e^-K"], 4) for r in hp],
           "sd_ratio_minute0_k1_to_k4": [round(r["sd_ratio_sqrtV_over_sigma_sqrt_h"], 4) for r in hp]}
    if "vol_scale_s_k" in hp[0]:
        out["vol_scale_s_k1_to_k4"] = [round(r["vol_scale_s_k"], 4) for r in hp]
    return out


def lam_identification(folds: list, final: dict, sens: dict, sk_pre: np.ndarray, pooled: dict) -> dict:
    """Second review: is lam < 0 (reversion growing through the hour) the mean, or an intra-hour volatility
    pattern read through the variance term? kappa sets both the mean pull and V < sigma^2 h, and sigma is the
    trailing hour's realised vol, whose ratio to the coming quarter's differs by quarter. The vol_by_k models
    give each quarter its own variance scale; lam is compared on the same rows."""
    pairs = (("reversion", "reversion_vol_by_k"), ("full", "full_vol_by_k"))
    per_fold = []
    for fo in folds:
        cell = {"test_month": fo["test_month"], "n_train_rows": fo["train"]["n_rows"]}
        for a, b in pairs:
            cell[a], cell[b] = _lam_cell(fo["fits"][a]), _lam_cell(fo["fits"][b])
        per_fold.append(cell)
    fits = {"full": final, **sens}
    pre = {k: {**_lam_cell(v), **_minute0_profile(v), "nll_per_row": v["nll_per_row"], "n": v["n"]}
           for k, v in fits.items()}
    cost = {"as_coded_reversion": sens["reversion_lam_ge_0"]["nll_per_row"] - sens["reversion"]["nll_per_row"],
            "vol_by_k_reversion": sens["reversion_vol_by_k_lam_ge_0"]["nll_per_row"]
            - sens["reversion_vol_by_k"]["nll_per_row"]}
    cost["share_of_as_coded_cost_left_with_vol_by_k"] = (cost["vol_by_k_reversion"] / cost["as_coded_reversion"]
                                                        if cost["as_coded_reversion"] > 0 else None)
    d = pooled["logloss_diff"]
    oos = {k: d[k] for k in ("reversion_vol_by_k_minus_reversion", "full_vol_by_k_minus_full",
                             "reversion_minus_martingale_scaled_by_k", "reversion_vol_by_k_minus_martingale_scaled_by_k",
                             "full_vol_by_k_minus_martingale_scaled_by_k", "martingale_scaled_by_k_minus_martingale_scaled")}
    rc, rk = pre["reversion"], pre["reversion_vol_by_k"]
    fc, fk = pre["full"], pre["full_vol_by_k"]
    n_neg = {m: sum(1 for c in per_fold if c[m]["lam"] < 0) for m in ("reversion", "reversion_vol_by_k", "full", "full_vol_by_k")}
    th_neg = {m: sum(1 for c in per_fold if c[m]["theta"] < 0) for m in ("full", "full_vol_by_k")}
    def verdict(label: str, cell: dict) -> str:
        z = cell["z_cluster_window"]
        if z is None:
            return f"{label}: lam sits on its numerical guard, so no z is defined there."
        if abs(z) >= Z95_1S:
            return f"{label}: lam is still more than 1.96 clustered standard errors from 0."
        return (f"{label}: lam is within 1.96 clustered standard errors of 0, so lam < 0 ('reversion grows "
                f"through the hour') cannot be told apart from the intra-hour volatility pattern that the "
                f"document's single trailing sigma leaves in the variance term.")

    reading = (
        f"Pre-Sep-17 rows (n {rk['n']:,}), same rows for every fit. Reversion only: lam {rc['lam']:.3f} (z "
        f"{rc['z_cluster_window']}) as coded, {rk['lam']:.3f} (z {rk['z_cluster_window']}) with one variance scale "
        f"per quarter. Full model: {fc['lam']:.3f} (z {fc['z_cluster_window']}) as coded, {fk['lam']:.3f} (z "
        f"{fk['z_cluster_window']}). The in-sample cost of forcing lam >= 0 falls from {cost['as_coded_reversion']:.2e} "
        f"to {cost['vol_by_k_reversion']:.2e} nll per row. Minute-0 reversion fraction k1..k4: as coded "
        f"{rc['reversion_fraction_minute0_k1_to_k4']}, per-quarter scales {rk['reversion_fraction_minute0_k1_to_k4']}. "
        f"lam < 0 in {n_neg['reversion']} / {len(per_fold)} folds as coded and {n_neg['reversion_vol_by_k']} / "
        f"{len(per_fold)} with per-quarter scales (reversion); {n_neg['full']} and {n_neg['full_vol_by_k']} (full). "
        f"theta in the full model (the fade/follow question): {fc['theta']:.4f} (z {fc['theta_z_cluster_window']}) "
        f"as coded, {fk['theta']:.4f} (z {fk['theta_z_cluster_window']}) with per-quarter scales; theta < 0 in "
        f"{th_neg['full']} / {len(per_fold)} folds as coded and {th_neg['full_vol_by_k']} / {len(per_fold)} with them. "
        + "With one variance scale per quarter: " + verdict("reversion only", rk) + " " + verdict("Full model", fk))
    return {"what": lam_identification.__doc__.split("\n\n")[0].replace("\n    ", " "),
            "martingale_scale_by_k_pre_sep17": [round(float(v), 4) for v in sk_pre],
            "pre_sep17_fits": pre, "in_sample_cost_of_lam_ge_0_per_row": cost,
            "per_fold": per_fold, "lam_negative_folds": n_neg, "theta_negative_folds": th_neg,
            "pooled_oos_logloss_diff": oos,
            "reading": reading}


Z95_1S = 1.959963984540054


def main() -> None:
    t_start = time.time()
    rows, skip = load_all()
    n_all = rows["up"].size
    print(f"rows {n_all:,} ({time.time() - t_start:.1f}s); skipped windows (missing minutes): "
          f"{ {c: skip[c]['skipped_missing_minutes'] for c in COINS} }", flush=True)

    P = {m: np.full(n_all, np.nan) for m in LOSS_MODELS}
    LL = {m: np.full(n_all, np.nan) for m in LOSS_MODELS}
    tested = np.zeros(n_all, bool)
    folds, warm = [], {m: None for m in MODELS}
    for (yy, mm) in FOLDS:
        lo_ts = _ts(yy, mm)
        hi_ts = _ts(yy, mm + 1) if mm < 12 else _ts(yy + 1, 1)
        tr = rows["t0"] < lo_ts
        te = (rows["t0"] >= lo_ts) & (rows["t0"] < hi_ts)
        otr, ote = take(rows, tr), take(rows, te)
        fold = {"test_month": f"{yy}-{mm:02d}",
                "train": {"first_window_utc": _iso(otr["t0"].min()), "last_window_utc": _iso(otr["t0"].max()),
                          "n_rows": int(tr.sum()), "n_windows": int(np.unique(otr["t0"]).size)},
                "test": {"first_window_utc": _iso(ote["t0"].min()), "last_window_utc": _iso(ote["t0"].max()),
                         "n_rows": int(te.sum()), "n_windows": int(np.unique(ote["t0"]).size)},
                "fits": {}}
        idx = np.flatnonzero(te)
        p0, l0 = predict_martingale(ote)
        P["martingale"][idx], LL["martingale"][idx] = p0, l0
        s = fit_scale(otr)
        ps, ls = predict_martingale(ote, s)
        P["martingale_scaled"][idx], LL["martingale_scaled"][idx] = ps, ls
        fold["fits"]["martingale_scaled"] = {"scale": s}
        sk = fit_scale_by_k(otr)
        ps, ls = predict_martingale(ote, sk[ote["k"].astype(int) - 1])
        P["martingale_scaled_by_k"][idx], LL["martingale_scaled_by_k"][idx] = ps, ls
        fold["fits"]["martingale_scaled_by_k"] = {"scale_by_k": {int(k): float(v) for k, v in zip(range(1, NQ + 1), sk)}}
        for name in MODELS:
            t1 = time.time()
            if name in _VOL_BASE:  # its as-coded model was fitted earlier in this loop (MODELS order)
                f = fit(otr, name, starts=vol_starts(name, warm[_VOL_BASE[name]], warm[name], sk))
            else:
                f = fit(otr, name, extra_starts=[warm[name]] + [warm[r] for r in _REVERSION_WARM.get(name, ())])
            warm[name] = f["x"]
            p, ll = predict(f["x"], ote)
            P[name][idx], LL[name][idx] = p, ll
            se = robust_se(f["x"], otr, name)
            fold["fits"][name] = {"params": params_dict(f["x"]), **{k: v for k, v in f.items() if k != "x"},
                                  "robust": se, "seconds": round(time.time() - t1, 1)}
            print(f"  {yy}-{mm:02d} {name:18} {params_dict(f['x'])}  nll/row {f['nll_per_row']:.7f}  "
                  f"starts spread {f['worst_start_minus_best_nll_per_row']:.2e}  "
                  f"({time.time() - t1:.0f}s)", flush=True)
        fold["lam_free_minus_lam_ge_0_in_sample_nll_per_row"] = {
            "full": fold["fits"]["full"]["nll_per_row"] - fold["fits"]["full_lam_ge_0"]["nll_per_row"],
            "reversion": fold["fits"]["reversion"]["nll_per_row"] - fold["fits"]["reversion_lam_ge_0"]["nll_per_row"]}
        fold["vol_by_k_minus_as_coded_in_sample_nll_per_row"] = {
            "full": fold["fits"]["full_vol_by_k"]["nll_per_row"] - fold["fits"]["full"]["nll_per_row"],
            "reversion": fold["fits"]["reversion_vol_by_k"]["nll_per_row"] - fold["fits"]["reversion"]["nll_per_row"]}
        tested |= te
        fold["oos"] = evaluate(ote, {m: P[m][idx] for m in P}, {m: LL[m][idx] for m in LL})
        d = fold["oos"]["logloss_diff"]
        print(f"  {yy}-{mm:02d} OOS n={fold['oos']['n']:,}  dLL vs martingale: momentum {d['momentum_minus_martingale']['mean']:+.2e} "
              f"(t {d['momentum_minus_martingale']['t_window']}), reversion {d['reversion_minus_martingale']['mean']:+.2e} "
              f"(t {d['reversion_minus_martingale']['t_window']}), full {d['full_minus_martingale']['mean']:+.2e} "
              f"(t {d['full_minus_martingale']['t_window']})", flush=True)
        folds.append(fold)

    oos = take(rows, tested)
    Po = {m: P[m][tested] for m in P}
    LLo = {m: LL[m][tested] for m in LL}
    pooled = evaluate(oos, Po, LLo)
    by_k = breakdown(oos, Po, LLo, "k", range(1, 5))
    by_minute = breakdown(oos, Po, LLo, "minute", MINUTES)
    by_coin = breakdown(oos, Po, LLo, "coin", range(4))
    for r in by_coin:
        r["coin"] = COINS[r["coin"]]
    grid = breakdown_grid(oos, Po, LLo)

    # Final fit for step 2: model (3) on every window opening before 2026-09-17 00:00 UTC.
    pre = rows["t0"] < CUTOFF
    opre = take(rows, pre)
    final = final_fit(opre, "full", warm)
    final_ge0 = final_fit(opre, "full_lam_ge_0", warm)
    final_ge0["in_sample_nll_per_row_minus_lam_free"] = final_ge0["nll_per_row"] - final["nll_per_row"]
    print(f"final fit pre-Sep17, lam free: {final['params']}  ({final['seconds']}s)", flush=True)
    print(f"final fit pre-Sep17, lam >= 0: {final_ge0['params']}  ({final_ge0['seconds']}s)", flush=True)

    # Sensitivity (second review): lam with one diffusion-variance scale per quarter, same rows.
    sk_pre = fit_scale_by_k(opre)
    fx = {"full": np.array([final["params"][k] for k in fm.PARAM_NAMES])}
    sens = {}
    for name, base, own in (("reversion", None, None), ("reversion_lam_ge_0", None, None),
                            ("reversion_vol_by_k", "reversion", "reversion_vol_by_k"),
                            ("reversion_vol_by_k_lam_ge_0", "reversion_lam_ge_0", "reversion_vol_by_k"),
                            ("full_vol_by_k", "full", "full_vol_by_k")):
        if base is None:
            sens[name] = final_fit(opre, name, warm)
        else:
            own_x = fx.get(own, warm.get(own))
            sens[name] = final_fit(opre, name, warm, starts=vol_starts(name, fx[base], own_x, sk_pre))
        fx[name] = np.array([sens[name]["params"][k] for k in NAMES[:len(_spec(name)["fixed"])]])
        print(f"final fit pre-Sep17, {name}: {sens[name]['params']}  ({sens[name]['seconds']}s)", flush=True)
    lam_ident = lam_identification(folds, final, sens, sk_pre, pooled)

    conventions = {
        "price_at_T": "close of the 1m candle that opened at T - 60 (last trade before T); strict no look-ahead",
        "strike_K": "open of the window's first 1m candle (outcome reference); known for minute >= 1",
        "y": "ln S(t) - ln K for minute >= 1; 0 at minute 0 (strike not known before it is set)",
        "x": "ln S(t) - ln(open of the hour's first 1m candle); 0 at the hour's first instant; unused in step 1",
        "prev_returns": "ln(close/open) of the 12 previous completed 15m candles, column 0 = candle ending at "
                        "the window open",
        "sigma2_per_hour": "sum of the 60 squared close-to-close 1m log returns of S ending at t",
        "mu": "mu_L = ln S(t) - ln S(t - 3600), per hour (L = 1)",
        "v": "sigma^2 / L = sigma^2 (no hourly-market tape for these months, so no blend)",
        "h": "(15 - minute) / 60 hours", "t_hour": "((window open mod 3600) + 60 minute) / 3600",
        "outcome": "close of the window's last 1m candle >= K (ties Up)",
        "decision_minutes": MINUTES.tolist(),
        "one_parameter_set_for_all_four_coins": True,
    }
    PARAMS_OUT.write_text(json.dumps({
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),
        "source": "tools/fade_1h_momentum_15m/step1_walkforward.py",
        "use_in_step2": "model.prob_up(y, stretch(prev_returns, alpha, c), mu, v, t, h, sigma, theta, kappa0, lam). "
                        "Fitted with mu = mu_L and v = sigma^2; step 2's blended mu, v come from the hourly tape. "
                        "'params' is the pre-registered fit (lam free in sign); 'params_lam_ge_0' is the lam >= 0 "
                        "sensitivity.",
        **final, "params_lam_ge_0": final_ge0["params"],
        "lam_ge_0_sensitivity": {k: v for k, v in final_ge0.items() if k not in ("params", "runs")},
        "params_full_vol_by_k_sensitivity_not_used_in_step2": sens["full_vol_by_k"]["params"],
        "conventions": conventions}, indent=1, default=float))

    rusage_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20  # macOS: bytes
    report = {
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),
        "protocol": {
            "pre_registration": "tasks/2026-09-21-fade-1h-momentum-on-15m.md section 8 step 1",
            "data": "Binance spot 1m (data/fade_1h_momentum_15m/spot1m.db), windows 2026-03-01 00:00 .. "
                    "2026-09-20 23:45 UTC",
            "conventions": conventions,
            "folds": "expanding window: train on every window opening before month M, test on month M; "
                     "M = 2026-04 .. 2026-09 (September = 1-20)",
            "models": {"martingale": "(0) Phi(y / (sigma sqrt h))", "momentum": "(1) theta free, kappa0 = 0",
                       "reversion": f"(2) kappa0, lam, alpha, c free, theta = 0; lam free in sign (lam >= {LAM_FREE} "
                                    "numerical guard only)",
                       "full": f"(3) theta, kappa0, lam, alpha, c free; lam free in sign (lam >= {LAM_FREE} "
                               "numerical guard only)",
                       "reversion_lam_ge_0": "sensitivity: (2) with lam >= 0 (the bound of the first run)",
                       "full_lam_ge_0": "sensitivity: (3) with lam >= 0 (the bound of the first run)",
                       "martingale_scaled": "(0s) diagnostic, not pre-registered: Phi(y / (s sigma sqrt h)), s fitted",
                       "martingale_scaled_by_k": "(0k) diagnostic, not pre-registered: (0s) with one scale s_k per "
                                                 "quarter of the hour (added after the second review: a single scale "
                                                 "cannot see a volatility pattern by quarter)",
                       "reversion_vol_by_k": "sensitivity added after the pre-registration: (2) with the diffusion "
                                             "variance of quarter k scaled, V = s_k^2 sigma^2 V1(kappa0, lam) "
                                             "(model.neg_log_lik_vol_by_quarter), so an intra-hour volatility "
                                             "pattern cannot load on lam through the variance term",
                       "full_vol_by_k": "sensitivity added after the pre-registration: (3) with V = s_k^2 sigma^2 V1",
                       "reversion_vol_by_k_lam_ge_0": "final (pre-Sep-17) fit only: reversion_vol_by_k with lam >= 0, "
                                                      "for the in-sample cost of the bound"},
            "optimiser": {"method": "L-BFGS-B on model.neg_log_lik (analytic gradient), c in units of 1e-3",
                          "options": OPT, "starts": [s.tolist() for s in STARTS],
                          "plus": "the previous fold's solution (full also starts from reversion's); vol_by_k "
                                  "models start from their as-coded fit on the same rows (s_k = 1 and s_k = the "
                                  "martingale's by-quarter scales), their own previous fit, and two generic starts"},
            "t_statistics": "paired per-row difference, mean / CR1 standard error clustered by window start "
                            "(t_window, pre-registered); clustered by UTC day (t_day, robustness)",
            "robust_se": "sandwich H^-1 B H^-1, clusters by window start and by day; parameters on a bound left out",
            "sign_convention": "logloss_diff and brier_diff are a - b per row: negative = a better",
        },
        "skipped": skip,
        "n_rows": int(n_all),
        "folds": folds,
        "pooled_oos": {"months": "2026-04 .. 2026-09-20", **pooled},
        "pooled_oos_by_quarter_of_hour_k": by_k,
        "pooled_oos_by_decision_minute": by_minute,
        "pooled_oos_by_coin": by_coin,
        "pooled_oos_by_k_and_minute": grid,
        "model_free_sign_reversal_by_k_diagnostic": {
            "what": "not pre-registered: score -sign(previous 15m candle), minute-0 rows, outcome up (ties Up); "
                    "hit rate over rows whose previous candle is not flat, t clustered by window start",
            "all_windows_2026-03-01_to_09-20": sign_reversal_by_k(rows),
            "oos_rows_2026-04-01_to_09-20": sign_reversal_by_k(oos)},
        "final_fit_pre_sep17": final,
        "final_fit_pre_sep17_lam_ge_0_sensitivity": final_ge0,
        "lam_identification_sensitivity": lam_ident,
        "final_fit_pre_sep17_sensitivities_vol_by_k": sens,
        "deviations": DEVIATIONS,
        "seconds": round(time.time() - t_start, 1),
        "peak_rss_mb": round(rusage_mb, 1),
    }
    OUT.write_text(json.dumps(report, indent=1, default=float))
    d = pooled["logloss_diff"]
    print(f"pooled OOS n={pooled['n']:,}: " + ", ".join(
        f"{k} {v['mean']:+.3e} (t {v['t_window']}, day {v['t_day']})" for k, v in d.items()))
    print(f"wrote {OUT}\nwrote {PARAMS_OUT}\n{report['seconds']}s, peak RSS {report['peak_rss_mb']} MB")


DEVIATIONS = [
    "lam bound (review finding 4): the first run fitted every model with lam >= 0, a bound the pre-registration "
    "does not set (section 7: maximum likelihood; section 8 asks whether lam > 0). The pre-registered fits "
    "(reversion, full, and the step 2 params) now leave lam free in sign; lam >= -10 is kept only as a numerical "
    "guard for the line search and at_bound reports whether it binds. The lam >= 0 fits are reported beside as a "
    "sensitivity (reversion_lam_ge_0, full_lam_ge_0), scored out of sample on the same rows.",
    "martingale_scaled (0s) and model_free_sign_reversal_by_k are diagnostics, not pre-registered.",
    "Second review (lam identification), a sensitivity added after the pre-registration: in section 1 kappa sets "
    "both the mean pull (1 - e^-K) and the variance shrink V < sigma^2 h, and sigma is the trailing hour's "
    "realised volatility, whose ratio to the coming quarter's volatility differs by quarter of the hour. So lam "
    "can fit an intra-hour volatility pattern instead of reversion that grows through the hour. reversion_vol_by_k "
    "and full_vol_by_k give each quarter its own variance scale s_k (V = s_k^2 sigma^2 V1), fitted walk-forward and "
    "on the pre-Sep-17 rows beside the as-coded fits; lam_identification_sensitivity reports lam, its clustered z, "
    "the in-sample cost of lam >= 0 and the reversion fraction by quarter for both on the same rows, and theta "
    "(the fade/follow question) for the full models. The (0k) "
    "diagnostic (one martingale scale per quarter) is added beside the single-scale (0s), which cannot see a "
    "pattern by quarter. The pre-registered params used by step 2 are unchanged (model (3) as coded).",
    "t-statistics are reported for many cells (folds, quarter k, minute, coin, k x minute); only the pooled "
    "out-of-sample comparisons of full, reversion and momentum against the martingale are the pre-registered "
    "headline, the breakdowns are exploratory and not corrected for multiple comparisons.",
]


if __name__ == "__main__":
    main()
