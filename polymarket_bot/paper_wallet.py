"""Persistent simulated cash ledger for BTC paper trades.

Fees (2026-11-18, Luke: "include the fees in the profit and losses"): every
taker fill pays Polymarket's fee, ``0.07 * p * (1 - p)`` dollars a share
(``shadow/fees.py``, the same math the live executor books, #133). A row's
``amount_usd`` stays the share value (BUY: -cost, SELL: +proceeds) and
``fee_usd`` is the fee on that fill, so cash moves by ``amount_usd -
fee_usd``. Trades booked before fees were charged had their fees filled in
and charged once by :func:`book_past_fees` (config ``paper.fees_from``).
"""
from __future__ import annotations

import bisect
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import aiosqlite

from db import connect, utc_now_iso
from polymarket_bot.shadow.fees import taker_fee_per_share

TransactionType = Literal["BUY", "SELL", "SETTLEMENT"]

# When fee booking started: a ``FeeCatchUp`` dict. Present = past fees charged.
FEES_KEY = "paper.fees_from"


def taker_fee(price: Any, shares: Any) -> float:
    """Venue taker fee in dollars for ``shares`` filled at ``price`` (4 dp)."""
    try:
        p, n = float(price), float(shares)
    except (TypeError, ValueError):
        return 0.0
    if not (0.0 < p < 1.0) or not math.isfinite(n) or n <= 0:
        return 0.0
    return round(taker_fee_per_share(p) * n, 4)

# Starting-balance switches (2026-11-18: Luke moved the practice wallet from
# $100 to $500). A switch multiplies the balance by new start / old start, so
# the percentage gain or loss carries over; the screen multiplies every
# earlier result by the same factor. JSON list of ``Rescale`` dicts.
RESCALE_KEY = "paper.wallet_rescales"
MIN_START_USD = 10.0
MAX_START_USD = 100_000.0
MAX_BACKDATE = timedelta(hours=24)

# Share cost of wallet-funded BUYs that have no exit yet (the open trades). Their
# entry fees were already paid from cash, so cash + this is the after-fee balance.
_OPEN_COST_SQL = """
SELECT COALESCE(SUM(-b.amount_usd), 0) AS cost
  FROM paper_wallet_transactions b
 WHERE b.transaction_type = 'BUY'
   AND NOT EXISTS (
     SELECT 1 FROM paper_wallet_transactions x
      WHERE x.position_id = b.position_id
        AND x.transaction_type IN ('SELL', 'SETTLEMENT'))
"""

# The same, as of wallet transaction ``id``: BUYs up to it without an exit up to it.
_OPEN_COST_AT_SQL = """
SELECT COALESCE(SUM(-b.amount_usd), 0) AS cost
  FROM paper_wallet_transactions b
 WHERE b.transaction_type = 'BUY' AND b.id <= ?
   AND NOT EXISTS (
     SELECT 1 FROM paper_wallet_transactions x
      WHERE x.position_id = b.position_id AND x.id <= ?
        AND x.transaction_type IN ('SELL', 'SETTLEMENT'))
"""


@dataclass(frozen=True)
class Rescale:
    """One starting-balance switch.

    ``at`` is when the switch counts from (when it was asked for); the wallet
    itself is changed at ``applied_at``. Wallet transactions up to
    ``last_txn_id`` happened before ``at``: their amounts are on the old scale
    and count ``factor`` times. Transactions after it count at their real size;
    the ones up to ``applied_txn_id`` were booked before the wallet changed, so
    their balances are ``balance_after_usd - balance_before_usd`` short of
    today's scale. Ids instead of timestamps keep a same-second exit on the
    right side of the switch.
    """

    at: str
    factor: float
    from_start_usd: float
    to_start_usd: float
    balance_before_usd: float  # balance at ``at``: cash + any open trade at cost
    balance_after_usd: float  # balance_before_usd * factor
    cash_before_usd: float  # cash just before the wallet changed
    cash_after_usd: float
    open_cost_usd: float  # a trade open at ``at`` keeps its real size (counts 1x)
    last_txn_id: int
    applied_at: str = ""
    applied_txn_id: int = -1  # -1: applied with no later transactions

    @property
    def change_pct(self) -> float:
        """The percentage gain or loss that the switch carried over."""
        return self.balance_before_usd / self.from_start_usd - 1.0

    @property
    def shift_usd(self) -> float:
        return self.balance_after_usd - self.balance_before_usd


