"""
Schema v24 - runner exit-policy replays (specs/RUNNER_EXIT_SPEC_v1.md).

One row per (session, ticker, policy): what that triggered runner candidate
would have realized, in planned-R, under each member of the pre-registered
exit family in agent/runner_exits.py. The `plan` row is the reference arm and
must reproduce runner_candidate_outcomes.r_planned exactly; the evening loop
refuses to store a candidate's replays when it does not.

exit_et / mfe_et are wall-clock ET, so "when do runners peak, and when should
they be sold" is a query, not a re-fetch of the bars.

Additive only. Same rules as the earlier migrations: idempotent, bare token
DOUBLE, no sequences, every column nullable.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 24

STATEMENTS: list[str] = [
    """CREATE TABLE IF NOT EXISTS runner_exit_replays (
        session_date    DATE,
        ticker          VARCHAR,
        policy          VARCHAR,
        policy_version  INTEGER,
        sim_version     INTEGER,
        r_planned       DOUBLE,
        exit_reason     VARCHAR,
        exit_et         TIMESTAMP,
        mfe_r           DOUBLE,
        mfe_et          TIMESTAMP,
        created_at      TIMESTAMP DEFAULT current_timestamp
    )""",
]
