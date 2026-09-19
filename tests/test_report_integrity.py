"""
Report integrity — the fixes for a weekly report that stated conclusions its
ledger could not support.

The playbook it produced was honest at the top ("NO VALIDATED EDGE YET") and
quietly unsupportable underneath, in five distinct ways. Each has a test here:

  1. Verdicts at n=1. Selection edge and management edge were both rendered
     with directional readings over a single trade, inviting the reading that
     "picking is bad but management is good" -- a divergence between two
     numbers that cannot yet differ from anything.
  2. A sum reported as if it were a price. `net forgone R` is a sum over a
     fat-tailed quantity; three rows authored it while the other hundred-odd
     rejections cancelled out, and nothing computed that relationship.
  3. Participation invisible. One triggered trade against ~114 evaluated
     candidates is either correct patience or a filter stack that is
     collectively too tight, and no aggregate in the report distinguished them.
  4. Pre-registered thresholds that the weekly rewrite could restate, plus an
     id collision (playbook "#7" = R:R test, code Q7 = RVOL separation) that
     fed one experiment's observations to another.
  5. A calibration table exempt from the document's own n>=8 suppression rule,
     because the rule was enforced by a scanner that only understood prose.
"""
from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pytest

from agent import ledger, open_questions
from agent.ablation import MIN_VERDICT_PICKS, ablation_md, random_pick_ablation
from agent.loops.evening import _planned_rr
from agent.memory import _strip_changelog_sections
from agent.loops.weekly import (MIN_INTERPRETABLE_N, _calibration_md,
                                _check_calibration_table, _counterfactuals_md,
                                _participation_md, _shadow_md)

D = date(2026, 9, 5)


@pytest.fixture()
def con():
    """The exact schema ledger.connect() builds, in memory, without touching
    the project database on disk. Built from _SCHEMA + _MIGRATIONS rather than
    hand-rolled tables so a column added by a later migration (session_degraded,
    eod_r, baseline_r) is present here exactly as it is in production."""
    c = duckdb.connect(":memory:")
    c.execute(ledger._SCHEMA)
    for mig in ledger._MIGRATIONS:
        c.execute(mig)
    yield c
    c.close()


def _pick(con, pick_id, ticker, d=D):
    con.execute(
        "INSERT INTO picks (pick_id, plan_date, ticker, direction, size_r) "
        "VALUES (?,?,?, 'long', 1.0)", [pick_id, d, ticker])


def _reject(con, rid, ticker, forgone, outcome="would_have_won",
            reason="R:R < 2.0", d=D):
    con.execute(
        """INSERT INTO rejections (rejection_id, plan_date, ticker, reason,
                                   eod_outcome, forgone_r)
           VALUES (?,?,?,?,?,?)""", [rid, d, ticker, reason, outcome, forgone])


def _cal(con, conviction, r, i):
    con.execute(
        """INSERT INTO calibration (session_date, ticker, stated_conviction,
                                    realized_r, win) VALUES (?,?,?,?,?)""",
        [D, f"T{i}", conviction, r, r > 0])


# ----------------------------------------------------- 1. verdicts at low n

def test_counterfactual_readings_are_suppressed_at_one_trade():
    """Every average gets its own n, and below the floor it gets no direction.

    The failure this pins is not a wrong number -- the numbers were right. It
    is that +2.13 and -18.2 printed with readings attached invite a story
    about stop machinery outperforming selection, from one trade.
    """
    mem = {"management": {"triggered": 1,
                          "avg_r": -0.05, "avg_r_n": 1,
                          "avg_baseline_r": 0.4, "avg_baseline_r_n": 1,
                          "edge_vs_baseline_r": -0.45, "edge_vs_baseline_n": 1,
                          "management_edge_r": 2.13, "management_edge_n": 1,
                          "stopped": 1, "stopped_then_target": 0,
                          "avg_eod_r_on_stopped": -0.9,
                          "avg_eod_r_on_stopped_n": 1,
                          "untriggered_with_baseline": 0,
                          "avg_baseline_r_untriggered": None},
           "sizing_cf": {"n": 1, "actual_weighted_r": -0.05,
                         "flat_size_weighted_r": -0.05, "sizing_edge_r": 0.0}}
    md = _counterfactuals_md(mem)
    assert md.count("NOT INTERPRETABLE") >= 4      # every average, not just one
    assert "2.13 over n=1" in md                   # still shown
    assert "no divergence" in md                   # the trap is named


