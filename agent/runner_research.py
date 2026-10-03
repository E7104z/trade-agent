"""
Runner mining: setup edge and selection edge (RUNNER_PORT_SPEC_v1 2.6).

Two questions, both routed through the v4 integrity layer so they carry the
same FDR correction and the same logged-look accounting as every other
comparison the weekly miner sees:

  1. SETUP EDGE     — mean R across ALL candidates vs. zero.
                      "Does the Runner setup have edge at all?"
  2. SELECTION EDGE — mean R of selected vs. mean R of unselected, same
                      sessions. "Did picking help?"

The second is the paired comparison the old manual workflow could never
produce, because it never priced the names it passed over.

── STATE THE NULL OUTCOME PLAINLY ────────────────────────────────────────────
If selection edge is not distinguishable from zero after enough sessions, the
correct conclusion is that SELECTION ADDS NOTHING, and the right action is to
trade the full filtered list or a random N of it. That is a real result, not a
failure of the experiment, and this module is built to report it: see
`selection_edge`'s `verdict` field, which says "no detectable selection edge"
rather than going quiet.

Every comparison emitted here joins the SAME Benjamini-Hochberg batch as the
rest of the weekly search (agent/integrity.build_search). Correcting the runner
comparisons separately would leave the across-family search uncontrolled, which
is exactly where the width lives.
"""
from __future__ import annotations

import math
import random
from datetime import date, timedelta
from typing import Any

from .clusters import unit_values
from .stats import (Comparison, min_detectable_effect_two_sample, one_sample_t,
                    welch_t)

__all__ = [
    "MINING_DIMENSIONS",
    "candidate_rows",
    "current_candidate_rows",
    "current_versions",
    "version_mix",
    "bucket_of",
    "candidate_comparisons",
    "setup_edge",
    "selection_edge",
    "runner_random_ablation",
    "runner_random_ablation_md",
    "runner_md",
    "DRIFT_PREFIX",
    "DRIFT_ALL",
    "LANE_VERDICT_SESSIONS",
    "lane_verdict",
    "current_drift_rows",
    "drift_comparisons",
    "continuation_md",
    "selection_coverage",
    "selection_coverage_md",
    "evidence_report_text",
]

# Mining dimensions (spec 2.6) and their bucketing live in agent/buckets.py,
# the single declaration the weekly search AND the evening forward-observation
# matcher both read (FORWARD_VALIDATION_SPEC_v1 Phase 1). Re-exported here
# under their established names.
from .buckets import RUNNER_DIMS as MINING_DIMENSIONS  # noqa: E402
from .buckets import runner_label as bucket_of  # noqa: E402


def _mean_std(values: list[float]) -> tuple[float | None, float | None]:
    n = len(values)
    if n == 0:
        return None, None
    mean = sum(values) / n
    if n < 2:
        return mean, None
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean, math.sqrt(var)


def _planned_r(r: dict) -> float | None:
    """Realized R in planned-risk units: `r_planned` when present, else the
    legacy `r_multiple` (which used the floored effective risk). Rows graded
    after v23 carry `r_planned`; older rows fall back until re-graded."""
    v = r.get("r_planned")
    if v is not None:
        return float(v)
    v = r.get("r_multiple")
    return float(v) if v is not None else None


def candidate_rows(con: Any, sessions: int = 60,
                   scan_version: int | None = None,
                   sim_version: int | None = None) -> list[dict]:
    """Joined candidate + outcome rows that actually triggered.

    Only triggered rows carry an R multiple, so only they can enter a mean-R
    comparison. Rows are filtered by `scan_version` and `sim_version` when
    given: never compare across either, because a detection change makes rows
    describe different populations and a grading change makes them describe
    different fills. Production callers go through `current_candidate_rows`,
    which pins both to the running code's versions.
    """
    from . import ledger
    since = date.today() - timedelta(days=sessions * 2)
    try:
        rows = ledger.runner_candidate_outcomes_since(con, since)
    except Exception:
        return []
    out = []
    for r in rows:
        if scan_version is not None and r.get("scan_version") != scan_version:
            continue
        if sim_version is not None and r.get("sim_version") != sim_version:
            continue
        if not r.get("triggered") or _planned_r(r) is None:
            continue
        out.append(r)
    return _attach_regime(con, out)


def current_versions() -> tuple[int, int]:
    """(scan_version, sim_version) of the code that is running now."""
    from .grading import CURRENT_SIM_VERSION
    from .runner_scan import SCAN_VERSION
    return SCAN_VERSION, CURRENT_SIM_VERSION


