"""
FORWARD_VALIDATION_SPEC_v1 — make "validated edge" reachable.

`research.record_rule_observation` was defined, documented as "called by the
evening loop", and never called; rules carried dimension names but no levels,
so no trade could ever be matched to one. No rule could ever be VALIDATED.

Pinned here:
  Phase 1  one bucketing source: generated SQL == the SQL it replaced == the
           Python labels the matcher uses, on every boundary
  Phase 2  predicates are validated against the cells the system emitted;
           identity comes from the cell, not the prose; discovery numbers are
           the system's own
  Phase 3  the evening hook records forward observations -- fill-valid rows
           only, forward-only, idempotent
  Phase 4  cohort FDR at graduation; decay by silence and by reversal in
           code; staleness counted on the market calendar
"""
from __future__ import annotations

import json
from datetime import date

import duckdb
import pytest

from agent import buckets, ledger, research
from agent.stats import Comparison

LEGACY_OUTCOME_SQL = {
    'ext_bucket': "CASE WHEN o.ext_atr_at_entry IS NULL THEN '?' WHEN o.ext_atr_at_entry < -2 THEN "
                  "'<-2' WHEN o.ext_atr_at_entry <= 2 THEN '-2..+2' ELSE '>+2' END",
    'gap_atr_bucket': "CASE WHEN o.gap_atr IS NULL THEN '?' WHEN o.gap_atr < 1 THEN '<1' WHEN "
                      "o.gap_atr < 2 THEN '1-2' ELSE '>2' END",
    'mcap_bucket': "CASE WHEN o.mkt_cap IS NULL THEN '?' WHEN o.mkt_cap < 2e9 THEN '<2B' WHEN "
                   "o.mkt_cap < 10e9 THEN '2-10B' ELSE '>10B' END",
    'or_alignment': "CASE WHEN o.or_break_dir IS NULL OR o.fill_vs_or IS NULL THEN '?' WHEN "
                    "(p.direction='long' AND o.or_break_dir='up')   OR (p.direction='short' AND "
                    "o.or_break_dir='down') THEN 'with' WHEN (p.direction='long' AND "
                    "o.or_break_dir='down')   OR (p.direction='short' AND o.or_break_dir='up') THEN "
                    "'against' ELSE '?' END",
    'rvol_bucket': "CASE WHEN o.rvol_at_1000 IS NULL THEN '?' WHEN o.rvol_at_1000 < 1 THEN '<1x' WHEN "
                   "o.rvol_at_1000 < 2 THEN '1-2x' ELSE '>2x' END",
    'short_bucket': "CASE WHEN o.short_pct_float IS NULL THEN '?' WHEN o.short_pct_float < 5 THEN "
                    "'<5%' WHEN o.short_pct_float < 15 THEN '5-15%' ELSE '>15%' END",
    'stop_atr_bucket': "CASE WHEN o.stop_dist_atr IS NULL THEN '?' WHEN o.stop_dist_atr < 0.5 THEN "
                       "'<0.5' WHEN o.stop_dist_atr < 1 THEN '0.5-1' WHEN o.stop_dist_atr < 2 THEN "
                       "'1-2' ELSE '>2' END",
    'trend_side_200d': "COALESCE(o.trend_side_200d, '?')",
    'vwap_side_at_fill': "COALESCE(o.vwap_side_at_fill, '?')",
}

EPS = 1e-9
NUMERIC_BOUNDARIES = {
    "rvol_at_1000": [None, 0.0, 1 - EPS, 1, 1 + EPS, 2 - EPS, 2, 5],
    "stop_dist_atr": [None, 0.0, 0.5 - EPS, 0.5, 1 - EPS, 1, 2 - EPS, 2, 3],
    "gap_atr": [None, -1, 1 - EPS, 1, 2 - EPS, 2, 9],
    "ext_atr_at_entry": [None, -3, -2 - EPS, -2, 0, 2, 2 + EPS, 5],
    "mkt_cap": [None, 1e9, 2e9 - 1, 2e9, 10e9 - 1, 10e9, 5e10],
    "short_pct_float": [None, 0, 5 - EPS, 5, 15 - EPS, 15, 40],
}


# ------------------------------------------------------------------ Phase 1

def _fixture_rows():
    rows = []
    for col, values in NUMERIC_BOUNDARIES.items():
        for v in values:
            rows.append({col: v})
    for side in (None, "above", "below"):
        rows.append({"vwap_side_at_fill": side, "trend_side_200d": side})
    for direction in ("long", "short"):
        for brk in (None, "up", "down"):
            for fvo in (None, 0.5):
                rows.append({"direction": direction, "or_break_dir": brk, "fill_vs_or": fvo})
    return rows


