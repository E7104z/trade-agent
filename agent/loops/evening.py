"""
EVENING LOOP (4:30 PM ET)

  pull actual intraday data for every pick -> deterministic outcome simulation
  (fills, stops, targets, MFE/MAE) -> Claude grades its own trades (process,
  not outcome) -> extract lessons -> write everything to memory.

v3 additions (specs/EVENING_GRADER_SPEC_v3.md):
  - grades REJECTED candidates too (shadow ledger: eod_outcome + forgone_r)
  - appends atomic calibration rows (stated conviction vs realized R)
  - emits rule_confirmation events on process-passes (grades A/B)
  - advances open-question progress counters (Q1-Q5)
  Writer isolation: this loop writes ONLY to the ledger DB, never playbook.md.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime

from .. import ledger
from ..config import Config
from ..delivery import evening_email_html, render_dashboard, send_email
from ..grading import (CURRENT_SIM_VERSION, TRADED_VIA_LANE, broker_summary_line,
                       classify_divergence, classify_shadow, grade_day, outcomes_md,
                       realized_r, shadow_pick, shadow_time_stop, simulate_pick)
from ..grading_context import build_grading_context
from ..llm import LLMClient, load_prompt, persona_block, persona_version, prompt_version
from ..coverage import OUTCOME_FIELDS, lane_of, outcome_coverage, plumbing
from ..coverage import describe as describe_plumbing
from ..failures import classify_failure, format_failure
from ..grounding import (lesson_in_scope, tape_summary, ungrounded_tokens,
                         validate_lesson_predicate)
from ..providers import MarketData
from ..util import is_market_day, log, now_et, prev_market_day, today_et

# vercel.json functions.maxDuration. A run killed at the budget logs nothing,
# so the warning fires on the runs that DO finish, while there is still room.
FUNCTION_BUDGET_S = 300
BUDGET_WARN_FRACTION = 0.70
BUDGET_WARN_S = FUNCTION_BUDGET_S * BUDGET_WARN_FRACTION

# Open-question registry (ids match playbook.md "Open Questions" numbering)
Q_GAP_FADE_MECHANICS = 1
Q_SYMPATHY_STANDALONE = 2
Q_CONVICTION = 3
Q_GAP_SIZE = 4
Q_SHADOW_FILTER_COST = 5
Q_STOP_PLACEMENT = 6
# indicator layer v1 (specs/INDICATOR_LAYER_SPEC_v1.md §5)
Q_RVOL_SEPARATION = 7      # does participation (RVOL) separate winners from chop?
Q_VWAP_REVERSAL_EDGE = 8   # VWAP-reclaim longs vs plain dip-buy longs -- same edge?
# indicator layer v2 (specs/INDICATOR_LAYER_SPEC_v2.md §6)
Q_STOP_ATR_SWEET_SPOT = 9      # do 1-2 ATR stops out-R sub-0.5 ATR stops?
Q_BLUE_SKY_VS_OVERHEAD = 10    # gaps near the 52w high vs gaps into the 20d range
Q_TREND_ALIGNMENT_200D = 11    # longs above vs below the 200d MA (shorts mirrored)
Q_SHORTEDNESS = 12             # does short%float >= 15 separate squeeze winners from chop?
# R:R threshold test -- ONE experiment, TWO arms, so two ids: a single counter
# cannot say whether each bucket has enough rows to compare. Ids 13/14 and not
# 7: the miner minted this as playbook "#7" in v4, which already belonged to
# Q_RVOL_SEPARATION here, so RVOL observations were being read as progress on
# an R:R experiment. See agent/open_questions.py.
Q_RR_TEST_TAKEN = 13           # taken trades at planned R:R >= 2.0
Q_RR_TEST_SHADOW = 14          # rejected candidates at 1.8 <= R:R < 2.0


def _planned_rr(row: dict) -> float | None:
    """Planned reward:risk from the stored level structure.

    Direction-agnostic on purpose: it is |target - entry| / |entry - stop|, and
    a row whose levels are the wrong side of entry for its direction yields a
    number that is still the ratio the risk manager was looking at.
    """
    try:
        entry = float(row["entry_price"])
        stop = float(row["stop_price"])
        target = float(row["target_price"])
    except (KeyError, TypeError, ValueError):
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    return abs(target - entry) / risk


def _pick_for_review(r: dict, picks: list[dict]) -> dict | None:
    """Resolve one pick_reviews row back to its pick, by pick_id when the LLM
    supplied a valid one, else by ticker. Shared by every consumer of
    pick_reviews so the resolution logic can't drift between them."""
    if isinstance(r.get("pick_id"), int):
        p = next((x for x in picks if x["pick_id"] == r["pick_id"]), None)
        if p is not None:
            return p
    return next((x for x in picks if x["ticker"].upper() == str(r.get("ticker", "")).upper()), None)


def _drop_invalid_fill_reviews(review: dict, picks: list[dict], outcomes: list[dict]) -> dict:
    """Strip any pick_reviews row that graded an invalid_fill pick.

    An invalid_fill outcome (agent/grading.py) means the fill landed on the
    wrong side of its own stop -- a simulation artifact, not a trade. The
    grader prompt tells the LLM not to grade these, but nothing enforces
    that: apply_grades and the v3 rule-confirmation loop both trust
    review["pick_reviews"] wholesale. Filtering it ONCE, here, before either
    consumer sees it, means neither has to re-derive the rule -- and a
    non-compliant LLM response can't write a letter grade (or, downstream,
    a rule confirmation) for a pick that was never actually a trade.
    """
    by_id = {o["pick_id"]: o for o in outcomes}
    kept = []
    for r in review.get("pick_reviews", []) or []:
        p = _pick_for_review(r, picks)
        o = by_id.get(p["pick_id"], {}) if p else {}
        if o.get("exit_reason") == "invalid_fill":
            log("evening", f"Dropping LLM grade for {r.get('ticker', '?')} "
                           f"(pick_id={p['pick_id'] if p else '?'}): invalid_fill -- "
                           f"not a real trade, no grade persisted regardless of what "
                           f"the grader returned")
            continue
        kept.append(r)
    review["pick_reviews"] = kept
    return review


def _all_fills_tainted(outcomes: list[dict]) -> bool:
    """True if every pick that had ANY fill activity today (a real trigger, or
    an invalid_fill artifact) is unreliable as evidence for a thesis lesson.

    A clean untriggered pick (real no_trigger) doesn't count against this --
    a genuine non-fill is a legitimate, lesson-worthy outcome on its own
    (missed_move_r etc). This only fires when the picks that DID see price
    action are ALL either not-really-trades (invalid_fill) or fills far
    enough from plan that the thesis was never actually tested
    (execution_anomaly) -- i.e. there is nothing clean left to learn from.
    """
    fills = [o for o in outcomes if o.get("triggered") or o.get("exit_reason") == "invalid_fill"]
    if not fills:
        return False
    return all(o.get("exit_reason") == "invalid_fill" or o.get("execution_anomaly") for o in fills)


def _lane_trades(picks: list[dict]) -> dict[tuple[str, str], dict]:
    """{(TICKER, direction): pick} for every pick the desk put on today, in any
    lane. A rejection that matches one was traded anyway."""
    return {(str(p["ticker"]).upper(), str(p.get("direction") or "").lower()): p for p in picks}


def _taken_r(con, pick_id: int) -> tuple[float | None, str]:
    """(the planned-risk R a taken pick was reported at, how it ended), from its
    stored outcome: filled | not_filled | invalid_fill | ungraded."""
    row = con.execute("SELECT r_planned, r_multiple, triggered, exit_reason FROM outcomes "
                      "WHERE pick_id = ?", [pick_id]).fetchone()
    if not row:
        return None, "ungraded"
    if row[3] == "invalid_fill":
        return None, "invalid_fill"
    if not row[2]:
        return None, "not_filled"
    r = row[0] if row[0] is not None else row[1]
    return (None if r is None else float(r)), "filled"


