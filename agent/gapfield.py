"""
The gap field: every liquid gapper, every session, traded both ways
(specs/EDGE_ACCELERATION_SPEC_v1.md Part 4).

WHY THIS MODULE EXISTS
----------------------
Evidence arrives at the rate the desk generates it. The morning desk takes
~20 hand-picked trades a session, so a morning cell gathers a forward
observation every other day and a modest edge takes months to decide. The
runner prices every candidate it detects, which is why runner rules decide in
about ten sessions -- but the runner is one setup.

The gap is the morning desk's raw material (gap_go, gap_fade, catalyst
gaps), and it can be priced mechanically for the WHOLE market from daily bars
alone: every liquid common stock that opens >= 3% from its prior close, traded
with the gap (momentum) and against it (fade), entry at the open, 1-ATR stop,
exit at the close -- the population monkeys' exact rules
(population.simulate_population_row), but over every gapper rather than the
morning scan's top thirty.

Because it needs only daily bars, the SAME code can price the past. A nightly
job fills in the last ~2 years (discovery data: tens of thousands of events
the moment it finishes) and prices each new session the night it closes
(forward data). History and forward rows come from one function, so a cell
discovered on history is tested forward on exactly the same definition.

INTEGRITY
---------
* History is DISCOVERY ONLY. Forward observations are recorded only from rows
  the nightly forward scan wrote for that session (source 'forward'), and only
  for sessions after a rule's quarantine (research.record_rule_observation's
  guard). Nothing here relaxes the forward-only guard.
* Evidence is counted in (session, industry) units, the runner's lesson
  ("count events, not rows"): fifteen biotech gappers on one morning are one
  observation per industry, not fifteen.
* The daily-bar stop is booked whenever the bar's range reaches it, whatever
  came first. That biases every arm DOWN (population.py's stated limit), so a
  positive cell is conservative, and a long arm and a short arm can both
  look worse than they are.
* Survivorship: the universe is today's listed common stocks. Names that
  delisted in the backfill window are absent, so history leans toward
  survivors. This is one more reason history may only nominate.
* A gap is measured on split-adjusted bars, so a split is not a gap.
"""
from __future__ import annotations

import json
import math
import time
from datetime import date, datetime, timedelta
from typing import Any, Iterable

__all__ = [
    "GAPFIELD_SIM_VERSION", "STRATEGY_DIM", "DIMS", "events_from_bars", "simulate",
    "scan_session", "ensure_backfill_job", "backfill_step", "refresh_cells", "comparisons",
    "forward_rows", "status", "weekly_md", "label", "report_text",
]

GAPFIELD_SIM_VERSION = 1
GAP_MIN_PCT = 3.0              # the morning gap scan's own threshold (data.gap_scan_min_pct)
PRICE_MIN = 5.0                # prior close; below it a daily-bar sim is mostly spread
ADV_MIN_USD = 10_000_000.0     # 20-day mean dollar volume before the gap day
LOOKBACK_DAYS = 300            # calendar days of bars before a session (SMA200 needs ~290)
BACKFILL_SESSIONS = 500        # ~2 years of history
CHUNK = 400                    # symbols per backfill fetch (memory and time bound)
FIRST_CHUNK_S = 45.0           # assumed duration of a chunk before one has been timed
                               # (measured: 13s locally for 400 symbols x 2 years; Neon
                               # round trips for ~12k rows add more on Vercel)
ARMS = ("momentum", "fade")
SPY = "SPY"
INSERT_BATCH = 200

STRATEGY_DIM = "gf_strategy"
DIMS: tuple[str, ...] = (
    "gf_gap_atr", "gf_gap_pct", "gf_price", "gf_liquidity", "gf_trend_200d",
    "gf_prior_day", "gf_vol_ratio", "gf_market_gap",
)

