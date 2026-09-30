"""
Playbook audit, 2026-09-29 — pins every defect the v9 review found.

Playbook v9 passed every write-time check and still told the desk things its
ledger could not support. Each test below reproduces one shape from v9 and
pins the fix:

  1. Integrity leaks: paraphrase past provenance, names left behind by
     redaction, findings restated in an unchecked changelog, a changelog
     describing a section the system had replaced.
  2. Tiers above their realized n; decay applied to Setup Rules only.
  3. Runner-lane picks counted in the morning desk's calibration, open
     questions, funnel, counterfactuals and selection ablation.
  4. Open questions "answered" from the wrong population, from one arm, or
     from noise.
  5. Runner buckets that held ~99% of the lane, tested as findings.
  6. A selection verdict from one contested session.
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from agent import ledger, playbook_enforcement as pe, question_eval
from agent.stats import Comparison, evaluate_search

D = date(2026, 9, 22)
AS_OF = date(2026, 9, 29)


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute(ledger._SCHEMA)
    for m in ledger._MIGRATIONS:
        c.execute(m)
    yield c
    c.close()


def _doc(setup="", regime="", sizing="", questions="", best="**NO VALIDATED EDGE YET.**"):
    return f"""# Trading Agent Playbook

## Reading This File (for the morning agent)

- old

## Current Best Edge

{best}

## Sizing Policy

{pe.SIZING_BANNER}

- Base unit: 1R = configured dollars_per_r.
{sizing}

## Setup Rules

{setup}

## Regime Rules

{regime}

## Runner Lane

**(no validated runner rules yet — do not assume any)**

## Shadow Ledger Summary

The filters are working.

## Open Questions (pre-registered experiments)

{questions}

## Retired Rules (graveyard)

