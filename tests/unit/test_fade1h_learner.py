"""Fade 1h Momentum on 15m: the live learner (recursive maximum likelihood with forgetting)."""

from __future__ import annotations

import math
import random
from pathlib import Path

import pytest
import pytest_asyncio

from ems import db as _db
from ems.fade_1h_momentum_15m import decide as D
from ems.fade_1h_momentum_15m import ledger
from ems.fade_1h_momentum_15m import learner as L
from tests.unit.test_fade1h_decide import END, START, make

STATE_ARGS = dict(spot=100.2, up_bid=0.39, up_ask=0.41)  # the leg is up, the market says Down


def window(outcome: str, n_rows: int = 3, asset: str = "btc",
           **kw) -> L.WindowEvidence:  # noqa: ANN003
    args = {**STATE_ARGS, **kw}
    states = tuple(D.state_from_inputs(make(asset, now=START + 60 + 120 * i, **args))
                   for i in range(n_rows))
    return L.WindowEvidence(f"{asset}-updown-15m-{START}", asset, float(START), outcome, states)


def varied_states(rng: random.Random, n: int) -> list[D.ModelState]:
    """Model states across the range the app sees: the price against the start, the book,
    the time left, the volatility, the last three hours and the 1h market."""
    out = []
    for _ in range(n):
        mid = min(0.95, max(0.05, rng.gauss(0.5, 0.18)))
        bid = round(mid - 0.01, 2)
        inp = make(now=START + rng.choice([60, 120, 240, 420, 600, 780]), up_bid=bid,
                   up_ask=round(bid + 0.02, 2), spot=100.0 * math.exp(rng.gauss(0, 0.0015)),
                   vol=rng.choice([0.0004, 0.0008, 0.0015]),
                   r15=tuple(rng.gauss(0, 0.002) for _ in range(12)),
                   hour_up=min(0.95, max(0.05, rng.gauss(0.5, 0.2))))
        out.append(D.state_from_inputs(inp))
    return out


