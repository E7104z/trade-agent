"""
INTEGRITY_REPAIR_SPEC_v1 — the FDR batch must not manufacture or blur edge.

Pinned here:

  A  a two-sample comparison keeps its own test through `evaluate_search`
     (the batch used to re-test the selection-edge delta as a one-sample mean
     with ONE arm's variance: Welch p=0.096 became batch p=0.003, significant)
  B  the runner corpus the weekly search mines is pinned to the CURRENT
     scan_version and sim_version, and the miner is told what was excluded
  B2 the morning-lane dimensions never contain a runner pick
  C  the integrity layer's own tables are readable through /api/table
  P4 re-gating relabels exactly the v1 rows the v2 gates would have admitted
"""
from __future__ import annotations

import random
from datetime import date

import pytest

from agent import integrity, ledger
from agent import runner_research as rr
from agent.stats import Comparison, evaluate_search

SESSION = date(2026, 9, 16)


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "t.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


def _selection_rows(seed=1, sessions=12):
    """The reproduction from the self-assessment: 3 selected ~N(0.3, 0.6) and
    20 unselected ~N(0, 2.5) per session."""
    rng = random.Random(seed)
    rows = []
    for s in range(sessions):
        d = date(2026, 9, 1 + s)
        for _ in range(3):
            rows.append({"session_date": d, "selected": True, "r_multiple": rng.gauss(0.3, 0.6)})
        for _ in range(20):
            rows.append({"session_date": d, "selected": False, "r_multiple": rng.gauss(0.0, 2.5)})
    return rows


# ------------------------------------------------------------------ Defect A

def _null_cells(k=9):
    """Cells with no effect, standing in for the rest of the weekly batch."""
    return [Comparison("d", f"l{i}", n=30, mean_r=0.0 if i % 2 else 0.01, std_r=1.0)
            for i in range(k)]


def test_welch_p_survives_the_fdr_batch_unchanged():
    se = rr.selection_edge(_selection_rows())
    welch_p = se["p_value"]
    assert 0.05 < welch_p < 0.2  # the reproduction: not significant on its own test

    # In a batch of 10 the BH rank-1 bound is 0.01. The pre-fix re-test gave
    # p=0.0026 and cleared it; the Welch p does not.
    res = evaluate_search([se["comparison"]] + _null_cells())
    c = res.comparisons[0]
    assert c.test == "welch"
    assert c.p_value == welch_p, "the batch must keep the Welch p, not re-test one-sample"
    assert c.t_stat == se["t_stat"]
    assert c.significant is False
    assert c.std_r is None, "no single-arm std may describe a two-arm comparison"


def test_two_sample_mde_uses_both_arms():
    se = rr.selection_edge(_selection_rows())
    mde = se["comparison"].mde
    assert mde is not None
    # The unselected arm (sd 2.5) dominates the SE; a one-arm MDE from the
    # selected arm (sd 0.6) would be far smaller.
    from agent.stats import min_detectable_effect
    one_arm = min_detectable_effect(se["comparison"].n, 0.6)
    assert mde > one_arm


def test_untestable_two_sample_comparison_is_not_counted_in_m():
    welch_none = Comparison("runner_selection_edge", "x", n=20, mean_r=0.1,
                            std_r=None, p_value=None, test="welch")
    a = Comparison("d", "a", n=30, mean_r=0.5, std_r=1.0)
    only = evaluate_search([a])
    both = evaluate_search([a, welch_none])
    assert both.testable_looks == only.testable_looks == 1
    assert both.comparisons[0].q_value == only.comparisons[0].q_value
    assert both.comparisons[1].q_value is None


def test_mixed_batch_corrects_across_every_test():
    welch = Comparison("runner_selection_edge", "sel", n=20, mean_r=0.2, std_r=None,
                       t_stat=2.0, df=30.0, p_value=0.01, test="welch")
    ones = [Comparison("d", f"l{i}", n=30, mean_r=0.0, std_r=1.0) for i in range(4)]
    res = evaluate_search([welch] + ones, q=0.10)
    assert res.testable_looks == 5
    # BH rank-1 bound with m=5 is 0.02; 0.01 passes and its q is 5*0.01.
    assert res.comparisons[0].significant is True
    assert res.comparisons[0].q_value == pytest.approx(0.05)


def test_record_looks_stores_the_test(con):
    from agent import research
    se = rr.selection_edge(_selection_rows())
    res = evaluate_search([se["comparison"], Comparison("d", "a", 30, 0.2, 1.0)])
    research.record_looks(con, SESSION, res.comparisons, 0.10)
    tests = dict(con.execute(
        "SELECT dimension, test FROM research_looks WHERE session_date = ?", [SESSION]).fetchall())
    assert tests == {"runner_selection_edge": "welch", "d": "one_sample"}


