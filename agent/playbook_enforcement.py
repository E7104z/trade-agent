"""
Write-time enforcement for the weekly playbook rewrite (WRITE_TIME_ENFORCEMENT_SPEC_v1,
extended by the 2026-09-29 playbook audit).

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

WHAT THE 2026-09-29 AUDIT ADDED
-------------------------------
Playbook v9 passed every check above and still told the desk things its
ledger could not support. The gaps, and what closes each:

  * Provenance was keyword-triggered, so paraphrase evaded it ("favor X"
    passed where "X beats" was withheld). Now every line in Setup Rules and
    Regime Rules must CARRY its status -- a VALIDATED `[rule: R...]` tag or
    `[UNVALIDATED prior]` -- and a line that carries neither is tagged
    `[UNVALIDATED prior]` by the system. The burden moved from "detect a
    claim" to "show a status".
  * A confidence tier was never checked against its n ("provisional, n=0
    triggered"). The stated tier may not exceed the tier the realized
    (triggered) n supports; a higher one is corrected.
  * Decay was enforced in Setup Rules only, from the confirmation table
    only. A dated `last_confirmed` 30+ market sessions old is now marked
    auto-demoted in Setup Rules, Regime Rules and Sizing Policy alike.
  * Redaction hid the line but not the name (the changelog listed it). A
    withheld setup line now says which setup it was and what status it has.
  * The Changelog was appended AFTER enforcement and never checked, so a
    finding withheld from the body reappeared there, and it described a
    Current Best Edge the system had replaced. It now passes through the
    same line checks, loses every line about a system-rendered section or an
    unanswerable question, and gets a system-computed decay line.
  * Measurement sections -- Calibration, Selection Edge, Runner Lane, Shadow
    Ledger Summary, Participation Funnel, Open Questions -- are rendered by
    the system from computed data (`computed`), so the miner cannot add a
    reading ("the filters are working") the numbers do not support.
  * A regime the desk traded in with no Regime Rules line (v9: uptrend/
    risk_off) gets an explicit "no playbook rule" line.

WRITER ISOLATION
----------------
Pure text in, text out. The weekly loop is still the only caller and still
the only writer of the playbook.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

__all__ = [
    "Violation",
    "EnforcementResult",
    "READING_TEMPLATE",
    "SIZING_BANNER",
    "SYSTEM_SECTIONS",
    "enforce",
    "collect",
    "dated_rule_lines",
]

# Sections where a line states a rule the desk may trade on. The provenance
# rule ("only VALIDATED rules may be stated as edge, each with its [rule:
# R...] tag") BLOCKS here and only here; elsewhere a sentence that merely
# mentions edge is prose and is recorded as a warning.
#
# Runner Lane is deliberately absent: it is system-rendered (see
# SYSTEM_SECTIONS), and when it is not, it has its own dedicated, blocking
# checker (_check_runner_section: missing, small-n means, contradictory tags).
RULE_SECTIONS = ("current best edge", "setup rules", "regime rules")

# Every bullet here must carry a status marker (see _STATUS).
STATUS_SECTIONS = ("setup rules", "regime rules")

# Bullets here are checked for a tier above their n, and for staleness.
TIERED_SECTIONS = ("setup rules", "regime rules", "sizing policy")

# Sections the system renders from computed data when the caller supplies it,
# in the order the playbook contract lists them. Keys are the lowercase
# heading prefixes used to find an existing section.
SYSTEM_SECTIONS: tuple[tuple[str, str], ...] = (
    ("calibration", "## Calibration"),
    ("selection edge", "## Selection Edge"),
    ("runner lane", "## Runner Lane"),
    ("shadow ledger", "## Shadow Ledger Summary"),
    ("participation funnel", "## Participation Funnel"),
    ("open questions", "## Open Questions (pre-registered experiments)"),
)

# The document's section order (the contract in prompts/weekly_miner.md), for
# inserting a missing system section in the right place.
_ORDER = ("reading this file", "current best edge", "core principles", "sizing policy",
          "setup rules", "regime rules", "do-not-trade", "calibration", "selection edge",
          "runner lane", "shadow ledger", "participation funnel", "open questions",
          "retired rules")

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
  recorded after its quarantine date. Only a VALIDATED rule may be stated AS
  EDGE anywhere in this file, and every such line carries its
  `[rule: Rxxxxxxxxxxxx]` tag.
- **Every other line in Setup Rules, Regime Rules and Sizing Policy is a
  working default, not edge.** It carries `[UNVALIDATED prior]` (the system
  adds the tag to a rule line that has no status): use it as a weak default,
  never cite it as edge, never size up on it.
- **n** = number of REALIZED (triggered) trades supporting the rule. Picks that
  never triggered are not n. The confidence tier follows from this n; a tier
  stated above it is corrected by the system.
- **last_confirmed** = session date of the most recent confirming observation.
  A rule line with no confirming observation in >= 30 market sessions is
  marked `[auto-demoted: stale N sessions]` in every section, and is treated
  as UNVALIDATED even if n is high.
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
- **System-rendered sections** -- Calibration, Selection Edge, Runner Lane,
  Shadow Ledger Summary, Participation Funnel, Open Questions -- are computed
  from the ledger, not written by the miner. An open question has a finding
  only when it shows a computed Result; until then it has none.
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
    "stale_rule": "stale-rule violation",
    "status_marker": "rule-status violation",
    "confidence_tier": "confidence-tier violation",
    "regime_coverage": "regime-coverage gap",
    "changelog": "changelog violation",
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
    changelog: str | None = None            # the repaired changelog (None if none given)

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


def _in(heading: str, keys: Sequence[str]) -> bool:
    return any(_name(heading).startswith(s) for s in keys)


def _is_rule_section(heading: str) -> bool:
    return _in(heading, RULE_SECTIONS)


def _is_system_section(heading: str, computed: dict | None) -> bool:
    if not computed:
        return False
    name = _name(heading)
    for key, _h in SYSTEM_SECTIONS:
        if key not in computed:
            continue
        if name.startswith(key) or (key == "runner lane" and "runner" in name):
            return True
    return False


def _withheld(checker: str, detail: str = "") -> str:
    return f"- [withheld by integrity check: {checker}{' — ' + detail if detail else ''}]"


# --------------------------------------------------------------------------
# Detection: the existing checkers, called on smaller inputs
# --------------------------------------------------------------------------

_SETUP_LINE = re.compile(r"^\s*[-*]\s+\**\s*([A-Za-z0-9_]+)\s*\**\s*:")
_BULLET = re.compile(r"^\s*[-*]\s+\S")
# A rule line's status: a VALIDATED tag (provenance checks it is one), an
# explicit UNVALIDATED marker, an auto-demotion, a system "no rule" line, or a
# withheld marker.
_STATUS = re.compile(
    r"\[(?:rule|provenance)\s*:\s*R[0-9a-f]{12}\]|unvalidated|auto-demoted|"
    r"no playbook rule|withheld by integrity check", re.I)
_TIER = re.compile(r"(confidence\s*:\s*)([A-Za-z]+)", re.I)
_TRIGGERED_N = re.compile(r"(\d+)\s+triggered\b", re.I)
_ANY_N = re.compile(r"\bn\s*=\s*(\d+)", re.I)
_LAST_CONFIRMED = re.compile(r"last_confirmed[\s:=]+(\d{4}-\d{2}-\d{2})", re.I)
_STALE = 30


def _stale_map(stale: Sequence[dict] | None) -> dict[str, dict]:
    return {str(s.get("rule_id") or "").lower(): s for s in (stale or [])}


def _realized_n(line: str) -> int | None:
    """The REALIZED n a rule line states: a "k triggered" count when the line
    has one ("n=4 picks (0 triggered)" is 0), else its n=."""
    m = _TRIGGERED_N.search(line)
    if m:
        return int(m.group(1))
    m = _ANY_N.search(line)
    return int(m.group(1)) if m else None


def _sessions_since(d: str, as_of: date) -> int | None:
    from .research import market_sessions_between
    try:
        return market_sessions_between(date.fromisoformat(d), as_of)
    except ValueError:
        return None


def _label(line: str) -> str:
    """The name a rule bullet leads with: "- **gap_go**: ..." -> "gap_go".
    A sentence-style line ("Correlated picks share a 1.5R cap. [n=1 ...]")
    has no name before a colon; its first words stand in for one."""
    body = re.sub(r"^\s*[-*]\s+", "", line)
    head = (body.split(":", 1)[0] if ":" in body else "").replace("*", "").replace("`", "").strip()
    if head and len(head) <= 40 and "[" not in head:
        return head
    text = body.split("[", 1)[0].replace("*", "").replace("`", "").strip().rstrip(".")
    return text if len(text) <= 40 else text[:37].rstrip() + "..."


def dated_rule_lines(md: str, as_of: date) -> list[dict]:
    """Every dated rule line in Setup Rules, Regime Rules and Sizing Policy,
    with its market-session age. The weekly miner is shown this list instead
    of only the setups that happen to have confirmation rows -- v9's "decay
    check" examined five rules and reported zero demotions while sympathy,
    trend_pullback, oversold_reversal, four regime rules and a sizing rule
    were never counted."""
    _pre, sections = _split(md)
    out = []
    for heading, body in sections:
        if not _in(heading, TIERED_SECTIONS):
            continue
        for line in body:
            m = _LAST_CONFIRMED.search(line) if _BULLET.match(line) else None
            if not m:
                continue
            n = _sessions_since(m.group(1), as_of)
            out.append({"section": _name(heading), "label": _label(line) or line.strip()[:60],
                        "last_confirmed": m.group(1), "sessions_since": n,
                        "stale": n is not None and n >= _STALE})
    return out


def _line_hits(heading: str, body: list[str], states: Sequence[dict],
               stale: Sequence[dict] | None = None,
               as_of: date | None = None) -> list[tuple[int, Violation]]:
    """Per-line violations inside one section, located by index into `body`."""
    from .loops import weekly as wk
    from .research import check_playbook_provenance, tier_for_n, tier_rank

    hits: list[tuple[int, Violation]] = []
    sec = _name(heading)
    rule_sec = _is_rule_section(heading)
    status_sec = _in(heading, STATUS_SECTIONS)
    tiered_sec = _in(heading, TIERED_SECTIONS)
    stale_by_setup = _stale_map(stale) if sec.startswith("setup rules") else {}
    for i, line in enumerate(body):
        if not line.strip():
            continue
        bullet = bool(_BULLET.match(line))
        low = line.lower()
        withheld = "withheld by integrity check" in low
        # Decay, enforced (FORWARD_VALIDATION_SPEC_v1 Phase 4): a setup rule
        # with no confirmation for >= 30 market sessions may still be listed,
        # but never as if it were live.
        stale_hit = False
        m = _SETUP_LINE.match(line)
        if m and stale_by_setup:
            s = stale_by_setup.get(m.group(1).lower())
            if s and "auto-demoted" not in low and "unvalidated" not in low:
                stale_hit = True
                hits.append((i, Violation(
                    "stale_setup_rule", "block", sec, line,
                    f"{m.group(1)} last confirmed {s.get('last_confirmed')}, "
                    f"{s.get('sessions_since')} market sessions ago: it must be marked "
                    f"auto-demoted")))
        # The same rule, from the date the line itself states -- in every
        # section that carries rules, not only Setup Rules.
        if tiered_sec and bullet and as_of is not None and not stale_hit and not withheld:
            md_ = _LAST_CONFIRMED.search(line)
            n_since = _sessions_since(md_.group(1), as_of) if md_ else None
            if n_since is not None and n_since >= _STALE and "auto-demoted" not in low:
                stale_hit = True
                hits.append((i, Violation(
                    "stale_rule", "block", sec, line,
                    f"last_confirmed {md_.group(1)} is {n_since} market sessions ago "
                    f"(>= {_STALE}): it must be marked auto-demoted")))
        # A stated tier above what the realized n supports.
        if tiered_sec and bullet and not withheld:
            mt = _TIER.search(line)
            n_real = _realized_n(line) if mt else None
            if mt and n_real is not None and tier_rank(mt.group(2)) > tier_rank(tier_for_n(n_real)):
                hits.append((i, Violation(
                    "confidence_tier", "block", sec, line,
                    f"states confidence '{mt.group(2)}' on a realized n of {n_real}; "
                    f"that n supports '{tier_for_n(n_real)}'")))
        # Every rule line carries its status. A line being auto-demoted gets
        # that tag instead, which is itself its status.
        needs_status = (status_sec and bullet) or (
            sec.startswith("sizing policy") and bullet
            and (_ANY_N.search(line) or _LAST_CONFIRMED.search(line)))
        if needs_status and not stale_hit and not _STATUS.search(line):
            hits.append((i, Violation(
                "status_marker", "block", sec, line,
                "rule line carries neither a VALIDATED [rule: R...] tag nor "
                "[UNVALIDATED prior]; nothing in this file is edge until validated")))
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


def collect(md: str, states: Sequence[dict] | None = None,
            stale: Sequence[dict] | None = None, as_of: date | None = None,
            computed: dict | None = None) -> list[Violation]:
    """Every violation in `md`, located, without changing anything."""
    from .loops import weekly as wk
    from . import open_questions

    states = list(states or [])
    pre, sections = _split(md)
    out: list[Violation] = []
    for heading, body in [("## (preamble)", pre)] + sections:
        out.extend(v for _, v in _line_hits(heading, body, states, stale, as_of))

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


def _order_index(heading: str) -> int:
    name = _name(heading)
    for i, key in enumerate(_ORDER):
        if name.startswith(key) or (key == "runner lane" and "runner" in name):
            return i
    return len(_ORDER)


def _place_system_sections(sections: list[tuple[str, list[str]]],
                           computed: dict) -> None:
    """Replace (or insert, in contract order) every system-rendered section
    the caller computed."""
    for key, heading in SYSTEM_SECTIONS:
        body = computed.get(key)
        if body is None:
            continue
        body = list(body)
        idx = next((i for i, (h, _b) in enumerate(sections)
                    if _name(h).startswith(key)
                    or (key == "runner lane" and "runner" in _name(h))), None)
        if idx is not None:
            sections[idx] = (heading, body)
            continue
        want = _ORDER.index(key) if key in _ORDER else len(_ORDER)
        at = next((i for i, (h, _b) in enumerate(sections) if _order_index(h) > want),
                  len(sections))
        sections.insert(at, (heading, body))


def _observed_regimes(regimes: Sequence[str] | None) -> list[str]:
    """trend/risk prefixes of the regime tags the desk actually saw."""
    out: list[str] = []
    for tag in regimes or []:
        parts = str(tag or "").strip().lower().split("/")
        if len(parts) < 2 or "unknown" in parts:
            continue
        prefix = "/".join(parts[:2])
        if prefix not in out:
            out.append(prefix)
    return out


def _cover_regimes(sections: list[tuple[str, list[str]]],
                   regimes: Sequence[str] | None) -> list[Violation]:
    """An explicit line for every observed regime no Regime Rules line covers:
    a desk classified into a state the playbook never mentions has to be told
    that no guidance applies, not left to infer it from a neighbouring rule."""
    observed = _observed_regimes(regimes)
    if not observed:
        return []
    for i, (heading, body) in enumerate(sections):
        if not _name(heading).startswith("regime rules"):
            continue
        labels = [_label(l).lower() for l in body if _BULLET.match(l)]
        missing = [r for r in observed
                   if not any(lab and (lab == r or r.startswith(lab + "/")
                                       or lab.startswith(r + "/")) for lab in labels)]
        if not missing:
            return []
        new_body = list(body)
        while new_body and not new_body[-1].strip():
            new_body.pop()
        done = []
        for r in missing:
            line = (f"- {r}: no playbook rule — the desk traded in this regime but no rule "
                    "or prior covers it; no regime-specific guidance applies.")
            new_body.append(line)
            done.append(Violation("regime_coverage", "warn", "regime rules", line,
                                  f"regime {r} was observed but has no Regime Rules line",
                                  "inserted"))
        new_body.append("")
        sections[i] = (heading, new_body)
        return done
    return []


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


def _repair_line(line: str, v: Violation, stale: Sequence[dict] | None,
                 as_of: date | None) -> str:
    """One non-withholding repair, applied on top of any earlier one."""
    from .research import tier_for_n
    if v.checker == "calibration_table":
        return _suppress_row(line, _CAL_ROW, "insufficient data")
    if v.checker == "selection_edge_table":
        return _suppress_row(line, _SE_ROW, "insufficient data")
    if v.checker == "stale_setup_rule":
        n = (_stale_map(stale).get(_SETUP_LINE.match(line).group(1).lower()) or {}).get("sessions_since")
        return f"{line.rstrip()} [auto-demoted: stale {n} sessions]"
    if v.checker == "stale_rule":
        m = _LAST_CONFIRMED.search(line)
        n = _sessions_since(m.group(1), as_of) if (m and as_of) else None
        return f"{line.rstrip()} [auto-demoted: stale {n} sessions]"
    if v.checker == "confidence_tier":
        n_real = _realized_n(line)
        return _TIER.sub(lambda m: m.group(1) + tier_for_n(n_real), line, count=1)
    if v.checker == "status_marker":
        return f"{line.rstrip()} [UNVALIDATED prior]"
    return line


# Repairs that rewrite a line rather than withhold it, in the order they are
# applied when one line has several: the tier and status first, the demotion
# tag last so it ends the line.
_Q_START = re.compile(
    r"^\s*(?:#+\s*)?(?:[-*]\s+)?(?:\*\*)?\s*(?:Q|Open\s+Question\s*#?\s*)(\d{1,2})\b"
    r"|^\s*(\d{1,2})\.\s", re.I)


def _question_of_line(body: list[str]) -> list[int | None]:
    """For each line of an Open Questions section, the question it belongs to."""
    out: list[int | None] = []
    cur: int | None = None
    for line in body:
        m = _Q_START.match(line)
        if m:
            cur = int(m.group(1) or m.group(2))
        out.append(cur)
    return out


_REPAIR_ORDER = ("confidence_tier", "status_marker", "stale_setup_rule", "stale_rule",
                 "calibration_table", "selection_edge_table")


def _withheld_line(line: str, v: Violation, section: str) -> str:
    """A redaction that says what it redacted. v9 withheld three Setup Rules
    lines as bare markers while the changelog named all three setups: the
    redaction hid the line, not the information, and left the morning agent
    without the one thing it needed -- the setup's status."""
    m = _SETUP_LINE.match(line)
    if m and section.startswith("setup rules"):
        name = m.group(1)
        return (f"- {name}: [withheld by integrity check: {v.checker} — the miner's line "
                f"for {name} made a claim its evidence does not support; treat {name} "
                "as an UNVALIDATED prior]")
    return _withheld(v.checker)


