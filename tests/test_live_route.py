from __future__ import annotations

from api import index
from agent.config import Config


def test_intraday_check_rejects_missing_bearer(monkeypatch):
    monkeypatch.setattr(index, "CRON_SECRET", "test-secret")
    response = index.app.test_client().post("/cron/intraday-check")
    assert response.status_code == 401


def test_intraday_check_is_noop_when_disabled(monkeypatch):
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


def test_intraday_check_runner_failure_cannot_break_the_monitor(monkeypatch):
    """The runner lane must never take down the live position monitor.

    A monitor that silently stops running leaves a real paper position
    unwatched, so the guard is a correctness requirement, not defensive habit.
    """
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