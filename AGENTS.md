# Agent Instructions — Polymarket Crypto

> This file is the agent constitution for **both Codex and Claude Code** (and any
> other coding agent). It is the single source of scope and rules for this repo.

## START HERE

- **Where to make what change:** read **[docs/CODE_MAP.md](docs/CODE_MAP.md)** first.
  It is the routing doc — "I want to change X → edit Y" — and it explains the
  two-tree structure below.
- **Two coupled code trees, both LIVE:**
  - **`polymarket_bot/`** — the live trading loop + signal math (`paper.py:run_paper_loop`
    is *the* loop; `strategy.py` is the live signal math).
  - **`polymarket_exec/`** — execution gates, live CLOB executor, connectors, FastAPI
    dashboard, recorder, backtest harness.
  - They are **bidirectionally coupled**: the dashboard imports `polymarket_bot.*`;
    `polymarket_bot` imports back into `polymarket_exec.{execution,connectors}`. Top-level
    `config.py` / `db.py` / `logging_setup.py` are the shared foundation.
- **Machine-generated facts** (module inventory, wired-vs-dead status, test count)
  live in **[docs/FILE_MAP.md](docs/FILE_MAP.md)** and in `<!-- GENERATED -->`
  blocks. They are kept fresh by `tools/gen_docs.py` (CI `docs-drift` job +
  `.claude` hooks) — **never hand-edit them.**

## Active Scope

This repository is a local Polymarket crypto binary-markets research and
paper-trading lab. Two strategies are wired and run simultaneously:

1. **BTC 5-minute Up/Down** (`polymarket_bot/paper.py:run_paper_loop`) — the
   original line; its active *development* closed 2026-08-29 (#182), but the
   loop itself is still the default paper-trading path, started/stopped by
   the dashboard's ▶ Start / Stop controls (see below). **No strategy is
   loaded:** the v0 strategy (entry gates, auto-pause, param tuner,
   calibration, model picker) was archived 2026-09-13 —
   [docs/archive/v0-strategy.md](docs/archive/v0-strategy.md). The loop runs
   and journals market data but takes no entries until a new strategy is
   plugged into `paper.py:_build_snapshot`.
2. **Daily altcoin Up/Down scanner** (`polymarket_bot/daily/scanner.py`,
   issue #185) — scans Polymarket's daily (24h-window) Up/Down family across
   a tracked set of thinner altcoin markets (doge/sol/xrp/bnb/eth by
   default, `config.DAILY_ASSETS`) and shadow-trades a flat $10 paper
   position on whichever asset shows the strongest signal. **Paper-only, no
   live gate exists for it at all** — unlike the BTC loop, it has no
   Start/Stop control: it auto-runs as soon as the dashboard process boots
   (`polymarket_exec/ops/dashboard/app.py`'s lifespan) and keeps running
   for the process's lifetime. Its own dashboard panel
   (`panels/daily_altcoin.py`) shows current position(s), settled PnL, and a
   plain-language explanation of the mechanism.
3. **BTC hourly Up/Down strategies** (`polymarket_bot/hourly/engine.py`) — the
   loop runs them when the operator selects **BTC 1h** and presses ▶ Start, in
   the mode selected (paper or live). One decision per hour per strategy, one
   open position per strategy, held to resolution and settled from the Binance
   1h candle. Strategy doc: `docs/strategies/hourly-btc-strategies.md`.

The primary active product behavior for the BTC loop specifically is:

1. Operator opens the local dashboard.
2. Operator presses **▶ Start**.
3. The loop runs on BTC 5-minute Up/Down markets (paper by default) and
   enters only once a strategy is loaded (none is today).
4. Operator presses **Stop** to halt new entries and close open simulated
   positions.

Live trading is also built and multi-gated (see the live rule below); it stays
off unless the operator explicitly arms every gate **and** this file names an
authorized live-trading market (BTC hourly Up/Down — see Absolute
Rules). This includes the daily altcoin scanner: it has no live path built at
all, so there is nothing to arm for it.

## Scope Fence (in scope / out of scope)

In scope:

- Discover current BTC 5-minute Up/Down Polymarket markets.
- **Research/shadow-only exploration is open by default** — any market, any
  timeframe, any venue instrument may be investigated, backtested, or shadow-run
  as long as it places no real orders. No fresh operator carve-out is needed to
  start a new research direction; the market/timeframe restriction below binds
  the **live trading path** only. Example: two-sided maker quoting across the
  venue's 5-minute Up/Down crypto family (btc/eth/sol/xrp/doge/bnb) —
  `polymarket_bot/pairarb/`, shadow only, placed no orders (#182, widened
  2026-08-14, closed 2026-08-29).
- Daily (24h-window) Up/Down markets across doge/sol/xrp/bnb/eth —
  `polymarket_bot/daily/`, shadow only, no live path exists, always-on
  (#185, started 2026-08-30).
- Use a settlement-aligned BTC reference feed for signal and paper fills.
- Show the Chainlink Data Streams reference in the dashboard.
- Compute a fair Up probability and edge versus market price.
- Size trades between $1 and $5 by confidence.
- Persist every tick, position, exit, and dashboard event in SQLite.
- Provide dashboard Start, Stop, Refresh, activity feed, and summary metrics.
- Summarize the optional exported BTC Polymarket history CSV.
- Run a local trade-history conditional backtest and parameter grid optimizer.
- Present a concise systems scorecard covering scope, risk, feed discipline,
  auditability, and failure visibility.
- Maintain a public engineering roadmap focused on market-data recording,
  replay, order lifecycle, risk/PnL, telemetry, and deterministic tests.
- **Live order execution** on the Polymarket CLOB — built, multi-gated, and
  off by default. The operator (never an agent) arms and launches it.

Out of scope:

- Flipping the live gate or placing live orders on behalf of the operator.
- **Any market other than BTC hourly Up/Down, on the live trading path (real
  capital).** BTC hourly is authorized (2026-09-14); everything else stays
  research/shadow-only until this file is explicitly updated naming it.
- Remote deployment / exposing the dashboard beyond localhost by default.

## Absolute Rules

- **Live trading (real capital) is authorized for the BTC hourly Up/Down market**
  (`polymarket_bot/hourly/`, operator decision 2026-09-14). There is no
  paper-only strategy: a strategy runs in whichever mode the operator selects at
  Start. Every other market stays research/shadow-only until this file names it.
- One open position per strategy at a time, per mode (operator decision
  2026-09-14). The legacy 5m loop is one slot; each hourly BTC strategy owns its
  own slot, in paper and in live. Every entry still passes RiskGate (loss halt,
  caps, slippage, kill switch).
- **Live trading is BUILT and multi-gated** (`polymarket_exec/execution/live.py:LiveExecutor`).
  It runs only when the operator **clicks LIVE in the dashboard**
  **AND** a private key **AND** a coherent wallet **AND** a clean config parse — then
  presses Start. There is no env confirm phrase; an env `BOT_MODE=live` default alone
  never trades. **Agents NEVER flip the gate or place live orders; the operator
  launches.** Default is paper.
- Do not read, print, log, commit, echo, or expose private keys.
- The dashboard must stay local by default at `127.0.0.1:7860`.
- Start means trade (paper unless every live gate is armed); Stop means stop.
- No strategy is loaded in the trading loop (v0 archived 2026-09-13). Whatever
  strategy is plugged in next, its entries still pass the live-path safety gates
  (`RiskGate`: loss halt, caps, slippage, kill switch) — those stay hard.
- No silent failures. Feed, market, state, or execution-loop errors must appear
  in structured logs or dashboard state.
- Keep modules small and boundaries clear.
- Keep public docs vendor-neutral and focused on trading-system quality:
  observability, risk control, feed discipline, persistence, and operator
  control.

## Code Conventions

- Python 3.11.
- Async I/O with `httpx` and `aiosqlite`.
- The live dashboard is a **FastAPI (uvicorn) app** at
  `polymarket_exec/ops/dashboard/app.py`. The top-level Gradio `dashboard.py` is a
  **dead, never-taken fallback** (`HAS_NEW_DASHBOARD` is always true) — do not
  treat it as the live UI.
- `structlog` JSON logs.
- SQLite for local paper ledger and dashboard state.
- Prefer explicit, boring safety over cleverness.

## Running Locally

```bash
./.venv/bin/python main.py
```

This boots the **FastAPI dashboard** (uvicorn serving
`polymarket_exec/ops/dashboard/app.py`), not Gradio.

Dashboard:

```text
http://127.0.0.1:7860
```

Optional snapshot:

```bash
./.venv/bin/python tools/demo_snapshot.py
```

## Live module status (generated)

<!-- BEGIN GENERATED:summary -->
- **Trees:** `polymarket_bot/` = live loop + signal math; `polymarket_exec/` = execution/connectors/dashboard/backtest; top-level `config.py`/`db.py`/`logging_setup.py` = foundation. Both ACTIVE, bidirectionally coupled.
- **Entry:** `python main.py` → FastAPI `polymarket_exec/ops/dashboard/app.py`; loop starts on operator ▶ Start → `polymarket_bot/controller.py:request_start`.
- **Tests:** 1120.
- **Built-but-dead (do not edit expecting runtime effect):** `polymarket_bot/chronos_signal.py`, `polymarket_exec/backtest/conditional.py`, `polymarket_exec/backtest/harness.py`, `polymarket_exec/connectors/base.py`, `polymarket_exec/connectors/binance.py`, `polymarket_exec/connectors/chainlink.py`, `polymarket_exec/connectors/polymarket.py`, `polymarket_exec/ops/controller.py`, `polymarket_exec/ops/dashboard/panels/_shared.py`, `polymarket_exec/storage/replay.py`, `polymarket_exec/strategy/signal.py`.
<!-- END GENERATED:summary -->
