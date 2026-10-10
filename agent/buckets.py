"""
The single source of truth for every mined bucket (FORWARD_VALIDATION_SPEC_v1 Phase 1).

WHY THIS EXISTS
---------------
A quarantined rule is a statement about a CELL -- `stop_atr_bucket=0.5-1`,
`gf_gap_atr=1-2` -- and forward validation means deciding, trade by
trade, whether a new trade falls in that cell. Before this module, the
morning lane's cells were SQL CASE expressions in ledger.py. Code that matched
a trade to a rule would have been a second copy, free to put a 1.0-ATR stop in a
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
    "MORNING_PICK_DIMS", "MORNING_OUTCOME_DIMS", "MORNING_DIMS",
    "LANES", "sql", "label", "dimensions",
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


# ------------------------------------------------------------------- unified

LANES = ("morning", "gapfield")


def _gapfield_dims() -> tuple[str, ...]:
    # The gap field's dimensions are declared in agent/gapfield.py, next to the
    # rows they label (EDGE_ACCELERATION_SPEC_v1 Part 4).
    from .gapfield import DIMS, STRATEGY_DIM
    return (STRATEGY_DIM,) + tuple(DIMS)


def dimensions(lane: str) -> tuple[str, ...]:
    if lane == "morning":
        return tuple(MORNING_DIMS)
    if lane == "gapfield":
        return _gapfield_dims()
    return ()


def label(lane: str, dimension: str, row: dict) -> str:
    """The bucket `row` falls in for `dimension`, exactly as the weekly search
    would have counted it. `row` is a merged pick+outcome dict for the morning
    lane."""
    if lane == "morning" and dimension in MORNING_DIMS:
        return _morning_label(dimension, row)
    if lane == "gapfield" and dimension in _gapfield_dims():
        from .gapfield import label as _gapfield_label
        return _gapfield_label(dimension, row)
    return "?"
