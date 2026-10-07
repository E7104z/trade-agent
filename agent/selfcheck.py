"""
The agent audits its own production data every evening
(specs/EDGE_ACCELERATION_SPEC_v1.md Part 3).

WHY THIS MODULE EXISTS
----------------------
Every spec since the standing directive has carried a "Phase 0: verify against
production" step, and every one of them was blocked: the only reader of the
production ledger was a person pasting /api/table JSON into a chat. So the
checks that decide whether a lane works -- does the backlog probe ever fire,
is the broker cap clipping orders, is the morning near its 300s budget, are
hypothesis tags written before outcomes -- went unrun, or ran weeks late.

The questions are SQL over tables the evening loop already writes. This module
asks them where the data lives, every evening, and:
  * stores each answer (`self_checks`, one row per check per session),
  * prints them at the end of the evening email,
  * surfaces anything 'warn' or 'bad' on the dashboard's health panel.

It changes nothing about trading. A failed check never breaks the evening
(every reading is guarded), and a missing table reads as zero, not an error.

STATUSES
--------
  ok    the property holds
  info  a reading with no pass/fail (the pipeline's counts)
  warn  worth a look; the system kept working
  bad   an integrity property is broken (e.g. a hypothesis tag dated before
        its idea opened) or a day's data did not arrive
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, timedelta
from typing import Any, Callable

__all__ = ["CHECKS", "run", "text", "latest", "MORNING_WARN_S"]

RUNNER = "dto_runner"
LOOKBACK_SESSIONS = 10
MORNING_WARN_S = 270          # of the 300s function budget
PERSONA_SESSIONS = 3          # the persona spec's descriptive check
_ORDER = {"bad": 3, "warn": 2, "info": 1, "ok": 0}


def _q(con: Any, sql: str, params: list | None = None) -> list[tuple]:
    try:
        return con.execute(sql, params or []).fetchall()
    except Exception:  # noqa: BLE001 -- a missing table/column is an empty reading
        return []


def _one(con: Any, sql: str, params: list | None = None) -> int:
    rows = _q(con, sql, params)
    try:
        return int(rows[0][0] or 0) if rows else 0
    except (TypeError, ValueError):
        return 0


def _cfg(cfg: Any, key: str) -> dict:
    try:
        return (cfg.get(key, {}) or {}) if cfg is not None else {}
    except Exception:  # noqa: BLE001
        return {}


def _sessions(d: date, n: int) -> list[date]:
    """The n most recent market sessions up to and including d, oldest first."""
    from .util import is_market_day
    out, x = [], d
    while len(out) < n and x > d - timedelta(days=n * 3 + 10):
        if is_market_day(x):
            out.append(x)
        x -= timedelta(days=1)
    return sorted(out)


def _as_date(v: Any) -> date | None:
    if v is None:
        return None
    if hasattr(v, "date") and not isinstance(v, date):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _res(status: str, headline: str, **value: Any) -> dict:
    return {"status": status, "headline": headline, "value": value}


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------

def check_plumbing(con: Any, cfg: Any, d: date) -> dict:
    """Did today's data arrive in every lane that should have produced it?"""
    picks = _one(con, f"""SELECT COUNT(*) FROM picks WHERE plan_date = ?
                          AND COALESCE(setup_type, '') <> '{RUNNER}'""", [d])
    graded = _one(con, f"""SELECT COUNT(*) FROM picks p JOIN outcomes o ON o.pick_id = p.pick_id
                           WHERE p.plan_date = ? AND COALESCE(p.setup_type, '') <> '{RUNNER}'""", [d])
    triggered = _one(con, f"""SELECT COUNT(*) FROM picks p JOIN outcomes o ON o.pick_id = p.pick_id
                              WHERE p.plan_date = ? AND o.triggered
                                AND COALESCE(p.setup_type, '') <> '{RUNNER}'""", [d])
    rejections = _one(con, "SELECT COUNT(*) FROM rejections WHERE plan_date = ?", [d])
    priced = _one(con, "SELECT COUNT(*) FROM rejections WHERE plan_date = ? AND forgone_r IS NOT NULL", [d])
    pop_u = _one(con, "SELECT COUNT(*) FROM population_universe WHERE plan_date = ?", [d])
    pop_o = _one(con, """SELECT COUNT(*) FROM population_outcomes
                         WHERE plan_date = ? AND r_multiple IS NOT NULL""", [d])
    run_c = _one(con, "SELECT COUNT(*) FROM runner_candidates WHERE session_date = ?", [d])
    run_o = _one(con, "SELECT COUNT(*) FROM runner_candidate_outcomes WHERE session_date = ?", [d])
    fwd = dict(_q(con, """SELECT COALESCE(source, 'taken'), COUNT(*) FROM research_observations
                          WHERE session_date = ? GROUP BY 1""", [d]))
    problems = []
    if picks and not graded:
        problems.append(f"{picks} morning pick(s) and none graded")
    if _cfg(cfg, "population").get("enabled") and pop_u and not pop_o:
        problems.append(f"{pop_u} gappers snapshotted and none graded")
    if _cfg(cfg, "runner").get("enabled") and run_c and not run_o:
        problems.append(f"{run_c} runner candidates and none priced")
    status = "bad" if problems else "ok"
    head = ("; ".join(problems) if problems else
            f"morning {graded}/{picks} graded ({triggered} triggered), shadow {priced}/{rejections} "
            f"priced, population {pop_o} rows, runner {run_o}/{run_c} priced, forward "
            f"observations {sum(int(v) for v in fwd.values())}")
    return _res(status, head, picks=picks, graded=graded, triggered=triggered,
                rejections=rejections, rejections_priced=priced, population_universe=pop_u,
                population_graded=pop_o, runner_candidates=run_c, runner_priced=run_o,
                forward_observations={str(k): int(v) for k, v in fwd.items()})


