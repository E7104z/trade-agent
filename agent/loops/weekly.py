"""
WEEKLY LOOP (Sunday)

  full-ledger pattern mining -> promote/retire lessons -> REWRITE the playbook
  (the agent's accumulated skill, version-bumped and archived) -> report.

v3 (specs/WEEKLY_MINER_SPEC_v3.md): the miner is the SOLE writer of playbook.md.
It applies decay first, recomputes calibration, summarizes the shadow ledger
(without auto-loosening filters), reports progress on every open question,
enforces WR suppression below n=8 at write time, and commits the playbook to git.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import date

from .. import ledger, open_questions
from ..config import BASE_DIR, Config
from ..delivery import render_dashboard, send_email, weekly_email_html
from ..llm import LLMClient, load_prompt
from ..memory import build_weekly_memory, write_playbook
from ..util import is_half_day, log, now_et, today_et

STALE_SESSIONS = 30  # >= this many sessions without confirmation -> auto-demote


def _fmt_stats(stats: list[dict], label: str) -> str:
    lines = [f"### by {label}"]
    for s in stats:
        wr = f"{s['win_rate']:.0%}" if s["win_rate"] is not None else "-"
        extra = ""
        if s.get("t_stat") is not None:
            extra += f", stdR={s.get('std_r')}, t={s['t_stat']}"
        if s.get("baseline_avg_r") is not None:
            extra += f", naiveBaselineAvgR={s['baseline_avg_r']:+.2f}"
        lines.append(f"- {s['dim']}: n={s['n']}, triggered={s['triggered']}, wr={wr}, "
                     f"avgR={s['avg_r']}, totalWeightedR={s['total_weighted_r']}{extra}")
    return "\n".join(lines)


# Below this many observations a counterfactual average is reported as a number
# and nothing else: no direction, no reading, no verdict. Two diagnostics that
# disagree at n=1 are not a divergence to investigate — they are two numbers.
MIN_INTERPRETABLE_N = 8


def _stat(value, n: int, reading: str) -> str:
    """One counterfactual average, with its own n and a reading it only gets to
    keep once n supports one.

    Each average in this block is computed over a DIFFERENT subset of rows —
    triggered, triggered-with-baseline, triggered-with-eod — so they cannot
    share a single n. Printing one `triggered` count beside four numbers let
    the largest n attach itself to whichever number was most interesting.
    """
    if value is None or not n:
        return "not computable (n=0)"
    if n < MIN_INTERPRETABLE_N:
        return (f"{value} over n={n} — NOT INTERPRETABLE (below n={MIN_INTERPRETABLE_N}); "
                "report the number, state no direction, draw no contrast with any "
                "other counterfactual")
    return f"{value} over n={n} ({reading})"


def _cfg_sim_version(cfg):
    """The sim_version the ablation was restricted to, if any."""
    try:
        return cfg.get("ablation_sim_version")
    except Exception:
        return None


def _selection_edge_row(abl: dict, week_ending, sim_version=None) -> dict:
    """One week's selection-edge measurement, from the ablation result.

    Every number here is computed by agent/ablation.py; this only reshapes
    it. `edge_r` is actual minus the random baseline -- positive means
    selection added R over drawing the same number of picks at random from
    the same pool.

    The verdict is SUPPRESSED below MIN_VERDICT_N triggered picks, matching
    the floor the Calibration table already enforces. The percentile is
    exact at any n; what cannot be generalized at low n is the jump from
    "these picks" to "the selection process", which is what a verdict
    asserts.
    """
    from ..migrations_v13 import MIN_VERDICT_N

    n = int(abl.get("picks") or 0)
    actual = abl.get("actual_total_r")
    baseline = abl.get("random_mean_total_r")
    edge = (round(actual - baseline, 3)
            if actual is not None and baseline is not None else None)
    pct = abl.get("percentile")

    if n < MIN_VERDICT_N:
        verdict = f"insufficient data (n={n} < {MIN_VERDICT_N})"
    elif pct is None or edge is None:
        verdict = "not computable"
    elif pct >= 95.0:
        verdict = "selection beat random"
    elif pct <= 5.0:
        verdict = "selection UNDERPERFORMED random"
    else:
        verdict = "no detectable selection edge"

    return {
        "week_ending": week_ending,
        "n_triggered": n,
        "actual_total_r": actual,
        "random_mean_r": baseline,
        "edge_r": edge,
        "pct_random_beaten": pct,
        "p_value": abl.get("p_value"),
        "sessions": abl.get("sessions"),
        "verdict": verdict,
        "sim_version": sim_version,
    }


def _selection_edge_md(history: list[dict]) -> str:
    """The Selection Edge table for the miner to reproduce into playbook.md.

    Rendered in code rather than left to the miner because every cell is a
    deterministic figure the ablation already computed -- the miner's job is
    to carry it into the document, not to recompute or re-round it.

    Reported EVERY week, including weeks where n did not move, for the same
    reason Calibration reports its unchanged rows: a table that only appears
    when it has something to say cannot show a flat trend, and "no edge yet,
    still" is the finding most likely to be quietly dropped.
    """
    from ..migrations_v13 import MIN_VERDICT_N

    header = (
        "## SELECTION EDGE (tracked weekly — reproduce this table verbatim)\n\n"
        "| Week | n (triggered) | Selection edge (R vs. baseline) | "
        "% of random portfolios beaten | verdict |\n"
        "|-----:|--:|--:|--:|:--|")
    if not history:
        return (header + "\n| — | 0 | — | — | insufficient data (n=0 < "
                + str(MIN_VERDICT_N) + ") |\n\n"
                "(no weekly selection-edge measurement recorded yet)")

    lines = [header]
    for r in history:
        n = r.get("n_triggered") or 0
        # Below the floor the numeric cells are suppressed too, not just the
        # verdict -- an edge figure printed beside "insufficient data" is the
        # number a reader remembers, exactly the failure the Calibration
        # write-time check exists to catch.
        if n < MIN_VERDICT_N:
            edge_s, pct_s = "—", "—"
        else:
            edge_s = ("—" if r.get("edge_r") is None
                      else f"{r['edge_r']:+.2f}R")
            pct_s = ("—" if r.get("pct_random_beaten") is None
                     else f"{r['pct_random_beaten']:.1f}%")
        lines.append(
            f"| {r['week_ending']} | {n} | {edge_s} | {pct_s} | "
            f"{r.get('verdict') or '—'} |")
    return "\n".join(lines)


def _untriggered_md(mem: dict) -> str:
    """Per-setup breakdown of WHY picks never triggered (Phase 1).

    Deliberately reported even when every bucket is no_setup_offered: the
    miner needs to see that the entry mechanic was NOT the problem, not
    merely fail to see evidence that it was.
    """
    rows = mem.get("untriggered") or []
    if not rows:
        return ("## UNTRIGGERED PICKS - WHY\n\n"
                "(no untriggered picks on record)")
    by_setup: dict[str, list[dict]] = {}
    for r in rows:
        by_setup.setdefault(r["setup_type"], []).append(r)
    out = ["## UNTRIGGERED PICKS - WHY", "",
           "`no_setup_offered` = the market never presented the setup (says "
           "nothing about the rule). `entry_condition_not_met` = the setup "
           "arrived and the entry mechanic missed it (the ONLY split that "
           "argues an entry is too narrow). `trigger_gap_r` is how far short "
           "price fell, in units of that pick's own planned risk.", ""]
    for setup, items in sorted(by_setup.items()):
        total = sum(i["n"] for i in items)
        parts = ", ".join(
            f"{i['n']} {i['reason']}"
            + (f" (avg gap {i['avg_gap_r']:+.2f}R)"
               if i["avg_gap_r"] is not None else "")
            for i in items)
        out.append(f"- **{setup}**: {total} untriggered - {parts}")
    return "\n".join(out)


def _counterfactuals_md(mem: dict) -> str:
    """Null-model, trade-management, and sizing diagnostics — computed in code,
    never by the LLM. Edge must beat the baseline; management must beat holding."""
    m = mem.get("management", {})
    sc = mem.get("sizing_cf", {})
    stop_rate = (f"{m['stopped_then_target']}/{m['stopped']}"
                 if m.get("stopped") else "0/0")
    sizing_n = sc.get("n", 0) or 0
    sizing_read = f"{sc.get('sizing_edge_r')}R" if sizing_n else "not computable"
    if 0 < sizing_n < 20:
        sizing_read += f" over n={sizing_n} — below the n>=20 bar this metric is read at"
    elif sizing_n:
        sizing_read += f" over n={sizing_n}"
    return (
        "## DETERMINISTIC COUNTERFACTUALS (computed by the system, not the LLM)\n\n"
        f"Every average below carries ITS OWN n. Readings are suppressed below "
        f"n={MIN_INTERPRETABLE_N}: a suppressed metric may be quoted as a number, "
        "but you may not call it positive or negative, may not contrast two "
        "suppressed metrics against each other, and may not describe the gap "
        "between them as a pattern to watch. At n=1 there is no divergence "
        "between selection and management to explain — there is one trade.\n\n"
        "### Edge vs null model (naive same-name open->close, planned-risk units)\n"
        f"- triggered trades: {m.get('triggered', 0)}\n"
        f"- desk avgR: {_stat(m.get('avg_r'), m.get('avg_r_n', 0), 'realized R per triggered trade')}\n"
        f"- naive baseline avgR: {_stat(m.get('avg_baseline_r'), m.get('avg_baseline_r_n', 0), 'same names, open->close')}\n"
        "- EDGE VS BASELINE (paired — actual minus baseline on the SAME trades): "
        f"{_stat(m.get('edge_vs_baseline_r'), m.get('edge_vs_baseline_n', 0), '<= 0 means the plan machinery adds nothing over buying/shorting the same names at the open')}\n"
        f"- untriggered picks with baseline: {m.get('untriggered_with_baseline', 0)}, "
        f"avg baseline R walked away from: {m.get('avg_baseline_r_untriggered')} "
        "(large positive = entries too cute)\n\n"
        "### Trade management quality\n"
        "- management edge (paired — actual R minus hold-to-close R): "
        f"{_stat(m.get('management_edge_r'), m.get('management_edge_n', 0), 'negative = the stop/target pair is destroying value vs just holding')}\n"
        f"- stopped trades that hit target later same day: {stop_rate} "
        "(avg eod R of stopped trades: "
        f"{_stat(m.get('avg_eod_r_on_stopped'), m.get('avg_eod_r_on_stopped_n', 0), 'a high rate means stops sit inside the noise')})\n\n"
        "### Sizing counterfactual\n"
        f"- n={sizing_n}: actual size-weighted total {sc.get('actual_weighted_r')}R vs "
        f"flat-size {sc.get('flat_size_weighted_r')}R -> sizing edge {sizing_read} "
        "(negative at n>=20 = sizing decisions are destroying value; feed this into the calibration verdict)"
    )


def _sim_version_md(mem: dict) -> str:
    """Flag the Task 3/4 simulation-semantics cutover so the miner doesn't read
    exit-reason mix as one homogeneous pile across a boundary where the exit
    rules actually changed (time_stop went from unenforced to a hard exit)."""
    rows = mem.get("sim_version_breakdown", [])
    if len(rows) <= 1:
        return ""
    table = "\n".join(
        f"- sim_version {r['sim_version']}: n={r['n']} ({r['first_date']} to {r['last_date']})"
        for r in rows
    )
    return (
        "## SIMULATION SEMANTICS VERSION BOUNDARY\n\n"
        "The grading engine's exit rules changed mid-ledger (sim_version bumped). "
        "Rows below reflect DIFFERENT simulation rules — comparisons of exit-reason "
        "mix (e.g. \"stops are up\") ACROSS sim_version values are unsound; a shift "
        "in the mix may be nothing more than the rule change, not a real pattern. "
        "Compare within a single sim_version, or note the boundary explicitly if "
        "you must compare across it.\n\n"
        f"{table}"
    )


def _half_day_md(con) -> str:
    """Phase 1c: name every early-close (13:00 ET) session in the mined ledger.
    Same pattern as _sim_version_md — a half-day outcome was graded against a
    session that ended three hours early (eod at 13:00, late flatten/time_stop
    cutoffs inert), which is a different rule set arriving through a door
    sim_version doesn't watch. Checked in code against the calendar rather than
    the session_length column so rows graded before that column existed are
    still flagged."""
    try:
        rows = con.execute(
            "SELECT DISTINCT eval_date FROM outcomes WHERE eval_date IS NOT NULL ORDER BY 1"
        ).fetchall()
    except Exception:
        return ""
    half = [r[0] for r in rows if r[0] is not None and is_half_day(r[0])]
    if not half:
        return ""
    dates = ", ".join(d.isoformat() if hasattr(d, "isoformat") else str(d) for d in half)
    return (
        "## HALF-DAY SESSIONS IN THIS LEDGER\n\n"
        f"The following session(s) closed early at 13:00 ET: {dates}. Outcomes from "
        "them were graded against a three-hours-shorter session — eod exits resolved "
        "at 13:00, and any flatten_at/time_stop cutoff later than the close never "
        "fired. Exclude them from (or explicitly flag them in) any comparison of "
        "exit-reason mix, time-based management, or hold-duration effects."
    )


def _data_quality_md(con) -> str:
    """Phase 1f (Addendum A §A4): name every session where a validation gate
    failed OPEN because a data provider was unavailable. Picks from a
    provider_degraded session cleared fewer checks than picks from a clean
    session — a confound sim_version doesn't watch and the miner cannot see
    unless it is told (the 2026-08-27 $OKTA "possibly delisted" burst was
    almost certainly a yfinance degradation, not a real delisting)."""
    try:
        rows = con.execute(
            "SELECT plan_date, gates_failed_open FROM daily_plans "
            "WHERE provider_degraded = TRUE ORDER BY plan_date"
        ).fetchall()
    except Exception:
        return ""
    if not rows:
        return ""
    lines = []
    for plan_date, gates_json in rows:
        try:
            counts = json.loads(gates_json) if gates_json else {}
        except (TypeError, ValueError):
            counts = {}
        detail = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
        d_str = plan_date.isoformat() if hasattr(plan_date, "isoformat") else str(plan_date)
        lines.append(f"- {d_str}: {detail or '(counts unavailable)'}")
    return (
        "## DATA QUALITY\n\n"
        "Sessions flagged `provider_degraded` had one or more validation gates fail "
        "OPEN because a data provider was unavailable. Picks from those sessions "
        "cleared fewer checks than picks from clean sessions. Do not pool them into "
        "a gate-effect comparison without saying so, and never attribute a pattern "
        "to a gate on a session where that gate did not run.\n\n" + "\n".join(lines)
    )


def _degraded_sessions_md(con) -> str:
    """Name every session the pipeline could not EVALUATE, so the miner never
    counts an outage as a stand-down.

    A no-trade day has two entirely different causes -- "the market offered
    nothing" and "our plumbing offered nothing" -- and until data_health existed
    they were stored identically (plan prose saying "systemic data gaps" beside a
    run row saying status "ok", error ""). Pooling them means the miner is
    measuring a mislabeled class, which no amount of FDR correction repairs.
    Degraded sessions are still STORED -- the degraded rate is itself evidence --
    they are simply not countable as decisions.
    """
    try:
        rows = con.execute(
            "SELECT plan_date, data_health FROM daily_plans "
            "WHERE session_degraded = TRUE ORDER BY plan_date"
        ).fetchall()
    except Exception:
        return ""
    if not rows:
        return ""
    lines = []
    for plan_date, health_json in rows:
        try:
            health = json.loads(health_json) if health_json else {}
        except (TypeError, ValueError):
            health = {}
        why = "; ".join(health.get("degraded_reasons") or []) or "(reasons unavailable)"
        d_str = plan_date.isoformat() if hasattr(plan_date, "isoformat") else str(plan_date)
        lines.append("- " + d_str + ": " + why)
    return (
        "## DEGRADED SESSIONS (data outages, NOT stand-down decisions)\n\n"
        "On the sessions below the pipeline could not evaluate the market: prices "
        "never reached the analyst or the risk manager, so the resulting no-trade "
        "was a consequence of the plumbing, not a judgment about market quality. "
        "EXCLUDE these dates from any no-trade / stand-down rate, from any claim "
        "about the desk's selectivity or patience, and from rejection-reason "
        "frequency (their rejection rows are flagged session_degraded and are "
        "already excluded from the forgone-R-by-reason table above). Their "
        "counterfactual grades remain valid and still count. The degraded RATE "
        "itself is worth reporting -- as an operational finding, never as a "
        "behavioral one.\n\n" + "\n".join(lines)
    )


def _no_trade_reason_md(con) -> str:
    """PARTICIPATION_UNBLOCK_SPEC_v2 Phase 3: no-trade days classified by a
    closed enum (`daily_plans.no_trade_category`) instead of only free
    prose, so the miner can see whether standing down is chronically caused
    by R:R, by regime fit, by the mechanical validator, or by an empty
    universe -- each implies a different fix, and pooling them under one
    "no_trade" label hides which one actually needs it. `data_incomplete`
    days are counted here too (for the rate), but are the SAME sessions
    _degraded_sessions_md already tells the miner to exclude from any
    stand-down/selectivity judgment.
    """
    try:
        rows = con.execute(
            "SELECT no_trade_category, count(*) FROM daily_plans "
            "WHERE no_trade = TRUE AND no_trade_category IS NOT NULL "
            "GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
    except Exception:
        return ""
    if not rows:
        return ""
    lines = [f"- {cat}: {n}" for cat, n in rows]
    return (
        "## NO-TRADE DAY CLASSIFICATION\n\n"
        "Distribution of WHY no-trade days happened, by closed category: "
        "`all_failed_rr` (every live candidate died on R:R alone), "
        "`all_failed_regime` (regime fit was the dominant kill-check), "
        "`all_failed_validator` (mixed reasons, or the mechanical validator "
        "dropped picks the risk manager had approved), `empty_universe` (no "
        "candidates reached the risk manager at all), `data_incomplete` (the "
        "session was degraded -- see DEGRADED SESSIONS above, exclude from "
        "selectivity judgments). A `all_failed_rr` share that stays high "
        "while the exploration lane's admitted-picks count stays low is the "
        "signal that the R:R band itself, not just the blocklist, needs "
        "another look.\n\n" + "\n".join(lines)
    )


def _calibration_md(cal: list[dict]) -> str:
    """The calibration table, with WR/avgR suppressed below the playbook's own
    n>=8 reporting floor.

    Suppressed buckets are still PRINTED. The table's job before it is
    actionable is to show the desk that conviction is unproven, and a hidden
    table cannot say that — but a bucket holding one trade rendered as
    "wr=0.0, avgR=-0.05" says something much worse, which is that conviction 4
    loses money. Suppression happens in ledger.calibration_aggregate; this
    renderer only has to not re-derive what was suppressed.
    """
    if not cal:
        return "(no realized calibration rows yet)"
    lines = []
    for c in cal:
        if c.get("suppressed"):
            lines.append(
                f"- conviction {c['conviction']}: n={c['n']} — WR/avgR SUPPRESSED "
                f"(below n={c.get('min_n', 8)}). Report this bucket as "
                "'insufficient data'. Do not state or imply a direction for it.")
        else:
            lines.append(f"- conviction {c['conviction']}: n={c['n']}, "
                         f"wr={c['win_rate']}, avgR={c['avg_r']}")
    return "\n".join(lines)


def _shadow_md(sh: dict) -> str:
    """The shadow ledger, with the concentration arithmetic already done.

    `net forgone R` is a SUM over a fat-tailed quantity, so a handful of rows
    can author it outright. Reported alone it reads as the average price of the
    filter, which is what it is not: the filter can be right on the median
    candidate and still show a large positive net because three rejections ran.
    So the top rows, the net WITHOUT them, and the median row are computed and
    printed together — and when the tail and the remainder carry opposite
    signs, that is stated as the headline finding rather than left for the
    miner to notice.
    """
    # `reason` is verbatim prose, so this list is one line per rejection, not a
    # grouping. It is kept for detail and capped, because thirty paragraphs of
    # kill-note prose crowd out everything computed above them. The aggregate
    # that actually answers "which reason costs the most" is by_category.
    reasons = sh.get("by_reason", [])
    reasons_md = "\n".join(
        f"  - \"{r['reason'][:180]}\": n={r['n']}, net forgone {r['net_forgone_r']:+.2f}R"
        for r in reasons[:12]
    ) or "  (no scored rejections yet)"
    if len(reasons) > 12:
        reasons_md += (f"\n  - ... {len(reasons) - 12} further rows not shown. This "
                       "list is verbatim kill notes, one line per rejection — it is "
                       "detail, NOT a grouping. Do not count lines in it to decide "
                       "which reason is most expensive; use the category table above.")
    cats_md = "\n".join(
        f"  - {c['category']}: n={c['n']}, net forgone {c['net_forgone_r']:+.2f}R"
        for c in sh.get("by_category", [])
    ) or "  (no scored rejections yet)"
    top_md = "\n".join(
        f"  - {t['ticker']} ({t['eod_outcome']}, {t['reason']}): {t['forgone_r']:+.2f}R"
        for t in sh.get("top_rows", [])
    ) or "  (none)"

    net = sh.get("net_forgone_r", 0) or 0
    top3 = sh.get("top3_forgone_r", 0) or 0
    ex3 = sh.get("net_forgone_r_ex_top3", 0) or 0
    scored = sh.get("scored_rows", 0) or 0
    rest_n = max(0, scored - len(sh.get("top_rows", [])))

    if scored and (net > 0) and (ex3 < 0):
        concentration = (
            f"READ THIS BEFORE QUOTING THE NET: the headline {net:+.2f}R is a "
            f"TAIL ARTIFACT. The top 3 rejections contribute {top3:+.2f}R; the "
            f"other {rest_n} scored rejections net {ex3:+.2f}R — the filters SAVED "
            "money on everything outside the tail. A net this shape is evidence "
            "about three rows, not about the filter's typical cost, and it is not "
            "grounds for loosening anything."
        )
    elif scored and abs(top3) > abs(net):
        concentration = (
            f"CONCENTRATION WARNING: the top 3 rejections contribute {top3:+.2f}R "
            f"against a headline net of {net:+.2f}R, so the remaining {rest_n} "
            f"scored rejections net {ex3:+.2f}R. The sum is dominated by its tail; "
            "quote the median and the ex-tail net alongside it or not at all."
        )
    else:
        concentration = (
            f"Top-3 contribution {top3:+.2f}R; the other {rest_n} scored "
            f"rejections net {ex3:+.2f}R."
        )

    median = sh.get("median_forgone_r")
    median_s = f"{median:+.3f}R" if median is not None else "n/a"
    return (
        f"- trailing sessions with shadow data: {sh.get('sessions', 0)}\n"
        f"- total rejected: {sh.get('total', 0)} "
        f"(would_have_won {sh.get('would_have_won', 0)}, "
        f"would_have_lost {sh.get('would_have_lost', 0)}, "
        f"scratch {sh.get('scratch', 0)}, unmeasurable {sh.get('unmeasurable', 0)})\n"
        f"- NET FORGONE R (positive = filters cost us): {net:+.2f} "
        f"across {scored} scored rows\n"
        f"- MEDIAN forgone R per scored rejection: {median_s} "
        "(the typical rejection, which the sum does not describe)\n"
        f"- NET EXCLUDING the 3 largest: {ex3:+.2f}R\n"
        f"- {concentration}\n"
        f"- the 3 largest forgone-R rejections (cite THESE rows, not rows carried "
        f"over from an earlier week):\n{top_md}\n"
        f"- captured R by taken trades over same window: {sh.get('captured_r_window', 0):+.2f}\n"
        "- forgone R BY REJECTION CATEGORY (the closed enum — this is the "
        f"aggregate that answers Open Question #5):\n{cats_md}\n"
        "  (`uncategorized` = rows written before the category column existed; "
        "they are not evidence that no category applied, only that none was "
        "recorded. `other` is a real category meaning the kill-check was not R:R.)\n"
        f"- verbatim kill notes, largest first (detail, not a grouping):\n{reasons_md}"
    )


def _participation_md(pt: dict) -> str:
    """Is the desk taking enough trades to ever validate anything?

    The shadow ledger asks whether the rejections were right and the ablation
    asks whether the selections were right. Neither can see a filter stack that
    is collectively too tight while no single filter is individually wrong,
    because a desk that almost never trades produces a small, quiet, entirely
    unremarkable ledger — which is also what a correctly patient desk in a
    genuinely thin stretch produces. Only the funnel distinguishes them, and
    only if someone is looking at it.
    """
    if not pt or not pt.get("sessions_evaluated"):
        return "(no non-degraded sessions in the window yet)"
    pct = lambda v: "n/a" if v is None else f"{v:.1%}"  # noqa: E731
    per = pt.get("candidates_per_triggered_trade")
    lines = [
        f"- sessions evaluated (degraded sessions excluded): {pt['sessions_evaluated']}",
        f"- sessions with at least one triggered trade: {pt['sessions_traded']} "
        f"({pct(pt.get('session_trade_rate'))} of sessions)",
        f"- candidates evaluated: {pt['candidates_evaluated']} "
        f"({pt['picks_taken']} taken, {pt['rejections']} rejected)",
        f"- take rate (taken / evaluated): {pct(pt.get('take_rate'))}",
        f"- trigger rate (triggered / taken): {pct(pt.get('trigger_rate'))}",
        f"- candidates evaluated per triggered trade: "
        f"{per if per is not None else 'n/a (nothing triggered)'}",
    ]
    if pt.get("picks_triggered", 0) == 0 and pt["candidates_evaluated"] > 0:
        lines.append(
            "- STANDING QUESTION: the desk has evaluated candidates and triggered "
            "NOTHING in this window. Every edge metric in this report is therefore "
            "uncomputable by construction, and will stay uncomputable until "
            "participation rises. Treat 'is the filter stack collectively too "
            "tight?' as an open question in its own right — no single filter need "
            "be wrong for the conjunction to be.")
    elif per is not None and per >= 50:
        lines.append(
            f"- STANDING QUESTION: {per} candidates evaluated per triggered trade. "
            "That may be correct patience in a thin stretch, or a filter stack that "
            "is collectively too tight while no single filter is individually "
            "wrong. The distinction is not visible in the shadow ledger or the "
            "ablation; say which you believe and on what evidence.")
    return "\n".join(lines)


def _v3_sections(mem: dict) -> str:
    """Render calibration, shadow, staleness, and question-progress data for the miner."""
    cal_md = _calibration_md(mem.get("calibration", []))
    shadow_md = _shadow_md(mem.get("shadow", {}))
    participation_md = _participation_md(mem.get("participation", {}))

    stale = mem.get("rule_staleness", [])
    stale_md = "\n".join(
        f"- {r['rule_id']}: last_confirmed {r['last_confirmed']}, "
        f"{r['sessions_since']} sessions since"
        + ("  << STALE: AUTO-DEMOTE to UNVALIDATED" if r["sessions_since"] >= STALE_SESSIONS else "")
        for r in stale
    ) or "(no rule confirmations recorded yet — rules keep their current last_confirmed values)"

    qp = mem.get("question_progress", [])
    qp_md = "\n".join(
        f"- Q{q['question_id']}: +{q['total']} this ledger, last event {q['last_event']}; "
        f"recent notes: " + " | ".join(q["recent_notes"][:3])
        for q in qp
    ) or "(no open-question events yet — report Progress unchanged, do not silently drop any question)"

    return (
        f"## CALIBRATION ROWS (aggregate by stated conviction)\n\n{cal_md}\n\n"
        f"## SHADOW LEDGER (trailing 20 sessions)\n\n{shadow_md}\n\n"
        f"## PARTICIPATION FUNNEL (trailing 20 non-degraded sessions)\n\n{participation_md}\n\n"
        f"## RULE CONFIRMATIONS / STALENESS (decay threshold: {STALE_SESSIONS} sessions)\n\n{stale_md}\n\n"
        f"{open_questions.registry_md(qp)}\n\n"
        f"## OPEN QUESTION PROGRESS EVENTS THIS LEDGER (raw event notes)\n\n{qp_md}"
    )


# A markdown row from the Calibration table: | 4 | 1 | 0% | -0.05 | ... |
# The prose check below keys on "n=8"-style text and never fired on the one
# table in the playbook that reports a win rate and an avg R side by side, so
# "conviction 4 -> 0% WR, -0.05R" over a single trade passed every gate the
# document imposes on its own prose.
_CAL_ROW = re.compile(
    r"^\|\s*([1-5])\s*\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|")


def _check_calibration_table(playbook_md: str) -> list[str]:
    """Flag calibration rows that show a win rate or avg R below n=8.

    Same floor the rest of the document declares for itself; the table was
    simply shaped differently from anything the prose scanner could see.
    """
    violations = []
    empty = {"", "-", "—", "--", "n/a", "na", "insufficient data", "suppressed"}
    for line in (playbook_md or "").splitlines():
        m = _CAL_ROW.match(line.strip())
        if not m:
            continue
        n = int(m.group(2))
        wr, avg_r = m.group(3).strip().lower(), m.group(4).strip().lower()
        if n < 8 and (wr not in empty or avg_r not in empty):
            violations.append(
                f"calibration row conviction {m.group(1)} shows wr={m.group(3).strip()} "
                f"avgR={m.group(4).strip()} at n={n} (floor is n=8): {line.strip()[:160]}")
    return violations


# A markdown row from the Selection Edge table:
# | 2026-09-18 | 25 | -18.70R | 12.6% | no detectable selection edge |
_SE_ROW = __import__("re").compile(
    r"^[|]\s*(\d{4}-\d{2}-\d{2})\s*[|]\s*(\d+)\s*[|]\s*([^|]+?)\s*[|]\s*([^|]+?)\s*[|]")


def _check_selection_edge_table(playbook_md: str) -> list[str]:
    """Flag Selection Edge rows showing an edge or percentile below the
    n floor.

    Same failure mode the Calibration table had: a real-looking number
    printed beside "insufficient data" is the number a reader remembers.
    The floor is enforced at write time rather than trusted to the miner,
    because the miner is an LLM reproducing a table and a reproduction that
    drops the suppression is indistinguishable from one that keeps it until
    someone reads the document.
    """
    from ..migrations_v13 import MIN_VERDICT_N

    violations = []
    empty = {"", "-", chr(8212), "--", "n/a", "na", "insufficient data", "suppressed"}
    for line in (playbook_md or "").splitlines():
        m = _SE_ROW.match(line.strip())
        if not m:
            continue
        n = int(m.group(2))
        edge, pct = m.group(3).strip().lower(), m.group(4).strip().lower()
        if n < MIN_VERDICT_N and (edge not in empty or pct not in empty):
            violations.append(
                f"selection-edge row {m.group(1)} shows edge={m.group(3).strip()} "
                f"pct={m.group(4).strip()} at n={n} (floor is n={MIN_VERDICT_N}): "
                f"{line.strip()[:160]}")
    return violations


# The provisional-status banner that must open ## Sizing Policy while any
# [UNVALIDATED prior] tag remains in it. Matched on a distinctive phrase
# rather than the whole sentence so trivial whitespace/wrapping differences
# do not produce a false violation.
_SIZING_BANNER_PHRASE = "defaults carried from inception, not derived limits"


def _sizing_policy_section(playbook_md: str) -> str:
    """The text of ## Sizing Policy, through to the next h2."""
    lines = (playbook_md or "").splitlines()
    out, inside = [], False
    for line in lines:
        if line.strip().lower().startswith("## sizing policy"):
            inside = True
            continue
        if inside and line.strip().startswith("## "):
            break
        if inside:
            out.append(line)
    return "\n".join(out)


