"""
The morning lane's entry window (schema v27).

The analyst prompt has always said "no entries in the first 15 minutes after
the open", and nothing enforced it. On 2026-10-02 (NFP) the plan read "no
entries before 09:45 ET" while every limit order sat at the broker from 07:11
as a day order live from 09:30, and the simulator filled from 09:30 too. The
NKE and WDC short limits were 0.3-0.6% through the premarket price, so both
were set to fill on the opening print -- against "retest after NFP settles"
and "continuation below the premarket low".

Now a limit/breakout pick carries entry_not_before (09:45): the simulator
cannot fill it earlier, the morning run does not send it, and the intraday
tick sends it at the window. These tests pin each piece with the real 10/02
levels.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from agent import grading, ledger, live_broker, live_monitor
from agent.config import Config
from agent.loops import morning

ET = timezone(timedelta(hours=-4))
D = date(2026, 10, 2)

# The 10/02 plan, verbatim levels.
NKE = {"pick_id": 1, "ticker": "NKE", "direction": "short", "entry_type": "limit",
       "entry_price": 31.70, "stop_price": 33.50, "target_price": 28.50}
WDC = {"pick_id": 2, "ticker": "WDC", "direction": "short", "entry_type": "limit",
       "entry_price": 424.0, "stop_price": 445.0, "target_price": 400.0}


def _bars(prices, start=time(9, 30)):
    t0 = datetime.combine(D, start, tzinfo=ET)
    return [{"t": t0 + timedelta(minutes=i), "o": p, "h": p + 0.02, "l": p - 0.02,
             "c": p, "v": 50_000} for i, p in enumerate(prices)]


# ------------------------------------------------------------------ simulator

def test_without_a_window_a_marketable_short_limit_fills_on_the_opening_print():
    """The 10/02 defect, as the simulator used to (and still does for a row
    with no window) price it: NKE opened at 31.80, above its 31.70 limit."""
    sim = grading.simulate_pick(NKE, _bars([31.80] * 30 + [31.40] * 60), None)
    assert sim["triggered"] and sim["fill_time"].strftime("%H:%M") == "09:30"


def test_the_window_holds_the_fill_until_0945():
    sim = grading.simulate_pick({**NKE, "entry_not_before": "09:45"},
                                _bars([31.80] * 30 + [31.40] * 60), None)
    # 09:45 is bar 15; it opens at 31.80 >= 31.70, so the sell limit fills there.
    assert sim["triggered"] and sim["fill_time"].strftime("%H:%M") == "09:45"
    assert sim["entry_fill"] == pytest.approx(31.80)


def test_a_retest_that_only_happened_before_the_window_never_fills():
    # Price touched 31.70 at 09:31, then fell away and never came back.
    prices = [31.50, 31.75] + [31.20] * 13 + [31.00] * 60
    sim = grading.simulate_pick({**NKE, "entry_not_before": "09:45"}, _bars(prices), None)
    assert not sim["triggered"]
    # Measured over the bars the order was live for, not the forbidden first 15 minutes.
    assert sim["untriggered_reason"] in ("no_setup_offered", "entry_condition_not_met")
    assert sim["trigger_gap_r"] < 0


def test_an_open_entry_ignores_the_window():
    pick = {**NKE, "entry_type": "open", "entry_not_before": "09:45"}
    sim = grading.simulate_pick(pick, _bars([31.80] * 30), None)
    assert sim["triggered"] and sim["fill_time"].strftime("%H:%M") == "09:30"


def test_a_window_after_the_last_bar_is_untriggered_not_an_error():
    sim = grading.simulate_pick({**NKE, "entry_not_before": "15:59"},
                                _bars([31.80] * 30), None)
    assert not sim["triggered"] and sim["untriggered_reason"] == "other"


@pytest.mark.parametrize("kind,expected", [
    ("limit", "09:45"), ("breakout", "09:45"), ("LIMIT", "09:45"),
    ("open", None), (None, None), ("limit", None),
])
def test_entry_not_before_for(kind, expected):
    window = None if (kind == "limit" and expected is None) else "09:45"
    assert grading.entry_not_before_for(kind, window) == expected


# --------------------------------------------------------- the morning stamp

def test_build_rejections_stamps_the_same_window_a_taken_pick_would_carry():
    candidates = {"candidates": [
        {"ticker": "WDC", "direction": "short", "setup_type": "sympathy",
         "entry_plan": {"type": "limit", "price": 424.0}, "stop_price": 445.0,
         "target_price": 400.0},
        {"ticker": "GAP", "direction": "long", "setup_type": "gap_go",
         "entry_plan": {"type": "open", "price": 20.0}, "stop_price": 19.0,
         "target_price": 22.0},
    ]}
    rows = {r["ticker"]: r for r in morning._build_rejections(candidates, {"picks": []}, "09:45")}
    assert rows["WDC"]["entry_not_before"] == "09:45"
    assert rows["GAP"]["entry_not_before"] is None


def test_validate_plan_stamps_every_limit_and_breakout_pick():
    plan = {"picks": [
        {"ticker": "MU", "direction": "long", "setup_type": "catalyst_momentum",
         "conviction": 3, "size_r": 0.25, "entry_plan": {"type": "limit", "price": 1090.0},
         "entry_price": 1090.0, "stop_price": 1060.0, "target_price": 1150.0,
         "time_stop": "by 15:00"},
        {"ticker": "GAP", "direction": "long", "setup_type": "gap_go",
         "conviction": 3, "size_r": 0.25, "entry_plan": {"type": "open", "price": 20.0},
         "entry_price": 20.0, "stop_price": 19.0, "target_price": 22.0,
         "time_stop": "by 15:00"},
    ]}
    cfg = Config(raw={"max_picks": 6, "max_total_risk_r": 3.0, "min_size_r": 0.25,
                      "max_size_r": 1.0, "risk_manager": {"min_rr": 1.5}})
    out, _warnings = morning._validate_plan(plan, cfg, ref_prices={"MU": 1097.6, "GAP": 20.0})
    by = {p["ticker"]: p for p in out["picks"]}
    assert by["MU"]["entry_not_before"] == "09:45"
    assert by["GAP"]["entry_not_before"] is None


def test_the_window_survives_into_the_picks_table(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "w.duckdb")
    con = ledger.connect()
    ids = ledger.save_daily_plan(con, D, {"no_trade": False, "picks": [
        {"ticker": "NKE", "direction": "short", "setup_type": "breakdown_short",
         "size_r": 0.25, "entry_plan": {"type": "limit"}, "entry_price": 31.7,
         "stop_price": 33.5, "target_price": 28.5, "entry_not_before": "09:45"}]}, {}, "chop")
    assert ledger.picks_for_date(con, D)[0]["entry_not_before"] == "09:45"
    ledger.save_rejections(con, D, [{"ticker": "WDC", "direction": "short", "entry_type": "limit",
                                     "entry_price": 424.0, "stop_price": 445.0,
                                     "target_price": 400.0, "entry_not_before": "09:45"}])
    assert ledger.rejections_for_date(con, D)[0]["entry_not_before"] == "09:45"
    con.close()
    assert ids


# ------------------------------------------------------------ live placement

def _live_cfg(tmp_path, monkeypatch, **live):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "live.duckdb")
    return Config(raw={
        "live_paper": {"enabled": True, "alpaca_paper": True,
                       "trading_base": live_broker.PAPER_BASE, "max_picks_per_day": 16,
                       "breakout_two_step": False, **live},
        "dollars_per_r": 500, "flatten_at": None,
    }, alpaca_key_id="key", alpaca_secret_key="secret")


def _seed_plan(picks):
    con = ledger.connect()
    ids = ledger.save_daily_plan(con, D, {"no_trade": False, "picks": picks}, {}, "chop")
    con.close()
    return ids


def _pick(ticker, direction, kind, entry, stop, target, window="09:45", time_stop="by 15:00"):
    return {"ticker": ticker, "direction": direction, "setup_type": "sympathy",
            "size_r": 0.25, "entry_plan": {"type": kind}, "entry_price": entry,
            "stop_price": stop, "target_price": target, "time_stop": time_stop,
            "entry_not_before": window}


class _MD:
    def __init__(self, prices):
        self.prices = prices
        self.alpaca = self

    def snapshots(self, syms):
        return {s: {"latestTrade": {"p": self.prices[s]}} for s in syms if s in self.prices}


def _run(cfg, at, prices, sent):
    con = ledger.connect()
    try:
        return live_monitor.place_deferred_orders(con, cfg, D, at, md=_MD(prices))
    finally:
        con.close()


@pytest.fixture()
def broker(monkeypatch):
    sent = []

    def _submit(ticker, qty, side, kind, entry, stop, target, cfg=None):
        sent.append((ticker, side, kind, entry))
        return {"id": f"o-{ticker}-{len(sent)}", "status": "new", "order_class": "bracket"}

    monkeypatch.setattr(live_broker, "submit_bracket_order", _submit)
    return sent


def test_nothing_goes_live_before_the_window(tmp_path, monkeypatch, broker):
    cfg = _live_cfg(tmp_path, monkeypatch)
    _seed_plan([_pick("NKE", "short", "limit", 31.7, 33.5, 28.5)])
    out = _run(cfg, datetime(2026, 10, 2, 9, 40, 2, tzinfo=ET), {"NKE": 31.8}, broker)
    assert out["waiting"] == 1 and broker == []


def test_orders_go_live_at_the_window_once(tmp_path, monkeypatch, broker):
    cfg = _live_cfg(tmp_path, monkeypatch)
    _seed_plan([_pick("NKE", "short", "limit", 31.7, 33.5, 28.5),
                _pick("MU", "long", "limit", 1090.0, 1060.0, 1140.0)])
    at = datetime(2026, 10, 2, 9, 45, 3, tzinfo=ET)
    out = _run(cfg, at, {"NKE": 31.4, "MU": 1097.6}, broker)
    assert out["placed"] == 2 and [b[0] for b in broker] == ["NKE", "MU"]
    again = _run(cfg, at + timedelta(minutes=5), {"NKE": 31.4, "MU": 1097.6}, broker)
    assert again["placed"] == 0 and len(broker) == 2          # idempotent
    con = ledger.connect()
    rows = ledger.deferred_placements_for_date(con, D)
    con.close()
    assert {r["status"] for r in rows.values()} == {"placed"}


def test_a_limit_already_through_its_stop_is_not_sent(tmp_path, monkeypatch, broker):
    """It would fill on the wrong side of its own stop -- the simulator grades
    that invalid_fill, so the paper account must not hold it either."""
    cfg = _live_cfg(tmp_path, monkeypatch)
    _seed_plan([_pick("NKE", "short", "limit", 31.7, 33.5, 28.5)])
    out = _run(cfg, datetime(2026, 10, 2, 9, 45, 3, tzinfo=ET), {"NKE": 33.9}, broker)
    assert broker == [] and "already through the stop" in out["skipped"][0]
    sim = grading.simulate_pick({**NKE, "entry_not_before": "09:45"},
                                _bars([31.8] * 15 + [33.9] * 30), None)
    assert sim["exit_reason"] == "invalid_fill"              # the two agree


def test_a_pick_whose_time_stop_passed_first_is_skipped(tmp_path, monkeypatch, broker):
    cfg = _live_cfg(tmp_path, monkeypatch)
    _seed_plan([_pick("NKE", "short", "limit", 31.7, 33.5, 28.5, time_stop="by 10:00")])
    out = _run(cfg, datetime(2026, 10, 2, 10, 5, 3, tzinfo=ET), {"NKE": 31.4}, broker)
    assert broker == [] and "time_stop passed" in out["skipped"][0]


def test_open_entries_and_runner_picks_are_not_the_placers_business(tmp_path, monkeypatch, broker):
    cfg = _live_cfg(tmp_path, monkeypatch)
    _seed_plan([_pick("GAP", "long", "open", 20.0, 19.0, 22.0, window=None)])
    out = _run(cfg, datetime(2026, 10, 2, 9, 50, 3, tzinfo=ET), {"GAP": 20.1}, broker)
    assert out["placed"] == 0 and broker == []


def test_a_breakout_is_skipped_and_said_so_while_two_step_is_off(tmp_path, monkeypatch, broker):
    cfg = _live_cfg(tmp_path, monkeypatch)
    _seed_plan([_pick("WDC", "short", "breakout", 420.0, 445.0, 395.0)])
    out = _run(cfg, datetime(2026, 10, 2, 9, 45, 3, tzinfo=ET), {"WDC": 426.0}, broker)
    assert broker == [] and "breakout_two_step" in out["skipped"][0]


def test_a_broker_refusal_is_retried_next_tick(tmp_path, monkeypatch):
    cfg = _live_cfg(tmp_path, monkeypatch)
    _seed_plan([_pick("MU", "long", "limit", 1090.0, 1060.0, 1140.0)])
    calls = []
    monkeypatch.setattr(live_broker, "submit_bracket_order",
                        lambda *a, **k: calls.append(a[0]) or (None if len(calls) == 1 else
                                                               {"id": "o-1", "status": "new"}))
    at = datetime(2026, 10, 2, 9, 45, 3, tzinfo=ET)
    first = _run(cfg, at, {"MU": 1097.6}, None)
    second = _run(cfg, at + timedelta(minutes=5), {"MU": 1097.6}, None)
    assert first["refused"] and second["placed"] == 1 and calls == ["MU", "MU"]


def test_the_morning_run_sends_only_open_entries(tmp_path, monkeypatch, broker):
    cfg = _live_cfg(tmp_path, monkeypatch)
    picks = [_pick("NKE", "short", "limit", 31.7, 33.5, 28.5),
             _pick("GAP", "long", "open", 20.0, 19.0, 22.0, window=None)]
    ids = _seed_plan(picks)
    con = ledger.connect()
    try:
        out = morning._place_premarket_orders(con, cfg, D, {"picks": picks}, ids)
        orders = ledger.live_orders_for_date(con, D)
    finally:
        con.close()
    assert out == {"placed": ["GAP"], "deferred": ["NKE"], "not_placed": []}
    assert [b[0] for b in broker] == ["GAP"] and [o["ticker"] for o in orders] == ["GAP"]


def test_evening_reconcile_names_a_windowed_pick_that_never_reached_the_broker(tmp_path, monkeypatch):
    cfg = _live_cfg(tmp_path, monkeypatch)
    _seed_plan([_pick("NKE", "short", "limit", 31.7, 33.5, 28.5)])
    con = ledger.connect()
    try:
        out = live_monitor.reconcile_evening(con, cfg, D)
    finally:
        con.close()
    assert out["never_placed"] == ["NKE"]


# ------------------------------------------------------------------ display

def test_the_morning_email_says_when_each_order_goes_live():
    from agent.delivery import morning_email_html
    html = morning_email_html(
        D, {"no_trade": False, "market_stance": "x", "picks": [
            {"ticker": "NKE", "direction": "short", "setup_type": "breakdown_short",
             "entry_plan": {"type": "limit"}, "entry_price": 31.7, "stop_price": 33.5,
             "target_price": 28.5, "size_r": 0.25, "conviction": 3, "thesis": "t",
             "entry_not_before": "09:45"}]},
        "regime")
    assert "limit @ 31.7 (live 09:45 ET)" in html


# ------------------------------------------- the comparison arms read it too

def test_paired_arm_b_is_graded_under_the_same_window(tmp_path, monkeypatch):
    from agent.loops import paired
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "p.duckdb")
    con = ledger.connect()
    paired.record_arm(con, D, "B", {"no_trade": False, "picks": [
        {"ticker": "NKE", "direction": "short", "setup_type": "breakdown_short",
         "entry_plan": {"type": "limit"}, "entry_price": 31.7, "stop_price": 33.5,
         "target_price": 28.5, "size_r": 0.25, "entry_not_before": "09:45"}]},
        {}, playbook_version="v9", context_mode="full", regime_tag="chop")
    seen = {}

    class _MD:
        def minute_bars(self, t, d):
            return _bars([31.80] * 30 + [31.40] * 60)
        def daily_bar(self, t, d):
            return None

    def _sim(pick, bars, daily, **kw):
        seen.update(pick)
        return grading.simulate_pick(pick, bars, daily, **kw)

    paired.grade_paired_arms(con, _MD(), D, _sim)
    row = con.execute("SELECT entry_not_before, entry_fill FROM paired_arm_picks").fetchone()
    con.close()
    assert seen["entry_not_before"] == "09:45" and row[0] == "09:45"
    assert row[1] == pytest.approx(31.80)          # the 09:45 bar, not the open


def test_the_shadow_ledger_prices_a_rejection_inside_its_window(tmp_path, monkeypatch):
    from agent.loops import evening
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "s.duckdb")
    con = ledger.connect()
    ledger.save_rejections(con, D, [{"ticker": "NKE", "direction": "short",
                                     "entry_type": "limit", "entry_price": 31.70,
                                     "stop_price": 33.50, "target_price": 28.50,
                                     "entry_not_before": "09:45"}])
    prices = [31.50, 31.75] + [31.20] * 13 + [31.00] * 60   # retest only at 09:31

    class _MD:
        def minute_bars(self, t, d):
            return _bars(prices)

    evening._grade_rejections(con, _MD(), D)
    r = ledger.rejections_for_date(con, D)[0]
    con.close()
    assert r["eod_outcome"] == "scratch" and r["forgone_r"] == 0.0     # never filled
