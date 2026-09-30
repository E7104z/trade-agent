"""
Integration facade — the ONE module the existing loops call.

WHY A FACADE
------------
Everything in the v4 research layer (stats, research, population, ablation,
paired) is deliberately self-contained and independently testable. But wiring
five modules into three loops by hand means five integration points per loop and
fifteen chances to get it wrong in files this project cannot afford to
destabilize.

So the loops call exactly one function each:

    morning loop :  integrity.on_morning(...)      -- snapshot the population
    evening loop :  integrity.on_evening(...)      -- population + arms + stress
    weekly  loop :  integrity.weekly_block(...)    -- one markdown block, and
                    integrity.check_playbook(...)  -- one list of violations

Every one of them is wrapped so that a failure inside the research layer logs a
warning and returns a degraded-but-valid result. THE RESEARCH LAYER MUST NEVER
BREAK A TRADING LOOP. It is measurement apparatus bolted onto a running system;
apparatus that can halt the experiment is worse than no apparatus.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from . import ablation, friction, population, research
from . import runner_research
from .stats import annotate_stats_rows, evaluate_search

__all__ = [
    "STATS_DIMENSION_SPEC",
    "on_morning",
    "on_evening",
    "stress_ladder_for_pick",
    "build_search",
    "weekly_block",
    "check_playbook",
]


def _log(scope: str, msg: str) -> None:
    try:
        from .util import log
        log(scope, msg)
    except Exception:
        pass


def _safe(scope: str, label: str, fn: Callable[[], Any], default: Any) -> Any:
    try:
        return fn()
    except Exception as e:
        _log(scope, f"WARN {label} failed ({type(e).__name__}: {e}) — continuing")
        return default


# Every dimension the weekly miner is shown, and whether it is a ledger `picks`
# column or an `outcomes` (indicator-layer) column. This list IS the search
# width: adding a dimension here widens the multiple-testing correction for
# every other dimension, which is exactly the trade-off it should make explicit.
MORNING_LANE = "morning"

STATS_DIMENSION_SPEC: tuple[tuple[str, str], ...] = (
    ("setup_type", "picks"),
    ("regime_tag", "picks"),
    ("direction", "picks"),
    ("conviction", "picks"),
    ("in_scan_universe", "picks"),
    ("exploration_lane", "picks"),
    ("rvol_bucket", "outcomes"),
    ("vwap_side_at_fill", "outcomes"),
    ("stop_atr_bucket", "outcomes"),
    ("gap_atr_bucket", "outcomes"),
    ("ext_bucket", "outcomes"),
    ("trend_side_200d", "outcomes"),
    ("mcap_bucket", "outcomes"),
    ("short_bucket", "outcomes"),
    ("or_alignment", "outcomes"),
)


# --------------------------------------------------------------------------
# Morning
# --------------------------------------------------------------------------

def on_morning(con: Any, cfg: Any, plan_date: date, bundle: dict, plan: dict) -> dict:
    """Snapshot the pre-selection gap-scan population. Never raises.

    Call at the END of the morning loop, after `_validate_plan` and after
    `save_daily_plan`, so `was_picked` reflects the final plan. The rows written
    are still the pre-selection population -- that is what makes them a valid
    unconditional null.
    """
    if not _enabled(cfg, "population"):
        return {"population_rows": 0, "skipped": "disabled"}

    gappers = (bundle or {}).get("premarket_gappers") or []
    picked = [str(p.get("ticker", "")) for p in (plan or {}).get("picks", []) or []]
    rows = _safe("integrity", "population snapshot",
                 lambda: population.snapshot_universe(con, plan_date, gappers, picked), 0)
    if rows:
        _log("integrity", f"Population snapshot: {rows} symbols captured for {plan_date}")
    return {"population_rows": rows}


# --------------------------------------------------------------------------
# Evening
# --------------------------------------------------------------------------

def on_evening(
    con: Any, cfg: Any, md: Any, plan_date: date,
    simulate: Callable[..., dict] | None = None,
) -> dict:
    """Grade the population monkeys and any challenger arms. Never raises."""
    out: dict[str, Any] = {}

    if _enabled(cfg, "population"):
        out["population"] = _safe(
            "integrity", "population grading",
            lambda: population.grade_population(
                con, md, plan_date,
                slippage_bps=float(_cfg_get(cfg, "slippage_bps", 0.0) or 0.0)),
            {"graded": 0},
        )

    if simulate is not None and _enabled(cfg, "paired"):
        from .loops import paired as paired_mod
        out["paired"] = _safe(
            "integrity", "paired arm grading",
            lambda: paired_mod.grade_paired_arms(
                con, md, plan_date, simulate,
                slippage_bps=float(_cfg_get(cfg, "slippage_bps", 0.0) or 0.0),
                flatten_at=_cfg_get(cfg, "flatten_at", None),
                min_bar_volume=float(_cfg_get(cfg, "min_bar_volume", 0) or 0)),
            {"graded": 0},
        )
    return out


def stress_ladder_for_pick(
    pick: dict,
    bars: list,
    daily_bar: dict | None,
    simulate: Callable[..., dict],
    params: friction.FrictionParams,
    atr_pct: float | None = None,
    adv20: float | None = None,
    dollars_per_r: float = 500.0,
    flatten_at: str | None = None,
    min_bar_volume: float = 0,
) -> dict[str, Any]:
    """Re-simulate one pick at 2x and 3x friction. Additive columns only.

    The primary `r_multiple` is NOT recomputed here. It keeps its exact current
    semantics under the configured flat slippage, so no historical row changes
    meaning and no regrade is required. These are parallel readings: if a rule's
    edge does not survive the 2x rung, the miner is told, and what it had was a
    fee estimate rather than an edge.

    Returns {} on any failure -- a missing stress reading is a missing reading,
    never a fabricated one.
    """
    try:
        ladder = friction.stress_ladder(pick, params, atr_pct, adv20, dollars_per_r)
        base_key = f"{params.stress_multipliers[0]}x"
        out: dict[str, Any] = {
            "slippage_bps_used": ladder.get(base_key),
            "friction_model": params.model,
        }
        part = friction.participation_for_pick(pick, adv20, dollars_per_r)
        out["participation_pct"] = round(part * 100, 5) if part is not None else None

        for mult, col in ((2.0, "r_multiple_2x"), (3.0, "r_multiple_3x")):
            key = f"{mult}x"
            if key not in ladder:
                out[col] = None
                continue
            sim = simulate(dict(pick), bars, daily_bar, slippage_bps=ladder[key],
                           flatten_at=flatten_at, min_bar_volume=min_bar_volume)
            out[col] = sim.get("r_multiple") if sim.get("triggered") else None
        return out
    except Exception as e:
        _log("integrity", f"WARN friction stress ladder failed: {e}")
        return {}


# --------------------------------------------------------------------------
# Weekly
# --------------------------------------------------------------------------

def build_search(con: Any, ledger_mod: Any, q: float = 0.10, min_n: int = 8):
    """Score every dimension the miner will see, under one FDR correction.

    Runs the correction across the WHOLE batch rather than per dimension. That
    matters: correcting within each dimension separately would leave the
    across-dimension search entirely uncontrolled, which is where most of the
    width actually lives.
    """
    comps = []
    for dim, source in STATS_DIMENSION_SPEC:
        fn = ledger_mod.stats_by if source == "picks" else ledger_mod.stats_by_outcome
        # MORNING lane only. With no lane these cells pooled runner picks (up
        # to 10 a day, at the runner's own $/R and entry semantics) into every
        # morning dimension, and counted selected runner trades a second time
        # in the runner family below. The runner lane has its own family.
        rows = _safe("integrity", f"stats_by({dim})",
                     lambda f=fn, d=dim: f(con, d, lane=MORNING_LANE), [])
        comps.extend(annotate_stats_rows(rows, dim, n_dimensions=1))

    # Runner lane (RUNNER_PORT_SPEC §2.6). Folded into the SAME batch rather
    # than corrected separately: the runner dimensions widen the same search,
    # and correcting them apart would leave the across-family multiplicity
    # uncontrolled — which is exactly where the width lives. Pinned to the
    # current scan_version and sim_version (INTEGRITY_REPAIR_SPEC_v1 Phase 3).
    runner_rows = _safe("integrity", "runner candidate rows",
                        lambda: runner_research.current_candidate_rows(con), [])
    if runner_rows:
        comps.extend(_safe("integrity", "runner dimensions",
                           lambda: runner_research.candidate_comparisons(runner_rows), []))
        comps.append(runner_research.setup_edge(runner_rows, min_n=min_n)["comparison"])
        comps.append(runner_research.selection_edge(runner_rows, min_n=min_n)["comparison"])

    # RUNNER_EXIT_SPEC_v1: every pre-registered exit policy vs the plan exit,
    # paired, per session. Same batch -- each policy is a look whether or not
    # it has rows yet.
    from . import runner_exits
    comps.extend(_safe("integrity", "runner exit policies",
                       lambda: runner_exits.exit_policy_comparisons(
                           runner_exits.current_replay_rows(con)), []))

    # STRUCTURAL_MINING_SPEC_v1: pre-registered 2-D pairs, exact win-frequency
    # tests, and the population family (the short side). All in THIS batch --
    # none is corrected apart from the rest, and every one is a counted look.
    from . import structural
    rows_by_lane = _safe("integrity", "structural rows",
                         lambda: structural.lane_rows(con), {})
    pairs, pair_status = _safe("integrity", "2-D pairs",
                               lambda: structural.pair_comparisons(rows_by_lane, min_n),
                               ([], []))
    comps.extend(pairs)
    comps.extend(_safe("integrity", "win-frequency family",
                       lambda: structural.winrate_comparisons(rows_by_lane), []))
    comps.extend(_safe("integrity", "population family",
                       lambda: structural.population_comparisons(structural.population_rows(con)),
                       []))
    result = evaluate_search(comps, q=q, min_n=min_n)
    result.pair_status = pair_status
    result.rows_by_lane = rows_by_lane
    return result


def weekly_block(con: Any, cfg: Any, ledger_mod: Any, session_date: date) -> dict[str, Any]:
    """Everything the weekly miner needs from the research layer, as markdown.

    Returns {"markdown": str, "states": [...], "search": SearchResult, ...}.
    `states` is passed straight back into `check_playbook` after the miner
    responds, so the enforcement pass sees exactly the rule set the miner was
    shown -- not a set that moved underneath it.
    """
    q = float(_cfg_get(cfg, "research", {}).get("fdr_q", 0.10)
              if isinstance(_cfg_get(cfg, "research", {}), dict) else 0.10)
    min_n = int(_cfg_get(cfg, "research", {}).get("min_n", 8)
                if isinstance(_cfg_get(cfg, "research", {}), dict) else 8)

    search = _safe("integrity", "search evaluation",
                   lambda: build_search(con, ledger_mod, q=q, min_n=min_n), None)

    looks = {"looks_this_run": 0, "testable_looks": 0, "q_level": q,
             "expected_false_positives_uncorrected": 0}
    if search is not None:
        looks = _safe("integrity", "record looks",
                      lambda: research.record_looks(con, session_date,
                                                    search.comparisons, q),
                      looks)

    # Decay FIRST, in code (FORWARD_VALIDATION_SPEC_v1 Phase 4): a validated
    # rule gone silent for 30 market sessions returns to quarantine, a reversed
    # one retires. Then the quarantine is evaluated with cohort FDR. Both are
    # applied before the miner call and handed to it as facts.
    decay = _safe("integrity", "rule decay",
                  lambda: research.apply_decay(con, session_date),
                  {"decayed": [], "retired": []})
    graduation = _safe("integrity", "quarantine evaluation",
                       lambda: research.eligible_for_production(con, session_date),
                       {"graduated": [], "failed": [], "waiting": []})
    graduation = {**graduation,
                  "decayed": decay.get("decayed", []),
                  "failed": list(graduation.get("failed", [])) + list(decay.get("retired", []))}
    states = _safe("integrity", "rule states", lambda: research.rule_states(con), [])
    cumulative = _safe("integrity", "looks summary", lambda: research.looks_summary(con), {})

    pop = _safe("integrity", "population stats",
                lambda: population.population_stats(con), {})
    abl = _safe("integrity", "selection ablation",
                lambda: ablation.random_pick_ablation(
                    con, sim_version=_cfg_get(cfg, "ablation_sim_version", None)),
                {})

    paired_cmp: dict = {}
    paired_mod = None
    if _enabled(cfg, "paired"):
        from .loops import paired as paired_mod
        paired_cmp = _safe("integrity", "paired comparison",
                           lambda: paired_mod.paired_comparison(con), {})

    runner_rows = _safe("integrity", "runner candidate rows",
                        lambda: runner_research.current_candidate_rows(con), [])
    runner_versions = _safe("integrity", "runner version mix",
                            lambda: runner_research.version_mix(con), [])
    runner_setup = _safe("integrity", "runner setup edge",
                         lambda: runner_research.setup_edge(runner_rows, min_n=min_n),
                         {"n": 0})
    runner_selection_edge = _safe("integrity", "runner selection edge",
                                  lambda: runner_research.selection_edge(runner_rows, min_n=min_n),
                                  {"sessions_contested": 0, "n_selected": 0,
                                   "n_unselected": 0, "mean_r_selected": None,
                                   "mean_r_unselected": None, "delta_r": None,
                                   "p_value": None, "verdict": "not computed"})
    runner_random = _safe("integrity", "runner random control",
                          lambda: runner_research.runner_random_ablation(runner_rows),
                          {})
    # Reuse the scored comparisons from the search so the q-values shown to the
    # miner are the corrected ones, not a second uncorrected pass.
    runner_comps = [c for c in (getattr(search, "comparisons", None) or [])
                    if str(getattr(c, "dimension", "")).startswith("runner_")]
    from . import runner_exits
    exit_comps = [c for c in runner_comps if c.dimension == runner_exits.DIMENSION]
    runner_comps = [c for c in runner_comps if c.dimension != runner_exits.DIMENSION]

    counterfactual = _safe("integrity", "counterfactual summary",
                           lambda: research.counterfactual_summary(con), {})
    parts = [
        research.provenance_md(states, looks, counterfactual),
        _graduation_md(graduation),
        _cumulative_md(cumulative),
        population.population_md(pop),
        ablation.ablation_md(abl),
        _safe("integrity", "runner markdown",
              lambda: runner_research.runner_md(runner_setup, runner_selection_edge,
                                                runner_comps, runner_versions), ""),
        _safe("integrity", "runner random markdown",
              lambda: runner_research.runner_random_ablation_md(runner_random), ""),
        _safe("integrity", "runner exit markdown",
              lambda: runner_exits.exit_md(exit_comps, runner_exits.current_replay_rows(con)), ""),
    ]
    if paired_mod is not None:
        parts.append(paired_mod.paired_md(paired_cmp))
    if search is not None:
        parts.append(_underpowered_md(search))
        from . import structural
        parts.append(_safe("integrity", "structural consistency",
                           lambda: structural.consistency_md(structural.consistency(
                               search, getattr(search, "rows_by_lane", {}) or {}, min_n)), ""))
        parts.append(_safe("integrity", "search width",
                           lambda: structural.width_md(search, getattr(search, "pair_status", None)),
                           ""))

    return {
        "markdown": "\n\n".join(p for p in parts if p),
        "states": states,
        "search": search,
        "looks": looks,
        "graduation": graduation,
        "population": pop,
        "ablation": abl,
        "paired": paired_cmp,
        "runner_setup_edge": runner_setup,
        "runner_selection_edge": runner_selection_edge,
        "runner_random": runner_random,
        "runner_comps": runner_comps,
    }


def check_playbook(playbook_md: str, states: list[dict]) -> list[str]:
    """Provenance violations in the rewritten playbook. Never raises."""
    return _safe("integrity", "playbook provenance check",
                 lambda: research.check_playbook_provenance(playbook_md, states), [])


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------

def _graduation_md(g: dict) -> str:
    lines = ["## QUARANTINE DECISIONS THIS RUN (applied by the system)\n"]
    if not any(g.get(k) for k in ("graduated", "failed", "waiting", "decayed")):
        return lines[0] + "(no rules are currently in quarantine)"
    for label, key in (("GRADUATED to VALIDATED", "graduated"),
                       ("DECAYED back to QUARANTINED", "decayed"),
                       ("RETIRED", "failed"),
                       ("STILL WAITING", "waiting")):
        rows = g.get(key) or []
        if not rows:
            continue
        lines.append(f"**{label} ({len(rows)}):**")
        for r in rows[:20]:
            lines.append(f"- [{r['rule_id']}] {r['statement'][:100]} — {r['verdict']}")
        lines.append("")
    lines.append("These transitions have ALREADY been applied to the rule registry. "
                 "Do not re-decide them; state them.")
    return "\n".join(lines)


def _cumulative_md(c: dict) -> str:
    if not c or not c.get("cumulative_looks"):
        return ""
    return (
        "### Cumulative search width\n\n"
        f"Across {c.get('sessions', 0)} weekly runs this ledger has been searched "
        f"{c.get('cumulative_looks', 0)} times ({c.get('cumulative_testable_looks', 0)} "
        f"testable comparisons, averaging {c.get('per_session_avg', 0)} per run). "
        f"Uncorrected, that search would be expected to produce about "
        f"{c.get('expected_false_positives_uncorrected', 0)} spurious findings in total. "
        f"{c.get('cumulative_survivors', 0)} comparisons have survived FDR correction "
        "over the ledger's whole life. Any single week's 'finding' has been selected "
        "from that entire history, not just from this week's batch."
    )


def _underpowered_md(search) -> str:
    """Name the cells that cannot see an effect at their current sample size.

    This is the most useful thing the miner can be told about a young ledger:
    not what the numbers say, but which numbers are incapable of saying anything.
    """
    rows = [c for c in search.comparisons
            if c.mde is not None and c.n > 0 and c.mde > 1.0]
    if not rows:
        return ""
    rows.sort(key=lambda c: -c.mde)
    lines = [
        "### Cells that CANNOT detect a realistic effect at their current n\n",
        "Minimum detectable effect at 80% power, alpha=0.05. A cell whose MDE is "
        "larger than any edge this desk could plausibly have is not weak evidence "
        "— it is NO evidence, whatever its point estimate looks like. Do not build "
        "a rule on any of these, and do not describe them as 'promising'.\n",
    ]
    for c in rows[:25]:
        avg = f"{c.mean_r:+.2f}R" if c.mean_r is not None else "n/a"
        lines.append(f"- {c.dimension}/{c.level}: n={c.n}, avgR={avg}, "
                     f"MDE={c.mde:.2f}R — cannot detect anything smaller")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Config helpers
# --------------------------------------------------------------------------

def _cfg_get(cfg: Any, key: str, default: Any = None) -> Any:
    get = getattr(cfg, "get", None)
    return get(key, default) if callable(get) else default


def _enabled(cfg: Any, feature: str) -> bool:
    """Feature flags default OFF except the two that are pure observation.

    `population` and `research` only ADD rows and never alter a trading
    decision, so they default on. `paired` spends real money on a second LLM
    pass, so it must be opted into explicitly.
    """
    block = _cfg_get(cfg, feature, None)
    if isinstance(block, dict) and "enabled" in block:
        return bool(block["enabled"])
    if isinstance(block, bool):
        return block
    return feature in ("population", "research")
