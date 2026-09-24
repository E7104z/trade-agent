"""
The evidence backlog: work the n<8 list every day (EVIDENCE_BACKLOG_SPEC_v1).

WHY THIS EXISTS
---------------
The directive: "Treat under-n<8 setups as the top backlog priority, not
background noise." Until now the system REPORTED under-powered cells -- to one
reader, the weekly miner, once a week -- and did nothing about them. The
morning analyst, the one loop that chooses what gets traded and therefore what
evidence gets collected, never learned what evidence was missing.

This module computes, every evening, what the desk is short of:

  rule      a quarantined rule short of its forward-observation target
  question  a pre-registered open question short of its threshold
  cell      a mined 1-D cell with 1 <= n < 8 (the n>=8 floor it must reach
            before anything about it can be said)

THE INTEGRITY RULE (enforced by a test on the table's columns): the backlog
stores COUNTS, TARGETS AND RATES ONLY. It never computes a mean, a win rate,
a p-value or an effect sign. A daily table of effect sizes would be an
unregistered look taken every day and fed to the loop that picks trades; a
table of counts is an allocation of collection effort and cannot bias any
test. The backlog says where to look, never what to conclude.
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from typing import Any

__all__ = ["RATE_WINDOW", "TOP_N", "compute", "latest", "morning_block", "daily_digest"]

RATE_WINDOW = 10   # trailing market sessions for the observation rate
TOP_N = 8          # items the morning analyst is shown
CELL_FLOOR = 8     # the project-wide n>=8 floor a cell must reach


def window_start(d: date, sessions: int = RATE_WINDOW) -> date:
    """The earliest date inside the trailing `sessions` market sessions ending at d."""
    from .util import is_market_day
    n, cur = 0, d
    while True:
        if is_market_day(cur):
            n += 1
            if n >= sessions:
                return cur
        cur -= timedelta(days=1)
        if (d - cur).days > sessions * 3 + 10:
            return cur


def _eta(remaining: int, rate: int) -> int | None:
    if remaining <= 0:
        return 0
    if not rate:
        return None
    return int(math.ceil(remaining / (rate / RATE_WINDOW)))


def _pred_text(pred: dict) -> str:
    return " AND ".join(f"{c['dimension']}={c['level']}" for c in pred.get("conditions", []))


def _rule_items(con: Any, since: date) -> list[dict]:
    from .research import COUNTED_SOURCES
    rows = con.execute(
        """SELECT rule_id, lane, predicate_json, forward_n, forward_n_required
           FROM research_rules WHERE state = 'QUARANTINED' AND predicate_json IS NOT NULL"""
    ).fetchall()
    out = []
    marks = ",".join("?" for _ in COUNTED_SOURCES)
    for rid, lane, pred_json, n, target in rows:
        rate = con.execute(
            f"""SELECT COUNT(*) FROM research_observations WHERE rule_id = ? AND session_date >= ?
                AND COALESCE(source, 'taken') IN ({marks})""",
            [rid, since, *COUNTED_SOURCES]).fetchone()[0] or 0
        try:
            pred = json.loads(pred_json)
        except (TypeError, ValueError):
            continue
        n, target = int(n or 0), int(target or 8)
        out.append({"item_type": "rule", "item_id": rid, "lane": lane, "n": n,
                    "target": target, "rate_10s": int(rate),
                    "what_counts": f"a fill-valid {lane} trade in {_pred_text(pred)}"})
    out.sort(key=lambda i: i["target"] - i["n"])
    return out


def _question_items(con: Any, since: date) -> list[dict]:
    from .open_questions import OPEN_QUESTIONS
    totals = dict(con.execute(
        "SELECT question_id, SUM(increment) FROM open_question_progress GROUP BY question_id"
    ).fetchall())
    recent = dict(con.execute(
        """SELECT question_id, SUM(increment) FROM open_question_progress
           WHERE session_date >= ? GROUP BY question_id""", [since]).fetchall())
    out = []
    for q in OPEN_QUESTIONS:
        n, target = int(totals.get(q["id"]) or 0), int(q["threshold"])
        if n >= target:
            continue
        out.append({"item_type": "question", "item_id": f"Q{q['id']}", "lane": None,
                    "n": n, "target": target, "rate_10s": int(recent.get(q["id"]) or 0),
                    "what_counts": q["title"]})
    # Largest remaining fraction first: the most starved question leads.
    out.sort(key=lambda i: -((i["target"] - i["n"]) / max(1, i["target"])))
    return out


def _cell_items(con: Any, since: date) -> list[dict]:
    from . import buckets, research
    out = []
    for lane, rows, date_key in (("morning", research.morning_rows(con), "plan_date"),
                                 ("runner", research.runner_rows(con), "session_date")):
        counts: dict[tuple, list[int]] = {}
        for r in rows:
            recent = 1 if (r.get(date_key) is not None and r.get(date_key) >= since) else 0
            for dim in buckets.dimensions(lane):
                level = buckets.label(lane, dim, r)
                if level == "?":
                    continue
                c = counts.setdefault((dim, level), [0, 0])
                c[0] += 1
                c[1] += recent
        for (dim, level), (n, rate) in counts.items():
            if 1 <= n < CELL_FLOOR:
                out.append({"item_type": "cell", "item_id": f"{lane}:{dim}={level}",
                            "lane": lane, "n": n, "target": CELL_FLOOR, "rate_10s": rate,
                            "what_counts": f"a fill-valid {lane} trade with {dim}={level}"})
    out.sort(key=lambda i: (-i["n"], i["item_id"]))
    return out


def compute(con: Any, d: date) -> list[dict]:
    """Rebuild the backlog snapshot for session `d`. Counts only. Returns it."""
    since = window_start(d)
    items: list[dict] = []
    for part in (_rule_items, _question_items, _cell_items):
        try:
            items.extend(part(con, since))
        except Exception as e:  # noqa: BLE001 -- one source must not cost the others
            from .util import log
            log("backlog", f"WARN {part.__name__} failed: {e}")
    con.execute("DELETE FROM evidence_backlog WHERE session_date = ?", [d])
    for rank, it in enumerate(items, 1):
        it["rank"] = rank
        it["eta_sessions"] = _eta(it["target"] - it["n"], it["rate_10s"])
        it["stalled"] = it["rate_10s"] == 0
        con.execute(
            """INSERT INTO evidence_backlog (session_date, rank, item_type, item_id, lane, n,
                   target, rate_10s, eta_sessions, stalled, what_counts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [d, rank, it["item_type"], it["item_id"], it["lane"], it["n"], it["target"],
             it["rate_10s"], it["eta_sessions"], it["stalled"], it["what_counts"][:500]])
    return items


