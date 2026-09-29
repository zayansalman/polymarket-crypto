# Operations Runbook

Running the EMS is meant to be boring: one process, visible state, paper unless the
operator arms LIVE (see Modes).

## Start

```bash
./.venv/bin/python main.py
```

Open `http://127.0.0.1:7860`. The market-data hub and the strategy start with the
process; there is no Start button. Stop the process to stop everything.

A second instance refuses to start while the first holds `data/bot.lock`.

## Controls

- **Strategy switch** (MY STRATEGIES card): off stops new entries on the next
  pass; open paper positions still settle. The change is logged to the activity
  feed.
- **Knobs** (SETTINGS card): bankroll, Kelly multiplier, max order, price levels,
  spot feed, which coins. Apply writes the value and the next pass uses it.
- **Kill switch:**

  ```bash
  touch data/KILL     # no new orders; resting paper orders are cancelled next pass
  rm data/KILL        # re-arm
  ```

  The path is `KILL_SWITCH_PATH` (default `$DATA_DIR/KILL`). The card shows
  "kill switch" while the file exists.
- **Refresh** re-renders the page between SSE pushes.

## Modes

PAPER/LIVE is chosen with the control in the top bar. `BOT_MODE` in `.env` only
sets the choice before one is made, and `BOT_MODE=live` is never consent to trade.

- **PAPER:** nothing is sent to the exchange. Both strategies trade on paper.
- **LIVE:** Kelly horse-race also sends its order to the exchange (post-only,
  good till the window end), only while all of these hold: LIVE clicked in the
  dashboard in this process, a wallet config that passes (`POLYMARKET_PRIVATE_KEY`,
  `POLYMARKET_FUNDER`, `POLYMARKET_SIGNATURE_TYPE=2`, and `pip install -e ".[live]"`)
  and its switch on. Its card says which condition is missing. Fade places
  nothing while LIVE is selected, and its paper orders already filled keep
  settling.
- **Before the first LIVE click:** `python tools/live_preflight.py` checks the
  wallet config, signs in and shows the USDC balance the exchange sees. It places
  no order.
- **Back to PAPER:** Kelly's resting live orders are cancelled on the next pass;
  their fills and settlement are still followed.
- Live risk limits are the "Risk" knobs in SETTINGS (per-trade cap $3, loss halt
  $10 a UTC day by default). Every live placement and cancel is in the
  `live_orders` table.

## Health checks

- The FEEDS card shows every hub connection, its state and the age of its last
  message. A red row that stays red is a network problem, not a strategy one.
- The fade card header says `running`, `setting up`, `switched off`, or the
  last error. "from an earlier run" on the last pass means this process has not
  completed a pass yet.
- The activity feed shows fills and settlements as they happen.
- Structured JSON logs go to stdout.

## Common issues

- Port busy: stop the old process or set `DASHBOARD_SERVER_PORT`.
- Binance unreachable: set `BINANCE_API_BASE` to a mirror that serves
  `/api/v3` (the default is `data-api.binance.vision`).
- No orders for a while: check the switch, the kill switch file, the requested
  mode, and the card's per-coin reasoning ("no order" is a valid decision when
  nothing adds growth).

## Data

SQLite lives at `DB_PATH` (default `./data/btc_5m_binary_fair_value.db`, kept
for existing data). The strategy's rows are the `fade_windows`,
`fade_decisions`, `fade_orders` and `fade_dials` tables; operator changes and
events are in `notification_feed`; switches and knobs in `config`.

```bash
sqlite3 data/btc_5m_binary_fair_value.db \
  "SELECT * FROM fade_orders ORDER BY id DESC LIMIT 20"
```

`data/` is local and gitignored.
