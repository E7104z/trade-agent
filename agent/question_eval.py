"""
Pre-registered analysis for the open-question registry (playbook audit, 2026-09-29).

WHY THIS EXISTS
---------------
agent/open_questions.py fixed each question's id and threshold in code, so the
weekly rewrite could no longer move a bar. It left the ANSWER to the miner. The
miner then answered questions the data could not answer, in four shapes, all
in one playbook (v9):

  * a threshold met on one population, a finding drawn from another -- Q9
    counted the morning lane's stopped trades to 20/8, then answered with the
    runner lane's stop buckets; Q1 counted untriggered observations to 15/8
    while its own finding said one comparison existed;
  * a cross-arm conclusion from one arm -- Q13 ("filters are not the
    problem") with its paired arm Q14 at 6/20;
  * a contrast that is noise -- Q3's conviction 3 vs 2 gap of 0.51R with a
    standard error of ~0.54R (t ~ 0.9), stated as a finding;
  * THRESHOLD MET on counts the question could not use -- Q5 with most of its
    rejections uncategorized.

So each question now carries its analysis in code. `evaluate` computes, for
every question: the progress the threshold is compared against (for a two-arm
question, the SMALLER arm), whether it is answerable, and -- only when it is --
the result, with its test. p-values of the answerable tests are FDR-corrected
together, the same posture as the weekly search. The playbook's Open Questions
section is rendered from this by the system; the miner no longer writes it.

Morning lane only, fill-valid only, realized R = COALESCE(r_planned,
r_multiple) -- except where a question compares against the shadow ledger
(Q13/Q14), where both arms use r_multiple, the shadow simulator's measure.
"""
from __future__ import annotations

import math
import re
from typing import Any

from .open_questions import OPEN_QUESTIONS
from .stats import benjamini_hochberg, one_sample_t, student_t_sf, welch_t

__all__ = ["evaluate", "answers_md", "playbook_md", "Q_FDR"]

Q_FDR = 0.10
ARM_MIN = 8


def _mean_std(vals: list[float]) -> tuple[float | None, float | None]:
    n = len(vals)
    if not n:
        return None, None
    m = sum(vals) / n
    if n < 2:
        return m, None
    return m, math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))


def _arm(label: str, vals: list[float]) -> dict:
    m, s = _mean_std(vals)
    return {"label": label, "n": len(vals), "mean": m, "std": s}


def _fmt_r(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.2f}R"


# ------------------------------------------------------------------ data

def _morning_trades(con: Any) -> list[dict]:
    """Every morning-lane pick with its outcome, fill-valid only."""
    from .ledger import RUNNER_SETUP
    cols = ["plan_date", "ticker", "direction", "setup_type", "conviction", "thesis",
            "entry_price", "stop_price", "target_price", "size_r", "exploration_lane",
            "triggered", "exit_reason", "r_planned", "r_multiple", "rvol_at_1000",
            "stop_dist_atr", "trend_side_200d", "short_pct_float",
            "target_hit_after_stop", "eod_r_planned", "eod_r"]
    rows = con.execute(
        f"""SELECT p.plan_date, p.ticker, p.direction, p.setup_type, p.conviction, p.thesis,
                   p.entry_price, p.stop_price, p.target_price, p.size_r, p.exploration_lane,
                   o.triggered, o.exit_reason, o.r_planned, o.r_multiple, o.rvol_at_1000,
                   o.stop_dist_atr, o.trend_side_200d, o.short_pct_float,
                   o.target_hit_after_stop, o.eod_r_planned, o.eod_r
            FROM picks p JOIN outcomes o ON o.pick_id = p.pick_id
            WHERE COALESCE(p.setup_type, '') <> '{RUNNER_SETUP}'
              AND COALESCE(o.exit_reason, '') NOT IN ('invalid_fill', 'no_data', 'invalid_plan')
              AND COALESCE(o.execution_anomaly, FALSE) = FALSE
            ORDER BY p.plan_date, p.pick_id""").fetchall()
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        v = d["r_planned"] if d["r_planned"] is not None else d["r_multiple"]
        d["r"] = float(v) if (d["triggered"] and v is not None) else None
        out.append(d)
    return out


def _triggered(trades: list[dict]) -> list[dict]:
    return [t for t in trades if t["r"] is not None]


