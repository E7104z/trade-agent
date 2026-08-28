"""
Tests for agent/friction.py.

The load-bearing property is the FAIL-OPEN direction: missing data must never
make a fill look cheaper than the status-quo flat assumption. A friction model
that silently discounts costs when a feed is down would flatter every edge the
ledger reports -- the precise failure mode this system exists to prevent.
"""
from __future__ import annotations

import pytest

from agent.friction import (
    FrictionParams,
    params_from_config,
    participation_for_pick,
    shares_for_pick,
    slippage_bps_for,
    stress_ladder,
)


def _pick(entry=25.0, stop=24.0, size_r=1.0):
    return {"entry_price": entry, "stop_price": stop, "size_r": size_r}


def _flat(**kw):
    kw.setdefault("flat_bps", 5.0)
    return FrictionParams(model="flat", **kw)


def _part(**kw):
    kw.setdefault("flat_bps", 5.0)
    return FrictionParams(model="participation", **kw)


# ------------------------------------------------------------------- geometry

def test_shares_from_risk_budget_and_stop_distance():
    # $500/R over a $1.00 stop => 500 shares
    assert shares_for_pick(_pick(25.0, 24.0, 1.0), 500.0) == pytest.approx(500.0)
    # half size => half the shares
    assert shares_for_pick(_pick(25.0, 24.0, 0.5), 500.0) == pytest.approx(250.0)
    # tighter stop => MORE shares for the same dollar risk
    assert shares_for_pick(_pick(25.0, 24.5, 1.0), 500.0) == pytest.approx(1000.0)


def test_shares_returns_none_on_unusable_plans():
    assert shares_for_pick({"entry_price": 25.0, "stop_price": 25.0}, 500.0) is None
    assert shares_for_pick({"entry_price": None, "stop_price": 24.0}, 500.0) is None
    assert shares_for_pick({}, 500.0) is None
    assert shares_for_pick(_pick(), 0.0) is None


def test_participation_is_shares_over_adv20():
    # 500 shares against 1,000,000 ADV = 0.05%
    assert participation_for_pick(_pick(), 1_000_000, 500.0) == pytest.approx(0.0005)
    assert participation_for_pick(_pick(), None, 500.0) is None
    assert participation_for_pick(_pick(), 0, 500.0) is None


# --------------------------------------------------------- flat model preserved

def test_flat_model_reproduces_existing_constant_slippage_exactly():
    # This is the no-op guarantee: default config must not change any number.
    assert slippage_bps_for(_pick(), _flat(), atr_pct=6.0, adv20=500_000) == 5.0


def test_flat_model_scales_with_stress_only():
    assert slippage_bps_for(_pick(), _flat(), stress=2.0) == 10.0
    assert slippage_bps_for(_pick(), _flat(), stress=3.0) == 15.0


# ------------------------------------------------------- participation model

def test_participation_model_charges_more_for_a_volatile_name():
    calm = slippage_bps_for(_pick(), _part(), atr_pct=1.2, adv20=50_000_000)
    wild = slippage_bps_for(_pick(), _part(), atr_pct=6.0, adv20=50_000_000)
    assert wild > calm


def test_participation_model_charges_more_for_a_bigger_footprint():
    small = slippage_bps_for(_pick(), _part(), atr_pct=3.0, adv20=50_000_000)
    large = slippage_bps_for(_pick(), _part(), atr_pct=3.0, adv20=200_000)
    assert large > small


def test_impact_follows_square_root_not_linear_law():
    # Quadrupling participation should roughly DOUBLE the impact term.
    p = _part(base_bps=0.0, spread_k=0.0, flat_bps=0.0, impact_k=100.0)
    one_x = slippage_bps_for(_pick(), p, atr_pct=0.0, adv20=4_000_000)
    four_x = slippage_bps_for(_pick(), p, atr_pct=0.0, adv20=1_000_000)
    # tolerance covers the 4-decimal quantization applied inside slippage_bps_for
    assert four_x == pytest.approx(2.0 * one_x, abs=1e-3)


