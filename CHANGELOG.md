# Changelog

## Unreleased — Kelly horse-race and a shared execution layer (2026-09-26)

- Added: `ems/execution/`, the execution layer every strategy shares. Fade's
  tape reader, fill model, result lookup, never-cross check, kill switch and
  PAPER/LIVE read moved there unchanged; on top of them, one resting-order
  interface (`resting.py`: a paper venue that fills only from the real taker
  tape through the queue ahead) and a risk gate with one leg per mode
  (`gate.py`: kill switch, daily loss halt, per-trade and daily caps, new
  "Risk" knobs in SETTINGS).
- Added: Kelly horse-race (`ems/kelly_horse_race/`), on paper. Once per BTC
  15m window it prices the chance of Up from the last hour's move and
  volatility, rolls a die weighted by it for the side, and rests one passive
  buy at that side's best bid with a random size between the venue's minimum
  and $5. Its doc is `docs/strategies/kelly_horse_race.md`.

## Unreleased — one package, every dashboard card folds (2026-09-26)

- Changed: the tree is one package, `ems/`, with `main.py` as the only entry
  point. `polymarket_bot/`, `connectors/` and `dashboard/` moved under it; the
  `sys.path` bootstrap, the `polymarket_bot.*` import names and the empty
  packages left by the strategy removal are gone. SQLite config keys keep
  their `polymarket_bot.*` names so existing databases still read.
- Changed: every dashboard card (FEEDS, MY STRATEGIES, STRATEGY, the fade
  card, SETTINGS) is a collapsible fold. Click the header to collapse or
  expand it; the browser remembers each card's state across refreshes and
  reloads.
- Docs: AGENTS.md, README.md, docs/CODE_MAP.md, docs/ARCHITECTURE.md and
  docs/OPERATIONS_RUNBOOK.md describe the one-package, one-strategy app.
  docs/ROADMAP.md, docs/RESEARCH_LOOP.md and docs/CHRONOS_INTEGRATION.md moved
  to docs/archive/ (they describe removed lines). The `.claude` doc-gen hooks,
  paused for the rebuild, are back on.
- Removed: the `py-clob-client-v2` dependency, the `httpx[http2]` extra and the
  `setup` extra (`polymarket-client`). They backed the deleted live executor
  and wallet tool; nothing in the tree imports them.

## Unreleased — Fade 1h Momentum on 15m is the only strategy (2026-09-26)