def _planned_rr(t: dict) -> float | None:
    try:
        entry, stop, target = (float(t[k]) for k in ("entry_price", "stop_price", "target_price"))
    except (TypeError, ValueError, KeyError):
        return None
    risk = abs(entry - stop)
    return abs(target - entry) / risk if risk > 0 else None


_NUM = r"([+-]?\d+(?:\.\d+)?)"


def _progress_rows(con: Any, qid: int) -> list[dict]:
    from .ledger import question_progress_rows
    try:
        return question_progress_rows(con, qid)
    except Exception:
        return []


# -------------------------------------------------------------- per question

def _two_arm(q: dict, a_label: str, a: list[float], b_label: str, b: list[float],
             min_arm: int = ARM_MIN) -> dict:
    arms = [_arm(a_label, a), _arm(b_label, b)]
    progress = min(len(a), len(b))
    need = max(min_arm, 0)
    out = {"arms": arms, "progress": progress, "target": need}
    if progress < need:
        short = [x for x in arms if x["n"] < need]
        out["reason"] = ("each arm needs n >= " + str(need) + "; "
                         + ", ".join(f"{x['label']} has {x['n']}" for x in short))
        return out
    t, df, p = welch_t(arms[0]["mean"], arms[0]["std"], arms[0]["n"],
                       arms[1]["mean"], arms[1]["std"], arms[1]["n"])
    out.update(answerable=p is not None, t=t, p=p,
               delta=(arms[0]["mean"] - arms[1]["mean"]),
               reason=None if p is not None else "zero variance in an arm; untestable")
    return out


def _q1(con: Any, q: dict, _trades: list[dict]) -> dict:
    diffs, sources = [], {"taken": 0, "shadow": 0, "unlabelled": 0}
    pat = re.compile(r"planned-entry R=" + _NUM + r".*?open-print R=" + _NUM)
    for row in _progress_rows(con, 1):
        m = pat.search(row["note"])
        if not m:
            continue
        diffs.append(float(m.group(1)) - float(m.group(2)))
        src = re.search(r"\[(taken|shadow)\]", row["note"])
        sources[src.group(1) if src else "unlabelled"] += 1
    m, s = _mean_std(diffs)
    out = {"progress": len(diffs), "target": q["threshold"],
           "arms": [{"label": "planned minus open-print (paired)", "n": len(diffs),
                     "mean": m, "std": s}],
           "detail": ", ".join(f"{k} {v}" for k, v in sources.items() if v)}
    if len(diffs) < q["threshold"]:
        out["reason"] = (f"{len(diffs)} observations where BOTH entries filled; "
                         f"needs {q['threshold']} (an untriggered planned entry has "
                         "nothing to compare)")
        return out
    t, df, p = one_sample_t(m, s, len(diffs))
    out.update(answerable=p is not None, t=t, p=p, delta=m,
               reason=None if p is not None else "zero variance; untestable")
    return out


def _q2(con: Any, q: dict, _trades: list[dict]) -> dict:
    vals = []
    for row in _progress_rows(con, 2):
        if not row["increment"]:
            continue
        m = re.search(r"R=" + _NUM, row["note"])
        if m:
            vals.append(float(m.group(1)))
    m, s = _mean_std(vals)
    out = {"progress": len(vals), "target": q["threshold"],
           "arms": [_arm("standalone sympathy", vals)]}
    if len(vals) < q["threshold"]:
        out["reason"] = f"{len(vals)} realized standalone observations; needs {q['threshold']}"
        return out
    t, df, p = one_sample_t(m, s, len(vals))
    out.update(answerable=p is not None, t=t, p=p, delta=m,
               reason=None if p is not None else "zero variance; untestable")
    return out


def _q3(con: Any, q: dict, _trades: list[dict]) -> dict:
    from .ledger import _RUNNER_ONLY_ROW
    runner_only = _RUNNER_ONLY_ROW.format(d="c.session_date", t="c.ticker")
    rows = con.execute(
        f"""SELECT stated_conviction, realized_r FROM calibration c
            WHERE realized_r IS NOT NULL AND stated_conviction IS NOT NULL
              AND NOT ({runner_only})""").fetchall()
    hi = [float(r) for c, r in rows if int(c) >= 4]
    lo = [float(r) for c, r in rows if int(c) <= 3]
    out = _two_arm(q, "conviction 4-5", hi, "conviction 1-3", lo)
    total = len(hi) + len(lo)
    if total < q["threshold"]:
        out.update(answerable=False,
                   reason=f"{total} graded picks; needs {q['threshold']} "
                          f"(and each arm n >= {ARM_MIN}: 4-5 has {len(hi)}, 1-3 has {len(lo)})")
    out["progress"], out["target"] = total, q["threshold"]
    return out


