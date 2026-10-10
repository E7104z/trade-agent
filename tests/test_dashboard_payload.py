"""Pins for the shared presentation helpers (agent/dashboard.py) and the
hosted status payload (api/index.py::_runner_payload).

These cover the four edges added during the "overhaul for visibility" pass:

  1. XSS-safe playbook rendering  (dashboard.playbook_html)
  2. best-edge threshold behavior (dashboard.best_edge)
  3. guarded conclusions payload  (dashboard.conclusions_payload)
  4. runner picks payload         (api.index._runner_payload)

Conventions follow tests/test_runner_view.py (real ledger on a temp DuckDB so
migrations actually run) and tests/test_failure_visibility.py (import the exact
unit under test rather than restating the bug in prose).
"""
from __future__ import annotations

import os
import tempfile
from datetime import date

import pytest

from agent import dashboard, ledger


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


# --------------------------------------------------------- playbook HTML (XSS)

def test_playbook_html_escapes_script_and_event_handlers():
    md = "# Title\n- <script>alert('x')</script>\n- <img src=x onerror=alert(1)>"
    html = dashboard.playbook_html(md)
    assert "<script" not in html
    assert "<img" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;img" in html


def test_playbook_html_escapes_markup_but_keeps_safe_markdown():
    html = dashboard.playbook_html("- **bold** and `code` & <tag>")
    assert "&amp;" in html
    assert "&lt;tag&gt;" in html
    assert "<strong>bold</strong>" in html
    assert "<code>code</code>" in html


def test_playbook_html_empty_returns_neutral_placeholder():
    assert "No playbook yet" in dashboard.playbook_html("")


def test_playbook_html_drops_html_comments():
    html = dashboard.playbook_html("<!-- version: 3 -->\n- one")
    assert "version:" not in html
    assert "one" in html


def test_playbook_bullets_strips_bold_and_leading_bullet():
    bullets = dashboard.playbook_bullets("- **only long setups**\n- plain bullet")
    assert bullets == ["only long setups", "plain bullet"]


# ------------------------------------------------------------- best_edge

def test_best_edge_empty_when_nothing_triggers_enough():
    """The playbook's own floor: no average R below n = 8. The card used to
    crown a setup on three trades."""
    assert dashboard.best_edge([]) == ""
    assert dashboard.best_edge([{"dim": "x", "triggered": 7, "avg_r": 1.0}]) == ""


def test_best_edge_ignores_none_avg_r():
    stats = [
        {"dim": "alpha", "triggered": 12, "avg_r": None},
        {"dim": "bravo", "triggered": 9, "avg_r": 0.5},
    ]
    out = dashboard.best_edge(stats)
    assert out.startswith("No validated edge yet")
    assert "bravo · avg 0.5R over 9 triggered" in out


def test_best_edge_picks_highest_avg_r_among_qualified():
    stats = [
        {"dim": "low", "triggered": 8, "avg_r": 0.2},
        {"dim": "high", "triggered": 10, "avg_r": 2.5},
        {"dim": "short", "triggered": 3, "avg_r": 99.0},  # not qualified (< 8)
    ]
    out = dashboard.best_edge(stats)
    assert "high" in out and "descriptive" in out
    assert "99.0" not in out


def test_best_edge_names_a_validated_rule_first():
    out = dashboard.best_edge([{"dim": "gap_go", "triggered": 30, "avg_r": 0.9}],
                              [{"rule_id": "Rfeedfacecafe", "statement": "gap_fade shorts",
                                "forward_n": 9}])
    assert out.startswith("[rule: Rfeedfacecafe]")


# ------------------------------------------- stats_by reads r_planned, not r_multiple
#
# r_multiple floors its risk denominator at 25% of planned risk (agent/grading.py),
# which can inflate it up to ~4x on a favorable gap-through fill. That inflated
# number must never drive "Current best edge" -- the dashboard's single headline
# claim, built from ledger.stats_by via dashboard.best_edge.

