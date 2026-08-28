"""
Engine-agnostic preflight check for the ledger schema (incl. the v4 research-
integrity migration). Confirms agent/ledger.py's _MIGRATIONS list applies
cleanly and idempotently — against local DuckDB OR a Postgres/Neon DATABASE_URL.

Usage:
  .venv\\Scripts\\python scripts/db_preflight.py [DATABASE_URL]

With no argument, uses the DATABASE_URL env var if set, else local DuckDB.
Exits non-zero on any failure so it's usable as a CI/task gate.
"""
from __future__ import annotations

import os
import sys

if len(sys.argv) > 1:
    os.environ["DATABASE_URL"] = sys.argv[1]

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import ledger  # noqa: E402  (path must be set up first)

V4_TABLES = ["research_looks", "research_rules", "research_observations",
             "population_universe", "population_outcomes",
             "paired_arms", "paired_arm_picks"]


def _cols(con, table: str) -> set[str]:
    rows = con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [table]
    ).fetchall()
    return {r[0] for r in rows}


def main() -> int:
    engine = "postgres (Neon)" if os.environ.get("DATABASE_URL") else "duckdb (local)"
    print(f"[preflight] engine: {engine}")

    try:
        con = ledger.connect()
        con.close()
        con = ledger.connect()  # second connect must be a no-op, never an error
    except Exception as e:
        print(f"[preflight] FAILED on connect: {type(e).__name__}: {e}")
        return 1

    try:
        existing = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()}
        missing = [t for t in V4_TABLES if t not in existing]
        if missing:
            print(f"[preflight] FAILED: missing v4 tables: {missing}")
            return 1
        print(f"[preflight] all {len(V4_TABLES)} v4 tables present")
        for t in V4_TABLES:
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"[preflight]   {t}: {n} row(s)")

        needed_outcomes = {"r_multiple_2x", "r_multiple_3x", "slippage_bps_used",
                            "friction_model", "participation_pct"}
        missing_outcomes = needed_outcomes - _cols(con, "outcomes")
        if missing_outcomes:
            print(f"[preflight] FAILED: outcomes missing columns: {missing_outcomes}")
            return 1
        print("[preflight] outcomes has all 5 v4 friction columns")

        if "rule_id" not in _cols(con, "picks"):
            print("[preflight] FAILED: picks missing rule_id column")
            return 1
        print("[preflight] picks has rule_id column")

        needed_plan = {"context_mode", "paired_run"}
        missing_plan = needed_plan - _cols(con, "daily_plans")
        if missing_plan:
            print(f"[preflight] FAILED: daily_plans missing columns: {missing_plan}")
            return 1
        print("[preflight] daily_plans has context_mode + paired_run columns")
    except Exception as e:
        print(f"[preflight] FAILED during verification: {type(e).__name__}: {e}")
        return 1
    finally:
        con.close()

    print("[preflight] ALL GOOD -- schema (incl. v4) is clean on this engine")
    return 0


if __name__ == "__main__":
    sys.exit(main())
