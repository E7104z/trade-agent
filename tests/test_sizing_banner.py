"""
Phase 3: the Sizing Policy provisional-status banner.

Several sizing constraints still carry `[UNVALIDATED prior]` -- defaults
carried from inception that have never been tested. They sit in the section
governing how much money each trade risks, which makes them the most
consequential untested content in the document, and the bare tag is easy to
read past as though it were a measured limit.

The spec ties this phase to Phase 0 explicitly: do not ship it until the
duplicate-write bug is fixed, and confirm the banner does not itself get
duplicated. Both are pinned here.
"""
from __future__ import annotations

from agent.loops.weekly import _check_sizing_banner, _sizing_policy_section

BANNER = ("> **Note:** rules tagged `[UNVALIDATED prior]` below are defaults\n"
          "> carried from inception, not derived limits. Treat as conservative\n"
          "> guesses, not risk-proven constraints, until they accumulate\n"
          "> confirming observations.")


def _policy(body: str) -> str:
    return f"## Core Principles\n- x\n\n## Sizing Policy\n\n{body}\n\n## Setup Rules\n- y\n"


def test_missing_banner_is_flagged_while_unvalidated_rules_remain():
    md = _policy("- Losing streak >= 3: cap 0.5R. [UNVALIDATED prior]")
    violations = _check_sizing_banner(md)
    assert len(violations) == 1
    assert "missing" in violations[0]


def test_present_banner_passes():
    md = _policy(BANNER + "\n\n- Losing streak >= 3: cap 0.5R. [UNVALIDATED prior]")
    assert _check_sizing_banner(md) == []


def test_duplicate_banner_is_flagged():
    """The Changelog shipped twice for exactly this reason -- a section the
    miner writes AND the template supplies. The banner is checked for
    duplication specifically because this document has already done it once."""
    md = _policy(BANNER + "\n\n" + BANNER + "\n\n- cap 0.5R. [UNVALIDATED prior]")
    violations = _check_sizing_banner(md)
    assert len(violations) == 1
    assert "2 times" in violations[0]


def test_banner_not_required_once_nothing_is_unvalidated():
    """If every sizing rule earns its evidence the warning should disappear,
    not become furniture nobody reads."""
    md = _policy("- Losing streak >= 3: cap 0.5R. [confidence: moderate, n=30]")
    assert _check_sizing_banner(md) == []


def test_line_wrapping_does_not_defeat_the_check():
    """Regression: the banner is a blockquote and wraps, so the phrase is
    split across a newline and a '> ' marker. Matching raw text reported the
    banner missing while it sat directly above the rules it described -- a
    checker that fires on correct input is worse than no checker."""
    wrapped = ("> **Note:** rules tagged `[UNVALIDATED prior]` below are\n"
               "> defaults carried from inception, not derived limits. Treat\n"
               "> as conservative guesses.")
    md = _policy(wrapped + "\n\n- cap 0.5R. [UNVALIDATED prior]")
    assert _check_sizing_banner(md) == []


def test_section_extraction_stops_at_the_next_heading():
    """An [UNVALIDATED prior] tag in a LATER section must not make the Sizing
    Policy banner look required."""
    md = ("## Sizing Policy\n- all validated [n=30]\n\n"
          "## Setup Rules\n- gap_go [UNVALIDATED prior]\n")
    section = _sizing_policy_section(md)
    assert "gap_go" not in section
    assert _check_sizing_banner(md) == []


def test_the_live_playbook_satisfies_the_check():
    """The shipped document must actually pass, not just synthetic input."""
    pb = open("playbook.md", encoding="utf-8").read()
    assert "[UNVALIDATED prior]" in pb, "fixture assumption: priors still exist"
    assert _check_sizing_banner(pb) == []
