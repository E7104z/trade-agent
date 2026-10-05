"""
Research integrity layer -- the looks registry and the rule quarantine.

WHY THIS MODULE EXISTS
----------------------
Two gaps sit above the playbook that neither the indicator specs nor the weekly
miner spec close:

1. THE UNCOUNTED LOOKS. The miner already honors a pre-registered open-question
   registry (Q1-Q12) with declared thresholds -- genuine pre-registration, and
   the correct defense against p-hacking. But the miner is ALSO handed twelve
   full stats dimensions every run and invited to find patterns in them. Those
   are unregistered looks, and nothing counts them. A pre-registration registry
   that ignores the unregistered search alongside it is documentation, not
   control. `record_looks` makes the search width a number in the ledger, which
   is what makes the FDR correction in agent/stats.py honest.

2. THE QUARANTINE HOLE. `lessons.promoted_at` gives lesson-derived rules a real
   in-sample/out-of-sample split, and `holdout_tier` caps the confidence a rule
   may claim. But the miner rewrites Setup Rules and Current Best Edge WHOLESALE
   from the full ledger each week, so a rule can enter the playbook without ever
   passing through the lesson bank's holdout machinery. This module closes that
   by giving every rule an explicit lifecycle with a forward-observation
   requirement, and by refusing promotion to any rule that cannot name the
   channel it came through.

LIFECYCLE
---------
    DISCOVERED  -> a pattern the miner noticed. Recorded, never acted on.
    QUARANTINED -> pre-registered with a target. Accumulating FORWARD-ONLY
                   observations. Still never acted on.
    VALIDATED   -> met its forward target with the effect intact. Eligible for
                   Setup Rules and Current Best Edge.
    RETIRED     -> failed forward, or decayed. Never silently re-proposed.

The critical property: observations that count toward validation must post-date
`quarantined_at`. A rule discovered BECAUSE it looked good in the data cannot be
validated by that same data. That is the whole distinction between "the rule
worked" and "we found the rule because it worked in the sample we then tested
it on."

WRITER ISOLATION
----------------
This module writes only to its own three tables. It never touches picks,
outcomes, lessons, or playbook.md.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Any, Iterable, Sequence

__all__ = [
    "MAX_RULE_DIMENSIONS",
    "DEFAULT_FORWARD_N",
    "rule_id_for",
    "rule_id_for_predicate",
    "canonical_predicate",
    "validate_predicate",
    "register_candidate",
    "register_batch_survivors",
    "register_exit_policy_candidates",
    "exit_rule_predicate",
    "EXIT_LANE",
    "EXIT_DIMENSION",
    "observe_session",
    "apply_decay",
    "morning_rows",
    "runner_rows",
    "record_looks",
    "looks_summary",
    "register_rule",
    "record_rule_observation",
    "advance_quarantine",
    "rule_states",
    "eligible_for_production",
    "reopen_underpowered_retirements",
    "check_playbook_provenance",
    "provenance_md",
]

# A ledger booking ~450 triggered trades a year cannot power a three-way
# interaction at any point in its life: 12 setups x ~9 regimes x 3 buckets is
# 324 cells averaging 1.4 trades each per year. Two-way is already generous.
MAX_RULE_DIMENSIONS = 2

# Forward observations required before a quarantined rule may be validated.
# Deliberately equal to the playbook's existing n>=8 reporting threshold so the
# two bars agree -- but these must be FORWARD observations, which the existing
# threshold does not require.
DEFAULT_FORWARD_N = 8

# Minimum sessions a rule must sit in quarantine regardless of observation
# count, so a single unusually busy week cannot validate a rule on one regime.
DEFAULT_MIN_QUARANTINE_SESSIONS = 10


# --------------------------------------------------------------------------
# Rule identity
# --------------------------------------------------------------------------

def rule_id_for(statement: str, dimensions: Sequence[str] | None = None) -> str:
    """Stable content-addressed id for a rule.

    Normalizes whitespace, case, and numeric literals before hashing so that a
    miner re-wording "gap_go longs in uptrend/risk_on (n=11)" as "gap_go longs
    during uptrend/risk_on, n=14" maps to the SAME rule rather than presenting
    as a fresh discovery with a reset quarantine clock. Laundering a stale rule
    through a rewrite is the most obvious way to defeat a quarantine, so it is
    closed at the identity layer.
    """
    text = (statement or "").strip().lower()
    text = re.sub(r"\bn\s*=\s*\d+", "", text)          # sample counts drift
    text = re.sub(r"[-+]?\d*\.\d+|\b\d+\b", "#", text)  # numeric literals drift
    text = re.sub(r"[^a-z0-9#\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    dims = ",".join(sorted(d.strip().lower() for d in (dimensions or []) if d))
    digest = hashlib.sha256(f"{text}|{dims}".encode("utf-8")).hexdigest()
    return f"R{digest[:12]}"


# --------------------------------------------------------------------------
# The looks registry
# --------------------------------------------------------------------------

def record_looks(
    con: Any,
    session_date: date,
    comparisons: Iterable[Any],
    q_level: float,
) -> dict[str, Any]:
    """Persist every comparison shown to the miner this run.

    `comparisons` accepts agent.stats.Comparison objects or plain dicts with the
    same keys. Returns a summary the weekly loop renders into the prompt, so the
    miner is told -- in the prompt itself -- how wide its own search was.
    """
    rows = [c.to_dict() if hasattr(c, "to_dict") else dict(c) for c in comparisons]
    con.execute("DELETE FROM research_looks WHERE session_date = ?", [session_date])
    for r in rows:
        con.execute(
            """INSERT INTO research_looks
               (session_date, dimension, level, n, mean_r, std_r, t_stat,
                p_value, q_value, significant, mde, n_dimensions, note, test)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                session_date, r.get("dimension"), r.get("level"),
                int(r.get("n") or 0), r.get("mean_r"), r.get("std_r"),
                r.get("t_stat"), r.get("p_value"), r.get("q_value"),
                bool(r.get("significant")), r.get("mde"),
                int(r.get("n_dimensions") or 1), (r.get("note") or "")[:400],
                r.get("test") or "one_sample",
            ],
        )
    testable = sum(1 for r in rows if r.get("p_value") is not None)
    survivors = sum(1 for r in rows if r.get("significant"))
    return {
        "session_date": str(session_date),
        "looks_this_run": len(rows),
        "testable_looks": testable,
        "q_level": q_level,
        "survivors": survivors,
        "expected_false_positives_uncorrected": round(0.05 * testable, 2),
    }


def looks_summary(con: Any, sessions: int = 26) -> dict[str, Any]:
    """Cumulative search width over the trailing `sessions` weekly runs.

    Cumulative width is the number that matters for interpreting any single
    week's 'finding': a rule that survives one week's FDR pass has still been
    selected from every look ever taken.
    """
    row = con.execute(
        """SELECT COUNT(*), COUNT(DISTINCT session_date),
                  SUM(CASE WHEN p_value IS NOT NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN significant THEN 1 ELSE 0 END)
           FROM research_looks
           WHERE session_date IN (
               SELECT DISTINCT session_date FROM research_looks
               ORDER BY session_date DESC LIMIT ?)""",
        [sessions],
    ).fetchone()
    total, n_sessions, testable, survivors = (row or (0, 0, 0, 0))
    total = int(total or 0)
    testable = int(testable or 0)
    return {
        "sessions": int(n_sessions or 0),
        "cumulative_looks": total,
        "cumulative_testable_looks": testable,
        "cumulative_survivors": int(survivors or 0),
        "expected_false_positives_uncorrected": round(0.05 * testable, 2),
        "per_session_avg": round(total / n_sessions, 1) if n_sessions else 0.0,
    }


# --------------------------------------------------------------------------
# Rule lifecycle
# --------------------------------------------------------------------------

