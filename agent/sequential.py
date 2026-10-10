"""
The forward test as a sequence of looks that spends one error budget
(specs/EDGE_ACCELERATION_SPEC_v1.md Part 1).

WHY THIS MODULE EXISTS
----------------------
Until this module the forward test was ONE look: at forward n >= 8 (and ten
days in quarantine) a rule was tested at two-sided 0.05, and a rule that
missed was RETIRED -- for good, under the graveyard rule. A trader's hypothesis
was worse off: a mean R <= 0 at its first look refuted it outright.

Trade R on this desk has a standard deviation of about 1.0-1.9R (stop -1R,
target +2R, session exits between). At n = 8 a rule therefore needed a forward
mean near +1.2R to pass. Simulated on R shaped like this desk's, a real +0.3R
edge validated about 6% of the time and a real +0.5R edge about 12%; the rest
were retired and could never be proposed again. A real +0.3R idea showed a
mean <= 0 over its first eight sessions about a third of the time. The gate
was a false-negative machine pointed at exactly the edges a desk can expect
to have.

THE DESIGN
----------
The same total error budget, spent across a pre-registered horizon instead of
in one place:

  * One-sided ALPHA = 0.025 in the direction the rule was discovered. That is
    the old two-sided 0.05 plus the sign check, so the budget is unchanged.
  * Information t = min(1, n / n_max). Cumulative alpha spent by t is
        A(t) = ALPHA * t ** RHO
    and each look tests at the increment A(t_now) - A(t_last_look). By the
    union bound the chance that ANY look passes a null rule is <= ALPHA,
    however many looks are taken. How large n is at a look depends on how many
    trades arrived, never on their R, so looking every week costs nothing.
  * Futility, non-binding: a rule is retired once the one-sided FUTILITY_CONF
    upper confidence bound on its effect falls below MIN_USEFUL_R. Stopping a
    test early can only lower its false-positive rate, so this spends nothing.
  * At t = 1 the budget is gone: a rule not shown by then is retired.

Simulated (scratch study, 2026-10-04; 1,200-4,000 runs per cell, weekly looks
after the ten-day quarantine):

    morning-rate rule (~0.5 obs/session)   old single look   this design
      false positive (true edge 0)              1.5%            0.6%
      true edge +0.3R validated                   6%             22%
      true edge +0.5R validated                  12%             71%
      true edge +1.0R validated                  50%            ~100%
      flat rule retired after (median)      15 sessions     ~45 sessions

    hypothesis (~0.8 session-units/session)
      false positive                            1.5%            0.3%
      true edge +0.5R supported                  21%             76%

FIELD LANES
-----------
The runner and the gap field price EVERY instance their scan finds, so a rule
there gathers 5-20 (session, industry) units a session, and the edges such a
field can hold are small per trade but frequent (twenty fades a day at +0.1R
is +2R a day). A 96-unit horizon with a +0.10R floor would retire exactly
those. Field lanes therefore spend the same budget over FIELD_N_MAX units with
a FIELD_MIN_USEFUL_R floor (simulated, ~8 units/session, unit sd 0.95):

                                  96 / 0.10R      768 / 0.05R
      false positive                 2.2%            0.7%
      true edge +0.12R validated      22%             77%  (~50 sessions)
      true edge +0.20R validated      52%             99%  (~30 sessions)
      true edge +0.30R validated      86%            ~100% (~15 sessions)

The design is fixed by lane in code (`design_for`), so it is pre-registered:
no rule's horizon can move with its data.

The forward-only guard, n >= 8 before any look, the ten-day quarantine floor,
cohort FDR and the sign requirement are all unchanged. Nothing here is a
number the LLM can move.
"""
from __future__ import annotations

import math
from typing import Any, Sequence

__all__ = [
    "ALPHA", "N_MAX", "RHO", "FUTILITY_CONF", "MIN_USEFUL_R", "DESIGN",
    "FIELD_LANES", "FIELD_N_MAX", "FIELD_MIN_USEFUL_R", "design_for",
    "spent_at", "p_one_sided", "t_quantile", "upper_bound", "look", "cohort",
    "describe",
]

ALPHA = 0.025          # one-sided; = the old two-sided 0.05 with the sign check
N_MAX = 96             # forward units by which the whole budget is spent
RHO = 2.0              # spending shape: A(t) = ALPHA * t**RHO (back-loaded)
FUTILITY_CONF = 0.80   # one-sided upper bound used to call a rule futile
MIN_USEFUL_R = 0.10    # an edge below this is not worth trading after friction
DESIGN = "spend-v1"    # stamped on every look, so a later design is never confused with this one

FIELD_LANES = ("gapfield",)
FIELD_N_MAX = 768
FIELD_MIN_USEFUL_R = 0.05


