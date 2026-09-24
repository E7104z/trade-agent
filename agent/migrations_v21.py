"""
Schema v21 - grader grounding (specs/GRADER_GROUNDING_SPEC_v1.md). Additive only.
Same rules as the earlier migrations: idempotent, bare token DOUBLE, no
sequences, every column nullable.

WHY

  outcomes.tape_summary
      A deterministic account of how each pick traded (open, high/low with
      times, fill, exit, VWAP side, MFE/MAE, minutes held), computed from the
      bars the evening already fetched. It replaces the "1-2 lines of tape
      narrative" the grader was asked for without being shown a single bar.

  outcomes.grounded
      Whether the grader's review of the pick cited only numbers that appear
      in what it was shown. An ungrounded review keeps its grade but can no
      longer stamp a rule confirmation.

  lessons.predicate_json
      The cell a lesson is about, in the same shape as a rule predicate, so a
      confirmation can be checked against the day's actual trades.

  lesson_contradictions
      Dated contradictions. Confirmations were dated (lesson_confirmations)
      and contradictions were only a counter, so the holdout split saw
      confirmations out-of-sample and never contradictions -- biasing every
      lesson's tier upward.

  grader_rejections
      Every lesson or lesson update the grounding checks refused, and why.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 21

STATEMENTS: list[str] = [
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS tape_summary VARCHAR",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS grounded BOOLEAN",
    "ALTER TABLE lessons ADD COLUMN IF NOT EXISTS predicate_json VARCHAR",
    """CREATE TABLE IF NOT EXISTS lesson_contradictions (
        lesson_id     INTEGER,
        session_date  DATE
    )""",
    """CREATE TABLE IF NOT EXISTS grader_rejections (
        session_date  DATE,
        kind          VARCHAR,
        lesson_id     INTEGER,
        ticker        VARCHAR,
        text          VARCHAR,
        unmatched     VARCHAR,
        created_at    TIMESTAMP DEFAULT current_timestamp
    )""",
]
