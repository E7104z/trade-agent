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
    "record_looks",
    "looks_summary",
    "register_rule",
    "record_rule_observation",
    "advance_quarantine",
    "rule_states",
    "eligible_for_production",
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
                p_value, q_value, significant, mde, n_dimensions, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                session_date, r.get("dimension"), r.get("level"),
                int(r.get("n") or 0), r.get("mean_r"), r.get("std_r"),
                r.get("t_stat"), r.get("p_value"), r.get("q_value"),
                bool(r.get("significant")), r.get("mde"),
                int(r.get("n_dimensions") or 1), (r.get("note") or "")[:400],
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
               forward_n = 0, forward_sum_r = 0.0, forward_sumsq_r = 0.0
           WHERE rule_id = ?""",
        [session_date, rule_id],
    )
    return "QUARANTINED"


def record_rule_observation(
    con: Any, rule_id: str, session_date: date, r_multiple: float
) -> bool:
    """Record ONE forward observation against a quarantined rule.

    Returns False -- and records nothing -- when the observation does not
    post-date `quarantined_at`. This is the single most important guard in the
    module: it is what makes 'out-of-sample' mean out-of-sample rather than
    'counted again'. Called by the evening loop for each triggered pick whose
    setup matches a quarantined rule.
    """
    row = con.execute(
        "SELECT state, quarantined_at FROM research_rules WHERE rule_id = ?",
        [rule_id],
    ).fetchone()
    if not row:
        return False
    state, q_at = row
    if state != "QUARANTINED" or q_at is None or r_multiple is None:
        return False
    obs_date = session_date
    if hasattr(q_at, "date"):
        q_at = q_at.date()
    if obs_date <= q_at:
        return False   # in-sample: discovered on or before this session

    con.execute(
        "INSERT INTO research_observations (rule_id, session_date, r_multiple) VALUES (?,?,?)",
        [rule_id, obs_date, float(r_multiple)],
    )
    con.execute(
        """UPDATE research_rules
           SET forward_n = forward_n + 1,
               forward_sum_r = forward_sum_r + ?,
               forward_sumsq_r = forward_sumsq_r + ?
           WHERE rule_id = ?""",
        [float(r_multiple), float(r_multiple) ** 2, rule_id],
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
                  forward_n, forward_sum_r, forward_sumsq_r, note
           FROM research_rules ORDER BY discovered_at DESC, rule_id"""
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        (rid, stmt, dims, ndim, state, source, disc_at, disc_n, disc_eff, disc_q,
         q_at, val_at, ret_at, fwd_req, fwd_n, fwd_sum, fwd_sumsq, note) = r
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
        })
    return out


def eligible_for_production(
    con: Any,
    session_date: date,
    alpha: float = 0.05,
    min_quarantine_sessions: int = DEFAULT_MIN_QUARANTINE_SESSIONS,
    apply: bool = True,
) -> dict[str, list[dict]]:
    """Decide which quarantined rules graduate, fail, or keep waiting.

    Graduation requires ALL of:
      - forward_n >= forward_n_required          (enough forward evidence)
      - at least `min_quarantine_sessions` days elapsed since quarantine
      - forward p < alpha                        (the effect is still there)
      - forward effect has the SAME SIGN as discovery (a rule discovered as a
        winner that forward-tests as a significant LOSER is falsified, not
        validated -- an easy and expensive sign error to make)

    A rule that reaches its forward-n target without significance is RETIRED,
    not left waiting. Rules must be able to fail; a quarantine you can only
    graduate from is a delay, not a test.

    `apply=False` evaluates without writing -- used by tests and by the weekly
    loop's dry-run path.
    """
    graduated: list[dict] = []
    failed: list[dict] = []
    waiting: list[dict] = []

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

        target_met = r["forward_n"] >= r["forward_n_required"]
        time_met = elapsed is not None and elapsed >= min_quarantine_sessions
        p = r["forward_p"]
        sig = p is not None and p < alpha
        disc_sign = 0
        if r["discovery_effect"] is not None:
            disc_sign = 1 if r["discovery_effect"] > 0 else -1
        fwd_sign = 0
        if r["forward_mean_r"] is not None:
            fwd_sign = 1 if r["forward_mean_r"] > 0 else -1
        sign_ok = (disc_sign == 0) or (fwd_sign == disc_sign)

        if target_met and time_met and sig and sign_ok:
            r["verdict"] = "VALIDATED"
            graduated.append(r)
            if apply:
                con.execute(
                    "UPDATE research_rules SET state='VALIDATED', validated_at=? WHERE rule_id=?",
                    [session_date, r["rule_id"]],
                )
        elif target_met and time_met and p is None:
            # UNTESTABLE is not the same as FAILED. A degenerate forward sample
            # (zero observed variance, or n<2) means the test could not be run,
            # not that the rule was falsified. Retiring here would be an
            # unrecoverable false negative, since the playbook's graveyard rule
            # forbids re-proposing a retired rule without post-retirement
            # evidence. Hold it in quarantine and say so.
            r["verdict"] = ("WAITING — forward sample not testable "
                            "(zero variance or n<2); collecting more observations")
            waiting.append(r)
        elif target_met and time_met:
            reason = ("forward effect reversed sign" if not sign_ok
                      else f"forward evidence not significant (p={p})")
            r["verdict"] = f"RETIRED — {reason}"
            failed.append(r)
            if apply:
                con.execute(
                    """UPDATE research_rules SET state='RETIRED', retired_at=?,
                       note = COALESCE(note,'') || ' | ' || ? WHERE rule_id=?""",
                    [session_date, reason[:300], r["rule_id"]],
                )
        else:
            need_n = max(0, r["forward_n_required"] - r["forward_n"])
            need_d = max(0, min_quarantine_sessions - (elapsed or 0))
            r["verdict"] = f"WAITING — needs {need_n} more obs, {need_d} more days"
            waiting.append(r)

    return {"graduated": graduated, "failed": failed, "waiting": waiting}


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


def provenance_md(states: Sequence[dict], looks: dict[str, Any]) -> str:
    """Render the research-integrity section handed to the weekly miner."""
    def _fmt(r: dict) -> str:
        eff = r["forward_mean_r"]
        eff_s = f"{eff:+.2f}R" if eff is not None else "n/a"
        p_s = f"{r['forward_p']:.3f}" if r["forward_p"] is not None else "n/a"
        mde_s = f"{r['forward_mde']:.2f}R" if r["forward_mde"] is not None else "n/a"
        return (f"- [{r['rule_id']}] {r['state']}: {r['statement'][:110]}\n"
                f"    discovered {r['discovered_at']} (n={r['discovery_n']}), "
                f"quarantined {r['quarantined_at'] or '—'}; "
                f"forward n={r['forward_n']}/{r['forward_n_required']}, "
                f"mean {eff_s}, p={p_s}, MDE={mde_s}")

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
