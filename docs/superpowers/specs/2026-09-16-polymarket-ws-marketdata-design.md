# Polymarket WebSocket market data — `polymarket_exec/marketdata/`

Status: built (2026-09-16); markets are streamed on demand since 2026-09-17 (see
"On-demand streams"). Consumers are not migrated yet: the order ticket, the feed
monitor, `paper._fetch_clob_book`, `live._book_context` and the daily scanner still
poll REST. Moving them onto the hub (each calling `hub.want`) is the next step.

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
anchor) and turn it off on the others. The announcements are not data for the followed
tokens: they don't reset the 45 s silence watchdog or the reconnect backoff, or a socket
whose subscription went quiet would never be resubscribed. A check on 2026-09-16 showed
that `market_resolved` also reaches a socket that subscribed after the window ended
(116-126 s after the end).

## Hedged connections for the busiest markets

A single connection to a busy market still stalls for seconds at times, and the stalls
are independent: three sockets on the same BTC 5m tokens stalled at different moments
(one at t=75-85 s, p50 up to 4.0 s, while the other two stayed near 101-110 ms). A hub
holding only btc-5m blocked its event loop for at most ~1-7 ms while that connection's
latency climbed from 105 ms to 9.7 s, so the delay is in delivery, not processing. An
Up-only subscription got 97-99% of the frames and bytes of a both-token one, so
dropping a token saves nothing.

So an asset x timeframe can run N connections (`clob_shard.ClobShard`;
`MarketDataHub(hedge=...)`, keys `(asset, timeframe)`, `"asset-timeframe"` or an asset).
The default is two for btc 5m, 15m and 1h and one for everything else (27 connections).

- Every connection keeps its own books; events are never applied across connections.
  A new connection starts with none and rebuilds from its snapshot. When the serving
  connection drops or is replaced, its tokens move to the freshest connection that is
  still up; with none, reads keep the last values and say `live=False`.
- Reads serve the connection furthest along the event stream: its newest server time,
  then the number of events applied at that millisecond. Both connections receive the
  same events in the same order, and a trade's sweep sends several top-changing
  messages in one millisecond, so the timestamp alone is not enough. An exact tie goes
  to a connection whose smoothed latency is more than 25 ms lower.
- `TopChanged` and `Trade` are pushed once, by whichever connection delivers first.
  Nothing behind what was already pushed goes out. Trades are known by (token, time,
  price, size, side, transaction hash), and alike trades are counted per connection:
  live, one socket delivered two trades in one millisecond at the same price, size and
  side (different hashes), and one transaction can fill several makers alike. `Trade`
  carries the hash. `MarketResolved` and `WindowOpened` go out once per market.
- A connection more than 3 s behind the freshest connection of its group is replaced,
  but only while another connection of the group is up and within 1 s of the front.
  So is a new connection that has applied no book data (not even its snapshot) after
  3 s on our clock, once the front has moved more than 3 s on.
  If all are 10 s behind their own best, one is replaced at a time. Each replacement is
  logged (`marketdata.clob_recycle`, with the group and the lag). With one connection,
  the stream's own rule (10 s behind its best) applies as before.
- Group status: served latency (first deliveries), served staleness (now minus the
  newest served event's server time), leader switches, stalls avoided (a connection
  fell >1 s behind while another kept serving), recycles, and each connection's status.
  The FEEDS delay is the worst group's served latency.

Live, 300 s on the full grid (2026-09-16 18:29 UTC, a busy stretch spanning a 5m and
15m close):

| Group | Served latency p50/p90/max | Connection #0 | Connection #1 |
|---|---|---|---|
| btc-5m | 100 / 290 / 2,090 ms | 110 / 380 / 4,485 ms | 110 / 560 / 3,778 ms |
| btc-15m | 100 / 220 / 1,211 ms | 110 / 610 / 3,818 ms | 110 / 530 / 3,589 ms |
| btc-1h | 100 / 190 / 1,650 ms | 100 / 270 / 5,172 ms | 110 / 360 / 5,169 ms |
| eth-5m (one connection) | 120 / 1,350 / 6,542 ms | 120 / 1,370 / 6,542 ms | — |

There were 9 recycles, 14 stalls avoided and 12 resolutions (each pushed once), and all
three REST checks agreed with the hub. Total traffic was 2.86 MiB/s (~4,700 frames/s,
hedged BTC included), at 32% of a core and 85 MiB RSS. The unhedged ETH groups still
stalled for up to ~7 s; hedging them would add roughly 450 KiB/s at that level of
activity.

