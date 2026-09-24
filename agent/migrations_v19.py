"""
Schema v19 - data continuity (specs/DATA_CONTINUITY_SPEC_v1.md). Additive only.
Same rules as the earlier migrations: idempotent, bare token DOUBLE, no
sequences, every column nullable.

WHY

  runs.stages
      Per-stage outcome and elapsed seconds for a run, as JSON. A run whose
      deterministic stage completed and whose LLM call then failed used to
      read exactly like a run that never happened; and a run killed by the
      300s budget logs nothing at all, so the only defense is seeing the
      stage timings creep toward it on the runs that do finish.

  alerts
      One row per provider condition per day that has already been emailed,
      so a credit outage that fails four runs sends one alert, not four.
      Credit ran out on 09-02, 09-04 and 09-22; each time it was found by
      reading a failed run after the fact.

  data_coverage
      Per session, lane, stage and field: how many rows were graded and how
      many had the field null. Alpaca daily bars were refused on every recent
      date for weeks and fell back to a source that returned nothing,
      nulling day OHLC in grading, and nothing noticed. Nulls across every
      ticker at once are plumbing, not signal; this is where that shows.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 19

STATEMENTS: list[str] = [
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS stages VARCHAR",
    """CREATE TABLE IF NOT EXISTS alerts (
        alert_date  DATE,
        kind        VARCHAR,
        loop        VARCHAR,
        detail      VARCHAR,
        sent_at     TIMESTAMP DEFAULT current_timestamp
    )""",
    """CREATE TABLE IF NOT EXISTS data_coverage (
        session_date  DATE,
        lane          VARCHAR,
        stage         VARCHAR,
        field         VARCHAR,
        n_total       INTEGER,
        n_null        INTEGER,
        source        VARCHAR,
        created_at    TIMESTAMP DEFAULT current_timestamp
    )""",
]
