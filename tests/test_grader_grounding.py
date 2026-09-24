"""
GRADER_GROUNDING_SPEC_v1 — no confirmation without a computed fact behind it.

Pinned here:
  Phase 1  the tape is computed from bars, not narrated by the grader
  Phase 2  quoted numbers/times must appear in what the grader was shown:
           an ungrounded lesson is not written; an ungrounded review keeps its
           grade but cannot confirm a rule
  Phase 3  a lesson update is accepted only when a trade fell inside the
           lesson; confirmations/contradictions are dated and idempotent, and
           the holdout tier is earned on net evidence
  Phase 4  grade inflation is measured against the system's own proxies
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from agent import grounding, ledger
from agent.config import load_config
from agent.loops import evening

ET = timezone(timedelta(hours=-4))
D = date(2026, 8, 24)


def _bars():
    t0 = datetime(2026, 8, 24, 9, 30, tzinfo=ET)
    px = [10.0, 10.4, 10.9, 10.6, 9.8, 10.2]
    return [{"t": t0 + timedelta(minutes=i), "o": p, "h": p + 0.1, "l": p - 0.1, "c": p, "v": 1000}
            for i, p in enumerate(px)]


# ------------------------------------------------------------------ Phase 1

def test_tape_summary_is_computed_and_deterministic():
    b = _bars()
    o = {"triggered": True, "entry_fill": 10.4, "fill_time": b[1]["t"], "exit_price": 10.9,
         "exit_reason": "target", "exit_time": b[2]["t"], "vwap_side_at_fill": "above",
         "mfe_r": 1.2, "mae_r": -0.1}
    s = grounding.tape_summary(b, o)
    assert s == grounding.tape_summary(b, o)
    assert "HOD 11.00 @09:32" in s and "LOD 9.70 @09:34" in s
    assert "filled 10.40 @09:31" in s and "exit 10.90 (target) @09:32" in s
    assert "MFE +1.20R / MAE -0.10R" in s and "held 1 min" in s


def test_tape_summary_for_untriggered_and_missing_bars():
    assert "never triggered" in grounding.tape_summary(_bars(), {"triggered": False,
                                                                 "exit_reason": "no_trigger"})
    assert "not observed" in grounding.tape_summary([], {})


# ------------------------------------------------------------------ Phase 2

CORPUS = ("RVOL@10:00 0.80x ADV20, stop 0.52 ATR from entry, R = +1.40R, "
          "filled 10.40 @09:31, session 62% time above VWAP")


@pytest.mark.parametrize("text, bad", [
    ("RVOL@10:00 0.8x — thin participation", []),
    ("stop was 0.3 ATR — inside noise", ["0.3 ATR"]),
    ("filled at 09:31 and ran", []),
    ("reversed at 10:12", ["10:12"]),
    ("gaps >15% faded all day", []),          # a threshold, not an observation
    ("RVOL above 2x separates winners", []),  # a threshold
    ("the setup worked as designed", []),     # no numbers: nothing to check
    ("paid +1.4R and 62% of the day above VWAP", []),
    ("paid +2.7R", ["+2.7R"]),
])
def test_ungrounded_tokens(text, bad):
    assert grounding.ungrounded_tokens(text, CORPUS) == bad


# ------------------------------------------------------------------ Phase 3

@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "g.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


def test_feedback_is_idempotent_and_contradictions_are_dated(con):
    lid = ledger.insert_lesson(con, {"lesson": "x", "setup_type": "gap_go"}, "evening", date(2026, 8, 1))
    assert ledger.update_lesson_feedback(con, lid, "confirm", D) is True
    assert ledger.update_lesson_feedback(con, lid, "confirm", D) is False
    assert ledger.update_lesson_feedback(con, lid, "contradict", D) is True
    row = con.execute("SELECT confirmations, contradictions FROM lessons WHERE lesson_id = ?",
                      [lid]).fetchone()
    assert row == (1, 1)
    assert con.execute("SELECT session_date FROM lesson_contradictions WHERE lesson_id = ?",
                       [lid]).fetchall() == [(D,)]


def test_holdout_tier_is_earned_on_net_evidence(con):
    promoted = date(2026, 6, 1)
    lid = ledger.insert_lesson(con, {"lesson": "x"}, "evening", promoted)
    for i in range(9):   # 9 out-of-sample confirmations -> moderate on their own
        ledger.update_lesson_feedback(con, lid, "confirm", date(2026, 7, 1) + timedelta(days=i))
    assert ledger.lesson_holdout_stats(con, lid, promoted)["tier"] == "moderate"
    for i in range(3):   # 3 dated contradictions -> 6 net -> no longer moderate
        ledger.update_lesson_feedback(con, lid, "contradict", date(2026, 8, 1) + timedelta(days=i))
    st = ledger.lesson_holdout_stats(con, lid, promoted)
    assert st["contradictions_out_of_sample"] == 3
    assert st["tier"] != "moderate"


def test_lesson_scope():
    rows = [{"setup_type": "gap_go", "regime_tag": "uptrend/risk_on/lowvol", "stop_dist_atr": 0.7}]
    assert grounding.lesson_in_scope({"setup_type": "gap_go"}, rows)[0]
    assert grounding.lesson_in_scope({"setup_type": "gap_go", "regime": "uptrend/risk_on"}, rows)[0]
    assert not grounding.lesson_in_scope({"setup_type": "gap_go", "regime": "chop"}, rows)[0]
    assert not grounding.lesson_in_scope({"setup_type": "breakout"}, rows)[0]
    assert not grounding.lesson_in_scope({"lesson": "vague"}, rows)[0]
    pred = '{"lane": "morning", "conditions": [{"dimension": "stop_atr_bucket", "level": "0.5-1"}]}'
    assert grounding.lesson_in_scope({"predicate_json": pred}, rows)[0]
    miss = '{"lane": "morning", "conditions": [{"dimension": "stop_atr_bucket", "level": "<0.5"}]}'
    assert not grounding.lesson_in_scope({"predicate_json": miss}, rows)[0]


def test_lesson_predicate_validation():
    ok = {"lane": "morning", "conditions": [{"dimension": "setup_type", "level": "gap_go"}]}
    assert grounding.validate_lesson_predicate(ok) is not None
    assert grounding.validate_lesson_predicate(
        {"lane": "morning", "conditions": [{"dimension": "moon_phase", "level": "full"}]}) is None
    assert grounding.validate_lesson_predicate("gap_go") is None


# ------------------------------------------------------------ end to end

def _seed(con):
    plan = {"no_trade": False, "market_stance": "t", "total_risk_r": 1.0, "rejected": [],
            "notes_for_evening": "", "picks": [{
                "ticker": "TEST", "direction": "long", "setup_type": "gap_go",
                "catalyst": "x", "thesis_final": "x", "conviction": 4, "size_r": 1.0,
                "entry_plan": {"type": "open", "price": 100.0}, "entry_price": 100.0,
                "stop_price": 98.0, "target_price": 104.0, "target2_price": None,
                "time_stop": None, "invalidation": "x"}]}
    return ledger.save_daily_plan(con, D, plan, {"candidates": []}, "chop")[0]


def test_evening_writes_only_grounded_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "e.duckdb")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("agent.loops.evening.send_email", lambda *a, **k: None)
    monkeypatch.setattr("agent.loops.evening.render_dashboard", lambda *a, **k: None)
    con = ledger.connect()
    pid = _seed(con)
    other = ledger.insert_lesson(con, {"lesson": "breakouts need 3 touches",
                                       "setup_type": "breakout"}, "evening", date(2026, 8, 1))
    con.close()

    captured = {}

    class _Grader:
        def __init__(self, cfg):
            pass

        def call(self, **kw):
            captured["user"] = kw["user"]

            class R:
                parsed = {
                    "plan_grade": "A",
                    "pick_reviews": [{"pick_id": pid, "ticker": "TEST", "grade": "A",
                                      "why": "stop was 7.77 ATR wide, clean"}],
                    "lessons": [
                        {"lesson": "gap_go holds 9.99x RVOL", "setup_type": "gap_go",
                         "evidence": "RVOL 9.99x"},
                        {"lesson": "gap_go without a catalyst is a coin flip",
                         "setup_type": "gap_go", "evidence": "no catalyst today"},
                    ],
                    "lesson_updates": [{"lesson_id": other, "action": "confirm", "note": "x"}],
                }
                usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0, "cost_usd": 0.0}
                text = ""
            return R()

    monkeypatch.setattr(evening, "LLMClient", _Grader)
    # The offline synthetic fill lands far from plan (an execution anomaly),
    # which the tainted-day guard would use to skip every lesson before the
    # grounding checks run. That guard is pinned elsewhere; bypass it here.
    monkeypatch.setattr(evening, "_all_fills_tainted", lambda outcomes: False)
    cfg = load_config()
    cfg.mock = False
    cfg.offline = True
    evening.run(cfg, run_date=D, force=True)

    con = ledger.connect()
    lessons = [r[0] for r in con.execute("SELECT lesson FROM lessons ORDER BY lesson_id").fetchall()]
    kinds = sorted(r[0] for r in con.execute("SELECT kind FROM grader_rejections").fetchall())
    grade, grounded, tape = con.execute(
        "SELECT grade, grounded, tape_summary FROM outcomes WHERE pick_id = ?", [pid]).fetchone()
    n_rc = con.execute("SELECT COUNT(*) FROM rule_confirmations").fetchone()[0]
    confirmations = con.execute("SELECT confirmations FROM lessons WHERE lesson_id = ?",
                                [other]).fetchone()[0]
    con.close()

    assert "## COMPUTED TAPE" in captured["user"] and tape
    assert "gap_go without a catalyst is a coin flip" in lessons
    assert "gap_go holds 9.99x RVOL" not in lessons
    assert kinds == ["lesson_ungrounded", "review_ungrounded", "update_out_of_scope"]
    assert grade == "A" and grounded is False
    assert n_rc == 0, "an ungrounded review may not stamp a rule confirmation"
    assert confirmations == 0, "no breakout traded today: the confirm is out of scope"


# ------------------------------------------------------------------ Phase 4

def test_grade_inflation_counts_ab_against_process_proxies(con):
    for i, (grade, stop_atr, anomaly, grounded) in enumerate(
            [("A", 0.3, False, True), ("B", 0.8, True, True), ("C", 0.3, False, True),
             ("A", 1.2, False, False)]):
        pid = con.execute(
            """INSERT INTO picks (plan_date, ticker, direction, setup_type, size_r, entry_type,
                                  entry_price, stop_price, target_price, conviction)
               VALUES (?, ?, 'long', 'gap_go', 0.5, 'open', 10, 9, 12, 3) RETURNING pick_id""",
            [D, f"T{i}"]).fetchone()[0]
        ledger.save_outcome(con, {"pick_id": pid, "eval_date": D, "triggered": True,
                                  "r_multiple": 1.0, "exit_reason": "eod",
                                  "stop_dist_atr": stop_atr, "execution_anomaly": anomaly})
        con.execute("UPDATE outcomes SET grade = ?, grounded = ? WHERE pick_id = ?",
                    [grade, grounded, pid])
    rows = ledger.grade_inflation(con)
    assert rows == [{"lane": "morning", "setup_type": "gap_go", "graded": 4, "ab": 3,
                     "ab_stop_inside_noise": 1, "ab_on_anomaly": 1, "ab_ungrounded": 1}]
