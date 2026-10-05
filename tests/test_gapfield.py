"""
EDGE_ACCELERATION_SPEC_v1 Part 4 -- the gap field.

Pinned here:
  - a gap is found only on consecutive sessions, above the price and
    liquidity floors, with every input taken from bars BEFORE the gap day
  - both arms are priced by the population monkeys' rules
  - the forward scan writes 'forward' rows; history writes 'backfill' rows
    and never overwrites a forward row; the backfill resumes where it stopped
  - evidence is counted in (session, industry) units
  - a surviving cell becomes a gap-field rule, and forward observations come
    ONLY from forward rows dated after the quarantine
  - the nightly job runs end to end and heals a missed night
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta

import pytest

from agent import gapfield, ledger, research
from agent.config import Config
from agent.stats import evaluate_search
from agent.util import ET, is_market_day


def _sessions(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if is_market_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _series(days: list[date], gaps: dict[date, float] | None = None, price=20.0,
            volume=2_000_000, close_move=0.0) -> list[dict]:
    """Flat daily bars at `price` (range 2% of price), with an open gap of
    gaps[d] percent on chosen sessions; the gap day closes `close_move`
    percent from its open."""
    bars, c = [], price
    for d in days:
        g = (gaps or {}).get(d)
        o = c * (1 + g / 100.0) if g else c
        cl = o * (1 + close_move / 100.0) if g else c
        hi, lo = max(o, cl) * 1.01, min(o, cl) * 0.99
        bars.append({"t": datetime.combine(d, datetime.min.time(), tzinfo=ET),
                     "o": o, "h": hi, "l": lo, "c": cl, "v": volume})
        c = cl
    return bars


class _FakeAlpaca:
    concurrency = 1

    def __init__(self, series):
        self.series = series
        self.calls = 0

    def multi_bars(self, symbols, timeframe, start, end, feed=None, adjustment="raw", workers=1):
        self.calls += 1
        return {s: [b for b in self.series.get(s, []) if start.date() <= b["t"].date() <= end.date()]
                for s in symbols}


class _FakeMD:
    def __init__(self, series):
        self.alpaca = _FakeAlpaca(series)

    def fundamentals(self, symbols, con=None):
        return {s: {"industry": "Biotechnology" if s.startswith("B") else "Software"} for s in symbols}


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "gf.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


CFG = Config(raw={"slippage_bps": 0})
DAYS = _sessions(date(2025, 6, 2), date(2026, 10, 9))
GAP_DAY = date(2026, 10, 6)


# ----------------------------------------------------------------- pure parts

def test_a_gap_is_found_with_inputs_from_before_the_gap_day():
    bars = _series(DAYS, {GAP_DAY: 6.0, date(2026, 10, 2): 1.0})
    evs = gapfield.events_from_bars("AAA", bars, GAP_DAY, GAP_DAY, {GAP_DAY: 0.4}, "Software")
    assert len(evs) == 1
    e = evs[0]
    assert e["gap_pct"] == pytest.approx(6.0, abs=1e-6)
    assert e["prev_close"] == pytest.approx(bars[DAYS.index(GAP_DAY) - 1]["c"])
    # ATR of flat 2%-range bars is ~2% of price: a 6% gap is ~3 ATR
    assert 2.5 < e["gap_atr"] < 3.5
    assert e["adv20_usd"] == pytest.approx(20.2 * 2_000_000, rel=0.02)
    assert e["sma200"] is not None and e["spy_gap_pct"] == 0.4 and e["industry"] == "Software"


@pytest.mark.parametrize("kw, why", [
    ({"price": 4.0}, "below the price floor"),
    ({"volume": 100_000}, "below the liquidity floor"),
])
def test_floors_exclude_a_gap(kw, why):
    bars = _series(DAYS, {GAP_DAY: 6.0}, **kw)
    assert gapfield.events_from_bars("AAA", bars, GAP_DAY, GAP_DAY) == [], why


def test_a_missing_session_is_not_a_gap():
    bars = [b for b in _series(DAYS, {GAP_DAY: 6.0}) if b["t"].date() != date(2026, 10, 5)]
    assert gapfield.events_from_bars("AAA", bars, GAP_DAY, GAP_DAY) == []


def test_both_arms_are_priced_by_the_monkeys_rules():
    bars = _series(DAYS, {GAP_DAY: 6.0}, close_move=-2.0)     # gap up, fades into the close
    ev = gapfield.events_from_bars("AAA", bars, GAP_DAY, GAP_DAY)[0]
    rows = {r["arm"]: r for r in gapfield.simulate(ev)}
    assert rows["momentum"]["direction"] == "long" and rows["fade"]["direction"] == "short"
    assert rows["fade"]["r_multiple"] > 0 > rows["momentum"]["r_multiple"]


@pytest.mark.parametrize("dim, row, lev", [
    ("gf_strategy", {"arm": "fade", "direction": "short"}, "fade_short"),
    ("gf_gap_atr", {"gap_atr": 0.99}, "<1"), ("gf_gap_atr", {"gap_atr": 3.0}, ">3"),
    ("gf_gap_pct", {"gap_pct": -7.0}, "5-10"), ("gf_gap_pct", {"gap_pct": 25}, ">20"),
    ("gf_price", {"prev_close": 10.0}, "10-30"), ("gf_liquidity", {"adv20_usd": 2.4e7}, "<25M"),
    ("gf_trend_200d", {"prev_close": 10, "sma200": 9}, "above"),
    ("gf_trend_200d", {"prev_close": 10}, "?"),
    ("gf_prior_day", {"prior_ret_pct": -3.5}, "<-3"), ("gf_vol_ratio", {"vol_ratio": 2.0}, ">2"),
    ("gf_market_gap", {"spy_gap_pct": 0.1}, "flat"), ("gf_market_gap", {"spy_gap_pct": -0.3}, "down"),
])
def test_labels(dim, row, lev):
    assert gapfield.label(dim, row) == lev


def test_evidence_is_counted_in_session_industry_units():
    rows = [{"session_date": GAP_DAY, "ticker": t, "industry": ind, "arm": "fade",
             "direction": "short", "gap_atr": 1.5, "r_multiple": r}
            for t, ind, r in (("BAA", "Biotechnology", 1.0), ("BBB", "Biotechnology", -1.0),
                              ("SSS", "Software", 0.5))]
    cell = next(c for c in gapfield.cell_stats(rows)
                if c["strategy"] == "fade_short" and c["dimension"] == "gf_gap_atr")
    assert cell["n_units"] == 2 and cell["n_rows"] == 3
    assert cell["mean_r"] == pytest.approx((0.0 + 0.5) / 2)


# ------------------------------------------------------------- with a ledger

def _md(n_gappers=3):
    series = {"SPY": _series(DAYS, {GAP_DAY: 0.5}, price=500)}
    for i in range(n_gappers):
        series[f"B{i}"] = _series(DAYS, {GAP_DAY: 6.0, date(2025, 12, 3): -5.0}, close_move=-2.0)
    series["QUIET"] = _series(DAYS)
    return _FakeMD(series)


def test_the_forward_scan_writes_forward_rows_once(con, monkeypatch):
    md = _md()
    monkeypatch.setattr(gapfield, "universe", lambda md_, con_, d: ["B0", "B1", "B2", "QUIET"])
    r = gapfield.scan_session(con, md, CFG, GAP_DAY)
    assert (r["gappers"], r["events"], r["rows"]) == (3, 3, 6)
    rows = gapfield.forward_rows(con, GAP_DAY)
    assert len(rows) == 6 and {x["industry"] for x in rows} == {"Biotechnology"}
    gapfield.scan_session(con, md, CFG, GAP_DAY)            # idempotent
    assert con.execute("SELECT COUNT(*) FROM gapfield_events").fetchone()[0] == 6


def test_the_backfill_resumes_and_never_overwrites_a_forward_row(con, monkeypatch):
    md = _md(n_gappers=5)
    syms = ["B0", "B1", "B2", "B3", "B4", "QUIET"]
    monkeypatch.setattr(gapfield, "CHUNK", 2)
    # the forward scan sees three gappers; history then covers all five
    monkeypatch.setattr(gapfield, "universe", lambda md_, con_, d: syms[:3])
    gapfield.scan_session(con, md, CFG, GAP_DAY)
    monkeypatch.setattr(gapfield, "universe", lambda md_, con_, d: syms)
    # deadline already passed after one chunk: the job stops and resumes
    res = gapfield.backfill_step(con, md, CFG, date(2026, 10, 9), deadline=time.monotonic())
    assert res["status"] == "in progress" and res["next_index"] == 0
    res = gapfield.backfill_step(con, md, CFG, date(2026, 10, 9), deadline=time.monotonic() + 60)
    assert res["status"] == "complete" and res["next_index"] == len(syms)
    src = dict(con.execute("SELECT source, COUNT(*) FROM gapfield_events WHERE session_date = ? "
                           "GROUP BY 1", [GAP_DAY]).fetchall())
    assert src == {"forward": 6, "backfill": 4}     # B3/B4 only came from history
    assert gapfield.backfill_step(con, md, CFG, date(2026, 10, 9), time.monotonic() + 60)["status"] \
        == "complete"


def test_a_surviving_cell_becomes_a_rule_tested_only_on_later_forward_rows(con, monkeypatch):
    md = _md()
    monkeypatch.setattr(gapfield, "universe", lambda md_, con_, d: ["B0", "B1", "B2"])
    # history: 120 sessions of gap-ups that fade (the cell is strongly positive for fade_short)
    hist = DAYS[DAYS.index(date(2026, 3, 2)):DAYS.index(date(2026, 9, 1))]
    rows = []
    for i, d in enumerate(hist):
        for sym, ind in (("B0", "Biotechnology"), ("S1", "Software")):
            rows.append({"session_date": d, "symbol": sym, "arm": "fade", "direction": "short",
                         "gap_pct": 6.0, "gap_atr": 1.5, "atr14": 0.4, "prev_close": 20.0,
                         "adv20_usd": 4e7, "industry": ind,
                         "r_multiple": 0.8 + (0.3 if i % 2 else -0.3), "sim_version": 1})
    gapfield._insert_rows(con, rows, "backfill")
    gapfield.refresh_cells(con, date(2026, 9, 1))
    search = evaluate_search(gapfield.comparisons(con), q=0.10, min_n=8)
    reg = research.register_batch_survivors(con, date(2026, 10, 3), search.comparisons, {})
    # the forward gaps are 6% (bucket 5-10), so this is the cell they fall in
    rule = next(r for r in reg if r["cell"] == "gf_gap_pct=5-10 AND gf_strategy=fade_short")
    assert rule["lane"] == "gapfield"
    st = next(s for s in research.rule_states(con) if s["rule_id"] == rule["rule_id"])
    assert st["state"] == "QUARANTINED" and st["discovery_n"] > 100
    # history on/before the quarantine never counts; a later forward session does,
    # one observation per (session, industry)
    assert research.observe_session(con, date(2026, 8, 31), lanes=("gapfield",))["recorded"] == 0
    gapfield.scan_session(con, md, CFG, GAP_DAY)
    out = research.observe_session(con, GAP_DAY, lanes=("gapfield",))
    assert out["recorded"] >= 1
    obs = con.execute("SELECT source, COUNT(*) FROM research_observations WHERE rule_id = ? "
                      "GROUP BY 1", [rule["rule_id"]]).fetchall()
    assert obs == [("gapfield", 1)]          # three Biotechnology gappers = one unit


def test_the_nightly_job_prices_the_session_backfills_and_heals_a_missed_night(con, monkeypatch):
    from agent.loops import nightly
    md = _md()
    monkeypatch.setattr(gapfield, "universe", lambda md_, con_, d: ["B0", "B1", "B2", "QUIET"])
    monkeypatch.setattr("agent.providers.MarketData", lambda cfg: md)
    out = nightly.run(CFG, date(2026, 10, 6), budget_s=200)   # its own connection, same file
    assert [f["session"] for f in out["forward"]] == ["2026-10-06"]
    assert out["backfill"]["status"] == "complete" and out["cells"] > 0
    assert con.execute("SELECT status FROM runs WHERE loop = 'nightly'").fetchone()[0] == "ok"
    # two nights missed: the next run prices both, oldest first
    assert [str(d) for d in nightly.pending_sessions(con, datetime(2026, 10, 8, 23, 0))] == \
        ["2026-10-07", "2026-10-08"]
