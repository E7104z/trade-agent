"""
Pinning tests for agent/grading.py — the deterministic outcome simulator.

These are hand-written bar fixtures (plain dicts: o/h/l/c/v, matching the shape
returned by agent.providers.MarketData.minute_bars). No network access, no
mocks/ fixtures (those are for pipeline smoke tests, not surgical unit tests).

Each test pins one documented rule from the simulate_pick docstring/spec so a
future refactor (see Task 3: time_stop, flatten, thin-bar filtering) can tell
a deliberate semantics change from an accidental regression.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from agent.grading import CURRENT_SIM_VERSION, classify_shadow, grade_day, outcomes_md, simulate_pick


def _pick(direction="long", entry_type="open", entry=100.0, stop=98.0,
          target=104.0, pick_id=1, **extra):
    d = {
        "pick_id": pick_id,
        "direction": direction,
        "entry_type": entry_type,
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
    }
    d.update(extra)
    return d


def _bar(o, h, l, c, v=10_000):
    return {"o": o, "h": h, "l": l, "c": c, "v": v}


def _tbar(hh, mm, o, h, l, c, v=10_000):
    """A bar with a timestamp, for time_stop/flatten bar-index mapping tests."""
    return {"t": datetime(2026, 1, 2, hh, mm), "o": o, "h": h, "l": l, "c": c, "v": v}


def _flat_bars(hh0, mm0, n, o, h, l, c, v=1000):
    """n consecutive 1-minute bars, identical OHLCV, starting at hh0:mm0."""
    bars, hh, mm = [], hh0, mm0
    for _ in range(n):
        bars.append({"t": datetime(2026, 1, 2, hh, mm), "o": o, "h": h, "l": l, "c": c, "v": v})
        mm += 1
        if mm == 60:
            mm = 0
            hh += 1
    return bars


# --------------------------------------------------------------------- fills

def test_open_fill_uses_bar0_open():
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)
    bars = [_bar(100.0, 100.8, 99.5, 100.2)]
    out = simulate_pick(pick, bars, None)
    assert out["triggered"] is True
    assert out["entry_fill"] == 100.0


def test_limit_long_fills_at_entry_when_touched_without_gap():
    # low touches entry, open is above entry -> no favorable gap, fill at entry
    pick = _pick(entry_type="limit", entry=100.0, stop=98.0, target=104.0)
    bars = [_bar(100.5, 101.0, 99.8, 100.2)]
    out = simulate_pick(pick, bars, None)
    assert out["triggered"] is True
    assert out["entry_fill"] == 100.0


def test_limit_long_fills_at_open_on_favorable_gap():
    # open below entry -> gapped through favorably, fill at the (cheaper) open
    pick = _pick(entry_type="limit", entry=100.0, stop=98.0, target=104.0)
    bars = [_bar(99.0, 99.5, 98.5, 99.2)]
    out = simulate_pick(pick, bars, None)
    assert out["triggered"] is True
    assert out["entry_fill"] == 99.0


def test_breakout_long_fills_when_high_touches_entry():
    pick = _pick(entry_type="breakout", entry=100.0, stop=98.0, target=104.0)
    bars = [_bar(99.5, 100.5, 99.2, 100.1)]
    out = simulate_pick(pick, bars, None)
    assert out["triggered"] is True
    assert out["entry_fill"] == 100.0  # open (99.5) did not gap past entry


def test_limit_short_fills_at_open_on_favorable_gap():
    # short limit favorable gap = opening ABOVE entry (better sell price)
    pick = _pick(direction="short", entry_type="limit", entry=100.0, stop=102.0, target=96.0)
    bars = [_bar(101.0, 101.5, 100.5, 100.8)]
    out = simulate_pick(pick, bars, None)
    assert out["triggered"] is True
    assert out["entry_fill"] == 101.0


def test_breakout_short_fills_when_low_touches_entry():
    pick = _pick(direction="short", entry_type="breakout", entry=100.0, stop=102.0, target=96.0)
    bars = [_bar(100.5, 100.8, 99.5, 100.0)]
    out = simulate_pick(pick, bars, None)
    assert out["triggered"] is True
    assert out["entry_fill"] == 100.0  # open (100.5) did not gap past entry


# --------------------------------------------------------------- exit rules

def test_stop_and_target_same_bar_is_conservative_stop():
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)
    bars = [_bar(100.0, 105.0, 97.0, 101.0)]  # touches both stop and target
    out = simulate_pick(pick, bars, None)
    assert out["exit_reason"] == "stop"


def test_gap_through_stop_exits_at_open_not_stop():
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)
    bars = [
        _bar(100.0, 100.2, 99.5, 99.8),   # fill bar, no stop/target touch
        _bar(97.0, 97.5, 96.0, 97.2),     # gaps through the stop
    ]
    out = simulate_pick(pick, bars, None)
    assert out["exit_reason"] == "stop"
    assert out["exit_price"] == 97.0  # the bar's open, not the stop price (98.0)


def test_favorable_gap_fill_floors_effective_risk_at_quarter_planned():
    # entry 100, stop 99 -> planned risk 1.0. Gap-through fill lands at 99.05,
    # 0.05 from the stop -- far inside the 25% floor (0.25), so effective risk
    # is floored at planned_risk * 0.25 = 0.25, not the raw 0.05.
    pick = _pick(entry_type="limit", entry=100.0, stop=99.0, target=105.0)
    bars = [
        _bar(99.05, 99.5, 99.02, 99.3),   # fill @ 99.05; l=99.02 stays above stop (99)
        _bar(99.4, 105.5, 99.3, 105.2),   # touches target
    ]
    out = simulate_pick(pick, bars, None)
    assert out["entry_fill"] == 99.05
    assert out["exit_reason"] == "target"
    # (105 - 99.05) / 0.25 == 23.8 -- if risk had NOT been floored (0.05), this
    # would instead be (105-99.05)/0.05 = 119.0 (fantasy R).
    assert out["r_multiple"] == pytest.approx(23.8, abs=1e-3)
    # r_planned is bounded by the PLAN's geometry, not the fill: a 5R plan
    # (target 105 - entry 100 over risk 1) scores 5.0R even though the favorable
    # gap fill at 99.05 would have made a fill-anchored P&L 5.95R.
    assert out["r_planned"] == pytest.approx(5.0, abs=1e-3)


# ----------------------------------------------------------- target-exit boundary

def test_target_exit_scores_plan_geometry_regardless_of_fill_drift():
    # A 2R plan (entry 100, stop 98, target 104). Sweep the fill across the whole
    # legal band: r_planned must stay pinned to the plan's reward:risk (2.0)
    # instead of moving with the fill. This is the "target-exit boundary" the
    # planned-R metric exists to enforce.
    pick = _pick(entry_type="limit", entry=100.0, stop=98.0, target=104.0)
    for open_px in (98.3, 98.7, 99.0, 99.5, 100.0, 100.5, 101.0, 102.0, 103.0):
        bars = [_bar(open_px, 104.5, min(open_px, 99.8), 104.2)]
        out = simulate_pick(pick, bars, None)
        if out["exit_reason"] != "target":
            continue
        assert out["r_planned"] == pytest.approx(2.0, abs=1e-3), \
            "fill {} moved r_planned to {}".format(open_px, out["r_planned"])


@pytest.mark.parametrize("direction,entry,stop,target,open_px,exit_px,want", [
    # long market entry filled 1.0 above its plan: a target exit is worth
    # (104 - 101) / 2 = 1.5R of the dollars sized on the plan, not 2.0R.
    ("long", 100.0, 98.0, 104.0, 101.0, 104.0, 1.5),
    ("short", 100.0, 102.0, 96.0, 99.0, 96.0, 1.5),
])
def test_adverse_fill_is_never_hidden_by_planned_r(direction, entry, stop, target,
                                                   open_px, exit_px, want):
    # Production 2026-09-21..23: runner fills an hour after detection printed
    # +0.32R by the detection price while the trades lost money by the fill.
    pick = _pick(direction=direction, entry_type="open", entry=entry, stop=stop,
                 target=target)
    hi, lo = (max(open_px, exit_px) + 0.5, open_px - 0.1) if direction == "long" \
        else (open_px + 0.1, min(open_px, exit_px) - 0.5)
    out = simulate_pick(pick, [_bar(open_px, open_px + 0.1, open_px - 0.1, open_px),
                               _bar(open_px, hi, lo, exit_px)], None)
    assert out["exit_reason"] == "target"
    assert out["r_planned"] == pytest.approx(want, abs=1e-3)
    # eod_r_planned shares the basis, so management edge never mixes them.
    assert out["eod_r_planned"] == pytest.approx(want, abs=1e-3)


def test_planned_r_pays_entry_friction():
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)
    out = simulate_pick(pick, [_bar(100.0, 100.1, 99.9, 100.0),
                               _bar(100.0, 104.5, 99.9, 104.2)], None, slippage_bps=10)
    # fill 100.1 (friction), exit 104 * 0.999 = 103.896 -> (103.896 - 100.1) / 2
    assert out["r_planned"] == pytest.approx(1.898, abs=1e-3)


def test_short_target_exit_scores_about_planned_r():
    pick = _pick(direction="short", entry_type="open", entry=100.0,
                 stop=102.0, target=96.0)
    bars = [_bar(100.0, 100.2, 99.8, 100.0), _bar(99.9, 100.0, 95.5, 95.8)]
    out = simulate_pick(pick, bars, None)
    assert out["exit_reason"] == "target"
    assert out["r_planned"] == pytest.approx(2.0, abs=1e-3)  # (100 - 96) / 2


# ----------------------------------------------------------- untriggered/null

def test_never_triggered_populates_missed_move_and_baseline():
    pick = _pick(entry_type="limit", entry=100.0, stop=98.0, target=104.0)
    bars = [
        _bar(102.0, 103.0, 101.0, 102.5),
        _bar(102.5, 103.2, 102.0, 102.8),
    ]
    out = simulate_pick(pick, bars, None)
    assert out["triggered"] is False
    # best high across both bars = 103.2; risk = |100-98| = 2 -> (103.2-100)/2 = 1.6
    assert out["missed_move_r"] == pytest.approx(1.6, abs=1e-3)
    # baseline_r is still computed for untriggered picks: (close-open)/risk
    # sess_open = bars[0].o = 102.0, sess_close = bars[-1].c = 102.8
    assert out["baseline_r"] == pytest.approx(0.4, abs=1e-3)


def test_no_bars_is_no_data():
    pick = _pick()
    out = simulate_pick(pick, [], None)
    assert out["exit_reason"] == "no_data"
    assert out["triggered"] is False


def test_missing_target_is_invalid_plan():
    pick = _pick(target=None)
    bars = [_bar(100.0, 100.5, 99.5, 100.2)]
    out = simulate_pick(pick, bars, None)
    assert out["exit_reason"] == "invalid_plan"


def test_zero_risk_is_invalid_plan():
    pick = _pick(entry=100.0, stop=100.0)  # entry == stop -> risk <= 0
    bars = [_bar(100.0, 100.5, 99.5, 100.2)]
    out = simulate_pick(pick, bars, None)
    assert out["exit_reason"] == "invalid_plan"


def test_short_stop_and_target_walk_forward_symmetry():
    # short: stop above entry, target below entry -- walk-forward mirror of the
    # long stop/target branches (b["h"] >= stop, b["l"] <= target).
    pick = _pick(direction="short", entry_type="open", entry=100.0, stop=102.0, target=96.0)
    stop_bars = [
        _bar(100.0, 100.2, 99.8, 100.0),
        _bar(100.1, 102.5, 100.0, 102.3),  # high touches the stop
    ]
    out = simulate_pick(pick, stop_bars, None)
    assert out["exit_reason"] == "stop"
    assert out["exit_price"] == 102.0

    target_bars = [
        _bar(100.0, 100.2, 99.8, 100.0),
        _bar(99.9, 100.0, 95.5, 95.8),   # low touches the target
    ]
    out = simulate_pick(pick, target_bars, None)
    assert out["exit_reason"] == "target"
    assert out["exit_price"] == 96.0


def test_daily_bar_overrides_bar_derived_ohlc_and_baseline():
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)
    bars = [_bar(100.0, 100.5, 99.5, 100.2)]
    daily = {"open": 99.0, "high": 101.0, "low": 98.5, "close": 100.5, "volume": 500_000}
    out = simulate_pick(pick, bars, daily)
    assert out["day_open"] == 99.0
    assert out["day_close"] == 100.5
    assert out["day_volume"] == 500_000
    # baseline_r uses daily open/close (not bar open/close) when daily is provided:
    # (100.5 - 99.0) / risk(2.0) == 0.75
    assert out["baseline_r"] == pytest.approx(0.75, abs=1e-3)


def test_untouched_trade_exits_eod_at_last_close():
    pick = _pick(entry_type="open", entry=100.0, stop=90.0, target=120.0)  # far away
    bars = [
        _bar(100.0, 101.0, 99.0, 100.5),
        _bar(100.5, 101.2, 99.8, 100.9),
    ]
    out = simulate_pick(pick, bars, None)
    assert out["exit_reason"] == "eod"
    assert out["exit_price"] == 100.9  # bars[-1]["c"]


# ---------------------------------------------------------------- friction

def test_slippage_worsens_fill_and_exit_and_lowers_r_multiple():
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)
    bars = [
        _bar(100.0, 100.5, 99.5, 100.2),
        _bar(100.2, 104.5, 100.0, 104.3),  # touches target
    ]
    zero = simulate_pick(dict(pick), bars, None, slippage_bps=0.0)
    slipped = simulate_pick(dict(pick), bars, None, slippage_bps=50.0)  # 0.5%/side

    assert zero["exit_reason"] == "target"
    assert slipped["exit_reason"] == "target"
    # long: slippage worsens fill by making entry MORE expensive
    assert slipped["entry_fill"] > zero["entry_fill"]
    # and worsens exit by making the sale price LOWER
    assert slipped["exit_price"] < zero["exit_price"]
    assert slipped["r_multiple"] < zero["r_multiple"]


# ------------------------------------------------------- target-after-stop

def test_target_hit_after_stop_on_later_bar():
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)
    bars = [
        _bar(100.0, 100.2, 99.5, 100.0),
        _bar(99.9, 100.0, 97.5, 97.8),   # stops out here
        _bar(98.0, 104.5, 97.9, 104.2),  # target prints later, same day
    ]
    out = simulate_pick(pick, bars, None)
    assert out["exit_reason"] == "stop"
    assert out["target_hit_after_stop"] is True


def test_target_hit_after_stop_true_on_same_bar_tie():
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)
    bars = [_bar(100.0, 105.0, 97.0, 101.0)]  # same-bar stop+target touch
    out = simulate_pick(pick, bars, None)
    assert out["exit_reason"] == "stop"
    assert out["target_hit_after_stop"] is True


# --------------------------------------------------------------------- eod_r

def test_eod_r_uses_same_risk_denominator_as_r_multiple():
    # Same fixture as the fantasy-R guard test: risk is floored at 0.25.
    pick = _pick(entry_type="limit", entry=100.0, stop=99.0, target=105.0)
    bars = [
        _bar(99.05, 99.5, 99.02, 99.3),
        _bar(99.4, 105.5, 99.3, 105.2),
    ]
    out = simulate_pick(pick, bars, None)
    assert out["exit_reason"] == "target"
    assert out["r_multiple"] == pytest.approx(23.8, abs=1e-3)
    assert out["r_planned"] == pytest.approx(5.0, abs=1e-3)   # (105 - 100) / 1
    # eod_r = (last close - fill) / SAME floored risk (0.25):
    # (105.2 - 99.05) / 0.25 == 24.6
    assert out["eod_r"] == pytest.approx(24.6, abs=1e-3)
    # eod_r_planned twins r_planned in planned-risk units, against the entry:
    # (105.2 - 100) / 1 == 5.2
    assert out["eod_r_planned"] == pytest.approx(5.2, abs=1e-3)


# ---------------------------------------------------------------- classify_shadow

def test_classify_shadow_no_data_and_invalid_plan_are_unmeasurable():
    assert classify_shadow({"exit_reason": "no_data", "triggered": False})[0] == "unmeasurable"
    assert classify_shadow({"exit_reason": "invalid_plan", "triggered": False})[0] == "unmeasurable"


def test_classify_shadow_untriggered_is_scratch():
    kind, forgone = classify_shadow({"exit_reason": "no_trigger", "triggered": False})
    assert kind == "scratch"
    assert forgone == 0.0


def test_classify_shadow_noise_band_is_scratch():
    kind, _ = classify_shadow({"exit_reason": "eod", "triggered": True, "r_multiple": 0.1})
    assert kind == "scratch"
    kind, _ = classify_shadow({"exit_reason": "eod", "triggered": True, "r_multiple": -0.1})
    assert kind == "scratch"


def test_classify_shadow_would_have_won_and_lost():
    kind, r = classify_shadow({"exit_reason": "target", "triggered": True, "r_multiple": 1.5})
    assert kind == "would_have_won"
    assert r == 1.5
    kind, r = classify_shadow({"exit_reason": "stop", "triggered": True, "r_multiple": -1.0})
    assert kind == "would_have_lost"
    assert r == -1.0


# ------------------------------------------------------------- grade_day / md

class _FakeMarketData:
    """Minimal duck-typed stand-in for agent.providers.MarketData — grade_day
    only calls .minute_bars() and .daily_bar()."""

    def __init__(self, bars):
        self._bars = bars

    def minute_bars(self, ticker, d):
        return self._bars

    def daily_bar(self, ticker, d):
        return None


def test_grade_day_and_outcomes_md_smoke():
    picks = [{
        "pick_id": 1, "ticker": "TEST", "direction": "long", "setup_type": "gap_go",
        "entry_type": "open", "entry_price": 100.0, "stop_price": 98.0,
        "target_price": 104.0, "size_r": 1.0, "conviction": 4,
        "time_stop": None, "thesis": "test thesis", "invalidation": "test invalidation",
    }]
    bars = [_bar(100.0, 100.5, 99.5, 100.2)]
    md = _FakeMarketData(bars)
    outcomes = grade_day(md, picks, __import__("datetime").date(2026, 1, 2))
    assert len(outcomes) == 1
    assert outcomes[0]["pick_id"] == 1
    assert outcomes[0]["eval_date"] is not None

    md_text = outcomes_md(picks, outcomes)
    assert "TEST" in md_text
    assert "OUTCOME" in md_text


# ============================================================================
# Task 3: time_stop, flatten_at, min_bar_volume (all pin NEW behavior; the
# tests above pin the Task 2 baseline and must keep passing unchanged).
# ============================================================================

# ------------------------------------------------------------- time_stop

def test_time_stop_exits_at_bar_close_using_bar_timestamps():
    pick = _pick(entry_type="open", entry=100.0, stop=95.0, target=110.0,
                 time_stop="by 09:32")
    bars = [
        _tbar(9, 30, 100.0, 100.5, 99.5, 100.2),
        _tbar(9, 31, 100.2, 100.6, 99.8, 100.4),
        _tbar(9, 32, 100.4, 100.8, 100.0, 100.6),  # time_stop cutoff reached here
        _tbar(9, 33, 100.6, 115.0, 100.0, 112.0),  # target would hit later -- must not matter
    ]
    out = simulate_pick(pick, bars, None)
    assert out["exit_reason"] == "time_stop"
    assert out["exit_price"] == 100.6  # the time-stop bar's close, not the stop or target


def test_time_stop_ordering_stop_wins_on_same_bar():
    pick = _pick(entry_type="open", entry=100.0, stop=95.0, target=110.0,
                 time_stop="by 09:31")
    bars = [
        _tbar(9, 30, 100.0, 100.5, 99.5, 100.2),
        _tbar(9, 31, 100.0, 100.5, 94.0, 95.0),   # time_stop cutoff AND stop touched
    ]
    out = simulate_pick(pick, bars, None)
    assert out["exit_reason"] == "stop"
    assert out["exit_price"] == 95.0


def test_unparseable_time_stop_falls_back_to_no_time_stop():
    # legacy/stale shape (a duration, not a clock time) -- must not crash, and
    # must simulate as though no time stop were set at all.
    pick = _pick(entry_type="open", entry=100.0, stop=90.0, target=120.0,
                 time_stop="2 days")
    bars = [
        _tbar(9, 30, 100.0, 100.5, 99.5, 100.2),
        _tbar(9, 31, 100.2, 100.6, 99.8, 100.4),
    ]
    out = simulate_pick(pick, bars, None)
    assert out["exit_reason"] == "eod"
    assert out["exit_price"] == 100.4


def test_time_stop_positional_fallback_when_bars_have_no_timestamp():
    # no "t" key on any bar -> fall back to a positional offset from bars[0],
    # assuming consecutive 1-minute bars starting at the 9:30 open.
    pick = _pick(entry_type="open", entry=100.0, stop=95.0, target=110.0,
                 time_stop="by 09:32")  # 2 minutes after 9:30 open -> index 2
    bars = [
        _bar(100.0, 100.5, 99.5, 100.2),
        _bar(100.2, 100.6, 99.8, 100.4),
        _bar(100.4, 100.8, 100.0, 100.6),  # index 2 -> time_stop bar
        _bar(100.6, 115.0, 100.0, 112.0),
    ]
    out = simulate_pick(pick, bars, None)
    assert out["exit_reason"] == "time_stop"
    assert out["exit_price"] == 100.6


# ------------------------------------------------------------- flatten_at (default off, explicitly enabled here)

def test_flatten_at_disabled_by_default_has_no_effect():
    pick = _pick(entry_type="open", entry=100.0, stop=95.0, target=110.0)
    bars = [
        _tbar(9, 30, 100.0, 100.5, 99.5, 100.2),
        _tbar(9, 31, 100.2, 100.6, 99.8, 100.4),
    ]
    out = simulate_pick(pick, bars, None)  # flatten_at not passed -> default None
    assert out["exit_reason"] == "eod"


def test_flatten_at_exits_open_position_at_its_close_when_enabled():
    pick = _pick(entry_type="open", entry=100.0, stop=95.0, target=110.0)
    bars = [
        _tbar(9, 30, 100.0, 100.5, 99.5, 100.2),
        _tbar(9, 31, 100.2, 100.6, 99.8, 100.4),  # flatten cutoff reached here
        _tbar(9, 32, 100.4, 120.0, 100.0, 118.0),  # target would hit later -- must not matter
    ]
    out = simulate_pick(pick, bars, None, flatten_at="09:31")
    assert out["exit_reason"] == "flatten"
    assert out["exit_price"] == 100.4


def test_flatten_wins_tie_against_time_stop_on_same_bar():
    pick = _pick(entry_type="open", entry=100.0, stop=95.0, target=110.0,
                 time_stop="by 09:31")
    bars = [
        _tbar(9, 30, 100.0, 100.5, 99.5, 100.2),
        _tbar(9, 31, 100.2, 100.6, 99.8, 100.4),  # both flatten and time_stop land here
    ]
    out = simulate_pick(pick, bars, None, flatten_at="09:31")
    assert out["exit_reason"] == "flatten"


# ------------------------------------------------------------- min_bar_volume (default off, explicitly enabled here)

def test_min_bar_volume_disabled_by_default_lets_thin_bar_trigger_fill():
    pick = _pick(entry_type="limit", entry=100.0, stop=98.0, target=104.0)
    bars = [
        _bar(101.0, 101.5, 100.5, 101.2, v=10_000),  # doesn't touch entry
        _bar(99.0, 99.5, 98.5, 99.2, v=1_000),       # thin bar, gaps through favorably
        _bar(100.0, 100.3, 99.6, 100.1, v=10_000),
    ]
    out = simulate_pick(pick, bars, None)  # min_bar_volume not passed -> default 0
    assert out["triggered"] is True
    assert out["entry_fill"] == 99.0  # filled on the thin bar, favorable gap honored


def test_min_bar_volume_excludes_thin_bar_from_trigger_when_enabled():
    pick = _pick(entry_type="limit", entry=100.0, stop=98.0, target=104.0)
    bars = [
        _bar(101.0, 101.5, 100.5, 101.2, v=10_000),  # doesn't touch entry
        _bar(99.0, 99.5, 98.5, 99.2, v=1_000),       # thin -- excluded from trigger logic
        _bar(100.0, 100.3, 99.6, 100.1, v=10_000),   # fill happens here instead
    ]
    out = simulate_pick(pick, bars, None, min_bar_volume=5_000)
    assert out["triggered"] is True
    assert out["entry_fill"] == 100.0  # NOT the thin bar's favorable 99.0 gap-fill


def test_min_bar_volume_excludes_thin_bar_from_stop_check_when_enabled():
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)
    bars = [
        _bar(100.0, 100.2, 99.8, 100.0, v=10_000),
        _bar(99.9, 100.0, 97.0, 99.5, v=1_000),   # touches stop, but thin
        _bar(99.5, 100.0, 99.0, 99.7, v=10_000),  # no stop touch, not thin
    ]
    out_disabled = simulate_pick(dict(pick), bars, None)  # default: thin filter off
    assert out_disabled["exit_reason"] == "stop"
    assert out_disabled["exit_price"] == 98.0

    out_enabled = simulate_pick(dict(pick), bars, None, min_bar_volume=5_000)
    assert out_enabled["exit_reason"] == "eod"
    assert out_enabled["exit_price"] == 99.7


def test_min_bar_volume_still_counts_thin_bars_for_mfe_mae():
    # thin bars are excluded from trigger/stop/target/gap logic but MUST still
    # count toward MFE/MAE and remain in the series for indexing/eod close.
    pick = _pick(entry_type="open", entry=100.0, stop=90.0, target=120.0)  # far away, no touch
    bars = [
        _bar(100.0, 100.2, 99.8, 100.0, v=10_000),
        _bar(99.9, 100.0, 92.0, 99.5, v=1_000),   # thin, but a real 92.0 low print
        _bar(99.5, 100.0, 99.0, 99.7, v=10_000),
    ]
    out = simulate_pick(pick, bars, None, min_bar_volume=5_000)
    assert out["exit_reason"] == "eod"
    # mae_r reflects the thin bar's low (92.0), not just the non-thin bars:
    # (92.0 - 100.0) / risk(10.0) == -0.8
    assert out["mae_r"] == pytest.approx(-0.8, abs=1e-3)


# ------------------------------------------------------------- sim_version (Task 4)

def test_simulate_pick_stamps_current_sim_version():
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)
    bars = [_bar(100.0, 100.5, 99.5, 100.2)]
    out = simulate_pick(pick, bars, None)
    assert out["sim_version"] == CURRENT_SIM_VERSION


def test_simulate_pick_stamps_sim_version_even_on_short_circuit_exits():
    # no_data and invalid_plan short-circuit before the walk-forward loop --
    # sim_version must still be present so every row is attributable to a
    # semantics version, not just the ones that reach a full walk.
    assert simulate_pick(_pick(), [], None)["sim_version"] == CURRENT_SIM_VERSION
    assert simulate_pick(_pick(target=None), [_bar(100, 100.5, 99.5, 100.2)], None)["sim_version"] == CURRENT_SIM_VERSION


# ------------------------------------------------------- fill validity gate (v3)
# Reproduces the class of bug where a long "limit" order priced above the
# market (should have been typed "breakout") gaps through on the open and
# fills BELOW its own stop -- structurally not a trade.

def test_long_limit_priced_above_market_rejects_fill_below_stop():
    # entry 12.80 (should've been a breakout order), stop 11.90. The session
    # opens at 11.75 -- already under the stop -- so the "limit" fills there.
    pick = _pick(direction="long", entry_type="limit", entry=12.80, stop=11.90,
                 target=14.60, pick_id=4)
    bars = [_bar(11.75, 12.46, 11.70, 12.10)]
    out = simulate_pick(pick, bars, None, slippage_bps=5.0)
    assert out["triggered"] is False
    assert out["exit_reason"] == "invalid_fill"
    assert out["r_multiple"] is None
    assert out["entry_fill"] == pytest.approx(11.75 * 1.0005, abs=1e-4)
    assert "wrong side of" in out["invalid_fill_detail"]


def test_short_limit_priced_below_market_rejects_fill_above_stop():
    # mirror: short limit entry 90, stop 92, but the open gaps up to 93 --
    # already past the stop for a short.
    pick = _pick(direction="short", entry_type="limit", entry=90.0, stop=92.0, target=80.0)
    bars = [_bar(93.0, 93.5, 92.5, 93.2)]
    out = simulate_pick(pick, bars, None)
    assert out["triggered"] is False
    assert out["exit_reason"] == "invalid_fill"
    assert out["r_multiple"] is None


def test_invalid_fill_is_excluded_from_shadow_ledger_stats():
    out = simulate_pick(_pick(direction="long", entry_type="limit", entry=12.80,
                              stop=11.90, target=14.60),
                        [_bar(11.75, 12.46, 11.70, 12.10)], None)
    assert out["exit_reason"] == "invalid_fill"
    kind, forgone = classify_shadow(out)
    assert kind == "unmeasurable"
    assert forgone is None


def test_valid_fill_far_from_plan_is_flagged_execution_anomaly_not_rejected():
    # breakout long: entry 100, stop 98 (planned risk 2.0). Price gaps to open
    # at 110 -- a real, valid fill (well clear of the stop) but 5R away from
    # the planned entry level, so the "thesis at 100" was never actually tested.
    # The target sits above the fill: a fill already past its target is a
    # different case (invalid_fill since sim_version 4 -- see
    # tests/test_loop_hardening.py), and under the old semantics this very
    # fixture with target=104 graded as a "target" exit at -0.5R.
    pick = _pick(direction="long", entry_type="breakout", entry=100.0, stop=98.0, target=120.0)
    bars = [_bar(110.0, 112.0, 109.5, 111.0)]
    out = simulate_pick(pick, bars, None)
    assert out["triggered"] is True
    assert out["exit_reason"] != "invalid_fill"
    assert out["execution_anomaly"] is True
    assert out["fill_vs_plan_r"] == pytest.approx(5.0, abs=1e-3)
    assert out["r_multiple"] is not None  # still graded -- it's a real trade


def test_fill_close_to_plan_is_not_flagged_anomalous():
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)
    bars = [_bar(100.0, 100.5, 99.5, 100.2)]
    out = simulate_pick(pick, bars, None)
    assert out["execution_anomaly"] is False


def test_invalid_fill_outcome_renders_in_outcomes_md_without_a_narrative():
    picks = [{
        "pick_id": 4, "ticker": "MARA", "direction": "long", "setup_type": "catalyst_momentum",
        "entry_type": "limit", "entry_price": 12.80, "stop_price": 11.90,
        "target_price": 14.60, "size_r": 0.5, "conviction": 4,
        "time_stop": None, "thesis": "test thesis", "invalidation": "test invalidation",
    }]
    outcomes = [simulate_pick(picks[0], [_bar(11.75, 12.46, 11.70, 12.10)], None, slippage_bps=5.0)]
    outcomes[0]["eval_date"] = None
    md_text = outcomes_md(picks, outcomes)
    assert "INVALID FILL" in md_text
    assert "do not construct a narrative" in md_text


# ---------------------------------------------------- indicator layer (spec v1)

def test_indicator_fields_default_to_none_shape():
    pick = _pick(entry_type="open")
    bars = [_bar(100.0, 100.5, 99.5, 100.2)]
    out = simulate_pick(pick, bars, None)
    for key in ("adv20", "rvol_at_1000", "rvol_at_1200", "rvol_close",
                "vwap_crosses", "pct_time_above_vwap",
                "first_vwap_reclaim_et", "first_vwap_reject_et"):
        assert key in out


def test_no_data_pick_has_null_indicator_fields_not_a_crash():
    pick = _pick(entry_type="open")
    out = simulate_pick(pick, [], None)
    assert out["exit_reason"] == "no_data"
    assert out["vwap_crosses"] is None
    assert out["vwap_at_fill"] is None


def test_untriggered_pick_keeps_session_indicators_but_not_fill_fields():
    # limit entry far below anything the single bar touches -> never fills
    bars = [_bar(100.0, 101.0, 99.0, 100.5)]
    pick = _pick(entry_type="limit", entry=50.0, stop=45.0, target=60.0)
    out = simulate_pick(pick, bars, None, adv20=1000.0)
    assert out["triggered"] is False
    assert out["vwap_at_fill"] is None
    assert out["vwap_side_at_fill"] is None
    assert out["ema9_5m_loss_et"] is None
    # session-level VWAP fields are computable from the bars regardless of trigger
    assert out["pct_time_above_vwap"] is not None


def test_vwap_side_at_fill_and_distance_computed_from_fill_bar():
    # typical price (h+l+c)/3 = (110+100+100)/3 = 103.3333, the only bar so
    # vwap[0] = 103.3333; entry_type=open fills at the bar's OPEN (100.0),
    # which sits below that -> "below" and a negative % distance.
    bar0 = {"t": datetime(2026, 1, 2, 9, 30), "o": 100.0, "h": 110.0, "l": 100.0, "c": 100.0, "v": 100}
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)
    out = simulate_pick(pick, [bar0], None)
    assert out["vwap_at_fill"] == pytest.approx(103.3333, abs=1e-3)
    assert out["vwap_side_at_fill"] == "below"
    assert out["fill_vs_vwap_pct"] == pytest.approx(-3.2258, abs=1e-2)


def test_rvol_checkpoints_use_cumulative_volume_and_adv20():
    # 35 one-minute bars from 9:30 -> 10:04; the first bar at/after 10:00 is
    # index 30 (9:30 + 30min), so cumulative volume through it is 31*1000.
    bars = _flat_bars(9, 30, 35, 100.0, 100.5, 99.5, 100.0, v=1000)
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)
    out = simulate_pick(pick, bars, None, adv20=31_000.0)
    assert out["rvol_at_1000"] == pytest.approx(1.0)
    assert out["rvol_at_1200"] is None  # bars never reach noon
    assert out["rvol_close"] == pytest.approx(35_000 / 31_000, abs=1e-3)


def test_rvol_checkpoints_use_full_session_bars_when_entry_sliced():
    # A runner pick detected intraday is simulated on bars sliced to start at
    # its entry time (entry_type "open" fills at the sliced bars[0]). Its RVOL
    # checkpoints must still read the WHOLE session: RVOL@10:00 = cumulative
    # volume through 10:00 / ADV20, NOT the first (post-entry) bar's volume —
    # the latter was the historical bug that printed a nonsense 0.00x for every
    # runner pick (single bar / ADV20 ~ 0.001).
    full = _flat_bars(9, 30, 35, 100.0, 100.5, 99.5, 100.0, v=1000)  # 9:30->10:04
    sliced = [b for b in full if b["t"] >= datetime(2026, 1, 2, 10, 3)]
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)
    out = simulate_pick(pick, sliced, None, adv20=31_000.0, session_bars=full)
    # 9:30->10:00 inclusive = 31 bars -> 31,000 shares -> exactly 1.0x ADV20.
    assert out["rvol_at_1000"] == pytest.approx(1.0)
    # RVOL@close = FULL session volume (35 bars), not entry->close (2 bars).
    assert out["rvol_close"] == pytest.approx(35_000 / 31_000, abs=1e-3)
    # The fill lands on the 10:03 bar; its VWAP must be read off the full
    # session at that bar, not off sliced index 0.
    assert out["vwap_at_fill"] is not None


def test_simulate_pick_reads_session_stats_from_the_full_session_not_the_sliced_bars():
    full = _flat_bars(9, 30, 35, 100.0, 100.5, 99.5, 100.0, v=1000)
    sliced = [b for b in full if b["t"].replace(tzinfo=None) >= datetime(2026, 1, 2, 10, 3)]
    out = simulate_pick(_pick(ticker="MARA", entry_type="open", pick_id=1), sliced, None,
                        adv20=31_000.0, session_bars=full, session_date=date(2026, 1, 2))
    # The fill walks the sliced bars, but RVOL@10:00 must read the whole session
    # rather than collapsing to the first post-entry bar's volume.
    assert out["rvol_at_1000"] == pytest.approx(1.0)
    assert out["rvol_close"] == pytest.approx(35_000 / 31_000, abs=1e-3)


def test_missing_adv20_leaves_rvol_fields_null_but_vwap_fields_populate():
    bars = _flat_bars(9, 30, 5, 100.0, 100.5, 99.5, 100.0)
    pick = _pick(entry_type="open")
    out = simulate_pick(pick, bars, None, adv20=None)
    assert out["adv20"] is None
    assert out["rvol_at_1000"] is None
    assert out["rvol_close"] is None
    assert out["vwap_crosses"] is not None


def test_ema9_5m_loss_et_detected_after_fill():
    # 45 flat 1-min bars (9 five-minute buckets, closes all 100) seed the 9
    # EMA at bucket index 8 (=100); a 10th bucket (5 bars closing at 90,
    # starting 10:15) pulls the EMA to 98 while itself closing at 90 -- a
    # long loses the EMA there, and nowhere earlier (flat tape, no wick).
    bars = (_flat_bars(9, 30, 45, 100.0, 100.5, 99.5, 100.0)
            + _flat_bars(10, 15, 5, 90.0, 91.0, 89.0, 90.0))
    pick = _pick(entry_type="open", entry=100.0, stop=50.0, target=200.0)
    out = simulate_pick(pick, bars, None)
    assert out["ema9_5m_loss_et"] == datetime(2026, 1, 2, 10, 15)


def test_ema9_5m_loss_et_none_when_never_lost():
    bars = _flat_bars(9, 30, 45, 100.0, 100.5, 99.5, 100.0)
    pick = _pick(entry_type="open", entry=100.0, stop=50.0, target=200.0)
    out = simulate_pick(pick, bars, None)
    assert out["ema9_5m_loss_et"] is None


def test_grade_day_passes_per_ticker_adv20_into_simulate_pick():
    class StubMD:
        def minute_bars(self, ticker, d):
            return _flat_bars(9, 30, 35, 100.0, 100.5, 99.5, 100.0, v=1000)

        def daily_bar(self, ticker, d):
            return None

    picks = [_pick(ticker="MARA", entry_type="open", pick_id=1)]
    outcomes = grade_day(StubMD(), picks, date(2026, 1, 2), adv20_map={"MARA": 31_000.0})
    assert outcomes[0]["rvol_at_1000"] == pytest.approx(1.0)


# -------------------------------------------------- indicator layer v2 (ATR/levels)

def test_v2_fields_default_to_none_shape():
    pick = _pick(entry_type="open")
    bars = [_bar(100.0, 100.5, 99.5, 100.2)]
    out = simulate_pick(pick, bars, None)
    # or15_high/or15_low/fill_vs_or/mae_before_mfe ARE computable from even a
    # single bar (a degenerate 1-bar opening range, and the pick triggers) --
    # checked separately below, not part of this "nothing supplied" check.
    # time_to_mfe_min stays None here because this bar carries no timestamp.
    for key in ("atr14", "stop_dist_atr", "target_dist_atr", "gap_atr",
                "or_break_dir", "time_to_mfe_min", "gap_fill_frac_close",
                "trend_side_200d", "ext_atr_at_entry", "short_pct_float"):
        assert key in out and out[key] is None


def test_stop_and_target_dist_atr_computed_from_planned_levels():
    # entry 100, stop 98 (risk 2), target 106 (risk-to-target 6); atr14=2.0
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=106.0)
    bars = [_bar(100.0, 100.5, 99.5, 100.2)]
    out = simulate_pick(pick, bars, None, atr14=2.0)
    assert out["atr14"] == 2.0
    assert out["stop_dist_atr"] == pytest.approx(1.0)
    assert out["target_dist_atr"] == pytest.approx(3.0)


def test_gap_atr_and_gap_fill_frac_computed_from_prev_close():
    # prev_close 95, day open 100 -> gap = 5; atr14 = 2.5 -> gap_atr = 2.0
    daily = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 97.5, "volume": 1000}
    bars = _flat_bars(9, 30, 5, 100.0, 100.5, 99.5, 97.5)
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=110.0)
    out = simulate_pick(pick, bars, daily, atr14=2.5, prev_close=95.0)
    assert out["gap_atr"] == pytest.approx(2.0)
    # gap=5, close checkpoint price=97.5 -> retraced (100-97.5)/5 = 0.5
    assert out["gap_fill_frac_close"] == pytest.approx(0.5)


def test_trend_and_fundamentals_stamped_from_caller_inputs():
    pick = _pick(entry_type="open")
    bars = [_bar(100.0, 100.5, 99.5, 100.2)]
    trend = {"above_ma200": True, "ext_atr": 1.8}
    fund = {"short_pct_float": 18.0, "float_shares": 41_000_000.0, "mkt_cap": 2.4e12}
    out = simulate_pick(pick, bars, None, trend_snapshot=trend, fundamentals=fund)
    assert out["trend_side_200d"] == "above"
    assert out["ext_atr_at_entry"] == pytest.approx(1.8)
    assert out["short_pct_float"] == 18.0
    assert out["float_shares"] == 41_000_000.0
    assert out["mkt_cap"] == 2.4e12


def test_trend_side_below_and_none():
    pick = _pick(entry_type="open")
    bars = [_bar(100.0, 100.5, 99.5, 100.2)]
    out_below = simulate_pick(pick, bars, None, trend_snapshot={"above_ma200": False})
    assert out_below["trend_side_200d"] == "below"
    out_none = simulate_pick(pick, bars, None, trend_snapshot={"above_ma200": None})
    assert out_none["trend_side_200d"] is None


def test_opening_range_and_fill_vs_or_above():
    # 15-bar OR at 100-101, then a breakout fill above it
    or_bars = _flat_bars(9, 30, 15, 100.0, 101.0, 100.0, 100.5)
    fill_bar = [_tbar(9, 45, 101.5, 103.0, 101.4, 102.8)]
    bars = or_bars + fill_bar
    pick = _pick(entry_type="breakout", entry=102.0, stop=99.0, target=110.0)
    out = simulate_pick(pick, bars, None, or_minutes=15)
    assert out["or15_high"] == 101.0
    assert out["or15_low"] == 100.0
    assert out["triggered"] is True
    assert out["fill_vs_or"] == "above"


def test_mae_before_mfe_true_when_worst_prints_first():
    # dips to a low first, then rallies to the high -- worst before best
    pick = _pick(entry_type="open", entry=100.0, stop=90.0, target=120.0)
    bars = [
        _tbar(9, 30, 100.0, 100.2, 99.8, 100.0),
        _tbar(9, 31, 99.9, 100.0, 95.0, 96.0),    # worst print (MAE) here
        _tbar(9, 32, 96.5, 115.0, 96.0, 114.0),   # best print (MFE) here
    ]
    out = simulate_pick(pick, bars, None)
    assert out["mae_before_mfe"] is True
    assert out["time_to_mfe_min"] == pytest.approx(2.0)


def test_mae_before_mfe_false_when_best_prints_first():
    pick = _pick(entry_type="open", entry=100.0, stop=90.0, target=120.0)
    bars = [
        _tbar(9, 30, 100.0, 100.2, 99.8, 100.0),
        _tbar(9, 31, 100.1, 115.0, 100.0, 114.0),  # best print (MFE) here
        _tbar(9, 32, 113.0, 113.5, 95.0, 96.0),    # worst print (MAE) here
    ]
    out = simulate_pick(pick, bars, None)
    assert out["mae_before_mfe"] is False


def test_grade_day_passes_v2_maps_through_to_simulate_pick():
    class StubMD:
        def minute_bars(self, ticker, d):
            return [_bar(100.0, 100.5, 99.5, 100.2)]

        def daily_bar(self, ticker, d):
            return {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.2, "volume": 1000}

    picks = [_pick(ticker="MARA", entry_type="open", pick_id=1)]
    outcomes = grade_day(
        StubMD(), picks, date(2026, 1, 2),
        atr_map={"MARA": 2.0},
        trend_map={"MARA": {"above_ma200": True, "ext_atr": 1.5}},
        fundamentals_map={"MARA": {"short_pct_float": 20.0}},
        prev_close_map={"MARA": 95.0},
    )
    assert outcomes[0]["atr14"] == 2.0
    assert outcomes[0]["trend_side_200d"] == "above"
    assert outcomes[0]["short_pct_float"] == 20.0
    assert outcomes[0]["gap_atr"] == pytest.approx(2.5)  # |100-95|/2.0


def test_outcomes_md_renders_v2_context_line():
    picks = [{
        "pick_id": 10, "ticker": "NVDA", "direction": "long", "setup_type": "gap_go",
        "entry_type": "open", "entry_price": 100.0, "stop_price": 98.0,
        "target_price": 106.0, "size_r": 1.0, "conviction": 4,
        "time_stop": None, "thesis": "t", "invalidation": "i",
    }]
    bars = [_bar(100.0, 100.5, 99.5, 100.2)]
    outcomes = [simulate_pick(picks[0], bars, None, atr14=2.0,
                              trend_snapshot={"above_ma200": True, "ext_atr": 1.8},
                              fundamentals={"short_pct_float": 18.0, "mkt_cap": 2.4e12})]
    outcomes[0]["eval_date"] = None
    md_text = outcomes_md(picks, outcomes)
    assert "Context (mechanically computed" in md_text
    assert "ATR" in md_text
    assert "above 200d MA" in md_text
    assert "short 18% of float" in md_text
    assert "mcap $2.4T" in md_text


def test_outcomes_md_renders_indicator_line_for_triggered_pick():
    picks = [{
        "pick_id": 9, "ticker": "AAPL", "direction": "long", "setup_type": "gap_go",
        "entry_type": "open", "entry_price": 100.0, "stop_price": 98.0,
        "target_price": 104.0, "size_r": 1.0, "conviction": 4,
        "time_stop": None, "thesis": "t", "invalidation": "i",
    }]
    bar0 = {"t": datetime(2026, 1, 2, 9, 30), "o": 100.0, "h": 110.0, "l": 100.0, "c": 100.0, "v": 100}
    outcomes = [simulate_pick(picks[0], [bar0], None)]
    outcomes[0]["eval_date"] = None
    md_text = outcomes_md(picks, outcomes)
    assert "vwap" in md_text.lower()
    assert "below VWAP" in md_text or "below vwap" in md_text.lower()


# ------------------------------------------------------------- half-day calendar (Phase 1c)

HALF_DAY = date(2026, 11, 27)   # Friday after Thanksgiving — 13:00 ET close
FULL_DAY = date(2026, 11, 30)   # the following Monday — normal 16:00 close


def test_market_close_et_and_is_half_day_calendar():
    from datetime import time

    from agent.util import is_half_day, market_close_et
    assert is_half_day(HALF_DAY) is True
    assert market_close_et(HALF_DAY) == time(13, 0)
    assert is_half_day(FULL_DAY) is False
    assert market_close_et(FULL_DAY) == time(16, 0)


def test_half_day_time_stop_after_close_exits_at_the_1300_close():
    # time_stop "by 14:00" on a 13:00-close session cannot fire — the position
    # exits at the actual session close (eod), never against a nonexistent bar.
    # sim_version 7: the desk's session exit is the live monitor's flatten,
    # 5 minutes before the 13:00 close -- the OPEN of the 12:55 bar.
    pick = _pick(entry_type="open", entry=100.0, stop=95.0, target=110.0,
                 time_stop="by 14:00")
    bars = [
        _tbar(12, 53, 100.0, 100.5, 99.5, 100.2),
        _tbar(12, 54, 100.2, 100.6, 99.8, 100.4),
        _tbar(12, 55, 100.3, 100.7, 99.9, 100.6),  # flatten bar: out at its open
        _tbar(12, 59, 100.6, 100.9, 100.1, 100.8),  # last bar of the short session
    ]
    out = simulate_pick(pick, bars, None, session_date=HALF_DAY)
    assert out["session_length"] == "half"
    assert out["exit_reason"] == "eod"
    assert out["exit_price"] == 100.3


def test_half_day_time_stop_before_close_still_fires_normally():
    pick = _pick(entry_type="open", entry=100.0, stop=95.0, target=110.0,
                 time_stop="by 12:00")
    bars = [
        _tbar(11, 58, 100.0, 100.5, 99.5, 100.2),
        _tbar(12, 0, 100.2, 100.6, 99.8, 100.4),   # time_stop bar
        _tbar(12, 1, 100.4, 120.0, 100.0, 118.0),  # target would hit later — must not matter
    ]
    out = simulate_pick(pick, bars, None, session_date=HALF_DAY)
    assert out["exit_reason"] == "time_stop"
    assert out["exit_price"] == 100.4


def test_half_day_flatten_1555_is_recorded_inert_not_silently_dropped():
    pick = _pick(entry_type="open", entry=100.0, stop=95.0, target=110.0)
    bars = [
        _tbar(12, 53, 100.0, 100.5, 99.5, 100.2),
        _tbar(12, 55, 100.3, 100.7, 99.9, 100.6),  # the desk's half-day flatten (v7)
        _tbar(12, 59, 100.6, 100.9, 100.1, 100.8),
    ]
    out = simulate_pick(pick, bars, None, flatten_at="15:55", session_date=HALF_DAY)
    assert out["flatten_inert"] is True
    assert out["exit_reason"] == "eod"
    assert out["exit_price"] == 100.3


def test_full_day_active_flatten_sets_inert_false_and_session_full():
    pick = _pick(entry_type="open", entry=100.0, stop=95.0, target=110.0)
    bars = [
        _tbar(9, 30, 100.0, 100.5, 99.5, 100.2),
        _tbar(9, 31, 100.2, 100.6, 99.8, 100.4),  # flatten cutoff reached here
    ]
    out = simulate_pick(pick, bars, None, flatten_at="09:31", session_date=FULL_DAY)
    assert out["session_length"] == "full"
    assert out["flatten_inert"] is False
    assert out["exit_reason"] == "flatten"


def test_session_length_set_even_when_there_is_no_data():
    out = simulate_pick(_pick(), [], None, session_date=HALF_DAY)
    assert out["exit_reason"] == "no_data"
    assert out["session_length"] == "half"


def test_no_session_date_keeps_legacy_behavior_and_null_session_length():
    # Legacy callers that don't thread a session date must see no change:
    # session_length stays None and no clamping is applied beyond the 16:00 default.
    pick = _pick(entry_type="open", entry=100.0, stop=95.0, target=110.0,
                 time_stop="by 14:00")
    bars = [_bar(100.0, 100.5, 99.5, 100.2)]
    out = simulate_pick(pick, bars, None)
    assert out["session_length"] is None
    assert out["flatten_inert"] is None


def test_grade_day_threads_session_date_into_session_length():
    class StubMD:
        def minute_bars(self, sym, d):
            return [_bar(100.0, 100.5, 99.5, 100.2)]

        def daily_bar(self, sym, d):
            return None

    picks = [dict(_pick(), ticker="AAPL", direction="long")]
    outcomes = grade_day(StubMD(), picks, HALF_DAY)
    assert outcomes[0]["session_length"] == "half"





# ---------------------------------------------------------------------------
# sim_version 7: the session exit is the live desk's 15:55 flatten
# ---------------------------------------------------------------------------

def _full_session(prices_by_minute):
    """1-minute bars 09:30-15:59 with a flat price unless overridden."""
    from datetime import datetime, timedelta
    t0 = datetime(FULL_DAY.year, FULL_DAY.month, FULL_DAY.day, 9, 30)
    out = []
    for i in range(390):
        t = t0 + timedelta(minutes=i)
        p = prices_by_minute.get(t.strftime("%H:%M"), 100.0)
        out.append({"t": t, "o": p, "h": p + 0.05, "l": p - 0.05, "c": p, "v": 10_000})
    return out


def test_a_held_position_exits_at_the_1555_open_like_the_live_monitor():
    """The monitor flattens at 15:55; the simulator held to the 15:59 close,
    so every hold-to-close trade carried four minutes the account never held."""
    pick = _pick(entry_type="open", entry=100.0, stop=95.0, target=110.0)
    bars = _full_session({"15:55": 101.0, "15:56": 104.0, "15:59": 104.0})
    out = simulate_pick(pick, bars, None, session_date=FULL_DAY)
    assert out["exit_reason"] == "eod"
    assert out["exit_price"] == 101.0
    assert out["exit_time"].strftime("%H:%M") == "15:55"
    assert out["eod_r_planned"] == pytest.approx(0.2)     # same exit for the counterfactual


def test_no_entry_fills_at_or_after_the_flatten():
    pick = _pick(entry_type="limit", entry=99.0, stop=97.0, target=103.0)
    bars = _full_session({"15:57": 98.5})                   # the only dip, after 15:55
    out = simulate_pick(pick, bars, None, session_date=FULL_DAY)
    assert out["triggered"] is False


# ----------------------------------- D2: a resting limit fills on a trade-THROUGH
# docs/FIX_PHASE0.md. RKLB, 2026-10-15: short limit 54.00, stop 56.50. The
# simulator filled it at 11:15 (a +0.24R "winner", the day's only one); Alpaca
# cancelled the order unfilled. The only bar above 54.00 in the whole live
# window was 11:15, high 54.02: two cents through, not a touch. So the rule is
# not just ">" but a trade-through of at least max(1 tick, 5 bps). The broker's
# own 28 post-10/02 entries are replayed in tests/test_fill_reconcile.py.

RKLB_DAY = date(2026, 10, 15)


def _rklb_session(high_1115, low_1115=53.73):
    """09:45-14:05, a quiet tape under the limit, with the real 11:15 bar
    (open 53.74, close 53.96) reaching `high_1115`."""
    bars, t = [], datetime(2026, 10, 15, 9, 45)
    while t.strftime("%H:%M") <= "14:05":
        if t.strftime("%H:%M") == "11:15":
            bars.append({"t": t, "o": 53.74, "h": high_1115, "l": low_1115, "c": 53.96, "v": 179_558})
        else:
            bars.append({"t": t, "o": 53.50, "h": 53.80, "l": 53.30, "c": 53.50, "v": 90_000})
        t = t.replace(minute=t.minute + 1) if t.minute < 59 else t.replace(hour=t.hour + 1, minute=0)
    return bars


def _rklb_pick(**extra):
    return _pick(direction="short", entry_type="limit", entry=54.00, stop=56.50, target=48.00,
                 time_stop="by 14:00", entry_not_before="09:45", **extra)


@pytest.mark.parametrize("high", [54.00, 54.01, 54.02])
def test_a_bar_that_only_touches_or_barely_pierces_a_sell_limit_is_not_a_fill(high):
    out = simulate_pick(_rklb_pick(), _rklb_session(high), None, session_date=RKLB_DAY)
    assert out["triggered"] is False
    assert out["touch_only_bars"] == 1
    assert out["exit_reason"] == "no_trigger"


@pytest.mark.parametrize("high", [54.03, 54.05, 54.40])
def test_a_bar_that_trades_through_a_sell_limit_by_the_cushion_fills_at_the_limit(high):
    out = simulate_pick(_rklb_pick(), _rklb_session(high), None, slippage_bps=0,
                        session_date=RKLB_DAY)
    assert out["triggered"] is True
    assert out["entry_fill"] == 54.00            # filled at the limit, not at the bar's high
    assert out["touch_only_bars"] == 0


def test_the_real_rklb_1115_bar_does_not_fill_what_alpaca_never_filled():
    """The headline: 11:15 high 54.02 (3.7 bps through). Before sim_version 8 the
    simulator filled 53.973 here and booked +0.21R at the 14:00 time stop."""
    out = simulate_pick(_rklb_pick(), _rklb_session(54.02), None, slippage_bps=5,
                        session_date=RKLB_DAY)
    assert out["triggered"] is False and out.get("r_planned") is None
    assert out["touch_only_bars"] >= 1
    assert out["untriggered_reason"] == "entry_condition_not_met"      # a near miss, not "no setup"
    assert out["trigger_gap_r"] is not None and out["trigger_gap_r"] <= 0


CMC_DAY = date(2026, 10, 15)


def _cmc_session(low_1000):
    bars, t = [], datetime(2026, 10, 15, 9, 45)
    while t.strftime("%H:%M") <= "10:30":
        if t.strftime("%H:%M") == "10:00":
            bars.append({"t": t, "o": 66.80, "h": 66.90, "l": low_1000, "c": 66.70, "v": 50_000})
        else:
            bars.append({"t": t, "o": 66.90, "h": 67.00, "l": 66.70, "c": 66.90, "v": 50_000})
        t = t.replace(minute=t.minute + 1) if t.minute < 59 else t.replace(hour=t.hour + 1, minute=0)
    return bars


@pytest.mark.parametrize("low,filled", [(66.50, False), (66.48, False), (66.47, False),
                                        (66.46, True), (66.20, True)])
def test_a_buy_limit_needs_the_same_trade_through(low, filled):
    """CMC's real buy limit 66.50: cushion = max(1c, 5 bps) = 3.325c, so a low
    must reach 66.46675 or lower. The long mirror of the RKLB cases."""
    pick = _pick(direction="long", entry_type="limit", entry=66.50, stop=64.00, target=72.00,
                 entry_not_before="09:45")
    out = simulate_pick(pick, _cmc_session(low), None, slippage_bps=0, session_date=CMC_DAY)
    assert out["triggered"] is filled
    if filled:
        assert out["entry_fill"] == 66.50
    else:
        assert out["touch_only_bars"] == 1


def test_a_limit_that_opens_through_the_price_still_fills_at_the_open():
    """Gap-through-at-open is unchanged: the order is marketable, no cushion."""
    short = _pick(direction="short", entry_type="limit", entry=54.00, stop=56.50, target=48.00)
    out = simulate_pick(short, [_bar(54.40, 54.45, 54.30, 54.35)], None, slippage_bps=0)
    assert out["triggered"] is True and out["entry_fill"] == 54.40
    long_ = _pick(direction="long", entry_type="limit", entry=66.50, stop=64.00, target=72.00)
    out = simulate_pick(long_, [_bar(66.31, 66.40, 66.25, 66.35)], None, slippage_bps=0)
    assert out["triggered"] is True and out["entry_fill"] == 66.31


def test_a_limit_that_opens_one_tick_through_the_price_is_marketable_and_fills_at_the_open():
    """The order is marketable the moment the bar opens through it, however
    little: it fills at that open, not at the limit and not after a cushion."""
    short = _pick(direction="short", entry_type="limit", entry=54.00, stop=56.50, target=48.00)
    out = simulate_pick(short, [_bar(54.01, 54.02, 53.95, 53.98)], None, slippage_bps=0)
    assert out["triggered"] is True and out["entry_fill"] == 54.01
    long_ = _pick(direction="long", entry_type="limit", entry=66.50, stop=64.00, target=72.00)
    out = simulate_pick(long_, [_bar(66.49, 66.52, 66.48, 66.50)], None, slippage_bps=0)
    assert out["triggered"] is True and out["entry_fill"] == 66.49


def test_a_long_stop_entry_triggers_when_the_high_only_touches_the_trigger():
    """UNG 2026-10-07: buy stop 11.12 filled with the bar 0.99 cents through."""
    pick = _pick(direction="long", entry_type="breakout", entry=11.12, stop=10.80, target=11.80)
    out = simulate_pick(pick, [_bar(11.05, 11.12, 11.00, 11.08)], None, slippage_bps=0)
    assert out["triggered"] is True and out["entry_fill"] == 11.12
    assert out["touch_only_bars"] is None


def test_a_limit_that_only_opens_at_the_price_is_not_filled():
    pick = _pick(direction="short", entry_type="limit", entry=54.00, stop=56.50, target=48.00)
    out = simulate_pick(pick, [_bar(54.00, 54.00, 53.90, 53.95)], None)
    assert out["triggered"] is False and out["touch_only_bars"] == 1


def test_stop_entries_still_trigger_on_a_touch():
    """AMAT 2026-10-16: sell stop 418.00. The broker filled it 2.4 bps through
    (67 of 68 stop legs fill within a minute of the first touching bar), so
    breakout entries keep touch semantics and carry no touch count."""
    pick = _pick(direction="short", entry_type="breakout", entry=418.00, stop=433.00, target=400.00)
    out = simulate_pick(pick, [_bar(418.60, 418.70, 418.00, 418.20)], None, slippage_bps=0)
    assert out["triggered"] is True and out["entry_fill"] == 418.00
    assert out["touch_only_bars"] is None


def test_touch_only_bars_is_zero_for_a_limit_nothing_touched_and_none_otherwise():
    far = _pick(direction="short", entry_type="limit", entry=54.00, stop=56.50, target=48.00)
    assert simulate_pick(far, [_bar(51.0, 51.5, 50.8, 51.2)], None)["touch_only_bars"] == 0
    assert simulate_pick(_pick(entry_type="open"), [_bar(100.0, 100.5, 99.5, 100.2)], None
                         )["touch_only_bars"] is None


def test_a_touch_after_the_cutoff_is_not_called_a_missed_trigger():
    """time_stop_expired_pretrigger means the entry WOULD have fired after the
    cutoff. A bar that only touches the limit would not have, so it must not be
    reported as a trigger the time stop cut off."""
    pick = _pick(direction="short", entry_type="limit", entry=54.00, stop=56.50, target=48.00,
                 time_stop="by 10:00", entry_not_before="09:45")
    from datetime import timedelta
    bars = [{"t": datetime(2026, 10, 15, 9, 45) + timedelta(minutes=i), "o": 53.5, "h": 53.8,
             "l": 53.3, "c": 53.5, "v": 1000} for i in range(30)]          # 09:45 .. 10:14
    bars[20]["h"] = 54.01                                         # 10:05, after the cutoff: a touch
    out = simulate_pick(pick, bars, None, session_date=RKLB_DAY)
    assert out["triggered"] is False
    assert out["untriggered_reason"] != "time_stop_expired_pretrigger"
    bars[20]["h"] = 54.30                                         # 10:05: a real trade-through
    out = simulate_pick(pick, bars, None, session_date=RKLB_DAY)
    assert out["untriggered_reason"] == "time_stop_expired_pretrigger"


def test_a_v7_row_is_not_the_same_semantics_as_v8():
    """Resting-limit fills changed in v8, so a v7 morning row is not equivalent
    (the chart drawer flags it instead of replaying it as if it matched)."""
    from agent.grading import same_semantics
    assert CURRENT_SIM_VERSION >= 8
    assert not same_semantics(7, "morning") and not same_semantics(7, "shadow")
    assert same_semantics(CURRENT_SIM_VERSION, "morning")


# ------------------------ Phase 5: r_if_held_to_flatten, the time-stop counterfactual
# docs/FIX_PHASE0.md. AMAT, 2026-10-16: breakout short 418.00, stop 433.00, time stop
# 14:00. It booked +0.06R at 14:00 and the stock kept falling to the 15:55 flatten with
# the stop 16 points away. `r_if_held_to_flatten` is that number: the SAME fill held with
# only its initial stop (no target, no time stop) to the live desk's flatten, in the
# planned-risk R a taken pick is reported in. It is a reference: never added to r_multiple,
# r_planned or any total.

def _stepped_session(steps, day=FULL_DAY, overrides=None):
    """1-minute bars 09:30-15:59. `steps` is {"HH:MM": price}: the price holds from that
    minute until the next step (default 100.0 before the first); `overrides` replaces whole
    bars by minute."""
    from datetime import timedelta
    t0 = datetime(day.year, day.month, day.day, 9, 30)
    marks = sorted(steps.items())
    out, price = [], 100.0
    for i in range(390):
        t = t0 + timedelta(minutes=i)
        hhmm = t.strftime("%H:%M")
        for k, v in marks:
            if k <= hhmm:
                price = v
        bar = {"t": t, "o": price, "h": price + 0.05, "l": price - 0.05, "c": price, "v": 10_000}
        if overrides and hhmm in overrides:
            bar.update(overrides[hhmm])
        out.append(bar)
    return out


def _amat_like(**pick_extra):
    pick = _pick(direction="short", entry_type="breakout", entry=418.0, stop=433.0, target=400.0,
                 time_stop="by 14:00", entry_not_before="09:45", **pick_extra)
    bars = _stepped_session(
        {"09:30": 420.0, "10:06": 417.0, "14:00": 416.75, "14:01": 412.0, "15:55": 409.3},
        overrides={"10:05": {"o": 418.2, "h": 418.3, "l": 417.9, "c": 418.0}})
    return pick, bars


def test_amat_1016_levels_time_stop_is_small_and_the_held_counterfactual_is_the_trend_it_cut():
    """The prompt's case: fill 417.79, 14:00 time stop ~ +0.06R, a 15:55 bar near 409.3 and
    the 433 stop never touched -> held ~ +0.57R (the exact figure depends on the 5 bps exit
    friction: 0.552 here)."""
    pick, bars = _amat_like()
    out = simulate_pick(pick, bars, None, slippage_bps=5, session_date=FULL_DAY)
    assert out["entry_fill"] == pytest.approx(417.791, abs=0.001)
    assert out["exit_reason"] == "time_stop" and out["exit_time"].strftime("%H:%M") == "14:00"
    assert out["r_planned"] == pytest.approx(0.06, abs=0.01)
    assert out["r_if_held_to_flatten"] == pytest.approx(0.57, abs=0.03)
    assert out["r_if_held_to_flatten"] == pytest.approx(0.552, abs=0.002)
    # the reference changes none of the numbers the desk is judged on
    assert out["r_multiple"] == pytest.approx(0.056, abs=0.002)


def test_the_real_amat_1016_session_held_to_the_flatten():
    import json
    import pathlib
    fx = json.loads((pathlib.Path(__file__).parent / "fixtures" / "shadow_day_bars.json")
                    .read_text(encoding="utf-8"))
    d = date(2026, 10, 16)
    bars = [{"t": datetime.combine(d, datetime.strptime(r[0], "%H:%M").time()), "o": r[1],
             "h": r[2], "l": r[3], "c": r[4], "v": r[5]} for r in fx["AMAT-2026-10-16"]]
    pick = _pick(direction="short", entry_type="breakout", entry=418.0, stop=433.0, target=400.0,
                 time_stop="by 14:00", entry_not_before="09:45")
    out = simulate_pick(pick, bars, None, slippage_bps=5, session_date=d)
    assert out["exit_reason"] == "time_stop"
    assert out["r_planned"] == pytest.approx(0.056, abs=0.002)
    assert out["r_if_held_to_flatten"] == pytest.approx(0.626, abs=0.002)    # 15:55 opened 408.20
    assert out["r_if_held_to_flatten"] == out["eod_r_planned"]               # the stop was never near


def test_held_equals_the_taken_r_when_the_trade_really_ended_at_its_stop():
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)
    bars = _stepped_session({"09:30": 100.0}, overrides={"11:00": {"l": 97.9}})
    out = simulate_pick(pick, bars, None, slippage_bps=0, session_date=FULL_DAY)
    assert out["exit_reason"] == "stop" and out["r_planned"] == -1.0
    assert out["r_if_held_to_flatten"] == -1.0


def test_a_stop_touched_on_the_fill_bar_ends_the_held_trade_too():
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)
    bars = _stepped_session({"09:30": 100.0}, overrides={"09:30": {"l": 97.9}})
    out = simulate_pick(pick, bars, None, slippage_bps=0, session_date=FULL_DAY)
    assert out["exit_reason"] == "stop"
    assert out["r_if_held_to_flatten"] == -1.0


def test_held_equals_the_taken_r_when_the_trade_was_flattened_at_1555():
    pick = _pick(entry_type="open", entry=100.0, stop=95.0, target=110.0)
    bars = _stepped_session({"09:30": 100.0, "15:55": 101.0})
    out = simulate_pick(pick, bars, None, slippage_bps=0, session_date=FULL_DAY)
    assert out["exit_reason"] == "eod" and out["r_planned"] == pytest.approx(0.2)
    assert out["r_if_held_to_flatten"] == out["r_planned"]


def test_a_target_hit_before_a_later_reversal_shows_what_the_target_saved():
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=104.0)
    bars = _stepped_session({"09:30": 100.0},
                            overrides={"10:30": {"h": 104.1}, "13:00": {"l": 97.8}})
    out = simulate_pick(pick, bars, None, slippage_bps=0, session_date=FULL_DAY)
    assert out["exit_reason"] == "target" and out["r_planned"] == 2.0
    assert out["r_if_held_to_flatten"] == -1.0               # held, it would have been stopped


def test_a_time_stop_that_beat_a_later_stop_out_shows_what_it_saved():
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=110.0, time_stop="by 11:00")
    bars = _stepped_session({"09:30": 100.0, "10:30": 99.5}, overrides={"14:00": {"l": 97.9}})
    out = simulate_pick(pick, bars, None, slippage_bps=0, session_date=FULL_DAY)
    assert out["exit_reason"] == "time_stop" and out["r_planned"] == pytest.approx(-0.25)
    assert out["r_if_held_to_flatten"] == -1.0


def test_held_short_mirrors_long_and_a_gap_through_the_stop_exits_at_the_open():
    pick = _pick(direction="short", entry_type="open", entry=100.0, stop=102.0, target=90.0,
                 time_stop="by 11:00")
    bars = _stepped_session({"09:30": 100.0, "10:30": 99.0, "13:00": 104.0})   # gaps over 102
    out = simulate_pick(pick, bars, None, slippage_bps=0, session_date=FULL_DAY)
    assert out["exit_reason"] == "time_stop" and out["r_planned"] == pytest.approx(0.5)
    assert out["r_if_held_to_flatten"] == pytest.approx(-2.0)    # out at the 104.00 open, not 102


def test_held_charges_the_same_exit_friction_as_the_taken_exit():
    pick = _pick(entry_type="open", entry=100.0, stop=95.0, target=110.0)
    bars = _stepped_session({"09:30": 100.0, "15:55": 101.0})
    free = simulate_pick(pick, bars, None, slippage_bps=0, session_date=FULL_DAY)
    paid = simulate_pick(pick, bars, None, slippage_bps=50, session_date=FULL_DAY)
    assert paid["r_if_held_to_flatten"] < free["r_if_held_to_flatten"]
    assert paid["r_if_held_to_flatten"] == paid["r_planned"]


def test_a_thin_bar_cannot_stop_the_held_trade_any_more_than_the_taken_one():
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=110.0)
    bars = _stepped_session({"09:30": 100.0}, overrides={"11:00": {"l": 97.0, "v": 10}})
    out = simulate_pick(pick, bars, None, slippage_bps=0, min_bar_volume=1000, session_date=FULL_DAY)
    assert out["exit_reason"] == "eod"
    assert out["r_if_held_to_flatten"] == out["r_planned"] == 0.0


def test_no_fill_and_invalid_fill_have_no_held_counterfactual():
    pick = _pick(entry_type="limit", entry=90.0, stop=88.0, target=95.0)
    out = simulate_pick(pick, _stepped_session({"09:30": 100.0}), None, session_date=FULL_DAY)
    assert out["triggered"] is False and out["r_if_held_to_flatten"] is None
    bad = _pick(entry_type="limit", entry=100.0, stop=97.0, target=103.0)
    bars = _stepped_session({"09:30": 96.0})                 # opens under the long's own stop
    out = simulate_pick(bad, bars, None, session_date=FULL_DAY)
    assert out["exit_reason"] == "invalid_fill" and out["r_if_held_to_flatten"] is None


def test_the_counterfactual_is_stored_and_never_enters_a_total():
    import pathlib
    from agent import ledger
    assert "r_if_held_to_flatten" in ledger._OUTCOME_SIM_COLS
    # nothing that sums or averages R may read it: the only code that names it is the
    # simulator that computes it, the ledger columns that store it, and the one-off
    # reference report (fix_verification.held_counterfactual: held minus booked R,
    # printed beside each other, never added to a desk total)
    root = pathlib.Path(__file__).resolve().parent.parent / "agent"
    users = sorted(p.relative_to(root).as_posix() for p in root.rglob("*.py")
                   if "r_if_held_to_flatten" in p.read_text(encoding="utf-8"))
    assert users == ["fix_verification.py", "grading.py", "ledger.py", "migrations_v37.py"]


def test_the_held_counterfactual_round_trips_through_the_ledger(tmp_path, monkeypatch):
    from agent import ledger
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "held.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    con = ledger.connect()
    plan = {"no_trade": False, "picks": [{
        "ticker": "AMAT", "direction": "short", "setup_type": "breakdown_short",
        "entry_plan": {"type": "breakout", "price": 418.0}, "entry_price": 418.0,
        "stop_price": 433.0, "target_price": 400.0, "time_stop": "by 14:00", "size_r": 0.25,
        "conviction": 3, "entry_not_before": "09:45"}]}
    pid = ledger.save_daily_plan(con, date(2026, 10, 16), plan, {"candidates": []}, "chop")[0]
    pick, bars = _amat_like()
    pick = {**pick, "pick_id": pid}
    out = simulate_pick(pick, bars, None, slippage_bps=5, session_date=FULL_DAY)
    out["eval_date"] = date(2026, 10, 16)
    ledger.save_outcome(con, out)
    stored = con.execute("SELECT r_planned, r_multiple, r_if_held_to_flatten FROM outcomes "
                         "WHERE pick_id = ?", [pid]).fetchone()
    con.close()
    assert stored[2] == out["r_if_held_to_flatten"] and stored[0] == out["r_planned"]
    assert stored[2] != stored[0]


def test_when_the_bars_end_before_the_flatten_the_held_trade_ends_at_the_last_close():
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=110.0)
    bars = _stepped_session({"09:30": 100.0}, overrides={"11:59": {"o": 99.0, "c": 100.8}})[:150]
    out = simulate_pick(pick, bars, None, slippage_bps=0, session_date=FULL_DAY)
    assert out["exit_reason"] == "eod" and out["exit_price"] == 100.8
    assert out["r_if_held_to_flatten"] == out["r_planned"] == pytest.approx(0.4)


def test_a_held_short_is_stopped_by_a_spike_it_later_recovers_from():
    pick = _pick(direction="short", entry_type="open", entry=100.0, stop=102.0, target=90.0,
                 time_stop="by 11:00")
    bars = _stepped_session({"09:30": 100.0, "10:30": 99.0}, overrides={"13:00": {"h": 102.2}})
    out = simulate_pick(pick, bars, None, slippage_bps=0, session_date=FULL_DAY)
    assert out["exit_reason"] == "time_stop" and out["r_planned"] == pytest.approx(0.5)
    assert out["r_if_held_to_flatten"] == -1.0               # the 13:00 spike took it out at 102


def test_a_held_long_that_gaps_under_its_stop_is_out_at_the_open():
    pick = _pick(entry_type="open", entry=100.0, stop=98.0, target=110.0, time_stop="by 11:00")
    bars = _stepped_session({"09:30": 100.0, "13:00": 96.0})
    out = simulate_pick(pick, bars, None, slippage_bps=0, session_date=FULL_DAY)
    assert out["exit_reason"] == "time_stop" and out["r_planned"] == 0.0
    assert out["r_if_held_to_flatten"] == pytest.approx(-2.0)    # 96.00 open, not the 98 stop