def design_for(lane: str | None) -> tuple[int, float]:
    """(horizon, futility floor) for a rule's lane, fixed in code."""
    if lane in FIELD_LANES:
        return FIELD_N_MAX, FIELD_MIN_USEFUL_R
    return N_MAX, MIN_USEFUL_R

_EPS = 1e-12


def spent_at(n: int | None, n_max: int = N_MAX) -> float:
    """Cumulative one-sided alpha spent once `n` forward units are in."""
    if not n or n <= 0:
        return 0.0
    t = min(1.0, float(n) / float(n_max))
    return ALPHA * t ** RHO


def p_one_sided(mean: float | None, std: float | None, n: int | None,
                direction: int = 1) -> float | None:
    """One-sided t-test p for H0: direction * mu <= 0. None when untestable."""
    from .stats import one_sample_t
    t, _df, p2 = one_sample_t(mean, std, n)
    if t is None or p2 is None:
        return None
    td = t * (1 if direction >= 0 else -1)
    return p2 / 2.0 if td >= 0 else 1.0 - p2 / 2.0


def t_quantile(q: float, df: float) -> float:
    """Upper quantile of Student's t: the x with P(T <= x) = q, for q >= 0.5."""
    from .stats import student_t_sf
    if q <= 0.5:
        return 0.0
    target = 2.0 * (1.0 - q)            # two-sided tail mass at +x
    lo, hi = 0.0, 1.0
    while student_t_sf(hi, df) > target and hi < 1e6:
        hi *= 2.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if student_t_sf(mid, df) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def upper_bound(mean: float | None, std: float | None, n: int | None,
                direction: int = 1, conf: float = FUTILITY_CONF) -> float | None:
    """One-sided `conf` upper confidence bound on direction * mu."""
    if mean is None or std is None or not n or n < 2 or std <= 0:
        return None
    d = 1 if direction >= 0 else -1
    return d * float(mean) + t_quantile(conf, n - 1) * float(std) / math.sqrt(n)


def look(n: int, mean: float | None, std: float | None, direction: int,
         spent_before: float | None, n_max: int = N_MAX,
         min_useful_r: float | None = None) -> dict[str, Any]:
    """The numbers for one look. Does not decide significance: that is the
    cohort's job (`cohort`), because rules looked at in the same run are one
    family.

    status:
      'no_new_information'  nothing spent since the last look (n has not grown)
      'untestable'          degenerate sample (n < 2 or zero variance); waits
                            and spends nothing
      'looked'              p, p_adj (p scaled to the full ALPHA), the upper
                            bound and whether the rule is futile or final
    """
    spent_before = float(spent_before or 0.0)
    mue = MIN_USEFUL_R if min_useful_r is None else float(min_useful_r)
    spent_now = spent_at(n, n_max)
    alpha_look = spent_now - spent_before
    info = min(1.0, float(n) / float(n_max)) if n_max else 1.0
    base = {"design": DESIGN, "n": int(n), "n_max": int(n_max), "info": round(info, 4),
            "spent_before": round(spent_before, 8), "direction": 1 if direction >= 0 else -1,
            "min_useful_r": mue}
    if alpha_look <= _EPS:
        return {**base, "status": "no_new_information", "spent": spent_before}
    p = p_one_sided(mean, std, n, direction)
    if p is None:
        return {**base, "status": "untestable", "spent": spent_before}
    ub = upper_bound(mean, std, n, direction)
    return {**base, "status": "looked",
            "alpha_look": alpha_look, "spent": spent_now,
            "p": p, "p_adj": min(1.0, p * ALPHA / alpha_look),
            "upper": ub,
            "futile": ub is not None and ub < mue,
            "reversed": ub is not None and ub < 0.0,
            "final": n >= n_max}


def cohort(p_adj: Sequence[float | None], alpha: float = ALPHA) -> tuple[list[bool], list[float | None]]:
    """Benjamini-Hochberg at `alpha` over one run's looks.

    Each p_adj is the look's one-sided p scaled by ALPHA / alpha_look, so a
    lone rule is significant exactly when p <= alpha_look. Ideas withdrawn
    from a family enter at p_adj = 1 (see hypotheses.evaluate)."""
    from .stats import benjamini_hochberg
    return benjamini_hochberg(list(p_adj), q=alpha)


def describe(lk: dict | None) -> str:
    """One line for the miner, the email and the CLI."""
    if not lk:
        return ""
    if lk.get("status") != "looked":
        return f"{lk.get('status')} (n={lk.get('n')})"
    ub = lk.get("upper")
    return (f"look at n={lk['n']}/{lk['n_max']}: one-sided p={lk['p']:.4g} vs "
            f"{lk['alpha_look']:.3g} this look; upper bound "
            f"{'n/a' if ub is None else f'{ub:+.2f}R'}")
