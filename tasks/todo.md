# Branch close-out — pivot off all 5-minute markets (2026-08-29)

Operator decision this session, after re-litigating "is this viable" from scratch
(prompted by a cloud-GPU/LLM-training question that turned out to be the wrong
question): **stop all 5-minute-market work.** Closing `feature/182-pairarb-shadow`.
Next chapter is daily/hourly/longer-horizon Polymarket markets — category not yet
chosen, zero code or data exists for it in this repo, fresh build.

## Why now, not a new finding — a second confirmation of the existing one

`docs/archive/PIVOT_2026-07.md` (2026-07-10) already reached this verdict for
BTC-direction prediction specifically: 0/75 edge slices survived FDR, real money
−$19.35 net over 351 live fills, fees (0.07 taker, crypto's rate is the platform's
highest) consumed the entire gross edge, and the actors who profit on this venue
are subsidized makers/latency snipers, not forecasters. Its own recommended next
step (option C) was to move toward slower, lower-fee categories where the edge
dimension is forecasting calibration, not latency — that recommendation sat
unactioned while `feature/182-pairarb-shadow` pursued a different mechanism
(copytrading a profitable account's flow into thin alt-coin books) instead of
prediction.

This session re-ran the copytrade shadow numbers as a second, independent test of
the same underlying constraint (5-minute crypto markets on this venue): still net
negative, and by more than the last recorded snapshot —
`data/copytrade_doge.db`: **−$307.68 over 1,980 settled fills**;
`data/copytrade_min.db`: **−$174.47 over 1,756 settled fills** (2026-08-17's
snapshot in e4bfbdb had these at roughly −$228/−$174 — the doge book kept
bleeding as more shadow trades settled, the min book held flat). Two different
mechanisms (direction prediction, copytrading), same market structure, same
result. `data/pairarb_shadow.db` never accumulated fills (`pair_execs: 0`,
`pair_windows: 31`) — no verdict possible there, moot now regardless.

## What carries forward to the next chapter

- The discipline, not the code: shadow/paper before capital, pre-registered
  deploy bar, OOS validation before any live weight, fee-true accounting from
  fill one. All of that stays; it's what correctly killed this chapter before it
  lost more than $19.35+shadow-losses total.
- Nothing model-specific carries forward. No Chronos integration, no calibration
  curve, no copytrade/pairarb code is known to work on a different market
  category — the failure mode here (fees exceed a thin real edge in a
  latency-dominated venue) is specific to 5-minute crypto, not necessarily
  present in slower categories, but that is an untested hypothesis, not a
  finding, until a category is chosen and shadow-tested.
- Immediate next decision, blocking any build: which longer-horizon category
  (crypto price-by-date, macro, politics, sports, etc.) — each has a different
  fee tier and a different information source, which determines the entire data
  pipeline and model shape. Not yet chosen.

---

# Update — fast feed wired into copytrade_shadow, gen_docs count_tests bug fixed (2026-08-29)

Continuing the concluded priority order from `tasks/2026-08-17-strategy-discussion.md`
tick 2 (fix-the-gate > get-pairarb-data > re-test-copytrade-cleanly >
merge-and-shelve-#180). Built item 1's shadow-safe half and unblocked item 3.

## Built: a key-free fast feed, wired into `copytrade_shadow.py` only

Left `copytrade_live.py` untouched — its trade-detection loop is live-money-
adjacent and any change there needs operator review, not a loop tick. Scoped
this to the shadow tool, which has no money at stake.

- **`btc_bot/pairarb/feed.py`**: added `http_poll_fills()` — polls
  `eth_getLogs` against a public Polygon RPC (`PUBLIC_HTTP_RPCS`, no API key)
  every ~2s. Same decoded `OrderFilled` event as the WSS transport, at
  poll-interval-plus-block-time latency instead of push — still an order of
  magnitude closer to block time than the ~20s-stale data-api. This does NOT
  change `open_feed()`'s existing WSS/api-fallback contract or its pinned
  refusal tests — it's a separate, explicitly-opted-into function, so a
  caller must choose it deliberately.
- **`btc_bot/pairarb/market_index.py`** (new): `TokenIndex` — the on-chain
  feed only carries a token_id, not a slug/outcome/conditionId. Resolves
  outcome tokens to market metadata by fetching each tracked asset's current
  + previous 5m window from Gamma (clock-derived slug, the #181 scheme). 12
  tests.
- **`btc_bot/pairarb/mirror.py`**: added `trade_dict_from_fast_fill()`, the
  adapter seam between a fast-feed fill + a `TokenIndex` resolution and
  `price_the_copy()`'s existing dict-shaped input. 2 tests.
- **`tools/copytrade_shadow.py`**: new `--feed {api,rpc}` flag (default
  `api`, unchanged behavior). `run_rpc()` sources fills from
  `http_poll_fills()` instead of polling `data-api/activity`, reuses the same
  ledger/settlement code via an extracted `_settle_due()` helper.
  **Network-smoke-tested against the real target before trusting it**:
  `--feed rpc --once` picked up 3 real fills in 30s, correctly resolved to
  `btc-updown-5m-1787982300`/Up, priced against the live book, written to the
  ledger. No orders placed (shadow only, unchanged).

## Started: a clean copytrade shadow run (fixes BOTH open bugs at once)

`data/copytrade_rpc.db`, PID 80736, `--feed rpc --assets btc,eth,sol,xrp,doge,bnb`
— fast feed AND proportional sizing (no `--max-shares 5 --min-shares 5`
override). This is the first run that is actually comparable to the 8-fill
"92% captured" sample. **Did not touch PIDs 9268/13259** — no kill required,
this runs alongside them to a separate DB. Let it accumulate before reading it.

## Fixed in passing: `tools/gen_docs.py`'s `count_tests()` silently returned 0

Found while re-running `gen_docs.py` per project convention before committing.
`count_tests()` returned 0 whenever ANY test module failed to *collect* —
which is always true right now (3 modules fail on missing optional local
deps: polars/py_clob_client_v2/h2, a pre-existing environment gap, not a
regression). It was about to commit `AGENTS.md`/`docs/CODE_MAP.md` and
`docs/FILE_MAP.md` with "**Tests:** 0.", replacing the stale-but-plausible
"828." with an actively false number. Root cause: `subprocess.run(...,
"--collect-only", ...)` without `--continue-on-collection-errors`, plus a
blanket `if out.returncode != 0: return 0` that discarded the real count
whenever unrelated modules errored. Fixed: added the flag, removed the
returncode gate, kept the trailing-summary-line parser as the actual source
of truth (already safely returns 0 for a genuinely empty/malformed run).
Verified: `test_gen_docs.py::test_test_count_is_positive_int` now passes;
`AGENTS.md` correctly reads "**Tests:** 822." (down from the stale 828 because
the 3-module collection gap predates this session — not something this fix
caused). `tests/unit/test_live_wiring.py::test_kill_switch_skips_new_entries_in_tick`
remains a pre-existing failure (missing `h2`), confirmed via `git stash`
against baseline before touching anything — unrelated to this session's work.

## Still open, unchanged from tick 2's priority list

- [ ] Wire the same fast feed into `copytrade_live.py`'s actual `run()` loop
      (or make `assert_copy_live_allowed()` stop implying it already does) —
      deliberately deferred this tick; live-money code path, needs operator
      review before an agent touches it.
- [ ] Let `pairarb_shadow.py` (PID 78172) and the new `copytrade_rpc.db` run
      (PID 80736) accumulate before reading either.
- [ ] Restart PIDs 9268/13259 (the original mis-sized shadow configs) — still
      blocked on operator go-ahead to kill them; superseded in practice by the
      new PID 80736 run, so this is now optional cleanup, not a blocker.
- [ ] #180 merge-and-shelve — unchanged, not touched this tick.

---

# Update — copy-tradeability decomposition + two new findings (2026-08-17, later)

Operator asked to decompose the −$174.47/−$228.67 aggregate loss (see section 2
below) before drawing any conclusion. Queried both live DBs directly.

## Decomposition result: the loss is not one phenomenon

**By asset** (`copytrade_min`, all-assets config): bnb (−$94.19) + doge (−$50.92)
= 83% of the −$174 total. sol/xrp are net POSITIVE and track the target closely.
btc's −$60.73 is mostly the target itself losing money on btc (−$77.32), not a
copy-cost story.

**By lag bucket**: not monotonic. 11–30s (730/1,756 fills, the largest bucket) is
the real lag-cost bucket — we lose (−$81.15) while the target wins (+$25.17).
30–60s has *more* lag and is breakeven. 60s+ is bad for both sides near-equally
(the target having a bad stretch, not a copy cost).

## Finding 1 — sizing bug invalidates the comparison to the 92%-capture sample

Both running shadow configs were pinned `--max-shares 5 --min-shares 5`. Verified
directly: **100% of fills in both DBs (11,666/11,666 and 3,080/3,080) are exactly
5.0 shares**, regardless of the target's actual trade size — `want = max(min,
min(their_size·scale, max))` collapses to a constant when min=max. The 8-fill
sample in `lessons.md` that read "92% captured" used proportional sizing (a
`sol Up: him 0.240 → −$4.80` leg implies ~20 shares, not 5). **The two numbers were
never comparable** — the sign flip may be substantially a methodology change, not
new information about the underlying edge.

## Finding 2 — the "feed fix" gate is a string-presence check, not a wired transport

More serious than expected. Traced both `tools/copytrade_shadow.py` and
`tools/copytrade_live.py`'s actual trade-detection loops: **both still poll
`data-api.polymarket.com/activity` on a plain sleep loop** (4s and 3s
respectively). `copytrade_live.py` imports `open_feed`/`FeedUnavailable` from
`btc_bot/pairarb/feed.py` (bf9a68a/1fc0b85) but **never calls them** —
`assert_copy_live_allowed()` only checks `os.getenv("POLYGON_RPC_WSS", "").strip()`
is non-empty as a boot gate. The actual `run()` loop (line ~233) is hardcoded to
the same ~20s-stale Data API poll regardless. **If the operator sets
`POLYGON_RPC_WSS` and arms every other gate, `--live` would still detect trades on
the stale feed** — the gate that's supposed to prevent exactly that doesn't
connect to anything. `tools/copytrade_onchain.py` (the actual onchain
listener/decoder) exists but was only ever a standalone validation script — never
wired into either the shadow ledger or the live executor's trade loop. Also:
`btc_bot/pairarb/feed.py`'s `FeedFill` only carries `token_id`, not
slug/outcome/conditionId — a token→market resolver (reverse of the existing
slug→`clobTokenIds` lookups in `btc_bot/paper.py`, `tools/venue_recorder.py`,
`tools/pairarb_shadow.py`) does not exist yet anywhere in the repo. Wiring the
real transport in is a real build, not a restart.

## Actions taken this session

- [x] Started `tools/pairarb_shadow.py` (all 6 assets: btc,eth,sol,xrp,doge,bnb) —
      PID 78172. It had **0 `pair_execs`** after 71h despite the investigation
      itself concluding this was the more real, reproducible edge. Running now.
- [ ] **Blocked, needs operator action**: restart the two `copytrade_shadow.py`
      processes (PID 9268 `data/copytrade_min.db`, PID 13259
      `data/copytrade_doge.db`) with proportional sizing (drop
      `--max-shares 5 --min-shares 5`, use the default `--scale 1.0 --max-shares
      50`) so the next read is actually comparable to the 8-fill sample. Killing
      them was blocked by the auto-mode permission classifier — needs explicit
      go-ahead or the operator's own `kill 9268 13259`.
- [ ] Not started: wiring `open_feed()` / the onchain transport into
      `copytrade_shadow.py`'s actual detection loop (needs a token_id→market
      resolver first). This is the fix for the 11–30s-bucket lag cost
      specifically — it does not touch the sizing bug or the doge/bnb structural
      markup, which are separate causes.
- [ ] `assert_copy_live_allowed()`'s `POLYGON_RPC_WSS` check should either
      actually call `open_feed()`/wire the transport into `run()`, or its
      docstring/error message should stop implying it does — currently a false
      sense of the gate working.

---

# Where we are (status check, 2026-08-17)

Prior turn wrote a review save-point to this file and it did not survive — the
working tree shows no trace of it, and `git reflog` around that time shows two
branch checkouts (`feature/180-m1-ofi-decay` → `develop` → `feature/182-pairarb-shadow`)
between then and now, which resets `tasks/todo.md` to whatever each target branch
had. This is exactly the `lessons.md` entry "Commit incrementally — uncommitted work
can be silently discarded" (2026-06-17, #89) reproducing itself. This update is being
committed immediately for that reason.

## 1. Sigma-Gap design (`docs/STRATEGY_DESIGN.md`) — fix already exists, unmerged

The adversarial review run two turns ago (5-lens multi-agent pass + verification)
found: M1's daily kill-leg is statistically vacuous, the M2′ capital gate has an
unmitigated EIV false-pass channel, the daily hedge line prices perp fees only
(funding/margin/basis omitted), the "27–67 obs/day" power claim is arithmetically
impossible for BTC/ETH, and — the load-bearing one — **no document computes the
annual PnL ceiling**, which from the design's own inputs is ~$5–55k/yr gross,
~$1–3k/yr central case.

**That work already happened.** `git log` / `git branch -a` show a commit
`docs: third review — D1 (research program) and D2 (M1 1h-only kill)` (6ef702e,
2026-08-13, co-authored by a prior Claude session) on branch
`feature/180-m1-ofi-decay` — 13 commits ahead of `develop`, 2 behind, **no PR**,
issue #180 still open. It logs `CORRECTIONS.md` **C11–C18**, and the findings match
this session's independent review almost exactly, including the same $5–55k/yr /
$1–3k central prize figures and the identical two-ground refutation of the M1 daily
leg (null clears its own bar 19–27% of the time; phantom-tilt guard needs N_eff>1,601
vs 743 available). **D1**: treat the program as research, not a funded business.
**D2**: amend the frozen M1 pre-registration — drop the daily leg from the kill
condition, 1h alone carries it (numeric thresholds unchanged; the run is still
paused, so amending before scoring is legitimate under its own rules).

**This branch was never merged**, and when `feature/182-pairarb-shadow` was cut from
`develop` afterward, only the venue-recorder commit was cherry-picked forward
(`a5ba16d` → `f809c64`) — the docs fixes (`619b35b`, `6ef702e`) were left behind.
`docs/STRATEGY_DESIGN.md` on the current branch is therefore still at
second-review state; the C11–C18 findings this session re-derived are real on this
branch, and the fix for them exists, just not here.

- [ ] Decide: merge/rebase `feature/180-m1-ofi-decay`'s doc commits into `develop`
      (cherry-pick `619b35b` + `6ef702e`), or explicitly abandon that branch if the
      program direction has moved on to #182 instead.
- [ ] If kept, open the PR for #180 that never got opened.

## 2. Copytrade + pairarb build — issue #182 (active line of work)

No commit has touched `docs/STRATEGY_DESIGN.md`'s program since 2026-08-12 on any
branch that fed into this one — #182 is a separate, concrete investigation: is
Polymarket account `@mayormamdani` ($213→$42,704 since 2026-06-09) copy-tradeable?
Issue #182 is **OPEN**. Plan: `tasks/2026-08-14-pair-arb-shadow-plan.md`.

### What shipped (11 commits, +3,629/−1 lines, `f809c64..bf9a68a`, all 2026-08-14)

- **#181 first** (`f809c64`): venue recorder — full-depth L2 + trade tape + aligned
  reference, fixing a zombie-window discovery bug (stale Dec-2025 markets sorted
  before Aug-2026 ones under `endDate` ascending; fixed by constructing the 5m slug
  from the clock instead of discovering it).
- **Investigation → two strategies, not one.** Measured `@mayormamdani`'s tape (5,072
  trades, 9.3h): 100% BUY, both legs held in 79% of windows, median combined leg cost
  $0.989 for a $1.00 payout. Live books ruled out a taker arb (best-ask sum 1.0100,
  crosses at 1.0449 after fees). His own fee record is cleanly bimodal — 40.3% maker
  (fee≈0), 35.3% taker (fee≈0.07, p95=0.0666 independently validating
  `btc_bot/shadow/fees.py`). **He's a market maker, not a directional bettor** — not
  copy-tradeable in the naive sense, but his strategy is reproducible natively.
- **`btc_bot/pairarb/`** (new package, shadow only, no execution ever) — two-sided
  resting-bid quoting on 5m Up/Down: initial shadow tester → re-quoting/VWAP
  settlement/durable ledger → hold quotes instead of chasing every tick → rest below
  the touch, not at it. Back-of-queue maker fill sim over recorded L2 + trade tape
  (conservative by construction — no assumed priority, no self-impact credit).
- **`copytrade`** (mirrors the target directly, separate from pairarb) — sized from
  an early small-sample read (`tasks/lessons.md`, 8 real fills: target +$24.79, copy
  +$22.81, **92% captured**, 1.56¢/share slippage) into: a live mirror shadow priced
  against the book we'd actually face → the venue's 5-share minimum modeled → asset
  filter + size ceiling + a **gated** live executor → slippage guard + read-only
  dashboard.
- **Feed latency fix** (last 2 commits): the data-api activity feed is ~20s stale
  (median), and slippage tracks lag almost linearly (2.82¢ at 0–2s vs. 9.56¢ at
  11–30s) — at a ~1¢/share edge, staleness inverts the trade, not just slows it.
  Added a Polygon `OrderFilled`-log transport (~2s, block time, carries maker/taker
  identity + fee). The stale transport now requires explicit opt-in
  (`allow_api_fallback=True`); `open_feed()` raises `FeedUnavailable` by default, and
  the live executor refuses to boot without `POLYGON_RPC_WSS` — same refusal class
  as the key/confirm/client gates. Operator: "if anything has a 20 second delay in
  this project get rid of it." **Every shadow number collected before this commit
  was on the stale feed — a lower bound, not an estimate of a 2s copier.**

### Current running state (checked live, 2026-08-17)

Three background processes, running continuously since Friday (~69h):

| PID | Process | DB | Rows |
|---|---|---|---|
| 13259 | `copytrade_shadow.py --assets doge --max-shares 5 --min-shares 5 --max-their-size 50` | `data/copytrade_doge.db` | 3,026 fills |
| 9268 | `copytrade_shadow.py --max-shares 5 --min-shares 5` (all assets) | `data/copytrade_min.db` | 11,628 fills |
| 14035 | `copytrade_dashboard.py --port 7861` | (reads both) | — |

**`pairarb_shadow.py` is NOT running.** `data/pairarb_shadow.db` has 25
`pair_windows` and **0 `pair_execs`** — the maker-quoting strategy (the one the
investigation concluded was the *real*, reproducible edge) has not accumulated
meaningful shadow data. The copytrade side — the one the investigation's own numbers
said should be structurally worse — is what's actually been running at scale.

### ⚠ Live shadow result contradicts the sample that justified the live executor

Queried both running copytrade DBs directly. On settled fills only (most rows are
still-open positions pending window resolution):

| Config | Settled fills | Our total PnL | Target's total PnL (same fills) | Avg our PnL/fill | Avg their PnL/fill |
|---|---|---|---|---|---|
| `copytrade_min` (all assets) | 1,756 / 11,628 | **−$174.47** | +$24.36 | **−$0.099** | +$0.014 |
| `copytrade_doge` | 1,194 / 3,026 | **−$228.67** | +$83.52 | **−$0.192** | +$0.070 |

Win rate matches exactly between us and them on every fill (as it must — same
outcome token, same resolution), so this isn't a directional-luck artifact; it's
entry-price slippage compounding against us on both winners and losers. This is the
opposite sign from the 8-fill sample in `tasks/lessons.md` that read "92% captured"
and was the evidentiary basis for building the asset filter, size ceiling, and gated
live executor afterward — and it's a much larger sample (1,194–1,756 settled vs. 8).
**This is the exact pattern the project's own lessons repeatedly warn about**
(screen-population-trap / small-sample trap). Two things need checking before
either conclusion is trusted: (1) the 8-fill sample may simply have been favorable
noise, and 69h at scale is the more trustworthy read; (2) this run predates the
onchain-feed fix in the two most recent commits, so per that commit's own framing,
**this −$174/−$229 result may itself be a lower bound collected on the handicapped
feed**, not a clean read of the current code path. Neither is resolved yet.

- [ ] Do not arm `COPY_LIVE_CONFIRM` / run `copytrade_live.py --live` until this is
      reconciled.
- [ ] Restart the two shadow processes on the onchain-feed code path and let a fresh
      sample accumulate before re-reading the capture rate.
- [ ] Decompose the −$174/−$229 by asset, side, and lag bucket before concluding
      either way — per `lessons.md`'s own repeated instruction not to trust an
      aggregate.
- [ ] Investigate why `pairarb_shadow.py` isn't running while `copytrade_shadow.py`
      is, given the investigation's own numbers favor pairarb.

### Hygiene noticed while checking this

- [ ] `AGENTS.md`, `docs/CODE_MAP.md`, `docs/FILE_MAP.md` have **uncommitted**
      `tools/gen_docs.py` regeneration (adds `pairarb/` rows, bumps test count to
      828, flags `btc_bot/pairarb/feed.py` as dead code). Commit or regenerate again
      next session so `AGENTS.md`'s snapshot isn't stale on read.
- [ ] That dead-code flag on `feed.py` is a **false positive** — it's imported and
      load-bearing in `tools/copytrade_live.py` (the live executor's refusal gate
      depends on `open_feed`/`FeedUnavailable`). `gen_docs.py`'s importer scan
      appears not to look inside `tools/`.
- [ ] Local test env: `pytest` collects 801/803 passing; 3 modules fail to *collect*
      (not fail) on missing optional local deps — `polars`, `py_clob_client_v2`,
      `h2`. Likely a `pip install -r requirements.txt` refresh, not a regression;
      confirm against CI before treating as one.

## Next step (not started)

Get an operator decision on #1 (merge/abandon the stranded #180 fix branch) and #2's
shadow-PnL reconciliation before any further build work on either program.

---

# Reopen: 9 issues filed, discuss-first process (#169–#177) (2026-08-04)

Project was ARCHIVED 2026-07-10 (v1.0.0, negative result — see `docs/archive/FINDINGS.md`,
`docs/archive/PIVOT_2026-07.md`, `docs/archive/POSTMORTEM_2026-07.md`). Operator is reopening it. Before any
build work, filed 9 issues to scope the reopen — 8 are discuss-first (mechanism has to make
sense in plain language + on paper before any code; only then test on samples), 1 is a
mechanical rename already in progress.

- [ ] **#169** `[P1]` Remove "fair value" branding across the codebase (mechanical; in progress
      this session — `btc_5m_fv/` → `btc_5m_exec/`, `fair_value.py` → `pricing_model.py`,
      `fair_value_v0/v1` → `pricing_v0/v1`; 175 text refs / 402 import-path refs across ~100
      files; regenerate `docs/FILE_MAP.md` via `tools/gen_docs.py`, don't hand-edit)
- [ ] **#170** `[P0]` Strategy legibility — what it's doing, evidence, math, references. Current
      gate lineage: `cushion_favorite_v2` → `cushion_fresh_v7` → `cushion_fresh_v7_f45` →
      `cushion_fresh_v7_f45_spread`, plus `fair_value_fresh_v8` (`btc_bot/shadow/signals.py`)
- [ ] **#171** `[P0]` Real architecture — PNG diagram of current state, decide target, rebuild.
      Sibling repos `pretrade-risk-controls`/`ledger-recon`/`polymarket-ems` look decomposed
      from this project but aren't wired as dependencies anywhere (not in requirements.txt,
      not published). `btc_5m_fv/backtest/harness.py` confirmed dead code this session (tested,
      zero production importers — unrelated to f45/v7/v8, which run through `tools/replay_race.py`
      and already fill at real best-ask, fee-true, validated against live fills)
- [ ] **#172** `[P1]` UI redesign — current dashboard reads as generic AI-scaffolded UI
- [ ] **#173** `[P2]` OCaml/C++ for perf-critical components — profile first, no blanket rewrite
- [ ] **#174** `[P1]` Market + reference data — used deliberately, not just Chainlink+Binance fallback
- [ ] **#175** `[P1]` Market regimes + order types — regime *switching* already falsified
      (0/12 cells, 0/75 slices, FDR); revisit only with genuinely new regime definitions
- [ ] **#176** `[P1]` Long/short + hedging via Kraken spot alongside Polymarket binaries —
      real-money cross-venue; same launch-gate discipline as existing live path (Claude never
      executes trades/transfers)
- [ ] **#177** `[P2]` Bidirectional sync with sibling repos — updates flow both ways (master →
      siblings and siblings → master); options include submodules, a private package index, or
      a GitHub Actions workflow (possibly an AI agent) opening sync PRs automatically

## Order-book re-test premise check (folded into #170, not a separate build)
Investigated re-testing f45/v7/v8 against real order-book depth instead of mid-price. Premise
was wrong: `replay_race.py` already fills at real recorded best-ask (not mid), fee-true,
validated against 12 live fills with positive slippage. No true L2 depth exists anywhere —
not locally (order-book fetch discards all but best level, `btc_bot/paper.py::_best_level`),
not in the public HF dataset (`aliplayer1/polymarket-crypto-updown`'s `orderbook` config,
23.6GB, is also top-of-book only). Venue books run 250-350 shares deep vs. the 5-share order
size — top-of-book was never binding. One legitimate cheap follow-up if revisited: confirm
`up_ask_size`/`down_ask_size` (already in schema, not currently SELECTed by `load_ticks()`)
were ≥5 on every f45 fill — needs the historical ticks DB, not present in this checkout.

---

Closed historical entries (issues #20-#144, 2026-06-10 to 2026-07-02) moved to
[`tasks/archive/todo_history.md`](archive/todo_history.md).
