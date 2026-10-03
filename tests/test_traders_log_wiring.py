"""TRADERS_LOG_SPEC_v1 Phase 4 -- the evening hook, the email sections, the
CLI and table access."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from agent import ledger, llm, traders_log as tl
from agent.config import load_config
from agent.delivery import evening_email_html
from agent.loops import evening
from _traders_log_seed import D, playbook, seed_session

ROOT = Path(__file__).resolve().parent.parent
CLEAN = json.loads((ROOT / "mocks" / "traders_log.json").read_text(encoding="utf-8"))["text"]
BAD = json.loads((ROOT / "mocks" / "traders_log_violation.json").read_text(encoding="utf-8"))["text"]
REVIEW = {"plan_grade": "B", "plan_notes": "Clean plan; one chase.",
          "pick_reviews": [{"ticker": "AAA", "grade": "A", "why": "stop held"},
                           {"ticker": "BBB", "grade": "C", "why": "chased"}],
          "lessons": [{"lesson": "gap fades after offerings"}]}
SUMMARY = "AAA +1.20R target\nBBB -1.00R stop"


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "ledger.duckdb")
    playbook(tmp_path, monkeypatch)
    c = ledger.connect()
    yield c
    c.close()


# ---------------------------------------------------------------------- email

def test_email_without_the_log_is_byte_identical_to_before():
    # Captured from evening_email_html at 2697dd2, before the log existed.
    before = (ROOT / "tests" / "fixtures" / "evening_email_before_log.html").read_text(encoding="utf-8")
    assert evening_email_html(date(2026, 10, 2), REVIEW, SUMMARY) == before
    assert evening_email_html(date(2026, 10, 2), REVIEW, SUMMARY, traders_log=None, day_contrast="") == before


def test_email_with_an_ok_log_and_the_contrast_block(con):
    seed_session(con)
    row = tl.generate(con, _cfg(), D, llm_client=_Stub(CLEAN))
    html = evening_email_html(D, REVIEW, SUMMARY, traders_log=row, day_contrast=tl.day_contrast(con, D))
    assert "<h3>Trader's log</h3>" in html and "<h4 style=\"margin:14px 0 4px\">What happened</h4>" in html
    assert "Morning picks taken: 5 (one session)." in html
    assert "facts-only" not in html
    assert "<h3>Today's winners and losers (descriptive)</h3>" in html
    assert html.index("Trader's log") > html.index(SUMMARY.splitlines()[0])


def test_email_with_the_fallback_says_so(con):
    seed_session(con)
    row = tl.generate(con, _cfg(), D, llm_client=_Stub(BAD, BAD))
    html = evening_email_html(D, REVIEW, SUMMARY, traders_log=row)
    assert tl.FALLBACK_NOTE in html and "FACTS ONLY (no narrative)" in html


def test_a_skipped_log_adds_nothing(con):
    row = tl.generate(con, _cfg(), D, llm_client=_Stub(CLEAN))
    assert row["status"] == "skipped"
    assert evening_email_html(D, REVIEW, SUMMARY, traders_log=row) == evening_email_html(D, REVIEW, SUMMARY)


def test_contrast_block_is_counts_only(con):
    seed_session(con)
    text = tl.day_contrast(con, D)
    assert text.startswith(tl.CONTRAST_HEADER)
    assert "setup_type=gap_go: winners 1, losers 0" in text
    assert "setup_type=gap_fade: winners 0, losers 1" in text
    for bad in ("mean", "avg", "win rate", "%", "p =", "edge"):
        assert bad not in text.lower(), bad
    assert tl.day_contrast(con, date(2026, 1, 5)) == ""


# ---------------------------------------------------------------- the hook

class _Stub:
    def __init__(self, *texts, raises=None):
        self.texts, self.raises = list(texts), raises

    def call(self, **kw):
        if self.raises:
            raise self.raises
        return llm.LLMResult(text=self.texts.pop(0),
                             usage={"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001})


class _Client:
    """Stands in for LLMClient in both the grader call and the log call."""
    def __init__(self, cfg):
        pass

    def call(self, **kw):
        usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0, "cost_usd": 0.0}
        if kw.get("mock_name") == "traders_log":
            return llm.LLMResult(text=CLEAN, usage=usage)
        return llm.LLMResult(text="", parsed={"plan_grade": "B", "pick_reviews": [], "lessons": [],
                                              "lesson_updates": []}, usage=usage)


def _cfg(enabled=True):
    from agent.config import Config
    return Config(raw={"models": {"evening": "claude-sonnet-4-5"},
                       "traders_log": {"enabled": enabled}})


def _evening(monkeypatch, enabled: bool, generate=None):
    sent = []
    monkeypatch.setattr("agent.loops.evening.send_email", lambda cfg, subj, html: sent.append(html))
    monkeypatch.setattr("agent.loops.evening.render_dashboard", lambda *a, **k: None)
    monkeypatch.setattr(evening, "LLMClient", _Client)
    monkeypatch.setattr(llm, "LLMClient", _Client)
    if generate is not None:
        monkeypatch.setattr(tl, "generate", generate)
    cfg = load_config()
    cfg.raw = {**cfg.raw, "traders_log": {**cfg.raw["traders_log"], "enabled": enabled},
               "persona": {"enabled": False, "version": 1}}
    cfg.mock, cfg.offline = False, True
    result = evening.run(cfg, run_date=D, force=True)
    return result, sent


def _seed_plan():
    con = ledger.connect()
    ledger.save_daily_plan(con, D, {
        "no_trade": False, "market_stance": "t", "rejected": [], "picks": [{
            "ticker": "TEST", "direction": "long", "setup_type": "gap_go", "conviction": 3,
            "size_r": 0.25, "entry_plan": {"type": "open", "price": 100.0}, "entry_price": 100.0,
            "stop_price": 98.0, "target_price": 104.0}]}, {"candidates": []}, "chop")
    con.close()


def test_evening_hook_off_writes_no_log_and_sends_the_old_email(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "e.duckdb")
    playbook(tmp_path, monkeypatch)
    _seed_plan()
    result, sent = _evening(monkeypatch, enabled=False)
    con = ledger.connect()
    try:
        assert con.execute("SELECT COUNT(*) FROM traders_log").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM runs WHERE loop = 'traders_log'").fetchone()[0] == 0
    finally:
        con.close()
    assert result is None and len(sent) == 1 and "Trader's log" not in sent[0]


def test_evening_hook_on_stores_the_log_and_emails_it(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "e.duckdb")
    playbook(tmp_path, monkeypatch)
    _seed_plan()
    result, sent = _evening(monkeypatch, enabled=True)
    con = ledger.connect()
    try:
        status = con.execute("SELECT status FROM traders_log WHERE session_date = ?", [D]).fetchone()[0]
        lock = con.execute("SELECT status FROM run_locks WHERE run_type = 'evening'").fetchone()[0]
    finally:
        con.close()
    assert status == "ok" and lock == "complete"
    assert result is None and "<h3>Trader's log</h3>" in sent[0]


def test_a_raising_log_never_costs_the_evening(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "e.duckdb")
    playbook(tmp_path, monkeypatch)
    _seed_plan()

    def boom(*a, **k):
        raise RuntimeError("log exploded")

    result, sent = _evening(monkeypatch, enabled=True, generate=boom)
    con = ledger.connect()
    try:
        lock = con.execute("SELECT status FROM run_locks WHERE run_type = 'evening'").fetchone()[0]
        ev = con.execute("SELECT status FROM runs WHERE loop = 'evening'").fetchone()[0]
    finally:
        con.close()
    assert result is None and lock == "complete" and ev in ("ok", "degraded")
    assert len(sent) == 1 and "Trader's log" not in sent[0]


# ------------------------------------------------------------ CLI and table

def test_table_is_browsable():
    import api.index as api
    assert api._QUERYABLE_TABLES["traders_log"] == "session_date"


def test_cli_refuses_an_ungraded_session_and_shows_a_stored_one(con, monkeypatch, capsys):
    import run
    seed_session(con)
    con.close()
    cfg = load_config()
    cfg.mock = False

    class A:
        show = False

    with pytest.raises(SystemExit):
        run.cmd_traders_log(cfg, A(), D)
    c = ledger.connect()
    tl.generate(c, _cfg(), D, llm_client=_Stub(CLEAN))
    c.close()
    A.show = True
    run.cmd_traders_log(cfg, A(), D)
    out = capsys.readouterr().out
    assert "status=ok" in out and "## What happened" in out
