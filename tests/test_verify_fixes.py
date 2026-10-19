"""
agent/fix_verification.py and its command line scripts/verify_fixes.py: the per-session
checks the evening self-check runs on production, and the one-off --impact report
(docs/FIX_VERIFICATION.md).

Every check is exercised both ways on a seeded ledger opened READ-ONLY, the way the script
opens production. Bars are the real ones for the incident sessions
(tests/fixtures/shadow_day_bars.json).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import sys
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agent import grading, ledger, research
from agent.config import Config

from agent import fix_verification as vf          # the checks (also run by the evening self-check)

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("verify_fixes_cli", ROOT / "scripts" / "verify_fixes.py")
cli = importlib.util.module_from_spec(_spec)       # the read-only command line
_spec.loader.exec_module(cli)

ET = ZoneInfo("America/New_York")
BARS = json.loads((ROOT / "tests" / "fixtures" / "shadow_day_bars.json").read_text(encoding="utf-8"))
D1015, D1016 = date(2026, 10, 15), date(2026, 10, 16)
CFG = Config(raw={"slippage_bps": 5, "live_paper": {"allow_overnight_symbols": []}})


def _real(key, d):
    return [{"t": datetime.combine(d, datetime.strptime(r[0], "%H:%M").time()), "o": r[1],
             "h": r[2], "l": r[3], "c": r[4], "v": r[5]} for r in BARS[f"{key}-{d}"]]


class _MD:
    def __init__(self, bars):
        self.bars = {k.upper(): v for k, v in bars.items()}

    def minute_bars(self, ticker, d):
        return self.bars.get(ticker.upper(), [])

    def daily_bar(self, ticker, d):
        return None

    def prefetch_session(self, tickers, d, daily=False):
        return None


class _Broker:
    def __init__(self, positions=None, fills=None):
        self._positions, self._fills = positions, fills

    def positions(self):
        return self._positions

    def fills(self, d):
        return self._fills


def _at(d, hh, mm):
    return datetime.combine(d, dtime(hh, mm), tzinfo=ET)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A seeded ledger file. `seed(fn)` writes with a normal connection; `ro()` reopens the
    file read-only, exactly as the script opens production."""
    path = tmp_path / "verify.duckdb"
    monkeypatch.setattr(ledger, "DB_PATH", str(path))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    class _DB:
        def seed(self, fn):
            con = ledger.connect()
            try:
                return fn(con)
            finally:
                con.close()

        def ro(self):
            return vf.open_readonly(dsn="", path=str(path))[0]

        def digest(self):
            return hashlib.sha256(path.read_bytes()).hexdigest()

    return _DB()


def _pick(ticker, d=D1016, **kw):
    p = {"ticker": ticker, "direction": "short", "setup_type": "breakdown_short", "catalyst": "c",
         "thesis_final": "t", "conviction": 3, "size_r": 0.25,
         "entry_plan": {"type": "breakout", "price": 418.0}, "entry_price": 418.0,
         "stop_price": 433.0, "target_price": 400.0, "time_stop": "by 14:00",
         "invalidation": "i", "entry_not_before": "09:45"}
    p.update(kw)
    return p


def _plan(picks, **kw):
    return {"no_trade": not picks, "market_stance": "t", "total_risk_r": 0.25 * len(picks),
            "rejected": [], "notes_for_evening": "", "picks": picks, **kw}


def _run_row(con, d, stages, status="ok"):
    ledger.log_run(con, "evening", d, datetime(d.year, d.month, d.day, 16, 30), status,
                   stages=stages)


# ------------------------------------------------------------------- read-only

def test_the_ledger_is_opened_read_only(db):
    db.seed(lambda con: None)
    con = db.ro()
    with pytest.raises(Exception):
        con.execute("CREATE TABLE intruder (x INTEGER)")
    con.close()


def test_the_broker_guard_refuses_anything_but_a_get(monkeypatch):
    from agent import live_broker
    calls = []
    monkeypatch.setattr(live_broker, "_request",
                        lambda method, path, *, cfg=None, payload=None: calls.append((method, path)) or [])
    vf.install_get_only_guard()
    assert live_broker._request("GET", "/v2/positions") == []
    for method, path in (("POST", "/v2/orders"), ("DELETE", "/v2/positions/IOVA"), ("PATCH", "/v2/orders/x")):
        with pytest.raises(RuntimeError, match="read-only"):
            live_broker._request(method, path)
    with pytest.raises(RuntimeError):
        live_broker._request("GET", "/v2/orders", payload={"qty": 1})
    assert calls == [("GET", "/v2/positions")]
    vf.install_get_only_guard()                  # idempotent: no double wrapping
    assert live_broker._request("GET", "/v2/account") == []


def test_the_script_run_end_to_end_writes_nothing(db, capsys, monkeypatch):
    from agent import live_broker
    monkeypatch.setattr(live_broker, "_request", live_broker._request)   # undo main()'s guard afterwards
    db.seed(lambda con: ledger.save_daily_plan(con, D1016, _plan([_pick("AMAT")]), {"candidates": []}, "chop"))
    before = db.digest()
    rc = cli.main(["--date", "2026-10-16", "--no-broker", "--no-market-data"])
    out = capsys.readouterr().out
    assert rc == 1                                           # this seeded day is not all PASS
    assert "writes nothing, places nothing" in out and "verify_fixes 2026-10-16" in out
    assert db.digest() == before