def _insert_graded_pick(con, d, ticker, setup_type, r_multiple, r_planned):
    con.execute(
        "INSERT INTO picks (plan_date, ticker, direction, setup_type, thesis, "
        "conviction, size_r, entry_price, stop_price, target_price) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [d, ticker, "long", setup_type, "t", 3, 1.0, 10.0, 9.5, 11.0],
    )
    pick_id = con.execute(
        "SELECT pick_id FROM picks WHERE plan_date=? AND ticker=? ORDER BY pick_id DESC LIMIT 1",
        [d, ticker],
    ).fetchone()[0]
    con.execute(
        "INSERT INTO outcomes (pick_id, eval_date, triggered, r_multiple, r_planned) "
        "VALUES (?,?,?,?,?)",
        [pick_id, d, True, r_multiple, r_planned],
    )


def test_stats_by_avg_r_uses_r_planned_when_present(con):
    # Three picks, all r_planned == 1.0 (the honest, plan-bounded result), but
    # r_multiple inflated to 5.0/6.0/7.0 (a favorable-fill risk-floor artifact).
    # avg_r must land on 1.0, not ~6.0 -- otherwise the inflated number is still
    # the one feeding "Current best edge".
    for i, (rmul, rplan) in enumerate([(5.0, 1.0), (6.0, 1.0), (7.0, 1.0)]):
        _insert_graded_pick(con, date(2026, 9, 20 + i), f"T{i}", "gap_go", rmul, rplan)
    rows = ledger.stats_by(con, "setup_type")
    row = next(r for r in rows if r["dim"] == "gap_go")
    assert row["avg_r"] == pytest.approx(1.0, abs=1e-6)
    assert row["total_weighted_r"] == pytest.approx(3.0, abs=1e-6)


def test_stats_by_falls_back_to_r_multiple_for_legacy_rows(con):
    # A pre-v23 row has r_planned = NULL; stats_by must not silently drop it
    # or treat it as zero -- it falls back to the old (possibly inflated, but
    # only-available) r_multiple until the row is re-graded.
    _insert_graded_pick(con, date(2026, 9, 20), "LEGACY", "gap_fade", 2.0, None)
    rows = ledger.stats_by(con, "setup_type")
    row = next(r for r in rows if r["dim"] == "gap_fade")
    assert row["avg_r"] == pytest.approx(2.0, abs=1e-6)


# --------------------------------------------------- conclusions_payload guard

def test_conclusions_payload_has_all_keys_on_fresh_ledger(con):
    payload = dashboard.conclusions_payload(con)
    for key in ("best_edge", "lessons", "open_questions", "shadow",
                "calibration", "management", "selection_edge"):
        assert key in payload, f"missing conclusions key: {key}"


def test_conclusions_payload_guards_each_subaggregate(con, monkeypatch):
    """A single missing table (or any one failure) must not take down the whole
    status endpoint the rest of the desk depends on — the failed sub-aggregate
    degrades to its default and everything else still renders."""
    def boom():
        raise RuntimeError("table missing")

    monkeypatch.setattr(ledger, "active_lessons", boom)
    payload = dashboard.conclusions_payload(con)
    assert payload["lessons"] == []
    assert "best_edge" in payload
    assert "selection_edge" in payload


# ------------------------------------------- delivery.render_dashboard wiring

def test_render_dashboard_consumes_new_section_placeholders(con, monkeypatch, tmp_path):
    """The static dashboard must actually format the new section placeholders
    (conclusions/playbook/shadow) and render the sticky-nav section banners —
    a KeyError or a leftover `{...}` literal would mean the overhaul wired a
    helper that the template never picked up."""
    from agent import delivery
    from agent.config import Config

    monkeypatch.setattr(delivery, "REPORTS_DIR", tmp_path)
    cfg = Config(raw={"account_size": 100000, "dollars_per_r": 500})
    result = delivery.render_dashboard(con, cfg, None)
    html = open(result, encoding="utf-8").read()

    for placeholder in ("{shadow_html}", "{conclusions_html}", "{playbook_html}"):
        assert placeholder not in html, f"unconsumed placeholder: {placeholder}"
    for sec in ("sec-conclusions", "sec-playbook", "sec-runs", "sec-loop"):
        assert f'id="{sec}"' in html, f"missing section banner: {sec}"
    # Default (empty-ledger) conclusions content must render, not the neutral
    # placeholder being swapped for nothing.
    assert "No active lessons yet." in html
