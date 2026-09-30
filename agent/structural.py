"""
Structural mining: winners' shared attributes, long and short, under one FDR batch
(STRUCTURAL_MINING_SPEC_v1).

WHY THIS EXISTS
---------------
The directive asks for an exhaustive search for the STRUCTURAL attributes of
winners -- across setups, regimes, lanes, and both directions -- with no false
edge. The weekly search was one-dimensional (every comparison n_dimensions=1),
tested only mean R, never asked whether a finding held within each stratum
(which is what separates a structural attribute from one hot week), and never
mined the largest short-side corpus the system owns: the population monkeys,
which grade every premarket gapper both long and short every day.

Every family here is a set of LOOKS in the SAME Benjamini-Hochberg batch as the
existing 1-D search (integrity.build_search), recorded in research_looks, and
counted in the width table. Widening the search raises every q-value; that is
the correct price, and nothing here is exempt from it.

  PAIRS        pre-registered 2-D cells (PAIR_SPEC, declared in code before
               looking; changed only by commit), computed only when BOTH
               marginal dimensions have >= 2 levels with n >= 8 -- a rule
               stated in advance, not chosen after seeing the cells
  WIN RATE     exact Fisher test per 1-D cell: win frequency in the cell vs
               the rest of its lane. "Win" is the ledger's existing
               definition (triggered, fill-valid, realized R > 0)
  POPULATION   mechanical-rule evidence over the whole gap population, arm x
               direction always separate -- long and short never share a cell
  CONSISTENCY  descriptive only (adds no looks): for each surviving finding,
               its sign and n within every stratum; `structural` only if the
               sign holds in every stratum with n >= 8, and there are >= 2
"""
from __future__ import annotations

import math
from typing import Any

from .stats import Comparison, fisher_exact_2x2

__all__ = ["PAIR_SPEC_VERSION", "PAIR_SPEC", "PAIR_SEP", "lane_rows", "pair_comparisons",
           "winrate_comparisons", "population_rows", "population_comparisons",
           "consistency", "consistency_md", "width_md", "pair_dimension"]

PAIR_SPEC_VERSION = 1
PAIR_SPEC: tuple[tuple[str, str, str], ...] = (
    ("morning", "setup_type", "regime_tag"),
    ("morning", "direction", "trend_side_200d"),
    ("morning", "direction", "regime_tag"),
    ("morning", "setup_type", "stop_atr_bucket"),
    ("runner", "runner_stop_atr_bucket", "runner_rvol_bucket"),
    ("runner", "runner_regime", "runner_detection_bucket"),
    ("runner", "runner_trend_side_200d", "runner_tc_bucket"),
)
PAIR_SEP = "|"
MIN_STRATA = 2

# Dimensions that exist in both lanes (agent/buckets.py), so a morning finding
# can be read in the runner field and vice versa as one more stratum. The
# runner stop-ATR dimension subdivides the morning "<0.5" cell; the read goes
# through buckets.RUNNER_STOP_ATR_COARSE (see `consistency`).
CROSS_LANE = {"stop_atr_bucket": "runner_stop_atr_bucket",
              "trend_side_200d": "runner_trend_side_200d"}
CROSS_LANE.update({v: k for k, v in list(CROSS_LANE.items())})


def _r(row: dict) -> float:
    """Realized R, on the same measure as the 1-D search (research.realized_r):
    planned-risk R where the row carries it. Pairs and win frequencies read the
    legacy r_multiple while the 1-D cells beside them read r_planned, so one
    batch compared two different denominators."""
    from .research import realized_r
    return float(realized_r(row))


def pair_dimension(dims: list[str]) -> str:
    return PAIR_SEP.join(sorted(dims))


def _mean_std(values: list[float]) -> tuple[float | None, float | None]:
    n = len(values)
    if not n:
        return None, None
    m = sum(values) / n
    if n < 2:
        return m, None
    return m, math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))


def lane_rows(con: Any) -> dict[str, list[dict]]:
    """Fill-valid triggered rows per lane (current scan/sim version for runner)."""
    from . import research
    out: dict[str, list[dict]] = {}
    for lane, fn in (("morning", research.morning_rows), ("runner", research.runner_rows)):
        try:
            out[lane] = fn(con)
        except Exception:
            out[lane] = []
    return out


