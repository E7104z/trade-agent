"""
Schema v33 - the agent's evening self-check (specs/EDGE_ACCELERATION_SPEC_v1.md
Part 3, agent/selfcheck.py). Additive only. Same rules as the earlier
migrations: idempotent, bare token types, no sequences.

  self_checks
      One row per check per session: the production questions every spec's
      "Phase 0" asked a person to answer by pasting /api/table JSON, now asked
      by the system itself where the data lives. status is ok | info | warn |
      bad; value_json holds the counts behind the headline.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 33

STATEMENTS: list[str] = [
    """CREATE TABLE IF NOT EXISTS self_checks (
        session_date   DATE,
        check_key      VARCHAR,
        status         VARCHAR,
        headline       VARCHAR,
        value_json     VARCHAR,
        PRIMARY KEY (session_date, check_key)
    )""",
]
