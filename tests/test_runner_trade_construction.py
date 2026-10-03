"""
Runner trade construction (specs/RUNNER_TRADE_CONSTRUCTION_v1.md).

Pins what the review's three changes depend on:
  - the construction arms do what their rule says, in their own risk unit,
    with no intra-bar look-ahead (arm A: stop floor + 2R on the new risk;
    arm B: the dashboard's Runner rules; arm C: A's stop, B's management, the
    trail armed at the 1R partial)
  - the at-entry fields read only bars that closed before the entry bar
  - continuation (drift) is measured for every candidate and tested per
    bucket in the one FDR batch, in ATR, never as a rule dimension
  - a surviving arm is quarantined by the system and validates only on
    forward sessions, one per session
  - every session states why it is or is not a contested selection session
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from agent import buckets, ledger, research, runner_entry, runner_exits, runner_research
from agent.config import Config
from agent.stats import Comparison
from agent.util import ET   # zone-aware: the half day below falls after DST ends
SESSION = date(2026, 9, 16)      # a Wednesday, full session
HALF_DAY = date(2026, 11, 27)    # 13:00 close
ENTRY, STOP = 10.0, 9.5          # plan risk 0.5
ATR = 1.0                        # floor stop = min(9.5, 10 - 1.0) = 9.0, risk 1.0


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "t.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


def _bars(prices, d: date = SESSION, start=time(9, 30), vol=10_000):
    t0 = datetime.combine(d, start, tzinfo=ET)
    out = []
    for i, p in enumerate(prices):
        o, h, l, c = p if isinstance(p, tuple) else (p, p + 0.01, p - 0.01, p)
        out.append({"t": t0 + timedelta(minutes=i), "o": o, "h": h, "l": l, "c": c,
                    "v": vol})
    return out


def _rise(n_before=60, to_i=90, step=0.01):
    """Exactly ENTRY at index n_before (10:30): the fill bar opens at the plan."""
    return [round(ENTRY + (i - n_before) * step, 4) for i in range(to_i)]


def _replay(bars, fill_idx=60, atr14=ATR, **kw):
    kw.setdefault("session_date", SESSION)
    return {r["policy"]: r for r in runner_exits.replay(bars, fill_idx, ENTRY, STOP,
                                                        atr14=atr14, **kw)}


# ================================================================ the family

def test_family_v4_carries_the_three_arms_once():
    names = [p.name for p in runner_exits.POLICIES]
    assert runner_exits.POLICY_VERSION == 4
    assert len(names) == len(set(names))
    assert set(runner_exits.ARMS) <= set(names)
    by = {p.name: p for p in runner_exits.POLICIES}
    a, b, c = (by[n] for n in runner_exits.ARMS)
    assert a.stop_floor_atr == 1.0 and a.target_r == 2.0 and a.trail is None
    assert b.stop_floor_atr is None and b.target_r is None
    assert (b.scale_at_r, b.breakeven_at_r, b.trail, b.hold_until) == \
        (1.0, 1.0, runner_exits.RATCHET, time(15, 0))      # 2:00 PM CT
    assert c.stop_floor_atr == 1.0 and c.trail_arm_r == 1.0
    assert (c.scale_at_r, c.breakeven_at_r, c.trail, c.hold_until) == \
        (b.scale_at_r, b.breakeven_at_r, b.trail, b.hold_until)


def test_floor_stop_is_the_lower_of_the_ema_stop_and_one_atr():
    a = next(p for p in runner_exits.POLICIES if p.name == "arm_a_floor")
    assert runner_exits.policy_stop(a, 10.0, 9.5, 1.0) == pytest.approx(9.0)
    # Already wider than 1 ATR: the plan's stop stands.
    assert runner_exits.policy_stop(a, 10.0, 8.5, 1.0) == pytest.approx(8.5)
    assert runner_exits.policy_stop(a, 10.0, 9.5, None) is None
    plan = runner_exits.POLICIES[0]
    assert runner_exits.policy_stop(plan, 10.0, 9.5, None) == 9.5


# ================================================================ arm A

def test_arm_a_survives_the_noise_that_stops_the_plan_and_books_its_own_r():
    path = (_rise(to_i=61) + [9.8, 9.6, (9.5, 9.5, 9.45, 9.46)]
            + [round(9.5 + 0.026 * i, 4) for i in range(1, 101)] + [12.1] * 230)
    r = _replay(_bars(path))
    assert r["plan"]["exit_reason"] == "stop" and r["plan"]["r_planned"] == pytest.approx(-1.0)
    a = r["arm_a_floor"]
    assert a["exit_reason"] == "target"
    assert (a["stop_used"], a["target_used"]) == (pytest.approx(9.0), pytest.approx(12.0))
    # Booked in ITS risk unit (1.0): +2R is +$2 per $1 at risk, same dollars
    # at risk as the plan's -1R.
    assert a["r_planned"] == pytest.approx(2.0)


def test_arm_a_without_atr_has_no_row_and_the_rest_of_the_family_stands():
    r = _replay(_bars(_rise() + [10.3] * 300), atr14=None)
    assert "arm_a_floor" not in r and "arm_c_floor_dashboard" not in r
    assert {"plan", "arm_b_dashboard", "no_target"} <= set(r)


def test_a_wider_stop_pays_the_same_cents_in_fewer_r():
    r = _replay(_bars(_rise() + [10.3] * 300), slippage_bps=5)
    plan, a = r["plan"], r["arm_a_floor"]
    assert plan["exit_reason"] == a["exit_reason"] == "eod"
    # Same exit, same dollars: risk 0.5 vs 1.0 halves both R and the friction in R.
    assert a["r_planned"] == pytest.approx(plan["r_planned"] / 2, abs=2e-3)
    assert a["cost_r"] == pytest.approx(plan["cost_r"] / 2, abs=2e-3)
    assert plan["cost_r"] == pytest.approx((10.0 * 0.0005 + 10.3 * 0.0005) / 0.5, abs=1e-3)


# ================================================================ arm B

def test_arm_b_banks_half_at_1r_and_scratches_the_rest_at_breakeven():
    # 10:31 touches +1.1R; 10:32 gaps under the fill. No 5m bar has closed
    # since the fill, so no trail level exists yet: the breakeven stop (armed
    # from 10:32) is what is hit, at the gap's open.
    bars = _bars(_rise(to_i=61) + [(10.0, 10.55, 10.0, 10.45), (9.95, 9.96, 9.9, 9.92)]
                 + [9.92] * 300)
    b = _replay(bars)["arm_b_dashboard"]
    assert b["exit_reason"] == "breakeven"
    assert b["r_planned"] == pytest.approx(0.5 * 1.0 + 0.5 * (9.95 - ENTRY) / 0.5)


def test_arm_b_trails_its_stop_up_to_the_ema_on_each_5m_close():
    # Steady rise below +1R to 10:59, then 11:00 drops through the stop the
    # 10:55-10:59 close ratcheted to (that bar's 5m 9 EMA).
    path = _rise() + [(10.29, 10.29, 9.6, 9.65)] + [9.65] * 299
    bars = _bars(path)
    ema_at_1059 = runner_exits._five_minute_marks(bars)[89]["ema9"]
    assert STOP < ema_at_1059 < 10.29
    r = _replay(bars)
    b = r["arm_b_dashboard"]
    assert b["exit_reason"] == "trail_stop"
    assert b["exit_et"] == datetime(2026, 9, 16, 11, 0)
    assert b["r_planned"] == pytest.approx((ema_at_1059 - ENTRY) / 0.5, abs=1e-3)
    # The plan's static stop sat below the drop: it held to the flatten.
    assert r["plan"]["exit_reason"] == "eod"


def test_arm_b_is_out_by_two_pm_central():
    # A name that rises all day: the lagging EMA never catches it, half is
    # banked at +1R (10.50), and the rest leaves at the 15:00 ET bar's close.
    bars = _bars(_rise(to_i=390))
    b = _replay(bars)["arm_b_dashboard"]
    assert b["exit_reason"] == "time_stop"
    assert b["exit_et"] == datetime(2026, 9, 16, 15, 0)
    close_1500 = bars[330]["c"]
    assert b["r_planned"] == pytest.approx(0.5 * 1.0 + 0.5 * (close_1500 - ENTRY) / 0.5)


def test_arm_b_trail_catches_a_stalled_name_before_two_pm():
    # The dashboard trail is a STOP at the 9 EMA: on a name that stalls, the
    # EMA converges up to price and a one-cent wiggle takes the position out.
    # It does not hold a dead name to 2 PM.
    r = _replay(_bars(_rise() + [10.3] * 300))
    b = r["arm_b_dashboard"]
    assert b["exit_reason"] == "trail_stop"
    assert b["exit_et"] < datetime(2026, 9, 16, 15, 0)
    assert 0 < b["r_planned"] < (10.3 - ENTRY) / 0.5
    # Never trailed down: its exit is above the plan's static stop.
    assert b["r_planned"] > r["plan"]["r_planned"] - 1.0


def test_hold_until_past_a_half_day_close_ends_at_the_desk_flatten_not_nowhere():
    bars = _bars(_rise(to_i=210), d=HALF_DAY)
    r = {x["policy"]: x for x in runner_exits.replay(
        bars, 60, ENTRY, STOP, atr14=ATR, session_date=HALF_DAY)}
    for name in ("arm_b_dashboard", "arm_c_floor_dashboard"):
        assert r[name]["exit_reason"] == "eod"
        assert r[name]["exit_et"] == datetime(2026, 11, 27, 12, 55)
    assert "time_1500" not in r     # the exit-grid time stops still drop


# ================================================================ arm C

def test_arm_c_ignores_the_ema_until_the_1r_partial():
    # Below +1R, a drop under the EMA (not under the 1-ATR floor) takes arm B
    # out on its trail; arm C rides its floor to 15:00.
    path = _rise() + [(10.29, 10.29, 9.6, 9.8)] + [9.8] * 299
    r = _replay(_bars(path))
    assert r["arm_b_dashboard"]["exit_reason"] == "trail_stop"
    c = r["arm_c_floor_dashboard"]
    assert c["exit_reason"] == "time_stop" and c["stop_used"] == pytest.approx(9.0)
    assert c["r_planned"] == pytest.approx((9.8 - ENTRY) / 1.0)


def test_arm_c_trails_once_the_partial_is_banked():
    # +1R on C's own risk is 11.0. Rise through it, then drop hard: C banks
    # half at 11.0, and the rest leaves on the ratcheted EMA stop.
    up = [round(ENTRY + 0.02 * i, 4) for i in range(1, 76)]          # to 11.50 by 11:45
    path = _rise(to_i=61) + up + [(11.5, 11.5, 10.2, 10.3)] + [10.3] * 250
    bars = _bars(path)
    drop_idx = 61 + len(up)
    mark_idx = max(i for i in runner_exits._five_minute_marks(bars) if i < drop_idx)
    ema = runner_exits._five_minute_marks(bars)[mark_idx]["ema9"]
    c = _replay(bars)["arm_c_floor_dashboard"]
    assert c["exit_reason"] == "trail_stop"
    assert c["r_planned"] == pytest.approx(0.5 * 1.0 + 0.5 * (ema - ENTRY) / 1.0, abs=1e-3)


# ================================================================ entry context

def _ctx_bars():
    # 09:30-10:29 rising from 9.40 to 9.99; entry bar 10:30 opens at 10.00
    # with an outsized print the at-entry VWAP must never see.
    pre = [round(9.40 + 0.01 * i, 4) for i in range(60)]
    bars = _bars(pre + [(10.0, 10.4, 9.9, 10.2)] + [10.2] * 200 + [10.5] * 129)
    bars[60]["v"] = 10_000_000
    return bars


def test_entry_context_reads_only_bars_closed_before_entry():
    bars = _ctx_bars()
    cutoff = datetime(2026, 9, 16, 10, 30)
    ctx = runner_entry.entry_context(bars, cutoff, datetime(2026, 9, 16, 10, 5), 0.5, {})
    vwap_before = runner_entry.session_vwap(bars[:60])
    assert ctx["entry_px"] == pytest.approx(10.0)
    assert ctx["entry_et"] == cutoff
    assert ctx["staleness_min"] == pytest.approx(25.0)
    assert ctx["vwap_at_entry"] == pytest.approx(vwap_before, abs=1e-4)
    assert ctx["ext_vwap_atr"] == pytest.approx((10.0 - vwap_before) / 0.5, abs=1e-3)
    assert ctx["drift_atr"] == pytest.approx((10.5 - 10.0) / 0.5)
    assert ctx["run_up_atr"] == pytest.approx((10.51 - 10.0) / 0.5)
    assert ctx["draw_down_atr"] == pytest.approx((10.0 - 9.9) / 0.5)


def test_entry_context_without_atr_keeps_the_atr_free_fields():
    ctx = runner_entry.entry_context(_ctx_bars(), datetime(2026, 9, 16, 10, 30), None, None, {})
    assert ctx["entry_px"] == pytest.approx(10.0)
    assert "ext_vwap_atr" not in ctx and "drift_atr" not in ctx
    assert "staleness_min" not in ctx
    assert runner_entry.entry_context([], None, None, 1.0, {}) == {}


def test_index_tape_is_read_through_the_last_bar_before_entry():
    up = runner_entry.IndexTape(_bars([400 + 0.1 * i for i in range(120)]))
    down = runner_entry.IndexTape(_bars([400 - 0.1 * i for i in range(60)]
                                        + [(500, 500, 500, 500)] * 60))
    at = datetime(2026, 9, 16, 10, 30)
    assert up.vwap_pct_before(at) > 0
    # The 10:30 bar (a spike to 500) has not closed when the entry bar opens.
    assert down.vwap_pct_before(at) < 0
    assert down.vwap_pct_before(datetime(2026, 9, 16, 9, 30)) is None
    assert buckets.index_tape(up.vwap_pct_before(at), down.vwap_pct_before(at)) == "split"


# ================================================================ dimensions

@pytest.mark.parametrize("spy,qqq,label", [
    (0.2, 0.1, "both_above"), (-0.2, -0.1, "both_below"), (0.2, -0.1, "split"),
    (0.0, 0.0, "both_above"), (None, 0.1, "?")])
def test_index_tape_label(spy, qqq, label):
    assert buckets.runner_label("runner_index_tape",
                                {"spy_vwap_pct": spy, "qqq_vwap_pct": qqq}) == label


@pytest.mark.parametrize("ext,label", [
    (-0.01, "<0"), (0.0, "0-0.15"), (0.149, "0-0.15"), (0.15, "0.15-0.3"),
    (0.3, "0.3-0.5"), (0.5, "0.5-1"), (1.0, "1+"), (None, "?")])
def test_ext_bucket_uses_the_stop_bucket_atr_grid(ext, label):
    assert buckets.runner_label("runner_ext_bucket", {"ext_vwap_atr": ext}) == label


@pytest.mark.parametrize("m,label", [
    (5, "<15m"), (15, "15-30m"), (45, "30-60m"), (60, "60-120m"), (130, "120m+"),
    (None, "?")])
def test_staleness_bucket(m, label):
    assert buckets.runner_label("runner_staleness_bucket", {"staleness_min": m}) == label


def test_new_dimensions_are_mined_and_the_review_pair_is_preregistered():
    from agent import structural
    for dim in ("runner_index_tape", "runner_ext_bucket", "runner_staleness_bucket"):
        assert dim in buckets.RUNNER_DIMS
    assert ("runner", "runner_index_tape", "runner_ext_bucket") in structural.PAIR_SPEC
    assert structural.PAIR_SPEC_VERSION == 2


def test_rvol_edges_keep_the_low_rvol_mass_split():
    # The brief proposed <1 / 1-1.5 / 1.5-2 / 2-3 / 3+. With 357 of 367
    # candidates under 1.5x (median 0.33x) a single "<1" cell would hold
    # >= 90% of the dimension and be marked degenerate -- not tested at all.
    # The live edges (17a55df) split that mass at 0.5x instead.
    labels = {buckets.runner_label("runner_rvol_bucket", {"rvol": v})
              for v in (0.2, 0.33, 0.6, 0.9)}
    assert labels == {"<0.5", "0.5-1"}


# ================================================================ continuation

def _drow(d, ticker, drift, industry=None, **kw):
    return {"session_date": d, "ticker": ticker, "industry": industry,
            "drift_atr": drift, **kw}


def test_drift_family_counts_units_and_lives_in_its_own_dimensions():
    rows = ([_drow(SESSION, f"B{i}", -0.4, "Banks", spy_vwap_pct=-0.2, qqq_vwap_pct=-0.1)
             for i in range(30)]
            + [_drow(SESSION, "SOLO", 0.8, None, spy_vwap_pct=0.1, qqq_vwap_pct=0.2)])
    comps = runner_research.drift_comparisons(rows)
    overall = next(c for c in comps if c.dimension == runner_research.DRIFT_ALL)
    assert overall.n == 2, "thirty banks on one tape are one unit"
    assert overall.mean_r == pytest.approx((-0.4 + 0.8) / 2)
    tape = {c.level: c for c in comps if c.dimension == "drift:runner_index_tape"}
    assert tape["both_below"].n == 1 and tape["both_above"].mean_r == pytest.approx(0.8)
    assert all(c.dimension.startswith(runner_research.DRIFT_PREFIX) for c in comps)
    assert runner_research.drift_comparisons([]) == []


def test_no_rule_can_be_stated_on_a_drift_dimension():
    comps = [Comparison(dimension="drift:runner_index_tape", level="both_above",
                        n=20, mean_r=0.3, std_r=1.0)]
    canon, state, _ = research.validate_predicate(
        {"lane": "runner", "conditions": [{"dimension": "drift:runner_index_tape",
                                           "level": "both_above"}]}, comps)
    assert canon is None and state == "REJECTED_UNMATCHABLE"


def test_continuation_md_states_the_null_outcome():
    rows = [_drow(SESSION, "AAA", 0.5, "X"), _drow(SESSION, "BBB", -0.5, "Y")]
    md = runner_research.continuation_md(runner_research.drift_comparisons(rows), rows)
    assert "cannot be won by any stop or target rule" in md
    assert "redesign or a pause" in md
    assert "No candidate" in runner_research.continuation_md([], [])


def test_weekly_search_scores_the_drift_family_in_the_same_batch(con, monkeypatch):
    from agent import integrity
    rows = [_drow(date(2026, 9, d), f"T{i}", 0.1 * ((i % 3) - 1), f"I{i}")
            for d in (14, 15, 16) for i in range(12)]
    monkeypatch.setattr(runner_research, "current_drift_rows", lambda con, sessions=60: rows)
    search = integrity.build_search(con, ledger)
    drift = [c for c in search.comparisons
             if c.dimension.startswith(runner_research.DRIFT_PREFIX)]
    assert any(c.dimension == runner_research.DRIFT_ALL for c in drift)
    assert all(c.q_value is not None or c.p_value is None for c in drift)


def test_lane_stop_condition_is_preregistered_and_reads_only_corrected_findings():
    pos_drift = Comparison(dimension="drift:runner_index_tape", level="both_above",
                           n=30, mean_r=0.2, std_r=1.0, significant=True)
    neg_arm = _exit_comp("arm_a_floor", -0.1)
    unsig_arm = _exit_comp("arm_b_dashboard", 0.3, significant=False)
    early = runner_research.lane_verdict(5, [], [neg_arm, unsig_arm])
    assert "5/20" in early and "too early" in early
    met = runner_research.lane_verdict(runner_research.LANE_VERDICT_SESSIONS, [],
                                       [neg_arm, unsig_arm])
    assert "MET" in met and "redesign or a pause" in met
    alive = runner_research.lane_verdict(25, [pos_drift], [neg_arm])
    assert "NOT met" in alive and "runner_index_tape=both_above" in alive
    # One strong tape cannot settle it either way before 20 sessions.
    one_day = runner_research.lane_verdict(1, [pos_drift], [neg_arm])
    assert "too early" in one_day and "NOT met" not in one_day
    assert "Early positives" in one_day
    # An arm that beats the plan without making money does not keep the lane
    # alive (10/02: every arm beat the plan, every arm lost).
    better = _exit_comp("arm_c_floor_dashboard", 0.5)
    still_losing = Comparison(dimension=runner_exits.EXPECTANCY_DIMENSION,
                              level="arm_c_floor_dashboard", n=20, mean_r=-0.15,
                              std_r=0.2, significant=True)
    met = runner_research.lane_verdict(20, [], [better], [still_losing])
    assert "MET" in met and "not an edge" in met and "arm_c_floor_dashboard" in met
    paying = Comparison(dimension=runner_exits.EXPECTANCY_DIMENSION,
                        level="arm_c_floor_dashboard", n=20, mean_r=0.1, std_r=0.2,
                        significant=True)
    assert "NOT met" in runner_research.lane_verdict(20, [], [better], [paying])


def test_construction_expectancy_is_per_session_and_counts_plan_and_arms():
    rows = []
    for d, vals in ((date(2026, 10, 2), (-1.0, 0.0)), (date(2026, 10, 5), (0.5, 0.5, 0.5))):
        for i, v in enumerate(vals):
            for pol in ("plan", *runner_exits.ARMS):
                rows.append({"session_date": d, "ticker": f"T{i}", "policy": pol,
                             "r_planned": v})
    comps = {c.level: c for c in runner_exits.arm_expectancy_comparisons(rows)}
    assert set(comps) == {"plan", *runner_exits.ARMS}
    assert comps["arm_a_floor"].n == 2
    assert comps["arm_a_floor"].mean_r == pytest.approx((-0.5 + 0.5) / 2)
    assert all(c.dimension == runner_exits.EXPECTANCY_DIMENSION for c in comps.values())
    assert runner_exits.arm_expectancy_comparisons([]) == []
    md = runner_exits.exit_md(runner_exits.exit_policy_comparisons(rows), rows,
                              list(comps.values()))
    assert "make money at all" in md and "not an edge" in md


# ================================================================ forward validation

def _exit_comp(level, mean, significant=True, n=9):
    return Comparison(dimension=runner_exits.DIMENSION, level=level, n=n, mean_r=mean,
                      std_r=0.1, q_value=0.04 if significant else 0.5,
                      significant=significant)


def test_a_surviving_positive_arm_is_quarantined_by_the_system(con):
    comps = [_exit_comp("arm_a_floor", 0.12), _exit_comp("arm_b_dashboard", -0.2),
             _exit_comp("arm_c_floor_dashboard", 0.3, significant=False)]
    got = research.register_exit_policy_candidates(con, SESSION, comps)
    assert [g["policy"] for g in got] == ["arm_a_floor"]
    st = {s["rule_id"]: s for s in research.rule_states(con)}[got[0]["rule_id"]]
    assert st["state"] == "QUARANTINED" and st["lane"] == research.EXIT_LANE
    assert st["source"] == "system" and st["discovery_effect"] == pytest.approx(0.12)
    assert research.register_exit_policy_candidates(con, SESSION, comps) == []


def test_the_miner_naming_the_same_arm_maps_to_the_same_rule(con):
    comps = [_exit_comp("arm_a_floor", 0.12)]
    rid = research.register_exit_policy_candidates(con, SESSION, comps)[0]["rule_id"]
    canon, state, _ = research.validate_predicate(
        research.exit_rule_predicate("arm_a_floor"), comps)
    assert state == "" and research.rule_id_for_predicate(canon) == rid
    bad, state, _ = research.validate_predicate(
        research.exit_rule_predicate("arm_z"), comps)
    assert bad is None and state == "REJECTED_UNMATCHABLE"


def _seed_session(con, d, deltas: dict[str, list[float]]):
    """Candidates + v4 replays for session d: plan 0.0, each policy plan+delta."""
    scan_v, sim_v = runner_research.current_versions()
    n = len(next(iter(deltas.values())))
    rows = []
    for i in range(n):
        t = f"T{i}"
        con.execute("INSERT INTO runner_candidates (session_date, ticker, scan_version) "
                    "VALUES (?, ?, ?)", [d, t, scan_v])
        rows.append({"ticker": t, "policy": "plan", "policy_version": runner_exits.POLICY_VERSION,
                     "sim_version": sim_v, "r_planned": 0.0})
        for pol, ds in deltas.items():
            rows.append({"ticker": t, "policy": pol,
                         "policy_version": runner_exits.POLICY_VERSION,
                         "sim_version": sim_v, "r_planned": ds[i]})
    ledger.save_runner_exit_replays(con, d, rows)


def test_one_forward_observation_per_later_session_and_never_in_sample(con):
    rid = research.register_exit_policy_candidates(
        con, SESSION, [_exit_comp("arm_a_floor", 0.12)])[0]["rule_id"]
    _seed_session(con, SESSION, {"arm_a_floor": [0.5, 0.5]})          # in-sample
    nxt = SESSION + timedelta(days=1)
    _seed_session(con, nxt, {"arm_a_floor": [0.2, -0.1, 0.5]})
    research.observe_session(con, SESSION)
    research.observe_session(con, nxt)
    research.observe_session(con, nxt)                                 # a retried evening
    st = {s["rule_id"]: s for s in research.rule_states(con)}[rid]
    assert st["forward_n"] == 1, "the session is the unit; in-sample never counts"
    assert st["forward_mean_r"] == pytest.approx((0.2 - 0.1 + 0.5) / 3)


# ================================================================ selection coverage

def _cov_candidate(con, d, ticker, scan_v, sim_v, *, selected=False, triggered=True, r=0.1):
    con.execute("INSERT INTO runner_candidates (session_date, ticker, scan_version) "
                "VALUES (?, ?, ?)", [d, ticker, scan_v])
    if sim_v is None:
        return
    ledger.save_runner_candidate_outcome(con, {
        "session_date": d, "ticker": ticker, "triggered": triggered,
        "r_planned": r if triggered else None, "sim_version": sim_v,
        "selected": selected})


def test_every_session_states_why_it_is_or_is_not_contested(con):
    scan_v, sim_v = runner_research.current_versions()
    today = date.today()
    d1, d2, d3, d4 = (today - timedelta(days=k) for k in (4, 3, 2, 1))
    _cov_candidate(con, d1, "A", scan_v, sim_v, selected=True)
    _cov_candidate(con, d1, "B", scan_v, sim_v)
    _cov_candidate(con, d2, "C", scan_v, sim_v)
    ledger.upsert_runner_session(con, {
        "session_date": d2, "selection_skipped_reason":
        "selection_failed: Anthropic API failed after 3 attempts: credit balance is too low"})
    _cov_candidate(con, d3, "D", scan_v - 1, sim_v, selected=True)
    _cov_candidate(con, d3, "E", scan_v - 1, sim_v)
    _cov_candidate(con, d4, "F", scan_v, None)
    cov = {c["session_date"]: c for c in runner_research.selection_coverage(con)}
    assert cov[d1]["contested"] and cov[d1]["reason"] == "contested"
    assert cov[d2]["reason"].startswith("the selector failed")
    assert cov[d3]["reason"].startswith("excluded by the version pin")
    assert cov[d4]["reason"].startswith("not graded yet")
    md = runner_research.selection_coverage_md(list(cov.values()))
    assert "1 of 4" in md


# ================================================================ evening + views

def test_evening_stores_entry_context_for_every_candidate(con):
    scan_v, _ = runner_research.current_versions()
    con.execute(
        """INSERT INTO runner_candidates
           (session_date, ticker, first_pass_et, scan_pass_et, detection_price,
            stop_price, target_price, stop_dist, scan_version)
           VALUES (?, 'RUNR', ?, ?, 10.0, 9.5, 11.0, 0.5, ?)""",
        [SESSION, datetime(2026, 9, 16, 10, 5), datetime(2026, 9, 16, 10, 30), scan_v])
    stock = _bars(_rise() + [10.3] * 300)
    spy = _bars([400 + 0.05 * i for i in range(390)])
    qqq = _bars([300 - 0.05 * i for i in range(390)])

    class _MD:
        def minute_bars(self, sym, d):
            return {"SPY": spy, "QQQ": qqq}.get(sym, stock)

        def daily_context(self, syms):
            return {"RUNR": {"atr14": 0.6}}

    from agent.loops import evening
    summary = evening._grade_runner_candidates(con, _MD(), SESSION,
                                               Config(raw={"slippage_bps": 5}))
    assert summary["entry_context"] == 1
    o = ledger.runner_outcomes_for_date(con, SESSION)[0]
    assert o["entry_et"] == datetime(2026, 9, 16, 10, 30)
    assert o["entry_px"] == pytest.approx(10.0)
    assert o["staleness_min"] == pytest.approx(25.0)
    assert o["spy_vwap_pct"] > 0 > o["qqq_vwap_pct"]
    assert buckets.runner_label("runner_index_tape", o) == "split"
    assert o["drift_atr"] == pytest.approx((10.3 - 10.0) / 0.6, abs=1e-3)
    assert o["ext_vwap_atr"] is not None
    replays = {r["policy"]: r for r in ledger.runner_exit_replays_since(con, SESSION)}
    assert replays["arm_a_floor"]["stop_used"] == pytest.approx(min(9.5, 10.0 - 0.6))
    assert replays["plan"]["cost_r"] is not None

    from agent import runner_view
    s = runner_view.session_summary(con, SESSION)
    row = s["rows"][0]
    assert row["index_tape"] == "split" and row["drift_atr"] == pytest.approx(o["drift_atr"])
    cs = runner_view.construction_summary(con, s)
    assert [a["policy"] for a in cs["arms"]] == ["plan", *runner_exits.ARMS]
    assert all(a["n"] == 1 for a in cs["arms"])
    assert cs["continuation"]["n"] == 1 and cs["continuation"]["up"] == 1
    text = runner_view.render_text(con, SESSION)
    assert "CONSTRUCTION ARMS" in text and "CONTINUATION AFTER ENTRY" in text


def test_playbook_runner_section_reports_arms_and_never_lists_them_as_selection_rules():
    from agent.loops import weekly
    from agent.runner_selection import NO_RUNNER_RULES
    exit_comp = _exit_comp("arm_b_dashboard", 0.1)
    drift_all = Comparison(dimension=runner_research.DRIFT_ALL, level="all_candidates",
                           n=40, mean_r=-0.2, std_r=0.5, q_value=0.01, significant=True)
    states = [{"rule_id": "Rabc", "state": "VALIDATED", "lane": research.EXIT_LANE,
               "statement": "Runner exit policy `arm_b_dashboard` beats the plan",
               "dimensions": [research.EXIT_DIMENSION], "forward_n": 8,
               "forward_n_required": 8,
               "predicate": research.exit_rule_predicate("arm_b_dashboard")}]
    pays = Comparison(dimension=runner_exits.EXPECTANCY_DIMENSION, level="arm_b_dashboard",
                      n=9, mean_r=0.15, std_r=0.1, q_value=0.03, significant=True)
    lines = "\n".join(weekly._runner_section(
        {"runner_exit_comps": [exit_comp], "runner_drift_comps": [drift_all],
         "runner_expectancy_comps": [pays]}, states))
    assert NO_RUNNER_RULES in lines, "an exit rule is not a selection rule"
    assert "Construction arms vs the plan" in lines and "arm_b_dashboard" in lines
    assert "Continuation after entry" in lines
    assert "awaiting Jason's approval" in lines
    assert "[rule: Rabc]" not in lines

    # Validated as BETTER than the plan, but losing money on its own: a smaller
    # loss is not a live candidate.
    loses = Comparison(dimension=runner_exits.EXPECTANCY_DIMENSION, level="arm_b_dashboard",
                       n=9, mean_r=-0.2, std_r=0.1, q_value=0.03, significant=True)
    lines = "\n".join(weekly._runner_section(
        {"runner_exit_comps": [exit_comp], "runner_expectancy_comps": [loses]}, states))
    assert "awaiting Jason's approval" not in lines
    assert "NOT making money on its own" in lines
    assert "not a positive expectancy" in lines


def test_playbook_suppresses_an_arm_under_eight_sessions():
    from agent.loops import weekly
    lines = "\n".join(weekly._runner_section(
        {"runner_exit_comps": [_exit_comp("arm_a_floor", 0.4, significant=False, n=3)]}, []))
    assert "arm_a_floor: 3 session(s), under n=8, ΔR suppressed" in lines
    assert "+0.40" not in lines
    assert "No arm or exit policy has validated" in lines
