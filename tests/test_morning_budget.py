"""
specs/MORNING_BUDGET_SPEC_v1.md -- the morning in two invocations.

The analyst half ends at the candidate quotes and, hosted, hands the risk half
to a fresh invocation through a checkpoint row; the safety-net cron resumes a
checkpoint the hand-off never reached. Locally both halves run in one process
through the same JSON payload. Also: the evening and dashboard read what each
half left behind.
"""
from __future__ import annotations

from datetime import date, datetime

import duckdb
import pytest

from agent import dispatch, ledger
from agent.config import Config
from agent.loops import morning

D = date(2026, 10, 6)


def _root() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(ledger._SCHEMA)
    for mig in ledger._MIGRATIONS:
        con.execute(mig)
    return con


def _cfg(paired: bool = False) -> Config:
    raw = {"models": {"morning": "test-model"},
           "paired": {"enabled": paired, "every_n_sessions": 1,
                      "arm_b_context_mode": "aggregate_only"},
           "max_picks": 4, "min_size_r": 0.25, "max_size_r": 1.5,
           "max_total_open_risk_r": 3.0, "drawdown_window": 10,
           "account_size": 100000, "dollars_per_r": 500}
    return Config(raw=raw, mock=False, offline=True)


def _pick(ticker="AAAA"):
    return {"ticker": ticker, "direction": "long", "setup_type": "gap_go",
            "entry_plan": {"type": "open", "price": 100.0}, "entry_price": 100.0,
            "stop_price": 98.0, "target_price": 104.0, "size_r": 1.0,
            "conviction": 4, "thesis": "t"}


class _Res:
    def __init__(self, parsed):
        self.parsed = parsed
        self.usage = {"input_tokens": 10, "output_tokens": 10, "web_searches": 0, "cost_usd": 0.1}


class _MD:
    def __init__(self, cfg):
        pass

    def morning_bundle(self, d):
        return {"date": d, "premarket_gappers": [], "movers": {}, "catalyst_quotes": [],
                "missing": []}

    def pick_quotes(self, missing, d):
        return {t: 100.0 for t in missing}

    def daily_context(self, missing):
        return {}

    def next_earnings(self, missing):
        return {}


@pytest.fixture
def env(monkeypatch):
    root = _root()
    calls: list[str] = []

    def _call(*, system, user, model, mock_name, thinking=False, web_search=False, **kw):
        calls.append(mock_name + (":B" if "ABLATED" in user else ""))
        if mock_name == "morning_analyst":
            return _Res({"candidates": [_pick()]})
        return _Res({"no_trade": False, "picks": [_pick()]})

    class _LLM:
        def __init__(self, cfg):
            pass
        call = staticmethod(_call)

    for name, fn in {
        "MarketData": _MD, "LLMClient": _LLM,
        "render_bundle_md": lambda b: "BUNDLE",
        "compute_regime": lambda cfg, d: {"date": d, "tag": "neutral"},
        "regime_md": lambda reg: "REGIME",
        "build_morning_memory": lambda con, cfg: {
            "text": "## YOUR LAST 20 GRADED TRADES\n\nnone\n", "playbook_version": "1"},
        "load_prompt": lambda name, repl: f"SYSTEM:{name}",
        "send_email": lambda cfg, subject, html: False,
        "render_dashboard": lambda con, cfg, d: "",
    }.items():
        monkeypatch.setattr(morning, name, fn)
    monkeypatch.setattr(morning.ledger, "connect", root.cursor)
    return {"root": root, "calls": calls}


def _hosted(monkeypatch, ok=True):
    fired: list[tuple] = []
    monkeypatch.setattr(dispatch, "available", lambda cfg=None: True)

    def _fire(path, params=None, wait_s=3.0, cfg=None):
        fired.append((path, dict(params or {})))
        return {"ok": ok, "how": "dispatched" if ok else "ConnectTimeout: test"}
    monkeypatch.setattr(dispatch, "fire", _fire)
    return fired


def _n(root, sql, params=None):
    return root.execute(sql, params or []).fetchone()[0]


# ---------------------------------------------------------------- hosted path

def test_hosted_analyst_half_saves_a_checkpoint_and_hands_off(env, monkeypatch):
    fired = _hosted(monkeypatch)
    morning.run(_cfg(), run_date=D, force=True)
    root = env["root"]
    assert env["calls"] == ["morning_analyst"]                  # the risk half did not run here
    assert _n(root, "SELECT COUNT(*) FROM picks") == 0
    stage, dispatched = root.execute(
        "SELECT stage, dispatch FROM morning_checkpoints WHERE trading_date = ?", [D]).fetchone()
    assert stage == "analyst_done" and dispatched.startswith("http")
    lock = ledger.run_lock(root, D, "morning")
    assert lock["status"] == "in_progress"                      # held for the risk half
    assert fired == [("/cron/morning-continue", {"d": D.isoformat(), "rid": lock["request_id"]})]


