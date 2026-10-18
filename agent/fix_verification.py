"""
The 2026-10 execution fixes, verified on production (docs/FIX_VERIFICATION.md).

One check per fixed defect, each returning PASS / FAIL / PENDING / UNVERIFIED with the evidence:

  D1     the account is flat after the close
  D2/D3  every live pick carries the broker's result beside the simulator's
  D4     the shadow ledger counts only what the filters stopped
  D5     every pick says whether its entry order expresses its setup
  D6     the grader is told how its orders work, and its claims about them are audited
  D7     a rule whose cell had rows received its forward observations

Two callers:
  * the evening self-check (agent/selfcheck.py, check_fix_verification) judges each
    finished session in production and keeps the 5-session streak in the evening email;
  * scripts/verify_fixes.py, the read-only command line (opens production read-only,
    GET-only broker reads) with the one-off --impact report of what each fix changes
    in history, re-priced in an in-memory database.

Nothing here writes to the ledger or sends an order: every check is a read.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PASS, FAIL, PENDING, UNVERIFIED = "PASS", "FAIL", "PENDING", "UNVERIFIED"
_SEVERITY = {PASS: 0, PENDING: 1, UNVERIFIED: 2, FAIL: 3}


@dataclass
class Result:
    item: str
    title: str
    status: str = PASS
    lines: list[str] = field(default_factory=list)

    def worse(self, status: str, line: str | None = None) -> None:
        if _SEVERITY[status] > _SEVERITY[self.status]:
            self.status = status
        if line:
            self.lines.append(line)


# ------------------------------------------------------------------ read-only access

def open_readonly(dsn: str | None = None, path: str | None = None) -> tuple[Any, str]:
    """A connection that cannot write. Never ledger.connect(): that runs migrations."""
    dsn = os.environ.get("DATABASE_URL") if dsn is None else dsn
    if dsn:
        from agent.pgshim import connect_pg
        con = connect_pg(dsn)
        con._conn.set_session(readonly=True, autocommit=True)
        return con, "production Postgres (read-only session)"
    import duckdb

    from agent import ledger
    p = str(path or ledger.DB_PATH)
    return duckdb.connect(p, read_only=True), f"local DuckDB {p} (read-only)"


def install_get_only_guard() -> None:
    """Refuse every broker call that is not a GET, before it leaves the process."""
    from agent import live_broker
    if getattr(live_broker._request, "_verify_fixes_guard", False):
        return
    real = live_broker._request

    def guarded(method, path, *, cfg=None, payload=None):
        if str(method).upper() != "GET" or payload is not None:
            raise RuntimeError(f"verify_fixes is read-only: refused {method} {path}")
        return real(method, path, cfg=cfg, payload=None)

    guarded._verify_fixes_guard = True
    live_broker._request = guarded


class Broker:
    """The paper account, read with GETs only."""

    def __init__(self, cfg):
        self.cfg = cfg

    def positions(self) -> list[dict] | None:
        from agent import live_broker
        return live_broker.list_positions(cfg=self.cfg)

    def fills(self, d: date) -> list[dict] | None:
        from agent import live_broker
        return live_broker.fills_for_date(d, cfg=self.cfg)


def _cols(con, table: str) -> set[str]:
    try:
        return {r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()}
    except Exception:  # noqa: BLE001 -- a table that does not exist yet
        return set()


def _q(con, names: list[str], sql: str, params: list | None = None) -> list[dict]:
    return [dict(zip(names, r)) for r in con.execute(sql, params or []).fetchall()]


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_r(v) -> str:
    v = _num(v)
    return "n/a" if v is None else f"{v:+.2f}R"


def _evening_ran(con, d: date) -> bool:
    try:
        row = con.execute("SELECT COUNT(*) FROM runs WHERE loop = 'evening' AND run_date = ? "
                          "AND status IN ('ok', 'degraded')", [d]).fetchone()
        return bool(row and row[0])
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------------------ D1

_UNCLOSED = {"refused", "failed", "rejected", "canceled", "cancelled", "expired"}


def _net_by_symbol(fills: list[dict]) -> dict[str, float]:
    net: dict[str, float] = {}
    for f in fills or []:
        sym = str(f.get("symbol") or "").upper()
        side = str(f.get("side") or "").lower()
        sign = 1 if side in ("buy", "buy_to_cover") else -1 if side in ("sell", "sell_short") else 0
        net[sym] = net.get(sym, 0.0) + sign * float(f.get("qty") or 0)
    return net


def check_d1(con, d: date, cfg, broker, now: datetime) -> Result:
    """Zero broker positions after the close; every sweep close explained."""
    from agent.util import is_market_day, market_close_et
    res = Result("D1", "the account is flat after the close")
    allow = {str(s).upper() for s in ((cfg.get("live_paper") or {}).get("allow_overnight_symbols") or [])}
    if not _cols(con, "eod_backstop_closes"):
        res.worse(FAIL, "table eod_backstop_closes does not exist: schema v36 is not deployed")
        return res
    names = ["kind", "symbol", "position_side", "qty", "avg_entry_price", "reason", "status",
             "carried_from"]
    rows = _q(con, names, f"SELECT {', '.join(names)} FROM eod_backstop_closes "
                          "WHERE session_date = ? ORDER BY submitted_at, symbol", [d])
    for r in rows:
        what = (f"{r['kind']}: {r['symbol']} {r['position_side'] or ''} {_num(r['qty']) or 0:g} sh "
                f"({r['reason']}"
                + (f", carried from {r['carried_from']}" if r.get("carried_from") else "")
                + f") -> {r['status']}")
        if str(r["status"] or "").lower() in _UNCLOSED:
            res.worse(FAIL, "the sweep's close did NOT go through: " + what)
        else:
            res.lines.append("FLATTEN MISSED, closed by the sweep: " + what)

    close_dt = datetime.combine(d, market_close_et(d), tzinfo=ET)
    if now.date() == d:
        if now < close_dt + timedelta(minutes=1):
            res.worse(PENDING, f"the account can only be judged after {close_dt:%H:%M} ET")
            return res
        pos = broker.positions() if broker is not None else None
        if pos is None:
            res.worse(UNVERIFIED, "the broker's positions could not be read")
            return res
        held = [p for p in pos if str(p.get("symbol") or "").upper() not in allow]
        for p in held:
            res.worse(FAIL, f"STILL OPEN after the close: {p.get('symbol')} {p.get('qty')} sh "
                            f"{p.get('side') or ''} @ {p.get('avg_entry_price')}")
        if not held:
            res.lines.append(f"broker: 0 positions after the close"
                             + (f" ({len(pos)} allow-listed)" if pos else ""))
        return res

    # A past session: the day's own fills must net to zero, except an earlier carry the
    # morning sweep closed that day.
    fills = broker.fills(d) if broker is not None else None
    if fills is None:
        res.worse(UNVERIFIED, f"the broker's fills for {d} could not be read")
        return res
    swept = {str(r["symbol"]).upper(): _num(r["qty"]) or 0.0 for r in rows
             if r["kind"] == "morning_sweep" and str(r["status"] or "").lower() not in _UNCLOSED}
    flat = True
    for sym, q in sorted(_net_by_symbol(fills).items()):
        if abs(q) < 1e-9 or sym in allow:
            continue
        if sym in swept and abs(abs(q) - swept[sym]) < 1e-9:
            res.lines.append(f"{sym}: net {q:+g} sh on {d} = the earlier carry the morning sweep closed")
            continue
        flat = False
        res.worse(FAIL, f"{sym}: the day's fills net {q:+g} sh, so the position was NOT flat after the close")
    if flat:
        res.lines.append(f"broker: the {d} fills net to zero for every symbol ({len(fills)} fill(s))")
    # A position carried THROUGH the session (opened earlier, still held after the close)
    # leaves no fill that day; the next session's morning sweep is what finds it.
    nxt = d + timedelta(days=1)
    while not is_market_day(nxt):
        nxt += timedelta(days=1)
    for r in _q(con, names, f"SELECT {', '.join(names)} FROM eod_backstop_closes "
                            "WHERE session_date = ? AND kind = 'morning_sweep' ORDER BY submitted_at, symbol",
                [nxt]):
        res.worse(FAIL, f"{r['symbol']} {r['position_side'] or ''} {_num(r['qty']) or 0:g} sh was still open "
                        f"after the {d} close: the {nxt} morning sweep found it ({r['reason']}, {r['status']})")
    return res


# -------------------------------------------------------------------------- D2 / D3

def check_d2_d3(con, d: date) -> Result:
    """Every live pick has broker fields populated; every divergence is listed."""
    res = Result("D2/D3", "every live pick carries the broker's result beside the simulator's")
    need = {"broker_status", "broker_entry_fill", "broker_exit_fill", "sim_broker_divergence",
            "touch_only_bars"}
    missing = sorted(need - _cols(con, "outcomes"))
    if missing:
        res.worse(FAIL, f"outcomes lacks {', '.join(missing)}: schema v37 is not deployed")
        return res
    live = _q(con, ["live_order_id", "pick_id", "ticker", "status", "entry_fill_price", "live_r"],
              "SELECT live_order_id, pick_id, ticker, status, entry_fill_price, live_r FROM live_orders "
              "WHERE session_date = ? AND pick_id IS NOT NULL ORDER BY pick_id", [d])
    names = ["pick_id", "triggered", "r_planned", "r_multiple", "exit_reason", "broker_status",
             "broker_entry_fill", "sim_broker_divergence", "touch_only_bars"]
    outs = {o["pick_id"]: o for o in _q(
        con, names + ["ticker", "entry_type", "entry_price"],
        f"SELECT {', '.join('o.' + n for n in names)}, p.ticker, p.entry_type, p.entry_price "
        "FROM outcomes o JOIN picks p ON p.pick_id = o.pick_id WHERE p.plan_date = ?", [d])}
    ran = _evening_ran(con, d)
    if not live:
        res.lines.append("no pick went to the broker this session")
    for row in live:
        o = outs.get(row["pick_id"])
        t = row["ticker"]
        if o is None:
            res.worse(PENDING if not ran else FAIL,
                      f"{t}: no simulated outcome yet" + ("" if not ran else " although the evening ran"))
            continue
        if o["broker_status"] is None or o["sim_broker_divergence"] is None:
            res.worse(PENDING if not ran else FAIL, f"{t}: the broker's result is not recorded beside the simulation")
            continue
        if row["entry_fill_price"] is not None and o["broker_entry_fill"] is None:
            res.worse(FAIL, f"{t}: the broker filled the entry at {row['entry_fill_price']} but the outcome has no broker fill")
            continue
        div = o["sim_broker_divergence"]
        if div != "none":
            res.lines.append(f"DIVERGENCE {t}: {div} (simulated {_fmt_r(o['r_planned'] if o['r_planned'] is not None else o['r_multiple'])}, "
                             f"broker {_fmt_r(row['live_r'])}, broker status {o['broker_status']})")
        else:
            res.lines.append(f"{t}: broker and simulator agree ({o['broker_status']})")
    for o in outs.values():
        if str(o.get("entry_type") or "") == "limit" and not o["triggered"] and (o["touch_only_bars"] or 0) >= 1:
            res.lines.append(f"touch, not a fill: {o['ticker']} limit {o['entry_price']} reached on "
                             f"{o['touch_only_bars']} bar(s) without trading through it")
    return res


# ------------------------------------------------------------------------------ D4

def _row(con, table: str, where: str, params: list) -> dict | None:
    cols = [c[0] for c in con.execute(f"DESCRIBE {table}").fetchall()]
    r = con.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchone()
    return dict(zip(cols, r)) if r else None


def check_d4(con, d: date, md=None, cfg=None) -> Result:
    """For every pick both rejected and taken, shadow R == taken R; no traded pick in forgone R."""
    from agent.grading import TRADED_VIA_LANE, realized_r, shadow_pick, simulate_pick
    res = Result("D4", "the shadow ledger counts only what the filters stopped")
    names = ["rejection_id", "ticker", "direction", "eod_outcome", "forgone_r", "pick_id",
             "exploration_lane", "r_planned", "r_multiple", "triggered", "exit_reason"]
    overlap = _q(con, names, """
        SELECT r.rejection_id, r.ticker, r.direction, r.eod_outcome, r.forgone_r, p.pick_id,
               p.exploration_lane, o.r_planned, o.r_multiple, o.triggered, o.exit_reason
        FROM rejections r
        JOIN picks p ON p.plan_date = r.plan_date AND UPPER(p.ticker) = UPPER(r.ticker)
                    AND LOWER(p.direction) = LOWER(r.direction)
        LEFT JOIN outcomes o ON o.pick_id = p.pick_id
        WHERE r.plan_date = ? ORDER BY r.rejection_id""", [d])
    ungraded = con.execute("SELECT COUNT(*) FROM rejections WHERE plan_date = ? AND eod_outcome IS NULL",
                           [d]).fetchone()[0]
    if ungraded:
        res.worse(PENDING if not _evening_ran(con, d) else FAIL,
                  f"{ungraded} rejection(s) not graded yet")
    if not overlap:
        res.lines.append("no rejected candidate was also traded this session")
    bars_for = {}
    for row in overlap:
        t, lane = row["ticker"], row["exploration_lane"] or "main"
        if row["eod_outcome"] is None:
            continue
        if row["forgone_r"] is not None:
            res.worse(FAIL, f"{t} was traded via {lane} but carries forgone R {_fmt_r(row['forgone_r'])}: "
                            "it is counted as if the filters had stopped it")
            continue
        if row["eod_outcome"] != TRADED_VIA_LANE:
            res.worse(FAIL, f"{t} was traded via {lane} but is marked {row['eod_outcome']}")
            continue
        taken = _num(row["r_planned"]) if row["r_planned"] is not None else _num(row["r_multiple"])
        line = f"{t} traded via {lane}: excluded from forgone R, taken {_fmt_r(taken) if row['triggered'] else 'no fill'}"
        if md is None:
            res.lines.append(line + " (shadow R not re-priced: no market data)")
            continue
        try:
            rej = _row(con, "rejections", "rejection_id = ?", [row["rejection_id"]])
            pick = _row(con, "picks", "pick_id = ?", [row["pick_id"]])
            if t not in bars_for:
                bars_for[t] = md.minute_bars(t, d)
            sim = simulate_pick(shadow_pick(rej, pick=pick), bars_for[t], None,
                                slippage_bps=float((cfg or {}).get("slippage_bps", 5) if cfg is not None else 5),
                                flatten_at=(cfg.get("flatten_at") if cfg is not None else None),
                                min_bar_volume=float((cfg.get("min_bar_volume", 0) if cfg is not None else 0) or 0),
                                session_date=d)
        except Exception as e:  # noqa: BLE001
            res.worse(UNVERIFIED, line + f" (shadow could not be re-priced: {type(e).__name__}: {e})")
            continue
        filled = bool(sim.get("triggered")) and sim.get("exit_reason") != "invalid_fill"
        shadow = realized_r(sim) if filled else None
        taken_cmp = taken if row["triggered"] and row["exit_reason"] != "invalid_fill" else None
        if (shadow is None) != (taken_cmp is None) or (shadow is not None and abs(shadow - taken_cmp) > 1e-6):
            res.worse(FAIL, f"{t}: shadow R {_fmt_r(shadow)} != taken R {_fmt_r(taken_cmp)}")
        else:
            res.lines.append(line + f"; shadow re-priced {_fmt_r(shadow) if shadow is not None else 'no fill'} = taken")
    return res


# ------------------------------------------------------------------------------ D5

def check_d5(con, d: date) -> Result:
    """Every pick has entry_semantics set."""
    res = Result("D5", "every pick says whether its entry order expresses its setup")
    if "entry_semantics" not in _cols(con, "picks"):
        res.worse(FAIL, "picks.entry_semantics does not exist: schema v38 is not deployed")
        return res
    picks = _q(con, ["ticker", "setup_type", "entry_type", "exploration_lane", "entry_semantics",
                     "entry_ref_price"],
               "SELECT ticker, setup_type, entry_type, exploration_lane, entry_semantics, entry_ref_price "
               "FROM picks WHERE plan_date = ? ORDER BY pick_id", [d])
    if not picks:
        plan = con.execute("SELECT COUNT(*) FROM daily_plans WHERE plan_date = ?", [d]).fetchone()[0]
        res.worse(PASS if plan else PENDING, "no picks this session" if plan else "the morning has not run")
        return res
    counts: dict[str, int] = {}
    for p in picks:
        tag = p["entry_semantics"]
        if not tag:
            res.worse(FAIL, f"{p['ticker']}: no entry_semantics tag")
            continue
        counts[tag] = counts.get(tag, 0) + 1
        if tag != "ok":
            res.lines.append(f"{p['ticker']} ({p['exploration_lane'] or 'main'}, {p['setup_type']}, "
                             f"{p['entry_type']} vs reference {p['entry_ref_price']}): {tag}")
    res.lines.insert(0, "tags: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return res


# ------------------------------------------------------------------------------ D6

def _stage(stages_text: str | None, key: str):
    """A value from runs.stages, which is JSON cut at 4000 characters."""
    if not stages_text:
        return None
    try:
        return json.loads(stages_text).get(key)
    except (TypeError, ValueError):
        m = re.search(rf'"{re.escape(key)}":\s*"([^"]*)"', stages_text)
        return m.group(1) if m else None


def _mechanics_flags(stages_text: str | None) -> int | None:
    audit = None
    try:
        audit = json.loads(stages_text or "").get("audit")
    except (TypeError, ValueError):
        m = re.search(r'"by_check":\s*(\{[^}]*\})', stages_text or "")
        if m:
            try:
                return int(json.loads(m.group(1)).get("mechanics_contradiction", 0))
            except ValueError:
                return None
    if not isinstance(audit, dict) or "by_check" not in audit:
        return None
    return int((audit.get("by_check") or {}).get("mechanics_contradiction", 0))


def check_d6(con, d: date, current: str | None = None) -> Result:
    """The grader's prompt_version is current; count of mechanics_contradiction flags."""
    from agent.llm import prompt_version
    current = current or prompt_version("evening_grader")
    res = Result("D6", "the grader is told how its orders work, and its claims about them are audited")
    rows = con.execute("SELECT stages, status FROM runs WHERE loop = 'evening' AND run_date = ? "
                       "ORDER BY run_id DESC LIMIT 1", [d]).fetchall()
    if not rows:
        res.worse(PENDING, f"the evening has not run for {d}")
        return res
    stages, status = rows[0]
    version = _stage(stages, "grader_prompt_version")
    if version is None:
        res.worse(FAIL, f"the {d} evening ({status}) recorded no grader prompt version: it ran on code "
                        "from before this fix")
        return res
    if str(version) != str(current):
        res.worse(FAIL, f"graded with prompt v{version}; the current grader prompt is v{current}")
    else:
        res.lines.append(f"grader prompt v{version} (current)")
    flags = _mechanics_flags(stages)
    res.lines.append("mechanics_contradiction flags: " + ("not recorded" if flags is None else str(flags)))
    return res


