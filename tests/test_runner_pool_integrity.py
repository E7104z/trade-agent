"""
Runner pool integrity (specs/RUNNER_POOL_INTEGRITY_v1.md).

Every test here pins a defect found in the 2026-10-01 production session, with
the real names and numbers that exposed it:

  1. the "movers" panel ranked by change_pct, a detection-time feature, and
     called the most-extended names misses (LITG +16.9%, exited -0.60R);
  2. 70 of 534 candidates were not operating companies' stock -- 2x
     single-stock ETFs (three on Lumentum alone), thematic and commodity
     funds, a closed-end fund, Redwood Trust's senior notes -- admitted
     because the exclusion list is frozen and screener names skipped the
     liquidity floor;
  3. the plan was taken at the walked-back first-pass bar while the fill was
     at the scan tick, so a name that kept running was bought on a stale
     bracket: PKE/PGY/FLR filled a cent under target and booked "target"
     exits at a loss, SEPN/FUL/BEPC printed +5..7R on r_multiple;
  4. six of ten picks were one tanker/shipping trade, and every n >= 8 counted
     correlated rows one by one.

Everything runs on a temporary DuckDB with stubbed providers -- no network.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone

import pytest

from agent import (clusters, grading, instruments, ledger, research, runner_research,
                   runner_scan, runner_selection, runner_universe, runner_view)
from agent.config import Config

ET = timezone(timedelta(hours=-4))
SESSION = date(2026, 10, 1)


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "t.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


# ===================================================================
# 1. What an instrument IS (agent/instruments.py)
# ===================================================================

# Real Alpaca registered names, 2026-10-01. Each one decided a rule.
FUNDS = {
    "LITX": "Tradr 2X Long LITE Daily ETF",
    "LITU": "T-REX 2X Long LITE Daily Target ETF",
    "LITG": "Leverage Shares 2X Long LITE Daily ETF",
    "COHX": "Tradr 2X Long COHR Daily ETF",
    "COHH": "Leverage Shares 2X Long COHR Daily ETF",
    "MUU": "Direxion Shares ETF Trust Direxion Daily MU Bull 2X ETF",
    "TSLY": "YieldMax TSLA Option Income Strategy ETF",
    "GDXD": "MicroSectors Gold Miners -3x Inverse Leveraged ETN",
    "SSG": "ProShares UltraShort Semiconductors",
    "CRCA": "ProShares Ultra CRCL",
    "DBC": "Invesco DB Commodity Index Tracking Fund",
    "GSG": "iShares S&P  GSCI Commodity-Indexed Trust",
    "BSTZ": "BlackRock Science and Technology Term Trust",
    "QQQ": "Invesco QQQ Trust, Series 1",
    "GLD": "SPDR Gold Trust, SPDR Gold Shares",
    "ILTB": "iShares Core 10+ Year USD Bond ETFof iShares Trust",
    "HYT": "BLACKROCK CORPORATE HIGH YIELD FUND INC.",
    "MSBT": "Morgan Stanley Bitcoin Trust",
}
COMMON = {
    # operating companies whose names carry a fund-ish word
    "ABTC": "American Bitcoin Corp. Class A Common Stock",
    "WT": "WisdomTree, Inc.",
    "RWT": "Redwood Trust, Inc.",
    "KRG": "Kite Realty Group Trust",
    "WASH": "Washington Trust Bancorp, Inc. Common Stock",
    "UHT": "Universal Health Realty Income Trust",
    "CRT": "Cross Timbers Royalty Trust",
    "NTRS": "Northern Trust Corporation Common Stock",
    "KRP": "Kimbell Royalty Partners, LP Common Units representing Limited Partner Interests",
    "BIP": "Brookfield Infrastructure Partners L.P. Limited Partnership Units",
    "PRT": "PermRock Royalty Trust Trust Unit",
    "AMX": ("America Movil S.A.B de C.V American Depositary Shares (each representing "
            "the right to receive twenty (20) Series B Shares)"),
    "BBD": "Banco Bradesco S.A. American Depositary Shares (Each representing one Preferred Shares)",
    "FLNG": "FLEX LNG Ltd. Ordinary Shares",
    "XRPN": "Armada Acquisition Corp. II Class A Ordinary Shares",
}


@pytest.mark.parametrize("sym,name", sorted(FUNDS.items()))
def test_funds_and_wrappers_are_not_common_stock(sym, name):
    assert instruments.classify(name) == instruments.FUND, sym


@pytest.mark.parametrize("sym,name", sorted(COMMON.items()))
def test_operating_companies_stay_common_whatever_their_name_says(sym, name):
    assert instruments.classify(name) == instruments.COMMON, sym


@pytest.mark.parametrize("name,expected", [
    ("Redwood Trust, Inc. 9.500% Senior Notes Due 2030", instruments.DEBT),   # RWTQ, in the pool
    ("Tennessee Valley Authority Power Bonds 1998 Series D due June 1, 2028", instruments.DEBT),
    ("Structured Products CorTS PECO Energy Cap Trust III, 8% Cert.", instruments.DEBT),
    ("AT&T Inc. Depositary Shares, each representing a 1/1,000th interest in a share "
     "of 5.000% Perpetual Preferred Stock, Series A", instruments.PREFERRED),
    ("Live Oak Acquisition Corp. VI Units", instruments.UNIT),
    ("Trump Media & Technology Group Corp. Warrants", instruments.UNIT),
    ("", instruments.COMMON),
    (None, instruments.COMMON),
])
def test_debt_preferred_and_units(name, expected):
    assert instruments.classify(name) == expected


@pytest.mark.parametrize("qt,industry,flt,expected", [
    ("ETF", None, None, "quoteType ETF"),
    ("MUTUALFUND", None, None, "quoteType MUTUALFUND"),
    ("EQUITY", "Asset Management", None, "closed-end fund (Asset Management, no float)"),  # DXYZ
    ("EQUITY", "Asset Management", 133.4, None),        # WisdomTree, an operating manager
    ("EQUITY", "Banks - Regional", None, None),          # a float gap is not a fund
    ("", None, None, None),                              # unmeasured decides nothing
    (None, "Asset Management", 10.0, None),
])
def test_fund_from_profile(qt, industry, flt, expected):
    assert instruments.fund_from_profile(qt, industry, flt) == expected


# ===================================================================
# 2. The universe: every source passes the same filters
# ===================================================================

class _UniAlp:
    """Liquid list holds AAPL + an ETF; the movers screener brings a thin new
    2x ETF (LITG), a liquid stock and a thin stock; most-actives brings a
    symbol that is not an active tradable asset."""
    def __init__(self):
        self.records = [
            {"symbol": "AAPL", "name": "Apple Inc. Common Stock", "tradable": True, "fractionable": True},
            {"symbol": "SOXQ", "name": "Invesco PHLX Semiconductor ETF", "tradable": True, "fractionable": True},
            {"symbol": "LITG", "name": "Leverage Shares 2X Long LITE Daily ETF", "tradable": True, "fractionable": True},
            {"symbol": "GDCO", "name": "Good Company Inc.", "tradable": True, "fractionable": True},
            {"symbol": "THIN", "name": "Thin Holdings Inc.", "tradable": True, "fractionable": True},
            {"symbol": "RWTQ", "name": "Redwood Trust, Inc. 9.500% Senior Notes Due 2030", "tradable": True, "fractionable": True},
        ]

    def asset_records(self):
        return self.records

    def movers(self, top=50):
        return {"gainers": [{"symbol": "LITG"}, {"symbol": "GDCO"}, {"symbol": "THIN"}],
                "losers": [{"symbol": "RWTQ"}]}

    def most_actives(self, top=100, by="volume"):
        return {"most_actives": [{"symbol": "OTCX"}]}


class _UniMD:
    def __init__(self):
        self.alpaca = _UniAlp()


def _universe(con, monkeypatch, prev=None):
    monkeypatch.setattr(runner_universe, "DEFAULT_UNIVERSE", ())
    monkeypatch.setattr(runner_universe, "liquid_universe",
                        lambda *a, **k: (["AAPL", "SOXQ"], prev or {}))
    at = datetime(2026, 10, 1, 10, 30, tzinfo=ET)
    return runner_universe.build_universe(_UniMD(), {"universe_size": 6000}, at=at, con=con)


def test_every_universe_source_is_instrument_classified(con, monkeypatch):
    syms, stats = _universe(con, monkeypatch)
    assert "AAPL" in syms and "GDCO" in syms and "THIN" in syms
    for gone in ("SOXQ", "LITG", "RWTQ", "OTCX"):
        assert gone not in syms, gone
    assert stats["dropped_instrument"] == {"fund": 2, "debt": 1, "unlisted": 1}
    # LITG came in through the movers screener, which is where the drop is charged.
    assert stats["dropped_instrument_by_source"]["movers"] == 2
    assert stats["instruments_classified"] is True
    assert stats["names"]["AAPL"] == "Apple Inc. Common Stock"


def test_screener_names_pass_the_same_liquidity_floor_as_the_liquid_list(con, monkeypatch):
    prev = {"THIN": {"close": 22.0, "volume": 17_000},        # LITG-sized: ~$0.4M/day
            "GDCO": {"close": 30.0, "volume": 1_000_000}}   # $30M/day
    syms, stats = _universe(con, monkeypatch, prev=prev)
    assert "THIN" not in syms and "GDCO" in syms
    assert stats["dropped_liquidity_by_source"] == {"movers": 1}


def test_instrument_classes_are_cached_per_session(con, monkeypatch):
    md = _UniMD()
    calls = []
    orig = md.alpaca.asset_records
    md.alpaca.asset_records = lambda: calls.append(1) or orig()
    a = runner_universe.instrument_classes(md, con, SESSION)
    b = runner_universe.instrument_classes(md, con, SESSION)
    assert a == b and len(calls) == 1
    assert a["other"]["LITG"] == "fund"


def test_unclassified_universe_is_recorded_not_silently_trusted(con, monkeypatch):
    class _NoAssets:
        def movers(self, top=50):
            return {"gainers": [], "losers": []}
        def most_actives(self, top=100, by="volume"):
            return {"most_actives": []}

    class _MD:
        alpaca = _NoAssets()

    monkeypatch.setattr(runner_universe, "DEFAULT_UNIVERSE", ("AAPL",))
    monkeypatch.setattr(runner_universe, "liquid_universe", lambda *a, **k: ([], {}))
    at = datetime(2026, 10, 1, 10, 30, tzinfo=ET)
    syms, stats = runner_universe.build_universe(_MD(), {}, at=at, con=con)
    assert syms == ["AAPL"] and stats["instruments_classified"] is False


def test_below_liquidity_floor_judges_only_known_volume():
    assert runner_universe.below_liquidity_floor(22.0, 17_000) is True
    assert runner_universe.below_liquidity_floor(30.0, 1_000_000) is False
    assert runner_universe.below_liquidity_floor(None, 1_000_000) is False
    assert runner_universe.below_liquidity_floor(22.0, None) is False


# ===================================================================
# 3. The scan: completed bars only, plan at the bar the system acts on
# ===================================================================

def _trend_bars(n=20, start=datetime(2026, 10, 1, 9, 30), base=10.0, step=0.05):
    """A steady 5-minute uptrend: every bar green, price well above VWAP, the
    9 EMA rising and below price -- the runner conditions hold on every bar
    from index 9, so the walk-back reaches its 12-bar cap."""
    out = []
    for i in range(n):
        c = round(base + step * i, 4)
        out.append({"t": start + timedelta(minutes=5 * i), "o": c - 0.02,
                    "h": c + 0.01, "l": c - 0.03, "c": c, "v": 200_000})
    return out


class _ScanAlp:
    def __init__(self, bars, prev_close=9.6, prev_volume=5_000_000):
        self.bars, self.prev = bars, (prev_close, prev_volume)

    def multi_bars(self, syms, tf, start, end):
        return {s: list(self.bars) for s in syms}

    def multi_daily_bars(self, syms, a, b):
        return {s: [{"close": self.prev[0], "volume": self.prev[1]}] for s in syms}


class _ScanMD:
    def __init__(self, bars, profile=None, **prev):
        self.alpaca = _ScanAlp(bars, **prev)
        self.profile = profile or {"quote_type": "EQUITY", "sector": "Energy",
                                   "industry": "Oil & Gas Midstream"}

    def fundamentals(self, syms, con=None):
        return {s: {"float_shares": 40e6, **self.profile} for s in syms}

    def next_earnings(self, syms):
        return {s: 30 for s in syms}


def _scan(con, monkeypatch, md, at, tickers=("FLNG",)):
    monkeypatch.setattr(runner_universe, "build_universe",
                        lambda *a, **k: (list(tickers), {
                            "provider_degraded": False, "instruments_classified": True,
                            "names": {t: f"{t} Ltd." for t in tickers}}))
    runner_scan._GATE_CACHE.clear()
    cfg = {"min_rvol": 0.0, "min_trend_consistency": 45.0, "min_vwap_pct": 0.5,
           "price_min": 5.0, "price_max": 50.0, "require_rvol": True,
           "min_stop_abs": 0.10, "min_stop_pct": 0.005, "walkback": True,
           "null_gates": "dto", "scan_end_et_minutes": 900}
    return runner_scan.scan(md, cfg, at=at, con=con)


def test_completed_bars_drops_the_bar_still_forming():
    bars = _trend_bars(3)                     # 09:30, 09:35, 09:40
    at = datetime(2026, 10, 1, 9, 40, 2, tzinfo=ET)
    assert [b["t"].strftime("%H:%M") for b in runner_scan.completed_bars(bars, at)] == \
        ["09:30", "09:35"]
    aware = [{**b, "t": b["t"].replace(tzinfo=ET)} for b in bars]
    assert len(runner_scan.completed_bars(aware, at)) == 2


def test_plan_is_taken_at_the_last_completed_bar_not_the_walked_back_one(con, monkeypatch):
    """v2 took detection_price/stop/target at first_pass_et, walked back up to
    an hour; the fill was at the tick. v3: first_pass_et still walks back (a
    research timestamp), the plan is the last completed bar at the tick."""
    bars = _trend_bars(20)                                     # 09:30 .. 11:05
    forming = {"t": datetime(2026, 10, 1, 11, 10), "o": 11.0, "h": 13.5,
               "l": 11.0, "c": 13.4, "v": 3_000}                # 2 seconds old
    at = datetime(2026, 10, 1, 11, 10, 2, tzinfo=ET)
    out = _scan(con, monkeypatch, _ScanMD(bars + [forming]), at)
    assert out["candidates"] == 1
    row = ledger.runner_candidates_for_date(con, SESSION)[0]
    last = bars[-1]
    assert row["plan_bar_et"] == last["t"]
    assert row["detection_price"] == pytest.approx(last["c"])          # not 13.4
    assert row["first_pass_et"] < row["plan_bar_et"]                   # walked back
    assert json.loads(row["gates_status"])["walkback_bars"] > 0
    assert row["target_price"] == pytest.approx(
        row["detection_price"] + 2 * (row["detection_price"] - row["stop_price"]))
    assert row["scan_version"] == runner_scan.SCAN_VERSION == 3
    assert row["industry"] == "Oil & Gas Midstream"
    assert row["quote_type"] == "EQUITY"
    assert row["asset_name"] == "FLNG Ltd." and row["instrument_class"] == "common"


def test_a_survivor_yahoo_calls_an_etf_is_rejected(con, monkeypatch):
    at = datetime(2026, 10, 1, 11, 10, 2, tzinfo=ET)
    out = _scan(con, monkeypatch, _ScanMD(_trend_bars(20), profile={"quote_type": "ETF"}), at)
    assert out["candidates"] == 0
    assert out["rejections"]["rejected_instrument"] == 1


def test_a_closed_end_fund_survivor_is_rejected(con, monkeypatch):
    md = _ScanMD(_trend_bars(20), profile={"quote_type": "EQUITY",
                                           "industry": "Asset Management"})
    md.fundamentals = lambda syms, con=None: {s: {"float_shares": None, "quote_type": "EQUITY",
                                                  "industry": "Asset Management"} for s in syms}
    at = datetime(2026, 10, 1, 11, 10, 2, tzinfo=ET)
    out = _scan(con, monkeypatch, md, at, tickers=("DXYZ",))
    assert out["candidates"] == 0 and out["rejections"]["rejected_instrument"] == 1


def test_a_thin_name_is_rejected_by_the_liquidity_floor_in_the_scan(con, monkeypatch):
    at = datetime(2026, 10, 1, 11, 10, 2, tzinfo=ET)
    md = _ScanMD(_trend_bars(20), prev_close=22.0, prev_volume=17_000)
    out = _scan(con, monkeypatch, md, at, tickers=("LITG",))
    assert out["candidates"] == 0 and out["rejections"]["rejected_liquidity"] == 1


def test_session_telemetry_records_universe_stats_and_new_counters(con, monkeypatch):
    at = datetime(2026, 10, 1, 11, 10, 2, tzinfo=ET)
    _scan(con, monkeypatch, _ScanMD(_trend_bars(20)), at)
    sess = ledger.runner_session(con, SESSION)
    assert json.loads(sess["universe_stats"])["instruments_classified"] is True
    assert sess["rejected_liquidity"] == 0 and sess["rejected_instrument"] == 0


# ===================================================================
# 4. The simulator: sim v6, the runner's minimum remaining reward
# ===================================================================

def _min_bars(prices, start=time(10, 30)):
    t0 = datetime.combine(SESSION, start, tzinfo=ET)
    return [{"t": t0 + timedelta(minutes=i), "o": p, "h": p + 0.005, "l": p - 0.005,
             "c": p, "v": 10_000} for i, p in enumerate(prices)]


# PKE on 2026-10-01: plan 29.49 / stop 29.34 / target 29.78 (risk 0.15). The
# fill landed within a cent of the target and slippage made the "target" a loss.
PKE = {"pick_id": 0, "direction": "long", "entry_type": "open",
       "entry_price": 29.49, "stop_price": 29.34, "target_price": 29.78}


def test_a_fill_with_no_reward_left_is_invalid_for_the_runner():
    bars = _min_bars([29.762, 29.79, 29.80])
    legacy = grading.simulate_pick(PKE, bars, None, slippage_bps=5)
    assert legacy["exit_reason"] == "target" and legacy["r_planned"] < 0   # the defect
    sim = grading.simulate_pick(PKE, bars, None, slippage_bps=5,
                                min_reward_r=grading.RUNNER_MIN_REWARD_R)
    assert sim["exit_reason"] == "invalid_fill" and sim["triggered"] is False
    assert sim["invalid_reason"] == "stale_bracket"
    assert sim.get("r_planned") is None


def test_a_fill_with_enough_reward_left_still_trades():
    bars = _min_bars([29.55, 29.60, 29.70, 29.79])          # 1.5R of reward left
    sim = grading.simulate_pick(PKE, bars, None, slippage_bps=5,
                                min_reward_r=grading.RUNNER_MIN_REWARD_R)
    assert sim["triggered"] is True and sim["exit_reason"] == "target"
    assert sim["r_planned"] == pytest.approx((29.78 * 0.9995 - 29.55 * 1.0005) / 0.15, abs=1e-3)


def test_the_morning_lane_keeps_its_exact_target_gate():
    """sim v6 changes nothing for the morning lane: without min_reward_r a fill
    one cent under the target still trades, as under v5."""
    bars = _min_bars([29.762, 29.79])
    sim = grading.simulate_pick(PKE, bars, None, slippage_bps=5)
    assert sim["triggered"] is True
    past = grading.simulate_pick(PKE, _min_bars([29.80, 29.81]), None, slippage_bps=5)
    assert past["exit_reason"] == "invalid_fill" and past["invalid_reason"] == "no_reward_left"


def test_r_planned_caps_a_fill_near_a_stale_stop():
    """SEPN 2026-10-01: plan 38.89 / stop 38.61 / target 39.44; a fill 0.66R
    below the plan printed +7.46R on r_multiple. r_planned books the plan's
    reward from the plan entry, never the fill-to-stop ratio."""
    sepn = {"pick_id": 0, "direction": "long", "entry_type": "open",
            "entry_price": 38.89, "stop_price": 38.61, "target_price": 39.44}
    sim = grading.simulate_pick(sepn, _min_bars([38.706, 38.9, 39.2, 39.45]), None,
                                slippage_bps=5, min_reward_r=grading.RUNNER_MIN_REWARD_R)
    assert sim["exit_reason"] == "target"
    assert sim["r_multiple"] > 5                       # the inflated diagnostic
    assert sim["r_planned"] == pytest.approx((39.44 * 0.9995 - 38.89) / 0.28, abs=1e-3)
    assert grading.realized_r(sim) == sim["r_planned"]


def test_sim_version_semantics_map():
    """v7 changed the session exit for EVERY lane (the 15:55 flatten), so no
    older version grades any lane identically to the running simulator."""
    assert grading.CURRENT_SIM_VERSION == 7
    for lane in ("morning", "shadow", "runner", "live"):
        assert grading.same_semantics(7, lane)
        assert not grading.same_semantics(6, lane)
        assert not grading.same_semantics(5, lane)
    assert grading.same_semantics(None, "runner")


def test_grade_day_applies_the_runner_entry_time_to_runner_picks_only():
    """entry_after_map is keyed by ticker. Applied to every pick, a morning
    gap-and-go on a name the runner also detected was graded from 10:30."""
    bars = _min_bars([10.0] * 30 + [10.5] * 90, start=time(9, 30))

    class _MD:
        def minute_bars(self, t, d):
            return list(bars)
        def daily_bar(self, t, d):
            return None

    morning = {"pick_id": 1, "ticker": "XYZ", "direction": "long", "entry_type": "open",
               "entry_price": 10.0, "stop_price": 9.5, "target_price": 11.0,
               "setup_type": "gap_go"}
    runner = {**morning, "pick_id": 2, "setup_type": ledger.RUNNER_SETUP}
    cut = datetime(2026, 10, 1, 10, 0)
    out = grading.grade_day(_MD(), [morning, runner], SESSION, entry_after_map={"XYZ": cut})
    by = {o["pick_id"]: o for o in out}
    assert by[1]["entry_fill"] == pytest.approx(10.0)          # the open, not 10:00
    assert by[2]["entry_fill"] == pytest.approx(10.5)          # its runner entry


# ===================================================================
# 5. Selection: industry column, industry cap, live stale-bracket check
# ===================================================================

def _cand(ticker, industry, price=20.0, stop_dist=0.4):
    return {"ticker": ticker, "industry": industry, "detection_price": price,
            "stop_price": price - stop_dist, "target_price": price + 2 * stop_dist,
            "stop_dist": stop_dist, "first_pass_et": datetime(2026, 10, 1, 10, 25),
            "detection_minutes": 625}


SHIPPING = ["BWLP", "FLNG", "CMBT", "TEN", "TRMD"]


def test_industry_cap_keeps_the_top_ranked_names_per_industry():
    cands = ([_cand(t, "Oil & Gas Midstream") for t in SHIPPING]
             + [_cand("CMRE", "Marine Shipping"), _cand("EFXT", "Oil & Gas Equipment & Services"),
                _cand("NOIND", None)])
    selected = [{"ticker": t, "rank": i} for i, t in
                enumerate(["EFXT", "BWLP", "FLNG", "CMBT", "TEN", "TRMD", "CMRE", "NOIND"], 1)]
    kept, dropped = runner_selection.apply_industry_cap(selected, cands, 2)
    assert [k["ticker"] for k in kept] == ["EFXT", "BWLP", "FLNG", "CMRE", "NOIND"]
    assert [k["rank"] for k in kept] == [1, 2, 3, 4, 5]
    assert sorted(d["ticker"] for d in dropped) == ["CMBT", "TEN", "TRMD"]
    assert "Oil & Gas Midstream: CMBT, TEN, TRMD" in runner_selection._cap_note(dropped, 2)
    assert runner_selection.apply_industry_cap(selected, cands, 0) == (selected, [])


def test_candidate_table_shows_industry_and_stays_alphabetical():
    table = runner_selection.build_candidate_table(
        [_cand("ZZZ", "Banks - Regional"), _cand("AAA", "Oil & Gas Midstream")])
    lines = table.splitlines()
    assert lines[0].split(" | ")[:2] == ["ticker", "industry"]
    assert lines[1].startswith("AAA | Oil & Gas Midstream")
    assert lines[2].startswith("ZZZ | Banks - Regional")


def test_selector_prompt_states_the_cap_and_no_false_guarantees():
    from agent.llm import load_prompt, prompt_version
    text = load_prompt("runner_selector", {"MAX_PICKS": "10", "MAX_PER_INDUSTRY": "2",
                                           "REGIME_TAG": "x", "PLAYBOOK_SECTION": "",
                                           "CANDIDATE_TABLE": ""})
    assert prompt_version("runner_selector") == "2"
    assert "At most 2 picks may share one `industry`" in text
    assert "{{" not in text
    # v1 claimed every row's float was inside the band and RVOL above a
    # minimum; a null float passes and there is no RVOL minimum.
    assert "Relative volume at or above the configured minimum" not in text
    assert "A blank `float_m` means the float could not be" in text


def _sel_cfg(**over):
    runner = {"max_picks": 10, "selection_model": "m", "live_orders_enabled": False,
              "size_r": 0.25, "dollars_per_r": 100, "max_picks_per_industry": 2, **over}
    return Config(raw={"runner": runner, "live_paper": {"enabled": False}, "min_size_r": 0.25})


def _seed_candidates(con, specs):
    for t, ind in specs:
        c = _cand(t, ind)
        ledger.save_runner_candidate(con, {
            "session_date": SESSION, "ticker": t, "first_pass_et": c["first_pass_et"],
            "detection_minutes": 625, "scan_pass_et": datetime(2026, 10, 1, 10, 30),
            "detection_price": c["detection_price"], "stop_price": c["stop_price"],
            "target_price": c["target_price"], "stop_dist": c["stop_dist"],
            "industry": ind, "scan_version": 3})


def test_auto_selection_also_honours_the_industry_cap(con):
    _seed_candidates(con, [(t, "Oil & Gas Midstream") for t in SHIPPING[:4]])
    out = runner_selection.run_selection(_sel_cfg(), con=con, session_date=SESSION,
                                         at=datetime(2026, 10, 1, 10, 30))
    assert len(out["selected"]) == 2
    sess = ledger.runner_session(con, SESSION)
    assert "industry cap 2/industry dropped 2" in sess["selection_skipped_reason"]


def test_llm_selection_is_capped_in_code(con, monkeypatch):
    _seed_candidates(con, [(t, "Oil & Gas Midstream") for t in SHIPPING]
                     + [(f"B{i}", "Banks - Regional") for i in range(8)])

    class _LLM:
        def __init__(self, cfg):
            pass

        def call(self, **kw):
            assert "Oil & Gas Midstream" in kw["system"]     # the selector SEES industry
            picks = [{"ticker": t, "rank": i, "conviction": 3, "rationale": "x"}
                     for i, t in enumerate(SHIPPING + ["B0", "B1", "B2"], 1)]
            return type("R", (), {"text": json.dumps({"picks": picks}), "usage": {}})()

    monkeypatch.setattr(runner_selection, "LLMClient", _LLM)
    out = runner_selection.run_selection(_sel_cfg(), con=con, session_date=SESSION,
                                         at=datetime(2026, 10, 1, 10, 30))
    assert out["selected"] == ["BWLP", "FLNG", "B0", "B1"]
    assert sorted(out["industry_cap_dropped"]) == ["B2", "CMBT", "TEN", "TRMD"]
    stamped = {o["ticker"]: o for o in ledger.runner_outcomes_for_date(con, SESSION)}
    assert stamped["BWLP"]["selected"] and not stamped.get("CMBT")


@pytest.mark.parametrize("last,expected", [
    (None, None),                          # no live price: nothing to judge
    (29.50, None),                         # (29.78-29.50)/0.15 = 1.87R of reward left
    (29.70, None),                         # 0.53R left clears the 0.5R line
    (29.71, "stale bracket"),              # 0.47R left
    (29.34, "at/below the stop"),
])
def test_live_order_is_refused_on_a_stale_bracket(last, expected):
    why = runner_selection.stale_bracket_reason(last, 29.49, 29.34, 29.78)
    if expected is None:
        assert why is None
    else:
        assert expected in why


def test_place_live_orders_skips_a_stale_bracket_and_says_why(con, monkeypatch):
    cfg = Config(raw={"runner": {"live_orders_enabled": True, "max_live_orders_per_day": 10,
                                 "dollars_per_r": 100, "size_r": 0.25},
                      "live_paper": {"enabled": True}, "min_size_r": 0.25})
    sent = []
    monkeypatch.setattr(runner_selection.live_broker, "_request",
                        lambda *a, **k: {"buying_power": "100000"})
    monkeypatch.setattr(runner_selection.live_broker, "submit_bracket_order",
                        lambda *a, **k: sent.append(a[0]) or {"id": "o1", "status": "new"})
    monkeypatch.setattr(ledger, "save_live_order", lambda *a, **k: None)

    class _Alp:
        def snapshots(self, syms):
            return {"PKE": {"latestTrade": {"p": 29.775}}, "OKAY": {"latestTrade": {"p": 20.05}}}

    class _MD:
        alpaca = _Alp()

    pke = {"ticker": "PKE", "detection_price": 29.49, "stop_price": 29.34, "target_price": 29.78}
    okay = {"ticker": "OKAY", "detection_price": 20.0, "stop_price": 19.6, "target_price": 20.8}
    out = runner_selection._place_live_orders(cfg, con, SESSION, [(pke, 1), (okay, 2)], md=_MD())
    assert sent == ["OKAY"] and out["placed"] == 1
    assert any("PKE: stale bracket" in s for s in out["skipped"])


# ===================================================================
# 6. The unit of evidence: one (session, industry) event
# ===================================================================

def _row(t, ind, r, d=SESSION, **over):
    return {"ticker": t, "industry": ind, "r_planned": r, "session_date": d,
            "triggered": True, "selected": False, **over}


def test_rows_of_one_industry_on_one_session_are_one_unit():
    rows = ([_row(f"B{i}", "Banks - Regional", 0.3) for i in range(35)]
            + [_row("X", None, 1.0), _row("Y", None, -1.0)]
            + [_row("B99", "Banks - Regional", 0.3, d=date(2026, 10, 2))])
    assert clusters.unit_count(rows) == 4
    assert sorted(clusters.unit_values(rows, lambda r: r["r_planned"])) == [-1.0, 0.3, 0.3, 1.0]
    assert clusters.unit_count(rows, lane="morning") == len(rows)


def test_setup_edge_and_cells_count_units_not_rows():
    rows = [_row(f"B{i}", "Banks - Regional", 0.3) for i in range(30)]
    setup = runner_research.setup_edge(rows)
    assert setup["n"] == 1 and setup["n_rows"] == 30 and setup["underpowered"] is True
    cells = runner_research.candidate_comparisons(rows)
    assert all(c.n == 1 for c in cells)
    assert "1 session x industry units from 30 rows" in cells[0].note


def test_selection_edge_counts_six_tankers_as_one_selected_observation():
    rows = ([_row(t, "Oil & Gas Midstream", -0.1, selected=True) for t in SHIPPING]
            + [_row(f"F{i}", f"Industry {i}", 0.2) for i in range(10)])
    sel = runner_research.selection_edge(rows)
    assert sel["n_selected"] == 1 and sel["n_unselected"] == 10
    assert sel["underpowered"] is True


def test_forward_validation_records_one_observation_per_unit(con):
    from agent.stats import Comparison
    pred = {"lane": "runner", "conditions": [{"dimension": "runner_tc_bucket", "level": "75-85"}]}
    comp = Comparison("runner_tc_bucket", "75-85", 20, 0.5, 1.0)
    comp.q_value, comp.significant = 0.01, True
    reg = research.register_candidate(con, {"statement": "tc 75-85", "predicate": pred},
                                      date(2026, 10, 1), [comp], {})
    scan_v, sim_v = runner_research.current_versions()
    obs_day = date(2026, 10, 2)
    for i in range(6):          # six banks, one session, all in the cell
        t = f"B{i}"
        con.execute("""INSERT INTO runner_candidates (session_date, ticker, trend_consistency,
                       industry, scan_version) VALUES (?,?,?,?,?)""",
                    [obs_day, t, 80.0, "Banks - Regional", scan_v])
        ledger.save_runner_candidate_outcome(con, {
            "session_date": obs_day, "ticker": t, "eval_date": obs_day, "triggered": True,
            "r_multiple": 1.0, "r_planned": 0.5 + i * 0.1, "exit_reason": "eod",
            "sim_version": sim_v})
    assert research.observe_session(con, obs_day)["recorded"] == 1
    n, s = con.execute("SELECT forward_n, forward_sum_r FROM research_rules WHERE rule_id = ?",
                       [reg["rule_id"]]).fetchone()
    assert n == 1 and s == pytest.approx(0.75)        # the six rows' mean


# ===================================================================
# 7. Evening grading stores what the panel and the miner read
# ===================================================================

def test_evening_runner_grading_stores_post_move_and_invalid_reason(con, monkeypatch):
    from agent.loops import evening
    ledger.save_runner_candidate(con, {
        "session_date": SESSION, "ticker": "PKE", "first_pass_et": datetime(2026, 10, 1, 11, 40),
        "detection_minutes": 700, "scan_pass_et": datetime(2026, 10, 1, 12, 40),
        "plan_bar_et": datetime(2026, 10, 1, 12, 35),
        "detection_price": 29.49, "stop_price": 29.34, "target_price": 29.78,
        "stop_dist": 0.15, "industry": "Aerospace & Defense", "scan_version": 3})
    # 130 bars at 29.40 (10:30-12:39), then the name is already at 29.76 when
    # the 12:40 tick can act: 0.03R of the plan's reward is left.
    bars = _min_bars([29.40] * 130 + [29.76] * 10 + [29.80] * 50, start=time(10, 30))

    class _MD:
        def minute_bars(self, t, d):
            return list(bars)
        def daily_context(self, syms):
            return {"PKE": {"atr14": 0.6}}

    cfg = Config(raw={"slippage_bps": 5, "flatten_at": None, "min_bar_volume": 0})
    evening._grade_runner_candidates(con, _MD(), SESSION, cfg)
    o = ledger.runner_outcomes_for_date(con, SESSION)[0]
    assert o["exit_reason"] == "invalid_fill" and o["invalid_reason"] == "stale_bracket"
    assert o["session_close"] == pytest.approx(29.80)
    assert o["post_move_atr"] == pytest.approx((29.80 - 29.49) / 0.6, abs=1e-3)
    assert o["sim_version"] == grading.CURRENT_SIM_VERSION


# ===================================================================
# 8. Headline numbers read planned-risk R
# ===================================================================

def _pick_with_outcome(con, pid, r_multiple, r_planned, setup="gap_go", d=SESSION):
    con.execute("""INSERT INTO picks (pick_id, plan_date, ticker, direction, setup_type,
                   size_r, entry_price, stop_price, target_price)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [pid, d, f"T{pid}", "long", setup, 1.0, 10.0, 9.0, 12.0])
    ledger.save_outcome(con, {"pick_id": pid, "eval_date": d, "triggered": True,
                              "exit_reason": "stop", "r_multiple": r_multiple,
                              "r_planned": r_planned, "sim_version": 6})


