"""
D6 audit precision (2026-10-18). Replayed over production's 140 stored pick reviews, the
mechanics_contradiction rule flagged 9: AMAT 10-16 and INTC 10-15, the incidents it was
built for, and 7 more. What can be read of those 7 shows two kinds of false positive:

  * an order that was already through its level when it went live. CMC 10-15 ("Entry 66.06
    filled @09:45 ..."): a buy limit at 66.50 that was above the market at 09:45, so it filled
    at once at the 09:45 open. That order was a market order in effect (live_monitor sends a
    crossed breakout as one), so the go-live time, a desk choice, set its fill time, and
    calling it chased or mistimed is a fair judgment. NKE 10-02 (09:30, before entry windows
    existed), S 10-13 (09:45) and CMC 10-15 (09:45) each filled on their first live bar,
    by their own notes;
  * criticism of the setup LABEL, not of the stop order. NVDA 10-05: "... so labeling as
    breakout is ...".

A sentence that only states when a resting order filled ("the fill came 97 minutes after
the open") is true by the numbers, and this module fires only on claims the numbers
contradict. "Entered N minutes after the open", the desk-decision phrase the fix-it spec
names, stays flagged.
"""
from __future__ import annotations

import json
import pathlib
from datetime import date, datetime

import pytest

from agent import review_audit
from agent.grading import simulate_pick

BARS = json.loads((pathlib.Path(__file__).parent / "fixtures" / "shadow_day_bars.json")
                  .read_text(encoding="utf-8"))
D1015, D1016 = date(2026, 10, 15), date(2026, 10, 16)


def _real(key, d):
    return [{"t": datetime.combine(d, datetime.strptime(r[0], "%H:%M").time()), "o": r[1],
             "h": r[2], "l": r[3], "c": r[4], "v": r[5]} for r in BARS[f"{key}-{d}"]]


def _cmc():
    pick = {"pick_id": 7, "ticker": "CMC", "direction": "long", "entry_type": "limit",
            "setup_type": "catalyst_momentum", "entry_price": 66.5, "stop_price": 64.0,
            "target_price": 72.0, "time_stop": "by 14:00", "entry_not_before": "09:45",
            "exploration_lane": "coverage"}
    return pick, simulate_pick(pick, _real("CMC", D1015), None, slippage_bps=5, session_date=D1015)


def _intc(**kw):
    pick = {"pick_id": 2, "ticker": "INTC", "direction": "short", "entry_type": "breakout",
            "entry_price": 92.0, "stop_price": 96.0, "target_price": 86.0,
            "time_stop": "by 14:30", "entry_not_before": "09:45", "exploration_lane": "coverage", **kw}
    return pick, simulate_pick(pick, _real("INTC", D1015), None, slippage_bps=5, session_date=D1015)


def _amat():
    pick = {"pick_id": 1, "ticker": "AMAT", "direction": "short", "entry_type": "breakout",
            "entry_price": 418.0, "stop_price": 433.0, "target_price": 400.0,
            "time_stop": "by 14:00", "entry_not_before": "09:45", "exploration_lane": "coverage"}
    return pick, simulate_pick(pick, _real("AMAT", D1016), None, slippage_bps=5, session_date=D1016)


def _flags(why, pick, out):
    review = {"plan_notes": "", "pick_reviews": [{"pick_id": pick["pick_id"], "ticker": pick["ticker"],
                                                  "grade": "C", "why": why}]}
    return [f["check"] for f in review_audit.audit(review, [pick], [out])["findings"]]


def _gapped_short(hhmm, **kw):
    """A breakout short at 92.00 filled at 91.20, a gap of 0.87% through its level."""
    pick = {"pick_id": 4, "ticker": "XYZ", "direction": "short", "entry_type": "breakout",
            "entry_price": 92.0, "stop_price": 96.0, "target_price": 86.0, "entry_not_before": "09:45", **kw}
    h, m = map(int, hhmm.split(":"))
    out = {"pick_id": 4, "triggered": True, "fill_time": datetime(2026, 10, 15, h, m),
           "entry_fill": 91.20, "exit_reason": "stop", "r_planned": -1.0}
    return pick, out


