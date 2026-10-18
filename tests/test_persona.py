"""PERSONA_CHARACTER_SHEET_SPEC_v1 -- the character sheet, its loader, and the
additive guarantee: with the persona off, every rendered prompt is
byte-identical to the pre-persona text (tests/fixtures/prompt_renders/)."""
from __future__ import annotations

import importlib.util
import re
import shutil
from pathlib import Path

import pytest

from agent import llm
from agent.config import Config, _DEFAULTS

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "prompt_renders"
PROMPTS = ("morning_analyst", "morning_risk", "evening_grader", "weekly_miner")
ROLE_OF = {"morning_analyst": "analyst", "morning_risk": "risk", "evening_grader": "grader",
           "weekly_miner": "miner"}


def _capture_module():
    spec = importlib.util.spec_from_file_location(
        "capture_prompt_renders", ROOT / "scripts" / "capture_prompt_renders.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CAPTURE = _capture_module()


def _cfg(enabled: bool, version=1) -> Config:
    return Config(raw={"persona": {"enabled": enabled, "version": version}})


def _fixture(name: str) -> str:
    # read_text normalizes line endings exactly as load_prompt does, so a
    # checkout that turned the fixture's LF into CRLF still compares equal.
    return (FIXTURES / f"{name}.md").read_text(encoding="utf-8")


# ------------------------------------------------------------ the sheet itself

def test_persona_default_is_off():
    assert _DEFAULTS["persona"] == {"enabled": False, "version": 1}
    assert Config(raw={}).persona_cfg == {}


def test_persona_file_has_no_digit_below_its_version_header():
    lines = (ROOT / "prompts" / "persona.md").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "<!-- persona-version: 1 -->"
    body = "\n".join(lines[1:])
    assert not re.search(r"\d", body), "P-1: the character sheet may not carry a number"


def test_persona_file_has_exactly_the_spec_sections():
    text = (ROOT / "prompts" / "persona.md").read_text(encoding="utf-8")
    heads = [l for l in text.splitlines() if l.startswith("## ")]
    assert heads == ["## Shared", "## Role: analyst", "## Role: risk", "## Role: grader",
                     "## Role: miner"]


def test_persona_block_is_shared_plus_own_role_only():
    _, sections = llm._persona_sections()
    for role in llm.PERSONA_ROLES:
        block = llm.persona_block(_cfg(True), role)
        assert block.count(sections["shared"]) == 1
        assert sections[role] in block
        for other in llm.PERSONA_ROLES:
            if other != role:
                assert sections[other] not in block, (role, other)
        assert "## Shared" not in block and "## Role:" not in block


def test_persona_block_off_is_empty_and_unstamped():
    assert llm.persona_block(_cfg(False), "analyst") == ""
    assert llm.persona_version(_cfg(False)) is None
    assert llm.persona_block(Config(raw={}), "analyst") == ""
    # Only a literal True turns it on.
    assert llm.persona_block(Config(raw={"persona": {"enabled": "yes"}}), "analyst") == ""


def test_persona_version_stamp_when_on():
    assert llm.persona_version(_cfg(True)) == "1"


def test_config_version_pin_mismatch_turns_the_persona_off():
    assert llm.persona_block(_cfg(True, version=2), "risk") == ""
    assert llm.persona_version(_cfg(True, version=2)) is None


def test_unknown_role_turns_the_persona_off_not_the_loop():
    assert llm.persona_block(_cfg(True), "astrologer") == ""


def test_shared_only_block_for_role_none():
    _, sections = llm._persona_sections()
    assert llm.persona_block(_cfg(True), None) == sections["shared"]


# ------------------------------------------------------ the additive guarantee

@pytest.mark.parametrize("name", PROMPTS)
def test_persona_off_render_is_byte_identical_to_fixture(name):
    repl = CAPTURE.RENDER_CASES[name]
    assert llm.load_prompt(name, repl) == _fixture(name)
    # Passing the block for a disabled persona is the same as not passing it.
    assert llm.load_prompt(name, {**repl, "PERSONA": llm.persona_block(_cfg(False), ROLE_OF[name])}) \
        == _fixture(name)


def test_morning_block_off_is_byte_identical_to_fixture():
    from agent.backlog import morning_block
    assert morning_block(CAPTURE.BACKLOG_ITEMS) == _fixture("morning_block")


def test_missing_persona_file_with_flag_on_degrades_to_no_persona(tmp_path, monkeypatch):
    for name in PROMPTS:
        shutil.copy(ROOT / "prompts" / f"{name}.md", tmp_path / f"{name}.md")
    monkeypatch.setattr(llm, "PROMPTS_DIR", tmp_path)
    assert llm.persona_block(_cfg(True), "analyst") == ""
    assert llm.persona_version(_cfg(True)) is None
    for name in PROMPTS:
        repl = CAPTURE.RENDER_CASES[name]
        rendered = llm.load_prompt(name, {**repl, "PERSONA": llm.persona_block(_cfg(True), ROLE_OF[name])})
        assert rendered == _fixture(name)
        assert "{{" not in rendered


# ------------------------------------------------------- Phase 2: integration

# Bumped by TRADER_HYPOTHESIS_SPEC_v1 (hypothesis placeholders; the lane-off
# render differs from the previous fixtures only by these version lines).
# evening_grader 10 -> 11: the order-mechanics paragraph (docs/FIX_PHASE0.md, D6).
# morning_analyst 13 -> 14: ETF levels priced from the ETF live prices line (D8).
PROMPT_VERSIONS = {"morning_analyst": "14", "morning_risk": "16", "evening_grader": "11",
                   "weekly_miner": "18"}

# The paragraph each persona block must directly follow (its prompt's role
# paragraph; the analyst's is its mission paragraph, which the sheet refines).
FOLLOWS = {
    "morning_analyst": "or a deliberate, labeled probe to find new edge.",
    "morning_risk": "a kill it shows was a loser is a save.",
    "evening_grader": "Your job is to turn today into durable memory.",
    "weekly_miner": "and **rewrite the playbook** — the living document that IS the desk's accumulated skill.",
}


@pytest.mark.parametrize("name", PROMPTS)
def test_persona_placeholder_appears_exactly_once(name):
    raw = (ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    assert raw.count("{{PERSONA}}") == 1
    assert llm.prompt_version(name) == PROMPT_VERSIONS[name]


@pytest.mark.parametrize("name", PROMPTS)
def test_persona_on_render_is_the_off_render_plus_one_block(name):
    repl = CAPTURE.RENDER_CASES[name]
    block = llm.persona_block(_cfg(True), ROLE_OF[name])
    on = llm.load_prompt(name, {**repl, "PERSONA": block})
    off = _fixture(name)
    _, sections = llm._persona_sections()
    assert on.count(sections["shared"]) == 1
    assert sections[ROLE_OF[name]] in on
    for other in llm.PERSONA_ROLES:
        if other != ROLE_OF[name]:
            assert sections[other] not in on
    # Nothing else moved: take the block out and the persona-off text is back.
    assert on.replace(block + "\n\n", "", 1) == off
    # ... and it sits right after the role paragraph.
    assert FOLLOWS[name] + "\n\n" + block + "\n\n" in on
    assert "{{" not in on


def test_persona_on_keeps_every_pinned_prompt_string():
    def on(name):
        return llm.load_prompt(name, {**CAPTURE.RENDER_CASES[name],
                                      "PERSONA": llm.persona_block(_cfg(True), ROLE_OF[name])})
    risk, analyst, miner, grader = (on("morning_risk"), on("morning_analyst"),
                                    on("weekly_miner"), on("evening_grader"))
    assert "The desk trades every session." in risk and "The desk trades every session." in analyst
    assert "Your mission is singular: **win**" in analyst
    assert analyst.count("Your mission is singular") == 1, "the sheet refines the mission, never restates it"
    assert "must be **>= 2.0** using the analyst's own numbers" in risk
    assert "no_trade_reason" not in risk
    assert "may not tell the desk to stand down for the day" in miner
    assert "No direction without significance" in miner
    assert "INVALID FILL" in grader and "Do not inflate grades to keep rules alive" in grader


def test_persona_adds_no_number_to_any_prompt():
    for name in PROMPTS:
        block = llm.persona_block(_cfg(True), ROLE_OF[name])
        assert block and not re.search(r"\d", block), name


def test_morning_block_persona_lead_in_adds_only_the_debt_paragraph():
    from agent.backlog import PERSONA_LEAD_IN, morning_block
    off = morning_block(CAPTURE.BACKLOG_ITEMS)
    on = morning_block(CAPTURE.BACKLOG_ITEMS, persona=True)
    assert off == _fixture("morning_block")
    assert on.replace(PERSONA_LEAD_IN + "\n\n", "", 1) == off
    lines = on.splitlines()
    assert lines[0] == "## DATA-STARVED (system backlog — top 3)" and lines[2] == PERSONA_LEAD_IN
    assert "COUNTS toward a threshold, not findings" in on and "probe_for" in on
    assert "2/8" in on and "~12 sessions at current pace" in on
    assert "STALLED (nothing in 10 sessions)" in on and "Counts: " in on
    assert not re.search(r"\d", PERSONA_LEAD_IN)
    for word in ("mean", "win", "p_value", "effect", "avg"):
        assert word not in PERSONA_LEAD_IN.lower()
    assert morning_block([], persona=True) == ""


def test_morning_memory_uses_the_lead_in_only_when_the_persona_is_on(tmp_path, monkeypatch):
    from datetime import date
    from agent import backlog, ledger, memory
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "l.duckdb")
    con = ledger.connect()
    try:
        con.execute(
            """INSERT INTO evidence_backlog (session_date, rank, item_type, item_id, lane, n,
                   target, rate_10s, eta_sessions, stalled, what_counts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [date(2026, 10, 2), 1, "cell", "morning:x=y", "morning", 3, 8, 0, None, True, "x"])
        raw = {"memory": {"recent_trades_in_context": 5, "max_active_lessons_in_context": 5}}
        off = memory.build_morning_memory(con, Config(raw=dict(raw)))["text"]
        on = memory.build_morning_memory(
            con, Config(raw={**raw, "persona": {"enabled": True, "version": 1}}))["text"]
    finally:
        con.close()
    assert backlog.PERSONA_LEAD_IN not in off and "## DATA-STARVED" in off
    assert backlog.PERSONA_LEAD_IN in on
