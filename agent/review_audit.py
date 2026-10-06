"""
The grader's claims, checked against the computed numbers before anything is
saved or emailed (specs/VERIFICATION_PRACTICE_v1.md).

Every evening review so far has contained claims a reader could refute from
numbers printed beside them. On 2026-10-05:
  * "participation was one-and-done" on ALEC, whose RVOL went 2.49x at 10:00
    to 6.88x at the close (4.4 average days of volume AFTER 10:00);
  * a C and "shouldn't have been taken" on NVDA, a coverage pick the system
    placed on purpose;
  * "-0.14R forgone confirms the filters are working", from six rejections.
The grounding layer checks that a quoted number exists in the input; it does
not check that the sentence around it agrees with the rest of the input.
This module does, for the claim shapes a reviewer kept catching by hand:

  volume_contradiction  a claim that participation faded, dried up, was
                        one-and-done or was weak, about a ticker whose own
                        RVOL says the opposite
  scratch_as_result     a scratch (|R| <= grading.SCRATCH_R) described as a
                        winner or a loser
  test_lane_penalized   a test-lane pick (coverage, floor, hypothesis, R:R
                        control, backlog probe) graded C-F for having been
                        taken at all
  forgone_conclusion    one session's forgone R read as proof about the filters

What happens to a finding:
  * a lesson or a new idea carrying one is DROPPED: it never becomes memory;
  * a pick review or the plan notes keep the grader's text, with an
    "[AUDIT: ...]" note appended so the email and dashboard show both;
  * every finding is returned for runs.stages, the email's AUDIT block and
    the self-check.
The rules are deliberately narrow: they fire on a claim the numbers plainly
contradict, never on a judgment call.
"""
from __future__ import annotations

import re
from typing import Any

from .grading import SCRATCH_R, outcome_class, realized_r

__all__ = ["audit", "text", "TEST_LANES"]

TEST_LANES = frozenset({"coverage", "daily_floor", "hypothesis", "rr_below_control",
                        "backlog_probe"})

# Volume AFTER 10:00 alone of at least one average full day contradicts a
# claim that participation stopped early.
_LATE_VOLUME_ADV = 1.0

_FADE = re.compile(
    r"one[- ]and[- ]done|front[- ]loaded|dr(?:ied|ies|ying|y)\s+up"
    r"|(?:volume|participation|buying|selling)\s+(?:fade[sd]?|fading|dried|dries|drying|died|dies|"
    r"dying|evaporat\w*|disappear\w*|stop(?:ped|s)|vanish\w*|petered)"
    r"|(?:faded|fading|dying|no)\s+(?:volume|participation)", re.I)
_WEAK = re.compile(
    r"\b(?:weak|light|low|thin|poor|anemic|lackluster|tepid)\s+(?:volume|participation|rvol)"
    r"|(?:volume|participation|rvol)\s+(?:was|were|stayed|remained)\s+"
    r"(?:weak|light|low|thin|poor|anemic|lackluster|tepid)", re.I)
_RESULT_WORD = re.compile(
    r"\b(?:winner|winning trade|won|profitable|made money|loser|losing trade|lost money)\b", re.I)
_PENALTY = re.compile(
    r"should(?:n't| not| never)\s+have\s+been\s+(?:taken|traded|placed|on the tape)"
    r"|should(?:n't| not| never)\s+have\s+(?:taken|traded|placed)\s+(?:it|this|the trade)"
    r"|should\s+have\s+been\s+(?:rejected|skipped|passed|killed)"
    r"|never\s+should\s+have\s+been\s+(?:taken|traded)"
    r"|not\s+a\s+trade\s+(?:worth|to)\s+tak", re.I)
_FORGONE = re.compile(
    r"(?:forgone|shadow|filter|rejection)[^.]{0,100}?\b(?:confirm|validat|prove|proof|vindicat|"
    r"justif|working|works|worked)"
    r"|\b(?:confirm|validat|prove|vindicat|justif)\w*[^.]{0,100}?(?:forgone|shadow|filter)", re.I)


def _finding(check: str, where: str, ticker: str | None, why: str, action: str) -> dict:
    return {"check": check, "where": where, "ticker": ticker, "why": why, "action": action}


def _volume_conflict(text: str, o: dict) -> str | None:
    """Why the ticker's RVOL contradicts a participation claim in `text`."""
    r10, rc = o.get("rvol_at_1000"), o.get("rvol_close")
    if rc is None:
        return None
    if _FADE.search(text) and r10 is not None and rc - r10 >= _LATE_VOLUME_ADV:
        return (f"claims participation stopped early, but RVOL went {r10:.2f}x at 10:00 to "
                f"{rc:.2f}x at the close: {rc - r10:.2f} average days of volume traded after 10:00")
    if _WEAK.search(text) and rc >= 1.0:
        return (f"claims weak participation, but the day traded {rc:.2f}x its 20-day average "
                f"volume (RVOL is cumulative: an early reading is not the day's pace)")
    return None


def _mentions(text: str, ticker: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z]){re.escape(ticker)}(?![A-Za-z])", text or ""))