def test_the_dispatched_risk_half_finishes_the_morning(env, monkeypatch):
    _hosted(monkeypatch)
    morning.run(_cfg(), run_date=D, force=True)
    root = env["root"]
    rid = ledger.run_lock(root, D, "morning")["request_id"]
    out = morning.resume(_cfg(), run_date=D, request_id=rid)
    assert out == {"resumed": True, "how": "dispatched"}
    assert env["calls"] == ["morning_analyst", "morning_risk"]
    assert _n(root, "SELECT COUNT(*) FROM picks") == 1
    assert ledger.run_lock(root, D, "morning")["status"] == "complete"
    cp = root.execute("SELECT stage, payload_json, completed_at FROM morning_checkpoints").fetchone()
    assert cp[0] == "complete" and cp[1] is None and cp[2] is not None
    import json
    stages = json.loads(_n(root, "SELECT stages FROM runs WHERE loop = 'morning' AND stages IS NOT NULL"))
    assert set(stages["invocations"]) == {"analyst_half", "risk_half"}
    assert stages["timings_s"]["total"] == max(stages["invocations"].values())
    assert {"analyst", "risk", "persist"} <= set(stages["timings_s"])


def test_a_second_dispatch_finds_nothing_to_take(env, monkeypatch):
    _hosted(monkeypatch)
    morning.run(_cfg(), run_date=D, force=True)
    rid = ledger.run_lock(env["root"], D, "morning")["request_id"]
    morning.resume(_cfg(), run_date=D, request_id=rid)
    assert morning.resume(_cfg(), run_date=D, request_id=rid)["resumed"] is False
    assert _n(env["root"], "SELECT COUNT(*) FROM picks") == 1


def test_a_failed_hand_off_runs_the_risk_half_here_when_the_budget_allows(env, monkeypatch):
    _hosted(monkeypatch, ok=False)
    morning.run(_cfg(), run_date=D, force=True)
    root = env["root"]
    assert _n(root, "SELECT COUNT(*) FROM picks") == 1
    stage, dispatched = root.execute("SELECT stage, dispatch FROM morning_checkpoints").fetchone()
    assert stage == "complete" and dispatched.startswith("FAILED")
    assert ledger.run_lock(root, D, "morning")["status"] == "complete"


def test_the_safety_net_cron_resumes_a_checkpoint_nobody_took(env, monkeypatch):
    _hosted(monkeypatch)
    morning.run(_cfg(), run_date=D, force=True)
    out = morning.resume(_cfg(), run_date=D)                   # the cron: no request id
    assert out["resumed"] is True and out["how"] == "safety-net cron"
    assert _n(env["root"], "SELECT COUNT(*) FROM picks") == 1


def test_a_dead_risk_half_is_retaken_but_a_saved_plan_is_never_redone(env, monkeypatch):
    _hosted(monkeypatch)
    morning.run(_cfg(), run_date=D, force=True)
    root = env["root"]
    rid = ledger.run_lock(root, D, "morning")["request_id"]
    morning.resume(_cfg(), run_date=D, request_id=rid)
    # Pretend that risk half died after saving the plan: claimed long ago,
    # never marked complete, lock still held.
    root.execute("""UPDATE morning_checkpoints SET stage = 'risk_running',
                    claimed_at = current_timestamp - INTERVAL '11 minutes'""")
    root.execute("UPDATE run_locks SET status = 'in_progress'")
    out = morning.resume(_cfg(), run_date=D)
    assert out == {"resumed": False, "reason": "plan already saved"}
    assert _n(root, "SELECT COUNT(*) FROM picks") == 1          # no second set of picks
    assert ledger.run_lock(root, D, "morning")["status"] == "complete"


def test_a_checkpoint_whose_run_lost_its_lock_is_superseded(env, monkeypatch):
    _hosted(monkeypatch)
    morning.run(_cfg(), run_date=D, force=True)
    env["root"].execute("UPDATE run_locks SET request_id = 'someone-else'")
    assert morning.resume(_cfg(), run_date=D)["reason"] == "superseded"
    assert _n(env["root"], "SELECT stage FROM morning_checkpoints") == "superseded"
    assert _n(env["root"], "SELECT COUNT(*) FROM picks") == 0


def test_the_safety_net_reruns_a_morning_that_left_no_plan_and_no_checkpoint(env, monkeypatch):
    # Local (no hand-off available): the rerun is one in-process morning.
    out = morning.resume(_cfg(), run_date=D)
    assert out == {"resumed": "full_rerun"}
    assert _n(env["root"], "SELECT COUNT(*) FROM picks") == 1


