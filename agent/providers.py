"""
Market data providers with graceful degradation.

Priority: Alpaca (premarket-capable, free with paper account) -> yfinance fallback.
Every fetch is individually fault-tolerant; failures are reported in bundle['missing']
so the model KNOWS which sources it is flying without.

Also: SEC EDGAR 8-K feed, rule-based econ calendar, offline synthetic mode for testing.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET_XML
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from datetime import date, datetime, time as dtime, timedelta
from typing import Any

import requests

from . import indicators as ind
from .config import ECON_OVERRIDES_PATH, MOCKS_DIR, Config
from .indicators import rvol_simple, vwap_distance, vwap_series
from .util import ET, is_market_day, log, now_et, prev_market_day

ALPACA_DATA = "https://data.alpaca.markets"

# Per-symbol yfinance lookups (earnings dates, fundamentals) are slow, serial,
# blocking network calls -- with 40+ symbols in a morning bundle these alone
# can approach the Vercel 300s function budget. Bounded worker pool + an
# overall batch deadline keeps total wall time flat regardless of universe
# size.
#
# 2026-08-31 incident: fut.result(timeout=_YF_FETCH_TIMEOUT) only bounds how
# long the CALLER waits on one future -- it does NOT cancel the underlying
# thread. `with ThreadPoolExecutor(...) as pool:` then calls
# pool.shutdown(wait=True) on exit, which blocks until every submitted task
# has actually finished, including ones the caller already gave up on. One
# yfinance call hanging on a rate-limited/unresponsive connection (no request
# timeout is set anywhere in the yfinance call path) was enough to stall the
# whole batch past the per-future timeout, and the morning cron ate a 300s
# Vercel timeout before ever reaching send_email. Fixed by waiting on the
# WHOLE batch against one deadline and shutting the pool down without
# joining stragglers -- see _bounded_map below.
_YF_FETCH_WORKERS = 8
_YF_BATCH_DEADLINE = 25  # hard wall-clock cap for one next_earnings()/fundamentals() call


def _bounded_map(items: list[str], fn, workers: int = _YF_FETCH_WORKERS,
                  deadline: float = _YF_BATCH_DEADLINE) -> dict[str, Any]:
    """Run fn(item) for each item across a bounded worker pool, but never
    wait past `deadline` seconds total. Stragglers are abandoned (their
    threads may keep running in the background, unjoined) rather than
    blocking the caller -- callers already treat a missing key as
    "couldn't determine," never as a fabricated value."""
    out: dict[str, Any] = {}
    if not items:
        return out
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(fn, s): s for s in items}
        done, not_done = wait_futures(futures, timeout=deadline)
        for fut in done:
            s = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                log("data", f"WARN fetch failed for {s}: {e}")
                result = None
            if result is not None:
                out[s] = result
        if not_done:
            stuck = ", ".join(sorted(futures[f] for f in not_done))
            log("data", f"WARN {len(not_done)} lookup(s) still running after {deadline}s "
                        f"batch deadline, abandoning: {stuck}")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return out

FUTURES = {"ES=F": "S&P 500 fut", "NQ=F": "Nasdaq fut", "RTY=F": "Russell fut",
           "CL=F": "Crude oil", "GC=F": "Gold", "DX-Y.NYB": "Dollar index", "^TNX": "10Y yield"}
INTERNATIONAL = {"^N225": "Nikkei", "^HSI": "Hang Seng", "^GDAXI": "DAX", "^FTSE": "FTSE"}
SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLC", "XLRE"]
INDEX_ETFS = ["SPY", "QQQ", "IWM"]

# Leveraged, inverse, and volatility products. These move on mechanical leverage
# and decay, not on company-specific catalysts, so a gap in one is a restatement
# of its underlying index, not an idea — a 3x semiconductor ETF gapping 6% is
# NVDA gapping 2% wearing a costume. SINGLE SOURCE OF TRUTH — import this,
# never re-declare it. (DTO's duplicate list drifted and silently readmitted
# the single-stock leveraged complex; do not repeat that.)
EXCLUDED_PRODUCTS: frozenset[str] = frozenset({
    # 3x/2x index & sector
    "TQQQ", "SQQQ", "SPXL", "SPXS", "SOXL", "SOXS", "TNA", "TZA", "UPRO",
    "SPXU", "UDOW", "SDOW", "LABU", "LABD", "FAS", "FAZ", "YINN", "YANG",
    "NUGT", "DUST", "JNUG", "JDST", "BOIL", "KOLD", "UCO", "SCO", "ERX", "ERY",
    "SSO", "SDS", "QLD", "QID", "TECL", "TECS", "FNGU", "FNGD",
    # volatility
    "UVXY", "VXX", "SVXY", "VIXY", "UVIX", "SVIX",
    # single-stock leveraged complex
    "NVDL", "NVDU", "NVDD", "TSLL", "TSLQ", "TSLS", "CONL", "MSTX", "MSTZ",
    "AMDL", "AAPU", "MSFU", "GGLL", "AMZU", "METU", "PLTU",
})

# ETFs never report single-company earnings -- skipping them here avoids a
# guaranteed-empty yfinance round trip for every one of them, every morning.
# EXCLUDED_PRODUCTS are all ETFs/ETNs, so they get the same treatment (the
# 2026-08/09 production error log was flooded by exactly these no-earnings
# lookups failing one symbol at a time).
_NO_EARNINGS_SYMBOLS = frozenset(SECTOR_ETFS) | frozenset(INDEX_ETFS) | EXCLUDED_PRODUCTS

# Liquid scan universe for premarket gap detection (Alpaca snapshots).
SCAN_UNIVERSE = [
    # mega/large tech & AI
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "AMD", "CRM",
    "ORCL", "ADBE", "NFLX", "INTC", "MU", "QCOM", "TXN", "AMAT", "LRCX", "KLAC",
    "SMCI", "ARM", "PLTR", "SNOW", "CRWD", "PANW", "NOW", "UBER", "SHOP", "SQ",
    "COIN", "MSTR", "HOOD", "SOFI", "RBLX", "NET", "DDOG", "MDB", "ZS", "TEAM",
    "DELL", "HPE", "IBM", "CSCO", "ANET", "MRVL", "ON", "SWKS", "QRVO", "TER",
    # financials / industrials / energy
    "JPM", "BAC", "GS", "MS", "WFC", "C", "SCHW", "BLK", "V", "MA", "AXP",
    "CAT", "DE", "BA", "GE", "HON", "UNP", "UPS", "FDX", "LMT", "RTX",
    "XOM", "CVX", "COP", "SLB", "OXY", "DVN", "FANG", "HAL", "MRO", "APA",
    # healthcare / biotech
    "UNH", "JNJ", "LLY", "PFE", "MRK", "ABBV", "TMO", "AMGN", "GILD", "BIIB",
    "MRNA", "VRTX", "REGN", "ISRG", "CVS", "HUM", "BMY", "ZTS",
    # consumer / other high-beta
    "WMT", "COST", "TGT", "HD", "LOW", "MCD", "SBUX", "NKE", "LULU", "DIS",
    "ABNB", "BKNG", "MAR", "DAL", "UAL", "AAL", "CCL", "RCL", "NCLH", "LVS",
    "F", "GM", "RIVN", "LCID", "NIO", "CHPT", "RUN", "ENPH", "FSLR", "PLUG",
    "GME", "AMC", "CVNA", "UPST", "AFRM", "DKNG", "PENN", "CZR", "WYNN", "MGM",
]

_SCAN_SET = frozenset(SCAN_UNIVERSE)

# Structural instrument filter (Addendum A §A3). Warrants, units, rights, and
# preferreds are not common-stock candidates: they have their own supply
# mechanics, they routinely gap on conversion/expiry events no equity thesis
# explains, and yfinance has no earnings/fundamentals coverage for them — so
# every ATR/earnings gate on one fails OPEN, silently. Observed in production
# 2026-08/09: GFAIW, GIPRW, PSQH.WS and dozens more via movers/most-actives.
# Deliberately NARROW: explicit dot/dash suffixes plus the NASDAQ 5th-letter
# W/U/R convention (allowlist-first — SCAN_UNIVERSE names always pass). A
# filter that silently drops a real candidate is worse than one that lets a
# warrant through, because the shadow ledger can price the warrant and cannot
# price the absence.
_STRUCTURED_SUFFIX_RE = re.compile(r"[.\-](WS[AB]?|U|R|RT|PR[A-Z]?)$", re.IGNORECASE)


def is_common_stock_symbol(sym: str) -> bool:
    """Conservative shape check. False for warrant/unit/rights suffixes
    (.WS/.U/.R/.RT/-WS, NASDAQ 5-char symbols ending W/U/R), for symbols
    carrying digits, and for symbols longer than 5 characters. Symbols in
    SCAN_UNIVERSE are always common stock and skip the shape rules."""
    s = str(sym or "").upper().strip()
    if not s:
        return False
    if s in _SCAN_SET:
        return True
    if len(s) > 5 or any(ch.isdigit() for ch in s):
        return False
    if _STRUCTURED_SUFFIX_RE.search(s):
        return False
    if len(s) == 5 and s[-1] in "WUR":
        return False
    return True


# ------------------------------------------------------------------ Alpaca API

class Alpaca:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.feed = cfg.data_cfg.get("alpaca_feed", "sip")
        self.h = {
            "APCA-API-KEY-ID": cfg.alpaca_key_id,
            "APCA-API-SECRET-KEY": cfg.alpaca_secret_key,
        }

    def _get(self, path: str, params: dict | None = None) -> dict:
        # One retry on transient failures (timeout/connection/429/5xx) so a single
        # network blip doesn't null an entire batched fetch (e.g. multi_bars for
        # every symbol in the morning enrichment pass) and get misread as "the
        # feed has no data" when it was actually "the first attempt hiccuped."
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                r = requests.get(f"{ALPACA_DATA}{path}", headers=self.h, params=params or {}, timeout=20)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_exc = e
                if attempt == 0:
                    time.sleep(0.75)
                    continue
                raise
            if r.status_code == 429 or r.status_code >= 500:
                last_exc = requests.exceptions.HTTPError(f"{r.status_code} for {path}", response=r)
                if attempt == 0:
                    time.sleep(0.75)
                    continue
            r.raise_for_status()
            return r.json()
        raise last_exc

    def snapshots(self, symbols: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(symbols), 100):
            batch = symbols[i : i + 100]
            data = self._get("/v2/stocks/snapshots",
                             {"symbols": ",".join(batch), "feed": self.feed})
            # Defensive unwrap: the endpoint returns a bare {symbol: snapshot}
            # map today, but some Alpaca data routes nest their payload under a
            # named key. If that shape ever arrives here the bare-map read below
            # would silently yield zero quotes -- indistinguishable from "the
            # feed has no data" -- so normalize it rather than trust the shape.
            if isinstance(data, dict) and isinstance(data.get("snapshots"), dict):
                data = data["snapshots"]
            out.update(data if isinstance(data, dict) else {})
        out.pop("next_page_token", None)
        return out

    def movers(self, top: int = 18) -> dict:
        return self._get("/v1beta1/screener/stocks/movers", {"top": top})

    def tradable_assets(self) -> list[str]:
        """Active, tradable US common-stock symbols from /v2/assets.

        /v2/assets lives on the TRADING host, not the data host, and the two
        take different key pairs. DTO's notes say its live keys worked on
        api.alpaca.markets and 401'd on paper-api; this account is the mirror
        image — measured 2026-09-15, paper-api returns 200 with 12,890 assets
        and api.alpaca.markets 401s. So the paper host is tried first here and
        the live host is the fallback, which covers either key type.

        The filter reproduces DTO's (js/alpaca.js:307-309): tradable,
        fractionable is not False, no digit in the symbol, at most 5
        characters. That leaves ~7,182 symbols, which the caller then screens
        on dollar volume.
        """
        hosts = ["https://paper-api.alpaca.markets", "https://api.alpaca.markets"]
        params = {"status": "active", "asset_class": "us_equity",
                  "exchange": "NYSE,NASDAQ,ARCA,BATS"}
        for host in hosts:
            try:
                r = requests.get(f"{host}/v2/assets", headers=self.h,
                                 params=params, timeout=60)
                if r.status_code in (401, 403):
                    continue
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                log("data", f"WARN /v2/assets failed on {host}: {e}")
                continue
            out = [
                a["symbol"] for a in data
                if a.get("tradable")
                and a.get("fractionable") is not False
                and not re.search(r"\d", a.get("symbol", ""))
                and len(a.get("symbol", "")) <= 5
            ]
            if out:
                return sorted(set(out))
        log("data", "WARN /v2/assets returned nothing on either host")
        return []

    def most_actives(self, top: int = 15, by: str = "volume") -> dict:
        """Most-actives screener. `by` is "volume" (default, unchanged for every
        existing caller) or "trades" — the runner universe unions both, since
        DTO's scanner pulls 100 of each and they do not fully overlap."""
        return self._get("/v1beta1/screener/stocks/most-actives", {"by": by, "top": top})

    def news(self, limit: int = 40) -> list[dict]:
        data = self._get("/v1beta1/news", {"limit": limit, "sort": "desc"})
        return data.get("news", [])

    def minute_bars(self, symbol: str, d: date) -> list[dict]:
        start = datetime.combine(d, dtime(9, 30), tzinfo=ET)
        end = datetime.combine(d, dtime(16, 0), tzinfo=ET)
        params = {
            "timeframe": "1Min",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": 10000,
            "adjustment": "raw",
            "feed": self.feed,
        }
        data = self._get(f"/v2/stocks/{symbol}/bars", params)
        bars = []
        for b in data.get("bars", []) or []:
            ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
            bars.append({"t": ts, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]})
        return bars

    def daily_bar(self, symbol: str, d: date) -> dict | None:
        params = {
            "timeframe": "1Day",
            "start": d.isoformat(),
            "end": (d + timedelta(days=1)).isoformat(),
            "feed": self.feed,
        }
        data = self._get(f"/v2/stocks/{symbol}/bars", params)
        bars = data.get("bars", []) or []
        if not bars:
            return None
        b = bars[0]
        return {"open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"], "volume": b["v"]}

    def multi_bars(self, symbols: list[str], timeframe: str,
                   start: datetime, end: datetime) -> dict[str, list[dict]]:
        """One batched multi-symbol bars fetch (the /v2/stocks/bars endpoint,
        distinct from /v2/stocks/{symbol}/bars) — used for ADV20 and premarket
        VWAP/RVOL enrichment so N gappers cost one round trip, not N.

        Follows `next_page_token`. Alpaca caps a response by TOTAL bars across
        every symbol, not per symbol, and it orders the payload alphabetically,
        so a request that spans pages returns a complete prefix and then simply
        stops — every symbol after the cut comes back absent. That is
        indistinguishable from "those tickers had no data," which is the worst
        possible failure shape for a gate that treats missing bars as a
        rejection.

        Measured before this fix (2026-09-11, 95 symbols, 5Min, 09:30–15:00 ET):
        40 symbols and 2289 bars returned with `next_page_token` set, 55 symbols
        silently empty — a contiguous alphabetical tail from LABU onward.
        The 09:30–11:30 window fit in one page, which is why this stayed
        invisible: it only bites as the window or the symbol count grows.
        """
        out: dict[str, list[dict]] = {}
        for i in range(0, len(symbols), 100):
            batch = symbols[i : i + 100]
            page_token: str | None = None
            # Bounded so a malformed/cyclic token can never spin forever; 10k
            # bars/page over a 100-symbol batch is far more than a session holds.
            for _ in range(50):
                params = {
                    "symbols": ",".join(batch),
                    "timeframe": timeframe,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "limit": 10000,
                    "adjustment": "raw",
                    "feed": self.feed,
                }
                if page_token:
                    params["page_token"] = page_token
                data = self._get("/v2/stocks/bars", params)
                for sym, bars in (data.get("bars") or {}).items():
                    rows = out.setdefault(sym, [])
                    for b in bars or []:
                        ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
                        rows.append({"t": ts, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]})
                page_token = data.get("next_page_token")
                if not page_token:
                    break
            else:
                log("data", f"WARN multi_bars hit the page cap for a {len(batch)}-symbol "
                            f"{timeframe} batch; results may be truncated")
            # A symbol present in the request but absent from every page genuinely
            # has no bars — keep the empty list so callers can tell "asked and got
            # nothing" from "never asked."
            for sym in batch:
                out.setdefault(sym, [])
        return out

    def multi_daily_bars(self, symbols: list[str], start: date, end: date) -> dict[str, list[dict]]:
        start_dt = datetime.combine(start, dtime(0, 0), tzinfo=ET)
        end_dt = datetime.combine(end, dtime(0, 0), tzinfo=ET)
        raw = self.multi_bars(symbols, "1Day", start_dt, end_dt)
        return {sym: [{"close": r["c"], "volume": r["v"]} for r in rows] for sym, rows in raw.items()}