def test_equity_curve_stats_streak_and_breaker_read_planned_r(con):
    """An adverse fill: entry 10, stop 9, filled 10.5, stopped at 9 -- the
    position (sized off the plan) lost 1.5R; r_multiple booked -1.0R."""
    _pick_with_outcome(con, 1, -1.0, -1.5)
    _pick_with_outcome(con, 2, 5.0, 1.9)           # a favorable fill near the stop
    curve = ledger.equity_curve(con, 100_000, 500)
    assert curve[-1]["cum_r"] == pytest.approx(0.4)
    stats = ledger.overall_stats(con)
    assert stats["avg_r"] == pytest.approx(0.2) and stats["total_weighted_r"] == pytest.approx(0.4)
    assert ledger.trailing_weighted_r(con, 10)["sum_weighted_r"] == pytest.approx(0.4)
    trades = ledger.recent_trades(con, 5)
    assert {t["r"] for t in trades} == {-1.5, 1.9}


def test_legacy_rows_without_r_planned_fall_back_to_r_multiple(con):
    _pick_with_outcome(con, 1, -1.0, None)
    assert ledger.overall_stats(con)["avg_r"] == pytest.approx(-1.0)


def test_schema_v26_columns_exist(con):
    def cols(t):
        return {c[0] for c in con.execute(f"DESCRIBE {t}").fetchall()}
    assert {"plan_bar_et", "asset_name", "instrument_class", "quote_type", "sector",
            "industry"} <= cols("runner_candidates")
    assert {"session_close", "post_move_atr", "invalid_reason"} <= cols("runner_candidate_outcomes")
    assert {"universe_stats", "rejected_liquidity", "rejected_instrument"} <= cols("runner_sessions")
    assert {"sector", "industry", "quote_type"} <= cols("fundamentals")


