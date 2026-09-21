# Code Map — where to make what change

> **⚠ Mid-rebuild (`chore/cleanup-dead-code`, started 2026-09-21):** this doc, and the
> generated tables below, describe the tree as it stood BEFORE the dead-code removal and
> re-architecture pass. Whole modules named here as WIRED are being deleted; `gen_docs.py`
> itself has a known bug (skips relative imports) that this pass fixes late, not first —
> so don't trust the wired/dead calls here until that lands. Treat this file as informative
> history, not routing, until the rebuild's Stage F regenerates it for real.

> Read this first. This repo is **two coupled code trees**, not one. Most doc confusion
> comes from treating one as "the codebase" and the other as legacy. Both are LIVE.

## The two trees

| Concern | Edit here |
|---|---|
| Trading loop, shared pricing math, paper fills | **`polymarket_bot/`** — `paper.py:run_paper_loop` is *the* loop; no strategy is loaded (v0 archived 2026-09-13, see `docs/archive/v0-strategy.md`) |
| Execution gates, live CLOB executor, connectors, dashboard, recorder, backtest harness | **`polymarket_exec/`** — `execution/gate.py:RiskGate`, `execution/live.py:LiveExecutor` |
| Config, DB schema, logging (foundation; imported by both, imports neither) | top-level **`config.py` / `db.py` / `logging_setup.py`** |

They are **bidirectionally coupled**: the FastAPI dashboard imports `polymarket_bot.*`; `polymarket_bot` imports back into `polymarket_exec.{execution,connectors}`.

## "I want to change X → edit Y"

