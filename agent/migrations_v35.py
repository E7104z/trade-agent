"""
Schema v35 - the morning in two invocations, and a runner selection that can
be finished by a later tick (specs/MORNING_BUDGET_SPEC_v1.md). Additive only.
Same rules as the earlier migrations: idempotent, bare token types, no
sequences.

  morning_checkpoints
      One row per session: everything the risk-manager half of the morning
      needs, written by the analyst half before it hands off. The analyst and
      the risk manager each took ~130s against Opus with thinking; together
      with the data bundle they filled the whole 300s function budget, and
      Vercel killed the run after it had saved its plan on 9/28, 9/30, 10/01,
      10/02 and 10/05. Each half now gets a budget of its own.
      stage: analyst_done -> risk_running -> complete | failed | superseded.
      arm_b_*: the paired challenger arm's own progress, so an arm killed
      mid-run is visible instead of silent.

  runner_sessions.selection_completed_at
      Set when the one selection call for a session has finished, whatever
      it decided. A claim with no completion, no reason and no selected
      candidate belongs to a request that was killed between the claim and
      the result; a later tick may take it over (ledger.claim_runner_selection).
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 35

STATEMENTS: list[str] = [
    """CREATE TABLE IF NOT EXISTS morning_checkpoints (
        trading_date   DATE PRIMARY KEY,
        request_id     VARCHAR,
        stage          VARCHAR,
        payload_json   VARCHAR,
        created_at     TIMESTAMP,
        claimed_at     TIMESTAMP,
        completed_at   TIMESTAMP,
        attempts       INTEGER,
        dispatch       VARCHAR,
        arm_b_status   VARCHAR,
        note           VARCHAR
    )""",
    "ALTER TABLE runner_sessions ADD COLUMN IF NOT EXISTS selection_completed_at TIMESTAMP",
]
