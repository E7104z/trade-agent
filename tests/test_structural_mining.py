"""
STRUCTURAL_MINING_SPEC_v1 — winners' shared attributes, long and short, all under one FDR batch.

Pinned here:
  Phase 1  pre-registered 2-D pairs, admitted only by the rule stated in advance
  Phase 2  consistency is descriptive: "structural" means same sign in every
           stratum with n >= 8, at least two strata
  Phase 3  the population family never pools long and short, and is pinned
           to one sim_version
  Phase 4  exact Fisher win-frequency tests keep their p through the batch
  Phase 5  width is reported by family; exploration is a measured dimension
"""
from __future__ import annotations

import random
from datetime import date

import duckdb
import pytest

from agent import buckets, integrity, ledger, research, structural
from agent.stats import Comparison, evaluate_search, fisher_exact_2x2


# ------------------------------------------------------------------ Phase 4

@pytest.mark.parametrize("table, ref", [
    ((3, 1, 1, 3), 0.4857142857),
    ((8, 2, 1, 5), 0.0349650350),
    ((10, 0, 0, 10), 1.082509e-05),
    ((1, 9, 11, 3), 0.002759456),
])
def test_fisher_matches_reference_values(table, ref):
    assert fisher_exact_2x2(*table) == pytest.approx(ref, rel=1e-6)


def test_fisher_degenerate_margins_are_untestable():
    assert fisher_exact_2x2(0, 0, 3, 4) is None
    assert fisher_exact_2x2(3, 0, 4, 0) is None


def test_a_fisher_comparison_keeps_its_p_in_the_batch():
    c = Comparison("winrate:setup_type", "gap_go", n=12, mean_r=None, std_r=None,
                   p_value=0.0123, test="fisher")
    res = evaluate_search([c])
    assert res.comparisons[0].p_value == 0.0123 and res.testable_looks == 1


def _row(setup, regime, r, direction="long", d=date(2026, 9, 1), **over):
    return {"setup_type": setup, "regime_tag": regime, "direction": direction,
            "r_multiple": r, "plan_date": d, **over}


def test_winrate_family_counts_wins_in_cell_vs_rest_of_lane():
    rows = ([_row("gap_go", "up", 1.0) for _ in range(9)] + [_row("gap_go", "up", -1.0)]
            + [_row("breakout", "up", -1.0) for _ in range(8)] + [_row("breakout", "up", 1.0)])
    comps = [c for c in structural.winrate_comparisons({"morning": rows})
             if c.dimension == "winrate:setup_type"]
    by_level = {c.level: c for c in comps}
    assert by_level["gap_go"].n == 10 and by_level["gap_go"].test == "fisher"
    assert by_level["gap_go"].p_value == pytest.approx(fisher_exact_2x2(9, 1, 1, 8))


# ------------------------------------------------------------------ Phase 1

def test_pair_spec_never_declares_more_than_two_dimensions():
    assert all(len(p) == 3 for p in structural.PAIR_SPEC)   # lane + exactly two dims
    for lane, a, b in structural.PAIR_SPEC:
        assert a in buckets.dimensions(lane) and b in buckets.dimensions(lane)


def test_an_inadmissible_pair_is_listed_not_computed():
    rows = [_row("gap_go", "up", 1.0) for _ in range(20)]      # one setup level only
    comps, status = structural.pair_comparisons({"morning": rows, "runner": []})
    st = {s["pair"]: s["admitted"] for s in status}
    assert st["regime_tag|setup_type"] is False
    assert not any(c.dimension == "regime_tag|setup_type" for c in comps)


def test_an_admissible_pair_emits_two_dimension_cells():
    rng = random.Random(3)
    rows = [_row(s, reg, rng.gauss(0, 1)) for s in ("gap_go", "breakout")
            for reg in ("up", "down") for _ in range(9)]
    comps, _ = structural.pair_comparisons({"morning": rows, "runner": []})
    cells = {c.level: c for c in comps if c.dimension == "regime_tag|setup_type"}
    assert set(cells) == {"down|breakout", "down|gap_go", "up|breakout", "up|gap_go"}
    assert all(c.n_dimensions == 2 and c.n == 9 for c in cells.values())


def test_s2_discovery_takes_a_pairs_numbers_from_the_batch():
    pred = research.canonical_predicate({"lane": "morning", "conditions": [
        {"dimension": "setup_type", "level": "gap_go"}, {"dimension": "regime_tag", "level": "up"}]})
    comp = Comparison("regime_tag|setup_type", "up|gap_go", 11, 0.4, 1.0, q_value=0.07,
                      n_dimensions=2)
    disc = research.discovery_stats(pred, [comp], [])
    assert disc == {"n": 11, "effect": 0.4, "q": 0.07,
                    "note": "pre-registered 2-D pair, screened in this run's batch"}


# ------------------------------------------------------------------ Phase 2

def _survivor(dim, level, mean):
    c = Comparison(dim, level, 30, mean, 1.0)
    c.significant = True
    return type("S", (), {"survivors": [c]})()


def test_a_finding_that_reverses_in_one_regime_is_pooled_not_structural():
    rows = ([_row("gap_go", "up", 1.0) for _ in range(20)]
            + [_row("gap_go", "down", -0.5) for _ in range(8)])
    res = structural.consistency(_survivor("setup_type", "gap_go", 0.6), {"morning": rows})
    assert res[0]["structural"] is False
    assert res[0]["detail"]["regime=down"] == {"n": 8, "sign": "-"}


