"""
The trader's log (specs/TRADERS_LOG_SPEC_v1.md): a first-person daily entry,
written after the evening grading, for the HUMAN who owns the desk.

WHY IT IS BUILT THIS WAY
------------------------
The desk should sound like a person who knows what he has and has not proven.
That voice is a liability the moment it becomes evidence, so the log is boxed
in three ways:

  Numbers by reference (L-2). The model never writes a number. It is handed a
  closed set of SLOTS -- facts the system already computed -- and writes
  `{{slot.name}}` where a fact belongs; render() substitutes the stored value.
  Any digit left in the draft outside a slot token is a violation, as is a
  number spelled out in words past "two". A model cannot mint a number it was
  never given, and cannot alter one it was given.

  No new look (L-3). build_facts() copies what the system already reports:
  the day's fill-valid outcomes by planned R, the shadow ledger's scored count
  and forgone R, runner counts and the exit replays exactly as
  `run.py runner-exits` summarizes them, the backlog as the morning sees it,
  calibration cells that clear n >= 8, and verdict lines copied verbatim from
  the playbook the weekly rendered. It computes no new comparison, split or
  test. day_contrast() (email only, never an LLM input) is counts only.

  Write-only (L-1). Nothing the agent reads is built from traders_log.
  tests/test_traders_log_firewall.py enforces that statically.

Failure is silent to the loops and loud to the human (L-6): every entry point
is guarded, and when generation or validation fails the evening email carries
a deterministic, labeled facts-only list instead.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from typing import Any

from .grading import SCRATCH_R, outcome_class
from .util import log

__all__ = [
    "LANE", "SCOPES", "HEADINGS", "TOMORROW", "build_facts", "render_facts_only", "render",
    "facts_prompt", "generate", "load", "day_contrast", "CONTRAST_HEADER", "FALLBACK_NOTE",
]

LANE = "combined"
SCOPES = ("one_session", "cumulative", "computed_verdict", "count")
SCOPE_LABEL = {"one_session": "one session", "cumulative": "cumulative",
               "computed_verdict": "computed verdict", "count": "count"}
FAMILIES = ("day", "pick", "shadow", "runner", "deficit", "calibration", "verdict", "menu")

H_CANT_PROVE = "## What I still can't prove"
H_HAPPENED = "## What happened"
H_BELIEVE = "## What I believe (not evidence, not acted on)"
HEADINGS = (H_CANT_PROVE, H_HAPPENED, H_BELIEVE)
TOMORROW = "Tomorrow:"
NO_CHANGE = "no change"

FALLBACK_NOTE = "facts-only: the written log failed its checks"
CONTRAST_HEADER = ("One session, descriptive. This table is a count of what happened, not "
                   "evidence of what separates winners from losers. Only the weekly search, "
                   "under FDR correction, may say that.")

TOMORROW_CLAUSE_MAX = 160   # "at most one short clause" after the menu id
_TOKEN = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")
_DIGIT = re.compile(r"\d")
# Numbers in words. "one" and "two" are ordinary prose (the spec permits them);
# anything larger is a count, and a count must come through a slot.
_NUMBER_WORD = re.compile(
    r"\b(three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
    r"sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|"
    r"ninety|hundred|thousand|million|billion|dozen|third|fourth|fifth|sixth|seventh|eighth|"
    r"ninth|tenth|twentieth)\b", re.IGNORECASE)


# =============================================================================
# Facts
# =============================================================================

def _key(text: str) -> str:
    """A slot-name-safe key: letters, digits and underscores only."""
    return re.sub(r"[^A-Za-z0-9]+", "_", str(text)).strip("_") or "x"


def _r(v: float | None) -> str:
    return "n/a" if v is None else f"{float(v):+.2f}R"


class _Slots:
    def __init__(self) -> None:
        self.slots: dict[str, dict] = {}

    def add(self, name: str, value: Any, scope: str, source: str, label: str) -> str:
        assert scope in SCOPES, scope
        base, n = name, 2
        while name in self.slots:          # never silently overwrite a fact
            name, n = f"{base}_{n}", n + 1
        self.slots[name] = {"name": name, "value": str(value), "scope": scope,
                            "source": source, "label": label}
        return name


def _q(con: Any, sql: str, params: list | None = None) -> list[tuple]:
    return con.execute(sql, params or []).fetchall()


def _day_facts(con: Any, d: date, s: _Slots) -> bool:
    """Picks, fills and planned-R outcomes for the session. True if the
    session had any pick or plan at all."""
    from . import ledger
    picks = ledger.picks_for_date(con, d)
    plan = ledger.plan_for_date(con, d)
    outs = {}
    if picks:
        marks = ",".join("?" for _ in picks)
        for pid, trig, reason, anomaly, rp in _q(
                con, f"""SELECT pick_id, triggered, exit_reason, execution_anomaly, r_planned
                         FROM outcomes WHERE pick_id IN ({marks})""",
                [p["pick_id"] for p in picks]):
            outs[pid] = {"triggered": trig, "exit_reason": reason,
                         "execution_anomaly": anomaly, "r_planned": rp}
    src = "picks + outcomes (r_planned)"
    morning = [p for p in picks if p.get("setup_type") != ledger.RUNNER_SETUP]
    runner = [p for p in picks if p.get("setup_type") == ledger.RUNNER_SETUP]
    s.add("day.picks_morning", len(morning), "one_session", "picks", "morning-lane picks taken")
    s.add("day.picks_runner", len(runner), "one_session", "picks", "runner-lane picks taken")

    invalid = [p for p in picks if (outs.get(p["pick_id"]) or {}).get("exit_reason") == "invalid_fill"]
    anomalies = [p for p in picks if (outs.get(p["pick_id"]) or {}).get("execution_anomaly")
                 and (outs.get(p["pick_id"]) or {}).get("exit_reason") != "invalid_fill"]
    valid = [p for p in picks
             if (outs.get(p["pick_id"]) or {}).get("triggered")
             and p not in invalid and p not in anomalies]
    graded = [p for p in picks if p["pick_id"] in outs]
    measured = [(p, outs[p["pick_id"]]["r_planned"]) for p in valid
                if outs[p["pick_id"]]["r_planned"] is not None]
    classes = [outcome_class(r) for _, r in measured]
    wins, losses, scratches = (classes.count("win"), classes.count("loss"),
                               classes.count("scratch"))
    band = f"{SCRATCH_R:.2f}R"
    s.add("day.graded", len(graded), "one_session", "outcomes", "picks with a graded outcome")
    s.add("day.triggered", len(valid), "one_session", src,
          "fill-valid trades that triggered (invalid fills and execution anomalies excluded)")
    s.add("day.wins", wins, "one_session", src, f"fill-valid trades with planned R above +{band}")
    s.add("day.losses", losses, "one_session", src, f"fill-valid trades with planned R below -{band}")
    s.add("day.scratches", scratches, "one_session", src,
          f"fill-valid trades within +/-{band} of zero: scratches, neither a win nor a loss")
    s.add("day.net_r", _r(sum(r for _, r in measured)) if measured else "n/a", "one_session", src,
          "sum of planned R per fill-valid trade (unweighted by size)")
    if len(measured) < len(valid):
        # Never backfilled from r_multiple: a trade with no planned R is
        # counted as unmeasured, so wins + losses + this add up honestly.
        s.add("day.unmeasured_r", len(valid) - len(measured), "one_session", src,
              "fill-valid trades with no planned R recorded (graded before r_planned existed)")
    s.add("day.invalid_fills", len(invalid), "one_session", "outcomes.exit_reason",
          "invalid fills: not trades, excluded and never narrated")
    if anomalies:
        s.add("day.execution_anomalies", len(anomalies), "one_session",
              "outcomes.execution_anomaly", "execution anomalies: excluded from results")
    exits: dict[str, int] = {}
    for p in valid:
        reason = outs[p["pick_id"]].get("exit_reason") or "unknown"
        exits[reason] = exits.get(reason, 0) + 1
    if exits:
        s.add("day.exits", ", ".join(f"{k} {v}" for k, v in sorted(exits.items())),
              "one_session", "outcomes.exit_reason", "how the fill-valid trades exited")
    untriggered = sum(1 for p in graded if not outs[p["pick_id"]].get("triggered")
                      and p not in invalid)
    s.add("day.untriggered", untriggered, "one_session", "outcomes.triggered",
          "picks whose entry never triggered")

    for p in picks:
        if p in invalid:
            continue                     # counted above, never narrated
        lane = "runner" if p.get("setup_type") == ledger.RUNNER_SETUP else "morning"
        o = outs.get(p["pick_id"])
        tag = p.get("exploration_lane")
        what = (f"{p['ticker']} {p.get('direction')} {p.get('setup_type')}"
                + (f" ({tag})" if tag else "")
                + (f", planned size {float(p['size_r']):.2f}R" if p.get("size_r") is not None else ""))
        if o is None:
            result = "not graded"
        elif p in anomalies:
            result = "execution anomaly (excluded)"
        elif not o.get("triggered"):
            result = "did not trigger"
        else:
            result = f"{_r(o.get('r_planned'))} ({o.get('exit_reason') or 'exit unknown'})"
        s.add(f"pick.{lane}.{_key(p['ticker'])}", f"{what}: {result}", "one_session",
              "picks + outcomes (r_planned)", "one pick and its planned-R result")

    if not morning:
        s.add("day.stand_down_reason", _stand_down_reason(con, d, plan), "one_session",
              "daily_plans.no_trade_category / runs.status", "why the morning lane had no picks")
    return bool(picks or plan)


def _stand_down_reason(con: Any, d: date, plan: dict | None) -> str:
    """What the system STORED about a session with no morning picks. Never a
    guess: if the stored data does not say, the log says so."""
    if plan is not None:
        row = _q(con, "SELECT no_trade_category, session_degraded FROM daily_plans WHERE plan_date = ?", [d])
        cat, degraded = (row[0] if row else (None, None))
        if cat == "data_incomplete" or degraded:
            return "data outage: the pipeline could not evaluate the market (data_incomplete)"
        if cat:
            return f"stand-down recorded by the morning loop ({cat})"
        return "not distinguished by the system"
    runs = _q(con, "SELECT status FROM runs WHERE loop = 'morning' AND run_date = ? ORDER BY run_id DESC", [d])
    if runs:
        return f"no morning plan was produced (morning run status: {runs[0][0]})"
    return "no morning plan and no morning run recorded (not distinguished by the system)"


def _shadow_facts(con: Any, d: date, s: _Slots) -> bool:
    rows = _q(con, """SELECT COUNT(*), COUNT(forgone_r), SUM(forgone_r) FROM rejections
                      WHERE plan_date = ? AND graded_at IS NOT NULL""", [d])
    n, priced, net = rows[0] if rows else (0, 0, None)
    if not n:
        return False
    s.add("shadow.scored", int(n), "one_session", "rejections (shadow ledger)",
          "rejected candidates scored by the shadow ledger")
    # Unmeasurable rows (no bars, invalid fill) carry no forgone R. Saying
    # "7 scored, -0.14R" when the R covers 6 made two honest counts disagree.
    s.add("shadow.priced", int(priced or 0), "one_session", "rejections.forgone_r IS NOT NULL",
          "of those, rejections with a measurable forgone R (the rest had no usable bars)")
    s.add("shadow.forgone_r", _r(net or 0.0), "one_session", "rejections.forgone_r",
          "net forgone R over the priced rejections (positive = the filters cost the desk)")
    if priced:
        s.add("shadow.forgone_per_rejection", _r((net or 0.0) / int(priced)), "one_session",
              "rejections.forgone_r / priced", "forgone R per priced rejection")
    return True


# The four runner exit policies the log reports, by the name a reader knows.
_EXIT_NAMES = {
    "plan": "plan exits (the selected plan's own stop and 2R target)",
    "arm_a_floor": "arm A, stop floor (stop no tighter than 1 ATR)",
    "arm_b_dashboard": "arm B, dashboard rules (half at 1R, 9 EMA trail)",
    "arm_c_floor_dashboard": "arm C, floor + dashboard (arm A's stop, arm B's management)",
}


def _runner_facts(con: Any, d: date, s: _Slots) -> bool:
    from . import ledger
    n_cand = (_q(con, "SELECT COUNT(*) FROM runner_candidates WHERE session_date = ?", [d]) or [(0,)])[0][0]
    if not n_cand:
        return False
    sel = (_q(con, """SELECT COUNT(*) FROM runner_candidate_outcomes
                      WHERE session_date = ? AND selected""", [d]) or [(0,)])[0][0]
    s.add("runner.candidates", int(n_cand), "one_session", "runner_candidates",
          "runner candidates the intraday scan found")
    s.add("runner.selected", int(sel or 0), "one_session", "runner_candidate_outcomes.selected",
          "runner candidates selected")
    note = (_q(con, "SELECT selection_skipped_reason FROM runner_sessions WHERE session_date = ?", [d])
            or [(None,)])[0][0]
    m = re.search(r"industry cap \S+ dropped (\d+)", note or "")
    s.add("runner.industry_cap_drops", int(m.group(1)) if m else 0, "one_session",
          "runner_sessions.selection_skipped_reason", "selections dropped by the per-industry cap")
    # Why the count of selected is what it is. "0 of 533 selected" with no
    # reason (10/05) read as a decision; it was a claim whose request died.
    s.add("runner.selection", ledger.runner_selection_note(con, d)["text"], "one_session",
          "runner_sessions via ledger.runner_selection_note",
          "what happened to the session's one runner selection")
    # The exit replays for this one session, summarized exactly as
    # `run.py runner-exits` summarizes them (runner_exits.policy_summary).
    try:
        from . import runner_exits
        rows = [r for r in ledger.runner_exit_replays_since(con, d)
                if r.get("session_date") == d and r.get("policy_version") == runner_exits.POLICY_VERSION
                and r.get("r_planned") is not None]
        for summ in runner_exits.policy_summary(rows):
            if summ["policy"] not in _EXIT_NAMES or not summ["n"]:
                continue
            win = "n/a" if summ["win_rate"] is None else f"{100 * summ['win_rate']:.0f}%"
            # The value names its policy: the reader sees the value, not the
            # slot, and four unnamed lines (10/05) had to be decoded by count.
            s.add(f"runner.exit.{summ['policy']}",
                  f"{_EXIT_NAMES[summ['policy']]}: {_r(summ['mean_r'])} per trade over "
                  f"{summ['n']} replayed trades, {win} closed positive",
                  "one_session", "runner_exit_replays via runner_exits.policy_summary",
                  f"runner exit replay, policy {summ['policy']} (each in its own risk unit)")
    except Exception as e:  # noqa: BLE001 -- one family must not cost the others
        log("traders_log", f"WARN runner exit summary unavailable: {e}")
    return True


def _deficit_facts(con: Any, d: date, s: _Slots) -> dict[str, str]:
    """The backlog exactly as tomorrow morning's analyst is shown it. Returns
    {menu slot name: item id}."""
    from . import backlog
    items = backlog.morning_selection(backlog.latest(con, d), backlog.TOP_N)
    menu: dict[str, str] = {}
    for it in items:
        eta = f", ~{it['eta_sessions']} sessions at current pace" if it.get("eta_sessions") else ""
        stalled = " — STALLED (nothing in 10 sessions)" if it.get("stalled") else ""
        k = _key(it["item_id"])
        s.add(f"deficit.{k}", f"{it['item_id']} ({it['item_type']}): {it['n']}/{it['target']}{eta}{stalled}",
              "count", "evidence_backlog via backlog.morning_selection",
              f"proof deficit: {it.get('what_counts') or it['item_id']}")
        name = s.add(f"menu.{k}", it["item_id"], "count", "evidence_backlog",
                     "a DATA-STARVED item the Tomorrow line may name")
        menu[name] = it["item_id"]
    return menu


def _calibration_facts(con: Any, s: _Slots) -> None:
    from . import ledger
    for c in ledger.calibration_aggregate(con):
        if c.get("suppressed") or not c.get("n") or c.get("conviction") is None:
            continue                      # under n=8: omitted, not shown as suppressed
        wr = "n/a" if c.get("win_rate") is None else f"{100 * c['win_rate']:.0f}%"
        s.add(f"calibration.conviction_{int(c['conviction'])}",
              f"conviction {int(c['conviction'])}: n {c['n']}, {wr} winners, {_r(c.get('avg_r'))} per trade",
              "cumulative", "calibration via ledger.calibration_aggregate (n >= 8 only)",
              "realized results at one stated conviction, morning lane, all sessions")


_VERDICT_LINES = (("verdict.calibration_q3", re.compile(r"^Calibration verdict \(computed, Q3\):.*$", re.M),
                   "Open Question 3's computed calibration verdict"),
                  ("verdict.runner_lane", re.compile(r"^Lane stop condition:.*$", re.M),
                   "the runner lane's computed stop-condition status"))


def _verdict_facts(con: Any, s: _Slots) -> None:
    """Verdict lines copied verbatim from the playbook the weekly rendered."""
    from .memory import read_playbook
    text, version = read_playbook(con)
    for name, pat, label in _VERDICT_LINES:
        m = pat.search(text or "")
        if m:
            s.add(name, m.group(0).strip(), "computed_verdict",
                  f"playbook v{version} (system-rendered section)", label)


def build_facts(con: Any, session_date: date) -> dict:
    """Every fact the log may state, as slots. Pure reads of what the system
    already computed. Never raises: a failing family is recorded in `errors`
    and the rest still build. `slots` is empty when the session has nothing
    to report (no plan, no pick, no runner candidate, no scored rejection)."""
    s = _Slots()
    errors: list[str] = []
    menu: dict[str, str] = {}
    session_has_data = False
    for fn in (_day_facts, _shadow_facts, _runner_facts):
        try:
            session_has_data = bool(fn(con, session_date, s)) or session_has_data
        except Exception as e:  # noqa: BLE001
            errors.append(f"{fn.__name__}: {e}")
    if not session_has_data:
        return {"session_date": str(session_date), "slots": {}, "menu": {},
                "menu_item_ids": [NO_CHANGE], "errors": errors}
    try:
        menu = _deficit_facts(con, session_date, s)
    except Exception as e:  # noqa: BLE001
        errors.append(f"_deficit_facts: {e}")
    for fn in (_calibration_facts, _verdict_facts):
        try:
            fn(con, s)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{fn.__name__}: {e}")
    return {"session_date": str(session_date), "slots": s.slots, "menu": menu,
            "menu_item_ids": list(menu.values()) + [NO_CHANGE], "errors": errors}


# =============================================================================
# Deterministic fallback
# =============================================================================

def render_facts_only(facts: dict) -> str:
    """The same facts as a plain, labeled list: no narrative, no persona.
    What the email shows whenever the written log is absent or refused."""
    slots = list((facts.get("slots") or {}).values())
    groups = (
        ("What the desk still can't prove (counts)", lambda x: x["name"].startswith("deficit.")),
        ("What happened (one session, not evidence)", lambda x: x["scope"] == "one_session"),
        ("Cumulative", lambda x: x["scope"] == "cumulative"),
        ("Computed verdicts (copied from the system)", lambda x: x["scope"] == "computed_verdict"),
    )
    out = ["FACTS ONLY (no narrative)"]
    for title, keep in groups:
        rows = [x for x in slots if keep(x)]
        if not rows:
            continue
        out += ["", title + ":"]
        for x in rows:
            val = f'"{x["value"]}"' if x["scope"] == "computed_verdict" else x["value"]
            # The group title already names a one-session group's scope.
            scope = "" if x["scope"] == "one_session" and "one session" in title \
                else f" ({SCOPE_LABEL[x['scope']]})"
            out.append(f"- {x['label']}: {val}{scope}")
    if len(out) == 1:
        out.append("(no facts for this session)")
    return "\n".join(out)


# =============================================================================
# Rendering and validation
# =============================================================================

# The "What happened" section is about one session by definition, so its
# heading carries that scope once. A one-session value cited anywhere else
# still carries its own suffix. (It used to be appended to every value, which
# on a full day put "(one session)" after nearly every number of the entry.)
H_HAPPENED_RENDERED = f"{H_HAPPENED} (one session, not evidence)"


def _substitute(slot: dict, scoped: bool = False) -> str:
    if slot["scope"] == "one_session":
        return slot["value"] if scoped else f"{slot['value']} (one session)"
    if slot["scope"] == "computed_verdict":
        return f'"{slot["value"]}"'
    return slot["value"]


def _normalize(raw: str) -> str:
    text = (raw or "").replace("\r\n", "\n").replace("’", "'").strip()
    # A draft wrapped in a code fence is the same draft.
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    if text.rstrip().endswith("```"):
        text = text.rstrip()[:-3]
    return text.strip()


def render(raw_text: str, facts: dict, max_chars: int = 3000) -> tuple[str, list[str]]:
    """(rendered, violations). `rendered` is "" unless violations is empty."""
    slots = facts.get("slots") or {}
    menu = facts.get("menu") or {}
    text = _normalize(raw_text)
    v: list[str] = []
    if not text:
        return "", ["empty draft"]

    tokens = _TOKEN.findall(text)
    for name in tokens:
        if name not in slots:
            v.append(f"unknown slot {{{{{name}}}}}")
    bare = _TOKEN.sub(" ", text)
    if "{{" in bare or "}}" in bare:
        v.append("malformed slot token (use exactly {{slot.name}} from the list)")
    for m in _DIGIT.finditer(bare):
        ctx = bare[max(0, m.start() - 12):m.end() + 12].replace("\n", " ")
        v.append(f"digit outside a slot: ...{ctx.strip()}...")
        break
    words = sorted({w.lower() for w in _NUMBER_WORD.findall(bare)})
    if words:
        v.append(f"number written in words (use a slot): {', '.join(words)}")

    lines = text.split("\n")
    heads = [(i, l.strip()) for i, l in enumerate(lines) if l.strip().startswith("#")]
    if [h for _, h in heads] != list(HEADINGS):
        v.append("sections must be exactly, in order: " + " / ".join(HEADINGS)
                 + f" (found: {' / '.join(h for _, h in heads) or 'none'})")
    nonempty = [(i, l.strip()) for i, l in enumerate(lines) if l.strip()]
    if heads and nonempty and nonempty[0][0] < heads[0][0]:
        v.append("text before the first section heading")
    last_i, last = nonempty[-1]
    tomorrow_lines = [i for i, l in nonempty if l.startswith(TOMORROW)]
    if not last.startswith(TOMORROW):
        v.append("the last line must start with 'Tomorrow:'")
    elif len(tomorrow_lines) != 1:
        v.append("exactly one 'Tomorrow:' line")
    else:
        rest = last[len(TOMORROW):]
        names = _TOKEN.findall(rest)
        menu_names = [n for n in names if n in menu]
        other = [n for n in names if n not in menu]
        says_no_change = NO_CHANGE in rest.lower()
        if other:
            v.append("the Tomorrow line may name only a menu item: " + ", ".join(other))
        if says_no_change and menu_names:
            v.append("the Tomorrow line names a menu item AND 'no change'; pick one")
        elif not says_no_change and len(menu_names) != 1:
            v.append(f"the Tomorrow line must name exactly one menu item or 'no change' "
                     f"(found {len(menu_names)})")
        clause = _TOKEN.sub("", rest).replace(NO_CHANGE, "").strip(" .:-—")
        if len(clause) > TOMORROW_CLAUSE_MAX:
            v.append("the Tomorrow line allows one short clause after the item")
    belief_i = next((i for i, h in heads if h == H_BELIEVE), None)
    if belief_i is not None:
        body = "\n".join(lines[belief_i + 1:last_i if last.startswith(TOMORROW) else len(lines)])
        if _TOKEN.search(body):
            v.append("the belief section may contain no slot (beliefs carry no result)")
        if _DIGIT.search(_TOKEN.sub(" ", body)):
            v.append("the belief section may contain no digit")

    if v:
        return "", v
    out, section = [], None
    for line in lines:
        if line.strip().startswith("#"):
            section = line.strip()
            if section == H_HAPPENED:
                line = line.replace(H_HAPPENED, H_HAPPENED_RENDERED, 1)
        scoped = section == H_HAPPENED
        out.append(_TOKEN.sub(lambda m: _substitute(slots[m.group(1)], scoped), line))
    rendered = "\n".join(out)
    if len(rendered) > max_chars:
        return "", [f"rendered log is {len(rendered)} characters; the cap is {max_chars}"]
    return rendered, []


# =============================================================================
# Generation
# =============================================================================

def facts_prompt(facts: dict) -> str:
    """The user message: the slots and the menu, nothing else -- no playbook,
    no lessons, no trade tables."""
    slots = facts.get("slots") or {}
    lines = ["# SLOTS", "",
             "Write a fact ONLY by writing its token exactly as shown. The value is what the "
             "reader will see; you never type it.", ""]
    for x in slots.values():
        if x["name"].startswith("menu."):
            continue
        lines.append(f"{{{{{x['name']}}}}} = {x['value']}  [{SCOPE_LABEL[x['scope']]}] — {x['label']}")
    lines += ["", "# MENU (the Tomorrow: line names exactly one of these tokens, or `no change`)", ""]
    for name, item in (facts.get("menu") or {}).items():
        lines.append(f"{{{{{name}}}}} = {item}")
    lines.append(f"`{NO_CHANGE}`")
    lines += ["", "Write today's log now."]
    return "\n".join(lines)


def _retry_prompt(user: str, draft: str, violations: list[str]) -> str:
    return (user + "\n\n# YOUR PREVIOUS DRAFT BROKE THESE RULES\n\n"
            + "\n".join(f"- {x}" for x in violations)
            + "\n\n# YOUR PREVIOUS DRAFT\n\n" + draft[:6000]
            + "\n\nWrite the whole log again, fixing every item above.")


def _merge(u1: dict, u2: dict) -> dict:
    out = dict(u1 or {})
    for k in ("input_tokens", "output_tokens", "cost_usd"):
        out[k] = (out.get(k) or 0) + ((u2 or {}).get(k) or 0)
    return out


def _persist(con: Any, row: dict) -> None:
    con.execute("DELETE FROM traders_log WHERE session_date = ? AND lane = ?",
                [row["session_date"], row["lane"]])
    con.execute(
        """INSERT INTO traders_log (session_date, lane, generated_at, persona_version, model,
               status, facts_json, raw_text, rendered_text, violations_json,
               tokens_in, tokens_out, cost_usd)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [row["session_date"], row["lane"], row["generated_at"], row["persona_version"],
         row["model"], row["status"], row["facts_json"], row["raw_text"], row["rendered_text"],
         row["violations_json"], row["tokens_in"], row["tokens_out"], row["cost_usd"]])


