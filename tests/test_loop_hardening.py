"""
Pins the 2026-09-23 hardening pass: every behavior below was a real defect in
the learning loop, the trading simulation, or the path the hosted app takes
to run them.

  - connect() ran ~92 DDL statements (and took table locks) on every request
  - a lock left by a run killed at the 300s budget wedged its session forever
  - the morning lane's streak clamp / drawdown breaker read runner-lane trades
  - the simulator filled limit/breakout entries AFTER the pick's own time stop
  - the evening loop re-fetched identical bars sequentially per consumer
  - live bracket exits closed a position whose shares the legs still held,
    never cancelled an expired entry, never flattened at the close, and
    recorded asynchronous close fills with no price
  - a --dry-run morning marked the day's lock complete
  - the weekly miner shared the Friday evening request's 300s budget
"""
from __future__ import annotations

import gzip
import json
from datetime import date, datetime, time, timedelta, timezone

import pytest

from agent import grading, health, ledger, live_broker, live_monitor, memory, providers
from agent.config import Config

ET = timezone(timedelta(hours=-4))
SESSION = date(2026, 9, 16)  # a Wednesday, full session


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "t.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


def _bars(d: date, prices: list[tuple[float, float, float, float]], start=time(9, 30)):
    """1-minute bars from `start` with explicit (o, h, l, c)."""
    t0 = datetime.combine(d, start, tzinfo=ET)
    return [{"t": t0 + timedelta(minutes=i), "o": o, "h": h, "l": l, "c": c, "v": 10_000}
            for i, (o, h, l, c) in enumerate(prices)]


def _flat(n: int, px: float = 100.0):
    return [(px, px + 0.05, px - 0.05, px)] * n


# ================================================================ connect()

def test_connect_records_fingerprint_and_skips_ddl_when_current(con, monkeypatch):
    row = con.execute("SELECT fingerprint FROM schema_meta WHERE id = 1").fetchone()
    assert row[0] == ledger._SCHEMA_FINGERPRINT
    con.close()

    calls = []
    monkeypatch.setattr(ledger, "_apply_schema", lambda c, t: calls.append(1))
    ledger.connect().close()
    assert calls == [], "a current database must not re-run the migration list"

    monkeypatch.setattr(ledger, "_SCHEMA_FINGERPRINT", "a-new-migration-landed")
    ledger.connect().close()
    assert calls == [1], "a changed schema must run the migrations exactly once"


def test_postgres_connect_is_one_query_then_zero(monkeypatch):
    executed: list[str] = []

    class _Res:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class _FakePG:
        def execute(self, sql, params=None):
            executed.append(sql)
            if "FROM schema_meta" in sql:
                return _Res((ledger._SCHEMA_FINGERPRINT,))
            return _Res(None)

        def close(self):
            pass

    from agent import pgshim
    monkeypatch.setattr(pgshim, "connect_pg", lambda dsn: _FakePG())
    monkeypatch.setenv("DATABASE_URL", "postgres://unit-test")
    monkeypatch.setattr(ledger, "_SCHEMA_READY", set())
    ledger.connect()
    assert len(executed) == 1 and "schema_meta" in executed[0]
    ledger.connect()
    assert len(executed) == 1, "a warm process must not touch the schema at all"


# ============================================================ run locks

def test_stale_in_progress_lock_is_taken_over(con):
    d = SESSION
    assert ledger.acquire_run_lock(con, d, "evening")["acquired"]
    fresh = ledger.acquire_run_lock(con, d, "evening")
    assert not fresh["acquired"], "a live run's lock must still suppress duplicates"
    con.execute("UPDATE run_locks SET started_at = current_timestamp - INTERVAL '31 minutes' "
                "WHERE trading_date = ? AND run_type = 'evening'", [d])
    stale = ledger.acquire_run_lock(con, d, "evening")
    assert stale["acquired"] and stale["took_over_stale"]


