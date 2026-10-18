"""
D2 + D3 (docs/FIX_PHASE0.md): the simulator fills only what a broker would, and
the review shows the broker.

Part 1 replays the paper account's own entry orders. tests/fixtures/
limit_fills_broker.json holds every limit and stop ENTRY the account placed at
the 09:45 entry window from 2026-10-02 to 2026-10-16 (28 orders: 14 filled, 14
never filled), with the levels, the broker's actual outcome and the real
1-minute bars. The simulator must enter exactly when the broker did.

Part 2 is D3: what the paper account did is stored next to the simulated
outcome, classified (none / sim_only_fill / broker_only_fill / price_gap), and
the review leads with it. RKLB's 2026-10-15 numbers are the originals: the
evening that day ran with the simulator's +0.21R "winner" for an order Alpaca
cancelled unfilled.
"""
from __future__ import annotations

import json
import pathlib
from datetime import date, datetime

import pytest

from agent import grading, ledger, live_broker, live_monitor
from agent.config import Config
from agent.grading import (broker_summary_line, classify_divergence, outcomes_md,
                           realized_r, simulate_pick)
from agent.loops import evening

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "limit_fills_broker.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def _bars(case):
    d = date.fromisoformat(case["date"])
    return d, [{"t": datetime.combine(d, datetime.strptime(b[0], "%H:%M").time()),
                "o": b[1], "h": b[2], "l": b[3], "c": b[4], "v": b[5]} for b in case["bars"]]


def _simulate(case):
    d, bars = _bars(case)
    pick = {"pick_id": 1, "direction": case["direction"], "entry_type": case["entry_type"],
            "entry_price": case["entry"], "stop_price": case["stop"],
            "target_price": case["target"], "time_stop": case["time_stop"],
            "entry_not_before": case["entry_not_before"]}
    return simulate_pick(pick, bars, None, slippage_bps=5, session_date=d, session_bars=bars)


# ------------------------------------------------- part 1: entries vs the broker

def test_the_fixture_is_the_brokers_whole_post_1002_entry_record():
    filled = [c for c in CASES if c["broker"]["filled"]]
    assert len(CASES) == 28 and len(filled) == 14
    assert "RKLB-2026-10-15" in {c["id"] for c in CASES if not c["broker"]["filled"]}


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_the_simulator_enters_exactly_when_the_broker_did(case):
    out = _simulate(case)
    assert bool(out["triggered"]) is case["broker"]["filled"], (
        f"{case['id']}: broker {case['broker']['status']}, simulator "
        f"triggered={out['triggered']} ({out['exit_reason']})")
    if case["broker"]["filled"]:
        broker_at = datetime.strptime(case["broker"]["filled_at"], "%H:%M:%S")
        sim_at = out["fill_time"]
        gap = abs((sim_at.hour * 60 + sim_at.minute) - (broker_at.hour * 60 + broker_at.minute))
        assert gap <= 1, f"{case['id']}: fills {gap} minutes apart"


def test_the_touch_rule_invented_exactly_one_fill_and_it_was_rklb(monkeypatch):
    """The v7 rule, reproduced by zeroing the cushion, disagrees with the broker
    on exactly one of the 28 entries: RKLB 2026-10-15 (+0.21R at the 14:00 time
    stop, the day's only winner, a sell limit Alpaca cancelled unfilled)."""
    monkeypatch.setattr(grading, "LIMIT_FILL_CUSHION_BPS", 0.0)
    monkeypatch.setattr(grading, "LIMIT_FILL_TICK", 0.0)
    wrong = {c["id"]: _simulate(c) for c in CASES
             if bool(_simulate(c)["triggered"]) != c["broker"]["filled"]}
    assert list(wrong) == ["RKLB-2026-10-15"]
    assert realized_r(wrong["RKLB-2026-10-15"]) == pytest.approx(0.207, abs=0.01)
    monkeypatch.undo()
    assert not [c["id"] for c in CASES if bool(_simulate(c)["triggered"]) != c["broker"]["filled"]]


# ------------------------------------------------------ part 2: sim vs broker