def test_generated_sql_legacy_sql_and_python_labels_agree_on_every_boundary():
    con = duckdb.connect(":memory:")
    cols = ["direction VARCHAR", "rvol_at_1000 DOUBLE", "stop_dist_atr DOUBLE", "gap_atr DOUBLE",
            "ext_atr_at_entry DOUBLE", "mkt_cap DOUBLE", "short_pct_float DOUBLE",
            "vwap_side_at_fill VARCHAR", "trend_side_200d VARCHAR", "or_break_dir VARCHAR",
            "fill_vs_or DOUBLE"]
    con.execute(f"CREATE TABLE t (id INTEGER, {', '.join(cols)})")
    names = [c.split()[0] for c in cols]
    rows = _fixture_rows()
    for i, r in enumerate(rows):
        con.execute(f"INSERT INTO t VALUES ({', '.join(['?'] * (len(names) + 1))})",
                    [i] + [r.get(n) for n in names])
    for dim in buckets.MORNING_OUTCOME_DIMS:
        gen = buckets.sql(dim).replace("o.", "t.").replace("p.", "t.")
        legacy = LEGACY_OUTCOME_SQL[dim].replace("o.", "t.").replace("p.", "t.")
        got_gen = dict(con.execute(f"SELECT id, {gen} FROM t").fetchall())
        got_leg = dict(con.execute(f"SELECT id, {legacy} FROM t").fetchall())
        assert got_gen == got_leg, dim
        for i, r in enumerate(rows):
            assert buckets.label("morning", dim, r) == got_leg[i], (dim, r)
    con.close()


def test_pick_dimensions_label_like_the_sql_cast():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE p (conviction INTEGER, in_scan_universe BOOLEAN, setup_type VARCHAR)")
    con.execute("INSERT INTO p VALUES (4, TRUE, 'gap_go'), (NULL, FALSE, NULL)")
    got = con.execute(f"SELECT {buckets.sql('conviction')}, {buckets.sql('in_scan_universe')}, "
                      f"{buckets.sql('setup_type')} FROM p").fetchall()
    con.close()
    py = [tuple(buckets.label("morning", d, r) for d in ("conviction", "in_scan_universe", "setup_type"))
          for r in ({"conviction": 4, "in_scan_universe": True, "setup_type": "gap_go"},
                    {"conviction": None, "in_scan_universe": False, "setup_type": None})]
    assert got == py


def test_runner_research_reexports_the_single_source():
    from agent import runner_research as rr
    assert rr.bucket_of is buckets.runner_label
    assert tuple(rr.MINING_DIMENSIONS) == buckets.RUNNER_DIMS


# ------------------------------------------------------------------ Phase 2

def _comps():
    return [Comparison("setup_type", "gap_go", 20, 0.3, 1.0, q_value=0.08),
            Comparison("setup_type", "breakout", 12, -0.1, 1.0, q_value=0.9),
            Comparison("stop_atr_bucket", "0.5-1", 15, 0.2, 1.1, q_value=0.4),
            Comparison("runner_rvol_bucket", "2-3", 40, 0.1, 1.2, q_value=0.5)]


@pytest.mark.parametrize("pred, state", [
    ({"lane": "morning", "conditions": [{"dimension": "setup_type", "level": "gap_go"}]}, ""),
    ({"lane": "morning", "conditions": [{"dimension": "setup_type", "level": "gap_go"},
                                         {"dimension": "stop_atr_bucket", "level": "0.5-1"}]}, ""),
    ({"lane": "runner", "conditions": [{"dimension": "runner_rvol_bucket", "level": "2-3"}]}, ""),
    ({"lane": "morning", "conditions": [{"dimension": "setup_type", "level": "moonshot"}]},
     "REJECTED_UNMATCHABLE"),
    ({"lane": "morning", "conditions": [{"dimension": "runner_rvol_bucket", "level": "2-3"}]},
     "REJECTED_UNMATCHABLE"),
    ({"lane": "morning", "conditions": [{"dimension": "setup_type", "level": "gap_go"},
                                         {"dimension": "stop_atr_bucket", "level": "0.5-1"},
                                         {"dimension": "direction", "level": "long"}]},
     "REJECTED_STRUCTURAL"),
    (None, "REJECTED_UNMATCHABLE"),
    ({"lane": "crypto", "conditions": []}, "REJECTED_UNMATCHABLE"),
])
def test_predicate_validation(pred, state):
    canon, got, _reason = research.validate_predicate(pred, _comps())
    assert got == state
    assert (canon is None) == bool(state)


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "f.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


