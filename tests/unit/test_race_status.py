"""Unit tests for the race-status CLI (tools/race_status.py).

Two layers, mirroring the test_shadow_performance.py approach:

1. The stdlib stats helpers (mean/std, z-interval, bootstrap, max drawdown,
   required-n) against hand-checked reference values and their structural
   properties.
2. The aggregators run against a tiny temp SQLite database seeded with two
   shadow models, a live book, and a config row — so the read path,
   per-model grouping, deploy-bar arithmetic, live-book totals, and
   bot-state / accruing logic are exercised end-to-end without any network,
   live DB, or bot interaction.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.race_status import (
    MIN_TICKS_PER_WINDOW,
    ModelStats,
    bootstrap_ci,
    breakeven_winrate,
    gather_bot_state,
    gather_live_book,
    gather_models,
    max_drawdown,
    mean_std,
    render_text,
    required_n_for_ci_gt_zero,
    t_ci,
)

# ---------------------------------------------------------------------------
# 1. Stats helpers
# ---------------------------------------------------------------------------


class TestStatsHelpers:
    def test_mean_std_basic(self) -> None:
        mu, sd = mean_std([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
        assert mu == pytest.approx(5.0)
        assert sd == pytest.approx(2.138, abs=1e-3)  # ddof=1

    def test_mean_std_degenerate(self) -> None:
        assert mean_std([]) == (0.0, 0.0)
        assert mean_std([3.0]) == (3.0, 0.0)  # no std with n<2

    def test_t_ci_straddles_zero_for_noisy_mean(self) -> None:
        lo, hi = t_ci([1.0, -1.0, 2.0, -2.0, 0.5])
        assert lo < 0 < hi  # near-zero mean, wide interval

    def test_t_ci_clears_zero_for_tight_positive(self) -> None:
        lo, hi = t_ci([1.0] * 30)  # zero variance → interval collapses to mean
        assert lo == pytest.approx(1.0)
        assert hi == pytest.approx(1.0)

    def test_bootstrap_ci_reproducible_and_ordered(self) -> None:
        xs = [0.5, -0.3, 1.2, -0.8, 0.4, 0.9, -0.1]
        a = bootstrap_ci(xs)
        b = bootstrap_ci(xs)
        assert a == b  # fixed seed → deterministic
        assert a[0] < a[1]

    def test_max_drawdown_is_nonpositive(self) -> None:
        assert max_drawdown([1.0, 1.0, 1.0]) == 0.0  # monotone up → no DD
        # +2, then -5 run to -3 trough from peak +2 → -5 drawdown.
        assert max_drawdown([2.0, -1.0, -2.0, -2.0]) == pytest.approx(-5.0)

    def test_required_n_positive_mean(self) -> None:
        # n > (1.96 * sd / mean)^2 = (1.96 * 2 / 0.5)^2 = 7.84^2 ≈ 61.5 → 62.
        assert required_n_for_ci_gt_zero(0.5, 2.0) == 62

    def test_required_n_none_for_nonpositive_mean(self) -> None:
        assert required_n_for_ci_gt_zero(0.0, 2.0) is None
        assert required_n_for_ci_gt_zero(-0.1, 2.0) is None
        assert required_n_for_ci_gt_zero(0.5, 0.0) is None

    def test_breakeven_winrate_exceeds_price(self) -> None:
        # Fee is strictly positive for 0<p<1, so breakeven > entry price.
        assert breakeven_winrate(0.54) == pytest.approx(0.54 + 0.07 * 0.54 * 0.46)
        assert breakeven_winrate(0.54) > 0.54


# ---------------------------------------------------------------------------
# 2. End-to-end against a temp DB
# ---------------------------------------------------------------------------

_SHADOW_TABLE = """
CREATE TABLE model_shadow_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT, window_slug TEXT, model_id TEXT, side TEXT,
  entry_price REAL, notional_usd REAL, shares REAL, fair_prob REAL,
  edge REAL, confidence REAL, reason TEXT, state TEXT, outcome TEXT,
  settlement_price REAL, resolved_at TEXT, realized_pnl_usd REAL,
  quote_source TEXT, feed_source TEXT
);
"""

_POSITIONS_TABLE = """
CREATE TABLE paper_positions (
  position_id INTEGER PRIMARY KEY AUTOINCREMENT,
  opened_at TEXT, closed_at TEXT, window_slug TEXT, side TEXT, state TEXT,
  entry_price REAL, exit_price REAL, notional_usd REAL, shares REAL,
  realized_pnl_usd REAL, mode TEXT, model_id TEXT
);
"""

_TICKS_TABLE = "CREATE TABLE paper_ticks (id INTEGER PRIMARY KEY, created_at TEXT);"

_CONFIG_TABLE = "CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);"

SINCE = "2026-07-02T14:50:50"


def _seed(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        _SHADOW_TABLE + _POSITIONS_TABLE + _TICKS_TABLE + _CONFIG_TABLE
    )

    def shadow(model_id: str, slug: str, created: str, pnl: float, px: float, state="settled"):
        conn.execute(
            "INSERT INTO model_shadow_positions "
            "(created_at, window_slug, model_id, side, entry_price, state, "
            "realized_pnl_usd) VALUES (?,?,?,?,?,?,?)",
            (created, slug, model_id, "Up", px, state, pnl),
        )

    # winner: 4 settled rows post-common-start, positive mean WITH variance
    # (so the deploy-bar required-n is a real finite number, not degenerate).
    for i, pnl in enumerate([2.5, 0.5, 2.0, 1.0]):  # mean +1.5, sd > 0
        shadow("winner", f"win-{i}", f"2026-07-03T0{i}:00:00+00:00", pnl, 0.5)
    # loser: 4 settled rows, negative mean.
    for i, pnl in enumerate([-1.5, -0.5, -1.0, -1.0]):
        shadow("loser", f"los-{i}", f"2026-07-03T0{i}:00:00+00:00", pnl, 0.5)
    # A row BEFORE the common start must be excluded from standings.
    shadow("winner", "pre-1", "2026-07-01T00:00:00+00:00", 99.0, 0.5)
    # An OPEN row must be excluded by the settled-only filter.
    shadow("winner", "open-1", "2026-07-03T09:00:00+00:00", 5.0, 0.5, state="open")

    # Live book: two days of real-money fills.
    for day, pnl in [("2026-07-06", 4.0), ("2026-07-07", -8.0)]:
        conn.execute(
            "INSERT INTO paper_positions "
            "(opened_at, mode, realized_pnl_usd, side, state, entry_price, "
            "notional_usd, shares) VALUES (?,?,?,?,?,?,?,?)",
            (f"{day}T00:00:00+00:00", "live", pnl, "Up", "closed", 0.5, 2.5, 5.0),
        )
    # A paper-mode row must NOT count toward the live book.
    conn.execute(
        "INSERT INTO paper_positions "
        "(opened_at, mode, realized_pnl_usd, side, state, entry_price, "
        "notional_usd, shares) VALUES (?,?,?,?,?,?,?,?)",
        ("2026-07-07T01:00:00+00:00", "paper", 3.0, "Up", "closed", 0.5, 2.5, 5.0),
    )

    conn.execute("INSERT INTO paper_ticks (created_at) VALUES ('2026-07-07T06:40:00+00:00')")
    for k, v in [
        ("polymarket_bot.mode", "paper"),
        ("polymarket_bot.state", "stopped"),
        ("polymarket_bot.updated_at", "2026-07-08T16:51:23+00:00"),
    ]:
        conn.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (?,?,?)",
            (k, v, "2026-07-08T16:51:23+00:00"),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    db = tmp_path / "race.db"
    _seed(db)
    return db


def _conn(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class TestGatherModels:
    def test_scopes_and_ranks(self, seeded_db: Path) -> None:
        conn = _conn(seeded_db)
        models = gather_models(conn, SINCE, today="2026-07-09")
        conn.close()
        by_id = {m.model_id: m for m in models}
        # Only settled, post-common-start rows: 4 each.
        assert by_id["winner"].n == 4
        assert by_id["loser"].n == 4
        # The pre-start +99 and the open +5 rows are excluded.
        assert by_id["winner"].total == pytest.approx(6.0)
        # Ranked by mean descending → winner first.
        assert models[0].model_id == "winner"

    def test_deploy_bar_fields(self, seeded_db: Path) -> None:
        conn = _conn(seeded_db)
        models = gather_models(conn, SINCE, today="2026-07-09")
        conn.close()
        by_id = {m.model_id: m for m in models}
        # winner has a positive mean → a finite required-n.
        assert by_id["winner"].req_n is not None
        # loser has a negative mean → never clears.
        assert by_id["loser"].req_n is None

    def test_today_delta_isolates_the_day(self, seeded_db: Path) -> None:
        conn = _conn(seeded_db)
        # All seeded rows are 07-03; "today" 07-03 should surface them.
        models = gather_models(conn, SINCE, today="2026-07-03")
        conn.close()
        by_id = {m.model_id: m for m in models}
        assert by_id["winner"].today_n == 4
        assert by_id["winner"].today_total == pytest.approx(6.0)


class TestGatherLiveBook:
    def test_live_only_and_totals(self, seeded_db: Path) -> None:
        conn = _conn(seeded_db)
        live = gather_live_book(conn)
        conn.close()
        assert live.n == 2  # paper row excluded
        assert live.total == pytest.approx(-4.0)  # +4 − 8
        assert live.by_day[-1] == ("2026-07-07", 1, -8.0)

    def test_missing_orders_table_yields_zero_split(self, seeded_db: Path) -> None:
        """The seeded DB has no live_orders — split degrades to zeroes."""
        conn = _conn(seeded_db)
        live = gather_live_book(conn)
        conn.close()
        assert (live.entries_matched, live.entries_rested) == (0, 0)


_LIVE_ORDERS_TABLE = """
CREATE TABLE live_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT, window_slug TEXT, token_id TEXT, intent TEXT, side TEXT,
  price REAL, size REAL, notional_usd REAL, order_type TEXT, status TEXT,
  clob_order_id TEXT, error TEXT, details_json TEXT, mode TEXT
);
"""


class TestPlacementSplit:
    """#137: maker/taker attribution from the journaled placement response."""

    def _db_with_orders(self, tmp_path: Path) -> Path:
        db = tmp_path / "orders.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            _SHADOW_TABLE + _POSITIONS_TABLE + _TICKS_TABLE + _CONFIG_TABLE
            + _LIVE_ORDERS_TABLE
        )
        import json as _json

        def order(status: str, placement: str | None, intent="ENTRY", mode="live"):
            details = (
                _json.dumps({"response": {"status": placement}})
                if placement
                else None
            )
            conn.execute(
                "INSERT INTO live_orders "
                "(created_at, intent, side, status, details_json, mode) "
                "VALUES ('2026-07-09T00:00:00+00:00',?,?,?,?,?)",
                (intent, "Up", status, details, mode),
            )

        order("SUBMITTED", "matched")
        order("SUBMITTED", "matched")
        order("SUBMITTED", "live")
        order("BLOCKED", None)              # no response → excluded
        order("SUBMITTED", "matched", intent="EXIT")   # exits excluded
        order("SUBMITTED", "matched", mode="paper")    # paper rows excluded
        conn.commit()
        conn.close()
        return db

    def test_counts_entry_placements_only(self, tmp_path: Path) -> None:
        db = self._db_with_orders(tmp_path)
        conn = _conn(db)
        live = gather_live_book(conn)
        conn.close()
        assert live.entries_matched == 2
        assert live.entries_rested == 1

    def test_render_shows_maker_share(self, tmp_path: Path) -> None:
        db = self._db_with_orders(tmp_path)
        conn = _conn(db)
        live = gather_live_book(conn)
        bot = gather_bot_state(conn)
        conn.close()
        out = render_text([], live, bot, SINCE, trades_per_day=9.0)
        assert "2 crossed (taker)" in out
        assert "1 rested (maker-eligible" in out
        assert "33% maker share" in out