def current_candidate_rows(con: Any, sessions: int = 60) -> list[dict]:
    """The runner corpus the weekly search may mine: triggered rows produced
    under the CURRENT detection and grading semantics only.

    Both integrity call sites use this one helper so they cannot drift apart --
    the filter existed on `candidate_rows` for weeks while neither caller
    passed it, and v1/v2 rows were pooled.
    """
    scan_v, sim_v = current_versions()
    return candidate_rows(con, sessions, scan_version=scan_v, sim_version=sim_v)


def version_mix(con: Any, sessions: int = 60) -> list[dict]:
    """Triggered runner rows by (scan_version, sim_version), current or not.

    Counts only. The excluded rows' statistics are never computed, so telling
    the miner WHY its n moved cannot leak a second, pooled reading.
    """
    rows = candidate_rows(con, sessions)
    counts: dict[tuple, int] = {}
    for r in rows:
        key = (r.get("scan_version"), r.get("sim_version"))
        counts[key] = counts.get(key, 0) + 1
    scan_v, sim_v = current_versions()
    return [{"scan_version": k[0], "sim_version": k[1], "n": n,
             "current": k == (scan_v, sim_v)}
            for k, n in sorted(counts.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1])))]


def _attach_regime(con: Any, rows: list[dict]) -> list[dict]:
    if not rows:
        return rows
    tags: dict[date, str] = {}
    try:
        for d, tag in con.execute(
                "SELECT plan_date, regime_tag FROM daily_plans").fetchall():
            tags[d] = tag
    except Exception:
        pass
    for r in rows:
        r["regime_tag"] = tags.get(r.get("session_date"))
    return rows


def candidate_comparisons(rows: list[dict], n_dimensions: int = 1) -> list[Comparison]:
    """One Comparison per (dimension, bucket) — every one of these is a look.

    n counts (session, industry) UNITS, not rows (agent/clusters.py): the rows
    of one session in one industry are one event, and counting them one by
    one let a cell clear n >= 8 on a single morning's tape. Each unit's value
    is its rows' mean realized R; the row count rides in the note.
    """
    out: list[Comparison] = []
    for dim in MINING_DIMENSIONS:
        buckets: dict[str, list[dict]] = {}
        for r in rows:
            buckets.setdefault(bucket_of(dim, r), []).append(r)
        for level, cell in sorted(buckets.items()):
            values = unit_values(cell, _planned_r)
            mean, std = _mean_std(values)
            note = f"{len(values)} session x industry units from {len(cell)} rows"
            if level == "?":
                note = "null/unmeasured bucket; " + note
            out.append(Comparison(
                dimension=dim, level=level, n=len(values),
                mean_r=mean, std_r=std, n_dimensions=n_dimensions, note=note,
            ))
    return out


def setup_edge(rows: list[dict], min_n: int = 8) -> dict:
    """Mean R across ALL candidates vs. zero. Does the setup have edge at all?

    Tested over (session, industry) units, not rows: see agent/clusters.py."""
    values = unit_values(rows, _planned_r)
    mean, std = _mean_std(values)
    t, df, p = one_sample_t(mean, std, len(values), 0.0)
    return {
        "n": len(values), "n_rows": len(rows), "mean_r": mean, "std_r": std,
        "t_stat": t, "df": df, "p_value": p,
        "underpowered": len(values) < min_n,
        "comparison": Comparison(
            dimension="runner_setup_edge", level="all_candidates",
            n=len(values), mean_r=mean, std_r=std,
            note=(f"mean R across every runner candidate vs zero; "
                  f"{len(values)} session x industry units from {len(rows)} rows")),
    }


# Contested sessions a selection verdict needs. The candidates of one session
# share its tape, so nine picks from one morning are closer to one observation
# than to nine; the random-portfolio control withholds its verdict below the
# same count, and the two readings of the same selector must not disagree
# about whether a verdict exists.
MIN_CONTESTED_SESSIONS = 3