# --------------------------------------------------------------------------
# Changelog
# --------------------------------------------------------------------------

# What a changelog line is ABOUT is the topic it opens with ("Calibration
# table: ...", "Runner Lane: ADDED ...", "Q7: 24/8 met; finding: ..."). An open
# question named anywhere in the line is a statement about that question.
_SYSTEM_TOPICS = ("calibration", "selection edge", "runner lane", "shadow ledger",
                  "participation", "open question")
_QUESTION_REF = re.compile(r"\bQ\d{1,2}\b")


def _changelog_topic(topics: Sequence[str]) -> re.Pattern:
    alts = "|".join(re.escape(t) for t in topics)
    return re.compile(r"^\s*(?:[-*]\s+)?\**\s*(?:" + alts + r")", re.I) if alts else re.compile(r"(?!x)x")


def _enforce_changelog(changelog: str, states: Sequence[dict], withheld_setups: set[str],
                       replaced: set[str], system_rendered: bool,
                       decay_line: str | None,
                       withheld_questions: set[int] | None = None) -> tuple[str, list[Violation]]:
    """The changelog gets the body's line checks, loses every line about a
    section the system rendered or replaced (the miner described content it
    did not author -- v9's changelog described a Current Best Edge "with a
    full counterfactual block" that the system had replaced, and restated
    three findings withheld from the body), and loses lines that describe a
    withheld setup rule as added or changed."""
    from .loops import weekly as wk
    out: list[str] = []
    found: list[Violation] = []
    topics = list(_SYSTEM_TOPICS) if system_rendered else []
    topics += [r.lower() for r in sorted(replaced)]
    if decay_line:
        topics.append("decay check")
    topic = _changelog_topic(topics)
    for line in (changelog or "").splitlines():
        if not line.strip():
            out.append(line)
            continue
        why = None
        q_refs = {int(q[1:]) for q in _QUESTION_REF.findall(line)}
        if topic.search(line) or (system_rendered and q_refs):
            why = ("describes a section the system rendered or replaced; see that "
                   "section for what was actually written")
        elif q_refs & (withheld_questions or set()):
            why = "restates an open-question finding that was withheld from the playbook body"
        elif any(re.search(rf"\b{re.escape(s)}\b", line, re.I) for s in withheld_setups):
            why = "describes a setup rule that was withheld from this version"
        else:
            msgs = [m for m in wk._check_wr_suppression(line) if "[missing:" not in m]
            if msgs:
                why = "WR/avgR below n=8"
            elif _EDGE_BAR.search(line):
                why = "states |t| >= 1.5 as the bar for edge"
        if why:
            found.append(Violation("changelog", "block", "changelog", line, why, "withheld"))
            out.append(_withheld("changelog", why))
        else:
            out.append(line)
    head = []
    if decay_line:
        head.append(decay_line)
    if system_rendered:
        head.append("- System-rendered this version (computed from the ledger, not written "
                    "by the miner): Calibration, Selection Edge, Runner Lane, Shadow Ledger "
                    "Summary, Participation Funnel, Open Questions.")
    for r in sorted(replaced):
        head.append(f"- {r}: the miner's draft failed a write-time integrity check and "
                    "was replaced by the system render (see playbook_violations).")
    text = "\n".join(head + ([""] if head and out else []) + out).strip("\n")
    return text, found


