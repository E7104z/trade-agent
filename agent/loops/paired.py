"""
Paired concurrent arms — measuring the learning loop without market confounding.

WHY THIS MODULE EXISTS
----------------------
The natural way to ask "did playbook v8 beat v7?" is to compare the ledger
before and after the rewrite. That comparison is worthless. Different weeks
carry different regimes, different opportunity sets, different volatility. A
v7-to-v8 improvement in out-of-sample expectancy is indistinguishable from the
market having simply been kinder in the second window.

The fix is pairing. Run BOTH playbook versions on the SAME MORNING, against the
SAME data bundle, the SAME regime read, and the SAME tape. Only arm A is traded
and written to `picks`; arm B is graded by the identical simulator and lives in
`paired_arms`. Every confounder that varies by day is now held fixed, and the
comparison becomes a paired-difference test on per-session R -- dramatically
more powerful than comparing two independent samples drawn weeks apart.

This is also the ONLY way to ablate the context channel. `build_morning_memory`
drops the last 20 graded trades into every morning call as a raw table. That is
recency-weighted learning happening entirely outside the n>=8 machinery which
guards the playbook -- the most-read, least-gated input in the system. Setting
arm B's `context_mode` to "aggregate_only" or "none" measures what that channel
is actually contributing.

COST
----
Arm B is a full duplicate morning (analyst + risk manager), so a paired session
costs about 2x a normal morning. At the documented few-dollars-a-week baseline
that is a rounding error, and `paired_every_n_sessions` throttles it further.
Measuring the loop IS the project; this is the cheapest honest way to do it.

SAFETY
------
Arm B can never trade. It is never written to `picks`, never emailed, never
shown on the dashboard as a plan. Any failure inside this module is caught and
logged, and the morning loop continues with arm A untouched -- an experiment
that can break production is not an experiment worth running.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any, Callable

__all__ = [
    "CONTEXT_MODES",
    "should_run_paired",
    "apply_context_mode",
    "run_challenger_arm",
    "record_arm",
    "grade_paired_arms",
    "paired_comparison",
    "paired_md",
]

CONTEXT_MODES = ("full", "aggregate_only", "none")

# Matches the section header emitted by agent/memory.py::build_morning_memory
# ("## YOUR LAST {n} GRADED TRADES"). Anchored on the literal prefix rather than
# an exact string so a change in the trade count does not silently disable the
# ablation.
_RECENT_TRADES_HEADER = re.compile(r"^##\s+YOUR LAST\b.*$", re.MULTILINE)
_ANY_H2 = re.compile(r"^##\s+", re.MULTILINE)


def _log(scope: str, msg: str) -> None:
    try:
        from ..util import log
        log(scope, msg)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------

def should_run_paired(cfg: Any, plan_date: date) -> bool:
    """True when this session should run a challenger arm.

    Deterministic on the date's ordinal rather than random, so a given calendar
    date always produces the same answer -- a re-run or a backfill cannot
    accidentally change whether a session was paired.
    """
    get = getattr(cfg, "get", None)
    if not callable(get):
        return False
    paired = get("paired", {}) or {}
    if not paired.get("enabled", False):
        return False
    every = int(paired.get("every_n_sessions", 1) or 1)
    if every <= 1:
        return True
    return (plan_date.toordinal() % every) == 0


# --------------------------------------------------------------------------
# The context-channel ablation
# --------------------------------------------------------------------------

def apply_context_mode(memory_text: str, mode: str) -> str:
    """Return the memory block with the recent-trades channel filtered.

      full           -- unchanged (production behavior)
      aggregate_only -- strips the per-trade table, keeps aggregate stats,
                        playbook, lessons, regime and self-awareness
      none           -- as above, and also strips the lesson bank, leaving only
                        the playbook and computed aggregates

    Fails OPEN: if the expected headers are not found the text is returned
    unchanged and a warning is logged. A silently-degraded challenger arm would
    contaminate the very comparison this module exists to make, so a failure to
    ablate must never masquerade as a successful ablation -- callers should
    treat an unchanged return as "ablation did not happen".
    """
    if mode not in CONTEXT_MODES:
        _log("paired", f"WARN unknown context_mode {mode!r} — treating as 'full'")
        return memory_text
    if mode == "full" or not memory_text:
        return memory_text

    out = _strip_section(memory_text, _RECENT_TRADES_HEADER, "recent-trades table")
    if mode == "none":
        out = _strip_section(
            out, re.compile(r"^##\s+YOUR LESSON BANK\b.*$", re.MULTILINE), "lesson bank"
        )
    return out


def _strip_section(text: str, header_re: re.Pattern, label: str) -> str:
    """Remove one '## ...' section, up to the next H2 or end of text."""
    m = header_re.search(text)
    if not m:
        _log("paired", f"WARN could not locate the {label} section — NOT ablated")
        return text
    nxt = _ANY_H2.search(text, m.end())
    end = nxt.start() if nxt else len(text)
    replacement = (
        f"## (ABLATED: {label} withheld from this arm by context_mode)\n\n"
        "This arm is deliberately not shown that channel. Reason from the "
        "playbook and the computed aggregates only.\n\n"
    )
    return text[:m.start()] + replacement + text[end:]


# --------------------------------------------------------------------------
# Running and recording the challenger
# --------------------------------------------------------------------------

def record_arm(
    con: Any,
    plan_date: date,
    arm: str,
    plan: dict,
    analyst: dict | None,
    playbook_version: str,
    context_mode: str,
    prompt_version: str = "",
    model: str = "",
    regime_tag: str = "",
    cost_usd: float = 0.0,
) -> int:
    """Persist one arm's plan and its picks. Idempotent per (date, arm).

    Writes ONLY to paired_arms / paired_arm_picks. Arm B never reaches `picks`,
    so it can never enter the equity curve, the streak calculation, the drawdown
    circuit breaker, or any stats_by dimension.
    """
    con.execute("DELETE FROM paired_arms WHERE plan_date = ? AND arm = ?", [plan_date, arm])
    con.execute("DELETE FROM paired_arm_picks WHERE plan_date = ? AND arm = ?", [plan_date, arm])
    con.execute(
        """INSERT INTO paired_arms
           (plan_date, arm, playbook_version, context_mode, prompt_version, model,
            no_trade, plan_json, analyst_json, regime_tag, cost_usd, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [plan_date, arm, str(playbook_version), context_mode, prompt_version, model,
         bool(plan.get("no_trade")), json.dumps(plan, default=str)[:1_000_000],
         json.dumps(analyst or {}, default=str)[:1_000_000], regime_tag,
         float(cost_usd or 0.0), datetime.now()],
    )
    n = 0
    for p in plan.get("picks", []) or []:
        entry_plan = p.get("entry_plan") or {}
        con.execute(
            """INSERT INTO paired_arm_picks
               (plan_date, arm, ticker, direction, setup_type, entry_type,
                entry_price, stop_price, target_price, size_r, conviction,
                time_stop, thesis, triggered, entry_fill, exit_price,
                exit_reason, r_multiple, sim_version, graded_at, entry_not_before)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,NULL,NULL,?)""",
            [plan_date, arm, str(p.get("ticker", "")).upper(), p.get("direction"),
             p.get("setup_type"), entry_plan.get("type") or p.get("entry_type") or "open",
             p.get("entry_price"), p.get("stop_price"), p.get("target_price"),
             p.get("size_r"), p.get("conviction"), p.get("time_stop"),
             str(p.get("thesis_final") or p.get("thesis") or "")[:2000],
             p.get("entry_not_before")],
        )
        n += 1
    return n


