"""
Deterministic technical-indicator primitives — VWAP, EMA, RVOL.

Pure functions over the bar dict shape used throughout the codebase:
{"t": datetime, "o": float, "h": float, "l": float, "c": float, "v": int}.
No I/O, no config, no clock reads — every value is a function of the bars
handed in, so the same inputs produce the same outputs whether the bars came
from Alpaca, yfinance, or agent.providers.synthetic_minute_bars (offline mode
parity, per specs/INDICATOR_LAYER_SPEC_v1.md §0.3).

Design rules (spec §0, non-negotiable):
  - Deterministic code computes every indicator number. The LLM never derives
    one; prompts only interpret values computed here.
  - No look-ahead: this module never reads a clock. Whether a value is
    "premarket VWAP" or "session VWAP" is entirely a function of which bars
    the caller passes in — the caller owns the cutoff.
  - Missing/insufficient data returns None, never a fabricated number.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal


def _typical_price(bar: dict) -> float:
    return (bar["h"] + bar["l"] + bar["c"]) / 3.0


def vwap_series(bars: list[dict]) -> list[float]:
    """Session-anchored cumulative VWAP per bar.

    Typical price (h+l+c)/3 weighted by volume, anchored at bars[0] — pass
    premarket-inclusive bars for a premarket VWAP, or 9:30-anchored bars for
    the regular-session VWAP; this function has no opinion on which.
    A bar with zero/missing volume can't move the cumulative average, so its
    VWAP value is just carried forward from the prior bar's (or that bar's
    own close, if it's the very first bar).
    """
    out: list[float] = []
    cum_pv = 0.0
    cum_v = 0.0
    for b in bars:
        v = b.get("v") or 0
        cum_pv += _typical_price(b) * v
        cum_v += v
        out.append(cum_pv / cum_v if cum_v > 0 else float(b["c"]))
    return out


def ema_series(closes: list[float], period: int) -> list[float | None]:
    """Standard EMA, seeded with the SMA of the first `period` closes.

    Values before the seed index are None — "not enough data yet" is not a
    number and must never be reported as one.
    """
    n = len(closes)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    seed = sum(closes[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1)
    prev = seed
    for i in range(period, n):
        prev = closes[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rvol_simple(cum_volume: float | int, adv20: float | None) -> float | None:
    """v1 RVOL: cumulative session (or premarket) volume / 20-day average
    DAILY volume.

    Honest label: this is "pct of ADV20", not a true time-of-day RVOL curve
    (which needs a per-symbol minute-volume profile — v2, see spec §1). None
    when adv20 is unavailable/non-positive — never fabricate a ratio against
    a missing denominator.
    """
    if not adv20 or adv20 <= 0:
        return None
    return cum_volume / adv20


def vwap_distance(price: float, vwap_value: float) -> float:
    """Signed % distance of price from VWAP; positive = above."""
    if not vwap_value:
        return 0.0
    return (price - vwap_value) / vwap_value * 100.0


def vwap_events(bars: list[dict], vwap: list[float]) -> dict:
    """First reclaim/reject times, % of session spent above VWAP, cross count.

    A cross counts ONLY on bar CLOSE through VWAP — a wick touch is noise,
    not a reclaim or a reject. "Reclaim" = the first close-through from below
    to at/above VWAP; "reject" = the first close-through from at/above down
    to below. Both are None if the tape never crosses.
    """
    out: dict = {"first_reclaim_et": None, "first_reject_et": None,
                 "pct_time_above": None, "crosses": 0}
    if not bars or len(bars) != len(vwap):
        return out
    above_count = 0
    prev_above: bool | None = None
    for b, vw in zip(bars, vwap):
        above = b["c"] >= vw
        if above:
            above_count += 1
        if prev_above is not None and above != prev_above:
            out["crosses"] += 1
            if above and out["first_reclaim_et"] is None:
                out["first_reclaim_et"] = b.get("t")
            if not above and out["first_reject_et"] is None:
                out["first_reject_et"] = b.get("t")
        prev_above = above
    out["pct_time_above"] = above_count / len(bars)
    return out


def ema_loss_time(bars: list[dict], ema: list[float | None],
                  direction: Literal["long", "short"]) -> datetime | None:
    """First bar-close through `ema` against the position's direction.

    A long loses the EMA on a close BELOW it; a short loses it on a close
    ABOVE it. Callers wanting "first loss after fill" should slice bars/ema
    to start at the fill index — this function has no notion of a fill, it
    just walks whatever bars it's given. Bars with no EMA value yet (None)
    are skipped, not treated as a loss.
    """
    if not bars or len(bars) != len(ema):
        return None
    long = direction == "long"
    for b, e in zip(bars, ema):
        if e is None:
            continue
        if long and b["c"] < e:
            return b.get("t")
        if not long and b["c"] > e:
            return b.get("t")
    return None
