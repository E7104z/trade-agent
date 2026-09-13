"""
The intraday Runner tick (RUNNER_PORT_SPEC_v1 1.4).

Called from `POST /cron/intraday-check`, which also runs the live position
monitor. Two rules govern everything in this file:

  1. This lane must never be able to break the live position monitor. The
     caller wraps `tick()` in try/except, and `tick()` additionally refuses to
     raise for any data-shaped reason of its own.

  2. The cron schedule is not a correctness mechanism. A cron can fire early,
     late, twice, or on a holiday the schedule does not know about, so `tick()`
     re-checks market day and the scan window itself and returns a skip reason
     rather than trusting when it was called.
"""
from __future__ import annotations

import time
from datetime import date, datetime, time as dtime

from .. import ledger
from ..config import Config
from ..providers import MarketData
from ..util import ET, is_market_day, log, market_close_et, now_et

__all__ = ["tick"]


def _parse_clock(value, default: dtime) -> dtime:
    """"HH:MM" -> time. A malformed value falls back rather than raising -
    a typo in config must not take down the intraday cron."""
    try:
        hh, mm = (int(x) for x in str(value).split(":"))
        return dtime(hh, mm)
    except Exception:
        log("runner", f"WARN could not parse clock {value!r}; using {default}")
        return default


def tick(cfg: Config, *, at: datetime | None = None, md: MarketData | None = None,
         con=None) -> dict:
    """Run one scan tick, or return why it was skipped.

    `at`, `md` and `con` are injectable so a forced local run and the tests can
    drive this without a clock, a network, or the production database.
    """
    started = time.monotonic()
    rcfg = cfg.get("runner", {}) or {}

    if not rcfg.get("enabled"):
        return {"skipped": "runner.enabled is false"}

    at = at or now_et()
    d: date = at.date()

    if not is_market_day(d):
        return {"skipped": f"{d} is not a market day"}

    start_t = _parse_clock(rcfg.get("scan_start_et", "09:45"), dtime(9, 45))
    end_t = _parse_clock(rcfg.get("scan_end_et", "15:00"), dtime(15, 0))

    # On a half day the session ends at 13:00 ET; a 15:00 scan window would
    # otherwise keep scanning a closed market and bank rejections that describe
    # nothing. Clamp to the real close.
    close_t = market_close_et(d)
    if end_t > close_t:
        end_t = close_t

    now_t = at.timetz().replace(tzinfo=None)
    if now_t < start_t:
        return {"skipped": f"before scan_start_et ({now_t:%H:%M} < {start_t:%H:%M})"}
    if now_t > end_t:
        return {"skipped": f"after scan_end_et ({now_t:%H:%M} > {end_t:%H:%M})"}

    owns_con = con is None
    if owns_con:
        con = ledger.connect()
    try:
        md = md or MarketData(cfg)
        if md.alpaca is None:
            # No market data provider is a pipeline failure, not a quiet day.
            ledger.upsert_runner_session(con, {
                "session_date": d, "universe_size": 0, "candidates_found": 0,
                "provider_degraded": True,
            })
            return {"skipped": "no Alpaca credentials", "provider_degraded": True}

        from .. import runner_scan
        # scan_end_et_minutes bounds the session window used for the condition
        # check, so an afternoon tick cannot read post-window bars into a
        # detection snapshot.
        scfg = dict(rcfg)
        scfg["scan_end_et_minutes"] = end_t.hour * 60 + end_t.minute
        result = runner_scan.scan(md, scfg, at=at, con=con)

        # -- Phase 2: the decision tick ----------------------------------
        # Runs only when selection is switched on AND we are at or past the
        # decision time. Guarded inside by a one-per-session database claim.
        if rcfg.get("selection_enabled"):
            decision_t = _parse_clock(rcfg.get("decision_time_et", "10:30"), dtime(10, 30))
            if now_t >= decision_t:
                try:
                    from .. import runner_selection
                    result["selection"] = runner_selection.run_selection(
                        cfg, con=con, session_date=d, md=md, at=at)
                except Exception as e:
                    # A failed selection is a no-trade session, never a
                    # fallback to some heuristic - a silent heuristic would
                    # contaminate the very baseline-vs-selection comparison
                    # this design exists to produce.
                    log("runner", f"WARN selection failed; no orders placed: {e}")
                    ledger.upsert_runner_session(con, {
                        "session_date": d,
                        "selection_skipped_reason": f"selection_failed: {e}"[:500],
                    })
                    result["selection"] = {"error": str(e)}

        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return result
    finally:
        if owns_con:
            con.close()