# ------------------------------------------------------------------------------ D7

def check_d7(con, d: date, now: datetime) -> Result:
    """Forward observations > 0 for any rule whose cell had matching rows that day."""
    from agent import research
    from agent.clusters import units
    res = Result("D7", "a rule whose cell had rows today received its forward observations")
    rules = con.execute(
        "SELECT rule_id, lane, predicate_json, quarantined_at FROM research_rules "
        "WHERE state IN ('QUARANTINED', 'VALIDATED') AND predicate_json IS NOT NULL ORDER BY rule_id"
    ).fetchall()
    if not rules:
        res.lines.append("no rule is under forward test")
        return res
    # the evening books morning-lane observations; the nightly (~23:00 ET) books the gap field's
    booked_at = {"morning": datetime.combine(d, dtime(17, 30), tzinfo=ET),
                 "gapfield": datetime.combine(d, dtime(23, 45), tzinfo=ET)}
    cache: dict[str, list[dict]] = {}
    for rid, lane, pred_json, q_at in rules:
        try:
            pred = json.loads(pred_json)
        except (TypeError, ValueError):
            continue
        q_day = q_at.date() if hasattr(q_at, "date") else q_at
        if q_day is not None and d <= q_day:
            continue                                   # in-sample for this rule, by design
        if lane not in cache:
            cache[lane] = (research.morning_rows(con, d) if lane == "morning"
                           else research.gapfield_rows(con, d) if lane == "gapfield" else [])
        hits = [r for r in cache[lane] if research.matches(pred, r)]
        if not hits:
            continue
        expected = len(units(hits, research.realized_r)) if lane == "gapfield" else len(hits)
        recorded = con.execute(
            "SELECT COUNT(*) FROM research_observations WHERE rule_id = ? AND session_date = ? "
            "AND source IN ('taken', 'gapfield')", [rid, d]).fetchone()[0]
        what = f"{rid} ({lane}): {len(hits)} matching row(s), {expected} unit(s), {recorded} forward observation(s)"
        if recorded:
            res.lines.append(what)
        elif now < booked_at.get(lane, booked_at["morning"]):
            res.worse(PENDING, what + f" -- booked by {'the nightly' if lane == 'gapfield' else 'the evening'}, "
                                      f"after {booked_at.get(lane, booked_at['morning']):%H:%M} ET")
        else:
            res.worse(FAIL, what + " -- rows matched and nothing was recorded")
    if not res.lines and res.status == PASS:
        res.lines.append("no rule's cell had a matching row this session")
    return res