def test_completed_lock_is_never_taken_over(con):
    d = SESSION
    lock = ledger.acquire_run_lock(con, d, "morning")
    ledger.finish_run_lock(con, d, "morning", lock["request_id"], "complete")
    con.execute("UPDATE run_locks SET started_at = current_timestamp - INTERVAL '5 hours'")
    assert not ledger.acquire_run_lock(con, d, "morning")["acquired"]


# ================================================================ lanes

def _pick(con, d, setup, r, pid_hint=""):
    row = con.execute(
        """INSERT INTO picks (plan_date, ticker, direction, setup_type, size_r,
                              entry_type, entry_price, stop_price, target_price, conviction)
           VALUES (?, ?, 'long', ?, 0.25, 'open', 10, 9, 12, 3) RETURNING pick_id""",
        [d, f"T{setup[:3].upper()}{pid_hint}", setup]).fetchone()
    ledger.save_outcome(con, {"pick_id": row[0], "eval_date": d, "triggered": True,
                              "r_multiple": r, "exit_reason": "eod"})


def test_morning_discipline_reads_only_the_morning_lane(con):
    _pick(con, SESSION, "gap_go", 1.0)
    for i in range(4):  # runner picks land later, with higher pick_ids
        _pick(con, SESSION, ledger.RUNNER_SETUP, -1.0, str(i))

    assert ledger.current_streak(con) == {"kind": "losing", "length": 4}
    assert ledger.current_streak(con, lane="morning") == {"kind": "winning", "length": 1}
    assert ledger.trailing_weighted_r(con, 10, lane="morning") == {"n": 1, "sum_weighted_r": 0.25}
    assert ledger.overall_stats(con, lane="morning")["triggered"] == 1
    assert [t["setup_type"] for t in ledger.recent_trades(con, 20, lane="morning")] == ["gap_go"]

    text = memory._self_awareness(con, None)
    assert "LOSING streak" not in text, "runner losers must not clamp the morning lane"


def test_equity_curve_for_the_morning_lane_leaves_out_legacy_runner_rows(con):
    _pick(con, SESSION, "gap_go", 2.0)                       # 0.5 weighted R
    _pick(con, SESSION, ledger.RUNNER_SETUP, 2.0, "x")       # a retired lane's row: 0.5 weighted R
    everything = ledger.equity_curve(con, 100_000, 500)
    morning = ledger.equity_curve(con, 100_000, 500, lane="morning")
    assert everything[-1]["equity"] == 100_500.0
    assert morning[-1]["equity"] == 100_000 + 0.5 * 500
    assert morning[-1]["day_r"] == 0.5 and everything[-1]["day_r"] == 1.0


def test_lane_clause_rejects_unknown_lanes():
    with pytest.raises(ValueError):
        ledger.lane_clause("afternoon")


# ======================================================= simulator (v4)

def test_limit_entry_cannot_fill_after_its_time_stop():
    # Price dips to the 99.00 limit only at 11:10, after the "by 11:00" stop.
    prices = _flat(100) + [(99.5, 99.6, 98.9, 99.2)] + _flat(10, 99.3)
    bars = _bars(SESSION, prices)
    pick = {"pick_id": 1, "direction": "long", "entry_type": "limit",
            "entry_price": 99.0, "stop_price": 98.0, "target_price": 101.0,
            "time_stop": "by 11:00"}
    out = grading.simulate_pick(pick, bars, None, session_date=SESSION)
    assert out["triggered"] is False
    assert out["untriggered_reason"] == "time_stop_expired_pretrigger"
    assert out["sim_version"] == grading.CURRENT_SIM_VERSION >= 4  # rule since v4


def test_limit_entry_inside_its_window_still_fills():
    prices = _flat(20) + [(99.5, 99.6, 98.9, 99.2)] + _flat(100, 99.3)
    pick = {"pick_id": 1, "direction": "long", "entry_type": "limit",
            "entry_price": 99.0, "stop_price": 98.0, "target_price": 101.0,
            "time_stop": "by 11:00"}
    out = grading.simulate_pick(pick, _bars(SESSION, prices), None, session_date=SESSION)
    assert out["triggered"] is True and out["entry_fill"] == 99.0
    assert out["exit_reason"] == "time_stop"


