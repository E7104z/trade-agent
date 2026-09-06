"""Schema v6 - candidate-flow and shadow-ledger provenance.

All statements are additive and idempotent. These fields explain where a
candidate left the morning pipeline without changing eligibility or grading.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 6

STATEMENTS: list[str] = [
    "ALTER TABLE daily_plans ADD COLUMN IF NOT EXISTS analyst_candidate_count INTEGER",
    "ALTER TABLE daily_plans ADD COLUMN IF NOT EXISTS risk_rejection_count INTEGER",
    "ALTER TABLE daily_plans ADD COLUMN IF NOT EXISTS validator_proposed_count INTEGER",
    "ALTER TABLE daily_plans ADD COLUMN IF NOT EXISTS validator_drop_count INTEGER",
    "ALTER TABLE daily_plans ADD COLUMN IF NOT EXISTS persisted_rejection_count INTEGER",
    "ALTER TABLE rejections ADD COLUMN IF NOT EXISTS source_stage VARCHAR",
    "ALTER TABLE rejections ADD COLUMN IF NOT EXISTS rejection_category VARCHAR",
    "ALTER TABLE rejections ADD COLUMN IF NOT EXISTS planned_rr DOUBLE",
    "ALTER TABLE picks ADD COLUMN IF NOT EXISTS exploration_lane VARCHAR",
    "ALTER TABLE picks ADD COLUMN IF NOT EXISTS exploration_rr DOUBLE",
]