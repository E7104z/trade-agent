"""
Schema v34 - the gap field (specs/EDGE_ACCELERATION_SPEC_v1.md Part 4,
agent/gapfield.py). Additive only. Same rules as the earlier migrations:
idempotent, bare token types, no sequences.

  gapfield_events
      One row per (session, symbol, arm): every liquid common stock that
      opened >= 3% away from its prior close, traded mechanically both ways
      (momentum = with the gap, fade = against it) on its daily bar. The
      inputs every dimension is labeled from are stored beside the outcome,
      so a bucket edge can be re-derived without refetching.
      source: 'forward' (computed the night of the session; the only rows a
      forward observation may come from) or 'backfill' (history, for
      discovery only).

  gapfield_jobs
      The backfill's progress: the symbol list frozen at the job's start and
      how far through it the nightly runs have got, so a run killed at the
      function budget resumes where it stopped.

  gapfield_cells
      The latest per-cell summary (strategy x dimension x level, in
      (session, industry) units) the weekly FDR batch reads, so the weekly
      run never has to pull the whole history across the network.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 34

STATEMENTS: list[str] = [
    """CREATE TABLE IF NOT EXISTS gapfield_events (
        session_date   DATE,
        symbol         VARCHAR,
        arm            VARCHAR,
        direction      VARCHAR,
        gap_pct        DOUBLE,
        gap_atr        DOUBLE,
        atr14          DOUBLE,
        prev_close     DOUBLE,
        adv20_usd      DOUBLE,
        sma200         DOUBLE,
        prior_ret_pct  DOUBLE,
        vol_ratio      DOUBLE,
        spy_gap_pct    DOUBLE,
        industry       VARCHAR,
        day_open       DOUBLE,
        day_high       DOUBLE,
        day_low        DOUBLE,
        day_close      DOUBLE,
        entry_price    DOUBLE,
        stop_price     DOUBLE,
        exit_price     DOUBLE,
        exit_reason    VARCHAR,
        r_multiple     DOUBLE,
        sim_version    INTEGER,
        source         VARCHAR,
        computed_at    TIMESTAMP,
        PRIMARY KEY (session_date, symbol, arm)
    )""",
    """CREATE TABLE IF NOT EXISTS gapfield_jobs (
        job_id         VARCHAR PRIMARY KEY,
        range_start    DATE,
        range_end      DATE,
        symbols_json   VARCHAR,
        n_symbols      INTEGER,
        next_index     INTEGER,
        rows_written   INTEGER,
        started_at     TIMESTAMP,
        updated_at     TIMESTAMP,
        finished_at    TIMESTAMP,
        note           VARCHAR
    )""",
    """CREATE TABLE IF NOT EXISTS gapfield_cells (
        strategy       VARCHAR,
        dimension      VARCHAR,
        level          VARCHAR,
        n_units        INTEGER,
        n_rows         INTEGER,
        mean_r         DOUBLE,
        std_r          DOUBLE,
        sessions       INTEGER,
        first_session  DATE,
        last_session   DATE,
        computed_on    DATE,
        PRIMARY KEY (strategy, dimension, level)
    )""",
]
