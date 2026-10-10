"""TRADERS_LOG_SPEC_v1 Phase 5 -- the firewall. The trader's log is written
for the owner and read by no part of the machine. These tests are what make
that true; a change that fails one is a change that lets narrative back into
the learning loop. Add to an allowlist deliberately or not at all."""
from __future__ import annotations

import ast
import json
import re
from datetime import date, datetime
from pathlib import Path

import pytest

from agent import ledger, llm, traders_log as tl

ROOT = Path(__file__).resolve().parent.parent
NAME = "traders_log"
SKIP_DIRS = {".venv", ".git", "__pycache__", "node_modules", ".pytest_cache", "reports",
             "llm_responses", "playbook_history", "data"}

# file -> predicate every line mentioning traders_log must satisfy (None = any line)
ALLOWLIST = {
    "agent/traders_log.py": None,
    "agent/migrations_v30.py": None,
    "prompts/traders_log.md": None,
    "run.py": None,                      # the owner's CLI: traders-log
    "agent/delivery.py": None,           # the evening email sections
    "agent/config.py": None,             # the config key
    "api/index.py": lambda l: '"traders_log": "session_date"' in l or l.lstrip().startswith("#"),
    "agent/webui.py": lambda l: '<option value="traders_log">' in l,
    "agent/loops/evening.py": None,      # the hook; confined to its block below
    # The pre-deploy rehearsal report (VERIFICATION_PRACTICE_v1): it shows the
    # owner the rendered log of a scratch session and clears that row before
    # re-grading; nothing it reads reaches a prompt or the ledger's learning.
    "agent/rehearsal.py": None,
}


def _files():
    for p in ROOT.rglob("*"):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS):
            rel = p.relative_to(ROOT).as_posix()
            if p.suffix == ".py" or (rel.startswith("prompts/") and p.suffix == ".md"):
                yield rel, p


# 1 ------------------------------------------------------------ read paths

def test_every_mention_of_the_log_is_on_the_allowlist():
    offenders = []
    for rel, p in _files():
        if rel.startswith("tests/"):
            continue
        lines = [l for l in p.read_text(encoding="utf-8").splitlines() if NAME in l]
        if not lines:
            continue
        if rel not in ALLOWLIST:
            offenders.append(rel)
            continue
        rule = ALLOWLIST[rel]
        offenders += [f"{rel}: {l.strip()}" for l in lines if rule and not rule(l)]
    assert not offenders, "traders_log reached a file outside the allowlist: " + "; ".join(offenders)


def test_the_evening_mentions_the_log_only_inside_its_hook():
    src = (ROOT / "agent" / "loops" / "evening.py").read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(src) if "TRADERS_LOG_SPEC_v1: the owner's first-person log" in l)
    end = next(i for i in range(start, len(src)) if "send_email(" in src[i])
    outside = [l for i, l in enumerate(src) if NAME in l and not start <= i <= end + 4]
    assert not outside, outside
    # ... and the hook is the only caller of generate() outside the CLI.
    callers = [rel for rel, p in _files() if not rel.startswith("tests/")
               and "traders_log.generate(" in p.read_text(encoding="utf-8")]
    assert sorted(callers) == ["agent/loops/evening.py", "run.py"] or callers == ["agent/loops/evening.py"]


# 2 ----------------------------------------------------- no learning import

LEARNERS = ["agent/memory.py", "agent/backlog.py", "agent/research.py", "agent/integrity.py",
            "agent/grading.py", "agent/loops/weekly.py", "agent/loops/morning.py",
            "agent/loops/paired.py", "agent/playbook_enforcement.py", "agent/question_eval.py",
            "agent/structural.py", "agent/population.py", "agent/ablation.py",
            "agent/grounding.py", "agent/persona_monitor.py", "agent/llm.py"]


@pytest.mark.parametrize("rel", LEARNERS)
def test_no_learning_module_imports_or_names_the_log(rel):
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert NAME not in src
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").endswith(NAME)
            assert all(a.name != NAME for a in node.names)
        elif isinstance(node, ast.Import):
            assert all(not a.name.endswith(NAME) for a in node.names)


# 3 ------------------------------------------------------ no prompt mentions

def test_no_other_prompt_mentions_the_log():
    for p in (ROOT / "prompts").glob("*.md"):
        if p.name == "traders_log.md":
            continue
        text = p.read_text(encoding="utf-8").lower()
        assert NAME not in text and "trader's log" not in text and "traders log" not in text, p.name


def test_only_the_log_module_loads_the_log_prompt():
    loaders = [rel for rel, p in _files() if not rel.startswith("tests/")
               and re.search(r"load_prompt\(\s*[\"']traders_log[\"']", p.read_text(encoding="utf-8"))]
    assert loaders == ["agent/traders_log.py"]


# 4 ---------------------------------------- rendered prompts never carry it

SENTINEL = "QUOKKA-SENTINEL-ZEBRA belief that must never travel"


