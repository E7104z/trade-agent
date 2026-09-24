"""
EVIDENCE_BACKLOG_SPEC_v1 — work the n<8 list every day.

Pinned here:
  Phase 1  the backlog is counts, targets and rates ONLY, ranked rules >
           questions > cells, recomputed nightly
  Phase 2  the morning desk sees it; a labeled probe can be admitted at
           minimum size only when the sole defect was rr or conviction
  Phase 3  the risk manager is no longer paid to stand down; R:R >= 2.0 intact
  Phase 4  a daily digest of what was collected and what broke
  Phase 5  shadow and population rows are recorded against open theses but
           never counted toward validation
"""
from __future__ import annotations

import io
from datetime import date

import pytest

from agent import backlog, ledger, research
from agent.config import Config
from agent.loops.morning import _add_exploration_pick
from agent.stats import Comparison

D = date(2026, 9, 16)


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "b.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


def _trade(con, d, setup, r, stop_dist_atr=0.7, i=[0]):
    i[0] += 1
    pid = con.execute(
        """INSERT INTO picks (plan_date, ticker, direction, setup_type, size_r, entry_type,
                              entry_price, stop_price, target_price, conviction)
           VALUES (?, ?, 'long', ?, 0.5, 'open', 10, 9, 12, 3) RETURNING pick_id""",
        [d, f"T{i[0]}", setup]).fetchone()[0]
    ledger.save_outcome(con, {"pick_id": pid, "eval_date": d, "triggered": True,
                              "r_multiple": r, "exit_reason": "eod", "stop_dist_atr": stop_dist_atr})


# ------------------------------------------------------------------ Phase 1

def test_the_backlog_table_holds_no_effect_sizes(con):
    cols = {c[0] for c in con.execute("DESCRIBE evidence_backlog").fetchall()}
    assert cols == {"session_date", "rank", "item_type", "item_id", "lane", "n", "target",
                    "rate_10s", "eta_sessions", "stalled", "what_counts"}
    for forbidden in ("mean", "win", "p_value", "q_value", "effect", "avg"):
        assert not any(forbidden in c for c in cols)


def test_compute_ranks_rules_then_questions_then_cells(con):
    P = {"lane": "morning", "conditions": [{"dimension": "setup_type", "level": "gap_go"}]}
    research.register_candidate(con, {"statement": "g", "predicate": P}, date(2026, 9, 1),
                                [Comparison("setup_type", "gap_go", 9, 0.1, 1.0)], {})
    for i in range(3):
        _trade(con, date(2026, 9, 10 + i), "gap_go", 0.5)
    ledger.add_question_progress(con, 3, date(2026, 9, 11), 1, "x")

    items = backlog.compute(con, D)
    types = [i["item_type"] for i in items]
    assert types[0] == "rule"
    assert types.index("question") < types.index("cell")
    cell = next(i for i in items if i["item_id"] == "morning:setup_type=gap_go")
    assert cell["n"] == 3 and cell["target"] == 8 and cell["rate_10s"] == 3
    assert cell["eta_sessions"] == 17 and cell["stalled"] is False
    assert backlog.compute(con, D) and len(backlog.latest(con)) == len(items), "idempotent"


def test_a_cell_at_the_floor_leaves_the_backlog(con):
    for i in range(8):
        _trade(con, date(2026, 9, 1 + i), "breakout", 0.2)
    ids = {i["item_id"] for i in backlog.compute(con, D)}
    assert "morning:setup_type=breakout" not in ids


def test_morning_block_is_counts_and_says_so():
    md = backlog.morning_block([{"item_id": "morning:stop_atr_bucket=0.5-1", "item_type": "cell",
                                 "n": 3, "target": 8, "eta_sessions": None, "stalled": True,
                                 "what_counts": "a fill-valid morning trade with stop_atr_bucket=0.5-1"}])
    assert "DATA-STARVED" in md and "3/8" in md and "STALLED" in md
    assert "COUNTS toward a threshold, not findings" in md


def test_morning_memory_carries_the_backlog(con):
    for i in range(2):
        _trade(con, date(2026, 9, 10 + i), "gap_go", 0.5)
    backlog.compute(con, D)
    from agent.config import load_config
    from agent.memory import build_morning_memory
    mem = build_morning_memory(con, load_config())
    assert "DATA-STARVED" in mem["text"]
    assert "morning:setup_type=gap_go" in mem["backlog_ids"]


# ------------------------------------------------------------------ Phase 2

LANE = {"enabled": True, "min_rr": 1.8, "max_rr": 2.0, "max_picks": 2, "size_r": 0.25,
        "allow_with_picks": True, "backlog_probe": True}


def _cand(ticker, target=12.4, probe=None):
    c = {"ticker": ticker, "direction": "long", "setup_type": "gap_go", "conviction": 4,
         "entry_plan": {"type": "limit", "price": 10.0}, "entry_price": 10.0,
         "stop_price": 9.0, "target_price": target}
    if probe:
        c["probe_for"] = probe
    return c


def _plan(picks, *rejected):
    return {"no_trade": not picks, "picks": list(picks),
            "rejected": [{"ticker": t, "reason": "x", "primary_kill_check": k} for t, k in rejected]}


def test_a_probe_killed_on_rr_or_conviction_is_admitted_at_minimum_size():
    cands = {"candidates": [_cand("AAA", probe="morning:setup_type=gap_go"),
                            _cand("BBB", target=13.0, probe="Q3")]}
    plan = _plan([{"ticker": "REG"}], ("AAA", "rr"), ("BBB", "conviction"))
    assert _add_exploration_pick(cands, plan, Config(raw={"exploration_lane": LANE}),
                                 ["morning:setup_type=gap_go", "Q3"])
    probes = [p for p in plan["picks"] if p.get("exploration_lane") == "backlog_probe"]
    assert [(p["ticker"], p["probe_for"], p["size_r"], p["conviction"]) for p in probes] == [
        ("AAA", "morning:setup_type=gap_go", 0.25, 3), ("BBB", "Q3", 0.25, 3)]
    assert plan["picks"][0] == {"ticker": "REG"}, "regular picks are untouched"