_COLS = ("session_date", "symbol", "arm", "direction", "gap_pct", "gap_atr", "atr14",
         "prev_close", "adv20_usd", "sma200", "prior_ret_pct", "vol_ratio", "spy_gap_pct",
         "industry", "day_open", "day_high", "day_low", "day_close", "entry_price",
         "stop_price", "exit_price", "exit_reason", "r_multiple", "sim_version", "source",
         "computed_at")


def _log(msg: str) -> None:
    try:
        from .util import log
        log("gapfield", msg)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# Labels (the lane's dimensions; agent/buckets.py routes the 'gapfield' lane here)
# --------------------------------------------------------------------------

def _cut(v: float | None, cuts: tuple[tuple[float, str], ...], top: str) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "?"
    for edge, lab in cuts:
        if v < edge:
            return lab
    return top


def label(dim: str, row: dict) -> str:
    """The bucket `row` falls in for `dim`. Pure; '?' when the input is missing."""
    def f(k: str) -> float | None:
        v = row.get(k)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    if dim == STRATEGY_DIM:
        arm, direction = row.get("arm"), row.get("direction")
        return f"{arm}_{direction}" if arm and direction else "?"
    if dim == "gf_gap_atr":
        return _cut(f("gap_atr"), ((1.0, "<1"), (2.0, "1-2"), (3.0, "2-3")), ">3")
    if dim == "gf_gap_pct":
        g = f("gap_pct")
        return _cut(abs(g) if g is not None else None,
                    ((5.0, "3-5"), (10.0, "5-10"), (20.0, "10-20")), ">20")
    if dim == "gf_price":
        return _cut(f("prev_close"), ((10.0, "<10"), (30.0, "10-30"), (100.0, "30-100")), ">100")
    if dim == "gf_liquidity":
        return _cut(f("adv20_usd"), ((25e6, "<25M"), (100e6, "25-100M"), (500e6, "100-500M")),
                    ">500M")
    if dim == "gf_trend_200d":
        pc, sma = f("prev_close"), f("sma200")
        if pc is None or sma is None:
            return "?"
        return "above" if pc >= sma else "below"
    if dim == "gf_prior_day":
        return _cut(f("prior_ret_pct"), ((-3.0, "<-3"), (3.0, "-3..3")), ">3")
    if dim == "gf_vol_ratio":
        return _cut(f("vol_ratio"), ((1.0, "<1"), (2.0, "1-2")), ">2")
    if dim == "gf_market_gap":
        return _cut(f("spy_gap_pct"), ((-0.25, "down"), (0.25, "flat")), "up")
    return "?"


# --------------------------------------------------------------------------
# Pure computation
# --------------------------------------------------------------------------

def _bar_date(b: dict) -> date:
    t = b.get("t")
    if isinstance(t, datetime):
        return t.date()
    if isinstance(t, date):
        return t
    return date.fromisoformat(str(t)[:10])


def spy_gaps(bars: list[dict]) -> dict[date, float]:
    """{session: SPY open gap %} from SPY's daily bars."""
    out: dict[date, float] = {}
    bars = sorted(bars or [], key=_bar_date)
    for prev, cur in zip(bars, bars[1:]):
        pc, o = prev.get("c"), cur.get("o")
        if pc and o:
            out[_bar_date(cur)] = (float(o) - float(pc)) / float(pc) * 100.0
    return out


