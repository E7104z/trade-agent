"""
Regression tests for the mock-mode isolation fix in agent/loops/{morning,evening,weekly}.py.

Root cause this pins: the dashboard's "Test mode" checkbox (cfg.mock=True) ran
the full pipeline against the REAL production ledger with no isolation --
mock LLM responses (mocks/*.json canned fixtures) got persisted as real picks,
outcomes, grades, lessons, and even playbook.md rewrites, and a real email
went out describing a fake trading day. This is confirmed to have happened:
production picks 1-3 (NVDA/CVNA/AMD, 2026-08-20) and their grades/lessons are
verbatim mocks/morning_risk.json + mocks/evening_grader.json content.

Each loop must now, under cfg.mock: still exercise prompts/parsing/validation
(so the pipeline is genuinely tested), but write nothing that would be
mistaken for real trading history, and send no email.

Runs fully offline (cfg.offline=True) against a throwaway DuckDB file --
no network, no Anthropic API key needed (LLMClient.call short-circuits to
the local mocks/*.json fixtures whenever cfg.mock is True).
"""
from __future__ import annotations

from datetime import date

import pytest

from agent import ledger
from agent.config import load_config
from agent.loops import evening, morning, weekly
from agent.memory import read_playbook


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "test.duckdb")
    c = load_config()
    c.mock = True
    c.offline = True
    return c


def _seed_one_pick(con, d: date) -> int:
    """Insert one plan/pick directly, standing in for a prior REAL (non-mock)
    morning run -- evening.py should still grade this for real even in a mock
    run, since the deterministic simulator doesn't depend on the LLM."""
    plan = {
        "no_trade": False, "market_stance": "test", "total_risk_r": 1.0,
        "picks": [{
            "ticker": "TEST", "direction": "long", "setup_type": "gap_go",
            "catalyst": "x", "thesis_final": "x", "conviction": 4, "size_r": 1.0,
            "entry_plan": {"type": "open", "price": 100.0}, "entry_price": 100.0,
            "stop_price": 98.0, "target_price": 104.0, "target2_price": None,
            "time_stop": None, "invalidation": "x",
        }],
        "rejected": [], "notes_for_evening": "",
    }
    ids = ledger.save_daily_plan(con, d, plan, {"candidates": []}, "chop")
    return ids[0]


# ------------------------------------------------------------------- morning

def test_morning_mock_run_writes_no_picks_or_plan(cfg, monkeypatch):
    sent = []
    monkeypatch.setattr("agent.loops.morning.send_email", lambda *a, **k: sent.append(1))
    monkeypatch.setattr("agent.loops.morning.render_dashboard", lambda *a, **k: None)

    morning.run(cfg, run_date=date(2026, 8, 24), force=True)

    con = ledger.connect()
    assert con.execute("SELECT count(*) FROM picks").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM daily_plans").fetchone()[0] == 0
    runs = con.execute("SELECT status FROM runs WHERE loop='morning'").fetchall()
    con.close()

    assert sent == []
    assert runs and runs[0][0] == "mock"


# ------------------------------------------------------------------- evening

def test_evening_mock_run_saves_outcomes_but_not_grades_or_lessons(cfg, monkeypatch):
    sent = []
    monkeypatch.setattr("agent.loops.evening.send_email", lambda *a, **k: sent.append(1))
    monkeypatch.setattr("agent.loops.evening.render_dashboard", lambda *a, **k: None)

    con = ledger.connect()
    d = date(2026, 8, 24)
    pick_id = _seed_one_pick(con, d)
    con.close()

    evening.run(cfg, run_date=d, force=True)

    con = ledger.connect()
    row = con.execute(
        "SELECT triggered, r_multiple, grade, grade_notes FROM outcomes WHERE pick_id = ?", [pick_id]
    ).fetchone()
    n_lessons = con.execute("SELECT count(*) FROM lessons").fetchone()[0]
    n_confirmations = con.execute("SELECT count(*) FROM rule_confirmations").fetchone()[0]
    runs = con.execute("SELECT status FROM runs WHERE loop='evening'").fetchall()
    con.close()

    # deterministic simulation IS real and DOES get saved, mock or not
    assert row is not None
    triggered, r_multiple, grade, grade_notes = row
    assert triggered is not None  # the sim ran for real against offline synthetic bars

    # LLM-derived writes must not happen under mock
    assert grade is None
    assert grade_notes is None
    assert n_lessons == 0
    assert n_confirmations == 0
    assert sent == []
    assert runs and runs[0][0] == "mock"


# -------------------------------------------------------------------- weekly

def test_weekly_mock_run_does_not_rewrite_playbook_or_lessons(cfg, monkeypatch):
    sent = []
    monkeypatch.setattr("agent.loops.weekly.send_email", lambda *a, **k: sent.append(1))
    monkeypatch.setattr("agent.loops.weekly.render_dashboard", lambda *a, **k: None)
    monkeypatch.setattr("agent.loops.weekly._git_commit_playbook", lambda *a, **k: None)

    con = ledger.connect()
    before_text, before_version = read_playbook(con)
    lesson_id = ledger.insert_lesson(
        con, {"lesson": "placeholder", "confidence": "low"}, "evening", date(2026, 8, 20))
    con.close()

    weekly.run(cfg, run_date=date(2026, 8, 24))

    con = ledger.connect()
    after_text, after_version = read_playbook(con)
    status = con.execute("SELECT status FROM lessons WHERE lesson_id = ?", [lesson_id]).fetchone()[0]
    runs = con.execute("SELECT status FROM runs WHERE loop='weekly'").fetchall()
    con.close()

    assert after_text == before_text
    assert after_version == before_version
    assert status == "active"  # not promoted/retired by the mock response
    assert sent == []
    assert runs and runs[0][0] == "mock"
