"""
Tests for agent/integrity.py — the integration facade.

The single most important property: THE RESEARCH LAYER MUST NEVER BREAK A
TRADING LOOP. Every entry point is exercised here with a deliberately broken
collaborator to prove it degrades instead of raising.
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from agent import integrity, migrations_v4
from agent.friction import FrictionParams


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE picks (pick_id INTEGER, plan_date DATE, ticker VARCHAR)")
    c.execute("""CREATE TABLE outcomes (pick_id INTEGER, triggered BOOLEAN,
                 r_multiple DOUBLE, exit_reason VARCHAR, sim_version INTEGER)""")
    c.execute("""CREATE TABLE rejections (rejection_id INTEGER, plan_date DATE,
                 ticker VARCHAR, eod_outcome VARCHAR, forgone_r DOUBLE,
                 sim_version INTEGER)""")
    c.execute("CREATE TABLE daily_plans (plan_date DATE)")
    for stmt in migrations_v4.research_schema():
        c.execute(stmt)
    yield c
    c.close()


class _Cfg:
    def __init__(self, raw=None):
        self.raw = raw or {}

    def get(self, k, d=None):
        return self.raw.get(k, d)


class _Ledger:
    """Stands in for agent.ledger with the real _stats_rows output shape."""

    def __init__(self, rows=None, explode=()):
        self.rows = rows or {}
        self.explode = set(explode)

    def _get(self, dim):
        if dim in self.explode:
            raise RuntimeError("query blew up")
        return self.rows.get(dim, [])

    def stats_by(self, con, dim, lane=None):
        return self._get(dim)

    def stats_by_outcome(self, con, dim, lane=None):
        return self._get(dim)


def _row(dim, n=20, avg=0.4, std=1.0):
    return {"dim": dim, "n": n, "triggered": n, "wins": int(n * 0.6),
            "win_rate": 0.6, "avg_r": avg, "total_weighted_r": avg * n,
            "std_r": std, "t_stat": 1.0, "baseline_avg_r": 0.05}


# ------------------------------------------------------------------ morning

def test_on_morning_snapshots_the_population(con):
    bundle = {"premarket_gappers": [
        {"symbol": "AAAA", "last": 50.0, "prev_close": 48.0, "gap_pct": 4.2, "atr14": 2.0},
        {"symbol": "BBBB", "last": 20.0, "prev_close": 21.0, "gap_pct": -4.8, "atr14": 1.0},
    ]}
    plan = {"picks": [{"ticker": "AAAA"}]}
    res = integrity.on_morning(con, _Cfg(), date(2026, 8, 27), bundle, plan)
    assert res["population_rows"] == 2
    picked = dict(con.execute(
        "SELECT symbol, was_picked FROM population_universe").fetchall())
    assert picked["AAAA"] is True and picked["BBBB"] is False


def test_on_morning_can_be_disabled(con):
    res = integrity.on_morning(con, _Cfg({"population": {"enabled": False}}),
                               date(2026, 8, 27), {"premarket_gappers": []}, {})
    assert res["skipped"] == "disabled"


def test_on_morning_survives_a_broken_connection(con):
    class _Broken:
        def execute(self, *a, **k):
            raise RuntimeError("db gone")

    res = integrity.on_morning(_Broken(), _Cfg(), date(2026, 8, 27),
                               {"premarket_gappers": [{"symbol": "A", "last": 1,
                                                       "prev_close": 1}]}, {})
    assert res["population_rows"] == 0      # degraded, not raised


def test_on_morning_tolerates_a_malformed_bundle(con):
    for bundle in ({}, {"premarket_gappers": None}, None):
        assert integrity.on_morning(con, _Cfg(), date(2026, 8, 27), bundle, {})[
            "population_rows"] == 0


# ------------------------------------------------------------------ evening

class _MD:
    def daily_bar(self, sym, d):
        return {"open": 100, "high": 104, "low": 99, "close": 103, "volume": 1000}

    def minute_bars(self, sym, d):
        return [{"o": 100, "h": 104, "l": 99, "c": 103, "v": 1000}]


def test_on_evening_grades_the_population(con):
    integrity.on_morning(con, _Cfg(), date(2026, 8, 27), {"premarket_gappers": [
        {"symbol": "AAAA", "last": 50.0, "prev_close": 48.0, "gap_pct": 4.2, "atr14": 2.0}]}, {})
    res = integrity.on_evening(con, _Cfg(), _MD(), date(2026, 8, 27))
    assert res["population"]["graded"] == 2      # one symbol, two arms


def test_on_evening_survives_a_broken_market_data_provider(con):
    class _Broken:
        def daily_bar(self, *a):
            raise RuntimeError("feed down")

    integrity.on_morning(con, _Cfg(), date(2026, 8, 27), {"premarket_gappers": [
        {"symbol": "AAAA", "last": 50.0, "prev_close": 48.0, "gap_pct": 4.2, "atr14": 2.0}]}, {})
    res = integrity.on_evening(con, _Cfg(), _Broken(), date(2026, 8, 27))
    assert "population" in res                   # degraded, not raised


def test_on_evening_skips_paired_grading_unless_enabled(con):
    res = integrity.on_evening(con, _Cfg(), _MD(), date(2026, 8, 27),
                               simulate=lambda *a, **k: {})
    assert "paired" not in res


# ------------------------------------------------------------ stress ladder

def _pick():
    return {"entry_price": 25.0, "stop_price": 24.0, "size_r": 1.0,
            "direction": "long", "entry_type": "open", "target_price": 27.0}


def _sim_records_slippage(calls):
    def _sim(pick, bars, daily, **kw):
        calls.append(kw.get("slippage_bps"))
        return {"triggered": True, "r_multiple": 1.0 - (kw.get("slippage_bps", 0) / 100.0)}
    return _sim


def test_stress_ladder_resimulates_at_2x_and_3x(con):
    calls = []
    params = FrictionParams(model="participation", flat_bps=5.0)
    out = integrity.stress_ladder_for_pick(
        _pick(), [], None, _sim_records_slippage(calls), params,
        atr_pct=3.0, adv20=2_000_000, dollars_per_r=500.0)
    assert out["r_multiple_2x"] is not None and out["r_multiple_3x"] is not None
    assert out["r_multiple_3x"] < out["r_multiple_2x"]    # more friction, less R
    assert len(calls) == 2 and calls[1] > calls[0]
    assert out["friction_model"] == "participation"
    assert out["participation_pct"] is not None


def test_stress_ladder_does_not_recompute_the_primary_r_multiple(con):
    out = integrity.stress_ladder_for_pick(
        _pick(), [], None, _sim_records_slippage([]),
        FrictionParams(model="flat", flat_bps=5.0))
    assert "r_multiple" not in out       # the existing column is never touched


def test_stress_ladder_returns_none_for_untriggered_arms(con):
    def _never(pick, bars, daily, **kw):
        return {"triggered": False, "r_multiple": None}

    out = integrity.stress_ladder_for_pick(
        _pick(), [], None, _never, FrictionParams(model="flat", flat_bps=5.0))
    assert out["r_multiple_2x"] is None and out["r_multiple_3x"] is None


def test_stress_ladder_returns_empty_rather_than_fabricating_on_failure(con):
    def _boom(pick, bars, daily, **kw):
        raise RuntimeError("simulator exploded")

    assert integrity.stress_ladder_for_pick(
        _pick(), [], None, _boom, FrictionParams()) == {}


# ------------------------------------------------------------------- weekly

def test_build_search_applies_one_correction_across_all_dimensions(con):
    rows = {d: [_row(f"{d}_a"), _row(f"{d}_b")] for d, _ in integrity.STATS_DIMENSION_SPEC}
    search = integrity.build_search(con, _Ledger(rows))
    assert search.looks == len(integrity.STATS_DIMENSION_SPEC) * 2
    assert search.testable_looks == search.looks


def test_build_search_isolates_a_failing_dimension_query(con):
    rows = {d: [_row(f"{d}_a")] for d, _ in integrity.STATS_DIMENSION_SPEC}
    search = integrity.build_search(con, _Ledger(rows, explode={"setup_type"}))
    assert search.looks == len(integrity.STATS_DIMENSION_SPEC) - 1


def test_weekly_block_renders_every_section_and_records_the_looks(con):
    rows = {d: [_row(f"{d}_a")] for d, _ in integrity.STATS_DIMENSION_SPEC}
    res = integrity.weekly_block(con, _Cfg(), _Ledger(rows), date(2026, 8, 30))
    md = res["markdown"]
    assert "RESEARCH INTEGRITY" in md
    assert "POPULATION BASELINE" in md
    assert "SELECTION ABLATION" in md
    assert "QUARANTINE DECISIONS" in md
    assert res["looks"]["looks_this_run"] == len(integrity.STATS_DIMENSION_SPEC)
    assert con.execute("SELECT COUNT(*) FROM research_looks").fetchone()[0] == \
        len(integrity.STATS_DIMENSION_SPEC)


def test_weekly_block_survives_a_completely_broken_ledger_module(con):
    class _AllBroken:
        def stats_by(self, *a, **k):
            raise RuntimeError("nope")

        def stats_by_outcome(self, *a, **k):
            raise RuntimeError("nope")

    res = integrity.weekly_block(con, _Cfg(), _AllBroken(), date(2026, 8, 30))
    assert isinstance(res["markdown"], str)      # degraded, not raised
    assert res["looks"]["looks_this_run"] == 0


def test_weekly_block_names_under_powered_cells(con):
    # n=4 with std 1.5 has an MDE well above 1.0R -> must be called out.
    rows = {"setup_type": [_row("thin", n=4, avg=1.2, std=1.5)]}
    res = integrity.weekly_block(con, _Cfg(), _Ledger(rows), date(2026, 8, 30))
    assert "CANNOT detect a realistic effect" in res["markdown"]
    assert "setup_type/thin" in res["markdown"]


def test_weekly_block_omits_the_paired_section_unless_enabled(con):
    rows = {"setup_type": [_row("gap_go")]}
    off = integrity.weekly_block(con, _Cfg(), _Ledger(rows), date(2026, 8, 30))
    on = integrity.weekly_block(con, _Cfg({"paired": {"enabled": True}}),
                                _Ledger(rows), date(2026, 8, 30))
    assert "PAIRED ARM COMPARISON" not in off["markdown"]
    assert "PAIRED ARM COMPARISON" in on["markdown"]


def test_weekly_block_honors_a_custom_fdr_level(con):
    rows = {"setup_type": [_row("gap_go")]}
    res = integrity.weekly_block(con, _Cfg({"research": {"fdr_q": 0.01}}),
                                 _Ledger(rows), date(2026, 8, 30))
    assert res["looks"]["q_level"] == 0.01


def test_check_playbook_returns_violations_and_never_raises(con):
    pb = "## Setup Rules\n\n- gap_go longs have positive expectancy\n"
    assert len(integrity.check_playbook(pb, [])) == 1
    assert integrity.check_playbook(None, []) == []      # degraded, not raised


# ------------------------------------------------------------- feature flags

def test_observation_only_features_default_on_and_costly_ones_default_off():
    cfg = _Cfg()
    assert integrity._enabled(cfg, "population") is True
    assert integrity._enabled(cfg, "research") is True
    assert integrity._enabled(cfg, "paired") is False


def test_flags_accept_both_bool_and_block_forms():
    assert integrity._enabled(_Cfg({"paired": True}), "paired") is True
    assert integrity._enabled(_Cfg({"paired": {"enabled": True}}), "paired") is True
    assert integrity._enabled(_Cfg({"population": False}), "population") is False
