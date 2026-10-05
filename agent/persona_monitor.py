"""
The persona monitor (specs/PERSONA_CHARACTER_SHEET_SPEC_v1.md §3.2).

The "earned hunger" character sheet is a TREATMENT, not a neutral restyle:
the spec's own worry (P-2) is that a hungry trader takes more, bigger or
higher-conviction trades. This module makes such a shift VISIBLE. It does not
explain it and it does not test it.

Rows are grouped by the stamp written when they were produced
(`persona_version`, migration v29; NULL = persona off). For each group it
prints counts and distributions only -- no test, no p-value, no verdict.
Persona-on and persona-off are different calendar periods under different
tape, and the paired challenger arm cannot separate them (it varies only the
memory context and inherits arm A's system prompts), so nothing printed here
can be attributed to the persona.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

__all__ = ["HEADER", "LIMITATION", "collect", "render_text"]

HEADER = ("Descriptive. Persona on/off periods are different calendar periods under different "
          "tape; this table cannot attribute any difference to the persona. It exists so a change "
          "in trade count, size or conviction is visible, not so it can be explained.")

LIMITATION = ("No paired persona on/off arm exists: the paired challenger arm varies only the "
              "memory context and receives arm A's system prompts, so with the persona on both "
              "arms carry it. Calendar periods are the only split available.")

# The hypothesis lane went live on the persona's first sessions (2026-10-05).
# It adds trades of its own (exploration_lane = 'hypothesis') and lets the
# analyst bring extra candidates tagged to an idea. Both are shown APART, and
# the pick counts are also given without the lane, so trades the lane added
# are never read as trades the persona added.
HYPOTHESIS_NOTE = ("Hypothesis-lane picks and hypothesis-tagged candidates are counted apart; "
                   "'excluding the hypothesis lane' is the like-for-like pick count.")

# The daily floor (50106eb) went live 2026-09-29: before it, a day could end
# with no picks at all, so persona-off sessions before it are not a baseline
# for trade counts. `collect(since=FLOOR_LIVE)` reads only sessions after it.
FLOOR_LIVE = "2026-09-29"

_CAP_NOTE = re.compile(r"industry cap \S+ dropped (\d+)")
_OUTAGE = "data_incomplete"
_RUNNER = "dto_runner"


def _label(v: Any) -> str:
    return "persona off" if v in (None, "") else f"persona v{v}"


def _q(con: Any, sql: str, params: list | None = None) -> list[tuple]:
    try:
        return con.execute(sql, params or []).fetchall()
    except Exception:  # noqa: BLE001 -- a missing table/column is an empty reading
        return []


def _recent(dates: list, n: int | None) -> set:
    return set(sorted(dates)[-n:]) if n else set(dates)


def _on_or_after(d: Any, since: Any) -> bool:
    return since is None or str(d)[:10] >= str(since)[:10]


def collect(con: Any, sessions: int | None = 10, since: Any = None) -> dict[str, dict]:
    """{group label: readings} for the most recent `sessions` sessions of each
    group (None = all), on or after `since` when given. Morning sessions are
    daily_plans rows; runner sessions are runner_sessions rows. Never raises."""
    groups: dict[str, dict] = {}

    # --- morning lane: one session per daily_plans row ---------------------
    plans = [r for r in _q(con, """SELECT plan_date, no_trade, no_trade_category,
                                          session_degraded, analyst_json, persona_version
                                   FROM daily_plans""")
             if _on_or_after(r[0], since)]
    by_group: dict[str, list[tuple]] = {}
    for row in plans:
        by_group.setdefault(_label(row[5]), []).append(row)
    for label, rows in by_group.items():
        keep = _recent([r[0] for r in rows], sessions)
        rows = [r for r in rows if r[0] in keep]
        g = groups.setdefault(label, {})
        g["sessions"] = len(rows)
        g["first"], g["last"] = (min(keep), max(keep)) if keep else (None, None)
        # The risk manager's no_trade flag, as stored. Since the daily floor
        # and coverage lanes, a flagged day can still carry picks, so taken
        # vs no-trade days are counted from `picks` below, not from this.
        g["no_trade_flag_days"] = sum(1 for r in rows if r[1])
        g["_flag"] = {r[0]: (bool(r[1]), r[2]) for r in rows}
        g["degraded_sessions"] = sum(1 for r in rows if r[3])
        cand_conv: Counter = Counter()
        n_cand = n_probe = n_hyp = 0
        for r in rows:
            try:
                cands = (json.loads(r[4] or "{}") or {}).get("candidates") or []
            except (TypeError, ValueError):
                cands = []
            for c in cands:
                n_cand += 1
                cand_conv[c.get("conviction")] += 1
                if c.get("probe_for"):
                    n_probe += 1
                if c.get("hypothesis_id"):
                    n_hyp += 1
        g["candidates"] = n_cand
        g["hypothesis_candidates"] = n_hyp
        g["candidate_conviction"] = dict(cand_conv)
        g["probe_candidates"] = n_probe
        g["_dates"] = sorted(keep)

    # Morning picks and their outcomes, on those sessions only.
    for label, g in groups.items():
        dates = g.pop("_dates", [])
        flags = g.pop("_flag", {})
        if not dates:
            continue
        marks = ",".join("?" for _ in dates)
        sql = f"""SELECT p.conviction, p.size_r, p.exploration_lane,
                         COALESCE(o.triggered, FALSE), p.plan_date, {{tag}}
                  FROM picks p LEFT JOIN outcomes o ON o.pick_id = p.pick_id
                  WHERE p.plan_date IN ({marks})
                    AND COALESCE(p.setup_type, '') <> '{_RUNNER}'"""
        picks = _q(con, sql.format(tag="p.hypothesis_id"), list(dates))
        if not picks:   # a pre-v31 ledger has no hypothesis_id column
            picks = _q(con, sql.format(tag="NULL"), list(dates))
        g["picks"] = len(picks)
        g["pick_conviction"] = dict(Counter(p[0] for p in picks))
        sizes = [float(p[1]) for p in picks if p[1] is not None]
        g["mean_size_r"] = (sum(sizes) / len(sizes)) if sizes else None
        lanes = Counter(p[2] for p in picks)
        g["daily_floor_picks"] = lanes.get("daily_floor", 0)
        g["coverage_picks"] = lanes.get("coverage", 0)
        g["hypothesis_lane_picks"] = lanes.get("hypothesis", 0)
        g["hypothesis_tagged_picks"] = sum(1 for p in picks if p[5])
        g["picks_ex_hypothesis_lane"] = len(picks) - g["hypothesis_lane_picks"]
        g["triggered"] = sum(1 for p in picks if p[3])
        g["triggered_ex_hypothesis_lane"] = sum(1 for p in picks if p[3] and p[2] != "hypothesis")
        with_picks = {p[4] for p in picks}
        g["taken_days"] = sum(1 for d in dates if d in with_picks)
        g["no_trade_days"] = sum(1 for d in dates if d not in with_picks)
        stand = Counter()
        for d in dates:
            if d in with_picks:
                continue
            _, category = flags.get(d, (False, None))
            stand["outage (data_incomplete)" if category == _OUTAGE
                  else f"genuine ({category or 'no category stored'})"] += 1
        g["stand_down_reasons"] = dict(stand)

    # --- runner lane: one session per runner_sessions row ------------------
    stamps = dict(_q(con, f"""SELECT plan_date, MAX(persona_version) FROM picks
                             WHERE setup_type = '{_RUNNER}' GROUP BY plan_date"""))
    cands = dict(_q(con, "SELECT session_date, COUNT(*) FROM runner_candidates GROUP BY session_date"))
    chosen = dict(_q(con, """SELECT session_date, COUNT(*) FROM runner_candidate_outcomes
                             WHERE selected GROUP BY session_date"""))
    notes = dict(_q(con, "SELECT session_date, selection_skipped_reason FROM runner_sessions"))
    runner_by_group: dict[str, list] = {}
    for d in set(cands) | set(notes):
        if _on_or_after(d, since):
            runner_by_group.setdefault(_label(stamps.get(d)), []).append(d)
    for label, dates in runner_by_group.items():
        keep = _recent(dates, sessions)
        g = groups.setdefault(label, {})
        g["runner_sessions"] = len(keep)
        g["runner_candidates"] = sum(int(cands.get(d) or 0) for d in keep)
        g["runner_selected"] = sum(int(chosen.get(d) or 0) for d in keep)
        drops = 0
        for d in keep:
            m = _CAP_NOTE.search(notes.get(d) or "")
            drops += int(m.group(1)) if m else 0
        g["runner_cap_drops"] = drops
    return groups


def _hist(h: dict) -> str:
    if not h:
        return "none"
    keyed = sorted(h.items(), key=lambda kv: (kv[0] is None, kv[0] if kv[0] is not None else 0))
    return ", ".join(f"{'unstated' if k is None else k}: {v}" for k, v in keyed)


def _per(n: int | None, d: int | None) -> str:
    if not d:
        return "n/a"
    return f"{(n or 0) / d:.2f}"


def render_text(groups: dict[str, dict], sessions: int | None = 10, since: Any = None) -> str:
    """Plain text for `run.py persona-monitor`. Counts and distributions only."""
    window = f"most recent {sessions} session(s) per group" if sessions else "all sessions"
    if since:
        window += f", on or after {since}"
    out = ["PERSONA MONITOR (" + window + ")", "", HEADER, "", LIMITATION, "", HYPOTHESIS_NOTE, ""]
    if not groups:
        out.append("No sessions recorded yet.")
        return "\n".join(out)
    for label in sorted(groups, key=lambda s: (s != "persona off", s)):
        g = groups[label]
        n = g.get("sessions") or 0
        out.append(f"== {label} ==")
        if n:
            out += [
                f"  morning sessions: {n} ({g.get('first')} .. {g.get('last')})",
                f"  taken days: {g.get('taken_days', 0)}; no-trade days: {g.get('no_trade_days', 0)}"
                + (f" ({', '.join(f'{k}: {v}' for k, v in sorted(g['stand_down_reasons'].items()))})"
                   if g.get("stand_down_reasons") else ""),
                f"  risk manager no_trade flag set: {g.get('no_trade_flag_days', 0)} day(s)",
                f"  degraded sessions: {g.get('degraded_sessions', 0)}",
                f"  picks: {g.get('picks', 0)}; triggered: {g.get('triggered', 0)}; "
                f"triggered per session: {_per(g.get('triggered'), n)}",
                f"  excluding the hypothesis lane: picks {g.get('picks_ex_hypothesis_lane', 0)} "
                f"(per session {_per(g.get('picks_ex_hypothesis_lane'), n)}); triggered "
                f"{g.get('triggered_ex_hypothesis_lane', 0)} (per session "
                f"{_per(g.get('triggered_ex_hypothesis_lane'), n)})",
                f"  hypothesis lane picks: {g.get('hypothesis_lane_picks', 0)}; picks tagged to "
                f"an idea (any lane): {g.get('hypothesis_tagged_picks', 0)}; candidates tagged to "
                f"an idea: {g.get('hypothesis_candidates', 0)}",
                f"  daily_floor picks: {g.get('daily_floor_picks', 0)} "
                f"(per session {_per(g.get('daily_floor_picks'), n)}); coverage picks: "
                f"{g.get('coverage_picks', 0)} (per session {_per(g.get('coverage_picks'), n)})",
                "  mean planned size of picks: "
                + ("n/a" if g.get("mean_size_r") is None else f"{g['mean_size_r']:.2f}R"),
                f"  candidates: {g.get('candidates', 0)}; with probe_for: {g.get('probe_candidates', 0)} "
                f"(share {_per(g.get('probe_candidates'), g.get('candidates'))})",
                f"  candidate conviction: {_hist(g.get('candidate_conviction') or {})}",
                f"  pick conviction: {_hist(g.get('pick_conviction') or {})}",
            ]
        else:
            out.append("  morning sessions: 0")
        rs = g.get("runner_sessions") or 0
        if rs:
            out += [f"  runner sessions: {rs}; candidates: {g.get('runner_candidates', 0)}; "
                    f"selected: {g.get('runner_selected', 0)} "
                    f"(per session {_per(g.get('runner_selected'), rs)}); "
                    f"industry-cap drops: {g.get('runner_cap_drops', 0)}"]
        else:
            out.append("  runner sessions: 0")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
