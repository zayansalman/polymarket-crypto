# Fade 1h Momentum on 15m

| | |
|---|---|
| Name | chosen by Zayan (operator), 2026-09-21 |
| Concept | Zayan, 2026-09-21 |
| Maths | Claude, 2026-09-21 |
| Status | maths validated against simulation and the literature (section 9); historical test pre-registered (section 8); **not yet fitted** |
| Checks | `tools/fade_1h_momentum_15m/validate_math.py` |
| Living doc | `docs/strategies/fade_1h_momentum_15m.md` (in the dashboard at `/strategy-docs/fade_1h_momentum_15m`). This file is the dated research record: the full derivation and the pre-registration. |

## What Zayan described

- Take a position in the 15m Up/Down market from the 1h momentum, at an entry price the maths
  calculates live. A fixed price rule is no better than random.
- The 1h side should carry more weight in the last 15m of the hour than in the first.
- At the start of the hour, a 1h market at 60c and a 15m market at 55c on the same side is not
  a reason to buy: the 15m can still go the other way depending on its own momentum. Both
  momentums count, with importance that depends on where in the hour we are. Nothing is linear.
- Mean reversion belongs in the first one or two 15m windows of the hour and should decay as the
  hour goes on, while the 1h momentum takes over.
- Nothing may act as a gate. Side, price and size come out of one calculation.

## Notation

Time $s$ is in hours from the open of the hour, $s \in [0,1]$. Quarter $k = 1..4$ is the 15m market
on $[t_k, t_k + \tfrac14]$, $t_k = (k-1)/4$. $X(s) = \ln S(s)$ is the log spot price.

A decision is made at hour-time $t = t_k + \tau/4$, with $\tau \in [0,1)$ the fraction of the
quarter already gone. Known at that moment:

| symbol | meaning |
|---|---|
| $x_t = X(t) - X(0)$ | the hour's move so far |
| $y_\tau = X(t) - X(t_k)$ | the quarter's move so far |
| $h = (1-\tau)/4$ | hours left in the quarter |
| $r_{-j}$ | return of the $j$-th previous completed 15m candle (crosses into the previous hour) |
| $m_H(t)$ | the 1h market's price for Up |
| $\sigma^2$ | variance per hour: sum of squared 1-minute log returns over the trailing 60 minutes (the discretised quadratic variation $\int\sigma^2 ds$) |

The 15m market resolves Up iff $y_\tau + R \ge 0$, where $R = X(t+h) - X(t)$.

## 1. The price process

$$dX = \big[\mu - \kappa(s)\,(X - a_t)\big]\,ds + \sigma\,dW, \qquad \kappa(s) = \kappa_0\, e^{-\lambda s}.$$