def _decay_line(md: str, as_of: date) -> str:
    """The changelog's decay statement, computed over every dated rule line."""
    dated = dated_rule_lines(md, as_of)
    stale = [d for d in dated if d["stale"]]
    live = sorted((d for d in dated if not d["stale"] and d["sessions_since"] is not None),
                  key=lambda d: -d["sessions_since"])
    demoted = (", ".join(f"{d['label']} ({d['sessions_since']} sessions)" for d in stale)
               + " auto-demoted") if stale else "no rule line is 30+ market sessions stale"
    nearest = (" Closest to demotion: " + ", ".join(
        f"{d['label']} {d['sessions_since']}" for d in live[:3]) + ".") if live else ""
    return (f"- Decay check (system, at write time, over every dated rule line in Setup "
            f"Rules, Regime Rules and Sizing Policy): {len(dated)} line(s) checked; "
            f"{demoted}.{nearest}")


# --------------------------------------------------------------------------
# enforce
# --------------------------------------------------------------------------

def enforce(md: str, states: Sequence[dict] | None = None,
            runner: dict | None = None,
            stale: Sequence[dict] | None = None, *,
            as_of: date | None = None,
            computed: dict | None = None,
            regimes: Sequence[str] | None = None,
            changelog: str | None = None) -> EnforcementResult:
    """Repair `md` at the line/section level and re-check it.

    `states` is research.rule_states() as the miner was shown it; `runner` is
    the weekly research block (for the runner fallback counts). The keyword
    arguments are the audit's additions, each optional so a caller without
    them gets the original behaviour:

      as_of     -- the run date: enables date-based decay in every rule section
      computed  -- {section key: body lines} for SYSTEM_SECTIONS the system
                   renders in place of the miner's text
      regimes   -- regime tags the desk traded in, for the coverage lines
      changelog -- the miner's changelog, returned repaired in `.changelog`
    """
    from .memory import _strip_changelog_sections

    states = list(states or [])
    md, _ = _strip_changelog_sections(md or "")
    pre, sections = _split(md)
    _ensure_reading(pre, sections)
    if computed:
        _place_system_sections(sections, computed)

    violations: list[Violation] = []
    replace_best_edge = False
    replace_runner = False
    withheld_setups: set[str] = set()
    withheld_questions: set[int] = set()

    for idx, (heading, body) in enumerate([("## (preamble)", pre)] + sections):
        if _name(heading).startswith("reading this file"):
            continue   # system-owned; just rewritten from the template
        if _is_system_section(heading, computed):
            continue   # rendered from computed data just now
        by_line: dict[int, list[Violation]] = {}
        for i, v in _line_hits(heading, body, states, stale, as_of):
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
            by_line.setdefault(i, []).append(v)
        new_body = list(body)
        q_of = (_question_of_line(body) if _name(heading).startswith("open questions")
                else [None] * len(body))
        for i, vs in by_line.items():
            fatal = [v for v in vs if v.checker not in _REPAIR_ORDER]
            if fatal:
                new_body[i] = _withheld_line(body[i], fatal[0], _name(heading))
                m = _SETUP_LINE.match(body[i])
                if m and _name(heading).startswith("setup rules"):
                    withheld_setups.add(m.group(1))
                if q_of[i] is not None:
                    withheld_questions.add(q_of[i])
                for v in vs:
                    v.action = "withheld"
                continue
            line = body[i]
            for v in sorted(vs, key=lambda v: _REPAIR_ORDER.index(v.checker)):
                line = _repair_line(line, v, stale, as_of)
                v.action = "replaced"
            new_body[i] = line
        if idx == 0:
            pre[:] = new_body
        else:
            sections[idx - 1] = (heading, new_body)

    doc = _join(pre, sections)
    from .loops import weekly as wk
    for msg in wk._check_best_edge(doc):
        violations.append(Violation("best_edge", "block", "current best edge", "", msg, "replaced"))
        replace_best_edge = True
    if not (computed and "runner lane" in computed):
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
    violations.extend(_cover_regimes(sections, regimes))

    repaired = _join(pre, sections)
    remaining = [v for v in collect(repaired, states, stale, as_of=as_of) if v.severity == "block"]

    new_changelog = None
    if changelog is not None or as_of is not None:
        decay_line = _decay_line(repaired, as_of) if as_of is not None else None
        replaced = set()
        if replace_best_edge:
            replaced.add("Current Best Edge")
        if replace_runner:
            replaced.add("Runner Lane")
        new_changelog, cl_found = _enforce_changelog(
            changelog or "", states, withheld_setups, replaced, bool(computed), decay_line,
            withheld_questions)
        violations.extend(cl_found)

    if remaining:
        violations.append(Violation(
            "rewrite_rejected", "block", "(document)", "",
            f"{len(remaining)} blocking violation(s) survived repair; prior version kept",
            "rewrite_rejected"))
        return EnforcementResult(None, violations, remaining, new_changelog)
    return EnforcementResult(repaired, violations, [], new_changelog)
