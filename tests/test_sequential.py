"""
EDGE_ACCELERATION_SPEC_v1 Part 1 -- the forward test spends one budget.

Pinned here:
  - the spending function: monotone, A(N_MAX) = ALPHA, nothing spent at n=0
  - however the looks fall, their per-look alphas sum to at most ALPHA
  - the one-sided p points in the direction of discovery
  - the t quantile the futility bound uses is right
  - simulated on null rules shaped like this desk's R, the false-positive
    rate stays inside the budget (the claim the whole design rests on)
  - the one-time reopening of rules the old one-look test retired
"""
from __future__ import annotations

import random
from datetime import date

import pytest

from agent import ledger, research, sequential as seq


def test_spending_is_monotone_and_ends_at_the_whole_budget():
    assert seq.spent_at(0) == 0.0
    assert seq.spent_at(seq.N_MAX) == pytest.approx(seq.ALPHA)
    assert seq.spent_at(10 * seq.N_MAX) == pytest.approx(seq.ALPHA)
    assert seq.spent_at(8) == pytest.approx(seq.ALPHA * (8 / seq.N_MAX) ** seq.RHO)
    vals = [seq.spent_at(n) for n in range(0, seq.N_MAX + 5)]
    assert vals == sorted(vals)


@pytest.mark.parametrize("schedule", [
    [8, 9, 10, 40, 96], [8, 50, 51, 52, 200], list(range(8, 120, 3)), [96]])
def test_any_look_schedule_spends_at_most_the_budget(schedule):
    spent, total = 0.0, 0.0
    for n in schedule:
        lk = seq.look(n, 0.1, 1.0, 1, spent)
        if lk["status"] == "looked":
            total += lk["alpha_look"]
            spent = lk["spent"]
    assert total <= seq.ALPHA + 1e-12


def test_a_look_without_new_evidence_spends_nothing():
    first = seq.look(20, 0.2, 1.0, 1, 0.0)
    again = seq.look(20, 0.2, 1.0, 1, first["spent"])
    assert again["status"] == "no_new_information"
    assert again["spent"] == pytest.approx(first["spent"])


def test_one_sided_p_follows_the_direction_of_discovery():
    up = seq.p_one_sided(0.5, 1.0, 16, direction=1)
    down = seq.p_one_sided(0.5, 1.0, 16, direction=-1)
    assert up < 0.05 and down > 0.95
    assert up + down == pytest.approx(1.0)
    assert seq.p_one_sided(0.5, 0.0, 16) is None      # degenerate: untestable


def test_t_quantile_matches_the_table():
    assert seq.t_quantile(0.975, 10) == pytest.approx(2.228, abs=1e-3)
    assert seq.t_quantile(0.80, 9) == pytest.approx(0.883, abs=1e-3)
    assert seq.t_quantile(0.95, 63) == pytest.approx(1.669, abs=1e-3)


def test_futility_reversal_and_the_horizon_are_flagged():
    flat = seq.look(10, 0.0, 0.3, 1, 0.0)
    assert flat["futile"] and not flat["reversed"]
    rev = seq.look(10, -1.0, 0.1, 1, 0.0)
    assert rev["futile"] and rev["reversed"]
    # a negative-direction rule with a strongly negative mean is not futile
    neg = seq.look(10, -1.0, 0.5, -1, 0.0)
    assert not neg["futile"] and neg["p"] < 0.001
    assert seq.look(seq.N_MAX, 0.0, 2.0, 1, 0.0)["final"]


def _desk_r(rng: random.Random, mu: float) -> float:
    """R shaped like this desk's: stop -1R (sometimes worse), target +2R,
    session exits in between; the win rate is set so the mean is `mu`."""
    w = min(max((mu - 0.25 * 0.4 + 0.75) / 3.0, 0.01), 0.74)
    u = rng.random()
    if u < w:
        return 2.0 + rng.gauss(0, 0.1)
    if u < 0.75:
        return -1.0 + rng.gauss(0, 0.08) - (rng.expovariate(2.0) if rng.random() < 0.05 else 0.0)
    return rng.uniform(-0.8, 1.6)