def check_lanes(con: Any, cfg: Any, d: date) -> dict:
    """Which lanes actually traded, does the backlog probe ever fire, and does
    the broker cap clip the plan?"""
    days = _sessions(d, LOOKBACK_SESSIONS)
    marks = ",".join("?" for _ in days)
    rows = _q(con, f"""SELECT plan_date, COALESCE(exploration_lane, 'approved'), COUNT(*)
                       FROM picks WHERE plan_date IN ({marks})
                         AND COALESCE(setup_type, '') <> '{RUNNER}'
                       GROUP BY 1, 2""", list(days))
    by_lane: Counter = Counter()
    per_day: Counter = Counter()
    for pd, lane, n in rows:
        by_lane[str(lane)] += int(n)
        per_day[_as_date(pd)] += int(n)
    planned = [_as_date(r[0]) for r in _q(con, f"SELECT plan_date FROM daily_plans WHERE plan_date IN ({marks})", list(days))]
    # A probe-tagged candidate can reach the tape as an approval or as
    # coverage, which collects the same observation under another label; only
    # one that reached it by no lane at all is a probe lost.
    lane_of_pick: dict = {}
    for pd, t, lane in _q(con, f"""SELECT plan_date, ticker, COALESCE(exploration_lane, 'approved')
                                   FROM picks WHERE plan_date IN ({marks})
                                     AND COALESCE(setup_type, '') <> '{RUNNER}'""", list(days)):
        lane_of_pick[(_as_date(pd), str(t).upper())] = str(lane)
    tagged = 0
    probes_traded: Counter = Counter()
    for pd, aj in _q(con, f"SELECT plan_date, analyst_json FROM daily_plans WHERE plan_date IN ({marks})", list(days)):
        try:
            for c in (json.loads(aj or "{}") or {}).get("candidates") or []:
                if not c.get("probe_for"):
                    continue
                tagged += 1
                lane = lane_of_pick.get((_as_date(pd), str(c.get("ticker", "")).upper()))
                if lane:
                    probes_traded[lane] += 1
        except (TypeError, ValueError, AttributeError):
            continue
    cap = int(_cfg(cfg, "live_paper").get("max_picks_per_day", 4) or 4)
    clipped = {str(k): v for k, v in per_day.items() if v > cap}
    notes, status = [], "ok"
    n_probe_traded = sum(probes_traded.values())
    if _cfg(cfg, "exploration_lane").get("backlog_probe") and len(planned) >= 5 \
            and not by_lane.get("backlog_probe") and not n_probe_traded:
        status = "warn"
        notes.append(
            f"backlog_probe has not fired in {len(planned)} sessions: "
            + ("the analyst tagged no candidate `probe_for`" if not tagged else
               f"the analyst tagged {tagged} candidate(s) `probe_for` and none reached the tape "
               "(each killed on a broken order or below the lane's R:R floor)"))
    if clipped:
        status = "warn"
        notes.append(f"broker cap {cap} clipped the plan on {len(clipped)} day(s) "
                     f"(max {max(clipped.values())} picks); the sim still grades them all")
    lane_txt = ", ".join(f"{k} {v}" for k, v in sorted(by_lane.items(), key=lambda kv: -kv[1]))
    if n_probe_traded:
        probe_txt = (f"; probes tagged {tagged}, traded {n_probe_traded} ("
                     + ", ".join(f"{k} {v}" for k, v in sorted(probes_traded.items())) + ")")
    else:
        probe_txt = f"; probes tagged {tagged}, none traded" if tagged else ""
    head = "; ".join(notes) if notes else \
        f"last {len(planned)} sessions: {lane_txt or 'no picks'}{probe_txt}"
    return _res(status, head, sessions=len(planned), by_lane=dict(by_lane),
                probe_tagged_candidates=tagged, probes_traded=dict(probes_traded), broker_cap=cap,
                max_picks_in_a_day=max(per_day.values()) if per_day else 0, clipped_days=clipped)


