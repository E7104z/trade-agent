"""
Outcome grading engine — deterministic simulation of each pick against real
(or synthetic, in offline mode) 1-minute bars.

Simulation rules (documented, conservative):
  Entry:
    open      -> fill at official 9:30 open price
    limit     -> a RESTING limit fills only when price trades THROUGH it (sim_version
                 8): long when bar.low <= entry - need, short when bar.high >= entry
                 + need, where need = max(LIMIT_FILL_TICK, entry * LIMIT_FILL_CUSHION_BPS
                 / 1e4); fill at entry. A bar that only touches the limit, or goes
                 through it by less than `need`, is NOT a fill (counted in
                 touch_only_bars). A bar that OPENS already through the limit fills at
                 that open (the order is marketable), exactly as before.
    breakout  -> long fills when bar.high >= entry (stop-order semantics);
                 short mirrored via bar.low. Touch triggers a stop, so no cushion.
    A limit/breakout entry that has not filled by the pick's time_stop (or the
    flatten cutoff, whichever is earlier) is CANCELLED: no bar at or after that
    cutoff may fill it (sim_version 4). The plan's "by 11:00" is the window the
    trade exists in, and the live monitor cancels unfilled entries at the same
    cutoff; before v4 the simulator filled such entries anyway and then exited
    them on the fill bar's close as a "time_stop" -- a trade that never existed,
    counted as triggered. The untriggered pick is tagged
    time_stop_expired_pretrigger when price reached the trigger only after the
    cutoff. `open` entries fill at bars[0] unconditionally, as before.
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
  is graded. The mirror case -- a fill already at or past the TARGET, so the
  plan has no reward left -- is invalid_fill too (v4); before, the exit walk
  booked it as an immediate "target" exit at a loss. A softer case -- fill lands on the correct side of the stop but far
  from the planned entry -- still grades normally but is flagged
  execution_anomaly so the evening grader doesn't narrate a thesis test that
  never actually happened at the intended level.
  Metrics:
    r_planned = signed P&L from the WORSE of the planned entry and the actual fill
                (friction included), over PLANNED risk per share (|entry - stop|,
                never floored). This is the honest realized R the weekly miner
                reports. A favorable fill is never credited (a target exit scores
                at most the plan's reward:risk), and an adverse fill -- a market
                or gap-through entry above a long's plan -- is never hidden.
                Fill drift is auditable in fill_vs_plan_r / execution_anomaly.
    r_multiple = signed P&L / effective risk per share (fill-to-stop, floored at
                 25% of planned risk) -- diagnostic only; INFLATED (up to ~4x) on
                 favorable gap-through fills, so never the headline number.
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
                 new exit rules above. In effective (floored) risk units; see
                 eod_r_planned for the planned-risk twin.
    eod_r_planned = the same hold-to-close counterfactual in PLANNED-risk units, so the
                 management edge (r_planned - eod_r_planned) never mixes denominators.
    target_hit_after_stop = for stopped-out trades: did price go on to touch the target
                 later the same session? A high rate = stops are placed inside the noise.
  Friction (slippage_bps, default 5):
    Fills and exits are worsened by slippage_bps in the adverse direction on BOTH
    sides of the trade. Zero-friction paper fills systematically overstate edge,
    and the entire learning loop depends on the ledger being honest. Applied
    identically to taken picks and shadow-ledger rejections so comparisons stay fair.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time as dtime, timedelta
from typing import Any

from . import indicators as ind
from .providers import MarketData
from .util import is_half_day, log, market_close_et

_CLOCK_RE = re.compile(r"(\d{1,2}):(\d{2})")

# Bump this whenever simulate_pick's exit-rule semantics change (Task 4).
# Written onto every outcome/rejection row so the weekly miner (and any
# cross-session comparison) can tell rows graded under different rules apart
# instead of silently averaging them together. v1 = pre-Task-3 semantics
# (no time_stop enforcement). v2 = time_stop enforced as a hard exit.
# v3 = fills on the wrong side of their own stop are rejected as
# "invalid_fill" instead of being graded through the risk floor, and
# surviving fills far from the planned entry are flagged execution_anomaly.
# v4 = a limit/breakout entry cannot fill at or after the pick's time_stop /
# flatten cutoff (the order is cancelled there, as the live monitor does);
# a fill already at/past the target is invalid_fill (no reward left -- it was
# booked as a "target" exit at a loss).
# v5 = minute bars end at the last REGULAR bar (15:59 / 12:59): Alpaca's
# inclusive `end` had admitted the 16:00 after-hours print, whose close was
# the "eod" exit of every hold-to-close trade (on an early close, every bar
# 13:00-16:00); and r_planned is scored from the worse of the planned entry
# and the fill, so an adverse market or gap-through fill is never hidden.
# v6 = a rule for the retired runner lane only (a minimum-remaining-reward
# gate, since deleted with that lane). Morning and shadow semantics were
# UNCHANGED from v5, so a morning row graded under v5 is equivalent to one
# graded under v6 (SAME_SEMANTICS).
# v7 = EVERY lane: the session exit is the live desk's. live_monitor flattens
# every open position EOD_FLATTEN_MINUTES before the close (15:55; 12:55 on an
# early close) and cancels any entry still unfilled; the simulator held to the
# last regular bar's close (15:59), so every hold-to-close trade in the ledger
# carried four minutes the paper account never held. Now a position still open
# at that bar exits at its OPEN (when the monitor's market order goes in), no
# entry may fill at or after it, and the hold-to-close counterfactual (eod_r)
# ends there too. The open->close null model (baseline_r) is unchanged.
# v8 = a resting LIMIT entry fills only when the bar trades through the limit by
# max(1 tick, 5 bps) -- a touch is not a fill (docs/FIX_PHASE0.md, D2). Alpaca's
# paper account did not fill RKLB's 2026-10-15 sell limit at 54.00 although the
# 11:15 bar printed 54.02 (3.7 bps through); across its 35 limit-entry and 10
# stop-entry orders the smallest filled limit was 5.07 bps through (NVDA
# 2026-10-08), and a touch preceded the fill by 1-4 minutes twice (MAT, ALEC).
# Stops, targets and breakout entries still trigger on a touch (67 of 68 filled
# stop legs did, within a minute). Gap-through-at-open is unchanged. Outcomes
# record touch_only_bars. A v7 row is therefore NOT equivalent to a v8 row for
# any lane (SAME_SEMANTICS). v8 also records r_if_held_to_flatten, a reference
# number that changes no fill and no R.
CURRENT_SIM_VERSION = 8

# How far a bar must trade through a resting limit for the order to count as
# filled: the larger of one tick and this many basis points of the limit price.
# 5 bps is also the configured friction (slippage_bps): the market has to pay
# at least what the simulator charges. Part of the sim version: change it only
# with a bump of CURRENT_SIM_VERSION.
LIMIT_FILL_CUSHION_BPS = 5.0
LIMIT_FILL_TICK = 0.01

# Minutes before the session close at which the live monitor flattens every
# position (and the simulator exits it). The single source: live_monitor
# imports it from here.
EOD_FLATTEN_MINUTES = 5

# A taken trade's result inside +/-SCRATCH_R is a SCRATCH: neither a win nor a
# loss. The simulator charges slippage_bps (5) a side; on a typical morning
# stop ~2% from entry, that round trip alone is 10 bps / 2% = 0.05R, so a
# result inside the band is flat at the desk's own cost of trading. ALEC on
# 2026-10-05 exited at +0.01R -- $0.0028 a share on a $2 stock, inside the
# spread -- and was counted a winner in every cell it touched. Only what is
# CALLED a win or a loss changes: mean R, the sequential tests and the FDR
# batch read R itself and are untouched. (The shadow ledger keeps its own,
# wider +/-0.2R band for "meaningful forgone R"; see classify_shadow.)
SCRATCH_R = 0.05


def outcome_class(r: float | None) -> str | None:
    """'win', 'loss' or 'scratch' for a realized R; None when unmeasured."""
    if r is None:
        return None
    if r > SCRATCH_R:
        return "win"
    if r < -SCRATCH_R:
        return "loss"
    return "scratch"


# Versions whose semantics are identical FOR A LANE. Used where a stored
# version is compared to the running one (the chart drawer's replay guard),
# so a v6 bump that changed nothing for the morning lane does not flag every
# morning chart as "graded under another simulator".
SAME_SEMANTICS = {"morning": frozenset({8}), "shadow": frozenset({8}),
                  "live": frozenset({8})}


# The morning lane's entry window: "no entries in the first 15 minutes after
# the open" (prompts/morning_analyst.md), as a time. Stamped on each limit /
# breakout pick, rejection and paired-arm pick by the morning run (config
# `morning_entry_not_before`), read by simulate_pick and by the live placer
# (live_monitor.place_deferred_orders), which submits those orders then.
MORNING_ENTRY_NOT_BEFORE = "09:45"


def entry_not_before_for(entry_type: str | None, window: str | None) -> str | None:
    """The entry window a pick of `entry_type` carries: `window` for a limit
    or breakout order, None for an `open` entry (the opening print is the
    point of an open entry, and it is placed pre-market)."""
    kind = str(entry_type or "open").lower()
    if kind not in ("limit", "breakout") or not window:
        return None
    return window if _parse_clock(window) is not None else None


def same_semantics(stored_version, lane: str | None) -> bool:
    """True when a row graded under `stored_version` was graded by rules
    identical, for `lane`, to the running simulator's."""
    if stored_version is None:
        return True
    if stored_version == CURRENT_SIM_VERSION:
        return True
    return stored_version in SAME_SEMANTICS.get(lane or "", frozenset()) \
        and CURRENT_SIM_VERSION in SAME_SEMANTICS.get(lane or "", frozenset())


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


