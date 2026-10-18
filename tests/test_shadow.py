"""
D4 (docs/FIX_PHASE0.md): the shadow ledger measures only what the filters
actually stopped.

2026-10-15's review said "-1.41R forgone: the filters saved money". That was
INTC (-1.01R in the shadow) and CMC (-0.39R), and the desk TRADED BOTH anyway,
as coverage picks. 2026-10-16's said "+0.62R forgone: the filters cost the
desk"; that was AMAT, also traded by coverage, at +0.06R as taken. Two defects:

  1. the shadow priced every rejection as if it held to the close (the
     rejections table has no time stop), and in r_multiple rather than the
     planned-risk R a taken pick is reported in -- INTC taken exited at its
     14:30 time stop (-0.51R), the shadow rode to its 15:42 stop (-1.01R);
  2. a rejection that the desk traded anyway was still summed into forgone R.

The bars are the real ones for those three trades
(tests/fixtures/shadow_day_bars.json).
"""
from __future__ import annotations

import json
import pathlib
from datetime import date, datetime

import pytest

from agent import grading, ledger
from agent.grading import (TRADED_VIA_LANE, classify_shadow, shadow_pick, shadow_time_stop,
                           simulate_pick)
from agent.loops import evening
from agent.providers import synthetic_minute_bars

BARS = json.loads((pathlib.Path(__file__).parent / "fixtures" / "shadow_day_bars.json")
                  .read_text(encoding="utf-8"))
D1015, D1016 = date(2026, 10, 15), date(2026, 10, 16)


class _MD:
    """MarketData reduced to what grading reads: minute bars by ticker."""

    def __init__(self, bars):
        self.bars = {k.upper(): v for k, v in bars.items()}

    def minute_bars(self, ticker, d):
        return self.bars.get(ticker.upper(), [])

    def daily_bar(self, ticker, d):
        return None


def _real(key, d):
    return [{"t": datetime.combine(d, datetime.strptime(r[0], "%H:%M").time()), "o": r[1],
             "h": r[2], "l": r[3], "c": r[4], "v": r[5]} for r in BARS[f"{key}-{d}"]]


def _flat(d, price=100.0):
    from datetime import timedelta
    t0 = datetime.combine(d, datetime.strptime("09:30", "%H:%M").time())
    return [{"t": t0 + timedelta(minutes=i), "o": price, "h": price + 0.2, "l": price - 0.2,
             "c": price, "v": 10_000} for i in range(390)]


# the three trades, as the morning plan carried them
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


def _rejection(spec, reason="R:R 1.4 below the control"):
    return {"ticker": spec["ticker"], "direction": spec["direction"], "setup_type": spec["setup_type"],
            "reason": reason, "entry_type": spec["entry_type"], "entry_price": spec["entry_price"],
            "stop_price": spec["stop_price"], "target_price": spec["target_price"], "conviction": 3,
            "thesis": "t", "source_stage": "risk_rejected", "rejection_category": "rr_below_control",
            "planned_rr": 1.4, "session_degraded": False, "primary_kill_check": "rr",
            "kill_checks": ["rr"], "entry_not_before": "09:45"}


def _candidate(spec):
    return {"ticker": spec["ticker"], "direction": spec["direction"], "setup_type": spec["setup_type"],
            "entry_plan": {"type": spec["entry_type"], "price": spec["entry_price"]},
            "stop_price": spec["stop_price"], "target_price": spec["target_price"],
            "time_stop": spec["time_stop"]}


def _untriggered(i):
    return dict(ticker=f"UNT{i}", direction="long", setup_type="trend_pullback",
                entry_type="limit", entry_price=95.0, stop_price=93.0, target_price=99.0,
                time_stop="by 11:00")


