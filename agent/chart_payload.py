"""Chart drawer payload builder (specs/CHART_DRAWER_SPEC_v1.md).

Pure(ish) server-side computation for the strategy-aware chart drawer. Every
strategy number the drawer draws -- VWAP, premarket VWAP, the 9 EMA, levels,
R, ATR bands -- is computed here in Python, with the SAME modules that
produced the original ledger value (R-0):
  - morning lane   -> agent.indicators (SMA-seeded EMA)
  - runner lane    -> agent.runner_indicators (ri; the DTO-parity port)
These two modules intentionally produce different EMAs from the same bars.
Never substitute one for the other, and never use a browser-side EMA.

Read-only: this module issues no ledger writes and no schema migrations. The
one exception (necessary, not incidental) is that the replay-vs-ledger guard
in `build()` calls `agent.grading.simulate_pick` -- a pure function, no
writes -- to recompute what the evening loop already computed, and compares
the two. A mismatch is surfaced in `guards`, never hidden (R-4).

Ledger timestamps such as `first_pass_et` are naive Eastern Time wall-clock.
They are localized to America/New_York before conversion to epoch
milliseconds -- treating them as UTC shifts every marker by 4-5 hours (R-6).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Any

from . import indicators as ind
from . import ledger
from . import runner_indicators as ri
from .config import Config
from .grading import (CURRENT_SIM_VERSION, RUNNER_MIN_REWARD_R, classify_shadow,
                      realized_r, same_semantics, simulate_pick)
from .grading_context import build_grading_context
from .providers import MarketData
from .runner_scan import DETECTION_BAR_MINUTES, earliest_entry_time, honest_entry_time
from .util import ET, log, market_close_et, prev_market_day

_REPLAY_TOL = 1e-6


# --------------------------------------------------------------- time utils

def _naive(ts):
    return ts.replace(tzinfo=None) if getattr(ts, "tzinfo", None) is not None else ts


def _localize_et(ts) -> datetime | None:
    """Naive-or-aware datetime -> aware America/New_York.

    Ledger timestamps are naive ET wall-clock (R-6); an already tz-aware
    datetime (provider bars) is converted, never re-localized.
    """
    if ts is None:
        return None
    if ts.tzinfo is not None:
        return ts.astimezone(ET)
    return ts.replace(tzinfo=ET)


def _ms(ts) -> int | None:
    aware = _localize_et(ts)
    return int(aware.timestamp() * 1000) if aware is not None else None


def _round(v, n=4):
    return round(float(v), n) if v is not None else None


# ------------------------------------------------------------------- to_kline

def to_kline(bars: list[dict]) -> list[dict]:
    """Bars (naive-or-aware ET `t`) -> KLineChart-ready dicts.

    Sorted ascending, deduplicated by timestamp -- KLineChart requires a
    strictly ascending, unique timestamp series.
    """
    by_ms: dict[int, dict] = {}
    for b in bars:
        ms = _ms(b.get("t"))
        if ms is None:
            continue
        by_ms[ms] = {
            "timestamp": ms,
            "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"],
            "volume": b.get("v") or 0,
        }
    return [by_ms[k] for k in sorted(by_ms)]


# ------------------------------------------------------------------ aggregate

def aggregate(bars_1m: list[dict], minutes: int) -> list[dict]:
    """1-minute bars -> `minutes`-minute bars, buckets anchored at 09:30 ET.

    A trailing partial bucket (fewer than `minutes` bars) is included as-is,
    not dropped -- the live session's most recent partial candle must still
    render. Premarket bars (before 09:30) bucket backward from the same
    anchor via floor division, which is well-defined for negative offsets.
    """
    if minutes <= 1 or not bars_1m:
        return list(bars_1m)
    out: list[dict] = []
    bucket: list[dict] = []
    bucket_start = None
    session_open = None

    def _flush():
        if bucket:
            out.append({
                "t": bucket_start,
                "o": bucket[0]["o"], "h": max(x["h"] for x in bucket),
                "l": min(x["l"] for x in bucket), "c": bucket[-1]["c"],
                "v": sum(x.get("v") or 0 for x in bucket),
            })

    for b in bars_1m:
        t_naive = _naive(b.get("t"))
        if session_open is None:
            session_open = t_naive.replace(hour=9, minute=30, second=0, microsecond=0)
        elapsed = int((t_naive - session_open).total_seconds() // 60)
        idx = elapsed // minutes  # floor division anchors premarket buckets too
        this_start = session_open + timedelta(minutes=idx * minutes)
        if bucket_start is None:
            bucket_start = this_start
        elif this_start != bucket_start:
            _flush()
            bucket = []
            bucket_start = this_start
        bucket.append(b)
    _flush()
    return out


# --------------------------------------------------------------- series (R-0)

def series_morning_vwap(bars_rth: list[dict], bars_pm: list[dict] | None) -> dict:
    """{"vwap": {ts_ms: value}, "pm_vwap": {ts_ms: value}} via
    indicators.vwap_series -- the morning lane's own module."""
    out: dict = {"vwap": {}, "pm_vwap": {}}
    if bars_rth:
        for b, v in zip(bars_rth, ind.vwap_series(bars_rth)):
            ms = _ms(b.get("t"))
            if ms is not None:
                out["vwap"][ms] = _round(v)
    if bars_pm:
        for b, v in zip(bars_pm, ind.vwap_series(bars_pm)):
            ms = _ms(b.get("t"))
            if ms is not None:
                out["pm_vwap"][ms] = _round(v)
    return out


