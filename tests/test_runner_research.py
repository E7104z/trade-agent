"""
Tests for the runner research layer (RUNNER_PORT_SPEC_v1 §2.6).

The two questions this has to answer honestly:

  1. Does the Runner setup have edge at all?  (setup edge vs zero)
  2. Did picking help?                        (selected vs unselected)

The most important behaviour pinned here is the NULL RESULT. If selection edge
is not distinguishable from zero, the system must say so — the correct action
then is to trade the full filtered list or a random N. A design that can only
report success is not measuring anything.
"""
from __future__ import annotations

from datetime import date

import pytest

from agent import runner_research as rr


def _row(session, ticker, r, selected=False, **over):
    base = {
        "session_date": session, "ticker": ticker, "triggered": True,
        "r_multiple": r, "selected": selected, "scan_version": 1,
        "detection_minutes": 620, "rvol": 2.5, "price_tier": "20-50",
        "vwap_pct_above": 1.2, "ema9_slope": 0.03, "detection_price": 20.0,
        "trend_consistency": 70.0, "float_shares": 40.0, "regime_tag": "bullish",
    }
    base.update(over)
    return base


# ------------------------------------------------------------- bucketing

@pytest.mark.parametrize("minutes, expected", [
    (585, "pre-10:00"), (600, "10:00-10:30"), (629, "10:00-10:30"),
    (630, "10:30-11:00"), (660, "11:00-12:00"), (800, "12:00+"), (None, "?"),
])
def test_detection_bucket(minutes, expected):
    assert rr.bucket_of("runner_detection_bucket", {"detection_minutes": minutes}) == expected


@pytest.mark.parametrize("rvol, expected", [
    (1.6, "1.5-2"), (2.0, "2-3"), (3.0, "3-5"), (9.9, "5+"), (None, "?"),
])
def test_rvol_bucket(rvol, expected):
    assert rr.bucket_of("runner_rvol_bucket", {"rvol": rvol}) == expected


def test_slope_bucket_is_normalized_by_price():
    """A $0.04 slope means something different on a $6 stock than a $45 one, so
    the raw dollar value is not comparable across the price band."""
    cheap = {"ema9_slope": 0.04, "detection_price": 6.0}    # 0.67%
    pricey = {"ema9_slope": 0.04, "detection_price": 45.0}  # 0.09%
    assert rr.bucket_of("runner_slope_bucket", cheap) == "0.20%+"
    assert rr.bucket_of("runner_slope_bucket", pricey) == "0.05-0.10%"


def test_null_bucket_is_kept_not_dropped():
    """A large '?' bucket is itself a finding about data coverage."""
    rows = [_row(date(2026, 9, 1), "AAAA", 1.0, float_shares=None)]
    comps = rr.candidate_comparisons(rows)
    floats = [c for c in comps if c.dimension == "runner_float_bucket"]
    assert [c.level for c in floats] == ["?"]
    assert floats[0].note == "null/unmeasured bucket"


def test_every_mining_dimension_emits_comparisons():
    rows = [_row(date(2026, 9, i + 1), f"T{i}", 1.0) for i in range(5)]
    comps = rr.candidate_comparisons(rows)
    assert {c.dimension for c in comps} == set(rr.MINING_DIMENSIONS)


# ------------------------------------------------------------ setup edge

def test_setup_edge_computes_mean_and_flags_underpowered():
    rows = [_row(date(2026, 9, 1), "A", 1.0), _row(date(2026, 9, 2), "B", -1.0)]
    out = rr.setup_edge(rows, min_n=8)
    assert out["n"] == 2
    assert out["mean_r"] == pytest.approx(0.0)
    assert out["underpowered"] is True


def test_setup_edge_with_no_rows_is_not_an_error():
    out = rr.setup_edge([], min_n=8)
    assert out["n"] == 0 and out["mean_r"] is None


# -------------------------------------------------------- selection edge

def test_selection_edge_only_counts_contested_sessions():
    """A session where everything was auto-selected carries no selection
    signal — there was no choice — so it must not dilute the comparison."""
    rows = [
        # contested: both sides present
        _row(date(2026, 9, 1), "A", 2.0, selected=True),
        _row(date(2026, 9, 1), "B", -1.0, selected=False),
        # not contested: everything selected
        _row(date(2026, 9, 2), "C", 5.0, selected=True),
        _row(date(2026, 9, 2), "D", 5.0, selected=True),
    ]
    out = rr.selection_edge(rows, min_n=1)
    assert out["sessions_contested"] == 1
    assert out["n_selected"] == 1 and out["n_unselected"] == 1
    assert out["mean_r_selected"] == pytest.approx(2.0)   # not polluted by the 5.0s