def events_from_bars(symbol: str, bars: list[dict], start: date, end: date,
                     spy_gap: dict[date, float] | None = None,
                     industry: str | None = None) -> list[dict]:
    """Every qualifying gap in [start, end] for one symbol, with the inputs
    each dimension is labeled from. Uses only bars BEFORE the gap day for
    ATR, ADV, SMA200, the prior-day return and the volume ratio."""
    from .util import prev_market_day
    bars = sorted((b for b in bars or [] if b.get("o") and b.get("c")), key=_bar_date)
    dates = [_bar_date(b) for b in bars]
    out = []
    for i in range(15, len(bars)):
        d = dates[i]
        if d < start or d > end:
            continue
        if dates[i - 1] != prev_market_day(d):
            continue   # a missing session: the "gap" would span more than one night
        prev, cur = bars[i - 1], bars[i]
        pc, o = float(prev["c"]), float(cur["o"])
        if pc < PRICE_MIN or pc <= 0:
            continue
        gap = (o - pc) / pc * 100.0
        if abs(gap) < GAP_MIN_PCT:
            continue
        hist = bars[max(0, i - 20):i]
        if len(hist) < 20:
            continue
        adv = sum(float(b["c"]) * float(b.get("v") or 0) for b in hist) / len(hist)
        if adv < ADV_MIN_USD:
            continue
        trs = []
        for j in range(i - 14, i):
            h, lo, pcj = float(bars[j]["h"]), float(bars[j]["l"]), float(bars[j - 1]["c"])
            trs.append(max(h - lo, abs(h - pcj), abs(lo - pcj)))
        atr = sum(trs) / len(trs)
        if atr <= 0:
            continue
        sma200 = (sum(float(b["c"]) for b in bars[i - 200:i]) / 200.0) if i >= 200 else None
        prior = ((pc / float(bars[i - 2]["c"]) - 1.0) * 100.0) if i >= 2 and bars[i - 2]["c"] else None
        mean_v = sum(float(b.get("v") or 0) for b in hist) / len(hist)
        vol_ratio = (float(prev.get("v") or 0) / mean_v) if mean_v > 0 else None
        out.append({
            "session_date": d, "symbol": symbol, "ticker": symbol, "gap_pct": round(gap, 4),
            "gap_atr": round(abs(o - pc) / atr, 4), "atr14": round(atr, 6), "prev_close": pc,
            "adv20_usd": round(adv, 2), "sma200": round(sma200, 6) if sma200 else None,
            "prior_ret_pct": round(prior, 4) if prior is not None else None,
            "vol_ratio": round(vol_ratio, 4) if vol_ratio is not None else None,
            "spy_gap_pct": round((spy_gap or {}).get(d), 4) if (spy_gap or {}).get(d) is not None else None,
            "industry": industry,
            "day_open": o, "day_high": float(cur["h"]), "day_low": float(cur["l"]),
            "day_close": float(cur["c"]),
        })
    return out


def simulate(event: dict, slippage_bps: float = 0.0) -> list[dict]:
    """Both arms of one gap, by the population monkeys' rules."""
    from .population import simulate_population_row
    bar = {"open": event["day_open"], "high": event["day_high"],
           "low": event["day_low"], "close": event["day_close"]}
    base = {"symbol": event["symbol"], "atr14": event["atr14"], "gap_pct": event["gap_pct"]}
    rows = []
    for arm in ARMS:
        o = simulate_population_row(base, bar, arm, slippage_bps)
        if o.get("r_multiple") is None:
            continue
        rows.append({**event, "arm": arm, "direction": o["direction"],
                     "entry_price": o["entry_price"], "stop_price": o["stop_price"],
                     "exit_price": o["exit_price"], "exit_reason": o["exit_reason"],
                     "r_multiple": o["r_multiple"], "sim_version": GAPFIELD_SIM_VERSION})
    return rows


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def _insert_rows(con: Any, rows: list[dict], source: str) -> int:
    """Multi-row inserts (one round trip per INSERT_BATCH rows): row-at-a-time
    inserts to Neon would spend the whole function budget on a single chunk.
    A row already present (same session, symbol, arm) is kept as it was."""
    now = datetime.now()
    written = 0
    for i in range(0, len(rows), INSERT_BATCH):
        part = rows[i:i + INSERT_BATCH]
        params: list = []
        for r in part:
            params += [r.get(c) if c not in ("source", "computed_at") else
                       (source if c == "source" else now) for c in _COLS]
        values = ",".join("(" + ",".join("?" for _ in _COLS) + ")" for _ in part)
        con.execute(f"INSERT INTO gapfield_events ({', '.join(_COLS)}) VALUES {values} "
                    "ON CONFLICT (session_date, symbol, arm) DO NOTHING", params)
        written += len(part)
    return written