def latest(con: Any, on_or_before: date | None = None, top: int | None = None) -> list[dict]:
    """The most recent backlog snapshot (optionally the top N ranks)."""
    params: list = []
    where = ""
    if on_or_before is not None:
        where, params = "WHERE session_date <= ?", [on_or_before]
    row = con.execute(f"SELECT MAX(session_date) FROM evidence_backlog {where}", params).fetchone()
    if not row or row[0] is None:
        return []
    sql = ("SELECT rank, item_type, item_id, lane, n, target, rate_10s, eta_sessions, stalled, "
           "what_counts FROM evidence_backlog WHERE session_date = ? ORDER BY rank")
    rows = con.execute(sql + (f" LIMIT {int(top)}" if top else ""), [row[0]]).fetchall()
    keys = ("rank", "item_type", "item_id", "lane", "n", "target", "rate_10s",
            "eta_sessions", "stalled", "what_counts")
    return [dict(zip(keys, r), session_date=row[0]) for r in rows]


def morning_selection(items: list[dict], n: int = TOP_N) -> list[dict]:
    """The items the morning analyst is shown: round-robin across item types,
    each in its own rank order.

    A straight top-N by rank would be all open questions (there are 14, most
    far from threshold) and never a rule or a cell -- the analyst would never
    see the cells the n<8 directive is about. Round-robin keeps every kind of
    missing evidence in view without re-ranking within a kind.
    """
    by_type: dict[str, list[dict]] = {}
    for it in sorted(items, key=lambda i: i.get("rank") or 0):
        by_type.setdefault(it["item_type"], []).append(it)
    order = [t for t in ("rule", "question", "cell") if t in by_type]
    out: list[dict] = []
    while len(out) < n and any(by_type.get(t) for t in order):
        for t in order:
            if by_type.get(t) and len(out) < n:
                out.append(by_type[t].pop(0))
    return out


