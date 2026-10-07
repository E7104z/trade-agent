"""
The 2026-10-06 plan review, fixed (specs/PLAN_INTEGRITY_v1.md), each case
built from that day's real levels:

  * CEG, the risk manager's approval, vanished between the plan and the tape;
    the validator now records why, and an approval dropped only by the
    discipline bar goes on at minimum size before any weaker sibling.
  * Three theses named the premarket low and priced it from memory, 0.41-0.49
    ATR below the computed value; none filled. Such entries move onto the
    computed level.
  * PTC disowned its own label and traded anyway.
  * A real duplicate (two share classes, an ETF and its top holding) could go
    on twice. The review's "GOOG and NVDA are one AI bet" did not hold up:
    their prior 20-day correlation was +0.06. And the plain 20-day number
    called CEG and HTZ one bet (+0.81) on the strength of a single shared
    gap day; the rule now needs the link to survive leaving any day out.
"""
from __future__ import annotations

import json
from datetime import date

import duckdb
import pytest

from agent import ledger, selfcheck
from agent.config import Config
from agent.delivery import morning_email_html
from agent.loops import morning
from agent.loops.morning import (_build_rejections, _level_fix, _ranked_rejects, _run_coverage,
                                 _same_bet, _validate_plan)

D = date(2026, 10, 6)


def _pick(t, entry, stop, target, thesis, et="limit", direction="long", setup="catalyst_momentum",
          conv=3, **kw):
    return {"ticker": t, "direction": direction, "setup_type": setup,
            "entry_plan": {"type": et, "price": entry}, "entry_price": entry, "stop_price": stop,
            "target_price": target, "size_r": 0.5, "conviction": conv, "thesis": thesis, **kw}


# 10/06 premarket: last price, computed levels, ATR14
LEVELS = {"CEG": {"pm_low": 275.25, "pm_high": 294.99}, "GOOG": {"pm_low": 344.16, "pm_high": 346.04},
          "NVDA": {"pm_low": 239.42, "pm_high": 242.4, "pm_vwap": 240.6},
          "PTC": {"pm_low": 192.32, "pm_high": 193.36},
          "RPM": {"pm_low": 92.55, "pm_high": 97.01, "pm_vwap": 94.1}}
REF = {"CEG": 286.10, "GOOG": 345.01, "NVDA": 241.04, "PTC": 192.65, "RPM": 93.48}
ATR = {"CEG": 10.81, "GOOG": 8.59, "NVDA": 5.93, "PTC": 8.38, "RPM": 2.66}

CEG = _pick("CEG", 270.0, 258.0, 295.0,
            "Entry on pullback to premarket low area ($268-270) respects the gap structure; "
            "stop below overnight support invalidates the breakout.")
GOOG = _pick("GOOG", 340.0, 334.0, 355.0,
             "The deal is CEG's story. Entry on pullback to overnight low area ~$340 gives structure.",
             conv=2)
NVDA = _pick("NVDA", 237.0, 232.0, 248.0,
             "In established uptrend. Entry on pullback to premarket low/VWAP area around $236-238 "
             "gives structure. Stop below overnight low invalidates today's momentum.",
             setup="trend_pullback", conv=2)
PTC = _pick("PTC", 192.0, 185.0, 202.0,
            "Deal spread 6.4%. This is NOT a momentum trade — it's M&A arbitrage, which is outside "
            "my setup taxonomy.", setup="event_driven", conv=2)


def _fix(p):
    p = json.loads(json.dumps(p))
    et = p["entry_plan"]["type"]
    return _level_fix(p, et, REF[p["ticker"]], LEVELS[p["ticker"]], ATR[p["ticker"]], 0.25), p


# ------------------------------------------------------------ level grounding

@pytest.mark.parametrize("pick, computed, shift", [
    (CEG, 275.25, 5.25), (GOOG, 344.16, 4.16), (NVDA, 239.42, 2.42)])
