# Kelly horse-race

<!-- BEGIN GENERATED:strategy -->
| | |
|---|---|
| Name | Kelly horse-race |
| Key | `kelly_horse_race` |
| Status | running now |
| Switch | `kelly_horse_race` on the MY STRATEGIES card |
| Code | `ems/kelly_horse_race/` — 14 files (`ems/execution/clob.py`, `ems/execution/controls.py`, `ems/execution/endpoints.py`, `ems/execution/gate.py`, `ems/execution/journal.py`, `ems/execution/live_control.py`, `ems/execution/queue.py`, `ems/execution/resting.py`, `ems/execution/tape.py` shared) |
| Code fingerprint | `0beb5b66be6d` |
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

Once per BTC 15m window, as soon as its inputs are in: $K$ and $X$ from the TWAP-60s stream, the last hour from sixty 1-minute Binance returns, then $P(\text{Up})$ and the die for the side. $K$ is only ever the print at the open: a window whose opening print the app did not see is skipped. It reads that side's order book and rests one passive limit buy at the best bid, behind the shares already resting there, with the random size. The same order goes to paper and, when the operator has armed LIVE, to the exchange as a post-only order, each through its own risk gate leg. Polymarket refuses a good-till-date order that expires less than 3 minutes ahead, so nothing is decided from 3 min 20 s before the close. From 60 s before the close anything still resting is cancelled. Once the venue calls the window, each filled share makes $1 - p$ if its side won and $-p$ if not, with no fee.

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
- **Built on the one-package tree:** Claude, 2026-09-26, after develop removed every strategy but Fade 1h Momentum on 15m. The operator chose then to build the spec's live leg, to share fade's paper fill model, and to keep everything on one branch. The live leg is recorded in AGENTS.md, "Live trading".
- **Review fixes:** Claude, 2026-09-27, from a review checked against the live venue and the app's first hour of paper records. $K$ comes only from the print at the open, since Gamma writes `priceToBeat` only when the window ends; nothing is decided inside Polymarket's 3-minute shortest expiry; the paper fill model counts trades printed a hair off their price level and reads a busy window's tape back from its start; live sizes reach the exchange whole, a 4xx refusal is a rejection, and a cancel of an order already closed is no error.
- **Second review fixes:** Claude, 2026-09-29, checked against the real trade tape; the operator chose to fix them. Paper orders now fill from the price levels each trade reached, not its average price, and a sale below the bid fills the order. Before, a buy waited for the whole queue ahead to trade, which missed the fills that come as the price runs through the bid. A live send with no reply is asked for by its order id, worked out before it is sent, in any state. The old search of the open orders missed an order that had already filled or been cancelled.

## How it works

### Inputs

| input | where it comes from |
|---|---|
| The window | the market-data hub's current BTC 15m market: bounds, Up and Down tokens, market id (Gamma `/markets?slug=` when the hub lacks it) |
| $K$, the price to beat | the Chainlink TWAP-60s print observed at the window's first second. It reaches the hub about 2 s late, so it is waited for up to 10 s after the open, and if a later print is held but not that one, up to 70 s, because the feed's history on a reconnect reaches about a minute back. There is no other source: Gamma writes `priceToBeat` only when the window ends (checked on 2026-09-27), after the last moment to decide |
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
- the app did not see the TWAP-60s print at the open, because it started after the open or the feed dropped then (`k_missing`). The window is skipped once no reconnect can bring that print back, 70 s after the open;
- another input is still missing 3 min 20 s before the close (the decision cutoff), and the reason ends "Still missing at the cutoff."

### The order

One passive limit buy at the chosen side's best bid, which is below the ask by construction, joining the queue behind the shares already resting at that price. It is good till the window's end; the venue stops such an order 60 s before its expiry, and the strategy cancels anything still resting from then too.

Polymarket refuses a good-till-date order whose expiry is less than 3 minutes ahead. Paper and live share one check that refuses an order expiring less than 3 min 5 s ahead (`too_late`), the extra 5 s for the time an order takes to reach the exchange, so paper never holds an order live could not have placed. The decision cutoff, 3 min 20 s before the close, leaves a decision started just before it 15 s for its reads. An order that passes rests for at least two minutes.

