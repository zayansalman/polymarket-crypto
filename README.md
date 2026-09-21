# Polymarket Crypto — a binary-markets trading lab

A local research and execution stack for Polymarket crypto binary markets: a pricing
model and a paper/live execution stack. Every strategy it runs is visible in the
dashboard. Originally built around BTC 5-minute Up/Down
markets; that line closed 2026-08-29 (#182) after a shadow copy-trade/maker-quoting
research phase came back net negative. Next chapter (daily/hourly/longer-horizon
markets) is not yet chosen or built — see Status below.

Agent instructions and scope live in **[AGENTS.md](AGENTS.md)**; the routing map is
**[docs/CODE_MAP.md](docs/CODE_MAP.md)**. Read those first for "where do I make what
change."

## Status (current)

5-minute-market work is closed (2026-08-29, #182 branch close-out). Two research
phases ran on it:

- A 30-day BTC-only signal-race (June–July 2026) found no exploitable directional edge
  at retail latency net of the venue's taker fee — archived 2026-07-10, see
  **[docs/archive/](docs/archive/)** for the full record (findings, timeline, pivot
  memo, postmortem).
- A shadow-only copy-trade / maker-quoting line (#182, reopened 2026-08-04) tested
  whether a top Polymarket account's two-sided quoting strategy was reproducible
  natively or copy-tradeable. A second independent read confirmed net-negative results
  (doge -$307.68/n=1980, min -$174.47/n=1756) and zero accumulated fills on the pairarb
  side — closed 2026-08-29, never armed for live. That code has since been deleted.

Copy-trade was removed from the app on 2026-09-21: no wallets are followed.

Next chapter: daily/hourly/longer-horizon crypto markets. Category not yet chosen,
nothing built for it yet — see `tasks/todo.md` for current status and open items.

## Architecture

Two coupled trees plus a small shared foundation:

```
polymarket_bot/           # the live loop + signal math
├── paper.py              #   tick loop, snapshots, settle-style position lifecycle
├── controller.py         #   start/stop, watchdog, silent-stop detector
├── strategy.py           #   pricing-model math + executable-edge signal (pure)
├── params.py             #   operator-tunable runtime params
├── fees.py               #   canonical Polymarket taker-fee math (live + paper)
├── daily/                #   daily altcoin Up/Down scanner (paper only)
└── pairarb/              #   market_index.py only (used by daily/); the rest of #182 is deleted

polymarket_exec/          # execution / connectors / ops
├── core/                 #   domain types, interfaces, exceptions
├── strategy/  connectors/  storage/
├── execution/            #   paper lifecycle + LIVE executor (multi-gated) + RiskGate
└── ops/dashboard/         #   FastAPI operator dashboard (SSE), panels, runtime controls

config.py  db.py  logging_setup.py   # foundation: env parsing, SQLite + migrations, structlog
tools/                    # research instruments and CLI runners
tests/                    # DB-isolated, network-free (see docs/FILE_MAP.md for current count)
```

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the full engineering tour
(patterns, ops defense-in-depth, data layer, testing/CI).

## Running it

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[test]"
cp .env.example .env
./.venv/bin/python main.py          # dashboard at http://127.0.0.1:7860
DB_PATH=/tmp/t.db ./.venv/bin/python -m pytest tests/ -q   # DB-isolated
```

Dashboard defaults to paper trading. Operator presses ▶ Start / Stop. Live trading is
built, multi-gated (operator clicks LIVE in the dashboard + key + coherent wallet), and off by
default — the operator, never an agent, arms and launches it. See
**[docs/OPERATIONS_RUNBOOK.md](docs/OPERATIONS_RUNBOOK.md)**.

## Reading the record

- **`AGENTS.md`** — agent rules and scope fence (source of truth).
- **`docs/CODE_MAP.md`** / **`docs/FILE_MAP.md`** — generated routing map and module status.
- **`docs/ARCHITECTURE.md`** — the engineering tour.
- **`docs/archive/`** — the June–July 2026 BTC-only research phase: findings, timeline,
  pivot memo, postmortem. Historical, not current status.
- **`tasks/todo.md`** — current status and open items.
- **`tasks/lessons.md`** — accumulated reasoning-error lessons, kept live.
- **`tasks/archive/todo_history.md`** — closed historical build log (issues #20–#144).
- **`CHANGELOG.md`** — release history.

## License

MIT — see `LICENSE`.
