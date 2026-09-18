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
              session=SESSION) -> dict:
    return {
        "session_date": session, "ticker": ticker,
        "first_pass_et": datetime(session.year, session.month, session.day,
                                  minutes // 60, minutes % 60),
        "detection_minutes": minutes,
        "scan_pass_et": datetime(session.year, session.month, session.day, 11, 30),
        "detection_price": price, "vwap": price * 0.99,
        "vwap_pct_above": 1.0, "ema9": price - stop_dist, "ema9_slope": 0.03,
        "rvol": 2.0, "trend_consistency": 70.0, "change_pct": 4.0,
        "float_shares": 40.0, "earnings_days_away": 30,
        "prev_close": price * 0.96, "prev_volume": 2_000_000,
        "price_tier": "5-20" if price < 20 else "20-50",
        "stop_price": price - stop_dist,
        "target_price": price + 2 * stop_dist, "stop_dist": stop_dist,
        "gates_status": json.dumps({"float": "pass"}), "scan_version": 1,
    }


def _seed_graded_session(con, session=SESSION):
    """A session with 4 candidates, 2 selected, all graded with distinct R --
    the shape needed to exercise both the main table and top_movers()."""
    tickers_r = [("WINNER", 5.0), ("RUNNERUP", 2.0), ("PICKED_LOSER", -1.0),
                ("ALSO_PICKED", 0.5)]
    for i, (t, _) in enumerate(tickers_r):
        ledger.save_runner_candidate(con, _candidate(t, price=10.0 + i, session=session))
    # Select the two that did WORST, so the mismatch top_movers surfaces is
    # unambiguous rather than accidentally matching.
    ledger.mark_runner_selected(con, session, "PICKED_LOSER", 1, None)
    ledger.mark_runner_selected(con, session, "ALSO_PICKED", 2, None)
    for t, r in tickers_r:
        ledger.save_runner_candidate_outcome(con, {
            "session_date": session, "ticker": t, "eval_date": session,
            "triggered": True, "r_multiple": r, "exit_reason": "eod",
            "sim_version": 3,
        })
    ledger.upsert_runner_session(con, {
        "session_date": session, "universe_size": 500, "candidates_found": 4,
        "provider_degraded": False, "scan_passes": 3,
    })


# --------------------------------------------------------- top_movers()

def test_top_movers_ranks_by_realized_r_descending(con):
    _seed_graded_session(con)
    s = runner_view.session_summary(con, SESSION)
    movers = runner_view.top_movers(s, n=10)
    assert [m["ticker"] for m in movers] == \
        ["WINNER", "RUNNERUP", "ALSO_PICKED", "PICKED_LOSER"]


def test_top_movers_excludes_ungraded_rows(con):
    ledger.save_runner_candidate(con, _candidate("UNGRADED"))
    _seed_graded_session(con)
    s = runner_view.session_summary(con, SESSION)
    movers = runner_view.top_movers(s, n=10)
    assert "UNGRADED" not in [m["ticker"] for m in movers]


def test_top_movers_respects_n(con):
    _seed_graded_session(con)
    s = runner_view.session_summary(con, SESSION)
    assert len(runner_view.top_movers(s, n=2)) == 2


def test_top_movers_flags_selection_status_per_row(con):
    """The exact mismatch this view exists to surface: the day's best mover
    was never picked, while a genuinely bad pick was."""
    _seed_graded_session(con)
    s = runner_view.session_summary(con, SESSION)
    by_ticker = {m["ticker"]: m for m in runner_view.top_movers(s, n=10)}
    assert by_ticker["WINNER"]["selected"] is False
    assert by_ticker["PICKED_LOSER"]["selected"] is True
    assert by_ticker["PICKED_LOSER"]["rank"] == 1


# ---------------------------------------------------------- render_text

def test_render_text_shows_top_movers_section_with_overlap_count(con):
    _seed_graded_session(con)
    text = runner_view.render_text(con, SESSION)
    assert "TOP 4 MOVERS TODAY" in text
    # 2 of the 4 movers shown were selected (PICKED_LOSER, ALSO_PICKED).
    assert "2 of 4 were selected" in text
    assert "WINNER" in text and "not picked" in text
    assert "#1 PICKED" in text  # PICKED_LOSER, rank 1


def test_render_text_omits_top_movers_when_nothing_graded(con):
    ledger.save_runner_candidate(con, _candidate("UNGRADED"))
    ledger.upsert_runner_session(con, {"session_date": SESSION,
                                       "universe_size": 100, "candidates_found": 1})
    text = runner_view.render_text(con, SESSION)
    assert "TOP" not in text or "MOVERS" not in text


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

def test_render_html_includes_top_movers_table(con):
    _seed_graded_session(con)
    html = runner_view.render_html(con, SESSION)
    assert "Top 4 movers today" in html
    assert "WINNER" in html
    assert "not picked" in html
    assert "picked</span>" in html  # the selected tag


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