def test_runner_md_prose_and_table_carry_the_same_selection_p():
    rows = [dict(r, triggered=True, ticker=f"T{i}", scan_version=2, sim_version=4)
            for i, r in enumerate(_selection_rows())]
    se = rr.selection_edge(rows)
    search = evaluate_search([se["comparison"]] + _null_cells())
    md = rr.runner_md(rr.setup_edge(rows), se, search.comparisons[:1])
    assert f"p = {se['p_value']:.4f}" in md
    assert search.comparisons[0].p_value == se["p_value"]
    assert "runner_selection_edge | selected_vs_unselected" in md
    assert " * |" not in md.split("runner_selection_edge | selected_vs_unselected")[1].splitlines()[0]


# ------------------------------------------------------------------ Defect B

def _cand(d, t, r, scan_v, sim_v, selected=False):
    return {"session_date": d, "ticker": t, "triggered": True, "r_multiple": r,
            "selected": selected, "scan_version": scan_v, "sim_version": sim_v}


def test_current_candidate_rows_pins_both_versions(monkeypatch):
    rows = [_cand(SESSION, "OLD", 5.0, 1, 3), _cand(SESSION, "MIX", 5.0, 2, 3),
            _cand(SESSION, "NEW", 1.0, 2, 4)]
    monkeypatch.setattr(ledger, "runner_candidate_outcomes_since", lambda con, since: rows)
    monkeypatch.setattr(rr, "current_versions", lambda: (2, 4))
    kept = rr.current_candidate_rows(object())
    assert [r["ticker"] for r in kept] == ["NEW"]


def test_version_mix_counts_and_markdown_names_the_exclusion(monkeypatch):
    rows = ([_cand(SESSION, f"O{i}", 1.0, 1, 3) for i in range(5)]
            + [_cand(SESSION, f"N{i}", 1.0, 2, 4) for i in range(2)])
    monkeypatch.setattr(ledger, "runner_candidate_outcomes_since", lambda con, since: rows)
    monkeypatch.setattr(rr, "current_versions", lambda: (2, 4))
    mix = rr.version_mix(object())
    assert {(m["scan_version"], m["sim_version"]): (m["n"], m["current"]) for m in mix} == {
        (1, 3): (5, False), (2, 4): (2, True)}
    kept = rr.current_candidate_rows(object())
    md = rr.runner_md(rr.setup_edge(kept), rr.selection_edge(kept), [], mix)
    assert "Excluded, never pooled: scan v1 x sim v3 = 5" in md
    assert "scan v2 x sim v4 = 2" in md


def test_both_integrity_call_sites_use_the_pinned_rows(monkeypatch):
    calls = []

    def _pinned(con, sessions=60):
        calls.append("pinned")
        return []

    def _unpinned(*a, **k):
        if not k.get("scan_version") and not k.get("sim_version"):
            calls.append("UNPINNED")
        return []

    monkeypatch.setattr(rr, "current_candidate_rows", _pinned)
    monkeypatch.setattr(rr, "candidate_rows", _unpinned)
    monkeypatch.setattr(rr, "version_mix", lambda con, sessions=60: [])

    class _L:
        @staticmethod
        def stats_by(con, dim, lane=None):
            return []

        @staticmethod
        def stats_by_outcome(con, dim, lane=None):
            return []

    integrity.build_search(object(), _L())

    class _Cfg:
        def get(self, k, d=None):
            return d

    class _Con:
        def execute(self, *a, **k):
            raise RuntimeError("no db")

    integrity.weekly_block(_Con(), _Cfg(), _L(), SESSION)
    # build_search once directly, then weekly_block's own search + its runner
    # block (and the structural family's rows, which go through the same pin).
    assert calls.count("pinned") >= 3
    assert "UNPINNED" not in calls


# ----------------------------------------------------------------- Defect B2

def test_build_search_asks_for_the_morning_lane_only():
    seen = []

    class _L:
        @staticmethod
        def stats_by(con, dim, lane=None):
            seen.append(lane)
            return []

        @staticmethod
        def stats_by_outcome(con, dim, lane=None):
            seen.append(lane)
            return []

    integrity.build_search(object(), _L())
    assert seen and set(seen) == {"morning"}


def _pick(con, setup, r, i):
    row = con.execute(
        """INSERT INTO picks (plan_date, ticker, direction, setup_type, size_r,
                              entry_type, entry_price, stop_price, target_price, conviction)
           VALUES (?, ?, 'long', ?, 0.25, 'open', 10, 9, 12, 3) RETURNING pick_id""",
        [SESSION, f"T{i}", setup]).fetchone()
    ledger.save_outcome(con, {"pick_id": row[0], "eval_date": SESSION, "triggered": True,
                              "r_multiple": r, "exit_reason": "eod", "stop_dist_atr": 0.7})


