"""
Daily floor: the desk trades every session.

Pinned here:
  - when review and validation leave the plan empty, the best VALID rejected
    candidate goes on at minimum size, labeled `daily_floor`
  - candidates are tried softest kill first, and each must clear the same
    _validate_plan a regular pick clears (an ungrounded price stays dropped)
  - the streak and drawdown breakers still size the floor pick, but cannot
    veto it (they only reset when the desk trades)
  - the risk manager's own `floor_pick` is sized and labeled the same way
  - the prompts, config and email say so
"""
from __future__ import annotations

import io
import json
from datetime import date

import pytest

from agent import ledger
from agent.config import Config
from agent.delivery import morning_email_html
from agent.loops.morning import (_apply_daily_floor, _floor_candidates,
                                 _normalize_floor_picks, _run_daily_floor,
                                 _validate_plan)

FLOOR = {"enabled": True, "size_r": 0.25, "min_rr": 1.0}


def _cfg(**overrides) -> Config:
    raw = {"max_picks": 4, "min_size_r": 0.25, "max_size_r": 1.5,
           "max_total_open_risk_r": 3.0, "drawdown_pause_r": -4.0, "daily_floor": FLOOR}
    raw.update(overrides)
    return Config(raw=raw)


def _cand(ticker, target=11.5, conviction=3, entry=10.0, stop=9.0, **extra):
    return {"ticker": ticker, "direction": "long", "setup_type": "gap_go",
            "conviction": conviction, "thesis": f"{ticker} thesis",
            "entry_plan": {"type": "limit", "price": entry}, "entry_price": entry,
            "stop_price": stop, "target_price": target, **extra}


def _empty_plan(*rejected):
    return {"no_trade": True, "no_trade_reason": "all fail R:R", "picks": [],
            "rejected": [{"ticker": t, "reason": "x", "primary_kill_check": k}
                         for t, k in rejected]}


def _validator(cfg, refs, streak=None, drawdown=None):
    return lambda trial: _validate_plan(trial, cfg, streak, drawdown, ref_prices=refs,
                                        atr_map={}, earnings_map={})


# ------------------------------------------------------------------ ranking

def test_softest_kill_first_then_conviction_then_rr():
    cands = {"candidates": [_cand("GRND", target=12.0), _cand("REGM"),
                            _cand("RRLO", target=11.2), _cand("RRHI", target=11.8),
                            _cand("CONV", conviction=4, target=11.1)]}
    plan = _empty_plan(("GRND", "grounding"), ("REGM", "regime_fit"), ("RRLO", "rr"),
                       ("RRHI", "rr"), ("CONV", "conviction"))
    order = [p["ticker"] for p in _floor_candidates(cands, plan, _cfg())]
    assert order == ["CONV", "RRHI", "RRLO", "REGM", "GRND"]


def test_candidates_are_minimum_sized_capped_and_labeled():
    cands = {"candidates": [_cand("AAA", conviction=5, probe_for="Q1")]}
    [pick] = _floor_candidates(cands, _empty_plan(("AAA", "rr")), _cfg())
    assert (pick["size_r"], pick["conviction"], pick["exploration_lane"]) == (0.25, 3, "daily_floor")
    assert pick["exploration_rr"] == 1.5
    assert "probe_for" not in pick, "attribution follows the admission path"


def test_taken_incomplete_and_sub_floor_rr_candidates_are_skipped():
    cands = {"candidates": [_cand("TAKEN"), _cand("NOSTOP", stop=None),
                            _cand("FLAT", stop=10.0), _cand("THIN", target=10.8),
                            _cand("OK"), _cand("OK")]}
    plan = {"picks": [{"ticker": "TAKEN"}], "rejected": []}
    assert [p["ticker"] for p in _floor_candidates(cands, plan, _cfg())] == ["OK"]


def test_disabled_floor_offers_nothing():
    cands = {"candidates": [_cand("AAA")]}
    assert _floor_candidates(cands, _empty_plan(("AAA", "rr")),
                             _cfg(daily_floor={"enabled": False})) == []


# ------------------------------------------------------------------ admission

def test_first_valid_candidate_is_admitted_and_the_day_trades():
    cfg = _cfg()
    cands = {"candidates": [_cand("NOQT"), _cand("AAA", target=11.2)]}
    plan = _empty_plan(("NOQT", "rr"), ("AAA", "rr"))
    notes = _apply_daily_floor(plan, _floor_candidates(cands, plan, cfg),
                               _validator(cfg, {"AAA": 10.05}))
    assert [p["ticker"] for p in plan["picks"]] == ["AAA"]
    assert plan["no_trade"] is False and "no_trade_reason" not in plan
    assert plan["stand_down_overridden"] == "all fail R:R"
    assert plan["total_risk_r"] == 0.25
    assert any("NOQT" in n and "could not be verified" in n for n in notes), \
        "the unpriced candidate was tried and dropped by the validator, fail closed"
    assert notes[-1].startswith("DAILY FLOOR: AAA admitted")


def test_no_valid_order_leaves_a_no_trade_day():
    cfg = _cfg()
    cands = {"candidates": [_cand("AAA"), _cand("BBB")]}
    plan = _empty_plan(("AAA", "rr"), ("BBB", "rr"))
    # BBB's entry is 40% off its live quote: ungrounded, dropped.
    notes = _apply_daily_floor(plan, _floor_candidates(cands, plan, cfg),
                               _validator(cfg, {"BBB": 16.7}))
    assert plan["picks"] == [] and plan["no_trade"] is True
    assert notes[-1].startswith("DAILY FLOOR: no candidate survived validation")


