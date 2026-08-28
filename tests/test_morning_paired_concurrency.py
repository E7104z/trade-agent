"""
Regression test for the morning-loop 504/FUNCTION_INVOCATION_TIMEOUT fix.

Root cause: with `paired.enabled: true`, every morning run (including manual
dashboard clicks) did FOUR sequential extended-thinking Claude calls -- arm A's
analyst + risk manager, then arm B's analyst + risk manager stacked after --
inside one synchronous Vercel function capped at maxDuration=300s. The fix
(agent/loops/morning.py) runs arm B on a background thread concurrently with
arm A instead of after it, so the request's critical path is ~2 sequential
LLM calls instead of ~4.

This test proves two things a plain "does it still pass" run can't:
  1. arm A and arm B's LLM calls actually OVERLAP in wall-clock time (not
     just that both eventually happen).
  2. a crashing arm B still leaves arm A's plan fully persisted -- the
     concurrency change must not weaken that existing safety guarantee.
"""
from __future__ import annotations

import threading
import time
from datetime import date

import duckdb
import pytest

from agent import ledger
from agent.config import Config
from agent.loops import morning


def _memory_con() -> duckdb.DuckDBPyConnection:
    """A fresh in-memory connection carrying the exact schema ledger.connect()
    would build for a local DuckDB file -- without touching the real project
    database on disk."""
    con = duckdb.connect(":memory:")
    con.execute(ledger._SCHEMA)
    for mig in ledger._MIGRATIONS:
        con.execute(mig)
    return con


def _cfg(**paired_overrides) -> Config:
    raw = {
        "models": {"morning": "test-model"},
        "paired": {"enabled": True, "every_n_sessions": 1,
                   "arm_b_context_mode": "aggregate_only", **paired_overrides},
        "max_picks": 4, "min_size_r": 0.25, "max_size_r": 1.5,
        "max_total_open_risk_r": 3.0, "drawdown_window": 10,
        "account_size": 100000, "dollars_per_r": 500,
    }
    return Config(raw=raw, mock=False, offline=True)


def _pick(ticker="AAAA"):
    return {"ticker": ticker, "direction": "long", "setup_type": "gap_go",
            "entry_plan": {"type": "open", "price": 100.0}, "entry_price": 100.0,
            "stop_price": 98.0, "target_price": 104.0, "size_r": 1.0,
            "conviction": 4, "thesis": "t"}


class _FakeLLMResult:
    def __init__(self, parsed, cost=0.1):
        self.parsed = parsed
        self.usage = {"input_tokens": 10, "output_tokens": 10,
                      "web_searches": 0, "cost_usd": cost}


class _FakeMarketData:
    def __init__(self, cfg):
        pass

    def morning_bundle(self, d):
        return {"premarket_gappers": [], "movers": {}, "catalyst_quotes": [], "missing": []}

    def pick_quotes(self, missing, d):
        return {t: 100.0 for t in missing}

    def daily_context(self, missing):
        return {}

    def next_earnings(self, missing):
        return {}


@pytest.fixture(autouse=True)
def _patch_dependencies(monkeypatch):
    """Replace everything in morning.py's dependency surface that would
    otherwise hit the network, an LLM API, email, or the real ledger file --
    except the real `_validate_plan`, `ledger.*`, and `paired.run_challenger_arm`
    logic under test."""
    monkeypatch.setattr(morning, "MarketData", _FakeMarketData)
    monkeypatch.setattr(morning, "render_bundle_md", lambda bundle: "BUNDLE")
    monkeypatch.setattr(morning, "compute_regime", lambda cfg, d: {"date": d, "tag": "neutral"})
    monkeypatch.setattr(morning, "regime_md", lambda reg: "REGIME")
    monkeypatch.setattr(morning, "build_morning_memory",
                        lambda con, cfg: {"text": "## YOUR LAST 20 GRADED TRADES\n\nnone\n",
                                          "playbook_version": "1"})
    monkeypatch.setattr(morning, "load_prompt", lambda name, repl: f"SYSTEM:{name}")
    monkeypatch.setattr(morning, "send_email", lambda cfg, subject, html: False)
    monkeypatch.setattr(morning, "render_dashboard", lambda con, cfg, d: "")
    monkeypatch.setattr(morning.ledger, "connect", _memory_con)