def selection_edge(rows: list[dict], min_n: int = 8) -> dict:
    """Mean R of selected vs. unselected, restricted to sessions where a real
    choice was made.

    Sessions where every candidate was auto-selected (candidates <= max_picks)
    carry no selection signal — there was no choice — so including them would
    dilute the comparison with rows that answer a different question. Only
    sessions containing BOTH a selected and an unselected candidate count, and
    below MIN_CONTESTED_SESSIONS of them the comparison is underpowered however
    many rows each side holds.
    """
    by_session: dict[Any, dict[str, list[dict]]] = {}
    for r in rows:
        d = r.get("session_date")
        side = "selected" if r.get("selected") else "unselected"
        by_session.setdefault(d, {"selected": [], "unselected": []})[side].append(r)

    contested = {d: v for d, v in by_session.items()
                 if v["selected"] and v["unselected"]}
    # Each side counts (session, industry) units, not rows (agent/clusters.py):
    # six tanker picks off one tick are one selected observation, not six.
    sel = [x for v in contested.values() for x in unit_values(v["selected"], _planned_r)]
    unsel = [x for v in contested.values() for x in unit_values(v["unselected"], _planned_r)]

    m_sel, s_sel = _mean_std(sel)
    m_unsel, s_unsel = _mean_std(unsel)
    t, df, p = welch_t(m_sel, s_sel, len(sel), m_unsel, s_unsel, len(unsel))

    delta = (m_sel - m_unsel) if (m_sel is not None and m_unsel is not None) else None
    underpowered = (len(sel) < min_n or len(unsel) < min_n
                    or len(contested) < MIN_CONTESTED_SESSIONS)
    if underpowered:
        verdict = (f"underpowered - {len(sel)} selected / {len(unsel)} unselected "
                   f"across {len(contested)} contested session(s), need n>={min_n} a side "
                   f"and >= {MIN_CONTESTED_SESSIONS} contested sessions; no direction "
                   "may be read from the delta")
    elif p is not None and p < 0.05:
        verdict = ("selected outperformed unselected"
                   if (delta or 0) > 0 else
                   "selected UNDERperformed unselected - the picker is subtracting value")
    else:
        verdict = ("no detectable selection edge - on this evidence the right action "
                   "is to trade the full filtered list or a random N of it")

    return {
        "sessions_contested": len(contested),
        "n_selected": len(sel), "n_unselected": len(unsel),
        "mean_r_selected": m_sel, "mean_r_unselected": m_unsel,
        "delta_r": delta, "t_stat": t, "df": df, "p_value": p,
        "underpowered": underpowered, "verdict": verdict,
        # test="welch": evaluate_search keeps this t/df/p instead of re-testing
        # the delta as a one-sample mean. std_r is None because no single
        # standard deviation describes a two-arm comparison; n is the smaller
        # arm so the n>=8 floor still binds on it.
        "comparison": Comparison(
            dimension="runner_selection_edge", level="selected_vs_unselected",
            n=min(len(sel), len(unsel)), mean_r=delta,
            std_r=None, t_stat=t, df=df, p_value=p,
            mde=min_detectable_effect_two_sample(len(sel), s_sel, len(unsel), s_unsel),
            test="welch", note=verdict[:400]),
    }


# ---------------------------------------------------------------- continuation
# specs/RUNNER_TRADE_CONSTRUCTION_v1.md. Realized R mixes "did price keep going
# up after entry" with "did the stop/target capture it". Drift answers the
# first alone, for every candidate with an enterable bar, traded or not:
# (session close - first enterable price) / ATR14. Where a whole bucket's drift
# is not positive, no stop or target rule can win in it; where it is, the
# construction arms say whether any rule captures it.
#
# Each (dimension, bucket) is one look in the SAME FDR batch, under dimensions
# prefixed DRIFT_PREFIX so they never mix with the R cells. Values are in ATR,
# not R; the Comparison's mean_r field carries ATR here. A drift finding is a
# map, not a rule: no rule can be stated on a drift dimension, and anything the
# desk acts on still validates forward in R (on the runner dimension itself,
# or on an exit arm).

DRIFT_PREFIX = "drift:"
DRIFT_ALL = "drift:all"

# The lane-level stop condition, pre-registered 2026-10-02 before any session
# under scan v3 x sim v7 was mined: once this many sessions are measured, if
# no continuation bucket is significantly positive AND no construction (the
# plan or an arm) has significantly positive expectancy of its own in the
# weekly batch, the detection carries no edge as built -- the lane needs a
# redesign or a pause, not more tuning. An arm that merely beats the plan does
# not keep the lane alive: sized by risk, a wider stop holds fewer shares, so
# it loses less on a falling field without making money (the 10/02 replay:
# every arm beat the plan, every arm lost). Surfaced weekly; the decision is
# Jason's, never automated.
LANE_VERDICT_SESSIONS = 20


