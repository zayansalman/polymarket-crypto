"""Unit tests for the CryptoTrader-LM daily Up/Down backtest inputs (no network)."""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from tools.cryptotrader_lm import binance, markets, news, orderflow, pricing, prompts
from tools.cryptotrader_lm.timing import market_times


def _utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")


def test_market_times_follow_new_york_dst():
    summer = market_times(date(2026, 7, 2))
    assert _utc(summer.t_ref) == "2026-07-01 16:00"
    assert _utc(summer.t_dec) == "2026-07-01 15:55"
    assert _utc(summer.t_settle) == "2026-07-02 16:00"
    winter = market_times(date(2026, 1, 2))
    assert _utc(winter.t_ref) == "2026-01-01 17:00"


def test_entry_mid_uses_last_point_before_decision_and_rejects_stale():
    hist = [{"t": 100, "p": 0.40}, {"t": 700, "p": 0.52}, {"t": 1300, "p": 0.99}]
    assert pricing.entry_mid(hist, t_dec=1000) == 0.52
    assert pricing.entry_mid(hist, t_dec=1400) == 0.99
    assert pricing.entry_mid([{"t": 0, "p": 0.5}], t_dec=601) is None
    assert pricing.entry_mid([], t_dec=10) is None


@pytest.mark.parametrize("mid,bid,ask", [
    (0.505, 0.50, 0.51),
    (0.50, 0.49, 0.51),
    (0.57, 0.56, 0.58),
    (0.5125, 0.51, 0.52),
    (0.995, 0.98, 0.99),
])
def test_touch_is_nearest_cent_strictly_around_mid(mid, bid, ask):
    assert pricing.touch_from_mid(mid) == (bid, ask)


def test_down_entry_is_one_minus_up_bid():
    assert pricing.entry_price("up", 0.505) == 0.51
    assert pricing.entry_price("down", 0.505) == 0.50
    assert pricing.entry_price("down", 0.50) == 0.51


def test_fee_schedules_match_polymarket_formulas():
    assert pricing.fee_per_share(0.5, 0.0, 1.0) == 0.0
    assert pricing.fee_per_share(0.5, 0.07, 1.0) == pytest.approx(0.0175)
    assert pricing.fee_per_share(0.5, 0.25, 2.0) == pytest.approx(0.015625)


def test_settle_win_loss_tie():
    win = pricing.settle("up", 0.505, "up", rate=0.07, exponent=1.0, stake_usd=10.0)
    shares = 10.0 / 0.51
    fee = 0.07 * 0.51 * 0.49
    assert win.won is True
    assert win.pnl_usd == pytest.approx(shares * (1 - 0.51 - fee))
    loss = pricing.settle("down", 0.505, "up", rate=0.0, exponent=1.0)
    assert loss.won is False and loss.pnl_usd == pytest.approx(-10.0)
    tie = pricing.settle("up", 0.505, "tie", rate=0.0, exponent=1.0)
    assert tie.won is None and tie.pnl_usd == pytest.approx(shares * (0.5 - 0.51))


def _event(**overrides):
    market = {
        "outcomes": '["Up", "Down"]', "clobTokenIds": '["111", "222"]',
        "outcomePrices": '["0", "1"]', "closed": True, "slug": "bitcoin-up-or-down-on-may-24",
        "endDate": "2025-05-24T16:00:00Z", "conditionId": "0xabc",
        "createdAt": "2025-05-22T16:00:00Z", "feesEnabled": False, "feeSchedule": None,
    }
    market.update(overrides)
    return {"markets": [market]}


def test_parse_event_outcome_and_fees():
    m = markets.parse_event("btc", _event())
    assert (m.market_date, m.outcome, m.up_token, m.fee_rate) == ("2025-05-24", "down", "111", 0.0)
    fee = markets.parse_event("btc", _event(
        feesEnabled=True, feeSchedule={"rate": 0.25, "exponent": 2}))
    assert (fee.fee_rate, fee.fee_exponent) == (0.25, 2.0)
    assert markets.parse_event("btc", _event(closed=False)).outcome is None
    assert markets.parse_event("btc", _event(outcomePrices='["0.5", "0.5"]')).outcome == "tie"
    with pytest.raises(ValueError):
        markets.parse_event("btc", _event(feesEnabled=True, feeSchedule=None))


def _kline(open_s, close, volume=10.0, taker_buy=5.0):
    return [open_s * 1000, "0", "0", "0", str(close), str(volume), 0, str(volume * close), 0,
            str(taker_buy), "0", "0"]


