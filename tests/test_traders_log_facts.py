"""TRADERS_LOG_SPEC_v1 Phase 1 -- the schema and the facts builder."""
from __future__ import annotations

import json
from datetime import date

import pytest

from agent import ledger, migrations_v30, traders_log as tl
from _traders_log_seed import D, PLAYBOOK, playbook, seed_session


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "ledger.duckdb")
    playbook(tmp_path, monkeypatch)
    c = ledger.connect()
    yield c
    c.close()


def _v(facts, name):
    return facts["slots"][name]["value"]


# ----------------------------------------------------------------- migration

def test_v30_table_and_portability(con):
    cols = [r[0] for r in con.execute("DESCRIBE traders_log").fetchall()]
    assert cols == ["session_date", "lane", "generated_at", "persona_version", "model", "status",
                    "facts_json", "raw_text", "rendered_text", "violations_json", "tokens_in",
                    "tokens_out", "cost_usd"]
    for s in migrations_v30.STATEMENTS:
        assert "nextval" not in s.lower() and "DOUBLE PRECISION" not in s.upper()
        assert "CREATE TABLE IF NOT EXISTS" in s
    assert all(s in ledger._MIGRATIONS for s in migrations_v30.STATEMENTS)


# -------------------------------------------------------------- facts builder

def test_slot_families_scopes_and_sources(con):
    seed_session(con)
    facts = tl.build_facts(con, D)
    assert facts["slots"] and not facts["errors"]
    for name, s in facts["slots"].items():
        assert name.split(".")[0] in tl.FAMILIES, name
        assert s["scope"] in tl.SCOPES and s["source"] and s["label"] and s["name"] == name
        if s["scope"] != "computed_verdict":
            text = (name + " " + s["value"]).lower()
            for bad in ("p =", "q =", "mean", "significant"):
                assert bad not in text, (name, bad)


def test_day_facts_by_planned_r_and_fill_validity(con):
    seed_session(con)
    f = tl.build_facts(con, D)
    assert _v(f, "day.picks_morning") == "5" and _v(f, "day.picks_runner") == "1"
    # AAA +1.2, BBB -1.0, RUN -0.4 are fill-valid; DDD is an execution anomaly.
    assert _v(f, "day.triggered") == "3"
    assert _v(f, "day.wins") == "1" and _v(f, "day.losses") == "2"
    assert _v(f, "day.net_r") == "-0.20R"            # r_planned, never r_multiple
    assert _v(f, "day.execution_anomalies") == "1"
    assert _v(f, "day.exits") == "eod 1, stop 1, target 1"
    assert _v(f, "day.untriggered") == "1"
    assert "AAA long gap_go" in _v(f, "pick.morning.AAA") and "+1.20R (target)" in _v(f, "pick.morning.AAA")
    assert "(coverage)" in _v(f, "pick.morning.BBB")
    assert "excluded" in _v(f, "pick.morning.DDD")
    assert "day.stand_down_reason" not in f["slots"]


def test_invalid_fills_are_counted_never_narrated(con):
    seed_session(con)
    f = tl.build_facts(con, D)
    assert _v(f, "day.invalid_fills") == "1"
    for name, s in f["slots"].items():
        if name.startswith("pick."):
            assert "CCC" not in name and "CCC" not in s["value"]


def test_shadow_runner_and_exit_replays(con):
    seed_session(con)
    f = tl.build_facts(con, D)
    assert _v(f, "shadow.scored") == "2" and _v(f, "shadow.forgone_r") == "-0.20R"
    assert _v(f, "runner.candidates") == "3" and _v(f, "runner.selected") == "1"
    assert _v(f, "runner.industry_cap_drops") == "1"
    assert _v(f, "runner.exit.plan") == ("plan exits (the selected plan's own stop and 2R target): "
                                         "+0.50R per trade over 2 replayed trades, 50% closed positive")
    assert _v(f, "runner.exit.arm_a_floor").startswith(
        "arm A, stop floor (stop no tighter than 1 ATR): +0.25R per trade over 2")
    assert "runner.exit.arm_b_dashboard" not in f["slots"]      # no replays, no slot
    # Every shadow row in the seed is priced, and the selection is explained.
    assert _v(f, "shadow.priced") == "2" and _v(f, "shadow.forgone_per_rejection") == "-0.10R"
    assert _v(f, "runner.selection").startswith("selected 1")