def _cached_industries(con: Any, symbols: Iterable[str]) -> dict[str, str]:
    syms = sorted({str(s).upper() for s in symbols if s})
    out: dict[str, str] = {}
    for i in range(0, len(syms), 500):
        part = syms[i:i + 500]
        try:
            rows = con.execute(
                f"SELECT symbol, industry FROM fundamentals WHERE symbol IN "
                f"({','.join('?' for _ in part)}) AND industry IS NOT NULL", part).fetchall()
        except Exception:  # noqa: BLE001
            return out
        out.update({str(s).upper(): ind for s, ind in rows if ind})
    return out


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def instrument_classes(md, con, d, *, force: bool = False) -> dict | None:
    """{"common": {SYM: registered name}, "other": {SYM: class}} for every
    active, tradable asset, cached per session. None when the asset list is unavailable --
    the caller then records the universe as unclassified rather than guessing.

    Covers ALL tradable assets, not only a fractionable subset.
    """
    from . import instruments, ledger
    if con is not None and not force:
        cached = ledger.get_universe_cache(con, d, kind="classes")
        if cached and isinstance(cached["payload"].get("common"), dict):
            return cached["payload"]
    alp = getattr(md, "alpaca", None)
    if alp is None or not hasattr(alp, "asset_records"):
        return None
    try:
        records = alp.asset_records()
    except Exception as e:
        _log(f"WARN asset list unavailable for instrument classes: {e}")
        return None
    if not records:
        return None
    common: dict[str, str] = {}
    other: dict[str, str] = {}
    for a in records:
        sym = str(a.get("symbol") or "").upper()
        if not sym:
            continue
        cls = instruments.classify(a.get("name"))
        if cls == instruments.COMMON:
            common[sym] = a.get("name") or ""
        else:
            other[sym] = cls
    payload = {"common": common, "other": other}
    if con is not None:
        try:
            ledger.save_universe_cache(
                con, d, payload, kind="classes", n=len(common),
                source=f"assets={len(records)} common={len(common)} other={len(other)}")
        except Exception as e:
            _log(f"WARN could not cache instrument classes: {e}")
    _log(f"instrument classes built: {len(common)} common, {len(other)} other "
                  f"of {len(records)} tradable assets")
    return payload


def universe(md: Any, con: Any, d: date) -> list[str]:
    """Operating companies' common stock, by registered name (instrument
    classes, cached per session), shape-filtered to plain tickers. Index ETFs
    and leveraged products are never in it."""
    from .providers import EXCLUDED_PRODUCTS, INDEX_ETFS, is_common_stock_symbol
    classes = instrument_classes(md, con, d) or {}
    common = classes.get("common") or {}
    out = []
    for sym in common:
        s = str(sym).upper()
        if (s and len(s) <= 5 and not any(ch.isdigit() for ch in s)
                and s not in INDEX_ETFS and s not in EXCLUDED_PRODUCTS
                and is_common_stock_symbol(s)):
            out.append(s)
    return sorted(set(out))


def _daily(md: Any, symbols: list[str], start: date, end: date) -> dict[str, list[dict]]:
    from .util import ET
    alp = getattr(md, "alpaca", None)
    if alp is None or not symbols:
        return {}
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=ET)
    end_dt = datetime.combine(end, datetime.min.time(), tzinfo=ET)
    workers = getattr(alp, "concurrency", 1) or 1
    return alp.multi_bars(symbols, "1Day", start_dt, end_dt, feed="sip",
                          adjustment="split", workers=workers)


