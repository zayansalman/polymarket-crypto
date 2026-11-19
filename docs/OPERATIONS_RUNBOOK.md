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

That address opens the **simple paper screen**: practice balance, Start/Stop,
the 5 min / 15 min choice, the current Up/Down window with the price to beat,
one plain-English line saying what the bot is doing and why, the open trade,
a balance chart, and the trade history. It also shows what the same trades
would have paid in estimated taker fees, because the paper ledger itself is
fee-free. The page is paper-only by construction: it is served without the
dashboard token, so it cannot select or start LIVE.

Below the trade history, **How accurate is the bot?** compares the chance the
bot gave each settled trade with how often those trades actually won, next to
the price paid and the profit after estimated fees. A bot with a real edge
wins more often than the price it pays.

**What it has learned** reports the learned-chances fit
(`polymarket_bot/learning.py`). Every hour, in a worker thread, the paper loop
refits `learned = sigmoid(a*logit(model) + b*logit(market mid))` from the tick
journal (ticks with more than 60 s left, every window weighted once, outcomes
from the next window's reference print, known shadow outcomes winning). Since
2026-11-19 the fit is used for paper entries while, replayed with the current
Settings over past windows (`strategy_lab.compare_learning`: walk-forward
fits, 5 shares, after fees), learned chances made more money than the raw
model; if that replay can't run it falls back to the old check (beat the raw
model's Brier score on the newest 20% of windows). Otherwise the bot trades on
the raw model. The fit is stored under `learning.blend_v1` in `config`, the
first active fit's time under `learning.started_at`, and each tick's learned
chance in `paper_ticks.learned_up_prob` (`fair_up_prob` stays the raw model).
*Use learned chances* in Settings switches it off. Live mode never uses it.

**Strategies tested side by side** compares this bot (counted from when
learning started) with the shadow roster, fee-true. The roster now always
uses its frozen config rules (`paper._shadow_params`), never the Settings
knobs, except `bot_raw_twin_v1`: the same live Settings rules on the raw
model, so the gap to this bot shows what learning adds. `learned_late_v1`
(60–180 s left) and `learned_mid_v1` (60–90¢ shares) are fixed-spec patterns
from the 2026-11-16..18 replay, judged only on windows after they started.
A verdict needs 50 settled trades and uses a strict z = 2.85 band because
about eleven strategies are compared at once; nothing switches automatically.

Since 2026-11-19: trade size on the simple screen is **shares only**. A dollar
range is refused (400): the RiskGate's per-trade cap stays at its $3 env
default for dollar sizing, so a $20–$100 range blocked every entry for 16 h
on 2026-11-18 (238 `per-trade cap` blocks), while N shares raise the cap to N
(`gate.effective_max_trade_usd`). The screen shows a banner when 3+ entries
were blocked in the last hour (`entry_blocks` in `/api/simple`, plain-English
reason). Learned shadow candidates now use the latest fit even when the main
bot has not adopted it, and two Strategy Lab grid hypotheses run as fixed
shadows: `raw_wide_v1` (raw, fee check, edge 3¢+, no cap) and
`learned_wide_v1` (learned, edge 3¢+, no cap).

Settings chosen from the data on 2026-11-18 evening (Luke asked for the best
parameters by the measured data): 5 shares, hold to settle, fee check on,
minimum edge 3¢ after fees, skip edges above 20¢ (kept as a bad-data guard),
share prices 50–95¢, no new trades in the last 60 s, learned chances on. On
387 replayed windows that combination made money in all four blocks (+$42.50,
171 trades, worst losing streak $14.56) vs −$54.20 and a $90 streak for the
previous rules; still within luck, so the forward record decides.

At 8:06 PM MT the same evening Luke asked for the Strategy Lab's most
profitable choices: minimum edge 2¢, edge limit 50¢ (no recorded trade had a
bigger edge, so this equals the Lab's "no limit" while keeping the
stale-model guard) and 10 shares a trade; the other rules were already the
Lab's picks. He then switched the practice balance from $100 to $500 (see
*Paper Trading* below). The strategies table counts every row at 5 shares a
trade, so this bot's 10-share trades are halved there to compare fairly with
the 5-share shadows. At 10 shares a full day of trading spends about $1,000,
close to the RiskGate's daily bankroll cap (`runtime.live.bankroll_cap_usd`,
$999), so on busy days late entries before the 00:00 UTC reset can show as
`daily bankroll cap` blocks.

