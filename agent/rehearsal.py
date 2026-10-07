"""
Rehearse the loops on real data before a change ships
(specs/VERIFICATION_PRACTICE_v1.md).

Every evening review so far found errors, because the first real test of each
change was the next live session: the test suite runs on fake model replies,
fake prices and a throwaway database, so it cannot see a 130-second model
call, a grader misreading a number, or a real opening print. A rehearsal runs
the CHANGED code end to end on real market data and real model calls, in a
scratch copy of the local ledger, and writes a report to read before deploying.
scripts/deploy.ps1 refuses to ship a runtime change without a passing one.

  morning  today's session through the HOSTED path: the analyst half, the
           checkpoint, the hand-off, the risk half -- each half timed against
           the 300s invocation budget it gets in production.
  evening  a past session from the local ledger, graded again: the simulator,
           the grader, the audit, the trader's log and the self-check, timed
           against the same budget.

Nothing leaves the machine: DATABASE_URL is removed for the duration (this
PC's user-level DATABASE_URL points at production), email and live paper
orders are switched off in the rehearsal's config, and the hand-off is
recorded rather than sent. The model calls are real and cost about $2-3 a
morning, $0.10 an evening.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import ledger
from .config import DATA_DIR, REPORTS_DIR, Config, load_config
from .util import is_market_day, prev_market_day, today_et

__all__ = ["rehearse", "verify_head", "REPORT_DIR", "RUNTIME_PATHS"]

ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = REPORTS_DIR / "rehearsals"
BUDGET_S = 300
PASS_S = 270            # selfcheck.MORNING_WARN_S: the margin a pass must keep
# A change to anything here changes what runs in production.
RUNTIME_PATHS = ("agent", "api", "prompts", "config.json", "vercel.json", "requirements.txt")


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------

def _git(*args: str) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return p.returncode, (p.stdout or "").strip()


def _head() -> tuple[str, bool]:
    """(HEAD sha, runtime files differ from HEAD)."""
    _, sha = _git("rev-parse", "HEAD")
    code, _ = _git("diff", "--quiet", "HEAD", "--", *RUNTIME_PATHS)
    return sha, code != 0


def verify_head() -> tuple[bool, str]:
    """True when a passing rehearsal covers HEAD's runtime files: one was
    run on a clean commit whose agent/, api/, prompts/ and config are
    identical to HEAD's."""
    sha, dirty = _head()
    if dirty:
        return False, "runtime files have uncommitted changes; commit, then rehearse"
    best = None
    for path in sorted(REPORT_DIR.glob("*.json")):
        try:
            r = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not r.get("passed") or r.get("dirty"):
            continue
        code, _ = _git("diff", "--quiet", r["commit"], sha, "--", *RUNTIME_PATHS)
        if code == 0:
            best = r
    if best is None:
        return False, (f"no passing rehearsal covers {sha[:12]}: run "
                       ".venv\\Scripts\\python run.py rehearse")
    return True, (f"rehearsed on {best['commit'][:12]} at {best.get('finished_at')}: "
                  + ", ".join(f"{k} {v.get('verdict')}" for k, v in best["loops"].items()))


# --------------------------------------------------------------------------
# isolation
# --------------------------------------------------------------------------

@contextlib.contextmanager
def _isolated(db_path: Path):
    """The ledger points at the scratch copy and nothing can reach production."""
    saved_url = os.environ.pop("DATABASE_URL", None)
    saved_path = ledger.DB_PATH
    ledger.DB_PATH = db_path
    try:
        yield
    finally:
        ledger.DB_PATH = saved_path
        if saved_url is not None:
            os.environ["DATABASE_URL"] = saved_url


def _cfg(paired: bool) -> Config:
    cfg = load_config()
    raw = json.loads(json.dumps(cfg.raw))
    raw["email_enabled"] = False
    raw.setdefault("live_paper", {})["enabled"] = False
    raw.setdefault("runner", {})["live_orders_enabled"] = False
    raw.setdefault("paired", {})["enabled"] = bool(paired) and bool(
        (cfg.raw.get("paired") or {}).get("enabled"))
    cfg.raw = raw
    cfg.mock = cfg.offline = cfg.dry_run = False
    cfg.no_email = True
    return cfg


class _Tee(io.TextIOBase):
    """Print as usual and keep every line for the report."""

    def __init__(self, out):
        self.out, self.lines = out, []

    def write(self, s: str) -> int:
        self.out.write(s)
        self.lines.extend(x for x in s.splitlines() if x.strip())
        return len(s)

    def flush(self) -> None:
        self.out.flush()