def test_an_entry_priced_from_memory_moves_onto_the_level_its_thesis_names(pick, computed, shift):
    fix, p = _fix(pick)
    assert fix["named"] == "premarket low" and fix["computed"] == computed
    assert p["entry_price"] == computed and p["entry_plan"]["price"] == computed
    # Risk and reward travel with the entry: the trade is the same trade.
    assert round(p["stop_price"] - pick["stop_price"], 2) == shift
    assert round(p["target_price"] - pick["target_price"], 2) == shift
    assert p["level_fix"] == fix


def test_a_thesis_naming_no_level_and_a_stop_clause_are_left_alone():
    assert _fix(PTC)[0] is None
    stop_only = dict(NVDA, thesis="Strong uptrend; buy the dip. Stop below overnight low.")
    assert _fix(stop_only)[0] is None


def test_only_a_level_on_the_side_the_order_waits_on_can_anchor_it():
    # RPM: a breakdown SHORT whose entry clause names levels ABOVE the price.
    rpm = _pick("RPM", 91.5, 96.0, 86.5, "Entry on failed bounce toward VWAP/overnight high area; "
                "short continuation below premarket low.", et="breakout", direction="short")
    assert _fix(rpm)[0] is None


def test_an_entry_within_tolerance_of_its_level_is_unchanged():
    near = dict(GOOG, entry_price=343.0, entry_plan={"type": "limit", "price": 343.0})
    assert _fix(near)[0] is None                      # 0.13 ATR from 344.16


def test_the_validator_trades_the_corrected_level():
    plan = {"no_trade": False, "picks": [json.loads(json.dumps(GOOG))]}
    out, warnings = _validate_plan(plan, Config(raw={}), ref_prices=REF, atr_map=ATR,
                                   earnings_map={"GOOG": 30}, level_map=LEVELS)
    assert out["picks"][0]["entry_price"] == 344.16
    assert any("corrected to 344.16, the premarket low its thesis names" in w for w in warnings)


# --------------------------------------------------------- dropped approvals

def test_every_drop_is_recorded_with_its_kind():
    plan = {"no_trade": False, "picks": [json.loads(json.dumps(CEG)),
                                         _pick("XLE", 91.5, 89.5, 95.0, "sector")]}
    out, _ = _validate_plan(plan, Config(raw={}), streak={"kind": "losing", "length": 3},
                            ref_prices={"CEG": 286.10}, atr_map=ATR, earnings_map={"CEG": 30})
    kinds = {x["ticker"]: x["kind"] for x in out["validator_drops"]}
    assert kinds == {"CEG": "discipline", "XLE": "validity"}
    assert "losing streak" in out["validator_drops"][0]["reason"]


def test_an_approval_dropped_by_discipline_goes_on_before_a_weaker_sibling():
    # The risk manager approved CEG and rejected GOOG (soft kill); the streak
    # bar dropped CEG. Coverage used to fill catalyst_momentum with GOOG.
    candidates = {"candidates": [GOOG, CEG]}
    plan = {"picks": [], "rejected": [{"ticker": "GOOG", "primary_kill_check": "conviction",
                                       "reason": "weak thesis"}],
            "validator_drops": [{"ticker": "CEG", "kind": "discipline", "reason": "streak"}]}
    ranked = _ranked_rejects(candidates, plan, "coverage", 0.25, 1.0)
    assert [p["ticker"] for p in ranked][:1] == ["CEG"]
    # ... and a validity drop earns no such place.
    plan["validator_drops"][0]["kind"] = "validity"
    assert [p["ticker"] for p in _ranked_rejects(candidates, plan, "coverage", 0.25, 1.0)][0] == "GOOG"


