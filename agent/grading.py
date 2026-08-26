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
  Exits, evaluated per bar AFTER the fill (and in the fill bar itself), in this
  conservative priority order — a bar that satisfies more than one of these
  is resolved by whichever comes FIRST in this list, not by price magnitude:
    1. stop     -> the stop level is touched. If both stop and target are
                    touched in the same bar, the stop is assumed to hit first.
                    A gap through the stop (session open already past it) exits
                    at that bar's OPEN, not at the stop price.
    2. flatten  -> session-end flatten cutoff (config `flatten_at`, e.g.
                    "15:55"), DEFAULT DISABLED (null). When enabled and the
                    position is still open at/after that bar, exit at its
                    close with exit_reason "flatten". Ties against time_stop
                    (same bar, or time_stop landing later) resolve to flatten.
    3. time_stop -> pick.time_stop (e.g. "by 11:00") mapped to a bar index;
                    if reached before stop/flatten/target, exit at that bar's
                    close with exit_reason "time_stop". This is the only
                    default-ON exit rule added by this pass — every prior
                    pick with a time_stop was previously graded against a
                    plan the desk never actually followed intraday.
    4. target1  -> target1 is touched, exit at target1.
    5. eod      -> otherwise exit at the session close.
  Thin-bar filtering (config `min_bar_volume`, default 0 = disabled): bars
  whose volume is below the threshold are excluded from trigger, stop,
  target, and gap-through logic (a quiet, prepopulated bar can fake a
  breakout or a touch) but still count toward MFE/MAE and remain in the bar
  series for indexing and for the eod close — they are not deleted, just
  untrusted for level-touch decisions. Open-type fills are unconditional
  (there is no "trigger" to distrust) and are unaffected by this filter.
  Fill validity (v3): a gap-through fill can land on the WRONG side of its own
  stop -- e.g. a long "limit" order effectively bought at the open because price
  gapped below the limit price, with that open already under the stop. That is
  not a trade, it's a data artifact of the entry_type being mismatched to how
  the level was meant to be approached. Such fills are hard-rejected before any
  risk math: triggered stays False, exit_reason is "invalid_fill", and nothing
  is graded. A softer case -- fill lands on the correct side of the stop but far
  from the planned entry -- still grades normally but is flagged
  execution_anomaly so the evening grader doesn't narrate a thesis test that
  never actually happened at the intended level.
  Metrics:
    r_multiple = signed P&L / initial risk per share
    mfe_r / mae_r = max favorable / adverse excursion in R while in the trade
    missed_move_r = for UNTRIGGERED picks, what the move from the entry level to the
                    best price would have been worth in R (measures entries that were too cute)
    fill_vs_plan_r = |actual fill - planned entry| / planned risk. execution_anomaly
                    is True when this exceeds 0.5 -- flags fills far enough from plan
                    that grading them as a thesis test would be misleading.
  Counterfactual diagnostics (deterministic, no LLM cost — the weekly miner eats these):
    baseline_r = the NULL MODEL: same name, same direction, enter at the session open,
                 exit at the close, measured in units of the PLANNED risk. Computed for
                 every pick, triggered or not. Edge = beating this, not raw positive R.
    eod_r      = for triggered picks: R if the trade had been held from the actual fill
                 to the close with NO stop, NO target, NO time_stop and NO flatten. This
                 counterfactual stays UNCONDITIONED by design — actual_r - eod_r must keep
                 measuring "did active management add value", not get contaminated by the
                 new exit rules above.
    target_hit_after_stop = for stopped-out trades: did price go on to touch the target
                 later the same session? A high rate = stops are placed inside the noise.
  Friction (slippage_bps, default 5):
    Fills and exits are worsened by slippage_bps in the adverse direction on BOTH
    sides of the trade. Zero-friction paper fills systematically overstate edge,
    and the entire learning loop depends on the ledger being honest. Applied
    identically to taken picks and shadow-ledger rejections so comparisons stay fair.