# --------------------------------------------------------------- yfinance side

def _yf():
    import yfinance as yf
    return yf


def yf_quote_change(symbols: dict[str, str]) -> list[dict]:
    """Last price + % change vs previous close for a dict of {ticker: label}."""
    yf = _yf()
    out = []
    for sym, label in symbols.items():
        try:
            fi = yf.Ticker(sym).fast_info
            last = float(fi["last_price"])
            prev = float(fi["previous_close"])
            out.append({"symbol": sym, "label": label, "last": round(last, 2),
                        "pct": round((last - prev) / prev * 100, 2) if prev else None})
        except Exception:
            continue
    return out


def yf_minute_bars(symbol: str, d: date) -> list[dict]:
    yf = _yf()
    hist = yf.Ticker(symbol).history(
        start=d.isoformat(), end=(d + timedelta(days=1)).isoformat(),
        interval="1m", prepost=False,
    )
    bars = []
    for ts, row in hist.iterrows():
        ts_et = ts.tz_convert(ET) if ts.tzinfo else ts.tz_localize(ET)
        if ts_et.time() < dtime(9, 30) or ts_et.time() >= dtime(16, 0):
            continue
        bars.append({"t": ts_et.to_pydatetime(), "o": float(row["Open"]), "h": float(row["High"]),
                     "l": float(row["Low"]), "c": float(row["Close"]), "v": int(row["Volume"])})
    return bars