def test_breakout_short_cannot_fill_after_flatten():
    prices = _flat(380) + [(100, 100.1, 98.0, 98.5)] + _flat(9, 98.5)
    pick = {"pick_id": 1, "direction": "short", "entry_type": "breakout",
            "entry_price": 99.0, "stop_price": 100.5, "target_price": 96.0}
    out = grading.simulate_pick(pick, _bars(SESSION, prices), None,
                                flatten_at="15:45", session_date=SESSION)
    assert out["triggered"] is False
    assert out["untriggered_reason"] == "time_stop_expired_pretrigger"


@pytest.mark.parametrize("direction,entry,stop,target,open_px", [
    ("long", 100.0, 98.0, 104.0, 110.0),   # gapped over the target
    ("short", 100.0, 102.0, 96.0, 95.0),   # gapped under the target
])
def test_a_fill_already_past_its_target_is_not_a_trade(direction, entry, stop, target, open_px):
    """Before v4 this graded as an immediate 'target' exit at a LOSS -- the
    walk sold at the target, below the price just paid."""
    pick = {"pick_id": 1, "direction": direction, "entry_type": "open",
            "entry_price": entry, "stop_price": stop, "target_price": target}
    bars = _bars(SESSION, [(open_px, open_px + 1, open_px - 1, open_px)] * 5)
    out = grading.simulate_pick(pick, bars, None, session_date=SESSION)
    assert out["triggered"] is False
    assert out["exit_reason"] == "invalid_fill"
    assert out["r_multiple"] is None
    assert grading.classify_shadow(out) == ("unmeasurable", None)


def test_open_entries_are_unaffected_by_the_cutoff():
    pick = {"pick_id": 1, "direction": "long", "entry_type": "open",
            "entry_price": 100.0, "stop_price": 99.0, "target_price": 103.0,
            "time_stop": "by 09:30"}
    out = grading.simulate_pick(pick, _bars(SESSION, _flat(30)), None, session_date=SESSION)
    assert out["triggered"] is True and out["entry_fill"] == 100.0


# ============================================================ bar memo

def _md(monkeypatch, now: datetime):
    monkeypatch.setattr(providers, "now_et", lambda: now)
    return providers.MarketData(Config(raw={"data": {}}))


def test_finished_session_bars_are_fetched_once(monkeypatch):
    calls = []
    monkeypatch.setattr(providers, "yf_minute_bars",
                        lambda s, d: calls.append(s) or [{"t": 1, "o": 1, "h": 1, "l": 1, "c": 1}])
    md = _md(monkeypatch, datetime(2026, 9, 17, 9, 0, tzinfo=ET))
    a = md.minute_bars("abc", SESSION)
    b = md.minute_bars("ABC", SESSION)
    assert calls == ["abc"] and a == b and a is not b


def test_live_session_and_empty_results_are_never_memoized(monkeypatch):
    calls = []
    monkeypatch.setattr(providers, "yf_minute_bars",
                        lambda s, d: calls.append(s) or [{"t": 1}])
    md = _md(monkeypatch, datetime(2026, 9, 16, 11, 0, tzinfo=ET))  # mid-session
    md.minute_bars("ABC", SESSION)
    md.minute_bars("ABC", SESSION)
    assert len(calls) == 2

    empty = []
    monkeypatch.setattr(providers, "yf_minute_bars", lambda s, d: empty.append(s) or [])
    md = _md(monkeypatch, datetime(2026, 9, 17, 9, 0, tzinfo=ET))
    md.minute_bars("XYZ", SESSION)
    md.minute_bars("XYZ", SESSION)
    assert len(empty) == 2, "an empty result may be transient and must be retried"


