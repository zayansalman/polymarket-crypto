"""Learned chances (polymarket_bot.learning) and the strategies that use them.

Pure math, a temporary SQLite tick journal, and the shadow wiring. No network.
"""
from __future__ import annotations

import math
import random
import sqlite3
from pathlib import Path

import pytest

import config as _config
from polymarket_bot import learning
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot.shadow import learned as learned_signals
from polymarket_bot.shadow.types import SnapshotView
from polymarket_bot.strategy import StrategyParams


@pytest.fixture(autouse=True)
def _no_fit():
    learning._set_current(None)
    yield
    learning._set_current(None)


def _logit(p: float) -> float:
    return math.log(p / (1 - p))


def _synthetic(n_windows: int, seed: int = 7) -> list[learning.Sample]:
    """An overconfident model (logit x2) and a near-calibrated, noisy market."""
    rng = random.Random(seed)
    out: list[learning.Sample] = []
    for w in range(n_windows):
        true_up = min(0.97, max(0.03, rng.random()))
        up = 1.0 if rng.random() < true_up else 0.0
        for _ in range(rng.randint(5, 40)):  # uneven tick counts per window
            drift = rng.gauss(0, 0.3)
            model = 1 / (1 + math.exp(-2.0 * (_logit(true_up) + drift)))
            market = 1 / (1 + math.exp(-(_logit(true_up) + rng.gauss(0, 0.4))))
            out.append(learning.Sample(("5m", 1_000_000 + 300 * w), model, market, up))
    return out


# ------------------------------------------------------------------- math


def test_market_mid_from_both_asks() -> None:
    assert learning.market_mid_up(0.70, 0.31) == pytest.approx(0.695)
    assert learning.market_mid_up(0.70, None) is None
    assert learning.market_mid_up(0.50, 0.40) is None  # crossed book
    assert learning.market_mid_up(1.0, 0.02) is None  # no real ask


def test_blend_reduces_to_either_input() -> None:
    assert learning.blend(0.9, 0.6, 1.0, 0.0) == pytest.approx(0.9)
    assert learning.blend(0.9, 0.6, 0.0, 1.0) == pytest.approx(0.6)
    assert 0.6 < learning.blend(0.9, 0.6, 0.5, 0.5) < 0.9


def test_fit_learns_to_distrust_an_overconfident_model() -> None:
    samples = _synthetic(400)
    a, b = learning.fit_weights(samples)
    assert 0.0 < a < 0.8  # the model's logit is shrunk, not trusted at face value
    assert b > 0.2  # the market carries information too
    bm, bq, bl = learning.brier_scores(samples, a, b)
    assert bl < bm and bl <= bq + 1e-9


def test_compute_fit_needs_enough_windows() -> None:
    fit = learning.compute_fit(_synthetic(40))
    assert not fit.active
    assert "needs 100" in fit.reason
    assert learning.adjust(0.9, 0.8, 0.21) is None


def test_compute_fit_activates_only_when_the_holdout_improves() -> None:
    fit = learning.compute_fit(_synthetic(300), now=123)
    assert fit.active and fit.fitted_at == 123
    assert fit.holdout_windows == 60
    assert fit.brier_learned < fit.brier_model
    # If the newest windows show a model that is confident AND right, the
    # correction learned on older windows would hurt there: stay on raw math.
    older = _synthetic(240)
    newest = []
    for w in range(240, 300):
        up = float(w % 2)
        for _ in range(10):
            newest.append(learning.Sample(("5m", 1_000_000 + 300 * w),
                                          0.95 if up else 0.05, 0.6 if up else 0.4, up))
    fit = learning.compute_fit(older + newest)
    assert not fit.active
    assert "not more accurate" in fit.reason


def test_fit_round_trips_through_json() -> None:
    fit = learning.compute_fit(_synthetic(300))
    assert learning.Fit.from_json(fit.to_json()) == fit
    assert learning.Fit.from_json("not json") is None
    assert learning.Fit.from_json(None) is None


