"""
The runner learning loop's last link: research -> playbook -> selector.

Every other piece of the runner lane was built and tested in isolation --
detection, grading, the research block, the selector prompt. They were never
connected. `integrity.weekly_block` computed a full `## Runner lane` block of
setup edge, selection edge and FDR-corrected dimensions every week; the weekly
miner was never told to write any of it into the playbook; and the selector,
looking in the playbook for a runner heading that nothing produced, fell back
to "(no validated runner rules yet)" every single session. The loop ran,
graded, mined, and taught the selector nothing.

These tests pin the connection itself rather than the pieces: that the miner
is instructed to write the section, that the selector's extractor and the
write-time checker are the SAME function (so they cannot disagree about what
counts as a section), and that low-n runner numbers are suppressed before they
reach the one consumer that acts on them the same day.
"""
from __future__ import annotations

import io
import re

import pytest

from agent.loops.weekly import _RUNNER_MIN_N, _check_runner_section
from agent.runner_selection import NO_RUNNER_RULES, runner_playbook_section


@pytest.fixture()
def isolated_playbook(tmp_path, monkeypatch):
    """A ledger AND a playbook file that are both throwaway.

    `write_playbook` writes the DB *and* the file, and the file path is a
    module constant -- so pointing only `ledger.DB_PATH` at a tmp database
    still lets a test overwrite the desk's real playbook.md and archive the
    genuine version into playbook_history/. That happened while writing these
    tests: a passing run silently replaced the live v5 playbook with fixture
    text, and the next weekly run then mined against it.
    """
    from agent import ledger, memory

    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "t.duckdb"))
    monkeypatch.setattr(memory, "PLAYBOOK_PATH", tmp_path / "playbook.md")
    monkeypatch.setattr(memory, "PLAYBOOK_HISTORY_DIR", tmp_path / "history")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    con = ledger.connect()
    yield con
    con.close()


def _doc(*sections: str) -> str:
    return "# Trading Agent Playbook\n\n" + "\n\n".join(sections)


def _prompt() -> str:
    return io.open("prompts/weekly_miner.md", encoding="utf-8").read()


# --------------------------------------------------- the miner is told to

def test_the_weekly_prompt_instructs_a_runner_section():
    """The break was not a bug in any function -- it was an instruction that
    was never written. If this leaves the prompt, the miner stops emitting the
    heading and the selector silently reverts to the placeholder, with nothing
    failing anywhere."""
    prompt = _prompt()
    assert "## Runner Lane" in prompt
    assert "dto_runner" in prompt
    # and it must be in the skeleton the miner reproduces, not only in prose
    skeleton = prompt[prompt.index("Keep the v3 skeleton exactly"):]
    assert "## Runner Lane" in skeleton


def test_the_prompt_forbids_inventing_rules_below_the_floor():
    part = _prompt()
    part = part[part.index("## 3c."):part.index("## 4.")]
    assert "no validated runner rules yet" in part
    assert "underpowered" in part.lower()


# ------------------------------------------------------------ the extractor

def test_a_runner_heading_is_found_and_stops_at_the_next_heading():
    doc = _doc("## Core Principles\nunrelated",
               "## Runner Lane\nthe runner content",
               "## Shadow Ledger Summary\nalso unrelated")
    got = runner_playbook_section(doc)
    assert got.startswith("## Runner Lane")
    assert "the runner content" in got
    assert "Shadow Ledger" not in got
    assert "unrelated" not in got


def test_no_runner_heading_yields_the_placeholder_not_the_whole_playbook():
    """Handing the selector an unrelated section is worse than handing it
    nothing: it may try to apply gap_go rules to a setup they were never
    about."""
    doc = _doc("## Core Principles\nlots of text about gap_go")
    assert runner_playbook_section(doc) == NO_RUNNER_RULES


def test_the_heading_match_is_not_fooled_by_the_word_mid_sentence():
    """Only a heading counts. A body line mentioning runners must not be
    mistaken for the start of the section."""
    doc = _doc("## Core Principles\nwe also trade runner setups sometimes")
    assert runner_playbook_section(doc) == NO_RUNNER_RULES


# ------------------------------------------------ checker/consumer agreement

def test_the_checker_and_the_selector_share_one_extractor():
    """The important property, stated directly: the checker must not have its
    own idea of what a runner section is. If it did, it could pass a document
    whose section the selector never finds -- reporting the loop closed while
    it stayed open, which is precisely the failure this work fixed."""
    import agent.loops.weekly as weekly
    src = io.open(weekly.__file__, encoding="utf-8").read()
    fn_src = src[src.index("def _check_runner_section"):]
    fn_src = fn_src[:fn_src.index("\ndef ")]
    assert "runner_playbook_section" in fn_src, (
        "the checker must call the selector's extractor, not reimplement it")
    assert not re.search(r're\.(match|search)\(r?"\^#', fn_src), (
        "the checker appears to parse headings itself")


def test_a_missing_section_is_reported_against_its_consumer():
    v = _check_runner_section(_doc("## Core Principles\nnothing here"))
    assert len(v) == 1
    assert "selector" in v[0]