def run_windows(seed: int, n: int, chance) -> dict:  # noqa: ANN001
    """Learn from ``n`` settled windows, four a step as the runner sends them, each settling
    Up with ``chance(state)``. Returns the dials at the end."""
    rng = random.Random(seed)
    pool = varied_states(rng, 300)
    chances = [chance(state) for state in pool]
    params = L.starting_params()
    for step in range(n // 4):
        evidence = []
        for i in range(4):
            k = rng.randrange(len(pool))
            outcome = "Up" if rng.random() < chances[k] else "Down"
            evidence.append(L.WindowEvidence(f"btc-w{step}-{i}", "btc", float(START), outcome,
                                             (pool[k],)))
        params = L.learn(params, evidence).params
    return params


def fit_se(name: str) -> float:
    if name in ("w_M", "w_S"):
        return L.STARTING_FIT[f"{name}_se"]
    return L._PROCESS_SE[name]


# ---------------------------------------------------------------------------
# The pure update
# ---------------------------------------------------------------------------


def test_the_prior_is_never_updated() -> None:
    with pytest.raises(ValueError, match="prior"):
        L.learn(dict(ledger.PRIOR_DIALS), [window("Up")])


def test_the_starting_state_matches_the_fits() -> None:
    state = L.starting_learner_state()
    info = state["info"]
    assert state["names"] == list(L.LEARNED)
    # The anchor block is the fit's own information (its 1,071 windows fit in the memory).
    assert info[5][5] == pytest.approx(115.32561373553072)
    assert info[6][6] == pytest.approx(125.78175726247159)
    # The process fit is the fixed prior at its full strength, so the forgotten part of the
    # information does not count it again.
    assert all(info[i][j] == 0.0 for i in range(5) for j in range(7))
    prior = state["prior"]
    assert prior["mean"] == [L.STARTING_DIALS[name] for name in L.LEARNED]
    assert prior["info"][0] == pytest.approx(1 / 0.021680485029106394 ** 2)
    assert prior["info"][4] == pytest.approx(1 / 0.003426019096080245 ** 2)
    assert prior["info"][5:] == [0.0, 0.0]  # the anchor weights are free to learn
    assert 700 < L.MEMORY_WINDOWS < 800 * 1.5
    assert state["rho_info"] > 0 and state["windows"] == 0


def test_one_window_barely_moves_the_dials_and_moves_them_toward_the_evidence() -> None:
    params = L.starting_params()
    # The model said Up (the leg is up), the market said Down, and it settled Up.
    result = L.learn(params, [window("Up")])
    assert result.windows == 1 and result.loglik is not None
    moved = result.params
    assert moved["w_S"] > params["w_S"] and moved["w_M"] < params["w_M"]
    for name in L.LEARNED:
        assert abs(moved[name] - params[name]) <= L.MAX_STEP[name] + 1e-12
    # One window moves it a small part of the starting fit's own uncertainty.
    assert abs(moved["w_S"] - params["w_S"]) < 0.25 * L.STARTING_FIT["w_S_se"]
    # And the other way when the market was right.
    back = L.learn(params, [window("Down")]).params
    assert back["w_S"] < params["w_S"] and back["w_M"] > params["w_M"]


def test_a_day_of_consistent_disagreement_moves_the_dials_a_lot_and_stays_bounded() -> None:
    params = L.starting_params()
    for _ in range(12):  # a day of windows, in passes of 32
        params = L.learn(params, [window("Up", n_rows=1) for _ in range(32)]).params
    assert params["w_S"] - L.STARTING_DIALS["w_S"] > 0.1
    for name in (*L.LEARNED, "rho"):
        lo, hi = L.BOUNDS[name]
        assert lo <= params[name] <= hi
    assert params[L.STATE_KEY]["windows"] == 12 * 32
    # Extreme evidence cannot push a dial past its bounds.
    params = {**params, "w_S": 2.99}
    for _ in range(5):
        params = L.learn(params, [window("Up", n_rows=1) for _ in range(40)]).params
    assert params["w_S"] <= L.BOUNDS["w_S"][1]


def test_the_dials_stay_near_the_truth_when_it_does_not_move() -> None:
    # Every window settles Up with the anchored chance at the starting dials, so the truth is
    # the starting dials. Settled 15m results say little about how the price moves: without
    # the fixed prior, theta and lam drifted more than ten of the fit's standard errors here.
    truth = D.resolve_dials(dict(L.STARTING_DIALS))
    params = run_windows(1, 1200, lambda state: D.evaluate(state, truth).p)
    for name in L.LEARNED:
        drift = abs(params[name] - L.STARTING_DIALS[name]) / fit_se(name)
        assert drift < (1.0 if name in L._PROCESS_SE else 2.0), (name, drift)
        lo, hi = L.BOUNDS[name]
        assert lo <= params[name] <= hi


def test_when_the_market_is_right_the_anchor_learns_it_and_the_process_dials_hold() -> None:
    params = run_windows(3, 1200, lambda state: state.m)  # settles with the market's chance
    assert params["w_M"] > L.STARTING_DIALS["w_M"] + 0.2
    assert params["w_S"] < L.STARTING_DIALS["w_S"] - 0.2
    for name in L._PROCESS_SE:
        assert abs(params[name] - L.STARTING_DIALS[name]) < fit_se(name), name


def test_old_information_decays_and_each_window_adds_its_own() -> None:
    params = L.starting_params()
    ev = window("Up")
    dials = D.resolve_dials({k: v for k, v in params.items() if k != L.STATE_KEY})
    _, fisher, _, used = L.window_score(ev, dials)
    assert used == 3
    old = params[L.STATE_KEY]["info"]
    new = L.learn(params, [ev]).params[L.STATE_KEY]["info"]
    for i in range(len(L.LEARNED)):
        for j in range(len(L.LEARNED)):
            assert new[i][j] == pytest.approx(L.GAMMA * old[i][j] + fisher[i][j], abs=1e-12)
    # A window the model could not price adds nothing and forgets nothing.
    flat = L.WindowEvidence("btc-x", "btc", float(START), "Up",
                            (D.state_from_inputs(make(vol=0.0)),))
    assert L.learn(params, [flat]).params[L.STATE_KEY]["info"] == old


def test_the_step_cap_holds_when_the_information_is_thin() -> None:
    params = L.starting_params()
    state = params[L.STATE_KEY]
    # Almost nothing remembered about the anchor: the uncapped step would be enormous.
    thin = {**state, "info": [[x * 1e-9 for x in row] for row in state["info"]]}
    moved = L.learn({**params, L.STATE_KEY: thin}, [window("Up")]).params
    assert abs(moved["w_S"] - params["w_S"]) == pytest.approx(L.MAX_STEP["w_S"])
    for name in L.LEARNED:
        assert abs(moved[name] - params[name]) <= L.MAX_STEP[name] + 1e-12
    # At a bound, the step stops there.
    edge = L.learn({**params, "w_S": L.BOUNDS["w_S"][1] - 0.01, L.STATE_KEY: thin},
                   [window("Up")]).params
    assert edge["w_S"] == L.BOUNDS["w_S"][1]


def test_a_process_dial_away_from_its_fit_is_pulled_back() -> None:
    params = {**L.starting_params(), "theta": L.STARTING_DIALS["theta"] + 0.06}  # ~3 SE off
    distances = []
    for _ in range(20):
        params = L.learn(params, [window("Up", n_rows=1), window("Down", n_rows=1)]).params
        distances.append(abs(params["theta"] - L.STARTING_DIALS["theta"]))
    assert distances[-1] < 0.06 and distances == sorted(distances, reverse=True)


def test_a_state_saved_before_the_fixed_prior_gets_the_starting_one() -> None:
    params = L.starting_params()
    old_state = {k: v for k, v in params[L.STATE_KEY].items() if k != "prior"}
    got = L.learn({**params, L.STATE_KEY: old_state}, [window("Up")]).params
    assert got[L.STATE_KEY]["prior"] == L.starting_prior()
    assert got["w_S"] == pytest.approx(L.learn(params, [window("Up")]).params["w_S"])


def test_rho_moves_with_how_the_coins_settle_together() -> None:
    params = L.starting_params()
    together = [L.SlotEvidence(float(START + 900 * i), ((0.5, True),) * 4) for i in range(20)]
    apart = [L.SlotEvidence(float(START + 900 * i), ((0.5, True), (0.5, False)) * 2)
             for i in range(20)]
    up = L.learn(params, [], together).params
    down = L.learn(params, [], apart).params
    assert up["rho"] > params["rho"] > down["rho"]
    assert abs(up["rho"] - params["rho"]) <= 20 * L.MAX_STEP["rho"] + 1e-12
    # A slot is counted once.
    again = L.learn(up, [], together[:3])
    assert again.slots == 0 and again.params["rho"] == up["rho"]


def test_rows_the_model_cannot_price_are_skipped() -> None:
    flat = L.WindowEvidence("btc-x", "btc", float(START), "Up",
                            (D.state_from_inputs(make(vol=0.0)),))
    result = L.learn(L.starting_params(), [flat])
    assert result.windows == 0 and result.skipped
    assert result.params["w_S"] == L.STARTING_DIALS["w_S"]


# ---------------------------------------------------------------------------
# From the ledger
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fade_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "fade.db")
    await _db.init_db()
    return _db