def _index_of_timestamp(bars: list[dict], ts) -> int | None:
    """First bar index whose timestamp is >= `ts` (tz-normalized comparison).

    Used to map a fill — which lives at some bar in the SLICED (entry-time)
    series — back to its position in the FULL session series, so
    session-anchored fields (VWAP, RVOL checkpoints) read the right bar even
    when the fill simulation walks an entry-time suffix. `>=` (not equality)
    so a fill whose bar has no exact timestamp match still resolves to the
    first bar at or after its time.
    """
    if ts is None:
        return None
    ts_n = ts.replace(tzinfo=None) if getattr(ts, "tzinfo", None) is not None else ts
    for i, b in enumerate(bars):
        bt = b.get("t")
        if bt is None:
            continue
        bt_n = bt.replace(tzinfo=None) if getattr(bt, "tzinfo", None) is not None else bt
        if bt_n >= ts_n:
            return i
    return None


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


def _limit_need(entry: float) -> float:
    """How far a bar must trade THROUGH a resting limit for it to count as
    filled (sim_version 8): one tick, or LIMIT_FILL_CUSHION_BPS of the price,
    whichever is larger. See the v8 note on CURRENT_SIM_VERSION for the broker
    evidence behind the number."""
    return max(LIMIT_FILL_TICK, abs(float(entry)) * LIMIT_FILL_CUSHION_BPS / 1e4)


def _limit_fills_on_bar(b: dict, entry: float, long: bool) -> bool:
    """True when this bar fills a resting limit: the bar opens already through
    the limit (the order is marketable: it fills at that open), or trades
    through it by at least `_limit_need`. A touch is not a fill."""
    if long:
        return b["o"] < entry or b["l"] <= entry - _limit_need(entry)
    return b["o"] > entry or b["h"] >= entry + _limit_need(entry)


def _limit_touched(b: dict, entry: float, long: bool) -> bool:
    """True when this bar reached the limit at all."""
    return b["l"] <= entry if long else b["h"] >= entry


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


NEAR_TRIGGER_R = 0.25


def _closest_approach(bars, entry, long, entry_type, min_bar_volume=0):
    """How far price finished from firing the entry mechanic, in price terms.

    Returns (shortfall, approach_price). `shortfall` is POSITIVE when the
    trigger was never reached -- the distance price still had to travel --
    and <= 0 when it was reached (which the caller will not see, since it
    only calls this for untriggered picks).

    Thin bars are excluded exactly as the fill loop excludes them, so a
    trigger that was only "reached" on a bar the fill logic refused to trust
    is not counted as reached here either.
    """
    usable = [b for b in bars if not _is_thin(b, min_bar_volume)]
    if not usable:
        return None, None

    if entry_type == "limit":
        # long limit fills on a dip THROUGH entry; short limit on a pop through
        # it (sim_version 8). The shortfall is measured to the level a bar has
        # to reach for the order to fill, so a bar that only touched the limit
        # is still short of the trigger (a small negative trigger_gap_r).
        need = _limit_need(entry)
        if long:
            approach = min(b["l"] for b in usable)
            return approach - (entry - need), approach
        approach = max(b["h"] for b in usable)
        return (entry + need) - approach, approach
    # breakout (stop-order semantics), and anything else that needs price to
    # travel TOWARD the entry in the trade's direction
    if long:
        approach = max(b["h"] for b in usable)
        return entry - approach, approach
    approach = min(b["l"] for b in usable)
    return approach - entry, approach


def _would_have_triggered_after(bars, entry, long, entry_type, cutoff_idx,
                                min_bar_volume=0):
    """True if the entry mechanic WOULD have fired on a bar at or after
    `cutoff_idx` -- i.e. the only thing that stopped it was the cutoff."""
    if cutoff_idx is None or cutoff_idx >= len(bars):
        return False
    for b in bars[cutoff_idx:]:
        if _is_thin(b, min_bar_volume):
            continue
        if entry_type == "limit":
            if _limit_fills_on_bar(b, entry, long):
                return True
        else:
            if (long and b["h"] >= entry) or (not long and b["l"] <= entry):
                return True
    return False


