"""
The weekly loop, driven for real, must land a Runner Lane in playbook.md.

Every other test in this area checks a piece: the prompt says to write the
section, the extractor finds it, the checker enforces it, the selector renders
it. None of them run `weekly.run`, and the break being fixed lived precisely
in the wiring between pieces that each worked -- the research layer computed a
full runner block every week and no code path carried it into the document.

So this drives the real loop with only the Anthropic call replaced: the actual
research block is built, the actual playbook write executes, the actual
checker runs, and the selector then reads the result. Mock mode cannot do this
job -- by design it skips the playbook rewrite entirely, which is the one step
under test.
"""
from __future__ import annotations

import io
import json
from datetime import date

import pytest

from agent import ledger, memory
from agent.config import load_config
from agent.loops import weekly


class _StubResult:
    """Shaped like agent.llm.LLMResult."""

    def __init__(self, text: str, parsed: dict):
        self.text = text
        self.parsed = parsed
        self.usage = {"input_tokens": 0, "output_tokens": 0,
                      "web_searches": 0, "cost_usd": 0.0}


def _mock_payload() -> dict:
    raw = json.load(io.open("mocks/weekly_miner.json", encoding="utf-8"))["text"]
    return json.loads(raw[raw.index("{"):raw.rindex("}") + 1])


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """A real (non-mock) weekly run with every outward effect stubbed."""
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "w.duckdb")
    monkeypatch.setattr(memory, "PLAYBOOK_PATH", tmp_path / "playbook.md")
    monkeypatch.setattr(memory, "PLAYBOOK_HISTORY_DIR", tmp_path / "history")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    sent: list = []
    monkeypatch.setattr(weekly, "send_email", lambda *a, **k: sent.append(1))
    monkeypatch.setattr(weekly, "render_dashboard", lambda *a, **k: None)
    monkeypatch.setattr(weekly, "_git_commit_playbook", lambda *a, **k: None)

    payload = _mock_payload()

    class _StubLLM:
        def __init__(self, cfg):
            pass

        def call(self, **kw):
            return _StubResult(json.dumps(payload), payload)

    monkeypatch.setattr(weekly, "LLMClient", _StubLLM)

    cfg = load_config()
    cfg.mock = False          # the real write path, not the mock short-circuit
    cfg.offline = True
    return cfg, payload


def test_weekly_run_writes_the_runner_section_into_the_playbook(wired):
    """The wiring, end to end, through the real loop."""
    cfg, payload = wired
    assert "## Runner Lane" in payload["playbook_markdown"], (
        "fixture precondition: the miner payload must contain the section")

    weekly.run(cfg, run_date=date(2026, 9, 21))

    con = ledger.connect()
    text, _version = memory.read_playbook(con)
    con.close()
    assert "## Runner Lane" in text, (
        "the weekly loop did not carry the miner's runner section into the "
        "playbook -- the selector will keep reading the placeholder")


def test_the_written_section_is_what_the_selector_receives(wired):
    """Written is not the same as delivered. This asserts the section the loop
    persisted is the text the selector's extractor hands to the prompt."""
    cfg, _payload = wired
    weekly.run(cfg, run_date=date(2026, 9, 21))

    con = ledger.connect()
    from agent.runner_selection import NO_RUNNER_RULES, _playbook_section
    section = _playbook_section(con)
    con.close()

    assert section != NO_RUNNER_RULES
    assert section.startswith("## Runner Lane")
    assert "selection edge" in section.lower()


def test_the_run_leaves_no_write_time_violations(wired, capsys):
    """The checker must pass on a well-formed document. A checker that warns
    on correct input trains its reader to ignore it."""
    cfg, _payload = wired
    weekly.run(cfg, run_date=date(2026, 9, 21))
    assert "runner-section violation" not in capsys.readouterr().out


def test_a_miner_that_omits_the_section_still_delivers_one(wired, monkeypatch, capsys):
    """And the negative: a miner response that drops the section must not
    silently revert the selector to the placeholder. The section is now
    system-rendered (2026-09-29 audit), so the selector gets the computed
    Runner Lane whatever the miner wrote."""
    cfg, payload = wired
    stripped = dict(payload)
    pb = stripped["playbook_markdown"]
    start = pb.index("## Runner Lane")
    end = pb.index("## Open Questions")
    stripped["playbook_markdown"] = pb[:start] + pb[end:]

    class _StubLLM:
        def __init__(self, cfg):
            pass

        def call(self, **kw):
            return _StubResult(json.dumps(stripped), stripped)

    monkeypatch.setattr(weekly, "LLMClient", _StubLLM)
    weekly.run(cfg, run_date=date(2026, 9, 21))

    con = ledger.connect()
    from agent.runner_selection import NO_RUNNER_RULES, _playbook_section
    section = _playbook_section(con)
    con.close()
    assert section != NO_RUNNER_RULES
    assert section.startswith("## Runner Lane")
    assert "System-rendered" in section and NO_RUNNER_RULES in section