def load_rescales(raw: str | None) -> list[Rescale]:
    """Parse the stored switches, skipping any entry that can't be read."""
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return []
    names = Rescale.__dataclass_fields__.keys()
    out: list[Rescale] = []
    for item in items if isinstance(items, list) else []:
        try:
            r = Rescale(**{k: item[k] for k in names if k in item})
        except TypeError:
            continue
        if r.factor > 0 and math.isfinite(r.factor):
            out.append(r)
    return out


def _txn_id(txn_id: Any) -> int | None:
    try:
        return int(txn_id)
    except (TypeError, ValueError):
        return None


def scale_for_txn(txn_id: Any, rescales: list[Rescale]) -> float:
    """Factor that puts a result (profit, fee) from wallet transaction
    ``txn_id`` on today's scale: earlier switches multiply it."""
    tid = _txn_id(txn_id)
    if tid is None:
        return 1.0
    factor = 1.0
    for r in rescales:
        if tid <= r.last_txn_id:
            factor *= r.factor
    return factor


def balance_on_today_scale(value: float, txn_id: Any, rescales: list[Rescale]) -> float:
    """A balance booked with wallet transaction ``txn_id``, on today's scale.

    Before a switch it is multiplied; booked after the switch counts from but
    before the wallet changed, it is shifted up by the switch's amount.
    """
    tid = _txn_id(txn_id)
    if tid is None:
        return value
    for r in rescales:
        applied = r.applied_txn_id if r.applied_txn_id >= 0 else r.last_txn_id
        if tid <= r.last_txn_id:
            value *= r.factor
        elif tid <= applied:
            value += r.shift_usd
    return value


@dataclass(frozen=True)
class FeeCatchUp:
    """The one-time charge for trades booked before the wallet paid fees.

    Wallet transactions up to ``last_txn_id`` were booked without a fee; their
    ``fee_usd`` was filled in with what Polymarket would have charged, and the
    wallet paid the total (on that day's balance scale) at ``at``. Balances
    recorded before then are shown minus the fees paid up to that point.
    """

    at: str
    last_txn_id: int
    amount_usd: float
    fills: int
    trades: int
    cash_before_usd: float
    cash_after_usd: float


def load_fee_catch_up(raw: str | None) -> FeeCatchUp | None:
    if not raw:
        return None
    try:
        item = json.loads(raw)
        return FeeCatchUp(**{k: item[k] for k in FeeCatchUp.__dataclass_fields__})
    except (TypeError, ValueError, KeyError):
        return None


class FeeShift:
    """Fees already paid by each point of the pre-fee history, on today's scale.

    ``events`` are ``(wallet txn id, fee on today's scale)`` for the fills the
    catch-up covered. A balance booked at transaction ``id`` up to the catch-up
    did not include those fees yet, so it is shown minus the ones paid by then.
    Balances booked after the catch-up already include them.
    """

    def __init__(self, catch_up: FeeCatchUp | None, events: list[tuple[int, float]]) -> None:
        self.last_txn_id = catch_up.last_txn_id if catch_up else -1
        pts = sorted((int(i), float(f)) for i, f in events if int(i) <= self.last_txn_id)
        self._ids = [i for i, _ in pts]
        self._sums: list[float] = []
        run = 0.0
        for _, fee in pts:
            run += fee
            self._sums.append(run)

    def at(self, txn_id: Any) -> float:
        tid = _txn_id(txn_id)
        if tid is None or tid > self.last_txn_id or not self._ids:
            return 0.0
        k = bisect.bisect_right(self._ids, tid)
        return self._sums[k - 1] if k else 0.0


