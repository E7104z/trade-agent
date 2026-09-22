"""
Schema v14 - execution drift gets its own columns. Additive only.

Same rules as the earlier migrations: idempotent, bare token DOUBLE, no
sequences, every column nullable.

WHY

Through sim_version 3 the R denominator was recomputed off the ACTUAL fill
and then floored at 25% of planned risk. A fill that drifted toward the stop
collapsed that denominator, and the resulting R multiples were not merely
noisy -- they were outside the range the plan's own geometry permits. Rows
planned at 2R scored +11R; rows planned at 2R scored +0.48R. Both directions
of error came from the same place, and averaging them produced a "selection
edge" number that measured fill drift, not selection.

sim_version 4 fixes the denominator to planned geometry. That makes the
headline number comparable across rows, but it must not simply DELETE the
execution cost -- a paper ledger that ignores fills is its own kind of lie.
So the cost moves here, into columns of its own:

  fill_drift_r        what the bars showed, signed, before the conservative
                      clamp. Negative = the bar offered a better price than
                      the plan asked for.
  fill_slippage       the drift actually CHARGED, in price units. Never
                      negative: the conservative fill model declines to credit
                      a favorable fill, so a favorable bar costs nothing rather
                      than paying out.
  fill_slippage_r     the same, in units of planned risk
  r_multiple_at_fill  R measured from the (clamped) fill instead of the plan

and the identity r_multiple - r_multiple_at_fill == fill_slippage_r holds on
every triggered row, which is what makes the split auditable rather than just
asserted.

Rows written under sim_version <= 3 leave these NULL. They are NOT
backfillable from what was stored, because the corrupted denominator was
never recorded alongside the inputs -- those rows have to be re-graded from
bars, which scripts/regrade_outcomes.py does. Until they are, sim_version is
the fence: never average a v3 row against a v4 one.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 14

_DRIFT_COLUMNS = (
    ("fill_slippage", "DOUBLE"),
    ("fill_slippage_r", "DOUBLE"),
    ("fill_drift_r", "DOUBLE"),
    ("r_multiple_at_fill", "DOUBLE"),
)

STATEMENTS: list[str] = [
    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typ}"
    for table in ("outcomes", "rejections", "runner_candidate_outcomes")
    for col, typ in _DRIFT_COLUMNS
] + [
    # The runner lane never persisted the v3 execution flags at all, so the
    # one signal that could have caught this from the data side was missing
    # exactly where it was needed most.
    "ALTER TABLE runner_candidate_outcomes ADD COLUMN IF NOT EXISTS fill_vs_plan_r DOUBLE",
    "ALTER TABLE runner_candidate_outcomes ADD COLUMN IF NOT EXISTS execution_anomaly BOOLEAN",
    # Feed provenance on the outcome row itself. runner_sessions records the
    # IEX downgrade per session, but every consumer of an outcome row then had
    # to remember to join back to it; none did.
    "ALTER TABLE runner_candidate_outcomes ADD COLUMN IF NOT EXISTS feed_downgraded BOOLEAN",
]