def test_the_shadow_ledger_says_what_happened_to_each_candidate():
    candidates = {"candidates": [CEG, GOOG]}
    plan = {"picks": [], "rejected": [],
            "validator_drops": [{"ticker": "CEG", "kind": "discipline",
                                 "reason": "CEG: dropped — losing streak >= 3 demands conviction >= 4 (had 3)"}]}
    rows = {r["ticker"]: r for r in _build_rejections(candidates, plan)}
    assert rows["CEG"]["reason"].startswith("approved by the risk manager, then dropped by the validator")
    assert rows["CEG"]["source_stage"] == "validator_dropped"
    assert rows["GOOG"]["reason"] == "the risk manager neither approved nor rejected it (absent from its output)"
    assert rows["GOOG"]["source_stage"] == "risk_unaddressed"


# ------------------------------------------------------- labels and one bet

@pytest.mark.parametrize("cand, row, ok", [
    (PTC, {"primary_kill_check": "staleness", "reason": "announced last week"}, False),
    (dict(PTC, thesis="Deal spread."), {"primary_kill_check": "rr",
                                        "reason": "Wrong time horizon for this desk."}, False),
    (dict(PTC, thesis="Deal spread."), {"primary_kill_check": "rr", "reason": "M&A arb is not intraday"}, False),
    (dict(PTC, thesis="Deal spread."), {"primary_kill_check": "staleness",
                                        "kill_checks": ["staleness", "setup_fit"]}, False),
    (GOOG, {"primary_kill_check": "conviction", "reason": "weak"}, True),
    (dict(GOOG, thesis="Not intraday-dependent catalyst."), {"primary_kill_check": "rr"}, True),
])
def test_a_trade_that_disowns_its_label_never_reaches_a_test_lane(cand, row, ok):
    plan = {"picks": [], "rejected": [{"ticker": cand["ticker"], **row}]}
    assert bool(_ranked_rejects({"candidates": [cand]}, plan, "coverage", 0.25, 1.0)) is ok


def test_one_shared_news_day_is_not_one_bet():
    from agent.loops.morning import _one_risk_unit
    # 19 unrelated small days, then both names gap ~+12-15% on the same day:
    # the CEG/HTZ shape of 10/06 (+0.81 with the day, +0.49 without it).
    a = [0.004, -0.006, 0.002, -0.003, 0.005, -0.002, 0.001, 0.003, -0.004, 0.002,
         -0.001, 0.006, -0.005, 0.002, 0.000, -0.003, 0.004, -0.002, 0.001, 0.12]
    b = [-0.003, 0.002, 0.005, 0.004, -0.006, 0.001, -0.004, 0.002, 0.003, -0.005,
         0.006, -0.002, 0.001, -0.003, 0.004, 0.002, -0.001, 0.005, -0.004, 0.15]
    from agent.loops.morning import _pearson
    assert _pearson(a, b) > 0.7                     # the plain number says one bet
    assert _one_risk_unit(a, b) is None             # it rests on one day
    # Two share classes of one company move together every day.
    c = [x * 1.01 + 0.0002 for x in a[:-1]] + [0.119]
    assert _one_risk_unit(a, c) > 0.9


def test_correlated_picks_pointing_the_same_way_are_one_bet():
    pairs = [["GOOG", "NVDA", 0.82], ["NVDA", "TLT", -0.75]]
    held = [{"ticker": "GOOG", "direction": "long"}]
    assert _same_bet({"ticker": "NVDA", "direction": "long"}, held, pairs).startswith("GOOG")
    assert _same_bet({"ticker": "NVDA", "direction": "short"}, held, pairs) is None   # a hedge
    assert _same_bet({"ticker": "NVDA", "direction": "long"},
                     [{"ticker": "TLT", "direction": "short"}], pairs).startswith("TLT")
    assert _same_bet({"ticker": "NVDA", "direction": "long"}, held, []) is None