def _level_counts(rows: list[dict], lane: str, dim: str) -> dict[str, int]:
    from . import buckets
    counts: dict[str, int] = {}
    for r in rows:
        lev = buckets.label(lane, dim, r)
        if lev != "?":
            counts[lev] = counts.get(lev, 0) + 1
    return counts


def admissible(rows: list[dict], lane: str, dim: str, min_n: int = 8) -> bool:
    return sum(1 for n in _level_counts(rows, lane, dim).values() if n >= min_n) >= 2


def pair_comparisons(rows_by_lane: dict[str, list[dict]], min_n: int = 8
                     ) -> tuple[list[Comparison], list[dict]]:
    """Joint cells for every ADMISSIBLE pre-registered pair, plus a status list."""
    from . import buckets
    comps: list[Comparison] = []
    status: list[dict] = []
    for lane, a, b in PAIR_SPEC:
        rows = rows_by_lane.get(lane) or []
        dims = sorted([a, b])
        if not (admissible(rows, lane, a, min_n) and admissible(rows, lane, b, min_n)):
            status.append({"lane": lane, "pair": pair_dimension(dims), "admitted": False})
            continue
        status.append({"lane": lane, "pair": pair_dimension(dims), "admitted": True})
        cells: dict[tuple, list[float]] = {}
        for r in rows:
            levels = tuple(buckets.label(lane, d, r) for d in dims)
            if "?" in levels:
                continue
            cells.setdefault(levels, []).append(_r(r))
        for levels, values in sorted(cells.items()):
            m, s = _mean_std(values)
            comps.append(Comparison(dimension=pair_dimension(dims), level=PAIR_SEP.join(levels),
                                    n=len(values), mean_r=m, std_r=s, n_dimensions=2,
                                    note=f"pre-registered pair (PAIR_SPEC v{PAIR_SPEC_VERSION})"))
    return comps, status


def winrate_comparisons(rows_by_lane: dict[str, list[dict]]) -> list[Comparison]:
    """Exact Fisher test per 1-D cell: win frequency in the cell vs the rest of the lane."""
    from . import buckets
    comps: list[Comparison] = []
    for lane, rows in rows_by_lane.items():
        if not rows:
            continue
        wins_total = sum(1 for r in rows if _r(r) > 0)
        n_total = len(rows)
        for dim in buckets.dimensions(lane):
            cells: dict[str, list[int]] = {}
            for r in rows:
                lev = buckets.label(lane, dim, r)
                if lev == "?":
                    continue
                c = cells.setdefault(lev, [0, 0])
                c[0] += 1 if _r(r) > 0 else 0
                c[1] += 1
            for lev, (w, n) in sorted(cells.items()):
                a, b = w, n - w
                c_, d_ = wins_total - w, (n_total - wins_total) - (n - w)
                comps.append(Comparison(
                    dimension=f"winrate:{dim}", level=lev, n=n, mean_r=None, std_r=None,
                    p_value=fisher_exact_2x2(a, b, c_, d_), test="fisher",
                    note=f"{lane} lane: win frequency in cell vs rest of lane (exact)"))
    return comps


def population_rows(con: Any) -> list[dict]:
    """Population-monkey outcomes joined to the snapshot, current sim_version only.

    The population simulator keeps its OWN version line
    (population.POPULATION_SIM_VERSION), stamped on every population_outcomes
    row. This filtered on grading.CURRENT_SIM_VERSION instead -- 4 against rows
    stamped 1 -- so from S7's first run the population family (the short side)
    entered the weekly batch with zero rows.
    """
    from .population import POPULATION_SIM_VERSION
    rows = con.execute(
        """SELECT o.plan_date, o.symbol, o.arm, o.direction, o.r_multiple,
                  u.gap_pct, u.atr_pct, u.in_scan_universe, u.was_picked
           FROM population_outcomes o
           LEFT JOIN population_universe u ON u.plan_date = o.plan_date AND u.symbol = o.symbol
           WHERE o.r_multiple IS NOT NULL AND o.sim_version = ?""",
        [POPULATION_SIM_VERSION]).fetchall()
    regimes: dict = {}
    try:
        regimes = dict(con.execute("SELECT plan_date, regime_tag FROM daily_plans").fetchall())
    except Exception:
        pass
    keys = ("plan_date", "symbol", "arm", "direction", "r_multiple", "gap_pct", "atr_pct",
            "in_scan_universe", "was_picked")
    out = []
    for r in rows:
        row = dict(zip(keys, r))
        row["regime_tag"] = regimes.get(row["plan_date"])
        out.append(row)
    return out