def test_a_plan_with_picks_is_left_alone():
    cfg = _cfg()
    plan = {"picks": [{"ticker": "REG"}], "rejected": []}
    assert _apply_daily_floor(plan, [_cand("AAA")], _validator(cfg, {"AAA": 10.0})) == []
    assert plan["picks"] == [{"ticker": "REG"}]


@pytest.mark.parametrize("streak, drawdown", [
    ({"kind": "losing", "length": 4}, None),
    (None, {"n": 10, "sum_weighted_r": -5.0}),
])
def test_breakers_size_the_floor_pick_but_cannot_veto_it(streak, drawdown):
    cfg = _cfg()
    floor_pick = _floor_candidates({"candidates": [_cand("AAA", conviction=2)]},
                                   _empty_plan(("AAA", "rr")), cfg)[0]
    regular = dict(_cand("BBB", conviction=3), size_r=1.0)
    out, warnings = _validate_plan({"no_trade": False, "picks": [floor_pick, regular]}, cfg,
                                   streak, drawdown, ref_prices={"AAA": 10.0, "BBB": 10.0},
                                   atr_map={}, earnings_map={})
    assert [p["ticker"] for p in out["picks"]] == ["AAA"], "the regular low-conviction pick still drops"
    assert out["picks"][0]["size_r"] == 0.25


class _NoNetwork:
    """Stands in for MarketData: the floor must not fetch quotes, and its ATR /
    earnings top-ups fail open."""
    def pick_quotes(self, *a, **k):
        raise AssertionError("the floor prices from quotes already fetched this run")

    def daily_context(self, tickers):
        return {}

    def next_earnings(self, tickers):
        return {}


def test_run_daily_floor_prices_from_the_bundle_and_candidate_quotes_only():
    cfg = _cfg()
    cands = {"candidates": [_cand("WEB"), _cand("GAP", target=11.2)]}
    plan = _empty_plan(("WEB", "rr"), ("GAP", "rr"))
    bundle = {"premarket_gappers": [{"symbol": "GAP", "last": 10.0}]}
    # WEB ranks first (higher R:R) and is priced by the candidate-quote pass.
    notes = _run_daily_floor(plan, cands, cfg, _NoNetwork(), bundle, {"WEB": 10.02}, None, None)
    assert [p["ticker"] for p in plan["picks"]] == ["WEB"], notes
    # Without that quote, WEB is unpriced and dropped; GAP is next.
    plan = _empty_plan(("WEB", "rr"), ("GAP", "rr"))
    _run_daily_floor(plan, cands, cfg, _NoNetwork(), bundle, {}, None, None)
    assert [p["ticker"] for p in plan["picks"]] == ["GAP"]


# ------------------------------------------------------------------ risk manager's own floor pick

def test_risk_managers_floor_pick_is_sized_and_labeled():
    plan = {"no_trade": True, "no_trade_reason": "contradiction",
            "picks": [dict(_cand("AAA", probe_for="Q1"), size_r=1.5, floor_pick=True),
                      dict(_cand("BBB"), size_r=1.0)]}
    _normalize_floor_picks(plan, _cfg())
    aaa, bbb = plan["picks"]
    assert (aaa["size_r"], aaa["exploration_lane"], aaa["exploration_rr"]) == (0.25, "daily_floor", 1.5)
    assert "probe_for" not in aaa
    assert bbb["size_r"] == 1.0 and "exploration_lane" not in bbb
    assert plan["no_trade"] is False and "no_trade_reason" not in plan


def test_unflagged_plans_are_untouched():
    plan = {"no_trade": True, "no_trade_reason": "r", "picks": []}
    _normalize_floor_picks(plan, _cfg())
    assert plan == {"no_trade": True, "no_trade_reason": "r", "picks": []}


# ------------------------------------------------------------------ persistence, prompts, config, email

@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "f.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


def test_the_floor_label_is_persisted_on_the_pick(con):
    pick = _floor_candidates({"candidates": [_cand("AAA")]}, _empty_plan(("AAA", "rr")), _cfg())[0]
    pid = ledger.save_daily_plan(con, date(2026, 9, 30), {"no_trade": False, "picks": [pick],
                                                          "rejected": []},
                                 {"candidates": []}, "chop")[0]
    row = con.execute("SELECT exploration_lane, exploration_rr, size_r FROM picks "
                      "WHERE pick_id = ?", [pid]).fetchone()
    assert row == ("daily_floor", 1.5, 0.25)


def test_prompts_say_the_desk_trades_every_session():
    risk = io.open("prompts/morning_risk.md", encoding="utf-8").read()
    assert "The desk trades every session." in risk
    assert '"floor_pick": true' in risk and "{{FLOOR_SIZE_R}}R" in risk
    assert "no_trade_reason" not in risk, "standing down is not one of the risk manager's outcomes"
    assert "must be **>= 2.0** using the analyst's own numbers" in risk, "full approvals keep the gate"
    analyst = io.open("prompts/morning_analyst.md", encoding="utf-8").read()
    assert "The desk trades every session." in analyst
    miner = io.open("prompts/weekly_miner.md", encoding="utf-8").read()
    assert "may not tell the desk to stand down for the day" in miner


def test_production_config_turns_the_floor_on():
    cfg = json.loads(io.open("config.json", encoding="utf-8").read())
    assert cfg["daily_floor"]["enabled"] is True
    assert cfg["daily_floor"]["size_r"] == cfg["min_size_r"]


def test_email_labels_the_floor_pick():
    pick = _floor_candidates({"candidates": [_cand("AAA")]}, _empty_plan(("AAA", "rr")), _cfg())[0]
    html = morning_email_html(date(2026, 9, 30), {"no_trade": False, "picks": [pick]}, "")
    assert "Daily floor pick" in html and "[daily floor]" in html
    assert "AAA thesis" in html, "a floor pick carries the analyst's thesis, not a blank cell"