@pytest.fixture()
def ledger_db(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "shadow.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)


def _day(d, lane_specs, rejected_specs, md, lane="coverage", extra_candidates=()):
    """A graded session exactly as the evening leaves it before the shadow runs:
    plan + analyst candidates, the lane picks, their saved outcomes, the rejections."""
    con = ledger.connect()
    picks = [_lane_pick(s, lane) for s in lane_specs]
    plan = {"no_trade": False, "market_stance": "t", "total_risk_r": 0.25 * len(picks),
            "rejected": [], "notes_for_evening": "", "picks": picks}
    cands = [_candidate(s) for s in list(lane_specs) + list(rejected_specs) + list(extra_candidates)]
    ledger.save_daily_plan(con, d, plan, {"candidates": cands}, "chop")
    ledger.save_rejections(con, d, [_rejection(s) for s in rejected_specs])
    for o in grading.grade_day(md, ledger.picks_for_date(con, d), d, slippage_bps=5):
        ledger.save_outcome(con, o)
    return con


def _rej_rows(con, d):
    return {r[0]: r[1:] for r in con.execute(
        "SELECT ticker, eod_outcome, forgone_r FROM rejections WHERE plan_date = ?", [d]).fetchall()}


# ------------------------------------------------------------ the 10-15 report

def test_10_15_intc_and_cmc_traded_by_coverage_leave_no_forgone_r(ledger_db):
    """The headline. INTC and CMC are rejections the coverage lane traded; with
    five untriggered rejections the day's forgone R is 0.00, and the two traded
    rows are listed on their own line with the R they were TAKEN at."""
    md = _MD({"INTC": _real("INTC", D1015), "CMC": _real("CMC", D1015),
              **{f"UNT{i}": _flat(D1015) for i in range(1, 6)}})
    con = _day(D1015, [INTC, CMC], [INTC, CMC] + [_untriggered(i) for i in range(1, 6)], md)
    shadow = evening._grade_rejections(con, md, D1015, slippage_bps=5)
    rows = _rej_rows(con, D1015)
    con.close()

    assert shadow["net_forgone_r"] == 0.0
    assert shadow["scored"] == 5 and shadow["priced"] == 5          # the five untriggered, priced 0R
    lane = {t["ticker"]: t for t in shadow["traded_via_lane"]}
    assert set(lane) == {"INTC", "CMC"}
    assert lane["INTC"]["taken_r"] == pytest.approx(-0.51, abs=0.006)
    assert lane["CMC"]["taken_r"] == pytest.approx(-0.39, abs=0.006)
    assert lane["INTC"]["lane"] == "coverage" and lane["INTC"]["taken_status"] == "filled"
    # stored: no forgone R on the traded rows, so no total, mean or test can count them
    assert rows["INTC"] == (TRADED_VIA_LANE, None) and rows["CMC"] == (TRADED_VIA_LANE, None)
    assert all(rows[f"UNT{i}"][1] == 0.0 for i in range(1, 6))
    md_txt = evening._shadow_md(shadow)
    assert "Traded via a lane" in md_txt
    assert "INTC (coverage) -0.51R taken" in md_txt and "CMC (coverage) -0.39R taken" in md_txt
    assert "neither saved nor cost" in md_txt


def test_10_16_amat_traded_by_coverage_is_not_a_cost_of_the_filters(ledger_db):
    md = _MD({"AMAT": _real("AMAT", D1016)})
    con = _day(D1016, [AMAT], [AMAT], md)
    shadow = evening._grade_rejections(con, md, D1016, slippage_bps=5)
    txt = evening._shadow_md(shadow)
    from agent import traders_log
    slots = traders_log.build_facts(con, D1016)["slots"]
    con.close()
    assert shadow["net_forgone_r"] == 0.0 and shadow["scored"] == 0
    assert shadow["traded_via_lane"][0]["taken_r"] == pytest.approx(0.06, abs=0.006)
    assert "cost the desk" not in txt and "saved the desk" not in txt
    assert "AMAT (coverage) +0.06R taken" in txt
    assert "stopped nothing today" in txt
    # what the trader's log is written from: no forgone-R slot at all, the lane trade listed
    assert "shadow.forgone_r" not in slots and "shadow.priced" not in slots
    assert slots["shadow.traded_via_lane"]["value"] == "AMAT (coverage) +0.06R taken"


def test_the_lane_line_says_what_became_of_each_taken_pick():
    rows = [{"ticker": "A", "lane": "coverage", "taken_r": -0.51, "taken_status": "filled"},
            {"ticker": "B", "lane": "daily_floor", "taken_r": None, "taken_status": "not_filled"},
            {"ticker": "C", "lane": "hypothesis", "taken_r": None, "taken_status": "invalid_fill"},
            {"ticker": "D", "lane": "coverage", "taken_r": None, "taken_status": "ungraded"}]
    line = evening._lane_line({"traded_via_lane": rows})
    assert "A (coverage) -0.51R taken" in line and "B (daily_floor) did not fill" in line
    assert "C (hypothesis) invalid fill, not a trade" in line and "D (coverage) not graded yet" in line
    assert evening._lane_line({"traded_via_lane": []}) == ""


def test_the_sign_legend_is_printed_only_beside_a_net_that_is_not_zero():
    """The review reads this line. "positive = the filters cost the desk" beside a
    +0.00R is how a zero got quoted as a verdict; it stays beside a real net."""
    base = {"scored": 4, "priced": 4, "triggered": 2, "traded_via_lane": []}
    cost = evening._shadow_md({**base, "net_forgone_r": 1.4})
    saved = evening._shadow_md({**base, "net_forgone_r": -0.9})
    zero = evening._shadow_md({**base, "net_forgone_r": 0.0})
    assert "net forgone +1.40R over the priced, +0.35R per priced rejection" in cost
    assert "net forgone -0.90R over the priced, -0.23R per priced rejection" in saved
    assert "positive = the filters cost the desk" in cost and "positive = the filters cost the desk" in saved
    assert "net forgone +0.00R" in zero and "neither saved nor cost R" in zero
    assert "cost the desk" not in zero and "saved the desk" not in zero
    nothing_priced = evening._shadow_md({"scored": 2, "priced": 0, "triggered": 0,
                                         "net_forgone_r": 0.0, "traded_via_lane": []})
    assert "0 priced" in nothing_priced and "cost the desk" not in nothing_priced
    assert evening._shadow_md({"scored": 0, "priced": 0, "net_forgone_r": 0.0,
                               "traded_via_lane": []}) == "No rejected candidates were scored today."
    only_lane = evening._shadow_md({"scored": 0, "priced": 0, "net_forgone_r": 0.0,
                                    "traded_via_lane": [{"ticker": "X", "lane": "coverage",
                                                         "taken_r": 0.3}]})
    assert "stopped nothing today" in only_lane and "X (coverage) +0.30R taken" in only_lane


# ------------------------------------------------------------- the time stop

def test_a_rejection_nobody_traded_is_priced_with_its_time_stop(ledger_db):
    """INTC's levels as a pure shadow: the 14:30 time stop comes before the
    15:42 stop touch, so the shadow exits at the time stop (-0.51R), not at the
    stop (-1.0R)."""
    md = _MD({"INTC": _real("INTC", D1015)})
    con = _day(D1015, [], [INTC], md)                       # rejected, never traded
    shadow = evening._grade_rejections(con, md, D1015, slippage_bps=5)
    outcome, forgone = con.execute("SELECT eod_outcome, forgone_r FROM rejections").fetchone()
    con.close()
    assert outcome == "would_have_lost"
    assert forgone == pytest.approx(-0.513, abs=0.003)
    # the day's line: one priced rejection, forgone -0.51R (negative = the filters saved it)
    assert (shadow["scored"], shadow["priced"], shadow["triggered"]) == (1, 1, 1)
    assert shadow["net_forgone_r"] == -0.51 and shadow["traded_via_lane"] == []
    assert "net forgone -0.51R over the priced" in evening._shadow_md(shadow)

    sim_stop = simulate_pick(shadow_pick({"rejection_id": 1, **_rejection(INTC)}), md.bars["INTC"],
                             None, slippage_bps=5, session_date=D1015)
    assert sim_stop["exit_reason"] == "stop"                # no time stop: the old pricing
    assert grading.realized_r(sim_stop) == pytest.approx(-1.02, abs=0.01)
    sim_ts = simulate_pick(shadow_pick({"rejection_id": 1, **_rejection(INTC)}, time_stop="by 14:30"),
                           md.bars["INTC"], None, slippage_bps=5, session_date=D1015)
    assert sim_ts["exit_reason"] == "time_stop"


def test_forgone_r_is_the_planned_risk_r_a_taken_pick_is_reported_in():
    out = {"triggered": True, "exit_reason": "stop", "r_planned": -0.51, "r_multiple": -1.04}
    assert classify_shadow(out) == ("would_have_lost", -0.51)
    legacy = {"triggered": True, "exit_reason": "stop", "r_multiple": -1.04}
    assert classify_shadow(legacy) == ("would_have_lost", -1.04)      # a row with no r_planned
    assert classify_shadow({"triggered": False, "exit_reason": "no_trigger"}) == ("scratch", 0.0)
    assert classify_shadow({"exit_reason": "invalid_fill"}) == ("unmeasurable", None)


def test_the_analysts_time_stop_is_recovered_from_the_stored_candidates():
    stored = json.dumps({"candidates": [
        {"ticker": "INTC", "direction": "short", "entry_plan": {"price": 92.0}, "time_stop": "by 14:30"},
        {"ticker": "INTC", "direction": "long", "entry_plan": {"price": 88.0}, "time_stop": "by 11:00"},
        {"ticker": "CMC", "direction": "long", "entry_price": 66.5, "time_stop": None}]})
    assert shadow_time_stop(stored, "intc", "short", 92.0) == "by 14:30"
    assert shadow_time_stop(stored, "INTC", "long", 88.0) == "by 11:00"
    # the side decides even when no entry price is given, or none matches
    assert shadow_time_stop(stored, "INTC", "long") == "by 11:00"
    assert shadow_time_stop(stored, "INTC", "long", 90.0) == "by 11:00"
    assert shadow_time_stop(stored, "INTC", "short", 90.0) == "by 14:30"
    assert shadow_time_stop(stored, "CMC", "long", 66.5) is None          # none written
    assert shadow_time_stop(stored, "NVDA", "long", 1.0) is None          # not a candidate
    assert shadow_time_stop("not json", "INTC", "short") is None
    assert shadow_time_stop(None, "INTC", "short") is None
    # two candidates on one ticker and side: the one at the rejection's entry price wins
    twin = json.dumps({"candidates": [
        {"ticker": "CMC", "direction": "long", "entry_plan": {"price": 66.5}, "time_stop": "by 14:00"},
        {"ticker": "CMC", "direction": "long", "entry_price": 64.0, "time_stop": "by 11:30"}]})
    assert shadow_time_stop(twin, "CMC", "long", 64.0) == "by 11:30"
    assert shadow_time_stop(twin, "CMC", "long", 66.5) == "by 14:00"
    assert shadow_time_stop(twin, "CMC", "long") == "by 14:00"            # no entry given: the first


def test_a_rejection_with_no_stored_candidates_is_priced_as_before(ledger_db):
    """Sessions whose candidates were never stored (or have no time stop) keep the
    old, no-time-stop pricing rather than failing."""
    md = _MD({"INTC": _real("INTC", D1015)})
    con = ledger.connect()
    ledger.save_daily_plan(con, D1015, {"no_trade": True, "picks": []}, {}, "chop")
    ledger.save_rejections(con, D1015, [_rejection(INTC)])
    evening._grade_rejections(con, md, D1015, slippage_bps=5)
    outcome, forgone = con.execute("SELECT eod_outcome, forgone_r FROM rejections").fetchone()
    con.close()
    assert outcome == "would_have_lost" and forgone == pytest.approx(-1.02, abs=0.01)


# --------------------------------------------------- shadow R == taken R, always

def _synthetic_day(d, specs):
    return _MD({s["ticker"]: synthetic_minute_bars(s["ticker"], d) for s in specs})


def test_a_pick_both_rejected_and_taken_has_the_same_r_in_the_shadow_as_taken(ledger_db):
    """The invariant, over a seeded day of eight lane trades of every entry type
    on deterministic synthetic tapes (one reaches its target, one its stop, some
    never trigger): for every pick that was both rejected and taken, the shadow's
    R is the taken R, exactly."""
    d = date(2026, 10, 14)
    base = dict(setup_type="gap_go")
    specs = []
    for i, (etype, direction) in enumerate([
            ("open", "long"), ("limit", "long"), ("breakout", "long"), ("limit", "short"),
            ("breakout", "short"), ("open", "short"), ("limit", "long"), ("breakout", "long")]):
        up = direction == "long"
        entry = 100.0 if etype == "open" else (99.5 if (etype == "limit") == up else 101.0)
        specs.append({**base, "ticker": f"SYN{i}", "direction": direction, "entry_type": etype,
                      "entry_price": entry, "stop_price": entry - 2.0 if up else entry + 2.0,
                      "target_price": entry + 3.0 if up else entry - 3.0,
                      "time_stop": ["by 10:30", "by 12:00", None, "by 14:00"][i % 4]})
    md = _synthetic_day(d, specs)
    con = _day(d, specs, specs, md, lane="daily_floor")
    shadow = evening._grade_rejections(con, md, d, slippage_bps=5)
    taken = {r[0]: r[1] for r in con.execute(
        "SELECT p.ticker, o.r_planned FROM picks p JOIN outcomes o USING (pick_id)").fetchall()}
    triggered = [t for t in taken.values() if t is not None]
    con.close()
    assert len(shadow["traded_via_lane"]) == 8 and shadow["scored"] == 0
    assert len(triggered) >= 3, "the seeded day must contain real fills for this to mean anything"
    for t in shadow["traded_via_lane"]:
        assert t["shadow_r"] == taken[t["ticker"]], t          # exactly equal, filled or not
        assert t["taken_status"] == ("filled" if taken[t["ticker"]] is not None else "not_filled")
        assert t["taken_r"] == (None if taken[t["ticker"]] is None else
                                pytest.approx(taken[t["ticker"]]))


def test_a_traded_rejection_is_priced_with_the_picks_own_levels(ledger_db):
    """The validator can move a lane pick's levels off the analyst's candidate
    (level grounding). Only the pick knows where it really went, so the shadow
    of a traded rejection uses the pick's parameters, not the rejection row's."""
    d = date(2026, 10, 14)
    spec = {"setup_type": "gap_go", "ticker": "SYN1", "direction": "long", "entry_type": "limit",
            "entry_price": 99.5, "stop_price": 97.5, "target_price": 102.5, "time_stop": "by 12:00"}
    md = _synthetic_day(d, [spec])
    con = _day(d, [spec], [spec], md, lane="coverage")
    # the rejection row carries a stale, different entry; the pick is what traded
    con.execute("UPDATE rejections SET entry_price = 90.0, stop_price = 88.0, target_price = 93.0")
    shadow = evening._grade_rejections(con, md, d, slippage_bps=5)
    con.close()
    t = shadow["traded_via_lane"][0]
    assert t["shadow_r"] == t["taken_r"]


def test_a_shadow_trade_waits_for_the_same_entry_window_a_taken_pick_does():
    """09:30-09:45 is forbidden to a limit or breakout entry (v27). A spike through
    the trigger at 09:35 that never comes back is a fill for a shadow that ignores
    the window and no fill for the taken pick it is compared with."""
    d = date(2026, 10, 14)
    bars = _flat(d, 100.0)
    bars[5] = {**bars[5], "h": 101.6}                       # 09:35, inside the window
    rej = {**_rejection(INTC), "rejection_id": 7, "direction": "long", "entry_type": "breakout",
           "entry_price": 101.0, "stop_price": 99.0, "target_price": 104.0,
           "entry_not_before": "09:45"}
    shim = shadow_pick(rej)
    assert shim["entry_not_before"] == "09:45"
    windowed = simulate_pick(shim, bars, None, slippage_bps=5, session_date=d)
    assert windowed["triggered"] is False
    unwindowed = simulate_pick({**shim, "entry_not_before": None}, bars, None, slippage_bps=5,
                               session_date=d)
    assert unwindowed["triggered"] is True                  # the window is what decides it


def test_a_rejection_in_the_other_direction_was_not_traded_via_a_lane(ledger_db):
    md = _MD({"INTC": _real("INTC", D1015)})
    longside = {**INTC, "direction": "long", "entry_type": "limit", "entry_price": 91.0,
                "stop_price": 89.0, "target_price": 95.0}
    con = _day(D1015, [INTC], [longside], md)               # INTC short traded; INTC long rejected
    shadow = evening._grade_rejections(con, md, D1015, slippage_bps=5)
    con.close()
    assert shadow["traded_via_lane"] == [] and shadow["scored"] == 1


def test_regrading_history_is_idempotent_and_reprices_a_traded_row(ledger_db):
    """regrade-shadow resets a session's rejections and re-runs the grader."""
    md = _MD({"INTC": _real("INTC", D1015), "CMC": _real("CMC", D1015)})
    con = _day(D1015, [INTC, CMC], [INTC, CMC], md)
    first = evening._grade_rejections(con, md, D1015, slippage_bps=5)
    con.execute("UPDATE rejections SET eod_outcome = NULL, forgone_r = NULL, graded_at = NULL, "
                "sim_version = NULL WHERE plan_date = ?", [D1015])
    second = evening._grade_rejections(con, md, D1015, slippage_bps=5)
    con.close()
    assert first["traded_via_lane"] == second["traded_via_lane"]
    assert second["net_forgone_r"] == 0.0
