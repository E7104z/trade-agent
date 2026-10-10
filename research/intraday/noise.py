"""The noise-area intraday momentum rule (S4b) as a parametrized engine over cached ETF days.

Registered form: lookback 14, checks every 30 min from 10:00, trail = max(boundary, VWAP),
sigma multiplier 1.0, entry/exit at the next bar's open, flat at 15:55.
"""
import math
from collections import defaultdict
from datetime import date

import numpy as np

from lab import EXIT_BAR, trade, vwap


def run(e5, sym, lookback=14, step=6, trail="max", mult=1.0, first=5, start=None, end=None,
        sides=(1, -1)):
    S = e5[sym]
    days, bars, pcs, atrs = S["days"], S["bars"], S["pc"], S["atr"]
    out = []      # (date, side, gross, cost)
    for i in range(lookback, len(days)):
        d = days[i]
        if (start and d < start) or (end and d > end):
            continue
        B = bars[i]
        if B[0, 0] != B[0, 0]:
            continue
        hist = bars[i - lookback:i]
        sigma = np.nanmean(np.abs(hist[:, :, 3] / hist[:, :1, 0] - 1), axis=0) * mult
        o, pc, atr = B[0, 0], pcs[i], atrs[i]
        if not (atr > 0 and pc > 0):
            continue
        ub, lb = max(o, pc) * (1 + sigma), min(o, pc) * (1 - sigma)
        vw = vwap(B)
        unit = 0.25 * atr
        pos, e = 0, None
        for k in range(first, 72, step):
            px, nxt = B[k, 3], B[k + 1, 0]
            if px != px or nxt != nxt:
                continue
            if pos:
                lvl_up = {"max": max(ub[k], vw[k]), "band": ub[k], "vwap": vw[k]}[trail]
                lvl_dn = {"max": min(lb[k], vw[k]), "band": lb[k], "vwap": vw[k]}[trail]
                if (pos > 0 and px < lvl_up) or (pos < 0 and px > lvl_dn):
                    out.append((d, pos, *trade(pos, e, e - pos * unit, nxt, etf=True)))
                    pos = 0
            if pos == 0 and k < 71:
                if px > ub[k] and 1 in sides:
                    pos, e = 1, nxt
                elif px < lb[k] and -1 in sides:
                    pos, e = -1, nxt
        if pos:
            x = B[EXIT_BAR, 0]
            if x != x:                       # no 15:55 print: last close before it
                prev = B[:EXIT_BAR, 3]
                prev = prev[~np.isnan(prev)]
                x = prev[-1] if len(prev) else e
            out.append((d, pos, *trade(pos, e, e - pos * unit, x, etf=True)))
    return out


def summary(tr, n_sessions=None):
    """Per-trade net mean, total, and t of the DAILY SUM (what a trader earns), plus years."""
    if not tr:
        return "no trades"
    net = np.array([g - c for _, _, g, c in tr])
    by_d = defaultdict(float)
    for d, _, g, c in tr:
        by_d[d] += g - c
    daily = np.array(list(by_d.values()))
    if n_sessions:                         # count flat days as zero days
        daily = np.concatenate([daily, np.zeros(max(0, n_sessions - len(daily)))])
    t = daily.mean() / (daily.std(ddof=1) / math.sqrt(len(daily)))
    yrs = defaultdict(float)
    for d, _, g, c in tr:
        yrs[d.year] += g - c
    longs = np.mean([g - c for _, s, g, c in tr if s > 0]) if any(s > 0 for _, s, _, _ in tr) else float("nan")
    shorts = np.mean([g - c for _, s, g, c in tr if s < 0]) if any(s < 0 for _, s, _, _ in tr) else float("nan")
    ys = " ".join(f"{y}:{v:+.0f}" for y, v in sorted(yrs.items()))
    return (f"n {len(tr):4d} per-trade {net.mean():+.3f} total {net.sum():+6.1f}R t_daily {t:5.2f} "
            f"long {longs:+.3f} short {shorts:+.3f} | {ys}")
