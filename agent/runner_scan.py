"""
DTO Runner detection - condition check and gate chain.

PHASE 0 SCOPE ONLY. This module currently contains the pure, deterministic
parts of the port: `check_runner_conditions` (spec 0.3), the ported
`EXCLUDED_TICKERS` set, and `evaluate_ticker` - the gate chain the parity
harness needs in order to diff DTO's rejection counters (spec 0.4).

Deliberately NOT here yet, per spec 0.6 ("No code written for Phase 1 or
Phase 2"): `scan()`, universe construction, any database write, any cron
wiring, any provider call. Nothing in this module performs I/O or reads a
clock.

Source: js/runner-scanner.js from the DTO Trading App repo (`E7104z/trading`).
"""
from __future__ import annotations

from . import runner_indicators as ri

__all__ = [
    "EXCLUDED_TICKERS",
    "SCAN_VERSION",
    "check_runner_conditions",
    "evaluate_ticker",
    "market_hours_bars",
    "first_pass_index",
]

# Bump on ANY change to detection semantics, exactly as `sim_version` works
# for grading. Never compare rows across scan_version values without
# filtering on it.
SCAN_VERSION = 1

# ---------------------------------------------------------------------------
# EXCLUDED_TICKERS - ported verbatim from js/constants.js:539.
#
# This is the 117-symbol superset shared by every DTO scanner, NOT the deleted
# 11-ticker regex that used to live in js/scanner.js. Leveraged, inverse, and
# volatility products: they trade like their underlying's derivative, not like
# an accumulation candidate, so a "runner" reading on one is an artifact.
#
# The count is asserted in tests/test_runner_parity.py against the live JS Set
# size, so drift in either direction fails the suite.
# ---------------------------------------------------------------------------
EXCLUDED_TICKERS: frozenset[str] = frozenset([
    # Index leveraged / inverse
    "UPRO", "SPXL", "SPXS", "SPXU", "SSO", "SDS", "TNA", "TZA", "SQQQ", "TQQQ",
    "QLD", "QID", "UDOW", "SDOW", "DDM", "DXD", "YINN", "YANG", "EDC", "EDZ",
    "EFO", "EFU", "CHAU", "CWEB", "SRTY", "URTY", "INDL",
    # Sector / commodity leveraged
    "LABU", "LABD", "KOLD", "BOIL", "ERX", "ERY", "NUGT", "DUST", "JNUG", "JDST",
    "AGQ", "ZSL", "UCO", "SCO", "GUSH", "DRIP", "FAS", "FAZ", "TECL", "TECS",
    "FNGU", "FNGD", "BULZ", "BERZ", "SOXL", "SOXS", "ROM", "REW", "DIG", "DUG",
    "DPST", "DRN", "DRV", "NAIL", "CLAW", "DFEN", "CURE", "PILL", "WANT", "WEBS",
    "WEBL", "HIBS", "HIBL", "TYD", "TMF", "TMV", "UBT", "TBT", "TBF", "TTT",
    "RXL", "RXD", "DGAZ", "UGAZ",
    # Single-stock leveraged
    "TSLL", "TSLS", "TSLZ", "TSLR", "NVDU", "NVDX", "NVDD", "NVDS", "NVDL",
    "CONL", "CONI", "AAPB", "AAPD", "AMZU", "AMZD", "MSFL", "MSFD", "GOOX",
    "TARK", "BITX", "BITI", "ETHU", "ETHD", "XBTU", "MSTU", "MSTX", "MSTZ",
    # Volatility products
    "UVIX", "UVXY", "VXX", "SVIX", "SVXY", "VIXY", "VIXM", "SVOL", "UVOL",
])


def _fixed(x: float, digits: int) -> str:
    """JS `toFixed` as a string, for reason-string parity. See
    runner_indicators.js_to_fixed for why round()/format() will not do."""
    return f"{ri.js_to_fixed(x, digits):.{digits}f}"


