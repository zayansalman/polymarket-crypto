# Copy trade v1 CLI (shadow / live / on-chain)

<!-- BEGIN GENERATED:strategy -->
| | |
|---|---|
| Name | Copy trade v1 CLI (shadow / live / on-chain) |
| Key | `copytrade_v1` |
| Status | offline only |
| Switch | none — nothing to turn on |
| Code | `tools/copytrade_shadow.py` |
| Code fingerprint | `139e03684791` |
<!-- END GENERATED:strategy -->

## What it does

A command-line tool that watches one wallet's public trades, prices a copy of each one against the order book we would face at that moment, and settles the copy when the market resolves. It places no orders. It is what is left of the first copy-trade line; its four scratch ledgers were deleted on 2026-09-21, and the dashboard's copy trader in `polymarket_bot/copytrade/` now does the same job.

## How it was formed

- **2026-08-14, Zayan (operator).** Zayan asked whether the account [@mayormamdani](https://polymarket.com/profile/0xf8af03f1e68ee7162db8983f0d6dd0dc869854c6) could be copy-traded. Claude measured 5,072 of its trades, found it was mostly a maker, and argued a copier would start about 2.75c/share behind (issue #182). Zayan pushed back twice and asked for the copy to be built and measured instead (`5120c3c`, `tasks/lessons.md`).
- **Same day** the venue's 5-share floor (`9c880a3`), an asset filter, a size ceiling and a separate live executor `tools/copytrade_live.py` (`27951bb`), a slippage guard and read-only dashboard (`79e19e0`), and an on-chain fill listener `tools/copytrade_onchain.py` (`1fc0b85`) were added.
- **2026-08-17 to 08-29.** Shadow PnL turned negative at scale and blocked any live use (`3d93cb4`). A faster on-chain feed was wired into this tool behind `--feed rpc` (`5ff2bde`). On 2026-08-29 Zayan stopped all 5-minute work (`27f38fd`); #182 closed on 2026-08-30.
- **2026-09-21, PR #267.** Deleted the live executor (`21c468d`), the on-chain listener and this tool's `--feed rpc` path (`e7eda6f`). Only the API-polling shadow remains.

## How it works

- **Input.** `--target` wallet, default [0xf8af03f1e68ee7162db8983f0d6dd0dc869854c6](https://polymarket.com/profile/0xf8af03f1e68ee7162db8983f0d6dd0dc869854c6). Every 4 seconds `run()` reads the last 100 rows of the Data API `/activity` for that wallet, keeps `TRADE` rows (optionally only slugs matching `--assets`), and skips any already seen (`_key()`: tx hash, token, price, size, time).
- **Pricing.** `fetch_asks()` reads the current CLOB ask ladder for the token. `price_the_copy()` in `polymarket_bot/pairarb/mirror.py` walks it cheapest-first for $n=\max(5,\min(k\,s,m))$ shares, where $s$ is the target's size, $k$ is `--scale` (default 1) and $m$ is `--max-shares` (default 50), capped by displayed depth. With $\bar a$ the average ask paid, the copy costs $c=\bar a+0.07\,\bar a(1-\bar a)$ per share (`taker_fee_per_share`), and slippage is $c-p$ against the target's price $p$.
- **Declines** a trade when the outcome is not Up/Down, depth cannot cover 5 shares, or an optional filter trips: `--skip-small`, `--max-their-size`, `--max-slippage`.
- **Settlement.** `_settle_due()` takes copies older than 360 s; `resolve_window()` asks Gamma `/markets?slug=` and treats the window as resolved once one outcome price is at least 0.99. With $w=1$ if the copied side won and 0 if not, our PnL is $n(w-c)$ and the target's is $n(w-p)$, on our share count.
- **Output.** Rows in `copy_fills` in `data/copytrade_shadow.db` (or `--db`); `report()` prints both PnLs, their difference, slippage per share, and average fill against average cost.

## Evidence so far

- First 8 settled copies (2026-08-14): target +$24.79, copy +$22.81 (`37a9ab6`).
- 137 settled on the all-assets 5-share ledger (2026-08-14): target +$12.85, copy -$15.09. Replaying the slippage guard made the target's PnL on the fills it kept worse, +$6.29 to -$52.28 over 124 fills: large slippage meant the target was right (`79e19e0`).
- 2026-08-17 breakdown: bnb and doge were 83% of the all-assets loss; the 11-30 s lag bucket lost $81.15 while the target made $25.17. Both ledgers were pinned at exactly 5 shares, so they cannot be compared with the 8-fill sample (`e4bfbdb`, `tasks/todo.md`).
- Last read (2026-08-29): doge ledger -$307.68 over 1,980 settled fills, all-assets ledger -$174.47 over 1,756 (`27f38fd`).

## Known weaknesses

- Settles through Gamma, which drops 15-minute markets once they end (`settle_due` in `polymarket_bot/copytrade/trader.py`, `5a0a6a0`). A copy of a 15-minute trade would never settle.
- No age check: on start-up, up to 100 past trades not already in the ledger are priced against today's book.
- The Data API ran about 20 s behind the fill at the median (`tasks/todo.md`, 2026-08-17). The faster feed was deleted in #267.

## Sources

- `tools/copytrade_shadow.py`; `polymarket_bot/pairarb/mirror.py` (`price_the_copy`, `CopyFill`); `polymarket_bot/fees.py` (`taker_fee_per_share`); `polymarket_bot/inventory.py` (record line).
- Issue #182 (origin measurements; closing comment 2026-08-30). PR #267, merged as `456f1f2` on 2026-09-21.
- `5120c3c` 2026-08-14 — copytrade: live mirror shadow, priced against the book we would face (#182)
- `9c880a3` 2026-08-14 — copytrade: model the venue 5-share floor for small-capital copying (#182)
- `27951bb` 2026-08-14 — copytrade: asset filter, size ceiling, and a gated live executor (#182)
- `79e19e0` 2026-08-14 — copytrade: slippage guard + read-only dashboard (#182)
- `1fc0b85` 2026-08-14 — onchain: verified real-time fill detection to replace the 20s-stale feed (#182)
- `37a9ab6` 2026-08-14 — lessons: a structural-cost argument is a hypothesis until measured (#182); see `tasks/lessons.md`
- `3d93cb4` 2026-08-17 — docs: status check — stranded #180 fix branch, #182 shadow-PnL flag
- `e4bfbdb` 2026-08-29 — docs: decomposition + two real findings — sizing bug, fake feed gate (#182)
- `5ff2bde` 2026-08-29 — copytrade: key-free fast feed wired into the shadow tool only (#182)
- `27f38fd` 2026-08-29 — docs: branch close-out — pivot off all 5-minute markets (#182)
- `5a0a6a0` 2026-09-20 — fix(copytrade): settle from the CLOB — Gamma cannot resolve these markets
- `21c468d` 2026-09-21 — chore: delete the dead dashboard-reporting half of paper.py + two tools
- `e7eda6f` 2026-09-21 — chore: retire the RPC fast-feed path — copytrade watcher replaced it
- Endpoints in the code: `https://data-api.polymarket.com/activity`, `https://clob.polymarket.com/book`, `https://gamma-api.polymarket.com/markets`

## Changelog

- 2026-09-21 · `139e03684791` · Doc created.
