"""
specs/VERIFICATION_PRACTICE_v1.md -- the grader's claims are checked against
the computed numbers before anything is saved (agent/review_audit.py), and a
runtime change ships only after a passing rehearsal (agent/rehearsal.py).
"""
from __future__ import annotations

import json
from datetime import date

import duckdb
import pytest

from agent import ledger, rehearsal, review_audit, selfcheck

D = date(2026, 10, 5)

PICKS = [
    {"pick_id": 1, "ticker": "ALEC", "exploration_lane": None},
    {"pick_id": 2, "ticker": "NVDA", "exploration_lane": "coverage"},
    {"pick_id": 3, "ticker": "CWH", "exploration_lane": None},
]
OUTCOMES = [
    # ALEC 10/05: RVOL 2.49x at 10:00, 6.88x at the close; +0.01R
    {"pick_id": 1, "triggered": True, "r_planned": 0.01, "rvol_at_1000": 2.49, "rvol_close": 6.88},
    {"pick_id": 2, "triggered": True, "r_planned": -0.06, "rvol_at_1000": 0.16, "rvol_close": 0.90},
    {"pick_id": 3, "triggered": True, "r_planned": -1.0, "rvol_at_1000": 0.5, "rvol_close": 0.7},
]


def _audit(**review):
    return review_audit.audit(review, PICKS, OUTCOMES)


def test_one_and_done_against_rising_rvol_is_caught_and_the_lesson_dropped():
    lesson = {"lesson": "Low-float gap-ups whose participation is one-and-done fade all day",
              "evidence": "ALEC participation was one-and-done after the open"}
    r = _audit(lessons=[lesson, {"lesson": "CWH broke down through its trigger", "evidence": "CWH"}])
    assert [l["evidence"] for l in r["review"]["lessons"]] == ["CWH"]
    assert r["dropped_lessons"] == 1
    f = r["findings"][0]
    assert f["check"] == "volume_contradiction" and f["action"] == "dropped"
    assert "4.39 average days of volume traded after 10:00" in f["why"]


def test_weak_volume_is_only_contradicted_by_an_above_average_day():
    r = _audit(pick_reviews=[{"pick_id": 2, "grade": "B", "why": "weak volume at 10:00"},
                             {"pick_id": 1, "grade": "B", "why": "volume was light"}])
    whys = [x["why"] for x in r["review"]["pick_reviews"]]
    assert "[AUDIT" not in whys[0]                       # NVDA closed at 0.90x: not contradicted
    assert "[AUDIT: claims weak participation, but the day traded 6.88x" in whys[1]


def test_a_scratch_called_a_winner_is_noted():
    r = _audit(pick_reviews=[{"pick_id": 1, "grade": "B", "why": "a winner thanks to the time stop"}])
    assert r["findings"][0]["check"] == "scratch_as_result"
    assert "scratch (+0.01R" in r["review"]["pick_reviews"][0]["why"]


def test_a_coverage_pick_graded_down_for_being_taken_keeps_its_grade_with_a_note():
    rv = {"pick_id": 2, "grade": "C", "why": "Label was a stretch; this shouldn't have been taken."}
    r = _audit(pick_reviews=[rv])
    out = r["review"]["pick_reviews"][0]
    assert out["grade"] == "C"                           # never rewritten
    assert "coverage pick the system placed on purpose" in out["why"]
    assert r["findings"][0]["check"] == "test_lane_penalized"
    # The same words on an approved pick are a judgment, not a finding.
    assert not _audit(pick_reviews=[dict(rv, pick_id=3)])["findings"]


def test_forgone_r_read_as_proof_is_noted():
    r = _audit(plan_notes="Net forgone -0.14R confirms the filters are working.")
    assert r["findings"][0]["check"] == "forgone_conclusion"
    assert r["review"]["plan_notes"].endswith("proves nothing]")
    assert not _audit(plan_notes="Net forgone -0.14R across six rejections.")["findings"]


def test_an_idea_built_on_a_misread_is_dropped():
    idea = {"statement": "Fade low-float runners once volume dries up", "origin": "ALEC today",
            "refutation": "mean R <= 0"}
    r = _audit(hypotheses=[idea])
    assert r["review"]["hypotheses"] == [] and r["dropped_ideas"] == 1


def test_a_clean_review_passes_untouched_and_text_is_empty():
    review = {"plan_grade": "B", "plan_notes": "Fine.", "pick_reviews": [
        {"pick_id": 3, "grade": "B", "why": "stopped below the trigger"}],
        "lessons": [{"lesson": "x", "evidence": "CWH"}]}
    r = review_audit.audit(review, PICKS, OUTCOMES)
    assert r["review"] == review and r["findings"] == [] and review_audit.text(r) == ""


