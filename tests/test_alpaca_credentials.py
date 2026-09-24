"""
Alpaca credential failures are diagnosed as credentials, not as a quiet market.

2026-09-24: every Alpaca route answered 401 ("unauthorized."). The morning
plan recorded "3 feeds down" and stood aside -- correctly marked degraded, but
the only recorded cause was "premarket gap scan returned no rows", nothing
alerted, and the dashboard could not say what the deployment was sending.

Pinned here:
  - a pasted credential is cleaned (whitespace, quotes, a `NAME = value` line)
    and what was cleaned is reported, never the value
  - the fingerprint describes the credentials without revealing them
  - a 401 on the data API raises AlpacaAuthError (still an HTTPError), is
    recorded once, is not retried, and does not trigger an IEX "downgrade"
  - /v2/assets refused by both hosts records the auth failure; one host
    refusing (the normal paper/live mismatch) does not
  - the morning's data-health verdict names the credentials as the cause
  - one alert per day, whichever loop hits it first
  - the runner's degraded session records auth_failed and health says so
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from agent import config as config_mod
from agent import ledger, providers
from agent.config import (Config, alpaca_credential_fingerprint, clean_credential,
                          describe_alpaca_fingerprint)

KEY20 = "PKABCDEFGHIJ12345678"
SECRET40 = "a" * 20 + "B" * 20


# --------------------------------------------------------------- cleaning

@pytest.mark.parametrize("raw, value, noted", [
    (KEY20, KEY20, False),
    (f"  {KEY20}\n", KEY20, True),
    (f'"{KEY20}"', KEY20, True),
    (f"ALPACA_API_KEY   = {KEY20}", KEY20, True),
    (f"= {KEY20}", KEY20, True),
    (f"'{KEY20}' ", KEY20, True),
])
def test_clean_credential(raw, value, noted):
    got, notes = clean_credential(raw)
    assert got == value
    assert bool(notes) is noted
    assert all(KEY20 not in n for n in notes), "notes must never carry the value"


def test_internal_whitespace_is_reported_not_removed():
    got, notes = clean_credential("PKAB CDEF")
    assert got == "PKAB CDEF" and any("internal whitespace" in n for n in notes)


def test_load_config_records_source_and_cleaning(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", f" {KEY20} ")
    monkeypatch.setenv("ALPACA_KEY_ID", "PKSTALEVALUE00000000")
    monkeypatch.setenv("ALPACA_SECRET_KEY", f'"{SECRET40}"')
    cfg = config_mod.load_config()
    assert cfg.alpaca_key_id == KEY20 and cfg.alpaca_secret_key == SECRET40
    assert cfg.alpaca_key_source == "env:ALPACA_API_KEY"
    assert "alpaca_key_id" in cfg.credential_notes and "alpaca_secret_key" in cfg.credential_notes


# --------------------------------------------------------------- fingerprint

def test_fingerprint_describes_without_revealing():
    cfg = Config(alpaca_key_id=KEY20, alpaca_secret_key=SECRET40,
                 alpaca_key_source="env:ALPACA_API_KEY")
    fp = alpaca_credential_fingerprint(cfg)
    assert fp["key_type"] == "paper" and fp["key_length"] == 20 and fp["usual_format"]
    line = describe_alpaca_fingerprint(fp)
    assert KEY20 not in line and SECRET40 not in line and KEY20[2:6] not in line
    assert "20 chars, paper" in line


def test_fingerprint_flags_an_unusual_shape():
    cfg = Config(alpaca_key_id="PKIXN64BlBJHADUO5NROCPQOTN", alpaca_secret_key="x" * 44,
                 alpaca_key_source="env:ALPACA_API_KEY")
    fp = alpaca_credential_fingerprint(cfg)
    assert fp["key_length"] == 26 and not fp["key_upper_alnum"] and not fp["usual_format"]
    line = describe_alpaca_fingerprint(fp)
    assert "NOT upper-case alphanumeric" in line and "does not match the usual" in line
    assert "PKIXN" not in line


def test_fingerprint_accepts_the_current_26_44_shape():
    # Alpaca issued 26-char key ids with 44-char secrets from 2026-09.
    cfg = Config(alpaca_key_id="PK" + "A1" * 12, alpaca_secret_key="aB3" * 14 + "xy",
                 alpaca_key_source="env:ALPACA_API_KEY")
    fp = alpaca_credential_fingerprint(cfg)
    assert fp["key_length"] == 26 and fp["secret_length"] == 44 and fp["usual_format"]
    assert "does not match" not in describe_alpaca_fingerprint(fp)


def test_fingerprint_names_what_is_missing():
    assert "secret not set" in describe_alpaca_fingerprint(
        alpaca_credential_fingerprint(Config(alpaca_key_id=KEY20)))


# --------------------------------------------------------------- data client

class _Resp:
    def __init__(self, status, text="", payload=None):
        self.status_code, self.text, self._p = status, text, payload or {}

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)


def _alpaca(monkeypatch, status):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _Resp(status, '{"message": "unauthorized."}')

    monkeypatch.setattr(providers.requests, "get", fake_get)
    cfg = Config(raw={"data": {"alpaca_feed": "sip"}}, alpaca_key_id=KEY20,
                 alpaca_secret_key=SECRET40)
    return providers.Alpaca(cfg), calls


def test_a_401_is_an_auth_error_recorded_once_and_not_retried(monkeypatch):
    a, calls = _alpaca(monkeypatch, 401)
    with pytest.raises(providers.AlpacaAuthError) as ei:
        a.movers(10)
    assert isinstance(ei.value, requests.exceptions.HTTPError)
    assert len(calls) == 1, "a rejected key pair is never retried"
    assert a.auth_error["status"] == 401 and "movers" in a.auth_error["path"]
    with pytest.raises(providers.AlpacaAuthError):
        a.snapshots(["SPY"])
    assert "movers" in a.auth_error["path"], "the FIRST refusal is kept"


def test_assets_refused_by_both_hosts_is_an_auth_failure(monkeypatch):
    a, _ = _alpaca(monkeypatch, 401)
    assert a.tradable_assets() == []
    assert a.auth_error and "paper and live" in a.auth_error["path"]


def test_assets_refused_by_one_host_is_normal(monkeypatch):
    responses = iter([_Resp(401), _Resp(200, payload=[{"symbol": "ABC", "tradable": True}])])
    monkeypatch.setattr(providers.requests, "get", lambda url, **kw: next(responses))
    cfg = Config(raw={"data": {}}, alpaca_key_id=KEY20, alpaca_secret_key=SECRET40)
    a = providers.Alpaca(cfg)
    assert a.tradable_assets() == ["ABC"]
    assert a.auth_error is None


def test_no_iex_downgrade_on_rejected_credentials(monkeypatch):
    _a, _ = _alpaca(monkeypatch, 401)
    cfg = Config(raw={"data": {"alpaca_feed": "sip"}}, alpaca_key_id=KEY20,
                 alpaca_secret_key=SECRET40)
    md = providers.MarketData(cfg)
    md._ensure_feed()
    assert md.feed_ok is False and md.feed_downgraded is False
    assert md.alpaca.feed == "sip"
    assert md.alpaca_auth_error["status"] == 401


# --------------------------------------------------------------- morning verdict

def test_data_health_names_the_credentials_as_the_cause():
    from agent.loops.morning import _validate_plan
    plan, _ = _validate_plan({"no_trade": True, "picks": []}, Config(raw={}), session_health={
        "bundle_gappers": 0,
        "provider_auth_error": {"status": 401, "path": "/v2/stocks/snapshots"}})
    dh = plan["data_health"]
    assert dh["degraded"] and dh["provider_auth_failed"]
    assert dh["degraded_reasons"][0].startswith("Alpaca rejected the API credentials (HTTP 401")
    assert "not a market read" in dh["degraded_reasons"][0]


# --------------------------------------------------------------- alerts + health

@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "a.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


def test_one_alpaca_alert_per_day_across_loops(con, monkeypatch):
    from agent import failures
    sent = []
    monkeypatch.setattr("agent.delivery.send_email", lambda cfg, subj, body: sent.append((subj, body)))
    cfg = Config(alpaca_key_id=KEY20, alpaca_secret_key=SECRET40, alpaca_key_source="env:ALPACA_API_KEY")
    assert failures.alert_alpaca_auth(con, cfg, "morning", "/v2/stocks/snapshots") is True
    assert failures.alert_alpaca_auth(con, cfg, "runner", "/v2/assets") is False
    assert failures.alert_alpaca_auth(con, cfg, "intraday", "paper broker") is False
    assert len(sent) == 1
    subj, body = sent[0]
    assert "401" in subj and "20 chars, paper" in body
    assert KEY20 not in body and SECRET40 not in body


def test_health_explains_a_credentials_outage(con):
    from agent import health
    # After 13:00 ET, when today's runner session is the one health expects.
    now = datetime(2026, 9, 24, 14, 0, tzinfo=timezone(timedelta(hours=-4)))
    ledger.record_alert(con, now.date(), "alpaca_auth_failed", "runner",
                        "runner: /v2/assets -- key from env:ALPACA_API_KEY: 26 chars, paper")
    ledger.upsert_runner_session(con, {
        "session_date": now.date(), "universe_size": 0, "candidates_found": 0,
        "provider_degraded": True, "scan_version": 2,
        "gates_unavailable": json.dumps({"auth_failed": True, "auth_path": "/v2/assets"})})
    h = health.loop_health(con, Config(raw={"runner": {"enabled": True}}), now=now)
    runner = next(s for s in h["stages"] if s["key"] == "runner")
    assert runner["status"] == "bad" and "rejected the credentials" in runner["headline"]
    assert any("Alpaca rejected the API credentials" in w and "26 chars, paper" in w
               for w in h["warnings"])


@pytest.mark.parametrize("secret", ["abcDEF123==", "ABCDEF==", "sk-ant-api03-Xy_z-AA"])
def test_cleaning_never_damages_a_real_secret_shape(secret):
    assert clean_credential(secret) == (secret, [])
