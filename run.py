"""
Trade Agent CLI.

  python run.py init                      create dirs, DB, credentials template
  python run.py morning [--dry-run|--mock] [--offline] [--force] [--date YYYY-MM-DD]
  python run.py evening [--mock] [--offline] [--date YYYY-MM-DD] [--force]
  python run.py weekly  [--mock] [--date YYYY-MM-DD]
  python run.py dashboard                 regenerate the HTML dashboard
  python run.py status                    ledger + performance snapshot
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
    elif args.cmd == "morning":
        from agent.loops import morning
        morning.run(cfg, run_date, force=args.force)
    elif args.cmd == "evening":
        from agent.loops import evening
        evening.run(cfg, run_date, force=args.force)
    elif args.cmd == "weekly":
        from agent.loops import weekly
        weekly.run(cfg, run_date)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
