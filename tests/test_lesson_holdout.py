"""
Tests for Task 5: out-of-sample holdout for lesson confirmation.

Uses a scratch DuckDB file (never data/ledger.duckdb) by monkeypatching
agent.ledger.DB_PATH directly -- ledger.py does `from .config import DB_PATH`,
so patching agent.config.DB_PATH alone would NOT be seen by ledger.connect()
once agent.ledger has already been imported (its own name binding is frozen
at import time). See the `con` fixture below.
"""
from __future__ import annotations

from datetime import date

import pytest

from agent import ledger


@pytest.fixture()
def con(tmp_path, monkeypatch):
    # ledger.py does `from .config import DB_PATH`, binding its OWN name at
    # import time -- patching agent.config.DB_PATH alone would NOT affect
    # agent.ledger.DB_PATH (already-imported modules keep their own binding).
    # Patch the name actually used by ledger.connect().
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "ledger.duckdb")
    c = ledger.connect()
    yield c
    c.close()


def _seed_lesson(con, promoted_at: date) -> int:
    lid = ledger.insert_lesson(
        con, {"lesson": "test lesson", "setup_type": None, "regime": None,
             "tags": [], "confidence": "low", "evidence": "seed"},
        source="evening", created=promoted_at,
    )
    return lid


def _confirm(con, lesson_id: int, session_date: date, n: int = 1) -> None:
    for _ in range(n):
        ledger.update_lesson_feedback(con, lesson_id, "confirm", session_date)


# --------------------------------------------------------------- tier function

def test_lesson_confidence_tier_bands_from_pure_counts():
    assert ledger.lesson_confidence_tier(0, 0) == "none"
    assert ledger.lesson_confidence_tier(1, 0) == "provisional"
    assert ledger.lesson_confidence_tier(7, 0) == "provisional"
    assert ledger.lesson_confidence_tier(8, 0) == "low"
    assert ledger.lesson_confidence_tier(19, 0) == "low"


def test_lesson_confidence_tier_in_sample_alone_cannot_exceed_low():
    # even a huge in-sample count, with zero out-of-sample, stays capped at low
    assert ledger.lesson_confidence_tier(1000, 0) == "low"


def test_lesson_confidence_tier_out_of_sample_unlocks_moderate_and_established():
    assert ledger.lesson_confidence_tier(9, 0) == "low"
    assert ledger.lesson_confidence_tier(9, 8) == "moderate"
    assert ledger.lesson_confidence_tier(0, 20) == "established"


# --------------------------------------------------------- promoted_at + DB split

def test_promoted_at_backfilled_on_insert(con):
    d = date(2026, 1, 10)
    lid = _seed_lesson(con, d)
    row = con.execute("SELECT promoted_at FROM lessons WHERE lesson_id = ?", [lid]).fetchone()
    assert row[0] == d


def test_nine_confirmations_predating_promotion_cannot_exceed_low(con):
    """Acceptance case 1: a lesson with 9 confirmations all predating its
    promoted_at cannot exceed low."""
    promoted_at = date(2026, 1, 10)
    lid = _seed_lesson(con, promoted_at)
    # 9 confirmations, all on/before promoted_at (in-sample)
    for i in range(9):
        _confirm(con, lid, date(2026, 1, 1) + __import__("datetime").timedelta(days=i))

    stats = ledger.lesson_holdout_stats(con, lid, promoted_at)
    assert stats["confirmations_in_sample"] == 9
    assert stats["confirmations_out_of_sample"] == 0
    assert stats["tier"] == "low"


def test_eight_more_confirmations_postdating_promotion_reaches_moderate(con):
    """Acceptance case 2: the SAME lesson, with 8 confirmations postdating
    promotion, reaches moderate."""
    promoted_at = date(2026, 1, 10)
    lid = _seed_lesson(con, promoted_at)
    for i in range(9):
        _confirm(con, lid, date(2026, 1, 1) + __import__("datetime").timedelta(days=i))
    for i in range(8):
        _confirm(con, lid, date(2026, 1, 15) + __import__("datetime").timedelta(days=i))

    stats = ledger.lesson_holdout_stats(con, lid, promoted_at)
    assert stats["confirmations_in_sample"] == 9
    assert stats["confirmations_out_of_sample"] == 8
    assert stats["tier"] == "moderate"


def test_holdout_sessions_zero_is_a_no_op(con):
    """With lesson_holdout_sessions = 0 (the default), lesson_holdout_stats
    must behave exactly as if the trailing-window mechanism doesn't exist --
    only the promoted_at split applies."""
    promoted_at = date(2026, 1, 10)
    lid = _seed_lesson(con, promoted_at)
    _confirm(con, lid, date(2026, 1, 5))
    _confirm(con, lid, date(2026, 1, 20))

    without_holdout = ledger.lesson_holdout_stats(con, lid, promoted_at, holdout_sessions=0)
    assert without_holdout["confirmations_in_sample"] == 1
    assert without_holdout["confirmations_out_of_sample"] == 1


def test_holdout_sessions_withholds_trailing_window_entirely(con):
    """When lesson_holdout_sessions > 0, confirmations inside the most recent
    N distinct sessions are excluded from BOTH in-sample and out-of-sample
    counts -- the true withheld-window mechanism."""
    promoted_at = date(2026, 1, 10)
    lid = _seed_lesson(con, promoted_at)

    # seed a session universe: 5 distinct daily_plans dates
    session_dates = [date(2026, 1, d) for d in (5, 10, 15, 20, 25)]
    for sd in session_dates:
        con.execute(
            "INSERT INTO daily_plans (plan_date, no_trade, market_summary, plan_json, "
            "analyst_json, regime_tag, notes_for_evening) VALUES (?, FALSE, '', '{}', '{}', '', '')",
            [sd],
        )

    _confirm(con, lid, date(2026, 1, 5))    # in-sample, outside any holdout window
    _confirm(con, lid, date(2026, 1, 20))   # out-of-sample, but inside trailing-2 window
    _confirm(con, lid, date(2026, 1, 25))   # out-of-sample, inside trailing-2 window

    # trailing 2 sessions = {2026-01-20, 2026-01-25} -- both confirmations on
    # those dates should be withheld entirely, leaving only the 1/5 confirmation.
    stats = ledger.lesson_holdout_stats(con, lid, promoted_at, holdout_sessions=2)
    assert stats["confirmations_in_sample"] == 1
    assert stats["confirmations_out_of_sample"] == 0
