# Fade 1h Momentum on 15m

| | |
|---|---|
| Name | chosen by Zayan (operator), 2026-09-21 |
| Concept | Zayan, 2026-09-21 |
| Maths | Claude, 2026-09-21 |
| Status | maths validated against simulation and the literature (section 9); historical test pre-registered (section 8), fitted and run 2026-09-21 after two adversarial reviews: results, deviations and open findings in section 11; never traded |
| Checks | `tools/fade_1h_momentum_15m/validate_math.py` |
| Living doc | `docs/strategies/fade_1h_momentum_15m.md` (in the dashboard at `/strategy-docs/fade_1h_momentum_15m`). This file is the dated research record: the full derivation and the pre-registration. |

## What Zayan described

- Take a position in the 15m Up/Down market from the 1h momentum, at an entry price that is
  computed, not a fixed number like 40c.
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

**Comparisons:** the model is judged only against the market's own price and the no-edge (martingale) baseline, on the same rows.

*Amendment, 2026-09-21 (Zayan): an earlier draft also scored price-threshold rules (1h side at ≥ 40c and ≥ 55c) as baselines. Removed: there are no rules or thresholds in this strategy, not even as comparisons. The maths decides.*

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

Run 2026-09-21, after two adversarial reviews of the code. Every number is from
`data/fade_1h_momentum_15m/step0.json`, `step1.json` and `step2.json` (scripts in
`tools/fade_1h_momentum_15m/`). The martingale taker and maker are a write-up check, not
pre-registered: `data/fade_1h_momentum_15m/writeup_same_rows_vs_martingale.json`. t is clustered by
15m window unless a line says otherwise. Score differences are a − b per row: negative means a
scored better.

**In short**

- **Fade or follow.** The fitted $\theta$ is negative, so the model fades the momentum, but by
  very little: −0.043 (z −2.00). Out of sample it adds nothing once reversion is in (+0.00001
  log-loss per row, t 0.15). With a separate volatility scale per quarter of the hour it is
  +0.007 (z 0.23). On the tape it moves $p$ by 0.3c on average and changes the side on 6 of
  1,069 rows.
- **Reversion through the hour.** Reversion is real and helps out of sample (−0.0020 log-loss
  per row against the martingale, t −4.05, 332,160 rows). It does not decay through the hour. It
  is weakest in the first quarter and strongest in the last: betting against the previous candle
  has AUC 0.500 in quarter 1 and 0.540 in quarter 4. Fitted $\lambda$ = −1.62 (z −3.34), so
  reversion grows. Part of that is volatility differing by quarter: with a per-quarter volatility
  scale, $\lambda$ = −0.49 (z −1.31).
- **Calibration.** At minute 2 on the tape (1,071 rows, 273 windows) the market's own price
  scored better than the model (log-loss +0.0072 per row, t 0.87). The model scored level with
  the martingale (+0.0004, t 0.11).
- **Taker, minute 2.** +1.38c a share over the price paid (355 entries, t 0.44). The martingale
  through the same rules on the same rows: −0.89c (253 entries, t −0.24). Per candidate row,
  model minus martingale: +0.67c (n 1,071, t 1.19).
- **Maker, minute 2.** −2.69c per quote (521 quotes, t −1.08), −3.27c per filled share (429
  fills). The martingale through the same rules on the same rows: −2.30c per quote (495 quotes,
  t −0.95). Per candidate row, model minus martingale: −0.24c (n 1,071, t −0.38).

### Step 0: the 15m reversal on our Binance data

Betting against the previous 15m candle's sign, Binance 2026-03-01 to 09-20, 19,584 bars per
coin, block-bootstrap 95% interval:

| coin | AUC | 95% CI | t (bootstrap SD) | pre-registration quoted |
|---|---|---|---|---|
| BTC | 0.518 | 0.514–0.524 | 6.49 | 0.533 |
| ETH | 0.522 | 0.516–0.530 | 6.20 | 0.538 |
| SOL | 0.519 | 0.512–0.527 | 5.38 | — |
| XRP | 0.513 | 0.506–0.520 | 3.35 | 0.536 |

The quoted numbers are the paper's 12-lag logit AUCs, not its one-lag sign score. The same score,
walk-forward and out of sample (13,824 rows per coin; the paper's sample is 2025-01 to 2026-02,
33,312 rows):

