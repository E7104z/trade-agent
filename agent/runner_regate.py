"""
Re-gate scan_version 1 runner candidates under scan_version 2's gates.

INTEGRITY_REPAIR_SPEC_v1 Phase 4. The weekly search mines only rows produced
under the CURRENT scan_version, so on the day v2 shipped the runner corpus
dropped to zero. Most of the lost rows are not different evidence -- they are
the same candidates, scanned by the same detection logic, from before three
row-REMOVING gates existed. v2 (runner_scan.SCAN_VERSION docstring):

  1. require_rvol        -- a candidate with no readable RVOL is rejected
  2. LANE_EXCLUDED_TICKERS -- crypto spot/futures/trust products excluded
  3. feed downgrade      -- with RVOL required, a downgraded session produces
                            no candidates at all

`git diff 1a8a618 470a8d3 -- agent/runner_scan.py agent/runner_universe.py`
shows that these are the ONLY detection differences between v1 and v2. So a
v1 row whose STORED inputs pass all three is exactly a row v2 would have
admitted, and relabelling it is re-derivation from this system's own data,
not inheritance. A v1 row that fails any of them stays v1 and is never mined.

This must be revisited before SCAN_VERSION moves past 2: a v3 that changes
detection itself (not just removes rows) cannot be reproduced from stored
inputs. `REGATE_TO` is pinned to SCAN_VERSION by a test so that bump fails
loudly here.

Explicit and opt-in, like `regrade`: a dry run by default, `--apply` to write.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from .runner_scan import LANE_EXCLUDED_TICKERS

__all__ = ["REGATE_FROM", "REGATE_TO", "v2_admits", "plan", "apply"]

REGATE_FROM = 1
REGATE_TO = 2


def v2_admits(row: dict, feed_downgraded: bool,
              require_rvol: bool = True) -> tuple[bool, str]:
    """Would scan_version 2 have admitted this stored v1 candidate?

    Mirrors the three v2 gates on the same inputs the live gate chain reads:
    `runner_scan.evaluate_ticker` rejects on `not prev_volume or rv is None`
    when require_rvol is on, on `ticker in extra_excluded`, and `scan` stands
    a downgraded session down before any ticker is evaluated.
    """
    if require_rvol and feed_downgraded:
        return False, "session feed downgraded (v2 stands the session down)"
    ticker = str(row.get("ticker") or "").upper()
    if ticker in LANE_EXCLUDED_TICKERS:
        return False, "lane-excluded ticker (crypto product)"
    if require_rvol and (not row.get("prev_volume") or row.get("rvol") is None):
        return False, "RVOL unreadable (v2 require_rvol)"
    return True, ""


def _session_downgraded(con: Any, d: date) -> bool:
    row = con.execute(
        "SELECT gates_unavailable FROM runner_sessions WHERE session_date = ?", [d]
    ).fetchone()
    if not row or not row[0]:
        return False
    try:
        gates = json.loads(row[0])
    except (TypeError, ValueError):
        return False
    return bool(gates.get("feed_downgraded"))


def plan(con: Any, from_date: date, require_rvol: bool = True) -> dict:
    """Which v1 rows on/after `from_date` pass the v2 gates. Reads only."""
    rows = con.execute(
        """SELECT session_date, ticker, rvol, prev_volume
           FROM runner_candidates
           WHERE session_date >= ? AND scan_version = ?
           ORDER BY session_date, ticker""",
        [from_date, REGATE_FROM],
    ).fetchall()
    downgraded: dict[date, bool] = {}
    admit: list[tuple[date, str]] = []
    reject: dict[str, int] = {}
    for d, ticker, rvol, prev_volume in rows:
        if d not in downgraded:
            downgraded[d] = _session_downgraded(con, d)
        ok, why = v2_admits({"ticker": ticker, "rvol": rvol, "prev_volume": prev_volume},
                            downgraded[d], require_rvol)
        if ok:
            admit.append((d, ticker))
        else:
            reject[why] = reject.get(why, 0) + 1
    return {"checked": len(rows), "admit": admit, "rejected_by_reason": reject,
            "sessions": sorted({d for d, _ in admit})}


def apply(con: Any, admitted: list[tuple[date, str]]) -> int:
    """Relabel the admitted rows scan_version 2 with regated_from = 1."""
    n = 0
    for d, ticker in admitted:
        con.execute(
            """UPDATE runner_candidates SET scan_version = ?, regated_from = ?
               WHERE session_date = ? AND ticker = ? AND scan_version = ?""",
            [REGATE_TO, REGATE_FROM, d, ticker, REGATE_FROM],
        )
        n += 1
    return n
