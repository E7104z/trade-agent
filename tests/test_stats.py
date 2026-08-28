"""
Hand-verified tests for agent/stats.py.

Every expected value here is either a published statistical table entry or a
worked textbook example -- never a value produced by this module and then
frozen. A test that only asserts the code agrees with itself proves nothing.
"""
from __future__ import annotations

import pytest

from agent.stats import (
    Comparison,
    annotate_stats_rows,
    benjamini_hochberg,
    bootstrap_ci_mean,
    evaluate_search,
    min_detectable_effect,
    one_sample_t,
    required_n,
    student_t_sf,
    welch_t,
)

# --------------------------------------------------------------- t distribution
# Critical values from standard two-tailed t tables.


@pytest.mark.parametrize("t,df,expected_p", [
    (12.706, 1, 0.05),
    (4.303, 2, 0.05),
    (2.228, 10, 0.05),
    (2.042, 30, 0.05),
    (1.812, 10, 0.10),
    (3.169, 10, 0.01),
    (2.086, 20, 0.05),
])
def test_student_t_sf_matches_published_critical_values(t, df, expected_p):
    assert student_t_sf(t, df) == pytest.approx(expected_p, abs=5e-4)


def test_student_t_converges_to_normal_at_large_df():
    # z = 1.959964 is the exact two-sided 5% normal critical value.
    assert student_t_sf(1.959964, 100000) == pytest.approx(0.05, abs=1e-4)


def test_student_t_sf_is_symmetric_and_bounded():
    assert student_t_sf(2.5, 12) == student_t_sf(-2.5, 12)
    assert student_t_sf(0.0, 12) == 1.0
    assert 0.0 <= student_t_sf(50.0, 5) <= 1.0


def test_student_t_sf_degenerate_inputs_never_reject():
    assert student_t_sf(None, 10) == 1.0
    assert student_t_sf(2.0, 0) == 1.0
    assert student_t_sf(2.0, None) == 1.0


# ------------------------------------------------------------------ one-sample t

def test_one_sample_t_hand_computed():
    # mean=0.5, std=1.0, n=16 -> se=0.25 -> t=2.0, df=15
    t, df, p = one_sample_t(0.5, 1.0, 16)
    assert t == pytest.approx(2.0, abs=1e-9)
    assert df == 15
    # published: t=2.131 at df=15 is p=0.05, so t=2.0 must be a bit above 0.05
    assert 0.05 < p < 0.08


def test_one_sample_t_refuses_degenerate_samples():
    assert one_sample_t(0.5, 1.0, 1) == (None, None, None)
    assert one_sample_t(0.5, 0.0, 20) == (None, None, None)
    assert one_sample_t(None, 1.0, 20) == (None, None, None)
    assert one_sample_t(0.5, None, 20) == (None, None, None)


# ----------------------------------------------------------------------- Welch

def test_welch_t_identical_samples_gives_zero():
    t, df, p = welch_t(0.4, 1.0, 20, 0.4, 1.0, 20)
    assert t == pytest.approx(0.0)
    assert p == pytest.approx(1.0)


def test_welch_t_hand_computed_separation():
    # a: mean 1.0 std 1.0 n 25 -> va = 0.04
    # b: mean 0.0 std 1.0 n 25 -> vb = 0.04
    # t = 1.0 / sqrt(0.08) = 3.5355
    t, df, p = welch_t(1.0, 1.0, 25, 0.0, 1.0, 25)
    assert t == pytest.approx(3.5355, abs=1e-3)
    assert df == pytest.approx(48.0, abs=0.5)
    assert p < 0.01


def test_welch_t_unequal_variance_reduces_df_below_pooled():
    # Wildly unequal variances must shrink df -- that is the whole point of Welch.
    _, df, _ = welch_t(1.0, 0.2, 30, 0.0, 3.0, 30)
    assert df < 40


# ------------------------------------------------------------ Benjamini-Hochberg

def test_bh_textbook_example_rejects_exactly_two():
    # Canonical worked example: m=8, q=0.05 -> only the first two survive.
    p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.060, 0.074, 0.205]
    rejected, q = benjamini_hochberg(p, q=0.05)
    assert rejected == [True, True, False, False, False, False, False, False]
    # hand-computed monotone adjusted p-values
    assert q[0] == pytest.approx(0.008, abs=1e-6)
    assert q[1] == pytest.approx(0.032, abs=1e-6)
    assert q[2] == pytest.approx(0.0672, abs=1e-4)
    assert q[7] == pytest.approx(0.205, abs=1e-6)


def test_bh_qvalues_are_monotone_nondecreasing_in_p():
    p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.060, 0.074, 0.205]
    _, q = benjamini_hochberg(p, q=0.05)
    ordered = [q[i] for i in sorted(range(len(p)), key=lambda i: p[i])]
    assert all(a <= b + 1e-12 for a, b in zip(ordered, ordered[1:]))


def test_bh_all_significant_when_every_p_is_tiny():
    rejected, _ = benjamini_hochberg([0.0001] * 5, q=0.05)
    assert all(rejected)


def test_bh_preserves_input_order_when_unsorted():
    p = [0.205, 0.001, 0.074, 0.008]
    rejected, q = benjamini_hochberg(p, q=0.05)
    assert rejected == [False, True, False, True]
    assert q[1] < q[3] < q[2] < q[0]


def test_bh_excludes_none_from_the_multiplicity_denominator():
    # Two testable comparisons among six entries: m must be 2, not 6. If None
    # counted toward m the correction would be spuriously lenient.
    strict, _ = benjamini_hochberg([0.02, 0.03], q=0.05)
    padded, _ = benjamini_hochberg([0.02, None, None, 0.03, None, None], q=0.05)
    assert [padded[0], padded[3]] == strict
    assert padded[1] is False and padded[2] is False


