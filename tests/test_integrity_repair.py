"""
INTEGRITY_REPAIR_SPEC_v1 — the FDR batch must not manufacture or blur edge.

Pinned here:

  A  a two-sample comparison keeps its own test through `evaluate_search`
     (the batch used to re-test the selection-edge delta as a one-sample mean
     with ONE arm's variance: Welch p=0.096 became batch p=0.003, significant)
  B2 the morning-lane dimensions never contain a legacy runner pick
  C  the integrity layer's own tables are readable through /api/table
"""
from __future__ import annotations

import random
from datetime import date

import pytest

from agent import integrity, ledger
from agent.stats import (Comparison, evaluate_search, min_detectable_effect_two_sample,
                         welch_t)

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

def _mean_std(vals):
    n = len(vals)
    m = sum(vals) / n
    sd = (sum((v - m) ** 2 for v in vals) / (n - 1)) ** 0.5 if n > 1 else None
    return m, sd


def _selection_edge(rows):
    """A two-sample (Welch) comparison of selected vs unselected, one value per
    session and side: the shape of the comparison this test pins in the batch."""
    by = {}
    for r in rows:
        by.setdefault((r["session_date"], r["selected"]), []).append(r["r_multiple"])
    sel = [sum(v) / len(v) for (d, s_), v in by.items() if s_]
    unsel = [sum(v) / len(v) for (d, s_), v in by.items() if not s_]
    m1, s1 = _mean_std(sel)
    m2, s2 = _mean_std(unsel)
    t, df, p = welch_t(m1, s1, len(sel), m2, s2, len(unsel))
    return {"p_value": p, "t_stat": t,
            "comparison": Comparison(
                dimension="selection_edge", level="selected_vs_unselected",
                n=min(len(sel), len(unsel)), mean_r=m1 - m2, std_r=None, t_stat=t, df=df,
                p_value=p, mde=min_detectable_effect_two_sample(len(sel), s1, len(unsel), s2),
                test="welch")}


def _null_cells(k=9):
    """Cells with no effect, standing in for the rest of the weekly batch."""
    return [Comparison("d", f"l{i}", n=30, mean_r=0.0 if i % 2 else 0.01, std_r=1.0)
            for i in range(k)]


def test_welch_p_survives_the_fdr_batch_unchanged():
    se = _selection_edge(_selection_rows())
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
    se = _selection_edge(_selection_rows())
    mde = se["comparison"].mde
    assert mde is not None
    # The unselected arm (sd 2.5) dominates the SE; a one-arm MDE from the
    # selected arm (sd 0.6) would be far smaller.
    from agent.stats import min_detectable_effect
    one_arm = min_detectable_effect(se["comparison"].n, 0.6)
    assert mde > one_arm


def test_untestable_two_sample_comparison_is_not_counted_in_m():
    welch_none = Comparison("selection_edge", "x", n=20, mean_r=0.1,
                            std_r=None, p_value=None, test="welch")
    a = Comparison("d", "a", n=30, mean_r=0.5, std_r=1.0)
    only = evaluate_search([a])
    both = evaluate_search([a, welch_none])
    assert both.testable_looks == only.testable_looks == 1
    assert both.comparisons[0].q_value == only.comparisons[0].q_value
    assert both.comparisons[1].q_value is None


def test_mixed_batch_corrects_across_every_test():
    welch = Comparison("selection_edge", "sel", n=20, mean_r=0.2, std_r=None,
                       t_stat=2.0, df=30.0, p_value=0.01, test="welch")
    ones = [Comparison("d", f"l{i}", n=30, mean_r=0.0, std_r=1.0) for i in range(4)]
    res = evaluate_search([welch] + ones, q=0.10)
    assert res.testable_looks == 5
    # BH rank-1 bound with m=5 is 0.02; 0.01 passes and its q is 5*0.01.
    assert res.comparisons[0].significant is True
    assert res.comparisons[0].q_value == pytest.approx(0.05)


def test_record_looks_stores_the_test(con):
    from agent import research
    se = _selection_edge(_selection_rows())
    res = evaluate_search([se["comparison"], Comparison("d", "a", 30, 0.2, 1.0)])
    research.record_looks(con, SESSION, res.comparisons, 0.10)
    tests = dict(con.execute(
        "SELECT dimension, test FROM research_looks WHERE session_date = ?", [SESSION]).fetchall())
    assert tests == {"selection_edge": "welch", "d": "one_sample"}


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
