"""
Pins the 2026-09-23 live paper-trading fixes (Algo Trader Plus upgrade):

  - every runner bracket was refused by Alpaca for sub-penny prices (422), and
    the refusal was swallowed into a bare None
  - positions a missed end-of-day flatten left open stayed open for days
  - nothing could prove the intraday monitor ran; the trigger needed a
    credential that also unlocked every protected endpoint
  - the runner's earnings gate re-queried yfinance for every survivor on every
    tick and abandoned dozens of lookups at the batch deadline
  - bar windows were clamped 16 minutes back even on a real-time plan
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from agent import ledger, live_broker, live_monitor, providers, runner_scan
from agent.config import Config

ET = timezone(timedelta(hours=-4))
TODAY = date(2026, 9, 24)
PAST = date(2026, 9, 23)


def _live_cfg(**extra) -> Config:
    raw = {"live_paper": {"enabled": True, "alpaca_paper": True,
                          "trading_base": live_broker.PAPER_BASE}, "flatten_at": None}
    raw.update(extra)
    return Config(raw=raw, alpaca_key_id="k", alpaca_secret_key="s")


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.content = b"x" if payload is not None else b""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(str(self.status_code), response=self)

    def json(self):
        return self._payload


# ============================================================ broker

@pytest.mark.parametrize("raw,expected", [
    (27.995591111111104, 28.0), (27.40220444444445, 27.4), (0.123456, 0.1235), (1.0, 1.0)])
def test_tick_price_rounds_to_what_alpaca_accepts(raw, expected):
    assert live_broker.tick_price(raw) == expected


def test_bracket_payload_carries_only_valid_ticks(monkeypatch):
    sent = []
    monkeypatch.setattr(live_broker.requests, "request",
                        lambda *a, **k: sent.append(k["json"]) or _Resp(200, {"id": "o1"}))
    live_broker.submit_bracket_order("aapl", 10, "buy", "limit", 27.6000000001,
                                     27.40220444444445, 27.995591111111104, cfg=_live_cfg())
    p = sent[0]
    assert p["limit_price"] == 27.6
    assert p["stop_loss"]["stop_price"] == 27.4
    assert p["take_profit"]["limit_price"] == 28.0


def test_a_refusal_is_kept_not_swallowed(monkeypatch):
    body = '{"code":42210000,"message":"sub-penny increment does not fulfill minimum pricing criteria"}'
    monkeypatch.setattr(live_broker.requests, "request", lambda *a, **k: _Resp(422, None, body))
    assert live_broker.submit_bracket_order("AAPL", 1, "buy", "open", 10, 9, 12, cfg=_live_cfg()) is None
    assert "422" in live_broker.last_error() and "sub-penny" in live_broker.last_error()


# ======================================================= carry cleanup

@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "paper.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


def _seed(con, d, ticker="UBER", order_id="br-1"):
    pid = ledger.save_daily_plan(con, d, {"no_trade": False, "picks": [{
        "ticker": ticker, "direction": "long", "setup_type": "gap_go", "size_r": 1.0,
        "entry_plan": {"type": "limit"}, "entry_type": "limit", "entry_price": 72.0,
        "stop_price": 70.0, "target_price": 76.0, "time_stop": "by 14:00"}]}, {}, "chop")[0]
    ledger.save_live_order(con, pid, {"id": order_id, "status": "new", "order_class": "bracket"},
                           d, ticker, "buy", 69, "limit")
    return pid


def _row(con, order_id="br-1"):
    cols = [c[0] for c in con.execute("DESCRIBE live_orders").fetchall()]
    return dict(zip(cols, con.execute("SELECT * FROM live_orders WHERE live_order_id = ?",
                                      [order_id]).fetchone()))


def test_an_overnight_position_is_closed_and_settled(db, monkeypatch):
    """UBER 2026-09-10: filled, legs expired at the close, held for days."""
    _seed(db, PAST)
    events = []
    orders = {"br-1": {"status": "filled", "filled_avg_price": "72", "legs": [
                  {"id": "tp", "status": "expired", "type": "limit"},
                  {"id": "sl", "status": "held", "type": "stop"}]},
              "close-1": {"status": "filled", "filled_avg_price": "73"}}
    monkeypatch.setattr(live_broker, "list_positions", lambda **k: [{"symbol": "UBER"}])
    monkeypatch.setattr(live_broker, "get_order", lambda oid, **k: orders.get(oid))
    monkeypatch.setattr(live_broker, "cancel_order", lambda oid, **k: events.append(("cancel", oid)) or True)
    monkeypatch.setattr(live_broker, "close_position",
                        lambda t, **k: events.append(("close", t)) or {"id": "close-1"})

    out = live_monitor.carry_cleanup(db, _live_cfg(), TODAY)
    assert out["closed"] == 1
    assert events == [("cancel", "sl"), ("close", "UBER")], "expired legs are left alone"
    assert _row(db)["status"] == "closing:overnight_carry"

    out = live_monitor.carry_cleanup(db, _live_cfg(), TODAY)
    row = _row(db)
    assert out["settled"] == 1
    assert row["exit_reason"] == "overnight_carry" and row["live_r"] == pytest.approx(0.5)


def test_an_entry_that_never_filled_is_marked_expired(db, monkeypatch):
    _seed(db, PAST)
    monkeypatch.setattr(live_broker, "list_positions", lambda **k: [])
    monkeypatch.setattr(live_broker, "get_order", lambda oid, **k: {"status": "expired"})
    monkeypatch.setattr(live_broker, "close_position", lambda *a, **k: pytest.fail("nothing to close"))
    live_monitor.carry_cleanup(db, _live_cfg(), TODAY)
    assert _row(db)["exit_reason"] == "expired_unfilled"


def test_cleanup_never_acts_blind(db, monkeypatch):
    _seed(db, PAST)
    monkeypatch.setattr(live_broker, "list_positions", lambda **k: None)
    monkeypatch.setattr(live_broker, "close_position", lambda *a, **k: pytest.fail("acted blind"))
    out = live_monitor.carry_cleanup(db, _live_cfg(), TODAY)
    assert "nothing closed" in out["error"]


def test_cleanup_leaves_a_ticker_that_is_also_traded_today(db, monkeypatch):
    _seed(db, PAST)
    _seed(db, TODAY, order_id="br-today")
    monkeypatch.setattr(live_broker, "list_positions", lambda **k: [{"symbol": "UBER"}])
    monkeypatch.setattr(live_broker, "get_order",
                        lambda oid, **k: {"status": "filled", "filled_avg_price": "72", "legs": []})
    monkeypatch.setattr(live_broker, "close_position",
                        lambda *a, **k: pytest.fail("would close today's trade too"))
    out = live_monitor.carry_cleanup(db, _live_cfg(), TODAY)
    assert out["skipped"] and "also traded today" in out["skipped"][0]


# ======================================================= trigger + heartbeat

def test_trigger_secret_opens_only_the_intraday_endpoint(monkeypatch, tmp_path):
    from api import index
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "trig.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(index, "CRON_SECRET", "cron-secret")
    monkeypatch.setattr(index, "INTRADAY_TRIGGER_SECRET", "trigger-only")
    monkeypatch.setattr(index, "APP_PASSWORD", "pw")
    monkeypatch.setattr(index, "load_config", lambda: Config(raw={"live_paper": {"enabled": False}}))
    client = index.app.test_client()
    hdr = {"Authorization": "Bearer trigger-only"}

    assert client.post("/cron/intraday-check", headers=hdr).status_code == 200
    assert client.get("/api/status", headers=hdr).status_code == 401
    assert client.get("/cron/morning", headers=hdr).status_code != 200
    assert client.post("/cron/intraday-check",
                       headers={"Authorization": "Bearer wrong"}).status_code == 401

    con = ledger.connect()
    beat = ledger.last_beat(con, "intraday_check")
    con.close()
    assert beat and beat["last_at"] is not None


def test_health_reports_the_intraday_monitor(db):
    from agent import health
    cfg = Config(raw={"live_paper": {"enabled": True}, "runner": {"enabled": False}})
    now = datetime(2026, 9, 24, 11, 0, tzinfo=ET)
    by = {s["key"]: s for s in health.loop_health(db, cfg, now=now)["stages"]}
    assert by["intraday"]["status"] == "bad" and "never run" in by["intraday"]["headline"]

    db.execute("INSERT INTO heartbeats (name, last_at, detail) VALUES ('intraday_check', ?, '')",
               [datetime(2026, 9, 24, 10, 55)])
    by = {s["key"]: s for s in health.loop_health(db, cfg, now=now)["stages"]}
    assert by["intraday"]["status"] == "ok"

    late = datetime(2026, 9, 24, 12, 0, tzinfo=ET)
    by = {s["key"]: s for s in health.loop_health(db, cfg, now=late)["stages"]}
    assert by["intraday"]["status"] == "bad", "65 minutes of silence mid-session"


# ======================================================= earnings cache

def test_earnings_are_looked_up_once_per_day(db, monkeypatch):
    calls = []

    class _MD:
        def fundamentals(self, syms, con=None):
            return {}

        def next_earnings(self, syms):
            calls.append(sorted(syms))
            return {s: (5 if s == "AAA" else None) for s in syms}

    runner_scan._session_gate_cache(TODAY).clear()
    _f, e = runner_scan._fetch_gate_inputs(_MD(), ["AAA", "BBB"], TODAY, db)
    assert e == {"AAA": 5, "BBB": None}
    runner_scan._session_gate_cache(TODAY).clear()  # a cold serverless start
    _f, e = runner_scan._fetch_gate_inputs(_MD(), ["AAA", "BBB", "CCC"], TODAY, db)
    assert calls == [["AAA", "BBB"], ["CCC"]], "only the unchecked symbol is looked up"
    assert e["AAA"] == 5 and e["BBB"] is None


def test_next_earnings_tells_no_date_from_a_failed_lookup(monkeypatch):
    class _T:
        def __init__(self, s):
            self.s = s

        def get_earnings_dates(self, limit=4):
            if self.s == "BAD":
                raise RuntimeError("yfinance down")
            import pandas as pd
            return pd.DataFrame()

    class _YF:
        Ticker = _T

    monkeypatch.setattr(providers, "_yf", lambda: _YF)
    md = providers.MarketData(Config(raw={"data": {}}))
    out = md.next_earnings(["NONE", "BAD"])
    assert out == {"NONE": None}, "a failure must be absent, never cached as 'no earnings'"


# ======================================================= Alpaca data

def _alpaca(monkeypatch):
    a = providers.Alpaca(Config(raw={"data": {"alpaca_concurrency": 4}},
                                alpaca_key_id="k", alpaca_secret_key="s"))
    monkeypatch.setattr(providers.Alpaca, "_realtime_refused", False)
    return a


def test_concurrent_batches_match_sequential(monkeypatch):
    a = _alpaca(monkeypatch)

    def fake_get(path, params):
        syms = params["symbols"].split(",")
        return {"bars": {s: [{"t": "2026-09-23T14:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": len(s)}]
                         for s in syms if not s.endswith("9")}}

    monkeypatch.setattr(a, "_get", fake_get)
    syms = [f"S{i:03d}" for i in range(350)]
    t0 = datetime(2026, 9, 23, 9, 30, tzinfo=ET)
    seq = a.multi_bars(syms, "5Min", t0, t0 + timedelta(hours=1))
    par = a.multi_bars(syms, "5Min", t0, t0 + timedelta(hours=1), workers=4)
    assert seq == par and list(seq) == list(par)
    assert par["S009"] == [], "asked-and-empty stays distinguishable from never-asked"


def test_realtime_windows_until_the_plan_refuses_them(monkeypatch):
    a = _alpaca(monkeypatch)
    ends = []

    def fake_get(path, params):
        ends.append(params["end"])
        if len(ends) == 1:
            raise requests.exceptions.HTTPError(
                "403", response=_Resp(403, None, "subscription does not permit querying recent SIP data"))
        return {"bars": []}

    monkeypatch.setattr(a, "_get", fake_get)
    d = datetime.now(tz=providers.ET).date()
    a.daily_bar("SPY", d)
    assert providers.Alpaca._realtime_refused is True
    first, second = (datetime.fromisoformat(e) for e in ends)
    assert second < first, "retried with the window clamped behind the exclusion"
