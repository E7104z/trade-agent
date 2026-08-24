"""
Pinning tests for agent/loops/morning.py's _validate_plan — specifically the
entry-type sanity gate (reference: agent/grading.py's invalid_fill gate, which
catches the resulting bad fill at grading time; this gate is meant to stop the
mistyped pick before it's ever sent to market).
"""
from __future__ import annotations

from agent.config import Config
from agent.loops.morning import _reference_prices, _validate_plan


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


def test_no_reference_price_skips_the_check():
    # Ticker outside the bundle's snapshot coverage -- unchecked, not dropped.
    plan = _plan([_pick(ticker="OBSCURE", entry_type="limit", entry=12.80)])
    out, warnings = _validate_plan(plan, _cfg(), ref_prices={"MARA": 11.75})
    assert len(out["picks"]) == 1


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
