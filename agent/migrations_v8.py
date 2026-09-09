"""Schema v8 - session data-health telemetry.

All statements are additive and idempotent. These fields record whether a
session's pipeline was ABLE to evaluate the market, as distinct from whether it
evaluated the market and declined. Nothing here gates a pick, changes R math,
or alters what the weekly miner learns from -- they only let a no-trade day
caused by a data outage be told apart from a no-trade day that was a judgment
call. Without that distinction the miner eventually pools two entirely
different classes under one "no_trade" label, and no amount of FDR correction
repairs a mislabeled class.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 8

STATEMENTS: list[str] = [
    # Full verdict as JSON (counts + thresholds that fired), mirroring how
    # gates_failed_open already stores its counter map.
    "ALTER TABLE daily_plans ADD COLUMN IF NOT EXISTS data_health VARCHAR",
    # Discrete boolean for cheap querying, mirroring provider_degraded.
    "ALTER TABLE daily_plans ADD COLUMN IF NOT EXISTS session_degraded BOOLEAN",
    # Carried onto every rejection row so rejection-reason frequency analysis
    # can exclude a session whose rejections were plumbing, not judgment. The
    # rows are still stored and still graded -- a counterfactual is still a
    # counterfactual -- they are just no longer countable as decisions.
    "ALTER TABLE rejections ADD COLUMN IF NOT EXISTS session_degraded BOOLEAN",
]