class TestGatherBotState:
    def test_stopped_bot_not_accruing(self, seeded_db: Path) -> None:
        conn = _conn(seeded_db)
        bot = gather_bot_state(conn)
        conn.close()
        assert bot.mode == "paper"
        assert bot.state == "stopped"
        assert bot.accruing is False  # stopped → never accruing
        assert bot.last_tick == "2026-07-07T06:40:00+00:00"
        # A stopped bot's cadence is N/A — never flagged degraded (#157).
        assert bot.cadence_ok is True
        assert bot.ticks_last_10min == 0


def _cadence_db(tmp_path: Path, *, state: str, n_recent_ticks: int) -> Path:
    """Minimal DB with a config state and ``n_recent_ticks`` ticks in the last
    ~9 minutes (inside the 10-min cadence window)."""
    db = tmp_path / "cadence.db"
    conn = sqlite3.connect(db)
    conn.executescript(_TICKS_TABLE + _CONFIG_TABLE + _SHADOW_TABLE)
    now = datetime.now(timezone.utc)
    for i in range(n_recent_ticks):
        ts = (now - timedelta(seconds=5 * (i + 1))).isoformat()
        conn.execute("INSERT INTO paper_ticks (created_at) VALUES (?)", (ts,))
    for k, v in [("polymarket_bot.mode", "paper"), ("polymarket_bot.state", state)]:
        conn.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (?,?,?)",
            (k, v, now.isoformat()),
        )
    conn.commit()
    conn.close()
    return db