- $\mu$: drift per hour (the momentum).
- $\kappa(s)$: speed of mean reversion, strongest at the top of the hour and decaying through it
  (Zayan's decaying mean reversion).
- $a_t$: the level price reverts toward, set at the decision so that $X(t) - a_t = M_t$, the
  **stretch**:

$$M_t = \sum_{j=1}^{12} w_j \; c\tanh\!\left(\frac{r_{-j}}{c}\right), \qquad w_j = \frac{j^{-\alpha}}{\sum_{i=1}^{12} i^{-\alpha}} .$$

The last three hours of 15m candles, weighted by a decaying lag kernel, each soft-clipped by
$\tanh$. This form is taken from the literature (section 9): crypto reversion at 15m is a
lag-one **sign** effect that saturates with size, so a linear pull proportional to the move
would overstate it after large moves. Because the previous candle can belong to the previous
hour, the first quarter of the hour has reversion too.

**Exact solution.** The mean satisfies the linear ODE $\dot m = \mu - \kappa(s)(m - a)$. With the
integrating factor $e^{K(t,u)}$, $K(t,u) = \int_t^u \kappa = \tfrac{\kappa_0}{\lambda}(e^{-\lambda t} - e^{-\lambda u})$,
and writing $A = \kappa_0/\lambda$, $w_1 = e^{-\lambda t}$, $w_2 = e^{-\lambda(t+h)}$:

$$E[R] = -\big(1 - e^{-K}\big)\,M_t + \mu\,G, \qquad G = \int_t^{t+h} e^{-(K(t,t+h)-K(t,u))}du = \frac{e^{A w_2}}{\lambda}\big[E_1(A w_2) - E_1(A w_1)\big],$$

$$\mathrm{Var}[R] = V = \sigma^2\!\int_t^{t+h} e^{-2(K(t,t+h)-K(t,u))}du = \frac{\sigma^2 e^{2A w_2}}{\lambda}\big[E_1(2A w_2) - E_1(2A w_1)\big],$$

with $K = K(t,t+h)$ and $E_1$ the exponential integral (substitute $w = e^{-\lambda u}$). As
$\kappa_0 \to 0$, $G \to h$ and $V \to \sigma^2 h$: plain Brownian motion. Reversion moves the
mean **and** shrinks the variance of what is left, so it sharpens the probability, not only
shifts it.

## 2. What the 1h market price says about the drift

The crowd prices the hour as a binary option on Brownian motion. Given the realised $x_t$:

$$m_H(t) = \Phi\!\left(\frac{x_t + \mu(1-t)}{\sigma\sqrt{1-t}}\right)
\quad\Longrightarrow\quad
\hat\mu_H(t) = \frac{\sigma\sqrt{1-t}\;\Phi^{-1}(m_H) - x_t}{1-t}.$$

This strips out the part of the hour that has already happened. The same 60c means a different
thing at :00 (all expectation) and at :45 (mostly realised move).

Quote noise alone, by the delta method ($z = \Phi^{-1}(m_H)$, $s_m$ = half the spread):
$v_H^{quote} = \big(\sigma / (\sqrt{1-t}\,\varphi(z))\big)^2 s_m^2$. This is a floor, not the full
error: the crowd's forecast error is estimated from data in section 4.

## 3. The spot's own momentum

The trailing return over $L$ hours, $\hat\mu_L = r_L / L$, with sampling variance $\sigma^2/L$.
$L = 1$ is the previous hour's return, what Zayan reads as "1h momentum".

## 4. Blending the two momentum estimates

Choose weights to minimise the variance of the blended error. For two estimates with error
variances $v_H, v_L$ and covariance $c_{HL}$, setting $\frac{d}{dw}\mathrm{Var} = 0$:

$$w_H = \frac{v_L - c_{HL}}{v_H + v_L - 2c_{HL}}, \qquad \hat\mu = w_H\hat\mu_H + (1-w_H)\hat\mu_L,$$
$$\hat v = \frac{v_H v_L - c_{HL}^2}{v_H + v_L - 2c_{HL}} .$$

$v_H, v_L, c_{HL}$ are the variances and covariance of each estimate's **historical forecast
error**, refit walk-forward. The covariance cannot be dropped: the 1h market already contains
the trailing spot move, so the two errors are correlated. Equal weights are reported beside the
fitted ones, because estimated weights often lose to 50/50 out of sample.

## 5. Probability the quarter closes Up

$$\boxed{\;p = \Phi\!\left(\frac{y_\tau - \big(1-e^{-K}\big)M_t + \theta\hat\mu\,G}{\sqrt{V + \theta^2\hat v\,G^2}}\right)\;}$$

$\theta$ scales the momentum. $\theta < 0$ fades the 1h momentum, $\theta > 0$ follows it, $\theta = 0$
is the martingale. It is fitted, not assumed; the strategy's name records the hypothesis that
it comes out negative.

What the formula does in Zayan's cases (momentum part isolated: $\theta = 1$, $\kappa_0 = 0$):

| situation | result |
|---|---|
| :00, 1h market at 60c | $p = \Phi(\Phi^{-1}(0.60)/2) =$ **0.55**. Exactly the example: nothing to buy at 55c. |
| Weight on the 1h price, general | $\theta\sigma G / (\sqrt{1-t}\sqrt{V})$; without reversion $\tfrac{1}{2\sqrt{1-t}}$ = **0.50, 0.58, 0.71, 1.00** at :00, :15, :30, :45. Nonlinear, from the $\sqrt{\ }$ scaling of Brownian variance. |
| :45, hour already up 0.3%, 1h still 60c, $\sigma = 0.5\%$ | implied last-quarter drift $-0.24\%$, $p =$ **0.17**. A 60c hour is bearish for the last quarter here: the crowd is pricing a give-back. |
| Late in a quarter, $\tau \to 1$ | $p \to \Phi(y_\tau/(\sigma\sqrt h))$: the quarter's own move takes over (the delta of a digital near expiry). |
| Mean reversion | pulls back the weighted recent stretch by the fraction $1 - e^{-K}$: largest at the top of the hour, fading at rate $\lambda$. |

## 6. Entry price: an output, not a threshold

**Taker.** Per share at price $a$ with fee $f = 0.07$ and fee per share $c = f a(1-a)$:
$EV(a) = p - a - c$. The most we would ever pay solves $EV = 0$:

$$a^* = \frac{(1+f) - \sqrt{(1+f)^2 - 4fp}}{2f}\qquad (p = 0.55 \to 53.3c,\;\; 0.60 \to 58.3c,\;\; 0.70 \to 68.5c).$$

Size by Kelly, from $\frac{d}{df}E[\ln W] = 0$: $\;f^* = \dfrac{p - a - c}{1 - a - c}$, zero when there is no edge.

**Maker** (Zayan's standing rule: rest bids under the price, never cross). A bid at $b$ fills when
the crowd's price comes down to $b$. In spot terms the quarter's move must reach the boundary

$$B(h') = \sigma\sqrt{h'}\;\Phi^{-1}(b) - \hat\mu_H\,h',$$

where $h'$ is the time left **at the moment of the fill**. The boundary moves as the window runs
down, so the fill probability $P_{fill}(b)$ is a curved-boundary crossing probability, computed
with the Wang–Pötzelberger piecewise-linear method (exact in the limit). The fixed-level
reflection formula is not used: it misses by up to 8 points on deep bids (section 9).

If it fills, our probability is re-evaluated at the fill state, $p_{fill}(b)$; adverse selection is
inside it, not a haircut. The resting price maximises

$$J(b) = P_{fill}(b)\,\big(p_{fill}(b) - b\big), \qquad J'(b^*) = 0 \text{ (solved by Brent's method)},$$