def test_selection_edge_reports_the_null_result_plainly():
    """The system must be able to say "selection adds nothing"."""
    rows = []
    for i in range(20):
        d = date(2026, 9, 1)
        rows.append(_row(d, f"S{i}", 1.0 + (i % 3) * 0.01, selected=True))
        rows.append(_row(d, f"U{i}", 1.0 + (i % 3) * 0.01, selected=False))
    # Make tickers unique per session so nothing collides.
    for i, r in enumerate(rows):
        r["session_date"] = date(2026, 9, 1 + (i % 5))

    out = rr.selection_edge(rows, min_n=8)
    assert out["underpowered"] is False
    assert "no detectable selection edge" in out["verdict"]
    assert "full filtered list or a random N" in out["verdict"]


def test_selection_edge_can_report_the_picker_subtracting_value():
    """If the selector is actively worse than the field, say so."""
    rows = []
    for i in range(30):
        d = date(2026, 9, 1 + (i % 6))
        rows.append(_row(d, f"S{i}", -2.0 + (i % 4) * 0.05, selected=True))
        rows.append(_row(d, f"U{i}", 2.0 + (i % 4) * 0.05, selected=False))
    out = rr.selection_edge(rows, min_n=8)
    assert out["p_value"] is not None and out["p_value"] < 0.05
    assert "UNDERperformed" in out["verdict"]
    assert out["delta_r"] < 0


def test_selection_edge_underpowered_message_names_the_counts():
    rows = [_row(date(2026, 9, 1), "A", 1.0, selected=True),
            _row(date(2026, 9, 1), "B", 0.0, selected=False)]
    out = rr.selection_edge(rows, min_n=8)
    assert out["underpowered"] is True
    assert "1 selected / 1 unselected" in out["verdict"]


def test_markdown_states_the_null_outcome_for_the_miner():
    rows = [_row(date(2026, 9, 1), "A", 1.0, selected=True),
            _row(date(2026, 9, 1), "B", 0.0, selected=False)]
    md = rr.runner_md(rr.setup_edge(rows), rr.selection_edge(rows),
                      rr.candidate_comparisons(rows))
    assert "Setup edge" in md and "Selection edge" in md
    assert "A null result here is a real finding" in md
    assert "do not write a playbook rule that assumes otherwise" in md


def test_markdown_handles_the_empty_case():
    md = rr.runner_md(rr.setup_edge([]), rr.selection_edge([]), [])
    assert "No triggered runner candidates yet" in md


# --------------------------------------------------- integration with v4

def test_runner_comparisons_join_the_same_fdr_batch(monkeypatch):
    """Correcting the runner dimensions separately would leave the
    across-family multiplicity uncontrolled — which is where the width lives."""
    from agent import integrity

    rows = [_row(date(2026, 9, 1 + (i % 5)), f"T{i}", 1.0 + (i % 3)) for i in range(12)]
    monkeypatch.setattr(rr, "candidate_rows", lambda *a, **k: rows)

    class _LedgerStub:
        @staticmethod
        def stats_by(con, dim):
            return []
        @staticmethod
        def stats_by_outcome(con, dim):
            return []

    search = integrity.build_search(object(), _LedgerStub(), q=0.10, min_n=3)
    dims = {c.dimension for c in search.comparisons}
    assert "runner_setup_edge" in dims
    assert "runner_selection_edge" in dims
    assert any(d.startswith("runner_") and d in rr.MINING_DIMENSIONS for d in dims)
    # All in ONE batch, so q-values were assigned across the whole search.
    assert len(search.comparisons) >= len(rr.MINING_DIMENSIONS)


def test_scan_version_filter_prevents_cross_semantics_comparison():
    """Never compare rows across scan_version, exactly as with sim_version."""
    class _Con:
        def execute(self, *a, **k):
            class _R:
                @staticmethod
                def fetchall():
                    return []
            return _R()

    import agent.ledger as ledger_mod
    rows_v1 = [_row(date(2026, 9, 1), "A", 1.0)]
    rows_v2 = [_row(date(2026, 9, 1), "B", 9.0, scan_version=2)]
    original = ledger_mod.runner_candidate_outcomes_since
    ledger_mod.runner_candidate_outcomes_since = lambda con, since: rows_v1 + rows_v2
    try:
        kept = rr.candidate_rows(_Con(), scan_version=1)
        assert [r["ticker"] for r in kept] == ["A"]
    finally:
        ledger_mod.runner_candidate_outcomes_since = original
