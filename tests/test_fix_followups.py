"""
The follow-ups to the D3/D4 fixes that live outside those phases' allowlists
(docs/FIX_VERIFICATION.md, "Waiting on Jason"): every reader of the shadow ledger and
of the broker's result must treat a rejection the desk traded anyway as what it is.

  agent/ablation.py      the random-pick pool no longer gets a 0.0R twin of a taken trade
  agent/ledger.py        shadow_summary counts lane-traded rows apart; captured R in planned-risk R
  agent/loops/weekly.py  the miner's total line says how many were excluded
  agent/traders_log.py   "the filters saved/cost" only with a non-zero forgone R, quoting it;
                         lane trades listed with their taken R; the broker headline for live picks
  agent/chart_payload.py the shadow replay prices the way the evening does (time stop, lane pick)
"""
from __future__ import annotations

import json
import pathlib
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from agent import grading, ledger, traders_log
from agent.config import Config
from agent.loops import evening

BARS = json.loads((pathlib.Path(__file__).parent / "fixtures" / "shadow_day_bars.json")
                  .read_text(encoding="utf-8"))
D1015, D1016 = date(2026, 10, 15), date(2026, 10, 16)


def _real(key, d):
    return [{"t": datetime.combine(d, datetime.strptime(r[0], "%H:%M").time()), "o": r[1],
             "h": r[2], "l": r[3], "c": r[4], "v": r[5]} for r in BARS[f"{key}-{d}"]]


class _MD:
    def __init__(self, bars):
        self.bars = {k.upper(): v for k, v in bars.items()}
        self.cfg = SimpleNamespace(offline=True)
        self.alpaca = None

    def minute_bars(self, ticker, d):
        return self.bars.get(ticker.upper(), [])

    def daily_bar(self, ticker, d):
        return None


INTC = dict(ticker="INTC", direction="short", setup_type="breakdown_short", entry_type="breakout",
            entry_price=92.0, stop_price=96.0, target_price=86.0, time_stop="by 14:30")
CMC = dict(ticker="CMC", direction="long", setup_type="catalyst_momentum", entry_type="limit",
           entry_price=66.5, stop_price=64.0, target_price=72.0, time_stop="by 14:00")
AMAT = dict(ticker="AMAT", direction="short", setup_type="breakdown_short", entry_type="breakout",
            entry_price=418.0, stop_price=433.0, target_price=400.0, time_stop="by 14:00")


def _lane_pick(spec, lane="coverage"):
    return {**spec, "size_r": 0.25, "conviction": 3, "catalyst": "c", "thesis_final": "t",
            "invalidation": "i", "entry_plan": {"type": spec["entry_type"], "price": spec["entry_price"]},
            "exploration_lane": lane, "entry_not_before": "09:45"}


def _rejection(spec):
    return {"ticker": spec["ticker"], "direction": spec["direction"], "setup_type": spec["setup_type"],
            "reason": "R:R below the control", "entry_type": spec["entry_type"],
            "entry_price": spec["entry_price"], "stop_price": spec["stop_price"],
            "target_price": spec["target_price"], "conviction": 3, "thesis": "t",
            "source_stage": "risk_rejected", "rejection_category": "rr_below_control",
            "planned_rr": 1.4, "session_degraded": False, "primary_kill_check": "rr",
            "kill_checks": ["rr"], "entry_not_before": "09:45"}


def _untriggered(i):
    return dict(ticker=f"UNT{i}", direction="long", setup_type="trend_pullback", entry_type="limit",
                entry_price=95.0, stop_price=93.0, target_price=99.0, time_stop="by 11:00")


def _flat(d):
    return [{**b, "o": 100.0, "h": 100.2, "l": 99.8, "c": 100.0} for b in _real("INTC", D1015)]


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "followups.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


