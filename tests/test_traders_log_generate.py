"""TRADERS_LOG_SPEC_v1 Phase 3 -- the prompt and generate(): one call, one
retry on violations, a facts-only fallback, never an exception."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from agent import ledger, llm, traders_log as tl
from agent.config import Config
from agent.llm import LLMResult
from _traders_log_seed import D, playbook, seed_session

ROOT = Path(__file__).resolve().parent.parent
CLEAN = json.loads((ROOT / "mocks" / "traders_log.json").read_text(encoding="utf-8"))["text"]
BAD = json.loads((ROOT / "mocks" / "traders_log_violation.json").read_text(encoding="utf-8"))["text"]


class Stub:
    def __init__(self, *texts, raises: Exception | None = None):
        self.texts, self.raises, self.calls = list(texts), raises, []

    def call(self, **kw):
        self.calls.append(kw)
        if self.raises:
            raise self.raises
        return LLMResult(text=self.texts.pop(0),
                         usage={"input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.006})


def _cfg(persona=False):
    return Config(raw={"models": {"evening": "claude-sonnet-4-5"},
                       "traders_log": {"enabled": True, "max_chars": 3000},
                       "persona": {"enabled": persona, "version": 1}})


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "ledger.duckdb")
    playbook(tmp_path, monkeypatch)
    c = ledger.connect()
    yield c
    c.close()


def _rows(con):
    return con.execute("SELECT status, rendered_text, raw_text FROM traders_log").fetchall()


def test_the_prompt_file(con):
    raw = (ROOT / "prompts" / "traders_log.md").read_text(encoding="utf-8")
    assert llm.prompt_version("traders_log") == "1"
    assert raw.count("{{PERSONA}}") == 1
    assert not re.search(r"\d", "\n".join(raw.splitlines()[1:])), "the log prompt carries no number"
    for h in tl.HEADINGS:
        assert h in raw
    assert "Tomorrow:" in raw and "no change" in raw


def test_clean_draft_is_stored_ok_with_its_cost(con):
    seed_session(con)
    stub = Stub(CLEAN)
    row = tl.generate(con, _cfg(), D, llm_client=stub)
    assert row["status"] == "ok" and len(stub.calls) == 1
    call = stub.calls[0]
    assert call["attempts"] == 1 and call["timeout"] <= 45 and call["expect_json"] is False
    assert call["model"] == "claude-sonnet-4-5"
    assert "{{day.net_r}} = -0.20R" in call["user"]
    # Slots and the menu only: no playbook text, no lessons, no trade tables.
    assert "# Trading Agent Playbook" not in call["user"] and "LESSON" not in call["user"]
    assert call["user"].count("{{verdict.calibration_q3}}") == 1
    (status, rendered, raw), = _rows(con)
    assert status == "ok" and raw == CLEAN
    assert "Morning picks taken: 5 (one session)." in rendered
    run = con.execute("SELECT loop, status, cost_usd, input_tokens FROM runs").fetchall()
    assert run == [("traders_log", "ok", 0.006, 1000)]


def test_a_digit_is_retried_once_then_falls_back(con):
    seed_session(con)
    stub = Stub(BAD, BAD)
    row = tl.generate(con, _cfg(), D, llm_client=stub)
    assert len(stub.calls) == 2
    assert "YOUR PREVIOUS DRAFT BROKE THESE RULES" in stub.calls[1]["user"]
    assert "digit outside a slot" in stub.calls[1]["user"]
    assert row["status"] == "fallback_facts_only"
    assert row["rendered_text"] == tl.render_facts_only(tl.build_facts(con, D))
    v = json.loads(row["violations_json"])
    assert v["first_draft"] and v["retry"]
    assert con.execute("SELECT status, cost_usd FROM runs").fetchone() == ("degraded", 0.012)


def test_a_retry_that_clears_the_rules_is_ok(con):
    seed_session(con)
    row = tl.generate(con, _cfg(), D, llm_client=Stub(BAD, CLEAN))
    assert row["status"] == "ok" and json.loads(row["violations_json"])["first_draft"]


def test_a_raising_client_never_escapes(con):
    seed_session(con)
    row = tl.generate(con, _cfg(), D, llm_client=Stub(raises=RuntimeError("provider down")))
    assert row["status"] == "error" and "provider down" in row["violations_json"]
    assert row["rendered_text"] == tl.render_facts_only(tl.build_facts(con, D))
    assert _rows(con)[0][0] == "error"


def test_empty_facts_are_skipped_without_a_call(con):
    stub = Stub(CLEAN)
    row = tl.generate(con, _cfg(), D, llm_client=stub)
    assert row["status"] == "skipped" and stub.calls == []
    assert con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_rerunning_a_date_replaces_its_row(con):
    seed_session(con)
    tl.generate(con, _cfg(), D, llm_client=Stub(BAD, BAD))
    tl.generate(con, _cfg(), D, llm_client=Stub(CLEAN))
    assert [r[0] for r in _rows(con)] == ["ok"]
    assert tl.load(con, D)["status"] == "ok"


def test_an_exhausted_budget_stores_facts_without_a_call(con):
    seed_session(con)
    stub = Stub(CLEAN)
    row = tl.generate(con, _cfg(), D, llm_client=stub, deadline=time.monotonic() + 5)
    assert stub.calls == [] and row["status"] == "fallback_facts_only"
    assert "evening budget" in row["violations_json"]


def test_persona_on_is_in_the_system_prompt_and_stamped(con):
    seed_session(con)
    stub = Stub(CLEAN)
    row = tl.generate(con, _cfg(persona=True), D, llm_client=stub)
    shared = llm._persona_sections()[1]["shared"]
    assert shared in stub.calls[0]["system"] and row["persona_version"] == "1"
    stub = Stub(CLEAN)
    row = tl.generate(con, _cfg(persona=False), D, llm_client=stub)
    assert shared not in stub.calls[0]["system"] and row["persona_version"] is None
    assert "{{PERSONA}}" not in stub.calls[0]["system"]


def test_the_canned_mocks_render_and_violate_as_labeled(con):
    seed_session(con)
    facts = tl.build_facts(con, D)
    assert tl.render(CLEAN, facts)[1] == []
    assert any("digit outside a slot" in x for x in tl.render(BAD, facts)[1])


def test_mock_mode_uses_the_canned_draft_and_persists_nothing(con):
    seed_session(con)
    cfg = _cfg()
    cfg.mock = True
    row = tl.generate(con, cfg, D)
    assert row["status"] == "ok" and row["raw_text"] == CLEAN
    assert _rows(con) == [] and con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
