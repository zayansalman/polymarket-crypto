# AI Research Loop (design — build once live fills accumulate)

The defensible way to "leverage AI" on this strategy. NOT a price predictor
(an LLM forecasting 5-minute BTC loses to latency bots reading the same
Chainlink feed). Instead: **AI as a research analyst over our own journal**,
inside a closed, human-gated loop.

## The loop

1. **Mine** — on a schedule (e.g. nightly), an agent reads the trade journal
   (`paper_positions`, `paper_ticks`, `live_orders`) and the
   recorded book/Chainlink archive, and finds where PnL concentrates:
   by hour-of-day, volatility regime, entry-price band, claimed-edge band,
   side (Up/Down), time-to-roll at entry, fill quality (live vs paper).

2. **Hypothesize** — it proposes concrete, testable changes: new entry
   filters, feature tweaks (e.g. a vol-regime gate), sizing adjustments.
   Each hypothesis is a parameter/filter delta, not a vibe.

3. **Backtest** — every proposal is replayed on the recorded archive
   **walk-forward, out-of-sample** (train window → test window, never
   in-sample), on a replay harness still to be built. Selection-bias and
   multiple-testing caveats are reported (N hypotheses tried → expected
   false positives).

4. **Surface** — only proposals that clear OOS with a CI excluding zero are
   written up for the operator, WITH numbers (ROI, n, win rate, drawdown,
   per-half stability). Everything else is logged and dropped.

5. **Approve** — the operator decides. Approved changes update config
   (e.g. `PAPER_ENTRY_EDGE_MAX`); nothing auto-applies to live.

**AI proposes, human disposes.** The loop never changes a live setting on its
own — that is the line between adaptive research and curve-fitting yourself
into a blow-up.

## Why human-gated, not autonomous

With a $30–40 bankroll and a few trades/hour, the live sample is tiny and
noisy. Autonomous RL / auto-tuning would overfit to that noise and amplify
variance. The operator gate + mandatory OOS validation is the overfitting
firewall.

## Prerequisites before building

- Enough live fills to measure the paper-vs-live fill gap (the one thing the
  archive can't simulate). Target: ≥ ~50 live settles.
- The adaptive risk controller (#36) that provided an edge-decay auto-pause
  was archived with the v0 strategy on 2026-09-13 (`docs/archive/v0-strategy.md`).

## Status

Designed, not built. Build after the first live soak yields real fills.

*Archived 2026-09-13 with the v0 strategy (`docs/archive/v0-strategy.md`); kept
here as history.* A human-gated **v0** of steps 2–5 existed for the backtest grid:
`polymarket_bot/params_propose.py` runs `polymarket_bot.backtest.build_report`, picks the
recommended params, and writes them to `$DATA_DIR/params_proposed.json` for
operator review — it never auto-applies. `polymarket_bot/params_apply.py` then promotes
proposed → active (`params_active.json`, which the live bot reloads each window
roll) only when invoked with `--confirm`. This is the "AI proposes, human
disposes" pattern in miniature over the existing grid search; the full loop above
adds the journal-mining (step 1) and walk-forward OOS replay (step 3) on top.
