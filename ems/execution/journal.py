"""The live venue's journal: one ``live_orders`` row for every live placement and cancel.

Every attempt is written, the refused and failed ones included, so the record of what was
sent to the exchange is complete. The strategy that sent it is in ``details_json``. Error text
and details are scrubbed of secrets before they are stored (``logging_setup.redact_secrets``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ems import db as _db  # type: ignore[import-untyped]
from ems.logging_setup import redact_secrets

# intent
ENTRY = "ENTRY"
CANCEL = "CANCEL"
# status
SUBMITTED = "SUBMITTED"
REJECTED = "REJECTED"
ERROR = "ERROR"
BLOCKED = "BLOCKED"
CANCELLED = "CANCELLED"


async def journal_live_order(
    *,
    intent: str,
    side: str,
    status: str,
    window_slug: str | None = None,
    token_id: str | None = None,
    price: float | None = None,
    size: float | None = None,
    order_type: str | None = None,
    clob_order_id: str | None = None,
    error: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    """Write one row. ``details`` is stored as JSON; ``response.status`` becomes
    ``placement_status`` when the venue gave one ("live" means it rests)."""
    details = dict(details or {})
    response = details.get("response")
    placement = (response.get("status") if isinstance(response, Mapping)
                 and isinstance(response.get("status"), str) and response.get("status")
                 else None)
    notional = round(price * size, 4) if price is not None and size is not None else None
    async with _db.connect() as conn:
        await conn.execute(
            """
            INSERT INTO live_orders (
              created_at, window_slug, token_id, intent, side, price, size, notional_usd,
              order_type, status, clob_order_id, error, details_json, mode, placement_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'live', ?)
            """,
            (
                _db.utc_now_iso(), window_slug, token_id, intent, side, price, size, notional,
                order_type, status, clob_order_id, redact_secrets(error),
                redact_secrets(json.dumps(details, sort_keys=True, default=str)), placement,
            ),
        )
        await conn.commit()


async def journal_rows(limit: int = 50) -> list[dict[str, Any]]:
    """The newest rows first."""
    async with _db.connect() as conn:
        async with conn.execute(
            "SELECT * FROM live_orders ORDER BY id DESC LIMIT ?", (int(limit),)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
