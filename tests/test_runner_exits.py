"""
Runner exit-policy replay (specs/RUNNER_EXIT_SPEC_v1.md).

Pins the properties the ΔR comparison depends on:
  - the `plan` arm reproduces simulate_pick's r_planned exactly (otherwise
    every ΔR measures engine drift, not an exit)
  - each alternative does what its question says, with no intra-bar look-ahead
    (breakeven never protects the bar that armed it; a trail exits at the NEXT
    bar's open after a completed 5m close)
  - time stops a trade could not have followed yield no row
  - the statistical unit is the session, and every policy is a counted look
  - the evening loop stores replays and, finally, r_planned
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from agent import grading, ledger, runner_exits
from agent.config import Config

ET = timezone(timedelta(hours=-4))
SESSION = date(2026, 9, 16)      # a Wednesday, full session
HALF_DAY = date(2026, 11, 27)    # day after Thanksgiving, 13:00 close
ENTRY, STOP, TARGET = 10.0, 9.5, 11.0   # risk 0.5, target 2R


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "t.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


def _bars(prices, d: date = SESSION, start=time(9, 30)):
    """1-minute bars from `start`; a float p is a tight bar around p, a tuple is
    explicit (o, h, l, c)."""
    t0 = datetime.combine(d, start, tzinfo=ET)
    out = []
    for i, p in enumerate(prices):
        o, h, l, c = p if isinstance(p, tuple) else (p, p + 0.01, p - 0.01, p)
        out.append({"t": t0 + timedelta(minutes=i), "o": o, "h": h, "l": l, "c": c,
                    "v": 10_000})
    return out


def _rise(n_before=60, to_i=90, step=0.01):
    """Steady rise, exactly ENTRY at index n_before (10:30), so the fill bar
    opens at the planned entry and every 5m close sits above its lagging EMA."""
    return [round(ENTRY + (i - n_before) * step, 4) for i in range(to_i)]


def _replay(bars, fill_idx=60, **kw):
    kw.setdefault("session_date", SESSION)
    return {r["policy"]: r for r in runner_exits.replay(bars, fill_idx, ENTRY, STOP, **kw)}


def _sim(bars, fill_idx=60, **kw):
    shim = {"pick_id": 0, "direction": "long", "entry_type": "open",
            "entry_price": ENTRY, "stop_price": STOP, "target_price": TARGET}
    return grading.simulate_pick(shim, bars[fill_idx:], None, session_date=SESSION, **kw)


# ================================================================ parity

_PATHS = {
    "stop": _rise() + [9.8, 9.6, (9.55, 9.56, 9.45, 9.5)] + [9.5] * 300,
    "target": _rise() + [round(10.3 + 0.02 * i, 4) for i in range(60)] + [11.2] * 240,
    "eod": _rise() + [10.3] * 300,
    "gap_through_stop": _rise() + [10.2, (9.3, 9.35, 9.2, 9.3)] + [9.3] * 298,
}


@pytest.mark.parametrize("path", sorted(_PATHS))
@pytest.mark.parametrize("slip", [0, 5])
def test_plan_arm_reproduces_the_grader(path, slip):
    bars = _bars(_PATHS[path])
    sim = _sim(bars, slippage_bps=slip)
    assert sim["triggered"]
    plan = _replay(bars, slippage_bps=slip)["plan"]
    assert plan["r_planned"] == pytest.approx(sim["r_planned"], abs=1e-9)
    assert plan["exit_reason"] == sim["exit_reason"]


def test_plan_arm_honors_flatten_like_the_grader():
    bars = _bars(_PATHS["eod"])
    sim = _sim(bars, flatten_at="15:30")
    plan = _replay(bars, flatten_at="15:30")["plan"]
    assert sim["exit_reason"] == plan["exit_reason"] == "flatten"
    assert plan["r_planned"] == pytest.approx(sim["r_planned"], abs=1e-9)


# ================================================================ policies

def test_no_target_holds_past_2r_to_the_close():
    bars = _bars(_rise() + [round(10.3 + 0.02 * i, 4) for i in range(60)] + [11.4] * 240)
    r = _replay(bars)
    assert r["plan"]["exit_reason"] == "target" and r["plan"]["r_planned"] == pytest.approx(2.0)
    assert r["no_target"]["exit_reason"] == "eod"
    assert r["no_target"]["r_planned"] == pytest.approx((11.4 - ENTRY) / 0.5)


def test_breakeven_exits_at_the_fill_after_1r():
    # +1.2R, then a slide to the stop: the plan loses 1R, breakeven scratches.
    bars = _bars(_rise(to_i=61) + [round(10.0 + 0.04 * i, 4) for i in range(1, 16)]
                 + [round(10.6 - 0.05 * i, 4) for i in range(1, 30)] + [9.4] * 200)
    r = _replay(bars)
    assert r["plan"]["exit_reason"] == "stop" and r["plan"]["r_planned"] == pytest.approx(-1.0)
    assert r["breakeven_1r"]["exit_reason"] == "breakeven"
    assert r["breakeven_1r"]["r_planned"] == pytest.approx(0.0)


def test_breakeven_never_protects_the_bar_that_armed_it():
    # One bar touches +1.2R AND the stop: intra-bar order is unknowable.
    bars = _bars(_rise(to_i=61) + [(10.0, 10.6, 9.4, 9.5)] + [9.5] * 200)
    r = _replay(bars)
    assert r["breakeven_1r"]["exit_reason"] == "stop"
    assert r["breakeven_1r"]["r_planned"] == pytest.approx(-1.0)


def test_ema9_trail_exits_at_the_next_open_after_a_5m_close_below_the_ema():
    # Rise to 10:59, a 5m bucket (11:00-11:04) closing at 9.90 -- below the
    # lagging 5m EMA, still above the 9.50 stop -- then 11:05 opens at 9.92
    # and the day bleeds to 9.70.
    bars = _bars(_rise() + [9.9] * 5 + [(9.92, 9.93, 9.70, 9.70)] + [9.7] * 294)
    r = _replay(bars)
    trail = r["ema9_trail"]
    assert trail["exit_reason"] == "ema9_5m"
    assert trail["exit_et"] == datetime(2026, 9, 16, 11, 5)
    assert trail["r_planned"] == pytest.approx((9.92 - ENTRY) / 0.5)
    assert r["plan"]["exit_reason"] == "eod"
    assert r["plan"]["r_planned"] == pytest.approx((9.7 - ENTRY) / 0.5)


def test_scale_banks_half_at_1r_and_breakeven_cannot_beat_a_gap():
    bars = _bars(_rise(to_i=61) + [round(10.0 + 0.04 * i, 4) for i in range(1, 16)]
                 + [(9.4, 9.4, 9.3, 9.35)] + [9.35] * 200)
    r = _replay(bars)
    gap_r = (9.4 - ENTRY) / 0.5
    assert r["plan"]["r_planned"] == pytest.approx(gap_r)
    assert r["scale_1r_ema9"]["r_planned"] == pytest.approx(0.5 * 1.0 + 0.5 * gap_r)
    assert r["breakeven_1r"]["exit_reason"] == "breakeven"
    assert r["breakeven_1r"]["r_planned"] == pytest.approx(gap_r)


def test_time_stop_exits_at_that_bars_close():
    bars = _bars(_PATHS["eod"])
    r = _replay(bars)["time_1100"]
    assert r["exit_reason"] == "time_stop"
    assert r["exit_et"] == datetime(2026, 9, 16, 11, 0)


def test_time_stops_the_trade_could_not_follow_yield_no_row():
    bars = _bars([10.0] * 390)
    late = _replay(bars, fill_idx=120)             # filled 11:30
    assert "time_1100" not in late and "time_1200" in late
    half = {r["policy"] for r in runner_exits.replay(
        _bars([10.0] * 210, d=HALF_DAY), 60, ENTRY, STOP, session_date=HALF_DAY)}
    assert "time_1200" in half
    assert not half & {"time_1300", "time_1400", "time_1500"}


def test_mfe_is_in_planned_r_with_its_clock_time():
    bars = _bars(_PATHS["eod"][:95] + [(10.3, 10.8, 10.3, 10.3)] + [10.3] * 200)
    r = _replay(bars)["no_target"]
    assert r["mfe_r"] == pytest.approx((10.8 - ENTRY) / 0.5)
    assert r["mfe_et"] == datetime(2026, 9, 16, 11, 5)


# ================================================================ the search

def _rows(spec):
    """spec: {session: [(plan_r, alt_r), ...]} -> replay rows for no_target."""
    out = []
    for d, pairs in spec.items():
        for i, (p, a) in enumerate(pairs):
            out.append({"session_date": d, "ticker": f"T{i}", "policy": "plan", "r_planned": p})
            out.append({"session_date": d, "ticker": f"T{i}", "policy": "no_target", "r_planned": a})
    return out


def test_the_session_is_the_unit_and_every_policy_is_a_look():
    rows = _rows({date(2026, 9, 1): [(0, 1)] * 50, date(2026, 9, 2): [(0, -1)] * 3})
    comps = {c.level: c for c in runner_exits.exit_policy_comparisons(rows)}
    assert set(comps) == {p.name for p in runner_exits.POLICIES} - {"plan"}
    nt = comps["no_target"]
    assert nt.n == 2, "50 candidates on one tape are one observation, not 50"
    assert nt.mean_r == pytest.approx(0.0)
    assert comps["ema9_trail"].n == 0


def test_exit_md_leads_with_delta_r_not_win_rate():
    rows = _rows({date(2026, 9, 1): [(0, 1)], date(2026, 9, 2): [(0, 1)]})
    md = runner_exits.exit_md(runner_exits.exit_policy_comparisons(rows), rows)
    assert "paired ΔR" in md and "forward validation" in md
    assert "no_target" in md
    assert "No exit replays" in runner_exits.exit_md([], [])


def test_report_puts_win_rate_beside_mean_r():
    rows = _rows({date(2026, 9, 1): [(-1, 0.5), (2, 0.5)]})
    txt = runner_exits.report_text(rows)
    assert "win%" in txt and "mean R" in txt and "uncorrected" in txt


# ================================================================ storage

def _candidate(con, d, ticker, scan_version=2):
    con.execute(
        """INSERT INTO runner_candidates
           (session_date, ticker, first_pass_et, scan_pass_et, detection_price,
            stop_price, target_price, stop_dist, scan_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [d, ticker, datetime(2026, 9, 16, 10, 20), datetime(2026, 9, 16, 10, 25),
         ENTRY, STOP, TARGET, ENTRY - STOP, scan_version])


