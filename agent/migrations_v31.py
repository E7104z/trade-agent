"""
Schema v31 - the trader's hypothesis notebook (specs/TRADER_HYPOTHESIS_SPEC_v1.md).
Additive only. Same rules as the earlier migrations: idempotent, bare token
types, no sequences, no DEFAULT nextval(), every column nullable.

  trader_hypotheses
      One row per idea the trader wrote down to test. statement, refutation,
      target_n, max_n and min_sessions are frozen at insert (pre-registration):
      no code path updates them, and changing an idea means registering a new
      one. The system sets the bar; the LLM never does.

      state            OPEN | SUPPORTED | REFUTED | ABANDONED | EXPIRED
      looks_done       0, 1 (positive but not yet significant at target_n), 2
      cohort_counted   an ABANDONED or EXPIRED idea joins the next weekly
                       cohort at p = 1, once; this marks that it has
      rule_id          the research rule a SUPPORTED idea with a predicate
                       was quarantined as

  picks.hypothesis_id
      The one hypothesis a pick tests, written by the morning loop BEFORE the
      outcome exists. Nothing in the evening or weekly loop writes it, so a
      tag can never be chosen with the result in view.

  hypothesis_reviews
      The evening grader's per-trade note: did the trade, as it actually
      traded, express the idea? An annotation only. It never removes a trade
      from the count -- only fill validity does that.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 31

STATEMENTS: list[str] = [
    """CREATE TABLE IF NOT EXISTS trader_hypotheses (
        hyp_id           VARCHAR PRIMARY KEY,
        created_date     DATE,
        opened_at        DATE,
        source           VARCHAR,
        from_lesson_id   INTEGER,
        statement        VARCHAR,
        refutation       VARCHAR,
        origin           VARCHAR,
        belief           INTEGER,
        direction        VARCHAR,
        setup_type       VARCHAR,
        predicate_json   VARCHAR,
        target_n         INTEGER,
        max_n            INTEGER,
        min_sessions     INTEGER,
        state            VARCHAR,
        looks_done       INTEGER,
        last_tagged_at   DATE,
        closed_at        DATE,
        close_reason     VARCHAR,
        verdict_json     VARCHAR,
        cohort_counted   BOOLEAN,
        rule_id          VARCHAR,
        note             VARCHAR
    )""",
    "ALTER TABLE picks ADD COLUMN IF NOT EXISTS hypothesis_id VARCHAR",
    """CREATE TABLE IF NOT EXISTS hypothesis_reviews (
        session_date     DATE,
        pick_id          INTEGER,
        hyp_id           VARCHAR,
        expressed        BOOLEAN,
        note             VARCHAR,
        PRIMARY KEY (session_date, pick_id)
    )""",
]
