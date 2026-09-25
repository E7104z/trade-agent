"""
Schema v23 - planned-R realized metrics.

`r_multiple` floors the effective risk at 25% of planned risk (agent/grading.py).
A favorable gap-through fill that lands close to its stop therefore inflates
`r_multiple` by up to ~4x: the trade's P&L is divided by a tiny (floored)
denominator instead of the risk the plan actually staked. That inflated number
was flowing into the weekly miner's headline realized-R and every counterfactual
built on it.

This migration adds the honest twin, computed off the ORIGINALLY PLANNED risk
(|entry - stop|, never floored) and scored against the PLANNED ENTRY (not the
actual fill), so a favorable fill can never inflate the headline past the
plan's reward:risk:

    outcomes.r_planned                     = signed P&L vs planned entry / planned risk
    outcomes.eod_r_planned                 = hold-to-close counterfactual, planned-risk units
    runner_candidate_outcomes.r_planned    = same, for the runner lane
    runner_candidate_outcomes.eod_r_planned = same

`r_multiple` / `eod_r` are kept as-is for diagnostics (the stress ladder, the
fantasy-R guard's history, and every pre-existing report that reads them).
Existing rows are NOT backfilled (computing r_planned needs the 1-minute bars);
reports COALESCE r_planned -> r_multiple so a legacy row still contributes its
old value until it is re-graded.

Additive only. Same rules as the earlier migrations: idempotent, bare token
DOUBLE, no sequences, every column nullable.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 23

STATEMENTS: list[str] = [
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS r_planned DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS eod_r_planned DOUBLE",
    "ALTER TABLE runner_candidate_outcomes ADD COLUMN IF NOT EXISTS r_planned DOUBLE",
    "ALTER TABLE runner_candidate_outcomes ADD COLUMN IF NOT EXISTS eod_r_planned DOUBLE",
]