P1 = {"lane": "morning", "conditions": [{"dimension": "setup_type", "level": "gap_go"}]}


def test_registration_uses_system_numbers_and_predicate_identity(con):
    a = research.register_candidate(
        con, {"statement": "gap_go works", "predicate": P1, "discovery_n": 99,
              "discovery_effect": 5.0}, date(2026, 9, 1), _comps(), {})
    b = research.register_candidate(
        con, {"statement": "gap go is a winner, reworded entirely", "predicate": P1},
        date(2026, 9, 8), _comps(), {})
    assert a["state"] == "QUARANTINED" and a["created"]
    assert b["rule_id"] == a["rule_id"] and not b["created"]
    st = next(s for s in research.rule_states(con) if s["rule_id"] == a["rule_id"])
    assert st["discovery_n"] == 20 and st["discovery_effect"] == 0.3 and st["discovery_q"] == 0.08
    assert "miner-reported" in st["note"]      # kept for audit only
    assert st["predicate"] == research.canonical_predicate(P1)


def test_two_condition_discovery_is_computed_from_rows_and_marked_unscreened(con):
    pred = {"lane": "morning", "conditions": [{"dimension": "setup_type", "level": "gap_go"},
                                              {"dimension": "stop_atr_bucket", "level": "0.5-1"}]}
    rows = [{"setup_type": "gap_go", "stop_dist_atr": 0.7, "r_multiple": 1.0},
            {"setup_type": "gap_go", "stop_dist_atr": 0.8, "r_multiple": 0.0},
            {"setup_type": "gap_go", "stop_dist_atr": 1.5, "r_multiple": 9.0}]
    reg = research.register_candidate(con, {"statement": "x", "predicate": pred},
                                      date(2026, 9, 1), _comps(), {"morning": rows})
    st = next(s for s in research.rule_states(con) if s["rule_id"] == reg["rule_id"])
    assert st["discovery_n"] == 2 and st["discovery_effect"] == pytest.approx(0.5)
    assert st["discovery_q"] is None and "unscreened" in st["note"]


def test_a_candidate_without_a_predicate_goes_to_the_graveyard(con):
    reg = research.register_candidate(con, {"statement": "gaps are good", "dimensions": ["setup_type"]},
                                      date(2026, 9, 1), _comps(), {})
    assert reg["state"] == "REJECTED_UNMATCHABLE"
    assert research.advance_quarantine(con, reg["rule_id"], date(2026, 9, 2)) == "REJECTED_UNMATCHABLE"


# ------------------------------------------------------------------ Phase 3

def _trade(con, d, setup, r, *, exit_reason="eod", anomaly=False, stop_dist_atr=0.7, i=[0]):
    i[0] += 1
    pid = con.execute(
        """INSERT INTO picks (plan_date, ticker, direction, setup_type, size_r, entry_type,
                              entry_price, stop_price, target_price, conviction)
           VALUES (?, ?, 'long', ?, 0.5, 'open', 10, 9, 12, 3) RETURNING pick_id""",
        [d, f"T{i[0]}", setup]).fetchone()[0]
    ledger.save_outcome(con, {"pick_id": pid, "eval_date": d, "triggered": exit_reason != "invalid_fill",
                              "r_multiple": r, "exit_reason": exit_reason,
                              "execution_anomaly": anomaly, "stop_dist_atr": stop_dist_atr})
    return pid


def test_observe_session_records_only_fill_valid_matching_forward_rows(con):
    q_day, d = date(2026, 9, 1), date(2026, 9, 2)
    reg = research.register_candidate(con, {"statement": "gap_go", "predicate": P1}, q_day, _comps(), {})
    _trade(con, d, "gap_go", 1.2)                                   # counts
    _trade(con, d, "breakout", 2.0)                                 # other cell
    _trade(con, d, "gap_go", None, exit_reason="invalid_fill")      # not a trade
    _trade(con, d, "gap_go", -1.0, anomaly=True)                    # thesis never tested
    _trade(con, d, ledger.RUNNER_SETUP, 3.0)                        # other lane
    _trade(con, q_day, "gap_go", 0.5)                               # in-sample (quarantine day)

    assert research.observe_session(con, d)["recorded"] == 1
    assert research.observe_session(con, d)["recorded"] == 0, "a retry must not recount"
    assert research.observe_session(con, q_day)["recorded"] == 0, "never in-sample"
    st = next(s for s in research.rule_states(con) if s["rule_id"] == reg["rule_id"])
    assert st["forward_n"] == 1 and st["forward_mean_r"] == pytest.approx(1.2)
    assert st["last_observed_at"] == str(d)