def series_morning_ema9(bars_5m: list[dict]) -> dict:
    """9 EMA on 5-minute closes via indicators.ema_series (SMA-seeded) --
    matching the morning lane's own grading timeframe. Never runner_indicators."""
    out: dict = {}
    closes = [b["c"] for b in bars_5m]
    for b, v in zip(bars_5m, ind.ema_series(closes, 9)):
        if v is None:
            continue
        ms = _ms(b.get("t"))
        if ms is not None:
            out[ms] = _round(v)
    return out


def series_runner(bars_scan_basis: list[dict]) -> dict:
    """{"vwap": {ts_ms: value}, "ema9": {ts_ms: value}}, computed bar-by-bar
    with runner_indicators (ri) over the SAME bar source runner_scan.scan()
    feeds it (native Alpaca 5Min, per Phase 0 §0.3) -- never agent.indicators."""
    out: dict = {"vwap": {}, "ema9": {}}
    for i, b in enumerate(bars_scan_basis):
        ms = _ms(b.get("t"))
        if ms is None:
            continue
        prefix = bars_scan_basis[: i + 1]
        vw = ri.vwap(prefix)
        if vw is not None:
            out["vwap"][ms] = _round(vw)
        e9 = ri.ema(prefix, 9)
        if e9 is not None:
            out["ema9"][ms] = _round(e9)
    return out


# ------------------------------------------------------------- resolve_contexts

def resolve_contexts(con, ticker: str, d: date) -> list[dict]:
    """Every lane context that exists for `ticker` on `d`.

    Runner items are listed in ticker order only (there is at most one per
    ticker/date so this is trivially satisfied) -- nothing here ranks (R-1).
    """
    t = ticker.upper()
    out: list[dict] = []
    for p in ledger.picks_for_date(con, d):
        if str(p.get("ticker", "")).upper() == t and p.get("setup_type") != "dto_runner":
            out.append({"lane": "morning", "id": p["pick_id"],
                       "label": f"{p.get('setup_type') or 'pick'} {p.get('direction') or ''}".strip()})
    for c in ledger.runner_candidates_for_date(con, d):
        if str(c.get("ticker", "")).upper() == t:
            out.append({"lane": "runner", "id": f"{t}:{d.isoformat()}",
                       "label": "runner candidate"})
    for r in ledger.rejections_for_date(con, d):
        if str(r.get("ticker", "")).upper() == t:
            out.append({"lane": "shadow", "id": r["rejection_id"],
                       "label": f"rejected {r.get('setup_type') or ''}".strip()})
    for o in ledger.live_orders_for_date(con, d):
        if str(o.get("ticker", "")).upper() == t:
            out.append({"lane": "live", "id": o["live_order_id"],
                       "label": f"live {o.get('side') or ''}".strip()})
    return out


# ---------------------------------------------------------------------- levels

def levels_morning(pick: dict, outcome: dict | None, cfg: Config) -> tuple[list[dict], list[str]]:
    levels: list[dict] = []
    warnings: list[str] = []
    direction = pick.get("direction") or "long"
    entry, stop, target = pick.get("entry_price"), pick.get("stop_price"), pick.get("target_price")
    entry_type = pick.get("entry_type") or "open"
    if entry is not None:
        levels.append({"kind": "entry", "price": entry, "style": "solid",
                       "label": f"Entry {entry_type} @ {entry:.2f}"})
    if stop is not None:
        levels.append({"kind": "stop", "price": stop, "style": "solid", "label": f"Stop {stop:.2f}"})
    if target is not None:
        levels.append({"kind": "target", "price": target, "style": "solid", "label": f"Target {target:.2f}"})
    t2 = pick.get("target2_price")
    if t2 is not None:
        levels.append({"kind": "target2", "price": t2, "style": "dashed", "label": f"Target2 {t2:.2f}"})
    overlay = pick.get("overlay") or {}
    for key, label in (("pdh", "PDH"), ("pdl", "PDL"), ("pm_high", "PMH"), ("pm_low", "PML")):
        v = overlay.get(key)
        if v is not None:
            levels.append({"kind": "structural", "price": v, "style": "dashed-fine",
                           "label": f"{label} {v:.2f}"})
    stop_dist_atr = (outcome or {}).get("stop_dist_atr")
    if entry is not None and stop is not None and stop_dist_atr:
        atr = abs(entry - stop) / stop_dist_atr
        band = float(cfg.get("min_stop_atr", 0.35)) * atr
        levels.append({"kind": "noise_band", "low": entry - band, "high": entry + band,
                       "label": "Noise band"})
    elif entry is not None and stop is not None:
        warnings.append("ATR band unavailable (stop_dist_atr null)")
    return levels, warnings


def levels_runner(candidate: dict) -> list[dict]:
    levels = []
    det = candidate.get("detection_price")
    stop = candidate.get("stop_price")
    target = candidate.get("target_price")
    if det is not None:
        levels.append({"kind": "entry", "price": det, "style": "solid", "label": f"Detection {det:.2f}"})
    if stop is not None:
        levels.append({"kind": "stop", "price": stop, "style": "solid",
                       "label": f"Stop (9 EMA) {stop:.2f}"})
    if target is not None:
        levels.append({"kind": "target", "price": target, "style": "solid", "label": f"Target {target:.2f}"})
    return levels


def levels_live(pick_ctx: dict, broker_levels: list[dict]) -> list[dict]:
    levels, _ = levels_morning(pick_ctx, None, Config(raw={}))
    return levels + broker_levels