The **Strategy Lab** (`/lab`, link in the footer) replays the recorded
history with `polymarket_bot/strategy_lab.py`: settle vs scalp (bid exits, a
second fee), trade sizes (drawdown, fills capped by top-of-book size), one
rule change at a time, and a 576-combination grid picked on the older half and
judged on the newer half. Read-only, cached 15 minutes (`/api/lab?refresh=true`).
Sizes are judged against the real practice balance (cash plus any open trade
at cost, `balance_usd` in `/api/lab`).

**Settings** (top bar, or the *Trading settings* section) edits the paper
rules without a restart: trade size (a fixed share count, or a dollar range
scaled by confidence), minimum edge, the stale-data edge limit, the share-price
range, the no-new-trades cutoff before a window ends, the check interval, and
an optional *Edge must beat trading fees* rule (off by default; subtracts the
estimated taker fee before the minimum-edge test). Every value is validated
before anything is saved; changes apply on the bot's next check and are logged
to the activity feed. *Reset rules to defaults* restores the rules but keeps
the trade size. The panel is refused while real-money mode is selected. The
rules are runtime knobs (`runtime.paper.*` in the `config` table), so the
advanced console shows the same values.

The full operator console (all panels, settings, LIVE controls) moved to:

```text
http://127.0.0.1:7860/advanced
```

## Paper Trading

- Press **▶ Start** to begin the BTC 5m or 15m paper loop (paper is the default mode).
- Select the timeframe in the dashboard. The 15m prototype is paper-only; live
  snapshots stay on BTC 5m.
- The default `settle` exit holds positions to resolution. For simulated
  intrawindow sells, select `scalp` in Settings; that experimental profile has
  negative historical results under honest fills.
- Press **Stop** to halt new paper entries and force-close open simulated
  positions.
- Use **Refresh** if you want an immediate dashboard update between timer ticks.
- Paper mode has a separate **virtual wallet** ($100 at first), initialized once in the
  local SQLite database. Simulated buys debit their entry cost; sells and
  settlements credit simulated proceeds. The `Paper Equity` ribbon shows
  virtual cash plus open-position value, marked at the current contract bid
  when available and otherwise at cost basis (shown in the ribbon).
- The virtual balance persists across restarts and is never automatically
  refilled. There is no dashboard reset control; resetting it requires a
  deliberate local database reset, which also removes the associated paper
  history. It is not connected to a Polymarket account or real funds.
