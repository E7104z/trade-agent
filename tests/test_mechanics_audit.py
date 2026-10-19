"""
D6 (docs/FIX_PHASE0.md): the grader is told how its orders work, and what it writes about
them is checked against that.

2026-10-16 the evening review said of AMAT (a breakout short, filled 11:07 when 418.00
broke) that it "entered 97 minutes late ... chasing", and of INTC 10-15 (a breakout short:
a sell stop below the level) that the breakout "makes no sense". Neither sentence has a
wrong number in it, so the numeric audit passed both. Nothing told the grader that a
breakout is a stop order and that a resting order's fill time is the tape's.

The fills below are simulated from the real minute bars of those two sessions
(tests/fixtures/shadow_day_bars.json).
"""
from __future__ import annotations

import json
import pathlib
from datetime import date, datetime

import pytest

from agent import llm, review_audit
from agent.grading import simulate_pick

BARS = json.loads((pathlib.Path(__file__).parent / "fixtures" / "shadow_day_bars.json")
                  .read_text(encoding="utf-8"))


def _real(key, d):
    return [{"t": datetime.combine(d, datetime.strptime(r[0], "%H:%M").time()), "o": r[1],
             "h": r[2], "l": r[3], "c": r[4], "v": r[5]} for r in BARS[f"{key}-{d}"]]


D1015, D1016 = date(2026, 10, 15), date(2026, 10, 16)


def _amat():
    pick = {"pick_id": 1, "ticker": "AMAT", "direction": "short", "entry_type": "breakout",
            "entry_price": 418.0, "stop_price": 433.0, "target_price": 400.0,
            "time_stop": "by 14:00", "entry_not_before": "09:45", "exploration_lane": "coverage"}
    return pick, simulate_pick(pick, _real("AMAT", D1016), None, slippage_bps=5, session_date=D1016)


def _intc():
    pick = {"pick_id": 2, "ticker": "INTC", "direction": "short", "entry_type": "breakout",
            "entry_price": 92.0, "stop_price": 96.0, "target_price": 86.0,
            "time_stop": "by 14:30", "entry_not_before": "09:45", "exploration_lane": "coverage"}
    return pick, simulate_pick(pick, _real("INTC", D1015), None, slippage_bps=5, session_date=D1015)


# the sentences, with the fragments the incident report quotes
AMAT_SENTENCE = ("Entry at 11:07 came 97 minutes late, after the move was already underway: this "
                 "was chasing a breakdown, not entering at a real level.")
INTC_SENTENCE = ("A breakout entry on a short thesis makes no sense here: you short weakness, you "
                 "do not wait for the stock to break out.")
CORRECT = ("The breakout short is a sell stop at 418.00, live from 09:45; it filled at 11:07 when "
           "418.00 broke, so the timing was the tape's. The 433.00 stop was 15.00 away and the "
           "14:00 time stop cut the trade short.")


# --------------------------------------------------------------- the computed line

def test_the_amat_breakout_short_line_is_the_spec_line():
    pick, out = _amat()
    assert out["triggered"] and out["fill_time"].strftime("%H:%M") == "11:07"
    assert review_audit.order_mechanics(pick, out) == (
        "breakout short = sell stop at 418.00, live from 09:45; filled 11:07 when price first "
        "traded through 418.00. The fill time is when the level broke, not a decision.")


def test_a_resting_limit_line_says_no_one_chose_the_fill_time():
    pick = {"entry_type": "limit", "direction": "short", "entry_price": 64.0, "entry_not_before": "09:45"}
    out = {"triggered": True, "fill_time": datetime(2026, 10, 15, 10, 12), "entry_fill": 64.0}
    assert review_audit.order_mechanics(pick, out) == (
        "resting sell limit at 64.00 from 09:45; filled 10:12 whenever price reached it. "
        "No one chose the fill time.")


