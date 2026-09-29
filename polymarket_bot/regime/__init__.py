"""Market regime overview: volatility, volume, move, jumps, book, session — advisory only.

Answers "what kind of market is this right now, and which strategy family's
mechanics are feasible in it?" from sources the lab already talks to (Binance spot
klines, the Polymarket Gamma market record, and the loop's own tick journal),
so the operator can decide what to run — and so a future strategy router has
a persisted, versioned input to be backtested against.

Package shape mirrors :mod:`polymarket_bot.daily`:

* :mod:`~polymarket_bot.regime.types` — frozen data contracts.
* :mod:`~polymarket_bot.regime.features` — pure feature math over bars.
* :mod:`~polymarket_bot.regime.classify` — a-priori bands + strategy-fit rules.
* :mod:`~polymarket_bot.regime.sources` — the I/O fetchers.
* :mod:`~polymarket_bot.regime.ledger` — ``regime_snapshots`` persistence.
* :mod:`~polymarket_bot.regime.monitor` — the always-on scan loop.

Nothing here is on the trading path: no strategy reads a regime verdict, no
order is placed, and the loop's entry decision is untouched. Regime
*switching* within one strategy family was falsified on this venue
(docs/archive/FINDINGS.md §3–4); this package is cross-family, advisory,
and human-gated by construction.
"""
