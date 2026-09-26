"""Unit tests for tools/fade_1h_momentum_15m/explain.py: the factor-by-factor explanation of
one Fade 1h Momentum on 15m decision.

The waterfall must reproduce model.prob_up exactly, its Shapley split must not depend on the
order the parts are added, and a row with the momentum switched off (theta = 0) must give the
momentum no share of the probability.

The words must match the maths: the stretch is a weighted average (never 'add up to'), sigma
is a swing size (never '% an hour'), a split between Binance and the settlement is said out loud,
a bid 1c under the last trade is not 'capped', and a bid-search miss sits in its own note.
"""
from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from tools.fade_1h_momentum_15m import explain as ex_mod  # noqa: E402
from tools.fade_1h_momentum_15m import model as fm  # noqa: E402

PARAMS = {"theta": -0.0434, "kappa0": 0.345, "lam": -1.62, "alpha": 0.978, "c": 0.0093}
T0 = 1789685100  # 2026-09-17 22:45 UTC, the 4th quarter of the hour


def _row(**over) -> dict:
    row = {
        "coin": "btc", "t0": T0, "minute": 2, "y": 0.0011, "x": 0.0030, "sigma": 0.0045,
        "prev": [0.0024, -0.0008, 0.0012, 0.0003, -0.0015, 0.0006, 0.0001, -0.0002, 0.0009, -0.0004,
                 0.0002, 0.0005],
        "mu_L": 0.0041, "m_H": 0.58, "vH": 0.6 * 0.0045 ** 2, "vL": 1.1 * 0.0045 ** 2,
        "cHL": 0.3 * 0.0045 ** 2, "market_price_up": 0.55, "quote_up": 0.56, "quote_dn": 0.46,
        "ask_up": 0.57, "ask_dn": 0.45, "later_min_up": 0.30, "later_max_up": 0.80, "up": 1.0,
    }
    row.update(over)
    return row


def _model_inputs(row, prm=PARAMS):
    """The same inputs explain() feeds the model, from model.py's own functions."""
    t = ((row["t0"] % 3600) + 60 * row["minute"]) / 3600.0
    h = (15 - row["minute"]) / 60.0
    M = fm.stretch(np.asarray(row["prev"]), prm["alpha"], prm["c"])
    mu_H = fm.mu_hat_H(row["m_H"], row["x"], row["sigma"], t)
    mu, v = fm.blend(mu_H, fm.mu_hat_L(row["mu_L"]), row["vH"], row["vL"], row["cHL"])
    return dict(y=row["y"], M=float(M), mu=float(mu), v=max(float(v), 0.0), t=t, h=h, sigma=row["sigma"],
                theta=prm["theta"], kappa0=prm["kappa0"], lam=prm["lam"])


ROWS = [
    _row(),
    _row(minute=0, y=0.0, t0=T0 - 2700),  # top of the hour, window just opened
    _row(y=-0.0021, m_H=0.31, x=-0.0012, mu_L=-0.0060),  # down leg, crowd pricing more downside
    _row(m_H=float("nan")),  # no 1h print: spot momentum alone
    _row(prev=[0.012, 0.009, -0.004] + [0.0] * 9, minute=5),  # big stretch, strong snap-back
]


@pytest.mark.parametrize("row", ROWS)
def test_contributions_sum_to_p_minus_one_half(row):
    ex = ex_mod.explain(row, PARAMS, maker=False)
    total = sum(ex.contributions.values())
    assert total == pytest.approx(ex.decision["p_up"] - 0.5, abs=1e-9)
    assert ex.waterfall[-1]["p_up"] == pytest.approx(ex.decision["p_up"], abs=1e-12)
    assert ex.waterfall[-2]["p_up"] == pytest.approx(ex.decision["p_up"], abs=1e-9)  # the running total lands on p


@pytest.mark.parametrize("row", ROWS)
def test_explain_p_equals_model_prob_up(row):
    ex = ex_mod.explain(row, PARAMS, maker=False)
    p = float(fm.prob_up(**_model_inputs(row)))
    assert ex.decision["p_up"] == p
    assert ex.decision["p_down"] == pytest.approx(1.0 - p, abs=1e-15)
    assert ex.decision["side"] == ("Up" if p >= 0.5 else "Down")


