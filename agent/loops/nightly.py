"""
The nightly research job (specs/EDGE_ACCELERATION_SPEC_v1.md Part 4).

No LLM call, no order, no playbook write. It runs on its own cron with its
own 300s function budget, so nothing it does can cost a trading loop time:

  1. FORWARD: price the gap field for every market session since the last
     one priced (at most CATCHUP_SESSIONS, so a missed night heals itself),
     and record those sessions' forward observations for gap-field rules.
  2. HISTORY: spend what is left of the budget on the gap-field backfill
     (resumable; ~2 years of every liquid gapper takes a few nights).
  3. SUMMARY: refresh the per-cell summary the weekly FDR batch reads.

A failure is logged as a `nightly` runs row and re-raised; the trading loops
never depend on this job having run.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, time as dtime, timedelta

from .. import gapfield, ledger, research
from ..config import Config
from ..failures import classify_failure, format_failure
from ..util import is_market_day, log, now_et, prev_market_day, today_et

FUNCTION_BUDGET_S = 300
RESERVE_S = 40              # left for the summary refresh, the runs row and the response
CATCHUP_SESSIONS = 5
SESSION_FINAL_ET = dtime(16, 15)   # a session's daily bar is final after the close


def _last_forward(con) -> date | None:
    try:
        row = con.execute("SELECT MAX(session_date) FROM gapfield_events "
                          "WHERE source = 'forward'").fetchone()
    except Exception:  # noqa: BLE001
        return None
    v = row[0] if row else None
    if v is None:
        return None
    return v.date() if isinstance(v, datetime) else (v if isinstance(v, date) else date.fromisoformat(str(v)[:10]))


def pending_sessions(con, now: datetime) -> list[date]:
    """Sessions to price forward, oldest first: every market session after the
    last one priced, up to the latest whose bar is final, at most
    CATCHUP_SESSIONS. The first run prices only the latest."""
    today = now.date()
    latest = today if (is_market_day(today) and now.time() >= SESSION_FINAL_ET) else prev_market_day(today)
    last = _last_forward(con)
    out, d = [], latest
    while len(out) < CATCHUP_SESSIONS and (last is None or d > last):
        if is_market_day(d):
            out.append(d)
            if last is None:
                break
        d -= timedelta(days=1)
    return sorted(out)


def run(cfg: Config, run_date: date | None = None, budget_s: float = FUNCTION_BUDGET_S) -> dict:
    from ..providers import MarketData
    t0 = time.monotonic()
    deadline = t0 + budget_s - RESERVE_S
    started = now_et().replace(tzinfo=None)
    d = run_date or today_et()
    con = ledger.connect()
    md = MarketData(cfg)
    out: dict = {"date": str(d), "forward": [], "backfill": None, "cells": None}
    stages: dict = {"timings_s": {}}
    try:
        now = now_et() if run_date is None else datetime.combine(d, dtime(23, 0))
        for s in pending_sessions(con, now):
            if time.monotonic() > deadline - 60:
                out["forward"].append({"session": str(s), "skipped": "budget"})
                break
            t = time.monotonic()
            r = gapfield.scan_session(con, md, cfg, s)
            obs = research.observe_session(con, s, lanes=("gapfield",))
            r["forward_observations"] = obs.get("recorded", 0)
            r["elapsed_s"] = round(time.monotonic() - t, 1)
            out["forward"].append(r)
            log("nightly", f"gap field {s}: {r.get('events', 0)} gap(s), {r.get('rows', 0)} row(s), "
                           f"{r['forward_observations']} forward observation(s)")
        stages["timings_s"]["forward"] = round(time.monotonic() - t0, 1)
        if time.monotonic() < deadline - 30:
            t = time.monotonic()
            out["backfill"] = gapfield.backfill_step(con, md, cfg, d, deadline - 20)
            stages["timings_s"]["backfill"] = round(time.monotonic() - t, 1)
            log("nightly", f"gap field backfill: {out['backfill']}")
        t = time.monotonic()
        out["cells"] = gapfield.refresh_cells(con, d)
        stages["timings_s"]["cells"] = round(time.monotonic() - t, 1)
        stages["timings_s"]["total"] = round(time.monotonic() - t0, 1)
        stages["forward_sessions"] = len([f for f in out["forward"] if "skipped" not in f])
        stages["backfill"] = (out["backfill"] or {}).get("status")
        ledger.log_run(con, "nightly", d, started, "ok", stages=stages)
        ledger.beat(con, "nightly", json.dumps(
            {"forward": [f.get("session") for f in out["forward"]],
             "backfill": (out["backfill"] or {}).get("status"), "cells": out["cells"]}))
        return out
    except Exception as e:
        ledger.log_run(con, "nightly", d, started, classify_failure(e),
                       error=format_failure(e), stages=stages)
        log("nightly", f"ERROR: {e}")
        raise
    finally:
        con.close()