## On-demand streams (2026-09-17)

Streaming the whole grid all the time cost 2.0-2.9 MiB/s (~170-245 GiB a day) and
24-32% of a core, and those sockets competed for this link with the markets actually
used. So the grid (assets x timeframes) is now the set of AVAILABLE markets, and the
market channel only carries the markets something uses:

- `hub.want(asset, timeframe, owner) -> Demand` registers a use (any thread or event
  loop; idempotent per owner and market). The market's first owner starts its socket
  group at once (the hub's loops are woken, not left to their 2 s tick; hedging rules
  unchanged) and subscribes its current and next window. `Demand.release()`, leaving a
  `with` block, or `hub.release(owner, asset=None, timeframe=None)` gives uses up;
  `hub.wanted()` shows the owners by market. `MarketDataHub(pinned=...)` sets fixed
  demand that cannot be released (the dashboard lifespan passes none for now).
- After the last release the market keeps streaming for `demand_linger_s` (60 s), so
  an owner that lets go and takes it again every tick causes no churn. Then its tokens
  are dropped: each connection closes its socket (an emptied socket would still receive
  the platform-wide `new_market` broadcast), which is not counted as a reconnect, and
  the group drops its books and its served-latency record.
- Reads for a market that is not streaming return None (`quote`, `market`, `top`,
  `levels`), never an old book.
- The universe resolves tokens (Gamma) only for markets that stream. `new_market`
  announcements are kept for every grid market, and looked-up tokens while their window
  is current or next, so wanting a market again is instant when they are known. With
  no socket open, no announcements arrive, so a first want after a quiet spell usually
  needs one Gamma read per window.
- The RTDS prices (Chainlink, Chainlink 60 s TWAP, Binance) stay always on: a few
  KiB/s, and strategies need their history warm.
- `snapshot().grid` has one `GridMarket` per available market: state (STREAMING,
  LINGERING, AVAILABLE), owners, streaming since, linger time left, connections
  up/total, served latency p50/p90, bytes/s and tokens; `clob_kib_s` and `rtds_kib_s`
  are the totals (characters per second over the last 10 whole seconds; RTDS after
  decompression).

Live check (2026-09-17 08:21 UTC, one process, `demand_linger_s=10`):

| Phase | Result |
|---|---|
| Nothing wanted, 30 s | no CLOB socket, 0 CLOB bytes, no Gamma read; RTDS 5.2 KiB/s steady (12.6 KiB/s in the first 10 s, history snapshots); 1.1% of a core |
| btc-5m + btc-1h wanted, 60 s | first socket 0.8 s after `want`, first live quotes after 0.9-1.2 s (4 Gamma reads); 4 sockets; CLOB 631 KiB/s (951 frames/s; 250 KiB/s in an earlier run); served latency p50/p90/max btc-5m 50/100/312 ms, btc-1h 60/180/334 ms; no reconnects or recycles; 12.6% of a core, 77 MiB RSS |
| Released | LINGERING for 10 s (quotes still live, 479 KiB/s); AVAILABLE at 10.0 s; all 4 sockets closed 10.2-10.3 s after the release, not counted as reconnects; reads None; 0 CLOB bytes in the next 11.5 s |

## Modules

| Module | Role |
|---|---|
| `clob_messages.py` | Pure `parse_frame(text) -> [event]`; unknown or malformed objects become `Unknown`. |
| `order_book.py` | `OrderBook` per token; `top()` is O(1) and returns a frozen `TopOfBook`. |
| `clob_stream.py` | `ClobMarketStream`: diffs as operation frames, PING 10 s, 45 s silence watchdog on the followed tokens' events (resubscribe, then reconnect), reconnect when >10 s behind its best, 1-30 s jittered backoff, stop within ~1 s; no tokens, no socket (an emptied set closes it). `Throughput` counts frames and characters per second for both streams. |
| `rtds_stream.py` | `RtdsPriceStream`: `PricePoint`s per (source, asset), 900-point history, gap count, 30 s silence reconnect. Shares the market channel's TLS context (`clob_stream.tls_context`): building one blocks the loop ~13 ms, and ~28 sockets opening at once froze it for 215-340 ms. |
| `universe.py` | `MarketUniverse`: current + next window per wanted asset x timeframe (`set_wanted`); tokens from `new_market`, else one Gamma read per window. `select()` rolls windows over from the tokens already known (no I/O); the hub runs it every 2 s (and at once on a demand change) apart from the lookups, so a slow Gamma read never holds up a window that starts. |
| `clob_shard.py` | `ClobShard`: one asset x timeframe's N connections, per-connection books, the freshest served, events pushed once, lagging connections replaced. |
| `hub.py` | `MarketDataHub`: the public API; demand (`want` / `release` / `wanted`, `pinned=`, `demand_linger_s`); one `ClobShard` per asset x timeframe (`hedge=` sets the connections), merged into one status plus the per-market grid. A stop cancels Gamma lookups in flight, so `run` returns within ~1 s. |

