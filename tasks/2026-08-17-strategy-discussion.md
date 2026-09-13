# Strategy discussion — self-paced /loop, started 2026-08-17

Running scratchpad for a `/loop 5m` self-dialogue converging on a concluded
strategy recommendation across #180 (Sigma-Gap) and #182 (pairarb/copytrade).
Each tick reads this file, adds a dated entry, and either refines or concludes.
**No operational actions are taken by this loop** (no processes started/killed, no
branches merged, no live gates armed) — it produces a recommendation for the
operator to act on, per this project's own AGENTS.md convention that arming
anything live-adjacent is the operator's call, never an agent's.

Context this loop starts from: `tasks/todo.md`'s "Where we are (status check,
2026-08-17)" entry, committed as `3d93cb4`.

---

## Tick 1 (2026-08-17)

### New evidence gathered this tick

**1. Copytrade shadow PnL decomposed by asset / side / lag.** (`copytrade_min.db`,
`copytrade_doge.db`, queried live — figures grew since the prior status check as the
processes kept running, e.g. doge settled fills went 1,194 → 1,980.)

- **By asset (`min`, all-asset config):** bnb −$94.19, doge −$50.92, btc −$60.73
  are the drag; sol +$15.86 and xrp +$18.12 are net positive; eth ≈flat (−$2.62).
  Not a uniform failure — asset-dependent.
- **By lag bucket (`min`):** avg our-PnL/fill is **positive at 0–2s ($0.54) and
  3–5s ($0.02)**, and **negative at 6–10s (−$0.17), 11–30s (−$0.08), 30s+ (−$0.13)**.
  Directionally consistent with the documented slippage-vs-lag relationship
  (2.82¢ @0–2s vs 9.56¢ @11–30s) that motivated the onchain-feed fix — but the
  fast buckets are tiny (n=19, n=35) so this is suggestive, not proof.
- **Every row in both DBs has average lag 36–45s** — i.e. **100% of the shadow
  data collected so far predates the onchain-feed fix** (the fix is the literal
  last commit on the branch, `bf9a68a`). The `−$174 / −$307` figures in
  `tasks/todo.md` are not a read of the current code path at all; they're a read
  of the code path the operator explicitly said to get rid of.
- **doge-only config is messier**: 0–2s bucket underperforms 3–5s
  (−$0.26 vs −$0.31 avg — inconsistent with the lag story), and Down-side losses
  (−$421.83 on 1,065 fills) dominate while Up is net positive for us (+$114.15).
  doge was likely trending during this window (target's own Down-leg PnL is also
  negative, −$121.26) — some of the doge loss may be regime (a trending asset
  punishing a laggy copier disproportionately on the losing side), not pure lag.

**2. Why pairarb is dormant: its data dependency was never started.**
`btc_bot/pairarb/runner.py` (per the plan doc) reads `rec_books` + `rec_trades` —
tables written by `tools/venue_recorder.py`. Checked: **no `venue_recorder.py`
process is running, and no `rec_books`/`rec_trades` tables exist in any `data/*.db`
file.** This matches issue #182's own text verbatim ("no `rec_*` tables exist yet;
the recorder has never been run") — and it's still true. `pairarb_shadow.db`'s 25
`pair_windows` / 0 `pair_execs` is not a code problem or a negative early result;
it's an unstarted pipeline. **This is the single most fixable gap found so far**:
one background process, no capital risk, no live gate — pure data collection.

### Emerging picture