def _check_sizing_banner(playbook_md: str) -> list[str]:
    """Phase 3: the UNVALIDATED banner must be present exactly once.

    Two failure modes, both real:

      MISSING   the section still carries [UNVALIDATED prior] constraints
                while reading like measured policy. These govern how much
                money each trade risks, which makes them the most
                consequential untested content in the document.

      DUPLICATE the same defect the Changelog had (Phase 0) -- a section the
                miner writes AND the template supplies appears twice. The
                banner is checked for duplication specifically because the
                document has already shipped one duplicated section.

    The banner is only required while an [UNVALIDATED prior] tag remains: if
    every sizing rule earns its evidence, the warning should disappear rather
    than becoming furniture nobody reads.
    """
    section = _sizing_policy_section(playbook_md)
    if not section.strip():
        return []
    # Normalise before matching: the banner is a blockquote and will be
    # line-wrapped, so "defaults carried from inception" is split across a
    # newline and a "> " marker in the real document. Matching the raw text
    # reported the banner missing while it sat directly above -- a checker
    # that fires on correct input is worse than no checker.
    flat = " ".join(section.replace(">", " ").split())
    count = flat.count(_SIZING_BANNER_PHRASE)
    has_unvalidated = "[UNVALIDATED prior]" in section
    violations = []
    if has_unvalidated and count == 0:
        violations.append(
            "Sizing Policy carries [UNVALIDATED prior] rules but is missing the "
            "provisional-status banner")
    if count > 1:
        violations.append(
            f"Sizing Policy banner appears {count} times; it must appear exactly once")
    return violations


