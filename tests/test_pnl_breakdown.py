"""Pins for the per-day / per-history P&L breakdown (agent/ledger.py
day_breakdown / pnl_breakdown_history and api/index.py /api/pnl/*).

The point of the feature is that a session's aggregate day_r is expanded back
into the picks/trades that produced it, so the dashboard can answer "what
actually made or lost money today". These tests pin that:

  * day_r reconciles exactly with equity_curve() (same SQL, same rounding),
  * each trade carries outcome / weighted_r / exit_reason / lane / runner, and
  * the two API endpoints serve the structured payload (auth-gated).

Follows tests/test_dashboard_payload.py: a real ledger on a temp DuckDB so
migrations actually run.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date

import pytest

from agent import ledger


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


def _seed(con, d, ticker, *, setup_type="gap_go", direction="long", size_r=1.0,
          triggered=True, r_planned=None, exit_reason=None, exploration_lane=None):
    con.execute(
        "INSERT INTO picks (plan_date, ticker, direction, setup_type, thesis, "
        "conviction, size_r, entry_price, stop_price, target_price, exploration_lane) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [d, ticker, direction, setup_type, "t", 3, size_r, 10.0, 9.5, 11.0, exploration_lane],
    )
    pick_id = con.execute(
        "SELECT pick_id FROM picks WHERE plan_date=? AND ticker=? ORDER BY pick_id DESC LIMIT 1",
        [d, ticker],
    ).fetchone()[0]
    con.execute(
        "INSERT INTO outcomes (pick_id, eval_date, triggered, exit_reason, r_planned) "
        "VALUES (?,?,?,?,?)",
        [pick_id, d, triggered, exit_reason, r_planned],
    )


# ------------------------------------------------------------ day_breakdown

def test_day_breakdown_reconciles_with_equity_curve(con):
    d = date(2026, 9, 20)
    _seed(con, d, "AAA", size_r=2.0, r_planned=1.0, exit_reason="target")
    _seed(con, d, "BBB", size_r=1.0, r_planned=-0.5, exit_reason="stop")
    _seed(con, d, "CCC", triggered=False)

    breakdown = ledger.day_breakdown(con, d, lane="morning")
    curve_day = next(x for x in ledger.equity_curve(con, 100000.0, 500.0, "morning")
                     if x["date"] == str(d))

    assert breakdown["day_r"] == pytest.approx(curve_day["day_r"])
    assert breakdown["day_r"] == pytest.approx(1.5)
    assert breakdown["picks"] == 3 and breakdown["triggered"] == 2
    assert breakdown["wins"] == 1 and breakdown["losses"] == 1


def test_day_breakdown_per_trade_fields(con):
    d = date(2026, 9, 21)
    _seed(con, d, "WIN", size_r=2.0, r_planned=0.6, exit_reason="target")
    _seed(con, d, "LOSS", size_r=1.0, r_planned=-0.8, exit_reason="stop")
    _seed(con, d, "SCR", size_r=1.0, r_planned=0.0, exit_reason="eod")
    _seed(con, d, "NO", triggered=False)

    by_ticker = {t["ticker"]: t for t in ledger.day_breakdown(con, d, lane="morning")["trades"]}

    assert by_ticker["WIN"]["outcome"] == "win"
    assert by_ticker["WIN"]["weighted_r"] == pytest.approx(1.2)  # 0.6R * size 2
    assert by_ticker["LOSS"]["outcome"] == "loss"
    assert by_ticker["SCR"]["outcome"] == "scratch"
    assert by_ticker["NO"]["triggered"] is False
    assert by_ticker["NO"]["outcome"] is None
    assert by_ticker["NO"]["weighted_r"] == 0.0
    assert by_ticker["WIN"]["exit_reason"] == "target"


def test_day_breakdown_runner_and_test_lane_flags(con):
    d = date(2026, 9, 22)
    _seed(con, d, "RUN", setup_type=ledger.RUNNER_SETUP, r_planned=1.0, exit_reason="target")
    _seed(con, d, "COV", setup_type="gap_go", exploration_lane="coverage",
          r_planned=0.3, exit_reason="target")
    _seed(con, d, "MORN", setup_type="gap_go", r_planned=0.2, exit_reason="target")

    by_ticker = {t["ticker"]: t for t in ledger.day_breakdown(con, d, lane=None)["trades"]}
    assert by_ticker["RUN"]["runner"] is True
    assert by_ticker["RUN"]["lane"] == "runner (retired)"
    assert by_ticker["COV"]["lane"] == "coverage"
    assert by_ticker["MORN"]["lane"] == "morning"

    # The morning desk's own record excludes the retired runner lane.
    morning_tickers = {t["ticker"] for t in ledger.day_breakdown(con, d, lane="morning")["trades"]}
    assert "RUN" not in morning_tickers and "MORN" in morning_tickers


# --------------------------------------------------------- pnl history

def test_pnl_breakdown_history_groups_and_orders(con):
    _seed(con, date(2026, 9, 20), "A1", r_planned=0.5, exit_reason="target")
    _seed(con, date(2026, 9, 21), "B1", r_planned=-0.4, exit_reason="stop")
    _seed(con, date(2026, 9, 21), "B2", r_planned=1.1, exit_reason="target")

    days = ledger.pnl_breakdown_history(con, lane="morning")
    assert [x["date"] for x in days] == ["2026-09-21", "2026-09-20"]
    assert days[0]["picks"] == 2 and days[0]["day_r"] == pytest.approx(0.7)
    assert days[1]["picks"] == 1 and days[1]["day_r"] == pytest.approx(0.5)
    assert "B1" in days[0]["summary"] and "B2" in days[0]["summary"]


# ------------------------------------------------------------------- API

def test_api_pnl_endpoints(monkeypatch, tmp_path):
    from api import index

    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "api.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    con = ledger.connect()
    _seed(con, date(2026, 9, 20), "AAA", size_r=2.0, r_planned=1.0, exit_reason="target")
    _seed(con, date(2026, 9, 21), "BBB", r_planned=-0.5, exit_reason="stop")
    con.close()

    monkeypatch.setattr(index, "CRON_SECRET", "s")
    client = index.app.test_client()
    hdr = {"Authorization": "Bearer s"}

    resp = client.get("/api/pnl/day?date=2026-09-20", headers=hdr)
    assert resp.status_code == 200
    day = resp.get_json()["day"]
    assert day["date"] == "2026-09-20" and day["day_r"] == pytest.approx(2.0)
    assert [t["ticker"] for t in day["trades"]] == ["AAA"]

    resp = client.get("/api/pnl/history", headers=hdr)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["sessions"] == 2
    assert body["total_day_r"] == pytest.approx(1.5)
    assert [x["date"] for x in body["days"]] == ["2026-09-21", "2026-09-20"]

    # Missing date / bad lane are 400s.
    assert client.get("/api/pnl/day", headers=hdr).status_code == 400
    assert client.get("/api/pnl/day?date=2026-09-20&lane=nope", headers=hdr).status_code == 400
    # Unauthenticated is blocked.
    assert client.get("/api/pnl/history").status_code != 200