The same order goes to every endpoint that is on when the decision is made:

- **Paper** is always on unless the kill switch file exists. A paper order fills only from the venue's real trades, read price level by price level. Besides one record per trade at its average price, the venue lists one record for every resting order a trade filled, at that order's own price, so a sale that swept several bids shows how many shares traded at each price level. Two rules fill the order:
  - A sale at the order's price first works through the queue ahead, the shares already bid at that price when it was placed, and then fills the order: filled $= \min(\text{size}, \max(0, \text{sold at its price} - \text{queue ahead}))$.
  - A sale below its price fills the rest of the order, up to that sale's size, however much of the queue ahead has traded. Nothing trades below a bid while that bid still rests, so the shares ahead had traded or been pulled, and a real order at that price would have filled. These are the fills that come as the price runs through the bid.

  The venue writes a price as notional over size, so a trade at 24c can print as 0.2399999981. Prices are rounded to 5 decimals first, so such a trade counts at its price level. The list of trades is read back up to about 10,500 trades, more than the busiest window seen (7,017). The list with the per-order records holds about 2.8 records a trade, so it reaches back only about 3,700 trades. A stretch deeper than that, after a restart or when the tape falls several minutes behind in a busy window, is read one record per trade at the trade's average price. Such a trade at the order's price counts in full. Below it, the trade clears the queue ahead but fills nothing, since how much of it reached the order's price is unknown. The card then says fills there may be over- or under-counted. A trade newer than the venue's per-order records waits for the next read.
- **Live** is on only while the operator has armed it (AGENTS.md, "Live trading"): LIVE selected and clicked in the dashboard in this process (a saved LIVE or `BOT_MODE=live` is never consent), a wallet config that passes, and this strategy's switch on. The order goes to the exchange as a post-only good-till-date limit buy expiring at the window end (the exchange refuses it rather than let it cross), cancelled by id, and journaled to `live_orders`. Its size reaches the exchange exactly as drawn: the client library rounds sizes down to hundredths, so it is handed the size plus 0.000001. Its fills are the exchange's `size_matched`. An exchange refusal (an HTTP 4xx reply, such as a post-only order that would cross) is recorded as `rejected` with the exchange's words. A cancel of an order the exchange has already closed (expired at its stop, matched, or not found) is journaled as a cancel with the exchange's words, not as an error; the order's next status read says whether it filled. Selecting PAPER again cancels whatever still rests live; fills and settlement of live orders are still followed. The card says which condition is missing.
- **A live send with no reply, or a server error,** may still have reached the exchange, so it is never sent again. The order is built and signed before it is sent, so its order id, the order's hash, is known before it goes. The exchange is asked for that id at once and then every pass, and it answers for an order in any state: resting, filled or cancelled. Found, the order is followed like any placed order and counts toward the caps. Its fills come from `size_matched`, as for any live order. Until then it is recorded as `unknown`, and no new live order goes out.
  - 120 s after its window's stop (60 s after the window ends), an order the exchange still has no record of was never placed. It is closed as never placed.
  - If the exchange has still not answered by then, or there is no live venue to ask (after a restart), the order is given up. The card says to check the Polymarket orders page.
  - Either way the window then settles, paper included, and live orders resume.

  So an order that is never found holds new live orders until a minute after its window ends, and the next window's live order, usually decided in its first minute, is recorded as blocked. Paper is not affected. An order whose id could not be worked out is searched for among the exchange's open orders, matching the size to the hundredth, and given up at the stop if it is not found. If the exchange's reply gives an order a different id from the one worked out, the card says so. A placed order that cannot be written to the ledger is cancelled straight away.

Each endpoint has its own risk gate leg (kill switch, daily loss halt, per-trade cap, daily notional cap; the "Risk" knobs in SETTINGS). A blocked order is recorded against its mode with the reason. The live per-trade cap starts at \$3, below this strategy's \$5 notional cap, so on live every draw above \$3 is blocked, and recorded as blocked, until the operator raises it.

### Bookkeeping, every pass

Fills are brought up to date whatever the switch says. An order that can fill no more gives its unfilled notional back to its gate leg. Once every order of an ended window is final and the venue's order-book service calls the window, each filled share is settled at its own price: $+ (1 - p)$ if its side won, $-p$ if not, with no fee (a passive order never takes). Windows with no order still get their result, so the record shows how $P(\text{Up})$ did in every window.