def test_arm_a_and_arm_b_llm_calls_overlap_in_time(monkeypatch):
    """The whole point of the fix: arm B must start before arm A finishes,
    not after. If this regresses back to sequential, this assertion fails."""
    calls: list[tuple[str, float, float]] = []
    lock = threading.Lock()

    def _call(*, system, user, model, mock_name, thinking=False, web_search=False, **kw):
        t0 = time.monotonic()
        time.sleep(0.2)
        t1 = time.monotonic()
        with lock:
            calls.append((mock_name, t0, t1))
        if mock_name == "morning_analyst":
            return _FakeLLMResult({"candidates": [_pick()]})
        return _FakeLLMResult({"no_trade": False, "picks": [_pick()]})

    class _FakeLLMClient:
        def __init__(self, cfg):
            pass
        call = staticmethod(_call)

    monkeypatch.setattr(morning, "LLMClient", _FakeLLMClient)

    morning.run(_cfg(), run_date=date(2026, 8, 27), force=True)

    # 4 calls total: arm A analyst+risk, arm B analyst+risk.
    assert len(calls) == 4

    analyst_starts = sorted(t0 for name, t0, t1 in calls if name == "morning_analyst")
    # arm A's analyst call and arm B's analyst call are independent (arm B's
    # risk call needs arm B's own analyst result, not arm A's) and are the
    # first thing each arm does -- so if they're running concurrently, both
    # should start within the same ~0.2s window, not 0.2s+ apart.
    assert len(analyst_starts) == 2
    assert analyst_starts[1] - analyst_starts[0] < 0.15, (
        "arm B's analyst call did not start until well after arm A's -- "
        "the loop regressed to running arm B sequentially after arm A")


def test_arm_a_still_persists_when_arm_b_crashes(monkeypatch):
    """Concurrency must not weaken the existing 'a broken challenger can never
    take down production' guarantee (see agent/loops/paired.py docstring).

    Both arms share the same analyst_system/risk_system prompt text, so which
    physical thread happens to run first is not a safe way to tell them apart
    (it's a race). Arm B's `user` text is reliably different, though: its
    context_mode="aggregate_only" ablation strips the recent-trades section
    (see paired.apply_context_mode), leaving an "ABLATED" marker arm A's
    input never has. Failing only on that marker fails arm B deterministically
    regardless of scheduling.
    """
    def _flaky_call(*, system, user, model, mock_name, thinking=False, web_search=False, **kw):
        if mock_name == "morning_analyst" and "ABLATED" in user:
            raise RuntimeError("simulated anthropic outage for arm B")
        if mock_name == "morning_analyst":
            return _FakeLLMResult({"candidates": [_pick()]})
        return _FakeLLMResult({"no_trade": False, "picks": [_pick()]})

    class _FakeLLMClient:
        def __init__(self, cfg):
            pass
        call = staticmethod(_flaky_call)

    monkeypatch.setattr(morning, "LLMClient", _FakeLLMClient)

    # Arm A and arm B each open their own connection via ledger.connect() (as
    # they do in production against separate Postgres connections). Here they
    # need to share one underlying in-memory database so this test can inspect
    # what both wrote afterward -- but each call must still return a distinct
    # connection OBJECT, or arm B's `finally: con2.close()` closes arm A's
    # connection out from under it too. DuckDB's `.cursor()` gives exactly
    # that: a separate handle onto the same database, safe to close
    # independently, and safe across threads.
    root = _memory_con()
    monkeypatch.setattr(morning.ledger, "connect", root.cursor)

    morning.run(_cfg(), run_date=date(2026, 8, 27), force=True)

    # arm A's plan reached the real picks table regardless of arm B's failure.
    assert root.execute("SELECT COUNT(*) FROM picks").fetchone()[0] == 1
    # arm B never wrote a (partial) row for the session it failed on.
    assert root.execute(
        "SELECT COUNT(*) FROM paired_arms WHERE plan_date = ?", [date(2026, 8, 27)]
    ).fetchone()[0] == 0
