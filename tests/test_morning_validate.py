"""
Pinning tests for agent/loops/morning.py's _validate_plan — specifically the
entry-type sanity gate (reference: agent/grading.py's invalid_fill gate, which
catches the resulting bad fill at grading time; this gate is meant to stop the
mistyped pick before it's ever sent to market).
"""
from __future__ import annotations

from agent.config import Config
from agent.loops.morning import (_add_exploration_pick, _build_rejections,
                                 _grounded_ref_prices, _reference_prices, _validate_plan)


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
    # (No atr_map/earnings_map supplied here, so the ATR/earnings gates fail
    # OPEN with an "unmeasured" warning — that's the documented null-asymmetry
    # behavior, not a grounding false positive; only grounding/mistype wording
    # is asserted absent.)
    plan = _plan([_pick(ticker="DKS", direction="short", entry_type="limit",
                        entry=130.0, stop=133.0, target=122.0)])
    out, warnings = _validate_plan(plan, _cfg(), ref_prices={"DKS": 128.03})
    assert len(out["picks"]) == 1
    assert not any("ungrounded" in w or "reads as" in w for w in warnings)


def test_leveraged_product_warns_but_survives_in_warn_mode():
    # Phase 1d: default is warn mode — tag first, gate second. SOXL is a 3x
    # semiconductor ETF; the pick survives but carries the gate warning.
    plan = _plan([_pick(ticker="SOXL", entry_type="limit",
                        entry=11.50, stop=10.90, target=13.00)])
    out, warnings = _validate_plan(plan, _cfg(), ref_prices={"SOXL": 11.75})
    assert [p["ticker"] for p in out["picks"]] == ["SOXL"]
    assert any("LEVERAGED GATE warn-only" in w for w in warnings)


def test_leveraged_product_dropped_in_enforce_mode():
    plan = _plan([_pick(ticker="SOXL", entry_type="limit",
                        entry=11.50, stop=10.90, target=13.00)])
    out, warnings = _validate_plan(plan, _cfg(leveraged_gate="enforce"),
                                   ref_prices={"SOXL": 11.75})
    assert out["picks"] == []
    assert any("SOXL" in w and "dropped" in w for w in warnings)


def test_warrant_shaped_symbol_hits_the_same_gate():
    # Addendum A3: the gate covers structural instruments too — GFAIW is a
    # warrant (production-observed 2026-08/09), not a common-stock candidate.
    plan = _plan([_pick(ticker="GFAIW", entry_type="limit",
                        entry=11.50, stop=10.90, target=13.00)])
    out, warnings = _validate_plan(plan, _cfg(), ref_prices={"GFAIW": 11.75})
    assert [p["ticker"] for p in out["picks"]] == ["GFAIW"]  # warn mode: survives
    assert any("warrant/unit/rights" in w for w in warnings)

    out2, warnings2 = _validate_plan(
        _plan([_pick(ticker="GFAIW", entry_type="limit",
                     entry=11.50, stop=10.90, target=13.00)]),
        _cfg(leveraged_gate="enforce"), ref_prices={"GFAIW": 11.75})
    assert out2["picks"] == []


def test_common_stock_pick_does_not_trigger_the_leveraged_gate():
    plan = _plan([_pick(entry_type="limit", entry=11.50, stop=10.90, target=13.00)])
    out, warnings = _validate_plan(plan, _cfg(), ref_prices={"MARA": 11.75})
    assert len(out["picks"]) == 1
    assert not any("LEVERAGED GATE" in w or "warrant/unit" in w for w in warnings)


def test_exploration_lane_reconsiders_only_risk_rejected_sub2_rr_candidate():
    candidates = {"candidates": [{
        "ticker": "MARA", "direction": "long", "setup_type": "gap_go",
        "entry_plan": {"type": "limit", "price": 11.50},
        "entry_price": 11.50, "stop_price": 10.90, "target_price": 12.60,
        "thesis": "fresh catalyst",
    }]}
    plan = {"no_trade": True, "picks": [],
            "rejected": [{"ticker": "MARA", "reason": "R:R 1.83 < 2.0"}]}
    assert _add_exploration_pick(candidates, plan, _cfg(
        exploration_lane={"enabled": True, "min_rr": 1.8, "max_rr": 2.0,
                          "max_picks": 1, "size_r": 0.25})) is True
    assert plan["picks"][0]["exploration_lane"] == "rr_below_control"
    assert plan["picks"][0]["size_r"] == 0.25
    assert plan["picks"][0]["exploration_rr"] == 1.8333
    assert plan["no_trade"] is False


