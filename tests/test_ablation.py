"""
Tests for agent/ablation.py — risk-manager selection vs random.

Two properties carry the module:
  1. Determinism under seed. A control whose answer drifts between runs over
     unchanged data is worse than no control.
  2. Directional correctness. A desk that deliberately took the WORST candidate
     every day must land in the bottom tail, and one that took the best in the
     top tail. If the sign can be wrong, nothing else matters.
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from agent import migrations_v4
from agent.ablation import ablation_md, candidate_pool, random_pick_ablation


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE picks (
        pick_id INTEGER, plan_date DATE, ticker VARCHAR, size_r DOUBLE)""")
    c.execute("""CREATE TABLE outcomes (
        pick_id INTEGER, triggered BOOLEAN, r_multiple DOUBLE,
        exit_reason VARCHAR, sim_version INTEGER)""")
    c.execute("""CREATE TABLE rejections (
        rejection_id INTEGER, plan_date DATE, ticker VARCHAR,
        eod_outcome VARCHAR, forgone_r DOUBLE, sim_version INTEGER)""")
    c.execute("CREATE TABLE daily_plans (plan_date DATE)")
    for stmt in migrations_v4.research_schema():
        c.execute(stmt)
    yield c
    c.close()


_PID = [0]
_RID = [0]


def _take(con, d, ticker, r, sim=2, exit_reason="eod"):
    _PID[0] += 1
    con.execute("INSERT INTO picks (pick_id, plan_date, ticker, size_r) VALUES (?,?,?,1.0)",
                [_PID[0], d, ticker])
    con.execute("""INSERT INTO outcomes (pick_id, triggered, r_multiple, exit_reason, sim_version)
                   VALUES (?, TRUE, ?, ?, ?)""", [_PID[0], r, exit_reason, sim])


def _reject(con, d, ticker, forgone, outcome="would_have_won", sim=2):
    _RID[0] += 1
    con.execute("""INSERT INTO rejections
                   (rejection_id, plan_date, ticker, eod_outcome, forgone_r, sim_version)
                   VALUES (?,?,?,?,?,?)""", [_RID[0], d, ticker, outcome, forgone, sim])


# ------------------------------------------------------------- pool assembly

def test_pool_unions_taken_picks_and_shadow_ledger_rejections(con):
    d = date(2026, 8, 27)
    _take(con, d, "AAAA", 1.0)
    _reject(con, d, "BBBB", 2.0)
    _reject(con, d, "CCCC", -1.0, outcome="would_have_lost")
    pool = candidate_pool(con)
    assert len(pool[str(d)]["taken"]) == 1
    assert len(pool[str(d)]["rejected"]) == 2


def test_pool_excludes_unmeasurable_rejections_rather_than_zero_filling(con):
    d = date(2026, 8, 27)
    _take(con, d, "AAAA", 1.0)
    _reject(con, d, "BBBB", None, outcome="unmeasurable")
    assert candidate_pool(con)[str(d)]["rejected"] == []


def test_pool_keeps_scratch_rejections_as_a_real_measured_zero(con):
    # A candidate that provably never filled contributes exactly 0.0R. That is
    # a measurement, not a missing value.
    d = date(2026, 8, 27)
    _take(con, d, "AAAA", 1.0)
    _reject(con, d, "BBBB", 0.0, outcome="scratch")
    rej = candidate_pool(con)[str(d)]["rejected"]
    assert len(rej) == 1 and rej[0]["r_multiple"] == 0.0


def test_pool_excludes_invalid_fill_picks(con):
    d = date(2026, 8, 27)
    _take(con, d, "AAAA", 1.0)
    _take(con, d, "BAD", -5.0, exit_reason="invalid_fill")
    assert [c["ticker"] for c in candidate_pool(con)[str(d)]["taken"]] == ["AAAA"]


def test_pool_respects_the_sim_version_boundary(con):
    d = date(2026, 8, 27)
    _take(con, d, "OLD", 1.0, sim=1)
    _take(con, d, "NEW", 2.0, sim=2)
    assert [c["ticker"] for c in candidate_pool(con, sim_version=2)[str(d)]["taken"]] == ["NEW"]
    assert [c["ticker"] for c in candidate_pool(con, sim_version=1)[str(d)]["taken"]] == ["OLD"]


def test_pool_drops_rejections_from_days_with_no_taken_picks(con):
    # A no-trade day has no k to draw, so its rejections cannot enter any arm.
    _reject(con, date(2026, 8, 27), "BBBB", 3.0)
    assert candidate_pool(con) == {}


def test_pool_session_window_keeps_the_most_recent(con):
    for i, d in enumerate((date(2026, 8, 25), date(2026, 8, 26), date(2026, 8, 27))):
        _take(con, d, f"T{i}", 1.0)
        _reject(con, d, f"R{i}", 0.5)
    assert sorted(candidate_pool(con, sessions=2)) == ["2026-08-26", "2026-08-27"]


# ---------------------------------------------------------------- directional