# ------------------------------------------------------------------------- D1

def test_d1_is_pending_until_the_close(db):
    db.seed(lambda con: None)
    r = vf.check_d1(db.ro(), D1016, CFG, _Broker(positions=[]), _at(D1016, 15, 0))
    assert r.status == vf.PENDING


def test_d1_passes_on_a_flat_account_after_the_close(db):
    db.seed(lambda con: None)
    r = vf.check_d1(db.ro(), D1016, CFG, _Broker(positions=[]), _at(D1016, 16, 30))
    assert r.status == vf.PASS and "0 positions after the close" in r.lines[-1]


def test_d1_fails_on_iova_left_open_and_names_it(db):
    db.seed(lambda con: None)
    held = [{"symbol": "IOVA", "qty": "250", "side": "long", "avg_entry_price": "14.36"}]
    r = vf.check_d1(db.ro(), D1016, CFG, _Broker(positions=held), _at(D1016, 16, 30))
    assert r.status == vf.FAIL
    assert "STILL OPEN after the close: IOVA 250 sh long @ 14.36" in r.lines[-1]


def test_d1_allow_listed_symbols_may_stay(db):
    db.seed(lambda con: None)
    cfg = Config(raw={"live_paper": {"allow_overnight_symbols": ["iova"]}})
    held = [{"symbol": "IOVA", "qty": "250", "side": "long", "avg_entry_price": "14.36"}]
    assert vf.check_d1(db.ro(), D1016, cfg, _Broker(positions=held), _at(D1016, 16, 30)).status == vf.PASS


def test_d1_an_unreadable_account_is_unverified_not_pass(db):
    db.seed(lambda con: None)
    assert vf.check_d1(db.ro(), D1016, CFG, _Broker(positions=None), _at(D1016, 16, 30)).status == vf.UNVERIFIED
    assert vf.check_d1(db.ro(), D1016, CFG, None, _at(D1016, 16, 30)).status == vf.UNVERIFIED


def _backstop(con, d, kind, sym, status, reason="ledger_open", qty=250):
    ledger.record_backstop_close(con, session_date=d, kind=kind, symbol=sym, position_side="long",
                                 qty=qty, avg_entry_price=14.36, reason=reason, claimed_by=None,
                                 carried_from=None, close_order_id="o1", status=status,
                                 submitted_at=None, filled_price=None, filled_at=None, detail=None)


def test_d1_a_sweep_close_is_reported_and_a_refused_one_fails(db):
    db.seed(lambda con: _backstop(con, D1016, "eod_backstop", "IOVA", "filled"))
    r = vf.check_d1(db.ro(), D1016, CFG, _Broker(positions=[]), _at(D1016, 16, 30))
    assert r.status == vf.PASS and any(l.startswith("FLATTEN MISSED, closed by the sweep: eod_backstop: IOVA")
                                       for l in r.lines)
    db.seed(lambda con: _backstop(con, D1016, "eod_backstop", "MU", "refused"))
    r = vf.check_d1(db.ro(), D1016, CFG, _Broker(positions=[]), _at(D1016, 16, 30))
    assert r.status == vf.FAIL and any("did NOT go through" in l and "MU" in l for l in r.lines)


def test_d1_a_past_session_is_judged_from_its_fills(db):
    db.seed(lambda con: None)
    now = _at(D1016 + timedelta(days=3), 18, 0)
    flat = [{"symbol": "AMAT", "side": "sell_short", "qty": "8"}, {"symbol": "AMAT", "side": "buy", "qty": "8"}]
    assert vf.check_d1(db.ro(), D1016, CFG, _Broker(fills=flat), now).status == vf.PASS
    left = flat + [{"symbol": "IOVA", "side": "buy", "qty": "250"}]
    r = vf.check_d1(db.ro(), D1016, CFG, _Broker(fills=left), now)
    assert r.status == vf.FAIL and "IOVA: the day's fills net +250 sh" in r.lines[-1]
    assert vf.check_d1(db.ro(), D1016, CFG, _Broker(fills=None), now).status == vf.UNVERIFIED


def test_d1_a_past_session_that_closed_an_earlier_carry_is_flat(db):
    db.seed(lambda con: _backstop(con, D1016, "morning_sweep", "IOVA", "filled", reason="unledgered"))
    sold = [{"symbol": "IOVA", "side": "sell", "qty": "250"}]
    r = vf.check_d1(db.ro(), D1016, CFG, _Broker(fills=sold), _at(D1016 + timedelta(days=3), 18, 0))
    assert r.status == vf.PASS and any("earlier carry the morning sweep closed" in l for l in r.lines)


def test_d1_before_the_schema_exists_it_fails(db):
    db.seed(lambda con: con.execute("DROP TABLE eod_backstop_closes"))
    r = vf.check_d1(db.ro(), D1016, CFG, _Broker(positions=[]), _at(D1016, 16, 30))
    assert r.status == vf.FAIL and "schema v36 is not deployed" in r.lines[0]


# ---------------------------------------------------------------------- D2/D3

