"""
DTO Runner detection - condition check and gate chain.

Two halves, deliberately separated by a hard line in the file:

  ABOVE the PHASE 1 banner - pure, deterministic, no I/O and no clock reads:
  `check_runner_conditions` (spec 0.3), the ported `EXCLUDED_TICKERS` set, and
  `evaluate_ticker`, the gate chain the parity harness diffs against DTO's own
  rejection counters (spec 0.4). This half is what Phase 0 proved; keep it
  pure so the parity gate stays meaningful.

  BELOW it - `scan()`, the Phase 1 tick that performs provider calls and
  database writes (spec 1.3). Gated by cfg["runner"]["enabled"], default
  false.

Source: js/runner-scanner.js from the DTO Trading App repo (`E7104z/trading`).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, time as dtime

from . import runner_indicators as ri
from .util import ET, log, prev_market_day

__all__ = [
    "EXCLUDED_TICKERS",
    "SCAN_VERSION",
    "scan",
    "apply_network_gates",
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
    defer_network_gates: bool = False,
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

    # RVOL - session bars only. Premarket volume distorts the ratio and
    # produces false positives on names that spike premarket then fade.
    #
    # ── WHY min_rvol CAN BE ZERO ──────────────────────────────────────────
    # DTO's rule is "RVOL >= 1.5", but its RVOL readings are inflated by
    # roughly an order of magnitude, so in practice the gate passed nearly
    # everything and the breadth of its scan list came from the VWAP/EMA
    # conditions alone.
    #
    # The evidence is in DTO's own output. A real 10:30 ET scan list of 46
    # names had a MEDIAN RVOL of 7.0x and included AGNC at 11.2x, EWZ at
    # 10.1x, KHC at 11.1x and ORC at 7.6x - a mortgage REIT, a Brazil ETF,
    # Kraft Heinz and Orchid Island Capital do not all trade at ten times
    # normal volume in the same hour. Measured on the same venue and session,
    # the true cross-sectional median is 0.41x.
    #
    # So porting "RVOL >= 1.5" onto a correctly-scaled RVOL is NOT porting
    # DTO's behaviour - it is applying a filter roughly 17x stricter than the
    # one that actually ran, which cut a 57-candidate list to 7. With
    # min_rvol at 0 the gate is skipped and RVOL is still computed, stored,
    # and shown to the selector, so it informs the choice without silently
    # deciding it. Raise min_rvol once forward evidence says a threshold
    # earns its keep on THIS scale.
    rv = ri.rvol(mkt, prev_volume) if prev_volume else None
    if min_rvol > 0:
        if not prev_volume:
            unavailable.append("prev_volume")
            return out(False, "rvol", "prev_volume unavailable")
        if rv is None or rv < min_rvol:
            return out(False, "rvol", f"rvol {rv} < {min_rvol}", rv=rv)
    elif not prev_volume:
        # Not a rejection, but the absence is still recorded.
        unavailable.append("prev_volume")

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

    # ---- everything below needs a NETWORK lookup ------------------------
    # `defer_network_gates` stops here and reports the ticker as a survivor of
    # every cheap local gate. The scan uses it to fetch float and earnings for
    # the handful of names that got this far instead of the whole ~1,190-symbol
    # universe, then finishes the chain with apply_network_gates(). DTO made
    # the same split for the same reason (js/runner-scanner.js:1149).
    # The parity harness never sets this, so the full chain is unchanged there.
    if defer_network_gates:
        return out(True, None, "pending network gates", check=check, rv=rv, tc=tc)

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


def apply_network_gates(
    res: dict,
    *,
    float_m: float | None,
    earnings_days_away: float | None,
    float_min_m: float = 5.0,
    float_max_m: float = 200.0,
    earnings_exclude_days: int = 2,
    null_gates: str = "dto",
) -> dict:
    """Finish a deferred gate chain with the float and earnings gates.

    `res` is what evaluate_ticker(defer_network_gates=True) returned. Returns a
    verdict in the same shape, so callers handle both identically.

    On `null_gates="dto"` a missing float or earnings PASSES and is recorded in
    `unavailable` — matching DTO, which fails these gates open. That is a
    deliberate reversal of the original spec's R-4 ("a gate that cannot run has
    not passed"): across a ~1,190-symbol universe, yfinance float and earnings
    coverage is patchy enough that rejecting on absence removes a large share
    of legitimate candidates and would not reproduce the desk this port exists
    to reproduce. The nulls are still recorded per candidate in `gates_status`
    and counted per session in `gates_unavailable`, so research can filter on
    them later — the information is kept, it just no longer silently kills the
    candidate. Pass `null_gates="reject"` to restore the strict behaviour.
    """
    unavailable = list(res.get("unavailable") or [])

    def out(passes, counter=None, reason=None):
        return {**res, "passes": passes, "counter": counter,
                "reason": reason if reason is not None else res.get("reason"),
                "unavailable": unavailable}

    if float_m is None:
        unavailable.append("float")
        if null_gates == "reject":
            return out(False, "conditions", "float unavailable")
    elif float_m < float_min_m or float_m > float_max_m:
        return out(False, "conditions",
                   f"float {float_m}M outside [{float_min_m}, {float_max_m}]")

    if earnings_days_away is None:
        unavailable.append("earnings")
        if null_gates == "reject":
            return out(False, "conditions", "earnings unavailable")
    elif abs(earnings_days_away) <= earnings_exclude_days:
        return out(False, "conditions", f"earnings {earnings_days_away}d away")

    check = res.get("check") or {}
    return out(True, None, check.get("reason") or "passes")


# ===========================================================================
# PHASE 1 - THE SCAN LOOP
# ===========================================================================
# Everything above this line is pure. Everything below performs I/O and is
# gated by cfg["runner"]["enabled"], which defaults to false.


# Float/earnings are per-symbol network round trips with slow-moving answers,
# so they are fetched once per symbol per session and reused across the ~60
# scan ticks a session contains. Keyed by (session_date, symbol) so the cache
# cannot leak across days in a warm serverless container.
_GATE_CACHE: dict[tuple, dict] = {}


def _session_gate_cache(d) -> dict:
    stale = [k for k in _GATE_CACHE if k[0] != d]
    for k in stale:
        _GATE_CACHE.pop(k, None)
    return _GATE_CACHE


def _price_tier(price: float | None) -> str | None:
    if price is None:
        return None
    if price < 20.0:
        return "5-20"
    return "20-50"


def _fetch_gate_inputs(md, symbols: list[str], d, con=None) -> tuple[dict, dict]:
    """Float (in millions) and earnings-days-away for a SMALL set of symbols.

    ── CALL THIS LAST, NEVER FOR THE WHOLE UNIVERSE ──────────────────────────
    These are the only network round trips in the gate chain, and they are slow
    per-symbol yfinance lookups. They must run AFTER every cheap local gate, so
    only the handful of genuine survivors pay for them.

    An earlier version prefetched float and earnings for the entire universe up
    front. At 84 symbols that cost 15 seconds; at the real population of ~1,190
    it would be minutes and would blow the serverless function limit outright.
    DTO hit exactly this and fixed it the same way (js/runner-scanner.js:1149,
    "Network gates LAST (perf fix 2026-06-24) ... so only a handful of genuine
    candidates per scan hit Finnhub instead of every RVOL-passing ticker").

    ── A PORTING DIVERGENCE WORTH KNOWING ────────────────────────────────────
    DTO's float gate reads Finnhub `shareOutstanding`, and its own comment says
    plainly that this is TOTAL shares, not free float ("for large-caps it runs
    into the billions (NVDA ~24,000M)"). Its [5M, 200M] band was calibrated
    against that quantity.

    Trade Agent has no Finnhub key. `providers.fundamentals()` returns yfinance
    `floatShares` - genuine free float, which for most names is materially
    SMALLER than shares outstanding, so applying DTO's band to it is a STRICTER
    filter, not an equivalent one. Using the number we actually have and
    labelling it honestly beats inventing a conversion factor; the band must be
    re-derived against this desk's own forward evidence.
    """
    cache = _session_gate_cache(d)
    need = [s for s in symbols if (d, s) not in cache]

    if need:
        floats: dict[str, float | None] = {}
        earnings: dict[str, int | None] = {}
        try:
            # `con` routes this through the ledger-backed fundamentals cache
            # (3-session TTL). The in-process cache above only helps inside one
            # warm container, and a cron tick is frequently a cold start.
            fund = md.fundamentals(need, con)
            for s, row in (fund or {}).items():
                fs = (row or {}).get("float_shares")
                floats[s] = (float(fs) / 1e6) if fs else None
        except Exception as e:
            log("runner", f"WARN fundamentals fetch failed for {len(need)} symbols: {e}")
        try:
            earn = md.next_earnings(need)
            for s, days in (earn or {}).items():
                earnings[s] = days
        except Exception as e:
            log("runner", f"WARN next_earnings fetch failed for {len(need)} symbols: {e}")
        for s in need:
            cache[(d, s)] = {"float_m": floats.get(s), "earnings_days_away": earnings.get(s)}

    out_f = {s: cache.get((d, s), {}).get("float_m") for s in symbols}
    out_e = {s: cache.get((d, s), {}).get("earnings_days_away") for s in symbols}
    return out_f, out_e


def scan(md, cfg: dict, *, at: datetime, con=None) -> dict:
    """One scan tick (RUNNER_PORT_SPEC_v1 §1.3).

    `cfg` is the `runner` config block. `at` is the tick time (ET). `con` is an
    open ledger connection; when None a connection is opened and closed here.

    Returns a stats dict. Never raises for data reasons - a provider failure
    produces a recorded, degraded session rather than an exception, because the
    caller (the intraday cron) also runs the live position monitor and must not
    be destabilised by this lane.
    """
    t0 = time.monotonic()
    # DTO fails the float/earnings gates OPEN. Across ~1,190 symbols,
    # yfinance coverage is patchy enough that rejecting on absence would
    # remove a large share of legitimate candidates; the nulls are recorded
    # in gates_status and gates_unavailable instead. "reject" restores the
    # original strict behaviour.
    null_policy = str(cfg.get("null_gates", "dto"))
    owns_con = con is None
    if owns_con:
        from . import ledger as _ledger
        con = _ledger.connect()
    from . import ledger
    from . import runner_universe

    d = at.date()
    counters = {"rejected_no_bars": 0, "rejected_too_few_bars": 0,
                "rejected_price": 0, "rejected_rvol": 0,
                "rejected_conditions": 0, "rejected_float": 0,
                "rejected_earnings": 0, "rejected_excluded": 0}
    unavailable = {"float": 0, "earnings": 0, "prev_volume": 0, "prev_close": 0}

    try:
        # -- 0. feed entitlement ---------------------------------------------
        # This account's SIP subscription covers historical data but NOT the
        # current session: feed=sip returns 403 for today's bars while iex
        # returns them. Without this probe the 10:30 fetch would fail wholesale
        # and the session would be recorded as a clean zero-candidate
        # stand-down - the exact confusion runner_sessions exists to prevent.
        try:
            md._ensure_feed()
        except Exception as e:
            log("runner", f"WARN feed preflight failed: {e}")
        feed = getattr(getattr(md, "alpaca", None), "feed", "?")
        feed_downgraded = bool(getattr(md, "feed_downgraded", False))
        if feed_downgraded:
            log("runner", "feed downgraded to IEX - IEX prints are a small "
                          "fraction of consolidated volume, so RVOL is not "
                          "comparable and is recorded as null for this session")

        # -- 1. universe -----------------------------------------------------
        universe, ustats = runner_universe.build_universe(md, cfg, at=at, con=con)
        if not universe:
            # An empty universe is a pipeline failure, not a market condition.
            # Record it loudly; do NOT silently no-op into "zero candidates".
            ledger.upsert_runner_session(con, {
                "session_date": d, "universe_size": 0, "candidates_found": 0,
                "provider_degraded": True, "scan_version": SCAN_VERSION,
                "gates_unavailable": json.dumps(unavailable),
            })
            log("runner", "universe empty - recorded provider_degraded, no scan performed")
            return {"universe_size": 0, "candidates": 0, "provider_degraded": True,
                    "elapsed_ms": int((time.monotonic() - t0) * 1000)}

        # -- 2. 5-min bars, session open through `at` ------------------------
        # multi_bars chunks at 100 symbols AND follows next_page_token. Before
        # that fix a multi-page response dropped a contiguous alphabetical tail
        # of symbols, which this loop would have counted as rejected_no_bars -
        # a plausible-looking stand-down produced by an outage.
        start = datetime.combine(d, dtime(9, 30), tzinfo=ET)
        bars_by_sym: dict[str, list[dict]] = {}
        bars_failed = False
        try:
            bars_by_sym = md.alpaca.multi_bars(universe, "5Min", start, at)
        except Exception as e:
            bars_failed = True
            log("runner", f"WARN 5Min bar fetch failed: {e}")
        if not any(bars_by_sym.values()):
            # No bars for ANY symbol during the session window is an outage,
            # never a market state. Say so, loudly and durably.
            bars_failed = True

        # -- 3. previous-session close/volume --------------------------------
        # Normally these arrive with the liquid universe: the same daily-bar
        # fetch that applies the dollar-volume floor produces them, and it is
        # cached for the session. Only symbols the cache missed (screener names
        # outside the liquid list) need a fetch here, so the ~77s daily pass
        # runs once a day rather than once a tick.
        prev_close: dict[str, float | None] = {}
        prev_volume: dict[str, float | None] = {}
        for sym, row in (ustats.get("prev_daily") or {}).items():
            prev_close[sym] = row.get("close")
            prev_volume[sym] = row.get("volume")
        missing = [s_ for s_ in universe if s_ not in prev_volume]
        if missing:
            try:
                pd_ = prev_market_day(d)
                daily = md.alpaca.multi_daily_bars(missing, pd_, d)
                for sym, rows in (daily or {}).items():
                    if rows:
                        prev_close[sym] = rows[0].get("close")
                        prev_volume[sym] = rows[0].get("volume")
            except Exception as e:
                log("runner", f"WARN previous-session daily fetch failed for "
                              f"{len(missing)} uncached symbols: {e}")

        # -- 4. cheap gates first, for the WHOLE universe --------------------
        # Nothing here touches the network. At ~1,190 symbols that matters: the
        # float and earnings lookups are per-symbol yfinance calls, and running
        # them over the full universe would take minutes and blow the function
        # limit. DTO made the same split (js/runner-scanner.js:1149).
        end_min = int(cfg.get("scan_end_et_minutes", 900))
        survivors: list[tuple[str, dict]] = []
        for ticker in sorted(universe):
            bars = bars_by_sym.get(ticker) or []
            res = evaluate_ticker(
                ticker, bars,
                prev_volume=prev_volume.get(ticker),
                prev_close=prev_close.get(ticker),
                min_vwap_pct=float(cfg.get("min_vwap_pct", 0.5)),
                price_min=float(cfg.get("price_min", 5.0)),
                price_max=float(cfg.get("price_max", 50.0)),
                min_rvol=float(cfg.get("min_rvol", 1.5)),
                min_trend_consistency=cfg.get("min_trend_consistency", 45.0),
                null_gates=null_policy,
                end_et_minutes=end_min,
                defer_network_gates=True,
            )
            for key in res["unavailable"]:
                if key in unavailable:
                    unavailable[key] += 1
            if res["passes"]:
                survivors.append((ticker, res))
            else:
                counters[_counter_column(res)] += 1

        # -- 5. network gates, for survivors ONLY ----------------------------
        floats, earnings = _fetch_gate_inputs(
            md, [t for t, _ in survivors], d, con)

        found = 0
        for ticker, res in survivors:
            bars = bars_by_sym.get(ticker) or []
            res = apply_network_gates(
                res,
                float_m=floats.get(ticker),
                earnings_days_away=earnings.get(ticker),
                float_min_m=float(cfg.get("float_min_m", 5.0)),
                float_max_m=float(cfg.get("float_max_m", 200.0)),
                earnings_exclude_days=int(cfg.get("earnings_exclude_days", 2)),
                null_gates=null_policy,
            )
            for key in res["unavailable"]:
                if key in unavailable:
                    unavailable[key] += 1

            if not res["passes"]:
                counters[_counter_column(res)] += 1
                continue

            mkt = market_hours_bars(bars, end_min)

            # First-pass anchoring. DTO walks firstSeenAt back up to 12 bars to
            # find when conditions actually turned true, but then takes its
            # detectionPrice from the CURRENT bar - so its detection time and
            # detection price describe different bars. That is incoherent for a
            # simulator that enters at "the open of the bar following
            # first_pass_et": the entry would be anchored to a bar whose price
            # was never recorded. Here the whole snapshot is taken AT the
            # walked-back bar, so first_pass_et, detection_price, ema9, vwap,
            # stop and target all describe one bar.
            idx = first_pass_index(mkt, float(cfg.get("min_vwap_pct", 0.5))) \
                if cfg.get("walkback", True) else len(mkt) - 1
            snap_bars = mkt[: idx + 1]
            snap = check_runner_conditions(snap_bars, float(cfg.get("min_vwap_pct", 0.5)))
            if not snap["passes"]:
                # Defensive: first_pass_index only returns indices where the
                # check passed, so this is unreachable by construction. If it
                # ever fires, fall back to the confirmed final bar rather than
                # storing a snapshot that never satisfied the conditions.
                snap_bars, snap = mkt, res["check"]

            detection_price = snap["price"]
            ema9 = snap["ema9"]
            stop_dist = (detection_price - ema9) if (detection_price and ema9) else None
            if not stop_dist or stop_dist <= 0:
                # A non-positive stop distance is not a tradeable plan; storing
                # it would put a negative-risk row into the evidence base.
                counters["rejected_conditions"] += 1
                continue

            # Minimum stop distance. When price sits almost exactly on the 9
            # EMA at detection, stop_dist collapses toward zero and every R
            # computed against it is arithmetically valid and meaningless -
            # measured live on 2026-09-11, ATEC detected with a $0.01 stop on a
            # $10.19 stock and graded -5.15R, which describes noise, not the
            # setup. Rows like that would silently corrupt the Phase 1 baseline
            # that Phase 1 exists to produce.
            #
            # DTO applies the same floor (js/runner-scanner.js:245,
            # `minStop = Math.max(0.10, entry * 0.005)`, commented "stop too
            # tight - R calc meaningless"). It is a sanity floor on tradeable
            # geometry rather than a corpus-derived edge claim, so porting the
            # numbers is consistent with R-2.
            min_stop = max(float(cfg.get("min_stop_abs", 0.10)),
                           detection_price * float(cfg.get("min_stop_pct", 0.005)))
            if stop_dist < min_stop:
                counters["rejected_conditions"] += 1
                log("runner", f"  reject {ticker}: stop_dist {stop_dist:.4f} below "
                              f"minimum {min_stop:.4f} (price sits on the 9 EMA)")
                continue

            first_pass_et = snap_bars[-1]["t"]
            pc = prev_close.get(ticker)
            row = {
                "session_date": d,
                "ticker": ticker,
                "first_pass_et": first_pass_et.replace(tzinfo=None),
                "detection_minutes": ri.et_minutes(first_pass_et),
                "scan_pass_et": at.replace(tzinfo=None),
                "detection_price": detection_price,
                "vwap": snap["vwap"],
                "vwap_pct_above": snap["vwap_pct_above"],
                "ema9": ema9,
                "ema9_slope": snap["ema9_slope"],
                # An IEX-only session sees a few percent of consolidated
                # volume, so the ratio is not an RVOL - recording the number
                # anyway would put a uniformly wrong figure in front of the
                # selector and into the research corpus.
                "rvol": (None if feed_downgraded
                         else ri.rvol(snap_bars, prev_volume.get(ticker))),
                "trend_consistency": ri.trend_consistency(snap_bars),
                "change_pct": (((detection_price - pc) / pc) * 100) if pc else None,
                "float_shares": floats.get(ticker),
                "earnings_days_away": earnings.get(ticker),
                "prev_close": pc,
                "prev_volume": prev_volume.get(ticker),
                "price_tier": _price_tier(detection_price),
                "stop_price": ema9,
                "target_price": detection_price + 2 * stop_dist,
                "stop_dist": stop_dist,
                "gates_status": json.dumps({
                    "float": "pass" if floats.get(ticker) is not None else "unavailable",
                    "earnings": "pass" if earnings.get(ticker) is not None else "unavailable",
                    "prev_volume": "pass" if prev_volume.get(ticker) else "unavailable",
                    "walkback_bars": len(mkt) - 1 - idx,
                    "feed": feed,
                    "rvol_unavailable_reason": ("feed_downgraded_iex"
                                                if feed_downgraded else None),
                }),
                "scan_version": SCAN_VERSION,
            }
            if ledger.save_runner_candidate(con, row):
                found += 1
                log("runner", f"  candidate {ticker} @ {detection_price:.2f} "
                              f"(det {row['detection_minutes']}m, rvol "
                              f"{(row['rvol'] or 0):.2f}, stop {stop_dist:.2f})")

        # -- 6. session telemetry -------------------------------------------
        scanned = len(universe)
        degraded = (bool(ustats.get("provider_degraded"))
                    or bars_failed
                    or any(v > scanned / 2 for v in unavailable.values()))
        prior = ledger.runner_session(con, d) or {}
        total_candidates = len(ledger.runner_candidate_tickers(con, d))
        payload = {
            "session_date": d,
            "universe_size": scanned,
            "scan_passes": (prior.get("scan_passes") or 0) + 1,
            "candidates_found": total_candidates,
            "gates_unavailable": json.dumps({**unavailable, "feed": feed,
                                             "feed_downgraded": feed_downgraded,
                                             "bars_failed": bars_failed}),
            "provider_degraded": degraded,
            "scan_version": SCAN_VERSION,
        }
        for k, v in counters.items():
            payload[k] = v
        ledger.upsert_runner_session(con, payload)

        elapsed = int((time.monotonic() - t0) * 1000)
        log("runner", f"scan {at:%H:%M} - {scanned} scanned, {found} new, "
                      f"{total_candidates} total candidates, {elapsed}ms"
                      + (" [DEGRADED]" if degraded else ""))
        return {"universe_size": scanned, "new_candidates": found,
                "candidates": total_candidates, "provider_degraded": degraded,
                "rejections": counters, "gates_unavailable": unavailable,
                "elapsed_ms": elapsed}
    finally:
        if owns_con:
            con.close()


def _counter_column(res: dict) -> str:
    """Map a gate-chain verdict onto its runner_sessions counter.

    evaluate_ticker reports DTO's five coarse counters (so the parity harness
    can diff them) plus a finer `reason`. runner_sessions has separate columns
    for float / earnings / excluded, which DTO folds into `conditions`. This
    splits them back out - the coarse counter is what DTO logged, the fine one
    is what actually makes a zero-candidate day diagnosable.
    """
    reason = res.get("reason") or ""
    if res["counter"] == "conditions":
        if reason == "excluded ticker":
            return "rejected_excluded"
        if reason.startswith("float"):
            return "rejected_float"
        if reason.startswith("earnings"):
            return "rejected_earnings"
        return "rejected_conditions"
    return f"rejected_{res['counter']}"
