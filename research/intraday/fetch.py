"""Bulk historical bars for the intraday strategy search (local research only).

Builds three stores under ~/.trade-agent/research:
  daily.pkl          {symbol: ndarray[n, 6]}  (date ordinal, o, h, l, c, v), split-adjusted,
                     2021-03-01 onward, active common stocks + delisted common stocks + ETFs
  open15/<date>.npz  the 09:30, 09:35, 09:40 five-minute bars of every symbol liquid the
                     day before (price >= $2, 20-day dollar volume >= $3M)
  day5/<date>.npz    the full 09:30-15:55 five-minute session of that day's in-play set:
                     liquid gappers (|gap| >= 2%), the top 80 by opening-bar relative volume,
                     the top 100 by dollar volume, and the index/sector ETFs

Only what was knowable at 09:35 chooses the in-play set (the gap, the first bar's volume
against its own 14-session average, yesterday's liquidity), so a day5 file never selects
a stock by how its day turned out.
"""
from __future__ import annotations

import os
import pickle
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent.config import load_config  # noqa: E402
from agent.providers import ALPACA_DATA, MarketData  # noqa: E402
from agent.util import ET  # noqa: E402

STORE = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".trade-agent" / "research"
DAILY_START = date(2021, 3, 1)
ETFS = ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB",
        "XLU", "XLRE", "XLC", "SMH", "XBI", "KRE"]
LIQ_PRICE, LIQ_ADV = 2.0, 3e6
GAP_INPLAY, TOP_RVOL, TOP_ADV = 2.0, 80, 100

_session = requests.Session()


def _alp():
    return MarketData(load_config()).alpaca


def get_bars(alp, symbols, timeframe, start, end, batch=400, adjustment="split"):
    """{symbol: [(epoch_s, o, h, l, c, v), ...]} with paging and patient retries."""
    out: dict[str, list] = {}
    for i in range(0, len(symbols), batch):
        part = symbols[i:i + batch]
        token = None
        for _ in range(200):
            params = {"symbols": ",".join(part), "timeframe": timeframe,
                      "start": start.isoformat(), "end": end.isoformat(), "limit": 10000,
                      "adjustment": adjustment, "feed": "sip"}
            if token:
                params["page_token"] = token
            for attempt in range(6):
                try:
                    r = _session.get(f"{ALPACA_DATA}/v2/stocks/bars", headers=alp.h,
                                     params=params, timeout=60)
                    if r.status_code == 429 or r.status_code >= 500:
                        raise requests.exceptions.HTTPError(str(r.status_code))
                    r.raise_for_status()
                    data = r.json()
                    break
                except Exception:  # noqa: BLE001
                    if attempt == 5:
                        raise
                    time.sleep(1.5 * (attempt + 1))
            for sym, bars in (data.get("bars") or {}).items():
                rows = out.setdefault(sym, [])
                for b in bars or []:
                    ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp()
                    rows.append((ts, b["o"], b["h"], b["l"], b["c"], b["v"]))
            token = data.get("next_page_token")
            if not token:
                break
    return out


def universe(alp):
    from agent import gapfield, instruments
    md = MarketData(load_config())
    active = set(gapfield.universe(md, None, date.today()))
    inactive = set()
    a = _session.get("https://paper-api.alpaca.markets/v2/assets", headers=alp.h,
                     params={"status": "inactive", "asset_class": "us_equity"}, timeout=60).json()
    for x in a:
        s = x.get("symbol") or ""
        if (x.get("exchange") in ("NYSE", "NASDAQ", "ARCA", "BATS", "AMEX")
                and re.fullmatch(r"[A-Z]{1,5}", s)
                and instruments.classify(x.get("name")) == instruments.COMMON):
            inactive.add(s)
    return sorted(active), sorted(inactive - active)


