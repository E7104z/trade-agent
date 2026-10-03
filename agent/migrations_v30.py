"""
Schema v30 - the trader's log (specs/TRADERS_LOG_SPEC_v1.md §1.1).
Additive only. Same rules as the earlier migrations: idempotent, bare token
types, no sequences, no DEFAULT nextval(), every column nullable.

  traders_log
      One first-person daily entry per session, written after the evening
      grading, for the HUMAN (evening email, /api/table, run.py traders-log).
      WRITE-ONLY for the machine: no loop, prompt, memory builder, miner,
      selector or grader reads it (tests/test_traders_log_firewall.py).

      facts_json     the exact slots the model was given (name, value, scope,
                     source) -- every number in the log came from here
      raw_text       the model's draft BEFORE substitution, slot tokens intact
      rendered_text  what the human sees: the substituted log, or the
                     deterministic facts-only fallback
      violations_json what the validator refused, if anything
      status         ok | fallback_facts_only | skipped | error

      One row per (session_date, lane); a re-run replaces it. lane is
      'combined' in v1.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 30

STATEMENTS: list[str] = [
    """CREATE TABLE IF NOT EXISTS traders_log (
        session_date     DATE,
        lane             VARCHAR,
        generated_at     TIMESTAMP,
        persona_version  VARCHAR,
        model            VARCHAR,
        status           VARCHAR,
        facts_json       VARCHAR,
        raw_text         VARCHAR,
        rendered_text    VARCHAR,
        violations_json  VARCHAR,
        tokens_in        INTEGER,
        tokens_out       INTEGER,
        cost_usd         DOUBLE,
        PRIMARY KEY (session_date, lane)
    )""",
]
