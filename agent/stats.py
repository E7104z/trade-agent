"""
Statistical inference for the weekly miner — deterministic, dependency-free.

WHY THIS MODULE EXISTS
----------------------
`agent/ledger.py::_stats_rows` already computes a t-statistic per cell and the
playbook's standing bar is |t| >= 1.5. That bar is p ~= 0.13 two-sided, and the
weekly miner is shown ~12 dimensions x 3-12 levels each = on the order of 60-90
comparisons EVERY RUN. At |t| >= 1.5 with 70 looks and zero true edge you expect
roughly nine "findings" per week by chance alone. The bar is not a filter; it is
a false-positive generator.

This module supplies the missing half:

  1. An exact two-sided p-value per comparison (Student-t, not a normal
     approximation -- at n=8 the difference matters).
  2. Benjamini-Hochberg FDR control ACROSS every comparison shown in a single
     weekly run, so the miner sees a q-value that already accounts for how wide
     the search was.
  3. A bootstrap confidence interval on mean R, so effect size is reported as a
     range rather than a point estimate the LLM can anchor on.
  4. A minimum-detectable-effect calculator, so the miner is told what a cell
     CANNOT see at its current n. This is the single most useful number on a
     young ledger: most cells will never reach significance in the system's
     lifetime, and saying so explicitly stops the miner from reading noise.

DESIGN RULES (inherited from the indicator-layer specs)
  - Deterministic code computes every number. The LLM only interprets.
  - Zero third-party dependencies: stdlib `math` and `statistics` only. This
    module must import cleanly in the hosted Vercel runtime, in offline mode,
    and in a bare test process.
  - Missing/insufficient data degrades to None. Never fabricate a p-value.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Any, Iterable, Sequence

__all__ = [
    "student_t_sf",
    "one_sample_t",
    "welch_t",
    "benjamini_hochberg",
    "bootstrap_ci_mean",
    "min_detectable_effect",
    "min_detectable_effect_two_sample",
    "fisher_exact_2x2",
    "required_n",
    "Comparison",
    "SearchResult",
    "evaluate_search",
    "annotate_stats_rows",
]

_ND = NormalDist()

# --------------------------------------------------------------------------
# Regularized incomplete beta -> Student-t tail probability
# --------------------------------------------------------------------------
# Numerical Recipes' continued-fraction evaluation (modified Lentz). We need
# this because Python's stdlib has no t-distribution CDF and we refuse to take
# a scipy dependency for four functions.

_BETACF_MAXIT = 300
_BETACF_EPS = 3.0e-16
_BETACF_FPMIN = 1.0e-300


def _betacf(a: float, b: float, x: float) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _BETACF_FPMIN:
        d = _BETACF_FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, _BETACF_MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _BETACF_FPMIN:
            d = _BETACF_FPMIN
        c = 1.0 + aa / c
        if abs(c) < _BETACF_FPMIN:
            c = _BETACF_FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _BETACF_FPMIN:
            d = _BETACF_FPMIN
        c = 1.0 + aa / c
        if abs(c) < _BETACF_FPMIN:
            c = _BETACF_FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _BETACF_EPS:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b). Domain-guarded."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_beta = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    front = math.exp(ln_beta)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_sf(t: float, df: float) -> float:
    """Two-sided tail probability P(|T_df| >= |t|).

    Returns a value in (0, 1]. df must be >= 1; below that there is no
    distribution to speak of and we return None-equivalent 1.0 (never reject).
    """
    if df is None or df < 1 or t is None:
        return 1.0
    t = abs(float(t))
    if not math.isfinite(t):
        return 0.0
    if t == 0.0:
        return 1.0
    x = df / (df + t * t)
    return max(0.0, min(1.0, _betai(df / 2.0, 0.5, x)))


# --------------------------------------------------------------------------
# Test statistics
# --------------------------------------------------------------------------

def one_sample_t(
    mean: float | None,
    std: float | None,
    n: int | None,
    mu0: float = 0.0,
) -> tuple[float | None, float | None, float | None]:
    """(t, df, two-sided p) for H0: mean == mu0.

    Accepts the exact (avg_r, std_r, triggered) triple `_stats_rows` already
    produces, so no re-aggregation of the ledger is required. Returns
    (None, None, None) when the sample cannot support a test -- n < 2 or a
    degenerate/zero standard deviation.
    """
    if mean is None or std is None or n is None:
        return None, None, None
    n = int(n)
    if n < 2 or std is None or std <= 0:
        return None, None, None
    se = std / math.sqrt(n)
    if se <= 0:
        return None, None, None
    t = (float(mean) - mu0) / se
    df = n - 1
    return round(t, 4), float(df), student_t_sf(t, df)


def welch_t(
    mean_a: float | None, std_a: float | None, n_a: int | None,
    mean_b: float | None, std_b: float | None, n_b: int | None,
) -> tuple[float | None, float | None, float | None]:
    """Welch's unequal-variance two-sample t. (t, df, two-sided p).

    Used for A-vs-B comparisons the miner actually cares about: long vs short,
    above-200d vs below, arm A vs arm B of a paired playbook run. Welch rather
    than Student because trade-return variance is emphatically not equal across
    buckets (a >2 ATR stop bucket has far wider dispersion than a <0.5 bucket).
    """
    if None in (mean_a, std_a, n_a, mean_b, std_b, n_b):
        return None, None, None
    n_a, n_b = int(n_a), int(n_b)
    if n_a < 2 or n_b < 2 or std_a <= 0 or std_b <= 0:
        return None, None, None
    va, vb = (std_a ** 2) / n_a, (std_b ** 2) / n_b
    denom = va + vb
    if denom <= 0:
        return None, None, None
    t = (float(mean_a) - float(mean_b)) / math.sqrt(denom)
    df_num = denom ** 2
    df_den = (va ** 2) / (n_a - 1) + (vb ** 2) / (n_b - 1)
    if df_den <= 0:
        return None, None, None
    df = df_num / df_den
    return round(t, 4), round(df, 2), student_t_sf(t, df)


# --------------------------------------------------------------------------
# Multiplicity control
# --------------------------------------------------------------------------

def benjamini_hochberg(pvals: Sequence[float | None], q: float = 0.10):
    """Benjamini-Hochberg FDR control.

    Returns (rejected, qvalues) aligned with the input order.
      rejected[i] : bool  -- survives FDR control at level q
      qvalues[i]  : float -- BH-adjusted p-value (monotone, capped at 1.0)

    `None` entries (comparisons that could not be tested) pass through as
    (False, None) and are EXCLUDED from the multiplicity count m -- an untestable
    cell is not a look, it is an absence of one. This matters: counting empty
    cells toward m would make the correction spuriously lenient by inflating the
    denominator with comparisons that could never have produced a finding.
    """
    idx = [i for i, p in enumerate(pvals) if p is not None]
    m = len(idx)
    rejected: list[bool] = [False] * len(pvals)
    qvalues: list[float | None] = [None] * len(pvals)
    if m == 0:
        return rejected, qvalues

    ordered = sorted(idx, key=lambda i: pvals[i])
    # Step-up: adjusted p_(k) = min over j>=k of (m/j) * p_(j), enforced monotone
    # from the largest rank downward.
    running_min = 1.0
    for rank in range(m, 0, -1):
        i = ordered[rank - 1]
        adj = min(1.0, (m / rank) * float(pvals[i]))
        running_min = min(running_min, adj)
        qvalues[i] = round(running_min, 6)

    # Largest k with p_(k) <= (k/m)*q; reject all ranks <= k.
    k_max = 0
    for rank in range(1, m + 1):
        if float(pvals[ordered[rank - 1]]) <= (rank / m) * q:
            k_max = rank
    for rank in range(1, k_max + 1):
        rejected[ordered[rank - 1]] = True
    return rejected, qvalues


# --------------------------------------------------------------------------
# Effect size and power
# --------------------------------------------------------------------------

def bootstrap_ci_mean(
    values: Sequence[float],
    conf: float = 0.90,
    draws: int = 2000,
    seed: int = 20260827,
) -> tuple[float, float] | None:
    """Percentile-bootstrap CI for the mean. Seeded => reproducible across runs.

    Deliberately seeded with a fixed default: a confidence interval that moves
    between two runs over the same ledger is indistinguishable from a real
    change in the data, which is exactly the confusion this whole system exists
    to prevent.
    """
    vals = [float(v) for v in values if v is not None]
    n = len(vals)
    if n < 3:
        return None
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(draws):
        s = 0.0
        for _ in range(n):
            s += vals[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo_i = int((1.0 - conf) / 2.0 * draws)
    hi_i = min(draws - 1, int((1.0 + conf) / 2.0 * draws))
    return round(means[lo_i], 4), round(means[hi_i], 4)


def min_detectable_effect(
    n: int | None, std: float | None, alpha: float = 0.05, power: float = 0.80
) -> float | None:
    """Smallest true mean R this sample size could reliably detect.

    THE most important number on a young ledger. A cell reporting avgR=+0.9 at
    n=6 with std=1.3 has an MDE of 1.49R -- it is not powered to detect anything
    smaller than 1.49R, so its +0.9 point estimate carries no information about
    whether a real +0.9 edge exists. Reporting MDE alongside avgR converts
    "looks promising" into "cannot see effects this small."
    """
    if n is None or std is None or n < 2 or std <= 0:
        return None
    z_a = _ND.inv_cdf(1.0 - alpha / 2.0)
    z_b = _ND.inv_cdf(power)
    return round((z_a + z_b) * float(std) / math.sqrt(int(n)), 3)


def min_detectable_effect_two_sample(
    n_a: int | None, std_a: float | None, n_b: int | None, std_b: float | None,
    alpha: float = 0.05, power: float = 0.80,
) -> float | None:
    """Smallest true DIFFERENCE in mean R two arms of these sizes could detect.

    Uses both arms' standard errors (the Welch denominator). Computing it from
    one arm alone understates it whenever the other arm is noisier, which is
    the usual case for an unselected runner field against a handful of picks.
    """
    if None in (n_a, std_a, n_b, std_b):
        return None
    n_a, n_b = int(n_a), int(n_b)
    if n_a < 2 or n_b < 2 or std_a <= 0 or std_b <= 0:
        return None
    se = math.sqrt(float(std_a) ** 2 / n_a + float(std_b) ** 2 / n_b)
    z_a = _ND.inv_cdf(1.0 - alpha / 2.0)
    z_b = _ND.inv_cdf(power)
    return round((z_a + z_b) * se, 3)


def fisher_exact_2x2(a: int, b: int, c: int, d: int) -> float | None:
    """Two-sided Fisher exact p for the table [[a, b], [c, d]]. Stdlib only.

    Sums the hypergeometric probability of every table with the same margins
    that is no more likely than the observed one (the standard two-sided
    definition, as in R's fisher.test). Exact at any n, which matters here:
    most cells this ledger can build are small, and a normal approximation to
    a proportion at n=9 is exactly the kind of false precision the integrity
    layer exists to refuse. None when a margin is empty (nothing to test).
    """
    a, b, c, d = (int(x) for x in (a, b, c, d))
    if min(a, b, c, d) < 0:
        return None
    r1, r2, c1 = a + b, c + d, a + c
    n = r1 + r2
    if r1 == 0 or r2 == 0 or c1 == 0 or c1 == n:
        return None

    def _lp(x: int) -> float:
        return (math.lgamma(r1 + 1) - math.lgamma(x + 1) - math.lgamma(r1 - x + 1)
                + math.lgamma(r2 + 1) - math.lgamma(c1 - x + 1) - math.lgamma(r2 - c1 + x + 1)
                - (math.lgamma(n + 1) - math.lgamma(c1 + 1) - math.lgamma(n - c1 + 1)))

    lo, hi = max(0, c1 - r2), min(r1, c1)
    obs = _lp(a)
    p = sum(math.exp(_lp(x)) for x in range(lo, hi + 1) if _lp(x) <= obs + 1e-7)
    return min(1.0, p)


def required_n(
    effect: float, std: float, alpha: float = 0.05, power: float = 0.80
) -> int | None:
    """Sample size needed to detect `effect` at the given alpha/power."""
    if effect is None or std is None or effect == 0 or std <= 0:
        return None
    z_a = _ND.inv_cdf(1.0 - alpha / 2.0)
    z_b = _ND.inv_cdf(power)
    return int(math.ceil(((z_a + z_b) * float(std) / abs(float(effect))) ** 2))


# --------------------------------------------------------------------------
# The search harness -- what the weekly loop actually calls
# --------------------------------------------------------------------------

@dataclass
class Comparison:
    """One cell the miner is shown. Every one of these counts as a look.

    `test` names the statistic the producer built this comparison for.
    "one_sample" (mean R vs zero) is computed by `evaluate_search`; any other
    value ("welch", "fisher", ...) arrives with its own t/df/p already
    attached and `evaluate_search` must keep them. Re-testing a two-sample
    delta as a one-sample mean, with one arm's variance, turned a Welch
    p=0.096 into a batch p=0.003 -- a manufactured finding on exactly the
    comparison that asks whether the selector adds anything.
    """
    dimension: str
    level: str
    n: int
    mean_r: float | None
    std_r: float | None
    t_stat: float | None = None
    df: float | None = None
    p_value: float | None = None
    q_value: float | None = None
    significant: bool = False
    mde: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    n_dimensions: int = 1
    note: str = ""
    test: str = "one_sample"

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension, "level": self.level, "n": self.n,
            "mean_r": self.mean_r, "std_r": self.std_r, "t_stat": self.t_stat,
            "df": self.df, "p_value": self.p_value, "q_value": self.q_value,
            "significant": self.significant, "mde": self.mde,
            "ci_low": self.ci_low, "ci_high": self.ci_high,
            "n_dimensions": self.n_dimensions, "note": self.note,
            "test": self.test,
        }


@dataclass
class SearchResult:
    comparisons: list[Comparison] = field(default_factory=list)
    looks: int = 0
    testable_looks: int = 0
    q_level: float = 0.10
    survivors: list[Comparison] = field(default_factory=list)
    # Filled by integrity.build_search for the weekly block (structural
    # consistency and width reporting); not part of the statistics.
    pair_status: list[dict] = field(default_factory=list)
    rows_by_lane: dict = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "looks": self.looks,
            "testable_looks": self.testable_looks,
            "q_level": self.q_level,
            "survivors": len(self.survivors),
            "expected_false_positives_uncorrected": round(0.05 * self.testable_looks, 2),
        }


# A cell holding at least this share of its dimension's rows is the lane mean
# under another name. Tested against zero it reproduces the lane's own mean R
# test once per dimension, so a lane that loses money shows up as a row of
# "significant" cells that are one fact counted several times -- four
# q<0.001 runner cells, each ~99% of the same 1,003 rows, read as four
# independent confirmations. Such a cell is kept and shown, but not tested and
# not counted as a look; the lane's mean is tested once, by its own comparison.
DEGENERATE_SHARE = 0.90


def _mark_degenerate(comps: list[Comparison]) -> None:
    totals: dict[str, int] = {}
    levels: dict[str, int] = {}
    for c in comps:
        if c.test != "one_sample":
            continue
        totals[c.dimension] = totals.get(c.dimension, 0) + int(c.n or 0)
        levels[c.dimension] = levels.get(c.dimension, 0) + 1
    for c in comps:
        total = totals.get(c.dimension, 0)
        # A dimension with one level (the lane-mean comparison itself) is the
        # test these cells duplicate; it is never degenerate.
        if c.test != "one_sample" or levels.get(c.dimension, 0) < 2 or not total:
            continue
        share = (c.n or 0) / total
        if share >= DEGENERATE_SHARE:
            c.test = "degenerate"
            c.t_stat = c.df = c.p_value = None
            c.mde = None
            c.note = (c.note + f" degenerate: holds {share:.0%} of this dimension's "
                      f"{total} rows, so it restates the lane mean; not tested").strip()


def evaluate_search(
    comparisons: Iterable[Comparison],
    q: float = 0.10,
    min_n: int = 8,
) -> SearchResult:
    """Score every comparison, then apply FDR control across the whole batch.

    `min_n` cells are still tested and reported, but flagged in `note` and never
    marked significant -- consistent with the playbook's existing n>=8 rule. The
    point of still testing them is that they must count toward the multiplicity
    denominator: a look you took and then dismissed is still a look.

    A cell holding >= DEGENERATE_SHARE of its dimension's rows is not tested
    (see `_mark_degenerate`): it is the lane mean again, not a look at the
    dimension.
    """
    comps = list(comparisons)
    _mark_degenerate(comps)
    for c in comps:
        # Only one-sample comparisons are tested here. Any other test was
        # computed by its producer on the data it needs (two arms, a 2x2
        # table) and its t/df/p/MDE are kept exactly as supplied; a missing p
        # means untestable and passes through BH as None, like an empty cell.
        if c.test == "one_sample":
            t, df, p = one_sample_t(c.mean_r, c.std_r, c.n)
            c.t_stat, c.df, c.p_value = t, df, p
            c.mde = min_detectable_effect(c.n, c.std_r)
        if c.n < min_n:
            c.note = (c.note + f" under-powered (n={c.n} < {min_n})").strip()

    rejected, qvals = benjamini_hochberg([c.p_value for c in comps], q=q)
    for c, rej, qv in zip(comps, rejected, qvals):
        c.q_value = qv
        # Three independent conditions, all required. Dimension cap is a hard
        # structural rule (see agent/research.py): a ledger this size cannot
        # power a three-way interaction at any point in its life.
        c.significant = bool(rej) and c.n >= min_n and c.n_dimensions <= 2

    testable = sum(1 for c in comps if c.p_value is not None)
    return SearchResult(
        comparisons=comps,
        looks=len(comps),
        testable_looks=testable,
        q_level=q,
        survivors=[c for c in comps if c.significant],
    )


def annotate_stats_rows(
    rows: Sequence[dict], dimension: str, n_dimensions: int = 1
) -> list[Comparison]:
    """Adapt `ledger._stats_rows` output into Comparison objects.

    Reads the exact keys _stats_rows emits (dim / triggered / avg_r / std_r) so
    no ledger query needs to change. Rows whose dim is the '?' null-bucket are
    kept -- a large '?' bucket is itself a finding about data coverage -- but
    marked so the miner does not read it as a setup.
    """
    out: list[Comparison] = []
    for r in rows:
        level = str(r.get("dim", "?"))
        c = Comparison(
            dimension=dimension,
            level=level,
            n=int(r.get("triggered") or 0),
            mean_r=r.get("avg_r"),
            std_r=r.get("std_r"),
            n_dimensions=n_dimensions,
            note="null/unmeasured bucket" if level == "?" else "",
        )
        out.append(c)
    return out
