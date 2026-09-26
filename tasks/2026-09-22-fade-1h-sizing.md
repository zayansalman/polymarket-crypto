# Fade 1h Momentum on 15m — sizing, position reduction and following the market

| | |
|---|---|
| Concept | Zayan (operator), 2026-09-22: "our goal isn't win rate, it's profit" — size by confidence (dynamic notional slicing), hedge trades we are confident we are about to lose, set it up optimally; the market prices in everything, we discover and follow it |
| Maths | Claude, 2026-09-22; sections 3 and 4 rewritten 2026-09-26 |
| Status | built into the app as a paper strategy (`polymarket_bot/fade_1h_momentum_15m/`, `sizing.py` and `decide.py`); formulas checked numerically (below) |

One objective drives all of it: **maximise the expected log of the bankroll** (Kelly 1956). It is
the growth-optimal choice for repeated bets — it maximises long-run profit, not win rate — and
every decision below (the side, the size, the price levels, cutting a position) is the calculus
of that one function. No rules, no thresholds: each output is zero when it does not pay and grows
continuously with the edge.

Every order is a **scaled passive limit order**: one parent order split into child orders resting
at several price levels on the passive side of the touch — a buy at or under the best bid, a sell
at or over the best ask. A level that would meet its own book (a buy at or above that side's ask,
which a locked book puts at the bid, or a sale at or below its bid) is left out. Nothing crosses
the spread, so no fee is paid. The strategy **never holds both sides** of a window: a position
is cut by a resting sell of the shares held (section 4).

## 1. Follow the market: the market anchor

The test showed the market's own price was slightly better calibrated than the model. So the
probability we trade on combines both, in probit space:

$$p = \Phi\big(w_M\,\Phi^{-1}(m) + w_S\,\Phi^{-1}(p_{\text{model}})\big)$$

$m$ is the 15m market's mid for Up, $p_{\text{model}}$ is the model from the living doc.
$w_M, w_S$ are learned live by maximum likelihood on settled windows (section 5), starting from
the Sep 17–20 tape. If the model adds nothing, $w_S \to 0$ and $p \to m$: the maths follows the
market and trades nothing. It trades exactly where, and as much as, the model has been adding
information beyond the price. (Forecast combination in log-odds: Bates & Granger 1969;
Ranjan & Gneiting 2010.)

## 2. Size: Kelly, jointly across the coins

**One bet.** A resting buy at $b$ on side $j$, with $q$ = our probability that side wins if the
order fills (adverse selection inside it). Staking a fraction $f$ of the bankroll:

$$G(f) = q\ln\!\Big(1 + f\,\tfrac{1-b}{b}\Big) + (1-q)\ln(1-f), \qquad G'(f) = 0 \;\Rightarrow\; f^* = \frac{q - b}{1 - b}.$$

As taker at ask $a$ with fee $c = 0.07\,a(1-a)$: $f^* = (q - a - c)/(1 - a - c)$. The strategy
never takes; this is only for comparison.

**Uncertainty in $q$ does not change it** — $G$ is linear in $q$, so the optimum uses the
posterior mean. Checked: $q \sim N(0.56, 0.05^2)$, $b = 0.50$ gives 0.1198 against 0.1200. The
caution belongs in $q$ itself (the market anchor shrinks it toward the price), not in an
arbitrary haircut.

**Four coins at once.** The four windows move together (all four settled the same way in 59%
of windows on the tape; Sep 20 3:45 PM lost all four). Sizing each coin alone overbets on the
same side. The joint Kelly stakes maximise

$$E\Big[\ln\Big(1 + \sum_{i} f_i\,R_i\Big)\Big]$$

