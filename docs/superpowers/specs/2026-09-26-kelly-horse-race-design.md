# Kelly horse-race — design

| | |
|---|---|
| Name and idea | Zayan (operator), 2026-09-22 |
| Build instructions | Zayan, 2026-09-22: "as the paper would do it and with randomness"; "completely randomise the notional between min shs required and 5$"; share one execution layer across strategies, the institutional pattern |
| Design | Claude, 2026-09-26 |
| Research | `tasks/2026-09-22-kelly-horse-race-research.md` (51 arXiv papers; settlement checks) |

Two pull requests, in this order:

1. **Shared resting-order layer** (`feat/resting-orders`): one execution layer for passive limit
   orders, used by every strategy, with a paper and a live endpoint.
2. **Kelly horse-race** (`feat/kelly-horse-race`): the strategy, built on that layer.

## 1. Shared resting-order layer

The strategy never talks to the exchange. It hands the layer "buy this token, at this price,
this many shares, until this time". The layer sends it to the paper or live endpoint and reports
fills. This is the order-management pattern trading desks use. Risk checks, the kill switch and
order tagging live in one place, and paper and live cannot drift apart.

**Source.** `polymarket_exec/execution/resting_orders.py` as written in the lc2004-Kronos BTC 24h
session on 2026-09-22 (uncommitted in worktree `polymarket-crypto-kronos-lc2004`, idle since).
It is ported without behaviour changes except where noted.

- `PlaceRequest`, `Placed`, `OrderView` and the `RestingVenue` protocol (`place`, `fills`,
  `cancel`, `closed_on_venue`).
- `PaperRestingVenue`: fills come from the real Polymarket taker tape (data-api
  `takerOnly=true`). An order fills only once taker volume has traded through the queue that
  was ahead of it: `filled = min(size, max(0, crossed - queue_ahead))`, counted with the
  maker's `crossed_volume` over trades after placement and before cancel or window end.
- `ClobRestingVenue`: GTD limit BUY with `post_only=True`, so the venue rejects any order that
  would cross. Expiry is the window end. Fills come from `get_order` `size_matched`. Cancels go
  by order id, never `cancel_all`. Every placement and cancel is journaled to `live_orders`,
  with the strategy name in `details`. The kill-switch file blocks placement.

**Changed from the source: protected order ids no longer depend on one strategy.** When the
`LiveExecutor` boots, it cancels every open order on the account. Resting orders owned by
strategies must survive that. The source read them from `lc2004_orders`, so they belonged to one
strategy only. Here they are read from the order journal itself. They are the `live_orders` rows
with `intent='ENTRY'`, `status='SUBMITTED'`, `order_type='GTD'` and `details.post_only` true,
whose `details.expiration` is still in the future, minus any id with a `CANCEL`/`CANCELLED` row.
The journal is the layer's own record, so any strategy that places through it is covered with
no registration step. GTD orders expire at the venue on their own, so a stale id can never keep
an order alive.

**`live.py` boot sweep** (ported): while protected ids exist, every other open order is
cancelled by id (`get_open_orders` then `cancel_orders`). With none, this step is exactly the
existing `cancel_all`. It is journaled with the kept and cancelled ids.

**Tests** (new `tests/unit/test_resting_orders.py`):
- paper fills against a fake tape: the queue ahead, the window-end cap and the cancel cap;
- the live venue against a fake CLOB client: the post-only GTD arguments, the kill switch,
  rejection journaling, cancel by id and fills from `size_matched`;
- the protected-id query;
- the boot sweep keeping protected ids.

## 2. Kelly horse-race

**Market.** Polymarket BTC 15m Up/Down. It settles on the Chainlink BTC/USD 60 s TWAP print at the
window close against the print at the open (`priceToBeat`). Checked two ways in the research
note: in 120 of 120 consecutive windows, and by an exact match to the RTDS print.

### The decision (once per window, shared by every endpoint)

Inputs, at decision time `t` in window `[t0, t1]`:

| input | source |
|---|---|
| `K`, price to beat | RTDS `chainlink_twap60` print at `t0` (the hub's price buffer); if missing, Gamma `eventMetadata.priceToBeat`; if both are missing, no decision this window, recorded |
| `X`, price now | latest RTDS `chainlink_twap60` print |
| `r60`, the 1h direction | Binance BTCUSDT 1m klines, last 61 closed bars: `ln(c60 / c0)` |
| `sigma_h`, hourly volatility | same bars: `sqrt(sum_i ln(c_i / c_{i-1})^2)` over the 60 returns |
| `tau`, hours left | `(t1 - t) / 3600` |

**Chance of Up.** This is a binary option in which the last hour's move carries forward as the
drift (research note, section 5; 2606.19517 with a forecast drift in place of the risk-neutral
one):

```
P(Up) = Phi( (ln(X / K) + r60 * tau) / (sigma_h * sqrt(tau)) )
```

With zero volatility, P(Up) is 1 or 0 by the sign of the numerator, and 0.5 when that is zero
too. Higher volatility pulls P(Up) toward 0.5. At the open, `X ≈ K`, so P(Up) =
`Phi(0.5 * r60 / sigma_h)`.

**Side: the dice.** Draw `u1` uniform on [0, 1) and buy Up if `u1 < P(Up)`, otherwise Down. Over
many windows, the stake lands on each side in proportion to its chance. That is Kelly's
horse-race split `b = p` (Kelly 1956; 1901.06278 Prop. 2). With at most $5 per window, the split
cannot happen inside one window, because each side needs the venue's minimum order. At passive
bid prices, Up + Down cost less than $1, and the horse-race rule is then growth-optimal with no
cash held (1901.06278 Prop. 9).

**Price.** One passive limit BUY at the chosen side's best bid. That is below the ask by
construction and never crosses, and the live venue enforces post-only. Read from CLOB REST
`/book` for that token: best bid, best ask, tick size, `min_order_size`, and the size resting at
the best bid, which becomes `queue_ahead`. There is no decision (recorded) if there is no bid,
or if the book is locked or crossed (`best_bid >= best_ask`).

**Size: random notional.** `lo = min_order_size * price`, `hi = max_notional` (knob, default
$5). Draw `u2` uniform on [0, 1). Shares are drawn uniformly between `min_order_size` and
`floor(hi / price, 0.01)`, which is the same as a uniform notional at a fixed price. If `lo > hi`,
there is no decision, recorded. Both draws (`u1`, `u2`) come from `random.SystemRandom` and are
stored with the decision so every window can be audited.

**Risk.** The request goes through the shared `RiskGate` leg for its mode (kill switch, loss halt,
per-trade and daily caps), as lc2004 does, and is recorded if blocked. The per-trade cap
(`runtime.max_trade_usd`, default $3) must be at least $5, or draws above it are blocked. This is
reported on the card, not hidden.

**Endpoints.** Paper is always on. Live is on only while the operator has LIVE selected, clicked
LIVE in this dashboard session, the wallet config passes and Start is pressed. These are the same
four conditions lc2004 uses. There is **one decision per window**: one P(Up), one pair of draws,
one price and one size. The same order goes to every active endpoint, so paper and live hold the
same position (the railroad-switch rule). Only the gate leg and the fills can differ, and both
are recorded per mode. An endpoint that becomes active partway through a window starts at the
next window. The strategy code never branches on mode; it calls the same methods on each active
endpoint.

### Bookkeeping (every pass, per endpoint)

- Refresh fills with `venue.fills`. Close any order the venue closed itself (`closed_on_venue`).
- From 60 s before `t1` (Polymarket stops a GTD order 60 s before its expiry), cancel what is
  still resting. Filled shares are kept, and the unfilled notional is credited back to the gate.
- Settle each window once CLOB `/markets/{condition_id}` shows `closed` with a winner token. PnL
  per filled share is `1[won] - price`. The fee is 0, because post-only orders only rest. Paper
  and live settle the same way.
- Switch off or kill file present: resting orders are cancelled and no new decisions are made.
  Bookkeeping and settlement continue.

### Storage

Its own two tables, created by the strategy's `ledger.init()` at loop start (the maker pattern),
so `db.py` is not touched:

- `kelly_horse_race_decisions`, one row per window: `t`, `K`, `X`, `r60`, `sigma_h`,
  `tau`, `P(Up)`, `u1`, side, `u2`, `min_order_size`, bid, ask, price, shares, notional, and
  either the order id or the reason there was no order.
- `kelly_horse_race_orders`, one row per order per mode (with the gate's block reason if blocked): mode, order id, token, outcome index, price,
  size, `queue_ahead`, `placed_ts`, `window_end_ts`, state (`resting`, `filled`, `cancelled`,
  `expired`, `rejected`), `filled_size`, `closed_ts`, and the result, payout and PnL.

### App wiring

- Package `polymarket_bot/kelly_horse_race/`: `maths.py` (pure: P(Up), the two draws, sizing),
  `inputs.py` (the reads), `ledger.py`, `runner.py` (`run_forever`, `pass_once`).
- `inventory.FAMILIES`: key `kelly_horse_race`, label "Kelly horse-race", status RUNNING.
  `strategies.STRATEGIES`: a switch, default on. `runtime_knobs`: `kelly_horse_race_max_notional_usd`
  (5.0) and `kelly_horse_race_poll_interval_seconds` (5).
- Dashboard lifespan task, as for the maker. MY STRATEGIES record (n, PnL, win rate), and a small
  panel with the latest decision factor by factor and the recent windows.
- `docs/strategies/kelly_horse_race.md` with all sections, including At a glance, and worked
  examples from real windows run through the real maths (`tools/kelly_horse_race/examples.py`).

### Tests

- `maths`:
  - P(Up) against the closed form;
  - its limits: zero volatility, and more volatility pulling toward 0.5;
  - dice frequencies with a seeded generator;
  - the share bounds and the `lo > hi` skip.
- `runner`, with a fake venue, fake inputs and a temporary database:
  - one decision per window, with the same order sent to each active endpoint;
  - never above the best bid;
  - a gate block is recorded;
  - cancel before the window end, with the credit back;
  - settlement PnL;
  - switch off cancels;
  - paper and live make the same calls.
- Doc and inventory checks pass (`test_strategy_docs`, `test_inventory`).

### Out of scope

Coins other than BTC. Fitting or recalibrating P(Up) from outcomes: the maths runs as written,
and the records show how it does. Scaled orders at more than one price level.