def test_bh_empty_and_all_none_are_safe():
    assert benjamini_hochberg([], q=0.05) == ([], [])
    rej, q = benjamini_hochberg([None, None], q=0.05)
    assert rej == [False, False] and q == [None, None]


def test_bh_is_stricter_than_raw_alpha_at_realistic_search_width():
    # THE core claim of this module: at the miner's real search width, a
    # comparison that clears raw p<0.05 need not survive FDR control.
    pvals = [0.04] + [0.5] * 69          # one "finding" among 70 looks
    rejected, _ = benjamini_hochberg(pvals, q=0.10)
    assert rejected[0] is False


# ----------------------------------------------------------------- effect size

def test_min_detectable_effect_hand_computed():
    # (1.959964 + 0.841621) * 1.3 / sqrt(6) = 1.487
    assert min_detectable_effect(6, 1.3) == pytest.approx(1.487, abs=1e-3)


def test_min_detectable_effect_shrinks_with_n():
    small = min_detectable_effect(10, 1.2)
    large = min_detectable_effect(200, 1.2)
    assert large < small


def test_required_n_matches_the_ledger_sizing_argument():
    # +0.2R against 1.2R dispersion needs ~283 triggered trades at 80% power.
    assert required_n(0.2, 1.2) == 283
    # a 0.1R edge needs roughly four times as many
    assert required_n(0.1, 1.2) == 1131


def test_required_n_and_mde_are_inverses():
    n = required_n(0.5, 1.2)
    assert min_detectable_effect(n, 1.2) == pytest.approx(0.5, abs=0.01)


def test_bootstrap_ci_is_deterministic_under_seed_and_brackets_the_mean():
    vals = [1.0, -0.5, 2.0, 0.25, -1.0, 0.75, 1.5, -0.25]
    a = bootstrap_ci_mean(vals)
    b = bootstrap_ci_mean(vals)
    assert a == b                                   # reproducible across runs
    mean = sum(vals) / len(vals)
    assert a[0] <= mean <= a[1]
    assert bootstrap_ci_mean([1.0, 2.0]) is None    # too small to bootstrap


# --------------------------------------------------------------- search harness

def _c(dim, level, n, mean, std, ndim=1):
    return Comparison(dimension=dim, level=level, n=n, mean_r=mean, std_r=std,
                      n_dimensions=ndim)


def test_evaluate_search_counts_every_look_including_untestable_ones():
    res = evaluate_search([
        _c("setup_type", "gap_go", 30, 0.9, 1.0),
        _c("setup_type", "sympathy", 1, 2.0, None),     # untestable
        _c("setup_type", "breakout", 12, 0.1, 1.2),
    ], q=0.10)
    assert res.looks == 3
    assert res.testable_looks == 2


def test_evaluate_search_blocks_under_powered_cells_from_significance():
    res = evaluate_search([_c("setup_type", "gap_go", 5, 3.0, 0.4)], q=0.10)
    c = res.comparisons[0]
    assert c.p_value is not None and c.p_value < 0.01   # raw test is significant
    assert c.significant is False                       # but n < 8 blocks it
    assert "under-powered" in c.note


def test_evaluate_search_blocks_three_way_interactions_structurally():
    # Even a large, clean, highly significant cell cannot be promoted if it
    # conditions on three dimensions. This is a hard structural rule.
    res = evaluate_search([_c("setup x regime x rvol", "cell", 60, 1.0, 0.8, ndim=3)], q=0.10)
    c = res.comparisons[0]
    assert c.p_value < 0.001
    assert c.significant is False


def test_evaluate_search_allows_a_clean_two_way_cell():
    res = evaluate_search([_c("setup x regime", "gap_go/uptrend", 60, 1.0, 0.8, ndim=2)], q=0.10)
    assert res.comparisons[0].significant is True
    assert len(res.survivors) == 1


def test_evaluate_search_attaches_mde_to_every_testable_cell():
    res = evaluate_search([_c("setup_type", "gap_go", 20, 0.3, 1.1)], q=0.10)
    assert res.comparisons[0].mde == pytest.approx(0.689, abs=1e-3)


def test_search_summary_reports_expected_false_positives():
    res = evaluate_search([_c("d", f"l{i}", 20, 0.0, 1.0) for i in range(70)], q=0.10)
    assert res.summary()["testable_looks"] == 70
    assert res.summary()["expected_false_positives_uncorrected"] == 3.5


def test_annotate_stats_rows_reads_ledger_stats_shape_unchanged():
    # Exactly the dict shape agent/ledger.py::_stats_rows emits.
    rows = [
        {"dim": "gap_go", "n": 12, "triggered": 10, "wins": 6, "win_rate": 0.6,
         "avg_r": 0.42, "total_weighted_r": 3.1, "std_r": 1.05, "t_stat": 1.26,
         "baseline_avg_r": 0.10},
        {"dim": "?", "n": 3, "triggered": 2, "wins": 1, "win_rate": 0.5,
         "avg_r": 0.0, "total_weighted_r": 0.0, "std_r": 0.5, "t_stat": 0.0,
         "baseline_avg_r": None},
    ]
    comps = annotate_stats_rows(rows, "setup_type")
    assert comps[0].n == 10 and comps[0].mean_r == 0.42 and comps[0].std_r == 1.05
    assert comps[1].note == "null/unmeasured bucket"