def test_adjust_uses_only_an_active_fit() -> None:
    learning._set_current(learning.Fit(0.4, 0.5, False, "x", 0, 300, 60, None, None, None))
    assert learning.adjust(0.97, 0.92, 0.09) is None
    learning._set_current(learning.Fit(0.4, 0.5, True, "x", 0, 300, 60, None, None, None))
    assert learning.adjust(0.97, 0.92, 0.09) == pytest.approx(
        learning.blend(0.97, 0.915, 0.4, 0.5)
    )
    assert learning.adjust(0.97, None, 0.09) is None  # one-sided book: no learned chance


def test_refit_schedule() -> None:
    learning._last_attempt, learning._last_failed = 0.0, False
    assert learning.refit_due(now=1_000)  # nothing yet: fit now
    learning._set_current(learning.Fit(0.4, 0.5, True, "x", 1_000, 300, 60, None, None, None))
    assert not learning.refit_due(now=1_000 + 1_800)  # a resumed fit is still fresh
    assert learning.refit_due(now=1_000 + 3_600)
    learning._last_attempt = 5_000
    assert not learning.refit_due(now=5_000 + 3_599)
    assert learning.refit_due(now=5_000 + 3_600)
    learning._last_failed = True
    assert learning.refit_due(now=5_000 + 600)
    learning._last_attempt, learning._last_failed = 0.0, False


def test_status_explains_the_correction() -> None:
    assert learning.status(None)["state"] == "waiting"
    fit = learning.Fit(0.372, 0.44, True, "ok", 0, 285, 57, 0.177, 0.184, 0.175)
    data = learning.status(fit)
    assert data["state"] == "active"
    assert data["example_learned"] < data["example_market"]  # 97% raw at 92c -> below 92%


# ---------------------------------------------------------- tick journal


def _journal(path: Path, windows: list[tuple[int, float, list[tuple[int, float, float, float]]]],
             known: dict[str, str] | None = None) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE paper_ticks (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, "
        "window_slug TEXT, remaining_seconds INTEGER, fair_up_prob REAL, up_best_ask REAL, "
        "down_best_ask REAL, reference_price REAL)"
    )
    con.execute(
        "CREATE TABLE model_shadow_positions (window_slug TEXT, outcome TEXT, state TEXT)"
    )
    for start, ref, ticks in windows:
        slug = f"btc-updown-5m-{start}"
        for remaining, fair, up_ask, down_ask in ticks:
            con.execute(
                "INSERT INTO paper_ticks(created_at, window_slug, remaining_seconds, fair_up_prob, "
                "up_best_ask, down_best_ask, reference_price) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("t", slug, remaining, fair, up_ask, down_ask, ref),
            )
    for slug, outcome in (known or {}).items():
        con.execute("INSERT INTO model_shadow_positions VALUES (?, ?, 'settled')", (slug, outcome))
    con.commit()
    con.close()


def test_load_samples_labels_windows_from_the_next_reference(tmp_path: Path) -> None:
    db = tmp_path / "ticks.db"
    tick = [(200, 0.8, 0.70, 0.31), (30, 0.9, 0.85, 0.16)]  # the 30s tick is too late to trade
    _journal(db, [
        (1_000_200, 100.0, tick),      # next ref 101 >= 100 -> Up
        (1_000_500, 101.0, tick),      # next ref 101 >= 101 -> Up (ties credit Up)
        (1_000_800, 101.0, tick),      # next ref says Down, but a known outcome wins
        (1_001_100, 99.0, tick),       # no next window: labeled only because it is known
        (1_002_000, 50.0, [(200, 0.6, 0.55, 0.40)]),  # crossed book -> no sample
    ], known={"btc-updown-5m-1001100": "Up", "btc-updown-5m-1000800": "Up",
              "btc-updown-5m-1002000": "Down"})
    samples = learning.load_samples(db)
    by_window = {s.window[1]: s.up for s in samples}
    assert by_window == {1_000_200: 1.0, 1_000_500: 1.0, 1_000_800: 1.0, 1_001_100: 1.0}
    assert all(s.model == pytest.approx(0.8) for s in samples)  # only the >60s tick


