"""
Tests for agent/research.py — the looks registry and the rule quarantine.

The load-bearing assertion in this file is
`test_observation_on_or_before_quarantine_date_is_rejected`. Everything else in
the quarantine is bookkeeping; that one test is the difference between a real
out-of-sample test and counting the discovery sample twice.

Runs against a real in-memory DuckDB using the v4 statements verbatim, so a
schema/SQL mismatch fails here rather than in production.
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from agent import migrations_v4
from agent.research import (
    MAX_RULE_DIMENSIONS,
    advance_quarantine,
    check_playbook_provenance,
    eligible_for_production,
    looks_summary,
    provenance_md,
    record_looks,
    record_rule_observation,
    register_rule,
    rule_id_for,
    rule_states,
)
from agent.stats import Comparison, evaluate_search


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    # The two base tables v4 alters, minimally shaped.
    c.execute("CREATE TABLE outcomes (pick_id INTEGER, r_multiple DOUBLE)")
    c.execute("CREATE TABLE daily_plans (plan_date DATE)")
    c.execute("CREATE TABLE picks (pick_id INTEGER, ticker VARCHAR)")
    for stmt in migrations_v4.research_schema():
        c.execute(stmt)
    yield c
    c.close()


def test_v4_statements_are_idempotent(con):
    # connect() runs every migration on EVERY connection. Twice must be a no-op.
    for stmt in migrations_v4.research_schema():
        con.execute(stmt)
    for stmt in migrations_v4.research_schema():
        con.execute(stmt)


def test_v4_never_emits_the_literal_double_precision():
    # ledger.connect()'s Postgres branch rewrites DOUBLE -> DOUBLE PRECISION.
    # Writing it ourselves would produce "DOUBLE PRECISION PRECISION" there.
    for stmt in migrations_v4.research_schema():
        assert "DOUBLE PRECISION" not in stmt.upper()


def test_v4_uses_no_sequences(con):
    # DuckDB sequences would not survive the Postgres branch unchanged.
    for stmt in migrations_v4.research_schema():
        assert "nextval" not in stmt.lower()


# ------------------------------------------------------------- rule identity

def test_rule_id_is_stable_across_rewording_and_sample_drift():
    a = rule_id_for("gap_go longs in uptrend/risk_on (n=11)", ["setup_type", "regime_tag"])
    b = rule_id_for("gap_go longs in uptrend/risk_on, n=14",  ["regime_tag", "setup_type"])
    assert a == b     # sample counts and dimension ORDER must not fork identity


def test_rule_id_separates_genuinely_different_claims():
    a = rule_id_for("gap_go longs in uptrend", ["setup_type"])
    b = rule_id_for("gap_fade shorts in downtrend", ["setup_type"])
    assert a != b


def test_rule_id_has_the_shape_the_provenance_regex_expects():
    rid = rule_id_for("anything at all", ["x"])
    assert rid.startswith("R") and len(rid) == 13


# ------------------------------------------------------------ looks registry

def test_record_looks_persists_every_comparison_and_counts_the_width(con):
    comps = [Comparison(dimension="setup_type", level=f"s{i}", n=20,
                        mean_r=0.0, std_r=1.0) for i in range(30)]
    res = evaluate_search(comps, q=0.10)
    summary = record_looks(con, date(2026, 8, 30), res.comparisons, res.q_level)
    assert summary["looks_this_run"] == 30
    assert summary["testable_looks"] == 30
    assert summary["expected_false_positives_uncorrected"] == 1.5
    assert con.execute("SELECT COUNT(*) FROM research_looks").fetchone()[0] == 30


def test_record_looks_is_idempotent_per_session(con):
    comps = [Comparison(dimension="d", level="l", n=10, mean_r=0.1, std_r=1.0)]
    for _ in range(3):
        record_looks(con, date(2026, 8, 30), evaluate_search(comps).comparisons, 0.1)
    assert con.execute("SELECT COUNT(*) FROM research_looks").fetchone()[0] == 1


def test_looks_summary_accumulates_across_sessions(con):
    for i, d in enumerate((date(2026, 8, 16), date(2026, 8, 23), date(2026, 8, 30))):
        comps = [Comparison(dimension="d", level=f"l{j}", n=20, mean_r=0.0, std_r=1.0)
                 for j in range(10)]
        record_looks(con, d, evaluate_search(comps).comparisons, 0.1)
    s = looks_summary(con)
    assert s["sessions"] == 3
    assert s["cumulative_looks"] == 30
    assert s["per_session_avg"] == 10.0


# ---------------------------------------------------------------- lifecycle

def test_register_is_idempotent_and_starts_in_discovered(con):
    a = register_rule(con, "gap_go longs work in uptrend", ["setup_type"], date(2026, 8, 30))
    b = register_rule(con, "gap_go longs work in uptrend", ["setup_type"], date(2026, 9, 6))
    assert a["created"] is True and a["state"] == "DISCOVERED"
    assert b["created"] is False and b["rule_id"] == a["rule_id"]
    assert con.execute("SELECT COUNT(*) FROM research_rules").fetchone()[0] == 1


def test_three_way_rule_is_structurally_rejected_not_merely_flagged(con):
    r = register_rule(con, "gap_go x uptrend x rvol>2 prints +0.84R",
                      ["setup_type", "regime_tag", "rvol_bucket"], date(2026, 8, 30))
    assert r["state"] == "REJECTED_STRUCTURAL"
    assert str(MAX_RULE_DIMENSIONS) in r["note"]
    # and it can never be advanced
    assert advance_quarantine(con, r["rule_id"], date(2026, 9, 6)) == "REJECTED_STRUCTURAL"


def test_two_way_rule_is_allowed(con):
    r = register_rule(con, "gap_go longs in uptrend/risk_on",
                      ["setup_type", "regime_tag"], date(2026, 8, 30))
    assert r["state"] == "DISCOVERED"


def test_advance_moves_discovered_to_quarantined_once(con):
    r = register_rule(con, "breakouts before 10:00 outperform", ["setup_type"], date(2026, 8, 30))
    assert advance_quarantine(con, r["rule_id"], date(2026, 8, 30)) == "QUARANTINED"
    assert advance_quarantine(con, r["rule_id"], date(2026, 9, 6)) == "QUARANTINED"
    assert advance_quarantine(con, "Rdeadbeef1234", date(2026, 9, 6)) == "UNKNOWN"


# ---------------------------------------- THE load-bearing out-of-sample guard

def test_observation_on_or_before_quarantine_date_is_rejected(con):
    r = register_rule(con, "gap_go longs work", ["setup_type"], date(2026, 8, 30))
    rid = r["rule_id"]
    advance_quarantine(con, rid, date(2026, 8, 30))

    # in-sample: the day of discovery, and anything before it
    assert record_rule_observation(con, rid, date(2026, 8, 30), 1.5) is False
    assert record_rule_observation(con, rid, date(2026, 8, 20), 2.0) is False
    # out-of-sample: strictly after
    assert record_rule_observation(con, rid, date(2026, 8, 31), 1.0) is True

    st = {s["rule_id"]: s for s in rule_states(con)}[rid]
    assert st["forward_n"] == 1
    assert st["forward_mean_r"] == pytest.approx(1.0)
    assert con.execute("SELECT COUNT(*) FROM research_observations").fetchone()[0] == 1


def test_observations_are_ignored_outside_the_quarantined_state(con):
    r = register_rule(con, "some rule", ["setup_type"], date(2026, 8, 30))
    # still DISCOVERED — the clock has not started
    assert record_rule_observation(con, r["rule_id"], date(2026, 9, 5), 1.0) is False


def test_forward_mean_and_std_are_computed_from_running_sums(con):
    r = register_rule(con, "rule", ["setup_type"], date(2026, 8, 1))
    rid = r["rule_id"]
    advance_quarantine(con, rid, date(2026, 8, 1))
    for i, v in enumerate([1.0, 2.0, 3.0]):
        record_rule_observation(con, rid, date(2026, 8, 10 + i), v)
    st = {s["rule_id"]: s for s in rule_states(con)}[rid]
    assert st["forward_n"] == 3
    assert st["forward_mean_r"] == pytest.approx(2.0)
    assert st["forward_std_r"] == pytest.approx(1.0)   # sample std of 1,2,3


# --------------------------------------------------------------- graduation

def _quarantined(con, stmt, discovered, effect=0.8):
    r = register_rule(con, stmt, ["setup_type"], discovered,
                      discovery_n=12, discovery_effect=effect)
    advance_quarantine(con, r["rule_id"], discovered)
    return r["rule_id"]


def test_rule_with_strong_consistent_forward_evidence_graduates(con):
    rid = _quarantined(con, "gap_go longs work", date(2026, 6, 1), effect=0.8)
    for i in range(10):
        record_rule_observation(con, rid, date(2026, 7, 1), 1.0 + (0.05 if i % 2 else -0.05))
    res = eligible_for_production(con, date(2026, 8, 30))
    assert [r["rule_id"] for r in res["graduated"]] == [rid]
    assert {s["rule_id"]: s for s in rule_states(con)}[rid]["state"] == "VALIDATED"


def test_rule_that_reaches_target_without_significance_is_retired_not_left_waiting(con):
    rid = _quarantined(con, "noisy rule", date(2026, 6, 1), effect=0.8)
    for i in range(10):
        record_rule_observation(con, rid, date(2026, 7, 1), 2.0 if i % 2 else -2.0)
    res = eligible_for_production(con, date(2026, 8, 30))
    assert [r["rule_id"] for r in res["failed"]] == [rid]
    assert {s["rule_id"]: s for s in rule_states(con)}[rid]["state"] == "RETIRED"


def test_sign_reversal_is_falsification_not_validation(con):
    # Discovered as a WINNER, forward-tests as a significant LOSER. Without the
    # sign check this graduates on |t| alone — an easy, expensive error.
    rid = _quarantined(con, "reverses sign", date(2026, 6, 1), effect=+0.9)
    for i in range(10):
        record_rule_observation(con, rid, date(2026, 7, 1), -1.0 + (0.05 if i % 2 else -0.05))
    res = eligible_for_production(con, date(2026, 8, 30))
    assert [r["rule_id"] for r in res["failed"]] == [rid]
    assert "reversed sign" in res["failed"][0]["verdict"]


def test_rule_short_of_its_forward_target_keeps_waiting(con):
    rid = _quarantined(con, "young rule", date(2026, 6, 1))
    for _ in range(3):
        record_rule_observation(con, rid, date(2026, 7, 1), 1.0)
    res = eligible_for_production(con, date(2026, 8, 30))
    assert [r["rule_id"] for r in res["waiting"]] == [rid]
    assert "needs 5 more obs" in res["waiting"][0]["verdict"]


def test_minimum_quarantine_duration_blocks_a_fast_burst(con):
    # Ten observations in three days must NOT validate a rule — that is one
    # regime, not a forward test.
    rid = _quarantined(con, "burst rule", date(2026, 8, 1))
    for _ in range(10):
        record_rule_observation(con, rid, date(2026, 8, 3), 1.0)
    res = eligible_for_production(con, date(2026, 8, 4))
    assert [r["rule_id"] for r in res["waiting"]] == [rid]
    assert "more days" in res["waiting"][0]["verdict"]


def test_degenerate_zero_variance_sample_waits_rather_than_retiring(con):
    # Ten identical R values give zero sample variance, so no t-test exists.
    # "Untestable" must not be collapsed into "falsified": retirement is
    # effectively permanent under the graveyard rule, so this would be an
    # unrecoverable false negative.
    rid = _quarantined(con, "constant rule", date(2026, 6, 1))
    for _ in range(10):
        record_rule_observation(con, rid, date(2026, 7, 1), 1.0)
    res = eligible_for_production(con, date(2026, 8, 30))
    assert [r["rule_id"] for r in res["waiting"]] == [rid]
    assert "not testable" in res["waiting"][0]["verdict"]
    assert {s["rule_id"]: s for s in rule_states(con)}[rid]["state"] == "QUARANTINED"


def test_apply_false_evaluates_without_mutating_state(con):
    rid = _quarantined(con, "gap_go longs work", date(2026, 6, 1))
    for i in range(10):
        record_rule_observation(con, rid, date(2026, 7, 1), 1.0 + (0.05 if i % 2 else -0.05))
    res = eligible_for_production(con, date(2026, 8, 30), apply=False)
    assert len(res["graduated"]) == 1
    assert {s["rule_id"]: s for s in rule_states(con)}[rid]["state"] == "QUARANTINED"


# ------------------------------------------------------ playbook enforcement

def test_edge_claim_without_provenance_is_flagged(con):
    pb = "## Setup Rules\n\n- gap_go longs have positive expectancy in uptrends\n"
    v = check_playbook_provenance(pb, rule_states(con))
    assert len(v) == 1 and "no [rule:] provenance tag" in v[0]


def test_quarantined_rule_stated_as_a_rule_is_flagged(con):
    rid = _quarantined(con, "gap_go longs work", date(2026, 6, 1))
    pb = f"## Setup Rules\n\n- gap_go longs beat the baseline [rule: {rid}]\n"
    v = check_playbook_provenance(pb, rule_states(con))
    assert len(v) == 1 and "QUARANTINED, not VALIDATED" in v[0]


def test_validated_rule_with_provenance_passes_clean(con):
    rid = _quarantined(con, "gap_go longs work", date(2026, 6, 1))
    for i in range(10):
        record_rule_observation(con, rid, date(2026, 7, 1), 1.0 + (0.05 if i % 2 else -0.05))
    eligible_for_production(con, date(2026, 8, 30))
    pb = f"## Setup Rules\n\n- gap_go longs beat the baseline [rule: {rid}]\n"
    assert check_playbook_provenance(pb, rule_states(con)) == []


def test_unknown_provenance_tag_is_flagged(con):
    pb = "## Setup Rules\n\n- something with edge [rule: Rabcdef123456]\n"
    v = check_playbook_provenance(pb, rule_states(con))
    assert len(v) == 1 and "names no registered rule" in v[0]


def test_non_claim_lines_are_not_flagged(con):
    pb = ("## Core Principles\n\n"
          "- Risk is defined before entry, always.\n"
          "- No-trade is a position.\n")
    assert check_playbook_provenance(pb, rule_states(con)) == []


def test_provenance_md_states_the_search_width_and_the_hard_rules(con):
    _quarantined(con, "gap_go longs work", date(2026, 6, 1))
    md = provenance_md(rule_states(con), {"looks_this_run": 70, "testable_looks": 70,
                                          "q_level": 0.10,
                                          "expected_false_positives_uncorrected": 3.5})
    assert "70 comparisons" in md
    assert "3.5 false findings" in md
    assert "QUARANTINED (1)" in md
    assert "FDR-corrected" in md


def test_provenance_md_is_explicit_when_no_rules_exist(con):
    md = provenance_md(rule_states(con), {"looks_this_run": 0, "testable_looks": 0,
                                          "q_level": 0.1,
                                          "expected_false_positives_uncorrected": 0})
    assert "no rules registered yet" in md
