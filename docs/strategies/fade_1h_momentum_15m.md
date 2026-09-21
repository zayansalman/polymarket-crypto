# Fade 1h Momentum on 15m

<!-- BEGIN GENERATED:strategy -->
| | |
|---|---|
| Name | Fade 1h Momentum on 15m |
| Key | `fade_1h_momentum_15m` |
| Status | cannot trade |
| Switch | none — nothing to turn on |
| Code | `tools/fade_1h_momentum_15m/` — 11 files |
| Code fingerprint | `096e99578fbf` |
<!-- END GENERATED:strategy -->

## At a glance

### Concept

Zayan's idea: take the 15-minute position from the 1-hour momentum at a price the maths calculates live. No price rules anywhere. The hour's side should count for more late in the hour than early. He expected mean reversion to matter most in the first windows and fade as the hour goes on; the test found the opposite, it grows toward the end of the hour. The entry price is the output of a calculation. The name records the hypothesis that the fitted momentum weight $\theta$ comes out negative, so the model fades; if it comes out positive, the same model follows.

### Main assumption

The log price is Brownian motion with a drift, plus an Ornstein–Uhlenbeck pull whose strength changes through the hour at a fitted rate (on the data it grows). The 1-hour market is priced efficiently when read, so its price can be inverted for the drift. Increments are Gaussian, and Binance stands in for Chainlink, which settles these markets.

### The maths

The price process, with $s$ in hours from the top of the hour:

$$dX=\big[\mu-\kappa(s)\,(X-a_t)\big]\,ds+\sigma\,dW,\qquad \kappa(s)=\kappa_0e^{-\lambda s}$$

The stretch it reverts from, a decaying kernel over the last twelve 15m candles, each soft-clipped:

$$M_t=\sum_{j=1}^{12}w_j\,c\tanh\!\Big(\frac{r_{-j}}{c}\Big),\qquad w_j\propto j^{-\alpha}$$

The drift the 1h price implies, given the hour's move so far $x_t$:

$$\hat\mu_H=\frac{\sigma\sqrt{1-t}\;\Phi^{-1}(m_H)-x_t}{1-t}$$

The chance the window closes Up, with $y_\tau$ the window's move so far:

$$p=\Phi\!\left(\frac{y_\tau-(1-e^{-K})\,M_t+\theta\hat\mu\,G}{\sqrt{V+\theta^2\hat v\,G^2}}\right)$$

The taker break-even price, with fee rate $f=0.07$:

$$a^*=\frac{(1+f)-\sqrt{(1+f)^2-4fp}}{2f}$$

$K$, $G$ and $V$ are the exact integrals of the decaying pull over the time left. The full doc derives them.

### How it works

At a decision time inside one of the hour's four windows:

1. Read the hour's move $x_t$, the window's move $y_\tau$, the last twelve 15m returns, and $\sigma$ from the last 60 one-minute returns.
2. Invert the 1h market's price for the drift, and blend it with the trailing spot return using minimum-variance weights.
3. Compute $p$ for the window.
4. As taker, buy below $a^*$. As maker, rest the bid that maximises fill probability times edge. Size by Kelly.

Not wired to trade yet. Fitted on Binance data before 2026-09-17: $\theta = -0.043$ (a very slight fade), the snap-back pull grows through the hour ($\lambda = -1.62$). Being built into the app as a paper strategy that re-learns these live.

### How it was derived

- **2026-09-21, Zayan.** Pulled his 18 recent manual trades, placed by hand from the 1h momentum. Four were right, for −$9.61. The opposite side would have made +$12.07.
- **Claude.** Measured how the 1h momentum side paid on 1,088 settled 15m markets, by the price it traded at (`threshold_scan.py`). It lost at low prices and paid at high ones; always fading it lost (−7.84c, t = −3.33).
- **Zayan** set out the concept above. **Claude** formalised it as Brownian motion with a decaying Ornstein–Uhlenbeck pull, and the entry price as an optimisation.
- A Monte Carlo check of every closed form (`validate_math.py`) found six errors in the first draft, all fixed. The literature changed the reversion term to a soft-clipped lag kernel (Kitron & Wengrowicz 2026).

