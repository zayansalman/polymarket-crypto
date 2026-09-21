# Slow-market forecasting pilot

<!-- BEGIN GENERATED:strategy -->
| | |
|---|---|
| Name | Slow-market forecasting pilot |
| Key | `forecast_journal` |
| Status | offline only |
| Switch | none — nothing to turn on |
| Code | `tools/forecast_journal.py` |
| Code fingerprint | `29771c4880cb` |
<!-- END GENERATED:strategy -->

## What it does

A small journal for testing whether hand-made forecasts on slow, low-fee Polymarket markets beat the market price. You log a probability before looking at the book, type in the bid and ask, record the outcome later, and a report scores the forecasts against a pre-registered bar. Not one forecast has ever been logged, so it has produced no result.

## How it was formed

- **2026-07-10, Claude**, in a decision memo Zayan (operator) asked for after the BTC 5-minute results. The memo (`docs/archive/PIVOT_2026-07.md`, `4498f9e`) recommended "option C": test forecasting skill in slow, fee-free or low-fee categories, and pre-registered the pilot in its §4. Issue #162 was opened the same day with the same bar.
- **2026-07-10.** PR #164 (`33855a4`) shipped part 1: this journal and its scoring. The other two #162 items, a Gamma market screener and a fuller scoring report, were never built.
- **2026-07-11.** #162 closed when the project was archived; open items were not pursued.
- **2026-08-18.** `3a49af7` only updated the memo's path after it moved to `docs/archive/`. The pilot has never been started (`polymarket_bot/inventory.py`).

## How it works

- **Storage.** Its own SQLite file, `data/forecast_journal.db`, table `forecasts`, kept apart from the trading ledger. Commands: `add`, `resolve`, `list`, `report` (`main()`).
- **`cmd_add`** takes `--market`, `--category`, your forecast $q$ (strictly between 0 and 1), and optionally `--yes-bid` $b$, `--yes-ask` $a$ and `--resolves-by`. It refuses the `crypto` category and stores the category's taker fee rate $r$ from `CATEGORY_FEE` (Fee Structure V2, 2026-03-30): geopolitics and world 0, sports 0.03, finance, politics, mentions and tech 0.04, economics, culture, weather and other 0.05.
- **`cmd_resolve`** records the outcome, yes or no, once per row.
- **`cmd_report`** scores resolved rows:
  - `brier`: $(q-y)^2$, with $y=1$ for yes and 0 for no. The market's forecast is the mid $m=(b+a)/2$ (`market_implied`). Skill is the market's mean Brier minus ours; above zero means we beat the market.
  - `simulated_pnl_per_share` trades only when $|q-m|\ge 0.05$. If $q>m$ it buys YES at $a$; otherwise it buys NO at $1-b$. With entry price $x$, PnL per share is $1-x$ on a win and $-x$ on a loss, minus the fee $r\,x(1-x)$.
  - `mean_ci`: the mean of those PnLs and a 95% interval, $\bar x\pm1.96\,s/\sqrt{n}$.
  - Verdict: under 30 resolved rows it reports "underpowered". From 30 on, it passes only if skill is above zero and the lower end of the PnL interval is above zero.
- It places no orders and reads no market data; every price is typed in by hand.

## Known weaknesses

- Our Brier covers every resolved row, but the market's covers only rows with a quote, so skill is not like for like when some rows have no quote.
- The 30-row bar counts all resolved rows, including ones with no quote or no trade, so the PnL interval can rest on far fewer than 30 trades.
- The universe filter in the pre-registration (resolves within 60 days, top-of-book depth at least $500, spread at most 3c) is not enforced; the screener that would apply it was never built.
- "Forecast before price" is on trust. The tool only timestamps the entry.

## Sources

- `tools/forecast_journal.py`; `polymarket_bot/inventory.py` (record line).
- `docs/archive/PIVOT_2026-07.md` §4 — the pre-registration (hypothesis, universe, protocol, scoring, success bar).
- Issue #162 — scope and bar (opened 2026-07-10); comments of 2026-07-10 (part 1 shipped) and 2026-07-11 (closed at archive).
- PR #164 — feat(#162): forecast_journal — slow-market skill pilot, part 1 (journal + scoring), merged 2026-07-10.
- `4498f9e` 2026-07-10 — docs: pivot analysis 2026-07 — finish the race + slow-market skill pilot (#162); 5m microstructure rejected
- `33855a4` 2026-07-10 — feat(#162): forecast_journal — pre-registered slow-market skill pilot, part 1 (#164)
- `3a49af7` 2026-08-18 — docs: reopen-proof the rules and front door, archive the stale tombstone

## Changelog

- 2026-09-21 · `29771c4880cb` · Doc created.
