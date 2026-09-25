# Fade 1h Momentum on 15m — sizing, hedging and following the market

| | |
|---|---|
| Concept | Zayan (operator), 2026-09-22: "our goal isn't win rate, it's profit" — size by confidence (dynamic notional slicing), hedge trades we are confident we are about to lose, set it up optimally; the market prices in everything, we discover and follow it |
| Maths | Claude, 2026-09-22 |
| Status | maths for review; formulas checked numerically (below); not built yet |

One objective drives all of it: **maximise the expected log of the bankroll** (Kelly 1956). It is
the growth-optimal choice for repeated bets — it maximises long-run profit, not win rate — and
every decision below (size, ladder, hedge) is the calculus of that one function. No rules, no
thresholds: each output is zero when it does not pay and grows continuously with the edge.

## 1. Follow the market: the market anchor

The test showed the market's own price was slightly better calibrated than the model. So the
probability we trade on combines both, in probit space:

$$p = \Phi\big(w_M\,\Phi^{-1}(m) + w_S\,\Phi^{-1}(p_{\text{model}})\big)$$

$m$ is the 15m market's mid for Up, $p_{\text{model}}$ is the model from the living doc.
$w_M, w_S$ are learned live by maximum likelihood on settled windows (section 4), starting from
the Sep 17–20 tape. If the model adds nothing, $w_S \to 0$ and $p \to m$: the maths follows the
market and trades nothing. It trades exactly where, and as much as, the model has been adding
information beyond the price. (Forecast combination in log-odds: Bates & Granger 1969;
Ranjan & Gneiting 2010.)

## 2. Size: Kelly, jointly across the coins

**One bet.** A resting bid at $b$ on side $j$, with $q$ = our probability that side wins if the
bid fills (adverse selection inside it). Staking a fraction $f$ of the bankroll:

$$G(f) = q\ln\!\Big(1 + f\,\tfrac{1-b}{b}\Big) + (1-q)\ln(1-f), \qquad G'(f) = 0 \;\Rightarrow\; f^* = \frac{q - b}{1 - b}.$$

As taker at ask $a$ with fee $c = 0.07\,a(1-a)$: $f^* = (q - a - c)/(1 - a - c)$.

**Uncertainty in $q$ does not change it** — $G$ is linear in $q$, so the optimum uses the
posterior mean. Checked: $q \sim N(0.56, 0.05^2)$, $b = 0.50$ gives 0.1198 against 0.1200. The
caution belongs in $q$ itself (the market anchor shrinks it toward the price), not in an
arbitrary haircut.

**Four coins at once.** The four windows move together (all four settled the same way in 59%
of windows on the tape; Sep 20 3:45 PM lost all four). Sizing each coin alone overbets. The
joint Kelly stakes maximise

$$E\Big[\ln\Big(1 + \sum_{i} f_i\,R_i\Big)\Big]$$

over the joint distribution of the four outcomes (Gaussian copula, correlation $\rho$ learned
live), solved numerically. Checked: four bets at $q = 0.56$, $b = 0.50$, $\rho = 0.6$ — alone,
Kelly says 0.120 each (0.48 in total); jointly, 0.05–0.06 each (**0.22 in total**).

**Your risk dial.** Full Kelly maximises growth but swings hard. A multiplier $k$ on the stakes
trades growth for smoothness. From the check: half Kelly keeps **75% of the growth with half
the swing**; a quarter keeps 44%. This is the one setting that is a preference, not a
measurement, so it is yours on the dashboard (default ½).

## 3. Slice: a ladder of bids

Instead of one bid, rest rungs at $b_1 > b_2 > \dots > b_K$ on the chosen side. A deeper rung
fills only after the shallower ones, so rung $k$ fills with probability $P_k$ (decreasing) and,
if it fills, wins with probability $q_k$ (lower for deeper rungs — being filled deep means price
moved against us). The stakes $x_1..x_K \ge 0$ maximise

$$E\ln W = \sum_{k=0}^{K} (P_k - P_{k+1}) \Big[ \bar q_k \ln\big(W + \textstyle\sum_{i\le k} x_i\,\tfrac{1-b_i}{b_i}\big) + (1-\bar q_k)\ln\big(W - \textstyle\sum_{i\le k} x_i\big)\Big]$$

where "exactly the first $k$ rungs filled" has probability $P_k - P_{k+1}$. It is concave in the
stakes, so the optimum is unique; rungs with no edge get zero. The rung prices are a grid 1–15c
under the touch (your ladder band) and the maths sizes each rung — often most of it goes to one or
two rungs.

## 4. Hedge: when the maths turns against a position

Holding $n$ shares of side $j$, cash $W$. The maths updates every minute; now side $j$ wins with
probability $p'$. Rest a bid for $h$ shares of the other side at $b_o$ (resting only — your
rule). Choose $h$ to maximise

$$E\ln W = p'\ln(W + n - h\,b_o) + (1 - p')\ln\big(W + h(1 - b_o)\big)$$

$$\frac{d}{dh} = 0 \;\Rightarrow\; h^* = \max\Big(0,\; \frac{(1-p')(1-b_o)(W+n) - p'\,b_o\,W}{b_o(1-b_o)}\Big).$$

Checked against numerical maximisation to the cent in four cases. What it does:

| holding | maths now | other side bid | hedge |
|---|---|---|---|
| 20 Up, $100 cash | Up 30% | Down 65c | **43.5 Down** — the maths expects to lose, and Down is cheap for that |
| 20 Up, $100 | Up 45% | Down 52c | 33.2 Down |
| 20 Up, $100 | Up 60% | Down 38c | 29.5 Down — still leaning Up, but Down at 38c is under its 40% |
| 20 Up, $100 | Up 70% | Down 35c | **0** — hedging would cost more than it saves |

It hedges more the more exposed we are ($n$) and the more the odds have turned ($1 - p'$), and
it can end up the other way round if the move is strong enough — which is the profit-optimal
version of "cut the loser". The flip side you named is inside the formula: when $p'$ stays high,
$h^* = 0$ and the upside is kept. For a resting hedge $p'$ is replaced by the fill-conditional
probability, as in section 2.

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

Only the formulas, and your ½ Kelly risk dial. Paper only: live trading is not authorised for
any market.

## Checks (2026-09-22)

| check | closed form | numerical |
|---|---|---|
| hedge, 20 Up + $100, Up 30%, Down bid 65c | 43.52 | 43.52 |
| hedge, Up 45%, Down 52c | 33.17 | 33.17 |
| hedge, Up 60%, Down 38c | 29.54 | 29.54 |
| hedge, 40 Up + $50, Up 20%, Down 75c | 56.00 | 56.00 |
| Kelly with $q$ uncertain ($\pm$0.05) | 0.1200 | 0.1198 |
| four coins, $\rho = 0.6$ | 0.48 alone | 0.22 jointly |