def run_challenger_arm(
    con: Any,
    cfg: Any,
    plan_date: date,
    *,
    analyst_system: str,
    risk_system: str,
    bundle_md: str,
    regime_md_text: str,
    memory_text: str,
    playbook_version: str,
    llm_call: Callable[..., Any],
    validate: Callable[[dict], tuple[dict, list[str]]] | None = None,
    quotes_md_for: Callable[[dict], str] | None = None,
    regime_tag: str = "",
    model: str = "",
) -> dict[str, Any]:
    """Run arm B on the SAME inputs arm A saw, varying only the declared factor.

    `llm_call(system=..., user=..., mock_name=..., thinking=..., web_search=...)`
    is injected rather than imported so this module can be tested without an API
    key and so the caller keeps control of cost accounting.

    Returns a summary dict. NEVER raises: any failure is logged and reported as
    {"ok": False, ...} so the morning loop's production path is untouched.
    """
    paired = (cfg.get("paired", {}) or {}) if hasattr(cfg, "get") else {}
    context_mode = str(paired.get("arm_b_context_mode", "full"))
    arm_b_playbook = paired.get("arm_b_playbook_version")

    try:
        arm_memory = apply_context_mode(memory_text, context_mode)
        ablated = arm_memory != memory_text
        if context_mode != "full" and not ablated:
            # Fail loudly rather than silently comparing two identical arms.
            return {"ok": False, "reason": "context ablation requested but no "
                                           "section matched; arm B not run"}

        user_block = (
            f"# TODAY: {plan_date.isoformat()} ({plan_date.strftime('%A')})\n\n"
            f"## MARKET DATA\n\n{bundle_md}\n\n"
            f"## REGIME READ (computed)\n\n{regime_md_text}\n\n"
            f"{arm_memory}\n\n"
            "Produce your candidate list now, following the output contract exactly."
        )
        analyst = llm_call(system=analyst_system, user=user_block, model=model,
                           mock_name="morning_analyst", thinking=True, web_search=True)
        candidates = getattr(analyst, "parsed", None) or {}

        # Same post-analyst quote top-up arm A gets, injected rather than
        # imported so this module stays free of MarketData/provider knowledge
        # (the same reason `validate` and `llm_call` are injected). Omitting it
        # would hold arm B to a weaker pipeline than arm A -- a handicap, not a
        # comparison.
        quotes_md = quotes_md_for(candidates) if quotes_md_for is not None else ""

        risk_user = (
            f"# TODAY: {plan_date.isoformat()}\n\n"
            f"## THE ANALYST'S CANDIDATES\n\n```json\n{json.dumps(candidates, indent=2)}\n```\n\n"
            f"## MARKET DATA\n\n{bundle_md}\n\n"
            + (f"{quotes_md}\n" if quotes_md else "")
            + f"## REGIME READ\n\n{regime_md_text}\n\n"
            f"{arm_memory}\n\n"
            "Run every candidate through the kill checklist and produce the final plan now."
        )
        risk = llm_call(system=risk_system, user=risk_user, model=model,
                        mock_name="morning_risk", thinking=True, web_search=False)
        plan = getattr(risk, "parsed", None) or {}

        if validate is not None:
            # Arm B goes through the SAME mechanical validator as arm A. An arm
            # held to weaker standards is not a comparison, it is a handicap.
            plan, _warnings = validate(plan)

        cost = 0.0
        for r in (analyst, risk):
            cost += float((getattr(r, "usage", {}) or {}).get("cost_usd", 0.0) or 0.0)

        n = record_arm(
            con, plan_date, "B", plan, candidates,
            playbook_version=str(arm_b_playbook or playbook_version),
            context_mode=context_mode, model=model, regime_tag=regime_tag,
            cost_usd=cost,
        )
        return {"ok": True, "arm": "B", "picks": n, "no_trade": bool(plan.get("no_trade")),
                "context_mode": context_mode, "cost_usd": round(cost, 4),
                "playbook_version": str(arm_b_playbook or playbook_version)}

    except Exception as e:
        _log("paired", f"WARN challenger arm failed ({type(e).__name__}: {e}) — "
                       "production arm A is unaffected")
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------
# Grading and comparison
# --------------------------------------------------------------------------

