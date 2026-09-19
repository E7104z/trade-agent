"""
Phase 1: untriggered-pick reason tagging.

An untriggered pick used to record only `exit_reason = 'no_trigger'` plus a
`missed_move_r`. That says how far the idea ran without us, but not why we
never got on -- and those are two different failures:

  the market never offered the setup   says nothing about the rule
  the setup arrived, the mechanic missed it   is a rule defect

Only the second would ever justify loosening an entry condition, and
collapsing both into "no_trigger" makes a systematically over-tight entry
indistinguishable from a quiet week.

These tests pin the deterministic classifier. Every expected value is worked
out from the bars in the test itself -- no fixtures, no network.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone

import pytest

from agent.grading import (NEAR_TRIGGER_R, classify_untriggered,
                           simulate_pick)
from agent.migrations_v12 import UNTRIGGERED_REASONS

ET = timezone(timedelta(hours=-4))


def _bar(o, h, l, c, v=100_000, minute=0):
    return {"t": datetime(2026, 9, 18, 9, 30, tzinfo=ET) + timedelta(minutes=minute),
            "o": o, "h": h, "l": l, "c": c, "v": v}


def _flat_session(price, n=30):
    """A session that sits still at `price` -- offers nothing."""
    return [_bar(price, price + 0.02, price - 0.02, price, minute=i)
            for i in range(n)]


# ------------------------------------------------------------- the enum

def test_every_reason_the_classifier_emits_is_in_the_closed_enum():
    """The grader, the miner and the schema must agree on spelling. A typo
    here would silently create a new bucket nothing aggregates."""
    cases = [
        # (bars, entry, long, entry_type)
        (_flat_session(10.0), 20.0, True, "breakout"),      # never close
        (_flat_session(10.0), 10.02, True, "breakout"),      # very close
        ([], 10.0, True, "breakout"),                        # no bars
    ]
    for bars, entry, long, etype in cases:
        reason, _note, _gap = classify_untriggered(
            bars, entry, risk=1.0, long=long, entry_type=etype)
        assert reason in UNTRIGGERED_REASONS, reason


# ------------------------------------------------- no_setup_offered

def test_price_nowhere_near_the_trigger_is_no_setup_offered():
    """Long breakout at 20 while price idles at 10 -- the market never
    presented this setup. Nothing to learn about the entry mechanic."""
    bars = _flat_session(10.0)
    reason, note, gap_r = classify_untriggered(
        bars, entry=20.0, risk=1.0, long=True, entry_type="breakout")
    assert reason == "no_setup_offered"
    assert note is None
    # high was 10.02, entry 20 -> short by 9.98 on risk 1.0
    assert gap_r == pytest.approx(-9.98, abs=0.01)


def test_short_breakout_mirrors_the_long_case():
    bars = _flat_session(10.0)
    reason, _n, gap_r = classify_untriggered(
        bars, entry=5.0, risk=1.0, long=False, entry_type="breakout")
    assert reason == "no_setup_offered"
    # low was 9.98, entry 5 -> short by 4.98
    assert gap_r == pytest.approx(-4.98, abs=0.01)


# ------------------------------------------- entry_condition_not_met

def test_price_just_short_of_the_trigger_is_entry_condition_not_met():
    """The setup WAS there -- price came within a quarter of planned risk and
    the mechanic still did not fire. This is the only bucket that argues the
    entry was too narrow."""
    bars = _flat_session(10.0)          # high 10.02
    reason, _n, gap_r = classify_untriggered(
        bars, entry=10.10, risk=1.0, long=True, entry_type="breakout")
    assert reason == "entry_condition_not_met"
    assert gap_r == pytest.approx(-0.08, abs=0.01)
    assert abs(gap_r) <= NEAR_TRIGGER_R


def test_the_near_threshold_scales_with_the_trade_s_own_risk():
    """0.25R on a wide-stop trade is a bigger absolute distance than on a
    tight one -- the threshold must not be an absolute cent value."""
    bars = _flat_session(10.0)          # high 10.02
    # 0.15 away. On risk=1.0 that is 0.15R -> close. On risk=0.2 it is
    # 0.75R -> not close.
    close = classify_untriggered(bars, entry=10.17, risk=1.0,
                                 long=True, entry_type="breakout")[0]
    far = classify_untriggered(bars, entry=10.17, risk=0.2,
                               long=True, entry_type="breakout")[0]
    assert close == "entry_condition_not_met"
    assert far == "no_setup_offered"


def test_long_limit_measures_the_dip_not_the_rally():
    """A long limit fills on a DIP to entry, so closeness is about the low."""
    bars = _flat_session(10.0)          # low 9.98
    reason, _n, gap_r = classify_untriggered(
        bars, entry=9.90, risk=1.0, long=True, entry_type="limit")
    assert reason == "entry_condition_not_met"
    assert gap_r == pytest.approx(-0.08, abs=0.01)


# --------------------------------------- time_stop_expired_pretrigger

def test_a_cutoff_that_blocked_a_real_trigger_is_its_own_category():
    """Price DID reach the trigger, but only after the cutoff. The setup
    arrived; the rule simply was not allowed to take it. Calling that
    'no_setup_offered' would blame the market for a policy choice."""
    bars = _flat_session(10.0, n=10)
    # A clean breakout well after the cutoff index.
    bars += [_bar(10.0, 11.0, 10.0, 11.0, minute=10 + i) for i in range(5)]
    reason, _n, _gap = classify_untriggered(
        bars, entry=10.5, risk=1.0, long=True, entry_type="breakout",
        cutoff_idx=10)
    assert reason == "time_stop_expired_pretrigger"


def test_a_cutoff_with_no_later_trigger_is_not_blamed():
    """The cutoff only earns the label if a trigger actually existed after
    it -- otherwise the market still never offered the setup."""
    bars = _flat_session(10.0, n=15)
    reason, _n, _gap = classify_untriggered(
        bars, entry=20.0, risk=1.0, long=True, entry_type="breakout",
        cutoff_idx=10)
    assert reason == "no_setup_offered"


# ------------------------------------------------------------- other

def test_no_usable_bars_is_other_with_a_note():
    """'other' must always carry a note saying what happened."""
    reason, note, gap_r = classify_untriggered(
        [], entry=10.0, risk=1.0, long=True, entry_type="breakout")
    assert reason == "other"
    assert note and "bars" in note
    assert gap_r is None


def test_thin_bars_are_excluded_exactly_as_the_fill_loop_excludes_them():
    """A trigger 'reached' only on a bar the fill logic refused to trust must
    not count as reached here either, or the two disagree about reality."""
    bars = _flat_session(10.0, n=10)
    bars.append(_bar(10.0, 11.0, 10.0, 11.0, v=1, minute=10))   # thin spike
    reason, _n, _g = classify_untriggered(
        bars, entry=10.5, risk=1.0, long=True, entry_type="breakout",
        min_bar_volume=50_000)
    assert reason == "no_setup_offered", (
        "a thin-bar spike must not be treated as an approach")


# ------------------------------------------- integration: simulate_pick

def test_simulate_pick_tags_an_untriggered_pick():
    """The classifier must actually be reached through the real grader, and
    populate all three columns."""
    bars = _flat_session(10.0)
    pick = {"pick_id": 1, "direction": "long", "entry_type": "breakout",
            "entry_price": 20.0, "stop_price": 19.0, "target_price": 22.0}
    out = simulate_pick(pick, bars, None, session_date=date(2026, 9, 18))
    assert out["triggered"] is False
    assert out["exit_reason"] == "no_trigger"
    assert out["untriggered_reason"] == "no_setup_offered"
    assert out["trigger_gap_r"] is not None


def test_simulate_pick_leaves_the_fields_null_on_a_triggered_pick():
    """NULL for triggered picks -- the column must not imply a failure that
    did not happen."""
    bars = _flat_session(10.0)
    pick = {"pick_id": 1, "direction": "long", "entry_type": "open",
            "entry_price": 10.0, "stop_price": 9.0, "target_price": 12.0}
    out = simulate_pick(pick, bars, None, session_date=date(2026, 9, 18))
    assert out["triggered"] is True
    assert out["untriggered_reason"] is None
    assert out["untriggered_note"] is None
    assert out["trigger_gap_r"] is None


# ------------------------------- Phase 1 verification: end-to-end persist

def test_reason_persists_through_save_outcome_and_aggregates(tmp_path, monkeypatch):
    """Spec Phase 1 step 4: confirm the field populates correctly BEFORE the
    weekly miner consumes it. The miner must never read a schema it has not
    been tested against."""
    import os
    from agent import ledger

    fd_path = tmp_path / "t.duckdb"
    monkeypatch.setattr(ledger, "DB_PATH", str(fd_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    con = ledger.connect()

    con.execute(
        "INSERT INTO picks (pick_id, plan_date, ticker, direction, setup_type, "
        "entry_type, entry_price, stop_price, target_price, size_r) "
        "VALUES (1, ?, 'AAAA', 'long', 'gap_go', 'breakout', 20.0, 19.0, 22.0, 1.0)",
        [date(2026, 9, 18)])
    con.execute(
        "INSERT INTO picks (pick_id, plan_date, ticker, direction, setup_type, "
        "entry_type, entry_price, stop_price, target_price, size_r) "
        "VALUES (2, ?, 'BBBB', 'long', 'gap_go', 'breakout', 10.05, 9.0, 12.0, 1.0)",
        [date(2026, 9, 18)])

    bars = _flat_session(10.0)
    for pick_id, entry in ((1, 20.0), (2, 10.05)):
        out = simulate_pick(
            {"pick_id": pick_id, "direction": "long", "entry_type": "breakout",
             "entry_price": entry, "stop_price": entry - 1.0,
             "target_price": entry + 2.0},
            bars, None, session_date=date(2026, 9, 18))
        out["eval_date"] = date(2026, 9, 18)
        ledger.save_outcome(con, out)

    stored = con.execute(
        "SELECT pick_id, untriggered_reason, trigger_gap_r FROM outcomes "
        "ORDER BY pick_id").fetchall()
    assert stored[0][1] == "no_setup_offered"      # entry 20 vs a 10.02 high
    assert stored[1][1] == "entry_condition_not_met"  # entry 10.05 vs 10.02
    assert all(r[2] is not None for r in stored)

    breakdown = ledger.untriggered_breakdown(con)
    reasons = {r["reason"]: r["n"] for r in breakdown}
    assert reasons == {"no_setup_offered": 1, "entry_condition_not_met": 1}
    assert all(r["setup_type"] == "gap_go" for r in breakdown)
    con.close()


def test_rows_graded_before_v12_are_not_folded_into_a_real_bucket(tmp_path, monkeypatch):
    """A NULL reason is an unknown, not evidence. It must report as its own
    bucket rather than inflating whichever category is convenient."""
    from agent import ledger
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "t2.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    con = ledger.connect()
    con.execute(
        "INSERT INTO picks (pick_id, plan_date, ticker, direction, setup_type, size_r) "
        "VALUES (1, ?, 'AAAA', 'long', 'gap_go', 1.0)", [date(2026, 9, 18)])
    con.execute(
        "INSERT INTO outcomes (pick_id, eval_date, triggered, exit_reason) "
        "VALUES (1, ?, FALSE, 'no_trigger')", [date(2026, 9, 18)])

    breakdown = ledger.untriggered_breakdown(con)
    assert [r["reason"] for r in breakdown] == ["ungraded_pre_v12"]
    con.close()
