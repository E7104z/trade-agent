"""
Schema v17 - integrity repair (specs/INTEGRITY_REPAIR_SPEC_v1.md). Additive only.

Same rules as the earlier migrations: idempotent, bare token DOUBLE, no
sequences, every column nullable.

WHY

  research_looks.test
      Which statistic produced the row's p-value. `evaluate_search` used to
      re-test every comparison as a one-sample mean, overwriting the Welch
      test the runner selection-edge comparison supplies. The registry must
      say which test each recorded look used, so a two-sample row is never
      read as a one-sample one. NULL on rows written before v17 (they were
      all re-tested one-sample, whatever their producer intended).

  runner_candidates.regated_from
      Provenance for `run.py regate-runner`: a scan_version 1 row whose
      STORED inputs pass every scan_version 2 gate is relabelled
      scan_version 2 and carries regated_from = 1. NULL for rows the live
      scan wrote under their own version.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "RESEARCH_STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 17

# Columns added to the v4 research tables (see migrations_v4.research_schema).
RESEARCH_STATEMENTS: list[str] = [
    "ALTER TABLE research_looks ADD COLUMN IF NOT EXISTS test VARCHAR",
]

STATEMENTS: list[str] = RESEARCH_STATEMENTS + [
    "ALTER TABLE runner_candidates ADD COLUMN IF NOT EXISTS regated_from INTEGER",
]
