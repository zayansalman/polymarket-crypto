# Code Map — where to make what change

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
| Feed health checks behind the FEEDS card | `polymarket_exec/ops/feed_monitor.py` (checks) + `ops/dashboard/panels/feeds.py` (card) |
| Venue trade-flow feeds (Binance spot/perp/liquidations, Kraken spot/futures) | `polymarket_exec/ops/flow_recorder.py` (recording) + `connectors/venue_flow.py`, `connectors/venue_messages.py`, `connectors/ws_runner.py` + `storage/venue_flow_store.py` (tables `venue_flow_hourly`, `venue_snapshot`) |
| Macro feeds (BLS/BEA/Census release schedules, Fed calendar, ForexFactory week + consensus) | `polymarket_exec/ops/macro_recorder.py` (recording; a new source is one more `MacroSource`) + `connectors/macro_calendar.py` (parsers, categories) + `storage/macro_store.py` (tables `macro_events`, `macro_consensus`; each source's schedule survives restarts in `config` keys `macro.feed_state.*`) |
| Live Polymarket books/prices for strategies (Up/Down books, trades, resolutions; Chainlink, Chainlink TWAP and Binance prices over WebSockets) | `polymarket_exec/marketdata/hub.py` (public API: `current()`, `top`, `quote`, `price`, `listen`) + `marketdata/{clob_stream,rtds_stream,universe,order_book,clob_messages}.py`; design in `docs/superpowers/specs/2026-09-16-polymarket-ws-marketdata-design.md` |
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
- **Tests:** 1211.
- **Built-but-dead (do not edit expecting runtime effect):** `polymarket_bot/chronos_signal.py`, `polymarket_exec/backtest/conditional.py`, `polymarket_exec/backtest/harness.py`, `polymarket_exec/connectors/base.py`, `polymarket_exec/connectors/binance.py`, `polymarket_exec/connectors/chainlink.py`, `polymarket_exec/connectors/polymarket.py`, `polymarket_exec/ops/controller.py`, `polymarket_exec/ops/dashboard/panels/_shared.py`, `polymarket_exec/storage/replay.py`, `polymarket_exec/strategy/signal.py`.
<!-- END GENERATED:summary -->

<!-- BEGIN GENERATED:inventory -->
| Module | Status | Importers | Role |
|---|---|---|---|
| `config.py` | WIRED | 31 | Configuration for the local Polymarket crypto trading lab. |
| `dashboard.py` | WIRED | 1 | Local Gradio dashboard for BTC 5-minute paper trading. |
| `db.py` | WIRED | 16 | SQLite storage for the local Polymarket crypto trading lab. |
| `logging_setup.py` | WIRED | 18 | Structured JSON logging with structlog. Module + trade_id context. |
| `main.py` | cli | 0 | Entrypoint for the BTC 5-minute paper trading system. |
| `polymarket_bot/__init__.py` | pkg | 13 | BTC 5-minute paper-trading package. |
| `polymarket_bot/backtest.py` | WIRED | 4 | Backtest and optimize the BTC 5-minute binary strategy on local history. |
| `polymarket_bot/chronos_signal.py` | DEAD? | 0 | Layer 3 — Chronos time-series ensemble (stub). |
| `polymarket_bot/controller.py` | WIRED | 2 | Start/stop controller for the BTC 5-minute trader (paper default, live opt-in). |
| `polymarket_bot/daily/__init__.py` | pkg | 1 | Daily (24h-window) altcoin Up/Down shadow strategy (issue #185). |
| `polymarket_bot/daily/ledger.py` | WIRED | 1 | Persistence for the daily altcoin scanner's shadow positions. |
| `polymarket_bot/daily/market.py` | WIRED | 1 | Daily Up/Down market discovery and per-asset price/spot resolution. |
| `polymarket_bot/daily/scanner.py` | WIRED | 1 | The daily altcoin scanner's tick loop (issue #185). |
| `polymarket_bot/daily/signal.py` | WIRED | 1 | Fair-value scoring for the daily altcoin scanner. |
| `polymarket_bot/daily/types.py` | WIRED | 3 | Shared data contracts for the daily altcoin scanner. |
| `polymarket_bot/history.py` | WIRED | 3 | Load the user's exported Polymarket history for BTC sizing context. |
| `polymarket_bot/market_selection.py` | WIRED | 4 | Operator market selection: which crypto asset + window timeframe to trade. |
| `polymarket_bot/pairarb/__init__.py` | pkg | 1 | Two-sided maker quoting on 5-minute Up/Down markets — shadow only (#182). |
| `polymarket_bot/pairarb/feed.py` | WIRED | 1 | Fill feed for the copier — one interface, two transports (#182). |
| `polymarket_bot/pairarb/fills.py` | WIRED | 1 | Back-of-queue maker fill simulation and window settlement (#182). |
| `polymarket_bot/pairarb/ledger.py` | WIRED | 1 | Persistence for the two-sided pair shadow tester (#182). |
| `polymarket_bot/pairarb/market_index.py` | WIRED | 2 | Outcome-token -> market metadata resolver for the 5m Up/Down family (#182). |
| `polymarket_bot/pairarb/mirror.py` | WIRED | 2 | Copy-trade mirror — what following a target wallet would actually cost (#182). |
| `polymarket_bot/pairarb/onchain.py` | WIRED | 2 | On-chain fill detection via Polygon ``OrderFilled`` logs (#182). |
| `polymarket_bot/pairarb/quoter.py` | WIRED | 1 | Two-sided quote placement for the 5m Up/Down pair strategy (#182). |
| `polymarket_bot/pairarb/types.py` | WIRED | 3 | Shared data contracts for the two-sided pair quoter (#182). |
| `polymarket_bot/paper.py` | WIRED | 6 | BTC 5-minute trading engine (paper by default, live opt-in). |
| `polymarket_bot/runtime_knobs.py` | WIRED | 7 | Operator runtime knobs: single dashboard-editable source of truth (#206). |
| `polymarket_bot/shadow/__init__.py` | pkg | 3 | Shadow forward-tester: candidate strategies logged and settled net of fees. |
| `polymarket_bot/shadow/fees.py` | WIRED | 6 | Polymarket taker-fee math for the shadow forward-tester. |
| `polymarket_bot/shadow/ledger.py` | WIRED | 2 | Persistence for the shadow forward-tester's would-be trades. |
| `polymarket_bot/shadow/runner.py` | WIRED | 2 | Shadow forward-tester runner. |
| `polymarket_bot/shadow/signals.py` | WIRED | 2 | Candidate strategies for the shadow forward-tester. |
| `polymarket_bot/shadow/types.py` | WIRED | 3 | Shared data contracts for the shadow forward-tester. |
| `polymarket_bot/strategy.py` | WIRED | 8 | Shared BTC 5-minute binary strategy math. |
| `polymarket_exec/__init__.py` | pkg | 0 | BTC 5m Binary Pricing Model trading system. |
| `polymarket_exec/backtest/__init__.py` | pkg | 0 | Backtesting harness and metrics. |
| `polymarket_exec/backtest/conditional.py` | DEAD? | 0 | Conditional backtest — evaluates strategy on historical user trades. |
| `polymarket_exec/backtest/harness.py` | DEAD? | 0 | Full-market backtest harness. |
| `polymarket_exec/backtest/metrics.py` | WIRED | 1 | Backtest metrics, friction models, and reporting data classes. |
| `polymarket_exec/connectors/__init__.py` | pkg | 2 | Exchange and data connectors. |
| `polymarket_exec/connectors/base.py` | DEAD? | 0 | Re-export abstract base classes and exceptions for connector authors. |
| `polymarket_exec/connectors/binance.py` | DEAD? | 0 | Binance connector — BTC spot price and recent close history. |
| `polymarket_exec/connectors/chainlink.py` | DEAD? | 0 | Chainlink Data Streams connector stub. |
| `polymarket_exec/connectors/chainlink_settlement.py` | WIRED | 2 | Settlement-aligned Chainlink BTC/USD feed via Polymarket endpoints (issue #21). |
| `polymarket_exec/connectors/macro_calendar.py` | WIRED | 2 | Pure parsers for US macro release calendars: BLS/BEA ICS, Census and Fed calendars, ForexFactory. |
| `polymarket_exec/connectors/polymarket.py` | DEAD? | 0 | Polymarket connector — discovers the current BTC 5-minute binary market window. |
| `polymarket_exec/connectors/registry.py` | WIRED | 1 | Connector registry — manages the lifecycle and discovery of all connectors. |
| `polymarket_exec/connectors/updown_quote.py` | WIRED | 3 | Live top-of-book quote for the current window of any crypto Up/Down market. |
| `polymarket_exec/connectors/venue_flow.py` | WIRED | 3 | Closed-hour trade-flow bars and venue state snapshots for the Binance and Kraken feeds. |
| `polymarket_exec/connectors/venue_messages.py` | WIRED | 1 | Parsers for venue feed frames: Kraken spot/futures trades, Binance liquidations, perp state. |
| `polymarket_exec/connectors/ws_runner.py` | WIRED | 1 | Reconnecting WebSocket loop shared by the venue trade feeds (Kraken, Binance liquidations). |
| `polymarket_exec/core/__init__.py` | pkg | 0 | Core domain types, interfaces, and exceptions. |
| `polymarket_exec/core/exceptions.py` | WIRED | 4 | Custom exception hierarchy for the BTC 5m Binary Pricing Model trading system. |
| `polymarket_exec/core/interfaces.py` | WIRED | 10 | Abstract base classes for all pluggable system components. |
| `polymarket_exec/core/types.py` | WIRED | 10 | All domain types and enums for the BTC 5m Binary Pricing Model trading system. |
| `polymarket_exec/execution/__init__.py` | pkg | 0 | Paper and live execution managers. |
| `polymarket_exec/execution/gate.py` | WIRED | 5 | Venue-independent pre-trade risk gate (issue #64). |
| `polymarket_exec/execution/live.py` | WIRED | 7 | Live execution on the Polymarket CLOB via py-clob-client. |
| `polymarket_exec/execution/paper.py` | WIRED | 1 | Paper execution manager — explicit order lifecycle with SQLite persistence. |
| `polymarket_exec/execution/risk.py` | WIRED | 1 | Venue-independent risk service — pre-trade and post-trade risk controls. |
| `polymarket_exec/marketdata/__init__.py` | pkg | 3 | Live Polymarket market data over WebSockets: Up/Down order books, trades, reference prices. |
| `polymarket_exec/marketdata/clob_messages.py` | WIRED | 4 | Pure parsers for the Polymarket CLOB market channel: one text frame in, typed events out. |
| `polymarket_exec/marketdata/clob_stream.py` | WIRED | 2 | Reconnecting connection to the Polymarket CLOB market channel (books, trades, lifecycle). |
| `polymarket_exec/marketdata/hub.py` | WIRED | 3 | Live Polymarket books, trades and reference prices for strategies (the module's public API). |
| `polymarket_exec/marketdata/order_book.py` | WIRED | 1 | One token's order book, rebuilt from CLOB snapshots and absolute level changes. |
| `polymarket_exec/marketdata/rtds_stream.py` | WIRED | 2 | Chainlink, Chainlink 60 s TWAP and Binance prices from Polymarket's RTDS WebSocket. |
| `polymarket_exec/marketdata/universe.py` | WIRED | 1 | Which Polymarket Up/Down windows to follow, and their outcome token ids. |
| `polymarket_exec/ops/__init__.py` | pkg | 3 | Operator controls and telemetry. |
| `polymarket_exec/ops/controller.py` | DEAD? | 0 | Unified bot controller — tick loop using execution manager + risk service. |
| `polymarket_exec/ops/dashboard/__init__.py` | pkg | 2 | FastAPI dashboard for BTC 5m Binary Pricing Model trading system. |
| `polymarket_exec/ops/dashboard/app.py` | WIRED | 2 | FastAPI dashboard for the local Polymarket crypto trading lab. |
| `polymarket_exec/ops/dashboard/execution_view.py` | WIRED | 1 | Execution view orchestrator (#37). |
| `polymarket_exec/ops/dashboard/panels/__init__.py` | pkg | 2 | Dashboard panels. |
| `polymarket_exec/ops/dashboard/panels/_data.py` | WIRED | 2 | Read-only SQLite loaders for dashboard panels. |
| `polymarket_exec/ops/dashboard/panels/_shared.py` | DEAD? | 0 | Shared rendering primitives for dashboard panels. |
| `polymarket_exec/ops/dashboard/panels/_wallet.py` | WIRED | 1 | Polymarket wallet size for the EMS ribbon: cash + open positions value. |
| `polymarket_exec/ops/dashboard/panels/blotter.py` | WIRED | 1 | Trade blotter: open positions on top, last 12 closed below, mode chip per row. |
| `polymarket_exec/ops/dashboard/panels/controls.py` | WIRED | 1 | Order-size ticket (#50, #89): the operator's share count, priced live. |
| `polymarket_exec/ops/dashboard/panels/daily_altcoin.py` | WIRED | 1 | Daily altcoin Up/Down scanner panel (issue #185). |
| `polymarket_exec/ops/dashboard/panels/decision_engine.py` | WIRED | 1 | Decision engine panel: inputs → computation → final banner + tail. |
| `polymarket_exec/ops/dashboard/panels/feeds.py` | WIRED | 1 | FEEDS card: one row per live upstream feed — what it feeds, source, delay, status. |
| `polymarket_exec/ops/dashboard/panels/market.py` | WIRED | 1 | Live market panel: probability gauge, UP/DOWN book, basis. |
| `polymarket_exec/ops/dashboard/panels/market_selector.py` | WIRED | 1 | Topbar market selector: asset buttons over timeframe buttons. |
| `polymarket_exec/ops/dashboard/panels/performance.py` | WIRED | 1 | Performance / alpha panel: combined equity curve + LIVE/PAPER mini-cards. |
| `polymarket_exec/ops/dashboard/panels/ribbon.py` | WIRED | 1 | Top status ribbon: wallet, P&L, open-position (live) P&L, loss-halt control. |
| `polymarket_exec/ops/dashboard/panels/settings.py` | WIRED | 1 | Settings panel: every dashboard-editable runtime knob (#206). |
| `polymarket_exec/ops/dashboard/panels/tca.py` | WIRED | 1 | TCA panel: quoted spread, half-spread, edge capture, Brier calibration. |
| `polymarket_exec/ops/dashboard/quote_feed.py` | WIRED | 2 | Background quote poller for the dashboard's order-size ticket. |
| `polymarket_exec/ops/feed_monitor.py` | WIRED | 3 | Always-on feed monitor: keeps the live feeds connected and checks each one. |
| `polymarket_exec/ops/flow_recorder.py` | WIRED | 3 | Always-on recorder for venue trade-flow feeds: Binance spot/perp/liquidations and Kraken. |
| `polymarket_exec/ops/incidents.py` | WIRED | 1 | Incident state machine and operator runbooks for the BTC 5m pricing-model system. |
| `polymarket_exec/ops/macro_recorder.py` | WIRED | 3 | Always-on recorder for macro feeds: each source polled on its own cadence into SQLite. |
| `polymarket_exec/ops/telemetry.py` | WIRED | 1 | Feed health telemetry and latency tracking for the BTC 5m pricing-model system. |
| `polymarket_exec/storage/__init__.py` | pkg | 2 | Persistence layer — database, recording, and replay. |
| `polymarket_exec/storage/macro_store.py` | WIRED | 1 | SQLite read/write for the macro calendar: scheduled events and first-seen consensus values. |
| `polymarket_exec/storage/recorder.py` | WIRED | 2 | Market data recorder — persists raw market snapshots to SQLite for deterministic replay. |
| `polymarket_exec/storage/replay.py` | DEAD? | 0 | Deterministic replay — feed recorded market data through a signal generator. |
| `polymarket_exec/storage/venue_flow_store.py` | WIRED | 1 | SQLite read/write for venue flow hour bars and perp venue snapshots. |
| `polymarket_exec/strategy/__init__.py` | pkg | 0 | Signal generation module — pricing model, sizing, and signal composition. |
| `polymarket_exec/strategy/pricing_model.py` | WIRED | 2 | Pricing-model probability and volatility estimation. |
| `polymarket_exec/strategy/signal.py` | DEAD? | 0 | Signal composition — bridge raw edge into a fully typed :class:`Signal`. |
| `polymarket_exec/strategy/sizing.py` | WIRED | 1 | Position sizing derived from signal confidence and strategy parameters. |
| `tools/backtest_btc_strategy.py` | cli | 0 | Run the BTC strategy backtest and parameter optimizer. |
| `tools/chainlink_lead_lag.py` | cli | 0 | Chainlink-vs-Binance BTC lead-lag analysis (issue #57). |
| `tools/copytrade_dashboard.py` | cli | 0 | Dashboard for the copy-trade shadow ledgers (#182). |
| `tools/copytrade_live.py` | cli | 0 | LIVE copy-trade executor — mirrors a target wallet with real funds (#182). |
| `tools/copytrade_onchain.py` | cli | 0 | Real-time on-chain fill listener for a target wallet (#182). |
| `tools/copytrade_shadow.py` | cli | 0 | Live copy-trade shadow — mirror a target wallet, priced honestly (#182). |
| `tools/demo_snapshot.py` | cli | 0 | Print a BTC paper trading snapshot. |
| `tools/fetch_polymarket_trades.py` | cli | 0 | Pull this account's Polymarket trade history via the CLOB API → CSV. |
| `tools/forecast_journal.py` | cli | 0 | Slow-market forecasting-skill pilot: journal + scoring (issue #162). |
| `tools/gen_docs.py` | cli | 0 | Generate the machine-derived sections of the agent docs. |
| `tools/live_detect_wallet.py` | cli | 0 | Find your MetaMask Polymarket wallet and write it into .env (#34). |
| `tools/live_preflight.py` | cli | 0 | Live-launch preflight: verify the .env wallet config end to end (issue #32). |
| `tools/offline_replay.py` | cli | 0 | Offline replay of the BTC 5-m pricing-model strategy on HF Polymarket data. |
| `tools/pairarb_report.py` | cli | 0 | Report on the two-sided pair shadow ledger (#182). |
| `tools/pairarb_shadow.py` | cli | 0 | Live shadow runner for two-sided maker quoting on 5m Up/Down markets (#182). |
| `tools/race_status.py` | cli | 0 | One-shot fee-true race standings + deploy-bar tracker (issue #150). |
| `tools/reconcile_live_ledger.py` | cli | 0 | Reconcile the live paper-ledger against the REAL Polymarket account (issue #102). |
| `tools/regime_attribution.py` | cli | 0 | Regime-attribution instrument for the shadow forward-tester (issue #120). |
| `tools/replay_race.py` | cli | 0 | Tick-replay backtest for the shadow roster over the FULL quote history (#144). |
| `tools/shadow_performance.py` | cli | 1 | Per-model performance comparison for the shadow forward-tester. |
| `tools/venue_recorder.py` | cli | 1 | Venue recorder — the research program's one blocking build item (C17). |
<!-- END GENERATED:inventory -->

See `docs/FILE_MAP.md` for the full generated index.
