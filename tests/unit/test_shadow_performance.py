"""Unit tests for the shadow forward-tester performance CLI (tools/shadow_performance.py).

Two layers:

1. The stdlib stats helpers (Wilson score interval, one-sided binomial tail)
   against hand-checked reference values and their structural properties.
2. The aggregator + report run against a tiny temp SQLite database seeded
   with two models, so the read path, per-model grouping, fee-net ROI, and
   EDGE? flagging are all exercised end-to-end without any network or live DB.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tools.shadow_performance import (
    binomial_sf_ge,
    breakeven_winrate,
    format_report,
    load_settled_rows,
    run,
    wilson_interval,
)

# ---------------------------------------------------------------------------
# Schema mirrors the model_shadow_positions contract (LEDGER agent owns the
# canonical CREATE TABLE in db.py; this is an independent copy for an isolated
# temp DB so the test never touches the live database).
# ---------------------------------------------------------------------------

_SHADOW_TABLE = """
CREATE TABLE model_shadow_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT,
  window_slug TEXT,
  model_id TEXT,
  side TEXT,
  entry_price REAL,
  notional_usd REAL,
  shares REAL,
  fair_prob REAL,
  edge REAL,
  confidence REAL,
  reason TEXT,
  state TEXT,
  outcome TEXT,
  settlement_price REAL,
  resolved_at TEXT,
  realized_pnl_usd REAL,
  quote_source TEXT,
  feed_source TEXT
);
CREATE UNIQUE INDEX idx_shadow_window_model
  ON model_shadow_positions(window_slug, model_id);
