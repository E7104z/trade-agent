"""
Pinning tests for the sim_version 4 R contract.

THE BUG THESE EXIST TO PREVENT

Through sim_version 3 the risk denominator was recomputed off the ACTUAL fill
and floored at 25% of planned risk:

    risk = abs(fill_price - stop)
    if risk < planned_risk * 0.25: risk = planned_risk * 0.25

A long whose bar opened below the detection price therefore got a denominator
collapsed toward that 25% floor while its target stayed anchored to the plan,
so a pick planned at 2R could score +11R. The same mechanism ran in reverse
for fills above the plan, scoring a 2R winner at +0.48R. Errors in both
directions, from one cause, all of it invisible in the stored row because only
the corrupted output was persisted -- never the denominator that produced it.

The consequence was not a noisy edge estimate. It was an edge estimate that
measured fill drift. Every test here pins one half of the fix: the headline
number is bounded by the plan's geometry, and the execution cost still exists
somewhere it can be audited.
"""
from __future__ import annotations

import pytest

from agent.grading import CURRENT_SIM_VERSION, simulate_pick


def _pick(direction="long", entry_type="open", entry=100.0, stop=98.0,
          target=104.0, pick_id=1):
    return {"pick_id": pick_id, "direction": direction, "entry_type": entry_type,
            "entry_price": entry, "stop_price": stop, "target_price": target}


def _bar(o, h, l, c, v=10_000):
    return {"o": o, "h": h, "l": l, "c": c, "v": v}


# ------------------------------------------------- the impossible-R regression

def test_favorable_gap_fill_cannot_inflate_r_past_plan_geometry():
    """The exact v3 failure, reduced: plan is 2R, the bar opens 0.85R below the
    detection price but still ABOVE the stop -- so the wrong-side-of-stop gate
    does not catch it -- and price then runs to the target.

    Under v3 the fill-to-stop distance was 0.30 against a planned risk of 2.00,
    below the 25% floor, so the denominator became 0.50 and the trade scored
    (104 - 98.30) / 0.50 = +11.4R on a plan that could only ever pay 2R. That
    is the same magnitude as the +11.08R and +9.96R rows in the live ledger.

    Under v4 the target is 2R from the planned entry, so it scores about 2R,
    and the fill drift is reported separately, never capitalised.
    """
    pick = _pick(entry=100.0, stop=98.0, target=104.0)   # planned risk 2.00, 2R
    bars = [_bar(98.3, 104.5, 98.25, 104.2)]             # opens 0.85R below plan
    out = simulate_pick(pick, bars, None, max_fill_deviation_r=0.0)
    assert out["exit_reason"] == "target"
    assert out["r_multiple"] == pytest.approx(2.0, abs=0.01)


def test_target_exit_is_always_near_planned_r_regardless_of_fill_drift():
    """Sweep the fill across the whole legal drift band. The headline number
    must not move with it -- that is the entire point of a fixed denominator."""
    for open_px in (97.5, 98.5, 99.0, 99.5, 100.0, 100.5, 101.0):
        pick = _pick(entry=100.0, stop=98.0, target=104.0)
        bars = [_bar(open_px, 104.5, min(open_px, 98.1), 104.2)]
        out = simulate_pick(pick, bars, None, max_fill_deviation_r=0.0)
        if out["exit_reason"] != "target":
            continue
        got = out["r_multiple"]
        assert got == pytest.approx(2.0, abs=0.01), \
            "fill at {} moved headline R to {}".format(open_px, got)


def test_stop_exit_scores_about_minus_one_r():
    pick = _pick(entry=100.0, stop=98.0, target=104.0)
    bars = [_bar(99.0, 99.5, 97.5, 97.8)]
    out = simulate_pick(pick, bars, None, max_fill_deviation_r=0.0)
    assert out["exit_reason"] == "stop"
    assert out["r_multiple"] == pytest.approx(-1.0, abs=0.01)


def test_eod_exit_lands_between_stop_and_target():
    pick = _pick(entry=100.0, stop=98.0, target=104.0)
    bars = [_bar(99.6, 101.0, 99.0, 101.0)]
    out = simulate_pick(pick, bars, None, max_fill_deviation_r=0.0)
    assert out["exit_reason"] == "eod"
    assert -1.0 <= out["r_multiple"] <= 2.0


# ----------------------------------------------------- execution is not erased

@pytest.mark.parametrize("open_px,direction,stop,target", [
    (100.4, "long", 98.0, 104.0),    # adverse fill  -> slippage charged
    (99.6, "long", 98.0, 104.0),     # favorable fill -> clamped, nothing charged
    (100.0, "long", 98.0, 104.0),    # exact fill
    (99.6, "short", 102.0, 96.0),    # adverse fill, mirrored
    (100.4, "short", 102.0, 96.0),   # favorable fill, mirrored
])
def test_slippage_identity_holds_on_both_sides_of_the_plan(
        open_px, direction, stop, target):
    """r_multiple - r_multiple_at_fill == fill_slippage_r, exactly, for every
    fill -- adverse, favorable, and exact.

    This is what makes the split auditable. The favorable cases are the ones
    that matter: the conservative clamp refuses to credit a better-than-plan
    fill, so the charged slippage must be ZERO there, not the negative raw
    drift. An earlier cut of this change reported the raw drift and broke the
    identity on 104 of 290 real rows without failing a single test.
    """
    pick = _pick(direction=direction, entry_type="limit", entry=100.0,
                 stop=stop, target=target)
    lo, hi = min(open_px, 95.0), max(open_px, 105.0)
    bars = [_bar(open_px, hi, lo, 100.0)]
    out = simulate_pick(pick, bars, None, slippage_bps=5,
                        max_fill_deviation_r=0.0)
    assert out["triggered"] is True
    assert (out["r_multiple"] - out["r_multiple_at_fill"]) == pytest.approx(
        out["fill_slippage_r"], abs=0.002)
    # Charged slippage is never negative -- a favorable fill costs nothing, it
    # does not pay.
    assert out["fill_slippage_r"] >= 0
    assert out["fill_slippage"] >= 0


