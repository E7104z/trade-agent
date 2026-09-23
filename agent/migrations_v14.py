"""
Schema v14 - runner candidate ATR/trend context. Additive only.

Same rules as the earlier migrations: idempotent, bare token DOUBLE, no
sequences, every column nullable.

WHY

The runner lane prices EVERY candidate (selected or not) at grading time, but
the indicator-layer-v2 fields (ATR, 200d trend side) were only ever computed
for MORNING picks, not for runner candidates. So the weekly miner could ask
"do 1-2 ATR stops out-R sub-0.5 ATR stops?" and "do names above the 200d MA
out-R names below it?" for the morning lane, but the SAME questions could not
be asked of the runner candidate pool -- which is exactly where the review of
2026-09-23 showed the selection was failing. stop_dist_atr and trend_side_200d
land here so every candidate carries the same dimensions the picks already do.

The values are computed deterministically by agent/loops/evening.py from the
same batched daily_context fetch the morning grader uses; this table only
stores what was computed.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 14

STATEMENTS: list[str] = [
    "ALTER TABLE runner_candidate_outcomes ADD COLUMN IF NOT EXISTS atr14 DOUBLE",
    "ALTER TABLE runner_candidate_outcomes ADD COLUMN IF NOT EXISTS stop_dist_atr DOUBLE",
    "ALTER TABLE runner_candidate_outcomes ADD COLUMN IF NOT EXISTS trend_side_200d VARCHAR",
]
