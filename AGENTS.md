# Agent Instructions — Polymarket Crypto

> The agent constitution for Codex, Claude Code and any other coding agent. It is
> the single source of scope and rules for this repo.

## START HERE

- **Where to make what change:** read **[docs/CODE_MAP.md](docs/CODE_MAP.md)**
  first. It is the routing doc: "I want to change X → edit Y".
- **One package, `ems/`.** One paper strategy (`ems/fade_1h_momentum_15m/`),
  the execution layer every strategy shares (`ems/execution/`: the fill model,
  the trade tape and result reads, the mode and kill switch checks), the
  WebSocket market-data hub it reads (`ems/marketdata/`), the FastAPI
  operator dashboard (`ems/dashboard/`) and the foundation (`ems/config.py`,
  `ems/db.py`, `ems/logging_setup.py`). `main.py` is the only entry point.
- **Machine-generated facts** (module inventory, wired-vs-dead status, test
  count) live in **[docs/FILE_MAP.md](docs/FILE_MAP.md)** and in
  `<!-- GENERATED -->` blocks, kept fresh by `tools/gen_docs.py` (CI
  `docs-drift` job + `.claude` hooks). **Never hand-edit them.**
- **Strategy docs:** every family in `ems/inventory.py` has
  `docs/strategies/<key>.md`, served in the dashboard at `/strategy-docs`.
  Changing a strategy's code, name, status or switch fails
  `tests/unit/test_strategy_docs.py` until you run
  `python tools/strategy_docs.py stamp <key> "what changed"` and update the
  prose. See [docs/strategies/README.md](docs/strategies/README.md).

## What this is

A small local execution management system (EMS) for trying trading ideas on
Polymarket's crypto Up/Down markets with real market data and paper fills.
One strategy is wired today: **Fade 1h Momentum on 15m**, which prices the
15-minute BTC, ETH, SOL and XRP windows every minute and rests paper orders
that fill only against the real trade tape. Everything else that used to live
here (the BTC 5-minute loop, the maker, the daily altcoin scanner, the live
CLOB executor and its risk gate) was removed on 2026-09-26; `git log` before
`ea93457` has it.

How it runs:

1. `python main.py` boots the dashboard. Its lifespan starts the market-data
   hub, then the strategy loop. There is no Start button.
2. The strategy opens new positions only while its switch on the MY
   STRATEGIES card is on. Off stops new entries; open paper positions still
   settle.
3. The kill switch file (`data/KILL` by default) stops new orders and cancels
   resting paper orders on the next pass. Delete it to re-arm.

## Scope

In scope:

- Paper strategies on Polymarket crypto Up/Down markets, any timeframe, any
  coin, driven by the market-data hub.
- Adding a strategy: a package under `ems/`, a `Strategy` in
  `ems/strategies.py`, a `Family` in `ems/inventory.py`, a doc under
  `docs/strategies/`, a card under `ems/dashboard/panels/`, and a start in the
  dashboard lifespan.
- Dashboard cards, runtime knobs (`ems/runtime_knobs.py`) and research tools
  under `tools/`.

Out of scope:

- **Live trading.** No live order path exists and none is authorised for any
  market. `BOT_MODE=live` makes the strategy place nothing and say so on its
  card. Building a live path is an operator decision recorded in this file
  first, never an agent's.
- Exposing the dashboard beyond localhost.

## Absolute rules

- Agents never place live orders, never build a live path unasked, and never
  flip a mode or switch on the operator's behalf.
- Do not read, print, log, commit, echo or expose private keys.
- The dashboard stays local at `127.0.0.1:7860`.
- No silent failures: hub, strategy and dashboard errors appear in the
  structured logs, the activity feed or the strategy's card.
- Keep modules small and boundaries clear. Panels are pure `render(...) -> str`
  functions; the strategy's maths has no I/O.
- Keep public docs vendor-neutral and focused on trading-system quality.

## Code conventions

- Python 3.11, async I/O with `httpx` and `aiosqlite`, `structlog` JSON logs,
  SQLite for the ledger and dashboard state.
- Absolute imports from `ems` (`from ems.db import ...`). No `sys.path`
  bootstraps.
- Runtime settings the operator may change live are knobs in
  `ems/runtime_knobs.py` (dashboard SETTINGS card, applied on the next pass);
  process settings are env vars in `ems/config.py`, documented in
  `.env.example`.
- SQLite config keys keep their historical `polymarket_bot.*` and `runtime.*`
  names so existing databases still read.
- Tests are network-free and DB-isolated (`tests/conftest.py`). Before pushing:
  `python -m pytest tests/ -q`, `ruff check ems/ tests/ tools/ main.py`,
  `python tools/gen_docs.py --check`.

## Running locally

```bash
./.venv/bin/python main.py
```

Dashboard:

```text
http://127.0.0.1:7860
```

## Live module status (generated)

<!-- BEGIN GENERATED:summary -->
- **Layout:** one package, `ems/`: `fade_1h_momentum_15m/` = the one strategy (paper only), `execution/` = the execution layer every strategy shares (fill model, tape and result reads, mode, kill switch), `marketdata/` = the WebSocket market-data hub it reads, `connectors/updown_quote.py` = live top-of-book for the hub, `dashboard/` = the FastAPI operator UI, `strategies.py` / `inventory.py` / `runtime_knobs.py` / `strategy_docs.py` = switches, inventory, knobs and docs, `config.py` / `db.py` / `logging_setup.py` = foundation.
- **Entry:** `python main.py` → FastAPI `ems/dashboard/app.py`; its lifespan starts the hub, then the strategy.
- **Tests:** 1000.
- **Built-but-dead (do not edit expecting runtime effect):** `ems/execution/gate.py`, `ems/execution/resting.py`.
<!-- END GENERATED:summary -->