class TestTickCadence:
    """#157: surface a journaling stall the heartbeat watchdog cannot see."""

    def test_healthy_cadence_ok(self, tmp_path: Path) -> None:
        db = _cadence_db(tmp_path, state="running", n_recent_ticks=100)
        conn = _conn(db)
        bot = gather_bot_state(conn)
        conn.close()
        assert bot.ticks_last_10min == 100
        assert bot.cadence_ok is True
        assert bot.accruing is True

    def test_running_but_stalled_cadence_flagged(self, tmp_path: Path) -> None:
        """The 07-09 flap signature: alive (a recent tick) but journaling
        collapsed → accruing reads YES yet cadence is DEGRADED."""
        db = _cadence_db(
            tmp_path, state="running", n_recent_ticks=MIN_TICKS_PER_WINDOW - 1
        )
        conn = _conn(db)
        bot = gather_bot_state(conn)
        conn.close()
        assert bot.accruing is True  # a tick within 10min → still "alive"
        assert bot.cadence_ok is False  # but the trickle is flagged

    def test_stopped_bot_cadence_not_flagged(self, tmp_path: Path) -> None:
        """Zero recent ticks while stopped is expected, not a stall."""
        db = _cadence_db(tmp_path, state="stopped", n_recent_ticks=0)
        conn = _conn(db)
        bot = gather_bot_state(conn)
        conn.close()
        assert bot.cadence_ok is True

    def test_render_warns_on_journaling_stall(self, tmp_path: Path) -> None:
        db = _cadence_db(tmp_path, state="running", n_recent_ticks=3)
        conn = _conn(db)
        bot = gather_bot_state(conn)
        conn.close()
        out = render_text([], LiveBook_stub(), bot, SINCE, trades_per_day=9.0)
        assert "tick cadence: 3" in out
        assert "JOURNALING STALL" in out


