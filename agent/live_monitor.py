"""Intraday synchronization for the optional live paper ledger."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

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


# This desk is intraday-only: the simulator exits every open position at the
# session close ("eod"). Bracket legs are day orders -- they EXPIRE at the
# close, and an unmanaged position then simply stays open overnight. The
# monitor flattens this many minutes before the close so the live account
# holds what the simulator holds.
EOD_FLATTEN_MINUTES = 5

# Alpaca order states that still hold shares / can still fill.
_WORKING = {"new", "accepted", "held", "pending_new", "partially_filled",
            "accepted_for_bidding", "pending_replace", "calculated"}


def _past_cutoff(pick: dict, live_cfg: dict, current: datetime,
                 close: time | None = None) -> str | None:
    stop_at = _time_stop(pick.get("time_stop"))
    flatten_at = _time_stop(live_cfg.get("flatten_at"))
    if stop_at and current.time() >= stop_at:
        return "time_stop"
    if flatten_at and current.time() >= flatten_at:
        return "flatten"
    if close is not None:
        eod_at = (datetime.combine(date.min, close)
                  - timedelta(minutes=EOD_FLATTEN_MINUTES)).time()
        if current.time() >= eod_at:
            return "eod"
    return None


def _cancel_working_legs(order: dict | None, cfg: Config) -> int:
    """Cancel every still-working leg of a multi-leg order.

    A filled bracket's take-profit and stop legs HOLD the position's shares,
    so a plain close_position is rejected ("insufficient qty available") until
    they are cancelled -- which is why time-stop exits on bracket orders never
    actually closed anything."""
    n = 0
    for leg in (order or {}).get("legs") or []:
        if str(leg.get("status", "")).lower() in _WORKING and leg.get("id"):
            live_broker.cancel_order(str(leg["id"]), cfg=cfg)
            n += 1
    return n


def _record_close(con, row: dict, pick: dict, reason: str, close_order: dict,
                  live_entry: float | None) -> bool:
    """Persist a flatten. Returns True when the row changed.

    close_position returns the market order it submitted; Alpaca fills it a
    moment later, so its response usually carries no fill price. In that case
    the close order's id is kept and the row stays open (status
    "closing:<reason>") until a later tick or the evening reconcile reads the
    fill -- instead of stamping an exit with no price, which is what happened
    before and left live_r permanently empty."""
    exit_price, exit_at = _fill(close_order)
    close_id = str(close_order["id"]) if close_order.get("id") else None
    if exit_price is not None:
        ledger.update_live_order(
            con, row["live_order_id"], exit_reason=reason,
            exit_fill_price=exit_price, exit_fill_at=exit_at,
            live_r=_live_r(pick, live_entry, exit_price),
            reconciled=True, status="closed", close_order_id=close_id)
        return True
    if close_id:
        ledger.update_live_order(con, row["live_order_id"], close_order_id=close_id,
                                 status=f"closing:{reason}")
        return True
    return False  # close rejected (e.g. legs still cancelling) -- retried next tick


def _pending_close_reason(row: dict) -> str:
    status = str(row.get("status") or "")
    return status.split(":", 1)[1] if status.startswith("closing:") else "closed"


def _settle_pending_close(con, row: dict, pick: dict, cfg: Config) -> bool:
    """Read back the fill of an in-flight flatten order. True once settled."""
    close_order = live_broker.get_order(str(row["close_order_id"]), cfg=cfg) or {}
    exit_price, exit_at = _fill(close_order)
    if exit_price is None:
        return False
    live_r = _live_r(pick, row.get("entry_fill_price"), exit_price)
    ledger.update_live_order(
        con, row["live_order_id"], exit_reason=_pending_close_reason(row),
        exit_fill_price=exit_price, exit_fill_at=exit_at, live_r=live_r,
        reconciled=True, status="closed")
    row["live_r"] = live_r
    return True


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
            if row.get("close_order_id"):
                # A flatten is already in flight; only its fill is missing.
                if _settle_pending_close(con, row, pick, cfg):
                    updated += 1
                continue
            order = live_broker.get_order(row["live_order_id"], cfg=cfg) or {}
            entry, entry_at = _fill(order)
            if entry is not None:
                ledger.update_live_order(con, row["live_order_id"],
                                         entry_fill_price=entry, entry_fill_at=entry_at,
                                         status=order.get("status"), raw_order_json=live_broker.raw_order_json(order))

            if row.get("order_class") == "stop_then_oco":
                live_entry = entry or row.get("entry_fill_price")
                if not row.get("exit_legs_armed"):
                    if str(order.get("status", "")).lower() == "filled":
                        closing_side = "sell" if row.get("side") == "buy" else "buy"
                        oco = live_broker.submit_oco_exit(
                            row["ticker"], row["qty"], closing_side,
                            pick["stop_price"], pick["target_price"], cfg=cfg)
                        if oco and oco.get("id"):
                            ledger.update_live_order(
                                con, row["live_order_id"], oco_order_id=str(oco["id"]),
                                exit_legs_armed=True, entry_fill_price=live_entry,
                                entry_fill_at=entry_at, status="filled")
                            continue

                        close_order = live_broker.close_position(row["ticker"], cfg=cfg) or {}
                        exit_price, exit_at = _fill(close_order)
                        live_r = _live_r(pick, live_entry, exit_price)
                        ledger.update_live_order(
                            con, row["live_order_id"], exit_reason="unmanaged_close_failsafe",
                            exit_fill_price=exit_price, exit_fill_at=exit_at, live_r=live_r,
                            reconciled=True, status="closed",
                            raw_order_json=live_broker.raw_order_json(close_order))
                        log("intraday", f"{row['ticker']}: OCO arm failed post-fill -- "
                                         "force-closed at market to avoid an unprotected position")
                        updated += 1
                        continue

                    cutoff = _past_cutoff(pick, {**live_cfg, "flatten_at": live_cfg.get("flatten_at") or cfg.get("flatten_at")}, current, close)
                    if cutoff:
                        live_broker.cancel_order(row["live_order_id"], cfg=cfg)
                        ledger.update_live_order(con, row["live_order_id"],
                                                 exit_reason="cancelled_unfilled", reconciled=True,
                                                 status="canceled")
                        updated += 1
                    continue

                oco_id = row.get("oco_order_id")
                oco_order = live_broker.get_order(oco_id, cfg=cfg) if oco_id else {}
                reason, exit_price, exit_at = _exit_from_order(oco_order or {})
                if reason and exit_price is not None:
                    live_r = _live_r(pick, live_entry, exit_price)
                    ledger.update_live_order(
                        con, row["live_order_id"], exit_reason=reason,
                        exit_fill_price=exit_price, exit_fill_at=exit_at, live_r=live_r,
                        reconciled=True, status=oco_order.get("status"))
                    updated += 1
                    continue

                cutoff = _past_cutoff(pick, {**live_cfg, "flatten_at": live_cfg.get("flatten_at") or cfg.get("flatten_at")}, current, close)
                if cutoff:
                    live_broker.cancel_order(str(oco_id), cfg=cfg)
                    _cancel_working_legs(oco_order, cfg)
                    close_order = live_broker.close_position(row["ticker"], cfg=cfg) or {}
                    if _record_close(con, row, pick, cutoff, close_order, live_entry):
                        updated += 1
                continue

            reason, exit_price, exit_at = _exit_from_order(order)
            if reason is None:
                cutoff = _past_cutoff(
                    pick, {**live_cfg, "flatten_at": live_cfg.get("flatten_at") or cfg.get("flatten_at")},
                    current, close)
                if cutoff:
                    live_entry = entry if entry is not None else row.get("entry_fill_price")
                    if live_entry is None and str(order.get("status", "")).lower() in _WORKING:
                        # The entry never filled inside its window: cancel the
                        # whole bracket (its legs go with the parent) rather
                        # than let it fill after the plan expired -- the same
                        # cutoff the simulator applies (sim_version 4).
                        live_broker.cancel_order(row["live_order_id"], cfg=cfg)
                        ledger.update_live_order(con, row["live_order_id"],
                                                 exit_reason="cancelled_unfilled",
                                                 reconciled=True, status="canceled")
                        updated += 1
                        continue
                    if live_entry is not None:
                        _cancel_working_legs(order, cfg)
                        close_order = live_broker.close_position(row["ticker"], cfg=cfg) or {}
                        if _record_close(con, row, pick, cutoff, close_order, live_entry):
                            updated += 1
                        continue
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


def carry_cleanup(con, cfg: Config, today: date) -> dict:
    """Settle live orders from PAST sessions that never got an exit.

    The end-of-day flatten only happens when something triggers the monitor
    near the close. When nothing did (the machine was off, the scheduler
    failed), a filled position simply stayed open: UBER was bought 2026-09-10,
    its day legs expired at the close, and it sat until a manual close on
    09-15. This runs before the day's work and:

      - closes a position still open at the broker (cancelling its legs
        first), recorded as exit_reason "overnight_carry";
      - records a leg exit (stop/target) that filled before the legs expired;
      - marks an entry that never filled as "expired_unfilled".

    Never acts blind: if the account's positions cannot be read, nothing is
    closed. Never touches a ticker that also has an order TODAY -- closing
    that position would close today's trade too.
    """
    live_cfg = cfg.get("live_paper", {}) or {}
    if not live_cfg.get("enabled"):
        return {"skipped": "live paper disabled"}
    cols = [c[0] for c in con.execute("DESCRIBE live_orders").fetchall()]
    stale = [dict(zip(cols, r)) for r in con.execute(
        "SELECT * FROM live_orders WHERE exit_reason IS NULL AND session_date < ? "
        "ORDER BY session_date, live_order_id", [today]).fetchall()]
    if not stale:
        return {"stale": 0}
    positions = live_broker.list_positions(cfg=cfg)
    if positions is None:
        return {"stale": len(stale), "error": "could not read positions -- nothing closed"}
    held = {str(p.get("symbol", "")).upper() for p in positions}
    today_tickers = {str(r["ticker"]).upper() for r in ledger.live_orders_for_date(con, today)}
    picks: dict = {}
    for d in {r["session_date"] for r in stale}:
        picks.update({p["pick_id"]: p for p in ledger.picks_for_date(con, d)})

    out = {"stale": len(stale), "closed": 0, "settled": 0, "expired": 0, "skipped": []}
    for row in stale:
        pick = picks.get(row.get("pick_id"), {})
        ticker = str(row["ticker"]).upper()
        if row.get("close_order_id"):
            if _settle_pending_close(con, row, pick, cfg):
                out["settled"] += 1
            continue
        order = live_broker.get_order(row["live_order_id"], cfg=cfg) or {}
        if not order:
            out["skipped"].append(f"{ticker}: order not readable")
            continue
        entry, entry_at = _fill(order)
        entry = entry if entry is not None else row.get("entry_fill_price")
        if entry is None:
            if str(order.get("status", "")).lower() in _WORKING:
                live_broker.cancel_order(row["live_order_id"], cfg=cfg)
            ledger.update_live_order(con, row["live_order_id"], exit_reason="expired_unfilled",
                                     reconciled=True, status=order.get("status") or "expired")
            out["expired"] += 1
            continue
        exit_src = order
        if row.get("oco_order_id"):
            exit_src = live_broker.get_order(str(row["oco_order_id"]), cfg=cfg) or {}
        reason, exit_price, exit_at = _exit_from_order(exit_src)
        if reason and exit_price is not None:
            ledger.update_live_order(
                con, row["live_order_id"], exit_reason=reason, exit_fill_price=exit_price,
                exit_fill_at=exit_at, live_r=_live_r(pick, entry, exit_price),
                entry_fill_price=entry, reconciled=True, status="closed")
            out["settled"] += 1
            continue
        if ticker in today_tickers:
            out["skipped"].append(f"{ticker}: also traded today -- left for the EOD flatten")
            continue
        if ticker not in held:
            ledger.update_live_order(con, row["live_order_id"], exit_reason="closed_externally",
                                     entry_fill_price=entry, reconciled=True, status="closed")
            out["settled"] += 1
            continue
        if row.get("entry_fill_price") is None:
            # The close fills asynchronously and is settled on a later call,
            # which computes live R from the ROW -- so the entry read from the
            # broker must be on the row before the close goes out.
            ledger.update_live_order(con, row["live_order_id"], entry_fill_price=entry,
                                     entry_fill_at=entry_at)
            row["entry_fill_price"] = entry
        _cancel_working_legs(exit_src, cfg)
        close_order = live_broker.close_position(ticker, cfg=cfg) or {}
        if _record_close(con, row, pick, "overnight_carry", close_order, entry):
            out["closed"] += 1
            log("intraday", f"{ticker}: position from {row['session_date']} was still open "
                            f"(missed end-of-day flatten) -- closing now")
        else:
            out["skipped"].append(f"{ticker}: close refused ({live_broker.last_error()})")
    return out


def reconcile_evening(con, cfg: Config, d: date) -> dict:
    """Poll today's orders and copy known live R into outcomes only."""
    if not (cfg.get("live_paper", {}) or {}).get("enabled"):
        return {"reconciled": 0, "divergence": []}
    picks = {p["pick_id"]: p for p in ledger.picks_for_date(con, d)}
    # live_r is (exit - fill) over PLANNED risk, so the simulated side of the
    # divergence is planned-risk R too. r_multiple divides by the fill-to-stop
    # distance; comparing the two measured the denominators, not the fills.
    outcomes = {r[0]: r for r in con.execute(
        "SELECT pick_id, COALESCE(r_planned, r_multiple) FROM outcomes WHERE eval_date = ?",
        [d]).fetchall()}
    divergence = []
    count = 0
    for row in ledger.live_orders_for_date(con, d):
        if row.get("live_r") is None and row.get("close_order_id"):
            if _settle_pending_close(con, row, picks.get(row.get("pick_id"), {}), cfg):
                ledger.set_live_r(con, row["pick_id"], row["live_r"])
                sim_r = outcomes.get(row["pick_id"], (None, None))[1]
                if sim_r is not None and row["live_r"] is not None:
                    divergence.append(abs(row["live_r"] - sim_r))
                count += 1
            continue
        if row.get("live_r") is not None:
            ledger.set_live_r(con, row["pick_id"], row["live_r"])
            sim_r = outcomes.get(row["pick_id"], (None, None))[1]
            if sim_r is not None:
                divergence.append(abs(row["live_r"] - sim_r))
            count += 1
            continue
        order_id = row.get("oco_order_id") if row.get("exit_legs_armed") else row["live_order_id"]
        order = live_broker.get_order(order_id, cfg=cfg) or {}
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