"""
from __future__ import annotations

import re
from datetime import date, time as dtime
from typing import Any

from . import indicators as ind
from .providers import MarketData
from .util import log

_CLOCK_RE = re.compile(r"(\d{1,2}):(\d{2})")

# Bump this whenever simulate_pick's exit-rule semantics change (Task 4).
# Written onto every outcome/rejection row so the weekly miner (and any
# cross-session comparison) can tell rows graded under different rules apart
# instead of silently averaging them together. v1 = pre-Task-3 semantics
# (no time_stop enforcement). v2 = time_stop enforced as a hard exit.
# v3 = fills on the wrong side of their own stop are rejected as
# "invalid_fill" instead of being graded through the risk floor, and
# surviving fills far from the planned entry are flagged execution_anomaly.
CURRENT_SIM_VERSION = 3


def _risk_per_share(entry: float, stop: float) -> float:
    return abs(entry - stop)


def _parse_clock(value: Any) -> dtime | None:
    """Parse a clock-time string into a datetime.time, or None if the value
    doesn't contain one.

    `time_stop` is documented (prompts/morning_analyst.md) as an intraday
    clock time like "by 11:00" or "by 14:30" — this desk is day-trade only.
    Older/stale data may carry a different shape (e.g. a legacy "2 days"
    duration string); any value with no HH:MM in it is unparseable and the
    caller falls back to "no time stop" rather than crashing the evening loop.
    """
    m = _CLOCK_RE.search(str(value))
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    return dtime(hh, mm)


def _clock_to_bar_index(target_t: dtime, bars: list[dict]) -> int | None:
    """Map a clock time to the index of the first bar at/after that time.

    Prefers each bar's own timestamp (bar["t"], populated by every real
    MarketData provider — Alpaca, yfinance, and the offline synthetic
    generator all set it). Falls back to a positional offset from bars[0]
    ONLY when bars carry no timestamp at all, assuming consecutive 1-minute
    bars starting at the 9:30 ET session open (documented fallback — note
    this assumption does not hold if a future provider returns bars at a
    different cadence or start time without timestamps).
    """
    if bars and bars[0].get("t") is not None:
        for i, b in enumerate(bars):
            t = b["t"]
            bar_t = t.time() if hasattr(t, "time") else t
            if bar_t >= target_t:
                return i
        return None
    open_t = dtime(9, 30)
    offset = (target_t.hour * 60 + target_t.minute) - (open_t.hour * 60 + open_t.minute)
    if offset < 0 or offset >= len(bars):
        return None
    return offset


def _is_thin(bar: dict, min_bar_volume: float) -> bool:
    """True if `bar` should be distrusted for trigger/stop/target/gap logic
    under the thin-bar filter (config min_bar_volume, default 0 = disabled).
    A bar with no volume field at all is never treated as thin — we don't
    invent a value, we just can't apply the filter to it."""
    if not min_bar_volume:
        return False
    v = bar.get("v")
    if v is None:
        return False
    return v < min_bar_volume


def _resample_5m(bars: list[dict]) -> list[dict]:
    """Positionally bucket 1-minute bars into 5-minute candles (o/h/l/c/v +
    the bucket's opening timestamp). Assumes contiguous 1-minute bars, same
    assumption _clock_to_bar_index's fallback path makes elsewhere in this
    module. Used only for the 9 EMA (spec: 5m timeframe -- 1m is too noisy
    for a process metric on an intraday desk)."""
    out: list[dict] = []
    for i in range(0, len(bars), 5):
        chunk = bars[i:i + 5]
        if not chunk:
            continue
        out.append({
            "t": chunk[0].get("t"),
            "o": chunk[0]["o"],
            "h": max(b["h"] for b in chunk),
            "l": min(b["l"] for b in chunk),
            "c": chunk[-1]["c"],
            "v": sum(b.get("v") or 0 for b in chunk),
        })
    return out