| coin | ours, 12-lag logit | paper, 12-lag logit | our CI holds the paper's point | the paper's CI holds ours |
|---|---|---|---|---|
| BTC | 0.526 (0.517–0.535) | 0.533 (0.527–0.539) | yes | no |
| ETH | 0.542 (0.531–0.551) | 0.538 (0.532–0.544) | yes | yes |
| XRP | 0.527 (0.516–0.536) | 0.536 (0.530–0.542) | no | no |

The intervals overlap for all three. The reversal grows with the size of the previous move: the
four-coin flip rate runs from 49.5% in the smallest-move decile to 55.3% in the largest (paper:
50.2% to 53.0%). Deciles 9 and 10 stay below 0.05 after Holm correction in all four coins (40
cells, 16 with |t| ≥ 2 against 1.8 expected; t here treats rows as independent). Against 203
whole-day label shifts, every coin's AUC is above every shifted one (p 0.005, the floor).

### Step 1: spot only, walk-forward by month

Six folds: train on every window before month M, test on M (April to September 20). 332,160
test rows in 16,608 windows, four coins, one parameter set.

| model | log-loss | vs martingale (t) | Brier vs martingale (t) |
|---|---|---|---|
| martingale $\Phi(y/\sigma\sqrt h)$ | 0.63199 | — | — |
| momentum only ($\theta$) | 0.63117 | −0.00082 (−2.39) | −0.00035 (−2.21) |
| reversion only ($\kappa_0, \lambda, \alpha, c$) | 0.63001 | −0.00198 (−4.05) | −0.00097 (−4.57) |
| full | 0.63002 | −0.00197 (−3.89) | −0.00093 (−4.18) |

Full minus reversion: +0.000013 (t 0.15). Against a martingale with its own volatility scale per
quarter (a check added after review): reversion −0.00166 (t −3.53), full −0.00165 (t −3.35).
$\lambda$ free against $\lambda \ge 0$: −0.00027 (t −1.40).

Fitted values, full model:

| fit | $\theta$ | $\kappa_0$ | $\lambda$ | $\alpha$ | $c$ | full − martingale in the test month (t) |
|---|---|---|---|---|---|---|
| test Apr | −0.120 | 0.49 | −1.37 | 1.02 | 0.0134 | −0.0040 (−2.79) |
| test May | −0.063 | 0.41 | −1.77 | 1.10 | 0.0079 | −0.0008 (−0.59) |
| test Jun | −0.044 | 0.30 | −2.00 | 1.07 | 0.0075 | −0.0025 (−2.18) |
| test Jul | −0.046 | 0.22 | −2.31 | 1.14 | 0.0066 | −0.0019 (−1.74) |
| test Aug | −0.047 | 0.27 | −2.08 | 1.03 | 0.0072 | −0.0016 (−1.56) |
| test Sep | −0.049 | 0.29 | −1.88 | 1.00 | 0.0075 | −0.0007 (−0.51) |
| frozen for step 2 (Mar 1 – Sep 16, 384,000 rows) | −0.043 (z −2.00) | 0.345 (z 2.52) | −1.62 (z −3.34) | 0.978 (z 6.89) | 0.0093 (z 2.72) | — |

$\theta$ and $\lambda$ are negative in 6 of 6 folds. With a volatility scale per quarter,
$\theta$ is +0.007 (z 0.23) on the frozen window and negative in 2 of 6 folds; $\lambda$ is −0.49
(z −1.31) and still negative in 6 of 6.

Does reversion decay through the hour? By quarter of the hour, decision at minute 0:

| | Q1 (:00) | Q2 (:15) | Q3 (:30) | Q4 (:45) |
|---|---|---|---|---|
| share of the stretch pulled back, as fitted ($\lambda$ −1.62) | 0.10 | 0.15 | 0.21 | 0.30 |
| same, with a volatility scale per quarter ($\lambda$ −0.49) | 0.24 | 0.27 | 0.30 | 0.33 |
| AUC of betting against the previous candle (t), all windows Mar–Sep, n 19,584 each | 0.500 (0.05) | 0.518 (3.42) | 0.512 (2.35) | 0.540 (7.77) |
| full − martingale log-loss out of sample (t), n 83,040 each, all minutes | −0.0005 (−0.91) | −0.0019 (−2.59) | −0.0007 (−0.63) | −0.0049 (−3.26) |

