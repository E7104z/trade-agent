"""
EDGE_ACCELERATION_SPEC_v1 Part 3 -- the agent audits its own production data.

Pinned here: every check runs on an empty ledger without raising and stores
one row each; each check's warn/bad condition fires on the state it exists to
catch, and stays quiet otherwise; the newest findings reach the health panel.
"""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from agent import health, ledger, selfcheck
from agent.config import Config

D = date(2026, 10, 9)          # a Friday


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "sc.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


def _cfg(**over):
    raw = {"population": {"enabled": True}, "runner": {"enabled": True},
           "exploration_lane": {"enabled": True, "backlog_probe": True},
           "live_paper": {"max_picks_per_day": 4},
           "hypothesis_lane": {"enabled": True}, "persona": {"enabled": True, "version": 1}}
    raw.update(over)
    return Config(raw=raw)


def _by_key(results):
    return {r["key"]: r for r in results}


def _pick(con, d, *, lane=None, hyp=None, graded=True, triggered=True, ticker="AAA"):
    pid = con.execute(
        """INSERT INTO picks (plan_date, ticker, direction, setup_type, size_r, entry_type,
                              entry_price, stop_price, target_price, conviction,
                              exploration_lane, hypothesis_id)
           VALUES (?, ?, 'long', 'gap_go', 0.25, 'open', 10, 9, 12, 3, ?, ?) RETURNING pick_id""",
        [d, ticker, lane, hyp]).fetchone()[0]
    if graded:
        con.execute("INSERT INTO outcomes (pick_id, eval_date, triggered, r_multiple) VALUES (?, ?, ?, 1.0)",
                    [pid, d, triggered])
    return pid


def test_every_check_runs_on_an_empty_ledger_and_is_stored(con):
    res = selfcheck.run(con, _cfg(), D)
    assert [r["key"] for r in res] == [k for k, _, _ in selfcheck.CHECKS]
    assert all(r["status"] in ("ok", "info", "warn", "bad") for r in res)
    assert con.execute("SELECT COUNT(*) FROM self_checks WHERE session_date = ?", [D]).fetchone()[0] \
        == len(selfcheck.CHECKS)
    # idempotent: a retried evening overwrites, never duplicates
    selfcheck.run(con, _cfg(), D)
    assert con.execute("SELECT COUNT(*) FROM self_checks").fetchone()[0] == len(selfcheck.CHECKS)
    assert "SELF-CHECK" in selfcheck.text(res)


def test_plumbing_is_bad_when_a_lane_produced_data_and_none_was_graded(con):
    _pick(con, D, graded=False)
    con.execute("INSERT INTO runner_candidates (session_date, ticker) VALUES (?, 'R1')", [D])
    r = _by_key(selfcheck.run(con, _cfg(), D))["plumbing"]
    assert r["status"] == "bad"
    assert "none graded" in r["headline"] and "none priced" in r["headline"]


def test_plumbing_is_ok_when_everything_graded(con):
    _pick(con, D)
    r = _by_key(selfcheck.run(con, _cfg(), D))["plumbing"]
    assert r["status"] == "ok" and r["value"]["graded"] == 1


def test_a_backlog_probe_that_never_fires_is_named_with_its_cause(con):
    from agent.util import is_market_day
    from datetime import timedelta
    d, n = D, 0
    while n < 5:
        if is_market_day(d):
            ledger.save_daily_plan(con, d, {"picks": [], "no_trade": False},
                                   {"candidates": [{"ticker": "X", "probe_for": "Q1"}]}, "chop")
            n += 1
        d -= timedelta(days=1)
    r = _by_key(selfcheck.run(con, _cfg(), D))["lanes"]
    assert r["status"] == "warn"
    assert "backlog_probe has not fired in 5 sessions" in r["headline"]
    assert "tagged 5 candidate(s)" in r["headline"]


def test_the_broker_cap_clipping_a_plan_is_flagged(con):
    for i in range(6):
        _pick(con, D, lane="coverage", ticker=f"T{i}")
    r = _by_key(selfcheck.run(con, _cfg(), D))["lanes"]
    assert r["status"] == "warn" and "broker cap 4 clipped" in r["headline"]


def test_a_morning_near_its_budget_and_a_missing_morning_are_flagged(con):
    con.execute(
        """INSERT INTO runs (loop, run_date, started_at, finished_at, status, stages)
           VALUES ('morning', ?, ?, ?, 'ok', ?)""",
        [D, datetime(2026, 10, 9, 7, 0), datetime(2026, 10, 9, 7, 5), json.dumps(
            {"timings_s": {"total": 284.0, "analyst": 150.0, "risk": 90.0}})])
    r = _by_key(selfcheck.run(con, _cfg(), D))["morning_budget"]
    assert r["status"] == "warn"
    assert "284s of 300s (analyst 150.0s)" in r["headline"]
    assert "no morning run recorded on" in r["headline"]


