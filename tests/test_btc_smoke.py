from pathlib import Path

from polymarket_bot.history import load_btc_history_stats
from polymarket_bot.backtest import BacktestParams, BuyOpportunity, evaluate_params, parse_market_window
from polymarket_bot.strategy import StrategyParams, notional_from_confidence
from config import PAPER_MAX_TRADE_USD, PAPER_MIN_CONFIDENCE, PAPER_MIN_TRADE_USD


def test_confidence_sizing_stays_in_paper_bounds() -> None:
    params = StrategyParams(
        min_trade_usd=PAPER_MIN_TRADE_USD,
        max_trade_usd=PAPER_MAX_TRADE_USD,
        entry_edge_min=0.045,
        min_confidence=PAPER_MIN_CONFIDENCE,
    )
    assert notional_from_confidence(PAPER_MIN_CONFIDENCE, params) >= PAPER_MIN_TRADE_USD
    assert notional_from_confidence(0.99, params) <= PAPER_MAX_TRADE_USD


def test_history_csv_is_optional(tmp_path: Path) -> None:
    stats = load_btc_history_stats(tmp_path / "missing.csv")
    assert stats.found is False
    assert stats.btc_rows == 0


def test_market_window_parser_uses_eastern_time() -> None:
    window = parse_market_window(
        "Bitcoin Up or Down - April 29, 2:10PM-2:15PM ET",
        1777486398,
    )
    assert window is not None
    assert window.end_ts - window.start_ts == 300


def test_backtest_filter_accepts_positive_edge() -> None:
    opp = BuyOpportunity(
        market_name="Bitcoin Up or Down - April 29, 2:10PM-2:15PM ET",
        side="Up",
        trade_ts=1777486398,
        window_start_ts=1777486200,
        window_end_ts=1777486500,
        remaining_seconds=102,
        entry_price=0.5,
        actual_notional_usd=2.0,
        actual_shares=4.0,
        reference_price=100.0,
        trade_spot_price=101.0,
        settlement_price=102.0,
        outcome="Up",
        fair_side_prob=0.62,
        edge=0.12,
        confidence=0.836,
        settlement_pnl_usd=2.0,
    )
    metrics = evaluate_params(
        [opp],
        BacktestParams(
            entry_edge_min=0.04,
            min_confidence=0.62,
            min_remaining_seconds=90,
            max_entry_price=0.95,
        ),
    )
    assert metrics.trades == 1
    assert metrics.total_pnl_usd > 0
