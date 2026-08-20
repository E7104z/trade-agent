"""
Market regime engine — a rolling, quantified read on current conditions.

Computed from daily closes (one batched yfinance download):
  trend:  SPY vs 20d/50d MAs + MA slope
  risk:   VIX level/momentum + sector breadth
  extras: realized vol, IWM/SPY relative strength

The agent learns which of its setups work in which regime — that cross-conditional
pattern lives in the ledger and gets mined weekly.
"""
from __future__ import annotations

import math
from datetime import date

from .config import Config
from .providers import SECTOR_ETFS, yf_history_closes
from .util import log


def _sma(vals: list[float], n: int) -> float | None:
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def compute_regime(cfg: Config, d: date) -> dict:
    if cfg.offline:
        return {
            "date": d, "trend": "uptrend", "risk": "risk_on", "vix": 14.2,
            "vix_5d_chg_pct": -4.1, "spy_vs_20d_pct": 1.8, "spy_vs_50d_pct": 3.5,
            "sectors_above_20d": 9, "iwm_spy_5d_rel_pct": 0.6, "realized_vol_10d": 9.8,
            "tag": "uptrend/risk_on/lowvol", "notes": "OFFLINE fixture regime",
        }

    symbols = ["SPY", "IWM", "^VIX"] + SECTOR_ETFS
    closes = yf_history_closes(symbols, days=80)

    spy = closes.get("SPY", [])
    vix = closes.get("^VIX", [])
    iwm = closes.get("IWM", [])
    if len(spy) < 50 or not vix:
        log("regime", "WARN insufficient history; emitting neutral regime")
        return {"date": d, "trend": "unknown", "risk": "neutral", "tag": "unknown", "notes": "insufficient data"}

    last = spy[-1]
    sma20, sma50 = _sma(spy, 20), _sma(spy, 50)
    spy20 = (last - sma20) / sma20 * 100 if sma20 else 0.0
    spy50 = (last - sma50) / sma50 * 100 if sma50 else 0.0
    sma20_prev = _sma(spy[:-5], 20)
    slope = ((sma20 - sma20_prev) / sma20_prev * 100) if (sma20 and sma20_prev) else 0.0

    # realized vol (10d, annualized %)
    rets = [(spy[i] / spy[i - 1] - 1) for i in range(max(1, len(spy) - 10), len(spy))]
    rvol = (sum(r * r for r in rets) / len(rets)) ** 0.5 * math.sqrt(252) * 100 if rets else 0.0

    vix_last = vix[-1]
    vix_5d = (vix_last - vix[-6]) / vix[-6] * 100 if len(vix) >= 6 else 0.0

    breadth = 0
    for s in SECTOR_ETFS:
        c = closes.get(s, [])
        m = _sma(c, 20)
        if c and m and c[-1] > m:
            breadth += 1

    iwm_rel = 0.0
    if len(iwm) >= 6 and len(spy) >= 6:
        iwm_rel = (iwm[-1] / iwm[-6] - spy[-1] / spy[-6]) * 100

    # classify
    if spy20 > 0.3 and slope > 0:
        trend = "uptrend"
    elif spy20 < -0.3 and slope < 0:
        trend = "downtrend"
    else:
        trend = "chop"

    if vix_last < 17 and breadth >= 7:
        risk = "risk_on"
    elif vix_last > 24 or breadth <= 3:
        risk = "risk_off"
    else:
        risk = "neutral"

    vol_label = "lowvol" if vix_last < 15 else ("highvol" if vix_last > 22 else "midvol")
    tag = f"{trend}/{risk}/{vol_label}"

    return {
        "date": d,
        "trend": trend,
        "risk": risk,
        "vix": round(vix_last, 2),
        "vix_5d_chg_pct": round(vix_5d, 1),
        "spy_vs_20d_pct": round(spy20, 2),
        "spy_vs_50d_pct": round(spy50, 2),
        "sectors_above_20d": breadth,
        "iwm_spy_5d_rel_pct": round(iwm_rel, 2),
        "realized_vol_10d": round(rvol, 1),
        "tag": tag,
        "notes": "",
    }


def regime_md(r: dict) -> str:
    if r.get("trend") == "unknown":
        return "Regime: UNKNOWN (insufficient data) — treat as elevated uncertainty."
    return (
        f"**Regime tag:** `{r['tag']}`\n"
        f"- Trend: {r['trend']} (SPY {r['spy_vs_20d_pct']:+.1f}% vs 20d MA, {r['spy_vs_50d_pct']:+.1f}% vs 50d)\n"
        f"- Risk appetite: {r['risk']} (VIX {r['vix']}, {r['vix_5d_chg_pct']:+.1f}% over 5d; "
        f"breadth {r['sectors_above_20d']}/11 sectors above 20d MA)\n"
        f"- Realized vol (10d, annualized): {r['realized_vol_10d']}%\n"
        f"- Small-cap relative strength (IWM-SPY, 5d): {r['iwm_spy_5d_rel_pct']:+.1f}%"
    )
