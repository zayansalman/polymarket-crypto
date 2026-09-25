# Fade 1h Momentum on 15m

<!-- BEGIN GENERATED:strategy -->
| | |
|---|---|
| Name | Fade 1h Momentum on 15m |
| Key | `fade_1h_momentum_15m` |
| Status | running now |
| Switch | `fade_1h_momentum_15m` on the MY STRATEGIES card |
| Code | `polymarket_bot/fade_1h_momentum_15m/` — 9 files |
| Code fingerprint | `32a1484ce579` |
<!-- END GENERATED:strategy -->

## At a glance

### Concept

Zayan's idea: take the 15-minute position from the 1-hour momentum at a computed price, not a fixed 40c. The hour's side should count for more late in the hour than early. Mean reversion matters in the first windows and fades as the hour goes on. There are no gates: the entry price is the output of a calculation. The name records the hypothesis that the fitted momentum weight $\theta$ comes out negative, so the model fades; if it comes out positive, the same model follows.

### Main assumption

The log price is Brownian motion with a drift, plus an Ornstein–Uhlenbeck pull whose strength decays through the hour. The 1-hour market is priced efficiently when read, so its price can be inverted for the drift. Increments are Gaussian. A 15m window settles Up when the Chainlink TWAP-60s print at its close (the average of the last 60 s) is at least the print at its open, and the model prices exactly that.

### The maths

The price process, with $s$ in hours from the top of the hour:

$$dX=\big[\mu-\kappa(s)\,(X-a_t)\big]\,ds+\sigma\,dW,\qquad \kappa(s)=\kappa_0e^{-\lambda s}$$

The stretch it reverts from, a decaying kernel over the last twelve 15m candles, each soft-clipped:

$$M_t=\sum_{j=1}^{12}w_j\,c\tanh\!\Big(\frac{r_{-j}}{c}\Big),\qquad w_j\propto j^{-\alpha}$$

The drift the 1h price implies, given the hour's move so far $x_t$:

$$\hat\mu_H=\frac{\sigma\sqrt{1-t}\;\Phi^{-1}(m_H)-x_t}{1-t}$$

The chance the window settles Up: $d$ is the price now against the opening print, $\bar a$ the part of the closing minute's average already printed ($\epsilon$ of it gone, $\ell$ to come):

$$p_{\text{model}}=\Phi\!\left(\frac{\epsilon\,\bar a+\ell\,d-B\,M_t+\theta\hat\mu\,\bar G}{\sqrt{\sigma^2\Psi+\theta^2\hat v\,\bar G^2}}\right)$$

The chance traded on follows the market $m$ as much as the settled windows say it should:

$$p=\Phi\big(w_M\,\Phi^{-1}(m)+w_S\,\Phi^{-1}(p_{\text{model}})\big)$$

$B$, $\bar G$ and $\Psi$ are exact integrals of the decaying pull over the closing minute. The full doc derives them.

### How it works

At a decision time inside one of the hour's four windows:

1. Read the hour's move $x_t$, the price now against the window's opening print, the closing minute's average so far, the last twelve 15m returns, and $\sigma$ from the last 60 one-minute returns.
2. Invert the 1h market's price for the drift, and blend it with the trailing spot return using minimum-variance weights.
3. Compute $p_{\text{model}}$ for the window, and blend it with the 15m market's own price (the market anchor) to get $p$. The side is where $p$ leans.
4. Simulate 2,000 paths of the fitted process to the window's end. A rung of the ladder fills when the market's price comes down to it; the chance of winning is re-read at that moment, so a fill that comes from the price moving against us counts against the rung.
5. Rest bids only, never crossing: a ladder under the ask, each rung sized by Kelly, with the four coins sized together because they move together. Hedge a position that has turned against us with a resting bid on the other side.
6. After every settled window, move the dials one step toward what the result says (recursive maximum likelihood, about two days of memory).

In the app now (paper only): every minute it does all of the above for BTC, ETH, SOL and XRP and rests paper bids where the maths says they pay. It starts from dials fitted on the Sep 17–20 tape: the market and the model each carry about half the weight ($w_M = 0.45$, $w_S = 0.57$) and the four coins move together ($\rho = 0.75$).

### How it was derived

- **2026-09-21, Zayan.** Pulled his 18 recent manual trades on the old rule (follow the 1h side on 15m if it costs 40c or more). Four were right, for −$9.61. The opposite side would have made +$12.07.
- **Claude.** Checked the rule on 1,088 settled 15m markets (`threshold_scan.py`). Following the 1h side lost below about 50c and made money from about 55c up (+4.66c, t = 1.98). Fading it lost (−7.84c, t = −3.33).
- **Zayan** set out the concept above. **Claude** formalised it as Brownian motion with a decaying Ornstein–Uhlenbeck pull, and the entry price as an optimisation.
- A Monte Carlo check of every closed form (`validate_math.py`) found six errors in the first draft, all fixed. The literature changed the reversion term to a soft-clipped lag kernel (Kitron & Wengrowicz 2026).

