"""PERSONA_CHARACTER_SHEET_SPEC_v1 Phase 3 -- the persona_version stamp
(migration v29) and the descriptive persona monitor."""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

import pytest

from agent import ledger, migrations_v29, persona_monitor

ROOT = Path(__file__).resolve().parent.parent
D = date(2026, 10, 2)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "ledger.duckdb")
    c = ledger.connect()
    yield c
    c.close()


def _cols(con, table):
    return {r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()}


# ------------------------------------------------------------------ migration

def test_v29_adds_the_stamp_to_plans_picks_and_runs(con):
    for t in ("daily_plans", "picks", "runs"):
        assert "persona_version" in _cols(con, t)


def test_v29_is_portable_and_additive():
    assert migrations_v29.SCHEMA_VERSION == 29
    assert len(migrations_v29.STATEMENTS) == 3
    for s in migrations_v29.STATEMENTS:
        assert "ADD COLUMN IF NOT EXISTS" in s
        assert "nextval" not in s.lower() and "DOUBLE PRECISION" not in s.upper()
    assert all(s in ledger._MIGRATIONS for s in migrations_v29.STATEMENTS)


def test_connect_twice_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "l.duckdb")
    ledger.connect().close()
    monkeypatch.setenv("LEDGER_FORCE_MIGRATE", "1")   # run the whole list again
    c = ledger.connect()
    try:
        assert "persona_version" in _cols(c, "runs")
    finally:
        c.close()


def test_a_pre_v29_ledger_upgrades_and_old_inserts_still_work(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "old.duckdb")
    c = ledger.connect()
    for t in ("daily_plans", "picks", "runs"):
        c.execute(f"ALTER TABLE {t} DROP COLUMN persona_version")
    c.execute("UPDATE schema_meta SET fingerprint = 'pre-v29'")
    # An insert that never names the new column works on the old schema...
    c.execute("INSERT INTO runs (loop, run_date, status) VALUES ('morning', ?, 'ok')", [D])
    c.close()
    c = ledger.connect()                     # ...and the upgrade keeps that row
    try:
        assert "persona_version" in _cols(c, "picks")
        assert c.execute("SELECT persona_version FROM runs").fetchall() == [(None,)]
        c.execute("INSERT INTO runs (loop, run_date, status) VALUES ('evening', ?, 'ok')", [D])
    finally:
        c.close()


def test_no_positional_insert_into_stamped_tables():
    pat = re.compile(r"INSERT\s+INTO\s+(daily_plans|picks|runs)\s+VALUES", re.I)
    files = [ROOT / "run.py", *(ROOT / "agent").rglob("*.py"), *(ROOT / "api").rglob("*.py")]
    offenders = [str(f) for f in files if pat.search(f.read_text(encoding="utf-8"))]
    assert not offenders


# --------------------------------------------------------------------- stamps

def _plan(**extra):
    return {"no_trade": False, "market_stance": "x", "regime_tag": "chop",
            "prompt_version": "analyst_v12/risk_v13", "playbook_version": "9",
            "picks": [{"ticker": "AAA", "direction": "long", "setup_type": "gap_go",
                       "conviction": 3, "size_r": 0.5, "entry_plan": {"type": "open"},
                       "entry_price": 10.0, "stop_price": 9.5, "target_price": 11.0}],
            **extra}


def test_save_daily_plan_stamps_plan_and_picks(con):
    ledger.save_daily_plan(con, D, _plan(persona_version="1"), {"candidates": []}, "chop")
    assert con.execute("SELECT persona_version FROM daily_plans").fetchone() == ("1",)
    assert con.execute("SELECT persona_version FROM picks").fetchall() == [("1",)]


def test_save_daily_plan_without_persona_leaves_null(con):
    ledger.save_daily_plan(con, D, _plan(), {"candidates": []}, "chop")
    assert con.execute("SELECT persona_version FROM daily_plans").fetchone() == (None,)
    assert con.execute("SELECT persona_version FROM picks").fetchall() == [(None,)]


def test_log_run_stamp(con):
    t = datetime(2026, 10, 2, 9, 0)
    ledger.log_run(con, "morning", D, t, "ok")
    ledger.log_run(con, "evening", D, t, "ok", persona_version="1")
    rows = con.execute("SELECT loop, persona_version FROM runs ORDER BY run_id").fetchall()
    assert rows == [("morning", None), ("evening", "1")]


