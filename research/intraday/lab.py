"""Run the registered day-trade tests (REGISTER.md) over a period and report them.

    python lab.py run discovery            # all 34 tests, 2022-01-03 .. 2024-06-28
    python lab.py run holdout S1_orb5_atr  # ONE look at a finalist, 2024-07-01 .. 2026-10-06

Conventions are the register's: 5-minute bars, flat at the 15:55 bar's open, stop before
target inside a bar, no target on a stop-entry/limit fill bar, cost max(5 bps, $0.01) a side,
R on planned prices, the session as the unit.
"""
from __future__ import annotations

import math
import pickle
import sys
from collections import defaultdict
from datetime import date

import numpy as np

from context import load_ctx, s1_candidates
from fetch import ETFS, STORE, load_daily, load_day5, load_min1, sessions

EXIT_BAR = 77           # 15:55
LAST_ENTRY = 75         # a stop-entry may fill through the 15:45 bar
PERIODS = {"discovery": (date(2022, 1, 3), date(2024, 6, 28)),
           "holdout": (date(2024, 7, 1), date(2026, 10, 6))}
N_TESTS = 34


def slip(px: float, etf: bool = False) -> float:
    return max((0.0001 if etf else 0.0005) * px, 0.01)


def sim(B, side, k0, e, stop, target=None, intrabar=False, exit_bar=EXIT_BAR):
    """Walk bars from k0; returns the exit price."""
    for k in range(k0, exit_bar):
        o, h, l = B[k, 0], B[k, 1], B[k, 2]
        if o != o:
            continue
        fill_bar = intrabar and k == k0
        if side > 0:
            if not fill_bar and o <= stop:
                return o
            if l <= stop:
                return stop
            if target is not None and not fill_bar and h >= target:
                return max(o, target)
        else:
            if not fill_bar and o >= stop:
                return o
            if h >= stop:
                return stop
            if target is not None and not fill_bar and l <= target:
                return min(o, target)
    x = B[exit_bar, 0]
    if x != x:
        prev = B[:exit_bar, 3]
        prev = prev[~np.isnan(prev)]
        x = prev[-1] if len(prev) else e
    return x


def trade(side, e, stop, x, etf=False):
    risk = abs(e - stop)
    if risk <= 0 or e <= 0:
        return None
    gross = side * (x - e) / risk
    cost = (slip(e, etf) + slip(x, etf)) / risk
    return gross, cost


def vwap(B):
    tp = (B[:, 1] + B[:, 2] + B[:, 3]) / 3
    v = np.nan_to_num(B[:, 4])
    pv = np.nan_to_num(tp) * v
    cv = np.cumsum(v)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(cv > 0, np.cumsum(pv) / cv, np.nan)


def stop_entry(B, side, trigger, k_from, k_to=LAST_ENTRY):  # noqa: D401
    """First bar whose range reaches the trigger; fill at the trigger or a gapped-through open."""
    for k in range(k_from, k_to + 1):
        o, h, l = B[k, 0], B[k, 1], B[k, 2]
        if o != o:
            continue
        if side > 0 and h >= trigger:
            return k, max(o, trigger)
        if side < 0 and l <= trigger:
            return k, min(o, trigger)
    return None, None


class Day:
    def __init__(self, d, syms, bars, ctx, index_hist, min1=None):
        self.d, self.syms, self.bars, self.ctx = d, syms, bars, ctx
        self.min1 = min1
        self.i = {s: k for k, s in enumerate(syms)}
        self._vw = {}
        self.index_hist = index_hist

    def B(self, s):
        k = self.i.get(s)
        return None if k is None else self.bars[k].astype(np.float64)

    def vw(self, s):
        if s not in self._vw:
            self._vw[s] = vwap(self.B(s))
        return self._vw[s]

    def c(self, s):
        return self.ctx.get(s)

    def stocks(self):
        return [s for s in self.syms if s not in ETFS and s in self.ctx]

    def gap(self, s):
        c = self.ctx[s]
        return (c["o"] / c["pc"] - 1) * 100 if c["pc"] else 0.0

    def ok(self, s, price=5.0, adv=10e6):
        c = self.ctx.get(s)
        return (c is not None and c["pc"] >= price and c["adv"] >= adv and c["atr"] > 0
                and not math.isnan(c["atr"]))

    def top_rv(self, n=80):
        r = [(c["rv"], s) for s, c in self.ctx.items() if c["rv"] is not None and s not in ETFS and c["pc"] >= 3]
        return {s for _, s in sorted(r, reverse=True)[:n]}


