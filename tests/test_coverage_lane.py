"""
Coverage lane: every setup the analyst finds gets traded, every day.

Pinned here:
  - each setup the plan does not already hold gets its best rejected candidate
    at minimum size, labeled `coverage`, one per setup, up to the lane's cap
  - a broken order (blocking kill, or anything _validate_plan drops) stays off
    the tape, and the next candidate in that setup takes the slot
  - coverage sits outside the desk's own max_picks / total-risk caps
  - coverage and floor trades are exempt from the conviction bar and are left
    out of the streak and drawdown windows (they are not the desk's judgment)
  - a coverage pick keeps its shadow row, so every kill is still priced
  - the prompts describe the lane only when it is on
"""
from __future__ import annotations

import io
from datetime import date

import pytest

from agent import ledger
from agent.config import _DEFAULTS, Config
from agent.delivery import morning_email_html
from agent.llm import load_prompt
from agent.loops.morning import (_build_rejections, _coverage_prompt_text,
                                 _run_coverage, _run_test_lanes, _validate_plan)

LANE = {"enabled": True, "size_r": 0.25, "min_rr": 1.0, "max_picks": 12,
        "blocking_kills": ["grounding", "entry_type", "event_risk"]}


def _cfg(lane=LANE, **overrides) -> Config:
    raw = {"max_picks": 4, "min_size_r": 0.25, "max_size_r": 1.5,
           "max_total_open_risk_r": 3.0, "drawdown_pause_r": -4.0,
           "coverage_lane": lane, "daily_floor": {"enabled": True, "size_r": 0.25, "min_rr": 1.0}}
    raw.update(overrides)
    return Config(raw=raw)


def _cand(ticker, setup, target=11.5, conviction=3):
    return {"ticker": ticker, "direction": "long", "setup_type": setup,
            "conviction": conviction, "thesis": f"{ticker} thesis",
            "entry_plan": {"type": "limit", "price": 10.0}, "entry_price": 10.0,
            "stop_price": 9.0, "target_price": target}


def _plan(picks=(), **kills):
    return {"no_trade": not picks, "no_trade_reason": None if picks else "all fail R:R",
            "picks": list(picks),
            "rejected": [{"ticker": t, "reason": "x", "primary_kill_check": k}
                         for t, k in kills.items()]}


def _validator(cfg, refs, streak=None, drawdown=None):
    return lambda trial: _validate_plan(trial, cfg, streak, drawdown, ref_prices=refs,
                                        atr_map={}, earnings_map={})


REFS = {t: 10.0 for t in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "REG")}


def _lanes(plan):
    return [(p["ticker"], p.get("setup_type"), p.get("exploration_lane")) for p in plan["picks"]]


# ------------------------------------------------------------------ one per setup

def test_every_setup_found_is_traded_once_softest_kill_first():
    cands = {"candidates": [_cand("AAA", "gap_go"), _cand("BBB", "gap_go", target=12.0),
                            _cand("CCC", "gap_fade"), _cand("DDD", "breakout")]}
    plan = _plan(AAA="rr", BBB="regime_fit", CCC="staleness", DDD="conviction")
    _run_coverage(plan, cands, _cfg(), _validator(_cfg(), REFS))
    assert sorted(_lanes(plan)) == [("AAA", "gap_go", "coverage"), ("CCC", "gap_fade", "coverage"),
                                    ("DDD", "breakout", "coverage")]
    assert all(p["size_r"] == 0.25 and p["conviction"] <= 3 for p in plan["picks"])
    assert plan["no_trade"] is False and plan["stand_down_overridden"] == "all fail R:R"
    assert plan["total_risk_r"] == 0.75 and plan["coverage_pick_count"] == 3


def test_a_setup_the_desk_already_holds_is_not_doubled():
    reg = dict(_cand("REG", "gap_go"), size_r=1.0)
    cands = {"candidates": [reg, _cand("AAA", "gap_go"), _cand("CCC", "gap_fade")]}
    plan = _plan([reg], AAA="rr", CCC="rr")
    _run_coverage(plan, cands, _cfg(), _validator(_cfg(), REFS))
    assert _lanes(plan) == [("REG", "gap_go", None), ("CCC", "gap_fade", "coverage")]
    assert plan["total_risk_r"] == 1.25


