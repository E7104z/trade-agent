"""
End-to-end smoke test for the v4 research layer.

Unit tests prove each module is right in isolation. This file proves they work
TOGETHER against a schema shaped like the real ledger, driven through a
simulated multi-week season: mornings snapshot, evenings grade, weeks mine, and
a rule is discovered, quarantined, and graduated on forward evidence only.

It also pins the two compatibility facts the integration guide asserts about the
existing codebase:
  * every base-table INSERT names its columns, so added columns cannot shift
  * `picks` is read via DESCRIBE + SELECT *, so `rule_id` appears as None
"""
from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pytest

from agent import ablation, integrity, migrations_v4, population, research
from agent.friction import params_from_config

# A faithful subset of the real schema: the columns these modules actually
# touch, created WITHOUT the v4 additions so the migrations do real work.
_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS picks (
    pick_id INTEGER PRIMARY KEY, plan_date DATE NOT NULL, ticker VARCHAR NOT NULL,
    direction VARCHAR, setup_type VARCHAR, conviction INTEGER, size_r DOUBLE,
    entry_type VARCHAR, entry_price DOUBLE, stop_price DOUBLE, target_price DOUBLE,
    time_stop VARCHAR, regime_tag VARCHAR, playbook_version VARCHAR,
    in_scan_universe BOOLEAN
);
CREATE TABLE IF NOT EXISTS outcomes (
    pick_id INTEGER PRIMARY KEY, eval_date DATE, triggered BOOLEAN,
    entry_fill DOUBLE, exit_price DOUBLE, exit_reason VARCHAR, r_multiple DOUBLE,
    mfe_r DOUBLE, mae_r DOUBLE, baseline_r DOUBLE, eod_r DOUBLE,
    rvol_at_1000 DOUBLE, vwap_side_at_fill VARCHAR, stop_dist_atr DOUBLE,
    gap_atr DOUBLE, ext_atr_at_entry DOUBLE, trend_side_200d VARCHAR,
    mkt_cap DOUBLE, short_pct_float DOUBLE, or_break_dir VARCHAR,
    fill_vs_or VARCHAR, grade VARCHAR, sim_version INTEGER
);
CREATE TABLE IF NOT EXISTS rejections (
    rejection_id INTEGER PRIMARY KEY, plan_date DATE, ticker VARCHAR,
    direction VARCHAR, setup_type VARCHAR, reason VARCHAR, entry_price DOUBLE,
    stop_price DOUBLE, target_price DOUBLE, conviction INTEGER, thesis VARCHAR,
    eod_outcome VARCHAR, forgone_r DOUBLE, sim_version INTEGER
);
CREATE TABLE IF NOT EXISTS daily_plans (
    plan_date DATE PRIMARY KEY, no_trade BOOLEAN, market_summary VARCHAR,
    plan_json VARCHAR, regime_tag VARCHAR
);
"""


class _Cfg:
    def __init__(self, raw=None):
        self.raw = raw or {}

    def get(self, k, d=None):
        return self.raw.get(k, d)


class _MD:
    """Deterministic market data: gap-ups run, gap-downs bleed."""

    def daily_bar(self, sym, d):
        if sym.startswith("UP"):
            return {"open": 100.0, "high": 108.0, "low": 99.0, "close": 106.0, "volume": 1_000_000}
        return {"open": 100.0, "high": 101.0, "low": 92.0, "close": 94.0, "volume": 1_000_000}

    def minute_bars(self, sym, d):
        b = self.daily_bar(sym, d)
        return [{"o": b["open"], "h": b["high"], "l": b["low"], "c": b["close"], "v": 1000}]


class _Ledger:
    """Mimics agent.ledger's stats_by / stats_by_outcome return shape."""

    DIMS = {d: s for d, s in integrity.STATS_DIMENSION_SPEC}

    def _rows(self, con, dim):
        col = {"setup_type": "p.setup_type", "regime_tag": "p.regime_tag",
               "direction": "p.direction", "conviction": "CAST(p.conviction AS VARCHAR)",
               "in_scan_universe": "CAST(p.in_scan_universe AS VARCHAR)",
               "trend_side_200d": "o.trend_side_200d",
               "vwap_side_at_fill": "o.vwap_side_at_fill"}.get(dim)
        if col is None:
            return []
        rows = con.execute(
            f"""SELECT COALESCE({col},'?') AS dim, COUNT(*),
                       SUM(CASE WHEN o.triggered THEN 1 ELSE 0 END),
                       AVG(CASE WHEN o.triggered THEN o.r_multiple END),
                       STDDEV_SAMP(CASE WHEN o.triggered THEN o.r_multiple END)
                FROM picks p JOIN outcomes o ON p.pick_id = o.pick_id
                GROUP BY 1 ORDER BY 2 DESC"""
        ).fetchall()
        out = []
        for dim_v, n, trig, avg, std in rows:
            out.append({"dim": dim_v, "n": n, "triggered": trig or 0, "wins": 0,
                        "win_rate": None,
                        "avg_r": round(float(avg), 3) if avg is not None else None,
                        "total_weighted_r": 0.0,
                        "std_r": round(float(std), 3) if std is not None else None,
                        "t_stat": None, "baseline_avg_r": None})
        return out

    def stats_by(self, con, dim, lane=None):
        return self._rows(con, dim)

    def stats_by_outcome(self, con, dim, lane=None):
        return self._rows(con, dim)


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute(_BASE_SCHEMA)
    for stmt in migrations_v4.research_schema():      # migrations do real work here
        c.execute(stmt)
    yield c
    c.close()