| Change | File |
|---|---|
| Plug in a **new strategy** (the loop's entry decision) | `polymarket_bot/paper.py:_build_snapshot` — the `NO_STRATEGY_REASON` block |
| Shared pricing math (fair value, sigma) used by shadow/daily/backtests | `polymarket_bot/strategy.py` (NOT `polymarket_exec/strategy/` — that only feeds backtests) |
| Risk limits / kill-switch / daily-loss halt | `polymarket_exec/execution/gate.py` |
| Live order placement | `polymarket_exec/execution/live.py` |
| Add/modify a market or price feed | `polymarket_exec/connectors/` |
| Where the app reads Polymarket order books | the hub, everywhere but the live executor: `polymarket_bot/paper.py:_fetch_clob_book` (the BTC loop, the hourly engine and the shadow roster through it) and `polymarket_exec/ops/dashboard/quote_feed.py` (the order ticket; REST still reads `min_order_size` once a window). `polymarket_exec/execution/live.py:_book_context` still reads REST at order time (it needs `tick_size` and `min_order_size`) |
| Feed health checks behind the FEEDS card | `polymarket_exec/ops/feed_monitor.py` (checks) + `ops/dashboard/panels/feeds.py` (card: Connection / Used for / Used by per row — static facts in its `_REST_FEEDS` / `_FLOW_FEEDS` / `_MACRO_FEEDS` tables — plus the Polymarket market grid) |
| Venue trade-flow feeds (Binance spot/perp/liquidations, Kraken spot/futures) | `polymarket_exec/ops/flow_recorder.py` (recording) + `connectors/venue_flow.py`, `connectors/venue_messages.py`, `connectors/ws_runner.py` + `storage/venue_flow_store.py` (tables `venue_flow_hourly`, `venue_snapshot`) |
| Macro feeds (BLS/BEA/Census release schedules, Fed calendar, ForexFactory week + consensus) | `polymarket_exec/ops/macro_recorder.py` (recording; a new source is one more `MacroSource`) + `connectors/macro_calendar.py` (parsers, categories) + `storage/macro_store.py` (tables `macro_events`, `macro_consensus`; each source's schedule survives restarts in `config` keys `macro.feed_state.*`) |
| Want a live Polymarket market (stream it on demand) | `hub.want(asset, timeframe, owner, hot=False)` in `polymarket_exec/marketdata/hub.py` — returns a `Demand` (`release()` or a `with` block gives it up; `hub.release(owner)` drops all of an owner's); reads return None until the market streams; a released market lingers `demand_linger_s` (60 s), then its sockets stop. `hot=True` (an owner pricing decisions off it) also runs the fresh REST `/book` poll on its current window |
| A strategy needs live prices | `hub.want(asset, timeframe, owner, hot=True)`, then `await hub.wait_ready(asset, timeframe, timeout_s)` and read `hub.quote(asset, timeframe)` / `hub.book_top(token_id, max_age_s)` per decision, or `hub.listen()` to be pushed every change. Module-level `hub.want` / `hub.release` / `hub.book_top` / `hub.wait_ready` work with or without a hub in the process |
| Live Polymarket books/prices for strategies (Up/Down books, trades, resolutions; Chainlink, Chainlink TWAP and Binance prices over WebSockets) | `polymarket_exec/marketdata/hub.py` (public API: `current()`, `want`/`release`/`wanted`/`hot`, `top`, `book_top`, `wait_ready`, `quote`, `price`, `listen`, `snapshot`; `hedge=` sets connections per asset x timeframe, `pinned=` fixed demand, `poll_hz=` the fresh REST rate) + `marketdata/clob_shard.py` (redundant connections, freshest served) + `marketdata/rest_poll.py` (the REST `/book` poll racing the sockets on hot markets) + `marketdata/universe.py` (resolves only the wanted markets) + `marketdata/{clob_stream,rtds_stream,order_book,clob_messages}.py`; design in `docs/superpowers/specs/2026-09-16-polymarket-ws-marketdata-design.md` |
| Dashboard panel / UI | `polymarket_exec/ops/dashboard/panels/` |
| An env knob / default | `config.py` + document in `.env.example` |
| A new DB column | `db.py` migration dict (NOT the `SCHEMA` literal) |
| Start/Stop behavior | `polymarket_bot/controller.py` |

## Runtime flow

```
main.py ─ singleton lock + init_db ─▶ uvicorn ─▶ polymarket_exec/ops/dashboard/app.py (FastAPI :7860)
   operator ▶ Start ─▶ polymarket_bot/controller.py:request_start ─(daemon thread)─▶ polymarket_bot/paper.py:run_paper_loop
       DISCOVER market → FEED (chainlink_settlement) → SIGNAL (none loaded — v0 archived)
       → RISK (polymarket_exec/execution/gate.py:RiskGate) → EXECUTE (paper sim | live.py) → STORE (db.py)
   DASHBOARD reads DB read-only via polymarket_exec/ops/dashboard/ems.py → 8 panels/
```

`main.py` takes a singleton `fcntl` lock on `data/bot.lock`, runs `init_db`, then serves the FastAPI app via uvicorn. The Gradio `dashboard.py` branch is a fallback that **never executes** — `HAS_NEW_DASHBOARD` is always true.

## Dual-fork warnings (same logic in both trees — change the LIVE one)

- `sigma_per_second` / `fair_up_probability` / `signal_from_edge` exist in BOTH `polymarket_bot/strategy.py` (live) and `polymarket_exec/strategy/*` (backtest/tests). Editing the `polymarket_exec` copy does **not** change live behavior.
- `RiskGate` (`polymarket_exec/execution/gate.py`, LIVE) vs `RiskService` (`polymarket_exec/execution/risk.py`, DEAD on the live path).

## WIRED in the table ≠ live on the trading path

The generated inventory below counts **direct non-test importers**. A non-zero count means "something imports this," not "this runs when the bot trades." Several modules import-resolve but are dead on the live path. Edit them only if you mean to touch backtests/tests — never expecting a runtime trading effect:

| Module | Table says | Reality |
|---|---|---|
| `dashboard.py` | WIRED (imported by `main.py`) | Never-taken Gradio fallback. The live UI is the FastAPI app. |
| `polymarket_exec/connectors/registry.py` | WIRED | Built, but the live loop **bypasses** the registry/ABC architecture; the live feed is `connectors/chainlink_settlement.py` only. Its importer is the dead controller. |
| `polymarket_exec/connectors/{base,binance,chainlink,polymarket}.py` | DEAD? | The ABC connectors behind the unused registry. Not on the live path. |
| `polymarket_exec/execution/risk.py` (`RiskService`) | WIRED | Only importer is the **dead** `polymarket_exec/ops/controller.py`. The LIVE risk check is `execution/gate.py:RiskGate`. |
| `polymarket_exec/execution/paper.py` (`PaperExecutionManager`) | WIRED | Live paper fills are journaled **inline in `polymarket_bot/paper.py`**, not via this class. |
| `polymarket_exec/ops/controller.py` (`BotController`) | DEAD? | The live controller is `polymarket_bot/controller.py`. |

Genuine **DEAD?** (no importers at all): `polymarket_exec/backtest/{conditional,harness}.py`, `connectors/{base,binance,chainlink,polymarket}.py`, `ops/controller.py`, `ops/dashboard/panels/_shared.py`, `storage/replay.py`, `strategy/signal.py`, `polymarket_bot/chronos_signal.py`.

## Live trading is built and gated

Live exists (`execution/live.py:LiveExecutor`). It activates ONLY when the operator clicks LIVE in the dashboard (the consent — no env phrase) AND a private key AND a coherent wallet AND a clean config parse. **Agents never flip the gate. The operator launches.**

Env knobs: `BTC_TRADE_*` are canonical; `BTC_LIVE_*` are deprecated read-aliases.

## Module status (generated)

> The inventory `Status`/`Importers` columns are **mechanical** (direct non-test importer
> count) — read the "WIRED ≠ live" callouts above for semantic truth. `pkg` (package marker)
> and `cli` (entrypoint script) statuses with zero importers are normal, not dead.

<!-- BEGIN GENERATED:summary -->
- **Trees:** `polymarket_bot/` = live loop + signal math; `polymarket_exec/` = execution/connectors/dashboard/backtest; top-level `config.py`/`db.py`/`logging_setup.py` = foundation. Both ACTIVE, bidirectionally coupled.
- **Entry:** `python main.py` → FastAPI `polymarket_exec/ops/dashboard/app.py`; loop starts on operator ▶ Start → `polymarket_bot/controller.py:request_start`.
- **Tests:** 964.
- **Built-but-dead (do not edit expecting runtime effect):** `polymarket_bot/history.py`, `polymarket_exec/ops/dashboard/panels/_shared.py`.
<!-- END GENERATED:summary -->

<!-- BEGIN GENERATED:inventory -->
| Module | Status | Importers | Role |
|---|---|---|---|
| `config.py` | WIRED | 27 | Configuration for the local Polymarket crypto trading lab. |
| `db.py` | WIRED | 16 | SQLite storage for the local Polymarket crypto trading lab. |
| `logging_setup.py` | WIRED | 19 | Structured JSON logging with structlog. Module + trade_id context. |
| `main.py` | cli | 0 | Entrypoint for the BTC 5-minute paper trading system. |
| `polymarket_bot/__init__.py` | pkg | 12 | BTC 5-minute paper-trading package. |
| `polymarket_bot/backtest.py` | WIRED | 2 | Backtest and optimize the BTC 5-minute binary strategy on local history. |
| `polymarket_bot/controller.py` | WIRED | 1 | Start/stop controller for the BTC 5-minute trader (paper default, live opt-in). |
| `polymarket_bot/daily/__init__.py` | pkg | 1 | Daily (24h-window) altcoin Up/Down shadow strategy (issue #185). |
| `polymarket_bot/daily/ledger.py` | WIRED | 1 | Persistence for the daily altcoin scanner's paper positions. |
| `polymarket_bot/daily/market.py` | WIRED | 1 | Daily Up/Down market discovery and per-asset price/spot resolution. |
| `polymarket_bot/daily/scanner.py` | WIRED | 1 | The daily altcoin scanner's tick loop (issue #185). |
| `polymarket_bot/daily/signal.py` | WIRED | 1 | Fair-value scoring for the daily altcoin scanner. |
| `polymarket_bot/daily/types.py` | WIRED | 3 | Shared data contracts for the daily altcoin scanner. |
| `polymarket_bot/fees.py` | WIRED | 2 | Canonical Polymarket taker-fee math. |
| `polymarket_bot/history.py` | DEAD? | 0 | Load the user's exported Polymarket history for BTC sizing context. |
| `polymarket_bot/inventory.py` | WIRED | 1 | Every strategy family in this repo, including the ones that do nothing. |
| `polymarket_bot/maker/__init__.py` | pkg | 3 | (needs docstring) |
| `polymarket_bot/maker/filler.py` | WIRED | 1 | Decide whether a resting quote would really have filled, and settle it. |
| `polymarket_bot/maker/ledger.py` | WIRED | 3 | Paper ledger for resting maker quotes. |
| `polymarket_bot/maker/quoter.py` | WIRED | 1 | Decide what to rest, and where in the queue it lands. |
| `polymarket_bot/maker/runner.py` | WIRED | 1 | The maker loop: quote the favourite, watch the queue, settle on resolution. |
| `polymarket_bot/market_selection.py` | WIRED | 5 | Operator market selection: which crypto asset + window timeframe to trade. |
| `polymarket_bot/pairarb/__init__.py` | pkg | 0 | Leftovers of the two-sided 5m maker-quoting shadow line (#182, closed 2026-08-29). |
| `polymarket_bot/pairarb/market_index.py` | WIRED | 1 | Outcome-token -> market metadata resolver for the daily altcoin scanner. |
| `polymarket_bot/paper.py` | WIRED | 3 | BTC 5-minute trading engine (paper by default, live opt-in). |
| `polymarket_bot/runtime_knobs.py` | WIRED | 7 | Operator runtime knobs: single dashboard-editable source of truth (#206). |
| `polymarket_bot/strategies.py` | WIRED | 7 | Operator strategy switches: which strategies may open new positions. |
| `polymarket_bot/strategy.py` | WIRED | 5 | Shared BTC 5-minute binary strategy math. |
| `polymarket_exec/__init__.py` | pkg | 0 | BTC 5m Binary Pricing Model trading system. |
| `polymarket_exec/connectors/__init__.py` | pkg | 2 | Exchange and data connectors. |
| `polymarket_exec/connectors/chainlink_settlement.py` | WIRED | 2 | Settlement-aligned Chainlink BTC/USD feed via Polymarket endpoints (issue #21). |
| `polymarket_exec/connectors/macro_calendar.py` | WIRED | 2 | Pure parsers for US macro release calendars: BLS/BEA ICS, Census and Fed calendars, ForexFactory. |
| `polymarket_exec/connectors/updown_quote.py` | WIRED | 4 | Live top-of-book quote for the current window of any crypto Up/Down market. |
| `polymarket_exec/connectors/venue_flow.py` | WIRED | 3 | Closed-hour trade-flow bars and venue state snapshots for the Binance and Kraken feeds. |
| `polymarket_exec/connectors/venue_messages.py` | WIRED | 1 | Parsers for venue feed frames: Kraken spot/futures trades, Binance liquidations, perp state. |
| `polymarket_exec/connectors/ws_runner.py` | WIRED | 1 | Reconnecting WebSocket loop shared by the venue trade feeds (Kraken, Binance liquidations). |
| `polymarket_exec/core/__init__.py` | pkg | 0 | Core domain exceptions. |
| `polymarket_exec/core/exceptions.py` | WIRED | 2 | Custom exception hierarchy for the BTC 5m Binary Pricing Model trading system. |
| `polymarket_exec/core/model.py` | WIRED | 1 | The domain model the live path uses. |
| `polymarket_exec/execution/__init__.py` | pkg | 0 | Execution: the live CLOB executor and pre-trade risk gate. |
| `polymarket_exec/execution/gate.py` | WIRED | 5 | Venue-independent pre-trade risk gate (issue #64). |
| `polymarket_exec/execution/live.py` | WIRED | 6 | Live execution on the Polymarket CLOB via py-clob-client. |
| `polymarket_exec/marketdata/__init__.py` | pkg | 5 | Live Polymarket market data over WebSockets: Up/Down order books, trades, reference prices. |
| `polymarket_exec/marketdata/clob_messages.py` | WIRED | 5 | Pure parsers for the Polymarket CLOB market channel: one text frame in, typed events out. |
| `polymarket_exec/marketdata/clob_shard.py` | WIRED | 1 | One asset x timeframe's market-channel connections: N redundant sockets, the freshest served. |
| `polymarket_exec/marketdata/clob_stream.py` | WIRED | 4 | Reconnecting connection to the Polymarket CLOB market channel (books, trades, lifecycle). |
| `polymarket_exec/marketdata/hub.py` | WIRED | 5 | Live Polymarket books, trades and reference prices for strategies (the module's public API). |
| `polymarket_exec/marketdata/order_book.py` | WIRED | 3 | One token's order book, rebuilt from CLOB snapshots and absolute level changes. |
| `polymarket_exec/marketdata/rest_poll.py` | WIRED | 1 | A light REST /book poll running alongside the sockets on the markets in use. |
| `polymarket_exec/marketdata/rtds_stream.py` | WIRED | 2 | Chainlink, Chainlink 60 s TWAP and Binance prices from Polymarket's RTDS WebSocket. |
| `polymarket_exec/marketdata/universe.py` | WIRED | 1 | Which Polymarket Up/Down windows to follow, and their outcome token ids. |
| `polymarket_exec/ops/__init__.py` | pkg | 3 | Operator controls and background recorders. |
| `polymarket_exec/ops/dashboard/__init__.py` | pkg | 2 | FastAPI dashboard for BTC 5m Binary Pricing Model trading system. |
| `polymarket_exec/ops/dashboard/app.py` | WIRED | 1 | FastAPI dashboard for the local Polymarket crypto trading lab. |
| `polymarket_exec/ops/dashboard/execution_view.py` | WIRED | 1 | Execution view orchestrator (#37). |
| `polymarket_exec/ops/dashboard/panels/__init__.py` | pkg | 2 | Dashboard panels. |
| `polymarket_exec/ops/dashboard/panels/_data.py` | WIRED | 2 | Read-only SQLite loaders for dashboard panels. |
| `polymarket_exec/ops/dashboard/panels/_shared.py` | DEAD? | 0 | Shared rendering primitives for dashboard panels. |
| `polymarket_exec/ops/dashboard/panels/_wallet.py` | WIRED | 1 | Polymarket wallet size for the EMS ribbon: cash + open positions value. |
| `polymarket_exec/ops/dashboard/panels/blotter.py` | WIRED | 1 | Trade blotter: open positions on top, last 12 closed below, mode chip per row. |
| `polymarket_exec/ops/dashboard/panels/controls.py` | WIRED | 1 | Order-size ticket (#50, #89): the operator's share count, priced live. |
| `polymarket_exec/ops/dashboard/panels/daily_altcoin.py` | WIRED | 1 | Daily altcoin Up/Down scanner panel (issue #185). |
| `polymarket_exec/ops/dashboard/panels/decision_engine.py` | WIRED | 1 | Decision engine panel: inputs → computation → final banner + tail. |
| `polymarket_exec/ops/dashboard/panels/feeds.py` | WIRED | 1 | FEEDS card: every live upstream feed — how it connects, what it is for, who uses it, health. |
| `polymarket_exec/ops/dashboard/panels/maker.py` | WIRED | 1 | MAKER card: every quote we rested, including the ones that never filled. |
| `polymarket_exec/ops/dashboard/panels/market.py` | WIRED | 1 | Live market panel: probability gauge, UP/DOWN book, basis. |
| `polymarket_exec/ops/dashboard/panels/market_selector.py` | WIRED | 1 | Topbar market selector: asset buttons over timeframe buttons. |
| `polymarket_exec/ops/dashboard/panels/performance.py` | WIRED | 1 | Performance / alpha panel: combined equity curve + LIVE/PAPER mini-cards. |
| `polymarket_exec/ops/dashboard/panels/ribbon.py` | WIRED | 1 | Top status ribbon: wallet, P&L, open-position (live) P&L, loss-halt control. |
| `polymarket_exec/ops/dashboard/panels/settings.py` | WIRED | 1 | Settings panel: every dashboard-editable runtime knob (#206). |
| `polymarket_exec/ops/dashboard/panels/strategies.py` | WIRED | 1 | MY STRATEGIES card: every strategy family in the repo, hiding none of them. |
| `polymarket_exec/ops/dashboard/panels/tca.py` | WIRED | 1 | TCA panel: quoted spread, half-spread, edge capture, Brier calibration. |
| `polymarket_exec/ops/dashboard/quote_feed.py` | WIRED | 2 | Background quote poller for the dashboard's order-size ticket. |
| `polymarket_exec/ops/feed_monitor.py` | WIRED | 3 | Always-on feed monitor: keeps the live feeds connected and checks each one. |
| `polymarket_exec/ops/flow_recorder.py` | WIRED | 3 | Always-on recorder for venue trade-flow feeds: Binance spot/perp/liquidations and Kraken. |
| `polymarket_exec/ops/macro_recorder.py` | WIRED | 3 | Always-on recorder for macro feeds: each source polled on its own cadence into SQLite. |
| `polymarket_exec/storage/__init__.py` | pkg | 2 | Persistence layer — database, recording, and replay. |
| `polymarket_exec/storage/macro_store.py` | WIRED | 1 | SQLite read/write for the macro calendar: scheduled events and first-seen consensus values. |
| `polymarket_exec/storage/repositories/__init__.py` | pkg | 1 | Repositories: the only place raw SQL against a given table should live. |
| `polymarket_exec/storage/repositories/positions.py` | WIRED | 1 | The only place ``paper_positions`` is read or written — issue #266 step 2. |
| `polymarket_exec/storage/venue_flow_store.py` | WIRED | 1 | SQLite read/write for venue flow hour bars and perp venue snapshots. |
| `tools/backtest_btc_strategy.py` | cli | 0 | Run the BTC strategy backtest and parameter optimizer. |
| `tools/chainlink_lead_lag.py` | cli | 0 | Chainlink-vs-Binance BTC lead-lag analysis (issue #57). |
| `tools/fetch_polymarket_trades.py` | cli | 0 | Pull this account's Polymarket trade history via the CLOB API → CSV. |
| `tools/forecast_journal.py` | cli | 0 | Slow-market forecasting-skill pilot: journal + scoring (issue #162). |
| `tools/gen_docs.py` | cli | 0 | Generate the machine-derived sections of the agent docs. |
| `tools/live_detect_wallet.py` | cli | 0 | Find your MetaMask Polymarket wallet and write it into .env (#34). |
| `tools/live_preflight.py` | cli | 0 | Live-launch preflight: verify the .env wallet config end to end (issue #32). |
| `tools/offline_replay.py` | cli | 0 | Offline replay of the BTC 5-m pricing-model strategy on HF Polymarket data. |
| `tools/reconcile_live_ledger.py` | cli | 0 | Reconcile the live paper-ledger against the REAL Polymarket account (issue #102). |
| `tools/venue_recorder.py` | cli | 1 | Venue recorder — the research program's one blocking build item (C17). |
| `tools/wallet_research/analyze.py` | cli | 0 | Score wallets on the scanned 1h/24h Up-or-Down universe. |
| `tools/wallet_research/filter_test.py` | cli | 0 | Does the slippage filter have an edge, independent of whose fill it is? |
| `tools/wallet_research/holdout_test.py` | cli | 0 | Does the taker screen predict, or does it fit noise? |
| `tools/wallet_research/inspect_wallet.py` | cli | 0 | Look hard at one wallet before copying it. |
| `tools/wallet_research/maker_band.py` | cli | 0 | Is the maker edge on favourites real, and what shape is it? |
| `tools/wallet_research/maker_edge.py` | cli | 0 | What did real resting orders actually earn, fee-free? |
| `tools/wallet_research/maker_holdout.py` | cli | 0 | Does a wallet's maker edge carry into the NEXT month, or is it last month's luck? |
| `tools/wallet_research/maker_label.py` | cli | 0 | Label every stored fill maker or taker, one API call per market. |
| `tools/wallet_research/maker_test.py` | cli | 0 | Could a resting bid have filled, and would it have paid? |
| `tools/wallet_research/maker_vs_taker.py` | cli | 0 | Measure adverse selection: do MAKER fills realise worse than TAKER fills? |
| `tools/wallet_research/rank.py` | cli | 0 | Rank wallets by copier edge, not by how much money they made. |
| `tools/wallet_research/recent_wallets.py` | cli | 0 | Who actually made money on the 15m markets in the last few days. |
| `tools/wallet_research/scan.py` | cli | 0 | Scan resolved 1h/24h crypto Up-or-Down markets and build per-wallet ledgers. |
| `tools/wallet_research/strike_scan.py` | cli | 0 | Scan crypto STRIKE markets ("will X be above $Y on <date>"). |
| `tools/wallet_research/taker_screen.py` | cli | 0 | Find wallets that made money on crypto Up/Down as TAKERS, recently. |
| `tools/wallet_research/validate.py` | cli | 0 | Cross-check a wallet's reconstructed PnL and measure how copyable it is. |
<!-- END GENERATED:inventory -->

See `docs/FILE_MAP.md` for the full generated index.
