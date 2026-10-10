"""
Grader grounding (GRADER_GROUNDING_SPEC_v1): no confirmation without a computed fact.

WHY THIS EXISTS
---------------
The evening grader is the daily learning step, and what it asserts becomes
durable memory: confirmations drive lesson promotion, and an A/B grade stamps
a setup's rule confirmation. Several of those assertions were never checked
against anything computed:

  - the output contract asked for a "tape narrative" while the prompt
    conceded the grader is shown no bars at all;
  - a lesson confirmation was accepted with no check that any trade that day
    met the lesson's condition;
  - numbers quoted in a lesson were never compared with the computed values
    the prompt tells the grader to quote.

This module supplies the deterministic halves: a computed tape summary to
replace the narrative, a citation check for quoted numbers, and a scope check
for confirmations. Judgment stays with the grader; the system only refuses to
record a claim that nothing computed supports.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

__all__ = ["tape_summary", "ungrounded_tokens", "validate_lesson_predicate",
           "lesson_in_scope"]


# --------------------------------------------------------------------------
# The computed tape
# --------------------------------------------------------------------------

def _hm(t: Any) -> str:
    if isinstance(t, datetime):
        return t.strftime("%H:%M")
    return str(t)[11:16] if t and len(str(t)) >= 16 else str(t or "?")


def _px(v: Any) -> str:
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "?"


def tape_summary(bars: list[dict] | None, o: dict) -> str:
    """How the pick actually traded, from the bars already fetched. Pure."""
    if not bars:
        return "no intraday bars (no_data) — not observed"
    hi = max(bars, key=lambda b: b.get("h") or float("-inf"))
    lo = min(bars, key=lambda b: b.get("l") or float("inf"))
    parts = [f"open {_px(bars[0].get('o'))}",
             f"HOD {_px(hi.get('h'))} @{_hm(hi.get('t'))}",
             f"LOD {_px(lo.get('l'))} @{_hm(lo.get('t'))}"]
    if o.get("exit_reason") == "invalid_fill":
        parts.append("INVALID FILL (not a trade)")
    elif o.get("triggered"):
        parts.append(f"filled {_px(o.get('entry_fill'))} @{_hm(o.get('fill_time'))}")
        parts.append(f"exit {_px(o.get('exit_price'))} ({o.get('exit_reason')}) "
                     f"@{_hm(o.get('exit_time'))}")
        if o.get("vwap_side_at_fill"):
            parts.append(f"{o['vwap_side_at_fill']} VWAP at fill")
        if o.get("mfe_r") is not None and o.get("mae_r") is not None:
            parts.append(f"MFE {o['mfe_r']:+.2f}R / MAE {o['mae_r']:+.2f}R")
        ft, et = o.get("fill_time"), o.get("exit_time")
        if isinstance(ft, datetime) and isinstance(et, datetime):
            parts.append(f"held {int((et - ft).total_seconds() // 60)} min")
    else:
        parts.append(f"never triggered ({o.get('untriggered_reason') or o.get('exit_reason') or 'no_trigger'})")
    parts.append(f"close {_px(bars[-1].get('c'))}")
    return "; ".join(parts)


# --------------------------------------------------------------------------
# Citation check
# --------------------------------------------------------------------------

# A number carrying a unit that a lesson could only have taken from computed
# data: R multiples, ATR distances, RVOL multiples, percentages.
_UNIT_NUM = re.compile(r"(?<![\w.])([-+−]?\d+(?:\.\d+)?)\s?(R|ATR|x|%)(?![A-Za-z])")
_TIME = re.compile(r"\b(\d{1,2}:\d{2})\b")
# A number right after a comparison is a threshold or a category, not an
# observation ("gaps >15%", "RVOL above 2x") -- not something to verify.
_THRESHOLD_BEFORE = re.compile(r"(?:[<>≤≥]=?|\bover|\bunder|\babove|\bbelow|\bat least|\bat most)\s*$", re.I)
_ANY_NUM = re.compile(r"[-+−]?\d+(?:\.\d+)?")


def _num(s: str) -> float:
    return float(s.replace("−", "-"))


def _decimals(s: str) -> int:
    return len(s.split(".")[1]) if "." in s else 0


def ungrounded_tokens(text: str, corpus: str) -> list[str]:
    """Numbers-with-units and clock times in `text` found nowhere in `corpus`.

    `corpus` is exactly what the grader was shown. A number matches when some
    number in the corpus ROUNDS to it at the precision it was quoted (the
    grader may write 0.8x for a computed 0.804x; it may not write 9.99x
    because a 10:00 timestamp is nearby, nor invent 0.3 ATR). Units are not
    required to match: the corpus prints some fields without them, and a
    false rejection of a correct lesson is not the failure this check exists
    to catch. Clock times are compared as times, never as numbers.
    Thresholds are skipped.
    """
    if not text:
        return []
    corpus = corpus or ""
    corpus_times = set(_TIME.findall(corpus))
    corpus_nums = [_num(m.group(0)) for m in _ANY_NUM.finditer(_TIME.sub(" ", corpus))]
    bad: list[str] = []
    for m in _UNIT_NUM.finditer(text):
        if _THRESHOLD_BEFORE.search(text[:m.start()]):
            continue
        raw = m.group(1)
        v, tol = _num(raw), 0.5 * 10 ** (-_decimals(raw)) + 1e-9
        if not any(abs(c - v) <= tol or abs(abs(c) - abs(v)) <= tol for c in corpus_nums):
            bad.append(m.group(0).strip())
    for t in _TIME.findall(text):
        if t not in corpus_times:
            bad.append(t)
    return bad


# --------------------------------------------------------------------------
# Lesson scope
# --------------------------------------------------------------------------

def validate_lesson_predicate(pred: Any) -> dict | None:
    """A lesson's optional predicate, in rule-predicate shape, or None.

    The lane and dimensions must be ones the system mines. Levels are not
    checked against a batch (the evening has none); a level no trade can have
    simply never matches, which costs the lesson its confirmations, not the
    ledger its integrity.
    """
    from . import buckets
    from .research import MAX_RULE_DIMENSIONS, canonical_predicate
    canon = canonical_predicate(pred)
    if canon is None or canon["lane"] not in buckets.LANES:
        return None
    conds = canon["conditions"]
    if not conds or len(conds) > MAX_RULE_DIMENSIONS:
        return None
    dims = set(buckets.dimensions(canon["lane"]))
    if any(c["dimension"] not in dims for c in conds):
        return None
    return canon


def _regime_matches(lesson_regime: str | None, row_regime: str | None) -> bool:
    if not lesson_regime:
        return True
    a, b = str(lesson_regime).lower(), str(row_regime or "").lower()
    return bool(b) and (a.startswith(b) or b.startswith(a))


def lesson_in_scope(lesson: dict, pick_rows: Iterable[dict]) -> tuple[bool, str]:
    """Did any of today's fill-valid trades fall inside this lesson?

    With a predicate: a row whose buckets match it. Without one: a trade of the lesson's
    setup_type (and regime, when the lesson names one). A lesson with neither
    cannot be confirmed or contradicted by a single evening at all -- only the
    weekly miner, from ledger counts, can act on it.
    """
    import json
    from .research import matches
    pred = lesson.get("predicate_json")
    if isinstance(pred, str) and pred:
        try:
            pred = json.loads(pred)
        except ValueError:
            pred = None
    pred = validate_lesson_predicate(pred) if pred else None
    if pred:
        return (any(matches(pred, r) for r in pick_rows),
                "no trade today fell in the lesson's predicate cell")
    setup = lesson.get("setup_type")
    if not setup:
        return False, "lesson names no setup_type or predicate; only the weekly miner can act on it"
    for r in pick_rows:
        if r.get("setup_type") == setup and _regime_matches(lesson.get("regime"), r.get("regime_tag")):
            return True, ""
    return False, f"no fill-valid {setup} trade today" + (
        f" in regime {lesson.get('regime')}" if lesson.get("regime") else "")