def yf_daily_bar(symbol: str, d: date) -> dict | None:
    yf = _yf()
    hist = yf.Ticker(symbol).history(
        start=d.isoformat(), end=(d + timedelta(days=1)).isoformat(), interval="1d")
    if hist.empty:
        return None
    row = hist.iloc[0]
    return {"open": float(row["Open"]), "high": float(row["High"]), "low": float(row["Low"]),
            "close": float(row["Close"]), "volume": int(row["Volume"])}


def yf_history_closes(symbols: list[str], days: int = 70) -> dict[str, list[float]]:
    """Daily closes per symbol (oldest -> newest), one batched download."""
    yf = _yf()
    df = yf.download(
        symbols, period=f"{days}d", interval="1d",
        progress=False, threads=True, auto_adjust=True, group_by="ticker",
    )
    out: dict[str, list[float]] = {}
    for sym in symbols:
        series = None
        for accessor in (lambda: df[sym]["Close"], lambda: df["Close"][sym], lambda: df["Close"]):
            try:
                series = accessor()
                break
            except Exception:
                continue
        try:
            out[sym] = [float(x) for x in series.dropna().tolist()] if series is not None else []
        except Exception:
            out[sym] = []
    return out


def yf_history_volumes(symbols: list[str], days: int = 40) -> dict[str, list[float]]:
    """Daily volumes per symbol (oldest -> newest), one batched download.
    Fallback source for ADV20 when Alpaca's multi-symbol daily bars miss a
    ticker (mirrors yf_history_closes)."""
    yf = _yf()
    df = yf.download(
        symbols, period=f"{days}d", interval="1d",
        progress=False, threads=True, auto_adjust=True, group_by="ticker",
    )
    out: dict[str, list[float]] = {}
    for sym in symbols:
        series = None
        for accessor in (lambda: df[sym]["Volume"], lambda: df["Volume"][sym], lambda: df["Volume"]):
            try:
                series = accessor()
                break
            except Exception:
                continue
        try:
            out[sym] = [float(x) for x in series.dropna().tolist()] if series is not None else []
        except Exception:
            out[sym] = []
    return out


def yf_history_bars(symbols: list[str], days: int = 380) -> dict[str, list[dict]]:
    """Full daily OHLCV bars per symbol (oldest -> newest), one batched
    download. Fallback source for MarketData.daily_context when Alpaca's
    multi_bars(1Day) misses a ticker (mirrors yf_history_closes/_volumes)."""
    yf = _yf()
    df = yf.download(
        symbols, period=f"{days}d", interval="1d",
        progress=False, threads=True, auto_adjust=True, group_by="ticker",
    )
    out: dict[str, list[dict]] = {}
    for sym in symbols:
        sub = None
        for accessor in (lambda: df[sym], lambda: df):
            try:
                sub = accessor()
                break
            except Exception:
                continue
        if sub is None:
            out[sym] = []
            continue
        try:
            rows = []
            for ts, row in sub.dropna().iterrows():
                ts_dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
                rows.append({"t": ts_dt, "o": float(row["Open"]), "h": float(row["High"]),
                            "l": float(row["Low"]), "c": float(row["Close"]),
                            "v": int(row["Volume"])})
            out[sym] = rows
        except Exception:
            out[sym] = []
    return out


# -------------------------------------------------------------------- SEC EDGAR

def edgar_recent_8ks(user_agent: str, count: int = 30) -> list[dict]:
    """Latest 8-K filings from EDGAR's public atom feed."""
    url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
           f"&type=8-K&company=&dateb=&owner=include&count={count}&output=atom")
    r = requests.get(url, headers={"User-Agent": user_agent}, timeout=20)
    r.raise_for_status()
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET_XML.fromstring(r.content)
    out = []
    for entry in root.findall("a:entry", ns):
        title = entry.findtext("a:title", "", ns)
        updated = entry.findtext("a:updated", "", ns)
        out.append({"title": title, "filed_at": updated})
    return out


# ---------------------------------------------------------------- econ calendar

FOMC_DATES = {
    # decision (day-2) dates, 2:00 PM ET statement
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
}


def econ_events(d: date) -> list[dict]:
    """Rule-based recurring events + user overrides from data/econ_overrides.json."""
    events: list[dict] = []
    if d.isoformat() in FOMC_DATES:
        events.append({"time": "14:00", "name": "FOMC rate decision + presser", "importance": "critical"})
    if d.weekday() == 3:  # Thursday
        events.append({"time": "08:30", "name": "Initial jobless claims", "importance": "medium"})
    if d.weekday() == 4 and 1 <= d.day <= 7:  # first Friday
        events.append({"time": "08:30", "name": "Nonfarm payrolls (NFP)", "importance": "critical"})
    if d.weekday() == 4 and 15 <= d.day <= 21:  # third Friday
        events.append({"time": "16:00", "name": "Monthly options expiration (OPEX)", "importance": "medium"})
    if (d + timedelta(days=1)).day == 1:
        events.append({"time": "", "name": "Month-end rebalancing flows", "importance": "low"})
    # user-maintained overrides (CPI, PPI, earnings, etc.)
    if ECON_OVERRIDES_PATH.exists():
        try:
            overrides = json.loads(ECON_OVERRIDES_PATH.read_text(encoding="utf-8"))
            for ev in overrides.get(d.isoformat(), []):
                events.append(ev)
        except Exception:
            pass
    return events


def econ_upcoming(d: date, days: int = 3) -> list[dict]:
    out = []
    for i in range(days + 1):
        di = d + timedelta(days=i)
        for ev in econ_events(di):
            out.append({**ev, "date": di.isoformat()})
    return out


# --------------------------------------------------------------- MarketData facade