def _q(con, sql: str, params: list | None = None) -> list[tuple]:
    try:
        return con.execute(sql, params or []).fetchall()
    except Exception:  # noqa: BLE001
        return []


def _cost(con, loop_names: tuple[str, ...], d: date) -> float:
    marks = ",".join("?" for _ in loop_names)
    rows = _q(con, f"SELECT SUM(cost_usd) FROM runs WHERE loop IN ({marks}) AND run_date = ?",
              list(loop_names) + [d])
    return round(float(rows[0][0] or 0.0), 3) if rows else 0.0


def _stages(con, loop: str, d: date) -> dict:
    rows = _q(con, """SELECT stages FROM runs WHERE loop = ? AND run_date = ? AND stages IS NOT NULL
                      ORDER BY run_id DESC LIMIT 1""", [loop, d])
    try:
        return json.loads(rows[0][0] or "{}") if rows else {}
    except (TypeError, ValueError):
        return {}


# --------------------------------------------------------------------------
# the loops
# --------------------------------------------------------------------------

def _morning(cfg: Config, d: date) -> dict:
    from . import dispatch
    from .loops import morning
    con = ledger.connect()
    for sql in ("DELETE FROM run_locks WHERE trading_date = ?",
                "DELETE FROM morning_checkpoints WHERE trading_date = ?",
                "DELETE FROM outcomes WHERE pick_id IN (SELECT pick_id FROM picks WHERE plan_date = ?)",
                "DELETE FROM picks WHERE plan_date = ?",
                "DELETE FROM daily_plans WHERE plan_date = ?",
                "DELETE FROM rejections WHERE plan_date = ?",
                "DELETE FROM paired_arm_picks WHERE plan_date = ?",
                "DELETE FROM paired_arms WHERE plan_date = ?",
                "DELETE FROM runs WHERE run_date = ? AND loop = 'morning'"):
        _q(con, sql, [d])
    con.close()

    fired: list[dict] = []
    saved = (dispatch.available, dispatch.fire)
    dispatch.available = lambda cfg=None: True
    dispatch.fire = lambda path, params=None, wait_s=3.0, cfg=None: (
        fired.append({"path": path, **(params or {})}) or {"ok": True, "how": "rehearsal"})
    problems: list[str] = []
    try:
        t0 = time.monotonic()
        morning.run(cfg, run_date=d, force=True)
        analyst_s = time.monotonic() - t0
        con = ledger.connect()
        lock = ledger.run_lock(con, d, "morning") or {}
        con.close()
        risk_s = None
        if not fired:
            problems.append("the analyst half never handed off")
        else:
            t1 = time.monotonic()
            try:
                morning.resume(cfg, run_date=d, request_id=fired[0].get("rid"))
            except Exception as e:  # noqa: BLE001
                problems.append(f"risk half raised {type(e).__name__}: {e}")
            risk_s = time.monotonic() - t1
    finally:
        dispatch.available, dispatch.fire = saved

    con = ledger.connect()
    try:
        plan = ledger.plan_for_date(con, d)
        cp = ledger.morning_checkpoint(con, d) or {}
        lock = ledger.run_lock(con, d, "morning") or {}
        picks = ledger.picks_for_date(con, d)
        arm_b = (_q(con, "SELECT COUNT(*) FROM paired_arms WHERE plan_date = ? AND arm = 'B'", [d])
                 or [(0,)])[0][0]
        stages = _stages(con, "morning", d)
        cost = _cost(con, ("morning",), d)
    finally:
        con.close()
    # Arm A's own work in each half, apart from the bounded wait for arm B:
    # the plan depends on the first, the paired comparison on the second.
    t = (stages.get("timings_s") or {})
    arm_a = {"analyst half": sum(float(t.get(k) or 0) for k in ("bundle", "regime", "analyst", "quotes")),
             "risk half": sum(float(t.get(k) or 0) for k in ("risk", "validate", "persist", "deliver"))}
    for name, s in arm_a.items():
        if s >= PASS_S:
            problems.append(f"arm A's {name} took {s:.0f}s (pass < {PASS_S}s of the {BUDGET_S}s budget)")
    for name, s in (("analyst half", analyst_s), ("risk half", risk_s)):
        if s is not None and s >= BUDGET_S - 5:
            problems.append(f"the {name} invocation ran {s:.0f}s, into the {BUDGET_S}s limit")
    if plan is None:
        problems.append("no plan was saved")
    if cp.get("stage") != "complete":
        problems.append(f"checkpoint ended '{cp.get('stage')}'")
    if lock.get("status") != "complete":
        problems.append(f"run lock ended '{lock.get('status')}'")
    if cfg.get("paired", {}).get("enabled") and not arm_b:
        problems.append(f"paired arm B recorded nothing (arm_b_status {cp.get('arm_b_status')})")
    return {
        "session": str(d), "analyst_half_s": round(analyst_s, 1),
        "risk_half_s": None if risk_s is None else round(risk_s, 1),
        "arm_a_s": {k: round(v, 1) for k, v in arm_a.items()},
        "picks": [f"{p['ticker']} {p['direction']} {p.get('setup_type')} "
                  f"{p.get('entry_type')} @{p.get('entry_price')} "
                  f"[{p.get('exploration_lane') or 'approved'}]" for p in picks],
        "no_trade": bool(plan and plan.get("no_trade")),
        "checkpoint": {k: str(v) for k, v in cp.items()},
        "arm_b_rows": int(arm_b or 0), "timings_s": stages.get("timings_s"),
        "cost_usd": cost, "problems": problems,
    }


