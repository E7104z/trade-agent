"""
D5 (docs/FIX_PHASE0.md): an entry order must express the setup it was proposed for.

Nothing linked setup_type to entry_type. 2026-10-15/16:
  * CMC  catalyst_momentum  buy LIMIT 66.50 under a stock that was 67.97 at plan time and
    opened 69.38: the limit waited for a dip the thesis never expected;
  * USO  sector_rotation    buy LIMIT 155.50 vs 156.80, never filled (low 155.80), closed +3%;
  * RKLB breakdown_short    a sell LIMIT 54.00 above the market instead of a sell STOP below
    it; the simulator "filled" it on a bar the broker never traded through.
INTC 10-15 and AMAT 10-16 (breakout shorts) are the controls: the order expresses the setup.

The reference prices are the real last 1-minute premarket closes at or before 07:10 ET (the
morning plan's clock), from Alpaca SIP bars: USO's 156.80 is the figure the incident report
quotes. The tag is TAG ONLY: `validator.enforce_entry_semantics` defaults to false and with it
off no pick is dropped.
"""
from __future__ import annotations

import pytest

from agent import config, ledger
from agent.config import Config
from agent.loops.morning import ENTRY_OK, _entry_semantics, _validate_plan

# (setup_type, direction, entry_type, entry, stop, target, time_stop, reference at 07:10 ET)
CMC = ("catalyst_momentum", "long", "limit", 66.50, 64.00, 72.00, "by 14:00", 67.97)     # 2026-10-15
USO = ("sector_rotation", "long", "limit", 155.50, 152.50, 161.00, "by 14:00", 156.80)   # 2026-10-16
RKLB = ("breakdown_short", "short", "limit", 54.00, 56.50, 48.00, "by 14:00", 52.35)     # 2026-10-15
INTC = ("breakdown_short", "short", "breakout", 92.00, 96.00, 86.00, "by 14:30", 93.821)  # 2026-10-15
AMAT = ("breakdown_short", "short", "breakout", 418.00, 433.00, 400.00, "by 14:00", 426.0)  # 2026-10-16

INCIDENTS = {
    "CMC": (CMC, "strength_buy_limit_below_ref"),
    "USO": (USO, "strength_buy_limit_below_ref"),
    "RKLB": (RKLB, "breakdown_short_with_limit"),
    "INTC": (INTC, ENTRY_OK),
    "AMAT": (AMAT, ENTRY_OK),
}


def _pick(ticker, case, **extra):
    setup, direction, etype, entry, stop, target, time_stop, _ref = case
    return {"ticker": ticker, "direction": direction, "setup_type": setup,
            "entry_plan": {"type": etype, "price": entry}, "entry_price": entry,
            "stop_price": stop, "target_price": target, "time_stop": time_stop,
            "size_r": 0.5, "conviction": 4, **extra}


def _cfg(enforce=None, **overrides):
    raw = {"max_picks": 8, "min_size_r": 0.25, "max_size_r": 1.5,
           "max_total_open_risk_r": 10.0, "drawdown_pause_r": -4.0}
    if enforce is not None:
        raw["validator"] = {"enforce_entry_semantics": enforce}
    raw.update(overrides)
    return Config(raw=raw)


def _refs():
    return {t: case[-1] for t, (case, _tag) in INCIDENTS.items()}


def _validate(enforce=None, tickers=tuple(INCIDENTS)):
    plan = {"no_trade": False, "picks": [_pick(t, INCIDENTS[t][0]) for t in tickers]}
    return _validate_plan(plan, _cfg(enforce), ref_prices=_refs())


# ------------------------------------------------------------- the five incident picks

@pytest.mark.parametrize("ticker", list(INCIDENTS))
def test_the_incident_picks_are_tagged_as_the_incidents_read(ticker):
    case, expected = INCIDENTS[ticker]
    setup, direction, etype, entry, _stop, _target, time_stop, ref = case
    tag, why = _entry_semantics(setup, direction, etype, entry, ref, time_stop=time_stop)
    assert tag == expected
    assert (why == "") == (tag == ENTRY_OK)
    if tag != ENTRY_OK:
        assert str(entry) in why                      # the message names the order it is about


