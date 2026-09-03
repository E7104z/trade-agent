"""
Schema v5 — DTO-port evidence columns (Phase 1, additive only).

Same integration and rules as migrations_v4: agent/ledger.py appends
`STATEMENTS` to `_MIGRATIONS`, which `ledger.connect()` executes in BOTH the
DuckDB and Postgres branches (the Postgres branch rewrites the bare token
DOUBLE to DOUBLE PRECISION). So:

  - Every statement must be idempotent (ADD COLUMN IF NOT EXISTS).
  - Write the bare token DOUBLE, never DOUBLE PRECISION.
  - No sequences, no DEFAULT nextval().

NOTHING HERE CHANGES EXISTING BEHAVIOR. Every added column is nullable with no
default; existing rows read exactly as they did before.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 5

STATEMENTS: list[str] = [

    # ------------------------------------------------ Phase 1c: half-day calendar
    # session_length: "full" | "half" (13:00 ET early close). A half-day outcome
    # was graded against a session that ended three hours early — eod resolves at
    # 13:00, flatten_at/time_stop cutoffs later than the close never fire. That
    # is a different rule set arriving through a door sim_version doesn't watch;
    # this column makes those rows filterable out of any comparison forever.
    # NULL = graded before this column existed (session date not threaded in).
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS session_length VARCHAR",
    # flatten_inert: the configured flatten_at was at/after the session close and
    # therefore could not fire (True), fired normally or could have (False), or
    # no flatten was configured / row predates the column (NULL). Recorded so an
    # inert flatten rule is a visible fact, not a silent no-op.
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS flatten_inert BOOLEAN",
]