def _q4(con: Any, q: dict, _trades: list[dict]) -> dict:
    big, small = [], []
    for row in _progress_rows(con, 4):
        m_b = re.search(r"bucket=(\S+)", row["note"])
        m_r = re.search(r"\bR=" + _NUM, row["note"])
        if not (m_b and m_r) or m_b.group(1) == "unknown":
            continue
        (big if m_b.group(1) == ">15%" else small).append(float(m_r.group(1)))
    out = _two_arm(q, "gap > 15%", big, "gap <= 15%", small)
    total = len(big) + len(small)
    if total < q["threshold"]:
        out.update(answerable=False,
                   reason=f"{total} realized gap trades; needs {q['threshold']} "
                          f"(and each arm n >= {ARM_MIN}: >15% has {len(big)}, <=15% has {len(small)})")
    out["progress"], out["target"] = total, q["threshold"]
    return out


def _q5(con: Any, q: dict, _trades: list[dict]) -> dict:
    from .ledger import _DEGRADED_REJECTION, _KILL_CATEGORY_SQL
    rows = con.execute(
        f"""SELECT plan_date, {_KILL_CATEGORY_SQL} AS cat, forgone_r FROM rejections
            WHERE eod_outcome IS NOT NULL AND forgone_r IS NOT NULL
              AND NOT {_DEGRADED_REJECTION}""").fetchall()
    cats: dict[str, list[float]] = {}
    sessions = set()
    uncategorized = 0
    for d, cat, f in rows:
        if cat in ("uncategorized", "other"):
            # `other` = no kill check recorded: as uninformative about WHICH
            # filter cost the R as a row with no category at all.
            uncategorized += 1
            continue
        cats.setdefault(cat, []).append(float(f))
        sessions.add(str(d))
    arms = [_arm(c, v) for c, v in sorted(cats.items())]
    out = {"progress": len(sessions), "target": q["threshold"], "arms": arms,
           "detail": f"{uncategorized} scored rejections carry no kill check and cannot "
                     "answer this question"}
    powered = [a for a in arms if a["n"] >= ARM_MIN]
    if len(sessions) < q["threshold"] or not powered:
        out["reason"] = (f"{len(sessions)} sessions with CATEGORIZED shadow data "
                         f"(needs {q['threshold']}); {len(powered)} categories with n >= {ARM_MIN}")
        return out
    ps = []
    for a in powered:
        t, df, p = one_sample_t(a["mean"], a["std"], a["n"])
        a["t"], a["p"] = t, p
        ps.append(p)
    rej, qv = benjamini_hochberg(ps, q=Q_FDR)
    for a, r, qq in zip(powered, rej, qv):
        a["q"], a["significant"] = qq, bool(r)
    out.update(answerable=True, family=powered)
    return out


def _wilson(k: int, n: int, z: float = 1.645) -> tuple[float, float]:
    if not n:
        return 0.0, 1.0
    phat = k / n
    den = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / den
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def _q6(con: Any, q: dict, trades: list[dict]) -> dict:
    stopped = [t for t in trades if t["exit_reason"] == "stop"]
    k = sum(1 for t in stopped if t["target_hit_after_stop"])
    eod = [float(t["eod_r_planned"] if t["eod_r_planned"] is not None else t["eod_r"])
           for t in stopped if (t["eod_r_planned"] if t["eod_r_planned"] is not None
                                else t["eod_r"]) is not None]
    lo, hi = _wilson(k, len(stopped))
    m, s = _mean_std(eod)
    out = {"progress": len(stopped), "target": q["threshold"],
           "arms": [{"label": "stopped morning trades", "n": len(stopped), "mean": m, "std": s}],
           "rate": (k, len(stopped), lo, hi)}
    if len(stopped) < q["threshold"]:
        out["reason"] = f"{len(stopped)} stopped morning trades; needs {q['threshold']}"
        return out
    out["answerable"] = True
    out["descriptive"] = True
    return out