@pytest.mark.parametrize("kill, admitted", [
    ("grounding", False), ("entry_type", False), ("event_risk", False),
    ("rr", True), ("conviction", True), ("regime_fit", True), ("staleness", True),
    ("chase", True), ("crowding", True), ("correlation", True), ("stop_quality", True),
    # 2026-10-05: a mislabel stays off whatever blocking_kills says, so a
    # momentum trade can never be recorded as a breakout.
    ("setup_fit", False),
])
def test_only_a_broken_order_stays_off_the_tape(kill, admitted):
    plan = _plan(AAA=kill)
    _run_coverage(plan, {"candidates": [_cand("AAA", "gap_go")]}, _cfg(), _validator(_cfg(), REFS))
    assert bool(plan["picks"]) is admitted


def test_the_validator_still_decides_and_the_next_candidate_takes_the_slot():
    cands = {"candidates": [_cand("AAA", "gap_go"), _cand("BBB", "gap_go", target=11.2)]}
    plan = _plan(AAA="rr", BBB="rr")
    # AAA has no live quote: fail closed. BBB is the next gap_go.
    notes = _run_coverage(plan, cands, _cfg(), _validator(_cfg(), {"BBB": 10.0}))
    assert _lanes(plan) == [("BBB", "gap_go", "coverage")]
    assert any(n.startswith("coverage AAA:") and "could not be verified" in n for n in notes)


def test_coverage_sits_outside_the_desks_own_caps_but_has_its_own():
    regs = [dict(_cand(f"R{i}", f"s{i}"), size_r=0.75) for i in range(4)]
    cands = {"candidates": regs + [_cand(t, f"x{t}") for t in ("AAA", "BBB", "CCC")]}
    plan = _plan(regs, AAA="rr", BBB="rr", CCC="rr")
    refs = {**REFS, **{f"R{i}": 10.0 for i in range(4)}}
    _run_coverage(plan, cands, _cfg(dict(LANE, max_picks=2)), _validator(_cfg(), refs))
    assert len(plan["picks"]) == 6, "4 approvals at the desk's cap, plus 2 coverage at the lane's"
    assert plan["total_risk_r"] == 3.5


def test_disabled_lane_adds_nothing():
    plan = _plan(AAA="rr")
    assert _run_coverage(plan, {"candidates": [_cand("AAA", "gap_go")]},
                         _cfg({"enabled": False}), _validator(_cfg(), REFS)) == []
    assert plan["picks"] == []


# ------------------------------------------------------------------ coverage, then floor

class _NoNetwork:
    def pick_quotes(self, *a, **k):
        raise AssertionError("test lanes price from quotes already fetched this run")

    def daily_context(self, tickers):
        return {}

    def next_earnings(self, tickers):
        return {}


def test_coverage_runs_first_and_the_floor_only_backs_it_up():
    cands = {"candidates": [_cand("AAA", "gap_go"), _cand("CCC", "gap_fade")]}
    bundle = {"premarket_gappers": [{"symbol": "AAA", "last": 10.0}, {"symbol": "CCC", "last": 10.0}]}
    plan = _plan(AAA="rr", CCC="rr")
    _run_test_lanes(plan, cands, _cfg(), _NoNetwork(), bundle, {}, None, None)
    assert {p["exploration_lane"] for p in plan["picks"]} == {"coverage"}
    # Every candidate killed for a broken order: coverage takes none, the floor
    # still tries them through the validator and keeps the valid one.
    plan = _plan(AAA="grounding", CCC="grounding")
    _run_test_lanes(plan, cands, _cfg(), _NoNetwork(), bundle, {}, None, None)
    assert [p["exploration_lane"] for p in plan["picks"]] == ["daily_floor"]


# ------------------------------------------------------------------ breakers