def audit(review: dict, picks: list[dict], outcomes: list[dict]) -> dict:
    """Check the grader's output against the computed outcomes. Returns
    {"review": the review with failing lessons/ideas removed and notes
    appended, "findings": [...], "dropped_lessons": n, "dropped_ideas": n}.
    Never raises: an audit that cannot run leaves the review unchanged."""
    try:
        return _audit(review, picks, outcomes)
    except Exception as e:  # noqa: BLE001 -- the audit never costs the evening
        return {"review": review, "findings": [_finding(
            "audit_error", "audit", None, f"{type(e).__name__}: {e}", "none")],
            "dropped_lessons": 0, "dropped_ideas": 0}


def _audit(review: dict, picks: list[dict], outcomes: list[dict]) -> dict:
    review = dict(review or {})
    by_pick = {o.get("pick_id"): o for o in outcomes or []}
    pick_by_id = {p.get("pick_id"): p for p in picks or []}
    pick_by_ticker = {str(p.get("ticker", "")).upper(): p for p in picks or []}
    findings: list[dict] = []

    def facts(ticker: str) -> tuple[dict | None, dict]:
        p = pick_by_ticker.get(ticker)
        return p, (by_pick.get(p["pick_id"]) if p else None) or {}

    # -- pick reviews: annotate, never rewrite the grade
    reviews = []
    for r in review.get("pick_reviews") or []:
        r = dict(r)
        p = pick_by_id.get(r.get("pick_id")) or pick_by_ticker.get(str(r.get("ticker", "")).upper())
        if p is None:
            reviews.append(r)
            continue
        o = by_pick.get(p["pick_id"]) or {}
        t = str(p.get("ticker", "")).upper()
        why = str(r.get("why") or "")
        notes = []
        conflict = _volume_conflict(why, o)
        if conflict:
            findings.append(_finding("volume_contradiction", "pick_review", t, conflict, "noted"))
            notes.append(conflict)
        if outcome_class(realized_r(o)) == "scratch" and _RESULT_WORD.search(why):
            msg = (f"describes a scratch ({realized_r(o):+.2f}R, inside +/-{SCRATCH_R:.2f}R) as a "
                   "win or a loss; its sign is noise")
            findings.append(_finding("scratch_as_result", "pick_review", t, msg, "noted"))
            notes.append(msg)
        if p.get("exploration_lane") in TEST_LANES and str(r.get("grade") or "").upper()[:1] in "CDF" \
                and _PENALTY.search(why):
            msg = (f"graded {r.get('grade')} for having been taken, but it is a "
                   f"{p.get('exploration_lane')} pick the system placed on purpose")
            findings.append(_finding("test_lane_penalized", "pick_review", t, msg, "noted"))
            notes.append(msg)
        if notes:
            r["why"] = why + " [AUDIT: " + "; ".join(notes) + "]"
        reviews.append(r)
    if "pick_reviews" in review:
        review["pick_reviews"] = reviews

    # -- plan notes: one session's forgone R proves nothing
    notes_txt = str(review.get("plan_notes") or "")
    if _FORGONE.search(notes_txt):
        msg = "reads one session's forgone R as proof about the filters; a handful of names proves nothing"
        findings.append(_finding("forgone_conclusion", "plan_notes", None, msg, "noted"))
        review["plan_notes"] = notes_txt + " [AUDIT: " + msg + "]"

    # -- lessons and new ideas: a failing one never becomes memory
    def gate(items: list, where: str, fields: tuple[str, ...]) -> tuple[list, int]:
        kept, dropped = [], 0
        for item in items or []:
            if not isinstance(item, dict):
                kept.append(item)
                continue
            body = " ".join(str(item.get(f) or "") for f in fields)
            reasons = []
            for t in pick_by_ticker:
                if not _mentions(body, t):
                    continue
                _, o = facts(t)
                conflict = _volume_conflict(body, o)
                if conflict:
                    reasons.append(f"{t}: {conflict}")
                if outcome_class(realized_r(o)) == "scratch" and _RESULT_WORD.search(body):
                    reasons.append(f"{t}: cites a scratch ({realized_r(o):+.2f}R) as a result")
            if reasons:
                dropped += 1
                findings.append(_finding(
                    "volume_contradiction" if "participation" in reasons[0] else "scratch_as_result",
                    where, None, "; ".join(reasons) + f" — dropped: {body[:160]}", "dropped"))
                continue
            kept.append(item)
        return kept, dropped

    n_lessons = n_ideas = 0
    if review.get("lessons"):
        review["lessons"], n_lessons = gate(review["lessons"], "lesson", ("lesson", "evidence"))
    if review.get("hypotheses"):
        review["hypotheses"], n_ideas = gate(review["hypotheses"], "idea",
                                             ("statement", "origin", "refutation"))
    return {"review": review, "findings": findings,
            "dropped_lessons": n_lessons, "dropped_ideas": n_ideas}


def text(result: dict) -> str:
    """The email's AUDIT block: empty when nothing was found."""
    findings = (result or {}).get("findings") or []
    if not findings:
        return ""
    lines = [f"AUDIT (the grader's claims checked against the computed numbers): "
             f"{len(findings)} finding(s), {result.get('dropped_lessons', 0)} lesson(s) and "
             f"{result.get('dropped_ideas', 0)} idea(s) dropped before saving"]
    for f in findings:
        who = f" {f['ticker']}" if f.get("ticker") else ""
        lines.append(f"- [{f['check']}] {f['where']}{who}: {f['why']} ({f['action']})")
    return "\n".join(lines)