def test_prefetch_warms_the_memo_through_the_same_fetchers(monkeypatch):
    calls = []
    monkeypatch.setattr(providers, "yf_minute_bars",
                        lambda s, d: calls.append(("m", s)) or [{"t": 1}])
    monkeypatch.setattr(providers, "yf_daily_bar",
                        lambda s, d: calls.append(("d", s, d)) or {"close": 1.0})
    md = _md(monkeypatch, datetime(2026, 9, 17, 9, 0, tzinfo=ET))
    md.prefetch_session(["AAA", "BBB"], SESSION)
    n = len(calls)
    assert n == 6  # minute + day + previous day, per symbol
    md.minute_bars("AAA", SESSION)
    md.daily_bar("BBB", SESSION)
    assert len(calls) == n, "consumers after a prefetch must be memo hits"


# ============================================================ live monitor

def _live_cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "live.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return Config(raw={"live_paper": {"enabled": True, "alpaca_paper": True,
                                      "trading_base": live_broker.PAPER_BASE},
                       "flatten_at": None},
                  alpaca_key_id="k", alpaca_secret_key="s")


def _seed_bracket(time_stop="by 11:30", entry_type="limit"):
    c = ledger.connect()
    pid = ledger.save_daily_plan(c, SESSION, {"no_trade": False, "picks": [{
        "ticker": "AAPL", "direction": "long", "setup_type": "gap_go", "size_r": 1.0,
        "entry_plan": {"type": entry_type}, "entry_type": entry_type,
        "entry_price": 100.0, "stop_price": 98.0, "target_price": 104.0,
        "time_stop": time_stop}]}, {}, "chop")[0]
    ledger.save_live_order(c, pid, {"id": "br-1", "status": "new", "order_class": "bracket"},
                           SESSION, "AAPL", "buy", 250, entry_type)
    c.close()


def _live_row():
    c = ledger.connect()
    cols = [x[0] for x in c.execute("DESCRIBE live_orders").fetchall()]
    row = dict(zip(cols, c.execute("SELECT * FROM live_orders").fetchone()))
    c.close()
    return row


def test_bracket_time_stop_cancels_legs_before_closing_and_settles_later(monkeypatch, tmp_path):
    cfg = _live_cfg(tmp_path, monkeypatch)
    _seed_bracket()
    events = []
    parent = {"status": "filled", "filled_avg_price": "100", "legs": [
        {"id": "tp-1", "status": "new", "type": "limit"},
        {"id": "sl-1", "status": "held", "type": "stop"}]}
    orders = {"br-1": parent, "close-1": {"status": "filled", "filled_avg_price": "101"}}
    monkeypatch.setattr(live_broker, "get_order", lambda oid, **k: orders.get(oid))
    monkeypatch.setattr(live_broker, "cancel_order",
                        lambda oid, **k: events.append(("cancel", oid)) or True)
    monkeypatch.setattr(live_broker, "close_position",
                        lambda t, **k: events.append(("close", t)) or {"id": "close-1"})

    r1 = live_monitor.check(cfg, run_date=SESSION, at=datetime(2026, 9, 16, 11, 35))
    assert events == [("cancel", "tp-1"), ("cancel", "sl-1"), ("close", "AAPL")]
    row = _live_row()
    assert r1["updated"] == 1
    assert row["close_order_id"] == "close-1" and row["status"] == "closing:time_stop"
    assert row["exit_reason"] is None, "no exit without a fill price"

    r2 = live_monitor.check(cfg, run_date=SESSION, at=datetime(2026, 9, 16, 11, 40))
    row = _live_row()
    assert r2["updated"] == 1
    assert row["exit_reason"] == "time_stop" and row["exit_fill_price"] == 101.0
    assert row["live_r"] == pytest.approx(0.5)


