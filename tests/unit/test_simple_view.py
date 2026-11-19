"""Simple paper-trading screen: payload math, plain-English decisions, and safety.

No network. DB-backed tests use a temporary SQLite file.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot import paper_wallet
from polymarket_exec.ops.dashboard import simple_view as sv

DASHBOARD_DIR = Path(sv.__file__).resolve().parent
RULES = {
    "edge_min": 0.045,
    "edge_max": 0.07,
    "price_min": 0.50,
    "price_max": 0.95,
    "min_seconds_left": 60,
    "tick_seconds": 5.0,
    "trade_min_usd": 1.0,
    "trade_max_usd": 5.0,
    "min_shares": 5.0,
    "fee_rate": 0.07,
}
SELECTION = {"asset": "btc", "timeframe": "5m", "supported": True}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def _tick(start: int, now: float, **over) -> dict:
    tick = {
        "created_at": _iso(now - 2),
        "window_slug": f"btc-updown-5m-{start}",
        "market_question": "Bitcoin Up or Down",
        "spot_price": 96455.20,
        "reference_price": 96412.18,
        "market_up_price": 0.57,
        "market_down_price": 0.45,
        "up_best_bid": 0.56,
        "down_best_bid": 0.44,
        "fair_up_prob": 0.585,
        "reason": "skip: edge/confidence below threshold",
    }
    tick.update(over)
    return tick


def _payload(raw: dict, *, now: float, state: str = "running", mode: str = "paper") -> dict:
    base = {
        "tick": None, "wallet": None, "trades": [], "closed": [], "open_positions": [],
        "any_open": 0, "curve": [], "activity": [], "window_trades": 0, "blocked": None,
    }
    base.update(raw)
    return sv.build_payload(
        base, now=now, bot={"state": state, "mode": mode, "detail": ""},
        selection=SELECTION, rules=RULES,
    )


# --------------------------------------------------------------------- pure


def test_parse_window_slug() -> None:
    assert sv.parse_window_slug("btc-updown-15m-1700000100") == {
        "asset": "btc", "timeframe": "15m", "start_ts": 1700000100, "end_ts": 1700001000,
    }
    assert sv.parse_window_slug("btc-updown-1h-1700000000") is None
    assert sv.parse_window_slug("garbage") is None
    assert sv.parse_window_slug(None) is None


def test_estimated_fee_matches_formula() -> None:
    # 10 shares at 50c: 10 * 0.07 * 0.5 * 0.5
    assert sv.estimated_fee(10, 0.5) == pytest.approx(0.175)
    assert sv.estimated_fee(10, 1.0) == 0.0
    assert sv.estimated_fee(0, 0.5) == 0.0


def test_downsample_keeps_first_and_last() -> None:
    pts = [{"t": i, "v": i} for i in range(1000)]
    out = sv.downsample(pts, 50)
    assert len(out) == 50
    assert out[0] == pts[0] and out[-1] == pts[-1]


def test_small_edge_is_explained_with_the_numbers() -> None:
    now = time.time()
    start = int(now // 300) * 300
    p = _payload({"tick": _tick(start, now)}, now=now)
    assert p["decision"]["headline"] == "Sitting out: the edge is too small."
    assert "1.5¢" in p["decision"]["detail"] and "4.5¢" in p["decision"]["detail"]
    assert p["market"]["best_side"] == "Up"
    assert p["market"]["fresh"] is True


def test_stale_tick_while_running_says_waiting() -> None:
    now = time.time()
    start = int(now // 300) * 300
    p = _payload({"tick": _tick(start, now, created_at=_iso(now - 120))}, now=now)
    assert p["market"]["fresh"] is False
    assert p["decision"]["tone"] == "warn"
    assert p["decision"]["headline"] == "Waiting for fresh market data."


def test_degraded_feed_hides_prices_it_cannot_trust() -> None:
    now = time.time()
    start = int(now // 300) * 300
    tick = _tick(
        start, now, spot_price=0.0, fair_up_prob=0.5,
        reason="skip: settlement feed degraded (chainlink spot unavailable)",
    )
    p = _payload({"tick": tick}, now=now)
    assert p["market"]["btc_price"] is None
    assert p["market"]["chance_up"] is None
    assert p["decision"]["tone"] == "warn"


def test_one_entry_per_window_is_explained() -> None:
    now = time.time()
    start = int(now // 300) * 300
    tick = _tick(start, now, reason="enter Up: executable edge +0.050 @ ask 0.550")
    p = _payload({"tick": tick, "window_trades": 1}, now=now)
    assert p["decision"]["headline"] == "Done with this window."


def test_stopped_in_live_mode_points_back_to_paper() -> None:
    p = _payload({}, now=time.time(), state="stopped", mode="live")
    assert p["decision"]["headline"] == "The bot is stopped."
    assert "Switch back to paper" in p["decision"]["detail"]


def test_down_trade_wording_uses_a_not_an() -> None:
    now = time.time()
    start = int(now // 300) * 300
    open_row = {
        "position_id": 7, "opened_at": _iso(now - 30), "window_slug": f"btc-updown-5m-{start}",
        "side": "Down", "entry_price": 0.6, "shares": 5.0, "cost_usd": 3.0,
    }
    p = _payload({"tick": _tick(start, now), "open_positions": [open_row], "any_open": 1}, now=now)
    assert p["decision"]["headline"] == "Holding a Down trade until this window settles."
    # Marked at the Down best bid (0.44) for the current window.
    assert p["position"]["value_usd"] == pytest.approx(2.2)
    assert p["position"]["marked_live"] is True


# --------------------------------------------------------------------- DB


@pytest_asyncio.fixture
async def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "simple_view.db")
    await _db.init_db()
    return _db


async def _add_trade(
    conn, start: int, side: str, shares: float, entry: float,
    exit_type: str | None, payout: float | None, exit_price: float | None = None,
    *, fees: bool = True, slug: str | None = None,
) -> int:
    """A wallet trade as the bot books it: the taker fee on the buy and on a
    sale (``fees=False``: a trade from before the wallet paid fees)."""
    slug = slug or f"btc-updown-5m-{start}"
    if exit_type == "SETTLEMENT" and exit_price is None:
        exit_price = 1.0 if payout else 0.0
    buy_fee = paper_wallet.taker_fee(entry, shares) if fees else 0.0
    sell_fee = paper_wallet.taker_fee(exit_price, shares) if fees and exit_type == "SELL" else 0.0
    gross = None if exit_type is None else payout - shares * entry
    cur = await conn.execute(
        "INSERT INTO paper_positions(opened_at, closed_at, window_slug, side, state, entry_price,"
        " exit_price, notional_usd, shares, quote_source, strategy_style, mode, realized_pnl_usd,"
        " fees_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_iso(start + 40), None if exit_type is None else _iso(start + 305), slug, side,
         "open" if exit_type is None else "closed", entry, exit_price, shares * entry, shares,
         "clob", "settle", "paper",
         None if gross is None else gross - buy_fee - sell_fee,
         (buy_fee + sell_fee) if fees else None),
    )
    pid = cur.lastrowid
    await paper_wallet.apply_cash_flow(
        conn, position_id=pid, window_slug=slug, transaction_type="BUY",
        amount_usd=shares * entry, fee_usd=buy_fee,
    )
    if exit_type is not None:
        await paper_wallet.apply_cash_flow(
            conn, position_id=pid, window_slug=slug, transaction_type=exit_type,
            amount_usd=payout, fee_usd=sell_fee,
        )
    return pid


@pytest.mark.asyncio
async def test_ledger_totals_curve_and_open_trade(ledger) -> None:
    now = time.time()
    start = int(now // 300) * 300
    async with ledger.connect() as conn:
        await _add_trade(conn, start - 7200, "Up", 10, 0.55, "SETTLEMENT", 10.0)
        await _add_trade(conn, start - 5400, "Down", 9.0909, 0.55, "SETTLEMENT", 0.0)
        await _add_trade(conn, start - 3600, "Up", 8, 0.62, "SELL", 4.64, exit_price=0.58)
        await _add_trade(conn, start - 2400, "Down", 6, 0.70, "SETTLEMENT", 6.0)
        await _add_trade(conn, start, "Up", 7, 0.53, None, None)
        await conn.execute(
            "INSERT INTO paper_ticks(created_at, window_slug, remaining_seconds, spot_price,"
            " reference_price, sigma_per_second, market_up_price, market_down_price,"
            " fair_up_prob, edge, confidence, notional_usd, reason, feed_source,"
            " up_best_bid, up_best_ask, down_best_bid, down_best_ask, quote_source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_iso(now - 2), f"btc-updown-5m-{start}", 200, 96455.2, 96412.18, 0.0001,
             0.56, 0.46, 0.58, 0.02, 0.6, 4.0, "enter Up: executable edge +0.020 @ ask 0.560",
             "spot=chainlink_ws", 0.55, 0.56, 0.45, 0.46, "clob"),
        )
        await conn.commit()

    raw = await sv.load_raw()
    p = sv.build_payload(
        raw, now=now, bot={"state": "running", "mode": "paper", "detail": ""},
        selection=SELECTION, rules=RULES,
    )
    wallet = p["wallet"]
    # Every figure is after fees: $0.8094 paid on five buys and one sale.
    assert wallet["cash_usd"] == pytest.approx(96.4606)
    assert wallet["open_value_usd"] == pytest.approx(7 * 0.55)
    assert wallet["equity_usd"] == pytest.approx(96.4606 + 3.85)
    assert wallet["fees_paid_usd"] == pytest.approx(0.8094)
    stats = p["stats"]
    assert (stats["trades"], stats["wins"], stats["losses"]) == (4, 2, 2)
    assert stats["total_pnl_usd"] == pytest.approx(0.98 - 0.6873)
    assert stats["gross_pnl_usd"] == pytest.approx(0.98)
    assert stats["fees_usd"] == pytest.approx(0.6873)
    assert "est_fees_usd" not in stats  # an older page would subtract fees twice
    assert [t["result"] for t in p["trades"]] == ["open", "won", "sold", "lost", "won"]
    sold = p["trades"][2]
    assert sold["fee_usd"] == pytest.approx(0.1319 + 0.1364)
    assert sold["pnl_usd"] == pytest.approx(4.64 - 4.96 - 0.1319 - 0.1364)
    # Start, each finished trade (cash after exit), then "now" = the balance on screen.
    assert [round(pt["v"], 2) for pt in p["curve"]] == [100.0, 104.33, 99.17, 98.58, 100.29, 100.31]
    assert p["curve"][-1]["now"] is True
    assert p["decision"]["headline"] == "Holding an Up trade until this window settles."
    assert p["position"]["fee_usd"] == pytest.approx(0.1221)
    assert p["position"]["unrealized_usd"] == pytest.approx(7 * 0.55 - 3.71 - 0.1221)
    assert [q["position_id"] for q in p["positions"]] == [p["position"]["position_id"]]


@pytest.mark.asyncio
async def test_trades_from_before_fees_are_charged_once(ledger, monkeypatch) -> None:
    """The catch-up: fee-free history pays its fees once, and every view agrees."""
    now = time.time()
    start = int(now // 300) * 300
    async with ledger.connect() as conn:
        a = await _add_trade(conn, start - 7200, "Up", 10, 0.55, "SETTLEMENT", 10.0, fees=False)
        b = await _add_trade(conn, start - 3600, "Up", 8, 0.62, "SELL", 4.64,
                             exit_price=0.58, fees=False)
        await _add_trade(conn, start, "Up", 7, 0.53, None, None, fees=False)
        await conn.commit()
    before = await sv.load_raw()
    old_curve = [pt["v"] for pt in sv.build_payload(
        before, now=now, bot={"state": "stopped", "mode": "paper", "detail": ""},
        selection=SELECTION, rules=RULES)["curve"]]

    event = await paper_wallet.book_past_fees()
    fees = [0.1733, 0.1319 + 0.1364, 0.1221]  # T1 buy, T2 buy + sale, open buy
    assert event is not None and event.trades == 3 and event.fills == 4
    assert event.amount_usd == pytest.approx(sum(fees))
    assert await paper_wallet.cash_balance() == pytest.approx(event.cash_before_usd - sum(fees))
    assert await paper_wallet.book_past_fees() is None  # once only

    async with ledger.connect() as conn:
        async with conn.execute(
            "SELECT position_id, fees_usd, realized_pnl_usd FROM paper_positions ORDER BY position_id"
        ) as cur:
            rows = {r["position_id"]: dict(r) for r in await cur.fetchall()}
    assert rows[a]["fees_usd"] == pytest.approx(0.1733)
    assert rows[a]["realized_pnl_usd"] == pytest.approx(4.5 - 0.1733)
    assert rows[b]["realized_pnl_usd"] == pytest.approx(4.64 - 4.96 - 0.2683)

    p = sv.build_payload(
        await sv.load_raw(), now=now, bot={"state": "stopped", "mode": "paper", "detail": ""},
        selection=SELECTION, rules=RULES,
    )
    assert p["stats"]["total_pnl_usd"] == pytest.approx(4.5 - 0.32 - 0.1733 - 0.2683)
    assert p["wallet"]["fees_paid_usd"] == pytest.approx(sum(fees))
    # The chart is after fees all the way back: each old point minus the fees
    # paid by then, and no drop where the catch-up was charged.
    new_curve = [pt["v"] for pt in p["curve"]]
    assert new_curve[0] == pytest.approx(old_curve[0])
    assert new_curve[1] == pytest.approx(old_curve[1] - 0.1733)
    # The open trade's fee was paid after this point (its buy came later).
    assert new_curve[2] == pytest.approx(old_curve[2] - 0.1733 - 0.2683, abs=1e-4)
    assert new_curve[-1] == pytest.approx(p["wallet"]["equity_usd"])
    assert new_curve[-1] == pytest.approx(old_curve[-1] - sum(fees))

    # A trade booked after the catch-up pays its own fee; nothing is charged twice.
    async with ledger.connect() as conn:
        await _add_trade(conn, start + 300, "Down", 5, 0.60, None, None,
                         slug=f"btc-updown-5m-{start + 300}")
        await conn.commit()
    assert await paper_wallet.book_past_fees() is None


@pytest.mark.asyncio
async def test_curve_counts_open_trades_at_cost_between_settlements(ledger) -> None:
    """Three trades in one window settle one row at a time: no dip in between."""
    now = time.time()
    start = int(now // 300) * 300 - 600
    slug = f"btc-updown-5m-{start}"
    async with ledger.connect() as conn:
        ids = []
        for _ in range(3):
            cur = await conn.execute(
                "INSERT INTO paper_positions(opened_at, closed_at, window_slug, side, state,"
                " entry_price, exit_price, notional_usd, shares, quote_source, strategy_style,"
                " mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (_iso(start + 40), _iso(start + 305), slug, "Up", "closed", 0.6, 1.0, 6.0, 10,
                 "clob", "settle", "paper"),
            )
            ids.append(cur.lastrowid)
            await paper_wallet.apply_cash_flow(conn, position_id=cur.lastrowid, window_slug=slug,
                                               transaction_type="BUY", amount_usd=6.0, fee_usd=0.168)
        for pid in ids:
            await paper_wallet.apply_cash_flow(conn, position_id=pid, window_slug=slug,
                                               transaction_type="SETTLEMENT", amount_usd=10.0)
        await conn.commit()
    p = sv.build_payload(
        await sv.load_raw(), now=now, bot={"state": "stopped", "mode": "paper", "detail": ""},
        selection=SELECTION, rules=RULES,
    )
    pts = [round(pt["v"], 3) for pt in p["curve"]]
    # 100 - 3 fees, with each later winner at cost until it settles.
    assert pts == [100.0, 103.496, 107.496, 111.496, 111.496]


@pytest.mark.asyncio
async def test_empty_ledger_is_a_clean_start(ledger) -> None:
    raw = await sv.load_raw()
    p = sv.build_payload(
        raw, now=time.time(), bot={"state": "stopped", "mode": "paper", "detail": ""},
        selection=SELECTION, rules=RULES,
    )
    assert p["market"] is None and p["position"] is None and p["trades"] == []
    assert p["wallet"]["equity_usd"] == pytest.approx(100.0)
    assert p["stats"]["trades"] == 0
    assert p["decision"]["headline"] == "The bot is stopped."


# --------------------------------------------------------------------- safety


def test_simple_page_cannot_arm_live() -> None:
    """The simple page never carries or sends the LIVE dashboard token."""
    html = (DASHBOARD_DIR / "templates" / "simple.html").read_text(encoding="utf-8")
    js = (DASHBOARD_DIR / "static" / "simple.js").read_text(encoding="utf-8")
    assert "dashboard_token" not in html
    assert 'name="dashboard-token"' not in html
    assert "X-Dashboard-Token" not in js.replace("never sends the X-Dashboard-Token", "")
    assert "innerHTML" not in js.replace("never innerHTML", "")
    # No outside requests: every URL the page uses is local.
    assert "http://" not in html.replace("http://www.w3.org", "")
    assert "https://" not in html and "https://" not in js


def test_routes_serve_simple_page_and_console(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fastapi.testclient import TestClient

    from polymarket_exec.ops.dashboard import app as app_module

    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "routes.db")
    with TestClient(app_module.app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Bitcoin Paper Trader" in page.text
        assert "/static/simple.js" in page.text
        assert app_module._DASHBOARD_TOKEN not in page.text

        console = client.get("/advanced")
        assert console.status_code == 200
        assert app_module._DASHBOARD_TOKEN in console.text

        data = client.get("/api/simple")
        assert data.status_code == 200
        body = data.json()
        for key in ("bot", "wallet", "decision", "stats", "trades", "curve", "rules"):
            assert key in body
        assert body["wallet"]["initial_usd"] == pytest.approx(100.0)
        assert client.get("/static/simple.css").status_code == 200


# --------------------------------------------------------------------- accuracy


def _settled(price: float, edge: float, won: bool, shares: float = 5.0) -> dict:
    payout = shares if won else 0.0
    fee = paper_wallet.taker_fee(price, shares)
    return {
        "exit_type": "SETTLEMENT", "entry_price": price, "edge": edge, "shares": shares,
        "exit_price": 1.0 if won else 0.0, "payout_usd": payout,
        "fee_usd": fee, "pnl_usd": payout - shares * price - fee,  # the wallet's, after fees
    }


def test_accuracy_compares_what_the_bot_said_with_what_happened() -> None:
    closed = [_settled(0.70, 0.05, i < 28) for i in range(40)]  # said 75%, won 70%
    closed += [_settled(0.92, 0.05, i < 8) for i in range(10)]  # said 97%, won 80%
    acc = sv._accuracy(closed)
    assert acc["trades"] == 50
    labels = {b["label"]: b for b in acc["buckets"]}
    assert labels["70% to 85%"]["trades"] == 40
    assert labels["70% to 85%"]["won"] == pytest.approx(0.70)
    assert labels["70% to 85%"]["bot_said"] == pytest.approx(0.75)
    assert labels["95% or more"]["won"] == pytest.approx(0.80)
    # 95%+ bucket: 8 wins * $0.40 - 2 losses * $4.60, minus small fees.
    assert labels["95% or more"]["profit_usd"] == pytest.approx(8 * 0.4 - 2 * 4.6)
    assert labels["95% or more"]["profit_after_fees_usd"] < labels["95% or more"]["profit_usd"]
    text = " ".join(acc["summary"])
    assert "said it had a 79% chance" in text and "won 72%" in text
    assert "points higher or lower" in text


def test_accuracy_waits_for_enough_trades() -> None:
    acc = sv._accuracy([_settled(0.6, 0.05, True) for _ in range(5)])
    assert acc["trades"] == 5
    assert "too few" in acc["summary"][0]
    assert sv._accuracy([])["trades"] == 0


def test_fee_aware_wording_says_after_fees() -> None:
    now = time.time()
    start = int(now // 300) * 300
    base = {
        "tick": _tick(start, now, market_up_price=0.50, fair_up_prob=0.56),
        "wallet": None, "trades": [], "closed": [], "open_positions": [], "any_open": 0,
        "curve": [], "activity": [], "window_trades": 0, "blocked": None,
    }
    p = sv.build_payload(
        base, now=now, bot={"state": "running", "mode": "paper", "detail": ""},
        selection=SELECTION, rules={**RULES, "fee_aware": True},
    )
    # 6c raw edge on Up at 50c is 4.25c after the 1.75c fee.
    assert p["market"]["best_edge"] == pytest.approx(0.0425)
    assert "4.2¢ after fees" in p["decision"]["detail"] or "4.3¢ after fees" in p["decision"]["detail"]
    assert "at least 4.5¢ after fees" in p["decision"]["detail"]


# ------------------------------------------------- learning + strategies


def test_market_shows_the_learned_chance_and_keeps_the_raw_one() -> None:
    now = 1_700_000_200.0
    plain = _payload({"tick": _tick(1_700_000_100, now)}, now=now)["market"]
    assert plain["chance_up"] == pytest.approx(0.585) and plain["chance_up_raw"] is None
    learned = _payload({"tick": _tick(1_700_000_100, now, learned_up_prob=0.55)}, now=now)["market"]
    assert learned["chance_up"] == pytest.approx(0.55)
    assert learned["chance_up_raw"] == pytest.approx(0.585)
    assert learned["edge_up"] == pytest.approx(0.55 - 0.57)


def test_learning_status_reads_the_saved_fit() -> None:
    from polymarket_bot import learning

    now = 1_700_000_200.0
    assert _payload({}, now=now)["learning"]["state"] == "waiting"
    fit = learning.Fit(0.37, 0.44, True, "ok", 1_700_000_000, 285, 57, 0.177, 0.184, 0.175)
    data = _payload({"learning_raw": fit.to_json(), "learning_started": _iso(now - 60)},
                    now=now)["learning"]
    assert data["state"] == "active" and data["windows"] == 285
    assert data["started_ts"] == pytest.approx(now - 60)
    assert data["example_learned"] < 0.92


def test_strategy_table_counts_fees_and_judges_luck() -> None:
    now = 1_700_100_000.0
    shadows = [
        # 60 trades, +0.10 each with no spread at all: ahead beyond luck
        {"model_id": "pricing_v0", "trades": 60, "wins": 45, "avg_price": 0.70,
         "pnl_usd": 6.0, "pnl_sq": 0.6, "first_at": _iso(now - 86_400)},
        # 60 noisy trades around zero: within luck
        {"model_id": "cushion_favorite_v2", "trades": 60, "wins": 42, "avg_price": 0.70,
         "pnl_usd": 1.0, "pnl_sq": 60 * 2.0, "first_at": _iso(now - 86_400)},
        {"model_id": "learned_mid_v1", "trades": 3, "wins": 2, "avg_price": 0.75,
         "pnl_usd": 0.5, "pnl_sq": 3.0, "first_at": _iso(now - 600)},
    ]
    f70, f80, f60 = (sv.estimated_fee(5, x) for x in (0.70, 0.80, 0.60))
    closed = [  # this bot: one trade before learning started, two after (pnl after fees)
        {"opened_at": _iso(now - 7_200), "shares": 5, "entry_price": 0.70,
         "pnl_usd": 1.5 - f70, "fee_usd": f70},
        {"opened_at": _iso(now - 1_800), "shares": 5, "entry_price": 0.80,
         "pnl_usd": 1.0 - f80, "fee_usd": f80},
        {"opened_at": _iso(now - 900), "shares": 5, "entry_price": 0.60,
         "pnl_usd": -3.0 - f60, "fee_usd": f60},
    ]
    rows = {r["id"]: r for r in _payload(
        {"shadows": shadows, "closed": closed, "learning_started": _iso(now - 3_600)},
        now=now)["strategies"]}
    assert [r["id"] for r in _payload({}, now=now)["strategies"]] == [s[0] for s in sv.STRATEGIES]
    assert rows["pricing_v0"]["verdict"] == "ahead"
    assert rows["cushion_favorite_v2"]["verdict"] == "luck"
    assert rows["learned_mid_v1"]["verdict"] == "too_early"
    assert rows["bot_raw_twin_v1"]["trades"] == 0 and rows["bot_raw_twin_v1"]["since_ts"] is None
    main = rows["main"]
    assert main["trades"] == 2 and main["won"] == pytest.approx(0.5)
    assert main["profit_after_fees_usd"] == pytest.approx(-2.0 - f80 - f60, abs=1e-4)


def test_strategy_table_waits_for_learning_before_counting_this_bot() -> None:
    closed = [{"opened_at": _iso(1_700_000_000), "shares": 5, "entry_price": 0.7, "pnl_usd": 1.5}]
    main = _payload({"closed": closed}, now=1_700_000_100.0)["strategies"][0]
    assert main["id"] == "main" and main["trades"] == 0 and main["since_ts"] is None



def test_blocked_entries_are_summarized_in_plain_english() -> None:
    now = 1_700_000_200.0
    raw = {"blocks_hour": 12, "last_block": {
        "created_at": _iso(now - 30), "error": "per-trade cap: 48.00 USD exceeds 3.00 USD"}}
    blocks = _payload(raw, now=now)["entry_blocks"]
    assert blocks["last_hour"] == 12 and blocks["latest_ts"] == pytest.approx(now - 30)
    assert "$48.00" in blocks["reason"] and "$3.00 a trade" in blocks["reason"]
    assert "shares" in blocks["reason"]
    assert _payload({}, now=now)["entry_blocks"] == {"last_hour": 0, "reason": None, "latest_ts": None}
    assert sv.block_reason_plain("daily loss halt: paper realized -10").startswith("today's loss limit")
    assert sv.block_reason_plain("something new") == "something new"


# ------------------------------------------------- several open trades


def _open_row(pid: int, start: int, now: float, side: str = "Up", age: float = 60.0,
              slug_start: int | None = None) -> dict:
    return {
        "position_id": pid, "opened_at": _iso(now - age),
        "window_slug": f"btc-updown-5m-{slug_start if slug_start is not None else start}",
        "side": side, "entry_price": 0.6, "shares": 10.0, "cost_usd": 6.0, "fee_usd": 0.168,
        "buy_id": pid,
    }


def _many(raw: dict, now: float, *, limit: int = 3, spacing: int = 30, mode: str = "paper") -> dict:
    base = {
        "tick": None, "wallet": None, "trades": [], "closed": [], "open_positions": [],
        "any_open": 0, "curve": [], "activity": [], "window_trades": 0, "blocked": None,
    }
    base.update(raw)
    return sv.build_payload(
        base, now=now, bot={"state": "running", "mode": mode, "detail": ""}, selection=SELECTION,
        rules={**RULES, "max_open": limit, "spacing_seconds": spacing, "exit_style": "settle"},
    )


def test_room_for_more_explains_the_market_and_what_is_held() -> None:
    now = time.time()
    start = int(now // 300) * 300
    rows = [_open_row(1, start, now)]
    p = _many({"tick": _tick(start, now), "open_positions": rows, "any_open": 1,
               "window_trades": 1}, now)
    assert p["decision"]["headline"] == "Sitting out: the edge is too small."
    assert p["decision"]["detail"].startswith(
        "It holds an Up trade in this window and can add 2 more.")
    assert len(p["positions"]) == 1


def test_waits_between_trades_in_one_window() -> None:
    now = time.time()
    start = int(now // 300) * 300
    tick = _tick(start, now, reason="enter Up: executable edge +0.050 @ ask 0.550")
    rows = [_open_row(1, start, now, age=12)]  # tick is 2 s old: ~20 s to go
    p = _many({"tick": tick, "open_positions": rows, "any_open": 1, "window_trades": 1}, now)
    assert p["decision"]["headline"] == "Holding an Up trade, waiting before another."
    assert "waits 30 seconds" in p["decision"]["detail"]
    assert "about 20 seconds to go" in p["decision"]["detail"]


def test_holding_the_most_allowed_and_the_window_cap() -> None:
    now = time.time()
    start = int(now // 300) * 300
    rows = [_open_row(3, start, now, "Down", 20), _open_row(2, start, now, "Up", 60),
            _open_row(1, start, now, "Up", 100)]
    p = _many({"tick": _tick(start, now), "open_positions": rows, "any_open": 3,
               "window_trades": 3}, now)
    assert p["decision"]["headline"] == "Holding 3 trades (2 Up, 1 Down) until this window settles."
    assert "3 is the most it holds at once" in p["decision"]["detail"]
    assert [q["position_id"] for q in p["positions"]] == [3, 2, 1]
    assert p["position"]["position_id"] == 3  # the newest, for the buy banner
    # A Stop sold them: the window still counts its trades.
    done = _many({"tick": _tick(start, now), "window_trades": 3}, now)
    assert done["decision"]["headline"] == "Done with this window."
    assert "at most 3 trades per window" in done["decision"]["detail"]


def test_last_window_still_settling_leaves_room_in_the_new_one() -> None:
    now = time.time()
    start = int(now // 300) * 300
    rows = [_open_row(1, start, now, slug_start=start - 300)]
    tick = _tick(start, now, reason="enter Down: executable edge +0.050 @ ask 0.450")
    p = _many({"tick": tick, "open_positions": rows, "any_open": 1}, now)
    assert p["decision"]["headline"] == "Buying Up now."
    # One at a time (or real-money mode) waits for the settlement instead.
    for limit, mode in ((1, "paper"), (3, "live")):
        one = _many({"tick": tick, "open_positions": rows, "any_open": 1}, now,
                    limit=limit, mode=mode)
        assert one["decision"]["headline"] == "Waiting for the last trade to settle."
        assert one["decision"]["detail"] == "The bot holds one trade at a time."


def test_open_trade_value_is_after_its_fee() -> None:
    now = time.time()
    start = int(now // 300) * 300
    p = _many({"tick": _tick(start, now), "open_positions": [_open_row(1, start, now)],
               "any_open": 1}, now)
    pos = p["positions"][0]
    # 10 Up shares marked at the 0.56 bid, bought for $6.00 + 16.8c fee.
    assert pos["value_usd"] == pytest.approx(5.6)
    assert pos["unrealized_usd"] == pytest.approx(5.6 - 6.0 - 0.168)
