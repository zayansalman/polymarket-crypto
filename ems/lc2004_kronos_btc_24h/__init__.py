"""lc2004-Kronos BTC 24h: a display-only forecast for Polymarket's daily BTC Up/Down market.

Every hour the ``lc2004/kronos_base_model_BTCUSDT_1h_finetune`` Kronos model reads the last 512
closed Binance BTCUSDT 1h candles and samples 30 price paths to the window's end (12:00 ET). The
forecast is the number of paths that finish strictly above the strike (the Binance BTCUSDT 1m
close at 12:00 ET on the window's first day), turned into a chance of Up with Laplace's rule,
q_up = (k + 1) / 32. The dashboard shows it next to the market's book so the operator can decide
and trade by hand.

This package places no orders, cancels none and simulates none. It has no paper or live leg, no
sizing and no strategy switch. Zayan (operator), 2026-09-29: "this doesnt need to trade for me
actually just show its prediction in the regime overview or somewhere in the dashboard and ill
go manaulla place a trade".

Modules:

* ``market`` - the daily BTC market: noon-ET window timing, Gamma discovery, Binance 1h and 1m
  candles and the CLOB book.
* ``maths`` - pure: the Laplace chance of Up from the paths, and the spread of the paths.
* ``forecast`` - one hourly model run, scored against the strike and stored.
* ``ledger`` - every read and write of the ``lc2004_forecasts`` table.

Ported and trimmed by Claude, 2026-09-29, from last week's
``polymarket_bot/lc2004_kronos_btc_24h/`` (Claude, 2026-09-22), dropping everything that traded.
"""
