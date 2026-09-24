"""
Data coverage and the plumbing detector (DATA_CONTINUITY_SPEC_v1 Phase 3).

WHY THIS EXISTS
---------------
A no-trade day, a no-trigger day and a no-data day can all produce the same
quiet ledger. The morning loop (`session_degraded`) and the runner scan
(`provider_degraded`) already refuse to record an outage as a stand-down; the
evening grader did not. When every symbol's bars were missing, each graded row
said `exit_reason='no_data'` and the run was logged `ok`. When Alpaca's daily
bars were refused on every recent date and the fallback returned nothing, day
OHLC and prev close were null on every graded row for weeks, and nothing
noticed.

The directive's definition is the rule used here: SIMULTANEOUS nulls across
every ticker are plumbing, not signal. A single missing symbol is a halt or a
thin name; the same field missing on EVERY row is the pipe.

`PLUMBING_MIN_ROWS` is the one constant: below it, "every row" is too few rows
to distinguish a broken feed from two halted names. It is an operational
threshold, not a trading parameter, and it deliberately matches the smallest
count at which the runner scan's own majority-unavailable rule
(runner_scan: `v > scanned / 2`) starts meaning something about a session.
"""
from __future__ import annotations

from typing import Any, Iterable

__all__ = ["PLUMBING_MIN_ROWS", "OUTCOME_FIELDS", "RUNNER_FIELDS",
           "outcome_coverage", "plumbing"]

PLUMBING_MIN_ROWS = 5

# Grading inputs that a working pipeline fills on essentially every graded row.
# `minute_bars` is not a column: a row with exit_reason == 'no_data' is a row
# whose intraday bars never arrived.
OUTCOME_FIELDS = ("minute_bars", "day_open", "atr14", "rvol_at_1000",
                  "trend_side_200d", "stop_dist_atr")
RUNNER_FIELDS = ("minute_bars", "atr14", "trend_side_200d", "stop_dist_atr")


def _is_null(row: dict, field: str) -> bool:
    if field == "minute_bars":
        return row.get("exit_reason") == "no_data"
    return row.get(field) is None


def outcome_coverage(rows: Iterable[dict], lane: str, fields: Iterable[str],
                     source: str = "") -> list[dict]:
    """One coverage row per field: how many rows were graded, how many null."""
    rows = list(rows or [])
    out = []
    for f in fields:
        n_null = sum(1 for r in rows if _is_null(r, f))
        out.append({"lane": lane, "field": f, "n_total": len(rows), "n_null": n_null,
                    "source": source})
    return out


def plumbing(coverage: Iterable[dict]) -> list[dict]:
    """Coverage rows that are null on EVERY row, with enough rows to mean it."""
    return [c for c in coverage
            if int(c.get("n_total") or 0) >= PLUMBING_MIN_ROWS
            and int(c.get("n_null") or 0) == int(c.get("n_total") or 0)]


def describe(bad: Iterable[dict]) -> str:
    return "; ".join(f"{c['lane']}:{c['field']} null on all {c['n_total']} rows" for c in bad)


def lane_of(pick: dict[str, Any]) -> str:
    from .ledger import RUNNER_SETUP
    return "runner" if pick.get("setup_type") == RUNNER_SETUP else "morning"
