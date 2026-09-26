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
| Execution gates, live CLOB executor, connectors, dashboard, recorder | **`polymarket_exec/`** — `execution/gate.py:RiskGate`, `execution/live.py:LiveExecutor` |
| Config, DB schema, logging (foundation; imported by both, imports neither) | top-level **`config.py` / `db.py` / `logging_setup.py`** |

They are **bidirectionally coupled**: the FastAPI dashboard imports `polymarket_bot.*`; `polymarket_bot` imports back into `polymarket_exec.{execution,connectors}`.

## "I want to change X → edit Y"

| Change | File |
|---|---|
| Plug in a **new strategy** (the loop's entry decision) | `polymarket_bot/paper.py:_build_snapshot` — the `NO_STRATEGY_REASON` block |
| Shared pricing math (fair value, sigma) used by the BTC loop and daily scanner | `polymarket_bot/strategy.py` |
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

- `RiskGate` (`polymarket_exec/execution/gate.py`, LIVE) vs `RiskService` (`polymarket_exec/execution/risk.py`, DEAD on the live path).

## WIRED in the table ≠ live on the trading path

The generated inventory below counts **direct non-test importers**. A non-zero count means "something imports this," not "this runs when the bot trades." Several modules import-resolve but are dead on the live path. Edit them only if you mean to touch tests — never expecting a runtime trading effect:

| Module | Table says | Reality |
|---|---|---|
| `dashboard.py` | WIRED (imported by `main.py`) | Never-taken Gradio fallback. The live UI is the FastAPI app. |
| `polymarket_exec/connectors/registry.py` | WIRED | Built, but the live loop **bypasses** the registry/ABC architecture; the live feed is `connectors/chainlink_settlement.py` only. Its importer is the dead controller. |
| `polymarket_exec/connectors/{base,binance,chainlink,polymarket}.py` | DEAD? | The ABC connectors behind the unused registry. Not on the live path. |
| `polymarket_exec/execution/risk.py` (`RiskService`) | WIRED | Only importer is the **dead** `polymarket_exec/ops/controller.py`. The LIVE risk check is `execution/gate.py:RiskGate`. |
| `polymarket_exec/execution/paper.py` (`PaperExecutionManager`) | WIRED | Live paper fills are journaled **inline in `polymarket_bot/paper.py`**, not via this class. |
| `polymarket_exec/ops/controller.py` (`BotController`) | DEAD? | The live controller is `polymarket_bot/controller.py`. |

Genuine **DEAD?** (no importers at all): `connectors/{base,binance,chainlink,polymarket}.py`, `ops/controller.py`, `ops/dashboard/panels/_shared.py`, `strategy/signal.py`, `polymarket_bot/chronos_signal.py`.

## Live trading is built and gated

Live exists (`execution/live.py:LiveExecutor`). It activates ONLY when the operator clicks LIVE in the dashboard (the consent — no env phrase) AND a private key AND a coherent wallet AND a clean config parse. **Agents never flip the gate. The operator launches.**

Env knobs: `BTC_TRADE_*` are canonical; `BTC_LIVE_*` are deprecated read-aliases.

## Module status (generated)

> The inventory `Status`/`Importers` columns are **mechanical** (direct non-test importer
> count) — read the "WIRED ≠ live" callouts above for semantic truth. `pkg` (package marker)
> and `cli` (entrypoint script) statuses with zero importers are normal, not dead.

<!-- BEGIN GENERATED:summary -->
- **Layout:** one package, `ems/`: `fade_1h_momentum_15m/` = the one strategy (paper only), `marketdata/` = the WebSocket market-data hub it reads, `connectors/updown_quote.py` = live top-of-book for the hub, `dashboard/` = the FastAPI operator UI, `strategies.py` / `inventory.py` / `runtime_knobs.py` / `strategy_docs.py` = switches, inventory, knobs and docs, `config.py` / `db.py` / `logging_setup.py` = foundation.
- **Entry:** `python main.py` → FastAPI `ems/dashboard/app.py`; its lifespan starts the hub, then the strategy.
- **Tests:** 957.
- **Built-but-dead (do not edit expecting runtime effect):** none.
<!-- END GENERATED:summary -->

<!-- BEGIN GENERATED:inventory -->
| Module | Status | Importers | Role |
|---|---|---|---|
| `ems/__init__.py` | pkg | 11 | Polymarket crypto EMS: one paper strategy, the market-data hub and the operator dashboard. |
| `ems/config.py` | WIRED | 8 | Configuration for the local Polymarket crypto trading lab. |
| `ems/connectors/__init__.py` | pkg | 0 | Data connectors: ``updown_quote`` (live top-of-book of a crypto Up/Down window), used by the market-data hub. |
| `ems/connectors/updown_quote.py` | WIRED | 1 | Live top-of-book quote for the current window of any crypto Up/Down market. |
| `ems/dashboard/__init__.py` | pkg | 1 | FastAPI dashboard for BTC 5m Binary Pricing Model trading system. |
| `ems/dashboard/app.py` | WIRED | 1 | FastAPI dashboard for the local Polymarket crypto trading lab. |
| `ems/dashboard/docs_view.py` | WIRED | 2 | Strategy docs in the dashboard: ``/strategy-docs`` and ``/strategy-docs/<key>``. |
| `ems/dashboard/execution_view.py` | WIRED | 1 | Execution view orchestrator (#37). |
| `ems/dashboard/panels/__init__.py` | pkg | 2 | Dashboard panels. |
| `ems/dashboard/panels/_shared.py` | WIRED | 1 | Shared rendering primitives for dashboard panels. |
| `ems/dashboard/panels/fade_1h.py` | WIRED | 1 | FADE 1H MOMENTUM ON 15M card: what the strategy has made, then what it is doing now. |
| `ems/dashboard/panels/feeds.py` | WIRED | 1 | FEEDS card: every live upstream feed — how it connects, what it is for, who uses it, health. |
| `ems/dashboard/panels/settings.py` | WIRED | 1 | Settings panel: every dashboard-editable runtime knob (#206). |
| `ems/dashboard/panels/strategies.py` | WIRED | 1 | MY STRATEGIES card: every strategy family in the repo, hiding none of them. |
| `ems/dashboard/panels/strategy_card.py` | WIRED | 1 | STRATEGY card, under ORDER SIZE: pick a strategy, read how it works. |
| `ems/db.py` | WIRED | 8 | SQLite storage for the local Polymarket crypto trading lab. |
| `ems/fade_1h_momentum_15m/__init__.py` | pkg | 6 | Fade 1h Momentum on 15m: a paper-only strategy on the 15-minute crypto Up/Down markets. |
| `ems/fade_1h_momentum_15m/decide.py` | WIRED | 2 | The model hook for Fade 1h Momentum on 15m: one coin's inputs in, a :class:`Decision` out. |
| `ems/fade_1h_momentum_15m/executor.py` | WIRED | 2 | Order execution for Fade 1h Momentum on 15m: paper child orders, their fills, settlement. |
| `ems/fade_1h_momentum_15m/inputs.py` | WIRED | 2 | Live inputs for Fade 1h Momentum on 15m: one read of everything the maths needs, per coin. |
| `ems/fade_1h_momentum_15m/learner.py` | WIRED | 1 | Live learning for Fade 1h Momentum on 15m: the dials move with every settled window. |
| `ems/fade_1h_momentum_15m/ledger.py` | WIRED | 5 | Paper ledger for Fade 1h Momentum on 15m. |
| `ems/fade_1h_momentum_15m/model.py` | WIRED | 2 | The Fade 1h Momentum on 15m model, standard library only: the chance a 15m window settles Up. |
| `ems/fade_1h_momentum_15m/runner.py` | WIRED | 2 | The Fade 1h Momentum on 15m loop: bookkeeping, inputs, the model hook, sizing and orders. |
| `ems/fade_1h_momentum_15m/sizing.py` | WIRED | 3 | Sizing maths for Fade 1h Momentum on 15m: market anchor, Kelly stakes, scaled passive limit |
| `ems/fees.py` | WIRED | 1 | Canonical Polymarket taker-fee math. |
| `ems/inventory.py` | WIRED | 5 | Every strategy family in this repo. |
| `ems/logging_setup.py` | WIRED | 12 | Structured JSON logging with structlog. Module + trade_id context. |
| `ems/marketdata/__init__.py` | pkg | 4 | Live Polymarket market data over WebSockets: Up/Down order books, trades, reference prices. |
| `ems/marketdata/clob_messages.py` | WIRED | 5 | Pure parsers for the Polymarket CLOB market channel: one text frame in, typed events out. |
| `ems/marketdata/clob_shard.py` | WIRED | 1 | One asset x timeframe's market-channel connections: N redundant sockets, the freshest served. |
| `ems/marketdata/clob_stream.py` | WIRED | 4 | Reconnecting connection to the Polymarket CLOB market channel (books, trades, lifecycle). |
| `ems/marketdata/hub.py` | WIRED | 4 | Live Polymarket books, trades and reference prices for strategies (the module's public API). |
| `ems/marketdata/order_book.py` | WIRED | 3 | One token's order book, rebuilt from CLOB snapshots and absolute level changes. |
| `ems/marketdata/rest_poll.py` | WIRED | 1 | A light REST /book poll running alongside the sockets on the markets in use. |
| `ems/marketdata/rtds_stream.py` | WIRED | 2 | Chainlink, Chainlink 60 s TWAP and Binance prices from Polymarket's RTDS WebSocket. |
| `ems/marketdata/universe.py` | WIRED | 1 | Which Polymarket Up/Down windows to follow, and their outcome token ids. |
| `ems/runtime_knobs.py` | WIRED | 4 | Operator runtime knobs: single dashboard-editable source of truth (#206). |
| `ems/strategies.py` | WIRED | 5 | Operator strategy switches: which strategies may open new positions. |
| `ems/strategy_docs.py` | WIRED | 4 | One document per strategy family, kept in step with the code it describes. |
| `main.py` | cli | 0 | Entrypoint: boots the FastAPI operator dashboard (uvicorn). |
| `tools/fade_1h_momentum_15m/data.py` | cli | 1 | Data layer for the Fade 1h Momentum on 15m historical test. |
| `tools/fade_1h_momentum_15m/examples.py` | cli | 0 | Fade 1h Momentum on 15m: real worked examples from the step-2 tape, explained factor by factor. |
| `tools/fade_1h_momentum_15m/explain.py` | cli | 0 | Fade 1h Momentum on 15m: one decision explained factor by factor, in a trader's words. |
| `tools/fade_1h_momentum_15m/manual_trades_flip.py` | cli | 0 | What a wallet's recent manual trades made, against taking the other side of each. |
| `tools/fade_1h_momentum_15m/model.py` | cli | 1 | Fade 1h Momentum on 15m: the model of tasks/2026-09-21-fade-1h-momentum-on-15m.md, sections 1-6 and 1b. |
| `tools/fade_1h_momentum_15m/step0_reversal_auc.py` | cli | 0 | Step 0 of the pre-registered test: reproduce the published 15-minute reversal on our Binance data. |
| `tools/fade_1h_momentum_15m/step1_walkforward.py` | cli | 0 | Step 1 of the pre-registered test: walk-forward maximum likelihood on Binance spot only. |
| `tools/fade_1h_momentum_15m/step2_polymarket.py` | cli | 0 | Step 2 of the pre-registered test: the frozen model on the real Polymarket 15m tape. |
| `tools/fade_1h_momentum_15m/threshold_scan.py` | cli | 0 | Does buying (or fading) the 1h-momentum side of a 15m market pay, by entry price? |
| `tools/fade_1h_momentum_15m/twap_proxy_agreement.py` | cli | 0 | Which Binance proxy of the 15m settlement agrees with the real resolutions? (section 1b) |
| `tools/fade_1h_momentum_15m/validate_math.py` | cli | 0 | Monte Carlo check of every closed form in tasks/2026-09-21-fade-1h-momentum-on-15m.md. |
| `tools/gen_docs.py` | cli | 0 | Generate the machine-derived sections of the agent docs. |
| `tools/strategy_docs.py` | cli | 0 | Keep each strategy's doc in step with its code. |
<!-- END GENERATED:inventory -->

See `docs/FILE_MAP.md` for the full generated index.
