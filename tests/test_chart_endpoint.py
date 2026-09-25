"""Tests for GET /api/chart (specs/CHART_DRAWER_SPEC_v1.md §1.6).

Follows tests/test_live_route.py / tests/test_paper_trading.py: CRON_SECRET
bypasses login_required cleanly without simulating a real password session.
"""
from __future__ import annotations

from datetime import date

from agent import ledger
from agent.config import Config


def _client(monkeypatch, tmp_path):
    from api import index
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "chart.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(index, "CRON_SECRET", "test-secret")
    monkeypatch.setattr(index, "load_config", lambda: Config(raw={"data": {"or_minutes": 15}}, offline=True))
    return index.app.test_client(), {"Authorization": "Bearer test-secret"}


def test_chart_endpoint_401_when_logged_out(monkeypatch, tmp_path):
    from api import index
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "chart.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(index, "APP_PASSWORD", "pw")
    monkeypatch.setattr(index, "CRON_SECRET", "")
    resp = index.app.test_client().get("/api/chart?ticker=AAPL")
    assert resp.status_code == 401


def test_chart_endpoint_400_on_bad_ticker(monkeypatch, tmp_path):
    client, hdr = _client(monkeypatch, tmp_path)
    resp = client.get("/api/chart?ticker=not-a-ticker!!", headers=hdr)
    assert resp.status_code == 400


def test_chart_endpoint_400_on_bad_tf(monkeypatch, tmp_path):
    client, hdr = _client(monkeypatch, tmp_path)
    resp = client.get("/api/chart?ticker=AAPL&tf=7", headers=hdr)
    assert resp.status_code == 400


def test_chart_endpoint_200_with_warnings_when_nothing_exists(monkeypatch, tmp_path):
    client, hdr = _client(monkeypatch, tmp_path)
    resp = client.get(f"/api/chart?ticker=ZZZZ&date={date(2026,1,5).isoformat()}", headers=hdr)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ticker"] == "ZZZZ"
    assert body["contexts"] == []
    assert body["lane"] is None


def test_chart_endpoint_rejects_unknown_lane(monkeypatch, tmp_path):
    client, hdr = _client(monkeypatch, tmp_path)
    resp = client.get("/api/chart?ticker=AAPL&lane=bogus", headers=hdr)
    assert resp.status_code == 400
