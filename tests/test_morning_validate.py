"""
Pinning tests for agent/loops/morning.py's _validate_plan — specifically the
entry-type sanity gate (reference: agent/grading.py's invalid_fill gate, which
catches the resulting bad fill at grading time; this gate is meant to stop the
mistyped pick before it's ever sent to market).
"""
from __future__ import annotations

from agent.config import Config
from agent.loops.morning import _grounded_ref_prices, _reference_prices, _validate_plan


def _cfg(**overrides) -> Config:
    raw = {
        "max_picks": 4, "min_size_r": 0.25, "max_size_r": 1.5,
        "max_total_open_risk_r": 3.0, "drawdown_pause_r": -4.0,
    }
    raw.update(overrides)
    return Config(raw=raw)


def _plan(picks):
    return {"no_trade": False, "picks": picks}


def _pick(ticker="MARA", direction="long", entry_type="limit", entry=12.80,
          stop=11.90, target=14.60, size_r=0.5, conviction=4):
    return {
        "ticker": ticker, "direction": direction,
        "entry_plan": {"type": entry_type, "price": entry},
        "entry_price": entry, "stop_price": stop, "target_price": target,
        "size_r": size_r, "conviction": conviction,
    }


def test_reference_prices_pulls_from_gappers_and_movers():
    bundle = {
        "premarket_gappers": [{"symbol": "mara", "last": 11.75}],
        "movers": {"gainers": [{"symbol": "XYZ", "price": 5.0}], "losers": []},
    }
    refs = _reference_prices(bundle)
    assert refs == {"MARA": 11.75, "XYZ": 5.0}


def test_reference_prices_includes_catalyst_quotes():
    # News-sourced tickers (outside scan universe/movers) get quoted via
    # catalyst_quotes — the coverage hole behind the 2026-08-24 INSM incident.
    bundle = {
        "premarket_gappers": [{"symbol": "MARA", "last": 11.75}],
        "catalyst_quotes": [{"symbol": "INSM", "last": 125.4, "prev_close": 121.0},
                            {"symbol": "MARA", "last": 999.0}],  # never overrides gapper quote
    }
    refs = _reference_prices(bundle)
    assert refs == {"MARA": 11.75, "INSM": 125.4}


def test_long_limit_far_below_market_is_dropped_as_ungrounded():
    # INSM regression (2026-08-24): long limit @ 72.5 on a ~125 stock. Passes
    # the direction check (below market looks like pullback buying) but the
    # level is fiction — magnitude gate must kill it.
    plan = _plan([_pick(ticker="INSM", entry_type="limit",
                        entry=72.5, stop=70.0, target=77.5)])
    out, warnings = _validate_plan(plan, _cfg(), ref_prices={"INSM": 125.4})
    assert out["picks"] == []
    assert out["no_trade"] is True
    assert any("ungrounded" in w for w in warnings)


def test_far_off_entry_diagnosed_as_ungrounded_not_mistyped():
    # A breakout entry 40% above market is ungrounded, not merely "mistyped";
    # magnitude check must fire first so the warning says what actually broke.
    plan = _plan([_pick(entry_type="breakout", entry=16.5, stop=15.5, target=18.5)])
    out, warnings = _validate_plan(plan, _cfg(), ref_prices={"MARA": 11.75})
    assert out["picks"] == []
    assert any("ungrounded" in w for w in warnings)
    assert not any("reads as" in w for w in warnings)


def test_entry_within_distance_tolerance_survives():
    # 14% below market: deep but within the default 15% grounding tolerance.
    plan = _plan([_pick(entry_type="limit", entry=86.0, stop=83.0, target=93.0)])
    out, warnings = _validate_plan(plan, _cfg(), ref_prices={"MARA": 100.0})
    assert len(out["picks"]) == 1
    assert not any("ungrounded" in w for w in warnings)


def test_distance_tolerance_is_configurable():
    plan = _plan([_pick(entry_type="limit", entry=86.0, stop=83.0, target=93.0)])
    out, warnings = _validate_plan(plan, _cfg(max_entry_ref_distance_pct=10.0),
                                   ref_prices={"MARA": 100.0})
    assert out["picks"] == []
    assert any("ungrounded" in w for w in warnings)