# The 2026-10-15 evening, from its two sources. RKLB: the simulator's original
# v7 numbers for a sell limit the broker cancelled (53.973 -> 53.457 at the
# 14:00 time stop). CRWV: simulated 63.968 -> 68.034 stop; the broker filled
# 64.091614 and its stop leg filled at 68.073226.
RKLB_V7 = {"pick_id": 1, "triggered": True, "entry_fill": 53.973, "exit_price": 53.457,
           "exit_reason": "time_stop", "r_planned": 0.207, "r_multiple": 0.204, "mfe_r": 0.4,
           "mae_r": -0.1, "fill_time": datetime(2026, 10, 15, 11, 15),
           "exit_time": datetime(2026, 10, 15, 14, 0)}
CRWV_SIM = {"pick_id": 2, "triggered": True, "entry_fill": 63.968, "exit_price": 68.034,
            "exit_reason": "stop", "r_planned": -1.016, "r_multiple": -1.0, "mfe_r": 0.2,
            "mae_r": -1.0, "fill_time": datetime(2026, 10, 15, 11, 9),
            "exit_time": datetime(2026, 10, 15, 13, 41)}
DAY = date(2026, 10, 15)


@pytest.mark.parametrize("sim,broker,expect", [
    # the simulator filled, the broker never did
    ({"triggered": True, "exit_reason": "time_stop", "r_planned": 0.207},
     {"entry_fill": None, "live_r": None, "status": "canceled"}, "sim_only_fill"),
    # the broker filled, the simulator did not trigger
    ({"triggered": False, "exit_reason": "no_trigger"},
     {"entry_fill": 12.98, "live_r": -1.2, "status": "closed"}, "broker_only_fill"),
    # ... or called the fill invalid (INKT 2026-09-30: a gap-through fill under its own stop)
    ({"triggered": False, "exit_reason": "invalid_fill"},
     {"entry_fill": 12.98, "live_r": -1.2, "status": "closed"}, "broker_only_fill"),
    # both filled, R within friction (CRWV 2026-10-15: -1.016 simulated, -0.995 at the broker)
    ({"triggered": True, "exit_reason": "stop", "r_planned": -1.016},
     {"entry_fill": 64.091614, "live_r": -0.9954, "status": "closed"}, "none"),
    # both filled, different exit: S 2026-10-13 (-0.29 simulated, +0.14 at the broker)
    ({"triggered": True, "exit_reason": "time_stop", "r_planned": -0.29},
     {"entry_fill": 25.256, "live_r": 0.14, "status": "closed"}, "price_gap"),
    # neither filled
    ({"triggered": False, "exit_reason": "no_trigger"},
     {"entry_fill": None, "live_r": None, "status": "canceled"}, "none"),
])
def test_divergence_classes(sim, broker, expect):
    assert classify_divergence(sim, broker) == expect


def test_a_pick_that_never_went_to_the_broker_has_no_divergence_to_report():
    assert classify_divergence({"triggered": True, "r_planned": 0.5}, None) is None


def test_with_no_exit_yet_the_entries_are_compared_in_planned_risk_units():
    pick = {"entry_price": 64.0, "stop_price": 68.0}
    sim = {"triggered": True, "entry_fill": 63.968, "exit_reason": "eod", "r_planned": None}
    near = {"entry_fill": 64.05, "live_r": None, "status": "filled"}
    far = {"entry_fill": 64.60, "live_r": None, "status": "filled"}     # 0.63 / 4.0 = 0.16R away
    assert classify_divergence(sim, near, pick) == "none"
    assert classify_divergence(sim, far, pick) == "price_gap"


def _seed_session():
    """The 2026-10-15 evening's RKLB and CRWV: picks, the simulator's outcomes, and
    the broker's live orders (RKLB cancelled by the 14:00 time stop; CRWV filled
    and then stopped out by its own stop leg)."""
    con = ledger.connect()
    picks_in = [
        {"ticker": "RKLB", "direction": "short", "setup_type": "breakdown_short", "size_r": 0.25,
         "entry_plan": {"type": "limit"}, "entry_type": "limit", "entry_price": 54.0,
         "stop_price": 56.5, "target_price": 48.0, "time_stop": "by 14:00",
         "exploration_lane": "coverage", "entry_not_before": "09:45"},
        {"ticker": "CRWV", "direction": "short", "setup_type": "gap_fade", "size_r": 0.25,
         "entry_plan": {"type": "limit"}, "entry_type": "limit", "entry_price": 64.0,
         "stop_price": 68.0, "target_price": 55.0, "time_stop": "by 15:30",
         "exploration_lane": "coverage", "entry_not_before": "09:45"}]
    ids = ledger.save_daily_plan(con, DAY, {"no_trade": False, "picks": picks_in}, {}, "chop")
    rklb_id, crwv_id = ids
    ledger.save_outcome(con, {**RKLB_V7, "pick_id": rklb_id, "eval_date": DAY})
    ledger.save_outcome(con, {**CRWV_SIM, "pick_id": crwv_id, "eval_date": DAY})
    ledger.save_live_order(con, rklb_id, {"id": "rklb-live", "status": "canceled",
                                          "order_class": "bracket"},
                           DAY, "RKLB", "sell", 50, "limit")
    ledger.update_live_order(con, "rklb-live", exit_reason="cancelled_unfilled",
                             reconciled=True, status="canceled")
    ledger.save_live_order(con, crwv_id, {"id": "crwv-live", "status": "filled",
                                          "order_class": "bracket"},
                           DAY, "CRWV", "sell", 31, "limit")
    con.close()
    return rklb_id, crwv_id