# --------------------------------------------------------------------- markers

def markers_from_replay(sim: dict, direction: str) -> list[dict]:
    """Fill/exit markers from a `simulate_pick` result. `sim` already carries
    real ET `fill_time`/`exit_time` datetimes (agent/grading.py) -- no bar
    index bookkeeping needed here."""
    out: list[dict] = []
    if sim.get("triggered"):
        ms = _ms(sim.get("fill_time"))
        if ms is not None and sim.get("entry_fill") is not None:
            out.append({"kind": "fill", "timestamp": ms, "price": sim["entry_fill"],
                       "label": f"Fill {sim['entry_fill']:.2f}", "source": "replay"})
        ems = _ms(sim.get("exit_time"))
        if ems is not None and sim.get("exit_price") is not None:
            out.append({"kind": "exit", "timestamp": ems, "price": sim["exit_price"],
                       "label": f"Exit ({sim.get('exit_reason') or ''}) {sim['exit_price']:.2f}",
                       "reason": sim.get("exit_reason"), "source": "replay"})
    return out


# --------------------------------------------------------------------- windows

def windows_premarket() -> dict:
    return {"kind": "premarket"}  # start/end filled by build() once the date is known


def parse_time_stop(raw: str | None) -> dtime | None:
    import re
    if not raw:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})", str(raw))
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    return dtime(hh, mm)


# ------------------------------------------------------------- replay helpers
# Each of these mirrors the EXACT call shape agent/loops/evening.py uses for
# that lane (Phase 0 §0.2), reusing the same shared context builder
# (agent.grading_context.build_grading_context) rather than hand-assembling
# kwargs -- a replay with different slippage/flatten settings is a different
# trade, and this is how that is avoided without touching the evening loop's
# own production grading path for a read-only chart feature.

def _replay_morning(md: MarketData, con, cfg: Config, pick: dict, d: date,
                    bars: list[dict], daily: dict | None) -> dict:
    gctx = build_grading_context(md, con, d, [pick["ticker"]], cfg, tag="chart")
    t = pick["ticker"].upper()
    slippage = float(cfg.get("slippage_bps", 0) or 0)
    flatten_at = cfg.get("flatten_at")
    min_bar_volume = float(cfg.get("min_bar_volume", 0) or 0)
    # The runner entry time and the runner fill rule apply to RUNNER picks only,
    # exactly as grading.grade_day applies them: a morning pick on a name the
    # runner also detected that day is still graded from the open.
    runner_pick = pick.get("setup_type") == ledger.RUNNER_SETUP
    entry_after = (gctx["entry_after_map"] or {}).get(t) if runner_pick else None
    sim_bars = bars
    if entry_after is not None and bars:
        sim_bars = [b for b in bars if _naive(b["t"]) >= _naive(entry_after)]
        if not sim_bars:
            return {"triggered": False, "exit_reason": "no_data",
                    "sim_version": CURRENT_SIM_VERSION}
    return simulate_pick(pick, sim_bars, daily, slippage_bps=slippage, flatten_at=flatten_at,
                         min_bar_volume=min_bar_volume, adv20=gctx["adv20_map"].get(t),
                         atr14=gctx["atr_map"].get(t), trend_snapshot=gctx["trend_map"].get(t),
                         fundamentals=gctx["fundamentals_map"].get(t),
                         prev_close=gctx["prev_close_map"].get(t),
                         or_minutes=gctx["or_minutes"], session_date=d, session_bars=bars,
                         min_reward_r=RUNNER_MIN_REWARD_R if runner_pick else 0.0)


def _replay_shadow(cfg: Config, rejection: dict, d: date, bars: list[dict]) -> dict:
    shim = {"pick_id": rejection["rejection_id"], "direction": rejection["direction"],
           "entry_type": rejection["entry_type"], "entry_price": rejection["entry_price"],
           "stop_price": rejection["stop_price"], "target_price": rejection["target_price"]}
    slippage = float(cfg.get("slippage_bps", 0) or 0)
    flatten_at = cfg.get("flatten_at")
    min_bar_volume = float(cfg.get("min_bar_volume", 0) or 0)
    return simulate_pick(shim, bars, None, slippage_bps=slippage, flatten_at=flatten_at,
                         min_bar_volume=min_bar_volume, session_date=d)


def _replay_runner(md: MarketData, con, cfg: Config, candidate: dict, d: date,
                   bars: list[dict]) -> tuple[dict, datetime | None]:
    session = ledger.runner_session(con, d) or {}
    fp = candidate.get("first_pass_et")
    cutoff = honest_entry_time(fp, candidate.get("scan_pass_et"), session.get("selection_at")) if fp else None
    entry_bars = bars
    if cutoff is not None and bars:
        entry_bars = [b for b in bars if _naive(b["t"]) >= _naive(cutoff)]
    if not entry_bars:
        return {"triggered": False, "exit_reason": "no_data", "sim_version": CURRENT_SIM_VERSION}, cutoff
    ctx = {}
    try:
        ctx = md.daily_context([candidate["ticker"]])
    except Exception as e:
        log("chart", f"runner replay: daily_context failed for {candidate['ticker']}: {e}")
    t = candidate["ticker"].upper()
    atr14 = (ctx.get(t) or {}).get("atr14")
    trend_snapshot = (ctx.get(t) or {}).get("trend_snapshot")
    slippage = float(cfg.get("slippage_bps", 0) or 0)
    flatten_at = cfg.get("flatten_at")
    min_bar_volume = float(cfg.get("min_bar_volume", 0) or 0)
    shim = {"pick_id": 0, "direction": "long", "entry_type": "open",
           "entry_price": candidate.get("detection_price"), "stop_price": candidate.get("stop_price"),
           "target_price": candidate.get("target_price")}
    sim = simulate_pick(shim, entry_bars, None, slippage_bps=slippage, flatten_at=flatten_at,
                        min_bar_volume=min_bar_volume, atr14=atr14, trend_snapshot=trend_snapshot,
                        session_date=d, min_reward_r=RUNNER_MIN_REWARD_R)
    return sim, cutoff