def _pop_label(dim: str, row: dict) -> str:
    if dim == "pop_gap_atr_bucket":
        g, a = row.get("gap_pct"), row.get("atr_pct")
        if g is None or not a:
            return "?"
        v = abs(float(g)) / float(a)     # gap in ATR units; same edges as gap_atr_bucket
        return "<1" if v < 1 else ("1-2" if v < 2 else ">2")
    if dim in ("pop_in_scan_universe", "pop_was_picked"):
        v = row.get(dim[4:])
        return "?" if v is None else ("true" if v else "false")
    if dim == "pop_regime":
        return row.get("regime_tag") or "?"
    return "?"


POP_DIMS = ("pop_gap_atr_bucket", "pop_in_scan_universe", "pop_regime", "pop_was_picked")


def population_comparisons(rows: list[dict]) -> list[Comparison]:
    """Mean R per (arm, direction, dimension level). Long and short never pool."""
    cells: dict[tuple, list[float]] = {}
    for r in rows:
        arm, direction = r.get("arm"), r.get("direction")
        if not arm or not direction:
            continue
        for dim in POP_DIMS:
            lev = _pop_label(dim, r)
            if lev == "?":
                continue
            cells.setdefault((arm, direction, dim, lev), []).append(float(r["r_multiple"]))
    comps = []
    for (arm, direction, dim, lev), values in sorted(cells.items()):
        m, s = _mean_std(values)
        comps.append(Comparison(
            dimension=f"population:{arm}:{direction}:{dim}", level=lev, n=len(values),
            mean_r=m, std_r=s,
            note="mechanical-rule evidence over the gap population, not desk selection"))
    return comps


# --------------------------------------------------------------------------
# Consistency (descriptive -- adds no looks)
# --------------------------------------------------------------------------

def _row_date(r: dict):
    return r.get("plan_date") or r.get("session_date")


def _parse(dim: str) -> tuple[str, list[str]] | None:
    """(lane, [dims]) for a 1-D or pair comparison this check can stratify."""
    from . import buckets
    dims = dim.split(PAIR_SEP)
    for lane in ("morning", "runner"):
        if all(d in buckets.dimensions(lane) for d in dims):
            return lane, dims
    return None


def consistency(search: Any, rows_by_lane: dict[str, list[dict]], min_n: int = 8) -> list[dict]:
    """For each significant mean-R finding: its sign and n in every stratum.

    Strata: regime, direction (morning), calendar month, and -- for a
    dimension that exists in both lanes with the same buckets -- the other
    lane. Only SIGN and n are reported, never a stratum mean: this is a
    robustness read on a finding the batch already admitted, not a new test.
    """
    from . import buckets
    out: list[dict] = []
    for c in getattr(search, "survivors", None) or []:
        if c.test != "one_sample" or c.mean_r is None:
            continue
        parsed = _parse(c.dimension)
        if parsed is None:
            continue
        lane, dims = parsed
        levels = c.level.split(PAIR_SEP)
        want = 1 if c.mean_r > 0 else -1
        cell = [r for r in rows_by_lane.get(lane) or []
                if all(buckets.label(lane, d, r) == lv for d, lv in zip(dims, levels))]
        strata: dict[str, list[float]] = {}
        for r in cell:
            v = _r(r)
            if r.get("regime_tag"):
                strata.setdefault(f"regime={r['regime_tag']}", []).append(v)
            if lane == "morning" and r.get("direction"):
                strata.setdefault(f"direction={r['direction']}", []).append(v)
            d = _row_date(r)
            if d is not None:
                strata.setdefault(f"month={str(d)[:7]}", []).append(v)
        if len(dims) == 1 and dims[0] in CROSS_LANE:
            other_dim = CROSS_LANE[dims[0]]
            other_lane = "runner" if lane == "morning" else "morning"
            # The runner lane splits the morning "<0.5" stop cell into finer
            # labels; read a morning cell in the runner field through the
            # coarse map. A runner-only label has no morning counterpart.
            coarse = buckets.RUNNER_STOP_ATR_COARSE
            if other_lane == "runner":
                def _hit(r, dim=other_dim):
                    lab = buckets.label("runner", dim, r)
                    return coarse.get(lab, lab) == levels[0]
            else:
                def _hit(r, dim=other_dim):
                    return buckets.label("morning", dim, r) == levels[0]
            other = ([] if (lane == "runner" and levels[0] in coarse) else
                     [_r(r) for r in rows_by_lane.get(other_lane) or [] if _hit(r)])
            if other:
                strata[f"lane={other_lane}"] = other
        powered = {k: v for k, v in strata.items() if len(v) >= min_n}
        agree = [k for k, v in powered.items() if (1 if sum(v) / len(v) > 0 else -1) == want]
        out.append({"dimension": c.dimension, "level": c.level, "strata": len(powered),
                    "consistent": len(agree),
                    "structural": len(powered) >= MIN_STRATA and len(agree) == len(powered),
                    "detail": {k: {"n": len(v), "sign": "+" if sum(v) > 0 else "-"}
                               for k, v in sorted(powered.items())}})
    return out


