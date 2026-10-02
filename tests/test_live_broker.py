from __future__ import annotations

from agent.config import Config
from agent import live_broker


def _cfg(**live_overrides) -> Config:
    live = {
        "enabled": True,
        "alpaca_paper": True,
        "trading_base": live_broker.PAPER_BASE,
        "max_picks_per_day": 4,
    }
    live.update(live_overrides)
    return Config(raw={"live_paper": live, "dollars_per_r": 500},
                  alpaca_key_id="key", alpaca_secret_key="secret")


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_entry_type_mapping():
    assert live_broker.entry_order_params("open", 10) == ("market", {})
    assert live_broker.entry_order_params("limit", 10) == ("limit", {"limit_price": 10.0})
    assert live_broker.entry_order_params("breakout", 10) == ("stop", {"stop_price": 10.0})


def test_successful_bracket_submit(monkeypatch):
    calls = []
    monkeypatch.setattr(live_broker.requests, "request",
                        lambda *args, **kwargs: (calls.append((args, kwargs)) or
                                                  _Response({"id": "order-1", "status": "accepted"})))
    result = live_broker.submit_bracket_order(
        "aapl", 10, "buy", "limit", 100, 98, 104, cfg=_cfg())
    assert result["id"] == "order-1"
    payload = calls[0][1]["json"]
    assert payload["symbol"] == "AAPL"
    assert payload["order_class"] == "bracket"
    assert payload["type"] == "limit"
    assert payload["limit_price"] == 100.0


def test_qty_below_one_skips_without_call(monkeypatch):
    monkeypatch.setattr(live_broker.requests, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    assert live_broker.quantity_for_r(0.25, 100, 100, 1) == 0
    assert live_broker.submit_bracket_order("AAPL", 0, "buy", "open", 100, 1, 101, cfg=_cfg()) is None


def test_alpaca_down_fails_soft(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(live_broker.requests, "request", fail)
    assert live_broker.submit_bracket_order("AAPL", 1, "buy", "open", 100, 98, 104, cfg=_cfg()) is None


def test_not_explicitly_paper_refuses_placement(monkeypatch):
    monkeypatch.setattr(live_broker.requests, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    assert live_broker.submit_bracket_order(
        "AAPL", 1, "buy", "open", 100, 98, 104, cfg=_cfg(alpaca_paper=False)) is None


def test_a_breakout_is_one_atomic_stop_entry_bracket(monkeypatch):
    """Alpaca accepts order_class=bracket with a STOP entry (verified on the
    paper account 2026-10-02, both sides): the exits are held until the entry
    triggers, so the position is protected from its fill."""
    calls = []
    monkeypatch.setattr(live_broker.requests, "request",
                        lambda *args, **kwargs: (calls.append((args, kwargs)) or
                                                  _Response({"id": "brk-1", "status": "pending_new"})))
    result = live_broker.submit_bracket_order(
        "AAPL", 3, "buy", "breakout", 100, 98, 104, cfg=_cfg())
    assert result["id"] == "brk-1"
    assert calls[0][1]["json"] == {
        "symbol": "AAPL", "qty": "3", "side": "buy", "type": "stop", "time_in_force": "day",
        "order_class": "bracket", "take_profit": {"limit_price": 104.0},
        "stop_loss": {"stop_price": 98.0}, "stop_price": 100.0,
    }


def test_breakout_two_step_is_still_opt_in(monkeypatch):
    monkeypatch.setattr(live_broker, "submit_stop_entry_order", lambda *a, **k: {"id": "stop-1"})
    result = live_broker.submit_bracket_order(
        "AAPL", 1, "buy", "breakout", 100, 98, 104,
        cfg=_cfg(breakout_two_step=True))
    assert result == {"id": "stop-1"}


def test_two_step_order_payloads(monkeypatch):
    calls = []
    monkeypatch.setattr(live_broker.requests, "request",
                        lambda *args, **kwargs: (calls.append((args, kwargs)) or
                                                  _Response({"id": "order-1"})))
    cfg = _cfg(breakout_two_step=True)
    live_broker.submit_stop_entry_order("aapl", 3, "buy", 100, cfg=cfg)
    live_broker.submit_oco_exit("aapl", 3, "sell", 98, 104, cfg=cfg)
    assert calls[0][1]["json"] == {
        "symbol": "AAPL", "qty": "3", "side": "buy", "type": "stop",
        "time_in_force": "day", "stop_price": 100.0,
    }
    assert calls[1][1]["json"] == {
        "symbol": "AAPL", "qty": "3", "side": "sell", "type": "limit",
        "time_in_force": "day", "order_class": "oco",
        "take_profit": {"limit_price": 104.0},
        "stop_loss": {"stop_price": 98.0},
    }


def test_cancel_order_returns_success(monkeypatch):
    monkeypatch.setattr(live_broker.requests, "request", lambda *a, **k: _Response({}))
    assert live_broker.cancel_order("stop-1", cfg=_cfg()) is True