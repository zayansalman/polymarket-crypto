# Fade 1h Momentum on 15m

<!-- BEGIN GENERATED:strategy -->
| | |
|---|---|
| Name | Fade 1h Momentum on 15m |
| Key | `fade_1h_momentum_15m` |
| Status | offline only |
| Switch | none — nothing to turn on |
| Code | `tools/fade_1h_momentum_15m/` — 10 files |
| Code fingerprint | `a5c0e596af30` |
<!-- END GENERATED:strategy -->

## What it does

Prices each 15-minute Up/Down window from a model of how the price moves through the hour. The
inputs are the 1h market's price, the spot's trailing return, the window's own move so far, and
a mean reversion that is strongest at the top of the hour and fades as the hour goes on. The
output is a probability for the window, and from it the price to rest a bid at and how much to
put there. Nothing in it is a threshold or a gate.

It is research only: nothing is wired to trade it yet.

## How it was formed

- **2026-09-21, Zayan (operator).** Asked to pull his recent manual trades on the idea that he
  was wrong in most of them, so the opposite side might be right. His rule had been: read the
  1h momentum and its side, and take the 15m position on that side if it cost at least 40c.
  Of 18 settled trades since 2026-09-13, 4 were right: −$9.61 on $20.39 staked. The opposite
  side of each would have made +$12.07. Four of the losses were one correlated window: BTC, ETH,
  XRP and DOGE all bought Up at Sep 20 3:45–4:00PM ET, and all four fell.
- **2026-09-21, Claude.** Checked the rule on 1,088 settled 15m markets instead of 18 trades
  (`threshold_scan.py`). Following the 1h momentum side lost below about 50c and made money
  from about 55c up; fading it was significantly negative (table under Evidence). The bulk
  data did not support a flat fade.
- **2026-09-21, Zayan.** Set out the concept: take the 15m position from the 1h momentum at a
  computed price, not a fixed 40c. The 1h side should weigh more in the last 15m of the hour
  than the first. At the start of the hour, a 1h market at 60c with the 15m at 55c on the same
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
decays through the hour:

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
| $\kappa_0, \lambda$ | size of mean reversion, and its decay through the hour | maximum likelihood |
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

At prices of 55c and up: following +4.66c (t = 1.98, n = 369), fading −7.84c (t = −3.33).

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

- Concept, the 40c rule and the name: Zayan (operator), 2026-09-21. His trades:
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

- 2026-09-22 · `a5c0e596af30` · Historical test results added (Step 0-2 evidence, fitted parameters, updated weaknesses): at minute 2 the market's price scored better than the model (log-loss +0.0072, t 0.87), the taker made +1.4c/share (n 355, t 0.44) and the maker -2.7c/quote (n 521, t -1.08); the martingale on the same rows made -0.9c and -2.3c.
- 2026-09-21 · `8e64a661d470` · Doc created: Zayan's concept, the maths as validated against simulation and the literature, and the two analyses that started it. Historical test pending.
