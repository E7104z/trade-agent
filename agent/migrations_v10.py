"""
Schema v10 - DTO Runner lane (RUNNER_PORT_SPEC_v1 1.1). Additive only.

Same integration and rules as migrations_v4/v5: agent/ledger.py appends
`STATEMENTS` to `_MIGRATIONS`, which `ledger.connect()` executes in BOTH the
DuckDB and Postgres branches (the Postgres branch rewrites the bare token
DOUBLE to DOUBLE PRECISION). So:

  - Every statement must be idempotent (CREATE TABLE IF NOT EXISTS).
  - Write the bare token DOUBLE, never DOUBLE PRECISION.
  - No sequences, no DEFAULT nextval().
  - Every column nullable.

NOTHING HERE CHANGES EXISTING BEHAVIOR. Three new tables; no existing table is
touched, no existing column changes meaning, no historical row is recomputed.

Why `runner_sessions` exists at all: it is the stand-down-vs-outage
discriminator. A zero-candidate day with `universe_size = 0` or
`provider_degraded = true` is a pipeline failure; a zero-candidate day with a
full universe and a high `rejected_conditions` is a genuine stand-down. Those
two must never be confusable after the fact, and Vercel log retention (~1 hour
on Hobby) means the app's own tables are the only durable record.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 10

STATEMENTS: list[str] = [

    # ---------------------------------------------------------- candidates
    # One row per (session, ticker) that passed EVERY gate at least once.
    # The detection snapshot is locked on first pass and never overwritten --
    # later scan cycles do nothing (INSERT ... ON CONFLICT DO NOTHING). That
    # anchoring is what makes detection_minutes a usable research dimension:
    # if a later cycle could rewrite the snapshot, the recorded price would
    # drift with the tape and the "when was this detected" question would have
    # no stable answer.
    """CREATE TABLE IF NOT EXISTS runner_candidates (
        session_date        DATE NOT NULL,
        ticker              VARCHAR NOT NULL,
        first_pass_et       TIMESTAMP,
        detection_minutes   INTEGER,
        scan_pass_et        TIMESTAMP,
        detection_price     DOUBLE,
        vwap                DOUBLE,
        vwap_pct_above      DOUBLE,
        ema9                DOUBLE,
        ema9_slope          DOUBLE,
        rvol                DOUBLE,
        trend_consistency   DOUBLE,
        change_pct          DOUBLE,
        float_shares        DOUBLE,
        earnings_days_away  INTEGER,
        prev_close          DOUBLE,
        prev_volume         DOUBLE,
        price_tier          VARCHAR,
        stop_price          DOUBLE,
        target_price        DOUBLE,
        stop_dist           DOUBLE,
        gates_status        VARCHAR,
        scan_version        INTEGER,
        PRIMARY KEY (session_date, ticker)
    )""",

    # ------------------------------------------------------------ outcomes
    # Every candidate is priced by the evening simulator, traded or not. That
    # is the whole point of the design: evidence on the full candidate list is
    # free (bar fetches, not tokens), and without it "did selection help?" has
    # no denominator. `selected` / `selection_rank` / `pick_id` stay NULL in
    # Phase 1 and are filled by Phase 2's selector.
    """CREATE TABLE IF NOT EXISTS runner_candidate_outcomes (
        session_date        DATE NOT NULL,
        ticker              VARCHAR NOT NULL,
        eval_date           DATE,
        triggered           BOOLEAN,
        entry_fill          DOUBLE,
        exit_price          DOUBLE,
        exit_reason         VARCHAR,
        r_multiple          DOUBLE,
        mfe_r               DOUBLE,
        mae_r               DOUBLE,
        eod_r               DOUBLE,
        selected            BOOLEAN,
        selection_rank      INTEGER,
        pick_id             INTEGER,
        sim_version         INTEGER,
        PRIMARY KEY (session_date, ticker)
    )""",

    # ------------------------------------------------------------ sessions
    # provider_degraded mirrors the migrations_v5 rule: TRUE when any single
    # gate was unavailable for MORE THAN HALF the scanned universe (one missing
    # float is noise; a session-wide absence is an outage).
    """CREATE TABLE IF NOT EXISTS runner_sessions (
        session_date             DATE PRIMARY KEY,
        universe_size            INTEGER,
        scan_passes              INTEGER,
        candidates_found         INTEGER,
        rejected_price           INTEGER,
        rejected_rvol            INTEGER,
        rejected_conditions      INTEGER,
        rejected_float           INTEGER,
        rejected_earnings        INTEGER,
        rejected_excluded        INTEGER,
        rejected_no_bars         INTEGER,
        rejected_too_few_bars    INTEGER,
        gates_unavailable        VARCHAR,
        provider_degraded        BOOLEAN,
        selection_made           BOOLEAN,
        selection_skipped_reason VARCHAR,
        scan_version             INTEGER
    )""",
]
