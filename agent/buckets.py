"""
The single source of truth for every mined bucket (FORWARD_VALIDATION_SPEC_v1 Phase 1).

WHY THIS EXISTS
---------------
A quarantined rule is a statement about a CELL -- `stop_atr_bucket=0.5-1`,
`runner_rvol_bucket=2-3` -- and forward validation means deciding, trade by
trade, whether a new trade falls in that cell. Before this module, the
morning lane's cells were SQL CASE expressions in ledger.py and the runner
lane's were a Python function in runner_research.py. Code that matched a
trade to a rule would have been a third copy, free to put a 1.0-ATR stop in a
different bucket than the one the miner was shown -- and a rule validated on
trades the weekly search would not have counted as its own.

So each dimension is DECLARED ONCE, as data. The SQL the weekly search groups
by and the Python label the evening matcher uses are both DERIVED from that
declaration, and a parity test runs the two over the same boundary values.

No bucket edge moved in this change; the parity test also pins the generated
SQL against the expressions it replaced.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

__all__ = [
    "MORNING_PICK_DIMS", "MORNING_OUTCOME_DIMS", "MORNING_DIMS", "RUNNER_DIMS",
    "LANES", "sql", "label", "runner_label", "dimensions", "RUNNER_STOP_ATR_COARSE",
]


@dataclass(frozen=True)
class _Numeric:
    column: str
    cuts: tuple[tuple[str, float, str], ...]   # (op, value, label), first match wins
    else_label: str
    alias: str = "o"


@dataclass(frozen=True)
class _Passthrough:
    column: str
    alias: str
    cast: bool = False


@dataclass(frozen=True)
class _Custom:
    sql: str
    py: Callable[[dict], str]


def _or_alignment(row: dict) -> str:
    brk, fvo, direction = row.get("or_break_dir"), row.get("fill_vs_or"), row.get("direction")
    if brk is None or fvo is None:
        return "?"
    if (direction == "long" and brk == "up") or (direction == "short" and brk == "down"):
        return "with"
    if (direction == "long" and brk == "down") or (direction == "short" and brk == "up"):
        return "against"
    return "?"


# --------------------------------------------------------------- morning lane
# picks columns (grouped with p.<dim>)
MORNING_PICK_DIMS: dict[str, Any] = {
    d: _Passthrough(d, "p", cast=True)
    for d in ("setup_type", "regime_tag", "direction", "conviction", "in_scan_universe")
}
# STRUCTURAL_MINING_SPEC_v1 Phase 5: exploration picks (R:R controls, backlog
# probes) are a MEASURED dimension, so their effect is seen rather than pooled
# into the desk's record. A regular pick labels as `none`, not `?`: it was not
# unmeasured, it simply was not exploration.
MORNING_PICK_DIMS["exploration_lane"] = _Custom(
    "COALESCE(p.exploration_lane, 'none')",
    lambda row: row.get("exploration_lane") or "none")

# outcomes columns (computed at grading time)
MORNING_OUTCOME_DIMS: dict[str, Any] = {
    "rvol_bucket": _Numeric("rvol_at_1000", (("<", 1, "<1x"), ("<", 2, "1-2x")), ">2x"),
    "vwap_side_at_fill": _Passthrough("vwap_side_at_fill", "o"),
    # Q9: stop distance sweet spot
    "stop_atr_bucket": _Numeric("stop_dist_atr",
                                (("<", 0.5, "<0.5"), ("<", 1, "0.5-1"), ("<", 2, "1-2")), ">2"),
    # Q10: blue-sky (small gap/ATR) vs overhead-supply (large gap/ATR)
    "gap_atr_bucket": _Numeric("gap_atr", (("<", 1, "<1"), ("<", 2, "1-2")), ">2"),
    "ext_bucket": _Numeric("ext_atr_at_entry", (("<", -2, "<-2"), ("<=", 2, "-2..+2")), ">+2"),
    # Q11: trend alignment (the 200d question)
    "trend_side_200d": _Passthrough("trend_side_200d", "o"),
    "mcap_bucket": _Numeric("mkt_cap", (("<", 2e9, "<2B"), ("<", 10e9, "2-10B")), ">10B"),
    # Q12: shortedness
    "short_bucket": _Numeric("short_pct_float", (("<", 5, "<5%"), ("<", 15, "5-15%")), ">15%"),
    "or_alignment": _Custom(
        "CASE WHEN o.or_break_dir IS NULL OR o.fill_vs_or IS NULL THEN '?' "
        "WHEN (p.direction='long' AND o.or_break_dir='up') "
        "  OR (p.direction='short' AND o.or_break_dir='down') THEN 'with' "
        "WHEN (p.direction='long' AND o.or_break_dir='down') "
        "  OR (p.direction='short' AND o.or_break_dir='up') THEN 'against' "
        "ELSE '?' END",
        _or_alignment),
}

MORNING_DIMS: dict[str, Any] = {**MORNING_PICK_DIMS, **MORNING_OUTCOME_DIMS}


def _num_sql(n: float) -> str:
    return repr(float(n)) if not float(n).is_integer() or abs(n) >= 1e6 else str(int(n))


def sql(dim: str) -> str:
    """The SQL expression the weekly search groups `dim` by."""
    spec = MORNING_DIMS[dim]
    if isinstance(spec, _Passthrough):
        col = f"{spec.alias}.{spec.column}"
        return f"COALESCE(CAST({col} AS VARCHAR),'?')" if spec.cast else f"COALESCE({col}, '?')"
    if isinstance(spec, _Numeric):
        col = f"{spec.alias}.{spec.column}"
        parts = [f"CASE WHEN {col} IS NULL THEN '?'"]
        for op, value, lab in spec.cuts:
            parts.append(f"WHEN {col} {op} {_num_sql(value)} THEN '{lab}'")
        parts.append(f"ELSE '{spec.else_label}' END")
        return " ".join(parts)
    return spec.sql


def _passthrough_py(value: Any, cast: bool) -> str:
    if value is None:
        return "?"
    if cast and isinstance(value, bool):
        return "true" if value else "false"   # CAST(boolean AS VARCHAR), both engines
    return str(value)


def _morning_label(dim: str, row: dict) -> str:
    spec = MORNING_DIMS[dim]
    if isinstance(spec, _Passthrough):
        return _passthrough_py(row.get(spec.column), spec.cast)
    if isinstance(spec, _Numeric):
        v = row.get(spec.column)
        if v is None:
            return "?"
        v = float(v)
        for op, value, lab in spec.cuts:
            if (op == "<" and v < value) or (op == "<=" and v <= value):
                return lab
        return spec.else_label
    return spec.py(row)


# ---------------------------------------------------------------- runner lane

RUNNER_DIMS: tuple[str, ...] = (
    "runner_detection_bucket",
    "runner_rvol_bucket",
    "runner_price_tier",
    "runner_vwap_bucket",
    "runner_slope_bucket",
    "runner_tc_bucket",
    "runner_float_bucket",
    "runner_stop_atr_bucket",
    "runner_trend_side_200d",
    "runner_regime",
)


def runner_label(dimension: str, row: dict) -> str:
    """Deterministic bucket label for one runner candidate row.

    '?' is the null/unmeasured bucket and is kept rather than dropped — a large
    '?' bucket is itself a finding about data coverage, and silently discarding
    it would make coverage gaps invisible.
    """
    def _b(value, edges, labels):
        if value is None:
            return "?"
        for edge, lab in zip(edges, labels):
            if value < edge:
                return lab
        return labels[-1]

    if dimension == "runner_detection_bucket":
        return _b(row.get("detection_minutes"), [600, 630, 660, 720],
                  ["pre-10:00", "10:00-10:30", "10:30-11:00", "11:00-12:00", "12:00+"])
    if dimension == "runner_rvol_bucket":
        # The lowest label used to be "1.5-2", written when the scan gated on
        # RVOL >= 1.5. The gate has been off (min_rvol 0) since the RVOL scale
        # was corrected, so every candidate under 2.0x -- about nine in ten,
        # median ~0.5x -- was filed as "1.5-2": one label over the whole lane.
        return _b(row.get("rvol"), [0.5, 1.0, 1.5, 2.0, 3.0, 5.0],
                  ["<0.5", "0.5-1", "1-1.5", "1.5-2", "2-3", "3-5", "5+"])
    if dimension == "runner_price_tier":
        return row.get("price_tier") or "?"
    if dimension == "runner_vwap_bucket":
        return _b(row.get("vwap_pct_above"), [1.0, 1.5, 2.5],
                  ["0.5-1", "1-1.5", "1.5-2.5", "2.5+"])
    if dimension == "runner_slope_bucket":
        # Slope normalized by price: a $0.04 slope means something different on
        # a $6 stock than on a $45 one, so the raw dollar value is not
        # comparable across the price band.
        slope, price = row.get("ema9_slope"), row.get("detection_price")
        norm = (slope / price * 100) if (slope is not None and price) else None
        return _b(norm, [0.05, 0.10, 0.20], ["<0.05%", "0.05-0.10%", "0.10-0.20%", "0.20%+"])
    if dimension == "runner_tc_bucket":
        return _b(row.get("trend_consistency"), [55, 65, 75, 85],
                  ["45-55", "55-65", "65-75", "75-85", "85+"])
    if dimension == "runner_float_bucket":
        return _b(row.get("float_shares"), [20, 50, 100],
                  ["<20M", "20-50M", "50-100M", "100M+"])
    if dimension == "runner_stop_atr_bucket":
        # The runner stop is the 5-minute 9 EMA measured against a DAILY
        # ATR14, so it sits under 0.5 ATR by construction (median stop ~0.8%
        # of price): with the morning lane's edges ~99% of candidates shared
        # one cell and the dimension compared nothing. The morning band
        # "<0.5" is split into three; the wider labels are unchanged, and
        # RUNNER_STOP_ATR_COARSE maps the split back for cross-lane reads.
        return _b(row.get("stop_dist_atr"), [0.15, 0.3, 0.5, 1.0, 2.0],
                  ["<0.15", "0.15-0.3", "0.3-0.5", "0.5-1", "1-2", ">2"])
    if dimension == "runner_trend_side_200d":
        return row.get("trend_side_200d") or "?"
    if dimension == "runner_regime":
        return row.get("regime_tag") or "?"
    return "?"


# Runner stop-ATR labels that subdivide the morning lane's "<0.5" cell, mapped
# back to it so a morning finding can still be read in the runner field.
RUNNER_STOP_ATR_COARSE = {"<0.15": "<0.5", "0.15-0.3": "<0.5", "0.3-0.5": "<0.5"}


# ------------------------------------------------------------------- unified

LANES = ("morning", "runner")


def dimensions(lane: str) -> tuple[str, ...]:
    if lane == "morning":
        return tuple(MORNING_DIMS)
    if lane == "runner":
        return RUNNER_DIMS
    return ()


def label(lane: str, dimension: str, row: dict) -> str:
    """The bucket `row` falls in for `dimension`, exactly as the weekly search
    would have counted it. `row` is a merged pick+outcome dict for the morning
    lane, a joined candidate+outcome dict for the runner lane."""
    if lane == "morning" and dimension in MORNING_DIMS:
        return _morning_label(dimension, row)
    if lane == "runner" and dimension in RUNNER_DIMS:
        return runner_label(dimension, row)
    return "?"