def test_pre_v26_fundamentals_rows_are_refetched_once(con):
    con.execute("""INSERT INTO fundamentals (symbol, float_shares, fetched_date)
                   VALUES ('OLD', 1e6, current_date)""")
    assert ledger.cached_fundamentals(con, ["OLD"]) == {}
    ledger.save_fundamentals(con, [{"symbol": "OLD", "float_shares": 1e6}])
    got = ledger.cached_fundamentals(con, ["OLD"])
    assert got["OLD"]["quote_type"] == ""        # fetched, Yahoo had none -- cached


# ===================================================================
# 9. Yahoo spells share classes with a dash
# ===================================================================

@pytest.mark.parametrize("sym,yahoo", [
    ("BF.B", "BF-B"), ("BRK.B", "BRK-B"), ("LGF.A", "LGF-A"), ("AAPL", "AAPL"),
    ("DX-Y.NYB", "DX-Y.NYB"), ("^N225", "^N225"), ("ES=F", "ES=F"), ("F.PRD", "F.PRD"),
])
def test_yf_symbol(sym, yahoo):
    from agent.providers import yf_symbol
    assert yf_symbol(sym) == yahoo


def test_batched_yahoo_history_is_keyed_by_our_symbol(monkeypatch):
    """BF.B was a 2026-10-01 candidate whose float, earnings and industry all
    came back empty: Yahoo 404s the dotted spelling."""
    import pandas as pd
    from agent import providers
    asked = {}

    class _YF:
        @staticmethod
        def download(symbols, **kw):
            asked["symbols"] = list(symbols)
            idx = pd.to_datetime(["2026-09-30", "2026-10-01"])
            cols = pd.MultiIndex.from_product([["BF-B", "AAPL"], ["Close", "Volume"]])
            return pd.DataFrame([[30.0, 1e6, 250.0, 5e7], [31.0, 2e6, 251.0, 6e7]],
                                index=idx, columns=cols)

    monkeypatch.setattr(providers, "_yf", lambda: _YF)
    closes = providers.yf_history_closes(["BF.B", "AAPL"], days=2)
    assert asked["symbols"] == ["AAPL", "BF-B"]
    assert closes == {"BF.B": [30.0, 31.0], "AAPL": [250.0, 251.0]}