def test_observe_session_matches_runner_rules_on_current_versions_only(con, monkeypatch):
    from agent import runner_research as rr
    # This fixture's dates predate unit counting; keep the clock restart out of it.
    monkeypatch.setattr(research, "RUNNER_UNIT_COUNTING_FROM", date(2026, 1, 1))
    pred = {"lane": "runner", "conditions": [{"dimension": "runner_rvol_bucket", "level": "2-3"}]}
    q_day, d = date(2026, 9, 1), date(2026, 9, 2)
    reg = research.register_candidate(con, {"statement": "rvol 2-3", "predicate": pred},
                                      q_day, _comps(), {})
    scan_v, sim_v = rr.current_versions()
    for t, rvol, sv in (("A", 2.5, sim_v), ("B", 4.0, sim_v), ("C", 2.5, sim_v - 1)):
        con.execute("""INSERT INTO runner_candidates (session_date, ticker, rvol, scan_version)
                       VALUES (?,?,?,?)""", [d, t, rvol, scan_v])
        ledger.save_runner_candidate_outcome(con, {
            "session_date": d, "ticker": t, "eval_date": d, "triggered": True,
            "r_multiple": 1.0, "exit_reason": "eod", "sim_version": sv})
    assert research.observe_session(con, d)["recorded"] == 1
    obs = con.execute("SELECT source, source_key FROM research_observations WHERE rule_id = ?",
                      [reg["rule_id"]]).fetchall()
    # One observation per (session, industry) unit; A has no industry, so it
    # is its own unit, keyed by ticker (agent/clusters.py).
    assert obs == [("runner_field", f"runner:{d}:ticker:A")]


def test_counterfactual_sources_are_recorded_but_not_counted(con):
    reg = research.register_candidate(con, {"statement": "g", "predicate": P1},
                                      date(2026, 9, 1), _comps(), {})
    assert research.record_rule_observation(con, reg["rule_id"], date(2026, 9, 2), 2.0,
                                            "shadow:1", source="shadow")
    st = next(s for s in research.rule_states(con) if s["rule_id"] == reg["rule_id"])
    assert st["forward_n"] == 0


# ------------------------------------------------------------------ Phase 4

def _state(rid, p, n=8, mean=0.5, disc=0.5, days=30):
    return {"rule_id": rid, "state": "QUARANTINED", "statement": rid,
            "quarantined_at": "2026-07-01", "forward_n": n, "forward_n_required": 8,
            "forward_p": p, "forward_mean_r": mean, "discovery_effect": disc}


def _decisions(monkeypatch, states, on=date(2026, 8, 30)):
    monkeypatch.setattr(research, "rule_states", lambda con: [dict(s) for s in states])
    res = research.eligible_for_production(None, on, apply=False)
    return ({r["rule_id"] for r in res["graduated"]}, {r["rule_id"] for r in res["failed"]})


def test_cohort_bh_two_rules(monkeypatch):
    # m=2, alpha=0.05: rank-1 bound 0.025... p=0.04 fails it, rank-2 bound 0.05:
    # BH rejects all ranks <= the largest k with p_(k) <= k/m*alpha. p_(2)=0.30
    # > 0.05 and p_(1)=0.04 > 0.025, so NEITHER graduates at alpha=0.05.
    grad, fail = _decisions(monkeypatch, [_state("A", 0.04), _state("B", 0.30)])
    assert grad == set() and fail == {"A", "B"}
    # A lone rule at p=0.04 is exactly the old per-rule test.
    grad, _ = _decisions(monkeypatch, [_state("A", 0.04)])
    assert grad == {"A"}


def test_cohort_bh_rejects_what_uncorrected_alpha_would_pass(monkeypatch):
    grad, fail = _decisions(monkeypatch, [_state("A", 0.04), _state("B", 0.60), _state("C", 0.70)])
    assert grad == set(), "0.04 < 0.05 uncorrected, but not under BH with m=3"
    assert fail == {"A", "B", "C"}


def test_cohort_bh_still_graduates_a_strong_rule(monkeypatch):
    grad, _ = _decisions(monkeypatch, [_state("A", 0.001), _state("B", 0.60)])
    assert grad == {"A"}


def test_market_sessions_skip_weekends_and_holidays():
    # Fri 09-04 (exclusive) .. Tue 09-08: Sat, Sun, Labor Day, then Tue.
    assert research.market_sessions_between(date(2026, 9, 4), date(2026, 9, 8)) == 1
    assert research.market_sessions_between(date(2026, 9, 8), date(2026, 9, 4)) == 0