def test_realistic_gapper_costs_materially_more_than_five_bps():
    # 3%-ATR $25 name, 2M ADV, 1R at $500/R -> 500 shares = 0.025% participation
    bps = slippage_bps_for(_pick(25.0, 24.0, 1.0), _part(), atr_pct=3.0,
                           adv20=2_000_000, dollars_per_r=500.0)
    assert bps > 5.0
    assert 8.0 < bps < 40.0     # sane magnitude, not a runaway


def test_megacap_lands_near_the_legacy_flat_assumption():
    # The model must degrade toward 5bps on exactly the names 5bps was right for.
    bps = slippage_bps_for(_pick(400.0, 396.0, 1.0), _part(), atr_pct=1.0,
                           adv20=80_000_000, dollars_per_r=500.0)
    assert 5.0 <= bps < 8.0


# ------------------------------------------------------------ fail-open safety

def test_missing_atr_never_produces_a_cheaper_fill_than_flat():
    bps = slippage_bps_for(_pick(), _part(), atr_pct=None, adv20=1_000_000)
    assert bps >= 5.0


def test_missing_adv_never_produces_a_cheaper_fill_than_flat():
    bps = slippage_bps_for(_pick(), _part(), atr_pct=3.0, adv20=None)
    assert bps >= 5.0


def test_all_inputs_missing_falls_back_to_the_flat_rate():
    assert slippage_bps_for(_pick(), _part(), atr_pct=None, adv20=None) == 5.0


def test_unusable_plan_geometry_still_returns_a_floor_not_zero():
    broken = {"entry_price": 25.0, "stop_price": 25.0}   # zero stop distance
    assert slippage_bps_for(broken, _part(), atr_pct=3.0, adv20=1_000_000) >= 5.0


def test_flat_rate_is_a_hard_floor_under_the_participation_model():
    generous = _part(base_bps=0.0, spread_k=0.0, impact_k=0.0)
    assert slippage_bps_for(_pick(), generous, atr_pct=0.1, adv20=1e12) == 5.0


def test_cost_is_capped_so_a_pathological_input_cannot_explode():
    p = _part(max_bps=120.0)
    # absurd footprint: tiny float, huge size
    bps = slippage_bps_for(_pick(25.0, 24.99, 10.0), p, atr_pct=40.0,
                           adv20=1_000, dollars_per_r=500.0)
    assert bps == 120.0


# ----------------------------------------------------------------- stress ladder

def test_stress_ladder_is_monotone_and_proportional():
    ladder = stress_ladder(_pick(), _part(), atr_pct=3.0, adv20=2_000_000)
    assert set(ladder) == {"1.0x", "2.0x", "3.0x"}
    assert ladder["1.0x"] < ladder["2.0x"] < ladder["3.0x"]
    assert ladder["2.0x"] == pytest.approx(2 * ladder["1.0x"], rel=1e-6)
    assert ladder["3.0x"] == pytest.approx(3 * ladder["1.0x"], rel=1e-6)


# ------------------------------------------------------------------- config

class _Cfg:
    def __init__(self, raw):
        self.raw = raw

    def get(self, k, d=None):
        return self.raw.get(k, d)


def test_config_without_a_friction_block_reproduces_current_behavior():
    p = params_from_config(_Cfg({"slippage_bps": 5}))
    assert p.model == "flat"
    assert p.flat_bps == 5.0
    assert slippage_bps_for(_pick(), p, atr_pct=6.0, adv20=100_000) == 5.0


def test_config_can_opt_into_the_participation_model():
    p = params_from_config(_Cfg({
        "slippage_bps": 5,
        "friction": {"model": "participation", "impact_k": 300.0,
                     "stress_multipliers": [1.0, 2.0]},
    }))
    assert p.model == "participation"
    assert p.impact_k == 300.0
    assert p.stress_multipliers == (1.0, 2.0)


def test_unknown_model_name_degrades_to_flat_rather_than_raising():
    p = FrictionParams(model="wishful-thinking", flat_bps=5.0)
    assert p.model == "flat"
    assert slippage_bps_for(_pick(), p, atr_pct=9.0, adv20=1000) == 5.0
