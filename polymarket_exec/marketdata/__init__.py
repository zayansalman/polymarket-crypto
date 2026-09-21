"""Live Polymarket market data over WebSockets: Up/Down order books, trades, reference prices.

``hub.MarketDataHub`` is the public API; the other modules are its parts:

* ``clob_messages`` — pure parsers for the CLOB market channel;
* ``order_book`` — one token's book rebuilt from snapshots and level changes;
* ``clob_stream`` — the reconnecting CLOB market-channel connection;
* ``rtds_stream`` — Chainlink, Chainlink 60 s TWAP and Binance prices from RTDS;
* ``universe`` — which Up/Down windows to follow and their token ids.

Observation data only: nothing here decides or gates trades, or reads the trading mode.
"""