def lane_verdict(sessions: int, drift_comps: list, exit_comps: list,
                 expectancy_comps: list | None = None) -> str:
    """One line: where the runner lane stands against its stop condition."""
    def _pos(c) -> bool:
        return getattr(c, "significant", False) and (c.mean_r or 0) > 0

    pos_drift = [c for c in drift_comps or [] if _pos(c)]
    pos_expect = [c for c in expectancy_comps or [] if _pos(c)]
    paying = {c.level for c in pos_expect}
    only_better = [c for c in exit_comps or [] if _pos(c) and c.level not in paying]
    found = ([f"drift {c.dimension[len(DRIFT_PREFIX):]}={c.level}" for c in pos_drift[:4]]
             + [f"expectancy {c.level}" for c in pos_expect[:4]])
    caveat = (f" Beats the plan but does not make money (a smaller loss, not an edge): "
              f"{', '.join(c.level for c in only_better[:4])}." if only_better else "")
    if sessions < LANE_VERDICT_SESSIONS:
        # Not readable either way yet: the drift cells count (session,
        # industry) units, and every unit of one session shares its tape, so
        # one strong day can make a bucket "significant" on its own.
        early = (f" Early positives (few tapes; not a verdict): {', '.join(found)}."
                 if found else " Nothing significantly positive yet.")
        return (f"Lane stop condition: {sessions}/{LANE_VERDICT_SESSIONS} sessions measured "
                "-- too early to read." + early + caveat)
    if found:
        return (f"Lane stop condition: NOT met -- significantly positive: "
                f"{', '.join(found)} ({sessions} sessions measured). Candidates, not "
                "rules: each must still validate forward before it changes a live order."
                + caveat)
    return (f"Lane stop condition: MET at {sessions} sessions -- no bucket where price keeps "
            "rising after entry and no construction with positive expectancy. As built, the "
            "detection carries no edge: the lane needs a redesign or a pause, not tuning "
            "(Jason's decision)." + caveat)


def _drift(r: dict) -> float | None:
    v = r.get("drift_atr")
    return float(v) if v is not None else None


def current_drift_rows(con: Any, sessions: int = 60) -> list[dict]:
    """Every candidate under the CURRENT scan/sim versions with a measured
    drift -- triggered, untriggered and invalid fills alike."""
    from . import ledger
    scan_v, sim_v = current_versions()
    since = date.today() - timedelta(days=sessions * 2)
    try:
        rows = ledger.runner_candidate_outcomes_since(con, since)
    except Exception:
        return []
    out = [r for r in rows
           if r.get("scan_version") == scan_v and r.get("sim_version") == sim_v
           and _drift(r) is not None]
    return _attach_regime(con, out)


def drift_comparisons(rows: list[dict]) -> list[Comparison]:
    """The continuation family: lane-wide drift vs 0, then every (dimension,
    bucket) vs 0, over (session, industry) units. Empty with no rows -- no
    drift measured, nothing looked at."""
    if not rows:
        return []
    vals = unit_values(rows, _drift)
    mean, std = _mean_std(vals)
    up = sum(1 for r in rows if (_drift(r) or 0) > 0)
    out = [Comparison(
        dimension=DRIFT_ALL, level="all_candidates", n=len(vals), mean_r=mean, std_r=std,
        note=(f"mean (close - first enterable price)/ATR14 vs 0, in ATR not R; "
              f"{len(vals)} session x industry units from {len(rows)} candidates, "
              f"{up} closed above entry"))]
    for dim in MINING_DIMENSIONS:
        cells: dict[str, list[dict]] = {}
        for r in rows:
            cells.setdefault(bucket_of(dim, r), []).append(r)
        for level, cell in sorted(cells.items()):
            values = unit_values(cell, _drift)
            m, s = _mean_std(values)
            above = sum(1 for r in cell if (_drift(r) or 0) > 0)
            note = (f"drift in ATR; {len(values)} units from {len(cell)} candidates, "
                    f"{above} closed above entry")
            if level == "?":
                note = "null/unmeasured bucket; " + note
            out.append(Comparison(dimension=DRIFT_PREFIX + dim, level=level,
                                  n=len(values), mean_r=m, std_r=s, note=note))
    return out


def _above_share(rows: list[dict], dim: str | None = None, level: str | None = None):
    cell = rows if dim is None else [r for r in rows if bucket_of(dim, r) == level]
    if not cell:
        return None, 0
    return sum(1 for r in cell if (_drift(r) or 0) > 0) / len(cell), len(cell)