# -------------------------------------------------------------------------- the run

def run_checks(con, d: date, cfg, broker=None, md=None, now: datetime | None = None) -> list[Result]:
    from agent.util import now_et
    now = now or now_et()
    out = []
    for fn, args in ((check_d1, (con, d, cfg, broker, now)), (check_d2_d3, (con, d)),
                     (check_d4, (con, d, md, cfg)), (check_d5, (con, d)), (check_d6, (con, d)),
                     (check_d7, (con, d, now))):
        try:
            out.append(fn(*args))
        except Exception as e:  # noqa: BLE001 -- one broken check never hides the others
            name = {check_d1: "D1", check_d2_d3: "D2/D3", check_d4: "D4", check_d5: "D5",
                    check_d6: "D6", check_d7: "D7"}[fn]
            out.append(Result(name, "check raised", UNVERIFIED, [f"{type(e).__name__}: {e}"]))
    return out


def render(d: date, results: list[Result]) -> str:
    lines = [f"verify_fixes {d}"]
    for r in results:
        lines.append(f"{r.status:<10} {r.item:<6} {r.title}")
        lines += [f"           - {x}" for x in r.lines]
    n_pass = sum(1 for r in results if r.status == PASS)
    lines.append(f"{n_pass}/{len(results)} PASS" + ("" if n_pass == len(results) else
                                                    " -- this session does not count toward the 5"))
    return "\n".join(lines)