def LiveBook_stub():  # noqa: N802 — tiny local stub, not a fixture
    from tools.race_status import LiveBook

    return LiveBook(n=0, total=0.0, by_day=[])


class TestDeployBarMinN:
    """A degenerate small sample must never print CLEARS NOW (f45 n=1 bug)."""

    def _model(self, n: int, t_lo: float) -> ModelStats:
        return ModelStats(
            model_id="m", n=n, total=2.2 * n, mean=2.2, sd=0.0,
            t_lo=t_lo, t_hi=2.2, boot_lo=t_lo, boot_hi=2.2,
            win_rate=1.0, breakeven_wr=0.56, max_dd=0.0,
            today_n=0, today_total=0.0, req_n=None,
        )

    def test_n1_positive_ci_does_not_clear(self, tmp_path: Path) -> None:
        db = _cadence_db(tmp_path, state="stopped", n_recent_ticks=0)
        conn = _conn(db)
        bot = gather_bot_state(conn)
        conn.close()
        out = render_text(
            [self._model(n=1, t_lo=2.2)], LiveBook_stub(), bot, SINCE, 9.0
        )
        assert "CLEARS NOW" not in out
        assert "sample too small" in out

    def test_large_n_positive_ci_clears(self, tmp_path: Path) -> None:
        db = _cadence_db(tmp_path, state="stopped", n_recent_ticks=0)
        conn = _conn(db)
        bot = gather_bot_state(conn)
        conn.close()
        out = render_text(
            [self._model(n=100, t_lo=0.05)], LiveBook_stub(), bot, SINCE, 9.0
        )
        assert "CLEARS NOW" in out


class TestRenderText:
    def test_report_flags_stalled_race(self, seeded_db: Path) -> None:
        conn = _conn(seeded_db)
        models = gather_models(conn, SINCE, today="2026-07-09")
        live = gather_live_book(conn)
        bot = gather_bot_state(conn)
        conn.close()
        out = render_text(models, live, bot, SINCE, trades_per_day=9.0)
        assert "RACE STANDINGS" in out
        assert "winner" in out and "loser" in out
        assert "DEPLOY-BAR TRACKER" in out
        assert "race accruing: NO" in out
        assert "NO NEW RACE DATA" in out  # the stalled-loop warning