def test_save_replaces_the_session_and_reads_back_with_scan_version(con):
    _candidate(con, SESSION, "AAA")
    row = {"ticker": "aaa", "policy": "plan", "policy_version": 1, "sim_version": 4,
           "r_planned": -1.0, "exit_reason": "stop",
           "exit_et": datetime(2026, 9, 16, 11, 0), "mfe_r": 0.4,
           "mfe_et": datetime(2026, 9, 16, 10, 40)}
    ledger.save_runner_exit_replays(con, SESSION, [row, {**row, "policy": "no_target"}])
    ledger.save_runner_exit_replays(con, SESSION, [row])
    got = ledger.runner_exit_replays_since(con, SESSION)
    assert [(r["ticker"], r["policy"], r["scan_version"]) for r in got] == [("AAA", "plan", 2)]


def test_save_chunks_large_sessions(con):
    rows = [{"ticker": f"T{i}", "policy": "plan", "r_planned": 0.0} for i in range(23)]
    ledger.save_runner_exit_replays(con, SESSION, rows, chunk=5)
    assert con.execute("SELECT COUNT(*) FROM runner_exit_replays").fetchone()[0] == 23


def test_evening_stores_replays_and_r_planned(con):
    from agent.loops import evening
    _candidate(con, SESSION, "RUNR")
    bars = _bars(_PATHS["eod"])

    class _MD:
        def minute_bars(self, sym, d):
            return bars

        def daily_context(self, syms):
            raise RuntimeError("offline")

    summary = evening._grade_runner_candidates(con, _MD(), SESSION,
                                               Config(raw={"slippage_bps": 5}))
    out = ledger.runner_outcomes_for_date(con, SESSION)[0]
    assert out["r_planned"] is not None, "v23 column finally populated"
    assert out["eod_r_planned"] is not None
    replays = {r["policy"]: r for r in ledger.runner_exit_replays_since(con, SESSION)}
    assert summary["exit_replays"] == 1 and summary["exit_replay_parity_fail"] == 0
    assert replays["plan"]["r_planned"] == pytest.approx(out["r_planned"], abs=1e-3)
    assert set(replays) == {p.name for p in runner_exits.POLICIES}
    assert all(r["policy_version"] == runner_exits.POLICY_VERSION for r in replays.values())
    # Stored as ET wall clock. A tz-aware write lands shifted into the writer's
    # zone (Central locally, UTC on Neon) and the time-of-day read goes wrong.
    assert replays["time_1100"]["exit_et"] == datetime(2026, 9, 16, 11, 0)
    assert replays["plan"]["exit_et"] == datetime(2026, 9, 16, 15, 59)


def test_weekly_search_counts_every_exit_policy_as_a_look(con, monkeypatch):
    from agent import integrity
    monkeypatch.setattr(runner_exits, "current_replay_rows", lambda con, sessions=60: [])
    search = integrity.build_search(con, ledger)
    assert not [c for c in search.comparisons if c.dimension == runner_exits.DIMENSION], \
        "no replays, no family: nothing was looked at"

    rows = _rows({date(2026, 9, 1): [(0, 1)]})
    monkeypatch.setattr(runner_exits, "current_replay_rows", lambda con, sessions=60: rows)
    search = integrity.build_search(con, ledger)
    exits = [c for c in search.comparisons if c.dimension == runner_exits.DIMENSION]
    assert len(exits) == len(runner_exits.POLICIES) - 1