def test_runner_pick_is_stamped_only_when_the_selector_call_chose_it(con):
    from agent import runner_selection
    from agent.config import Config
    cfg = Config(raw={"runner": {"size_r": 0.25}, "min_size_r": 0.25})
    c = {"ticker": "RUN", "detection_price": 10.0, "stop_price": 9.8, "target_price": 10.4}
    runner_selection.save_runner_pick(con, D, c, {"rationale": "x", "conviction": 3,
                                                  "persona_version": "1"}, cfg)
    runner_selection.save_runner_pick(con, D, dict(c, ticker="AUTO"),
                                      {"rationale": "auto-selected: candidates <= max_picks"}, cfg)
    rows = dict(con.execute("SELECT ticker, persona_version FROM picks").fetchall())
    assert rows == {"RUN": "1", "AUTO": None}


# -------------------------------------------------------------------- monitor

FORBIDDEN = ("p =", "q =", "significant", "edge", "better", "worse")


def _clean(text: str) -> None:
    low = text.lower()
    for w in FORBIDDEN:
        assert w not in low, w


def test_monitor_on_an_empty_ledger(con):
    groups = persona_monitor.collect(con)
    text = persona_monitor.render_text(groups)
    assert persona_monitor.HEADER in text and "No sessions recorded yet." in text
    _clean(text)


def test_monitor_header_is_verbatim():
    assert persona_monitor.HEADER == (
        "Descriptive. Persona on/off periods are different calendar periods under different "
        "tape; this table cannot attribute any difference to the persona. It exists so a change "
        "in trade count, size or conviction is visible, not so it can be explained.")


def _seed(con):
    analyst = {"candidates": [{"ticker": "AAA", "conviction": 3},
                              {"ticker": "BBB", "conviction": 2, "probe_for": "Q3"}]}
    ledger.save_daily_plan(con, date(2026, 10, 1), _plan(), analyst, "chop")
    ledger.save_daily_plan(con, date(2026, 10, 2), _plan(persona_version="1"), analyst, "chop")
    con.execute("UPDATE picks SET exploration_lane = 'daily_floor' WHERE plan_date = ?",
                [date(2026, 10, 2)])
    pid = con.execute("SELECT pick_id FROM picks WHERE plan_date = ?", [date(2026, 10, 2)]).fetchone()[0]
    con.execute("INSERT INTO outcomes (pick_id, triggered, exit_reason) VALUES (?, TRUE, 'stop')", [pid])
    con.execute("""INSERT INTO daily_plans (plan_date, no_trade, no_trade_category, analyst_json)
                   VALUES (?, TRUE, 'data_incomplete', '{}')""", [date(2026, 9, 30)])
    con.execute("INSERT INTO runner_candidates (session_date, ticker) VALUES (?, 'R1'), (?, 'R2')",
                [date(2026, 10, 2), date(2026, 10, 2)])
    ledger.upsert_runner_session(con, {
        "session_date": date(2026, 10, 2),
        "selection_skipped_reason": "industry cap 2/industry dropped 1 (Banks: R2)"})


def test_monitor_groups_by_stamp_and_prints_counts_only(con):
    _seed(con)
    groups = persona_monitor.collect(con, None)
    assert set(groups) == {"persona off", "persona v1"}
    off, on = groups["persona off"], groups["persona v1"]
    assert off["sessions"] == 2 and off["no_trade_days"] == 1
    assert off["stand_down_reasons"] == {"outage (data_incomplete)": 1}
    assert on["sessions"] == 1 and on["daily_floor_picks"] == 1 and on["triggered"] == 1
    assert on["candidate_conviction"] == {3: 1, 2: 1} and on["probe_candidates"] == 1
    assert on["mean_size_r"] == pytest.approx(0.5)
    # Runner session 10/02 had no LLM-chosen pick, so it is not persona-influenced.
    assert off["runner_sessions"] == 1 and off["runner_cap_drops"] == 1
    text = persona_monitor.render_text(groups, None)
    _clean(text)
    assert "== persona off ==" in text and "== persona v1 ==" in text
    assert persona_monitor.LIMITATION in text


def test_monitor_with_only_unstamped_rows(con):
    ledger.save_daily_plan(con, D, _plan(), {"candidates": []}, "chop")
    groups = persona_monitor.collect(con)
    assert set(groups) == {"persona off"}
    _clean(persona_monitor.render_text(groups))


def test_monitor_survives_a_broken_analyst_json(con):
    con.execute("INSERT INTO daily_plans (plan_date, no_trade, analyst_json) VALUES (?, FALSE, 'not json')", [D])
    _clean(persona_monitor.render_text(persona_monitor.collect(con)))


def test_a_flagged_no_trade_day_that_still_traded_counts_as_taken(con):
    # Since the floor and coverage lanes, the risk manager's no_trade flag can
    # sit on a day that carried picks; that day was traded.
    ledger.save_daily_plan(con, D, _plan(no_trade=True), {"candidates": []}, "chop")
    g = persona_monitor.collect(con)["persona off"]
    assert g["taken_days"] == 1 and g["no_trade_days"] == 0
    assert g["no_trade_flag_days"] == 1 and g["stand_down_reasons"] == {}
