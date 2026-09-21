"""Leftovers of the two-sided 5m maker-quoting shadow line (#182, closed 2026-08-29).

That line's own modules (fills, quoter, types, ledger) and the two-sided
copy-trade research tooling built alongside it (feed, onchain, and the RPC
fast-feed path in tools/copytrade_shadow.py) are gone — deleted 2026-09-21
once the operator confirmed polymarket_bot/copytrade's live watcher had
replaced them. Two modules from that era are still live and stay here:

* ``mirror.py`` — ``price_the_copy``, used by the live copytrade trader.
* ``market_index.py`` — ``parse_market``, used by the live daily scanner.

Neither places an order. Both just happen to have been written for this
package before the strategy it was named for was retired.
"""