def check_morning_budget(con: Any, cfg: Any, d: date) -> dict:
    """How close the morning runs to its 300s function budget, and which
    sessions have no morning run on record at all (a kill at 300s logs nothing)."""
    days = _sessions(d, LOOKBACK_SESSIONS)
    rows = _q(con, """SELECT run_date, status, stages FROM runs WHERE loop = 'morning'
                      AND run_date >= ? ORDER BY run_id""", [days[0]] if days else [d])
    totals: dict[str, float] = {}
    slowest: dict[str, str] = {}
    seen: set = set()
    for rd, _status, stages in rows:
        seen.add(_as_date(rd))
        try:
            t = (json.loads(stages or "{}") or {}).get("timings_s") or {}
        except (TypeError, ValueError):
            continue
        if t.get("total") is not None:
            totals[str(rd)] = float(t["total"])
            stage = max(((k, v) for k, v in t.items() if k != "total" and isinstance(v, (int, float))),
                        key=lambda kv: kv[1], default=(None, None))
            slowest[str(rd)] = f"{stage[0]} {stage[1]}s" if stage[0] else ""
    missing = [str(x) for x in days if x not in seen and x < d]
    # A morning killed at the budget AFTER saving its plan leaves a runs row
    # but never releases its lock (2026-10-05); a bounded arm-B wait records
    # the abandonment instead.
    abandoned = []
    for rd, _status, stages in rows:
        try:
            if (json.loads(stages or "{}") or {}).get("arm_b") == "abandoned_at_budget":
                abandoned.append(str(rd))
        except (TypeError, ValueError):
            continue
    stuck = [str(_as_date(r[0])) for r in _q(
        con, """SELECT trading_date FROM run_locks WHERE run_type = 'morning'
                AND status = 'in_progress' AND trading_date >= ? AND trading_date < ?""",
        [days[0] if days else d, d])]
    # The two-invocation morning (schema v35) says which half died.
    halves: dict[str, list[str]] = {}
    for td, stage, arm_b in _q(
            con, """SELECT trading_date, stage, arm_b_status FROM morning_checkpoints
                    WHERE trading_date >= ? AND trading_date <= ?""", [days[0] if days else d, d]):
        why = {"analyst_done": "risk half never started (hand-off and safety-net cron both missed)",
               "risk_running": "risk half died mid-run",
               "failed": "risk half failed"}.get(str(stage))
        if why:
            halves.setdefault(why, []).append(str(_as_date(td)))
        if str(arm_b or "") == "running":
            abandoned.append(f"{_as_date(td)} (killed mid-run)")
    vals = sorted(totals.values())
    med = vals[len(vals) // 2] if vals else None
    worst = max(totals.items(), key=lambda kv: kv[1]) if totals else None
    status, notes = "ok", []
    if worst and worst[1] >= MORNING_WARN_S:
        status = "warn"
        notes.append(f"{worst[0]} took {worst[1]:.0f}s of 300s ({slowest.get(worst[0])})")
    if missing:
        status = "warn"
        notes.append(f"no morning run recorded on {', '.join(missing)} (a 300s kill logs nothing)")
    if stuck:
        status = "warn"
        notes.append(f"morning run lock never released on {', '.join(sorted(set(stuck)))} "
                     "(a run killed at the 300s limit)")
    for why, dates in sorted(halves.items()):
        status = "warn"
        notes.append(f"{why} on {', '.join(sorted(dates))}")
    if abandoned:
        status = "warn"
        notes.append(f"paired arm B abandoned at the budget on {', '.join(abandoned)} "
                     "(no paired comparison those sessions)")
    head = "; ".join(notes) if notes else (
        f"median {med:.0f}s, max {worst[1]:.0f}s of 300s over {len(vals)} run(s)" if worst
        else "no timed morning run in the window")
    return _res(status, head, median_s=med, max_s=worst[1] if worst else None,
                runs=len(vals), missing_sessions=missing, slowest=slowest,
                killed_sessions=stuck, arm_b_abandoned=abandoned, halves=halves)


def check_entries(con: Any, cfg: Any, d: date) -> dict:
    """Do the morning's orders fill, and what did the validator change? On
    10/06 not one of the plan's limits was reached: the day produced no
    trade and no observation. Counts the limit/breakout fill rate over the
    lookback, the entries moved onto the computed level their thesis named
    (and how those filled), and approvals the validator dropped, by kind."""
    days = _sessions(d, LOOKBACK_SESSIONS)
    marks = ",".join("?" for _ in days)
    rows = _q(con, f"""SELECT p.plan_date, p.ticker, o.triggered FROM picks p
                       JOIN outcomes o ON o.pick_id = p.pick_id
                       WHERE p.plan_date IN ({marks}) AND p.entry_type IN ('limit', 'breakout')
                         AND COALESCE(p.setup_type, '') <> '{RUNNER}'""", list(days))
    filled = {(_as_date(pd), str(t).upper()): bool(tr) for pd, t, tr in rows}
    n, n_fill = len(filled), sum(filled.values())
    fixes = fix_fill = 0
    drops: Counter = Counter()
    for pd, pj in _q(con, f"SELECT plan_date, plan_json FROM daily_plans WHERE plan_date IN ({marks})",
                     list(days)):
        try:
            plan = json.loads(pj or "{}") or {}
        except (TypeError, ValueError):
            continue
        for p in plan.get("picks") or []:
            if p.get("level_fix"):
                fixes += 1
                fix_fill += int(filled.get((_as_date(pd), str(p.get("ticker", "")).upper()), False))
        for x in plan.get("validator_drops") or []:
            drops[str(x.get("kind"))] += 1
    rate = f"{100 * n_fill / n:.0f}%" if n else "n/a"
    head = (f"last {len(days)} sessions: {n_fill} of {n} limit/breakout orders filled ({rate}); "
            f"{fixes} entr{'y' if fixes == 1 else 'ies'} moved onto the computed level their thesis "
            f"named ({fix_fill} filled); approvals dropped by the validator: "
            + (", ".join(f"{k} {v}" for k, v in sorted(drops.items())) or "none"))
    status = "warn" if n >= 8 and n_fill * 2 < n else "ok"
    if status == "warn":
        head = "fewer than half the orders filled — " + head
    return _res(status, head, orders=n, filled=n_fill, level_fixes=fixes,
                level_fixes_filled=fix_fill, validator_drops=dict(drops))


def check_runner_selection(con: Any, cfg: Any, d: date) -> dict:
    """Did the runner lane make its one selection today? A claim whose
    request died left 10/05 with 533 candidates, 0 picks and no reason."""
    rcfg = _cfg(cfg, "runner")
    if not (rcfg.get("enabled") and rcfg.get("selection_enabled")):
        return _res("info", "runner selection off")
    from .ledger import runner_selection_note
    n_cand = _one(con, "SELECT COUNT(*) FROM runner_candidates WHERE session_date = ?", [d])
    try:
        note = runner_selection_note(con, d)
    except Exception as e:  # noqa: BLE001
        return _res("warn", f"selection state unreadable ({type(e).__name__})")
    if n_cand and note["state"] in ("unfinished", "not_run"):
        status = "warn"
    else:
        status = "ok" if note["state"] == "decided" else "info"
    return _res(status, f"{n_cand} candidate(s): {note['text']}", candidates=n_cand, **note)


def check_grader_audit(con: Any, cfg: Any, d: date) -> dict:
    """What the evening audit (agent/review_audit.py) caught in today's grader
    output: claims the computed numbers contradict, before they were saved."""
    rows = _q(con, """SELECT stages FROM runs WHERE loop = 'evening' AND run_date = ?
                      AND stages IS NOT NULL ORDER BY run_id DESC LIMIT 1""", [d])
    try:
        audit = (json.loads(rows[0][0] or "{}") or {}).get("audit") if rows else None
    except (TypeError, ValueError):
        audit = None
    if audit is None:
        return _res("info", "no audited evening review for this session")
    n = int(audit.get("findings") or 0)
    if not n:
        return _res("ok", "every checked claim agreed with the computed numbers", **audit)
    kinds = Counter(str(i.get("check")) for i in audit.get("items") or [])
    return _res("warn", f"{n} grader claim(s) contradicted the numbers "
                        f"({', '.join(f'{k} {v}' for k, v in sorted(kinds.items()))}); "
                        f"{audit.get('dropped_lessons', 0)} lesson(s) and "
                        f"{audit.get('dropped_ideas', 0)} idea(s) dropped before saving", **audit)


def check_hypotheses(con: Any, cfg: Any, d: date) -> dict:
    """The notebook's integrity property (every tag written before its idea's
    outcome can exist) and whether the lane is being used at all."""
    if not _cfg(cfg, "hypothesis_lane").get("enabled"):
        return _res("info", "hypothesis lane off")
    early = _one(con, """SELECT COUNT(*) FROM picks p JOIN trader_hypotheses h
                         ON h.hyp_id = p.hypothesis_id WHERE p.plan_date < h.opened_at""")
    states = dict(_q(con, "SELECT state, COUNT(*) FROM trader_hypotheses GROUP BY 1"))
    sources = dict(_q(con, "SELECT source, COUNT(*) FROM trader_hypotheses GROUP BY 1"))
    days = _sessions(d, 5)
    marks = ",".join("?" for _ in days)
    lane = _one(con, f"""SELECT COUNT(*) FROM picks WHERE plan_date IN ({marks})
                         AND exploration_lane = 'hypothesis'""", list(days))
    tagged = _one(con, f"""SELECT COUNT(*) FROM picks WHERE plan_date IN ({marks})
                           AND hypothesis_id IS NOT NULL""", list(days))
    since_on = _one(con, "SELECT COUNT(*) FROM daily_plans WHERE plan_date >= ?", [date(2026, 10, 5)])
    total = sum(int(v) for v in states.values())
    # What the analyst actually wrote, so "not using the notebook" is told
    # apart from "writing ideas the register refuses".
    proposed = cand_tagged = 0
    for (aj,) in _q(con, f"SELECT analyst_json FROM daily_plans WHERE plan_date IN ({marks})",
                    list(days)):
        try:
            a = json.loads(aj or "{}") or {}
        except (TypeError, ValueError):
            continue
        proposed += len(a.get("new_hypotheses") or [])
        cand_tagged += sum(1 for c in a.get("candidates") or [] if c.get("hypothesis_id"))
    rejected = _one(con, "SELECT COUNT(*) FROM trader_hypotheses WHERE state = 'REJECTED'")
    if early:
        return _res("bad", f"{early} pick(s) tagged to an idea BEFORE the idea opened: a tag "
                           "that could see its outcome", early_tags=early)
    status = "ok"
    head = (f"{total} idea(s) ({', '.join(f'{k} {v}' for k, v in sorted(states.items()))}; "
            f"by {', '.join(f'{k} {v}' for k, v in sorted(sources.items()))}); last 5 sessions: "
            f"the analyst wrote {proposed} idea(s) and tagged {cand_tagged} candidate(s); "
            f"{tagged} tagged pick(s), {lane} lane pick(s); every tag post-dates its idea")
    if since_on >= 3 and not (total - rejected):
        status = "warn"
        head = (f"lane on for {since_on} sessions and no idea is in the notebook: "
                + (f"the analyst wrote {proposed} and the register refused them ({rejected} "
                   "REJECTED)" if proposed or rejected else
                   "the analyst and grader are not writing ideas down"))
    return _res(status, head, states={str(k): int(v) for k, v in states.items()},
                sources={str(k): int(v) for k, v in sources.items()}, lane_picks_5=lane,
                tagged_picks_5=tagged, early_tags=early, analyst_proposed_5=proposed,
                candidates_tagged_5=cand_tagged, rejected=rejected)


def check_lessons(con: Any, cfg: Any, d: date) -> dict:
    """How many lessons steer the analyst without ever having been tested."""
    active = _one(con, "SELECT COUNT(*) FROM lessons WHERE status IN ('active', 'promoted')")
    untested = _one(con, """SELECT COUNT(*) FROM lessons WHERE status IN ('active', 'promoted')
                            AND COALESCE(confirmations, 0) = 0 AND COALESCE(contradictions, 0) = 0""")
    with_pred = _one(con, """SELECT COUNT(*) FROM lessons WHERE status IN ('active', 'promoted')
                             AND predicate_json IS NOT NULL""")
    nominated = _one(con, "SELECT COUNT(DISTINCT from_lesson_id) FROM trader_hypotheses "
                          "WHERE from_lesson_id IS NOT NULL")
    head = (f"{active} active lesson(s): {untested} never confirmed or contradicted, {with_pred} "
            f"carry a predicate, {nominated} nominated for a deliberate test")
    return _res("info", head, active=active, untested=untested, with_predicate=with_pred,
                nominated=nominated)


def check_edge_pipeline(con: Any, cfg: Any, d: date) -> dict:
    """Where the rule registry stands: what is in forward test, how close the
    nearest rules are to a decision, and whether any rule is starved."""
    states = dict(_q(con, "SELECT state, COUNT(*) FROM research_rules GROUP BY 1"))
    sources = dict(_q(con, """SELECT source, COUNT(*) FROM research_rules
                              WHERE state IN ('QUARANTINED', 'VALIDATED') GROUP BY 1"""))
    top = _q(con, """SELECT rule_id, lane, forward_n, forward_sum_r, alpha_spent, looks_done
                     FROM research_rules WHERE state = 'QUARANTINED'
                     ORDER BY forward_n DESC LIMIT 3""")
    starved = _one(con, """SELECT COUNT(*) FROM research_rules WHERE state = 'QUARANTINED'
                           AND quarantined_at <= ? AND COALESCE(forward_n, 0) = 0""",
                   [d - timedelta(days=14)])
    lead = [f"{rid} ({lane}) n={int(n or 0)} mean {((s or 0) / n):+.2f}R, {int(lk or 0)} look(s)"
            for rid, lane, n, s, _a, lk in top if n]
    q = int(states.get("QUARANTINED", 0) or 0)
    v = int(states.get("VALIDATED", 0) or 0)
    head = (f"{v} VALIDATED, {q} in forward test"
            + (f" (by {', '.join(f'{k} {n}' for k, n in sorted(sources.items()))})" if sources else "")
            + (f"; nearest: {'; '.join(lead)}" if lead else ""))
    status = "info"
    if starved:
        status = "warn"
        head += (f"; {starved} rule(s) quarantined 2+ weeks with no forward observation "
                 "(their cell is not being traded or priced)")
    return _res(status, head, states={str(k): int(n) for k, n in states.items()},
                sources={str(k): int(n) for k, n in sources.items()}, starved=starved)


def check_persona(con: Any, cfg: Any, d: date) -> dict:
    """The persona spec's descriptive check, run by the system once three
    persona sessions exist. Counts only; the hypothesis lane's own picks are
    shown apart so they are not read as the persona's."""
    if not _cfg(cfg, "persona").get("enabled"):
        return _res("info", "persona off")
    on = _one(con, "SELECT COUNT(*) FROM daily_plans WHERE persona_version IS NOT NULL")
    if on < PERSONA_SESSIONS:
        return _res("info", f"{on} of {PERSONA_SESSIONS} persona sessions recorded; the "
                            "descriptive check prints here once there are three", sessions=on)
    from . import persona_monitor as pm
    groups = pm.collect(con, None, since=pm.FLOOR_LIVE)

    def line(label: str) -> str:
        g = groups.get(label) or {}
        n = g.get("sessions") or 0
        if not n:
            return f"{label}: no sessions since {pm.FLOOR_LIVE}"
        per = lambda k: f"{(g.get(k) or 0) / n:.1f}"  # noqa: E731
        size = g.get("mean_size_r")
        return (f"{label}: {n} session(s), picks/session {per('picks')} "
                f"({per('picks_ex_hypothesis_lane')} excluding the hypothesis lane), "
                f"triggered/session {per('triggered_ex_hypothesis_lane')} excl. lane, mean size "
                f"{'n/a' if size is None else f'{size:.2f}R'}, pick conviction "
                f"{pm._hist(g.get('pick_conviction') or {})}")
    labels = sorted(groups, key=lambda s: (s != "persona off", s))
    head = " | ".join(line(lb) for lb in labels) + " (descriptive: different weeks, different tape)"
    return _res("info", head, groups={k: {kk: vv for kk, vv in v.items()
                                          if isinstance(vv, (int, float, str, type(None)))}
                                      for k, v in groups.items()})


def check_gapfield(con: Any, cfg: Any, d: date) -> dict:
    """Is the nightly job pricing each session, and how far is the history?"""
    from . import gapfield
    from .util import prev_market_day
    st = gapfield.status(con)
    job = st.get("job") or {}
    fwd = st.get("forward") or {}
    if not job and not fwd:
        return _res("info", "the nightly job has not run yet (it prices the gap field after "
                            "each close and fills ~2 years of history over a few nights)")
    last = _as_date(fwd.get("last"))
    # The nightly prices session d after the evening runs, so by this evening
    # the newest forward session should be the previous one.
    expect = prev_market_day(d)
    status = "ok"
    parts = []
    if fwd:
        parts.append(f"forward {fwd['rows']} rows over {fwd['sessions']} session(s), last {last}")
    if job:
        parts.append("history complete" if job.get("finished_at") else
                     f"history {job.get('next_index')}/{job.get('n_symbols')} symbols")
    if (st.get("backfill") or {}).get("rows"):
        parts.append(f"{st['backfill']['rows']} backfilled rows over "
                     f"{st['backfill']['sessions']} sessions")
    if last is not None and last < expect:
        status = "warn"
        parts.insert(0, f"no forward gap field since {last} (expected {expect}): is the "
                        "nightly cron running?")
    return _res(status, "; ".join(parts), forward=fwd, job={k: str(v) for k, v in job.items()},
                backfill=st.get("backfill") or {})


CHECKS: tuple[tuple[str, str, Callable[[Any, Any, date], dict]], ...] = (
    ("plumbing", "Data collected today", check_plumbing),
    ("lanes", "Lanes and broker cap", check_lanes),
    ("entries", "Entries and fills", check_entries),
    ("runner_selection", "Runner selection", check_runner_selection),
    ("grader_audit", "Grader audit", check_grader_audit),
    ("morning_budget", "Morning time budget", check_morning_budget),
    ("hypotheses", "Hypothesis notebook", check_hypotheses),
    ("lessons", "Lessons steering untested", check_lessons),
    ("edge_pipeline", "From data to edge", check_edge_pipeline),
    ("gapfield", "Gap field (nightly)", check_gapfield),
    ("persona", "Persona check", check_persona),
)


# --------------------------------------------------------------------------
# Run, store, render
# --------------------------------------------------------------------------

def run(con: Any, cfg: Any, d: date, persist: bool = True) -> list[dict]:
    """Every check for session d. Never raises; persists one row per check."""
    out = []
    for key, title, fn in CHECKS:
        try:
            r = fn(con, cfg, d)
        except Exception as e:  # noqa: BLE001 -- a check that fails is a reading, not a crash
            r = _res("warn", f"check could not run ({type(e).__name__}: {e})")
        r.update({"key": key, "title": title})
        out.append(r)
        if persist:
            try:
                con.execute(
                    """INSERT INTO self_checks (session_date, check_key, status, headline, value_json)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT (session_date, check_key) DO UPDATE SET
                           status = excluded.status, headline = excluded.headline,
                           value_json = excluded.value_json""",
                    [d, key, r["status"], r["headline"][:2000],
                     json.dumps(r.get("value") or {}, default=str)[:20000]])
            except Exception:  # noqa: BLE001 -- storing the reading never breaks the evening
                pass
    return out


def text(results: list[dict]) -> str:
    """The evening email's closing block."""
    if not results:
        return ""
    lines = ["SELF-CHECK (the agent auditing its own production data; changes nothing about trading)"]
    for r in results:
        lines.append(f"[{r['status'].upper():<4}] {r['title']}: {r['headline']}")
    return "\n".join(lines)


def latest(con: Any) -> dict:
    """The newest session's checks, for the dashboard health panel."""
    rows = _q(con, """SELECT session_date, check_key, status, headline FROM self_checks
                      WHERE session_date = (SELECT MAX(session_date) FROM self_checks)""")
    if not rows:
        return {}
    titles = {k: t for k, t, _ in CHECKS}
    worst = max((r[2] for r in rows), key=lambda s: _ORDER.get(s, 0))
    return {"session_date": str(rows[0][0]), "worst": worst,
            "flagged": [f"{titles.get(r[1], r[1])}: {r[3]}" for r in rows
                        if _ORDER.get(r[2], 0) >= _ORDER["warn"]]}