# ============================================================ --impact (history, once)

def _plain(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return v


_COPY = (
    ("daily_plans", "plan_date >= ?"),
    ("picks", "plan_date >= ?"),
    ("rejections", "plan_date >= ?"),
    ("outcomes", "pick_id IN (SELECT pick_id FROM picks WHERE plan_date >= ?)"),
)


def _copy_table(src, dst, table: str, where: str, since: date) -> int:
    dcols = [r[0] for r in dst.execute(f"DESCRIBE {table}").fetchall()]
    scols = _cols(src, table)
    cols = [c for c in dcols if c in scols]
    rows = src.execute(f"SELECT {', '.join(cols)} FROM {table} WHERE {where}", [since]).fetchall()
    if rows:
        dst.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        [tuple(_plain(v) for v in r) for r in rows])
    return len(rows)


def scratch_copy(src, since: date):
    """An in-memory ledger holding the window's plans, picks, rejections and outcomes."""
    import duckdb

    from agent import ledger
    scratch = duckdb.connect(":memory:")
    ledger._apply_schema(scratch, lambda s: s)
    return scratch, {t: _copy_table(src, scratch, t, w, since) for t, w in _COPY}


def _rejection_rows(con) -> dict[int, dict]:
    names = ["rejection_id", "plan_date", "ticker", "eod_outcome", "forgone_r"]
    return {r["rejection_id"]: r for r in _q(con, names, f"SELECT {', '.join(names)} FROM rejections")}