async def book_past_fees() -> FeeCatchUp | None:
    """Charge, once, the fees the trades before fee booking would have paid.

    Fills every unbooked BUY/SELL fee (``fee_usd = 0``) with the venue fee for
    its price and size, restates each such paper position's ``fees_usd`` and
    realized PnL after fees, and debits the wallet the total on today's scale
    (trades from before a practice balance switch count its factor, like
    every other result). Idempotent: does nothing once ``paper.fees_from``
    exists, and records it even when there was nothing to charge so later
    trades are never charged twice. Returns the charge, or None when it had
    already run or no wallet exists yet.
    """
    now = datetime.now(UTC).replace(microsecond=0).isoformat(timespec="seconds")
    async with connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "SELECT value FROM config WHERE key = ?", (FEES_KEY,)
            ) as cur:
                if await cur.fetchone() is not None:
                    await db.rollback()
                    return None
            async with db.execute(
                "SELECT cash_usd FROM paper_wallet WHERE wallet_id = 1"
            ) as cur:
                wallet = await cur.fetchone()
            if wallet is None:
                await db.rollback()
                return None
            cash = float(wallet["cash_usd"])
            async with db.execute(
                "SELECT value FROM config WHERE key = ?", (RESCALE_KEY,)
            ) as cur:
                row = await cur.fetchone()
            rescales = load_rescales(row["value"] if row else None)
            async with db.execute(
                "SELECT COALESCE(MAX(id), 0) AS id FROM paper_wallet_transactions"
            ) as cur:
                max_id = int((await cur.fetchone())["id"])
            async with db.execute(
                "SELECT position_id, id FROM paper_wallet_transactions "
                "WHERE transaction_type IN ('SELL', 'SETTLEMENT')"
            ) as cur:
                exit_ids = {int(r["position_id"]): int(r["id"]) for r in await cur.fetchall()}
            async with db.execute(
                """
                SELECT t.id, t.position_id, t.transaction_type, p.shares,
                       p.entry_price, p.exit_price
                  FROM paper_wallet_transactions t
                  JOIN paper_positions p ON p.position_id = t.position_id
                 WHERE t.transaction_type IN ('BUY', 'SELL') AND t.fee_usd = 0
                """
            ) as cur:
                fills = [dict(r) for r in await cur.fetchall()]
            total = 0.0
            charged = 0
            positions: set[int] = set()
            for f in fills:
                price = f["entry_price"] if f["transaction_type"] == "BUY" else f["exit_price"]
                fee = taker_fee(price, f["shares"])
                if fee <= 0:
                    continue
                await db.execute(
                    "UPDATE paper_wallet_transactions SET fee_usd = ? WHERE id = ?",
                    (fee, f["id"]),
                )
                total += fee * scale_for_txn(exit_ids.get(int(f["position_id"])), rescales)
                charged += 1
                positions.add(int(f["position_id"]))
            # The ledger's own PnL column, after fees too (paper rows only: live
            # rows already carry the executor's fee-true PnL). Gross stays
            # recoverable as realized_pnl_usd + fees_usd.
            async with db.execute(
                "SELECT position_id, state, shares, entry_price, exit_price "
                "FROM paper_positions WHERE COALESCE(mode, 'paper') = 'paper' "
                "AND fees_usd IS NULL"
            ) as cur:
                restate = [dict(r) for r in await cur.fetchall()]
            for p in restate:
                fee = taker_fee(p["entry_price"], p["shares"])
                exit_price = p["exit_price"]
                if p["state"] == "closed" and exit_price is not None and 0.0 < float(exit_price) < 1.0:
                    fee += taker_fee(exit_price, p["shares"])  # sold into the bid
                fee = round(fee, 4)
                await db.execute(
                    "UPDATE paper_positions SET fees_usd = ?, realized_pnl_usd = "
                    "CASE WHEN realized_pnl_usd IS NULL THEN NULL "
                    "ELSE realized_pnl_usd - ? END WHERE position_id = ?",
                    (fee, fee, p["position_id"]),
                )
            total = round(total, 4)
            new_cash = round(cash - total, 4)
            cur = await db.execute(
                "UPDATE paper_wallet SET cash_usd = ? WHERE wallet_id = 1 AND cash_usd = ?",
                (new_cash, wallet["cash_usd"]),
            )
            if cur.rowcount != 1:
                raise RuntimeError("the practice balance changed while fees were charged")
            event = FeeCatchUp(
                at=now, last_txn_id=max_id, amount_usd=total, fills=charged,
                trades=len(positions), cash_before_usd=round(cash, 4), cash_after_usd=new_cash,
            )
            await db.execute(
                """
                INSERT INTO config(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  value = excluded.value, updated_at = excluded.updated_at
                """,
                (FEES_KEY, json.dumps(asdict(event)), now),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return event


def describe_fee_catch_up(e: FeeCatchUp) -> str:
    """Recent-activity line for the one-time fee charge, in plain English."""
    if e.trades == 0:
        return "Trading fees are now part of every practice trade."
    return (
        "Trading fees are now part of every practice trade and every profit and loss. "
        f"The {e.trades} earlier trades were charged the fees they would have paid, "
        f"${e.amount_usd:,.2f} on today's balance: ${e.cash_before_usd:,.2f} in cash became "
        f"${e.cash_after_usd:,.2f}."
    )


def _local_clock(iso: str) -> str:
    """"8:12 PM" in this computer's time zone (the PC runs on Luke's local time)."""
    try:
        t = datetime.fromisoformat(iso).astimezone()
    except (TypeError, ValueError):
        return iso
    return t.strftime("%I:%M %p").lstrip("0")


def describe_rescale(r: Rescale) -> str:
    """Recent-activity line for a practice balance switch, in plain English."""
    pct = r.change_pct * 100
    if abs(pct) < 0.005:
        kept = "no gain or loss yet"
    else:
        sign, word = ("+", "gain") if pct > 0 else ("\u2212", "loss")
        kept = f"the same {sign}{abs(pct):.2f}% {word}"
    backdated = bool(r.applied_at) and r.applied_txn_id > r.last_txn_id
    when = f"Counted from {_local_clock(r.at)}, when it was asked for: " if backdated else ""
    tail = " Trades since then count at their real size." if backdated else ""
    return (
        f"Practice balance switched from ${r.from_start_usd:,.2f} to ${r.to_start_usd:,.2f}. "
        f"{when}${r.balance_before_usd:,.2f} became ${r.balance_after_usd:,.2f}, keeping {kept}. "
        f"Earlier results now count {r.factor:g}\u00d7.{tail}"
    )


def _as_of(value: Any, now: datetime, earliest: datetime | None) -> datetime:
    """Validate a backdated switch time: within the last day, not in the future,
    and not before the previous switch was applied."""
    try:
        t = datetime.fromisoformat(str(value))
    except ValueError:
        raise ValueError("The switch time must be an ISO date and time.") from None
    if t.tzinfo is None:
        raise ValueError("The switch time needs a time zone.")
    t = t.astimezone(UTC).replace(microsecond=0)
    if t > now:
        raise ValueError("The switch time can't be in the future.")
    if now - t > MAX_BACKDATE:
        raise ValueError("The switch can count from at most 24 hours ago.")
    if earliest is not None and t < earliest:
        raise ValueError("The switch can't count from before the previous switch.")
    return t


async def rescale(start_usd: Any, *, as_of: Any = None) -> Rescale:
    """Change the practice wallet's starting balance, keeping its % gain or loss.

    Only on an explicit request from the operator, and recorded: the wallet is
    still created once and nothing tops it up on its own. The balance at the
    switch (cash plus any open trade at cost) is multiplied by
    ``start_usd / old start``; an open trade keeps its real size. ``as_of``
    (ISO time, at most a day back) makes the switch count from when it was
    asked for, so trades placed since then count at their real size instead
    of being multiplied. Raises ``ValueError`` with a screen-safe message.
    """
    try:
        target = round(float(start_usd), 2)
    except (TypeError, ValueError):
        raise ValueError("The practice balance must be a dollar amount.") from None
    if not math.isfinite(target) or not MIN_START_USD <= target <= MAX_START_USD:
        raise ValueError(
            f"The practice balance must be between ${MIN_START_USD:,.0f} and "
            f"${MAX_START_USD:,.0f}."
        )
    now = datetime.now(UTC).replace(microsecond=0)
    async with connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "SELECT initial_cash_usd, cash_usd FROM paper_wallet WHERE wallet_id = 1"
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise RuntimeError("paper wallet is not initialized; initialize the database first")
            old_start = float(row["initial_cash_usd"])
            cash = float(row["cash_usd"])
            if old_start <= 0:
                raise ValueError("The practice wallet has no starting balance to scale.")
            if abs(target - old_start) < 0.005:
                raise ValueError(f"The practice balance already starts at ${old_start:,.2f}.")
            async with db.execute(
                "SELECT value FROM config WHERE key = ?", (RESCALE_KEY,)
            ) as cur:
                prior_row = await cur.fetchone()
            prior = load_rescales(prior_row["value"] if prior_row else None)
            async with db.execute(
                "SELECT COALESCE(MAX(id), 0) AS id FROM paper_wallet_transactions"
            ) as cur:
                max_id = int((await cur.fetchone())["id"])

            if as_of is None:
                at, boundary = now, max_id
                async with db.execute(_OPEN_COST_SQL) as cur:
                    open_cost = round(float((await cur.fetchone())["cost"] or 0.0), 4)
                before = round(cash + open_cost, 4)
            else:
                earliest = None
                if prior:
                    earliest = datetime.fromisoformat(prior[-1].applied_at or prior[-1].at)
                at = _as_of(as_of, now, earliest)
                # Balances booked before fees were charged don't include them;
                # a switch counted from back then would scale a fee-free balance.
                async with db.execute(
                    "SELECT value FROM config WHERE key = ?", (FEES_KEY,)
                ) as cur:
                    fee_row = await cur.fetchone()
                fees_from = load_fee_catch_up(fee_row["value"] if fee_row else None)
                if fees_from is not None and at < datetime.fromisoformat(fees_from.at):
                    raise ValueError(
                        "The switch can't count from before trading fees were added "
                        f"at {_local_clock(fees_from.at)}."
                    )
                async with db.execute(
                    "SELECT id, cash_after_usd FROM paper_wallet_transactions "
                    "WHERE created_at <= ? ORDER BY id DESC LIMIT 1",
                    (at.isoformat(timespec="seconds"),),
                ) as cur:
                    last = await cur.fetchone()
                if last is None:
                    boundary, cash_then = 0, old_start
                else:
                    boundary = int(last["id"])
                    cash_then = balance_on_today_scale(
                        float(last["cash_after_usd"]), boundary, prior
                    )
                async with db.execute(_OPEN_COST_AT_SQL, (boundary, boundary)) as cur:
                    open_cost = round(float((await cur.fetchone())["cost"] or 0.0), 4)
                before = round(cash_then + open_cost, 4)

            factor = target / old_start
            after = round(before * factor, 4)
            new_cash = round(cash + after - before, 4)
            if new_cash < 0:
                raise ValueError("The new balance would leave less than nothing in cash.")
            event = Rescale(
                at=at.isoformat(timespec="seconds"), factor=factor, from_start_usd=old_start,
                to_start_usd=target, balance_before_usd=before, balance_after_usd=after,
                cash_before_usd=cash, cash_after_usd=new_cash, open_cost_usd=open_cost,
                last_txn_id=boundary, applied_at=now.isoformat(timespec="seconds"),
                applied_txn_id=max_id,
            )
            cur = await db.execute(
                "UPDATE paper_wallet SET initial_cash_usd = ?, cash_usd = ? "
                "WHERE wallet_id = 1 AND cash_usd = ?",
                (target, new_cash, row["cash_usd"]),
            )
            if cur.rowcount != 1:
                raise RuntimeError("the practice balance changed during the switch; try again")
            await db.execute(
                """
                INSERT INTO config(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  value = excluded.value, updated_at = excluded.updated_at
                """,
                (RESCALE_KEY, json.dumps([asdict(r) for r in prior + [event]]),
                 event.applied_at),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return event


async def balance_at_cost() -> float:
    """Cash plus any open trade at what it cost (the Strategy Lab's balance)."""
    async with connect() as db:
        async with db.execute(
            "SELECT cash_usd FROM paper_wallet WHERE wallet_id = 1"
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise RuntimeError("paper wallet is not initialized; initialize the database first")
        async with db.execute(_OPEN_COST_SQL) as cur:
            open_cost = float((await cur.fetchone())["cost"] or 0.0)
    return round(float(row["cash_usd"]) + open_cost, 4)


async def apply_cash_flow(
    db: aiosqlite.Connection,
    *,
    position_id: int,
    window_slug: str,
    transaction_type: TransactionType,
    amount_usd: float,
    fee_usd: float = 0.0,
    details: dict[str, object] | None = None,
) -> float | None:
    """Apply one cash movement inside the caller's SQLite transaction.

    BUY amounts are debited; SELL and SETTLEMENT amounts are credited. ``fee_usd``
    is the taker fee on the fill: a BUY debits ``amount + fee`` (and needs that
    much cash), a SELL credits ``amount - fee``, a SETTLEMENT pays no fee.
    ``None`` means the wallet is missing, cash is insufficient, or the position
    has no wallet-funded BUY.
    """
    amount = round(float(amount_usd), 4)
    fee = round(float(fee_usd or 0.0), 4)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("paper wallet cash flow must be a finite non-negative amount")
    if not math.isfinite(fee) or fee < 0:
        raise ValueError("paper wallet fee must be a finite non-negative amount")
    if transaction_type == "BUY" and amount <= 0:
        raise ValueError("paper wallet BUY debit must be greater than zero")
    if transaction_type not in {"BUY", "SELL", "SETTLEMENT"}:
        raise ValueError(f"unsupported paper wallet transaction: {transaction_type}")
    if transaction_type == "SETTLEMENT" and fee:
        raise ValueError("a settlement pays no trading fee")
    if transaction_type == "SELL" and fee > amount:
        raise ValueError("a sale's fee can't exceed its proceeds")

    if transaction_type == "BUY":
        debit = round(amount + fee, 4)
        cursor = await db.execute(
            """
            UPDATE paper_wallet
               SET cash_usd = ROUND(cash_usd - ?, 4)
             WHERE wallet_id = 1 AND ROUND(cash_usd, 4) >= ?
            """,
            (debit, debit),
        )
    else:
        async with db.execute(
            """
            SELECT 1 FROM paper_wallet_transactions
             WHERE position_id = ? AND transaction_type = 'BUY'
            """,
            (position_id,),
        ) as cursor:
            if await cursor.fetchone() is None:
                return None
        cursor = await db.execute(
            """
            UPDATE paper_wallet
               SET cash_usd = ROUND(cash_usd + ?, 4)
             WHERE wallet_id = 1
            """,
            (round(amount - fee, 4),),
        )

    if cursor.rowcount != 1:
        return None
    async with db.execute(
        "SELECT cash_usd FROM paper_wallet WHERE wallet_id = 1"
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    cash_after = float(row["cash_usd"])
    await db.execute(
        """
        INSERT INTO paper_wallet_transactions(
          created_at, position_id, window_slug, transaction_type,
          amount_usd, fee_usd, cash_after_usd, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now_iso(),
            position_id,
            window_slug,
            transaction_type,
            -amount if transaction_type == "BUY" else amount,
            fee,
            cash_after,
            json.dumps(details or {}, sort_keys=True),
        ),
    )
    return cash_after


async def has_entry_transaction(
    db: aiosqlite.Connection, position_id: int
) -> bool:
    async with db.execute(
        """
        SELECT 1 FROM paper_wallet_transactions
         WHERE position_id = ? AND transaction_type = 'BUY'
        """,
        (position_id,),
    ) as cursor:
        return await cursor.fetchone() is not None


async def cash_balance() -> float:
    async with connect() as db:
        async with db.execute(
            "SELECT cash_usd FROM paper_wallet WHERE wallet_id = 1"
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("paper wallet is not initialized; initialize the database first")
    return float(row["cash_usd"])