@pytest.fixture
def poisoned(tmp_path, monkeypatch):
    """A seeded ledger whose traders_log rows (today and yesterday) carry a
    sentinel in every text column."""
    from _traders_log_seed import D, playbook, seed_session
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "ledger.duckdb")
    playbook(tmp_path, monkeypatch)
    con = ledger.connect()
    seed_session(con)
    for d in (D, date(2026, 10, 1)):
        con.execute(
            """INSERT INTO traders_log (session_date, lane, generated_at, status, facts_json,
                   raw_text, rendered_text, violations_json)
               VALUES (?, 'combined', ?, 'ok', ?, ?, ?, ?)""",
            [d, datetime(2026, 10, 2, 21), json.dumps({"x": SENTINEL}), SENTINEL, SENTINEL,
             json.dumps([SENTINEL])])
    con.close()
    return D


def _spy(monkeypatch):
    seen: list[str] = []
    real = llm.LLMClient.call

    def spy(self, **kw):
        seen.append(kw.get("system", "") + "\n" + kw.get("user", ""))
        return real(self, **kw)

    monkeypatch.setattr(llm.LLMClient, "call", spy)
    return seen


def test_morning_and_weekly_prompts_never_carry_the_log(poisoned, monkeypatch):
    from agent.config import load_config
    from agent.loops import morning, weekly
    seen = _spy(monkeypatch)
    for mod in (morning, weekly):
        monkeypatch.setattr(f"agent.loops.{mod.__name__.rsplit('.', 1)[1]}.send_email", lambda *a, **k: None)
        monkeypatch.setattr(f"agent.loops.{mod.__name__.rsplit('.', 1)[1]}.render_dashboard",
                            lambda *a, **k: None)
    monkeypatch.setattr("agent.loops.weekly._git_commit_playbook", lambda *a, **k: None)
    cfg = load_config()
    cfg.mock, cfg.offline = True, True
    cfg.raw = {**cfg.raw, "persona": {"enabled": True, "version": 1}}
    morning.run(cfg, run_date=date(2026, 10, 5), force=True)
    weekly.run(cfg, run_date=date(2026, 10, 3))
    assert len(seen) >= 3
    assert not any(SENTINEL in s for s in seen)


def test_the_grader_prompt_never_carries_the_log(poisoned, monkeypatch):
    from agent.config import load_config
    from agent.loops import evening
    seen: list[str] = []

    class _Client:
        def __init__(self, cfg):
            pass

        def call(self, **kw):
            seen.append(kw.get("system", "") + "\n" + kw.get("user", ""))
            return llm.LLMResult(text="", parsed={"plan_grade": "B", "pick_reviews": [], "lessons": [],
                                                  "lesson_updates": []},
                                 usage={"input_tokens": 0, "output_tokens": 0, "web_searches": 0,
                                        "cost_usd": 0.0})

    monkeypatch.setattr(evening, "LLMClient", _Client)
    monkeypatch.setattr("agent.loops.evening.send_email", lambda *a, **k: None)
    monkeypatch.setattr("agent.loops.evening.render_dashboard", lambda *a, **k: None)
    cfg = load_config()
    cfg.mock, cfg.offline = False, True
    cfg.raw = {**cfg.raw, "traders_log": {**cfg.raw["traders_log"], "enabled": False}}
    evening.run(cfg, run_date=poisoned, force=True)
    assert seen and not any(SENTINEL in s for s in seen)


def test_morning_memory_never_carries_the_log(poisoned):
    from agent import memory
    from agent.config import load_config
    cfg = load_config()
    con = ledger.connect()
    try:
        texts = [memory.build_morning_memory(con, cfg)["text"],
                 json.dumps(memory.build_weekly_memory(con, cfg), default=str)]
    finally:
        con.close()
    assert not any(SENTINEL in t for t in texts)


# 5 ------------------------------------------- the belief persists only once

def test_the_log_module_writes_only_its_own_table():
    src = (ROOT / "agent" / "traders_log.py").read_text(encoding="utf-8")
    writes = re.findall(r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(\w+)", src, re.I)
    assert writes and {t for _, t in writes} == {NAME}


def test_the_draft_skips_the_response_archive_and_runs_error(tmp_path, monkeypatch):
    from _traders_log_seed import D, playbook, seed_session
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "l.duckdb")
    playbook(tmp_path, monkeypatch)
    con = ledger.connect()
    seed_session(con)
    calls = []

    class Stub:
        def call(self, **kw):
            calls.append(kw)
            return llm.LLMResult(text="## What I believe\n" + SENTINEL + " 7", usage={})

    from agent.config import Config
    tl.generate(con, Config(raw={"traders_log": {"enabled": True}}), D, llm_client=Stub())
    errors = [r[0] or "" for r in con.execute("SELECT error FROM runs").fetchall()]
    stored = con.execute("SELECT raw_text FROM traders_log").fetchone()[0]
    con.close()
    assert calls and all(c["archive"] is False for c in calls)
    assert SENTINEL in stored                       # kept once, where it belongs
    assert not any(SENTINEL in e for e in errors)   # and nowhere else