def shadow_impact(src, md, cfg, since: date) -> dict:
    """D4: re-price every rejection on/after `since` with the fixed code, in memory, through
    the evening's own _grade_rejections (what `run.py regrade-shadow --apply` would run)."""
    from agent import ledger
    from agent.loops import evening
    from agent.loops.weekly import shadow_verdict
    scratch, copied = scratch_copy(src, since)
    before_sum = ledger.shadow_summary(scratch, since=since)
    before = _rejection_rows(scratch)
    dates = [r[0] for r in scratch.execute("SELECT DISTINCT plan_date FROM rejections ORDER BY 1").fetchall()]
    lane = []
    with contextlib.redirect_stdout(io.StringIO()):
        for d in dates:
            scratch.execute("UPDATE rejections SET eod_outcome = NULL, forgone_r = NULL, graded_at = NULL, "
                            "sim_version = NULL WHERE plan_date = ?", [d])
            tickers = [r[0] for r in scratch.execute(
                "SELECT DISTINCT ticker FROM rejections WHERE plan_date = ?", [d]).fetchall()]
            with contextlib.suppress(Exception):
                md.prefetch_session(tickers, d, daily=False)
            shadow = evening._grade_rejections(
                scratch, md, d, slippage_bps=float(cfg.get("slippage_bps", 5)),
                flatten_at=cfg.get("flatten_at"), min_bar_volume=float(cfg.get("min_bar_volume", 0) or 0))
            lane += [{**t, "date": d} for t in shadow["traded_via_lane"]]
    after = _rejection_rows(scratch)
    after_sum = ledger.shadow_summary(scratch, since=since)
    by_date: dict = {}
    for rid, b in before.items():
        s = by_date.setdefault(b["plan_date"], {"n": 0, "before": 0.0, "after": 0.0, "lane": 0})
        a = after.get(rid, {})
        s["n"] += 1
        s["before"] += _num(b["forgone_r"]) or 0.0
        s["after"] += _num(a.get("forgone_r")) or 0.0
        s["lane"] += 1 if a.get("eod_outcome") == "traded_via_lane" else 0
    lane_stored = 0.0
    for t in lane:
        match = [b for b in before.values()
                 if b["plan_date"] == t["date"] and str(b["ticker"]).upper() == str(t["ticker"]).upper()]
        t["stored_forgone_r"] = match[0]["forgone_r"] if match else None
        lane_stored += _num(t["stored_forgone_r"]) or 0.0
    stored_total = sum(_num(b["forgone_r"]) or 0.0 for b in before.values())
    return {"since": since, "copied": copied, "dates": dates, "before": before_sum, "after": after_sum,
            "verdict_before": shadow_verdict(before_sum), "verdict_after": shadow_verdict(after_sum),
            "by_date": by_date, "lane": lane, "lane_stored_forgone_r": round(lane_stored, 2) + 0.0,
            # + 0.0 turns a -0.0 into 0.0, which would otherwise print as "-0.00R"
            "exclusion_only_net": round(stored_total - lane_stored, 2) + 0.0}