### References

- Full derivation and the pre-registered historical test: `tasks/2026-09-21-fade-1h-momentum-on-15m.md`
- Scripts: `tools/fade_1h_momentum_15m/manual_trades_flip.py`, `threshold_scan.py`, `validate_math.py`
- Binaries as options on Brownian motion: Taleb, *Quantitative Finance* 2019, [arXiv 1703.06351](https://arxiv.org/abs/1703.06351)
- 15m reversal in crypto: Kitron & Wengrowicz 2026, [arXiv 2608.21888](https://arxiv.org/abs/2608.21888)
- Time-varying Ornstein–Uhlenbeck: Vasicek 1977; Hull & White 1990
- Boundary crossing: Wang & Pötzelberger, *J. Appl. Prob.* 1997
- Forecast combination: Bates & Granger 1969
- Kelly sizing: Kelly 1956; Thorp 2006

## What it does

Prices each 15-minute Up/Down window from a model of how the price moves through the hour. The
inputs are the 1h market's price, the spot's trailing return, the window's own move so far, and
a snap-back whose strength changes through the hour (on the data it grows toward the end). The
output is a probability for the window, and from it the price to rest a bid at and how much to
put there. Nothing in it is a threshold or a gate.

Not wired to trade yet: it is being built into the app as a paper strategy.

## How it was formed

- **2026-09-21, Zayan (operator).** Asked to pull his recent manual trades on the idea that he
  was wrong in most of them, so the opposite side might be right. He had been placing them by
  hand from the 1h momentum and its side. Of 18 settled trades since 2026-09-13, 4 were right:
  −$9.61 on $20.39 staked. The opposite
  side of each would have made +$12.07. Four of the losses were one correlated window: BTC, ETH,
  XRP and DOGE all bought Up at Sep 20 3:45–4:00PM ET, and all four fell.
- **2026-09-21, Claude.** Looked at 1,088 settled 15m markets instead of 18 trades
  (`threshold_scan.py`): how the 1h momentum side paid, by the price it traded at. It lost at
  low prices and paid at high ones; always taking the other side lost (table under Evidence).
  A flat fade was not supported.
- **2026-09-21, Zayan.** Set out the concept: no rules or thresholds anywhere — the maths
  decides the side, the entry price and the size. The 1h side should weigh more in the last
  15m of the hour than the first. At the start of the hour, a 1h market at 60c with the 15m at 55c on the same
  side is not a buy on its own, because the 15m can still turn on its own momentum. Both
  momentums count, with importance that depends on where in the hour the window sits. Mean
  reversion belongs in the first one or two windows and should decay as the 1h momentum takes
  over. No gates: the entry price must be the result of a calculation, using calculus.
- **2026-09-21, Claude.** Formalised it as a Brownian-motion model of the hour with a decaying
  Ornstein–Uhlenbeck pull, and the entry price as the solution of an optimisation.
- **2026-09-21, Zayan.** Asked for the maths to be checked against arXiv and proven methods.
  A Monte Carlo check of every closed form, and a literature pass, found six errors in the
  first draft, all fixed (listed under Evidence). The literature also changed the mean-reversion
  term: crypto 15m reversal is a lag-one sign effect that saturates with move size (Kitron &
  Wengrowicz 2026), so the pull is now a soft-clipped, decaying kernel over the last twelve 15m
  candles rather than a linear pull toward the hour's open.
- **2026-09-21, Zayan.** Named it Fade 1h Momentum on 15m. The name records the hypothesis
  that the fitted momentum coefficient $\theta$ comes out negative. If it comes out positive,
  the same model follows the momentum instead.

## How it works

Time $s$ is in hours from the top of the hour. Window $k = 1..4$ covers $[t_k, t_k + \tfrac14]$.
$X = \ln S$ is the log spot price. At a decision time $t$ inside window $k$, with $h$ hours left
in it:

| symbol | meaning |
|---|---|
| $x_t = X(t) - X(0)$ | the hour's move so far (1h momentum and side) |
| $y_\tau = X(t) - X(t_k)$ | the window's move so far (15m momentum) |
| $r_{-j}$ | return of the $j$-th previous completed 15m candle, crossing into the previous hour |
| $m_H$ | the 1h market's price for Up |
| $\sigma^2$ | variance per hour from the last 60 one-minute returns |

**Price process.** The drift is the momentum; the second term is mean reversion whose speed
changes through the hour ($\lambda > 0$: fades; $\lambda < 0$: grows):

$$dX = \big[\mu - \kappa(s)\,(X - a_t)\big]\,ds + \sigma\,dW, \qquad \kappa(s) = \kappa_0 e^{-\lambda s}.$$

The level it reverts from is the **stretch** $M_t = X(t) - a_t$, a decaying kernel over the last
twelve 15m candles, each soft-clipped:

$$M_t = \sum_{j=1}^{12} w_j \, c\tanh\!\left(\frac{r_{-j}}{c}\right), \qquad w_j \propto j^{-\alpha}.$$

**Exact moments of the rest of the window.** With $K = \int_t^{t+h}\kappa = \tfrac{\kappa_0}{\lambda}(e^{-\lambda t} - e^{-\lambda(t+h)})$,
$A = \kappa_0/\lambda$, $w_1 = e^{-\lambda t}$, $w_2 = e^{-\lambda(t+h)}$ and $E_1$ the exponential integral:

$$E[R] = -(1 - e^{-K})\,M_t + \mu\,G, \qquad G = \frac{e^{A w_2}}{\lambda}\big[E_1(A w_2) - E_1(A w_1)\big],$$

$$V = \mathrm{Var}[R] = \frac{\sigma^2 e^{2A w_2}}{\lambda}\big[E_1(2A w_2) - E_1(2A w_1)\big].$$

Reversion both shifts the mean and narrows the spread. With $\kappa_0 = 0$ it is Brownian motion.

**What the 1h price says about the drift.** The hour is a binary option on the same path, so
inverting its price removes the part of the hour already realised:

$$\hat\mu_H = \frac{\sigma\sqrt{1-t}\;\Phi^{-1}(m_H) - x_t}{1-t}.$$

**Blending the two momentum estimates** $\hat\mu_H$ (1h market) and $\hat\mu_L$ (trailing spot return)
with weights that minimise the variance of the blended error, allowing for their correlation:

$$w_H = \frac{v_L - c_{HL}}{v_H + v_L - 2c_{HL}}, \qquad \hat\mu = w_H\hat\mu_H + (1 - w_H)\hat\mu_L .$$

**Probability the window closes Up:**

$$p = \Phi\!\left(\frac{y_\tau - (1 - e^{-K})\,M_t + \theta\hat\mu\,G}{\sqrt{V + \theta^2\hat v\,G^2}}\right).$$

What that does in the cases Zayan described (momentum part only):

| situation | result |
|---|---|
| top of the hour, 1h market at 60c | window worth **55c**: nothing to buy at 55c |
| weight on the 1h price at :00, :15, :30, :45 | **0.50, 0.58, 0.71, 1.00** — from $\tfrac{1}{2\sqrt{1-t}}$, the $\sqrt{}$ scaling of Brownian variance |
| :45, hour already up 0.3%, 1h still 60c | last window worth **17c**: the crowd is pricing a give-back |
| late in a window | the window's own move takes over |

**Entry price.** Taker break-even, fee $f = 0.07$, solves $p - a - fa(1-a) = 0$:

$$a^* = \frac{(1+f) - \sqrt{(1+f)^2 - 4fp}}{2f}.$$

Resting bid (Zayan's standing rule: rest under the price, never cross): the bid $b$ maximises
$J(b) = P_{fill}(b)\,(p_{fill}(b) - b)$, where $P_{fill}$ is the probability the path crosses the
moving boundary $B(h') = \sigma\sqrt{h'}\,\Phi^{-1}(b) - \hat\mu_H h'$ before expiry (Wang–Pötzelberger)
and $p_{fill}$ is our probability re-evaluated at the fill. Size by Kelly: $f^* = (p - a - c)/(1 - a - c)$
as taker with fee $c$ per share, $(p_{fill} - b)/(1 - b)$ as maker. Side, price and size are all
continuous in the inputs.

## Parameters

| parameter | meaning | how it is set |
|---|---|---|
| $\sigma$ | volatility per $\sqrt{\text{hour}}$ | trailing 60 one-minute returns, every minute |
| $\theta$ | momentum: negative fades, positive follows | maximum likelihood, walk-forward |
| $\kappa_0, \lambda$ | size of mean reversion, and how it changes through the hour | maximum likelihood |
| $\alpha, c$ | lag-kernel decay and soft-clip scale | maximum likelihood |
| $v_H, v_L, c_{HL}$ | blend weights | variances and covariance of each estimate's forecast errors |

**Fitted values** used by the historical test, from `data/fade_1h_momentum_15m/params_pre_sep17.json`
(written by `step1_walkforward.py`). Fit window: Binance 1-minute spot, BTC/ETH/SOL/XRP, 15m
windows opening 2026-03-01 00:00 to 2026-09-16 23:45 UTC (384,000 rows, 19,200 windows). One set
for all four coins, maximum likelihood, $\lambda$ free in sign. z is clustered by window.

| parameter | value | z | reading |
|---|---|---|---|
| $\theta$ | −0.043 | −2.00 | fades the momentum estimate, slightly |
| $\kappa_0$ | 0.345 per hour | 2.52 | reversion speed at the top of the hour |
| $\lambda$ | −1.62 | −3.34 | negative: reversion speeds up through the hour, to $\kappa$ = 1.74 per hour by :60 |
| $\alpha$ | 0.978 | 6.89 | lag weights fall off roughly as $1/j$ |
| $c$ | 0.0093 | 2.72 | soft-clip scale: a 0.93% 15m move |

**Blend weights** (`data/fade_1h_momentum_15m/step2.json`, `blend`): estimated on the tape's four
days, leaving each day out when scoring it. Weight on the 1h market's implied drift $w_H$ = 0.63,
0.84, 0.51 and 0.69 for Sep 17, 18, 19 and 20; 0.66 over all four days (descriptive only).

**Sensitivities, not used in the test** (same params file). With $\lambda \ge 0$: $\theta$ −0.040,
$\kappa_0$ 0.948, $\lambda$ 0 (on the bound), $\alpha$ 0.899, $c$ 0.0131. With a separate volatility
scale for each quarter of the hour: $\theta$ +0.007 (z 0.23), $\kappa_0$ 1.056, $\lambda$ −0.494 (z −1.31),
$\alpha$ 0.862, $c$ 0.0094, scales 1.14, 1.07, 1.13, 0.96.

## Worked examples

Real decisions from the Sep 17–20 Polymarket tape, run through the maths fitted before Sep 17, at minute 2 of each window. Each card walks through every factor behind the trade. Five of the ten cards are here; all ten are in `docs/strategies/examples/fade_1h_momentum_15m.md`, regenerated by `tools/fade_1h_momentum_15m/examples.py`.

### How to read a card

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
- **Resting bid and fill chance** (b*): the maths looks for the bid with the most expected profit per share bid (fill chance × edge), from 1c up to 1c under the last trade. The paper book keeps a bid at least 1c under the last trade so that it rests rather than buys at once, and it rests one bid per window, with no ladder. When expected profit is still rising at the top of that range, the bid sits 1c under the last trade: the book sets that level, not a peak in the maths. Every resting bid in these cards sits there. On the whole minute-2 tape, all 521 of step 2's 521 resting bids sit there too. The fill chance is the maths' own estimate that the price trades down to the bid before the close. A fill only happens when the price falls to the bid, so the maths marks its side lower for a filled bid.
- **Size**: full Kelly, the bankroll share that grows money fastest for that edge, from the maths' probability and the entry price. Taker and maker are two separate paper books compared side by side, not stacked on the same window. Each card gives the size in its Decision bullets.

The waterfall shows each factor's fair share of the move from 50% (the same total whichever order you add them).

The strategy has no price rules: side, entry price and size all come out of the one calculation.

The formula and the fitted parameters are in the footnote at the end.

### How the examples were picked

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

### (a) Top of the hour: the maths and the market agree

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

### (b) Late in the hour after a fast 15m up leg: the snap-back leans against it

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

### (c) The 1h market disagrees with the hour's move

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

### (f) A resting bid that filled

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

### (j) A Down-side entry

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

## Evidence so far

### Historical test (2026-09-21)

Pre-registered in the research write-up (section 8) and run after two adversarial reviews. Full
tables, every deviation and the open review findings are in its section 11. Sources:
`data/fade_1h_momentum_15m/step0.json`, `step1.json`, `step2.json`; the martingale taker and maker
are a write-up check in `writeup_same_rows_vs_martingale.json`. t is clustered by 15m window. The
model is judged only against the market's own price and the martingale $\Phi(y/\sigma\sqrt h)$, on
the same rows.

**Step 0, the 15m reversal on our Binance data** (2026-03-01 to 09-20, 19,584 bars per coin).
Betting against the previous candle's sign: AUC 0.518 BTC, 0.522 ETH, 0.519 SOL, 0.513 XRP
(t 3.35 to 6.49). The paper's own score, a 12-lag logit, out of sample: 0.526, 0.542, 0.527 for
BTC, ETH, XRP against its 0.533, 0.538, 0.536.

**Step 1, spot only, walk-forward by month** (April to Sep 20, 332,160 out-of-sample rows):

| model | log-loss vs martingale | t |
|---|---|---|
| momentum only | −0.00082 | −2.39 |
| reversion only | −0.00198 | −4.05 |
| full | −0.00197 | −3.89 |

- $\theta$ is negative in 6 of 6 folds, −0.043 (z −2.00) on the frozen fit. Adding it to
  reversion changes log-loss by +0.00001 (t 0.15).
- Reversion does not decay through the hour. By quarter, at minute 0:

| | Q1 | Q2 | Q3 | Q4 |
|---|---|---|---|---|
| share of the stretch pulled back, as fitted ($\lambda$ −1.62, z −3.34) | 0.10 | 0.15 | 0.21 | 0.30 |
| same, volatility scaled per quarter ($\lambda$ −0.49, z −1.31) | 0.24 | 0.27 | 0.30 | 0.33 |
| AUC of betting against the previous candle, n 19,584 each | 0.500 | 0.518 | 0.512 | 0.540 |

**Step 2, Polymarket tape** (Sep 17–20, 1,136 markets). Headline minute 2: 1,071 rows in 273
windows.

| same 1,071 rows | log-loss | Brier |
|---|---|---|
| model | 0.6402 | 0.2242 |
| market price | 0.6330 | 0.2216 |
| martingale | 0.6398 | 0.2243 |

Model − market +0.0072 (t 0.87); model − martingale +0.0004 (t 0.11).

| minute 2 | model | martingale, same rows and rules |
|---|---|---|
| taker entries | 355 | 253 |
| taker c/share over the price paid, net of fee (t) | +1.38 (0.44) | −0.89 (−0.24) |
| taker c per candidate row, n 1,071 (t) | +0.46 (0.44) | −0.21 (−0.24) |
| maker quotes / fills | 521 / 429 | 495 / 405 |
| maker c per quote, 0 if unfilled (t) | −2.69 (−1.08) | −2.30 (−0.95) |
| maker c per candidate row, n 1,071 (t) | −1.31 (−1.08) | −1.06 (−0.95) |

- Model minus martingale per candidate row: taker +0.67c (t 1.19), maker −0.24c (t −0.38).
- The model expected +6.7c a share on its taker entries and made +1.4c. Its maker expected 97%
  fills and a 63.5% win rate on them; it got 82% and 53%, at a mean bid of 0.564.
- At minute 0 the market already leaned 1.25c against the previous candle (n 871, t 6.57) and
  the model 2.23c. The outcome went against the previous candle in 49.3% of those rows.

### Before the test

**Zayan's trades** (`manual_trades_flip.py`, settled 2026-09-21): 18 trades, 4 right, −$9.61
actual vs +$12.07 on the opposite side. On 15m only: 8 trades, −$2.34 vs +$7.41.

**Following the 1h momentum side on 1,088 15m markets** (`threshold_scan.py`, BTC/ETH/SOL/XRP,
Sep 17–20, net of taker fee, one row per market):

| price band | n | c/share | t |
|---|---|---|---|
| 0.30–0.40 | 190 | −8.52 | −2.62 |
| 0.40–0.50 | 309 | −5.77 | −2.06 |
| 0.50–0.60 | 285 | −1.36 | −0.46 |
| 0.60–0.70 | 166 | +2.01 | +0.55 |
| 0.70–0.80 | 62 | +10.64 | +2.36 |


**The maths** (`validate_math.py`): every closed form matches a simulation of the process it
describes. The six errors the check found in the first draft, all fixed:

1. First-order reversion was 31% off on the mean and ignored the variance; replaced by the
   exact exponential-integral solution.
2. Reversion toward the hour's open is zero in the first window; replaced by the lag kernel.
3. Kelly denominator was $1 - a$; correct is $1 - a - c$.
4. Maker fill assumed a fixed level; the level moves with time left (off by up to 8 points).
5. Blend assumed the two momentum errors were independent; now covariance-aware.
6. $\theta$ was limited to $[0, 1]$ and could not fade.

## Known weaknesses

- One 3.5-day window of Polymarket data: 1,071 rows in 273 windows at the headline minute. No
  headline t in step 2 reaches 2.
- Gaussian increments; crypto minutes are fat-tailed.
- The 1h market is assumed efficiently priced when read. Thin books go stale.
- Binance stands in for Chainlink, which settles the 15m markets. On the tape, 55 of the 1,071
  minute-2 rows resolved against Binance's direction, and they carry most of the model's
  log-loss gap to the market (+0.0051 of +0.0072).
- Reversion does not decay through the hour. On 2026 Binance data it is weakest in the first
  quarter and strongest in the last, so the fitted $\lambda$ is negative. How much of that is
  reversion and how much is volatility differing by quarter is not settled: $\lambda$ is −1.62 as
  fitted and −0.49 (z −1.31) with a volatility scale per quarter.
- The fade is small and fragile. $\theta$ is −0.043 (z −2.00), adds nothing out of sample once
  reversion is in, and turns +0.007 with a volatility scale per quarter. On the tape it changed
  the side on 6 of 1,069 rows.
- $\theta$ was fitted with the trailing spot return as the momentum, then applied on the tape to
  the blend of the 1h market and the spot return.
- The model is overconfident. Its taker entries expected +6.7c a share and made +1.4c. Its
  rows at $p \ge 0.8$ (n 49) won 69% against a mean $p$ of 0.85.
- The maker's fill model understates adverse selection: 82% of bids filled against 97%
  modelled, and fills won 53% against the 63.5% the model expected at the fill.
- On spot, the 15m reversal is too small to trade (1.3bp gross vs 5bp cost). On the tape the
  crowd already prices part of it: at minute 0 the market leaned 1.25c against the previous
  candle (n 871, t 6.57). Over Sep 17–20 the reversal did not show: 49.3% of those windows
  went against the previous candle.

## Sources

- Concept and name: Zayan (operator), 2026-09-21. His trades:
  [0xc1daaec036a8a49e4a71cad2daa51dcb19bb00c5](https://polymarket.com/profile/0xc1daaec036a8a49e4a71cad2daa51dcb19bb00c5).
- Research write-up with the full derivation, validation table and pre-registered historical
  test: `tasks/2026-09-21-fade-1h-momentum-on-15m.md`.
- Scripts: `tools/fade_1h_momentum_15m/manual_trades_flip.py`, `threshold_scan.py`,
  `validate_math.py`.
- Data: `data/wallet_research/m15.db` (1,136 settled 15m markets, Sep 17–20);
  `data/wallet_research/wallets.db` (1h markets, same days); Binance 1-minute klines.
- Prediction-market prices as probabilities, binaries as options on Brownian motion:
  Taleb, *Quantitative Finance* 2019, [arXiv 1703.06351](https://arxiv.org/abs/1703.06351);
  Wolfers & Zitzewitz, [NBER w12200](https://www.nber.org/papers/w12200).
- 15m reversal in crypto: Kitron & Wengrowicz 2026, [arXiv 2608.21888](https://arxiv.org/abs/2608.21888).
- Quarter-hour boundaries: Kim & Hansen 2026, [arXiv 2607.09426](https://arxiv.org/abs/2607.09426).
- Intraday momentum and reversal: Gao, Han, Li & Zhou, *JFE* 129 (2018),
  [link](https://www.sciencedirect.com/science/article/abs/pii/S0304405X18301351); Wen, Bouri,
  Xu & Zhao, *NAJEF* 62 (2022), [link](https://www.sciencedirect.com/science/article/abs/pii/S1062940822000833).
- Brownian motion, first passage, reflection principle: Karatzas & Shreve, *Brownian Motion and
  Stochastic Calculus* (1991). Time-varying Ornstein–Uhlenbeck: Vasicek 1977; Hull & White 1990.
- Barrier crossing: Broadie, Glasserman & Kou, *Math. Finance* 1997,
  [link](https://onlinelibrary.wiley.com/doi/abs/10.1111/1467-9965.00035); Wang & Pötzelberger,
  *J. Appl. Prob.* 1997, [link](https://www.cambridge.org/core/journals/journal-of-applied-probability/article/abs/boundary-crossing-probability-for-brownian-motion/5D79C4BAC345AEDB816C901544B0236D).
- Forecast combination: Bates & Granger 1969; review [arXiv 2205.04216](https://arxiv.org/abs/2205.04216).
- Resting-order price: Avellaneda & Stoikov, *Quant. Finance* 2008; prediction-market making
  [arXiv 2607.17991](https://arxiv.org/abs/2607.17991); fill probabilities [arXiv 2403.02572](https://arxiv.org/abs/2403.02572).
- Kelly: Kelly 1956; Thorp 2006, [pdf](https://gwern.net/doc/statistics/decision/2006-thorp.pdf);
  prediction markets [arXiv 2412.14144](https://arxiv.org/abs/2412.14144).

## Changelog

- 2026-09-22 · `096e99578fbf` · Settlement correction (Zayan): the 15m market settles on a Chainlink TWAP, not on the close. Added the average-price probability with one switch between the two rules (task doc section 1b), checked by simulation (52 checks, largest |z| 2.43). On the tape, the real resolutions follow the TWAP-60s value at the close against its value before the open (96.8% of 1,136 markets) far better than a 15-minute average (85.2%). Nothing refitted; the maths on this page still describes the close.
- 2026-09-22 · `4a7e98a879d9` · Added the test code (data, model, steps 0-2, explain, examples) and five worked examples in a trader's words; corrected the text to what the test found: the snap-back grows through the hour, the 1h momentum adds almost nothing, no price rules anywhere.
- 2026-09-22 · `a5c0e596af30` · Historical test results added (Step 0-2 evidence, fitted parameters, updated weaknesses): at minute 2 the market's price scored better than the model (log-loss +0.0072, t 0.87), the taker made +1.4c/share (n 355, t 0.44) and the maker -2.7c/quote (n 521, t -1.08); the martingale on the same rows made -0.9c and -2.3c.
- 2026-09-21 · `91319a8794bc` · Added an At a glance summary (concept, main assumption, maths, how it works, how it was derived, references) for the dashboard's STRATEGY card.
- 2026-09-21 · `91319a8794bc` · Status moved from offline only to cannot trade: the offline-only status was removed (#273). Nothing is wired to trade it yet.
- 2026-09-21 · `a3ca60e9bd16` · Lint only: removed an unused import from threshold_scan.py. No change to the analysis.
- 2026-09-21 · `8e64a661d470` · Doc created: Zayan's concept, the maths as validated against simulation and the literature, and the two analyses that started it. Historical test pending.