def _valid(B, ks):
    return all(B[k, 0] == B[k, 0] for k in ks)


# --------------------------------------------------------------------------- the 34 tests

M1_EXIT, M1_LAST_ENTRY = 385, 375     # 15:55 and 15:45 on one-minute bars


def s1(day: Day, or_min: int, stop_mode: str):
    """Opening-range breakout on stocks in play, on one-minute bars (REGISTER fix 1)."""
    out = []
    if day.min1 is None:
        return out
    syms, bars = day.min1
    for s in s1_candidates(day.ctx):
        if s not in syms:
            continue
        B = bars[syms.index(s)].astype(np.float64)
        if not _valid(B, [0, or_min - 1]):
            continue
        hi, lo = np.nanmax(B[:or_min, 1]), np.nanmin(B[:or_min, 2])
        o0, cN = B[0, 0], B[or_min - 1, 3]
        side = 1 if cN > o0 else (-1 if cN < o0 else 0)
        if not side:
            continue
        k, e = stop_entry(B, side, hi if side > 0 else lo, or_min, M1_LAST_ENTRY)
        if k is None:
            continue
        atr = day.c(s)["atr"]
        stop = (e - side * 0.1 * atr) if stop_mode == "atr" else (lo if side > 0 else hi)
        if side * (e - stop) <= 0:
            continue
        x = sim(B, side, k, e, stop, intrabar=True, exit_bar=M1_EXIT)
        out.append((s, side, trade(side, e, stop, x)))
    return out


def s2(day: Day, mode: str, fade: int):
    """fade=-1 shorts gap-ups, fade=+1 buys gap-downs."""
    out = []
    for s in day.stocks():
        if not day.ok(s):
            continue
        g = day.gap(s)
        if not ((fade < 0 and g >= 3) or (fade > 0 and g <= -3)):
            continue
        B, atr, side = day.B(s), day.c(s)["atr"], fade
        if B is None or not _valid(B, [0, 1]):
            continue
        if mode == "open":
            e = B[0, 0]
            stop = e - side * atr
            out.append((s, side, trade(side, e, stop, sim(B, side, 0, e, stop))))
        else:
            against = (B[0, 3] < B[0, 0]) if side < 0 else (B[0, 3] > B[0, 0])
            if not against:
                continue
            e = B[1, 0]
            stop = B[0, 1] if side < 0 else B[0, 2]
            if side * (e - stop) < 0.05 * atr:      # risk = side*(e-stop); too thin to price
                continue
            out.append((s, side, trade(side, e, stop, sim(B, side, 1, e, stop))))
    return out


def s3(day: Day, rvmin: float, side: int):
    out = []
    for s in day.stocks():
        if not day.ok(s):
            continue
        g, c = day.gap(s), day.c(s)
        if not ((side > 0 and g >= 3) or (side < 0 and g <= -3)):
            continue
        if rvmin and (c["rv"] is None or c["rv"] < rvmin):
            continue
        B = day.B(s)
        if B is None or not _valid(B, [0]):
            continue
        if not ((side > 0 and B[0, 3] > B[0, 0]) or (side < 0 and B[0, 3] < B[0, 0])):
            continue
        trig, stop = (B[0, 1], B[0, 2]) if side > 0 else (B[0, 2], B[0, 1])
        k, e = stop_entry(B, side, trig, 1)
        if k is None or side * (e - stop) <= 0:
            continue
        out.append((s, side, trade(side, e, stop, sim(B, side, k, e, stop, intrabar=True))))
    return out


def s4a(day: Day, sym: str):
    B, c = day.B(sym), day.c(sym)
    if B is None or c is None or not _valid(B, [6, 72, EXIT_BAR]):
        return []
    r = B[6, 0] / c["pc"] - 1
    side = 1 if r > 0 else (-1 if r < 0 else 0)
    if not side:
        return []
    e = B[72, 0]
    stop = e - side * 0.25 * c["atr"]        # a unit for R only; the published rule has no stop
    x = sim(B, side, 72, e, e - side * 1e9)
    return [(sym, side, trade(side, e, stop, x, etf=True))]