def grade_paired_arms(
    con: Any, md: Any, plan_date: date, simulate: Callable[..., dict],
    slippage_bps: float = 0.0, flatten_at: str | None = None,
    min_bar_volume: float = 0,
) -> dict[str, Any]:
    """Grade every non-production arm for a date with the production simulator.

    `simulate` is injected (agent.grading.simulate_pick) so the arms are graded
    by exactly the same code path as real picks -- a challenger graded by a
    different simulator would be measuring the simulator, not the playbook.
    """
    rows = con.execute(
        """SELECT arm, ticker, direction, setup_type, entry_type, entry_price,
                  stop_price, target_price, time_stop, entry_not_before
           FROM paired_arm_picks
           WHERE plan_date = ? AND arm <> 'A' AND graded_at IS NULL
           ORDER BY arm, ticker""",
        [plan_date],
    ).fetchall()
    if not rows:
        return {"graded": 0, "arms": {}}

    keys = ["arm", "ticker", "direction", "setup_type", "entry_type",
            "entry_price", "stop_price", "target_price", "time_stop", "entry_not_before"]
    graded = 0
    per_arm: dict[str, list[float]] = {}
    now = datetime.now()

    for r in rows:
        row = dict(zip(keys, r))
        pick = {
            "pick_id": None, "direction": row["direction"],
            "entry_type": row["entry_type"], "entry_price": row["entry_price"],
            "stop_price": row["stop_price"], "target_price": row["target_price"],
            "time_stop": row["time_stop"],
            # Arm B is graded under the same entry window as production picks.
            "entry_not_before": row["entry_not_before"],
        }
        try:
            bars = md.minute_bars(row["ticker"], plan_date)
            daily = md.daily_bar(row["ticker"], plan_date)
            out = simulate(pick, bars, daily, slippage_bps=slippage_bps,
                           flatten_at=flatten_at, min_bar_volume=min_bar_volume,
                           session_date=plan_date)
        except Exception as e:
            _log("paired", f"WARN grading {row['arm']}/{row['ticker']} failed: {e}")
            continue

        con.execute(
            """UPDATE paired_arm_picks
               SET triggered = ?, entry_fill = ?, exit_price = ?, exit_reason = ?,
                   r_multiple = ?, sim_version = ?, graded_at = ?
               WHERE plan_date = ? AND arm = ? AND ticker = ?""",
            [bool(out.get("triggered")), out.get("entry_fill"), out.get("exit_price"),
             out.get("exit_reason"), out.get("r_multiple"), out.get("sim_version"),
             now, plan_date, row["arm"], row["ticker"]],
        )
        graded += 1
        if out.get("triggered") and out.get("r_multiple") is not None \
                and out.get("exit_reason") != "invalid_fill":
            per_arm.setdefault(row["arm"], []).append(float(out["r_multiple"]))

    return {
        "graded": graded,
        "arms": {a: {"n": len(v), "total_r": round(sum(v), 3),
                     "avg_r": round(sum(v) / len(v), 4) if v else None}
                 for a, v in per_arm.items()},
    }


