"""
Schema v32 - the forward test spends one error budget across many looks
(specs/EDGE_ACCELERATION_SPEC_v1.md Part 1, agent/sequential.py). Additive
only. Same rules as the earlier migrations: idempotent, bare token types, no
sequences, every column nullable.

  research_rules.alpha_spent / trader_hypotheses.alpha_spent
      Cumulative one-sided alpha this rule's (or idea's) looks have already
      spent. Each new look tests only at the increment, so the total across
      every look stays at sequential.ALPHA. NULL reads as 0: a rule that has
      never been looked at under the new design starts with its whole budget.

  research_rules.looks_done / last_look_at
      How many looks have been taken and when, for the record and the email.
      (trader_hypotheses already has looks_done.)

  research_rules.test_design / trader_hypotheses.test_design
      The design a look was taken under ('spend-v1'). A rule retired by the
      old single look carries NULL here, which is how the one-time reopening
      in research.reopen_underpowered_retirements finds it -- and why it can
      never reopen the same rule twice.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "RESEARCH_STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 32

# The research_rules columns alone, for fixtures that build only the research
# layer (migrations_v4.research_schema).
RESEARCH_STATEMENTS: list[str] = [
    "ALTER TABLE research_rules ADD COLUMN IF NOT EXISTS alpha_spent DOUBLE",
    "ALTER TABLE research_rules ADD COLUMN IF NOT EXISTS looks_done INTEGER",
    "ALTER TABLE research_rules ADD COLUMN IF NOT EXISTS last_look_at DATE",
    "ALTER TABLE research_rules ADD COLUMN IF NOT EXISTS test_design VARCHAR",
]

STATEMENTS: list[str] = RESEARCH_STATEMENTS + [
    "ALTER TABLE trader_hypotheses ADD COLUMN IF NOT EXISTS alpha_spent DOUBLE",
    "ALTER TABLE trader_hypotheses ADD COLUMN IF NOT EXISTS test_design VARCHAR",
]
