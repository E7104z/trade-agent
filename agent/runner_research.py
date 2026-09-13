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
from datetime import date, timedelta
from typing import Any

from .stats import Comparison, one_sample_t, welch_t

__all__ = [
    "MINING_DIMENSIONS",
    "candidate_rows",
    "bucket_of",
    "candidate_comparisons",
    "setup_edge",
    "selection_edge",
    "runner_md",
]

# Mining dimensions (spec 2.6). Each is a deterministic bucketing of a stored
# candidate column — no LLM derives any of these.
MINING_DIMENSIONS = (
    "runner_detection_bucket",
    "runner_rvol_bucket",
    "runner_price_tier",
    "runner_vwap_bucket",
    "runner_slope_bucket",
    "runner_tc_bucket",
    "runner_float_bucket",
    "runner_regime",
)


def _mean_std(values: list[float]) -> tuple[float | None, float | None]:
    n = len(values)
    if n == 0:
        return None, None
    mean = sum(values) / n
    if n < 2:
        return mean, None
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean, math.sqrt(var)


def bucket_of(dimension: str, row: dict) -> str:
    """Deterministic bucket label for one candidate row.

    '?' is the null/unmeasured bucket and is kept rather than dropped — a large
    '?' bucket is itself a finding about data coverage, and silently discarding
    it would make coverage gaps invisible.
    """
    def _b(value, edges, labels):
        if value is None:
            return "?"
        for edge, label in zip(edges, labels):
            if value < edge:
                return label
        return labels[-1]

    if dimension == "runner_detection_bucket":
        return _b(row.get("detection_minutes"), [600, 630, 660, 720],
                  ["pre-10:00", "10:00-10:30", "10:30-11:00", "11:00-12:00", "12:00+"])
    if dimension == "runner_rvol_bucket":
        return _b(row.get("rvol"), [2.0, 3.0, 5.0],
                  ["1.5-2", "2-3", "3-5", "5+"])
    if dimension == "runner_price_tier":
        return row.get("price_tier") or "?"
    if dimension == "runner_vwap_bucket":
        return _b(row.get("vwap_pct_above"), [1.0, 1.5, 2.5],
                  ["0.5-1", "1-1.5", "1.5-2.5", "2.5+"])
    if dimension == "runner_slope_bucket":
        # Slope normalized by price: a $0.04 slope means something different on
        # a $6 stock than on a $45 one, so the raw dollar value is not
        # comparable across the price band.
        slope, price = row.get("ema9_slope"), row.get("detection_price")
        norm = (slope / price * 100) if (slope is not None and price) else None
        return _b(norm, [0.05, 0.10, 0.20], ["<0.05%", "0.05-0.10%", "0.10-0.20%", "0.20%+"])
    if dimension == "runner_tc_bucket":
        return _b(row.get("trend_consistency"), [55, 65, 75, 85],
                  ["45-55", "55-65", "65-75", "75-85", "85+"])
    if dimension == "runner_float_bucket":
        return _b(row.get("float_shares"), [20, 50, 100],
                  ["<20M", "20-50M", "50-100M", "100M+"])
    if dimension == "runner_regime":
        return row.get("regime_tag") or "?"
    return "?"


def candidate_rows(con: Any, sessions: int = 60,
                   scan_version: int | None = None) -> list[dict]:
    """Joined candidate + outcome rows that actually triggered.

    Only triggered rows carry an R multiple, so only they can enter a mean-R
    comparison. Rows are filtered by `scan_version` when one is given: never
    compare across scan_version values, exactly as with sim_version for
    grading — a detection-semantics change makes rows describe different rules.
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
        if not r.get("triggered") or r.get("r_multiple") is None:
            continue
        out.append(r)
    return _attach_regime(con, out)


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
            buckets.setdefault(bucket_of(dim, r), []).append(float(r["r_multiple"]))
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
    values = [float(r["r_multiple"]) for r in rows]
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
            float(r["r_multiple"]))

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
        "comparison": Comparison(
            dimension="runner_selection_edge", level="selected_vs_unselected",
            n=min(len(sel), len(unsel)), mean_r=delta,
            std_r=s_sel, t_stat=t, df=df, p_value=p,
            note=verdict[:400]),
    }


def _fmt(v, digits: int = 3) -> str:
    return "n/a" if v is None else f"{v:.{digits}f}"


def runner_md(setup: dict, selection: dict, comparisons: list[Comparison]) -> str:
    """Markdown for the weekly miner prompt."""
    if not setup.get("n"):
        return ("## Runner lane\n\n"
                "No triggered runner candidates yet — no setup or selection "
                "comparison is possible.")

    lines = [
        "## Runner lane (setup_type `dto_runner`)",
        "",
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