- **Practice balance switch** (`POST /api/simple/wallet` with
  `{"start_usd": 500}`, paper mode only, on the operator's request): sets a
  new starting balance and multiplies the balance (cash plus any open trade at
  cost) by new start / old start, so the percentage gain or loss carries over.
  It is logged to Recent activity and stored in `config`
  `paper.wallet_rescales` with the last wallet transaction id before the
  switch. The simple screen counts every earlier trade at the same factor in
  the balance chart, totals, fee estimate and accuracy profits; the trade list
  shows trades as they happened with a marker row at the switch. An open trade
  keeps its real size. An optional `"as_of"` (ISO time, at most 24 h back)
  makes the switch count from when it was asked for: trades booked between
  then and the restart that installed the switch count at their real size,
  and their balances are shifted up by the switch amount on the chart. Luke
  asked for $100 → $500 at 8:12 PM MT on 2026-11-18, so trades before then
  count 5×. This is not a refill: a loss scales up just like a gain.

## Updates without a manual restart

When the app is started by `tools/windows_paper_startup.py` (desktop shortcut or
logon task), it sets `PAPER_SELF_RESTART=1` and the dashboard watches its own
Python files (`polymarket_exec/ops/dashboard/self_restart.py`). After a change
has been still for 20 s, every changed file compiles, the mode is paper and no
paper trade is open, it logs "Restarting to load an update" to Recent activity,
starts `windows_paper_startup.py restart --after-pid <pid>` and exits (the same
as ending Python in Task Manager, timed for a flat moment). The launcher waits
for the old process, then starts the app again in paper mode without opening a
window. A file that fails to compile blocks the restart and is reported in
Recent activity; real-money mode is never restarted. Page files (templates,
JS, CSS) and Settings never needed a restart.

The launcher opens the dashboard in its own window (Microsoft Edge app mode:
no tabs or address bar), falling back to the default browser if Edge is
missing.

## Repeatable Trial Checks

The focused unit tests use deterministic inputs and temporary SQLite databases;
they do not require a Polymarket account or market-data access. On Windows,
with Python 3.11 installed:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest tests\unit\test_settle_style.py tests\unit\test_live_wiring.py tests\unit\test_data_integrity.py tests\unit\test_mode_switch.py tests\unit\test_runtime_config.py tests\unit\test_offline_replay.py tests\unit\test_main_singleton.py tests\unit\test_windows_paper_startup.py -q
```

The Copilot app's **Run paper BTC trials** script runs the same test selection.
It creates `.venv` if needed, installs `.[test]`, removes GitHub and Polymarket
environment credentials from test subprocesses, and forces `BOT_MODE=paper`.
After adding or changing `.github/github-app.yml`, review and accept the
repository configuration in the app before using the script. It runs tests
only; it does not start the dashboard or contact market-data services.

## Automatic Windows Paper Startup

The local startup installer creates a persistent clone under
`%LOCALAPPDATA%\PolymarketCrypto\repository`, installs runtime dependencies in
its own `.venv`, and registers a current-user, limited-permission Task Scheduler
task. It opens the local dashboard at login, starts the BTC loop in paper mode,
and stores its database/logs under `%LOCALAPPDATA%\PolymarketCrypto`. It
requires the source branch to be pushed so the persistent clone can be created;
no administrator permission or Polymarket account is used.

Run once from the repository checkout:

```powershell
py -3.11 tools\windows_paper_startup.py install
```

The installer creates the **Start Polymarket Paper Trading** shortcut on your
Windows desktop. Double-click it whenever you want the dashboard opened and the
paper loop started. It reuses the durable installation and current dashboard;
repeated clicks do not create another runner. The dashboard starts or keeps
running only in paper mode, and its normal signal/risk guards still decide
whether to open a simulated position.

The task defaults to the saved BTC timeframe (5m for a fresh install). BTC 5m
and 15m are both supported, but only one timeframe can run in the current
single-loop/single-position design. Choose the active timeframe in the dashboard;
the automatic task resumes that selection at the next login. Entries are
strategy-gated, so a connected market does not guarantee a paper buy on every
window. The dashboard still displays the observed contract and skip reason.

Remove the scheduled start with:

```powershell
& "$env:LOCALAPPDATA\PolymarketCrypto\repository\.venv\Scripts\python.exe" `
  "$env:LOCALAPPDATA\PolymarketCrypto\repository\tools\windows_paper_startup.py" uninstall
```

Uninstall removes the login task but preserves the repository, database, and
logs, and removes the desktop launcher. A dashboard already running remains
open; use its Stop control to stop paper entries. `main.py` uses a
platform-specific singleton lock and now runs natively on Windows; no WSL
installation is needed.

For a historical replay against resolved BTC markets (downloads public data,
does not use account credentials), run:

```powershell
.\.venv\Scripts\python.exe tools\offline_replay.py --crypto BTC --timeframe 15-minute --max-markets 250 --output data\replay\btc-15m.json
```

Use `--timeframe 5-minute` for 5m markets. This replay takes at most one
position per window and holds it through resolution; it is not an intrawindow
buy-then-sell simulation. It uses market mid-prices as ask proxies, so results
are optimistic versus fills at the executable ask.

## Health Checks

```bash
./.venv/bin/python tools/demo_snapshot.py
```

Expected:

- Risk state is `OK`, `IDLE`, or an explicit stale/feed state.
- Open positions are `0` or `1`.
- Activity feed contains BTC bot events.
- Start/Stop events are visible in structured logs and SQLite notifications.
- The MARKET REGIME card's age is under ~3× the `Scan interval` knob (default
  60 s). Older means the always-on regime monitor has stalled: look for
  `regime.scan_failed` / `regime.*_unavailable` structlog events. A card that
  says "partial data" is not a fault — it lists exactly which input was
  missing (e.g. `book_out_of_phase` at a window edge, `book_absent` with the
  loop stopped and no venue tokens).

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
   exit path flattens it. Open rows with no live order behind them (paper
   artifacts, never-filled entries) are closed harmlessly with reason
   `RECONCILED_*`.
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