Switch off, or the kill switch file present: whatever rests is cancelled, nothing new is decided, and fills and settlement go on.

## Parameters

| knob (SETTINGS) | default | what it does |
|---|---|---|
| Largest order (`kelly_horse_race_max_notional_usd`) | \$5 | the cap the random size is drawn up to |
| Pass interval (`kelly_horse_race_poll_interval_seconds`) | 5 s | how often the loop runs |
| Paper / Live risk knobs ("Risk" group) | paper: \$5 per trade, no daily cap, no halt; live: \$3 per trade, no daily cap, \$10 loss halt | the gate legs every strategy's orders pass |
| PAPER/LIVE (top bar) | PAPER | LIVE arms the live leg as described above |
| Wallet (`.env`) | none | `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`, `POLYMARKET_SIGNATURE_TYPE=2`; the `live` extra installed |

Fixed in code: the decision cutoff (3 min 20 s before the close), the shortest expiry an order may have (3 min 5 s ahead, from Polymarket's 3 minutes), the cancel lead (60 s before the close), the wait for the print at the open (10 s, or 70 s when a reconnect could still bring it), the price-now age limit (5 s), how long a live order sent with no reply is asked for (until 120 s after its window's stop).

## Worked examples

The app's first two paper decisions, on 2026-09-27, read from its ledger (`kelly_horse_race_decisions` ids 2 and 3, `kelly_horse_race_orders` ids 1 and 2) and run again through `ems/kelly_horse_race/maths.py`, which gives the same chance of Up, side and size to every digit. Both were paper only: LIVE was not armed. Times are UTC.

### How to read a decision

- **The gap to the open** is how far the price now $X$ sits from the price to beat $K$, as a log return, $\ln(X/K)$.
- **The drift** is the last hour's move carried forward over the time left, $r_{60}\,\tau$.
- **A typical move for the time left** is the last hour's realised volatility scaled to the time left, $\sigma_h\sqrt{\tau}$.
- **$z$** is the gap plus the drift, counted in typical moves. The chance of Up is the normal curve's $\Phi(z)$: 50% at $z = 0$, more than 50% when the gap and drift point up.
- **The die** $u_1$ picks Up when it lands under $P(\text{Up})$, Down otherwise. **The size draw** $u_2$ picks one share count, in hundredths of a share, between the venue's minimum and what the \$5 cap buys at the bid.

### Decision 2: BTC 11:00–11:15, the maths makes Down 56.9%, the die picks Down, a passive buy of 6.79 Down at 56c; Up won

> BTC's TWAP-60s print sat on the open, and the last hour had fallen 0.072%. Carried forward, that is 0.17 typical moves against Up, so the maths makes Up 43.1% and Down 56.9%. The die landed on 0.494, above 0.431, so Down. The app rested a passive limit buy of 6.79 Down at the 56c best bid, behind 20 shares. It filled 5 s later. The window settled Up: −\$3.80.

**The story**