# ---------------------------------------------------------------------- guard

def _replay_guard(name: str, stored: float | None, replayed: float | None,
                  stored_sim_version: int | None, graded: bool = True,
                  lane: str | None = None) -> dict:
    # An ungraded row (evening loop hasn't run for it yet) has no ledger value
    # to disagree with -- that's "nothing to compare", not a mismatch, and
    # flagging it put a red banner on every same-day chart.
    if not graded:
        return {"name": name, "ok": True, "detail": "not graded yet -- replay only"}
    # A version bump that changed nothing for this lane (sim v6 is runner-only)
    # is not a disagreement: grading.same_semantics knows which versions grade
    # a lane identically.
    if stored_sim_version is not None and not same_semantics(stored_sim_version, lane):
        return {"name": name, "ok": False,
               "detail": f"graded under sim v{stored_sim_version}, replayed under v{CURRENT_SIM_VERSION}"}
    if stored is None and replayed is None:
        return {"name": name, "ok": True, "detail": "no stored outcome to compare"}
    if stored is None or replayed is None or abs(float(stored) - float(replayed)) > _REPLAY_TOL:
        return {"name": name, "ok": False, "detail": f"stored {stored}, replay {replayed}"}
    return {"name": name, "ok": True,
           "detail": f"stored {stored:+.2f}R, replay {replayed:+.2f}R, sim v{CURRENT_SIM_VERSION}"}


# -------------------------------------------------------------------- premarket

def _premarket_bars(md: MarketData, ticker: str, d: date) -> list[dict]:
    """Reuses the SAME premarket-minute-bars fetch shape as
    providers.py::_enrich_indicators (04:00-09:30 ET, 1Min) -- no new Alpaca
    call type, zero additional network surface."""
    if md.cfg.offline or not md.alpaca:
        return []
    from datetime import time as _t
    start = datetime.combine(d, _t(4, 0), tzinfo=ET)
    end = datetime.combine(d, _t(9, 30), tzinfo=ET)
    try:
        got = md.alpaca.multi_bars([ticker], "1Min", start, end)
        return got.get(ticker.upper()) or []
    except Exception as e:
        log("chart", f"premarket bars fetch failed for {ticker}: {e}")
        return []


def _runner_scan_bars(md: MarketData, ticker: str, d: date, end: datetime) -> list[dict]:
    """Native Alpaca 5Min bars, session open through `end` -- the exact
    source runner_scan.scan() feeds `ri` (Phase 0 §0.3), never 1Min
    aggregated."""
    if md.cfg.offline or not md.alpaca:
        from .providers import synthetic_minute_bars
        return aggregate(synthetic_minute_bars(ticker, d), 5)
    from datetime import time as _t
    start = datetime.combine(d, _t(9, 30), tzinfo=ET)
    try:
        got = md.alpaca.multi_bars([ticker], "5Min", start, end)
        return got.get(ticker.upper()) or []
    except Exception as e:
        log("chart", f"runner scan-basis bars fetch failed for {ticker}: {e}")
        return []


# ----------------------------------------------------------------------- build

def build(con, cfg: Config, ticker: str, d: date, lane: str | None, ctx_id: Any,
         tf: int = 5, asof: bool = False, since: int | None = None) -> dict:
    """The chart payload orchestrator. Never raises for a data problem -- any
    exception is caught and reported through `warnings`, with whatever
    partial payload was built (R-4/R-5: the drawer never blanks on a
    transient failure). Only bad input (caller's job) is a 400, and that is
    the endpoint's responsibility, not this function's."""
    t = ticker.upper()
    warnings: list[str] = []
    guards: list[dict] = []
    md = MarketData(cfg)

    contexts = resolve_contexts(con, t, d)
    if lane is None and contexts:
        lane = contexts[0]["lane"]
        ctx_id = contexts[0]["id"]

    try:
        bars = md.minute_bars(t, d)
    except Exception as e:
        warnings.append(f"minute bars unavailable: {e}")
        bars = []

    try:
        daily = md.daily_bar(t, d)
    except Exception:
        daily = None

    # Alpaca._realtime_refused is a process-wide, class-level flag flipped the
    # first time the SIP subscription refuses a recent window (providers.py
    # _clamp_end) -- reading it here tells the drawer honestly whether "the
    # last bar" might be stale by design, not a bug.
    from .providers import Alpaca as _Alpaca
    clamped = bool(getattr(_Alpaca, "_realtime_refused", False))
    feed = getattr(getattr(md, "alpaca", None), "feed", None) or "yfinance"
    data_through_et = bars[-1]["t"].isoformat() if bars else None

    out: dict = {
        "ticker": t, "date": d.isoformat(), "tf": tf, "asof": bool(asof),
        "feed": feed, "data_through_et": data_through_et, "clamped": clamped,
        "bars": [], "premarket_bars": [],
        "contexts": contexts, "lane": lane, "id": ctx_id,
        "series": {}, "levels": [], "markers": [], "windows": [],
        "context": {}, "guards": guards, "warnings": warnings,
    }
    if not lane:
        out["bars"] = to_kline(aggregate(bars, tf))
        out["summary"] = trade_summary(out)
        return out

    try:
        if lane == "morning":
            _build_morning(con, cfg, md, out, t, d, ctx_id, bars, daily, tf, asof, warnings, guards)
        elif lane == "shadow":
            _build_shadow(con, cfg, md, out, t, d, ctx_id, bars, tf, asof, warnings, guards)
        elif lane == "runner":
            _build_runner(con, cfg, md, out, t, d, bars, tf, asof, warnings, guards)
        elif lane == "live":
            _build_live(con, cfg, md, out, t, d, ctx_id, bars, tf, warnings, guards)
        else:
            warnings.append(f"unknown lane {lane!r}")
            out["bars"] = to_kline(aggregate(bars, tf))
    except Exception as e:  # noqa: BLE001 -- never blank the drawer on a data problem
        log("chart", f"build() failed for {t}/{d}/{lane}: {e}")
        warnings.append(f"chart build error: {e}")
        if not out["bars"]:
            out["bars"] = to_kline(aggregate(bars, tf))
    out["summary"] = trade_summary(out)
    return out


