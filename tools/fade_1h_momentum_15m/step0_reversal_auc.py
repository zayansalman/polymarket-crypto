"""Step 0 of the pre-registered test: reproduce the published 15-minute reversal on our Binance data.

Pre-registration: tasks/2026-09-21-fade-1h-momentum-on-15m.md, section 8, step 0.
Reference: Kitron & Wengrowicz 2026, arXiv 2608.21888 (section 2 protocol box, Table 2,
Appendix A.3-A.4, Table 6).

Candles: Binance spot 15m built from 1m by ``data.candles_15m`` (identical to Binance's own 15m
klines). As in the paper: intra-bar return r_t = (close_t - open_t) / open_t and label
up_t = 1[r_t > 0], so flat bars (close == open) are labelled 0. Labelled rows are the quarters
opening 2026-03-01 00:00 .. 2026-09-20 23:45 UTC; features reach back into 2026-02-28 for the
first rows' lags and never past t-1 (r_{t-1}'s close is the price at bar t's open).

(a) one-lag score -sign(r_{t-1}): AUC (Mann-Whitney, midranks for tied scores) and accuracy
(b) sign-flip rate by decile of |r_{t-1}|
(c) moving-block bootstrap 95% CI of the (a) AUC: block 384, B = 300, percentile interval,
    one-sided p = (1 + #{AUC* <= 0.5}) / (B + 1), t = (AUC - 0.5) / bootstrap SD
(d) constrained logit P(up) = sigmoid(C + A sum_{k=1..12} k^-alpha tanh(150 r_{t-k})), walk-forward
    train 5,760 / test 960 (step 960). alpha profiled on a [0, 3] grid, chosen by log-loss on the
    last 20% of each training window, then (C, A) refit on the whole window. Out-of-sample blocks
    are concatenated and scored once; the bootstrap holds the fitted models fixed (fit-conditional,
    as the paper's per-asset test). The N = 1 special case sigmoid(C + A tanh(150 r_{t-1})) is fit
    the same way; it is what the paper's "one-lag 0.527" measures.

Every comparison is on the same rows; the row count is stored beside each number.

Reproduction check (review finding 7). The pre-registration says the one-lag sign score should
give an AUC "near 0.53 (0.533 BTC, 0.538 ETH, 0.536 XRP)". Those three numbers are the paper's
Table 2 constrained 12-lag logit, not a sign score. The report therefore gives, per coin:
(1) the check as written: the sign score's CI against the quoted numbers; (2) like for like: our
walk-forward 12-lag logit against Table 2's 12-lag logit (point and CI); (3) the one-lag figures
beside the paper's one-lag class mean, which is over 183 pairs, a different population from our
four coins; (4) the rule the first run used instead (sign-score CI lower bound > 0.5), which is
not the pre-registered one. No single pass/fail flag. The 40 flip-rate decile cells are reported
with Holm and Benjamini-Hochberg adjusted p-values (binomial t, rows treated as independent).

    PYTHON_DOTENV_DISABLED=1 python3 tools/fade_1h_momentum_15m/step0_reversal_auc.py
"""
from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from scipy.special import expit
from scipy.stats import rankdata

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data  # noqa: E402  (sibling module: tools/fade_1h_momentum_15m/data.py)

OUT = data.OUT_DIR / "step0.json"
COINS = ["btc", "eth", "sol", "xrp"]
CRITERION_COINS = ["btc", "eth", "xrp"]

START = int(datetime(2026, 3, 1, tzinfo=UTC).timestamp())  # first labelled quarter
END = int(datetime(2026, 9, 21, tzinfo=UTC).timestamp())  # exclusive: last labelled quarter opens 09-20 23:45
HIST = int(datetime(2026, 2, 28, tzinfo=UTC).timestamp())  # lag history only, never labelled

LAM = 150.0  # tanh scale, fixed in the paper
NLAG = 12
TRAIN, TEST, VAL_FRAC = 5760, 960, 0.20
ALPHAS = np.round(np.arange(0.0, 3.0 + 1e-9, 0.05), 2)  # paper: grid on [0, 3]; step not stated
BLOCK, B = 384, 300
SEED = 20260921
DAY = 96  # quarters per day, for the day-aligned label-shift null