def test_exploration_lane_stays_off_without_explicit_enablement():
    candidates = {"candidates": [{
        "ticker": "MARA", "entry_plan": {"type": "limit", "price": 11.50},
        "entry_price": 11.50, "stop_price": 10.90, "target_price": 12.60,
    }]}
    plan = {"no_trade": True, "picks": [],
            "rejected": [{"ticker": "MARA", "reason": "R:R 1.83 < 2.0"}]}
    assert _add_exploration_pick(candidates, plan, _cfg()) is False
    assert plan["picks"] == []


def test_rejection_provenance_records_stage_category_and_rr():
    candidates = {"candidates": [{
        "ticker": "MARA", "direction": "long", "setup_type": "gap_go",
        "entry_plan": {"type": "limit", "price": 11.50},
        "stop_price": 10.90, "target_price": 12.60,
    }]}
    plan = {"picks": [], "rejected": [{"ticker": "MARA", "reason": "R:R 1.83 < 2.0"}]}
    row = _build_rejections(candidates, plan)[0]
    assert row["source_stage"] == "risk_rejected"
    assert row["rejection_category"] == "rr_below_control"
    assert row["planned_rr"] == 1.8333


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


# --------------------------------------------------- indicator layer v2 gates

def test_noise_stop_dropped_in_enforce_mode():
    # stop 0.3 ATR from entry (ATR=1.0) is inside daily noise -- min_stop_atr default 0.35
    plan = _plan([_pick(entry_type="open", entry=100.0, stop=99.7, target=104.0)])
    out, warnings = _validate_plan(plan, _cfg(atr_gate="enforce"), ref_prices={"MARA": 100.0},
                                   atr_map={"MARA": 1.0})
    assert out["picks"] == []
    assert any("inside daily noise" in w and "dropped" in w for w in warnings)


def test_noise_stop_survives_at_half_atr():
    # stop 0.5 ATR from entry clears the 0.35 floor
    plan = _plan([_pick(entry_type="open", entry=100.0, stop=99.5, target=104.0)])
    out, warnings = _validate_plan(plan, _cfg(atr_gate="enforce"), ref_prices={"MARA": 100.0},
                                   atr_map={"MARA": 1.0})
    assert len(out["picks"]) == 1
    assert not any("inside daily noise" in w for w in warnings)


def test_noise_stop_warn_mode_never_drops():
    plan = _plan([_pick(entry_type="open", entry=100.0, stop=99.7, target=104.0)])
    out, warnings = _validate_plan(plan, _cfg(atr_gate="warn"), ref_prices={"MARA": 100.0},
                                   atr_map={"MARA": 1.0})
    assert len(out["picks"]) == 1
    assert any("warn-only" in w and "inside daily noise" in w for w in warnings)


def test_wide_stop_warns_but_passes():
    # 3 ATR stop (ATR=1.0) exceeds warn_stop_atr default 2.5 -- warns, doesn't drop
    plan = _plan([_pick(entry_type="open", entry=100.0, stop=97.0, target=110.0)])
    out, warnings = _validate_plan(plan, _cfg(), ref_prices={"MARA": 100.0}, atr_map={"MARA": 1.0})
    assert len(out["picks"]) == 1
    assert any("unusual" in w for w in warnings)


def test_atr_unavailable_skips_check_with_unmeasured_warning():
    plan = _plan([_pick(entry_type="open", entry=100.0, stop=99.7, target=104.0)])
    out, warnings = _validate_plan(plan, _cfg(atr_gate="enforce"), ref_prices={"MARA": 100.0},
                                   atr_map={})
    assert len(out["picks"]) == 1  # fails OPEN -- not dropped
    assert any("ATR unavailable" in w and "unmeasured" in w for w in warnings)


def test_earnings_today_dropped_in_enforce_mode_when_not_declared():
    plan = _plan([_pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)])
    out, warnings = _validate_plan(plan, _cfg(earnings_gate="enforce"), ref_prices={"MARA": 100.0},
                                   earnings_map={"MARA": 0})
    assert out["picks"] == []
    assert any("earnings today" in w and "dropped" in w for w in warnings)