def _live_day(con, broker_fields: bool, divergence="none", touch_pick=False):
    picks = [_pick("RKLB", entry_plan={"type": "limit", "price": 54.0}, entry_price=54.0,
                   stop_price=56.5, target_price=48.0)]
    if touch_pick:
        picks.append(_pick("CMC", direction="long", setup_type="catalyst_momentum",
                           entry_plan={"type": "limit", "price": 66.5}, entry_price=66.5,
                           stop_price=64.0, target_price=72.0))
    ids = ledger.save_daily_plan(con, D1015, _plan(picks), {"candidates": []}, "chop")
    for pid in ids:
        ledger.save_outcome(con, {"pick_id": pid, "eval_date": D1015, "triggered": False,
                                  "exit_reason": "no_trigger", "touch_only_bars": 2})
        if broker_fields:          # what the evening's _record_broker_truth writes
            con.execute("UPDATE outcomes SET broker_status = 'canceled', sim_broker_divergence = ? "
                        "WHERE pick_id = ?", [divergence, pid])
    ledger.save_live_order(con, ids[0], {"id": "lo-1", "status": "canceled"}, D1015, "RKLB", "sell", 46, "limit")
    return ids


def test_d2_d3_pass_lists_divergences_and_touches(db):
    db.seed(lambda con: _live_day(con, True, divergence="sim_only_fill", touch_pick=True))
    r = vf.check_d2_d3(db.ro(), D1015)
    assert r.status == vf.PASS
    assert any(l.startswith("DIVERGENCE RKLB: sim_only_fill") for l in r.lines)
    assert any("touch, not a fill: CMC limit 66.5 reached on 2 bar(s)" in l for l in r.lines)


def test_d2_d3_missing_broker_fields_fail_once_the_evening_ran(db):
    db.seed(lambda con: _live_day(con, False))
    assert vf.check_d2_d3(db.ro(), D1015).status == vf.PENDING          # the evening has not run
    db.seed(lambda con: _run_row(con, D1015, {"llm": "complete"}))
    r = vf.check_d2_d3(db.ro(), D1015)
    assert r.status == vf.FAIL and "not recorded beside the simulation" in r.lines[0]


def test_d2_d3_a_day_with_no_live_pick_passes(db):
    db.seed(lambda con: ledger.save_daily_plan(con, D1015, _plan([]), {"candidates": []}, "chop"))
    r = vf.check_d2_d3(db.ro(), D1015)
    assert r.status == vf.PASS and r.lines == ["no pick went to the broker this session"]


# ------------------------------------------------------------------------- D4

INTC = dict(direction="short", setup_type="breakdown_short", entry_plan={"type": "breakout", "price": 92.0},
            entry_price=92.0, stop_price=96.0, target_price=86.0, time_stop="by 14:30",
            exploration_lane="coverage")


def _rej(ticker, spec):
    return {"ticker": ticker, "direction": spec["direction"], "setup_type": spec["setup_type"],
            "reason": "R:R below the control", "entry_type": spec["entry_plan"]["type"],
            "entry_price": spec["entry_price"], "stop_price": spec["stop_price"],
            "target_price": spec["target_price"], "conviction": 3, "thesis": "t",
            "source_stage": "risk_rejected", "rejection_category": "rr_below_control",
            "planned_rr": 1.4, "session_degraded": False, "primary_kill_check": "rr",
            "kill_checks": ["rr"], "entry_not_before": "09:45"}


def _shadow_day(con, graded_by="fixed"):
    """10-15 INTC: rejected, and traded by coverage. Graded the way the fixed evening grades
    it, or the way the old one did (forgone R stamped on a traded row)."""
    md = _MD({"INTC": _real("INTC", D1015)})
    plan = _plan([_pick("INTC", D1015, **INTC)])
    ledger.save_daily_plan(con, D1015, plan, {"candidates": [{"ticker": "INTC", "direction": "short",
                                                              "entry_plan": {"price": 92.0},
                                                              "time_stop": "by 14:30"}]}, "chop")
    ledger.save_rejections(con, D1015, [_rej("INTC", INTC)])
    for o in grading.grade_day(md, ledger.picks_for_date(con, D1015), D1015, slippage_bps=5):
        ledger.save_outcome(con, o)
    if graded_by == "fixed":
        from agent.loops import evening
        evening._grade_rejections(con, md, D1015, slippage_bps=5)
    else:
        con.execute("UPDATE rejections SET eod_outcome = 'would_have_lost', forgone_r = -1.012")
    _run_row(con, D1015, {"llm": "complete"})
    return md


def test_d4_passes_when_the_traded_rejection_is_excluded_and_reprices_to_the_taken_r(db):
    md = db.seed(_shadow_day)
    r = vf.check_d4(db.ro(), D1015, md, CFG)
    assert r.status == vf.PASS
    assert "INTC traded via coverage: excluded from forgone R, taken -0.51R" in r.lines[0]
    assert r.lines[0].endswith("shadow re-priced -0.51R = taken")


def test_d4_fails_on_a_traded_rejection_counted_as_forgone(db):
    db.seed(lambda con: _shadow_day(con, graded_by="old"))
    r = vf.check_d4(db.ro(), D1015, None, CFG)
    assert r.status == vf.FAIL and "carries forgone R -1.01R" in r.lines[0]