def scan_session(con: Any, md: Any, cfg: Any, d: date, *, fetch_industry: bool = True) -> dict:
    """Price session d's gap field (the forward rows). Two stages so a whole
    market costs a handful of requests: the last two daily bars for every
    symbol find the gappers; a year of bars for the gappers alone computes
    their inputs. Idempotent: a session already priced keeps its rows."""
    from .util import prev_market_day
    syms = universe(md, con, d)
    if not syms:
        return {"session": str(d), "events": 0, "rows": 0, "note": "universe unavailable"}
    p = prev_market_day(d)
    two = _daily(md, syms + [SPY], p, d)
    gappers = []
    for sym, bars in two.items():
        if sym == SPY or len(bars) < 2:
            continue
        b = sorted(bars, key=_bar_date)
        if _bar_date(b[-1]) != d or _bar_date(b[-2]) != p:
            continue
        pc, o = b[-2].get("c"), b[-1].get("o")
        if pc and o and float(pc) >= PRICE_MIN and abs((o - pc) / pc * 100.0) >= GAP_MIN_PCT:
            gappers.append(sym)
    spy = spy_gaps(two.get(SPY) or [])
    if not gappers:
        return {"session": str(d), "universe": len(syms), "events": 0, "rows": 0}
    hist = _daily(md, gappers, d - timedelta(days=LOOKBACK_DAYS), d)
    industries = _cached_industries(con, gappers)
    if fetch_industry:
        missing = [s for s in gappers if s not in industries]
        if missing and hasattr(md, "fundamentals"):
            try:
                for s, f in (md.fundamentals(missing, con) or {}).items():
                    if f.get("industry"):
                        industries[str(s).upper()] = f["industry"]
            except Exception as e:  # noqa: BLE001 -- an unknown industry is its own unit
                _log(f"WARN industry lookup failed for {len(missing)} gapper(s): {e}")
    events = []
    for sym in gappers:
        events += events_from_bars(sym, hist.get(sym) or [], d, d, spy, industries.get(sym))
    slip = float((cfg.get("slippage_bps", 0) if cfg is not None else 0) or 0)
    rows = [r for ev in events for r in simulate(ev, slip)]
    written = _insert_rows(con, rows, "forward")
    return {"session": str(d), "universe": len(syms), "gappers": len(gappers),
            "events": len(events), "rows": written}


# --------------------------------------------------------------------------
# Backfill (history; discovery only)
# --------------------------------------------------------------------------

def _range_for(today: date) -> tuple[date, date]:
    from .util import is_market_day, prev_market_day
    end = prev_market_day(today)
    n, d = 1, end
    while n < BACKFILL_SESSIONS:
        d -= timedelta(days=1)
        if is_market_day(d):
            n += 1
    return d, end


def ensure_backfill_job(con: Any, md: Any, today: date) -> dict | None:
    """The current backfill job, created (with its symbol list frozen) if
    none exists. None when the universe is unavailable."""
    row = con.execute("""SELECT job_id, range_start, range_end, n_symbols, next_index,
                                rows_written, finished_at FROM gapfield_jobs
                         ORDER BY started_at DESC LIMIT 1""").fetchone()
    if row:
        keys = ("job_id", "range_start", "range_end", "n_symbols", "next_index",
                "rows_written", "finished_at")
        return dict(zip(keys, row))
    syms = universe(md, con, today)
    if not syms:
        return None
    start, end = _range_for(today)
    job_id = f"gf-{start.isoformat()}-{end.isoformat()}"
    now = datetime.now()
    con.execute("""INSERT INTO gapfield_jobs (job_id, range_start, range_end, symbols_json,
                   n_symbols, next_index, rows_written, started_at, updated_at, finished_at, note)
                   VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, NULL, ?)""",
                [job_id, start, end, json.dumps(syms), len(syms), now, now,
                 f"{BACKFILL_SESSIONS} sessions of history for discovery; forward rows "
                 f"start after {end}"])
    _log(f"backfill job {job_id}: {len(syms)} symbols, {start} .. {end}")
    return {"job_id": job_id, "range_start": start, "range_end": end, "n_symbols": len(syms),
            "next_index": 0, "rows_written": 0, "finished_at": None}


