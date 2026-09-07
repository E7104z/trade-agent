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