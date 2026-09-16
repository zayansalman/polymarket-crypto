# Operations Runbook

This runbook is for the local Polymarket crypto binary-markets strategy lab
(currently-wired path is inherited BTC 5-minute — see AGENTS.md for status).
The goal is to
make operation boring: visible state, bounded risk, and fast Stop behavior.
Paper mode is the default; live mode is strictly opt-in (see "Going live").

## Start Locally

```bash
./.venv/bin/python main.py
```

Open:

```text
http://127.0.0.1:7860
```

## Paper Trading

- Press **▶ Start** to begin the BTC 5-minute paper loop (paper is the default mode).
- Press **Stop** to halt new paper entries and force-close open simulated
  positions.
- Use **Refresh** if you want an immediate dashboard update between timer ticks.

## Health Checks

```bash
./.venv/bin/python tools/demo_snapshot.py
```

Expected:

- Risk state is `OK`, `IDLE`, or an explicit stale/feed state.
- Open positions are `0` or `1`.
- Activity feed contains BTC bot events.
- Start/Stop events are visible in structured logs and SQLite notifications.

## Common Issues

- If the dashboard port is busy, stop the old process or change
  `DASHBOARD_SERVER_PORT`.
- If no current BTC market is found, wait for the next 5-minute boundary and
  refresh.
- If public BTC spot data is unavailable, the paper loop surfaces the error in
  logs and dashboard detail instead of opening silent entries.

## Going Live

Live mode places REAL orders with REAL funds on the Polymarket CLOB. Read this
whole section before flipping the switch.

### Wallet setup (MetaMask)

Live trading uses the MetaMask account you already trade with on Polymarket.
Its funds sit in a Polymarket wallet (a Gnosis Safe your MetaMask key owns,
signature type 2) — no fund movement, no deploy, no approvals.

```bash
# 1. Put your MetaMask key in .env (MetaMask: Account details -> Show private key):
#       POLYMARKET_PRIVATE_KEY=0x...
# 2. Find your Polymarket wallet and write it into .env:
python3 -m pip install --pre polymarket-client   # one-time
python3 tools/live_detect_wallet.py
```

It derives your Polymarket wallet from the key, checks its on-chain USDC
balance, and writes `POLYMARKET_FUNDER` + `POLYMARKET_SIGNATURE_TYPE=2`.
**Security:** the key controls your whole MetaMask wallet, not just the
Polymarket balance — keep only what you're willing to expose on this machine.
`.env` and `.env.bak` are gitignored and written `0600`.

### Launch steps

1. Do the MetaMask wallet setup above (never commit `.env`). The key is
   never logged and never journaled.

2. Preflight — verifies the gate, credential derivation, CLOB reachability,
   and that the funder balance is actually visible to the CLOB:

   ```bash
   ./.venv/bin/python tools/live_preflight.py
   ```

   Do not launch on a NO-GO.

3. Start the app, click **LIVE** in the header toggle (no dialog — the click
   is the consent), then press **Start**:

   ```bash
   ./.venv/bin/python main.py
   ```

   > **The LIVE click is the consent.** There is no env confirm phrase, and an
   > env `BOT_MODE=live` default alone never trades — Start refuses a live boot
   > the operator didn't select in the dashboard (`polymarket_bot/controller.py`).
   > LIVE is always clickable; hover it to see whether it's armed. The private
   > key + funder from step 1 and a clean config parse are the remaining gates;
   > any missing one makes Start refuse.

4. Verify the dashboard says **LIVE — orders are real** and the activity feed
   shows `live_started`. If any boot gate is missing, Start refuses with
   an explicit error and nothing runs — live never silently falls back to paper.

### Hard risk limits (enforced in code before every order)

| Limit | Env var | Default |
| --- | --- | --- |
| Max notional per trade | `TRADE_MAX_USD` | $3 |
| Open positions | (fixed) | 1 |
| Daily realized-loss halt | `TRADE_DAILY_LOSS_HALT_USD` | $10 (UTC day, persisted) |
| Daily bankroll cap (sum of buys) | `TRADE_BANKROLL_CAP_USD` | **disabled** when blank/unset/≤0; positive number = cap (UTC day, persisted) |
| Entry slippage guard (ask vs signal) | `TRADE_MAX_ENTRY_SLIPPAGE` | 0.02 |
| Exit fill wait before cancel/retry | `LIVE_EXIT_FILL_TIMEOUT_SECONDS` | 10s |