def test_unfilled_bracket_entry_is_cancelled_at_its_time_stop(monkeypatch, tmp_path):
    cfg = _live_cfg(tmp_path, monkeypatch)
    _seed_bracket()
    cancelled = []
    monkeypatch.setattr(live_broker, "get_order", lambda oid, **k: {"status": "new"})
    monkeypatch.setattr(live_broker, "cancel_order", lambda oid, **k: cancelled.append(oid) or True)
    monkeypatch.setattr(live_broker, "close_position",
                        lambda *a, **k: pytest.fail("nothing to close: the entry never filled"))
    live_monitor.check(cfg, run_date=SESSION, at=datetime(2026, 9, 16, 11, 31))
    assert cancelled == ["br-1"]
    assert _live_row()["exit_reason"] == "cancelled_unfilled"


def test_open_position_is_flattened_before_the_close(monkeypatch, tmp_path):
    cfg = _live_cfg(tmp_path, monkeypatch)
    _seed_bracket(time_stop=None)
    monkeypatch.setattr(live_broker, "get_order", lambda oid, **k: {
        "status": "filled", "filled_avg_price": "100", "legs": []})
    monkeypatch.setattr(live_broker, "cancel_order", lambda *a, **k: True)
    monkeypatch.setattr(live_broker, "close_position",
                        lambda *a, **k: {"id": "c", "filled_avg_price": "102"})
    live_monitor.check(cfg, run_date=SESSION, at=datetime(2026, 9, 16, 15, 50))
    assert _live_row()["exit_reason"] is None, "15:50 is inside the session"
    live_monitor.check(cfg, run_date=SESSION, at=datetime(2026, 9, 16, 15, 56))
    row = _live_row()
    assert row["exit_reason"] == "eod" and row["live_r"] == pytest.approx(1.0)


def test_cancel_reads_a_204_as_success(monkeypatch):
    class _R:
        status_code = 204
        content = b""

        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("no body")

    monkeypatch.setattr(live_broker.requests, "request", lambda *a, **k: _R())
    cfg = Config(raw={"live_paper": {"enabled": True, "alpaca_paper": True,
                                     "trading_base": live_broker.PAPER_BASE}},
                 alpaca_key_id="k", alpaca_secret_key="s")
    assert live_broker.cancel_order("x", cfg=cfg) is True


# ============================================================ best edge

def test_a_losing_setup_is_never_called_the_best_edge():
    from agent import dashboard
    out = dashboard.best_edge([{"dim": "dto_runner", "triggered": 19, "avg_r": -0.41},
                               {"dim": "gap_go", "triggered": 3, "avg_r": -0.9}])
    assert out.startswith("No positive edge yet")
    assert "dto_runner" in out
    # three trades are not an edge, nor even a reportable average
    assert dashboard.best_edge([{"dim": "gap_go", "triggered": 3, "avg_r": 0.4}]) == ""


# ============================================================ health

def test_loop_health_names_the_stage_that_did_not_complete(con):
    started = datetime(2026, 9, 16, 7, 0)
    ledger.log_run(con, "morning", SESSION, started, "ok")
    ledger.acquire_run_lock(con, SESSION, "evening")
    con.execute("UPDATE run_locks SET started_at = current_timestamp - INTERVAL '2 hours'")
    h = health.loop_health(con, Config(raw={"runner": {"enabled": False}}),
                           now=datetime(2026, 9, 16, 19, 0, tzinfo=ET))
    by = {s["key"]: s for s in h["stages"]}
    assert by["morning"]["status"] == "ok"
    assert by["evening"]["status"] == "bad"
    assert by["weekly"]["status"] == "warn"
    assert h["overall"] == "bad"
    assert any("stuck" in w for w in h["warnings"])


def test_loop_health_never_raises():
    class _Broken:
        def execute(self, *a, **k):
            raise RuntimeError("db down")
    h = health.loop_health(_Broken(), Config(raw={}), now=datetime(2026, 9, 16, 12, 0, tzinfo=ET))
    assert h["overall"] in ("bad", "unknown", "warn")


# ============================================================ morning dry run