and maker Kelly is $f^* = (p_{fill} - b)/(1-b)$ with no fee. Ladder rungs sit across the 5–15c band
under the price, each sized by Kelly at its own $p_{fill}$.

No gate anywhere: side from $\mathrm{sign}(p - \tfrac12)$, price $a^*$ or $b^*$, size Kelly. All are
continuous in the state.

## 7. Parameters

| parameter | meaning | how |
|---|---|---|
| $\sigma$ | vol per $\sqrt{\text{hour}}$ | trailing 60-min realised variance, every minute |
| $\theta$ | momentum: sign = follow or fade | maximum likelihood |
| $\kappa_0, \lambda$ | size of mean reversion and its decay through the hour | maximum likelihood |
| $\alpha, c$ | lag-kernel decay and soft-clip scale | maximum likelihood |
| $v_H, v_L, c_{HL}$ | blend weights | forecast-error moments, walk-forward |
| $L$ | spot momentum window | 1h pre-registered; 30m and 2h as context |

Likelihood $\mathcal{L} = \sum_i [y_i\ln p_i + (1-y_i)\ln(1-p_i)]$ over quarter outcomes; maximised
with analytic gradients.

## 8. Historical test (pre-registered)

**Data.** Binance 1-minute spot, BTC/ETH/SOL/XRP, 2026-03-01 to 2026-09-20 (quarter close vs
open as the outcome proxy). Polymarket 15m tape `data/wallet_research/m15.db` (1,136 markets,
Sep 17–20, real resolutions). 1h market tape `data/wallet_research/wallets.db` (BTC/ETH/XRP,
same days).

