"""Index/sector ETF five-minute sessions in one array per symbol, plus prior close and ATR,
for fast work on single-instrument strategies (S4b family)."""
import pickle
from datetime import date

import numpy as np

from context import load_ctx
from fetch import ETFS, STORE, load_daily, load_day5, sessions


def build(start=date(2021, 12, 1), end=date(2026, 10, 6)):
    daily = load_daily()
    days = sessions(daily, start, end)
    del daily
    out = {s: {"days": [], "bars": [], "pc": [], "atr": []} for s in ETFS}
    for d in days:
        got = load_day5(d)
        if got is None:
            continue
        syms, bars = got
        try:
            ctx = load_ctx(d)
        except FileNotFoundError:
            continue
        for s in ETFS:
            if s in syms and s in ctx:
                out[s]["days"].append(d)
                out[s]["bars"].append(bars[syms.index(s)].astype(np.float64))
                out[s]["pc"].append(ctx[s]["pc"])
                out[s]["atr"].append(ctx[s]["atr"])
    for s in ETFS:
        for k in ("bars", "pc", "atr"):
            out[s][k] = np.array(out[s][k])
    pickle.dump(out, open(STORE / "etf5.pkl", "wb"))
    return out


def load():
    return pickle.load(open(STORE / "etf5.pkl", "rb"))