def _season(con, weeks=4, per_day=2):
    """Simulate a trading season: mornings, picks, rejections, outcomes."""
    md, pid, rid = _MD(), 0, 0
    start = date(2026, 5, 4)
    days = []
    for w in range(weeks):
        for dow in range(5):
            d = start + timedelta(days=w * 7 + dow)
            days.append(d)
            gappers = [
                {"symbol": f"UP{chr(65 + dow)}", "last": 50.0, "prev_close": 48.0,
                 "gap_pct": 4.2, "atr14": 2.0, "adv20": 3e6, "in_scan_universe": True},
                {"symbol": f"DN{chr(65 + dow)}", "last": 20.0, "prev_close": 21.0,
                 "gap_pct": -4.8, "atr14": 1.0, "adv20": 1e6, "in_scan_universe": True},
                {"symbol": f"UPX{chr(65 + dow)}", "last": 30.0, "prev_close": 29.0,
                 "gap_pct": 3.4, "atr14": 1.5, "adv20": 2e6, "in_scan_universe": False},
            ]
            picks = []
            for i in range(per_day):
                pid += 1
                sym = f"UP{chr(65 + dow)}" if i == 0 else f"DN{chr(65 + dow)}"
                r = 1.2 if sym.startswith("UP") else -0.8
                con.execute(
                    """INSERT INTO picks (pick_id, plan_date, ticker, direction,
                       setup_type, conviction, size_r, entry_type, entry_price,
                       stop_price, target_price, regime_tag, playbook_version,
                       in_scan_universe)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,TRUE)""",
                    [pid, d, sym, "long" if i == 0 else "short",
                     "gap_go" if i == 0 else "gap_fade", 4, 1.0, "open",
                     100.0, 98.0, 106.0, "uptrend/risk_on/lowvol", "3"])
                con.execute(
                    """INSERT INTO outcomes (pick_id, eval_date, triggered, r_multiple,
                       exit_reason, trend_side_200d, vwap_side_at_fill, sim_version)
                       VALUES (?,?,TRUE,?, 'eod', 'above', 'above', 2)""",
                    [pid, d, r + (0.05 if pid % 2 else -0.05)])
                picks.append({"ticker": sym})
            rid += 1
            con.execute(
                """INSERT INTO rejections (rejection_id, plan_date, ticker, direction,
                   setup_type, reason, eod_outcome, forgone_r, sim_version)
                   VALUES (?,?,?,'long','sympathy','too correlated','would_have_lost',?,2)""",
                [rid, d, f"UPX{chr(65 + dow)}", -0.4])
            con.execute(
                "INSERT INTO daily_plans (plan_date, no_trade, regime_tag) VALUES (?, FALSE, ?)",
                [d, "uptrend/risk_on/lowvol"])
            integrity.on_morning(con, _Cfg(), d, {"premarket_gappers": gappers},
                                 {"picks": picks})
            integrity.on_evening(con, _Cfg(), md, d)
    return days


# ------------------------------------------------------------------ the season

def test_a_full_season_runs_through_every_module_without_error(con):
    days = _season(con)
    assert len(days) == 20
    assert con.execute("SELECT COUNT(*) FROM population_universe").fetchone()[0] == 60
    assert con.execute("SELECT COUNT(*) FROM population_outcomes").fetchone()[0] == 120
    assert con.execute("SELECT COUNT(*) FROM picks").fetchone()[0] == 40


def test_weekly_block_produces_a_complete_report_over_real_rows(con):
    _season(con)
    res = integrity.weekly_block(con, _Cfg(), _Ledger(), date(2026, 6, 1))
    md = res["markdown"]
    for section in ("RESEARCH INTEGRITY", "POPULATION BASELINE",
                    "SELECTION ABLATION", "QUARANTINE DECISIONS"):
        assert section in md, f"missing section: {section}"
    assert res["looks"]["looks_this_run"] > 0
    assert res["population"]["selection_edge_r"] is not None
    assert res["ablation"]["sessions"] > 0