The last two rows are exploratory, not pre-registered.

### Step 2: Polymarket tape, Sep 17–20

1,136 settled 15m markets (BTC/ETH/SOL/XRP) and 213 hourly markets. Frozen step 1 params. Of
5,680 market-minutes, 5,096 had a 15m print in the last 60 s and are used. Binance's direction
matched the real resolution on 94.7% of them.

**Scores on the same rows** (log-loss, lower is better):

| minute | n | windows | model | market | martingale | model − market (t) | model − martingale (t) | market − martingale (t) |
|---|---|---|---|---|---|---|---|---|
| 0 | 877 | 274 | 0.6940 | 0.6870 | 0.6931 | +0.0070 (1.73) | +0.0009 (0.22) | −0.0062 (−1.58) |
| 1 | 1,044 | 274 | 0.6656 | 0.6675 | 0.6654 | −0.0020 (−0.26) | +0.0002 (0.05) | +0.0021 (0.34) |
| **2 (headline)** | **1,071** | **273** | **0.6402** | **0.6330** | **0.6398** | **+0.0072 (0.87)** | **+0.0004 (0.11)** | **−0.0068 (−1.03)** |
| 3 | 1,052 | 272 | 0.6100 | 0.6070 | 0.6101 | +0.0030 (0.34) | −0.0001 (−0.03) | −0.0031 (−0.44) |
| 5 | 1,052 | 272 | 0.5451 | 0.5397 | 0.5477 | +0.0054 (0.69) | −0.0025 (−0.86) | −0.0080 (−1.16) |
| all | 5,096 | 274 | 0.6288 | 0.6247 | 0.6291 | +0.0041 (0.61) | −0.0003 (−0.08) | −0.0043 (−0.82) |

Each minute has its own set of markets (a row needs a print in the last 60 s). Brier at minute
2: model 0.2242, market 0.2216, martingale 0.2243.

Calibration at minute 2 ($p$ = probability of Up; pairs of the 0.1-wide bins in step2.json):

| $p$ bin | model n | model mean $p$ | Up rate | market n | market mean price | Up rate |
|---|---|---|---|---|---|---|
| 0.0–0.2 | 34 | 0.141 | 0.118 | 21 | 0.165 | 0.190 |
| 0.2–0.4 | 247 | 0.320 | 0.360 | 259 | 0.319 | 0.332 |
| 0.4–0.6 | 467 | 0.500 | 0.525 | 450 | 0.502 | 0.533 |
| 0.6–0.8 | 274 | 0.680 | 0.734 | 277 | 0.680 | 0.686 |
| 0.8–1.0 | 49 | 0.854 | 0.694 | 64 | 0.837 | 0.828 |

**Taker, minute 2.** Decide on the last taker print before $t$; buy limited at $a^*(p)$; fill at
the first taker print in the next 30 s if it is at or below $a^*$. A decided row with no such
print fills at its pre-$t$ quote. One entry per market, side from $\mathrm{sign}(p - \tfrac12)$.

| | model | martingale, same rows and rules |
|---|---|---|
| decided | 447 | 340 |
| entered | 355 | 253 |
| limit misses (print above $a^*$) | 92 | 87 |
| c/share over the price paid, net of fee (t) | +1.38 (0.44) | −0.89 (−0.24) |
| win rate / mean price paid | 0.620 / 0.590 | 0.581 / 0.574 |
| mean fee | 1.57c | 1.60c |
| its own expected c/share at those prices | +6.68 | +5.84 |
| per candidate row, 0 where no entry (n 1,071) | +0.46 (0.44) | −0.21 (−0.24) |

Model minus martingale per candidate row: +0.67c (t 1.19). They entered together on 229 rows,
always on the same side (−3.09c each, t −0.81). Model only: 126 rows, +9.49c (t 2.31).
Martingale only: 24 rows, +20.12c (t 2.29). These splits are exploratory.

Where the model's taker price came from: 274 entries at a taker print after $t$ made −0.97c
(t −0.29); 81 decided rows with no taker print in 30 s, filled at their pre-$t$ quote, made
+9.30c (t 1.74). The same 355 entries paying the pre-$t$ quote: +1.65c (t 0.52). Every decided
row, a limit miss counted as 0: +1.09c (n 447, t 0.44). As a market order that pays whatever
prints (447 entries): −0.26c (t −0.09).

