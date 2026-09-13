# Architecture & Code Quality

A tour of the engineering underneath the research. The system ran unattended for weeks,
survived three loss-halts, two feed regimes, a wedged-loop incident, and an operator
mid-experiment — and every number it produced was reproducible afterward. That property
was designed, not lucky. (Module-level routing lives in [CODE_MAP.md](CODE_MAP.md);
generated status in [FILE_MAP.md](FILE_MAP.md).)

## 1. Shape of the system

```
                        ┌────────────────────────────────────────────┐
                        │ FastAPI dashboard (SSE)  ops/dashboard/    │
                        │  panels · runtime controls · activity feed │
                        └───────────────▲────────────────────────────┘
                                        │ reads SQLite / POST controls
┌──────────────┐   ticks   ┌────────────┴───────────┐   would-be trades
│ Feeds        │──────────▶│ polymarket_bot/paper.py       │──────────────────┐
│  Chainlink WS│           │  the ONE loop          │                  ▼
│  + REST poll │           │  snapshot → signal →   │        polymarket_bot/shadow/
│  (settlement-│           │  gate → paper/live     │         runner+ledger
│   aligned)   │           │  execution → settle    │        (5-model race,
│  Binance vol │           └────────┬───────────────┘         idempotent)
└──────────────┘                    │ real orders (multi-gated)
                                    ▼
                          polymarket_exec/execution/live.py
                          CLOB executor · RiskGate · kill switch
```

- **`polymarket_bot/`** — the live tick loop, pricing-model math, and the shadow race.
- **`polymarket_exec/`** — layered core/strategy/connectors/storage/execution/ops.
- **Foundation** — `config.py` (env parsing that *refuses* live boot on parse errors),
  `db.py` (SQLite + additive migrations + backfills), `logging_setup.py` (structlog).
- **`tools/`** — research instruments; read-only against the ledger by construction.

## 2. The load-bearing patterns

### Pure strategy functions
Every candidate model is `fn(SnapshotView, params) → ShadowSignal | None` over **frozen
dataclasses**. No I/O, no clock, no DB. Consequence: the exact same function is
(a) unit-tested with hand-built fixtures, (b) raced live by the shadow runner,
(c) replayed over months of tick history by the backtester. When the replay reproduced
the recorded shadow ledger **100% on side and entry price**, that wasn't a happy accident
— purity made behavioral drift impossible.

### One fee model, one place
`polymarket_bot/shadow/fees.py` (`0.07·p·(1−p)` per share, charged at entry) settles the shadow
ledger, the live book, the replayer, and the breakeven arithmetic. The project's June bug —
fee-blind live books overstating PnL by 2× — is structurally unrepresentable now: there is
no second implementation to disagree.

### Idempotent journaling
Shadow trades are `INSERT OR IGNORE` against a unique `(window_slug, model_id)` index.
Crash replays, duplicate ticks, and watchdog respawns cannot double-count. Settlement is a
single UPDATE keyed on the open state.

### One risk path for paper and live
The `RiskGate` (per-trade cap, daily *trailing* loss halt, optional bankroll cap) evaluates
identically in both modes, with persisted daily counters. Paper is therefore a faithful
preview of live — and the halts fired correctly in production three times, once ending a
real-money session at −$8.10 exactly as specified.

### Live is opt-in four times over
Real orders require: the operator clicking **LIVE** in the dashboard (the consent — no
env phrase) **and** a private key **and** a coherent wallet
**and** a clean config parse (`CONFIG_PARSE_ERRORS` non-empty refuses live boot — a typo in
a risk limit must never silently loosen it). A kill-switch file cancels and flattens.
Missing gates refuse loudly; live never falls back to paper silently, and paper never
escalates.

## 3. Ops defense in depth

Failures observed in production each got a *layered* answer, and each layer is tested:

| Failure class | Answer |
|---|---|
| Two processes sharing one ledger | Advisory `flock` singleton (`data/bot.lock`) — second instance fails fast and loud |
| Loop wedged on an unbounded await (process alive, zero ticks, 14h) | In-process heartbeat watchdog: stamps every iteration; stalls > 180s → abandon + respawn **paper**; **live** only notifies — a watchdog must never auto-restart a real-money path (#147) |
| Loop/process dies with state still reading "running" | Silent-stop detector in `get_status()`: one alert per death with last-heartbeat time, poll-spam-guarded, re-arms on recovery (#138) |
| Feed flapping throttles journaling while the heartbeat stays fresh | Tick-cadence signal: a running loop below ~30 ticks/10min is flagged as a JOURNALING STALL even while "accruing: YES" (#157) |
| Settlement feed goes stale | Entries blocked (never trade a stale reference); exits still run; per-component feed provenance journaled on every tick |
| Operator display lies | State derives from the actual runner thread, not the stored row; the feed label renders real per-component sources with a settlement-alignment qualifier (#151) |

## 4. Data layer

- **Additive migrations with backfills.** Schema evolves via `ALTER TABLE` column maps;
  historical rows are backfilled where the data already existed — e.g. maker/taker
  attribution was recovered retroactively by `json_extract`-ing journaled CLOB placement
  responses (#137). Read-only tools tolerate pre-migration snapshots (`NULL AS col`).
- **Everything journaled.** Every tick (with both books, provenance, σ), every would-be
  trade, every order attempt (verbatim venue response), every notification. The final
  ledger — 2,924 shadow rows, ~100k ticks — replays the entire project.
- **Secrets redacted at the sink.** All strings persisted to the ledger/notification feed
  pass through redaction; the key never logs, even inside stringified exceptions.
- **Research reads are isolation-safe:** snapshot-copy + `immutable=1` opens; analysis can
  never lock or mutate the live ledger.

## 5. Testing & CI

- **828 tests**, network-free, deterministic, and **DB-isolated** (`DB_PATH=<tmp>`; the
  suite never touches the live ledger).
- Layer coverage: pure math truth tables (fees, Brier, Wilson/binomial, bands), signal
  gate behavior (fires/blocks at boundaries), ledger idempotence + fee-true settlement,
  execution wiring (paper never touches the live executor; blocked entries write no
  position), watchdog/silent-stop/cadence state machines, dashboard rendering, tool
  end-to-end runs against seeded temp DBs.
- **Clean-venv gate before main:** fresh `pip install -e .[test]` + full suite (dev venvs
  mask undeclared dependencies).
- **Docs can't rot:** `tools/gen_docs.py` regenerates the module map, import graph status
  (`WIRED`/`DEAD?`), and test counts into `<!-- GENERATED -->` blocks; a CI drift job fails
  when they're stale. [CODE_MAP.md](CODE_MAP.md) marks built-but-unwired modules so nobody
  edits dead code expecting runtime effect.

## 6. Process quality (the part that saved the money)

- **Issue → branch → tests → PR → develop; never main directly.** 165 issues/PRs of
  recorded decisions.
- **Pre-registration:** every gate variant's thresholds were frozen in an issue *before*
  the replay that evaluated it; in-sample slicing was hypothesis generation only.
- **Negative results never merge:** failed spikes are documented in the issue and the
  branch deleted (the finding survives; the code doesn't rot in `develop`).
- **An agent ops-loop** ([../tasks/race_loop.md](../tasks/race_loop.md)) ran
  assess→build→log every 6h under binding guardrails — read-only on the ledger, never
  touching the bot lifecycle or live gate — and left a complete audit trail
  ([../tasks/race_log.md](../tasks/race_log.md)) including its own mistakes (a degenerate
  n=1 CI printing "CLEARS NOW" was caught and hotfixed the same iteration).
- **Kill criteria pre-registered** — deploy bar, kill floors, sunset date — and enforced
  against three successive "winning" models, and finally against the project itself.