def test_selection_edge_is_computed_against_the_population_not_the_picks(con):
    _season(con)
    stats = population.population_stats(con)
    # Momentum monkey: gap-ups open 100 -> close 106 with a 2.0 ATR stop (low 99
    # never reaches 98) = +3.0R. It should comfortably beat the desk's ~+1.2R.
    assert stats["best_arm"] == "momentum"
    assert stats["selection_edge_r"] < 0
    assert "DESTROYING value" in population.population_md(stats)


def test_ablation_runs_over_the_whole_back_history_with_no_api_cost(con):
    _season(con)
    res = ablation.random_pick_ablation(con, draws=1000, sim_version=2)
    assert res["sessions"] == 20
    assert res["picks"] == 40
    assert res["percentile"] is not None
    assert ablation.random_pick_ablation(con, draws=1000, sim_version=2) == res


# ------------------------------------------------- discovery -> quarantine -> validation

def test_a_rule_can_only_graduate_on_evidence_that_postdates_its_quarantine(con):
    _season(con, weeks=2)
    discovery_day = date(2026, 5, 15)

    reg = research.register_rule(
        con, "gap_go longs in uptrend/risk_on beat the population",
        ["setup_type", "regime_tag"], discovery_day,
        discovery_n=10, discovery_effect=0.62, discovery_q=0.04)
    research.advance_quarantine(con, reg["rule_id"], discovery_day)

    # Backfill "evidence" from BEFORE the quarantine -- must all be refused.
    for i in range(10):
        assert research.record_rule_observation(
            con, reg["rule_id"], discovery_day - timedelta(days=i), 1.5) is False
    assert research.eligible_for_production(
        con, date(2026, 6, 30), apply=False)["graduated"] == []

    # Genuine forward evidence, spread across sessions.
    for i in range(10):
        assert research.record_rule_observation(
            con, reg["rule_id"], discovery_day + timedelta(days=i + 1),
            1.2 + (0.1 if i % 2 else -0.1)) is True

    res = integrity.weekly_block(con, _Cfg(), _Ledger(), date(2026, 6, 30))
    assert [r["rule_id"] for r in res["graduation"]["graduated"]] == [reg["rule_id"]]

    pb = f"## Setup Rules\n\n- gap_go longs beat the population [rule: {reg['rule_id']}]\n"
    assert integrity.check_playbook(pb, research.rule_states(con)) == []


def test_playbook_claiming_a_quarantined_rule_is_blocked_end_to_end(con):
    _season(con, weeks=1)
    reg = research.register_rule(con, "shorts work in chop", ["setup_type"],
                                 date(2026, 5, 4), discovery_n=6, discovery_effect=0.4)
    research.advance_quarantine(con, reg["rule_id"], date(2026, 5, 4))
    res = integrity.weekly_block(con, _Cfg(), _Ledger(), date(2026, 5, 11))
    pb = f"## Setup Rules\n\n- shorts have positive expectancy [rule: {reg['rule_id']}]\n"
    violations = integrity.check_playbook(pb, res["states"])
    assert len(violations) == 1 and "not VALIDATED" in violations[0]


def test_three_way_rule_is_refused_at_registration_end_to_end(con):
    _season(con, weeks=1)
    reg = research.register_rule(
        con, "gap_go x uptrend x rvol>2 prints +0.84R",
        ["setup_type", "regime_tag", "rvol_bucket"], date(2026, 5, 4),
        discovery_n=30, discovery_effect=0.84, discovery_q=0.01)
    assert reg["state"] == "REJECTED_STRUCTURAL"
    res = integrity.weekly_block(con, _Cfg(), _Ledger(), date(2026, 5, 11))
    assert "REJECTED_STRUCTURAL" in res["markdown"]


# ------------------------------------------------------------------- friction