def _q7(con, q, trades):
    trig = [t for t in _triggered(trades) if t["rvol_at_1000"] is not None]
    return _two_arm(q, "RVOL@10:00 < 1x", [t["r"] for t in trig if t["rvol_at_1000"] < 1],
                    "RVOL@10:00 >= 1x", [t["r"] for t in trig if t["rvol_at_1000"] >= 1])


def _q8(con, q, trades):
    longs = [t for t in _triggered(trades) if t["direction"] == "long"]
    cites = [t["r"] for t in longs if "vwap" in str(t["thesis"] or "").lower()]
    other = [t["r"] for t in longs if "vwap" not in str(t["thesis"] or "").lower()]
    return _two_arm(q, "longs citing VWAP", cites, "other longs", other)


def _q9(con, q, trades):
    trig = [t for t in _triggered(trades) if t["stop_dist_atr"] is not None]
    return _two_arm(q, "stop 1-2 ATR", [t["r"] for t in trig if 1 <= t["stop_dist_atr"] < 2],
                    "stop < 0.5 ATR", [t["r"] for t in trig if t["stop_dist_atr"] < 0.5])


def _q10(con, q, trades):
    n = sum(r["increment"] for r in _progress_rows(con, 10))
    return {"progress": n, "target": q["threshold"], "arms": [],
            "reason": ("no analysis is declared: agent/buckets.py measures this question as "
                       "gap/ATR (blue-sky = small gap) while the evening loop describes it as "
                       "proximity to the 52-week high. Declare one in code before any finding")}


def _q11(con, q, trades):
    trig = [t for t in _triggered(trades) if t["trend_side_200d"] in ("above", "below")]
    aligned = [t["r"] for t in trig
               if (t["direction"] == "long") == (t["trend_side_200d"] == "above")]
    counter = [t["r"] for t in trig
               if (t["direction"] == "long") != (t["trend_side_200d"] == "above")]
    return _two_arm(q, "trend-aligned", aligned, "counter-trend", counter)


def _q12(con, q, trades):
    trig = [t for t in _triggered(trades) if t["short_pct_float"] is not None
            and t["setup_type"] in ("short_squeeze", "catalyst_momentum")]
    return _two_arm(q, "short%float >= 15", [t["r"] for t in trig if t["short_pct_float"] >= 15],
                    "short%float < 15", [t["r"] for t in trig if t["short_pct_float"] < 15])


def _rr_arms(con: Any, trades: list[dict]) -> tuple[list[float], list[float]]:
    """ARM A: measurable taken picks at planned R:R >= 2.0; ARM B: scored
    rejections at 1.8 <= R:R < 2.0. Both on r_multiple (the shadow measure),
    untriggered = 0R on both sides."""
    from .ledger import _DEGRADED_REJECTION
    arm_a = []
    for t in trades:
        rr = _planned_rr(t)
        if rr is None or rr < 2.0:
            continue
        if t["triggered"] and t["r_multiple"] is not None:
            arm_a.append(float(t["r_multiple"]))
        elif not t["triggered"] and t["exit_reason"] == "no_trigger":
            arm_a.append(0.0)
    rows = con.execute(
        f"""SELECT forgone_r FROM rejections
            WHERE planned_rr >= 1.8 AND planned_rr < 2.0 AND forgone_r IS NOT NULL
              AND COALESCE(eod_outcome, '') NOT IN ('', 'unmeasurable')
              AND NOT {_DEGRADED_REJECTION}""").fetchall()
    return arm_a, [float(r[0]) for r in rows]


def _q13(con, q, trades):
    a, b = _rr_arms(con, trades)
    out = _two_arm(q, "ARM A taken, R:R >= 2.0", a, "ARM B rejected, 1.8-2.0", b,
                   min_arm=q["threshold"])
    out["progress"], out["target"] = len(a), q["threshold"]
    if not out.get("answerable"):
        out["reason"] = (f"paired with Q14: ARM A {len(a)}/{q['threshold']}, ARM B "
                         f"{len(b)}/{q['threshold']}; no conclusion from one arm")
    return out