def test_d4_fails_when_the_shadow_and_the_taken_r_disagree(db):
    md = db.seed(_shadow_day)
    db.seed(lambda con: con.execute("UPDATE outcomes SET r_planned = -0.40"))
    r = vf.check_d4(db.ro(), D1015, md, CFG)
    assert r.status == vf.FAIL and "shadow R -0.51R != taken R -0.40R" in r.lines[0]


def test_d4_without_market_data_says_it_did_not_reprice(db):
    db.seed(_shadow_day)
    r = vf.check_d4(db.ro(), D1015, None, CFG)
    assert r.status == vf.PASS and "(shadow R not re-priced: no market data)" in r.lines[0]


# ------------------------------------------------------------------------- D5

def test_d5_counts_the_tags_and_fails_on_an_untagged_pick(db):
    def seed(con):
        ledger.save_daily_plan(con, D1016, _plan([
            _pick("AMAT", entry_semantics="ok", entry_ref_price=426.0),
            _pick("USO", direction="long", setup_type="sector_rotation",
                  entry_plan={"type": "limit", "price": 155.5}, entry_price=155.5, stop_price=152.5,
                  target_price=161.0, entry_semantics="strength_buy_limit_below_ref",
                  entry_ref_price=156.8)]), {"candidates": []}, "chop")
    db.seed(seed)
    r = vf.check_d5(db.ro(), D1016)
    assert r.status == vf.PASS
    assert r.lines[0] == "tags: ok 1, strength_buy_limit_below_ref 1"
    assert "USO (main, sector_rotation, limit vs reference 156.8): strength_buy_limit_below_ref" in r.lines[1]
    db.seed(lambda con: con.execute("UPDATE picks SET entry_semantics = NULL WHERE ticker = 'AMAT'"))
    r = vf.check_d5(db.ro(), D1016)
    assert r.status == vf.FAIL and "AMAT: no entry_semantics tag" in r.lines


def test_d5_no_plan_is_pending_and_a_no_trade_plan_passes(db):
    db.seed(lambda con: None)
    assert vf.check_d5(db.ro(), D1016).status == vf.PENDING
    db.seed(lambda con: ledger.save_daily_plan(con, D1016, _plan([]), {"candidates": []}, "chop"))
    assert vf.check_d5(db.ro(), D1016).status == vf.PASS


def test_d5_before_the_schema_exists_it_fails(db):
    db.seed(lambda con: con.execute("ALTER TABLE picks DROP COLUMN entry_semantics"))
    r = vf.check_d5(db.ro(), D1016)
    assert r.status == vf.FAIL and "schema v38 is not deployed" in r.lines[0]


# ------------------------------------------------------------------------- D6

def test_d6_checks_the_grader_prompt_version_and_counts_the_flags(db):
    db.seed(lambda con: _run_row(con, D1016, {"grader_prompt_version": "11",
                                              "audit": {"findings": 2, "by_check": {"mechanics_contradiction": 2}}}))
    r = vf.check_d6(db.ro(), D1016, current="11")
    assert r.status == vf.PASS and r.lines == ["grader prompt v11 (current)", "mechanics_contradiction flags: 2"]
    assert vf.check_d6(db.ro(), D1016, current="12").status == vf.FAIL


def test_d6_a_run_from_before_the_fix_or_no_run(db):
    db.seed(lambda con: None)
    assert vf.check_d6(db.ro(), D1016, current="11").status == vf.PENDING
    db.seed(lambda con: _run_row(con, D1016, {"llm": "complete"}))
    r = vf.check_d6(db.ro(), D1016, current="11")
    assert r.status == vf.FAIL and "recorded no grader prompt version" in r.lines[0]


def test_d6_reads_a_stages_record_cut_at_4000_characters():
    long = json.dumps({"grader_prompt_version": "11",
                       "audit": {"findings": 30, "by_check": {"mechanics_contradiction": 3},
                                 "items": [{"why": "x" * 300}] * 30}})[:4000]
    with pytest.raises(ValueError):
        json.loads(long)
    assert vf._stage(long, "grader_prompt_version") == "11"
    assert vf._mechanics_flags(long) == 3


def test_d6_the_current_version_is_the_repositorys_grader_prompt():
    from agent.llm import prompt_version
    assert prompt_version("evening_grader") == "11"


# ------------------------------------------------------------------------- D7

def _rule_day(con, record: bool):
    rid = research.register_rule(con, "gap_go trades", ["setup_type"], date(2026, 10, 1))["rule_id"]
    research.advance_quarantine(con, rid, date(2026, 10, 1))
    con.execute("UPDATE research_rules SET lane = 'morning', predicate_json = ? WHERE rule_id = ?",
                [json.dumps({"lane": "morning", "conditions": [{"dimension": "setup_type", "level": "gap_go"}]}), rid])
    pid = ledger.save_daily_plan(con, D1016, _plan([_pick("TEST", setup_type="gap_go")]),
                                 {"candidates": []}, "chop")[0]
    ledger.save_outcome(con, {"pick_id": pid, "eval_date": D1016, "triggered": True, "exit_reason": "eod",
                              "r_multiple": 0.4, "r_planned": 0.4, "execution_anomaly": False})
    if record:
        research.record_rule_observation(con, rid, D1016, 0.4, f"outcome:{pid}", "taken")
    return rid


