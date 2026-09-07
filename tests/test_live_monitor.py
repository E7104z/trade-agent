from __future__ import annotations

from datetime import date, datetime

from agent import ledger, live_broker, live_monitor
from agent.config import Config


SESSION = date(2026, 9, 4)


def _cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "live-monitor.duckdb")
    return Config(raw={
        "live_paper": {
            "enabled": True, "alpaca_paper": True,
            "trading_base": live_broker.PAPER_BASE,
            "breakout_two_step": True, "flatten_at": None,
        },
        "flatten_at": None,
    }, alpaca_key_id="key", alpaca_secret_key="secret")


def _seed(cfg, entry_type="breakout", time_stop="by 11:30", armed=False, oco_id=None):
    con = ledger.connect()
    pick_id = ledger.save_daily_plan(con, SESSION, {
        "no_trade": False,
        "picks": [{"ticker": "AAPL", "direction": "long", "setup_type": "breakout",
                    "size_r": 1.0, "entry_plan": {"type": entry_type},
                    "entry_type": entry_type, "entry_price": 100.0,
                    "stop_price": 98.0, "target_price": 104.0,
                    "time_stop": time_stop}],
    }, {}, "chop")[0]
    ledger.save_live_order(
        con, pick_id, {"id": "stop-1", "status": "new", "order_class": "stop_then_oco"},
        SESSION, "AAPL", "buy", 250, entry_type)
    if armed:
        ledger.update_live_order(con, "stop-1", oco_order_id=oco_id,
                                 exit_legs_armed=True, entry_fill_price=100.0,
                                 entry_fill_at=datetime(2026, 9, 4, 14, 0))
    con.close()
    return pick_id


def _row():
    con = ledger.connect()
    cols = [c[0] for c in con.execute("DESCRIBE live_orders").fetchall()]
    row = con.execute("SELECT * FROM live_orders").fetchone()
    con.close()
    return dict(zip(cols, row))


def test_stop_entry_fill_arms_oco(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, monkeypatch)
    _seed(cfg)
    monkeypatch.setattr(live_broker, "get_order", lambda order_id, **k: {
        "status": "filled", "filled_avg_price": "101", "filled_at": "2026-09-04T14:00:00Z",
    })
    monkeypatch.setattr(live_broker, "submit_oco_exit", lambda *a, **k: {"id": "oco-1"})
    result = live_monitor.check(cfg, run_date=SESSION,
                                at=datetime(2026, 9, 4, 10, 0))
    row = _row()
    assert result["updated"] == 0
    assert row["oco_order_id"] == "oco-1"
    assert row["exit_legs_armed"] is True
    assert row["exit_reason"] is None


def test_oco_failure_force_closes(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, monkeypatch)
    _seed(cfg)
    monkeypatch.setattr(live_broker, "get_order", lambda order_id, **k: {
        "status": "filled", "filled_avg_price": "101", "filled_at": "2026-09-04T14:00:00Z",
    })
    monkeypatch.setattr(live_broker, "submit_oco_exit", lambda *a, **k: None)
    monkeypatch.setattr(live_broker, "close_position", lambda *a, **k: {
        "filled_avg_price": "100.5", "filled_at": "2026-09-04T14:00:01Z",
    })
    result = live_monitor.check(cfg, run_date=SESSION,
                                at=datetime(2026, 9, 4, 10, 0))
    row = _row()
    assert result["updated"] == 1
    assert row["exit_reason"] == "unmanaged_close_failsafe"
    assert row["reconciled"] is True
    assert row["live_r"] == -0.25


def test_unfilled_stop_entry_is_cancelled_at_time_stop(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, monkeypatch)
    _seed(cfg)
    cancelled = []
    monkeypatch.setattr(live_broker, "get_order", lambda order_id, **k: {"status": "new"})
    monkeypatch.setattr(live_broker, "cancel_order", lambda order_id, **k: cancelled.append(order_id) or True)
    result = live_monitor.check(cfg, run_date=SESSION,
                                at=datetime(2026, 9, 4, 12, 0))
    row = _row()
    assert result["updated"] == 1
    assert cancelled == ["stop-1"]
    assert row["exit_reason"] == "cancelled_unfilled"
    assert row["live_r"] is None


def test_armed_oco_target_computes_live_r(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, monkeypatch)
    _seed(cfg, armed=True, oco_id="oco-1")

    def get_order(order_id, **kwargs):
        if order_id == "stop-1":
            return {"status": "filled", "filled_avg_price": "100"}
        return {"status": "filled", "legs": [{
            "status": "filled", "type": "limit", "filled_avg_price": "104",
            "filled_at": "2026-09-04T15:00:00Z",
        }]}

    monkeypatch.setattr(live_broker, "get_order", get_order)
    result = live_monitor.check(cfg, run_date=SESSION,
                                at=datetime(2026, 9, 4, 15, 0))
    row = _row()
    assert result["updated"] == 1
    assert row["exit_reason"] == "target"
    assert row["live_r"] == 2.0