def market_hours_bars(bars: list[dict], end_et_minutes: int | None = None) -> list[dict]:
    """Regular-session bars only.

    js/runner-scanner.js:1104-1108 filters to `et >= 570` with NO upper bound,
    so an afternoon scan legitimately includes bars past 15:00 ET. Pass
    `end_et_minutes` only to reproduce a bounded window; leave it None for DTO
    parity. (RUNNER_PORT_SPEC 1.3 step 5 specifies `570 <= m <= 900`, which is
    a Phase-1 tightening, not the ported behaviour - see the Phase 0 report.)
    """
    out = []
    for b in bars:
        m = ri.et_minutes(b["t"])
        if m < ri.SESSION_OPEN_ET_MINUTES:
            continue
        if end_et_minutes is not None and m > end_et_minutes:
            continue
        out.append(b)
    return out


def check_runner_conditions(bars: list[dict], min_vwap_pct: float = 0.5) -> dict:
    """Port of `checkRunnerConditions` (js/runner-scanner.js:121-183).

    A PURE condition check, statement for statement. It applies no price,
    float, earnings, RVOL, or exclusion gate - those live in the scan loop so
    each can report its own rejection reason (spec 0.3).

    Returns keys: passes, reason, price, vwap, vwap_pct_above, ema9,
    ema9_slope. The JS only attaches `vwapPctAbove` on the pass branch and on
    the "below minimum" fail branch; here the key is always present and is
    None wherever the JS leaves it undefined, which is the same information in
    a shape Python callers can rely on.

    Falsiness is deliberate throughout. The JS guards read `!vwap` and
    `!ema9Now`, so a computed 0.0 is treated as "unavailable", not as a real
    value. `not x` reproduces that for both None and 0.0.
    """
    n = len(bars) if bars else 0
    if not bars or n < 10:
        return {
            "passes": False,
            "reason": f"insufficient bars ({n}/10)",
            "price": None, "vwap": None, "vwap_pct_above": None,
            "ema9": None, "ema9_slope": None,
        }

    price = bars[-1]["c"]
    vw = ri.vwap(bars)

    if not vw or vw <= 0:
        return {
            "passes": False, "reason": "VWAP unavailable",
            "price": price, "vwap": None, "vwap_pct_above": None,
            "ema9": None, "ema9_slope": None,
        }

    # Condition 1: price clearly above VWAP.
    vwap_pct_above = ((price - vw) / vw) * 100
    if vwap_pct_above < min_vwap_pct:
        return {
            "passes": False,
            "reason": (f"Price only {_fixed(vwap_pct_above, 2)}% above VWAP "
                       f"(need >={min_vwap_pct}%)"),
            "price": price, "vwap": vw, "vwap_pct_above": vwap_pct_above,
            "ema9": None, "ema9_slope": None,
        }

    # Condition 2: 9 EMA ascending - current vs 3 bars ago.
    #
    # prev_idx >= 8 gives a properly seeded 9-bar EMA. Below that the JS falls
    # back to an EMA3 read at the same index as a direction proxy. That
    # fallback is load-bearing before ~10:15 ET, when a 12-bar history does
    # not exist yet, and it is ported rather than simplified away: without it
    # every pre-10:15 candidate would fail on "9 EMA unavailable" and the
    # detection_minutes distribution would be silently truncated.
    ema9_now = ri.ema(bars, 9)
    prev_idx = len(bars) - 4
    if prev_idx >= 8:
        ema9_prev = ri.ema_at(bars, 9, prev_idx)
    elif prev_idx >= 2:
        ema9_prev = ri.ema_at(bars, 3, prev_idx)
    else:
        ema9_prev = None

    if not ema9_now or not ema9_prev:
        return {
            "passes": False, "reason": f"9 EMA unavailable ({n} bars)",
            "price": price, "vwap": vw, "vwap_pct_above": vwap_pct_above,
            "ema9": ema9_now, "ema9_slope": None,
        }

    ema9_slope = ema9_now - ema9_prev
    if ema9_slope <= 0:
        return {
            "passes": False,
            "reason": f"9 EMA flat/descending (slope {_fixed(ema9_slope, 4)})",
            "price": price, "vwap": vw, "vwap_pct_above": vwap_pct_above,
            "ema9": ema9_now, "ema9_slope": ema9_slope,
        }

    # Condition 3: price above the 9 EMA - riding it, not fighting it.
    if price < ema9_now:
        return {
            "passes": False,
            "reason": (f"Price ${_fixed(price, 2)} below 9 EMA "
                       f"${_fixed(ema9_now, 2)}"),
            "price": price, "vwap": vw, "vwap_pct_above": vwap_pct_above,
            "ema9": ema9_now, "ema9_slope": ema9_slope,
        }

    return {
        "passes": True,
        "reason": (f"Price {_fixed(vwap_pct_above, 2)}% above VWAP "
                   f"9 EMA ascending (+{_fixed(ema9_slope, 4)}) "
                   f"Price above EMA"),
        "price": price, "vwap": vw, "vwap_pct_above": vwap_pct_above,
        "ema9": ema9_now, "ema9_slope": ema9_slope,
    }


