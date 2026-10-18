"""
Schema v36 - the account is flat after the close (docs/FIX_PHASE0.md, D1). Additive only.

Same rules as the earlier migrations: idempotent, bare token types, no
sequences, every column nullable.

IOVA, 250 shares, was bought on 2026-09-30 and held through 16 sessions. No
ledger row knew about it, and both the 15:55 flatten and the morning carry
cleanup walk LEDGER rows, so nothing ever closed it. The fix reads the broker's
positions instead, and these two tables are what that needs.

  eod_backstop_closes
      One row per position the broker-truth sweeps tried to close, whatever any
      ledger said. kind is 'eod_backstop' (at/after close - EOD_FLATTEN_MINUTES)
      or 'morning_sweep' (a position that was opened in a PRIOR session and was
      still open: a carry). reason says what the ledger knew: 'unledgered' (no
      row at all), 'ledger_open' (a row of the session, or of the carry, was
      still open), 'prior_session_claim' (only a past session's row claimed it).
      A morning_sweep row is the FLATTEN MISSED line in the morning email and
      /api/status; an empty table on a clean day is the pass condition
      scripts/verify_fixes.py checks.

  tick_leases
      A short named lease so two overlapping intraday ticks (the 15:55, 15:57
      and 15:59 retries, or a slow tick still running) cannot both close the
      same position. A lease expires on its own, so a tick killed mid-run never
      blocks the next one.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 36

STATEMENTS: list[str] = [
    """CREATE TABLE IF NOT EXISTS eod_backstop_closes (
        close_id        VARCHAR PRIMARY KEY,
        session_date    DATE,
        kind            VARCHAR,
        symbol          VARCHAR,
        position_side   VARCHAR,
        qty             DOUBLE,
        avg_entry_price DOUBLE,
        reason          VARCHAR,
        claimed_by      VARCHAR,
        carried_from    DATE,
        close_order_id  VARCHAR,
        status          VARCHAR,
        submitted_at    TIMESTAMP,
        filled_price    DOUBLE,
        filled_at       TIMESTAMP,
        detail          VARCHAR
    )""",
    """CREATE TABLE IF NOT EXISTS tick_leases (
        lease_name   VARCHAR PRIMARY KEY,
        holder       VARCHAR,
        acquired_at  TIMESTAMP,
        expires_at   TIMESTAMP
    )""",
]
