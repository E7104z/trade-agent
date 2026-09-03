"""
Tests for agent/population.py — the unconditional gap-scan null.

Two things matter most here:
  1. The monkey's arithmetic is right (hand-computed R on constructed bars).
  2. It CANNOT break the evening loop. A control arm that can crash the thing
     it controls for is worse than having no control arm.
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from agent import migrations_v4
from agent.population import (
    POPULATION_SIM_VERSION,
    grade_population,
    population_md,
    population_stats,
    simulate_population_row,
    snapshot_universe,
)


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE outcomes (pick_id INTEGER, triggered BOOLEAN, r_multiple DOUBLE)")
    c.execute("CREATE TABLE daily_plans (plan_date DATE)")
    c.execute("CREATE TABLE picks (pick_id INTEGER, ticker VARCHAR)")
    for stmt in migrations_v4.STATEMENTS:
        c.execute(stmt)
    yield c
    c.close()


def _row(symbol="AAAA", gap_pct=4.0, atr14=2.0, last=50.0):
    return {"symbol": symbol, "gap_pct": gap_pct, "atr14": atr14, "last": last}


def _bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


# ------------------------------------------------------------ monkey arithmetic

def test_momentum_long_on_a_gap_up_exits_at_close():
    # open 100, ATR 2 -> stop 98. Low 99 never reaches it. Close 103.
    # R = (103 - 100) / 2 = +1.5
    out = simulate_population_row(_row(gap_pct=4.0, atr14=2.0), _bar(100, 104, 99, 103))
    assert out["direction"] == "long"
    assert out["stop_price"] == 98.0
    assert out["exit_reason"] == "eod"
    assert out["r_multiple"] == pytest.approx(1.5)


def test_momentum_long_stopped_books_exactly_minus_one_r():
    # A 1-ATR stop, by construction, is exactly -1R when hit.
    out = simulate_population_row(_row(gap_pct=4.0, atr14=2.0), _bar(100, 101, 97, 99))
    assert out["exit_reason"] == "stop"
    assert out["r_multiple"] == pytest.approx(-1.0)


def test_momentum_short_on_a_gap_down():
    # gap down -> momentum shorts. open 100, ATR 2 -> stop 102 (high 101 misses).
    # close 96 -> R = -1 * (96 - 100) / 2 = +2.0
    out = simulate_population_row(_row(gap_pct=-4.0, atr14=2.0), _bar(100, 101, 95, 96))
    assert out["direction"] == "short"
    assert out["stop_price"] == 102.0
    assert out["r_multiple"] == pytest.approx(2.0)


def test_fade_arm_takes_the_opposite_side_of_the_same_gap():
    bar = _bar(100, 104, 99, 103)
    mom = simulate_population_row(_row(gap_pct=4.0, atr14=2.0), bar, arm="momentum")
    fade = simulate_population_row(_row(gap_pct=4.0, atr14=2.0), bar, arm="fade")
    assert mom["direction"] == "long" and fade["direction"] == "short"
    # fade is stopped (high 104 >= stop 102); momentum rides to the close
    assert fade["exit_reason"] == "stop"
    assert fade["r_multiple"] == pytest.approx(-1.0)


def test_slippage_worsens_both_sides_of_the_monkey_trade():
    bar = _bar(100, 104, 99, 103)
    clean = simulate_population_row(_row(), bar, slippage_bps=0.0)
    slipped = simulate_population_row(_row(), bar, slippage_bps=50.0)
    assert slipped["entry_price"] > clean["entry_price"]   # pays up to get in
    assert slipped["exit_price"] < clean["exit_price"]     # sells lower
    assert slipped["r_multiple"] < clean["r_multiple"]


def test_stop_resolution_is_conservative_and_handicaps_the_monkey():
    # Dipped to the stop then recovered to close green. The daily-bar sim cannot
    # know the order, so it books the stop -- deliberately biasing the monkey DOWN.
    out = simulate_population_row(_row(gap_pct=4.0, atr14=2.0), _bar(100, 106, 97.9, 105))
    assert out["exit_reason"] == "stop"
    assert out["r_multiple"] == pytest.approx(-1.0)


# --------------------------------------------------------------- null handling

@pytest.mark.parametrize("row,bar", [
    (_row(), None),                              # no bar
    (_row(atr14=None), _bar(100, 104, 99, 103)),  # no ATR
    (_row(atr14=0.0), _bar(100, 104, 99, 103)),   # degenerate ATR
    (_row(gap_pct=None), _bar(100, 104, 99, 103)),  # no gap
    (_row(gap_pct=0.0), _bar(100, 104, 99, 103)),   # flat: no side to take
    (_row(), {"open": None, "high": 1, "low": 1, "close": 1}),  # broken bar
])
def test_unusable_inputs_return_no_data_never_a_fabricated_r(row, bar):
    out = simulate_population_row(row, bar)
    assert out["exit_reason"] == "no_data"
    assert out["r_multiple"] is None
    assert out["sim_version"] == POPULATION_SIM_VERSION


# ------------------------------------------------------------------- snapshot

def test_snapshot_excludes_index_etfs_and_flags_picked_names(con):
    gappers = [
        {"symbol": "AAAA", "last": 50.0, "prev_close": 48.0, "gap_pct": 4.2,
         "atr14": 2.0, "adv20": 3e6, "in_scan_universe": True},
        {"symbol": "BBBB", "last": 20.0, "prev_close": 21.0, "gap_pct": -4.8,
         "atr14": 1.0, "adv20": 1e6, "in_scan_universe": False},
        {"symbol": "SPY", "last": 640.0, "prev_close": 638.0, "gap_pct": 0.3},
    ]
    n = snapshot_universe(con, date(2026, 8, 27), gappers, picked_tickers=["aaaa"])
    assert n == 2
    rows = con.execute(
        "SELECT symbol, was_picked, atr_pct FROM population_universe ORDER BY symbol"
    ).fetchall()
    assert [r[0] for r in rows] == ["AAAA", "BBBB"]
    assert rows[0][1] is True and rows[1][1] is False
    assert rows[0][2] == pytest.approx(4.0)     # 2.0 / 50.0 * 100


def test_snapshot_is_idempotent_per_date(con):
    g = [{"symbol": "AAAA", "last": 50.0, "prev_close": 48.0, "gap_pct": 4.2, "atr14": 2.0}]
    for _ in range(3):
        snapshot_universe(con, date(2026, 8, 27), g)
    assert con.execute("SELECT COUNT(*) FROM population_universe").fetchone()[0] == 1


def test_snapshot_skips_rows_missing_a_price_anchor(con):
    g = [{"symbol": "AAAA", "last": None, "prev_close": 48.0, "gap_pct": 4.2},
         {"symbol": "BBBB", "last": 20.0, "prev_close": None, "gap_pct": -4.8}]
    assert snapshot_universe(con, date(2026, 8, 27), g) == 0


def test_snapshot_respects_the_symbol_cap(con):
    # letter-only synthetic symbols: digit-bearing ones now read as structural
    # instruments (Phase 1d shape filter) and are excluded before the cap
    g = [{"symbol": f"S{chr(65 + i // 26)}{chr(65 + i % 26)}", "last": 10.0,
          "prev_close": 9.0, "gap_pct": 5.0, "atr14": 1.0} for i in range(100)]
    assert snapshot_universe(con, date(2026, 8, 27), g, max_symbols=5) == 5


def test_snapshot_excludes_leveraged_products_and_warrants(con):
    # Phase 1d universe hygiene: leveraged/inverse/vol products and
    # warrant/unit-shaped symbols never enter the population baseline.
    gappers = [
        {"symbol": "AAAA", "last": 50.0, "prev_close": 48.0, "gap_pct": 4.2, "atr14": 2.0},
        {"symbol": "SOXL", "last": 30.0, "prev_close": 28.0, "gap_pct": 7.1, "atr14": 1.5},
        {"symbol": "TQQQ", "last": 60.0, "prev_close": 57.0, "gap_pct": 5.3, "atr14": 2.0},
        {"symbol": "UVXY", "last": 20.0, "prev_close": 18.0, "gap_pct": 11.1, "atr14": 2.5},
        {"symbol": "NVDL", "last": 45.0, "prev_close": 42.0, "gap_pct": 7.1, "atr14": 2.0},
        {"symbol": "GFAIW", "last": 1.2, "prev_close": 0.9, "gap_pct": 33.3, "atr14": 0.2},
        {"symbol": "PSQH.WS", "last": 0.6, "prev_close": 0.4, "gap_pct": 50.0, "atr14": 0.1},
    ]
    n = snapshot_universe(con, date(2026, 8, 27), gappers)
    assert n == 1
    rows = con.execute("SELECT symbol FROM population_universe").fetchall()
    assert [r[0] for r in rows] == ["AAAA"]


# ------------------------------------------------------------------- grading

class _MD:
    def __init__(self, bars, explode_on=()):
        self.bars, self.explode_on = bars, set(explode_on)

    def daily_bar(self, sym, d):
        if sym in self.explode_on:
            raise RuntimeError("feed down")
        return self.bars.get(sym)


def _seed(con, d=date(2026, 8, 27)):
    snapshot_universe(con, d, [
        {"symbol": "AAAA", "last": 50.0, "prev_close": 48.0, "gap_pct": 4.2, "atr14": 2.0},
        {"symbol": "BBBB", "last": 20.0, "prev_close": 21.0, "gap_pct": -4.8, "atr14": 1.0},
    ])
    return d


def test_grade_population_writes_both_arms_for_every_symbol(con):
    d = _seed(con)
    md = _MD({"AAAA": _bar(100, 104, 99, 103), "BBBB": _bar(50, 51, 45, 46)})
    res = grade_population(con, md, d)
    assert res["symbols"] == 2
    assert res["graded"] == 4                      # 2 symbols x 2 arms
    assert set(res["arms"]) == {"momentum", "fade"}
    assert con.execute("SELECT COUNT(*) FROM population_outcomes").fetchone()[0] == 4


def test_grade_population_is_idempotent_per_date(con):
    d = _seed(con)
    md = _MD({"AAAA": _bar(100, 104, 99, 103), "BBBB": _bar(50, 51, 45, 46)})
    for _ in range(3):
        grade_population(con, md, d)
    assert con.execute("SELECT COUNT(*) FROM population_outcomes").fetchone()[0] == 4


def test_a_broken_feed_isolates_per_symbol_and_never_raises(con):
    d = _seed(con)
    md = _MD({"BBBB": _bar(50, 51, 45, 46)}, explode_on={"AAAA"})
    res = grade_population(con, md, d)          # must not raise
    assert res["graded"] == 4
    reasons = dict(con.execute(
        "SELECT symbol, MIN(exit_reason) FROM population_outcomes GROUP BY symbol"
    ).fetchall())
    assert reasons["AAAA"] == "no_data"


def test_grade_population_with_no_snapshot_is_a_clean_noop(con):
    res = grade_population(con, _MD({}), date(2026, 8, 27))
    assert res["symbols"] == 0 and res["graded"] == 0
    assert "no population snapshot" in res["note"]


# --------------------------------------------------------- the selection edge

def _desk(con, rs):
    # Explicit column list: the v4 migrations add columns to `outcomes`, so a
    # positional INSERT would break the moment the schema grows again.
    for i, r in enumerate(rs):
        con.execute(
            "INSERT INTO outcomes (pick_id, triggered, r_multiple) VALUES (?, TRUE, ?)",
            [i, r],
        )


def test_selection_edge_is_measured_against_the_better_monkey(con):
    d = _seed(con)
    # momentum wins on AAAA (+1.5R), fade is stopped (-1R); mirrored on BBBB.
    grade_population(con, _MD({"AAAA": _bar(100, 104, 99, 103),
                               "BBBB": _bar(100, 101, 95, 96)}), d)
    _desk(con, [0.1, 0.2, 0.1])
    s = population_stats(con)
    assert s["best_arm"] == "momentum"
    assert s["desk"]["avg_r"] == pytest.approx(0.1333, abs=1e-3)
    # desk (0.133) minus the better monkey => negative selection edge
    assert s["selection_edge_r"] < 0


def test_a_desk_that_beats_the_population_shows_positive_selection_edge(con):
    d = _seed(con)
    grade_population(con, _MD({"AAAA": _bar(100, 101, 97, 99),
                               "BBBB": _bar(100, 103, 99, 102)}), d)
    _desk(con, [2.0, 2.5, 3.0])
    s = population_stats(con)
    assert s["selection_edge_r"] > 0


def test_population_stats_is_safe_on_an_empty_ledger(con):
    s = population_stats(con)
    assert s["arms"] == {} and s["best_arm"] is None
    assert s["selection_edge_r"] is None


def test_population_stats_session_window_filters_without_sql_errors(con):
    for i, day in enumerate((date(2026, 8, 25), date(2026, 8, 26), date(2026, 8, 27))):
        _seed(con, day)
        grade_population(con, _MD({"AAAA": _bar(100, 104, 99, 103),
                                   "BBBB": _bar(50, 51, 45, 46)}), day)
    everything = population_stats(con)
    windowed = population_stats(con, sessions=1)
    assert everything["arms"]["momentum"]["n"] == 6
    assert windowed["arms"]["momentum"]["n"] == 2


# ------------------------------------------------------------------- rendering

def test_population_md_states_the_verdict_and_the_precision_limit(con):
    d = _seed(con)
    grade_population(con, _MD({"AAAA": _bar(100, 104, 99, 103),
                               "BBBB": _bar(100, 101, 95, 96)}), d)
    _desk(con, [0.1, 0.2, 0.1])
    md = population_md(population_stats(con))
    assert "SELECTION EDGE" in md
    assert "DESTROYING value" in md
    assert "biases the monkeys DOWNWARD" in md.replace("that biases", "biases")
    assert "CANNOT answer this question" in md


def test_population_md_is_explicit_when_there_is_nothing_to_report(con):
    assert "no population outcomes recorded yet" in population_md(population_stats(con))