def register_rule(
    con: Any,
    statement: str,
    dimensions: Sequence[str],
    session_date: date,
    discovery_n: int = 0,
    discovery_effect: float | None = None,
    discovery_q: float | None = None,
    source: str = "miner",
    forward_n_required: int = DEFAULT_FORWARD_N,
) -> dict[str, Any]:
    """Register a discovered rule, or return the existing record unchanged.

    Idempotent by rule_id. A rule conditioning on more than MAX_RULE_DIMENSIONS
    is registered as REJECTED_STRUCTURAL and can never be promoted -- recorded
    rather than dropped, so the graveyard shows what was proposed and refused.
    """
    dims = [str(d) for d in (dimensions or []) if d]
    rid = rule_id_for(statement, dims)
    existing = con.execute(
        "SELECT rule_id, state FROM research_rules WHERE rule_id = ?", [rid]
    ).fetchone()
    if existing:
        return {"rule_id": rid, "state": existing[1], "created": False}

    state = "DISCOVERED"
    note = ""
    if len(dims) > MAX_RULE_DIMENSIONS:
        state = "REJECTED_STRUCTURAL"
        note = (f"conditions on {len(dims)} dimensions; the ledger cannot power "
                f"more than {MAX_RULE_DIMENSIONS} at any realistic sample size")

    con.execute(
        """INSERT INTO research_rules
           (rule_id, statement, dimensions, n_dimensions, state, source,
            discovered_at, discovery_n, discovery_effect, discovery_q,
            quarantined_at, validated_at, retired_at, forward_n_required,
            forward_n, forward_sum_r, forward_sumsq_r, note)
           VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,0,0.0,0.0,?)""",
        [rid, statement[:1000], json.dumps(dims), len(dims), state, source,
         session_date, int(discovery_n or 0), discovery_effect, discovery_q,
         int(forward_n_required), note],
    )
    return {"rule_id": rid, "state": state, "created": True, "note": note}


def advance_quarantine(con: Any, rule_id: str, session_date: date) -> str:
    """Move DISCOVERED -> QUARANTINED and start the forward-observation clock.

    Only DISCOVERED rules advance. A REJECTED_STRUCTURAL rule stays rejected; a
    RETIRED rule is not resurrected here (that requires a fresh registration
    with post-retirement evidence, per the playbook's graveyard rule).
    """
    row = con.execute(
        "SELECT state FROM research_rules WHERE rule_id = ?", [rule_id]
    ).fetchone()
    if not row:
        return "UNKNOWN"
    if row[0] != "DISCOVERED":
        return row[0]
    con.execute(
        """UPDATE research_rules
           SET state = 'QUARANTINED', quarantined_at = ?,
               forward_n = 0, forward_sum_r = 0.0, forward_sumsq_r = 0.0,
               alpha_spent = 0.0, looks_done = 0
           WHERE rule_id = ?""",
        [session_date, rule_id],
    )
    return "QUARANTINED"


# Observation sources that count toward validation. `taken` = a fill-valid
# trade the desk actually took (morning lane); `runner_field` = a runner
# candidate priced by the same simulator (runner rules are statements about the
# candidate population, selected or not); `runner_exit` = one session's mean
# paired ΔR of an exit policy / construction arm against the plan, over that
# session's triggered candidates (lane EXIT_LANE). Counterfactual streams --
# `shadow` (rejected candidates) and `population` (mechanical monkeys) -- are
# RECORDED as confirming/disconfirming evidence but never counted: the desk
# did not take them, so they cannot validate a rule about what the desk takes.
COUNTED_SOURCES = ("taken", "runner_field", "runner_exit")

# Exit policies and construction arms (agent/runner_exits.py) are rules about
# HOW a runner candidate is traded, not WHICH: their one dimension is the
# policy, their unit is the session (a few hundred candidates on one tape are
# one observation of "did this beat the plan today"), and their value is the
# paired ΔR. RUNNER_EXIT_SPEC_v1 Phase 3 / RUNNER_TRADE_CONSTRUCTION_v1.
EXIT_LANE = "runner_exit"
EXIT_DIMENSION = "runner_exit_policy"


def record_rule_observation(
    con: Any, rule_id: str, session_date: date, r_multiple: float,
    source_key: str | None = None, source: str = "taken",
) -> bool:
    """Record ONE forward observation against a quarantined (or validated) rule.

    Returns False -- and records nothing -- when the observation does not
    post-date `quarantined_at`. This is the single most important guard in the
    module: it is what makes 'out-of-sample' mean out-of-sample rather than
    'counted again'. Called by `observe_session` (the evening loop) for each
    fill-valid row whose buckets match a rule's predicate.

    Idempotent on (rule_id, source_key) when a key is given: an evening retry
    must not count the same trade twice. VALIDATED rules keep receiving
    observations -- that is what decay and reversal are measured on.
    """
    row = con.execute(
        "SELECT state, quarantined_at FROM research_rules WHERE rule_id = ?",
        [rule_id],
    ).fetchone()
    if not row:
        return False
    state, q_at = row
    if state not in ("QUARANTINED", "VALIDATED") or q_at is None or r_multiple is None:
        return False
    obs_date = session_date
    if hasattr(q_at, "date"):
        q_at = q_at.date()
    if obs_date <= q_at:
        return False   # in-sample: discovered on or before this session
    if source_key is not None:
        dup = con.execute(
            "SELECT COUNT(*) FROM research_observations WHERE rule_id = ? AND source_key = ?",
            [rule_id, source_key]).fetchone()
        if dup and dup[0]:
            return False

    con.execute(
        """INSERT INTO research_observations
           (rule_id, session_date, r_multiple, source_key, source) VALUES (?,?,?,?,?)""",
        [rule_id, obs_date, float(r_multiple), source_key, source],
    )
    if source not in COUNTED_SOURCES:
        return True
    con.execute(
        """UPDATE research_rules
           SET forward_n = forward_n + 1,
               forward_sum_r = forward_sum_r + ?,
               forward_sumsq_r = forward_sumsq_r + ?,
               last_observed_at = ?
           WHERE rule_id = ?""",
        [float(r_multiple), float(r_multiple) ** 2, obs_date, rule_id],
    )
    return True