def _grade_rejections(con, md: MarketData, d: date, slippage_bps: float = 0.0,
                      flatten_at: str | None = None, min_bar_volume: float = 0) -> dict:
    """Shadow ledger: simulate every rejected candidate; never touches the equity curve.

    Priced by the SAME simulate_pick call a taken pick goes through, with the
    same friction and exit rules (flatten_at, min_bar_volume), the same entry
    window and the same time stop (grading.shadow_pick): comparisons between
    taken and rejected are only fair if both sides are graded identically. The
    time stop is the analyst's, recovered from daily_plans.analyst_json (the
    rejections table never stored one, so INTC 2026-10-15 was priced to its
    15:42 stop at -1.01R when the taken trade exited at its 14:30 time stop at
    -0.51R).

    A rejection the desk TRADED ANYWAY (coverage, daily floor, hypothesis) is
    not a counterfactual -- the filters did not stop it. It is priced with its
    taken pick's own parameters, so its shadow R IS the pick's R, stored as
    eod_outcome='traded_via_lane' with NO forgone_r (so it enters no total,
    mean or test), and listed under summary["traded_via_lane"] with its taken R.
    """
    rejections = ledger.ungraded_rejections(con, d)
    summary = {"scored": 0, "priced": 0, "triggered": 0, "net_forgone_r": 0.0,
               "gap_fade_notes": [], "rr_band_notes": [], "traded_via_lane": []}
    if not rejections:
        return summary
    traded = _lane_trades(ledger.picks_for_date(con, d))
    try:
        row = con.execute("SELECT analyst_json FROM daily_plans WHERE plan_date = ?", [d]).fetchone()
        analyst_json = row[0] if row else None
    except Exception:  # noqa: BLE001 -- a plan row without candidates prices with no time stop
        analyst_json = None
    for r in rejections:
        try:
            bars = md.minute_bars(r["ticker"], d)
        except Exception:
            bars = []
        pick = traded.get((str(r["ticker"]).upper(), str(r.get("direction") or "").lower()))
        shim = shadow_pick(
            r, pick=pick,
            time_stop=None if pick is not None else shadow_time_stop(
                analyst_json, r["ticker"], r.get("direction"), r.get("entry_price")))
        sim = simulate_pick(shim, bars, None, slippage_bps=slippage_bps,
                            flatten_at=flatten_at, min_bar_volume=min_bar_volume,
                            session_date=d)
        if pick is not None:
            eod_outcome, forgone = TRADED_VIA_LANE, None
            filled = bool(sim.get("triggered")) and sim.get("exit_reason") != "invalid_fill"
            taken_r, taken_status = _taken_r(con, pick["pick_id"])
            summary["traded_via_lane"].append({
                "ticker": r["ticker"], "lane": pick.get("exploration_lane") or "main",
                "shadow_r": realized_r(sim) if filled else None,
                "taken_r": taken_r, "taken_status": taken_status})
            ledger.save_rejection_outcome(con, r["rejection_id"], eod_outcome, forgone,
                                          sim_version=sim.get("sim_version"))
            log("evening", f"  shadow {r['ticker']} ({r['reason'][:40]}): traded via "
                           f"{pick.get('exploration_lane') or 'main'} -- excluded from forgone R")
            continue
        eod_outcome, forgone = classify_shadow(sim)
        ledger.save_rejection_outcome(con, r["rejection_id"], eod_outcome, forgone,
                                      sim_version=sim.get("sim_version"))
        summary["scored"] += 1
        summary["net_forgone_r"] += forgone or 0.0
        if forgone is not None:
            summary["priced"] += 1
            summary["triggered"] += 1 if sim.get("triggered") else 0
        log("evening", f"  shadow {r['ticker']} ({r['reason'][:40]}): {eod_outcome}"
                       + (f" {forgone:+.2f}R" if forgone is not None else ""))
        if r.get("setup_type") == "gap_fade":
            summary["gap_fade_notes"].append((r, sim, bars))
        # Arm B of the R:R threshold test. Only measurable rows count -- an
        # unmeasurable rejection contributes nothing to the comparison, so
        # counting it toward the threshold would let the bucket "fill" with
        # rows that can never be compared against anything.
        rr = r.get("planned_rr")
        if rr is None:
            rr = _planned_rr(r)
        if rr is not None and 1.8 <= rr < 2.0 and eod_outcome != "unmeasurable":
            summary["rr_band_notes"].append(
                (r["ticker"], round(float(rr), 3), eod_outcome, forgone))
    summary["net_forgone_r"] = round(summary["net_forgone_r"], 2)
    return summary


def _record_broker_truth(con, d: date, picks: list[dict], outcomes: list[dict]) -> list[dict]:
    """Store what the paper account actually did next to each simulated outcome
    (docs/FIX_PHASE0.md, D3), and say where it disagrees.

    For every pick with a live order: the broker's entry fill, exit fill, exit
    reason and live_r (R from its own fills) go onto the outcomes row, with
    sim_broker_divergence = none | sim_only_fill | broker_only_fill |
    price_gap. The simulated columns are never touched: the simulated R stays
    the research number. The outcome dicts are enriched in place so the
    grader's bundle and the email lead with the broker's result. Idempotent.
    Returns [{pick_id, ticker, divergence}] for the picks that went live."""
    live = {r["pick_id"]: r for r in ledger.live_orders_for_date(con, d)}
    if not live:
        return []
    by_pick = {p["pick_id"]: p for p in picks}
    out = []
    for o in outcomes:
        row = live.get(o["pick_id"])
        if row is None:
            continue
        broker = {"entry_fill": row.get("entry_fill_price"), "exit_fill": row.get("exit_fill_price"),
                  "exit_reason": row.get("exit_reason"), "live_r": row.get("live_r"),
                  "status": row.get("status")}
        div = classify_divergence(o, broker, by_pick.get(o["pick_id"]))
        o.update(broker_entry_fill=broker["entry_fill"], broker_exit_fill=broker["exit_fill"],
                 broker_exit_reason=broker["exit_reason"], broker_status=broker["status"] or "unknown",
                 live_r=broker["live_r"], sim_broker_divergence=div)
        con.execute(
            "UPDATE outcomes SET broker_entry_fill = ?, broker_exit_fill = ?, "
            "broker_exit_reason = ?, broker_status = ?, sim_broker_divergence = ? WHERE pick_id = ?",
            [broker["entry_fill"], broker["exit_fill"], broker["exit_reason"],
             o["broker_status"], div, o["pick_id"]])
        if broker["live_r"] is not None:
            ledger.set_live_r(con, o["pick_id"], broker["live_r"])
        out.append({"pick_id": o["pick_id"], "ticker": (by_pick.get(o["pick_id"]) or {}).get("ticker"),
                    "divergence": div})
    return out


def _lane_line(shadow: dict) -> str:
    """The rejections the desk traded anyway, on their own line with their
    taken R. Empty when there were none."""
    rows = shadow.get("traded_via_lane") or []
    if not rows:
        return ""
    parts = []
    for t in rows:
        r = t.get("taken_r")
        what = (f"{r:+.2f}R taken" if r is not None else
                {"invalid_fill": "invalid fill, not a trade",
                 "ungraded": "not graded yet"}.get(t.get("taken_status"), "did not fill"))
        parts.append(f"{t['ticker']} ({t['lane']}) {what}")
    return ("Traded via a lane (the filters did NOT stop these, so they are excluded from "
            "forgone R; each shows the R it was taken at): " + "; ".join(parts) + ".")