Everything but Fade 1h Momentum on 15m was removed at the operator's request:
the BTC Up/Down loop (with its Start/Stop controller, market selector, order
ticket, live CLOB executor and risk gate), the maker, the daily altcoin
scanner, the venue flow and macro recorders and the feed monitor, plus the
dashboard cards, tables, knobs, tools, tests and docs that existed only for
them. The dashboard keeps the FEEDS card (the market-data hub's rows), MY
STRATEGIES, the fade card, the STRATEGY card, SETTINGS (fade's knobs), the
activity log and the strategy docs. Paper only: no live order path exists.
`config.py` keeps the data/dashboard settings, `BOT_MODE` (paper) and the
kill switch; `db.py` keeps `config`, `notification_feed` and the `fade_*`
tables. Existing databases keep their old tables untouched.

## Unreleased — Fade 1h Momentum on 15m runs as a paper strategy (2026-09-26)

A new paper strategy for the BTC, ETH, SOL and XRP 15-minute Up/Down windows,
switch `fade_1h_momentum_15m` on MY STRATEGIES. Paper only: there is no live
order path, and with LIVE selected it places nothing and says so on its card.

- Added: `polymarket_bot/fade_1h_momentum_15m/` (live inputs from the market
  data hub, a standard-library port of the research model for the TWAP-60s
  settlement, the sizing and decision maths, the paper executor and ledger, the
  live learner and the runner), the `fade_windows`, `fade_decisions`,
  `fade_orders` and `fade_dials` tables, the "Fade 1h Momentum on 15m" group of
  settings, and the FADE 1H MOMENTUM ON 15M card.
- Orders are scaled passive limit orders: a parent order split into child
  orders resting at price levels at or under the best bid (a sale: at or over
  the best ask), never crossing the spread. Side, prices and sizes come out of
  the expected log growth of the bankroll, over both sides; each is zero when
  it does not pay. A losing position is cut by a resting sell of the shares
  held, never by buying the other side.
- Paper fills follow the real trade tape through the depth ahead of each order,
  price level by price level, with partial fills; orders keep their place in
  the queue across passes. Windows settle from the venue.
- The dials learn from every settled window, with the research fit of the
  price process held as a fixed prior.
- The FEEDS card now names the strategy as the user of the Chainlink,
  Chainlink TWAP-60s and Binance price streams (was "none yet").
- `runtime_knobs.get` treats a stored choice a knob no longer offers as unset,
  so the default applies and the Settings page shows what the code reads.
- Standard terms throughout: code, tables, settings, the card and docs say
  child orders, price levels and depth ahead.
- `tools/venue_recorder.py`: the guessed market window is stored as
  `window_guess` and `window_evidence` (no archive existed to migrate).
- Maths: `tasks/2026-09-22-fade-1h-sizing.md`. Living doc:
  `docs/strategies/fade_1h_momentum_15m.md`.
- Not done: no job deletes old `fade_decisions` rows (one row per coin a
  minute, so the table keeps growing). Deleting recorded data is the
  operator's call.

## Unreleased — Remove the strategy group split (2026-09-21)

The group field existed only to move copy-trade switches onto their own card.
With copy-trade gone every strategy and family sat in the one group, so the
split did nothing.

- Removed: `strategies.MINE`, `Strategy.group`, `strategies.in_group`,
  `Family.group`, `inventory.in_group`, and the group argument of
  `inventory.by_status`.
- The strategy-doc fingerprint hashes the old constant in place of the field,
  so no doc needed re-stamping.
- No behaviour change: MY STRATEGIES, the STRATEGY card and the docs index
  render byte-for-byte the same.

## Unreleased — Remove copy-trade (2026-09-21)

The operator is cleaning up the app and will pick wallets to follow again,
from research, later. Until then nothing in the process watches or copies
anyone else's wallet.

- Removed: `polymarket_bot/copytrade/` (watcher, trader, ledger, targets —
  the one registered wallet — and the 30-minute backup), the COPY TRADE and
  COPY TRADE WALLETS dashboard cards with their JS and CSS, `POST
  /api/copy-fill`, the copy watcher started at dashboard boot, the nine
  `copy_*` knobs (the SETTINGS card's "Copy trade" group), the
  `copy_macro_daily` and `copy_autocopy` switches, and both copy entries in
  the strategy inventory (only the v1 CLI one showed on MY STRATEGIES).
- Removed with it: `polymarket_bot/pairarb/mirror.py` (copy pricing — its only
  callers were copy-trade), `tools/copytrade_shadow.py`,
  `tools/copytrade_dashboard.py`, `config.POLYMARKET_DATA_API` (only the
  watcher read it), and the dashboard's scroll-keep hook (only the copy fill
  list used it).
- Kept: `tools/wallet_research/` — the research used to choose wallets.
- Layout: MY STRATEGIES now spans both columns, so the removed wallets card
  leaves no gap beside it.
- Recorded data left alone: the `copy_trades` and `copy_decisions` tables,
  `data/copytrade_snapshots/`, and the stored `runtime.copy.*` and
  `runtime.strategy.copy_*.enabled` settings stay on disk. Nothing reads or
  writes them any more, and nothing drops them.

## Unreleased — Remove the shadow forward-tester (2026-09-19)

The shadow forward-tester recorded what candidate parameter sets *would* have
traded on every BTC window — and no dashboard panel ever showed any of it. Work
the operator cannot see is work the operator cannot learn from, so it is gone.
Every strategy left in the process appears on the dashboard's STRATEGIES card.

- Removed: `polymarket_bot/shadow/` (runner, signal roster, ledger, types), the
  `_record_and_settle_shadow` path in `paper.py`, `SHADOW_ENABLED`, the
  "shadow roster" tag on the FEEDS card, and its row on the STRATEGIES card
  (now BTC Up/Down loop + daily altcoin scanner).
- Removed with it: `tools/shadow_performance.py`, `tools/replay_race.py`,
  `tools/race_status.py`, `tools/regime_attribution.py` — all four existed only
  to analyse the race, and nothing feeds them any more.
- Moved, not removed: the Polymarket taker-fee math is now
  `polymarket_bot/fees.py`. It was only ever in `shadow/` by accident — the live
  executor, the daily ledger and pairarb all import it. Math unchanged.
- Recorded rows are left alone: `model_shadow_positions` is no longer created
  or written, but existing data stays on disk. Nothing drops it.

## Unreleased — Archive the v0 strategy (2026-09-13)

The operator shut down the v0 BTC 5-minute strategy to make room for new ones.
The loop still runs and journals market data, but no strategy is loaded, so it
takes no entries in paper or live (`skip: no strategy loaded`). Details and
restore steps: `docs/archive/v0-strategy.md`; code preserved at tag
`archive/v0-strategy`.

- Removed from the loop: v0 entry gates, model picker, edge-decay auto-pause,
  calibration, param tuner (`adaptive.py`, `calibration*.py`, `params*.py`,
  `tools/clear_auto_pause.py`, `AUTO_PAUSE_*` knobs).
- Removed from the dashboard: STRATEGY card, decision-engine GATES column,
  model picker, auto-pause chip, and the SETTINGS card's entry-gate and
  auto-pause knobs.
- Kept: live-path safety gates (`RiskGate`), shared pricing math, shadow
  forward-tester, daily altcoin scanner.

## v1.0.2 — Rename project to polymarket-crypto (#184) (2026-08-30)

Following the branch close-out in #182 / commit 27f38fd: 5-minute-market work is closed
and the next chapter (daily-window altcoin markets, #185) is not BTC- or 5-minute-specific.
The old name — folder, GitHub repo, package `btc-5m-exec`, module dirs — was inaccurate
BTC-5m-era branding. Mechanical rename plus a small set of honest status rewrites, no
behavior change: 908 tests still green.

- **`btc_5m_exec/` → `polymarket_exec/`**, **`btc_bot/` → `polymarket_bot/`** (`git mv`,
  history preserved); package name `btc-5m-exec` → `polymarket-crypto` in `pyproject.toml`;
  all imports, docstrings, comments, CI config, and current-state docs (README, AGENTS.md,
  `docs/*.md`) updated to match.
- Deleted `btc_5m_fv/` (confirmed dead — untracked, only `__pycache__`/`.DS_Store`,
  superseded by #169) and the stale `btc_5m_exec.egg-info/` build artifact.
- `docs/FILE_MAP.md` and the `<!-- GENERATED -->` blocks in `AGENTS.md`/`docs/CODE_MAP.md`
  regenerated via `tools/gen_docs.py` — not hand-edited.
- **Substantive, not cosmetic**: `AGENTS.md`'s Scope Fence previously named "BTC 5-minute
  Up/Down" as the sole authorized live-trading market. Updated to state no market is
  currently authorized for live trading — 5-minute work closed, no replacement category
  chosen or built. Strictly more restrictive than before, cannot enable anything that
  wasn't already stopped.
- Left unchanged (deliberately): the `data/btc_5m_binary_fair_value.db` default filename
  (`config.py`, `.env.example`) — same reasoning as #169: a data-layer identifier, not code
  branding, renaming it risks silently pointing at a new, empty DB. Also left unchanged:
  historical records that would be falsified by editing them — every `CHANGELOG.md` entry
  below this one, `tasks/*.md` session logs, and the dated per-issue archives under
  `docs/specs/` and `docs/superpowers/`.
- Also renamed: the GitHub repo (`zayansalman/polymarket-btc-5m-pricing` →
  `zayansalman/polymarket-crypto`, old URL redirects) and the local project folder.

## v1.0.1 — Remove "fair value" branding (#169) (2026-08-04)

Reopen housekeeping before any new build work (see the 9-issue reopen scope filed this
session). "Fair value" overclaimed rigor the strategy never had — the logic is empirical
entry-gate heuristics layered on a market-implied probability, not a derived fair value.
Mechanical rename, no behavior change: 828 tests still green.

- **`btc_5m_fv/` → `btc_5m_exec/`** (matches its real role per `docs/CODE_MAP.md`: execution
  gate + dashboard + backtest + connectors); **`strategy/fair_value.py` →
  `strategy/pricing_model.py`**.
- Model-id literals `fair_value_v0` → `pricing_v0`, `fair_value_fresh_v8` → `pricing_fresh_v8`
  (`btc_bot/shadow/runner.py`, `signals.py`); dashboard dropdown/labels follow.
- Package name `btc-5m-fv` → `btc-5m-exec` in `pyproject.toml`; all imports, docstrings,
  comments, and current-state docs (README, AGENTS.md, `docs/*.md`) updated to match.
- `docs/FILE_MAP.md` and the `<!-- GENERATED -->` blocks in `AGENTS.md`/`docs/CODE_MAP.md`
  regenerated via `tools/gen_docs.py` — not hand-edited.
- Left unchanged (deliberately): the `data/btc_5m_binary_fair_value.db` default filename
  (`config.py`, `.env.example`, `Dockerfile`, `tools/*.py`) — a data-layer identifier, not
  code branding; renaming it risks silently pointing a live deployment at a new, empty DB
  file. Also left unchanged: historical records that would be falsified by editing them —
  every `CHANGELOG.md` entry below this one, `tasks/*.md` session logs, and the dated
  per-issue archives under `docs/specs/` and `docs/superpowers/`.

## v1.0.0 — FINAL: research archive (2026-07-10)

The program reached its pre-registered verdict and closed. Post-freeze out-of-sample
segment (data recorded after the final candidate's spec was frozen in PR #152):
f45 −$0.41/trade over n=37 at WR 0.486; the whole fresh family flipped negative; the live
shadow book agreed (−$3.79/n12, replay-consistent 12/12). Deploy rule required
sign-consistent segments → FAILS → bot stopped, live never re-enabled, repo archived.

Final ledger: real money −$19.35 across 351 fills (fees exceeded 100% of the loss);
2,924 shadow positions across 10 variants; 828 tests green in a clean-venv install.

Week of instrumentation shipped by the agent ops-loop before close (PRs #152–#164):
f45 signals + replay grid (#149), race_status CLI (#150), honest feed labels (#151),
silent-stop alerts (#138), vol/basis regime columns (#122), f45 roster arm (#155),
tick-cadence stall detection (#157), maker/taker placement telemetry — 23% maker share
(#137), deploy-bar min-n guard, forecast_journal pilot tool (#162). Plus decision docs:
docs/archive/PIVOT_2026-07.md and the full audit trail in tasks/race_log.md.

## v0.4.27 — Tick-replay backtest + v8 pre-registered (2026-07-02)

`tools/replay_race.py` (#144, PR #145) replays the full quote history (74,580 ticks, 1,626 labeled windows from Jun 11) through the current roster, fee-true. Validation first: outcome labels agree 564/564 with ground truth (next-window reference print); the harness reproduces the recorded shadow v2 ledger 249/249 windows exactly.

- **cushion_fresh_v7's CI excludes zero** on the full period (+0.344/trade [+0.072, +0.617], n=272) AND on the pre-race half its gates never saw (+0.346 [+0.005, +0.687], n=164) — near-identical expectancy across a regime change.
- Fragility grid: the **freshness gate carries most of the effect** (v0+fresh60: largest totals in both independent halves, BH-q<0.05 each; 99.4% of first-60s signal ticks had ≥5-share depth) → pre-registered **`fair_value_fresh_v8`** (v0 + first-60s only). Roster is now a clean ablation: v0 / v2 (cushion) / v7 (all gates) / v8 (fresh only).
- Engine restarted on the 4-model roster (paper mode). Deploy bar unchanged: live only when the LIVE race CI clears zero net of fees — at current point estimates that is ~2–3 weeks of race data, not 5–6.

## v0.4.26 — Roster surgery + race restart (2026-07-02)

Operator mandate: bin the losers, build better candidates (#142, PR #143).

- **Binned** `late_convergence_v3` (favorite-soak trap), `down_skeptic_v4` (IS→OOS rank flip), `cushion_drift_v5` (redundant with v2), `down_skeptic_drift_v6` (worst everywhere). History stays in the ledger; −695 lines of retired signal code.
- **New challenger `cushion_fresh_v7`** = v2 + first-60s freshness gate (+$88.57 of family profit sat in 0–60s; every later bucket negative) + 0.065 claimed-edge cap (larger claims realized worst). Gates frozen a-priori; the resumed race is the out-of-sample test.
- **Retired-model heal**: the persisted active model (`down_skeptic_drift_v6`) now falls back to the v0 native path with a loud one-time notification (`_resolve_active_model`).
- Rejected in recon (not built): both-sides pair-arb (only 2–2.5% of sub-$1 ticks persist one tick — stale-book flickers) and a regression-calibrated EV gate (unidentifiable coefficients).
- Race restarted 2026-07-02 in **paper mode** on the new roster (v0 control / v2 champion / v7 challenger); stale position 1768 settled organically (+$2.49 gross; next reconcile trues to +$2.41 net).

## v0.4.25 — Postmortem: boot heal, fee-true books, ledger reconciled (2026-07-02)

Forensic postmortem of the 06-25 outage and the full trade history (docs/archive/POSTMORTEM_2026-07.md, #132–#138). Venue truth: bot-era PnL **−$17.24** = +$6.27 gross signal − $23.51 taker fees; the books had shown −$8.01/−$3.10 (fee-blind).

- **#132** — boot reconciliation no longer hard-refuses on CLOB-pruned entry orders (the outage): resolved-window rows close as `RECONCILED_STALE_RESOLVED`; unresolved rows adopt the journal's placement match; refusal reserved for genuinely unknowable live risk.
- **#133** — fee-true booking: the venue's taker fee (`0.07·p·(1−p)`/share, USDC, on the placement-crossed portion) is captured at entry and booked at settlement/exit; ledger row = journal = daily-halt to the cent; fee math shared with the shadow ledger.
- **#134** — ledger reconciled to venue records (324 corrections, 4 phantoms voided): live closed PnL −$3.10 → −$17.77; stranded position 1768 (won +$2.41, auto-redeemed 30 s after the crash) heals on next boot.
- Postmortem doc adds the pre-registered restart protocol: shadow-only ≥6 weeks, deploy bar = 95% CI > 0 net of fees; night-gate and selectivity hypotheses formally dead (FDR/permutation/OOS).

## v0.4.24 — Fix red CI: declare numpy for the regime-attribution tool (2026-06-23)

`tools/regime_attribution.py` (#120) imports numpy, but numpy was declared nowhere — so a clean CI install (`pip install -e .[test]`) couldn't import it, failing **two hard gates**: the test job (`ModuleNotFoundError` collecting `test_regime_attribution.py` → whole suite aborts) and docs-drift (`gen_docs` can't introspect the tool → AGENTS.md/CODE_MAP.md drift). Local/dev venvs had numpy, masking it.

- **`pyproject.toml` `[test]` extra** + **`requirements.txt`** — declare `numpy` (matching how `polars`/`huggingface-hub` are handled for the offline-replay tool; same drift class as #79/#126). Verified in a clean venv: 731 tests pass, `gen_docs --check` clean.

## v0.4.23 — Prod-readiness for the HTTP/2 fix + trailing-halt message (2026-06-23)

Caught by the pre-main-merge safety gate before promotion.

- **`requirements.txt`** (BLOCKER) — pin `httpx[http2]==0.27.2` (was `httpx==0.27.2`). The prod Dockerfile installs from `requirements.txt`, not pyproject, so v0.4.22's `http2=True` client would have `ImportError`'d (no `h2`) on every tick in a fresh prod container — a full trading outage. CI missed it because CI installs the pyproject `.[test]` extra. Same drift class as v0.4.17. Verified in a clean venv: the pin pulls `h2` and `AsyncClient(http2=True)` constructs.
- **`btc_bot/paper.py`** — the loss-halt stop message (`_loss_halt_stop_detail`) now cites the **trailing floor** (`peak − limit`) instead of the fixed `-limit`; with the #112 trailing halt the stop can fire at a positive PnL, where the old wording was nonsensical. Enforcement was already correct — display only.

## v0.4.22 — Fix: bot never trades — crypto-price reference 403 over HTTP/1.1 (2026-06-23)

The live bot SKIPped every window (`skip: settlement feed degraded`, `reference_price=0`, zero entries). Not the signal logic — the **reference data feed** was blocked.

### Root cause
Polymarket's `crypto-price` reference endpoint is behind Cloudflare bot management, which now 403s ("Just a moment...") a Chrome-spoofed request sent over **HTTP/1.1** — real Chrome speaks HTTP/2, so the UA-vs-protocol mismatch reads as a bot. The connector already sent browser headers, but the client defaulted to HTTP/1.1. Measured from the bot's host: HTTP/1.1 **0/5**, HTTP/2 **5/5** (plain curl over h2 also 200) — so it's the client, not the IP.

### Fix
- **`btc_bot/paper.py`** — new `_make_settlement_client()` factory builds the reference/settlement httpx client with `http2=True`; both client sites use it. Over HTTP/2 the reference fetch returns a valid `openPrice`, so fair value computes and the bot can trade again.
- **`pyproject.toml`** — `httpx[http2]` is now a hard dependency (the `h2` extra is load-bearing, not optional).
- Test guards that the factory enables HTTP/2.

## v0.4.21 — Reset halt also clears the adaptive auto-pause (2026-06-23)

Operator ask (#117): "I should be able to clear auto-pause when I hit Reset halt." The Reset button is now the single "let me trade again" control.

### What
- **`btc_bot/adaptive.py`** — `clear_auto_pause()` records a `cleared_at` timestamp; `evaluate_and_maybe_pause()` scopes the rolling edge window to trades after `max(session_start, cleared_at)`. So a manual clear **actually resumes entries** instead of re-pausing on the same losing streak on the next tick — while the guard still re-protects once enough *fresh* post-clear trades fall below the floor.
- **`/api/loss_halt/reset`** — always clears the auto-pause (a live config the running loop honours next tick, so it works while running); resets the loss-halt tally + peaks only when **stopped** (the loop owns those in memory). Returns `halt_reset` + `auto_pause_cleared`.
- **`guardrails.py`** — the Reset halt button is enabled when running-but-auto-paused (not just when stopped), so the operator can resume without stopping the bot.

### Verification
- **699 tests green**, ruff clean, zero new mypy on changed files.

## v0.4.20 — Trailing loss halt + PnL panel accuracy + live open-position P&L (2026-06-23)

Three operator asks from one session: protect banked profit with a trailing halt, make the PnL/performance panels honest about what they show, and surface live unrealized P&L on an open position.

### Trailing high-water-mark loss halt (#112, #115)
- **`gate.py`** — the daily loss halt now trails the session **peak** realized PnL: `floor = peak − BTC_TRADE_DAILY_LOSS_HALT_USD`, peak ratchets up only and resets at UTC midnight, tracked per leg (live/paper) and persisted as `btc_risk.{live,paper}_peak_pnl`. Banked profit can no longer be bled back beyond the limit — a +$30 run halts at +$20, not −$10. A **never-profitable session keeps peak 0**, so behaviour is identical to the old fixed −$10 floor; the change can only halt *earlier* (after locking gains), never later. Backward-compat load derives `peak = max(0, leg_pnl)` when the key is absent. New `halt_peak` / `loss_halt_floor` / `loss_halt_headroom` properties; `loss_halt_breached` and `block_reason` use the trailing floor.
- **`#115`** — `reset_daily_loss_halt()` + `/api/loss_halt/reset` now zero the **peaks too**, not just the PnL tally: the floor is `peak − limit`, so resetting PnL alone would leave a banked peak holding the halt latched (a +$30 day reset to PnL 0 still floors at +$20).
- **`ems.py` + `guardrails.py`** — the LOSS HALT panel shows **Peak**, **Halt floor**, and trailing **Headroom**; the panel formula mirrors `RiskGate.loss_halt_breached` (the enforcement truth).

### PnL / performance panel accuracy + open-position widget (#113)
- The panels are already structurally **BTC-only** (the bot only trades `btc-updown-5m-*` markets, so non-bot Polymarket trades never enter the ledger). This fixes the real gaps:
  - **`tools/reconcile_live_ledger.py`** — isolate BTC by the slug prefix `btc-updown-5m-` instead of the fragile `"Bitcoin Up or Down" in title` substring (immune to null/renamed titles and non-bot markets that merely mention Bitcoin; verified on real data: 648/648 BTC, 0 false positives).
  - **`performance.py`** — relabel the recon footer `account` → `account (incl. non-bot)` so the whole-account figure can't be misread as the bot's; add a **freshness badge** (`assumed-fill` vs `reconciled <date>`) so the operator knows whether the headline metrics are grounded to real fills.
  - **`market.py` + `_shared.side_mid`** — an **OPEN POSITION** block in the LIVE MARKET card showing live unrealized P&L `(mark − entry) × shares`, marked at the current side **mid**; a position in a non-live window shows `—` (no fabricated mark).
  - **`blotter.py`** — open rows show the same live unrealized instead of a static `OPEN` for the live window.

### Verification
- Full suite **686 green**, ruff clean, zero new mypy on changed files; end-to-end dashboard render confirmed against the live DB.

### Backlog
- **#114 [P2]** — persist `conditionId`/`token_id` on `btc_paper_positions` so per-window reconciliation can deconflict the rare case where the operator manually trades the same 5-min window as the bot.

## v0.4.19 — Restore full model roster to the strategy-model selector (2026-06-22)

Reverses the #100 roster trim per operator request (#111): all six logged models are operator-selectable from the dashboard again.

### What
- **`btc_bot/shadow/runner.py`** — `SELECTABLE_MODELS` is now the full roster in vN-experiment order (`fair_value_v0`, `cushion_favorite_v2`, `late_convergence_v3`, `down_skeptic_v4`, `cushion_drift_v5`, `down_skeptic_drift_v6`); the former silent controls (`fair_value_v0`, `cushion_favorite_v2`) are no longer hidden from the dropdown.
- **`btc_bot/shadow/signals.py`** — **resurrects `late_convergence_v3`** (deleted in #100), restored faithfully from git `39649c8`; re-registered in `_MODELS`, `MODEL_LABELS` ("Late Convergence (v3)"), `MODEL_DESCRIPTIONS`, and `CANDIDATE_SIGNALS` (live-dispatchable).
- **`panels/controls.py`** — comment refresh (controls no longer hidden); the orphan guard that always renders an unknown active model is unchanged.
- **Tests** — `TestLateConvergenceV3` restored (11 cases); `test_shadow_runner` asserts the full selectable roster; `test_dashboard` lists all six options and exercises the orphan guard + allow-list rejection with a genuinely unknown id. Full suite **656 green**, ruff clean.

### Caveat
`late_convergence_v3` is net-negative in shadow (91% win rate, ~-$14 PnL — the favorite-soak trap). It is on the live selector at the operator's explicit request, not because it has a proven edge.

## v0.4.18 — Stop phantom fills at settlement on a venue lookup failure (2026-06-22)

Addresses #109 — the dominant live-PnL inflation channel surfaced by the strategy assessment + reconciliation (#102/#103). A **settle-style** position whose entry **rested and never matched on-venue** was booked at the **full submitted size** when the `get_order` fill lookup failed at settlement — manufacturing a phantom WIN (`won`) or phantom LOSS (`!won`). This is why the as-booked live ledger showed a profit that flips to a real loss once the never-executed rows are voided.

### Root cause
`_order_fill_info` returns its `default_size` on lookup failure. The entry callers passed `default_size=self._entry_size` (**assume fully filled**) — correct for the EXIT/SELL path (under-selling strands real tokens) but wrong for SETTLE (no SELL, so over-counting books fiction). The optimism leaked into `record_settlement` via `cancel_open` (live.py:960) and `_matched_entry_size` (live.py:1109).

### What
- **`btc_5m_fv/execution/live.py`** — `_matched_entry_size` and `cancel_open` gain `assume_filled_on_error` (default `True`, preserving exit/flatten/kill behavior). `record_settlement` passes `False`, so a failed venue lookup on a never-matched entry settles to **held=0** — no phantom PnL, in either direction.
- **Tests** (`test_live_executor.py`): phantom-WIN, phantom-LOSS, and an exit-vs-settle asymmetry guard. Full suite **646 green**, ruff clean.

### Not fixed here (follow-up)
A distinct, smaller channel — premature loss-booking of *matched-but-unresolved* windows from settlement-source (Chainlink vs Polymarket) timing — needs its own reproduction and is tracked separately, not rushed in.

## v0.4.17 — Fix red CI: offline-harness deps missing from pyproject test extra (2026-06-22)

`develop` CI was failing. CI installs `pip install -e ".[test]"` (pyproject), but `polars` and `huggingface-hub` lived only in `requirements.txt` — so CI never installed them and `tests/unit/test_offline_replay.py` + `test_chainlink_lead_lag.py` broke pytest **collection** (`ModuleNotFoundError: polars`), failing the test job and zeroing `docs-drift`'s `count_tests()` (#79). Passed locally only because the dev venv happened to have polars (requirements/pyproject drift).

### What
- **`pyproject.toml`** — add `polars>=1.17`, `huggingface-hub>=0.26` to the `test` optional-dependencies group so `.[test]` (and therefore CI) installs them. No runtime/core dependency change.
- **Verified** in a clean venv mimicking CI (`pip install -e ".[test]"`, no requirements.txt): the two modules now collect (12 tests); full local suite **643 passed**.

## v0.4.16 — Selector labels carry the model version (2026-06-22)

Addresses #108. The shadow-model dropdown showed semantic labels (`Down-Skeptic`, `Down-Skeptic · Regime Drift`) that didn't map back to the persisted `model_id`s (`down_skeptic_v4`, `down_skeptic_drift_v6`). The version jump compounds the confusion: down-skeptic is **v4** and its regime-drift child is **v6** because the `vN` suffix is a *global* experiment counter — `cushion_drift` (v5) was logged between them — not a per-family version.

### What
- **`btc_bot/shadow/runner.py`** — `MODEL_LABELS` now appends the version tag, e.g. `Down-Skeptic (v4)`, `Down-Skeptic · Regime Drift (v6)`, so each dropdown label maps 1:1 to its `model_id`. **Display-only**: the `model_id`s — persisted keys behind 828 ledger rows, the `btc_model.active` pointer (currently `down_skeptic_v4`), and ~121 code refs — are untouched. No rename, no DB migration, no behavior change.
- **Tests**: `test_shadow_runner.py` green (label/description key coverage unchanged); ruff clean.

## v0.4.15 — Real exit fill price + reconcile staleness guard (2026-06-22)

Completes #103's fill-price work. The **exit** (SELL) path recorded the posted limit, mirroring the entry bug fixed in v0.4.14. Now both sides record the real fill.

### What
- **`btc_5m_fv/execution/live.py`** — `submit_exit` registers exit fills at `_avg_fill_price(result.raw, SELL, price)` (a SELL's realised price is `takingAmount`/`makingAmount`), with the same safe limit fallback for resting/unmatched orders. Exits are rare (≈7 vs 383 buys) and most positions settle held-to-resolution, so the entry price dominates — this just makes the recording symmetric and correct.
- **`tools/reconcile_live_ledger.py`** — **staleness guard**: `--apply` now refuses when the ledger holds live positions newer than the Data-API snapshot (they'd be voided as phantoms before the API indexes them — the near-miss from the manual run). `--force` overrides.
- **Tests** (`test_live_executor.py`): `_avg_fill_price` direct unit coverage (BUY ratio, SELL inverse, limit fallbacks). Full suite **643 green**, ruff clean.

### #103 closed
Phantom wins + paper double-count (v0.4.13), real entry fill price (v0.4.14), real exit fill price + staleness guard (this). **Auto post-stop reconcile is intentionally not built**: with the recording fixed at the source (real fills, no phantoms, no double-count), the ledger now stays accurate going forward, so auto-mutating the financial ledger on a Data-API pull (which races API indexing) would add risk for no benefit. The manual reconcile tool remains as an audit backstop; fees are captured there via real USDC flows.

## v0.4.14 — Record the real average fill price, not the limit (2026-06-22)

Addresses #103. A matched live entry recorded `self._entry_price = price` — the posted **limit**, not the price the order actually filled at. Since `_entry_price` feeds settlement PnL, an order that filled *better* than the ask (the common case, buying at the best ask into a moving book) silently **overstated** PnL.

### What
- **`btc_5m_fv/execution/live.py`** — new `_avg_fill_price(response, side, limit)`: a market-matched order reports `makingAmount` (USDC paid) / `takingAmount` (tokens received), so the realised price is their ratio (inverse for a SELL). `submit_entry` now records that real average for a matched fill. **Safe fallback:** any missing/unparseable amount, or a price outside `(0, 1]`, returns the limit — zero regression for resting/later-matched orders.
- **Tests** (`test_live_executor.py`): matched response with `makingAmount/takingAmount` records the real avg (0.55, not the 0.57 limit); a response without `makingAmount` falls back to the limit. Full suite **640 green**, ruff clean.

### Fees & remaining for #103
Real **fees** are already captured by the Data-API reconciliation (`tools/reconcile_live_ledger.py`, #102) — it books economic PnL from actual USDC flows, net of any fee. The order-placement response does not reliably expose a per-fill fee (and real fees on these 5m markets are ≈ 0). Remaining hardening: real **exit** fill price (entry is the dominant PnL term for held-to-resolution), and an automatic post-stop reconcile so the ledger self-heals each session.

## v0.4.13 — Settle from real held size; stop phantom wins + paper double-count (2026-06-22)

Addresses #103 (the root cause behind the #102 reconciliation). At settlement, `record_settlement` already books the **real** matched/held size to the live counter — but `_close_position(settled=True)` was booking the **ledger** row from `pos["shares"]` (the *recorded* size, which includes entries that never filled on-venue). Two bugs:

1. **Phantom wins** — a never-filled entry (`held=0`) booked `recorded_shares × (payout − entry)` as a fictional profit into the ledger (the 6 phantoms / +$14.44 the reconciliation found).
2. **Paper double-count** — a *live* settled close also called `record_realized_pnl(is_live=False)`, polluting the **paper** PnL leg with live PnL (the bug behind `paper_realized_pnl` mirroring `live_realized_pnl`).

### What
- **`btc_bot/paper.py`** — `_close_position` gains `settled_held`; the settled branch splits: a **live** settle uses the executor's real held size for the ledger row (so `held=0` → PnL 0, no phantom) and does **not** re-book to the gate; **paper** settles keep the existing `is_live=False` halt feed. `_settle_position_outcome` threads `record_settlement(...).size` through.
- **Tests** (`test_settle_style.py`): real-held sizing (4 of 6 filled → books 2.0 not 3.0), phantom books 0, live settle skips the paper counter. Full suite **638 green**, ruff clean.

### Remaining for #103 (follow-up)
- Capture the **actual average fill price** (not the limit) and **taker fees** from the CLOB order response; settle from the venue's real resolution. A lightweight periodic Data-API reconcile to self-heal drift.

## v0.4.12 — Reconcile live ledger to real Polymarket fills (2026-06-22)

Closes #102. The dashboard/DB did not reflect the real Polymarket account. Verified read-only against the Polymarket **Data API** (`/activity` + `/positions` + `/value`, funder `0xc1Daa…00c5`):

| | DB claimed | Real (Polymarket) |
|---|---|---|
| Bot live PnL (recorded windows) | **+$3.33** | **−$6.74** |
| BTC bot (full history, realized) | — | **−$14.51** |
| Whole account (incl. manual non-BTC bets) | — | **−$34.10** |

The ~$20 overstatement was driven by **6 phantom positions** (+$14.44 of fictional wins — orders the bot recorded as filled+won that **never executed on-venue**), plus fee/fill-price drift. Resolved-window PnL was already accurate (Δ −$2.56 across 119). Root cause tracked in #103 (live.py books assumed fills/resolution/zero-fees).

### What
- **`tools/reconcile_live_ledger.py`** — idempotent, auditable (`--dry-run`/`--apply`). Per window, economic PnL = `sell + redeem + open_currentValue − buy_cost`. Rewrites the 191 real `btc_paper_positions(mode='live')` rows to real entry/exit/shares/notional/PnL (tagged `recon:dataapi`, resolution-disagreements flagged), voids the 6 phantoms (`state='void'`), writes `btc_recon.*` truth keys. Atomic transaction; DB backed up first.
- **Dashboard "Reconciled vs Polymarket" line** (`panels/performance.py`, `panels/_data.py:reconciliation()`, `ems.py`): surfaces the whole-account ground truth (real BTC PnL, account PnL, open value, as-of) under the LIVE/PAPER cards — the per-window rows can't show it.
- **Process**: reconciliation only runs against a **stopped** bot (frozen ledger) with a **fresh** Data-API pull; the agent never stops/starts live trading.
- **Tests**: `test_dashboard.py::TestPerformanceReconLine`. Full suite green (DB-isolated), ruff clean.
## v0.4.11 — Regime-aware Down-Skeptic + roster trim (2026-06-22)

Closes #100. The live bot bled on a one-sided book: the last 15 live trades were **15/15 Up, 6W/9L, net −$9.71 (−24.3% ROI)**. Root cause was structural — the active model `down_skeptic_v4` charges a **fixed +0.02 edge toll on every Down pick** (to fight v0's `spot >= reference` Up bias), which leans the book almost entirely Up. That is correct in a flat/up market but backwards when the regime turns **bearish** (over the same window, Down bets won 8/9). At ~0.53 entries the asymmetric payoff (break-even win rate 53%) turns a sub-53% Up hit-rate into a steady bleed.

### What
- **New shadow candidate `down_skeptic_drift_v6`** (`btc_bot/shadow/signals.py`): v4's exact structure (reuse v0's side pick, gate by an edge toll), but the toll **flexes with the same standardised-momentum regime as `cushion_drift_v5`** — `regime = clamp((drift/σ)/0.3, −1, +1)`. `down_extra = 0.02·clamp(1+regime, 0, 2)` and `up_extra = 0.02·clamp(−regime, 0, 1)`: a bear regime tolls Up and frees Down; a bull regime strengthens the Down toll. At `regime == 0` (or no drift feed) it is **byte-for-byte identical to `down_skeptic_v4`**, which is therefore its exact control.
- **Roster trim** (`btc_bot/shadow/runner.py`): new `SELECTABLE_MODELS = [down_skeptic_v4, cushion_drift_v5, down_skeptic_drift_v6]` **decouples** the operator selector from the logged set. `fair_value_v0` + `cushion_favorite_v2` keep logging as silent controls but are hidden from the dropdown; `late_convergence_v3` is **removed entirely**.
- **Selector wiring** (`panels/controls.py`, `app.py`): the dropdown and the `POST /api/runtime-config` allow-list both use `SELECTABLE_MODELS`, with an orphan guard so the currently-active model always renders even if hidden.
- **Shadow-first:** v6 logs alongside live from registration; the agent does **not** flip `btc_model.active` (stays `down_skeptic_v4`). The operator promotes v6 via the selector after a validation window.
- **Tests**: `test_shadow_signals.py` (v6 ≡ v4 at regime 0 / no drift; bear vetoes a thin Up v4 takes; bull vetoes a thin Down v4 keeps), `test_shadow_runner.py` (registries + `SELECTABLE_MODELS`; late_convergence gone), `test_dashboard.py` (selector lists only selectable; orphan guard; allow-list rejects hidden controls). Full suite **633 green** (DB-isolated), ruff clean, no new mypy errors.

### Out of scope (separate issue)
- DB/panel reconciliation vs **real Polymarket fills** — the zero-fee assumption, assumed-fill-price-as-limit, and the four divergent on-screen live-PnL numbers (+8.92 / +6.98 / −2.75 / −7.30). Investigated; to be reconciled separately.

## v0.4.10 — Heal phantom "max 1" singleton block (live) (2026-06-17)

Closes #91. Live trading silently stopped: the BLOCKED panel showed every entry rejected with **"an open position/order already exists (max 1)"** while the position ledger was **flat (0 open rows)** and Polymarket history showed the last position fully **bought and sold** (flat, funds intact). The bot's trade blotter and Polymarket history therefore *agreed* — both flat — but the live executor's **in-memory** singleton flag was stranded `True`, so it blocked forever until a clean restart.

**Root cause** (from `btc_live_orders` + the Polymarket history): the singleton gate (`gate.py:block_reason`) blocks on `position_open or entry_order_resting`, and for live those read the executor's in-memory `self._position_open` / `self._entry_order_id` (`live.py`). Two ways they stranded:
1. A **fully-matched** entry (`status='matched'`, filled shares in `takingAmount`) kept its `_entry_order_id` set — but a filled order is **not resting**, so `entry_order_resting` lied. Polymarket confirms a matched BUY can't be cancelled (`"matched orders can't be canceled"`).
2. Across the operator's **rapid stop/start** cycle (~10 `BOOT_RECONCILE` in 20 min), an interrupted shutdown left `_position_open=True` with no matching open ledger row — and boot reconciliation only re-syncs *from* an open ledger row; it never heals a phantom flag when the ledger is already flat.

### What
- **`btc_5m_fv/execution/live.py`** — two-part fix:
  - **`submit_entry`**: a fully-matched entry (new `_filled_shares(response)` reads `status`/`takingAmount`) drops `_entry_order_id` and records `_entry_matched_size` instead, so `entry_order_resting` stays honest (the open *position* holds the max-1 slot) and the next flatten skips a doomed "matched orders can't be canceled" round trip.
  - **`resync_flat()`**: heals stale in-memory open-state. Called by the entry path once the ledger is confirmed flat; cancels anything still tracked on the venue (cheap insurance), then clears the slot. **Safe by invariant:** a live ledger row is closed only after a *confirmed venue flatten* (`paper._close_position`), so ledger-flat ⟹ venue-flat — clearing can never strand real tokens.
- **`btc_bot/paper.py`**: `_maybe_open_position` calls `await executor.resync_flat()` right after the `COUNT(open)=0` check, so a phantom **self-heals on the next tick** — no restart, no manual DB edit.
- **Tests**: `test_live_executor.py` — fully-matched entry isn't tracked as resting; `resync_flat` heals a phantom (and cancels the stale order) / no-ops when already flat. `test_live_wiring.py` fixture gains `resync_flat`. Full suite green (558, +3); ruff clean; no new mypy errors.

### Why this shape
- The **position ledger is the source of truth** (it's what reconcile trusts, the dashboard shows, and Polymarket matched). The in-memory flags are a cache that can go stale under rapid restarts; healing them against the authoritative-and-safe "ledger flat ⟹ venue flat" invariant is minimal and cannot abandon real exposure.
- **Out of scope:** the restart *storm* itself (operator/tooling driving ~10 boots in 20 min) — filed separately; this change makes the bot resilient to it rather than depending on clean shutdowns.

## v0.4.8 — Set trade size in shares from the CONTROLS panel (2026-06-17)

Closes #89. The operator thinks in shares (contracts), not dollars — a binary's $ cost varies with price. The CONTROLS panel now takes a **share count** (≥5, the Polymarket minimum), shows the **$ value** of that many shares at the live price (plus the $2.50–$5-style range), and an **infographic** of the 5-share venue minimum. The share setting drives sizing everywhere — paper + live, next tick, no restart.

### What
- **`btc_5m_fv/execution/gate.py`**: new runtime knob `btc_runtime.trade_shares` (`set/get_runtime_trade_shares`, `runtime_trade_shares`, refreshed each tick). When set it takes precedence over the dollar `max_trade_usd` override: `effective_max_trade_usd` returns `trade_shares` (N shares cost ≤ ~$N since binary prices < 1, so the per-trade cap never blocks the bot's own N-share clip). DRYed the config reads behind `_read_positive`.
- **`btc_bot/paper.py`**: new pure `_share_sized_notional(side, notional, up_ask, down_ask, trade_shares)` — when a share target is set, `notional = trade_shares × the chosen side's ask`, so the loop sizes to ≈N shares (exact in paper; ≈N in live within rounding). The executor still auto-bumps to the venue minimum (#87). Unset → the dollar path is untouched (backward compatible).
- **`POST /api/runtime-config`**: new key `trade_shares` (validated 5 ≤ v ≤ 1000, audited).
- **CONTROLS panel** (`panels/controls.py`, wired in `ems.py`): shares input (`min=5`, live `≈ $` value computed in `dashboard.js:updateShareValue()` from the favoured side's ask), `$` range, share-minimum infographic (pips + label), updated hint. `setTradeShares()` POSTs the new key. STRATEGY sizing line shows `N shares (~$X)`.
- **Tests**: `test_share_sizing.py` (the resize helper), `test_risk_gate.py` (shares precedence + cap derivation), `test_runtime_config.py` (endpoint accept ≥5 / reject <5), `test_dashboard.py` (panel + handler). Full suite green (555); ruff clean; no new mypy errors.

### Why this shape
- The cap becomes a **target the venue minimum may exceed**, bounded by the derived dollar cap; the bankroll cap (when enabled) remains the dollar guard. The share count is the stable quantity the operator controls; the $ cost is shown as a derived, live estimate. The dollar `max_trade_usd` knob remains as a fallback when no share target is set (fully backward compatible).
- **Out of scope:** #83 (backtest leak) / #84 (signal overfit) — live *edge*, not sizing.

## v0.4.7 — Auto-bump sub-minimum orders to the venue share minimum (2026-06-17)

Closes #87; **supersedes the v0.4.6 floor**. v0.4.6 stopped the operator from setting a clip below ~$5, which removed legitimate control — Polymarket's real constraint is **5 shares/order**, which at the ≥0.50 favourites floor costs only **$2.50–$5** depending on price, not a flat $5. Operator wants to set any clip and still have small orders place. So instead of forbidding small caps, the bot now **rounds any sub-minimum order up to exactly the venue minimum** so it always places.

The "max trade size" cap becomes a **target, not a hard ceiling**: it may be exceeded only by what the venue minimum requires (e.g. a $3 clip at price 0.70 places 5 shares = ~$3.50), bounded so an abnormally large minimum can't overspend the bankroll.

### What
- **Reverted v0.4.6's floor** — `POST /api/runtime-config` accepts any `0 < v ≤ 1000` again; the gate no longer drops sub-floor overrides (`gate.py` back to `value if value > 0 else None`); the CONTROLS input `min` is `0.5` again and the hint explains auto-bump. Any positive clip is valid.
- **`btc_5m_fv/execution/live.py`**: in `submit_entry`, when `size < min_size` the order size is bumped UP to `min_size` (the book's `min_order_size`, default 5) and placed, instead of blocked. `record_buy_notional` uses the bumped size; the bump is logged (`live_executor.entry_bumped_to_min`). Guard: if `min_size > MAX_AUTO_BUMP_SHARES` (= 2 × `DEFAULT_MIN_ORDER_SIZE` = 10) the order is BLOCKED rather than overspend — a venue minimum that large is too expensive for this bankroll.
- **`btc_bot/paper.py`**: mirrors the bump (`shares = max(shares, DEFAULT_MIN_ORDER_SIZE)` before the top-of-book cap) so paper stays a faithful preview of live (#64).
- **Tests**: `test_live_executor.py` — the old `test_entry_blocked_below_min_order_size` becomes `..._bumps_to_minimum` (places 5 shares), plus a new too-large-to-bump guard test. Dropped the v0.4.6 floor tests. Full suite green (542); ruff clean; no new mypy errors.

### Why this shape
- The 5-share minimum is the venue's, and the smallest *placeable* favourite order ($2.50) is well below $5 — a flat floor over-restricted the operator. Bumping to the exact minimum gives full size control while never blocking on "too small". The cap-as-target trade-off (slight overspend only when the venue forces it) is bounded to ≤ `MAX_AUTO_BUMP_SHARES × price` (~$10 worst case); the bankroll cap (when enabled) remains the dollar-level guard.
- **Out of scope:** #83 (backtest leak) / #84 (signal overfit) — live *edge*, not placement.

## v0.4.6 — Enforce a min-trade floor on the runtime max-trade cap (2026-06-17)

Closes #85. Live trading was **100% blocked**: the dashboard BLOCKED panel showed every entry rejected — `size 1.78 shares below Polymarket minimum 5.00 at price 0.5600`. The operator had set the runtime **Max trade size to $1.00** from the dashboard (#50). At the favourites-only entry floor (price ≥ 0.50), $1.00 buys < 2 shares — below Polymarket's **5-share venue minimum** — so `LiveExecutor.submit_entry` (`execution/live.py`) correctly refused every order, every window. Funds were untouched; the bot was stopped.

Root cause: the #50 slice shipped **no floor** on the per-trade cap. Its CHANGELOG states the flawed assumption directly — *"a value below min gives a smaller fixed clip … no `min` changes needed."* But the cap is the **ceiling** of the confidence-sizing range, and `notional_from_confidence` clamps every order to `[min_trade, max_trade]`; a ceiling below `BTC_PAPER_MIN_TRADE_USD` ($5 in the operator's env) pins every order's notional to $1 → sub-minimum. Nothing enforced the floor: `POST /api/runtime-config` validated only `0 < v ≤ 1000`, the HTML input hardcoded `min='0.5'`, and the gate read accepted any `value > 0`.

### What
- **`btc_5m_fv/execution/gate.py`**: new `_runtime_override_or_none(value)` — a stored override that is non-positive **or below `BTC_PAPER_MIN_TRADE_USD`** is invalid and dropped, so the gate falls back to the (placeable) env default. Wired into both `RiskGate.refresh_runtime_limits()` (per-tick) and the module reader `get_runtime_max_trade_usd()` (dashboard display), so the live bot **auto-heals the stale $1.00 on the next tick** — no manual DB surgery, no migration. The floor is read from `config` at call time (no restart to change it).
- **`btc_5m_fv/ops/dashboard/app.py`**: `POST /api/runtime-config` now rejects `value < BTC_PAPER_MIN_TRADE_USD` with an actionable error ("must be at least the $5.00 min trade size — a smaller cap … blocks all entries") instead of silently accepting an unplaceable clip.
- **`btc_5m_fv/ops/dashboard/panels/controls.py`**: the number input's `min` attribute now tracks the displayed `min_trade` floor (was a hardcoded `0.5`), so the browser widget and the "min $X" hint agree.
- **Tests**: `test_runtime_config.py` (endpoint rejects sub-floor / accepts at floor); `test_risk_gate.py` (a stored $1 override is dropped → effective cap falls back to env default — the exact incident). Existing cap tests now pin `BTC_PAPER_MIN_TRADE_USD` explicitly, removing a latent dependency on the local `.env` (floor of 5 vs the CI default of 1). Full suite green (544, +3 new).

### Why this shape
- The invariant is **effective per-trade cap ≥ min-trade size** — the ceiling can't sit below the floor without inverting the sizing range. Enforced at every boundary (endpoint reject → operator feedback; gate read drop → heals legacy state; HTML `min` → honest widget), mirroring the existing "invalid override → None → env default" handling rather than adding new machinery.
- The gate drop is silent (no per-tick log spam) and the reader masks the stale value, so the dashboard already shows the corrected env-default cap. The stored $1 is inert until overwritten.
- **Out of scope:** #83 (backtest look-ahead leak) and #84 (entry-signal overfit) — those question whether the signal has live *edge*; this change only unblocks order *placement*.

## v0.4.5 — UI-settable max trade size (2026-06-16)

Part of #50 (the max-trade-size slice). Resizing the clip required editing `.env` and restarting uvicorn; with `BTC_PAPER_MIN_TRADE_USD=BTC_PAPER_MAX_TRADE_USD=5` every trade went in at a fixed $5 with no way to tune it mid-session. Now the operator sets it from the dashboard and it takes effect on the next tick — paper AND live, no restart.

### What
- **`btc_5m_fv/execution/gate.py`**: new runtime per-trade cap override. `RiskGate.refresh_runtime_limits()` re-reads `btc_runtime.max_trade_usd` from the `config` table every tick (runs in BOTH modes — it's a tuning knob, not the paper-only loss-halt bypass). New `runtime_max_trade_usd` (raw override) and `effective_max_trade_usd` (override else env default) properties; `block_reason` enforces the **effective** cap. Module helpers `set_runtime_max_trade_usd` / `get_runtime_max_trade_usd` with validation.
- **`btc_bot/paper.py`**: `paper_tick_once` calls `refresh_runtime_limits()` each tick; `_strategy_params()` uses the runtime override for the sizing ceiling when set (else env default — fully backward-compatible). So one knob governs both the sizing ceiling and the gate cap (unified). `notional_from_confidence` already clamps to `[min, max]`, so a value below min gives a smaller fixed clip and above min re-enables confidence-scaled sizing — no `min` changes needed.
- **Dashboard CONTROLS card** (`btc_5m_fv/ops/dashboard/panels/controls.py`, new; wired in `ems.py`, first grid row after RISK GUARDRAILS): shows current max (operator vs env default), a number input + Apply. `POST /api/runtime-config` (`app.py`) validates (0 < v ≤ $1000), persists via the gate setter, audits to `notification_feed`. `setMaxTradeSize()` in `dashboard.js`; `.ctl-input`/`.ctl-row` theme CSS. STRATEGY card's sizing line now reflects the effective cap.
- **Tests**: `test_risk_gate.py` (override applies in both modes, refresh fallback, clear, set/get round-trip, invalid-value handling); `test_runtime_config.py` (endpoint persists + validates against an isolated DB); `test_dashboard.py` (CONTROLS card + handler render). Full suite green (503 tests, +15 new).

### Why this shape
- Mirrors the existing per-tick `refresh_overrides` + `config`-table pattern (#65), so no new machinery and no restart. Unset key = exact prior behaviour (backward-compatible). The UI follows the existing panel architecture (pure `render()` panel, data in `ems.py`, POST + `refreshAll`, theme CSS) — no bespoke surface.
- Singleton position mode and multi-position were deliberately left untouched (deferred at operator request); `EntryRequest` / `GateConfig` / `LiveExecutor` single-position state are unchanged.

## v0.4.4 — Bankroll Cap Opt-In + RISK GUARDRAILS Panel (2026-06-15)

Closes #61. Investigation of "why did the bot stop trading after lunch?" surfaced a UX gap: the daily $30 bankroll cap had been silently rejecting every entry from 10:46 UTC onward (43 BLOCKED entries journaled in `btc_live_orders`), but nothing on the dashboard showed it. Operator had to grep logs and query SQLite to figure out the cap was hit.

### What
- **`config.py`**: new `_env_optional_float` helper; `BTC_LIVE_BANKROLL_CAP_USD` is now `Optional[float]` — blank / unset / ≤0 → `None` (gate disabled). Default behavior is now **no cap**. The per-trade cap and the daily loss halt are unchanged — both still mandatory.
- **`btc_5m_fv/execution/live.py`**: the bankroll-cap gate in `entry_block_reason` is skipped when `bankroll_cap_usd is None`. The persisted `daily_buy_notional` counter keeps incrementing on every matched fill regardless, so the dashboard can still show throughput when the cap is off.
- **RISK GUARDRAILS panel** (new wide card in `btc_5m_fv/ops/dashboard/ems.py`, first row of the EMS grid). Four columns:
  - **DAILY SPEND** — filled notional today, cap status (disabled or $X with headroom), submitted-entry count + total notional.
  - **LOSS HALT** — realized day P&L, halt threshold (–$10), headroom, OK/HALTED pill.
  - **BOT STATE** — state pill (RUNNING/STOPPED/OFF), uptime, last loop detail line (red when it contains error/fail/crash keywords — the NoneType crash that triggered today's investigation would have surfaced here), auto-pause status.
  - **BLOCKED (LAST 5 TODAY)** — newest-first tail of risk-gate rejections from `btc_live_orders` WHERE `status='BLOCKED'`, full reason on hover.
- Knock-on callsites (`btc_bot/controller.py:_default_detail`, `btc_5m_fv/ops/dashboard/app.py` `_brief_html`/`_settings_html`) render the cap as "disabled" when `None`.
- New unit test `test_bankroll_cap_none_does_not_block` confirms the gate bypasses arbitrarily large cumulative spend when the cap is unset.

### Why this, not a config flip
- The cap is "opt-in default off" rather than ripped out — same code path, just a new sentinel. If a future operator wants a budget guard back, they set the env var; no code change needed. Reversible.
- The guardrails panel makes the cap's status (and the loss halt's, the loop's last error, and any silent BLOCKED queue) **visible by default** — the original failure mode was lack of observability, not the cap itself.

### Out of scope (filed separately)
- The `TypeError: unsupported format string passed to NoneType.__format__` crash in the live loop tick at 11:24 UTC (surfaced today's investigation). Needs its own fix.
- Orphan live position 1222 from the morning's failed `exit_untracked` event — manual ledger reconcile required.

## v0.4.3 — Layer 1 Self-Improvement: Isotonic Calibration (2026-06-15)

Fixes #37 (new). Closes the prediction → outcome loop on the strategy's own raw probability without touching strategy logic.

### What
- **`btc_bot/calibration.py`**: pure-Python pool-adjacent-violators (PAV) isotonic regression, no sklearn/numpy dependency. `IsotonicCalibrator` (monotonic piecewise-constant map, JSON round-trip), `IdentityCalibrator` (no-op fallback so the bot is a no-op until a fit exists), `apply_to_pair` (calibrates fair_up and 1-fair_up, renormalises to sum=1 since exactly one outcome resolves).
- **`btc_bot/calibration_fit.py`**: CLI that pulls closed clob trades from the journal, derives `(model_p_side, side_won)` pairs from existing `edge`, `entry_price`, `realized_pnl_usd` columns (no schema change), fits, persists to `$DATA_DIR/calibration.json` atomically, prints Brier-before vs Brier-after and the fitted blocks.
- **`btc_bot/paper.py`**: applies the calibrator to `fair_up_raw` before computing side edges; preserves raw value on the snapshot as `fair_up_prob_raw` for diagnostics. Cached at module level with a `reload_calibrator()` helper for next-tick refresh after a refit.
- **Dashboard STRATEGY card**: new "Calibration" row showing kind / n_samples / Brier raw → calibrated (delta). Renders `identity (no fit yet)` until the first fit.
- **Tests**: 14 new unit tests (PAV correctness, monotonicity, identity, JSON round-trip, missing/corrupt/unknown-kind fallback). 442 green.

### Fit on live DB (n=844 closed clob trades)
- Brier **0.275 → 0.242 (+0.033 absolute, ~12% relative)**.
- Calibration curve makes the bias plain: model's claimed 95% confidence trades win ~59%, claimed ~68% win ~54%. The bot has been paying that overconfidence at every entry. Renormalised pairs compress: raw fair_up=0.93 maps to calibrated (0.62, 0.38).

### Why this, not Hugging Face (yet)
- HF time-series foundation models (Chronos, TimesFM, Lag-Llama) and FinBERT are interesting but speculative for a 5-minute BTC binary; the calibration layer is the no-regret first step that the existing journal already supports. Layer 2 (param auto-tune) and Layer 3 (model ensembles) are deferred to later issues.

### Out of scope (next)
- Daily/scheduled refit (run `python -m btc_bot.calibration_fit` manually for now).
- Layer 2: rolling-window auto-tune of `entry_edge_min`, sigma floor, sizing curve via the existing backtest harness.
- Layer 3: optional HF time-series model as a second probability source, A/B'd via the replay harness.

## v0.4.2 — Bloomberg-EMS Theme + Mode Selector + Single-Process Lock (2026-06-15)

Refs #37, #36.

- **Bloomberg-EMS retheme**: amber accent (the trading-terminal signature) on dark slate, amber category-header bars, dense monospace grid; convention colors (green/red/amber/blue). Replaces the teal "modern fintech" palette per EMS design references.
- **Paper/Live mode selector** in the topbar: a runtime toggle (`/api/mode`, `controller.set_mode`) that switches mode via the config table and restarts the loop cleanly (stop-before-start = single loop). Live is gated exactly like boot (`assert_live_boot_allowed`); the LIVE option is disabled with the reason when the gate isn't met, and a confirm dialog precedes any switch to real.
- **Single-process lock** (`main.py`, advisory flock on `data/bot.lock`): a second instance fails fast. This is the root-cause fix for #36 — multiple overlapping processes each ran a loop with independent live-executor state, which is why "live" entries silently took the paper path. `run_paper_loop` now reads the runtime-selected mode.
- 428 tests green; retheme + selector verified in-browser.

## v0.4.1 — EMS-Style Dashboard (2026-06-15)

Fixes #37. Rebuilt the dashboard as an execution-management terminal.

- New `btc_5m_fv/ops/dashboard/ems.py`: status ribbon (mode/run pills, session equity, day P&L, open risk, daily-halt headroom, feed chips, uptime, auto-pause/kill state); STRATEGY panel (model, edge band, entry floor, sizing, settlement rule, auto-pause); LIVE MARKET (fair-Up gauge, Up/Down book, spot/ref/basis/edge, decision); PERFORMANCE/ALPHA (inline SVG equity curve, net P&L, ROI, win rate, expectancy, profit factor, max DD); TCA (quoted spread, taker half-spread, signaled-vs-realized edge capture, Brier, SVG calibration); TRADE BLOTTER. Inline SVG charts — no JS charting dependency.
- Performance/TCA/blotter use a **recent rolling window** (current regime), not the lifetime blend that mixed in older experimental configs; the ribbon stays session/day-scoped. Honest framing, labeled "recent N".
- Dark trading-terminal CSS (tabular monospace numbers, P&L color coding, dense panels); reuses the SSE pipeline (`ems` added to `/api/data` + `/api/stream`; JS swaps `#ems-content`).
- Fixed a latent `float(None)` crash in `load_paper_summary` when a tick has no up-ask (now common in live).
- Dashboard tests rewritten to the EMS contract. 428 green; verified visually in-browser.

## v0.4.0 — Adaptive Risk Controller + AI Research-Loop Design (2026-06-14)

Fixes #36. The "self-improving" layer, done rigorously — adaptive risk control over our own journal, not price prediction.

### Adaptive risk controller (`btc_bot/adaptive.py`)
- Rolling expectancy / win-rate / **Brier calibration** over the last N closed clob trades of the active style. Model probability reconstructed as `edge + entry_price`; outcome = `realized_pnl > 0`. No schema change.
- **Auto-pause**: blocks NEW entries when rolling ROI drops below a floor after a minimum sample — STICKY until an operator clears it (`tools/clear_auto_pause.py`). Catches EDGE DECAY before losses pile up; complements (does not replace) the hard −$10/day halt. Notifies on trip; existing positions still settle.
- Config: `BTC_AUTO_PAUSE_ENABLED/WINDOW/MIN_TRADES/MIN_ROI`. 8 tests (metric math, calibration, style/quote/state filtering, sticky no-auto-resume, warm-up, disabled).

### AI research loop (designed, `docs/RESEARCH_LOOP.md`)
- Nightly agent mines the journal for loss clusters → proposes filters → backtests walk-forward OOS on the recorded archive → surfaces only survivors with numbers for operator approval. AI proposes, human disposes; never auto-applies to live. Built once ≥~50 live fills accumulate.

### Won't do
- RL auto-tuning on a live $30–40 bankroll (overfits to noise). No live-param change without OOS validation + operator sign-off.

443 tests green.

## v0.3.7 — Existing-Wallet (MetaMask) Onboarding + Auto-Detect (2026-06-12)

Fixes #34. For users who already hold funds in a connected-wallet Polymarket account, added a no-fund-movement path: trade the existing balance in place using the wallet's signer key.

- `tools/live_detect_wallet.py`: given the signer key in `.env`, derives every wallet it could control (EOA / POLY_PROXY / Gnosis Safe via the SDK's `derive_proxy_wallet_address` / `derive_safe_wallet_address`), reads each candidate's on-chain pUSD balance on Polygon (multi-RPC fallback: publicnode / 1rpc / drpc), and writes the funded one's address + matching signature type into `.env`. Deterministic — the funded address's derivation *is* its signature type (EOA=0, POLY_PROXY=1, GNOSIS_SAFE=2); no guessing.
- Runbook documents Path A (existing connected wallet, auto-detected) vs Path B (fresh isolated deposit wallet), with the key-blast-radius tradeoff stated plainly.
- pUSD collateral token address (`0xC011a7E1…`, Polygon) sourced from the SDK's PRODUCTION environment config.

## v0.3.6 — Verified Self-Serve Deposit-Wallet Onboarding (2026-06-12)

Fixes #33. Hardened and verified `tools/live_setup.py` end-to-end against the live Polymarket API:

- **Key never leaks**: the generated private key is written straight into `.env` (perms `0600`, `.env.bak` saved) and is never printed to stdout — so it cannot land in terminal scrollback or an assistant transcript. Only the public signer/deposit addresses are shown. `_merge_env` updates keys in place, preserves comments/other keys, and is unit-tested to NEVER auto-write `BTC_LIVE_CONFIRM` (the operator's conscious go-live step).
- **Correct deposit-wallet flow** (verified live): mint a Builder API Key from the signer key (L1→L2→builder, self-serve, used only for the gasless deploy then discarded) → `SecureClient.create(api_key=...)` deploys the deterministic type-3 deposit wallet gaslessly. The earlier `setup_trading_approvals()` call was the EOA method and hit a relayer allowlist rejection; removed. Collateral allowance is set by `update_balance_allowance` on first funded connect (executor + preflight already call it) — no separate approval step.
- `polymarket-client` declared as the optional `setup` extra (one-time onboarding only; runtime trading uses `py-clob-client-v2`).

## v0.3.5 — Real Live-Setup Flow (2026-06-12)

Fixes #32. The runbook's "export private key from Polymarket settings" step does not exist in the product (operator-verified; absent from current docs). Setup rewritten to the documented reality — you bring a wallet you control:

- `tools/live_setup.py` — one-time onboarding via official py-sdk (optional install): generates a fresh key if needed, deploys the deterministic deposit wallet (signature type 3), runs idempotent gasless trading approvals, prints the `.env` block and funding instructions. Existing UI funds move by **withdrawing to the funder address** — no export anywhere.
- `tools/live_preflight.py` — read-only GO/NO-GO: boot gate, credential derivation, CLOB reachability, funder balance/allowance as the CLOB sees it.
- `LiveExecutor.start()` now refreshes the CLOB balance/allowance cache (`update_balance_allowance`, best-effort) — the documented pre-first-order step.
- Runbook "Going Live" rewritten (wallet decision table, scripted path, preflight gate); `.env.example` signature-type guidance corrected (type 3 recommended).

## v0.3.4 — Migrate to py-clob-client-v2 (2026-06-12)

Fixes #31 (launch blocker). Upstream archived `py-clob-client` (v1) with "no longer functional — should not be used"; live mode would have failed at first auth. Migrated `LiveExecutor` to official `py-clob-client-v2` (1.0.1):

- `create_or_derive_api_creds()` → `create_or_derive_api_key()`; `cancel(order_id)` → `cancel_order(OrderPayload(orderID=...))`; import paths updated. Constructor surface (incl. `signature_type`/`funder` for proxy wallets), `set_api_creds`, `get_order`, `cancel_all`, order/cancel response bodies, and `OrderBookSummary` fields (`min_order_size`, `tick_size`, worst→best level ordering) are unchanged — verified against installed v2 source.
- v1 removed from the venv and dependency declarations; suite green with zero v1 references; v2 smoke-tested against the live CLOB API (`get_ok`, `get_server_time`).

## v0.3.3 — Anti-Adverse-Selection Entry Filters (2026-06-12)

Fixes #29. The 26h settle soak (n=225, -14.4% ROI overall) revealed structured losses: PnL by claimed edge decreases monotonically (4.5–7%: +7.0% ROI; >15%: -36% to -57%), and entries below 50¢ lose badly while favorites win 63–72%. Large apparent edge = the model lagging a fast market (adverse selection), not opportunity.

- `BTC_PAPER_ENTRY_EDGE_MAX` (default **0.07**): entries whose claimed edge exceeds the cap are rejected — "skip: edge above cap (stale-model guard)"
- `BTC_PAPER_MIN_ENTRY_PRICE` default raised 0.05 → **0.50**: favorites only
- In-sample, the joint surviving slice (edge 4.5–7%, entry ≥ 50¢) ran **+22.8% ROI, 73% win, n=48**, positive in both sample halves. This is post-hoc structure: the filtered strategy must hold in a fresh out-of-sample soak before the live gate opens.

## v0.3.2 — Settle-Style Strategy Profile (2026-06-11)

Fixes #28, closes #27. First honest-baseline soak (135 trades / 70 min) showed the legacy scalp shape is structurally negative under real fills: -$7.87, median hold 8s, up to 17 entries per window, 65 STOPs (-$55) vs 48 TARGETs (+$44) — it pays the spread every few ticks and stops out on noise. The +31% April backtest used the opposite shape.

- New `BTC_EXIT_STYLE`: **`settle` (default)** — max one entry per window (kills churn by construction), no TARGET/STOP/BAND/TIME exits; positions ride to resolution and close at the Chainlink-settled 1.00/0.00. `scalp` keeps the legacy behavior for experiments.
- Live mode: `LiveExecutor.record_settlement(won, window_slug)` registers the resolution outcome (PnL into the persisted daily-loss halt, journal `SETTLEMENT` row, slot freed) without placing an exit order; any resting entry remainder is cancelled. Winning tokens await operator redemption — runbook section added.
- Positions journal `strategy_style`; KPIs aggregate only the active style (third baseline reset; prior rows remain as audit trail).
- 7 new tests (style gating, one-entry-per-window, settlement win/loss accounting, no-exit-order bypass).

## v0.3.1 — Data Integrity: CLOB Quotes + Settlement Feed (2026-06-11)

Fixes #21, #22, #23. The signal path now prices, fills, and settles against the same data the market actually uses.

### Executable quotes (#22)
- Signal edge is computed against the CLOB best ask per outcome token (`signal_from_executable_edges`); Gamma `outcomePrices` are journaled (`gamma_up_price`) only to quantify their staleness, never used for pricing
- Honest paper fills: BUY at best ask, SELL at best bid, capped by top-of-book size; empty/crossed books skip with a journaled reason
- Window-rolled paper positions settle at the actual Chainlink resolution (1.0/0.0 via the settlement endpoint) instead of pricing the old position off the new window's book
- Tick journal gains top-of-book columns (both sides) + `quote_source`; performance KPIs exclude pre-fix rows (re-baseline — old rows stay as audit trail)

### Settlement-aligned Chainlink feed (#21)
- New `ChainlinkSettlementConnector` (REST): window reference open with provisional-revision stabilization, fast settlement via `open(N+1) == close(N)`, cache-busting, full Chrome-fingerprint WAF headers
- New `ChainlinkWsFeed` (WS): live 1s prints from `ws-live-data.polymarket.com` (byte-exact compact subscribe filters, literal-PING keepalive, reconnect with backoff, 429-aware), feeding spot + sigma
- REST spot poll fallback: `openPrice` of a window starting seconds ago IS the near-live settlement print — the engine survives WS outages/rate limits without mixing sources
- Binance demoted to volatility-shape fallback and backtest tooling — its LEVELS never touch the model (measured Chainlink−Binance basis ≈ −$50.7, std $3.8)
- Tie rule: fair value now includes the discrete tie mass P(close == open), which resolves Up — `fair_up > 0.5` when price pins the reference
- A degraded settlement feed blocks new entries and suppresses fair-value-based exits (BAND_REENTRY); time/target/stop exits still run

### Dashboard state (#23)
- Controller state derives from the actual runner thread in both directions: never STOPPED while ticking, never RUNNING while dead

## v0.3.0 — Live Execution Mode (2026-06-10)

Closes #20. Adds an opt-in live trading mode that routes the existing signal path through Polymarket's CLOB via `py-clob-client`. **Paper remains the default**; nothing changes unless the operator explicitly flips every gate.

### Live executor (`btc_5m_fv/execution/live.py`)
- `LiveExecutor` wraps the synchronous `ClobClient` behind an async API (all network calls via `asyncio.to_thread`)
- `start()` derives + sets CLOB API creds (`create_or_derive_api_creds` / `set_api_creds`) and verifies reachability before any trading
- Entries: GTC limit BUY at best ask (book best ask, gamma price fallback); price rounded to the market tick size, size rounded **down** to the CLOB 2-decimal share granularity, Polymarket minimum order size enforced from the order book (default 5 shares)
- Exits: GTC limit SELL at best bid for the **matched** entry size (`get_order` → `size_matched`), so the bot never sells shares that never filled; any resting entry remainder is cancelled before the sell
- Cancel-on-roll: unfilled entry orders are cancelled on `WINDOW_ROLL` and `BAND_REENTRY` exits (matched size is captured post-cancel so fills landing mid-cancel still get flattened)

### Hard risk limits (enforced in code BEFORE every order)
- Per-trade cap: `BTC_LIVE_MAX_TRADE_USD` (default $3)
- Max 1 open live position/order
- Daily realized-loss halt: `BTC_LIVE_DAILY_LOSS_HALT_USD` (default $10, UTC day) — **persisted in SQLite and reloaded at boot**, so Stop/Start or a restart cannot reset it within the day
- Daily bankroll cap on summed buy notionals: `BTC_LIVE_BANKROLL_CAP_USD` (default $30/UTC day, persisted); the unfilled remainder of a cancelled entry is credited back
- Entry slippage guard: `BTC_LIVE_MAX_ENTRY_SLIPPAGE` (default 0.02) blocks buys when the live ask has gapped above the signal price that produced the edge
- Kill switch: the file `data/KILL` blocks all NEW entries and cancels resting orders, checked every tick plus immediately before each entry POST (TOCTOU guard); it re-arms when the file is deleted. Exits stay allowed under kill — flattening only reduces exposure
- Realized PnL feeds the loss halt from CONFIRMED exit fills (actual matched size at the executed order's limit price), never from paper-price estimates at submission time

### Exit lifecycle & stop safety
- Exit SELLs never rest: the order is awaited up to `BTC_LIVE_EXIT_FILL_TIMEOUT_SECONDS` (default 10s) and cancelled if unfilled, so no stale GTC exit can sit in a 5-minute book into resolution; partial fills are accounted per tranche and only the remainder is retried
- A failed/blocked/unfilled live exit **keeps the ledger row OPEN** and is retried on the next tick — the ledger can never claim flat while real tokens remain on the exchange
- Cancels are verified against the DELETE response body (`canceled` list) with a terminal-status re-check; on cancel failure the order id stays tracked for retry instead of being forgotten
- Live entries write the ledger row BEFORE the order is submitted (failed submits delete it), so a DB failure after submit can never leave a real position unmanaged
- Stop: the controller sets the stop flag and **waits for the runner thread** to cancel resting orders and flatten through the executor on its own event loop — single-threaded executor ownership, no stop race, no paper-closing of live positions; unflattenable rows are reported for manual action

### Boot gating & reconciliation
- Live mode REFUSES to start unless `POLYMARKET_PRIVATE_KEY` is set AND `BTC_LIVE_CONFIRM=YES_I_UNDERSTAND` — checked on the dashboard `/api/start` path and again at loop start; it never silently falls back to paper
- Boot also REFUSES when: `POLYMARKET_FUNDER` is empty with signature type 1/2 (such orders are signed with the EOA as maker and rejected by the CLOB), the signature type is unknown, or any risk-limit env var failed to parse (no silent fallback to looser defaults)
- Boot reconciliation: `start()` cancels ALL resting CLOB orders on the account and re-adopts any open ledger position from the order journal (exchange-confirmed fill size); paper artifacts / never-filled rows are closed as `RECONCILED_*`; unreconcilable state refuses boot instead of trading on top of unknown exposure
- Wallet config: `POLYMARKET_FUNDER` (proxy wallet), `POLYMARKET_SIGNATURE_TYPE` (0 EOA / 1 email / 2 browser, default 1)
- The private key is never logged and never journaled

### Audit trail
- New SQLite table `btc_live_orders` journals every order/cancel attempt — including risk-gate BLOCKED attempts that never reach the network
- Engine ledger (`btc_paper_positions`) mirrors live fills (executor price/size), notifications use `btc_live_entry` / `btc_live_exit` events

### Dashboard & docs
- Status/brief/settings copy is mode-aware: "LIVE — orders are real" vs paper; stale "no live orders are placed by this build" claims removed
- `.env.example` documents all new vars (key/funder ship empty); `docs/OPERATIONS_RUNBOOK.md` gains a "Going live" section with launch steps, risk limits, and kill-switch drill

### Tests
- 70 new unit tests with a fully mocked `ClobClient` (boot refusal incl. funder/signature/parse-error gates, order construction/rounding/min-size, slippage guard, all risk gates incl. restart persistence, kill switch incl. re-arm and TOCTOU, exit timeout/partial-fill lifecycle, boot reconciliation, failed-exit-keeps-row-open wiring, paper-default invariance, dashboard copy)
- `py-clob-client` pinned in `requirements.txt` and `pyproject.toml`
- **395 total, all passing**

## v0.2.2 — Deterministic Test Fills (2026-06-10)

Fixes #24.

- **Flaky paper-execution tests** — `PaperExecutionManager._determine_fill()` rolled module-level unseeded `random.random()` per order (1% PARTIAL_FILL, 0.1% REJECTED), so FILLED-assuming unit tests failed in ~30-40% of full-suite runs. The RNG is now injectable via a new `rng: random.Random | None` constructor param (default unchanged: fresh unseeded `random.Random()`); test fixtures pin a deterministic always-fill RNG. Partial-fill/reject paths keep their dedicated tests. Verified 20/20 consecutive green runs of `tests/unit/test_paper_execution.py`.

## v0.2.1 — Runtime Blockers (2026-06-10)

Fixes #19.

- **main.py boot crash** — imported `DASHBOARD_PORT`, renamed to `DASHBOARD_SERVER_PORT` in the v0.2 dashboard migration. FastAPI entrypoint never started.
- **Binance endpoint unreachable** — `api.binance.com` was hardcoded in 4 modules and times out on some networks. New `BINANCE_API_BASE` env var (default `https://data-api.binance.vision`, Binance's public market-data mirror with identical `/api/v3` routes) threaded through `btc_bot/paper.py`, `btc_bot/backtest.py`, `btc_5m_fv/backtest/conditional.py`, and `BinanceConnector`.
- **Backtest resilience** — kline fetches in both backtest modules now retry 3× with backoff; a single transient SSL timeout no longer kills a 2,688-combination grid run.

## v0.2.0 — Full System Rebuild (2026-05-28)

A complete architectural rebuild from monolithic demo to modular trading system. Every open GitHub issue has been addressed.

### Architecture
- **New package structure** (`btc_5m_fv/`) with 7 sub-packages: `core`, `strategy`, `connectors`, `storage`, `backtest`, `execution`, `ops`
- **Interface-driven design** — all components implement ABCs from `core.interfaces`
- **`pyproject.toml`** replaces `requirements.txt` with modern Python packaging
- **GitHub Actions CI** — tests on Python 3.11/3.12, lint with ruff, type-check with mypy

### Core (closes #10, #15)
- `core/types.py` — 16 frozen dataclasses (MarketWindow, Signal, Tick, PaperOrder, PaperPosition, etc.)
- `core/interfaces.py` — 5 abstract base classes (market connector, price connector, signal generator, execution manager, risk service)
- `core/exceptions.py` — 5 custom exceptions with hierarchy

### Strategy
- Extracted from `btc_bot/strategy.py` into 3 focused modules:
  - `strategy/fair_value.py` — `sigma_per_second()`, `fair_up_probability()`
  - `strategy/sizing.py` — `confidence_from_edge()`, `notional_from_confidence()`
  - `strategy/signal.py` — `signal_from_edge()` with `SignalAction` enum

### Connectors (closes #11, #12, #16, #9)
- `connectors/polymarket.py` — market discovery with slug pattern matching
- `connectors/binance.py` — spot price, reference price, recent closes with rate limiting awareness
- `connectors/chainlink.py` — stub for Chainlink Data Streams integration (#9)
- `connectors/registry.py` — registration, health checks, history tracking
- All connectors implement `AbstractPriceConnector` / `AbstractMarketConnector`

### Storage & Backtest (closes #2, #3, #4)
- `storage/recorder.py` — `MarketDataRecorder` persists windows, ticks, CLOB snapshots to SQLite
- `storage/replay.py` — `DeterministicReplay` feeds recorded data through signal generator
- `backtest/harness.py` — `FullMarketBacktestHarness` runs strategy on ALL recorded windows
- `backtest/metrics.py` — `BacktestResult` with exit attribution, `FrictionModel` for realistic simulation
- `backtest/conditional.py` — original trade-history conditional backtest preserved

### Execution & Risk (closes #13, #14, #5)
- `execution/paper.py` — `PaperExecutionManager` with explicit order lifecycle: PENDING -> ACKNOWLEDGED -> FILLED
- `execution/risk.py` — `RiskService` with pre-trade checks, drawdown monitoring, win/loss tracking
- `ops/controller.py` — `BotController` unified tick loop

### Operations (closes #6, #7, #8)
- `ops/telemetry.py` — `FeedHealthTracker` (p50/p95/p99 latency) and `LatencyTracker`
- `ops/incidents.py` — `IncidentManager` state machine + `RunbookActions` for every incident type
- `tests/conftest.py` — deterministic fixtures for reproducible tests
- 18 test files, 321 total tests

### Dashboard Migration
- Replaced Gradio (150MB+ dependency) with **FastAPI + Jinja2**
- `ops/dashboard/app.py` — FastAPI with routes, SSE for real-time updates
- `ops/dashboard/templates/` — Jinja2 templates (5 tabs)
- `ops/dashboard/static/` — CSS and vanilla JS
- Server-Sent Events replace `gr.Timer` polling
- Visual design preserved exactly

### Tests
- 288 unit tests (network-free, deterministic)
- 11 integration tests
- 23 e2e tests
- 4 preserved smoke tests
- **321 total, all passing**

---

## v0.1.0 — Initial Demo

Original monolithic implementation:
- Gradio dashboard with custom CSS
- Paper trading loop in `btc_bot/paper.py`
- Strategy math in `btc_bot/strategy.py`
- SQLite persistence in `db.py`
- 4 smoke tests
- 7 commits, 14 Python files