def rule_states(con: Any) -> list[dict[str, Any]]:
    """Every registered rule with its forward statistics, freshly computed.

    Forward mean/std are derived from the running sums so this stays a single
    cheap query, and the significance verdict is computed here in code -- the
    miner is never asked to decide whether a rule graduated.
    """
    from .stats import min_detectable_effect, one_sample_t

    rows = con.execute(
        """SELECT rule_id, statement, dimensions, n_dimensions, state, source,
                  discovered_at, discovery_n, discovery_effect, discovery_q,
                  quarantined_at, validated_at, retired_at, forward_n_required,
                  forward_n, forward_sum_r, forward_sumsq_r, note,
                  predicate_json, lane, decay_count, last_observed_at,
                  alpha_spent, looks_done, last_look_at, test_design
           FROM research_rules ORDER BY discovered_at DESC, rule_id"""
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        (rid, stmt, dims, ndim, state, source, disc_at, disc_n, disc_eff, disc_q,
         q_at, val_at, ret_at, fwd_req, fwd_n, fwd_sum, fwd_sumsq, note,
         pred_json, lane, decay_count, last_obs,
         alpha_spent, looks_done, last_look, test_design) = r
        fwd_n = int(fwd_n or 0)
        mean = (fwd_sum / fwd_n) if fwd_n else None
        std = None
        if fwd_n >= 2:
            var = (fwd_sumsq - fwd_n * (mean ** 2)) / (fwd_n - 1)
            std = max(0.0, var) ** 0.5
        t, df, p = one_sample_t(mean, std, fwd_n)
        out.append({
            "rule_id": rid,
            "statement": stmt,
            "dimensions": json.loads(dims) if dims else [],
            "n_dimensions": int(ndim or 0),
            "state": state,
            "source": source,
            "discovered_at": str(disc_at) if disc_at else None,
            "discovery_n": int(disc_n or 0),
            "discovery_effect": disc_eff,
            "discovery_q": disc_q,
            "quarantined_at": str(q_at) if q_at else None,
            "validated_at": str(val_at) if val_at else None,
            "retired_at": str(ret_at) if ret_at else None,
            "forward_n_required": int(fwd_req or DEFAULT_FORWARD_N),
            "forward_n": fwd_n,
            "forward_mean_r": round(mean, 4) if mean is not None else None,
            "forward_std_r": round(std, 4) if std is not None else None,
            "forward_t": t,
            "forward_p": p,
            "forward_mde": min_detectable_effect(fwd_n, std),
            "note": note or "",
            "predicate": json.loads(pred_json) if pred_json else None,
            "lane": lane,
            "decay_count": int(decay_count or 0),
            "last_observed_at": str(last_obs) if last_obs else None,
            "alpha_spent": float(alpha_spent or 0.0),
            "looks_done": int(looks_done or 0),
            "last_look_at": str(last_look) if last_look else None,
            "test_design": test_design,
        })
    return out


def eligible_for_production(
    con: Any,
    session_date: date,
    alpha: float | None = None,
    min_quarantine_sessions: int = DEFAULT_MIN_QUARANTINE_SESSIONS,
    apply: bool = True,
) -> dict[str, list[dict]]:
    """Decide which quarantined rules graduate, fail, or keep collecting.

    A rule is LOOKED AT once it has forward_n >= forward_n_required and has sat
    `min_quarantine_sessions` days in quarantine. Each look spends part of one
    fixed error budget (agent/sequential.py, EDGE_ACCELERATION_SPEC_v1 Part 1):

      VALIDATED  its one-sided forward p, in the direction it was discovered,
                 passes this look's share of the budget after cohort FDR
      RETIRED    the forward evidence rules out a useful edge (futility), or
                 the effect reversed sign, or the whole budget is spent
                 (n >= sequential.N_MAX) without the rule being shown
      WAITING    anything else: the rule keeps collecting forward evidence

    Rules must still be able to fail -- futility and the horizon retire them --
    but a rule is no longer retired merely because eight trades could not
    prove a realistic edge. That one-look test retired ~90% of real +0.3R
    edges for good.

    Cohort FDR (FORWARD_VALIDATION_SPEC_v1 Phase 4) is unchanged in spirit:
    every rule looked at in the SAME run is one family, BH at the full budget
    over each look's p scaled to that budget. With one rule it is exactly
    p <= this look's alpha; with more it only gets stricter.

    `alpha` is accepted for backward compatibility and ignored: the budget is
    sequential.ALPHA, a constant no caller can move. `apply=False` evaluates
    without writing.
    """
    from . import sequential as seq

    graduated: list[dict] = []
    failed: list[dict] = []
    waiting: list[dict] = []

    candidates = []
    for r in rule_states(con):
        if r["state"] != "QUARANTINED":
            continue
        q_at = r["quarantined_at"]
        elapsed = None
        if q_at:
            try:
                y, m, d = (int(x) for x in str(q_at)[:10].split("-"))
                elapsed = (session_date - date(y, m, d)).days
            except (ValueError, TypeError):
                elapsed = None
        r["_elapsed"] = elapsed
        candidates.append(r)

    looks: dict[str, dict] = {}
    for r in candidates:
        if not (r["forward_n"] >= r["forward_n_required"]
                and r["_elapsed"] is not None and r["_elapsed"] >= min_quarantine_sessions):
            continue
        direction = -1 if (r.get("discovery_effect") or 0) < 0 else 1
        looks[r["rule_id"]] = seq.look(
            r["forward_n"], r.get("forward_mean_r"), r.get("forward_std_r"),
            direction, r.get("alpha_spent"))
    tested = [rid for rid, lk in looks.items() if lk["status"] == "looked"]
    rejected, qvals = seq.cohort([looks[rid]["p_adj"] for rid in tested])
    cohort = {rid: (rej, qv) for rid, rej, qv in zip(tested, rejected, qvals)}

    for r in candidates:
        elapsed = r.pop("_elapsed")
        rid = r["rule_id"]
        lk = looks.get(rid)
        r["look"] = lk
        if lk is None:
            need_n = max(0, r["forward_n_required"] - r["forward_n"])
            need_d = max(0, min_quarantine_sessions - (elapsed or 0))
            r["verdict"] = f"WAITING — needs {need_n} more obs, {need_d} more days"
            waiting.append(r)
            continue
        if lk["status"] == "untestable":
            # UNTESTABLE is not FAILED. A degenerate forward sample (zero
            # observed variance, or n<2) means the test could not be run; it
            # spends nothing and the rule keeps collecting.
            r["verdict"] = ("WAITING — forward sample not testable "
                            "(zero variance or n<2); collecting more observations")
            waiting.append(r)
            continue
        if lk["status"] == "no_new_information":
            r["verdict"] = (f"WAITING — no new forward evidence since the last look "
                            f"(n={r['forward_n']})")
            waiting.append(r)
            continue

        sig, r["forward_q"] = cohort.get(rid, (False, None))
        if sig:
            r["verdict"] = (f"VALIDATED — {seq.describe(lk)}; significant after cohort FDR "
                            f"(cohort of {len(tested)})")
            graduated.append(r)
            if apply:
                con.execute(
                    """UPDATE research_rules SET state='VALIDATED', validated_at=?,
                       alpha_spent=?, looks_done=COALESCE(looks_done,0)+1,
                       last_look_at=?, test_design=? WHERE rule_id=?""",
                    [session_date, lk["spent"], session_date, seq.DESIGN, rid])
            continue

        reason = None
        if lk["reversed"]:
            reason = (f"forward effect reversed sign (mean {r.get('forward_mean_r'):+.3f}R "
                      f"against a discovery of {r.get('discovery_effect')}, n={lk['n']})")
        elif lk["futile"]:
            reason = (f"futile: the forward evidence rules out an edge of "
                      f"+{seq.MIN_USEFUL_R:.2f}R ({int(seq.FUTILITY_CONF * 100)}% upper bound "
                      f"{lk['upper']:+.3f}R at n={lk['n']})")
        elif lk["final"]:
            reason = (f"not shown: forward evidence not significant with the whole budget "
                      f"spent (n={lk['n']}, p={lk['p']:.4g}, q={r['forward_q']}, cohort of "
                      f"{len(tested)})")
        if reason:
            r["verdict"] = f"RETIRED — {reason}"
            failed.append(r)
            if apply:
                con.execute(
                    """UPDATE research_rules SET state='RETIRED', retired_at=?,
                       alpha_spent=?, looks_done=COALESCE(looks_done,0)+1,
                       last_look_at=?, test_design=?,
                       note = COALESCE(note,'') || ' | ' || ? WHERE rule_id=?""",
                    [session_date, lk["spent"], session_date, seq.DESIGN, reason[:300], rid])
            continue

        r["verdict"] = f"WAITING — {seq.describe(lk)}; not yet shown, collecting"
        waiting.append(r)
        if apply:
            con.execute(
                """UPDATE research_rules SET alpha_spent=?, looks_done=COALESCE(looks_done,0)+1,
                   last_look_at=?, test_design=? WHERE rule_id=?""",
                [lk["spent"], session_date, seq.DESIGN, rid])

    return {"graduated": graduated, "failed": failed, "waiting": waiting}


# The phrase the one-look test wrote when it retired a rule for want of
# significance at its forward target. Those rules were judged by a test with
# ~10% power against a +0.3R edge; they are re-quarantined once, on fresh
# evidence only (reopen_underpowered_retirements).
_UNDERPOWERED_RETIREMENT = "forward evidence not significant after cohort FDR"


def reopen_underpowered_retirements(con: Any, session_date: date) -> list[str]:
    """Give every rule the old single look retired for want of significance
    one fresh forward test under the sequential design.

    Fresh means fresh: the forward clock restarts at `session_date`, the
    running sums are zeroed and only observations dated after today can count
    (record_rule_observation's guard), so nothing already seen is counted
    again. Rules retired for a REVERSED sign or as repeatedly stale stay
    retired -- those were findings, not want of power. Idempotent: a reopened
    rule is stamped with the new design and never matches again.
    """
    from . import sequential as seq
    try:
        rows = con.execute(
            """SELECT rule_id, forward_n FROM research_rules
               WHERE state = 'RETIRED' AND test_design IS NULL
                 AND predicate_json IS NOT NULL AND note LIKE ?""",
            [f"%{_UNDERPOWERED_RETIREMENT}%"]).fetchall()
    except Exception:
        return []
    reopened = []
    for rid, fwd_n in rows:
        note = (f" | reopened {session_date}: retired at forward n={int(fwd_n or 0)} by the "
                f"one-look test (~10% power against a +0.3R edge); fresh forward clock under "
                f"{seq.DESIGN}, nothing seen before today counts")
        con.execute(
            """UPDATE research_rules
               SET state = 'QUARANTINED', quarantined_at = ?, retired_at = NULL,
                   forward_n = 0, forward_sum_r = 0.0, forward_sumsq_r = 0.0,
                   last_observed_at = NULL, alpha_spent = 0.0, looks_done = 0,
                   last_look_at = NULL, test_design = ?, note = COALESCE(note, '') || ?
               WHERE rule_id = ?""",
            [session_date, seq.DESIGN, note, rid])
        reopened.append(rid)
    if reopened:
        from .util import log
        log("research", f"reopened {len(reopened)} rule(s) the one-look test retired "
                        f"underpowered: {', '.join(reopened)}")
    return reopened


# --------------------------------------------------------------------------
# Predicates: the machine-readable cell a rule is about
# (FORWARD_VALIDATION_SPEC_v1 Phase 2)
# --------------------------------------------------------------------------

def canonical_predicate(pred: Any) -> dict | None:
    """{"lane": str, "conditions": [{"dimension", "level"}, ...]} sorted by
    dimension, or None if the shape is wrong. Pure; validates nothing else."""
    if not isinstance(pred, dict):
        return None
    lane = str(pred.get("lane") or "").strip().lower()
    conds = pred.get("conditions")
    if not lane or not isinstance(conds, list):
        return None
    out = []
    for c in conds:
        if not isinstance(c, dict):
            return None
        dim = str(c.get("dimension") or "").strip()
        level = str(c.get("level") if c.get("level") is not None else "").strip()
        if not dim or not level:
            return None
        out.append({"dimension": dim, "level": level})
    out.sort(key=lambda c: c["dimension"])
    return {"lane": lane, "conditions": out}


def rule_id_for_predicate(pred: dict) -> str:
    """Identity from the CELL, not the prose: the same predicate reworded is
    the same rule, so a stale rule cannot be laundered through a rewrite."""
    key = pred["lane"] + "|" + ";".join(f"{c['dimension']}={c['level']}"
                                         for c in pred["conditions"])
    return f"R{hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]}"


def emitted_levels(comparisons: Iterable[Any]) -> dict[str, set[str]]:
    """Every (dimension -> levels) the system emitted in a search batch."""
    out: dict[str, set[str]] = {}
    for c in comparisons or []:
        dim = getattr(c, "dimension", None) or (c.get("dimension") if isinstance(c, dict) else None)
        lev = getattr(c, "level", None) or (c.get("level") if isinstance(c, dict) else None)
        if dim is not None and lev is not None:
            out.setdefault(str(dim), set()).add(str(lev))
    return out


def validate_predicate(pred: Any, comparisons: Iterable[Any]) -> tuple[dict | None, str, str]:
    """(canonical predicate or None, state, reason).

    state is "" when the predicate is valid, else REJECTED_STRUCTURAL (too many
    dimensions) or REJECTED_UNMATCHABLE (a cell the system never emitted, or a
    shape it cannot match a trade against). Levels must be labels the system
    itself produced for that dimension THIS run -- a rule on an invented level
    can never be matched to a trade, and would sit in quarantine forever.
    """
    from . import buckets

    canon = canonical_predicate(pred)
    if canon is None:
        return None, "REJECTED_UNMATCHABLE", "no machine-readable predicate"
    if canon["lane"] not in buckets.LANES + (EXIT_LANE,):
        return None, "REJECTED_UNMATCHABLE", f"unknown lane {canon['lane']!r}"
    conds = canon["conditions"]
    if not conds:
        return None, "REJECTED_UNMATCHABLE", "predicate has no conditions"
    if len(conds) > MAX_RULE_DIMENSIONS:
        return None, "REJECTED_STRUCTURAL", (
            f"conditions on {len(conds)} dimensions; the ledger cannot power "
            f"more than {MAX_RULE_DIMENSIONS} at any realistic sample size")
    if len({c["dimension"] for c in conds}) != len(conds):
        return None, "REJECTED_UNMATCHABLE", "the same dimension appears twice"
    lane_dims = ({EXIT_DIMENSION} if canon["lane"] == EXIT_LANE
                 else set(buckets.dimensions(canon["lane"])))
    levels = emitted_levels(comparisons)
    for c in conds:
        if c["dimension"] not in lane_dims:
            return None, "REJECTED_UNMATCHABLE", (
                f"{c['dimension']!r} is not a {canon['lane']}-lane dimension")
        if c["level"] not in levels.get(c["dimension"], set()):
            return None, "REJECTED_UNMATCHABLE", (
                f"{c['dimension']}={c['level']!r} is not a cell the system emitted this run")
    return canon, "", ""


def realized_r(row: dict) -> float | None:
    """The realized R every mined statistic uses: planned-risk R (`r_planned`)
    where the row carries it, else the legacy `r_multiple`. Same measure as
    ledger's COALESCE(r_planned, r_multiple) and runner_research._planned_r,
    so discovery, pairs, and forward validation share one denominator."""
    v = row.get("r_planned")
    if v is None:
        v = row.get("r_multiple")
    return float(v) if v is not None else None


def matches(pred: dict, row: dict) -> bool:
    from . import buckets
    return all(buckets.label(pred["lane"], c["dimension"], row) == c["level"]
               for c in pred["conditions"])


def morning_rows(con: Any, on: date | None = None) -> list[dict]:
    """Fill-valid, triggered MORNING-lane trades (merged pick + outcome rows).

    The fill-validity gates run here, before any rule can see a row: an
    invalid_fill is not a trade and an execution_anomaly never tested the
    thesis at its level, so neither may count for or against a rule.
    """
    from .ledger import RUNNER_SETUP
    cols_p = [c[0] for c in con.execute("DESCRIBE picks").fetchall()]
    cols_o = [c[0] for c in con.execute("DESCRIBE outcomes").fetchall()]
    where = [
        "o.triggered = TRUE", "o.r_multiple IS NOT NULL",
        "COALESCE(o.exit_reason, '') <> 'invalid_fill'",
        "COALESCE(o.execution_anomaly, FALSE) = FALSE",
        f"COALESCE(p.setup_type, '') <> '{RUNNER_SETUP}'",
    ]
    params: list = []
    if on is not None:
        where.append("p.plan_date = ?")
        params.append(on)
    rows = con.execute(
        f"SELECT p.*, o.* FROM picks p JOIN outcomes o ON o.pick_id = p.pick_id "
        f"WHERE {' AND '.join(where)} ORDER BY p.plan_date, p.pick_id", params).fetchall()
    out = []
    for r in rows:
        row = dict(zip(cols_p, r[:len(cols_p)]))
        row.update(dict(zip(cols_o, r[len(cols_p):])))
        out.append(row)
    return out


def runner_rows(con: Any, on: date | None = None) -> list[dict]:
    """Triggered runner candidates under the CURRENT scan/sim version."""
    from . import runner_research
    if on is None:
        return runner_research.current_candidate_rows(con)
    from . import ledger
    scan_v, sim_v = runner_research.current_versions()
    rows = [r for r in ledger.runner_candidate_outcomes_since(con, on)
            if r.get("session_date") == on and r.get("scan_version") == scan_v
            and r.get("sim_version") == sim_v and r.get("triggered")
            and realized_r(r) is not None
            and r.get("exit_reason") != "invalid_fill"]
    return runner_research._attach_regime(con, rows)


def discovery_stats(pred: dict, comparisons: Iterable[Any], rows: list[dict]) -> dict:
    """The discovery numbers for a predicate, computed by the system.

    One condition: that cell's Comparison from the (FDR-corrected) batch. Two
    conditions: the joint cell computed from the same rows, with q = None --
    a 2-D cell outside the batch is unscreened at discovery, which is safe
    because forward validation, not discovery, is what admits a rule.
    """
    conds = pred["conditions"]
    if len(conds) == 1:
        c0 = conds[0]
        for c in comparisons or []:
            if getattr(c, "dimension", None) == c0["dimension"] and getattr(c, "level", None) == c0["level"]:
                return {"n": int(c.n or 0), "effect": c.mean_r, "q": c.q_value, "note": ""}
        return {"n": 0, "effect": None, "q": None, "note": "cell not found in this run's batch"}
    # A pre-registered, admitted pair is IN the batch (STRUCTURAL_MINING_SPEC_v1
    # Phase 1): take its corrected numbers from there.
    from .structural import PAIR_SEP, pair_dimension
    pair_dim = pair_dimension([c["dimension"] for c in conds])
    pair_level = PAIR_SEP.join(c["level"] for c in conds)   # conds sorted by dimension
    for c in comparisons or []:
        if getattr(c, "dimension", None) == pair_dim and getattr(c, "level", None) == pair_level:
            return {"n": int(c.n or 0), "effect": c.mean_r, "q": c.q_value,
                    "note": "pre-registered 2-D pair, screened in this run's batch"}
    from .clusters import unit_values
    vals = unit_values([r for r in rows if matches(pred, r)], realized_r,
                       pred.get("lane") or "morning")
    mean = (sum(vals) / len(vals)) if vals else None
    return {"n": len(vals), "effect": mean, "q": None,
            "note": "2-D cell outside this run's FDR batch — unscreened at discovery"}


def register_candidate(con: Any, candidate: dict, session_date: date,
                       comparisons: Iterable[Any], rows_by_lane: dict[str, list[dict]],
                       source: str = "miner") -> dict:
    """Register one miner-nominated rule candidate, the system's way.

    The predicate is validated against the cells this run emitted, the
    discovery numbers are computed here (the miner's own numbers are kept in
    the note for audit only), identity comes from the predicate, and a valid
    rule is quarantined at once. A candidate that cannot be matched to trades
    is recorded in the graveyard as REJECTED_UNMATCHABLE rather than being
    allowed to sit in quarantine forever.
    """
    comparisons = list(comparisons or [])
    statement = str(candidate.get("statement") or "")[:1000]
    canon, rej_state, reason = validate_predicate(candidate.get("predicate"), comparisons)
    miner = {k: candidate.get(k) for k in ("discovery_n", "discovery_effect", "discovery_q")
             if candidate.get(k) is not None}
    audit = f"miner-reported {miner}" if miner else ""

    if canon is None:
        rid = rule_id_for(statement, candidate.get("dimensions") or [])
        existing = con.execute("SELECT state FROM research_rules WHERE rule_id = ?", [rid]).fetchone()
        if existing:
            return {"rule_id": rid, "state": existing[0], "created": False, "note": reason}
        con.execute(
            """INSERT INTO research_rules
               (rule_id, statement, dimensions, n_dimensions, state, source,
                discovered_at, discovery_n, discovery_effect, discovery_q,
                quarantined_at, validated_at, retired_at, forward_n_required,
                forward_n, forward_sum_r, forward_sumsq_r, note, predicate_json, lane,
                decay_count, last_observed_at)
               VALUES (?,?,?,?,?,?,?,0,NULL,NULL,NULL,NULL,NULL,?,0,0.0,0.0,?,NULL,NULL,0,NULL)""",
            [rid, statement, json.dumps(candidate.get("dimensions") or []),
             len(candidate.get("dimensions") or []), rej_state, source, session_date,
             DEFAULT_FORWARD_N, " | ".join(x for x in (reason, audit) if x)[:1000]])
        return {"rule_id": rid, "state": rej_state, "created": True, "note": reason}

    rid = rule_id_for_predicate(canon)
    existing = con.execute("SELECT state FROM research_rules WHERE rule_id = ?", [rid]).fetchone()
    if existing:
        return {"rule_id": rid, "state": existing[0], "created": False, "note": ""}
    disc = discovery_stats(canon, comparisons, rows_by_lane.get(canon["lane"], []))
    dims = [c["dimension"] for c in canon["conditions"]]
    con.execute(
        """INSERT INTO research_rules
           (rule_id, statement, dimensions, n_dimensions, state, source,
            discovered_at, discovery_n, discovery_effect, discovery_q,
            quarantined_at, validated_at, retired_at, forward_n_required,
            forward_n, forward_sum_r, forward_sumsq_r, note, predicate_json, lane,
            decay_count, last_observed_at)
           VALUES (?,?,?,?,'DISCOVERED',?,?,?,?,?,NULL,NULL,NULL,?,0,0.0,0.0,?,?,?,0,NULL)""",
        [rid, statement, json.dumps(dims), len(dims), source, session_date, int(disc["n"] or 0),
         disc["effect"], disc["q"], DEFAULT_FORWARD_N,
         " | ".join(x for x in (disc["note"], audit) if x)[:1000],
         json.dumps(canon), canon["lane"]])
    state = advance_quarantine(con, rid, session_date)
    return {"rule_id": rid, "state": state, "created": True, "note": disc["note"]}


# How many NEW rules the system may quarantine from one weekly batch. A budget
# choice, not an edge claim: each quarantined rule is a forward test, and the
# cohort correction at graduation gets stricter as the cohort grows.
SYSTEM_MAX_NEW_PER_RUN = 12


def register_batch_survivors(con: Any, session_date: date, comparisons: Iterable[Any],
                             rows_by_lane: dict[str, list[dict]],
                             max_new: int = SYSTEM_MAX_NEW_PER_RUN) -> list[dict]:
    """Quarantine every mean-R cell that survived THIS run's FDR batch.

    EDGE_ACCELERATION_SPEC_v1 Part 2. A cell that survives the corrected
    batch is a computed fact, and until now it reached the forward test only
    if the miner happened to nominate it in prose that week -- so a real
    survivor could sit unvalidated forever on the strength of one LLM's
    attention. The system now does what it already did for exit policies:
    every surviving 1-D cell and admitted 2-D pair of the morning and runner
    lanes is registered through `register_candidate` (predicate validated
    against the cells emitted this run, discovery numbers from the batch)
    and quarantined today. Losers are registered too: a validated losing cell
    is an exclusion the desk can enforce.

    Win-frequency, drift, population and selection-edge comparisons are not
    cells a trade can be matched to and are skipped. Identity comes from the
    predicate, so a cell already registered keeps its state and clock (a
    RETIRED one stays in the graveyard). At most `max_new` new rules per run,
    smallest q first.
    """
    from .structural import PAIR_SEP, _parse
    comparisons = list(comparisons or [])
    survivors = []
    for c in comparisons:
        if not getattr(c, "significant", False) or c.mean_r is None or c.mean_r == 0:
            continue
        parsed = _parse(str(c.dimension))
        if not parsed:
            continue
        lane, dims = parsed
        levels = str(c.level).split(PAIR_SEP) if len(dims) > 1 else [str(c.level)]
        if len(levels) != len(dims) or any(lv in ("", "?") for lv in levels):
            continue
        survivors.append((c, lane, dims, levels))
    survivors.sort(key=lambda t: (t[0].q_value if t[0].q_value is not None else 1.0,
                                  -abs(t[0].mean_r)))
    out: list[dict] = []
    for c, lane, dims, levels in survivors:
        if len(out) >= max_new:
            break
        cell = " AND ".join(f"{d_}={lv}" for d_, lv in zip(dims, levels))
        unit = "unit" if lane == "runner" else "trade"
        statement = (f"[system] {lane} cell {cell}: mean {c.mean_r:+.3f}R per {unit} over "
                     f"n={c.n}, a survivor of the {session_date} FDR batch (q={c.q_value})")
        pred = {"lane": lane, "conditions": [{"dimension": d_, "level": lv}
                                             for d_, lv in zip(dims, levels)]}
        reg = register_candidate(con, {"statement": statement, "predicate": pred},
                                 session_date, comparisons, rows_by_lane, source="system")
        if reg.get("created"):
            out.append({"rule_id": reg["rule_id"], "state": reg["state"], "cell": cell,
                        "lane": lane, "mean_r": c.mean_r, "n": c.n, "q": c.q_value})
    if out:
        from .util import log
        log("research", f"system quarantined {len(out)} batch survivor(s): "
                        + ", ".join(f"{r['rule_id']} {r['cell']}" for r in out))
    return out


# The first session whose runner evidence is counted in (session, industry)
# UNITS under scan_version 3 (specs/RUNNER_POOL_INTEGRITY_v1.md). Before it,
# runner forward observations were recorded one per candidate row -- thirty
# correlated banks on one morning were thirty observations -- and described a
# pool that held ETFs and stale-bracket fills.
RUNNER_UNIT_COUNTING_FROM = date(2026, 10, 2)


def _is_runner_rule(lane: Any, dimensions: Any) -> bool:
    if lane == EXIT_LANE:
        return False   # session-unit evidence from the start; nothing to restart
    if lane == "runner":
        return True
    return "runner_" in str(dimensions or "")


def exit_rule_predicate(policy: str) -> dict:
    return {"lane": EXIT_LANE,
            "conditions": [{"dimension": EXIT_DIMENSION, "level": str(policy)}]}


def register_exit_policy_candidates(con: Any, session_date: date,
                                    comparisons: Iterable[Any]) -> list[dict]:
    """Quarantine every exit policy / construction arm whose paired ΔR against
    the plan survived THIS run's FDR batch with a positive sign.

    The system does this itself, not the miner: the family is pre-registered
    in code (agent/runner_exits.py), so "which arm is a candidate" is a
    computed fact, and leaving it to a prose nomination would let a surviving
    arm sit unvalidated -- or an unsurvived one be talked into quarantine.
    A policy that tests significantly WORSE than the plan is not a candidate
    for anything and is not registered. Idempotent: the rule id comes from the
    predicate, so a policy already in the registry keeps its state and clock.
    """
    out: list[dict] = []
    for c in comparisons or []:
        if getattr(c, "dimension", None) != EXIT_DIMENSION or not getattr(c, "significant", False):
            continue
        if c.mean_r is None or c.mean_r <= 0:
            continue
        canon = canonical_predicate(exit_rule_predicate(c.level))
        rid = rule_id_for_predicate(canon)
        if con.execute("SELECT 1 FROM research_rules WHERE rule_id = ?", [rid]).fetchone():
            continue
        statement = (f"Runner exit policy `{c.level}` beats the plan exit: paired ΔR "
                     f"{c.mean_r:+.3f}R per session over n={c.n} sessions (q={c.q_value}).")
        con.execute(
            """INSERT INTO research_rules
               (rule_id, statement, dimensions, n_dimensions, state, source,
                discovered_at, discovery_n, discovery_effect, discovery_q,
                quarantined_at, validated_at, retired_at, forward_n_required,
                forward_n, forward_sum_r, forward_sumsq_r, note, predicate_json, lane,
                decay_count, last_observed_at)
               VALUES (?,?,?,1,'DISCOVERED','system',?,?,?,?,NULL,NULL,NULL,?,0,0.0,0.0,?,?,?,0,NULL)""",
            [rid, statement, json.dumps([EXIT_DIMENSION]), session_date, int(c.n or 0),
             c.mean_r, c.q_value, DEFAULT_FORWARD_N,
             "construction arm / exit policy, registered by the system from the weekly batch; "
             "forward observations are per-session paired ΔR",
             json.dumps(canon), EXIT_LANE])
        state = advance_quarantine(con, rid, session_date)
        out.append({"rule_id": rid, "policy": c.level, "state": state})
    return out


def _exit_session_deltas(con: Any, d: date) -> dict[str, float]:
    """{policy: session d's mean paired ΔR vs the plan}, current versions only."""
    from . import ledger, runner_exits
    from .runner_research import current_versions
    scan_v, sim_v = current_versions()
    try:
        rows = [r for r in ledger.runner_exit_replays_since(con, d)
                if r.get("session_date") == d and r.get("scan_version") == scan_v
                and r.get("sim_version") == sim_v
                and r.get("policy_version") == runner_exits.POLICY_VERSION
                and r.get("r_planned") is not None]
    except Exception:
        return {}
    by_cand: dict[str, dict[str, float]] = {}
    for r in rows:
        by_cand.setdefault(r["ticker"], {})[r["policy"]] = float(r["r_planned"])
    deltas: dict[str, list[float]] = {}
    for m in by_cand.values():
        if runner_exits.REFERENCE not in m:
            continue
        for pol, v in m.items():
            if pol != runner_exits.REFERENCE:
                deltas.setdefault(pol, []).append(v - m[runner_exits.REFERENCE])
    return {p: sum(v) / len(v) for p, v in deltas.items() if v}


def restart_runner_forward_clocks(con: Any, anchor: date | None = None) -> list[str]:
    """Restart the forward clock of every live runner rule quarantined before
    `anchor`, so its forward evidence is all unit-counted, scan_version 3
    evidence. Idempotent: a restarted rule's quarantined_at becomes the day
    before `anchor`, which the test below no longer selects.

    This can only DELAY validation -- it never lowers a bar. A VALIDATED
    runner rule goes back to QUARANTINED: its validation was counted on rows
    that were not independent, from a population that included funds. Its old
    observations stay in research_observations as the record; they stop
    counting because the running sums are zeroed.
    """
    from datetime import timedelta
    anchor = anchor or RUNNER_UNIT_COUNTING_FROM
    restart_at = anchor - timedelta(days=1)
    rows = con.execute(
        """SELECT rule_id, state, quarantined_at, lane, dimensions, forward_n
           FROM research_rules WHERE state IN ('QUARANTINED', 'VALIDATED')"""
    ).fetchall()
    restarted: list[str] = []
    for rid, state, q_at, lane, dims, fwd_n in rows:
        if not _is_runner_rule(lane, dims):
            continue
        q = q_at.date() if hasattr(q_at, "date") and not isinstance(q_at, date) else q_at
        if q is not None and q >= restart_at:
            continue
        note = (f" | forward clock restarted {anchor}: runner evidence is now counted in "
                f"(session, industry) units under scan_version 3; {int(fwd_n or 0)} earlier "
                f"row-level observation(s) no longer count"
                + ("; was VALIDATED" if state == "VALIDATED" else ""))
        con.execute(
            """UPDATE research_rules
               SET state = 'QUARANTINED', quarantined_at = ?, validated_at = NULL,
                   forward_n = 0, forward_sum_r = 0.0, forward_sumsq_r = 0.0,
                   last_observed_at = NULL, alpha_spent = 0.0, looks_done = 0,
                   note = COALESCE(note, '') || ?
               WHERE rule_id = ?""",
            [restart_at, note, rid])
        restarted.append(rid)
    if restarted:
        from .util import log
        log("research", f"restarted the forward clock of {len(restarted)} runner rule(s): "
                        f"{', '.join(restarted)}")
    return restarted


def observe_session(con: Any, d: date) -> dict:
    """Record every forward observation session `d` produced (Phase 3).

    For each QUARANTINED or VALIDATED rule with a predicate, every fill-valid
    row of its lane from `d` whose buckets match the predicate is one forward
    observation -- one per (session, industry) unit for the runner lane.
    `record_rule_observation` keeps the forward-only guard and is idempotent on
    source_key, so a retried evening changes nothing.
    """
    restart_runner_forward_clocks(con)
    reopen_underpowered_retirements(con, d)
    rules = con.execute(
        """SELECT rule_id, lane, predicate_json FROM research_rules
           WHERE state IN ('QUARANTINED', 'VALIDATED') AND predicate_json IS NOT NULL"""
    ).fetchall()
    out = {"rules": len(rules), "recorded": 0}
    if not rules:
        return out
    cache: dict[str, list[dict]] = {}
    counterfactual: list[tuple[str, str, dict]] | None = None
    exit_deltas: dict[str, float] | None = None
    out["counterfactual"] = 0
    for rid, lane, pred_json in rules:
        try:
            pred = json.loads(pred_json)
        except (TypeError, ValueError):
            continue
        if lane == EXIT_LANE:
            # One observation per SESSION: that session's mean paired ΔR of
            # the policy against the plan, on the same candidates and fills.
            if exit_deltas is None:
                exit_deltas = _exit_session_deltas(con, d)
            policy = (pred.get("conditions") or [{}])[0].get("level")
            if policy in exit_deltas and record_rule_observation(
                    con, rid, d, exit_deltas[policy], f"runner_exit:{d}:{policy}",
                    "runner_exit"):
                out["recorded"] += 1
            continue
        if lane not in cache:
            cache[lane] = morning_rows(con, d) if lane == "morning" else runner_rows(con, d)
        hits = [row for row in cache[lane] if matches(pred, row)]
        if lane == "morning":
            for row in hits:
                if record_rule_observation(con, rid, d, realized_r(row),
                                           f"outcome:{row.get('pick_id')}", "taken"):
                    out["recorded"] += 1
        else:
            # One observation per (session, industry) UNIT, valued at its rows'
            # mean R (agent/clusters.py). Recorded per row, a runner rule could
            # collect its whole forward n on one session: thirty-odd regional
            # banks firing in one half hour are one event, not thirty.
            from .clusters import units
            for (sess, cluster), vals in units(hits, realized_r).items():
                key = f"runner:{sess}:{cluster}"
                if record_rule_observation(con, rid, d, sum(vals) / len(vals), key,
                                           "runner_field"):
                    out["recorded"] += 1
        # EVIDENCE_BACKLOG_SPEC_v1 Phase 5: the desk's counterfactual streams
        # -- rejected candidates (shadow ledger) and the gap population's
        # mechanical monkeys -- give every open morning thesis a confirming or
        # disconfirming channel at no trading cost. Recorded with their source,
        # NEVER counted: the desk did not take them, so they cannot validate a
        # rule about what the desk takes.
        if lane == "morning":
            if counterfactual is None:
                counterfactual = _counterfactual_rows(con, d)
            for key, source, row in counterfactual:
                if matches(pred, row) and record_rule_observation(
                        con, rid, d, realized_r(row), key, source):
                    out["counterfactual"] += 1
    return out


def _counterfactual_rows(con: Any, d: date) -> list[tuple[str, str, dict]]:
    """(source_key, source, row) for session d's shadow and population rows.

    Only the fields these rows actually carry can match a predicate (setup_type,
    direction, regime_tag); an outcome-indicator condition labels as '?' and
    never matches, which is the honest answer for a row nobody graded that way.
    """
    rows: list[tuple[str, str, dict]] = []
    regime = None
    try:
        r = con.execute("SELECT regime_tag FROM daily_plans WHERE plan_date = ?", [d]).fetchone()
        regime = r[0] if r else None
    except Exception:
        pass
    try:
        for rej_id, direction, setup, forgone in con.execute(
                """SELECT rejection_id, direction, setup_type, forgone_r FROM rejections
                   WHERE plan_date = ? AND forgone_r IS NOT NULL
                     AND COALESCE(eod_outcome, '') <> 'unmeasurable'""", [d]).fetchall():
            rows.append((f"shadow:{rej_id}", "shadow",
                         {"direction": direction, "setup_type": setup, "regime_tag": regime,
                          "r_multiple": forgone}))
    except Exception:
        pass
    try:
        for sym, arm, direction, r_mult in con.execute(
                """SELECT symbol, arm, direction, r_multiple FROM population_outcomes
                   WHERE plan_date = ? AND r_multiple IS NOT NULL""", [d]).fetchall():
            rows.append((f"population:{d}:{sym}:{arm}", "population",
                         {"direction": direction, "setup_type": None, "regime_tag": regime,
                          "r_multiple": r_mult}))
    except Exception:
        pass
    return rows


def counterfactual_summary(con: Any) -> dict[str, dict]:
    """Per rule: counterfactual forward n and mean, by source (for provenance_md)."""
    out: dict[str, dict] = {}
    try:
        rows = con.execute(
            """SELECT rule_id, source, COUNT(*), AVG(r_multiple) FROM research_observations
               WHERE source IN ('shadow', 'population') GROUP BY rule_id, source""").fetchall()
    except Exception:
        return out
    for rid, source, n, mean in rows:
        out.setdefault(rid, {})[source] = {"n": int(n or 0), "mean": mean}
    return out


# --------------------------------------------------------------------------
# Deterministic decay (Phase 4)
# --------------------------------------------------------------------------

DECAY_SESSIONS = 30   # the directive's and playbook principle 8's own number


def market_sessions_between(start: date, end: date) -> int:
    """Market sessions strictly after `start`, up to and including `end`."""
    from datetime import timedelta
    from .util import is_market_day
    if start is None or end is None or end <= start:
        return 0
    n, d = 0, start + timedelta(days=1)
    while d <= end:
        if is_market_day(d):
            n += 1
        d += timedelta(days=1)
    return n


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


def apply_decay(con: Any, session_date: date, alpha: float = 0.05,
                stale_sessions: int = DECAY_SESSIONS, apply: bool = True) -> dict:
    """Silence and reversal, applied in code -- the miner is told, never asked.

    SILENCE: a VALIDATED rule with no counted forward observation for
    `stale_sessions` market sessions goes back to QUARANTINED with a fresh
    clock (it must re-earn validation on evidence dated after today). Silent
    a second time, it retires as `repeatedly stale`.

    REVERSAL: a VALIDATED rule whose trailing `forward_n_required` counted
    observations are significantly of the OPPOSITE sign to its discovery is
    retired as `forward effect reversed`.
    """
    from .stats import one_sample_t

    decayed: list[dict] = []
    retired: list[dict] = []
    for r in rule_states(con):
        if r["state"] != "VALIDATED":
            continue
        rid = r["rule_id"]
        # reversal first: a rule that reversed is falsified, not merely quiet
        counted = ", ".join(f"'{s}'" for s in COUNTED_SOURCES)
        obs = con.execute(
            f"""SELECT r_multiple FROM research_observations
               WHERE rule_id = ? AND COALESCE(source, 'taken') IN ({counted})
               ORDER BY session_date DESC LIMIT ?""",
            [rid, int(r["forward_n_required"])]).fetchall()
        vals = [float(o[0]) for o in obs if o[0] is not None]
        if len(vals) >= r["forward_n_required"] and r["discovery_effect"] is not None:
            m = sum(vals) / len(vals)
            sd = (sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5 if len(vals) > 1 else 0
            _t, _df, p = one_sample_t(m, sd, len(vals))
            disc_sign = 1 if r["discovery_effect"] > 0 else -1
            if p is not None and p < alpha and (1 if m > 0 else -1) != disc_sign:
                r["verdict"] = f"RETIRED — forward effect reversed (trailing mean {m:+.2f}R, p={p:.3f})"
                retired.append(r)
                if apply:
                    con.execute(
                        """UPDATE research_rules SET state='RETIRED', retired_at=?,
                           note = COALESCE(note,'') || ' | forward effect reversed' WHERE rule_id=?""",
                        [session_date, rid])
                continue
        last = _as_date(r["last_observed_at"]) or _as_date(r["validated_at"])
        silent = market_sessions_between(last, session_date) if last else 0
        if silent < stale_sessions:
            continue
        if r["decay_count"] >= 1:
            r["verdict"] = f"RETIRED — repeatedly stale (silent {silent} sessions, second decay)"
            retired.append(r)
            if apply:
                con.execute(
                    """UPDATE research_rules SET state='RETIRED', retired_at=?,
                       note = COALESCE(note,'') || ' | repeatedly stale' WHERE rule_id=?""",
                    [session_date, rid])
        else:
            r["verdict"] = (f"DECAYED — silent {silent} market sessions; back to QUARANTINED, "
                            "must re-earn validation on evidence after today")
            decayed.append(r)
            if apply:
                con.execute(
                    """UPDATE research_rules
                       SET state='QUARANTINED', quarantined_at=?, validated_at=NULL,
                           forward_n=0, forward_sum_r=0.0, forward_sumsq_r=0.0,
                           alpha_spent=0.0, looks_done=0,
                           decay_count = COALESCE(decay_count, 0) + 1,
                           note = COALESCE(note,'') || ?
                       WHERE rule_id=?""",
                    [session_date, f" | decayed {session_date}: silent {silent} sessions", rid])
    return {"decayed": decayed, "retired": retired}


# --------------------------------------------------------------------------
# Playbook write-time enforcement
# --------------------------------------------------------------------------

_RULE_LINE = re.compile(r"^\s*[-*]\s+(?P<body>.+)$")
_PROVENANCE = re.compile(r"\[(?:rule|provenance)\s*:\s*(?P<rid>R[0-9a-f]{12})\]", re.I)
_CONFIDENCE = re.compile(r"confidence\s*:\s*(?P<tier>\w+)", re.I)
_CLAIMS_EDGE = re.compile(
    r"\b(edge|expectancy|outperform|beats?|wins?\s+more|avgR\s*=\s*[+-]?\d)", re.I
)

_TIER_ORDER = {"none": 0, "unvalidated": 0, "provisional": 1, "low": 2,
               "moderate": 3, "established": 4}


def tier_for_n(n: int | None) -> str:
    """The confidence tier a REALIZED (triggered) n supports -- the table in
    the playbook's own Reading section. Picks that never triggered are not n."""
    n = int(n or 0)
    if n <= 0:
        return "none"
    if n < 8:
        return "provisional"
    if n < 20:
        return "low"
    if n < 40:
        return "moderate"
    return "established"


def tier_rank(tier: str | None) -> int:
    """Order of a stated tier; an unknown word ("high") ranks above every real
    tier, so it can never pass as a legitimate claim."""
    return _TIER_ORDER.get(str(tier or "").strip().lower(), 99)


def check_playbook_provenance(
    playbook_md: str, states: Sequence[dict]
) -> list[str]:
    """Write-time violations for the rewritten playbook. Returns warning strings.

    Extends the existing `_check_wr_suppression` contract rather than replacing
    it. Flags:
      1. A rule line making an edge claim with no [rule: Rxxxx] provenance tag.
      2. A provenance tag naming a rule that is not VALIDATED.
      3. A provenance tag naming a rule that does not exist at all.
      4. A Current Best Edge section citing a non-VALIDATED rule.

    Warnings, not exceptions: the weekly loop must never crash after its
    expensive LLM call has already completed. The same posture the indicator
    specs take on every enrichment failure.
    """
    by_id = {s["rule_id"]: s for s in states}
    violations: list[str] = []
    in_best_edge = False

    for raw in (playbook_md or "").splitlines():
        line = raw.rstrip()
        low = line.strip().lower()
        if low.startswith("## "):
            in_best_edge = "current best edge" in low
            continue

        m = _RULE_LINE.match(line)
        if not m:
            continue
        body = m.group("body")
        tags = _PROVENANCE.findall(body)
        claims = bool(_CLAIMS_EDGE.search(body))

        if claims and not tags:
            violations.append(f"edge claim with no [rule:] provenance tag: {body[:150]}")
            continue

        for rid in tags:
            rid = rid if rid.startswith("R") else f"R{rid}"
            st = by_id.get(rid)
            if st is None:
                violations.append(f"provenance tag {rid} names no registered rule: {body[:120]}")
            elif st["state"] != "VALIDATED":
                violations.append(
                    f"{rid} is {st['state']}, not VALIDATED, but is stated as a rule: {body[:120]}"
                )
            elif in_best_edge and st["forward_n"] < st["forward_n_required"]:
                violations.append(
                    f"{rid} appears in Current Best Edge with only {st['forward_n']} "
                    f"forward observations (needs {st['forward_n_required']})"
                )
    return violations


def provenance_md(states: Sequence[dict], looks: dict[str, Any],
                  counterfactual: dict[str, dict] | None = None) -> str:
    """Render the research-integrity section handed to the weekly miner."""
    counterfactual = counterfactual or {}

    def _cf(rid: str) -> str:
        # Counterfactual forward evidence: shown as n and SIGN only, and the
        # sign only at n >= 8 (the same floor as everything else). Never counted
        # toward validation.
        parts = []
        for source, v in sorted((counterfactual.get(rid) or {}).items()):
            sign = ("—" if v["n"] < DEFAULT_FORWARD_N or v["mean"] is None
                    else ("+" if v["mean"] > 0 else "-"))
            parts.append(f"{source} n={v['n']} sign {sign}")
        return ("\n    counterfactual forward (NOT counted): " + "; ".join(parts)) if parts else ""

    def _fmt(r: dict) -> str:
        eff = r["forward_mean_r"]
        eff_s = f"{eff:+.2f}R" if eff is not None else "n/a"
        p_s = f"{r['forward_p']:.3f}" if r["forward_p"] is not None else "n/a"
        mde_s = f"{r['forward_mde']:.2f}R" if r["forward_mde"] is not None else "n/a"
        return (f"- [{r['rule_id']}] {r['state']}: {r['statement'][:110]}\n"
                f"    discovered {r['discovered_at']} (n={r['discovery_n']}), "
                f"quarantined {r['quarantined_at'] or '—'}; "
                f"forward n={r['forward_n']}/{r['forward_n_required']}, "
                f"mean {eff_s}, p={p_s}, MDE={mde_s}" + _cf(r["rule_id"]))

    by_state: dict[str, list[dict]] = {}
    for r in states:
        by_state.setdefault(r["state"], []).append(r)

    sections = [
        "## RESEARCH INTEGRITY (computed by the system — not your judgment call)\n",
        f"Search width this run: {looks.get('looks_this_run', 0)} comparisons "
        f"({looks.get('testable_looks', 0)} testable) at FDR q={looks.get('q_level', 0.10)}.",
        f"At an uncorrected alpha=0.05 you would expect about "
        f"{looks.get('expected_false_positives_uncorrected', 0)} false findings this run "
        f"from chance alone. Every `significant` flag below has ALREADY been "
        f"FDR-corrected across the full batch — do NOT re-derive significance "
        f"from raw t-statistics.\n",
        "**Hard structural rule:** no rule may condition on more than "
        f"{MAX_RULE_DIMENSIONS} dimensions. This ledger cannot power a three-way "
        "interaction at any sample size it will reach in its lifetime.\n",
        "**Promotion rule:** only rules in state VALIDATED may appear in Setup "
        "Rules or Current Best Edge, and every rule line making an edge claim "
        "must carry its `[rule: Rxxxxxxxxxxxx]` tag. Validation requires FORWARD "
        "observations recorded after the quarantine date — never the sample the "
        "rule was discovered in.\n",
    ]
    for state in ("VALIDATED", "QUARANTINED", "DISCOVERED", "RETIRED",
                  "REJECTED_STRUCTURAL"):
        rules = by_state.get(state) or []
        if not rules:
            continue
        sections.append(f"### {state} ({len(rules)})")
        sections.extend(_fmt(r) for r in rules[:40])
        sections.append("")
    if not states:
        sections.append("(no rules registered yet — register this week's "
                        "candidates; none may be stated as edge until validated)")
    return "\n".join(sections)