def continuation_md(comparisons: list[Comparison], rows: list[dict]) -> str:
    """The miner's continuation section. `comparisons` carry batch q-values."""
    head = "### 5. Continuation after entry — exit-independent, in ATR"
    if not rows:
        return (f"{head}\n\nNo candidate under the current scan/sim versions carries a "
                "measured drift yet (schema v28 fields are written by the evening; "
                "`run.py regrade-runner` fills earlier sessions).")
    by_key = {(c.dimension, c.level): c for c in comparisons}
    overall = by_key.get((DRIFT_ALL, "all_candidates"))
    share, n_rows = _above_share(rows)

    def q_cell(c):
        if c is None:
            return "n/a"
        if c.test == "degenerate":
            return "not tested (degenerate)"
        return f"{_fmt(c.q_value, 4)}{' *' if c.significant else ''}"

    lines = [
        head, "",
        "Every candidate with a first enterable price, traded or not. drift = (session "
        "close − first enterable price) ÷ ATR14; no stop, no target. n = session x "
        "industry units. Every unit of one session shares that session's tape — one "
        "fade day pushes every bucket down together — so a bucket's sign means "
        "something only across many sessions.",
        "",
        f"- all candidates: n = {overall.n if overall else 0} units ({n_rows} candidates), "
        f"mean drift {_fmt(overall.mean_r if overall else None)} ATR, "
        f"{_fmt(100 * share if share is not None else None, 0)}% closed above entry, "
        f"q = {q_cell(overall)}",
        "",
        "| dimension | bucket | n | mean drift (ATR) | closed above entry | q |",
        "|---|---|---|---|---|---|",
    ]
    for c in comparisons:
        if not c.dimension.startswith(DRIFT_PREFIX) or c.dimension == DRIFT_ALL or c.n < 3:
            continue
        dim = c.dimension[len(DRIFT_PREFIX):]
        sh, n_c = _above_share(rows, dim, c.level)
        lines.append(f"| {dim} | {c.level} | {c.n} | {_fmt(c.mean_r)} | "
                     f"{_fmt(100 * sh if sh is not None else None, 0)}% of {n_c} | {q_cell(c)} |")
    lines += [
        "",
        "Read: a bucket whose drift is not positive cannot be won by any stop or "
        "target rule — the price did not go up after entry. A significant POSITIVE "
        "bucket is where to look, not a rule: any filter the desk acts on still has "
        "to validate forward in R on its runner dimension, under the plan or an "
        "exit arm (section 4). If no bucket shows positive drift and no arm beats "
        "the plan over enough sessions, that is a real answer: the detection itself "
        "carries no edge, and the lane needs a redesign or a pause, not tuning.",
    ]
    return "\n".join(lines)


# ------------------------------------------------------- selection coverage
# Why a session is or is not a contested session (selected AND unselected
# triggered candidates priced under the current versions). v9's selection
# verdict came from ONE contested session out of 1,003 runner rows; the count
# alone cannot say whether the selector failed, the session was never graded,
# or the version pin excluded it. Every session states its reason.

def selection_coverage(con: Any, sessions: int = 60) -> list[dict]:
    """One row per runner session in the window, any version, newest last."""
    from . import ledger
    since = date.today() - timedelta(days=sessions * 2)
    try:
        rows = ledger.runner_candidate_outcomes_since(con, since)
    except Exception:
        return []
    try:
        meta = {s["session_date"]: s for s in ledger.recent_runner_sessions(con, sessions * 2)}
    except Exception:
        meta = {}
    scan_v, sim_v = current_versions()
    by_session: dict[Any, list[dict]] = {}
    for r in rows:
        by_session.setdefault(r.get("session_date"), []).append(r)

    out = []
    for d in sorted(by_session, key=str):
        cell = by_session[d]
        current = [r for r in cell
                   if r.get("scan_version") == scan_v and r.get("sim_version") == sim_v]
        priced = [r for r in current if r.get("triggered") and _planned_r(r) is not None]
        sel = sum(1 for r in cell if r.get("selected"))
        sel_priced = sum(1 for r in priced if r.get("selected"))
        unsel_priced = len(priced) - sel_priced
        skip = str((meta.get(d) or {}).get("selection_skipped_reason") or "")
        versions = sorted({(r.get("scan_version"), r.get("sim_version")) for r in cell},
                          key=str)
        if sel_priced and unsel_priced:
            reason = "contested"
        elif not any(r.get("sim_version") is not None for r in cell):
            reason = "not graded yet (no evening pricing for this session)"
        elif not current:
            reason = ("excluded by the version pin (" + ", ".join(
                f"scan v{a} x sim v{b}" for a, b in versions) +
                f"; current is scan v{scan_v} x sim v{sim_v})")
        elif skip.startswith("selection_failed"):
            reason = "the selector failed: " + skip[len("selection_failed: "):][:120]
        elif skip:
            reason = f"no selection ({skip[:120]})"
        elif not priced:
            reason = "no candidate filled validly under the current sim"
        elif sel == 0:
            reason = "no selection recorded"
        elif sel_priced == 0:
            reason = "no selected name filled validly (invalid fill or no bars)"
        else:
            reason = "every priced candidate was selected (no alternative)"
        out.append({"session_date": d, "candidates": len(cell),
                    "current": len(current), "priced": len(priced), "selected": sel,
                    "selected_priced": sel_priced, "unselected_priced": unsel_priced,
                    "contested": reason == "contested", "reason": reason})
    return out


