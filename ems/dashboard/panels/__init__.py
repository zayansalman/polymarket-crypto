"""Dashboard panels.

Each module here owns one EMS panel (ribbon, feeds, controls, strategy,
market, decision_engine, performance, tca, blotter, daily_altcoin). The
orchestrator in ``execution_view.py`` loads data once and dispatches to
``render(...)`` on each panel — panels are pure transforms from
(data, context) → HTML string, with no DB access. Shared helpers live in
``_shared.py``; data loaders live in ``_data.py``.
"""