def _evening_view(monkeypatch, tmp_path):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "reconcile.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    rklb_id, crwv_id = _seed_session()
    con = ledger.connect()
    picks = ledger.picks_for_date(con, DAY)
    outcomes = [{**RKLB_V7, "pick_id": rklb_id}, {**CRWV_SIM, "pick_id": crwv_id}]
    return con, picks, outcomes, rklb_id, crwv_id


def _cfg():
    return Config(raw={"live_paper": {"enabled": True, "alpaca_paper": True,
                                      "trading_base": live_broker.PAPER_BASE}},
                  alpaca_key_id="key", alpaca_secret_key="secret")


def test_crwv_live_r_is_computed_from_the_brokers_prices(monkeypatch, tmp_path):
    """The monitor reads the broker's fills (entry 64.091614, stop leg 68.073226)
    and books R from THOSE prices over the planned risk of 4.00, not from the
    simulator's levels."""
    con, picks, outcomes, rklb_id, crwv_id = _evening_view(monkeypatch, tmp_path)
    con.close()
    monkeypatch.setattr(live_broker, "get_order", lambda oid, **k: {
        "status": "filled", "filled_avg_price": "64.091614", "filled_at": "2026-10-15T15:09:28Z",
        "legs": [{"id": "tp", "type": "limit", "status": "canceled"},
                 {"id": "sl", "type": "stop", "status": "filled",
                  "filled_avg_price": "68.073226", "filled_at": "2026-10-15T18:42:43Z"}]})
    result = live_monitor.check(_cfg(), run_date=DAY, at=datetime(2026, 10, 15, 14, 45))
    con = ledger.connect()
    row = [r for r in ledger.live_orders_for_date(con, DAY) if r["live_order_id"] == "crwv-live"][0]
    con.close()
    assert result["updated"] >= 1
    assert row["entry_fill_price"] == 64.091614 and row["exit_fill_price"] == 68.073226
    assert row["exit_reason"] == "stop"
    assert row["live_r"] == pytest.approx((64.091614 - 68.073226) / 4.0)       # -0.9954R, short


def test_the_broker_result_is_stored_next_to_the_simulated_outcome(monkeypatch, tmp_path):
    con, picks, outcomes, rklb_id, crwv_id = _evening_view(monkeypatch, tmp_path)
    ledger.update_live_order(con, "crwv-live", entry_fill_price=64.091614,
                             exit_fill_price=68.073226, exit_reason="stop", live_r=-0.9954,
                             reconciled=True, status="closed")
    rows = evening._record_broker_truth(con, DAY, picks, outcomes)
    stored = {r[0]: r for r in con.execute(
        """SELECT pick_id, broker_entry_fill, broker_exit_fill, broker_exit_reason, broker_status,
                  sim_broker_divergence, live_r, r_planned, entry_fill FROM outcomes""").fetchall()}
    con.close()
    assert {r["ticker"]: r["divergence"] for r in rows} == {"RKLB": "sim_only_fill", "CRWV": "none"}
    rk, cr = stored[rklb_id], stored[crwv_id]
    assert (rk[1], rk[3], rk[4], rk[5]) == (None, "cancelled_unfilled", "canceled", "sim_only_fill")
    assert (cr[1], cr[2], cr[3], cr[5]) == (64.091614, 68.073226, "stop", "none")
    assert cr[6] == pytest.approx(-0.9954)
    # the research number is untouched: the simulator's R and fill are still the simulator's
    assert rk[7] == pytest.approx(0.207) and rk[8] == pytest.approx(53.973)
    assert cr[7] == pytest.approx(-1.016)