def test_counterfactual_readings_return_once_n_supports_them():
    n = MIN_INTERPRETABLE_N
    mem = {"management": {"triggered": n,
                          "avg_r": 0.5, "avg_r_n": n,
                          "avg_baseline_r": 0.1, "avg_baseline_r_n": n,
                          "edge_vs_baseline_r": 0.4, "edge_vs_baseline_n": n,
                          "management_edge_r": 0.3, "management_edge_n": n,
                          "stopped": n, "stopped_then_target": 2,
                          "avg_eod_r_on_stopped": -0.2,
                          "avg_eod_r_on_stopped_n": n,
                          "untriggered_with_baseline": 0,
                          "avg_baseline_r_untriggered": None},
           "sizing_cf": {"n": 0}}
    md = _counterfactuals_md(mem)
    assert "NOT INTERPRETABLE" not in md
    assert "destroying value vs just holding" in md


def test_each_counterfactual_average_carries_its_own_n(con):
    """Three triggered trades; only one has eod_r and only two a baseline.

    Before the fix a single `triggered` count sat beside four averages, so the
    largest n attached itself to whichever number the reader cared about.
    """
    for pid, (r, eod, base) in enumerate([(1.0, 0.5, 0.2), (0.5, None, 0.1),
                                          (-1.0, None, None)]):
        _pick(con, pid, f"T{pid}")
        con.execute(
            "INSERT INTO outcomes (pick_id, triggered, r_multiple, eod_r, baseline_r) "
            "VALUES (?, TRUE, ?, ?, ?)", [pid, r, eod, base])
    m = ledger.management_quality(con)
    assert m["triggered"] == 3
    assert m["avg_r_n"] == 3
    assert m["avg_baseline_r_n"] == 2
    assert m["management_edge_n"] == 1
    # paired, not a difference of two means over different row sets
    assert m["edge_vs_baseline_n"] == 2
    assert m["edge_vs_baseline_r"] == pytest.approx(((1.0 - 0.2) + (0.5 - 0.1)) / 2)


def test_ablation_withholds_its_verdict_over_one_session(con):
    _pick(con, 1, "AAAA")
    con.execute("INSERT INTO outcomes (pick_id, triggered, r_multiple, sim_version) "
                "VALUES (1, TRUE, -0.5, 2)")
    for i in range(6):
        _reject(con, i + 1, f"R{i}", 1.0)
    res = random_pick_ablation(con, draws=500)
    md = ablation_md(res)
    assert res["sessions"] == 1
    assert res["picks"] < MIN_VERDICT_PICKS
    assert "NO VERDICT" in md
    assert "actively destroying value" not in md
    assert f"beat {res['percentile']}% of random portfolios" in md   # still shown


# --------------------------------------------- 2. the sum and its three rows

def test_shadow_summary_computes_the_concentration_of_its_own_sum(con):
    """Three large winners, a hundred small savers: the arithmetic the
    headline hides is computed, not left to the reader's eye."""
    _reject(con, 1, "BIG1", 6.16)
    _reject(con, 2, "BIG2", 5.95)
    _reject(con, 3, "BIG3", 3.29)
    for i in range(110):
        _reject(con, 10 + i, f"S{i}", -0.122, outcome="would_have_lost")
    sh = ledger.shadow_summary(con)
    assert sh["scored_rows"] == 113
    assert sh["top3_forgone_r"] == pytest.approx(15.40, abs=0.01)
    assert sh["net_forgone_r"] == pytest.approx(1.98, abs=0.05)
    # the number that was missing: the rest of the book SAVED money
    assert sh["net_forgone_r_ex_top3"] < 0
    assert sh["median_forgone_r"] < 0
    assert [t["ticker"] for t in sh["top_rows"]] == ["BIG1", "BIG2", "BIG3"]


def test_shadow_md_leads_with_the_tail_artifact_when_the_signs_disagree(con):
    _reject(con, 1, "BIG1", 6.16)
    _reject(con, 2, "BIG2", 5.95)
    _reject(con, 3, "BIG3", 3.29)
    for i in range(110):
        _reject(con, 10 + i, f"S{i}", -0.122, outcome="would_have_lost")
    md = _shadow_md(ledger.shadow_summary(con))
    assert "TAIL ARTIFACT" in md
    assert "MEDIAN forgone R" in md
    assert "NET EXCLUDING the 3 largest" in md
    assert "not grounds for loosening anything" in md
    # and it names this run's rows, so last week's sentence cannot be reused
    assert "BIG1" in md