def paired_comparison(con: Any, arm: str = "B") -> dict[str, Any]:
    """Paired-difference test on per-session R: production arm A vs a challenger.

    Arm A's per-session R comes from the real `picks`/`outcomes` tables (it IS
    production). Arm B's comes from `paired_arm_picks`. Only sessions where BOTH
    arms ran are included -- that is what makes it paired, and it is where all
    the statistical power comes from: same day, same bundle, same tape, so
    everything that varies by day cancels in the difference.
    """
    from ..stats import bootstrap_ci_mean, one_sample_t

    sessions = [str(r[0]) for r in con.execute(
        "SELECT DISTINCT plan_date FROM paired_arms WHERE arm = ? ORDER BY plan_date",
        [arm],
    ).fetchall()]
    if not sessions:
        return {"sessions": 0, "note": f"no arm {arm} sessions recorded"}

    diffs: list[float] = []
    rows_out: list[dict] = []
    for s in sessions:
        a = con.execute(
            """SELECT COALESCE(SUM(o.r_multiple), 0.0)
               FROM picks p JOIN outcomes o ON p.pick_id = o.pick_id
               WHERE p.plan_date = ? AND o.triggered
                 AND o.r_multiple IS NOT NULL
                 AND COALESCE(o.exit_reason,'') <> 'invalid_fill'""",
            [s],
        ).fetchone()[0]
        b_row = con.execute(
            """SELECT COALESCE(SUM(r_multiple), 0.0)
               FROM paired_arm_picks
               WHERE plan_date = ? AND arm = ? AND triggered
                 AND r_multiple IS NOT NULL
                 AND COALESCE(exit_reason,'') <> 'invalid_fill'""",
            [s, arm],
        ).fetchone()
        b = float(b_row[0] or 0.0)
        ungraded = con.execute(
            """SELECT COUNT(*) FROM paired_arm_picks
               WHERE plan_date = ? AND arm = ? AND graded_at IS NULL""",
            [s, arm],
        ).fetchone()[0]
        if ungraded:
            continue   # an incompletely graded session would bias the pair
        a = float(a or 0.0)
        diffs.append(a - b)
        rows_out.append({"session": s, "arm_a_r": round(a, 3),
                         "arm_b_r": round(b, 3), "diff_r": round(a - b, 3)})

    n = len(diffs)
    if n == 0:
        return {"sessions": 0, "note": "no fully-graded paired sessions yet"}

    mean = sum(diffs) / n
    std = None
    if n >= 2:
        var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
        std = var ** 0.5
    t, df, p = one_sample_t(mean, std, n)
    ci = bootstrap_ci_mean(diffs) if n >= 3 else None

    return {
        "sessions": n,
        "arm": arm,
        "mean_diff_r": round(mean, 4),
        "std_diff_r": round(std, 4) if std is not None else None,
        "t": t, "p": p,
        "ci_low": ci[0] if ci else None,
        "ci_high": ci[1] if ci else None,
        "arm_a_total_r": round(sum(r["arm_a_r"] for r in rows_out), 3),
        "arm_b_total_r": round(sum(r["arm_b_r"] for r in rows_out), 3),
        "rows": rows_out[-20:],
        "config": _arm_config(con, arm),
    }