@pytest.mark.parametrize("kill, target, ids, admitted", [
    ("staleness", 12.4, ["P1"], False),   # a validity defect stays binding
    ("grounding", 12.4, ["P1"], False),
    ("rr", 11.5, ["P1"], False),           # R:R 1.5 < min_rr 1.8
    ("rr", 12.4, ["OTHER"], False),        # not a top backlog item
    ("rr", 12.4, ["P1"], True),
])
def test_probe_gates(kill, target, ids, admitted):
    plan = _plan([], ("AAA", kill))
    got = _add_exploration_pick({"candidates": [_cand("AAA", target, probe="P1")]}, plan,
                                Config(raw={"exploration_lane": LANE}), ids)
    assert got is admitted


def test_the_cap_spans_both_exploration_types():
    lane = dict(LANE, max_picks=1)
    cands = {"candidates": [_cand("AAA", target=11.9), _cand("BBB", probe="P1")]}
    plan = _plan([], ("AAA", "rr"), ("BBB", "rr"))
    _add_exploration_pick(cands, plan, Config(raw={"exploration_lane": lane}), ["P1"])
    assert len(plan["picks"]) == 1


def test_legacy_behaviour_without_the_new_switches():
    lane = {"enabled": True, "min_rr": 1.8, "max_rr": 2.0, "max_picks": 1, "size_r": 0.25}
    cands = {"candidates": [_cand("AAA", target=11.9, probe="P1")]}
    busy = _plan([{"ticker": "REG"}], ("AAA", "rr"))
    assert _add_exploration_pick(cands, busy, Config(raw={"exploration_lane": lane}), ["P1"]) is False
    empty = _plan([], ("AAA", "rr"))
    assert _add_exploration_pick(cands, empty, Config(raw={"exploration_lane": lane}), ["P1"])
    assert empty["picks"][0]["exploration_lane"] == "rr_below_control"
    assert "probe_for" not in empty["picks"][0], "attribution follows the admission path"


def test_probe_for_is_persisted_on_the_pick(con):
    plan = {"no_trade": False, "picks": [dict(_cand("AAA", probe="P1"), size_r=0.25,
                                              exploration_lane="backlog_probe")],
            "rejected": []}
    pid = ledger.save_daily_plan(con, D, plan, {"candidates": []}, "chop")[0]
    row = con.execute("SELECT exploration_lane, probe_for FROM picks WHERE pick_id = ?", [pid]).fetchone()
    assert row == ("backlog_probe", "P1")


# ------------------------------------------------------------------ Phase 3

def test_the_risk_manager_is_judged_on_kills_not_paid_for_them():
    text = io.open("prompts/morning_risk.md", encoding="utf-8").read()
    assert "rewarded for what you reject" not in text
    assert "strongly consider no_trade" not in text
    assert "judged on whether your kills were RIGHT" in text
    assert "must be **>= 2.0** using the analyst's own numbers" in text   # unchanged gate


# ------------------------------------------------------------------ Phase 4

def test_daily_digest_records_collection_and_breakage_only(con):
    backlog.compute(con, D)
    payload = backlog.daily_digest(con, D, {"forward_observations": 3})
    row = con.execute("SELECT payload_json FROM daily_diagnostics WHERE session_date = ?",
                      [D]).fetchone()
    assert row and payload["forward_observations"] == 3
    assert "DAILY DIAGNOSTICS" in backlog.digest_text(payload)
    for forbidden in ("mean_r", "win_rate", "p_value", "avg_r"):
        assert forbidden not in row[0]


# ------------------------------------------------------------------ Phase 5

def test_shadow_rows_are_recorded_against_a_thesis_but_never_counted(con):
    P = {"lane": "morning", "conditions": [{"dimension": "setup_type", "level": "gap_fade"}]}
    reg = research.register_candidate(con, {"statement": "fade", "predicate": P}, date(2026, 9, 1),
                                      [Comparison("setup_type", "gap_fade", 9, 0.2, 1.0)], {})
    d = date(2026, 9, 2)
    con.execute("""INSERT INTO rejections (plan_date, ticker, direction, setup_type, reason,
                   eod_outcome, forgone_r) VALUES (?, 'ZZZ', 'short', 'gap_fade', 'rr', 'would_have_won', 1.3)""",
                [d])
    out = research.observe_session(con, d)
    assert out["recorded"] == 0 and out["counterfactual"] == 1
    st = next(s for s in research.rule_states(con) if s["rule_id"] == reg["rule_id"])
    assert st["forward_n"] == 0
    md = research.provenance_md(research.rule_states(con), {}, research.counterfactual_summary(con))
    assert "counterfactual forward (NOT counted): shadow n=1 sign —" in md


def test_the_morning_selection_shows_every_kind_of_missing_evidence():
    items = ([{"item_type": "question", "item_id": f"Q{i}", "rank": i} for i in range(1, 15)]
             + [{"item_type": "cell", "item_id": f"c{i}", "rank": 20 + i} for i in range(5)]
             + [{"item_type": "rule", "item_id": "R1", "rank": 0}])
    sel = [i["item_id"] for i in backlog.morning_selection(items, 8)]
    assert sel == ["R1", "Q1", "c0", "Q2", "c1", "Q3", "c2", "Q4"]