def s4b(day: Day, sym: str):
    """Noise-area breakout, checked at the half-hour marks from 10:00, trailed by
    max(boundary, VWAP); several trades a day are possible."""
    B, c = day.B(sym), day.c(sym)
    hist = day.index_hist.get(sym, [])
    if B is None or c is None or len(hist) < 14 or not _valid(B, [0]):
        return []
    moves = np.array([np.abs(h[:, 3] / h[0, 0] - 1) for h in hist[-14:]])
    sigma = np.nanmean(moves, axis=0)
    o, pc = B[0, 0], c["pc"]
    ub, lb = max(o, pc) * (1 + sigma), min(o, pc) * (1 - sigma)
    vw = day.vw(sym)
    unit = 0.25 * c["atr"]                 # R unit only (REGISTER fix 2)
    out, pos, e = [], 0, None
    for k in range(5, 72, 6):              # bar closes at 10:00, 10:30, ... 15:30
        px = B[k, 3]
        nxt = B[k + 1, 0]
        if px != px or nxt != nxt:
            continue
        if pos > 0 and px < max(ub[k], vw[k]):
            out.append((sym, 1, trade(1, e, e - unit, nxt, etf=True))); pos = 0
        elif pos < 0 and px > min(lb[k], vw[k]):
            out.append((sym, -1, trade(-1, e, e + unit, nxt, etf=True))); pos = 0
        if pos == 0 and k < 71:
            if px > ub[k]:
                pos, e = 1, nxt
            elif px < lb[k]:
                pos, e = -1, nxt
    if pos:
        x = sim(B, pos, EXIT_BAR, e, -pos * 1e9)
        out.append((sym, pos, trade(pos, e, e - pos * unit, x, etf=True)))
    return out


def _inplay(day: Day):
    top = day.top_rv(80)
    return [s for s in day.stocks() if day.ok(s) and (s in top or abs(day.gap(s)) >= 3)]


def s5(day: Day, stop_mode: str, side: int):
    out = []
    for s in _inplay(day):
        B = day.B(s)
        if not _valid(B, [0, 60]):
            continue
        atr, e = day.c(s)["atr"], B[60, 0]
        vw = day.vw(s)[59]
        move = (e - B[0, 0]) / atr
        if side > 0:
            ext = np.nanmax(B[:60, 1])
            if not (move >= 1 and e > vw and e >= ext - 0.25 * atr):
                continue
        else:
            ext = np.nanmin(B[:60, 2])
            if not (move <= -1 and e < vw and e <= ext + 0.25 * atr):
                continue
        stop = e - side * 0.5 * atr if stop_mode == "atr" else vw
        if side * (e - stop) < 0.1 * atr:
            continue
        out.append((s, side, trade(side, e, stop, sim(B, side, 60, e, stop))))
    return out


def s6(day: Day, exit_mode: str, side: int):
    out = []
    for s in _inplay(day):
        B = day.B(s)
        if not _valid(B, range(0, 12)):
            continue
        atr, vw = day.c(s)["atr"], day.vw(s)
        held = all(side * (B[k, 3] - vw[k]) > 0 for k in range(3, 12))
        if not held or side * (B[11, 3] - B[0, 0]) / atr < 0.75:
            continue
        for k in range(12, 61):
            if B[k, 0] != B[k, 0]:
                continue
            lvl = vw[k - 1]
            touched = B[k, 2] <= lvl if side > 0 else B[k, 1] >= lvl
            if not touched:
                continue
            e = min(B[k, 0], lvl) if side > 0 else max(B[k, 0], lvl)
            stop = lvl - side * 0.3 * atr
            if side * (e - stop) <= 0:
                break
            tgt = e + side * 2 * abs(e - stop) if exit_mode == "2r" else None
            out.append((s, side, trade(side, e, stop, sim(B, side, k, e, stop, tgt, intrabar=True))))
            break
    return out


def s7(day: Day, kT: int, side: int):
    spy = day.B("SPY")
    if spy is None or not _valid(spy, [0, kT]):
        return []
    sr = spy[kT, 0] / spy[0, 0] - 1
    big = sorted(((day.c(s)["adv"], s) for s in day.stocks() if day.ok(s)), reverse=True)[:100]
    rows = []
    for _, s in big:
        B = day.B(s)
        if B is None or not _valid(B, [0, kT]):
            continue
        rows.append((B[kT, 0] / B[0, 0] - 1 - sr, s))
    rows.sort()
    pick = rows[:5] if side > 0 else rows[-5:]
    out = []
    for _, s in pick:
        B, atr = day.B(s), day.c(s)["atr"]
        e = B[kT, 0]
        stop = e - side * atr
        out.append((s, side, trade(side, e, stop, sim(B, side, kT, e, stop))))
    return out