- **Price to beat.** The Chainlink TWAP-60s print at 11:00:00 was \$84,845.21. That is $K$.
- **Price now.** The newest print, at 11:00:02, was \$84,845.89: \$0.69 above the open, +0.0008%. With 14 min 55 s left, the price was on the open.
- **The last hour.** Over the 60 one-minute Binance candles to 11:00, BTC fell 0.072% ($r_{60} = -0.000718$), with a realised volatility of 0.196% for the hour ($\sigma_h = 0.001958$).
- **The chance of Up.** Carrying the hour's fall forward over the 0.2487 h left gives a drift of −0.018%. A typical move for the time left is 0.098%. The gap (+0.0008%) and the drift (−0.018%) come to −0.017%, which is −0.17 typical moves ($z = -0.175$). The normal curve puts Up at 43.1% and Down at 56.9%.
- **The die.** $u_1 = 0.494$. Up needs a draw under 0.431, so the side is Down. The die picks Down 56.9% of the time here.
- **The book.** Down was 56c bid with 20 shares resting there, and 57c offered. The tick was 1c and the minimum order 5 shares. The market's mid put Down at 56.5c.
- **The size.** At 56c the \$5 cap buys at most 8.92 shares. With the 5-share minimum, there are 393 possible sizes from 5.00 to 8.92 shares (\$2.80 to \$5.00), each equally likely. $u_2 = 0.456$ picks the 180th: 6.79 shares, \$3.80.
- **The order.** A passive limit buy of 6.79 Down at 56c, placed at 11:00:07, behind the 20 shares already bid there. It was good till 11:15:00, so the venue would stop it at 11:14:00.
- **The fill.** Three sales into Down's bids reached the order. First, 2.22 shares at 55c at 11:00:08, a cent under the bid. Then 2.00 at 56c at 11:00:09. Then, at 11:00:12, a sale of 28.70 that swept 20.00 at 55c and 8.70 at 54c. Nothing trades below a bid while that bid still rests, so the first sale shows the 20 shares ahead at 56c had already traded or been pulled. The order took all 2.22 of that sale, then the 2.00 at 56c, then its last 2.57 from the sweep: in full at 11:00:12. The ledger, from the fill model of the day, has all 6.79 at 11:00:12, once the 20 shares ahead had traded at 56c or lower. Run again on the same tape with the fill model of 2026-09-29, the first 2.22 fill 4 s earlier. The size and the result are the same.
- **The result.** The TWAP-60s print at the close, 11:15:00, was \$84,885.12: \$39.92 above the price to beat (+0.047%). The window settled Up, so the 6.79 Down shares paid nothing: −\$3.80 (6.79 × 56c), settled at 11:25:49 when the venue's order-book service called the window.

| factor | value |
|---|---|
| window | `btc-updown-15m-1790506800`, 2026-09-27 11:00–11:15 UTC |
| decided | 11:00:04, 14 min 55 s left ($\tau = 0.2487$ h) |
| $K$, the TWAP-60s print at the open | \$84,845.21 |
| $X$, the print at 11:00:02 | \$84,845.89 ($\ln(X/K) = +0.0000081$) |
| $r_{60}$, the last hour's log return | −0.000718 (drift over the time left −0.000179) |
| $\sigma_h$, the last hour's realised volatility | 0.001958 (typical move for the time left 0.000976) |
| $z$ | −0.175 |
| $P(\text{Up})$ | 0.431 |
| $u_1$, the die | 0.494 → Down |
| Down book | 56c bid (20 shares) / 57c offered, tick 1c, minimum 5 shares |
| $u_2$, the size draw | 0.456 → 6.79 shares of 5.00 to 8.92 |
| order | passive limit buy, 6.79 Down at 56c, \$3.80, 20 shares ahead |
| fill | 6.79 shares, in full at 11:00:12 (the first 2.22 at 11:00:08 with the fill model of 2026-09-29) |
| close print (11:15:00) | \$84,885.12 → Up |
| P&L | −\$3.80 |

<sub>Check: Gamma's `priceToBeat` for this window, published after it ended, is 84,845.20585685875, and its `finalPrice` 84,885.12293279995: the same two prints the app recorded, to every digit.</sub>

### Decision 3: BTC 11:15–11:30, the maths makes Down 63.9%, the die picks Up, a passive buy of 6.68 Up at 48c; Up won

> BTC's print again sat on the open, and the last hour had fallen 0.144%. Carried forward, that is 0.36 typical moves against Up, so the maths makes Up 36.1% and Down 63.9%. The die landed on 0.159, under 0.361, so Up. The app rested a passive limit buy of 6.68 Up at the 48c best bid, behind 1,572.59 shares. It filled about a minute later, when Up traded a cent under the bid. The window settled Up: +\$3.47.

**The story**

