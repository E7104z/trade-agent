"""
Trade Agent CLI.

  python run.py init                      create dirs, DB, credentials template
  python run.py morning [--dry-run|--mock] [--offline] [--force] [--date YYYY-MM-DD]
  python run.py evening [--mock] [--offline] [--date YYYY-MM-DD] [--force]
  python run.py weekly  [--mock] [--date YYYY-MM-DD]
  python run.py dashboard                 regenerate the HTML dashboard
  python run.py status                    ledger + performance snapshot
  python run.py regrade --from YYYY-MM-DD [--force]
                                           recompute outcomes under CURRENT sim
                                           semantics (explicit, never automatic)
  python run.py regrade-runner --from YYYY-MM-DD [--force]
                                           re-price runner candidates under the
                                           CURRENT entry rule (explicit, opt-in)
  python run.py research [--full]         research-integrity report: selection
                                           ablation + population baseline
                                           (zero API cost, read-only)
"""
from __future__ import annotations

import argparse
import shutil
import sys

from agent import ledger
from agent.config import (BASE_DIR, CREDENTIALS_PATH, DB_PATH, ensure_dirs, load_config)
from agent.util import log, parse_date, today_et


def cmd_init(cfg, args) -> None:
    ensure_dirs()
    example = BASE_DIR / "credentials.json.example"
    if not CREDENTIALS_PATH.exists() and example.exists():
        shutil.copy2(example, CREDENTIALS_PATH)
        log("init", f"Created {CREDENTIALS_PATH.name} — add your Anthropic API key")
    con = ledger.connect()
    con.close()
    log("init", f"Ledger ready at {DB_PATH}")
    log("init", "Next: put your Anthropic key in credentials.json, then run:  python run.py morning")


def cmd_research(cfg, args) -> None:
    """Research-integrity report: selection ablation + population baseline,
    computed entirely from data already in the ledger (zero API cost). --full
    also renders the weekly miner's full research block (FDR-corrected looks,
    quarantine states, paired-arm comparison).
    """
    from agent import ablation, integrity, population
    from agent import ledger as ledger_mod

    con = ledger.connect()
    try:
        print(ablation.ablation_md(ablation.random_pick_ablation(con)))
        print()
        print(population.population_md(population.population_stats(con)))
        if args.full:
            print()
            print(integrity.weekly_block(con, cfg, ledger_mod, today_et())["markdown"])
    finally:
        con.close()


def cmd_status(cfg, args) -> None:
    con = ledger.connect()
    overall = ledger.overall_stats(con)
    streak = ledger.current_streak(con)
    curve = ledger.equity_curve(con, cfg.get("account_size"), cfg.get("dollars_per_r"))
    lessons = ledger.active_lessons(con, 100)
    runs = ledger.recent_runs(con, 5)
    from agent.memory import read_playbook
    _, pb_version = read_playbook(con)

    print("\n=== TRADE AGENT STATUS ===")
    print(f"Playbook version: v{pb_version}")
    print(f"Graded picks: {overall['picks_graded']}  (triggered: {overall['triggered']})")
    wr = f"{overall['win_rate']:.0%}" if overall["win_rate"] is not None else "n/a"
    print(f"Win rate: {wr}   Avg R: {overall['avg_r']}   Total weighted R: {overall['total_weighted_r']}")
    equity = curve[-1]["equity"] if curve else cfg.get("account_size")
    print(f"Paper equity: ${equity:,.0f}  (start ${cfg.get('account_size'):,})")
    if streak["kind"] != "none":
        print(f"Current streak: {streak['length']} {streak['kind']}")
    print(f"Active lessons: {len(lessons)}")
    if runs:
        print("\nRecent runs:")
        for r in runs:
            print(f"  {r['run_date']} {r['loop']:8s} {r['status']:8s} "
                  f"${r['cost_usd']:.3f} {r['model']}")
    try:
        from agent import runner_view
        rs = runner_view.session_summary(con, today_et())
        if rs["session"] or rs["rows"]:
            print("")
            print(f"Runner today: {rs['n_candidates']} candidates, "
                  f"{rs['n_selected']} selected, {len(rs['orders'])} live orders"
                  + ("  [DEGRADED]" if rs["session"].get("provider_degraded") else ""))
            print("  full detail: run.py runner-status")
    except Exception as e:
        print("")
        print(f"Runner today: unavailable ({e})")

    total_cost = sum(r["cost_usd"] or 0 for r in ledger.recent_runs(con, 1000))
    print(f"\nAll-time API spend: ~${total_cost:.2f}")
    con.close()


