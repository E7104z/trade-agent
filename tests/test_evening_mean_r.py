"""
The evening runner summary's "mean R" must quote r_planned -- the number the
weekly miner (COALESCE(o.r_planned, o.r_multiple)), the runner stats and the
exit-replay parity check all read -- not r_multiple, a different measure
(fill-to-stop risk, floored at 25% of plan). On 2026-09-26 the regrade log
printed the r_multiple means (-0.05..-0.47) while `run.py runner-exits` printed
the r_planned mean (+0.32, inflated by r_planned's since-fixed detection-price
basis): two unrelated "mean R"s for the same trades.

Pinned here by stubbing simulate_pick to a triggered outcome where the two
disagree, then asserting the summary's mean_r tracks r_planned.
"""
from __future__ import annotations

from datetime import date

import pytest

from agent import ledger
from agent.config import Config
from agent.loops import evening


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "t.duckdb"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = ledger.connect()
    yield c
    c.close()


class _MD:
    def daily_context(self, symbols):
        return {}

    def minute_bars(self, symbol, d):
        # One bar clears the no_bars branch; simulate_pick is stubbed below.
        return [{"t": None, "o": 100.0, "h": 100.0, "l": 100.0,
                 "c": 100.0, "v": 10_000}]


def test_runner_mean_r_uses_planned_risk_units(con, monkeypatch):
    # The two measures disagree in sign, so the summary can only match one.
    monkeypatch.setattr(evening, "simulate_pick", lambda *a, **k: {
        "triggered": True, "r_planned": -0.45, "r_multiple": 0.32,
    })
    monkeypatch.setattr(evening, "_replay_exits", lambda *a, **k: None)
    monkeypatch.setattr(ledger, "save_runner_candidate_outcome", lambda *a, **k: None)
    monkeypatch.setattr(ledger, "save_runner_exit_replays", lambda *a, **k: None)
    monkeypatch.setattr(ledger, "runner_candidates_for_date",
                        lambda c, d: [{"ticker": "AAA", "detection_price": 100.0,
                                       "stop_price": 99.0, "target_price": 102.0,
                                       "first_pass_et": None}])
    monkeypatch.setattr(ledger, "runner_session", lambda c, d: None)

    summary = evening._grade_runner_candidates(con, _MD(), date(2026, 9, 16),
                                               Config(raw={}))
    assert summary["triggered"] == 1
    assert summary["mean_r"] == -0.45
