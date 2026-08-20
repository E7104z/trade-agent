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
import xml.etree.ElementTree as ET_XML
from datetime import date, datetime, time as dtime, timedelta
from typing import Any

import requests

from .config import ECON_OVERRIDES_PATH, MOCKS_DIR, Config
from .util import ET, is_market_day, log, now_et, prev_market_day

ALPACA_DATA = "https://data.alpaca.markets"

FUTURES = {"ES=F": "S&P 500 fut", "NQ=F": "Nasdaq fut", "RTY=F": "Russell fut",
           "CL=F": "Crude oil", "GC=F": "Gold", "DX-Y.NYB": "Dollar index", "^TNX": "10Y yield"}
INTERNATIONAL = {"^N225": "Nikkei", "^HSI": "Hang Seng", "^GDAXI": "DAX", "^FTSE": "FTSE"}
SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLC", "XLRE"]
INDEX_ETFS = ["SPY", "QQQ", "IWM"]

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


# ------------------------------------------------------------------ Alpaca API

class Alpaca:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.feed = cfg.data_cfg.get("alpaca_feed", "iex")
        self.h = {
            "APCA-API-KEY-ID": cfg.alpaca_key_id,
            "APCA-API-SECRET-KEY": cfg.alpaca_secret_key,
        }

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = requests.get(f"{ALPACA_DATA}{path}", headers=self.h, params=params or {}, timeout=20)
        r.raise_for_status()
        return r.json()

    def snapshots(self, symbols: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(symbols), 100):
            batch = symbols[i : i + 100]
            data = self._get("/v2/stocks/snapshots",
                             {"symbols": ",".join(batch), "feed": self.feed})
            out.update(data if isinstance(data, dict) else {})
        out.pop("next_page_token", None)
        return out

    def movers(self, top: int = 18) -> dict:
        return self._get("/v1beta1/screener/stocks/movers", {"top": top})

    def most_actives(self, top: int = 15) -> dict:
        return self._get("/v1beta1/screener/stocks/most-actives", {"by": "volume", "top": top})

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

    # ---- morning bundle -------------------------------------------------
    def morning_bundle(self, d: date) -> dict:
        if self.cfg.offline:
            return self._offline_bundle(d)

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
        safe("filings_8k", lambda: edgar_recent_8ks(self.cfg.data_cfg["edgar_user_agent"]))
        bundle["econ_today"] = econ_events(d)
        bundle["econ_upcoming"] = econ_upcoming(d + timedelta(days=1), 3)
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
        snaps = self.alpaca.snapshots(SCAN_UNIVERSE + INDEX_ETFS)
        min_gap = float(self.cfg.data_cfg.get("gap_scan_min_pct", 3.0))
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
                    out.append({"symbol": sym, "last": round(float(last), 2),
                                "gap_pct": round(gap, 2), "prev_close": round(float(prev_close), 2)})
            except (KeyError, TypeError):
                continue
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
        parts.append("**Premarket gap scan (liquid universe):**")
        for g in gappers[:25]:
            parts.append(f"- {g['symbol']}: {g['gap_pct']:+.2f}% (last {g['last']}, prev close {g['prev_close']})")

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
