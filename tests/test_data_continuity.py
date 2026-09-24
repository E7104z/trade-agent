"""
DATA_CONTINUITY_SPEC_v1 — no outage may cost data that didn't need the outage's service.

Pinned here:

  Phase 1  an LLM failure no longer costs deterministic data: the morning
           population snapshot and the evening calibration/question rows are
           written BEFORE the model is called, and the runs row says which
           stage completed; retries replace session rows instead of doubling
  Phase 2  a permanent provider condition emails ONE alert per day
  Phase 3  a field null on every graded row is plumbing: the run is logged
           degraded and no lesson is drawn from a session never observed
  Phase 4  stage timings are recorded and a run near the budget is flagged
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from agent import coverage, failures, ledger
from agent.config import Config, load_config
from agent.loops import evening, morning

CREDIT = ("Anthropic API failed after 3 attempts: Error code: 400 - {'type': "
          "'invalid_request_error', 'message': 'Your credit balance is too low to "
          "access the Anthropic API.'}")
D = date(2026, 8, 24)


class _Boom:
    def __init__(self, cfg):
        pass

    def call(self, **kw):
        raise RuntimeError(CREDIT)


@pytest.fixture()
def live(tmp_path, monkeypatch):
    """A real (non-mock) run against offline synthetic market data, with the
    LLM refusing every call and every outward effect captured."""
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "c.duckdb")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    sent: list = []
    monkeypatch.setattr("agent.delivery.send_email", lambda *a, **k: sent.append(a[1]))
    monkeypatch.setattr("agent.loops.evening.send_email", lambda *a, **k: sent.append(a[1]))
    monkeypatch.setattr("agent.loops.morning.send_email", lambda *a, **k: sent.append(a[1]))
    monkeypatch.setattr("agent.loops.evening.render_dashboard", lambda *a, **k: None)
    monkeypatch.setattr("agent.loops.morning.render_dashboard", lambda *a, **k: None)
    monkeypatch.setattr(evening, "LLMClient", _Boom)
    monkeypatch.setattr(morning, "LLMClient", _Boom)
    cfg = load_config()
    cfg.mock = False
    cfg.offline = True
    return cfg, sent


def _seed_pick(con, d=D, ticker="TEST"):
    plan = {
        "no_trade": False, "market_stance": "test", "total_risk_r": 1.0,
        "picks": [{
            "ticker": ticker, "direction": "long", "setup_type": "gap_go",
            "catalyst": "x", "thesis_final": "x", "conviction": 4, "size_r": 1.0,
            "entry_plan": {"type": "open", "price": 100.0}, "entry_price": 100.0,
            "stop_price": 98.0, "target_price": 104.0, "target2_price": None,
            "time_stop": None, "invalidation": "x",
        }],
        "rejected": [], "notes_for_evening": "",
    }
    return ledger.save_daily_plan(con, d, plan, {"candidates": []}, "chop")[0]


# ------------------------------------------------------------------ Phase 1

def test_evening_llm_outage_keeps_deterministic_rows_and_says_so(live):
    cfg, sent = live
    con = ledger.connect()
    _seed_pick(con)
    con.close()

    with pytest.raises(RuntimeError):
        evening.run(cfg, run_date=D, force=True)

    con = ledger.connect()
    n_cal = con.execute("SELECT COUNT(*) FROM calibration WHERE session_date = ?", [D]).fetchone()[0]
    status, stages = con.execute(
        "SELECT status, stages FROM runs WHERE loop = 'evening' ORDER BY run_id DESC LIMIT 1").fetchone()
    con.close()

    assert n_cal == 1, "calibration is deterministic and must survive an LLM outage"
    assert status == "api_error"
    st = json.loads(stages)
    assert st["deterministic"] == "complete" and st["llm"] == "failed"
    assert "total" in st["timings_s"]


def test_evening_retry_replaces_session_rows_instead_of_doubling(live, monkeypatch):
    cfg, _sent = live
    con = ledger.connect()
    _seed_pick(con)
    con.close()
    for _ in range(2):
        with pytest.raises(RuntimeError):
            evening.run(cfg, run_date=D, force=True)
    con = ledger.connect()
    cal = con.execute("SELECT COUNT(*) FROM calibration WHERE session_date = ?", [D]).fetchone()[0]
    q = con.execute("SELECT question_id, COUNT(*) FROM open_question_progress "
                    "WHERE session_date = ? GROUP BY 1 HAVING COUNT(*) > 1", [D]).fetchall()
    con.close()
    assert cal == 1
    assert q == [], "a retried evening must not count the same observation twice"


def test_rule_confirmations_are_idempotent_per_session(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "r.duckdb")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    con = ledger.connect()
    pid = _seed_pick(con)
    picks = ledger.picks_for_date(con, D)
    outcomes = [{"pick_id": pid, "triggered": True}]
    review = {"pick_reviews": [{"pick_id": pid, "ticker": "TEST", "grade": "A"}]}
    evening._llm_events(con, D, picks, outcomes, review)
    evening._llm_events(con, D, picks, outcomes, review)
    n = con.execute("SELECT COUNT(*) FROM rule_confirmations").fetchone()[0]
    con.close()
    assert n == 1


def test_morning_llm_outage_keeps_the_population_snapshot(live):
    cfg, _sent = live
    with pytest.raises(RuntimeError):
        morning.run(cfg, run_date=D, force=True)
    con = ledger.connect()
    n = con.execute("SELECT COUNT(*) FROM population_universe WHERE plan_date = ?", [D]).fetchone()[0]
    status = con.execute("SELECT status FROM runs WHERE loop = 'morning'").fetchone()[0]
    con.close()
    assert n > 0, "the gap-scan population needs no LLM and must be snapshotted first"
    assert status == "api_error"


# ------------------------------------------------------------------ Phase 2

def test_one_alert_per_condition_per_day(live):
    cfg, sent = live
    con = ledger.connect()
    _seed_pick(con)
    con.close()
    for _ in range(3):
        with pytest.raises(RuntimeError):
            evening.run(cfg, run_date=D, force=True)
    alerts = [s for s in sent if "LLM provider refusing requests" in s]
    assert len(alerts) == 1

    con = ledger.connect()
    rows = con.execute("SELECT kind, loop FROM alerts").fetchall()
    con.close()
    assert rows == [("llm_credit_exhausted", "evening")]


def test_a_code_defect_sends_no_provider_alert(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "a.duckdb")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    sent = []
    monkeypatch.setattr("agent.delivery.send_email", lambda *a, **k: sent.append(1))
    con = ledger.connect()
    assert failures.alert_provider_outage(con, Config(raw={}), "evening", D,
                                          TypeError("'NoneType' object is not subscriptable")) is False
    assert failures.alert_provider_outage(con, Config(raw={}), "evening", D,
                                          RuntimeError("rate_limit_error")) is False
    con.close()
    assert sent == []


# ------------------------------------------------------------------ Phase 3

def test_plumbing_rule_needs_every_row_and_enough_rows():
    rows = [{"exit_reason": "no_data", "day_open": None} for _ in range(coverage.PLUMBING_MIN_ROWS)]
    cov = coverage.outcome_coverage(rows, "runner_candidates", ("minute_bars", "day_open"))
    assert {c["field"] for c in coverage.plumbing(cov)} == {"minute_bars", "day_open"}

    one_ok = rows[:-1] + [{"exit_reason": "eod", "day_open": 10.0}]
    assert coverage.plumbing(coverage.outcome_coverage(one_ok, "x", ("minute_bars",))) == []

    too_few = rows[: coverage.PLUMBING_MIN_ROWS - 1]
    assert coverage.plumbing(coverage.outcome_coverage(too_few, "x", ("minute_bars",))) == []


def test_all_bars_missing_marks_the_session_degraded_and_skips_lessons(live, monkeypatch):
    cfg, _sent = live
    con = ledger.connect()
    # save_daily_plan replaces a date's picks, so seed them all in one plan.
    plan = {"no_trade": False, "market_stance": "t", "total_risk_r": 1.0, "rejected": [],
            "notes_for_evening": "", "picks": [{
                "ticker": f"T{i}", "direction": "long", "setup_type": "gap_go",
                "catalyst": "x", "thesis_final": "x", "conviction": 3, "size_r": 0.25,
                "entry_plan": {"type": "open", "price": 100.0}, "entry_price": 100.0,
                "stop_price": 98.0, "target_price": 104.0, "target2_price": None,
                "time_stop": None, "invalidation": "x"} for i in range(coverage.PLUMBING_MIN_ROWS)]}
    ledger.save_daily_plan(con, D, plan, {"candidates": []}, "chop")
    con.close()

    from agent.providers import MarketData
    monkeypatch.setattr(MarketData, "minute_bars", lambda self, t, d, *a, **k: [])

    class _Grader:
        def __init__(self, cfg):
            pass

        def call(self, **kw):
            class R:
                parsed = {"plan_grade": "B", "pick_reviews": [],
                          "lessons": [{"lesson": "gaps faded all day", "confidence": "low"}],
                          "lesson_updates": []}
                usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0, "cost_usd": 0.0}
                text = ""
            return R()

    monkeypatch.setattr(evening, "LLMClient", _Grader)
    evening.run(cfg, run_date=D, force=True)

    con = ledger.connect()
    status, stages = con.execute(
        "SELECT status, stages FROM runs WHERE loop='evening' ORDER BY run_id DESC LIMIT 1").fetchone()
    n_lessons = con.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
    cov = ledger.coverage_for(con, D)
    con.close()
    assert status == "degraded"
    assert "minute_bars" in json.loads(stages)["plumbing"]
    assert n_lessons == 0, "no lesson may be drawn from a session whose bars never arrived"
    assert any(c["field"] == "minute_bars" and c["n_null"] == c["n_total"] for c in cov)


# ------------------------------------------------------------------ health

def test_health_surfaces_alerts_plumbing_and_budget(tmp_path, monkeypatch):
    from agent import health
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "h.duckdb")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    con = ledger.connect()
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone(timedelta(hours=-4)))
    ledger.record_alert(con, now.date(), "llm_credit_exhausted", "evening", "x")
    ledger.save_data_coverage(con, now.date(), "evening", [
        {"lane": "runner_candidates", "field": "minute_bars", "n_total": 40, "n_null": 40}])
    ledger.log_run(con, "evening", now.date(), now.replace(tzinfo=None), "ok",
                   stages={"timings_s": {"total": 250.0}, "budget_warning": True})
    h = health.loop_health(con, Config(raw={}), now=now)
    con.close()
    text = " ".join(h["warnings"])
    assert "credit exhausted" in text
    assert "Data plumbing" in text
    assert "300s function budget" in text


# ------------------------------------------------- morning budget follow-up

def test_mark_picked_only_flags_the_picks(tmp_path, monkeypatch):
    from agent import population
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "m.duckdb")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    con = ledger.connect()
    gappers = [{"symbol": s, "last": 10.0, "prev_close": 9.0, "gap_pct": 11.0} for s in ("AAA", "BBB")]
    assert population.snapshot_universe(con, D, gappers) == 2
    assert population.mark_picked(con, D, ["bbb"]) == 1
    rows = dict(con.execute("SELECT symbol, was_picked FROM population_universe").fetchall())
    con.close()
    assert rows == {"AAA": False, "BBB": True}


def test_morning_stage_timings_reach_health(tmp_path, monkeypatch):
    from agent import health
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "t.duckdb")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    con = ledger.connect()
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone(timedelta(hours=-4)))
    ledger.log_run(con, "morning", now.date(), now.replace(tzinfo=None), "ok")
    ledger.record_run_stages(con, "morning", now.date(),
                             {"timings_s": {"bundle": 70.0, "analyst": 150.0, "total": 260.0}})
    h = health.loop_health(con, Config(raw={}), now=now)
    con.close()
    warn = [w for w in h["warnings"] if "morning run took 260.0s" in w]
    assert warn and "slowest stage: analyst 150.0s" in warn[0]