def test_the_buy_side_lines():
    out = {"triggered": True, "fill_time": datetime(2026, 10, 15, 10, 12), "entry_fill": 100.0}
    assert review_audit.order_mechanics(
        {"entry_type": "breakout", "direction": "long", "entry_price": 100.0,
         "entry_not_before": "09:45"}, out).startswith("breakout long = buy stop at 100.00, live from 09:45")
    assert review_audit.order_mechanics(
        {"entry_type": "limit", "direction": "long", "entry_price": 100.0,
         "entry_not_before": "09:45"}, out).startswith("resting buy limit at 100.00 from 09:45; filled 10:12")


def test_a_gap_through_fill_says_where_it_filled():
    out = {"triggered": True, "fill_time": datetime(2026, 10, 15, 9, 45), "entry_fill": 91.20}
    line = review_audit.order_mechanics(
        {"entry_type": "breakout", "direction": "short", "entry_price": 92.0,
         "entry_not_before": "09:45"}, out)
    assert "(price gapped past it: filled 91.20)" in line


def test_an_unfilled_limit_that_was_only_touched_says_a_touch_is_not_a_fill():
    pick = {"entry_type": "limit", "direction": "short", "entry_price": 54.0,
            "entry_not_before": "09:45", "time_stop": "by 14:00"}
    line = review_audit.order_mechanics(pick, {"triggered": False, "touch_only_bars": 2})
    assert line == ("resting sell limit at 54.00 from 09:45; never filled: price touched 54.00 on 2 "
                    "bar(s) but never traded through it (a touch is not a fill), and it was "
                    "cancelled at 14:00.")
    untouched = review_audit.order_mechanics(pick, {"triggered": False, "touch_only_bars": 0})
    assert "price never reached 54.00 while the order was live" in untouched


def test_an_unfilled_breakout_and_the_default_cancel_time():
    line = review_audit.order_mechanics(
        {"entry_type": "breakout", "direction": "long", "entry_price": 101.0,
         "entry_not_before": "09:45"}, {"triggered": False})
    assert line == ("breakout long = buy stop at 101.00, live from 09:45; never filled: price never "
                    "traded through 101.00 while the order was live, and it was cancelled at the "
                    "15:55 flatten.")


def test_an_open_entry_is_the_one_timing_the_desk_chose():
    line = review_audit.order_mechanics(
        {"entry_type": "open", "direction": "long", "entry_price": 100.0},
        {"triggered": True, "entry_fill": 100.05})
    assert "market order sent at the open" in line and "The desk chose this time" in line


def test_an_invalid_fill_is_not_described_as_a_fill():
    line = review_audit.order_mechanics(
        {"entry_type": "limit", "direction": "long", "entry_price": 100.0, "entry_not_before": "09:45"},
        {"triggered": True, "exit_reason": "invalid_fill", "fill_time": datetime(2026, 10, 15, 9, 45)})
    assert "invalid" in line and "filled 09:45" not in line


def test_the_bundle_section_has_one_line_per_pick():
    amat, amat_out = _amat()
    intc, intc_out = _intc()
    md = review_audit.mechanics_md([amat, intc], [amat_out, intc_out])
    lines = md.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("- AMAT (pick_id=1, breakout): breakout short = sell stop at 418.00")
    assert lines[1].startswith("- INTC (pick_id=2, breakout): breakout short = sell stop at 92.00")
    assert review_audit.mechanics_md([], []) == ""


# ---------------------------------------------------------------- the audit rule

def _review(why, lesson=None, plan_notes=""):
    review = {"plan_grade": "B", "plan_notes": plan_notes,
              "pick_reviews": [{"pick_id": 1, "ticker": "AMAT", "grade": "C", "why": why}]}
    if lesson:
        review["lessons"] = [lesson]
    return review


def _audit(why, **kw):
    pick, out = _amat()
    return review_audit.audit(_review(why, **kw), [pick], [out])


