"""
Tests for agent/runner_view.py and its wiring into the local dashboard
(agent/delivery.py::render_dashboard).

Two real bugs were caught by hand while building this and are pinned here so
they cannot silently reoccur:

  1. render_dashboard computed `runner_html` but never passed it to
     `_PAGE_TEMPLATE.format(...)` -- the local static dashboard.html had been
     silently missing the Runner card entirely since it was added.
  2. The runner card was keyed off the MORNING loop's `plan_date`, not the
     runner lane's own latest session. The two loops run on independent
     schedules now (the runner lane runs from a local Task Scheduler job, the
     morning loop from whenever it's last invoked), so a stale morning
     plan_date silently pointed the runner card at a day with no runner data
     at all while the actual latest session sat unrendered.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone

import pytest

from agent import ledger, runner_view
from agent.config import Config

ET = timezone(timedelta(hours=-4))
SESSION = date(2026, 9, 17)


@pytest.fixture()
def con(monkeypatch):
    """A real ledger on a temp DuckDB file, so migrations actually run."""
    fd, path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(path)
    monkeypatch.setattr(ledger, "DB_PATH", path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()
    for p in (path, path + ".wal"):
        try:
            os.unlink(p)
        except OSError:
            pass


def _candidate(ticker, *, price=20.0, minutes=630, stop_dist=0.5,
              change_pct=4.0, session=SESSION) -> dict:
    return {
        "session_date": session, "ticker": ticker,
        "first_pass_et": datetime(session.year, session.month, session.day,
                                  minutes // 60, minutes % 60),
        "detection_minutes": minutes,
        "scan_pass_et": datetime(session.year, session.month, session.day, 11, 30),
        "detection_price": price, "vwap": price * 0.99,
        "vwap_pct_above": 1.0, "ema9": price - stop_dist, "ema9_slope": 0.03,
        "rvol": 2.0, "trend_consistency": 70.0, "change_pct": change_pct,
        "float_shares": 40.0, "earnings_days_away": 30,
        "prev_close": price * 0.96, "prev_volume": 2_000_000,
        "price_tier": "5-20" if price < 20 else "20-50",
        "stop_price": price - stop_dist,
        "target_price": price + 2 * stop_dist, "stop_dist": stop_dist,
        "gates_status": json.dumps({"float": "pass"}), "scan_version": 1,
    }


def _seed_graded_session(con, session=SESSION):
    """A session with 4 candidates, 2 selected, all graded.

    Three orders differ on purpose, so a regression to any of the wrong
    rankings fails loudly:
      - change_pct (the move ALREADY made at detection): RUNNERUP first;
      - post_move_atr (the move AFTER detection -- what the panel ranks by):
        WINNER first;
      - r_multiple (fill-based, inflated) vs r_planned (what is shown):
        WINNER's r_multiple is a fake +5.0, its planned R +1.9.
    """
    tickers = [  # ticker, r_planned, r_multiple, change_pct, post_move_atr
        ("WINNER", 1.9, 5.0, 2.0, 2.4),
        ("RUNNERUP", 0.4, 0.4, 18.0, 0.3),
        ("PICKED_LOSER", -1.0, -1.0, -6.0, -1.2),
        ("ALSO_PICKED", 0.5, 0.5, 1.5, 0.6),
    ]
    for i, (t, _, _, chg, _) in enumerate(tickers):
        ledger.save_runner_candidate(con, _candidate(
            t, price=10.0 + i, change_pct=chg, session=session))
    # Select the two that did WORST after detection, so the mismatch the
    # panel surfaces is unambiguous rather than accidentally matching.
    ledger.mark_runner_selected(con, session, "PICKED_LOSER", 1, None)
    ledger.mark_runner_selected(con, session, "ALSO_PICKED", 2, None)
    for t, rp, rm, _, mv in tickers:
        ledger.save_runner_candidate_outcome(con, {
            "session_date": session, "ticker": t, "eval_date": session,
            "triggered": True, "r_multiple": rm, "r_planned": rp,
            "exit_reason": "eod", "sim_version": 6, "post_move_atr": mv,
        })
    ledger.upsert_runner_session(con, {
        "session_date": session, "universe_size": 500, "candidates_found": 4,
        "provider_degraded": False, "scan_passes": 3,
    })


# ------------------------------------------------------ biggest_moves()

def test_biggest_moves_ranks_by_the_move_after_detection_not_change_pct(con):
    """2026-10-01: ranking by change_pct listed the most-extended names AT
    detection (LITG +16.9%, a 2x ETF that exited at -0.60R) and labelled the
    ones the selector skipped as misses. The panel ranks by what happened
    after the system could act: (close - plan price) / ATR."""
    _seed_graded_session(con)
    s = runner_view.session_summary(con, SESSION)
    moves = runner_view.biggest_moves(s, n=10)
    assert [m["ticker"] for m in moves] ==         ["WINNER", "ALSO_PICKED", "RUNNERUP", "PICKED_LOSER"]


def test_biggest_moves_excludes_rows_with_no_outcome_yet(con):
    """An after-detection move is an outcome: an ungraded row has none, so it
    is absent rather than ranked on its detection-time change."""
    ledger.save_runner_candidate(con, _candidate("UNGRADED", change_pct=99.0))
    _seed_graded_session(con)
    s = runner_view.session_summary(con, SESSION)
    assert "UNGRADED" not in [m["ticker"] for m in runner_view.biggest_moves(s, n=10)]


def test_biggest_moves_respects_n(con):
    _seed_graded_session(con)
    s = runner_view.session_summary(con, SESSION)
    assert len(runner_view.biggest_moves(s, n=2)) == 2


def test_biggest_moves_flags_selection_status_per_row(con):
    _seed_graded_session(con)
    s = runner_view.session_summary(con, SESSION)
    by_ticker = {m["ticker"]: m for m in runner_view.biggest_moves(s, n=10)}
    assert by_ticker["WINNER"]["selected"] is False
    assert by_ticker["PICKED_LOSER"]["selected"] is True
    assert by_ticker["PICKED_LOSER"]["rank"] == 1


def test_rows_show_planned_r_never_the_fill_based_r_multiple(con):
    """BALY/SEPN/FUL on 2026-10-01: a fill a few cents above a stale stop
    printed +5..7R on r_multiple. The displayed R is r_planned."""
    _seed_graded_session(con)
    s = runner_view.session_summary(con, SESSION)
    row = next(r for r in s["rows"] if r["ticker"] == "WINNER")
    assert row["r"] == 1.9
    assert "r_multiple" not in row


def test_outcome_summary_is_planned_r_with_units_and_invalid_counts(con):
    _seed_graded_session(con)
    ledger.save_runner_candidate(con, _candidate("STALE", price=30.0))
    ledger.save_runner_candidate_outcome(con, {
        "session_date": SESSION, "ticker": "STALE", "eval_date": SESSION,
        "triggered": False, "exit_reason": "invalid_fill",
        "invalid_reason": "stale_bracket", "sim_version": 6})
    o = runner_view.outcome_summary(runner_view.session_summary(con, SESSION))
    assert o["selected"]["n"] == 2 and o["selected"]["mean_r"] == pytest.approx(-0.25)
    assert o["field"]["n"] == 2 and o["field"]["mean_r"] == pytest.approx(1.15)
    assert o["field"]["invalid"] == 1          # counted, never averaged in
    assert o["delta_r"] == pytest.approx(-1.4)


# ---------------------------------------------------------- render_text

def test_render_text_shows_biggest_moves_section_with_overlap_count(con):
    _seed_graded_session(con)
    text = runner_view.render_text(con, SESSION)
    assert "BIGGEST 4 MOVES AFTER DETECTION" in text
    assert "2 of 4 were selected" in text
    assert "WINNER" in text and "not picked" in text
    assert "#1 PICKED" in text  # PICKED_LOSER, rank 1
    assert "TOP 4 MOVERS" not in text
    assert "planned-risk R" in text


def test_render_text_omits_moves_when_nothing_graded(con):
    ledger.save_runner_candidate(con, _candidate("UNGRADED"))
    ledger.upsert_runner_session(con, {"session_date": SESSION,
                                       "universe_size": 100, "candidates_found": 1})
    text = runner_view.render_text(con, SESSION)
    assert "BIGGEST" not in text


def test_render_text_reports_what_the_universe_filters_removed(con):
    _seed_graded_session(con)
    ledger.upsert_runner_session(con, {
        "session_date": SESSION,
        "universe_stats": json.dumps({"dropped_instrument": {"fund": 61, "debt": 3},
                                      "dropped_liquidity_by_source": {"movers": 4},
                                      "instruments_classified": True})})
    text = runner_view.render_text(con, SESSION)
    assert "instrument filter removed 61 fund, 3 debt" in text
    assert "liquidity floor removed 4 (movers 4)" in text


def test_render_text_health_line_distinguishes_outage_from_standdown(con):
    """Regression pin for the health-line fix: ~all no_bars must never read
    as a clean stand-down."""
    ledger.upsert_runner_session(con, {
        "session_date": SESSION, "universe_size": 500,
        "rejected_no_bars": 480, "candidates_found": 0})
    text = runner_view.render_text(con, SESSION)
    assert "DEGRADED" in text
    assert "data outage" in text


# ---------------------------------------------------------- render_html

def test_render_html_includes_biggest_moves_table(con):
    _seed_graded_session(con)
    html = runner_view.render_html(con, SESSION)
    assert "Biggest 4 moves after detection" in html
    assert "WINNER" in html
    assert "not picked" in html
    assert "picked</span>" in html  # the selected tag
    assert "movers today" not in html
    assert "5.00" not in html          # WINNER's fake fill-based +5.0R is never shown


def test_render_html_empty_when_no_session(con):
    assert runner_view.render_html(con, date(2020, 1, 1)) == ""


# ------------------------------------------- render_dashboard integration

def test_dashboard_runner_card_uses_latest_runner_session_not_stale_plan_date(con, monkeypatch, tmp_path):
    """Pins the actual bug found in this session: a daily_plans row from an
    OLD date must not make the runner card look up that old (empty) date
    instead of the runner lane's own latest session."""
    from agent import delivery
    from agent.util import ET as UTIL_ET

    old_plan_date = date(2026, 9, 1)   # morning loop last ran a while ago
    newer_runner_date = date(2026, 9, 17)   # runner lane ran since then

    con.execute(
        "INSERT INTO daily_plans (plan_date, no_trade, market_summary, plan_json) "
        "VALUES (?, FALSE, '', '{}')", [old_plan_date])
    _seed_graded_session(con, session=newer_runner_date)

    monkeypatch.setattr(delivery, "REPORTS_DIR", tmp_path)
    cfg = Config(raw={"account_size": 100000, "dollars_per_r": 500})
    result = delivery.render_dashboard(con, cfg, old_plan_date)
    # render_dashboard writes a file and returns its PATH in local (non-hosted)
    # mode -- matching what `run.py dashboard` actually does.
    html = open(result, encoding="utf-8").read()

    assert "WINNER" in html, (
        "runner card must reflect the LATEST runner session, not the stale "
        "morning plan_date")
    assert f"Runner lane — {newer_runner_date}" in html


def test_dashboard_template_actually_renders_the_runner_html(con, monkeypatch, tmp_path):
    """Pins the other bug: runner_html was computed but never threaded through
    .format(), so the card silently never appeared in dashboard.html at all."""
    from agent import delivery
    _seed_graded_session(con)
    monkeypatch.setattr(delivery, "REPORTS_DIR", tmp_path)
    cfg = Config(raw={"account_size": 100000, "dollars_per_r": 500})
    result = delivery.render_dashboard(con, cfg, None)
    html = open(result, encoding="utf-8").read()
    assert "Runner lane" in html
    assert "{runner_html}" not in html  # the placeholder must be substituted