def test_forgone_r_is_grouped_by_category_not_by_verbatim_prose(con):
    """`reason` is the risk manager's free text, so grouping on it yields one
    group per rejection -- a list wearing a GROUP BY, from which Open Question
    #5 ("which reasons forgo the most R?") cannot be answered at all."""
    for i in range(4):
        con.execute(
            """INSERT INTO rejections (rejection_id, plan_date, ticker, reason,
                                       rejection_category, eod_outcome, forgone_r)
               VALUES (?,?,?,?,?,?,?)""",
            [i, D, f"T{i}", f"R:R = 1.{i}5 < 2.0, and a uniquely worded note {i}",
             "rr_below_control", "would_have_won", 1.0])
    con.execute(
        """INSERT INTO rejections (rejection_id, plan_date, ticker, reason,
                                   rejection_category, eod_outcome, forgone_r)
           VALUES (9,?, 'ZZZZ', 'stale catalyst', 'other', 'would_have_lost', -2.0)""",
        [D])
    sh = ledger.shadow_summary(con)
    assert len(sh["by_reason"]) == 5            # verbatim prose: one group each
    cats = {c["category"]: c for c in sh["by_category"]}
    assert cats["rr_below_control"]["n"] == 4
    assert cats["rr_below_control"]["net_forgone_r"] == pytest.approx(4.0)
    assert cats["other"]["net_forgone_r"] == pytest.approx(-2.0)


def test_uncategorized_rows_are_named_rather_than_folded_into_other(con):
    """Rows predating the category column are not evidence that no category
    applied, and `other` is a real category meaning "checked, not R:R"."""
    _reject(con, 1, "OLD", 3.0)                 # rejection_category IS NULL
    sh = ledger.shadow_summary(con)
    cats = {c["category"] for c in sh["by_category"]}
    assert cats == {"uncategorized"}


def test_shadow_md_names_the_category_table_as_the_answer_to_q5(con):
    _reject(con, 1, "OLD", 3.0)
    md = _shadow_md(ledger.shadow_summary(con))
    assert "BY REJECTION CATEGORY" in md
    assert "Open Question #5" in md
    assert "detail, not a grouping" in md


def test_shadow_md_caps_the_verbatim_kill_notes(con):
    for i in range(20):
        _reject(con, i + 1, f"T{i}", 0.1, reason=f"a very long verbatim note {i} " * 20)
    md = _shadow_md(ledger.shadow_summary(con))
    assert "further rows not shown" in md
    assert "Do not count lines in it" in md


def test_shadow_md_is_calm_when_the_sum_is_not_tail_driven(con):
    for i in range(30):
        _reject(con, i + 1, f"T{i}", 0.5)
    md = _shadow_md(ledger.shadow_summary(con))
    assert "TAIL ARTIFACT" not in md
    assert "CONCENTRATION WARNING" not in md
    assert "Top-3 contribution" in md


# ------------------------------------------------------- 3. the participation funnel

def test_participation_funnel_counts_the_whole_funnel(con):
    for i in range(15):
        con.execute("INSERT INTO daily_plans (plan_date) VALUES (?)",
                    [date(2026, 8, 17) + timedelta(days=i)])
    _pick(con, 1, "AAAA")
    con.execute("INSERT INTO outcomes (pick_id, triggered, r_multiple) "
                "VALUES (1, TRUE, -0.05)")
    for i in range(113):
        _reject(con, i + 1, f"R{i}", 0.0, outcome="scratch")
    pt = ledger.participation_funnel(con)
    assert pt["candidates_evaluated"] == 114
    assert pt["picks_taken"] == 1
    assert pt["picks_triggered"] == 1
    assert pt["candidates_per_triggered_trade"] == pytest.approx(114.0)
    md = _participation_md(pt)
    assert "STANDING QUESTION" in md
    assert "collectively too tight" in md


def test_participation_funnel_flags_a_desk_that_triggered_nothing(con):
    con.execute("INSERT INTO daily_plans (plan_date) VALUES (?)", [D])
    for i in range(20):
        _reject(con, i + 1, f"R{i}", 0.0, outcome="scratch")
    pt = ledger.participation_funnel(con)
    assert pt["picks_triggered"] == 0
    assert pt["candidates_per_triggered_trade"] is None
    md = _participation_md(pt)
    assert "triggered\nNOTHING" in md or "triggered" in md
    assert "uncomputable by construction" in md


def test_participation_funnel_excludes_degraded_sessions(con):
    con.execute("INSERT INTO daily_plans (plan_date, session_degraded) VALUES (?, TRUE)",
                [date(2026, 9, 1)])
    con.execute("INSERT INTO daily_plans (plan_date, session_degraded) VALUES (?, FALSE)",
                [date(2026, 9, 2)])
    pt = ledger.participation_funnel(con)
    assert pt["sessions_evaluated"] == 1   # the outage was not a decision