def _evening(cfg: Config, d: date) -> dict:
    from .loops import evening
    con = ledger.connect()
    picks = ledger.picks_for_date(con, d)
    if not picks:
        con.close()
        return {"session": str(d), "problems": [f"no picks for {d} in the local ledger"]}
    for sql in ("DELETE FROM run_locks WHERE trading_date = ? AND run_type = 'evening'",
                "DELETE FROM outcomes WHERE pick_id IN (SELECT pick_id FROM picks WHERE plan_date = ?)",
                "UPDATE rejections SET graded_at = NULL WHERE plan_date = ?",
                "DELETE FROM self_checks WHERE session_date = ?",
                "DELETE FROM traders_log WHERE session_date = ?",
                "DELETE FROM runs WHERE run_date = ? AND loop IN ('evening', 'traders_log')"):
        _q(con, sql, [d])
    con.close()

    problems: list[str] = []
    t0 = time.monotonic()
    try:
        evening.run(cfg, run_date=d, force=True)
    except Exception as e:  # noqa: BLE001
        problems.append(f"evening raised {type(e).__name__}: {e}")
    total = time.monotonic() - t0

    con = ledger.connect()
    try:
        stages = _stages(con, "evening", d)
        status = (_q(con, """SELECT status FROM runs WHERE loop = 'evening' AND run_date = ?
                             ORDER BY run_id DESC LIMIT 1""", [d]) or [(None,)])[0][0]
        grades = [f"{t}: {g}" for t, g in _q(
            con, """SELECT p.ticker, o.grade FROM picks p JOIN outcomes o ON o.pick_id = p.pick_id
                    WHERE p.plan_date = ? ORDER BY p.pick_id""", [d])]
        log_row = _q(con, "SELECT status, rendered_text FROM traders_log WHERE session_date = ?", [d])
        checks = [f"[{s.upper()}] {k}: {h}" for k, s, h in _q(
            con, "SELECT check_key, status, headline FROM self_checks WHERE session_date = ?", [d])]
        cost = _cost(con, ("evening", "traders_log"), d)
    finally:
        con.close()
    if total >= PASS_S:
        problems.append(f"evening took {total:.0f}s of its {BUDGET_S}s budget (pass < {PASS_S}s)")
    if status not in ("ok", "degraded"):
        problems.append(f"evening run status '{status}'")
    if stages.get("llm") != "complete":
        problems.append(f"grader stage '{stages.get('llm')}'")
    return {
        "session": str(d), "total_s": round(total, 1), "status": status,
        "timings_s": stages.get("timings_s"), "audit": stages.get("audit"),
        "grades": grades, "traders_log": (log_row[0][0] if log_row else None),
        "traders_log_text": (log_row[0][1] if log_row else None),
        "self_check": checks, "cost_usd": cost, "problems": problems,
    }


def _preflight(cfg: Config) -> str | None:
    """One tiny model call before minutes of rehearsal: an exhausted credit
    balance or a bad key fails here, named, instead of halfway through."""
    try:
        from .llm import LLMClient
        LLMClient(cfg).call(system="Reply with OK.", user="OK?", model="claude-haiku-4-5-20251001",
                            mock_name="preflight", expect_json=False, max_tokens=5,
                            attempts=1, timeout=30, archive=False)
        return None
    except (Exception, SystemExit) as e:  # noqa: BLE001
        return f"the Anthropic API refused a one-token call: {e}"[:500]


