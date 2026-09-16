"""Hourly BTC Up/Down strategies (sources: docs/strategies/hourly-btc-strategies.md).

- Binance BTCUSDT 1h reversal after a spot taker-buy/sell push that perps didn't match, with
  the hour closing at its high or low (``btcusdt_1h_spot_taker_push_reversal``).
- Kronos BTCUSDT 1h fine-tune (Hugging Face lc2004): next-hour Up chance vs Polymarket price
  (added in a later change).
"""