def classify_untriggered(bars, entry, risk, long, entry_type, *,
                         cutoff_idx=None, min_bar_volume=0,
                         near_threshold_r=NEAR_TRIGGER_R):
    """Why a pick never triggered. Deterministic, from bars alone.

    Returns (reason, note, trigger_gap_r) where trigger_gap_r is NEGATIVE by
    how many R price fell short of the trigger.

    The split that matters is between:

      no_setup_offered         price never came near -- the market simply did
                               not present this setup, which says nothing
                               about the rule
      entry_condition_not_met  price came within `near_threshold_r` of firing
                               and the mechanic still did not catch it --
                               the only case that would ever justify
                               loosening an entry condition

    Collapsing these into one "no_trigger" makes a systematically over-tight
    entry indistinguishable from a quiet week.

    `near_threshold_r` is a REPORTING convention, not an edge claim: it draws
    the line for "came close" at a quarter of the trade's own planned risk,
    so the threshold scales with each pick rather than being an absolute
    cent value. trigger_gap_r is stored raw alongside the label so the line
    can be redrawn later without re-simulating anything.
    """
    shortfall, _approach = _closest_approach(bars, entry, long, entry_type,
                                             min_bar_volume)
    if shortfall is None:
        return "other", "no usable (non-thin) bars to evaluate the trigger", None

    gap_r = round(-shortfall / risk, 3) if risk > 0 else None

    # A cutoff that prevented an otherwise-valid trigger is its own category:
    # the setup DID arrive, the rule simply was not allowed to take it.
    if _would_have_triggered_after(bars, entry, long, entry_type, cutoff_idx,
                                   min_bar_volume):
        return "time_stop_expired_pretrigger", None, gap_r

    if gap_r is not None and abs(gap_r) <= near_threshold_r:
        return "entry_condition_not_met", None, gap_r
    return "no_setup_offered", None, gap_r


