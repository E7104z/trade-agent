from __future__ import annotations

from api import index
from agent.config import Config


def test_intraday_check_rejects_missing_bearer(monkeypatch):
    monkeypatch.setattr(index, "CRON_SECRET", "test-secret")
    response = index.app.test_client().post("/cron/intraday-check")
    assert response.status_code == 401


def _tmp_ledger(monkeypatch, tmp_path):
    """The endpoint now records a heartbeat: never let it touch the real ledger."""
    from agent import ledger
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "route.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)


def test_intraday_check_is_noop_when_disabled(monkeypatch, tmp_path):
    _tmp_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(index, "CRON_SECRET", "test-secret")
    monkeypatch.setattr(index, "load_config", lambda: Config(raw={
        "live_paper": {"enabled": False},
    }))
    response = index.app.test_client().post(
        "/cron/intraday-check",
        headers={"Authorization": "Bearer test-secret"},
    )
    assert response.status_code == 200
    body = response.get_json()
    # The monitor's own keys stay at the top level (this response is an
    # established contract), and are additionally nested under "monitor" per
    # RUNNER_PORT_SPEC §1.4.
    assert body["reason"] == "live paper disabled"
    assert body["monitor"]["reason"] == "live paper disabled"
    # Runner lane is off by default, so it contributes nothing.
    assert "runner" not in body


def test_intraday_check_runner_failure_cannot_break_the_monitor(monkeypatch, tmp_path):
    """The runner lane must never take down the live position monitor.

    A monitor that silently stops running leaves a real paper position
    unwatched, so the guard is a correctness requirement, not defensive habit.
    """
    _tmp_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(index, "CRON_SECRET", "test-secret")
    monkeypatch.setattr(index, "load_config", lambda: Config(raw={
        "live_paper": {"enabled": False},
        "runner": {"enabled": True},
    }))

    import agent.loops.runner as runner_loop

    def _boom(*a, **kw):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(runner_loop, "tick", _boom)

    response = index.app.test_client().post(
        "/cron/intraday-check",
        headers={"Authorization": "Bearer test-secret"},
    )
    assert response.status_code == 200
    body = response.get_json()
    # Monitor result is intact and unchanged...
    assert body["reason"] == "live paper disabled"
    assert body["monitor"]["reason"] == "live paper disabled"
    # ...and the runner failure is recorded rather than raised.
    assert "scan exploded" in body["runner"]["error"]

def test_api_status_includes_runner_movers(monkeypatch, tmp_path):
    """/api/status must carry the same top-movers diagnostic the CLI and
    local dashboard show, so the hosted card has feature parity once a
    Postgres DATABASE_URL is actually configured."""
    import os
    import json as _json
    from datetime import date, datetime

    from api import index
    from agent import ledger

    db_path = tmp_path / "test.duckdb"
    monkeypatch.setattr(ledger, "DB_PATH", str(db_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(index, "CRON_SECRET", "test-secret")
    monkeypatch.setattr(index, "load_config", lambda: __import__(
        "agent.config", fromlist=["load_config"]).load_config())

    session = date(2026, 9, 18)
    con = ledger.connect()
    for i, (ticker, r) in enumerate([("WINNER", 5.0), ("PICKED_LOSER", -1.0)]):
        con.execute(
            """INSERT INTO runner_candidates
               (session_date, ticker, first_pass_et, detection_price,
                stop_price, target_price, stop_dist, scan_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
            [session, ticker, datetime(2026, 9, 18, 10, 30),
             10.0 + i, 9.5 + i, 11.0 + i, 0.5])
    ledger.mark_runner_selected(con, session, "PICKED_LOSER", 1, None)
    for ticker, r in [("WINNER", 5.0), ("PICKED_LOSER", -1.0)]:
        ledger.save_runner_candidate_outcome(con, {
            "session_date": session, "ticker": ticker, "eval_date": session,
            "triggered": True, "r_multiple": r, "exit_reason": "eod",
            "sim_version": 3,
        })
    ledger.upsert_runner_session(con, {
        "session_date": session, "universe_size": 100, "candidates_found": 2,
        "provider_degraded": False,
    })
    con.close()

    # today_et() drives which date api_status looks up -- point it at the
    # session date we just seeded.
    monkeypatch.setattr(index, "today_et", lambda: session)

    response = index.app.test_client().get(
        "/api/status", headers={"Authorization": "Bearer test-secret"})
    assert response.status_code == 200
    body = response.get_json()
    movers = body["runner"]["movers"]
    assert [m["ticker"] for m in movers] == ["WINNER", "PICKED_LOSER"]
    assert movers[0]["selected"] is False
    assert movers[1]["selected"] is True
    assert movers[1]["rank"] == 1


def test_runner_is_a_runnable_loop_and_cron_target(monkeypatch):
    """The runner must be triggerable SERVER-SIDE, both by the dashboard
    button and by Vercel Cron.

    This is the whole fix for "why isn't this on iwilltrade.com": running the
    scan locally writes to a local DuckDB the hosted dashboard cannot read.
    Running it server-side writes to the same Postgres the dashboard reads.
    """
    from api import index

    assert "runner" in index._VALID_LOOPS
    assert "runner" in index._CRON_WINDOWS

    monkeypatch.setattr(index, "CRON_SECRET", "test-secret")
    # Outside the ET window the cron must refuse rather than run at the wrong
    # time -- a DST drift should skip, not silently scan a closed market.
    lo, hi = index._CRON_WINDOWS["runner"]
    assert lo <= 10 <= hi, "10:30 ET decision tick must fall inside the window"


def test_runner_loop_execution_reports_skip_reason(monkeypatch):
    """_execute_loop('runner') must surface tick()'s skip reason rather than
    silently reporting success when the lane declined to run."""
    from api import index
    from agent.config import Config
    import agent.loops.runner as runner_loop

    monkeypatch.setattr(runner_loop, "tick",
                        lambda cfg, **kw: {"skipped": "runner.enabled is false"})
    cfg = Config(raw={"runner": {"enabled": False}})
    result = index._execute_loop("runner", cfg, force=False)
    assert result["error"] is None
    assert "runner.enabled is false" in result["log"]
