# Polymarket WebSocket market data — `polymarket_exec/marketdata/`

Status: built (2026-09-16). Consumers are not migrated yet: the order ticket, the feed
monitor, `paper._fetch_clob_book`, `live._book_context` and the daily scanner still
poll REST. Moving them onto the hub is the next step.

## Goal

Stop polling Polymarket where a WebSocket exists, and give strategies one in-process
module with the freshest books, trades and reference prices. It runs in the dashboard
process: a REST round trip from this machine is ~230 ms, while parsing a full book
takes ~27 µs and a `price_change` ~2 µs. The data is observation only; nothing here
decides or gates trades, or reads the trading mode.

## Wire facts (live-checked 2026-09-16)

### CLOB market channel — `wss://ws-subscriptions-clob.polymarket.com/ws/market`

- No auth, no compression. First frame
  `{"assets_ids":[...],"type":"market","custom_feature_enabled":true}`. Later changes
  are `{"assets_ids":[...],"operation":"subscribe","custom_feature_enabled":true}` and
  `{"assets_ids":[...],"operation":"unsubscribe"}`. A second `type: market` frame is
  rejected with `INVALID OPERATION`.
- `custom_feature_enabled` is last-write-wins for the whole connection. Every
  subscribe frame must carry it, or `best_bid_ask`, `new_market` (and probably
  `market_resolved`) stop for all tokens. Unsubscribe frames don't reset it.
- The snapshot is one frame holding a JSON array of books, ~0.2 s after a subscribe.
  A frame naming more than 750 tokens gets no snapshot, so frames hold at most 500.
  600 tokens of snapshot is over 1 MiB, so `max_size` is 16 MiB.
- Books list levels worst to best (bids ascending, asks descending). Only snapshot
  books carry `tick_size` and `last_trade_price`. Every book is a full reset.
- `price_change` holds the order and its mirror on the other outcome; `size` is the
  new absolute size ("0" removes the level). Fills do send `price_change`.
- `last_trade_price` (`side` = taker; `fee_rate_bps` is always "0"),
  `tick_size_change` (sent twice), `best_bid_ask` (no sizes), `new_market` (every new
  market platform-wide; Up/Down ~24 h ahead), `market_resolved` (subscribed tokens
  only, ~115-155 s after a 5m window ends).
- Text frames: `PONG`, `NO NEW ASSETS`, `INVALID MESSAGE`, `INVALID OPERATION`,
  `[]` / `[]\n`. The server closes with 1000 "all subscribed assets resolved" once
  every subscribed token has resolved.
- Heartbeat: text `PING` every 10 s. No replay after a reconnect. One run went
  silent for 15 s on a busy market, hence the watchdog.
- 350-900 `price_change` frames/s on the current BTC 5m window; the websockets
  default `max_queue` (16) stalls at that rate. Latency p50 ~40-55 ms, p90 ~90-140 ms.
  Timestamps are ms strings and not monotonic; there are no sequence numbers.

### RTDS — `wss://ws-live-data.polymarket.com`

- No auth; permessage-deflate. Each subscribe is acknowledged with an empty frame.
  RTDS never answers `PING`; liveness is the 1 Hz per-symbol update flow.
- Topics: `crypto_prices` (Binance, `btcusdt`...), `crypto_prices_chainlink` and
  `crypto_prices_twap_sixty` (`btc/usd`...). Filters must be compact JSON, and only
  one filter per topic+type is honoured for updates, so each topic is subscribed
  unfiltered plus one filtered entry per asset (the filtered entries only add a
  history snapshot).
- The Chainlink snapshot arrives under topic `crypto_prices`; the source is read
  from the symbol form (`/usd` Chainlink, `usdt` Binance, twap topic = TWAP).
- Delay (receive minus observation): Binance ~0.5 s, Chainlink ~1.4 s, TWAP ~1.5 s.
- Settlement: 5m/15m markets resolve on the Chainlink 60 s TWAP (since 2026-08-14);
  1h/1d on Binance BTC_USDT.

## Connections: one market-channel socket per asset x timeframe

The first live run put all 96 tokens (48 windows) on one socket, as first designed. It
carried ~2,100 frames/s (~1.2 MiB/s), fell 3-4 s behind, and the server closed it with
1013 "slow consumer: send buffer full" four times in two minutes. A bare reader that
did no parsing (9% CPU) was dropped the same way, so the limit is delivery to this
machine, not the Python code. Measured on 2026-09-16 from this machine:

| Layout | Result |
|---|---|
| 96 tokens, one socket | 1013 disconnect within 40 s |
| one socket per asset (6) | no disconnects, but the BTC socket lagged (p50 1.9 s, max 23 s) |
| BTC 5m current window alone | 956 frames/s, 644 KiB/s, p50 109 ms |
| one socket per asset x timeframe (24) | every socket p50 103-188 ms, max 482 ms, no disconnects |

So the hub opens one socket per (asset, timeframe): 24 for the default grid, each
holding that pair's current and next window (and an ended window until it resolves).
Windows roll inside their socket, and a window's two tokens always share a socket,
because every `price_change` carries both outcomes.

Sharding removed the constant drops, but on this network the full grid (~2,300-2,800
frames/s, ~1.4-1.7 MiB/s) is still close to what gets through. In a later run the BTC
5m socket drifted to 23 s behind before the server reset it. So a socket whose median
latency over its last 64 events is more than 10 s above its own best on that
connection is replaced, which throws the backlog away and starts from a fresh snapshot
(a constant clock offset raises the best too, so it doesn't trigger). The FEEDS delay
is the slowest socket's median, so one lagging market can't hide behind 23 healthy ones.

Every subscribe frame still carries `custom_feature_enabled`, so each socket also
receives the platform-wide `new_market` broadcast: measured at 0.6-1.4 events/s
(1.4-3.3 KiB/s) per socket, so roughly 35-80 KiB/s across 24 sockets. A later option
is to keep the flag on one "lifecycle" socket (ended windows plus one unresolved
anchor) and turn it off on the others. A check on 2026-09-16 showed that
`market_resolved` also reaches a socket that subscribed after the window ended
(116-126 s after the end).

## Modules

| Module | Role |
|---|---|
| `clob_messages.py` | Pure `parse_frame(text) -> [event]`; unknown or malformed objects become `Unknown`. |
| `order_book.py` | `OrderBook` per token; `top()` is O(1) and returns a frozen `TopOfBook`. |
| `clob_stream.py` | `ClobMarketStream`: diffs as operation frames, PING 10 s, 45 s silence watchdog (resubscribe, then reconnect), reconnect when >10 s behind its best, 1-30 s jittered backoff, stop within ~1 s. |
| `rtds_stream.py` | `RtdsPriceStream`: `PricePoint`s per (source, asset), 900-point history, gap count, 30 s silence reconnect. |
| `universe.py` | `MarketUniverse`: current + next window per asset x timeframe; tokens from `new_market`, else one Gamma read per window. |
| `hub.py` | `MarketDataHub`: the public API; one `ClobMarketStream` per asset x timeframe, merged into one status. |

## API (`polymarket_exec/marketdata/hub.py`)

```python
hub = hub_module.current()          # set by the dashboard lifespan
hub.top(token_id)                   # TopOfBook | None
hub.levels(token_id, "bid", 10)     # ((price, size), ...) best first
hub.market("btc", "5m", "next")     # MarketRef | None
hub.quote("btc", "5m")              # MarketQuote(market, up, down) | None
hub.price("chainlink_twap60", "btc")        # PricePoint | None
hub.prices("binance", "btc", seconds=60)    # (PricePoint, ...)

listener = hub.listen()             # on any thread's running event loop
async for event in listener:        # TopChanged | Trade | PriceTick | MarketResolved | WindowOpened
    ...
listener.close()
```

Reads are safe from any thread: they return frozen objects that are replaced whole.
`TopChanged` fires only when the best bid/ask or their sizes move. Listener delivery
uses `call_soon_threadsafe`; a listener more than `maxsize` (2048) events behind loses
its oldest events, and `snapshot().listener_drops` counts them.

The universe follows an ended window for 30 s, and until its `market_resolved` arrives
(at most 300 s after the end), so `MarketResolved` is not missed.
`MarketUniverse(await_resolution_s=0)` drops every ended window after the 30 s.

## FEEDS card

Four rows under the feed-monitor rows: "Polymarket books" (CLOB market WS; delay =
the slowest socket's median event latency, flagged past 2 s and STALE past 5 s with the
socket named; STALE after 45 s without data; DOWN names the sockets that are down, e.g.
"1 of 24 sockets down; btc-5m: ..."), and "Chainlink prices",
"Chainlink 60s TWAP", "Binance prices" (RTDS WS; delay = age of the newest print;
STALE past 10 s). The full FEEDS redesign is a later change.