### References

- Full derivation and the pre-registered historical test: `tasks/2026-09-21-fade-1h-momentum-on-15m.md`
- Sizing, ladder, hedge and market anchor: `tasks/2026-09-22-fade-1h-sizing-hedging.md`
- Code in the app: `polymarket_bot/fade_1h_momentum_15m/` (the model hook is `decide.py`)
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
a mean reversion that is strongest at the top of the hour and fades as the hour goes on. The
output is a probability for the window, and from it the price to rest a bid at and how much to
put there. Nothing in it is a threshold or a gate.

It runs in the app as a paper strategy (switch `fade_1h_momentum_15m` on the MY STRATEGIES
card). Every minute it:

- reads each of BTC, ETH, SOL and XRP's current 15m window: both books with their depth, the
  1h market's price, the Chainlink TWAP-60s, Chainlink and Binance prices, the window's price
  to beat (the TWAP-60s print at the open, which the window settles against), the known part
  of the closing minute's average, and Binance candles for $\sigma$ and the 15m returns;
- prices the window with the model (the chance it settles Up on the TWAP-60s print at the
  close), follows the market as far as the dials say, and picks the side;
- simulates 2,000 paths of the price to the window's end to get each rung's chance of filling
  and of winning once filled, and a hedge quote at the other side's best bid;
- rests a ladder of paper bids 1–15c under the ask (Settings), sized by Kelly across the four
  coins together (half Kelly by default, on a 100 USD starting paper bankroll), and hedges a
  held position with a resting bid on the other side when the odds have turned;
- writes one sentence per coin for the card, for example: "BTC's 15m leg is up 0.25% with
  13 min left; the snap-back from the last candles barely moves it; the hour's momentum adds
  little; the market has Up at 41c against the model's 83%, so the chance traded on is 67%;
  the maths rests an Up bid at 41c.";
- keeps checking fills, settles every window from the venue, traded or not, and after each
  settled window moves the dials one step toward the result (a new dials version each time).

It never crosses the spread and pays no fee. A paper bid fills only when the real trade tape
reaches it through the queue in front of it. There is no live order path: with LIVE selected
it places nothing and says so on the card.

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