class MarketData:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.alpaca = Alpaca(cfg) if cfg.has_alpaca else None
        # Feed health, established lazily by _ensure_feed() on the first call
        # that actually needs quotes (never in __init__ -- constructing a
        # MarketData must stay free, since tests and the paired arm-B thread
        # both build one). None = not probed yet.
        self.feed_ok: bool | None = None
        self.feed_downgraded = False

    # ---- feed preflight -------------------------------------------------
    def _preflight_feed(self) -> None:
        """One snapshot call on a known-liquid symbol to distinguish 'this feed
        is not entitled' from 'this symbol has no data'.

        Without it, an entitlement error (403 on a `sip` feed the deployed keys
        aren't subscribed to) fails EVERY ticker identically to a genuinely
        empty market: each caller catches broadly, logs one WARN, and the
        session becomes a no-trade with no durable trace. Sets self.feed_ok and
        logs loudly. Never raises -- a preflight failure must not become a new
        way for the morning loop to die.
        """
        if not self.alpaca:
            self.feed_ok = False
            return
        try:
            snap = self.alpaca.snapshots(["SPY"])
            self.feed_ok = bool(snap.get("SPY"))
            if not self.feed_ok:
                log("data", f"WARN feed '{self.alpaca.feed}' returned no data for SPY")
        except Exception as e:  # noqa: BLE001
            self.feed_ok = False
            log("data", f"WARN feed preflight FAILED for '{self.alpaca.feed}': {e}")

    def _ensure_feed(self) -> None:
        """Probe the configured feed once per instance, and fall back from
        `sip` to `iex` rather than losing the whole session to an entitlement
        error.

        Degraded data beats no data for a system whose only job is accumulating
        evidence -- but ONLY because the downgrade is recorded (feed_downgraded
        rides into the plan's data_health, so a later analysis can segregate
        IEX-sourced sessions). The downgrade is deliberately not sticky beyond
        this instance: every run re-probes sip, so a restored entitlement is
        picked up on its own instead of being cached away.
        """
        if self.feed_ok is not None or self.cfg.offline or not self.alpaca:
            return
        self._preflight_feed()
        if self.feed_ok or self.alpaca.feed == "iex":
            return
        original = self.alpaca.feed
        log("data", f"Feed '{original}' unusable — retrying preflight on 'iex'")
        self.alpaca.feed = "iex"
        self._preflight_feed()
        if self.feed_ok:
            self.feed_downgraded = True
            log("data", f"FEED DOWNGRADED: '{original}' -> 'iex' for this run "
                        f"(recorded as data_health.feed_downgraded; sessions sourced "
                        f"from IEX are not comparable to SIP sessions without saying so)")
        else:
            self.alpaca.feed = original
            log("data", f"WARN 'iex' preflight also failed — no usable quote feed "
                        f"this run; every price-grounding check will fail closed")

    # ---- morning bundle -------------------------------------------------
    def morning_bundle(self, d: date) -> dict:
        if self.cfg.offline:
            return self._offline_bundle(d)
        self._ensure_feed()

        bundle: dict[str, Any] = {"date": d.isoformat(), "generated_at": now_et().isoformat(), "missing": []}

        def safe(key: str, fn):
            try:
                bundle[key] = fn()
            except Exception as e:
                bundle[key] = None
                bundle["missing"].append(f"{key}: {type(e).__name__}: {e}")
                log("data", f"WARN {key} failed: {e}")

        safe("futures", lambda: yf_quote_change(FUTURES))
        safe("international", lambda: yf_quote_change(INTERNATIONAL))
        safe("vix", self._vix)
        safe("yesterday_recap", lambda: self._recap(prev_market_day(d)))
        safe("premarket_gappers", lambda: self._gappers())
        safe("movers", self._movers)
        safe("news", self._news)
        safe("catalyst_quotes", lambda: self._catalyst_quotes(bundle))
        safe("filings_8k", lambda: edgar_recent_8ks(self.cfg.data_cfg["edgar_user_agent"]))
        bundle["econ_today"] = econ_events(d)
        bundle["econ_upcoming"] = econ_upcoming(d + timedelta(days=1), 3)
        try:
            self._enrich_indicators((bundle.get("premarket_gappers") or [])
                                     + (bundle.get("catalyst_quotes") or []))
        except Exception as e:
            bundle["missing"].append(f"indicator_enrich: {type(e).__name__}: {e}")
            log("data", f"WARN indicator enrichment failed: {e}")
        return bundle

    def _vix(self) -> dict:
        closes = yf_history_closes(["^VIX"], days=12)["^VIX"]
        if not closes:
            raise RuntimeError("no VIX data")
        last = closes[-1]
        chg5 = (last - closes[-6]) / closes[-6] * 100 if len(closes) >= 6 else None
        return {"level": round(last, 2), "chg_5d_pct": round(chg5, 1) if chg5 is not None else None}

    def _recap(self, d: date) -> dict:
        syms = INDEX_ETFS + SECTOR_ETFS
        closes = yf_history_closes(syms, days=8)
        recap = {}
        for s in syms:
            c = closes.get(s, [])
            if len(c) >= 2:
                recap[s] = round((c[-1] - c[-2]) / c[-2] * 100, 2)
        return {"date": d.isoformat(), "pct_change": recap}

    def _gappers(self) -> list[dict]:
        if not self.alpaca:
            raise RuntimeError("Alpaca not configured — premarket gap scan unavailable")
        min_gap = float(self.cfg.data_cfg.get("gap_scan_min_pct", 3.0))
        base = set(SCAN_UNIVERSE) | set(INDEX_ETFS)

        # Movers/most-actives aren't restricted to SCAN_UNIVERSE, so folding
        # them in surfaces small-float/low-price gappers the fixed list
        # structurally can't see (dilution gaps, squeezes, etc).
        screener_symbols: set[str] = set()
        try:
            mv = self.alpaca.movers(int(self.cfg.data_cfg.get("movers_top", 18)))
            screener_symbols |= {m["symbol"] for m in mv.get("gainers", []) + mv.get("losers", []) if m.get("symbol")}
        except Exception as e:
            log("data", f"WARN movers fetch inside _gappers failed: {e}")
        try:
            act = self.alpaca.most_actives(int(self.cfg.data_cfg.get("actives_top", 15)))
            screener_symbols |= {m["symbol"] for m in act.get("most_actives", []) if m.get("symbol")}
        except Exception as e:
            log("data", f"WARN most_actives fetch inside _gappers failed: {e}")

        scan_symbols = base | screener_symbols
        snaps = self.alpaca.snapshots(list(scan_symbols))
        out = []
        for sym, s in snaps.items():
            if not isinstance(s, dict):
                continue
            try:
                prev_close = s["prevDailyBar"]["c"]
                last = (s.get("latestTrade") or {}).get("p") or (s.get("minuteBar") or {}).get("c")
                if not prev_close or not last:
                    continue
                gap = (last - prev_close) / prev_close * 100
                if sym in INDEX_ETFS or abs(gap) >= min_gap:
                    # Provenance tags (same pattern as in_scan_universe): tag,
                    # don't drop — the analyst and the shadow ledger both see
                    # these rows; exclusion decisions live in _validate_plan
                    # (leveraged_gate) and population.snapshot_universe.
                    out.append({"symbol": sym, "last": round(float(last), 2),
                                "gap_pct": round(gap, 2), "prev_close": round(float(prev_close), 2),
                                "in_scan_universe": sym in base,
                                "is_leveraged_product": sym in EXCLUDED_PRODUCTS,
                                "is_common_stock": sym in base or is_common_stock_symbol(sym)})
            except (KeyError, TypeError):
                continue
        band = self.cfg.data_cfg.get("gap_scan_price_band") or [None, None]
        lo, hi = (list(band) + [None, None])[:2]
        if lo is not None or hi is not None:
            # INDEX_ETFS are context, not candidates -- never filtered by price.
            out = [g for g in out if g["symbol"] in INDEX_ETFS
                   or ((lo is None or g["last"] >= lo) and (hi is None or g["last"] <= hi))]
        out.sort(key=lambda x: -abs(x["gap_pct"]))
        return out[:30]

    def _movers(self) -> dict | None:
        if not self.alpaca:
            raise RuntimeError("Alpaca not configured")
        top = int(self.cfg.data_cfg.get("movers_top", 18))
        mv = self.alpaca.movers(top)
        act = self.alpaca.most_actives(15)
        return {
            "gainers": [{"symbol": m["symbol"], "pct": round(m.get("percent_change", 0), 2),
                         "price": m.get("price")} for m in mv.get("gainers", [])],
            "losers": [{"symbol": m["symbol"], "pct": round(m.get("percent_change", 0), 2),
                        "price": m.get("price")} for m in mv.get("losers", [])],
            "most_active": [m["symbol"] for m in act.get("most_actives", [])],
        }

    def _news(self) -> list[dict]:
        if not self.alpaca:
            raise RuntimeError("Alpaca not configured")
        items = self.alpaca.news(int(self.cfg.data_cfg.get("news_limit", 40)))
        return [{"headline": n.get("headline", ""), "symbols": n.get("symbols", []),
                 "source": n.get("source", ""), "at": n.get("created_at", "")} for n in items]

    def _catalyst_quotes(self, bundle: dict) -> list[dict]:
        """Last price for every ticker named as a news-headline subject.

        News/8-K driven picks are frequently OUTSIDE the scan universe, so the
        gap scan and movers never quote them -- which left the analyst free to
        invent a price and _validate_plan's grounding gate with nothing to
        check against (real incident 2026-08-24: INSM Japan-approval pick
        planned at 72.5 while the stock traded ~125). One extra batched
        snapshot call closes that hole, and rendering these into the prompt
        anchors the analyst BEFORE it proposes levels. (EDGAR 8-K entries are
        company-name-keyed, not ticker-keyed, so they can't be quoted this
        way; Alpaca news symbols cover the wire stories that drive catalyst
        picks.)
        """
        if not self.alpaca:
            raise RuntimeError("Alpaca not configured — catalyst quotes unavailable")
        covered = {str(g.get("symbol", "")).upper() for g in (bundle.get("premarket_gappers") or [])}
        movers = bundle.get("movers") or {}
        covered |= {str(m.get("symbol", "")).upper()
                    for m in (movers.get("gainers") or []) + (movers.get("losers") or [])}
        syms: list[str] = []
        for n in bundle.get("news") or []:
            for s in n.get("symbols") or []:
                s = str(s).upper()
                # equities only: crypto pairs (BTCUSD) and malformed symbols are skipped
                if s and s.isalpha() and len(s) <= 5 and s not in covered and s not in syms:
                    syms.append(s)
        syms = syms[:60]
        if not syms:
            return []
        snaps = self.alpaca.snapshots(syms)
        out = []
        for sym in syms:
            s = snaps.get(sym)
            if not isinstance(s, dict):
                continue
            try:
                last = (s.get("latestTrade") or {}).get("p") or (s.get("minuteBar") or {}).get("c")
                prev_close = (s.get("prevDailyBar") or {}).get("c")
                if not last:
                    continue
                q: dict[str, Any] = {"symbol": sym, "last": round(float(last), 2)}
                if prev_close:
                    q["prev_close"] = round(float(prev_close), 2)
                    q["gap_pct"] = round((float(last) - float(prev_close)) / float(prev_close) * 100, 2)
                out.append(q)
            except (KeyError, TypeError, ZeroDivisionError):
                continue
        return out

    def pick_quotes(self, symbols: list[str], d: date) -> dict[str, float]:
        """Live last price for arbitrary proposed-pick tickers (one batched
        snapshot call). Used by the morning loop to ground tickers the bundle
        never quoted — web-search- or 8-K-sourced picks that no feed tagged
        (2026-08-25 DKS incident). Fail-closed contract: a symbol absent from
        the result means "could not verify" and the caller drops the pick, so
        this returns only quotes it actually obtained — no guesses.
        """
        # equities only, same convention as _catalyst_quotes
        syms: list[str] = []
        for s in symbols:
            s = str(s).upper().strip()
            if s and s.isalpha() and len(s) <= 5 and s not in syms:
                syms.append(s)
        if not syms:
            return {}
        if self.cfg.offline:
            # deterministic synthetic anchor, consistent with offline grading bars
            return {s: float(synthetic_minute_bars(s, d)[0]["o"]) for s in syms}
        if not self.alpaca:
            return {}
        # Arm B (and any caller that never built a bundle) reaches the quote
        # feed through here first, so probe from this entry point too.
        self._ensure_feed()
        snaps = self.alpaca.snapshots(syms)
        out: dict[str, float] = {}
        for sym in syms:
            s = snaps.get(sym)
            if not isinstance(s, dict):
                continue
            try:
                last = (s.get("latestTrade") or {}).get("p") or (s.get("minuteBar") or {}).get("c")
                if last:
                    out[sym] = round(float(last), 2)
            except (KeyError, TypeError):
                continue
        return out

    def adv20(self, symbols: list[str]) -> dict[str, float]:
        """20-day average daily volume per symbol, one batched fetch.

        Feeds v1 RVOL (agent.indicators.rvol_simple — percent of ADV20, not a
        true time-of-day curve; see specs/INDICATOR_LAYER_SPEC_v1.md §1 v2
        note). Alpaca's batched multi-symbol daily bars first, yfinance for
        whatever it missed. A symbol absent from the result means "couldn't
        compute ADV20" — callers treat that as None, never a fabricated ratio.
        """
        syms = sorted({str(s).upper().strip() for s in symbols if s})
        if not syms:
            return {}
        if self.cfg.offline:
            # deterministic synthetic ADV, consistent with offline grading bars
            return {s: float(2_000_000 + (int(hashlib.sha256(s.encode()).hexdigest(), 16) % 3_000_000))
                    for s in syms}
        out: dict[str, float] = {}
        end = now_et().date()
        start = end - timedelta(days=40)  # buffer for weekends/holidays -> >=20 trading days
        if self.alpaca:
            try:
                bars = self.alpaca.multi_daily_bars(syms, start, end)
                for sym, rows in bars.items():
                    vols = [r["volume"] for r in rows[-20:] if r.get("volume")]
                    if vols:
                        out[sym] = sum(vols) / len(vols)
            except Exception as e:
                log("data", f"WARN Alpaca multi_daily_bars failed for adv20: {e}")
        missing = [s for s in syms if s not in out]
        if missing:
            try:
                vol_hist = yf_history_volumes(missing, days=40)
                for sym, vols in vol_hist.items():
                    v20 = vols[-20:]
                    if v20:
                        out[sym] = sum(v20) / len(v20)
            except Exception as e:
                log("data", f"WARN yfinance adv20 fallback failed: {e}")
        return out

    def daily_context(self, symbols: list[str]) -> dict[str, dict]:
        """Widened ~260-trading-day daily-bar fetch (specs indicator layer
        v2 §2): one batched call derives adv20, atr14, atr_pct, daily_levels,
        and trend_snapshot together -- this REPLACES the standalone adv20()
        fetch inside bundle enrichment (one fetch replaces one fetch), though
        adv20() itself stays for grading-time use elsewhere.

        Alpaca's batched multi-symbol daily bars first (full OHLCV, needed
        for ATR/levels — unlike multi_daily_bars' close/volume-only shape),
        yfinance for whatever it missed. Any partial "today" bar is dropped
        before handing bars to agent.indicators — daily_levels/atr must only
        see COMPLETED sessions. Per-symbol fault isolation: a symbol whose
        bars can't be fetched/parsed gets an all-None context dict, never a
        crash and never a fabricated value.
        """
        syms = sorted({str(s).upper().strip() for s in symbols if s})
        out: dict[str, dict] = {}
        if not syms:
            return out

        def _null_ctx() -> dict:
            return {"adv20": None, "atr14": None, "atr_pct": None,
                    "daily_levels": ind.daily_levels([]), "trend_snapshot": ind.trend_snapshot([], None)}

        if self.cfg.offline:
            for s in syms:
                seed = int(hashlib.sha256(s.encode()).hexdigest(), 16)
                adv = 2_000_000 + (seed % 3_000_000)
                out[s] = {
                    "adv20": float(adv), "atr14": 2.5, "atr_pct": 2.1,
                    "daily_levels": {"pdh": 101.0, "pdl": 98.0, "pdc": 100.0,
                                     "hi_5d": 103.0, "lo_5d": 97.0,
                                     "hi_20d": 108.0, "lo_20d": 92.0,
                                     "hi_52w": 130.0, "lo_52w": 60.0},
                    "trend_snapshot": {"above_ma20": True, "above_ma50": True,
                                       "above_ma200": True, "ma20_slope_pct": 0.5,
                                       "ext_atr": 0.4},
                }
            return out

        end = now_et().date()
        start = end - timedelta(days=380)  # buffer for weekends/holidays -> ~260 trading days
        bars_by_sym: dict[str, list[dict]] = {}
        if self.alpaca:
            try:
                start_dt = datetime.combine(start, dtime(0, 0), tzinfo=ET)
                end_dt = datetime.combine(end, dtime(0, 0), tzinfo=ET)
                bars_by_sym = self.alpaca.multi_bars(syms, "1Day", start_dt, end_dt)
            except Exception as e:
                log("data", f"WARN Alpaca multi_bars(1Day) failed for daily_context: {e}")
        missing = [s for s in syms if not bars_by_sym.get(s)]
        if missing:
            try:
                bars_by_sym.update(yf_history_bars(missing, days=380))
            except Exception as e:
                log("data", f"WARN yfinance daily_context fallback failed: {e}")

        today = now_et().date()
        for s in syms:
            bars = [b for b in (bars_by_sym.get(s) or [])
                    if not (b.get("t") and b["t"].date() >= today)]
            if not bars:
                out[s] = _null_ctx()
                continue
            try:
                vols = [b["v"] for b in bars[-20:] if b.get("v")]
                adv20 = sum(vols) / len(vols) if vols else None
                atr14 = ind.atr(bars, 14)
                last_close = bars[-1]["c"]
                atr_pct = (atr14 / last_close * 100) if (atr14 and last_close) else None
                out[s] = {
                    "adv20": adv20, "atr14": atr14, "atr_pct": atr_pct,
                    "daily_levels": ind.daily_levels(bars),
                    "trend_snapshot": ind.trend_snapshot([b["c"] for b in bars], atr14),
                }
            except Exception as e:
                log("data", f"WARN daily_context computation failed for {s}: {e}")
                out[s] = _null_ctx()
        return out

    def fundamentals(self, symbols: list[str], con: Any = None) -> dict[str, dict]:
        """Float shares, market cap, and short-%-of-float per symbol —
        yfinance, cached in the ledger DB with a 3-session TTL (this data
        moves slowly; short interest itself is only exchange-reported
        roughly biweekly). short_asof always accompanies short_pct_float —
        stale-and-labeled beats fresh-looking fiction. A symbol this can't
        compute for is simply absent; callers treat that as unmeasured,
        never a fabricated number.
        """
        syms = sorted({str(s).upper().strip() for s in symbols if s})
        out: dict[str, dict] = {}
        if not syms:
            return out
        if self.cfg.offline:
            for s in syms:
                seed = int(hashlib.sha256(s.encode()).hexdigest(), 16)
                out[s] = {"mkt_cap": float(1_000_000_000 + seed % 400_000_000_000),
                          "float_shares": float(10_000_000 + seed % 500_000_000),
                          "short_pct_float": round((seed % 300) / 10, 1),
                          "short_asof": now_et().date().isoformat()}
            return out
        from . import ledger
        owns_con = con is None
        if owns_con:
            con = ledger.connect()
        try:
            out.update(ledger.cached_fundamentals(con, syms))
            missing = [s for s in syms if s not in out]
            if not missing:
                return out
            yf = _yf()

            def _fetch_one(s: str) -> dict | None:
                try:
                    t = yf.Ticker(s)
                    info = t.get_info() if hasattr(t, "get_info") else t.info
                    mkt_cap = info.get("marketCap")
                    float_shares = info.get("floatShares")
                    short_pct = info.get("shortPercentOfFloat")
                    short_ts = info.get("dateShortInterest")
                    asof = None
                    if short_ts:
                        try:
                            asof = datetime.fromtimestamp(short_ts).date().isoformat()
                        except Exception:
                            asof = None
                    return {"symbol": s, "mkt_cap": float(mkt_cap) if mkt_cap else None,
                            "float_shares": float(float_shares) if float_shares else None,
                            "short_pct_float": round(float(short_pct) * 100, 2) if short_pct else None,
                            "short_asof": asof}
                except Exception as e:
                    log("data", f"WARN fundamentals fetch failed for {s}: {e}")
                    return None

            rows: list[dict] = []
            fetched = _bounded_map(missing, _fetch_one)
            for s, row in fetched.items():
                out[s] = {k: v for k, v in row.items() if k != "symbol"}
                rows.append(row)
            if rows:
                ledger.save_fundamentals(con, rows)
        finally:
            if owns_con:
                con.close()
        return out

    def next_earnings(self, symbols: list[str]) -> dict[str, int | None]:
        """Trading days until next earnings report per symbol (0 = reports
        today after close), from yfinance's earnings calendar. Known-
        imperfect source (spec §9: week 1 measures its null/error rate before
        any gate acts on it hard). Fetched only for the symbols handed in
        (card-rendered tickers), never the whole scan universe. A symbol
        this can't determine a date for is simply absent.
        """
        # Structural instruments (warrants/units/rights) have no earnings by
        # construction and yfinance errors one-by-one on every lookup — the
        # 2026-08/09 production error flood (GFAIW, GIPRW, PSQH.WS, ...) was
        # exactly this. Skip them like the ETFs above.
        syms = sorted({str(s).upper().strip() for s in symbols if s} - _NO_EARNINGS_SYMBOLS)
        syms = [s for s in syms if is_common_stock_symbol(s)]
        out: dict[str, int | None] = {}
        if not syms:
            return out
        if self.cfg.offline:
            for s in syms:
                seed = int(hashlib.sha256(s.encode()).hexdigest(), 16)
                out[s] = seed % 45
            return out
        yf = _yf()
        today = now_et().date()

        def _fetch_one(s: str) -> int | None:
            try:
                cal = yf.Ticker(s).get_earnings_dates(limit=4)
                if cal is None or cal.empty:
                    return None
                future = [ts.date() for ts in cal.index if ts.date() >= today]
                if not future:
                    return None
                return (min(future) - today).days
            except Exception as e:
                log("data", f"WARN next_earnings fetch failed for {s}: {e}")
                return None

        out.update(_bounded_map(syms, _fetch_one))
        return out

    def _enrich_indicators(self, quotes: list[dict]) -> None:
        """Attach premarket VWAP/RVOL (v1) and ATR/levels/trend/fundamentals/
        earnings (indicator layer v2) fields to gap-scan/catalyst-quote rows,
        IN PLACE: pm_vwap, vs_pm_vwap, pm_vol_pct_adv20, adv20, atr14,
        atr_pct, gap_atr, pdh, pdl, pm_high, pm_low, hi_20d, lo_20d, hi_52w,
        lo_52w, above_ma20/50/200, ext_atr, mkt_cap, float_shares,
        short_pct_float, short_asof, earnings_days_away.

        adv20 now comes from ONE widened daily_context fetch (replaces the
        old standalone adv20() call here — same one-fetch-per-round-trip
        discipline) and the premarket-minute-bars fetch is reused for BOTH
        pmVWAP/RVOL (v1) and premarket high/low (v2) — zero additional
        network for PM levels. Fundamentals/earnings are separate fetches,
        gated independently via `context_enrich` (default on) so v1
        pmVWAP/RVOL keeps working even when the v2 pieces are off or fail.
        Individually fault-tolerant per ticker and per field group: nothing
        here fabricates a value or crashes the morning run (spec §0.4/§10).
        """
        if not quotes or not self.alpaca:
            return
        indicator_on = self.cfg.data_cfg.get("indicator_enrich", True)
        context_on = self.cfg.data_cfg.get("context_enrich", True)
        if not indicator_on and not context_on:
            return
        syms = [q["symbol"] for q in quotes if q.get("symbol")]
        if not syms:
            return

        try:
            ctx = self.daily_context(syms)
        except Exception as e:
            log("data", f"WARN daily_context fetch failed during enrichment: {e}")
            ctx = {}
        try:
            start = datetime.combine(now_et().date(), dtime(4, 0), tzinfo=ET)
            pm_bars = self.alpaca.multi_bars(syms, "1Min", start, now_et())
        except Exception as e:
            log("data", f"WARN premarket bars fetch failed during enrichment: {e}")
            pm_bars = {}

        fund: dict[str, dict] = {}
        earn: dict[str, int | None] = {}
        if context_on:
            try:
                fund = self.fundamentals(syms)
            except Exception as e:
                log("data", f"WARN fundamentals fetch failed during context enrichment: {e}")
            try:
                earn = self.next_earnings(syms)
            except Exception as e:
                log("data", f"WARN next_earnings fetch failed during context enrichment: {e}")

        for q in quotes:
            sym = q.get("symbol")
            c = ctx.get(sym) or {}
            a = c.get("adv20")
            bars = pm_bars.get(sym) or []

            if indicator_on:
                q["adv20"] = round(a) if a else None
                if bars:
                    try:
                        last_vwap = vwap_series(bars)[-1]
                        q["pm_vwap"] = round(last_vwap, 4)
                        q["vs_pm_vwap"] = round(vwap_distance(float(q.get("last", 0)), last_vwap), 2)
                    except Exception:
                        q["pm_vwap"] = None
                        q["vs_pm_vwap"] = None
                    cum_vol = sum(b.get("v") or 0 for b in bars)
                    rv = rvol_simple(cum_vol, a)
                    q["pm_vol_pct_adv20"] = round(rv * 100, 1) if rv is not None else None
                    q["pm_volume_status"] = "measured" if rv is not None else "adv20_unavailable"
                else:
                    q["pm_vwap"] = None
                    q["vs_pm_vwap"] = None
                    q["pm_vol_pct_adv20"] = None
                    q["pm_volume_status"] = (
                        "batch_unavailable" if not pm_bars else "symbol_unavailable")

            if not context_on:
                continue

            q["atr14"] = round(c["atr14"], 4) if c.get("atr14") else None
            q["atr_pct"] = round(c["atr_pct"], 2) if c.get("atr_pct") else None
            levels = c.get("daily_levels") or {}
            for k in ("pdh", "pdl", "hi_20d", "lo_20d", "hi_52w", "lo_52w"):
                q[k] = levels.get(k)
            trend = c.get("trend_snapshot") or {}
            q["above_ma20"] = trend.get("above_ma20")
            q["above_ma50"] = trend.get("above_ma50")
            q["above_ma200"] = trend.get("above_ma200")
            q["ext_atr"] = round(trend["ext_atr"], 2) if trend.get("ext_atr") is not None else None
            try:
                pml = ind.premarket_levels(bars)
            except Exception:
                pml = {"pm_high": None, "pm_low": None}
            q["pm_high"], q["pm_low"] = pml["pm_high"], pml["pm_low"]
            if q.get("atr14") and q.get("gap_pct") is not None and q.get("prev_close"):
                try:
                    gap_abs = abs(float(q["prev_close"])) * abs(float(q["gap_pct"])) / 100.0
                    q["gap_atr"] = round(gap_abs / q["atr14"], 2)
                except Exception:
                    q["gap_atr"] = None
            else:
                q["gap_atr"] = None
            f = fund.get(sym) or {}
            q["mkt_cap"] = f.get("mkt_cap")
            q["float_shares"] = f.get("float_shares")
            q["short_pct_float"] = f.get("short_pct_float")
            q["short_asof"] = f.get("short_asof")
            q["earnings_days_away"] = earn.get(sym)

    # ---- grading data ----------------------------------------------------
    def minute_bars(self, symbol: str, d: date) -> list[dict]:
        if self.cfg.offline:
            return synthetic_minute_bars(symbol, d)
        if self.alpaca:
            try:
                bars = self.alpaca.minute_bars(symbol, d)
                if bars:
                    return bars
            except Exception as e:
                log("data", f"Alpaca minute bars failed for {symbol}: {e}; falling back to yfinance")
        return yf_minute_bars(symbol, d)

    def daily_bar(self, symbol: str, d: date) -> dict | None:
        if self.cfg.offline:
            bars = synthetic_minute_bars(symbol, d)
            if not bars:
                return None
            return {"open": bars[0]["o"], "high": max(b["h"] for b in bars),
                    "low": min(b["l"] for b in bars), "close": bars[-1]["c"],
                    "volume": sum(b["v"] for b in bars)}
        if self.alpaca:
            try:
                bar = self.alpaca.daily_bar(symbol, d)
                if bar:
                    return bar
            except Exception:
                pass
        return yf_daily_bar(symbol, d)

    def market_recap(self, d: date) -> dict:
        if self.cfg.offline:
            return {"date": d.isoformat(), "pct_change": {"SPY": 0.42, "QQQ": 0.66, "IWM": -0.31}}
        syms = INDEX_ETFS + SECTOR_ETFS + ["^VIX"]
        recap = {}
        for s in syms:
            try:
                bar = yf_daily_bar(s, d)
                prev = yf_daily_bar(s, prev_market_day(d))
                if bar and prev and prev["close"]:
                    recap[s] = round((bar["close"] - prev["close"]) / prev["close"] * 100, 2)
            except Exception:
                continue
        return {"date": d.isoformat(), "pct_change": recap}

    # ---- offline fixtures -------------------------------------------------
    def _offline_bundle(self, d: date) -> dict:
        path = MOCKS_DIR / "data_bundle.json"
        bundle = json.loads(path.read_text(encoding="utf-8"))
        bundle["date"] = d.isoformat()
        bundle["generated_at"] = now_et().isoformat()
        bundle.setdefault("missing", []).append("OFFLINE MODE — all data is canned fixture data for pipeline testing")
        bundle["econ_today"] = econ_events(d)
        bundle["econ_upcoming"] = econ_upcoming(d + timedelta(days=1), 3)
        return bundle