def test_long_limit_priced_above_market_is_dropped_as_mistyped_breakout():
    # Same shape as the grading.py invalid_fill regression: a catalyst_momentum
    # breakout thesis typed as "limit" with an entry above where the stock is
    # actually trading. Caught here, it never reaches the ledger at all.
    plan = _plan([_pick(entry_type="limit", entry=12.80)])
    out, warnings = _validate_plan(plan, _cfg(), ref_prices={"MARA": 11.75})
    assert out["picks"] == []
    assert out["no_trade"] is True
    assert any("reads as breakout" in w for w in warnings)


def test_long_breakout_priced_above_market_survives():
    plan = _plan([_pick(entry_type="breakout", entry=12.80)])
    out, warnings = _validate_plan(plan, _cfg(), ref_prices={"MARA": 11.75})
    assert len(out["picks"]) == 1
    assert not any("reads as" in w for w in warnings)


def test_long_limit_priced_below_market_survives():
    plan = _plan([_pick(entry_type="limit", entry=11.50, stop=10.90, target=13.00)])
    out, warnings = _validate_plan(plan, _cfg(), ref_prices={"MARA": 11.75})
    assert len(out["picks"]) == 1
    assert not any("reads as" in w for w in warnings)


def test_short_breakout_priced_above_market_is_dropped_as_mistyped_limit():
    # short breakout should be below market (breaking DOWN through support);
    # priced above market instead, it reads as a limit order mistyped as breakout.
    plan = _plan([_pick(direction="short", entry_type="breakout", entry=12.50,
                        stop=13.50, target=9.00)])
    out, warnings = _validate_plan(plan, _cfg(), ref_prices={"MARA": 11.75})
    assert out["picks"] == []
    assert any("reads as limit" in w for w in warnings)


def test_unverifiable_price_is_dropped():
    # Fail closed: a ticker with no bundle reference AND no fetchable live
    # quote is dropped, not waved through. (This replaces the old "no
    # reference price skips the check" behavior — the exact loophole that
    # let the 2026-08-25 DKS hallucination reach the risk manager.)
    plan = _plan([_pick(ticker="OBSCURE", entry_type="limit", entry=12.80)])
    out, warnings = _validate_plan(plan, _cfg(), ref_prices={"MARA": 11.75})
    assert out["picks"] == []
    assert out["no_trade"] is True
    assert any("OBSCURE" in w and "could not be verified" in w for w in warnings)


def test_dks_regression_web_search_sourced_hallucinated_entry_is_dropped():
    # DKS incident (2026-08-25): earnings catalyst surfaced purely by the
    # analyst's own web search — absent from gappers/movers/news symbols —
    # short planned off a fictional "live price $179.64" (entry 182 / stop
    # 186.5 / target 174) while DKS actually traded ~$124-139. With the
    # pre-validation snapshot fetch merged into ref_prices, the magnitude
    # gate must kill it before the risk manager ever sees it.
    plan = _plan([_pick(ticker="DKS", direction="short", entry_type="limit",
                        entry=182.0, stop=186.5, target=174.0)])
    refs = {"MARA": 11.75, "DKS": 128.03}  # DKS live-fetched, not bundle-sourced
    out, warnings = _validate_plan(plan, _cfg(), ref_prices=refs)
    assert out["picks"] == []
    assert any("DKS" in w and "ungrounded" in w for w in warnings)


def test_fetched_quote_with_close_entry_survives():
    # No false positives: same web-search-sourced situation, but the entry is
    # legitimately near the live-fetched quote — must pass through untouched.
    plan = _plan([_pick(ticker="DKS", direction="short", entry_type="limit",
                        entry=130.0, stop=133.0, target=122.0)])
    out, warnings = _validate_plan(plan, _cfg(), ref_prices={"DKS": 128.03})
    assert len(out["picks"]) == 1
    assert not any("DKS" in w for w in warnings)


def test_grounded_ref_prices_fetches_only_missing_pick_tickers():
    # The merge layer: bundle-covered tickers are NOT re-fetched; proposed
    # tickers the bundle missed get one batched pick_quotes call, and
    # unfetchable ones stay absent (validator then fails closed).
    from datetime import date

    class StubMD:
        def __init__(self):
            self.asked = None

        def pick_quotes(self, symbols, d):
            self.asked = list(symbols)
            return {"DKS": 128.03}  # GONE intentionally unresolvable

    md = StubMD()
    bundle = {"premarket_gappers": [{"symbol": "MARA", "last": 11.75}]}
    plan = _plan([_pick(ticker="MARA"),
                  _pick(ticker="DKS", direction="short", entry=130.0,
                        stop=133.0, target=122.0),
                  _pick(ticker="GONE"),
                  _pick(ticker="DKS", direction="short", entry=130.0,
                        stop=133.0, target=122.0)])  # dupe: fetched once
    refs = _grounded_ref_prices(md, plan, bundle, date(2026, 8, 25))
    assert md.asked == ["DKS", "GONE"]
    assert refs == {"MARA": 11.75, "DKS": 128.03}


