"""
The unconditional population baseline — the honest null model.

WHY THIS MODULE EXISTS
----------------------
`outcomes.baseline_r` is the naive same-name open->close return in the pick's
direction. It is a good counterfactual, and it answers a real question: did the
entry/stop/target machinery beat simply holding these names?

But it is conditioned on selection. The names in it are the names the analyst
already chose. It cannot answer the question that actually decides whether this
desk has edge:

    Did our NAME SELECTION beat the opportunity set we selected from?

If the whole premarket gap population pays +0.3R under a mechanical rule and the
desk's picks average +0.25R, the desk is destroying value while showing a
positive equity curve and a positive edge_vs_baseline_r. Nothing currently in
the ledger would catch that.

WHAT THIS DOES
--------------
Morning: snapshot the ENTIRE gap-scan population (before any LLM sees it) to
`population_universe`, flagged with which symbols were subsequently picked.
Evening: run two mechanical monkeys over every symbol in that snapshot:

    momentum : trade WITH the gap (long a gap up, short a gap down)
    fade     : trade AGAINST the gap

both with a 1-ATR stop and an at-the-close exit, entered at the open.

The gap between the desk's avg R and the better monkey's avg R is the desk's
actual selection edge.

PRECISION LIMITS — STATED, NOT HIDDEN
-------------------------------------
The population sim uses DAILY bars, not minute bars. Fetching 30 symbols of
1-minute data every evening would multiply the loop's data cost for a control
arm, which is not a good trade. The consequence is a real and specific
imprecision: within a single daily bar we cannot know whether the low preceded
the close, so a stop touch is resolved conservatively (stop assumed hit at the
stop price whenever the bar's range reaches it). This BIASES THE MONKEY DOWNWARD
— it books the stop on days where price dipped and recovered.

That bias direction is deliberate and is the safe one: it makes the population
baseline easier for the desk to beat. If the desk still fails to beat a monkey
that is being handicapped, the finding is strong. If the desk beats it only
narrowly, the margin should be treated as an upper bound, not a result. Both
readings are stated in the rendered markdown so the miner cannot forget them.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

__all__ = [
    "MIN_VERDICT_N",
    "POPULATION_SIM_VERSION",
    "snapshot_universe",
    "simulate_population_row",
    "grade_population",
    "population_stats",
    "population_md",
]

# Triggered desk trades required before the selection edge is allowed to carry
# a direction. Matches the playbook's own n>=8 reporting floor: the number is
# computable at n=1 and meaningless there, and a computable meaningless number
# printed beside the word "DESTROYING" is read as a finding.
MIN_VERDICT_N = 8

# Bumped independently of grading.CURRENT_SIM_VERSION: this is a different
# simulator with different (coarser) semantics, and conflating the two version
# lines would let a change in one look like a change in the other.
POPULATION_SIM_VERSION = 1

MAX_SYMBOLS_PER_DAY = 40   # bounds the evening fetch; gap scan returns <= 30

# Index ETFs ride along in the gap scan as market CONTEXT, never as candidates,
# so they must not dilute the population the desk was actually choosing among.
# Sourced from providers when importable; the literal is a resilience fallback
# so this control arm cannot be broken by an import problem elsewhere.
_INDEX_FALLBACK = frozenset({"SPY", "QQQ", "IWM"})


def _index_etfs() -> frozenset[str]:
    try:
        from .providers import INDEX_ETFS
        return frozenset(str(s).upper() for s in INDEX_ETFS)
    except Exception:
        return _INDEX_FALLBACK


def _excluded_products() -> frozenset[str]:
    """Leveraged/inverse/volatility products (providers.EXCLUDED_PRODUCTS).
    Excluded for the same reason as index ETFs: they move on mechanical
    leverage and decay, not company catalysts, so they are not names the desk
    was genuinely choosing among — leaving them in inflates the monkey
    baseline's variance and biases selection_edge_r. Fail-open on import
    problems (empty set = old behavior), never break the control arm."""
    try:
        from .providers import EXCLUDED_PRODUCTS
        return frozenset(str(s).upper() for s in EXCLUDED_PRODUCTS)
    except Exception:
        return frozenset()


def _is_common_stock(sym: str) -> bool:
    """Shape check for warrants/units/rights (providers.is_common_stock_symbol).
    Fail-open: if the import breaks, treat everything as common stock rather
    than silently shrinking the population."""
    try:
        from .providers import is_common_stock_symbol
        return is_common_stock_symbol(sym)
    except Exception:
        return True


def _log(scope: str, msg: str) -> None:
    """Log through the repo logger when available; never raise from a control arm."""
    try:
        from .util import log
        log(scope, msg)
    except Exception:
        pass


def snapshot_universe(
    con: Any,
    plan_date: date,
    gappers: Iterable[dict],
    picked_tickers: Iterable[str] = (),
    max_symbols: int = MAX_SYMBOLS_PER_DAY,
) -> int:
    """Persist the morning's full gap-scan population. Returns rows written.

    Called from the morning loop AFTER the bundle is built and AFTER the plan is
    validated, so `was_picked` is accurate — but the rows themselves are the
    pre-selection population, which is the whole point. Idempotent per date.

    Index ETFs are excluded: they are in the gap scan as market context, never
    as candidates, so including them would dilute the population with rows the
    desk was never choosing among.
    """
    index_etfs = _index_etfs()
    excluded_products = _excluded_products()
    picked = {str(t).upper() for t in picked_tickers if t}
    con.execute("DELETE FROM population_universe WHERE plan_date = ?", [plan_date])

    written = 0
    now = datetime.now()
    for g in list(gappers)[:max_symbols]:
        sym = str(g.get("symbol", "")).upper().strip()
        if not sym or sym in index_etfs:
            continue
        # Leveraged/inverse/volatility products and structural instruments
        # (warrants/units/rights) are not candidates the desk was choosing
        # among — same rationale as the index-ETF exclusion above.
        if sym in excluded_products or not _is_common_stock(sym):
            continue
        last, prev = g.get("last"), g.get("prev_close")
        if last is None or prev is None:
            continue
        atr14 = g.get("atr14")
        atr_pct = g.get("atr_pct")
        if atr_pct is None and atr14 and last:
            try:
                atr_pct = round(float(atr14) / float(last) * 100, 3)
            except (TypeError, ZeroDivisionError):
                atr_pct = None
        con.execute(
            """INSERT INTO population_universe
               (plan_date, symbol, last, prev_close, gap_pct, atr14, atr_pct,
                adv20, in_scan_universe, was_picked, captured_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [plan_date, sym, _f(last), _f(prev), _f(g.get("gap_pct")), _f(atr14),
             _f(atr_pct), _f(g.get("adv20")),
             bool(g.get("in_scan_universe", True)), sym in picked, now],
        )
        written += 1
    return written


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def simulate_population_row(
    row: dict, daily_bar: dict | None, arm: str = "momentum",
    slippage_bps: float = 0.0,
) -> dict:
    """Mechanical monkey for one symbol on one day. Pure function.

    Rules, fixed and deliberately dumb:
      - direction: momentum trades WITH the gap sign, fade trades AGAINST it
      - entry at the session open
      - stop 1 ATR away
      - otherwise exit at the close
      - if the bar's range reaches the stop, the stop is booked (conservative;
        see the module docstring on the downward bias this creates)

    Returns a row shaped for `population_outcomes`. `r_multiple` is denominated
    in ATRs, which IS the risk unit here since the stop is 1 ATR wide -- so it
    is directly comparable to the desk's R-multiples.
    """
    out = {
        "symbol": row.get("symbol"), "arm": arm, "direction": None,
        "entry_price": None, "stop_price": None, "exit_price": None,
        "exit_reason": "no_data", "r_multiple": None,
        "day_open": None, "day_high": None, "day_low": None, "day_close": None,
        "sim_version": POPULATION_SIM_VERSION,
    }
    atr = _f(row.get("atr14"))
    gap = _f(row.get("gap_pct"))
    if not daily_bar or atr is None or atr <= 0 or gap is None or gap == 0:
        return out

    o = _f(daily_bar.get("open"))
    h = _f(daily_bar.get("high"))
    lo = _f(daily_bar.get("low"))
    c = _f(daily_bar.get("close"))
    if None in (o, h, lo, c) or o <= 0:
        return out

    with_gap = gap > 0
    long = with_gap if arm == "momentum" else (not with_gap)
    out["direction"] = "long" if long else "short"
    out.update({"day_open": o, "day_high": h, "day_low": lo, "day_close": c})

    slip = (slippage_bps or 0.0) / 10_000.0
    entry = o * (1 + slip) if long else o * (1 - slip)
    stop = (o - atr) if long else (o + atr)
    out["entry_price"] = round(entry, 4)
    out["stop_price"] = round(stop, 4)

    stopped = (lo <= stop) if long else (h >= stop)
    if stopped:
        exit_px, reason = stop, "stop"
    else:
        exit_px, reason = c, "eod"
    exit_px = exit_px * (1 - slip) if long else exit_px * (1 + slip)

    sign = 1.0 if long else -1.0
    out["exit_price"] = round(exit_px, 4)
    out["exit_reason"] = reason
    out["r_multiple"] = round(sign * (exit_px - entry) / atr, 4)
    return out