def _run_rule(rng: random.Random, mu: float, per_week: int) -> str:
    """Weekly looks after the ten-day quarantine, exactly as the weekly loop
    takes them; returns 'validated', 'retired' or 'open'."""
    xs: list[float] = []
    spent = 0.0
    for _week in range(60):
        xs += [_desk_r(rng, mu) for _ in range(per_week)]
        n = len(xs)
        if n < 8:
            continue
        m = sum(xs) / n
        sd = (sum((x - m) ** 2 for x in xs) / (n - 1)) ** 0.5
        lk = seq.look(n, m, sd, 1, spent)
        if lk["status"] != "looked":
            continue
        spent = lk["spent"]
        rej, _q = seq.cohort([lk["p_adj"]])
        if rej[0]:
            return "validated"
        if lk["futile"] or lk["final"]:
            return "retired"
    return "open"


def test_simulated_null_rules_stay_inside_the_budget():
    rng = random.Random(20261004)
    outcomes = [_run_rule(rng, 0.0, per_week=3) for _ in range(500)]
    fp = outcomes.count("validated") / len(outcomes)
    # The budget is 0.025; with 500 runs the 99.9% binomial ceiling is ~0.048.
    assert fp <= 0.048, fp
    assert outcomes.count("open") == 0, "every null rule must eventually be decided"


def test_simulated_real_edge_is_found_far_more_often_than_one_look_could():
    rng = random.Random(7)
    outcomes = [_run_rule(rng, 0.5, per_week=3) for _ in range(300)]
    # The one-look test validated a +0.5R edge ~12% of the time at this rate.
    assert outcomes.count("validated") / len(outcomes) >= 0.5


# ---------------------------------------------------------------- reopening

@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "s.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


def _retired(con, rid, note):
    con.execute(
        """INSERT INTO research_rules (rule_id, statement, dimensions, n_dimensions, state,
               source, discovered_at, discovery_n, discovery_effect, quarantined_at,
               retired_at, forward_n_required, forward_n, forward_sum_r, forward_sumsq_r,
               note, predicate_json, lane, decay_count)
           VALUES (?, 's', '["setup_type"]', 1, 'RETIRED', 'miner', ?, 12, 0.4, ?, ?, 8, 9,
                   2.0, 20.0, ?, '{"lane":"morning","conditions":[]}', 'morning', 0)""",
        [rid, date(2026, 8, 1), date(2026, 8, 2), date(2026, 9, 26), note])


def test_underpowered_retirements_reopen_once_on_fresh_evidence(con):
    _retired(con, "Runder0000001", " | forward evidence not significant after cohort FDR (p=0.2)")
    _retired(con, "Rrevers000001", " | forward effect reversed sign")
    _retired(con, "Rstale0000001", " | repeatedly stale")

    reopened = research.reopen_underpowered_retirements(con, date(2026, 10, 5))
    assert reopened == ["Runder0000001"]
    st = {s["rule_id"]: s for s in research.rule_states(con)}
    r = st["Runder0000001"]
    assert r["state"] == "QUARANTINED" and r["quarantined_at"] == "2026-10-05"
    assert r["forward_n"] == 0 and r["alpha_spent"] == 0.0 and r["test_design"] == seq.DESIGN
    assert st["Rrevers000001"]["state"] == "RETIRED"
    assert st["Rstale0000001"]["state"] == "RETIRED"
    # Nothing on or before the reopening day counts.
    assert not research.record_rule_observation(con, "Runder0000001", date(2026, 10, 5), 1.0)
    assert research.record_rule_observation(con, "Runder0000001", date(2026, 10, 6), 1.0)
    # Idempotent: a second pass reopens nothing.
    assert research.reopen_underpowered_retirements(con, date(2026, 10, 12)) == []


def test_field_lanes_spend_over_a_longer_horizon_with_a_lower_floor():
    assert seq.FIELD_LANES == ("gapfield",)
    assert seq.design_for("gapfield") == (seq.FIELD_N_MAX, seq.FIELD_MIN_USEFUL_R)
    assert seq.design_for("morning") == (seq.N_MAX, seq.MIN_USEFUL_R)
    assert seq.design_for("runner") == (seq.N_MAX, seq.MIN_USEFUL_R), "a removed lane gets no field design"
    # A +0.08R field cell at n=200 with sd 0.9: the morning floor (0.10R) would
    # call it futile; the field floor (0.05R) keeps it collecting.
    n_max, mue = seq.design_for("gapfield")
    lk = seq.look(200, 0.0, 0.9, 1, 0.0, n_max=n_max, min_useful_r=mue)
    assert lk["upper"] < seq.MIN_USEFUL_R and lk["upper"] > mue and not lk["futile"]
    assert not lk["final"] and lk["n_max"] == seq.FIELD_N_MAX
