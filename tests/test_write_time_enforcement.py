"""
WRITE_TIME_ENFORCEMENT_SPEC_v1 — integrity checks the playbook cannot ignore.

The weekly checkers used to print a WARN line and write the document anyway.
Pinned here: every blocking shape is repaired at the line/row/section level,
the repaired document re-checks clean, prose that merely mentions edge is not
mistaken for a claim, the definitions section is system-owned, a document
that cannot be repaired is not written, and every finding is persisted.
"""
from __future__ import annotations

import io
import json
from datetime import date

import pytest

from agent import ledger, memory, playbook_enforcement as pe
from agent.loops import weekly
from agent.runner_selection import NO_RUNNER_RULES, runner_playbook_section

_BANNER = pe.SIZING_BANNER


def _doc(best_edge="**NO VALIDATED EDGE YET.**", setup="- gap_go: hold above VWAP. [confidence: none, n=0, UNVALIDATED prior]",
         runner="**(no validated runner rules yet — do not assume any)**\n\nSetup edge: n=3, underpowered.",
         calibration="| 5 | 0 | — | — | insufficient data |", sizing=_BANNER,
         questions="1. **gap_fade** — Threshold: n >= 8. **Progress: 1/8.**",
         reading="- whatever the miner wrote"):
    return f"""# Trading Agent Playbook
<!-- version: 6 -->

## Reading This File (for the morning agent)

{reading}

## Current Best Edge

{best_edge}

## Core Principles

1. Edge decays by default.

## Sizing Policy

{sizing}

- Losing streak >= 3: cap 0.5R. [UNVALIDATED prior]

## Setup Rules

{setup}

## Regime Rules

- chop: halve sizes. [n=0, UNVALIDATED]

## Calibration

| Conviction | n | realized WR | realized avgR | verdict |
|-----------:|--:|:-----------:|:-------------:|:--------|
{calibration}

## Runner Lane

{runner}

## Shadow Ledger Summary

- Net forgone R: +1.0R

## Open Questions (pre-registered experiments)

{questions}

## Retired Rules (graveyard)

_None retired yet._
"""


def _blocks(res):
    return [v for v in res.violations if v.severity == "block"]


def _clean(md, states=()):
    return [v for v in pe.collect(md, states) if v.severity == "block"] == []


# ------------------------------------------------------------ clean input

def test_a_well_formed_document_passes_with_no_blocks():
    res = pe.enforce(_doc())
    assert res.markdown is not None
    assert _blocks(res) == []


def test_negated_edge_is_not_a_claim():
    res = pe.enforce(_doc(best_edge="- NO VALIDATED EDGE YET — gap_fade is a candidate, not edge."))
    assert _blocks(res) == []
    assert "gap_fade is a candidate, not edge" in res.markdown


def test_an_edge_mention_outside_rule_sections_is_a_warning_and_kept():
    q = "- Does the failed-bounce entry beat open-print entries? Threshold: n >= 8. Progress: 1/8."
    res = pe.enforce(_doc(questions=q))
    assert _blocks(res) == []
    assert any(v.checker == "provenance" and v.severity == "warn" for v in res.violations)
    assert q in res.markdown


# ------------------------------------------------------------ line repairs

def test_untagged_edge_claim_in_setup_rules_is_withheld():
    bad = "- gap_go: this setup has edge in uptrends, beats the baseline."
    res = pe.enforce(_doc(setup=bad))
    assert res.markdown is not None
    assert bad not in res.markdown
    # The redaction names what it redacted and the status it leaves behind.
    assert "- gap_go: [withheld by integrity check: provenance" in res.markdown
    assert "treat gap_go as an UNVALIDATED prior" in res.markdown
    v = next(v for v in res.violations if v.checker == "provenance")
    assert v.severity == "block" and v.action == "withheld" and v.line == bad
    assert _clean(res.markdown)


def test_small_n_win_rate_is_withheld():
    bad = "- gap_go: n=3, wr=67%, avgR=+0.9, t=1.1, stdR=0.8"
    res = pe.enforce(_doc(setup=bad))
    assert bad not in res.markdown
    assert any(v.checker == "wr_suppression" and v.action == "withheld" for v in res.violations)
    assert _clean(res.markdown)


def test_calibration_row_below_floor_is_suppressed_not_deleted():
    res = pe.enforce(_doc(calibration="| 4 | 1 | 0% | -0.05 | loses money |"))
    assert "| 4 | 1 | — | — | insufficient data |" in res.markdown
    assert "-0.05" not in res.markdown
    assert _clean(res.markdown)