def morning_block(items: list[dict]) -> str:
    """The DATA-STARVED block for the morning analyst. Counts only, as stored."""
    if not items:
        return ""
    lines = [
        f"## DATA-STARVED (system backlog — top {len(items)})",
        "",
        "Evidence the desk is short of, ranked by the system. These are COUNTS toward a "
        "threshold, not findings: nothing here says any of these cells works. A candidate "
        "that would produce one of these observations may be proposed as a labeled probe "
        "by setting `probe_for` to the item id — it must still be a real setup with a real "
        "catalyst and a real stop, and it faces every kill-check.",
        "",
    ]
    for it in items:
        eta = f", ~{it['eta_sessions']} sessions at current pace" if it.get("eta_sessions") else ""
        stalled = " — STALLED (nothing in 10 sessions)" if it.get("stalled") else ""
        lines.append(f"- `{it['item_id']}` ({it['item_type']}): {it['n']}/{it['target']}{eta}"
                     f"{stalled}. Counts: {it['what_counts']}")
    return "\n".join(lines)


def daily_digest(con: Any, d: date, extra: dict | None = None) -> dict:
    """One row per evening: what was collected and what broke. No effect sizes."""
    from . import ledger
    payload: dict[str, Any] = dict(extra or {})
    try:
        from .health import loop_health
        from .config import load_config
        h = loop_health(con, load_config())
        payload["health"] = {"overall": h.get("overall"),
                             "stages": {s["key"]: s["status"] for s in h.get("stages", [])},
                             "warnings": len(h.get("warnings", []))}
    except Exception:
        pass
    try:
        cov = ledger.coverage_for(con, d)
        payload["coverage"] = [c for c in cov if c["n_total"] and c["n_null"] == c["n_total"]]
    except Exception:
        pass
    try:
        now = {i["item_id"]: i for i in latest(con, d)}
        prev_row = con.execute(
            "SELECT MAX(session_date) FROM evidence_backlog WHERE session_date < ?", [d]).fetchone()
        prev = {i["item_id"]: i for i in latest(con, prev_row[0])} if prev_row and prev_row[0] else {}
        payload["backlog"] = {
            "items": len(now),
            "advanced": sorted(k for k, v in now.items() if k in prev and v["n"] > prev[k]["n"]),
            "newly_stalled": sorted(k for k, v in now.items()
                                    if v["stalled"] and k in prev and not prev[k]["stalled"]),
        }
    except Exception:
        pass
    try:
        payload["grader_rejections"] = dict(con.execute(
            "SELECT kind, COUNT(*) FROM grader_rejections WHERE session_date = ? GROUP BY kind",
            [d]).fetchall())
    except Exception:
        pass
    try:
        pv = ledger.latest_playbook_violations(con)
        payload["playbook_violations"] = {"block": pv.get("block", 0), "rejected": pv.get("rejected", False)}
    except Exception:
        pass
    con.execute("DELETE FROM daily_diagnostics WHERE session_date = ?", [d])
    con.execute("INSERT INTO daily_diagnostics (session_date, payload_json) VALUES (?, ?)",
                [d, json.dumps(payload, default=str)[:8000]])
    return payload


def digest_text(payload: dict) -> str:
    """Plain-text digest for the evening email."""
    lines = ["DAILY DIAGNOSTICS (what was collected and what broke — no effect sizes)"]
    h = payload.get("health") or {}
    if h:
        lines.append(f"- loop health: {h.get('overall')} "
                     + ", ".join(f"{k}={v}" for k, v in (h.get("stages") or {}).items()))
    if "forward_observations" in payload:
        lines.append(f"- forward observations recorded: {payload['forward_observations']}")
    b = payload.get("backlog") or {}
    if b:
        lines.append(f"- evidence backlog: {b.get('items', 0)} items; advanced today: "
                     f"{', '.join(b.get('advanced') or []) or 'none'}; newly stalled: "
                     f"{', '.join(b.get('newly_stalled') or []) or 'none'}")
    if payload.get("coverage"):
        lines.append("- DATA PLUMBING: " + "; ".join(
            f"{c['lane']}:{c['field']} null on all {c['n_total']}" for c in payload["coverage"]))
    if payload.get("grader_rejections"):
        lines.append("- grounding refusals: " + ", ".join(
            f"{k}={v}" for k, v in payload["grader_rejections"].items()))
    pv = payload.get("playbook_violations") or {}
    if pv.get("block") or pv.get("rejected"):
        lines.append(f"- last playbook rewrite: {pv.get('block')} blocking finding(s) repaired"
                     + (" — REWRITE REJECTED" if pv.get("rejected") else ""))
    return "\n".join(lines)