def test_recording_the_broker_result_twice_changes_nothing(monkeypatch, tmp_path):
    con, picks, outcomes, *_ = _evening_view(monkeypatch, tmp_path)
    first = evening._record_broker_truth(con, DAY, picks, outcomes)
    second = evening._record_broker_truth(con, DAY, picks, outcomes)
    con.close()
    assert first == second


def test_the_review_leads_with_the_broker_and_flags_the_divergence(monkeypatch, tmp_path):
    """RKLB: the broker cancelled, the simulator booked +0.21R. The review must
    say the broker never filled and label the simulator's number as research."""
    con, picks, outcomes, rklb_id, crwv_id = _evening_view(monkeypatch, tmp_path)
    ledger.update_live_order(con, "crwv-live", entry_fill_price=64.091614,
                             exit_fill_price=68.073226, exit_reason="stop", live_r=-0.9954,
                             reconciled=True, status="closed")
    evening._record_broker_truth(con, DAY, picks, outcomes)
    con.close()
    md = outcomes_md(picks, outcomes)
    rklb = md.split("### CRWV")[0]
    crwv = md.split("### CRWV")[1]
    assert "BROKER (the headline" in rklb and "NEVER FILLED" in rklb and "cancelled unfilled" in rklb
    assert "SIM/BROKER DIVERGENCE (sim_only_fill)" in rklb
    assert "SIMULATED (research number, not the headline): filled @ 53.973" in rklb
    assert "OUTCOME: filled" not in rklb                       # the simulated fill is not "the outcome"
    assert "filled @ 64.091614, exit @ 68.073226 (stop), R = -1.00" in crwv
    assert "DIVERGENCE" not in crwv
    one_line = broker_summary_line(picks, outcomes)
    assert "2 pick(s) went to the broker, 1 filled" in one_line
    assert "Broker result -1.00R over 1 settled" in one_line
    assert "RKLB sim_only_fill" in one_line


def test_a_pick_that_never_went_to_the_broker_is_reviewed_exactly_as_before():
    pick = {"pick_id": 7, "ticker": "XYZ", "direction": "long", "setup_type": "gap_go",
            "entry_type": "open", "entry_price": 10.0, "stop_price": 9.0, "target_price": 12.0,
            "size_r": 1.0, "conviction": 3, "time_stop": "by 11:00", "thesis": "t",
            "invalidation": "i"}
    out = {"pick_id": 7, "triggered": True, "entry_fill": 10.0, "exit_price": 11.0,
           "exit_reason": "target", "r_planned": 1.0, "r_multiple": 1.0, "mfe_r": 1.2, "mae_r": -0.1}
    md = outcomes_md([pick], [out])
    assert "- OUTCOME: filled @ 10.0" in md and "BROKER" not in md and "SIMULATED" not in md
    assert broker_summary_line([pick], [out]) == ""


# ---------------------------------------- the evening run, end to end (offline data)

RUN_DAY = date(2026, 10, 15)


@pytest.fixture()
def evening_env(tmp_path, monkeypatch):
    """A real (non-mock) evening against offline synthetic bars, a stub grader
    that records exactly what it was shown, and a captured email. Every broker
    call is faked: this machine's config carries live Alpaca credentials."""
    from agent.config import load_config
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "evening.duckdb")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    shown: dict = {}
    sent: list = []

    class _Grader:
        def __init__(self, cfg):
            pass

        def call(self, **kw):
            shown["user"] = kw.get("user", "")

            class R:
                parsed = {"plan_grade": "B", "plan_notes": "ok", "pick_reviews": [],
                          "lessons": [], "lesson_updates": []}
                usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0, "cost_usd": 0.0}
                text = ""
            return R()

    monkeypatch.setattr(evening, "LLMClient", _Grader)
    monkeypatch.setattr(evening, "send_email", lambda cfg, subject, html: sent.append(html))
    monkeypatch.setattr(evening, "render_dashboard", lambda *a, **k: None)
    monkeypatch.setattr(live_broker, "get_order", lambda *a, **k: None)
    monkeypatch.setattr(live_broker, "cancel_order", lambda *a, **k: True)
    monkeypatch.setattr(live_broker, "list_positions", lambda **k: [])
    cfg = load_config()
    cfg.mock = False
    cfg.offline = True
    con = ledger.connect()
    plan = {"no_trade": False, "market_stance": "t", "total_risk_r": 0.25, "rejected": [],
            "notes_for_evening": "", "picks": [{
                "ticker": "TEST", "direction": "long", "setup_type": "gap_go", "catalyst": "x",
                "thesis_final": "x", "conviction": 3, "size_r": 0.25,
                "entry_plan": {"type": "open", "price": 100.0}, "entry_price": 100.0,
                "stop_price": 98.0, "target_price": 104.0, "target2_price": None,
                "time_stop": None, "invalidation": "x"}]}
    pick_id = ledger.save_daily_plan(con, RUN_DAY, plan, {"candidates": []}, "chop")[0]
    # The simulator fills an `open` entry on the first bar. The broker's order was
    # cancelled unfilled -- the RKLB shape.
    ledger.save_live_order(con, pick_id, {"id": "t-1", "status": "canceled",
                                          "order_class": "bracket"},
                           RUN_DAY, "TEST", "buy", 100, "open")
    ledger.update_live_order(con, "t-1", exit_reason="cancelled_unfilled", reconciled=True,
                             status="canceled")
    con.close()
    return cfg, shown, sent, pick_id