# --------------------------------------------------------------------- summary

_EXIT_LABELS = {"target": "Target hit", "stop": "Stopped out", "eod": "Flat at close",
                "time_stop": "Time stop", "no_trigger": "Not triggered", "no_data": "No data"}
_SHADOW_LABELS = {"would_have_won": "Would have won", "would_have_lost": "Would have lost",
                  "scratch": "Scratch", "unmeasurable": "Unmeasurable"}


def _humanize(raw, table: dict) -> str | None:
    if raw is None or raw == "":
        return None
    return table.get(str(raw), str(raw).replace("_", " ").capitalize())


def _first_level(levels: list[dict], kind: str) -> float | None:
    """Price of the first `kind` level; a broker leg (live lane) outranks the
    plan's number because it is what is actually resting at the broker."""
    hits = [lv for lv in levels if lv.get("kind") == kind and lv.get("price") is not None]
    broker = [lv for lv in hits if lv.get("source") == "broker"]
    pick = (broker or hits or [None])[0]
    return float(pick["price"]) if pick else None


def _first_marker(markers: list[dict], kind: str) -> dict | None:
    m = next((m for m in markers if m.get("kind") == kind and m.get("price") is not None
              and m.get("timestamp") is not None), None)
    if m is None:
        return None
    out = {"price": float(m["price"]), "timestamp": m["timestamp"]}
    if kind == "exit":
        out["reason"] = _humanize(m.get("reason"), _EXIT_LABELS)
    return out


def trade_summary(out: dict) -> dict:
    """The drawer's headline numbers (entry/stop/target, risk, reward R, fill,
    exit, result), derived here from what the lane builder already put in
    `out` so the browser only formats them and never computes one (R-0).
    Runs after as-of filtering, so a hidden outcome stays hidden."""
    levels = out.get("levels") or []
    markers = out.get("markers") or []
    ctx = out.get("context") or {}
    live = ctx.get("live") or None
    outcome = ctx.get("outcome")

    direction = ctx.get("direction")
    if direction is None and live:
        direction = {"buy": "long", "sell": "short"}.get(str(live.get("side") or "").lower())

    entry, stop, target = (_first_level(levels, k) for k in ("entry", "stop", "target"))
    risk = abs(entry - stop) if entry is not None and stop is not None else None
    risk_pct = round(risk / entry * 100, 2) if risk is not None and entry else None
    reward_r = (round(abs(target - entry) / risk, 2)
                if risk and target is not None and entry is not None else None)

    result, status = None, None
    if live and live.get("open"):
        result = {"label": "Unrealized", "r": live.get("unrealized_r")}
        status = "Position open"
    elif outcome:
        if "forgone_r" in outcome:
            if outcome.get("eod_outcome") is not None:  # None = evening loop hasn't graded it
                result = {"label": "Forgone", "r": outcome.get("forgone_r")}
                status = _humanize(outcome.get("eod_outcome"), _SHADOW_LABELS)
        else:
            # Planned-risk R, the one realized-R measure (grading.realized_r);
            # r_multiple only for a legacy row that has nothing else.
            result = {"label": "Result", "r": realized_r(outcome)}
            if outcome.get("triggered") is False:
                why = _humanize(outcome.get("untriggered_reason"), {})
                status = "Not triggered" + (f" ({why.lower()})" if why else "")
            else:
                status = _humanize(outcome.get("exit_reason"), _EXIT_LABELS)
    elif out.get("asof"):
        status = "Outcome hidden (as-of)"
    if status is None and out.get("lane"):
        status = "Not graded yet"

    return {
        "direction": direction,
        "entry_label": "Detection" if out.get("lane") == "runner" else "Entry",
        "entry": entry, "stop": stop, "target": target,
        "risk_per_share": round(risk, 4) if risk is not None else None,
        "risk_pct": risk_pct, "reward_r": reward_r,
        "fill": _first_marker(markers, "fill"), "exit": _first_marker(markers, "exit"),
        "result": result, "status": status,
    }