_None retired yet._
"""


def _blocks_left(md, **kw):
    return [v for v in pe.collect(md, [], None, **kw) if v.severity == "block"]


# ------------------------------------------------------ 1. integrity leaks

def test_a_paraphrased_prior_gets_its_status_not_a_pass():
    """'favor X' is not an edge keyword, so it passed provenance and steered
    the morning agent without any status. Every rule line now carries one."""
    line = "- uptrend/risk_on: favor catalyst_momentum and gap_go longs."
    res = pe.enforce(_doc(regime=line))
    assert f"{line} [UNVALIDATED prior]" in res.markdown
    assert any(v.checker == "status_marker" and v.action == "replaced" for v in res.violations)
    assert _blocks_left(res.markdown) == []


def test_a_sizing_rule_resting_on_one_observation_is_tagged_a_prior():
    line = ("- Correlated picks share a combined 1.5R cap. "
            "[n=1 supporting: AMD/NVDA overlap; last_confirmed 2026-08-20]")
    res = pe.enforce(_doc(sizing=line))
    assert f"{line} [UNVALIDATED prior]" in res.markdown
    assert "- Base unit: 1R = configured dollars_per_r.\n" in res.markdown   # a definition, untouched


def test_a_withheld_setup_line_says_which_setup_and_what_status_it_has():
    bad = "- catalyst_momentum: fresh catalyst longs have edge and beat the baseline."
    res = pe.enforce(_doc(setup=bad), changelog="- catalyst_momentum: kept at provisional.",
                     as_of=AS_OF)
    assert bad not in res.markdown
    assert "- catalyst_momentum: [withheld by integrity check: provenance" in res.markdown
    assert "treat catalyst_momentum as an UNVALIDATED prior" in res.markdown
    # and the changelog does not describe a rule the body withheld
    assert "kept at provisional" not in res.changelog


def test_the_changelog_is_checked_and_cannot_describe_system_sections():
    changelog = "\n".join([
        "- Decay check (applied FIRST): 5 rules, zero auto-demotions.",
        "- Runner Lane: selection edge shows selected worse than unselected.",
        "- Shadow Ledger Summary: filters SAVED money.",
        "- Q7: 24/8 met; finding: no separation.",
        "- gap_go: n=3, wr=67%, avgR=+0.9",
        "- oversold_reversal: ADDED to Setup Rules.",
    ])
    computed = {"open questions": ["", "system text", ""]}
    res = pe.enforce(_doc(), changelog=changelog, as_of=AS_OF, computed=computed)
    cl = res.changelog
    for gone in ("zero auto-demotions", "selected worse", "SAVED money", "no separation",
                 "wr=67%"):
        assert gone not in cl
    assert "oversold_reversal: ADDED to Setup Rules." in cl
    assert cl.startswith("- Decay check (system")
    assert "System-rendered this version" in cl


def test_a_replaced_current_best_edge_is_not_described_by_the_miners_changelog():
    res = pe.enforce(_doc(best="Best edge: gap_go longs, mean -0.77R."),
                     changelog="- Current Best Edge: added a full counterfactual block.",
                     as_of=AS_OF)
    assert "full counterfactual block" not in res.changelog
    assert "Current Best Edge: the miner's draft failed" in res.changelog


def test_a_finding_withheld_from_the_body_is_not_restated_in_the_changelog():
    q = ("Q7: RVOL separation\nThreshold: 8. Progress: 24/8.\n"
         "Finding: no separation, all losing (n=3, avgR=-0.9).")
    res = pe.enforce(_doc(questions=q), as_of=AS_OF,
                     changelog="- Q7: 24/8 met; finding: no separation detected.")
    assert "no separation detected" not in res.changelog
    assert "restates an open-question finding" in res.changelog


# --------------------------------------------- 2. tiers and decay

@pytest.mark.parametrize("stated, expected", [
    ("[confidence: provisional, n=0 triggered (1 untriggered), last_confirmed 2026-09-20]",
     "[confidence: none, n=0 triggered (1 untriggered), last_confirmed 2026-09-20]"),
    ("[confidence: provisional, n=4 picks (0 triggered), last_confirmed 2026-09-20]",
     "[confidence: none, n=4 picks (0 triggered), last_confirmed 2026-09-20]"),
    ("[confidence: high, n=9, last_confirmed 2026-09-20]",
     "[confidence: low, n=9, last_confirmed 2026-09-20]"),
])
def test_a_tier_above_its_realized_n_is_corrected(stated, expected):
    res = pe.enforce(_doc(setup=f"- earnings_drift: continuation. {stated}"), as_of=AS_OF)
    assert expected in res.markdown


def test_a_tier_below_its_n_is_left_alone():
    """A lesson-derived rule may sit below its raw n (holdout cap)."""
    line = "- gap_go: x. [confidence: none, n=3, UNVALIDATED prior]"
    assert line in pe.enforce(_doc(setup=line)).markdown


def test_decay_applies_to_regime_and_sizing_lines_not_only_setup_rules():
    regime = "- chop/neutral: halve sizes. [confidence: provisional, n=2 (1 triggered), last_confirmed 2026-08-10]"
    sizing = "- Correlated picks cap 1.5R. [n=1, last_confirmed 2026-08-10, UNVALIDATED prior]"
    res = pe.enforce(_doc(regime=regime, sizing=sizing), as_of=AS_OF)
    assert f"{regime} [auto-demoted: stale 35 sessions]" in res.markdown
    assert f"{sizing} [auto-demoted: stale 35 sessions]" in res.markdown
    assert _blocks_left(res.markdown, as_of=AS_OF) == []


def test_dated_rule_lines_cover_every_rule_section():
    md = _doc(setup="- sympathy: x. [confidence: none, n=0, last_confirmed 2026-08-20]",
              regime="- chop/neutral: y. [last_confirmed 2026-08-27, UNVALIDATED prior]",
              sizing="- Correlated picks cap. [n=1, last_confirmed 2026-08-20]")
    dated = pe.dated_rule_lines(md, AS_OF)
    assert {d["section"].split()[0] for d in dated} == {"setup", "regime", "sizing"}
    assert {d["label"] for d in dated} >= {"sympathy", "chop/neutral"}


# ---------------------------------------- regime coverage, system sections

def test_an_observed_regime_with_no_rule_gets_an_explicit_line():
    res = pe.enforce(_doc(regime="- uptrend/risk_on: favor longs. [UNVALIDATED prior]"),
                     regimes=["uptrend/risk_on/lowvol", "uptrend/risk_off/midvol", "unknown"])
    assert "- uptrend/risk_off: no playbook rule" in res.markdown
    assert res.markdown.count("no playbook rule") == 1
    assert _blocks_left(res.markdown) == []


def test_system_sections_replace_the_miners_text_in_contract_order():
    computed = {"shadow ledger": ["", "SYSTEM SHADOW", ""],
                "participation funnel": ["", "SYSTEM FUNNEL", ""],
                "runner lane": ["", "**(no validated runner rules yet — do not assume any)**",
                                "", "SYSTEM RUNNER", ""]}
    res = pe.enforce(_doc(), computed=computed)
    md = res.markdown
    assert "The filters are working." not in md
    assert md.index("SYSTEM RUNNER") < md.index("SYSTEM SHADOW") < md.index("SYSTEM FUNNEL") \
        < md.index("## Open Questions")
    assert _blocks_left(md) == []


# ------------------------------------------- 3. lane isolation in the ledger

def _pick(con, pid, ticker, setup="gap_go", conviction=3, size=1.0, d=D):
    con.execute("INSERT INTO picks (pick_id, plan_date, ticker, direction, setup_type, "
                "conviction, size_r, entry_price, stop_price, target_price) "
                "VALUES (?,?,?, 'long', ?, ?, ?, 10, 9, 12)",
                [pid, d, ticker, setup, conviction, size])


def _out(con, pid, r, trig=True, exit_reason="eod"):
    con.execute("INSERT INTO outcomes (pick_id, eval_date, triggered, r_multiple, r_planned, "
                "exit_reason) VALUES (?,?,?,?,?,?)", [pid, D, trig, r, r, exit_reason])


def test_calibration_leaves_out_runner_picks(con):
    _pick(con, 1, "MORN")
    _pick(con, 2, "RUNR", setup="dto_runner")
    for t, r in (("MORN", 1.0), ("RUNR", -1.0)):
        ledger.add_calibration_row(con, D, t, 3, r)
    rows = ledger.calibration_aggregate(con)
    assert [(r["conviction"], r["n"]) for r in rows] == [(3, 1)]


def test_open_question_totals_leave_out_runner_rows(con):
    _pick(con, 1, "MORN")
    _pick(con, 2, "RUNR", setup="dto_runner")
    ledger.add_question_progress(con, 13, D, 1, "MORN planned R:R=2.00 R=-1")
    ledger.add_question_progress(con, 13, D, 1, "RUNR planned R:R=2.00 R=-1")
    ledger.add_question_progress(con, 14, D, 1, "RUNR planned R:R=1.90 (1.8-2.0 band) x")
    totals = {q["question_id"]: q["total"] for q in ledger.question_progress_totals(con)}
    assert totals == {13: 1, 14: 1}          # Q14 is shadow rows: never a runner pick


def test_counterfactuals_and_sizing_are_morning_lane_only(con):
    _pick(con, 1, "MORN", size=1.0)
    _out(con, 1, 1.0)
    _pick(con, 2, "RUNR", setup="dto_runner", size=0.25)
    _out(con, 2, -1.0)
    assert ledger.management_quality(con)["triggered"] == 1
    assert ledger.sizing_counterfactual(con)["n"] == 1


def test_the_evening_writes_no_runner_rows_and_labels_q1(con, monkeypatch):
    from agent.loops import evening
    picks = [{"pick_id": 1, "ticker": "MORN", "setup_type": "gap_go", "conviction": 3,
              "direction": "long", "entry_price": 10, "stop_price": 9, "target_price": 12},
             {"pick_id": 2, "ticker": "RUNR", "setup_type": "dto_runner", "conviction": 4,
              "direction": "long", "entry_price": 10, "stop_price": 9, "target_price": 12}]
    outcomes = [{"pick_id": 1, "triggered": True, "r_multiple": 1.0, "r_planned": 0.9,
                 "exit_reason": "eod"},
                {"pick_id": 2, "triggered": True, "r_multiple": -1.0, "r_planned": -1.0,
                 "exit_reason": "stop"}]

    class _MD:
        def minute_bars(self, *a, **k):
            return []

        def daily_bar(self, *a, **k):
            return None

    evening._deterministic_events(con, _MD(), D, picks, outcomes, {"scored": 0})
    cal = con.execute("SELECT ticker, realized_r FROM calibration").fetchall()
    assert cal == [("MORN", 0.9)]                     # planned-risk R, morning lane only
    notes = [r[0] for r in con.execute("SELECT note FROM open_question_progress").fetchall()]
    assert notes and not any(n.startswith("RUNR") for n in notes)


def test_q1_counts_only_observations_where_both_entries_filled(con, monkeypatch):
    from agent.loops import evening
    sims = iter([{"triggered": False}, {"triggered": True, "r_multiple": 0.2},
                 {"triggered": True, "r_multiple": 0.8}, {"triggered": True, "r_multiple": 0.1}])
    monkeypatch.setattr(evening, "simulate_pick", lambda *a, **k: next(sims))
    shadow = {"gap_fade_notes": [
        ({"ticker": "AAA", "direction": "short", "entry_price": 10, "stop_price": 11,
          "target_price": 8}, {}, [{"o": 10}]),
        ({"ticker": "WING", "direction": "short", "entry_price": 10, "stop_price": 11,
          "target_price": 8}, {}, [{"o": 10}])]}
    n = evening._q1_gap_fade_mechanics(con, None, D, [], [], shadow)
    rows = con.execute("SELECT increment, note FROM open_question_progress ORDER BY note").fetchall()
    assert n == 1
    assert rows[0] == (0, "AAA: [shadow] planned-entry R=untriggered, open-print R=0.2")
    assert rows[1] == (1, "WING: [shadow] planned-entry R=0.8, open-print R=0.1")


# ------------------------------------------------ 4. answerable questions

def _trade(con, pid, conv=3, r=-1.0, stop_atr=None, setup="gap_go", direction="long",
           trig=True, exit_reason="eod", runner=False):
    con.execute("INSERT INTO picks (pick_id, plan_date, ticker, direction, setup_type, "
                "conviction, size_r, entry_price, stop_price, target_price) "
                "VALUES (?,?,?,?,?,?,1.0,10,9,12)",
                [pid, D, f"T{pid}", direction, "dto_runner" if runner else setup, conv])
    con.execute("INSERT INTO outcomes (pick_id, eval_date, triggered, r_multiple, r_planned, "
                "exit_reason, stop_dist_atr) VALUES (?,?,?,?,?,?,?)",
                [pid, D, trig, r if trig else None, r if trig else None, exit_reason, stop_atr])
    if not runner:
        ledger.add_calibration_row(con, D, f"T{pid}", conv, r if trig else None)


def _q(evals, qid):
    return next(e for e in evals if e["id"] == qid)


def test_q3_is_not_answerable_when_an_arm_is_short_however_large_the_total(con):
    """v9: 25 graded picks, 3 of them at conviction 4-5 -- and a conv 3 vs 2
    contrast at t ~ 0.9 written up as a finding."""
    for i in range(22):
        _trade(con, i + 1, conv=3 if i % 2 else 2, r=-0.5 - 0.1 * (i % 3))
    for i in range(3):
        _trade(con, 100 + i, conv=4, r=0.2)
    q3 = _q(question_eval.evaluate(con), 3)
    assert q3["answerable"] is False and q3["result"] is None
    assert "4-5 has 3" in q3["reason"]


def test_q9_ignores_runner_stops_and_compares_trades_not_stopped_trades(con):
    """v9 counted 20 stopped morning trades toward Q9, then answered it with
    the runner lane's stop buckets."""
    for i in range(12):
        _trade(con, i + 1, stop_atr=0.3, r=-1.0, exit_reason="stop")
    for i in range(30):
        _trade(con, 100 + i, stop_atr=1.5, r=-1.0, runner=True)
    q9 = _q(question_eval.evaluate(con), 9)
    assert q9["answerable"] is False
    assert q9["progress"] == 0                       # the 1-2 ATR arm is empty
    assert "stop 1-2 ATR has 0" in q9["reason"]