def simulate_pick(pick: dict, bars: list[dict], daily: dict | None,
                  slippage_bps: float = 0.0, flatten_at: str | None = None,
                  min_bar_volume: float = 0, adv20: float | None = None,
                  atr14: float | None = None, trend_snapshot: dict | None = None,
                  fundamentals: dict | None = None, prev_close: float | None = None,
                  or_minutes: int = 15, session_date: date | None = None,
                  session_bars: list[dict] | None = None) -> dict:
    """Returns an outcome dict ready for ledger.save_outcome (minus grade fields).

    atr14/trend_snapshot/fundamentals/prev_close are the indicator-layer-v2
    grading inputs (agent/loops/evening.py passes these from one batched
    daily_context/fundamentals call per grading run, mirroring how adv20 is
    already threaded through). All are grader INPUT / miner FUEL only — they
    never gate a fill or change R math.

    session_date (Phase 1c) drives the half-day calendar: on a 13:00 ET early
    close, flatten_at/time_stop cutoffs at/after the session close are inert by
    construction and are recorded as such instead of silently never firing.
    When session_date is None (legacy callers), behavior is unchanged and
    session_length stays None.

    session_bars (optional) is the FULL session minute series, used only for
    session-level indicator fields (RVOL checkpoints, VWAP events, opening
    range, gap-fill fractions). It may differ from `bars` when the caller has
    sliced `bars` to an intraday entry time: the fill simulation must not fill
    before the pick existed, but session-level stats must still read the whole
    session. Defaults to `bars`, so behavior is unchanged when no slice
    happened."""
    # Session close for cutoff clamping. Without a session_date we assume the
    # historical behavior: a full 16:00 session.
    close_t = market_close_et(session_date) if session_date else dtime(16, 0)
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
        # Phase 1 instrumentation: why an untriggered pick never triggered.
        # NULL for triggered picks. See agent/migrations_v12.py.
        "untriggered_reason": None,
        "untriggered_note": None,
        "trigger_gap_r": None,
        # sim_version 8: for a LIMIT entry, how many live bars (up to the fill, or
        # the cutoff) reached the limit without trading through it enough to fill
        # it. 0 = none; None = not a limit entry. A pick that never triggered with
        # touch_only_bars >= 1 is a touch the old simulator would have filled.
        "touch_only_bars": None,
        "baseline_r": None,
        "eod_r": None,
        # The time-stop counterfactual (docs/FIX_PHASE0.md, Phase 5): planned-risk R if
        # this fill were held with only its initial stop until the 15:55 flatten. A
        # reference number, never added to r_multiple, r_planned or any total.
        "r_if_held_to_flatten": None,
        "target_hit_after_stop": None,
        "execution_anomaly": False,
        "fill_vs_plan_r": None,
        "sim_version": CURRENT_SIM_VERSION,
        # Phase 1c half-day calendar: a property of the SESSION, set for every
        # outcome (including no_data/invalid_plan) whenever the caller supplies
        # session_date. flatten_inert stays None unless a flatten was configured.
        "session_length": (("half" if is_half_day(session_date) else "full")
                           if session_date else None),
        "flatten_inert": None,
        # indicator layer v1 (specs/INDICATOR_LAYER_SPEC_v1.md §4) — deterministic,
        # checkpoint-accurate; session-level fields below apply whether or not
        # the pick triggered, fill-dependent ones only when it did (see end).
        "adv20": None,
        "first_vwap_reclaim_et": None, "first_vwap_reject_et": None,
        "vwap_crosses": None, "pct_time_above_vwap": None,
        "rvol_at_1000": None, "rvol_at_1200": None, "rvol_close": None,
        "vwap_at_fill": None, "fill_vs_vwap_pct": None, "vwap_side_at_fill": None,
        "ema9_5m_loss_et": None,
        # indicator layer v2 (specs/INDICATOR_LAYER_SPEC_v2.md §5) — ATR/levels/
        # context, computed here or stamped from the caller's fundamentals cache.
        "atr14": None, "stop_dist_atr": None, "target_dist_atr": None, "gap_atr": None,
        "or15_high": None, "or15_low": None, "or_break_dir": None, "or_break_et": None,
        "fill_vs_or": None, "mae_before_mfe": None, "time_to_mfe_min": None,
        "gap_fill_frac_1000": None, "gap_fill_frac_1200": None, "gap_fill_frac_close": None,
        "trend_side_200d": None, "ext_atr_at_entry": None,
        "short_pct_float": None, "float_shares": None, "mkt_cap": None,
    }
    above200 = trend_snapshot.get("above_ma200") if trend_snapshot else None
    out["trend_side_200d"] = "above" if above200 is True else ("below" if above200 is False else None)
    ext = trend_snapshot.get("ext_atr") if trend_snapshot else None
    out["ext_atr_at_entry"] = round(ext, 2) if ext is not None else None
    if fundamentals:
        out["short_pct_float"] = fundamentals.get("short_pct_float")
        out["float_shares"] = fundamentals.get("float_shares")
        out["mkt_cap"] = fundamentals.get("mkt_cap")
    if daily:
        out.update({
            "day_open": daily["open"], "day_high": daily["high"],
            "day_low": daily["low"], "day_close": daily["close"],
            "day_volume": daily.get("volume"),
        })
    if not bars:
        out["exit_reason"] = "no_data"
        return out

    # Session-level indicators describe the WHOLE session and must be computed
    # from the full session bars, not an entry-time suffix that drives fill
    # logic (see _clock_to_bar_index first-match).
    sb = session_bars if session_bars is not None else bars

    try:
        ind_fields, vwap = _indicator_fields(sb, adv20)
        out.update(ind_fields)
    except Exception as e:
        log("grade", f"pick {pick.get('pick_id')}: indicator computation failed ({e}) — fields left null")
        vwap = []

    out["atr14"] = round(atr14, 4) if atr14 else None
    try:
        orr = ind.opening_range(sb, minutes=or_minutes)
        out["or15_high"], out["or15_low"] = orr["or_high"], orr["or_low"]
        out["or_break_dir"], out["or_break_et"] = orr["break_dir"], orr["break_et"]
    except Exception as e:
        log("grade", f"pick {pick.get('pick_id')}: opening-range computation failed ({e}) — fields left null")

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
    sess_open = daily["open"] if daily else sb[0]["o"]
    sess_close = daily["close"] if daily else sb[-1]["c"]
    if sess_open is not None and sess_close is not None:
        naive_sign = 1.0 if (pick["direction"] or "long").lower() == "long" else -1.0
        out["baseline_r"] = round(naive_sign * (sess_close - sess_open) / risk, 3)

    # ---- ATR-relative plan geometry + gap/OR context (indicator layer v2) ----
    # Off the PLANNED entry/stop/target, not the eventual fill — these describe
    # how the plan was constructed, independent of execution quality.
    if atr14:
        out["stop_dist_atr"] = round(abs(entry - stop) / atr14, 2)
        out["target_dist_atr"] = round(abs(target - entry) / atr14, 2)
        if prev_close and sess_open is not None:
            out["gap_atr"] = round(abs(sess_open - prev_close) / atr14, 2)

    # gap_fill_frac_*: fraction of the overnight gap retraced vs prev_close,
    # checkpoint-stamped (never read a later bar into an earlier checkpoint).
    if prev_close and sess_open is not None and sess_open != prev_close:
        gap = sess_open - prev_close
        for key, checkpoint in (("gap_fill_frac_1000", dtime(10, 0)), ("gap_fill_frac_1200", dtime(12, 0))):
            idx = _clock_to_bar_index(checkpoint, sb)
            if idx is not None and idx < len(sb):
                out[key] = round((sess_open - sb[idx]["c"]) / gap, 3)
        out["gap_fill_frac_close"] = round((sess_open - sb[-1]["c"]) / gap, 3)

    # ---- entry cutoff (sim_version 4) ------------------------------------
    # The earliest of time_stop and flatten_at, clamped against the session
    # close exactly as the exit walk clamps them. A resting limit/breakout
    # order is cancelled at this bar: nothing at or after it may fill.
    # ---- session exit (sim_version 7): the live desk's 15:55 flatten ------
    eod_t = (datetime.combine(date.min, close_t)
             - timedelta(minutes=EOD_FLATTEN_MINUTES)).time()
    eod_idx = _clock_to_bar_index(eod_t, bars) if bars and bars[0].get("t") is not None else None

    entry_cutoff_idx = eod_idx   # the monitor cancels unfilled entries there too
    for raw in (pick.get("time_stop"), flatten_at):
        if not raw:
            continue
        parsed = _parse_clock(raw)
        if parsed is None or parsed >= close_t:
            continue
        idx = _clock_to_bar_index(parsed, bars)
        if idx is not None:
            entry_cutoff_idx = idx if entry_cutoff_idx is None else min(entry_cutoff_idx, idx)

    # ---- entry window: the pick's entry_not_before ----------------------
    # A limit/breakout order cannot fill before the time the plan put it live
    # (schema v27: the morning lane's 09:45, "no entries in the first 15
    # minutes"). The live placer submits those orders at that time, so the
    # simulator starts its fill search there too. An `open` entry is the
    # opening print by definition and is never windowed. A row with no
    # entry_not_before (every row before 2026-10-02) grades exactly as before.
    window_idx = 0
    if entry_type != "open" and pick.get("entry_not_before"):
        nb = _parse_clock(pick.get("entry_not_before"))
        if nb is not None:
            idx = _clock_to_bar_index(nb, bars)
            window_idx = idx if idx is not None else len(bars)

    # ---- find fill -------------------------------------------------------
    fill_price = None
    fill_idx = None
    touch_only = 0           # limit entries: live bars that reached the limit but did not fill it
    if entry_type == "open":
        fill_price = bars[0]["o"]
        fill_idx = 0
    else:
        for i, b in enumerate(bars):
            if entry_cutoff_idx is not None and i >= entry_cutoff_idx:
                break  # order cancelled at the cutoff -- see sim_version 4
            if i < window_idx:
                continue  # not live yet -- the order goes in at entry_not_before
            if _is_thin(b, min_bar_volume):
                continue  # thin-bar filter: don't trust a quiet bar to trigger entry
            if entry_type == "limit":
                # A RESTING limit fills on a trade-THROUGH, not a touch (sim_version
                # 8): RKLB's sell limit at 54.00 never filled at the broker on a bar
                # that printed 54.02. A bar that opens through the limit still fills
                # at that open (marketable), as before.
                if _limit_fills_on_bar(b, entry, long):
                    fill_price = ((min(entry, b["o"]) if b["o"] < entry else entry) if long
                                  else (max(entry, b["o"]) if b["o"] > entry else entry))
                    fill_idx = i
                    break
                if _limit_touched(b, entry, long):
                    touch_only += 1
            else:  # breakout (stop-order semantics)
                if long and b["h"] >= entry:
                    fill_price = max(entry, b["o"]) if b["o"] > entry else entry
                    fill_idx = i
                    break
                if not long and b["l"] <= entry:
                    fill_price = min(entry, b["o"]) if b["o"] < entry else entry
                    fill_idx = i
                    break

    if entry_type == "limit":
        out["touch_only_bars"] = touch_only

    if fill_price is None:
        # Measured over the bars the order was LIVE for: a move that happened
        # before entry_not_before is one the plan forbade itself to take.
        live_bars = bars[window_idx:]
        if not live_bars:
            out["untriggered_reason"] = "other"
            out["untriggered_note"] = (f"no bars at/after entry_not_before "
                                       f"{pick.get('entry_not_before')}")
            return out
        # untriggered: how much did the idea move without us?
        best = max(b["h"] for b in live_bars) if long else min(b["l"] for b in live_bars)
        move = (best - entry) if long else (entry - best)
        out["missed_move_r"] = round(move / risk, 2)
        # ...and WHY we never got on. missed_move_r says how far the idea ran
        # without us; it cannot distinguish "the market never offered this
        # setup" from "the setup arrived and our entry mechanic was too
        # narrow". Only the second is a rule defect, and only the second
        # would ever justify loosening an entry condition. The cutoff is the
        # same one the fill search above honored.
        cut = None if entry_cutoff_idx is None else max(0, entry_cutoff_idx - window_idx)
        reason, note, gap_r = classify_untriggered(
            live_bars, entry, risk, long, entry_type,
            cutoff_idx=cut, min_bar_volume=min_bar_volume)
        out["untriggered_reason"] = reason
        out["untriggered_note"] = note
        out["trigger_gap_r"] = gap_r
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
        out["invalid_reason"] = "wrong_side_of_stop"
        out["invalid_fill_detail"] = (
            f"{'long' if long else 'short'} fill {fill_price:.4f} landed on the wrong side of "
            f"stop {stop:.4f} (planned entry {entry:.4f}, type {entry_type}) -- not a real trade"
        )
        log("grade", f"pick {pick.get('pick_id')}: INVALID FILL -- {out['invalid_fill_detail']}")
        return out
    # ...and the mirror case (sim_version 4): a fill already AT or PAST the
    # target leaves no reward in the plan. The exit walk would book it as an
    # immediate "target" exit at a LOSS (selling below the price just paid),
    # and a live bracket with a take-profit on the wrong side of the market is
    # rejected by the broker -- so it is not a trade either. Seen when an open
    # entry gaps over its target.
    reward_left = (target - fill_price) if long else (fill_price - target)
    if reward_left <= 0:
        out["entry_fill"] = round(fill_price, 4)
        out["exit_reason"] = "invalid_fill"
        out["invalid_reason"] = "no_reward_left"
        out["invalid_fill_detail"] = (
            f"{'long' if long else 'short'} fill {fill_price:.4f} is already at/past "
            f"target {target:.4f} (planned entry {entry:.4f}, type {entry_type}) -- "
            f"no reward left in the plan; not a real trade"
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
        # vwap is the session-anchored series (computed off `sb`, the full
        # session). `fill_idx` indexes the SLICED `bars`, so when the two
        # differ, remap the fill to its full-session bar by timestamp before
        # reading the VWAP at that moment.
        fill_vwap_idx = (_index_of_timestamp(sb, fill_time)
                         if session_bars is not None else fill_idx)
        if fill_vwap_idx is not None and fill_vwap_idx < len(vwap):
            fill_vwap = vwap[fill_vwap_idx]
            out["vwap_at_fill"] = round(fill_vwap, 4)
            out["fill_vs_vwap_pct"] = round(ind.vwap_distance(fill_price, fill_vwap), 3)
            out["vwap_side_at_fill"] = "above" if fill_price >= fill_vwap else "below"
        bars5 = _resample_5m(bars)
        ema9_5m = ind.ema_series([b["c"] for b in bars5], 9)
        fill5_idx = fill_idx // 5
        out["ema9_5m_loss_et"] = ind.ema_loss_time(bars5[fill5_idx:], ema9_5m[fill5_idx:], direction)
    except Exception as e:
        log("grade", f"pick {pick.get('pick_id')}: fill-indicator computation failed ({e}) — fields left null")

    if out["or15_high"] is not None and out["or15_low"] is not None:
        if fill_price > out["or15_high"]:
            out["fill_vs_or"] = "above"
        elif fill_price < out["or15_low"]:
            out["fill_vs_or"] = "below"
        else:
            out["fill_vs_or"] = "inside"

    # ---- map time_stop / flatten_at to bar indices, once, before the walk ----
    # Both cutoffs are clamped against the session close (13:00 on a half day,
    # via market_close_et). A cutoff at/after the close cannot fire — the eod
    # exit at the actual close handles the position — and on a half day that
    # inertness is recorded (flatten_inert) instead of passing silently.
    time_stop_idx = None
    time_stop_raw = pick.get("time_stop")
    if time_stop_raw:
        parsed = _parse_clock(time_stop_raw)
        if parsed is None:
            log("grade", f"pick {pick.get('pick_id')}: unparseable time_stop "
                         f"{time_stop_raw!r}; simulating without a time stop")
        elif parsed >= close_t:
            log("grade", f"pick {pick.get('pick_id')}: time_stop {time_stop_raw!r} is at/after "
                         f"the {close_t.strftime('%H:%M')} session close — inert; eod exit applies")
        else:
            time_stop_idx = _clock_to_bar_index(parsed, bars)

    flatten_idx = None
    if flatten_at:
        parsed = _parse_clock(flatten_at)
        if parsed is None:
            log("grade", f"unparseable flatten_at config value {flatten_at!r}; "
                         f"flatten disabled for this simulation")
        elif parsed >= close_t:
            out["flatten_inert"] = True
            log("grade", f"flatten_at {flatten_at!r} is at/after the "
                         f"{close_t.strftime('%H:%M')} session close — flatten rule inert this session")
        else:
            out["flatten_inert"] = False
            flatten_idx = _clock_to_bar_index(parsed, bars)

    # ---- walk forward ----------------------------------------------------
    exit_price = None
    exit_reason = "eod"
    exit_idx = len(bars) - 1
    best_px = fill_price
    worst_px = fill_price
    best_idx = fill_idx
    worst_idx = fill_idx

    for i in range(fill_idx, len(bars)):
        b = bars[i]
        if eod_idx is not None and i >= eod_idx and i > fill_idx:
            # The live monitor's 15:55 flatten: out at this bar's open, before
            # anything inside the bar (sim_version 7).
            exit_price, exit_reason, exit_idx = b["o"], "eod", i
            break
        thin = _is_thin(b, min_bar_volume)
        if long:
            if b["l"] < worst_px:
                worst_px, worst_idx = b["l"], i
            if b["h"] > best_px:
                best_px, best_idx = b["h"], i
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
            if b["h"] > worst_px:
                worst_px, worst_idx = b["h"], i
            if b["l"] < best_px:
                best_px, best_idx = b["l"], i
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
    # r_planned is scored from the WORSE of the planned entry and the actual
    # fill (friction included). A favorable fill is never credited -- the plan
    # bounds the upside -- and an adverse one is never hidden. Scoring from the
    # planned entry alone credited every point a market or gap-through fill
    # paid ABOVE the plan (production 2026-09-21..23: session means of
    # -0.05..-0.47R by the fill printed as +0.32R). Position size is set off the
    # planned risk, so this is the dollars in planned-R units.
    ref = max(entry, fill_price) if long else min(entry, fill_price)
    r_planned = sign * (exit_price - ref) / planned_risk  # planned risk, never floored
    mfe = sign * (best_px - fill_price) / risk
    mae = sign * (worst_px - fill_price) / risk  # negative or zero

    # mae_before_mfe / time_to_mfe_min: mechanical entry-timing-quality read —
    # did the worst print before the best, and how long did the best take.
    out["mae_before_mfe"] = worst_idx <= best_idx
    fill_t, best_t = bars[fill_idx].get("t"), bars[best_idx].get("t")
    if fill_t is not None and best_t is not None:
        out["time_to_mfe_min"] = round((best_t - fill_t).total_seconds() / 60, 1)

    # ---- management counterfactuals (same risk denominator as r_multiple) ----
    # eod_r: hold from the actual fill to the desk's session exit (the 15:55
    # flatten; the last bar's close only when the bars end before it), no
    # stop, no target.
    eod_exit = (bars[eod_idx]["o"] if eod_idx is not None and eod_idx > fill_idx
                else bars[-1]["c"])
    if slip > 0:
        eod_exit = eod_exit * (1 - slip) if long else eod_exit * (1 + slip)
    out["eod_r"] = round(sign * (eod_exit - fill_price) / risk, 3)
    out["eod_r_planned"] = round(sign * (eod_exit - ref) / planned_risk, 3)

    # r_if_held_to_flatten: the same fill held with ONLY the initial stop (no target,
    # no time stop) to the live desk's 15:55 flatten, in the planned-risk R a taken
    # pick is reported in. AMAT 2026-10-16 booked +0.06R at its 14:00 time stop; held,
    # it was +0.57R at 15:55 with the 433 stop never touched. When the real exit WAS the
    # stop or the flatten the two numbers are equal.
    held_exit = None
    for i in range(fill_idx, len(bars)):
        b = bars[i]
        if eod_idx is not None and i >= eod_idx and i > fill_idx:
            held_exit = b["o"]
            break
        if _is_thin(b, min_bar_volume):
            continue
        if long and b["l"] <= stop:
            held_exit = b["o"] if b["o"] < stop else stop      # gapped through: out at the open
            break
        if not long and b["h"] >= stop:
            held_exit = b["o"] if b["o"] > stop else stop
            break
    if held_exit is None:
        held_exit = bars[-1]["c"]
    if slip > 0:
        held_exit = held_exit * (1 - slip) if long else held_exit * (1 + slip)
    out["r_if_held_to_flatten"] = round(sign * (held_exit - ref) / planned_risk, 3)

    # target_hit_after_stop: only meaningful for stopped trades. Scans from the
    # exit bar onward (the same-bar case counts, consistent with the conservative
    # stop-first tie rule above).
    if exit_reason == "stop":
        hit = False
        for b in bars[exit_idx:(eod_idx if eod_idx is not None and eod_idx > exit_idx else len(bars))]:
            if (long and b["h"] >= target) or (not long and b["l"] <= target):
                hit = True
                break
        out["target_hit_after_stop"] = hit

    out.update({
        "exit_price": round(float(exit_price), 4),
        "exit_reason": exit_reason,
        "exit_time": exit_time,
        "r_multiple": round(float(r_mult), 3),
        "r_planned": round(float(r_planned), 3),
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

    forgone_r is the planned-risk R a TAKEN pick is reported in (`realized_r`:
    r_planned, falling back to r_multiple only on a legacy row), so a rejection
    that was also traded is priced in the same unit as its taken twin. It was
    r_multiple, the floored-denominator diagnostic (docs/FIX_PHASE0.md, D4).
    """
    if outcome.get("exit_reason") in ("no_data", "invalid_plan", "invalid_fill"):
        return "unmeasurable", None
    if not outcome.get("triggered"):
        return "scratch", 0.0
    r = realized_r(outcome) or 0.0
    if r >= 0.2:
        return "would_have_won", round(r, 3)
    if r <= -0.2:
        return "would_have_lost", round(r, 3)
    return "scratch", round(r, 3)


# A rejection the desk traded anyway (the coverage lane, the daily floor, a
# hypothesis test) is not a counterfactual: the filters did not stop it. Such a
# row keeps this label, carries no forgone_r, and is reported beside the
# filters' real effect -- 2026-10-15's "-1.41R, the filters saved money" was
# INTC and CMC, both traded by coverage, and 2026-10-16's "+0.62R, the filters
# cost the desk" was AMAT, traded by coverage at +0.06R (docs/FIX_PHASE0.md, D4).
TRADED_VIA_LANE = "traded_via_lane"


def shadow_time_stop(analyst_json: Any, ticker: str, direction: str | None,
                     entry_price: float | None = None) -> str | None:
    """The time stop the ANALYST wrote for the candidate behind a rejection.

    The rejections table never stored one, so the shadow ledger priced every
    rejection as if it held to the close: INTC 2026-10-15 was -1.01R in the
    shadow (stopped out at 15:42) and -0.51R as taken (14:30 time stop). Every
    session's candidates are stored whole in daily_plans.analyst_json, so the
    stop is recovered from there, history included. Matches on ticker and
    direction, preferring the candidate whose entry price matches; None when
    the candidate or its time stop is not there."""
    try:
        data = json.loads(analyst_json) if isinstance(analyst_json, (str, bytes)) else analyst_json
    except (TypeError, ValueError):
        return None
    cands = [c for c in ((data or {}).get("candidates") or [])
             if isinstance(c, dict) and str(c.get("ticker", "")).upper() == str(ticker).upper()
             and (not direction or str(c.get("direction") or "").lower() == str(direction).lower())]
    if not cands:
        return None
    if entry_price is not None and len(cands) > 1:
        def _entry(c):
            ep = c.get("entry_plan") if isinstance(c.get("entry_plan"), dict) else {}
            return ep.get("price", c.get("entry_price"))
        exact = [c for c in cands if isinstance(_entry(c), (int, float))
                 and abs(float(_entry(c)) - float(entry_price)) < 1e-9]
        cands = exact or cands
    ts = cands[0].get("time_stop")
    return str(ts) if ts else None


def shadow_pick(rejection: dict, *, time_stop: str | None = None,
                pick: dict | None = None) -> dict:
    """The dict a rejection is priced with: the ONE definition of a shadow
    trade, handed to the same simulate_pick a taken pick goes through.

    A rejection that was also traded takes the TAKEN pick's own trading
    parameters (entry type, levels, entry window, time stop), so its shadow R
    is the pick's R by construction -- the validator can move a lane pick's
    levels, and only the pick knows where it actually went. A rejection that
    was not traded takes its own levels and the analyst's time stop."""
    src = pick if pick is not None else rejection
    return {
        "pick_id": rejection["rejection_id"] if "rejection_id" in rejection else src.get("pick_id"),
        "direction": src.get("direction"),
        "entry_type": src.get("entry_type"),
        "entry_price": src.get("entry_price"),
        "stop_price": src.get("stop_price"),
        "target_price": src.get("target_price"),
        # Same entry window a taken pick of this type carried (v27).
        "entry_not_before": src.get("entry_not_before"),
        "time_stop": (pick or {}).get("time_stop") if pick is not None else time_stop,
    }


def grade_day(md: MarketData, picks: list[dict], d: date,
              slippage_bps: float = 0.0, flatten_at: str | None = None,
              min_bar_volume: float = 0, adv20_map: dict[str, float] | None = None,
              atr_map: dict[str, float] | None = None,
              trend_map: dict[str, dict] | None = None,
              fundamentals_map: dict[str, dict] | None = None,
              prev_close_map: dict[str, float] | None = None,
              or_minutes: int = 15) -> list[dict]:
    """Grade a day's picks."""
    outcomes = []
    for p in picks:
        t = p["ticker"].upper()
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
                          adv20=(adv20_map or {}).get(t),
                          atr14=(atr_map or {}).get(t),
                          trend_snapshot=(trend_map or {}).get(t),
                          fundamentals=(fundamentals_map or {}).get(t),
                          prev_close=(prev_close_map or {}).get(t),
                          or_minutes=or_minutes, session_date=d,
                          session_bars=bars)
        o["eval_date"] = d
        outcomes.append(o)
        log("grade", f"{p['ticker']} {p['direction']}: triggered={o['triggered']} "
                     f"exit={o['exit_reason']} R={realized_r(o)}")
    return outcomes


def realized_r(o: dict) -> float | None:
    """The realized R every report shows: planned-risk R (r_planned), falling
    back to r_multiple only for a legacy row that has none. r_multiple divides
    by the fill-to-stop distance (floored at 25% of plan), so it inflates a
    fill near the stop and deflates an adverse fill -- a diagnostic, never the
    headline (see the module docstring)."""
    v = o.get("r_planned")
    if v is None:
        v = o.get("r_multiple")
    return v


# Why a minimum-size test pick is on the tape. The grader is told, per pick,
# that the SYSTEM placed it to sample a setup; on 2026-10-05 NVDA (coverage)
# got a C and "shouldn't have been taken" for being exactly what the lane is for.
_TEST_LANE_NOTES = {
    "coverage": "coverage: the risk manager rejected it and the desk traded it at minimum "
                "size by design, so every setup the analyst found gets tested",
    "daily_floor": "daily floor: the desk trades every session, and this minimum-size pick "
                   "was placed by design so the day is not empty",
    "hypothesis": "hypothesis test: a minimum-size trade placed by design to test an idea "
                  "the desk wrote down",
    "rr_below_control": "R:R control: a minimum-size trade placed by design to measure the "
                        "R:R threshold",
    "backlog_probe": "backlog probe: a minimum-size trade placed by design to collect "
                     "evidence the desk is short of",
}


def _volume_share(part: float | None, whole: float | None) -> str | None:
    """Share of the day's volume done by a checkpoint: RVOL@T / RVOL@close.
    Both are cumulative volume over the same ADV20, so the ratio is the
    stock's own intraday volume curve -- no outside volume profile needed."""
    if part is None or not whole or whole <= 0:
        return None
    return f"{min(part / whole, 1.0):.0%}"


# ---------------------------------------------------------------------------
# Simulator vs broker (docs/FIX_PHASE0.md, D3)
# ---------------------------------------------------------------------------
# A pick that went to the broker has two results: the simulator's (the research
# number, comparable across every pick) and the paper account's (what was
# actually earned). On 2026-10-15 the review showed only the first: -1.56R and
# one winner, against -$222 realized and no winner at the broker, because the
# simulated RKLB winner was an order Alpaca cancelled unfilled.

# Both filled, and the broker's R and the simulated R differ by at least this
# much: the fills or the exit differ. Ordinary friction and price improvement on
# the paper account move R by well under this (CRWV, INTC, CMC and AMAT on
# 2026-10-15/16 all sit within 0.07R).
DIVERGENCE_R = 0.10

_DIVERGENCE_TEXT = {
    "sim_only_fill": ("the simulator filled this entry and the broker did not. The simulated R "
                      "below is a research number, NOT a result the paper account earned; do not "
                      "grade the trade on it."),
    "broker_only_fill": ("the broker filled this entry and the simulator did not (it never "
                         "triggered, or called the fill invalid). The broker's R is the result; "
                         "the simulated outcome below did not happen."),
    "price_gap": (f"both filled, but the broker's R and the simulated R differ by "
                  f"{DIVERGENCE_R:.2f}R or more (a different fill or exit). The broker's R is "
                  f"the result."),
}


def classify_divergence(sim: dict, broker: dict | None, pick: dict | None = None) -> str | None:
    """How the simulator and the paper account disagree about one pick.

    `broker` is {entry_fill, exit_fill, exit_reason, live_r, status} from the
    pick's live order; None when the pick never went to the broker (nothing to
    compare, returns None).

      sim_only_fill    the simulator filled the entry; the broker never did
      broker_only_fill the broker filled it; the simulator did not (untriggered,
                       or an invalid_fill it refused to call a trade)
      price_gap        both filled but live_r and the simulated R differ by
                       DIVERGENCE_R or more (or, with no exit yet, the entries
                       differ by that many planned-risk units)
      none             they agree (including: neither filled)
    """
    if not broker:
        return None
    sim_filled = bool(sim.get("triggered")) and sim.get("exit_reason") != "invalid_fill"
    b_entry = broker.get("entry_fill")
    if sim_filled and b_entry is None:
        return "sim_only_fill"
    if b_entry is not None and not sim_filled:
        return "broker_only_fill"
    if not sim_filled:
        return "none"
    live_r, sim_r = broker.get("live_r"), realized_r(sim)
    if live_r is not None and sim_r is not None:
        return "price_gap" if abs(float(live_r) - float(sim_r)) >= DIVERGENCE_R else "none"
    # The broker has not exited yet: compare the entries in planned-risk units.
    try:
        risk = abs(float(pick["entry_price"]) - float(pick["stop_price"]))
        gap = abs(float(b_entry) - float(sim["entry_fill"])) / risk
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return "none"
    return "price_gap" if gap >= DIVERGENCE_R else "none"


def _broker_lines(o: dict) -> list[str]:
    """The paper account's result for a pick that went to the broker, as the
    HEADLINE of its review, plus one line when it disagrees with the
    simulator. Empty for a pick that never went to the broker."""
    if o.get("broker_status") is None:
        return []
    status, reason = o.get("broker_status"), o.get("broker_exit_reason")
    entry, exit_px, live_r = o.get("broker_entry_fill"), o.get("broker_exit_fill"), o.get("live_r")
    if entry is None:
        why = (reason or status or "no fill").replace("_", " ")
        lines = [f"- BROKER (the headline: what the paper account actually did): the entry was "
                 f"NEVER FILLED ({why}). No trade, no R."]
    elif exit_px is None:
        lines = [f"- BROKER (the headline: what the paper account actually did): filled @ {entry}; "
                 f"no exit recorded yet ({status})."]
    else:
        r_txt = f"{float(live_r):+.2f}" if live_r is not None else "n/a"
        lines = [f"- BROKER (the headline: what the paper account actually did): filled @ {entry}, "
                 f"exit @ {exit_px} ({reason or status}), R = {r_txt} (planned-risk R from the "
                 f"broker's own fills)"]
    div = o.get("sim_broker_divergence")
    if div and div != "none":
        lines.append(f"  ⚠ SIM/BROKER DIVERGENCE ({div}): {_DIVERGENCE_TEXT.get(div, div)}")
    return lines


def broker_summary_line(picks: list[dict], outcomes: list[dict]) -> str:
    """One line for the top of the evening review: what the paper account
    earned against what the simulator says, and which picks disagree. Empty
    when nothing went to the broker."""
    live = [o for o in outcomes if o.get("broker_status") is not None]
    if not live:
        return ""
    names = {p["pick_id"]: p["ticker"] for p in picks}
    filled = [o for o in live if o.get("broker_entry_fill") is not None]
    broker_rs = [float(o["live_r"]) for o in filled if o.get("live_r") is not None]
    sim_rs = [realized_r(o) for o in live
              if o.get("triggered") and o.get("exit_reason") != "invalid_fill"
              and realized_r(o) is not None]
    diverged = [f"{names.get(o['pick_id'], o['pick_id'])} {o['sim_broker_divergence']}"
                for o in live if o.get("sim_broker_divergence") not in (None, "none")]
    line = (f"LIVE vs SIMULATED: {len(live)} pick(s) went to the broker, {len(filled)} filled. "
            f"Broker result {sum(broker_rs):+.2f}R over {len(broker_rs)} settled trade(s); "
            f"simulator {sum(sim_rs):+.2f}R over {len(sim_rs)}.")
    if diverged:
        line += " DIVERGENT: " + ", ".join(diverged) + "."
    return line


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
        lane_note = _TEST_LANE_NOTES.get(str(p.get("exploration_lane") or ""))
        if lane_note:
            lines.append(f"- Lane: {lane_note}. Being taken is the system's decision, not the "
                         f"desk's: grade its construction and what the tape did, never that it "
                         f"was traded.")
        lines.append(f"- Thesis: {p['thesis']}")
        lines.append(f"- Invalidation: {p['invalidation']}")
        # A pick that went to the broker has two results. The paper account's is
        # the headline; the simulator's is the research number, and says so.
        live = o.get("broker_status") is not None
        lines.extend(_broker_lines(o))
        tag = "SIMULATED (research number, not the headline)" if live else "OUTCOME"
        if o.get("triggered"):
            times = f" [{o['fill_time']} -> {o['exit_time']}]" if o.get("fill_time") and o.get("exit_time") else ""
            scratch = (f" — a SCRATCH (inside +/-{SCRATCH_R:.2f}R: neither a win nor a loss, "
                       f"its sign is noise)" if outcome_class(realized_r(o)) == "scratch" else "")
            lines.append(f"- {tag}: filled @ {o['entry_fill']}, exit @ {o['exit_price']} "
                         f"({o['exit_reason']}), R = {realized_r(o):+.2f} (planned-risk R){scratch}, "
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
                # RVOL@T is CUMULATIVE session volume to T over the FULL-day
                # ADV20, so 0.16x at 10:00 can be an ordinary pace. The share
                # of the day's volume done by each checkpoint is what reads
                # pace (NVDA 10/05: 0.16x at 10:00, 0.90x at close = 18%).
                for key, label in (("rvol_at_1000", "10:00"), ("rvol_at_1200", "12:00")):
                    if o.get(key) is not None:
                        share = _volume_share(o[key], o.get("rvol_close"))
                        bits.append(f"RVOL@{label} {o[key]:.2f}x ADV20"
                                    + (f" ({share} of the day's volume by {label})" if share else ""))
                if o.get("rvol_close") is not None:
                    bits.append(f"RVOL@close {o['rvol_close']:.2f}x ADV20")
                if o.get("ema9_5m_loss_et"):
                    bits.append(f"lost 9EMA(5m) at {o['ema9_5m_loss_et']}")
                lines.append(f"  Indicators (mechanically computed — cite these numbers, "
                            f"never a guess): {', '.join(bits)}")
        elif o.get("exit_reason") == "invalid_fill":
            lines.append(
                f"- {tag}: INVALID FILL — {o.get('invalid_fill_detail', 'fill landed on the wrong side of the stop; not a real trade.')} "
                f"This is an execution-simulation artifact, not a trade outcome. Do not grade it, "
                f"do not construct a narrative to explain it, and do not extract a lesson from it.")
        else:
            mm = o.get("missed_move_r")
            extra = f" — untriggered; the move from entry level would have been {mm:+.2f}R" if mm is not None else ""
            touched = o.get("touch_only_bars")
            if touched:
                extra += (f"; price touched the limit on {touched} bar(s) without trading through it "
                          f"enough to fill (a resting limit needs a trade-through)")
            lines.append(f"- {tag}: NOT TRIGGERED ({o.get('exit_reason','no_trigger')}){extra}")
        if o.get("day_open") is not None:
            lines.append(f"- Day OHLC: O {o['day_open']} H {o['day_high']} L {o['day_low']} "
                         f"C {o['day_close']} Vol {o.get('day_volume','?')}")
        ctx_bits = []
        if o.get("stop_dist_atr") is not None:
            ctx_bits.append(f"stop {o['stop_dist_atr']:.2f} ATR from entry (ATR14 {o.get('atr14')})")
        if o.get("target_dist_atr") is not None:
            ctx_bits.append(f"target {o['target_dist_atr']:.2f} ATR from entry")
        if o.get("gap_atr") is not None:
            ctx_bits.append(f"gap {o['gap_atr']:.2f} ATR")
        if o.get("ext_atr_at_entry") is not None:
            ctx_bits.append(f"extended {o['ext_atr_at_entry']:+.2f} ATR from 20d MA at entry")
        if o.get("trend_side_200d"):
            ctx_bits.append(f"{o['trend_side_200d']} 200d MA")
        if o.get("or15_high") is not None:
            or_bit = f"OR15 {o['or15_low']:.2f}-{o['or15_high']:.2f}"
            if o.get("or_break_dir"):
                or_bit += f", broke {o['or_break_dir']}"
            ctx_bits.append(or_bit)
        if o.get("fill_vs_or"):
            ctx_bits.append(f"filled {o['fill_vs_or']} the opening range")
        if o.get("mae_before_mfe") is not None:
            ctx_bits.append("MAE printed before MFE" if o["mae_before_mfe"] else "MFE printed before MAE")
        if o.get("short_pct_float") is not None:
            ctx_bits.append(f"short {o['short_pct_float']:.0f}% of float")
        if o.get("mkt_cap"):
            mc = o["mkt_cap"]
            ctx_bits.append(f"mcap ${mc / 1e12:.1f}T" if mc >= 1e12 else f"mcap ${mc / 1e9:.1f}B")
        if ctx_bits:
            lines.append(f"  Context (mechanically computed — cite these numbers, "
                        f"never chart memory): {', '.join(ctx_bits)}")
        lines.append("")
    return "\n".join(lines)
