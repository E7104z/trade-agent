"""Shared presentation helpers for the two dashboards.

The static dashboard (agent/delivery.py) and the hosted dashboard
(api/index.py) both render the same conclusions from the same ledger. Keeping
those derivations here means the two surfaces cannot drift apart: a playbook
rule, the current best edge, and the selection-edge history are rendered
identically wherever they appear.

Nothing here writes to the ledger. Every function is either a pure string
transform or a read-only aggregation, so a rendering bug can never corrupt
state.
"""
from __future__ import annotations

import html as _html
import re
from typing import Any

from . import ledger

__all__ = ["playbook_bullets", "playbook_html", "best_edge", "conclusions_payload"]


# ------------------------------------------------------------------- playbook

_BULLET_LEAD = re.compile(r"^\s*[-*\u2022]\s+")
_INLINE = [
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"__([^_]+)__"), r"<strong>\1</strong>"),
    (re.compile(r"\*([^*\n]+)\*"), r"<em>\1</em>"),
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
]
_HR = re.compile(r"^-{3,}$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^[-*\u2022]\s+(.*)$")
_NUMBERED = re.compile(r"^\d+[.)]\s+(.*)$")
_SEP = re.compile(r"^\s*:?-{2,}:?\s*$")


def _inline(text: str) -> str:
    """HTML-escape a markdown inline span, then apply bold/italic/code. The
    escape runs first so a model-written playbook can never inject markup; the
    markdown patterns only add the safe tags (<strong>/<em>/<code>)."""
    text = _html.escape(text)
    for pat, repl in _INLINE:
        text = pat.sub(repl, text)
    return text


def playbook_bullets(markdown: str, limit: int = 4) -> list[str]:
    """The first `limit` top-level bullet lines, stripped of markdown — the
    mascot's talking points. Empty for a missing/empty playbook."""
    if not markdown:
        return []
    return [
        _BULLET_LEAD.sub("", ln).replace("**", "").strip()
        for ln in markdown.splitlines()
        if _BULLET_LEAD.match(ln)
    ][:limit]


def _table_html(lines: list[str]) -> str:
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in lines]
    header = rows[0]
    body = [r for r in rows[1:] if not all(_SEP.match(c) for c in r)]
    out = ["<table class='pb-table'><tr>"]
    out += [f"<th>{_inline(c)}</th>" for c in header]
    out.append("</tr>")
    for r in body:
        out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
    out.append("</table>")
    return "".join(out)


def playbook_html(markdown: str) -> str:
    """Render the markdown playbook to safe, structured HTML.

    A deliberately small subset of markdown: headings, bullet/numbered lists,
    bold/italic/inline-code, horizontal rules, and pipe tables. Everything is
    HTML-escaped first, so a model-written playbook can never inject markup.
    HTML comments (the `<!-- version: N -->` header) are dropped.
    """
    if not markdown:
        return "<p class='neutral'>No playbook yet.</p>"
    lines = markdown.splitlines()
    out: list[str] = []
    list_tag: str | None = None
    i = 0

    def close_list() -> None:
        nonlocal list_tag
        if list_tag is not None:
            out.append(f"</{list_tag}>")
            list_tag = None

    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        i += 1

        if not stripped or stripped.startswith("<!--"):
            close_list()
            continue
        if _HR.fullmatch(stripped):
            close_list()
            out.append("<hr class='pb-hr'/>")
            continue

        m = _HEADING.match(stripped)
        if m:
            close_list()
            level = min(len(m.group(1)) + 1, 6)  # '#' -> h2, '##' -> h3, ...
            out.append(f"<h{level} class='pb-h'>{_inline(m.group(2))}</h{level}>")
            continue

        m = _BULLET.match(stripped)
        if m:
            if list_tag != "ul":
                close_list()
                list_tag = "ul"
                out.append("<ul class='pb-list'>")
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue

        m = _NUMBERED.match(stripped)
        if m:
            if list_tag != "ol":
                close_list()
                list_tag = "ol"
                out.append("<ol class='pb-list'>")
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue

        if stripped.startswith("|"):
            close_list()
            block = [stripped]
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i].strip())
                i += 1
            out.append(_table_html(block))
            continue

        close_list()
        out.append(f"<p class='pb-p'>{_inline(stripped)}</p>")

    close_list()
    return "\n".join(out)


# ------------------------------------------------------------------ best edge

def best_edge(setup_stats: list[dict]) -> str:
    """Deterministic "current best edge": the setup with the best realized avg R
    over at least 3 triggered trades. The LLM never derives this number."""
    candidates = [
        s for s in setup_stats
        if (s.get("triggered") or 0) >= 3 and s.get("avg_r") is not None
    ]
    if not candidates:
        return ""
    top = max(candidates, key=lambda s: s["avg_r"])
    return f"{top['dim']} \u00b7 avg {top['avg_r']}R over {top['triggered']} triggered"


# ----------------------------------------------------------------- conclusions

def conclusions_payload(con: Any) -> dict:
    """Everything the "Conclusions & Changes" section needs, read-only.

    Each sub-aggregate is individually guarded: a missing older hosted table
    (or any single failure) must not take down the whole status endpoint the
    rest of the desk depends on.
    """
    def _guard(fn, default):
        try:
            return fn()
        except Exception:
            return default

    setup_stats = _guard(lambda: ledger.stats_by(con, "setup_type"), [])
    return {
        "best_edge": best_edge(setup_stats),
        "lessons": _guard(lambda: ledger.active_lessons(con, 25), []),
        "open_questions": _guard(lambda: ledger.question_progress_totals(con), []),
        "shadow": _guard(lambda: ledger.shadow_summary(con, 20), {}),
        "calibration": _guard(lambda: ledger.calibration_aggregate(con), []),
        "management": _guard(lambda: ledger.management_quality(con), {}),
        "selection_edge": _guard(lambda: ledger.selection_edge_history(con, 26), []),
    }