def _shadow_md(shadow: dict) -> str:
    """The grader's lines on the shadow ledger. Counts what the net covers, and
    says plainly what one session of it can and cannot show: on 2026-10-05 the
    grader read -0.14R over six priced rejections (0.02R a rejection) as
    "confirms filters are working".

    The count is of the rejections the filters actually stopped: ones the desk
    traded anyway are excluded and listed apart (see _lane_line). The sign
    legend ("positive = the filters cost the desk") appears only when there is
    a net to read -- beside a zero it was being quoted as a verdict, and when
    every rejection was traded the line says the filters stopped nothing."""
    scored, priced = int(shadow.get("scored") or 0), int(shadow.get("priced") or 0)
    net = float(shadow.get("net_forgone_r") or 0.0)
    lane = _lane_line(shadow)
    if not scored and not lane:
        return "No rejected candidates were scored today."
    if not scored:
        return ("Every rejected candidate was traded anyway, so the filters stopped "
                "nothing today and there is no forgone R to read.\n" + lane)
    per = f", {net / priced:+.2f}R per priced rejection" if priced else ""
    head = (f"{scored} rejected candidates scored, {priced} priced "
            f"({shadow.get('triggered', 0)} of them would have triggered; "
            f"{scored - priced} unmeasurable); net forgone {net:+.2f}R over the priced{per}")
    head += (", so the filters neither saved nor cost R on what they stopped."
             if not priced or abs(net) < 0.005 else " (positive = the filters cost the desk).")
    return (head + " One session of forgone R is a single noisy draw over a handful of names: "
            "it neither confirms nor refutes the filters. Only the weekly miner's cumulative "
            "shadow read can." + ("\n" + lane if lane else ""))


def _rr_band_note(ticker: str, rr: float, eod_outcome: str,
                  forgone: float | None) -> str:
    """Q14 progress note for one rejection in the 1.8-2.0 R:R band. Starts with
    the ticker, which is what `run.py regrade-shadow` keys on to avoid
    recording the same observation twice."""
    return (f"{ticker} planned R:R={rr:.2f} (1.8-2.0 band) {eod_outcome} "
            f"forgone={forgone if forgone is None else round(forgone, 2)}R")


def _q1_gap_fade_mechanics(con, md: MarketData, d: date, picks: list[dict],
                           outcomes: list[dict], shadow: dict,
                           slippage_bps: float = 0.0) -> int:
    """Q1: on every gap_fade candidate, compare planned entry vs open-print entry.

    Every note names its source -- `[taken]` for a desk pick, `[shadow]` for a
    rejected candidate priced by the simulator -- because a shadow row whose
    planned entry "triggered" in simulation was being reported as "the one
    triggered comparison", i.e. as a trade the desk took (WING, playbook v9,
    which the same document listed as a rejection). And the counter only
    moves when BOTH entries produced an R: the question compares two realized
    entries, and an untriggered planned entry has nothing to compare, which is
    how Q1 read 15/8 "THRESHOLD MET" while its own finding said the number of
    comparable observations was one.
    """
    n = 0
    candidates: list[tuple[dict, list, str]] = []
    for p in picks:
        if p.get("setup_type") == "gap_fade":
            try:
                candidates.append((p, md.minute_bars(p["ticker"], d), "taken"))
            except Exception:
                continue
    for r, _sim, bars in shadow.get("gap_fade_notes", []):
        candidates.append((r, bars, "shadow"))
    for c, bars, source in candidates:
        if not bars:
            continue
        planned = simulate_pick({"pick_id": 0, "direction": c["direction"],
                                 "entry_type": c.get("entry_type", "limit"),
                                 "entry_price": c["entry_price"], "stop_price": c["stop_price"],
                                 "target_price": c["target_price"],
                                 "entry_not_before": c.get("entry_not_before")}, bars, None,
                                slippage_bps=slippage_bps, session_date=d)
        open_sim = simulate_pick({"pick_id": 0, "direction": c["direction"],
                                  "entry_type": "open", "entry_price": bars[0]["o"],
                                  "stop_price": c["stop_price"],
                                  "target_price": c["target_price"]}, bars, None,
                                 slippage_bps=slippage_bps, session_date=d)
        planned_r = planned.get("r_multiple") if planned.get("triggered") else None
        open_r = open_sim.get("r_multiple") if open_sim.get("triggered") else None
        comparable = planned_r is not None and open_r is not None
        note = (f"{c['ticker']}: [{source}] planned-entry R="
                f"{planned_r if planned_r is not None else 'untriggered'}, "
                f"open-print R={open_r if open_r is not None else 'untriggered'}")
        ledger.add_question_progress(con, Q_GAP_FADE_MECHANICS, d,
                                     1 if comparable else 0, note)
        n += 1 if comparable else 0
    return n


def _v3_events(con, md: MarketData, d: date, picks: list[dict], outcomes: list[dict],
               review: dict, shadow: dict, slippage_bps: float = 0.0) -> dict:
    """Calibration rows, rule confirmations, open-question progress. DB writes only.

    Kept for callers that run both halves at once. The evening loop itself
    calls `_deterministic_events` BEFORE the grader call and `_llm_events`
    after it, so an LLM outage no longer costs the deterministic rows.
    """
    counts = _deterministic_events(con, md, d, picks, outcomes, shadow, slippage_bps)
    counts["confirmations"] = _llm_events(con, d, picks, outcomes, review)
    return counts


def _llm_events(con, d: date, picks: list[dict], outcomes: list[dict], review: dict) -> int:
    """Rule confirmations: the only v3 event that depends on the grader.

    Process-pass (A/B) confirms the setup's rule — regardless of P&L. A
    process-FAIL does not confirm a rule even if it made money. An
    execution_anomaly pick never confirms a rule either, no matter what grade
    the LLM assigned — the fill landed far enough from the planned entry that
    the thesis at the intended level was never actually tested (enforced here
    in code rather than trusted to prompt compliance; see grading.py).

    Idempotent per session: a retried evening replaces this session's
    confirmations instead of stamping them twice (runs 64-66 on 09-22 were
    three attempts at one session).
    """
    by_id = {o["pick_id"]: o for o in outcomes}
    con.execute("DELETE FROM rule_confirmations WHERE session_date = ?", [d])
    n = 0
    tick_to_pick = {p["ticker"].upper(): p for p in picks}
    for r in review.get("pick_reviews", []) or []:
        p = None
        if isinstance(r.get("pick_id"), int):
            p = next((x for x in picks if x["pick_id"] == r["pick_id"]), None)
        if p is None:
            p = tick_to_pick.get(str(r.get("ticker", "")).upper())
        o = by_id.get(p["pick_id"], {}) if p else {}
        if (p and str(r.get("grade", "")).upper() in ("A", "B") and p.get("setup_type")
                and not o.get("execution_anomaly") and not r.get("_ungrounded")):
            ledger.add_rule_confirmation(con, p["setup_type"], d, p["pick_id"])
            n += 1
    return n