def test_the_evening_shows_the_grader_and_the_email_the_broker_first(evening_env):
    import json as _json
    cfg, shown, sent, pick_id = evening_env
    evening.run(cfg, run_date=RUN_DAY, force=True)

    assert "BROKER (the headline" in shown["user"] and "NEVER FILLED" in shown["user"]
    assert "SIM/BROKER DIVERGENCE (sim_only_fill)" in shown["user"]
    assert "SIMULATED (research number, not the headline)" in shown["user"]
    assert any("LIVE vs SIMULATED" in html and "sim_only_fill" in html for html in sent)
    con = ledger.connect()
    div, status, triggered = con.execute(
        "SELECT sim_broker_divergence, broker_status, triggered FROM outcomes WHERE pick_id = ?",
        [pick_id]).fetchone()
    stages = _json.loads(con.execute(
        "SELECT stages FROM runs WHERE loop = 'evening' ORDER BY run_id DESC LIMIT 1").fetchone()[0])
    con.close()
    assert (div, status) == ("sim_only_fill", "canceled") and triggered is True
    assert stages["broker_divergences"] == 1


def test_an_unreadable_broker_leaves_the_evening_on_simulated_outcomes(evening_env, monkeypatch):
    cfg, shown, sent, pick_id = evening_env

    def boom(*a, **k):
        raise RuntimeError("broker down")

    monkeypatch.setattr(live_monitor, "reconcile_evening", boom)
    evening.run(cfg, run_date=RUN_DAY, force=True)                  # must still complete
    assert "BROKER (the headline" not in shown["user"] and "- OUTCOME: filled" in shown["user"]
    con = ledger.connect()
    status = con.execute("SELECT status FROM runs WHERE loop = 'evening' "
                         "ORDER BY run_id DESC LIMIT 1").fetchone()[0]
    div = con.execute("SELECT sim_broker_divergence FROM outcomes WHERE pick_id = ?",
                      [pick_id]).fetchone()[0]
    con.close()
    assert status in ("ok", "degraded") and div is None


def test_touch_only_bars_is_persisted_and_survives_the_broker_update(monkeypatch, tmp_path):
    """Untriggered touch-only picks must be countable from the ledger, and the
    evening's broker write must not disturb the simulated columns."""
    con, picks, outcomes, rklb_id, crwv_id = _evening_view(monkeypatch, tmp_path)
    ledger.save_outcome(con, {"pick_id": rklb_id, "eval_date": DAY, "triggered": False,
                              "exit_reason": "no_trigger", "touch_only_bars": 2,
                              "r_if_held_to_flatten": None})
    stored = con.execute("SELECT touch_only_bars, triggered FROM outcomes WHERE pick_id = ?",
                         [rklb_id]).fetchone()
    assert stored == (2, False)
    evening._record_broker_truth(con, DAY, picks, [{"pick_id": rklb_id, "triggered": False,
                                                    "exit_reason": "no_trigger"}])
    after = con.execute("SELECT touch_only_bars, sim_broker_divergence, broker_status "
                        "FROM outcomes WHERE pick_id = ?", [rklb_id]).fetchone()
    count = con.execute("SELECT COUNT(*) FROM outcomes WHERE NOT triggered "
                        "AND touch_only_bars >= 1").fetchone()[0]
    con.close()
    assert after == (2, "none", "canceled")        # neither filled: they agree
    assert count == 1
