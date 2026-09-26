# Polymarket Crypto — a small EMS for paper-trading ideas

A local execution management system for trying trading ideas on Polymarket's
crypto Up/Down markets: live market data over WebSockets, paper orders that fill
only against the real trade tape, a ledger in SQLite and a dashboard that shows
every strategy, its numbers and its reasoning.

One strategy runs today: **Fade 1h Momentum on 15m**. Every minute it prices the
15-minute BTC, ETH, SOL and XRP windows with its own model, sizes with Kelly and
rests scaled passive limit orders on paper. Its doc, with the maths and worked
examples, is [docs/strategies/fade_1h_momentum_15m.md](docs/strategies/fade_1h_momentum_15m.md).

Paper only. There is no live order path; `BOT_MODE=live` makes the strategy
place nothing and say so on its card. Agent rules and scope are in
**[AGENTS.md](AGENTS.md)**; "where do I change what" is
**[docs/CODE_MAP.md](docs/CODE_MAP.md)**.

## Running it

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[test]"
cp .env.example .env
./.venv/bin/python main.py          # dashboard at http://127.0.0.1:7860
```

Tests, lint and the docs-drift check:

```bash
DB_PATH=/tmp/t.db ./.venv/bin/python -m pytest tests/ -q
./.venv/bin/ruff check ems/ tests/ tools/ main.py
./.venv/bin/python tools/gen_docs.py --check
```

The strategy starts with the process. Its switch on the MY STRATEGIES card
starts and stops new entries; `touch data/KILL` stops new orders and cancels
resting paper orders until the file is deleted. See
**[docs/OPERATIONS_RUNBOOK.md](docs/OPERATIONS_RUNBOOK.md)**.

## The dashboard

Every card collapses on a click of its header and remembers its state.

- **FEEDS** — the market-data hub's connections: Polymarket books and trades,
  Chainlink, Chainlink TWAP and Binance prices.
- **MY STRATEGIES** — every strategy family, its switch and what it has settled.
- **STRATEGY** — the picked strategy's concept, assumption, maths and derivation,
  from its doc.
- **FADE 1H MOMENTUM ON 15M** — net P&L, drawdown, open exposure, then per coin:
  the inputs, the model's chance against the market, the order it chose and why.
- **SETTINGS** — the runtime knobs (bankroll, Kelly multiplier, price levels,
  which coins), applied on the next pass without a restart.
- **Activity** — fills, settlements, switch and knob changes.

## Architecture

```
main.py                   # singleton lock, init_db, uvicorn
ems/
├── config.py  db.py  logging_setup.py   # env, SQLite schema + migrations, structlog
├── strategies.py  inventory.py  runtime_knobs.py  strategy_docs.py
├── fade_1h_momentum_15m/  # inputs → model → sizing → decide → executor → ledger; runner loop
├── marketdata/            # WebSocket hub: CLOB books/trades, RTDS prices, REST poll, universe
├── connectors/            # updown_quote: top-of-book REST read used by the hub
└── dashboard/             # FastAPI app, execution view, panels/, static/, templates/
tools/                     # gen_docs, strategy_docs, fade_1h_momentum_15m research scripts
tests/                     # network-free, DB-isolated
```

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the patterns behind it.

## Adding a strategy

1. A package under `ems/` with pure maths and a runner that reads the hub.
2. A `Strategy` in `ems/strategies.py` (the switch) and a `Family` in
   `ems/inventory.py` (what MY STRATEGIES says).
3. `python tools/strategy_docs.py new <key>` and fill the doc.
4. A card under `ems/dashboard/panels/`, wired in `execution_view.py`, and a
   start in the lifespan of `ems/dashboard/app.py`.

## Reading the record

- **`CHANGELOG.md`** — what changed and when.
- **`docs/archive/`** — the earlier research lines (BTC 5-minute, copy-trade,
  maker) and their findings. History, not current status.
- **`tasks/`** — design notes and the working log.

## License

MIT — see `LICENSE`.
