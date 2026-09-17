"""
The pre-registered open-question registry.

WHY THIS MODULE EXISTS
----------------------
Pre-registration is the desk's defense against reading a threshold off the
data it is supposed to test. That defense only works if the threshold is
written down somewhere the test cannot edit. It was not. Question ids lived
as constants in the evening loop, thresholds lived ONLY as prose inside
playbook.md, and playbook.md is rewritten from scratch every week by the
weekly miner. A pre-registered threshold that the weekly rewrite can restate
is not pre-registered; it is a number the miner re-chooses every Sunday, and
"12/15, nearly resolved" is what that looks like from outside when the
declared bar was 20.

Two concrete failures this closes:

1. THRESHOLD DRIFT. The miner restates every `Progress: X/threshold` line on
   each rewrite. Nothing compared the restated threshold against the declared
   one, so a question could quietly get easier the week it was close to
   answering -- the exact moment a moving bar does the most damage.

2. ID COLLISION. `agent/loops/evening.py` claimed ids 7-12 for the indicator
   layer (Q7 = RVOL separation). The miner independently minted its own "Open
   Question #7" for the R:R threshold test when it wrote playbook v4. Both
   then existed as "Q7": the evening loop incremented Q7 with RVOL
   observations, and the playbook reported those increments as progress on an
   R:R experiment they had nothing to do with. The registry is now the single
   authority on ids, and the R:R test holds id 13 -- its own id, so historical
   rows tagged 7 keep meaning what they meant when they were written.

The registry is deliberately dumb data. It does not read the ledger and it
never changes as a result of a run. Changing a threshold means editing this
file in a commit, which is what pre-registration is supposed to cost.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

__all__ = [
    "OPEN_QUESTIONS",
    "by_id",
    "registry_md",
    "check_playbook_questions",
]


def _q(qid: int, slug: str, title: str, threshold: int, unit: str,
       pre_registered: bool = True, note: str = "") -> dict[str, Any]:
    return {"id": qid, "slug": slug, "title": title, "threshold": threshold,
            "unit": unit, "pre_registered": pre_registered, "note": note}


# ids 1-12 match the constants in agent/loops/evening.py, which are what the
# `open_question_progress` rows are actually tagged with. Do not renumber:
# the ids are the join key to every progress row ever written.
OPEN_QUESTIONS: list[dict[str, Any]] = [
    _q(1, "gap_fade_entry_mechanics",
       "gap_fade entry mechanics - does failed-bounce entry beat open-print entry?",
       8, "gap_fade observations with both prices"),
    _q(2, "sympathy_standalone",
       "sympathy standalone - salvageable, or edge-free?",
       8, "standalone sympathy observations"),
    _q(3, "conviction_calibration",
       "conviction calibration - does stated conviction predict realized R?",
       20, "graded picks"),
    _q(4, "gap_size_chase_threshold",
       "gap-size chase threshold - is 15% the right cutoff?",
       20, "gap trades"),
    _q(5, "shadow_filter_cost",
       "shadow-ledger filter cost - which rejection reasons forgo the most R?",
       20, "sessions of shadow data"),
    _q(6, "stop_placement_cost",
       "stop placement cost - do targets print later on stopped trades?",
       10, "stopped trades"),
    # ---- indicator layer. These were given ids in code but never a written
    # threshold, so they carry the project's declared default forward-n
    # (research.DEFAULT_FORWARD_N) rather than a number invented here. They
    # are marked NOT pre-registered so nothing reports them as though a bar
    # had been set in advance.
    _q(7, "rvol_separation",
       "RVOL separation - does participation separate winners from chop?",
       8, "triggered picks with RVOL@10:00", pre_registered=False,
       note="defaulted to the standard forward-n; never pre-registered in prose"),
    _q(8, "vwap_reversal_edge",
       "VWAP-reclaim longs vs plain dip-buy longs - same edge?",
       8, "triggered longs citing VWAP", pre_registered=False,
       note="defaulted to the standard forward-n; never pre-registered in prose"),
    _q(9, "stop_atr_sweet_spot",
       "stop distance sweet spot - do 1-2 ATR stops out-R sub-0.5 ATR stops?",
       8, "stopped trades with stop_dist_atr", pre_registered=False,
       note="defaulted to the standard forward-n; never pre-registered in prose"),
    _q(10, "blue_sky_vs_overhead",
       "blue-sky gaps vs gaps into overhead supply",
       8, "triggered gap/breakout picks", pre_registered=False,
       note="defaulted to the standard forward-n; never pre-registered in prose"),
    _q(11, "trend_alignment_200d",
       "trend alignment - longs above vs below the 200d MA (shorts mirrored)",
       8, "triggered picks with trend_side_200d", pre_registered=False,
       note="defaulted to the standard forward-n; never pre-registered in prose"),
    _q(12, "shortedness",
       "shortedness - does short%float >= 15 separate squeeze winners from chop?",
       8, "triggered squeeze/momentum picks", pre_registered=False,
       note="defaulted to the standard forward-n; never pre-registered in prose"),
    # ---- ids 13 and 14, not 7. The miner minted this experiment as "#7" in
    # playbook v4, colliding with the indicator layer's RVOL question, which
    # had held id 7 in code since before v4 was written.
    #
    # It is ONE test with TWO arms, and it gets two ids because one counter
    # cannot express "20 observations per bucket": a single total reaching 20
    # says nothing about whether either arm has enough rows to compare. The
    # playbook reported "~7/20" and later "~12/15" for this test while the
    # evening loop was writing no observations for it at all -- those numbers
    # were the miner's estimates from reading the shadow ledger, which is
    # exactly the hand-counting that pre-registration exists to replace.
    _q(13, "rr_threshold_test_taken",
       "R:R threshold test, ARM A - taken trades at planned R:R >= 2.0",
       20, "triggered trades at R:R >= 2.0",
       note="paired with Q14; was numbered #7 in playbook v4-v5"),
    _q(14, "rr_threshold_test_shadow",
       "R:R threshold test, ARM B - rejected candidates at 1.8 <= R:R < 2.0",
       20, "scored rejections in the 1.8-2.0 band",
       note="paired with Q13; the shadow arm of the same experiment"),
]

_BY_ID = {q["id"]: q for q in OPEN_QUESTIONS}


def by_id(qid: int) -> dict[str, Any] | None:
    return _BY_ID.get(qid)


def registry_md(progress: Iterable[dict[str, Any]] | None = None) -> str:
    """Render the registry with observed progress, thresholds included.

    The threshold column comes from THIS file, never from the playbook the
    miner is about to rewrite, so the bar the miner is shown is the bar that
    was declared.
    """
    seen = {}
    for p in (progress or []):
        qid = p.get("question_id")
        if qid is not None:
            seen[int(qid)] = p
    lines = [
        "## OPEN QUESTION REGISTRY (pre-registered; thresholds are declared in "
        "agent/open_questions.py, NOT in the playbook)\n",
        "The `threshold` column is authoritative. Rewrite each question's "
        "`Progress: X/threshold` line using THESE numbers. You may not restate "
        "a threshold to a different value, and you may not renumber a question "
        "-- the ids are the join key to every progress row ever recorded. If a "
        "question genuinely needs a new bar, say so in the changelog and leave "
        "the registry value in the Progress line; changing it is a code commit, "
        "not a rewrite.\n",
    ]
    for q in OPEN_QUESTIONS:
        p = seen.get(q["id"], {})
        total = p.get("total", 0) or 0
        last = p.get("last_event", "never")
        tag = "" if q["pre_registered"] else f"  [NOT pre-registered: {q['note']}]"
        lines.append(
            f"- Q{q['id']} ({q['slug']}): {q['title']} "
            f"- Progress {total}/{q['threshold']} {q['unit']}; last event {last}{tag}"
        )
    return "\n".join(lines)


# Matches "Open Question #7", "Q7", or a leading "7." on a playbook list item.
_STATED = re.compile(
    r"(?:open\s+question\s*#?\s*|\bq|^\s*)(\d{1,2})[.):\s]", re.IGNORECASE)
_THRESH = re.compile(
    r"threshold[:\s]*(?:n\s*>=\s*)?(\d+)|progress[:\s*]*~?\s*\d+\s*/\s*(\d+)",
    re.IGNORECASE)


def check_playbook_questions(playbook_md: str) -> list[str]:
    """Flag any question whose stated threshold disagrees with the registry,
    and any question id the playbook uses that the registry does not define.

    Deliberately reports rather than rewrites: the miner owns the playbook
    prose, and a silent correction would hide that a rewrite tried to move a
    pre-registered bar. Reporting is what makes the attempt visible.
    """
    violations: list[str] = []
    for line in (playbook_md or "").splitlines():
        low = line.lower()
        if "threshold" not in low and "progress" not in low:
            continue
        m = _STATED.search(line)
        if not m:
            continue
        qid = int(m.group(1))
        stripped = line.strip()[:160]
        q = _BY_ID.get(qid)
        if q is None:
            violations.append(f"Q{qid} is not in the registry: {stripped}")
            continue
        rest = line[m.end():]
        # One line usually states its bar twice ("Threshold: 20 ... Progress:
        # 0/20"). That is one drift, not two, so the same (question, stated
        # value) is reported once.
        seen_here: set[int] = set()
        for tm in _THRESH.finditer(rest):
            stated = tm.group(1) or tm.group(2)
            if not stated:
                continue
            val = int(stated)
            if val == q["threshold"] or val in seen_here:
                continue
            seen_here.add(val)
            violations.append(
                f"Q{qid} ({q['slug']}) states threshold {val}, "
                f"registry declares {q['threshold']}: {stripped}")
    return violations
