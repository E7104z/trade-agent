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
    """One Comparison per (dimension, bucket) — every one of these is a look."""
    out: list[Comparison] = []
    for dim in MINING_DIMENSIONS:
        buckets: dict[str, list[float]] = {}
        for r in rows:
            buckets.setdefault(bucket_of(dim, r), []).append(float(_planned_r(r)))
        for level, values in sorted(buckets.items()):
            mean, std = _mean_std(values)
            out.append(Comparison(
                dimension=dim, level=level, n=len(values),
                mean_r=mean, std_r=std, n_dimensions=n_dimensions,
                note="null/unmeasured bucket" if level == "?" else "",
            ))
    return out


def setup_edge(rows: list[dict], min_n: int = 8) -> dict:
    """Mean R across ALL candidates vs. zero. Does the setup have edge at all?"""
    values = [float(_planned_r(r)) for r in rows]
    mean, std = _mean_std(values)
    t, df, p = one_sample_t(mean, std, len(values), 0.0)
    return {
        "n": len(values), "mean_r": mean, "std_r": std,
        "t_stat": t, "df": df, "p_value": p,
        "underpowered": len(values) < min_n,
        "comparison": Comparison(
            dimension="runner_setup_edge", level="all_candidates",
            n=len(values), mean_r=mean, std_r=std,
            note="mean R across every runner candidate vs zero"),
    }


def selection_edge(rows: list[dict], min_n: int = 8) -> dict:
    """Mean R of selected vs. unselected, restricted to sessions where a real
    choice was made.

    Sessions where every candidate was auto-selected (candidates <= max_picks)
    carry no selection signal — there was no choice — so including them would
    dilute the comparison with rows that answer a different question. Only
    sessions containing BOTH a selected and an unselected candidate count.
    """
    by_session: dict[Any, dict[str, list[float]]] = {}
    for r in rows:
        d = r.get("session_date")
        side = "selected" if r.get("selected") else "unselected"
        by_session.setdefault(d, {"selected": [], "unselected": []})[side].append(
            float(_planned_r(r)))

    contested = {d: v for d, v in by_session.items()
                 if v["selected"] and v["unselected"]}
    sel = [x for v in contested.values() for x in v["selected"]]
    unsel = [x for v in contested.values() for x in v["unselected"]]

    m_sel, s_sel = _mean_std(sel)
    m_unsel, s_unsel = _mean_std(unsel)
    t, df, p = welch_t(m_sel, s_sel, len(sel), m_unsel, s_unsel, len(unsel))

    delta = (m_sel - m_unsel) if (m_sel is not None and m_unsel is not None) else None
    underpowered = len(sel) < min_n or len(unsel) < min_n
    if underpowered:
        verdict = (f"underpowered - {len(sel)} selected / {len(unsel)} unselected "
                   f"across {len(contested)} contested session(s), need n>={min_n} a side")
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
              versions: list[dict] | None = None) -> str:
    """Markdown for the weekly miner prompt. `versions` is `version_mix()`."""
    pin = version_mix_md(versions)
    if not setup.get("n"):
        return ("## Runner lane\n\n"
                "No triggered runner candidates yet — no setup or selection "
                "comparison is possible."
                + (f"\n\n{pin}" if pin else ""))

    lines = [
        "## Runner lane (setup_type `dto_runner`)",
        "",
    ]
    if pin:
        lines += [pin, ""]
    lines += [
        "### 1. Setup edge — every candidate, traded or not, vs zero",
        f"- n = {setup['n']} triggered candidates",
        f"- mean R = {_fmt(setup['mean_r'])} (sd {_fmt(setup['std_r'])})",
        f"- p = {_fmt(setup['p_value'], 4)}"
        + ("  **UNDERPOWERED**" if setup["underpowered"] else ""),
        "",
        "### 2. Selection edge — did picking help?",
        f"- contested sessions (both a selected and an unselected candidate): "
        f"{selection['sessions_contested']}",
        f"- selected:   n = {selection['n_selected']}, "
        f"mean R = {_fmt(selection['mean_r_selected'])}",
        f"- unselected: n = {selection['n_unselected']}, "
        f"mean R = {_fmt(selection['mean_r_unselected'])}",
        f"- delta = {_fmt(selection['delta_r'])} R, p = {_fmt(selection['p_value'], 4)}",
        f"- **{selection['verdict']}**",
        "",
        "A null result here is a real finding. If selection edge is not "
        "distinguishable from zero, the correct conclusion is that selection "
        "adds nothing and the right action is to trade the full filtered list "
        "or a random N — do not write a playbook rule that assumes otherwise.",
        "",
        "### 3. Candidate dimensions",
        "",
        "| dimension | bucket | n | mean R | q |",
        "|---|---|---|---|---|",
    ]
    for c in comparisons:
        if c.n < 3:
            continue
        lines.append(
            f"| {c.dimension} | {c.level} | {c.n} | {_fmt(c.mean_r)} | "
            f"{_fmt(c.q_value, 4)}{' *' if c.significant else ''} |")
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
    if res["sessions"] < 3:
        verdict = ("NO VERDICT -- under 3 contested sessions; the percentile "
                   "is about these picks, not the runner selector")
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
