"""
Hand-computed fixtures for agent/indicators.py — pure, deterministic technical
indicator primitives. No network access, no LLM: every value here is checked
against arithmetic worked out by hand in the test itself.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from agent.indicators import (
    atr,
    beta_corr,
    daily_levels,
    ema_loss_time,
    ema_series,
    opening_range,
    premarket_levels,
    rvol_simple,
    trend_snapshot,
    vwap_distance,
    vwap_events,
    vwap_series,
)


def _bar(h, l, c, v, t=None):
    return {"t": t, "o": (h + l) / 2, "h": h, "l": l, "c": c, "v": v}


def _tbar(hh, mm, h, l, c, v):
    return _bar(h, l, c, v, t=datetime(2026, 8, 25, hh, mm))


# --------------------------------------------------------------------- VWAP

def test_vwap_series_hand_computed_three_bars():
    # bar1: typical (10+8+9)/3=9.0, v=100 -> cum_pv=900,  cum_v=100  -> vwap=9.0
    # bar2: typical (11+9+10)/3=10.0, v=200 -> cum_pv=2900, cum_v=300 -> vwap=9.6667
    # bar3: typical (12+10+11)/3=11.0, v=100 -> cum_pv=4000, cum_v=400 -> vwap=10.0
    bars = [_bar(10, 8, 9, 100), _bar(11, 9, 10, 200), _bar(12, 10, 11, 100)]
    vwap = vwap_series(bars)
    assert vwap == pytest.approx([9.0, 9.666666667, 10.0])


def test_vwap_series_zero_volume_bar_carries_prior_value_forward():
    bars = [_bar(10, 8, 9, 100), _bar(20, 20, 20, 0)]
    vwap = vwap_series(bars)
    assert vwap[0] == pytest.approx(9.0)
    assert vwap[1] == pytest.approx(9.0)  # zero-volume bar can't move the average


def test_vwap_series_first_bar_zero_volume_falls_back_to_its_own_close():
    bars = [_bar(10, 8, 9, 0)]
    assert vwap_series(bars) == [9.0]


def test_vwap_series_invariant_to_a_copy_of_the_bar_list():
    bars = [_bar(10, 8, 9, 100), _bar(11, 9, 10, 200), _bar(12, 10, 11, 100)]
    assert vwap_series(bars) == vwap_series(list(bars))


def test_vwap_series_monotone_nondecreasing_cum_volume_all_bars_at_price():
    # every bar priced identically -> vwap stays pinned at that price regardless
    # of how much volume trades (a degenerate but useful monotonicity check)
    bars = [_bar(10, 10, 10, v) for v in (1, 1000, 1_000_000)]
    vwap = vwap_series(bars)
    assert vwap == pytest.approx([10.0, 10.0, 10.0])


# ---------------------------------------------------------------------- EMA

def test_ema_series_seeded_with_sma_then_standard_recurrence():
    # period=3, closes=[1,2,3,4,5]
    # seed (SMA of first 3) = 2.0 at index 2; k = 2/(3+1) = 0.5
    # index3: 4*0.5 + 2.0*0.5 = 3.0
    # index4: 5*0.5 + 3.0*0.5 = 4.0
    ema = ema_series([1, 2, 3, 4, 5], 3)
    assert ema == [None, None, 2.0, 3.0, 4.0]


def test_ema_series_insufficient_data_is_all_none():
    assert ema_series([1, 2], 5) == [None, None]


def test_ema_series_empty_input():
    assert ema_series([], 9) == []


# -------------------------------------------------------------------- RVOL

def test_rvol_simple_basic_ratio():
    assert rvol_simple(500_000, 2_000_000) == pytest.approx(0.25)


def test_rvol_simple_none_when_adv20_missing_or_zero():
    assert rvol_simple(500_000, None) is None
    assert rvol_simple(500_000, 0) is None


# --------------------------------------------------------------- vwap_distance

def test_vwap_distance_signed_pct():
    assert vwap_distance(110.0, 100.0) == pytest.approx(10.0)
    assert vwap_distance(90.0, 100.0) == pytest.approx(-10.0)


def test_vwap_distance_zero_vwap_is_zero_not_a_crash():
    assert vwap_distance(100.0, 0.0) == 0.0


# ---------------------------------------------------------------- vwap_events

def test_vwap_events_reclaim_then_reject_close_only():
    # bar0: heavy-volume wick bar whose CLOSE (8) sits below its own typical
    # price (h+l+c)/3=12.0 -> starts already "below" its own vwap (12.0).
    # bar1/2: light volume, close 14 >= vwap(~12.02/12.04) -> reclaim at bar1.
    # bar3: heavy-volume low bar drags vwap down to ~9.71 but closes at 6,
    # still below it -> reject at bar3.
    bars = [
        _tbar(9, 30, h=20, l=8, c=8, v=1000),
        _tbar(9, 31, h=15, l=13, c=14, v=10),
        _tbar(9, 32, h=15, l=13, c=14, v=10),
        _tbar(9, 33, h=10, l=6, c=6, v=1000),
    ]
    vwap = vwap_series(bars)
    assert vwap == pytest.approx([12.0, 12.019802, 12.039216, 9.709571], abs=1e-4)
    ev = vwap_events(bars, vwap)
    assert ev["first_reclaim_et"] == bars[1]["t"]
    assert ev["first_reject_et"] == bars[3]["t"]
    assert ev["first_reject_et"] > ev["first_reclaim_et"]
    assert ev["crosses"] == 2
    assert ev["pct_time_above"] == pytest.approx(0.5)


def test_vwap_events_never_crosses_stays_none():
    bars = [_tbar(9, 30 + i, h=10, l=9, c=9.5, v=100) for i in range(5)]
    vwap = vwap_series(bars)
    ev = vwap_events(bars, vwap)
    assert ev["crosses"] == 0
    assert ev["first_reclaim_et"] is None
    assert ev["first_reject_et"] is None


def test_vwap_events_mismatched_lengths_returns_empty_shape():
    bars = [_tbar(9, 30, h=10, l=9, c=9.5, v=100)]
    ev = vwap_events(bars, [])
    assert ev == {"first_reclaim_et": None, "first_reject_et": None,
                  "pct_time_above": None, "crosses": 0}


# ------------------------------------------------------------- ema_loss_time

def test_ema_loss_time_long_loses_on_close_below():
    bars = [
        _tbar(9, 30, h=11, l=9, c=10.5, v=100),
        _tbar(9, 31, h=10, l=8, c=9.0, v=100),  # closes below ema -> loss
    ]
    ema = [10.0, 10.0]
    t = ema_loss_time(bars, ema, "long")
    assert t == bars[1]["t"]


def test_ema_loss_time_short_loses_on_close_above():
    bars = [
        _tbar(9, 30, h=10, l=9, c=9.5, v=100),
        _tbar(9, 31, h=12, l=10, c=11.0, v=100),  # closes above ema -> loss
    ]
    ema = [10.0, 10.0]
    t = ema_loss_time(bars, ema, "short")
    assert t == bars[1]["t"]


def test_ema_loss_time_none_when_never_lost():
    bars = [_tbar(9, 30, h=11, l=10, c=10.5, v=100)]
    ema = [10.0]
    assert ema_loss_time(bars, ema, "long") is None


def test_ema_loss_time_skips_bars_with_no_ema_value_yet():
    bars = [
        _tbar(9, 30, h=9, l=8, c=8.5, v=100),   # would be a "loss" but ema is None here
        _tbar(9, 31, h=11, l=10, c=10.5, v=100),
    ]
    ema = [None, 10.0]
    assert ema_loss_time(bars, ema, "long") is None


# ---------------------------------------------------------------------- ATR

def _dbar(h, l, c):
    return {"h": h, "l": l, "c": c}


def test_atr_hand_computed_wilder_three_bars():
    # bar0->bar1 TR = max(12-9, |12-9|, |9-9|) = 3
    # bar1->bar2 TR = max(13-11, |13-11|, |11-11|) = 2
    # period=2 needs 3 bars; seed = avg(3, 2) = 2.5, no remaining TRs to smooth
    bars = [_dbar(10, 8, 9), _dbar(12, 9, 11), _dbar(13, 11, 12)]
    assert atr(bars, period=2) == pytest.approx(2.5)


def test_atr_insufficient_data_is_none():
    bars = [_dbar(10, 8, 9), _dbar(12, 9, 11)]
    assert atr(bars, period=14) is None


def test_atr_empty_input_is_none():
    assert atr([], period=14) is None


# ------------------------------------------------------------------ daily_levels

def test_daily_levels_ten_day_tape_hand_computed():
    # 10 COMPLETED sessions (caller already excluded any partial current day).
    # Highs/lows chosen distinctly per day so windowed max/min are unambiguous.
    bars = [_dbar(10 + i, 5 + i, 8 + i) for i in range(10)]  # day0..day9
    lv = daily_levels(bars)
    last = bars[-1]  # day9: h=19, l=14, c=17
    assert lv["pdh"] == last["h"] == 19
    assert lv["pdl"] == last["l"] == 14
    assert lv["pdc"] == last["c"] == 17
    # last 5 days: day5..day9 -> h in [15..19], l in [10..14]
    assert lv["hi_5d"] == 19
    assert lv["lo_5d"] == 10
    # only 10 bars available -> 20d/52w windows stay None, not a short-window guess
    assert lv["hi_20d"] is None and lv["lo_20d"] is None
    assert lv["hi_52w"] is None and lv["lo_52w"] is None


def test_daily_levels_empty_input_all_none():
    lv = daily_levels([])
    assert all(v is None for v in lv.values())


# -------------------------------------------------------------- premarket_levels

def test_premarket_levels_hand_computed():
    bars = [_dbar(10.5, 9.5, 10.0), _dbar(11.0, 9.8, 10.5)]
    lv = premarket_levels(bars)
    assert lv == {"pm_high": 11.0, "pm_low": 9.5}


def test_premarket_levels_empty_input():
    assert premarket_levels([]) == {"pm_high": None, "pm_low": None}


# -------------------------------------------------------------- trend_snapshot

def test_trend_snapshot_hand_computed():
    closes = [float(i) for i in range(1, 226)]  # 1.0 .. 225.0
    snap = trend_snapshot(closes, atr_val=5.0)
    # ma20 = avg(206..225) = 215.5, ma50 = avg(176..225) = 200.5,
    # ma200 = avg(26..225) = 125.5 -- last (225.0) is above all three.
    assert snap["above_ma20"] is True
    assert snap["above_ma50"] is True
    assert snap["above_ma200"] is True
    # ma20 5 sessions ago = avg(201..220) = 210.5 -> slope = (215.5-210.5)/210.5*100
    assert snap["ma20_slope_pct"] == pytest.approx((215.5 - 210.5) / 210.5 * 100, abs=1e-6)
    # ext_atr = (225.0 - 215.5) / 5.0 = 1.9
    assert snap["ext_atr"] == pytest.approx(1.9)


def test_trend_snapshot_insufficient_data_is_null_shape():
    snap = trend_snapshot([100.0, 101.0], atr_val=5.0)
    assert snap == {"above_ma20": None, "above_ma50": None, "above_ma200": None,
                    "ma20_slope_pct": None, "ext_atr": None}


def test_trend_snapshot_empty_input():
    snap = trend_snapshot([], atr_val=5.0)
    assert snap["above_ma20"] is None


def test_trend_snapshot_no_atr_leaves_ext_atr_none():
    closes = [float(i) for i in range(1, 226)]
    snap = trend_snapshot(closes, atr_val=None)
    assert snap["above_ma20"] is True
    assert snap["ext_atr"] is None


# -------------------------------------------------------------- opening_range

def test_opening_range_close_through_break_up():
    or_bars = [_bar(10.0, 9.0, 9.5, 100) for _ in range(15)]  # or_high=10.0, or_low=9.0
    post = [_tbar(9, 45, h=10.2, l=9.6, c=10.5, v=100)]       # closes above range -> break up
    orr = opening_range(or_bars + post, minutes=15)
    assert orr["or_high"] == 10.0
    assert orr["or_low"] == 9.0
    assert orr["break_dir"] == "up"
    assert orr["break_et"] == post[0]["t"]


def test_opening_range_wick_touch_is_not_a_break():
    or_bars = [_bar(10.0, 9.0, 9.5, 100) for _ in range(15)]
    # high pokes above the range but the bar CLOSES back inside -- not a break
    post = [_tbar(9, 45, h=10.5, l=9.4, c=9.8, v=100)]
    orr = opening_range(or_bars + post, minutes=15)
    assert orr["break_dir"] is None
    assert orr["break_et"] is None


def test_opening_range_break_down():
    or_bars = [_bar(10.0, 9.0, 9.5, 100) for _ in range(15)]
    post = [_tbar(9, 45, h=9.2, l=8.5, c=8.8, v=100)]
    orr = opening_range(or_bars + post, minutes=15)
    assert orr["break_dir"] == "down"


def test_opening_range_empty_input():
    assert opening_range([], minutes=15) == {"or_high": None, "or_low": None,
                                              "break_dir": None, "break_et": None}


# ----------------------------------------------------------------- beta_corr

def test_beta_corr_two_x_levered_copy_has_beta_two_and_perfect_correlation():
    closes_a = [100.0]
    for i in range(1, 21):
        closes_a.append(closes_a[-1] * (1 + (0.01 if i % 2 == 0 else -0.007)))
    closes_b = [50.0]
    for i in range(1, 21):
        ret_a = closes_a[i] / closes_a[i - 1] - 1
        closes_b.append(closes_b[-1] * (1 + 2 * ret_a))
    result = beta_corr(closes_a, closes_b, n=20)
    assert result is not None
    beta, corr = result
    assert beta == pytest.approx(2.0, abs=1e-6)
    assert corr == pytest.approx(1.0, abs=1e-6)


def test_beta_corr_insufficient_history_is_none():
    assert beta_corr([1.0, 2.0], [1.0, 2.0], n=20) is None


def test_beta_corr_flat_benchmark_is_none():
    flat = [100.0] * 25
    other = [float(i) for i in range(100, 125)]
    assert beta_corr(flat, other, n=20) is None
