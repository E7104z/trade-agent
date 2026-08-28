"""
Retrospective ablation — does the risk manager's selection beat random?

WHY THIS MODULE EXISTS
----------------------
The obvious control for "does the LLM add value" is to run a second desk with
randomized candidate selection. But that conflates two very different things:
candidate DISCOVERY (the gap scan, the news feed, the analyst's search) and
candidate EVALUATION (the risk manager's kill checklist and conviction sizing).
Randomizing which tickers get considered tests the former; it tells you almost
nothing about the latter.

The isolating control is narrower and much more interesting: hold the candidate
pool fixed at exactly what the analyst proposed, then choose k of them AT RANDOM
instead of by the risk manager's judgment. If the risk manager has selection
skill, the real plan should sit in the right tail of that random distribution.

And the data for it already exists. The shadow ledger (`rejections`) stores every
killed candidate with full plan levels AND grades them under the same simulator
and the same friction as taken picks. Union that with `picks` and you have the
analyst's complete candidate set, already graded, for every session in the
ledger's history. This ablation therefore costs ZERO additional API calls, ZERO
additional market data, and can be run over the entire back history today.

WHAT IS AND IS NOT COMPARABLE
-----------------------------
Handled explicitly rather than papered over:

  * SIZING IS EXCLUDED. Rejections carry no size_r, so every arm is compared on
    UNWEIGHTED mean R. This ablation measures SELECTION, not sizing. The sizing
    question already has its own counterfactual (`ledger.sizing_counterfactual`)
    and mixing them would make both unreadable.

  * TIME STOPS DIFFER. Rejections are not stored with a time_stop, so they are
    graded as if they had none, while taken picks may exit early on one. That is
    a real asymmetry and it FAVORS THE RANDOM ARM (an unbounded hold captures
    more of a winning move). Reported alongside the result, and it means a
    positive result here is conservative.

  * SIM VERSION IS RESPECTED. Rows are restricted to a single sim_version by
    default. Comparing across a semantics change would let a rule change
    masquerade as a selection finding -- the exact confusion `sim_version` was
    introduced to prevent.

  * UNMEASURABLE ROWS ARE DROPPED, not zero-filled. A candidate with no
    intraday data is an absence of evidence; scoring it as 0.0R would pull every
    arm toward the mean and manufacture agreement.
"""
from __future__ import annotations

import random
from typing import Any

__all__ = ["candidate_pool", "random_pick_ablation", "ablation_md"]

DEFAULT_DRAWS = 5000
DEFAULT_SEED = 20260827


def candidate_pool(
    con: Any, sim_version: int | None = None, sessions: int | None = None
) -> dict[str, dict[str, list[dict]]]:
    """Reconstruct the analyst's full candidate set per session, already graded.

    Returns {plan_date: {"taken": [...], "rejected": [...]}} where each entry is
    {"ticker", "r_multiple", "triggered"}. Only rows with a measurable outcome
    are included -- see the module docstring on why unmeasurable rows are
    dropped rather than zero-filled.

    A rejection that would not have triggered contributes r_multiple = 0.0: it
    is measurable (we know it never filled) and its true contribution to a
    portfolio is exactly zero. That is different from a row we could not measure
    at all, which is excluded.
    """
    pool: dict[str, dict[str, list[dict]]] = {}

    taken_where = ["o.triggered", "o.r_multiple IS NOT NULL",
                   "COALESCE(o.exit_reason,'') <> 'invalid_fill'"]
    taken_params: list[Any] = []
    if sim_version is not None:
        taken_where.append("COALESCE(o.sim_version, 1) = ?")
        taken_params.append(int(sim_version))
    rows = con.execute(
        f"""SELECT p.plan_date, p.ticker, o.r_multiple
            FROM picks p JOIN outcomes o ON p.pick_id = o.pick_id
            WHERE {' AND '.join(taken_where)}
            ORDER BY p.plan_date, p.pick_id""",
        taken_params,
    ).fetchall()
    for d, ticker, r in rows:
        pool.setdefault(str(d), {"taken": [], "rejected": []})["taken"].append(
            {"ticker": ticker, "r_multiple": float(r), "triggered": True}
        )

    # Rejections: `eod_outcome` is the shadow classification written by the
    # evening loop. 'unmeasurable' means no intraday data or an invalid_fill
    # artifact -- excluded. 'scratch' from a non-trigger is a real, measurable
    # zero and stays in.
    rej_where = ["COALESCE(r.eod_outcome,'') <> ''",
                 "COALESCE(r.eod_outcome,'') <> 'unmeasurable'"]
    rej_params: list[Any] = []
    if sim_version is not None:
        rej_where.append("COALESCE(r.sim_version, 1) = ?")
        rej_params.append(int(sim_version))
    rows = con.execute(
        f"""SELECT r.plan_date, r.ticker, r.forgone_r, r.eod_outcome
            FROM rejections r
            WHERE {' AND '.join(rej_where)}
            ORDER BY r.plan_date, r.rejection_id""",
        rej_params,
    ).fetchall()
    for d, ticker, forgone, outcome in rows:
        key = str(d)
        if key not in pool:
            continue   # no taken picks that day: nothing to compare an arm against
        val = 0.0 if forgone is None else float(forgone)
        pool[key]["rejected"].append(
            {"ticker": ticker, "r_multiple": val,
             "triggered": outcome in ("would_have_won", "would_have_lost")}
        )

    if sessions:
        for d in sorted(pool, reverse=True)[sessions:]:
            pool.pop(d)
    return pool