def cron_conflict(now_utc: datetime | None = None, lead_min: int = 30,
                  late_min: int = 60) -> str | None:
    """The scheduled cron (vercel.json) a deploy now would land on, if any:
    from `lead_min` before its schedule until `late_min` after it (Hobby
    fires up to ~59 minutes late). A deploy that lands on a cron makes that
    run pay the schema migration, or swaps code under it."""
    from datetime import timedelta, timezone
    now = now_utc or datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        crons = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8")).get("crons") or []
    except (OSError, ValueError):
        return None

    def dow_ok(field: str, day: datetime) -> bool:
        cron_dow = (day.weekday() + 1) % 7            # cron: 0 = Sunday
        if field == "*":
            return True
        for part in field.split(","):
            lo, _, hi = part.partition("-")
            if int(lo) <= cron_dow <= int(hi or lo):
                return True
        return False

    for c in crons:
        try:
            minute, hour, _dom, _mon, dow = c["schedule"].split()
            for day_offset in (-1, 0, 1):
                day = now + timedelta(days=day_offset)
                at = day.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
                if dow_ok(dow, at) and at - timedelta(minutes=lead_min) <= now <= at + timedelta(minutes=late_min):
                    return f"{c['path']} is scheduled at {at:%H:%M} UTC ({c['schedule']})"
        except (KeyError, ValueError):
            continue
    return None


def _default_evening_date(con) -> date | None:
    rows = _q(con, f"""SELECT plan_date FROM picks
                      WHERE COALESCE(setup_type, '') <> '{ledger.RUNNER_SETUP}' AND plan_date < ?
                      GROUP BY plan_date ORDER BY plan_date DESC LIMIT 1""", [today_et()])
    return rows[0][0] if rows else None


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def rehearse(loops: tuple[str, ...] = ("morning", "evening"), evening_date: date | None = None,
             paired: bool = True) -> dict:
    """Run the loops on a scratch copy of the local ledger and write the
    report. Returns the summary (also saved as REPORT_DIR/<sha12>.json)."""
    sha, dirty = _head()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    work = REPORT_DIR / f"{sha[:12]}-{stamp}"
    work.mkdir(parents=True, exist_ok=True)
    db = work / "ledger.duckdb"
    src = DATA_DIR / "ledger.duckdb"
    if src.exists():
        shutil.copyfile(src, db)          # real memory: lessons, playbook, history
    cfg = _cfg(paired)
    summary: dict[str, Any] = {"commit": sha, "dirty": dirty, "started_at": stamp,
                               "workdir": str(work), "loops": {}}
    tee = _Tee(sys.stdout)
    refused = _preflight(cfg)
    if refused:
        for loop in loops:
            summary["loops"][loop] = {"problems": [refused]}
        loops = ()
    with _isolated(db), contextlib.redirect_stdout(tee):
        if "morning" in loops:
            d = today_et()
            if not is_market_day(d):
                d = prev_market_day(d)
            summary["loops"]["morning"] = _morning(cfg, d)
        if "evening" in loops:
            con = ledger.connect()
            d = evening_date or _default_evening_date(con)
            con.close()
            summary["loops"]["evening"] = (
                _evening(cfg, d) if d else {"problems": ["no past session with picks to grade"]})
    for r in summary["loops"].values():
        r["verdict"] = "PASS" if not r.get("problems") else "FAIL"
    summary["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary["passed"] = (not dirty and set(summary["loops"]) == {"morning", "evening"}
                         and all(r["verdict"] == "PASS" for r in summary["loops"].values()))
    (work / "log.txt").write_text("\n".join(tee.lines), encoding="utf-8")
    (work / "report.md").write_text(_report_md(summary), encoding="utf-8")
    (REPORT_DIR / f"{sha[:12]}.json").write_text(json.dumps(summary, indent=2, default=str),
                                                 encoding="utf-8")
    return summary


def _report_md(s: dict) -> str:
    out = [f"# Rehearsal of {s['commit'][:12]}{' (UNCOMMITTED runtime changes)' if s['dirty'] else ''}",
           "", f"Finished {s['finished_at']}. Overall: {'PASS' if s['passed'] else 'FAIL'}.", ""]
    for loop, r in s["loops"].items():
        out += [f"## {loop}: {r.get('verdict')} (session {r.get('session')}, "
                f"~${r.get('cost_usd', 0)})", ""]
        for p in r.get("problems") or []:
            out.append(f"- PROBLEM: {p}")
        for k, v in r.items():
            if k in ("problems", "verdict", "session", "traders_log_text"):
                continue
            out.append(f"- {k}: {json.dumps(v, default=str) if isinstance(v, (dict, list)) else v}")
        if r.get("traders_log_text"):
            out += ["", "### Trader's log as rendered", "", r["traders_log_text"]]
        out.append("")
    return "\n".join(out)