def test_the_amat_sentence_is_flagged_and_names_the_mechanics():
    res = _audit(AMAT_SENTENCE)
    f = [x for x in res["findings"] if x["check"] == "mechanics_contradiction"]
    assert len(f) == 1 and f[0]["where"] == "pick_review" and f[0]["ticker"] == "AMAT"
    assert f[0]["action"] == "noted"
    assert "filled 11:07 when price first traded through 418.00" in f[0]["why"]
    why = res["review"]["pick_reviews"][0]["why"]
    assert why.startswith(AMAT_SENTENCE) and "[AUDIT:" in why     # the grader's words stay, annotated


def test_the_intc_sentence_is_flagged():
    pick, out = _intc()
    review = {"plan_notes": "", "pick_reviews": [{"pick_id": 2, "ticker": "INTC", "grade": "D",
                                                  "why": INTC_SENTENCE}]}
    res = review_audit.audit(review, [pick], [out])
    f = [x for x in res["findings"] if x["check"] == "mechanics_contradiction"]
    assert len(f) == 1 and f[0]["ticker"] == "INTC"
    assert "breakout order senseless" in f[0]["why"] and "sell stop at 92.00" in f[0]["why"]


def test_the_intc_sentence_is_flagged_even_when_nothing_filled():
    pick, out = _intc()
    pick = {**pick, "entry_price": 80.0}                     # a level the tape never reached
    out = simulate_pick(pick, _real("INTC", D1015), None, slippage_bps=5, session_date=D1015)
    assert out["triggered"] is False
    review = {"plan_notes": "", "pick_reviews": [{"pick_id": 2, "ticker": "INTC", "grade": "D",
                                                  "why": INTC_SENTENCE}]}
    res = review_audit.audit(review, [pick], [out])
    assert [x["check"] for x in res["findings"]] == ["mechanics_contradiction"]


def test_a_lesson_built_on_the_amat_sentence_is_dropped():
    lesson = {"lesson": "Breakout shorts that fill 97 minutes into the session are chasing the move.",
              "evidence": "AMAT 2026-10-16 entered 97 minutes late, chasing a breakdown."}
    res = _audit(AMAT_SENTENCE, lesson=lesson)
    assert res["review"]["lessons"] == [] and res["dropped_lessons"] == 1
    drop = [x for x in res["findings"] if x["action"] == "dropped"]
    assert len(drop) == 1 and drop[0]["check"] == "mechanics_contradiction"


def test_a_plan_note_reading_a_resting_fill_as_a_decision_is_annotated():
    res = _audit(CORRECT, plan_notes="AMAT chased the breakdown and entered 97 minutes after the open.")
    f = [x for x in res["findings"] if x["where"] == "plan_notes"]
    assert len(f) == 1 and f[0]["check"] == "mechanics_contradiction"
    assert "[AUDIT:" in res["review"]["plan_notes"]


def test_a_correct_sentence_about_the_mechanics_is_not_flagged():
    res = _audit(CORRECT)
    assert res["findings"] == []
    assert res["review"]["pick_reviews"][0]["why"] == CORRECT


@pytest.mark.parametrize("sentence", [
    "It filled 97 minutes after the open because that is when 418.00 broke: not a decision.",
    "Entry came 97 minutes late only in clock terms; the sell stop fills when the level breaks, "
    "and no one chose the fill time.",
    "This was not a chase: the order was resting and filled when price reached it.",
    "The stop at 433.00 was never threatened and the trade kept running after the time stop.",
    "The breakout short was the right order for a breakdown below the premarket low.",
])
def test_sentences_that_state_or_respect_the_mechanics_are_not_flagged(sentence):
    assert _audit(sentence)["findings"] == []


