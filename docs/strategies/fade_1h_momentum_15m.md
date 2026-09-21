# Fade 1h Momentum on 15m

<!-- BEGIN GENERATED:strategy -->
| | |
|---|---|
| Name | Fade 1h Momentum on 15m |
| Key | `fade_1h_momentum_15m` |
| Status | cannot trade |
| Switch | none — nothing to turn on |
| Code | `tools/fade_1h_momentum_15m/` — 3 files |
| Code fingerprint | `91319a8794bc` |
<!-- END GENERATED:strategy -->

## At a glance

### Concept

Zayan's idea: take the 15-minute position from the 1-hour momentum at a price the maths calculates live. No price rules anywhere. The hour's side should count for more late in the hour than early. Mean reversion matters in the first windows and fades as the hour goes on. The entry price is the output of a calculation. The name records the hypothesis that the fitted momentum weight $\theta$ comes out negative, so the model fades; if it comes out positive, the same model follows.

### Main assumption

The log price is Brownian motion with a drift, plus an Ornstein–Uhlenbeck pull whose strength decays through the hour. The 1-hour market is priced efficiently when read, so its price can be inverted for the drift. Increments are Gaussian, and Binance stands in for Chainlink, which settles these markets.

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

Research only: nothing is wired to trade it, and none of $\theta$, $\kappa_0$, $\lambda$, $\alpha$ or $c$ is fitted yet.

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
a mean reversion that is strongest at the top of the hour and fades as the hour goes on. The
output is a probability for the window, and from it the price to rest a bid at and how much to
put there. Nothing in it is a threshold or a gate.

It is research only: nothing is wired to trade it yet.

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

None is fitted yet. The fit and its test are pre-registered in the research write-up.

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
- Binance stands in for Chainlink, which settles the 15m markets.
- That reversion decays through the hour is Zayan's hypothesis. The lag-kernel reversal is
  documented in the literature; its decay by hour position is not.
- On spot, the 15m reversal is too small to trade (1.3bp gross vs 5bp cost). It can only pay
  here if a binary, which pays on the sign, is priced without it — the historical test decides.

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

- 2026-09-21 · `91319a8794bc` · Added an At a glance summary (concept, main assumption, maths, how it works, how it was derived, references) for the dashboard's STRATEGY card.
- 2026-09-21 · `91319a8794bc` · Status moved from offline only to cannot trade: the offline-only status was removed (#273). Nothing is wired to trade it yet.
- 2026-09-21 · `a3ca60e9bd16` · Lint only: removed an unused import from threshold_scan.py. No change to the analysis.
- 2026-09-21 · `8e64a661d470` · Doc created: Zayan's concept, the maths as validated against simulation and the literature, and the two analyses that started it. Historical test pending.