PAPER = {
    "source": "Kitron & Wengrowicz 2026, arXiv 2608.21888",
    "sample": "Binance spot 15m, 2025-01-01..2026-02-11, 33,312 OOS candles per focal coin",
    "table2_constrained_logit_N12_oos_auc": {"btc": [0.533, 0.527, 0.539], "eth": [0.538, 0.532, 0.544],
                                             "xrp": [0.536, 0.530, 0.542],
                                             "sol_independent_venue_refetch": 0.529},
    "table6_class_mean_auc_183_pairs": {"N1": 0.5267, "N12": 0.5305},
    "nonflat_rescoring_class_mean_auc": 0.520,
    "holdout_2026-02-12_to_2026-08-08_crypto_class_mean_auc": 0.522,
    "flip_rate_by_decile_class_mean": "50.2% (smallest |r_{t-1}|) -> 53.0% (largest), monotone",
    "btc_unthresholded_accuracy": 0.523,
}
# What the pre-registration (section 8, step 0) quotes for the one-lag SIGN score. These are the
# paper's Table 2 N = 12 constrained-logit AUCs (PAPER above), attached to the wrong score.
PREREG_QUOTED = {"btc": 0.533, "eth": 0.538, "xrp": 0.536}


# --------------------------------------------------------------------------- statistics


