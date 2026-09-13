# Layer 3 — Chronos Time-Series Ensemble (design)

**Status:** sketch only. Module stub at `polymarket_bot/chronos_signal.py`. No Hugging
Face dependency added. Disabled by default. Live activation requires explicit
operator approval AND an OOS replay-archive validation step that is not yet
built.

## Why

Layer 1 (isotonic calibration) reduced Brier from 0.275 to 0.242 on the existing
log-normal pricing model — a real lift. Layer 3 asks: is there a second,
independent probability source that can be ensembled with the calibrated
baseline to push Brier further?

The candidate is [**Chronos-Bolt-Small**](https://huggingface.co/amazon/chronos-bolt-small)
(Amazon, ~90M params, on Hugging Face Hub). It's a time-series foundation model
fine-tuned on broad time-series corpora. It takes a context series and produces
a probability distribution over future values. For our case: feed 60 minutes of
1-second BTC closes, ask for the 5-minute-ahead distribution, integrate the
density above the window reference to get P(close ≥ reference) = `fair_up_chronos`.

## Why behind a flag and not just turned on

- Foundation models on 5-minute crypto direction are unproven. The baseline
  (Black-Scholes + tie mass + isotonic calibration) is strong and cheap. A
  ~90M-param model that can't beat it on the replay archive earns zero weight
  and is therefore disabled — that's the whole point of A/B-via-replay.
- Operational cost: Chronos-Bolt-Small at CPU runs in ~1-3s for a single
  context; at 5s tick interval that's tight. GPU eliminates the latency but
  adds a hardware dependency we don't otherwise have.
- Failure modes: a broken Chronos inference would currently fall through to
  the calibrated baseline (good), but only if the integration treats Chronos
  as an optional second source — not a replacement.

## Integration shape

```
                           ┌──────────────────────────┐
   Binance 1s closes ────▶ │ chronos_signal.predict() │ ──▶ fair_up_chronos
                           └──────────────────────────┘
                                      │
   Black-Scholes + tie ──▶ fair_up_raw           ┌─────────────────────────┐
                                      ├────────▶ │ apply_ensemble()        │ ──▶ fair_up_ensemble
   isotonic calibrator ──▶ fair_up_cal          │  weights from rolling   │
                                      └────────▶ │  Brier over recent N    │
                                                 └─────────────────────────┘
```

**Default behavior (Chronos inactive):** `fair_up_ensemble = fair_up_cal`.
Identity. `predict()` returns `None`, so `apply_ensemble()` short-circuits to
the calibrated baseline and the Chronos path is never invoked.

**Active behavior:**
```
w_cal     = brier_weight(brier_cal_recent)
w_chronos = brier_weight(brier_chronos_recent)
fair_up_ensemble = (w_cal * fair_up_cal + w_chronos * fair_up_chronos) / (w_cal + w_chronos)
```
A model with worse rolling Brier gets less weight automatically. A new Chronos
deployment starts at weight 0 until enough samples accumulate.

## Activation gate (REQUIRED before any live weight)

1. **Replay-archive OOS validation**: run `tools/chronos_replay_eval.py`
   (to-be-built) over the recorded archive, splitting into in-sample / OOS
   windows. Chronos must:
   - Produce a probability for ≥ 90% of opportunities (no NaN, no timeout).
   - Achieve Brier < `fair_up_cal` Brier on the OOS set with statistical
     significance (paired bootstrap, p < 0.05).
2. **Latency budget**: 95th percentile inference time < 2.5s on the
   target deployment.
3. **Operator sign-off**: the operator writes the activation marker
   `data/chronos_active.json` (fields: `activated_at`, `weight_cap`,
   `samples_required`, `model_id`). `chronos_signal.load_activation()` /
   `is_active()` read it; its absence means OFF. There is no CLI to flip this
   today — the stub never produces a signal regardless.
4. **Initial weight cap**: First live deployment caps Chronos weight at 0.30
   until 200 closed-trade Brier samples accumulate, then unlocks to
   pure Brier-weighted.

Until all four are satisfied, the live bot path is unchanged.

## File layout (planned)

- `polymarket_bot/chronos_signal.py` — the module: `predict(window_closes, reference_price)`,
  `apply_ensemble(fair_up_cal, fair_up_chronos, *, weight_cal, weight_chronos)`,
  `is_active()` / `load_activation()`. **Implemented as a stub today** —
  `predict()` returns `None` (no signal), so `apply_ensemble()` is identity and
  `is_active()` is `False` unless the marker file exists.
- `tools/chronos_replay_eval.py` — OOS validator. NOT YET WRITTEN.
- `data/chronos_active.json` — operator activation marker. ABSENCE = OFF.

## What's in this PR

Only the design doc + the stub module + tests for the stub. No HF
dependency, no model download, no live behavior change. The next PR (if and
when this is greenlit) adds:
- `transformers` and `chronos-forecasting` to optional `[chronos]` extras.
- Model load (lazy, singleton), inference wrapper, latency monitor.
- The OOS replay evaluator.

## Why we're not just doing it now

Per project policy (CLAUDE.md / `tasks/todo.md`): no live-param change
without OOS validation + operator sign-off, and "RL/foundation-model
auto-tuning on a live $30-40 bankroll overfits to noise."

The calibrated baseline at Brier 0.242 is the bar. Anything that comes in
heavier than that needs to clear that bar with evidence.
