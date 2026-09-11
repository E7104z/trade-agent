"""Fault-isolated Alpaca paper-trading wrapper."""
from __future__ import annotations

import json
import logging
import math
from typing import Any

import requests

from .config import Config, load_config

PAPER_BASE = "https://paper-api.alpaca.markets"
_LOG = logging.getLogger(__name__)


def _cfg(cfg: Config | None = None) -> Config:
    return cfg or load_config()


def _enabled(cfg: Config) -> bool:
    live = cfg.get("live_paper", {}) or {}
    return bool(live.get("enabled") and live.get("alpaca_paper") is True
                and str(live.get("trading_base", "")).rstrip("/") == PAPER_BASE
                and cfg.has_alpaca)


def _request(method: str, path: str, *, cfg: Config | None = None,
             payload: dict | None = None) -> dict | list | None:
    """Call Alpaca and return JSON; API/network failures fail soft."""
    config = _cfg(cfg)
    if not _enabled(config):
        return None
    live = config.get("live_paper", {})
    url = str(live["trading_base"]).rstrip("/") + path
    headers = {"APCA-API-KEY-ID": config.alpaca_key_id,
               "APCA-API-SECRET-KEY": config.alpaca_secret_key,
               "Content-Type": "application/json"}
    try:
        response = requests.request(method, url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        _LOG.warning("Alpaca paper API failure for %s %s: %s", method, path, exc)
        return None


def entry_order_params(entry_type: str, entry_price: float | None) -> tuple[str, dict]:
    kind = str(entry_type or "open").lower()
    if kind == "limit":
        if entry_price is None:
            raise ValueError("limit entry requires entry_price")
        return "limit", {"limit_price": float(entry_price)}
    if kind == "breakout":
        if entry_price is None:
            raise ValueError("breakout entry requires entry_price")
        return "stop", {"stop_price": float(entry_price)}
    return "market", {}


def quantity_for_r(size_r: float, dollars_per_r: float, entry: float, stop: float) -> int:
    risk_per_share = abs(float(entry) - float(stop))
    if risk_per_share <= 0:
        return 0
    return math.floor(float(size_r) * float(dollars_per_r) / risk_per_share)


def submit_bracket_order(ticker: str, qty: int, side: str, entry_type: str,
                         entry_price: float, stop_price: float,
                         target_price: float, *, cfg: Config | None = None) -> dict | None:
    if qty < 1:
        _LOG.info("Skipping live paper order for %s: qty < 1", ticker)
        return None
    order_type, entry_params = entry_order_params(entry_type, entry_price)
    if str(entry_type or "open").lower() == "breakout":
        config = _cfg(cfg)
        if (config.get("live_paper", {}) or {}).get("breakout_two_step"):
            return submit_stop_entry_order(ticker, qty, side, entry_price, cfg=config)
        # Alpaca documents bracket entries as market/limit. A stop-entry
        # bracket is not atomic, so leave breakout live placement for the
        # later stop-entry plus OCO flow rather than submit a rejected order.
        _LOG.warning("Skipping breakout live order for %s: stop-entry bracket is not supported atomically", ticker)
        return None
    payload = {
        "symbol": str(ticker).upper(), "qty": str(int(qty)), "side": side.lower(),
        "type": order_type, "time_in_force": "day", "order_class": "bracket",
        "take_profit": {"limit_price": float(target_price)},
        "stop_loss": {"stop_price": float(stop_price)},
        **entry_params,
    }
    result = _request("POST", "/v2/orders", cfg=cfg, payload=payload)
    return result if isinstance(result, dict) else None


def submit_stop_entry_order(ticker: str, qty: int, side: str, stop_price: float,
                            *, cfg: Config | None = None) -> dict | None:
    """Submit a plain stop entry; callers must arm exits after fill."""
    if qty < 1:
        _LOG.info("Skipping live paper stop entry for %s: qty < 1", ticker)
        return None
    payload = {
        "symbol": str(ticker).upper(), "qty": str(int(qty)), "side": side.lower(),
        "type": "stop", "time_in_force": "day", "stop_price": float(stop_price),
    }
    result = _request("POST", "/v2/orders", cfg=cfg, payload=payload)
    return result if isinstance(result, dict) else None


def submit_oco_exit(ticker: str, qty: int, side: str, stop_loss_price: float,
                    target_price: float, *, cfg: Config | None = None) -> dict | None:
    """Submit the closing OCO pair after a stop entry is confirmed filled."""
    if qty < 1:
        return None
    payload = {
        "symbol": str(ticker).upper(), "qty": str(int(qty)), "side": side.lower(),
        "type": "limit", "time_in_force": "day", "order_class": "oco",
        "take_profit": {"limit_price": float(target_price)},
        "stop_loss": {"stop_price": float(stop_loss_price)},
    }
    result = _request("POST", "/v2/orders", cfg=cfg, payload=payload)
    return result if isinstance(result, dict) else None


def cancel_order(order_id: str, *, cfg: Config | None = None) -> bool:
    return _request("DELETE", f"/v2/orders/{order_id}", cfg=cfg) is not None


def get_order(order_id: str, *, cfg: Config | None = None) -> dict | None:
    result = _request("GET", f"/v2/orders/{order_id}", cfg=cfg)
    return result if isinstance(result, dict) else None


def list_open_orders(*, cfg: Config | None = None) -> list[dict]:
    result = _request("GET", "/v2/orders?status=open", cfg=cfg)
    return result if isinstance(result, list) else []


def close_position(ticker: str, *, cfg: Config | None = None) -> dict | None:
    result = _request("DELETE", f"/v2/positions/{str(ticker).upper()}", cfg=cfg)
    return result if isinstance(result, dict) else None


def raw_order_json(order: dict) -> str:
    return json.dumps(order, default=str)