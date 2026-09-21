"""Execution: the live CLOB executor and pre-trade risk gate.

PaperExecutionManager and RiskService used to be re-exported here. Neither had
a production constructor anywhere in the repo — live paper fills are journaled
inline in `polymarket_bot/paper.py`, and the live risk check is
`execution/gate.py:RiskGate`. Removed 2026-09-21.
"""