def test_participation_funnel_is_safe_on_an_empty_ledger(con):
    pt = ledger.participation_funnel(con)
    assert pt["sessions_evaluated"] == 0
    assert _participation_md(pt).startswith("(no non-degraded")


# ------------------------------------------- 4. the open-question registry

def test_registry_catches_a_threshold_that_moved():
    drifted = ("13. **R:R threshold test** Threshold: 15 observations per "
               "bucket. **Progress: ~12/15.**")
    v = open_questions.check_playbook_questions(drifted)
    assert len(v) == 1
    assert "states threshold 15" in v[0] and "registry declares 20" in v[0]


def test_registry_accepts_the_declared_threshold():
    ok = "13. **R:R threshold test** Threshold: 20 observations per bucket. **Progress: 7/20.**"
    assert open_questions.check_playbook_questions(ok) == []


def test_registry_catches_an_invented_question_id():
    v = open_questions.check_playbook_questions(
        "99. **new idea** Threshold: n >= 4. Progress: 0/4.")
    assert len(v) == 1 and "not in the registry" in v[0]


def test_the_rr_test_does_not_squat_on_the_rvol_question_id():
    """The collision that made "Q7" mean two experiments at once: the evening
    loop increments id 7 with RVOL observations, while the playbook read those
    increments as progress on an R:R threshold test."""
    assert open_questions.by_id(7)["slug"] == "rvol_separation"
    assert open_questions.by_id(13)["slug"] == "rr_threshold_test_taken"
    assert open_questions.by_id(14)["slug"] == "rr_threshold_test_shadow"


def test_the_rr_test_gets_one_counter_per_arm(con):
    """One counter cannot express "20 per bucket": a single total reaching 20
    says nothing about whether either arm can be compared to the other."""
    assert open_questions.by_id(13)["threshold"] == 20
    assert open_questions.by_id(14)["threshold"] == 20
    ledger.add_question_progress(con, 14, D, 1, "shadow band observation")
    totals = {q["question_id"]: q["total"]
              for q in ledger.question_progress_totals(con)}
    assert totals == {14: 1}                      # arm A stays empty on its own


def test_planned_rr_is_computed_defensively():
    """Feeds both arms of the R:R test, so it runs on every pick and every
    rejection -- including rows with missing or degenerate levels."""
    assert _planned_rr({"entry_price": 100.0, "stop_price": 95.0,
                        "target_price": 110.0}) == pytest.approx(2.0)
    # short: levels inverted, same ratio
    assert _planned_rr({"entry_price": 100.0, "stop_price": 105.0,
                        "target_price": 90.0}) == pytest.approx(2.0)
    assert _planned_rr({"entry_price": 100.0, "stop_price": 100.0,
                        "target_price": 110.0}) is None      # zero risk
    assert _planned_rr({"entry_price": None, "stop_price": 95.0,
                        "target_price": 110.0}) is None
    assert _planned_rr({}) is None


def test_registry_md_shows_the_declared_threshold_not_the_observed_count():
    md = open_questions.registry_md([{"question_id": 13, "total": 12,
                                      "last_event": "2026-09-05"}])
    assert "Progress 12/20" in md
    assert "may not restate a threshold" in md


def test_registry_marks_questions_that_were_never_pre_registered():
    md = open_questions.registry_md([])
    assert "[NOT pre-registered" in md          # the indicator-layer questions
    assert md.count("[NOT pre-registered") == 6


# ------------------------------------------------- 5. the calibration table

def test_calibration_suppresses_win_rate_and_avg_r_below_the_floor(con):
    _cal(con, 4, -0.05, 1)
    rows = ledger.calibration_aggregate(con)
    assert rows[0]["n"] == 1
    assert rows[0]["suppressed"] is True
    assert rows[0]["win_rate"] is None and rows[0]["avg_r"] is None


def test_calibration_reports_once_the_bucket_clears_the_floor(con):
    for i in range(ledger.CALIBRATION_MIN_N):
        _cal(con, 4, 1.0 if i % 2 else -1.0, i)
    row = ledger.calibration_aggregate(con)[0]
    assert row["suppressed"] is False
    assert row["win_rate"] == pytest.approx(0.5)


def test_calibration_md_still_shows_the_suppressed_bucket(con):
    """Visibility is the point of the table before it is actionable --
    suppression hides the numbers, not the row."""
    _cal(con, 4, -0.05, 1)
    md = _calibration_md(ledger.calibration_aggregate(con))
    assert "conviction 4" in md
    assert "n=1" in md
    assert "SUPPRESSED" in md
    assert "-0.05" not in md