def test_a_significant_two_arm_result_states_its_direction_and_noise_does_not(con):
    for i in range(10):
        _trade(con, i + 1, stop_atr=1.5, r=1.0 + 0.05 * i)
        _trade(con, 100 + i, stop_atr=0.3, r=-1.0 - 0.05 * i)
    q9 = _q(question_eval.evaluate(con), 9)
    assert q9["answerable"] and "stop 1-2 ATR did better" in q9["result"]

    c2 = duckdb.connect(":memory:")
    c2.execute(ledger._SCHEMA)
    for m in ledger._MIGRATIONS:
        c2.execute(m)
    for i in range(10):
        _trade(c2, i + 1, stop_atr=1.5, r=(-1) ** i * 1.0)
        _trade(c2, 100 + i, stop_atr=0.3, r=(-1) ** (i + 1) * 1.0)
    noisy = _q(question_eval.evaluate(c2), 9)
    assert noisy["answerable"] and "NOT distinguishable" in noisy["result"]
    c2.close()


def test_q13_needs_both_arms(con):
    for i in range(25):
        _trade(con, i + 1, r=-0.7)                   # R:R 2.0 taken trades
    q13, q14 = _q(question_eval.evaluate(con), 13), _q(question_eval.evaluate(con), 14)
    assert q13["progress"] == 25 and q13["answerable"] is False
    assert "no conclusion from one arm" in q13["reason"]
    assert q14["answerable"] is False


