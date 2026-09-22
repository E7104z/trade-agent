"""
Re-grade stored outcomes under the CURRENT simulator, from bars.

WHY THIS EXISTS

sim_version 4 changed the R contract: the risk denominator is planned geometry
(entry - stop), never the actual fill. Rows written under v1-v3 used a
fill-anchored denominator floored at 25% of planned risk, which produced R
values outside what the plan's own geometry permits -- +11R on a 2R plan, and
+0.48R on another 2R plan. Those two errors have one cause, and averaging them
produced a "selection edge" that was measuring fill drift.

Those rows CANNOT be repaired arithmetically. The stored row kept only the
corrupted output, never the denominator that produced it, so there is nothing
to invert. The only honest repair is to re-simulate from the bars.

The miner fences on sim_version, so until a session is re-graded it is simply
absent from the edge numbers rather than silently wrong. That is the intended
state: no data beats bad data.

USAGE

    python -m scripts.regrade_outcomes --dry-run              # report only
    python -m scripts.regrade_outcomes --since 2026-09-01     # write
    python -m scripts.regrade_outcomes --session 2026-09-21

--dry-run prints the before/after for every row and writes nothing. Run it
first; the diff is the audit of the fix.

NOTE ON DEGRADED-FEED SESSIONS

Re-grading a session that was scanned on IEX does not make it comparable. The
DETECTION prices came from sparse IEX prints, so the stop and target hang off
a level the consolidated tape may never have shown. Those sessions are
re-graded for completeness and still excluded from cross-session aggregates by
their feed_downgraded stamp. Re-grading is not a substitute for rescanning.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta

from agent import ledger
from agent.config import Config, load_config
from agent.grading import CURRENT_SIM_VERSION, simulate_pick
from agent.providers import MarketData
from agent.runner_scan import earliest_entry_time


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _sessions(con, since: date | None, one: date | None) -> list[date]:
    if one:
        return [one]
    rows = con.execute(
        "SELECT DISTINCT session_date FROM runner_candidate_outcomes "
        "WHERE session_date >= ? ORDER BY session_date",
        [since or date(2000, 1, 1)]).fetchall()
    return [r[0] for r in rows]


def _feed_downgraded(con, d: date) -> bool:
    import json
    try:
        row = con.execute(
            "SELECT gates_unavailable FROM runner_sessions WHERE session_date = ?",
            [d]).fetchone()
    except Exception:
        return False
    if not row or not row[0]:
        return False
    try:
        return bool(json.loads(row[0]).get("feed_downgraded"))
    except (TypeError, ValueError):
        return False


def regrade_session(con, md: MarketData, d: date, cfg: Config,
                    dry_run: bool = True) -> dict:
    """Re-simulate every runner candidate for one session. Mirrors
    agent.loops.evening._grade_runner_candidates exactly -- same entry cutoff,
    same shim, same config -- so a re-grade and a fresh grade agree."""
    slippage = float(cfg.get("slippage_bps", 0) or 0)
    flatten_at = cfg.get("flatten_at")
    min_bar_volume = float(cfg.get("min_bar_volume", 0) or 0)
    max_dev = float(cfg.get("max_fill_deviation_r", 0.5) or 0)
    degraded = _feed_downgraded(con, d)

    candidates = ledger.runner_candidates_for_date(con, d)
    prior = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT ticker, r_multiple, sim_version FROM runner_candidate_outcomes "
        "WHERE session_date = ?", [d]).fetchall()}

    stats = {"session": d, "n": 0, "regraded": 0, "no_bars": 0,
             "now_invalid": 0, "impossible_before": 0,
             "old_sum": 0.0, "new_sum": 0.0, "degraded_feed": degraded}

    for c in candidates:
        ticker = c["ticker"]
        stats["n"] += 1
        try:
            bars = md.minute_bars(ticker, d)
        except Exception:
            bars = []

        first_pass = c.get("first_pass_et")
        entry_bars = bars
        if bars and isinstance(first_pass, datetime):
            cutoff = earliest_entry_time(first_pass).replace(tzinfo=None)
            entry_bars = [b for b in bars
                          if b["t"].replace(tzinfo=None) >= cutoff]

        if not entry_bars:
            stats["no_bars"] += 1
            continue

        shim = {"pick_id": 0, "direction": "long", "entry_type": "open",
                "entry_price": c.get("detection_price"),
                "stop_price": c.get("stop_price"),
                "target_price": c.get("target_price")}
        sim = simulate_pick(shim, entry_bars, None, slippage_bps=slippage,
                            flatten_at=flatten_at, min_bar_volume=min_bar_volume,
                            session_date=d, max_fill_deviation_r=max_dev)

        old_r, old_ver = prior.get(ticker, (None, None))
        new_r = sim.get("r_multiple")

        # How far outside the plan's geometry was the stored value? A long
        # plan can only reach about (target-entry)/(entry-stop) at target and
        # about -1 at stop; anything past that was arithmetically impossible.
        planned_rr = None
        try:
            risk = c["detection_price"] - c["stop_price"]
            if risk > 0:
                planned_rr = (c["target_price"] - c["detection_price"]) / risk
        except (TypeError, KeyError):
            pass
        if (old_r is not None and planned_rr is not None
                and (old_r > planned_rr + 0.25 or old_r < -1.5)):
            stats["impossible_before"] += 1

        if old_r is not None:
            stats["old_sum"] += float(old_r)
        if new_r is not None:
            stats["new_sum"] += float(new_r)
            stats["regraded"] += 1
        if sim.get("exit_reason") == "invalid_fill":
            stats["now_invalid"] += 1

        if dry_run:
            plan_txt = ("planned {:.2f}R".format(planned_rr)
                        if planned_rr is not None else "planned ?")
            flag = ""
            if (old_r is not None and planned_rr is not None
                    and old_r > planned_rr + 0.25):
                flag = "   <-- stored R was IMPOSSIBLE under this plan"
            reason = sim.get("exit_reason")
            print("  {:<8} v{} R={:>8}  ->  v{} R={:>8}  ({}, {}){}".format(
                ticker, old_ver, str(old_r), CURRENT_SIM_VERSION,
                str(new_r), reason, plan_txt, flag))
        else:
            ledger.save_runner_candidate_outcome(con, {
                "session_date": d, "ticker": ticker, "eval_date": d,
                "triggered": sim.get("triggered"),
                "entry_fill": sim.get("entry_fill"),
                "exit_price": sim.get("exit_price"),
                "exit_reason": sim.get("exit_reason"),
                "r_multiple": sim.get("r_multiple"),
                "mfe_r": sim.get("mfe_r"),
                "mae_r": sim.get("mae_r"),
                "eod_r": sim.get("eod_r"),
                "sim_version": sim.get("sim_version"),
                "fill_slippage": sim.get("fill_slippage"),
                "fill_slippage_r": sim.get("fill_slippage_r"),
                "fill_drift_r": sim.get("fill_drift_r"),
                "r_multiple_at_fill": sim.get("r_multiple_at_fill"),
                "fill_vs_plan_r": sim.get("fill_vs_plan_r"),
                "execution_anomaly": sim.get("execution_anomaly"),
                "feed_downgraded": degraded,
            })
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="regrade_outcomes",
        description="Re-grade stored runner outcomes under the current simulator.")
    ap.add_argument("--since", type=_parse_date, default=None,
                    help="earliest session date to re-grade (YYYY-MM-DD)")
    ap.add_argument("--session", type=_parse_date, default=None,
                    help="a single session date to re-grade")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the before/after diff and write nothing")
    args = ap.parse_args()

    cfg = load_config()
    md = MarketData(cfg)
    con = ledger.connect()

    since = args.since or (date.today() - timedelta(days=120))
    sessions = _sessions(con, since, args.session)
    if not sessions:
        print("no sessions to re-grade")
        return

    mode = "DRY RUN (nothing written)" if args.dry_run else "WRITING"
    print(f"{mode} -- re-grading {len(sessions)} session(s) "
          f"to sim_version {CURRENT_SIM_VERSION}\n")

    totals = {"n": 0, "regraded": 0, "impossible_before": 0,
              "now_invalid": 0, "no_bars": 0, "old_sum": 0.0, "new_sum": 0.0}
    for d in sessions:
        print(f"{d}:")
        st = regrade_session(con, md, d, cfg, dry_run=args.dry_run)
        for k in totals:
            totals[k] += st[k]
        feed = "  [DEGRADED FEED -- excluded from aggregates]" if st["degraded_feed"] else ""
        print(f"  -> {st['regraded']}/{st['n']} regraded, "
              f"{st['impossible_before']} were outside plan geometry, "
              f"{st['now_invalid']} now invalid_fill, "
              f"sum R {st['old_sum']:.2f} -> {st['new_sum']:.2f}{feed}\n")

    print("=" * 72)
    print(f"TOTAL: {totals['regraded']}/{totals['n']} rows regraded")
    print(f"  rows whose stored R was impossible under their own plan: "
          f"{totals['impossible_before']}")
    print(f"  rows now rejected as invalid_fill:                       "
          f"{totals['now_invalid']}")
    print(f"  rows with no bars available:                             "
          f"{totals['no_bars']}")
    print(f"  summed R: {totals['old_sum']:.2f} -> {totals['new_sum']:.2f}")
    if args.dry_run:
        print("\nDRY RUN -- nothing was written. Re-run without --dry-run to apply.")


if __name__ == "__main__":
    main()