def _indicator_fields(bars: list[dict], adv20: float | None) -> tuple[dict, list[float]]:
    """Session-level indicator fields computed from whatever bars a pick has
    (regardless of trigger status) -- VWAP events + RVOL-at-checkpoint. Never
    fabricates a value: any field this can't compute from the bars/adv20 on
    hand stays None (spec §0.4/§9 — never fail a grading run, never guess).
    Returns (fields, vwap_series) — the caller reuses the series for the
    fill-dependent fields (vwap_at_fill etc.) instead of recomputing it."""
    out: dict[str, Any] = {
        "adv20": round(adv20) if adv20 else None,
        "first_vwap_reclaim_et": None, "first_vwap_reject_et": None,
        "vwap_crosses": None, "pct_time_above_vwap": None,
        "rvol_at_1000": None, "rvol_at_1200": None, "rvol_close": None,
    }
    if not bars:
        return out, []
    vwap = ind.vwap_series(bars)
    ev = ind.vwap_events(bars, vwap)
    out["first_vwap_reclaim_et"] = ev["first_reclaim_et"]
    out["first_vwap_reject_et"] = ev["first_reject_et"]
    out["vwap_crosses"] = ev["crosses"]
    out["pct_time_above_vwap"] = ev["pct_time_above"]

    for key, checkpoint in (("rvol_at_1000", dtime(10, 0)), ("rvol_at_1200", dtime(12, 0))):
        idx = _clock_to_bar_index(checkpoint, bars)
        if idx is not None:
            cum_vol = sum(b.get("v") or 0 for b in bars[: idx + 1])
            rv = ind.rvol_simple(cum_vol, adv20)
            out[key] = round(rv, 3) if rv is not None else None

    cum_vol_close = sum(b.get("v") or 0 for b in bars)
    rv_close = ind.rvol_simple(cum_vol_close, adv20)
    out["rvol_close"] = round(rv_close, 3) if rv_close is not None else None
    return out, list(vwap)