def backfill_step(con: Any, md: Any, cfg: Any, today: date, deadline: float) -> dict:
    """Advance the backfill one symbol chunk at a time until `deadline`
    (a time.monotonic() value) or the job is done. Resumable: progress is
    written after every chunk."""
    job = ensure_backfill_job(con, md, today)
    if not job:
        return {"status": "universe unavailable"}
    if job.get("finished_at"):
        return {"status": "complete", "job": job["job_id"], "rows": job["rows_written"]}
    syms = json.loads(con.execute("SELECT symbols_json FROM gapfield_jobs WHERE job_id = ?",
                                  [job["job_id"]]).fetchone()[0] or "[]")
    start, end = _as_date(job["range_start"]), _as_date(job["range_end"])
    slip = float((cfg.get("slippage_bps", 0) if cfg is not None else 0) or 0)
    idx, written, chunks = int(job["next_index"] or 0), int(job["rows_written"] or 0), 0
    spy = None
    est, worst = FIRST_CHUNK_S, 0.0
    # A chunk is started only if it is expected to FINISH before the deadline:
    # one that started at the deadline's edge would run the function past its
    # budget and lose the summary refresh and the runs row after it.
    while idx < len(syms) and time.monotonic() + est < deadline:
        t_chunk = time.monotonic()
        part = syms[idx: idx + CHUNK]
        fetch = part + ([SPY] if spy is None else [])
        bars = _daily(md, fetch, start - timedelta(days=LOOKBACK_DAYS), end)
        if spy is None:
            spy = spy_gaps(bars.pop(SPY, []) or [])
        industries = _cached_industries(con, part)
        rows = []
        for sym in part:
            for ev in events_from_bars(sym, bars.get(sym) or [], start, end, spy,
                                       industries.get(sym)):
                rows += simulate(ev, slip)
        written += _insert_rows(con, rows, "backfill")
        idx += len(part)
        chunks += 1
        done = idx >= len(syms)
        con.execute("""UPDATE gapfield_jobs SET next_index = ?, rows_written = ?, updated_at = ?,
                       finished_at = ? WHERE job_id = ?""",
                    [idx, written, datetime.now(), datetime.now() if done else None, job["job_id"]])
        worst = max(worst, time.monotonic() - t_chunk)
        est = worst * 1.25          # the slowest chunk so far, with a margin
    return {"status": "complete" if idx >= len(syms) else "in progress", "job": job["job_id"],
            "chunks": chunks, "next_index": idx, "n_symbols": len(syms), "rows": written}


def _as_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


# --------------------------------------------------------------------------
# Research: cells for the weekly batch, rows for forward observations
# --------------------------------------------------------------------------

_ROW_COLS = ("session_date", "symbol", "arm", "direction", "gap_pct", "gap_atr", "prev_close",
             "adv20_usd", "sma200", "prior_ret_pct", "vol_ratio", "spy_gap_pct", "industry",
             "r_multiple")


def _rows(con: Any, where: str = "", params: list | None = None) -> list[dict]:
    try:
        rows = con.execute(
            f"SELECT {', '.join(_ROW_COLS)} FROM gapfield_events WHERE r_multiple IS NOT NULL "
            f"AND sim_version = ? {where}", [GAPFIELD_SIM_VERSION] + list(params or [])).fetchall()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        row = dict(zip(_ROW_COLS, r))
        row["ticker"] = row["symbol"]
        out.append(row)
    return out


def forward_rows(con: Any, d: date) -> list[dict]:
    """Session d's FORWARD rows only -- the rows a forward observation may
    come from. Backfilled history never is."""
    return _rows(con, "AND session_date = ? AND source = 'forward'", [d])