def test_chasing_is_still_fair_game_for_a_market_entry():
    pick = {"pick_id": 1, "ticker": "AMAT", "direction": "long", "entry_type": "open",
            "entry_price": 100.0, "stop_price": 98.0, "target_price": 104.0}
    out = {"pick_id": 1, "triggered": True, "entry_fill": 103.0, "exit_reason": "stop",
           "r_planned": -1.0}
    review = {"plan_notes": "", "pick_reviews": [{"pick_id": 1, "ticker": "AMAT", "grade": "D",
                                                  "why": "Chased the open 97 minutes late."}]}
    assert review_audit.audit(review, [pick], [out])["findings"] == []


def test_a_fill_far_from_the_plan_may_really_have_been_a_chase():
    pick, out = _amat()
    out = {**out, "execution_anomaly": True}            # the order effectively became a market order
    res = review_audit.audit(_review(AMAT_SENTENCE), [pick], [out])
    assert res["findings"] == []


def test_a_late_remark_that_is_not_about_the_entry_is_not_flagged():
    res = _audit("The stop was hit late in the session after a long drift lower.")
    assert res["findings"] == []


def test_lessons_that_name_no_traded_ticker_are_left_alone():
    lesson = {"lesson": "Entries chasing a gap-up in the first 15 minutes lose.", "evidence": "history"}
    res = _audit(CORRECT, lesson=lesson)
    assert res["review"]["lessons"] == [lesson] and res["dropped_lessons"] == 0


def test_the_audit_block_in_the_email_names_the_check():
    res = _audit(AMAT_SENTENCE)
    assert "[mechanics_contradiction] pick_review AMAT" in review_audit.text(res)


# ------------------------------------------------------- the prompt and the bundle

def test_the_grader_prompt_is_version_11_and_teaches_the_mechanics():
    assert llm.prompt_version("evening_grader") == "11"
    text = llm.load_prompt("evening_grader", {})
    assert "read the ORDER MECHANICS section" in text
    assert "nobody chose the fill time" in text
    assert 'never call such a fill "late", "chasing"' in text
    assert "sell stop below the level is the correct order for a breakdown" in text
    # the rubric no longer lets "chased" be said of a stop or limit fill
    assert "target too greedy, entry slightly chased" not in text
    assert "an `open` entry slightly chased" in text


def test_the_evening_bundle_carries_the_order_mechanics_section(tmp_path, monkeypatch):
    from agent import ledger
    from agent.config import load_config
    from agent.loops import evening

    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "m.duckdb")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("agent.loops.evening.send_email", lambda *a, **k: None)
    monkeypatch.setattr("agent.loops.evening.render_dashboard", lambda *a, **k: None)
    con = ledger.connect()
    plan = {"no_trade": False, "market_stance": "t", "total_risk_r": 0.25, "rejected": [],
            "notes_for_evening": "", "picks": [{
                "ticker": "TEST", "direction": "short", "setup_type": "breakdown_short",
                "catalyst": "x", "thesis_final": "x", "conviction": 3, "size_r": 0.25,
                "entry_plan": {"type": "breakout", "price": 95.0}, "entry_price": 95.0,
                "stop_price": 98.0, "target_price": 90.0, "target2_price": None,
                "time_stop": "by 14:00", "invalidation": "x", "entry_not_before": "09:45"}]}
    ledger.save_daily_plan(con, D1016, plan, {"candidates": []}, "chop")
    con.close()

    captured = {}

    class _Grader:
        def __init__(self, cfg):
            pass

        def call(self, **kw):
            captured["user"] = kw["user"]

            class R:
                parsed = {"plan_grade": "B", "pick_reviews": [], "lessons": []}
                usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0, "cost_usd": 0.0}
                text = ""
            return R()

    monkeypatch.setattr(evening, "LLMClient", _Grader)
    cfg = load_config()
    cfg.mock = False
    cfg.offline = True
    evening.run(cfg, run_date=D1016, force=True)
    user = captured["user"]
    assert "## ORDER MECHANICS" in user
    assert "- TEST (pick_id=" in user and "breakout short = sell stop at 95.00, live from 09:45" in user
    # it sits between the computed tape facts and the execution ground truth
    assert user.index("## ORDER MECHANICS") < user.index("## EXECUTION GROUND TRUTH")

    # the run records which grader prompt wrote the review, and the audit's counts up front
    con = ledger.connect()
    raw = con.execute("SELECT stages FROM runs WHERE loop = 'evening' AND run_date = ? "
                      "ORDER BY run_id DESC LIMIT 1", [D1016]).fetchone()[0]
    con.close()
    stages = json.loads(raw)
    assert stages["grader_prompt_version"] == "11"
    assert stages["audit"]["findings"] == 0 and stages["audit"]["by_check"] == {}
    assert list(stages["audit"]).index("by_check") < list(stages["audit"]).index("items")