def _q14(con, q, trades):
    a, b = _rr_arms(con, trades)
    out = _two_arm(q, "ARM A taken, R:R >= 2.0", a, "ARM B rejected, 1.8-2.0", b,
                   min_arm=q["threshold"])
    out["progress"], out["target"] = len(b), q["threshold"]
    if not out.get("answerable"):
        out["reason"] = (f"paired with Q13: ARM A {len(a)}/{q['threshold']}, ARM B "
                         f"{len(b)}/{q['threshold']}; no conclusion from one arm")
    out["same_test_as"] = 13
    return out


def _q15(con, q, trades):
    from .ledger import TEST_LANES
    pts = [(float(t["size_r"] if t["size_r"] is not None else 1.0), t["r"])
           for t in _triggered(trades) if t["exploration_lane"] not in TEST_LANES]
    n = len(pts)
    out = {"progress": n, "target": q["threshold"], "arms": []}
    if n < q["threshold"]:
        out["reason"] = f"{n} triggered judgment trades; needs {q['threshold']}"
        return out
    zs, rs = [p[0] for p in pts], [p[1] for p in pts]
    mz, mr = sum(zs) / n, sum(rs) / n
    vz = sum((z - mz) ** 2 for z in zs)
    vr = sum((r - mr) ** 2 for r in rs)
    if vz <= 0 or vr <= 0:
        out["reason"] = "every trade was the same size: sizing was never varied, so it cannot be tested"
        return out
    corr = sum((z - mz) * (r - mr) for z, r in zip(zs, rs)) / math.sqrt(vz * vr)
    p = None
    if abs(corr) < 1:
        tt = corr * math.sqrt((n - 2) / (1 - corr * corr))
        p = student_t_sf(tt, n - 2)
    out.update(answerable=p is not None, p=p, t=None, delta=corr, corr=corr,
               reason=None if p is not None else "perfect correlation; untestable")
    return out


_ANALYSES = {1: _q1, 2: _q2, 3: _q3, 4: _q4, 5: _q5, 6: _q6, 7: _q7, 8: _q8, 9: _q9,
             10: _q10, 11: _q11, 12: _q12, 13: _q13, 14: _q14, 15: _q15}


def evaluate(con: Any) -> list[dict]:
    """Every registry question, with progress, answerability and -- only when
    answerable -- a computed result. Never raises: a question whose analysis
    fails is reported as not answerable, with the error."""
    try:
        trades = _morning_trades(con)
    except Exception as e:  # noqa: BLE001
        trades = []
        trade_err = f"{type(e).__name__}: {e}"
    else:
        trade_err = None
    events = {}
    try:
        from .ledger import question_progress_totals
        events = {int(r["question_id"]): int(r["total"] or 0)
                  for r in question_progress_totals(con)}
    except Exception:
        pass
    out = []
    for q in OPEN_QUESTIONS:
        fn = _ANALYSES.get(q["id"])
        try:
            res = fn(con, q, trades) if fn else {"progress": events.get(q["id"], 0),
                                                 "target": q["threshold"],
                                                 "reason": "no analysis declared"}
        except Exception as e:  # noqa: BLE001
            res = {"progress": 0, "target": q["threshold"],
                   "reason": f"analysis failed ({type(e).__name__}: {e})"
                             + (f"; trade load failed ({trade_err})" if trade_err else "")}
        res.setdefault("answerable", False)
        res.setdefault("arms", [])
        out.append({**q, **res, "events": events.get(q["id"], 0)})

    # FDR across the answerable tests (Q5's categories are corrected within
    # the question; Q6 is descriptive; Q14 repeats Q13's test).
    tested = [e for e in out if e["answerable"] and e.get("p") is not None
              and not e.get("same_test_as")]
    rej, qv = benjamini_hochberg([e["p"] for e in tested], q=Q_FDR)
    for e, r, qq in zip(tested, rej, qv):
        e["q"], e["significant"] = qq, bool(r)
    by_id = {e["id"]: e for e in out}
    for e in out:
        if e.get("same_test_as") and e["answerable"]:
            src = by_id.get(e["same_test_as"]) or {}
            e["q"], e["significant"] = src.get("q"), src.get("significant", False)
    for e in out:
        e["result"] = _result(e) if e["answerable"] else None
    return out


