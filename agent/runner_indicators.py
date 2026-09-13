"""
DTO Runner indicator primitives - a deliberate, isolated port.

Source of truth: the DTO Trading App repo (`E7104z/trading`), files
`js/indicators.js` and `js/runner-scanner.js`. This module exists so the
Runner port's numeric conventions can never be confused with the ones in
`agent/indicators.py` (specs/RUNNER_PORT_SPEC_v1.md 0.1, 0.2).

Do not import `agent.indicators` here, and do not route Runner detection
through it. The two modules agree today on EMA seeding (see below) but they
are answerable to different sources and must be free to diverge.


===========================================================================
EMA SEEDING - the finding spec 0.1 flagged as the highest-risk unknown
===========================================================================

DTO does not have one `calcEMA`. It has six, and they do not agree:

  js/runner-scanner.js:96           SMA of the first `period` closes  <- AUTHORITATIVE
  js/runner-backtest.js:115         bars[0].c
  js/runner-clean30-backtest.js:46  bars[0].c
  js/runner-live-grade.js:32        bars[0].c
  js/runner-results.js:37           bars[0].c
  js/daryl-engine.js:30             bars[0].c

`checkRunnerConditions` - the function this port reproduces - lives in
runner-scanner.js and calls the runner-scanner.js `calcEMA`, so SMA seeding
is the correct convention for detection, and it is what `ema()` implements
here. The runner-scanner.js source comments the choice explicitly
(js/runner-scanner.js:92-95):

    // Properly seeded: initial value = SMA of first `period` bars,
    // then recursive EMA for the rest. This converges ~3-4x faster than
    // seeding on bar[0].close alone and produces accurate values within
    // 12 bars instead of ~30.

Consequence worth recording - narrower than it first looks, but real.
js/runner-backtest.js:23 imports `checkRunnerConditions` from the scanner, so
DETECTION in the backtest does use the SMA-seeded EMA and does agree with the
live scanner. What does NOT agree is everything the backtest computes with its
own local `calcEMAAtIndex`:

  - the EMA exit walk          (runner-backtest.js:148, :520, :1580)
  - the rule-based hard stop   (exit-rules.js via :715, :1570)
  - the emaSlopeAcceleration feature (:780-781)

So a backtested Runner trade is entered on an SMA-seeded 9 EMA, has its stop
anchored to that same SMA-seeded value, and is then exited by a DIFFERENT,
first-close-seeded 9 EMA. The two converge within ~30 bars, so the damage
concentrates in early-session trades - which is most of them, since detection
starts at 09:45. Any DTO exit statistic inherits that mismatch.

This port does not reproduce the mismatch. Phase 1's simulator exits on
target/stop/EOD and never reads an EMA, so the question does not arise yet; if
an EMA-based exit is ever added, it must state which seeding it uses and why.
It is also an independent reason to re-derive conclusions rather than port them
(spec 0 R-2).

`seed="first_close"` is provided so the parity harness can reproduce the
backtest/grade family too, but detection must never use it.

Both variants return None when `len(bars) < period`, matching the JS
`if (!bars || bars.length < period) return null` guard. runner-backtest.js's
`calcEMAAtIndex` spells the same guard as `endIndex < period - 1`; with
`slice = bars[:endIndex+1]` the two are equivalent.

Note also: `agent/indicators.py::ema_series` already seeds with the SMA of the
first `period` closes, so it happens to agree with the scanner. The spec
warned it might not - the mismatch it anticipated is real, but it lives in
js/runner-stream.js, which seeds streaming EMAs on `bar.c` of the first bar
and is not part of this port.


Bar dict shape is the existing codebase shape, unchanged:
    {"t": datetime, "o": float, "h": float, "l": float, "c": float, "v": int}

No I/O, no config, no clock reads. `rvol()` derives minutes-into-session from
the LAST BAR's ET timestamp, never from the wall clock, exactly as
`calculateRVOL` does.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

__all__ = [
    "INTRADAY_VOL_CURVE",
    "SESSION_OPEN_ET_MINUTES",
    "js_to_fixed",
    "et_minutes",
    "vwap",
    "ema",
    "ema_at",
    "expected_volume_fraction",
    "rvol",
    "trend_consistency",
]

# 9:30 ET expressed as minutes past midnight. calculateRVOL spells this
# inline as `(9 * 60 + 30)`.
SESSION_OPEN_ET_MINUTES = 570

# Ported verbatim from js/indicators.js. Each entry is
# (minutesIntoSession, cumulativeFraction).
INTRADAY_VOL_CURVE: list[tuple[int, float]] = [
    (5,   0.08),
    (10,  0.13),
    (15,  0.17),
    (20,  0.20),
    (25,  0.23),
    (30,  0.26),
    (45,  0.31),
    (60,  0.35),
    (90,  0.42),
    (120, 0.48),
    (150, 0.53),
    (180, 0.57),
    (210, 0.61),
    (240, 0.65),
    (270, 0.70),
    (300, 0.75),
    (330, 0.82),
    (360, 0.90),
    (390, 1.00),
]


def js_to_fixed(x: float, digits: int) -> float:
    """`Number.prototype.toFixed` semantics, returned as a float.

    ECMA-262 picks the integer n minimising |n/10^f - x| and, on a tie, picks
    the LARGER n - round-half-away-from-zero on the double's exact binary
    value. Python's built-in round() rounds half-to-even instead, so the two
    disagree on exact ties.

    This is not hypothetical. The parity harness caught it on the first
    randomized run: a ticker with 13 green bars out of 16 gives exactly 81.25,
    which JS renders 81.3 and Python's round() renders 81.2. A tenth of a
    percentage point is meaningless on its own, but trend_consistency feeds a
    hard gate at 45.0 and a research bucket boundary, so a value that lands on
    a tie can flip a candidate in or out. Match the source.
    """
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(x).quantize(q, rounding=ROUND_HALF_UP))


def et_minutes(ts) -> int:
    """Minutes past ET midnight for a bar timestamp.

    The JS spells this as a `toLocaleString('en-US', {timeZone:
    'America/New_York'})` round-trip; here the bar timestamps are already
    tz-aware ET datetimes produced by agent.providers, so this is a direct
    read. A naive datetime is taken to be ET already.
    """
    from .util import ET

    if ts.tzinfo is not None:
        ts = ts.astimezone(ET)
    return ts.hour * 60 + ts.minute


def _typical_price(bar: dict) -> float:
    return (bar["h"] + bar["l"] + bar["c"]) / 3.0


def vwap(bars: list[dict]) -> float | None:
    """Cumulative sum(typicalPrice * volume) / cumulative volume.

    Port of `calculateVWAP`. typicalPrice = (h + l + c) / 3 - this is the
    typical-price basis DTO standardized on 2026-07-15, and every DTO corpus
    rule was validated against it. Returns None on empty bars or zero total
    volume; never substitutes a close-based average.
    """
    if not bars:
        return None
    cum_pv = 0.0
    cum_v = 0.0
    for b in bars:
        v = b["v"]
        cum_pv += _typical_price(b) * v
        cum_v += v
    if cum_v == 0:
        return None
    return cum_pv / cum_v


def ema(bars: list[dict], period: int, seed: str = "sma") -> float | None:
    """Final EMA value over `bars`. Port of runner-scanner.js `calcEMA`.

    seed="sma"          seed = SMA of the first `period` closes  (detection)
    seed="first_close"  seed = bars[0].c                         (parity only)

    See the module docstring for why detection must use "sma".
    """
    if not bars or len(bars) < period:
        return None
    k = 2.0 / (period + 1)
    if seed == "first_close":
        e = bars[0]["c"]
        for i in range(1, len(bars)):
            e = bars[i]["c"] * k + e * (1 - k)
        return e
    # Seed with the SMA of the first `period` bars, then recurse.
    e = sum(bars[i]["c"] for i in range(period)) / period
    for i in range(period, len(bars)):
        e = bars[i]["c"] * k + e * (1 - k)
    return e


def ema_at(bars: list[dict], period: int, idx: int, seed: str = "sma") -> float | None:
    """EMA evaluated as of bar `idx` (inclusive). Port of `calcEMAAtIndex`.

    A negative `idx` returns None explicitly rather than relying on Python's
    negative slicing to produce an empty list and fall through ema()'s length
    guard - same answer, but the intent stays legible.
    """
    if idx < 0:
        return None
    return ema(bars[: idx + 1], period, seed=seed)


def expected_volume_fraction(minutes_in: float | None) -> float | None:
    """Expected cumulative volume fraction N minutes into the session.

    Port of `expectedVolumeFraction`, including the first-segment special case
    and the final clamp. A `minutes_in` past the last curve point (>= 390)
    leaves the fraction at its 1.0 initialization because the JS loop never
    breaks - reproduced here by keeping the same loop shape.
    """
    if minutes_in is None or minutes_in <= 0:
        return None
    expected_fraction = 1.0
    for i, (mn, frac) in enumerate(INTRADAY_VOL_CURVE):
        if minutes_in <= mn:
            if i == 0:
                expected_fraction = frac * (minutes_in / mn)
            else:
                prev_min, prev_frac = INTRADAY_VOL_CURVE[i - 1]
                t = (minutes_in - prev_min) / (mn - prev_min)
                expected_fraction = prev_frac + t * (frac - prev_frac)
            break
    return min(1.0, max(0.01, expected_fraction))


def rvol(bars: list[dict], prev_day_volume: float | None) -> float | None:
    """today_vol / (prev_day_volume * expected_volume_fraction(minutes_in)).

    Port of `calculateRVOL`. `minutes_in` comes from the LAST bar's ET
    timestamp minus 570 - not from a clock - so this stays replayable.

    Returns None when bars are empty, when `prev_day_volume` is falsy, when
    `minutes_in <= 0`, or when expected volume is 0. It never estimates a
    substitute denominator: the `mega-backtest.js` estimated-RVOL fallback is
    deliberately not ported (spec 0 R-4, spec 3). An RVOL that cannot be
    computed has not passed.
    """
    if not bars or not prev_day_volume:
        return None
    today_vol = sum(b["v"] for b in bars)
    minutes_in = et_minutes(bars[-1]["t"]) - SESSION_OPEN_ET_MINUTES
    if minutes_in <= 0:
        return None
    expected_fraction = expected_volume_fraction(minutes_in)
    if expected_fraction is None:
        return None
    expected_vol = prev_day_volume * expected_fraction
    if expected_vol == 0:
        return None
    return today_vol / expected_vol


def trend_consistency(bars: list[dict]) -> float | None:
    """Percent of `bars` that closed green (c >= o), rounded to 1dp.

    The CALLER owns the window. Per js/runner-backtest.js:534-537 the window
    must be pre-entry bars only - up to and including the detection bar - so
    no future data leaks into a detection-time feature:

        // TC must reflect what the live scanner sees at detection time: bars up
        // to AND INCLUDING the entry bar. [...] using post-entry bars here would
        // leak future data the scanner never had and skew the corpus gate.

    The live scanner (js/runner-scanner.js:1140-1144) computes it over its
    market-hours bars at scan time, which is the same window by construction.
    """
    if not bars:
        return None
    green = sum(1 for b in bars if b["c"] >= b["o"])
    # js_to_fixed, not round() - the JS spells this
    # `parseFloat((greenBars / marketBars.length * 100).toFixed(1))` and the
    # two disagree on exact ties. See js_to_fixed's docstring.
    return js_to_fixed(green / len(bars) * 100, 1)