def s8(day: Day, side: int):
    out = []
    top = day.top_rv(80)
    for s in day.stocks():
        if not day.ok(s) or abs(day.gap(s)) >= 2:
            continue
        c = day.c(s)
        if not (s in top or (c["rv"] or 0) >= 2):
            continue
        B = day.B(s)
        if B is None or not _valid(B, [0, 1, 2, 3]):
            continue
        atr = c["atr"]
        move = (B[2, 3] - B[0, 0]) / atr
        if side < 0 and move >= 1:
            stop = np.max(B[:3, 1]) + 0.1 * atr
        elif side > 0 and move <= -1:
            stop = np.min(B[:3, 2]) - 0.1 * atr
        else:
            continue
        e = B[3, 0]
        if side * (e - stop) <= 0:
            continue
        out.append((s, side, trade(side, e, stop, sim(B, side, 3, e, stop))))
    return out


def s9(day: Day, side: int):
    out = []
    for s in day.stocks():
        if not day.ok(s):
            continue
        c = day.c(s)
        if not (side * c["pret"] >= 8 and (c["pdvr"] or 0) >= 3):
            continue
        B = day.B(s)
        if B is None or not _valid(B, [0]):
            continue
        trig, stop = (B[0, 1], B[0, 2]) if side > 0 else (B[0, 2], B[0, 1])
        k, e = stop_entry(B, side, trig, 1)
        if k is None or side * (e - stop) <= 0:
            continue
        out.append((s, side, trade(side, e, stop, sim(B, side, k, e, stop, intrabar=True))))
    return out


def s10(day: Day, side: int):
    out = []
    for s in _inplay(day):
        B = day.B(s)
        if not _valid(B, [0, 18]):
            continue
        atr, e, vw = day.c(s)["atr"], B[18, 0], day.vw(s)[17]
        ext = (e - vw) / atr
        if side < 0 and ext >= 1.5:
            stop = np.nanmax(B[:18, 1]) + 0.1 * atr
        elif side > 0 and ext <= -1.5:
            stop = np.nanmin(B[:18, 2]) - 0.1 * atr
        else:
            continue
        if side * (e - stop) <= 0:
            continue
        out.append((s, side, trade(side, e, stop, sim(B, side, 18, e, stop))))
    return out


TESTS = {
    "S1_orb5_atr": lambda d: s1(d, 5, "atr"), "S1_orb5_range": lambda d: s1(d, 5, "range"),
    "S1_orb15_atr": lambda d: s1(d, 15, "atr"), "S1_orb15_range": lambda d: s1(d, 15, "range"),
    "S2_open_short": lambda d: s2(d, "open", -1), "S2_open_long": lambda d: s2(d, "open", 1),
    "S2_conf_short": lambda d: s2(d, "confirm", -1), "S2_conf_long": lambda d: s2(d, "confirm", 1),
    "S3_all_long": lambda d: s3(d, 0, 1), "S3_all_short": lambda d: s3(d, 0, -1),
    "S3_rv2_long": lambda d: s3(d, 2, 1), "S3_rv2_short": lambda d: s3(d, 2, -1),
    "S4a_SPY": lambda d: s4a(d, "SPY"), "S4a_QQQ": lambda d: s4a(d, "QQQ"),
    "S4b_SPY": lambda d: s4b(d, "SPY"), "S4b_QQQ": lambda d: s4b(d, "QQQ"),
    "S5_atr_long": lambda d: s5(d, "atr", 1), "S5_atr_short": lambda d: s5(d, "atr", -1),
    "S5_vwap_long": lambda d: s5(d, "vwap", 1), "S5_vwap_short": lambda d: s5(d, "vwap", -1),
    "S6_eod_long": lambda d: s6(d, "eod", 1), "S6_eod_short": lambda d: s6(d, "eod", -1),
    "S6_2r_long": lambda d: s6(d, "2r", 1), "S6_2r_short": lambda d: s6(d, "2r", -1),
    "S7_1000_long": lambda d: s7(d, 6, 1), "S7_1000_short": lambda d: s7(d, 6, -1),
    "S7_1100_long": lambda d: s7(d, 18, 1), "S7_1100_short": lambda d: s7(d, 18, -1),
    "S8_long": lambda d: s8(d, 1), "S8_short": lambda d: s8(d, -1),
    "S9_long": lambda d: s9(d, 1), "S9_short": lambda d: s9(d, -1),
    "S10_long": lambda d: s10(d, 1), "S10_short": lambda d: s10(d, -1),
}
assert len(TESTS) == N_TESTS


