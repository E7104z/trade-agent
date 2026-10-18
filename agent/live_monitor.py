"""Intraday synchronization for the optional live paper ledger."""
from __future__ import annotations

import time as time_mod
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


# This desk is intraday-only. Bracket legs are day orders -- they EXPIRE at
# the close, and an unmanaged position then simply stays open overnight. The
# monitor flattens this many minutes before the close, and the simulator exits
# at the same bar (grading.EOD_FLATTEN_MINUTES, sim_version 7), so the live
# account and the ledger hold the same thing.
from .grading import EOD_FLATTEN_MINUTES  # noqa: E402

# Alpaca order states that still hold shares / can still fill.
_WORKING = {"new", "accepted", "held", "pending_new", "partially_filled",
            "accepted_for_bidding", "pending_replace", "calculated"}


def _eod_at(close: time) -> time:
    """The clock time the end-of-day flatten starts: EOD_FLATTEN_MINUTES before
    the session close (15:55; 12:55 on an early close)."""
    return (datetime.combine(date.min, close) - timedelta(minutes=EOD_FLATTEN_MINUTES)).time()


def _past_cutoff(pick: dict, live_cfg: dict, current: datetime,
                 close: time | None = None) -> str | None:
    stop_at = _time_stop(pick.get("time_stop"))
    flatten_at = _time_stop(live_cfg.get("flatten_at"))
    if stop_at and current.time() >= stop_at:
        return "time_stop"
    if flatten_at and current.time() >= flatten_at:
        return "flatten"
    if close is not None:
        if current.time() >= _eod_at(close):
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


# Waits between close attempts after a bracket's legs are cancelled, in
# seconds. Alpaca cancels legs ASYNCHRONOUSLY: until a leg reports canceled it
# still holds the position's shares, and close_position is refused
# ("insufficient qty available"). The refused close used to be left for "the
# next tick" -- but the 15:55 flatten IS the last tick of the session, so it
# got one attempt, lost the race, and the position was carried overnight
# (three of them on 2026-10-01, closed by carry_cleanup the next morning).
_CLOSE_RETRY_WAITS = (0.5, 1.0, 1.5, 2.0)
_sleep = time_mod.sleep


def _close_with_retries(ticker: str, cfg: Config, qty: float | None = None) -> dict:
    """close_position (all of it, or `qty` shares), retried within this tick
    while cancelled legs settle. Returns the close order (empty when every
    attempt was refused)."""
    kw = {} if qty is None else {"qty": qty}
    close_order = live_broker.close_position(ticker, cfg=cfg, **kw) or {}
    for wait in _CLOSE_RETRY_WAITS:
        if close_order:
            break
        _sleep(wait)
        close_order = live_broker.close_position(ticker, cfg=cfg, **kw) or {}
    if not close_order:
        log("intraday", f"{ticker}: close refused after {len(_CLOSE_RETRY_WAITS) + 1} "
                        f"attempts ({live_broker.last_error()})")
    return close_order


def _close_position_after_legs(ticker: str, order: dict | None, cfg: Config) -> dict:
    """Cancel the working legs of `order`, then close the position, retrying
    within this tick while the cancels settle. Returns the close order (empty
    when every attempt was refused)."""
    _cancel_working_legs(order, cfg)
    return _close_with_retries(ticker, cfg)


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
                    close_order = _close_position_after_legs(row["ticker"], oco_order, cfg)
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
                        close_order = _close_position_after_legs(row["ticker"], order, cfg)
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
        close_order = _close_position_after_legs(ticker, exit_src, cfg)
        if _record_close(con, row, pick, "overnight_carry", close_order, entry):
            out["closed"] += 1
            log("intraday", f"{ticker}: position from {row['session_date']} was still open "
                            f"(missed end-of-day flatten) -- closing now")
            _note_carry(con, row, entry, close_order, today)
        else:
            out["skipped"].append(f"{ticker}: close refused ({live_broker.last_error()})")
    return out