def cmd_dashboard(cfg, args) -> None:
    from agent.delivery import render_dashboard
    con = ledger.connect()
    row = con.execute("SELECT max(plan_date) FROM daily_plans").fetchone()
    latest = row[0] if row and row[0] else None
    path = render_dashboard(con, cfg, latest)
    con.close()
    print(f"Dashboard: {path}")


def cmd_regrade(cfg, args) -> None:
    """Recompute outcomes for picks on/after --from under CURRENT simulation
    semantics (agent.grading.CURRENT_SIM_VERSION). Explicit, opt-in, never
    invoked automatically by any loop -- existing rows were graded under
    whatever rules were live when they were written, and are not silently
    rewritten just because the code changed underneath them.
    """
    from collections import defaultdict

    from agent.grading import CURRENT_SIM_VERSION, grade_day
    from agent.grading_context import build_grading_context
    from agent.providers import MarketData

    from_date = parse_date(args.from_date)
    con = ledger.connect()
    picks = ledger.graded_picks_since(con, from_date)
    if not picks:
        log("regrade", f"No graded picks on/after {from_date} — nothing to do")
        con.close()
        return

    if len(picks) > 50 and not args.force:
        log("regrade", f"Refusing: this would rewrite {len(picks)} outcome rows (>50). "
                       "Re-run with --force to proceed.")
        con.close()
        sys.exit(1)

    by_date: dict = defaultdict(list)
    for p in picks:
        by_date[p["plan_date"]].append(p)

    md = MarketData(cfg)
    slippage = float(cfg.get("slippage_bps", 5))
    flatten_at = cfg.get("flatten_at")
    min_bar_volume = float(cfg.get("min_bar_volume", 0) or 0)

    n = 0
    for d in sorted(by_date):
        day_picks = by_date[d]
        gctx = build_grading_context(md, con, d, [p["ticker"] for p in day_picks],
                                     cfg, tag="regrade")
        outcomes = grade_day(md, day_picks, d, slippage_bps=slippage,
                             flatten_at=flatten_at, min_bar_volume=min_bar_volume,
                             adv20_map=gctx["adv20_map"], atr_map=gctx["atr_map"],
                             trend_map=gctx["trend_map"],
                             fundamentals_map=gctx["fundamentals_map"],
                             prev_close_map=gctx["prev_close_map"],
                             or_minutes=gctx["or_minutes"],
                             entry_after_map=gctx["entry_after_map"])
        for o in outcomes:
            ledger.save_outcome(con, o)
            n += 1
    log("regrade", f"Recomputed {n} outcome row(s) from {from_date} onward "
                   f"under sim_version={CURRENT_SIM_VERSION}")
    con.close()


def cmd_regrade_runner(cfg, args) -> None:
    """Re-price every runner candidate for sessions on/after --from under the
    CURRENT simulation semantics -- in particular the honest entry time
    (runner_scan.honest_entry_time: never before the scan tick that recorded
    the candidate or the selection decision). Rows graded before sim_version 4
    entered at the walked-back detection bar, up to an hour before anything
    could have acted on it, which flatters every runner R the weekly miner
    reads. Explicit and opt-in, like `regrade`: never run automatically.
    Selection stamps (selected/rank/pick_id) are preserved by the upsert.
    Runner PICKS (setup dto_runner) are morning-style outcome rows -- re-grade
    those with `regrade`.
    """
    from agent.grading import CURRENT_SIM_VERSION
    from agent.loops.evening import _grade_runner_candidates
    from agent.providers import MarketData

    from_date = parse_date(args.from_date)
    con = ledger.connect()
    rows = con.execute(
        """SELECT session_date, COUNT(*) FROM runner_candidates
           WHERE session_date >= ? GROUP BY session_date ORDER BY session_date""",
        [from_date]).fetchall()
    total = sum(int(r[1]) for r in rows)
    if not rows:
        log("regrade", f"No runner candidates on/after {from_date} -- nothing to do")
        con.close()
        return
    if total > 500 and not args.force:
        log("regrade", f"Refusing: this would re-price {total} runner candidate rows "
                       f"across {len(rows)} session(s) (>500). Re-run with --force.")
        con.close()
        sys.exit(1)
    md = MarketData(cfg)
    for d, n in rows:
        tickers = ledger.runner_candidate_tickers(con, d)
        md.prefetch_session(tickers, d, daily=False)
        summary = _grade_runner_candidates(con, md, d, cfg)
        log("regrade", f"{d}: re-priced {summary['scored']}/{n} candidate(s), "
                       f"mean R {summary.get('mean_r')}")
    log("regrade", f"Re-priced {total} runner candidate row(s) from {from_date} onward "
                   f"under sim_version={CURRENT_SIM_VERSION}")
    con.close()