**Maker, minute 2.** Rest $b^*$; a fill needs a later print strictly below the bid.

| | model | martingale, same rows and rules |
|---|---|---|
| quotes | 521 | 495 |
| fills (rate) | 429 (82.3%) | 405 (81.8%) |
| its own expected fill rate | 97.1% | 97.0% |
| c per quote, 0 if unfilled (t) | −2.69 (−1.08) | −2.30 (−0.95) |
| c per filled share (t) | −3.27 (−1.08) | −2.81 (−0.95) |
| win rate on fills / mean bid on fills | 0.532 / 0.564 | 0.528 / 0.557 |
| its own $p$ at the fill | 0.635 | 0.613 |
| its own expected c per quote ($J$) | +6.94 | +5.51 |
| per candidate row, 0 where no fill (n 1,071) | −1.31 (−1.08) | −1.06 (−0.95) |

Model minus martingale per candidate row: −0.24c (t −0.38). On the 424 rows where both quoted:
model −3.34c, martingale −2.85c, difference −0.49c (t −0.74).

Every quote, model and martingale, sat at the top of its search range, 1c under the last print,
where $J$ was still rising. Window-clustered 95% interval of the model's fill rate: 0.78–0.87; of its win rate
on fills: 0.47–0.59.

**Other minutes** (each minute its own market set):

| minute | taker entries | taker c/share (t) | maker quotes | maker c/quote (t) |
|---|---|---|---|---|
| 0 | 176 | +3.60 (0.74) | 435 | +0.87 (0.29) |
| 1 | 361 | +4.76 (1.50) | 546 | −2.50 (−1.04) |
| 2 | 355 | +1.38 (0.44) | 521 | −2.69 (−1.08) |
| 3 | 326 | +5.77 (1.94) | 527 | −1.53 (−0.69) |
| 5 | 328 | +2.28 (0.84) | 471 | −0.18 (−0.08) |
| earliest entry per market, all minutes | 747 | +2.95 (1.33) | 963 | −1.99 (−1.11) |

**Does the crowd already price the reversal?** At minute 0 (n 871) the market leaned 1.25c
against the previous candle (t 6.57) and the model 2.23c (t 13.56). The outcome went against the
previous candle in 49.3% of those rows (t −0.33). Over these four days the reversal did not show,
and the crowd already priced part of it.

**$\theta$ on the tape** (diagnostic, not used): refitting $\theta$ alone gives −0.168 (z −1.14,
5,096 rows).

**Binance against Chainlink.** At minute 2, Binance's direction differs from the resolution on
55 of 1,071 rows. Those rows carry +0.0051 of the +0.0072 model − market gap. On the other 1,016
rows the gap is +0.0022 (t 0.26). Scored against Binance's own direction, model − market is
−0.0050 (t −0.57). The split conditions on the outcome, so neither reading is the truth.

**Many comparisons.** step2.json holds 2,648 t-statistics; 339 have |t| ≥ 2, against about 120
expected if every cell were null and independent. The cells overlap heavily. Only the minute-2
block is the pre-registered headline; quarter, asset and sensitivity tables are exploratory.

### Deviations from section 8

Step 0

1. The pre-registration set the paper's 12-lag logit AUCs (0.533/0.538/0.536) as the target for
   the one-lag sign score. Both readings are reported, with no single yes/no flag. The first run
   had judged it by a rule of its own (sign-score CI above 0.5); that is kept as a labelled reading.
2. The flip-rate decile t-statistics treat rows as independent; Holm and BH p-values sit beside them.

Step 1

3. $\lambda$ is left free in sign (section 7 says maximum likelihood; the first run bounded it at
   $\lambda \ge 0$). The $\lambda \ge 0$ fits are a sensitivity.
4. Diagnostics added: a martingale with one fitted volatility scale, one with a scale per
   quarter, and the model-free reversal by quarter.
5. Added after the second review: reversion and full models with a volatility scale per quarter,
   to separate reversion from a volatility pattern in $\lambda$. The step 2 params are unchanged.
6. Only the pooled out-of-sample comparisons against the martingale are the headline. Fold,
   quarter, minute and coin tables are exploratory and not corrected for multiple comparisons.

Step 2

7. Blend moments are estimated leaving one tape day out, not walk-forward: there is one tape, so
   earlier days use weights estimated partly on later days.