def build_daily(alp):
    active, inactive = universe(alp)
    syms = active + inactive + ETFS
    print(f"daily: {len(active)} active + {len(inactive)} delisted + {len(ETFS)} ETFs", flush=True)
    start = datetime.combine(DAILY_START, datetime.min.time(), tzinfo=ET)
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    chunks = [syms[i:i + 100] for i in range(0, len(syms), 100)]
    out: dict[str, np.ndarray] = {}
    t0 = time.time()

    def one(part):
        got = get_bars(alp, part, "1Day", start, end, batch=100)
        res = {}
        for s, rows in got.items():
            if len(rows) < 30:
                continue
            arr = np.array([(datetime.fromtimestamp(r[0], ET).date().toordinal(), *r[1:]) for r in rows],
                           dtype=np.float64)
            dv = arr[:, 4] * arr[:, 5]
            # keep names that were ever liquid enough to matter (20-day mean dollar volume)
            if len(dv) >= 20 and np.convolve(dv, np.ones(20) / 20, "valid").max() >= LIQ_ADV or s in ETFS:
                res[s] = arr
        return res

    with ThreadPoolExecutor(8) as pool:
        for k, res in enumerate(pool.map(one, chunks)):
            out.update(res)
            if k % 10 == 0:
                print(f"  chunk {k}/{len(chunks)} kept {len(out)} {time.time() - t0:.0f}s", flush=True)
    meta = {"active": active, "inactive": inactive, "etfs": ETFS, "built": str(datetime.now())}
    pickle.dump({"bars": out, "meta": meta}, open(STORE / "daily.pkl", "wb"))
    print(f"daily done: {len(out)} symbols, {time.time() - t0:.0f}s", flush=True)


def load_daily():
    return pickle.load(open(STORE / "daily.pkl", "rb"))


def sessions(daily, start: date, end: date):
    spy = daily["bars"]["SPY"]
    return [date.fromordinal(int(x)) for x in spy[:, 0] if start.toordinal() <= x <= end.toordinal()]


def liquid_by_day(daily, days):
    """{session: [symbols liquid as of the PRIOR session's close]} plus prior close and adv20."""
    idx = {d.toordinal(): d for d in days}
    out = {d: {} for d in days}
    for s, a in daily["bars"].items():
        dv = a[:, 4] * a[:, 5]
        adv = np.full(len(a), np.nan)
        if len(a) >= 21:
            adv[20:] = np.convolve(dv, np.ones(20) / 20, "valid")[:-1]   # mean of the 20 sessions BEFORE i
        for i in range(21, len(a)):
            d = idx.get(int(a[i, 0]))
            if d is None:
                continue
            pc = a[i - 1, 4]
            if s in ETFS or (pc >= LIQ_PRICE and adv[i] >= LIQ_ADV):
                out[d][s] = (pc, adv[i], a[i, 1])     # prior close, adv20 before today, today's open
    return out


def _epoch(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET)


def build_open15(alp, start: date, end: date):
    daily = load_daily()
    days = sessions(daily, start, end)
    liq = liquid_by_day(daily, days)
    todo = [d for d in days if not (STORE / "open15" / f"{d}.npz").exists()]
    print(f"open15: {len(todo)} sessions to fetch", flush=True)
    t0 = time.time()

    def one(d):
        syms = sorted(liq[d])
        got = get_bars(alp, syms, "5Min", _epoch(d, 9, 30), _epoch(d, 9, 45), batch=1000)
        names, arr = [], []
        base = _epoch(d, 9, 30).timestamp()
        for s in syms:
            rows = got.get(s) or []
            m = np.full((3, 5), np.nan)
            for r in rows:
                k = int(round((r[0] - base) / 300))
                if 0 <= k < 3:
                    m[k] = r[1:]
            names.append(s); arr.append(m)
        np.savez_compressed(STORE / "open15" / f"{d}.npz", syms=np.array(names), bars=np.array(arr))
        return d

    with ThreadPoolExecutor(8) as pool:
        for k, d in enumerate(pool.map(one, todo)):
            if k % 50 == 0:
                print(f"  {k}/{len(todo)} {d} {time.time() - t0:.0f}s", flush=True)
    print(f"open15 done {time.time() - t0:.0f}s", flush=True)


def load_open15(d: date):
    p = STORE / "open15" / f"{d}.npz"
    if not p.exists():
        return None
    z = np.load(p)
    return list(z["syms"]), z["bars"]


