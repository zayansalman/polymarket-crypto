"""Daily (24h-window) altcoin Up/Down shadow strategy (issue #185).

Paper-only, no live gate: scans the Polymarket daily "Up or Down" family
across a tracked set of thinner altcoin markets (doge/sol/xrp/bnb/eth by
default) and shadow-trades a fixed-size position on whichever asset
currently shows the strongest signal against a realized-vol fair-value
model. See ``docs/CODE_MAP.md`` for how this fits the rest of the repo.
"""
from __future__ import annotations