def test_no_runner_pick_reaches_a_morning_dimension(con):
    _pick(con, "gap_go", 1.0, 0)
    for i in range(1, 6):
        _pick(con, ledger.RUNNER_SETUP, -1.0, i)

    for dim, source in integrity.STATS_DIMENSION_SPEC:
        fn = ledger.stats_by if source == "picks" else ledger.stats_by_outcome
        rows = fn(con, dim, lane="morning")
        assert sum(r["triggered"] for r in rows) == 1, dim

    setups = {r["dim"] for r in ledger.stats_by(con, "setup_type", lane="morning")}
    assert ledger.RUNNER_SETUP not in setups

    from agent.config import Config
    from agent.memory import build_weekly_memory
    mem = build_weekly_memory(con, Config(raw={}))
    assert {r["dim"] for r in mem["by_setup"]} == {"gap_go"}
    assert sum(r["triggered"] for r in mem["by_stop_atr_bucket"]) == 1


# ------------------------------------------------------------------ Defect C

@pytest.mark.parametrize("table", [
    "research_rules", "research_observations", "research_looks",
    "selection_edge_history", "population_universe", "population_outcomes",
    "paired_arm_picks",
])
def test_integrity_tables_are_readable(monkeypatch, tmp_path, table):
    from api import index
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "api.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(index, "CRON_SECRET", "s")
    client = index.app.test_client()
    resp = client.get(f"/api/table/{table}?limit=1", headers={"Authorization": "Bearer s"})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["table"] == table


def test_unknown_table_still_404s(monkeypatch, tmp_path):
    from api import index
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "api.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(index, "CRON_SECRET", "s")
    resp = index.app.test_client().get("/api/table/credentials",
                                        headers={"Authorization": "Bearer s"})
    assert resp.status_code == 404


# ----------------------------------------------------------------- Phase 4

def test_regate_targets_the_current_scan_version():
    """A SCAN_VERSION bump past 2 must fail here until regate is re-derived
    for it: v3 may change detection itself, which stored inputs cannot replay."""
    from agent import runner_regate
    from agent.runner_scan import SCAN_VERSION
    assert runner_regate.REGATE_TO == SCAN_VERSION


@pytest.mark.parametrize("row, downgraded, expected", [
    ({"ticker": "ABC", "rvol": 2.1, "prev_volume": 1e6}, False, True),
    ({"ticker": "ABC", "rvol": None, "prev_volume": 1e6}, False, False),
    ({"ticker": "ABC", "rvol": 2.1, "prev_volume": None}, False, False),
    ({"ticker": "IBIT", "rvol": 2.1, "prev_volume": 1e6}, False, False),
    ({"ticker": "ABC", "rvol": 2.1, "prev_volume": 1e6}, True, False),
])
def test_v2_admits_mirrors_the_three_v2_gates(row, downgraded, expected):
    from agent import runner_regate
    assert runner_regate.v2_admits(row, downgraded)[0] is expected


def test_regate_plan_and_apply(con):
    import json
    from agent import runner_regate

    def _c(d, t, rvol, prev_volume, scan_v=1):
        con.execute(
            """INSERT INTO runner_candidates (session_date, ticker, rvol, prev_volume, scan_version)
               VALUES (?,?,?,?,?)""", [d, t, rvol, prev_volume, scan_v])

    d1, d2 = date(2026, 9, 11), date(2026, 9, 15)
    _c(d1, "KEEP", 2.0, 1e6)
    _c(d1, "NORV", None, 1e6)
    _c(d1, "IBIT", 2.0, 1e6)
    _c(d2, "DOWN", 2.0, 1e6)
    _c(d2, "ALREADY", 2.0, 1e6, scan_v=2)
    con.execute("INSERT INTO runner_sessions (session_date, gates_unavailable) VALUES (?, ?)",
                [d2, json.dumps({"feed": "iex", "feed_downgraded": True})])

    res = runner_regate.plan(con, d1)
    assert res["checked"] == 4
    assert res["admit"] == [(d1, "KEEP")]
    assert sum(res["rejected_by_reason"].values()) == 3

    assert runner_regate.apply(con, res["admit"]) == 1
    rows = dict((t, (v, g)) for t, v, g in con.execute(
        "SELECT ticker, scan_version, regated_from FROM runner_candidates").fetchall())
    assert rows["KEEP"] == (2, 1)
    assert rows["NORV"] == (1, None)
    assert rows["ALREADY"] == (2, None)
