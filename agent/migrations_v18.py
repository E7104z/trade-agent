"""
Schema v18 - write-time enforcement record (specs/WRITE_TIME_ENFORCEMENT_SPEC_v1.md).
Additive only. Same rules as the earlier migrations: idempotent, bare token
DOUBLE, no sequences, every column nullable.

WHY

  playbook_violations
      Every write-time integrity finding on a weekly rewrite, with what the
      system did about it (withheld / replaced / inserted / deduplicated /
      logged / rewrite_rejected) and the offending text itself. The checkers
      used to print a WARN line and write the document anyway; in production
      those lines lived in Vercel runtime logs for minutes. A finding nobody
      can read back cannot change anything, so it is now a ledger row.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 18

STATEMENTS: list[str] = [
    """CREATE TABLE IF NOT EXISTS playbook_violations (
        run_date    DATE,
        version     INTEGER,
        checker     VARCHAR,
        severity    VARCHAR,
        section     VARCHAR,
        line        VARCHAR,
        message     VARCHAR,
        action      VARCHAR,
        created_at  TIMESTAMP DEFAULT current_timestamp
    )""",
]
