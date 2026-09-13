"""Persistence for the shadow forward-tester's would-be trades.

Each tick, every candidate strategy logs the trade it *would* have taken to
``model_shadow_positions`` via :func:`record_shadow_signal`. No real order
is ever placed. When a window resolves, :func:`settle_open_shadow` marks every
open row for that window settled and stamps the realized PnL net of the
Polymarket taker fee, so candidates can be compared on the same after-fee basis
as the live book.

The unique ``(window_slug, model_id)`` index makes recording idempotent: a
model that fires repeatedly within one window keeps only its first logged trade,
so re-running a tick (or a crash-replay) never double-counts.
"""
from __future__ import annotations

import db as _db
from polymarket_bot.shadow.fees import net_pnl_per_share


async def record_shadow_signal(
    *,
    created_at: str,
    window_slug: str,
    model_id: str,
    side: str,
    entry_price: float,
    fair_prob: float,
    edge: float,
    confidence: float,
    reason: str,
    notional_usd: float,
    shares: float,
    quote_source: str,
    feed_source: str,
    spot_at_decision: float | None = None,
    reference_at_decision: float | None = None,
    sigma_per_second: float | None = None,
    drift_per_second: float | None = None,
) -> None:
    """Log one model's would-be trade for a window as an OPEN shadow position.

    Uses ``INSERT OR IGNORE`` against the unique ``(window_slug, model_id)``
    index, so the first signal a model emits for a window wins and later signals
    in the same window are silently dropped — recording is idempotent per
    (window, model).

    ``spot_at_decision`` / ``reference_at_decision`` / ``sigma_per_second`` /
    ``drift_per_second`` snapshot the market state at record time (issue #122)
    so volatility and basis become a-priori regime axes; they default to NULL
    for callers that do not supply them.
    """
    async with _db.connect() as conn:
        await conn.execute(
            """
            INSERT OR IGNORE INTO model_shadow_positions(
              created_at, window_slug, model_id, side, entry_price,
              notional_usd, shares, fair_prob, edge, confidence, reason,
              state, quote_source, feed_source,
              spot_at_decision, reference_at_decision, sigma_per_second,
              drift_per_second
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                window_slug,
                model_id,
                side,
                entry_price,
                notional_usd,
                shares,
                fair_prob,
                edge,
                confidence,
                reason,
                quote_source,
                feed_source,
                spot_at_decision,
                reference_at_decision,
                sigma_per_second,
                drift_per_second,
            ),
        )
        await conn.commit()


async def settle_open_shadow(
    *,
    window_slug: str,
    outcome_side: str,
    settlement_price: float,
    resolved_at: str,
    fee_rate: float = 0.07,
) -> int:
    """Settle every OPEN shadow position for ``window_slug``; return the count.

    A row wins iff its ``side`` matches ``outcome_side``. Realized PnL is
    ``shares * net_pnl_per_share(entry_price, won, fee_rate)`` — the after-fee
    per-share PnL scaled by the shares the model would have held. Each settled
    row is stamped with the outcome, settlement price, resolution time, and its
    net PnL, and flipped to state ``'settled'``.
    """
    async with _db.connect() as conn:
        async with conn.execute(
            """
            SELECT id, side, entry_price, shares
              FROM model_shadow_positions
             WHERE window_slug = ? AND state = 'open'
            """,
            (window_slug,),
        ) as cur:
            rows = list(await cur.fetchall())

        for row in rows:
            won = row["side"] == outcome_side
            net = row["shares"] * net_pnl_per_share(
                row["entry_price"], won, fee_rate
            )
            await conn.execute(
                """
                UPDATE model_shadow_positions
                   SET state = 'settled',
                       outcome = ?,
                       settlement_price = ?,
                       resolved_at = ?,
                       realized_pnl_usd = ?
                 WHERE id = ?
                """,
                (outcome_side, settlement_price, resolved_at, net, row["id"]),
            )
        await conn.commit()
    return len(rows)
