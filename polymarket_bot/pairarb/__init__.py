"""Leftovers of the two-sided 5m maker-quoting shadow line (#182, closed 2026-08-29).

That line's own modules (fills, quoter, types, ledger) and every piece of
copy-trade code built alongside or after it are gone (copy-trade removed
2026-09-21). One module from that era is still live and stays here:

* ``market_index.py`` — ``parse_market``, used by the live daily scanner.

It places no orders. It just happens to have been written for this package
before the strategy it was named for was retired.
"""