def format_shadow_impact(res: dict) -> str:
    if not res.get("dates"):
        return "D4 SHADOW LEDGER: no rejections in the window."
    b, a = res["before"], res["after"]
    out = [f"D4 SHADOW LEDGER from {res['since']}: {len(res['dates'])} session(s), "
           f"{sum(s['n'] for s in res['by_date'].values())} rejection(s) re-priced in memory",
           "  session      rows   stored net   re-priced net   traded via lane"]
    for d in sorted(res["by_date"]):
        s = res["by_date"][d]
        out.append(f"  {d}  {s['n']:>4}   {s['before']:>+9.2f}R   {s['after']:>+12.2f}R   {s['lane']:>5}")
    out += ["",
            f"  net_forgone_r  stored {b['net_forgone_r']:+.2f}R",
            f"    lane rows removed only (no re-pricing): {res['exclusion_only_net']:+.2f}R "
            f"[{len(res['lane'])} rejection(s) the desk traded anyway, {res['lane_stored_forgone_r']:+.2f}R of the stored net]",
            f"    fully re-priced (lane rows out, time stops, planned-risk R, sim v8): {a['net_forgone_r']:+.2f}R",
            f"  scored rows {b['scored_rows']} -> {a['scored_rows']}; mean forgone {b['mean_forgone_r']} -> "
            f"{a['mean_forgone_r']}; t/p {b['t_forgone']}/{b['p_forgone']} -> {a['t_forgone']}/{a['p_forgone']}"]
    if res["lane"]:
        out.append("  traded via lane:")
        for t in res["lane"]:
            tr = (_fmt_r(t["taken_r"]) if t.get("taken_r") is not None
                  else {"invalid_fill": "invalid fill", "ungraded": "not graded"}.get(t.get("taken_status"), "no fill"))
            out.append(f"    {t['date']} {t['ticker']:<6} ({t['lane']}) stored forgone {_fmt_r(t['stored_forgone_r'])}; "
                       f"taken {tr}; shadow now {_fmt_r(t['shadow_r'])}")
    out += [f"  weekly verdict BEFORE: {res['verdict_before']}",
            f"  weekly verdict AFTER : {res['verdict_after']}"]
    return "\n".join(out)


def _graded_picks(con, since: date, where: str = "") -> list[dict]:
    pcols = [c[0] for c in con.execute("DESCRIBE picks").fetchall()]
    ocols = ["triggered", "entry_fill", "exit_reason", "r_planned", "r_multiple", "execution_anomaly",
             "grade", "grade_notes", "touch_only_bars", "sim_version"]
    have = _cols(con, "outcomes")
    ocols = [c for c in ocols if c in have]
    rows = con.execute(
        f"SELECT {', '.join('p.' + c for c in pcols)}, {', '.join('o.' + c + ' AS o_' + c for c in ocols)} "
        f"FROM picks p JOIN outcomes o ON o.pick_id = p.pick_id WHERE p.plan_date >= ? {where} "
        "ORDER BY p.plan_date, p.pick_id", [since]).fetchall()
    out = []
    for r in rows:
        row = dict(zip(pcols, r[:len(pcols)]))
        row["stored"] = dict(zip(ocols, r[len(pcols):]))
        out.append(row)
    return out