def test_a_hypothesis_tag_dated_before_its_idea_is_bad(con):
    con.execute("""INSERT INTO trader_hypotheses (hyp_id, created_date, opened_at, source,
                   statement, refutation, target_n, max_n, min_sessions, state, looks_done)
                   VALUES ('H-x', ?, ?, 'analyst', 's', 'r', 8, 96, 10, 'OPEN', 0)""",
                [D, D])
    _pick(con, date(2026, 10, 8), hyp="H-x", lane="hypothesis")
    r = _by_key(selfcheck.run(con, _cfg(), D))["hypotheses"]
    assert r["status"] == "bad" and "BEFORE the idea opened" in r["headline"]


def test_the_persona_check_runs_itself_after_three_sessions_with_the_lane_apart(con):
    for day in (date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)):
        ledger.save_daily_plan(con, day, {"picks": [], "no_trade": False, "persona_version": "1"},
                               {"candidates": []}, "chop")
        con.execute("UPDATE daily_plans SET persona_version = '1' WHERE plan_date = ?", [day])
        _pick(con, day, ticker="A")
        _pick(con, day, ticker="B", lane="hypothesis", hyp="H-1")
    r = _by_key(selfcheck.run(con, _cfg(), date(2026, 10, 7)))["persona"]
    assert r["status"] == "info"
    assert "persona v1: 3 session(s), picks/session 2.0 (1.0 excluding the hypothesis lane)" \
        in r["headline"]


def test_findings_reach_the_health_panel(con):
    _pick(con, D, graded=False)
    selfcheck.run(con, _cfg(), D)
    from agent.util import ET
    h = health.loop_health(con, _cfg(), now=datetime(2026, 10, 9, 20, 0, tzinfo=ET))
    stage = next(s for s in h["stages"] if s["key"] == "selfcheck")
    assert stage["status"] == "bad" and "Data collected today" in stage["detail"]


def test_a_morning_killed_at_the_budget_and_an_abandoned_arm_b_are_flagged(con):
    day = date(2026, 10, 5)
    con.execute(
        """INSERT INTO runs (loop, run_date, started_at, finished_at, status, stages)
           VALUES ('morning', ?, ?, ?, 'ok', ?)""",
        [day, datetime(2026, 10, 5, 7, 11), datetime(2026, 10, 5, 7, 15), json.dumps(
            {"timings_s": {"total": 240.0, "analyst": 120.0}, "arm_b": "abandoned_at_budget"})])
    con.execute("""INSERT INTO run_locks (trading_date, run_type, status, request_id)
                   VALUES (?, 'morning', 'in_progress', 'r1')""", [day])
    r = _by_key(selfcheck.run(con, _cfg(), date(2026, 10, 6)))["morning_budget"]
    assert r["status"] == "warn"
    assert "killed at the 300s budget after saving its plan on 2026-10-05" in r["headline"]
    assert "arm B abandoned at the budget on 2026-10-05" in r["headline"]


def test_the_notebook_check_tells_not_writing_from_refused(con):
    from datetime import timedelta
    from agent.util import is_market_day
    d, n = D, 0
    while n < 3:
        if is_market_day(d):
            ledger.save_daily_plan(con, d, {"picks": [], "no_trade": False},
                                   {"candidates": [], "new_hypotheses": []}, "chop")
            n += 1
        d -= timedelta(days=1)
    r = _by_key(selfcheck.run(con, _cfg(), D))["hypotheses"]
    assert r["status"] == "warn" and "not writing ideas down" in r["headline"]
    con.execute("UPDATE daily_plans SET analyst_json = ? WHERE plan_date = ?",
                [json.dumps({"candidates": [], "new_hypotheses": [{"statement": "x"}]}), D])
    con.execute("""INSERT INTO trader_hypotheses (hyp_id, created_date, opened_at, source,
                   statement, refutation, target_n, max_n, min_sessions, state, looks_done)
                   VALUES ('H-r', ?, ?, 'analyst', 'x', '', 8, 96, 10, 'REJECTED', 0)""", [D, D])
    r = _by_key(selfcheck.run(con, _cfg(), D))["hypotheses"]
    assert "the analyst wrote 1 and the register refused them (1 REJECTED)" in r["headline"]
