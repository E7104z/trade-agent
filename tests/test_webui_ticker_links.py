"""Guard: every ticker in every structured table must route through
tickerLink() (specs/CHART_DRAWER_SPEC_v1.md §4.4).

Scans the fully-assembled page (mascot + chart drawer injected, exactly as
served) for `esc(<expr>.ticker)` / `esc(<expr>.symbol)` -- the pattern a new
table would use if it printed a raw ticker instead of linking it. Any match
must appear on the ALLOWED_LINES allow-list (Arby's own internals, or a
documented exception), otherwise a new table just broke "every ticker,
always" and CI should say so.
"""
from __future__ import annotations

import re

from agent.chartui import CHART_MARKER, chart_embed
from agent.mascot import MASCOT_MARKER, mascot_embed
from agent.webui import PAGE

_PATTERN = re.compile(r"esc\(\s*[\w.]+\.(ticker|symbol)\s*\)")

# Nothing is on this list today -- it exists so a genuinely new, deliberate
# exception (e.g. a future Arby-internal ticker readout) has somewhere to go
# without weakening the regex. Entries are exact source lines.
ALLOWED_LINES: set[str] = set()


def _assembled_page() -> str:
    page = PAGE.replace(MASCOT_MARKER, mascot_embed())
    page = page.replace(CHART_MARKER, chart_embed())
    return page


def test_no_raw_ticker_or_symbol_escape_outside_tickerlink():
    page = _assembled_page()
    offenders = []
    for lineno, line in enumerate(page.splitlines(), start=1):
        for m in _PATTERN.finditer(line):
            if line.strip() in ALLOWED_LINES:
                continue
            offenders.append(f"line {lineno}: {line.strip()[:160]}")
    assert not offenders, (
        "raw ticker/symbol rendered outside tickerLink() -- route it through "
        "tickerLink(ticker, ctx) instead (see agent/webui.py):\n" + "\n".join(offenders)
    )


def test_ticker_link_helper_is_defined_and_used():
    page = _assembled_page()
    assert "function tickerLink(" in page
    assert page.count("tickerLink(") >= 6  # definition + every wired site


def test_chart_marker_and_drawer_are_present_in_served_page():
    page = _assembled_page()
    assert CHART_MARKER not in page  # marker was replaced, not left dangling
    # The drawer's DOM is built lazily on first open (mirrors Arby), so the
    # static page carries the builder script, not the DOM itself.
    assert "wrap.id = 'chart-drawer'" in page
    assert "window.TA.openChart = openChart" in page