- **Price to beat.** The TWAP-60s print at 11:15:00, \$84,885.12, the same print that closed the window before.
- **Price now.** The print at 11:15:03 was \$84,885.26: \$0.14 above the open, +0.0002%, with 14 min 55 s left.
- **The last hour.** BTC fell 0.144% over the hour to 11:15 ($r_{60} = -0.001440$), with a realised volatility of 0.200% ($\sigma_h = 0.002005$).
- **The chance of Up.** Carried forward over the 0.2486 h left, the fall is a drift of −0.036%. A typical move for the time left is 0.100%. The gap (+0.0002%) and the drift come to −0.36 typical moves ($z = -0.357$). The normal curve puts Up at 36.1% and Down at 63.9%.
- **The die.** $u_1 = 0.159$ is under 0.361, so the side is Up. The die picks Up 36.1% of the time here. Over many windows, that is how the stake splits between the sides in proportion to their chances.
- **The book.** Up was 48c bid, with 1,572.59 shares resting there, and 49c offered. The tick was 1c and the minimum order 5 shares. The market's mid put Up at 48.5c, against the maths' 36.1%.
- **The size.** At 48c the \$5 cap buys at most 10.41 shares. There are 542 possible sizes from 5.00 to 10.41 shares (\$2.40 to \$5.00). $u_2 = 0.310$ picks the 169th: 6.68 shares, \$3.21.
- **The order.** A passive limit buy of 6.68 Up at 48c, placed at 11:15:06, behind the 1,572.59 shares already bid there. It was good till 11:30:00, so the venue would stop it at 11:29:00.
- **The fill.** Sellers worked through the queue ahead at 48c: by 11:16:11, 1,147.26 of the 1,572.59 shares had traded there. Then, at 11:16:11, 20 Up traded at 47c, a cent under the bid. Nothing trades below a bid while that bid still rests, so the rest of the queue ahead had traded or been pulled, and the order filled in full from that sale. The ledger has the fill at 11:17:39, from the fill model before its 2026-09-27 fix, and the fill model of 2026-09-27 gave 11:17:02. Both waited for the whole queue ahead to trade at 48c or lower. Run again on the same tape with the fill model of 2026-09-29, it fills at 11:16:11, 88 s before the ledger's time. The size and the result are the same.
- **The result.** The print at the close, 11:30:00, was \$84,893.86: \$8.73 above the price to beat (+0.010%). The window settled Up, so the 6.68 Up shares paid \$6.68 against \$3.21 paid: +\$3.47 (6.68 × 52c), settled at 11:44:40.

| factor | value |
|---|---|
| window | `btc-updown-15m-1790507700`, 2026-09-27 11:15–11:30 UTC |
| decided | 11:15:05, 14 min 55 s left ($\tau = 0.2486$ h) |
| $K$, the TWAP-60s print at the open | \$84,885.12 |
| $X$, the print at 11:15:03 | \$84,885.26 ($\ln(X/K) = +0.0000016$) |
| $r_{60}$, the last hour's log return | −0.001440 (drift over the time left −0.000358) |
| $\sigma_h$, the last hour's realised volatility | 0.002005 (typical move for the time left 0.001000) |
| $z$ | −0.357 |
| $P(\text{Up})$ | 0.361 |
| $u_1$, the die | 0.159 → Up |
| Up book | 48c bid (1,572.59 shares) / 49c offered, tick 1c, minimum 5 shares |
| $u_2$, the size draw | 0.310 → 6.68 shares of 5.00 to 10.41 |
| order | passive limit buy, 6.68 Up at 48c, \$3.21, 1,572.59 shares ahead |
| fill | 6.68 shares; 11:17:39 in the ledger, 11:16:11 with the fill model of 2026-09-29 |
| close print (11:30:00) | \$84,893.86 → Up |
| P&L | +\$3.47 |

<sub>Check: Gamma's `priceToBeat` for this window is 84,885.12293279995 and its `finalPrice` 84,893.85601890941, the app's two prints to every digit.</sub>

### The maths on hand-picked numbers

From `ems/kelly_horse_race/maths.py`. These show how the formula behaves; they are not market decisions.

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

## Evidence so far

### The chance of Up at the open, 96 windows

Measured on 2026-09-27 at 12:00 UTC by `python tools/kelly_horse_race/examples.py --windows 96`. It covers the 96 resolved BTC 15m windows from 2026-09-26 11:30 UTC to 2026-09-27 11:15 UTC, each priced as the app prices it at the open: $X = K$, $\tau = 0.25$ h, and the last hour from Binance. $K$ is the window's Gamma `priceToBeat`, read after the window ended; it is the same print the app reads live. The book at the open is not in any public history, so this scores the chance of Up, not orders or fills.

