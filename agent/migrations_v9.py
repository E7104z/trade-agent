"""Schema v9 - structured kill reason + closed-enum no-trade classification
(specs/PARTICIPATION_UNBLOCK_SPEC_v2.md, Phase 1.1 and Phase 3).

`rejections.primary_kill_check` is the risk manager's own enum naming which
kill-check actually ended a candidate (staleness | chase | entry_type |
grounding | crowding | stop_quality | rr | regime_fit | correlation |
event_risk | conviction), so the exploration lane can admit a candidate
because R:R was its SOLE binding reason instead of parsing prose for it.
Old rows (pre-migration) keep this NULL; callers fall back to the existing
prose blocklist for those.

`daily_plans.no_trade_category` is a closed enum (all_failed_rr |
all_failed_regime | all_failed_validator | empty_universe | data_incomplete |
llm_error) recording WHY a no-trade day happened, distinct from the existing
free-text `plan["no_trade_reason"]` (the risk manager's own prose, which this
does not replace). `data_incomplete` always wins when data_health.degraded is
true -- a stand-down declared on degraded data is not a stand-down.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 9

STATEMENTS: list[str] = [
    "ALTER TABLE rejections ADD COLUMN IF NOT EXISTS primary_kill_check VARCHAR",
    "ALTER TABLE daily_plans ADD COLUMN IF NOT EXISTS no_trade_category VARCHAR",
]
