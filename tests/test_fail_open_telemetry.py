"""
Phase 1f (Addendum A §A4) — fail-open telemetry.

_grounded_atr/_grounded_earnings/price-grounding all fail OPEN by design on a
data-provider outage (a data outage must not silently kill a trading day),
but nothing previously recorded that a gate was skipped. A pick from a
degraded session sat in the ledger indistinguishable from one that cleared
every gate. This pins the structural counter (gates_failed_open JSON),
provider_degraded, and the round-trip through daily_plans.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from agent import ledger
from agent.loops.morning import _validate_plan
from agent.config import Config


def _cfg(**overrides) -> Config:
    raw = {
        "max_picks": 4, "min_size_r": 0.25, "max_size_r": 1.5,
        "max_total_open_risk_r": 3.0, "drawdown_pause_r": -4.0,
    }
    raw.update(overrides)
    return Config(raw=raw)


def _pick(ticker="AAAA", entry=100.0, stop=98.0, target=104.0):
    return {
        "ticker": ticker, "direction": "long",
        "entry_plan": {"type": "open", "price": entry},
        "entry_price": entry, "stop_price": stop, "target_price": target,
        "size_r": 0.5, "conviction": 4,
    }


def _plan(picks):
    return {"no_trade": False, "picks": picks}


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "test.duckdb")
    c = ledger.connect()
    yield c
    c.close()


# ---------------------------------------------------- provider_degraded threshold

def test_every_ticker_missing_atr_sets_provider_degraded():
    picks = [_pick(f"T{c}") for c in "ABCD"]
    ref = {p["ticker"]: 100.0 for p in picks}
    out, _ = _validate_plan(_plan(picks), _cfg(), ref_prices=ref, atr_map={})
    assert out["provider_degraded"] is True
    assert json.loads(out["gates_failed_open"])["atr"] == 4


def test_one_of_four_missing_atr_does_not_set_provider_degraded():
    picks = [_pick(f"T{c}") for c in "ABCD"]
    ref = {p["ticker"]: 100.0 for p in picks}
    atr_map = {"TA": 1.0, "TB": 1.0, "TC": 1.0}  # only TD missing -> 1/4
    earn_map = {p["ticker"]: 10 for p in picks}  # fully covered -- isolate the ATR gate
    out, _ = _validate_plan(_plan(picks), _cfg(), ref_prices=ref, atr_map=atr_map,
                            earnings_map=earn_map)
    assert out["provider_degraded"] is False
    assert json.loads(out["gates_failed_open"])["atr"] == 1


# --------------------------------------------------------------- round-trip

def test_gates_failed_open_round_trips_through_daily_plans(con):
    d = date(2026, 8, 27)
    picks = [_pick(f"T{c}") for c in "ABCD"]
    ref = {p["ticker"]: 100.0 for p in picks}
    plan, _ = _validate_plan(_plan(picks), _cfg(), ref_prices=ref, atr_map={})
    plan["regime_tag"], plan["prompt_version"], plan["playbook_version"] = "chop", "v1", "1"

    ledger.save_daily_plan(con, d, plan, {"candidates": []}, "chop")

    row = con.execute(
        "SELECT gates_failed_open, provider_degraded FROM daily_plans WHERE plan_date = ?", [d]
    ).fetchone()
    assert row is not None
    counts = json.loads(row[0])
    assert counts["atr"] == 4
    assert row[1] is True


def test_clean_session_round_trips_provider_degraded_false(con):
    d = date(2026, 8, 28)
    picks = [_pick("AAAA")]
    plan, _ = _validate_plan(_plan(picks), _cfg(), ref_prices={"AAAA": 100.0},
                             atr_map={"AAAA": 1.0}, earnings_map={"AAAA": 10})
    ledger.save_daily_plan(con, d, plan, {"candidates": []}, "chop")
    row = con.execute(
        "SELECT gates_failed_open, provider_degraded FROM daily_plans WHERE plan_date = ?", [d]
    ).fetchone()
    assert json.loads(row[0]) == {"atr": 0, "earnings": 0, "grounding": 0}
    assert row[1] is False


def test_candidate_flow_and_rejection_provenance_round_trip(con):
    d = date(2026, 8, 29)
    plan = {
        "no_trade": True,
        "analyst_candidate_count": 8,
        "risk_rejection_count": 8,
        "validator_proposed_count": 0,
        "validator_drop_count": 0,
        "persisted_rejection_count": 1,
        "gates_failed_open": '{"atr": 0, "earnings": 0, "grounding": 0}',
        "provider_degraded": False,
        "picks": [],
    }
    ledger.save_daily_plan(con, d, plan, {"candidates": []}, "chop")
    ledger.save_rejections(con, d, [{
        "ticker": "MARA", "direction": "long", "setup_type": "gap_go",
        "reason": "R:R 1.83 < 2.0", "entry_type": "limit",
        "entry_price": 11.5, "stop_price": 10.9, "target_price": 12.6,
        "source_stage": "risk_rejected", "rejection_category": "rr_below_control",
        "planned_rr": 1.8333,
    }])
    assert con.execute(
        "SELECT analyst_candidate_count, risk_rejection_count, persisted_rejection_count "
        "FROM daily_plans WHERE plan_date = ?", [d]).fetchone() == (8, 8, 1)
    assert con.execute(
        "SELECT source_stage, rejection_category, planned_rr FROM rejections "
        "WHERE plan_date = ?", [d]).fetchone() == ("risk_rejected", "rr_below_control", 1.8333)


# ------------------------------------------------------------- weekly render

def test_data_quality_md_lists_only_degraded_sessions(con):
    from agent.loops.weekly import _data_quality_md

    con.execute(
        "INSERT INTO daily_plans (plan_date, no_trade, gates_failed_open, provider_degraded) "
        "VALUES (?, FALSE, ?, TRUE)",
        [date(2026, 8, 20), json.dumps({"atr": 3, "earnings": 0, "grounding": 0})],
    )
    con.execute(
        "INSERT INTO daily_plans (plan_date, no_trade, gates_failed_open, provider_degraded) "
        "VALUES (?, FALSE, ?, FALSE)",
        [date(2026, 8, 21), json.dumps({"atr": 0, "earnings": 0, "grounding": 0})],
    )
    md = _data_quality_md(con)
    assert "2026-08-20" in md
    assert "2026-08-21" not in md
    assert "atr=3" in md


def test_data_quality_md_empty_when_no_degraded_sessions(con):
    from agent.loops.weekly import _data_quality_md

    con.execute(
        "INSERT INTO daily_plans (plan_date, no_trade, gates_failed_open, provider_degraded) "
        "VALUES (?, FALSE, ?, FALSE)",
        [date(2026, 8, 21), json.dumps({"atr": 0, "earnings": 0, "grounding": 0})],
    )
    assert _data_quality_md(con) == ""