def consistency_md(results: list[dict]) -> str:
    if not results:
        return ""
    lines = [
        "### Structural consistency of this run's surviving findings (descriptive — no new looks)\n",
        "A finding may be called STRUCTURAL only if it survived FDR above AND its sign holds "
        "in every stratum with n >= 8 (regime, direction, month, and the other lane where the "
        "dimension is shared), with at least two such strata. Otherwise it is a POOLED "
        "finding: say so. Strata show sign and n only.\n",
    ]
    for r in results:
        verdict = "STRUCTURAL" if r["structural"] else "pooled, not structural"
        detail = ", ".join(f"{k} n={v['n']} {v['sign']}" for k, v in r["detail"].items()) or "no stratum at n>=8"
        lines.append(f"- {r['dimension']} = {r['level']}: consistent in {r['consistent']}/"
                     f"{r['strata']} strata with n>=8 -> **{verdict}** ({detail})")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Search width
# --------------------------------------------------------------------------

def _family(dim: str) -> str:
    if dim.startswith("population:"):
        return "population (mechanical, long/short separate)"
    if dim.startswith("winrate:"):
        return "winner frequency (Fisher exact)"
    if PAIR_SEP in dim:
        return "2-D pre-registered pairs"
    if dim in ("runner_setup_edge", "runner_selection_edge"):
        return "runner setup/selection edge"
    if dim.startswith("runner_"):
        return "runner 1-D"
    return "morning 1-D"


def width_md(search: Any, pair_status: list[dict] | None = None) -> str:
    comps = list(getattr(search, "comparisons", None) or [])
    if not comps:
        return ""
    fam: dict[str, list[int]] = {}
    for c in comps:
        f = fam.setdefault(_family(str(c.dimension)), [0, 0, 0])
        f[0] += 1
        f[1] += 1 if c.p_value is not None else 0
        f[2] += 1 if c.significant else 0
    m = sum(v[1] for v in fam.values())
    lines = ["### Search width by family (every row is a look in ONE FDR batch)\n",
             "| family | comparisons | testable | survivors |", "|---|--:|--:|--:|"]
    for name, (n, t, s) in sorted(fam.items()):
        lines.append(f"| {name} | {n} | {t} | {s} |")
    lines.append(f"\nTestable m = {m}. Uncorrected at alpha=0.05 that search would produce about "
                 f"{0.05 * m:.1f} false findings from chance alone. A wider search is not more "
                 "evidence: the q-values already price the width — cite them.")
    if pair_status:
        waiting = [p for p in pair_status if not p["admitted"]]
        if waiting:
            lines.append("\nDeclared 2-D pairs not yet admissible (a marginal has < 2 levels "
                         "with n >= 8): " + ", ".join(f"{p['lane']}:{p['pair']}" for p in waiting))
    return "\n".join(lines)