# ===================================================================
# 10. Runner rules restart their forward clock under unit counting
# ===================================================================

def _rule(con, rid, lane, dims, state, q_at, fwd_n):
    con.execute(
        """INSERT INTO research_rules (rule_id, statement, dimensions, n_dimensions, state,
               source, discovered_at, quarantined_at, validated_at, forward_n_required,
               forward_n, forward_sum_r, forward_sumsq_r, lane, note)
           VALUES (?,?,?,1,?,'weekly',?,?,?,8,?,?,?,?,'')""",
        [rid, rid, json.dumps(dims), state, q_at, q_at,
         q_at if state == "VALIDATED" else None, fwd_n, 0.4 * fwd_n, 0.5 * fwd_n, lane])


def test_runner_rules_counted_per_row_restart_their_forward_clock(con):
    """A runner rule quarantined on 9/26 had collected its forward n one ROW at
    a time -- 30 correlated banks on one morning were 30 observations -- and
    could have graduated at the 10/3 weekly on that. It restarts at the first
    unit-counted session; a morning rule is untouched."""
    _rule(con, "R_RUN", "runner", ["runner_tc_bucket"], "QUARANTINED", date(2026, 9, 26), 40)
    _rule(con, "R_VAL", None, ["runner_rvol_bucket"], "VALIDATED", date(2026, 9, 19), 25)
    _rule(con, "R_NEW", "runner", ["runner_tc_bucket"], "QUARANTINED", date(2026, 10, 3), 0)
    _rule(con, "M_ONE", "morning", ["setup_type"], "QUARANTINED", date(2026, 9, 26), 9)
    restarted = research.restart_runner_forward_clocks(con)
    assert sorted(restarted) == ["R_RUN", "R_VAL"]
    states = {r["rule_id"]: r for r in research.rule_states(con)}
    for rid in ("R_RUN", "R_VAL"):
        assert states[rid]["state"] == "QUARANTINED"
        assert states[rid]["forward_n"] == 0
        assert states[rid]["quarantined_at"] == "2026-10-01"
        assert states[rid]["validated_at"] is None
        assert "forward clock restarted" in states[rid]["note"]
    assert "was VALIDATED" in states["R_VAL"]["note"]
    assert states["M_ONE"]["forward_n"] == 9 and states["M_ONE"]["quarantined_at"] == "2026-09-26"
    assert states["R_NEW"]["quarantined_at"] == "2026-10-03"
    assert research.restart_runner_forward_clocks(con) == []        # idempotent


def test_weekly_block_restarts_runner_clocks_before_promoting(con):
    from agent import integrity
    _rule(con, "R_RUN", "runner", ["runner_tc_bucket"], "QUARANTINED", date(2026, 9, 12), 60)
    integrity.weekly_block(con, Config(raw={}), ledger, date(2026, 10, 3))
    st = {r["rule_id"]: r for r in research.rule_states(con)}["R_RUN"]
    assert st["state"] == "QUARANTINED" and st["forward_n"] == 0