def _note_carry(con, row: dict, entry: float | None, close_order: dict, today: date) -> None:
    """Record a carry carry_cleanup just closed, so the FLATTEN MISSED line
    covers it. Bookkeeping only: the close already went out."""
    try:
        ledger.record_backstop_close(
            con, session_date=today, kind="morning_sweep", symbol=row["ticker"],
            position_side="long" if row.get("side") == "buy" else "short",
            qty=float(row.get("qty") or 0), avg_entry_price=entry, reason="ledger_open",
            claimed_by=row["live_order_id"], carried_from=row["session_date"],
            close_order_id=str(close_order["id"]) if close_order.get("id") else None,
            status="submitted")
    except Exception as e:  # noqa: BLE001
        log("intraday", f"WARN could not record the carry close of {row.get('ticker')}: {e}")


# ---------------------------------------------------------------------------
# The account is flat after the close (docs/FIX_PHASE0.md, D1)
# ---------------------------------------------------------------------------
# check() and carry_cleanup() both walk LEDGER rows. A position no row knows
# about -- IOVA, 250 shares, bought 2026-09-30 by a run that wrote to a local
# ledger -- is invisible to both, and was carried for 16 sessions. These two
# functions read the BROKER's positions instead:
#
#   eod_backstop  at/after close - EOD_FLATTEN_MINUTES, closes every position
#                 whatever any ledger says;
#   orphan_sweep  before that, closes what was opened in a PRIOR session. A
#                 claim only protects what was opened today.
#
# Both are idempotent (a lease, plus a check for a close already in flight),
# both do nothing when the account cannot be read (None is not "holds
# nothing"), and both record what they closed in eod_backstop_closes -- the
# FLATTEN MISSED line is built from those rows.

_IN_FLIGHT = _WORKING | {"pending_cancel"}
_EPS = 1e-6


def _allow_overnight(cfg: Config) -> set[str]:
    live = cfg.get("live_paper", {}) or {}
    return {str(s).upper().strip() for s in (live.get("allow_overnight_symbols") or [])
            if str(s).strip()}


