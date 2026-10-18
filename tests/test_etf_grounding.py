"""
D8 (docs/FIX_PHASE0.md): every fund the analyst can name has a live price in the bundle.

2026-10-15/16 the analyst proposed USO at 68.50 (it traded ~153.6), XLV at 148 (176.70) and
XLE at 87 (66.85). The bundle gave the sector ETFs as percent moves only and USO not at all,
so it priced them from memory; the 15% grounding gate killed each pick afterwards, which
fixes the order and not the cause. The gate's own quote was right every time. Now the bundle
carries an "ETF live prices" line, the gate judges against the same numbers, and the analyst
prompt says an ETF off that line may be named only with a quote it retrieved itself.

The three prices below are the grounding gate's quotes from those mornings.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from agent import llm
from agent import providers
from agent.config import Config
from agent.loops.morning import _reference_prices, _validate_plan
from agent.providers import (EXCLUDED_PRODUCTS, INDEX_ETFS, NAMEABLE_FUNDS, SECTOR_ETFS,
                             MarketData, render_bundle_md)

# the gate's live quotes on the mornings of the three misses
INCIDENT_PRICES = {"USO": 153.60, "XLV": 176.70, "XLE": 66.85}
# what the analyst proposed from memory
MEMORY_LEVELS = {"USO": (68.50, 66.00, 74.00), "XLV": (148.0, 145.0, 154.0), "XLE": (87.0, 84.5, 92.0)}


class _StubAlpaca:
    def __init__(self, prices=None, missing=()):
        self.prices = {**{s: 100.0 for s in NAMEABLE_FUNDS}, **INCIDENT_PRICES, **(prices or {})}
        self.missing = set(missing)
        self.asked: list[list[str]] = []

    def snapshots(self, symbols):
        self.asked.append(list(symbols))
        return {s: {"latestTrade": {"p": self.prices[s]}, "prevDailyBar": {"c": self.prices[s] * 0.99}}
                for s in symbols if s in self.prices and s not in self.missing}


def _md(alpaca=None):
    md = MarketData.__new__(MarketData)
    md.alpaca = alpaca if alpaca is not None else _StubAlpaca()
    md.cfg = SimpleNamespace(offline=False, data_cfg={"edgar_user_agent": "test"})
    md.feed_ok = True
    return md


def _bundle(md=None):
    quotes = (md or _md())._fund_quotes()
    return {"date": "2026-10-16", "generated_at": "2026-10-16T07:10:00-04:00", "fund_quotes": quotes,
            "yesterday_recap": {"date": "2026-10-15", "pct_change": {
                "SPY": 0.4, "QQQ": 0.6, "IWM": -0.3, "XLE": 1.2, "XLV": -0.4}}}


# ---------------------------------------------------------------- the funds are priced

def test_every_fund_the_analyst_can_name_is_quoted_in_one_call():
    alpaca = _StubAlpaca()
    quotes = _md(alpaca)._fund_quotes()
    assert alpaca.asked == [list(NAMEABLE_FUNDS)]                 # one batched snapshot call
    assert [q["symbol"] for q in quotes] == list(NAMEABLE_FUNDS)
    by = {q["symbol"]: q for q in quotes}
    for sym, px in INCIDENT_PRICES.items():
        assert by[sym]["last"] == px
    assert by["XLE"]["gap_pct"] == pytest.approx(1.01, abs=0.01)   # vs the prior close


def test_the_list_covers_the_sector_and_index_funds_and_the_incident_fund():
    assert set(SECTOR_ETFS) <= set(NAMEABLE_FUNDS) and set(INDEX_ETFS) <= set(NAMEABLE_FUNDS)
    assert "USO" in NAMEABLE_FUNDS                                  # not a sector ETF: it had no price at all
    assert len(NAMEABLE_FUNDS) == len(set(NAMEABLE_FUNDS))
    assert not set(NAMEABLE_FUNDS) & EXCLUDED_PRODUCTS              # no leveraged or inverse fund


def test_a_fund_the_feed_cannot_quote_is_absent_not_guessed():
    quotes = _md(_StubAlpaca(missing={"USO"}))._fund_quotes()
    assert "USO" not in [q["symbol"] for q in quotes] and len(quotes) == len(NAMEABLE_FUNDS) - 1


def test_a_dead_feed_raises_so_the_bundle_records_it():
    with pytest.raises(RuntimeError):
        _md(_StubAlpaca(missing=set(NAMEABLE_FUNDS)))._fund_quotes()
    no_alpaca = _md()
    no_alpaca.alpaca = None
    with pytest.raises(RuntimeError):
        no_alpaca._fund_quotes()


# ------------------------------------------------------------ what the analyst reads

def test_the_rendered_bundle_gives_the_incident_funds_a_price():
    text = render_bundle_md(_bundle())
    line = next(l for l in text.splitlines() if l.startswith("**ETF live prices"))
    for sym, px in INCIDENT_PRICES.items():
        assert f"{sym} {px}" in line, sym
    assert "percent moves, NOT prices" in text                     # the Sectors line says what it is
    assert "An ETF not listed here may be named only with a live quote you retrieved by search" in line


def test_a_bundle_whose_fund_fetch_failed_tells_the_analyst_to_name_no_etf():
    b = _bundle()
    b["fund_quotes"] = None
    text = render_bundle_md(b)
    assert "ETF live prices: UNAVAILABLE this morning" in text and "Name no ETF" in text


def test_a_bundle_that_never_had_the_feed_renders_no_fund_line():
    b = _bundle()
    del b["fund_quotes"]                                            # offline fixtures, stored history
    assert "ETF live prices" not in render_bundle_md(b)


def test_morning_bundle_carries_the_fund_quotes(monkeypatch):
    md = _md()
    monkeypatch.setattr(providers, "yf_quote_change", lambda m: [])
    monkeypatch.setattr(providers, "edgar_recent_8ks", lambda ua, count=30: [])
    md._vix = lambda: {"level": 16.0, "chg_5d_pct": 1.0}
    md._recap = lambda d: {"date": d.isoformat(), "pct_change": {"XLE": 1.2}}
    md._gappers = lambda: []
    md._movers = lambda: {"gainers": [], "losers": [], "most_active": []}
    md._news = lambda: []
    md._catalyst_quotes = lambda bundle: []
    md._enrich_indicators = lambda rows: None
    bundle = md.morning_bundle(date(2026, 10, 16))
    assert [q["symbol"] for q in bundle["fund_quotes"]] == list(NAMEABLE_FUNDS)
    assert not [m for m in bundle["missing"] if m.startswith("fund_quotes")]
    assert "XLE 66.85" in render_bundle_md(bundle)

    # ... and a dead feed is on the record, with the prompt told so
    md.alpaca = _StubAlpaca(missing=set(NAMEABLE_FUNDS))
    bundle = md.morning_bundle(date(2026, 10, 16))
    assert bundle["fund_quotes"] is None
    assert any(m.startswith("fund_quotes: RuntimeError") for m in bundle["missing"])
    assert "ETF live prices: UNAVAILABLE" in render_bundle_md(bundle)


# ----------------------------------------------------------------- what the gate reads

def test_the_gate_judges_against_the_price_the_analyst_was_shown():
    refs = _reference_prices(_bundle())
    for sym, px in INCIDENT_PRICES.items():
        assert refs[sym] == px
    # a gap-scan or news quote for the same fund wins, exactly as before
    both = _reference_prices({**_bundle(), "premarket_gappers": [{"symbol": "XLE", "last": 67.0}]})
    assert both["XLE"] == 67.0


def _plan(levels):
    return {"no_trade": False, "picks": [
        {"ticker": t, "direction": "long", "setup_type": "sector_rotation",
         "entry_plan": {"type": "limit", "price": e}, "entry_price": e, "stop_price": s,
         "target_price": tg, "size_r": 0.5, "conviction": 4} for t, (e, s, tg) in levels.items()]}


def _cfg():
    return Config(raw={"max_picks": 8, "min_size_r": 0.25, "max_size_r": 1.5,
                       "max_total_open_risk_r": 10.0, "drawdown_pause_r": -4.0})


def test_levels_priced_from_memory_are_still_killed_and_levels_on_the_quote_survive():
    refs = _reference_prices(_bundle())
    out, warnings = _validate_plan(_plan(MEMORY_LEVELS), _cfg(), ref_prices=refs)
    assert out["picks"] == [] and {d["ticker"] for d in out["validator_drops"]} == set(INCIDENT_PRICES)
    assert all("ungrounded" in d["reason"] for d in out["validator_drops"])
    # the same three ideas, priced off the line the analyst now reads
    anchored = {t: (round(px * 0.995, 2), round(px * 0.975, 2), round(px * 1.03, 2))
                for t, px in INCIDENT_PRICES.items()}
    out, _ = _validate_plan(_plan(anchored), _cfg(), ref_prices=refs)
    assert sorted(p["ticker"] for p in out["picks"]) == sorted(INCIDENT_PRICES)


# ---------------------------------------------------------------- what the prompt says

def test_the_analyst_prompt_is_version_14_and_prices_etfs_from_the_etf_line():
    assert llm.prompt_version("morning_analyst") == "14"
    text = llm.load_prompt("morning_analyst", {"COVERAGE_ANALYST": "", "COVERAGE_SECTION": "",
                                               "HYPOTHESIS_TAXONOMY": "", "HYPOTHESIS_SECTION": "",
                                               "HYPOTHESIS_FIELD": "", "HYPOTHESIS_TOPLEVEL": ""})
    assert 'or the\n  "ETF live prices" line)' in text
    assert "ETFs follow the same rule, with no exceptions" in text
    assert "percent moves only: it is not a quote" in text
    assert "if the line says the\n  prices are unavailable, name no ETF at all" in text


def test_a_snapshot_with_no_trade_is_skipped_not_priced_at_zero():
    class _NoTrade(_StubAlpaca):
        def snapshots(self, symbols):
            snaps = super().snapshots(symbols)
            snaps["SLV"] = {"prevDailyBar": {"c": 27.0}}        # a symbol that has not traded
            snaps["GLD"] = {"latestTrade": {"p": 0}, "minuteBar": {"c": 0}}
            return snaps

    quotes = {q["symbol"]: q for q in _md(_NoTrade())._fund_quotes()}
    assert "SLV" not in quotes and "GLD" not in quotes
    assert all(q["last"] > 0 for q in quotes.values())
