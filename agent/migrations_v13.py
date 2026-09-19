"""
Schema v13 - Selection Edge as a tracked, per-week table. Additive only.

Same rules as the earlier migrations: idempotent, bare token DOUBLE, no
sequences, every column nullable.

WHY

Selection-edge-vs-baseline lived only as prose inside the playbook's
"Current Best Edge" paragraph: a figure like "-18.2R, beat 12.6% of random
portfolios" written once and then rewritten from scratch each week. Prose
has no history, so nothing could answer "is selection getting better or
worse?" -- the single most important question about a lane whose entire
justification is that picking beats not-picking.

Promoting it to a tracked table, same tier as Calibration, gives it:
  - a row per week that persists rather than being overwritten
  - the same n-floor suppression Calibration already enforces, so a verdict
    is never stated below the threshold
  - a trend a reader can see at a glance

The numbers are computed deterministically by agent/ablation.py; this table
only stores what was computed. The miner renders it, it does not derive it.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION", "MIN_VERDICT_N"]

SCHEMA_VERSION = 13

# Below this many triggered picks, no verdict is stated -- matching the bar
# the Calibration table already uses for its own verdict line. The percentile
# itself is exact at any n; what is suppressed is the GENERALIZATION from
# "these picks" to "the selection process".
MIN_VERDICT_N = 20

STATEMENTS: list[str] = [
    """CREATE TABLE IF NOT EXISTS selection_edge_history (
        week_ending        DATE NOT NULL,
        n_triggered        INTEGER,
        actual_total_r     DOUBLE,
        random_mean_r      DOUBLE,
        edge_r             DOUBLE,
        pct_random_beaten  DOUBLE,
        p_value            DOUBLE,
        sessions           INTEGER,
        verdict            VARCHAR,
        sim_version        INTEGER,
        PRIMARY KEY (week_ending)
    )""",
]