def _graded_day(con, d, lane_specs, rejected_specs, md):
    picks = [_lane_pick(s) for s in lane_specs]
    plan = {"no_trade": False, "market_stance": "t", "total_risk_r": 0.25 * len(picks), "rejected": [],
            "notes_for_evening": "", "picks": picks}
    cands = [{"ticker": s["ticker"], "direction": s["direction"], "entry_plan": {"price": s["entry_price"]},
              "time_stop": s["time_stop"]} for s in list(lane_specs) + list(rejected_specs)]
    ledger.save_daily_plan(con, d, plan, {"candidates": cands}, "chop")
    ledger.save_rejections(con, d, [_rejection(s) for s in rejected_specs])
    for o in grading.grade_day(md, ledger.picks_for_date(con, d), d, slippage_bps=5):
        ledger.save_outcome(con, o)
    return evening._grade_rejections(con, md, d, slippage_bps=5)


def _day_1015(con):
    md = _MD({"INTC": _real("INTC", D1015), "CMC": _real("CMC", D1015),
              **{f"UNT{i}": _flat(D1015) for i in range(1, 6)}})
    return _graded_day(con, D1015, [INTC, CMC], [INTC, CMC] + [_untriggered(i) for i in range(1, 6)], md), md


# ------------------------------------------------------------------- ablation pool

def test_a_lane_traded_rejection_is_not_pooled_as_a_second_candidate(con):
    from agent.ablation import candidate_pool
    _day_1015(con)
    day = candidate_pool(con)[str(D1015)]
    assert sorted(c["ticker"] for c in day["taken"]) == ["CMC", "INTC"]
    assert sorted(c["ticker"] for c in day["rejected"]) == [f"UNT{i}" for i in range(1, 6)]


# ------------------------------------------------------------------ shadow_summary

def test_the_shadow_summary_counts_lane_trades_apart(con):
    _day_1015(con)
    sh = ledger.shadow_summary(con, since=D1015)
    assert sh["total"] == 5 and sh["traded_via_lane"] == 2
    assert sh["scratch"] == 5 and sh["net_forgone_r"] == 0.0 and sh["scored_rows"] == 5
    assert sum(c["n"] for c in sh["by_category"]) == 5
    # captured R is the taken trades' planned-risk R, the unit forgone R is in now
    taken = con.execute("SELECT SUM(r_planned) FROM outcomes WHERE triggered").fetchone()[0]
    assert sh["captured_r_window"] == round(taken, 2)


def test_a_session_whose_only_rejections_were_traded_has_no_shadow_session(con):
    md = _MD({"AMAT": _real("AMAT", D1016)})
    _graded_day(con, D1016, [AMAT], [AMAT], md)
    sh = ledger.shadow_summary(con)
    assert sh["sessions"] == 0 and sh["total"] == 0


def test_the_weekly_line_says_how_many_were_excluded(con):
    from agent.loops.weekly import _shadow_md
    _day_1015(con)
    text = _shadow_md(ledger.shadow_summary(con, since=D1015))
    assert "total rejected and graded: 5" in text
    assert "2 further rejection(s) were traded anyway through a test lane" in text


# --------------------------------------------------------------------- trader's log

def test_the_10_15_log_lists_the_lane_trades_and_has_zero_forgone_r(con):
    _day_1015(con)
    slots = traders_log.build_facts(con, D1015)["slots"]
    assert slots["shadow.scored"]["value"] == "5" and slots["shadow.forgone_r"]["value"] == "+0.00R"
    assert "neither saved nor cost" in slots["shadow.forgone_r"]["label"]
    assert slots["shadow.traded_via_lane"]["value"] == "INTC (coverage) -0.51R taken; CMC (coverage) -0.39R taken"


def test_the_10_16_log_has_no_scored_rejection_and_lists_amat_as_traded(con):
    md = _MD({"AMAT": _real("AMAT", D1016)})
    _graded_day(con, D1016, [AMAT], [AMAT], md)
    slots = traders_log.build_facts(con, D1016)["slots"]
    assert "shadow.scored" not in slots and "shadow.forgone_r" not in slots
    assert slots["shadow.traded_via_lane"]["value"] == "AMAT (coverage) +0.06R taken"