def _result(e: dict) -> str:
    """One sentence, computed. Direction is stated only when the test survives."""
    if e["id"] == 5:
        parts = []
        for a in e.get("family") or []:
            verdict = ("filter COST R" if a["significant"] and a["mean"] > 0 else
                       "filter SAVED R" if a["significant"] else "not distinguishable from 0")
            parts.append(f"{a['label']}: n={a['n']}, mean forgone {_fmt_r(a['mean'])}, "
                         f"q={a['q']:.3f} -> {verdict}")
        return "; ".join(parts)
    if e["id"] == 6:
        k, n, lo, hi = e["rate"]
        m = e["arms"][0]["mean"] if e["arms"] else None
        return (f"{k}/{n} stopped trades later printed their target ({k / n:.0%}, 90% CI "
                f"{lo:.0%}-{hi:.0%}); their mean hold-to-close R was {_fmt_r(m)}. "
                "Descriptive: no pre-registered decision rule")
    if e["id"] == 15:
        verdict = ("size predicts R" if e.get("significant") and e["corr"] > 0 else
                   "size predicts R INVERSELY" if e.get("significant") else
                   "size does not predict R on this evidence")
        return (f"corr(size, R) = {e['corr']:+.2f} over n={e['progress']}, p={e['p']:.3f}, "
                f"q={e['q']:.3f} -> {verdict}")
    sig = e.get("significant")
    arms = e.get("arms") or []
    head = "; ".join(f"{a['label']} n={a['n']} mean {_fmt_r(a['mean'])}" for a in arms)
    stat = f"difference {_fmt_r(e.get('delta'))}, p={e['p']:.3f}, q={e['q']:.3f}"
    if not sig:
        verdict = "NOT distinguishable: no difference may be claimed in either direction"
    elif len(arms) >= 2:
        verdict = f"{arms[0]['label'] if e['delta'] > 0 else arms[1]['label']} did better"
    else:
        verdict = "positive" if e["delta"] > 0 else "negative"
    if e["id"] == 3:
        calibrated = sig and (e.get("delta") or 0) >= 0.3
        verdict += (" -> conviction CALIBRATED (>= 0.3R spread, survives FDR)" if calibrated
                    else " -> conviction NOT shown to be calibrated: sizing-by-conviction "
                         "is unsupported")
    return f"{head}; {stat} -> {verdict}"


# ------------------------------------------------------------------ rendering

def _status(e: dict) -> str:
    if e["answerable"]:
        return f"ANSWERABLE. Result: {e['result']}"
    return f"NOT ANSWERABLE: {e.get('reason') or 'below threshold'}"


def answers_md(evals: list[dict]) -> str:
    """For the weekly miner's prompt."""
    lines = ["## OPEN QUESTIONS — COMPUTED ANSWERS (system; the playbook section is rendered from this)\n",
             "Each question's analysis is declared in code. `progress` is the count its "
             "threshold is compared against (the SMALLER arm for a two-arm question). A "
             "question without a Result is NOT answerable: do not state, imply, or "
             "paraphrase a finding for it anywhere -- not in the playbook, not in the "
             "changelog, not in key_findings. Where a Result exists, quote it; do not "
             "re-derive it or add a direction it does not state.\n"]
    for e in evals:
        lines.append(f"- Q{e['id']} ({e['slug']}): progress {e['progress']}/{e['target']}. "
                     f"{_status(e)}" + (f" [{e['detail']}]" if e.get("detail") else ""))
    return "\n".join(lines)


def playbook_md(evals: list[dict]) -> list[str]:
    """Body lines of the system-rendered `## Open Questions` section."""
    lines = [
        "",
        "> System-rendered from the registry in agent/open_questions.py and the ledger.",
        "> Progress counts only observations the question can use (for a two-arm",
        "> question, the smaller arm). A question carries a Result only when it is",
        "> answerable, and the Result is computed, FDR-corrected across the questions.",
        "> No other finding exists for a question until it does.",
        "",
    ]
    for e in evals:
        lines.append(f"**Q{e['id']}: {e['title']}**")
        lines.append("")
        lines.append(f"Threshold: {e['target']} ({e['unit']}). Progress: {e['progress']}/{e['target']}.")
        if e.get("analysis"):
            lines.append(f"Analysis: {e['analysis']}.")
        lines.append(_status(e) + ".")
        if e.get("detail"):
            lines.append(f"Note: {e['detail']}.")
        lines.append("")
    return lines
