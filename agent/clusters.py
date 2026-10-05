"""
The runner lane's unit of evidence: one (session, industry) event, not one row.

WHY THIS EXISTS
---------------
The runner prices every candidate the scan finds -- hundreds a session -- and
the miner, the 2-D pairs, the win-frequency family, the evidence backlog and
forward validation all counted each candidate as one observation toward n >= 8.
Rows are not independent. On 2026-10-01 six of the ten picks were tanker and
shipping names flagged on one 10:25 tick (five of them one Yahoo industry,
"Oil & Gas Midstream"), 35 of the pool's 40 regional banks fired between 11:55
and 12:20, and three 2x Lumentum ETFs were one event counted three times. The
session's 444 triggered operating-company rows were 110 (session, industry)
units. A cell could clear n >= 8, and a rule its whole forward validation, on
one morning's tape.

So, for the runner lane only, every statistic is computed over UNITS: the rows
of one session that share an industry collapse to one value, their mean
realized R. A row with no known industry is its own unit -- an unknown is not
evidence of a duplicate. Within a unit the rows still count for the descriptive
row totals shown beside n; they just stop counting as separate evidence.

The morning lane is unchanged: it takes a handful of hand-picked trades a day,
and its rows are passed through one value per row.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

__all__ = ["cluster_key", "unit_key", "unit_values", "unit_count", "units"]


def cluster_key(row: dict) -> str:
    """The cluster a runner row belongs to within its session: its industry,
    or the ticker itself when the industry is unknown."""
    ind = str(row.get("industry") or "").strip()
    return f"industry:{ind}" if ind else f"ticker:{str(row.get('ticker') or '').upper()}"


def unit_key(row: dict) -> tuple:
    return (row.get("session_date") or row.get("plan_date"), cluster_key(row))


def units(rows: Iterable[dict], value: Callable[[dict], Any]) -> dict[tuple, list[float]]:
    """{(session, cluster): [values]} over the rows whose value is not None."""
    out: dict[tuple, list[float]] = {}
    for r in rows:
        v = value(r)
        if v is None:
            continue
        out.setdefault(unit_key(r), []).append(float(v))
    return out


# Lanes whose rows are a FIELD (every instance the scan finds, many per
# session), so a session's rows in one industry are one event. The gap field
# joined the runner here (EDGE_ACCELERATION_SPEC_v1 Part 4).
UNIT_LANES = ("runner", "gapfield")


def unit_values(rows: Iterable[dict], value: Callable[[dict], Any],
                lane: str = "runner") -> list[float]:
    """One value per unit (the mean of its rows) for a field lane; one per
    row for any other lane."""
    if lane not in UNIT_LANES:
        vals = (value(r) for r in rows)
        return [float(v) for v in vals if v is not None]
    return [sum(v) / len(v) for v in units(rows, value).values()]


def unit_count(rows: Iterable[dict], lane: str = "runner") -> int:
    """How many independent observations `rows` are."""
    rows = list(rows)
    if lane not in UNIT_LANES:
        return len(rows)
    return len({unit_key(r) for r in rows})