def test_d7_passes_when_a_matching_trade_was_recorded(db):
    db.seed(lambda con: _rule_day(con, record=True))
    r = vf.check_d7(db.ro(), D1016, _at(D1016, 18, 0))
    assert r.status == vf.PASS and "1 matching row(s), 1 unit(s), 1 forward observation(s)" in r.lines[0]


def test_d7_fails_when_a_matching_trade_was_not_recorded_and_waits_before_the_evening(db):
    db.seed(lambda con: _rule_day(con, record=False))
    assert vf.check_d7(db.ro(), D1016, _at(D1016, 15, 0)).status == vf.PENDING
    r = vf.check_d7(db.ro(), D1016, _at(D1016, 18, 0))
    assert r.status == vf.FAIL and "rows matched and nothing was recorded" in r.lines[0]


def test_d7_a_session_inside_the_rules_own_sample_is_not_judged(db):
    def seed(con):
        rid = _rule_day(con, record=False)
        con.execute("UPDATE research_rules SET quarantined_at = ? WHERE rule_id = ?", [D1016, rid])
    db.seed(seed)
    r = vf.check_d7(db.ro(), D1016, _at(D1016, 18, 0))
    assert r.status == vf.PASS and r.lines == ["no rule's cell had a matching row this session"]


# ---------------------------------------------------------------- run + render

def test_one_broken_check_never_hides_the_others(db, monkeypatch):
    db.seed(lambda con: None)
    monkeypatch.setattr(vf, "check_d5", lambda con, d: 1 / 0)
    results = vf.run_checks(db.ro(), D1016, CFG, _Broker(positions=[]), None, _at(D1016, 16, 30))
    assert [r.item for r in results] == ["D1", "D2/D3", "D4", "D5", "D6", "D7"]
    d5 = results[3]
    assert d5.status == vf.UNVERIFIED and "ZeroDivisionError" in d5.lines[0]
    text = vf.render(D1016, results)
    assert "UNVERIFIED D5" in text and text.endswith("this session does not count toward the 5")


# ------------------------------------------------------------------ --impact

def _old_1015(con):
    """2026-10-15 as the OLD evening left it: INTC and CMC rejected, traded by coverage, and
    still priced as forgone (-1.41R, "the filters saved money"); five untriggered rejections."""
    cmc = dict(direction="long", setup_type="catalyst_momentum", entry_plan={"type": "limit", "price": 66.5},
               entry_price=66.5, stop_price=64.0, target_price=72.0, time_stop="by 14:00",
               exploration_lane="coverage")
    ledger.save_daily_plan(con, D1015, _plan([_pick("INTC", D1015, **INTC), _pick("CMC", D1015, **cmc)]),
                           {"candidates": [{"ticker": "INTC", "direction": "short", "time_stop": "by 14:30",
                                            "entry_plan": {"price": 92.0}},
                                           {"ticker": "CMC", "direction": "long", "time_stop": "by 14:00",
                                            "entry_plan": {"price": 66.5}}]}, "chop")
    unt = [dict(direction="long", setup_type="trend_pullback", entry_plan={"type": "limit", "price": 95.0},
                entry_price=95.0, stop_price=93.0, target_price=99.0) for _ in range(5)]
    ledger.save_rejections(con, D1015, [_rej("INTC", INTC), _rej("CMC", cmc)]
                           + [_rej(f"UNT{i}", s) for i, s in enumerate(unt, 1)])
    md = _MD({"INTC": _real("INTC", D1015), "CMC": _real("CMC", D1015),
              **{f"UNT{i}": [{**b, "o": 100.0, "h": 100.2, "l": 99.8, "c": 100.0} for b in _real("INTC", D1015)]
                 for i in range(1, 6)}})
    for o in grading.grade_day(md, ledger.picks_for_date(con, D1015), D1015, slippage_bps=5):
        ledger.save_outcome(con, o)
    con.execute("UPDATE rejections SET eod_outcome = 'scratch', forgone_r = 0.0")
    con.execute("UPDATE rejections SET eod_outcome = 'would_have_lost', forgone_r = -1.012 WHERE ticker = 'INTC'")
    con.execute("UPDATE rejections SET eod_outcome = 'would_have_lost', forgone_r = -0.395 WHERE ticker = 'CMC'")
    return md


def test_impact_reprices_1015_from_minus_1_41_to_zero(db):
    md = db.seed(_old_1015)
    before = db.digest()
    res = vf.shadow_impact(db.ro(), md, CFG, D1015)
    assert res["before"]["net_forgone_r"] == -1.41                 # what the 10-15 review read
    assert res["after"]["net_forgone_r"] == 0.0
    assert res["exclusion_only_net"] == 0.0 and res["lane_stored_forgone_r"] == -1.41
    lane = {t["ticker"]: t for t in res["lane"]}
    assert lane["INTC"]["taken_r"] == pytest.approx(-0.513, abs=0.002)
    assert lane["CMC"]["taken_r"] == pytest.approx(-0.385, abs=0.002)
    text = vf.format_shadow_impact(res)
    assert "lane rows removed only (no re-pricing): +0.00R" in text
    assert "2026-10-15 INTC   (coverage) stored forgone -1.01R; taken -0.51R" in text
    assert db.digest() == before                                     # re-priced in memory only