def test_stress_ladder_columns_persist_alongside_an_untouched_r_multiple(con):
    _season(con, weeks=1)

    def _sim(pick, bars, daily, **kw):
        slip = kw.get("slippage_bps", 0.0) / 10_000.0
        return {"triggered": True, "r_multiple": round(1.2 - slip * 200, 4)}

    params = params_from_config(_Cfg({
        "slippage_bps": 5,
        "friction": {"model": "participation", "stress_multipliers": [1.0, 2.0, 3.0]},
    }))
    pick = {"entry_price": 25.0, "stop_price": 24.0, "size_r": 1.0}
    stress = integrity.stress_ladder_for_pick(
        pick, [], None, _sim, params, atr_pct=3.0, adv20=2_000_000, dollars_per_r=500)

    before = con.execute("SELECT r_multiple FROM outcomes WHERE pick_id = 1").fetchone()[0]
    con.execute(
        """UPDATE outcomes SET r_multiple_2x = ?, r_multiple_3x = ?,
           slippage_bps_used = ?, friction_model = ?, participation_pct = ?
           WHERE pick_id = 1""",
        [stress["r_multiple_2x"], stress["r_multiple_3x"], stress["slippage_bps_used"],
         stress["friction_model"], stress["participation_pct"]])
    row = con.execute(
        """SELECT r_multiple, r_multiple_2x, r_multiple_3x, friction_model
           FROM outcomes WHERE pick_id = 1""").fetchone()
    assert row[0] == before                 # primary R is NEVER recomputed
    assert row[2] < row[1]                  # more friction, less R
    assert row[3] == "participation"


def test_flat_friction_is_a_true_no_op_against_the_current_config(con):
    params = params_from_config(_Cfg({"slippage_bps": 5}))
    assert params.model == "flat"
    calls = []

    def _sim(pick, bars, daily, **kw):
        calls.append(kw.get("slippage_bps"))
        return {"triggered": True, "r_multiple": 1.0}

    integrity.stress_ladder_for_pick(
        {"entry_price": 25.0, "stop_price": 24.0, "size_r": 1.0},
        [], None, _sim, params, atr_pct=6.0, adv20=100_000)
    # stress rungs are pure multiples of the existing flat rate -- no new model
    assert calls == [10.0, 15.0]


# --------------------------------------------------- compatibility assertions

def test_added_columns_do_not_break_a_describe_plus_select_star_read(con):
    """Pins the `picks_for_date` pattern used by the real ledger."""
    _season(con, weeks=1)
    cols = [c[0] for c in con.execute("DESCRIBE picks").fetchall()]
    rows = con.execute("SELECT * FROM picks WHERE plan_date = ? ORDER BY pick_id",
                       [date(2026, 5, 4)]).fetchall()
    picks = [dict(zip(cols, r)) for r in rows]
    assert picks and "rule_id" in picks[0]
    assert picks[0]["rule_id"] is None          # benign: present, always None
    assert picks[0]["ticker"] and picks[0]["entry_price"] == 100.0


def test_named_insert_into_outcomes_still_works_after_five_columns_are_added(con):
    """Pins the `save_outcome` pattern: named columns are immune to additions."""
    con.execute("""INSERT INTO picks (pick_id, plan_date, ticker, direction)
                   VALUES (999, ?, 'ZZZZ', 'long')""", [date(2026, 5, 4)])
    con.execute("""INSERT INTO outcomes (pick_id, eval_date, triggered, r_multiple,
                   exit_reason, sim_version) VALUES (999, ?, TRUE, 1.0, 'eod', 2)""",
                [date(2026, 5, 4)])
    row = con.execute("""SELECT r_multiple, r_multiple_2x, friction_model
                         FROM outcomes WHERE pick_id = 999""").fetchone()
    assert row == (1.0, None, None)


def test_migrations_are_safe_to_run_repeatedly_on_a_populated_database(con):
    _season(con, weeks=1)
    before = con.execute("SELECT COUNT(*) FROM population_universe").fetchone()[0]
    for _ in range(3):
        for stmt in migrations_v4.research_schema():
            con.execute(stmt)
    assert con.execute("SELECT COUNT(*) FROM population_universe").fetchone()[0] == before


def test_the_whole_layer_degrades_rather_than_raising_when_everything_breaks(con):
    """The contract that matters most: research code cannot halt a trading loop."""
    class _Broken:
        def execute(self, *a, **k):
            raise RuntimeError("database on fire")

    class _BrokenLedger:
        def stats_by(self, *a, **k):
            raise RuntimeError("nope")

        def stats_by_outcome(self, *a, **k):
            raise RuntimeError("nope")

    class _BrokenMD:
        def daily_bar(self, *a):
            raise RuntimeError("feed down")

        def minute_bars(self, *a):
            raise RuntimeError("feed down")

    d = date(2026, 5, 4)
    assert integrity.on_morning(_Broken(), _Cfg(), d, {"premarket_gappers": [
        {"symbol": "A", "last": 1.0, "prev_close": 1.0}]}, {})["population_rows"] == 0
    integrity.on_evening(_Broken(), _Cfg(), _BrokenMD(), d)
    res = integrity.weekly_block(_Broken(), _Cfg(), _BrokenLedger(), d)
    assert isinstance(res["markdown"], str)
    assert integrity.check_playbook("## Rules\n\n- edge here\n", []) != []
