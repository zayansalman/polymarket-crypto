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
