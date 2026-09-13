# Archived: v0 BTC 5-minute strategy (2026-09-13)

The operator shut down the v0 strategy to make room for new ones. The trading
loop still runs (market discovery, Chainlink feed, order books, journaling,
settlement of anything already open), but **no strategy is loaded, so it never
enters** — in paper or live. Every tick journals `skip: no strategy loaded`.

## What was removed

| Piece | Where it lived |
|---|---|
| Entry gates on the loop: edge band 0.045–0.07, confidence ≥ 0.50, entry price ≥ 0.50, time-left cutoff, confidence sizing | `polymarket_bot/paper.py:_build_snapshot` → `strategy.signal_from_executable_edges`; their SETTINGS card knobs (`runtime_knobs.py`) |
| Model picker (which roster model the loop trades) | `paper.py:_resolve_active_model`, CONTROLS card, `/api/runtime-config key=active_model` |
| Edge-decay auto-pause | `polymarket_bot/adaptive.py`, `tools/clear_auto_pause.py`, `AUTO_PAUSE_*` env knobs, the SETTINGS card's Auto-pause group, ribbon pause chip |
| Probability calibration | `polymarket_bot/calibration.py`, `calibration_fit.py` |
| Param tuner (propose → apply) | `polymarket_bot/params.py`, `params_propose.py`, `params_apply.py` |
| Dashboard | STRATEGY card (`panels/strategy.py`), GATES column of the decision engine |
| Tests | `tests/unit/test_adaptive.py`, `test_calibration.py`, `test_params.py` |

## What stayed

- **Live-path safety gates** (`polymarket_exec/execution/gate.py:RiskGate` —
  loss halt, per-trade and bankroll caps, slippage guard, kill switch). A new
  strategy's entries still pass through them.
- **Shared pricing math** in `polymarket_bot/strategy.py` — the shadow roster,
  daily altcoin scanner, backtest and replay tools use it; the roster's entry
  thresholds are the fixed `config.PAPER_ENTRY_*` defaults. The loop still
  journals fair value and executable edge as market observations.
- The shadow forward-tester and the daily altcoin scanner.

## Restoring any of it

Everything is preserved at the git tag `archive/v0-strategy`. The last
versions — after the SETTINGS card moved these knobs into `runtime_knobs` —
are at commit `d881ad0` (the develop tip this was merged onto):

```bash
git show archive/v0-strategy:polymarket_bot/adaptive.py
git checkout archive/v0-strategy -- polymarket_bot/params.py
```

## Plugging in a new strategy

The decision slot is the signal block in `polymarket_bot/paper.py:_build_snapshot`
(look for `NO_STRATEGY_REASON`). Set `side`, `confidence`, `notional`, `reason`
there; everything downstream (sizing to shares, RiskGate, paper/live execution,
settlement) is unchanged and shared by both modes.