"""


def _net_pnl(entry: float, shares: float, won: bool) -> float:
    """Settled net PnL the LEDGER would store: binary payoff minus entry fee."""
    fee = 0.07 * entry * (1.0 - entry)
    payoff = (1.0 - entry) if won else (-entry)
    return (payoff - fee) * shares


def _seed_db(path: Path) -> None:
    """Two models: a strong winner and a coin-flip, all settled."""
    conn = sqlite3.connect(path)
    conn.executescript(_SHADOW_TABLE)

    rows: list[tuple] = []

    def add(model_id: str, slug: str, side: str, entry: float, shares: float, won: bool):
        # The ledger stores the winning SIDE in `outcome`: a row won iff its
        # own side is the winning side.
        outcome = side if won else ("Down" if side == "Up" else "Up")
        rows.append(
            (
                "2026-06-18T00:00:00+00:00",
                slug,
                model_id,
                side,
                entry,
                entry * shares,  # notional_usd = price * shares
                shares,
                0.6,
                0.05,
                0.7,
                "enter",
                "settled",
                outcome,
                61_000.0,
                "2026-06-18T00:05:00+00:00",
                _net_pnl(entry, shares, won),
                "clob",
                "chainlink",
            )
        )

    # model_strong: 9 wins / 10 at entry 0.50 — well above the ~0.5175
    # fee-adjusted breakeven; should trip EDGE?.
    for i in range(9):
        add("model_strong", f"w-strong-{i}", "Up", 0.50, 10.0, True)
    add("model_strong", "w-strong-9", "Up", 0.50, 10.0, False)

    # model_flip: 5 wins / 10 at entry 0.50 — right at coin-flip, below
    # breakeven net of fees; must NOT trip EDGE?.
    for i in range(5):
        add("model_flip", f"w-flip-{i}", "Down", 0.50, 10.0, True)
    for i in range(5, 10):
        add("model_flip", f"w-flip-{i}", "Down", 0.50, 10.0, False)

    # An OPEN row that must be ignored by the settled-only read path.
    rows.append(
        (
            "2026-06-18T00:06:00+00:00",
            "w-open-0",
            "model_flip",
            "Up",
            0.50,
            5.0,
            10.0,
            0.6,
            0.05,
            0.7,
            "enter",
            "open",
            None,
            None,
            None,
            None,
            "clob",
            "chainlink",
        )
    )

    conn.executemany(
        """
        INSERT INTO model_shadow_positions (
          created_at, window_slug, model_id, side, entry_price, notional_usd,
          shares, fair_prob, edge, confidence, reason, state, outcome,
          settlement_price, resolved_at, realized_pnl_usd, quote_source, feed_source
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Wilson score interval
# ---------------------------------------------------------------------------


def test_wilson_known_value_5_of_10() -> None:
    """Textbook 95% Wilson interval for 5/10 is (0.2366, 0.7634)."""
    low, high = wilson_interval(5, 10)
    assert low == pytest.approx(0.2366, abs=1e-4)
    assert high == pytest.approx(0.7634, abs=1e-4)
    # Symmetric around 0.5 for a balanced count.
    assert (low + high) == pytest.approx(1.0, abs=1e-9)


def test_wilson_known_value_80_of_100() -> None:
    """95% Wilson interval for 80/100 is approximately (0.7112, 0.8666)."""
    low, high = wilson_interval(80, 100)
    assert low == pytest.approx(0.7112, abs=1e-4)
    assert high == pytest.approx(0.8666, abs=1e-4)


def test_wilson_empty_sample_is_degenerate() -> None:
    assert wilson_interval(0, 0) == (0.0, 0.0)


def test_wilson_bounds_stay_in_unit_interval() -> None:
    """Even at the extremes the interval never leaves [0, 1] (Wald would)."""
    for wins, n in [(0, 5), (5, 5), (1, 3), (99, 100)]:
        low, high = wilson_interval(wins, n)
        assert 0.0 <= low <= high <= 1.0


def test_wilson_narrows_with_more_data() -> None:
    """Same proportion, more trials → tighter interval."""
    w_small = wilson_interval(5, 10)
    w_large = wilson_interval(50, 100)
    assert (w_large[1] - w_large[0]) < (w_small[1] - w_small[0])


# ---------------------------------------------------------------------------
# One-sided binomial tail
# ---------------------------------------------------------------------------


def test_binomial_known_value_8_of_10_at_half() -> None:
    """P(X >= 8 | n=10, p=0.5) = 56/1024 = 0.0546875."""
    assert binomial_sf_ge(8, 10, 0.5) == pytest.approx(0.0546875, abs=1e-9)


def test_binomial_full_and_empty_tails() -> None:
    assert binomial_sf_ge(0, 10, 0.5) == pytest.approx(1.0, abs=1e-12)
    assert binomial_sf_ge(11, 10, 0.5) == pytest.approx(0.0, abs=1e-12)


def test_binomial_monotonic_decreasing_in_k() -> None:
    """For fixed n, p0 the upper-tail p-value is non-increasing as k rises."""
    ps = [binomial_sf_ge(k, 10, 0.5) for k in range(0, 11)]
    assert all(ps[i] >= ps[i + 1] for i in range(len(ps) - 1))
    # Strictly decreasing across this range (no ties for the binomial(10, .5)).
    assert ps[0] > ps[-1]


def test_binomial_empty_sample() -> None:
    assert binomial_sf_ge(1, 0, 0.5) == 1.0


# ---------------------------------------------------------------------------
# breakeven_winrate (fallback or canonical) honours the fee contract
# ---------------------------------------------------------------------------


def test_breakeven_winrate_matches_fee_contract() -> None:
    """w* = p + 0.07*p*(1-p): a 0.50 side must win > 51.75% net of fees."""
    assert breakeven_winrate(0.50) == pytest.approx(0.5175, abs=1e-9)
    assert breakeven_winrate(0.40) == pytest.approx(0.4168, abs=1e-9)
    # Fees always raise the bar above the price-implied breakeven.
    for p in (0.2, 0.5, 0.8):
        assert breakeven_winrate(p) > p


# ---------------------------------------------------------------------------
# End-to-end: aggregate + report against a seeded temp DB
# ---------------------------------------------------------------------------


def test_report_runs_against_seeded_db(tmp_path: Path) -> None:
    db_path = tmp_path / "shadow.db"
    _seed_db(db_path)

    rows = load_settled_rows(db_path)
    # 20 settled rows (10 + 10); the single OPEN row is excluded.
    assert len(rows) == 20

    stats = run(db_path)
    assert len(stats) == 2
    by_id = {s.model_id: s for s in stats}
    assert set(by_id) == {"model_strong", "model_flip"}

    strong = by_id["model_strong"]
    flip = by_id["model_flip"]

    # Counts and win-rates.
    assert strong.n == 10 and strong.wins == 9
    assert flip.n == 10 and flip.wins == 5
    assert strong.win_rate == pytest.approx(0.9)
    assert flip.win_rate == pytest.approx(0.5)

    # Average entry 0.50 → fee-adjusted breakeven 0.5175 for both.
    assert strong.avg_entry == pytest.approx(0.50)
    assert strong.breakeven == pytest.approx(0.5175, abs=1e-6)

    # Net ROI is below gross ROI for both — the fee always drags.
    assert strong.net_roi < strong.gross_roi
    assert flip.net_roi < flip.gross_roi

    # The flip model loses net of fees (5/10 < 51.75% breakeven); strong wins.
    assert strong.net_roi > 0
    assert flip.net_roi < 0

    # avg net pnl/share: strong = mean of [9 wins, 1 loss] net per share.
    # win net/sh = (1-0.5) - 0.07*0.25 = 0.5 - 0.0175 = 0.4825
    # loss net/sh = -0.5 - 0.0175 = -0.5175
    expected_strong_pps = (9 * 0.4825 + 1 * -0.5175) / 10
    assert strong.avg_net_pnl_per_share == pytest.approx(expected_strong_pps, abs=1e-9)

    # EDGE? flag: strong's Wilson lower bound clears 0.5175; flip's does not.
    assert strong.edge is True
    assert flip.edge is False

    # Sorted by net ROI descending → strong first.
    assert stats[0].model_id == "model_strong"

    # Binomial p: strong (9/10 vs breakeven) is small; flip (5/10) is large.
    assert strong.binomial_p < 0.05
    assert flip.binomial_p > 0.20

    # Report renders without error and surfaces the flag + both models.
    report = format_report(stats)
    assert "model_strong" in report
    assert "model_flip" in report
    assert "EDGE?" in report


def test_report_empty_db(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_SHADOW_TABLE)
    conn.commit()
    conn.close()

    stats = run(db_path)
    assert stats == []
    assert "no settled shadow positions" in format_report(stats)


def test_missing_db_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_settled_rows(tmp_path / "nope.db")
