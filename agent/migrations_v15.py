"""
Schema v15 - runner decision timestamp + outcome entry time. Additive only.

Same rules as the earlier migrations: idempotent, bare token DOUBLE, no
sequences, every column nullable.

WHY

A runner candidate's snapshot is walked back to the bar where its conditions
FIRST held (up to 12 five-minute bars), but the candidate only exists once a
scan tick has seen it -- and the pool only contains names whose conditions
STILL hold at that tick. Production runs a single daily tick (~11:18 ET), so
a name that set up at 10:25 was graded as if entered at 10:30, forty-odd
minutes before anything could have acted on it, and names that set up and
then failed before the tick were never in the pool at all. That is look-ahead
bias in every runner R the weekly miner learns from, and it made the
simulator and the live paper account measure different trades (live orders
go in at market at selection time).

The honest entry is the LATEST of: the detection bar's close, the scan tick
that first recorded the candidate (scan_pass_et, already stored), and -- for
every candidate that existed when the choice was made -- the selection
decision itself. `selection_at` records that decision time; `entry_after_et`
records the entry cutoff the evening grader actually used, so each outcome row
can be audited without re-deriving the rule.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 15

STATEMENTS: list[str] = [
    "ALTER TABLE runner_sessions ADD COLUMN IF NOT EXISTS selection_at TIMESTAMP",
    "ALTER TABLE runner_candidate_outcomes ADD COLUMN IF NOT EXISTS entry_after_et TIMESTAMP",
    # Live paper: the market order that flattened a position at its time stop
    # / end of day. Alpaca fills it asynchronously -- the DELETE /positions
    # response carries no fill price -- so its id is kept and the fill is read
    # back on a later monitor tick or at the evening reconcile. Without it
    # those exits were recorded with no price (or not at all) and live_r was
    # never computed for them.
    "ALTER TABLE live_orders ADD COLUMN IF NOT EXISTS close_order_id TEXT",
    # Hosted crash-recovery archive for paid LLM responses (agent/llm.py
    # _archive_response). The local archive is a directory in the repo, which
    # is read-only on Vercel -- so in production, where the expensive weekly
    # rewrite actually runs, the archive silently never existed.
    """CREATE TABLE IF NOT EXISTS llm_archive (
        archive_id   VARCHAR PRIMARY KEY,
        saved_at     TIMESTAMP,
        prompt_name  VARCHAR,
        model        VARCHAR,
        usage_json   VARCHAR,
        text         VARCHAR
    )""",
]