def first_pass_index(bars: list[dict], min_vwap_pct: float = 0.5,
                     walkback: int = 12) -> int:
    """Index of the bar where conditions FIRST turned true, walking backwards.

    Port of the firstSeenAt walkback at js/runner-scanner.js:1225-1241. The
    live scanner only sees a ticker once it enters the universe, so a name
    that has been passing since 10:20 but only got scanned at 10:50 would
    otherwise be stamped 10:50. The walk steps back from the second-to-last
    bar while conditions still hold, floored at index 9 (ten bars, the
    condition-check minimum) and capped at `walkback` bars.

    Caller must have already confirmed `check_runner_conditions(bars).passes`;
    this returns len(bars) - 1 if the walk finds nothing earlier.

    NOTE for Phase 1: DTO stamps firstSeenAt from this walkback but takes
    `entryPriceAtDetection` from the CURRENT bar's close, so its detection
    time and detection price describe different bars. See the Phase 0 report -
    this needs a decision before detection_minutes is used as a research
    dimension.
    """
    idx = len(bars) - 1
    floor = max(9, len(bars) - walkback)
    for wi in range(len(bars) - 2, floor - 1, -1):
        if not check_runner_conditions(bars[: wi + 1], min_vwap_pct)["passes"]:
            break
        idx = wi
    return idx


