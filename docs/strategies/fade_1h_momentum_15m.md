# Fade 1h Momentum on 15m

<!-- BEGIN GENERATED:strategy -->
| | |
|---|---|
| Name | Fade 1h Momentum on 15m |
| Key | `fade_1h_momentum_15m` |
| Status | running now |
| Switch | `fade_1h_momentum_15m` on the MY STRATEGIES card |
| Code | `polymarket_bot/fade_1h_momentum_15m/` — 9 files |
| Code fingerprint | `e97d1c0c4462` |
<!-- END GENERATED:strategy -->

## At a glance

### Concept

Zayan's idea: take the 15-minute position from the 1-hour momentum at a computed price, not a fixed one. The hour's side should count for more late in the hour than early. Mean reversion matters in the first windows and fades as the hour goes on. There are no gates: the entry price is the output of a calculation. The name records the hypothesis that the fitted momentum weight $\theta$ comes out negative, so the model fades; if it comes out positive, the same model follows. On sizing, his goal is profit, not win rate: size by confidence, cut positions the maths now expects to lose, and follow the market, which prices in everything.

### Main assumption

The log price is Brownian motion with a drift, plus an Ornstein–Uhlenbeck pull whose strength decays through the hour. Increments are Gaussian. The 1-hour market, which settles Up when the Binance 1h candle closes at or above its open, is priced efficiently when read, so its price can be inverted for the drift. A 15m window settles Up when the Chainlink TWAP-60s print at its close (the average of the last 60 s) is at least the print at its open (Gamma's priceToBeat), and the model prices exactly that. A resting order's chance of winning is read at the moment it fills, so a fill that comes from the price moving against us counts against it.

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

Each side's parent order is split into child orders at price levels $b_1 > \dots > b_K$ under that side's best bid. With $P_k$ the chance level $k$ fills, $\bar q_k$ the chance of winning when exactly the first $k$ fill, $n$ shares held and $C$ the cash of the Kelly account, the stakes $x_k \ge 0$ maximise

$$\sum_{k}(P_k-P_{k+1})\Big[\bar q_k\ln\big(C+n+\textstyle\sum_{i\le k}x_i\tfrac{1-b_i}{b_i}\big)+(1-\bar q_k)\ln\big(C-\textstyle\sum_{i\le k}x_i\big)\Big]$$

and the side is the one whose order adds the most to that expected log. A held position is cut by a resting sell at $s$ of

$$x^*=\frac{(1-p)\,s\,(C+n)-p\,(1-s)\,C}{s\,(1-s)}\quad\text{clipped to }[0,\,n],$$

with $p$ the held side's chance given the sale fills. The other side is never bought.

### How it works

Every minute, for each of BTC, ETH, SOL and XRP, on paper:

1. Read the live inputs: both 15m books with their depth, the 1h market's Up price, the live Chainlink price (the price now), the window's opening TWAP-60s print, the part of the closing minute's average already printed, the hour's move on Binance, and Binance candles for $\sigma$ and the last twelve 15m returns.
2. Price the chance the window settles Up on the TWAP-60s print at the close ($p_{\text{model}}$), then blend it with the 15m market's own price, the market anchor, to get the chance traded on, $p$.
3. Simulate 2,000 paths to the window's end. For every price level on each side, they give the chance a resting order there fills and the chance its side wins given that fill.
4. Price a parent buy order on each side: child orders at price levels from the best bid down to 15c under it (Settings), sized by Kelly. Rest the side that adds the most expected log growth, or nothing. The side is not simply where $p$ leans: a Down buy can pay while $p$ leans Up, when the market leans further.
5. Holding shares, the choices are: buy more of that side, offer some of them with a resting sell at or above the best ask ($x^*$ above), or do nothing. The other side is never bought while one side is held.
6. Size the four coins together, because they tend to settle the same way (an Up bet and a Down bet offset each other). Cap each child order, round down to the venue's share step and 5-share minimum, fit the buys into the free cash, and bring the resting paper orders in line. An unchanged order keeps its place in the queue. Nothing crosses the spread, so no fee is paid.
7. Paper orders fill only from the real trade tape, after the depth ahead of them at each price level. Every window settles from the venue. After each settled window, all eight dials take one step toward the result (recursive maximum likelihood, about two days of memory).

It started from dials fitted on the Sep 17–20 tape: the market and the model each carry about half the weight ($w_M = 0.45$, $w_S = 0.57$) and the four coins move together ($\rho = 0.75$). There is no live order path.

### How it was derived

- **2026-09-21, Zayan.** Pulled his 18 recent manual trades, placed by hand on the 15m from the 1h momentum's side. Four were right, for −$9.61. The opposite side would have made +$12.07.
- **Claude.** Measured following the 1h side on 1,088 settled 15m markets, by the price the market was at (`threshold_scan.py`). It lost where the market priced that side below about 50c and made money from about 55c up (+4.66c, t = 1.98). Fading it lost (−7.84c, t = −3.33). These are measurements of the market, not entry rules.
- **Zayan** set out the concept above. **Claude** formalised it as Brownian motion with a decaying Ornstein–Uhlenbeck pull, and the entry price as an optimisation.
- A Monte Carlo check of every closed form (`validate_math.py`) found six errors in the first draft, all fixed. The literature changed the reversion term to a soft-clipped lag kernel (Kitron & Wengrowicz 2026).
- **2026-09-22, Zayan** asked for sizing by confidence, cutting positions the maths expects to lose, and following the market. **Claude** derived it from one objective, the expected log of the bankroll: the market anchor, Kelly sizing across the four coins, and learning every dial live.
- **2026-09-26, Claude (review).** Both sides are now priced and the one that adds the most is taken; a position is cut by selling the shares held, never by buying the other side; paper fills follow the real queue, level by level.

### References

- Full derivation and the pre-registered historical test: `tasks/2026-09-21-fade-1h-momentum-on-15m.md`
- Sizing, scaled passive limit orders, cutting a position and the market anchor: `tasks/2026-09-22-fade-1h-sizing.md`
- One side plus cash, never both: `tasks/2026-09-22-kelly-horse-race-research.md` (branch `research/1h-direction-vol-15m`, PR #280)
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
output is a probability for the window, and from it which side to buy, the price levels to rest
at and how much to put at each. Nothing in it is a threshold or a gate: the side, the prices and
the sizes come out of the maths, and each is zero when it does not pay.

It runs in the app as a paper strategy (switch `fade_1h_momentum_15m` on the MY STRATEGIES
card). Every minute it:

- reads each of BTC, ETH, SOL and XRP's current 15m window live: both books with their depth,
  the 1h market's price, the live Chainlink price (the price now) and Binance's, the window's
  price to beat (the TWAP-60s print at the open, which the window settles against), the part of
  the closing minute's average already printed, and Binance candles for $\sigma$ and the 15m
  returns;
- prices the chance the window settles Up on the TWAP-60s print at the close, and follows the
  market as far as the settled windows say (the market anchor);
- simulates 2,000 paths of the price to the window's end. For every price level on both sides
  they give the chance a resting order there fills, and the chance its side wins once filled;
- prices a parent buy order on each side, split into child orders at price levels from the best
  bid down to 15c under it (Settings), each sized by Kelly, and keeps the side that adds the most
  to the expected log of the bankroll, or nothing;
- with shares held, compares buying more of that side, a resting sell of some of them at or
  above the best ask, and doing nothing. It never buys the other side while one side is held;
- sizes the four coins together (half Kelly by default, on a 100 USD starting paper bankroll),
  caps each child order at 25 USD, and brings the resting paper orders in line with the plan
  (an unchanged order keeps its place in the queue);
- writes one sentence per coin for the card, for example (the app's own code on a test window):
  "BTC's 15m leg is down 0.03% with 13 min left; the snap-back from the last candles barely
  moves it; the hour's momentum adds little; the market has Up at 60c against the model's 45%,
  so the chance traded on is 52%; the maths rests a Down buy order at 39c (filled at 39c, Down
  wins 47% of the time).";
- keeps checking fills, settles every window from the venue, traded or not, and after each
  settled window moves the dials one step toward the result (a new dials version each time).

It never crosses the spread and pays no fee. A paper order fills only when the real trade tape
reaches it through the depth ahead of it, price level by price level, and partial fills count.
There is no live order path: with LIVE selected it places nothing and says so on the card.

What the card shows, top to bottom:

- profit first: net P&L (with any shares sold before the result and what they brought in),
  return on the dollars staked, cents per share, the largest drawdown, and the open exposure
  (windows not yet settled, child orders resting, dollars of buys resting, shares offered);
- the loop's state: the last pass and its time (flagged when it is more than three intervals
  old), every distinct error of that pass, and a header pill when the loop died (LOOP DIED),
  stopped (LOOP STOPPED) or a pass failed before it finished (PASS FAILED), so it shows with the
  card folded. A loop that dies is shown as dead, never as an earlier run's clean pass;
- per coin: a pill for the order (BUY UP, SELL UP, NO ORDER, ...), the inputs in plain words,
  the chances (the model's, the market's and the one traded on), and the Orders column. That
  column gives the choice ("No order: neither side adds growth." when nothing pays), the growth
  each option adds, and a table of the child orders: Child order, Shares, Depth ahead, Fill
  chance, Wins if filled, State. Under it are the orders this pass kept, placed and cancelled,
  any order held back and why, the position held and the sale that would reduce it;
- the coin's warnings: a failure that did not stop the coin (a database read or write) is shown
  on its entry and counted as an error of the pass. Notes are plain facts about the inputs (a
  default that was assumed).

## How it was formed

- **2026-09-21, Zayan (operator).** Asked to pull his recent manual trades on the idea that he
  was wrong in most of them, so the opposite side might be right. He had placed them by hand:
  read the 1h momentum and its side, and take the 15m position on that side.
  Of 18 settled trades since 2026-09-13, 4 were right: −$9.61 on $20.39 staked. The opposite
  side of each would have made +$12.07. Four of the losses were one correlated window: BTC, ETH,
  XRP and DOGE all bought Up at Sep 20 3:45–4:00PM ET, and all four fell.
- **2026-09-21, Claude.** Measured following the 1h momentum side on 1,088 settled 15m markets
  instead of 18 trades, by the price the market was at (`threshold_scan.py`). It lost where the
  market priced that side below about 50c and made money from about 55c up; fading it was
  significantly negative (table under Evidence). The bulk data did not support a flat fade.
  The price bands measure the market; they are not entry rules.
- **2026-09-21, Zayan.** Set out the concept: take the 15m position from the 1h momentum at a
  computed price, not a fixed one. The 1h side should weigh more in the last 15m of the hour
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
- **2026-09-22, Zayan.** Set out the sizing: "our goal isn't win rate, it's profit". Size by
  confidence, cut trades the maths is confident it is about to lose, and follow the market,
  which prices in everything. **Claude** derived all of it from one objective, the expected log
  of the bankroll (`tasks/2026-09-22-fade-1h-sizing.md`), and it was wired into the app
  as a paper strategy.
- **2026-09-26, Claude (review).** A review of the paper build found the side was picked by whether $p$
  was over one half, and a losing position was cut by buying the other side. Both are replaced:
  the maths prices a buy of each side and takes the one that adds the most, and a position is
  cut by a resting sell of the shares held (the same payoff, without holding both sides).

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

**Taker break-even, for comparison.** With fee $f = 0.07$, the ask at which taking breaks even
solves $p - a - fa(1-a) = 0$:

$$a^* = \frac{(1+f) - \sqrt{(1+f)^2 - 4fp}}{2f}.$$

The strategy never takes. Every order is a scaled passive limit order (Zayan's standing rule:
rest under the price, never cross), sized in `tasks/2026-09-22-fade-1h-sizing.md`.

**Fill and win chances at each price level.** 2,000 simulated paths of the fitted process run to
the window's end, seeded per window. The market's price along a path is a drift plus Brownian
noise under the same settlement, set to equal the market's mid now. A resting buy at $b$ fills
when that price for its side comes down to $b$; a resting sell at $s$ when it comes up to $s$.
Between grid points the crossing is caught by the Brownian-bridge extreme of the step, so a path
that dips through a level and comes back still fills it. That gives $P_{fill}$ for each level, and
$q_{fill}$, the chance traded on re-evaluated at the fill with the market exactly at the level.
Nothing fills at once: every level is on the passive side of the mid.

**Scaled passive limit orders.** A parent buy order on one side is split into child orders
resting at price levels a cent apart, from the side's best bid down to 15c under it (Settings).
The stakes maximise the expected log of the Kelly account over "exactly the first $k$ child
orders filled, then won or lost", counting any shares of that side already held. The objective
is concave, so the answer is unique. At zero stakes its slope for level $k$ is
$P_k (q_k - b_k)/(b_k C)$ with nothing held: a level gets money only when its win chance at the
fill beats its price, and shares already held raise that bar.

**Which side.** Both sides are priced from the same paths: a buy of Up under Up's best bid and a
buy of Down under Down's best bid. A price level that would meet its own book is left out: a buy
at or above that side's ask (a locked book, bid equal to ask, puts the best bid there), or a sale
at or below its bid. The maths rests whichever adds more to the expected log of the account, and
nothing when neither adds anything. With shares held, the choices are more of that side, a resting
sell of some of them, or nothing.

**Cutting a position.** Holding $n$ shares, a resting sell of $x$ of them at $s$, at or above the
best ask, leaves $C + n - x(1-s)$ if the side wins and $C + xs$ if it loses. Maximising the
expected log gives

$$x^* = \frac{(1-p)\,s\,(C+n) - p\,(1-s)\,C}{s\,(1-s)},\qquad \text{clipped to } [0,\,n],$$

with $p$ the held side's chance given the sale fills. This is the first version's formula for
buying the other side at $b_o = 1 - s$: the same payoff in both outcomes, without holding a pair
that just pays one dollar back. With cash allowed, the best position is one side plus cash, never
both (research note, `tasks/2026-09-22-kelly-horse-race-research.md`, PR #280). The sell price is
the level where the fill chance times the gain is largest.

**The Kelly account.** The Kelly multiplier $k$ (½ by default) is applied to wealth, not to each
pass's stake: the sizing works in an account with cash $C = k(W + n\,\text{mark}) - n\,\text{mark}$,
where $W$ is the coin's bankroll (the free paper cash plus what the other coins' open positions
are worth at their mids) and $n\,\text{mark}$ the value of the shares held in this window. So a
fill at unchanged odds adds nothing more, and a position that has grown past $k$ of the wealth is
trimmed.

**Four coins together.** The four coins are sized through a one-factor Gaussian copula with
correlation $\rho$, learned on their Up results. A Down bet loads on the shared factor with the
opposite sign, so two Up bets are shrunk and an Up bet next to a Down bet is not. The joint sizing
only ever shrinks a coin's buy. Sells are not shrunk.

**Paper orders.** An order rests from the second after it is written. It fills only from the real
trade tape. Trades at better prices use up the depth ahead of it, price level by price level; a
trade at or through its own price then works through its own level and fills it, in part or in
full. A sell fills from takers buying its token at its price or above. Each pass keeps the
orders already resting at a wanted price, so they keep their place in the queue, cancels the
rest and places the difference. Side, price and size are all continuous in the inputs.

**Worked examples** (the app's own code on the test windows in `tests/unit/test_fade1h_decide.py`,
half Kelly, a 100 USD bankroll):

- *Buying the side $p$ leans against.* BTC is 0.03% under its opening print with 13 minutes left.
  The leg alone takes the model to 45% Up (4.6 points under even). The snap-back is zero, as the
  last candles were flat, and the hour's momentum moves it 0.03 points. The market has Up at 60c,
  and at its fitted weight it pulls the chance traded on up 6.6 points, to 52% Up. Buying Up at
  59c fills on 98% of paths, but when it does Up wins only 51%, under the price, and no deeper Up
  level wins more often than its price either. Buying Down at 39c fills on 98% of paths and,
  when it does, Down wins 46.8%, 7.8c over the price. Deeper Down levels fill less often and win less often when they do, so the
  whole parent order sits at 39c: 16.40 Down shares, 6.40 USD of the 50 USD Kelly account.
- *Cutting a position.* Holding 20 BTC Up, BTC is 0.30% under its opening print with 13 minutes
  left. The model says 12% Up, the market 31c, and the chance traded on is 19%. The Kelly account
  is ½ × (100 + 20 × 0.31) − 6.20 = 46.90 USD. Offered at 32c, the best ask, the sale fills on 95%
  of paths, and when it does Up still wins 19.9%. $x^*$ is 49.7 shares, more than the 20 held, so
  all 20 are offered. Higher prices fill less often (40c: 65% of paths), and the fill chance times
  the gain is largest at 32c, so the sale rests there. Buying more Up pays nothing at any level.

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
with forgetting; about two days of windows carry half the weight). The evidence is that
window's own decision rows, read by its slug. The five process dials ($\theta, \kappa_0,
\lambda, \alpha, c$) also carry a fixed prior at the research fit's own strength
($1/\text{SE}^2$, from 76,800 windows of Binance minutes) that is never forgotten: settled 15m
results say little about how the price moves, so without it those dials would drift on noise.
With it they stay within a fraction of the fit's standard error unless the results really
disagree (on 1,200 to 2,000 simulated windows where the truth never moves, they stayed
within 0.22 of a standard error). $w_M$, $w_S$ and $\rho$ have no fixed prior: they are what the live results
are for, and when the market is right $w_M$ goes to about 1 and $w_S$ to about 0. Version 0,
the prior, follows the market exactly and is never updated.

