# Closed build history (issues #20-#144)

Historical, fully-closed development log moved out of `tasks/todo.md` to keep
that file focused on current/open work. Nothing here is current status.

---

# Tick-replay backtest + v8 (#144) (2026-07-02)

Full autonomy granted ("your call and game"). Chose the move that collapses the waiting time:
the tick journal reaches back to Jun 11 — six days BEFORE the shadow race — so v7's gates
(designed on race data) were testable out-of-sample immediately.

- [x] `tools/replay_race.py`: replay 74,580 ticks / 1,626 windows, fee-true, first-signal-per-window
- [x] Outcome labeler validated 564/564 (next-window reference print); harness reproduces recorded shadow v2 249/249
- [x] v7 OOS CONFIRMED: +0.346/trade [+0.005, +0.687] pre-race vs +0.341 in-sample — stable across regimes
- [x] Fragility grid → freshness gate is the engine; depth realism checked (99.4% ≥5 shares)
- [x] Pre-registered `fair_value_fresh_v8` (freshness alone); roster = clean ablation v0/v2/v7/v8
- [x] PR #145 → develop (728 tests green); engine restarted on the 4-model roster

## Review
The replay converts "wait 6 weeks" into "confirm in ~2–3": v7/v8 enter the live race with a
quantified prior instead of a hunch. Deploy bar unchanged and non-negotiable: live capital
only when the LIVE race CI clears zero net of fees. If the race contradicts the replay,
the race wins — that disagreement itself would be the most important finding.

# Roster surgery + race restart (#142) (2026-07-02)

Operator escalated: "create new or better strategies, bin the ones that suck, become profitable."

