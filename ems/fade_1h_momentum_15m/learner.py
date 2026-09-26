"""Live learning for Fade 1h Momentum on 15m: the dials move with every settled window.

Section 5 of ``tasks/2026-09-22-fade-1h-sizing.md``: after each settled window, one step
of recursive maximum likelihood with forgetting, around a fixed prior,

    I_t = gamma I_{t-1} + Fisher_t
    beta_t = beta_{t-1} + (I_t + P)^-1 [score_t(beta_{t-1}) - (1 - gamma) P (beta_{t-1} - beta_0)],

on the model's dials (theta, kappa0, lam, alpha, c), the market anchor's weights (w_M, w_S) and
the four-coin correlation (rho). ``gamma`` gives about two days of windows half the weight, so
one window barely moves the dials and a day of consistent disagreement moves the anchor a lot.

The fixed prior (``P``, ``beta_0``): the process fit's own information, 1 / SE^2 of each of
theta, kappa0, lam, alpha and c at their fitted values (76,800 windows' worth of Binance
minutes), is never forgotten. Settled 15m results say little about how the price moves, so
without it those five dials would wander across their bounds on noise alone; with it they stay
within a fraction of the fit's standard error unless the results really disagree with it. The
step above is the Newton step on "the forgotten evidence plus the fixed prior": the prior's
pull, (1 - gamma) P (beta - beta_0), is the part of it the forgetting takes away each window.
The anchor weights and rho have no fixed prior: they are what the live results are for.

What one window contributes
---------------------------
- The window's real result (Up or Down) and every decision row recorded in it with complete
  inputs. Each row is re-priced under the current dials (``decide.evaluate``): the anchored
  ``p = Phi(w_M z_market + w_S z_model)``. The window counts once: its score and Fisher
  information are the averages over its rows of the probit model's
  ``(o - p) phi(z) / (p (1 - p)) grad z`` and ``phi(z)^2 / (p (1 - p)) grad z grad z^T``.
- ``grad z``: exact in theta, alpha, c (through the stretch), w_M and w_S; central differences
  in kappa0 and lam (the moments are integrals of closed forms; a relative step of 1e-4).
- rho: once every coin of a 15-minute slot has settled, the one-factor Gaussian copula's
  likelihood of the slot's pattern of results (``sizing.outcome_probabilities``, marginals =
  each coin's anchored p at the row nearest minute 2), with its own score and information.
- Bounded steps: no dial moves more than ``MAX_STEP`` in one window's step, and each stays
  inside ``BOUNDS``. A step that would leave the bounds stops at them.
- The evidence: each window's rows are read by its slug (``ledger.decisions_for_window``), so
  the cost is that window's rows and no other window's rows can leak in.

Every update is stored as a new ``fade_dials`` version with source ``"live"``; the version
carries the learner's information matrix and its fixed prior (``params["learner"]``) so the
next update continues from them, across restarts. The starting dials are version 1 (``STARTING_DIALS``, source
``"fit_sep17_20"``); version 0 is the prior, which the learner never updates.

``learn_from_settled`` never raises: failures come back in the report for the card.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ems.logging_setup import get_logger
from ems.fade_1h_momentum_15m import decide as _decide
from ems.fade_1h_momentum_15m import ledger as _ledger
from ems.fade_1h_momentum_15m import model as _model
from ems.fade_1h_momentum_15m import sizing as _sizing
from ems.fade_1h_momentum_15m.decide import ModelState, NoDecision

log = get_logger("fade_1h.learner")

ASSETS = ("btc", "eth", "sol", "xrp")
LEARNED = ("theta", "kappa0", "lam", "alpha", "c", "w_M", "w_S")
LIVE_SOURCE = "live"
STARTING_SOURCE = "fit_sep17_20"
STATE_KEY = "learner"

# About two days of windows carry half the weight: 96 windows a day for each of four coins.
HALF_LIFE_WINDOWS = 2 * 96 * 4
GAMMA = 0.5 ** (1.0 / HALF_LIFE_WINDOWS)
HALF_LIFE_SLOTS = 2 * 96  # rho learns once per 15-minute slot
GAMMA_RHO = 0.5 ** (1.0 / HALF_LIFE_SLOTS)
MEMORY_WINDOWS = 1.0 / (1.0 - GAMMA)  # the information of this many windows at steady state
MEMORY_SLOTS = 1.0 / (1.0 - GAMMA_RHO)

BOUNDS: Mapping[str, tuple[float, float]] = MappingProxyType({
    "theta": (-3.0, 3.0), "kappa0": (0.0, 50.0), "lam": (-10.0, 20.0), "alpha": (0.0, 4.0),
    "c": (1e-5, 0.1), "w_M": (0.0, 3.0), "w_S": (-3.0, 3.0), "rho": (0.0, 0.99),
})
MAX_STEP: Mapping[str, float] = MappingProxyType({
    "theta": 0.05, "kappa0": 0.25, "lam": 0.5, "alpha": 0.1, "c": 0.002,
    "w_M": 0.05, "w_S": 0.05, "rho": 0.02,
})
_RIDGE = 1e-9  # added to the information's diagonal, relative to its own scale
_REL_STEP = 1e-4  # central differences in kappa0 and lam
_P_EPS = 1e-9
MINUTE_2_S = 120.0  # rho's marginals: the row nearest this far into the window
MAX_SLOTS_KEPT = 400  # slots already used for rho, remembered to count each once

# ---------------------------------------------------------------------------------------------
# The starting dials: version 1
# ---------------------------------------------------------------------------------------------

# How the price moves (theta, kappa0, lam, alpha, c): the research's step 1 fit on Binance
# 1-minute data, 2026-03-01 to 2026-09-16 (data/fade_1h_momentum_15m/params_pre_sep17.json on
# the research branch; 384,000 rows, 19,200 fifteen-minute slots). They describe the process,
# so they carry over to the TWAP-60s settlement.
# The market anchor (w_M, w_S) and rho: fitted 2026-09-22 on the Sep 17-20 Polymarket tape
# (m15.db, wallets.db): p_model recomputed with the TWAP-60s settlement at minute 2 of every
# settled 15m window (start reference = the Binance minute before the open), maximum likelihood
# of Phi(w_M probit(m) + w_S probit(p_model)) on the real results, 1,071 windows in 273 slots;
# rho by maximum likelihood of the one-factor Gaussian copula on the 273 slots' results with
# those p as marginals (254 slots had all four coins; all four settled the same way in 155).
STARTING_DIALS: Mapping[str, float] = MappingProxyType({
    "w_M": 0.45447094334177934,
    "w_S": 0.5661594276343016,
    "theta": -0.04337994156163829,
    "kappa0": 0.3450046754153034,
    "lam": -1.619586626172444,
    "alpha": 0.9777935961999615,
    "c": 0.00931853032495376,
    "rho": 0.7501567685091034,
    "blend_HH": _model.BLEND_HH,
    "blend_LL": _model.BLEND_LL,
    "blend_HL": _model.BLEND_HL,
})
STARTING_FIT: Mapping[str, Any] = MappingProxyType({
    "anchor_windows": 1071,
    "anchor_slots": 273,
    "w_M_se": 0.24937486246524068,
    "w_S_se": 0.2387848295772319,
    "w_M_se_clustered": 0.3128392164489325,
    "w_S_se_clustered": 0.291959232395869,
    "loglik": -675.1551706412051,
    "loglik_market_only": -677.9652391051764,
    "loglik_model_only": -676.8477899245822,
    "rho_se": 0.0346202445954882,
    "rho_slots": 273,
    "process_rows": 384000,
    "process_slots": 19200,
})
# The anchor fit's expected information (2 x 2, w_M then w_S) over its 1,071 windows.
_ANCHOR_INFO = ((115.32561373553072, 111.72845476660447),
                (111.72845476660447, 125.78175726247159))
# The process fit's standard errors, clustered by slot (params_pre_sep17.json, robust).
_PROCESS_SE: Mapping[str, float] = MappingProxyType({
    "theta": 0.021680485029106394, "kappa0": 0.13685334297681573, "lam": 0.4843993864920555,
    "alpha": 0.14193679100381607, "c": 0.003426019096080245,
})
STARTING_NOTE = (
    "Starting dials. How the price moves (theta, kappa0, lam, alpha, c): the research fit on "
    "Binance minutes from Mar 1 to Sep 16, kept as a fixed prior, so settled windows only "
    "nudge them. The market anchor and the coin correlation: fitted on the Sep 17-20 "
    "Polymarket tape with the TWAP-60s settlement, at minute 2 of each window: w_M 0.45, w_S "
    "0.57 (1,071 windows; the model and the market each carry about half the weight), rho "
    "0.75 (273 slots). That fit read the market from the last Up trade before minute 2 and the "
    "price move from Binance; the app reads the book's mid and the live Chainlink price at "
    "every minute. So these are starting values for the learner to correct, not the "
    "historical re-test."
)


def starting_prior() -> dict[str, list[float]]:
    """The fixed prior: the process fit's values and its full information (1 / SE^2) for
    theta, kappa0, lam, alpha and c, never forgotten; nothing for the anchor weights."""
    return {
        "mean": [float(STARTING_DIALS[name]) for name in LEARNED],
        "info": [1.0 / _PROCESS_SE[name] ** 2 if name in _PROCESS_SE else 0.0
                 for name in LEARNED],
    }


def starting_learner_state() -> dict[str, Any]:
    """The learner's state at the start. The forgotten part of the information holds the
    anchor fit (its 1,071 windows fit in the learner's memory of about two days of windows),
    so live evidence can move the anchor weights. The process fit is the fixed prior
    (``starting_prior``), so it is not also counted here."""
    n = len(LEARNED)
    info = [[0.0] * n for _ in range(n)]
    anchor_scale = min(1.0, MEMORY_WINDOWS / STARTING_FIT["anchor_windows"])
    for a in range(2):
        for b in range(2):
            info[5 + a][5 + b] = anchor_scale * _ANCHOR_INFO[a][b]
    rho_scale = min(1.0, MEMORY_SLOTS / STARTING_FIT["rho_slots"])
    return {
        "names": list(LEARNED),
        "info": info,
        "prior": starting_prior(),
        "rho_info": rho_scale / STARTING_FIT["rho_se"] ** 2,
        "windows": 0,
        "slots": 0,
        "rho_slots": [],
    }


def _prior_of(state: Mapping[str, Any]) -> tuple[list[float], list[float]]:
    """(mean, diagonal information) of a learner state's fixed prior. A state saved before
    the prior was kept gets the starting one."""
    prior = state.get("prior")
    if isinstance(prior, Mapping):
        mean = [float(x) for x in prior.get("mean") or ()]
        info = [float(x) for x in prior.get("info") or ()]
        if len(mean) == len(LEARNED) == len(info) and all(
                math.isfinite(x) for x in (*mean, *info)) and min(info) >= 0.0:
            return mean, info
    prior = starting_prior()
    return prior["mean"], prior["info"]


def starting_params() -> dict[str, Any]:
    """Version 1's params: the starting dials plus the learner's starting state."""
    return {**dict(STARTING_DIALS), STATE_KEY: starting_learner_state()}


async def seed_starting_dials(*, ts: float) -> dict:
    """Store the prior (version 0) if the table is empty, then the starting dials as version 1
    if the prior is all there is. Returns the newest dials."""
    current = await _ledger.seed_dials(ts=ts)
    if int(current["version"]) == 0 and current.get("source") == _ledger.PRIOR_SOURCE:
        await _ledger.save_dials(
            starting_params(), source=STARTING_SOURCE, ts=ts,
            n_windows=int(STARTING_FIT["anchor_windows"]), loglik=STARTING_FIT["loglik"],
            note=STARTING_NOTE,
        )
        current = await _ledger.dials() or current
    return current


# ---------------------------------------------------------------------------------------------
# Evidence and the pure update
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowEvidence:
    """One settled coin-window: its result and the model states of its recorded rows."""

    window_slug: str
    asset: str
    window_start: float
    outcome: str  # "Up" | "Down"
    states: tuple[ModelState, ...]


@dataclass(frozen=True)
class SlotEvidence:
    """One 15-minute slot every coin of which has settled: (anchored p of Up, settled Up)."""

    slot_start: float
    results: tuple[tuple[float, bool], ...]


@dataclass
class LearnResult:
    params: dict[str, Any]
    windows: int = 0
    slots: int = 0
    loglik: float | None = None  # mean log-likelihood per window, under the dials before
    moved: dict[str, float] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


def _dials_floats(params: Mapping[str, Any]) -> dict[str, float]:
    return _decide.resolve_dials(params)


def _z_and_grad(state: ModelState, dials: Mapping[str, float]) -> tuple[float, list[float]]:
    """The anchored z = w_M z_market + w_S z_model at one row, and its gradient in LEARNED."""
    view = _decide.evaluate(state, dials)
    parts = view.parts
    z_m, w_M, w_S = view.z_market, dials["w_M"], dials["w_S"]
    z_s = view.z_model
    grad_s = [0.0] * 5
    if parts.sd > 0.0 and abs(parts.z) < 7.0:  # beyond +-7 the probability is saturated
        sd, z = parts.sd, parts.z
        mom, theta, mu, v = parts.moments, dials["theta"], view.mu, view.v
        grad_s[0] = (mu * mom.Gbar) / sd - z * theta * v * mom.Gbar * mom.Gbar / (sd * sd)
        grad_s[3] = -mom.B * view.dM_dalpha / sd
        grad_s[4] = -mom.B * view.dM_dc / sd
        h, t = state.h, state.t
        for j, key in ((1, "kappa0"), (2, "lam")):
            base = dials[key]
            step = max(abs(base) * _REL_STEP, 1e-6)
            lo = base - step
            if key == "kappa0" and lo < 0.0:
                lo = base  # one-sided at the bound
            hi = base + step
            zs = []
            for value in (lo, hi):
                k0 = value if key == "kappa0" else dials["kappa0"]
                lm = value if key == "lam" else dials["lam"]
                p2 = _model.twap_parts(view.abar, state.d, view.M, mu, v, t, h, state.sigma,
                                       theta, k0, lm, _model.AVG_60S)
                zs.append(p2.z)
            grad_s[j] = (zs[1] - zs[0]) / (hi - lo)
    z = w_M * z_m + w_S * z_s
    return z, [w_S * g for g in grad_s] + [z_m, z_s]


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting; a small ridge keeps it well posed."""
    n = len(b)
    scale = max(1e-12, max(abs(a[i][i]) for i in range(n)))
    m = [[a[i][j] + (_RIDGE * scale if i == j else 0.0) for j in range(n)] + [b[i]]
         for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-300:
            raise ValueError("the information matrix is singular")
        m[col], m[piv] = m[piv], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            if f:
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (m[i][n] - sum(m[i][j] * x[j] for j in range(i + 1, n))) / m[i][i]
    return x


def _bounded(name: str, old: float, step: float) -> float:
    step = max(-MAX_STEP[name], min(MAX_STEP[name], step))
    lo, hi = BOUNDS[name]
    return min(hi, max(lo, old + step))


def window_score(evidence: WindowEvidence, dials: Mapping[str, float]
                 ) -> tuple[list[float], list[list[float]], float, int]:
    """(score, Fisher information, mean log-likelihood, rows used) of one window: averages
    over its rows. Rows the model cannot price are skipped."""
    n = len(LEARNED)
    score = [0.0] * n
    info = [[0.0] * n for _ in range(n)]
    ll = 0.0
    used = 0
    up = evidence.outcome == "Up"
    for state in evidence.states:
        try:
            z, g = _z_and_grad(state, dials)
        except (NoDecision, ValueError, ZeroDivisionError, OverflowError):
            continue
        if not all(math.isfinite(x) for x in (z, *g)):
            continue
        z = max(-8.0, min(8.0, z))
        p = min(max(_model.ndtr(z), _P_EPS), 1.0 - _P_EPS)
        phi = _model.norm_pdf(z)
        lam = phi / (p * (1.0 - p))
        resid = (1.0 if up else 0.0) - p
        for i in range(n):
            score[i] += resid * lam * g[i]
            for j in range(n):
                info[i][j] += phi * lam * g[i] * g[j]
        ll += math.log(p if up else 1.0 - p)
        used += 1
    if used:
        score = [s / used for s in score]
        info = [[x / used for x in row] for row in info]
        ll /= used
    return score, info, ll, used


def _slot_loglik(results: Sequence[tuple[float, bool]], rho: float) -> float:
    table = _sizing.outcome_probabilities([p for p, _ in results], rho)
    key = tuple(bool(up) for _, up in results)
    return math.log(max(table.get(key, 0.0), 1e-300))


def rho_score(slot: SlotEvidence, rho: float) -> tuple[float, float]:
    """(score, Fisher information) of rho for one slot's pattern of results, by central
    differences of the copula's pattern probabilities."""
    ps = [min(max(p, 1e-6), 1.0 - 1e-6) for p, _ in slot.results]
    lo_b, hi_b = BOUNDS["rho"]
    h = 1e-4
    lo, hi = max(lo_b, rho - h), min(hi_b, rho + h)
    t_lo = _sizing.outcome_probabilities(ps, lo)
    t_hi = _sizing.outcome_probabilities(ps, hi)
    t_0 = _sizing.outcome_probabilities(ps, rho)
    key = tuple(bool(up) for _, up in slot.results)
    fisher = 0.0
    for k, p0 in t_0.items():
        if p0 > 1e-300:
            dp = (t_hi.get(k, 0.0) - t_lo.get(k, 0.0)) / (hi - lo)
            fisher += dp * dp / p0
    p_obs = max(t_0.get(key, 0.0), 1e-300)
    dp_obs = (t_hi.get(key, 0.0) - t_lo.get(key, 0.0)) / (hi - lo)
    return dp_obs / p_obs, fisher


def learn(params: Mapping[str, Any], windows: Sequence[WindowEvidence],
          slots: Sequence[SlotEvidence] = ()) -> LearnResult:
    """One recursive step per window (then per slot for rho), from ``params`` (a dials
    version's params with its learner state). Pure; raises ValueError on dials without a
    learner state."""
    state = params.get(STATE_KEY)
    if not isinstance(state, Mapping) or list(state.get("names") or ()) != list(LEARNED):
        raise ValueError("these dials carry no learner state (the prior is never updated)")
    new = {k: v for k, v in params.items() if k != STATE_KEY}
    dials = _dials_floats(new)
    n = len(LEARNED)
    info = [[float(x) for x in row] for row in state["info"]]
    prior_mean, prior_info = _prior_of(state)
    rho_info = float(state.get("rho_info") or 0.0)
    before = dict(dials)
    result = LearnResult(params={})
    lls = []
    for ev in windows:
        score, fisher, ll, used = window_score(ev, dials)
        if not used:
            result.skipped.append(f"{ev.window_slug}: no row the model could price")
            continue
        info = [[GAMMA * info[i][j] + fisher[i][j] for j in range(n)] for i in range(n)]
        # The Newton step on the forgotten evidence plus the fixed prior (module docstring).
        pull = [score[i] - (1.0 - GAMMA) * prior_info[i] * (dials[name] - prior_mean[i])
                for i, name in enumerate(LEARNED)]
        total = [[info[i][j] + (prior_info[i] if i == j else 0.0) for j in range(n)]
                 for i in range(n)]
        step = _solve(total, pull)
        for name, s in zip(LEARNED, step):
            dials[name] = _bounded(name, dials[name], s)
        result.windows += 1
        lls.append(ll)
    used_slots = [float(x) for x in state.get("rho_slots") or ()]
    for slot in slots:
        if len(slot.results) < 2 or slot.slot_start in used_slots:
            continue
        score, fisher = rho_score(slot, dials["rho"])
        rho_info = GAMMA_RHO * rho_info + fisher
        if rho_info > 0.0:
            dials["rho"] = _bounded("rho", dials["rho"], score / rho_info)
        used_slots.append(slot.slot_start)
        result.slots += 1
    for key in (*LEARNED, "rho"):
        new[key] = dials[key]
        if dials[key] != before[key]:
            result.moved[key] = dials[key] - before[key]
    new[STATE_KEY] = {
        "names": list(LEARNED), "info": info,
        "prior": {"mean": prior_mean, "info": prior_info}, "rho_info": rho_info,
        "windows": int(state.get("windows") or 0) + result.windows,
        "slots": int(state.get("slots") or 0) + result.slots,
        "rho_slots": used_slots[-MAX_SLOTS_KEPT:],
    }
    result.params = new
    result.loglik = sum(lls) / len(lls) if lls else None
    return result


# ---------------------------------------------------------------------------------------------
# From the ledger: settled windows in, a new dials version out
# ---------------------------------------------------------------------------------------------


@dataclass
class LearnReport:
    """What one learning step did, for the card."""

    windows: int = 0
    slots: int = 0
    skipped: int = 0  # settled windows with no row the model could price (switch off...)
    version: int | None = None
    note: str | None = None
    errors: list[str] = field(default_factory=list)


def _asset_of(slug: str) -> str:
    return slug.split("-", 1)[0].lower()


def _sibling(slug: str, asset: str) -> str:
    return f"{asset}-{slug.split('-', 1)[1]}" if "-" in slug else slug


def _states(rows: Sequence[Mapping[str, Any]]) -> list[ModelState]:
    out = []
    for row in rows:
        inputs = row.get("inputs")
        if not isinstance(inputs, Mapping) or inputs.get("status") != "ok":
            continue
        try:
            out.append(_decide.state_from_record(inputs))
        except (NoDecision, ValueError, TypeError, KeyError):
            continue
    return sorted(out, key=lambda s: s.ts)


def _marginal(states: Sequence[ModelState], dials: Mapping[str, float]) -> float | None:
    """The anchored p at the row nearest minute 2 of its window."""
    for state in sorted(states, key=lambda s: abs((s.ts - s.window_start) - MINUTE_2_S)):
        try:
            return _decide.evaluate(state, dials).p
        except (NoDecision, ValueError, ZeroDivisionError, OverflowError):
            continue
    return None


async def learn_from_settled(settled: Sequence[Any], *, ts: float) -> LearnReport:
    """Learn from windows the bookkeeping just settled (``ledger.Settlement`` values), and
    store the result as a new dials version. Never raises."""
    report = LearnReport()
    try:
        if not settled:
            return report
        current = await _ledger.dials()
        if current is None:
            return report
        params = dict(current.get("params") or {})
        if not isinstance(params.get(STATE_KEY), Mapping):
            report.note = "The current dials are the prior, which does not learn."
            return report
        rows_by_window: dict[str, list[dict]] = {}

        async def rows_of(slug: str) -> list[dict]:
            """One window's rows, by its slug (indexed), read once per step."""
            if slug not in rows_by_window:
                rows_by_window[slug] = await _ledger.decisions_for_window(slug)
            return rows_by_window[slug]

        windows: list[WindowEvidence] = []
        slot_starts: dict[float, str] = {}
        for s in settled:
            slug, outcome = str(s.window_slug), str(s.outcome)
            if outcome not in ("Up", "Down"):
                continue
            asset = _asset_of(slug)
            states = _states([r for r in await rows_of(slug) if r.get("window_slug") == slug])
            window = await _ledger.get_window(slug)
            start = float(window["window_start"]) if window else (
                states[0].window_start if states else math.nan)
            windows.append(WindowEvidence(slug, asset, start, outcome, tuple(states)))
            if math.isfinite(start):
                slot_starts.setdefault(start, slug)

        dials = _decide.resolve_dials(params)
        slots: list[SlotEvidence] = []
        for start, slug in slot_starts.items():
            results: list[tuple[float, bool]] = []
            complete = True
            for asset in ASSETS:
                sib = _sibling(slug, asset)
                window = await _ledger.get_window(sib)
                if window is None:
                    continue  # this coin was not seen in that slot
                if window.get("outcome") not in ("Up", "Down"):
                    complete = False  # a coin still to settle: learn rho when it does
                    break
                p = _marginal(_states([r for r in await rows_of(sib)
                                       if r.get("window_slug") == sib]), dials)
                if p is not None:
                    results.append((p, window["outcome"] == "Up"))
            if complete and len(results) >= 2:
                slots.append(SlotEvidence(start, tuple(results)))

        result = await asyncio.to_thread(learn, params, windows, slots)
        report.skipped = len(result.skipped)
        if not result.windows and not result.slots:
            return report
        names = ", ".join(f"{w.asset.upper()} {w.outcome}" for w in windows if w.states)
        moved = ", ".join(f"{k} {v:+.4g}" for k, v in result.moved.items()) or "nothing moved"
        report.note = (f"Learned from {result.windows} settled window(s) ({names})"
                       + (f" and {result.slots} slot(s) of coins for the correlation"
                          if result.slots else "") + f": {moved}.")
        report.version = await _ledger.save_dials(
            result.params, source=LIVE_SOURCE, ts=ts, n_windows=result.windows,
            loglik=result.loglik, note=report.note)
        report.windows, report.slots = result.windows, result.slots
    except Exception as exc:  # noqa: BLE001 - learning must never stop the loop
        message = f"Learning from settled windows failed: {type(exc).__name__}: {exc}"
        report.errors.append(message)
        log.warning("fade1h.learner_failed", error=f"{type(exc).__name__}: {exc}")
    return report