def test_a_finding_that_holds_everywhere_is_structural():
    rows = ([_row("gap_go", "up", 1.0, d=date(2026, 8, 3)) for _ in range(10)]
            + [_row("gap_go", "down", 0.5, d=date(2026, 9, 3)) for _ in range(10)])
    res = structural.consistency(_survivor("setup_type", "gap_go", 0.7), {"morning": rows})
    assert res[0]["structural"] is True and res[0]["strata"] >= 2
    assert "STRUCTURAL" in structural.consistency_md(res)


def test_a_shared_dimension_is_read_in_the_other_lane_too():
    morning = [_row("gap_go", "up", 1.0, stop_dist_atr=0.7) for _ in range(10)]
    runner = [{"stop_dist_atr": 0.7, "r_multiple": -1.0, "session_date": date(2026, 9, 1)}
              for _ in range(9)]
    res = structural.consistency(_survivor("stop_atr_bucket", "0.5-1", 1.0),
                                 {"morning": morning, "runner": runner})
    assert res[0]["detail"]["lane=runner"] == {"n": 9, "sign": "-"}
    assert res[0]["structural"] is False


# ------------------------------------------------------------------ Phase 3

@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "s.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


def test_population_family_keeps_long_and_short_apart_and_pins_sim_version(con):
    # population rows carry the population simulator's own version line
    from agent.population import POPULATION_SIM_VERSION
    d = date(2026, 9, 1)
    for sym, gap in (("AAA", 6.0), ("BBB", -9.0)):
        con.execute("""INSERT INTO population_universe (plan_date, symbol, gap_pct, atr_pct,
                       in_scan_universe, was_picked) VALUES (?,?,?,?,TRUE,FALSE)""", [d, sym, gap, 4.0])
    rows = [("AAA", "momentum", "long", 1.0, POPULATION_SIM_VERSION),
            ("AAA", "fade", "short", -1.0, POPULATION_SIM_VERSION),
            ("BBB", "momentum", "short", 2.0, POPULATION_SIM_VERSION),
            ("BBB", "fade", "long", 0.5, POPULATION_SIM_VERSION - 1)]      # old sim: excluded
    for sym, arm, direction, r, sv in rows:
        con.execute("""INSERT INTO population_outcomes (plan_date, symbol, arm, direction,
                       r_multiple, sim_version) VALUES (?,?,?,?,?,?)""", [d, sym, arm, direction, r, sv])
    pop = structural.population_rows(con)
    assert len(pop) == 3
    comps = structural.population_comparisons(pop)
    dims = {c.dimension for c in comps}
    assert "population:momentum:long:pop_gap_atr_bucket" in dims
    assert "population:fade:short:pop_gap_atr_bucket" in dims
    assert not any(c.dimension.startswith("population:fade:long") for c in comps)
    gap = {(c.dimension, c.level) for c in comps if c.dimension.endswith("pop_gap_atr_bucket")}
    assert ("population:momentum:long:pop_gap_atr_bucket", "1-2") in gap     # 6/4 = 1.5 ATR
    assert ("population:momentum:short:pop_gap_atr_bucket", ">2") in gap     # |-9|/4


def test_every_family_joins_the_same_batch(con):
    # population rows carry the population simulator's own version line
    from agent.population import POPULATION_SIM_VERSION
    d = date(2026, 9, 1)
    con.execute("""INSERT INTO population_universe (plan_date, symbol, gap_pct, atr_pct)
                   VALUES (?, 'AAA', 6.0, 4.0)""", [d])
    con.execute("""INSERT INTO population_outcomes (plan_date, symbol, arm, direction, r_multiple,
                   sim_version) VALUES (?, 'AAA', 'momentum', 'long', 1.0, ?)""", [d, POPULATION_SIM_VERSION])
    pid = con.execute("""INSERT INTO picks (plan_date, ticker, direction, setup_type, size_r,
                         entry_type, entry_price, stop_price, target_price, conviction)
                         VALUES (?, 'T', 'long', 'gap_go', 0.5, 'open', 10, 9, 12, 3)
                         RETURNING pick_id""", [d]).fetchone()[0]
    ledger.save_outcome(con, {"pick_id": pid, "eval_date": d, "triggered": True,
                              "r_multiple": 1.0, "exit_reason": "eod"})
    search = integrity.build_search(con, ledger)
    fams = {structural._family(c.dimension) for c in search.comparisons}
    assert {"morning 1-D", "winner frequency (Fisher exact)",
            "population (mechanical, long/short separate)"} <= fams
    md = structural.width_md(search, search.pair_status)
    assert "Testable m =" in md and "not yet admissible" in md


# ------------------------------------------------------------------ Phase 5

def test_exploration_lane_is_a_measured_dimension():
    assert buckets.label("morning", "exploration_lane", {}) == "none"
    assert buckets.label("morning", "exploration_lane", {"exploration_lane": "backlog_probe"}) \
        == "backlog_probe"
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE p (exploration_lane VARCHAR)")
    con.execute("INSERT INTO p VALUES (NULL), ('rr_below_control')")
    got = [r[0] for r in con.execute(f"SELECT {buckets.sql('exploration_lane')} FROM p").fetchall()]
    con.close()
    assert got == ["none", "rr_below_control"]
    assert ("exploration_lane", "picks") in integrity.STATS_DIMENSION_SPEC