async def record_window(asset: str, outcome: str, **kw) -> ledger.Settlement:  # noqa: ANN003
    slug = f"{asset}-updown-15m-{START}"
    await ledger.upsert_window(window_slug=slug, asset=asset, window_start=START,
                               window_end=END, ts=START, condition_id=f"0x{asset}",
                               up_token=f"UP-{asset}", down_token=f"DN-{asset}")
    for i in range(3):
        inp = make(asset, now=START + 60 + 120 * i, **{**STATE_ARGS, **kw})
        record = inp.as_record()
        record["settings"] = {"spot_feed": "chainlink_twap60"}
        await ledger.record_decision(ts=inp.ts, asset=asset, inputs=record, action="no_bid",
                                     window_slug=slug, mode="paper", dials_version=1)
    settled = await ledger.settle_window(slug, outcome=outcome, ts=END + 30)
    assert settled is not None
    return settled


@pytest.mark.asyncio
async def test_the_starting_dials_are_seeded_once_after_the_prior(fade_db) -> None:
    first = await L.seed_starting_dials(ts=START)
    assert first["version"] == 1 and first["source"] == L.STARTING_SOURCE
    assert first["params"]["w_S"] == pytest.approx(L.STARTING_DIALS["w_S"])
    assert L.STATE_KEY in first["params"]
    again = await L.seed_starting_dials(ts=START + 60)
    assert again["version"] == 1
    history = await ledger.dial_history()
    assert [d["version"] for d in history] == [1, 0]
    assert history[1]["source"] == ledger.PRIOR_SOURCE