def evaluate_ticker(
    ticker: str,
    bars: list[dict] | None,
    *,
    prev_volume: float | None = None,
    prev_close: float | None = None,
    float_m: float | None = None,
    earnings_days_away: float | None = None,
    min_vwap_pct: float = 0.5,
    price_min: float = 5.0,
    price_max: float = 50.0,
    min_rvol: float = 1.5,
    float_min_m: float = 5.0,
    float_max_m: float = 200.0,
    earnings_exclude_days: int = 2,
    min_prev_close: float = 4.0,
    min_trend_consistency: float | None = 45.0,
    null_gates: str = "dto",
    end_et_minutes: int | None = None,
) -> dict:
    """Run DTO's full gate chain over one ticker.

    Gate ORDER is reproduced from js/runner-scanner.js:1096-1176 exactly,
    because the rejection COUNTERS are order-dependent even though the passing
    SET is not (every gate is a conjunct). The parity harness diffs those
    counters, so order matters here.

    `null_gates` selects what a missing gate input means:

      "dto"    - reproduce the JS: a null float, null earnings, or null
                 prev_close passes silently. Use this for parity only.
      "reject" - spec 0 R-4: a gate that cannot run has not passed. A null
                 float / earnings / prev_volume REJECTS and is recorded. This
                 is the required Phase 1 behaviour and is the `pm_vol`
                 null-rate lesson applied at the source.

    Returns {"passes", "counter", "reason", "check", "rvol",
             "trend_consistency", "unavailable"} where `counter` is the DTO
    counter name to increment ("no_bars" | "too_few_bars" | "price" | "rvol" |
    "conditions") and `unavailable` lists gate inputs that were missing.

    DTO folds the excluded / trend / float / earnings rejections into its
    single `rejectedCond` counter. `reason` carries the finer-grained label
    (spec 1.1 wants separate columns for them) so both views are available
    without a second pass.
    """
    unavailable: list[str] = []

    def out(passes, counter=None, reason=None, check=None, rv=None, tc=None):
        return {"passes": passes, "counter": counter, "reason": reason,
                "check": check, "rvol": rv, "trend_consistency": tc,
                "unavailable": unavailable}

    if not bars:
        return out(False, "no_bars", "no bars returned")
    if len(bars) < 10:
        return out(False, "too_few_bars", f"raw bars {len(bars)}/10")

    mkt = market_hours_bars(bars, end_et_minutes)
    if len(mkt) < 10:
        return out(False, "too_few_bars", f"session bars {len(mkt)}/10")

    # Price gate reads the LAST RAW BAR's close, not the last session bar -
    # js/runner-scanner.js:1111 indexes `tickerBars`, not `_marketBars`. With
    # a 9:30-anchored fetch the two coincide; they diverge the moment a caller
    # passes premarket-inclusive bars, so the source behaviour is preserved.
    last_price = bars[-1]["c"]
    if last_price < price_min or last_price > price_max:
        return out(False, "price", f"price {last_price} outside "
                                   f"[{price_min}, {price_max}]")

    # Secondary price gate: a prev close under $4 means the name gapped up out
    # of penny territory overnight.
    if prev_close is None:
        unavailable.append("prev_close")
        if null_gates == "reject":
            return out(False, "price", "prev_close unavailable")
    elif prev_close < min_prev_close:
        return out(False, "price", f"prev_close {prev_close} < {min_prev_close}")

    if ticker in EXCLUDED_TICKERS:
        return out(False, "conditions", "excluded ticker")

    # RVOL gate - session bars only. Premarket volume distorts the ratio and
    # produces false positives on names that spike premarket then fade.
    # Null is a FAIL in both modes here: the JS comment at
    # js/runner-scanner.js:1129-1130 is explicit that unverified volume was a
    # documented corpus integrity failure and must never pass silently.
    if not prev_volume:
        unavailable.append("prev_volume")
        return out(False, "rvol", "prev_volume unavailable")
    rv = ri.rvol(mkt, prev_volume)
    if rv is None or rv < min_rvol:
        return out(False, "rvol", f"rvol {rv} < {min_rvol}", rv=rv)

    check = check_runner_conditions(mkt, min_vwap_pct)
    if not check["passes"]:
        return out(False, "conditions", check["reason"], check=check, rv=rv)

    tc = ri.trend_consistency(mkt)
    # DTO hard-gates TC < 45. The THRESHOLD is corpus-derived and therefore a
    # "conclusion" under spec 0 R-2, but it is also a live condition that
    # changes the passing set, so parity requires it. Pass
    # min_trend_consistency=None to run detection without it.
    if min_trend_consistency is not None and tc is not None and tc < min_trend_consistency:
        return out(False, "conditions", f"trend_consistency {tc} < "
                                        f"{min_trend_consistency}",
                   check=check, rv=rv, tc=tc)

    # Float gate. Finnhub shareOutstanding in millions.
    if float_m is None:
        unavailable.append("float")
        if null_gates == "reject":
            return out(False, "conditions", "float unavailable",
                       check=check, rv=rv, tc=tc)
    elif float_m < float_min_m or float_m > float_max_m:
        return out(False, "conditions", f"float {float_m}M outside "
                                        f"[{float_min_m}, {float_max_m}]",
                   check=check, rv=rv, tc=tc)

    # Earnings gate.
    if earnings_days_away is None:
        unavailable.append("earnings")
        if null_gates == "reject":
            return out(False, "conditions", "earnings unavailable",
                       check=check, rv=rv, tc=tc)
    elif abs(earnings_days_away) <= earnings_exclude_days:
        return out(False, "conditions",
                   f"earnings {earnings_days_away}d away",
                   check=check, rv=rv, tc=tc)

    return out(True, None, check["reason"], check=check, rv=rv, tc=tc)
