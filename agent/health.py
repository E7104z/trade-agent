"""
Learning-loop health: is each stage of the loop actually completing?

The product is the loop (morning plan -> evening grade -> weekly mine ->
playbook -> next morning), and every one of its stages can stop without
anything looking broken: a function killed at the 300s budget leaves no error
row, a wedged lock silently suppresses every retry, a cron that fires outside
its window returns a polite skip. Each failure mode has happened here. This
module reads the ledger and answers, per stage, "did it complete when it
should have?" -- deterministically, from tables the loops already write.

Read-only. Never raises: a health check that can take down the status
endpoint is worse than none.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from typing import Any

from .util import is_market_day, now_et, prev_market_day

OK_STATUSES = ("ok", "degraded")

# When each stage's output should exist for the day, ET. Cron fire times on
# the Hobby plan drift up to ~59 minutes past the schedule, and each run takes
# minutes, so these are deliberately generous: they mark "overdue", not "due".
MORNING_DUE = dtime(8, 30)
RUNNER_DUE = dtime(13, 0)
EVENING_DUE = dtime(18, 0)
WEEKLY_MAX_AGE_DAYS = 8
LEARNING_WINDOW_DAYS = 7


def _q(con, sql: str, params=None, default=None):
    try:
        return con.execute(sql, params or []).fetchall()
    except Exception:
        return default if default is not None else []


def _runs_by_loop(con) -> dict[str, list[dict]]:
    rows = _q(con, """SELECT loop, run_date, status, finished_at, error, cost_usd
                      FROM runs ORDER BY run_id DESC LIMIT 400""")
    out: dict[str, list[dict]] = {}
    for loop, run_date, status, finished_at, error, cost in rows:
        out.setdefault(loop, []).append({
            "run_date": run_date, "status": status, "finished_at": finished_at,
            "error": (error or "")[:300], "cost_usd": float(cost or 0)})
    return out


def _as_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _stage(key: str, label: str, status: str, headline: str,
           detail: str = "", last: dict | None = None) -> dict:
    return {"key": key, "label": label, "status": status, "headline": headline,
            "detail": detail,
            "last_status": (last or {}).get("status"),
            "last_date": str((last or {}).get("run_date") or "") or None,
            "last_error": (last or {}).get("error") or None}


def _expected_session(now: datetime, due: dtime) -> date:
    """The most recent market session whose output should exist by now."""
    today = now.date()
    if is_market_day(today) and now.time() >= due:
        return today
    return prev_market_day(today)


def _day_stage(key: str, label: str, runs: list[dict], expected: date,
               what: str) -> dict:
    """A once-per-session stage (morning plan / evening grade)."""
    last = runs[0] if runs else None
    for_session = [r for r in runs if _as_date(r["run_date"]) == expected]
    ok = next((r for r in for_session if r["status"] in OK_STATUSES), None)
    if ok:
        status = "warn" if ok["status"] == "degraded" else "ok"
        head = (f"{what} completed for {expected}"
                + (" (data degraded)" if ok["status"] == "degraded" else ""))
        return _stage(key, label, status, head, last=ok)
    skipped = next((r for r in for_session if r["status"] in ("skipped", "mock", "dry_run")), None)
    failed = next((r for r in for_session if r["status"] not in OK_STATUSES
                   and r["status"] not in ("skipped", "mock", "dry_run")), None)
    if failed:
        return _stage(key, label, "bad", f"{what} FAILED for {expected}",
                      failed.get("error") or failed["status"], last=failed)
    if skipped:
        return _stage(key, label, "warn", f"{what} was skipped for {expected}",
                      f"status={skipped['status']}", last=skipped)
    return _stage(key, label, "bad", f"No {what.lower()} recorded for {expected}",
                  "the scheduled run never reached the ledger -- a missed cron, "
                  "or a run killed by the function timeout before it logged",
                  last=last)


def loop_health(con: Any, cfg: Any, now: datetime | None = None) -> dict:
    try:
        return _loop_health(con, cfg, now or now_et())
    except Exception as e:  # noqa: BLE001
        return {"overall": "unknown", "stages": [], "signals": {},
                "warnings": [f"health check unavailable: {type(e).__name__}: {e}"]}


def _loop_health(con: Any, cfg: Any, now: datetime) -> dict:
    runs = _runs_by_loop(con)
    stages: list[dict] = []
    warnings: list[str] = []

    # --- 1. morning plan ------------------------------------------------
    m_session = _expected_session(now, MORNING_DUE)
    stages.append(_day_stage("morning", "Morning plan", runs.get("morning", []),
                             m_session, "Morning plan"))

    # --- 2. runner lane -------------------------------------------------
    rcfg = (cfg.get("runner", {}) or {}) if cfg else {}
    if rcfg.get("enabled"):
        r_session = _expected_session(now, RUNNER_DUE)
        row = _q(con, """SELECT universe_size, candidates_found, provider_degraded,
                                selection_made, selection_skipped_reason, gates_unavailable
                         FROM runner_sessions WHERE session_date = ?""", [r_session])
        last = (runs.get("runner") or [None])[0]
        if not row:
            stages.append(_stage("runner", "Runner scan", "bad",
                                 f"No runner scan recorded for {r_session}",
                                 "the runner cron never scanned inside the 10:20-15:00 ET "
                                 "window, or its tick was skipped", last=last))
        else:
            universe, found, degraded, selected, skip, gates = row[0]
            try:
                import json as _json
                gates = _json.loads(gates) if gates else {}
            except (TypeError, ValueError):
                gates = {}
            if degraded and gates.get("feed_downgraded") and gates.get("rvol_required"):
                stages.append(_stage(
                    "runner", "Runner scan", "bad",
                    f"Runner stood down for {r_session}: no real-time SIP feed",
                    "The Alpaca data plan refuses real-time SIP (only 15-minute-delayed "
                    "SIP), so the scan fell back to IEX, where RVOL is unreadable -- and "
                    "require_rvol stands the session down. Needs real-time SIP, or a "
                    "delayed-SIP scan, or require_rvol off.", last=last))
            elif degraded or not universe:
                stages.append(_stage("runner", "Runner scan", "bad",
                                     f"Runner scan DEGRADED for {r_session}",
                                     "the provider failed -- a zero-candidate day here is "
                                     "an outage, not a stand-down", last=last))
            elif not found:
                stages.append(_stage("runner", "Runner scan", "ok",
                                     f"Scanned {universe:,} names for {r_session}; "
                                     "no candidate qualified (stand-down)", last=last))
            else:
                sel = ("selection made" if selected
                       else f"no selection ({skip or 'decision tick not reached'})")
                status = "ok" if selected else "warn"
                detail = ""
                note_row = _q(con, "SELECT live_orders_note FROM runner_sessions "
                                   "WHERE session_date = ?", [r_session])
                note = (note_row[0][0] if note_row else None) or ""
                if note:
                    detail = f"live paper: {note[:300]}"
                    if note.startswith("placed 0/") and not note.startswith("placed 0/0"):
                        status = "bad"
                stages.append(_stage("runner", "Runner scan", status,
                                     f"{found} candidate(s) from {universe:,} names "
                                     f"for {r_session}; {sel}", detail, last=last))

    # --- 2b. intraday monitor (live paper) ---------------------------------
    if ((cfg.get("live_paper", {}) or {}) if cfg else {}).get("enabled"):
        from .ledger import last_beat
        hb = last_beat(con, "intraday_check")
        in_session = (is_market_day(now.date())
                      and dtime(9, 45) <= now.time() <= dtime(16, 0))
        if not hb or not hb.get("last_at"):
            stages.append(_stage(
                "intraday", "Intraday monitor", "bad",
                "The intraday monitor has never run",
                "Nothing calls /cron/intraday-check, so live paper time stops and the "
                "15:55 flatten are not enforced (positions can be held overnight)."))
        else:
            last_at = hb["last_at"]
            age_min = (now.replace(tzinfo=None) - last_at).total_seconds() / 60
            age_txt = (f"{age_min:.0f} min ago" if age_min < 120
                       else f"{age_min / 60:.1f} h ago" if age_min < 48 * 60
                       else f"{age_min / 1440:.0f} days ago")
            if in_session and age_min > 15:
                stages.append(_stage(
                    "intraday", "Intraday monitor", "bad",
                    f"Intraday monitor last ran {age_txt}",
                    "It should tick every 5 minutes during the session -- the trigger "
                    "machine is off or its task stopped."))
            elif not in_session and _as_date(last_at) != _expected_session(now, dtime(16, 0)) \
                    and is_market_day(_expected_session(now, dtime(16, 0))):
                stages.append(_stage(
                    "intraday", "Intraday monitor", "warn",
                    f"Intraday monitor did not run last session (last {age_txt})"))
            else:
                stages.append(_stage("intraday", "Intraday monitor", "ok",
                                     f"Intraday monitor ran {age_txt}"))

    # --- 3. evening grading ---------------------------------------------
    e_session = _expected_session(now, EVENING_DUE)
    stages.append(_day_stage("evening", "Evening grading", runs.get("evening", []),
                             e_session, "Evening grading"))

    # --- 4. weekly miner / playbook rewrite -----------------------------
    weekly = runs.get("weekly", [])
    last_ok = next((r for r in weekly if r["status"] in OK_STATUSES), None)
    last_w = weekly[0] if weekly else None
    if not last_ok:
        stages.append(_stage("weekly", "Weekly mining", "warn",
                             "The weekly miner has never completed on this ledger",
                             "the playbook has not been rewritten from evidence yet",
                             last=last_w))
    else:
        age = (now.date() - (_as_date(last_ok["run_date"]) or now.date())).days
        if age > WEEKLY_MAX_AGE_DAYS:
            detail = ""
            if last_w and last_w["status"] not in OK_STATUSES:
                detail = f"last attempt {last_w['run_date']}: {last_w['status']} {last_w.get('error') or ''}"
            stages.append(_stage("weekly", "Weekly mining", "bad",
                                 f"Last successful weekly mining was {age} days ago "
                                 f"({last_ok['run_date']})", detail, last=last_w))
        else:
            stages.append(_stage("weekly", "Weekly mining", "ok",
                                 f"Playbook mined {age} day(s) ago ({last_ok['run_date']})",
                                 last=last_ok))

    # --- wedged locks -----------------------------------------------------
    from .ledger import STALE_LOCK_MINUTES
    stale = _q(con, f"""SELECT trading_date, run_type FROM run_locks
                        WHERE status = 'in_progress'
                          AND started_at < current_timestamp - INTERVAL '{STALE_LOCK_MINUTES} minutes'
                        ORDER BY trading_date DESC LIMIT 10""")
    for td, rt in stale:
        warnings.append(f"{rt} lock for {td} is stuck 'in_progress' (its run died without "
                        f"releasing it). The next attempt takes it over automatically.")

    # --- live paper orders nobody is managing ---------------------------
    if ((cfg.get("live_paper", {}) or {}) if cfg else {}).get("enabled"):
        orphan = _q(con, """SELECT COUNT(*) FROM live_orders
                            WHERE exit_reason IS NULL AND session_date < ?""", [now.date()])
        n_orphan = int(orphan[0][0]) if orphan else 0
        if n_orphan:
            warnings.append(
                f"{n_orphan} live paper order(s) from past sessions have no recorded exit "
                "(no intraday tick reached them before the close). The next intraday tick "
                "or morning run settles them, closing any position still open.")

    # --- is anything actually being learned? ------------------------------
    since = now.date() - timedelta(days=LEARNING_WINDOW_DAYS)

    def _count(sql: str) -> int:
        rows = _q(con, sql, [since])
        try:
            return int(rows[0][0] or 0) if rows else 0
        except Exception:
            return 0

    signals = {
        "window_days": LEARNING_WINDOW_DAYS,
        "outcomes_graded": _count("SELECT COUNT(*) FROM outcomes WHERE eval_date >= ?"),
        "runner_candidates_priced": _count(
            "SELECT COUNT(*) FROM runner_candidate_outcomes WHERE eval_date >= ?"),
        "rejections_scored": _count(
            "SELECT COUNT(*) FROM rejections WHERE plan_date >= ? AND eod_outcome IS NOT NULL"),
        "lessons_created": _count("SELECT COUNT(*) FROM lessons WHERE created_date >= ?"),
        "lesson_confirmations": _count(
            "SELECT COUNT(*) FROM lesson_confirmations WHERE session_date >= ?"),
        "rule_confirmations": _count(
            "SELECT COUNT(*) FROM rule_confirmations WHERE session_date >= ?"),
        "calibration_rows": _count("SELECT COUNT(*) FROM calibration WHERE session_date >= ?"),
    }
    active = _q(con, "SELECT COUNT(*) FROM lessons WHERE status IN ('active', 'promoted')")
    signals["active_lessons"] = int(active[0][0] or 0) if active else 0
    graded_any = signals["outcomes_graded"] + signals["runner_candidates_priced"]
    learned_any = (signals["lessons_created"] + signals["lesson_confirmations"]
                   + signals["rule_confirmations"])
    if graded_any and not learned_any:
        warnings.append(
            f"{graded_any} outcome(s) were graded in the last {LEARNING_WINDOW_DAYS} days but "
            "no lesson was created or confirmed and no rule was confirmed -- grading is "
            "running without anything reaching the lesson bank.")

    order = {"bad": 3, "warn": 2, "unknown": 1, "ok": 0}
    worst = max((s["status"] for s in stages), key=lambda s: order.get(s, 0), default="ok")
    if warnings and order.get(worst, 0) < 2:
        worst = "warn"
    return {"overall": worst, "stages": stages, "signals": signals,
            "warnings": warnings, "as_of": now.replace(tzinfo=None).isoformat(timespec="seconds")}
