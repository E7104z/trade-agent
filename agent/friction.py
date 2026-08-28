"""
Participation-aware execution friction.

WHY THIS MODULE EXISTS
----------------------
`config.slippage_bps: 5` is a single flat number applied to every fill in the
ledger. Five basis points per side is a reasonable assumption for SPY. It is
fiction for the names this desk actually scans: 3%+ premarket gappers in the
$10-50 band, frequently with sub-100M floats and double-digit short interest.

That matters more than it looks. If the desk's edge lands anywhere near +0.2R,
friction is a first-order term, not a rounding error. An "edge" that only exists
at 5bps and dies at 15bps was never an edge -- it was a fee estimate. The ledger
currently cannot tell those two cases apart.

MODEL
-----
Per-side cost in basis points:

    bps = base + spread_component + impact_component

    spread_component = spread_k * atr_pct
        Realized volatility is the best cheap proxy for quoted spread on a
        gapping name; a 6%-ATR small cap quotes far wider than a 1.2%-ATR
        large cap, and premarket/open quotes are wider still.

    impact_component = impact_k * sqrt(participation)
        The square-root law of market impact (Almgren et al.), which is the
        standard empirical form: cost scales with the square root of the
        fraction of daily volume you consume, not linearly.

    participation = shares / adv20

`shares` is derivable from the plan without any new data: the desk risks
`size_r * dollars_per_r` dollars over a stop distance of |entry - stop|, so
shares = size_r * dollars_per_r / |entry - stop|.

CALIBRATION HONESTY
-------------------
spread_k and impact_k are PRIORS, not measurements. The defaults below produce
roughly 12-18bps/side on a typical 3%-ATR $25 gapper at 0.2% participation and
roughly 6bps/side on a 1.2%-ATR mega cap -- consistent with published equity
TCA studies, but not calibrated against this desk's own fills (it has none; it
is a paper system). They are therefore CONFIGURABLE and, more importantly,
every ledger conclusion is meant to be re-run at 1x/2x/3x these values via
`stress_multipliers`. The stress ladder is the real defense; the point estimate
is only a starting guess.

This module computes numbers. It changes no existing behavior: `friction_model`
defaults to "flat", which reproduces the current constant-slippage semantics
exactly.
"""
from __future__ import annotations

import math
from typing import Any

__all__ = [
    "FrictionParams",
    "params_from_config",
    "shares_for_pick",
    "participation_for_pick",
    "slippage_bps_for",
    "stress_ladder",
]

# Defaults chosen so a 1.2%-ATR mega cap at negligible participation lands near
# the repo's existing flat 5bps assumption -- i.e. the model degrades gracefully
# toward current behavior on exactly the names the flat number was right for.
DEFAULT_BASE_BPS = 2.0
DEFAULT_SPREAD_K = 1.6      # bps per 1 percentage point of daily ATR
DEFAULT_IMPACT_K = 220.0    # bps per sqrt(participation)
DEFAULT_MAX_BPS = 120.0     # hard cap: beyond this the trade is not executable
DEFAULT_STRESS = (1.0, 2.0, 3.0)


class FrictionParams:
    """Immutable friction configuration. Constructed once per run."""

    __slots__ = ("base_bps", "spread_k", "impact_k", "max_bps",
                 "flat_bps", "model", "stress_multipliers")

    def __init__(
        self,
        model: str = "flat",
        flat_bps: float = 5.0,
        base_bps: float = DEFAULT_BASE_BPS,
        spread_k: float = DEFAULT_SPREAD_K,
        impact_k: float = DEFAULT_IMPACT_K,
        max_bps: float = DEFAULT_MAX_BPS,
        stress_multipliers: tuple[float, ...] = DEFAULT_STRESS,
    ) -> None:
        self.model = model if model in ("flat", "participation") else "flat"
        self.flat_bps = float(flat_bps)
        self.base_bps = float(base_bps)
        self.spread_k = float(spread_k)
        self.impact_k = float(impact_k)
        self.max_bps = float(max_bps)
        self.stress_multipliers = tuple(float(m) for m in stress_multipliers) or (1.0,)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (f"FrictionParams(model={self.model!r}, flat_bps={self.flat_bps}, "
                f"base={self.base_bps}, spread_k={self.spread_k}, "
                f"impact_k={self.impact_k}, stress={self.stress_multipliers})")