@pytest.mark.parametrize("sentence", [
    # "The fill came 97 minutes after the open." was here until 2026-10-18: a fill time stated
    # as a fact the numbers confirm is not a claim they contradict (tests/test_mechanics_precision.py)
    "The pick entered right after the open on a stale level.",
    "The entry was too late.",
    "A late fill on a stretched tape.",
    "The desk was chasing the entry.",
    "It chased the breakdown instead of waiting.",
    "Got in 97 minutes late.",
])
def test_each_way_of_calling_a_resting_fill_a_decision_is_flagged(sentence):
    assert [f["check"] for f in _audit(sentence)["findings"]] == ["mechanics_contradiction"]


@pytest.mark.parametrize("sentence", [
    "Price kept chasing lower highs all morning.",
    "The setup arrived late in the day and the move was already done.",
])
def test_timing_words_that_are_not_about_the_entry_are_not_flagged(sentence):
    assert _audit(sentence)["findings"] == []


def test_one_bad_sentence_is_flagged_even_beside_a_correct_one():
    res = _audit(AMAT_SENTENCE + " " + CORRECT)
    assert [f["check"] for f in res["findings"]] == ["mechanics_contradiction"]


def test_an_unfilled_order_has_no_fill_to_call_late():
    pick, out = _intc()
    pick = {**pick, "entry_price": 80.0}                     # a level the tape never reached
    out = simulate_pick(pick, _real("INTC", D1015), None, slippage_bps=5, session_date=D1015)
    assert out["triggered"] is False
    review = {"plan_notes": "", "pick_reviews": [{"pick_id": 2, "ticker": "INTC", "grade": "C",
                                                  "why": "The entry was too late, chasing weakness."}]}
    assert review_audit.audit(review, [pick], [out])["findings"] == []


def test_the_senseless_breakout_rule_is_for_breakout_orders_only():
    pick = {"pick_id": 3, "ticker": "RKLB", "direction": "short", "entry_type": "limit",
            "entry_price": 54.0, "stop_price": 56.5, "target_price": 48.0, "entry_not_before": "09:45"}
    out = {"pick_id": 3, "triggered": True, "entry_fill": 54.0, "exit_reason": "stop", "r_planned": -1.0}
    review = {"plan_notes": "", "pick_reviews": [{"pick_id": 3, "ticker": "RKLB", "grade": "C",
              "why": "A breakout of the opening range makes no sense as a thesis on this tape."}]}
    assert review_audit.audit(review, [pick], [out])["findings"] == []


def test_a_digit_led_timing_claim_is_caught_even_after_an_earlier_ambiguous_word():
    # "Chased" matches first and, with no entry word in the sentence, would not count alone;
    # the "97 minutes late" later in the same sentence is a timing claim by itself.
    res = _audit("Chased it, then 97 minutes late.")
    assert [f["check"] for f in res["findings"]] == ["mechanics_contradiction"]
    assert '"97 minutes late"' in res["findings"][0]["why"]


def test_judgment_is_per_sentence_so_a_tape_aware_remark_does_not_excuse_a_timing_claim():
    res = _audit(AMAT_SENTENCE + " Nobody chose the fill time.")
    assert [f["check"] for f in res["findings"]] == ["mechanics_contradiction"]
