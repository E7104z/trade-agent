"""
The trader's hypothesis notebook (specs/TRADER_HYPOTHESIS_SPEC_v1.md).

WHY THIS MODULE EXISTS
----------------------
The desk already trades to collect data -- coverage, the daily floor, the
exploration lane, backlog probes -- but the SYSTEM aims every one of those
trades: the taxonomy, the R:R band, the n<8 backlog. The trader had no way to
say "I think I'm seeing X; I'm going to trade it to find out." A belief could
live only as prose in a thesis or a lesson, and a lesson steers the analyst
without ever being tested on R.

A disciplined human trader learns like this, and this module is that loop:

  1. notice something (morning on the tape, or evening in review);
  2. write it down -- the claim, and what would prove it wrong -- BEFORE
     trading it (`register`: statement and refutation are frozen at insert);
  3. commit to the test before looking (the SYSTEM sets target_n, the first
     look, and max_n, the horizon; the LLM never does);
  4. trade it small and on purpose (the `hypothesis` test lane, morning.py);
  5. review each trade honestly (`hypothesis_reviews`: did it express the
     idea? an annotation, never an exclusion);
  6. judge on a pre-registered schedule, not on a whim (`evaluate`, weekly:
     the first look at target_n, then one look a week as tests accrue, all
     spending ONE fixed error budget (agent/sequential.py); cohort BH;
     abandoned ideas at p = 1). An idea is supported when the evidence shows
     it, refuted when the evidence rules out a useful edge, and otherwise
     keeps collecting -- a real idea is no longer killed by one unlucky week;
  7. remember what was tested (the notebook block shows judged ideas, and a
     dead idea cannot come back for GRAVEYARD_SESSIONS);
  8. graduate a survivor only through the existing forward test (a SUPPORTED
     idea with a predicate becomes a QUARANTINED research rule).

INTEGRITY
---------
* Forward by construction: a pick's hypothesis_id is written by the morning
  loop before the outcome exists. Nothing in the evening or weekly loop writes
  it (tests/test_hypotheses.py greps for that).
* The unit is the session: several tagged trades for one idea on one tape are
  one observation (their mean R), the same lesson the runner lane learned
  ("count events, not rows").
* Open ideas are shown as COUNTS only. A running mean shown every morning is an
  unregistered look taken daily; the registered look is the verdict.
* SUPPORTED is not edge. It says the trader's selection within this idea made
  money over n sessions. Only a VALIDATED research rule is edge.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from typing import Any, Iterable

__all__ = [
    "LANE", "NOVEL_SETUP", "enabled", "lane_cfg", "clean_proposal",
    "all_hypotheses", "open_hypotheses", "plan_registrations", "plan_for_morning",
    "commit_morning", "samples", "notebook_md", "analyst_prompt_text",
    "risk_prompt_text", "grader_prompt_text", "grader_block", "record_evening",
    "evening_text", "evaluate", "weekly_md", "weekly_html", "render_text",
]

LANE = "hypothesis"
NOVEL_SETUP = "novel"

# Frozen schedule. These are the research layer's existing constants, not new
# tuning: n >= 8 is the desk-wide floor, 10 sessions is the quarantine time
# floor, 30 market sessions is the decay window.
from .research import DEFAULT_FORWARD_N as TARGET_N  # noqa: E402
from .research import DEFAULT_MIN_QUARANTINE_SESSIONS as MIN_SESSIONS  # noqa: E402

from . import sequential as _seq  # noqa: E402

# The horizon: sessions-with-a-valid-test by which an idea's whole error budget
# is spent (EDGE_ACCELERATION_SPEC_v1 Part 1). It replaced "a second look at
# 2 x target_n" -- two looks at n = 8 and 16 judged a real +0.5R idea right
# about one time in five, and refuted any idea whose first eight sessions
# averaged <= 0, which a real +0.3R idea does about a third of the time.
MAX_N = _seq.N_MAX
EXPIRE_SESSIONS = 30
GRAVEYARD_SESSIONS = 30

# An idea is judged on sessions it was tested in, at one per session, so it
# must be tradeable often. Each number in a statement is, in practice, one
# more condition a trade must meet ("gap >40%", "RVOL 2.49x at 10:00", "5%
# above VWAP"): H-de095870 (2026-10-05) stacked five and was unlikely ever to
# reach its first look. A clock time counts once.
MAX_STATEMENT_NUMBERS = 3
_NUMBER = re.compile(r"\d{1,2}:\d{2}|\d+(?:\.\d+)?")
ALPHA = _seq.ALPHA              # one-sided, across every look an idea gets
DUP_JACCARD = 0.8               # the lesson bank's near-duplicate bar

OPEN, SUPPORTED, REFUTED, ABANDONED, EXPIRED = (
    "OPEN", "SUPPORTED", "REFUTED", "ABANDONED", "EXPIRED")

_DEFAULTS = {"enabled": False, "size_r": 0.25, "max_picks": 2, "max_new_per_day": 1,
             "max_open": 6, "min_rr": 1.0,
             "blocking_kills": ["grounding", "entry_type", "event_risk", "hypothesis_fit"]}

_COLS = ("hyp_id", "created_date", "opened_at", "source", "from_lesson_id", "statement",
         "refutation", "origin", "belief", "direction", "setup_type", "predicate_json",
         "target_n", "max_n", "min_sessions", "state", "looks_done", "last_tagged_at",
         "closed_at", "close_reason", "verdict_json", "cohort_counted", "rule_id", "note",
         "alpha_spent", "test_design")


def _log(msg: str) -> None:
    try:
        from .util import log
        log("hypotheses", msg)
    except Exception:  # noqa: BLE001
        pass


def lane_cfg(cfg: Any) -> dict:
    raw = {}
    try:
        raw = cfg.get("hypothesis_lane", {}) or {}
    except Exception:  # noqa: BLE001
        raw = {}
    return {**_DEFAULTS, **raw}


def enabled(cfg: Any) -> bool:
    return bool(lane_cfg(cfg).get("enabled"))


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def _jaccard(a: str, b: str) -> float:
    ta, tb = set(_norm(a).split()), set(_norm(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _text(v: Any, limit: int) -> str:
    return " ".join(str(v or "").split())[:limit]


def _as_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, date) and not hasattr(v, "hour"):
        return v
    if hasattr(v, "date"):
        return v.date()
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _sessions_between(start: Any, end: date) -> int:
    from .research import market_sessions_between
    s = _as_date(start)
    return market_sessions_between(s, end) if s else 0


def next_session(d: date) -> date:
    from .util import is_market_day
    nxt = d + timedelta(days=1)
    for _ in range(10):
        if is_market_day(nxt):
            return nxt
        nxt += timedelta(days=1)
    return nxt


def hyp_id_for(statement: str, d: date) -> str:
    key = f"{_norm(statement)}|{d.isoformat()}"
    return "H-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


# --------------------------------------------------------------------------
# Proposals
# --------------------------------------------------------------------------

def clean_proposal(raw: Any) -> tuple[dict | None, str]:
    """A proposal the register can accept, or (None, why not).

    The test is always the same claim -- trading this idea, as tagged, makes
    money in R -- so the statement must be a repeatable trade idea and the
    refutation must exist. A predicate is optional; an invalid one is dropped
    (the idea still registers) because a hunch need not fit the mined cells.
    """
    if not isinstance(raw, dict):
        return None, "not an object"
    statement = _text(raw.get("statement"), 400)
    refutation = _text(raw.get("refutation"), 300)
    if len(statement) < 15:
        return None, "statement missing or too short to be a repeatable trade idea"
    if len(refutation) < 10:
        return None, "no refutation: a hypothesis must say what would prove it wrong"
    n_cond = len(set(_NUMBER.findall(statement)))
    if n_cond > MAX_STATEMENT_NUMBERS:
        return None, (f"names {n_cond} numeric conditions: an idea that needs that many things "
                      f"at once fits too few trades to reach a verdict (state its core "
                      f"condition, {MAX_STATEMENT_NUMBERS} numbers at most)")
    direction = str(raw.get("direction") or "").strip().lower()
    direction = direction if direction in ("long", "short") else None
    setup = str(raw.get("setup_type") or "").strip().lower()[:40] or None
    if setup in ("null", "none"):
        setup = None
    belief = raw.get("belief")
    belief = int(belief) if isinstance(belief, (int, float)) and 1 <= int(belief) <= 5 else None
    pred, note = None, ""
    if raw.get("predicate"):
        from .grounding import validate_lesson_predicate
        pred = validate_lesson_predicate(raw.get("predicate"))
        if pred is not None and pred.get("lane") != "morning":
            pred = None
        if pred is None:
            note = "predicate dropped (not a 1-2 condition morning-lane cell)"
    return {"statement": statement, "refutation": refutation,
            "origin": _text(raw.get("origin"), 300) or None,
            "belief": belief, "direction": direction, "setup_type": setup,
            "predicate": pred, "note": note}, ""


# --------------------------------------------------------------------------
# Register (reads)
# --------------------------------------------------------------------------

def all_hypotheses(con: Any) -> list[dict]:
    try:
        rows = con.execute(
            f"SELECT {', '.join(_COLS)} FROM trader_hypotheses "
            "ORDER BY opened_at, hyp_id").fetchall()
    except Exception:  # noqa: BLE001 -- table absent on a pre-v31 ledger
        return []
    out = []
    for r in rows:
        row = dict(zip(_COLS, r))
        row["predicate"] = json.loads(row["predicate_json"]) if row.get("predicate_json") else None
        row["verdict"] = json.loads(row["verdict_json"]) if row.get("verdict_json") else {}
        out.append(row)
    return out


def open_hypotheses(con: Any) -> list[dict]:
    return [r for r in all_hypotheses(con) if r["state"] == OPEN]


def samples(con: Any) -> dict[str, dict]:
    """Per hypothesis: its tagged picks, and the session-unit sample.

    A unit is one session's mean R over the idea's fill-valid, triggered
    tagged picks dated on or after the idea opened. Untriggered and invalid
    picks are counted separately, never in the sample.
    """
    try:
        rows = con.execute(
            """SELECT p.pick_id, p.plan_date, p.hypothesis_id, p.ticker, p.exploration_lane,
                      o.triggered, o.r_planned, o.r_multiple, o.exit_reason, o.execution_anomaly,
                      o.pick_id
               FROM picks p LEFT JOIN outcomes o ON o.pick_id = p.pick_id
               WHERE p.hypothesis_id IS NOT NULL
               ORDER BY p.plan_date, p.pick_id""").fetchall()
    except Exception:  # noqa: BLE001
        return {}
    opened = {h["hyp_id"]: _as_date(h["opened_at"]) for h in all_hypotheses(con)}
    out: dict[str, dict] = {}
    for (pid, pd, hid, ticker, lane, trig, r_pl, r_mu, exit_reason, anomaly, graded) in rows:
        pd = _as_date(pd)
        if hid not in opened or (opened[hid] and pd and pd < opened[hid]):
            continue
        box = out.setdefault(hid, {"tagged": 0, "pending": 0, "untriggered": 0, "invalid": 0,
                                   "trades": [], "by_session": {}})
        box["tagged"] += 1
        r = r_pl if r_pl is not None else r_mu
        status = "valid"
        if graded is None:
            status = "pending"
            box["pending"] += 1
        elif not trig:
            status = "untriggered"
            box["untriggered"] += 1
        elif exit_reason == "invalid_fill" or anomaly or r is None:
            status = "invalid"
            box["invalid"] += 1
        else:
            box["by_session"].setdefault(pd, []).append(float(r))
        box["trades"].append({"pick_id": pid, "plan_date": pd, "ticker": ticker,
                              "lane": lane, "status": status,
                              "r": float(r) if r is not None and status == "valid" else None})
    for box in out.values():
        units = [sum(v) / len(v) for _, v in sorted(box["by_session"].items())]
        box["units"] = units
        box["n"] = len(units)
    return out


def _stats(units: list[float]) -> dict:
    from .stats import one_sample_t
    n = len(units)
    mean = sum(units) / n if n else None
    std = None
    if n >= 2:
        var = sum((u - mean) ** 2 for u in units) / (n - 1)
        std = max(0.0, var) ** 0.5
    t, df, p = one_sample_t(mean, std, n)
    return {"n": n, "mean_r": round(mean, 4) if mean is not None else None,
            "std_r": round(std, 4) if std is not None else None, "t": t, "p": p}


# --------------------------------------------------------------------------
# Register (writes) -- planned read-only, committed by the morning/evening loop
# --------------------------------------------------------------------------

def plan_registrations(con: Any, d: date, proposals: Iterable[Any], cfg: Any,
                       existing: list[dict] | None = None) -> list[dict]:
    """Decide, without writing, what each proposal becomes.

    Returns one entry per proposal, keyed "new:<index>":
      create    a new OPEN idea (hyp_id assigned now)
      existing  the same idea is already OPEN (or was created earlier in the
                batch): the tag resolves to that id
      reject    with the reason (graveyard, a full notebook, the daily cap,
                a malformed proposal)
    """
    lane = lane_cfg(cfg)
    max_new = max(0, int(lane.get("max_new_per_day", 1)))
    max_open = max(0, int(lane.get("max_open", 6)))
    rows = existing if existing is not None else all_hypotheses(con)
    open_rows = [r for r in rows if r["state"] == OPEN]
    n_open = len(open_rows)
    created: list[dict] = []
    out: list[dict] = []
    for k, raw in enumerate(list(proposals or [])):
        key = f"new:{k}"
        prop, why = clean_proposal(raw)
        if prop is None:
            out.append({"key": key, "action": "reject", "reason": why})
            continue
        twin = next((r for r in open_rows + created
                     if _jaccard(r["statement"], prop["statement"]) >= DUP_JACCARD), None)
        if twin is not None:
            out.append({"key": key, "action": "existing", "hyp_id": twin["hyp_id"],
                        "reason": f"same idea as open {twin['hyp_id']}"})
            continue
        dead = next((r for r in reversed(rows)   # the most recent closed twin decides
                     if r["state"] in (REFUTED, EXPIRED, ABANDONED, SUPPORTED)
                     and _jaccard(r["statement"], prop["statement"]) >= DUP_JACCARD), None)
        if dead is not None and dead["state"] == SUPPORTED:
            out.append({"key": key, "action": "reject",
                        "reason": f"already SUPPORTED as {dead['hyp_id']}"
                                  + (f" (rule {dead['rule_id']})" if dead.get("rule_id") else "")})
            continue
        if dead is not None and _sessions_between(dead.get("closed_at"), d) < GRAVEYARD_SESSIONS:
            out.append({"key": key, "action": "reject",
                        "reason": f"{dead['hyp_id']} was {dead['state']} "
                                  f"{_sessions_between(dead.get('closed_at'), d)} session(s) ago; "
                                  f"a closed idea returns only after {GRAVEYARD_SESSIONS}"})
            continue
        if len(created) >= max_new:
            out.append({"key": key, "action": "reject", "reason": f"daily cap of {max_new} new idea(s)"})
            continue
        if n_open >= max_open:
            out.append({"key": key, "action": "reject",
                        "reason": f"notebook full ({max_open} open ideas): close or finish one first"})
            continue
        row = {**prop, "hyp_id": hyp_id_for(prop["statement"], d)}
        created.append(row)
        n_open += 1
        out.append({"key": key, "action": "create", "hyp_id": row["hyp_id"], "row": row})
    return out


def _insert(con: Any, row: dict, created: date, opened: date, source: str,
            from_lesson_id: int | None = None) -> bool:
    if con.execute("SELECT 1 FROM trader_hypotheses WHERE hyp_id = ?",
                   [row["hyp_id"]]).fetchone():
        return False
    con.execute(
        f"""INSERT INTO trader_hypotheses ({', '.join(_COLS)})
            VALUES ({', '.join('?' for _ in _COLS)})""",
        [row["hyp_id"], created, opened, source, from_lesson_id, row["statement"],
         row["refutation"], row.get("origin"), row.get("belief"), row.get("direction"),
         row.get("setup_type"), json.dumps(row["predicate"]) if row.get("predicate") else None,
         TARGET_N, MAX_N, MIN_SESSIONS, OPEN, 0, None, None, None, None, None, None,
         row.get("note") or None, 0.0, _seq.DESIGN])
    return True


def plan_for_morning(con: Any, d: date, candidates: dict, cfg: Any,
                     existing: list[dict] | None = None) -> dict:
    """Everything the morning lane needs, decided without writing.

    `tags` maps every candidate tag that resolves (an open id, or "new:k") to
    the hyp_id it tests; `priority` puts unfinished ideas first, closest to a
    verdict first, then new ones. Used by both paired arms; only arm A commits.
    """
    rows = existing if existing is not None else all_hypotheses(con)
    open_ids = {r["hyp_id"] for r in rows if r["state"] == OPEN}
    regs = plan_registrations(con, d, (candidates or {}).get("new_hypotheses") or [], cfg,
                              existing=rows)
    tags: dict[str, str] = {hid: hid for hid in open_ids}
    for reg in regs:
        if reg["action"] in ("create", "existing"):
            tags[reg["key"]] = reg["hyp_id"]
    sample = samples(con) if open_ids else {}
    priority = {hid: (sample.get(hid) or {}).get("n", 0) for hid in open_ids}
    priority = {hid: max(0, TARGET_N - n) for hid, n in priority.items()}
    for reg in regs:
        if reg["action"] == "create":
            priority[reg["hyp_id"]] = TARGET_N + 1
    abandon = []
    for a in (candidates or {}).get("abandon") or []:
        if isinstance(a, dict) and str(a.get("hyp_id") or "") in open_ids:
            abandon.append({"hyp_id": str(a["hyp_id"]), "why": _text(a.get("why"), 200) or "abandoned"})
    return {"tags": tags, "registrations": regs, "abandon": abandon, "priority": priority}


def commit_morning(con: Any, d: date, hyp_plan: dict, plan: dict) -> dict:
    """Arm A only, before the plan is saved: write the morning's new ideas,
    the abandonments, and when each tagged idea was last tested. Never touches
    a frozen column."""
    out = {"created": [], "rejected": [], "abandoned": [], "tested": []}
    for reg in hyp_plan.get("registrations") or []:
        if reg["action"] == "create" and _insert(con, reg["row"], d, d, "analyst"):
            out["created"].append(reg["hyp_id"])
            out.setdefault("opened", []).append(
                {"hyp_id": reg["hyp_id"], "statement": reg["row"]["statement"],
                 "refutation": reg["row"]["refutation"], "belief": reg["row"].get("belief")})
            _log(f"opened {reg['hyp_id']}: {reg['row']['statement'][:100]}")
        elif reg["action"] == "reject":
            out["rejected"].append(reg.get("reason"))
            _log(f"proposal {reg['key']} not registered: {reg.get('reason')}")
    tested = sorted({p.get("hypothesis_id") for p in plan.get("picks") or []
                     if p.get("hypothesis_id")})
    for a in hyp_plan.get("abandon") or []:
        if a["hyp_id"] in tested:
            continue   # tested today: an idea cannot be traded and dropped in one breath
        con.execute(
            """UPDATE trader_hypotheses SET state = ?, closed_at = ?, close_reason = ?,
                      cohort_counted = FALSE
               WHERE hyp_id = ? AND state = ?""",
            [ABANDONED, d, a["why"], a["hyp_id"], OPEN])
        out["abandoned"].append(a["hyp_id"])
    for hid in tested:
        con.execute("UPDATE trader_hypotheses SET last_tagged_at = ? WHERE hyp_id = ?", [d, hid])
    out["tested"] = tested
    return out


# --------------------------------------------------------------------------
# The morning notebook
# --------------------------------------------------------------------------

def _gut_md(rows: list[dict]) -> str:
    judged = [r for r in rows if r["state"] in (SUPPORTED, REFUTED) and r.get("belief")]
    if not judged:
        return ""
    hi = [r for r in judged if r["belief"] >= 4]
    lo = [r for r in judged if r["belief"] <= 3]

    def tally(group):
        s = sum(1 for r in group if r["state"] == SUPPORTED)
        return f"{s} supported, {len(group) - s} refuted"
    return (f"Your gut so far (belief when written vs verdict): strong hunches (4-5): {tally(hi)}; "
            f"weaker ones (1-3): {tally(lo)}.")


def notebook_md(con: Any, d: date, cfg: Any) -> tuple[str, list[str]]:
    """The morning block and the open ids. Counts only for open ideas; the
    registered verdict (with its numbers) for judged ones."""
    rows = all_hypotheses(con)
    lane = lane_cfg(cfg)
    open_rows = [r for r in rows if r["state"] == OPEN]
    sample = samples(con) if rows else {}
    lines = [f"## YOUR HYPOTHESIS NOTEBOOK ({len(open_rows)} of {lane['max_open']} open)", ""]
    if not open_rows:
        lines.append("(no open ideas: nothing you are testing right now)")
    for r in open_rows:
        s = sample.get(r["hyp_id"]) or {}
        sessions = _sessions_between(r["opened_at"], d)
        last = _as_date(r.get("last_tagged_at"))
        idle = _sessions_between(last or r["opened_at"], d)
        stall = (f" UNTESTED for {idle} sessions; it expires at {EXPIRE_SESSIONS}."
                 if idle >= MIN_SESSIONS else "")
        looks = int(r.get("looks_done") or 0)
        look = (f"looked at {looks} time(s), not yet shown either way, so it keeps collecting"
                if looks else
                f"first judged at {r['target_n']}, then each week as tests accrue")
        belief = f"belief {r['belief']}/5, " if r.get("belief") else ""
        lines += [
            f"- `{r['hyp_id']}`: {r['statement']}",
            f"  ({belief}opened {r['opened_at']} by the {r['source']})",
            f"  Wrong if: {r['refutation']}",
        ]
        if r.get("origin"):
            lines.append(f"  Where it came from: {r['origin']}")
        lines.append(
            f"  Progress: {s.get('n', 0)}/{r['target_n']} sessions with a valid test, over {sessions} "
            f"session(s) open ({s.get('tagged', 0)} tagged trade(s), {s.get('untriggered', 0)} "
            f"untriggered); {look}. Last tested: {last or 'never'}.{stall}")
    closed = sorted([r for r in rows if r["state"] != OPEN],
                    key=lambda r: str(r.get("closed_at") or ""), reverse=True)[:10]
    if closed:
        lines += ["", "Judged and closed (most recent first; these are the registered verdicts):"]
        for r in closed:
            v = r.get("verdict") or {}
            fin = v.get("final") or {}
            num = (f" n={fin.get('n')}, mean {fin.get('mean_r'):+.2f}R, p={fin.get('p')}"
                   if fin.get("mean_r") is not None else "")
            why = f" ({r['close_reason']})" if r.get("close_reason") else ""
            lines.append(f"- `{r['hyp_id']}` {r['state']} {r.get('closed_at')}:{num}{why}: "
                         f"{r['statement'][:160]}")
    gut = _gut_md(rows)
    if gut:
        lines += ["", gut]
    return "\n".join(lines), [r["hyp_id"] for r in open_rows]


# --------------------------------------------------------------------------
# Prompt text (empty when the lane is off, so every prompt renders exactly as
# it did before this module existed)
# --------------------------------------------------------------------------

_OFF_ANALYST = {"HYPOTHESIS_TAXONOMY": "", "HYPOTHESIS_SECTION": "",
                "HYPOTHESIS_FIELD": "", "HYPOTHESIS_TOPLEVEL": ""}
_OFF_RISK = {"HYPOTHESIS_KILL": "", "HYPOTHESIS_RISK": ""}
_OFF_GRADER = {"HYPOTHESIS_GRADER": "", "HYPOTHESIS_REVIEW_FIELD": "",
               "HYPOTHESIS_GRADER_FIELD": ""}


def analyst_prompt_text(cfg: Any) -> dict[str, str]:
    if not enabled(cfg):
        return dict(_OFF_ANALYST)
    lane = lane_cfg(cfg)
    return {
        "HYPOTHESIS_TAXONOMY": (
            " Testing one of your own hypotheses on a trade none of these describes: `novel`,"
            " allowed only with a `hypothesis_id`."),
        "HYPOTHESIS_SECTION": (
            "\n\n# YOUR HYPOTHESES (how you get better, the way a real trader does)\n\n"
            "You notice things: a tape that keeps doing the same thing, a pattern your lessons"
            " hint at, an anecdote from a review, a hunch you cannot shake. A good trader neither"
            " argues with a hunch nor bets on it. They write it down, say what would prove it"
            " wrong, trade it small and on purpose, and let a committed sample decide. That is"
            " how you get better, and it is part of your job every morning.\n\n"
            "- Your HYPOTHESIS NOTEBOOK (in your memory below) lists the ideas you are testing,"
            " how many sessions each has, and the ideas already judged. Read it before you hunt.\n"
            "- To test an open idea today, put its id in a candidate's `hypothesis_id`. The trade"
            " must actually express the idea, and it must still be a real, grounded, placeable"
            " trade with a real stop.\n"
            f"- To start a new idea, write it in `new_hypotheses` (at most {lane['max_new_per_day']}"
            " today) and point a candidate at it with `\"hypothesis_id\": \"new:0\"` (its index in"
            " that list). You may also write an idea down without trading it today. Write a"
            " repeatable trade claim (\"fading a gap_go that loses premarket VWAP in the first"
            " half hour makes money on a risk-off tape\"), not a mood (\"the market feels"
            " heavy\"). Say where it came from in `origin`, and how strongly you believe it in"
            " `belief` (1-5): the desk tracks whether your strong hunches beat your weak ones.\n"
            "- Keep an idea testable: one core condition, two at most, beyond its setup. An"
            " idea is judged only on sessions it was traded, and every extra condition is fewer"
            " trades that fit it; the register refuses a statement with more than"
            f" {MAX_STATEMENT_NUMBERS} numbers in it. An idea your notebook shows UNTESTED for"
            " many sessions is too narrow: abandon it, or restate its core as a new idea.\n"
            f"- Beyond the one candidate per setup, you may bring up to {lane['max_picks']} extra"
            " candidates that exist only to test a hypothesis. They may repeat a setup.\n"
            f"- A tagged candidate the risk manager rejects still trades at {lane['size_r']}R as a"
            " hypothesis test, unless the kill was grounding, entry type, event risk, or"
            " `hypothesis_fit` (the trade does not test the idea). A tagged candidate the desk"
            " approves counts for the idea at its approved size.\n"
            "- One observation per idea per session: several tagged names on one tape are one"
            " observation, not several. Spread an idea across days, not across tickers.\n"
            f"- The system first judges each idea after {TARGET_N} sessions with a valid test,"
            " then again each week as tests accrue, spending one fixed error budget across"
            " those looks, with every idea judged that week corrected together. An idea is"
            " supported when the evidence shows it, refuted when the evidence rules out an"
            " edge worth trading, and otherwise keeps collecting: one unlucky week does not"
            " kill a real idea, and one lucky week does not crown a false one. You never see a"
            " running average before the verdict, on purpose: peeking is how noise becomes a"
            " belief.\n"
            "- A hypothesis never raises conviction, never sizes a trade and never ranks a"
            " candidate. An idea that survives becomes a quarantined rule and must pass a"
            " forward test like any other. A refuted idea cannot be re-proposed for"
            f" {GRAVEYARD_SESSIONS} sessions.\n"
            "- If an idea no longer makes sense, drop it in `abandon` with a reason. That is"
            " honest and allowed. It still counts as a failed test in that week's correction,"
            " so it cannot be used to hide a loser."),
        "HYPOTHESIS_FIELD": (
            ",\n      \"hypothesis_id\": \"optional: an open idea's id (H-...) or new:N for one in"
            " new_hypotheses, else omit\""),
        "HYPOTHESIS_TOPLEVEL": (
            ",\n  \"new_hypotheses\": [\n"
            "    {\"statement\": \"a repeatable trade claim\", \"refutation\": \"what result would"
            " prove it wrong\", \"origin\": \"what you saw that made you think this\","
            " \"belief\": 3, \"direction\": \"long | short | null\", \"setup_type\": \"taxonomy"
            " value, novel, or null\", \"predicate\": {\"lane\": \"morning\", \"conditions\":"
            " [{\"dimension\": \"setup_type\", \"level\": \"gap_fade\"}]}}\n"
            "  ],\n"
            "  \"abandon\": [{\"hyp_id\": \"H-...\", \"why\": \"one line\"}]"),
    }


def risk_prompt_text(cfg: Any) -> dict[str, str]:
    if not enabled(cfg):
        return dict(_OFF_RISK)
    lane = lane_cfg(cfg)
    return {
        "HYPOTHESIS_KILL": " | hypothesis_fit",
        "HYPOTHESIS_RISK": (
            "\n\n**Hypothesis tests.** A candidate carrying `hypothesis_id` is the analyst testing"
            " one of their own written-down ideas (the HYPOTHESIS NOTEBOOK in the memory below,"
            " or `new_hypotheses` in the analyst's output). Attack the test as well as the trade."
            " If the trade does not express the idea it is tagged with (wrong direction, the"
            " condition the idea is about is absent, a different setup wearing the label),"
            " reject it with `primary_kill_check: \"hypothesis_fit\"`, and it stays off the tape."
            " A tagged candidate you reject on any other check except grounding, entry type or"
            f" event risk still trades at {lane['size_r']}R as a hypothesis test, so your kill"
            " decides size and label, not whether the idea gets tested. Never approve one"
            " because it is a test, and never kill one for being one."),
    }


def grader_prompt_text(cfg: Any) -> dict[str, str]:
    if not enabled(cfg):
        return dict(_OFF_GRADER)
    lane = lane_cfg(cfg)
    return {
        "HYPOTHESIS_GRADER": (
            "\n\n## Hypotheses (the trader's notebook)\n\n"
            "Some picks were tagged in the morning, before the outcome, as tests of an idea the"
            " trader wrote down (HYPOTHESIS TESTS TODAY in your input). For each one add"
            " `hypothesis_review`: `expressed: true` if the trade, as it actually traded, was a"
            " fair test of the idea, `false` if it was not (the condition the idea is about never"
            " showed up, or it traded something else). It is a note for the trader and it is"
            " shown beside the verdict. It does not remove the trade from the count, because the"
            " system counts every fill-valid tagged trade, so do not use it to rescue or bury an"
            " idea.\n\n"
            f"You may also start up to {lane['max_new_per_day']} new idea(s) in `hypotheses`."
            " This is where a single ordinary event goes: it is not a lesson, but it can be an"
            " idea worth testing. Write a repeatable trade claim with a refutation, and copy"
            " every number from your input (numbers are checked). Keep it testable: one core"
            " condition, two at most (the register refuses a statement with more than"
            f" {MAX_STATEMENT_NUMBERS} numbers), and a mechanism the computed numbers agree with:"
            " an idea built on a misread number tests the wrong thing for weeks. Or nominate an"
            " existing lesson"
            " for a deliberate test with `from_lesson_id` and a refutation, because an untested"
            " lesson should not steer the desk forever. Do not re-propose an idea already open"
            " in your input. New ideas are tested from the next session on."),
        "HYPOTHESIS_REVIEW_FIELD": (
            ",\n      \"hypothesis_review\": {\"expressed\": true, \"note\": \"only for a pick tagged"
            " with a hypothesis: did the trade, as it actually traded, test the idea? One"
            " line.\"}"),
        "HYPOTHESIS_GRADER_FIELD": (
            ",\n  \"hypotheses\": [\n"
            "    {\"statement\": \"a repeatable trade claim\", \"refutation\": \"what would prove it"
            " wrong\", \"origin\": \"what today showed, numbers copied from the input\","
            " \"belief\": 2, \"direction\": \"long | short | null\", \"setup_type\": \"taxonomy"
            " value or null\"},\n"
            "    {\"from_lesson_id\": 7, \"refutation\": \"what would prove the lesson wrong\"}\n"
            "  ]"),
    }


# --------------------------------------------------------------------------
# Evening
# --------------------------------------------------------------------------

def grader_block(con: Any, picks: list[dict]) -> str:
    rows = {r["hyp_id"]: r for r in all_hypotheses(con)}
    tagged = [p for p in picks if p.get("hypothesis_id") in rows]
    open_rows = [r for r in rows.values() if r["state"] == OPEN]
    if not tagged and not open_rows:
        return ""
    lines = ["## HYPOTHESIS TESTS TODAY (tagged in the morning, before the outcome)", ""]
    if not tagged:
        lines.append("(no pick was tagged as a hypothesis test today)")
    for p in tagged:
        h = rows[p["hypothesis_id"]]
        lines.append(f"- pick_id={p['pick_id']} {p['ticker']} {p.get('direction')} tests "
                     f"`{h['hyp_id']}`: {h['statement']} | wrong if: {h['refutation']}")
    if open_rows:
        lines += ["", "## OPEN HYPOTHESES (already being tested: do not re-propose these)", ""]
        lines += [f"- `{r['hyp_id']}`: {r['statement']}" for r in open_rows]
    return "\n".join(lines)


def record_evening(con: Any, cfg: Any, d: date, review: dict, picks: list[dict],
                   corpus: str) -> dict:
    """Store the grader's per-trade notes and register its ideas.

    Reviews are replaced per session (an evening retry must not double them).
    A grader idea opens on the NEXT session: it is tested only on trades
    tagged after the review that produced it.
    """
    from .grounding import ungrounded_tokens
    out = {"reviews": 0, "created": [], "rejected": []}
    tagged = {p["pick_id"]: p.get("hypothesis_id") for p in picks if p.get("hypothesis_id")}
    con.execute("DELETE FROM hypothesis_reviews WHERE session_date = ?", [d])
    for r in review.get("pick_reviews") or []:
        hr = r.get("hypothesis_review")
        try:
            pid = int(r.get("pick_id"))
        except (TypeError, ValueError):
            continue
        if pid not in tagged or not isinstance(hr, dict):
            continue
        expressed = hr.get("expressed")
        con.execute(
            "INSERT INTO hypothesis_reviews (session_date, pick_id, hyp_id, expressed, note) "
            "VALUES (?,?,?,?,?)",
            [d, pid, tagged[pid], bool(expressed) if isinstance(expressed, bool) else None,
             _text(hr.get("note"), 300) or None])
        out["reviews"] += 1

    proposals: list[dict] = []
    for raw in review.get("hypotheses") or []:
        if not isinstance(raw, dict):
            continue
        lid = raw.get("from_lesson_id")
        if lid is not None:
            try:
                row = con.execute("SELECT lesson, evidence, setup_type FROM lessons WHERE lesson_id = ?",
                                  [int(lid)]).fetchone()
            except (TypeError, ValueError):
                row = None
            if not row:
                out["rejected"].append(f"from_lesson_id {lid}: no such lesson")
                continue
            raw = {"statement": row[0], "setup_type": row[2], "belief": raw.get("belief"),
                   "direction": raw.get("direction"),
                   "refutation": raw.get("refutation") or
                   "trading it as tagged does not make money over the committed sample",
                   "origin": f"lesson L{int(lid)}: {_text(row[1], 200)}",
                   "_lesson_id": int(lid)}
        else:
            bad = ungrounded_tokens(" ".join(str(raw.get(k) or "") for k in
                                             ("statement", "refutation", "origin")), corpus)
            if bad:
                out["rejected"].append(f"ungrounded number(s) {bad[:3]}")
                continue
        proposals.append(raw)
    opened = next_session(d)
    for reg, raw in zip(plan_registrations(con, d, proposals, cfg), proposals):
        if reg["action"] == "create":
            if _insert(con, reg["row"], d, opened, "lesson" if raw.get("_lesson_id") else "grader",
                       from_lesson_id=raw.get("_lesson_id")):
                out["created"].append(reg["hyp_id"])
                _log(f"opened {reg['hyp_id']} (grader, from {opened}): "
                     f"{reg['row']['statement'][:100]}")
        else:
            out["rejected"].append(reg.get("reason"))
    return out


def evening_text(con: Any, d: date, summary: dict | None = None) -> str:
    rows = all_hypotheses(con)
    if not rows:
        return ""
    sample = samples(con)
    open_rows = [r for r in rows if r["state"] == OPEN]
    today = [(hid, s) for hid, s in sample.items()
             if any(t["plan_date"] == d for t in s["trades"])]
    lines = ["HYPOTHESES (the trader's notebook: counts only until judged)"]
    for hid, s in today:
        names = ", ".join(f"{t['ticker']}" + (f" [{t['lane']}]" if t["lane"] else "")
                          for t in s["trades"] if t["plan_date"] == d)
        lines.append(f"- tested today: {hid} via {names}; {s['n']}/{TARGET_N} sessions with a valid test")
    for hid in (summary or {}).get("created") or []:
        h = next((r for r in rows if r["hyp_id"] == hid), None)
        if h:
            lines.append(f"- new idea from the review: {hid}: {h['statement'][:160]}")
    lines.append(f"- open ideas: {len(open_rows)}"
                 + ("; " + ", ".join(f"{r['hyp_id']} {(sample.get(r['hyp_id']) or {}).get('n', 0)}"
                                     f"/{r['target_n']}" for r in open_rows) if open_rows else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Weekly: verdicts, applied in code
# --------------------------------------------------------------------------

def _register_rule(con: Any, h: dict, stats: dict, q: float | None, d: date) -> str | None:
    from . import research
    canon = h.get("predicate")
    if not canon:
        return None
    rid = research.rule_id_for_predicate(canon)
    if con.execute("SELECT 1 FROM research_rules WHERE rule_id = ?", [rid]).fetchone():
        return rid
    dims = [c["dimension"] for c in canon["conditions"]]
    con.execute(
        """INSERT INTO research_rules
           (rule_id, statement, dimensions, n_dimensions, state, source,
            discovered_at, discovery_n, discovery_effect, discovery_q,
            quarantined_at, validated_at, retired_at, forward_n_required,
            forward_n, forward_sum_r, forward_sumsq_r, note, predicate_json, lane,
            decay_count, last_observed_at)
           VALUES (?,?,?,?,'DISCOVERED','trader',?,?,?,?,NULL,NULL,NULL,?,0,0.0,0.0,?,?,?,0,NULL)""",
        [rid, h["statement"][:1000], json.dumps(dims), len(dims), d, int(stats["n"]),
         stats["mean_r"], q, research.DEFAULT_FORWARD_N,
         f"from trader hypothesis {h['hyp_id']}: the trader's selection within the idea, "
         f"n={stats['n']} sessions, p={stats['p']}; the cell itself is untested until forward",
         json.dumps(canon), canon["lane"]])
    research.advance_quarantine(con, rid, d)
    return rid


def _horizon(h: dict) -> int:
    """The spending horizon an idea is judged over. An idea registered before
    the sequential design (max_n = 16, no look taken yet) is judged over the
    current horizon: changing the schedule before the first look is
    data-independent, so it moves no goalpost."""
    return max(int(h.get("max_n") or 0), MAX_N)


def _look(h: dict, st: dict) -> dict:
    """This run's look at one idea (agent/sequential.py). Every idea claims
    the same thing -- trading it, as tagged, makes money -- so the test is
    always one-sided, positive."""
    return _seq.look(st["n"], st["mean_r"], st["std_r"], 1, h.get("alpha_spent"),
                     n_max=_horizon(h))


def evaluate(con: Any, d: date, apply: bool = True) -> dict:
    """Judge every OPEN idea that is due a look; expire the neglected ones.

    An idea is first looked at once it has target_n sessions with a valid
    test and has been open min_sessions market sessions; after that, at every
    weekly run that has a new session for it. Each look spends its share of
    one one-sided budget (agent/sequential.py). Every look taken this run,
    plus every ABANDONED/EXPIRED idea not yet counted, is one BH family (an
    abandoned idea at p = 1): dropping a loser can never shrink the family a
    survivor is judged in.

      SUPPORTED  significant after cohort BH
      REFUTED    the evidence rules out a +MIN_USEFUL_R edge (futility), or
                 the horizon is reached without the idea being shown
      OPEN       anything else: it keeps collecting
    """
    rows = all_hypotheses(con)
    sample = samples(con)
    res = {"supported": [], "refuted": [], "continuing": [], "expired": [], "waiting": [],
           "cohort": 0, "counted_closed": 0}
    due: list[tuple[dict, dict, dict]] = []
    for h in rows:
        if h["state"] != OPEN:
            continue
        s = sample.get(h["hyp_id"]) or {"units": [], "n": 0}
        last = _as_date(h.get("last_tagged_at")) or _as_date(h["opened_at"])
        if _sessions_between(last, d) >= EXPIRE_SESSIONS:
            res["expired"].append(h)
            if apply:
                con.execute(
                    """UPDATE trader_hypotheses SET state = ?, closed_at = ?, close_reason = ?,
                              cohort_counted = FALSE WHERE hyp_id = ? AND state = ?""",
                    [EXPIRED, d, f"untested for {EXPIRE_SESSIONS} sessions", h["hyp_id"], OPEN])
            h["state"], h["cohort_counted"] = EXPIRED, False
            continue
        n = s["n"]
        if (_sessions_between(h["opened_at"], d) < int(h.get("min_sessions") or MIN_SESSIONS)
                or n < int(h["target_n"])):
            h["_why"] = f"{n}/{h['target_n']} sessions before the first look"
            res["waiting"].append(h)
            continue
        stats = _stats(s["units"])
        lk = _look(h, stats)
        if lk["status"] != "looked":
            h["_why"] = ("no new session since the last look"
                         if lk["status"] == "no_new_information"
                         else "sample not testable (zero variance)")
            res["waiting"].append(h)
            continue
        due.append((h, lk, stats))

    closed = [h for h in rows if h["state"] in (ABANDONED, EXPIRED) and not h.get("cohort_counted")]
    if not due:
        return res
    pvals = [lk["p_adj"] for _, lk, _ in due] + [1.0] * len(closed)
    rejected, qvals = _seq.cohort(pvals)
    res["cohort"], res["counted_closed"] = len(pvals), len(closed)
    for (h, lk, st), rej, qv in zip(due, rejected, qvals):
        verdict = dict(h.get("verdict") or {})
        look_no = int(h.get("looks_done") or 0) + 1
        entry = {**st, "q": qv, "look": look_no, "cohort": len(pvals), "date": d.isoformat(),
                 "not_expressed": _not_expressed(con, h["hyp_id"]),
                 "p_one_sided": lk.get("p"), "alpha_look": lk.get("alpha_look"),
                 "alpha_spent": lk.get("spent"), "upper": lk.get("upper"),
                 "n_max": lk.get("n_max"), "design": _seq.DESIGN}
        h["_stats"] = entry
        if rej:
            state, reason = SUPPORTED, (f"positive and significant at n={st['n']} after cohort "
                                        f"BH (look {look_no})")
        elif lk["futile"]:
            state, reason = REFUTED, (f"the evidence rules out a +{_seq.MIN_USEFUL_R:.2f}R edge "
                                      f"at n={st['n']} ({int(_seq.FUTILITY_CONF * 100)}% upper "
                                      f"bound {lk['upper']:+.2f}R)")
        elif lk["final"]:
            state, reason = REFUTED, (f"not shown at n={st['n']}: the whole budget is spent "
                                      f"(positive or not, never significant)")
        else:
            verdict[f"look{look_no}"] = entry
            res["continuing"].append(h)
            if apply:
                con.execute(
                    """UPDATE trader_hypotheses SET looks_done = ?, alpha_spent = ?,
                              test_design = ?, verdict_json = ? WHERE hyp_id = ?""",
                    [look_no, lk["spent"], _seq.DESIGN, json.dumps(verdict, default=str),
                     h["hyp_id"]])
            continue
        verdict["final"] = entry
        rule_id = None
        if apply:
            if state == SUPPORTED:
                rule_id = _safe_rule(con, h, st, qv, d)
            con.execute(
                """UPDATE trader_hypotheses SET state = ?, closed_at = ?, close_reason = ?,
                          looks_done = ?, alpha_spent = ?, test_design = ?, verdict_json = ?,
                          cohort_counted = TRUE, rule_id = ?
                   WHERE hyp_id = ? AND state = ?""",
                [state, d, reason, look_no, lk["spent"], _seq.DESIGN,
                 json.dumps(verdict, default=str), rule_id, h["hyp_id"], OPEN])
        h.update({"state": state, "close_reason": reason, "rule_id": rule_id})
        res["supported" if state == SUPPORTED else "refuted"].append(h)
    if apply:
        for h in closed:
            con.execute("UPDATE trader_hypotheses SET cohort_counted = TRUE WHERE hyp_id = ?",
                        [h["hyp_id"]])
    return res


def _safe_rule(con: Any, h: dict, st: dict, q: float | None, d: date) -> str | None:
    try:
        return _register_rule(con, h, st, q, d)
    except Exception as e:  # noqa: BLE001 -- a verdict must not be lost to the rule write
        _log(f"WARN could not quarantine {h['hyp_id']} as a rule: {e}")
        return None


def _not_expressed(con: Any, hyp_id: str) -> int:
    try:
        row = con.execute("SELECT COUNT(*) FROM hypothesis_reviews WHERE hyp_id = ? "
                          "AND expressed = FALSE", [hyp_id]).fetchone()
        return int(row[0] or 0)
    except Exception:  # noqa: BLE001
        return 0


def _verdict_line(h: dict) -> str:
    st = h.get("_stats") or {}
    nums = (f" n={st.get('n')}, mean {st['mean_r']:+.2f}R, p={st.get('p')}, q={round(st['q'], 4) if st.get('q') is not None else None}"
            if st.get("mean_r") is not None else "")
    rule = f" -> quarantined as rule [{h['rule_id']}]" if h.get("rule_id") else ""
    ne = (f" ({st['not_expressed']} trade(s) the grader said did not express the idea)"
          if st.get("not_expressed") else "")
    return f"- `{h['hyp_id']}` {h['statement'][:140]} —{nums}{ne}{rule}"


def weekly_md(res: dict, con: Any) -> str:
    rows = all_hypotheses(con)
    if not rows:
        return ""
    open_rows = [r for r in rows if r["state"] == OPEN]
    sample = samples(con)
    lines = ["## TRADER HYPOTHESES (the desk's own ideas, tested at minimum size; "
             "verdicts applied by the system)", ""]
    for label, key in (("SUPPORTED", "supported"), ("REFUTED", "refuted"),
                       ("STILL COLLECTING (looked at, not yet shown either way)", "continuing"),
                       ("EXPIRED", "expired")):
        if res.get(key):
            lines.append(f"**{label} ({len(res[key])}):**")
            lines += [_verdict_line(h) for h in res[key]]
            lines.append("")
    if res.get("cohort"):
        lines.append(f"Judged as one family of {res['cohort']} (Benjamini-Hochberg at one-sided "
                     f"{ALPHA}; each look spends only its share of the idea's budget; "
                     f"{res['counted_closed']} abandoned/expired idea(s) counted at p = 1).")
    lines.append(f"Open: {len(open_rows)}" + ("; " + ", ".join(
        f"{r['hyp_id']} {(sample.get(r['hyp_id']) or {}).get('n', 0)}/{r['target_n']}"
        for r in open_rows) if open_rows else ""))
    lines.append("These verdicts have ALREADY been applied. Do not re-decide them; state them. A "
                 "SUPPORTED idea is NOT edge: it is the trader's selection within the idea, and "
                 "only a VALIDATED rule may be cited as edge. If a SUPPORTED idea has no rule "
                 "yet, you may nominate it as a rule_candidate with a predicate.")
    return "\n".join(lines)


def weekly_html(md: str) -> str:
    if not md:
        return ""
    import html as _html
    return ('<h3>Trader hypotheses</h3><pre style="white-space:pre-wrap;font-size:12px">'
            + _html.escape(md) + "</pre>")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def render_text(con: Any, d: date, hyp_id: str | None = None) -> str:
    rows = all_hypotheses(con)
    if not rows:
        return "(no hypotheses registered)"
    sample = samples(con)
    if hyp_id:
        h = next((r for r in rows if r["hyp_id"] == hyp_id), None)
        if h is None:
            return f"(no hypothesis {hyp_id})"
        s = sample.get(hyp_id) or {"trades": []}
        notes = {}
        try:
            for pid, exp, note in con.execute(
                    "SELECT pick_id, expressed, note FROM hypothesis_reviews WHERE hyp_id = ?",
                    [hyp_id]).fetchall():
                notes[pid] = (exp, note)
        except Exception:  # noqa: BLE001
            pass
        out = [f"{h['hyp_id']} [{h['state']}] opened {h['opened_at']} by the {h['source']}",
               f"  {h['statement']}", f"  wrong if: {h['refutation']}"]
        if h.get("origin"):
            out.append(f"  origin: {h['origin']}")
        if h.get("verdict"):
            out.append(f"  verdict: {json.dumps(h['verdict'], default=str)}")
        out.append("  trades:")
        for t in s["trades"]:
            exp, note = notes.get(t["pick_id"], (None, None))
            r = f"{t['r']:+.2f}R" if t.get("r") is not None else t["status"]
            rev = f" | expressed={exp} {note or ''}" if exp is not None or note else ""
            out.append(f"    {t['plan_date']} {t['ticker']} [{t['lane'] or 'approved'}] {r}{rev}")
        return "\n".join(out)
    out = []
    for r in rows:
        s = sample.get(r["hyp_id"]) or {}
        out.append(f"{r['hyp_id']} {r['state']:<9} n={s.get('n', 0)}/{r['target_n']} "
                   f"tagged={s.get('tagged', 0)} opened={r['opened_at']} ({r['source']}) "
                   f"{r['statement'][:90]}")
    return "\n".join(out)