def test_load_samples_keeps_the_newest_windows(tmp_path: Path) -> None:
    db = tmp_path / "ticks.db"
    tick = [(200, 0.8, 0.70, 0.31)]
    _journal(db, [(1_000_000 + 300 * i, 100.0 + i, tick) for i in range(6)])
    windows = {s.window[1] for s in learning.load_samples(db, max_windows=3)}
    assert windows == {1_000_000 + 300 * i for i in (2, 3, 4)}


# ------------------------------------------------------- learned strategies


def _view(remaining: int, fair_up: float, up_ask: float, down_ask: float) -> SnapshotView:
    return SnapshotView(
        window_slug="btc-updown-5m-1", remaining_seconds=remaining, spot=100.0, reference=100.0,
        up_ask=up_ask, down_ask=down_ask, market_up_price=up_ask, fair_up=fair_up,
        sigma_per_second=None, feed_source="test", quote_source="test",
    )


def _frozen() -> StrategyParams:
    return StrategyParams(
        min_trade_usd=1.0, max_trade_usd=5.0, entry_edge_min=0.045, min_confidence=0.5,
        entry_min_remaining_seconds=60, entry_edge_max=0.07, min_entry_price=0.5,
    )


def _trust_model_fit(active: bool = True) -> learning.Fit:
    # a=1, b=0: learned == raw model, so the edges below are easy to reason about.
    return learning.Fit(1.0, 0.0, active, "test", 0, 300, 60, 0.16, 0.17, 0.15)


def test_learned_strategies_need_a_fit_but_not_an_adopted_one() -> None:
    view = _view(120, 0.80, 0.74, 0.27)
    assert learned_signals.learned_late_v1(view, _frozen()) is None  # never fitted
    learning._set_current(learning.Fit(0.0, 0.0, False, "needs 100 finished windows (has 40)",
                                       0, 40, 0, None, None, None))
    assert learned_signals.learned_late_v1(view, _frozen()) is None  # no real weights yet
    # A fit the main bot rejected is still tested by the shadow candidates.
    learning._set_current(_trust_model_fit(active=False))
    assert learning.adjust(0.80, 0.74, 0.27) is None  # main bot: not adopted
    assert learning.adjust(0.80, 0.74, 0.27, require_active=False) == pytest.approx(0.80)
    assert learned_signals.learned_late_v1(view, _frozen()) is not None


def test_wide_strategies_drop_the_edge_cap() -> None:
    view = _view(200, 0.86, 0.74, 0.27)  # 12c raw edge: over the frozen 7c cap
    assert learned_signals.raw_wide_v1(view, _frozen()) is not None  # no fit needed
    assert learned_signals.learned_wide_v1(view, _frozen()) is None  # no fit yet
    learning._set_current(_trust_model_fit(active=False))
    sig = learned_signals.learned_wide_v1(view, _frozen())
    assert sig is not None and sig.side == "Up" and sig.edge == pytest.approx(0.12)
    # 3.5c raw edge at 50c: the fee (1.75c) leaves 1.75c, under raw_wide's 3c minimum
    assert learned_signals.raw_wide_v1(_view(200, 0.535, 0.50, 0.51), _frozen()) is None


def test_learned_late_only_enters_late_and_after_fees() -> None:
    learning._set_current(_trust_model_fit())
    # edge 6c on a 74c share; fee is about 1.35c, so 4.65c after fees >= 4.5c
    sig = learned_signals.learned_late_v1(_view(120, 0.80, 0.74, 0.27), _frozen())
    assert sig is not None and sig.side == "Up" and sig.entry_price == 0.74
    assert learned_signals.learned_late_v1(_view(200, 0.80, 0.74, 0.27), _frozen()) is None
    # 5c raw edge at 50c: fee 1.75c leaves 3.25c, below the 4.5c minimum
    assert learned_signals.learned_late_v1(_view(120, 0.55, 0.50, 0.51), _frozen()) is None


