"""
Outcome grading engine — deterministic simulation of each pick against real
(or synthetic, in offline mode) 1-minute bars.

Simulation rules (documented, conservative):
  Entry:
    open      -> fill at official 9:30 open price
    limit     -> long fills when bar.low <= entry (fill at entry, or at open if it
                 gaps through favorably); short mirrored via bar.high
    breakout  -> long fills when bar.high >= entry (stop-order semantics);
                 short mirrored via bar.low
  Exits, evaluated per bar AFTER the fill (and in the fill bar itself):
    - If the stop level is touched, exit at the stop. CONSERVATIVE: if both stop and
      target are touched in the same bar, the stop is assumed to hit first.
    - If target1 is touched, exit at target1.
    - Otherwise exit at the session close (eod).
  Metrics:
    r_multiple = signed P&L / initial risk per share
    mfe_r / mae_r = max favorable / adverse excursion in R while in the trade
    missed_move_r = for UNTRIGGERED picks, what the move from the entry level to the
                    best price would have been worth in R (measures entries that were too cute)
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .providers import MarketData
from .util import log


def _risk_per_share(entry: float, stop: float) -> float:
    return abs(entry - stop)


def simulate_pick(pick: dict, bars: list[dict], daily: dict | None) -> dict:
    """Returns an outcome dict ready for ledger.save_outcome (minus grade fields)."""
    out: dict[str, Any] = {
        "pick_id": pick["pick_id"],
        "triggered": False,
        "entry_fill": None,
        "exit_price": None,
        "exit_reason": "no_trigger",
        "r_multiple": None,
        "mfe_r": None,
        "mae_r": None,
        "missed_move_r": None,
    }
    if daily:
        out.update({
            "day_open": daily["open"], "day_high": daily["high"],
            "day_low": daily["low"], "day_close": daily["close"],
            "day_volume": daily.get("volume"),
        })
    if not bars:
        out["exit_reason"] = "no_data"
        return out

    direction = (pick["direction"] or "long").lower()
    long = direction == "long"
    entry_type = (pick.get("entry_type") or "open").lower()
    entry = pick.get("entry_price")
    stop = pick.get("stop_price")
    target = pick.get("target_price")

    if entry is None or stop is None or target is None:
        out["exit_reason"] = "invalid_plan"
        return out
    risk = _risk_per_share(entry, stop)
    if risk <= 0:
        out["exit_reason"] = "invalid_plan"
        return out

    # ---- find fill -------------------------------------------------------
    fill_price = None
    fill_idx = None
    if entry_type == "open":
        fill_price = bars[0]["o"]
        fill_idx = 0
    else:
        for i, b in enumerate(bars):
            if entry_type == "limit":
                if long and b["l"] <= entry:
                    fill_price = min(entry, b["o"]) if b["o"] < entry else entry
                    fill_idx = i
                    break
                if not long and b["h"] >= entry:
                    fill_price = max(entry, b["o"]) if b["o"] > entry else entry
                    fill_idx = i
                    break
            else:  # breakout (stop-order semantics)
                if long and b["h"] >= entry:
                    fill_price = max(entry, b["o"]) if b["o"] > entry else entry
                    fill_idx = i
                    break
                if not long and b["l"] <= entry:
                    fill_price = min(entry, b["o"]) if b["o"] < entry else entry
                    fill_idx = i
                    break

    if fill_price is None:
        # untriggered: how much did the idea move without us?
        best = max(b["h"] for b in bars) if long else min(b["l"] for b in bars)
        move = (best - entry) if long else (entry - best)
        out["missed_move_r"] = round(move / risk, 2)
        return out

    # Recompute risk off the actual fill (a limit/breakout order can fill at a
    # more favorable price than requested when the market gaps through it).
    # GUARD: if that gap-through fill lands very close to the stop, the
    # fill-to-stop distance collapses toward zero and produces absurd,
    # fantasy R-multiples (e.g. a 3-cent risk against a $12 move -> 300+R).
    # Floor the effective risk at 25% of the ORIGINALLY PLANNED risk so a
    # favorable fill still gets credit (smaller effective risk = bigger R)
    # without letting the denominator run away to near-zero.
    planned_risk = _risk_per_share(entry, stop)
    risk = _risk_per_share(fill_price, stop)
    if risk <= 0:
        risk = planned_risk
    elif planned_risk > 0 and risk < planned_risk * 0.25:
        risk = planned_risk * 0.25

    out["triggered"] = True
    out["entry_fill"] = round(fill_price, 4)

    # ---- walk forward ----------------------------------------------------
    exit_price = None
    exit_reason = "eod"
    best_px = fill_price
    worst_px = fill_price

    for b in bars[fill_idx:]:
        if long:
            worst_px = min(worst_px, b["l"])
            best_px = max(best_px, b["h"])
            if b["l"] <= stop:                # conservative: stop first
                exit_price, exit_reason = stop, "stop"
                if b["o"] < stop:             # gapped through the stop
                    exit_price = b["o"]
                break
            if b["h"] >= target:
                exit_price, exit_reason = target, "target"
                break
        else:
            worst_px = max(worst_px, b["h"])
            best_px = min(best_px, b["l"])
            if b["h"] >= stop:
                exit_price, exit_reason = stop, "stop"
                if b["o"] > stop:
                    exit_price = b["o"]
                break
            if b["l"] <= target:
                exit_price, exit_reason = target, "target"
                break

    if exit_price is None:
        exit_price = bars[-1]["c"]

    sign = 1.0 if long else -1.0
    r_mult = sign * (exit_price - fill_price) / risk
    mfe = sign * (best_px - fill_price) / risk
    mae = sign * (worst_px - fill_price) / risk  # negative or zero

    out.update({
        "exit_price": round(float(exit_price), 4),
        "exit_reason": exit_reason,
        "r_multiple": round(float(r_mult), 3),
        "mfe_r": round(float(mfe), 3),
        "mae_r": round(float(mae), 3),
    })
    return out


def classify_shadow(outcome: dict) -> tuple[str, float | None]:
    """
    Map a simulated rejection outcome to (eod_outcome, forgone_r).

    unmeasurable  -> no intraday data (halt/gap); contributes 0, increments nothing
    scratch       -> untriggered (the trade would not have filled; the filter cost
                     nothing) or |R| <= 0.2 noise
    would_have_won / would_have_lost -> triggered with meaningful signed R
    forgone_r: positive = the filter cost us; negative = the filter saved us.
    """
    if outcome.get("exit_reason") in ("no_data", "invalid_plan"):
        return "unmeasurable", None
    if not outcome.get("triggered"):
        return "scratch", 0.0
    r = outcome.get("r_multiple") or 0.0
    if r >= 0.2:
        return "would_have_won", round(r, 3)
    if r <= -0.2:
        return "would_have_lost", round(r, 3)
    return "scratch", round(r, 3)


def grade_day(md: MarketData, picks: list[dict], d: date) -> list[dict]:
    outcomes = []
    for p in picks:
        try:
            bars = md.minute_bars(p["ticker"], d)
        except Exception as e:
            log("grade", f"minute bars failed for {p['ticker']}: {e}")
            bars = []
        try:
            daily = md.daily_bar(p["ticker"], d)
        except Exception:
            daily = None
        o = simulate_pick(p, bars, daily)
        o["eval_date"] = d
        outcomes.append(o)
        log("grade", f"{p['ticker']} {p['direction']}: triggered={o['triggered']} "
                     f"exit={o['exit_reason']} R={o['r_multiple']}")
    return outcomes


def outcomes_md(picks: list[dict], outcomes: list[dict]) -> str:
    """Render computed outcomes for the evening grader prompt."""
    by_id = {o["pick_id"]: o for o in outcomes}
    lines = []
    for p in picks:
        o = by_id.get(p["pick_id"], {})
        lines.append(f"### {p['ticker']} ({p['direction']}, {p['setup_type']}, pick_id={p['pick_id']})")
        lines.append(f"- Plan: entry {p['entry_type']} @ {p['entry_price']}, stop {p['stop_price']}, "
                     f"target {p['target_price']}, size {p['size_r']}R, conviction {p['conviction']}, "
                     f"time_stop: {p['time_stop']}")
        lines.append(f"- Thesis: {p['thesis']}")
        lines.append(f"- Invalidation: {p['invalidation']}")
        if o.get("triggered"):
            lines.append(f"- OUTCOME: filled @ {o['entry_fill']}, exit @ {o['exit_price']} "
                         f"({o['exit_reason']}), R = {o['r_multiple']:+.2f}, "
                         f"MFE {o['mfe_r']:+.2f}R, MAE {o['mae_r']:+.2f}R")
        else:
            mm = o.get("missed_move_r")
            extra = f" — untriggered; the move from entry level would have been {mm:+.2f}R" if mm is not None else ""
            lines.append(f"- OUTCOME: NOT TRIGGERED ({o.get('exit_reason','no_trigger')}){extra}")
        if o.get("day_open") is not None:
            lines.append(f"- Day OHLC: O {o['day_open']} H {o['day_high']} L {o['day_low']} "
                         f"C {o['day_close']} Vol {o.get('day_volume','?')}")
        lines.append("")
    return "\n".join(lines)