def _validated(con, validated_at, disc=0.5):
    reg = research.register_candidate(con, {"statement": "g", "predicate": P1},
                                      date(2026, 6, 1), _comps(), {})
    con.execute("""UPDATE research_rules SET state='VALIDATED', validated_at=?,
                   discovery_effect=? WHERE rule_id=?""", [validated_at, disc, reg["rule_id"]])
    return reg["rule_id"]


def _state_of(con, rid):
    return next(s for s in research.rule_states(con) if s["rule_id"] == rid)


def test_silence_decays_once_then_retires(con):
    rid = _validated(con, date(2026, 7, 1))
    out = research.apply_decay(con, date(2026, 8, 20))       # > 30 market sessions later
    assert [r["rule_id"] for r in out["decayed"]] == [rid]
    s = _state_of(con, rid)
    assert s["state"] == "QUARANTINED" and s["decay_count"] == 1 and s["forward_n"] == 0
    assert s["quarantined_at"] == "2026-08-20"

    con.execute("UPDATE research_rules SET state='VALIDATED', validated_at=? WHERE rule_id=?",
                [date(2026, 8, 21), rid])
    out = research.apply_decay(con, date(2026, 10, 15))
    assert [r["rule_id"] for r in out["retired"]] == [rid]
    assert _state_of(con, rid)["state"] == "RETIRED"


def test_a_recent_observation_keeps_a_rule_alive(con):
    rid = _validated(con, date(2026, 7, 1))
    con.execute("UPDATE research_rules SET last_observed_at=? WHERE rule_id=?",
                [date(2026, 8, 18), rid])
    assert research.apply_decay(con, date(2026, 8, 20)) == {"decayed": [], "retired": []}


def test_a_reversed_rule_retires(con):
    rid = _validated(con, date(2026, 8, 1), disc=0.5)
    for i in range(8):
        con.execute("""INSERT INTO research_observations (rule_id, session_date, r_multiple, source)
                       VALUES (?,?,?,'taken')""", [rid, date(2026, 8, 3 + i), -1.0 - 0.05 * (i % 2)])
    con.execute("UPDATE research_rules SET last_observed_at=? WHERE rule_id=?", [date(2026, 8, 12), rid])
    out = research.apply_decay(con, date(2026, 8, 14))
    assert [r["rule_id"] for r in out["retired"]] == [rid]
    assert "reversed" in out["retired"][0]["verdict"]


def test_setup_rule_staleness_counts_market_sessions(con):
    con.execute("INSERT INTO rule_confirmations (rule_id, session_date, pick_id) VALUES ('gap_go', ?, 1)",
                [date(2026, 9, 1)])
    st = ledger.rule_staleness(con, as_of=date(2026, 9, 10))
    # 09-02, 03, 04, 08, 09, 10 (Labor Day and the weekend excluded)
    assert st == [{"rule_id": "gap_go", "last_confirmed": "2026-09-01", "sessions_since": 6}]


def test_a_stale_setup_rule_is_annotated_at_write_time():
    from agent import playbook_enforcement as pe
    md = ("# P\n\n## Setup Rules\n\n- gap_go: hold above VWAP. [confidence: provisional, n=3]\n"
          "- breakout: 3 touches. [confidence: none, n=0, UNVALIDATED prior]\n")
    stale = [{"rule_id": "gap_go", "last_confirmed": "2026-07-01", "sessions_since": 41},
             {"rule_id": "breakout", "last_confirmed": "2026-07-01", "sessions_since": 41}]
    res = pe.enforce(md, [], None, stale)
    assert "- gap_go: hold above VWAP. [confidence: provisional, n=3] [auto-demoted: stale 41 sessions]" \
        in res.markdown
    # already UNVALIDATED: left alone
    assert "- breakout: 3 touches. [confidence: none, n=0, UNVALIDATED prior]\n" in res.markdown
    stale_hits = [v for v in res.violations if v.checker == "stale_setup_rule"]
    assert len(stale_hits) == 1 and stale_hits[0].action == "replaced"


def test_weekly_block_reports_decay_as_applied_fact(con, monkeypatch):
    from agent import integrity
    from agent.config import Config
    rid = _validated(con, date(2026, 7, 1))
    blk = integrity.weekly_block(con, Config(raw={}), ledger, date(2026, 8, 20))
    assert "DECAYED back to QUARANTINED" in blk["markdown"]
    assert rid in blk["markdown"]