## API (`polymarket_exec/marketdata/hub.py`)

```python
hub = hub_module.current()          # set by the dashboard lifespan
                                    # (MarketDataHub(hedge={"btc-5m": 2, ...}) to build one)
demand = hub.want("btc", "5m", "bot loop")   # stream it (any thread); idempotent
with hub.want("eth", "1h", "panel"):         # or hold it for a block
    ...
demand.release()                    # or hub.release("bot loop") for all of it
hub.wanted()                        # {("btc", "5m"): frozenset({"bot loop"}), ...}
hub.top(token_id)                   # TopOfBook | None (.live: see below)
hub.levels(token_id, "bid", 10)     # ((price, size), ...) best first | None
hub.market("btc", "5m", "next")     # MarketRef | None
hub.quote("btc", "5m")              # MarketQuote(market, up, down, live) | None
hub.price("chainlink_twap60", "btc")        # PricePoint | None
hub.prices("binance", "btc", seconds=60)    # (PricePoint, ...)

listener = hub.listen()             # on any thread's running event loop
async for event in listener:        # TopChanged | Trade | PriceTick | MarketResolved | WindowOpened
    ...
listener.close()
```

Reads are safe from any thread: they return frozen objects that are replaced whole.
They return None for a market that is not streaming (want it first; its first book
arrives ~0.2 s after the socket subscribes, plus a Gamma read if its tokens are unknown).
`TopOfBook.live` is False while no connection that is up serves the token (the socket
dropped, or a new one has not delivered its snapshot yet): the values are the last ones
seen, and `levels` returns the last levels seen. `MarketQuote.live` needs both tops live.
`TopChanged` fires only when the best bid/ask or their sizes move. Listener delivery
uses `call_soon_threadsafe`; a listener more than `maxsize` (2048) events behind loses
its oldest events, and `snapshot().listener_drops` counts them.

The universe follows an ended window for 30 s, and until its `market_resolved` arrives,
so `MarketResolved` is not missed (the server only sends it for subscribed tokens). The
wait is capped at 5 min for 5m/15m and 45 min for 1h/1d: measured on 2026-09-17 from the
window end, the resolution reached the socket after ~2.5 min for 5m/15m, 12-28 min for
1h and ~15 min for 1d. `MarketUniverse(await_resolution_s=0)` drops every ended window
after the 30 s; a mapping (e.g. `{"1h": 600}`) sets the wait per timeframe.

## FEEDS card

Columns: Feed | Connection | Used for | Used by | Delay | Status. Connection says how
the data arrives and how often ("WebSocket · RTDS", "WebSocket · CLOB · on demand",
"REST · every 10 s", "REST · hourly", "REST · every 6 h"; REST cadences come from the
monitor and the recorders), with the endpoint on hover. Used by names the components
that consume the feed today (e.g. "bot loop · shadow roster · FEEDS check", "flow
recorder"); the RTDS price rows say "none yet (kept warm)" until strategies read them.

Four rows under the feed-monitor rows:

- "Polymarket books" (WebSocket · CLOB · on demand). Used by = the owners of the
  markets in use. Its delay and status come from the markets in use only: delay = the
  worst served latency p50, flagged past 2 s and STALE past 5 s with the market named;
  STALE after 45 s without data; DOWN when a market in use has no connection up after
  its own 30 s grace (e.g. "1 of 2 markets in use down; btc-5m: ..."); OK notes
  connections that are reconnecting or markets still connecting; IDLE when nothing is
  in use. Under it, a grid of every available market (assets as rows, timeframes as
  columns): the served latency for a market in use (coloured by its health; "connecting"
  or "down" when there is none), "lingering" and "available" otherwise. Hover: used by,
  connections up/total, p50/p90, KiB/s. Lingering and available markets never count as
  issues.
- "Chainlink prices", "Chainlink 60s TWAP", "Binance prices" (WebSocket · RTDS; delay =
  age of the newest print; STALE past 10 s).