| measure | value |
|---|---|
| Windows settled Up | 57 of 96 (59.4%) |
| The side the maths made likelier won | 41 of 96 (42.7%) |
| The die's side, expected wins | 45.5 of 96 (47.4%) |
| Brier score of $P(\text{Up})$ | 0.3025 |
| Brier score of always 0.5 | 0.2500 |

The Brier score is the mean squared gap between the chance given and the result (1 for Up, 0 for Down). Lower is closer to the results.

| $P(\text{Up})$ | windows | settled Up |
|---|---|---|
| 0.00 to 0.40 | 26 | 21 (81%) |
| 0.40 to 0.45 | 8 | 5 (62%) |
| 0.45 to 0.50 | 6 | 2 (33%) |
| 0.50 to 0.55 | 11 | 4 (36%) |
| 0.55 to 0.60 | 9 | 6 (67%) |
| 0.60 to 1.00 | 36 | 19 (53%) |

At the open the price is on the open, so the likelier side is the one the last hour pointed to; it won 41 of the 96 windows. The 26 windows the maths put under 40% Up settled Up 21 times.

### The app's first paper records

- **10:45–11:00 UTC, no decision.** The app started at 10:49:33, 4 min 33 s after the open, so it never saw the print at the open. The code then asked Gamma for `priceToBeat` every 30 s. It recorded `k_missing` at the cutoff, 10:58:04. Gamma first answered with that window's `priceToBeat` at 11:00:31, after the window had ended. So since 2026-09-27, a window whose opening print was missed is skipped 70 s after the open, once no reconnect can bring that print back.
- **11:00–11:15 and 11:15–11:30**: decisions 2 and 3 above, −\$3.80 and +\$3.47.

The card has the full record as it grows.

## Known weaknesses

- **The die costs growth.** The research note (section 4) shows a weighted die picks the winning side less often than always taking the likelier side (68% against 80% at $p = 0.8$). Its expected profit per dollar matches a split, but its log growth is lower. It is built with the die because the operator asked for randomness.
- **The drift is noisy, and its sign is contested.** One hour of data estimates the drift with an error about as large as a typical hourly move (arXiv 2606.08209). Over 15 minutes, crypto leans slightly toward reversal (arXiv 2608.21888), while this maths carries the hour forward.
- **The bar is the market price, not 50%.** On Polymarket BTC 15m, the market's own mid scored better than a 43-feature model and a drift-only probability (arXiv 2607.26245). The maths here is not fitted to outcomes, and the records show how its chance of Up compares with the settled results.
- **Fat tails.** One-hour BTC returns have excess kurtosis near 30 (arXiv 2010.07402), so a normal-curve chance needs checking against outcomes.
- **Fills at the bid.** A buy at the best bid fills when sellers come to it, which is more likely when the price is moving against it. The paper fill model counts real trades through the queue ahead, and a trade below the bid as a fill, so it does not flatter this.
- **The paper queue.** A paper order sees only the shares displayed at its price when it was placed. It does not see bids that join ahead later, or cancels ahead of it until a trade below its price shows they are gone.
- **Trades read at their average price.** In a stretch too deep for the venue's per-order records, a trade below the bid fills nothing, so fills there are under-counted, and a sweep whose average lands exactly on the bid is counted in full. The card says so when it happens.
- **Two paper books on one market.** This strategy's paper orders and Fade 1h Momentum on 15m's are filled from the same trades separately, so one trade can fill an order of each, together beyond the trade's size. Fade trades BTC 15m too.

## Sources