The daily loss halt is on by default but is now operator-controllable from the
dashboard — see **Loss-halt operator controls (#76)** below. As of #76 the live
halt fires on the **live (real-money) leg only**; paper-study losses no longer
halt live trading. The daily bankroll cap is **opt-in** as of
v0.4.4: leave `TRADE_BANKROLL_CAP_USD` blank/unset and the cap gate is
bypassed (the spend counter still increments so the dashboard can show daily
throughput). When set to a positive dollar amount it behaves as before —
persisted in SQLite, restart-safe within the UTC day. Realized PnL feeds the
halt from CONFIRMED exit fills at the executed order's limit price, never from
paper-price estimates at submission time.

### Loss-halt operator controls (#76)

The LOSS HALT panel exposes two one-click controls that work in **both paper and
live** (the old "live halt can never be disabled from the UI" lock was removed):

- **STATUS pill (button)** — toggles the loss-halt **bypass**. `OK`/`HALTED` →
  click to disable the halt and keep trading past the limit; `BYPASS` → click to
  re-enable. In live this affects **real money**. Re-read by the loop every tick,
  so it takes effect without a restart.
- **Reset halt** — zeroes today's realized-loss tally so the halt clears. It is
  **stopped-only** (disabled while running, and the endpoint rejects a running
  bot) because the loop holds the counters in memory; the bankroll-cap notional
  is left untouched.

When the halt trips with bypass OFF, the bot now **auto-stops** (cancel + flatten,
same path as the kill switch) and the LAST DETAIL reads e.g. *"Daily loss halt:
live realized −$12.40 ≤ −$10.00. Bot stopped & flattened — Reset the halt, then
Start to resume."* Operator workflow after a halt: **Reset halt → Start**.
Pressing Start without resetting re-trips on the first tick and stops again.

Every bypass/reset is journaled to `notification_feed` (`loss_halt_bypass`,
`loss_halt_reset`, `loss_halt_stop`). On the first dashboard boot after
this change, a one-shot migration clears any stale paper-era bypass flag so live
starts halt-ON.

A malformed risk-limit env value (e.g. `TRADE_MAX_USD=O.50`) makes
live boot REFUSE with the exact parse error instead of silently falling back
to the looser default.

Blocked attempts are journaled to the `live_orders` table with status
`BLOCKED` — check it if the bot seems quiet.

Note: Polymarket enforces a minimum order size (typically 5 shares). With a $3
per-trade cap, entries above roughly $0.60/share are blocked as below-minimum;
this is expected and journaled.

### Boot reconciliation (restarts are safe)

Every live boot, BEFORE any trading:

1. **All resting CLOB orders on the account are cancelled** (`cancel_all`).
   Use a dedicated bot wallet — manual orders from the same wallet would be
   cancelled too.
2. Any **open ledger position is re-adopted** from the `live_orders`
   journal (token, entry price, exchange-confirmed fill size) so the normal
   exit path flattens it. Shares its exit orders already sold are restored
   too, so a later exit or settlement never counts them twice; a row whose
   exits already sold every share closes as `RECONCILED_FLAT` (Claude,
   2026-09-15, branch-review finding reconcile-resets-sold-size-double-books).
   Open rows
   with no live order behind them (paper artifacts, never-filled entries)
   are closed harmlessly with reason `RECONCILED_*`.
3. If the account state cannot be reconciled (CLOB unreachable, >1 open row),
   boot is REFUSED with instructions — the bot never trades on top of unknown
   exposure.

### Kill switch

```bash
touch data/KILL     # block all NEW entries NOW and cancel resting orders
rm data/KILL        # re-arm (the bot resumes on the next tick)
```

- The file is checked on every tick, before every entry, and once more
  immediately before the order is posted (covers the book-fetch window).
- **Exits stay allowed under kill**: flattening an open position only reduces
  exposure, and on a 5-minute binary a frozen position can become a 100%
  loss at resolution. If you want literally zero order flow, flatten manually
  in the Polymarket UI after touching the kill file.
- Deleting the file re-arms the handler: touching it again later cancels the
  orders resting at that moment again.

Drill this once before going live so you know it works.

### Stopping and flattening

- **Stop** on the dashboard sets the stop flag, then **waits for the runner
  thread to finish its own shutdown**: the runner cancels any resting entry
  order and flattens whatever filled through the live executor before it
  exits. The controller never drives the executor itself, so Stop cannot race
  the trading loop.
- A position whose live exit fails (no bid, CLOB error, unfilled sell) is
  **left OPEN in the ledger** — it is never paper-closed — and the Stop
  status tells you how many rows need manual flattening on Polymarket.
- If the process dies mid-position, just restart the bot — boot
  reconciliation cancels stale orders and re-adopts the position — or flatten
  manually in the Polymarket UI and close the row:

  ```bash
  sqlite3 data/btc_5m_binary_fair_value.db \
    "UPDATE paper_positions SET state='closed', exit_reason='MANUAL' WHERE state='open'"
  ```

### Settlement and redemption (EXIT_STYLE=settle, the default)

Settle-style positions ride to window resolution and never place exit
orders. The engine reads the Chainlink settlement (Up iff close ≥ open),
books the 1.00/0.00 outcome into the ledger and the daily-loss halt, and
frees the position slot.

- **Winning tokens are NOT auto-redeemed.** The USDC sits in resolved
  positions until you redeem them on Polymarket (portfolio → Claim). Redeem
  every few hours during live sessions so the bankroll keeps cycling — with
  a $30 bankroll and $1–5 entries, roughly 6–10 unredeemed wins will starve
  new entries.
- Losing tokens expire worthless; nothing to do.
- Legacy scalp behavior (intra-window TARGET/STOP/BAND exits, always flat
  before resolution) is available with `EXIT_STYLE=scalp` — note it
  soaked **negative** under honest fills and exists for experiments only.

### Live audit trail

Every order, cancel, and blocked attempt lands in SQLite:

```bash
sqlite3 data/btc_5m_binary_fair_value.db \
  "SELECT created_at, intent, side, price, size, status, error FROM live_orders ORDER BY id DESC LIMIT 20"
```

## Data

SQLite lives at `DB_PATH`, defaulting to `./data/btc_5m_binary_fair_value.db`.

The `data/` directory is local and gitignored.
