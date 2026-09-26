"""
Re-grading an outcome must not erase what the simulator does not produce.

ledger.save_outcome was DELETE-then-INSERT of the simulator's columns, so
`run.py regrade` wiped every re-graded pick's LLM process grade (grade,
grade_notes, written by apply_grades) and the live broker's realized R (live_r,
written by set_live_r).
"""
from __future__ import annotations

from datetime import date

import pytest

from agent import ledger, structural
from agent.population import POPULATION_SIM_VERSION

D = date(2026, 9, 25)


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "t.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


def _pick(con) -> int:
    return con.execute(
        """INSERT INTO picks (plan_date, ticker, direction, setup_type, entry_type,
                              entry_price, stop_price, target_price)
           VALUES (?, 'SMCX', 'long', 'dto_runner', 'open', 15.06, 14.955, 15.27)
           RETURNING pick_id""", [D]).fetchone()[0]


def test_regrade_keeps_the_grade_and_live_fields(con):
    pid = _pick(con)
    ledger.save_outcome(con, {"pick_id": pid, "eval_date": D, "triggered": True,
                              "r_planned": 1.93, "sim_version": 4})
    ledger.apply_grades(con, [{"pick_id": pid, "grade": "A", "why": "clean winner"}],
                        [{"pick_id": pid, "ticker": "SMCX"}])
    ledger.set_live_r(con, pid, 1.4)

    ledger.save_outcome(con, {"pick_id": pid, "eval_date": D, "triggered": True,
                              "r_planned": 1.76, "sim_version": 5})

    row = con.execute("""SELECT r_planned, sim_version, grade, live_r, COUNT(*) OVER ()
                         FROM outcomes WHERE pick_id = ?""", [pid]).fetchone()
    assert row == (1.76, 5, "A", 1.4, 1)
    assert "clean winner" in con.execute(
        "SELECT grade_notes FROM outcomes WHERE pick_id = ?", [pid]).fetchone()[0]


def test_sim_columns_are_fully_rewritten(con):
    pid = _pick(con)
    ledger.save_outcome(con, {"pick_id": pid, "eval_date": D, "triggered": True,
                              "exit_reason": "target", "r_planned": 1.9})
    ledger.save_outcome(con, {"pick_id": pid, "eval_date": D, "triggered": False,
                              "exit_reason": "no_trigger"})
    assert con.execute("SELECT triggered, exit_reason, r_planned FROM outcomes "
                       "WHERE pick_id = ?", [pid]).fetchone() == (False, "no_trigger", None)


def test_grade_is_written_when_supplied(con):
    pid = _pick(con)
    ledger.save_outcome(con, {"pick_id": pid, "eval_date": D, "grade": "B",
                              "grade_notes": "n"})
    ledger.save_outcome(con, {"pick_id": pid, "eval_date": D, "grade": "C",
                              "grade_notes": "m"})
    assert con.execute("SELECT grade, grade_notes FROM outcomes WHERE pick_id = ?",
                       [pid]).fetchone() == ("C", "m")


def test_population_rows_read_the_population_simulators_version(con):
    # Rows are stamped with POPULATION_SIM_VERSION; filtering them on the
    # grading version returned nothing from S7's first run on.
    con.execute("""INSERT INTO population_outcomes
                   (plan_date, symbol, arm, direction, r_multiple, sim_version)
                   VALUES (?, 'GAPR', 'fade', 'short', 0.8, ?)""",
                [D, POPULATION_SIM_VERSION])
    assert [r["symbol"] for r in structural.population_rows(con)] == ["GAPR"]