8. Section 4 does not fix the forecast-error target. Chosen: the rest-of-hour drift on Binance,
   errors scaled by $\sigma$ and weighted by $1 - t$, target noise removed.
9. The maker's fill boundary moves with the crowd drift implied by the 15m price at $t$, not
   $\hat\mu_H$ (which does not exist for SOL or rows without an hourly print).
10. The maker quotes only $b^*$ (no ladder across 5–15c) and only where $J(b^*) > 0$. Fills use
    prints on both tokens (one shared book), not only the token bid for; token-only prints and a
    bid on the 1c tick are sensitivities.
11. Maker sensitivities are run at minute 2 only; the headline maker at every minute.
12. Taker quotes and fills include taker sells of the other token (the same book); direct buys
    only is a sensitivity.
13. The market price and $m_H$ use prints up to and including the decision second; the taker
    quote uses prints strictly before $t$.
14. Log-loss clips every probability to [0.001, 0.999].
15. The $\theta$ refit on the tape is a diagnostic; the frozen $\theta$ is used everywhere.
16. Diagnostics added: the model's side against the 1h move, and whether the crowd leans
    against the previous candle.
17. The taker decides on the last taker print in $[t-60, t)$ and fills at the first in
    $[t, t+30]$. The first run decided and priced on prints after $t$; that rule is a sensitivity.
18. Side from $\mathrm{sign}(p - \tfrac12)$, one entry per market (the first run tested both sides
    and sometimes bought both).
19. At a maker fill, $p$ is re-evaluated with the stretch reset at the fill state; holding it at
    the quote is a sensitivity.
20. The frozen params are the $\lambda$-free fit; $\lambda \ge 0$ is a sensitivity.
21. One entry per market per minute; the pooled result keeps each market's earliest entry.
22. Wilson intervals (pre-registered) treat rows as independent; window-clustered intervals sit
    beside them.
23. Quarter and asset tables are exploratory; every t is counted and Holm and BH are applied per table.
24. The model reads Binance and the markets settle on Chainlink; scores and P&L are also given
    against Binance's own direction.
25. $b^*$ at the top of its range (last print − 1c) is reported apart from an interior $b^*$.
26. Every decided taker row is kept; one with no taker print in 30 s fills at its pre-$t$ quote.
    Other pricings, and dropping those rows, are sensitivities on the same decided rows.
27. The taker is a buy limited at $a^*$, section 6's most it would ever pay. A market order is
    a sensitivity.
28. The maker's 1c-tick sensitivity bids the highest tick at or below $b^*$.
29. The price-threshold baselines are removed by the amendment in section 8. Their numbers and
    side definition (step2.json deviation 22) are not reported.
30. Added in the write-up, not pre-registered: the martingale probability run through the same
    taker and maker rules on the same minute-2 rows.

### Open review findings

From the second review, not fixed in the code:

1. **High, checked but not resolved.** $\lambda < 0$ may mostly be volatility differing by quarter,
   read through the variance term rather than the mean. With a volatility scale per quarter,
   $\lambda$ goes from −1.62 to −0.49 (z −1.31) and $\theta$ from −0.043 to +0.007. Step 2 still
   uses the as-coded params.
2. Low. $\theta$ was fitted with the trailing spot return as the momentum but is applied in
   step 2 to the blend of the 1h market and the spot return. step2.json's deviations do not say so.
3. Low. Blend weights and $v$ are pooled across quarters of the hour. The error moments differ by
   quarter, and in quarter 4 the spot estimate's error variance comes out negative (descriptive,
   not used).
4. Low. Leave-one-day-out blend weights use later days. Disclosed; equal weights change minute-2
   log-loss by +0.0001 (t 0.61).
5. Low. Minute-by-minute step 2 results cover different market sets, so they are not a
   like-for-like comparison across minutes.
6. Low. The maker sensitivity chain is read per quote over different quote sets, and its
   one-sided / two-sided split conditions on the path after $t$.
7. Low. 18 headline maker quotes would have crossed the ask but are booked as fee-free maker fills.
8. Low. Step 0's "always-Down accuracy (same rows)" is over all 19,584 rows, not the decided rows
   it sits beside, and flat labels inflate it.
9. Low. The in-sample cost of $\lambda \ge 0$ is non-negative by construction (the models are
   nested). Read the out-of-sample comparison instead: −0.00027 per row (t −1.40).
