"""Shared Polymarket venue plumbing (formerly the 5m pair-quoting strategy).

The two-sided maker quoting strategy this package was built for (#182, shadow
only) targeted the 5-minute Up/Down family and was **removed 2026-09-19** along
with that family: ``quoter.py``, ``fills.py``, ``types.py`` and ``ledger.py``
are gone, as are ``tools/pairarb_shadow.py`` and ``tools/pairarb_report.py``.

What survives here is timeframe-agnostic venue plumbing that other live lines
depend on, which is the only reason the package name is still ``pairarb``:

* ``market_index`` — outcome-token -> market metadata resolver. Used by the
  daily altcoin scanner (``polymarket_bot/daily/market.py``).
* ``mirror`` / ``feed`` / ``onchain`` — the on-chain fill tape and copy pricing.
  Used by the copytrade tools.

**No order is ever placed from this package.** Renaming it to something honest
is a worthwhile follow-up; it was left alone here to keep this change small.
"""
