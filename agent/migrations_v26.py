"""
Schema v26 - runner pool integrity (specs/RUNNER_POOL_INTEGRITY_v1.md). Additive only.

Same rules as the earlier migrations: idempotent, bare token types, no
sequences, every column nullable.

  runner_candidates.plan_bar_et
      The 5-minute bar the trade plan (detection_price, stop, target and the
      feature snapshot) was taken from: the last COMPLETE bar at the scan tick
      that recorded the candidate (scan_version 3). first_pass_et stays the
      walked-back bar where the VWAP/EMA conditions first held -- a research
      timestamp, never a plan. Under scan_version 2 the plan was taken at that
      walked-back bar, up to an hour before anything could act on it, so a
      name that kept running was entered on a stale stop/target pair.

  runner_candidates.asset_name / instrument_class / quote_type / sector / industry
      What the instrument IS. asset_name is Alpaca's registered name;
      instrument_class is agent/instruments.classify() on it; quote_type,
      sector and industry come from the same yfinance info call that already
      supplies float. industry is the cluster key: one session's rows in one
      industry are one event, not n observations.

  runner_candidate_outcomes.session_close / post_move_atr / invalid_reason
      session_close is the last regular-session close; post_move_atr is
      (session_close - detection_price) / ATR14, the move after the system
      could act, in ATR units -- what the dashboard ranks "biggest moves" by.
      invalid_reason says WHY a fill is not a trade (wrong side of the stop,
      or too little reward left at the fill).

  runner_sessions.universe_stats
      JSON: how many symbols each universe source contributed and how many
      each lane filter removed (instrument class, liquidity floor), so a
      shrinking universe is explained rather than inferred.

  runner_sessions.rejected_liquidity / rejected_instrument
      Gate-chain counters for the two filters scan_version 3 added.

  fundamentals.sector / industry / quote_type
      Cached with float; a cached row without quote_type predates this
      migration and is refetched once (ledger.cached_fundamentals).
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 26

STATEMENTS: list[str] = [
    "ALTER TABLE runner_candidates ADD COLUMN IF NOT EXISTS plan_bar_et TIMESTAMP",
    "ALTER TABLE runner_candidates ADD COLUMN IF NOT EXISTS asset_name VARCHAR",
    "ALTER TABLE runner_candidates ADD COLUMN IF NOT EXISTS instrument_class VARCHAR",
    "ALTER TABLE runner_candidates ADD COLUMN IF NOT EXISTS quote_type VARCHAR",
    "ALTER TABLE runner_candidates ADD COLUMN IF NOT EXISTS sector VARCHAR",
    "ALTER TABLE runner_candidates ADD COLUMN IF NOT EXISTS industry VARCHAR",
    "ALTER TABLE runner_candidate_outcomes ADD COLUMN IF NOT EXISTS session_close DOUBLE",
    "ALTER TABLE runner_candidate_outcomes ADD COLUMN IF NOT EXISTS post_move_atr DOUBLE",
    "ALTER TABLE runner_candidate_outcomes ADD COLUMN IF NOT EXISTS invalid_reason VARCHAR",
    "ALTER TABLE runner_sessions ADD COLUMN IF NOT EXISTS universe_stats VARCHAR",
    "ALTER TABLE runner_sessions ADD COLUMN IF NOT EXISTS rejected_liquidity INTEGER",
    "ALTER TABLE runner_sessions ADD COLUMN IF NOT EXISTS rejected_instrument INTEGER",
    "ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS sector VARCHAR",
    "ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS industry VARCHAR",
    "ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS quote_type VARCHAR",
]