def selection_coverage_md(cov: list[dict]) -> str:
    if not cov:
        return ""
    contested = sum(1 for c in cov if c["contested"])
    lines = [f"Selection coverage: {contested} of {len(cov)} runner session(s) contested "
             "(a selected AND an unselected candidate priced under the current versions). "
             "Why each session counts or not:", "",
             "| session | candidates | priced (current) | selected / priced | contested? |",
             "|---|---|---|---|---|"]
    for c in cov[-15:]:
        lines.append(f"| {c['session_date']} | {c['candidates']} | {c['priced']} | "
                     f"{c['selected']} / {c['selected_priced']} | {c['reason']} |")
    return "\n".join(lines)


def evidence_report_text(drift_rows: list[dict], coverage: list[dict]) -> str:
    """Plain text for `run.py runner-evidence` (read-only, zero API cost).

    Descriptive and UNCORRECTED: the drift means and their t-tests here are
    one look each with no FDR; only the weekly batch's q-value is a verdict.
    """
    out = []
    if coverage:
        out += [selection_coverage_md(coverage).replace("| ", "").replace(" |", ""), ""]
    if not drift_rows:
        out.append("No candidate under the current scan/sim versions carries a measured "
                   "drift yet. The evening writes it from schema v28 on; "
                   "`run.py regrade-runner --from <date>` fills earlier sessions.")
        return "\n".join(out)
    sessions = sorted({str(r.get("session_date")) for r in drift_rows})
    out += [f"Continuation after entry (drift = (close - first enterable price) / ATR14), "
            f"{len(drift_rows)} candidates, {len(sessions)} session(s) "
            f"{sessions[0]}..{sessions[-1]}", "",
            f"{'dimension':<26}{'bucket':<14}{'units':>6}{'rows':>6}{'drift ATR':>11}"
            f"{'% up':>7}{'run-up':>8}{'draw-dn':>9}"]

    def med(vals):
        vals = sorted(v for v in vals if v is not None)
        return vals[len(vals) // 2] if vals else None

    def line(dim, level, cell):
        vals = unit_values(cell, _drift)
        m, _s = _mean_std(vals)
        up = 100 * sum(1 for r in cell if (_drift(r) or 0) > 0) / len(cell)
        return (f"{dim:<26}{level:<14}{len(vals):>6}{len(cell):>6}{_fmt(m):>11}"
                f"{up:>6.0f}%{_fmt(med(r.get('run_up_atr') for r in cell), 2):>8}"
                f"{_fmt(med(r.get('draw_down_atr') for r in cell), 2):>9}")

    out.append(line("all", "", drift_rows))
    for dim in MINING_DIMENSIONS:
        cells: dict[str, list[dict]] = {}
        for r in drift_rows:
            cells.setdefault(bucket_of(dim, r), []).append(r)
        for level, cell in sorted(cells.items()):
            out.append(line(dim, level, cell))
    stopped_noise = sum(1 for r in drift_rows if (r.get("draw_down_atr") or 0) >= 1.0)
    out += ["",
            f"run-up / draw-dn = median highest high / lowest low from entry to the close, "
            f"in ATR. {stopped_noise} of {len(drift_rows)} candidates traded at least "
            "1 ATR below entry before the close (arm A's floor would have been hit at "
            "some point; whether before its target is the replay's question).",
            "Uncorrected and descriptive. The weekly search's q-value is the only verdict."]
    return "\n".join(out)


def _fmt(v, digits: int = 3) -> str:
    return "n/a" if v is None else f"{v:.{digits}f}"


def version_mix_md(mix: list[dict] | None) -> str:
    """One line naming the rows the version pin excluded, with counts only."""
    if not mix:
        return ""
    current = [m for m in mix if m.get("current")]
    excluded = [m for m in mix if not m.get("current")]
    if not excluded:
        return ""
    cur_s = (f"scan v{current[0]['scan_version']} x sim v{current[0]['sim_version']} "
             f"= {current[0]['n']}" if current else "none yet under the current versions")
    exc_s = ", ".join(f"scan v{m['scan_version']} x sim v{m['sim_version']} = {m['n']}"
                      for m in excluded)
    return (f"Version pin: only rows produced under the current detection and grading "
            f"semantics are mined ({cur_s}). Excluded, never pooled: {exc_s}. "
            "Their statistics are not shown; re-price with `run.py regrade-runner` "
            "or re-gate with `run.py regate-runner` to bring them under the current "
            "semantics.")


def runner_md(setup: dict, selection: dict, comparisons: list[Comparison],
              versions: list[dict] | None = None,
              coverage: list[dict] | None = None) -> str:
    """Markdown for the weekly miner prompt. `versions` is `version_mix()`,
    `coverage` is `selection_coverage()`."""
    pin = version_mix_md(versions)
    cov = selection_coverage_md(coverage or [])
    if not setup.get("n"):
        return ("## Runner lane\n\n"
                "No triggered runner candidates yet — no setup or selection "
                "comparison is possible."
                + (f"\n\n{pin}" if pin else "")
                + (f"\n\n{cov}" if cov else ""))

    lines = [
        "## Runner lane (setup_type `dto_runner`)",
        "",
    ]
    if pin:
        lines += [pin, ""]
    lines += [
        "### 1. Setup edge — every candidate, traded or not, vs zero",
        f"- n = {setup['n']} session x industry units "
        f"({setup.get('n_rows', setup['n'])} triggered candidates; the rows of one "
        "session in one industry are one observation)",
        f"- mean R = {_fmt(setup['mean_r'])} (sd {_fmt(setup['std_r'])})",
        f"- p = {_fmt(setup['p_value'], 4)}"
        + ("  **UNDERPOWERED**" if setup["underpowered"] else ""),
        "",
        "### 2. Selection edge — did picking help?",
        f"- contested sessions (both a selected and an unselected candidate): "
        f"{selection['sessions_contested']}",
        f"- selected:   n = {selection['n_selected']} units, "
        f"mean R = {_fmt(selection['mean_r_selected'])}",
        f"- unselected: n = {selection['n_unselected']} units, "
        f"mean R = {_fmt(selection['mean_r_unselected'])}",
        f"- delta = {_fmt(selection['delta_r'])} R, p = {_fmt(selection['p_value'], 4)}",
        f"- **{selection['verdict']}**",
        "",
    ]
    if cov:
        lines += [cov, ""]
    lines += [
        "A null result here is a real finding. If selection edge is not "
        "distinguishable from zero, the correct conclusion is that selection "
        "adds nothing and the right action is to trade the full filtered list "
        "or a random N — do not write a playbook rule that assumes otherwise.",
        "",
        "### 3. Candidate dimensions",
        "",
        "n = session x industry units (rows of one session in one industry are one "
        "observation; the row count is in the note).",
        "",
        "| dimension | bucket | n | mean R | q |",
        "|---|---|---|---|---|",
    ]
    for c in comparisons:
        if c.n < 3:
            continue
        q_cell = ("not tested (degenerate: restates the lane mean)"
                  if c.test == "degenerate" else
                  f"{_fmt(c.q_value, 4)}{' *' if c.significant else ''}")
        lines.append(
            f"| {c.dimension} | {c.level} | {c.n} | {_fmt(c.mean_r)} | {q_cell} |")
    return "\n".join(lines)


RUNNER_ABLATION_DRAWS = 5000
RUNNER_ABLATION_SEED = 20260827


def runner_random_ablation(rows: list[dict], draws: int = RUNNER_ABLATION_DRAWS,
                           seed: int = RUNNER_ABLATION_SEED) -> dict:
    """Did the runner selector beat drawing the same number of names at random
    from its own candidate pool, session by session?

    Complements selection_edge (the paired selected-vs-unselected comparison)
    with the exact Monte-Carlo control the morning lane runs in
    agent/ablation.py. For each contested session -- at least one selected AND
    one unselected triggered candidate -- the selector's realized total R is
    ranked against `draws` random portfolios that draw the SAME number of names
    from that session's triggered pool.

    Unweighted (size_r is identical across runner picks, so weighted ==
    unweighted). Only triggered rows carry an R; untriggered rows are excluded
    exactly as the morning ablation drops unmeasurable rows. Reproducible via
    `seed`.
    """
    rng = random.Random(seed)
    by_session: dict[Any, dict[str, list[float]]] = {}
    for r in rows:
        if not r.get("triggered") or _planned_r(r) is None:
            continue
        s = r.get("session_date")
        by_session.setdefault(s, {"pool": [], "selected": []})
        by_session[s]["pool"].append(float(_planned_r(r)))
        if r.get("selected"):
            by_session[s]["selected"].append(float(_planned_r(r)))

    actual_total = 0.0
    contested = 0
    sizes: list[tuple[list[float], int]] = []
    n_selected = 0
    for d in by_session.values():
        k = len(d["selected"])
        n_selected += k
        if k == 0 or k >= len(d["pool"]):
            continue  # no alternative existed -> no selection to test
        actual_total += sum(d["selected"])
        contested += 1
        sizes.append((d["pool"], k))

    if not contested:
        return {"sessions": 0, "picks": n_selected, "actual_total_r": None,
                "random_mean_total_r": None, "random_p05_total_r": None,
                "random_p95_total_r": None, "percentile": None, "p_value": None,
                "draws": draws, "seed": seed,
                "note": "no contested sessions (every candidate taken or none)"}

    totals = []
    for _ in range(draws):
        t = 0.0
        for pool, k in sizes:
            t += sum(rng.sample(pool, k))
        totals.append(t)
    totals.sort()
    # Same convention as agent/ablation.py: "beat" is a strict greater-than,
    # and the one-sided empirical p is P(random >= actual) with the +1
    # correction so an empirical p of exactly 0 is never claimed from finite
    # draws.
    beaten = sum(1 for t in totals if actual_total > t)
    percentile = 100.0 * beaten / len(totals)
    at_least = sum(1 for t in totals if t >= actual_total)
    p_value = (at_least + 1) / (len(totals) + 1)
    mean = sum(totals) / draws

    return {
        "sessions": contested,
        "picks": n_selected,
        "actual_total_r": round(actual_total, 3),
        "random_mean_total_r": round(mean, 3),
        "random_p05_total_r": round(totals[int(0.05 * len(totals))], 3),
        "random_p95_total_r": round(totals[min(len(totals) - 1, int(0.95 * len(totals)))], 3),
        "percentile": round(percentile, 1),
        "p_value": round(p_value, 4),
        "draws": draws,
        "seed": seed,
    }


def runner_random_ablation_md(res: dict) -> str:
    """Render the runner random-selection control for the weekly miner."""
    if not res or not res.get("sessions"):
        return ("## RUNNER SELECTION ABLATION (selector vs random)\n\n"
                "(not computable yet -- " + str(res.get("note", "no graded candidates")) + ")")
    pct = res["percentile"]
    if res["sessions"] < MIN_CONTESTED_SESSIONS:
        verdict = (f"NO VERDICT -- under {MIN_CONTESTED_SESSIONS} contested sessions; "
                   "the percentile is about these picks, not the runner selector")
    elif pct >= 95:
        verdict = "the runner selector is beating random draws from its own candidate pool"
    elif pct <= 5:
        verdict = "the runner selector is selecting WORSE than random from its own candidate pool"
    else:
        verdict = "runner selection is indistinguishable from choosing at random among its own candidates"

    return "\n".join([
        "## RUNNER SELECTION ABLATION - SELECTOR vs RANDOM (computed, zero API cost)\n",
        f"Over {res['sessions']} contested sessions and {res['picks']} selected candidates, "
        f"the selector returned {res['actual_total_r']:+.2f}R (unweighted).",
        f"{res['draws']} Monte Carlo portfolios drawing the SAME number of candidates at "
        f"random from the same triggered pools returned {res['random_mean_total_r']:+.2f}R "
        f"on average (5th pct {res['random_p05_total_r']:+.2f}R, "
        f"95th pct {res['random_p95_total_r']:+.2f}R).",
        f"\n**The runner selector beat {pct}% of random portfolios** (one-sided empirical "
        f"p={res['p_value']}). Read: {verdict}.\n",
        f"- Seed {res['seed']}: reproducible. If it moves between runs, the ledger "
        "changed -- not the dice.",
    ])
