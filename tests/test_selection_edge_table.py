"""
Phase 2: Selection Edge promoted from prose to a tracked table.

Selection-edge-vs-baseline previously lived only inside the playbook's
"Current Best Edge" paragraph -- rewritten from scratch each week, so it had
no history. Nothing could answer "is selection getting better or worse?",
which is the single most important question about a process whose entire
justification is that picking beats not-picking.

These tests pin: the per-week row, the n-floor suppression (numbers AND
verdict), the write-time enforcement of that suppression, and the fact that
the table is emitted every week rather than only when it has news.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date

import pytest

from agent import ledger
from agent.loops.weekly import (_check_selection_edge_table, _selection_edge_md,
                                _selection_edge_row)
from agent.migrations_v13 import MIN_VERDICT_N


@pytest.fixture()
def con(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(path)
    monkeypatch.setattr(ledger, "DB_PATH", path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()
    for p in (path, path + ".wal"):
        try:
            os.unlink(p)
        except OSError:
            pass


def _abl(picks, actual=-18.2, baseline=0.5, pct=12.6, sessions=5):
    from agent.ablation import METHOD_VERSION
    return {"picks": picks, "actual_total_r": actual,
            "random_mean_total_r": baseline, "percentile": pct,
            "p_value": 0.87, "sessions": sessions, "method_version": METHOD_VERSION}


# ------------------------------------------------------------- the row

def test_edge_is_actual_minus_the_random_baseline():
    r = _selection_edge_row(_abl(25, actual=-18.2, baseline=0.5), date(2026, 9, 18))
    assert r["edge_r"] == pytest.approx(-18.7)
    assert r["n_triggered"] == 25
    assert r["pct_random_beaten"] == 12.6


def test_verdict_is_suppressed_below_the_n_floor():
    """The percentile is exact at any n. What cannot be generalized at low n
    is the jump from 'these picks' to 'the selection process'."""
    r = _selection_edge_row(_abl(MIN_VERDICT_N - 1), date(2026, 9, 18))
    assert r["verdict"].startswith("insufficient data")
    assert str(MIN_VERDICT_N) in r["verdict"]


@pytest.mark.parametrize("pct, expected", [
    (99.0, "selection beat random"),
    (2.0, "selection UNDERPERFORMED random"),
    (50.0, "no detectable selection edge"),
])
def test_verdict_wording_at_and_above_the_floor(pct, expected):
    r = _selection_edge_row(_abl(MIN_VERDICT_N, pct=pct), date(2026, 9, 18))
    assert r["verdict"] == expected


def test_an_underperforming_selector_is_named_as_such():
    """If the picker is actively worse than random, the table must say so
    rather than hedging -- that is the result most worth surfacing."""
    r = _selection_edge_row(_abl(30, pct=1.0), date(2026, 9, 18))
    assert "UNDERPERFORMED" in r["verdict"]


# ------------------------------------------------------- the rendering

def test_table_is_emitted_even_with_no_history():
    """Reported every week, including when there is nothing to report."""
    md = _selection_edge_md([])
    assert "## SELECTION EDGE" in md
    assert "insufficient data" in md


def test_low_n_row_suppresses_the_numbers_not_just_the_verdict():
    """A real-looking edge figure printed beside 'insufficient data' is the
    number a reader remembers -- the exact failure the Calibration
    write-time check was added to catch."""
    r = _selection_edge_row(_abl(3), date(2026, 9, 18))
    md = _selection_edge_md([r])
    assert "-18.70R" not in md
    assert "12.6%" not in md
    assert "insufficient data" in md


def test_row_at_the_floor_shows_its_numbers():
    r = _selection_edge_row(_abl(MIN_VERDICT_N), date(2026, 9, 25))
    md = _selection_edge_md([r])
    assert "-18.70R" in md
    assert "12.6%" in md


def test_history_renders_oldest_first_so_it_reads_as_a_trend(con):
    for i, wk in enumerate([date(2026, 9, 4), date(2026, 9, 11), date(2026, 9, 18)]):
        ledger.save_selection_edge(con, _selection_edge_row(_abl(25 + i), wk))
    md = _selection_edge_md(ledger.selection_edge_history(con))
    i4, i11, i18 = (md.index("2026-09-04"), md.index("2026-09-11"),
                    md.index("2026-09-18"))
    assert i4 < i11 < i18


# ------------------------------------------------------- persistence

def test_rerunning_a_week_overwrites_rather_than_double_counting(con):
    """The weekly loop is not guaranteed to run exactly once -- see the
    duplicate-changelog diagnosis. A history table that grew a spurious row
    on every retry would misreport the very trend it exists to show."""
    wk = date(2026, 9, 18)
    ledger.save_selection_edge(con, _selection_edge_row(_abl(25), wk))
    ledger.save_selection_edge(con, _selection_edge_row(_abl(30), wk))
    rows = ledger.selection_edge_history(con)
    assert len(rows) == 1
    assert rows[0]["n_triggered"] == 30


def test_rows_from_the_old_ablation_method_are_withdrawn(con):
    """The v7 -18.2R / 12.6% and v9 -6.77R / 0.3% rows were computed by method
    1: the desk's arm held only its triggered picks (runner picks included)
    while the random arm drew from rejections worth 0R when they never filled.
    Their numbers are not a point on the method-2 trend and are not shown."""
    ledger.save_selection_edge(con, {
        "week_ending": date(2026, 9, 11), "n_triggered": 22,
        "actual_total_r": -18.2, "random_mean_r": 0.0, "edge_r": -18.2,
        "pct_random_beaten": 12.6, "p_value": 0.87, "sessions": 8,
        "verdict": "no detectable selection edge", "sim_version": 3,
    })
    ledger.save_selection_edge(con, _selection_edge_row(_abl(25), date(2026, 10, 2)))
    md = _selection_edge_md(ledger.selection_edge_history(con))
    assert "-18.20R" not in md and "12.6% |" not in md.split("2026-10-02")[0]
    assert "withdrawn: ablation method 1" in md
    assert "-18.70R" in md                      # the method-2 row still shows
    assert _check_selection_edge_table(md) == []


# ------------------------------------------------ write-time enforcement

def test_write_time_check_catches_an_unsuppressed_low_n_row():
    bad = "| 2026-09-18 | 3 | -18.70R | 12.6% | insufficient data |"
    violations = _check_selection_edge_table(bad)
    assert len(violations) == 1
    assert "floor is n=" in violations[0]


def test_write_time_check_passes_a_properly_suppressed_row():
    good = "| 2026-09-18 | 3 | — | — | insufficient data (n=3 < 20) |"
    assert _check_selection_edge_table(good) == []


def test_write_time_check_passes_a_row_above_the_floor():
    ok = "| 2026-09-25 | 25 | -18.70R | 12.6% | no detectable selection edge |"
    assert _check_selection_edge_table(ok) == []


def test_write_time_check_ignores_unrelated_tables():
    """It must not fire on the Calibration table, which has a different shape
    (conviction 1-5 in column one, not a date)."""
    cal = "| 4 | 1 | 0% | -0.05 | insufficient data |"
    assert _check_selection_edge_table(cal) == []


def test_the_rendered_table_passes_its_own_write_time_check(con):
    """The renderer and the checker must agree -- a renderer that emitted a
    row its own checker rejects would warn on every clean run."""
    for n, wk in ((3, date(2026, 9, 11)), (25, date(2026, 9, 18))):
        ledger.save_selection_edge(con, _selection_edge_row(_abl(n), wk))
    md = _selection_edge_md(ledger.selection_edge_history(con))
    assert _check_selection_edge_table(md) == []