# -------------------------------------------------- synthetic bars (offline test)

def synthetic_minute_bars(symbol: str, d: date) -> list[dict]:
    """
    Deterministic synthetic session for offline pipeline tests.
    Scenario per ticker (stable hash): 0 = trigger->target, 1 = trigger->stop, 2 = no trigger.
    Prices are anchored near 100 so offline picks should use ~100-level entries.
    """
    seed = int(hashlib.sha256(symbol.encode()).hexdigest(), 16)
    scenario = seed % 3
    base = 100.0
    bars = []
    t = datetime.combine(d, dtime(9, 30), tzinfo=ET)
    price = base * 1.01  # opens slightly above
    for i in range(390):
        if scenario == 0:  # dip to trigger, then rally through target
            drift = -0.05 if i < 30 else 0.03
        elif scenario == 1:  # dip to trigger, keep bleeding to stop
            drift = -0.03 if i < 200 else -0.005
        else:  # runs away without pulling back
            drift = 0.02
        wiggle = (((seed >> (i % 32)) & 3) - 1.5) * 0.05
        new_price = max(1.0, price + drift + wiggle)
        o, c = price, new_price
        h = max(o, c) + 0.08
        l = min(o, c) - 0.08
        bars.append({"t": t, "o": round(o, 2), "h": round(h, 2),
                     "l": round(l, 2), "c": round(c, 2), "v": 10_000 + (seed >> (i % 16)) % 5000})
        price = new_price
        t += timedelta(minutes=1)
    return bars


