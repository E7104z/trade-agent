"""
Schema v12 - untriggered-pick reason tagging. Additive only.

Same rules as the earlier migrations: idempotent, bare token DOUBLE, no
sequences, every column nullable.

WHY

An untriggered pick currently records `exit_reason = 'no_trigger'` and a
`missed_move_r`, which says how far the idea ran without us but NOT why we
never got on. Those are two very different failures:

  - the market never offered the setup at all, or
  - the setup was there and our specific entry mechanic was too narrow to
    catch it

The first is the market's doing and implies nothing about the rule. The
second is a rule defect, and it is the only one that would justify loosening
an entry condition. Collapsing both into "no_trigger" means a systematically
over-tight entry looks identical to a quiet week, which is the same
stand-down-vs-outage confusion runner_sessions exists to prevent, one layer
up.

`untriggered_reason` is a closed enum:
  no_setup_offered             price never approached the trigger
  entry_condition_not_met      price came close; the mechanic did not fire
  time_stop_expired_pretrigger the cutoff passed before price could trigger
  other                        requires untriggered_note

NULL for triggered picks, and for rows graded before this column existed.
`untriggered_note` is free text, required only for `other`.

This is INSTRUMENTATION. Nothing reads it to change a decision; the weekly
miner reports the breakdown so a loosening decision can later be made on
evidence rather than on the absence of it.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION", "UNTRIGGERED_REASONS"]

SCHEMA_VERSION = 12

# The closed enum, exported so the grader, the miner and the tests all agree
# on the same spelling rather than each hard-coding string literals.
UNTRIGGERED_REASONS = (
    "no_setup_offered",
    "entry_condition_not_met",
    "time_stop_expired_pretrigger",
    "other",
)

STATEMENTS: list[str] = [
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS untriggered_reason VARCHAR",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS untriggered_note VARCHAR",
    # How close price came to the trigger, in units of PLANNED risk, signed so
    # that negative means "never reached it". This is the deterministic number
    # the reason is derived from, stored alongside so the classification can
    # be audited (and re-derived under a different threshold) without
    # re-simulating the session.
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS trigger_gap_r DOUBLE",
]