def test_candles_price_and_imbalance_only_use_closed_candles():
    t_dec = 1_000_000_200
    rows = [_kline(t_dec - 300 * i, close=100 + i, taker_buy=8.0 if i <= 12 else 5.0)
            for i in range(1, 300)]
    rows.append(_kline(t_dec, close=999.0, taker_buy=10.0))
    c = binance.Candles(rows)
    assert c.price_at(t_dec) == 101
    assert c.taker_imbalance(t_dec, 3_600) == pytest.approx(0.6)
    assert c.taker_imbalance(t_dec, 7 * binance.DAY_S) is None


def test_up_pressure_direction():
    trades = [
        {"timestamp": 10, "side": "BUY", "outcome": "Up", "price": 0.5, "size": 10},
        {"timestamp": 11, "side": "SELL", "outcome": "Down", "price": 0.5, "size": 4},
        {"timestamp": 12, "side": "BUY", "outcome": "Down", "price": 0.5, "size": 2},
        {"timestamp": 99, "side": "BUY", "outcome": "Up", "price": 0.5, "size": 100},
    ]
    flow = orderflow.up_pressure(trades, 0, 50)
    assert (flow["up_usd"], flow["down_usd"], flow["trades"]) == (7.0, 1.0, 3)
    assert flow["imbalance"] == pytest.approx(0.75)
    assert orderflow.up_pressure([], 0, 1)["imbalance"] is None


def _post(title, seen, summary=""):
    return {"site": "x", "seen": seen, "title": title, "summary": summary}


def test_select_news_window_asset_first_dedup():
    archive = news.Archive([
        _post("Solana rallies", 140),
        _post("Bitcoin jumps 5%", 100),
        _post("BITCOIN JUMPS 5%!", 90),
        _post("ETF flows update", 95, summary="Spot BTC funds saw inflows."),
        _post("BTC future post", 150),
        _post("Too old bitcoin", 10),
    ])
    picked = news.select_news(archive, "btc", t_dec=150, k=4, window_s=100)
    assert [p["title"] for p in picked] == ["Bitcoin jumps 5%", "ETF flows update", "Solana rallies"]


def test_parse_post_cleans_html_and_uses_gmt():
    raw = {"date_gmt": "2026-01-01T15:30:00",
           "title": {"rendered": "Bitcoin&#8217;s <em>big</em> week"},
           "excerpt": {"rendered": "<p>ETF inflows hit a record &amp; more [&hellip;]</p>\n"}}
    post = news.parse_post("bitcoinist.com", raw)
    assert post == {"site": "bitcoinist.com", "seen": 1767281400,
                    "title": "Bitcoin\u2019s big week", "summary": "ETF inflows hit a record & more"}
    assert news.parse_post("x", {"date_gmt": None, "title": {"rendered": "t"}}) is None


def test_finmem_prompt_layout():
    text = prompts.finmem_prompt("eth", "2026-01-01", [("ETH Rallies", "positive")], momentum=-1)
    assert text.startswith(
        "The ticker of the cryptocurrency to be analyzed is ETH-USD and the current date is "
        "2026-01-01The short-term information:\n1. eth rallies (sentiment:positive)In investment")
    assert "past 3 days for this cryptocurrency is negative.Given the information" in text
    no_momentum = prompts.finmem_prompt("btc", "2026-01-01", [], momentum=0)
    assert "Momentum" not in no_momentum


def test_teacher_prompt_has_no_answer_line():
    text = prompts.teacher_prompt("btc", [("Bitcoin up", "neutral")], 65000.123, [1.0, -2.5])
    assert "Correct trading decision" not in text
    assert "Current price: 65000.12" in text
    assert '"+1.00%", "-2.50%"' in text
    assert prompts.wrap("x", "tutorial") == "[INST] x [/INST]"


@pytest.mark.parametrize("text,expected", [
    ('{"investment_decision": "sell", "summary_reason": "..."}', ("sell", "explicit")),
    ("Decision: **Buy**\nReasoning: momentum is positive", ("buy", "explicit")),
    ("The decision is to hold because signals conflict.", ("hold", "explicit")),
    ("I recommend to sell given negative sentiment.", ("sell", "explicit")),
    ("Momentum is strong, so buy.", ("buy", "single_word")),
    ("Could buy or sell here.", (None, "none")),
])
def test_parse_decision(text, expected):
    assert prompts.parse_decision(text) == expected