def simulate_pick(pick: dict, bars: list[dict], daily: dict | None,
                  slippage_bps: float = 0.0, flatten_at: str | None = None,
                  min_bar_volume: float = 0, adv20: float | None = None) -> dict:
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
        "baseline_r": None,
        "eod_r": None,
        "target_hit_after_stop": None,
        "execution_anomaly": False,
        "fill_vs_plan_r": None,
        "sim_version": CURRENT_SIM_VERSION,
        # indicator layer (specs/INDICATOR_LAYER_SPEC_v1.md §4) — deterministic,
        # checkpoint-accurate; session-level fields below apply whether or not
        # the pick triggered, fill-dependent ones only when it did (see end).
        "adv20": None,
        "first_vwap_reclaim_et": None, "first_vwap_reject_et": None,
        "vwap_crosses": None, "pct_time_above_vwap": None,
        "rvol_at_1000": None, "rvol_at_1200": None, "rvol_close": None,
        "vwap_at_fill": None, "fill_vs_vwap_pct": None, "vwap_side_at_fill": None,
        "ema9_5m_loss_et": None,
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

    try:
        ind_fields, vwap = _indicator_fields(bars, adv20)
        out.update(ind_fields)
    except Exception as e:
        log("grade", f"pick {pick.get('pick_id')}: indicator computation failed ({e}) — fields left null")
        vwap = []

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

    # ---- null model: open->close in the pick's direction, in planned-risk units.
    # Computed for EVERY pick (triggered or not) so "edge" always has a benchmark:
    # a plan that underperforms naively holding the same name is not edge.
    sess_open = daily["open"] if daily else bars[0]["o"]
    sess_close = daily["close"] if daily else bars[-1]["c"]
    if sess_open is not None and sess_close is not None:
        naive_sign = 1.0 if (pick["direction"] or "long").lower() == "long" else -1.0
        out["baseline_r"] = round(naive_sign * (sess_close - sess_open) / risk, 3)

    # ---- find fill -------------------------------------------------------
    fill_price = None
    fill_idx = None
    if entry_type == "open":
        fill_price = bars[0]["o"]
        fill_idx = 0
    else:
        for i, b in enumerate(bars):
            if _is_thin(b, min_bar_volume):
                continue  # thin-bar filter: don't trust a quiet bar to trigger entry
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

    fill_time = bars[fill_idx].get("t")

    # Friction: worsen the fill in the adverse direction before computing risk.
    slip = float(slippage_bps) / 10000.0
    if slip > 0:
        fill_price = fill_price * (1 + slip) if long else fill_price * (1 - slip)

    # ---- hard validity gate ------------------------------------------------
    # A gap-through fill can land on the WRONG side of its own stop (e.g. a
    # long "limit" order effectively bought at the open because price gapped
    # below the limit price, with the open itself already under the stop).
    # abs(entry - stop) below only measures DISTANCE, not direction, so it
    # cannot catch this -- it must be checked explicitly, before any risk
    # math, or a structurally impossible trade slips through as a small,
    # plausible-looking loss (see the risk-floor guard's history below).
    if (long and fill_price <= stop) or (not long and fill_price >= stop):
        out["entry_fill"] = round(fill_price, 4)
        out["exit_reason"] = "invalid_fill"
        out["invalid_fill_detail"] = (
            f"{'long' if long else 'short'} fill {fill_price:.4f} landed on the wrong side of "
            f"stop {stop:.4f} (planned entry {entry:.4f}, type {entry_type}) -- not a real trade"
        )
        log("grade", f"pick {pick.get('pick_id')}: INVALID FILL -- {out['invalid_fill_detail']}")
        return out

    # Flag (but don't reject) a fill that landed far from the planned entry,
    # in units of the ORIGINALLY PLANNED risk. Still a real, valid trade --
    # the stop wasn't breached -- but the evening grader must not narrate a
    # thesis test at a level the trade never actually filled near.
    planned_risk = _risk_per_share(entry, stop)
    if planned_risk > 0:
        out["fill_vs_plan_r"] = round(abs(fill_price - entry) / planned_risk, 2)
        out["execution_anomaly"] = out["fill_vs_plan_r"] > 0.5

    # Recompute risk off the actual fill (a limit/breakout order can fill at a
    # more favorable price than requested when the market gaps through it).
    # GUARD: if that gap-through fill lands very close to the stop, the
    # fill-to-stop distance collapses toward zero and produces absurd,
    # fantasy R-multiples (e.g. a 3-cent risk against a $12 move -> 300+R).
    # Floor the effective risk at 25% of the ORIGINALLY PLANNED risk so a
    # favorable fill still gets credit (smaller effective risk = bigger R)
    # without letting the denominator run away to near-zero. The hard gate
    # above already ruled out the case where this fill is on the wrong side
    # of the stop entirely, so what's left here is always a genuine trade.
    risk = _risk_per_share(fill_price, stop)
    if risk <= 0:
        risk = planned_risk
    elif planned_risk > 0 and risk < planned_risk * 0.25:
        risk = planned_risk * 0.25

    out["triggered"] = True
    out["entry_fill"] = round(fill_price, 4)
    out["fill_time"] = fill_time

    # ---- fill-dependent indicator fields (spec §4) ------------------------
    # Only meaningful once we know there IS a real fill (the invalid_fill gate
    # above already returned for fills on the wrong side of their own stop).
    try:
        if fill_idx < len(vwap):
            fill_vwap = vwap[fill_idx]
            out["vwap_at_fill"] = round(fill_vwap, 4)
            out["fill_vs_vwap_pct"] = round(ind.vwap_distance(fill_price, fill_vwap), 3)
            out["vwap_side_at_fill"] = "above" if fill_price >= fill_vwap else "below"
        bars5 = _resample_5m(bars)
        ema9_5m = ind.ema_series([b["c"] for b in bars5], 9)
        fill5_idx = fill_idx // 5
        out["ema9_5m_loss_et"] = ind.ema_loss_time(bars5[fill5_idx:], ema9_5m[fill5_idx:], direction)
    except Exception as e:
        log("grade", f"pick {pick.get('pick_id')}: fill-indicator computation failed ({e}) — fields left null")

    # ---- map time_stop / flatten_at to bar indices, once, before the walk ----
    time_stop_idx = None
    time_stop_raw = pick.get("time_stop")
    if time_stop_raw:
        parsed = _parse_clock(time_stop_raw)
        if parsed is None:
            log("grade", f"pick {pick.get('pick_id')}: unparseable time_stop "
                         f"{time_stop_raw!r}; simulating without a time stop")
        else:
            time_stop_idx = _clock_to_bar_index(parsed, bars)

    flatten_idx = None
    if flatten_at:
        parsed = _parse_clock(flatten_at)
        if parsed is None:
            log("grade", f"unparseable flatten_at config value {flatten_at!r}; "
                         f"flatten disabled for this simulation")
        else:
            flatten_idx = _clock_to_bar_index(parsed, bars)

    # ---- walk forward ----------------------------------------------------
    exit_price = None
    exit_reason = "eod"
    exit_idx = len(bars) - 1
    best_px = fill_price
    worst_px = fill_price

    for i in range(fill_idx, len(bars)):
        b = bars[i]
        thin = _is_thin(b, min_bar_volume)
        if long:
            worst_px = min(worst_px, b["l"])
            best_px = max(best_px, b["h"])
            if not thin and b["l"] <= stop:    # conservative: stop first
                exit_price, exit_reason, exit_idx = stop, "stop", i
                if b["o"] < stop:             # gapped through the stop
                    exit_price = b["o"]
                break
            if flatten_idx is not None and i >= flatten_idx:
                # flatten wins ties against time_stop (checked first, below)
                exit_price, exit_reason, exit_idx = b["c"], "flatten", i
                break
            if time_stop_idx is not None and i >= time_stop_idx:
                exit_price, exit_reason, exit_idx = b["c"], "time_stop", i
                break
            if not thin and b["h"] >= target:
                exit_price, exit_reason, exit_idx = target, "target", i
                break
        else:
            worst_px = max(worst_px, b["h"])
            best_px = min(best_px, b["l"])
            if not thin and b["h"] >= stop:
                exit_price, exit_reason, exit_idx = stop, "stop", i
                if b["o"] > stop:
                    exit_price = b["o"]
                break
            if flatten_idx is not None and i >= flatten_idx:
                exit_price, exit_reason, exit_idx = b["c"], "flatten", i
                break
            if time_stop_idx is not None and i >= time_stop_idx:
                exit_price, exit_reason, exit_idx = b["c"], "time_stop", i
                break
            if not thin and b["l"] <= target:
                exit_price, exit_reason, exit_idx = target, "target", i
                break

    if exit_price is None:
        exit_price = bars[-1]["c"]
    exit_time = bars[exit_idx].get("t")

    # Friction: worsen the exit in the adverse direction too.
    if slip > 0:
        exit_price = exit_price * (1 - slip) if long else exit_price * (1 + slip)

    sign = 1.0 if long else -1.0
    r_mult = sign * (exit_price - fill_price) / risk
    mfe = sign * (best_px - fill_price) / risk
    mae = sign * (worst_px - fill_price) / risk  # negative or zero

    # ---- management counterfactuals (same risk denominator as r_multiple) ----
    # eod_r: hold from the actual fill to the close, no stop, no target.
    eod_exit = bars[-1]["c"]
    if slip > 0:
        eod_exit = eod_exit * (1 - slip) if long else eod_exit * (1 + slip)
    out["eod_r"] = round(sign * (eod_exit - fill_price) / risk, 3)

    # target_hit_after_stop: only meaningful for stopped trades. Scans from the
    # exit bar onward (the same-bar case counts, consistent with the conservative
    # stop-first tie rule above).
    if exit_reason == "stop":
        hit = False
        for b in bars[exit_idx:]:
            if (long and b["h"] >= target) or (not long and b["l"] <= target):
                hit = True
                break
        out["target_hit_after_stop"] = hit

    out.update({
        "exit_price": round(float(exit_price), 4),
        "exit_reason": exit_reason,
        "exit_time": exit_time,
        "r_multiple": round(float(r_mult), 3),
        "mfe_r": round(float(mfe), 3),
        "mae_r": round(float(mae), 3),
    })
    return out


