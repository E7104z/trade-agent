"""
D1 (docs/FIX_PHASE0.md): the account must be flat after the close, every session.

IOVA, 250 shares at 14.36276, was bought on 2026-09-30 by a coverage-lane order
that a LOCAL run placed. The production ledger has no row for it, so neither
the 15:55 flatten (it walks today's open ledger rows) nor carry_cleanup (it
walks past open ledger rows) ever looked at it: 16 sessions, flat nowhere.

These tests pin the rule that closes the loophole: at and after
close - EOD_FLATTEN_MINUTES every broker position is closed whatever any ledger
says, and a position held at the morning that was opened in a PRIOR session is
closed even when a ledger row claims it. The broker is faked; the ledger is a
real temporary DuckDB.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from agent import delivery, ledger, live_broker, live_monitor
from agent.config import Config

ET = ZoneInfo("America/New_York")
TODAY = date(2026, 10, 16)          # the Friday IOVA was found still open
YESTERDAY = date(2026, 10, 15)
IOVA = {"symbol": "IOVA", "qty": "250", "side": "long", "avg_entry_price": "14.36276"}


def _cfg(tmp_path, monkeypatch, allow=None):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "backstop.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    live = {"enabled": True, "alpaca_paper": True, "trading_base": live_broker.PAPER_BASE,
            "breakout_two_step": False, "flatten_at": None}
    if allow is not None:
        live["allow_overnight_symbols"] = allow
    return Config(raw={"live_paper": live, "flatten_at": None},
                  alpaca_key_id="key", alpaca_secret_key="secret")


class FakeBroker:
    """The broker as the monitor sees it. `positions=None` is an unreadable
    account (NOT an empty one); `fills=None` is unreadable activity."""

    def __init__(self, positions=None, open_orders=None, fills=(), orders=None,
                 close_fill=None):
        self.positions = positions
        self.open_orders = list(open_orders or [])
        self.fills = fills
        self.orders = dict(orders or {})
        self.close_fill = close_fill          # {"filled_avg_price": "13.62", ...} or None
        self.closed: list[tuple[str, float | None]] = []
        self.cancelled: list[str] = []

    def install(self, monkeypatch):
        monkeypatch.setattr(live_broker, "list_positions", lambda **k: self.positions)
        monkeypatch.setattr(live_broker, "list_open_orders", lambda **k: list(self.open_orders))
        monkeypatch.setattr(live_broker, "cancel_order",
                            lambda oid, **k: self.cancelled.append(str(oid)) or True)
        monkeypatch.setattr(live_broker, "get_order", lambda oid, **k: self.orders.get(str(oid)))
        monkeypatch.setattr(live_broker, "fills_for_date", lambda d, **k: self.fills)
        monkeypatch.setattr(live_broker, "close_position", self._close)
        monkeypatch.setattr(live_monitor, "_sleep", lambda s: None)
        return self

    def _close(self, ticker, *, cfg=None, qty=None):
        self.closed.append((ticker, qty))
        oid = f"close-{len(self.closed)}"
        order = {"id": oid, "symbol": ticker, "status": "accepted", **(self.close_fill or {})}
        self.orders[oid] = order
        return order


def _seed(session, rows):
    """rows: dicts of ticker, qty, side, order_id, filled (bool)."""
    con = ledger.connect()
    picks = [{"ticker": r["ticker"], "direction": "long" if r.get("side", "buy") == "buy" else "short",
              "setup_type": "catalyst_momentum", "size_r": 0.25, "entry_plan": {"type": "limit"},
              "entry_type": "limit", "entry_price": 14.40, "stop_price": 13.90,
              "target_price": 15.30, "time_stop": "by 14:00", "exploration_lane": "coverage"}
             for r in rows]
    pick_ids = ledger.save_daily_plan(con, session, {"no_trade": False, "picks": picks}, {}, "chop")
    for r, pid in zip(rows, pick_ids):
        ledger.save_live_order(con, pid, {"id": r["order_id"], "status": "filled",
                                          "order_class": "bracket"},
                               session, r["ticker"], r.get("side", "buy"), r["qty"], "limit")
        if r.get("filled", True):
            ledger.update_live_order(con, r["order_id"], entry_fill_price=14.36276,
                                     entry_fill_at=datetime(session.year, session.month,
                                                            session.day, 9, 46))
    con.close()


def _row(order_id):
    con = ledger.connect()
    cols = [c[0] for c in con.execute("DESCRIBE live_orders").fetchall()]
    r = con.execute("SELECT * FROM live_orders WHERE live_order_id = ?", [order_id]).fetchone()
    con.close()
    return dict(zip(cols, r))


def _closes(kind=None):
    con = ledger.connect()
    try:
        return ledger.backstop_closes_for_date(con, TODAY, kind=kind)
    finally:
        con.close()


def _at(h, m, s=2, day=TODAY):
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=ET)


def _backstop(cfg, now):
    con = ledger.connect()
    try:
        return live_monitor.eod_backstop(con, cfg, now)
    finally:
        con.close()


def _sweep(cfg, now):
    con = ledger.connect()
    try:
        return live_monitor.orphan_sweep(con, cfg, TODAY, now)
    finally:
        con.close()


# ---------------------------------------------------------------- the route

def test_the_1555_tick_flattens_a_position_no_ledger_row_knows_about(monkeypatch, tmp_path):
    """The production entry point, end to end: IOVA 250 @ 14.36 is open at the
    broker, the ledger is empty, the clock is 15:55:02. Before the fix the tick
    answered {"updated": 0, "carry": {"stale": 0}} and left it open."""
    from api import index
    cfg = _cfg(tmp_path, monkeypatch)
    broker = FakeBroker(positions=[IOVA]).install(monkeypatch)
    fixed = _at(15, 55)
    monkeypatch.setattr(index, "CRON_SECRET", "s")
    monkeypatch.setattr(index, "load_config", lambda: cfg)
    monkeypatch.setattr(index, "today_et", lambda: TODAY)
    monkeypatch.setattr("agent.util.now_et", lambda: fixed)
    monkeypatch.setattr(live_monitor, "now_et", lambda: fixed)
    monkeypatch.setattr(live_monitor, "today_et", lambda: TODAY)
    resp = index.app.test_client().post("/cron/intraday-check",
                                        headers={"Authorization": "Bearer s"})
    assert resp.status_code == 200
    assert broker.closed == [("IOVA", None)]
    assert resp.get_json()["backstop"]["closed"] == ["IOVA"]


# ------------------------------------------------------------- the backstop

def test_at_1555_the_backstop_closes_everything_claimed_or_not(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, monkeypatch)
    _seed(TODAY, [{"ticker": "AAPL", "qty": 20, "order_id": "aapl-today"}])
    _seed(YESTERDAY, [{"ticker": "MSFT", "qty": 10, "side": "sell", "order_id": "msft-old"}])
    positions = [IOVA,                                                    # no row anywhere
                 {"symbol": "AAPL", "qty": "20", "side": "long", "avg_entry_price": "340"},
                 {"symbol": "MSFT", "qty": "-10", "side": "short", "avg_entry_price": "510"}]
    legs = [{"id": "tp-1", "symbol": "AAPL", "side": "sell", "type": "limit", "status": "new"},
            {"id": "sl-1", "symbol": "AAPL", "side": "sell", "type": "stop", "status": "held"}]
    broker = FakeBroker(positions=positions, open_orders=legs).install(monkeypatch)

    result = _backstop(cfg, _at(15, 55))

    assert result["closed"] == ["IOVA", "AAPL", "MSFT"]
    assert [t for t, _ in broker.closed] == ["IOVA", "AAPL", "MSFT"]
    assert sorted(broker.cancelled) == ["sl-1", "tp-1"]       # the legs hold the shares
    reasons = {r["symbol"]: r["reason"] for r in _closes("eod_backstop")}
    assert reasons == {"IOVA": "unledgered", "AAPL": "ledger_open", "MSFT": "prior_session_claim"}
    # the rows the close belongs to are pointed at it, so the next tick settles the fill
    assert _row("aapl-today")["close_order_id"] and _row("aapl-today")["exit_reason"] is None
    assert _row("msft-old")["close_order_id"]


def test_the_backstop_does_nothing_before_the_window(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, monkeypatch)
    broker = FakeBroker(positions=[IOVA]).install(monkeypatch)
    result = _backstop(cfg, _at(15, 50))
    assert broker.closed == [] and "before" in result["skipped"]


def test_a_position_opened_today_and_claimed_is_left_alone_before_1555(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, monkeypatch)
    _seed(TODAY, [{"ticker": "IOVA", "qty": 250, "order_id": "iova-today"}])
    fills = [{"symbol": "IOVA", "side": "buy", "qty": "250", "price": "14.36"}]
    broker = FakeBroker(positions=[IOVA], fills=fills).install(monkeypatch)

    swept = _sweep(cfg, _at(10, 30))
    early = _backstop(cfg, _at(10, 30))

    assert broker.closed == []
    assert swept["left_alone"] == ["IOVA"] and swept["closed"] == []
    assert "before" in early["skipped"]


def test_unreadable_positions_close_nothing(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, monkeypatch)
    _seed(TODAY, [{"ticker": "AAPL", "qty": 20, "order_id": "aapl-today"}])
    _seed(YESTERDAY, [{"ticker": "IOVA", "qty": 250, "order_id": "iova-old"}])
    broker = FakeBroker(positions=None).install(monkeypatch)    # None is NOT "holds nothing"

    eod = _backstop(cfg, _at(15, 56))
    morning = _sweep(cfg, _at(7, 12))

    assert broker.closed == [] and broker.cancelled == []
    assert "could not read positions" in eod["error"]
    assert "could not read positions" in morning["error"]
    assert _row("iova-old")["exit_reason"] is None


def test_two_backstop_calls_never_double_close(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, monkeypatch)
    broker = FakeBroker(positions=[IOVA]).install(monkeypatch)
    first = _backstop(cfg, _at(15, 55))
    second = _backstop(cfg, _at(15, 55, 4))                 # the position is still listed
    assert first["closed"] == ["IOVA"]
    assert second["closed"] == [] and second["in_flight"] == ["IOVA"]
    assert len(broker.closed) == 1


def test_a_concurrent_tick_holding_the_lease_blocks_the_backstop(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, monkeypatch)
    broker = FakeBroker(positions=[IOVA]).install(monkeypatch)
    con = ledger.connect()
    assert ledger.acquire_tick_lease(con, f"eod_backstop:{TODAY}", 60)
    assert ledger.acquire_tick_lease(con, f"eod_backstop:{TODAY}", 60) is None   # one holder
    con.close()
    result = _backstop(cfg, _at(15, 55))
    assert broker.closed == [] and "lease" in result["skipped"]


def test_a_close_the_broker_dropped_is_retried_on_the_next_tick(monkeypatch, tmp_path):
    """The double-close guard must not become a never-close guard: 15:55's close
    was cancelled by the broker, so the 15:57 tick sends another."""
    cfg = _cfg(tmp_path, monkeypatch)
    broker = FakeBroker(positions=[IOVA]).install(monkeypatch)
    _backstop(cfg, _at(15, 55))
    broker.orders["close-1"]["status"] = "canceled"
    again = _backstop(cfg, _at(15, 57))
    assert again["closed"] == ["IOVA"] and len(broker.closed) == 2


def test_a_close_refused_every_time_is_recorded_and_retried_later(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, monkeypatch)
    attempts = []
    FakeBroker(positions=[IOVA]).install(monkeypatch)
    monkeypatch.setattr(live_broker, "close_position",
                        lambda t, **k: attempts.append(t) or None)
    result = _backstop(cfg, _at(15, 55))
    assert result["closed"] == [] and "IOVA" in result["refused"][0]
    assert len(attempts) == len(live_monitor._CLOSE_RETRY_WAITS) + 1
    assert [r["status"] for r in _closes("eod_backstop")] == ["refused"]
    monkeypatch.setattr(live_broker, "close_position", lambda t, **k: {"id": "c2", "status": "accepted"})
    assert _backstop(cfg, _at(15, 57))["closed"] == ["IOVA"]


def test_allow_overnight_symbols_are_never_touched(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, monkeypatch, allow=["IOVA"])
    broker = FakeBroker(positions=[IOVA, {"symbol": "AAPL", "qty": "20", "side": "long",
                                          "avg_entry_price": "340"}]).install(monkeypatch)
    eod = _backstop(cfg, _at(15, 55))
    swept = _sweep(cfg, _at(7, 12))
    assert eod["closed"] == ["AAPL"] and eod["allowed"] == ["IOVA"]
    assert [t for t, _ in broker.closed if t == "IOVA"] == []
    assert "IOVA" not in swept["closed"]


# ------------------------------------------------------------ morning sweep

def test_the_morning_sweep_closes_a_position_claimed_by_yesterdays_row_and_settles_it(
        monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, monkeypatch)
    _seed(YESTERDAY, [{"ticker": "IOVA", "qty": 250, "order_id": "iova-old"}])
    broker = FakeBroker(positions=[IOVA],
                        close_fill={"filled_avg_price": "13.62",
                                    "filled_at": "2026-10-16T13:30:01Z"}).install(monkeypatch)

    result = _sweep(cfg, _at(7, 12))

    assert result["closed"] == ["IOVA"] and broker.closed == [("IOVA", None)]
    row = _row("iova-old")
    assert row["exit_reason"] == "overnight_carry"
    assert row["exit_fill_price"] == 13.62 and row["status"] == "closed"
    assert row["live_r"] == pytest.approx((13.62 - 14.36276) / 0.5)
    rec = _closes("morning_sweep")
    assert [(r["symbol"], r["qty"], r["reason"]) for r in rec] == [("IOVA", 250.0, "ledger_open")]


def test_the_morning_sweep_closes_an_unledgered_carry_too(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, monkeypatch)
    broker = FakeBroker(positions=[IOVA]).install(monkeypatch)
    result = _sweep(cfg, _at(7, 12))
    assert result["closed"] == ["IOVA"] and broker.closed == [("IOVA", None)]
    assert [r["reason"] for r in _closes("morning_sweep")] == ["unledgered"]


def test_a_claim_from_today_protects_only_what_was_opened_today(monkeypatch, tmp_path):
    """IOVA was carried (250) AND the desk bought 40 more this morning. carry_cleanup
    used to leave the whole 290 alone ("also traded today"); only the 250 is carry."""
    cfg = _cfg(tmp_path, monkeypatch)
    _seed(YESTERDAY, [{"ticker": "IOVA", "qty": 250, "order_id": "iova-old"}])
    _seed(TODAY, [{"ticker": "IOVA", "qty": 40, "order_id": "iova-today"}])
    position = {"symbol": "IOVA", "qty": "290", "side": "long", "avg_entry_price": "14.2"}
    fills = [{"symbol": "IOVA", "side": "buy", "qty": "40", "price": "14.5"}]
    broker = FakeBroker(positions=[position], fills=fills).install(monkeypatch)

    result = _sweep(cfg, _at(9, 40))

    assert broker.closed == [("IOVA", 250.0)]              # a partial close, not the whole position
    assert _row("iova-old")["close_order_id"] and _row("iova-today")["close_order_id"] is None
    assert result["closed"] == ["IOVA"]


def test_a_carry_whose_close_is_already_queued_is_reported_but_not_sent_twice(
        monkeypatch, tmp_path):
    """Tonight a market sell for IOVA's 250 shares is already queued for the open
    (someone sent it by hand). A second one would turn long 250 into short 250,
    so the sweep sends nothing -- but the position WAS a carry, and the morning
    email must say so rather than stay silent."""
    cfg = _cfg(tmp_path, monkeypatch)
    queued = {"id": "ba4f831f", "symbol": "IOVA", "side": "sell", "type": "market",
              "status": "accepted", "submitted_at": "2026-10-17T01:11:13.483296623Z"}
    broker = FakeBroker(positions=[IOVA], open_orders=[queued]).install(monkeypatch)

    first = _sweep(cfg, _at(7, 12))
    _sweep(cfg, _at(7, 17))                                   # a later tick: still one report

    assert broker.closed == [] and first["in_flight"] == ["IOVA"] and first["closed"] == []
    rows = _closes("morning_sweep")
    assert [(r["symbol"], r["status"], r["close_order_id"]) for r in rows] == [
        ("IOVA", "in_flight", "ba4f831f")]
    con = ledger.connect()
    lines = live_monitor.flatten_missed_lines(con, TODAY)
    con.close()
    assert len(lines) == 1 and "already working" in lines[0] and "IOVA" in lines[0]


def test_unreadable_fills_mid_session_close_nothing(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, monkeypatch)
    broker = FakeBroker(positions=[IOVA], fills=None).install(monkeypatch)
    result = _sweep(cfg, _at(11, 0))
    assert broker.closed == [] and "fills" in result["error"]


# --------------------------------------------------------- FLATTEN MISSED

def _seed_carry():
    con = ledger.connect()
    ledger.record_backstop_close(
        con, session_date=TODAY, kind="morning_sweep", symbol="IOVA", position_side="long",
        qty=250.0, avg_entry_price=14.36276, reason="unledgered", close_order_id="close-1",
        status="submitted")
    con.close()


def test_a_seeded_carry_produces_the_flatten_missed_line(monkeypatch, tmp_path):
    _cfg(tmp_path, monkeypatch)
    _seed_carry()
    con = ledger.connect()
    lines = live_monitor.flatten_missed_lines(con, TODAY)
    con.close()
    assert len(lines) == 1
    assert lines[0].startswith("FLATTEN MISSED") and "IOVA" in lines[0] and "250" in lines[0]
    html = delivery.morning_email_html(TODAY, {"no_trade": True, "flatten_missed": lines}, "")
    assert "FLATTEN MISSED" in html and "IOVA" in html


def test_a_clean_day_produces_no_flatten_missed_line(monkeypatch, tmp_path):
    _cfg(tmp_path, monkeypatch)
    con = ledger.connect()
    assert live_monitor.flatten_missed_lines(con, TODAY) == []
    con.close()
    assert "FLATTEN MISSED" not in delivery.morning_email_html(
        TODAY, {"no_trade": True, "flatten_missed": []}, "")
    assert "FLATTEN MISSED" not in delivery.morning_email_html(TODAY, {"no_trade": True}, "")


def test_status_carries_the_flatten_missed_line(monkeypatch, tmp_path):
    from agent import health
    from api import index
    _cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(index, "CRON_SECRET", "s")
    monkeypatch.setattr(index, "today_et", lambda: TODAY)
    monkeypatch.setattr(health, "now_et", lambda: _at(18, 0))
    client = index.app.test_client()
    hdr = {"Authorization": "Bearer s"}
    assert client.get("/api/status", headers=hdr).get_json()["flatten_missed"] == []
    _seed_carry()
    body = client.get("/api/status", headers=hdr).get_json()
    assert len(body["flatten_missed"]) == 1 and "IOVA" in body["flatten_missed"][0]
    warns = " ".join(body["loop_health"]["warnings"])
    assert "FLATTEN MISSED" in warns


def test_the_morning_loop_closes_the_carry_and_tells_the_email(monkeypatch, tmp_path):
    """The morning run (pre-open) is the first thing to see a carry. It closes
    it and hands the line to the email."""
    from agent.loops import morning
    cfg = _cfg(tmp_path, monkeypatch)
    broker = FakeBroker(positions=[IOVA]).install(monkeypatch)
    monkeypatch.setattr(morning, "now_et", lambda: _at(7, 12))
    plan = {"no_trade": True, "picks": []}
    con = ledger.connect()
    try:
        morning._close_overnight_carry(con, cfg, TODAY, plan)
    finally:
        con.close()
    assert broker.closed == [("IOVA", None)]
    assert len(plan["flatten_missed"]) == 1 and "IOVA" in plan["flatten_missed"][0]
    assert "FLATTEN MISSED" in delivery.morning_email_html(TODAY, plan, "")


def test_the_morning_loop_adds_nothing_on_a_clean_day(monkeypatch, tmp_path):
    from agent.loops import morning
    cfg = _cfg(tmp_path, monkeypatch)
    broker = FakeBroker(positions=[]).install(monkeypatch)
    monkeypatch.setattr(morning, "now_et", lambda: _at(7, 12))
    plan = {"no_trade": True, "picks": []}
    con = ledger.connect()
    try:
        morning._close_overnight_carry(con, cfg, TODAY, plan)
    finally:
        con.close()
    assert broker.closed == [] and plan["flatten_missed"] == []
    assert "FLATTEN MISSED" not in delivery.morning_email_html(TODAY, plan, "")


def test_a_failing_sweep_never_costs_the_morning_plan(monkeypatch, tmp_path):
    from agent.loops import morning
    cfg = _cfg(tmp_path, monkeypatch)
    FakeBroker(positions=[IOVA]).install(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("broker exploded")

    monkeypatch.setattr(live_monitor, "orphan_sweep", boom)
    monkeypatch.setattr(live_monitor, "carry_cleanup", boom)
    plan = {"no_trade": True, "picks": []}
    con = ledger.connect()
    try:
        morning._close_overnight_carry(con, cfg, TODAY, plan)    # must not raise
    finally:
        con.close()
    assert "flatten_missed" not in plan


def test_with_live_paper_off_the_morning_loop_touches_nothing(monkeypatch, tmp_path):
    from agent.loops import morning
    _cfg(tmp_path, monkeypatch)
    cfg = Config(raw={"live_paper": {"enabled": False}})
    broker = FakeBroker(positions=[IOVA]).install(monkeypatch)
    plan = {"no_trade": True, "picks": []}
    con = ledger.connect()
    try:
        morning._close_overnight_carry(con, cfg, TODAY, plan)
    finally:
        con.close()
    assert broker.closed == [] and "flatten_missed" not in plan


def test_carry_cleanup_closes_are_recorded_for_the_flatten_missed_line(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, monkeypatch)
    _seed(YESTERDAY, [{"ticker": "IOVA", "qty": 250, "order_id": "iova-old"}])
    broker = FakeBroker(positions=[IOVA], orders={"iova-old": {
        "status": "filled", "filled_avg_price": "14.36276", "filled_at": "2026-10-15T13:46:00Z",
        "legs": []}}).install(monkeypatch)
    con = ledger.connect()
    out = live_monitor.carry_cleanup(con, cfg, TODAY)
    con.close()
    assert out["closed"] == 1 and broker.closed == [("IOVA", None)]
    assert [(r["symbol"], r["reason"]) for r in _closes("morning_sweep")] == [("IOVA", "ledger_open")]


# ------------------------------------------------------------ the broker API

class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.content = payload, status, b"x"
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def test_close_position_can_close_part_of_a_position(monkeypatch):
    import requests
    cfg = Config(raw={"live_paper": {"enabled": True, "alpaca_paper": True,
                                     "trading_base": live_broker.PAPER_BASE}},
                 alpaca_key_id="key", alpaca_secret_key="secret")
    seen = []
    monkeypatch.setattr(requests, "request", lambda m, u, **k: seen.append((m, u)) or _Resp({"id": "x"}))
    live_broker.close_position("IOVA", cfg=cfg, qty=250)
    live_broker.close_position("IOVA", cfg=cfg)
    assert seen[0][0] == "DELETE" and seen[0][1].endswith("/v2/positions/IOVA?qty=250")
    assert seen[1][1].endswith("/v2/positions/IOVA")


def test_fills_for_date_pages_and_signs_nothing_it_cannot_read(monkeypatch):
    import requests
    cfg = Config(raw={"live_paper": {"enabled": True, "alpaca_paper": True,
                                     "trading_base": live_broker.PAPER_BASE}},
                 alpaca_key_id="key", alpaca_secret_key="secret")
    page1 = [{"id": f"a{i}", "symbol": "IOVA", "side": "buy", "qty": "1"} for i in range(100)]
    page2 = [{"id": "z1", "symbol": "IOVA", "side": "sell", "qty": "5"}]
    urls = []

    def fake(method, url, **k):
        urls.append(url)
        return _Resp(page2 if "page_token=a99" in url else page1)

    monkeypatch.setattr(requests, "request", fake)
    got = live_broker.fills_for_date(TODAY, cfg=cfg)
    assert len(got) == 101 and "date=2026-10-16" in urls[0] and "page_token=a99" in urls[1]
    monkeypatch.setattr(requests, "request", lambda *a, **k: _Resp({"message": "no"}, 500))
    assert live_broker.fills_for_date(TODAY, cfg=cfg) is None      # unreadable is not empty