def test_impact_finds_the_rklb_fill_only_a_touch_produced(db):
    def seed(con):
        ledger.save_daily_plan(con, D1015, _plan([_pick(
            "RKLB", D1015, entry_plan={"type": "limit", "price": 54.0}, entry_price=54.0, stop_price=56.5,
            target_price=48.0)]), {"candidates": []}, "chop")
        pid = ledger.picks_for_date(con, D1015)[0]["pick_id"]
        ledger.save_outcome(con, {"pick_id": pid, "eval_date": D1015, "triggered": True,
                                  "exit_reason": "time_stop", "r_planned": 0.21, "r_multiple": 0.21})
    db.seed(seed)
    # a second limit pick whose bars cannot be read: skipped, and the rule is still restored
    db.seed(lambda con: con.execute(
        "INSERT INTO picks (pick_id, plan_date, ticker, direction, entry_type, entry_price, stop_price, "
        "target_price, size_r, conviction) VALUES (999, ?, 'BOOM', 'long', 'limit', 10, 9, 12, 0.25, 3)",
        [D1015]))
    db.seed(lambda con: ledger.save_outcome(con, {"pick_id": 999, "eval_date": D1015, "triggered": False,
                                                  "exit_reason": "no_trigger"}))
    bars, t = [], datetime(2026, 10, 15, 9, 30)
    while t.strftime("%H:%M") <= "15:59":
        hi = 54.02 if t.strftime("%H:%M") == "11:15" else 53.80
        bars.append({"t": t, "o": 53.5, "h": hi, "l": 53.3, "c": 53.5, "v": 90_000})
        t += timedelta(minutes=1)

    class _Flaky(_MD):
        def minute_bars(self, ticker, d):
            if ticker.upper() == "BOOM":
                raise RuntimeError("no bars")
            return super().minute_bars(ticker, d)

    text = vf.limit_fill_impact(db.ro(), _Flaky({"RKLB": bars}), CFG, D1015)
    assert "touch rule (sim v7) fills 1; trade-through rule (sim v8) fills 0" in text
    assert "2026-10-15 RKLB limit 54.0" in text and "touched 1 bar(s)" in text
    assert grading.LIMIT_FILL_CUSHION_BPS == 5.0 and grading.LIMIT_FILL_TICK == 0.01   # restored


def test_impact_tags_history_and_tabulates_fill_rate_and_r(db):
    def seed(con):
        ledger.save_daily_plan(con, D1016, _plan([
            _pick("AMAT"),
            _pick("USO", direction="long", setup_type="sector_rotation",
                  entry_plan={"type": "limit", "price": 155.5}, entry_price=155.5, stop_price=152.5,
                  target_price=161.0, entry_ref_price=156.8)]), {"candidates": []}, "chop")
        ids = {p["ticker"]: p["pick_id"] for p in ledger.picks_for_date(con, D1016)}
        con.execute("UPDATE picks SET entry_semantics = NULL")       # history from before the tag
        ledger.save_outcome(con, {"pick_id": ids["AMAT"], "eval_date": D1016, "triggered": True,
                                  "exit_reason": "time_stop", "r_planned": 0.06, "r_multiple": 0.055})
        ledger.save_outcome(con, {"pick_id": ids["USO"], "eval_date": D1016, "triggered": False,
                                  "exit_reason": "no_trigger"})
    db.seed(seed)
    text = vf.entry_semantics_table(db.ro(), None, D1016)
    assert "2 graded pick(s) (2 tagged here from stored levels)" in text
    ok = next(l for l in text.splitlines() if l.strip().startswith("ok "))
    bad = next(l for l in text.splitlines() if "strength_buy_limit_below_ref" in l)
    assert "100%" in ok and "+0.06R" in ok
    assert "0%" in bad and "n/a" in bad


def test_impact_held_counterfactual_for_amat(db):
    def seed(con):
        pid = ledger.save_daily_plan(con, D1016, _plan([_pick("AMAT")]), {"candidates": []}, "chop")[0]
        ledger.save_outcome(con, {"pick_id": pid, "eval_date": D1016, "triggered": True,
                                  "exit_reason": "time_stop", "r_planned": 0.056, "r_multiple": 0.055})
    db.seed(seed)
    text = vf.held_counterfactual(db.ro(), _MD({"AMAT": _real("AMAT", D1016)}), CFG, D1016)
    assert "1 time-stopped trade(s)" in text
    assert "r_if_held_to_flatten - r_multiple: sum +0.57R" in text
    assert "2026-10-16 AMAT: time stop +0.06R, held +0.63R" in text


def test_impact_replays_the_mechanics_audit_over_stored_reviews(db):
    def seed(con):
        pid = ledger.save_daily_plan(con, D1016, _plan([_pick("AMAT")]), {"candidates": []}, "chop")[0]
        ledger.save_outcome(con, {"pick_id": pid, "eval_date": D1016, "triggered": True,
                                  "exit_reason": "time_stop", "r_planned": 0.056, "r_multiple": 0.055})
        con.execute("UPDATE outcomes SET grade = 'C', grade_notes = ?",
                    ["Entry at 11:07 came 97 minutes late: chasing a breakdown."])
    db.seed(seed)
    text = vf.mechanics_replay(db.ro(), D1016)
    assert "1 of 1 stored pick review(s) would have been flagged" in text and "AMAT (grade C)" in text


