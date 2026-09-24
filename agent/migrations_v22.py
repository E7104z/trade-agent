"""
Schema v22 - evidence backlog and daily diagnostics (specs/EVIDENCE_BACKLOG_SPEC_v1.md).
Additive only. Same rules as the earlier migrations: idempotent, bare token
DOUBLE, no sequences, every column nullable.

WHY

  evidence_backlog
      One nightly snapshot of what evidence the desk is MISSING: quarantined
      rules short of their forward target, open questions short of their
      threshold, and mined cells with 1 <= n < 8. COUNTS, TARGETS AND RATES
      ONLY -- never a mean, a win rate, a p-value or an effect sign. A daily
      table of effect sizes would be an unregistered look taken every day; a
      table of counts is an allocation of collection effort and cannot bias a
      test. The morning analyst reads the top of it.

  daily_diagnostics
      One row per evening: what was collected and what broke (health stages,
      coverage, forward observations, backlog movement, grounding refusals,
      integrity findings). Same rule: no effect sizes.

  picks.probe_for
      The backlog item an exploration probe was taken to feed, so its
      evidence is attributable and the probe lane itself can be measured.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 22

STATEMENTS: list[str] = [
    """CREATE TABLE IF NOT EXISTS evidence_backlog (
        session_date  DATE,
        rank          INTEGER,
        item_type     VARCHAR,
        item_id       VARCHAR,
        lane          VARCHAR,
        n             INTEGER,
        target        INTEGER,
        rate_10s      INTEGER,
        eta_sessions  INTEGER,
        stalled       BOOLEAN,
        what_counts   VARCHAR
    )""",
    """CREATE TABLE IF NOT EXISTS daily_diagnostics (
        session_date  DATE,
        payload_json  VARCHAR,
        created_at    TIMESTAMP DEFAULT current_timestamp
    )""",
    "ALTER TABLE picks ADD COLUMN IF NOT EXISTS probe_for VARCHAR",
]
