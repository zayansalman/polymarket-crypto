# Architecture

The patterns behind the code. Routing lives in [CODE_MAP.md](CODE_MAP.md); generated
module status in [FILE_MAP.md](FILE_MAP.md).

## 1. Shape of the system

```
                 ┌──────────────────────────────────────────────┐
                 │ FastAPI dashboard (SSE)   ems/dashboard/     │
                 │  cards · switches · knobs · activity feed    │
                 └────────────▲─────────────────────────────────┘
                              │ reads SQLite + hub.snapshot(); POST /api/runtime-config
┌────────────────────┐        │        ┌───────────────────────────────────┐
│ ems/marketdata/    │ books, │        │ ems/fade_1h_momentum_15m/         │
│  CLOB WebSockets   │ trades,├───────▶│  runner: every minute per coin    │
│  RTDS prices       │ prices │        │  inputs → model → sizing → decide │
│  REST /book poll   │        │        │  → paper executor → ledger        │
└────────────────────┘        │        └───────────────┬───────────────────┘
                              │                        │ fade_* tables
                              └────────────────────────▼──────────────────
                                            ems/db.py (SQLite, WAL)
```

- **`ems/marketdata/`** owns every socket. Strategies ask for a market with
  `hub.want(...)` and read `quote`, `book_top` or `listen`; they never open connections.
- **`ems/fade_1h_momentum_15m/`** is one strategy. Its maths (`model.py`, `sizing.py`,
  `decide.py`) is pure; `runner.py` does the I/O; `executor.py` fills paper orders from the
  real trade tape; `ledger.py` is the only writer of the `fade_*` tables.
- **`ems/kelly_horse_race/`** is the other: one randomised passive buy per BTC 15m window.
  Its maths (`maths.py`) is pure; `inputs.py` reads; `runner.py` sends one order to every
  active endpoint through the shared layer; `ledger.py` is the only writer of the
  `kelly_horse_race_*` tables.
- **`ems/execution/`** is the execution layer every strategy shares: resting-order venues
  behind one interface (`resting.py`), the risk gate with one leg per mode (`gate.py`), the
  fill model (`queue.py`), the trade tape and result reads (`tape.py`), and the checks every
  placement makes first: PAPER/LIVE mode, kill switch, never cross the spread (`controls.py`).
- **`ems/dashboard/`** reads the ledger and the hub snapshot and renders cards. Every card
  is a pure `render(...) -> str`; `execution_view.py` loads the data once.
- **Foundation:** `config.py` (env), `db.py` (schema + additive migrations),
  `logging_setup.py` (structlog).

## 2. The load-bearing patterns

### Pure maths, I/O at the edges
The model, the sizing and the decision take plain values and return plain values: no clock,
no DB, no network. The same functions are unit-tested with hand-built inputs, run live by
the runner, and replayed by the research scripts under `tools/fade_1h_momentum_15m/`.

### Paper fills only from the real tape
A paper order rests at a price level and fills only when the CLOB trade stream prints a
trade through it, with the depth ahead of it in the queue accounted for. No fill is invented
from a mid price, so the paper ledger is a lower bound on what a real order would have done.
Every strategy uses the same fill model (`ems/execution/queue.py:allocate_fills`).

### One executor interface
`runner.py` asks an `Executor` to rest, cancel and settle. Paper is the only implementation.
Any other requested mode, or the kill switch file, yields an executor that places nothing
and says why on the card, while the bookkeeper keeps filling and settling what was already
placed.

### Switches and knobs, not restarts
The strategy switch (`ems/strategies.py`) and every runtime knob (`ems/runtime_knobs.py`)
live in the SQLite `config` table, are read at the top of each pass, and are changed from
the dashboard through one endpoint. Every change is journalled to the activity feed.

### The learner moves the dials, never the code
Settled windows update the strategy's dials (`learner.py`); the dials are versioned rows in
`fade_dials`, so any decision can be reproduced from the dials it was made with.

## 3. Failure visibility

| Failure | What shows |
|---|---|
| Two processes on one ledger | `main.py` holds an advisory lock on `data/bot.lock`; the second instance exits loudly |
| A pass raises | The card header says so, the error is listed, the loop keeps its cadence |
| The loop dies | The card's state comes from this process, never from a saved copy of an earlier run |
| A socket drops | The hub reconnects; FEEDS shows each connection's state and age |
| Missing or stale inputs | That coin gets no order this pass and its block on the card says why |

## 4. Data layer

- **Additive migrations.** New columns are added through the migration dicts in
  `init_db`; existing databases keep every old table untouched.
- **Everything journalled.** Every window, decision, order and dial version is a row;
  the activity feed keeps fills, settlements and every operator change.
- **Isolation in tests.** `tests/conftest.py` points the DB at a temp file and stubs the
  hub and the runner, so the suite is network-free and never touches the live ledger.

## 5. Testing, CI and docs

- `pytest tests/` (network-free, DB-isolated), `ruff check`, and
  `tools/gen_docs.py --check` run in CI.
- `tools/gen_docs.py` regenerates the module inventory, import status and test count into
  the `<!-- GENERATED -->` blocks of `AGENTS.md`, `docs/CODE_MAP.md` and `docs/FILE_MAP.md`.
- `tools/strategy_docs.py` fingerprints each strategy's code into its doc;
  `tests/unit/test_strategy_docs.py` fails when the code moved and the doc did not.