def test_impact_counts_the_fund_picks_the_gate_killed(db):
    plan = _plan([], validator_drops=[
        {"ticker": "USO", "kind": "validity", "reason": "USO: entry 68.5 is 55% away from last price 153.6 "
                                                        "(max 15%) — dropped as ungrounded"},
        {"ticker": "NVDA", "kind": "validity", "reason": "NVDA: dropped as ungrounded"}])
    db.seed(lambda con: ledger.save_daily_plan(con, D1015, plan, {"candidates": []}, "chop"))
    text = vf.etf_gate_kills(db.ro(), D1015)
    assert "1 fund pick(s) the 15% gate killed" in text and "2026-10-15 USO" in text


def test_impact_report_survives_a_section_that_cannot_be_computed(db, monkeypatch):
    db.seed(lambda con: None)
    monkeypatch.setattr(vf, "limit_fill_impact", lambda *a: 1 / 0)
    text = vf.impact_report(db.ro(), _MD({}), CFG, D1015)
    assert "D2: could not be computed (ZeroDivisionError" in text
    assert "D4 SHADOW LEDGER" in text and "D8 ETF GROUNDING" in text


def test_d1_a_position_carried_through_a_past_session_fails_from_the_next_mornings_sweep(db):
    """IOVA's shape: opened long before, never traded on the session, still held after its
    close. That day's fills net to zero, so only the next morning's sweep row can show it."""
    monday = date(2026, 10, 19)                    # the market day after Friday 10-16
    db.seed(lambda con: _backstop(con, monday, "morning_sweep", "IOVA", "filled", reason="unledgered"))
    flat = [{"symbol": "AMAT", "side": "sell_short", "qty": "8"}, {"symbol": "AMAT", "side": "buy", "qty": "8"}]
    r = vf.check_d1(db.ro(), D1016, CFG, _Broker(fills=flat), _at(date(2026, 10, 20), 18, 0))
    assert r.status == vf.FAIL
    assert ("IOVA long 250 sh was still open after the 2026-10-16 close: the 2026-10-19 morning sweep "
            "found it (unledgered, filled)") in r.lines
    # the same row is, for Monday itself, an earlier carry its own sweep closed
    sold = [{"symbol": "IOVA", "side": "sell", "qty": "250"}]
    assert vf.check_d1(db.ro(), monday, CFG, _Broker(fills=sold), _at(date(2026, 10, 21), 18, 0)).status == vf.PASS


# ---------------------------------- impact precision (2026-10-18, the first production run)

CMC_1015 = dict(direction="long", setup_type="catalyst_momentum", entry_plan={"type": "limit", "price": 66.5},
                entry_price=66.5, stop_price=64.0, target_price=72.0, exploration_lane="coverage")


def _seed_reviews(con, tapes=True):
    """INTC and CMC 10-15 as stored: graded, reviewed and, when `tapes`, with the evening's
    tape summary, where the replay reads each fill time (a re-grade keeps it)."""
    ledger.save_daily_plan(con, D1015, _plan([_pick("INTC", D1015, **INTC), _pick("CMC", D1015, **CMC_1015)]),
                           {"candidates": []}, "chop")
    by = {p["ticker"]: p["pick_id"] for p in ledger.picks_for_date(con, D1015)}
    ledger.save_outcome(con, {"pick_id": by["INTC"], "eval_date": D1015, "triggered": True, "entry_fill": 91.954,
                              "exit_reason": "time_stop", "r_planned": -0.51, "r_multiple": -0.51})
    ledger.save_outcome(con, {"pick_id": by["CMC"], "eval_date": D1015, "triggered": True, "entry_fill": 66.063,
                              "exit_reason": "time_stop", "r_planned": -0.39, "r_multiple": -0.39})
    con.execute("UPDATE outcomes SET grade = 'C', grade_notes = ? WHERE pick_id = ?",
                ["Stop was fine. A breakout short makes no sense on a stock already this weak.", by["INTC"]])
    con.execute("UPDATE outcomes SET grade = 'C', grade_notes = ? WHERE pick_id = ?",
                ["Thesis partially right, structure lazy. Entry 66.06 filled @09:45 chasing a falling open.",
                 by["CMC"]])
    if tapes:
        ledger.set_outcome_grounding(con, by["INTC"], tape_summary=(
            "open 93.60; HOD 93.86 @09:30; LOD 91.50 @10:40; filled 91.95 @09:49; exit 94.01 (time_stop) @14:30"))
        ledger.set_outcome_grounding(con, by["CMC"], tape_summary=(
            "open 69.38; HOD 69.50 @09:30; LOD 65.60 @09:46; filled 66.06 @09:45; exit 65.81 (time_stop) @14:00"))


def test_impact_replay_quotes_what_fired_and_spares_an_order_through_its_level_at_go_live(db):
    db.seed(_seed_reviews)
    text = vf.mechanics_replay(db.ro(), D1015)
    assert "1 of 2 stored pick review(s) would have been flagged mechanics_contradiction" in text
    assert ('2026-10-15 INTC (grade C): senseless "makes no sense" in "A breakout short makes no sense on a '
            'stock already this weak."') in text
    assert "CMC (grade C)" not in text
    assert ("1 more left alone: the order was already through its level when it went live, a market order "
            "in effect (CMC 2026-10-15)") in text


