"""
verify_fixes.py -- read-only proof that the 2026-10 execution fixes hold in production
(docs/FIX_PHASE0.md, docs/FIX_VERIFICATION.md). The checks live in agent/fix_verification.py;
the production evening runs them itself every session (SELF-CHECK: "Fix verification").

    python scripts/verify_fixes.py --date 2026-10-19        PASS/FAIL per item for one session
    python scripts/verify_fixes.py --impact                  one-off: what each fix changes in history
    python scripts/verify_fixes.py --impact --since 2026-09-21

Read-only by construction:
  * with DATABASE_URL set it reads production through a Postgres session opened READ ONLY,
    with no schema migration (ledger.connect() would migrate; this never calls it) -- the
    database itself refuses a write;
  * without it, the local DuckDB file opened read_only;
  * the broker is read with GET requests only; anything else is refused before it leaves
    the process;
  * --impact re-prices history in an in-memory scratch database.
It writes nothing and places nothing.

Statuses: PASS, FAIL, PENDING (too early in the day to judge), UNVERIFIED (a source could
not be read). The exit code is 0 only when every item is PASS.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.fix_verification import (PASS, Broker, impact_report,  # noqa: E402
                                    install_get_only_guard, open_readonly, render, run_checks)


# ------------------------------------------------------------------------------ main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only verification of the 2026-10 execution fixes.")
    ap.add_argument("--date", type=date.fromisoformat, help="session to verify (YYYY-MM-DD)")
    ap.add_argument("--impact", action="store_true", help="one-off: what each fix changes in history")
    ap.add_argument("--since", type=date.fromisoformat,
                    help="--impact window start (default: the trailing 20 morning sessions)")
    ap.add_argument("--no-broker", action="store_true", help="do not read the paper account")
    ap.add_argument("--no-market-data", action="store_true", help="do not fetch bars (skips re-pricing)")
    args = ap.parse_args(argv)
    if not args.date and not args.impact:
        ap.error("give --date YYYY-MM-DD and/or --impact")

    install_get_only_guard()
    from agent.config import load_config
    from agent.util import now_et
    cfg = load_config()
    con, where = open_readonly()
    print(f"verify_fixes: reading {where}; writes nothing, places nothing")
    md = None
    if not args.no_market_data:
        try:
            from agent.providers import MarketData
            md = MarketData(cfg)
        except Exception as e:  # noqa: BLE001
            print(f"(no market data: {type(e).__name__}: {e})")
    rc = 0
    if args.date:
        broker = None if args.no_broker else Broker(cfg)
        results = run_checks(con, args.date, cfg, broker, md, now_et())
        print(render(args.date, results))
        rc = 0 if all(r.status == PASS for r in results) else 1
    if args.impact:
        if md is None:
            print("--impact re-prices history from real bars: it needs market data")
            return 2
        from agent import ledger
        since = args.since or ledger.morning_window_start(con, 20)
        if since is None:
            print("--impact: no morning sessions in the ledger")
            return rc
        print(impact_report(con, md, cfg, since))
    return rc


if __name__ == "__main__":
    sys.exit(main())