def test_earnings_today_survives_as_declared_earnings_play():
    picks = [_pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)]
    picks[0]["setup_type"] = "earnings_drift"
    plan = _plan(picks)
    out, warnings = _validate_plan(plan, _cfg(earnings_gate="enforce"), ref_prices={"MARA": 100.0},
                                   earnings_map={"MARA": 0})
    assert len(out["picks"]) == 1
    assert not any("earnings today" in w and "dropped" in w for w in warnings)


def test_earnings_next_week_does_not_trigger_gate():
    plan = _plan([_pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)])
    out, warnings = _validate_plan(plan, _cfg(earnings_gate="enforce"), ref_prices={"MARA": 100.0},
                                   earnings_map={"MARA": 5})
    assert len(out["picks"]) == 1
    assert not any("earnings today" in w for w in warnings)


def test_earnings_gate_warn_mode_never_drops():
    plan = _plan([_pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)])
    out, warnings = _validate_plan(plan, _cfg(earnings_gate="warn"), ref_prices={"MARA": 100.0},
                                   earnings_map={"MARA": 0})
    assert len(out["picks"]) == 1
    assert any("warn-only" in w and "earnings today" in w for w in warnings)


def test_earnings_unavailable_skips_check_with_unmeasured_warning():
    plan = _plan([_pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)])
    out, warnings = _validate_plan(plan, _cfg(earnings_gate="enforce"), ref_prices={"MARA": 100.0},
                                   earnings_map={})
    assert len(out["picks"]) == 1
    assert any("earnings date unavailable" in w and "unmeasured" in w for w in warnings)


# --------------------------------------------- Phase 1f: fail-open telemetry

def test_gates_failed_open_all_zero_when_every_gate_has_data():
    import json as _json
    plan = _plan([_pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)])
    out, _ = _validate_plan(plan, _cfg(), ref_prices={"MARA": 100.0},
                            atr_map={"MARA": 1.0}, earnings_map={"MARA": 12})
    assert _json.loads(out["gates_failed_open"]) == {"atr": 0, "earnings": 0, "grounding": 0}
    assert out["provider_degraded"] is False


def test_atr_fail_open_is_counted_structurally():
    import json as _json
    plan = _plan([_pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)])
    out, _ = _validate_plan(plan, _cfg(), ref_prices={"MARA": 100.0}, atr_map={})
    assert _json.loads(out["gates_failed_open"])["atr"] == 1


def test_earnings_fail_open_is_counted_structurally():
    import json as _json
    plan = _plan([_pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)])
    out, _ = _validate_plan(plan, _cfg(), ref_prices={"MARA": 100.0}, earnings_map={})
    assert _json.loads(out["gates_failed_open"])["earnings"] == 1


def test_unverifiable_price_counted_as_grounding_fail_open():
    import json as _json
    plan = _plan([_pick(ticker="OBSCURE", entry_type="limit", entry=12.80)])
    out, _ = _validate_plan(plan, _cfg(), ref_prices={"MARA": 11.75})
    assert _json.loads(out["gates_failed_open"])["grounding"] == 1


def test_provider_degraded_true_when_a_gate_fails_open_for_more_than_half():
    # 2 of 3 proposed tickers missing ATR -> session-wide outage, not noise.
    plan = _plan([_pick(ticker="AAAA", entry_type="open", entry=100.0, stop=98.0, target=104.0),
                  _pick(ticker="BBBB", entry_type="open", entry=50.0, stop=49.0, target=53.0),
                  _pick(ticker="CCCC", entry_type="open", entry=20.0, stop=19.5, target=21.5)])
    out, _ = _validate_plan(plan, _cfg(), ref_prices={"AAAA": 100.0, "BBBB": 50.0, "CCCC": 20.0},
                            atr_map={"AAAA": 1.0})  # BBBB, CCCC missing -> 2/3
    assert out["provider_degraded"] is True