@pytest.mark.parametrize("row", ROWS)
def test_shapley_is_order_free(row):
    """The subset formula equals the average of marginal contributions over all 6 orders."""
    ex = ex_mod.explain(row, PARAMS, maker=False)
    value = ex_mod.coalition_value(**_model_inputs(row))
    by_orders = ex_mod.shapley_by_orders(value, ex_mod.PARTS)
    assert len(list(__import__("itertools").permutations(ex_mod.PARTS))) == 6
    for part in ex_mod.PARTS:
        assert float(by_orders[part]) == pytest.approx(ex.contributions[part], abs=1e-12)
    # and the formula itself gives the same values whatever order the players are listed in
    rev = ex_mod.shapley(value, tuple(reversed(ex_mod.PARTS)))
    for part in ex_mod.PARTS:
        assert float(rev[part]) == pytest.approx(ex.contributions[part], abs=1e-15)


def test_zero_theta_row_has_zero_momentum_contribution():
    prm = {**PARAMS, "theta": 0.0}
    ex = ex_mod.explain(_row(m_H=0.80, mu_L=0.02), prm, maker=False)  # strong momentum, switched off
    assert ex.contributions["momentum"] == 0.0
    assert ex.inputs["momentum_push"] == 0.0
    assert ex.inputs["theta_reads"] == "off"
    assert sum(ex.contributions.values()) == pytest.approx(ex.decision["p_up"] - 0.5, abs=1e-9)


def test_stretch_parts_add_up_to_the_models_stretch():
    row = _row()
    ex = ex_mod.explain(row, PARAMS, maker=False)
    M = float(fm.stretch(np.asarray(row["prev"]), PARAMS["alpha"], PARAMS["c"]))
    assert sum(ex.inputs["candle_stretch_parts"]) == pytest.approx(M, abs=1e-15)
    assert ex.inputs["stretch"] == M


def test_vectorised_contributions_match_single_rows():
    rows = [_model_inputs(r) for r in ROWS]
    arr = {k: np.array([r[k] for r in rows]) for k in ("y", "M", "mu", "v", "t", "h", "sigma")}
    C = ex_mod.contributions(**arr, theta=PARAMS["theta"], kappa0=PARAMS["kappa0"], lam=PARAMS["lam"])
    for n, row in enumerate(ROWS):
        one = ex_mod.explain(row, PARAMS, maker=False).contributions
        for part in ex_mod.PARTS:
            assert float(C[part][n]) == pytest.approx(one[part], abs=1e-15)


def test_taker_decision_uses_model_break_even_and_limit():
    row = _row()
    ex = ex_mod.explain(row, PARAMS, maker=False)
    d = ex.decision
    side_p = d["p_side"]
    assert d["taker"]["break_even"] == pytest.approx(float(fm.taker_break_even(side_p)), abs=1e-15)
    q = row["quote_up"] if d["side"] == "Up" else row["quote_dn"]
    assert d["taker"]["decided"] == (q < d["taker"]["break_even"])
    if d["taker"]["filled"]:
        assert d["taker"]["fill_price"] <= d["taker"]["break_even"] + 1e-12
        win = 1.0 if (row["up"] >= 0.5) == (d["side"] == "Up") else 0.0
        a = d["taker"]["fill_price"]
        assert ex.outcome["taker_pnl"] == pytest.approx(win - a - float(fm.taker_fee(a)), abs=1e-15)


def test_story_has_five_to_eight_sentences_and_no_banned_words():
    for row in ROWS:
        ex = ex_mod.explain(row, PARAMS, maker={"b": 0.5, "P_fill": 0.8, "p_fill": 0.55, "J": 0.04,
                                                "kelly": 0.1})
        sents = ex.sentences()
        assert 5 <= len(sents) <= 8
        text = (ex.story() + " ".join(ex.summary())).lower()
        for word in ("coin flip", "coin-flip", "luck", "gambl"):
            assert word not in text


def test_story_lines_are_leg_snap_momentum_trade_and_summary_is_short():
    for row in ROWS:
        ex = ex_mod.explain(row, PARAMS, maker={"b": 0.5, "P_fill": 0.8, "p_fill": 0.55, "J": 0.04,
                                                "kelly": 0.1})
        lines = ex.story_lines()
        assert [g for g, _ in lines] == ["Leg", "Snap-back", "Momentum", "Trade"]
        assert all(text for _, text in lines)
        assert 2 <= len(ex.summary()) <= 3


@pytest.mark.parametrize("x, text", [(0.002, "adds only 0.2 points to Up"), (-0.002, "takes only 0.2 points off Up"),
                                     (0.0001, "under 0.1 points"), (0.03, "adds 3.0 points to Up"),
                                     (-0.03, "takes 3.0 points off Up")])
def test_small_contributions_keep_their_direction(x, text):
    assert text in ex_mod._moves_up(x)