def test_with_enforcement_off_every_pick_is_tagged_and_none_is_dropped():
    out, warnings = _validate()                        # the config default
    assert [p["ticker"] for p in out["picks"]] == list(INCIDENTS)
    assert out["validator_drops"] == [] and out["no_trade"] is False
    for p in out["picks"]:
        assert p["entry_semantics"] == INCIDENTS[p["ticker"]][1], p["ticker"]
        assert p["entry_ref_price"] == pytest.approx(INCIDENTS[p["ticker"]][0][-1])
    tagged = [w for w in warnings if "ENTRY SEMANTICS tag-only" in w]
    assert len(tagged) == 3 and all(t in " ".join(tagged) for t in ("CMC", "USO", "RKLB"))
    assert not any(t in " ".join(tagged) for t in ("INTC", "AMAT"))


def test_explicitly_off_behaves_exactly_like_the_default():
    default, _ = _validate()
    off, _ = _validate(enforce=False)
    assert [(p["ticker"], p["entry_semantics"]) for p in default["picks"]] == \
           [(p["ticker"], p["entry_semantics"]) for p in off["picks"]]


def test_enforcement_on_drops_exactly_the_mismatched_picks():
    out, warnings = _validate(enforce=True)
    assert [p["ticker"] for p in out["picks"]] == ["INTC", "AMAT"]
    assert all(p["entry_semantics"] == ENTRY_OK for p in out["picks"])
    dropped = {x["ticker"]: x for x in out["validator_drops"]}
    assert set(dropped) == {"CMC", "USO", "RKLB"}
    assert all(x["kind"] == "validity" for x in dropped.values())
    assert "validator.enforce_entry_semantics" in dropped["RKLB"]["reason"]
    assert "sell stop" in dropped["RKLB"]["reason"]


def test_the_default_config_does_not_enforce():
    assert config._DEFAULTS["validator"] == {"enforce_entry_semantics": False}
    # no `validator` section at all (an old config, a bare test Config) means off
    out, _ = _validate_plan({"no_trade": False, "picks": [_pick("RKLB", RKLB)]},
                            Config(raw={"max_picks": 4, "min_size_r": 0.25, "max_size_r": 1.5,
                                        "max_total_open_risk_r": 3.0}),
                            ref_prices={"RKLB": RKLB[-1]})
    assert len(out["picks"]) == 1 and out["picks"][0]["entry_semantics"] == "breakdown_short_with_limit"