def cell_stats(rows: list[dict]) -> list[dict]:
    """Per (strategy, dimension, level): n in (session, industry) units, the
    unit mean and standard deviation, and the row count beside it."""
    from .clusters import cluster_key
    cells: dict[tuple, dict[tuple, list[float]]] = {}
    sessions: dict[tuple, set] = {}
    for r in rows:
        strat = label(STRATEGY_DIM, r)
        if strat == "?":
            continue
        unit = (r["session_date"], cluster_key(r))
        for dim in DIMS:
            lev = label(dim, r)
            if lev == "?":
                continue
            key = (strat, dim, lev)
            cells.setdefault(key, {}).setdefault(unit, []).append(float(r["r_multiple"]))
            sessions.setdefault(key, set()).add(r["session_date"])
    out = []
    for (strat, dim, lev), units in sorted(cells.items()):
        vals = [sum(v) / len(v) for v in units.values()]
        n = len(vals)
        mean = sum(vals) / n
        std = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1)) if n > 1 else None
        days = sessions[(strat, dim, lev)]
        out.append({"strategy": strat, "dimension": dim, "level": lev, "n_units": n,
                    "n_rows": sum(len(v) for v in units.values()), "mean_r": mean,
                    "std_r": std, "sessions": len(days), "first_session": min(days),
                    "last_session": max(days)})
    return out


def refresh_cells(con: Any, d: date) -> int:
    """Recompute the per-cell summary from every row and replace the table."""
    stats = cell_stats(_rows(con))
    con.execute("DELETE FROM gapfield_cells")
    cols = ("strategy", "dimension", "level", "n_units", "n_rows", "mean_r", "std_r",
            "sessions", "first_session", "last_session", "computed_on")
    for i in range(0, len(stats), INSERT_BATCH):
        part = stats[i:i + INSERT_BATCH]
        params: list = []
        for s in part:
            params += [s.get(c) if c != "computed_on" else d for c in cols]
        con.execute(f"INSERT INTO gapfield_cells ({', '.join(cols)}) VALUES "
                    + ",".join("(" + ",".join("?" for _ in cols) + ")" for _ in part), params)
    return len(stats)


def pair_name(dim: str, strategy: str, level: str) -> tuple[str, str]:
    """(dimension, level) of a strategy x dimension cell, named as a
    pre-registered pair (structural.pair_dimension: dimensions sorted, levels
    in the same order), so research.discovery_stats and the survivor
    registration read it like any 2-D pair in the batch."""
    from .structural import PAIR_SEP, pair_dimension
    dims = sorted([STRATEGY_DIM, dim])
    levels = {STRATEGY_DIM: strategy, dim: level}
    return pair_dimension(dims), PAIR_SEP.join(levels[x] for x in dims)


def comparisons(con: Any) -> list:
    """The gap field's cells as Comparisons for the weekly FDR batch, from the
    latest nightly summary. Every cell is a look, testable or not."""
    from .stats import Comparison
    try:
        rows = con.execute("""SELECT strategy, dimension, level, n_units, mean_r, std_r, n_rows,
                                     sessions FROM gapfield_cells""").fetchall()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for strat, dim, lev, n, mean, std, n_rows, sess in rows:
        name, level = pair_name(dim, strat, lev)
        out.append(Comparison(
            dimension=name, level=level, n=int(n or 0), mean_r=mean, std_r=std,
            n_dimensions=2,
            note=(f"gap field ({int(n_rows or 0)} rows over {int(sess or 0)} sessions, counted "
                  "in (session, industry) units; history is discovery only)")))
    return out