@pytest.mark.parametrize("x, m_H, alone, text", [
    (-0.0006, 0.61, 0.382, "the drop to be more than won back, with the hour finishing Up"),  # example (c)
    (0.0011, 0.54, 0.633, "part of the rise to be given back"),  # example (a): 54c between 63c and 50c
    (0.0011, 0.45, 0.633, "the rise to be more than given back, with the hour finishing Down"),
    (-0.0006, 0.45, 0.382, "part of the drop to be won back"),
    (-0.0006, 0.50, 0.382, "all of the drop to be won back"),
    (-0.0006, 0.28, 0.311, "the drop to extend"),
    (0.0, 0.58, 0.50, "the hour to go up"),
])
def test_crowd_reading_is_on_the_right_side_of_50c(x, m_H, alone, text):
    assert text in ex_mod.crowd_reading(x, m_H, alone)


def test_down_hour_with_1h_market_over_50c_reads_as_hour_finishing_up():
    ex = ex_mod.explain(_row(x=-0.0006, m_H=0.61), PARAMS, maker=False)
    story = ex.story()
    assert "more than won back" in story and "finishing Up" in story
    assert "part of the drop" not in story


def _stuck_at_one_cent(price_now, *args, **kw):
    return {"b": np.array(0.01), "P_fill": np.array(0.3), "p_fill": np.array(0.009),
            "J": np.array(-3e-4), "kelly": np.array(0.0)}


def test_bid_search_miss_is_flagged_not_reported_as_no_bid(monkeypatch):
    """A local search that stops at 1c while a bid one tick under the price has J > 0 (the Brent miss
    on example c): explain() must flag it in its own note and never say that no bid is worth resting.
    The miss is a gap in the paper book's search, so it stays out of the summary and the headline."""
    monkeypatch.setattr(ex_mod.fm, "maker_best_bid", _stuck_at_one_cent)
    ex = ex_mod.explain(_row(), PARAMS)
    mk = ex.decision["maker"]
    price_j = mk["price"]
    assert not mk["quoted"]
    assert mk["search_missed"]
    g = mk["grid"]
    assert g["J"] > 0 and g["top"] == pytest.approx(price_j - 0.01, abs=1e-9)
    # the grid's best bid is a real maker_fill evaluation at that bid
    P, pf = fm.maker_fill(g["b"], *[_model_inputs(_row())[k] for k in ("y", "t", "h", "sigma")],
                          float(fm.implied_drift(0.55, 0.0011, 0.0045, 13 / 60)),
                          *[_model_inputs(_row())[k] for k in ("M", "mu", "v", "theta", "kappa0", "lam")])
    assert g["J"] == pytest.approx(float(P * (pf - g["b"])), abs=2e-4)
    note = ex.bid_search_note()
    assert note is not None and "missed" in note and ex_mod.cents(g["b"]) in note
    text = ex.story() + " ".join(ex.summary())
    assert "no bid has" not in text and "no bid is worth" not in text
    assert "missed" not in " ".join(ex.summary())
    assert ex.outcome["missed_bid_pnl"] == pytest.approx(1.0 - g["b"])  # later_min_up 0.30 < bid; Up won


def test_headline_does_not_lead_with_a_bid_search_miss(monkeypatch):
    examples = pytest.importorskip("tools.fade_1h_momentum_15m.examples")
    monkeypatch.setattr(ex_mod.fm, "maker_best_bid", _stuck_at_one_cent)
    ex = ex_mod.explain(_row(), PARAMS)
    title = examples.headline_title(ex)
    assert "search" not in title and "missed" not in title
    assert title.endswith("no trade") or "buys" in title


def test_no_bid_worth_resting_says_why(monkeypatch):
    """With J <= 0 at every 1c bid, p_fill <= b at every bid: the story says so in a trader's words."""
    monkeypatch.setattr(ex_mod.fm, "maker_best_bid", _stuck_at_one_cent)
    monkeypatch.setattr(ex_mod, "bid_grid", lambda *a, **k: {"b": 0.01, "P_fill": 0.3, "p_fill": 0.009,
                                                             "J": -3e-4, "kelly": 0.0, "top": 0.54, "J_top": -0.02})
    ex = ex_mod.explain(_row(), PARAMS)
    assert not ex.decision["maker"]["quoted"] and not ex.decision["maker"]["search_missed"]
    trade = dict(ex.story_lines())["Trade"]
    assert "fills only after Up has fallen to it" in trade and "at or below the bid" in trade
    assert ex.bid_search_note() is None


