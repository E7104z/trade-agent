"""
Schema v27 - the morning lane's entry window (2026-10-02). Additive only.

Same rules as the earlier migrations: idempotent, bare token types, no
sequences, every column nullable.

  picks / rejections / paired_arm_picks .entry_not_before
      "HH:MM" ET before which a limit or breakout entry may not fill. The
      analyst prompt has always said "no entries in the first 15 minutes after
      the open", but nothing enforced it: limit orders went to the broker at
      ~07:11 ET as day orders live from 09:30, and the simulator filled from
      09:30 too. On 2026-10-02 (NFP) the NKE and WDC short limits sat 0.3-0.6%
      through the premarket price and were set to fill on the opening print,
      against their own theses. The window is stamped on each row by the
      morning run, so the simulator, the shadow ledger, the paired arms and the
      live placer all read the same rule, and a row written before it existed
      (NULL) is graded exactly as it always was.

  deferred_placements
      One row per morning pick whose live order the intraday tick placed (or
      refused to place) at its entry window, and why -- so a pick that never
      reached the broker is a recorded fact, not an absent row.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 27

STATEMENTS: list[str] = [
    "ALTER TABLE picks ADD COLUMN IF NOT EXISTS entry_not_before VARCHAR",
    "ALTER TABLE rejections ADD COLUMN IF NOT EXISTS entry_not_before VARCHAR",
    "ALTER TABLE paired_arm_picks ADD COLUMN IF NOT EXISTS entry_not_before VARCHAR",
    """CREATE TABLE IF NOT EXISTS deferred_placements (
        plan_date   DATE NOT NULL,
        pick_id     INTEGER NOT NULL,
        ticker      VARCHAR,
        status      VARCHAR,
        detail      VARCHAR,
        attempted_at TIMESTAMP,
        PRIMARY KEY (plan_date, pick_id)
    )""",
]