def test_coverage_skips_the_second_name_of_one_bet():
    candidates = {"candidates": [GOOG, NVDA]}
    plan = {"picks": [], "rejected": [{"ticker": "GOOG", "primary_kill_check": "conviction"},
                                      {"ticker": "NVDA", "primary_kill_check": "conviction"}]}

    def validate_one(trial):
        return trial, []
    cfg = Config(raw={"coverage_lane": {"enabled": True, "size_r": 0.25, "min_rr": 1.0}})
    notes = _run_coverage(plan, candidates, cfg, validate_one, [["GOOG", "NVDA", 0.82]])
    assert [p["ticker"] for p in plan["picks"]] == ["GOOG"]
    assert any("NVDA: skipped — the same bet as GOOG (20d corr +0.82)" in n for n in notes)


# ------------------------------------------------------------ premarket data

@pytest.mark.parametrize("hour, fetched", [(0, False), (3, False), (8, True)])
def test_before_four_am_there_is_no_premarket_window_to_fetch(monkeypatch, hour, fetched):
    # Before 04:00 ET the window would end before it starts; Alpaca refuses
    # that with a 400 for any symbol (verified: CEG alone, 00:18 ET 10/07),
    # which an after-midnight rehearsal logged as a failed fetch.
    from datetime import datetime
    from agent import providers
    from agent.providers import MarketData
    from agent.util import ET

    class _Alpaca:
        def __init__(self):
            self.calls = []

        def multi_bars(self, syms, tf, start, end):
            assert start < end
            self.calls.append(list(syms))
            t = datetime(2026, 10, 7, 7, 59, tzinfo=ET)
            return {s: [{"t": t, "o": 10.0, "h": 10.5, "l": 9.5, "c": 10.2, "v": 1000}] for s in syms}

    monkeypatch.setattr(providers, "now_et", lambda: datetime(2026, 10, 7, hour, 18, tzinfo=ET))
    md = MarketData.__new__(MarketData)
    md.cfg = Config(raw={"data": {"indicator_enrich": True, "context_enrich": False}})
    md.alpaca = _Alpaca()
    md.daily_context = lambda syms: {}
    quotes = [{"symbol": "CEG", "last": 10.2}]
    md._enrich_indicators(quotes)
    assert bool(md.alpaca.calls) is fetched
    assert (quotes[0].get("pm_vwap") is not None) is fetched


# ---------------------------------------------------- surfaces and the check

def test_the_email_says_when_a_level_was_corrected():
    fix, p = _fix(GOOG)
    html = morning_email_html(D, {"picks": [dict(p, exploration_lane="coverage")]}, "")
    assert "[coverage]" in html and "moved from 340.0 to the computed premarket low" in html


def test_the_dashboard_marks_lanes_and_shadow_rows_traded_anyway():
    from agent import webui
    src = webui.__file__ and open(webui.__file__, encoding="utf-8").read()
    assert "traded anyway:" in src and "are test picks" in src and "to the computed" in src


def test_the_self_check_counts_fills_fixes_and_drops():
    con = duckdb.connect(":memory:")
    con.execute(ledger._SCHEMA)
    for mig in ledger._MIGRATIONS:
        con.execute(mig)
    fix, g = _fix(GOOG)
    plan = {"no_trade": False, "picks": [dict(g, size_r=0.25), dict(PTC, size_r=0.25)],
            "validator_drops": [{"ticker": "CEG", "kind": "discipline", "reason": "x"}]}
    ids = ledger.save_daily_plan(con, D, plan, {"candidates": []}, "chop")
    con.execute("INSERT INTO outcomes (pick_id, triggered) VALUES (?, TRUE)", [ids[0]])
    con.execute("INSERT INTO outcomes (pick_id, triggered) VALUES (?, FALSE)", [ids[1]])
    r = selfcheck.check_entries(con, None, D)
    assert "1 of 2 limit/breakout orders filled (50%)" in r["headline"]
    assert "1 entry moved onto the computed level their thesis named (1 filled)" in r["headline"]
    assert "discipline 1" in r["headline"]