- `tasks/2026-09-22-kelly-horse-race-research.md` — research note, 51 arXiv papers and the settlement checks (Claude, 2026-09-22)
- `tools/kelly_horse_race/checks.py` — the numerical checks; `tools/kelly_horse_race/price_to_beat_chain.py` — the 120-of-120 settlement check
- `docs/superpowers/specs/2026-09-26-kelly-horse-race-design.md` — the design (Claude, 2026-09-26)
- `tools/kelly_horse_race/examples.py` — the 96-window scores under Evidence so far (run 2026-09-27 12:00 UTC)
- The app's ledger, `kelly_horse_race_decisions` ids 1 to 3 and `kelly_horse_race_orders` ids 1 and 2 (2026-09-27) — the worked examples; Gamma `/events?slug=` for the same windows — the cross-check of their prints
- [Polymarket, placing orders](https://docs.polymarket.com/trading/place-orders) — a GTD order stops one minute before its expiry, and an expiry less than 3 minutes ahead is refused
- [Polymarket, get a single order by id](https://docs.polymarket.com/api-reference/trade/get-single-order-by-id) and [post a new order](https://docs.polymarket.com/api-reference/trade/post-a-new-order) — the order id is the order's hash, and an order is returned in any state, cancelled and fully matched ones included
- Polymarket data-api `/trades` for the two worked windows, both lists (`takerOnly=true` and `takerOnly=false`), read on 2026-09-29 — the worked examples' fills re-run with the fill model of 2026-09-29
- Kelly, J. L. (1956), *A New Interpretation of Information Rate*, Bell System Technical Journal 35(4)
- [arXiv 1901.06278](https://arxiv.org/abs/1901.06278) — horse-race betting, $b = p$ (Prop. 2), and holding no cash when the sides cost under \$1 (Prop. 9)
- [arXiv 2606.19517](https://arxiv.org/abs/2606.19517) — a binary option's probability from drift and volatility
- [arXiv 2606.08209](https://arxiv.org/abs/2606.08209), [arXiv 2608.21888](https://arxiv.org/abs/2608.21888), [arXiv 2607.26245](https://arxiv.org/abs/2607.26245), [arXiv 2010.07402](https://arxiv.org/abs/2010.07402) — the weaknesses above

## Changelog

- 2026-09-29 · `0beb5b66be6d` · Docstrings only: the execution package and the unknown-order error describe the per-level fills and the lookup by order id
- 2026-09-29 · `46ee0ee01a4c` · Paper fills from the price levels each trade reached, and a sale below the bid fills the order; a live send with no reply is asked for by its order id, worked out before sending, until 120 s after its window's stop. Worked examples re-run on the real tape: decision 3 fills at 11:16:11.
- 2026-09-29 · `4f0040ba72fa` · K waits up to 70 s after the open while a feed reconnect could still bring the opening print, then skips the window
- 2026-09-27 · `affd5391d776` · Review fixes: K comes only from the TWAP-60s print at the open, and a window whose open print was missed is skipped once no reconnect can bring it back, 70 s after the open (Gamma writes priceToBeat only when the window ends); the decision cutoff is 3 min 20 s before the close and both venues refuse an order expiring under 3 min 5 s ahead, as Polymarket does; live sizes reach the exchange whole, a 4xx refusal is a rejection and a cancel of an already-closed order is no error; paper fills count trades printed a hair off their price level and read the tape back about 10,500 trades. Worked examples from decisions 2 and 3 and a 96-window score added.
- 2026-09-26 · `9f693c30713a` · A placed order's notional the risk gate failed to record is tried again next pass, before anything new is checked, and caught up at startup, so the daily cap never under-counts.
- 2026-09-26 · `ad2bb25e746d` · A settled P&L the risk gate failed to record is tried again next pass; a shutdown mid-send waits for the order to be recorded; an order is stamped with the time it goes, so a paper order counts trades only from then.
- 2026-09-26 · `69daf64caa7a` · Live safety: the switch, the kill switch and LIVE are checked again right before an order goes; a live send with no reply is searched for on the exchange and recorded as unknown until found, holding new live orders; an empty status reply is a miss, not an end; failed cancels reach the card; an order that cannot be recorded is cancelled.
- 2026-09-26 · `422daf624a50` · Live venue: an order the exchange reports expired or invalid, or still open two minutes past its GTD stop, is taken as closed (the stuck case flagged forced), so its window can settle.
- 2026-09-26 · `ef4bc244bf1a` · The live leg: the order also goes to the exchange as a post-only GTD buy while the operator has armed LIVE (selected and clicked in this process, a wallet that passes, the switch on); PAPER again cancels resting live orders; fills from size_matched; every live placement and cancel journaled.
- 2026-09-26 · `ffd0d90d7706` · First build: the maths, the inputs, the ledger and the runner, on the shared resting-order layer; paper always, live not built yet.