def generate(con: Any, cfg: Any, session_date: date, *, llm_client: Any = None,
             deadline: float | None = None) -> dict:
    """Build, write, validate and store the session's log. Never raises.

    `deadline` is a time.monotonic() instant the evening must not pass; when
    too little time is left the model is not called and the facts-only
    fallback is stored instead. One retry on violations; a second failure
    stores the fallback."""
    from .llm import LLMClient, load_prompt, persona_block, persona_version
    lcfg = dict(cfg.get("traders_log", {}) or {})
    model = lcfg.get("model") or (cfg.get("models", {}) or {}).get("evening") or "claude-sonnet-4-5"
    max_chars = int(lcfg.get("max_chars") or 3000)
    timeout = float(lcfg.get("call_timeout_s") or 45)
    started = datetime.now()
    row: dict[str, Any] = {
        "session_date": session_date, "lane": LANE, "generated_at": started,
        "persona_version": None, "model": model, "status": "error", "facts_json": None,
        "raw_text": None, "rendered_text": None, "violations_json": None,
        "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
    usage: dict = {}
    facts: dict = {}
    try:
        facts = build_facts(con, session_date)
        row["facts_json"] = json.dumps(facts, default=str)
        if not facts.get("slots"):
            row.update(status="skipped", model=None,
                       violations_json=json.dumps({"reason": "no facts for this session",
                                                   "errors": facts.get("errors") or []}))
            _store(con, row)
            return row
        fallback = render_facts_only(facts)

        def _time_left() -> float:
            return float("inf") if deadline is None else deadline - time.monotonic()

        if _time_left() < 15:
            row.update(status="fallback_facts_only", rendered_text=fallback,
                       violations_json=json.dumps({"not_generated": "evening budget exhausted"}))
            _store(con, row)
            return row

        persona = persona_block(cfg, None)
        row["persona_version"] = persona_version(cfg) if persona else None
        system = load_prompt("traders_log", {"PERSONA": persona})
        user = facts_prompt(facts)
        try:
            client = llm_client or LLMClient(cfg)
        except SystemExit as e:   # no API key: LLMClient exits; the evening must not
            raise RuntimeError(f"LLM client unavailable: {e}") from None
        mock_name = lcfg.get("mock_name") or "traders_log"

        def _call(u: str):
            return client.call(system=system, user=u, model=model, mock_name=mock_name,
                               thinking=False, web_search=False, expect_json=False,
                               max_tokens=int(lcfg.get("max_tokens") or 1500), attempts=1,
                               timeout=max(5.0, min(timeout, _time_left() - 10)),
                               # The draft is stored once, in this table's raw_text;
                               # the generic response archive would be a second copy.
                               archive=False)

        res = _call(user)
        usage = res.usage or {}
        rendered, violations = render(res.text, facts, max_chars)
        raw, first, retried = res.text, violations, False
        if violations and _time_left() >= 15:
            res2 = _call(_retry_prompt(user, res.text, violations))
            usage = _merge(usage, res2.usage or {})
            rendered, violations = render(res2.text, facts, max_chars)
            raw, retried = res2.text, True
        if violations:
            row.update(status="fallback_facts_only", rendered_text=fallback,
                       violations_json=json.dumps({"first_draft": first,
                                                   "retry": violations if retried else None}))
        else:
            row.update(status="ok", rendered_text=rendered,
                       violations_json=json.dumps({"first_draft": first}) if first else None)
        row["raw_text"] = raw
    except Exception as e:  # noqa: BLE001 -- never raise into the evening
        log("traders_log", f"WARN trader's log failed; the evening is unaffected: {e}")
        row.update(status="error", violations_json=json.dumps({"error": str(e)[:1000]}),
                   rendered_text=render_facts_only(facts) if facts.get("slots") else None)
    row.update(tokens_in=int(usage.get("input_tokens") or 0),
               tokens_out=int(usage.get("output_tokens") or 0),
               cost_usd=float(usage.get("cost_usd") or 0.0))
    _store(con, row, usage=usage, started=started, cfg=cfg)
    return row


def _store(con: Any, row: dict, usage: dict | None = None, started: datetime | None = None,
           cfg: Any = None) -> None:
    """Persist the row, and the call's cost in `runs` (loop 'traders_log').
    Mock runs persist nothing: a canned draft is not a log."""
    if cfg is not None and getattr(cfg, "mock", False):
        return
    try:
        _persist(con, row)
    except Exception as e:  # noqa: BLE001
        log("traders_log", f"WARN could not store the trader's log: {e}")
    if usage is not None and started is not None:
        try:
            from . import ledger
            ledger.log_run(con, "traders_log", row["session_date"], started,
                           {"ok": "ok", "fallback_facts_only": "degraded"}.get(row["status"], "error"),
                           row.get("model") or "", usage,
                           # The status only: validator messages can quote the
                           # draft, and the draft lives in traders_log alone.
                           error="" if row["status"] == "ok" else row["status"],
                           persona_version=row.get("persona_version"))
        except Exception as e:  # noqa: BLE001
            log("traders_log", f"WARN could not log the trader's log cost: {e}")


def load(con: Any, session_date: date) -> dict | None:
    """The stored row for the owner's surfaces (email, CLI). Never read by a loop."""
    cols = ("session_date", "lane", "generated_at", "persona_version", "model", "status",
            "facts_json", "raw_text", "rendered_text", "violations_json", "tokens_in",
            "tokens_out", "cost_usd")
    try:
        r = con.execute(f"SELECT {', '.join(cols)} FROM traders_log WHERE session_date = ? AND lane = ?",
                        [session_date, LANE]).fetchone()
    except Exception:  # noqa: BLE001
        return None
    return dict(zip(cols, r)) if r else None


# =============================================================================
# The day's winners and losers -- email only, counts only, never an LLM input
# =============================================================================

def _counts_table(rows: list[dict], lane: str, dims: list[str]) -> list[str]:
    from . import buckets
    out = []
    for dim in dims:
        cells: dict[str, list[int]] = {}
        for r in rows:
            lvl = buckets.label(lane, dim, r)
            c = cells.setdefault(lvl, [0, 0, 0])
            cls = outcome_class(r.get("r_planned"))
            c[0 if cls == "win" else 1 if cls == "loss" else 2] += 1
        for lvl, (w, l, other) in sorted(cells.items()):
            out.append(f"  {dim}={lvl}: winners {w}, losers {l}"
                       + (f", scratch or unmeasured {other}" if other else ""))
    return out


def day_contrast(con: Any, session_date: date) -> str:
    """Counts of the day's winners and losers by each already-defined
    dimension. No rate, no difference, no sign. "" when there is nothing."""
    from . import buckets, research
    try:
        morning = research.morning_rows(con, on=session_date)
    except Exception:  # noqa: BLE001
        morning = []
    try:
        runner = research.runner_rows(con, on=session_date)
    except Exception:  # noqa: BLE001
        runner = []
    if not morning and not runner:
        return ""
    out = [CONTRAST_HEADER]
    if morning:
        out += ["", f"Taken trades (morning lane, fill-valid, triggered): {len(morning)}"]
        out += _counts_table(morning, "morning", list(buckets.dimensions("morning")))
    if runner:
        out += ["", f"Runner candidates (triggered, fill-valid, current scan/sim): {len(runner)}"]
        out += _counts_table(runner, "runner", list(buckets.dimensions("runner")))
    return "\n".join(out)