def test_coverage_is_exempt_from_the_conviction_bar_not_the_size_clamp():
    cov = dict(_cand("AAA", "gap_go", conviction=2), size_r=0.25, exploration_lane="coverage")
    reg = dict(_cand("BBB", "gap_fade", conviction=3), size_r=1.0)
    out, _ = _validate_plan({"no_trade": False, "picks": [cov, reg]}, _cfg(),
                            {"kind": "losing", "length": 5}, None,
                            ref_prices=REFS, atr_map={}, earnings_map={})
    assert [(p["ticker"], p["size_r"]) for p in out["picks"]] == [("AAA", 0.25)]


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "c.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


def _trade(con, d, r, lane=None, i=[0]):
    i[0] += 1
    pid = con.execute(
        """INSERT INTO picks (plan_date, ticker, direction, setup_type, size_r, entry_type,
                              entry_price, stop_price, target_price, conviction, exploration_lane)
           VALUES (?, ?, 'long', 'gap_go', 0.25, 'open', 10, 9, 12, 3, ?) RETURNING pick_id""",
        [d, f"T{i[0]}", lane]).fetchone()[0]
    ledger.save_outcome(con, {"pick_id": pid, "eval_date": d, "triggered": True,
                              "r_multiple": r, "exit_reason": "eod"})


def test_breakers_read_the_desks_judgment_only(con):
    d = date(2026, 9, 28)
    _trade(con, d, 1.2)                       # the desk's own winner
    for lane in ("coverage", "daily_floor", "coverage"):
        _trade(con, date(2026, 9, 29), -1.0, lane)
    assert ledger.current_streak(con, lane="morning") == {"kind": "losing", "length": 3}
    assert ledger.current_streak(con, lane="morning", judgment_only=True) == {"kind": "winning", "length": 1}
    assert ledger.trailing_weighted_r(con, 10, lane="morning", judgment_only=True) == {
        "n": 1, "sum_weighted_r": 0.3}


# ------------------------------------------------------------------ shadow ledger

def test_a_coverage_pick_keeps_its_shadow_row():
    cands = {"candidates": [_cand("AAA", "gap_go"), _cand("REG", "gap_fade"),
                            _cand("CCC", "breakout")]}
    plan = _plan([dict(_cand("REG", "gap_fade"), size_r=1.0),
                  dict(_cand("AAA", "gap_go"), size_r=0.25, exploration_lane="coverage")],
                 AAA="rr", CCC="grounding")
    rows = {r["ticker"]: r for r in _build_rejections(cands, plan)}
    assert set(rows) == {"AAA", "CCC"}, "the approval has no shadow row; the coverage trade does"
    assert rows["AAA"]["primary_kill_check"] == "rr"


# ------------------------------------------------------------------ prompts, config, email

def test_prompts_describe_the_lane_only_when_it_is_on():
    on, off = _coverage_prompt_text(_cfg()), _coverage_prompt_text(_cfg({"enabled": False}))
    assert off == {"COVERAGE_ANALYST": "", "COVERAGE_SECTION": ""}
    assert "`grounding`, `entry_type`, `event_risk`" in on["COVERAGE_SECTION"]
    assert "0.25R" in on["COVERAGE_SECTION"]
    for text in (on, off):
        risk = load_prompt("morning_risk", {**text, "FLOOR_SIZE_R": "0.25"})
        analyst = load_prompt("morning_analyst", text)
        assert "{{COVERAGE" not in risk and "{{COVERAGE" not in analyst
    assert "# COVERAGE" in load_prompt("morning_risk", on)
    assert "# COVERAGE" not in load_prompt("morning_risk", off)


def test_the_analyst_hunts_one_candidate_per_setup():
    analyst = io.open("prompts/morning_analyst.md", encoding="utf-8").read()
    assert "**one candidate per setup**" in analyst
    assert '"setups_without_instance"' in analyst
    assert "8–10 candidate trades" not in analyst


def test_the_lane_is_off_unless_config_turns_it_on():
    assert _DEFAULTS["coverage_lane"]["enabled"] is False


def test_email_counts_coverage_trades():
    picks = [dict(_cand("AAA", "gap_go"), size_r=0.25, exploration_lane="coverage"),
             dict(_cand("CCC", "gap_fade"), size_r=0.25, exploration_lane="coverage")]
    html = morning_email_html(date(2026, 9, 30), {"no_trade": False, "picks": picks}, "")
    assert "2 coverage trades:" in html and "[coverage]" in html