def test_learned_mid_only_buys_60_to_90_cent_shares() -> None:
    learning._set_current(_trust_model_fit())
    assert learned_signals.learned_mid_v1(_view(200, 0.80, 0.74, 0.27), _frozen()) is not None
    # 6.5c edge, about 4.8c after the fee: only the 57c price rules it out
    assert learned_signals.learned_mid_v1(_view(200, 0.635, 0.57, 0.44), _frozen()) is None
    assert learned_signals.learned_mid_v1(_view(200, 0.98, 0.92, 0.09), _frozen()) is None


def test_roster_lists_the_new_candidates_and_the_live_rule_twin() -> None:
    from polymarket_bot.shadow import runner

    for model_id in ("bot_raw_twin_v1", "learned_late_v1", "learned_mid_v1", "raw_wide_v1",
                     "learned_wide_v1"):
        assert model_id in runner.MODEL_IDS and model_id in runner.MODEL_LABELS
    assert runner.LIVE_RULE_MODELS == {"bot_raw_twin_v1"}


def test_shadow_roster_ignores_the_settings_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    from polymarket_bot import paper

    monkeypatch.setitem(_knobs._cache, "paper_entry_edge_min", 0.2)
    monkeypatch.setitem(_knobs._cache, "paper_max_entry_price", 0.6)
    monkeypatch.setitem(_knobs._cache, "paper_fee_aware", True)
    frozen = paper._shadow_params()
    assert frozen.entry_edge_min == _config.PAPER_ENTRY_EDGE_MIN
    assert frozen.max_entry_price == 0.95 and frozen.fee_rate == 0.0
    live = paper._strategy_params()
    assert live.entry_edge_min == 0.2 and live.max_entry_price == 0.6 and live.fee_rate > 0


def test_twin_gets_the_live_rules_and_the_roster_keeps_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from polymarket_bot.paper import PaperSnapshot
    from polymarket_bot.shadow import runner

    recorded: list[str] = []

    async def _capture(**kwargs):
        recorded.append(kwargs["model_id"])

    monkeypatch.setattr(runner.ledger, "record_shadow_signal", _capture)
    snap = PaperSnapshot(
        created_at="2026-11-18T09:00:00+00:00", window_slug="btc-updown-5m-1",
        market_question="q", remaining_seconds=200, spot_price=100.0, reference_price=100.0,
        sigma_per_second=0.0001, market_up_price=0.74, market_down_price=0.27,
        fair_up_prob=0.80, edge=0.06, signal_side=None, confidence=0.0, notional_usd=0.0,
        reason="", feed_source="test", up_token_id="u", down_token_id="d",
        up_best_bid=0.73, up_best_ask=0.74, up_bid_size=100, up_ask_size=100,
        down_best_bid=0.26, down_best_ask=0.27, down_bid_size=100, down_ask_size=100,
        quote_source="clob",
    )
    strict = StrategyParams(
        min_trade_usd=1.0, max_trade_usd=5.0, entry_edge_min=0.2, min_confidence=0.5,
        entry_min_remaining_seconds=60, entry_edge_max=0.3, min_entry_price=0.5,
    )
    asyncio.run(runner.record_shadow(snap, _frozen(), live_params=strict))
    assert "pricing_v0" in recorded  # 6c edge passes the frozen 4.5c rule
    assert "bot_raw_twin_v1" not in recorded  # the live rule wants 20c


# ------------------------------------------------- the paper decision path


