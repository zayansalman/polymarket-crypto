# Local strategy rebuild + full rebrand — self-paced /loop, started 2026-08-30

Running scratchpad for a `/loop 10m` build effort. Each tick reads this file,
adds a dated entry, and either continues the build or concludes. Follows this
project's own AGENTS.md convention: an agent never arms a live gate or places
a real order — this loop builds paper-trading infrastructure only.

## Decisions locked this session (do not re-litigate; if evidence contradicts
one, stop and flag it to the operator rather than silently overriding)

1. **No cloud hosting.** Local-only build, runs on the operator's machine.
2. **Stop all BTC work, stop all 5-minute-market work.** Both were already
   concluded dead in `tasks/2026-08-17-strategy-discussion.md` (tick 2) and
   `docs/archive/PIVOT_2026-07.md` — fees exceed the real edge in a
   latency-dominated venue, for both direction-prediction and copytrading
   mechanisms, on BTC specifically.
3. **New asset focus: non-BTC altcoins with less efficient/thinner markets**
   — DOGE is the operator's explicit flagship example ("the other markets
   like doge which are not efficient"). Thesis: retail-driven, thin-book
   altcoin markets are more likely to carry real, slower-moving mispricing
   than BTC's heavily-arbitraged book. This is a **hypothesis to shadow-test,
   not yet a finding** — same discipline as every prior chapter in this repo
   (shadow before capital, pre-registered bar, fee-true accounting).
4. **New timeframe: daily (24h window), not 5-minute.** Operator confirmed
   "Daily [asset] up/down (24h window)" as the mechanic before narrowing the
   asset away from BTC — so the up/down-on-a-clock-window mechanic carries
   forward, just on a 24h clock instead of 5m, and on alts instead of BTC.
   Removes the latency race entirely (24h is not a speed game); fee drag as
   % of a slower-moving edge should be far smaller than it was at 5m.
5. **Paper trade only, $10 positions.** No live gate touched. This is a
   shadow/paper build, same as every strategy in this repo's history before
   any live-arming discussion.
6. **Must be visible in the dashboard UI** — a new panel showing the
   strategy's positions, PnL, and a plain-language explanation of the
   mechanism (mirrors the existing panel architecture per `tasks/lessons.md`
   "Respect the existing UI architecture for dashboard work").