def test_the_tag_and_its_reference_are_saved_with_the_pick(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "sem.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    out, _ = _validate()
    con = ledger.connect()
    from datetime import date
    ledger.save_daily_plan(con, date(2026, 10, 16), out, {"candidates": []}, "chop")
    saved = {r["ticker"]: r for r in ledger.picks_for_date(con, date(2026, 10, 16))}
    con.close()
    assert set(saved) == set(INCIDENTS)
    for t, (case, expected) in INCIDENTS.items():
        assert saved[t]["entry_semantics"] == expected, t
        assert saved[t]["entry_ref_price"] == pytest.approx(case[-1]), t


# --------------------------------------------------------------- each rule, both ways

def test_a_momentum_short_that_waits_for_a_bounce_is_the_mirror_mismatch():
    assert _entry_semantics("catalyst_momentum", "short", "limit", 105.0, 100.0)[0] == \
           "weakness_sell_limit_above_ref"
    assert _entry_semantics("sector_rotation", "short", "limit", 105.0, 100.0)[0] == \
           "weakness_sell_limit_above_ref"
    # a short limit at or below the market is the mistype gate's business, not this tag's
    assert _entry_semantics("catalyst_momentum", "short", "limit", 99.0, 100.0)[0] == ENTRY_OK


def test_a_strength_long_is_ok_when_the_order_does_not_wait_for_a_dip():
    for etype in ("breakout", "open"):
        assert _entry_semantics("catalyst_momentum", "long", etype, 66.50, 67.97)[0] == ENTRY_OK
    assert _entry_semantics("catalyst_momentum", "long", "limit", 67.97, 67.97)[0] == ENTRY_OK
    assert _entry_semantics("catalyst_momentum", "long", "limit", 68.50, 67.97)[0] == ENTRY_OK


def test_a_pullback_setup_may_rest_a_limit_under_the_market():
    for setup in ("trend_pullback", "oversold_reversal", "sympathy", "gap_go", "earnings_drift"):
        assert _entry_semantics(setup, "long", "limit", 66.5, 67.97)[0] == ENTRY_OK, setup


def test_a_breakdown_short_is_ok_only_as_a_stop():
    assert _entry_semantics("breakdown_short", "short", "breakout", 92.0, 93.8)[0] == ENTRY_OK
    assert _entry_semantics("breakdown_short", "short", "open", 92.0, 93.8)[0] == ENTRY_OK
    assert _entry_semantics("breakdown_short", "short", "limit", 94.5, 93.8)[0] == \
           "breakdown_short_with_limit"
    assert _entry_semantics("breakdown_short", "short", "limit", 91.0, 93.8)[0] == \
           "breakdown_short_with_limit"           # below the market too: still a limit


def test_a_breakout_long_is_ok_only_as_a_stop_and_the_rule_is_for_longs():
    assert _entry_semantics("breakout", "long", "limit", 99.0, 100.0)[0] == "breakout_long_with_limit"
    assert _entry_semantics("breakout", "long", "breakout", 101.0, 100.0)[0] == ENTRY_OK
    assert _entry_semantics("breakout", "short", "limit", 101.0, 100.0)[0] == ENTRY_OK


def test_a_gap_fade_limit_needs_an_expiry_earlier_than_its_time_stop():
    no_expiry = _entry_semantics("gap_fade", "short", "limit", 25.0, 24.0, time_stop="by 14:00")
    assert no_expiry[0] == "gap_fade_limit_without_expiry" and "by 14:00" in no_expiry[1]
    assert _entry_semantics("gap_fade", "short", "limit", 25.0, 24.0, time_stop="by 14:00",
                            entry_expires="by 10:30")[0] == ENTRY_OK
    assert _entry_semantics("gap_fade", "short", "limit", 25.0, 24.0, time_stop="by 14:00",
                            entry_expires="by 14:00")[0] == "gap_fade_limit_without_expiry"
    assert _entry_semantics("gap_fade", "short", "limit", 25.0, 24.0, time_stop="by 14:00",
                            entry_expires="by 15:00")[0] == "gap_fade_limit_without_expiry"
    assert _entry_semantics("gap_fade", "short", "limit", 25.0, 24.0, time_stop=None,
                            entry_expires="by 10:30")[0] == ENTRY_OK
    assert _entry_semantics("gap_fade", "short", "breakout", 24.0, 25.0,
                            time_stop="by 14:00")[0] == ENTRY_OK
    assert _entry_semantics("gap_fade", "short", "limit", 25.0, 24.0, time_stop="by 14:00",
                            entry_expires="soon")[0] == "gap_fade_limit_without_expiry"


def test_the_validator_passes_the_expiry_and_the_time_stop_to_the_gap_fade_rule():
    def run(expires_by, time_stop="by 14:00"):
        pick = {"ticker": "GFDE", "direction": "short", "setup_type": "gap_fade",
                "entry_plan": {"type": "limit", "price": 25.0, **(
                    {"expires_by": expires_by} if expires_by else {})},
                "entry_price": 25.0, "stop_price": 26.5, "target_price": 22.0,
                "time_stop": time_stop, "size_r": 0.5, "conviction": 4}
        out, _ = _validate_plan({"no_trade": False, "picks": [pick]}, _cfg(),
                                ref_prices={"GFDE": 24.0})
        return out["picks"][0]["entry_semantics"]

    assert run(None) == "gap_fade_limit_without_expiry"
    assert run("by 10:30") == ENTRY_OK
    assert run("by 15:00") == "gap_fade_limit_without_expiry"     # later than the time stop


def test_a_reference_priced_rule_without_a_reference_says_so_rather_than_ok():
    assert _entry_semantics("catalyst_momentum", "long", "limit", 66.5, None)[0] == "unknown_ref"
    assert _entry_semantics("catalyst_momentum", "long", "limit", 66.5, 0)[0] == "unknown_ref"
    # rules that need no reference do not need one
    assert _entry_semantics("breakdown_short", "short", "limit", 54.0, None)[0] == \
           "breakdown_short_with_limit"
    assert _entry_semantics("catalyst_momentum", "long", "breakout", 66.5, None)[0] == ENTRY_OK


def test_setup_and_direction_text_is_matched_case_insensitively_and_unknowns_are_ok():
    assert _entry_semantics(" Catalyst_Momentum ", "LONG", "LIMIT", 66.5, 67.97)[0] == \
           "strength_buy_limit_below_ref"
    assert _entry_semantics(None, "long", "limit", 66.5, 67.97)[0] == ENTRY_OK
    assert _entry_semantics("novel_setup", None, None, 66.5, 67.97)[0] == ENTRY_OK