def grade_population(
    con: Any, md: Any, plan_date: date, slippage_bps: float = 0.0,
    arms: tuple[str, ...] = ("momentum", "fade"),
) -> dict[str, Any]:
    """Simulate every arm over the day's snapshotted population.

    Per-symbol fault isolation, exactly like every other fetch in this codebase:
    a symbol whose daily bar cannot be fetched records `no_data` and the run
    continues. This must never fail the evening loop -- it is a control arm, and
    a control arm that can break the thing it is controlling for is worse than
    no control arm at all.
    """
    rows = con.execute(
        """SELECT symbol, last, prev_close, gap_pct, atr14, atr_pct, adv20,
                  in_scan_universe, was_picked
           FROM population_universe WHERE plan_date = ? ORDER BY symbol""",
        [plan_date],
    ).fetchall()
    keys = ["symbol", "last", "prev_close", "gap_pct", "atr14", "atr_pct",
            "adv20", "in_scan_universe", "was_picked"]
    universe = [dict(zip(keys, r)) for r in rows]
    if not universe:
        return {"symbols": 0, "graded": 0, "arms": {}, "note": "no population snapshot for this date"}

    con.execute("DELETE FROM population_outcomes WHERE plan_date = ?", [plan_date])

    bars: dict[str, dict | None] = {}
    for u in universe:
        sym = u["symbol"]
        try:
            bars[sym] = md.daily_bar(sym, plan_date)
        except Exception as e:
            _log("population", f"WARN daily bar fetch failed for {sym}: {e}")
            bars[sym] = None

    now = datetime.now()
    graded = 0
    per_arm: dict[str, list[float]] = {a: [] for a in arms}
    for u in universe:
        for arm in arms:
            try:
                o = simulate_population_row(u, bars.get(u["symbol"]), arm, slippage_bps)
            except Exception as e:
                _log("population", f"WARN population sim failed for {u['symbol']}/{arm}: {e}")
                continue
            con.execute(
                """INSERT INTO population_outcomes
                   (plan_date, symbol, arm, direction, entry_price, stop_price,
                    exit_price, exit_reason, r_multiple, day_open, day_high,
                    day_low, day_close, sim_version, graded_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [plan_date, o["symbol"], o["arm"], o["direction"], o["entry_price"],
                 o["stop_price"], o["exit_price"], o["exit_reason"], o["r_multiple"],
                 o["day_open"], o["day_high"], o["day_low"], o["day_close"],
                 o["sim_version"], now],
            )
            graded += 1
            if o["r_multiple"] is not None:
                per_arm[arm].append(o["r_multiple"])

    return {
        "symbols": len(universe),
        "graded": graded,
        "arms": {
            a: {
                "n": len(v),
                "avg_r": round(sum(v) / len(v), 4) if v else None,
                "total_r": round(sum(v), 3) if v else 0.0,
            }
            for a, v in per_arm.items()
        },
    }


def population_stats(con: Any, sessions: int | None = None) -> dict[str, Any]:
    """Population monkey performance vs the desk's own triggered picks.

    Returns the desk's selection edge over the BETTER monkey -- deliberately the
    better one. Comparing against the weaker arm would let the desk claim credit
    for merely picking the right side of the tape, which the monkeys already
    tell you for free.
    """
    from .stats import one_sample_t, welch_t

    # Build the WHERE clause as an explicit list of conjuncts rather than
    # interpolating an "AND"/"WHERE" switch into an f-string -- that pattern
    # produces a syntactically valid query in both branches only by accident,
    # and silently breaks the moment a third condition is added.
    where = ["r_multiple IS NOT NULL"]
    params: list[Any] = []
    if sessions:
        where.append(
            "plan_date IN (SELECT DISTINCT plan_date FROM population_outcomes "
            "ORDER BY plan_date DESC LIMIT ?)"
        )
        params.append(int(sessions))
    where_sql = " AND ".join(where)

    arm_rows = con.execute(
        f"""SELECT arm, COUNT(*), AVG(r_multiple), STDDEV_SAMP(r_multiple),
                   SUM(r_multiple)
            FROM population_outcomes
            WHERE {where_sql}
            GROUP BY arm ORDER BY arm""",
        params,
    ).fetchall()

    arms: dict[str, dict] = {}
    for arm, n, avg, std, total in arm_rows:
        n = int(n or 0)
        avg = float(avg) if avg is not None else None
        std = float(std) if std is not None else None
        t, df, p = one_sample_t(avg, std, n)
        arms[arm] = {"n": n, "avg_r": round(avg, 4) if avg is not None else None,
                     "std_r": round(std, 4) if std is not None else None,
                     "total_r": round(float(total or 0), 3), "t": t, "p": p}

    desk = con.execute(
        """SELECT COUNT(*), AVG(o.r_multiple), STDDEV_SAMP(o.r_multiple)
           FROM outcomes o WHERE o.triggered AND o.r_multiple IS NOT NULL"""
    ).fetchone()
    d_n = int(desk[0] or 0)
    d_avg = float(desk[1]) if desk[1] is not None else None
    d_std = float(desk[2]) if desk[2] is not None else None

    best_arm, best = None, None
    for a, s in arms.items():
        if s["avg_r"] is None:
            continue
        if best is None or s["avg_r"] > best["avg_r"]:
            best_arm, best = a, s

    selection_edge = None
    edge_t = edge_p = None
    if best and d_avg is not None:
        selection_edge = round(d_avg - best["avg_r"], 4)
        edge_t, _, edge_p = welch_t(d_avg, d_std, d_n,
                                    best["avg_r"], best["std_r"], best["n"])

    return {
        "arms": arms,
        "best_arm": best_arm,
        "desk": {"n": d_n,
                 "avg_r": round(d_avg, 4) if d_avg is not None else None,
                 "std_r": round(d_std, 4) if d_std is not None else None},
        "selection_edge_r": selection_edge,
        "selection_edge_t": edge_t,
        "selection_edge_p": edge_p,
    }


def population_md(stats: dict[str, Any]) -> str:
    """Render the population baseline for the weekly miner prompt."""
    if not stats.get("arms"):
        return ("## POPULATION BASELINE (unconditional null)\n\n"
                "(no population outcomes recorded yet — the gap-scan population "
                "has not been graded for any session)")

    lines = [
        "## POPULATION BASELINE — THE UNCONDITIONAL NULL (computed, not estimated)\n",
        "Mechanical monkeys run over the ENTIRE premarket gap-scan population, "
        "snapshotted before any candidate selection happened. Entry at the open, "
        "1-ATR stop, exit at the close. R is denominated in ATRs, so these are "
        "directly comparable to the desk's R-multiples.\n",
    ]
    for arm, s in sorted(stats["arms"].items()):
        avg = f"{s['avg_r']:+.3f}R" if s["avg_r"] is not None else "n/a"
        p = f"{s['p']:.3f}" if s["p"] is not None else "n/a"
        lines.append(f"- {arm} monkey: n={s['n']}, avgR={avg}, "
                     f"totalR={s['total_r']:+.1f}, p={p}")

    d = stats["desk"]
    d_avg = f"{d['avg_r']:+.3f}R" if d["avg_r"] is not None else "n/a"
    lines.append(f"\n- THE DESK (triggered picks): n={d['n']}, avgR={d_avg}")

    if stats.get("selection_edge_r") is not None:
        edge = stats["selection_edge_r"]
        p = stats.get("selection_edge_p")
        p_s = f"{p:.3f}" if p is not None else "n/a"
        d_n = d["n"]
        if d_n < MIN_VERDICT_N:
            # A selection edge is a difference between the desk's MEAN and a
            # monkey's mean. At n=1 the desk has no mean, it has a trade. The
            # subtraction still returns a number, and that number used to
            # arrive wearing a verdict ("DESTROYING value") that reads as a
            # finding about the risk manager's judgment rather than as one
            # outcome minus an average.
            lines.append(
                f"\n**SELECTION EDGE = {edge:+.3f}R** (desk avgR minus the BEST "
                f"monkey, '{stats['best_arm']}'), desk n={d_n}, Welch p={p_s}.\n"
                f"**NO VERDICT — desk n={d_n} is below n={MIN_VERDICT_N}.** This is "
                f"the arithmetic gap between a mean over {d_n} trade(s) and a mean "
                "over many. Quote it as an unresolved measurement or not at all. Do "
                "not call selection good or bad, do not contrast it with the "
                "management counterfactual, and do not present the two pointing "
                "opposite ways as a pattern to watch: at this n they cannot agree "
                "or disagree about anything."
            )
        else:
            verdict = (
                "the desk's name selection is ADDING value over the population"
                if edge > 0 else
                "the desk's name selection is DESTROYING value — a mechanical rule "
                "over the same population did better"
            )
            lines.append(
                f"\n**SELECTION EDGE = {edge:+.3f}R** (desk avgR minus the BEST monkey, "
                f"'{stats['best_arm']}'), desk n={d_n}, Welch p={p_s}. Read: {verdict}."
            )
        lines.append(
            "\nThis is the number that decides whether candidate selection has "
            "edge. `edge_vs_baseline_r` elsewhere in this prompt is conditioned "
            "on names the analyst already chose and CANNOT answer this question."
        )

    lines.append(
        "\n*Precision limit, stated deliberately:* the monkeys use DAILY bars, so "
        "a stop is booked whenever the day's range reaches it, even if price dipped "
        "and recovered. That biases the monkeys DOWNWARD — they are handicapped. "
        "A desk that fails to beat them anyway is a strong finding; a desk that "
        "beats them narrowly should treat the margin as an upper bound."
    )
    return "\n".join(lines)
