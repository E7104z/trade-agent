"""
`run.py regrade-shadow` -- the repair for 2026-09-25, when a test run carrying
the production DATABASE_URL inserted two fixture rejections and stamped EVERY
rejection would_have_won +1.0R (and that evening's grader skipped the day's
real rejections because they already looked graded).

Replays that damage on a tmp ledger and pins the repair:
  - a dry run writes nothing
  - --apply deletes the named fixture rows and re-prices every rejection with
    the evening simulator (a win at its real R, a loss as a loss)
  - a missing Q14 (R:R band) note is added; one the evening already wrote is
    not duplicated
"""
from __future__ import annotations

import importlib.util
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import ledger, providers
from agent.config import Config
from agent.loops.evening import Q_RR_TEST_SHADOW

ET = timezone(timedelta(hours=-4))
A = date(2026, 9, 16)   # the session whose grading was pre-empted: no Q14 note yet
B = date(2026, 9, 17)   # a session the evening already graded: its Q14 note exists

_spec = importlib.util.spec_from_file_location(
    "run_cli", Path(__file__).resolve().parent.parent / "run.py")
run_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_cli)


def _bars(d: date, prices):
    t0 = datetime.combine(d, time(9, 30), tzinfo=ET)
    return [{"t": t0 + timedelta(minutes=i), "o": o, "h": h, "l": l, "c": c, "v": 10_000}
            for i, (o, h, l, c) in enumerate(prices)]


BARS = {
    # long breakout 100 / stop 98 / target 104: fills at 100, runs to target -> +2R
    ("WINR", A): _bars(A, [(99.5, 99.8, 99.4, 99.6), (99.6, 100.5, 99.5, 100.4),
                           (100.4, 104.5, 100.3, 104.2)]),
    # long breakout 50 / stop 49 / target 51.9 (R:R 1.9, in band): fills, stops -> -1R
    ("LOSR", A): _bars(A, [(49.8, 50.2, 49.7, 50.1), (50.1, 50.15, 48.8, 48.9)]),
    # long breakout 20 / stop 19 / target 21.9 (R:R 1.9, in band): fills, targets -> +1.9R
    ("BANDB", B): _bars(B, [(19.8, 20.2, 19.7, 20.1), (20.1, 22.0, 20.0, 21.95)]),
}


class _MD:
    def __init__(self, cfg):
        pass

    def prefetch_session(self, symbols, d, **kw):
        return {}

    def minute_bars(self, symbol, d):
        return BARS.get((symbol, d), [])


def _rej(ticker, entry, stop, target):
    return {"ticker": ticker, "direction": "long", "entry_type": "breakout",
            "reason": "R:R < 2.0", "entry_price": entry, "stop_price": stop,
            "target_price": target}


@pytest.fixture()
def damaged(tmp_path, monkeypatch):
    """The ledger as the 2026-09-25 test run left it."""
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "shadow.duckdb"))
    monkeypatch.setattr(providers, "MarketData", _MD)
    con = ledger.connect()
    ledger.save_rejections(con, A, [_rej("WINR", 100, 98, 104), _rej("LOSR", 50, 49, 51.9),
                                    {"ticker": "AAAA", "reason": "R:R 1.4 < 2.0",
                                     "entry_price": 10.0, "stop_price": 9.0,
                                     "target_price": 11.4}])
    ledger.save_rejections(con, B, [_rej("BANDB", 20, 19, 21.9)])
    ledger.add_question_progress(
        con, Q_RR_TEST_SHADOW, B, 1,
        "BANDB planned R:R=1.90 (1.8-2.0 band) would_have_won forgone=1.9R")
    for (rid,) in con.execute("SELECT rejection_id FROM rejections").fetchall():
        ledger.save_rejection_outcome(con, rid, "would_have_won", 1.0)
    fixture_id = con.execute(
        "SELECT rejection_id FROM rejections WHERE ticker = 'AAAA'").fetchone()[0]
    con.close()
    return fixture_id


