"""
Schema v20 - forward validation (specs/FORWARD_VALIDATION_SPEC_v1.md). Additive only.
Same rules as the earlier migrations: idempotent, bare token DOUBLE, no
sequences, every column nullable.

WHY

  research_rules.predicate_json / lane
      The machine-readable cell a rule is about: 1-2 {dimension, level}
      conditions in one lane, labelled by agent/buckets.py exactly as the
      weekly search labels them. Without it no trade could ever be matched to
      a rule, so no rule ever received a forward observation and nothing
      could ever be VALIDATED.

  research_rules.decay_count / last_observed_at
      Deterministic decay: a VALIDATED rule silent for 30 market sessions
      goes back to quarantine; silent twice, it retires. Decay used to be
      applied by the LLM miner from prose.

  research_observations.source_key / source
      source_key makes an observation idempotent (an evening retry must not
      count the same trade twice). source says where it came from: `taken`
      and `runner_field` count toward validation; counterfactual streams
      (`shadow`, `population`) are recorded as evidence but never counted.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "RESEARCH_STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 20

RESEARCH_STATEMENTS: list[str] = [
    "ALTER TABLE research_rules ADD COLUMN IF NOT EXISTS predicate_json VARCHAR",
    "ALTER TABLE research_rules ADD COLUMN IF NOT EXISTS lane VARCHAR",
    "ALTER TABLE research_rules ADD COLUMN IF NOT EXISTS decay_count INTEGER",
    "ALTER TABLE research_rules ADD COLUMN IF NOT EXISTS last_observed_at DATE",
    "ALTER TABLE research_observations ADD COLUMN IF NOT EXISTS source_key VARCHAR",
    "ALTER TABLE research_observations ADD COLUMN IF NOT EXISTS source VARCHAR",
]

STATEMENTS: list[str] = list(RESEARCH_STATEMENTS)