def test_dry_run_never_claims_the_days_lock(con, monkeypatch, tmp_path):
    from agent.loops import morning

    class _Stop(Exception):
        pass

    # Stop right after the lock decision: the market-data pull is the first
    # thing inside the guarded block.
    monkeypatch.setattr(morning, "MarketData",
                        lambda cfg: (_ for _ in ()).throw(_Stop()))
    cfg = Config(raw={"models": {"morning": "m"}})
    cfg.dry_run = True
    con.close()
    with pytest.raises(_Stop):
        morning.run(cfg, run_date=SESSION, force=True)
    c = ledger.connect()
    assert c.execute("SELECT COUNT(*) FROM run_locks").fetchone()[0] == 0
    c.close()


# ============================================================ API + cron

def _client(monkeypatch, tmp_path):
    from api import index
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "api.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(index, "CRON_SECRET", "s")
    monkeypatch.setattr(index, "today_et", lambda: SESSION)
    return index, index.app.test_client()


def test_status_is_compact_gzipped_and_carries_health_and_lanes(monkeypatch, tmp_path):
    index, client = _client(monkeypatch, tmp_path)
    hdr = {"Authorization": "Bearer s"}
    plain = client.get("/api/status", headers=hdr)
    assert plain.status_code == 200
    assert b"\n  " not in plain.data, "the dashboard's status payload is compact"
    body = plain.get_json()
    assert set(body["lanes"]) == {"morning"}
    assert body["loop_health"]["stages"]

    zipped = client.get("/api/status", headers={**hdr, "Accept-Encoding": "gzip"})
    assert zipped.headers.get("Content-Encoding") == "gzip"
    assert json.loads(gzip.decompress(zipped.data))["today"] == SESSION.isoformat()


def test_weekly_has_its_own_cron_and_friday_evening_no_longer_chains_it(monkeypatch, tmp_path):
    index, client = _client(monkeypatch, tmp_path)
    import agent.util as util
    ran = []
    monkeypatch.setattr(index, "_execute_loop",
                        lambda name, cfg, force: ran.append(name) or {"loop": name, "error": None})
    hdr = {"Authorization": "Bearer s"}

    monkeypatch.setattr(util, "now_et", lambda: datetime(2026, 9, 18, 17, 10, tzinfo=ET))  # Fri
    assert client.get("/cron/evening", headers=hdr).status_code == 200
    assert ran == ["evening"]

    monkeypatch.setattr(util, "now_et", lambda: datetime(2026, 9, 19, 10, 20, tzinfo=ET))  # Sat
    assert client.get("/cron/weekly", headers=hdr).get_json()["cron"] == "weekly"
    assert ran == ["evening", "weekly"]

    assert client.get("/cron/runner", headers=hdr).status_code == 400, "the runner cron is gone"
    assert client.get("/cron/nope", headers=hdr).status_code == 400


def test_weekly_run_date_is_the_last_session(monkeypatch):
    from api import index
    monkeypatch.setattr(index, "today_et", lambda: date(2026, 9, 19))  # Saturday
    assert index._weekly_run_date() == date(2026, 9, 18)
    monkeypatch.setattr(index, "today_et", lambda: date(2026, 9, 16))
    assert index._weekly_run_date() == date(2026, 9, 16)


def test_every_scheduled_cron_path_is_routable():
    import pathlib
    from api import index
    crons = json.loads((pathlib.Path(__file__).parents[1] / "vercel.json").read_text())["crons"]
    for c in crons:
        name = c["path"].rsplit("/", 1)[-1]
        assert name in index._CRON_LOOP, c["path"]
        assert name in index._CRON_WINDOWS, c["path"]


def test_pick_bars_skips_the_network_before_the_open(monkeypatch):
    from api import index
    import agent.util as util
    monkeypatch.setattr(util, "now_et", lambda: datetime(2026, 9, 16, 8, 0, tzinfo=ET))
    monkeypatch.setattr(providers, "yf_minute_bars",
                        lambda *a: pytest.fail("no bars exist before the open"))
    assert index._pick_bars([{"ticker": "RCL"}], SESSION) == {}