def test_impact_replay_resimulates_a_fill_time_the_tape_summary_lacks(db):
    db.seed(lambda con: _seed_reviews(con, tapes=False))
    unknown = vf.mechanics_replay(db.ro(), D1015)                 # no fill times: nothing can be spared
    assert "2 of 2 stored pick review(s)" in unknown and "CMC (grade C)" in unknown
    md = _MD({"INTC": _real("INTC", D1015), "CMC": _real("CMC", D1015)})
    text = vf.mechanics_replay(db.ro(), D1015, md, CFG)
    assert "1 of 2 stored pick review(s)" in text and "CMC (grade C)" not in text


def test_impact_report_hands_the_replay_its_market_data(db, monkeypatch):
    db.seed(lambda con: None)
    seen = {}
    monkeypatch.setattr(vf, "mechanics_replay", lambda con, since, md=None, cfg=None: seen.update(md=md, cfg=cfg) or "D6")
    md = _MD({})
    vf.impact_report(db.ro(), md, CFG, D1015)
    assert seen == {"md": md, "cfg": CFG}


def test_impact_counts_fund_kills_wherever_they_were_recorded(db):
    """The first production run printed 0. The incident kills were lane trials, whose
    validator lines are kept in plan.validation_warnings, not validator_drops; and the risk
    manager can kill a fund for its price before any validator sees it."""
    plan = _plan([], validation_warnings=[
        "coverage XLE: XLE: entry 87.0 is 30% away from last price 66.85 (max 15%) — dropped as ungrounded; "
        "the level was priced off something other than the actual market",
        "XLE: entry 87.0 is 30% away from last price 66.85 (max 15%) — dropped as ungrounded",     # same kill
        "coverage NVDA: NVDA: entry 100.0 is 44% away from last price 180.0 (max 15%) — dropped as ungrounded",
        "COVERAGE: 1 setup(s) traded at 0.25R: AMAT breakdown_short"])

    def seed(con):
        ledger.save_daily_plan(con, D1016, plan, {"candidates": []}, "chop")
        base = dict(direction="long", setup_type="sector_rotation", entry_type="limit", stop_price=1.0,
                    target_price=3.0, conviction=3, thesis="t", source_stage="risk_rejected")
        ledger.save_rejections(con, D1016, [
            {**base, "ticker": "XLE", "entry_price": 87.0, "reason": "R:R 1.5 below 2.0",
             "primary_kill_check": "rr", "kill_checks": ["rr"]},                   # killed for R:R; the gate later
            {**base, "ticker": "USO", "entry_price": 68.5, "reason": "entry 68.50 vs live 153.6",
             "primary_kill_check": "grounding", "kill_checks": ["grounding"]},
            {**base, "ticker": "XLV", "entry_price": 148.0, "reason": "levels priced from memory; stale",
             "primary_kill_check": "staleness", "kill_checks": ["staleness"]},
            {**base, "ticker": "XLU", "entry_price": 61.0, "reason": "chasing the gap",
             "primary_kill_check": "chase", "kill_checks": ["chase", "grounding"]},   # grounding binds too
            {**base, "ticker": "MSFT", "entry_price": 300.0, "reason": "ungrounded price",
             "primary_kill_check": "grounding", "kill_checks": ["grounding"]}])   # not a fund
    db.seed(seed)
    text = vf.etf_gate_kills(db.ro(), D1016)
    assert text.splitlines()[0] == ("D8 ETF GROUNDING from 2026-10-16: 1 fund pick(s) the 15% gate killed "
                                    "as priced from memory")
    assert text.count("2026-10-16 XLE") == 1 and "2026-10-16 XLE (coverage lane validator): XLE: entry 87.0" in text
    assert "  and 3 fund candidate(s) the risk manager killed for their price" in text
    assert "2026-10-16 XLU (risk manager, chase): chasing the gap" in text
    assert "2026-10-16 USO (risk manager, grounding): entry 68.50 vs live 153.6" in text
    assert "2026-10-16 XLV (risk manager, staleness): levels priced from memory; stale" in text
    assert "MSFT" not in text and "NVDA" not in text


def test_impact_d5_counts_only_the_picks_in_its_table(db):
    """The first production run printed "141 graded pick(s) (155 tagged here ...)": the tagged
    count included the invalid and no-data rows the table leaves out."""
    def seed(con):
        ledger.save_daily_plan(con, D1016, _plan([_pick("AMAT"), _pick("INTC", D1016, **INTC)]),
                               {"candidates": []}, "chop")
        ids = {p["ticker"]: p["pick_id"] for p in ledger.picks_for_date(con, D1016)}
        con.execute("UPDATE picks SET entry_semantics = NULL")       # history from before the tag
        ledger.save_outcome(con, {"pick_id": ids["AMAT"], "eval_date": D1016, "triggered": True,
                                  "exit_reason": "time_stop", "r_planned": 0.06, "r_multiple": 0.055})
        ledger.save_outcome(con, {"pick_id": ids["INTC"], "eval_date": D1016, "triggered": True,
                                  "exit_reason": "invalid_fill"})
    db.seed(seed)
    text = vf.entry_semantics_table(db.ro(), None, D1016)
    assert text.splitlines()[0] == ("D5 ENTRY SEMANTICS from 2026-10-16: 1 graded pick(s) (1 tagged here from "
                                    "stored levels); 1 left out: invalid fill, no data or invalid plan")
