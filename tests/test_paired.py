"""
Tests for agent/loops/paired.py — the paired-arm harness.

The safety properties matter as much as the statistics here:
  * arm B must NEVER reach the picks table (it would enter the equity curve,
    the streak logic, and the drawdown circuit breaker)
  * a failing challenger must never break the production morning run
  * a requested-but-silently-failed ablation must be refused, not run — two
    identical arms compared as though they differed is worse than no experiment
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from agent import migrations_v4
from agent.loops.paired import (
    apply_context_mode,
    grade_paired_arms,
    paired_comparison,
    paired_md,
    record_arm,
    run_challenger_arm,
    should_run_paired,
)

MEMORY = """## YOUR PLAYBOOK (v3 — your accumulated, evidence-backed rules)

Core principles here.

## YOUR LESSON BANK (active lessons, most trusted first)

- [L1] a lesson

## YOUR LAST 20 GRADED TRADES

| date | ticker | R |
|---|---|---|
| 2026-08-01 | AAAA | 1.2 |

## YOUR PERFORMANCE BY SETUP TYPE

- gap_go: n=10

## SELF-AWARENESS CHECK (computed from your ledger — obey these)

No streak.
"""


class _Cfg:
    def __init__(self, raw):
        self.raw = raw

    def get(self, k, d=None):
        return self.raw.get(k, d)


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE picks (pick_id INTEGER, plan_date DATE, ticker VARCHAR)")
    c.execute("""CREATE TABLE outcomes (pick_id INTEGER, triggered BOOLEAN,
                 r_multiple DOUBLE, exit_reason VARCHAR)""")
    c.execute("CREATE TABLE daily_plans (plan_date DATE)")
    for stmt in migrations_v4.research_schema():
        c.execute(stmt)
    # v27: paired_arm_picks carries the entry window production picks carry.
    c.execute("ALTER TABLE paired_arm_picks ADD COLUMN IF NOT EXISTS entry_not_before VARCHAR")
    yield c
    c.close()


# ------------------------------------------------------------------ scheduling

def test_paired_is_off_unless_explicitly_enabled():
    assert should_run_paired(_Cfg({}), date(2026, 8, 27)) is False
    assert should_run_paired(_Cfg({"paired": {"enabled": False}}), date(2026, 8, 27)) is False


def test_paired_every_session_when_enabled():
    cfg = _Cfg({"paired": {"enabled": True, "every_n_sessions": 1}})
    assert should_run_paired(cfg, date(2026, 8, 27)) is True


def test_paired_frequency_is_deterministic_per_date():
    cfg = _Cfg({"paired": {"enabled": True, "every_n_sessions": 3}})
    d = date(2026, 8, 27)
    # same date always gives the same answer -- a re-run cannot change history
    assert should_run_paired(cfg, d) == should_run_paired(cfg, d)
    hits = sum(should_run_paired(cfg, date(2026, 8, 1 + i)) for i in range(30))
    assert 8 <= hits <= 12          # ~1 in 3


# ------------------------------------------------------- context ablation

def test_full_mode_leaves_memory_untouched():
    assert apply_context_mode(MEMORY, "full") == MEMORY


def test_aggregate_only_strips_the_trade_table_but_keeps_everything_else():
    out = apply_context_mode(MEMORY, "aggregate_only")
    assert "| 2026-08-01 | AAAA | 1.2 |" not in out
    assert "ABLATED" in out
    assert "YOUR PLAYBOOK" in out
    assert "YOUR LESSON BANK" in out
    assert "YOUR PERFORMANCE BY SETUP TYPE" in out
    assert "SELF-AWARENESS CHECK" in out


def test_none_mode_also_strips_the_lesson_bank():
    out = apply_context_mode(MEMORY, "none")
    assert "[L1] a lesson" not in out
    assert "| 2026-08-01 | AAAA | 1.2 |" not in out
    assert "YOUR PLAYBOOK" in out
    assert "YOUR PERFORMANCE BY SETUP TYPE" in out


def test_ablation_is_a_noop_when_the_header_is_missing():
    text = "## SOMETHING ELSE\n\nno trades section here\n"
    assert apply_context_mode(text, "aggregate_only") == text


def test_unknown_mode_degrades_to_full_rather_than_raising():
    assert apply_context_mode(MEMORY, "wishful") == MEMORY


def test_ablation_survives_a_changed_trade_count_in_the_header():
    text = MEMORY.replace("YOUR LAST 20 GRADED TRADES", "YOUR LAST 5 GRADED TRADES")
    assert "AAAA" not in apply_context_mode(text, "aggregate_only")


# ------------------------------------------------------------ recording arms

def _plan(*picks, no_trade=False):
    return {"no_trade": no_trade, "picks": list(picks)}


def _p(ticker="AAAA", entry=100.0, stop=98.0, target=104.0):
    return {"ticker": ticker, "direction": "long", "setup_type": "gap_go",
            "entry_plan": {"type": "open", "price": entry}, "entry_price": entry,
            "stop_price": stop, "target_price": target, "size_r": 1.0,
            "conviction": 4, "thesis": "t"}


def test_record_arm_writes_only_to_paired_tables(con):
    n = record_arm(con, date(2026, 8, 27), "B", _plan(_p()), {}, "3", "full")
    assert n == 1
    assert con.execute("SELECT COUNT(*) FROM paired_arm_picks").fetchone()[0] == 1
    # THE safety property: arm B never enters production tables
    assert con.execute("SELECT COUNT(*) FROM picks").fetchone()[0] == 0


def test_record_arm_is_idempotent_per_date_and_arm(con):
    for _ in range(3):
        record_arm(con, date(2026, 8, 27), "B", _plan(_p()), {}, "3", "full")
    assert con.execute("SELECT COUNT(*) FROM paired_arms").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM paired_arm_picks").fetchone()[0] == 1


def test_record_arm_handles_a_no_trade_plan(con):
    assert record_arm(con, date(2026, 8, 27), "B", _plan(no_trade=True), {}, "3", "full") == 0
    assert con.execute("SELECT no_trade FROM paired_arms").fetchone()[0] is True


# ------------------------------------------------------------- running arm B

class _Res:
    def __init__(self, parsed, cost=0.5):
        self.parsed = parsed
        self.usage = {"cost_usd": cost}


def _llm_ok(**kw):
    if kw.get("mock_name") == "morning_analyst":
        return _Res({"candidates": [_p()]})
    return _Res(_plan(_p()))


def test_challenger_runs_and_records(con):
    cfg = _Cfg({"paired": {"enabled": True, "arm_b_context_mode": "aggregate_only"}})
    res = run_challenger_arm(
        con, cfg, date(2026, 8, 27), analyst_system="A", risk_system="R",
        bundle_md="bundle", regime_md_text="regime", memory_text=MEMORY,
        playbook_version="3", llm_call=_llm_ok, model="m",
    )
    assert res["ok"] is True and res["picks"] == 1
    assert res["context_mode"] == "aggregate_only"
    assert res["cost_usd"] == 1.0
    assert con.execute("SELECT COUNT(*) FROM picks").fetchone()[0] == 0


def test_challenger_refuses_to_run_when_a_requested_ablation_did_not_apply(con):
    # Comparing two identical arms as though they differed would poison the
    # experiment. Refusing is strictly better than running a fake contrast.
    cfg = _Cfg({"paired": {"enabled": True, "arm_b_context_mode": "aggregate_only"}})
    res = run_challenger_arm(
        con, cfg, date(2026, 8, 27), analyst_system="A", risk_system="R",
        bundle_md="b", regime_md_text="r", memory_text="## NOTHING TO ABLATE\n",
        playbook_version="3", llm_call=_llm_ok,
    )
    assert res["ok"] is False
    assert "ablation requested" in res["reason"]
    assert con.execute("SELECT COUNT(*) FROM paired_arms").fetchone()[0] == 0


def test_a_failing_challenger_never_breaks_the_production_run(con):
    def _boom(**kw):
        raise RuntimeError("anthropic down")

    cfg = _Cfg({"paired": {"enabled": True, "arm_b_context_mode": "full"}})
    res = run_challenger_arm(
        con, cfg, date(2026, 8, 27), analyst_system="A", risk_system="R",
        bundle_md="b", regime_md_text="r", memory_text=MEMORY,
        playbook_version="3", llm_call=_boom,
    )
    assert res["ok"] is False and "RuntimeError" in res["reason"]


def test_challenger_is_held_to_the_same_validator_as_production(con):
    seen = {}

    def _validate(plan):
        seen["called"] = True
        return {"no_trade": False, "picks": []}, ["dropped everything"]

    cfg = _Cfg({"paired": {"enabled": True, "arm_b_context_mode": "full"}})
    res = run_challenger_arm(
        con, cfg, date(2026, 8, 27), analyst_system="A", risk_system="R",
        bundle_md="b", regime_md_text="r", memory_text=MEMORY,
        playbook_version="3", llm_call=_llm_ok, validate=_validate,
    )
    assert seen.get("called") is True
    assert res["picks"] == 0


def test_arm_b_can_pin_a_different_playbook_version(con):
    cfg = _Cfg({"paired": {"enabled": True, "arm_b_context_mode": "full",
                           "arm_b_playbook_version": "2"}})
    res = run_challenger_arm(
        con, cfg, date(2026, 8, 27), analyst_system="A", risk_system="R",
        bundle_md="b", regime_md_text="r", memory_text=MEMORY,
        playbook_version="3", llm_call=_llm_ok,
    )
    assert res["playbook_version"] == "2"
    assert con.execute("SELECT playbook_version FROM paired_arms").fetchone()[0] == "2"


# ---------------------------------------------------------------- grading

def _sim(pick, bars, daily, **kw):
    return {"triggered": True, "entry_fill": 100.0, "exit_price": 102.0,
            "exit_reason": "target", "r_multiple": 1.0, "sim_version": 2}


class _MD:
    def minute_bars(self, sym, d):
        return [{"o": 100, "h": 103, "l": 99, "c": 102, "v": 1000}]

    def daily_bar(self, sym, d):
        return {"open": 100, "high": 103, "low": 99, "close": 102, "volume": 1000}


def test_grade_paired_arms_uses_the_injected_production_simulator(con):
    d = date(2026, 8, 27)
    record_arm(con, d, "B", _plan(_p("AAAA"), _p("BBBB")), {}, "3", "full")
    res = grade_paired_arms(con, _MD(), d, _sim)
    assert res["graded"] == 2
    assert res["arms"]["B"]["total_r"] == 2.0


def test_grading_skips_already_graded_rows(con):
    d = date(2026, 8, 27)
    record_arm(con, d, "B", _plan(_p("AAAA")), {}, "3", "full")
    grade_paired_arms(con, _MD(), d, _sim)
    assert grade_paired_arms(con, _MD(), d, _sim)["graded"] == 0


def test_grading_never_touches_arm_a(con):
    d = date(2026, 8, 27)
    record_arm(con, d, "A", _plan(_p("AAAA")), {}, "3", "full")
    assert grade_paired_arms(con, _MD(), d, _sim)["graded"] == 0


def test_a_broken_simulator_call_isolates_per_ticker(con):
    d = date(2026, 8, 27)
    record_arm(con, d, "B", _plan(_p("AAAA"), _p("BBBB")), {}, "3", "full")

    def _flaky(pick, bars, daily, **kw):
        if pick["entry_price"] == 100.0 and _flaky.calls == 0:
            _flaky.calls += 1
            raise RuntimeError("bars unavailable")
        return _sim(pick, bars, daily)

    _flaky.calls = 0
    res = grade_paired_arms(con, _MD(), d, _flaky)   # must not raise
    assert res["graded"] == 1


# -------------------------------------------------------------- comparison

def _seed_pair(con, d, arm_a_r, arm_b_r, pid):
    con.execute("INSERT INTO picks (pick_id, plan_date, ticker) VALUES (?,?,?)",
                [pid, d, "AAAA"])
    con.execute("""INSERT INTO outcomes (pick_id, triggered, r_multiple, exit_reason)
                   VALUES (?, TRUE, ?, 'target')""", [pid, arm_a_r])
    record_arm(con, d, "B", _plan(_p("BBBB")), {}, "2", "full")
    con.execute("""UPDATE paired_arm_picks SET triggered = TRUE, r_multiple = ?,
                   exit_reason = 'target', graded_at = current_timestamp
                   WHERE plan_date = ? AND arm = 'B'""", [arm_b_r, d])


def test_paired_comparison_computes_per_session_differences(con):
    for i in range(10):
        _seed_pair(con, date(2026, 8, 3 + i), 1.0, 0.2, pid=i + 1)
    cmp = paired_comparison(con)
    assert cmp["sessions"] == 10
    assert cmp["mean_diff_r"] == pytest.approx(0.8)
    assert cmp["arm_a_total_r"] == pytest.approx(10.0)
    assert cmp["arm_b_total_r"] == pytest.approx(2.0)


def test_a_consistent_production_advantage_is_detected(con):
    for i in range(12):
        _seed_pair(con, date(2026, 8, 3 + i), 1.0 + (0.05 if i % 2 else -0.05), 0.0, pid=i + 1)
    cmp = paired_comparison(con)
    assert cmp["p"] < 0.01
    assert "production is significantly ahead" in paired_md(cmp)


def test_a_challenger_advantage_is_reported_as_such(con):
    for i in range(12):
        _seed_pair(con, date(2026, 8, 3 + i), 0.0, 1.0 + (0.05 if i % 2 else -0.05), pid=i + 1)
    cmp = paired_comparison(con)
    assert cmp["mean_diff_r"] < 0
    assert "CHALLENGER is significantly ahead" in paired_md(cmp)


def test_incompletely_graded_sessions_are_excluded_from_the_pairing(con):
    _seed_pair(con, date(2026, 8, 10), 1.0, 0.5, pid=1)
    # a second session whose arm B was never graded
    d2 = date(2026, 8, 11)
    con.execute("INSERT INTO picks (pick_id, plan_date, ticker) VALUES (2, ?, 'AAAA')", [d2])
    con.execute("""INSERT INTO outcomes (pick_id, triggered, r_multiple, exit_reason)
                   VALUES (2, TRUE, 5.0, 'target')""")
    record_arm(con, d2, "B", _plan(_p("BBBB")), {}, "2", "full")
    cmp = paired_comparison(con)
    assert cmp["sessions"] == 1          # the ungraded session must not bias the pair


def test_comparison_is_safe_with_no_paired_sessions(con):
    cmp = paired_comparison(con)
    assert cmp["sessions"] == 0
    assert "no arm B sessions" in cmp["note"]
    assert "no arm B sessions" in paired_md(cmp)


def test_paired_md_states_why_a_causal_read_is_defensible(con):
    for i in range(10):
        _seed_pair(con, date(2026, 8, 3 + i), 1.0, 0.2, pid=i + 1)
    md = paired_md(paired_comparison(con))
    assert "SAME" in md and "cancels in the difference" in md
    assert "never as 'no effect'" in md
