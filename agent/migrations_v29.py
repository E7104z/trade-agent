"""
Schema v29 - the persona stamp (specs/PERSONA_CHARACTER_SHEET_SPEC_v1.md §3.1).
Additive only. Same rules as the earlier migrations: idempotent, bare token
types, no sequences, every column nullable.

  daily_plans.persona_version / picks.persona_version / runs.persona_version
      The version of prompts/persona.md (the "earned hunger" character sheet)
      that was in the system prompt when the row was produced; NULL when the
      persona was off, or for a pick that no persona-carrying call chose (an
      auto-selected runner pick). The persona is a TREATMENT, so every row it
      influenced says so.

      It is deliberately its own column, not folded into prompt_version:
      nothing pools by prompt_version today, and keeping the treatment on its
      own axis means switching it on can never silently split an n>=8 cell.
      Historical rows stay NULL -- they were produced without the persona,
      which is exactly what NULL means.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 29

STATEMENTS: list[str] = [
    f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS persona_version VARCHAR"
    for t in ("daily_plans", "picks", "runs")
]
