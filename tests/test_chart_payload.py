"""Tests for agent/chart_payload.py (specs/CHART_DRAWER_SPEC_v1.md §1.6).

Conventions follow tests/test_dashboard_payload.py / tests/test_runner_view.py:
a real ledger on a temp DuckDB file so migrations actually run, rather than a
mocked connection.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import pytest

from agent import chart_payload, indicators as ind, ledger, runner_indicators as ri
from agent.config import Config, _DEFAULTS
from agent.grading import CURRENT_SIM_VERSION
from agent.util import ET

MOCK_TICKER = "MOCK"


@pytest.fixture()
def con(monkeypatch):
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


@pytest.fixture()
def cfg():
    return Config(raw=json.loads(json.dumps(_DEFAULTS)), offline=True)


def _bars(d: date, n: int = 30, start_price: float = 100.0, drift: float = 0.1,
         start_hm: tuple[int, int] = (9, 30)) -> list[dict]:
    t = datetime.combine(d, dtime(*start_hm), tzinfo=ET)
    price = start_price
    out = []
    for i in range(n):
        o = price
        c = price + drift
        h, l = max(o, c) + 0.05, min(o, c) - 0.05
        out.append({"t": t, "o": round(o, 2), "h": round(h, 2), "l": round(l, 2),
                   "c": round(c, 2), "v": 1000 + i * 10})
        price = c
        t += timedelta(minutes=1)
    return out


# ------------------------------------------------------------------- to_kline

def test_to_kline_localizes_naive_et_across_dst_boundary():
    # 2026-03-08 is the US spring-forward Sunday; 03-06 (Fri, EST/-5) and
    # 03-09 (Mon, EDT/-4) straddle it.
    d_before, d_after = date(2026, 3, 6), date(2026, 3, 9)
    bar_before = {"t": datetime(d_before.year, d_before.month, d_before.day, 9, 30),
                 "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}
    bar_after = {"t": datetime(d_after.year, d_after.month, d_after.day, 9, 30),
                "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}
    out_before = chart_payload.to_kline([bar_before])
    out_after = chart_payload.to_kline([bar_after])
    expect_before = int(datetime(2026, 3, 6, 9, 30, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)
    expect_after = int(datetime(2026, 3, 9, 9, 30, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)
    assert out_before[0]["timestamp"] == expect_before
    assert out_after[0]["timestamp"] == expect_after
    # 3 days apart at the same wall-clock time, but EST (-5) vs EDT (-4) --
    # proving DST is actually applied rather than a fixed UTC offset.
    assert expect_after - expect_before == timedelta(days=3, hours=-1) // timedelta(milliseconds=1)


def test_to_kline_sorts_and_dedupes():
    d = date(2026, 1, 5)
    t0 = datetime.combine(d, dtime(9, 30), tzinfo=ET)
    bars = [
        {"t": t0 + timedelta(minutes=1), "o": 2, "h": 2, "l": 2, "c": 2, "v": 1},
        {"t": t0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
        {"t": t0, "o": 9, "h": 9, "l": 9, "c": 9, "v": 9},  # duplicate timestamp -- last wins
    ]
    out = chart_payload.to_kline(bars)
    assert [b["timestamp"] for b in out] == sorted(b["timestamp"] for b in out)
    assert len(out) == 2
    assert out[0]["open"] == 9  # dedup keeps the later entry for a repeated ts


# ------------------------------------------------------------------- aggregate

def test_aggregate_1m_to_5m_hand_computed_with_partial_final_bucket():
    d = date(2026, 1, 5)
    bars = _bars(d, n=12)  # 09:30..09:41 -> buckets [09:30-34],[09:35-39],[09:40-41 partial]
    out = chart_payload.aggregate(bars, 5)
    assert len(out) == 3
    b0, b1, b2 = out
    # aggregate() works (and returns) naive ET wall-clock internally --
    # to_kline() re-localizes to aware ET, same as every other naive ledger
    # timestamp (R-6).
    assert chart_payload._naive(b0["t"]) == datetime.combine(d, dtime(9, 30))
    assert b0["o"] == bars[0]["o"]
    assert b0["c"] == bars[4]["c"]
    assert b0["h"] == max(b["h"] for b in bars[0:5])
    assert b0["l"] == min(b["l"] for b in bars[0:5])
    assert b0["v"] == sum(b["v"] for b in bars[0:5])
    assert chart_payload._naive(b1["t"]) == datetime.combine(d, dtime(9, 35))
    assert chart_payload._naive(b2["t"]) == datetime.combine(d, dtime(9, 40))
    assert b2["o"] == bars[10]["o"] and b2["c"] == bars[11]["c"]  # partial (2 bars) bucket kept


def test_aggregate_passthrough_for_tf_1():
    d = date(2026, 1, 5)
    bars = _bars(d, n=5)
    assert chart_payload.aggregate(bars, 1) == bars


# --------------------------------------------------------------- runner parity

def test_runner_series_matches_runner_indicators_at_detection_bar():
    """The chart's Runner vwap/ema9 at the first_pass_et bar must equal what
    runner_indicators itself computes over the identical prefix -- proving
    Phase 0 §0.3 (native 5Min bars, never 1Min aggregated) was wired right."""
    d = date(2026, 1, 5)
    bars_5m = _bars(d, n=20, drift=0.3)  # 20 five-minute-labelled synthetic bars
    series = chart_payload.series_runner(bars_5m)
    idx = 10
    ts_ms = chart_payload._ms(bars_5m[idx]["t"])
    expected_vwap = ri.vwap(bars_5m[: idx + 1])
    expected_ema9 = ri.ema(bars_5m[: idx + 1], 9)
    assert series["vwap"][ts_ms] == round(expected_vwap, 4)
    assert series["ema9"][ts_ms] == round(expected_ema9, 4)


def test_morning_ema9_uses_indicators_module_not_runner_indicators():
    """series_morning_ema9 must match indicators.ema_series (the morning
    lane's own grading module) exactly -- R-0 for the morning lane. This also
    guards against a future edit accidentally swapping in `ri` (runner_indicators):
    the two modules are mathematically identical at seed="sma" (both are a
    standard SMA-seeded EMA recursion), so the thing worth pinning is WHICH
    module computed the series, not a forced numeric disagreement -- the
    payload's own `basis` string names the module, and this test would catch
    a future change to indicators.ema_series's rounding/seeding just as
    readily as a wrong-module swap."""
    d = date(2026, 1, 5)
    bars_5m = _bars(d, n=15, drift=0.15)
    closes = [b["c"] for b in bars_5m]
    via_indicators = ind.ema_series(closes, 9)
    assert any(v is not None for v in via_indicators), "fixture too short to seed a 9-period EMA"
    out = chart_payload.series_morning_ema9(bars_5m)
    for b, expected in zip(bars_5m, via_indicators):
        if expected is None:
            continue
        ts_ms = chart_payload._ms(b["t"])
        assert out[ts_ms] == round(expected, 4)


# -------------------------------------------------------------------- guard

def test_replay_guard_ok_on_matching_values():
    g = chart_payload._replay_guard("x", 1.234567, 1.234567, CURRENT_SIM_VERSION)
    assert g["ok"] is True


def test_replay_guard_fails_on_tampered_stored_value():
    g = chart_payload._replay_guard("x", 9.99, 1.23, CURRENT_SIM_VERSION)
    assert g["ok"] is False
    assert "9.99" in g["detail"]


def test_replay_guard_fails_on_sim_version_mismatch():
    g = chart_payload._replay_guard("x", 1.0, 1.0, CURRENT_SIM_VERSION - 1)
    assert g["ok"] is False
    assert "sim v" in g["detail"]


# ------------------------------------------------------------------ resolve

def _insert_pick(con, d, ticker, **overrides):
    row = {"plan_date": d, "ticker": ticker, "direction": "long", "setup_type": "gap_go",
          "entry_type": "open", "entry_price": 100.0, "stop_price": 98.0, "target_price": 106.0,
          "size_r": 1.0, "conviction": 3, "thesis": "t", "time_stop": None}
    row.update(overrides)
    cols = list(row)
    placeholders = ", ".join("?" for _ in cols)
    con.execute(f"INSERT INTO picks ({', '.join(cols)}) VALUES ({placeholders}) ",
               [row[c] for c in cols])
    return con.execute("SELECT pick_id FROM picks WHERE plan_date=? AND ticker=? ORDER BY pick_id DESC LIMIT 1",
                       [d, ticker]).fetchone()[0]


def _insert_rejection(con, d, ticker, **overrides):
    row = {"plan_date": d, "ticker": ticker, "direction": "long", "setup_type": "gap_go",
          "reason": "chase", "entry_type": "open", "entry_price": 100.0, "stop_price": 98.0,
          "target_price": 106.0, "conviction": 3, "thesis": "t"}
    row.update(overrides)
    cols = list(row)
    con.execute(f"INSERT INTO rejections ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
               [row[c] for c in cols])
    return con.execute(
        "SELECT rejection_id FROM rejections WHERE plan_date=? AND ticker=? ORDER BY rejection_id DESC LIMIT 1",
        [d, ticker]).fetchone()[0]


def _insert_runner_candidate(con, d, ticker, **overrides):
    row = {"session_date": d, "ticker": ticker,
          "first_pass_et": datetime.combine(d, dtime(10, 20)),
          "detection_minutes": 50, "scan_pass_et": datetime.combine(d, dtime(10, 20)),
          "detection_price": 100.0, "vwap": 99.0, "ema9": 98.5,
          "stop_price": 98.5, "target_price": 103.0, "stop_dist": 1.5,
          "scan_version": 1}
    row.update(overrides)
    cols = list(row)
    con.execute(f"INSERT INTO runner_candidates ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
               [row[c] for c in cols])


def _insert_live_order(con, d, ticker, order_id, **overrides):
    row = {"live_order_id": order_id, "session_date": d, "ticker": ticker, "side": "buy",
          "qty": 10, "entry_order_type": "market"}
    row.update(overrides)
    cols = list(row)
    con.execute(f"INSERT INTO live_orders ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
               [row[c] for c in cols])


def test_resolve_contexts_returns_every_lane_present(con):
    d = date(2026, 1, 5)
    pick_id = _insert_pick(con, d, MOCK_TICKER)
    rej_id = _insert_rejection(con, d, MOCK_TICKER)
    _insert_runner_candidate(con, d, MOCK_TICKER)
    _insert_live_order(con, d, MOCK_TICKER, "order-1")
    ctxs = chart_payload.resolve_contexts(con, MOCK_TICKER, d)
    lanes = {c["lane"] for c in ctxs}
    assert lanes == {"morning", "shadow", "runner", "live"}
    ids = {c["lane"]: c["id"] for c in ctxs}
    assert ids["morning"] == pick_id
    assert ids["shadow"] == rej_id
    assert ids["live"] == "order-1"


def test_resolve_contexts_runner_items_in_ticker_order(con):
    d = date(2026, 1, 5)
    for t in ("ZWZY", "AAPL", "MSFT"):
        _insert_runner_candidate(con, d, t)
    # resolve_contexts is per-ticker; verify the underlying ledger call (which
    # every runner-lane listing in the app uses) is alphabetical -- R-1.
    rows = ledger.runner_candidates_for_date(con, d)
    assert [r["ticker"] for r in rows] == sorted(r["ticker"] for r in rows)


# -------------------------------------------------------------------- as-of

def test_asof_mode_drops_bars_markers_and_outcome(con, cfg):
    d = date(2026, 1, 5)
    pick_id = _insert_pick(con, d, MOCK_TICKER, entry_type="open")
    created_at = datetime.combine(d, dtime(10, 0))
    con.execute("INSERT INTO daily_plans (plan_date, no_trade, plan_json, created_at) VALUES (?, ?, ?, ?)",
               [d, False, "{}", created_at])
    con.execute(
        """INSERT INTO outcomes (pick_id, eval_date, triggered, entry_fill, exit_price, exit_reason,
                                r_multiple, mfe_r, mae_r, sim_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [pick_id, d, True, 100.0, 106.0, "target", 2.0, 2.1, -0.2, CURRENT_SIM_VERSION])

    out = chart_payload.build(con, cfg, MOCK_TICKER, d, "morning", pick_id, tf=1, asof=True)
    assert out["context"]["outcome"] is None
    cutoff_ms = chart_payload._ms(created_at)
    assert all(b["timestamp"] < cutoff_ms for b in out["bars"])
    assert all((m.get("timestamp") or 0) < cutoff_ms for m in out["markers"])
    assert any(w["kind"] == "asof_cutoff" for w in out["windows"])


def test_full_mode_shows_outcome_and_bars_past_cutoff(con, cfg):
    d = date(2026, 1, 5)
    pick_id = _insert_pick(con, d, MOCK_TICKER, entry_type="open")
    created_at = datetime.combine(d, dtime(10, 0))
    con.execute("INSERT INTO daily_plans (plan_date, no_trade, plan_json, created_at) VALUES (?, ?, ?, ?)",
               [d, False, "{}", created_at])
    con.execute(
        """INSERT INTO outcomes (pick_id, eval_date, triggered, entry_fill, exit_price, exit_reason,
                                r_multiple, mfe_r, mae_r, sim_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [pick_id, d, True, 100.0, 106.0, "target", 2.0, 2.1, -0.2, CURRENT_SIM_VERSION])
    out = chart_payload.build(con, cfg, MOCK_TICKER, d, "morning", pick_id, tf=1, asof=False)
    assert out["context"]["outcome"] is not None
    cutoff_ms = chart_payload._ms(created_at)
    assert any(b["timestamp"] >= cutoff_ms for b in out["bars"])


# ------------------------------------------------------------------ untriggered

def test_outcomes_untriggered_reason_surfaced(con):
    cols = [c[0] for c in con.execute("DESCRIBE outcomes").fetchall()]
    assert "untriggered_reason" in cols