over the joint distribution of the four outcomes (a one-factor Gaussian copula, correlation
$\rho$ learned live on the coins' Up results), solved numerically. A bet on Down loads on the
shared factor with the opposite sign of a bet on Up, so two bets on the same side move together
and two on opposite sides offset each other. Checked: four Up bets at $q = 0.56$, $b = 0.50$,
$\rho = 0.6$ — alone, Kelly says 0.120 each (0.48 in total); jointly, 0.053 each (**0.21 in
total**). Two bets at $\rho = 0.75$: 0.078 each on the same side, 0.241 each on opposite sides.
In the app the joint sizing only ever shrinks: each coin's buy is scaled by its joint stake over
its own Kelly stake, capped at 1. Sells are not joint-sized; they only lower the exposure.

**Your risk dial.** Full Kelly maximises growth but swings hard. A multiplier $k$ on the stakes
trades growth for smoothness. From the check: half Kelly keeps **75% of the growth with half
the swing**; a quarter keeps 44%. This is the one setting that is a preference, not a
measurement, so it is yours on the dashboard (default ½).

**The Kelly account.** With shares already held, multiplying the full-Kelly stake by $k$ every
minute would re-bet the same outcome pass after pass and creep up to full Kelly. So $k$ is applied
to wealth instead. Holding $n$ shares of one side, marked at that side's mid $\text{mark}$, with
cash $W$, the sizing works in an account worth $k$ times the whole wealth that holds those shares.
Its cash is

$$C = k\,(W + n\,\text{mark}) - n\,\text{mark}.$$

Full-Kelly sizing on $(C, n)$ stakes exactly $k$ times full Kelly when nothing is held, adds
nothing after a fill at unchanged odds, and sells when the position has grown past $k$ of the
wealth or the odds have turned. Checked: with $W$ = 100 USD and $k = ½$, nothing held gives
$C$ = 50.00 USD; holding 20 Up marked at 31c gives $C$ = 46.90 USD. Sections 3 and 4 use $C$ as the cash.

## 3. Slice: scaled passive limit orders

**The parent order and its child orders.** A parent buy order on side $j$ is split into child
orders resting at price levels $b_1 > b_2 > \dots > b_K$ at or under that side's best bid. The
levels run a cent apart (or the book's tick, when coarser) from the nearest to the deepest level
set in Settings; the default is from the best bid itself down to 15c under it. A buy there never
crosses the spread and sits on the passive side of the mid, so it fills only when the market
comes down to it.

**What each price level knows.** From the same simulated paths of the model (2,000 per coin per
minute), level $k$ fills with probability $P_k$ and, if it fills, side $j$ wins with probability
$q_k$. A deeper child order fills only after every nearer one, so $P_1 \ge P_2 \ge \dots \ge P_K$.
The win chance is re-evaluated at the moment of the fill, with the market exactly at the level, so
$q_k$ falls with depth when being filled deep means the price moved against us. That adverse
selection is inside $q_k$, not a haircut. With $P_{K+1} = 0$, "exactly the first $k$ child orders
filled" has probability $P_k - P_{k+1}$, and side $j$ wins in that event with probability

$$\bar q_k = \frac{P_k q_k - P_{k+1} q_{k+1}}{P_k - P_{k+1}}.$$

**The objective.** Dollar stakes $x_1, \dots, x_K \ge 0$ (child order $k$ is $x_k / b_k$ shares),
with $n$ shares of side $j$ already held and Kelly-account cash $C$, maximise

$$E\ln W = \sum_{k=1}^{K} (P_k - P_{k+1}) \Big[ \bar q_k \ln\big(C + n + \textstyle\sum_{i\le k} x_i\,\tfrac{1-b_i}{b_i}\big) + (1-\bar q_k)\ln\big(C - \textstyle\sum_{i\le k} x_i\big)\Big].$$

The no-fill term does not depend on the stakes and drops out. Each term is the log of an affine
function of the stakes, so the objective is **concave**: the optimum is unique, and `sizing.py`
finds it by projected Newton (`scaled_limits`).

**Zero where there is no edge.** Differentiating at zero stakes and telescoping the sums
($\sum_{m\ge k}(P_m - P_{m+1})\bar q_m = P_k q_k$):

$$\frac{\partial\,E\ln W}{\partial x_k}\Big|_{x=0} = P_k\Big[\frac{q_k (1-b_k)}{b_k (C+n)} - \frac{1-q_k}{C}\Big].$$

With nothing held this is $P_k (q_k - b_k)/(b_k C)$: a level gets a stake only when the chance of
winning given its fill beats its price. With shares held the bar is higher,
$q_k > b_k (C+n)/(C + b_k n)$, so a position already at its log-optimal size gets nothing more.
The fill chance $P_k$ scales the gain but never creates one. On the test windows the whole parent
order usually lands on one or two levels: deeper levels are cheaper, but they fill less often and
win less often when they do.

**What the app does with the stakes.** Each child order is shrunk by the joint sizing (section 2),
capped at the largest single order, rounded down to the venue's 0.01-share step, dropped when
under the venue's 5-share minimum, and the buys as a whole are fitted into the free cash. The
resting orders are then brought in line with the plan: an order already at a wanted price keeps
its place in the queue while the plan still wants at least its size.

### 3b. Which side: expected log growth over both sides

The side is not where $p$ leans. With nothing held in the window, the maths prices two parent
orders from the same simulated paths — a buy of Up at levels under Up's best bid, and a buy of
Down at levels under Down's best bid — sizes each as above, and computes what each adds to the
expected log of the Kelly account:

$$\Delta G_j = E\ln W(x^*_j) - E\ln W(0), \qquad j \in \{\text{Up}, \text{Down}\}.$$

It rests the one with the larger $\Delta G_j$, and nothing when neither is positive. With $n$
shares of side $j$ held, the choices are: buy more of $j$ (section 3, counting the $n$ shares), a
resting sell of some of them (section 4), or nothing — again the largest expected log growth. The
other side is never a choice while one side is held.

Why this and not "Up when $p \ge ½$": the side where $p$ leans can be the dear side. A test window
run through the app's own code (`tests/unit/test_fade1h_decide.py`, the fade case): the model says
Up 45%, the market has Up at 60c, and the anchor puts the chance traded on at 52% Up. A buy of Up at
59c fills on 98% of paths but, filled there, Up wins only 51%: no level pays, $\Delta G_{\text{Up}} = 0$.
A buy of Down at 39c fills on 98% of paths and, filled there, Down wins 46.8% — 7.8c over the price.
$\Delta G_{\text{Down}} = 0.0123$ in the 50 USD Kelly account, so the maths rests 16.40 Down shares at 39c
(6.40 USD), though $p$ leans Up.

## 4. Reduce: a passive sell of the shares held

Holding $n$ shares of side $j$ with cash $W$ (the Kelly account's $C$ in the app). The maths
updates every minute. Rest a sell of $x$ of the shares at $s$, at or over $j$'s best ask. If the
sale fills and $j$ wins, we have $W + xs + (n - x) = W + n - x(1-s)$; if it fills and $j$ loses,
$W + xs$. If it never fills nothing changes, whatever $x$ is, so the best $x$ uses $p$ = the chance
$j$ wins **given the sale fills**. Choose $x$ to maximise

$$g(x) = p\ln\big(W + n - x(1-s)\big) + (1-p)\ln(W + xs).$$

Setting $g'(x) = -\dfrac{p(1-s)}{W + n - x(1-s)} + \dfrac{(1-p)s}{W + xs} = 0$ and collecting the
terms in $x$, $(1-p)s(W+n) - p(1-s)W = x\,s(1-s)$, so

$$x^* = \frac{(1-p)\,s\,(W+n) - p\,(1-s)\,W}{s\,(1-s)}, \qquad x = \min\big(\max(0, x^*),\, n\big).$$

$g$ is concave ($g'' < 0$), so clipping $x^*$ to $[0, n]$ gives the best sale that sells no more
than is held.

**It is the old other-side formula.** The first version of this section cut a position by buying
$h$ shares of the other side at $b_o$:
$h^* = \big[(1-p)(1-b_o)(W+n) - p\,b_o W\big] / \big[b_o(1-b_o)\big]$. Put $b_o = 1 - s$: the
numerator becomes $(1-p)s(W+n) - p(1-s)W$ and the denominator $(1-s)s$, so $h^* = x^*$ exactly.
The payoffs match too. Buying $h$ of the other side at $1-s$ leaves $W + n - h(1-s)$ if $j$ wins
and $W + hs$ if it loses — the same two numbers as selling $h$ at $s$. In the book they are one
price: a Down bid at $b_o$ is an Up ask at $1 - b_o$.

**Why never both sides.** For $x \le n$ the sale pays exactly what the other-side buy paid, without
holding a pair. A pair (one Up and one Down share) pays exactly one dollar whatever happens. It is cash
bought through two books, and until settlement it ties up the $x$ dollars the sale would have
released for the other coins. The research note on Zayan's Kelly horse-race idea
(`tasks/2026-09-22-kelly-horse-race-research.md`, branch `research/1h-direction-vol-15m`, PR #280,
finding 3; arXiv 2603.13581 Thm 3, 2607.06166) shows the general case: with cash allowed, the
log-optimal position is at most one side plus cash. So $x$ stops at $n$. When $x^* > n$ the maths
would rather be on the other side: the whole position is offered, and once it has sold the window
is flat and the next pass prices both sides afresh (section 3b). A buy of the other side is then
a new bet with its own fill and win chances. The paper ledger refuses a buy while the other side
is held, or might still be (an order resting there, or cancelled with its tape not yet read).

**The sell price.** The sell levels $s_1 < s_2 < \dots$ run from the best ask up, over the same
band as the buys. Each has a fill chance $P(s)$ (the market's price for $j$ comes up to $s$) and a
win chance $p(s)$ given that fill. A sale fills when the price has risen, which is when $j$ looks
better, so $p(s)$ sits above the chance now and that lowers $x^*$. The expected gain of resting the
sale at $s$ is

$$P(s)\Big[p(s)\ln\frac{W + n - x^*(1-s)}{W + n} + \big(1 - p(s)\big)\ln\frac{W + x^* s}{W}\Big],$$

and the price is the level where it is largest. Nothing rests when no level gains.

What it does (the four rows of the first version's table, now as sales; then a larger position):

| holding | chance the held side wins if the sale fills | sell at | $x^*$ | sale |
|---|---|---|---|---|
| 20 Up, $100 cash | Up 30% | 35c | 43.5 | **all 20** — the maths expects to lose, and 35c is above the 30% |
| 20 Up, $100 | Up 45% | 48c | 33.2 | all 20 |
| 20 Up, $100 | Up 60% | 62c | 29.5 | all 20 — still leaning Up, but 62c is above its 60% |
| 20 Up, $100 | Up 70% | 65c | −4.8 | **0** — keeping the shares pays more |
| 60 Up, $100 | Up 70% | 60c | 3.3 | 3.3 — trims a little |
| 60 Up, $100 | Up 60% | 55c | 33.1 | 33.1 — keeps 26.9, the Kelly size for 60% at 55c |
| 60 Up, $100 | Up 50% | 45c | 34.3 | 34.3 — sells under the win chance: the position is over its Kelly size |

It sells more the more is held ($n$) and the further the odds have turned ($1 - p$), and it sells
nothing when keeping pays more. That is the profit-optimal version of "cut the loser". The flip
side you named is inside the formula: when $p$ stays high, $x^* = 0$ and the upside is kept.

Worked through the app's own code on a test window (`tests/unit/test_fade1h_decide.py`, the sale
case): holding 20 BTC Up with a 100 USD bankroll, BTC is 0.30% under the opening print with 13 minutes
left. The market has Up at 31c, the model 12%, and the chance traded on is 19%. The Kelly account's
cash is ½ × (100 + 20 × 0.31) − 6.20 = 46.90 USD. At 32c, the best ask, the sale fills on 95% of
paths and, when it does, Up still wins 19.9%. $x^* = 49.7$, more than the 20 held, so all 20 are
offered. At 40c it would fill on only 65% of paths, and the fill chance times the gain is largest
at 32c (0.0546 against 0.0381), so the sale rests at 32c.

## 5. Everything learns live

Every parameter — the model's dials ($\theta, \kappa_0, \lambda, \alpha, c$), the anchor
weights $w_M, w_S$, and the coin correlation $\rho$ — updates after each settled window by one
step of recursive maximum likelihood with forgetting:

$$\hat\beta_{t} = \hat\beta_{t-1} + I_t^{-1}\,\nabla\ell_t(\hat\beta_{t-1}), \qquad I_t = \gamma\,I_{t-1} + \mathcal{I}_t$$

($\nabla\ell_t$ the new window's score, $\mathcal{I}_t$ its Fisher information, $\gamma < 1$ so
old windows fade; half-life set so that about two days of windows carry half the weight). It
starts from the fitted values and moves as fast as the evidence justifies: one window barely moves
it, a day of consistent disagreement moves it a lot.

## 6. What stays fixed

Only the formulas and your settings on the dashboard: the Kelly multiplier (default ½), the range
of price levels the child orders may rest at (default from the best bid to 15c under it; for
sales, from the best ask to 15c over it), the largest single child order (default 25 USD), the
starting paper bankroll (default 100 USD), whether a held position may be cut by a resting sell (on),
and the feed for the price now (the live Chainlink price). Paper only: live trading is not
authorised for any market.

## Checks

2026-09-22, and 2026-09-26 for the rows marked *.

| check | closed form | numerical |
|---|---|---|
| sale*, 20 Up + 100 USD, Up 30%, sell at 35c | $x^*$ 43.52, sell 20 | 43.52, sell 20 |
| sale*, Up 45%, sell at 48c | 33.17, sell 20 | 33.17, sell 20 |
| sale*, Up 60%, sell at 62c | 29.54, sell 20 | 29.54, sell 20 |
| sale*, 40 Up + 50 USD, Up 20%, sell at 25c | 56.00, sell 40 | 56.00, sell 40 |
| sale equals other-side buy*, 7 of 20 Up sold at 40c vs 7 Down bought at 60c, 100 USD | 115.80 / 102.80 USD | 115.80 / 102.80 USD |
| Kelly account*, 100 USD, $k$ = ½, 20 Up marked at 31c | 46.90 USD | 46.90 USD |
| Kelly with $q$ uncertain ($\pm$0.05) | 0.1200 | 0.1198 |
| four Up bets, $\rho = 0.6$ | 0.48 alone | 0.2135 jointly |
| two bets*, $\rho = 0.75$, same side / opposite sides | 0.12 alone | 0.0777 / 0.2411 each |

The sale rows' closed form is the first version's other-side formula with $b_o = 1 - s$ (the
same numbers); "numerical" maximises $g$ directly, first over all $x$ and then over $[0, n]$. The
checks run in `tests/unit/test_fade1h_sizing.py`.
