"""Repositories: the only place raw SQL against a given table should live.

One module per aggregate. Today: ``positions.py`` (``db.py``'s
``paper_positions`` table). More follow as each ledger migrates — see
issue #266.
"""
