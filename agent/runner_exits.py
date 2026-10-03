"""
Runner exit-policy replay (specs/RUNNER_EXIT_SPEC_v1.md).

The runner lane has one exit plan: a static stop at the 5-minute 9 EMA as of
detection, a fixed 2R target, otherwise hold to the close. Whether that plan
leaves money on the table is an empirical question this module makes
answerable -- without changing a single live exit.

Every triggered runner candidate is replayed, on the SAME minute bars, the SAME
fill and the SAME friction the grader used, under a small PRE-REGISTERED family
of alternative exits. The weekly search then scores each alternative as a paired
per-session ΔR against the plan, inside the one FDR batch.

── WHY A FIXED FAMILY, NOT "LEARN THE BEST EXIT" ────────────────────────────
Exit rules are the easiest place in trading to manufacture edge. With hindsight
every trade has a perfect exit (its MFE bar), and a sweep over enough stop /
target / trail / clock parameters always finds one that "would have" worked on
the sample it was fit to. So the family is fixed in code, versioned, and every
member is a counted look. Changing it bumps POLICY_VERSION, and the comparison
reads current-version rows only: no policy is ever tuned on the rows it is
scored on.

Every parameter comes from the plan itself (its 1R / 2R units, its entry, its
stop) or from the setup's own definition (price above the rising 5m 9 EMA and
above VWAP). The only additions are the time-stop clock times, an hourly grid
chosen before any data was seen and stated as a grid, not a finding.

── THE CONSTRUCTION ARMS (family v4, specs/RUNNER_TRADE_CONSTRUCTION_v1.md) ──
The v1-v3 family varied only the EXIT on a fixed stop. The 2026-10-02 review
found the stop itself is the leak: the 9 EMA at detection sits under 0.5 daily
ATR on ~99% of candidates (median ~0.8% of price), inside ordinary intraday
noise, and 5 bps a side costs a median ~0.12R on a stop that tight. The miner
cannot learn a stop from existing rows -- they all share one stop rule -- so
the arms create the variation:

  arm_a  stop floor: the LOWER of the 9 EMA stop and entry - 1.0 ATR14, with
         the 2R target on that new risk.
  arm_b  the Runner rules on the DTO dashboard's trade-rules card, as written:
         sell half at +1R and move the stop to breakeven, trail the stop up to
         the 9 EMA on each 5m bar close (a close below it exits at the next
         open), hold to 2:00 PM CT (15:00 ET), no target.
  arm_c  arm A's stop with arm B's management. The 9 EMA trail arms at the 1R
         partial: trailed from entry, the first 5m close would ratchet the
         stop straight back up to the EMA and undo the floor.

Sizing is by risk, exactly as today: an arm with a wider stop buys fewer
shares for the same dollars at risk, so each arm's R is booked in ITS OWN risk
unit and the paired ΔR against the plan is a difference in dollars at risk.
The 1R partial raises win rate by construction; only ΔR is tested.

── WHY WIN RATE IS NOT THE SCORE ─────────────────────────────────────────────
A tighter exit always raises win rate. The local corpus (2026-09-11..21, sim
v3, not production) made that concrete: a 0.5R target wins 72% of the time
against the plan's 48%, and earns about a quarter of the plan's mean R. Every
comparison here is in mean R (planned-risk units), paired against the plan.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time as dtime, timedelta
from typing import Any

from . import runner_indicators as ri
from .grading import (CURRENT_SIM_VERSION, EOD_FLATTEN_MINUTES, _clock_to_bar_index, _is_thin,
                      _parse_clock)
from .stats import Comparison
from .util import ET, market_close_et

__all__ = [
    "POLICY_VERSION",
    "REFERENCE",
    "ExitPolicy",
    "POLICIES",
    "ARMS",
    "RATCHET",
    "EXPECTANCY_DIMENSION",
    "arm_expectancy_comparisons",
    "policy_stop",
    "fill_index",
    "replay",
    "current_replay_rows",
    "exit_policy_comparisons",
    "exit_md",
]

# Bump when the family or any member's semantics change. Rows from different
# versions describe different experiments and are never pooled.
# v2: R booked from the worse of planned entry and fill (grader's r_planned
# fix); breakeven/scale levels measured from that basis, not the plan's entry.
# v3: the session exit is the live desk's 15:55 flatten (the open of the bar
# EOD_FLATTEN_MINUTES before the close), as the grader's sim_version 7.
# v4: the construction arms (arm_a/b/c): a stop floor in ATR, the dashboard's
# Runner rules, and the two combined; R per policy in that policy's own risk
# unit; stop/target used and friction-in-R stored per row.
POLICY_VERSION = 4

REFERENCE = "plan"
DIMENSION = "runner_exit_policy"

# Trail modes. "ema9_5m"/"vwap_5m" exit at the next open after a 5m close below
# the line. "ema9_ratchet" is the dashboard's trail: the STOP moves up to the
# 5m 9 EMA at each completed 5m bar (never down), and a 5m close below the EMA
# also exits at the next open.
RATCHET = "ema9_ratchet"


@dataclass(frozen=True)
class ExitPolicy:
    """One pre-registered exit. Levels are in R of the policy's own risk
    (entry - its stop), measured from the planned entry.

    target_r        fixed take-profit; None = no target
    breakeven_at_r  once touched, the stop moves to the fill from the NEXT bar
                    (intra-bar order is unknowable, so the move cannot protect
                    the bar that armed it)
    scale_at_r      sell half at this level; the rest follows the other rules
    trail           "ema9_5m" / "vwap_5m": exit at the next bar's open after a
                    5-minute close below that line -- the setup's own
                    invalidation, observable only once the 5m bar has closed.
                    "ema9_ratchet": the stop also follows the 5m 9 EMA up
    trail_arm_r     the trail only engages once this much R has traded
                    (None = from the fill)
    time_stop       exit at the close of the first bar at/after this clock
                    time; a trade that could not follow it yields no row
    hold_until      the same exit, but a session that closes before it simply
                    ends at the desk's flatten (the row is kept)
    stop_floor_atr  the stop is the LOWER of the plan stop and
                    entry - this many ATR14; risk and target move with it
    """
    name: str
    question: str
    target_r: float | None = 2.0
    breakeven_at_r: float | None = None
    scale_at_r: float | None = None
    trail: str | None = None
    time_stop: dtime | None = None
    trail_arm_r: float | None = None
    hold_until: dtime | None = None
    stop_floor_atr: float | None = None


POLICIES: tuple[ExitPolicy, ...] = (
    ExitPolicy(REFERENCE, "the current plan: static stop, 2R target, else close"),
    ExitPolicy("no_target", "does the 2R target cut winners short?",
               target_r=None),
    ExitPolicy("breakeven_1r", "is giving back open profit the leak?",
               breakeven_at_r=1.0),
    ExitPolicy("ema9_trail", "exit when the setup breaks (5m close < 9 EMA)",
               target_r=None, trail="ema9_5m"),
    ExitPolicy("vwap_trail", "exit when price loses VWAP (5m close < VWAP)",
               target_r=None, trail="vwap_5m"),
    ExitPolicy("scale_1r_ema9", "bank half at 1R, trail the rest on the 9 EMA",
               target_r=None, scale_at_r=1.0, trail="ema9_5m"),
    ExitPolicy("time_1100", "the plan, flat by 11:00", time_stop=dtime(11, 0)),
    ExitPolicy("time_1200", "the plan, flat by 12:00", time_stop=dtime(12, 0)),
    ExitPolicy("time_1300", "the plan, flat by 13:00", time_stop=dtime(13, 0)),
    ExitPolicy("time_1400", "the plan, flat by 14:00", time_stop=dtime(14, 0)),
    ExitPolicy("time_1500", "the plan, flat by 15:00", time_stop=dtime(15, 0)),
    # ---- the construction arms (v4) -------------------------------------
    ExitPolicy("arm_a_floor",
               "stop floor: lower of the 9 EMA stop and entry - 1 ATR, 2R target on that risk",
               stop_floor_atr=1.0),
    ExitPolicy("arm_b_dashboard",
               "the dashboard Runner rules: half + breakeven at 1R, trail the 9 EMA on "
               "5m closes, out by 2:00 PM CT",
               target_r=None, scale_at_r=1.0, breakeven_at_r=1.0, trail=RATCHET,
               hold_until=dtime(15, 0)),
    ExitPolicy("arm_c_floor_dashboard",
               "arm A's stop with arm B's management (the 9 EMA trail arms at the 1R partial)",
               target_r=None, scale_at_r=1.0, breakeven_at_r=1.0, trail=RATCHET,
               trail_arm_r=1.0, hold_until=dtime(15, 0), stop_floor_atr=1.0),
)

# The arms whose comparison is the review's question; the rest of the family
# is the exit-only grid from RUNNER_EXIT_SPEC_v1.
ARMS: tuple[str, ...] = ("arm_a_floor", "arm_b_dashboard", "arm_c_floor_dashboard")

_BY_NAME = {p.name: p for p in POLICIES}


# ---------------------------------------------------------------- the replay

def _naive_et(ts):
    """ET wall-clock, tz stripped -- how first_pass_et is stored. A tz-aware
    value written to a TIMESTAMP column is shifted into the writer's zone
    (Central on the local box, UTC on Neon), which moved every exit an hour
    or four off the clock the time-of-day question is asked in."""
    if ts is None or getattr(ts, "tzinfo", None) is None:
        return ts
    return ts.astimezone(ET).replace(tzinfo=None)


def fill_index(bars: list[dict], cutoff) -> int | None:
    """Index of the first bar at/after `cutoff` (naive ET), matching the slice
    the evening grader hands simulate_pick. None when no bar qualifies."""
    if cutoff is None:
        return 0 if bars else None
    for i, b in enumerate(bars):
        if b["t"].replace(tzinfo=None) >= cutoff:
            return i
    return None


def _five_minute_marks(bars: list[dict]) -> dict[int, dict]:
    """{index of the LAST 1m bar in each clock-aligned 5m bucket: its close,
    the 5m 9 EMA through that bucket, and session VWAP through that bar}.

    Clock-aligned (09:30, 09:35, ...) like the Alpaca 5Min bars the scan reads,
    not positional: a thin name with missing minutes would otherwise smear
    buckets across the clock. The EMA is SMA-seeded, as detection computes it.
    Pre-09:30 bars are excluded, as runner_scan.market_hours_bars excludes them.
    """
    buckets: list[tuple[int, int]] = []   # (bucket key, last 1m index)
    for i, b in enumerate(bars):
        m = ri.et_minutes(b["t"])
        if m < ri.SESSION_OPEN_ET_MINUTES:
            continue
        key = (m - ri.SESSION_OPEN_ET_MINUTES) // 5
        if buckets and buckets[-1][0] == key:
            buckets[-1] = (key, i)
        else:
            buckets.append((key, i))

    marks: dict[int, dict] = {}
    k = 2.0 / (9 + 1)
    closes: list[float] = []
    ema: float | None = None
    cum_pv = cum_v = 0.0
    j = 0
    for _, last in buckets:
        while j <= last:
            b = bars[j]
            if ri.et_minutes(b["t"]) >= ri.SESSION_OPEN_ET_MINUTES:
                v = b.get("v") or 0
                cum_pv += ri._typical_price(b) * v
                cum_v += v
            j += 1
        close = bars[last]["c"]
        closes.append(close)
        if len(closes) == 9:
            ema = sum(closes) / 9
        elif len(closes) > 9:
            ema = close * k + ema * (1 - k)
        marks[last] = {"close": close, "ema9": ema,
                       "vwap": (cum_pv / cum_v) if cum_v else None}
    return marks


def _walk(p: ExitPolicy, bars: list[dict], fill_idx: int, fill: float,
          entry: float, stop: float, risk: float, marks: dict[int, dict],
          slip: float, min_bar_volume: float, flatten_idx: int | None,
          time_idx: int | None, eod_idx: int | None = None,
          raw_open: float | None = None) -> dict:
    """One policy, long only, walked bar by bar from the fill. Within a bar the
    order is the grader's conservative one: a pending trail exit (at the open),
    then the stop, flatten, time stop, target, and only then anything that
    arms for a LATER bar (breakeven, trail, a ratcheted stop).

    `stop` and `risk` are THIS policy's (an arm with a stop floor passes its
    wider stop); every R level and the booked R are in that risk unit."""
    # R is booked from `ref`, the worse of the planned entry and the fill -- the
    # grader's r_planned basis. The target is the PLAN's level (what the live
    # bracket carries); open-profit levels (breakeven, scale) are measured on
    # the position as booked, from `ref`.
    ref = max(entry, fill)
    target = entry + p.target_r * risk if p.target_r is not None else None
    be_level = ref + p.breakeven_at_r * risk if p.breakeven_at_r is not None else None
    scale_level = ref + p.scale_at_r * risk if p.scale_at_r is not None else None
    arm_level = ref + p.trail_arm_r * risk if p.trail_arm_r is not None else None

    active_stop, stop_kind = stop, "stop"
    be_pending = be_done = False
    ratchet_to: float | None = None
    trail_armed = arm_level is None
    open_frac, banked = 1.0, 0.0
    trail_pending = False
    best_px, best_idx = fill, fill_idx
    # Friction actually charged on this path, in price: the entry side counts
    # only where it moved the booked basis (ref), the exit side per fraction.
    raw = fill / (1 + slip) if raw_open is None else raw_open
    cost_px = max(entry, fill) - max(entry, raw)

    def r_at(px: float) -> float:
        return ((px * (1 - slip)) - ref) / risk

    def done(px: float, reason: str, idx: int) -> dict:
        exit_cost = cost_px + open_frac * px * slip
        return {"policy": p.name,
                "r_planned": round(banked + open_frac * r_at(px), 3),
                "exit_reason": reason,
                "exit_et": _naive_et(bars[idx].get("t")),
                "mfe_r": round((best_px - ref) / risk, 3),
                "mfe_et": _naive_et(bars[best_idx].get("t")),
                "stop_used": round(stop, 4),
                "target_used": round(target, 4) if target is not None else None,
                "cost_r": round(exit_cost / risk, 3)}

    for i in range(fill_idx, len(bars)):
        b = bars[i]
        if eod_idx is not None and i >= eod_idx and i > fill_idx:
            return done(b["o"], "eod", i)          # the desk's 15:55 flatten
        if trail_pending:
            return done(b["o"], "ema9_close" if p.trail == RATCHET else p.trail, i)
        if be_pending:
            if fill > active_stop:
                active_stop, stop_kind = fill, "breakeven"
            be_pending, be_done = False, True
        if ratchet_to is not None:
            if ratchet_to > active_stop:
                active_stop, stop_kind = ratchet_to, "trail_stop"
            ratchet_to = None
        thin = _is_thin(b, min_bar_volume)
        if b["h"] > best_px:
            best_px, best_idx = b["h"], i
        if not thin and b["l"] <= active_stop:
            px = b["o"] if b["o"] < active_stop else active_stop
            return done(px, stop_kind, i)
        if flatten_idx is not None and i >= flatten_idx:
            return done(b["c"], "flatten", i)
        if time_idx is not None and i >= time_idx:
            return done(b["c"], "time_stop", i)
        if target is not None and not thin and b["h"] >= target:
            return done(target, "target", i)
        if scale_level is not None and open_frac == 1.0 and not thin \
                and b["h"] >= scale_level:
            banked, open_frac = 0.5 * r_at(scale_level), 0.5
            cost_px += 0.5 * scale_level * slip
        if be_level is not None and not be_done and not thin and b["h"] >= be_level:
            be_pending = True
        # The touch that arms a trail happened inside this bar, before its
        # close, so the 5m close that ends this bar may already trail.
        if not trail_armed and not thin and b["h"] >= arm_level:
            trail_armed = True
        if p.trail and trail_armed and i in marks:
            m = marks[i]
            line = m["vwap"] if p.trail == "vwap_5m" else m["ema9"]
            if line is not None:
                if p.trail == RATCHET:
                    ratchet_to = line            # effective from the NEXT bar
                if m["close"] < line:
                    if i + 1 >= len(bars):
                        return done(b["c"], "ema9_close" if p.trail == RATCHET else p.trail, i)
                    trail_pending = True
    return done(bars[-1]["c"], "eod", len(bars) - 1)


def policy_stop(p: ExitPolicy, entry: float, stop: float,
                atr14: float | None) -> float | None:
    """The stop policy `p` carries: the plan's, or for a stop-floor arm the
    LOWER of the plan's and entry - k x ATR14. None when the floor needs an
    ATR the candidate does not have (that arm then has no row)."""
    if p.stop_floor_atr is None:
        return stop
    if not atr14 or atr14 <= 0:
        return None
    return min(stop, entry - p.stop_floor_atr * float(atr14))


def replay(bars: list[dict], fill_idx: int | None, entry: float, stop: float, *,
           slippage_bps: float = 0.0, min_bar_volume: float = 0,
           flatten_at: str | None = None, session_date: date | None = None,
           atr14: float | None = None,
           policies: tuple[ExitPolicy, ...] = POLICIES) -> list[dict]:
    """Replay one triggered long runner under every policy.

    `bars` is the FULL session (the 5m EMA and VWAP need it); the trade fills
    at bars[fill_idx]["o"] plus slippage, exactly as simulate_pick fills an
    `open` entry on the sliced series. The caller has already established the
    fill is valid (simulate_pick's invalid_fill gates); this does not re-judge
    it. A time stop at/after the session close, or at/before the fill, is not
    a policy this trade could have followed, so it yields no row; a
    `hold_until` past a shortened session's close just ends at the desk's
    flatten. A stop-floor arm needs `atr14` (the daily ATR known at entry) and
    yields no row without it.
    """
    risk = entry - stop
    if not bars or fill_idx is None or fill_idx >= len(bars) or risk <= 0:
        return []
    slip = float(slippage_bps or 0) / 10000.0
    raw_open = float(bars[fill_idx]["o"])
    fill = raw_open * (1 + slip)
    trade = bars[fill_idx:]
    close_t = market_close_et(session_date) if session_date else dtime(16, 0)

    flatten_idx = None
    if flatten_at:
        parsed = _parse_clock(flatten_at)
        if parsed is not None and parsed < close_t:
            local = _clock_to_bar_index(parsed, trade)
            flatten_idx = None if local is None else fill_idx + local

    # The desk's session exit, as the grader applies it (sim_version 7).
    from datetime import datetime as _dt
    eod_t = (_dt.combine(date.min, close_t) - timedelta(minutes=EOD_FLATTEN_MINUTES)).time()
    local_eod = _clock_to_bar_index(eod_t, trade)
    eod_idx = None if local_eod is None else fill_idx + local_eod

    marks = _five_minute_marks(bars)
    fill_t = trade[0]["t"]
    fill_clock = fill_t.time() if hasattr(fill_t, "time") else fill_t
    out = []
    for p in policies:
        time_idx = None
        if p.time_stop is not None:
            if p.time_stop >= close_t or p.time_stop <= fill_clock:
                continue
            local = _clock_to_bar_index(p.time_stop, trade)
            time_idx = None if local is None else fill_idx + local
        if p.hold_until is not None:
            if p.hold_until <= fill_clock:
                continue
            if p.hold_until < eod_t:
                local = _clock_to_bar_index(p.hold_until, trade)
                time_idx = None if local is None else fill_idx + local
        p_stop = policy_stop(p, entry, stop, atr14)
        if p_stop is None or fill <= p_stop:
            continue
        out.append(_walk(p, bars, fill_idx, fill, entry, p_stop, entry - p_stop, marks,
                         slip, min_bar_volume, flatten_idx, time_idx, eod_idx,
                         raw_open=raw_open))
    return out


# ---------------------------------------------------------------- the search

def current_replay_rows(con: Any, sessions: int = 60) -> list[dict]:
    """Replay rows under the RUNNING code's scan, sim and policy versions."""
    from . import ledger
    from .runner_research import current_versions
    scan_v, sim_v = current_versions()
    since = date.today() - timedelta(days=sessions * 2)
    try:
        rows = ledger.runner_exit_replays_since(con, since)
    except Exception:
        return []
    return [r for r in rows
            if r.get("scan_version") == scan_v and r.get("sim_version") == sim_v
            and r.get("policy_version") == POLICY_VERSION
            and r.get("r_planned") is not None]


def _mean_std(values: list[float]) -> tuple[float | None, float | None]:
    n = len(values)
    if n == 0:
        return None, None
    mean = sum(values) / n
    if n < 2:
        return mean, None
    return mean, (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5


def _by_candidate(rows: list[dict]) -> dict[tuple, dict[str, float]]:
    out: dict[tuple, dict[str, float]] = {}
    for r in rows:
        out.setdefault((r["session_date"], r["ticker"]), {})[r["policy"]] = float(r["r_planned"])
    return out


def exit_policy_comparisons(rows: list[dict]) -> list[Comparison]:
    """One Comparison per non-reference policy: paired ΔR vs the plan.

    The unit is the SESSION. A few hundred candidates on one tape share that
    day's market, so they are one observation of "did this exit beat the plan
    today", not hundreds; n is the number of sessions, the value is each
    session's mean paired ΔR. With no replays there is no family to search
    (like the runner block in integrity.build_search); once there are, every
    registered policy is emitted, including one with no pairs yet -- a look is
    a look (agent/stats.evaluate_search).
    """
    if not rows:
        return []
    cand = _by_candidate(rows)
    out = []
    for p in POLICIES:
        if p.name == REFERENCE:
            continue
        per_session: dict[Any, list[float]] = {}
        pairs = 0
        for (d, _t), m in cand.items():
            if REFERENCE in m and p.name in m:
                per_session.setdefault(d, []).append(m[p.name] - m[REFERENCE])
                pairs += 1
        means = [sum(v) / len(v) for v in per_session.values()]
        mean, std = _mean_std(means)
        out.append(Comparison(
            dimension=DIMENSION, level=p.name, n=len(means), mean_r=mean, std_r=std,
            note=f"paired ΔR vs plan exit, per-session means ({pairs} candidate pairs)"))
    return out


# Beating the plan is not an edge. Sized by risk, an arm with a 1-ATR stop holds
# about a fifth of the plan's shares, so on a field that drifts down after
# entry it loses less by being less exposed (2026-10-02, replayed: every arm
# beat the plan by +0.3..+0.5R per trade and every arm still lost money). The
# relative claim (paired ΔR) says which construction is better; this absolute
# one says whether a construction makes money at all. Both are looks in the
# one batch; a construction is only a live candidate when both hold.
EXPECTANCY_DIMENSION = "runner_arm_expectancy"


def arm_expectancy_comparisons(rows: list[dict]) -> list[Comparison]:
    """One Comparison per construction (the plan and each arm): its per-session
    mean R vs 0, in its own risk unit. The session is the unit, as for ΔR."""
    if not rows:
        return []
    out = []
    for name in (REFERENCE,) + ARMS:
        per_session: dict[Any, list[float]] = {}
        for r in rows:
            if r["policy"] == name:
                per_session.setdefault(r["session_date"], []).append(float(r["r_planned"]))
        means = [sum(v) / len(v) for v in per_session.values()]
        mean, std = _mean_std(means)
        out.append(Comparison(
            dimension=EXPECTANCY_DIMENSION, level=name, n=len(means), mean_r=mean,
            std_r=std, note=(f"per-session mean R of this construction vs 0, own risk unit "
                             f"({sum(len(v) for v in per_session.values())} trades)")))
    return out


def policy_summary(rows: list[dict]) -> list[dict]:
    """Descriptive per-policy numbers (mean R, win rate) for humans and the
    report command. Never a test; the tested quantity is the paired ΔR."""
    by_policy: dict[str, list[float]] = {}
    costs: dict[str, list[float]] = {}
    for r in rows:
        by_policy.setdefault(r["policy"], []).append(float(r["r_planned"]))
        if r.get("cost_r") is not None:
            costs.setdefault(r["policy"], []).append(float(r["cost_r"]))
    out = []
    for p in POLICIES:
        vals = by_policy.get(p.name, [])
        cs = sorted(costs.get(p.name, []))
        out.append({"policy": p.name, "question": p.question, "n": len(vals),
                    "mean_r": (sum(vals) / len(vals)) if vals else None,
                    "win_rate": (sum(v > 0 for v in vals) / len(vals)) if vals else None,
                    "median_cost_r": cs[len(cs) // 2] if cs else None})
    return out


def report_text(rows: list[dict]) -> str:
    """Plain-text report for `run.py runner-exits` (read-only, for humans).

    Win rate is printed beside mean R deliberately, so the trade-off is on the
    page. The ΔR column is UNCORRECTED; only the weekly search's q-value is a
    verdict.
    """
    if not rows:
        return f"No runner exit replays (policy family v{POLICY_VERSION})."
    sessions = sorted({r["session_date"] for r in rows})
    deltas = {c.level: c for c in exit_policy_comparisons(rows)}
    out = [f"Runner exit replays - family v{POLICY_VERSION}, {len(sessions)} session(s) "
           f"{sessions[0]}..{sessions[-1]}",
           "",
           f"{'policy':<23}{'n':>6}{'mean R':>9}{'win%':>7}{'cost R':>8}"
           f"{'sessions':>10}{'dR/session':>12}",
           ]
    for s in policy_summary(rows):
        c = deltas.get(s["policy"])
        win = "n/a" if s["win_rate"] is None else f"{100 * s['win_rate']:.0f}"
        cost = "n/a" if s["median_cost_r"] is None else f"{s['median_cost_r']:.3f}"
        n_sess = str(c.n) if c else ""
        delta = _fmt(c.mean_r) if c else "reference"
        out.append(f"{s['policy']:<23}{s['n']:>6}{_fmt(s['mean_r']):>9}{win:>7}{cost:>8}"
                   f"{n_sess:>10}{delta:>12}")
    out += ["",
            "Each policy's R is in its OWN risk unit (sizing by risk: a wider stop buys "
            "fewer shares for the same dollars), so mean R and dR/session compare dollars "
            "at risk. cost R = median friction charged per trade in that unit. The arms "
            "(arm_a/b/c) are specs/RUNNER_TRADE_CONSTRUCTION_v1.md; a 1R partial raises "
            "win% by construction, so read dR, not win%."]

    plan = [r for r in rows if r["policy"] == REFERENCE]
    stopped = [r for r in plan if r.get("exit_reason") == "stop"]
    gave_back = sum(1 for r in stopped if (r.get("mfe_r") or 0) >= 1.0)
    out += ["",
            f"Give-back: {gave_back}/{len(stopped)} plan stop-outs were up >= 1R first."]

    peaks: dict[int, int] = {}
    for r in rows:
        if r["policy"] == "no_target" and r.get("mfe_et") is not None:
            peaks[r["mfe_et"].hour] = peaks.get(r["mfe_et"].hour, 0) + 1
    if peaks:
        total = sum(peaks.values())
        out += ["", "When the move peaked (no_target: static stop, held to close), by ET hour:"]
        for h in sorted(peaks):
            out.append(f"  {h:02d}:00  {peaks[h]:>5}  {'#' * round(40 * peaks[h] / total)}")
    out += ["", "dR/session is uncorrected. The weekly search's q-value is the only verdict."]
    return "\n".join(out)


def _fmt(x: float | None, digits: int = 3) -> str:
    return "n/a" if x is None else f"{x:+.{digits}f}"


def exit_md(comparisons: list[Comparison], rows: list[dict],
            expectancy: list[Comparison] | None = None) -> str:
    """The miner's exit-policy section. `comparisons` (paired ΔR) and
    `expectancy` (per-construction mean R vs 0) carry batch q-values."""
    if not rows:
        return ("### 4. Exit policies\n\nNo exit replays under the current "
                f"policy family (v{POLICY_VERSION}) yet -- nothing to compare.")
    ref = next((s for s in policy_summary(rows) if s["policy"] == REFERENCE), {})
    lines = [
        "### 4. Exit policies — paired ΔR vs the plan exit",
        "",
        f"Family v{POLICY_VERSION}, fixed in code before any data was seen. Plan "
        f"exit (static 9 EMA stop, 2R target, else close): mean "
        f"{_fmt(ref.get('mean_r'))}R over {ref.get('n', 0)} triggered candidates.",
        "n = sessions; ΔR = that session's mean (policy R − plan R) over the same "
        "candidates, same fills, same friction.",
        "",
        "| policy | question | sessions | mean ΔR/session | q |",
        "|---|---|---|---|---|",
    ]
    for c in comparisons:
        p = _BY_NAME.get(c.level)
        lines.append(
            f"| {c.level} | {p.question if p else ''} | {c.n} | {_fmt(c.mean_r)} | "
            f"{_fmt(c.q_value, 4) if c.q_value is not None else 'n/a'}"
            f"{' *' if c.significant else ''} |")
    if expectancy:
        lines += [
            "",
            "**Does the construction make money at all?** Per-session mean R vs 0, "
            "each in its own risk unit:",
            "",
            "| construction | sessions | mean R/session | q |",
            "|---|---|---|---|",
        ]
        for c in expectancy:
            lines.append(f"| {c.level} | {c.n} | {_fmt(c.mean_r)} | "
                         f"{_fmt(c.q_value, 4) if c.q_value is not None else 'n/a'}"
                         f"{' *' if c.significant else ''} |")
    lines += [
        "",
        "arm_a/arm_b/arm_c change the STOP or the whole management, not only the "
        "exit, and book R in their own risk unit (same dollars at risk, fewer "
        "shares on a wider stop), so their ΔR is a difference in dollars at risk. "
        "A wider stop holds fewer shares, so on a field that drifts down it loses "
        "less by being less exposed: a positive ΔR with a negative mean R is a "
        "smaller loss, not an edge.",
        "",
        "A significant positive ΔR is a CANDIDATE, not a rule. The system "
        "quarantines it itself (lane `runner_exit`) and records one forward "
        "observation per later session; it can change a live order only after it "
        "passes forward validation on those sessions AND Jason approves live management "
        "(specs/RUNNER_EXIT_SPEC_v1.md Phase 4). Do not prescribe an exit from "
        "these point estimates, and do not argue from win rate: a tighter exit or "
        "a 1R partial always wins more often, and that alone says nothing about "
        "mean R.",
    ]
    return "\n".join(lines)