def test_the_paired_arm_runs_beside_each_half(env, monkeypatch):
    _hosted(monkeypatch)
    morning.run(_cfg(paired=True), run_date=D, force=True)
    root = env["root"]
    assert sorted(env["calls"]) == ["morning_analyst", "morning_analyst:B"]
    rid = ledger.run_lock(root, D, "morning")["request_id"]
    morning.resume(_cfg(paired=True), run_date=D, request_id=rid)
    assert sorted(env["calls"]) == ["morning_analyst", "morning_analyst:B",
                                    "morning_risk", "morning_risk:B"]
    assert _n(root, "SELECT COUNT(*) FROM paired_arms WHERE arm = 'B'") == 1
    assert _n(root, "SELECT arm_b_status FROM morning_checkpoints") == "complete"


# ----------------------------------------------------------------- local path

def test_locally_both_halves_run_in_one_process_without_a_checkpoint(env):
    morning.run(_cfg(), run_date=D, force=True)
    root = env["root"]
    assert env["calls"] == ["morning_analyst", "morning_risk"]
    assert _n(root, "SELECT COUNT(*) FROM picks") == 1
    assert _n(root, "SELECT COUNT(*) FROM morning_checkpoints") == 0
    assert ledger.run_lock(root, D, "morning")["status"] == "complete"


def test_a_failing_risk_half_releases_the_lock_as_failed(env, monkeypatch):
    def _boom(*, mock_name, **kw):
        if mock_name == "morning_risk":
            raise RuntimeError("risk manager down")
        return _Res({"candidates": [_pick()]})

    class _LLM:
        def __init__(self, cfg):
            pass
        call = staticmethod(_boom)
    monkeypatch.setattr(morning, "LLMClient", _LLM)
    _hosted(monkeypatch)
    morning.run(_cfg(), run_date=D, force=True)
    rid = ledger.run_lock(env["root"], D, "morning")["request_id"]
    with pytest.raises(morning._RiskHalfFailed):
        morning.resume(_cfg(), run_date=D, request_id=rid)
    assert ledger.run_lock(env["root"], D, "morning")["status"] == "failed"
    assert _n(env["root"], "SELECT stage FROM morning_checkpoints") == "failed"


# --------------------------------------------------------------- checkpoints

def test_checkpoint_claims_are_exclusive_until_the_holder_is_stale():
    con = _root()
    ledger.save_morning_checkpoint(con, D, "r1", {"x": 1, "when": datetime(2026, 10, 6, 7, 0)})
    assert ledger.claim_morning_checkpoint(con, D, "wrong-id") is None
    first = ledger.claim_morning_checkpoint(con, D, "r1")
    assert first["payload"]["x"] == 1 and first["attempts"] == 1
    assert ledger.claim_morning_checkpoint(con, D) is None       # held, fresh
    con.execute("UPDATE morning_checkpoints SET claimed_at = current_timestamp - INTERVAL '11 minutes'")
    assert ledger.claim_morning_checkpoint(con, D)["attempts"] == 2


def test_dispatch_is_only_available_on_the_hosted_deployment(monkeypatch):
    for k in ("VERCEL", "CRON_SECRET", "VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL",
              "TRADE_AGENT_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    assert dispatch.available() is False
    assert dispatch.fire("/x")["ok"] is False
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("CRON_SECRET", "s")
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "iwilltrade.com")
    assert dispatch.available() is True and dispatch.base_url() == "https://iwilltrade.com"
    monkeypatch.delenv("VERCEL_PROJECT_PRODUCTION_URL")
    monkeypatch.setenv("VERCEL_URL", "trade-agent-abc.vercel.app")
    cfg = Config(raw={"public_base_url": "https://iwilltrade.com"})
    assert dispatch.base_url(cfg) == "https://iwilltrade.com"      # before a protected URL
    assert dispatch.base_url() == "https://trade-agent-abc.vercel.app"


def test_dispatch_reads_a_read_timeout_as_delivered(monkeypatch):
    import requests
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("CRON_SECRET", "s")
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "example.test")
    seen = {}

    def _get(url, params=None, timeout=None, headers=None):
        seen.update(url=url, params=params, auth=headers["Authorization"])
        raise requests.exceptions.ReadTimeout("still working")
    monkeypatch.setattr(requests, "get", _get)
    assert dispatch.fire("/cron/morning-continue", {"rid": "r"}) == {"ok": True, "how": "dispatched"}
    assert seen == {"url": "https://example.test/cron/morning-continue", "params": {"rid": "r"},
                    "auth": "Bearer s"}


def test_the_continue_route_and_its_cron_exist():
    import json
    from pathlib import Path
    from api import index
    crons = json.loads((Path(__file__).resolve().parent.parent / "vercel.json").read_text())["crons"]
    assert {"path": "/cron/morning-continue", "schedule": "15 12 * * 1-5"} in crons
    assert index._CRON_LOOP["morning-continue"] == "morning-continue"
    assert index._CRON_WINDOWS["morning-continue"] == (5, 10)