def _deterministic_events(con, md: MarketData, d: date, picks: list[dict],
                          outcomes: list[dict], shadow: dict,
                          slippage_bps: float = 0.0) -> dict:
    """Calibration rows and every open-question increment. No LLM involved.

    Runs BEFORE the grader call (DATA_CONTINUITY_SPEC_v1 Phase 1): these rows
    are computed from outcomes and bars alone, and an Anthropic outage used
    to cost them because they were written after the call. Idempotent per
    session: calibration rows already replace by (session_date, ticker), and
    this session's question-progress rows are cleared first -- every
    open_question_progress row is written by this function, so a retry
    replaces them rather than counting the same observation twice.
    """
    counts = {"calibration": 0, "confirmations": 0, "q_increments": 0}
    by_id = {o["pick_id"]: o for o in outcomes}
    con.execute("DELETE FROM open_question_progress WHERE session_date = ?", [d])

    # MORNING lane only: legacy rows from the retired runner lane share the
    # `picks` table, and every calibration row and open question below is about
    # the morning desk.
    picks = [p for p in picks if p.get("setup_type") != ledger.RUNNER_SETUP]

    # calibration rows (taken picks only; conviction as stated in the morning,
    # never adjusted). Realized R is the planned-risk R every other morning
    # statistic reads, falling back to r_multiple on legacy rows.
    for p in picks:
        o = by_id.get(p["pick_id"], {})
        realized = None
        if o.get("triggered") and o.get("exit_reason") != "invalid_fill":
            realized = o.get("r_planned") if o.get("r_planned") is not None else o.get("r_multiple")
        ledger.add_calibration_row(con, d, p["ticker"], p.get("conviction"), realized)
        counts["calibration"] += 1
        if realized is not None:  # Q3 increments with every realized calibration row
            ledger.add_question_progress(con, Q_CONVICTION, d, 1,
                                         f"{p['ticker']} conv={p.get('conviction')} R={realized:+.2f}")
            counts["q_increments"] += 1

    # Q2: sympathy standalone — only when the sympathy pick is the day's sole pick
    # (approximation of "leader not concurrently held"; noted for the miner)
    for p in picks:
        if p.get("setup_type") == "sympathy":
            o = by_id.get(p["pick_id"], {})
            if len(picks) == 1:
                # R here stays r_multiple: question_eval._q2 parses it back out
                # of every note ever written, and those were all r_multiple.
                ledger.add_question_progress(con, Q_SYMPATHY_STANDALONE, d, 1,
                                             f"{p['ticker']} standalone sympathy R={o.get('r_multiple')}")
                counts["q_increments"] += 1
            else:
                ledger.add_question_progress(con, Q_SYMPATHY_STANDALONE, d, 0,
                                             f"{p['ticker']} sympathy taken alongside other picks — not standalone, no increment")

    # Q4: gap trades — bucket the actual gap size vs realized R
    for p in picks:
        if p.get("setup_type") in ("gap_go", "gap_fade"):
            o = by_id.get(p["pick_id"], {})
            gap_pct = None
            try:
                prev = md.daily_bar(p["ticker"], prev_market_day(d))
                if prev and o.get("day_open"):
                    gap_pct = round((o["day_open"] - prev["close"]) / prev["close"] * 100, 1)
            except Exception:
                pass
            bucket = ("unknown" if gap_pct is None else
                      "<5%" if abs(gap_pct) < 5 else
                      "5-10%" if abs(gap_pct) < 10 else
                      "10-15%" if abs(gap_pct) < 15 else ">15%")
            # r_multiple, not planned R: question_eval._q4 parses this note
            # and every earlier one carries r_multiple (one series, one measure).
            ledger.add_question_progress(con, Q_GAP_SIZE, d, 1,
                                         f"{p['ticker']} gap={gap_pct}% bucket={bucket} R={o.get('r_multiple')}")
            counts["q_increments"] += 1

    # Q5: one increment per session that produced >= 1 scored rejection
    if shadow.get("scored", 0) > 0:
        ledger.add_question_progress(con, Q_SHADOW_FILTER_COST, d, 1,
                                     f"{shadow['scored']} rejections scored, net forgone {shadow['net_forgone_r']:+.2f}R")
        counts["q_increments"] += 1

    # Q6: stop placement — every stopped trade logs whether the target printed later
    # (deterministic counterfactual from the outcome sim; high rate = stops in the noise)
    for p in picks:
        o = by_id.get(p["pick_id"], {})
        if o.get("exit_reason") == "stop":
            ledger.add_question_progress(
                con, Q_STOP_PLACEMENT, d, 1,
                f"{p['ticker']} stopped; target_hit_after_stop={o.get('target_hit_after_stop')}, "
                f"eod_r={o.get('eod_r')}")
            counts["q_increments"] += 1

    # Q7 (indicator layer): does participation (RVOL@10:00 vs ADV20) separate
    # winners from chop? One increment per triggered pick with a non-null
    # rvol_at_1000 -- ledger.stats_by_outcome("rvol_bucket") is the deterministic
    # aggregate the weekly miner reads; this just logs the raw events.
    for p in picks:
        o = by_id.get(p["pick_id"], {})
        if o.get("triggered") and o.get("rvol_at_1000") is not None:
            ledger.add_question_progress(
                con, Q_RVOL_SEPARATION, d, 1,
                f"{p['ticker']} RVOL@10:00={o['rvol_at_1000']:.2f}x R={realized_r(o)}")
            counts["q_increments"] += 1

    # Q8 (indicator layer): VWAP-reclaim longs vs plain dip-buy longs -- same
    # edge or different? Approximated by whether the thesis text itself cites
    # VWAP (the analyst can only do that now that pmVWAP/session VWAP are
    # checkable bundle fields -- see prompts/morning_analyst.md).
    for p in picks:
        o = by_id.get(p["pick_id"], {})
        if (p.get("direction") == "long" and o.get("triggered")
                and "vwap" in str(p.get("thesis", "")).lower()):
            ledger.add_question_progress(
                con, Q_VWAP_REVERSAL_EDGE, d, 1,
                f"{p['ticker']} thesis cited VWAP; side_at_fill={o.get('vwap_side_at_fill')} "
                f"crosses={o.get('vwap_crosses')} R={realized_r(o)}")
            counts["q_increments"] += 1

    # Q9 (indicator layer v2): stop distance sweet spot -- every stopped trade
    # with a non-null stop_dist_atr (the ATR-normalized denominator makes this
    # comparable across tickers, unlike the earlier Q6 raw stop-placement question).
    for p in picks:
        o = by_id.get(p["pick_id"], {})
        if o.get("exit_reason") == "stop" and o.get("stop_dist_atr") is not None:
            ledger.add_question_progress(
                con, Q_STOP_ATR_SWEET_SPOT, d, 1,
                f"{p['ticker']} stop_dist_atr={o['stop_dist_atr']:.2f} R={realized_r(o)}")
            counts["q_increments"] += 1

    # Q10: blue-sky (gap near the 52w high) vs overhead-supply (gap into the
    # 20d range) -- one increment per triggered gap/breakout-class pick.
    for p in picks:
        o = by_id.get(p["pick_id"], {})
        if o.get("triggered") and p.get("setup_type") in ("gap_go", "gap_fade", "breakout"):
            ledger.add_question_progress(
                con, Q_BLUE_SKY_VS_OVERHEAD, d, 1,
                f"{p['ticker']} setup={p.get('setup_type')} R={realized_r(o)}")
            counts["q_increments"] += 1

    # Q11: trend alignment (the 200d question) -- every triggered pick with a
    # non-null trend_side_200d.
    for p in picks:
        o = by_id.get(p["pick_id"], {})
        if o.get("triggered") and o.get("trend_side_200d") is not None:
            ledger.add_question_progress(
                con, Q_TREND_ALIGNMENT_200D, d, 1,
                f"{p['ticker']} {p.get('direction')} trend_side_200d={o['trend_side_200d']} "
                f"R={realized_r(o)}")
            counts["q_increments"] += 1

    # Q12: shortedness -- every triggered squeeze/momentum pick with a
    # non-null short_pct_float.
    for p in picks:
        o = by_id.get(p["pick_id"], {})
        if (o.get("triggered") and o.get("short_pct_float") is not None
                and p.get("setup_type") in ("short_squeeze", "catalyst_momentum")):
            ledger.add_question_progress(
                con, Q_SHORTEDNESS, d, 1,
                f"{p['ticker']} short_pct_float={o['short_pct_float']} R={realized_r(o)}")
            counts["q_increments"] += 1

    # Q13 / Q14: the two arms of the R:R threshold test. Both are written here
    # so the registry's Progress lines are observations the system recorded,
    # not the miner's estimate from re-reading the shadow ledger each week.
    for p in picks:
        o = by_id.get(p["pick_id"], {})
        rr = _planned_rr(p)
        if o.get("triggered") and rr is not None and rr >= 2.0:
            ledger.add_question_progress(
                con, Q_RR_TEST_TAKEN, d, 1,
                f"{p['ticker']} planned R:R={rr:.2f} R={realized_r(o)}")
            counts["q_increments"] += 1

    for ticker, rr, eod_outcome, forgone in shadow.get("rr_band_notes", []):
        ledger.add_question_progress(
            con, Q_RR_TEST_SHADOW, d, 1,
            _rr_band_note(ticker, rr, eod_outcome, forgone))
        counts["q_increments"] += 1

    counts["q_increments"] += _q1_gap_fade_mechanics(con, md, d, picks, outcomes, shadow,
                                                     slippage_bps=slippage_bps)
    return counts


def _fill_valid_rows(picks: list[dict], outcomes: list[dict]) -> list[dict]:
    """Today's triggered, fill-valid trades (both lanes), as merged pick+outcome rows."""
    by_id = {o["pick_id"]: o for o in outcomes}
    rows = []
    for p in picks:
        o = by_id.get(p["pick_id"]) or {}
        if (o.get("triggered") and o.get("exit_reason") != "invalid_fill"
                and not o.get("execution_anomaly")):
            rows.append({**p, **o})
    return rows


