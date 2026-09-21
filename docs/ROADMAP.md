# Engineering Roadmap

This roadmap keeps the project useful as a local Polymarket crypto paper-trading
tool while moving it toward the engineering shape expected of serious trading
systems.

## Current Strengths

- Narrow market scope (inherited): BTC 5-minute Up/Down was the original build;
  that line closed 2026-08-29 (#182) and no replacement category is chosen yet.
- Explicit operator controls: Start, Stop, Refresh, activity feed.
- Local paper ledger: ticks, simulated positions, exits, config state, and
  notifications are persisted in SQLite.
- Basic risk rules: bounded $1-$5 paper sizing, one open position, late-window
  skip, target/stop/time exits.
- Failure visibility: loop/feed/market errors are surfaced in logs and
  dashboard state.

> Several earlier roadmap items have since shipped and were removed from this
> list: the market-data recorder (`polymarket_exec/storage/recorder.py`),
> feed/latency telemetry
> (`polymarket_exec/ops/telemetry.py`), incident states (`polymarket_exec/ops/incidents.py`
> + `docs/OPERATIONS_RUNBOOK.md`), the dedicated-wallet live executor
> (`polymarket_exec/execution/live.py`), and CI with deterministic fixtures
> (`.github/workflows/ci.yml`). What remains below is genuine future work.

## Priority Buildout

1. **Order Lifecycle Simulator**

   Model paper orders as separate acknowledgement, fill, partial-fill, cancel,
   exit, and reconciliation events. This keeps the paper system structurally
   close to the live executor without adding live risk.

2. **Risk And PnL Console**

   Add realized/unrealized PnL, exposure, inventory, drawdown, win/loss by
   market window, and stop-reason attribution. Keep risk metrics visible in
   both dashboard and CLI snapshots.

3. **Research-To-Production Boundary**

   Separate signal research from execution state. A new signal should be
   testable in replay before it is allowed in the live paper loop. The
   human-gated params propose/apply flow was a first step (archived with the v0
   strategy on 2026-09-13, `docs/archive/v0-strategy.md`); a new strategy needs
   its own replay-before-loop path.

## Later, Explicitly Reviewed

- CLOB quote integration with freshness checks.
- Chainlink Data Streams as primary reference input.
- Position and balance reconciliation against venue state.
- Remote monitoring only after private-key handling is isolated.
