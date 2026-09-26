"""The execution layer every strategy shares: how a resting order meets the venue.

A strategy decides what to rest; this package owns how an order is checked, where it goes and
how its fills and results are read, so paper and live cannot drift apart between strategies.

- ``queue``: pure maths. The depth ahead of a new order, and how the taker tape fills resting
  orders level by level (``allocate_fills``).
- ``tape``: reads from the venue. The taker trade tape (``read_taker_tape``,
  ``tape_newest_ts``) and a market's result (``market_outcome``).
- ``controls``: what every placement checks first. The operator's PAPER/LIVE selection
  (``requested_mode``), the kill switch file and the never-cross rule (``check_passive``).

Each strategy keeps its own ledger and its own bookkeeping on top of these.
"""