def build_day5(alp, start: date, end: date):
    import pickle as _p
    from context import inplay, save_ctx, stream
    daily = load_daily()
    days = sessions(daily, start, end)
    sets_path = STORE / "inplay_sets.pkl"
    if sets_path.exists():
        sets = _p.load(open(sets_path, "rb"))
    else:
        sets = {}
        t0 = time.time()
        for k, (d, ctx) in enumerate(stream(daily, days)):
            sets[d] = inplay(ctx)
            save_ctx(d, ctx, sets[d])
            if k % 100 == 0:
                print(f"  context {k}/{len(days)} {d} in-play {len(sets[d])} {time.time() - t0:.0f}s", flush=True)
        _p.dump(sets, open(sets_path, "wb"))
    del daily
    todo = [d for d in days if not (STORE / "day5" / f"{d}.npz").exists()]
    print(f"day5: {len(todo)} sessions, mean in-play {np.mean([len(sets[d]) for d in days]):.0f}", flush=True)
    t0 = time.time()

    def one(d):
        syms = sets[d]
        got = get_bars(alp, syms, "5Min", _epoch(d, 9, 30), _epoch(d, 16, 0), batch=400)
        base = _epoch(d, 9, 30).timestamp()
        names, arr = [], []
        for s in syms:
            m = np.full((78, 5), np.nan)
            for r in got.get(s) or []:
                k = int(round((r[0] - base) / 300))
                if 0 <= k < 78:
                    m[k] = r[1:]
            names.append(s); arr.append(m)
        np.savez_compressed(STORE / "day5" / f"{d}.npz", syms=np.array(names), bars=np.array(arr, dtype=np.float32))
        return d

    with ThreadPoolExecutor(8) as pool:
        for k, d in enumerate(pool.map(one, todo)):
            if k % 50 == 0:
                print(f"  {k}/{len(todo)} {d} {time.time() - t0:.0f}s", flush=True)
    print(f"day5 done {time.time() - t0:.0f}s", flush=True)


def load_day5(d: date):
    p = STORE / "day5" / f"{d}.npz"
    if not p.exists():
        return None
    z = np.load(p)
    return list(z["syms"]), z["bars"]


def build_min1(alp, start: date, end: date):
    """One-minute sessions for S1's candidates only (REGISTER refinement log, fix 1)."""
    from context import load_ctx, s1_candidates
    (STORE / "min1").mkdir(exist_ok=True)
    daily = load_daily()
    days = sessions(daily, start, end)
    del daily
    todo = [d for d in days if not (STORE / "min1" / f"{d}.npz").exists()
            and (STORE / "ctx" / f"{d}.pkl").exists()]
    print(f"min1: {len(todo)} sessions", flush=True)
    t0 = time.time()

    def one(d):
        syms = s1_candidates(load_ctx(d))
        got = get_bars(alp, syms, "1Min", _epoch(d, 9, 30), _epoch(d, 16, 0), batch=400) if syms else {}
        base = _epoch(d, 9, 30).timestamp()
        names, arr = [], []
        for s in syms:
            m = np.full((390, 5), np.nan)
            for r in got.get(s) or []:
                k = int(round((r[0] - base) / 60))
                if 0 <= k < 390:
                    m[k] = r[1:]
            names.append(s); arr.append(m)
        np.savez_compressed(STORE / "min1" / f"{d}.npz", syms=np.array(names),
                            bars=np.array(arr, dtype=np.float32).reshape(len(names), 390, 5))
        return d

    with ThreadPoolExecutor(6) as pool:
        for k, d in enumerate(pool.map(one, todo)):
            if k % 100 == 0:
                print(f"  {k}/{len(todo)} {d} {time.time() - t0:.0f}s", flush=True)
    print(f"min1 done {time.time() - t0:.0f}s", flush=True)


def load_min1(d: date):
    p = STORE / "min1" / f"{d}.npz"
    if not p.exists():
        return None
    z = np.load(p)
    return list(z["syms"]), z["bars"]


if __name__ == "__main__":
    STORE.mkdir(parents=True, exist_ok=True)
    alp = _alp()
    stage = sys.argv[1]
    s, e = date(2022, 1, 3), date(2026, 10, 6)
    if stage == "daily":
        build_daily(alp)
    elif stage == "open15":
        build_open15(alp, s, e)
    elif stage == "day5":
        build_day5(alp, s, e)
    elif stage == "min1":
        build_min1(alp, s, e)