def _check_wr_suppression(playbook_md: str) -> list[str]:
    """Write-time enforcement:
    (a) spec Sec5 (original check) -- flag any line carrying wr/avgR at n < 8.
    (b) Task 5 #5 -- flag any line making an effect-size claim (avgR=<number>)
        that doesn't ALSO cite n, t, and stdR. stats_by computes all three
        deterministically; the miner must cite them, never derive its own
        read of significance. Flagged the same way small-sample win rates are.
    """
    violations = []
    for line in playbook_md.splitlines():
        stripped = line.strip()[:160]
        n_match = re.search(r"n\s*=\s*(\d+)", line)
        has_effect_claim = re.search(r"avgR\s*=\s*[+-]?\d", line, re.IGNORECASE)
        if n_match and int(n_match.group(1)) < 8 and re.search(r"(wr\s*=|avgR\s*=\s*[+-]?\d)", line, re.IGNORECASE):
            violations.append(stripped)
        if has_effect_claim:
            missing = [tok for tok in ("n", "t", "stdR")
                      if not re.search(rf"\b{tok}\s*=\s*[+-]?\d", line, re.IGNORECASE)]
            if missing:
                violations.append(f"{stripped}  [missing: {', '.join(missing)}]")
    return violations


def _git_commit_playbook(version: str) -> None:
    """Best-effort: commit the rewritten playbook so its evolution is a readable diff."""
    if not (BASE_DIR / ".git").exists():
        log("weekly", "No git repo — skipping playbook commit (git init to enable diff history)")
        return
    try:
        subprocess.run(["git", "add", "playbook.md", "playbook_history"],
                       cwd=BASE_DIR, check=True, capture_output=True, timeout=30)
        subprocess.run(["git", "commit", "-m", f"playbook v{version} — weekly miner rewrite"],
                       cwd=BASE_DIR, check=True, capture_output=True, timeout=30)
        log("weekly", f"Playbook v{version} committed to git")
    except Exception as e:
        log("weekly", f"Git commit skipped: {e}")


