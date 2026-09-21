"""Operator controls and background recorders.

incidents.IncidentManager and telemetry.FeedHealthTracker/LatencyTracker used
to be re-exported here. Neither had a live consumer anywhere outside this
barrel and their own tests. Removed 2026-09-21.
"""