The operator's settings (group Fade 1h Momentum on 15m), read fresh every pass. They are
preferences and caps around the maths, not price rules:

| setting | default |
|---|---|
| Kelly multiplier (1 = full Kelly) | 0.5 |
| Starting paper bankroll | 100 USD |
| Largest single child order (buys) | 25 USD |
| Price range for child orders: nearest level below the best bid (sells: above the best ask) | 0c (join the best bid) |
| Price range for child orders: deepest level below the best bid (sells: above the best ask) | 15c |
| Reduce a held position with a resting sell | on |
| Price now for the model | Chainlink (Binance to compare) |
| Pass interval | 60 s |
| Trade BTC / ETH / SOL / XRP | on |

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
  mid; a real book can gap past a price level. Paper fills come from the real tape, so the record
  shows the difference.
- The maths counts a level as filled when the market's mid reaches it. The paper fills need real
  trades through the depth ahead of the order. The two rules are not the same yet, so the maths
  can expect more fills than the paper record gets.
- The paper queue sees only the depth displayed in the book when the order is placed. It does not
  see orders that join ahead later, or cancels ahead of it.
- Switching sides waits for the tape: a buy of the other side waits until the tape shows the held
  shares sold and no cancelled order on that side filled late (a few minutes).
- The joint sizing only shrinks a coin's buy; an Up bet next to a Down bet could carry more than
  either alone, and the app does not size it up.