7. **Full rebrand, in-repo only** (not the GitHub repo itself — operator did
   not ask for that and it wasn't offered). New project name:
   **`polymarket-crypto`** (operator's explicit choice, after rejecting a
   generic-lab-name suggestion and a thesis-named suggestion). Package rename
   map decided this tick (no operator input on internal names, so this is my
   call, consistent with the chosen project name):
   - `btc_bot/` → `bot/`
   - `btc_5m_exec/` → `exec_engine/`
   - `pyproject.toml` `name = "btc-5m-exec"` → `"polymarket-crypto"`
   - `BTC_*`-prefixed env vars / config constants → same name minus the
     `BTC_` prefix (e.g. `BTC_BOT_MODE` → `BOT_MODE`,
     `BTC_LIVE_MAX_TRADE_USD` → `LIVE_MAX_TRADE_USD`). Safe to do cleanly:
     confirmed no `.env` file exists in this repo, so there is no existing
     operator config to break.
   - Deleted `btc_5m_fv/` and `btc_5m_exec.egg-info/` — confirmed both 100%
     untracked build/cache leftovers (zero `.py` source in `btc_5m_fv`,
     zero tracked files in either) predating the #169 rename, not real
     content.
   - Playbook follows the #169 precedent (`981c46a`,
     "refactor(#169): remove 'fair value' branding — btc_5m_fv →
     btc_5m_exec"): `git mv` directories, sweep text refs, regenerate
     `docs/FILE_MAP.md`/`docs/CODE_MAP.md` via `tools/gen_docs.py` (never
     hand-edit), run full test suite before committing.

## Known pre-existing gap (not part of this build, flagging so a future tick
doesn't rediscover it from scratch)

Read-only connector/API health check this session (before the rebrand)
found: Polymarket/CLOB/Gamma APIs and the Chainlink BTC feed all reachable;
`live_preflight.py` refuses cleanly with no `.env`; 177/177 targeted
connector/live-executor tests pass. One real gap — **not urgent, no wallet
configured yet**: `tools/live_detect_wallet.py`'s `polymarket` SDK import
(`polymarket.environments`, `polymarket._internal.wallet`) is not installed
in this environment (it's an optional extra never pulled into the base
install). Only matters once the operator actually tries to connect a real
MetaMask wallet — flag it then, don't fix speculatively now.

## Market research (this session, before build)

Polymarket Gamma API confirmed **non-5m altcoin markets exist and are
liquid**: e.g. `will-dogecoin-reach-0pt2-in-august-2026` — a monthly DOGE
price-threshold market, $105.8k volume, $37.2k liquidity, Binance
DOGE/USDT-sourced resolution. This is a **threshold-by-date** market shape
(will DOGE cross $X before date Y), not a repeating 24h up/down clock like
the old 5m family. **Open question for the next tick, before writing any
strategy code**: does Polymarket actually run a *repeating daily* DOGE
up/down market (the mechanic the operator confirmed), or only these
monthly/longer threshold markets? If only threshold markets exist for DOGE,
the mechanic needs to adapt to that market shape instead — check the Gamma
API for a recurring daily-close market on doge/sol/xrp/bnb before assuming
the 24h-up/down mechanic has a matching market to trade. This is a
precondition, not a detail — get it right before building the signal.

---

## Tick 1 (2026-08-30)

Wrote this scratchpad. Executing the mechanical rebrand (directory moves +
repo-wide text sweep + docs regen + test verification) this tick, per the
playbook above. Strategy design/build is next tick's work, gated on
resolving the "does a repeating daily market exist for these assets" open
question first.

### Market research result (resolves the open question above)

Confirmed via live Gamma API query: Polymarket runs a **daily "Up or Down on
[date]" market per asset** (btc/eth/sol/xrp/doge/bnb at minimum), separate
from the 5m/15m/1h family, resolving on a ~24h cadence. Checked
`solana-up-or-down-on-august-30-2026` directly: outcomes `["Up","Down"]`,
price `[0.505, 0.495]`, **spread 0.07** (wide — 14% of the 0.50 fair range,
notably worse than the old 5m books), `orderMinSize` 5 shares, liquidity
$8.1k, 24h volume ~$401. Confirmed the same shape exists for
`dogecoin-up-or-down-on-august-30-2026` (doge's daily volume was much
smaller when queried by keyword relevance, ~$29-66 on the finer-grained
hourly variants — the full-day version needs a direct slug check per asset,
not a keyword search, to find reliably). **Verdict: tradeable, real market
exists, genuinely wider spread than 5m (consistent with the "less
efficient" thesis) — but this is a market-structure observation, not yet
evidence of an exploitable edge.** Same discipline as every other chapter
here: build the shadow-test, don't presume the edge before measuring it.

Separately, Polymarket also runs **monthly price-threshold markets** per
asset (e.g. `what-price-will-dogecoin-hit-in-august-2026`,
$105.8k volume — an order of magnitude more liquid than the daily up/down
market). Not the confirmed mechanic (daily up/down), but worth knowing
this higher-liquidity alternative shape exists if the daily up/down family
proves too thin to size $10 into cleanly across enough assets for a real
sample.

### New requirements added mid-tick (operator, while rebrand was in
progress) — logged here so a future tick has them even if this session ends

1. **Strategy must scan across markets/assets dynamically**, not run pinned
   to one fixed asset — allocate toward whichever asset currently shows the
   strongest signal within the daily up/down family (doge/sol/xrp/bnb/eth),
   same spirit as the old project's simultaneous multi-asset shadow runs.
2. **New connectors to build, decided this tick:**
   - **Binance** — un-deaden the existing (currently DEAD, zero
     importers) `btc_5m_exec/connectors/binance.py` / its post-rebrand
     path — wire it as the real OHLC/realized-vol input across the alt
     asset set. Clearly load-bearing, building it.
   - **Kraken** — new connector, same shape as Binance (public spot
     OHLC, no auth), for cross-venue price consistency checking and as a
     Binance fallback. Building it.
   - **Deribit** — **flagged, not started.** Deribit only lists options
     on BTC/ETH/(recently)SOL — not doge/xrp/bnb, the thin alts actually
     driving this pivot. An options-implied-probability signal from it
     would only ever cover part of the asset set. Don't build until the
     operator says how partial coverage should be handled (e.g. SOL-only
     signal boost, or skip Deribit entirely).
   - **OpenRouter** — **flagged, not started.** Not a market-data
     connector — an LLM gateway. Needs a stated target (what signal is it
     meant to produce — news/sentiment scoring feeding fair-value? Something
     else?) before building; also the only piece here with a real external
     per-call API cost the operator hasn't sized. Don't build speculatively.
   - **HuggingFace** — already partially integrated
     (`tools/offline_replay.py` pulls historical Polymarket data from an HF
     dataset for BTC backtesting). Extend/generalize this for the alt
     asset set when the backtest-the-new-strategy tick comes up — bounded,
     clear, lower priority than getting the live shadow loop running first.

---

## Tick 2 (2026-08-30) — finished the rebrand, shipped the daily strategy

Continued this same session (not a fresh `/loop` tick — the operator was
present and reviewed the plan before either phase). Two commits.

### Phase A — finished the deferred `BTC_`/`btc_` rename, with a real migration

Tick 1 left one rebrand item unresolved: whether to rename the remaining
`BTC_`-prefixed env vars/config constants and the 4 `btc_*` DB table names.
Flagged the real risk (DB tables hold this environment's actual accumulated
config/history; `BTC_LIVE_CONFIRM` is the literal live-trading safety gate)
and asked the operator — **chose the full rename, DB tables included**, over
a lower-risk skip/env-vars-only option. Executed with a real migration, not
a blind sweep: `db.init_db()` now idempotently `ALTER TABLE`-renames the 4
tables and `UPDATE`-renames the `btc_risk.`/`btc_runtime.`/`btc_model.`/
`btc_recon.` config-key prefixes, verified against this environment's actual
`data/btc_5m_binary_fair_value.db` (all 9 `btc_risk.*` rows migrated to
`risk.*` with values intact, 908→934 tests green including the new ones).
Deleted (not renamed) the `BTC_LIVE_*` deprecated-alias env vars and their
`_trade_knob` plumbing — no `.env` in this repo, nothing external read them.
Left unchanged on purpose: the 3 `btc_live.*` keys in `gate.py`'s pre-#64
legacy-migration path (they must keep pointing at whatever literal key an
ancient DB actually wrote), the default `DB_PATH` filename, and every
asset-scoped identifier that legitimately means "the BTC asset specifically"
(`btc_rows`, `btc_pnl`, `btc_slug`, `tools/backtest_btc_strategy.py`, etc.).
Also fixed two logic bugs the rename surfaced in passing: two
`notification_feed` `event_type LIKE 'btc_%'` filters that would have
silently stopped matching anything once the shared prefix was gone, and
`gen_docs.py`'s `collect_env_knobs()` heuristic, which relied on the same
prefix to exclude non-knob env vars like `PATH`.

### Phase B — daily altcoin Up/Down shadow scanner, shipped end-to-end

New `polymarket_bot/daily/` package (`types.py`, `market.py`, `signal.py`,
`ledger.py`, `scanner.py`) plus a new dashboard panel
(`panels/daily_altcoin.py`) and a new `daily_shadow_positions` table.
Auto-runs as an in-process asyncio task from the dashboard's FastAPI
lifespan (operator's choice — no separate Start/Stop control, since it's
paper-only with zero capital risk); scans every ~60s and opens one $10
paper position on the single strongest qualifying signal across the
tracked assets (operator's choice over one-position-per-qualifying-asset).

**Reuse over rebuild**: `strategy.fair_up_probability`'s log-normal CDF math
and `signal_from_executable_edges` (asset/timeframe-agnostic already),
`shadow/fees.py`'s fee math verbatim, `shadow/ledger.py`'s
`INSERT OR IGNORE` idempotency pattern, `pairarb/market_index.parse_market`
for token extraction, `db.py`'s additive-table + `_migrate_columns`
convention, and the existing `panels/_data.py` → `render()` → `ems.py`
wiring convention. New multi-asset Binance spot/kline fetchers were written
from scratch (small, ~60 lines) rather than resurrecting the dead
`connectors/binance.py` or importing `tools/venue_recorder.py`'s heavier
websocket-based `SpotFeed` (unnecessary at a 60s scan cadence). Kraken
(flagged in tick 1) stayed deferred — no need surfaced building this.

**Three live-verified corrections to the tick-1 plan**, each found by
actually querying the venue rather than assuming:
1. **Market discovery**: tried "list everything, classify it"
   (`tools.venue_recorder.discover()`) first since it doesn't need knowing
   each asset's exact slug spelling — a live run found it unreliable
   (crowded out by unrelated categories platform-wide, silently missed 4/5
   tracked assets). Switched to constructing today's slug directly per
   asset (`{name}-up-or-down-on-{month}-{day}-{year}`) and querying by exact
   slug, with the `discover()` sweep kept only as a per-asset fallback.
2. **Reference price timing**: the market's `description` field (only
   found by actually reading it) states the real comparison window is the
   24h ending at `endDate`, NOT `[startDate, endDate]` — `startDate` is when
   trading *opens*, up to ~2 days before the actual comparison period
   starts. Getting this wrong would have silently fed a stale reference
   price into every fair-value calculation.
3. **Tie rule**: same `description` field states this family resolves an
   *exact* tie 50-50, unlike the BTC family's tie-credits-Up. Reusing
   `strategy.fair_up_probability` unmodified would have overstated Up's
   fair value. `daily/signal.py`'s `fair_up_probability` is the plain
   log-normal CDF with no tie-mass term at all (worked out that a 50-50 tie
   needs no discretization correction, given `fair_down` is always derived
   as `1 - fair_up` rather than scored independently).

**Settlement is self-contained**, not dependent on Polymarket's own
resolution status: a live check found an already-resolved market in this
family stops being returned by the same `?slug=` lookup used to discover it
while open, so re-querying Gamma for the outcome would silently strand every
settlement. Each `daily_shadow_positions` row stamps its own
`reference_price`/`resolves_at`/`binance_symbol` at record time; settlement
recomputes the outcome from Binance directly once `resolves_at` passes.

**Verified end-to-end against the live venue** (not mocked): discovered all
5 assets' markets, scored real signals (ETH qualified first run, edge
+14.3%, entered Down @ 0.25), confirmed idempotency (re-scanning doesn't
duplicate), confirmed the dashboard panel renders correctly with the open
position and mechanism blurb via a live-booted `main.py` + browser check.
Settlement itself could not be verified live this session — the recorded
ETH window resolves at 2026-08-30T16:00:00Z, hours after this tick — so the
next tick (or the operator, whenever they next check) should confirm a
settled row appears with a sane `realized_pnl_usd` sign matching the
recorded side vs. what Binance ETH/USDT actually did.

**26 new tests** (`test_daily_ledger.py`, `test_daily_signal.py`,
`test_daily_market.py`) covering idempotency, fee-true settlement including
the 50-50 tie path, the sigma/drift time-scaling conversion, and the
reference-price-timing regression specifically. 934/934 passing.

### Next tick, if picked up

- Confirm the first real settlement lands correctly (see above).
- Consider whether daily-strategy config knobs (`DAILY_*`) need dashboard
  controls, or env-only is fine for now (operator didn't ask for one).
- Kraken cross-check, Deribit, OpenRouter, HuggingFace backtest extension:
  all still flagged, still not started, still not blocking.