# ---------------------------------------------------------- bundle -> markdown

def _indicator_suffix(q: dict) -> str:
    """Compact 'vs pmVWAP / pm vol %ADV20' suffix for one gap-scan or
    catalyst-quote row. Null fields render explicitly as 'n/a' — the model
    must know what it's flying without, same as the bundle's 'missing' list,
    never silently omitted."""
    vwap_bit = f"{q['vs_pm_vwap']:+.1f}% vs pmVWAP" if q.get("vs_pm_vwap") is not None else "pmVWAP n/a"
    if q.get("pm_vol_pct_adv20") is not None:
        vol_bit = f"pm vol {q['pm_vol_pct_adv20']:.0f}% ADV20"
    else:
        status = q.get("pm_volume_status")
        vol_bit = f"pm vol n/a ({status})" if status else "pm vol n/a"
    return f"{vwap_bit}, {vol_bit}"


def _context_lines(q: dict) -> str:
    """Extra indented lines for one ticker card (indicator layer v2 §2):
    ATR/gap-in-ATRs, PDH/PDL/PMH/PML, 52w-high distance, trend/extension,
    market cap, earnings, and — only when it clears the attention bar
    (rule 5: no decoration fields) — float/short. Null fields render
    explicitly as 'n/a', never silently omitted. Empty string when NONE of
    this context was computable for the ticker (context_enrich off, or every
    fetch failed) — no half-populated card with nothing but 'n/a' noise."""
    if q.get("atr_pct") is None and q.get("pdh") is None and q.get("mkt_cap") is None:
        return ""
    atr_bit = f"ATR {q['atr_pct']:.1f}%/d" if q.get("atr_pct") is not None else "ATR n/a"
    gap_bit = f"gap {q['gap_atr']:.1f} ATR" if q.get("gap_atr") is not None else "gap n/a"
    levels = []
    if q.get("pdh") is not None:
        levels.append(f"PDH {q['pdh']:.2f} PDL {q['pdl']:.2f}")
    if q.get("pm_high") is not None:
        levels.append(f"PMH {q['pm_high']:.2f} PML {q['pm_low']:.2f}")
    last = q.get("last")
    if q.get("hi_52w") is not None and last:
        dist = (last - q["hi_52w"]) / q["hi_52w"] * 100
        levels.append(f"52wH {dist:+.1f}%")
    trend_bits = []
    for label, key in (("20d", "above_ma20"), ("50d", "above_ma50"), ("200d", "above_ma200")):
        v = q.get(key)
        trend_bits.append(("^" if v else ("v" if v is False else "?")) + label)
    ext_bit = f"ext {q['ext_atr']:+.1f} ATR" if q.get("ext_atr") is not None else "ext n/a"
    if q.get("mkt_cap"):
        mc = q["mkt_cap"]
        mcap_bit = f"mcap ${mc / 1e12:.1f}T" if mc >= 1e12 else f"mcap ${mc / 1e9:.1f}B"
    else:
        mcap_bit = "mcap n/a"
    earn = q.get("earnings_days_away")
    earn_bit = f"earnings in {earn}d" if earn is not None else "earnings n/a"
    line1 = f"{atr_bit}, {gap_bit}" + (" | " + " | ".join(levels) if levels else "")
    line2 = f"trend {' '.join(trend_bits)}, {ext_bit} | {mcap_bit} | {earn_bit}"
    out_lines = [line1, line2]
    short_pct, float_sh = q.get("short_pct_float"), q.get("float_shares")
    if (short_pct is not None and short_pct >= 5) or (float_sh is not None and float_sh <= 100_000_000):
        float_bit = f"float {float_sh / 1e6:.0f}M" if float_sh else "float n/a"
        short_bit = (f"short {short_pct:.0f}% of float (as of {q.get('short_asof') or '?'})"
                    if short_pct is not None else "short n/a")
        out_lines.append(f"{float_bit}, {short_bit}")
    return "\n  " + "\n  ".join(out_lines)