**Step 0, reproduce the literature.** On our Binance data, betting against the previous 15m
candle's sign should give an AUC near 0.53 (Kitron & Wengrowicz report 0.533 BTC, 0.538 ETH,
0.536 XRP). If it does not, our data or code is wrong and nothing downstream is read.

**Step 1, spot only, walk-forward by month.** Fit $(\theta,\kappa_0,\lambda,\alpha,c)$ on months
before $M$, score $M$. Log-loss and Brier against the martingale baseline $p = \Phi(y_\tau/(\sigma\sqrt h))$.
Report fitted values per fold: does reversion really decay through the hour ($\lambda > 0$), and
does $\theta$ come out negative (fade) or positive (follow)?

**Step 2, Polymarket tape.** Decision at $\tau \in \{0, \tfrac1{15}, \tfrac2{15}, \tfrac3{15}, \tfrac5{15}\}$,
headline $\tfrac2{15}$ (where Zayan enters). Calibration of $p$ against real resolutions, with the
market's own calibration beside it. Taker: buy where the ask is below $a^*$, c/share net of fee.
Maker: rest $b^*$; a fill counts only if a later print on that token is strictly below $b^*$
(queue position unknown, stated). Equal-weighted per market, $n$, mean, $t$ clustered by window
(the four coins move together), Wilson intervals. No pass/fail label.

The question step 2 answers: on spot the 15m reversion is too small to trade (1.3bp gross vs
5bp cost). A binary pays on the **sign**, which is where the effect lives. If the Polymarket
crowd does not already price it, a ~1.5% sign edge is ~1.5c/share at 50c, and a maker pays no
fee. If the crowd does price it, the edge is zero and this will show it.

**Comparisons:** the model is judged only against the market's own price and the no-edge
(martingale) baseline, on the same rows.

*Amendment, 2026-09-21 (Zayan): an earlier draft also scored price-threshold rules (1h side at
≥ 40c and ≥ 55c) as baselines. Removed: there are no rules or thresholds in this strategy, not
even as comparisons. The maths decides.*

## 9. Validation (2026-09-21)

**Every closed form against simulation** (`validate_math.py`, seed 20260921):

| check | closed form | simulation |
|---|---|---|
| hour binary, $P(\text{Up})$ | 0.8711 | 0.8711 |
| drift recovered from the price | 0.004000 | true 0.004 |
| 60c hour → first quarter | 0.5504 | 0.5504 |
| :45 case → last quarter | 0.1719 | 0.1722 (hour re-prices to 0.6003) |
| reversion mean, $t=0$ | −0.000683 | −0.000689 |
| reversion variance, $t=0$ | 4.357e-6 | 4.359e-6 (Brownian would be 6.25e-6) |
| first passage, drifted BM | 0.7309 with Broadie–Glasserman–Kou shift | 0.7326 discrete |
| taker break-even $a^*$ | EV$(a^*)$ = 0 to $10^{-16}$ | — |
| Kelly with fee | 0.1217 | 0.1217 (argmax of $E\ln W$) |

**Corrected from the first draft:**

1. Mean reversion was a first-order expansion; it was 31% off on the mean at $\kappa_0 h = 0.75$ and
   ignored the variance reduction. Replaced by the exact exponential-integral solution.
2. Reversion was anchored at the hour's open, which gives **zero** reversion in the first quarter,
   against both the literature and Zayan's ask. Now a decaying lag kernel over previous 15m
   candles with a soft clip.
