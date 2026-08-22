"""
Pinning tests for the invalid_fill/execution_anomaly enforcement in
agent/loops/evening.py -- the part of the fix that closes the gap between
"the LLM was TOLD not to grade/lesson off a bad fill" and "the code actually
stops it if the LLM ignores that instruction."
"""
from __future__ import annotations

from agent.loops.evening import _all_fills_tainted, _drop_invalid_fill_reviews, _pick_for_review


def _pick(pick_id, ticker="TEST", setup_type="gap_go"):
    return {"pick_id": pick_id, "ticker": ticker, "setup_type": setup_type}


def _outcome(pick_id, triggered=True, exit_reason="target", execution_anomaly=False):
    return {"pick_id": pick_id, "triggered": triggered, "exit_reason": exit_reason,
            "execution_anomaly": execution_anomaly}


# ------------------------------------------------------------- _pick_for_review

def test_pick_for_review_resolves_by_pick_id_first():
    picks = [_pick(1, "AAA"), _pick(2, "BBB")]
    assert _pick_for_review({"pick_id": 2, "ticker": "wrong"}, picks)["ticker"] == "BBB"


def test_pick_for_review_falls_back_to_ticker():
    picks = [_pick(1, "AAA"), _pick(2, "BBB")]
    assert _pick_for_review({"ticker": "bbb"}, picks)["pick_id"] == 2


# ------------------------------------------------------ _drop_invalid_fill_reviews

def test_drop_invalid_fill_reviews_strips_graded_invalid_fill_pick():
    picks = [_pick(4, "MARA")]
    outcomes = [_outcome(4, triggered=False, exit_reason="invalid_fill")]
    review = {"pick_reviews": [{"pick_id": 4, "ticker": "MARA", "grade": "A"}]}
    out = _drop_invalid_fill_reviews(review, picks, outcomes)
    assert out["pick_reviews"] == []


def test_drop_invalid_fill_reviews_keeps_clean_and_anomalous_picks():
    picks = [_pick(1, "AAA"), _pick(2, "BBB")]
    outcomes = [_outcome(1, exit_reason="target"),
                _outcome(2, exit_reason="stop", execution_anomaly=True)]
    review = {"pick_reviews": [{"pick_id": 1, "ticker": "AAA", "grade": "B"},
                               {"pick_id": 2, "ticker": "BBB", "grade": "C"}]}
    out = _drop_invalid_fill_reviews(review, picks, outcomes)
    assert len(out["pick_reviews"]) == 2


def test_drop_invalid_fill_reviews_resolves_by_ticker_when_pick_id_missing():
    picks = [_pick(4, "MARA")]
    outcomes = [_outcome(4, triggered=False, exit_reason="invalid_fill")]
    review = {"pick_reviews": [{"ticker": "mara", "grade": "A"}]}  # no pick_id, lowercase ticker
    out = _drop_invalid_fill_reviews(review, picks, outcomes)
    assert out["pick_reviews"] == []


# ------------------------------------------------------------- _all_fills_tainted

def test_all_fills_tainted_true_when_only_pick_is_invalid_fill():
    outcomes = [_outcome(4, triggered=False, exit_reason="invalid_fill")]
    assert _all_fills_tainted(outcomes) is True


def test_all_fills_tainted_true_when_only_pick_is_execution_anomaly():
    outcomes = [_outcome(1, triggered=True, exit_reason="target", execution_anomaly=True)]
    assert _all_fills_tainted(outcomes) is True


def test_all_fills_tainted_false_with_one_clean_triggered_pick():
    outcomes = [_outcome(4, triggered=False, exit_reason="invalid_fill"),
                _outcome(1, triggered=True, exit_reason="target", execution_anomaly=False)]
    assert _all_fills_tainted(outcomes) is False


def test_all_fills_tainted_false_when_no_picks_ever_filled():
    # a real, clean no-trigger day -- untriggered picks are legitimate lesson
    # material (missed_move_r etc), not a taint signal
    outcomes = [_outcome(1, triggered=False, exit_reason="no_trigger")]
    assert _all_fills_tainted(outcomes) is False


def test_all_fills_tainted_false_on_no_trade_day():
    assert _all_fills_tainted([]) is False