Effort has flowed toward the strategy the investigation's own numbers predicted
would be *weaker* (copytrade — forced taker role against a fee-free maker, 2.75¢
behind a ~0.55¢/leg edge) and away from the one predicted to be *stronger*
(pairarb — reproduces the target's actual, structurally sound maker edge). The
copytrade shadow's current negative reading can't yet be trusted either
direction: it's 100% pre-fix data, on the transport the operator explicitly
condemned. Meanwhile the strategy that should be getting the shadow-test budget
has none, for a boring infrastructure reason (recorder never launched), not a
strategic one.

### Draft recommendation (not yet final — refine next tick)

1. **Don't decide copytrade's fate on the current shadow numbers.** They're
   confounded with the exact defect the last two commits fixed. Needs a
   post-fix sample before either arming live or shelving it.
2. **Get pairarb actually running** — start the venue recorder, let
   `pairarb_shadow.py` accumulate real `pair_windows`/`pair_execs` data. This is
   the strategy the evidence favors and it currently has none.
3. **#180 (Sigma-Gap): recommend merge-and-shelve, not active pursuit.** The fix
   (`619b35b` + `6ef702e`) is already written, correct (independently
   re-derived this session), and cheap to land — merging it stops the branch
   from rotting and closes out the open issue honestly. But its own economics
   (2027-Q2 earliest verdict, ~$1–3k/yr central prize, ~3% prior) don't compete
   for attention against #182, which already has a measured, real edge signal
   (the target account itself) and a concrete near-term data gap to close. Don't
   resume recorder/pre-registration build work on #180 while #182 is live.

### Open for next tick

- [ ] Is there a reason (cost? rate limits? an explicit operator pause?) the
      recorder was never started, or was it just not gotten to yet? Check
      `tools/venue_recorder.py --help` / any config gating it, and re-check
      `tasks/todo.md` / issue comments for a stated reason before recommending
      "just start it."
  - [ ] Stress-test the "confounded by lag" explanation harder — the doge 0–2s
      vs 3–5s inconsistency is a wrinkle; is there a cleaner subset (e.g.
      restrict to sol/xrp, the two assets currently net-positive) that already
      shows a real signal even pre-fix?
- [ ] Any capacity/competition argument (analogous to the Sigma-Gap review's
      "prize" critique) for copytrade/pairarb specifically — what's the realistic
      annual ceiling here, given $7 median clips and a 5-share venue floor?
- [ ] Firm up the #180 merge-and-shelve call — check for merge conflicts against
      current `develop` before recommending it as "cheap."

---

## Tick 2 (2026-08-17, later) — CONCLUDED

Between tick 1 and this tick, a parallel session (the operator, working directly)
did real verification work and landed it in `tasks/todo.md` (now committed as
`e4bfbdb` — it was sitting uncommitted when this tick started; preserved it rather
than risk losing it again). That work supersedes and sharpens tick 1's reads in
three ways, checked directly against the code and running processes this tick:

**Correction to tick 1: pairarb does not need the recorder.** I assumed (from the
original plan doc) that `pairarb_shadow.py` reads `rec_books`/`rec_trades` written
by a separate `venue_recorder.py` process, and recommended starting the recorder.
**Wrong** — checked the shipped code (`tools/pairarb_shadow.py`'s own docstring +
its CLOB/Data-API calls): it polls the venue directly, self-contained, no recorder
dependency. The as-shipped architecture diverged from the original plan doc and
tick 1 was reading the stale version. The operator has already started it (PID
78172, all 6 assets) — correctly, just not for the reason I gave. It has 31
`pair_windows` / 0 `pair_execs` so far (just started; too early to read).

**Sharper than tick 1: the copytrade loss decomposes to two independent,
compounding invalidations, not one.** Tick 1 found the lag/staleness confound
(real, confirmed: 100% of shadow data is pre-onchain-fix). The operator's pass
found a **second, separate** one: both shadow configs are hard-pinned to a
constant 5-share size (`--max-shares 5 --min-shares 5`), while the 8-fill "92%
captured" sample that justified building the live executor used proportional
sizing. **The two numbers were never comparable in the first place** — the sign
flip could be substantially a methodology artifact, independent of whatever the
lag confound contributes. Two bugs, not one, both pointing the same direction:
no verdict on copytrade is currently possible, in either direction.

**More serious than tick 1 anticipated: the feed fix from `bf9a68a`/`1fc0b85`
never actually got wired in.** `copytrade_live.py` imports `open_feed`/
`FeedUnavailable` but never calls them; `assert_copy_live_allowed()`'s
`POLYGON_RPC_WSS` check is string-presence only, and the real `run()` trade-
detection loop is hardcoded to the same ~20s-stale Data API poll regardless. This
is not a shadow-data-quality issue — it's a **live-money safety gate that doesn't
gate what its own docstring says it gates**. If the operator sets
`POLYGON_RPC_WSS` (satisfying the boot check) and arms every other gate,
`--live` would still trade on the stale feed. This needs fixing (or its claim
downgrading) independent of any strategy decision — it's a correctness bug in
the refusal logic itself, not a shadow-test finding.

### Concluded strategy

**#180 (Sigma-Gap): merge-and-shelve, don't actively pursue.** Unchanged from
tick 1 — the fix is already written and independently re-verified twice now
(this review, and the prior session that wrote it). Land `619b35b` + `6ef702e`
into `develop` (check for conflicts first) to close #180 honestly and stop the
branch rotting. Do not resume recorder/pre-registration build work on it — its
own economics (2027-Q2 earliest verdict, ~$1–3k/yr central prize) don't compete
with #182 for attention.

**#182 copytrade: no verdict yet, and don't try to force one before three fixes
land, in this order:**
1. **Fix the fake gate first, regardless of what else happens.** Either wire
   `open_feed()` into `copytrade_live.py`'s actual `run()` loop (needs the
   token_id→market resolver noted in the operator's finding) or change
   `assert_copy_live_allowed()`'s check and docstring to stop claiming a
   protection it doesn't provide. This is a correctness/safety fix, not a
   strategy question — ship it before anyone even considers arming live,
   independent of whether copytrade turns out to have an edge at all.
2. **Re-run the shadow test with proportional sizing** (drop
   `--max-shares 5 --min-shares 5`) so the next read is comparable to the
   original 8-fill sample. This needs the operator to restart PIDs 9268/13259
   (blocked on explicit go-ahead, per the auto-mode classifier and this
   session's own boundary — an agent doesn't kill running processes without
   being asked to, in a live-trading-adjacent repo).
3. **Only after (1) and (2)**, let a fresh sample accumulate on the *actual*
   fast-feed, proportionally-sized code path, and re-read the capture rate
   before any live-arming decision. Until then `COPY_LIVE_CONFIRM` stays off.

**#182 pairarb: the strategy the evidence favors, now correctly running, too
early to read.** No action needed beyond letting PID 78172 accumulate
`pair_execs` — check back once it has produced some (the back-of-queue fill
model is conservative by design, so this may take longer than copytrade did to
produce a first read).

**Overall priority ordering, closing this loop:** fix-the-gate > get-pairarb-
data > re-test-copytrade-cleanly > merge-and-shelve-#180. Nothing left in this
loop is resolvable by more discussion — every remaining item is either blocked
on operator action (process restarts, the gate fix, the #180 merge) or blocked
on time (letting pairarb accumulate data). Stopping the loop here.