def _build_morning(con, cfg, md, out, t, d, pick_id, bars, daily, tf, asof, warnings, guards):
    pick = next((p for p in ledger.picks_for_date(con, d)
                if p["pick_id"] == pick_id and str(p["ticker"]).upper() == t), None)
    if pick is None:
        warnings.append(f"pick {pick_id} not found for {t}/{d}")
        out["bars"] = to_kline(aggregate(bars, tf))
        return
    outcome = con.execute("SELECT * FROM outcomes WHERE pick_id = ?", [pick_id]).fetchone()
    outcome_cols = [c[0] for c in con.execute("DESCRIBE outcomes").fetchall()]
    outcome_row = dict(zip(outcome_cols, outcome)) if outcome else None

    pm_bars = _premarket_bars(md, t, d)
    vwap_series = series_morning_vwap(bars, pm_bars)
    ema9 = series_morning_ema9(aggregate(bars, 5))
    out["series"] = {
        "vwap": {"basis": "indicators.vwap_series RTH", "bar_minutes": 1, "points": vwap_series["vwap"]},
        "pm_vwap": {"basis": "indicators.vwap_series premarket 04:00-09:30 ET", "bar_minutes": 1,
                    "points": vwap_series["pm_vwap"]},
        "ema9": {"basis": "indicators.ema_series 5m (SMA seed)", "bar_minutes": 5, "points": ema9},
    }
    levels, level_warnings = levels_morning(pick, outcome_row, cfg)
    out["levels"] = levels
    warnings.extend(level_warnings)

    windows = [{"kind": "premarket", "start": _ms(datetime.combine(d, dtime(4, 0), tzinfo=ET)),
               "end": _ms(datetime.combine(d, dtime(9, 30), tzinfo=ET))}]
    ts = parse_time_stop(pick.get("time_stop"))
    if ts is not None:
        windows.append({"kind": "time_stop", "at": _ms(datetime.combine(d, ts, tzinfo=ET))})
    elif pick.get("time_stop"):
        warnings.append(f"time_stop {pick['time_stop']!r} did not parse as HH:MM")
    out["windows"] = windows

    sim = _replay_morning(md, con, cfg, pick, d, bars, daily)
    markers = markers_from_replay(sim, pick.get("direction") or "long")
    if not sim.get("triggered"):
        untriggered = sim.get("untriggered_reason") or (outcome_row or {}).get("untriggered_reason")
        markers.append({"kind": "no_trigger", "timestamp": _ms(bars[0]["t"]) if bars else None,
                        "label": f"No trigger ({untriggered or 'unknown'})", "source": "replay"})
    out["markers"] = markers

    stored_r = realized_r(outcome_row or {})
    stored_sim = (outcome_row or {}).get("sim_version")
    guards.append(_replay_guard("replay_vs_ledger", stored_r, realized_r(sim), stored_sim,
                                graded=outcome_row is not None,
                                lane=("runner" if pick.get("setup_type") == ledger.RUNNER_SETUP
                                      else "morning")))
    if not guards[-1]["ok"]:
        for m in markers:
            if m.get("source") == "replay":
                m["source"] = "replay — disagrees with ledger"

    out["context"] = {
        "setup_type": pick.get("setup_type"), "direction": pick.get("direction"),
        "conviction": pick.get("conviction"), "size_r": pick.get("size_r"),
        "catalyst": pick.get("catalyst"), "thesis": pick.get("thesis"),
        "invalidation": pick.get("invalidation"),
        "outcome": None if asof else ({
            "triggered": (outcome_row or {}).get("triggered"),
            "exit_reason": (outcome_row or {}).get("exit_reason"),
            "r_planned": (outcome_row or {}).get("r_planned"),
            "r_multiple": (outcome_row or {}).get("r_multiple"),
            "mfe_r": (outcome_row or {}).get("mfe_r"),
            "mae_r": (outcome_row or {}).get("mae_r"), "sim_version": stored_sim,
            "untriggered_reason": (outcome_row or {}).get("untriggered_reason"),
        } if outcome_row else None),
    }

    cutoff = _plan_created_at(con, d)
    out["bars"] = to_kline(aggregate(_apply_cutoff_bars(bars, cutoff) if asof else bars, tf))
    out["premarket_bars"] = to_kline(_apply_cutoff_bars(pm_bars, cutoff) if asof else pm_bars)
    if asof:
        _apply_asof(out, cutoff)