def test_a_desk_that_always_took_the_worst_candidate_lands_in_the_bottom_tail(con):
    for i in range(12):
        d = date(2026, 8, 1 + i)
        _take(con, d, "WORST", -2.0)
        _reject(con, d, "GOOD1", 2.0)
        _reject(con, d, "GOOD2", 2.5)
    res = random_pick_ablation(con, draws=2000)
    assert res["percentile"] <= 5.0
    assert res["actual_total_r"] < res["random_mean_total_r"]


def test_a_desk_that_always_took_the_best_candidate_lands_in_the_top_tail(con):
    for i in range(12):
        d = date(2026, 8, 1 + i)
        _take(con, d, "BEST", 3.0)
        _reject(con, d, "BAD1", -1.0, outcome="would_have_lost")
        _reject(con, d, "BAD2", -1.5, outcome="would_have_lost")
    res = random_pick_ablation(con, draws=2000)
    assert res["percentile"] >= 95.0
    assert res["p_value"] < 0.05


def test_a_middling_desk_is_indistinguishable_from_random(con):
    for i in range(12):
        d = date(2026, 8, 1 + i)
        _take(con, d, "MID", 0.0)
        _reject(con, d, "HI", 1.0)
        _reject(con, d, "LO", -1.0, outcome="would_have_lost")
    res = random_pick_ablation(con, draws=2000)
    assert 20.0 <= res["percentile"] <= 80.0
    assert res["p_value"] > 0.05


# -------------------------------------------------------------- determinism

def test_result_is_byte_identical_across_runs_with_the_same_seed(con):
    for i in range(8):
        d = date(2026, 8, 1 + i)
        _take(con, d, "A", 1.0)
        _reject(con, d, "B", 0.5)
        _reject(con, d, "C", -0.5, outcome="would_have_lost")
    a = random_pick_ablation(con, draws=1000)
    b = random_pick_ablation(con, draws=1000)
    assert a == b


def test_a_different_seed_changes_the_draw_but_not_the_actual(con):
    for i in range(8):
        d = date(2026, 8, 1 + i)
        _take(con, d, "A", 1.0)
        _reject(con, d, "B", 0.5)
        _reject(con, d, "C", -0.5, outcome="would_have_lost")
    a = random_pick_ablation(con, draws=1000, seed=1)
    b = random_pick_ablation(con, draws=1000, seed=2)
    assert a["actual_total_r"] == b["actual_total_r"]
    assert a["random_mean_total_r"] != b["random_mean_total_r"]


# ------------------------------------------------------------- guard rails

def test_sessions_with_no_alternative_are_excluded_and_counted(con):
    d1, d2 = date(2026, 8, 26), date(2026, 8, 27)
    _take(con, d1, "ONLY", 1.0)                 # nothing rejected: no choice existed
    _take(con, d2, "A", 1.0)
    _reject(con, d2, "B", 0.5)
    res = random_pick_ablation(con, draws=500)
    assert res["sessions"] == 1
    assert res["skipped_no_alternative"] == 1


def test_empty_ledger_returns_a_clean_not_computable_result(con):
    res = random_pick_ablation(con, draws=500)
    assert res["sessions"] == 0
    assert res["percentile"] is None
    assert "no session offered a genuine alternative" in res["note"]


def test_empirical_p_is_never_exactly_zero(con):
    # A finite Monte Carlo is not entitled to claim p = 0.
    for i in range(10):
        d = date(2026, 8, 1 + i)
        _take(con, d, "BEST", 100.0)
        _reject(con, d, "BAD", -100.0, outcome="would_have_lost")
    res = random_pick_ablation(con, draws=500)
    assert res["p_value"] > 0.0


def test_draw_size_matches_the_number_of_picks_actually_taken(con):
    # 2 picks taken from a 4-candidate pool: arms must draw 2, not 1 or 4.
    d = date(2026, 8, 27)
    _take(con, d, "A", 1.0)
    _take(con, d, "B", 1.0)
    _reject(con, d, "C", 0.0, outcome="scratch")
    _reject(con, d, "D", 0.0, outcome="scratch")
    res = random_pick_ablation(con, draws=200)
    assert res["picks"] == 2
    # pool total is 2.0 (A+B) and both rejects are 0.0, so every 2-draw sums
    # to one of {0.0, 1.0, 2.0} and the mean must land strictly between.
    assert 0.0 < res["random_mean_total_r"] < 2.0


# ------------------------------------------------------------------ rendering

def test_ablation_md_names_the_verdict_and_its_own_limitations(con):
    for i in range(12):
        d = date(2026, 8, 1 + i)
        _take(con, d, "BEST", 3.0)
        _reject(con, d, "BAD", -1.0, outcome="would_have_lost")
    md = ablation_md(random_pick_ablation(con, draws=1000))
    assert "beat" in md and "% of random portfolios" in md
    assert "UNWEIGHTED" in md
    assert "FAVORS the random arm" in md
    assert "reproducible" in md


def test_ablation_md_handles_the_not_computable_case(con):
    md = ablation_md(random_pick_ablation(con, draws=10))
    assert "not computable yet" in md