def _draft(happened):
    return (f"{traders_log.H_CANT_PROVE}\nStill short of data.\n\n{traders_log.H_HAPPENED}\n{happened}\n\n"
            f"{traders_log.H_BELIEVE}\nThe tape felt heavy.\n\nTomorrow: no change")


def _facts(forgone):
    return {"slots": {"shadow.forgone_r": {"name": "shadow.forgone_r", "value": forgone,
                                           "scope": "one_session", "source": "s", "label": "l"}},
            "menu": {}}


@pytest.mark.parametrize("sentence,forgone,ok", [
    ("The filters saved the desk {{shadow.forgone_r}} today.", "-0.90R", True),
    ("The filters cost the desk {{shadow.forgone_r}}.", "+1.40R", True),
    ("The filters saved the desk {{shadow.forgone_r}} today.", "+0.00R", False),   # 10-15, 10-16
    ("The filters cost the desk money today.", "+1.40R", False),                     # no figure quoted
    ("Filtering cost me nothing I can see.", "+0.00R", False),
    ("Two rejections were traded anyway, so the filters stopped little.", "+0.00R", True),
])
def test_a_filters_saved_or_cost_claim_needs_a_non_zero_forgone_r(sentence, forgone, ok):
    rendered, violations = traders_log.render(_draft(sentence), _facts(forgone))
    assert (violations == []) is ok, violations
    if not ok:
        assert any("filters saved or cost" in v for v in violations)


def test_the_broker_result_is_the_log_headline_for_live_picks(con):
    md = _MD({"AMAT": _real("AMAT", D1016)})
    _graded_day(con, D1016, [AMAT], [], md)
    pid = ledger.picks_for_date(con, D1016)[0]["pick_id"]
    con.execute("UPDATE outcomes SET broker_status = 'closed', sim_broker_divergence = 'price_gap', "
                "live_r = 0.04 WHERE pick_id = ?", [pid])
    slots = traders_log.build_facts(con, D1016)["slots"]
    assert slots["day.broker_net_r"]["value"] == "+0.04R"
    assert slots["day.divergences"]["value"] == "AMAT: price_gap"
    con.execute("UPDATE outcomes SET sim_broker_divergence = 'none'")
    assert traders_log.build_facts(con, D1016)["slots"]["day.divergences"]["value"] == "none"


def test_a_day_with_no_live_pick_has_no_broker_slots(con):
    md = _MD({"AMAT": _real("AMAT", D1016)})
    _graded_day(con, D1016, [AMAT], [], md)
    slots = traders_log.build_facts(con, D1016)["slots"]
    assert "day.broker_net_r" not in slots and "day.divergences" not in slots


# ------------------------------------------------------------------- chart replay

def _chart_shadow(con, md, d, ticker):
    from agent import chart_payload
    rid = con.execute("SELECT rejection_id FROM rejections WHERE ticker = ?", [ticker]).fetchone()[0]
    out, warnings, guards = {}, [], []
    chart_payload._build_shadow(con, Config(raw={"slippage_bps": 5}), md, out, ticker, d, rid,
                                md.minute_bars(ticker, d), 5, False, warnings, guards)
    return out, guards


def test_the_chart_replays_a_lane_traded_rejection_against_its_taken_r(con):
    _, md = _day_1015(con)
    out, guards = _chart_shadow(con, md, D1015, "INTC")
    assert guards[-1]["name"] == "replay_vs_taken" and guards[-1]["ok"], guards
    assert out["context"]["outcome"]["eod_outcome"] == "traded_via_lane"


def test_the_chart_replay_of_a_rejection_uses_the_analysts_time_stop(con):
    md = _MD({"INTC": _real("INTC", D1015)})
    _graded_day(con, D1015, [], [INTC], md)                   # rejected, never traded
    out, guards = _chart_shadow(con, md, D1015, "INTC")
    assert guards[-1]["name"] == "replay_vs_ledger" and guards[-1]["ok"], guards