def _build_shadow(con, cfg, md, out, t, d, rejection_id, bars, tf, asof, warnings, guards):
    rejection = next((r for r in ledger.rejections_for_date(con, d)
                      if r["rejection_id"] == rejection_id and str(r["ticker"]).upper() == t), None)
    if rejection is None:
        warnings.append(f"rejection {rejection_id} not found for {t}/{d}")
        out["bars"] = to_kline(aggregate(bars, tf))
        return

    pm_bars = _premarket_bars(md, t, d)
    vwap_series = series_morning_vwap(bars, pm_bars)
    ema9 = series_morning_ema9(aggregate(bars, 5))
    out["series"] = {
        "vwap": {"basis": "indicators.vwap_series RTH", "bar_minutes": 1, "points": vwap_series["vwap"]},
        "pm_vwap": {"basis": "indicators.vwap_series premarket 04:00-09:30 ET", "bar_minutes": 1,
                    "points": vwap_series["pm_vwap"]},
        "ema9": {"basis": "indicators.ema_series 5m (SMA seed)", "bar_minutes": 5, "points": ema9},
    }
    pick_shaped = {"direction": rejection.get("direction"), "entry_price": rejection.get("entry_price"),
                  "stop_price": rejection.get("stop_price"), "target_price": rejection.get("target_price"),
                  "entry_type": rejection.get("entry_type"), "time_stop": None, "overlay": {}}
    levels, level_warnings = levels_morning(pick_shaped, None, cfg)
    out["levels"] = levels
    warnings.extend(level_warnings)
    out["windows"] = [{"kind": "premarket", "start": _ms(datetime.combine(d, dtime(4, 0), tzinfo=ET)),
                       "end": _ms(datetime.combine(d, dtime(9, 30), tzinfo=ET))}]

    sim = _replay_shadow(cfg, rejection, d, bars)
    out["markers"] = markers_from_replay(sim, rejection.get("direction") or "long")

    eod_outcome, forgone_replay = classify_shadow(sim)
    stored_forgone = rejection.get("forgone_r")
    guards.append(_replay_guard("replay_vs_ledger", stored_forgone, forgone_replay, None,
                                graded=rejection.get("eod_outcome") is not None))
    if not guards[-1]["ok"]:
        for m in out["markers"]:
            if m.get("source") == "replay":
                m["source"] = "replay — disagrees with ledger"

    out["context"] = {
        "setup_type": rejection.get("setup_type"), "direction": rejection.get("direction"),
        "conviction": rejection.get("conviction"), "thesis": rejection.get("thesis"),
        "kill_reason": rejection.get("reason"), "counterfactual": True,
        "outcome": None if asof else {
            "eod_outcome": rejection.get("eod_outcome"), "forgone_r": stored_forgone,
        },
    }
    cutoff = _plan_created_at(con, d)
    out["bars"] = to_kline(aggregate(_apply_cutoff_bars(bars, cutoff) if asof else bars, tf))
    out["premarket_bars"] = to_kline(_apply_cutoff_bars(pm_bars, cutoff) if asof else pm_bars)
    if asof:
        _apply_asof(out, cutoff)


def _build_runner(con, cfg, md, out, t, d, bars, tf, asof, warnings, guards):
    candidate = next((c for c in ledger.runner_candidates_for_date(con, d)
                      if str(c["ticker"]).upper() == t), None)
    if candidate is None:
        warnings.append(f"no runner candidate for {t}/{d}")
        out["bars"] = to_kline(aggregate(bars, tf))
        return

    close_dt = datetime.combine(d, market_close_et(d), tzinfo=ET)
    scan_bars = _runner_scan_bars(md, t, d, close_dt)
    rseries = series_runner(scan_bars)
    out["series"] = {
        "vwap": {"basis": "runner_indicators.vwap (native 5Min, DTO parity)", "bar_minutes": 5,
                 "points": rseries["vwap"]},
        "ema9": {"basis": "runner_indicators.ema period=9 (native 5Min, DTO parity)", "bar_minutes": 5,
                 "points": rseries["ema9"]},
    }
    out["levels"] = levels_runner(candidate)

    windows = []
    fp = candidate.get("first_pass_et")
    if fp is not None:
        windows.append({"kind": "detection_bar", "start": _ms(fp),
                        "end": _ms(earliest_entry_time(fp))})

    outcomes = {o["ticker"]: o for o in ledger.runner_outcomes_for_date(con, d)}
    stored = outcomes.get(t)
    sim, cutoff = _replay_runner(md, con, cfg, candidate, d, bars)
    markers = []
    if fp is not None:
        markers.append({"kind": "detection", "timestamp": _ms(fp), "label": "Detected", "source": "ledger"})
        markers.append({"kind": "detection_close", "timestamp": _ms(earliest_entry_time(fp)),
                        "label": "Detection bar close", "source": "ledger"})
    if cutoff is not None:
        markers.append({"kind": "honest_entry", "timestamp": _ms(cutoff),
                        "label": "Earliest honest fill", "source": "ledger"})
        windows.append({"kind": "honest_entry", "at": _ms(cutoff)})
    markers.extend(markers_from_replay(sim, "long"))
    out["markers"] = markers
    out["windows"] = windows

    stored_r = realized_r(stored or {})
    stored_sim = (stored or {}).get("sim_version")
    guards.append(_replay_guard("replay_vs_ledger", stored_r, realized_r(sim), stored_sim,
                                graded=stored is not None, lane="runner"))
    if not guards[-1]["ok"]:
        for m in out["markers"]:
            if m.get("source") == "replay":
                m["source"] = "replay — disagrees with ledger"

    out["context"] = {
        "setup_type": "dto_runner", "direction": "long",
        "selection_rank": (stored or {}).get("selection_rank"),
        "outcome": None if asof else ({
            "triggered": (stored or {}).get("triggered"), "exit_reason": (stored or {}).get("exit_reason"),
            "r_planned": (stored or {}).get("r_planned"),
            "r_multiple": (stored or {}).get("r_multiple"),
            "invalid_reason": (stored or {}).get("invalid_reason"),
            "mfe_r": (stored or {}).get("mfe_r"),
            "mae_r": (stored or {}).get("mae_r"), "sim_version": stored_sim,
        } if stored else None),
    }

    display_cutoff = cutoff if asof else None
    out["bars"] = to_kline(aggregate(_apply_cutoff_bars(bars, display_cutoff) if asof else bars, tf))
    if asof:
        _apply_asof(out, display_cutoff)