3. Kelly denominator was $1-a$; correct is $1-a-c$ (0.1171 vs 0.1217).
4. Maker fill used a fixed level; the level moves with time left. Fixed-level vs true:
   0.598 vs 0.676 for a bid 20c under, 0.783 vs 0.797 at 10c under.
5. Blend weights assumed the two momentum errors independent; they are not. Now
   covariance-aware, estimated from forecast errors, equal weights reported beside.
6. $\theta$ was restricted to $[0,1]$, which could not express a fade.

**Each component against the literature:**

| component | source | what it establishes |
|---|---|---|
| price = probability, binary on Brownian motion, martingale forecasts | Taleb, *Quantitative Finance* 2019, [arXiv 1703.06351](https://arxiv.org/abs/1703.06351); Wolfers & Zitzewitz, [NBER w12200](https://www.nber.org/papers/w12200) | a binary forecast is a Bachelier-style digital option and must be a martingale; market prices ≈ mean beliefs, with biases |
| √t variance scaling, first passage, reflection principle | Karatzas & Shreve, *Brownian Motion and Stochastic Calculus* (1991) | standard |
| OU with time-varying speed, integrating factor | Vasicek 1977; Hull & White 1990 | standard |
| 15m reversion in crypto | Kitron & Wengrowicz 2026, [arXiv 2608.21888](https://arxiv.org/abs/2608.21888) | 90% of 183 Binance pairs out of sample, every coin-year since 2021; lag-one **sign** effect that grows with move size; weaker at 1h, gone by 4h; not tradeable on spot (1.3bp vs 5bp) |
| quarter-hour boundaries | Kim & Hansen 2026, [arXiv 2607.09426](https://arxiv.org/abs/2607.09426) | activity bursts at :00/:15/:30/:45, strongest at the top of the hour; negative 1-minute autocorrelation across each boundary; boundary order flow reverses over the next half hour |
| intraday momentum and reversal | Gao, Han, Li & Zhou, *JFE* 129 (2018); Wen, Bouri, Xu & Zhao, *NAJEF* 62 (2022) | early-period return predicts late-period return; in Bitcoin both momentum and reversal exist |
| blending forecasts | Bates & Granger 1969; Granger & Ramanathan 1984; Wang, Hyndman et al., [arXiv 2205.04216](https://arxiv.org/abs/2205.04216) | minimum-variance weights; equal weights often win out of sample |
| barrier crossing | Broadie, Glasserman & Kou, *Math. Finance* 1997; Wang & Pötzelberger, *J. Appl. Prob.* 1997 | discrete-monitoring correction; curved boundaries |
| resting-order price | Avellaneda & Stoikov, *Quant. Finance* 2008; [arXiv 2607.17991](https://arxiv.org/abs/2607.17991); [arXiv 2403.02572](https://arxiv.org/abs/2403.02572) | fill probability × margin trade-off; prediction-market making; fill probabilities |
| sizing | Kelly 1956; Thorp 2006; [arXiv 2412.14144](https://arxiv.org/abs/2412.14144) | maximise expected log wealth; Kelly in prediction markets |

**Where Zayan's ideas sit against the literature.** Mean reversion at 15m: strongly supported.
Fading the 1h move: supported but weaker (1h reversal is about a third the size of 15m). Decay of
reversion through the hour: not measured anywhere; directionally consistent with the top of the
hour being the strongest boundary. $\lambda$ will say.

## 10. Assumptions that could be wrong

1. Gaussian increments. Crypto minutes are fat-tailed; a Student-t variant if tail calibration
   is poor.
2. The 1h market is priced efficiently when read. Thin books make stale quotes, which are bias,
   not noise.
3. The crowd's price as a function of spot (used for the maker fill boundary) is our model of
   the crowd, not the crowd. The tape test measures real fills.
4. Binance stands in for Chainlink in steps 0–1; step 2 uses real resolutions.
5. Reversion decaying with hour position is a hypothesis; the lag kernel is the documented part.

## 11. Results

Pending.
