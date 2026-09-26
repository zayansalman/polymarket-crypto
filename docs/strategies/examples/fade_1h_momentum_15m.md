# Fade 1h Momentum on 15m: worked examples

Generated 2026-09-21 18:52 UTC by `tools/fade_1h_momentum_15m/examples.py` from step 2's rows (`step2_polymarket.build_rows`) and `data/fade_1h_momentum_15m/params_pre_sep17.json`. 10 real decisions on the Sep 17-20 Polymarket tape, each 2 minutes into a 15-minute Up/Down window. (d) and (e) are picked on how the window settled. Picked on what traded after the decision: (f) on a later fill, which happens only when the market moves against the bid, so it leans toward a loss; (h) on the first taker buy in the 30 s after the decision; (i) on no later trade below the bid. The other filters use only what is known at the decision.

## How to read a card

Each card opens with a short summary and the story in four lines: the leg, the snap-back, the momentum and the trade. The tables under them hold every number behind the story.

What the maths looks at, in the order a card shows it. The symbol in brackets is only for readers who want the formula.

- **15m leg so far** (y): how far spot has moved since the window opened, in % and in typical moves for the time left.
- **Time left** (h): minutes until the window closes. The less time left, the more the leg so far decides the window.
- **Typical swing** (sigma): the size of a typical one-hour move in spot, up or down, from its realised volatility over the last 60 minutes. It is a size, not a direction or a trend. One typical move for the time left (s) is this scaled to the minutes left, trimmed by the snap-back and widened by doubt about the momentum.
- **Snap-back from the last 12 candles** (stretch M): a weighted average of the last twelve 15m candles, not their sum. The newest candle carries about 32% of the weight, the 2nd 16%, the 12th 3%, so the newest candle often sets the stretch's sign. Each candle is also soft-capped at c (0.93% with these parameters), which only bites on candles near that size: a candle of 0.47% is trimmed by about 8%, a smaller one by less. The maths expects part of the stretch to come back before the close.
- **Where in the hour**: the snap-back gets stronger as the hour goes on. With these parameters about 9% of the stretch comes back in a 1st-quarter window and about 27% in a 4th-quarter window (decided 2 minutes in).
- **The hour's move so far** (x): how far spot has moved since the hour opened. On its own it gives a price for the hour.
- **The 1h market price** (m_H): the crowd's price for the hour closing Up. Its gap from the hour-alone price is the drift the crowd expects for the rest of the hour.
- **Trailing 1h spot trend** (mu_L): spot's move over the last 60 minutes. It is blended with the 1h market's drift into one **blended momentum** (mu), by weights fitted on the other tape days' forecast errors (leave one day out; there is no earlier tape to fit on).
- **Momentum weight** (theta): negative, the maths fades the momentum; positive, it follows it. With theta -0.0434, the maths expects the rest of the window to go against the momentum by 4.3% of what the momentum alone would carry. The push is theta × blended momentum (% an hour) × the time it counts for (G, in hours). A drift that builds up during the window is itself partly pulled back by the snap-back, so G is a little less than the time left.
- **How much the momentum moves Up**: at minute 2 on the tape (1,071 rows) the momentum push moves Up by 0.35 points on average and 2.6 points at most. Setting it to zero would change the maths' side on 6 rows. The biggest factor is the leg on 971 rows, the snap-back on 96 and the momentum on 4.
- **Spot feed and settlement feed**: the maths reads Binance 1-minute candles. The markets settle on Chainlink. At minute 2 the two closed the window in different directions on 55 of 1,071 rows (5.1%). A split can turn a right read of Binance into a loss, or the other way round. Each card's Outcome says which way each feed closed.
- **The 15m market price**: the last trade on the window's own market. A trade can be a buy or a sell, so the last trade may have hit a bid. The **ask** is the last price a taker actually paid for that side in the minute before the decision. A taker buy fills at the first price a taker pays in the 30 s after.
- **Break-even** (a*): the most a taker share is worth, the price where price plus fee equals the maths' probability. The fee is 7% × price × (1 − price): 1.75c a share at 50c, less further from 50c.
- **Resting bid and fill chance** (b*): the maths looks for the bid with the most expected profit per share bid (fill chance × edge), from 1c up to 1c under the last trade. The paper book keeps a bid at least 1c under the last trade so that it rests rather than buys at once, and it rests one bid per window, with no scaled child orders. When expected profit is still rising at the top of that range, the bid sits 1c under the last trade: the book sets that level, not a peak in the maths. Every resting bid in these cards sits there. On the whole minute-2 tape, all 521 of step 2's 521 resting bids sit there too. The fill chance is the maths' own estimate that the price trades down to the bid before the close. A fill only happens when the price falls to the bid, so the maths marks its side lower for a filled bid.
- **Size**: full Kelly, the bankroll share that grows money fastest for that edge, from the maths' probability and the entry price. Taker and maker are two separate paper books compared side by side, not stacked on the same window. Each card gives the size in its Decision bullets.

The waterfall shows each factor's fair share of the move from 50% (the same total whichever order you add them).

The strategy has no price rules: side, entry price and size all come out of the one calculation.

The formula and the fitted parameters are in the footnote at the end.

## How the examples were picked

Each card is the first row in time order that fits its filter and is not already shown on an earlier card. The filters only choose which real rows to show; they are not part of the strategy.