def test_grounded_ref_prices_fetch_failure_fails_closed_not_open():
    # An exploding quote fetch must leave the ticker absent (→ dropped by the
    # validator), never crash the morning run or silently skip the gate.
    from datetime import date

    class ExplodingMD:
        def pick_quotes(self, symbols, d):
            raise RuntimeError("alpaca down")

    plan = _plan([_pick(ticker="DKS", direction="short", entry=182.0,
                        stop=186.5, target=174.0)])
    refs = _grounded_ref_prices(ExplodingMD(), plan, {}, date(2026, 8, 25))
    assert refs == {}
    out, warnings = _validate_plan(plan, _cfg(), ref_prices=refs)
    assert out["picks"] == []
    assert any("could not be verified" in w for w in warnings)


def test_catalyst_quotes_fetches_only_uncovered_equity_news_symbols():
    # The bundle-side half of the INSM fix: news symbols not already quoted by
    # the gap scan / movers get one batched snapshot fetch; crypto pairs and
    # already-covered tickers are skipped.
    from agent.providers import MarketData

    class StubAlpaca:
        def __init__(self):
            self.asked = None

        def snapshots(self, symbols):
            self.asked = list(symbols)
            return {"INSM": {"latestTrade": {"p": 125.44},
                             "prevDailyBar": {"c": 121.0}}}

    md = MarketData.__new__(MarketData)
    md.alpaca = StubAlpaca()
    bundle = {
        "premarket_gappers": [{"symbol": "NVDA", "last": 99.8}],
        "movers": {"gainers": [{"symbol": "MRNA", "price": 101.5}], "losers": []},
        "news": [
            {"headline": "Insmed wins Japan approval", "symbols": ["INSM"]},
            {"headline": "NVDA raises guidance", "symbols": ["NVDA"]},      # covered by gappers
            {"headline": "Moderna endpoint hit", "symbols": ["MRNA"]},      # covered by movers
            {"headline": "BTC rips", "symbols": ["BTCUSD"]},                # not an equity symbol
            {"headline": "Insmed follow-up", "symbols": ["INSM"]},          # dedupe
        ],
    }
    quotes = md._catalyst_quotes(bundle)
    assert md.alpaca.asked == ["INSM"]
    assert quotes == [{"symbol": "INSM", "last": 125.44, "prev_close": 121.0,
                       "gap_pct": 3.67}]


def test_pick_quotes_batches_filters_and_returns_only_real_quotes():
    # Provider half of the DKS fix: arbitrary proposed tickers get one batched
    # snapshot; non-equity/malformed symbols are filtered, dupes collapse, and
    # symbols Alpaca can't quote are simply absent (caller fails closed).
    from datetime import date
    from types import SimpleNamespace

    from agent.providers import MarketData

    class StubAlpaca:
        def __init__(self):
            self.asked = None

        def snapshots(self, symbols):
            self.asked = list(symbols)
            return {"DKS": {"latestTrade": {"p": 128.032},
                            "prevDailyBar": {"c": 130.98}},
                    "BAD": "rate-limited"}

    md = MarketData.__new__(MarketData)
    md.alpaca = StubAlpaca()
    md.cfg = SimpleNamespace(offline=False)
    quotes = md.pick_quotes(["dks", "DKS", "BAD", "GONE", "BTCUSD", "BRK.B", ""],
                            date(2026, 8, 25))
    assert md.alpaca.asked == ["DKS", "BAD", "GONE"]
    assert quotes == {"DKS": 128.03}


def test_pick_quotes_without_alpaca_returns_nothing():
    # No data source → empty dict → every uncovered pick fails closed downstream.
    from datetime import date
    from types import SimpleNamespace

    from agent.providers import MarketData

    md = MarketData.__new__(MarketData)
    md.alpaca = None
    md.cfg = SimpleNamespace(offline=False)
    assert md.pick_quotes(["DKS"], date(2026, 8, 25)) == {}