def test_favorable_fill_charges_nothing_but_still_reports_the_raw_drift():
    pick = _pick(entry_type="limit", entry=100.0, stop=98.0, target=104.0)
    bars = [_bar(99.0, 104.5, 98.5, 104.2)]    # bar offered 0.5R better
    out = simulate_pick(pick, bars, None, max_fill_deviation_r=0.0)
    assert out["fill_slippage_r"] == 0          # not credited
    assert out["fill_drift_r"] == pytest.approx(-0.5, abs=1e-3)   # but recorded
    assert out["r_multiple_at_fill"] == pytest.approx(out["r_multiple"], abs=1e-3)


def test_adverse_fill_is_reported_as_positive_slippage():
    pick = _pick(entry_type="breakout", entry=100.0, stop=98.0, target=104.0)
    bars = [_bar(100.6, 104.5, 100.0, 104.2)]  # gapped through the trigger
    out = simulate_pick(pick, bars, None, max_fill_deviation_r=0.0)
    assert out["fill_slippage"] > 0
    assert out["fill_slippage_r"] > 0
    assert out["r_multiple_at_fill"] < out["r_multiple"]


# --------------------------------------------------------- conservative fills

def test_long_fill_is_never_better_than_the_plan_price():
    """A market or stop order at the trigger does not get filled below it."""
    pick = _pick(entry_type="limit", entry=100.0, stop=98.0, target=104.0)
    bars = [_bar(99.6, 104.5, 99.5, 104.2)]
    out = simulate_pick(pick, bars, None, max_fill_deviation_r=0.0)
    assert out["entry_fill"] >= 100.0


def test_short_fill_is_never_better_than_the_plan_price():
    pick = _pick(direction="short", entry_type="limit", entry=100.0,
                 stop=102.0, target=96.0)
    bars = [_bar(100.4, 100.5, 95.5, 95.8)]
    out = simulate_pick(pick, bars, None, max_fill_deviation_r=0.0)
    assert out["entry_fill"] <= 100.0


def test_short_target_exit_scores_about_planned_r():
    pick = _pick(direction="short", entry_type="open", entry=100.0,
                 stop=102.0, target=96.0)
    bars = [_bar(100.0, 100.2, 95.5, 95.8)]
    out = simulate_pick(pick, bars, None, max_fill_deviation_r=0.0)
    assert out["exit_reason"] == "target"
    assert out["r_multiple"] == pytest.approx(2.0, abs=0.01)


# ------------------------------------------------------- fill-drift rejection

def test_fill_drifting_past_the_threshold_is_rejected_not_graded():
    pick = _pick(entry=100.0, stop=98.0, target=104.0)
    bars = [_bar(98.4, 104.5, 98.3, 104.2)]    # 0.8R below plan
    out = simulate_pick(pick, bars, None, max_fill_deviation_r=0.5)
    assert out["exit_reason"] == "invalid_fill"
    assert out["triggered"] is False
    assert out["r_multiple"] is None
    assert "0.8" in out["invalid_fill_detail"]


def test_fill_inside_the_threshold_still_grades():
    pick = _pick(entry=100.0, stop=98.0, target=104.0)
    bars = [_bar(99.8, 104.5, 99.5, 104.2)]    # 0.1R below plan
    out = simulate_pick(pick, bars, None, max_fill_deviation_r=0.5)
    assert out["exit_reason"] == "target"
    assert out["r_multiple"] == pytest.approx(2.0, abs=0.01)


def test_threshold_of_zero_disables_the_rejection():
    pick = _pick(entry=100.0, stop=98.0, target=104.0)
    bars = [_bar(98.4, 104.5, 98.3, 104.2)]
    out = simulate_pick(pick, bars, None, max_fill_deviation_r=0.0)
    assert out["exit_reason"] == "target"


def test_wrong_side_of_stop_still_rejected_ahead_of_the_drift_gate():
    """The v3 gate must keep firing, and keep its own detail message: a fill
    below its own stop is a structurally impossible trade, not merely a distant
    one, and the two are worth telling apart in the ledger."""
    pick = _pick(entry=100.0, stop=98.0, target=104.0)
    bars = [_bar(97.0, 104.5, 96.9, 104.2)]
    out = simulate_pick(pick, bars, None, max_fill_deviation_r=0.5)
    assert out["exit_reason"] == "invalid_fill"
    assert "wrong side of" in out["invalid_fill_detail"]


# ------------------------------------------------------------------ provenance

def test_sim_version_is_stamped_and_current():
    assert CURRENT_SIM_VERSION >= 4
    out = simulate_pick(_pick(), [_bar(100.0, 100.5, 99.5, 100.2)], None)
    assert out["sim_version"] == CURRENT_SIM_VERSION
