"""
Hand-computed fixtures for agent/indicators.py — pure, deterministic technical
indicator primitives. No network access, no LLM: every value here is checked
against arithmetic worked out by hand in the test itself.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from agent.indicators import (
    ema_loss_time,
    ema_series,
    rvol_simple,
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