def run(cfg: Config, run_date: date | None = None) -> None:
    d = run_date or today_et()
    started = now_et().replace(tzinfo=None)
    con = ledger.connect()
    lock = None

    if not cfg.mock:
        lock = ledger.acquire_run_lock(con, d, "weekly")
        if not lock["acquired"]:
            log("weekly", f"Duplicate trigger suppressed for {d} (lock status={lock['status']})")
            con.close()
            return

    try:
        mem = build_weekly_memory(con, cfg)
        overall = mem["overall"]

        # v4: research integrity block (significance/FDR, quarantine, population
        # baseline, selection ablation, paired-arm comparison) — computed BEFORE
        # the miner call so `states` reflects exactly what the miner was shown.
        from .. import integrity
        from .. import ledger as ledger_mod
        research_block = integrity.weekly_block(con, cfg, ledger_mod, d)

        # Phase 2: record this week's selection edge as a tracked row, then
        # hand the whole history to the miner. Persisted BEFORE the LLM call
        # so the measurement survives even if the call fails -- the figure is
        # deterministic and does not depend on anything the miner says.
        try:
            se_row = _selection_edge_row(
                research_block.get("ablation") or {}, d,
                sim_version=_cfg_sim_version(cfg))
            ledger.save_selection_edge(con, se_row)
        except Exception as e:
            log("weekly", f"WARN could not record selection edge: {e}")
        selection_edge_md = _selection_edge_md(
            ledger.selection_edge_history(con))

        log("weekly", f"Mining ledger: {overall['picks_graded']} graded picks, "
                      f"{len(mem['lessons'])} lessons on file, playbook v{mem['playbook_version']}")

        lessons_md = "\n".join(
            f"- [L{l['lesson_id']}] status={l['status']}, conf={l['confidence']}, "
            f"+{l['confirmations']}/-{l['contradictions']}, source={l['source']}, "
            f"setup={l['setup_type'] or 'any'}, "
            f"holdout_tier={l['tier']} (n={l['confirmations_in_sample'] + l['confirmations_out_of_sample']} "
            f"({l['confirmations_out_of_sample']} out-of-sample), promoted_at={l['promoted_at']}): {l['lesson']}"
            for l in mem["lessons"]
        ) or "(no lessons recorded yet)"

        regimes_md = "\n".join(
            f"- {r['date']}: {r['tag']}" for r in mem["regimes"]
        ) or "(no regime history)"

        system = load_prompt("weekly_miner", {})
        user = (
            f"# WEEKLY REVIEW — {d.isoformat()}\n\n"
            f"## OVERALL RECORD\n\n{json.dumps(overall, indent=2)}\n\n"
            f"{_fmt_stats(mem['by_setup'], 'setup_type')}\n\n"
            f"{_fmt_stats(mem['by_regime'], 'regime_tag')}\n\n"
            f"{_fmt_stats(mem['by_conviction'], 'conviction')}\n\n"
            f"{_fmt_stats(mem['by_direction'], 'direction')}\n\n"
            f"{_fmt_stats(mem['by_rvol_bucket'], 'rvol_bucket (RVOL@10:00 vs ADV20)')}\n\n"
            f"{_fmt_stats(mem['by_vwap_side'], 'vwap_side_at_fill')}\n\n"
            f"{_fmt_stats(mem['by_stop_atr_bucket'], 'stop_atr_bucket (Q9)')}\n\n"
            f"{_fmt_stats(mem['by_gap_atr_bucket'], 'gap_atr_bucket (Q10)')}\n\n"
            f"{_fmt_stats(mem['by_ext_bucket'], 'ext_bucket')}\n\n"
            f"{_fmt_stats(mem['by_trend_side_200d'], 'trend_side_200d (Q11)')}\n\n"
            f"{_fmt_stats(mem['by_mcap_bucket'], 'mcap_bucket')}\n\n"
            f"{_fmt_stats(mem['by_short_bucket'], 'short_bucket (Q12)')}\n\n"
            f"{_fmt_stats(mem['by_or_alignment'], 'or_alignment')}\n\n"
            f"{selection_edge_md}\n\n"
            f"{_untriggered_md(mem)}\n\n"
            f"{_counterfactuals_md(mem)}\n\n"
            f"{_v3_sections(mem)}\n\n"
            f"{_sim_version_md(mem)}\n\n"
            f"{_half_day_md(con)}\n\n"
            f"{_data_quality_md(con)}\n\n"
            f"{_degraded_sessions_md(con)}\n\n"
            f"{_no_trade_reason_md(con)}\n\n"
            f"{research_block['markdown']}\n\n"
            f"## FULL TRADE LEDGER (CSV)\n\n```\n{mem['trades_csv']}\n```\n\n"
            f"## LESSON BANK (with IDs for your lesson_actions)\n\n"
            "Each lesson's `holdout_tier` is gated on OUT-OF-SAMPLE confirmations only "
            "(session date after promoted_at) -- moderate/established requires that n "
            "independent of in-sample n. Cite `holdout_tier` when promoting a lesson into "
            "a rule; do not promote past what holdout_tier supports.\n"
            + (f"Lesson-confirmation holdout window: {int(cfg.get('lesson_holdout_sessions', 0) or 0)} "
               "trailing sessions withheld from BOTH in-sample and out-of-sample counts.\n\n"
               if cfg.get("lesson_holdout_sessions") else "\n")
            + f"{lessons_md}\n\n"
            f"## REGIME HISTORY (last 30 reads)\n\n{regimes_md}\n\n"
            f"## CURRENT PLAYBOOK (v{mem['playbook_version']}) — you are rewriting this\n\n"
            f"{mem['playbook']}\n\n"
            "Apply decay FIRST, then mine the ledger and produce the full output contract now."
        )

        model = cfg.models["weekly"]
        log("weekly", f"Pattern-mining call ({model})...")
        llm = LLMClient(cfg)
        result = llm.call(system=system, user=user, model=model,
                          mock_name="weekly_miner", thinking=True, web_search=False)
        mined = result.parsed or {}

        if cfg.mock:
            # mocks/weekly_miner.json is a canned fixture. This loop is the SOLE
            # writer of playbook.md and commits it to git -- persisting a mock
            # response here would overwrite the agent's real accumulated playbook
            # with placeholder content and commit that over real history, on top
            # of writing fake lesson promotions/retirements. Same failure mode as
            # the morning/evening mock-write bug, but with a much bigger blast
            # radius (playbook.md, git history), so it gets the same hard stop.
            log("weekly", f"MOCK RUN — playbook NOT rewritten, no lesson actions applied, "
                          f"no email sent. Would-be summary: {mined.get('summary_for_email', '(none)')}")
            ledger.log_run(con, "weekly", d, started, "mock", model, result.usage)
            return

        # apply lesson actions
        for act in mined.get("lesson_actions", []) or []:
            lid, action = act.get("lesson_id"), act.get("action")
            if not isinstance(lid, int):
                continue
            if action == "promote":
                ledger.set_lesson_status(con, lid, "promoted")
            elif action == "retire":
                ledger.set_lesson_status(con, lid, "retired")
            log("weekly", f"Lesson L{lid}: {action} — {act.get('reason','')}")

        for l in mined.get("new_lessons", []) or []:
            lid = ledger.insert_lesson(con, l, "weekly", d)
            log("weekly", f"New mined lesson L{lid}: {l['lesson']}")

        # v4: register/quarantine any new rule candidates the miner nominated.
        # Nomination only — the miner cannot promote a rule to tradable status;
        # only research.eligible_for_production (already applied inside
        # integrity.weekly_block, above) graduates a rule out of quarantine.
        from .. import research
        for c in mined.get("rule_candidates", []) or []:
            reg = research.register_rule(
                con, c.get("statement", ""), c.get("dimensions", []), d,
                discovery_n=c.get("discovery_n", 0),
                discovery_effect=c.get("discovery_effect"),
                discovery_q=c.get("discovery_q"))
            if reg["state"] == "DISCOVERED":
                research.advance_quarantine(con, reg["rule_id"], d)
            log("weekly", f"Rule {reg['rule_id']}: {reg['state']} — {c.get('statement','')[:80]}")

        # rewrite the playbook (this loop is the SOLE writer of playbook.md)
        new_pb = mined.get("playbook_markdown", "")
        if new_pb and len(new_pb) > 200:
            wr_violations = _check_wr_suppression(new_pb)
            for v in wr_violations:
                log("weekly", f"WARN WR-suppression violation (n<8 with wr/avgR shown): {v}")
            for v in _check_calibration_table(new_pb):
                log("weekly", f"WARN calibration-table suppression violation: {v}")
            for v in _check_selection_edge_table(new_pb):
                log("weekly", f"WARN selection-edge suppression violation: {v}")
            for v in _check_sizing_banner(new_pb):
                log("weekly", f"WARN sizing-policy banner violation: {v}")
            # A pre-registered threshold the weekly rewrite can restate is not
            # pre-registered. agent/open_questions.py holds the declared bars;
            # this is where a rewrite that moved one becomes visible.
            for v in open_questions.check_playbook_questions(new_pb):
                log("weekly", f"WARN open-question registry violation: {v}")
            for v in integrity.check_playbook(new_pb, research_block["states"]):
                log("weekly", f"WARN playbook violation: {v}")
            version = write_playbook(new_pb, mined.get("changelog", ""), con)
            log("weekly", f"Playbook rewritten -> v{version} (old version archived)")
            if not os.environ.get("DATABASE_URL"):
                _git_commit_playbook(version)
        else:
            log("weekly", "WARN: miner returned no usable playbook — keeping current version")

        ledger.log_run(con, "weekly", d, started, "ok", model, result.usage)
        ledger.finish_run_lock(con, d, "weekly", lock["request_id"], "complete")

        log("weekly", f"Summary: {mined.get('summary_for_email','(none)')}")
        if mined.get("current_best_edge"):
            log("weekly", f"Current best edge: {mined['current_best_edge']}")
        for f in mined.get("key_findings", []) or []:
            log("weekly", f"  Finding: {f.get('finding','')} (n={f.get('n','?')}, {f.get('strength','')})")
        for ex in mined.get("experiments", []) or []:
            log("weekly", f"  Experiment: {ex}")
        log("weekly", f"Tokens ~${result.usage['cost_usd']:.3f}")

        send_email(cfg, f"Trade Agent — Weekly Review {d.isoformat()}", weekly_email_html(mined))
        render_dashboard(con, cfg, None)

    except Exception as e:
        if lock:
            ledger.finish_run_lock(con, d, "weekly", lock["request_id"], "failed")
        ledger.log_run(con, "weekly", d, started, "error", error=str(e))
        log("weekly", f"ERROR: {e}")
        raise
    finally:
        con.close()