- **(a) Top of the hour: the maths and the market agree**: 1st quarter of the hour; the maths' Up probability within 2 points of the 15m market's last Up price; no taker buy (the ask on the maths' side is not under its break-even) (58 rows qualify).
- **(b) Late in the hour after a fast 15m up leg: the snap-back leans against it**: 4th quarter of the hour; the last completed 15m candle up by at least one typical 15-minute swing; the stretch above zero, so the snap-back works against Up (48 rows qualify).
- **(c) The 1h market disagrees with the hour's move**: a 1h market trade in the last minute, its Up price at least 5c away from what the hour's move so far would price on its own; the momentum push worth at least 0.5 points on Up (17 rows qualify).
- **(d) A taker entry that won**: the maths' taker buy filled (the ask before the decision under the break-even, then a buy limited at the break-even) and the side won (220 rows qualify).
- **(e) A taker entry that lost**: the maths' taker buy filled (same execution) and the side lost (135 rows qualify).
- **(f) A resting bid that filled**: a bid rests on the maths' side (a positive expected profit) and a later trade on that side is below it (429 rows qualify). This depends on what traded after the decision: a bid fills only when the market moves against it, so this pick leans toward a loss.
- **(g) The snap-back or the momentum leads**: the snap-back or the momentum moves Up more than the 15m leg does (100 rows qualify).
- **(h) A taker buy that missed its limit**: the ask before the decision is under the break-even, but the first taker buy in the 30 s after it is over the break-even, so the limited buy does not fill (92 rows qualify).
- **(i) A resting bid that did not fill**: a bid rests on the maths' side and no later trade on that side is below it (92 rows qualify).
- **(j) A Down-side entry**: the maths makes Down more likely and acts on it: it sends a taker buy of Down (the ask under the break-even) or rests a bid on Down (289 rows qualify).

## (a) Top of the hour: the maths and the market agree

**XRP 2 min into the 21:00 window: the leg leads, Up 76.8%, no trade**

> The +0.108% 15m leg adds 27.6 points to Up, the snap-back takes 0.6 off and the momentum takes 0.2 off.
>
> The maths makes Up 76.8% against Up's 77c market price: Up's 77c ask is at or over its 75.5c break-even, so no taker buy, and no bid is worth resting.
>
> Down won: the maths had no position.

**The story**

- **Leg.** XRP is up 0.108% since the 21:00 window opened, with 13 of 15 minutes left (1st quarter of the hour). A typical move for the time left is 0.143%, from XRP's typical one-hour swing of 0.322% (up or down, realised over the last 60 min), so the leg is 0.75 typical moves, which adds 27.6 points to Up.
- **Snap-back.** Weighted toward the newest, the last twelve 15m candles average a stretch of +0.027%. The latest candle, +0.185% (1.1 typical 15m moves), carries 32% of the weight and alone gives +0.058%. In a 1st-quarter window, where the snap-back is weakest, the maths expects 9% of the stretch back before the close, which takes 0.6 points off Up.
- **Momentum.** The hour is up 0.108% so far, which on its own prices Up at 63.3c; the 1h market pays 54c, so the crowd expects part of the rise to be given back (-0.079% an hour for the rest of the hour). Blended 63/37 with the trailing hour on spot (+0.385% an hour), momentum is +0.091% an hour; the maths fades it (weight -0.043), which takes only 0.2 points off Up, so it barely matters.
- **Trade.** Net: Up 76.8%, Down 23.2%. Up's 77c ask is at or over its 75.5c break-even: no taker buy. Rests no bid: a bid fills only after Up has fallen to it, and then the maths values Up at or below the bid, at every bid from 1c to 76c.

**Inputs**

| input | value |
|---|---|
| coin | XRP |
| window | 2026-09-17 21:00-21:15 UTC, 1st quarter of the hour |
| decision time | 21:02 UTC: 2 min gone, 13 min left |
| 15m leg so far | +0.108% = +0.75 typical moves for the time left |
| typical move for the time left | 0.143% (without the snap-back or momentum adjustments, sigma × √time left: 0.150%) |
| typical one-hour swing, up or down (sigma, realised over the last 60 min) | 0.322% |
| last 12 15m candles, newest first | +0.185 -0.316 +0.115 +0.231 +0.031 -0.015 +0.108 -0.108 -0.131 -0.193 -0.023 -0.085 (%) |
| weight of each candle, newest first | 31.5 16.0 10.8 8.1 6.5 5.5 4.7 4.1 3.7 3.3 3.0 2.8 (%) |
| each candle's part of the stretch (weight × capped candle) | +0.058 -0.049 +0.012 +0.018 +0.002 -0.001 +0.005 -0.004 -0.005 -0.006 -0.001 -0.002 (%) |
| stretch (weighted average = sum of each candle's part above) | +0.027% |
| share pulled back before the close | 9.0% in a 1st-quarter window, so the snap-back pull is +0.0025%. The snap-back is stronger later in the hour: 1st 9.0%, 2nd 13.2%, 3rd 19.1%, 4th 27.3% at this minute |
| hour's move so far | +0.108% (on its own it prices the hour Up at 63.3c) |
| 1h market Up price | 54c |
| drift implied by the 1h price | -0.079% an hour for the rest of the hour |
| trailing 1h spot trend | +0.385% an hour |
| blend weights | 1h market 63%, spot 37%, fitted on the other tape days' forecast errors (Sep 18, 19 and 20) |
| blended momentum | +0.091% an hour (how far off this estimate has been: 0.381% an hour, one sd) |
| momentum weight (theta) | -0.0434 (negative: fades the momentum) |
| time the momentum counts for (G) | 12.4 min = 0.206 h. A drift that builds up during the window is itself partly pulled back by the snap-back, so the 13 minutes left count as 12.4 |
| momentum push = theta × blended momentum × G | -0.0434 × +0.091% an hour × 0.206 h = -0.0008% |

**Probability waterfall**: each factor's fair share of the move from 50% (the same total whichever order you add them).

| step | points on Up | Up after | in typical moves for the time left |
|---|---|---|---|
| start |  | 50.0% |  |
| the 15m leg so far | +27.6 | 77.6% | +0.755 |
| the snap-back pull | -0.6 | 77.0% | -0.017 |
| the momentum push | -0.2 | 76.8% | -0.006 |
| net | +26.8 | 76.8% | +0.732 |

**Decision**

| | Up | Down |
|---|---|---|
| the maths' probability | 76.8% | 23.2% |
| 15m market price (last trade, a buy or a sell) | 77c | 23c |
| ask at the decision (last price a taker paid in the minute before 21:02) | 77c | none |
| first taker buy in the 30 s after 21:02 | 77c | 19c |
| taker break-even (a*) | 75.5c | 22c |

- The side the maths makes more likely: **Up** (76.8%).
- Taker: Up's ask of 77c is at or over the 75.5c break-even (75.5c plus the 1.30c fee at that price adds up to the maths' 76.8%): no taker buy.
- Maker: no bid is worth resting. A bid fills only after Up has fallen to it, and at every bid from 1c to 76c the maths then values Up at or below the bid (the best, 1c, gives -0.01c per share bid).

**Outcome**

- Down won: the maths' side lost. Binance, which the maths reads, and Chainlink, which settles the market, both closed Down.

<sub>Market: `xrp-updown-15m-1789678800`. Check: explain() p = 0.767782918492, step 2 p = 0.767782918492.</sub>

## (b) Late in the hour after a fast 15m up leg: the snap-back leans against it

**BTC 2 min into the 22:45 window: the snap-back takes 1.7 points off Up, Up 50.5%, the maths buys Up at 48c and bids Up at 47c**

> The snap-back takes 1.7 points off Up: weighted toward the newest, the last twelve 15m candles average a stretch of +0.016%, and in a 4th-quarter window the maths expects 27% of it back before the close. The +0.005% 15m leg adds 2.0 and the momentum adds 0.1.
>
> The maths makes Up 50.5% against Up's 48c market price: it buys Up at 48c as a taker, and it rests a bid on Up at 47c.
>
> Down won: the taker buy lost 49.7c a share and the filled bid lost 47.0c a share. The feeds split: Binance, which the maths reads, closed Up; Chainlink, which settles the market, closed Down.

**The story**

- **Leg.** BTC is up 0.005% since the 22:45 window opened, with 13 of 15 minutes left (4th quarter of the hour). A typical move for the time left is 0.105%, from BTC's typical one-hour swing of 0.265% (up or down, realised over the last 60 min), so the leg is 0.05 typical moves, which adds 2.0 points to Up.
- **Snap-back.** Weighted toward the newest, the last twelve 15m candles average a stretch of +0.016%. The latest candle, +0.189% (1.4 typical 15m moves), carries 32% of the weight and alone gives +0.059%. In a 4th-quarter window, where the snap-back is strongest, the maths expects 27% of the stretch back before the close, which takes 1.7 points off Up.
- **Momentum.** The hour is up 0.008% so far, which on its own prices Up at 52.6c; the 1h market pays 51c, so the crowd expects part of the rise to be given back (-0.023% an hour for the rest of the hour). Blended 63/37 with the trailing hour on spot (-0.089% an hour), momentum is -0.047% an hour; the maths fades it (weight -0.043), which adds only 0.1 points to Up, so it barely matters.
- **Trade.** Net: Up 50.5%, Down 49.5%. Buys Up at 48c against a 48.7c break-even. Rests a bid on Up at 47c, 1c under the last trade.

**Inputs**

| input | value |
|---|---|
| coin | BTC |
| window | 2026-09-17 22:45-23:00 UTC, 4th quarter of the hour |
| decision time | 22:47 UTC: 2 min gone, 13 min left |
| 15m leg so far | +0.005% = +0.05 typical moves for the time left |
| typical move for the time left | 0.105% (without the snap-back or momentum adjustments, sigma × √time left: 0.124%) |
| typical one-hour swing, up or down (sigma, realised over the last 60 min) | 0.265% |
| last 12 15m candles, newest first | +0.189 -0.123 -0.064 -0.143 -0.046 +0.069 -0.064 +0.037 -0.240 +0.098 +0.055 -0.013 (%) |
| weight of each candle, newest first | 31.5 16.0 10.8 8.1 6.5 5.5 4.7 4.1 3.7 3.3 3.0 2.8 (%) |
| each candle's part of the stretch (weight × capped candle) | +0.059 -0.019 -0.007 -0.012 -0.003 +0.004 -0.003 +0.002 -0.009 +0.003 +0.002 -0.000 (%) |
| stretch (weighted average = sum of each candle's part above) | +0.016% |
| share pulled back before the close | 27.3% in a 4th-quarter window, so the snap-back pull is +0.0044%. The snap-back is stronger later in the hour: 1st 9.0%, 2nd 13.2%, 3rd 19.1%, 4th 27.3% at this minute |
| hour's move so far | +0.008% (on its own it prices the hour Up at 52.6c) |
| 1h market Up price | 51c |
| drift implied by the 1h price | -0.023% an hour for the rest of the hour |
| trailing 1h spot trend | -0.089% an hour |
| blend weights | 1h market 63%, spot 37%, fitted on the other tape days' forecast errors (Sep 18, 19 and 20) |
| blended momentum | -0.047% an hour (how far off this estimate has been: 0.314% an hour, one sd) |
| momentum weight (theta) | -0.0434 (negative: fades the momentum) |
| time the momentum counts for (G) | 11.0 min = 0.184 h. A drift that builds up during the window is itself partly pulled back by the snap-back, so the 13 minutes left count as 11.0 |
| momentum push = theta × blended momentum × G | -0.0434 × -0.047% an hour × 0.184 h = +0.0004% |

**Probability waterfall**: each factor's fair share of the move from 50% (the same total whichever order you add them).

| step | points on Up | Up after | in typical moves for the time left |
|---|---|---|---|
| start |  | 50.0% |  |
| the 15m leg so far | +2.0 | 52.0% | +0.050 |
| the snap-back pull | -1.7 | 50.3% | -0.042 |
| the momentum push | +0.1 | 50.5% | +0.004 |
| net | +0.5 | 50.5% | +0.011 |

**Decision**

| | Up | Down |
|---|---|---|
| the maths' probability | 50.5% | 49.5% |
| 15m market price (last trade, a buy or a sell) | 48c | 52c |
| ask at the decision (last price a taker paid in the minute before 22:47) | 48c | 51c |
| first taker buy in the 30 s after 22:47 | 48c | 54c |
| taker break-even (a*) | 48.7c | 47.8c |

- The side the maths makes more likely: **Up** (50.5%).
- Taker: Up's ask of 48c is under the 48.7c break-even (48.7c plus the 1.75c fee at that price adds up to the maths' 50.5%). It buys with a limit at 48.7c: filled at 48c (the first taker buy after 22:47), fee 1.75c, expected profit +0.70c a share, full-Kelly size 1.4% of bankroll.
- Maker: rests a bid on Up at 47c, 1c under the 48c last trade. Expected profit per share bid is still rising there, at the top of the paper book's bid range, so the book's range sets this level, not a peak in the maths. Fill chance 98.3%: the maths' own estimate that Up trades down to 47c before the close. A fill only happens if the price falls to the bid, which means the market has moved against Up, so the maths marks Up down from 50.5% to 49.2% for a filled bid. Expected profit +2.20c per share bid; full-Kelly size 4.2% of bankroll.

**Outcome**

- Down won: the maths' side lost. **The feeds split:** Binance, which the maths reads, closed Up; Chainlink, which settles the market, closed Down.
- Taker: -49.7c a share after the fee; at full-Kelly size -$1.40 per $100 of bankroll.
- Maker: filled (lowest later Up price: 0.1c); -47.0c a share, no fee; at full-Kelly size -$4.22 per $100 of bankroll.

<sub>Market: `btc-updown-15m-1789685100`. Check: explain() p = 0.504503419923, step 2 p = 0.504503419923.</sub>

## (c) The 1h market disagrees with the hour's move

**BTC 2 min into the 05:45 window: the 1h market pays 61c against 38.2c for the hour's move alone, Up 28.9%, no trade**

> The momentum takes 0.8 points off Up: the 1h market pays 61c for Up against 38.2c from the hour's move alone, which pulls the blended momentum to +0.462% an hour, and the maths fades it. The -0.078% 15m leg takes 17.7 off and the snap-back takes 2.5 off.
>
> The maths makes Down 71.1% against Down's 70c market price: Down's 71c ask is at or over its 69.6c break-even, so no taker buy, and the paper book rests no bid.
>
> Down won: the maths had no position.

**The story**

- **Leg.** BTC is down 0.078% since the 05:45 window opened, with 13 of 15 minutes left (4th quarter of the hour). A typical move for the time left is 0.167%, from BTC's typical one-hour swing of 0.421% (up or down, realised over the last 60 min), so the leg is 0.47 typical moves, which takes 17.7 points off Up.
- **Snap-back.** Weighted toward the newest, the last twelve 15m candles average a stretch of +0.041%. The latest candle, -0.013%, carries 32% of the weight and alone gives -0.004%. In a 4th-quarter window, where the snap-back is strongest, the maths expects 27% of the stretch back before the close, which takes 2.5 points off Up.
- **Momentum.** The hour is down 0.059% so far, which on its own prices Up at 38.2c; the 1h market pays 61c, so the crowd expects the drop to be more than won back, with the hour finishing Up (+0.524% an hour for the rest of the hour). Blended 84/16 with the trailing hour on spot (+0.139% an hour), momentum is +0.462% an hour; the maths fades it (weight -0.043), which takes 0.8 points off Up.
- **Trade.** Net: Up 28.9%, Down 71.1%. Down's 71c ask is at or over its 69.6c break-even: no taker buy. The paper book rests no bid (see the note on its bid search).

**Inputs**

| input | value |
|---|---|
| coin | BTC |
| window | 2026-09-18 05:45-06:00 UTC, 4th quarter of the hour |
| decision time | 05:47 UTC: 2 min gone, 13 min left |
| 15m leg so far | -0.078% = -0.47 typical moves for the time left |
| typical move for the time left | 0.167% (without the snap-back or momentum adjustments, sigma × √time left: 0.196%) |
| typical one-hour swing, up or down (sigma, realised over the last 60 min) | 0.421% |
| last 12 15m candles, newest first | -0.013 +0.094 -0.062 +0.162 +0.225 -0.019 -0.119 +0.052 -0.108 +0.134 +0.434 +0.054 (%) |
| weight of each candle, newest first | 31.5 16.0 10.8 8.1 6.5 5.5 4.7 4.1 3.7 3.3 3.0 2.8 (%) |
| each candle's part of the stretch (weight × capped candle) | -0.004 +0.015 -0.007 +0.013 +0.014 -0.001 -0.006 +0.002 -0.004 +0.004 +0.012 +0.002 (%) |
| stretch (weighted average = sum of each candle's part above) | +0.041% |
| share pulled back before the close | 27.3% in a 4th-quarter window, so the snap-back pull is +0.0113%. The snap-back is stronger later in the hour: 1st 9.0%, 2nd 13.2%, 3rd 19.1%, 4th 27.3% at this minute |
| hour's move so far | -0.059% (on its own it prices the hour Up at 38.2c) |
| 1h market Up price | 61c |
| drift implied by the 1h price | +0.524% an hour for the rest of the hour |
| trailing 1h spot trend | +0.139% an hour |
| blend weights | 1h market 84%, spot 16%, fitted on the other tape days' forecast errors (Sep 17, 19 and 20) |
| blended momentum | +0.462% an hour (how far off this estimate has been: 0.314% an hour, one sd) |
| momentum weight (theta) | -0.0434 (negative: fades the momentum) |
| time the momentum counts for (G) | 11.0 min = 0.184 h. A drift that builds up during the window is itself partly pulled back by the snap-back, so the 13 minutes left count as 11.0 |
| momentum push = theta × blended momentum × G | -0.0434 × +0.462% an hour × 0.184 h = -0.0037% |

**Probability waterfall**: each factor's fair share of the move from 50% (the same total whichever order you add them).

| step | points on Up | Up after | in typical moves for the time left |
|---|---|---|---|
| start |  | 50.0% |  |
| the 15m leg so far | -17.7 | 32.3% | -0.466 |
| the snap-back pull | -2.5 | 29.7% | -0.068 |
| the momentum push | -0.8 | 28.9% | -0.022 |
| net | -21.1 | 28.9% | -0.556 |

**Decision**

| | Up | Down |
|---|---|---|
| the maths' probability | 28.9% | 71.1% |
| 15m market price (last trade, a buy or a sell) | 30c | 70c |
| ask at the decision (last price a taker paid in the minute before 05:47) | 30c | 71c |
| first taker buy in the 30 s after 05:47 | 30c | 71c |
| taker break-even (a*) | 27.5c | 69.6c |

- The side the maths makes more likely: **Down** (71.1%).
- Taker: Down's ask of 71c is at or over the 69.6c break-even (69.6c plus the 1.48c fee at that price adds up to the maths' 71.1%): no taker buy.
- Maker: the paper book rests no bid (see the note on its bid search below).

**Outcome**

- Down won: the maths' side won. Binance, which the maths reads, and Chainlink, which settles the market, both closed Down.

**Note.** Known gap in the paper book's bid search: a check of every 1c bid finds a 69c Down bid with a 97.2% fill chance, Down at 69.9% if filled, and +0.88c expected per share bid. The search missed it, and the book rested nothing. In hindsight it would have filled (lowest later Down price: 67c) and made +31.0c a share.

<sub>Market: `btc-updown-15m-1789710300`. Check: explain() p = 0.289169562335, step 2 p = 0.289169562335.</sub>

## (d) A taker entry that won

**BTC 2 min into the 20:45 window: the leg leads, Up 65.7%, the maths buys Up at 62c and bids Up at 60c**

> The +0.026% 15m leg adds 9.7 points to Up, the snap-back adds 5.8 and the momentum adds 0.1.
>
> The maths makes Up 65.7% against Up's 61c market price: it buys Up at 62c as a taker, and it rests a bid on Up at 60c.
>
> Up won: the taker buy made 36.4c a share and the filled bid made 40.0c a share.

**The story**

- **Leg.** BTC is up 0.026% since the 20:45 window opened, with 13 of 15 minutes left (4th quarter of the hour). A typical move for the time left is 0.105%, from BTC's typical one-hour swing of 0.265% (up or down, realised over the last 60 min), so the leg is 0.25 typical moves, which adds 9.7 points to Up.
- **Snap-back.** Weighted toward the newest, the last twelve 15m candles average a stretch of -0.058%. The latest candle, -0.240% (1.8 typical 15m moves), carries 32% of the weight and alone gives -0.074%. In a 4th-quarter window, where the snap-back is strongest, the maths expects 27% of the stretch back before the close, which adds 5.8 points to Up.
- **Momentum.** The hour is down 0.061% so far, which on its own prices Up at 31.1c; the 1h market pays 28c, so the crowd expects the drop to extend (-0.052% an hour for the rest of the hour). Blended 63/37 with the trailing hour on spot (-0.021% an hour), momentum is -0.041% an hour; the maths fades it (weight -0.043), which adds only 0.1 points to Up, so it barely matters.
- **Trade.** Net: Up 65.7%, Down 34.3%. Buys Up at 62c against a 64.1c break-even. Rests a bid on Up at 60c, 1c under the last trade.

**Inputs**

| input | value |
|---|---|
| coin | BTC |
| window | 2026-09-17 20:45-21:00 UTC, 4th quarter of the hour |
| decision time | 20:47 UTC: 2 min gone, 13 min left |
| 15m leg so far | +0.026% = +0.25 typical moves for the time left |
| typical move for the time left | 0.105% (without the snap-back or momentum adjustments, sigma × √time left: 0.123%) |
| typical one-hour swing, up or down (sigma, realised over the last 60 min) | 0.265% |
| last 12 15m candles, newest first | -0.240 +0.098 +0.055 -0.013 +0.029 +0.022 -0.015 -0.066 -0.153 +0.032 +0.068 -0.063 (%) |
| weight of each candle, newest first | 31.5 16.0 10.8 8.1 6.5 5.5 4.7 4.1 3.7 3.3 3.0 2.8 (%) |
| each candle's part of the stretch (weight × capped candle) | -0.074 +0.016 +0.006 -0.001 +0.002 +0.001 -0.001 -0.003 -0.006 +0.001 +0.002 -0.002 (%) |
| stretch (weighted average = sum of each candle's part above) | -0.058% |
| share pulled back before the close | 27.3% in a 4th-quarter window, so the snap-back pull is -0.0158%. The snap-back is stronger later in the hour: 1st 9.0%, 2nd 13.2%, 3rd 19.1%, 4th 27.3% at this minute |
| hour's move so far | -0.061% (on its own it prices the hour Up at 31.1c) |
| 1h market Up price | 28c |
| drift implied by the 1h price | -0.052% an hour for the rest of the hour |
| trailing 1h spot trend | -0.021% an hour |
| blend weights | 1h market 63%, spot 37%, fitted on the other tape days' forecast errors (Sep 18, 19 and 20) |
| blended momentum | -0.041% an hour (how far off this estimate has been: 0.313% an hour, one sd) |
| momentum weight (theta) | -0.0434 (negative: fades the momentum) |
| time the momentum counts for (G) | 11.0 min = 0.184 h. A drift that builds up during the window is itself partly pulled back by the snap-back, so the 13 minutes left count as 11.0 |
| momentum push = theta × blended momentum × G | -0.0434 × -0.041% an hour × 0.184 h = +0.0003% |

**Probability waterfall**: each factor's fair share of the move from 50% (the same total whichever order you add them).

| step | points on Up | Up after | in typical moves for the time left |
|---|---|---|---|
| start |  | 50.0% |  |
| the 15m leg so far | +9.7 | 59.7% | +0.251 |
| the snap-back pull | +5.8 | 65.6% | +0.151 |
| the momentum push | +0.1 | 65.7% | +0.003 |
| net | +15.7 | 65.7% | +0.404 |

**Decision**

| | Up | Down |
|---|---|---|
| the maths' probability | 65.7% | 34.3% |
| 15m market price (last trade, a buy or a sell) | 61c | 39c |
| ask at the decision (last price a taker paid in the minute before 20:47) | 61c | 40c |
| first taker buy in the 30 s after 20:47 | 62c | 39c |
| taker break-even (a*) | 64.1c | 32.8c |

- The side the maths makes more likely: **Up** (65.7%).
- Taker: Up's ask of 61c is under the 64.1c break-even (64.1c plus the 1.61c fee at that price adds up to the maths' 65.7%). It buys with a limit at 64.1c: filled at 62c (the first taker buy after 20:47), fee 1.65c, expected profit +2.05c a share, full-Kelly size 5.6% of bankroll.
- Maker: rests a bid on Up at 60c, 1c under the 61c last trade. Expected profit per share bid is still rising there, at the top of the paper book's bid range, so the book's range sets this level, not a peak in the maths. Fill chance 97.6%: the maths' own estimate that Up trades down to 60c before the close. A fill only happens if the price falls to the bid, which means the market has moved against Up, so the maths marks Up down from 65.7% to 64.5% for a filled bid. Expected profit +4.42c per share bid; full-Kelly size 11.3% of bankroll.

**Outcome**

- Up won: the maths' side won. Binance, which the maths reads, and Chainlink, which settles the market, both closed Up.
- Taker: +36.4c a share after the fee; at full-Kelly size +$3.22 per $100 of bankroll.
- Maker: filled (lowest later Up price: 26c); +40.0c a share, no fee; at full-Kelly size +$7.54 per $100 of bankroll.

<sub>Market: `btc-updown-15m-1789677900`. Check: explain() p = 0.656992304873, step 2 p = 0.656992304873.</sub>

## (e) A taker entry that lost

**BTC 2 min into the 22:00 window: the leg leads, Up 69.7%, the maths buys Up at 62c and bids Up at 59c**

> The +0.034% 15m leg adds 17.1 points to Up, the snap-back adds 2.3 and the momentum adds 0.3.
>
> The maths makes Up 69.7% against Up's 60c market price: it buys Up at 62c as a taker, and it rests a bid on Up at 59c.
>
> Down won: the taker buy lost 63.6c a share and the filled bid lost 59.0c a share.

**The story**

- **Leg.** BTC is up 0.034% since the 22:00 window opened, with 13 of 15 minutes left (1st quarter of the hour). A typical move for the time left is 0.077%, from BTC's typical one-hour swing of 0.173% (up or down, realised over the last 60 min), so the leg is 0.45 typical moves, which adds 17.1 points to Up.
- **Snap-back.** Weighted toward the newest, the last twelve 15m candles average a stretch of -0.052%. The latest candle, -0.143% (1.7 typical 15m moves), carries 32% of the weight and alone gives -0.045%. In a 1st-quarter window, where the snap-back is weakest, the maths expects 9% of the stretch back before the close, which adds 2.3 points to Up.
- **Momentum.** The hour is up 0.034% so far, which on its own prices Up at 58c; the 1h market pays 55c, so the crowd expects part of the rise to be given back (-0.013% an hour for the rest of the hour). Blended 63/37 with the trailing hour on spot (-0.171% an hour), momentum is -0.071% an hour; the maths fades it (weight -0.043), which adds only 0.3 points to Up, so it barely matters.
- **Trade.** Net: Up 69.7%, Down 30.3%. Buys Up at 62c against a 68.2c break-even. Rests a bid on Up at 59c, 1c under the last trade.

**Inputs**

| input | value |
|---|---|
| coin | BTC |
| window | 2026-09-17 22:00-22:15 UTC, 1st quarter of the hour |
| decision time | 22:02 UTC: 2 min gone, 13 min left |
| 15m leg so far | +0.034% = +0.45 typical moves for the time left |
| typical move for the time left | 0.077% (without the snap-back or momentum adjustments, sigma × √time left: 0.080%) |
| typical one-hour swing, up or down (sigma, realised over the last 60 min) | 0.173% |
| last 12 15m candles, newest first | -0.143 -0.046 +0.069 -0.064 +0.037 -0.240 +0.098 +0.055 -0.013 +0.029 +0.022 -0.015 (%) |
| weight of each candle, newest first | 31.5 16.0 10.8 8.1 6.5 5.5 4.7 4.1 3.7 3.3 3.0 2.8 (%) |
| each candle's part of the stretch (weight × capped candle) | -0.045 -0.007 +0.007 -0.005 +0.002 -0.013 +0.005 +0.002 -0.000 +0.001 +0.001 -0.000 (%) |
| stretch (weighted average = sum of each candle's part above) | -0.052% |
| share pulled back before the close | 9.0% in a 1st-quarter window, so the snap-back pull is -0.0047%. The snap-back is stronger later in the hour: 1st 9.0%, 2nd 13.2%, 3rd 19.1%, 4th 27.3% at this minute |
| hour's move so far | +0.034% (on its own it prices the hour Up at 58c) |
| 1h market Up price | 55c |
| drift implied by the 1h price | -0.013% an hour for the rest of the hour |
| trailing 1h spot trend | -0.171% an hour |
| blend weights | 1h market 63%, spot 37%, fitted on the other tape days' forecast errors (Sep 18, 19 and 20) |
| blended momentum | -0.071% an hour (how far off this estimate has been: 0.205% an hour, one sd) |
| momentum weight (theta) | -0.0434 (negative: fades the momentum) |
| time the momentum counts for (G) | 12.4 min = 0.206 h. A drift that builds up during the window is itself partly pulled back by the snap-back, so the 13 minutes left count as 12.4 |
| momentum push = theta × blended momentum × G | -0.0434 × -0.071% an hour × 0.206 h = +0.0006% |

**Probability waterfall**: each factor's fair share of the move from 50% (the same total whichever order you add them).

| step | points on Up | Up after | in typical moves for the time left |
|---|---|---|---|
| start |  | 50.0% |  |
| the 15m leg so far | +17.1 | 67.1% | +0.447 |
| the snap-back pull | +2.3 | 69.4% | +0.062 |
| the momentum push | +0.3 | 69.7% | +0.008 |
| net | +19.7 | 69.7% | +0.517 |

**Decision**

| | Up | Down |
|---|---|---|
| the maths' probability | 69.7% | 30.3% |
| 15m market price (last trade, a buy or a sell) | 60c | 40c |
| ask at the decision (last price a taker paid in the minute before 22:02) | 60c | 41c |
| first taker buy in the 30 s after 22:02 | 62c | 39c |
| taker break-even (a*) | 68.2c | 28.8c |

- The side the maths makes more likely: **Up** (69.7%).
- Taker: Up's ask of 60c is under the 68.2c break-even (68.2c plus the 1.52c fee at that price adds up to the maths' 69.7%). It buys with a limit at 68.2c: filled at 62c (the first taker buy after 22:02), fee 1.65c, expected profit +6.08c a share, full-Kelly size 16.7% of bankroll.
- Maker: rests a bid on Up at 59c, 1c under the 60c last trade. Expected profit per share bid is still rising there, at the top of the paper book's bid range, so the book's range sets this level, not a peak in the maths. Fill chance 96.9%: the maths' own estimate that Up trades down to 59c before the close. A fill only happens if the price falls to the bid, which means the market has moved against Up, so the maths marks Up down from 69.7% to 68.7% for a filled bid. Expected profit +9.37c per share bid; full-Kelly size 23.6% of bankroll.

**Outcome**

- Down won: the maths' side lost. Binance, which the maths reads, and Chainlink, which settles the market, both closed Down.
- Taker: -63.6c a share after the fee; at full-Kelly size -$16.72 per $100 of bankroll.
- Maker: filled (lowest later Up price: 1c); -59.0c a share, no fee; at full-Kelly size -$23.58 per $100 of bankroll.

<sub>Market: `btc-updown-15m-1789682400`. Check: explain() p = 0.697277204362, step 2 p = 0.697277204362.</sub>

## (f) A resting bid that filled

**XRP 2 min into the 21:15 window: the leg leads, Up 56.3%, the maths bids Up at 53c**

> The +0.015% 15m leg adds 4.1 points to Up, the snap-back adds 1.9 and the momentum adds 0.3.
>
> The maths makes Up 56.3% against Up's 54c market price: no one bought Up in the minute before, so there is no ask to take, and it rests a bid on Up at 53c.
>
> Down won: the filled bid lost 53.0c a share. The feeds split: Binance, which the maths reads, closed Up; Chainlink, which settles the market, closed Down.

**The story**

- **Leg.** XRP is up 0.015% since the 21:15 window opened, with 13 of 15 minutes left (2nd quarter of the hour). A typical move for the time left is 0.148%, from XRP's typical one-hour swing of 0.342% (up or down, realised over the last 60 min), so the leg is 0.10 typical moves, which adds 4.1 points to Up.
- **Snap-back.** Weighted toward the newest, the last twelve 15m candles average a stretch of -0.053%. The latest candle, -0.208% (1.2 typical 15m moves), carries 32% of the weight and alone gives -0.065%. In a 2nd-quarter window, the maths expects 13% of the stretch back before the close, which adds 1.9 points to Up.
- **Momentum.** There is no 1h market trade in the last minute, so momentum is the trailing hour on spot alone, -0.116% an hour; the maths fades it (weight -0.043), which adds only 0.3 points to Up, so it barely matters.
- **Trade.** Net: Up 56.3%, Down 43.7%. No one bought Up in the minute before: no ask to take. Rests a bid on Up at 53c, 1c under the last trade.

**Inputs**

| input | value |
|---|---|
| coin | XRP |
| window | 2026-09-17 21:15-21:30 UTC, 2nd quarter of the hour |
| decision time | 21:17 UTC: 2 min gone, 13 min left |
| 15m leg so far | +0.015% = +0.10 typical moves for the time left |
| typical move for the time left | 0.148% (without the snap-back or momentum adjustments, sigma × √time left: 0.159%) |
| typical one-hour swing, up or down (sigma, realised over the last 60 min) | 0.342% |
| last 12 15m candles, newest first | -0.208 +0.185 -0.316 +0.115 +0.231 +0.031 -0.015 +0.108 -0.108 -0.131 -0.193 -0.023 (%) |
| weight of each candle, newest first | 31.5 16.0 10.8 8.1 6.5 5.5 4.7 4.1 3.7 3.3 3.0 2.8 (%) |
| each candle's part of the stretch (weight × capped candle) | -0.065 +0.029 -0.033 +0.009 +0.015 +0.002 -0.001 +0.004 -0.004 -0.004 -0.006 -0.001 (%) |
| stretch (weighted average = sum of each candle's part above) | -0.053% |
| share pulled back before the close | 13.2% in a 2nd-quarter window, so the snap-back pull is -0.0070%. The snap-back is stronger later in the hour: 1st 9.0%, 2nd 13.2%, 3rd 19.1%, 4th 27.3% at this minute |
| hour's move so far | -0.193% (on its own it prices the hour Up at 25.3c) |
| 1h market Up price | no trade in the last minute |
| drift implied by the 1h price | none |
| trailing 1h spot trend | -0.116% an hour |
| blend weights | spot 100% (no 1h market trade in the last minute) |
| blended momentum | -0.116% an hour (how far off this estimate has been: 0.463% an hour, one sd) |
| momentum weight (theta) | -0.0434 (negative: fades the momentum) |
| time the momentum counts for (G) | 12.1 min = 0.201 h. A drift that builds up during the window is itself partly pulled back by the snap-back, so the 13 minutes left count as 12.1 |
| momentum push = theta × blended momentum × G | -0.0434 × -0.116% an hour × 0.201 h = +0.0010% |

**Probability waterfall**: each factor's fair share of the move from 50% (the same total whichever order you add them).

| step | points on Up | Up after | in typical moves for the time left |
|---|---|---|---|
| start |  | 50.0% |  |
| the 15m leg so far | +4.1 | 54.1% | +0.104 |
| the snap-back pull | +1.9 | 56.0% | +0.048 |
| the momentum push | +0.3 | 56.3% | +0.007 |
| net | +6.3 | 56.3% | +0.159 |

**Decision**

| | Up | Down |
|---|---|---|
| the maths' probability | 56.3% | 43.7% |
| 15m market price (last trade, a buy or a sell) | 54c | 46c |
| ask at the decision (last price a taker paid in the minute before 21:17) | none | 46c |
| first taker buy in the 30 s after 21:17 | none | 55c |
| taker break-even (a*) | 54.6c | 42c |

- The side the maths makes more likely: **Up** (56.3%).
- Taker: no taker bought Up in the minute before 21:17, so there is no ask to act on. The 54c last trade matches a taker buy of Down at 46c, which is the same as someone selling Up at 54c: it hit a bid on Up, so it is not a price anyone could buy Up at.
- Maker: rests a bid on Up at 53c, 1c under the 54c last trade. Expected profit per share bid is still rising there, at the top of the paper book's bid range, so the book's range sets this level, not a peak in the maths. Fill chance 97.9%: the maths' own estimate that Up trades down to 53c before the close. A fill only happens if the price falls to the bid, which means the market has moved against Up, so the maths marks Up down from 56.3% to 55.2% for a filled bid. Expected profit +2.17c per share bid; full-Kelly size 4.7% of bankroll.

**Outcome**

- Down won: the maths' side lost. **The feeds split:** Binance, which the maths reads, closed Up; Chainlink, which settles the market, closed Down.
- Maker: filled (lowest later Up price: 1c); -53.0c a share, no fee; at full-Kelly size -$4.71 per $100 of bankroll.

<sub>Market: `xrp-updown-15m-1789679700`. Check: explain() p = 0.563049474816, step 2 p = 0.563049474816.</sub>

## (g) The snap-back or the momentum leads

**BTC 2 min into the 21:15 window: the snap-back leads, Up 54.1%, no trade**

> The snap-back adds 2.2 points to Up: weighted toward the newest, the last twelve 15m candles average a stretch of -0.033%, and in a 2nd-quarter window the maths expects 13% of it back before the close. The +0.004% 15m leg adds 1.8 and the momentum adds 0.2.
>
> The maths makes Up 54.1% against Up's 56c market price: Up's 57c ask is at or over its 52.4c break-even, so no taker buy, and no bid is worth resting.
>
> Up won: the maths had no position.

**The story**

- **Leg.** BTC is up 0.004% since the 21:15 window opened, with 13 of 15 minutes left (2nd quarter of the hour). A typical move for the time left is 0.080%, from BTC's typical one-hour swing of 0.185% (up or down, realised over the last 60 min), so the leg is 0.04 typical moves, which adds 1.8 points to Up.
- **Snap-back.** Weighted toward the newest, the last twelve 15m candles average a stretch of -0.033%. The latest candle, -0.064%, carries 32% of the weight and alone gives -0.020%. In a 2nd-quarter window, the maths expects 13% of the stretch back before the close, which adds 2.2 points to Up.
- **Momentum.** The hour is down 0.061% so far, which on its own prices Up at 34.9c; the 1h market pays 37c, so the crowd expects part of the drop to be won back (+0.012% an hour for the rest of the hour). Blended 63/37 with the trailing hour on spot (-0.123% an hour), momentum is -0.038% an hour; the maths fades it (weight -0.043), which adds only 0.2 points to Up, so it barely matters.
- **Trade.** Net: Up 54.1%, Down 45.9%. Up's 57c ask is at or over its 52.4c break-even: no taker buy. Rests no bid: a bid fills only after Up has fallen to it, and then the maths values Up at or below the bid, at every bid from 1c to 55c.

**Inputs**

| input | value |
|---|---|
| coin | BTC |
| window | 2026-09-17 21:15-21:30 UTC, 2nd quarter of the hour |
| decision time | 21:17 UTC: 2 min gone, 13 min left |
| 15m leg so far | +0.004% = +0.04 typical moves for the time left |
| typical move for the time left | 0.080% (without the snap-back or momentum adjustments, sigma × √time left: 0.086%) |
| typical one-hour swing, up or down (sigma, realised over the last 60 min) | 0.185% |
| last 12 15m candles, newest first | -0.064 +0.037 -0.240 +0.098 +0.055 -0.013 +0.029 +0.022 -0.015 -0.066 -0.153 +0.032 (%) |
| weight of each candle, newest first | 31.5 16.0 10.8 8.1 6.5 5.5 4.7 4.1 3.7 3.3 3.0 2.8 (%) |
| each candle's part of the stretch (weight × capped candle) | -0.020 +0.006 -0.025 +0.008 +0.004 -0.001 +0.001 +0.001 -0.001 -0.002 -0.005 +0.001 (%) |
| stretch (weighted average = sum of each candle's part above) | -0.033% |
| share pulled back before the close | 13.2% in a 2nd-quarter window, so the snap-back pull is -0.0043%. The snap-back is stronger later in the hour: 1st 9.0%, 2nd 13.2%, 3rd 19.1%, 4th 27.3% at this minute |
| hour's move so far | -0.061% (on its own it prices the hour Up at 34.9c) |
| 1h market Up price | 37c |
| drift implied by the 1h price | +0.012% an hour for the rest of the hour |
| trailing 1h spot trend | -0.123% an hour |
| blend weights | 1h market 63%, spot 37%, fitted on the other tape days' forecast errors (Sep 18, 19 and 20) |
| blended momentum | -0.038% an hour (how far off this estimate has been: 0.219% an hour, one sd) |
| momentum weight (theta) | -0.0434 (negative: fades the momentum) |
| time the momentum counts for (G) | 12.1 min = 0.201 h. A drift that builds up during the window is itself partly pulled back by the snap-back, so the 13 minutes left count as 12.1 |
| momentum push = theta × blended momentum × G | -0.0434 × -0.038% an hour × 0.201 h = +0.0003% |

**Probability waterfall**: each factor's fair share of the move from 50% (the same total whichever order you add them).

| step | points on Up | Up after | in typical moves for the time left |
|---|---|---|---|
| start |  | 50.0% |  |
| the 15m leg so far | +1.8 | 51.8% | +0.045 |
| the snap-back pull | +2.2 | 53.9% | +0.054 |
| the momentum push | +0.2 | 54.1% | +0.004 |
| net | +4.1 | 54.1% | +0.103 |

**Decision**

| | Up | Down |
|---|---|---|
| the maths' probability | 54.1% | 45.9% |
| 15m market price (last trade, a buy or a sell) | 56c | 44c |
| ask at the decision (last price a taker paid in the minute before 21:17) | 57c | 43c |
| first taker buy in the 30 s after 21:17 | 57.3c | 44c |
| taker break-even (a*) | 52.4c | 44.2c |

- The side the maths makes more likely: **Up** (54.1%).
- Taker: Up's ask of 57c is at or over the 52.4c break-even (52.4c plus the 1.75c fee at that price adds up to the maths' 54.1%): no taker buy.
- Maker: no bid is worth resting. A bid fills only after Up has fallen to it, and at every bid from 1c to 55c the maths then values Up at or below the bid (the best, 1c, gives -0.04c per share bid).

**Outcome**

- Up won: the maths' side won. Binance, which the maths reads, and Chainlink, which settles the market, both closed Up.

<sub>Market: `btc-updown-15m-1789679700`. Check: explain() p = 0.541082121042, step 2 p = 0.541082121042.</sub>

## (h) A taker buy that missed its limit

**SOL 2 min into the 21:00 window: the leg leads, Up 55.8%, the maths sends a buy of Up limited at 54.1c that misses**

> The +0.030% 15m leg adds 7.7 points to Up, the snap-back takes 1.1 off and the momentum takes 0.8 off.
>
> The maths makes Up 55.8% against Up's 57c market price: its taker buy limited at 54.1c does not fill, and no bid is worth resting.
>
> Down won: the maths had no position.

**The story**

- **Leg.** SOL is up 0.030% since the 21:00 window opened, with 13 of 15 minutes left (1st quarter of the hour). A typical move for the time left is 0.153%, from SOL's typical one-hour swing of 0.345% (up or down, realised over the last 60 min), so the leg is 0.19 typical moves, which adds 7.7 points to Up.
- **Snap-back.** Weighted toward the newest, the last twelve 15m candles average a stretch of +0.046%. The latest candle, +0.237% (1.4 typical 15m moves), carries 32% of the weight and alone gives +0.073%. In a 1st-quarter window, where the snap-back is weakest, the maths expects 9% of the stretch back before the close, which takes 1.1 points off Up.
- **Momentum.** There is no 1h market trade in the last minute, so momentum is the trailing hour on spot alone, +0.346% an hour; the maths fades it (weight -0.043), which takes 0.8 points off Up.
- **Trade.** Net: Up 55.8%, Down 44.2%. Sends a buy of Up limited at its 54.1c break-even; the next taker buy is 70c, so it does not fill. Rests no bid: a bid fills only after Up has fallen to it, and then the maths values Up at or below the bid, at every bid from 1c to 56c.

**Inputs**

| input | value |
|---|---|
| coin | SOL |
| window | 2026-09-17 21:00-21:15 UTC, 1st quarter of the hour |
| decision time | 21:02 UTC: 2 min gone, 13 min left |
| 15m leg so far | +0.030% = +0.19 typical moves for the time left |
| typical move for the time left | 0.153% (without the snap-back or momentum adjustments, sigma × √time left: 0.161%) |
| typical one-hour swing, up or down (sigma, realised over the last 60 min) | 0.345% |
| last 12 15m candles, newest first | +0.237 -0.237 +0.069 +0.237 +0.030 -0.079 +0.198 -0.030 -0.327 -0.148 -0.177 +0.000 (%) |
| weight of each candle, newest first | 31.5 16.0 10.8 8.1 6.5 5.5 4.7 4.1 3.7 3.3 3.0 2.8 (%) |
| each candle's part of the stretch (weight × capped candle) | +0.073 -0.037 +0.007 +0.019 +0.002 -0.004 +0.009 -0.001 -0.012 -0.005 -0.005 +0.000 (%) |
| stretch (weighted average = sum of each candle's part above) | +0.046% |
| share pulled back before the close | 9.0% in a 1st-quarter window, so the snap-back pull is +0.0042%. The snap-back is stronger later in the hour: 1st 9.0%, 2nd 13.2%, 3rd 19.1%, 4th 27.3% at this minute |
| hour's move so far | +0.030% (on its own it prices the hour Up at 53.5c) |
| 1h market Up price | no trade in the last minute |
| drift implied by the 1h price | none |
| trailing 1h spot trend | +0.346% an hour |
| blend weights | spot 100% (no 1h market trade in the last minute) |
| blended momentum | +0.346% an hour (how far off this estimate has been: 0.467% an hour, one sd) |
| momentum weight (theta) | -0.0434 (negative: fades the momentum) |
| time the momentum counts for (G) | 12.4 min = 0.206 h. A drift that builds up during the window is itself partly pulled back by the snap-back, so the 13 minutes left count as 12.4 |
| momentum push = theta × blended momentum × G | -0.0434 × +0.346% an hour × 0.206 h = -0.0031% |

**Probability waterfall**: each factor's fair share of the move from 50% (the same total whichever order you add them).

| step | points on Up | Up after | in typical moves for the time left |
|---|---|---|---|
| start |  | 50.0% |  |
| the 15m leg so far | +7.7 | 57.7% | +0.194 |
| the snap-back pull | -1.1 | 56.6% | -0.027 |
| the momentum push | -0.8 | 55.8% | -0.020 |
| net | +5.8 | 55.8% | +0.146 |

**Decision**

| | Up | Down |
|---|---|---|
| the maths' probability | 55.8% | 44.2% |
| 15m market price (last trade, a buy or a sell) | 57c | 43c |
| ask at the decision (last price a taker paid in the minute before 21:02) | 45c | 43c |
| first taker buy in the 30 s after 21:02 | 70c | 41c |
| taker break-even (a*) | 54.1c | 42.5c |

- The side the maths makes more likely: **Up** (55.8%).
- Taker: Up's ask of 45c is under the 54.1c break-even (54.1c plus the 1.74c fee at that price adds up to the maths' 55.8%). It buys with a limit at 54.1c, but the first taker buy after 21:02 was 70c, over the limit: no position.
- Maker: no bid is worth resting. A bid fills only after Up has fallen to it, and at every bid from 1c to 56c the maths then values Up at or below the bid (the best, 1c, gives -0.03c per share bid).

**Outcome**

- Down won: the maths' side lost. Binance, which the maths reads, and Chainlink, which settles the market, both closed Down.

<sub>Market: `sol-updown-15m-1789678800`. Check: explain() p = 0.558129448801, step 2 p = 0.558129448801.</sub>

## (i) A resting bid that did not fill

**BTC 2 min into the 21:45 window: the leg leads, Up 21.1%, the maths buys Down at 71c and bids Down at 70c**

> The -0.046% 15m leg takes 31.3 points off Up, the snap-back adds 2.8 and the momentum takes 0.4 off.
>
> The maths makes Down 78.9% against Down's 71c market price: it buys Down at 71c as a taker, and it rests a bid on Down at 70c.
>
> Down won: the taker buy made 27.6c a share and the resting bid did not fill.

**The story**

- **Leg.** BTC is down 0.046% since the 21:45 window opened, with 13 of 15 minutes left (4th quarter of the hour). A typical move for the time left is 0.052%, from BTC's typical one-hour swing of 0.132% (up or down, realised over the last 60 min), so the leg is 0.87 typical moves, which takes 31.3 points off Up.
- **Snap-back.** Weighted toward the newest, the last twelve 15m candles average a stretch of -0.016%. The latest candle, -0.046%, carries 32% of the weight and alone gives -0.014%. In a 4th-quarter window, where the snap-back is strongest, the maths expects 27% of the stretch back before the close, which adds 2.8 points to Up.
- **Momentum.** The hour is down 0.086% so far, which on its own prices Up at 8c; the 1h market pays 21c, so the crowd expects part of the drop to be won back (+0.169% an hour for the rest of the hour). Blended 63/37 with the trailing hour on spot (-0.075% an hour), momentum is +0.080% an hour; the maths fades it (weight -0.043), which takes only 0.4 points off Up, so it barely matters.
- **Trade.** Net: Up 21.1%, Down 78.9%. Buys Down at 71c against a 77.7c break-even. Rests a bid on Down at 70c, 1c under the last trade.

**Inputs**

| input | value |
|---|---|
| coin | BTC |
| window | 2026-09-17 21:45-22:00 UTC, 4th quarter of the hour |
| decision time | 21:47 UTC: 2 min gone, 13 min left |
| 15m leg so far | -0.046% = -0.87 typical moves for the time left |
| typical move for the time left | 0.052% (without the snap-back or momentum adjustments, sigma × √time left: 0.062%) |
| typical one-hour swing, up or down (sigma, realised over the last 60 min) | 0.132% |
| last 12 15m candles, newest first | -0.046 +0.069 -0.064 +0.037 -0.240 +0.098 +0.055 -0.013 +0.029 +0.022 -0.015 -0.066 (%) |
| weight of each candle, newest first | 31.5 16.0 10.8 8.1 6.5 5.5 4.7 4.1 3.7 3.3 3.0 2.8 (%) |
| each candle's part of the stretch (weight × capped candle) | -0.014 +0.011 -0.007 +0.003 -0.015 +0.005 +0.003 -0.001 +0.001 +0.001 -0.000 -0.002 (%) |
| stretch (weighted average = sum of each candle's part above) | -0.016% |
| share pulled back before the close | 27.3% in a 4th-quarter window, so the snap-back pull is -0.0043%. The snap-back is stronger later in the hour: 1st 9.0%, 2nd 13.2%, 3rd 19.1%, 4th 27.3% at this minute |
| hour's move so far | -0.086% (on its own it prices the hour Up at 8c) |
| 1h market Up price | 21c |
| drift implied by the 1h price | +0.169% an hour for the rest of the hour |
| trailing 1h spot trend | -0.075% an hour |
| blend weights | 1h market 63%, spot 37%, fitted on the other tape days' forecast errors (Sep 18, 19 and 20) |
| blended momentum | +0.080% an hour (how far off this estimate has been: 0.157% an hour, one sd) |
| momentum weight (theta) | -0.0434 (negative: fades the momentum) |
| time the momentum counts for (G) | 11.0 min = 0.184 h. A drift that builds up during the window is itself partly pulled back by the snap-back, so the 13 minutes left count as 11.0 |
| momentum push = theta × blended momentum × G | -0.0434 × +0.080% an hour × 0.184 h = -0.0006% |

**Probability waterfall**: each factor's fair share of the move from 50% (the same total whichever order you add them).

| step | points on Up | Up after | in typical moves for the time left |
|---|---|---|---|
| start |  | 50.0% |  |
| the 15m leg so far | -31.3 | 18.7% | -0.873 |
| the snap-back pull | +2.8 | 21.5% | +0.081 |
| the momentum push | -0.4 | 21.1% | -0.012 |
| net | -28.9 | 21.1% | -0.804 |

**Decision**

| | Up | Down |
|---|---|---|
| the maths' probability | 21.1% | 78.9% |
| 15m market price (last trade, a buy or a sell) | 29c | 71c |
| ask at the decision (last price a taker paid in the minute before 21:47) | 32c | 71c |
| first taker buy in the 30 s after 21:47 | 30c | 71c |
| taker break-even (a*) | 20c | 77.7c |

- The side the maths makes more likely: **Down** (78.9%).
- Taker: Down's ask of 71c is under the 77.7c break-even (77.7c plus the 1.21c fee at that price adds up to the maths' 78.9%). It buys with a limit at 77.7c: filled at 71c (the first taker buy after 21:47), fee 1.44c, expected profit +6.48c a share, full-Kelly size 23.5% of bankroll.
- Maker: rests a bid on Down at 70c, 1c under the 71c last trade. Expected profit per share bid is still rising there, at the top of the paper book's bid range, so the book's range sets this level, not a peak in the maths. Fill chance 96.5%: the maths' own estimate that Down trades down to 70c before the close. A fill only happens if the price falls to the bid, which means the market has moved against Down, so the maths marks Down down from 78.9% to 77.8% for a filled bid. Expected profit +7.51c per share bid; full-Kelly size 25.9% of bankroll.

**Outcome**

- Down won: the maths' side won. Binance, which the maths reads, and Chainlink, which settles the market, both closed Down.
- Taker: +27.6c a share after the fee; at full-Kelly size +$8.94 per $100 of bankroll.
- Maker: not filled. The lowest later Down price was 70c, and the paper book counts a fill only on a later trade below the bid, since its place in the queue at the bid is unknown.

<sub>Market: `btc-updown-15m-1789681500`. Check: explain() p = 0.210796956537, step 2 p = 0.210796956537.</sub>

## (j) A Down-side entry

**BTC 2 min into the 22:15 window: the leg leads, Up 33.6%, the maths bids Down at 62c**

> The -0.050% 15m leg takes 19.0 points off Up, the snap-back adds 2.3 and the momentum adds 0.3.
>
> The maths makes Down 66.4% against Down's 63c market price: Down's 65c ask is at or over its 64.8c break-even, so no taker buy, and it rests a bid on Down at 62c.
>
> Down won: the resting bid did not fill.

**The story**

- **Leg.** BTC is down 0.050% since the 22:15 window opened, with 13 of 15 minutes left (2nd quarter of the hour). A typical move for the time left is 0.103%, from BTC's typical one-hour swing of 0.238% (up or down, realised over the last 60 min), so the leg is 0.49 typical moves, which takes 19.0 points off Up.
- **Snap-back.** Weighted toward the newest, the last twelve 15m candles average a stretch of -0.048%. The latest candle, -0.064%, carries 32% of the weight and alone gives -0.020%. In a 2nd-quarter window, the maths expects 13% of the stretch back before the close, which adds 2.3 points to Up.
- **Momentum.** The hour is down 0.114% so far, which on its own prices Up at 28.5c; the 1h market pays 30c, so the crowd expects part of the drop to be won back (+0.012% an hour for the rest of the hour). Blended 63/37 with the trailing hour on spot (-0.237% an hour), momentum is -0.079% an hour; the maths fades it (weight -0.043), which adds only 0.3 points to Up, so it barely matters.
- **Trade.** Net: Up 33.6%, Down 66.4%. Down's 65c ask is at or over its 64.8c break-even: no taker buy. Rests a bid on Down at 62c, 1c under the last trade.

**Inputs**

| input | value |
|---|---|
| coin | BTC |
| window | 2026-09-17 22:15-22:30 UTC, 2nd quarter of the hour |
| decision time | 22:17 UTC: 2 min gone, 13 min left |
| 15m leg so far | -0.050% = -0.49 typical moves for the time left |
| typical move for the time left | 0.103% (without the snap-back or momentum adjustments, sigma × √time left: 0.111%) |
| typical one-hour swing, up or down (sigma, realised over the last 60 min) | 0.238% |
| last 12 15m candles, newest first | -0.064 -0.143 -0.046 +0.069 -0.064 +0.037 -0.240 +0.098 +0.055 -0.013 +0.029 +0.022 (%) |
| weight of each candle, newest first | 31.5 16.0 10.8 8.1 6.5 5.5 4.7 4.1 3.7 3.3 3.0 2.8 (%) |
| each candle's part of the stretch (weight × capped candle) | -0.020 -0.023 -0.005 +0.006 -0.004 +0.002 -0.011 +0.004 +0.002 -0.000 +0.001 +0.001 (%) |
| stretch (weighted average = sum of each candle's part above) | -0.048% |
| share pulled back before the close | 13.2% in a 2nd-quarter window, so the snap-back pull is -0.0063%. The snap-back is stronger later in the hour: 1st 9.0%, 2nd 13.2%, 3rd 19.1%, 4th 27.3% at this minute |
| hour's move so far | -0.114% (on its own it prices the hour Up at 28.5c) |
| 1h market Up price | 30c |
| drift implied by the 1h price | +0.012% an hour for the rest of the hour |
| trailing 1h spot trend | -0.237% an hour |
| blend weights | 1h market 63%, spot 37%, fitted on the other tape days' forecast errors (Sep 18, 19 and 20) |
| blended momentum | -0.079% an hour (how far off this estimate has been: 0.281% an hour, one sd) |
| momentum weight (theta) | -0.0434 (negative: fades the momentum) |
| time the momentum counts for (G) | 12.1 min = 0.201 h. A drift that builds up during the window is itself partly pulled back by the snap-back, so the 13 minutes left count as 12.1 |
| momentum push = theta × blended momentum × G | -0.0434 × -0.079% an hour × 0.201 h = +0.0007% |

**Probability waterfall**: each factor's fair share of the move from 50% (the same total whichever order you add them).

| step | points on Up | Up after | in typical moves for the time left |
|---|---|---|---|
| start |  | 50.0% |  |
| the 15m leg so far | -19.0 | 31.0% | -0.491 |
| the snap-back pull | +2.3 | 33.4% | +0.062 |
| the momentum push | +0.3 | 33.6% | +0.007 |
| net | -16.4 | 33.6% | -0.423 |

**Decision**

| | Up | Down |
|---|---|---|
| the maths' probability | 33.6% | 66.4% |
| 15m market price (last trade, a buy or a sell) | 37c | 63c |
| ask at the decision (last price a taker paid in the minute before 22:17) | 37c | 65c |
| first taker buy in the 30 s after 22:17 | 37c | 66c |
| taker break-even (a*) | 32.1c | 64.8c |

- The side the maths makes more likely: **Down** (66.4%).
- Taker: Down's ask of 65c is at or over the 64.8c break-even (64.8c plus the 1.60c fee at that price adds up to the maths' 66.4%): no taker buy.
- Maker: rests a bid on Down at 62c, 1c under the 63c last trade. Expected profit per share bid is still rising there, at the top of the paper book's bid range, so the book's range sets this level, not a peak in the maths. Fill chance 97.3%: the maths' own estimate that Down trades down to 62c before the close. A fill only happens if the price falls to the bid, which means the market has moved against Down, so the maths marks Down down from 66.4% to 65.3% for a filled bid. Expected profit +3.18c per share bid; full-Kelly size 8.6% of bankroll.

**Outcome**

- Down won: the maths' side won. Binance, which the maths reads, and Chainlink, which settles the market, both closed Down.
- Maker: not filled. The lowest later Down price was 63c, and the paper book counts a fill only on a later trade below the bid, since its place in the queue at the bid is unknown.

<sub>Market: `btc-updown-15m-1789683300`. Check: explain() p = 0.336313664064, step 2 p = 0.336313664064.</sub>

## Footnote: the formula and the parameters

p = Φ((y − R + Mo) / s). y is the 15m leg so far. R = share pulled back × stretch is the snap-back pull. Mo = theta × blended momentum × G is the momentum push. s = √(V + theta² × v × G²) is one typical move for the time left: V is the variance of spot's move over the time left after the snap-back, v the error variance of the blended momentum. Φ turns a number of typical moves into a probability. The waterfall splits p − 50% over y, R and Mo by exact Shapley values: each factor's effect averaged over every order of adding them.

Parameters, from `data/fade_1h_momentum_15m/params_pre_sep17.json`:

- theta -0.0434: the momentum weight. Negative fades the momentum, positive follows it.
- kappa0 0.3450: how hard the snap-back pulls at the top of the hour (a speed per hour).
- lam -1.6196: how that pull changes through the hour. The pull is kappa0 × e^(−lam × hours into the hour), so a negative lam makes it grow: 5.1× stronger at the end of the hour than at the start.
- alpha 0.9778: how fast older candles lose weight in the stretch. The latest candle carries 32% of the weight, the 2nd back 16%, the 12th back 3%.
- c 0.00932: the cap on one candle. Each candle enters as c × tanh(move / c), so a move well under 0.93% counts in full and no candle counts for more than 0.93%.
