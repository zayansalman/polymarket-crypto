# Copy trade — followed wallets

<!-- BEGIN GENERATED:strategy -->
| | |
|---|---|
| Name | Copy trade — followed wallets |
| Key | `copytrade` |
| Status | running now |
| Switch | `copy_macro_daily` on the COPY TRADE WALLETS card |
| Code | `polymarket_bot/copytrade/` — 6 files |
| Code fingerprint | `8c7aa4239b55` |
<!-- END GENERATED:strategy -->

## What it does

A paper-only copier. Every 12 seconds it reads the public activity feed of each followed wallet. Today there is one, [t-d901](https://polymarket.com/profile/0xd9013df863c1ba932780857b020dfdeacedf8e14). For each new buy on the kind of market the wallet was screened on, it prices a copy against the live ask ladder, adds the taker fee and books the result. It holds copies to resolution and settles them from the CLOB. Each copy is priced twice: once when the fill is seen and again about 25 seconds later. Every fill it declines is logged with a reason and scored.

## How it was formed

- **2026-08-14 — the question.** Zayan (operator), 2026-08-14: could the account [@mayormamdani](https://polymarket.com/profile/0xf8af03f1e68ee7162db8983f0d6dd0dc869854c6) be copy-traded? (#182, `tasks/lessons.md`). The session argued no, because the account was a maker and a copier has to cross and pay the fee. The operator pushed back twice. A mirror was built to measure it, and on its first 8 settled fills the account made +$24.79 and the copy +$22.81 (`tasks/lessons.md`). That tooling is now the `copytrade_v1` family.
- **2026-08-17 — a larger sample reversed it.** Two shadow runs lost money: −$174.47 over 1,756 settled fills and −$228.67 over 1,194, while the account made +$24.36 and +$83.52 on the same fills (`tasks/todo.md`). The same notes measured the public activity feed at about 20 seconds behind. Slippage grew with that lag: 2.82c at 0–2 seconds, 9.56c at 11–30 seconds.
- **2026-09-20 — rebuilt inside the dashboard.** `d83e5c3` added `polymarket_bot/copytrade/` as a watch-only panel, with targets taken from the new wallet screen (see the wallet-research doc). It used the ~20-second public feed on purpose. The first five targets traded daily macro markets that settled 2–3.5 hours after entry, and a replay showed a copier 30 minutes late still kept their edge. Who asked for the rebuild is not recorded. The same night brought these changes:
  - `aa43034` booked paper copies through `pairarb.mirror.price_the_copy`.
  - `c3d10c2` added hourly crypto targets so something would settle overnight.
  - `68e2a38` found that 7 of the 9 registered targets had left Up/Down markets.
  - `9d58a39`, `0fe9b03` and `d22d7a6` moved to taker-only screens, ending at 40 wallets on 1h and 15m markets.
- **2026-09-20 — measuring the copier itself.** Zayan named two gaps: skips were silent, and paper fills assumed the displayed ladder would still be there when the order landed (`490f013`). The fixes:
  - A decision log, and a second price taken about 25 seconds later.
  - `713d626` settles every copy both ways.
  - `5a0a6a0` moved settlement to the CLOB, because Gamma could not resolve 15m or hourly markets.
  - `94fe61d` scores declined fills.
  - `4d34a08` lets copies of fills under 5 shares round up to the venue minimum.
- **2026-09-20 — a correction.** An earlier headline said paper fills overstated the realistic result by 40%. It compared different rows. Like-for-like on the 14 rows that had both prices, paper was +$33.13 and realistic +$32.81 (`06467eb`).
- **2026-09-20 — down to one wallet.** `holdout_test.py` showed the 30-day screen had no predictive value out of sample (`a59e384`). `0c80179` replaced the 40-name list with one 15m wallet, chosen on three days of data by `recent_wallets.py` and `inspect_wallet.py`. The runner-up scored higher but had moved to 5-minute markets.
- **2026-09-21.** `7dae8ee` split autocopy into its own switch (`copy_autocopy`) and added a Copy button per fill. Both paths run `trader.consider`. The same commit made the watcher settle open copies even when switched off. `e7eda6f` (#267) deleted the Polygon on-chain feed: the operator confirmed this watcher had replaced it.

## How it works

**Polling** — `watcher.py`. `run_forever` runs inside the dashboard app (`polymarket_exec/ops/dashboard/app.py`, lifespan). Every `copy_poll_interval_seconds`, `CopyWatcher.poll_once` does the following:

1. If the watch switch `copy_macro_daily` is on, it polls every wallet in `targets.TARGETS` (`copy_follow_all`). For each wallet it calls `GET /activity?user=<address>&limit=100` on the Data API and keeps `TRADE` rows newer than that wallet's watermark. The watermark is a timestamp plus the `tx:size:price` keys seen in that same second. The first successful poll of a wallet is history (backfill) and is never copied.
2. Each fill is tagged `followed` by `_is_followed_impl`, based on its market slug:
   - Intraday targets: the slug contains `-up-or-down-` or `-updown-15m-`.
   - Daily targets: the slug contains `-up-or-down-on-`.
   Anything else is shown on the card but not copied.
3. If `copy_autocopy` is on, each fresh fill seen within 300 seconds of the wallet's fill goes to `trader.consider`. If autocopy is off, the Copy button on the card calls the same function (`api_copy_fill`).
4. Every poll, whether or not the watch switch is on, it re-prices copies that are due, settles resolved copies and scores resolved skips. Every 30 minutes it also snapshots the ledger.

**Deciding a copy** — `trader.consider`.

- **A sell by the target is never mirrored.** If we hold that outcome, the row is marked `target_exited` and logged `diverged`, and we still hold to resolution.
- **A fill that is not `followed` is skipped.** So is one whose book returns 404, which means the market closed before we saw the fill.
- **Otherwise it is priced** with `pairarb.mirror.price_the_copy`. With target size $s$ and scale $k$, it wants

$$q^\star=\max\big(5,\ \min(s\,k,\ 50)\big)\ \text{shares}.$$

If `copy_skip_below_min` is on and $s<5$, the fill is skipped. Otherwise it walks the ask ladder cheapest first up to $q^\star$. If fewer than 5 shares are on offer, it is skipped. Where the walk takes $q_i$ shares at ask $a_i$, and $p_t$ is the target's price:

$$\bar a=\frac{\sum_i q_i a_i}{\sum_i q_i},\qquad f(\bar a)=0.07\,\bar a\,(1-\bar a),\qquad \Delta=\bar a+f(\bar a)-p_t$$

The copy is skipped if $\Delta>0.03$. If not, it is booked at size $\sum_i q_i$, with cost equal to size times $(\bar a+f(\bar a))$ and fee $f(\bar a)$ per share. Every fill examined, copied or not, gets a `copy_decisions` row with its reason.

**Re-pricing** — `trader.requote_due`. Once a copy is at least 25 seconds old, the same size is walked again against the live ladder. The ledger stores the real price, the total fee, the cost and the size actually available. If the book is empty, the order is recorded as unfilled.

**Settling** — `trader.settle_due`. It reads CLOB `/markets/{condition_id}` and waits for `closed` and a `winner` flag. Three results are stored per copy:

- The paper result is $n\,\mathbb{1}[\text{won}]-\text{cost}$.
- The realistic result uses the re-priced size and cost. It is 0 when the book was gone and null when the copy was never re-priced.
- The target's own result on the same fill is $s\,(\mathbb{1}[\text{won}]-p_t)$, gross of any fee it paid.

**Scoring skips** — `trader.settle_skips`. A declined fill that had a price is settled at 15 minutes or later at the best ask. Its size is $\max(5,s)$ and the taker fee is included.

**Keeping the record** — `backup.write_snapshot`. It copies `copy_trades` and `copy_decisions` to `data/copytrade_snapshots/`, keeping the last 48 copies.

## Parameters

Knobs in `polymarket_bot/runtime_knobs.py` (group "Copy trade"), with the live `config` overrides as read on 2026-09-21:

| Knob | Default | Live | Where it came from |
|---|---|---|---|
| `copy_follow_all` | on | default | watcher.py: each target is its own experiment, and a paper lab runs several at once |
| `copy_target_wallet` | the t-d901 address | set 2026-09-20 07:41 UTC | The only entry in `targets.TARGETS` (`0c80179`) |
| `copy_scale` | 1.0 | default | Mirrors their size one to one |
| `copy_skip_below_min` | on (skip) | **off since 2026-09-19 22:49 UTC**: fills under 5 shares round up to 5 | `4d34a08`: 37% of fills were under the floor. Rounding up keeps them as samples but bets more than the target did |
| `copy_max_shares` | 50 | default | Not recorded |
| `copy_max_slippage_cents` | 3 | default | Not recorded. mirror.py notes that tightening it on 2026-08-14 discarded the target's winners. `filter_test.py` found the 3c rule separates nothing (`5d69d0c`) |
| `copy_poll_interval_seconds` | 12 s | 12 s | Not recorded |
| `copy_max_fill_age_seconds` | 300 s | default | `4305bde`: a fill that old cannot be followed at any latency |
| `copy_observe_limit` | 100 | default | Not recorded |
| 5-share floor | 5 | fixed | Venue minimum: Gamma `orderMinSize` = 5, CLOB `min_order_size` = 5 (`pairarb/mirror.py`) |
| Taker fee rate | 0.07 | fixed | `polymarket_bot/fees.py` |
| Re-price delay | 25 s | fixed | copytrade ledger: when a real order would land. `490f013`: the copier is 20–40 s behind |

The target, as recorded in `targets.py` (screened 2026-09-20 on the last three days of 15m markets: 996 markets, 822k fills):

| | |
|---|---|
| Wallet | [t-d901 — 0xd9013df863c1ba932780857b020dfdeacedf8e14](https://polymarket.com/profile/0xd9013df863c1ba932780857b020dfdeacedf8e14) |
| Taker share of notional | 96% |
| Taker edge after fee | +13.95c/share over 14,880 shares (+$2,076), 195 markets |
| Holds to resolution | 309 buys, 0 sells |
| Consistency | up on 2 of 3 days, 62% of markets positive, best 19 markets hold 148% of the total |
| t | not computed (recorded as 0 on purpose) |
| Average entry, stake, activity | 0.540, $8 a market, 103 fills a day |

## Evidence so far

Read-only from `copy_trades` and `copy_decisions` on 2026-09-21 at 07:12 UTC.

**Current target, since 2026-09-20 07:46 UTC.** The copier saw 48 of t-d901's fills:

- 38 were on 5-minute Up/Down markets (BTC, ETH, XRP, BNB, SOL, HYPE, DOGE). They were declined as outside the family the wallet was measured on.
- 9 were declined on the 3c slippage rule. Seven of them have been scored: taken, they would have made −$82.22, with 4 winners.
- 1 was copied: a BTC 15m market, bought at 0.22 against the wallet's 0.48, 50 shares. It lost −$11.60 paper and −$0.53 realistic. The wallet lost −$33.60 on its own fill.
- The feed ran on average 23.3 seconds behind the wallet's fills (1.6 to 89.2 seconds, n=48).

**All real copies so far.** 54 copies settled between 2026-09-20 00:55 and 07:58 UTC, from 17 wallets. 53 came from the earlier registries and one from t-d901.

| Over the same 54 rows | Result |
|---|---|
| Paper, priced when the fill was seen | −$98.35 (28 won) |
| Realistic, re-priced about 25 s later (0 unfilled) | −$57.00 |
| The wallets' own result on those fills, gross of their fee | −$148.27 on $763.59 staked |

Of the declined fills, 42 have been scored. Taken, they would have made −$126.48, with 21 winners. The wallets themselves lost on the fills we copied, which is in line with the holdout result (`a59e384`). The earlier registry was also down 43.3% at the wallets' own prices over its first 15 observed fills.

**Two record-keeping gaps.**

- The inventory figure of 55 copies and −$93.45 includes one test row. Its label is `t-x`, its timestamp is 1970-01-01, and it made +$4.90. `tests/unit/test_copytrade_ledger_populations.py` wrote it before that test was isolated from the live database. Without it, the figure is 54 copies and −$98.35.
- `copy_decisions` logs 28 copies between 2026-09-19 22:22 and 2026-09-20 00:33 UTC that have no row in `copy_trades`. `backup.py` records that a test with no database isolation once cleared this table. Those 28 are not in the totals above.

## Known weaknesses

- **The only target has moved to 5-minute markets.** 38 of its 48 observed fills (79%) were 5-minute markets. The screen required at least 60% of recent flow to stay in scope, and this same drift is why the runner-up was rejected (`0c80179`). Over the 23 hours the decision log covers, it produced one copy.
- **The feed is slow for these markets.** The feed runs about 20 seconds behind. `watcher.py` argues that is fine only for daily markets, and says 15m or 1h copying needs the Polygon feed in `pairarb/feed.py`. That file was deleted on 2026-09-21 (`e7eda6f`), and the target trades 15m markets.
- **Thin selection evidence.** The target was picked on three days, 195 markets, with no t computed (`targets.py`). The 30-day screen before it had no predictive value out of sample (`a59e384`).
- **Rounding up changes the bet.** With `copy_skip_below_min` off, a fill under 5 shares becomes a 5-share copy, a larger bet than the target made. The per-share edge is unchanged, but exposure is not a one-to-one mirror.
- **Entries only.** The copier never sells. If the target exits early, the copy holds to resolution, so its result stops tracking the target (`target_exited`).
- **Skip scores are on a different scale.** Declined fills are scored at the target's full size (at least 5 shares, with no 50-share cap) and at the best ask rather than a ladder walk. The skip totals are not directly comparable to the copies.
- **Names and comments are out of date.** The switch key is `copy_macro_daily`, which stays for storage reasons. The comment in `app.py` still says it follows daily macro markets.
- **No supervisor.** The copier runs inside the dashboard process. When that stops, polling, re-pricing and settlement stop with it.

## Sources

- `d83e5c3` 2026-09-20 — feat(copytrade): watch a target wallet's fills in the dashboard
- `aa43034` 2026-09-20 — feat(copytrade): paper-trade every target, settle on resolution
- `c3d10c2` 2026-09-20 — feat(copytrade): add the four crypto-hourly targets for overnight coverage
- `68e2a38` 2026-09-20 — feat(copytrade): show target drift before P&L
- `9d58a39` 2026-09-20 — feat(copytrade): screen for TAKER wallets, and retarget on them
- `0fe9b03` 2026-09-20 — feat(copytrade): follow 25 drift-checked taker wallets
- `490f013` 2026-09-20 — feat(copytrade): log every decision, and re-price fills as a real order would land
- `d22d7a6` 2026-09-20 — feat(copytrade): add 15-minute taker targets — 40 wallets, ~391 fills/day
- `4305bde` 2026-09-20 — fix(copytrade): never act on a stale fill
- `5a0a6a0` 2026-09-20 — fix(copytrade): settle from the CLOB — Gamma cannot resolve these markets
- `4d34a08` 2026-09-20 — feat(copytrade): mirror sub-floor clips at the venue minimum, and say so
- `713d626` 2026-09-20 — feat(copytrade): settle every copy twice — paper fill and realistic fill
- `94fe61d` 2026-09-20 — feat(copytrade): score the fills we declined
- `06467eb` 2026-09-20 — fix(copytrade): compare paper and realistic P&L over the same rows
- `a59e384` 2026-09-20 — test(wallet-research): holdout-test the screen — it does not predict
- `5d69d0c` 2026-09-20 — test(wallet-research): the fee, not the edge, is what is missing
- `0c80179` 2026-09-20 — feat(copytrade): one 15m target, chosen on three days, every trade on the card
- `7dae8ee` 2026-09-21 — feat(dashboard): split the strategy card, and show the whole tree in it
- `e7eda6f` 2026-09-21 — chore: retire the RPC fast-feed path — copytrade watcher replaced it
- PR #264 — carried every copytrade commit above except `7dae8ee` and `e7eda6f`
- PR #267 — dead-code deletion, including the Polygon feed
- Issue #182 — the @mayormamdani investigation
- `tasks/lessons.md` — 2026-08-14: the operator's question, pushback and the 8-fill result
- `tasks/todo.md` — 2026-08-17 shadow results; feed lag and slippage by lag
- `polymarket_bot/copytrade/watcher.py`, `trader.py`, `ledger.py`, `targets.py`, `backup.py`
- `polymarket_bot/pairarb/mirror.py` — `price_the_copy`, the 5-share floor, the slippage-guard note
- `polymarket_bot/fees.py` — the taker fee
- `polymarket_bot/runtime_knobs.py`, `polymarket_bot/strategies.py` — knobs and switches
- `polymarket_exec/ops/dashboard/app.py` — startup and `api_copy_fill`
- `tests/unit/test_copytrade_ledger_populations.py` — the source of the `t-x` row
- `tools/wallet_research/recent_wallets.py`, `inspect_wallet.py` — the target screen
- `data/btc_5m_binary_fair_value.db`, tables `copy_trades`, `copy_decisions` and `config`, read-only on 2026-09-21 at 07:12 UTC

## Changelog

- 2026-09-21 · `8c7aa4239b55` · Doc created.