def test_write_time_check_catches_a_calibration_row_below_the_floor():
    """The prose scanner keys on "n=8"-style text and never saw the one table
    in the playbook that prints a win rate beside an avg R."""
    pb = "\n".join([
        "| Conviction | n | realized WR | realized avgR | verdict |",
        "|-----------:|--:|:-----------:|:-------------:|:--------|",
        "| 5 | 0 | — | — | insufficient data |",
        "| 4 | 1 | 0% | -0.05 | insufficient data |",
    ])
    v = _check_calibration_table(pb)
    assert len(v) == 1
    assert "conviction 4" in v[0] and "n=1" in v[0]


# ------------------------------------------- 6. one changelog, not two

def test_the_miners_own_changelog_section_is_stripped_before_the_canonical_one():
    """v5 shipped with two sections both headed "Changelog (v5)": the document
    contract lists no Changelog section, but the miner wrote one anyway and
    write_playbook appended the canonical one on top of it."""
    body = "\n".join([
        "<!-- version: 4 -->",
        "## Core Principles",
        "1. Risk is defined before entry.",
        "",
        "---",
        "",
        "## Changelog (v5)",
        "- the miner wrote this one itself",
    ])
    out, n = _strip_changelog_sections(body)
    assert n == 1
    assert "Changelog" not in out
    assert "Risk is defined before entry." in out   # nothing else removed


def test_stripping_leaves_a_compliant_playbook_untouched():
    body = "<!-- version: 4 -->\n## Core Principles\n1. Risk is defined before entry.\n"
    out, n = _strip_changelog_sections(body)
    assert n == 0
    assert out.strip() == body.strip()


def test_stripping_removes_every_duplicate_section(con):
    body = "\n".join([
        "## Setup Rules", "- a rule",
        "", "---", "", "## Changelog (v5)", "- detailed version",
        "", "---", "", "## Changelog (v5)", "- condensed version",
    ])
    out, n = _strip_changelog_sections(body)
    assert n == 2
    assert "Changelog" not in out
    assert "- a rule" in out


def test_write_time_check_passes_a_properly_suppressed_table():
    pb = "\n".join([
        "| 5 | 0 | — | — | insufficient data |",
        "| 4 | 1 | — | — | insufficient data |",
        "| 3 | 12 | 58% | +0.41 | calibrated |",
    ])
    assert _check_calibration_table(pb) == []


def test_write_playbook_emits_exactly_one_changelog_end_to_end(con, monkeypatch, tmp_path):
    """The end-to-end guarantee, not just the strip helper in isolation.

    ROOT CAUSE THIS PINS (diagnosed 2026-09-18 from playbook v4/v5 on disk):
    the miner LLM wrote a `## Changelog` section INSIDE `playbook_markdown`,
    while write_playbook independently appended the canonical changelog from
    the separate `changelog` JSON field. Two sources, two sections -- visible
    in v4 and v5 as a rich prose block followed by a terse per-rule block,
    same facts, different wording. v3 predates it and is clean.

    It was NOT a cron double-invocation: the runs table shows exactly one
    weekly run per version (Sep 3 -> v4, Sep 6 -> v5). Nor an append-vs-
    overwrite bug: write_playbook overwrites, and archiving is a separate path.

    Relying on the prompt alone to stop this would leave the invariant at the
    mercy of model compliance, so the strip happens in code. This test asserts
    the actual output contract: however many the miner writes, exactly one
    survives.
    """
    from agent import memory

    monkeypatch.setattr(memory, "PLAYBOOK_PATH", tmp_path / "playbook.md")
    monkeypatch.setattr(memory, "PLAYBOOK_HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(memory, "_HOSTED", lambda: False)
    (tmp_path / "playbook.md").write_text(
        "<!-- version: 5 -->\n\n## Core Principles\n1. Risk first.\n",
        encoding="utf-8")

    # A non-compliant miner response: body carries TWO changelogs of its own.
    miner_body = "\n".join([
        "<!-- version: 5 -->", "", "## Setup Rules", "- gap_go: n=0",
        "", "---", "", "## Changelog (v6)", "- rich prose the miner wrote",
        "", "---", "", "## Changelog (v6)", "- terse list the miner wrote",
    ])
    version = memory.write_playbook(miner_body, "- the canonical changelog", con)

    written = (tmp_path / "playbook.md").read_text(encoding="utf-8")
    assert written.count("## Changelog") == 1, (
        "exactly one changelog must survive regardless of what the miner wrote")
    assert "the canonical changelog" in written
    assert "rich prose the miner wrote" not in written
    assert "terse list the miner wrote" not in written
    # The rest of the document must be untouched by the strip.
    assert "- gap_go: n=0" in written
    assert f"## Changelog (v{version})" in written
