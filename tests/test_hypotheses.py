"""
The trader's hypothesis notebook (specs/TRADER_HYPOTHESIS_SPEC_v1.md).

Pinned here:
  - the register: frozen pre-registration, system-set bar, near-duplicates
    resolve to the open idea, the graveyard, the daily cap, a full notebook
  - the lane: a tagged reject is tested at minimum size; a broken order or a
    `hypothesis_fit` kill stays off; a tagged approval is stamped, not
    duplicated; one test per idea per day; coverage is untouched; `novel`
    needs an idea; off, nothing changes
  - the sample: one unit per session, fill-valid triggered trades only,
    nothing before the idea opened
  - verdicts: two looks at alpha/2, cohort BH, abandoned ideas at p = 1,
    expiry, a SUPPORTED idea with a predicate is quarantined as a rule
  - the evening: notes never change n; ungrounded ideas are refused; a lesson
    can be nominated; grader ideas open on the next session
  - forward by construction: only the morning writes a pick's tag
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from agent import hypotheses as hyp
from agent import ledger, research
from agent.config import Config
from agent.llm import load_prompt
from agent.loops.morning import _run_test_lanes, _validate_plan

ROOT = Path(__file__).resolve().parent.parent
MON = date(2026, 10, 5)

LANE = {"enabled": True, "size_r": 0.25, "min_rr": 1.0, "max_picks": 2, "max_new_per_day": 1,
        "max_open": 6, "blocking_kills": ["grounding", "entry_type", "event_risk", "hypothesis_fit"]}
COVERAGE = {"enabled": True, "size_r": 0.25, "min_rr": 1.0, "max_picks": 12,
            "blocking_kills": ["grounding", "entry_type", "event_risk"]}


def _cfg(lane=LANE, coverage=COVERAGE) -> Config:
    return Config(raw={"max_picks": 4, "min_size_r": 0.25, "max_size_r": 1.5,
                       "max_total_open_risk_r": 3.0, "drawdown_pause_r": -4.0,
                       "coverage_lane": coverage, "hypothesis_lane": lane,
                       "daily_floor": {"enabled": True, "size_r": 0.25, "min_rr": 1.0}})


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "h.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


IDEA = {"statement": "fading a gap_go that loses premarket VWAP in the first half hour makes money",
        "refutation": "the fades lose money over the committed sample", "belief": 4,
        "direction": "short", "setup_type": "gap_fade", "origin": "three of those faded on 10/02"}


def _open(con, statement=IDEA["statement"], d=date(2026, 9, 1), **kw):
    regs = hyp.plan_registrations(con, d, [{**IDEA, "statement": statement, **kw}], _cfg())
    assert regs[0]["action"] == "create", regs
    hyp._insert(con, regs[0]["row"], d, d, "analyst")
    return regs[0]["hyp_id"]


def _pick(con, d, hid, r=None, triggered=True, exit_reason="target", ticker="AAA", lane=None):
    pid = con.execute(
        "INSERT INTO picks (plan_date, ticker, direction, setup_type, hypothesis_id, exploration_lane) "
        "VALUES (?,?,?,?,?,?) RETURNING pick_id",
        [d, ticker, "short", "gap_fade", hid, lane]).fetchone()[0]
    if r is not None or not triggered:
        con.execute("INSERT INTO outcomes (pick_id, eval_date, triggered, r_multiple, r_planned, "
                    "exit_reason) VALUES (?,?,?,?,?,?)",
                    [pid, d, triggered, r, r, exit_reason])
    return pid


# ------------------------------------------------------------------ register

def test_v31_schema(con):
    cols = {r[0] for r in con.execute("DESCRIBE trader_hypotheses").fetchall()}
    assert {"statement", "refutation", "target_n", "max_n", "min_sessions", "state",
            "looks_done", "cohort_counted", "belief", "origin"} <= cols
    assert "hypothesis_id" in {r[0] for r in con.execute("DESCRIBE picks").fetchall()}
    assert {r[0] for r in con.execute("DESCRIBE hypothesis_reviews").fetchall()} == \
        {"session_date", "pick_id", "hyp_id", "expressed", "note"}


def test_a_hypothesis_needs_a_refutation_and_a_real_claim():
    assert hyp.clean_proposal({**IDEA, "refutation": ""})[0] is None
    assert hyp.clean_proposal({**IDEA, "statement": "weird tape"})[0] is None
    prop, _ = hyp.clean_proposal({**IDEA, "predicate": {"lane": "morning", "conditions": [
        {"dimension": "a"}, {"dimension": "b"}, {"dimension": "c"}]}})
    assert prop["predicate"] is None and "dropped" in prop["note"]


def test_the_system_sets_the_bar_not_the_llm(con):
    hid = _open(con, target_n=3, max_n=4)
    row = next(r for r in hyp.all_hypotheses(con) if r["hyp_id"] == hid)
    assert (row["target_n"], row["max_n"], row["min_sessions"]) == (8, 16, 10)


def test_a_reworded_twin_resolves_to_the_open_idea(con):
    hid = _open(con)
    twin = IDEA["statement"].replace("makes money", "makes money too")
    reg = hyp.plan_registrations(con, MON, [{**IDEA, "statement": twin}], _cfg())[0]
    assert reg == {**reg, "action": "existing", "hyp_id": hid}


def test_graveyard_daily_cap_and_full_notebook(con):
    hid = _open(con)
    con.execute("UPDATE trader_hypotheses SET state = 'REFUTED', closed_at = ? WHERE hyp_id = ?",
                [date(2026, 9, 28), hid])
    early = hyp.plan_registrations(con, MON, [IDEA], _cfg())[0]
    assert early["action"] == "reject" and "REFUTED" in early["reason"]
    late = hyp.plan_registrations(con, date(2026, 11, 16), [IDEA], _cfg())[0]
    assert late["action"] == "create"
    two = [{**IDEA, "statement": "longs on sympathy names after a leader beats by 10% work"},
           {**IDEA, "statement": "shorting breakdown_short on high short interest fails often"}]
    regs = hyp.plan_registrations(con, MON, two, _cfg())
    assert [r["action"] for r in regs] == ["create", "reject"] and "daily cap" in regs[1]["reason"]
    full = hyp.plan_registrations(con, MON, two[:1], _cfg({**LANE, "max_open": 0}))[0]
    assert full["action"] == "reject" and "notebook full" in full["reason"]


def test_frozen_columns_are_never_updated_and_only_the_morning_tags_a_pick():
    frozen = ("statement", "refutation", "target_n", "max_n", "min_sessions", "opened_at")
    offenders = []
    for path in list((ROOT / "agent").rglob("*.py")) + [ROOT / "run.py", ROOT / "api" / "index.py"]:
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"UPDATE\s+trader_hypotheses\s+SET(.*?)WHERE", text, re.S | re.I):
            if any(re.search(rf"\b{c}\s*=", m.group(1)) for c in frozen):
                offenders.append(path.name)
        if re.search(r"UPDATE\s+picks\s+SET[^\"']*hypothesis_id", text, re.I):
            offenders.append(f"{path.name}: writes picks.hypothesis_id")
        if "hypothesis_id" in text and "INSERT INTO picks" in text and path.name != "ledger.py":
            offenders.append(f"{path.name}: inserts tagged picks")
    assert offenders == []


# ---------------------------------------------------------------------- lane

def _cand(ticker, setup, tag=None, target=11.5, conviction=3):
    c = {"ticker": ticker, "direction": "long", "setup_type": setup, "conviction": conviction,
         "thesis": f"{ticker} thesis", "entry_plan": {"type": "limit", "price": 10.0},
         "entry_price": 10.0, "stop_price": 9.0, "target_price": target}
    if tag:
        c["hypothesis_id"] = tag
    return c


REFS = {t: 10.0 for t in ("AAA", "BBB", "CCC", "DDD", "EEE", "NOV")}


def _lanes(plan, cands, cfg, tags, priority=None):
    hp = {"tags": tags, "priority": priority or {}}
    import agent.loops.morning as m
    orig = m._trial_validator
    m._trial_validator = lambda cfg, md, bundle, c, q, s, dd: (
        lambda trial: _validate_plan(trial, cfg, s, dd, ref_prices=REFS, atr_map={},
                                     earnings_map={}))
    try:
        return _run_test_lanes(plan, {"candidates": cands}, cfg, None, {}, {}, None, None, hyp=hp)
    finally:
        m._trial_validator = orig


def _plan(picks=(), **kills):
    return {"no_trade": not picks, "picks": list(picks),
            "rejected": [{"ticker": t, "reason": "x", "primary_kill_check": k} for t, k in kills.items()]}


def test_a_tagged_reject_is_tested_at_minimum_size_and_coverage_is_untouched():
    cands = [_cand("AAA", "gap_go"), _cand("BBB", "gap_go", tag="H-1"),
             _cand("CCC", "breakout", tag="new:0")]
    off_plan = _plan(AAA="rr", BBB="conviction", CCC="hypothesis_fit")
    on_plan = _plan(AAA="rr", BBB="conviction", CCC="hypothesis_fit")
    _lanes(off_plan, cands, _cfg({**LANE, "enabled": False}), {"H-1": "H-1", "new:0": "H-9"})
    _lanes(on_plan, cands, _cfg(), {"H-1": "H-1", "new:0": "H-9"})
    cov_off = [p["ticker"] for p in off_plan["picks"] if p["exploration_lane"] == "coverage"]
    cov_on = [p["ticker"] for p in on_plan["picks"] if p["exploration_lane"] == "coverage"]
    assert cov_on == cov_off == ["AAA", "CCC"]
    tested = [p for p in on_plan["picks"] if p["exploration_lane"] == "hypothesis"]
    assert [(p["ticker"], p["hypothesis_id"], p["size_r"]) for p in tested] == [("BBB", "H-1", 0.25)]
    # CCC was killed on hypothesis_fit: covered as a setup, never counted for the idea
    ccc = next(p for p in on_plan["picks"] if p["ticker"] == "CCC")
    assert "hypothesis_id" not in ccc
    assert all("hypothesis_id" not in p for p in off_plan["picks"])


def test_a_hypothesis_fit_kill_never_reaches_the_tape_through_the_lane():
    cands = [_cand("AAA", "gap_go"), _cand("BBB", "gap_go", tag="H-1")]
    plan = _plan([_cand("AAA", "gap_go") | {"size_r": 1.0}], BBB="hypothesis_fit")
    _lanes(plan, cands, _cfg(coverage={**COVERAGE, "enabled": False}), {"H-1": "H-1"})
    assert [p["ticker"] for p in plan["picks"]] == ["AAA"]


def test_a_tagged_approval_is_stamped_not_duplicated_and_one_test_per_idea_per_day():
    cands = [_cand("AAA", "gap_go", tag="H-1"), _cand("BBB", "breakout", tag="H-1"),
             _cand("DDD", "gap_fade", tag="H-2")]
    approved = {**_cand("AAA", "gap_go"), "size_r": 1.0}
    plan = _plan([approved], BBB="rr", DDD="rr")
    _lanes(plan, cands, _cfg(coverage={**COVERAGE, "enabled": False}), {"H-1": "H-1", "H-2": "H-2"})
    by = {p["ticker"]: p for p in plan["picks"]}
    assert by["AAA"]["hypothesis_id"] == "H-1" and by["AAA"]["size_r"] == 1.0
    assert "BBB" not in by                      # H-1 is already tested today
    assert by["DDD"]["exploration_lane"] == "hypothesis"


def test_unfinished_ideas_go_first_and_the_cap_holds():
    cands = [_cand(t, "gap_go", tag=h) for t, h in
             (("AAA", "new:0"), ("BBB", "H-far"), ("CCC", "H-close"), ("DDD", "H-mid"))]
    plan = _plan(AAA="rr", BBB="rr", CCC="rr", DDD="rr")
    tags = {"new:0": "H-new", "H-far": "H-far", "H-close": "H-close", "H-mid": "H-mid"}
    _lanes(plan, cands, _cfg(coverage={**COVERAGE, "enabled": False}), tags,
           priority={"H-far": 7, "H-close": 1, "H-mid": 4, "H-new": 9})
    assert [p["hypothesis_id"] for p in plan["picks"]] == ["H-close", "H-mid"]


def test_novel_needs_an_idea_and_raw_tags_never_survive():
    cands = [_cand("NOV", "novel"), _cand("AAA", "gap_go", tag="new:3")]
    plan = _plan([{**_cand("NOV", "novel"), "size_r": 1.0},
                  {**_cand("AAA", "gap_go", tag="new:3"), "size_r": 1.0}])
    _lanes(plan, cands, _cfg(coverage={**COVERAGE, "enabled": False}), {})
    assert [p["ticker"] for p in plan["picks"]] == ["AAA"]
    assert "hypothesis_id" not in plan["picks"][0]


# -------------------------------------------------------------------- commit

def test_commit_writes_ideas_abandons_and_last_tested(con):
    keep = _open(con)
    drop = _open(con, statement="sympathy longs on a second-tier name fade by noon reliably")
    hp = hyp.plan_for_morning(con, MON, {"new_hypotheses": [
        {**IDEA, "statement": "a breakout above the premarket high on 3x volume holds into 11:00"}],
        "abandon": [{"hyp_id": drop, "why": "regime changed"}, {"hyp_id": keep, "why": "x"}]}, _cfg())
    plan = {"picks": [{"ticker": "AAA", "hypothesis_id": keep}]}
    out = hyp.commit_morning(con, MON, hp, plan)
    rows = {r["hyp_id"]: r for r in hyp.all_hypotheses(con)}
    assert len(out["created"]) == 1 and rows[out["created"][0]]["state"] == "OPEN"
    assert rows[drop]["state"] == "ABANDONED" and rows[drop]["cohort_counted"] is False
    assert rows[keep]["state"] == "OPEN"        # traded today: not dropped in the same breath
    assert str(rows[keep]["last_tagged_at"]) == "2026-10-05"


# -------------------------------------------------------------------- sample

def test_the_unit_is_the_session_and_only_valid_trades_count(con):
    hid = _open(con)
    _pick(con, date(2026, 8, 28), hid, r=5.0)                      # before it opened
    _pick(con, date(2026, 9, 2), hid, r=1.0)
    _pick(con, date(2026, 9, 2), hid, r=-1.0, ticker="BBB")       # same tape: one unit
    _pick(con, date(2026, 9, 3), hid, r=2.0)
    _pick(con, date(2026, 9, 4), hid, triggered=False)
    _pick(con, date(2026, 9, 8), hid, r=9.0, exit_reason="invalid_fill")
    _pick(con, date(2026, 9, 9), hid)                              # not graded yet
    s = hyp.samples(con)[hid]
    assert s["units"] == [0.0, 2.0] and s["n"] == 2
    assert (s["untriggered"], s["invalid"], s["pending"]) == (1, 1, 1)


# ------------------------------------------------------------------ verdicts

def _units(con, hid, values, start=date(2026, 9, 2)):
    from datetime import timedelta
    from agent.util import is_market_day
    d = start
    for v in values:
        while not is_market_day(d):
            d += timedelta(days=1)
        _pick(con, d, hid, r=v)
        d += timedelta(days=1)


def _fix_p(monkeypatch, by_hid):
    real = hyp._stats

    def fake(units):
        st = real(units)
        for hid, p in by_hid.items():
            if units and units == fake.units.get(hid):
                st["p"] = p
        return st
    fake.units = {}
    monkeypatch.setattr(hyp, "_stats", fake)
    return fake


def test_bh_over_the_cohort_with_abandoned_ideas_at_p_one(con, monkeypatch):
    good = _open(con)
    gone = _open(con, statement="sympathy longs on a second-tier name fade by noon reliably")
    _units(con, good, [0.5, 0.6, 0.4, 0.7, 0.5, 0.6, 0.5, 0.4])
    fake = _fix_p(monkeypatch, {good: 0.02})
    fake.units[good] = hyp.samples(con)[good]["units"]
    con.execute("UPDATE trader_hypotheses SET state='ABANDONED', closed_at=?, cohort_counted=FALSE "
                "WHERE hyp_id=?", [date(2026, 9, 20), gone])
    res = hyp.evaluate(con, MON)
    # m = 2, p = (0.02, 1.0): the rank-1 BH bound is 0.025 * 1/2 = 0.0125
    assert res["cohort"] == 2 and not res["supported"] and [h["hyp_id"] for h in res["extended"]] == [good]
    rows = {r["hyp_id"]: r for r in hyp.all_hypotheses(con)}
    assert rows[good]["state"] == "OPEN" and rows[good]["looks_done"] == 1
    assert rows[gone]["cohort_counted"] is True


def test_alone_a_strong_idea_is_supported_and_quarantined_as_a_rule(con, monkeypatch):
    pred = {"lane": "morning", "conditions": [{"dimension": "setup_type", "level": "gap_fade"}]}
    good = _open(con, predicate=pred)
    _units(con, good, [0.5, 0.6, 0.4, 0.7, 0.5, 0.6, 0.5, 0.4])
    fake = _fix_p(monkeypatch, {good: 0.02})
    fake.units[good] = hyp.samples(con)[good]["units"]
    res = hyp.evaluate(con, MON)
    assert [h["hyp_id"] for h in res["supported"]] == [good]
    row = next(r for r in hyp.all_hypotheses(con) if r["hyp_id"] == good)
    assert row["state"] == "SUPPORTED" and row["rule_id"]
    rule = next(s for s in research.rule_states(con) if s["rule_id"] == row["rule_id"])
    assert rule["state"] == "QUARANTINED" and rule["source"] == "trader"
    assert rule["quarantined_at"] == "2026-10-05"
    assert "SUPPORTED idea is NOT edge" in hyp.weekly_md(res, con)


def test_at_look_one_a_marginal_idea_waits_and_a_loser_is_refuted(con, monkeypatch):
    marginal = _open(con)
    loser = _open(con, statement="sympathy longs on a second-tier name fade by noon reliably")
    _units(con, marginal, [0.5, 0.6, 0.4, 0.7, 0.5, 0.6, 0.5, 0.4])
    _units(con, loser, [-0.5, -0.6, 0.4, -0.7, -0.5, 0.6, -0.5, -0.4])
    fake = _fix_p(monkeypatch, {marginal: 0.04})
    fake.units[marginal] = hyp.samples(con)[marginal]["units"]
    res = hyp.evaluate(con, MON)
    assert [h["hyp_id"] for h in res["extended"]] == [marginal]
    assert [h["hyp_id"] for h in res["refuted"]] == [loser]


def test_neglected_ideas_expire_and_young_ones_wait(con):
    old = _open(con, d=date(2026, 8, 3))
    young = _open(con, statement="sympathy longs on a second-tier name fade by noon reliably",
                  d=date(2026, 9, 30))
    res = hyp.evaluate(con, MON)
    assert [h["hyp_id"] for h in res["expired"]] == [old]
    assert young in [h["hyp_id"] for h in res["waiting"]]


# ------------------------------------------------------------------ evening

def test_evening_notes_never_change_n_and_grader_ideas_open_next_session(con):
    hid = _open(con)
    pid = _pick(con, date(2026, 9, 2), hid, r=1.0)
    lid = ledger.insert_lesson(con, {"lesson": "gap fades into a secondary work below VWAP",
                                     "confidence": "low", "evidence": "two cases"}, "evening",
                               date(2026, 9, 1))
    review = {"pick_reviews": [{"pick_id": pid, "grade": "B",
                                "hypothesis_review": {"expressed": False, "note": "no VWAP loss"}}],
              "hypotheses": [{**IDEA, "statement": "longs that reclaim VWAP by 9:52 run 3.7R"},
                             {"from_lesson_id": lid}]}
    picks = [{"pick_id": pid, "hypothesis_id": hid, "ticker": "AAA"}]
    out = hyp.record_evening(con, _cfg({**LANE, "max_new_per_day": 2}), date(2026, 10, 2),
                             review, picks, corpus="nothing numeric here")
    assert out["reviews"] == 1 and hyp.samples(con)[hid]["n"] == 1
    assert any("ungrounded" in r for r in out["rejected"])
    new = next(r for r in hyp.all_hypotheses(con) if r["hyp_id"] in out["created"])
    assert new["source"] == "lesson" and new["from_lesson_id"] == lid
    assert str(new["opened_at"]) == "2026-10-05"           # Friday's review -> Monday


# ------------------------------------------------------------------ prompts

def test_prompts_carry_the_lane_only_when_it_is_on():
    for name, fn in (("morning_analyst", hyp.analyst_prompt_text),
                     ("morning_risk", hyp.risk_prompt_text),
                     ("evening_grader", hyp.grader_prompt_text)):
        on = load_prompt(name, {"COVERAGE_ANALYST": "", "COVERAGE_SECTION": "", **fn(_cfg())})
        off = load_prompt(name, {"COVERAGE_ANALYST": "", "COVERAGE_SECTION": "",
                                 **fn(_cfg({**LANE, "enabled": False}))})
        assert "{{HYPOTHESIS" not in on and "{{HYPOTHESIS" not in off
        assert "hypothes" in on.lower() and len(on) > len(off)
        assert "hypothesis_id" not in off and "new_hypotheses" not in off


def test_notebook_shows_counts_for_open_ideas_and_verdicts_for_closed(con):
    hid = _open(con)
    _pick(con, date(2026, 9, 2), hid, r=1.0)
    gone = _open(con, statement="sympathy longs on a second-tier name fade by noon reliably")
    con.execute("UPDATE trader_hypotheses SET state='REFUTED', closed_at=?, verdict_json=? "
                "WHERE hyp_id=?", [date(2026, 10, 2), json.dumps({"final": {"n": 8, "mean_r": -0.31,
                                                                          "p": 0.2}}), gone])
    md, ids = hyp.notebook_md(con, MON, _cfg())
    assert ids == [hid]
    open_block = md.split("Judged and closed")[0]
    assert "1/8 sessions" in open_block and "mean" not in open_block
    assert "REFUTED" in md and "-0.31R" in md


# ------------------------------------------------------- end to end (morning)

def test_morning_run_opens_the_idea_and_tags_the_test_trade(monkeypatch):
    """The real morning loop, LLMs and market data stubbed, both paired arms
    running: the analyst writes an idea down and tags a candidate with it; the
    risk manager kills that candidate on R:R; the lane trades it at minimum
    size with the registered id. Arm B makes the same decision but registers
    nothing."""
    import duckdb
    from agent.loops import morning
    import tests.test_morning_paired_concurrency as harness

    root = duckdb.connect(":memory:")
    root.execute(ledger._SCHEMA)
    for mig in ledger._MIGRATIONS:
        root.execute(mig)
    for name, fn in (("MarketData", harness._FakeMarketData),
                     ("render_bundle_md", lambda bundle: "BUNDLE"),
                     ("compute_regime", lambda cfg, d: {"date": d, "tag": "neutral"}),
                     ("regime_md", lambda reg: "REGIME"),
                     ("build_morning_memory", lambda con, cfg: {
                         "text": "## YOUR LAST 20 GRADED TRADES\n\nnone\n", "playbook_version": "1"}),
                     ("send_email", lambda cfg, subject, html: False),
                     ("render_dashboard", lambda con, cfg, d: "")):
        monkeypatch.setattr(morning, name, fn)
    monkeypatch.setattr(morning.ledger, "connect", root.cursor)

    approved = harness._pick("AAAA")
    tagged = {**harness._pick("BBBB"), "setup_type": "gap_fade", "direction": "short",
              "entry_plan": {"type": "open", "price": 100.0}, "stop_price": 102.0,
              "target_price": 96.5, "hypothesis_id": "new:0", "conviction": 2}

    def _call(*, system, user, model, mock_name, thinking=False, web_search=False, **kw):
        if mock_name == "morning_analyst":
            return harness._FakeLLMResult({"candidates": [approved, tagged],
                                           "new_hypotheses": [IDEA]})
        return harness._FakeLLMResult({"no_trade": False, "picks": [approved],
                                       "rejected": [{"ticker": "BBBB", "reason": "R:R 1.75",
                                                     "primary_kill_check": "rr"}]})

    class _LLM:
        def __init__(self, cfg):
            pass
        call = staticmethod(_call)
    monkeypatch.setattr(morning, "LLMClient", _LLM)

    cfg = harness._cfg()
    cfg.raw["hypothesis_lane"] = dict(LANE)
    cfg.raw["coverage_lane"] = {**COVERAGE, "enabled": False}
    morning.run(cfg, run_date=MON, force=True)

    rows = root.execute("SELECT hyp_id, state, source, opened_at FROM trader_hypotheses").fetchall()
    assert len(rows) == 1 and rows[0][1:] == ("OPEN", "analyst", MON)
    picks = dict(root.execute("SELECT ticker, hypothesis_id FROM picks").fetchall())
    lanes = dict(root.execute("SELECT ticker, exploration_lane FROM picks").fetchall())
    assert picks == {"AAAA": None, "BBBB": rows[0][0]}
    assert lanes["BBBB"] == "hypothesis"
    last = root.execute("SELECT last_tagged_at FROM trader_hypotheses").fetchone()[0]
    assert last == MON


# ------------------------------------------------------- end to end (evening)

def test_evening_run_reviews_the_test_and_writes_the_anecdote_down(tmp_path, monkeypatch):
    """The real evening loop, offline, grader stubbed: the tagged pick's
    review is stored, the grader's new idea opens on the next session, and the
    email carries the notebook lines."""
    from agent import llm
    from agent.config import load_config
    from agent.loops import evening
    from _traders_log_seed import playbook

    D = date(2026, 10, 2)
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "e.duckdb")
    playbook(tmp_path, monkeypatch)
    con = ledger.connect()
    hid = _open(con)
    ledger.save_daily_plan(con, D, {
        "no_trade": False, "market_stance": "t", "rejected": [], "picks": [{
            "ticker": "TEST", "direction": "short", "setup_type": "gap_fade", "conviction": 3,
            "size_r": 0.25, "entry_plan": {"type": "open", "price": 100.0}, "entry_price": 100.0,
            "stop_price": 102.0, "target_price": 96.0, "exploration_lane": "hypothesis",
            "hypothesis_id": hid}]}, {"candidates": []}, "chop")
    pid = con.execute("SELECT pick_id FROM picks").fetchone()[0]
    con.close()

    sent: list[str] = []
    grader_systems: list[str] = []

    class _Client:
        def __init__(self, cfg):
            pass

        def call(self, **kw):
            usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0, "cost_usd": 0.0}
            grader_systems.append(kw.get("system", "") + kw.get("user", ""))
            return llm.LLMResult(text="", usage=usage, parsed={
                "plan_grade": "B", "lessons": [], "lesson_updates": [],
                "pick_reviews": [{"pick_id": pid, "ticker": "TEST", "grade": "B", "why": "ok",
                                  "hypothesis_review": {"expressed": True, "note": "a clean test"}}],
                "hypotheses": [{**IDEA, "statement":
                                "shorting a sympathy name after the leader fades works midday",
                                "origin": "the leader faded and the follower followed"}]})

    monkeypatch.setattr("agent.loops.evening.send_email", lambda cfg, subj, html: sent.append(html))
    monkeypatch.setattr("agent.loops.evening.render_dashboard", lambda *a, **k: None)
    monkeypatch.setattr(evening, "LLMClient", _Client)
    monkeypatch.setattr(llm, "LLMClient", _Client)
    cfg = load_config()
    cfg.raw = {**cfg.raw, "traders_log": {"enabled": False},
               "persona": {"enabled": False, "version": 1}, "hypothesis_lane": dict(LANE)}
    cfg.mock, cfg.offline = False, True
    evening.run(cfg, run_date=D, force=True)

    con = ledger.connect()
    try:
        reviews = con.execute("SELECT pick_id, hyp_id, expressed FROM hypothesis_reviews").fetchall()
        rows = {r["hyp_id"]: r for r in hyp.all_hypotheses(con)}
    finally:
        con.close()
    assert reviews == [(pid, hid, True)]
    new = [r for h, r in rows.items() if h != hid]
    assert len(new) == 1 and new[0]["source"] == "grader" and str(new[0]["opened_at"]) == "2026-10-05"
    assert "HYPOTHESIS TESTS TODAY" in grader_systems[0] and "hypothesis_review" in grader_systems[0]
    assert sent and "HYPOTHESES (the trader's notebook" in sent[0]


def test_morning_email_names_the_idea_each_test_trades():
    from agent.delivery import morning_email_html
    pick = {**_cand("BBB", "gap_fade"), "size_r": 0.25, "exploration_lane": "hypothesis",
            "hypothesis_id": "H-1a2b3c4d"}
    html = morning_email_html(MON, {"no_trade": False, "picks": [pick], "hypotheses": {
        "opened": [{"hyp_id": "H-1a2b3c4d", "statement": "fades <work>", "refutation": "they lose"}]}},
        "REGIME")
    assert "(tests H-1a2b3c4d)" in html and "1 hypothesis test:" in html
    assert "New idea written down" in html and "&lt;work&gt;" in html
    plain = morning_email_html(MON, {"no_trade": False, "picks": [_cand("AAA", "gap_go")]}, "R")
    assert "hypothesis" not in plain and "New idea" not in plain
