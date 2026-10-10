"""Per-session context for the intraday search, streamed one day at a time (memory-light).

For each session and each stock liquid the day before, it computes what was knowable by 09:35:
prior close, 20-day dollar volume, today's open, ATR14, yesterday's return and volume
multiple, and RVOL5. RVOL5 is the first five-minute bar's volume over its own mean for the
prior 14 sessions.
"""
from __future__ import annotations

import pickle
from collections import deque
from datetime import date

import numpy as np

from fetch import ETFS, LIQ_ADV, LIQ_PRICE, STORE, TOP_ADV, TOP_RVOL, GAP_INPLAY, load_open15

FIELDS = ("pc", "adv", "o", "atr", "pret", "pdvr", "rv")


class Daily:
    def __init__(self, daily: dict):
        self.syms = []
        self.pos = []      # {date ordinal: row}
        self.arr = []      # float32 [n, 6]: pc, adv, o, atr, pret, pdvr
        for s, a in daily["bars"].items():
            if len(a) < 23:
                continue
            o, h, l, c, v = a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5]
            n = len(a)
            tr = np.empty(n)
            tr[0] = h[0] - l[0]
            tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
            dv = c * v
            cs_tr = np.concatenate([[0], np.cumsum(tr)])
            cs_dv = np.concatenate([[0], np.cumsum(dv)])
            out = np.full((n, 6), np.nan, dtype=np.float32)
            i = np.arange(22, n)
            out[i, 0] = c[i - 1]
            out[i, 1] = (cs_dv[i] - cs_dv[i - 20]) / 20            # mean dollar volume, sessions i-20..i-1
            out[i, 2] = o[i]
            out[i, 3] = (cs_tr[i] - cs_tr[i - 14]) / 14            # ATR14 through i-1
            out[i, 4] = (c[i - 1] / c[i - 2] - 1) * 100
            prev_adv = (cs_dv[i - 1] - cs_dv[i - 21]) / 20
            out[i, 5] = np.where(prev_adv > 0, dv[i - 1] / np.where(prev_adv > 0, prev_adv, 1), np.nan)
            self.syms.append(s)
            self.pos.append({int(x): k for k, x in enumerate(a[:, 0])})
            self.arr.append(out)

    def day(self, d: date) -> dict:
        """{symbol: [pc, adv, o, atr, pret, pdvr]} for symbols liquid the day before."""
        od = d.toordinal()
        res = {}
        for s, pos, arr in zip(self.syms, self.pos, self.arr):
            k = pos.get(od)
            if k is None or k < 22:
                continue
            row = arr[k]
            if s in ETFS or (row[0] >= LIQ_PRICE and row[1] >= LIQ_ADV):
                res[s] = row
        return res


def stream(daily: dict, days: list[date]):
    """Yields (d, {symbol: dict(FIELDS)}) in date order; rv is None when unknown."""
    D = Daily(daily)
    hist: dict[str, deque] = {}
    for d in days:
        base = D.day(d)
        got = load_open15(d)
        first = {}
        if got is not None:
            for s, b in zip(*got):
                first[s] = b[0, 4]
        ctx = {}
        for s, row in base.items():
            h = hist.get(s)
            rv = None
            if h is not None and len(h) >= 5:
                m = float(np.mean(h))
                v = first.get(s)
                if m > 0 and v is not None and not np.isnan(v):
                    rv = float(v) / m
            ctx[s] = dict(zip(FIELDS, [float(x) for x in row] + [rv]))
        for s, v in first.items():
            if v is not None and not np.isnan(v):
                hist.setdefault(s, deque(maxlen=14)).append(float(v))
        yield d, ctx


def inplay(ctx: dict) -> list[str]:
    """Chosen only from what was known by 09:35."""
    def gap(c):
        return abs(c["o"] / c["pc"] - 1) * 100 if c["pc"] and not np.isnan(c["o"]) else 0.0
    gappers = [s for s, c in ctx.items() if gap(c) >= GAP_INPLAY]
    rvs = [(c["rv"], s) for s, c in ctx.items() if c["rv"] is not None]
    top_rv = [s for _, s in sorted(((r, s) for r, s in rvs if ctx[s]["pc"] >= 3), reverse=True)[:TOP_RVOL]]
    orb = [s for _, s in sorted(((r, s) for r, s in rvs
                                 if ctx[s]["pc"] >= 5 and ctx[s]["adv"] / ctx[s]["pc"] >= 1e6
                                 and ctx[s]["atr"] >= 0.5), reverse=True)[:40]]
    top_adv = [s for _, s in sorted(((c["adv"], s) for s, c in ctx.items() if s not in ETFS),
                                    reverse=True)[:TOP_ADV]]
    movers = [s for s, c in ctx.items() if c["pc"] >= 5 and abs(c["pret"]) >= 8]
    etfs = [s for s in ETFS if s in ctx]
    return sorted(set(gappers) | set(top_rv) | set(orb) | set(top_adv) | set(movers) | set(etfs))


def save_ctx(d: date, ctx: dict, syms: list[str]) -> None:
    (STORE / "ctx").mkdir(exist_ok=True)
    pickle.dump({s: ctx[s] for s in syms if s in ctx}, open(STORE / "ctx" / f"{d}.pkl", "wb"))


def load_ctx(d: date) -> dict:
    return pickle.load(open(STORE / "ctx" / f"{d}.pkl", "rb"))


def s1_candidates(ctx: dict, n: int = 20) -> list[str]:
    """The ORB study's stocks in play: price >= $5, >= 1M shares/day, ATR >= $0.50, RVOL5 >= 1,
    top n by RVOL5. One definition shared by the 1-minute fetch and the S1 test."""
    c = [(v["rv"], s) for s, v in ctx.items()
         if s not in ETFS and v["rv"] is not None and v["rv"] >= 1 and v["pc"] >= 5
         and v["adv"] / v["pc"] >= 1e6 and v["atr"] >= 0.5]
    return [s for _, s in sorted(c, reverse=True)[:n]]