def _arm_config(con: Any, arm: str) -> dict[str, Any]:
    rows = con.execute(
        """SELECT DISTINCT playbook_version, context_mode
           FROM paired_arms WHERE arm = ? ORDER BY playbook_version""",
        [arm],
    ).fetchall()
    return {"playbook_versions": [r[0] for r in rows],
            "context_modes": sorted({r[1] for r in rows if r[1]})}


def paired_md(cmp: dict[str, Any]) -> str:
    """Render the paired-arm comparison for the weekly miner prompt."""
    if not cmp or not cmp.get("sessions"):
        return ("## PAIRED ARM COMPARISON (measuring the loop itself)\n\n"
                f"({cmp.get('note', 'no paired sessions yet')})")

    cfg = cmp.get("config") or {}
    p = cmp.get("p")
    p_s = f"{p:.3f}" if p is not None else "n/a"
    ci = ""
    if cmp.get("ci_low") is not None:
        ci = f" (90% CI {cmp['ci_low']:+.2f} to {cmp['ci_high']:+.2f})"

    if p is not None and p < 0.05:
        verdict = ("production is significantly ahead of the challenger"
                   if cmp["mean_diff_r"] > 0 else
                   "the CHALLENGER is significantly ahead of production — the "
                   "factor being varied is hurting, not helping")
    else:
        verdict = ("no detectable difference between the arms; the varied factor "
                   "is not measurably contributing")

    return "\n".join([
        "## PAIRED ARM COMPARISON — MEASURING THE LOOP (computed, not inferred)\n",
        f"Arm A (production) and arm {cmp['arm']} (challenger) ran on the SAME "
        f"{cmp['sessions']} mornings, against the same data bundle, the same regime "
        "read, and the same tape. Because the arms are paired by day, every "
        "confounder that varies by session — regime, volatility, the opportunity "
        "set — cancels in the difference. This is the ONLY comparison in this "
        "prompt from which a causal read is defensible.\n",
        f"- Challenger config: playbook version(s) {cfg.get('playbook_versions')}, "
        f"context mode(s) {cfg.get('context_modes')}",
        f"- Arm A total {cmp['arm_a_total_r']:+.2f}R vs arm {cmp['arm']} total "
        f"{cmp['arm_b_total_r']:+.2f}R",
        f"- Mean per-session difference (A minus {cmp['arm']}): "
        f"{cmp['mean_diff_r']:+.3f}R{ci}, paired t={cmp.get('t')}, p={p_s}",
        f"\n**Read: {verdict}.**",
        f"\nAt {cmp['sessions']} paired sessions this test is still young; treat a "
        "non-significant result as 'not yet measurable', never as 'no effect'.",
    ])