def test_deficits_and_menu_are_the_morning_list(con):
    seed_session(con)
    f = tl.build_facts(con, D)
    assert _v(f, "deficit.morning_stop_atr_bucket_0_5_1") == \
        "morning:stop_atr_bucket=0.5-1 (cell): 3/8, ~4 sessions at current pace"
    assert _v(f, "deficit.Q3") == "Q3 (question): 5/8 — STALLED (nothing in 10 sessions)"
    assert f["menu"] == {"menu.morning_stop_atr_bucket_0_5_1": "morning:stop_atr_bucket=0.5-1",
                         "menu.Q3": "Q3"}
    # Round-robin by type (rule, question, cell), exactly as the morning sees it.
    assert f["menu_item_ids"] == ["Q3", "morning:stop_atr_bucket=0.5-1", "no change"]
    assert all(f["slots"][n]["scope"] == "count" for n in f["menu"])


def test_calibration_cells_under_n8_are_absent(con):
    seed_session(con)
    f = tl.build_facts(con, D)
    assert "calibration.conviction_3" in f["slots"]
    assert f["slots"]["calibration.conviction_3"]["scope"] == "cumulative"
    assert not any(n.startswith("calibration.conviction_5") for n in f["slots"])


def test_verdicts_are_copied_verbatim(con):
    seed_session(con)
    f = tl.build_facts(con, D)
    assert _v(f, "verdict.calibration_q3") == \
        "Calibration verdict (computed, Q3): not yet computable — conviction 4-5 has n=3 of 8."
    assert _v(f, "verdict.runner_lane").startswith("Lane stop condition: 1/20 sessions measured")
    assert f["slots"]["verdict.runner_lane"]["scope"] == "computed_verdict"


def test_empty_ledger_is_empty_facts(con):
    f = tl.build_facts(con, D)
    assert f["slots"] == {} and f["menu_item_ids"] == ["no change"]


def test_outage_day_reports_what_was_stored(con):
    con.execute("""INSERT INTO daily_plans (plan_date, no_trade, no_trade_category, session_degraded)
                   VALUES (?, TRUE, 'data_incomplete', TRUE)""", [D])
    f = tl.build_facts(con, D)
    assert _v(f, "day.stand_down_reason").startswith("data outage")


def test_no_trade_day_with_a_genuine_category(con):
    con.execute("""INSERT INTO daily_plans (plan_date, no_trade, no_trade_category)
                   VALUES (?, TRUE, 'all_failed_rr')""", [D])
    f = tl.build_facts(con, D)
    assert _v(f, "day.stand_down_reason") == "stand-down recorded by the morning loop (all_failed_rr)"


def test_runner_only_day_without_a_morning_plan(con):
    from datetime import datetime
    con.execute("INSERT INTO runner_candidates (session_date, ticker) VALUES (?, 'R1')", [D])
    f = tl.build_facts(con, D)
    assert _v(f, "runner.candidates") == "1"
    assert "not distinguished by the system" in _v(f, "day.stand_down_reason")
    ledger.log_run(con, "morning", D, datetime(2026, 10, 2, 6, 45), "api_error")
    f = tl.build_facts(con, D)
    assert _v(f, "day.stand_down_reason") == "no morning plan was produced (morning run status: api_error)"


def test_a_failing_family_costs_only_itself(con, monkeypatch):
    seed_session(con)
    monkeypatch.setattr(tl, "_runner_facts", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    f = tl.build_facts(con, D)
    assert any("boom" in e for e in f["errors"])
    assert "day.wins" in f["slots"] and not any(n.startswith("runner.") for n in f["slots"])


# ---------------------------------------------------------- facts-only render

def test_render_facts_only_is_deterministic_and_labeled(con):
    seed_session(con)
    f = tl.build_facts(con, D)
    a, b = tl.render_facts_only(f), tl.render_facts_only(json.loads(json.dumps(f)))
    assert a == b
    assert a.startswith("FACTS ONLY (no narrative)")
    # A one-session group names its scope in its title, not on every line.
    assert "What happened (one session, not evidence):" in a and "(one session)" not in a
    assert "(count)" in a and "(cumulative)" in a
    assert '"Calibration verdict (computed, Q3)' in a and "(computed verdict)" in a
    assert tl.render_facts_only({"slots": {}}) == "FACTS ONLY (no narrative)\n(no facts for this session)"


def test_a_trade_without_planned_r_is_unmeasured_not_backfilled(con):
    ids = seed_session(con)
    con.execute("UPDATE outcomes SET r_planned = NULL WHERE pick_id = ?", [ids["AAA"]])
    f = tl.build_facts(con, D)
    assert _v(f, "day.wins") == "0" and _v(f, "day.unmeasured_r") == "1"
    assert _v(f, "day.net_r") == "-1.40R"            # BBB -1.0 + RUN -0.4; AAA's r_multiple unused
