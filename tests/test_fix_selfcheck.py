"""
The fixes verify themselves (docs/FIX_VERIFICATION.md): instead of someone remembering to
run scripts/verify_fixes.py after each of five sessions, the evening SELF-CHECK judges every
finished session on the fixed code, keeps the 5-session streak, flags the account if it is
not flat right after today's close, and retires to one line once verified. The entry-
semantics check says "decision due" when a mismatch tag has the evidence for Jason's call.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

import pytest

from agent import fix_verification as fv
from agent import ledger, selfcheck
from agent.config import Config

ET = ZoneInfo("America/New_York")
WEEK = [date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22),
        date(2026, 10, 23)]                                   # Monday to Friday
NEXT_MON, NEXT_TUE = date(2026, 10, 26), date(2026, 10, 27)
LIVE = Config(raw={"live_paper": {"enabled": True}})


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "fixcheck.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


class _Account:
    """The paper account as the self-check reads it (GET-only in production)."""
    positions_now: list = []
    fills_by_day: dict = {}

    def __init__(self, cfg):
        self.cfg = cfg

    def positions(self):
        return list(type(self).positions_now)

    def fills(self, d):
        return list(type(self).fills_by_day.get(d, []))


@pytest.fixture()
def account(monkeypatch):
    _Account.positions_now, _Account.fills_by_day = [], {}
    monkeypatch.setattr(fv, "Broker", _Account)
    return _Account


def _evening_at(monkeypatch, d, hh=17, mm=10):
    monkeypatch.setattr("agent.util.now_et", lambda: datetime.combine(d, dtime(hh, mm), tzinfo=ET))


def _finished(con, d, grader="11"):
    """A session whose evening ran on the fixed code (it records the grader prompt version)."""
    ledger.save_daily_plan(con, d, {"no_trade": True, "picks": []}, {"candidates": []}, "chop")
    ledger.log_run(con, "evening", d, datetime.combine(d, dtime(16, 30)), "ok",
                   stages={"grader_prompt_version": grader,
                           "audit": {"findings": 0, "by_check": {}, "items": []}})


def _check(con, cfg, d):
    return {r["key"]: r for r in selfcheck.run(con, cfg, d)}["fix_verification"]


# ------------------------------------------------------------------ the streak

def test_before_any_finished_session_it_says_when_judging_starts(con, account, monkeypatch):
    _evening_at(monkeypatch, WEEK[0])
    r = _check(con, LIVE, WEEK[0])
    assert r["status"] == "ok"
    assert r["headline"].startswith("no finished session on the fixed code yet")
    assert r["headline"].endswith("| today: flat after the close")


def test_it_counts_finished_sessions_in_a_row(con, account, monkeypatch):
    for d in WEEK[:3]:
        _finished(con, d)
    _evening_at(monkeypatch, WEEK[3])
    r = _check(con, LIVE, WEEK[3])
    assert r["status"] == "ok"
    assert r["headline"].startswith("3/5 sessions in a row pass (2026-10-19, 2026-10-20, 2026-10-21)")
    assert r["value"]["streak"] == 3 and len(r["value"]["sessions"]) == 3


def test_five_in_a_row_is_verified_and_the_next_evening_it_retires(con, account, monkeypatch):
    for d in WEEK:
        _finished(con, d)
    _evening_at(monkeypatch, NEXT_MON)
    r = _check(con, LIVE, NEXT_MON)
    assert r["status"] == "ok"
    assert r["headline"].startswith("VERIFIED: 5 straight sessions passed (2026-10-19 to 2026-10-23)")
    assert r["value"]["verified_through"] == "2026-10-23"

    def boom(*a, **k):
        raise AssertionError("a retired check must not re-judge sessions")
    monkeypatch.setattr(fv, "run_checks", boom)
    _evening_at(monkeypatch, NEXT_TUE)
    r = _check(con, LIVE, NEXT_TUE)
    assert r["status"] == "info" and "has retired" in r["headline"]
    # and it stays retired
    _evening_at(monkeypatch, date(2026, 10, 28))
    assert "has retired" in _check(con, LIVE, date(2026, 10, 28))["headline"]


def test_the_newest_session_failing_is_bad_and_says_why(con, account, monkeypatch):
    _finished(con, WEEK[0])
    _finished(con, WEEK[1])
    _finished(con, WEEK[2], grader="10")                  # ran on a stale grader prompt
    _evening_at(monkeypatch, WEEK[3])
    r = _check(con, LIVE, WEEK[3])
    assert r["status"] == "bad"
    assert r["headline"].startswith("0/5 sessions in a row pass; 2026-10-21 did not: D6 FAIL: "
                                    "graded with prompt v10")
    assert len(r["value"]["sessions"]) == 1                # older sessions do not matter any more


def test_an_older_failure_restarts_the_count_without_turning_it_red(con, account, monkeypatch):
    for d in WEEK[:3]:
        _finished(con, d)
    account.fills_by_day = {WEEK[0]: [{"symbol": "IOVA", "side": "buy", "qty": "250"}]}
    _evening_at(monkeypatch, WEEK[3])
    r = _check(con, LIVE, WEEK[3])
    assert r["status"] == "ok"
    assert r["headline"].startswith("2/5 sessions in a row pass (2026-10-20, 2026-10-21); 2026-10-19 did "
                                    "not: D1 FAIL: IOVA: the day's fills net +250 sh")


def test_a_position_open_right_after_todays_close_is_bad_today(con, account, monkeypatch):
    _finished(con, WEEK[0])
    account.positions_now = [{"symbol": "IOVA", "qty": "250", "side": "long", "avg_entry_price": "14.36"}]
    _evening_at(monkeypatch, WEEK[1])
    r = _check(con, LIVE, WEEK[1])
    assert r["status"] == "bad"
    assert "| TODAY: STILL OPEN after the close: IOVA 250 sh long @ 14.36" in r["headline"]
    assert r["value"]["today_d1"] == "FAIL"


def test_an_evening_that_runs_before_the_close_does_not_judge_today(con, account, monkeypatch):
    _evening_at(monkeypatch, WEEK[1], 15, 30)
    r = _check(con, LIVE, WEEK[1])
    assert "| today: pending" in r["headline"]


def test_a_backfilled_evening_for_an_old_date_never_reads_today_s_account(con, account, monkeypatch):
    account.positions_now = [{"symbol": "IOVA", "qty": "250", "side": "long", "avg_entry_price": "14.36"}]
    _evening_at(monkeypatch, NEXT_MON)
    r = _check(con, LIVE, WEEK[0])                          # an evening re-run for an earlier date
    assert "| today" not in r["headline"].lower()
    assert r["value"]["today_d1"] is None


def test_offline_and_mock_runs_never_read_the_account(con, monkeypatch):
    class _Refuse:
        def __init__(self, cfg):
            raise AssertionError("an offline run built a broker")
    monkeypatch.setattr(fv, "Broker", _Refuse)
    _finished(con, WEEK[0])
    _evening_at(monkeypatch, WEEK[1])
    for flag in ("offline", "mock"):
        cfg = Config(raw={"live_paper": {"enabled": True}})
        setattr(cfg, flag, True)
        r = _check(con, cfg, WEEK[1])
        assert "could not run" not in r["headline"] and "today" not in r["headline"]
        assert r["headline"].startswith("0/5 sessions in a row pass; 2026-10-19 did not: D1 UNVERIFIED")


def test_without_live_trading_there_is_no_account_to_carry_anything(con, monkeypatch):
    class _Refuse:
        def __init__(self, cfg):
            raise AssertionError("no live trading: nothing to read")
    monkeypatch.setattr(fv, "Broker", _Refuse)
    _finished(con, WEEK[0])
    _evening_at(monkeypatch, WEEK[1])
    r = _check(con, Config(raw={"live_paper": {"enabled": False}}), WEEK[1])
    assert r["headline"].startswith("1/5 sessions in a row pass (2026-10-19)")
    assert r["headline"].endswith("| today: flat after the close")


def test_sessions_before_the_fixed_code_are_not_counted(con, account, monkeypatch):
    ledger.save_daily_plan(con, WEEK[0], {"no_trade": True, "picks": []}, {"candidates": []}, "chop")
    ledger.log_run(con, "evening", WEEK[0], datetime.combine(WEEK[0], dtime(16, 30)), "ok",
                   stages={"llm": "complete"})                  # old code: no grader version
    _finished(con, WEEK[1])
    _evening_at(monkeypatch, WEEK[2])
    r = _check(con, LIVE, WEEK[2])
    assert r["headline"] == "1/5 sessions in a row pass (2026-10-20) | today: flat after the close"
    assert [s["date"] for s in r["value"]["sessions"]] == ["2026-10-20"]


def test_the_evening_email_and_the_health_panel_carry_it(con, account, monkeypatch):
    _finished(con, WEEK[0], grader="10")
    _evening_at(monkeypatch, WEEK[1])
    res = selfcheck.run(con, LIVE, WEEK[1])
    assert "[BAD ] Fix verification (5 sessions): 0/5 sessions in a row pass" in selfcheck.text(res)
    assert any(f.startswith("Fix verification (5 sessions):") for f in selfcheck.latest(con)["flagged"])


# --------------------------------------------------------------- entry semantics

def _tagged(con, d, tag, n, triggered=True, r=-0.5, exit_reason="time_stop"):
    for i in range(n):
        pid = con.execute(
            """INSERT INTO picks (plan_date, ticker, direction, setup_type, size_r, entry_type, entry_price,
                                  stop_price, target_price, conviction, entry_semantics)
               VALUES (?, ?, 'long', 'catalyst_momentum', 0.25, 'limit', 10, 9, 12, 3, ?) RETURNING pick_id""",
            [d, f"{tag[:3].upper()}{i}", tag]).fetchone()[0]
        con.execute("INSERT INTO outcomes (pick_id, eval_date, triggered, r_planned, exit_reason) "
                    "VALUES (?, ?, ?, ?, ?)", [pid, d, triggered, r if triggered else None,
                                               exit_reason if triggered else "no_trigger"])


def _sem(con, cfg, d=NEXT_MON):
    return {r["key"]: r for r in selfcheck.run(con, cfg, d)}["entry_semantics"]


def test_entry_semantics_before_any_tag(con):
    r = _sem(con, Config(raw={}))
    assert r["status"] == "info" and r["headline"].startswith("no tagged pick graded yet")


def test_entry_semantics_collects_until_a_tag_has_eight(con):
    _tagged(con, WEEK[0], "ok", 5, r=0.3)
    _tagged(con, WEEK[1], "breakdown_short_with_limit", 3)
    r = _sem(con, Config(raw={}))
    assert r["status"] == "info"
    assert r["headline"] == "collecting (tag only): breakdown_short_with_limit 3/8; ok n 5"


def test_entry_semantics_says_decision_due_with_the_numbers(con):
    _tagged(con, WEEK[0], "ok", 6, r=0.3)
    _tagged(con, WEEK[0], "ok", 4, triggered=False)
    _tagged(con, WEEK[1], "strength_buy_limit_below_ref", 4, r=-0.5)
    _tagged(con, WEEK[2], "strength_buy_limit_below_ref", 4, triggered=False)
    _tagged(con, WEEK[2], "strength_buy_limit_below_ref", 2, exit_reason="invalid_fill")   # not trades
    r = _sem(con, Config(raw={}))
    assert r["status"] == "warn"
    assert r["headline"].startswith(
        "DECISION DUE (enforcement is off): strength_buy_limit_below_ref: n 8, filled 4/8, mean -0.50R "
        "-- against ok: n 10, filled 6/10, mean +0.30R")
    assert r["value"]["due"] == ["strength_buy_limit_below_ref"]


def test_entry_semantics_with_enforcement_on_just_reports(con):
    _tagged(con, WEEK[0], "strength_buy_limit_below_ref", 9)
    r = _sem(con, Config(raw={"validator": {"enforce_entry_semantics": True}}))
    assert r["status"] == "info" and r["headline"].startswith("enforcement is ON")