def status(con: Any) -> dict:
    """Backfill progress and row counts, for the self-check and the CLI."""
    out: dict[str, Any] = {}
    try:
        job = con.execute("""SELECT job_id, range_start, range_end, n_symbols, next_index,
                                    rows_written, finished_at FROM gapfield_jobs
                             ORDER BY started_at DESC LIMIT 1""").fetchone()
        if job:
            out["job"] = dict(zip(("job_id", "range_start", "range_end", "n_symbols",
                                   "next_index", "rows_written", "finished_at"), job))
        for src, n, s, lo, hi in con.execute(
                """SELECT source, COUNT(*), COUNT(DISTINCT session_date), MIN(session_date),
                          MAX(session_date) FROM gapfield_events GROUP BY source""").fetchall():
            out[str(src)] = {"rows": int(n), "sessions": int(s), "first": str(lo), "last": str(hi)}
    except Exception:  # noqa: BLE001
        pass
    return out


def weekly_md(search_comps: Iterable[Any], con: Any) -> str:
    """The gap field as the miner sees it: coverage, and its cells that
    survived this run's batch (already quarantined by the system)."""
    st = status(con)
    if not st.get("forward") and not st.get("backfill"):
        return ""
    gf = [c for c in search_comps or [] if STRATEGY_DIM in str(getattr(c, "dimension", ""))]
    sig = sorted([c for c in gf if getattr(c, "significant", False)],
                 key=lambda c: (c.q_value if c.q_value is not None else 1.0))
    job = st.get("job") or {}
    lines = ["## GAP FIELD (every liquid >=3% gapper, both arms, daily bars; history is "
             "discovery only, forward rows decide)", ""]
    for src in ("backfill", "forward"):
        if st.get(src):
            s = st[src]
            lines.append(f"- {src}: {s['rows']} rows over {s['sessions']} sessions "
                         f"({s['first']} .. {s['last']})")
    if job and not job.get("finished_at"):
        lines.append(f"- backfill in progress: {job.get('next_index')}/{job.get('n_symbols')} symbols")
    lines.append(f"- {len(gf)} cells in this run's batch; {len(sig)} survived FDR")
    for c in sig[:15]:
        lines.append(f"  - {c.dimension}={c.level}: mean {c.mean_r:+.3f}R over n={c.n} units, "
                     f"q={c.q_value}")
    lines += ["", "Survivors are quarantined by the system and tested on forward sessions only. "
              "A daily-bar stop biases every arm down; survivorship biases history up. Neither "
              "is edge until forward-validated."]
    return "\n".join(lines)


def report_text(con: Any, top: int = 15) -> str:
    """`run.py gapfield`: coverage, backfill progress and the strongest cells
    by |t| (descriptive; the weekly batch is where they are tested)."""
    st = status(con)
    lines = ["GAP FIELD (every liquid >=3% gapper, both arms, daily bars)"]
    job = st.get("job")
    if job:
        lines.append(f"backfill {job['job_id']}: {job['next_index']}/{job['n_symbols']} symbols, "
                     f"{job['rows_written']} rows" + (" (complete)" if job.get("finished_at") else ""))
    for src in ("backfill", "forward"):
        if st.get(src):
            s = st[src]
            lines.append(f"{src}: {s['rows']} rows, {s['sessions']} sessions ({s['first']} .. {s['last']})")
    try:
        cells = con.execute("""SELECT strategy, dimension, level, n_units, mean_r, std_r
                               FROM gapfield_cells WHERE n_units >= 8 AND std_r > 0""").fetchall()
    except Exception:  # noqa: BLE001
        cells = []
    ranked = sorted(cells, key=lambda c: -abs(c[4] / (c[5] / math.sqrt(c[3]))))[:top]
    if ranked:
        lines.append("")
        lines.append(f"strongest {len(ranked)} cell(s) by |t| (uncorrected; the weekly batch corrects):")
        for strat, dim, lev, n, mean, std in ranked:
            lines.append(f"  {strat:<15} {dim}={lev:<9} n={n:<5} mean {mean:+.3f}R  "
                         f"t={mean / (std / math.sqrt(n)):+.1f}")
    if len(lines) == 1:
        lines.append("(nothing priced yet: the nightly job fills this)")
    return "\n".join(lines)
