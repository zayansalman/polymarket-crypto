# Timeline — thirty days from first tick to final verdict

> **Historical record, June–July 2026 research phase — superseded by the
> 2026-08-04 reopen, kept for reference.** "Final verdict" and "project closed"
> below describe the state of the BTC-only signal-race program on 2026-07-10,
> not the current project. See `tasks/todo.md` for current status.

The short version: build (June) → live losses traced to a booking bug → venue-true
re-accounting → pre-registered restart → an ablation race that kept killing its own
leaders → one week of agent-operated iteration → a selection-free negative → clean close.

| Date (2026) | Event |
|---|---|
| **Jun 11** | Tick journaling begins — the quote history that later powers the replayer. |
| **Jun 15** | First live session. Early live PnL looks flat-to-positive. |
| **Jun 15–24** | Live era: 333 buys. Books show a small loss; shadow race runs 6–8 model variants. Operator flips the live model to `down_skeptic_drift_v6` after a hot streak — it's the worst model. |
| **Jun 22–23** | First nulls land: regime attribution finds 0/12 significant cells; "no edge demonstrated" enters the record. Win-rate-as-target explicitly rejected (91%-WR model was losing money). |
| **Jun 25** | Bot dies on a boot-reconciliation crash. Race freezes ~7 days underpowered. |
| **Jul 2** | **Postmortem** (`POSTMORTEM_2026-07.md`): venue Data-API reconciliation reveals the live books were **fee-blind** — true bot-era PnL **−$17.24**, fees > 100% of the loss. Fee-true booking lands (#133). Roster surgery: v3–v6 retired; clean ablation (v0 ⊃ v2/v8 ⊃ v7) restarts in paper with a pre-registered deploy bar and sunset date. |
| **Jul 2** | Tick-replay backtester ships (#144): outcome labels 564/564 vs ground truth; reproduces the recorded shadow ledger exactly. v7's replay CI clears zero — the race must confirm. |
| **Jul 6–7** | Operator live-probes v7 overnight against protocol: 16 fills, −$4.07, ended by the trailing daily halt. Probe's one yield: fill fidelity at 5-share size is good (positive slip vs shadow). |
| **Jul 7** | **Agent ops-loop chartered** (`tasks/race_loop.md`): 6-hourly assess→build→log with binding guardrails. Baseline logged; deploy-bar math shows the race needs weeks, not days. |
| **Jul 8** | Loop iteration 1: pre-registered replay (#149) finds **f45** (freshness ≤45s) — OOS CI [+0.275, +0.887], the strongest candidate the project ever had. Spec frozen in PR #152. **Every tick recorded after this commit is selection-free.** |
| **Jul 8–10** | Loop iterations 2–8 ship the instrumentation: `race_status` CLI (#150), honest feed labels (#151), silent-stop alerts (#138), vol/basis regime columns (#122), tick-cadence stall detection (#157), maker/taker telemetry — 23% maker share (#137), deploy-bar min-n guard, `forecast_journal` pilot tool (#162). Meanwhile the race keeps regressing: v7 collapses, then v8; a Chainlink feed flap silently throttles accrual for 6h (caught, filed, fixed). |
| **Jul 9** | Operator grants unblock the gated moves: f45 joins the shadow roster (#155). |
| **Jul 10 (early)** | Operator switches the engine to v8 after its best day; it halts within 76 minutes (−$6.30). Switching-vs-holding simulated on the race's own data: switching loses. Regime-switching formally rejected; regime *measurement* kept. |
| **Jul 10, 11:11 UTC** | Process relaunched on current code (operator grant): f45 accrual finally starts; all merged fixes go live and are verified in production. |
| **Jul 10, ~13:00** | Wind-down decision: the 6h agent loop is retired (its token cost exceeded the strategy's ceiling); verdict rule amended to **deploy-if-clears / archive-if-fails**. |
| **Jul 10, 19:00 UTC** | **Final verdict by simulation** (operator-directed): post-freeze segment (≥Jul 9, zero selection contamination) is **−$0.41/trade over n=37 at WR 48.6%**; the whole fresh family flipped; the live f45 book agrees (−$3.79/n12, replay-consistent 12/12). **FAILS the sign-consistency requirement → bot stopped, live never re-enabled, project closed.** |
| **Jul 10–11** | Archive: showcase README, findings/architecture/timeline docs, MIT license, v1.0.0 release, repo archived. Final ledger: 2,924 shadow rows, ~100k ticks, 828 tests green from a clean-venv install. Real-money total: **−$19.35** — the full cost of a definitive answer. |

**The pattern that repeats through the whole table:** every apparent edge — v6, v7, v8,
f45; night hours; mid-vol; selectivity slices — looked real on the data that suggested it
and died on the first data that didn't. The project's contribution is that its machinery
was built to notice, each time, before size followed.