def test_selection_edge_row_below_floor_is_suppressed():
    table = ("## Selection Edge\n\n| Week | n (triggered) | Selection edge (R vs. baseline) | "
             "% of random portfolios beaten | verdict |\n|-----:|--:|--:|--:|:--|\n"
             "| 2026-09-18 | 3 | -1.20R | 12.6% | no detectable selection edge |\n")
    md = _doc().replace("## Runner Lane", table + "\n## Runner Lane")
    res = pe.enforce(md)
    assert "| 2026-09-18 | 3 | — | — | insufficient data |" in res.markdown
    assert "-1.20R" not in res.markdown
    assert _clean(res.markdown)


# --------------------------------------------------------- section repairs

@pytest.mark.parametrize("best", [
    "Best edge: gap_go longs, mean -0.77R over the week.",
    "The bar for calling anything edge: (a) n >= 8, (b) beats baseline, (c) t >= 1.5.",
    "- gap_go longs in uptrend beat the baseline, our best edge.",
])
def test_a_bad_current_best_edge_is_rebuilt_from_the_registry(best):
    states = [{"rule_id": "Rabc123def456", "state": "QUARANTINED",
               "statement": "gap_go longs in uptrend beat the baseline",
               "forward_n": 3, "forward_n_required": 8}]
    res = pe.enforce(_doc(best_edge=best), states)
    assert res.markdown is not None
    assert best not in res.markdown
    assert "System-rendered" in res.markdown
    assert "NO VALIDATED EDGE YET" in res.markdown
    assert "Rabc123def456 (candidate, not edge)" in res.markdown
    assert _clean(res.markdown, states)


def test_a_validated_rule_is_named_with_its_tag_when_rebuilt():
    states = [{"rule_id": "Rfeedfacecafe", "state": "VALIDATED", "statement": "gap_fade shorts",
               "forward_n": 9, "forward_n_required": 8}]
    res = pe.enforce(_doc(best_edge="Best: mean -0.2R"), states)
    assert "[rule: Rfeedfacecafe]" in res.markdown
    assert _clean(res.markdown, states)


def test_a_runner_section_with_a_small_n_mean_becomes_the_explicit_no_rules_block():
    bad = ("| dimension | bucket | n | mean R | q |\n|---|---|---|---|---|\n"
           "| runner_tc_bucket | 75-85 | 4 | 0.812 | 0.4400 |")
    runner = {"runner_setup_edge": {"n": 40, "underpowered": False},
              "runner_selection_edge": {"n_selected": 5, "n_unselected": 30,
                                        "sessions_contested": 2,
                                        "verdict": "underpowered - 5 selected / 30 unselected"}}
    res = pe.enforce(_doc(runner=bad), [], runner)
    section = runner_playbook_section(res.markdown)
    assert section.startswith("## Runner Lane")
    assert NO_RUNNER_RULES in section
    assert "0.812" not in section
    assert "n=40 triggered candidates" in section
    assert weekly._check_runner_section(res.markdown) == []


def test_a_missing_runner_section_is_inserted_before_the_shadow_ledger():
    md = _doc()
    start = md.index("## Runner Lane")
    md = md[:start] + md[md.index("## Shadow Ledger Summary"):]
    res = pe.enforce(md)
    assert res.markdown.index("## Runner Lane") < res.markdown.index("## Shadow Ledger Summary")
    assert NO_RUNNER_RULES in runner_playbook_section(res.markdown)


def test_sizing_banner_is_inserted_or_deduplicated():
    missing = pe.enforce(_doc(sizing=""))
    assert missing.markdown.count("defaults carried from inception") == 1
    assert any(v.checker == "sizing_banner" and v.action == "inserted" for v in missing.violations)

    doubled = pe.enforce(_doc(sizing=_BANNER + "\n\n" + _BANNER))
    assert doubled.markdown.count("defaults carried from inception") == 1
    assert any(v.action == "deduplicated" for v in doubled.violations)


def test_reading_section_is_system_owned():
    res = pe.enforce(_doc(reading="- Edge means t >= 1.5 and n >= 8."))
    assert "t >= 1.5" not in res.markdown
    assert "System-owned section" in res.markdown
    assert "**Edge** = a rule in state VALIDATED" in res.markdown


def test_a_missing_reading_section_is_added():
    md = _doc().replace("## Reading This File (for the morning agent)\n\n- whatever the miner wrote\n", "")
    res = pe.enforce(md)
    assert "## Reading This File" in res.markdown