def test_stretch_reads_as_weighted_average_not_a_sum():
    """The stretch is sum_j w_j c tanh(r_j / c) with weights adding up to 1: a weighted average."""
    row = _row()
    ex = ex_mod.explain(row, PARAMS, maker=False)
    w = np.asarray(ex.inputs["candle_weights"])
    raw = np.arange(1, 13, dtype=float) ** -PARAMS["alpha"]
    assert w == pytest.approx(raw / raw.sum(), abs=1e-12)
    assert w.sum() == pytest.approx(1.0, abs=1e-12)
    capped = PARAMS["c"] * np.tanh(np.asarray(row["prev"]) / PARAMS["c"])
    assert np.asarray(ex.inputs["candle_stretch_parts"]) == pytest.approx(w * capped, abs=1e-15)
    for r in ROWS:
        text = ex_mod.explain(r, PARAMS, maker=False).story()
        assert "add up to" not in text and "added up" not in text
        assert "average a stretch of" in text
        assert f"carries {100 * w[0]:.0f}% of the weight" in text


def test_sigma_is_a_swing_size_not_a_trend():
    """'% an hour' is kept for drifts; sigma is worded as the size of a typical swing, up or down."""
    for row in ROWS:
        ex = ex_mod.explain(row, PARAMS, maker=False)
        text = ex.story() + " ".join(ex.summary())
        sig = f"{100 * row['sigma']:.3f}%"
        assert f"{sig} an hour" not in text
        assert "is moving" not in text
        assert f"typical one-hour swing of {sig} (up or down" in text


def test_feed_split_is_said_out_loud():
    split = ex_mod.explain(_row(up=0.0, binance_up=1.0), PARAMS, maker=False)
    assert split.outcome["feeds_split"] and split.outcome["binance_winner"] == "Up"
    last = split.summary()[-1]
    assert "Binance, which the maths reads, closed Up" in last and "Chainlink, which settles the market, closed Down" in last
    same = ex_mod.explain(_row(up=1.0, binance_up=1.0), PARAMS, maker=False)
    assert not same.outcome["feeds_split"]
    assert "split" not in same.summary()[-1]
    assert "both closed Up" in ex_mod.feeds_text(same.outcome)
    bare = ex_mod.explain(_row(), PARAMS, maker=False)  # no Binance direction on the row: nothing claimed
    assert bare.outcome["binance_winner"] is None and ex_mod.feeds_text(bare.outcome) == ""


def test_bid_one_tick_under_the_last_trade_is_not_called_capped():
    row = _row()
    ex = ex_mod.explain(row, PARAMS, maker={"b": row["market_price_up"] - 0.01, "P_fill": 0.9, "p_fill": 0.57,
                                            "J": 0.02, "kelly": 0.05})
    mk = ex.decision["maker"]
    assert mk["quoted"] and mk["on_upper_bound"]
    trade = dict(ex.story_lines())["Trade"]
    assert "1c under the last trade" in trade and "capped" not in trade
    # the story's trade line stays short: fill chance, mark-down and Kelly live in the decision table
    assert "Kelly" not in trade and "fill chance" not in trade


def test_intro_reads_from_the_parameters():
    examples = pytest.importorskip("tools.fade_1h_momentum_15m.examples")
    stats = {"n": 1071, "mom_mean": 0.0035, "mom_max": 0.026, "mom_flips": 6, "lead": [971, 96, 4],
             "feed_split": 55, "quotes": 521, "quotes_on_bound": 521, "maker_unknown": 0, "maker_live": 0}
    text = examples.intro(PARAMS, stats, 2, 2)
    raw = np.arange(1, 13, dtype=float) ** -PARAMS["alpha"]
    assert "weighted average of the last twelve 15m candles, not their sum" in text
    assert f"newest candle carries about {100 * raw[0] / raw.sum():.0f}% of the weight" in text
    assert f"soft-capped at c ({100 * PARAMS['c']:.2f}%" in text
    assert "cannot dominate" not in text and "added up" not in text
    assert "against the momentum by 4.3% of what the momentum alone would carry" in text
    assert "55 of 1,071 rows" in text and "Chainlink" in text
    assert "one bid per window, with no scaled child orders" in text
    assert "Every resting bid in these cards sits there" in text and "all 521 of step 2's 521" in text
    assert "1 of the 2 resting bids in these cards sit there" in examples.intro(PARAMS, stats, 2, 1)
    assert "go with the momentum" in examples.intro({**PARAMS, "theta": 0.05}, stats, 0, 0)


def test_bid_search_that_quotes_the_best_bid_is_not_flagged():
    ex = ex_mod.explain(_row(), PARAMS)
    mk = ex.decision["maker"]
    assert mk["quoted"] and not mk["search_missed"]
    assert mk["grid"]["J"] <= mk["J"] + ex_mod.MISS_TOL