- [x] Design recon: pair-arb REJECTED (2% tick persistence = stale books); EV-regression gate REJECTED (unidentifiable); entry-timing structure FOUND (all family profit in first 60s); edge-cap supported (+4.8¢ vs −1.2¢/share around 0.065)
- [x] Bin v3/v4/v5/v6 (evidence in #142); roster = v0 control / v2 champion / v7 challenger
- [x] Build `cushion_fresh_v7` = v2 + ≤60s freshness + 0.065 edge-cap (TDD; gates frozen a-priori)
- [x] Retired-active-model heal (`_resolve_active_model`, loud one-time notify) — v6 was still persisted
- [x] PR #143 → develop (720 tests green, ruff clean)
- [x] Race RESTARTED in paper mode 2026-07-02; 1768 settled organically +$2.49; fallback notification confirmed

## Review
The profitable path is procedural, not magical: fee-true books (#133), a 3-model race with
one evidence-motivated challenger, and the pre-registered deploy bar (95% CI > 0 net of
fees, sign-consistent OOS). v7's two gates are the only levers the frozen data supports;
everything else tested null or artifactual. Live stays OFF until a model clears the bar.

# Postmortem + EMS repair (#132–#138) (2026-07-02)

Operator declared the project failed; full forensic pass over live/shadow/paper history.

- [x] Forensics: venue-true PnL −$17.24 (gross +$6.27, fees −$23.51); books were fee-blind
- [x] Shadow race final read: no model ≠ 0; IS→OOS rank inversion; night gate + selectivity dead
- [x] #132 boot-reconcile heal (the 06-25 outage) — fixed, TDD, PR #139 → develop
- [x] #133 fee-true booking (journal = ledger = halt) — fixed, TDD, PR #140 → develop
- [x] #134 ledger reconciled: −$3.10 → −$17.77, 4 phantoms voided; 1768 heals on next boot
- [x] #135 docs/POSTMORTEM_2026-07.md + pre-registered restart protocol
- [x] Backlog as issues: #136 paper fee parity, #137 maker/taker telemetry, #138 stop watchdog

## Review
The failure was three-layered: (1) a fee-dominated market where the signal's gross edge
(+0.7% of turnover) is under the taker fee (2.6%); (2) fee-blind books that hid the true
bleed and let the loss-halt fire late; (3) an EMS that died on a heal-able boot state and
stayed dead. Layers 2–3 are fixed on develop. Layer 1 has no validated fix: the only
evidence-sane path is the shadow-only restart protocol in the postmortem (deploy bar:
95% CI > 0 net of fees). OPERATOR ACTIONS: reset active model off `down_skeptic_drift_v6`;
one Start to heal row 1768; decide shadow-restart vs retire.

# Regime-attribution instrument (#120) (2026-06-23)

Operator pushed back on my stance re: regime ID + auto strategy selection. Decision,
owned: **do NOT build a live auto-selector** (overfitting trap on a null surface); build
the **measuring instrument** that decides the question rigorously when the sample is powered.

## Plan
- [x] Recon (4 parallel agents): shadow pipeline/schema, attribution record, power audit, stats primitives.
- [x] `tools/regime_attribution.py` — read-only, never in live path. A-priori bands; attribute by side-BET; two-sided-edge gate; permutation test + Benjamini-Hochberg FDR; power gate.
- [x] TDD: `tests/unit/test_regime_attribution.py` (29 tests, incl. positive controls proving it CAN find a constructed two-sided edge).
- [x] Verify on real ledger (read-only, both axes).
- [x] Adversarial stats review (3 lenses) — fixed all 3 confirmed: (1) omnibus→per-(model,regime) one-vs-rest, testable-only + FDR across model×regime; (2) side-residualize PnL (side-mix≠regime); (3) UTC banding. All false-positive-direction; null verdict unchanged.
- [x] Commit + PR to develop → PR #121 (commit 51d24d8).
- [ ] Follow-up issues to file: schema migration to log spot/reference/sigma/drift on shadow rows (unlocks vol/basis axes); dashboard panel.

## REVIEW (2026-06-23)
- **Recon ground truth:** shadow ledger `btc_model_shadow_positions` = clean fee-netted counterfactual (8 models, not 6); side-BET recoverable; only time-of-day + edge axes available without schema change; ~5.7 days, only `fair_value_v0` individually powered → instrument ships **dormant**, lights up as sample grows.
- **Real-data verdict (both axes):** *"no regime clears the bar — no significant two-sided regime edge."* Every model permutation p∈[0.11,1.0], nothing survives FDR, zero regimes pass the two-sided gate. Confirms: eye-catching cells are one-sided tilt or high-win/negative-expectancy (late_convergence_v3 90%+ win, NEGATIVE exp — the "91%-win loses money" lesson, made visible).
- **Verification:** 696 unit tests green; ruff clean; 29 new tests incl. positive controls.

---

# PnL accuracy + open-position widget + trailing loss-halt (2026-06-23)

Two operator asks from this session, investigated via two parallel agent workflows.

## REVIEW (2026-06-23) — SHIPPED on `feature/112-113-pnl-accuracy-trailing-halt`
Operator said "go" with the recommended defaults (D1 manual + freshness badge, D2 mid mark, D3 "—" for non-current windows). Implemented TDD, DB-isolated.
- **#112** trailing HWM loss halt — `gate.py` + `ems.py` + `guardrails.py` (commit af622e9).
- **#115** reset clears peaks too — `gate.py` + `app.py` (commit 48710d6; a real gap: floor=peak−limit stays latched if only PnL is reset).
- **#113** PnL accuracy + open-position widget — `reconcile_live_ledger.py` slug filter, `performance.py` relabel + freshness badge, `market.py`/`_shared.side_mid`/`blotter.py` live unrealized (commit 3df6b3d).
- **#114 [P2]** filed (conditionId/token_id per-window deconfliction) — not built.
- **Verification:** 686 tests green; ruff clean; zero new mypy on changed files; end-to-end render confirmed on the live DB; adversarial review run before PR.

Original plan (kept for reference) below.

---

## ITEM A — Trailing high-water-mark loss halt (READY — no decisions)

### Ask (verbatim)
"every profit should reset the halt headroom… if i made $2 and then i lose $2 the halt
headroom should go down this session $2, but if i make $2 again it should go back to $10.
basically we have to prevent losing $10 each session — not, i made $30 and now the headroom
is technically 30+10."

### Semantics (confirmed against the operator's worked example)
Convert the halt from a **fixed cumulative floor** (halt when session PnL ≤ −$10) to a
**trailing drawdown from the session high-water mark**:
```
peak  = max(peak, session_pnl)          # ratchets up only; starts at 0 each UTC day
floor = peak − LIMIT                     # LIMIT stays $10 (BTC_TRADE_DAILY_LOSS_HALT_USD)
halted = session_pnl ≤ floor            # (unless operator bypass)
headroom = session_pnl − floor          # = LIMIT − (peak − session_pnl)
```
Checkpoints (LIMIT=10): start→10; +2→10; then −2→8; then +2→10; at +30 peak, halt at +20.
**Fail-safe property:** for a never-profitable session peak stays 0 ⇒ behaviour is IDENTICAL
to today's −$10 floor. The change can only halt *earlier* (after locking gains), never later.

### Current mechanism (verified)
- Threshold: `BTC_TRADE_DAILY_LOSS_HALT_USD=10.0` — `config.py:172`.
- Enforcement (TWO places): hard loop-stop `gate.loss_halt_breached()` in `btc_bot/paper.py:404`;
  pre-entry block `block_reason()` in `gate.py:346`. Both call `loss_halt_breached()` → one fix point.
- Decision: `loss_halt_breached()` `gate.py:227` → `halt_pnl <= -cfg.daily_loss_halt_usd`.
- Leg: `halt_pnl` `gate.py:220` → live leg in live mode, paper leg in paper mode (#76). Realized-only.
- Counters: `_live_pnl`/`_paper_pnl`, persisted `btc_risk.{live,paper}_realized_pnl`; reset at UTC
  midnight `_roll_daily_window()` `gate.py:159`; fed by `record_realized_pnl()` `gate.py:274`.
- Display: panel RE-COMPUTES halt independently — `guardrails.py:68-71`
  (`halted = halt_pnl <= -loss_halt_usd`; `headroom = loss_halt_usd + min(0, halt_pnl)`).
  Dashboard reads config keys directly via `ems.py:45-51,146` (no gate object) → peak must be a config key.

### Implementation (TDD, DB-isolated per `tests-hit-live-db` memory)
1. **`gate.py`** — add `_live_peak`/`_paper_peak` (start 0.0); persist `btc_risk.live_peak_pnl`/
   `btc_risk.paper_peak_pnl`; reset peaks in `_roll_daily_window()`; in `record_realized_pnl()` set
   `peak = max(peak, leg_pnl)` after the add; add `halt_peak` property (mirror `halt_pnl`);
   change `loss_halt_breached()` → `halt_pnl <= halt_peak - cfg.daily_loss_halt_usd`. Backward-compat
   load: when peak key absent, init `peak = max(0.0, leg_pnl)`. Update `block_reason()` message to cite floor.
2. **`ems.py`** — read the two new peak config keys; pass `live_peak`/`paper_peak` to `guardrails.render`.
3. **`guardrails.py`** — accept `live_peak`/`paper_peak`; compute `floor = peak - loss_halt_usd`,
   `headroom = halt_pnl - floor`, `halted = halt_pnl <= floor`; show **Peak P&L**, **Floor**, **Headroom**.
4. **Tests** — `tests/unit/test_risk_gate.py`: the operator's exact checkpoint sequence; never-positive
   session ≡ old behaviour; peak persistence across `load()`; UTC rollover zeros peak; bypass still wins.
5. Full suite green (DB-isolated); ruff/mypy clean on changed files; CHANGELOG; adversarial review of the
   breach math + migration before commit. Push feature branch → PR to **develop** (never main).

---

## ITEM B — PnL/performance accuracy + open-position widget (3 DECISIONS PENDING)

### Root cause (investigation result — operator's intuition was half-right)
- **Panels are already BTC-only.** `btc_paper_positions` can only hold `btc-updown-5m-*` markets
  (hardcoded discovery `connectors/polymarket.py:60`, `paper.py:1029`). Empirically 236 live + 1,349 paper
  rows, all "Bitcoin Up or Down"; the 5 non-bot trades (Wembanyama/Wimbledon/Hormuz/etc.) never enter the DB.
  → No contamination of the headline metrics.
- **What the operator actually reacted to:** the "Reconciled vs Polymarket" footer `performance.py:42`
  prints `account $X` (whole Polymarket account, incl. non-bot trades — `reconcile_live_ledger.py:136`)
  next to `BTC bot $Y`, under-labeled. Reads like a bot number; isn't. → 1-line label fix.
- **The real accuracy gap (drift, not contamination):** every headline PnL/ROI/win-rate is computed from
  *assumed* fills (`realized_pnl_usd` as booked at entry, zero-fee) — `_data.py:178-184`, `performance.py:106`,
  ribbon counters `ems.py:45-51`. Reconciliation to real Polymarket fills only runs when the operator
  manually invokes `tools/reconcile_live_ledger.py --apply`; daily counters aren't recomputed after; and
  the recon footer vanishes entirely when never run (`performance.py:25`) → no "stale vs reconciled" signal.
- **BTC filter fragility (secondary):** `"Bitcoin Up or Down" in title` `reconcile_live_ledger.py:135` is
  case/format-fragile and only governs the lifetime footer. Robust replacement: `slug.startswith("btc-updown-5m-")`.

### Open-position widget (feasible)
- Inputs at render: open rows (`entry_price`,`shares`,`side`,`window_slug`) `_data.py:96`; live mark
  (`market_up_price`/`market_down_price`, side bid/ask) from `latest_tick()` `_data.py:14`. Both already
  loaded in `ems.py:63,67`.
- `unrealized = (mark_side − entry_price) × shares`. Mark = side mid `(bid+ask)/2` (conservative).
- **Placement:** no literal empty column — LIVE MARKET is already grid-col 2 of `repeat(2,1fr)` (`style.css:113`).
  Cleanest = append an "OPEN POSITION" block *inside* `market.py`'s existing card (after the decision div,
  `market.py:37`); pass `open_pos`+`tick` (in scope). Also replace blotter's hardcoded `OPEN` (`blotter.py:47`)
  with the same unrealized number for current-window rows.
- Do **not** use `btc_recon.open_positions_value` for the live widget — it's the offline Data-API snapshot.

### Plan (after decisions)
1. `reconcile_live_ledger.py:135` → slug-prefix filter (verify activity field is `slug` vs `eventSlug` first).
2. `performance.py:42` → relabel `account` as `account (incl. non-bot)`.
3. Freshness badge on the performance card (`performance.py:102`)/ribbon: "reconciled as-of {asof}" vs
   "assumed-fill (unreconciled)" driven by presence/staleness of `btc_recon.*`.
4. Open-position widget in `market.py` (+ blotter unrealized).
5. DB-isolated verification: hand-check one UP + one DOWN unrealized; badge toggles with/without recon keys;
   render diff before/after on a snapshot DB.

### DECISIONS NEEDED FROM OPERATOR
- **D1 — Reconciliation cadence.** (a) Keep manual + add the freshness badge (least invasive; recommended),
  or (b) automate `reconcile_live_ledger.py` on a schedule with auto-`--apply` so headline numbers are
  continuously real-fill (mutates ledger; staleness guard is built for offline runs).
- **D2 — Widget scope & mark.** Per-position rows, single aggregate, or both? Mark = mid (recommended) vs
  side best-ask (exit-conservative)?
- **D3 — Non-current-window open positions.** Show "—/different window" v1 (recommended), or add the extra
  per-window tick query to mark them live?
- **P2 backlog (file as issue per `feedback_backlog_as_issues`):** add `conditionId`/`token_id` columns to
  `btc_paper_positions` so per-window reconciliation can deconflict the (rare) case where the operator
  manually trades the *same* 5-min BTC window as the bot — the only genuine per-window contamination vector
  (`reconcile_live_ledger.py:118-121`; no venue id on positions today, `db.py:68-89`).

---

# #91 — Heal phantom "max 1" singleton block (live) (2026-06-17)

**Issue:** #91. **Branch:** `feature/91-heal-phantom-singleton-block` off `develop`.

## Symptom
BLOCKED panel: every live entry rejected "an open position/order already exists (max 1)". Ledger flat (0 open rows); Polymarket history showed the last position bought AND sold (flat, funds intact). Bot blotter ↔ Polymarket agreed; the live executor's in-memory flag was stranded `True`.

## Root cause (from btc_live_orders details_json + Polymarket history)
Singleton gate blocks on `position_open or entry_order_resting`, read from live's in-memory `_position_open` / `_entry_order_id`. Stranded because: (1) a fully-matched entry kept its order id (a filled order isn't "resting"); (2) rapid stop/start (~10 BOOT_RECONCILE / 20 min) left `_position_open=True` with no open ledger row, and reconcile only syncs from an *open* row.

## Done (TDD, "Both" per operator)
- [x] Failing tests first (RED → GREEN).
- [x] `live.py submit_entry`: fully-matched entry (`_filled_shares`) drops `_entry_order_id`, records `_entry_matched_size` → `entry_order_resting` honest; skips doomed matched-cancel.
- [x] `live.py resync_flat()`: heals stale open-state; cancels tracked order then clears. Safe by invariant (ledger-flat ⟹ venue-flat: row closes only after confirmed flatten).
- [x] `paper.py _maybe_open_position`: `await executor.resync_flat()` after `COUNT(open)=0` → self-heals next tick, no restart/manual edit.
- [x] 558 green (+3); ruff clean; no new mypy. CHANGELOG v0.4.10; lessons.
- [ ] File P2 issue for the restart storm itself (operator/tooling driving ~10 boots/20min).

---

# #89 — Set trade size in SHARES from the CONTROLS panel (2026-06-17)

**Issue:** #89. **Branch:** `feature/89-share-denominated-sizing` off `develop`.

## Ask (operator)
Set trade size in shares (not $) from the CONTROLS panel; see the $ value of the selected shares; infographic that the minimum order is 5 shares; the share setting drives sizing everywhere.

## Done (committed incrementally — see note)
- `gate.py`: `btc_runtime.trade_shares` knob; `effective_max_trade_usd` = trade_shares when set (N shares ≤ ~$N); `_read_positive` DRY. Tests.
- `paper.py`: `_share_sized_notional()` → notional = shares × side ask when set. Test.
- `app.py`: `/api/runtime-config` key `trade_shares` (5 ≤ v ≤ 1000). Tests.
- `controls.py` + `ems.py`: shares input (min 5), live ≈$ value (favourite ask), $ range, 5-share infographic, hint. `strategy.py` sizing line shows shares.
- `dashboard.js` (`setTradeShares`/`updateShareValue`) + `style.css` infographic.
- **555 green; ruff clean; no new mypy.** CHANGELOG v0.4.8.

## ⚠️ Note — incremental commits
Mid-task, an external `git checkout develop` silently discarded uncommitted edits (reflog HEAD@{0}; not a hook — hooks only run gen_docs). Mitigation: committed after every chunk so work can't be wiped. If this recurs, investigate what runs `git checkout` (another session / scheduled task).

---

# #87 — Auto-bump sub-minimum orders to the venue share minimum (2026-06-17)

**Issue:** #87. **Branch:** `feature/87-auto-bump-min-order` off `develop`. **Supersedes #85's $5 floor.**

## Why
The #85 floor blocked the operator from setting sub-$5 clips — over-restrictive. Polymarket's real limit is 5 shares/order, which at ≥0.50 favourites costs only $2.50–$5. Operator wants full size control. So bump small orders UP to the venue minimum instead of forbidding small caps.

## Done
- Reverted #85 floor (endpoint/gate/controls + its tests) — any positive clip valid again.
- `live.py::submit_entry`: `size < min_size` → bump to `min_size` and place (guard: block if `min_size > MAX_AUTO_BUMP_SHARES = 10`). Logs the bump; `record_buy_notional` uses bumped size.
- `paper.py`: parity bump (`shares = max(shares, DEFAULT_MIN_ORDER_SIZE)`).
- Tests: live block test → places-bumped + too-large guard; dropped #85 floor tests. **542 green; ruff clean; no new mypy.**
- Cap is now a TARGET (may exceed by venue minimum, bounded ~$10). CHANGELOG v0.4.7.

---

# #85 — Live trades 100% blocked: runtime max-trade cap below Polymarket share minimum (2026-06-16)

**Issue:** #85. **Branch:** `feature/85-runtime-cap-min-floor` off `develop`.

## Problem
Dashboard BLOCKED panel: every entry rejected — `size 1.78 shares below Polymarket minimum 5.00 at price 0.5600`.
Operator set runtime **Max trade size = $1.00**. At favourites (price ≥ 0.50), $1.00 buys < 2 shares < Polymarket's
**5-share venue minimum** → `LiveExecutor.submit_entry` (`live.py:662`) correctly refuses every order. Bot is STOPPED, funds untouched.

## Root cause
No floor enforced on the per-trade cap (the #50 slice shipped without one):
- `POST /api/runtime-config` validates only `0 < value <= 1000` (`app.py:710`).
- HTML input hardcodes `min='0.5'` (`controls.py:36`); the "min $5.00" hint is cosmetic.
- Gate read accepts any `value > 0` (`gate.py`).
v0.4.5 (#50) CHANGELOG states the flawed assumption: *"a value below min gives a smaller fixed clip — no `min` changes needed."*
False — a clip below the min-trade size sizes every order below the venue minimum.

## Invariant
**Effective per-trade cap ≥ min-trade size (`BTC_PAPER_MIN_TRADE_USD`).** The cap is the sizing-range ceiling;
`notional_from_confidence` clamps to `[min, max]`, so a ceiling below the floor forces every order below the floor.

## Tasks (TDD)
- [ ] Failing tests: endpoint rejects sub-floor / accepts at floor; gate treats stored sub-floor override as invalid → env default.
- [ ] Pin `BTC_PAPER_MIN_TRADE_USD` in existing cap tests (they silently depended on the local `.env` floor=5).
- [ ] `gate.py`: stored override `< floor` treated as invalid → `None` in `refresh_runtime_limits` + `get_runtime_max_trade_usd` (heals the live bot's stale $1.00).
- [ ] `app.py`: reject `value < floor` with an actionable error.
- [ ] `controls.py`: HTML `min='{min_trade}'` so widget + hint agree.
- [ ] Full suite green; CHANGELOG v0.4.6; lessons.md; push to develop.

## Not in scope
#83 (backtest leak) / #84 (signal overfit) — those question live edge; this only unblocks order placement.

## Review (2026-06-17)
**Done.** Invariant enforced: effective per-trade cap ≥ `BTC_PAPER_MIN_TRADE_USD`.
- `gate.py`: `_runtime_override_or_none()` drops a non-positive / sub-floor stored override → env-default fallback; wired into `refresh_runtime_limits()` + `get_runtime_max_trade_usd()`. **Auto-heals the live bot's stale $1.00 on the next tick** — no migration, no manual DB edit.
- `app.py`: `POST /api/runtime-config` rejects sub-floor values with an actionable error.
- `controls.py`: HTML `min` now tracks the displayed floor.
- TDD: wrote failing tests first (RED → GREEN). Full suite **544 green (+3)**; ruff clean; zero new mypy errors on changed files.
- **Immediate operator action** (one of): in the dashboard set Max trade size back to ≥ $5.00 (or clear the override), then Start. After deploying this fix, the stale $1.00 self-heals on the first tick regardless.

---

# Plan — UI-settable max trade size (2026-06-16)

**Issue:** #50 (runtime config controls — the max-trade-size slice only).
**Branch:** `feature/50-runtime-max-trade-size` off `develop`.
**Scope (confirmed):** ONE runtime-settable **unified max trade size**, editable from the dashboard, read **every tick** (no restart). Position mode / multi-position is OUT — leave singleton, revisit later.

**Current behaviour:** operator `.env` has `BTC_PAPER_MIN_TRADE_USD=5`, `BTC_PAPER_MAX_TRADE_USD=5`, `BTC_LIVE_MAX_TRADE_USD=5` → fixed $5 clip. Goal: reduce/increase that $5 from the UI.

## Decisions
- New runtime knob `btc_runtime.max_trade_usd` in the `config` table, read every tick, **runtime → env** fallback (unset = current behaviour, fully backward-compatible).
- When set it governs BOTH the sizing ceiling (`_strategy_params().max_trade_usd`) and the gate per-trade cap (`effective_max_trade_usd`) → unified. `notional_from_confidence` already clamps to `[min,max]`, so values below min give a smaller fixed clip; above min re-enable confidence-scaled sizing — no `min` changes needed.
- UI follows the existing panel architecture: pure `render()` panel in `panels/`, data via `ems.py`, POST endpoint + `refreshAll`, theme CSS vocab. No bespoke UI.
- Singleton enforcement, `EntryRequest`, `GateConfig` position fields, `LiveExecutor` scalar state: **untouched**.

## Tasks
### 1. Gate (`btc_5m_fv/execution/gate.py`)
- [ ] `_runtime_max_trade_usd` state + `refresh_runtime_limits()` (both modes) reading `btc_runtime.max_trade_usd`.
- [ ] Properties `runtime_max_trade_usd` (raw override) + `effective_max_trade_usd` (override else `cfg.max_trade_usd`).
- [ ] `block_reason`: per-trade cap uses `effective_max_trade_usd`.
- [ ] Module helpers `set_runtime_max_trade_usd` / `get_runtime_max_trade_usd` + key const + validation.

### 2. Loop (`btc_bot/paper.py`)
- [ ] `paper_tick_once`: `await _risk_gate.refresh_runtime_limits()` each tick (both modes).
- [ ] `_strategy_params()`: `max_trade_usd = gate.runtime_max_trade_usd if set else BTC_PAPER_MAX_TRADE_USD`.

### 3. Dashboard (respect existing panel architecture)
- [ ] `POST /api/runtime-config` (`{key:'max_trade_usd', value}`): validate (0 < v ≤ 1000), persist via gate setter, audit via `notify`.
- [ ] `panels/controls.py` CONTROLS card: current max (operator vs env), number input + Apply. Built for mode/positions to be added later.
- [ ] `ems.py`: load current value + render the card in the grid. `strategy.py`: show effective max.
- [ ] `dashboard.js`: `setMaxTradeSize()` (confirm → POST → toast → refreshAll). `style.css`: `.ctl-row`/`.ctl-input` matching theme.

### 4. Tests + ship
- [x] gate: effective override set/cleared; `refresh_runtime_limits` fallback; per-trade cap uses effective; set/get round-trip + validation.
- [x] dashboard: endpoint persists + validates; controls panel renders.
- [x] `pytest tests/` green (503, +15 new); my changed files add zero new `ruff`/`mypy` errors (pre-existing develop debt untouched).
- [x] README/CHANGELOG note. Commit + push to **develop** (never main).

## Review (2026-06-16)
**Done.** Operator can now set the unified max trade size from the dashboard CONTROLS card; the loop reads `btc_runtime.max_trade_usd` every tick (paper + live), no restart. Unset = prior behaviour.

- **Gate** (`gate.py`): `refresh_runtime_limits()` + `runtime_max_trade_usd`/`effective_max_trade_usd`; `block_reason` uses the effective cap; `set/get_runtime_max_trade_usd` helpers.
- **Loop** (`paper.py`): per-tick refresh; `_strategy_params()` honours the override for the sizing ceiling. One knob = sizing ceiling + gate cap (unified).
- **Dashboard**: new `panels/controls.py` CONTROLS card, `POST /api/runtime-config` (validated, audited), `setMaxTradeSize()` JS, `.ctl-*` theme CSS, STRATEGY sizing line override-aware — all following the existing panel architecture.
- **Verified**: 503 tests green (+15). Browser round-trip confirmed: set $3 → card "operator", STRATEGY "$3/clip", out-of-range rejected; restored to env default $5 after.
- **Out of scope (deferred):** singleton/multiple mode + max positions + LiveExecutor multi-position refactor — left intact for later.

---

# (Superseded) Plan — singleton/multiple position mode + max live positions
Deferred at operator request (2026-06-16): keep singleton for now; revisit multi-position + the LiveExecutor scalar→map refactor later. Full design preserved in session history / memory.

---

# Plan — Unified RiskGate: paper as a faithful preview of live (2026-06-15)

## Goal

Paper and live share **one gate stack**. What paper does is what live will do.
Live-only is the actual order submission. The user wants to trust paper as a
live preview before flipping `BTC_BOT_MODE=live`.

## Why this is needed

After #61 removed the daily bankroll cap default, paper and live STILL diverge.
Today (2026-06-15) the operator switched paper→live→paper because every $5 live
entry from 11:10–11:30 was BLOCKED by the (then-active) bankroll cap; the same
signal opened cleanly as paper rows at 11:57 and 12:00. Even with that cap off,
4 other gates still produce silent divergence:

| # | Gate | Paper | Live | Lives in |
|---|---|---|---|---|
| 1 | Bankroll cap | none | `BTC_LIVE_BANKROLL_CAP_USD` (opt-in, off now) | `LiveExecutor.entry_block_reason` |
| 2 | Kill-switch file | ignored | blocks + cancels resting | `LiveExecutor.kill_switch_active` |
| 3 | Daily realized-loss halt | ignored | `≤ -BTC_LIVE_DAILY_LOSS_HALT_USD` ($10 default) | `LiveExecutor` + persisted `btc_live.*` |
| 4 | Per-trade USD cap | `BTC_PAPER_MAX_TRADE_USD` | `BTC_LIVE_MAX_TRADE_USD` (different env knob) | strategy params vs gate |
| 5 | Slippage guard | none | `BTC_LIVE_MAX_ENTRY_SLIPPAGE` ($0.02) | `LiveExecutor.submit_entry` |
| 6 | Singleton check | open ledger row only | open row OR resting unfilled order | `LiveExecutor` |

## Implementation plan

### Phase 1 — Extract venue-independent gate
- [ ] New `btc_5m_fv/execution/gate.py::RiskGate`
  - Move `entry_block_reason` body + the daily counter machinery
    (`record_realized_pnl`, `_roll_daily_window`, `_load_risk_state`,
    `_persist_risk_state`) out of `LiveExecutor` and into the new class.
  - Counters keyed on a generic prefix (`btc_risk.*`) not `btc_live.*`.
  - `LiveExecutor` holds a `RiskGate` instance (composition over inheritance)
    and delegates; live also has the slippage guard, which it applies on top
    of the gate's verdict using the live book at submit time.

### Phase 2 — Wire the gate into the paper path
- [ ] `btc_bot/paper.py::_maybe_open_position` calls `gate.block_reason(...)`
      before opening any row. Blocked entries are journaled to a new
      `btc_paper_blocked` lightweight log (or reuse `btc_live_orders` with a
      `mode='paper'` column — pick whichever the user prefers).
- [ ] Slippage guard in paper: re-quote the book just before "filling" and
      apply `BTC_TRADE_MAX_ENTRY_SLIPPAGE` against the snapshot signal price.
      Same threshold as live.
- [ ] Singleton check in paper picks up the resting-order parity by virtue of
      sharing the gate.

### Phase 3 — Paper closes feed shared counters
- [ ] On every paper position close, call `gate.record_realized_pnl(pnl)` so
      the daily-loss halt advances identically across modes.
- [ ] On every paper entry, call `gate.record_buy_notional(notional)` so the
      bankroll cap (when set) advances identically.
- [ ] Counter rollover at UTC 00:00 already works — no change there.

### Phase 4 — One knob per concept
- [ ] `BTC_TRADE_MAX_USD` becomes the canonical per-trade ceiling.
- [ ] `BTC_TRADE_MAX_ENTRY_SLIPPAGE` becomes the canonical slippage cap.
- [ ] Old `BTC_PAPER_*` / `BTC_LIVE_*` knobs read as deprecated aliases for one
      release; startup logs a WARN when only the old name is set.
- [ ] Persisted `btc_live.{risk_date,daily_realized_pnl,daily_buy_notional}`
      renamed to `btc_risk.*` with a one-time read-and-migrate on boot.

### Phase 5 — Tests, dashboard, runbook
- [ ] `tests/unit/test_risk_gate.py`: 10-row table-driven fixture covering
      kill switch, daily-loss halt, bankroll cap (when set), per-trade cap,
      slippage, singleton, settle-style one-per-window. Asserts the SAME
      decision in paper and live for every row.
- [ ] Dashboard: new line "Next entry: would BLOCK — <reason>" or "OK"
      sourced from `gate.block_reason(...)` with the same params the next
      entry would use. Operator never needs SQL to see gate state.
- [ ] `docs/OPERATIONS_RUNBOOK.md`: update "going live" section — paper now
      already enforces every live gate, so a clean paper session is the live
      readiness signal.

### Phase 6 — Ship
- [ ] Branch from develop: `feature/<issue#>-unified-risk-gate`.
- [ ] `pytest -q`, `ruff check`, `mypy` all green.
- [ ] Commit + push to develop. Operator merges develop→main.

## Out of scope

- Changing gate VALUES — only the wiring changes.
- Adaptive auto-pause (#36) — already symmetric across modes, nothing to do.
- Replacing `RiskService` in `btc_5m_fv/ops/controller.py` — that controller
  is not on the live path; can be reconciled separately.

## Acceptance

1. With paper running on today's signal, paper-side BLOCKED rows match the
   reasons the same minute would emit in live (re-run the 11:10–11:30 window).
2. All 10 unit-test rows show paper == live decision.
3. Dashboard shows the live-equivalent gate verdict for the next entry, refreshed every tick.

## Won't do without explicit operator sign-off

- Filing the GitHub issue (waiting on review of this plan first).
- Touching `main`.

---

# (Historical) Adaptive layer + go live (2026-06-14)

Preflight GO: MetaMask key in, Gnosis Safe funder `0xc1Daa…` holds $37.86, sig
type 2, wallet already approved (max allowances). Hard caps in code: $5/trade,
1 position, -$10/day halt, $30/day cap, kill switch.

## "Leverage AI" — the honest split
- NOT a price predictor (loses to latency bots on 5m BTC).
- YES adaptive risk control + an AI research analyst over OUR OWN journal.

## Phase 1 — Adaptive risk controller (#36, build now, protects from trade 1)
- [x] btc_bot/adaptive.py: rolling expectancy / win-rate / Brier calibration
      over the last N closed trades of the active style.
- [x] Auto-pause when rolling ROI drops below a floor — sticky until cleared.
- [x] Config + entry-path integration + journal/dashboard + clear tool. Tests.

## Phase 2 — AI research loop (design now, build once live fills accumulate)
- [ ] docs/RESEARCH_LOOP.md: nightly agent mines journal → proposes filters →
      backtests OOS on the recorded archive → surfaces survivors for operator
      approval. AI proposes, human disposes. Never auto-applies to live.

## Phase 3 — Go live
- [x] Final preflight GO → start live bot → verify boot gate + first real entry.
      Live ran 2026-06-15 07:07–08:31 (7 fills, +$7.78 realized).

## Won't do
- RL auto-tuning on live $37 (overfits/blows up — sample far too small).
- Any live-param change without OOS validation + operator sign-off.

---

# (Historical) Live Executor Build — Issue #20 (2026-06-10)

## Plan
- [x] Fix runtime blockers (#19): main.py import, Binance endpoint, backtest retries
- [x] Paper bot running end-to-end (dashboard :7860, ticks in SQLite)
- [x] Backtest grid running on April history (background)
- [x] Implement live execution mode (py-clob-client) with hard risk limits
- [x] Adversarial review: financial-risk, API correctness, safety gates
- [x] Fix findings, tests green (395 passing)
- [x] Push to develop
- [x] Operator (Zayan) provides POLYMARKET_PRIVATE_KEY + BTC_LIVE_CONFIRM and launches live

## Review

Adversarial review (3 reviewers, 22 findings: 4 critical / 8 major / 10 minor)
— all addressed on this branch:

- **Persisted daily risk counters** — daily loss halt + daily bankroll cap now
  live in SQLite (`config` table) and are reloaded at executor start.
- **No false closes** — a blocked/failed/unfilled live exit keeps the ledger
  row OPEN and retries next tick.
- **Exit lifecycle** — exit SELLs are tracked, awaited, cancelled on timeout.
- **Stop race eliminated** — controller waits for the runner thread.
- **Boot reconciliation** — cancel_all + journal-based re-adoption.
- **Kill switch** — re-arms on file deletion, TOCTOU re-check before entry POST.
- **Boot gate hardening** — funder required for signature types 1/2.
- **Misc** — entry slippage guard, ledger-before-submit ordering for entries.

## Data-integrity build (#21/#22/#23) — 2026-06-11
- [x] CLOB executable quotes in signal path + honest fills
- [x] Chainlink settlement connector (REST + WS) + REST spot-poll fallback
- [x] Tie-rule fair value; degraded-feed entry/exit gating
- [x] WS feed lifecycle wired into run loop
- [x] KPI re-baseline: pre-clob rows excluded
- [x] Dashboard state derived from runner thread both directions (#23)
- [x] 16 new tests; full suite green

## Execution plan — soak to live gate (2026-06-11, issues #25-#27)

Soak started 2026-06-11 05:28Z on the honest baseline.

### Phase 1 — let it soak (no action)
- [x] 4-6h runtime, target 100+ closed clob-baseline trades

### Phase 2 — quality review (#25)
- [x] PnL/win/drawdown by exit reason and entry-time bucket
- [x] Churn analysis; per-window cooldown spec (#27)
- [x] Fill-realism haircut on expectancy
- [x] Gamma-vs-CLOB staleness stats

### Phase 3 — stability + ops (#26)
- [x] Tick-gap/crash audit; WS 429 recovery check
- [x] CI green on develop
- [x] Kill-switch drill in paper mode

### Phase 4 — live gate (operator decision)
- [x] Present haircut-adjusted expectancy verdict
- [x] If GO: operator sets POLYMARKET_PRIVATE_KEY etc. (done 2026-06-15)

### Backlog (non-blocking)
- [ ] Backtest harness on Chainlink data instead of Binance
- [ ] develop -> main merge (requires explicit operator approval)
- [ ] Evaluate beta polymarket-client SDK migration
