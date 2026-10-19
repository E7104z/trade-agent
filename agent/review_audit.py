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
  mechanics_contradiction
                        a resting-order or stop-order fill called "late", "chasing"
                        or "entered N minutes after the open" as if the desk chose
                        when to enter, or a breakout order described as one that
                        "makes no sense" (docs/FIX_PHASE0.md, D6). An order already
                        through its level when it went live was a market order in
                        effect, so its timing is fair game

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

__all__ = ["audit", "text", "TEST_LANES", "order_mechanics", "mechanics_md"]

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


# ---- order mechanics (docs/FIX_PHASE0.md, D6) --------------------------------------
# A `breakout` entry is a STOP order and a `limit` entry a RESTING order: both go live
# at their window and fill whenever price gets there, so the fill time is the tape's,
# never a desk decision. 2026-10-16 the grader wrote of AMAT (a breakout short, filled
# 11:07 when 418.00 broke) that it "entered 97 minutes late ... chasing", and of INTC
# (a breakout short, a sell stop below the level) that the breakout "makes no sense":
# narrative about orders it had never been told how to read.

def _hhmm(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M")
    m = re.search(r"(\d{1,2}):(\d{2})", str(value))
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def _cancel_at(p: dict) -> str:
    """When an unfilled entry order is cancelled: the time stop, else the flatten."""
    return _hhmm(p.get("time_stop")) or "the 15:55 flatten"


# A fill this far through its level (0.1%) is a gap, not the simulator's slippage (config
# slippage_bps, 5): keep it above that, or a breakout filled at its level reads as gapped.
_GAP = 0.001


def _went_live_through(p: dict, o: dict) -> bool:
    """A limit or breakout that was already through its level when it went live: it filled
    on its first live bar (the entry window, else the 09:30 open), more than slippage past
    the level on the side the market already was. That order was a market order in effect
    (live_monitor sends a crossed breakout as one), so the go-live time, which the desk
    chose, set its fill time. CMC 10-15: a buy limit at 66.50 that filled at once at the
    09:45 open, 66.03."""
    etype = str(p.get("entry_type") or "open").lower()
    px, fill = p.get("entry_price"), o.get("entry_fill")
    if etype not in ("limit", "breakout") or not o.get("triggered") or o.get("exit_reason") == "invalid_fill":
        return False
    if not isinstance(px, (int, float)) or not isinstance(fill, (int, float)) or px <= 0:
        return False
    ft = _hhmm(o.get("fill_time"))
    if ft is None or ft != (_hhmm(p.get("entry_not_before")) or "09:30"):
        return False
    long = str(p.get("direction") or "long").lower() != "short"
    # a crossed stop fills past its level against the trade, a marketable limit in its favour
    through = (fill - px) if long == (etype == "breakout") else (px - fill)
    return through / px > _GAP


def order_mechanics(p: dict, o: dict | None) -> str:
    """One computed line on how this pick's entry order works and why it filled when
    it did. Pure: a function of the plan and the simulated outcome."""
    o = o or {}
    etype = str(p.get("entry_type") or "open").lower()
    long = str(p.get("direction") or "long").lower() != "short"
    side = "buy" if long else "sell"
    px = p.get("entry_price")
    live = _hhmm(p.get("entry_not_before")) or "the open"
    ft = _hhmm(o.get("fill_time"))
    fill = o.get("entry_fill")
    invalid = o.get("exit_reason") == "invalid_fill"
    filled = bool(o.get("triggered")) and not invalid
    gapped = (f" (price gapped past it: filled {fill:.2f})"
              if filled and isinstance(fill, (int, float)) and isinstance(px, (int, float))
              and abs(fill - px) / px > _GAP else "")
    at_once = (f"; already through {px:.2f} when the order went live{gapped}, so it filled at once "
               f"at {ft}: in effect a market order sent at {ft}. The go-live time set this fill time, "
               if _went_live_through(p, o) else None)
    if etype == "open":
        if not filled:
            return "market order at the open; it did not produce a valid fill"
        px_txt = f"{fill:.2f}" if isinstance(fill, (int, float)) else "?"
        return (f"market order sent at the open; filled at the opening print "
                f"({px_txt}). The desk chose this time: the open.")
    if not isinstance(px, (int, float)):
        return f"{etype} entry with no price"
    if etype == "breakout":
        head = f"breakout {'long' if long else 'short'} = {side} stop at {px:.2f}, live from {live}"
        if invalid:
            return head + "; its simulated fill was invalid (wrong side of the stop), not a trade"
        if at_once:
            return head + at_once + "not the level breaking."
        if filled:
            return (head + f"; filled {ft or '?'} when price first traded through {px:.2f}{gapped}. "
                    "The fill time is when the level broke, not a decision.")
        return (head + f"; never filled: price never traded through {px:.2f} while the order was "
                f"live, and it was cancelled at {_cancel_at(p)}.")
    if etype == "limit":
        head = f"resting {side} limit at {px:.2f} from {live}"
        if invalid:
            return head + "; its simulated fill was invalid (wrong side of the stop), not a trade"
        if at_once:
            return head + at_once + "not price reaching the level."
        if filled:
            return (head + f"; filled {ft or '?'} whenever price reached it{gapped}. "
                    "No one chose the fill time.")
        touched = o.get("touch_only_bars")
        why = (f"price touched {px:.2f} on {touched} bar(s) but never traded through it (a touch is "
               "not a fill)" if touched else f"price never reached {px:.2f} while the order was live")
        return head + f"; never filled: {why}, and it was cancelled at {_cancel_at(p)}."
    return f"{etype} entry at {px:.2f}"


def mechanics_md(picks: list[dict], outcomes: list[dict]) -> str:
    """The grader's ORDER MECHANICS section body: one line per pick."""
    by_pick = {o.get("pick_id"): o for o in outcomes or []}
    return "\n".join(f"- {p['ticker']} (pick_id={p['pick_id']}, {str(p.get('entry_type') or 'open').lower()}): "
                     f"{order_mechanics(p, by_pick.get(p['pick_id']))}" for p in picks or [])


# A fill described as the desk's timing decision: a judgment ("late", "chasing") or the
# desk's own verb ("entered ... after the open"). "The fill came 97 minutes after the open"
# is a fact the numbers confirm, so it is not one.
_TIMING = re.compile(
    r"\b\d+\s+minutes?\s+late\b"
    r"|\b(?:too\s+)?late\b[^.;]{0,40}\b(?:entry|entries|enter(?:ed|s)?|fill(?:ed|s)?)\b"
    r"|\b(?:entry|entries|enter(?:ed|s)?|fill(?:ed|s)?|got\s+in|came\s+in)\b[^.;]{0,40}\b(?:too\s+)?late\b"
    r"|\bchas(?:e|ed|es|ing)\b"
    r"|\b(?:entered|got\s+in)\b[^.;]{0,30}\bafter\s+the\s+(?:open|bell)\b", re.I)
# ... that a sentence is about an entry, for the words that are ambiguous alone.
_ENTRY_WORDS = re.compile(
    r"\b(?:entry|entries|enter(?:ed|ing|s)?|fill(?:ed|s)?|order|breakout|breakdown|limit|stop\s+order|"
    r"got\s+in|came\s+in|bought|sold|shorted)\b", re.I)
# ... unless the sentence itself says the timing was the tape's.
_MECH_AWARE = re.compile(
    r"not\s+(?:a\s+)?(?:desk\s+)?(?:decision|choice|chase|chasing)|no\s+one\s+chose|nobody\s+chose"
    r"|when\s+(?:the\s+)?(?:level|price)\s+(?:broke|reached|traded)|resting\s+(?:order|limit)"
    r"|tape\s+(?:set|chose|decided)|wasn'?t\s+(?:a\s+)?chas\w*|not\s+a\s+timing", re.I)
# A breakout (a stop order) called senseless.
_NONSENSE = re.compile(
    r"(?:makes?|made)\s+no\s+sense|doesn'?t\s+make\s+sense|does\s+not\s+make\s+sense"
    r"|nonsens\w+|illogical|incoherent|self[- ]contradict\w*", re.I)
_BREAKOUT_WORDS = re.compile(r"\b(?:breakout|stop\s+order|sell\s+stop|buy\s+stop)\b", re.I)
# ... where the words name the ORDER ("breakout short", "sell stop", "entering on a breakout"),
# not the setup label: NVDA 10-05's "labeling as breakout is ..." criticized the label
_BREAKOUT_ORDER = re.compile(
    r"\bbreakout[\s-]+(?:entry|entries|order|orders|stop|short|long|trigger)\b"
    r"|\bstop[\s-]+(?:order|entry)\b|\b(?:sell|buy)[\s-]+stop\b"
    r"|\bentry[\s_-]+type\W{0,3}breakout\b"
    r"|\b(?:enter(?:ed|ing|s)?|entry|short(?:ed|ing)?)\s+(?:on|via|with|through|using)\s+"
    r"(?:a\s+|the\s+)?breakout\b", re.I)
# ... or the senseless thing is the entry itself: INTC 10-15's "entry logic makes no sense"
_ENTRY_NONSENSE = re.compile(
    r"\b(?:entry|entries|order)\b(?:\s+[\w-]+){0,2}\s+(?:makes?\s+no\s+sense|made\s+no\s+sense"
    r"|doesn'?t\s+make\s+sense|does\s+not\s+make\s+sense"
    r"|(?:is|was)\s+(?:illogical|incoherent|nonsensical|self[- ]contradictory))", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def _mechanics_hit(text: str, p: dict, o: dict) -> tuple[str, str, str] | None:
    """(kind, phrase, sentence) for the first sentence of `text` that contradicts the
    pick's order mechanics, kind "timing" or "senseless"; None when none does. Fires only
    where it cannot be a judgment call: a resting or stop order that FILLED (not a
    market-order anomaly) described as a timing decision, or a breakout order called
    senseless. An order already through its level at go-live is exempt from both: it
    was a market order in effect."""
    etype = str(p.get("entry_type") or "open").lower()
    if etype not in ("limit", "breakout") or _went_live_through(p, o):
        return None
    filled = bool(o.get("triggered")) and o.get("exit_reason") != "invalid_fill" \
        and not o.get("execution_anomaly")
    for sent in (s.strip() for s in _SENTENCE.split(text or "") if s.strip()):
        if _MECH_AWARE.search(sent):
            continue
        hits = [m.group(0).strip() for m in _TIMING.finditer(sent)]
        # a "<N> minutes late" claim is about timing by itself; the ambiguous words
        # (late, chasing) must sit in a sentence about the entry
        if filled and hits and (_ENTRY_WORDS.search(sent) or any(h[:1].isdigit() for h in hits)):
            return "timing", next((h for h in hits if h[:1].isdigit()), hits[0]), sent
        nonsense = _NONSENSE.search(sent) if etype == "breakout" else None
        if nonsense and _BREAKOUT_WORDS.search(sent) and (_BREAKOUT_ORDER.search(sent)
                                                          or _ENTRY_NONSENSE.search(sent)):
            return "senseless", nonsense.group(0), sent
    return None


def _mechanics_conflict(text: str, p: dict, o: dict) -> str | None:
    """Why the pick's order mechanics contradict a sentence of `text`, or None."""
    hit = _mechanics_hit(text, p, o)
    if hit is None:
        return None
    if hit[0] == "timing":
        return f"calls the fill a timing decision (\"{hit[1]}\"), but {order_mechanics(p, o)}"
    return ("calls the breakout order senseless, but " + order_mechanics(p, o)
            + " A breakout is the correct order for a break of a level, short or long.")


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
        mech = _mechanics_conflict(why, p, o)
        if mech:
            findings.append(_finding("mechanics_contradiction", "pick_review", t, mech, "noted"))
            notes.append(mech)
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

    # -- plan notes: a sentence about a ticker's resting order read as a timing choice
    mech_notes = []
    for t, p in pick_by_ticker.items():
        for sent in (s for s in _SENTENCE.split(notes_txt) if _mentions(s, t)):
            mech = _mechanics_conflict(sent, p, by_pick.get(p["pick_id"]) or {})
            if mech:
                findings.append(_finding("mechanics_contradiction", "plan_notes", t, mech, "noted"))
                mech_notes.append(f"{t}: {mech}")
    if mech_notes:
        review["plan_notes"] = str(review.get("plan_notes") or "") + " [AUDIT: " + "; ".join(mech_notes) + "]"

    # -- lessons and new ideas: a failing one never becomes memory
    def gate(items: list, where: str, fields: tuple[str, ...]) -> tuple[list, int]:
        kept, dropped = [], 0
        for item in items or []:
            if not isinstance(item, dict):
                kept.append(item)
                continue
            body = " ".join(str(item.get(f) or "") for f in fields)
            reasons = []
            checks = []
            for t in pick_by_ticker:
                if not _mentions(body, t):
                    continue
                p, o = facts(t)
                conflict = _volume_conflict(body, o)
                if conflict:
                    reasons.append(f"{t}: {conflict}")
                    checks.append("volume_contradiction")
                if outcome_class(realized_r(o)) == "scratch" and _RESULT_WORD.search(body):
                    reasons.append(f"{t}: cites a scratch ({realized_r(o):+.2f}R) as a result")
                    checks.append("scratch_as_result")
                mech = _mechanics_conflict(body, p, o) if p else None
                if mech:
                    reasons.append(f"{t}: {mech}")
                    checks.append("mechanics_contradiction")
            if reasons:
                dropped += 1
                findings.append(_finding(
                    "volume_contradiction" if "participation" in reasons[0] else checks[0],
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
