"""
Write-time enforcement for the weekly playbook rewrite (WRITE_TIME_ENFORCEMENT_SPEC_v1).

WHY THIS MODULE EXISTS
----------------------
The weekly loop ran eight write-time checkers over every rewrite and then
wrote the document anyway: each checker only printed a WARN line, and in
production those lines live in Vercel runtime logs for minutes. The morning
analyst and the intraday runner selector then acted on whatever was written.
A check whose finding cannot change the output, and cannot be read back, is
not a check.

WHAT IT DOES
------------
1. DETECTION IS NOT RE-IMPLEMENTED. The existing checkers stay the single
   source of what counts as a violation. To locate an offender, a checker is
   called on one line (or one section) at a time -- the same function, on a
   smaller input -- so this module and the checkers can never disagree.
2. REPAIR IS LINE-LEVEL, NOT DOCUMENT-LEVEL. Refusing the whole rewrite would
   freeze the playbook on any single defect and trade daily learning for
   integrity. So only the offending line is withheld (or the table row
   suppressed, or the section re-rendered by the system). The withheld text
   is kept in `playbook_violations`, never lost.
3. NO LLM REPAIRS ANYTHING. Replacements are fixed strings or deterministic
   renders from the research layer's own state.
4. RE-CHECK. The repaired document must produce zero blocking violations.
   If it cannot be repaired to zero, the caller keeps the prior version and
   records `rewrite_rejected` -- the only whole-document fallback.

WRITER ISOLATION
----------------
Pure text in, text out. The weekly loop is still the only caller and still
the only writer of the playbook.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

__all__ = [
    "Violation",
    "EnforcementResult",
    "READING_TEMPLATE",
    "SIZING_BANNER",
    "enforce",
    "collect",
]

# Sections where a line states a rule the desk may trade on. The provenance
# rule ("only VALIDATED rules, each with its [rule: R...] tag") BLOCKS here and
# only here; elsewhere a sentence that merely mentions edge is prose and is
# recorded as a warning.
#
# Runner Lane is deliberately absent: the miner is instructed to REPORT the
# runner measurements there ("setup edge: n=..., selection edge: ..."), and the
# provenance regex treats the bare word "edge" as a claim. That section has its
# own dedicated, blocking checker (_check_runner_section: missing, small-n
# means, contradictory tags), which is the authority for it.
RULE_SECTIONS = ("current best edge", "setup rules", "regime rules")

# A line that says there is NO edge is not an edge claim.
_NEGATED_EDGE = re.compile(
    r"no\s+validated\s+edge|candidate,?\s+not\s+edge|not\s+(an?\s+)?edge|"
    r"\bno\s+(detectable\s+)?edge|absence\s+of\s+edge|no\s+detectable|"
    r"not\s+yet\s+edge|isn'?t\s+edge", re.I)

# The retired edge bar. |t| >= 1.5 is p ~= 0.13 two-sided over ~100 looks a
# week (agent/stats.py); edge is now a VALIDATED rule, defined in the
# system-owned Reading section.
_EDGE_BAR = re.compile(r"\bt\s*(>=|≥|=>)\s*1\.5\b", re.I)

SIZING_BANNER = (
    "> **Note:** rules tagged `[UNVALIDATED prior]` below are defaults carried "
    "from inception, not derived limits. Treat as conservative guesses, not "
    "risk-proven constraints, until they accumulate confirming observations.")

READING_TEMPLATE = """\
> System-owned section: rewritten by the weekly loop on every write, never by
> the miner, so the definitions below cannot drift between versions.

- **Edge** = a rule in state VALIDATED: discovered in the ledger, screened by
  the FDR-corrected weekly search, then confirmed on FORWARD observations
  recorded after its quarantine date. Only VALIDATED rules may be stated in
  Setup Rules, Regime Rules, Runner Lane or Current Best Edge, and every such
  line carries its `[rule: Rxxxxxxxxxxxx]` tag.
- **n** = number of REALIZED trades supporting the rule. This is ground truth.
- **last_confirmed** = session date of the most recent confirming observation.
  A rule with no confirming observation in >= 30 sessions is treated as
  UNVALIDATED even if n is high.
- **WR / avgR are shown ONLY at n >= 8.** Below that you get the raw trade list.
  Do not infer a win rate from a short list. A 1-for-1 rule is noise, not skill.
- **Significance** is computed by the system and already corrected for how many
  comparisons were searched (q-values). A raw t-statistic is not evidence of
  edge on its own.
- **[UNVALIDATED prior]** = a belief carried from inception with zero or
  sub-threshold evidence. You may use it as a weak default; you may not cite it
  as edge, and you must not size up on it.