# ------------------------------------------------- an order through its level at go-live

def test_cmc_a_limit_already_through_its_level_when_it_went_live_is_a_market_order_in_effect():
    pick, out = _cmc()
    assert out["triggered"] and out["fill_time"].strftime("%H:%M") == "09:45"
    assert out["entry_fill"] == pytest.approx(66.063, abs=0.001)     # the 09:45 open, 66.03, plus slippage
    assert review_audit.order_mechanics(pick, out) == (
        "resting buy limit at 66.50 from 09:45; already through 66.50 when the order went live "
        "(price gapped past it: filled 66.06), so it filled at once at 09:45: in effect a market "
        "order sent at 09:45. The go-live time set this fill time, not price reaching the level.")


CMC_NOTE = ("Thesis partially right, structure lazy. Entry 66.06 filled @09:45 chasing a falling open, "
            "and the stop 1.29 ATR from entry is absurdly wide.")


def test_cmc_judging_the_timing_of_that_fill_is_fair():
    pick, out = _cmc()
    assert _flags(CMC_NOTE, pick, out) == []


def test_the_same_words_about_a_fill_the_tape_timed_are_still_flagged():
    pick, out = _intc()
    assert out["fill_time"].strftime("%H:%M") == "09:49"            # 92.00 broke four minutes after go-live
    assert _flags("Entry filled @09:49 chasing a falling open.", pick, out) == ["mechanics_contradiction"]


def test_a_first_bar_fill_at_the_level_is_still_the_tapes_timing():
    pick, out = _intc(entry_price=93.2)          # the 09:45 bar opens 93.53 and trades down through 93.20
    assert out["fill_time"].strftime("%H:%M") == "09:45"
    assert out["entry_fill"] == pytest.approx(93.2 * 0.9995, abs=0.001)    # at the level, less slippage
    line = review_audit.order_mechanics(pick, out)
    assert "in effect a market order" not in line and "when price first traded through 93.20" in line
    assert _flags("The entry was chasing the open.", pick, out) == ["mechanics_contradiction"]


def test_a_gap_through_the_level_later_in_the_session_is_still_the_tapes_timing():
    pick, out = _gapped_short("10:12")
    line = review_audit.order_mechanics(pick, out)
    assert "(price gapped past it: filled 91.20)" in line and "in effect a market order" not in line
    assert _flags("The entry chased the breakdown.", pick, out) == ["mechanics_contradiction"]


def test_a_breakout_already_through_its_level_at_go_live_says_so():
    pick, out = _gapped_short("09:45")
    assert review_audit.order_mechanics(pick, out) == (
        "breakout short = sell stop at 92.00, live from 09:45; already through 92.00 when the order "
        "went live (price gapped past it: filled 91.20), so it filled at once at 09:45: in effect a "
        "market order sent at 09:45. The go-live time set this fill time, not the level breaking.")


def test_before_entry_windows_the_open_is_the_go_live_bar():
    # NKE 10-02: no entry_not_before (every pick before 2026-10-02), filled on the 09:30 open
    pick, out = _gapped_short("09:30", entry_not_before=None)
    line = review_audit.order_mechanics(pick, out)
    assert line.startswith("breakout short = sell stop at 92.00, live from the open; already through 92.00")
    assert "in effect a market order sent at 09:30" in line
    assert _flags("Structure flaw: the entry was mistimed, chasing the gap down at the open.", pick, out) == []
    # with a 09:45 window, a 09:30 print is not the go-live bar (and cannot fill there at all)
    windowed, out = _gapped_short("09:30")
    assert "in effect a market order" not in review_audit.order_mechanics(windowed, out)


def test_a_breakout_already_through_its_level_at_go_live_may_be_called_senseless():
    pick, out = _gapped_short("09:45")
    sentence = "The breakout entry makes no sense here: the level had broken before the order went live."
    assert _flags(sentence, pick, out) == []
    late_pick, late_out = _gapped_short("10:12")
    assert _flags(sentence, late_pick, late_out) == ["mechanics_contradiction"]


