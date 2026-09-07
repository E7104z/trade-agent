"""Intraday synchronization for the optional live paper ledger."""
from __future__ import annotations

from datetime import date, datetime, time

from . import ledger, live_broker
from .config import Config
from .util import is_market_day, log, market_close_et, now_et, today_et


def _time_stop(value: str | None) -> time | None:
    if not value:
        return None
    text = str(value).lower().replace("by", " ").replace("at", " ").strip()
    for token in text.split():
        try:
            return datetime.strptime(token, "%H:%M").time()
        except ValueError:
            continue
    return None


def _fill(order: dict) -> tuple[float | None, datetime | None]:
    price = order.get("filled_avg_price")
    filled_at = order.get("filled_at")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    try:
        filled_at = datetime.fromisoformat(str(filled_at).replace("Z", "+00:00")) if filled_at else None
    except ValueError:
        filled_at = None
    return price, filled_at


def _exit_from_order(order: dict) -> tuple[str | None, float | None, datetime | None]:
    for leg in order.get("legs") or []:
        if leg.get("status") != "filled":
            continue
        reason = "stop" if leg.get("type") == "stop" else "target"
        price, at = _fill(leg)
        return reason, price, at
    return None, None, None


def _live_r(pick: dict, entry: float | None, exit_price: float | None) -> float | None:
    if entry is None or exit_price is None or not pick.get("entry_price") or not pick.get("stop_price"):
        return None
    risk = abs(float(pick["entry_price"]) - float(pick["stop_price"]))
    if risk <= 0:
        return None
    direction = 1 if pick.get("direction") == "long" else -1
    return (float(exit_price) - float(entry)) * direction / risk


def check(cfg: Config, *, run_date: date | None = None, at: datetime | None = None) -> dict:
    d = run_date or today_et()
    current = at or now_et()
    live_cfg = cfg.get("live_paper", {}) or {}
    if not live_cfg.get("enabled"):
        return {"skipped": True, "reason": "live paper disabled", "updated": 0}
    if not is_market_day(d):
        return {"skipped": True, "reason": "not a market day", "updated": 0}
    close = market_close_et(d)
    if not (time(9, 30) <= current.time() <= close):
        return {"skipped": True, "reason": "outside market hours", "updated": 0}

    con = ledger.connect()
    updated = 0
    try:
        picks = {p["pick_id"]: p for p in ledger.picks_for_date(con, d)}
        for row in ledger.live_orders_for_date(con, d, open_only=True):
            pick = picks.get(row.get("pick_id"))
            if not pick:
                continue
            order = live_broker.get_order(row["live_order_id"], cfg=cfg) or {}
            entry, entry_at = _fill(order)
            if entry is not None:
                ledger.update_live_order(con, row["live_order_id"],
                                         entry_fill_price=entry, entry_fill_at=entry_at,
                                         status=order.get("status"), raw_order_json=live_broker.raw_order_json(order))

            reason, exit_price, exit_at = _exit_from_order(order)
            if reason is None:
                stop_at = _time_stop(pick.get("time_stop"))
                flatten_at = _time_stop(live_cfg.get("flatten_at") or cfg.get("flatten_at"))
                if stop_at and current.time() >= stop_at:
                    reason = "time_stop"
                elif flatten_at and current.time() >= flatten_at:
                    reason = "flatten"
                if reason:
                    close_order = live_broker.close_position(row["ticker"], cfg=cfg) or {}
                    exit_price, exit_at = _fill(close_order)
            if reason and exit_price is not None:
                live_r = _live_r(pick, entry or row.get("entry_fill_price"), exit_price)
                ledger.update_live_order(
                    con, row["live_order_id"], status=order.get("status") or "closed",
                    exit_reason=reason, exit_fill_price=exit_price, exit_fill_at=exit_at,
                    live_r=live_r, reconciled=True,
                    raw_order_json=live_broker.raw_order_json(order),
                )
                updated += 1
        return {"skipped": False, "updated": updated}
    finally:
        con.close()


def reconcile_evening(con, cfg: Config, d: date) -> dict:
    """Poll today's orders and copy known live R into outcomes only."""
    if not (cfg.get("live_paper", {}) or {}).get("enabled"):
        return {"reconciled": 0, "divergence": []}
    picks = {p["pick_id"]: p for p in ledger.picks_for_date(con, d)}
    outcomes = {r[0]: r for r in con.execute(
        "SELECT pick_id, r_multiple FROM outcomes WHERE eval_date = ?", [d]).fetchall()}
    divergence = []
    count = 0
    for row in ledger.live_orders_for_date(con, d):
        order = live_broker.get_order(row["live_order_id"], cfg=cfg) or {}
        reason, exit_price, exit_at = _exit_from_order(order)
        entry, entry_at = _fill(order)
        if entry is not None:
            ledger.update_live_order(con, row["live_order_id"], entry_fill_price=entry, entry_fill_at=entry_at)
        if reason and exit_price is not None:
            live_r = _live_r(picks.get(row.get("pick_id"), {}), entry or row.get("entry_fill_price"), exit_price)
            if live_r is not None:
                ledger.update_live_order(con, row["live_order_id"], exit_reason=reason,
                                         exit_fill_price=exit_price, exit_fill_at=exit_at,
                                         live_r=live_r, reconciled=True)
                ledger.set_live_r(con, row["pick_id"], live_r)
                sim_r = outcomes.get(row["pick_id"], (None, None))[1]
                if sim_r is not None:
                    divergence.append(abs(live_r - sim_r))
                count += 1
    if divergence:
        log("evening", f"Live paper reconciliation: {count} exits, "
                       f"mean |live_r - simulated_r|={sum(divergence) / len(divergence):.3f}R")
    return {"reconciled": count, "divergence": divergence}