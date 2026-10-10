"""PERSONA_CHARACTER_SHEET_SPEC_v1 Phase 4 -- offline parity. The persona
changes prompt TEXT, never the output contract: every loop still runs end to
end in mock mode with the persona on, and the canned responses still parse."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from agent import ledger, llm
from agent.config import load_config
from agent.loops import evening, morning, weekly

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "test.duckdb")
    c = load_config()
    c.raw = {**c.raw, "persona": {"enabled": True, "version": 1}}
    c.mock = True
    c.offline = True
    return c


@pytest.fixture
def systems(monkeypatch):
    seen: dict[str, str] = {}
    real = llm.LLMClient.call

    def spy(self, *, system, mock_name, **kw):
        seen[mock_name] = system
        return real(self, system=system, mock_name=mock_name, **kw)

    monkeypatch.setattr(llm.LLMClient, "call", spy)
    return seen


def _sheet() -> str:
    return llm._persona_sections()[1]["shared"]


def test_morning_mock_runs_with_the_persona_in_both_prompts(cfg, systems, monkeypatch):
    monkeypatch.setattr("agent.loops.morning.send_email", lambda *a, **k: None)
    monkeypatch.setattr("agent.loops.morning.render_dashboard", lambda *a, **k: None)
    morning.run(cfg, run_date=date(2026, 8, 24), force=True)
    sections = llm._persona_sections()[1]
    assert _sheet() in systems["morning_analyst"] and sections["analyst"] in systems["morning_analyst"]
    assert _sheet() in systems["morning_risk"] and sections["risk"] in systems["morning_risk"]
    con = ledger.connect()
    try:
        assert con.execute("SELECT status FROM runs WHERE loop='morning'").fetchone()[0] == "mock"
    finally:
        con.close()


def test_evening_mock_runs_with_the_persona(cfg, systems, monkeypatch):
    monkeypatch.setattr("agent.loops.evening.send_email", lambda *a, **k: None)
    monkeypatch.setattr("agent.loops.evening.render_dashboard", lambda *a, **k: None)
    con = ledger.connect()
    d = date(2026, 8, 24)
    ledger.save_daily_plan(con, d, {
        "no_trade": False, "market_stance": "t", "picks": [{
            "ticker": "TEST", "direction": "long", "setup_type": "gap_go", "conviction": 4,
            "size_r": 1.0, "entry_plan": {"type": "open", "price": 100.0}, "entry_price": 100.0,
            "stop_price": 98.0, "target_price": 104.0}], "rejected": []}, {"candidates": []}, "chop")
    con.close()
    evening.run(cfg, run_date=d, force=True)
    assert llm._persona_sections()[1]["grader"] in systems["evening_grader"]


def test_weekly_mock_runs_with_the_persona(cfg, systems, monkeypatch):
    monkeypatch.setattr("agent.loops.weekly.send_email", lambda *a, **k: None)
    monkeypatch.setattr("agent.loops.weekly.render_dashboard", lambda *a, **k: None)
    monkeypatch.setattr("agent.loops.weekly._git_commit_playbook", lambda *a, **k: None)
    weekly.run(cfg, run_date=date(2026, 8, 24))
    assert llm._persona_sections()[1]["miner"] in systems["weekly_miner"]


@pytest.mark.parametrize("name,keys", [
    ("morning_analyst", {"market_read", "candidates"}),
    ("morning_risk", {"picks", "rejected"}),
    ("evening_grader", {"plan_grade", "pick_reviews", "lessons"}),
    ("weekly_miner", {"summary_for_email", "playbook_markdown"}),
])
def test_mock_contracts_unchanged_with_the_persona_on(cfg, name, keys):
    res = llm.LLMClient(cfg).call(system=llm.load_prompt(name, {"PERSONA": llm.persona_block(cfg, "risk")}),
                                  user="u", model="m", mock_name=name)
    assert keys <= set(res.parsed or {})