def test_provider_degraded_false_for_a_single_missing_atr_in_a_larger_book():
    plan = _plan([_pick(ticker="AAAA", entry_type="open", entry=100.0, stop=98.0, target=104.0),
                  _pick(ticker="BBBB", entry_type="open", entry=50.0, stop=49.0, target=53.0),
                  _pick(ticker="CCCC", entry_type="open", entry=20.0, stop=19.5, target=21.5)])
    out, _ = _validate_plan(plan, _cfg(), ref_prices={"AAAA": 100.0, "BBBB": 50.0, "CCCC": 20.0},
                            atr_map={"AAAA": 1.0, "BBBB": 1.0},  # only CCCC missing -> 1/3
                            earnings_map={"AAAA": 10, "BBBB": 10, "CCCC": 10})  # isolate the ATR gate
    assert out["provider_degraded"] is False


def test_gate_telemetry_still_stamped_on_no_trade_early_return():
    import json as _json
    plan = {"no_trade": True, "picks": []}
    out, _ = _validate_plan(plan, _cfg())
    assert _json.loads(out["gates_failed_open"]) == {"atr": 0, "earnings": 0, "grounding": 0}
    assert out["provider_degraded"] is False


def test_grounded_atr_uses_bundle_first_then_fetches_missing():
    class StubMD:
        def daily_context(self, symbols):
            self.asked = list(symbols)
            return {"DKS": {"atr14": 3.5}}

    md = StubMD()
    bundle = {"premarket_gappers": [{"symbol": "MARA", "atr14": 1.0}]}
    plan = _plan([_pick(ticker="MARA"), _pick(ticker="DKS", direction="short",
                        entry=130.0, stop=133.0, target=122.0)])
    from agent.loops.morning import _grounded_atr
    atr_map = _grounded_atr(md, plan, bundle)
    assert md.asked == ["DKS"]
    assert atr_map == {"MARA": 1.0, "DKS": 3.5}


def test_grounded_atr_fetch_failure_fails_open_not_closed():
    class ExplodingMD:
        def daily_context(self, symbols):
            raise RuntimeError("alpaca down")

    from agent.loops.morning import _grounded_atr
    plan = _plan([_pick(ticker="DKS", direction="short", entry=130.0, stop=133.0, target=122.0)])
    atr_map = _grounded_atr(ExplodingMD(), plan, {})
    assert atr_map == {}
    out, warnings = _validate_plan(plan, _cfg(atr_gate="enforce"), ref_prices={"DKS": 128.03},
                                   atr_map=atr_map)
    assert len(out["picks"]) == 1  # fails open: gate skipped, pick survives
    assert any("ATR unavailable" in w for w in warnings)


def test_grounded_earnings_uses_bundle_first_then_fetches_missing():
    class StubMD:
        def next_earnings(self, symbols):
            self.asked = list(symbols)
            return {"DKS": 0}

    md = StubMD()
    bundle = {"premarket_gappers": [{"symbol": "MARA", "earnings_days_away": 12}]}
    plan = _plan([_pick(ticker="MARA"), _pick(ticker="DKS", direction="short",
                        entry=130.0, stop=133.0, target=122.0)])
    from agent.loops.morning import _grounded_earnings
    earn_map = _grounded_earnings(md, plan, bundle)
    assert md.asked == ["DKS"]
    assert earn_map == {"MARA": 12, "DKS": 0}


def test_correlation_matrix_md_empty_below_two_tickers():
    from agent.loops.morning import _correlation_matrix_md
    assert _correlation_matrix_md([]) == ""
    assert _correlation_matrix_md(["MARA"]) == ""


def test_correlation_matrix_md_swallows_fetch_failure():
    import agent.loops.morning as morning_mod
    from agent.loops.morning import _correlation_matrix_md

    def _boom(*a, **k):
        raise RuntimeError("network down")

    orig = morning_mod.yf_history_closes
    morning_mod.yf_history_closes = _boom
    try:
        assert _correlation_matrix_md(["MARA", "DKS"]) == ""
    finally:
        morning_mod.yf_history_closes = orig


def test_pick_quotes_without_alpaca_returns_nothing():
    # No data source → empty dict → every uncovered pick fails closed downstream.
    from datetime import date
    from types import SimpleNamespace

    from agent.providers import MarketData

    md = MarketData.__new__(MarketData)
    md.alpaca = None
    md.cfg = SimpleNamespace(offline=False)
    assert md.pick_quotes(["DKS"], date(2026, 8, 25)) == {}