def test_friction_past_a_level_is_not_the_market_being_through_it():
    # the simulator worsens every fill by its slippage: a limit filled WORSE than its price
    # was never through it at go-live, however large the friction
    pick = {"pick_id": 8, "ticker": "XYZ", "direction": "long", "entry_type": "limit",
            "entry_price": 100.0, "stop_price": 98.0, "target_price": 104.0, "entry_not_before": "09:45"}
    out = {"triggered": True, "fill_time": datetime(2026, 10, 15, 9, 45), "entry_fill": 100.20}
    assert review_audit._went_live_through(pick, out) is False
    assert review_audit._went_live_through(pick, {**out, "entry_fill": 99.80}) is True


@pytest.mark.parametrize("change, flags", [
    ({"triggered": False}, []),                              # nothing filled: no fill to call late
    ({"exit_reason": "invalid_fill"}, []),                   # not a trade
    ({"entry_fill": None}, ["mechanics_contradiction"]),     # no fill price: the gap is unknown
    ({"fill_time": None}, ["mechanics_contradiction"]),      # no fill time: the go-live bar is unknown
])
def test_without_a_known_fill_at_go_live_nothing_is_spared(change, flags):
    pick, out = _gapped_short("09:45")
    out = {**out, **change}
    assert review_audit._went_live_through(pick, out) is False
    assert _flags("The entry chased the breakdown.", pick, out) == flags


# ----------------------------------------------------------------- the setup label

NVDA_NOTE = ("Structure lazy: the thesis admits a breakout setup requires 3+ prior touches; this is "
             "more of a momentum continuation, so labeling it as a breakout is self-contradictory.")


def test_criticising_the_setup_label_is_not_calling_the_order_senseless():
    pick, out = _amat()                              # a breakout order the tape filled at 11:07
    assert _flags(NVDA_NOTE, pick, out) == []


@pytest.mark.parametrize("sentence", [
    # INTC 10-15's own words, as the incident report quotes them: "entry logic makes no sense"
    "Shorting the breakout at 92.00, the entry logic makes no sense for a stock already down 4%.",
    "A breakout short makes no sense on a stock already this weak.",
    "A sell stop below the level is illogical for a short.",
    "Entering on a breakout makes no sense when you are short.",
    "The breakout order does not make sense for a fade.",
])
def test_calling_the_breakout_order_senseless_is_still_flagged(sentence):
    pick, out = _intc()
    assert _flags(sentence, pick, out) == ["mechanics_contradiction"]


# ------------------------------------------------------------ a fill time stated as fact

@pytest.mark.parametrize("sentence", [
    "The fill came 97 minutes after the open.",
    "Filled at 11:07, 97 minutes into the session, when 418.00 broke.",
])
def test_a_fill_time_stated_as_a_fact_is_not_flagged(sentence):
    pick, out = _amat()
    assert _flags(sentence, pick, out) == []


@pytest.mark.parametrize("sentence", [
    "AMAT entered 97 minutes after the open.",       # the fix-it spec's phrase
    "Got in 97 minutes after the open, well after the move.",
    "The entry came 97 minutes late.",
])
def test_the_desk_decision_phrases_are_still_flagged(sentence):
    pick, out = _amat()
    assert _flags(sentence, pick, out) == ["mechanics_contradiction"]


# ----------------------------------------------------------- what the replay can quote

def test_the_hit_names_the_phrase_and_the_sentence():
    pick, out = _amat()
    assert review_audit._mechanics_hit("Thesis fine. Entry at 11:07 came 97 minutes late, chasing. Stop held.",
                                       pick, out) == (
        "timing", "Entry at 11:07 came 97 minutes late", "Entry at 11:07 came 97 minutes late, chasing.")
    pick, out = _intc()
    assert review_audit._mechanics_hit("Weak tape. A breakout short makes no sense here.", pick, out) == (
        "senseless", "makes no sense", "A breakout short makes no sense here.")
    assert review_audit._mechanics_hit("Weak tape. The stop held.", pick, out) is None