def _sim(md, cfg, pick: dict, **over):
    from agent.grading import simulate_pick
    d = pick["plan_date"]
    bars = md.minute_bars(pick["ticker"], d)
    return simulate_pick(pick, bars, None, slippage_bps=float(cfg.get("slippage_bps", 5)),
                         flatten_at=cfg.get("flatten_at"),
                         min_bar_volume=float(cfg.get("min_bar_volume", 0) or 0), session_date=d, **over)


def limit_fill_impact(con, md, cfg, since: date) -> str:
    """D2: every graded LIMIT pick re-simulated under the touch rule and the trade-through
    rule on the same bars -- the difference is the fix alone."""
    from agent import grading
    from agent.grading import realized_r
    picks = _graded_picks(con, since, "AND p.entry_type = 'limit'")
    removed, kept, n_touch, n_through = [], 0, 0, 0
    saved = (grading.LIMIT_FILL_CUSHION_BPS, grading.LIMIT_FILL_TICK)
    with contextlib.redirect_stdout(io.StringIO()):
        for p in picks:
            try:
                grading.LIMIT_FILL_CUSHION_BPS, grading.LIMIT_FILL_TICK = 0.0, 0.0
                old = _sim(md, cfg, p)
                grading.LIMIT_FILL_CUSHION_BPS, grading.LIMIT_FILL_TICK = saved
                new = _sim(md, cfg, p)
            except Exception:  # noqa: BLE001 -- no bars for a session: skip it
                continue
            finally:
                grading.LIMIT_FILL_CUSHION_BPS, grading.LIMIT_FILL_TICK = saved
            n_touch += 1 if old["triggered"] else 0
            n_through += 1 if new["triggered"] else 0
            if old["triggered"] and not new["triggered"]:
                removed.append((p["plan_date"], p["ticker"], p["entry_price"], realized_r(old),
                                new.get("touch_only_bars")))
            elif old["triggered"]:
                kept += 1
    lines = [f"D2 LIMIT FILLS from {since}: {len(picks)} graded limit pick(s) re-simulated on the same bars",
             f"  touch rule (sim v7) fills {n_touch}; trade-through rule (sim v8) fills {n_through}",
             f"  fills only a touch produced: {len(removed)}"
             + (f", their R summed {sum(r[3] or 0 for r in removed):+.2f}R (booked, never real)" if removed else "")]
    lines += [f"    {d} {t} limit {e}: touch-rule R {_fmt_r(r)}, touched {n} bar(s)" for d, t, e, r, n in removed]
    return "\n".join(lines)


def _premarket_ref(md, ticker: str, d: date) -> float | None:
    """Last 1-minute premarket close at or before 07:10 ET (the morning plan's clock)."""
    alp = getattr(md, "alpaca", None)
    if alp is None:
        return None
    try:
        got = alp.multi_bars([ticker], "1Min", datetime.combine(d, dtime(4, 0), tzinfo=ET),
                             datetime.combine(d, dtime(7, 10), tzinfo=ET)).get(ticker.upper()) or []
    except Exception:  # noqa: BLE001
        return None
    return float(got[-1]["c"]) if got else None


def entry_semantics_table(con, md, since: date) -> str:
    """D5: graded picks by entry_semantics, with fill rate and mean R. History before the tag
    existed is tagged here from the stored levels; the reference price is the stored one, else
    the last premarket print at or before 07:10 ET."""
    from agent.grading import realized_r
    from agent.loops.morning import _entry_semantics
    picks = _graded_picks(con, since)
    table: dict[str, dict] = {}
    recomputed = 0
    for p in picks:
        tag = p.get("entry_semantics")
        if not tag:
            ref = _num(p.get("entry_ref_price"))
            if ref is None and str(p.get("setup_type") or "") in ("catalyst_momentum", "sector_rotation") \
                    and str(p.get("entry_type") or "") == "limit" and md is not None:
                ref = _premarket_ref(md, p["ticker"], p["plan_date"])
            tag, _ = _entry_semantics(p.get("setup_type"), p.get("direction"), p.get("entry_type"),
                                      _num(p.get("entry_price")), ref, time_stop=p.get("time_stop"))
            recomputed += 1
        s = p["stored"]
        if s.get("exit_reason") in ("invalid_fill", "no_data", "invalid_plan"):
            continue
        row = table.setdefault(tag, {"n": 0, "filled": 0, "r": []})
        row["n"] += 1
        if s.get("triggered"):
            row["filled"] += 1
            r = realized_r({"r_planned": _num(s.get("r_planned")), "r_multiple": _num(s.get("r_multiple"))})
            if r is not None:
                row["r"].append(r)
    lines = [f"D5 ENTRY SEMANTICS from {since}: {sum(v['n'] for v in table.values())} graded pick(s) "
             f"({recomputed} tagged here from stored levels)",
             "  tag                               n   filled  fill rate   mean R (filled)   sum R"]
    for tag, v in sorted(table.items(), key=lambda kv: (kv[0] != "ok", kv[0])):
        mean = sum(v["r"]) / len(v["r"]) if v["r"] else None
        lines.append(f"  {tag:<32} {v['n']:>3}   {v['filled']:>5}   {v['filled'] / v['n']:>7.0%}   "
                     f"{_fmt_r(mean):>15}   {_fmt_r(sum(v['r']))}")
    return "\n".join(lines)


