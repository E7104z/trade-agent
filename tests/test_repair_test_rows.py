"""
`run.py repair-test-rows` -- removes what the 2026-09-25 test runs wrote into
production and rebuilds the shadow rows they deleted.

Replays that damage on a tmp ledger next to real rows of the same kinds, and
pins: the dry run writes nothing, --apply removes exactly the fixture rows and
leaves every real row alone, a wiped session's rejections come back from its
stored plan, and a session that still holds real rejections is refused.
"""
from __future__ import annotations

import importlib.util
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import ledger
from agent.config import Config

_spec = importlib.util.spec_from_file_location(
    "run_cli", Path(__file__).resolve().parent.parent / "run.py")
run_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_cli)

WIPED = date(2026, 9, 8)       # real session whose rejections the fixture wiped
LIVE = date(2026, 9, 4)        # real session the live-monitor fixture attached to
OLD = datetime(2026, 9, 10, 12, 0)

CANDIDATES = {"candidates": [
    {"ticker": t, "direction": "long", "setup_type": "catalyst_momentum",
     "entry_plan": {"type": "breakout", "price": px}, "stop_price": px - 1,
     "target_price": px + 1.5}
    for t, px in (("GLUE", 45.0), ("INSM", 125.0), ("MARA", 11.5))]}


@pytest.fixture()
def damaged(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "repair.duckdb"))
    con = ledger.connect()

    # real rows
    ledger.save_daily_plan(con, WIPED, {
        "no_trade": True, "picks": [], "persisted_rejection_count": 3,
        "rejected": [{"ticker": c["ticker"], "reason": "R:R 1.5 < 2.0"}
                     for c in CANDIDATES["candidates"]]}, CANDIDATES, "chop")
    pick_id = ledger.save_daily_plan(con, LIVE, {"no_trade": False, "picks": [{
        "ticker": "NVDA", "direction": "long", "setup_type": "gap_go",
        "entry_price": 100.0, "stop_price": 98.0, "target_price": 104.0}]},
        {"candidates": []}, "chop")[0]
    ledger.save_outcome(con, {"pick_id": pick_id, "eval_date": LIVE, "triggered": False})
    ledger.save_rejections(con, date(2026, 9, 3), [{
        "ticker": "SOXX", "reason": "regime", "entry_price": 200.0,
        "stop_price": 196.0, "target_price": 206.0}])
    real_lesson = ledger.insert_lesson(
        con, {"lesson": "Gap-and-go needs premarket volume", "confidence": "low"},
        "evening", date(2026, 9, 2))
    ledger.log_run(con, "evening", date(2026, 9, 23), datetime(2026, 9, 23, 16, 40), "ok")
    con.execute("UPDATE daily_plans SET created_at = ?", [OLD])

    # what the test runs wrote
    ledger.save_rejections(con, WIPED, [{
        "ticker": "PHVS", "reason": "no live quote", "entry_price": 12.4,
        "stop_price": 11.9, "target_price": 13.9, "session_degraded": True}])
    ledger.save_rejections(con, LIVE, [{
        "ticker": "AAAA", "reason": "R:R 1.4 < 2.0", "entry_price": 10.0,
        "stop_price": 9.0, "target_price": 11.4}])
    ledger.save_daily_plan(con, date(2026, 8, 29), {"no_trade": True, "picks": []},
                           {"candidates": []}, "chop")
    ledger.save_rejections(con, date(2026, 8, 29), [{
        "ticker": "MARA", "reason": "R:R 1.83 < 2.0", "entry_price": 11.5,
        "stop_price": 10.9, "target_price": 12.6}])
    con.execute("INSERT INTO daily_plans (plan_date, no_trade, market_summary, plan_json, "
                "analyst_json, regime_tag, notes_for_evening) "
                "VALUES (?, FALSE, '', '{}', '{}', '', '')", [date(2026, 1, 10)])
    fake = ledger.insert_lesson(
        con, {"lesson": "test lesson", "confidence": "low", "evidence": "seed"},
        "evening", date(2026, 1, 10))
    ledger.update_lesson_feedback(con, fake, "confirm", date(2026, 1, 25))
    ledger.save_live_order(con, pick_id, {"id": "stop-1", "status": "new",
                                          "order_class": "stop_then_oco"},
                           LIVE, "AAPL", "buy", 250, "breakout")
    ledger.set_live_r(con, pick_id, 2.0)
    ledger.log_run(con, "morning", WIPED, datetime.now(), "degraded")
    con.close()
    return {"pick_id": pick_id, "real_lesson": real_lesson, "fake_lesson": fake}


def _run(apply, rebuild=str(WIPED)):
    run_cli.cmd_repair_test_rows(
        Config(raw={}), SimpleNamespace(since="2026-09-24", rebuild=rebuild, apply=apply))


def _q(sql, params=()):
    con = ledger.connect()
    try:
        return con.execute(sql, list(params)).fetchall()
    finally:
        con.close()


def _snapshot():
    return {t: _q(f"SELECT COUNT(*) FROM {t}")[0][0]
            for t in ("lessons", "lesson_confirmations", "live_orders", "runs",
                      "daily_plans", "rejections")}


def test_dry_run_writes_nothing(damaged):
    before = _snapshot()
    _run(apply=False)
    assert _snapshot() == before


def test_apply_removes_only_fixture_rows(damaged):
    _run(apply=True)
    assert [r[0] for r in _q("SELECT lesson_id FROM lessons")] == [damaged["real_lesson"]]
    assert _q("SELECT COUNT(*) FROM lesson_confirmations WHERE lesson_id = ?",
              [damaged["fake_lesson"]])[0][0] == 0
    assert _q("SELECT COUNT(*) FROM live_orders")[0][0] == 0
    assert _q("SELECT live_r FROM outcomes WHERE pick_id = ?", [damaged["pick_id"]]) == [(None,)]
    assert _q("SELECT loop, run_date FROM runs") == [("evening", date(2026, 9, 23))]
    assert sorted(r[0] for r in _q("SELECT plan_date FROM daily_plans")) == [LIVE, WIPED]
    assert _q("SELECT ticker FROM rejections WHERE plan_date = ?", [date(2026, 9, 3)]) == [("SOXX",)]
    assert _q("SELECT COUNT(*) FROM rejections WHERE ticker IN ('AAAA', 'PHVS') "
              "OR plan_date = ?", [date(2026, 8, 29)])[0][0] == 0


def test_apply_rebuilds_the_wiped_session_from_its_stored_plan(damaged):
    _run(apply=True)
    rows = _q("SELECT ticker, reason, entry_price, eod_outcome FROM rejections "
              "WHERE plan_date = ? ORDER BY ticker", [WIPED])
    assert rows == [("GLUE", "R:R 1.5 < 2.0", 45.0, None),
                    ("INSM", "R:R 1.5 < 2.0", 125.0, None),
                    ("MARA", "R:R 1.5 < 2.0", 11.5, None)]


def test_rebuild_is_refused_while_real_rejections_remain(damaged, capsys):
    _run(apply=False, rebuild="2026-09-03")
    assert "REFUSED: the ledger still holds 1 real rejection(s)" in capsys.readouterr().out