def _run(fixture_id, apply, progress_for=None):
    run_cli.cmd_regrade_shadow(
        Config(raw={"slippage_bps": 0}),
        SimpleNamespace(from_date=str(A), delete_ids=str(fixture_id),
                        apply=apply, force=False, progress_for=progress_for))


def _outcomes():
    con = ledger.connect()
    try:
        return {t: (o, f) for t, o, f in con.execute(
            "SELECT ticker, eod_outcome, forgone_r FROM rejections").fetchall()}
    finally:
        con.close()


def test_dry_run_writes_nothing(damaged):
    _run(damaged, apply=False)
    assert _outcomes() == {t: ("would_have_won", 1.0)
                           for t in ("WINR", "LOSR", "AAAA", "BANDB")}


def test_apply_deletes_fixture_rows_and_reprices_every_rejection(damaged):
    _run(damaged, apply=True)
    out = _outcomes()
    assert "AAAA" not in out
    assert out["WINR"] == ("would_have_won", pytest.approx(2.0))
    assert out["LOSR"] == ("would_have_lost", pytest.approx(-1.0))
    assert out["BANDB"] == ("would_have_won", pytest.approx(1.9))


def test_find_test_rows_reports_fixtures_and_writes_nothing(damaged, capsys):
    con = ledger.connect()
    before = con.execute("SELECT COUNT(*) FROM rejections").fetchone()[0]
    ledger.save_rejections(con, date(2026, 8, 29), [{
        "ticker": "MARA", "reason": "R:R 1.83 < 2.0", "entry_price": 11.5,
        "stop_price": 10.9, "target_price": 12.6}])
    ledger.insert_lesson(con, {"lesson": "placeholder", "confidence": "low"},
                         "evening", date(2026, 8, 20))
    con.close()

    run_cli.cmd_find_test_rows(Config(raw={}), SimpleNamespace(since="2026-09-24"))
    out = capsys.readouterr().out
    assert "'AAAA'" in out and "'MARA'" in out and "'placeholder'" in out
    assert "2026-08-29" in out                          # a Saturday session
    con = ledger.connect()
    try:
        assert con.execute("SELECT COUNT(*) FROM rejections").fetchone()[0] == before + 1
    finally:
        con.close()


def test_apply_adds_only_the_missing_rr_band_notes(damaged):
    _run(damaged, apply=True, progress_for=f"{A},{B}")
    con = ledger.connect()
    try:
        notes = con.execute(
            "SELECT session_date, note FROM open_question_progress "
            "WHERE question_id = ? ORDER BY session_date", [Q_RR_TEST_SHADOW]).fetchall()
    finally:
        con.close()
    assert [(d, n.split()[0]) for d, n in notes] == [(A, "LOSR"), (B, "BANDB")]
    assert "would_have_lost" in notes[0][1]


def _q(sql, params=()):
    con = ledger.connect()
    try:
        return con.execute(sql, list(params)).fetchall()
    finally:
        con.close()


def test_no_progress_notes_are_added_unless_a_session_is_named(damaged):
    # A blanket backfill writes observations for sessions from before a
    # question was being recorded -- that is how the first version inflated
    # Q14. Re-pricing alone must leave open-question progress untouched.
    _run(damaged, apply=True)
    assert _q("SELECT session_date, note FROM open_question_progress") == [
        (B, "BANDB planned R:R=1.90 (1.8-2.0 band) would_have_won forgone=1.9R")]