def held_counterfactual(con, md, cfg, since: date) -> str:
    """Phase 5: for time-stopped trades, r_if_held_to_flatten minus the R the time stop booked."""
    picks = [p for p in _graded_picks(con, since) if p["stored"].get("exit_reason") == "time_stop"]
    diffs_m, diffs_p, rows = [], [], []
    with contextlib.redirect_stdout(io.StringIO()):
        for p in picks:
            try:
                out = _sim(md, cfg, p)
            except Exception:  # noqa: BLE001
                continue
            held = out.get("r_if_held_to_flatten")
            rm, rp = _num(p["stored"].get("r_multiple")), _num(p["stored"].get("r_planned"))
            if held is None or rm is None:
                continue
            diffs_m.append(held - rm)
            if rp is not None:
                diffs_p.append(held - rp)
            rows.append((p["plan_date"], p["ticker"], rp if rp is not None else rm, held))
    if not rows:
        return f"PHASE 5 HELD-TO-FLATTEN from {since}: no time-stopped trade could be re-simulated"
    lines = [f"PHASE 5 HELD-TO-FLATTEN from {since}: {len(rows)} time-stopped trade(s)",
             f"  r_if_held_to_flatten - r_multiple: sum {sum(diffs_m):+.2f}R, mean {sum(diffs_m) / len(diffs_m):+.3f}R",
             (f"  r_if_held_to_flatten - r_planned:  sum {sum(diffs_p):+.2f}R, mean {sum(diffs_p) / len(diffs_p):+.3f}R"
              if diffs_p else "  (no r_planned on these rows)"),
             "  reference only: none of this enters any total"]
    lines += [f"    {d} {t}: time stop {_fmt_r(r)}, held {_fmt_r(h)}" for d, t, r, h in rows[:25]]
    return "\n".join(lines)


def mechanics_replay(con, since: date) -> str:
    """D6: the stored pick reviews the new audit rule would have flagged."""
    from agent import review_audit
    picks = _graded_picks(con, since, "AND o.grade_notes IS NOT NULL")
    flagged = []
    for p in picks:
        s = p["stored"]
        o = {"triggered": s.get("triggered"), "exit_reason": s.get("exit_reason"),
             "execution_anomaly": s.get("execution_anomaly"), "entry_fill": _num(s.get("entry_fill"))}
        why = review_audit._mechanics_conflict(str(s.get("grade_notes") or ""), p, o)
        if why:
            flagged.append((p["plan_date"], p["ticker"], s.get("grade"), str(s.get("grade_notes"))[:140]))
    lines = [f"D6 MECHANICS AUDIT from {since}: {len(flagged)} of {len(picks)} stored pick review(s) "
             "would have been flagged mechanics_contradiction"]
    lines += [f"    {d} {t} (grade {g}): \"{txt}\"" for d, t, g, txt in flagged]
    return "\n".join(lines)


def etf_gate_kills(con, since: date) -> str:
    """D8: fund picks the grounding gate killed as ungrounded (the price came from memory)."""
    from agent.providers import NAMEABLE_FUNDS, SECTOR_ETFS
    funds = set(NAMEABLE_FUNDS) | set(SECTOR_ETFS) | {"USO"}
    kills = []
    for d, plan_json in con.execute("SELECT plan_date, plan_json FROM daily_plans WHERE plan_date >= ? "
                                    "ORDER BY plan_date", [since]).fetchall():
        try:
            drops = (json.loads(plan_json) if isinstance(plan_json, str) else plan_json or {}).get("validator_drops") or []
        except (TypeError, ValueError):
            continue
        for x in drops:
            if str(x.get("ticker", "")).upper() in funds and "ungrounded" in str(x.get("reason", "")):
                kills.append((d, x["ticker"], str(x["reason"])[:120]))
    lines = [f"D8 ETF GROUNDING from {since}: {len(kills)} fund pick(s) the 15% gate killed as priced from memory"]
    lines += [f"    {d} {t}: {why}" for d, t, why in kills]
    return "\n".join(lines)


def impact_report(con, md, cfg, since: date) -> str:
    parts = []
    for title, fn in (("D4", lambda: format_shadow_impact(shadow_impact(con, md, cfg, since))),
                      ("D2", lambda: limit_fill_impact(con, md, cfg, since)),
                      ("D5", lambda: entry_semantics_table(con, md, since)),
                      ("Phase 5", lambda: held_counterfactual(con, md, cfg, since)),
                      ("D6", lambda: mechanics_replay(con, since)),
                      ("D8", lambda: etf_gate_kills(con, since))):
        try:
            parts.append(fn())
        except Exception as e:  # noqa: BLE001 -- one section never hides the others
            parts.append(f"{title}: could not be computed ({type(e).__name__}: {e})")
    return "\n\n".join(parts)
