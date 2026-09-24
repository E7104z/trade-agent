"""
Morning pipeline repair (2026-09-09) — pins the fixes for the 2026-09-08
no-trade day, where five candidates (two at conviction 4-5) died annotated
"excellent catalyst, untradeable structure".

Covered here:
  A1  candidate grounding runs BEFORE the risk manager, not after it
  B   data_health / degraded — a provider outage and a genuine stand-down
      stop being the same stored fact
  C   log_run's started_at and finished_at share one clock
  D   feed preflight, and a RECORDED downgrade instead of a lost session
  E   the exploration lane's blocklist is unchanged, but now says when it fires

Nothing here asserts a loosened gate: every fail-closed drop still drops.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from agent import ledger
from agent.config import Config
from agent.loops import morning as morning_mod
from agent.loops.morning import (_add_exploration_pick, _build_rejections,
                                 _bundle_health, _candidate_grounding_health,
                                 _data_health, _grounded_candidate_prices,
                                 _grounded_ref_prices, _live_quotes_md,
                                 _validate_plan)
from agent.providers import MarketData
from agent.util import now_et

D = date(2026, 9, 8)


def _cfg(**overrides) -> Config:
    raw = {
        "max_picks": 4, "min_size_r": 0.25, "max_size_r": 1.5,
        "max_total_open_risk_r": 3.0, "drawdown_pause_r": -4.0,
    }
    raw.update(overrides)
    return Config(raw=raw)


def _pick(ticker="AAAA", entry=100.0, stop=98.0, target=104.0):
    return {
        "ticker": ticker, "direction": "long",
        "entry_plan": {"type": "open", "price": entry},
        "entry_price": entry, "stop_price": stop, "target_price": target,
        "size_r": 0.5, "conviction": 4,
    }


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "test.duckdb")
    c = ledger.connect()
    yield c
    c.close()


class _StubMD:
    """Minimal stand-in for MarketData.pick_quotes."""

    def __init__(self, quotes=None, boom=False):
        self.quotes = quotes or {}
        self.boom = boom
        self.asked: list[str] | None = None

    def pick_quotes(self, symbols, d):
        self.asked = list(symbols)
        if self.boom:
            raise RuntimeError("403 entitlement")
        return {s: self.quotes[s] for s in symbols if s in self.quotes}


# ------------------------------------------------- A1: grounding before stage 2

def test_candidate_grounding_fetches_only_tickers_the_bundle_missed():
    # The 2026-09-08 shape: two web-search-sourced biotech catalysts that no
    # feed tagged, alongside one name the gap scan already quoted.
    bundle = {"premarket_gappers": [{"symbol": "MARA", "last": 11.75}]}
    candidates = {"candidates": [{"ticker": "PHVS"}, {"ticker": "glue"},
                                 {"ticker": "MARA"}, {"ticker": "PHVS"}]}
    md = _StubMD({"PHVS": 12.34, "GLUE": 45.60})
    quotes = _grounded_candidate_prices(md, candidates, bundle, D)
    assert md.asked == ["PHVS", "GLUE"]        # deduped, normalized, MARA excluded
    assert quotes == {"PHVS": 12.34, "GLUE": 45.60}


def test_candidate_grounding_skips_the_fetch_when_bundle_covers_everything():
    bundle = {"premarket_gappers": [{"symbol": "MARA", "last": 11.75}]}
    md = _StubMD({"MARA": 99.0})
    assert _grounded_candidate_prices(md, {"candidates": [{"ticker": "MARA"}]},
                                      bundle, D) == {}
    assert md.asked is None


def test_candidate_grounding_fails_soft_and_leaves_tickers_ungrounded():
    # A fetch failure must not raise into the morning loop, and must NOT be
    # papered over: the tickers stay absent, so they still fail closed later.
    md = _StubMD(boom=True)
    assert _grounded_candidate_prices(md, {"candidates": [{"ticker": "PHVS"}]}, {}, D) == {}


def test_live_quotes_block_grounds_only_the_named_tickers():
    md = _live_quotes_md({"PHVS": 12.34, "GLUE": 45.6})
    assert "- PHVS: $12.34" in md and "- GLUE: $45.6" in md
    # Must not read as a blanket pass on kill-check 2c.
    assert "NOT listed here and not in MARKET DATA remains ungrounded" in md
    assert _live_quotes_md({}) == ""


def test_seeded_ref_prices_are_not_refetched_but_still_gated():
    # A1's quotes flow into validation as real references (no second snapshot
    # call for the same ticker) -- and the magnitude gate still fires on them.
    md = _StubMD({"ZZZZ": 500.0})
    plan = {"picks": [_pick("PHVS", entry=12.40, stop=11.90, target=13.90)]}
    refs = _grounded_ref_prices(md, plan, {}, D, seed={"PHVS": 12.34})
    assert refs == {"PHVS": 12.34}
    assert md.asked is None

    out, warnings = _validate_plan({"no_trade": False, "picks": [
        _pick("PHVS", entry=99.0, stop=95.0, target=110.0)]}, _cfg(), ref_prices=refs)
    assert out["picks"] == []
    assert any("PHVS" in w and "ungrounded" in w for w in warnings)


# ------------------------------------------------------- B: data health verdict

def _health(**session):
    return _data_health({"atr": 0, "earnings": 0, "grounding": 0},
                        session.pop("n_proposed", 1), session)


def test_empty_gap_scan_is_an_outage_not_a_flat_tape():
    # _gappers() folds in INDEX_ETFS unconditionally, so a healthy scan cannot
    # come back empty.
    h = _health(bundle_gappers=0, bundle_catalyst_quotes=3, candidates_seen=4,
                candidates_ungrounded=0)
    assert h["degraded"] is True
    assert any("gap scan" in r for r in h["degraded_reasons"])


def test_catalyst_quote_failure_is_degraded():
    h = _health(bundle_gappers=12, catalyst_quotes_failed=True,
                candidates_seen=4, candidates_ungrounded=0)
    assert h["degraded"] is True


def test_half_the_candidates_unpriceable_is_degraded():
    h = _health(bundle_gappers=12, candidates_seen=4, candidates_ungrounded=2)
    assert h["degraded"] is True
    assert h["ungrounded_basis"] == "candidates"


def test_one_unpriceable_candidate_is_not_degraded():
    # Floor of 2 — a single unquotable ticker is ordinary, not an outage.
    h = _health(bundle_gappers=12, candidates_seen=4, candidates_ungrounded=1)
    assert h["degraded"] is False


def test_healthy_session_is_not_degraded():
    h = _health(bundle_gappers=12, bundle_catalyst_quotes=3, candidates_seen=5,
                candidates_ungrounded=0, catalyst_quotes_failed=False)
    assert h["degraded"] is False and h["degraded_reasons"] == []


def test_feed_downgrade_is_recorded_but_is_not_itself_degraded():
    # An IEX session still evaluated the market. It must be segregable later,
    # which is what the flag is for -- but it is not an outage.
    h = _health(bundle_gappers=12, candidates_seen=4, candidates_ungrounded=0,
                feed_downgraded=True)
    assert h["feed_downgraded"] is True
    assert h["degraded"] is False


def test_ungrounded_ratio_falls_back_to_picks_when_candidates_unknown():
    # Populations are never mixed: with no candidate census, the ratio is
    # measured over proposed picks instead.
    h = _data_health({"atr": 0, "earnings": 0, "grounding": 3}, 4,
                     {"bundle_gappers": 12})
    assert h["ungrounded_basis"] == "picks"
    assert h["degraded"] is True


def test_no_trade_plan_still_gets_a_data_health_verdict():
    # The whole point: a no-trade day is exactly where the distinction matters.
    plan, _ = _validate_plan({"no_trade": True, "picks": []}, _cfg(),
                             session_health={"bundle_gappers": 0})
    assert plan["data_health"]["degraded"] is True


def test_bundle_health_reads_coverage_and_catalyst_failure():
    bundle = {
        "premarket_gappers": [{"symbol": "MARA", "last": 1.0}],
        "catalyst_quotes": [{"symbol": "INSM", "last": 125.4}],
        "missing": ["catalyst_quotes: HTTPError: 403 for /v2/stocks/snapshots"],
    }
    h = _bundle_health(SimpleNamespace(feed_downgraded=True), bundle)
    assert h == {"bundle_gappers": 1, "bundle_catalyst_quotes": 1,
                 "catalyst_quotes_failed": True, "feed_downgraded": True,
                 "provider_auth_error": None}


def test_candidate_grounding_health_counts_over_the_analyst_population():
    bundle = {"premarket_gappers": [{"symbol": "MARA", "last": 11.75}]}
    candidates = {"candidates": [{"ticker": "MARA"}, {"ticker": "PHVS"},
                                 {"ticker": "GLUE"}]}
    assert _candidate_grounding_health(candidates, bundle, {"PHVS": 12.34}) == {
        "candidates_seen": 3, "candidates_ungrounded": 1}


def test_data_health_round_trips_through_daily_plans(con):
    plan, _ = _validate_plan({"no_trade": True, "picks": []}, _cfg(),
                             session_health={"bundle_gappers": 0, "candidates_seen": 5,
                                             "candidates_ungrounded": 5})
    ledger.save_daily_plan(con, D, plan, {"candidates": []}, "chop")
    row = con.execute(
        "SELECT data_health, session_degraded FROM daily_plans WHERE plan_date = ?", [D]
    ).fetchone()
    assert json.loads(row[0])["degraded"] is True
    assert row[1] is True


def test_clean_session_round_trips_session_degraded_false(con):
    plan, _ = _validate_plan({"no_trade": False, "picks": [_pick("AAAA")]}, _cfg(),
                             ref_prices={"AAAA": 100.0}, atr_map={"AAAA": 1.0},
                             earnings_map={"AAAA": 10},
                             session_health={"bundle_gappers": 12, "candidates_seen": 1,
                                             "candidates_ungrounded": 0})
    ledger.save_daily_plan(con, D, plan, {"candidates": []}, "chop")
    assert con.execute(
        "SELECT session_degraded FROM daily_plans WHERE plan_date = ?", [D]).fetchone()[0] is False


# ------------------------------------------- B3: degraded rejections are flagged

def test_rejections_carry_the_sessions_degraded_flag():
    plan = {"picks": [], "rejected": [{"ticker": "PHVS", "reason": "no live quote"}],
            "data_health": {"degraded": True}}
    candidates = {"candidates": [{"ticker": "PHVS", "entry_price": 12.4,
                                  "stop_price": 11.9, "target_price": 13.9}]}
    assert _build_rejections(candidates, plan)[0]["session_degraded"] is True


def test_rejections_from_a_clean_session_are_not_flagged():
    plan = {"picks": [], "rejected": [{"ticker": "PHVS", "reason": "R:R 1.4 < 2.0"}],
            "data_health": {"degraded": False}}
    candidates = {"candidates": [{"ticker": "PHVS", "entry_price": 12.4,
                                  "stop_price": 11.9, "target_price": 13.9}]}
    assert _build_rejections(candidates, plan)[0]["session_degraded"] is False


def test_degraded_rejections_are_stored_and_graded_but_not_counted_as_reasons(con):
    # Stored: yes. Graded: yes. Counted as a REASON: no — the reason is a fact
    # about the plumbing, not about the desk's judgment.
    clean, dirty = date(2026, 9, 4), date(2026, 9, 8)
    ledger.save_rejections(con, clean, [{
        "ticker": "AAAA", "reason": "R:R 1.4 < 2.0", "entry_price": 10.0,
        "stop_price": 9.0, "target_price": 11.4, "session_degraded": False}])
    ledger.save_rejections(con, dirty, [{
        "ticker": "PHVS", "reason": "no live quote", "entry_price": 12.4,
        "stop_price": 11.9, "target_price": 13.9, "session_degraded": True}])
    for (rid,) in con.execute("SELECT rejection_id FROM rejections").fetchall():
        ledger.save_rejection_outcome(con, rid, "would_have_won", 1.0)

    flags = dict(con.execute(
        "SELECT ticker, session_degraded FROM rejections").fetchall())
    assert flags == {"AAAA": False, "PHVS": True}

    summary = ledger.shadow_summary(con)
    assert summary["total"] == 2                      # both still counted overall
    reasons = [r["reason"] for r in summary["by_reason"]]
    assert reasons == ["R:R 1.4 < 2.0"]               # only the judgment call


# --------------------------------------------------------------- C: one clock

def test_run_duration_is_minutes_not_timezone_offset(con):
    # started_at was naive ET while finished_at came from Postgres'
    # current_timestamp (UTC), inflating every duration by the UTC offset —
    # a ~4 minute run read as 4h04m, making `runs` useless for the one
    # question it exists to answer.
    started = now_et().replace(tzinfo=None)
    ledger.log_run(con, "morning", D, started, "ok")
    row = con.execute(
        "SELECT started_at, finished_at FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    elapsed = row[1] - row[0]
    assert timedelta(0) <= elapsed < timedelta(minutes=1)


def test_degraded_is_a_first_class_run_status(con):
    ledger.log_run(con, "morning", D, now_et().replace(tzinfo=None), "degraded")
    assert con.execute(
        "SELECT status FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()[0] == "degraded"


# --------------------------------------------------------- D: feed entitlement

class _StubAlpaca:
    def __init__(self, works_on=("sip",), feed="sip"):
        self.feed = feed
        self.works_on = works_on
        self.tried: list[str] = []

    def snapshots(self, symbols):
        self.tried.append(self.feed)
        if self.feed not in self.works_on:
            raise RuntimeError(f"403 forbidden for feed {self.feed}")
        return {s: {"latestTrade": {"p": 500.0}} for s in symbols}


def _md(alpaca):
    md = MarketData.__new__(MarketData)
    md.cfg = SimpleNamespace(offline=False)
    md.alpaca = alpaca
    md.feed_ok = None
    md.feed_downgraded = False
    return md


def test_healthy_sip_feed_needs_no_downgrade():
    md = _md(_StubAlpaca(works_on=("sip",)))
    md._ensure_feed()
    assert md.feed_ok is True and md.feed_downgraded is False
    assert md.alpaca.feed == "sip" and md.alpaca.tried == ["sip"]


def test_unentitled_sip_falls_back_to_iex_and_records_it():
    # Degraded data beats no data — but only because the downgrade is recorded.
    md = _md(_StubAlpaca(works_on=("iex",)))
    md._ensure_feed()
    assert md.feed_ok is True
    assert md.feed_downgraded is True
    assert md.alpaca.feed == "iex" and md.alpaca.tried == ["sip", "iex"]


def test_both_feeds_dead_restores_the_configured_feed_and_never_raises():
    md = _md(_StubAlpaca(works_on=()))
    md._ensure_feed()                      # must not raise
    assert md.feed_ok is False
    assert md.feed_downgraded is False
    assert md.alpaca.feed == "sip"         # not left silently pointing at iex


def test_feed_is_probed_once_per_instance():
    md = _md(_StubAlpaca(works_on=("sip",)))
    md._ensure_feed()
    md._ensure_feed()
    assert md.alpaca.tried == ["sip"]


def test_snapshots_unwraps_a_nested_payload_shape():
    from agent.providers import Alpaca
    a = Alpaca.__new__(Alpaca)
    a.feed = "sip"
    a._get = lambda path, params: {"snapshots": {"SPY": {"latestTrade": {"p": 1.0}}}}
    assert a.snapshots(["SPY"]) == {"SPY": {"latestTrade": {"p": 1.0}}}


# ---------------------------------------- E: exploration blocklist diagnostics

_LANE = {"enabled": True, "min_rr": 1.8, "max_rr": 2.0, "max_picks": 1, "size_r": 0.25}


def _explore_candidates():
    return {"candidates": [{
        "ticker": "MARA", "entry_plan": {"type": "limit", "price": 11.50},
        "entry_price": 11.50, "stop_price": 10.90, "target_price": 12.60,
    }]}


def test_compound_reason_still_blocks_and_now_says_so(monkeypatch):
    # A candidate killed on R:R alone is rescuable; the same candidate killed
    # with ", and regime is chop" is blocked by the substring `regime`. The
    # blocklist is NOT loosened — the lane just stops failing silently.
    lines: list[str] = []
    monkeypatch.setattr(morning_mod, "log", lambda tag, msg: lines.append(msg))
    plan = {"no_trade": True, "picks": [], "rejected": [
        {"ticker": "MARA", "reason": "R:R 1.83 < 2.0, and regime is chop"}]}
    assert _add_exploration_pick(_explore_candidates(), plan,
                                 _cfg(exploration_lane=_LANE)) is False
    assert plan["picks"] == []
    assert any("EXPLORATION: MARA in-band (R:R 1.83)" in ln and "'regime'" in ln
               for ln in lines)


def test_clean_rr_reason_still_admits_the_exploration_pick(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(morning_mod, "log", lambda tag, msg: lines.append(msg))
    plan = {"no_trade": True, "picks": [],
            "rejected": [{"ticker": "MARA", "reason": "R:R 1.83 < 2.0"}]}
    assert _add_exploration_pick(_explore_candidates(), plan,
                                 _cfg(exploration_lane=_LANE)) is True
    assert plan["picks"][0]["exploration_lane"] == "rr_below_control"
    assert not any("EXPLORATION:" in ln for ln in lines)


def test_out_of_band_candidate_is_not_reported_as_blocked(monkeypatch):
    # The log line is for in-band near-misses only; an out-of-band R:R is an
    # ordinary miss and must not add noise to the evidence being collected.
    lines: list[str] = []
    monkeypatch.setattr(morning_mod, "log", lambda tag, msg: lines.append(msg))
    candidates = {"candidates": [{
        "ticker": "MARA", "entry_plan": {"type": "limit", "price": 11.50},
        "entry_price": 11.50, "stop_price": 10.90, "target_price": 12.10,  # R:R 1.0
    }]}
    plan = {"no_trade": True, "picks": [], "rejected": [
        {"ticker": "MARA", "reason": "R:R 1.00 < 2.0, and regime is chop"}]}
    assert _add_exploration_pick(candidates, plan, _cfg(exploration_lane=_LANE)) is False
    assert not any("EXPLORATION:" in ln for ln in lines)