def cmd_runner(cfg, args) -> None:
    """One intraday Runner tick (or a loop of them) from this machine.

    The hosted path for this lane is POST /cron/intraday-check, driven by a
    scheduler. On this account neither available scheduler can do it: GitHub
    Actions returns startup_failure for every run (an account-level block -- a
    trivial `echo hello` workflow fails identically), and Vercel Cron on the
    Hobby plan rejects any schedule that fires more than once a day.

    So this runs the same `agent.loops.runner.tick()` locally, against the
    local ledger, using the credentials already in credentials.json. Orders go
    to the SAME Alpaca paper account either way -- the only difference is which
    database records the evidence.
    """
    import time as _time
    from datetime import datetime, time as dtime

    from agent.loops import runner as runner_loop
    from agent.providers import MarketData
    from agent.util import ET, now_et

    if args.no_orders:
        cfg.raw.setdefault("runner", {})["live_orders_enabled"] = False
        log("runner", "--no-orders: order placement disabled for this run")

    rcfg = cfg.get("runner", {}) or {}
    if not rcfg.get("enabled"):
        log("runner", "runner.enabled is false in config.json -- nothing to do")
        return

    md = MarketData(cfg)
    con = ledger.connect()

    def _at_override():
        if not args.at:
            return None
        hh, mm = (int(x) for x in args.at.split(":"))
        return datetime.combine(now_et().date(), dtime(hh, mm), tzinfo=ET)

    try:
        while True:
            at = _at_override() or now_et()
            started = _time.monotonic()
            try:
                res = runner_loop.tick(cfg, at=at, md=md, con=con)
            except Exception as e:
                # A tick must never kill the loop: a transient provider error
                # at 10:35 should not cost the rest of the session.
                log("runner", f"WARN tick failed ({type(e).__name__}: {e}) -- continuing")
                res = {"error": str(e)}

            if res.get("skipped"):
                log("runner", f"skipped: {res['skipped']}")
            elif "error" not in res:
                log("runner",
                    f"universe={res.get('universe_size')} "
                    f"new={res.get('new_candidates')} "
                    f"total={res.get('candidates')} "
                    f"{res.get('elapsed_ms')}ms"
                    + (" [DEGRADED]" if res.get("provider_degraded") else ""))
                sel = res.get("selection") or {}
                if sel.get("selected"):
                    log("runner", f"SELECTED {len(sel['selected'])}: "
                                  + ", ".join(sel["selected"]))
                    orders = sel.get("orders") or {}
                    log("runner", f"orders placed: {orders.get('placed', 0)}"
                                  + (f" | skipped: {orders['skipped']}"
                                     if orders.get("skipped") else ""))
                elif sel.get("selection_skipped_reason"):
                    log("runner", f"selection: {sel['selection_skipped_reason']}")

            if not args.loop:
                break

            # Stop once the window has closed for the day rather than spinning
            # until the process is killed.
            end = str(rcfg.get("scan_end_et", "15:00"))
            eh, em = (int(x) for x in end.split(":"))
            now_t = now_et().timetz().replace(tzinfo=None)
            if now_t > dtime(eh, em):
                log("runner", f"scan window closed ({end} ET) -- exiting loop")
                break

            sleep_for = max(5.0, args.every - (_time.monotonic() - started))
            _time.sleep(sleep_for)
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="trade-agent", description="AI trading analyst with a track record")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p, dates=True):
        p.add_argument("--mock", action="store_true", help="use canned LLM responses (no API cost)")
        p.add_argument("--offline", action="store_true", help="use canned/synthetic market data (no network)")
        p.add_argument("--force", action="store_true", help="run even on non-market days")
        p.add_argument("--no-email", action="store_true", help="skip email delivery")
        if dates:
            p.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (default: today ET)")

    p_morning = sub.add_parser("morning", help="generate today's trade plan")
    add_common(p_morning)
    p_morning.add_argument("--dry-run", action="store_true",
                           help="build and save the full prompt without calling the API")

    p_evening = sub.add_parser("evening", help="grade today's picks and extract lessons")
    add_common(p_evening)

    p_weekly = sub.add_parser("weekly", help="mine the ledger and rewrite the playbook")
    add_common(p_weekly)

    sub.add_parser("dashboard", help="regenerate the HTML dashboard")
    sub.add_parser("status", help="print ledger and performance snapshot")
    sub.add_parser("init", help="initialize directories, DB, credentials template")

    p_regrade = sub.add_parser("regrade", help="recompute outcomes under current sim semantics (explicit, opt-in)")
    p_regrade.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD -- recompute picks on/after this date")
    p_regrade.add_argument("--force", action="store_true", help="required if this would rewrite more than 50 rows")

    p_rregrade = sub.add_parser(
        "regrade-runner",
        help="re-price runner candidates under the current (honest) entry rule (explicit, opt-in)")
    p_rregrade.add_argument("--from", dest="from_date", required=True,
                            help="YYYY-MM-DD -- re-price sessions on/after this date")
    p_rregrade.add_argument("--force", action="store_true",
                            help="required if this would re-price more than 500 rows")

    p_runner = sub.add_parser(
        "runner",
        help="run one intraday Runner scan tick (scan -> select -> place)")
    add_common(p_runner)
    p_runner.add_argument("--at", type=str, default=None,
                          help="override the tick time, HH:MM ET (testing)")
    p_runner.add_argument("--loop", action="store_true",
                          help="keep ticking every --every seconds until the "
                               "scan window closes, then exit")
    p_runner.add_argument("--every", type=int, default=300,
                          help="seconds between ticks in --loop mode (default 300)")
    p_runner.add_argument("--no-orders", action="store_true",
                          help="scan and select but never place an order")

    p_rstatus = sub.add_parser(
        "runner-status",
        help="show today's Runner scan: candidates, picks, outcomes")
    p_rstatus.add_argument("--date", type=str, default=None,
                           help="YYYY-MM-DD (default: today ET)")

    p_research = sub.add_parser("research", help="research integrity report (selection ablation + population baseline, zero API cost)")
    p_research.add_argument("--full", action="store_true", help="also render the full weekly research block (FDR looks, quarantine, paired arms)")

    args = parser.parse_args()
    cfg = load_config()
    cfg.mock = getattr(args, "mock", False)
    cfg.offline = getattr(args, "offline", False)
    cfg.dry_run = getattr(args, "dry_run", False)
    cfg.no_email = getattr(args, "no_email", False)

    run_date = None
    if getattr(args, "date", None):
        run_date = parse_date(args.date)

    if args.cmd == "init":
        cmd_init(cfg, args)
    elif args.cmd == "status":
        cmd_status(cfg, args)
    elif args.cmd == "dashboard":
        cmd_dashboard(cfg, args)
    elif args.cmd == "regrade":
        cmd_regrade(cfg, args)
    elif args.cmd == "regrade-runner":
        cmd_regrade_runner(cfg, args)
    elif args.cmd == "research":
        cmd_research(cfg, args)
    elif args.cmd == "morning":
        from agent.loops import morning
        morning.run(cfg, run_date, force=args.force)
    elif args.cmd == "evening":
        from agent.loops import evening
        evening.run(cfg, run_date, force=args.force)
    elif args.cmd == "weekly":
        from agent.loops import weekly
        weekly.run(cfg, run_date)
    elif args.cmd == "runner":
        cmd_runner(cfg, args)
    elif args.cmd == "runner-status":
        from agent import runner_view
        con = ledger.connect()
        try:
            print(runner_view.render_text(con, run_date or today_et()))
        finally:
            con.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
