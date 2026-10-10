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
    # established contract), and are additionally nested under "monitor".
    assert body["reason"] == "live paper disabled"
    assert body["monitor"]["reason"] == "live paper disabled"
    # The runner lane was removed, so it contributes nothing.
    assert "runner" not in body


def test_the_runner_loop_and_crons_are_gone(monkeypatch):
    from api import index
    assert "runner" not in index._VALID_LOOPS
    assert not {"runner", "runner-late"} & set(index._CRON_WINDOWS)
    assert not {"runner", "runner-late"} & set(index._CRON_LOOP)
    client = index.app.test_client()
    monkeypatch.setattr(index, "CRON_SECRET", "test-secret")
    resp = client.get("/api/runner/candidates", headers={"Authorization": "Bearer test-secret"})
    assert resp.status_code == 404