def _num(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _signed_qty(pos: dict) -> float:
    q = _num(pos.get("qty")) or 0.0
    if str(pos.get("side", "")).lower() == "short" and q > 0:
        q = -q
    return q


def _pos_fields(pos: dict) -> dict:
    q = _signed_qty(pos)
    return {"position_side": "long" if q > 0 else "short", "qty": abs(q),
            "avg_entry_price": _num(pos.get("avg_entry_price"))}


def _fills_net(fills: list[dict], symbol: str) -> float:
    """Shares bought minus shares sold in `symbol` today, from the broker's
    own fill feed."""
    net = 0.0
    for f in fills or []:
        if str(f.get("symbol", "")).upper() != symbol:
            continue
        q = _num(f.get("qty")) or 0.0
        side = str(f.get("side", "")).lower()
        if side == "buy":
            net += q
        elif side.startswith("sell"):
            net -= q
    return net


def _open_claims(con, today: date) -> dict[str, list[dict]]:
    """{SYMBOL: [live_orders rows still without an exit]} for today and every
    earlier session: what the ledger believes is open."""
    cols = [c[0] for c in con.execute("DESCRIBE live_orders").fetchall()]
    rows = con.execute(
        "SELECT * FROM live_orders WHERE exit_reason IS NULL AND session_date <= ? "
        "ORDER BY session_date, live_order_id", [today]).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        row = dict(zip(cols, r))
        out.setdefault(str(row["ticker"]).upper(), []).append(row)
    return out


def _working_close(con, cfg: Config, d: date, symbol: str, closing_side: str,
                   open_orders: list[dict]) -> str | None:
    """The id of a close for `symbol` that is already working at the broker, or
    None. A second tick must not send another one: it would open the opposite
    position (a long 250 closed twice is a short 250). Our own recorded closes
    are checked against the broker's current state, so one the broker dropped
    (cancelled, rejected) is NOT working and is retried; an order that cannot
    be read is assumed to be working."""
    for r in ledger.backstop_closes_for_date(con, d, symbol=symbol):
        if r.get("status") not in ("submitted", "in_flight") or not r.get("close_order_id"):
            continue
        order = live_broker.get_order(str(r["close_order_id"]), cfg=cfg)
        if order is None:
            return str(r["close_order_id"])
        state = str(order.get("status", "")).lower()
        if state in _IN_FLIGHT:
            return str(r["close_order_id"])
        price, at = _fill(order)
        ledger.update_backstop_close(con, r["close_id"], status=state or "failed",
                                     filled_price=price, filled_at=at)
    # A close that did not come through this table: check()'s own flatten, or
    # one sent by hand, is a working market order on the closing side.
    for o in open_orders or []:
        if (str(o.get("symbol", "")).upper() == symbol
                and str(o.get("side", "")).lower() == closing_side
                and str(o.get("type", "")).lower() == "market"
                and str(o.get("status", "")).lower() in _IN_FLIGHT):
            return str(o.get("id") or "unknown")
    return None


def _cancel_working_orders(cfg: Config, symbol: str, open_orders: list[dict],
                           before: date | None = None) -> int:
    """Cancel the working orders on `symbol` (bracket legs hold the position's
    shares, and Alpaca refuses a close while they do). With `before`, only
    orders submitted before that date: a morning sweep must never cancel the
    order the desk placed for the same ticker today."""
    n = 0
    for o in open_orders or []:
        if str(o.get("symbol", "")).upper() != symbol or not o.get("id"):
            continue
        if str(o.get("status", "")).lower() not in _WORKING:
            continue
        if before is not None:
            sub = str(o.get("submitted_at") or "")[:10]
            if not sub or sub >= before.isoformat():
                continue
        live_broker.cancel_order(str(o["id"]), cfg=cfg)
        n += 1
    return n


def _settle_rows(con, rows: list[dict], close_order: dict, reason: str,
                 fallback_entry: float | None) -> None:
    """Point every ledger row that claimed the closed position at its close, so
    the fill is read back and R computed (the same path a monitor flatten
    takes). Rows whose entry never filled claim no shares and are left."""
    picks: dict = {}
    for row in rows:
        if row.get("entry_fill_price") is None or row.get("close_order_id") \
                or row.get("exit_reason") is not None:
            continue
        sd = row["session_date"]
        if sd not in picks:
            picks[sd] = {p["pick_id"]: p for p in ledger.picks_for_date(con, sd)}
        entry = row.get("entry_fill_price")
        _record_close(con, row, picks[sd].get(row.get("pick_id"), {}), reason, close_order,
                      entry if entry is not None else fallback_entry)


def _book_close(con, cfg: Config, fields: dict, order: dict) -> None:
    price, at = _fill(order)
    ledger.record_backstop_close(
        con, status="filled" if price is not None else "submitted",
        close_order_id=str(order["id"]) if order.get("id") else None,
        filled_price=price, filled_at=at, **fields)


def eod_backstop(con, cfg: Config, now: datetime | None = None) -> dict:
    """At and after close - EOD_FLATTEN_MINUTES, close EVERY broker position
    not in live_paper.allow_overnight_symbols, whatever any ledger says.

    The intraday trigger calls this on every tick from 15:55 (15:55, 15:57 and
    15:59), so a close refused at 15:55 is retried twice more before the bell.
    Idempotent: a lease serializes overlapping ticks and a close already
    working at the broker is never sent twice. When the account cannot be read
    nothing is closed."""
    live_cfg = cfg.get("live_paper", {}) or {}
    if not live_cfg.get("enabled"):
        return {"skipped": "live paper disabled"}
    now = now or now_et()
    d = now.date()
    if not is_market_day(d):
        return {"skipped": "not a market day"}
    close = market_close_et(d)
    eod_at = _eod_at(close)
    if now.time() < eod_at:
        return {"skipped": f"before the {eod_at.strftime('%H:%M')} flatten window"}
    if now.time() > close:
        return {"skipped": "after the close: the next morning's sweep closes anything left"}
    name = f"eod_backstop:{d}"
    lease = ledger.acquire_tick_lease(con, name, 90)
    if lease is None:
        return {"skipped": "another tick holds the eod_backstop lease"}
    try:
        return _run_backstop(con, cfg, d)
    finally:
        ledger.release_tick_lease(con, name, lease)


def _run_backstop(con, cfg: Config, d: date) -> dict:
    positions = live_broker.list_positions(cfg=cfg)
    if positions is None:
        return {"error": f"could not read positions -- nothing closed ({live_broker.last_error()})"}
    allow = _allow_overnight(cfg)
    open_orders = live_broker.list_open_orders(cfg=cfg)
    claims = _open_claims(con, d)
    out: dict = {"closed": [], "in_flight": [], "refused": [], "allowed": []}
    for pos in positions:
        sym = str(pos.get("symbol", "")).upper()
        q = _signed_qty(pos)
        if not sym or abs(q) < _EPS:
            continue
        if sym in allow:
            out["allowed"].append(sym)
            continue
        if _working_close(con, cfg, d, sym, "sell" if q > 0 else "buy", open_orders):
            out["in_flight"].append(sym)
            continue
        rows = claims.get(sym, [])
        reason = ("ledger_open" if any(r["session_date"] == d for r in rows)
                  else "prior_session_claim" if rows else "unledgered")
        fields = dict(session_date=d, kind="eod_backstop", symbol=sym, reason=reason,
                      claimed_by=rows[0]["live_order_id"] if rows else None, **_pos_fields(pos))
        _cancel_working_orders(cfg, sym, open_orders)
        order = _close_with_retries(sym, cfg)
        if not order:
            err = live_broker.last_error() or "no detail"
            ledger.record_backstop_close(con, status="refused", detail=err, **fields)
            out["refused"].append(f"{sym}: close refused ({err})")
            continue
        _book_close(con, cfg, fields, order)
        _settle_rows(con, rows, order, "eod_backstop", _num(pos.get("avg_entry_price")))
        out["closed"].append(sym)
        log("intraday", f"{sym}: EOD BACKSTOP closed {abs(q):g} sh ({reason}) -- a position the "
                        f"15:55 flatten did not close")
    return out


def orphan_sweep(con, cfg: Config, today: date, now: datetime | None = None) -> dict:
    """Close what the broker holds that was opened in a PRIOR session.

    Every lane is intraday, so the account is flat after each close; anything
    still open at the next morning is a carry, closed even when a ledger row
    claims it. A claim only protects a position opened TODAY: the quantity
    protected is the larger of what today's fills and today's open ledger rows
    account for, and only the rest is closed (a partial close when the desk
    also bought the name this morning). The claiming prior-session row, if any,
    is settled as 'overnight_carry' with the real fill.

    Run from the morning loop (pre-open, nothing can have been bought today)
    and every intraday tick before the end-of-day window. Mid-session it needs
    the broker's fill feed; unreadable, it closes nothing."""
    live_cfg = cfg.get("live_paper", {}) or {}
    if not live_cfg.get("enabled"):
        return {"skipped": "live paper disabled"}
    if not is_market_day(today):
        return {"skipped": "not a market day"}
    now = now or now_et()
    if now.time() >= _eod_at(market_close_et(today)):
        return {"skipped": "inside the end-of-day window: the backstop owns it"}
    name = f"orphan_sweep:{today}"
    lease = ledger.acquire_tick_lease(con, name, 90)
    if lease is None:
        return {"skipped": "another tick holds the orphan_sweep lease"}
    try:
        return _run_sweep(con, cfg, today, now)
    finally:
        ledger.release_tick_lease(con, name, lease)


def _run_sweep(con, cfg: Config, today: date, now: datetime) -> dict:
    positions = live_broker.list_positions(cfg=cfg)
    if positions is None:
        return {"error": f"could not read positions -- nothing closed ({live_broker.last_error()})"}
    out: dict = {"closed": [], "carried": [], "left_alone": [], "in_flight": [],
                 "refused": [], "allowed": []}
    if not positions:
        return out
    # Before 09:30 nothing can have been bought today; after it, the fill feed
    # is what separates today's shares from the carried ones.
    fills = [] if now.time() < time(9, 30) else live_broker.fills_for_date(today, cfg=cfg)
    if fills is None:
        return {"error": "could not read today's fills -- nothing closed (an unreadable feed "
                         "is not proof that nothing was bought today)"}
    allow = _allow_overnight(cfg)
    open_orders = live_broker.list_open_orders(cfg=cfg)
    claims = _open_claims(con, today)
    for pos in positions:
        sym = str(pos.get("symbol", "")).upper()
        q = _signed_qty(pos)
        if not sym or abs(q) < _EPS:
            continue
        if sym in allow:
            out["allowed"].append(sym)
            continue
        sgn = 1.0 if q > 0 else -1.0
        rows = claims.get(sym, [])
        today_rows = [r for r in rows if r["session_date"] == today]
        prior_rows = [r for r in rows if r["session_date"] < today]
        ledger_today = sum((r["qty"] if r.get("side") == "buy" else -r["qty"]) for r in today_rows
                           if r.get("entry_fill_price") is not None and r.get("qty"))
        protected = max(0.0, sgn * _fills_net(fills, sym), sgn * ledger_today)
        carry = abs(q) - protected
        if carry <= _EPS:
            out["left_alone"].append(sym)
            continue
        reason = "ledger_open" if prior_rows else "unledgered"
        fields = dict(session_date=today, kind="morning_sweep", symbol=sym, reason=reason,
                      claimed_by=prior_rows[0]["live_order_id"] if prior_rows else None,
                      carried_from=prior_rows[0]["session_date"] if prior_rows else None,
                      **{**_pos_fields(pos), "qty": carry})
        working = _working_close(con, cfg, today, sym, "sell" if q > 0 else "buy", open_orders)
        if working:
            # Someone (a manual close, an earlier tick) already has a close
            # working: send nothing. The position WAS a carry, so say so once;
            # the morning email must not stay silent about it.
            out["in_flight"].append(sym)
            if not ledger.backstop_closes_for_date(con, today, kind="morning_sweep", symbol=sym):
                ledger.record_backstop_close(
                    con, status="in_flight", close_order_id=working,
                    detail="a close order was already working when the sweep found it", **fields)
            continue
        whole = carry >= abs(q) - _EPS
        qty_arg = None if whole else round(carry, 6)
        _cancel_working_orders(cfg, sym, open_orders, before=today)
        order = _close_with_retries(sym, cfg, qty=qty_arg)
        if not order:
            err = live_broker.last_error() or "no detail"
            ledger.record_backstop_close(con, status="refused", detail=err, **fields)
            out["refused"].append(f"{sym}: close refused ({err})")
            continue
        _book_close(con, cfg, fields, order)
        _settle_rows(con, prior_rows, order, "overnight_carry", _num(pos.get("avg_entry_price")))
        out["closed"].append(sym)
        out["carried"].append({"symbol": sym, "qty": carry, "reason": reason})
        log("intraday", f"{sym}: ORPHAN SWEEP closed {carry:g} sh carried from a prior session "
                        f"({reason})")
    return out


def _qty_text(q) -> str:
    q = float(q or 0)
    return str(int(q)) if q == int(q) else f"{q:g}"


def flatten_missed_lines(con, since: date) -> list[str]:
    """One FLATTEN MISSED line per carry closed on/after `since`: a position
    that was still open after a close. Empty on a clean day -- the morning
    email and /api/status show nothing then."""
    how = {"unledgered": "no ledger row knew about it, so no flatten could have caught it",
           "ledger_open": "its ledger row had no exit",
           "prior_session_claim": "only a past session's ledger row claimed it"}
    lines = []
    for r in ledger.flatten_missed_rows(con, since):
        px = f" @ {r['avg_entry_price']:.2f}" if r.get("avg_entry_price") else ""
        origin = f" from {r['carried_from']}" if r.get("carried_from") else ""
        status = r.get("status")
        did = ("found a close order already working (sent by someone else) and sent none"
               if status == "in_flight" else "closed it at market"
               + (" -- the close was REFUSED, so it is still open" if status == "refused" else ""))
        lines.append(
            f"FLATTEN MISSED: {r['symbol']} {r.get('position_side') or ''} "
            f"{_qty_text(r.get('qty'))} sh{px} was still open after the close{origin} "
            f"({how.get(r.get('reason'), r.get('reason') or 'unknown')}); "
            f"the {r['session_date']} sweep {did}.")
    return lines


def _live_prices(md, tickers: list[str]) -> dict[str, float]:
    """Latest trade per ticker from one snapshots call; {} when unavailable."""
    alp = getattr(md, "alpaca", None) if md is not None else None
    if alp is None or not tickers:
        return {}
    try:
        snaps = alp.snapshots(tickers) or {}
    except Exception as e:  # noqa: BLE001 -- a price read never blocks placement
        log("intraday", f"WARN live prices unavailable for deferred placement: {e}")
        return {}
    out: dict[str, float] = {}
    for t, snap in snaps.items():
        p = ((snap or {}).get("latestTrade") or {}).get("p")
        if p:
            out[str(t).upper()] = float(p)
    return out


def place_deferred_orders(con, cfg: Config, d: date, now: datetime, md=None) -> dict:
    """Send the morning picks whose entry window has opened (schema v27).

    A limit or breakout pick carries entry_not_before (09:45 by default --
    "no entries in the first 15 minutes after the open"). The morning run no
    longer sends those orders pre-market, where they were day orders live from
    09:30 and a limit already through the price filled on the opening print
    (NKE and WDC on 2026-10-02). This sends them at the first intraday tick at
    or after the window, with exactly the morning run's sizing and bracket.

    Mirrors the simulator, which starts the same pick's fill search at the
    window:
      - a limit whose price is ALREADY through its stop is not sent: it would
        fill on the wrong side of its own stop, which the simulator grades
        invalid_fill (a breakout simply rests -- the simulator fills it later);
      - a pick whose time stop / flatten passed before its window is not sent.
    Every outcome is recorded in deferred_placements. A broker refusal is
    retried on the next tick; a skip is final. Idempotent: a pick that has a
    live order, or a final outcome, is never sent twice.
    """
    live_cfg = cfg.get("live_paper", {}) or {}
    if not live_cfg.get("enabled"):
        return {"skipped": "live paper disabled"}
    if not is_market_day(d):
        return {"skipped": "not a market day"}
    close = market_close_et(d)
    t = now.time()                           # `now` is ET wall-clock (now_et())
    if not (time(9, 30) <= t < close):
        return {"skipped": "outside market hours"}

    max_live = int(live_cfg.get("max_picks_per_day", 4))
    morning = [p for p in ledger.picks_for_date(con, d)
               if p.get("setup_type") != ledger.RUNNER_SETUP]
    eligible = morning[:max_live]            # the morning run's own slice, pick_id order
    have_order = {o.get("pick_id") for o in ledger.live_orders_for_date(con, d)}
    final = {pid for pid, r in ledger.deferred_placements_for_date(con, d).items()
             if r.get("status") in ("placed", "skipped")}
    flat_cfg = {**live_cfg, "flatten_at": live_cfg.get("flatten_at") or cfg.get("flatten_at")}

    out = {"placed": 0, "waiting": 0, "skipped": [], "refused": []}
    due = []
    for p in eligible:
        nb = _time_stop(p.get("entry_not_before"))
        if nb is None or p["pick_id"] in have_order or p["pick_id"] in final:
            continue
        if t < nb:
            out["waiting"] += 1
            continue
        cutoff = _past_cutoff(p, flat_cfg, now, close)
        if cutoff:
            why = f"its {cutoff} passed before the order could go live"
            ledger.record_deferred_placement(con, d, p["pick_id"], p["ticker"], "skipped", why)
            out["skipped"].append(f"{p['ticker']}: {why}")
            continue
        due.append(p)
    if not due:
        return out

    if md is None:
        from .providers import MarketData
        md = MarketData(cfg)
    last = _live_prices(md, [p["ticker"] for p in due])
    for p in due:
        ticker = str(p["ticker"]).upper()
        long = p.get("direction") == "long"
        entry, stop, target = (float(p["entry_price"]), float(p["stop_price"]),
                               float(p["target_price"]))
        kind = str(p.get("entry_type") or "open").lower()
        px = last.get(ticker)
        if kind == "limit" and px is not None and ((long and px <= stop) or (not long and px >= stop)):
            why = (f"price {px:.2f} already through the stop {stop:.2f} when the window "
                   f"opened -- the limit would fill on the wrong side of its own stop "
                   f"(simulated as invalid_fill)")
            ledger.record_deferred_placement(con, d, p["pick_id"], ticker, "skipped", why)
            out["skipped"].append(f"{ticker}: {why}")
            log("intraday", f"{ticker}: deferred order NOT placed -- {why}")
            continue
        qty = live_broker.quantity_for_r(p.get("size_r") or 0, cfg.get("dollars_per_r", 500),
                                         entry, stop)
        if qty < 1:
            why = f"qty < 1 at size_r={p.get('size_r')}"
            ledger.record_deferred_placement(con, d, p["pick_id"], ticker, "skipped", why)
            out["skipped"].append(f"{ticker}: {why}")
            continue
        send_as = kind
        if kind == "breakout" and px is not None and ((long and px >= entry) or
                                                      (not long and px <= entry)):
            # Already through the trigger when the window opened. The simulator
            # fills a gapped breakout at that bar's open; Alpaca refuses a stop
            # already crossed. So it enters at market now, same bracket -- or,
            # past its target, not at all (the simulator: no reward left).
            if (long and px >= target) or (not long and px <= target):
                why = (f"price {px:.2f} already past the target {target:.2f} when the window "
                       f"opened (simulated as invalid_fill, no reward left)")
                ledger.record_deferred_placement(con, d, p["pick_id"], ticker, "skipped", why)
                out["skipped"].append(f"{ticker}: {why}")
                continue
            send_as = "open"
        side = "buy" if long else "sell"
        order = live_broker.submit_bracket_order(ticker, qty, side, send_as, entry, stop, target,
                                                 cfg=cfg)
        if not order:
            why = (live_broker.last_error() or "no detail")[:400]
            ledger.record_deferred_placement(con, d, p["pick_id"], ticker, "refused", why)
            out["refused"].append(f"{ticker}: {why}")
            log("intraday", f"{ticker}: deferred order refused ({why}) -- retried next tick")
            continue
        if send_as == "breakout" and live_cfg.get("breakout_two_step"):
            order["order_class"] = "stop_then_oco"
        ledger.save_live_order(con, p["pick_id"], order, d, ticker, side, qty, send_as)
        how = kind if send_as == kind else f"{kind} already through its trigger, sent at market"
        ledger.record_deferred_placement(con, d, p["pick_id"], ticker, "placed",
                                         f"{how}; placed at {t.strftime('%H:%M:%S')} ET"
                                         + (f", last {px:.2f}" if px is not None else ""))
        out["placed"] += 1
        log("intraday", f"{ticker}: deferred order placed at its entry window ({how}, "
                        f"qty {qty})")
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
    # A windowed morning pick with neither a live order nor a recorded
    # placement outcome never reached the broker: no intraday tick ran at its
    # entry window. The simulator still graded it, so say so.
    unplaced = []
    try:
        have = {o.get("pick_id") for o in ledger.live_orders_for_date(con, d)}
        tried = ledger.deferred_placements_for_date(con, d)
        max_live = int((cfg.get("live_paper", {}) or {}).get("max_picks_per_day", 4))
        morning = [p for p in picks.values() if p.get("setup_type") != ledger.RUNNER_SETUP]
        for p in sorted(morning, key=lambda x: x["pick_id"])[:max_live]:
            if p.get("entry_not_before") and p["pick_id"] not in have and p["pick_id"] not in tried:
                unplaced.append(p["ticker"])
        if unplaced:
            log("evening", f"WARN live paper: {len(unplaced)} pick(s) never reached the broker "
                           f"(no intraday tick at the entry window): {', '.join(unplaced)} -- "
                           f"the ledger grades them, the paper account does not hold them")
    except Exception as e:  # noqa: BLE001 -- a diagnostic never fails the reconcile
        log("evening", f"WARN could not check deferred placements: {e}")
    return {"reconciled": count, "divergence": divergence, "never_placed": unplaced}