@pytest.mark.asyncio
async def test_settled_windows_become_a_new_live_dials_version(fade_db) -> None:
    await L.seed_starting_dials(ts=START)
    settled = [await record_window(a, "Up") for a in ("btc", "eth", "sol", "xrp")]
    report = await L.learn_from_settled(settled, ts=END + 60)
    assert report.errors == [] and report.windows == 4 and report.slots == 1
    newest = await ledger.dials()
    assert newest["version"] == report.version == 2 and newest["source"] == L.LIVE_SOURCE
    assert newest["n_windows"] == 4
    assert newest["params"]["w_S"] > L.STARTING_DIALS["w_S"]
    assert newest["params"][L.STATE_KEY]["windows"] == 4
    assert newest["params"][L.STATE_KEY]["rho_slots"] == [float(START)]
    assert "BTC Up" in newest["note"]
    # The new dials still price a window.
    D.decide(make(**STATE_ARGS), newest["params"])


@pytest.mark.asyncio
async def test_each_window_learns_only_from_its_own_rows(fade_db, monkeypatch) -> None:
    await L.seed_starting_dials(ts=START)
    settled = await record_window("btc", "Up")
    # The coin's next window has rows too (a different market), and has not settled.
    nxt = f"btc-updown-15m-{END}"
    await ledger.upsert_window(window_slug=nxt, asset="btc", window_start=END,
                               window_end=END + 900, ts=END)
    for i in range(4):
        inp = make("btc", now=START + 60 + 120 * i, spot=99.5, up_bid=0.2, up_ask=0.22)
        record = {**inp.as_record(), "window_slug": nxt}
        await ledger.record_decision(ts=END + 60 * i, asset="btc", inputs=record,
                                     action="no_order", window_slug=nxt, mode="paper",
                                     dials_version=1)
    seen: list = []
    real_learn = L.learn

    def spy(params, windows, slots=()):  # noqa: ANN001
        seen.extend(windows)
        return real_learn(params, windows, slots)

    monkeypatch.setattr(L, "learn", spy)
    report = await L.learn_from_settled([settled], ts=END + 60)
    assert report.windows == 1
    (ev,) = seen
    assert ev.window_slug == settled.window_slug and len(ev.states) == 3
    assert all(state.window_slug == settled.window_slug for state in ev.states)


@pytest.mark.asyncio
async def test_a_slot_waits_for_every_coin_before_rho_learns(fade_db) -> None:
    await L.seed_starting_dials(ts=START)
    btc = await record_window("btc", "Up")
    await ledger.upsert_window(window_slug=f"eth-updown-15m-{START}", asset="eth",
                               window_start=START, window_end=END, ts=START)
    report = await L.learn_from_settled([btc], ts=END + 60)
    assert report.windows == 1 and report.slots == 0  # ETH has not settled yet


@pytest.mark.asyncio
async def test_the_prior_does_not_learn_and_nothing_raises(fade_db, monkeypatch) -> None:
    await ledger.seed_dials(ts=START)  # the prior only
    settled = [await record_window("btc", "Up")]
    report = await L.learn_from_settled(settled, ts=END + 60)
    assert report.version is None and "prior" in report.note
    assert (await ledger.dials())["version"] == 0

    async def broken(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("database locked")

    monkeypatch.setattr(ledger, "dials", broken)
    report = await L.learn_from_settled(settled, ts=END + 60)
    assert report.errors and "database locked" in report.errors[0]
    assert await L.learn_from_settled([], ts=END) == L.LearnReport()


def test_the_learned_dials_stay_numbers() -> None:
    params = L.learn(L.starting_params(), [window("Up"), window("Down", asset="eth")]).params
    assert all(math.isfinite(float(params[k])) for k in (*L.LEARNED, "rho"))
    info = params[L.STATE_KEY]["info"]
    assert all(math.isfinite(x) for row in info for x in row)
    assert info[5][6] == pytest.approx(info[6][5])  # symmetric