- Confidence tiers: none (n=0) < provisional (1 <= n < 8) < low (8 <= n < 20)
  < moderate (20 <= n < 40) < established (n >= 40). No rule is "high."
- **Current Best Edge** is the desk's one-paragraph standing answer to "where is
  our edge right now" — hunt it first. If it says NO VALIDATED EDGE YET, the
  hunt is the provisional candidates it names.
"""

# Log labels the weekly loop has always printed, kept verbatim so the
# existing log-reading tests and humans grepping for them keep working.
LOG_LABEL = {
    "wr_suppression": "WR-suppression violation (n<8 with wr/avgR shown)",
    "wr_citation": "WR-suppression violation (n<8 with wr/avgR shown)",
    "calibration_table": "calibration-table suppression violation",
    "selection_edge_table": "selection-edge suppression violation",
    "sizing_banner": "sizing-policy banner violation",
    "runner_section": "runner-section violation",
    "best_edge": "best-edge mislabel",
    "edge_bar": "edge-bar violation",
    "open_questions": "open-question registry violation",
    "provenance": "playbook violation",
    "stale_setup_rule": "stale-rule violation",
    "rewrite_rejected": "rewrite rejected",
}


@dataclass
class Violation:
    checker: str
    severity: str            # "block" | "warn"
    section: str
    line: str
    message: str
    action: str = "logged"   # withheld | replaced | inserted | deduplicated | logged | rewrite_rejected

    def to_row(self) -> dict[str, Any]:
        return {"checker": self.checker, "severity": self.severity,
                "section": self.section[:200], "line": self.line[:1000],
                "message": self.message[:1000], "action": self.action}


@dataclass
class EnforcementResult:
    markdown: str | None                    # None = rewrite rejected; keep the prior version
    violations: list[Violation] = field(default_factory=list)
    remaining_blocks: list[Violation] = field(default_factory=list)

    @property
    def n_block(self) -> int:
        return sum(1 for v in self.violations if v.severity == "block")


# --------------------------------------------------------------------------
# Section model
# --------------------------------------------------------------------------

def _split(md: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """(preamble lines, [(h2 heading line, body lines), ...])."""
    pre: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in (md or "").splitlines():
        if line.startswith("## "):
            sections.append((line, []))
        elif sections:
            sections[-1][1].append(line)
        else:
            pre.append(line)
    return pre, sections


def _join(pre: list[str], sections: list[tuple[str, list[str]]]) -> str:
    out = list(pre)
    for heading, body in sections:
        out.append(heading)
        out.extend(body)
    text = "\n".join(out)
    return text if text.endswith("\n") else text + "\n"


def _name(heading: str) -> str:
    return heading[3:].strip().lower()


def _is_rule_section(heading: str) -> bool:
    return any(_name(heading).startswith(s) for s in RULE_SECTIONS)


def _withheld(checker: str, detail: str = "") -> str:
    return f"- [withheld by integrity check: {checker}{' — ' + detail if detail else ''}]"


# --------------------------------------------------------------------------
# Detection: the existing checkers, called on smaller inputs
# --------------------------------------------------------------------------

def _line_hits(heading: str, body: list[str], states: Sequence[dict]) -> list[tuple[int, Violation]]:
    """Per-line violations inside one section, located by index into `body`."""
    from .loops import weekly as wk
    from .research import check_playbook_provenance

    hits: list[tuple[int, Violation]] = []
    sec = _name(heading)
    rule_sec = _is_rule_section(heading)
    for i, line in enumerate(body):
        if not line.strip():
            continue
        for msg in wk._check_wr_suppression(line):
            citation = "[missing:" in msg
            hits.append((i, Violation("wr_citation" if citation else "wr_suppression",
                                      "warn" if citation else "block", sec, line, msg)))
        for msg in wk._check_calibration_table(line):
            hits.append((i, Violation("calibration_table", "block", sec, line, msg)))
        for msg in wk._check_selection_edge_table(line):
            hits.append((i, Violation("selection_edge_table", "block", sec, line, msg)))
        if _EDGE_BAR.search(line):
            hits.append((i, Violation(
                "edge_bar", "block" if rule_sec else "warn", sec, line,
                "states |t| >= 1.5 as the bar for edge; edge is a VALIDATED rule "
                "(forward-tested after an FDR-screened discovery)")))
        # Provenance keeps its section context: the checker switches on the
        # h2 it has seen, so it is given the heading plus the one line.
        for msg in check_playbook_provenance(f"{heading}\n{line}", states):
            negated = bool(_NEGATED_EDGE.search(line)) and msg.startswith("edge claim")
            block = rule_sec and not negated
            hits.append((i, Violation("provenance", "block" if block else "warn",
                                      sec, line, msg)))
    return hits


def collect(md: str, states: Sequence[dict] | None = None) -> list[Violation]:
    """Every violation in `md`, located, without changing anything."""
    from .loops import weekly as wk
    from . import open_questions

    states = list(states or [])
    pre, sections = _split(md)
    out: list[Violation] = []
    for heading, body in [("## (preamble)", pre)] + sections:
        out.extend(v for _, v in _line_hits(heading, body, states))

    for msg in wk._check_sizing_banner(md):
        out.append(Violation("sizing_banner", "warn", "sizing policy", "", msg))
    for msg in wk._check_runner_section(md):
        out.append(Violation("runner_section", "block", "runner lane", "", msg))
    for msg in wk._check_best_edge(md):
        out.append(Violation("best_edge", "block", "current best edge", "", msg))
    for msg in open_questions.check_playbook_questions(md):
        out.append(Violation("open_questions", "warn", "open questions", "", msg))
    return out


# --------------------------------------------------------------------------
# Repairs
# --------------------------------------------------------------------------

_CAL_ROW = re.compile(r"^(\|\s*[1-5]\s*\|\s*\d+\s*\|)")
_SE_ROW = re.compile(r"^(\|\s*\d{4}-\d{2}-\d{2}\s*\|\s*\d+\s*\|)")


def _suppress_row(line: str, prefix_re: re.Pattern, verdict: str) -> str:
    """Keep the key cells of a table row, blank the numbers below the floor."""
    m = prefix_re.match(line.strip())
    if not m:
        return _withheld("table row")
    return f"{m.group(1)} — | — | {verdict} |"


def _best_edge_block(states: Sequence[dict]) -> list[str]:
    validated = [s for s in states if s.get("state") == "VALIDATED"]
    quarantined = [s for s in states if s.get("state") == "QUARANTINED"]
    lines = [
        "",
        "> System-rendered: the miner's Current Best Edge failed a write-time",
        "> integrity check, so this section was rebuilt from the rule registry.",
        "> The withheld text is recorded in `playbook_violations`.",
        "",
    ]
    if validated:
        lines.append("Validated rules (forward-tested after quarantine):")
        lines.append("")
        for s in validated:
            lines.append(f"- [rule: {s['rule_id']}] {s['statement'][:200]} "
                         f"(forward n={s['forward_n']})")
    else:
        lines.append("**NO VALIDATED EDGE YET.** No rule has completed forward validation.")
    if quarantined:
        # Plain lines, not bullets, and each one says what it is: a rule the
        # miner phrased as "beats the baseline" is still only a candidate.
        lines += ["", "Leading candidates (QUARANTINED, forward-testing):", ""]
        for s in sorted(quarantined, key=lambda s: -(s.get("forward_n") or 0))[:5]:
            lines.append(f"{s['rule_id']} (candidate, not edge): {s['statement'][:160]} "
                         f"— forward n={s['forward_n']}/{s['forward_n_required']}  ")
    lines.append("")
    return lines


def _runner_block(runner: dict | None) -> list[str]:
    from .runner_selection import NO_RUNNER_RULES
    runner = runner or {}
    setup = runner.get("runner_setup_edge") or {}
    sel = runner.get("runner_selection_edge") or {}
    lines = [
        "",
        f"**{NO_RUNNER_RULES}**",
        "",
        "> System-rendered: the miner's Runner Lane section failed a write-time",
        "> integrity check (or was missing), so the selector is given this",
        "> explicit no-rules statement instead. The withheld text is recorded in",
        "> `playbook_violations`.",
        "",
        # Plain lines, not bullets: these are counts and the computed verdict,
        # stated as measurements, never as a rule to act on.
        f"Setup edge measurement: n={setup.get('n', 0)} triggered candidates under "
        f"the current scan/sim version"
        + (" (UNDERPOWERED)" if setup.get("underpowered") else "") + ".  ",
        f"Selection edge measurement: {sel.get('n_selected', 0)} selected / "
        f"{sel.get('n_unselected', 0)} unselected across "
        f"{sel.get('sessions_contested', 0)} contested session(s): "
        f"{sel.get('verdict', 'not computed')}.",
        "",
    ]
    return lines


def _ensure_reading(pre: list[str], sections: list[tuple[str, list[str]]]) -> None:
    body = [""] + READING_TEMPLATE.rstrip("\n").splitlines() + [""]
    for i, (heading, _old) in enumerate(sections):
        if _name(heading).startswith("reading this file"):
            sections[i] = (heading, body)
            return
    sections.insert(0, ("## Reading This File (for the morning agent)", body))


def _fix_banner(sections: list[tuple[str, list[str]]]) -> list[Violation]:
    """Insert or de-duplicate the Sizing Policy banner. Returns what it did."""
    from .loops import weekly as wk
    done: list[Violation] = []
    for i, (heading, body) in enumerate(sections):
        if not _name(heading).startswith("sizing policy"):
            continue
        text = "\n".join(body)
        flat = " ".join(text.replace(">", " ").split())
        count = flat.count(wk._SIZING_BANNER_PHRASE)
        if count == 1 or (count == 0 and "[UNVALIDATED prior]" not in text):
            return done
        # Remove every blockquote paragraph that carries the banner phrase,
        # then put exactly one back directly under the heading.
        kept: list[str] = []
        para: list[str] = []

        def _flush():
            p = " ".join(" ".join(para).replace(">", " ").split())
            if para and wk._SIZING_BANNER_PHRASE not in p:
                kept.extend(para)
            para.clear()

        for line in body:
            if line.lstrip().startswith(">"):
                para.append(line)
            else:
                _flush()
                kept.append(line)
        _flush()
        while kept and not kept[0].strip():
            kept.pop(0)
        sections[i] = (heading, ["", SIZING_BANNER, ""] + kept)
        done.append(Violation("sizing_banner", "warn", "sizing policy", "",
                              f"banner count was {count}; set to exactly one",
                              "inserted" if count == 0 else "deduplicated"))
        return done
    return done


def enforce(md: str, states: Sequence[dict] | None = None,
            runner: dict | None = None) -> EnforcementResult:
    """Repair `md` at the line/section level and re-check it.

    `states` is research.rule_states() as the miner was shown it; `runner` is
    the weekly research block (for the runner fallback counts).
    """
    from .memory import _strip_changelog_sections

    states = list(states or [])
    md, _ = _strip_changelog_sections(md or "")
    pre, sections = _split(md)
    _ensure_reading(pre, sections)

    violations: list[Violation] = []
    replace_best_edge = False
    replace_runner = False

    for idx, (heading, body) in enumerate([("## (preamble)", pre)] + sections):
        if _name(heading).startswith("reading this file"):
            continue   # system-owned; just rewritten from the template
        new_body = list(body)
        for i, v in _line_hits(heading, body, states):
            violations.append(v)
            if v.severity != "block":
                continue
            if _name(heading).startswith("current best edge") and v.checker in (
                    "provenance", "edge_bar", "wr_suppression"):
                replace_best_edge = True
                v.action = "replaced"
                continue
            if _name(heading).startswith("runner lane"):
                replace_runner = True
                v.action = "replaced"
                continue
            if v.checker == "calibration_table":
                new_body[i] = _suppress_row(body[i], _CAL_ROW, "insufficient data")
                v.action = "replaced"
            elif v.checker == "selection_edge_table":
                new_body[i] = _suppress_row(body[i], _SE_ROW, "insufficient data")
                v.action = "replaced"
            else:
                new_body[i] = _withheld(v.checker)
                v.action = "withheld"
        if idx == 0:
            pre[:] = new_body
        else:
            sections[idx - 1] = (heading, new_body)

    doc = _join(pre, sections)
    from .loops import weekly as wk
    for msg in wk._check_best_edge(doc):
        violations.append(Violation("best_edge", "block", "current best edge", "", msg, "replaced"))
        replace_best_edge = True
    for msg in wk._check_runner_section(doc):
        violations.append(Violation("runner_section", "block", "runner lane", "", msg, "replaced"))
        replace_runner = True
    from . import open_questions
    for msg in open_questions.check_playbook_questions(doc):
        violations.append(Violation("open_questions", "warn", "open questions", "", msg))

    if replace_best_edge:
        found = False
        for i, (heading, _body) in enumerate(sections):
            if _name(heading).startswith("current best edge"):
                sections[i] = (heading, _best_edge_block(states))
                found = True
        if not found:
            sections.insert(1, ("## Current Best Edge", _best_edge_block(states)))
    if replace_runner:
        found = False
        for i, (heading, _body) in enumerate(sections):
            if "runner" in _name(heading):
                sections[i] = ("## Runner Lane", _runner_block(runner))
                found = True
                break
        if not found:
            at = next((i for i, (h, _) in enumerate(sections)
                       if _name(h).startswith("shadow ledger")), len(sections))
            sections.insert(at, ("## Runner Lane", _runner_block(runner)))

    violations.extend(_fix_banner(sections))

    repaired = _join(pre, sections)
    remaining = [v for v in collect(repaired, states) if v.severity == "block"]
    if remaining:
        violations.append(Violation(
            "rewrite_rejected", "block", "(document)", "",
            f"{len(remaining)} blocking violation(s) survived repair; prior version kept",
            "rewrite_rejected"))
        return EnforcementResult(None, violations, remaining)
    return EnforcementResult(repaired, violations, [])
