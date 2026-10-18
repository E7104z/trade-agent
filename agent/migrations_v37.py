"""
Schema v37 - the simulator fills only what a broker would, and the review shows
the broker (docs/FIX_PHASE0.md, D2 + D3). Additive only.

Same rules as the earlier migrations: idempotent, bare token types, no
sequences, every column nullable.

  outcomes.touch_only_bars            (sim_version 8)
      For a LIMIT entry: how many live bars reached the limit without trading
      through it enough to fill it. A pick that never triggered with this >= 1
      is a touch the old simulator would have filled (RKLB 2026-10-15: sell
      limit 54.00, the only bar above it printed 54.02, Alpaca never filled it).

  outcomes.broker_entry_fill / broker_exit_fill / broker_exit_reason /
  broker_status / sim_broker_divergence
      What the paper account actually did with a pick that went to the broker,
      stored NEXT TO the simulated outcome (never in place of it: the simulated
      R stays the research number). Copied from live_orders at the evening
      reconcile; live_r (schema v7) is the broker's R from its own fills.
      sim_broker_divergence is none | sim_only_fill | broker_only_fill |
      price_gap (grading.classify_divergence).

  outcomes.r_if_held_to_flatten       (docs/FIX_PHASE0.md, Phase 5)
      R if the position were held from the fill to the EOD flatten with only
      the initial stop. A reference number, never mixed into r_multiple or any
      total. Added here, with the other outcome columns, so the Phase 5 change
      is only the simulator's.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 37

STATEMENTS: list[str] = [
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS touch_only_bars INTEGER",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS broker_entry_fill DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS broker_exit_fill DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS broker_exit_reason VARCHAR",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS broker_status VARCHAR",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS sim_broker_divergence VARCHAR",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS r_if_held_to_flatten DOUBLE",
]