def render_bundle_md(b: dict) -> str:
    """Render the morning data bundle as compact markdown for the prompt."""
    parts: list[str] = [f"### Data as of {b.get('generated_at', '?')} ET\n"]

    def quote_lines(items, title):
        if not items:
            return
        parts.append(f"**{title}:** " + " | ".join(
            f"{q['label'] if 'label' in q else q['symbol']} {q['pct']:+.2f}%" for q in items if q.get("pct") is not None))

    quote_lines(b.get("futures"), "Overnight futures")
    quote_lines(b.get("international"), "International")

    vix = b.get("vix")
    if vix:
        parts.append(f"**VIX:** {vix['level']} ({vix.get('chg_5d_pct', 'n/a')}% 5-day change)")

    recap = b.get("yesterday_recap")
    if recap and recap.get("pct_change"):
        pc = recap["pct_change"]
        idx = " | ".join(f"{s} {pc[s]:+.2f}%" for s in ("SPY", "QQQ", "IWM") if s in pc)
        sectors = sorted(((s, v) for s, v in pc.items() if s not in ("SPY", "QQQ", "IWM")),
                         key=lambda x: -x[1])
        sec = " | ".join(f"{s} {v:+.1f}%" for s, v in sectors)
        parts.append(f"**Yesterday ({recap['date']}):** {idx}\n**Sectors:** {sec}")

    gappers = b.get("premarket_gappers")
    if gappers:
        parts.append("**Premarket gap scan (curated universe + live movers/actives):**")
        for g in gappers[:25]:
            tag = "" if g.get("in_scan_universe", True) else " [off-universe]"
            if g.get("is_leveraged_product"):
                tag += " [leveraged]"
            if g.get("is_common_stock") is False:
                tag += " [warrant/unit]"
            parts.append(f"- {g['symbol']}{tag}: {g['gap_pct']:+.2f}% (last {g['last']}, prev close {g['prev_close']}) "
                        f"| {_indicator_suffix(g)}{_context_lines(g)}")

    movers = b.get("movers")
    if movers:
        gain = ", ".join(f"{m['symbol']} {m['pct']:+.1f}%" for m in movers.get("gainers", [])[:12])
        lose = ", ".join(f"{m['symbol']} {m['pct']:+.1f}%" for m in movers.get("losers", [])[:12])
        parts.append(f"**Movers — gainers:** {gain}\n**Movers — losers:** {lose}")
        if movers.get("most_active"):
            parts.append("**Most active:** " + ", ".join(movers["most_active"][:15]))

    news = b.get("news")
    if news:
        parts.append("**Headlines (newest first):**")
        for n in news[:30]:
            syms = f" [{','.join(n['symbols'][:4])}]" if n.get("symbols") else ""
            parts.append(f"- {n['headline']}{syms} ({n.get('source','')})")

    cq = b.get("catalyst_quotes")
    if cq:
        parts.append("**Live last prices for headline tickers (anchor ALL entry/stop/target levels "
                     "on these or on scan/mover quotes above — never on remembered prices):** " +
                     " | ".join(
                         f"{q['symbol']} {q['last']}"
                         + (f" ({q['gap_pct']:+.1f}%)" if q.get("gap_pct") is not None else "")
                         + f" [{_indicator_suffix(q)}]"
                         for q in cq[:40]))

    filings = b.get("filings_8k")
    if filings:
        parts.append("**Fresh 8-K filings (SEC EDGAR):**")
        for f in filings[:15]:
            parts.append(f"- {f['title']}")

    if b.get("econ_today"):
        parts.append("**Econ calendar TODAY:**")
        for ev in b["econ_today"]:
            parts.append(f"- {ev.get('time','')} {ev['name']} ({ev.get('importance','')})")
    else:
        parts.append("**Econ calendar TODAY:** nothing on the rule-based calendar "
                      "(verify via web search — CPI/PPI/earnings are not in the rule-based feed)")

    if b.get("econ_upcoming"):
        parts.append("**Upcoming (next 3 days):** " + "; ".join(
            f"{e['date']} {e['name']}" for e in b["econ_upcoming"][:8]))

    if b.get("missing"):
        parts.append("**DATA SOURCES UNAVAILABLE THIS MORNING (factor this in):**")
        for m in b["missing"]:
            parts.append(f"- {m}")

    return "\n".join(parts)