- That reversion decays through the hour is Zayan's hypothesis. The lag-kernel reversal is
  documented in the literature; its decay by hour position is not.
- On spot, the 15m reversal is too small to trade (1.3bp gross vs 5bp cost). It can only pay
  here if a binary, which pays on the sign, is priced without it — the historical test decides.

## Sources

- Concept, the manual trades and the name: Zayan (operator), 2026-09-21. His trades:
  [0xc1daaec036a8a49e4a71cad2daa51dcb19bb00c5](https://polymarket.com/profile/0xc1daaec036a8a49e4a71cad2daa51dcb19bb00c5).
- Research write-up with the full derivation, validation table and pre-registered historical
  test: `tasks/2026-09-21-fade-1h-momentum-on-15m.md`.
- Sizing concept: Zayan (operator), 2026-09-22. The maths for the market anchor, Kelly sizing,
  scaled passive limit orders, cutting a position with a resting sell, and live learning:
  `tasks/2026-09-22-fade-1h-sizing.md` (Claude, 2026-09-22; sections 3 and 4 rewritten
  2026-09-26).
- Never both sides: with cash allowed, the log-optimal position is at most one side plus cash.
  Research note `tasks/2026-09-22-kelly-horse-race-research.md` (branch
  `research/1h-direction-vol-15m`, PR #280), finding 3;
  [arXiv 2603.13581](https://arxiv.org/abs/2603.13581), [arXiv 2607.06166](https://arxiv.org/abs/2607.06166).
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

- 2026-09-26 · `e97d1c0c4462` · Review fixes. Both sides are priced and the one adding the most expected log growth is rested as scaled passive limit orders: a parent order split into child orders at price levels at or under the best bid, none at or across its own book. A losing position is reduced by a resting passive sell of the shares held, never by buying the other side. Paper orders are stamped when written, fill from the real tape through the depth ahead level by level, and keep their queue place across passes. Joint sizing accounts for each bet's side. The price now is the live Chainlink price. The learner holds the price-process fit as a fixed prior. The card shows every error of a pass, input warnings, and a dead or stopped loop. Standard terms throughout.
- 2026-09-22 · `32a1484ce579` · The model is plugged in, so it now bids on paper. decide.py prices each window with model.py, a standard-library port of the research model for the TWAP-60s settlement (checked against the research code to 1e-9 on 197 cases), follows the market with fitted anchor weights, and gets the fill and win chances at each price level, and the quotes for hedging on the other side (replaced on 2026-09-26 by a resting sell of the shares held), from 2,000 simulated paths. Starting dials (version 1) fitted on the Sep 17-20 tape: w_M 0.45, w_S 0.57, rho 0.75. learner.py moves every dial one bounded step after each settled window (recursive maximum likelihood, about two days of memory).
- 2026-09-22 · `47989d06aa7c` · Wired into the app as a running paper strategy (switch fade_1h_momentum_15m; Settings group Fade 1h Momentum on 15m): each minute it records every coin's inputs, checks fills and settles every window; the model hook returns nothing yet, so no bids are placed. Code moved to polymarket_bot/fade_1h_momentum_15m/. Start reference is now the window's priceToBeat (TWAP-60s print at the open), per the verified settlement rule.
- 2026-09-21 · `91319a8794bc` · Added an At a glance summary (concept, main assumption, maths, how it works, how it was derived, references) for the dashboard's STRATEGY card.
- 2026-09-21 · `91319a8794bc` · Status moved from offline only to cannot trade: the offline-only status was removed (#273). Nothing is wired to trade it yet.
- 2026-09-21 · `a3ca60e9bd16` · Lint only: removed an unused import from threshold_scan.py. No change to the analysis.
- 2026-09-21 · `8e64a661d470` · Doc created: Zayan's concept, the maths as validated against simulation and the literature, and the two analyses that started it. Historical test pending.