def random_pick_ablation(
    con: Any,
    draws: int = DEFAULT_DRAWS,
    seed: int = DEFAULT_SEED,
    sim_version: int | None = None,
    sessions: int | None = None,
) -> dict[str, Any]:
    """Monte Carlo: how would random selection from the same pool have done?

    For each session, draw k = (number of picks actually taken) candidates
    uniformly at random, without replacement, from the union of taken and
    rejected candidates. Sum unweighted R across sessions. Repeat `draws` times.

    The reported percentile is the fraction of random portfolios the real desk
    BEAT. 50 means indistinguishable from coin-flipping among its own
    candidates. Sessions where the pool offers no genuine alternative (every
    candidate was taken) are excluded and counted separately: they are not
    evidence about selection either way, and leaving them in would drag the
    percentile toward 50 with sessions that had no choice to make.

    Seeded => byte-identical across runs, so a moving result means the LEDGER
    changed, never the dice.
    """
    pool = candidate_pool(con, sim_version=sim_version, sessions=sessions)
    rng = random.Random(seed)

    usable: list[tuple[list[float], int]] = []
    actual_total = 0.0
    skipped_no_alternative = 0
    n_taken_total = 0

    for _, day in sorted(pool.items()):
        taken, rejected = day["taken"], day["rejected"]
        if not taken:
            continue
        k = len(taken)
        combined = [c["r_multiple"] for c in taken] + [c["r_multiple"] for c in rejected]
        if len(combined) <= k:
            skipped_no_alternative += 1
            continue
        actual_total += sum(c["r_multiple"] for c in taken)
        n_taken_total += k
        usable.append((combined, k))

    if not usable:
        return {
            "sessions": 0, "draws": 0, "picks": 0,
            "actual_total_r": None, "actual_avg_r": None,
            "random_mean_total_r": None, "random_std_total_r": None,
            "percentile": None, "p_value": None,
            "skipped_no_alternative": skipped_no_alternative,
            "note": "no session offered a genuine alternative to what was taken",
        }

    totals: list[float] = []
    for _ in range(draws):
        t = 0.0
        for combined, k in usable:
            t += sum(rng.sample(combined, k))
        totals.append(t)
    totals.sort()

    beaten = sum(1 for t in totals if actual_total > t)
    percentile = 100.0 * beaten / len(totals)
    # One-sided empirical p: P(random >= actual). The +1 correction keeps p
    # strictly positive -- an empirical p of exactly 0 is not a claim a Monte
    # Carlo with finite draws is entitled to make.
    at_least = sum(1 for t in totals if t >= actual_total)
    p_value = (at_least + 1) / (len(totals) + 1)

    mean_total = sum(totals) / len(totals)
    var = sum((t - mean_total) ** 2 for t in totals) / max(1, len(totals) - 1)

    return {
        "sessions": len(usable),
        "draws": draws,
        "picks": n_taken_total,
        "actual_total_r": round(actual_total, 3),
        "actual_avg_r": round(actual_total / n_taken_total, 4) if n_taken_total else None,
        "random_mean_total_r": round(mean_total, 3),
        "random_std_total_r": round(var ** 0.5, 3),
        "random_p05_total_r": round(totals[int(0.05 * len(totals))], 3),
        "random_p95_total_r": round(totals[min(len(totals) - 1, int(0.95 * len(totals)))], 3),
        "percentile": round(percentile, 1),
        "p_value": round(p_value, 4),
        "skipped_no_alternative": skipped_no_alternative,
        "seed": seed,
        "sim_version": sim_version,
    }


def ablation_md(res: dict[str, Any]) -> str:
    """Render the selection ablation for the weekly miner prompt."""
    if not res or not res.get("sessions"):
        return (
            "## SELECTION ABLATION (risk manager vs random)\n\n"
            "(not computable yet — " + str(res.get("note", "no graded sessions")) + ")"
        )

    pct = res["percentile"]
    if pct >= 95:
        verdict = ("the risk manager's selection is beating random draws from its "
                   "own candidate pool")
    elif pct <= 5:
        verdict = ("the risk manager is selecting WORSE than random from its own "
                   "candidate pool — it is actively destroying value")
    else:
        verdict = ("selection is statistically indistinguishable from choosing at "
                   "random among the analyst's own candidates")

    return "\n".join([
        "## SELECTION ABLATION — RISK MANAGER vs RANDOM (computed, zero API cost)\n",
        f"Over {res['sessions']} sessions and {res['picks']} taken picks, the actual "
        f"plan returned {res['actual_total_r']:+.2f}R unweighted.",
        f"{res['draws']} Monte Carlo portfolios drawing the SAME NUMBER of picks at "
        f"random from the SAME candidate pool (taken + shadow-ledger rejections) "
        f"returned {res['random_mean_total_r']:+.2f}R on average "
        f"(5th pct {res['random_p05_total_r']:+.2f}R, 95th pct {res['random_p95_total_r']:+.2f}R).",
        f"\n**The desk beat {pct}% of random portfolios** (one-sided empirical "
        f"p={res['p_value']}). Read: {verdict}.\n",
        "Scope, stated deliberately:",
        "- Compared UNWEIGHTED. This measures SELECTION, not sizing — the sizing "
        "counterfactual is reported separately.",
        "- Rejections are graded without a time_stop while taken picks may exit on "
        "one. That asymmetry FAVORS the random arm, so a positive result here is "
        "conservative.",
        f"- {res['skipped_no_alternative']} session(s) excluded where every candidate "
        "was taken (no alternative existed, so there was no selection to test).",
        f"- Seed {res['seed']}: this figure is reproducible. If it moves between "
        "runs, the ledger changed — not the dice.",
    ])