def params_from_config(cfg: Any) -> FrictionParams:
    """Build FrictionParams from a Config, with every key optional.

    Reads `friction` as a nested config block and falls back to the existing
    top-level `slippage_bps`, so a config.json that has never heard of this
    module produces exactly today's behavior.
    """
    get = getattr(cfg, "get", None)
    raw: dict = (get("friction", {}) or {}) if callable(get) else {}
    flat = (get("slippage_bps", 5.0) if callable(get) else 5.0) or 0.0
    return FrictionParams(
        model=str(raw.get("model", "flat")),
        flat_bps=float(flat),
        base_bps=float(raw.get("base_bps", DEFAULT_BASE_BPS)),
        spread_k=float(raw.get("spread_k", DEFAULT_SPREAD_K)),
        impact_k=float(raw.get("impact_k", DEFAULT_IMPACT_K)),
        max_bps=float(raw.get("max_bps", DEFAULT_MAX_BPS)),
        stress_multipliers=tuple(raw.get("stress_multipliers", DEFAULT_STRESS)),
    )


def shares_for_pick(pick: dict, dollars_per_r: float) -> float | None:
    """Share count implied by the plan's risk budget and stop distance.

    Returns None when the plan lacks a usable entry/stop pair -- an unknown
    share count must never silently become zero participation (which would
    understate cost and flatter the edge).
    """
    try:
        entry = float(pick.get("entry_price"))
        stop = float(pick.get("stop_price"))
    except (TypeError, ValueError):
        return None
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        return None
    size_r = pick.get("size_r")
    size_r = float(size_r) if isinstance(size_r, (int, float)) else 1.0
    dollars = abs(float(dollars_per_r)) * size_r
    if dollars <= 0:
        return None
    return dollars / risk_per_share


def participation_for_pick(
    pick: dict, adv20: float | None, dollars_per_r: float
) -> float | None:
    """Fraction of 20-day average daily volume this position consumes."""
    if not adv20 or adv20 <= 0:
        return None
    shares = shares_for_pick(pick, dollars_per_r)
    if shares is None:
        return None
    return shares / float(adv20)


def slippage_bps_for(
    pick: dict,
    params: FrictionParams,
    atr_pct: float | None = None,
    adv20: float | None = None,
    dollars_per_r: float = 500.0,
    stress: float = 1.0,
) -> float:
    """Per-side slippage in basis points for one pick.

    Fails OPEN toward the flat assumption: if `model` is "flat", or the inputs
    needed for the participation model are missing, this returns the configured
    flat rate (scaled by `stress`). Missing ATR or ADV must never make a trade
    look CHEAPER than the status quo assumption -- that would be a silent,
    edge-flattering degradation, the exact failure mode the indicator specs'
    null-handling rules exist to prevent.
    """
    stress = float(stress) if stress and stress > 0 else 1.0

    if params.model != "participation":
        return round(params.flat_bps * stress, 4)

    if atr_pct is None and adv20 is None:
        return round(params.flat_bps * stress, 4)

    bps = params.base_bps
    if atr_pct is not None:
        try:
            bps += params.spread_k * abs(float(atr_pct))
        except (TypeError, ValueError):
            pass
    else:
        # Unknown volatility: assume the flat model's implied spread rather
        # than zero, so a missing feed cannot cheapen the fill.
        bps += max(0.0, params.flat_bps - params.base_bps)

    part = participation_for_pick(pick, adv20, dollars_per_r)
    if part is not None and part > 0:
        bps += params.impact_k * math.sqrt(part)

    # Never model a fill as cheaper than the flat assumption -- the flat rate is
    # a floor, not a competing estimate.
    bps = max(bps, params.flat_bps)
    return round(min(bps, params.max_bps) * stress, 4)


def stress_ladder(
    pick: dict,
    params: FrictionParams,
    atr_pct: float | None = None,
    adv20: float | None = None,
    dollars_per_r: float = 500.0,
) -> dict[str, float]:
    """{"1.0x": bps, "2.0x": bps, "3.0x": bps} for the configured multipliers.

    The evening loop re-simulates each pick once per rung and stores the
    resulting R alongside the primary `r_multiple`. A rule whose edge does not
    survive the 2x rung is auto-flagged by the weekly miner.
    """
    return {
        f"{m}x": slippage_bps_for(pick, params, atr_pct, adv20, dollars_per_r, stress=m)
        for m in params.stress_multipliers
    }
