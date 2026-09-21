"""Outcome-token -> market metadata resolver for the daily altcoin scanner.

Gamma returns each outcome token as a bare id; ``parse_market`` pulls the
window slug, condition id and both outcome tokens (Up-first, regardless of
the order Gamma returns them in) out of one market row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MarketTokens:
    """One market's identity plus its two outcome tokens, Up-first."""

    slug: str
    condition_id: str
    up_token: str
    down_token: str


def parse_market(market: dict[str, Any]) -> MarketTokens | None:
    """Extract token ids from one Gamma market row, or ``None`` if malformed."""
    try:
        tokens = json.loads(market.get("clobTokenIds") or "[]")
        outcomes = [str(o).lower() for o in json.loads(market.get("outcomes") or "[]")]
    except (TypeError, ValueError):
        return None
    if len(tokens) != 2 or len(outcomes) != 2:
        return None
    idx_up = 0 if outcomes[0] == "up" else 1
    idx_down = 1 - idx_up
    slug = str(market.get("slug") or "")
    up_token = str(tokens[idx_up] or "")
    down_token = str(tokens[idx_down] or "")
    if not slug or not up_token or not down_token:
        return None
    return MarketTokens(
        slug=slug,
        condition_id=str(market.get("conditionId") or ""),
        up_token=up_token,
        down_token=down_token,
    )
