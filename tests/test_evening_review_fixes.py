"""
The 2026-10-05 evening review (specs/MORNING_BUDGET_SPEC_v1.md, Parts 2-6):
a runner selection a killed request can no longer lose, a plan-time gate on
triggers the open will cross, the scratch band, what the grader is shown
(RVOL pace, lanes, scratches, the shadow line), testable hypotheses, and the
health panel's lock warning.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb
import pytest

from agent import grading, hypotheses, ledger, selfcheck
from agent.config import Config
from agent.health import loop_health
from agent.loops import evening, runner as runner_loop
from agent.loops.morning import _validate_plan

D = date(2026, 10, 5)


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute(ledger._SCHEMA)
    for mig in ledger._MIGRATIONS:
        c.execute(mig)
    return c


# ------------------------------------------------------------ runner selection

def test_a_claim_whose_request_died_is_retaken_after_ten_minutes(con):
    t0 = datetime(2026, 10, 5, 10, 31)
    assert ledger.claim_runner_selection(con, D, at=t0)
    assert not ledger.claim_runner_selection(con, D, at=t0 + timedelta(minutes=5))
    assert ledger.runner_selection_note(con, D)["state"] == "unfinished"
    assert ledger.claim_runner_selection(con, D, at=t0 + timedelta(minutes=11))
    at = con.execute("SELECT selection_at FROM runner_sessions").fetchone()[0]
    assert at == t0 + timedelta(minutes=11)                     # the honest decision time


@pytest.mark.parametrize("trace", ["reason", "completed", "selected", "orders"])
def test_any_trace_of_a_result_keeps_the_claim(con, trace):
    t0 = datetime(2026, 10, 5, 10, 31)
    assert ledger.claim_runner_selection(con, D, at=t0)
    if trace == "reason":
        ledger.upsert_runner_session(con, {"session_date": D, "selection_skipped_reason": "no candidates"})
    elif trace == "completed":
        ledger.complete_runner_selection(con, D)
    elif trace == "selected":
        ledger.mark_runner_selected(con, D, "AAA", 1, None)
    else:
        ledger.upsert_runner_session(con, {"session_date": D, "live_orders_note": "placed 0/0"})
    assert not ledger.claim_runner_selection(con, D, at=t0 + timedelta(minutes=30))
    assert ledger.runner_selection_note(con, D)["state"] == "decided"


def test_the_selection_note_names_each_state(con):
    assert ledger.runner_selection_note(con, D)["state"] == "no_session"
    ledger.upsert_runner_session(con, {"session_date": D, "candidates_found": 533})
    assert ledger.runner_selection_note(con, D)["state"] == "not_run"
    ledger.claim_runner_selection(con, D, at=datetime(2026, 10, 5, 11, 18))
    note = ledger.runner_selection_note(con, D)
    assert note["state"] == "unfinished" and "claimed at 11:18 ET and never finished" in note["text"]


def test_a_hosted_tick_with_its_budget_spent_does_not_claim(monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    assert runner_loop._budget_too_spent(299) is False            # local: no budget
    monkeypatch.setenv("VERCEL", "1")
    assert runner_loop._budget_too_spent(209) is False
    assert runner_loop._budget_too_spent(211) is True


def test_the_self_check_flags_a_lost_selection(con):
    cfg = Config(raw={"runner": {"enabled": True, "selection_enabled": True}})
    con.execute("""INSERT INTO runner_candidates (session_date, ticker) VALUES (?, 'AAA')""", [D])
    ledger.claim_runner_selection(con, D, at=datetime(2026, 10, 5, 10, 31))
    r = selfcheck.check_runner_selection(con, cfg, D)
    assert r["status"] == "warn" and "never finished" in r["headline"]


# ------------------------------------------------------------ plan-time gates

def _plan(entry_type, entry, stop, target, direction="long"):
    return {"no_trade": False, "picks": [{
        "ticker": "CWH", "direction": direction, "setup_type": "breakdown_short",
        "entry_plan": {"type": entry_type, "price": entry}, "entry_price": entry,
        "stop_price": stop, "target_price": target, "size_r": 0.25, "conviction": 3}]}


def _kept(plan, ref):
    out, warnings = _validate_plan(plan, Config(raw={}), ref_prices={"CWH": ref},
                                   atr_map={"CWH": 0.5}, earnings_map={"CWH": 30})
    return bool(out["picks"]), " | ".join(warnings)


def test_a_breakout_trigger_the_open_will_cross_is_dropped():
    # CWH 10/05: short trigger 4.90 against a ~4.91 price, risk 0.35.
    kept, why = _kept(_plan("breakout", 4.90, 5.25, 4.20, "short"), 4.91)
    assert not kept and "+0.03R from last price" in why and "crossed" in why
    kept, _ = _kept(_plan("breakout", 4.80, 5.15, 4.10, "short"), 4.91)     # 0.31R away
    assert kept


def test_a_breakout_already_through_its_trigger_is_dropped():
    kept, why = _kept(_plan("breakout", 10.0, 9.5, 11.0), 10.05)            # long, price above
    assert not kept and "-0.10R" in why


def test_an_entry_whose_target_already_traded_is_dropped():
    kept, why = _kept(_plan("open", 100.0, 102.0, 96.0, "short"), 95.5)
    assert not kept and "already reached" in why
    # A limit that buys a pullback below a price already above its target is
    # a real trade (the reward is the bounce back), so it stays.
    kept, _ = _kept(_plan("limit", 10.0, 9.6, 10.8), 10.09)
    assert kept


# ---------------------------------------------------------------- scratches

@pytest.mark.parametrize("r, cls", [(0.01, "scratch"), (-0.05, "scratch"), (0.05, "scratch"),
                                    (0.051, "win"), (-0.06, "loss"), (None, None)])
def test_outcome_class(r, cls):
    assert grading.outcome_class(r) == cls


def _trade(con, d, r, pid):
    con.execute("""INSERT INTO picks (pick_id, plan_date, ticker, direction, setup_type, size_r)
                   VALUES (?, ?, ?, 'long', 'gap_go', 1.0)""", [pid, d, f"T{pid}"])
    con.execute("""INSERT INTO outcomes (pick_id, triggered, r_multiple, r_planned)
                   VALUES (?, TRUE, ?, ?)""", [pid, r, r])


def test_a_scratch_neither_wins_nor_breaks_a_losing_streak(con):
    for i, r in enumerate([-1.0, -1.0, -1.0, 0.01]):               # newest last
        _trade(con, date(2026, 9, 28) + timedelta(days=i), r, i + 1)
    assert ledger.current_streak(con) == {"kind": "losing", "length": 3}
    assert ledger.overall_stats(con)["wins"] == 0


def test_the_grader_is_shown_lane_pace_and_scratch():
    pick = {"pick_id": 1, "ticker": "NVDA", "direction": "long", "setup_type": "breakout",
            "entry_type": "breakout", "entry_price": 180, "stop_price": 178, "target_price": 184,
            "size_r": 0.25, "conviction": 3, "time_stop": None, "thesis": "t", "invalidation": "i",
            "exploration_lane": "coverage"}
    out = {"pick_id": 1, "triggered": True, "entry_fill": 181.3, "exit_price": 181.4,
           "exit_reason": "time_stop", "r_planned": 0.01, "r_multiple": 0.01, "mfe_r": 0.5,
           "mae_r": -0.4, "vwap_side_at_fill": "above", "fill_vs_vwap_pct": 0.3,
           "pct_time_above_vwap": 0.05, "vwap_crosses": 1, "rvol_at_1000": 0.16,
           "rvol_at_1200": 0.45, "rvol_close": 0.90}
    md = grading.outcomes_md([pick], [out])
    assert "- Lane: coverage:" in md and "never that it was traded" in md
    assert "RVOL@10:00 0.16x ADV20 (18% of the day's volume by 10:00)" in md
    assert "RVOL@12:00 0.45x ADV20 (50% of the day's volume by 12:00)" in md
    assert "a SCRATCH (inside +/-0.05R" in md


def test_the_shadow_line_counts_what_its_net_covers_and_claims_nothing():
    txt = evening._shadow_md({"scored": 7, "priced": 6, "triggered": 4, "net_forgone_r": -0.14})
    assert "7 rejected candidates scored, 6 priced" in txt and "1 unmeasurable" in txt
    assert "-0.02R per priced rejection" in txt
    assert "neither confirms nor refutes the filters" in txt


def test_the_grader_prompt_defines_rvol_and_protects_test_lanes():
    from agent.llm import load_prompt
    text = load_prompt("evening_grader", {})
    assert "RVOL is cumulative, not time-of-day normalized" in text
    assert "Test-lane picks" in text and "Scratches" in text
    assert "never write that it confirms" in text


# --------------------------------------------------------------- hypotheses

def test_an_idea_stacking_numeric_conditions_is_refused():
    base = {"refutation": "mean R per session at or below zero over the sample"}
    ok, why = hypotheses.clean_proposal(
        {**base, "statement": "Gap-ups over 15% with a same-morning offering fade below VWAP by 10:30"})
    assert ok is not None and why == ""
    bad, why = hypotheses.clean_proposal({**base, "statement": (
        "Low-float gap-ups over 40% with RVOL 2.49x at 10:00 that spend 5% of the session "
        "above VWAP fade into 14:00")})
    assert bad is None and "numeric conditions" in why


# -------------------------------------------------------------------- health

def test_only_todays_stuck_lock_is_a_warning(con):
    now = datetime(2026, 10, 6, 9, 0)
    # Staleness is judged by the database clock, so every lock started well
    # before it; only the trading dates differ.
    long_ago = datetime.now() - timedelta(days=30)
    for td in (date(2026, 9, 30), date(2026, 10, 5), date(2026, 10, 6)):
        con.execute("""INSERT INTO run_locks (trading_date, run_type, status, request_id, started_at)
                       VALUES (?, 'morning', 'in_progress', 'r', ?)""", [td, long_ago])
    warnings = [w for w in loop_health(con, None, now=now)["warnings"] if "lock" in w]
    assert len(warnings) == 1 and "today's morning lock (2026-10-06)" in warnings[0]


def test_the_self_check_names_which_morning_half_died(con):
    ledger.save_morning_checkpoint(con, date(2026, 10, 5), "r1", {})
    ledger.claim_morning_checkpoint(con, date(2026, 10, 5))
    ledger.set_morning_checkpoint(con, date(2026, 10, 5), arm_b_status="running")
    r = selfcheck.check_morning_budget(con, None, date(2026, 10, 6))
    assert r["status"] == "warn"
    assert "risk half died mid-run on 2026-10-05" in r["headline"]
    assert "2026-10-05 (killed mid-run)" in r["headline"]


def test_probes_that_reached_the_tape_by_another_lane_are_not_a_dead_lane(con):
    from agent.util import is_market_day
    d, n = date(2026, 10, 5), 0
    while n < 5:
        if is_market_day(d):
            ledger.save_daily_plan(
                con, d, {"no_trade": False, "picks": [{
                    "ticker": "X", "direction": "long", "setup_type": "gap_go",
                    "entry_price": 10, "stop_price": 9, "target_price": 12, "size_r": 0.25,
                    "conviction": 3, "exploration_lane": "coverage"}]},
                {"candidates": [{"ticker": "X", "probe_for": "Q1"}]}, "chop")
            n += 1
        d -= timedelta(days=1)
    cfg = Config(raw={"exploration_lane": {"enabled": True, "backlog_probe": True}})
    r = selfcheck.check_lanes(con, cfg, date(2026, 10, 5))
    assert r["status"] == "ok" and "probes tagged 5, traded 5 (coverage 5)" in r["headline"]
