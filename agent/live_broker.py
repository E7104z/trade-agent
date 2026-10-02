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

# The most recent broker rejection, verbatim (e.g. Alpaca's 422 body). Callers
# record it next to the order they meant to place: before this existed every
# rejection was swallowed into a bare None, which is how every runner order
# was refused for weeks without a single visible error.
_last_error: str | None = None
# Set when the broker answers 401: the credentials, not the order, were
# refused. Cleared by the next successful call.
_auth_rejected: bool = False


def last_error() -> str | None:
    return _last_error


def auth_rejected() -> bool:
    """True when the most recent broker call was refused for its credentials."""
    return _auth_rejected


def tick_price(price: float) -> float:
    """Round to a price Alpaca accepts: whole cents at/above $1.00, four
    decimals below. Anything finer is rejected outright ("sub-penny increment
    does not fulfill minimum pricing criteria") -- runner levels are computed
    (detection price +/- EMA distance) and carry float noise like
    27.995591111111104, so every runner bracket was rejected on its price."""
    p = float(price)
    return round(p, 2) if abs(p) >= 1.0 else round(p, 4)


def _cfg(cfg: Config | None = None) -> Config:
    return cfg or load_config()


def _enabled(cfg: Config) -> bool:
    live = cfg.get("live_paper", {}) or {}
    return bool(live.get("enabled") and live.get("alpaca_paper") is True
                and str(live.get("trading_base", "")).rstrip("/") == PAPER_BASE
                and cfg.has_alpaca)


def _request(method: str, path: str, *, cfg: Config | None = None,
             payload: dict | None = None) -> dict | list | None:
    """Call Alpaca and return JSON; API/network failures fail soft (None), with
    the reason kept in last_error() and logged where the loop can see it."""
    global _last_error, _auth_rejected
    config = _cfg(cfg)
    if not _enabled(config):
        _last_error = "live paper trading is not enabled/configured"
        return None
    live = config.get("live_paper", {})
    url = str(live["trading_base"]).rstrip("/") + path
    headers = {"APCA-API-KEY-ID": config.alpaca_key_id,
               "APCA-API-SECRET-KEY": config.alpaca_secret_key,
               "Content-Type": "application/json"}
    try:
        response = requests.request(method, url, headers=headers, json=payload, timeout=15)
        status = getattr(response, "status_code", 200)
        if isinstance(status, int) and status >= 400:
            body = (getattr(response, "text", "") or "")[:300]
            _last_error = f"HTTP {status} {method} {path}: {body}"
            _auth_rejected = status == 401
            _LOG.warning("Alpaca paper API failure: %s", _last_error)
            from .util import log
            log("live", f"broker refused: {_last_error}")
            return None
        response.raise_for_status()
        _last_error = None
        _auth_rejected = False
        # DELETE /v2/orders/{id} answers 204 No Content on success. Parsing
        # that body raised, so every successful cancel read as a failure.
        if status == 204 or not getattr(response, "content", b"x"):
            return {}
        return response.json()
    except Exception as exc:
        _last_error = f"{method} {path}: {type(exc).__name__}: {exc}"[:300]
        _LOG.warning("Alpaca paper API failure for %s %s: %s", method, path, exc)
        return None


def entry_order_params(entry_type: str, entry_price: float | None) -> tuple[str, dict]:
    kind = str(entry_type or "open").lower()
    if kind == "limit":
        if entry_price is None:
            raise ValueError("limit entry requires entry_price")
        return "limit", {"limit_price": tick_price(entry_price)}
    if kind == "breakout":
        if entry_price is None:
            raise ValueError("breakout entry requires entry_price")
        return "stop", {"stop_price": tick_price(entry_price)}
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
            # Legacy path: a plain stop entry, exits armed by the monitor after
            # the fill -- unprotected until the next tick. Kept only behind the
            # flag; the atomic bracket below is the default.
            return submit_stop_entry_order(ticker, qty, side, entry_price, cfg=config)
        # A breakout is ONE stop-entry bracket: the entry is a stop order and
        # the take-profit and stop-loss legs are held until it triggers, so the
        # position is protected from its fill, exactly as the simulator
        # assumes. Verified on the paper account 2026-10-02 for both sides
        # (status pending_new, legs limit/stop "held"). Breakouts used to be
        # skipped here, on a reading that Alpaca allowed only market/limit
        # bracket entries, so the ledger graded trades the account never held.
        # A stop already through the market is refused by Alpaca ("stop price
        # must be greater than current price"); the caller enters those at
        # market (live_monitor.place_deferred_orders), as the simulator fills a
        # gapped breakout at the bar's open.
    payload = {
        "symbol": str(ticker).upper(), "qty": str(int(qty)), "side": side.lower(),
        "type": order_type, "time_in_force": "day", "order_class": "bracket",
        "take_profit": {"limit_price": tick_price(target_price)},
        "stop_loss": {"stop_price": tick_price(stop_price)},
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
        "type": "stop", "time_in_force": "day", "stop_price": tick_price(stop_price),
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
        "take_profit": {"limit_price": tick_price(target_price)},
        "stop_loss": {"stop_price": tick_price(stop_loss_price)},
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


def list_positions(*, cfg: Config | None = None) -> list[dict] | None:
    """Open positions, or None when the account could not be read (which is
    NOT the same as holding nothing)."""
    result = _request("GET", "/v2/positions", cfg=cfg)
    return result if isinstance(result, list) else None


def get_account(*, cfg: Config | None = None) -> dict | None:
    result = _request("GET", "/v2/account", cfg=cfg)
    return result if isinstance(result, dict) else None


def recent_orders(limit: int = 50, *, cfg: Config | None = None) -> list[dict]:
    result = _request("GET", f"/v2/orders?status=all&limit={int(limit)}&direction=desc&nested=true",
                      cfg=cfg)
    return result if isinstance(result, list) else []


def raw_order_json(order: dict) -> str:
    return json.dumps(order, default=str)