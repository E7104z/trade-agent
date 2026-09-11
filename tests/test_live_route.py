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
    assert response.get_json()["reason"] == "live paper disabled"