def test_a_present_section_passes():
    doc = _doc("## Runner Lane\nno validated runner rules yet - do not assume any\n"
               "n=10 triggered candidates, selection edge underpowered.")
    assert _check_runner_section(doc) == []


# ---------------------------------------------------------- low-n suppression

def test_a_per_bucket_mean_below_the_floor_is_caught():
    """Same floor Calibration and Selection Edge already enforce. This table
    is shaped differently -- a text dimension in column one -- so neither of
    the existing row scanners could ever have seen it."""
    doc = _doc("## Runner Lane\n"
               "| dimension | bucket | n | mean R | q |\n"
               "|---|---|---|---|---|\n"
               "| runner_tc_bucket | 75-85 | 4 | 0.812 | 0.44 |")
    v = _check_runner_section(doc)
    assert len(v) == 1
    assert "n=4" in v[0] and str(_RUNNER_MIN_N) in v[0]


def test_a_suppressed_row_below_the_floor_passes():
    doc = _doc("## Runner Lane\n| runner_tc_bucket | 75-85 | 4 | — | — |")
    assert _check_runner_section(doc) == []


def test_a_row_at_the_floor_may_show_its_mean():
    doc = _doc("## Runner Lane\n"
               f"| runner_vwap_bucket | 0-0.2% | {_RUNNER_MIN_N} | 0.812 | 0.04 * |")
    assert _check_runner_section(doc) == []


def test_the_scanner_ignores_the_other_tables():
    """It must not fire on Calibration (conviction 1-5 in column one) or
    Selection Edge (a date), both of which legitimately carry suppressed and
    unsuppressed rows of their own."""
    doc = _doc("## Runner Lane\n"
               "| 4 | 1 | 0% | -0.05 | insufficient data |\n"
               "| 2026-09-18 | 3 | — | — | insufficient data |")
    assert _check_runner_section(doc) == []


# --------------------------------------------------------------- contradiction

def test_declaring_no_rules_while_carrying_a_rule_tag_is_flagged():
    """Both statements cannot be true, and the selector has no way to decide
    which of them to trade on."""
    doc = _doc("## Runner Lane\n"
               "no validated runner rules yet - do not assume any\n"
               "- prefer candidates above VWAP [rule: R00a1b2c3d4e5]")
    v = _check_runner_section(doc)
    assert any("cannot tell" in x for x in v)


def test_a_validated_rule_without_the_disclaimer_passes():
    doc = _doc("## Runner Lane\n"
               "- prefer candidates above VWAP [rule: R00a1b2c3d4e5] "
               "[confidence: low, n=11, last_confirmed 2026-09-18]")
    assert _check_runner_section(doc) == []


# ----------------------------------------------- end to end through the ledger

def test_a_written_playbook_reaches_the_selector_prompt(isolated_playbook):
    """The whole point, end to end: a playbook containing a Runner Lane must
    arrive in the selector's system prompt in place of the placeholder."""
    from agent.memory import write_playbook
    from agent.runner_selection import _playbook_section

    con = isolated_playbook
    assert _playbook_section(con) == NO_RUNNER_RULES, "empty ledger -> placeholder"

    write_playbook(_doc(
        "## Core Principles\nirrelevant",
        "## Runner Lane\nselection edge underpowered: 4 selected / 6 unselected "
        "across 1 contested session.",
        "## Retired Rules (graveyard)\nnothing"), "test", con)

    got = _playbook_section(con)
    assert got.startswith("## Runner Lane")
    assert "underpowered" in got
    assert "irrelevant" not in got and "graveyard" not in got


def test_the_section_lands_in_the_assembled_selector_prompt(isolated_playbook):
    """The last inch. Everything above proves the section is extractable; this
    proves it is actually substituted into the system prompt the selector is
    called with, rather than extracted into a variable nothing renders."""
    from agent.llm import load_prompt
    from agent.memory import write_playbook
    from agent.runner_selection import _playbook_section

    con = isolated_playbook
    marker = "selected candidates did not beat unselected ones"
    write_playbook(_doc(f"## Runner Lane\n{marker}"), "test", con)

    system = load_prompt("runner_selector", {
        "MAX_PICKS": "10",
        "REGIME_TAG": "neutral",
        "PLAYBOOK_SECTION": _playbook_section(con),
        "CANDIDATE_TABLE": "| ticker |\n|---|\n| AAAA |",
    })
    assert marker in system
    assert "{{PLAYBOOK_SECTION}}" not in system


def test_the_placeholder_is_what_an_empty_ledger_sends(isolated_playbook):
    """And the honest negative: with no runner section written, the selector
    is told plainly that no rules exist -- never left to infer from silence."""
    from agent.llm import load_prompt
    from agent.runner_selection import _playbook_section

    con = isolated_playbook
    system = load_prompt("runner_selector", {
        "MAX_PICKS": "10", "REGIME_TAG": "neutral",
        "PLAYBOOK_SECTION": _playbook_section(con),
        "CANDIDATE_TABLE": "| ticker |\n|---|\n| AAAA |",
    })
    assert "no validated runner rules yet" in system