def test_a_miner_written_changelog_is_stripped_before_checking():
    md = _doc() + "\n## Changelog (v6)\n\n- Current Best Edge: t >= 1.5 bar kept\n"
    res = pe.enforce(md)
    assert "## Changelog" not in res.markdown
    assert not any(v.checker == "edge_bar" for v in res.violations)


# ------------------------------------------------------------ fallback

def test_an_unrepairable_document_is_rejected(monkeypatch):
    stuck = pe.Violation("provenance", "block", "setup rules", "x", "cannot repair")
    monkeypatch.setattr(pe, "collect", lambda md, states=None, stale=None, **kw: [stuck])
    res = pe.enforce(_doc())
    assert res.markdown is None
    assert res.remaining_blocks == [stuck]
    assert res.violations[-1].action == "rewrite_rejected"


# ------------------------------------------------------ citation rule

def test_an_effect_claim_may_cite_q_instead_of_t():
    ok = "- gap_go: n=20, avgR=+0.4, stdR=1.1, q=0.04"
    assert weekly._check_wr_suppression(ok) == []
    missing = weekly._check_wr_suppression("- gap_go: n=20, avgR=+0.4, stdR=1.1")
    assert missing and "q (or t)" in missing[0]


def test_the_email_reports_the_findings():
    from agent.delivery import weekly_email_html
    html = weekly_email_html({"summary_for_email": "s"}, [
        {"checker": "provenance", "severity": "block", "action": "withheld",
         "message": "edge claim with no [rule:] provenance tag"}])
    assert "Write-time integrity" in html
    assert "repaired before the playbook was written" in html


# ------------------------------------------- the real weekly loop

class _StubResult:
    def __init__(self, parsed):
        self.text = json.dumps(parsed)
        self.parsed = parsed
        self.usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0, "cost_usd": 0.0}


def _payload():
    raw = json.load(io.open("mocks/weekly_miner.json", encoding="utf-8"))["text"]
    return json.loads(raw[raw.index("{"):raw.rindex("}") + 1])


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "w.duckdb")
    monkeypatch.setattr(memory, "PLAYBOOK_PATH", tmp_path / "playbook.md")
    monkeypatch.setattr(memory, "PLAYBOOK_HISTORY_DIR", tmp_path / "history")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(weekly, "send_email", lambda *a, **k: None)
    monkeypatch.setattr(weekly, "render_dashboard", lambda *a, **k: None)
    monkeypatch.setattr(weekly, "_git_commit_playbook", lambda *a, **k: None)
    from agent.config import load_config
    cfg = load_config()
    cfg.mock = False
    cfg.offline = True

    def _use(payload):
        class _LLM:
            def __init__(self, cfg):
                pass

            def call(self, **kw):
                return _StubResult(payload)
        monkeypatch.setattr(weekly, "LLMClient", _LLM)
    return cfg, _use


def test_weekly_run_persists_findings_and_writes_a_clean_document(wired):
    cfg, use = wired
    payload = _payload()
    use(payload)
    weekly.run(cfg, run_date=date(2026, 9, 25))

    con = ledger.connect()
    text, version = memory.read_playbook(con)
    rows = con.execute("SELECT checker, severity, action, version FROM playbook_violations").fetchall()
    summary = ledger.latest_playbook_violations(con)
    from agent.config import load_config
    from agent.health import loop_health
    health = loop_health(con, load_config())
    con.close()

    # The fixture carries setup rules quoting WR/avgR at n<8: they must be
    # recorded AND kept out of the written document.
    assert any(r[1] == "block" and r[2] == "withheld" for r in rows)
    assert all(str(r[3]) == str(version) for r in rows)
    assert [v for v in pe.collect(text.split("## Changelog")[0]) if v.severity == "block"] == []
    assert summary["block"] > 0 and not summary["rejected"]
    weekly_stage = next(s for s in health["stages"] if s["key"] == "weekly")
    assert weekly_stage["status"] == "warn"
    assert "repaired before" in weekly_stage["detail"]


def test_a_rejected_rewrite_keeps_the_prior_version(wired, monkeypatch):
    cfg, use = wired
    use(_payload())
    con = ledger.connect()
    _before, v_before = memory.read_playbook(con)
    con.close()

    stuck = pe.Violation("provenance", "block", "setup rules", "x", "cannot repair")
    monkeypatch.setattr(pe, "collect", lambda md, states=None, stale=None, **kw: [stuck])
    weekly.run(cfg, run_date=date(2026, 9, 25))

    con = ledger.connect()
    _after, v_after = memory.read_playbook(con)
    summary = ledger.latest_playbook_violations(con)
    con.close()
    assert v_after == v_before
    assert summary["rejected"] is True
