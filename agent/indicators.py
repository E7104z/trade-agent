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


def atr(daily_bars: list[dict], period: int = 14) -> float | None:
    """Wilder-smoothed Average True Range from DAILY bars (h/l/c only needed).

    Needs period+1 bars (true range requires the prior close); with fewer,
    returns None — not enough data is not a number. Seeded with the SMA of
    the first `period` true ranges, then Wilder's recursive smoothing for
    whatever bars remain (standard ATR construction, matches every public
    charting package)."""
    if len(daily_bars) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(daily_bars)):
        h, l, prev_c = daily_bars[i]["h"], daily_bars[i]["l"], daily_bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    val = sum(trs[:period]) / period
    for tr in trs[period:]:
        val = (val * (period - 1) + tr) / period
    return val


def daily_levels(daily_bars: list[dict]) -> dict:
    """{"pdh","pdl","pdc","hi_5d","lo_5d","hi_20d","lo_20d","hi_52w","lo_52w"}
    from COMPLETED sessions only — the caller must exclude any partial
    current-day bar before calling; this function asserts nothing about
    "today" (no clock reads, per module design rules). pdh/pdl/pdc come from
    the LAST bar in the list. Each windowed high/low (5d/20d/52w) is None
    when fewer than that many bars are available — never computed from a
    short window silently passed off as the real thing."""
    out: dict = {"pdh": None, "pdl": None, "pdc": None, "hi_5d": None, "lo_5d": None,
                 "hi_20d": None, "lo_20d": None, "hi_52w": None, "lo_52w": None}
    if not daily_bars:
        return out
    last = daily_bars[-1]
    out["pdh"], out["pdl"], out["pdc"] = last["h"], last["l"], last["c"]
    for n, hi_key, lo_key in ((5, "hi_5d", "lo_5d"), (20, "hi_20d", "lo_20d"),
                              (252, "hi_52w", "lo_52w")):
        if len(daily_bars) >= n:
            chunk = daily_bars[-n:]
            out[hi_key] = max(b["h"] for b in chunk)
            out[lo_key] = min(b["l"] for b in chunk)
    return out


def premarket_levels(pm_bars: list[dict]) -> dict:
    """{"pm_high","pm_low"} from premarket 1Min bars — the same bars v1
    already fetches for pm_vwap, zero additional network for the caller."""
    if not pm_bars:
        return {"pm_high": None, "pm_low": None}
    return {"pm_high": max(b["h"] for b in pm_bars), "pm_low": min(b["l"] for b in pm_bars)}


def trend_snapshot(closes: list[float], atr_val: float | None) -> dict:
    """{"above_ma20","above_ma50","above_ma200" (bool|None), "ma20_slope_pct",
    "ext_atr"} from DAILY closes (oldest -> newest). SMAs throughout —
    consistent with agent/regime.py; at a 200-bar lookback the SMA/EMA
    divergence is noise, so only one is computed. ma20_slope_pct compares
    today's 20d SMA to the 20d SMA five sessions ago (same construction as
    regime.py's trend slope). ext_atr = (last - ma20) / ATR — extension from
    the mean in units of how the name actually moves; None without both a
    20d SMA and a usable ATR."""
    out: dict = {"above_ma20": None, "above_ma50": None, "above_ma200": None,
                 "ma20_slope_pct": None, "ext_atr": None}
    if not closes:
        return out
    last = closes[-1]

    def _sma(n: int) -> float | None:
        return sum(closes[-n:]) / n if len(closes) >= n else None

    ma20, ma50, ma200 = _sma(20), _sma(50), _sma(200)
    out["above_ma20"] = (last > ma20) if ma20 is not None else None
    out["above_ma50"] = (last > ma50) if ma50 is not None else None
    out["above_ma200"] = (last > ma200) if ma200 is not None else None
    if ma20 is not None and len(closes) >= 25:
        ma20_prev = sum(closes[-25:-5]) / 20
        out["ma20_slope_pct"] = ((ma20 - ma20_prev) / ma20_prev * 100) if ma20_prev else None
    if ma20 is not None and atr_val:
        out["ext_atr"] = (last - ma20) / atr_val
    return out


def opening_range(bars: list[dict], minutes: int = 15) -> dict:
    """{"or_high","or_low","break_dir" ("up"/"down"/None),"break_et"} from
    intraday bars (assumed 1-minute, session-anchored — same assumption
    grading.py's clock-mapping helpers make elsewhere in this codebase).
    Break = the first bar CLOSE beyond the completed opening range —
    close-through, a wick touch is noise, same doctrine as vwap_events."""
    out: dict = {"or_high": None, "or_low": None, "break_dir": None, "break_et": None}
    if not bars or minutes <= 0:
        return out
    or_bars = bars[:minutes]
    if not or_bars:
        return out
    or_high = max(b["h"] for b in or_bars)
    or_low = min(b["l"] for b in or_bars)
    out["or_high"], out["or_low"] = or_high, or_low
    for b in bars[minutes:]:
        if b["c"] > or_high:
            out["break_dir"], out["break_et"] = "up", b.get("t")
            break
        if b["c"] < or_low:
            out["break_dir"], out["break_et"] = "down", b.get("t")
            break
    return out


def beta_corr(closes_a: list[float], closes_b: list[float],
              n: int = 20) -> tuple[float, float] | None:
    """(beta, correlation) of b relative to a over the last n daily returns.

    `a` is the benchmark (e.g. SPY), `b` the candidate: beta = cov(a,b) /
    var(a), the standard "stock's beta to the market" regression coefficient
    — so a 2x-levered copy of `a` has beta 2.0 to it. None if either series
    has fewer than n+1 closes (can't form n returns) or `a`'s variance is
    zero (a flat benchmark makes beta undefined, not infinite)."""
    if len(closes_a) < n + 1 or len(closes_b) < n + 1:
        return None
    ra = [(closes_a[i] / closes_a[i - 1] - 1) for i in range(len(closes_a) - n, len(closes_a))]
    rb = [(closes_b[i] / closes_b[i - 1] - 1) for i in range(len(closes_b) - n, len(closes_b))]
    mean_a, mean_b = sum(ra) / n, sum(rb) / n
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(ra, rb)) / n
    var_a = sum((x - mean_a) ** 2 for x in ra) / n
    var_b = sum((y - mean_b) ** 2 for y in rb) / n
    if var_a <= 0 or var_b <= 0:
        return None
    return cov / var_a, cov / (var_a ** 0.5 * var_b ** 0.5)


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