def classify_shadow(outcome: dict) -> tuple[str, float | None]:
    """
    Map a simulated rejection outcome to (eod_outcome, forgone_r).

    unmeasurable  -> no intraday data (halt/gap) or an invalid_fill artifact
                     (fill landed on the wrong side of its own stop -- not a
                     real trade the filter could have avoided or caused);
                     contributes 0, increments nothing
    scratch       -> untriggered (the trade would not have filled; the filter cost
                     nothing) or |R| <= 0.2 noise
    would_have_won / would_have_lost -> triggered with meaningful signed R
    forgone_r: positive = the filter cost us; negative = the filter saved us.
    """
    if outcome.get("exit_reason") in ("no_data", "invalid_plan", "invalid_fill"):
        return "unmeasurable", None
    if not outcome.get("triggered"):
        return "scratch", 0.0
    r = outcome.get("r_multiple") or 0.0
    if r >= 0.2:
        return "would_have_won", round(r, 3)
    if r <= -0.2:
        return "would_have_lost", round(r, 3)
    return "scratch", round(r, 3)


def grade_day(md: MarketData, picks: list[dict], d: date,
              slippage_bps: float = 0.0, flatten_at: str | None = None,
              min_bar_volume: float = 0, adv20_map: dict[str, float] | None = None) -> list[dict]:
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
        o = simulate_pick(p, bars, daily, slippage_bps=slippage_bps,
                          flatten_at=flatten_at, min_bar_volume=min_bar_volume,
                          adv20=(adv20_map or {}).get(p["ticker"].upper()))
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
            times = f" [{o['fill_time']} -> {o['exit_time']}]" if o.get("fill_time") and o.get("exit_time") else ""
            lines.append(f"- OUTCOME: filled @ {o['entry_fill']}, exit @ {o['exit_price']} "
                         f"({o['exit_reason']}), R = {o['r_multiple']:+.2f}, "
                         f"MFE {o['mfe_r']:+.2f}R, MAE {o['mae_r']:+.2f}R{times}")
            if o.get("execution_anomaly"):
                lines.append(
                    f"  ⚠ EXECUTION ANOMALY: fill landed {o['fill_vs_plan_r']:.2f}R away from the "
                    f"planned entry ({p['entry_price']}) — the thesis was never actually tested at "
                    f"the intended level. Grade EXECUTION ONLY for this pick; do not extract a "
                    f"thesis lesson or confirm/contradict a setup rule from it.")
            if o.get("vwap_side_at_fill"):
                bits = [f"filled {o['vwap_side_at_fill']} VWAP ({o['fill_vs_vwap_pct']:+.1f}%)",
                        f"session {(o.get('pct_time_above_vwap') or 0) * 100:.0f}% time above VWAP",
                        f"{o.get('vwap_crosses', 0)} VWAP cross(es)"]
                if o.get("rvol_at_1000") is not None:
                    bits.append(f"RVOL@10:00 {o['rvol_at_1000']:.2f}x ADV20")
                if o.get("rvol_close") is not None:
                    bits.append(f"RVOL@close {o['rvol_close']:.2f}x ADV20")
                if o.get("ema9_5m_loss_et"):
                    bits.append(f"lost 9EMA(5m) at {o['ema9_5m_loss_et']}")
                lines.append(f"  Indicators (mechanically computed — cite these numbers, "
                            f"never a guess): {', '.join(bits)}")
        elif o.get("exit_reason") == "invalid_fill":
            lines.append(
                f"- OUTCOME: INVALID FILL — {o.get('invalid_fill_detail', 'fill landed on the wrong side of the stop; not a real trade.')} "
                f"This is an execution-simulation artifact, not a trade outcome. Do not grade it, "
                f"do not construct a narrative to explain it, and do not extract a lesson from it.")
        else:
            mm = o.get("missed_move_r")
            extra = f" — untriggered; the move from entry level would have been {mm:+.2f}R" if mm is not None else ""
            lines.append(f"- OUTCOME: NOT TRIGGERED ({o.get('exit_reason','no_trigger')}){extra}")
        if o.get("day_open") is not None:
            lines.append(f"- Day OHLC: O {o['day_open']} H {o['day_high']} L {o['day_low']} "
                         f"C {o['day_close']} Vol {o.get('day_volume','?')}")
        lines.append("")
    return "\n".join(lines)
