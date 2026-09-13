"""Live execution on the Polymarket CLOB via py-clob-client.

Safety model
------------
* Boot is gated: live mode refuses to start unless ``POLYMARKET_PRIVATE_KEY``
  is set AND the wallet is the MetaMask setup: signature type 2 with the
  Polymarket Safe as funder (``tools/live_detect_wallet.py`` finds it). Consent is the operator clicking LIVE
  in the dashboard — the controller refuses a live Start that wasn't selected
  there — not an env phrase.
* Hard risk gates run BEFORE every order: per-trade notional cap, one open
  position max, daily realized-loss halt, and an OPTIONAL daily bankroll cap
  (disabled when ``TRADE_BANKROLL_CAP_USD`` is blank/unset/≤0). The daily
  counters are PERSISTED in SQLite and rebuilt at boot, so Stop/Start or a
  process restart cannot reset the daily loss halt or grant a fresh bankroll
  when the cap is enabled. The spend counter is still tracked when the cap is
  off — the dashboard surfaces daily throughput regardless.
* Boot reconciliation: ``start()`` cancels ALL resting CLOB orders on the
  account and re-adopts any open ledger position from the order journal, so
  a crash or restart never silently abandons real tokens or resting orders.
* A kill-switch file (``data/KILL`` by default) blocks all NEW entries and
  cancels resting orders the moment it appears, and re-arms automatically
  when the file is deleted. Exits stay ALLOWED under kill — flattening only
  reduces exposure.
* Exits never rest: the GTC SELL is awaited for a bounded time and cancelled
  if unfilled, so no stale exit order can sit in the book of a 5-minute
  market into resolution. Callers must treat a non-ok exit as "position
  still open — retry".
* Every order/cancel attempt — including blocked ones — is journaled to the
  ``live_orders`` SQLite table.

Threading: a single runner thread owns the executor for its whole life
(entries, exits, kill handling, shutdown flatten). The dashboard controller
never drives it directly, which is what makes Stop race-free.

The private key is never logged and never journaled.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

# Ensure project root importable (mirrors ops/dashboard/app.py convention).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import config as _config
from db import (  # type: ignore[import-untyped]
    connect,
    journal_live_order,
    notify,
)
from logging_setup import get_logger  # type: ignore[import-untyped]
from polymarket_bot.shadow.fees import taker_fee_per_share  # canonical venue fee math
from polymarket_exec.execution.gate import EntryRequest, GateConfig, RiskGate

log = get_logger("live")

BUY = "BUY"
SELL = "SELL"
# MetaMask-connected Polymarket accounts trade from a Gnosis Safe the key owns.
METAMASK_SIGNATURE_TYPE = 2

# Polymarket CLOB conventions (see installed py_clob_client_v2 source):
# - size granularity is 2 decimals for every tick size (ROUNDING_CONFIG.size == 2)
# - tick sizes are one of 0.1 / 0.01 / 0.001 / 0.0001 (GET /tick-size per token)
# - the order book reports min_order_size in shares (typically 5 for 5m markets)
SIZE_DECIMALS = 2
DEFAULT_TICK_SIZE = 0.01
DEFAULT_MIN_ORDER_SIZE = 5.0
# Auto-bump (#87): a configured clip too small to clear the venue's per-order
# share minimum is rounded UP to that minimum so it still places. Refuse to bump
# past this ceiling — a market demanding far more than the usual 5 shares would
# overspend the bankroll, so block instead of silently buying a large position.
MAX_AUTO_BUMP_SHARES = 2 * DEFAULT_MIN_ORDER_SIZE

# get_order statuses meaning the order can never trade again.
_TERMINAL_ORDER_STATUSES = {"matched", "canceled", "cancelled"}

# Boot reconciliation: Polymarket's order manager can return a transient
# 425 "order manager not ready, please retry" right after a cold start. The
# cancel-all is idempotent, so we retry it before refusing to boot live.
_BOOT_CANCEL_MAX_ATTEMPTS = 5
_BOOT_CANCEL_BACKOFF_SECONDS = 1.5


class LiveBootRefused(RuntimeError):
    """Raised when live mode is requested but the boot gate is not satisfied."""


def live_boot_problems(
    private_key: str | None = None,
    funder: str | None = None,
    signature_type: int | None = None,
) -> list[str]:
    """Every reason live boot would be refused right now (empty = armed)."""
    key = private_key if private_key is not None else _config.POLYMARKET_PRIVATE_KEY
    fund = funder if funder is not None else _config.POLYMARKET_FUNDER
    sig = (
        signature_type
        if signature_type is not None
        else _config.POLYMARKET_SIGNATURE_TYPE
    )
    problems: list[str] = []
    parse_errors = getattr(_config, "CONFIG_PARSE_ERRORS", [])
    if parse_errors:
        problems.append(
            "invalid env value(s): " + "; ".join(parse_errors)
            + " (fix the typo instead of trading on silent defaults)"
        )
    if not key:
        problems.append("POLYMARKET_PRIVATE_KEY is not set")
    if sig != METAMASK_SIGNATURE_TYPE:
        problems.append(
            f"POLYMARKET_SIGNATURE_TYPE={sig} is not {METAMASK_SIGNATURE_TYPE} "
            "(MetaMask) — run tools/live_detect_wallet.py"
        )
    if not fund:
        problems.append(
            "POLYMARKET_FUNDER (your Polymarket wallet address) is not set — run "
            "tools/live_detect_wallet.py"
        )
    return problems


def assert_live_boot_allowed(
    private_key: str | None = None,
    funder: str | None = None,
    signature_type: int | None = None,
) -> None:
    """Refuse live boot unless the operator config is complete AND coherent.

    Reads ``config.POLYMARKET_*`` at call time
    (not import time) so operators and tests can adjust config. Also refuses
    when any risk-limit env var failed to parse (config.CONFIG_PARSE_ERRORS):
    a typo in a risk limit must never silently degrade to looser defaults.
    """
    problems = live_boot_problems(private_key, funder, signature_type)
    if problems:
        raise LiveBootRefused(
            "Live mode boot REFUSED: " + " and ".join(problems) + ". "
            "Live trading places real orders with real funds; all gates are mandatory. "
            "The bot will NOT fall back to paper mode."
        )


@dataclass
class LiveOrderResult:
    """Outcome of one live order attempt."""

    ok: bool
    status: str  # SUBMITTED / BLOCKED / SKIPPED / ERROR / CANCELLED / UNFILLED / FLAT
    reason: str = ""
    order_id: Optional[str] = None
    price: Optional[float] = None
    size: Optional[float] = None
    notional_usd: Optional[float] = None
    raw: dict[str, Any] = field(default_factory=dict)


def _round_price_to_tick(price: float, tick: float) -> float:
    """Round a price to the nearest valid tick, clamped to [tick, 1 - tick]."""
    if tick <= 0:
        tick = DEFAULT_TICK_SIZE
    decimals = max(0, -int(math.floor(math.log10(tick))))
    rounded = round(round(price / tick) * tick, decimals)
    lower, upper = tick, round(1 - tick, decimals)
    return min(max(rounded, lower), upper)


def _round_size_down(size: float) -> float:
    """Round a share size DOWN to the CLOB size granularity (2 decimals)."""
    factor = 10**SIZE_DECIMALS
    return math.floor(size * factor) / factor


def _filled_shares(response: dict[str, Any]) -> float:
    """Outcome-token shares an order matched on submission (0 if none/unknown).

    A market-matched BUY returns status='matched' with the filled outcome
    tokens in ``takingAmount``; an unmatched GTC rests with status='live' and
    no taking amount. Anything unparseable is treated as no fill.
    """
    if str(response.get("status") or "").lower() != "matched":
        return 0.0
    try:
        return float(response.get("takingAmount") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _placement_crossed_shares(response: dict[str, Any], side: str) -> float:
    """Outcome-token shares that CROSSED at placement — the taker portion.

    The venue charges its taker fee only on this portion; anything that rests
    and fills later is a maker fill and fee-free. For a BUY the tokens are the
    ``takingAmount`` (received); for a SELL they are the ``makingAmount``
    (sold). Unknown/unparseable responses count as zero crossed (maker), so a
    fee is never invented.
    """
    if str(response.get("status") or "").lower() != "matched":
        return 0.0
    key = "takingAmount" if side == BUY else "makingAmount"
    try:
        return float(response.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


_WINDOW_SECONDS = 300  # 5-minute up/down markets
_WINDOW_RESOLVE_GRACE_SECONDS = 60


def _window_resolved(window_slug: str, *, now: float | None = None) -> bool:
    """True when the slug's window has certainly resolved.

    Window slugs end in the window's unix start second (…-5m-1782332700); the
    market resolves ``_WINDOW_SECONDS`` later. Unparseable slugs return False,
    so an unknown window is treated as possibly-live risk, never discarded.
    """
    try:
        start = int(str(window_slug).rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        return False
    now_s = time.time() if now is None else now
    return now_s >= start + _WINDOW_SECONDS + _WINDOW_RESOLVE_GRACE_SECONDS


def _journal_filled_shares(details_json: object) -> float:
    """Shares the journalled placement response matched at submit (0 if unknown).

    The ENTRY journal row stores the raw CLOB placement response under
    ``details_json.response`` — venue truth captured at submit time, still
    available after the CLOB has pruned the order itself.
    """
    try:
        details = json.loads(details_json) if details_json else {}  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    response = details.get("response") if isinstance(details, dict) else None
    return _filled_shares(response) if isinstance(response, dict) else 0.0


def _avg_fill_price(response: dict[str, Any], side: str, limit_price: float) -> float:
    """Average executed price from a matched order, else the limit (#103).

    A market-matched order reports ``makingAmount`` / ``takingAmount``. For a
    BUY the maker asset is USDC (paid) and the taker asset is outcome tokens
    (received), so the realised average price is ``makingAmount / takingAmount``;
    a SELL is the inverse. Falls back to ``limit_price`` whenever the amounts are
    absent or unusable (a resting / later-matched / unparseable order, or a price
    outside ``(0, 1]``), so this never regresses below the prior limit-price
    behaviour — it only *improves* the recorded price when the venue reports a
    real fill.
    """
    if str(response.get("status") or "").lower() != "matched":
        return limit_price
    try:
        making = float(response.get("makingAmount") or 0.0)
        taking = float(response.get("takingAmount") or 0.0)
    except (TypeError, ValueError):
        return limit_price
    if making <= 0 or taking <= 0:
        return limit_price
    px = (making / taking) if side == BUY else (taking / making)
    return px if 0.0 < px <= 1.0 else limit_price


class LiveExecutor:
    """Wraps a (synchronous) ``ClobClient`` behind an async, risk-gated API.

    All network calls run in a worker thread via ``asyncio.to_thread`` so the
    engine's event loop never blocks. A pre-built client can be injected for
    tests; production builds one from config in :meth:`start`.

    Single ownership: only the runner thread that called :meth:`start` may
    drive this object. There is no internal locking by design — the
    controller communicates via the stop event and waits for the runner to
    finish its own shutdown flatten.
    """

    def __init__(
        self,
        private_key: str,
        funder: str = "",
        signature_type: int = METAMASK_SIGNATURE_TYPE,
        *,
        host: str | None = None,
        chain_id: int | None = None,
        max_trade_usd: float | None = None,
        daily_loss_halt_usd: float | None = None,
        bankroll_cap_usd: float | None = None,
        max_entry_slippage: float | None = None,
        exit_fill_timeout_seconds: float | None = None,
        kill_switch_path: Path | None = None,
        client: Any | None = None,
    ) -> None:
        if not private_key and client is None:
            raise LiveBootRefused("LiveExecutor requires a private key.")
        self._private_key = private_key  # never logged, never journaled
        self._funder = funder
        self._signature_type = signature_type
        self._host = host or _config.POLYMARKET_CLOB_API
        self._chain_id = chain_id or _config.POLYMARKET_CHAIN_ID
        gate_cfg = GateConfig(
            max_trade_usd=(
                max_trade_usd if max_trade_usd is not None
                else _config.TRADE_MAX_USD
            ),
            daily_loss_halt_usd=(
                daily_loss_halt_usd if daily_loss_halt_usd is not None
                else _config.TRADE_DAILY_LOSS_HALT_USD
            ),
            bankroll_cap_usd=(
                bankroll_cap_usd if bankroll_cap_usd is not None
                else _config.TRADE_BANKROLL_CAP_USD
            ),
            max_entry_slippage=(
                max_entry_slippage if max_entry_slippage is not None
                else _config.TRADE_MAX_ENTRY_SLIPPAGE
            ),
            kill_switch_path=Path(
                kill_switch_path if kill_switch_path is not None
                else _config.KILL_SWITCH_PATH
            ),
        )
        # is_live=True → the gate halts on the live (real-money) leg (#76).
        self.gate = RiskGate(gate_cfg, is_live=True)
        # An explicit constructor value (tests; a caller that wants a fixed
        # timeout) always wins. Otherwise this tracks the dashboard-editable
        # ``live_exit_fill_timeout_seconds`` knob (#206) via the shared gate,
        # which is re-read every tick — so unset construction reflects operator
        # changes without a restart, same as the gate's other risk knobs.
        self._exit_fill_timeout_override = exit_fill_timeout_seconds

        self._client = client
        self._started = client is not None
        # Position / order tracking (max 1 open position by design)
        self._entry_order_id: Optional[str] = None
        self._entry_token_id: Optional[str] = None
        self._entry_price: Optional[float] = None
        self._entry_size: float = 0.0
        self._entry_matched_size: Optional[float] = None
        self._entry_sold_size: float = 0.0
        # USDC taker fee charged at entry on the placement-crossed portion
        # (0.07·p·(1−p) per share); 0 for maker fills. Booked at realization.
        self._entry_taker_fee_usd: float = 0.0
        self._position_open = False
        # Exit order tracking — only set while an exit SELL might still rest.
        self._exit_order_id: Optional[str] = None
        self._exit_price: Optional[float] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Authenticate, verify reachability, rebuild risk state, reconcile.

        Reconciliation makes restarts safe: all resting CLOB orders from any
        previous session are cancelled, and an open ledger position is
        re-adopted from the order journal so it keeps being managed. If the
        account state cannot be reconciled, boot is REFUSED — the bot never
        trades on top of unknown exposure.
        """
        if self._client is None:
            self._client = await asyncio.to_thread(self._build_client)
        creds = await asyncio.to_thread(self._client.create_or_derive_api_key)
        if creds is None:
            raise LiveBootRefused(
                "Could not create or derive Polymarket CLOB API credentials."
            )
        await asyncio.to_thread(self._client.set_api_creds, creds)
        # Reachability check (raises on network failure).
        await asyncio.to_thread(self._client.get_ok)
        # Refresh the CLOB's cached view of the funder's collateral balance
        # and allowance — the documented step before a first order. Best
        # effort: a refresh failure is logged, and an actually unfunded or
        # unapproved wallet will surface as order rejections (and in
        # tools/live_preflight.py), not as a silent boot failure here.
        try:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams

            await asyncio.to_thread(
                self._client.update_balance_allowance,
                BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=self._signature_type,
                ),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("live_executor.allowance_refresh_failed", error=str(e))
        await self.gate.load()
        # Pick up any operator-set dashboard knobs (#206) immediately at boot,
        # rather than waiting for the paper loop's first per-tick refresh.
        await self.gate.refresh_runtime_limits()
        await self._reconcile_account()
        log.info(
            "live_executor.started",
            host=self._host,
            signature_type=self._signature_type,
            funder_set=bool(self._funder),
            max_trade_usd=self.gate.effective_max_trade_usd,
            daily_loss_halt_usd=self.gate.effective_daily_loss_halt_usd,
            bankroll_cap_usd=self.gate.effective_bankroll_cap_usd,
            daily_realized_pnl=round(self.gate.daily_realized_pnl, 4),
            daily_buy_notional=round(self.gate.daily_buy_notional, 4),
            adopted_position=self._position_open,
            kill_switch=str(self.gate.cfg.kill_switch_path),
        )

    def _build_client(self) -> Any:
        # py-clob-client (v1) is archived and non-functional; v2 keeps the
        # same constructor surface incl. signature_type/funder (issue #31).
        from py_clob_client_v2 import ClobClient

        return ClobClient(
            self._host,
            chain_id=self._chain_id,
            key=self._private_key,
            signature_type=self._signature_type,
            funder=self._funder or None,
        )

    async def _reconcile_account(self) -> None:
        """Cancel resting orders from dead sessions and re-adopt open positions."""
        # 1) Cancel ALL resting orders on the account. A resting GTC in the
        # book of a 5-minute market from a dead session is pure downside.
        # Retry transient API errors (notably the 425 "order manager not ready,
        # please retry" Polymarket returns right after a cold start) — the
        # cancel is idempotent, so retrying is safe; only refuse after the
        # retries are exhausted, preserving the never-trade-on-unknown-orders
        # safety.
        raw: Any = None
        last_err: Exception | None = None
        for attempt in range(1, _BOOT_CANCEL_MAX_ATTEMPTS + 1):
            try:
                raw = await asyncio.to_thread(self._client.cancel_all)
                last_err = None
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.warning(
                    "live.boot_cancel_retry",
                    attempt=attempt,
                    max_attempts=_BOOT_CANCEL_MAX_ATTEMPTS,
                    error=f"{type(e).__name__}: {e}",
                )
                if attempt < _BOOT_CANCEL_MAX_ATTEMPTS:
                    await asyncio.sleep(_BOOT_CANCEL_BACKOFF_SECONDS * attempt)
        if last_err is not None:
            raise LiveBootRefused(
                f"Boot reconciliation failed: could not cancel resting orders "
                f"after {_BOOT_CANCEL_MAX_ATTEMPTS} attempts "
                f"({type(last_err).__name__}: {last_err}). Refusing to trade on "
                "top of unknown resting orders."
            ) from last_err
        await journal_live_order(
            intent="CANCEL_ALL", side="-", status="CANCELLED",
            details={"reason": "BOOT_RECONCILE", "response": raw},
        )

        # 2) Re-adopt any open ledger position so it keeps being managed.
        async with connect() as db:
            async with db.execute(
                "SELECT * FROM paper_positions WHERE state = 'open' ORDER BY opened_at"
            ) as cur:
                open_rows = [dict(r) for r in await cur.fetchall()]
        if not open_rows:
            return
        if len(open_rows) > 1:
            raise LiveBootRefused(
                f"Boot reconciliation failed: {len(open_rows)} open ledger positions "
                "found (max 1 by design). Resolve them manually (flatten on Polymarket, "
                "then UPDATE paper_positions SET state='closed', exit_reason='MANUAL' "
                "for each row) before restarting live mode."
            )
        row = open_rows[0]
        async with connect() as db:
            async with db.execute(
                "SELECT token_id, clob_order_id, price, size, details_json "
                "FROM live_orders "
                "WHERE intent = 'ENTRY' AND status = 'SUBMITTED' AND window_slug = ? "
                "ORDER BY id DESC LIMIT 1",
                (row["window_slug"],),
            ) as cur:
                entry = await cur.fetchone()

        if entry is None or not entry["token_id"] or not entry["clob_order_id"]:
            # No live order was ever submitted for this row — it is a paper
            # artifact (e.g. from a previous paper session). Closing it costs
            # nothing real.
            await self._close_ledger_row(row, "RECONCILED_NO_LIVE_TRACE")
            log.warning(
                "live_executor.reconcile_closed_paper_row",
                position_id=row["position_id"], window_slug=row["window_slug"],
            )
            return

        matched: float | None
        try:
            raw_order = await asyncio.to_thread(
                self._client.get_order, entry["clob_order_id"]
            )
            # The CLOB returns None once it has pruned an order (e.g. its
            # window resolved long ago) — no answer, not an error.
            matched = (
                float(raw_order.get("size_matched") or 0.0)
                if raw_order is not None
                else None
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "live_executor.reconcile_order_lookup_failed",
                order_id=entry["clob_order_id"],
                error=f"{type(e).__name__}: {e}",
            )
            matched = None

        if matched is None:
            # No venue answer for the order. A resolved window holds no
            # executable risk, so refusing boot protects nothing — close the
            # stale row and let tools/reconcile_live_ledger.py true-up its
            # realized PnL from the Data API. For a window still in flight,
            # the journal's own placement response is venue truth from submit
            # time: adopt any match it recorded; refuse only when live risk is
            # genuinely unknowable.
            journal_matched = _journal_filled_shares(entry["details_json"])
            if _window_resolved(row["window_slug"]):
                await self._close_ledger_row(row, "RECONCILED_STALE_RESOLVED")
                await notify(
                    "live_reconciled",
                    f"Closed stale live position {row['position_id']} "
                    f"({row['window_slug']}): its window already resolved and "
                    "the CLOB no longer returns the entry order. Run "
                    "tools/reconcile_live_ledger.py to true-up realized PnL.",
                    {
                        "position_id": row["position_id"],
                        "journal_matched": journal_matched,
                    },
                )
                log.warning(
                    "live_executor.reconcile_closed_stale_resolved",
                    position_id=row["position_id"],
                    window_slug=row["window_slug"],
                    journal_matched=journal_matched,
                )
                return
            if journal_matched > 0:
                matched = journal_matched
            else:
                raise LiveBootRefused(
                    f"Boot reconciliation failed: the CLOB could not return "
                    f"entry order {entry['clob_order_id']} for open position "
                    f"{row['position_id']} and window {row['window_slug']} has "
                    "not resolved yet — refusing to trade blind on live risk. "
                    "Retry once the CLOB is reachable, or flatten manually on "
                    "Polymarket and close the ledger row."
                )

        if matched <= 0:
            # Entry never filled and its remainder was just cancelled by
            # cancel_all — there is nothing real behind this row.
            await self._close_ledger_row(row, "RECONCILED_UNFILLED")
            log.info(
                "live_executor.reconcile_closed_unfilled",
                position_id=row["position_id"], window_slug=row["window_slug"],
            )
            return

        # Adopt: the resting remainder is already cancelled; track the filled
        # size so the normal exit path flattens it.
        self._entry_order_id = None
        self._entry_token_id = str(entry["token_id"])
        self._entry_price = float(entry["price"] or row["entry_price"])
        self._entry_size = float(entry["size"] or row["shares"])
        self._entry_matched_size = matched
        self._entry_sold_size = 0.0
        # Bot entries are marketable limits, so assume the adopted fill
        # crossed (taker) — the reconcile tool is the exact true-up.
        self._entry_taker_fee_usd = round(
            taker_fee_per_share(self._entry_price) * matched, 6
        )
        self._position_open = True
        await notify(
            "live_reconciled",
            f"Re-adopted open live position from a previous session: "
            f"{matched:.2f} shares of {row['side']} in {row['window_slug']}. "
            "It will be flattened by the normal exit path.",
            {"position_id": row["position_id"]},
        )
        log.warning(
            "live_executor.reconcile_adopted_position",
            position_id=row["position_id"], matched=matched,
            window_slug=row["window_slug"],
        )

    @staticmethod
    async def _close_ledger_row(row: dict[str, Any], reason: str) -> None:
        async with connect() as db:
            await db.execute(
                "UPDATE paper_positions SET state = 'closed', closed_at = ?, "
                "exit_reason = ?, realized_pnl_usd = COALESCE(realized_pnl_usd, 0) "
                "WHERE position_id = ?",
                (datetime.now(UTC).isoformat(timespec="seconds"), reason,
                 row["position_id"]),
            )
            await db.commit()

    # ------------------------------------------------------------------
    # Gate proxies — single source of truth is self.gate (issue #64)
    # ------------------------------------------------------------------

    @property
    def max_trade_usd(self) -> float:
        return self.gate.effective_max_trade_usd

    @property
    def daily_loss_halt_usd(self) -> float:
        return self.gate.effective_daily_loss_halt_usd

    @property
    def bankroll_cap_usd(self) -> float | None:
        cap = self.gate.effective_bankroll_cap_usd
        return cap if cap and cap > 0 else None

    @property
    def max_entry_slippage(self) -> float:
        return self.gate.effective_max_entry_slippage

    @property
    def exit_fill_timeout_seconds(self) -> float:
        if self._exit_fill_timeout_override is not None:
            return self._exit_fill_timeout_override
        return self.gate.effective_exit_fill_timeout_seconds

    @property
    def kill_switch_path(self) -> Path:
        return self.gate.cfg.kill_switch_path

    def kill_switch_active(self) -> bool:
        return self.gate.kill_switch_active()

    async def enforce_kill_switch(self) -> bool:
        """If the kill file exists, cancel open orders once and halt entries.

        Re-arms automatically when the file is removed, so touching the file
        again later triggers the cancel sweep again. Exits remain allowed
        while the kill switch is active: flattening only reduces exposure.
        """
        if not self.gate.kill_switch_active():
            if self.gate.kill_already_handled():
                self.gate.rearm_kill()
                log.info(
                    "live_executor.kill_switch_rearmed",
                    path=str(self.gate.cfg.kill_switch_path),
                )
            return False
        if not self.gate.kill_already_handled():
            self.gate.mark_kill_handled()
            log.error(
                "live_executor.kill_switch_triggered",
                path=str(self.gate.cfg.kill_switch_path),
            )
            await notify(
                "live_kill_switch",
                f"KILL switch file detected at {self.gate.cfg.kill_switch_path}. "
                "New entries halted; cancelling resting orders. Open positions "
                "will still be flattened by the exit path.",
            )
            await self.cancel_open(reason="KILL_SWITCH")
        return True

    async def record_realized_pnl(self, pnl_usd: float) -> None:
        """Feed realized PnL into the shared daily-loss-halt tracker."""
        await self.gate.record_realized_pnl(pnl_usd, is_live=True)

    async def record_settlement(self, won: bool, window_slug: str) -> LiveOrderResult:
        """Register a resolution outcome for the held position without selling.

        Settle-style positions ride to resolution: winning tokens redeem at
        $1.00 (redemption is an operator action — see the runbook), losing
        tokens expire worthless. PnL feeds the daily-loss halt exactly like
        an exit fill, and the position slot frees for the next window.
        """
        if not self._position_open:
            return LiveOrderResult(
                ok=False, status="SKIPPED", reason="no live position tracked"
            )
        if self._entry_order_id is not None:
            # The market has resolved; any resting entry remainder is dead.
            # Cancel it so boot reconciliation never re-adopts a ghost order.
            # Conservative fill fallback: settling never sells, so a failed
            # venue lookup must NOT manufacture held size (#109).
            await self.cancel_open(reason="SETTLEMENT", assume_filled_on_error=False)
        matched = await self._matched_entry_size(assume_filled_on_error=False)
        held = _round_size_down(max(0.0, matched - self._entry_sold_size))
        entry_price = self._entry_price or 0.0
        payout = 1.0 if won else 0.0
        # Realize net of the entry taker fee (#133): the venue charged it in
        # USDC at entry, so a lost taker position costs exactly the cash paid
        # and a won one redeems at $1/share minus that entry fee.
        fee = self._entry_taker_fee_usd if held > 0 else 0.0
        pnl = round(held * (payout - entry_price) - fee, 4)
        if held > 0:
            await self.record_realized_pnl(pnl)
        await journal_live_order(
            intent="SETTLEMENT",
            side=SELL,
            status="SETTLED",
            window_slug=window_slug,
            token_id=self._entry_token_id,
            price=payout,
            size=held,
            notional_usd=pnl,
            error=None if won else "resolved against position; tokens worthless",
            details={"entry_taker_fee_usd": fee},
        )
        log.info(
            "live_executor.settled",
            window_slug=window_slug,
            won=won,
            held=held,
            pnl=pnl,
        )
        self._clear_position()
        return LiveOrderResult(
            ok=True, status="SETTLED", price=payout, size=held, notional_usd=pnl
        )

    @property
    def daily_realized_pnl(self) -> float:
        return self.gate.daily_realized_pnl

    @property
    def daily_buy_notional(self) -> float:
        return self.gate.daily_buy_notional

    def entry_block_reason(
        self,
        notional_usd: float,
        *,
        side_price: float | None = None,
        best_ask: float | None = None,
    ) -> str | None:
        """Return why a new entry is blocked, or None if all risk gates pass.

        Live-only callers omit ``side_price`` / ``best_ask`` and apply the
        slippage guard separately after fetching the live book; the unified
        gate handles slippage too when both prices are supplied.
        """
        return self.gate.block_reason(
            EntryRequest(
                notional_usd=notional_usd,
                position_open=self._position_open,
                entry_order_resting=self._entry_order_id is not None,
                side_price=side_price,
                best_ask=best_ask,
            )
        )

    # ------------------------------------------------------------------
    # Market metadata helpers
    # ------------------------------------------------------------------

    async def _book_context(self, token_id: str) -> tuple[float | None, float | None, float, float]:
        """Return (best_ask, best_bid, tick_size, min_order_size) for a token.

        py-clob-client order books list levels from worst to best, so the best
        ask/bid is the LAST element (see builder.calculate_*_market_price).
        ``py-clob-client-v2`` returns the raw JSON dict; the v1 SDK returned an
        ``OrderBookSummary`` object — accept either. Falls back to safe defaults
        when the book is unavailable.
        """
        best_ask: float | None = None
        best_bid: float | None = None
        tick = DEFAULT_TICK_SIZE
        min_size = DEFAULT_MIN_ORDER_SIZE
        try:
            book = await asyncio.to_thread(self._client.get_order_book, token_id)
        except Exception as e:  # noqa: BLE001
            log.warning("live_executor.order_book_failed", error=str(e))
            return best_ask, best_bid, tick, min_size

        def _get(obj: object, name: str, default: object = None) -> object:
            if isinstance(obj, dict):
                return obj.get(name, default)
            return getattr(obj, name, default)

        def _level_price(level: object) -> float | None:
            p = _get(level, "price")
            return float(p) if p is not None else None

        try:
            asks = _get(book, "asks") or []
            bids = _get(book, "bids") or []
            if asks:
                best_ask = _level_price(asks[-1])
            if bids:
                best_bid = _level_price(bids[-1])
            t = _get(book, "tick_size")
            if t is not None:
                tick = float(t)
            m = _get(book, "min_order_size")
            if m is not None:
                min_size = float(m)
        except (TypeError, ValueError, AttributeError) as e:
            log.warning("live_executor.order_book_parse_failed", error=str(e))
        return best_ask, best_bid, tick, min_size

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    async def submit_entry(
        self,
        token_id: str,
        side_price: float,
        notional_usd: float,
        window_slug: str | None = None,
    ) -> LiveOrderResult:
        """Place a GTC limit BUY at the best ask, sized from *notional_usd*.

        Runs every risk gate first; blocked attempts are journaled with
        status BLOCKED and never reach the network. A slippage guard blocks
        the entry when the live ask has moved too far above the signal price
        that justified the trade.
        """
        # First-pass gate WITHOUT slippage (no book yet). Cheap pre-filter for
        # kill switch, daily-loss halt, singleton, per-trade cap, bankroll cap.
        blocked = self.entry_block_reason(notional_usd)
        if blocked is not None:
            await self._journal_blocked(
                intent="ENTRY", side=BUY, reason=blocked,
                window_slug=window_slug, token_id=token_id,
                notional_usd=notional_usd, mode="live",
            )
            return LiveOrderResult(ok=False, status="BLOCKED", reason=blocked)
        if not token_id:
            reason = "no CLOB token id available for this market/outcome"
            await self._journal_blocked(
                intent="ENTRY", side=BUY, reason=reason,
                window_slug=window_slug, notional_usd=notional_usd, mode="live",
            )
            return LiveOrderResult(ok=False, status="BLOCKED", reason=reason)

        best_ask, _, tick, min_size = await self._book_context(token_id)
        # Second pass: re-evaluate INCLUDING the slippage guard now that we
        # have the live book. The kill switch / counters could also have
        # tipped over during the book fetch — re-checking is free.
        blocked = self.entry_block_reason(
            notional_usd, side_price=side_price, best_ask=best_ask
        )
        if blocked is not None:
            await self._journal_blocked(
                intent="ENTRY", side=BUY, reason=blocked,
                window_slug=window_slug, token_id=token_id,
                price=best_ask, notional_usd=notional_usd, mode="live",
            )
            return LiveOrderResult(ok=False, status="BLOCKED", reason=blocked)
        raw_price = best_ask if best_ask is not None else side_price
        price = _round_price_to_tick(raw_price, tick)
        size = _round_size_down(notional_usd / price)
        if size < min_size:
            # Auto-bump (#87): round a sub-minimum clip UP to exactly the venue
            # minimum so a small configured size still places. The per-trade cap is
            # a target the venue minimum may exceed — bounded by MAX_AUTO_BUMP_SHARES
            # so an abnormally large minimum blocks instead of overspending.
            if min_size > MAX_AUTO_BUMP_SHARES:
                reason = (
                    f"venue minimum {min_size:.2f} shares exceeds the auto-bump "
                    f"ceiling {MAX_AUTO_BUMP_SHARES:.2f} at price {price:.4f} "
                    f"(would cost {min_size * price:.2f} USD)"
                )
                await self._journal_blocked(
                    intent="ENTRY", side=BUY, reason=reason,
                    window_slug=window_slug, token_id=token_id,
                    price=price, size=size, notional_usd=notional_usd, mode="live",
                )
                return LiveOrderResult(ok=False, status="BLOCKED", reason=reason)
            log.info(
                "live_executor.entry_bumped_to_min",
                requested_size=round(size, 2), bumped_size=min_size,
                price=price, requested_notional=round(notional_usd, 2),
                bumped_notional=round(min_size * price, 2),
            )
            size = min_size

        result = await self._place_order(
            intent="ENTRY", token_id=token_id, side=BUY,
            price=price, size=size, window_slug=window_slug,
        )
        if result.ok:
            self._entry_token_id = token_id
            # Record the REAL average fill price when the venue matched the order
            # (makingAmount/takingAmount), not the posted limit — the limit
            # overstates PnL whenever the order fills better than the ask (#103).
            self._entry_price = _avg_fill_price(result.raw, BUY, price)
            self._entry_size = size
            self._entry_sold_size = 0.0
            # The venue charges its taker fee, in USDC, on the shares that
            # crossed at placement; a resting (maker) remainder is fee-free.
            crossed = _round_size_down(_placement_crossed_shares(result.raw, BUY))
            self._entry_taker_fee_usd = (
                round(taker_fee_per_share(self._entry_price) * min(crossed, size), 6)
                if crossed > 0
                else 0.0
            )
            self._position_open = True
            await self.gate.record_buy_notional(round(price * size, 4))
            filled = _round_size_down(_filled_shares(result.raw))
            if filled >= size:
                # Fully matched on submission: nothing rests in the book, so
                # this is NOT a resting entry order. Drop the id so the gate's
                # `entry_order_resting` stays honest (the open position holds
                # the max-1 slot) and the next flatten skips a doomed
                # "matched orders can't be canceled" round trip.
                self._entry_order_id = None
                self._entry_matched_size = filled
            else:
                self._entry_order_id = result.order_id
                self._entry_matched_size = None
        return result

    async def submit_exit(
        self,
        token_id: str | None = None,
        side_price: float | None = None,
        size: float | None = None,
        window_slug: str | None = None,
    ) -> LiveOrderResult:
        """Flatten the FILLED entry size with a GTC limit SELL at the best bid.

        Allowed even while the kill switch is active — flattening only reduces
        exposure. The SELL is awaited for a bounded time and cancelled if it
        does not fill, so no stale exit order ever rests in the book of a
        5-minute market. Realized PnL is recorded here, on confirmed fills at
        the exit order's limit price — never on submission alone.

        Returns ok=True only when the position is confirmed flat (or was
        confirmed never-filled, status SKIPPED is returned with ok=False but
        nothing real remains). Callers MUST treat any other non-ok result as
        "real tokens may still be held — keep the position open and retry".
        """
        token = token_id or self._entry_token_id
        if not token:
            reason = (
                "no live entry tracked; cannot flatten "
                "(restart reconciliation should have adopted it — manual check required)"
            )
            await journal_live_order(
                intent="EXIT", side=SELL, status="ERROR",
                window_slug=window_slug, error=reason,
            )
            log.error("live_executor.exit_untracked", window_slug=window_slug)
            return LiveOrderResult(ok=False, status="ERROR", reason=reason)

        # Clear any stale exit SELL from a previous attempt whose cancel
        # failed, so we never have two exit orders working at once.
        if self._exit_order_id is not None:
            stale_id, stale_price = self._exit_order_id, self._exit_price
            if not await self._try_cancel(stale_id, reason="EXIT_RETRY"):
                return LiveOrderResult(
                    ok=False, status="ERROR",
                    reason="could not cancel stale exit order; will retry",
                )
            sold, order_price = await self._order_fill_info(stale_id, default_size=0.0)
            self._exit_order_id = None
            self._exit_price = None
            await self._register_exit_fill(sold, stale_price or order_price)

        # Cancel any still-resting entry remainder before flattening, so the
        # bot is never buying and selling the same token simultaneously.
        if self._entry_order_id is not None:
            await self.cancel_open(reason="EXIT_FLATTEN")
            if self._entry_order_id is not None:
                return LiveOrderResult(
                    ok=False, status="ERROR",
                    reason="could not cancel resting entry order; exit deferred",
                )

        matched = await self._matched_entry_size()
        remaining = _round_size_down(max(0.0, matched - self._entry_sold_size))
        sellable = remaining if size is None else _round_size_down(min(size, remaining))
        if sellable <= 0:
            if matched <= 0:
                self._clear_position()
                reason = "entry order has no matched size; nothing to sell"
                await journal_live_order(
                    intent="EXIT", side=SELL, status="SKIPPED",
                    window_slug=window_slug, token_id=token, error=reason,
                )
                return LiveOrderResult(ok=False, status="SKIPPED", reason=reason)
            # Earlier partial exits already sold everything that filled.
            self._clear_position()
            await journal_live_order(
                intent="EXIT", side=SELL, status="FLAT",
                window_slug=window_slug, token_id=token,
                error="already fully flattened by earlier exits",
            )
            return LiveOrderResult(
                ok=True, status="FLAT", reason="already fully flattened", size=0.0
            )

        _, best_bid, tick, _ = await self._book_context(token)
        raw_price = best_bid if best_bid is not None else (side_price or 0.0)
        if raw_price <= 0:
            reason = "no bid available to price the exit"
            await self._journal_blocked(
                intent="EXIT", side=SELL, reason=reason,
                window_slug=window_slug, token_id=token, size=sellable,
            )
            return LiveOrderResult(ok=False, status="BLOCKED", reason=reason)
        price = _round_price_to_tick(raw_price, tick)

        result = await self._place_order(
            intent="EXIT", token_id=token, side=SELL,
            price=price, size=sellable, window_slug=window_slug,
        )
        if not result.ok:
            return result

        # Track the resting SELL until it is confirmed filled or cancelled.
        self._exit_order_id = result.order_id
        self._exit_price = price
        filled = await self._await_fill(result.order_id, sellable)
        if filled >= sellable:
            self._exit_order_id = None
            self._exit_price = None
            await self._register_exit_fill(
                sellable,
                _avg_fill_price(result.raw, SELL, price),
                taker_size=_placement_crossed_shares(result.raw, SELL),
            )
            if self._entry_sold_size >= matched:
                self._clear_position()
            return result

        # Not filled in time: cancel so no stale SELL rests into resolution.
        if not await self._try_cancel(result.order_id, reason="EXIT_FILL_TIMEOUT"):
            # Cancel failed — keep tracking the order id so the next attempt
            # clears it before placing a new SELL (no double exposure).
            return LiveOrderResult(
                ok=False, status="UNFILLED",
                reason="exit SELL not filled in time and cancel failed; will retry",
                order_id=result.order_id, price=price, size=0.0,
            )
        final, _ = await self._order_fill_info(result.order_id, default_size=0.0)
        self._exit_order_id = None
        self._exit_price = None
        await self._register_exit_fill(
            final,
            _avg_fill_price(result.raw, SELL, price),
            taker_size=_placement_crossed_shares(result.raw, SELL),
        )
        if matched > 0 and self._entry_sold_size >= matched:
            # The order actually filled completely during the cancel race.
            self._clear_position()
            return LiveOrderResult(
                ok=True, status="SUBMITTED", reason="filled during cancel race",
                order_id=result.order_id, price=price, size=final,
                notional_usd=round(price * final, 4),
            )
        await journal_live_order(
            intent="EXIT", side=SELL, status="UNFILLED",
            window_slug=window_slug, token_id=token,
            price=price, size=final, order_type="GTC",
            clob_order_id=result.order_id,
            error=f"exit SELL filled {final:.2f}/{sellable:.2f} before timeout-cancel",
        )
        return LiveOrderResult(
            ok=False, status="UNFILLED",
            reason=f"exit SELL filled only {final:.2f}/{sellable:.2f} shares; will retry",
            order_id=result.order_id, price=price, size=final,
        )

    async def cancel_open(
        self, reason: str = "CANCEL_REQUEST", *, assume_filled_on_error: bool = True
    ) -> list[str]:
        """Cancel tracked resting orders (entry, and any stale exit).

        The matched (filled) size of the entry is captured AFTER the cancel
        is confirmed, so a fill landing during the cancel round-trip is still
        counted and the follow-up exit flattens the right amount. The unfilled
        remainder's notional is credited back to the daily bankroll cap.
        On cancel failure the order id stays tracked so a later attempt can
        retry — the order is never silently forgotten while possibly live.

        ``assume_filled_on_error`` mirrors :meth:`_matched_entry_size`: the
        exit/flatten callers keep the optimistic default (don't strand
        tokens), but the SETTLEMENT caller passes ``False`` so a failed fill
        lookup captures 0, never a phantom held size (#109).
        """
        cancelled: list[str] = []
        # Stale exit order first (only set if an exit cancel failed earlier).
        if self._exit_order_id is not None:
            stale_id, stale_price = self._exit_order_id, self._exit_price
            if await self._try_cancel(stale_id, reason=reason):
                cancelled.append(stale_id)
                sold, order_price = await self._order_fill_info(stale_id, default_size=0.0)
                self._exit_order_id = None
                self._exit_price = None
                await self._register_exit_fill(sold, stale_price or order_price)

        order_id = self._entry_order_id
        if order_id is None:
            return cancelled
        if not await self._try_cancel(order_id, reason=reason):
            return cancelled
        cancelled.append(order_id)
        matched, _ = await self._order_fill_info(
            order_id,
            default_size=self._entry_size if assume_filled_on_error else 0.0,
        )
        self._entry_matched_size = matched
        unfilled = max(0.0, self._entry_size - matched)
        if unfilled > 0 and self._entry_price:
            # Credit back the unfilled notional so the bankroll counter only
            # reflects size that actually traded.
            await self.gate.record_buy_notional(
                -round(unfilled * self._entry_price, 4)
            )
        self._entry_order_id = None
        return cancelled

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _clear_position(self) -> None:
        self._entry_order_id = None
        self._entry_token_id = None
        self._entry_price = None
        self._entry_size = 0.0
        self._entry_matched_size = None
        self._entry_sold_size = 0.0
        self._entry_taker_fee_usd = 0.0
        self._position_open = False
        self._exit_order_id = None
        self._exit_price = None

    async def resync_flat(self) -> bool:
        """Heal stale in-memory open-state that a flat position ledger refutes.

        The live entry path calls this AFTER confirming the ledger has zero
        open rows. A live ledger row is closed only once the venue flatten is
        confirmed (see ``polymarket_bot.paper._close_position``), so a flat ledger
        guarantees no real exposure remains. Any lingering ``_position_open`` /
        ``_entry_order_id`` is therefore a phantom — left by an interrupted
        stop/restart — that would otherwise block every entry with "max 1".

        Cancel anything still tracked on the venue first (cheap insurance on the
        money path; the invariant says it should find nothing live), then clear
        the slot. Returns True when a phantom was healed.
        """
        if (
            not self._position_open
            and self._entry_order_id is None
            and self._exit_order_id is None
        ):
            return False
        had_entry_order = self._entry_order_id is not None
        if had_entry_order or self._exit_order_id is not None:
            try:
                await self.cancel_open(reason="LEDGER_FLAT_RESYNC")
            except Exception as e:  # noqa: BLE001
                log.warning("live_executor.resync_cancel_failed", error=str(e))
        log.warning(
            "live_executor.healed_phantom_position",
            had_entry_order=had_entry_order,
            position_open=self._position_open,
        )
        self._clear_position()
        return True

    async def _register_exit_fill(
        self,
        sold_size: float,
        exit_price: float | None,
        *,
        taker_size: float = 0.0,
    ) -> None:
        """Account a confirmed exit fill: track sold shares, record realized PnL.

        ``taker_size`` is the portion of the SELL that crossed at placement —
        the venue charges its taker fee on that portion's proceeds (#133); a
        fill of a resting SELL is a maker fill and fee-free, so callers that
        register fills discovered by order lookup pass no taker size. The
        ENTRY-side taker fee stays booked at settlement (settle style is the
        deployed config); a position fully flattened by exits leaves its entry
        fee to the reconcile tool.
        """
        if sold_size <= 0:
            return
        self._entry_sold_size = round(self._entry_sold_size + sold_size, SIZE_DECIMALS)
        entry_px = self._entry_price or 0.0
        px = exit_price if exit_price is not None else entry_px
        fee = (
            round(taker_fee_per_share(px) * min(taker_size, sold_size), 6)
            if taker_size > 0
            else 0.0
        )
        await self.record_realized_pnl(sold_size * (px - entry_px) - fee)

    async def _try_cancel(self, order_id: str, reason: str) -> bool:
        """Cancel one order; True only when it is confirmed no longer live.

        Inspects the DELETE response body ({"canceled": [...], "not_canceled":
        {...}}) instead of trusting a non-exception. An order reported
        not-canceled is re-checked via get_order: a terminal status (already
        matched/cancelled) also counts as "no longer live".
        """
        try:
            from py_clob_client_v2 import OrderPayload

            raw = await asyncio.to_thread(
                self._client.cancel_order, OrderPayload(orderID=order_id)
            )
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
            await journal_live_order(
                intent="CANCEL", side="-", status="ERROR",
                token_id=self._entry_token_id, clob_order_id=order_id, error=error,
                details={"reason": reason},
            )
            log.warning("live_executor.cancel_failed", order_id=order_id, error=error)
            return False
        if isinstance(raw, dict):
            canceled_ids = raw.get("canceled") or []
            if order_id not in canceled_ids:
                status = await self._order_status(order_id)
                if status not in _TERMINAL_ORDER_STATUSES:
                    error = f"cancel not confirmed (status={status or 'unknown'})"
                    await journal_live_order(
                        intent="CANCEL", side="-", status="ERROR",
                        token_id=self._entry_token_id, clob_order_id=order_id,
                        error=error, details={"reason": reason, "response": raw},
                    )
                    log.warning(
                        "live_executor.cancel_not_confirmed",
                        order_id=order_id, response=str(raw),
                    )
                    return False
        await journal_live_order(
            intent="CANCEL", side="-", status="CANCELLED",
            token_id=self._entry_token_id, clob_order_id=order_id,
            details={"reason": reason, "response": raw},
        )
        log.info("live_executor.order_cancelled", order_id=order_id, reason=reason)
        return True

    async def _order_status(self, order_id: str) -> str:
        try:
            raw = await asyncio.to_thread(self._client.get_order, order_id)
            return str(raw.get("status") or "").lower()
        except Exception:  # noqa: BLE001
            return ""

    async def _order_fill_info(
        self, order_id: str, default_size: float
    ) -> tuple[float, float | None]:
        """(size_matched, limit_price) for an order; default on lookup failure."""
        try:
            raw = await asyncio.to_thread(self._client.get_order, order_id)
        except Exception as e:  # noqa: BLE001
            log.warning("live_executor.get_order_failed", error=str(e))
            return default_size, None
        try:
            matched = float(raw.get("size_matched") or 0.0)
        except (AttributeError, TypeError, ValueError):
            return default_size, None
        try:
            price = float(raw.get("price")) if raw.get("price") else None
        except (TypeError, ValueError):
            price = None
        return matched, price

    async def _matched_entry_size(self, *, assume_filled_on_error: bool = True) -> float:
        """Matched (filled) share size of the tracked entry order.

        ``assume_filled_on_error`` picks the fallback when the venue fill
        lookup fails. The EXIT/SELL path passes ``True`` (the default): an
        oversized SELL is rejected by the exchange and retried, while
        underselling strands real tokens, so assuming fully filled is safe.
        The SETTLE path passes ``False``: there is no SELL, so over-counting
        a never-matched entry books a phantom win/loss into the ledger (#103,
        #109). Conservative 0 is correct there — an under-counted winner is
        just a token the operator redeems later, never a fictional PnL.
        """
        if self._entry_order_id is None:
            return self._entry_matched_size or 0.0
        matched, _ = await self._order_fill_info(
            self._entry_order_id,
            default_size=self._entry_size if assume_filled_on_error else 0.0,
        )
        return matched

    async def _await_fill(self, order_id: str | None, target_size: float) -> float:
        """Poll an order's matched size until *target_size* or timeout."""
        if order_id is None:
            return 0.0
        deadline = time.monotonic() + max(0.0, self.exit_fill_timeout_seconds)
        while True:
            matched, _ = await self._order_fill_info(order_id, default_size=0.0)
            if matched >= target_size:
                return matched
            now = time.monotonic()
            if now >= deadline:
                return matched
            await asyncio.sleep(min(0.5, deadline - now))

    async def _place_order(
        self,
        intent: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        window_slug: str | None,
    ) -> LiveOrderResult:
        from py_clob_client_v2 import OrderArgs

        notional = round(price * size, 4)
        # Last-instant kill re-check for ENTRIES: the gate ran before the
        # order-book round-trip, and a kill file appearing in that window must
        # still stop the buy. Exits stay allowed (they reduce exposure).
        if side == BUY and self.kill_switch_active():
            reason = f"KILL switch appeared before posting at {self.kill_switch_path}"
            await self._journal_blocked(
                intent=intent, side=side, reason=reason,
                window_slug=window_slug, token_id=token_id,
                price=price, size=size, notional_usd=notional,
            )
            return LiveOrderResult(
                ok=False, status="BLOCKED", reason=reason,
                price=price, size=size, notional_usd=notional,
            )
        args = OrderArgs(token_id=token_id, price=price, size=size, side=side)
        try:
            raw = await asyncio.to_thread(self._client.create_and_post_order, args)
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
            await journal_live_order(
                intent=intent, side=side, status="ERROR",
                window_slug=window_slug, token_id=token_id,
                price=price, size=size, notional_usd=notional,
                order_type="GTC", error=error,
            )
            log.warning("live_executor.order_failed", intent=intent, error=error)
            return LiveOrderResult(
                ok=False, status="ERROR", reason=error,
                price=price, size=size, notional_usd=notional,
            )

        response = raw if isinstance(raw, dict) else {}
        order_id = response.get("orderID") or response.get("orderId")
        success = bool(response.get("success", order_id is not None))
        status = "SUBMITTED" if success else "ERROR"
        error = None if success else str(response.get("errorMsg") or response)
        await journal_live_order(
            intent=intent, side=side, status=status,
            window_slug=window_slug, token_id=token_id,
            price=price, size=size, notional_usd=notional,
            order_type="GTC", clob_order_id=order_id, error=error,
            details={"response": response},
        )
        log.info(
            "live_executor.order_submitted" if success else "live_executor.order_rejected",
            intent=intent, side=side, price=price, size=size,
            notional=notional, order_id=order_id,
        )
        return LiveOrderResult(
            ok=success, status=status, reason=error or "",
            order_id=order_id, price=price, size=size,
            notional_usd=notional, raw=response,
        )

    async def _journal_blocked(
        self,
        intent: str,
        side: str,
        reason: str,
        window_slug: str | None = None,
        token_id: str | None = None,
        price: float | None = None,
        size: float | None = None,
        notional_usd: float | None = None,
        mode: str = "live",
    ) -> None:
        await journal_live_order(
            intent=intent, side=side, status="BLOCKED",
            window_slug=window_slug, token_id=token_id,
            price=price, size=size, notional_usd=notional_usd, error=reason,
            mode=mode,
        )
        log.warning("live_executor.order_blocked", intent=intent, reason=reason, mode=mode)


def build_live_executor() -> LiveExecutor:
    """Build a LiveExecutor from config after passing the boot gate."""
    assert_live_boot_allowed()
    return LiveExecutor(
        private_key=_config.POLYMARKET_PRIVATE_KEY,
        funder=_config.POLYMARKET_FUNDER,
        signature_type=_config.POLYMARKET_SIGNATURE_TYPE,
    )