def run(period: str, only: list[str] | None = None):
    start, end = PERIODS[period]
    daily = load_daily()
    days = sessions(daily, start, end)
    del daily
    tests = {k: v for k, v in TESTS.items() if not only or k in only}
    rows = []          # (date, test, symbol, side, gross, cost)
    index_hist: dict[str, list] = defaultdict(list)
    # warm the noise-area history with the sessions just before the period
    all_days = sorted(p.stem for p in (STORE / "day5").glob("*.npz"))
    prior = [date.fromisoformat(x) for x in all_days if date.fromisoformat(x) < start][-14:]
    for d in prior:
        got = load_day5(d)
        if got:
            syms, bars = got
            for s in ("SPY", "QQQ"):
                if s in syms:
                    index_hist[s].append(bars[syms.index(s)].astype(np.float64))
    for n, d in enumerate(days):
        got = load_day5(d)
        if got is None:
            continue
        syms, bars = got
        day = Day(d, syms, bars, load_ctx(d), index_hist, load_min1(d))
        for name, fn in tests.items():
            for s, side, tr in fn(day):
                if tr is not None and all(map(math.isfinite, tr)):
                    rows.append((d, name, s, side, tr[0], tr[1]))
        for s in ("SPY", "QQQ"):
            B = day.B(s)
            if B is not None:
                index_hist[s].append(B)
                index_hist[s] = index_hist[s][-14:]
        if n % 100 == 0:
            print(f"  {n}/{len(days)} {d} rows {len(rows)}", flush=True)
    out = STORE / f"lab_{period}{'_' + '_'.join(only) if only else ''}.pkl"
    pickle.dump(rows, open(out, "wb"))
    return rows


def stats(rows, cost_mult=1.0):
    by_s = defaultdict(list)
    for r in rows:
        by_s[r[0]].append(r[4] - cost_mult * r[5])
    sm = np.array([np.mean(v) for v in by_s.values()])
    n = len(sm)
    if n < 2:
        return dict(n=len(rows), sess=n, mean=float("nan"), t=float("nan"))
    m, sd = sm.mean(), sm.std(ddof=1)
    return dict(n=len(rows), sess=n, mean=float(m), t=float(m / (sd / math.sqrt(n))) if sd > 0 else float("nan"),
                per_trade=float(np.mean([r[4] - cost_mult * r[5] for r in rows])),
                win=float(np.mean([(r[4] - cost_mult * r[5]) > 0 for r in rows]) * 100))


def report(rows, title=""):
    by_t = defaultdict(list)
    for r in rows:
        by_t[r[1]].append(r)
    print(f"\n{title}")
    print(f"{'test':16s} {'trades':>7s} {'sess':>5s} {'/day':>5s} {'gross':>7s} {'cost':>6s} {'net':>7s} "
          f"{'t_net':>6s} {'t_gross':>7s} {'net3x':>7s} {'win%':>5s}  by year (net)")
    for name in TESTS:
        rs = by_t.get(name)
        if not rs:
            print(f"{name:16s}  no trades")
            continue
        g, nt, n3 = stats(rs, 0.0), stats(rs, 1.0), stats(rs, 3.0)
        cost = float(np.mean([r[5] for r in rs]))
        years = defaultdict(list)
        for r in rs:
            years[r[0].year if r[0] < date(2024, 1, 1) or r[0] >= date(2025, 1, 1) else 2024].append(r)
        yr = " ".join(f"{y}:{stats(v, 1.0)['mean']:+.3f}" for y, v in sorted(years.items()))
        flag = " <==" if abs(nt["t"]) >= 3.18 or abs(g["t"]) >= 3.18 else ""
        print(f"{name:16s} {nt['n']:7d} {nt['sess']:5d} {nt['n'] / nt['sess']:5.1f} {g['mean']:+7.3f} {cost:6.3f} "
              f"{nt['mean']:+7.3f} {nt['t']:6.2f} {g['t']:7.2f} {n3['mean']:+7.3f} {nt['win']:5.1f}  {yr}{flag}")


if __name__ == "__main__":
    cmd, period = sys.argv[1], sys.argv[2]
    only = sys.argv[3:] or None
    if period == "holdout" and not only:
        sys.exit("the hold-out is looked at one finalist at a time: name it")
    rows = run(period, only)
    report(rows, f"{period}: {len(rows)} trades")