def test_the_audit_never_raises():
    r = review_audit.audit({"pick_reviews": [{"pick_id": 1, "why": None}]}, PICKS, [None])
    assert r["findings"][0]["check"] == "audit_error"


def test_the_self_check_reports_the_audit():
    con = duckdb.connect(":memory:")
    con.execute(ledger._SCHEMA)
    for mig in ledger._MIGRATIONS:
        con.execute(mig)
    assert selfcheck.check_grader_audit(con, None, D)["status"] == "info"
    stages = {"audit": {"findings": 2, "dropped_lessons": 1, "dropped_ideas": 0,
                        "items": [{"check": "volume_contradiction"}, {"check": "forgone_conclusion"}]}}
    con.execute("""INSERT INTO runs (loop, run_date, started_at, status, stages)
                   VALUES ('evening', ?, current_timestamp, 'ok', ?)""", [D, json.dumps(stages)])
    r = selfcheck.check_grader_audit(con, None, D)
    assert r["status"] == "warn"
    assert "2 grader claim(s) contradicted the numbers" in r["headline"]
    assert "1 lesson(s) and 0 idea(s) dropped" in r["headline"]


# ------------------------------------------------------------------ rehearsal

def test_deploying_needs_a_passing_rehearsal_of_the_same_runtime_files(tmp_path, monkeypatch):
    monkeypatch.setattr(rehearsal, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(rehearsal, "_head", lambda: ("a" * 40, False))
    monkeypatch.setattr(rehearsal, "_git", lambda *a: (0, ""))      # trees identical
    ok, msg = rehearsal.verify_head()
    assert not ok and "no passing rehearsal" in msg
    (tmp_path / "x.json").write_text(json.dumps(
        {"commit": "b" * 40, "passed": False, "dirty": False, "loops": {}}))
    assert not rehearsal.verify_head()[0]                           # a failed one does not count
    (tmp_path / "y.json").write_text(json.dumps(
        {"commit": "c" * 40, "passed": True, "dirty": False, "finished_at": "now",
         "loops": {"morning": {"verdict": "PASS"}, "evening": {"verdict": "PASS"}}}))
    ok, msg = rehearsal.verify_head()
    assert ok and "morning PASS, evening PASS" in msg
    monkeypatch.setattr(rehearsal, "_git", lambda *a: (1, ""))      # runtime files differ
    assert not rehearsal.verify_head()[0]
    monkeypatch.setattr(rehearsal, "_head", lambda: ("a" * 40, True))
    assert "uncommitted" in rehearsal.verify_head()[1]


@pytest.mark.parametrize("utc, clash", [
    ((2026, 10, 6, 10, 20), "/cron/morning"),            # 25 min before the 10:45 schedule
    ((2026, 10, 6, 11, 40), "/cron/morning"),            # inside Hobby's late-firing hour
    ((2026, 10, 6, 12, 30), "/cron/morning-continue"),
    ((2026, 10, 6, 17, 30), None),                       # 13:30 ET: a quiet window
    ((2026, 10, 10, 10, 50), None),                      # Saturday: no morning cron
    ((2026, 10, 10, 14, 10), "/cron/weekly"),
    ((2026, 10, 7, 3, 20), "/cron/nightly"),
])
def test_a_deploy_never_lands_on_a_cron(utc, clash):
    from datetime import datetime
    got = rehearsal.cron_conflict(datetime(*utc))
    assert (got is None) if clash is None else got.startswith(clash + " ")


def test_arm_b_calls_are_one_attempt_inside_the_budget(monkeypatch):
    import time as _t
    from agent.loops import morning
    seen = {}

    def _call(**kw):
        seen.update(kw)
        return "ok"
    bounded = morning._bounded_call(_call, _t.monotonic() - 100)
    assert bounded(mock_name="morning_risk") == "ok"
    assert seen["attempts"] == 1 and 170 <= seen["timeout"] <= 176
    late = morning._bounded_call(_call, _t.monotonic() - 270)
    with pytest.raises(TimeoutError):
        late(mock_name="morning_risk")


def test_a_rehearsal_cannot_reach_production_email_or_the_broker(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgres://prod")
    with rehearsal._isolated(tmp_path / "x.duckdb"):
        import os
        assert "DATABASE_URL" not in os.environ
        assert ledger.DB_PATH == tmp_path / "x.duckdb"
    assert os.environ["DATABASE_URL"] == "postgres://prod"
    cfg = rehearsal._cfg(paired=True)
    assert cfg.email_configured is False
    assert cfg.get("live_paper")["enabled"] is False