def _snapshot(monkeypatch: pytest.MonkeyPatch, *, learning_on: bool, allow: bool = True):
    """Build one paper snapshot with every network read faked."""
    import asyncio
    from types import SimpleNamespace

    from polymarket_bot import paper

    now = 1_800_000_000
    start = now - 100  # 200 s left in a 5-minute window

    async def _market(client, now_, asset="btc", timeframe="5m"):
        return {"window_start_ts": start, "slug": f"btc-updown-5m-{start}", "question": "q"}

    books = {"up": paper.BookTop(0.73, 0.74, 100.0, 100.0), "down": paper.BookTop(0.26, 0.27, 100.0, 100.0)}

    async def _book(client, token_id):
        return books[token_id]

    async def _ref(client, slug, start_ts):
        return 100.0

    async def _sigma(client, closes):
        return 0.0001, "test"

    async def _selection():
        return SimpleNamespace(asset="btc", timeframe="5m")

    monkeypatch.setattr(paper, "_now", lambda: now)
    monkeypatch.setattr(paper, "_fetch_current_market", _market)
    monkeypatch.setattr(paper, "_fetch_clob_book", _book)
    monkeypatch.setattr(paper, "_outcome_prices", lambda m: (0.74, 0.27))
    monkeypatch.setattr(paper, "_outcome_token_ids", lambda m: ("up", "down"))
    monkeypatch.setattr(paper, "_chainlink_spot_and_closes", lambda: (100.0, [100.0, 100.0]))
    monkeypatch.setattr(paper, "_get_window_reference", _ref)
    monkeypatch.setattr(paper, "_sigma_with_fallback", _sigma)
    monkeypatch.setattr(paper, "fair_up_probability", lambda *a, **k: 0.80)
    monkeypatch.setattr(paper._market_selection, "get_selection", _selection)
    monkeypatch.setitem(_knobs._cache, "paper_learning", learning_on)
    return asyncio.run(paper._build_snapshot(None, allow_paper_strategy=allow))


def test_paper_decision_uses_learned_chances_when_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # Raw model 80% vs a 74c ask = 6c edge: an entry on the raw math.
    raw = _snapshot(monkeypatch, learning_on=True)
    assert raw.decision_up_prob is None and raw.signal_side == "Up"

    learning._set_current(learning.Fit(0.372, 0.44, True, "test", 0, 285, 57, None, None, None))
    snap = _snapshot(monkeypatch, learning_on=True)
    expected = learning.blend(0.80, 0.735, 0.372, 0.44)
    assert snap.decision_up_prob == pytest.approx(expected)
    assert snap.fair_up_prob == pytest.approx(0.80)  # the journal keeps the raw model
    assert snap.edge == pytest.approx(max(expected - 0.74, (1 - expected) - 0.27))
    assert snap.signal_side is None  # corrected chance sits below the ask: no trade

    off = _snapshot(monkeypatch, learning_on=False)
    assert off.decision_up_prob is None and off.signal_side == "Up"
    live = _snapshot(monkeypatch, learning_on=True, allow=False)  # live path: never learned
    assert live.decision_up_prob is None



# ------------------------------------------------- trading-based adoption


def test_trading_replay_decides_when_available() -> None:
    samples = _synthetic(300)
    better = {"ok": True, "windows": 240, "learned_usd": 42.5, "raw_usd": -54.2,
              "learned_n": 171, "raw_n": 232}
    fit = learning.compute_fit(samples, now=5, trading=better)
    assert fit.active and fit.trade_learned_usd == 42.5 and fit.trade_raw_n == 232
    assert "+$42.50" in fit.reason and "-$54.20" in fit.reason
    assert (fit.a_model, fit.b_market) == pytest.approx(learning.fit_weights(samples))  # all-data fit
    worse = dict(better, learned_usd=-5.0, raw_usd=3.0)
    assert not learning.compute_fit(samples, trading=worse).active
    # No usable replay: the accuracy check decides, as before.
    assert learning.compute_fit(samples, trading={"ok": False}).trade_windows is None


def test_saved_fits_from_before_the_replay_still_load() -> None:
    old = ('{"a_model": 0.37, "active": false, "b_market": 0.39, "brier_learned": 0.16, '
           '"brier_market": 0.17, "brier_model": 0.15, "fitted_at": 1, "holdout_windows": 95, '
           '"reason": "x", "windows": 472}')
    fit = learning.Fit.from_json(old)
    assert fit is not None and fit.trade_windows is None
    assert learning.Fit.from_json(fit.to_json()) == fit


def test_current_rules_follow_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(_knobs._cache, "paper_entry_edge_min", 0.03)
    monkeypatch.setitem(_knobs._cache, "paper_entry_edge_max", 0.20)
    monkeypatch.setitem(_knobs._cache, "paper_fee_aware", True)
    rules = learning.current_rules()
    assert (rules.edge_min, rules.edge_max, rules.fee_aware, rules.shares) == (0.03, 0.20, True, 5.0)