def auc(score: np.ndarray, y: np.ndarray) -> float:
    """Mann-Whitney AUC with midranks, so a tied score pair counts one half."""
    y = y.astype(bool)
    n1 = int(y.sum())
    n0 = y.size - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    r = rankdata(score)
    return float((r[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def block_starts(n: int, rng: np.random.Generator) -> np.ndarray:
    """(B, ceil(n / BLOCK)) moving-block start positions, uniform on [0, n - BLOCK]."""
    return rng.integers(0, n - BLOCK + 1, size=(B, -(-n // BLOCK)))


def block_index(starts_row: np.ndarray, n: int) -> np.ndarray:
    return (starts_row[:, None] + np.arange(BLOCK)).ravel()[:n]


def boot_summary(point: float, reps: np.ndarray, n: int) -> dict:
    reps = reps[np.isfinite(reps)]
    sd = float(reps.std(ddof=1))
    return {"auc": round(point, 5), "n": int(n),
            "ci95": [round(float(np.percentile(reps, 2.5)), 5), round(float(np.percentile(reps, 97.5)), 5)],
            "boot_sd": round(sd, 5), "t": round((point - 0.5) / sd, 2) if sd > 0 else None,
            "p_one_sided_auc_gt_half": round(float((1 + np.sum(reps <= 0.5)) / (reps.size + 1)), 4),
            "B": int(reps.size)}


def bootstrap(series: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray | None]], n: int,
              rng: np.random.Generator, diffs: list[tuple[str, str]] = ()) -> dict:
    """Fit-conditional moving-block bootstrap of several AUCs on ONE shared set of block draws.

    ``series[name] = (score, y, keep)`` over the same n time-ordered rows; ``keep`` (bool or None)
    restricts the replicate to those rows (flat-dropped rescoring). ``diffs`` are paired
    differences (a - b) read off the same replicates.
    """
    starts = block_starts(n, rng)
    reps = {k: np.empty(B) for k in series}
    for b in range(B):
        idx = block_index(starts[b], n)
        for k, (s, y, keep) in series.items():
            ii = idx if keep is None else idx[keep[idx]]
            reps[k][b] = auc(s[ii], y[ii])
    out = {}
    for k, (s, y, keep) in series.items():
        sel = slice(None) if keep is None else keep
        out[k] = boot_summary(auc(s[sel], y[sel]), reps[k], int(np.asarray(y[sel]).size))
    for a, b_ in diffs:
        d = reps[a] - reps[b_]
        pt = out[a]["auc"] - out[b_]["auc"]
        out[f"{a}_minus_{b_}"] = {"diff": round(pt, 5), "n": out[a]["n"],
                                  "ci95": [round(float(np.percentile(d, 2.5)), 5),
                                           round(float(np.percentile(d, 97.5)), 5)],
                                  "t": round(pt / float(d.std(ddof=1)), 2)}
    return out


def logloss(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(p, 1e-12, 1 - 1e-12)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log1p(-p)))


def fit_logit(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """(C, A) of sigmoid(C + A x) by Newton-Raphson; unpenalised MLE, as in the paper."""
    X = np.column_stack([np.ones_like(x), x])
    w = np.array([np.log(y.mean() / (1 - y.mean())), 0.0])
    for _ in range(100):
        p = expit(X @ w)
        g = X.T @ (y - p)
        H = (X * (p * (1 - p))[:, None]).T @ X
        step = np.linalg.solve(H, g)
        w += step
        if np.max(np.abs(step)) < 1e-10:
            return w
    raise RuntimeError("logit did not converge")


# --------------------------------------------------------------------------- per coin


def load(coin: str) -> dict:
    q = data.candles_15m(coin, HIST, END)
    if not q.complete.all():
        raise ValueError(f"{coin}: {int((~q.complete).sum())} incomplete quarters in {HIST}..{END}")
    h0 = int(np.searchsorted(q.t, START))
    if q.t[h0] != START or h0 < NLAG + 1 or q.t[-1] != END - data.Q15:
        raise ValueError(f"{coin}: quarter grid does not span {START}..{END}")
    r = q.c / q.o - 1.0  # intra-bar, the paper's convention
    flat = q.c == q.o
    rcc = np.full_like(r, np.nan)
    rcc[1:] = q.c[1:] / q.c[:-1] - 1.0  # close-to-close, sensitivity only
    return {"t": q.t, "o": q.o, "c": q.c, "r": r, "flat": flat, "rcc": rcc, "h0": h0}


def one_lag(d: dict) -> dict:
    """(a) accuracy and point AUCs of -sign(r_{t-1}) over every labelled row (bootstrap in (c))."""
    h0, r, flat = d["h0"], d["r"], d["flat"]
    y = (r[h0:] > 0).astype(float)
    fl = flat[h0:]
    s = -np.sign(r[h0 - 1:-1])
    decided = s != 0
    pred_up = s > 0
    acc_dec = float(np.mean(pred_up[decided] == y[decided].astype(bool)))
    acc_half = float((np.sum(pred_up[decided] == y[decided].astype(bool)) + 0.5 * np.sum(~decided)) / y.size)
    nf = ~fl
    dec_nf = decided & nf
    return {
        "n": int(y.size), "up_rate": round(float(y.mean()), 5), "n_flat_label_bars": int(fl.sum()),
        "flat_share": round(float(fl.mean()), 5), "n_prev_flat_score_ties": int((~decided).sum()),
        "accuracy_decided_rows": {"acc": round(acc_dec, 5), "n": int(decided.sum())},
        "accuracy_all_rows_ties_half": {"acc": round(acc_half, 5), "n": int(y.size)},
        "always_down_accuracy_same_rows": {"acc": round(float(1 - y.mean()), 5), "n": int(y.size)},
        "flat_dropped": {
            "accuracy_decided_rows": {"acc": round(float(np.mean(pred_up[dec_nf] == y[dec_nf].astype(bool))), 5),
                                      "n": int(dec_nf.sum())},
            "up_rate": round(float(y[nf].mean()), 5), "n": int(nf.sum())},
    }


def flip_deciles(d: dict) -> dict:
    """(b) P(sign r_t != sign r_{t-1}) by decile of |r_{t-1}|; rows where either bar is flat are excluded."""
    h0, r = d["h0"], d["r"]
    cur, prev = r[h0:], r[h0 - 1:-1]
    m = (cur != 0) & (prev != 0)
    a = np.abs(prev[m])
    flip = np.sign(cur[m]) != np.sign(prev[m])
    n = a.size
    dec = np.minimum(((rankdata(a) - 0.5) * 10 / n).astype(int), 9)  # tied |r| share a decile
    rows = []
    for k in range(10):
        f = flip[dec == k]
        p = float(f.mean())
        rows.append({"decile": k + 1, "n": int(f.size),
                     "abs_r_bp": [round(1e4 * float(a[dec == k].min()), 3), round(1e4 * float(a[dec == k].max()), 3)],
                     "flip_rate": round(p, 5), "t_vs_half": round((p - 0.5) / np.sqrt(0.25 / f.size), 2)})
    p_all = float(flip.mean())
    return {"n": int(n), "excluded_flat_rows": int((~m).sum()), "flip_rate_all": round(p_all, 5),
            "t_all_vs_half": round((p_all - 0.5) / np.sqrt(0.25 / n), 2),
            "t_note": "binomial t against 0.5, rows treated as independent", "deciles": rows}


def perm_null(score: np.ndarray, y: np.ndarray) -> dict:
    """Day-aligned circular label shifts (the paper's permutation null): score-label alignment broken,
    label autocorrelation and intraday pattern kept."""
    n = y.size
    r = rankdata(score)
    yb = y.astype(bool)
    n1 = int(yb.sum())
    n0 = n - n1
    obs = (r[yb].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)
    null = np.array([(r[np.roll(yb, DAY * k)].sum() - n1 * (n1 + 1) / 2) / (n1 * n0) for k in range(1, n // DAY)])
    return {"auc": round(float(obs), 5), "n_shifts": int(null.size), "null_mean": round(float(null.mean()), 5),
            "null_sd": round(float(null.std(ddof=1)), 5), "null_max": round(float(null.max()), 5),
            "z": round(float((obs - null.mean()) / null.std(ddof=1)), 2),
            "p": round(float((1 + np.sum(null >= obs)) / (1 + null.size)), 4)}


def walk_forward(d: dict) -> dict:
    """(d) constrained logit N = 12 and N = 1, walk-forward; OOS predictions on the concatenated test blocks."""
    h0, r = d["h0"], d["r"]
    n = r.size - h0
    y = (r[h0:] > 0).astype(float)
    spin = np.tanh(LAM * r)
    S = np.column_stack([spin[h0 - k:r.size - k] for k in range(1, NLAG + 1)])  # S[i, k-1] = s_{t-k}
    Xa = S @ (np.arange(1, NLAG + 1)[None, :] ** -ALPHAS[:, None]).T  # (n, n_alpha): kernel sum per alpha
    n_fit = int(round(TRAIN * (1 - VAL_FRAC)))
    p12, p1, pbase = (np.full(n, np.nan) for _ in range(3))
    folds = []
    f0 = 0
    while f0 + TRAIN < n:
        tr, te = slice(f0, f0 + TRAIN), slice(f0 + TRAIN, min(f0 + TRAIN + TEST, n))
        fit, val = slice(f0, f0 + n_fit), slice(f0 + n_fit, f0 + TRAIN)
        val_ll = []
        for j in range(ALPHAS.size):
            w = fit_logit(Xa[fit, j], y[fit])
            val_ll.append(logloss(expit(w[0] + w[1] * Xa[val, j]), y[val]))
        j = int(np.argmin(val_ll))
        w12 = fit_logit(Xa[tr, j], y[tr])
        w1 = fit_logit(S[tr, 0], y[tr])
        p12[te] = expit(w12[0] + w12[1] * Xa[te, j])
        p1[te] = expit(w1[0] + w1[1] * S[te, 0])
        pbase[te] = y[tr].mean()
        folds.append({"fold": len(folds), "train_first_utc": data._iso(d["t"][h0 + f0]),
                      "test_first_utc": data._iso(d["t"][h0 + te.start]), "n_test": te.stop - te.start,
                      "alpha": float(ALPHAS[j]), "alpha_at_grid_edge": j in (0, ALPHAS.size - 1),
                      "N12_C": round(float(w12[0]), 4), "N12_A": round(float(w12[1]), 4),
                      "N1_C": round(float(w1[0]), 4), "N1_A": round(float(w1[1]), 4)})
        f0 += TEST
    oos = slice(TRAIN, n)
    yo = y[oos]
    fits = {k: {"logloss": round(logloss(p[oos], yo), 6), "brier": round(float(np.mean((p[oos] - yo) ** 2)), 6)}
            for k, p in (("N12", p12), ("N1", p1), ("base_rate", pbase))}
    return {"oos_slice": [TRAIN, n], "n_oos": n - TRAIN, "folds": folds, "fit_quality_same_rows": fits,
            "A_negative_folds_N12": sum(f["N12_A"] < 0 for f in folds),
            "A_negative_folds_N1": sum(f["N1_A"] < 0 for f in folds),
            "_p12": p12[oos], "_p1": p1[oos]}


def run_coin(coin: str, rng: np.random.Generator) -> dict:
    d = load(coin)
    h0, r, flat, rcc = d["h0"], d["r"], d["flat"], d["rcc"]
    n = r.size - h0
    y = (r[h0:] > 0).astype(float)
    nf = ~flat[h0:]
    s_sign = -np.sign(r[h0 - 1:-1])
    s_cont = -r[h0 - 1:-1]  # the ranking of the fitted N = 1 logit whenever A < 0
    y_cc = (rcc[h0:] > 0).astype(float)
    s_cc = -np.sign(rcc[h0 - 1:-1])
    t0 = time.time()

    out = {"coin": coin, "first_row_utc": data._iso(d["t"][h0]), "last_row_utc": data._iso(d["t"][-1]),
           "open_differs_from_prev_close_share": round(float(np.mean(d["o"][h0:] != d["c"][h0 - 1:-1])), 5)}
    out["a_one_lag"] = one_lag(d)
    out["b_flip_deciles"] = flip_deciles(d)
    # (c) full-sample bootstrap: one shared block draw for every full-sample score
    out["c_bootstrap_full_sample"] = bootstrap({
        "sign_all_bars": (s_sign, y, None),
        "sign_flat_dropped": (s_sign, y, nf),
        "neg_r_all_bars": (s_cont, y, None),
        "neg_r_flat_dropped": (s_cont, y, nf),
        "sign_close_to_close": (s_cc, y_cc, None),
    }, n, rng, diffs=[("neg_r_all_bars", "sign_all_bars")])
    out["c_permutation_null_sign_all_bars"] = perm_null(s_sign, y)
    # (d) walk-forward, then every OOS score on the SAME OOS rows with one shared block draw
    wf = walk_forward(d)
    p12, p1 = wf.pop("_p12"), wf.pop("_p1")
    oos = slice(TRAIN, n)
    yo, nfo = y[oos], nf[oos]
    wf["bootstrap_same_oos_rows"] = bootstrap({
        "N12_logit": (p12, yo, None),
        "N1_logit": (p1, yo, None),
        "sign_score": (s_sign[oos], yo, None),
        "neg_r": (s_cont[oos], yo, None),
        "N12_logit_flat_dropped": (p12, yo, nfo),
        "sign_score_flat_dropped": (s_sign[oos], yo, nfo),
    }, yo.size, rng, diffs=[("N12_logit", "N1_logit"), ("N12_logit", "sign_score"),
                                      ("N1_logit", "sign_score")])
    wf["permutation_null_N12"] = perm_null(p12, yo)
    out["d_walk_forward"] = wf
    out["seconds"] = round(time.time() - t0, 1)
    return out


def main() -> None:
    ss = np.random.SeedSequence(SEED)
    rngs = dict(zip(COINS, (np.random.default_rng(s) for s in ss.spawn(len(COINS)))))
    res = {}
    for coin in COINS:
        res[coin] = run_coin(coin, rngs[coin])
        c = res[coin]["c_bootstrap_full_sample"]["sign_all_bars"]
        print(f"{coin}: one-lag sign AUC {c['auc']:.4f} {c['ci95']} n={c['n']} t={c['t']}  "
              f"({res[coin]['seconds']}s)", flush=True)

    def cmean(get, coins=COINS):
        return round(float(np.mean([get(res[c]) for c in coins])), 5)

    summary = {
        "one_lag_sign_all_bars": {c: res[c]["c_bootstrap_full_sample"]["sign_all_bars"] for c in COINS},
        "class_mean_4_coins": {
            "population": "these four coins (BTC/ETH/SOL/XRP), 2026-03-01..09-20; the paper's class means are over "
                          "183 Binance pairs, 2025-01..2026-02: different populations, not a like-for-like comparison",
            "sign_all_bars_full": cmean(lambda x: x["c_bootstrap_full_sample"]["sign_all_bars"]["auc"]),
            "sign_flat_dropped_full": cmean(lambda x: x["c_bootstrap_full_sample"]["sign_flat_dropped"]["auc"]),
            "neg_r_all_bars_full": cmean(lambda x: x["c_bootstrap_full_sample"]["neg_r_all_bars"]["auc"]),
            "N1_logit_oos": cmean(lambda x: x["d_walk_forward"]["bootstrap_same_oos_rows"]["N1_logit"]["auc"]),
            "N12_logit_oos": cmean(lambda x: x["d_walk_forward"]["bootstrap_same_oos_rows"]["N12_logit"]["auc"]),
            "N12_logit_flat_dropped_oos": cmean(
                lambda x: x["d_walk_forward"]["bootstrap_same_oos_rows"]["N12_logit_flat_dropped"]["auc"]),
            "flip_rate_by_decile": [cmean(lambda x, k=k: x["b_flip_deciles"]["deciles"][k]["flip_rate"])
                                    for k in range(10)]},
    }
    summary["flip_decile_cells"] = flip_cells(res)
    reproduction = reproduction_checks(res)
    report = {
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),
        "protocol": {
            "pre_registration": "tasks/2026-09-21-fade-1h-momentum-on-15m.md section 8 step 0",
            "candles": "Binance spot 15m from 1m (data.candles_15m), UTC quarter-hours",
            "labelled_rows": "2026-03-01 00:00 .. 2026-09-20 23:45 UTC; lags may reach into 2026-02-28",
            "return": "r_t = close_t / open_t - 1 (intra-bar)", "label": "1[r_t > 0], flat -> 0",
            "tanh_scale": LAM, "n_lags": NLAG, "alpha_grid": [float(ALPHAS[0]), float(ALPHAS[-1]), 0.05],
            "walk_forward": {"train": TRAIN, "test": TEST, "step": TEST, "validation_tail": VAL_FRAC,
                             "last_fold": "partial test block kept"},
            "bootstrap": {"type": "moving block, non-circular, fit-conditional, percentile CI", "block": BLOCK,
                          "B": B, "seed": SEED},
            "permutation_null": "circular label shifts by whole days (96 quarters)",
        },
        "paper_reference": PAPER,
        "preregistration_quoted_values": {**PREREG_QUOTED, "note": "quoted for the one-lag sign score; they are the "
                                          "paper's Table 2 N = 12 constrained-logit AUCs"},
        "reproduction": reproduction,
        "deviations": DEVIATIONS,
        "summary": summary,
        "coins": res,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=1, default=float))
    for c in CRITERION_COINS:
        a = reproduction["as_preregistered_sign_score_vs_quoted"][c]
        b = reproduction["like_for_like_N12_logit_vs_paper_table2"][c]
        print(f"{c}: sign AUC {a['auc']} CI {a['ci95']} vs quoted {a['quoted']} (contains: {a['ci_contains_quoted']}); "
              f"N12 logit {b['ours']['auc']} CI {b['ours']['ci95']} vs Table 2 {b['paper']['auc']} "
              f"{b['paper']['ci95']} (our CI contains theirs: {b['our_ci_contains_paper_point']}, "
              f"CIs overlap: {b['cis_overlap']})")
    print(f"wrote {OUT}")


def flip_cells(res: dict) -> dict:
    """The 40 coin x decile flip-rate cells with multiplicity-adjusted p (binomial t, rows independent)."""
    cells = [(c, r["decile"], r["t_vs_half"]) for c in COINS for r in res[c]["b_flip_deciles"]["deciles"]]
    p = [data.p_two_sided(t) for _, _, t in cells]
    ph, pb = data.holm(p), data.bh(p)
    return {"n_cells": len(cells), "t": "binomial t against 0.5, rows treated as independent",
            "n_abs_t_ge_2": int(sum(abs(t) >= 2 for _, _, t in cells)),
            "expected_abs_t_ge_2_if_no_effect": round(len(cells) * data.p_two_sided(2.0), 1),
            "cells": [{"coin": c, "decile": d, "t": t, "p": round(pi, 5), "holm_p": round(h, 5), "bh_p": round(b, 5)}
                      for (c, d, t), pi, h, b in zip(cells, p, ph, pb)],
            "holm_p_below_0.05": [f"{c} decile {d}" for (c, d, _), h in zip(cells, ph) if h < 0.05],
            "bh_p_below_0.05": [f"{c} decile {d}" for (c, d, _), b in zip(cells, pb) if b < 0.05]}


def reproduction_checks(res: dict) -> dict:
    """Step 0 read four ways (see the module docstring); no single pass/fail flag."""
    as_written, like, one_lag, first_rule = {}, {}, {}, {}
    for c in COINS:
        sg = res[c]["c_bootstrap_full_sample"]["sign_all_bars"]
        oos = res[c]["d_walk_forward"]["bootstrap_same_oos_rows"]
        if c in PREREG_QUOTED:
            q = PREREG_QUOTED[c]
            as_written[c] = {"auc": sg["auc"], "ci95": sg["ci95"], "n": sg["n"], "quoted": q,
                             "ci_contains_quoted": bool(sg["ci95"][0] <= q <= sg["ci95"][1])}
            pt, plo, phi = PAPER["table2_constrained_logit_N12_oos_auc"][c]
            ours = oos["N12_logit"]
            like[c] = {"ours": {k: ours[k] for k in ("auc", "ci95", "n")},
                       "paper": {"auc": pt, "ci95": [plo, phi], "n": 33312},
                       "our_ci_contains_paper_point": bool(ours["ci95"][0] <= pt <= ours["ci95"][1]),
                       "paper_ci_contains_our_point": bool(plo <= ours["auc"] <= phi),
                       "cis_overlap": bool(ours["ci95"][0] <= phi and plo <= ours["ci95"][1])}
            first_rule[c] = {"ci95_lower": sg["ci95"][0], "lower_above_half": bool(sg["ci95"][0] > 0.5)}
        one_lag[c] = {"sign_score_full_sample": {k: sg[k] for k in ("auc", "ci95", "n")},
                      "sign_score_oos_rows": {k: oos["sign_score"][k] for k in ("auc", "ci95", "n")},
                      "N1_logit_oos": {k: oos["N1_logit"][k] for k in ("auc", "ci95", "n")}}
    return {
        "as_preregistered_sign_score_vs_quoted": {
            **as_written, "note": "the check as the pre-registration words it; the quoted numbers belong to the "
                                  "12-lag logit, so this compares different scores"},
        "like_for_like_N12_logit_vs_paper_table2": {
            **like, "note": "same score (walk-forward constrained 12-lag logit, out of sample) on different periods: "
                            "ours 2026-03..09 (13,824 OOS rows per coin), the paper's 2025-01..2026-02 (33,312)"},
        "one_lag_vs_paper_class_mean": {
            **one_lag, "ours_4_coin_mean_N1_logit_oos": round(float(np.mean(
                [one_lag[c]["N1_logit_oos"]["auc"] for c in COINS])), 5),
            "paper_table6_N1_class_mean_183_pairs": PAPER["table6_class_mean_auc_183_pairs"]["N1"],
            "note": "different populations (4 coins vs 183 pairs, different periods): context, not a test"},
        "rule_used_in_first_run_not_preregistered": {
            **first_rule, "rule": "sign-score CI lower bound > 0.5 for BTC, ETH and XRP (weaker than 'near 0.53')"},
    }


DEVIATIONS = [
    "Review finding 7: the pre-registration quotes the paper's Table 2 12-lag logit AUCs (0.533/0.538/0.536) as "
    "the target for the one-lag sign score. The first run then judged reproduction by its own rule (sign-score CI "
    "lower bound > 0.5), which the pre-registration does not state, and set a 4-coin mean beside the paper's 183-pair "
    "mean. Now every reading is reported side by side (reproduction), with no single flag, and the population "
    "difference is labelled.",
    "The flip-rate decile t-statistics are binomial (rows independent) over 40 coin x decile cells; Holm and "
    "Benjamini-Hochberg adjusted p-values are given beside them.",
]


if __name__ == "__main__":
    main()
