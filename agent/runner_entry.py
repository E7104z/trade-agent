"""
What was knowable when a runner candidate could first be bought, and what price
did after that (specs/RUNNER_TRADE_CONSTRUCTION_v1.md).

WHY THIS EXISTS
---------------
The runner lane's realized R mixes two different questions:

  1. Does price keep going up after the system could buy?  (the detection)
  2. Does the stop/target/management capture it?          (the construction)

A 9-EMA stop that sits inside normal noise loses on (2) even where (1) is
true, so a bucket can look dead in R while price in it kept rising. The
continuation fields here answer (1) with no stop and no target: drift from the
first enterable price to the close, and the run-up and draw-down on the way, in
ATR. Where drift is not positive for a whole bucket, no exit rule can win
there; where it is, the exit arms (agent/runner_exits.py) say whether any
construction captures it.

The three at-entry dimensions are the suspects the 2026-10-02 review named:
the index tape the long was bought into, how extended above VWAP it was, and
how long the plan had been sitting since the conditions first held.

HONESTY RULES
-------------
- "Entry" is the open of the first minute bar at or after the honest entry
  time (runner_scan.honest_entry_time) -- the price simulate_pick fills an
  `open` entry at, before friction.
- Every AT-ENTRY field (VWAP, extension, index tape) reads only bars that had
  closed before the entry bar opened. The entry bar itself is never read.
- Continuation fields are outcomes: they read the bars after entry and are
  never offered to the selector.
- ATR14 is the daily ATR from completed sessions (providers.daily_context), so
  it too was known at entry.
"""
from __future__ import annotations

from bisect import bisect_left
from datetime import datetime
from typing import Any

from . import runner_indicators as ri

__all__ = ["INDEX_SYMBOLS", "IndexTape", "entry_context", "fill_index", "session_vwap"]

INDEX_SYMBOLS = ("SPY", "QQQ")


def _naive(ts):
    """Naive ET wall-clock: how first_pass_et and the cutoff are stored. Bars
    carry ET tzinfo; strip it without shifting."""
    if ts is None:
        return None
    if getattr(ts, "tzinfo", None) is None:
        return ts
    from .util import ET
    return ts.astimezone(ET).replace(tzinfo=None)


def fill_index(bars: list[dict], cutoff) -> int | None:
    """Index of the first bar at/after `cutoff` (naive ET) -- the slice the
    evening hands simulate_pick. None when no bar qualifies."""
    if not bars:
        return None
    if cutoff is None:
        return 0
    for i, b in enumerate(bars):
        if _naive(b["t"]) >= cutoff:
            return i
    return None


def session_vwap(bars: list[dict]) -> float | None:
    """Session VWAP over the regular-session bars given (typical price x
    volume, from 09:30), as runner_exits' 5-minute marks compute it."""
    pv = v = 0.0
    for b in bars:
        if ri.et_minutes(b["t"]) < ri.SESSION_OPEN_ET_MINUTES:
            continue
        vol = b.get("v") or 0
        pv += ri._typical_price(b) * vol
        v += vol
    return (pv / v) if v else None


class IndexTape:
    """An index's session, prepared once, answering "where was it against its
    session VWAP just before time t?" for every candidate of the session."""

    def __init__(self, bars: list[dict] | None):
        self.times: list[datetime] = []
        self.pct: list[float | None] = []
        pv = v = 0.0
        for b in bars or []:
            if ri.et_minutes(b["t"]) < ri.SESSION_OPEN_ET_MINUTES:
                continue
            vol = b.get("v") or 0
            pv += ri._typical_price(b) * vol
            v += vol
            vwap = (pv / v) if v else None
            self.times.append(_naive(b["t"]))
            self.pct.append(round((b["c"] - vwap) / vwap * 100, 4) if vwap else None)

    def vwap_pct_before(self, at) -> float | None:
        """(close - VWAP) / VWAP * 100 through the last bar that STARTED
        before `at` -- the last bar complete when the entry bar opened."""
        if at is None or not self.times:
            return None
        i = bisect_left(self.times, _naive(at)) - 1
        return self.pct[i] if i >= 0 else None


def _r(x: float | None, digits: int = 4) -> float | None:
    return None if x is None else round(float(x), digits)


def entry_context(bars: list[dict], cutoff, first_pass_et, atr14: float | None,
                  tapes: dict[str, IndexTape] | None = None) -> dict[str, Any]:
    """The v28 outcome columns for one candidate, or {} with no enterable bar.

    `bars` is the candidate's full regular session; `cutoff` the honest entry
    time (naive ET); `tapes` the session's IndexTape per INDEX_SYMBOLS.
    """
    idx = fill_index(bars, cutoff)
    if idx is None:
        return {}
    entry_bar = bars[idx]
    entry_px = float(entry_bar["o"])
    entry_et = _naive(entry_bar["t"])
    out: dict[str, Any] = {"entry_px": _r(entry_px), "entry_et": entry_et}

    fp = _naive(first_pass_et) if isinstance(first_pass_et, datetime) else None
    if fp is not None and entry_et is not None:
        out["staleness_min"] = round((entry_et - fp).total_seconds() / 60.0, 1)

    vwap = session_vwap(bars[:idx]) if idx > 0 else None
    out["vwap_at_entry"] = _r(vwap)
    atr = float(atr14) if atr14 else None
    if atr and vwap is not None:
        out["ext_vwap_atr"] = _r((entry_px - vwap) / atr, 3)

    for sym, col in (("SPY", "spy_vwap_pct"), ("QQQ", "qqq_vwap_pct")):
        tape = (tapes or {}).get(sym)
        out[col] = tape.vwap_pct_before(entry_et) if tape is not None else None

    after = bars[idx:]
    if atr and after:
        out["drift_atr"] = _r((float(bars[-1]["c"]) - entry_px) / atr, 3)
        out["run_up_atr"] = _r((max(float(b["h"]) for b in after) - entry_px) / atr, 3)
        out["draw_down_atr"] = _r((entry_px - min(float(b["l"]) for b in after)) / atr, 3)
    return out