def _build_live(con, cfg, md, out, t, d, order_id, bars, tf, warnings, guards):
    from . import live_broker
    order_row = next((o for o in ledger.live_orders_for_date(con, d)
                      if o["live_order_id"] == order_id and str(o["ticker"]).upper() == t), None)
    if order_row is None:
        warnings.append(f"live order {order_id} not found for {t}/{d}")
        out["bars"] = to_kline(aggregate(bars, tf))
        return

    pm_bars = _premarket_bars(md, t, d)
    vwap_series = series_morning_vwap(bars, pm_bars)
    ema9 = series_morning_ema9(aggregate(bars, 5))
    out["series"] = {
        "vwap": {"basis": "indicators.vwap_series RTH", "bar_minutes": 1, "points": vwap_series["vwap"]},
        "pm_vwap": {"basis": "indicators.vwap_series premarket 04:00-09:30 ET", "bar_minutes": 1,
                    "points": vwap_series["pm_vwap"]},
        "ema9": {"basis": "indicators.ema_series 5m (SMA seed)", "bar_minutes": 5, "points": ema9},
    }

    pick_row = None
    if order_row.get("pick_id"):
        pick_row = next((p for p in ledger.picks_for_date(con, d)
                         if p["pick_id"] == order_row["pick_id"]), None)
    broker_levels: list[dict] = []
    open_position = order_row.get("exit_reason") is None
    if open_position:
        try:
            broker_order = live_broker.get_order(order_row["live_order_id"], cfg=cfg)
        except Exception as e:
            broker_order = None
            warnings.append(f"broker order lookup failed: {e}")
        if broker_order:
            for leg in broker_order.get("legs") or []:
                if leg.get("stop_price") is not None:
                    broker_levels.append({"kind": "stop", "price": float(leg["stop_price"]),
                                          "style": "solid", "label": f"Stop {float(leg['stop_price']):.2f}",
                                          "source": "broker", "status": leg.get("status")})
                if leg.get("limit_price") is not None:
                    broker_levels.append({"kind": "target", "price": float(leg["limit_price"]),
                                          "style": "solid", "label": f"Target {float(leg['limit_price']):.2f}",
                                          "source": "broker", "status": leg.get("status")})
    out["levels"] = levels_live(pick_row or {"direction": order_row.get("side") == "buy" and "long" or "short",
                                             "entry_price": order_row.get("entry_fill_price"),
                                             "stop_price": None, "target_price": None,
                                             "entry_type": order_row.get("entry_order_type"), "overlay": {}},
                                broker_levels)

    markers = []
    if order_row.get("entry_fill_at"):
        markers.append({"kind": "fill", "timestamp": _ms(order_row["entry_fill_at"]),
                        "price": order_row.get("entry_fill_price"),
                        "label": f"Fill {order_row.get('entry_fill_price')}", "source": "broker"})
    if order_row.get("exit_fill_at"):
        markers.append({"kind": "exit", "timestamp": _ms(order_row["exit_fill_at"]),
                        "price": order_row.get("exit_fill_price"),
                        "label": f"Exit ({order_row.get('exit_reason') or ''}) {order_row.get('exit_fill_price')}",
                        "reason": order_row.get("exit_reason"), "source": "broker"})
    out["markers"] = markers
    out["windows"] = [{"kind": "premarket", "start": _ms(datetime.combine(d, dtime(4, 0), tzinfo=ET)),
                       "end": _ms(datetime.combine(d, dtime(9, 30), tzinfo=ET))}]

    unrealized_r = None
    if open_position and pick_row and order_row.get("entry_fill_price"):
        from .live_monitor import _live_r
        last_price = (bars[-1]["c"] if bars else None)
        unrealized_r = _live_r(pick_row, order_row.get("entry_fill_price"), last_price)
    out["context"] = {
        "live": {"fill": order_row.get("entry_fill_price"), "fill_at": _iso(order_row.get("entry_fill_at")),
                "qty": order_row.get("qty"), "side": order_row.get("side"),
                "unrealized_r": unrealized_r, "status": order_row.get("status"),
                "open": open_position},
        "outcome": {
            "exit_reason": order_row.get("exit_reason"), "r_multiple": order_row.get("live_r"),
        } if not open_position else None,
    }
    out["bars"] = to_kline(aggregate(bars, tf))
    out["premarket_bars"] = to_kline(pm_bars)


# ------------------------------------------------------------------- as-of

def _plan_created_at(con, d: date) -> datetime | None:
    row = con.execute("SELECT created_at FROM daily_plans WHERE plan_date = ?", [d]).fetchone()
    return row[0] if row else None


def _apply_cutoff_bars(bars: list[dict], cutoff: datetime | None) -> list[dict]:
    if cutoff is None:
        return bars
    return [b for b in bars if _naive(b["t"]) < _naive(cutoff)]


def _apply_asof(out: dict, cutoff: datetime | None) -> None:
    """Drop every marker at/after the cutoff and null the outcome (already
    done by the per-lane builder). Bars/premarket_bars are already cut before
    `to_kline`; this only needs to filter markers, which carry epoch ms."""
    cutoff_ms = _ms(cutoff)
    out["context"]["outcome"] = None
    if cutoff_ms is None:
        out["markers"] = []
    else:
        out["markers"] = [m for m in out["markers"] if (m.get("timestamp") or 0) < cutoff_ms]
        out["windows"].append({"kind": "asof_cutoff", "at": cutoff_ms})


def _iso(dt) -> str | None:
    aware = _localize_et(dt)
    return aware.isoformat() if aware is not None else None