def _ground_review(con, d: date, review: dict, picks: list[dict],
                   outcomes: list[dict], corpus: str) -> dict:
    """Mark each pick review grounded or not (GRADER_GROUNDING_SPEC_v1 Phase 2).

    A review that quotes a number or time found nowhere in what the grader was
    shown keeps its letter grade -- that judgment stays with the grader -- but
    is flagged `_ungrounded`, recorded, and can no longer stamp a rule
    confirmation (see _llm_events).
    """
    counts = {"reviews_ungrounded": 0, "lessons_rejected": 0, "updates_rejected": 0}
    for r in review.get("pick_reviews", []) or []:
        bad = ungrounded_tokens(str(r.get("why") or ""), corpus)
        p = _pick_for_review(r, picks)
        if bad:
            r["_ungrounded"] = True
            counts["reviews_ungrounded"] += 1
            ledger.save_grader_rejection(con, d, "review_ungrounded", str(r.get("why") or ""),
                                         ", ".join(bad), ticker=r.get("ticker"))
            log("evening", f"Review of {r.get('ticker', '?')} cites values nothing computed "
                           f"supports ({', '.join(bad)}); grade kept, cannot confirm a rule")
        if p is not None:
            ledger.set_outcome_grounding(con, p["pick_id"], grounded=not bad)
    return counts


def _ground_lessons(con, d: date, new_lessons: list[dict], lesson_updates: list[dict],
                    picks: list[dict], outcomes: list[dict], corpus: str,
                    counts: dict) -> tuple[list[dict], list[dict]]:
    """Filter the grader's lessons and lesson updates down to grounded ones.

    New lessons (Phase 2): every quoted number/time must appear in the input,
    and an optional predicate must be one the system can match. Lesson
    updates (Phase 3): a confirm or contradict is accepted only when at least
    one of today's fill-valid trades fell inside the lesson (its predicate
    cell, or its setup_type/regime). Everything refused is recorded.
    """
    kept_lessons = []
    for l in new_lessons or []:
        text = f"{l.get('lesson', '')} {l.get('evidence', '')}"
        bad = ungrounded_tokens(text, corpus)
        if bad:
            counts["lessons_rejected"] += 1
            ledger.save_grader_rejection(con, d, "lesson_ungrounded", text, ", ".join(bad))
            log("evening", f"Rejected lesson (cites {', '.join(bad)}, found nowhere in the "
                           f"computed input): {str(l.get('lesson', ''))[:80]}")
            continue
        if l.get("predicate") is not None:
            l = dict(l, predicate=validate_lesson_predicate(l.get("predicate")))
        kept_lessons.append(l)

    pick_rows = _fill_valid_rows(picks, outcomes)
    kept_updates = []
    seen = set()
    for upd in lesson_updates or []:
        lid, action = upd.get("lesson_id"), upd.get("action", "")
        if not isinstance(lid, int) or action not in ("confirm", "contradict"):
            continue
        if (lid, action) in seen:
            continue            # the same lesson listed twice is one event
        seen.add((lid, action))
        lesson = ledger.get_lesson(con, lid)
        if lesson is None:
            continue
        ok, why = lesson_in_scope(lesson, pick_rows)
        if not ok:
            counts["updates_rejected"] += 1
            ledger.save_grader_rejection(con, d, "update_out_of_scope",
                                         str(upd.get("note") or ""), why, lesson_id=lid)
            log("evening", f"Rejected {action} of L{lid}: {why}")
            continue
        kept_updates.append(upd)
    return kept_lessons, kept_updates


def _record_coverage(con, d: date, picks: list[dict], outcomes: list[dict]) -> list[dict]:
    """Write this session's grading coverage and return the plumbing rows.

    One row per lane and field: how many rows were graded, how many had the
    field null. Picks are split by lane. Never raises -- measurement must not
    cost the run.
    """
    try:
        by_pick = {o["pick_id"]: o for o in outcomes}
        lanes: dict[str, list[dict]] = {}
        for p in picks:
            o = by_pick.get(p["pick_id"])
            if o is not None:
                lanes.setdefault(lane_of(p), []).append(o)
        cov: list[dict] = []
        for lane, rows in lanes.items():
            cov += outcome_coverage(rows, f"{lane}_picks", OUTCOME_FIELDS, "grade_day")
        ledger.save_data_coverage(con, d, "evening", cov)
        return plumbing(cov)
    except Exception as e:  # noqa: BLE001
        log("evening", f"WARN coverage accounting failed ({e}) -- grading unaffected")
        return []


def _prefetch_grading_bars(con, md: MarketData, d: date, picks: list[dict]) -> None:
    """Warm MarketData's bar memo for every symbol this run will grade.

    The graders below each fetch the same session's bars symbol by symbol --
    picks, the shadow ledger and the paired challenger arm -- and a long
    sequential walk can exceed the 300s serverless budget. One concurrent
    prefetch through the SAME fetch functions makes every later
    call a memo hit returning identical data. Best effort: anything missed is
    fetched on demand exactly as before.
    """
    prefetch = getattr(md, "prefetch_session", None)
    if prefetch is None:
        return

    def _q(sql: str) -> set[str]:
        try:
            return {str(r[0]).upper() for r in con.execute(sql, [d]).fetchall() if r[0]}
        except Exception:
            return set()

    try:
        # Each consumer group gets exactly the bars it reads:
        #   picks      minute + session daily + previous-session daily
        #              (grade_day, stress ladder, grading context, Q4)
        #   arm B      minute + session daily (grade_paired_arms)
        #   shadow     minute only (simulated with daily=None)
        #   population session daily only (grade_population)
        pick_syms = {str(p["ticker"]).upper() for p in picks}
        paired = _q("""SELECT DISTINCT ticker FROM paired_arm_picks
                       WHERE plan_date = ? AND arm <> 'A' AND graded_at IS NULL""") - pick_syms
        minute_only = {str(r["ticker"]).upper() for r in ledger.ungraded_rejections(con, d)}
        minute_only -= pick_syms | paired
        pop = _q("SELECT symbol FROM population_universe WHERE plan_date = ?") \
            - pick_syms - paired - minute_only
        if pick_syms:
            prefetch(sorted(pick_syms), d)
        if paired:
            prefetch(sorted(paired), d, include_prev_daily=False)
        if minute_only:
            prefetch(sorted(minute_only), d, daily=False)
        if pop:
            prefetch(sorted(pop), d, minute=False, include_prev_daily=False)
    except Exception as e:  # noqa: BLE001 -- an optimization must never fail a run
        log("evening", f"WARN bar prefetch failed ({e}) -- grading fetches on demand")