def test_an_existing_colon_format_q1_note_is_not_doubled(damaged):
    from agent.loops.evening import Q_GAP_FADE_MECHANICS
    c = date(2026, 9, 18)
    BARS[("GAPF", c)] = _bars(c, [(50.6, 50.8, 50.2, 50.4), (50.4, 50.5, 48.3, 48.4)])
    try:
        con = ledger.connect()
        ledger.save_rejections(con, c, [{
            "ticker": "GAPF", "direction": "short", "setup_type": "gap_fade",
            "reason": "gap < 5%", "entry_type": "limit", "entry_price": 50.5,
            "stop_price": 51.5, "target_price": 48.5}])
        ledger.add_question_progress(con, Q_GAP_FADE_MECHANICS, c, 1,
                                     "GAPF: planned-entry R=2.0, open-print R=2.1")
        con.close()
        _run(damaged, apply=True, progress_for=str(c))
        notes = _q("SELECT note FROM open_question_progress WHERE question_id = ? "
                   "AND session_date = ?", [Q_GAP_FADE_MECHANICS, c])
        assert notes == [("GAPF: planned-entry R=2.0, open-print R=2.1",)]
    finally:
        BARS.pop(("GAPF", c), None)


# ------------------------------------------------ undo-progress-backfill

@pytest.fixture()
def over_backfilled(tmp_path, monkeypatch):
    """Progress as the first regrade-shadow left it: Q14 notes for sessions
    before Q14 was recorded, and every gap-fade rejection's Q1 note twice."""
    from agent.loops.evening import Q_GAP_FADE_MECHANICS
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "progress.duckdb"))
    con = ledger.connect()
    fade = {"direction": "short", "setup_type": "gap_fade", "reason": "r",
            "entry_type": "limit", "entry_price": 10.0, "stop_price": 10.5,
            "target_price": 9.0}
    ledger.save_rejections(con, date(2026, 9, 2), [{**fade, "ticker": "AON"}])
    ledger.save_rejections(con, date(2026, 9, 24), [{**fade, "ticker": "VKTX"}])
    add = ledger.add_question_progress
    add(con, Q_RR_TEST_SHADOW, date(2026, 8, 19), 1, "MRVL planned R:R=1.80 (1.8-2.0 band) x")
    add(con, Q_RR_TEST_SHADOW, date(2026, 9, 14), 1, "DELL planned R:R=1.80 (1.8-2.0 band) x")
    add(con, Q_RR_TEST_SHADOW, date(2026, 9, 17), 1, "GE planned R:R=1.83 (1.8-2.0 band) x")
    add(con, Q_RR_TEST_SHADOW, date(2026, 9, 25), 1, "GRAL planned R:R=1.86 (1.8-2.0 band) x")
    for _ in range(2):
        add(con, Q_GAP_FADE_MECHANICS, date(2026, 9, 2), 1, "AON: planned-entry R=untriggered, open-print R=0.5")
    add(con, Q_GAP_FADE_MECHANICS, date(2026, 9, 24), 1, "VKTX: planned-entry R=untriggered, open-print R=None")
    add(con, 3, date(2026, 9, 2), 1, "AON conv=2 R=+0.50")        # another question: untouched
    con.close()


def _undo(apply):
    run_cli.cmd_undo_progress_backfill(
        Config(raw={}), SimpleNamespace(q14_from="2026-09-17", apply=apply))


def _progress():
    return sorted((q, d, n) for q, d, n in _q(
        "SELECT question_id, session_date, note FROM open_question_progress"))


def test_undo_dry_run_writes_nothing(over_backfilled):
    before = _progress()
    _undo(apply=False)
    assert _progress() == before


def test_undo_drops_pre_recording_q14_and_doubled_q1_only(over_backfilled):
    _undo(apply=True)
    assert _progress() == [
        (1, date(2026, 9, 2), "AON: planned-entry R=untriggered, open-print R=0.5"),
        (1, date(2026, 9, 24), "VKTX: planned-entry R=untriggered, open-print R=None"),
        (3, date(2026, 9, 2), "AON conv=2 R=+0.50"),
        (Q_RR_TEST_SHADOW, date(2026, 9, 17), "GE planned R:R=1.83 (1.8-2.0 band) x"),
        (Q_RR_TEST_SHADOW, date(2026, 9, 25), "GRAL planned R:R=1.86 (1.8-2.0 band) x"),
    ]
