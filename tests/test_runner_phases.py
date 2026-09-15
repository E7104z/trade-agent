"""
Phase 1 and Phase 2 tests for the DTO Runner lane
(specs/RUNNER_PORT_SPEC_v1.md §1.8 and §2.7).

Everything here runs against a real temporary ledger (DuckDB) with stubbed
providers and a stubbed LLM — no network, no orders, no tokens. The exit
criteria that genuinely require live market sessions (15 for Phase 1, 10 for
Phase 2) cannot be simulated and are not claimed here; what IS pinned is every
mechanism those sessions would exercise.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone

import pytest

from agent import ledger, runner_scan, runner_selection, runner_universe
from agent.config import Config

ET = timezone(timedelta(hours=-4))
SESSION = date(2026, 9, 11)


# --------------------------------------------------------------- fixtures

@pytest.fixture()
def con(monkeypatch):
    """A real ledger on a temp DuckDB file, so migrations actually run."""
    fd, path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(path)
    monkeypatch.setattr(ledger, "DB_PATH", path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()
    for p in (path, path + ".wal"):
        try:
            os.unlink(p)
        except OSError:
            pass


def _cfg(**runner_over) -> Config:
    runner = {
        "enabled": True, "scan_start_et": "09:45", "scan_end_et": "15:00",
        "universe_size": 6000, "price_min": 5.0, "price_max": 50.0,
        "min_vwap_pct": 0.5, "min_rvol": 1.5, "float_min_m": 5.0,
        "float_max_m": 200.0, "min_trend_consistency": 45.0, "walkback": True,
        "earnings_exclude_days": 2, "decision_time_et": "10:30", "max_picks": 2,
        "selection_enabled": True, "selection_model": "test-model",
        "live_orders_enabled": False, "max_live_orders_per_day": 10,
        "dollars_per_r": 100, "size_r": 0.25,
    }
    runner.update(runner_over)
    return Config(raw={"runner": runner, "live_paper": {"enabled": False},
                       "slippage_bps": 0, "min_bar_volume": 0,
                       "flatten_at": None, "min_size_r": 0.25})


def _candidate(ticker: str, *, price=20.0, minutes=630, rvol=3.0,
               stop_dist=0.5, tc=70.0) -> dict:
    return {
        "session_date": SESSION, "ticker": ticker,
        "first_pass_et": datetime(2026, 9, 11, minutes // 60, minutes % 60),
        "detection_minutes": minutes,
        "scan_pass_et": datetime(2026, 9, 11, 11, 30),
        "detection_price": price, "vwap": price * 0.99,
        "vwap_pct_above": 1.0, "ema9": price - stop_dist, "ema9_slope": 0.03,
        "rvol": rvol, "trend_consistency": tc, "change_pct": 4.0,
        "float_shares": 40.0, "earnings_days_away": 30,
        "prev_close": price * 0.96, "prev_volume": 2_000_000,
        "price_tier": "5-20" if price < 20 else "20-50",
        "stop_price": price - stop_dist,
        "target_price": price + 2 * stop_dist, "stop_dist": stop_dist,
        "gates_status": json.dumps({"float": "pass"}), "scan_version": 1,
    }


# ====================================================================
# PHASE 1 — schema, first-pass anchoring, session telemetry
# ====================================================================

def test_v10_tables_exist_with_expected_keys(con):
    for t in ("runner_candidates", "runner_candidate_outcomes", "runner_sessions"):
        cols = [c[0] for c in con.execute(f"DESCRIBE {t}").fetchall()]
        assert "session_date" in cols
    assert "scan_version" in [c[0] for c in con.execute("DESCRIBE runner_candidates").fetchall()]


def test_first_pass_wins_later_cycles_never_overwrite(con):
    """The detection snapshot is an anchor. If a later scan cycle could rewrite
    it, detection_price would drift with the tape and detection_minutes would
    stop meaning anything."""
    assert ledger.save_runner_candidate(con, _candidate("AAAA", price=20.0)) is True
    # Same ticker, later cycle, different price — must be ignored entirely.
    assert ledger.save_runner_candidate(con, _candidate("AAAA", price=99.0)) is False

    rows = ledger.runner_candidates_for_date(con, SESSION)
    assert len(rows) == 1
    assert rows[0]["detection_price"] == 20.0


def test_candidates_are_returned_alphabetically_not_ranked(con):
    """Spec R-1: detection must not rank. If this returned rows ordered by RVOL
    the selector would inherit a ranking and selection quality — the thing
    being measured — would be contaminated by the ranker."""
    for t, rv in (("ZZZZ", 9.0), ("AAAA", 1.6), ("MMMM", 5.0)):
        ledger.save_runner_candidate(con, _candidate(t, rvol=rv))
    assert [r["ticker"] for r in ledger.runner_candidates_for_date(con, SESSION)] \
        == ["AAAA", "MMMM", "ZZZZ"]


def test_runner_session_distinguishes_stand_down_from_outage(con):
    """A zero-candidate day must never be ambiguous after the fact."""
    ledger.upsert_runner_session(con, {
        "session_date": SESSION, "universe_size": 0, "candidates_found": 0,
        "provider_degraded": True})
    outage = ledger.runner_session(con, SESSION)
    assert outage["provider_degraded"] is True and outage["universe_size"] == 0

    ledger.upsert_runner_session(con, {
        "session_date": date(2026, 9, 10), "universe_size": 120,
        "candidates_found": 0, "rejected_conditions": 118,
        "provider_degraded": False})
    stand_down = ledger.runner_session(con, date(2026, 9, 10))
    assert stand_down["provider_degraded"] is False
    assert stand_down["universe_size"] == 120
    assert stand_down["rejected_conditions"] == 118


def test_partial_session_update_does_not_null_scan_counters(con):
    """Selection writes touch the same row as the scan. A partial update must
    not wipe the counters that make the day diagnosable."""
    ledger.upsert_runner_session(con, {
        "session_date": SESSION, "universe_size": 120, "rejected_price": 50,
        "scan_passes": 3})
    ledger.upsert_runner_session(con, {
        "session_date": SESSION, "selection_skipped_reason": "no candidates"})
    row = ledger.runner_session(con, SESSION)
    assert row["universe_size"] == 120
    assert row["rejected_price"] == 50
    assert row["selection_skipped_reason"] == "no candidates"


def test_counter_column_splits_dto_coarse_counters(con):
    """DTO folds excluded/float/earnings into one `conditions` counter. The
    session table keeps them separate — that split is what turns a
    zero-candidate day into a diagnosable one."""
    f = runner_scan._counter_column
    assert f({"counter": "conditions", "reason": "excluded ticker"}) == "rejected_excluded"
    assert f({"counter": "conditions", "reason": "float 4.0M outside [5.0, 200.0]"}) == "rejected_float"
    assert f({"counter": "conditions", "reason": "earnings 1d away"}) == "rejected_earnings"
    assert f({"counter": "conditions", "reason": "9 EMA flat"}) == "rejected_conditions"
    assert f({"counter": "price", "reason": "x"}) == "rejected_price"
    assert f({"counter": "no_bars", "reason": "x"}) == "rejected_no_bars"


def test_outcome_upsert_preserves_selection_stamps(con):
    """Selection happens at 10:30, grading at 20:30. The evening pass must not
    erase the fact that a candidate was selected."""
    ledger.save_runner_candidate(con, _candidate("AAAA"))
    ledger.mark_runner_selected(con, SESSION, "AAAA", 1, 4242)
    ledger.save_runner_candidate_outcome(con, {
        "session_date": SESSION, "ticker": "AAAA", "eval_date": SESSION,
        "triggered": True, "r_multiple": 1.5, "sim_version": 3})

    o = ledger.runner_outcomes_for_date(con, SESSION)[0]
    assert o["r_multiple"] == 1.5
    assert o["selected"] is True
    assert o["selection_rank"] == 1
    assert o["pick_id"] == 4242


def test_universe_drops_excluded_and_non_common(monkeypatch, con):
    """The screeners are unioned INTO the standing list, not used instead of it.

    The first version of build_universe used movers + most-actives alone and
    landed on 84 symbols against a real population of ~4,200, which starved
    detection so thoroughly that the empty result read as a quiet market. So
    this asserts the filters behave AND that the standing list is present.
    """
    class _Alp:
        def movers(self, top=50):
            return {"gainers": [{"symbol": "GOOD"}, {"symbol": "SOXL"}],
                    "losers": [{"symbol": "BADW"}]}
        def most_actives(self, top=50, by="volume"):
            return {"most_actives": [{"symbol": "ALSO"}]}

    class _MD:
        alpaca = _Alp()

    monkeypatch.setattr(runner_universe, "is_common_stock_symbol",
                        lambda s: s != "BADW")
    # No liquid universe in this test: assert the screener/standing-list union.
    monkeypatch.setattr(runner_universe, "liquid_universe",
                        lambda *a, **k: ([], {}))

    at = datetime(2026, 9, 11, 11, 30, tzinfo=ET)
    syms, stats = runner_universe.build_universe(_MD(), _cfg().get("runner"),
                                                 at=at, con=con)
    assert "GOOD" in syms and "ALSO" in syms      # screener names included
    assert "SOXL" not in syms                     # leveraged ETF excluded
    assert "BADW" not in syms                     # not common stock
    assert "AAPL" in syms                         # standing list present
    assert len(syms) > 1000, "the standing list must not be dropped"
    assert stats["dropped_excluded"] >= 1
    assert stats["dropped_not_common"] == 1


def test_universe_below_the_expected_floor_is_recorded_as_degraded(monkeypatch, con):
    """A collapsed universe is an outage, not a market condition.

    This is the guard against repeating the original bug silently: if the
    population falls far below the standing list, the session is flagged so a
    thin candidate list can never be read as a genuine stand-down.
    """
    class _MD:
        alpaca = None

    monkeypatch.setattr(runner_universe, "liquid_universe", lambda *a, **k: ([], {}))
    monkeypatch.setattr(runner_universe, "DEFAULT_UNIVERSE", ("AAA", "BBB"))
    at = datetime(2026, 9, 11, 11, 30, tzinfo=ET)
    syms, stats = runner_universe.build_universe(_MD(), _cfg().get("runner"),
                                                 at=at, con=con)
    assert len(syms) < runner_universe.EXPECTED_MIN_UNIVERSE
    assert stats["provider_degraded"] is True


def test_universe_cap_must_sit_above_the_real_population(con, monkeypatch):
    """The cap bounds a runaway screener; it must never trim the real universe.

    Measured: a 1,500 cap against a 4,224-symbol population silently cut the
    scan and took candidates from 11 to 3.
    """
    monkeypatch.setattr(runner_universe, "liquid_universe",
                        lambda *a, **k: ([f"L{i:04d}" for i in range(4000)], {}))
    monkeypatch.setattr(runner_universe, "is_common_stock_symbol", lambda s: True)

    class _MD:
        alpaca = None

    at = datetime(2026, 9, 11, 11, 30, tzinfo=ET)
    cfg = dict(_cfg().get("runner"))
    cfg["universe_size"] = 6000
    syms, _ = runner_universe.build_universe(_MD(), cfg, at=at, con=con)
    assert len(syms) > 4000, "a cap above the population must not truncate"


def test_universe_keeps_today_s_candidates_even_if_they_fall_off_movers(con):
    """Sticky candidates: a name that ran at 10:05 and cooled by 10:30 must
    stay observable for the rest of the session."""
    ledger.save_runner_candidate(con, _candidate("STIK"))

    class _Alp:
        def movers(self, top=50):
            return {"gainers": [{"symbol": "GOOD"}], "losers": []}
        def most_actives(self, top=50):
            return {"most_actives": []}
        def snapshots(self, syms):
            return {s: {"latestTrade": {"p": 10.0}} for s in syms}

    class _MD:
        alpaca = _Alp()

    at = datetime(2026, 9, 11, 11, 30, tzinfo=ET)
    syms, stats = runner_universe.build_universe(_MD(), _cfg().get("runner"),
                                                 at=at, con=con)
    assert "STIK" in syms
    assert stats["sticky_added"] == 1


def test_empty_universe_is_recorded_as_degraded_never_silent(con, monkeypatch):
    class _MD:
        alpaca = None

    monkeypatch.setattr(runner_universe, "build_universe",
                        lambda *a, **k: ([], {"provider_degraded": True}))
    at = datetime(2026, 9, 11, 11, 30, tzinfo=ET)
    out = runner_scan.scan(_MD(), _cfg().get("runner"), at=at, con=con)
    assert out["provider_degraded"] is True
    assert ledger.runner_session(con, SESSION)["provider_degraded"] is True


# ====================================================================
# PHASE 2 — selection
# ====================================================================

class _StubLLM:
    """Records calls so "exactly one LLM call per session" is testable."""
    calls: list = []

    def __init__(self, cfg):
        self.cfg = cfg

    def call(self, **kw):
        _StubLLM.calls.append(kw)
        from agent.llm import LLMResult
        return LLMResult(text=json.dumps({"picks": [
            {"ticker": "AAAA", "rank": 1, "conviction": 4, "rationale": "widest stop."},
            {"ticker": "CCCC", "rank": 2, "conviction": 2, "rationale": "early detection."},
        ]}), usage={"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001})


@pytest.fixture()
def stub_llm(monkeypatch):
    _StubLLM.calls = []
    monkeypatch.setattr(runner_selection, "LLMClient", _StubLLM)
    return _StubLLM


def _seed(con, n: int):
    for i in range(n):
        ledger.save_runner_candidate(con, _candidate(chr(65 + i) * 4, price=10.0 + i))


def test_candidate_table_is_alphabetical_and_has_no_ranking_column(con):
    _seed(con, 4)
    table = runner_selection.build_candidate_table(
        ledger.runner_candidates_for_date(con, SESSION))
    lines = table.splitlines()
    assert lines[0].split(" | ") == runner_selection.TABLE_COLUMNS
    assert [ln.split(" | ")[0] for ln in lines[1:]] == ["AAAA", "BBBB", "CCCC", "DDDD"]
    # Nothing that pre-ranks the field for the selector.
    for banned in ("score", "rank", "runner_score"):
        assert banned not in lines[0].lower()


def test_selection_writes_picks_and_stamps_outcomes(con, stub_llm):
    _seed(con, 5)   # 5 candidates, max_picks 2 -> a real choice
    out = runner_selection.run_selection(_cfg(), con=con, session_date=SESSION)

    assert out["selected"] == ["AAAA", "CCCC"]
    assert len(stub_llm.calls) == 1

    picks = con.execute(
        "SELECT pick_id, ticker, setup_type, direction, entry_type, size_r, conviction "
        "FROM picks WHERE plan_date = ? ORDER BY ticker", [SESSION]).fetchall()
    assert [p[1] for p in picks] == ["AAAA", "CCCC"]
    assert all(p[2] == "dto_runner" for p in picks)   # own bucket for the integrity layer
    assert all(p[3] == "long" and p[4] == "open" for p in picks)

    # Every selected pick has a matching outcome row carrying its pick_id.
    outcomes = {o["ticker"]: o for o in ledger.runner_outcomes_for_date(con, SESSION)}
    for pick_id, ticker, *_ in picks:
        assert outcomes[ticker]["selected"] is True
        assert outcomes[ticker]["pick_id"] == pick_id
    assert outcomes["AAAA"]["selection_rank"] == 1
    assert outcomes["CCCC"]["selection_rank"] == 2


def test_conviction_is_recorded_but_never_scales_size(con, stub_llm):
    """Spec §2.5: size scales only on validated forward evidence from this
    desk's own ledger, never on the selector's self-reported confidence."""
    _seed(con, 5)
    runner_selection.run_selection(_cfg(), con=con, session_date=SESSION)
    rows = con.execute(
        "SELECT conviction, size_r FROM picks WHERE plan_date = ? ORDER BY ticker",
        [SESSION]).fetchall()
    convictions = {r[0] for r in rows}
    sizes = {r[1] for r in rows}
    assert convictions == {4, 2}      # recorded, and they differ
    assert sizes == {0.25}            # yet size is identical


def test_no_llm_call_when_candidates_fit_in_max_picks(con, stub_llm):
    """There is no selection problem to solve; do not pay for one."""
    _seed(con, 2)   # == max_picks
    out = runner_selection.run_selection(_cfg(), con=con, session_date=SESSION)

    assert len(stub_llm.calls) == 0
    assert out["selection_skipped_reason"] == "candidates <= max_picks"
    assert sorted(out["selected"]) == ["AAAA", "BBBB"]
    # All of them are still written as picks and stamped.
    assert con.execute("SELECT COUNT(*) FROM picks WHERE plan_date = ?",
                       [SESSION]).fetchone()[0] == 2


def test_zero_candidates_records_reason_and_calls_nothing(con, stub_llm):
    out = runner_selection.run_selection(_cfg(), con=con, session_date=SESSION)
    assert len(stub_llm.calls) == 0
    assert out["selection_skipped_reason"] == "no candidates"
    assert ledger.runner_session(con, SESSION)["selection_skipped_reason"] == "no candidates"


def test_selection_is_idempotent_under_forced_double_invocation(con, stub_llm):
    """The intraday cron fires every five minutes and can retry or overlap.
    A second call must not pay for a second LLM call or write competing picks."""
    _seed(con, 5)
    first = runner_selection.run_selection(_cfg(), con=con, session_date=SESSION)
    second = runner_selection.run_selection(_cfg(), con=con, session_date=SESSION)

    assert first["selected"] == ["AAAA", "CCCC"]
    assert second["skipped"] == "selection already made for this session"
    assert len(stub_llm.calls) == 1
    assert con.execute("SELECT COUNT(*) FROM picks WHERE plan_date = ?",
                       [SESSION]).fetchone()[0] == 2


def test_selection_failure_is_a_no_trade_session_with_no_orders(con, monkeypatch):
    """Never fall back to a heuristic: a silent heuristic would contaminate the
    baseline-vs-selection comparison this design exists to produce."""
    class _Boom:
        def __init__(self, cfg): pass
        def call(self, **kw): raise RuntimeError("api exploded")

    monkeypatch.setattr(runner_selection, "LLMClient", _Boom)
    _seed(con, 5)
    out = runner_selection.run_selection(_cfg(), con=con, session_date=SESSION)

    assert out["selection_skipped_reason"].startswith("selection_failed:")
    assert con.execute("SELECT COUNT(*) FROM picks WHERE plan_date = ?",
                       [SESSION]).fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM live_orders").fetchone()[0] == 0


def test_unparseable_response_is_a_no_trade_session(con, monkeypatch):
    class _Garbage:
        def __init__(self, cfg): pass
        def call(self, **kw):
            from agent.llm import LLMResult
            return LLMResult(text="I think you should buy AAAA!", usage={})

    monkeypatch.setattr(runner_selection, "LLMClient", _Garbage)
    _seed(con, 5)
    out = runner_selection.run_selection(_cfg(), con=con, session_date=SESSION)
    assert out["selection_skipped_reason"].startswith("selection_failed:")
    assert con.execute("SELECT COUNT(*) FROM picks WHERE plan_date = ?",
                       [SESSION]).fetchone()[0] == 0


def test_hallucinated_tickers_are_dropped_and_counted(con, monkeypatch):
    class _Halluc:
        def __init__(self, cfg): pass
        def call(self, **kw):
            from agent.llm import LLMResult
            return LLMResult(text=json.dumps({"picks": [
                {"ticker": "AAAA", "rank": 1, "conviction": 3, "rationale": "real"},
                {"ticker": "NVDA", "rank": 2, "conviction": 5, "rationale": "invented"},
            ]}), usage={})

    monkeypatch.setattr(runner_selection, "LLMClient", _Halluc)
    _seed(con, 5)
    out = runner_selection.run_selection(_cfg(), con=con, session_date=SESSION)

    assert out["selected"] == ["AAAA"]
    assert out["hallucinated"] == 1
    tickers = [r[0] for r in con.execute(
        "SELECT ticker FROM picks WHERE plan_date = ?", [SESSION]).fetchall()]
    assert "NVDA" not in tickers


@pytest.mark.parametrize("text", [
    '```json\n{"picks": [{"ticker": "AAAA", "rank": 1, "conviction": 3, "rationale": "x"}]}\n```',
    'Here you go:\n{"picks": [{"ticker": "AAAA", "rank": 1, "conviction": 3, "rationale": "x"}]}',
    '{"picks": [{"ticker": "aaaa", "rank": 1, "conviction": 3, "rationale": "x"}]}',
])
def test_parse_selection_is_defensive(text):
    picks, halluc = runner_selection.parse_selection(text, {"AAAA"})
    assert [p["ticker"] for p in picks] == ["AAAA"]
    assert halluc == []


def test_parse_selection_reranks_densely():
    """A skipped or duplicated rank must not leave holes in selection_rank."""
    text = json.dumps({"picks": [
        {"ticker": "CCCC", "rank": 7, "conviction": 1, "rationale": "x"},
        {"ticker": "AAAA", "rank": 2, "conviction": 1, "rationale": "x"},
        {"ticker": "AAAA", "rank": 3, "conviction": 1, "rationale": "dupe"},
    ]})
    picks, _ = runner_selection.parse_selection(text, {"AAAA", "CCCC"})
    assert [(p["ticker"], p["rank"]) for p in picks] == [("AAAA", 1), ("CCCC", 2)]


def test_empty_pick_list_is_a_legitimate_answer(con, monkeypatch):
    class _None:
        def __init__(self, cfg): pass
        def call(self, **kw):
            from agent.llm import LLMResult
            return LLMResult(text='{"picks": []}', usage={})

    monkeypatch.setattr(runner_selection, "LLMClient", _None)
    _seed(con, 5)
    out = runner_selection.run_selection(_cfg(), con=con, session_date=SESSION)
    assert out["selection_skipped_reason"] == "selector returned no picks"
    assert con.execute("SELECT COUNT(*) FROM picks WHERE plan_date = ?",
                       [SESSION]).fetchone()[0] == 0


def test_no_orders_placed_when_live_orders_disabled(con, stub_llm):
    _seed(con, 5)
    out = runner_selection.run_selection(_cfg(live_orders_enabled=False),
                                         con=con, session_date=SESSION)
    assert out["orders"]["placed"] == 0
    assert "live_paper.enabled is false" in out["orders"]["skipped"]


def test_runner_picks_do_not_disturb_the_morning_plan(con, stub_llm):
    """save_daily_plan deletes every pick for the date before inserting.
    Runner picks must be APPENDED, never routed through it."""
    con.execute(
        """INSERT INTO picks (plan_date, ticker, direction, setup_type, entry_type,
                              entry_price, stop_price, target_price)
           VALUES (?,?,?,?,?,?,?,?)""",
        [SESSION, "MORNING", "long", "gap_go", "open", 10.0, 9.0, 12.0])
    _seed(con, 5)
    runner_selection.run_selection(_cfg(), con=con, session_date=SESSION)

    tickers = [r[0] for r in con.execute(
        "SELECT ticker FROM picks WHERE plan_date = ? ORDER BY ticker",
        [SESSION]).fetchall()]
    assert "MORNING" in tickers
    assert {"AAAA", "CCCC"} <= set(tickers)


def test_selection_logs_cost_to_runs(con, stub_llm):
    _seed(con, 5)
    runner_selection.run_selection(_cfg(), con=con, session_date=SESSION)
    row = con.execute(
        "SELECT loop, model, input_tokens, output_tokens, cost_usd FROM runs "
        "WHERE loop = 'runner'").fetchone()
    assert row is not None
    assert row[1] == "test-model"
    assert row[4] == pytest.approx(0.001)


# ====================================================================
# tick() — window and calendar guards
# ====================================================================

@pytest.mark.parametrize("clock, expect", [
    ((9, 30), "before scan_start_et"),
    ((15, 30), "after scan_end_et"),
])
def test_tick_enforces_its_own_window(clock, expect):
    """The cron schedule is not a correctness mechanism — it can fire early,
    late, or twice, so tick() re-checks rather than trusting when it was called."""
    from agent.loops import runner as runner_loop
    at = datetime(2026, 9, 11, clock[0], clock[1], tzinfo=ET)
    out = runner_loop.tick(_cfg(), at=at, md=object())
    assert expect in out["skipped"]


def test_tick_skips_non_market_days():
    from agent.loops import runner as runner_loop
    at = datetime(2026, 9, 12, 11, 30, tzinfo=ET)   # Saturday
    out = runner_loop.tick(_cfg(), at=at, md=object())
    assert "not a market day" in out["skipped"]


def test_tick_is_a_noop_when_disabled():
    from agent.loops import runner as runner_loop
    at = datetime(2026, 9, 11, 11, 30, tzinfo=ET)
    out = runner_loop.tick(_cfg(enabled=False), at=at, md=object())
    assert out["skipped"] == "runner.enabled is false"


def test_tick_survives_a_malformed_clock_in_config():
    """A config typo must not take down the intraday cron."""
    from agent.loops import runner as runner_loop
    at = datetime(2026, 9, 11, 9, 0, tzinfo=ET)
    out = runner_loop.tick(_cfg(scan_start_et="not-a-time"), at=at, md=object())
    assert "before scan_start_et" in out["skipped"]


# ====================================================================
# Minimum stop distance — geometry sanity, not an edge claim
# ====================================================================

def _bars_on_the_ema(n=24, price=10.19):
    """Bars engineered so price finishes essentially ON the 9 EMA: a long
    flat run with a tiny final tick up. Passes the conditions, but the stop
    distance collapses toward zero."""
    from datetime import datetime as _dt
    out = []
    for i in range(n):
        c = price + (0.004 if i == n - 1 else 0.0) + i * 0.0004
        out.append({"t": _dt(2026, 9, 11, 9, 30) + timedelta(minutes=5 * i),
                    "o": c - 0.0002, "h": c + 0.0005, "l": c - 0.0005,
                    "c": c, "v": 200_000})
    return out


def test_min_stop_guard_rejects_a_collapsed_stop(con, monkeypatch):
    """Measured live on 2026-09-11: ATEC detected with a $0.0069 stop on a
    $10.19 stock and the simulator graded it -5.15R. That R is arithmetically
    valid and describes noise, not the setup — rows like it would corrupt the
    very Phase 1 baseline Phase 1 exists to produce."""
    captured = {}

    def _fake_evaluate(ticker, bars, **kw):
        captured["min_tc"] = kw.get("min_trend_consistency")
        return {"passes": True, "counter": None, "reason": "ok",
                "check": runner_scan.check_runner_conditions(bars),
                "rvol": 2.0, "trend_consistency": 60.0, "unavailable": []}

    bars = _bars_on_the_ema()
    monkeypatch.setattr(runner_scan, "evaluate_ticker", _fake_evaluate)
    monkeypatch.setattr(runner_universe, "build_universe",
                        lambda *a, **k: (["ATEC"], {"provider_degraded": False}))

    class _Alp:
        def multi_bars(self, syms, tf, start, end):
            return {"ATEC": bars}
        def multi_daily_bars(self, syms, a, b):
            return {"ATEC": [{"close": 10.0, "volume": 5_000_000}]}

    class _MD:
        alpaca = _Alp()
        def fundamentals(self, syms, con=None):
            return {"ATEC": {"float_shares": 40e6}}
        def next_earnings(self, syms):
            return {"ATEC": 30}

    at = datetime(2026, 9, 11, 13, 0, tzinfo=ET)
    rcfg = dict(_cfg().get("runner"))
    rcfg["min_stop_abs"] = 0.10
    rcfg["min_stop_pct"] = 0.005
    out = runner_scan.scan(_MD(), rcfg, at=at, con=con)

    assert out["candidates"] == 0, "a sub-minimum stop must never be stored"
    assert out["rejections"]["rejected_conditions"] >= 1
    assert ledger.runner_candidate_tickers(con, SESSION) == []


def test_min_stop_guard_allows_a_normal_stop(con, monkeypatch):
    """The guard must not reject ordinary geometry — PMTS on the same live day
    had a $0.37 stop on a $22.59 stock and is a legitimate candidate."""
    price, stop_dist = 22.59, 0.37
    check = {"passes": True, "reason": "ok", "price": price,
             "vwap": price * 0.98, "vwap_pct_above": 1.99,
             "ema9": price - stop_dist, "ema9_slope": 0.03}

    monkeypatch.setattr(runner_scan, "evaluate_ticker",
                        lambda t, b, **kw: {"passes": True, "counter": None,
                                            "reason": "ok", "check": check,
                                            "rvol": 10.68, "trend_consistency": 71.4,
                                            "unavailable": []})
    monkeypatch.setattr(runner_scan, "check_runner_conditions", lambda *a, **k: check)
    monkeypatch.setattr(runner_scan, "first_pass_index", lambda *a, **k: 0)
    monkeypatch.setattr(runner_universe, "build_universe",
                        lambda *a, **k: (["PMTS"], {"provider_degraded": False}))

    from datetime import datetime as _dt
    bars = [{"t": _dt(2026, 9, 11, 10, 35), "o": price, "h": price, "l": price,
             "c": price, "v": 200_000}] * 12

    class _Alp:
        def multi_bars(self, syms, tf, start, end):
            return {"PMTS": bars}
        def multi_daily_bars(self, syms, a, b):
            return {"PMTS": [{"close": 21.0, "volume": 5_000_000}]}

    class _MD:
        alpaca = _Alp()
        def fundamentals(self, syms, con=None):
            return {"PMTS": {"float_shares": 40e6}}
        def next_earnings(self, syms):
            return {"PMTS": 30}

    at = datetime(2026, 9, 11, 11, 30, tzinfo=ET)
    rcfg = dict(_cfg().get("runner"))
    rcfg["min_stop_abs"] = 0.10
    rcfg["min_stop_pct"] = 0.005
    out = runner_scan.scan(_MD(), rcfg, at=at, con=con)

    assert out["candidates"] == 1
    row = ledger.runner_candidates_for_date(con, SESSION)[0]
    assert row["ticker"] == "PMTS"
    assert row["stop_dist"] == pytest.approx(stop_dist)
    # target = detection_price + 2 * stop_dist
    assert row["target_price"] == pytest.approx(price + 2 * stop_dist)