**Probability the window closes Up** (the research's first version, settling on the close):

$$p = \Phi\!\left(\frac{y_\tau - (1 - e^{-K})\,M_t + \theta\hat\mu\,G}{\sqrt{V + \theta^2\hat v\,G^2}}\right).$$

**What the app prices: the TWAP-60s settlement** (verified 2026-09-22 on the live markets and
1,136 settled ones). A window settles Up iff the TWAP-60s print at its close, the average of the
last 60 s, is at least the print at its open (Gamma's priceToBeat). With $d$ the price now
against that opening print, $\bar a$ the closing minute's average so far, $\epsilon$ of the
minute gone and $\ell$ to come:

$$p_{\text{model}} = \Phi\!\left(\frac{\epsilon\,\bar a + \ell\,d - B\,M_t + \theta\hat\mu\,\bar G}{\sqrt{\sigma^2\Psi + \theta^2\hat v\,\bar G^2}}\right),$$

where $B$, $\bar G$ and $\Psi$ are the pull, momentum and noise of the section 1 process integrated
over the closing minute (section 1b of the research write-up). The app's code
(`model.py`) is a standard-library port of the research code, checked against it to 1e-9 on
about 200 cases.

**Following the market.** $p = \Phi\big(w_M\,\Phi^{-1}(m) + w_S\,\Phi^{-1}(p_{\text{model}})\big)$, with
$m$ the 15m market's Up mid. If the model adds nothing, $w_S \to 0$ and nothing trades.

What the close version does in the cases Zayan described (momentum part only):

| situation | result |
|---|---|
| top of the hour, 1h market at 60c | window worth **55c**: nothing to buy at 55c |
| weight on the 1h price at :00, :15, :30, :45 | **0.50, 0.58, 0.71, 1.00** — from $\tfrac{1}{2\sqrt{1-t}}$, the $\sqrt{}$ scaling of Brownian variance |
| :45, hour already up 0.3%, 1h still 60c | last window worth **17c**: the crowd is pricing a give-back |
| late in a window | the window's own move takes over |

**Entry price.** Taker break-even, fee $f = 0.07$, solves $p - a - fa(1-a) = 0$:

$$a^* = \frac{(1+f) - \sqrt{(1+f)^2 - 4fp}}{2f}.$$

Resting bids (Zayan's standing rule: rest under the price, never cross). The rungs are every
cent from 1c to 15c under the side's ask. For each, 2,000 simulated paths of the fitted process
give $P_{fill}$, the chance the market's price for that side comes down to the rung before the
window ends (the market's price along a path is a drift plus Brownian noise under the same
settlement, set to equal today's mid), and $q_{fill}$, our $p$ re-evaluated at the fill. The
ladder's stakes maximise the expected log of the bankroll over "exactly the first $k$ rungs
filled, then won or lost"; a rung whose $q_{fill}$ does not beat its price gets nothing. The
four coins are sized together through a one-factor Gaussian copula with correlation $\rho$,
and a held position is hedged with $h^* = \max\big(0, \frac{(1-p')(1-b_o)(W+n) - p' b_o W}{b_o(1-b_o)}\big)$
shares of the other side at its best bid, $p'$ the held side's chance given the hedge fills.
Side, price and size are all continuous in the inputs.

## Parameters

| parameter | meaning | how it is set |
|---|---|---|
| $\sigma$ | volatility per $\sqrt{\text{hour}}$ | trailing 60 one-minute returns, every minute |
| $\theta$ | momentum: negative fades, positive follows | maximum likelihood, walk-forward |
| $\kappa_0, \lambda$ | size of mean reversion, and its decay through the hour | maximum likelihood |
| $\alpha, c$ | lag-kernel decay and soft-clip scale | maximum likelihood |
| $v_H, v_L, c_{HL}$ | blend weights | variances and covariance of each estimate's forecast errors |
| $w_M, w_S$ | how far to follow the market, and the model | maximum likelihood on settled windows |
| $\rho$ | how the four coins move together | maximum likelihood of a one-factor Gaussian copula |

Starting values (dials version 1, source `fit_sep17_20`): $\theta = -0.043$, $\kappa_0 = 0.345$,
$\lambda = -1.62$, $\alpha = 0.98$, $c = 0.0093$ from the research's fit on Binance minutes,
Mar 1 – Sep 16 (they describe how the price moves, so they carry over to the TWAP-60s
settlement); $w_M = 0.45$ and $w_S = 0.57$ (standard errors 0.25 and 0.24; 1,071 windows in 273
slots) and $\rho = 0.75$ (0.03; 273 slots) fitted on the Sep 17–20 tape with the TWAP-60s
settlement at minute 2. On that tape the fitted blend's log-likelihood is −675.2 against −678.0
for the market alone and −676.8 for the model alone. A quick fit for starting values, not the
historical re-test. The blend's error moments are the tape's all-four-days values.

Every settled window then moves all eight dials one bounded step (recursive maximum likelihood
with forgetting; about two days of windows carry half the weight). Version 0, the prior, follows
the market exactly and is never updated.

## Evidence so far

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

- One 3.5-day window of Polymarket data. The t-statistics are near 2, not proof.
- Gaussian increments; crypto minutes are fat-tailed.
- The 1h market is assumed efficiently priced when read. Thin books go stale.
- The starting anchor and $\rho$ come from 3.5 days of tape, with the Binance minute before the
  open standing in for the opening TWAP-60s print. The live learner corrects them as windows
  settle, but its first days lean on that fit.
- The market's price along a simulated path is a drift and Brownian noise calibrated to today's
  mid; a real book can gap past a rung. Paper fills come from the real tape, so the record shows
  the difference.
- The four coins are sized as if every bet wins together; when one coin's bid is Up and
  another's is Down, that overstates how they move together and sizes them smaller than needed.
- That reversion decays through the hour is Zayan's hypothesis. The lag-kernel reversal is
  documented in the literature; its decay by hour position is not.
- On spot, the 15m reversal is too small to trade (1.3bp gross vs 5bp cost). It can only pay
  here if a binary, which pays on the sign, is priced without it — the historical test decides.

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

- 2026-09-22 · `32a1484ce579` · The model is plugged in, so it now bids on paper. decide.py prices each window with model.py, a standard-library port of the research model for the TWAP-60s settlement (checked against the research code to 1e-9 on 197 cases), follows the market with fitted anchor weights, and gets each ladder rung's fill and win chances and the hedge quotes from 2,000 simulated paths. Starting dials (version 1) fitted on the Sep 17-20 tape: w_M 0.45, w_S 0.57, rho 0.75. learner.py moves every dial one bounded step after each settled window (recursive maximum likelihood, about two days of memory).
- 2026-09-22 · `47989d06aa7c` · Wired into the app as a running paper strategy (switch fade_1h_momentum_15m; Settings group Fade 1h Momentum on 15m): each minute it records every coin's inputs, checks fills and settles every window; the model hook returns nothing yet, so no bids are placed. Code moved to polymarket_bot/fade_1h_momentum_15m/. Start reference is now the window's priceToBeat (TWAP-60s print at the open), per the verified settlement rule.
- 2026-09-21 · `91319a8794bc` · Added an At a glance summary (concept, main assumption, maths, how it works, how it was derived, references) for the dashboard's STRATEGY card.
- 2026-09-21 · `91319a8794bc` · Status moved from offline only to cannot trade: the offline-only status was removed (#273). Nothing is wired to trade it yet.
- 2026-09-21 · `a3ca60e9bd16` · Lint only: removed an unused import from threshold_scan.py. No change to the analysis.
- 2026-09-21 · `8e64a661d470` · Doc created: Zayan's concept, the maths as validated against simulation and the literature, and the two analyses that started it. Historical test pending.