def test_q5_cannot_be_answered_by_rows_with_no_kill_check(con):
    for i in range(30):
        con.execute("INSERT INTO rejections (rejection_id, plan_date, ticker, reason, "
                    "eod_outcome, forgone_r) VALUES (?,?,?,?,?,?)",
                    [i + 1, date(2026, 8, 1 + i % 28), f"R{i}", "x", "would_have_lost", -0.5])
    q5 = _q(question_eval.evaluate(con), 5)
    assert q5["answerable"] is False and q5["progress"] == 0
    assert "30 scored rejections carry no kill check" in q5["detail"]


# ----------------------------------------- 5. degenerate buckets, 6. one session

def test_a_cell_holding_the_whole_lane_is_not_a_finding():
    comps = [Comparison("runner_stop_atr_bucket", "<0.5", 992, -0.26, 1.2),
             Comparison("runner_stop_atr_bucket", "0.5-1", 11, -0.2, 1.1),
             Comparison("runner_setup_edge", "all_candidates", 1003, -0.25, 1.2)]
    res = evaluate_search(comps)
    big, small, lane = res.comparisons
    assert big.test == "degenerate" and big.p_value is None and not big.significant
    assert "restates the lane mean" in big.note
    assert small.test == "one_sample" and small.p_value is not None
    assert lane.test == "one_sample" and lane.significant       # the lane mean, tested once


def test_one_contested_session_gives_no_selection_verdict():
    from agent.runner_research import selection_edge
    rows = ([{"session_date": D, "selected": True, "r_planned": -0.8 + 0.01 * i}
             for i in range(9)]
            + [{"session_date": D, "selected": False, "r_planned": -0.3 + 0.01 * i}
               for i in range(40)])
    se = selection_edge(rows)
    assert se["underpowered"] is True
    assert "no direction may be read" in se["verdict"]


def test_structural_pairs_use_planned_r_like_the_one_d_search():
    from agent import structural
    assert structural._r({"r_planned": -1.0, "r_multiple": -3.9}) == -1.0
    assert structural._r({"r_multiple": 0.5}) == 0.5