def run(cfg: Config, run_date: date | None = None, force: bool = False) -> None:
    d = run_date or today_et()
    started = now_et().replace(tzinfo=None)
    con = ledger.connect()
    lock = None

    if not is_market_day(d) and not force:
        log("evening", f"{d} is not a market day — skipping")
        ledger.log_run(con, "evening", d, started, "skipped")
        con.close()
        return

    if not cfg.mock:
        lock = ledger.acquire_run_lock(con, d, "evening")
        if not lock["acquired"]:
            log("evening", f"Duplicate trigger suppressed for {d} (lock status={lock['status']})")
            con.close()
            return

    # DATA_CONTINUITY_SPEC_v1: per-stage outcome and elapsed seconds, written
    # to runs.stages on success AND on failure. "Deterministic data collected,
    # grading LLM failed" must never read like "no evening recorded", and a
    # run creeping toward the 300s kill is visible on the runs that finish.
    import time as _time
    t_start = _time.monotonic()
    t_last = [t_start]
    stages: dict = {"deterministic": "pending", "llm": "pending", "timings_s": {}}

    def _mark(name: str) -> None:
        now = _time.monotonic()
        stages["timings_s"][name] = round(now - t_last[0], 1)
        t_last[0] = now

    def _finish_stages() -> dict:
        total = round(_time.monotonic() - t_start, 1)
        stages["timings_s"]["total"] = total
        if total > BUDGET_WARN_S:
            stages["budget_warning"] = True
            log("evening", f"WARN run took {total}s -- over {BUDGET_WARN_S:.0f}s, "
                           f"{BUDGET_WARN_FRACTION:.0%} of the {FUNCTION_BUDGET_S}s function budget")
        return stages

    try:
        plan_row = ledger.plan_for_date(con, d)
        if not plan_row:
            log("evening", f"No plan for {d} — nothing to grade")
            ledger.log_run(con, "evening", d, started, "skipped")
            # Release the lock. An early return that leaves it 'in_progress'
            # wedges the date permanently: acquire_run_lock only re-issues a
            # lock that is retryable, so every later attempt to grade this
            # session is suppressed as a duplicate trigger.
            if lock:
                # 'failed' is the only retryable lock state the schema's CHECK
                # constraint allows, and retryable is what a skip must be --
                # the `runs` table already carries the honest 'skipped' label
                # for humans. Leaving it 'in_progress' wedged 2026-09-21 so
                # that every later attempt to grade that session was
                # suppressed as a duplicate trigger.
                ledger.finish_run_lock(con, d, "evening", lock["request_id"], "failed")
            return
        picks = ledger.picks_for_date(con, d)
        md = MarketData(cfg)
        _prefetch_grading_bars(con, md, d, picks)
        _mark("prefetch")
        slippage = float(cfg.get("slippage_bps", 5))
        flatten_at = cfg.get("flatten_at")
        min_bar_volume = float(cfg.get("min_bar_volume", 0) or 0)

        # 1. deterministic outcome simulation (with friction — see grading.py)
        if picks:
            log("evening", f"Simulating outcomes for {len(picks)} picks against intraday bars "
                           f"(slippage {slippage:.0f} bps/side)...")
            gctx = build_grading_context(md, con, d, [p["ticker"] for p in picks],
                                         cfg, tag="evening")
            adv20_map = gctx["adv20_map"]
            atr_map = gctx["atr_map"]
            atr_pct_map = gctx["atr_pct_map"]
            trend_map = gctx["trend_map"]
            fundamentals_map = gctx["fundamentals_map"]
            prev_close_map = gctx["prev_close_map"]
            outcomes = grade_day(md, picks, d, slippage_bps=slippage,
                                 flatten_at=flatten_at, min_bar_volume=min_bar_volume,
                                 adv20_map=adv20_map, atr_map=atr_map, trend_map=trend_map,
                                 fundamentals_map=fundamentals_map, prev_close_map=prev_close_map,
                                 or_minutes=gctx["or_minutes"])
            for o in outcomes:
                ledger.save_outcome(con, o)

            # v4: friction stress ladder — re-simulate every triggered pick at
            # 2x/3x the configured friction (parallel columns only; r_multiple
            # itself is never recomputed). Reuses the tickers/atr/adv20 already
            # fetched above; a missing bar/daily fetch just skips that pick's
            # stress reading rather than failing the grading run.
            from .. import integrity
            from ..friction import params_from_config
            fparams = params_from_config(cfg)
            by_pick_id = {o["pick_id"]: o for o in outcomes}
            for p in picks:
                o = by_pick_id.get(p["pick_id"])
                if not o or not o.get("triggered"):
                    continue
                t = p["ticker"].upper()
                try:
                    bars = md.minute_bars(p["ticker"], d)
                    daily_bar = md.daily_bar(p["ticker"], d)
                except Exception as e:
                    log("evening", f"WARN friction stress bars fetch failed for {t}: {e}")
                    continue
                sim_fn = simulate_pick
                stress = integrity.stress_ladder_for_pick(
                    p, bars, daily_bar, sim_fn, fparams,
                    atr_pct=atr_pct_map.get(t), adv20=adv20_map.get(t),
                    dollars_per_r=cfg.get("dollars_per_r", 500),
                    flatten_at=flatten_at, min_bar_volume=min_bar_volume)
                if stress:
                    o.update(stress)
                    ledger.save_outcome(con, o)
        else:
            outcomes = []
            live_fills = [o for o in ledger.live_orders_for_date(con, d)
                          if o.get("entry_fill_price") is not None]
            log("evening", "No simulated picks; confirmed no live fills exist."
                if not live_fills else
                f"No simulated picks, but found {len(live_fills)} live fill(s); grading execution evidence.")
        _mark("simulate")

        # v3: shadow ledger — grade what the desk REJECTED (counterfactual only,
        # never touches the taken-trade equity curve)
        log("evening", "Scoring rejected candidates (shadow ledger)...")
        shadow = _grade_rejections(con, md, d, slippage_bps=slippage,
                                   flatten_at=flatten_at, min_bar_volume=min_bar_volume)
        _mark("shadow")

        # v4: population monkeys + challenger arms, graded by the SAME simulator.
        from .. import integrity
        integrity.on_evening(con, cfg, md, d, simulate=simulate_pick)
        _mark("population")

        # GRADER_GROUNDING_SPEC_v1 Phase 1: a COMPUTED tape per pick, from the
        # bars already fetched (memoized). It replaces the "tape narrative" the
        # grader used to be asked for without being shown a single bar, and it
        # is stored on the outcome so the email and dashboard show facts.
        tapes: dict[int, str] = {}
        by_pick = {o["pick_id"]: o for o in outcomes}
        for p in picks:
            o = by_pick.get(p["pick_id"])
            if o is None:
                continue
            try:
                tapes[p["pick_id"]] = tape_summary(md.minute_bars(p["ticker"], d), o)
                if not cfg.mock:
                    ledger.set_outcome_grounding(con, p["pick_id"],
                                                 tape_summary=tapes[p["pick_id"]])
            except Exception as e:  # noqa: BLE001
                log("evening", f"WARN tape summary for {p['ticker']} failed: {e}")

        # DATA_CONTINUITY_SPEC_v1 Phase 3: per-field coverage, and the
        # plumbing rule -- a field null on EVERY graded row is the pipe, not
        # the market. Recorded before the LLM so an outage still leaves it.
        plumbing_bad = _record_coverage(con, d, picks, outcomes)
        if plumbing_bad:
            stages["plumbing"] = describe_plumbing(plumbing_bad)
            log("evening", f"DATA PLUMBING: {stages['plumbing']} -- this session's grading "
                           "is logged as degraded, not as a normal day")

        # Phase 1: every deterministic event (calibration, open-question
        # progress) BEFORE the grader call, so a provider outage cannot cost
        # them. Mock runs keep their existing contract: nothing past the
        # simulated outcomes is persisted.
        counts = {"calibration": 0, "confirmations": 0, "q_increments": 0}
        forward = {"rules": 0, "recorded": 0}
        if not cfg.mock:
            counts = _deterministic_events(con, md, d, picks, outcomes, shadow,
                                           slippage_bps=slippage)
            # FORWARD_VALIDATION_SPEC_v1 Phase 3: the first caller the
            # quarantine's forward-only guard has ever had. Every fill-valid
            # row from today that falls in a quarantined (or validated) rule's
            # cell is one forward observation; idempotent on source_key.
            try:
                from .. import research
                forward = research.observe_session(con, d)
                if forward["recorded"]:
                    log("evening", f"Forward observations: {forward['recorded']} recorded "
                                   f"against {forward['rules']} active rule(s)")
            except Exception as e:  # noqa: BLE001 -- measurement never breaks grading
                log("evening", f"WARN forward observation pass failed: {e}")
            # EVIDENCE_BACKLOG_SPEC_v1 Phase 1: what evidence the desk is
            # short of, as counts, for tomorrow morning's analyst.
            try:
                from .. import backlog
                n_items = len(backlog.compute(con, d))
                stages["backlog_items"] = n_items
            except Exception as e:  # noqa: BLE001
                log("evening", f"WARN evidence backlog failed: {e}")
        stages["deterministic"] = "complete"
        stages["forward_observations"] = forward["recorded"]
        _mark("events")

        # BROKER TRUTH before anyone reads the day (docs/FIX_PHASE0.md, D3): on
        # 2026-10-15 the review said -1.56R and one winner while the paper
        # account lost $222 with no winner, because the winner (RKLB) was an
        # order Alpaca cancelled unfilled and nothing compared the two. Guarded:
        # a broker that cannot be read leaves the review on simulated outcomes.
        live_summary: dict = {"reconciled": 0, "divergence": []}
        broker_rows: list[dict] = []
        if not cfg.mock:
            try:
                from ..live_monitor import reconcile_evening
                live_summary = reconcile_evening(con, cfg, d)
                broker_rows = _record_broker_truth(con, d, picks, outcomes)
            except Exception as e:  # noqa: BLE001
                log("evening", f"WARN broker reconciliation failed; the review stays on "
                               f"simulated outcomes: {e}")
        stages["broker_divergences"] = sum(1 for r in broker_rows
                                           if r["divergence"] not in (None, "none"))
        out_md = outcomes_md(picks, outcomes) if picks else "(no picks today — no-trade day)"
        broker_line = broker_summary_line(picks, outcomes) if picks else ""
        if broker_line:
            out_md = broker_line + "\n\n" + out_md
        tape_md = "\n".join(f"- {p['ticker']} (pick_id={p['pick_id']}): {tapes[p['pick_id']]}"
                            for p in picks if p["pick_id"] in tapes)
        # How each entry order works and why it filled when it did, computed from the
        # plan and the simulated outcome (docs/FIX_PHASE0.md, D6): on 2026-10-16 the
        # review called AMAT's breakout fill "97 minutes late ... chasing" and INTC's
        # breakout short senseless, because nothing told it a breakout is a stop order
        # that fills when the level breaks.
        from .. import review_audit
        mech_md = review_audit.mechanics_md(picks, outcomes) if picks else ""
        recap = md.market_recap(d)
        recap_md = ", ".join(f"{k} {v:+.2f}%" for k, v in (recap.get("pct_change") or {}).items())

        # 2. LLM process-grading + lesson extraction
        lessons_now = ledger.active_lessons(con, 40)
        lessons_md = "\n".join(
            f"- [L{l['lesson_id']}] ({l['confidence']}, +{l['confirmations']}/-{l['contradictions']}) {l['lesson']}"
            for l in lessons_now
        ) or "(lesson bank empty)"

        # TRADER_HYPOTHESIS_SPEC_v1: the grader reviews each tagged trade and
        # may write an anecdote down as an idea to test. Off, every
        # placeholder renders as nothing and the prompt is unchanged.
        from .. import hypotheses
        hyp_on = hypotheses.enabled(cfg)
        hyp_md = ""
        if hyp_on:
            try:
                hyp_md = hypotheses.grader_block(con, picks)
            except Exception as e:  # noqa: BLE001
                log("evening", f"WARN hypothesis block unavailable: {e}")
        system = load_prompt("evening_grader", {"PERSONA": persona_block(cfg, "grader"),
                                                **hypotheses.grader_prompt_text(cfg)})
        shadow_md = _shadow_md(shadow)
        user = (
            f"# GRADING DATE: {d.isoformat()}\n\n"
            f"## THE MORNING PLAN\n\n"
            + (f"Market stance: {plan_row['plan'].get('market_stance','')}\n"
               f"No-trade: {plan_row['no_trade']}"
               + (f" — reason: {plan_row['plan'].get('no_trade_reason','')}"
                  if plan_row["no_trade"] else "")
               + f"\nNotes for evening from the risk manager: "
                 f"{plan_row.get('notes_for_evening','')}\n\n")
            + f"## PICKS + COMPUTED OUTCOMES (mechanically simulated from real bars)\n\n{out_md}\n\n"
            + (f"## COMPUTED TAPE (from the real bars — the ONLY tape facts you may cite)\n\n"
               f"{tape_md}\n\n" if tape_md else "")
            + (f"## ORDER MECHANICS (computed from the plan: how each entry order works and why it "
               f"filled when it did. A resting or stop order fills when price reaches it; the fill "
               f"time is the tape's, never a desk decision)\n\n{mech_md}\n\n" if mech_md else "")
            + f"## EXECUTION GROUND TRUTH\n\n"
            f"Live fills recorded for {d}: "
            f"{sum(1 for o in ledger.live_orders_for_date(con, d) if o.get('entry_fill_price') is not None)}. "
            "Build the narrative from fills when present; only call this a stand-down after confirming zero fills.\n\n"
            f"## SHADOW LEDGER TODAY (rejected candidates — context only, do not grade them)\n\n{shadow_md}\n\n"
            + f"## MARKET RECAP TODAY\n\n{recap_md or '(recap unavailable)'}\n\n"
            + f"## ACTIVE LESSONS (for confirm/contradict updates — use these IDs)\n\n{lessons_md}\n\n"
            + (f"{hyp_md}\n\n" if hyp_md else "")
            + "Grade the day now, following the output contract exactly."
        )

        model = cfg.models["evening"]
        log("evening", f"Grading call ({model})...")
        llm = LLMClient(cfg)
        try:
            result = llm.call(system=system, user=user, model=model,
                              mock_name="evening_grader", thinking=False, web_search=False)
        except Exception:
            stages["llm"] = "failed"
            raise
        stages["llm"] = "complete"
        # Which grader prompt wrote this review (the order-mechanics paragraph is v11):
        # scripts/verify_fixes.py reads it back, since `runs` keeps no prompt version.
        stages["grader_prompt_version"] = prompt_version("evening_grader")
        _mark("llm")
        review = result.parsed or {}
        review = _drop_invalid_fill_reviews(review, picks, outcomes)

        if cfg.mock:
            # mocks/evening_grader.json is a canned fixture, not real analysis of
            # today's picks -- persisting it (grades, lessons, rule confirmations)
            # would write fabricated process narrative onto real trade rows. This
            # is exactly how canned pick_reviews/lessons text ended up verbatim in
            # production for a prior session's NVDA/CVNA/AMD day. The deterministic
            # outcomes above (real market data, no LLM involved) are already saved
            # above and stay saved -- that's the legitimate, cost-free value mock
            # mode is for. Nothing LLM-derived past this point is persisted, and no
            # email goes out describing a day that didn't really get graded.
            log("evening", "MOCK RUN — deterministic outcomes saved; grades, lessons, and "
                           "rule confirmations NOT persisted (canned LLM response); no email sent")
            ledger.log_run(con, "evening", d, started, "mock", model, result.usage)
            return

        # VERIFICATION_PRACTICE_v1: the grader's claims checked against the
        # computed numbers BEFORE any of it is stored. A lesson or idea the
        # numbers contradict is dropped; a pick review or the plan notes keep
        # the grader's words with an [AUDIT: ...] note beside them.
        from .. import review_audit
        audit = review_audit.audit(review, picks, outcomes)
        review = audit["review"]
        stages["audit"] = {
            "findings": len(audit["findings"]),
            # counts first: `runs.stages` is cut at 4000 characters and the items below are long
            "by_check": dict(Counter(f["check"] for f in audit["findings"])),
            "dropped_lessons": audit["dropped_lessons"],
            "dropped_ideas": audit["dropped_ideas"],
            "items": [{k: str(v)[:300] for k, v in f.items()} for f in audit["findings"][:20]]}
        for f in audit["findings"]:
            log("evening", f"AUDIT [{f['check']}] {f['where']} {f.get('ticker') or ''}: "
                           f"{f['why'][:200]} ({f['action']})")

        # 3. persist grades + lessons
        n_graded = ledger.apply_grades(con, review.get("pick_reviews", []), picks)

        # (Live paper fills were reconciled above, before the grader read the
        # day: the broker's result is the headline, the simulator's R intact.)

        tainted_day = _all_fills_tainted(outcomes)
        new_lessons = review.get("lessons", []) or []
        lesson_updates = review.get("lesson_updates", []) or []
        if tainted_day and (new_lessons or lesson_updates):
            log("evening", f"Skipping {len(new_lessons)} new lesson(s) and "
                           f"{len(lesson_updates)} lesson update(s) -- every pick with fill "
                           f"activity today was invalid_fill or execution_anomaly; nothing "
                           f"clean today to draw or confirm a thesis lesson from")
            new_lessons, lesson_updates = [], []
        # A session whose picks had no intraday bars at all was not observed:
        # any lesson drawn from it is narrative about data that never arrived.
        pick_lanes = {f"{lane_of(p)}_picks" for p in picks}
        if any(c["field"] == "minute_bars" and c["lane"] in pick_lanes
               for c in plumbing_bad) and (new_lessons or lesson_updates):
            log("evening", f"Skipping {len(new_lessons)} new lesson(s) and "
                           f"{len(lesson_updates)} lesson update(s) -- the picks' intraday "
                           f"bars never arrived (plumbing), so nothing was observed to learn from")
            new_lessons, lesson_updates = [], []
        # GRADER_GROUNDING_SPEC_v1: nothing the grader asserts becomes memory
        # unless something computed supports it. `corpus` is exactly what it
        # was shown; a number it quotes (R, ATR, x, %) or a clock time it
        # cites must appear there.
        grounding = _ground_review(con, d, review, picks, outcomes,
                                   corpus=f"{system}\n{user}")
        new_lessons, lesson_updates = _ground_lessons(
            con, d, new_lessons, lesson_updates, picks, outcomes,
            corpus=f"{system}\n{user}", counts=grounding)

        for l in new_lessons:
            dup = ledger.find_duplicate_lesson(con, l.get("lesson", ""))
            if dup:
                log("evening", f"Skipped duplicate lesson (matches L{dup}): "
                               f"{l.get('lesson', '')[:80]}")
                continue
            lid = ledger.insert_lesson(con, l, "evening", d)
            log("evening", f"New lesson L{lid}: {l['lesson']}")
        for upd in lesson_updates:
            ledger.update_lesson_feedback(con, upd["lesson_id"], upd.get("action", ""), d)

        # v3: rule confirmations -- the one v3 event that needs the grader.
        # (Calibration and open-question progress were written before the call.)
        counts["confirmations"] = _llm_events(con, d, picks, outcomes, review)

        # TRADER_HYPOTHESIS_SPEC_v1: the grader's per-trade notes and the
        # ideas it wrote down. Guarded: the notebook never costs the evening.
        hyp_summary: dict = {}
        if hyp_on:
            try:
                hyp_summary = hypotheses.record_evening(con, cfg, d, review, picks,
                                                        corpus=f"{system}\n{user}")
                stages["hypotheses"] = {k: (len(v) if isinstance(v, list) else v)
                                        for k, v in hyp_summary.items()}
            except Exception as e:  # noqa: BLE001
                log("evening", f"WARN hypothesis notebook update failed: {e}")
        _mark("persist")

        ledger.log_run(con, "evening", d, started, "degraded" if plumbing_bad else "ok",
                       model, result.usage, stages=_finish_stages(),
                       persona_version=persona_version(cfg))

        # 4. console + delivery — the nightly eyeball summary (spec §5)
        log("evening", f"Plan grade: {review.get('plan_grade','?')} — {review.get('plan_notes','')}")
        for r in review.get("pick_reviews", []):
            log("evening", f"  {r.get('ticker','?')}: {r.get('grade','?')} — {r.get('why','')}")
        log("evening", "RUN SUMMARY: "
                       f"picks graded={n_graded}, rejections scored={shadow['scored']}, "
                       f"net forgone today={shadow['net_forgone_r']:+.2f}R, "
                       f"calibration rows={counts['calibration']}, "
                       f"rule confirmations={counts['confirmations']}, "
                       f"open-question increments={counts['q_increments']}, "
                       f"forward observations={forward['recorded']}, "
                       f"live reconciled={live_summary['reconciled']}, "
                       f"sim/broker divergences={stages['broker_divergences']}, "
                       f"lessons extracted={len(new_lessons)}, "
                       f"lessons rejected (ungrounded)={grounding['lessons_rejected']}, "
                       f"lesson updates rejected (out of scope)={grounding['updates_rejected']}, "
                       f"ungrounded reviews={grounding['reviews_ungrounded']}, "
                       f"cost ~${result.usage['cost_usd']:.3f}")

        # EVIDENCE_BACKLOG_SPEC_v1 Phase 4: the daily diagnostics digest --
        # what was collected and what broke, never what worked.
        digest_txt = ""
        try:
            from .. import backlog
            digest = backlog.daily_digest(con, d, {
                "forward_observations": forward["recorded"],
                "plumbing": stages.get("plumbing"),
                "lessons_rejected": grounding["lessons_rejected"],
                "updates_rejected": grounding["updates_rejected"]})
            digest_txt = backlog.digest_text(digest)
        except Exception as e:  # noqa: BLE001
            log("evening", f"WARN daily diagnostics digest failed: {e}")

        # TRADERS_LOG_SPEC_v1: the owner's first-person log, written after
        # everything above is stored, and the day's winner/loser counts. Off by
        # default; guarded and bounded by what is left of the function budget,
        # so it can never cost the evening its grading, playbook or email.
        email_log: dict = {}
        if (cfg.get("traders_log", {}) or {}).get("enabled"):
            try:
                from .. import traders_log
                reserve = float((cfg.get("traders_log", {}) or {}).get("reserve_s") or 45)
                email_log["traders_log"] = traders_log.generate(
                    con, cfg, d, deadline=t_start + FUNCTION_BUDGET_S - reserve)
                email_log["day_contrast"] = traders_log.day_contrast(con, d)
                stages["traders_log"] = email_log["traders_log"].get("status")
            except Exception as e:  # noqa: BLE001 -- the log never breaks the evening
                log("evening", f"WARN trader's log failed; the evening is unaffected: {e}")

        hyp_txt = ""
        if hyp_on:
            try:
                hyp_txt = hypotheses.evening_text(con, d, hyp_summary)
            except Exception as e:  # noqa: BLE001
                log("evening", f"WARN hypothesis summary failed: {e}")
        # EDGE_ACCELERATION_SPEC_v1 Part 3: the agent asks its own production
        # data the questions every spec's Phase 0 used to ask a person. Cheap
        # SQL, guarded; it never changes trading and never breaks the evening.
        check_txt = ""
        if not cfg.mock:
            try:
                from .. import selfcheck
                checks = selfcheck.run(con, cfg, d)
                check_txt = selfcheck.text(checks)
                stages["selfcheck"] = max(
                    (c["status"] for c in checks),
                    key=lambda s: {"bad": 3, "warn": 2, "info": 1}.get(s, 0), default="ok")
            except Exception as e:  # noqa: BLE001
                log("evening", f"WARN self-check failed; the evening is unaffected: {e}")
        audit_txt = review_audit.text(audit)
        send_email(cfg, f"Trade Agent — Evening Review {d.isoformat()}",
                   evening_email_html(d, review, out_md + (
                       f"\n\nCOMPUTED TAPE\n{tape_md}" if tape_md else "") + (
                       f"\n\n{digest_txt}" if digest_txt else "") + (
                       f"\n\n{hyp_txt}" if hyp_txt else "") + (
                       f"\n\n{audit_txt}" if audit_txt else "") + (
                       f"\n\n{check_txt}" if check_txt else ""), **email_log))
        render_dashboard(con, cfg, d)
        ledger.finish_run_lock(con, d, "evening", lock["request_id"], "complete")

    except Exception as e:
        # str(e) alone loses the stack, and a bare "error" status cannot tell a
        # provider outage from a defect in this code -- both were true of the
        # 2026-09-22 failures. See agent/failures.py.
        status = classify_failure(e)
        detail = format_failure(e)
        if lock:
            ledger.finish_run_lock(con, d, "evening", lock["request_id"], "failed")
        ledger.log_run(con, "evening", d, started, status, error=detail,
                       stages=_finish_stages())
        log("evening", f"ERROR: {e}")
        from ..failures import alert_provider_outage
        alert_provider_outage(con, cfg, "evening", d, e)
        raise
    finally:
        con.close()
