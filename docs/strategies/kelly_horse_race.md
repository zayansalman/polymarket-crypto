# Kelly horse-race

<!-- BEGIN GENERATED:strategy -->
| | |
|---|---|
| Name | Kelly horse-race |
| Key | `kelly_horse_race` |
| Status | running now |
| Switch | `kelly_horse_race` on the MY STRATEGIES card |
| Code | `ems/kelly_horse_race/` — 11 files (`ems/execution/controls.py`, `ems/execution/endpoints.py`, `ems/execution/gate.py`, `ems/execution/queue.py`, `ems/execution/resting.py`, `ems/execution/tape.py` shared) |
| Code fingerprint | `ffd0d90d7706` |
<!-- END GENERATED:strategy -->

## At a glance

### Concept

Zayan's idea (2026-09-22): read the last hour's direction and volatility as a probability, then split the 15-minute stake between Up and Down by it. 100% up with no volatility means all Up. 80% up means mostly Up with some Down. High volatility means more mixing, still weighted toward the hour's direction. In his words, a smarter random bet, or a smarter dice roll. He named it Kelly horse-race and asked for it to be built "as the paper would do it and with randomness", with the notional drawn at random between the venue's minimum and $5.

### Main assumption

The Chainlink TWAP-60s print moves like Brownian motion with a drift. Over the rest of a 15-minute window, the drift is the last hour's log return carried forward, and the volatility is the last hour's realised volatility. A 15m window settles Up when the TWAP-60s print at the close is at least the print at the open (Gamma's priceToBeat). The chance of Up is the chance that print ends at or above the open.

### The maths

The chance of Up, with $X$ the TWAP-60s print now, $K$ the print at the open, $r_{60}$ the last hour's log return, $\sigma_h$ its realised volatility per square-root hour and $\tau$ the hours left:

$$P(\text{Up})=\Phi\!\left(\frac{\ln(X/K)+r_{60}\,\tau}{\sigma_h\sqrt{\tau}}\right),\qquad r_{60}=\sum_{i=1}^{60}\ln\frac{c_i}{c_{i-1}},\qquad \sigma_h=\sqrt{\sum_{i=1}^{60}\Big(\ln\frac{c_i}{c_{i-1}}\Big)^2}$$

The side is a die: draw $u_1$ uniform on $[0,1)$ and buy Up if $u_1 < P(\text{Up})$, else Down. Over many windows the stake lands on each side in proportion to its chance, Kelly's horse-race split $b = p$.

The size: draw $u_2$ uniform on $[0,1)$ and pick one share count, in hundredths of a share, from the venue's minimum order $m$ up to $\lfloor 5/p \rfloor$ at the bid price $p$, each equally likely. At a fixed price that is a uniform notional between $m\,p$ and \$5.

### How it works

Once per BTC 15m window, as soon as its inputs are in: $K$ and $X$ from the TWAP-60s stream, the last hour from sixty 1-minute Binance returns, then $P(\text{Up})$ and the die for the side. It reads that side's order book and rests one passive limit buy at the best bid, behind the shares already resting there, with the random size. The same order goes to paper and, when armed, live, each through its own risk gate leg. From 60 s before the close anything still resting is cancelled. Once the venue calls the window, each filled share makes $1 - p$ if its side won and $-p$ if not, with no fee.

### How it was derived

The research note (Claude, 2026-09-22; 51 arXiv papers, each checked against its arXiv page) found the two readings of the idea. Splitting the stake in one window by the probability is Kelly's horse-race rule, $b = p$. Rolling a weighted die each window is probability matching. At \$5 a window the split cannot happen inside one window, because each side needs the venue's minimum order, so the die spreads it across windows instead. The chance of Up is the digital-option formula with the last hour's move as the drift. The settlement was checked twice: in 120 of 120 consecutive windows the outcome equals `priceToBeat(next) >= priceToBeat(this)`, and one priceToBeat matched the RTDS TWAP-60s print to every digit. The design (Claude, 2026-09-26) keeps the maths as written and does not fit it to outcomes.

### References

- Research note: `tasks/2026-09-22-kelly-horse-race-research.md`; checks in `tools/kelly_horse_race/checks.py` and `price_to_beat_chain.py`
- Design: `docs/superpowers/specs/2026-09-26-kelly-horse-race-design.md`
- Code in the app: `ems/kelly_horse_race/` (`maths.py` is the maths); the venues, risk gate and fill model it shares with every strategy: `ems/execution/`
- Kelly, *A New Interpretation of Information Rate*, Bell System Technical Journal 1956
- Horse-race betting and the split $b = p$: [arXiv 1901.06278](https://arxiv.org/abs/1901.06278), Prop. 2 and Prop. 9
- A binary option priced from drift and volatility: [arXiv 2606.19517](https://arxiv.org/abs/2606.19517)

## What it does

Once per BTC 15m Up/Down window it reads the chance of Up as a binary option on the Chainlink TWAP-60s print: the price now against the price to beat, with the last hour's move carried forward as the drift and its volatility as the spread. A die weighted by that chance picks the side, so over many windows the stake splits between Up and Down in proportion to their chances (Kelly's horse-race rule). It rests one passive limit buy at that side's best bid, sized at random between the venue's minimum order and the notional cap, on paper always and on live when armed.

It never crosses the spread and never holds both sides of a window: there is one order per window per mode, on one side.

## How it was formed

- **Idea and name:** Zayan (operator), 2026-09-22. Split 15m Up/Down stakes by a probability built from the 1h direction and volatility; "a smarter dice roll".
- **Research:** Claude, 2026-09-22, `tasks/2026-09-22-kelly-horse-race-research.md`. It covers the Kelly horse-race rule and probability matching, the digital-option probability, whether the last hour predicts the next 15 minutes, short-horizon volatility and how the 15m market settles.
- **Build instructions:** Zayan (operator), 2026-09-22: "as the paper would do it and with randomness"; "completely randomise the notional between min shs required and 5$"; one execution layer shared by every strategy.
- **Design:** Claude, 2026-09-26, `docs/superpowers/specs/2026-09-26-kelly-horse-race-design.md`.
- **Built on the one-package tree:** Claude, 2026-09-26, after develop removed every strategy but Fade 1h Momentum on 15m. The operator chose then to build the spec's live leg, to share fade's paper fill model, and to keep everything on one branch.

## How it works

### Inputs

| input | where it comes from |
|---|---|
| The window | the market-data hub's current BTC 15m market: bounds, Up and Down tokens, market id (Gamma `/markets?slug=` when the hub lacks it) |
| $K$, the price to beat | the Chainlink TWAP-60s print observed at the window's first second; if the hub does not hold it, Gamma `eventMetadata.priceToBeat`, asked every 30 s |
| $X$, the price now | the newest TWAP-60s print, observed within the last 5 s |
| $r_{60}$ and $\sigma_h$ | sixty 1-minute log returns from 61 completed Binance BTCUSDT candles |
| $\tau$ | hours left in the window |
| The book | CLOB REST `/book` for the chosen token: best bid, the shares resting there, best ask, tick size, minimum order |

### One decision per window

The decision is made on the first pass that has every input, and never again for that window. The draws $u_1$ and $u_2$ come from `random.SystemRandom` once per window and are kept while inputs are awaited, so waiting never re-rolls the die. Everything the maths saw is stored with the decision.

There is no order, and the window records why, when:

- nobody is bidding for the chosen side (`no_bid`);
- the best bid meets the best ask (`book_locked`);
- the minimum order costs more than the notional cap (`min_order_over_cap`);
- an input cannot arrive in time: still missing 2 minutes before the close, or the price to beat neither held nor published by then.

### The order

One passive limit buy at the chosen side's best bid, which is below the ask by construction, joining the queue behind the shares already resting at that price. It is good till the window's end; the venue stops such an order 60 s before its expiry, and the strategy cancels anything still resting from then too.

The same order goes to every endpoint that is on when the decision is made:

- **Paper** is always on unless the kill switch file exists. A paper order fills only from the real taker trade tape, once the shares that were ahead of it have traded: for this one order, filled $= \min(\text{size}, \max(0, \text{crossed} - \text{queue ahead}))$.
- **Live** places nothing until a live venue is built and armed; the card says which.

Each endpoint has its own risk gate leg (kill switch, daily loss halt, per-trade cap, daily notional cap; the "Risk" knobs in SETTINGS). A blocked order is recorded against its mode with the reason. The live per-trade cap starts at \$3, below this strategy's \$5 notional cap, so on live every draw above \$3 is blocked, and recorded as blocked, until the operator raises it.

### Bookkeeping, every pass

Fills are brought up to date whatever the switch says. An order that can fill no more gives its unfilled notional back to its gate leg. Once every order of an ended window is final and the venue's order-book service calls the window, each filled share is settled at its own price: $+ (1 - p)$ if its side won, $-p$ if not, with no fee (a passive order never takes). Windows with no order still get their result, so the record shows how $P(\text{Up})$ did in every window.

Switch off, or the kill switch file present: whatever rests is cancelled, nothing new is decided, and fills and settlement go on.

## Parameters

| knob (SETTINGS) | default | what it does |
|---|---|---|
| Largest order (`kelly_horse_race_max_notional_usd`) | \$5 | the cap the random size is drawn up to |
| Pass interval (`kelly_horse_race_poll_interval_seconds`) | 5 s | how often the loop runs |
| Paper / Live risk knobs ("Risk" group) | see SETTINGS | the gate legs every strategy's orders pass |

Fixed in code: the decision cutoff (2 minutes before the close), the cancel lead (60 s before the close), the price-now age limit (5 s).

## Worked examples

The maths on hand-picked numbers, from `ems/kelly_horse_race/maths.py`. These show the formula's behaviour; they are not market decisions. Worked examples from real windows are made by `tools/kelly_horse_race/examples.py` (it reads Gamma and Binance) and are not committed yet.

With $K = 100{,}000$ and 15 minutes left ($\tau = 0.25$):

| $X$ | $r_{60}$ | $\sigma_h$ | $P(\text{Up})$ | reading |
|---|---|---|---|---|
| 100,000 | 0 | 0.004 | 0.500 | no move, no lean |
| 100,000 | +0.002 | 0.004 | 0.599 | the hour rose 0.2%: leans Up |
| 100,000 | −0.002 | 0.004 | 0.401 | the mirror image |
| 100,000 | +0.002 | 0.008 | 0.550 | twice the volatility: pulled toward 0.5 |
| 100,000 | +0.002 | 0.016 | 0.525 | twice again |

With 10 minutes left ($\tau = 1/6$), $r_{60} = +0.002$ and $\sigma_h = 0.004$: at $X = 100{,}050$ the chance of Up is 0.695, and at $X = 99{,}950$ it is 0.459.

The size at a bid of 0.53 with a 5-share minimum and the \$5 cap: from 5.00 to 9.43 shares, \$2.65 to \$5.00. $u_2 = 0$ gives 5.00 shares and $u_2 = 0.5$ gives 7.22 shares (\$3.83).

## Known weaknesses

- **The die costs growth.** The research note (section 4) shows a weighted die picks the winning side less often than always taking the likelier side (68% against 80% at $p = 0.8$). Its expected profit per dollar matches a split, but its log growth is lower. It is built with the die because the operator asked for randomness.
- **The drift is noisy, and its sign is contested.** One hour of data estimates the drift with an error about as large as a typical hourly move (arXiv 2606.08209). Over 15 minutes, crypto leans slightly toward reversal (arXiv 2608.21888), while this maths carries the hour forward.
- **The bar is the market price, not 50%.** On Polymarket BTC 15m, the market's own mid scored better than a 43-feature model and a drift-only probability (arXiv 2607.26245). The maths here is not fitted to outcomes, and the records show how its chance of Up compares with the settled results.
- **Fat tails.** One-hour BTC returns have excess kurtosis near 30 (arXiv 2010.07402), so a normal-curve chance needs checking against outcomes.
- **Fills at the bid.** A buy at the best bid fills when sellers come to it, which is more likely when the price is moving against it. The paper fill model counts only real trades through the queue ahead, so it does not flatter this.

## Sources

- `tasks/2026-09-22-kelly-horse-race-research.md` — research note, 51 arXiv papers and the settlement checks (Claude, 2026-09-22)
- `tools/kelly_horse_race/checks.py` — the numerical checks; `tools/kelly_horse_race/price_to_beat_chain.py` — the 120-of-120 settlement check
- `docs/superpowers/specs/2026-09-26-kelly-horse-race-design.md` — the design (Claude, 2026-09-26)
- Kelly, J. L. (1956), *A New Interpretation of Information Rate*, Bell System Technical Journal 35(4)
- [arXiv 1901.06278](https://arxiv.org/abs/1901.06278) — horse-race betting, $b = p$ (Prop. 2), and holding no cash when the sides cost under \$1 (Prop. 9)
- [arXiv 2606.19517](https://arxiv.org/abs/2606.19517) — a binary option's probability from drift and volatility
- [arXiv 2606.08209](https://arxiv.org/abs/2606.08209), [arXiv 2608.21888](https://arxiv.org/abs/2608.21888), [arXiv 2607.26245](https://arxiv.org/abs/2607.26245), [arXiv 2010.07402](https://arxiv.org/abs/2010.07402) — the weaknesses above

## Changelog

- 2026-09-26 · `ffd0d90d7706` · First build: the maths, the inputs, the ledger and the runner, on the shared resting-order layer; paper always, live not built yet.
