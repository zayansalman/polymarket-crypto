"""Persistence for the daily altcoin scanner's shadow positions.

Mirrors :mod:`polymarket_bot.shadow.ledger` (same ``INSERT OR IGNORE`` +
settle-by-outcome shape, same after-fee PnL via
:mod:`polymarket_bot.shadow.fees`), but keyed on ``window_slug`` alone: this
market family's slug is already asset+day-specific (e.g.
``solana-up-or-down-on-august-30-2026``), so — unlike the 5m shadow ledger,
which needs ``(window_slug, model_id)`` because several competing models
share one window — a single scan decision per window is naturally
idempotent on the slug by itself.
"""
from __future__ import annotations

import db as _db
from polymarket_bot.shadow.fees import net_pnl_per_share


async def record_signal(
    *,
    created_at: str,
    window_slug: str,
    asset: str,
    side: str,
    entry_price: float,
    fair_prob: float,
    edge: float,
    confidence: float,
    reason: str,
    notional_usd: float,
    shares: float,
    reference_price: float,
    resolves_at: str,
    binance_symbol: str,
    sigma_per_second: float | None = None,
    drift_per_second: float | None = None,
) -> bool:
    """Log one asset's would-be trade for a day-window as an OPEN position.

    Returns ``True`` iff this call actually inserted a new row (``False``
    when the window already had one — the caller uses this to log "entry"
    only for a genuinely new position, not on every idempotent re-scan).

    ``INSERT OR IGNORE`` against the unique ``window_slug`` index — a
    restart or a re-run within the same scan tick never double-opens.
    ``reference_price``/``resolves_at``/``binance_symbol`` are stamped here
    so :func:`polymarket_bot.daily.scanner._settle_due` can settle this row
    later without depending on Polymarket's own market-discovery/resolution
    API still listing the (by-then-resolved) market.
    """
    async with _db.connect() as conn:
        await conn.execute(
            """
            INSERT OR IGNORE INTO daily_shadow_positions(
              created_at, window_slug, asset, side, entry_price,
              notional_usd, shares, fair_prob, edge, confidence, reason,
              state, sigma_per_second, drift_per_second,
              reference_price, resolves_at, binance_symbol
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                window_slug,
                asset,
                side,
                entry_price,
                notional_usd,
                shares,
                fair_prob,
                edge,
                confidence,
                reason,
                sigma_per_second,
                drift_per_second,
                reference_price,
                resolves_at,
                binance_symbol,
            ),
        )
        inserted = conn.total_changes > 0
        await conn.commit()
    return inserted


async def settle(
    *,
    window_slug: str,
    outcome_side: str | None,
    settlement_price: float,
    resolved_at: str,
    fee_rate: float = 0.07,
) -> int:
    """Settle the OPEN position for ``window_slug``, if any; return 0 or 1.

    ``outcome_side`` is ``None`` for an exact tie: this family's own rules
    (confirmed via the market's Gamma ``description``) resolve an exact tie
    50-50 rather than crediting Up, unlike the BTC 5m family. A 50-50
    resolution pays every share $0.50 regardless of side, so PnL is
    ``0.50 - entry_price - fee`` rather than the binary win/loss formula.
    """
    async with _db.connect() as conn:
        async with conn.execute(
            """
            SELECT id, side, entry_price, shares
              FROM daily_shadow_positions
             WHERE window_slug = ? AND state = 'open'
            """,
            (window_slug,),
        ) as cur:
            rows = list(await cur.fetchall())
        for row in rows:
            if outcome_side is None:
                fee = row["entry_price"] * (1.0 - row["entry_price"]) * fee_rate
                net = row["shares"] * (0.50 - row["entry_price"] - fee)
            else:
                won = row["side"] == outcome_side
                net = row["shares"] * net_pnl_per_share(row["entry_price"], won, fee_rate)
            await conn.execute(
                """
                UPDATE daily_shadow_positions
                   SET state = 'settled',
                       outcome = ?,
                       settlement_price = ?,
                       resolved_at = ?,
                       realized_pnl_usd = ?
                 WHERE id = ?
                """,
                (outcome_side or "tie", settlement_price, resolved_at, net, row["id"]),
            )
        await conn.commit()
    return len(rows)


async def open_settlement_candidates() -> list[dict]:
    """Every OPEN position's settlement inputs: ``window_slug``,
    ``reference_price``, ``resolves_at``, ``binance_symbol``.

    Self-contained on purpose — see :func:`record_signal` — so the caller
    never needs to re-resolve the market through Polymarket to settle it.
    """
    async with _db.connect() as conn:
        async with conn.execute(
            "SELECT window_slug, reference_price, resolves_at, binance_symbol "
            "FROM daily_shadow_positions WHERE state = 'open'"
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]
