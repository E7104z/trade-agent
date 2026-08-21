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
from datetime import date

from .. import ledger
from ..config import Config
from ..delivery import evening_email_html, render_dashboard, send_email
from ..grading import classify_shadow, grade_day, outcomes_md, simulate_pick
from ..llm import LLMClient, load_prompt, prompt_version
from ..providers import MarketData
from ..util import is_market_day, log, now_et, prev_market_day, today_et

# Open-question registry (ids match playbook.md "Open Questions" numbering)
Q_GAP_FADE_MECHANICS = 1
Q_SYMPATHY_STANDALONE = 2
Q_CONVICTION = 3
Q_GAP_SIZE = 4
Q_SHADOW_FILTER_COST = 5
Q_STOP_PLACEMENT = 6


def _grade_rejections(con, md: MarketData, d: date, slippage_bps: float = 0.0,
                      flatten_at: str | None = None, min_bar_volume: float = 0) -> dict:
    """Shadow ledger: simulate every rejected candidate; never touches the equity curve.

    Uses the same friction and exit rules (flatten_at, min_bar_volume) as taken
    picks -- comparisons between taken and rejected are only fair if both sides
    are graded identically. Rejections don't carry a time_stop (not stored on
    the rejections table), which behaves the same as a pick with no time_stop.
    """
    rejections = ledger.ungraded_rejections(con, d)
    summary = {"scored": 0, "net_forgone_r": 0.0, "gap_fade_notes": []}
    for r in rejections:
        try:
            bars = md.minute_bars(r["ticker"], d)
        except Exception:
            bars = []
        shim = {"pick_id": r["rejection_id"], "direction": r["direction"],
                "entry_type": r["entry_type"], "entry_price": r["entry_price"],
                "stop_price": r["stop_price"], "target_price": r["target_price"]}
        sim = simulate_pick(shim, bars, None, slippage_bps=slippage_bps,
                            flatten_at=flatten_at, min_bar_volume=min_bar_volume)
        eod_outcome, forgone = classify_shadow(sim)
        ledger.save_rejection_outcome(con, r["rejection_id"], eod_outcome, forgone,
                                      sim_version=sim.get("sim_version"))
        summary["scored"] += 1
        summary["net_forgone_r"] += forgone or 0.0
        log("evening", f"  shadow {r['ticker']} ({r['reason'][:40]}): {eod_outcome}"
                       + (f" {forgone:+.2f}R" if forgone is not None else ""))
        if r.get("setup_type") == "gap_fade":
            summary["gap_fade_notes"].append((r, sim, bars))
    summary["net_forgone_r"] = round(summary["net_forgone_r"], 2)
    return summary


def _q1_gap_fade_mechanics(con, md: MarketData, d: date, picks: list[dict],
                           outcomes: list[dict], shadow: dict,
                           slippage_bps: float = 0.0) -> int:
    """Q1: on every gap_fade candidate, compare planned entry vs open-print entry."""
    n = 0
    candidates: list[tuple[dict, list]] = []
    for p in picks:
        if p.get("setup_type") == "gap_fade":
            try:
                candidates.append((p, md.minute_bars(p["ticker"], d)))
            except Exception:
                continue
    for r, _sim, bars in shadow.get("gap_fade_notes", []):
        candidates.append((r, bars))
    for c, bars in candidates:
        if not bars:
            continue
        planned = simulate_pick({"pick_id": 0, "direction": c["direction"],
                                 "entry_type": c.get("entry_type", "limit"),
                                 "entry_price": c["entry_price"], "stop_price": c["stop_price"],
                                 "target_price": c["target_price"]}, bars, None,
                                slippage_bps=slippage_bps)
        open_sim = simulate_pick({"pick_id": 0, "direction": c["direction"],
                                  "entry_type": "open", "entry_price": bars[0]["o"],
                                  "stop_price": c["stop_price"],
                                  "target_price": c["target_price"]}, bars, None,
                                 slippage_bps=slippage_bps)
        note = (f"{c['ticker']}: planned-entry R="
                f"{planned.get('r_multiple') if planned.get('triggered') else 'untriggered'}, "
                f"open-print R={open_sim.get('r_multiple')}")
        ledger.add_question_progress(con, Q_GAP_FADE_MECHANICS, d, 1, note)
        n += 1
    return n


def _v3_events(con, md: MarketData, d: date, picks: list[dict], outcomes: list[dict],
               review: dict, shadow: dict, slippage_bps: float = 0.0) -> dict:
    """Calibration rows, rule confirmations, open-question progress. DB writes only."""
    counts = {"calibration": 0, "confirmations": 0, "q_increments": 0}
    by_id = {o["pick_id"]: o for o in outcomes}

    # calibration rows (taken picks only; conviction as stated in the morning, never adjusted)
    for p in picks:
        o = by_id.get(p["pick_id"], {})
        realized = o.get("r_multiple") if o.get("triggered") else None
        ledger.add_calibration_row(con, d, p["ticker"], p.get("conviction"), realized)
        counts["calibration"] += 1
        if realized is not None:  # Q3 increments with every realized calibration row
            ledger.add_question_progress(con, Q_CONVICTION, d, 1,
                                         f"{p['ticker']} conv={p.get('conviction')} R={realized:+.2f}")
            counts["q_increments"] += 1

    # rule confirmations: process-pass (A/B) confirms the setup's rule — regardless of P&L.
    # A process-FAIL does not confirm a rule even if it made money. An
    # execution_anomaly pick never confirms a rule either, no matter what grade
    # the LLM assigned — the fill landed far enough from the planned entry that
    # the thesis at the intended level was never actually tested (enforced here
    # in code rather than trusted to prompt compliance; see grading.py).
    graded = {}
    tick_to_pick = {p["ticker"].upper(): p for p in picks}
    for r in review.get("pick_reviews", []) or []:
        p = None
        if isinstance(r.get("pick_id"), int):
            p = next((x for x in picks if x["pick_id"] == r["pick_id"]), None)
        if p is None:
            p = tick_to_pick.get(str(r.get("ticker", "")).upper())
        o = by_id.get(p["pick_id"], {}) if p else {}
        if (p and str(r.get("grade", "")).upper() in ("A", "B") and p.get("setup_type")
                and not o.get("execution_anomaly")):
            ledger.add_rule_confirmation(con, p["setup_type"], d, p["pick_id"])
            counts["confirmations"] += 1

    # Q2: sympathy standalone — only when the sympathy pick is the day's sole pick
    # (approximation of "leader not concurrently held"; noted for the miner)
    for p in picks:
        if p.get("setup_type") == "sympathy":
            o = by_id.get(p["pick_id"], {})
            if len(picks) == 1:
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

    counts["q_increments"] += _q1_gap_fade_mechanics(con, md, d, picks, outcomes, shadow,
                                                     slippage_bps=slippage_bps)
    return counts


def run(cfg: Config, run_date: date | None = None, force: bool = False) -> None:
    d = run_date or today_et()
    started = now_et().replace(tzinfo=None)
    con = ledger.connect()

    if not is_market_day(d) and not force:
        log("evening", f"{d} is not a market day — skipping")
        ledger.log_run(con, "evening", d, started, "skipped")
        con.close()
        return

    try:
        plan_row = ledger.plan_for_date(con, d)
        if not plan_row:
            log("evening", f"No plan found for {d} — nothing to grade")
            ledger.log_run(con, "evening", d, started, "skipped")
            return

        picks = ledger.picks_for_date(con, d)
        md = MarketData(cfg)
        slippage = float(cfg.get("slippage_bps", 5))
        flatten_at = cfg.get("flatten_at")
        min_bar_volume = float(cfg.get("min_bar_volume", 0) or 0)

        # 1. deterministic outcome simulation (with friction — see grading.py)
        if picks:
            log("evening", f"Simulating outcomes for {len(picks)} picks against intraday bars "
                           f"(slippage {slippage:.0f} bps/side)...")
            outcomes = grade_day(md, picks, d, slippage_bps=slippage,
                                 flatten_at=flatten_at, min_bar_volume=min_bar_volume)
            for o in outcomes:
                ledger.save_outcome(con, o)
        else:
            outcomes = []
            log("evening", "No-trade day — grading the decision to stand down.")

        # v3: shadow ledger — grade what the desk REJECTED (counterfactual only,
        # never touches the taken-trade equity curve)
        log("evening", "Scoring rejected candidates (shadow ledger)...")
        shadow = _grade_rejections(con, md, d, slippage_bps=slippage,
                                   flatten_at=flatten_at, min_bar_volume=min_bar_volume)

        out_md = outcomes_md(picks, outcomes) if picks else "(no picks today — no-trade day)"
        recap = md.market_recap(d)
        recap_md = ", ".join(f"{k} {v:+.2f}%" for k, v in (recap.get("pct_change") or {}).items())

        # 2. LLM process-grading + lesson extraction
        lessons_now = ledger.active_lessons(con, 40)
        lessons_md = "\n".join(
            f"- [L{l['lesson_id']}] ({l['confidence']}, +{l['confirmations']}/-{l['contradictions']}) {l['lesson']}"
            for l in lessons_now
        ) or "(lesson bank empty)"

        system = load_prompt("evening_grader", {})
        shadow_md = (f"{shadow['scored']} rejected candidates scored; net forgone "
                     f"{shadow['net_forgone_r']:+.2f}R today (positive = filters cost us)")
        user = (
            f"# GRADING DATE: {d.isoformat()}\n\n"
            f"## THE MORNING PLAN\n\n"
            f"Market stance: {plan_row['plan'].get('market_stance','')}\n"
            f"No-trade: {plan_row['no_trade']}"
            + (f" — reason: {plan_row['plan'].get('no_trade_reason','')}" if plan_row["no_trade"] else "")
            + f"\nNotes for evening from the risk manager: {plan_row.get('notes_for_evening','')}\n\n"
            f"## PICKS + COMPUTED OUTCOMES (mechanically simulated from real bars)\n\n{out_md}\n\n"
            f"## SHADOW LEDGER TODAY (rejected candidates — context only, do not grade them)\n\n{shadow_md}\n\n"
            f"## MARKET RECAP TODAY\n\n{recap_md or '(recap unavailable)'}\n\n"
            f"## ACTIVE LESSONS (for confirm/contradict updates — use these IDs)\n\n{lessons_md}\n\n"
            "Grade the day now, following the output contract exactly."
        )

        model = cfg.models["evening"]
        log("evening", f"Grading call ({model})...")
        llm = LLMClient(cfg)
        result = llm.call(system=system, user=user, model=model,
                          mock_name="evening_grader", thinking=False, web_search=False)
        review = result.parsed or {}

        # 3. persist grades + lessons
        n_graded = ledger.apply_grades(con, review.get("pick_reviews", []), picks)
        new_lessons = review.get("lessons", []) or []
        for l in new_lessons:
            lid = ledger.insert_lesson(con, l, "evening", d)
            log("evening", f"New lesson L{lid}: {l['lesson']}")
        for upd in review.get("lesson_updates", []) or []:
            if isinstance(upd.get("lesson_id"), int):
                ledger.update_lesson_feedback(con, upd["lesson_id"], upd.get("action", ""), d)

        # v3: calibration rows, rule confirmations, open-question progress
        counts = _v3_events(con, md, d, picks, outcomes, review, shadow, slippage_bps=slippage)

        ledger.log_run(con, "evening", d, started, "ok", model, result.usage)

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
                       f"lessons extracted={len(new_lessons)}, "
                       f"cost ~${result.usage['cost_usd']:.3f}")

        send_email(cfg, f"Trade Agent — Evening Review {d.isoformat()}",
                   evening_email_html(d, review, out_md))
        render_dashboard(con, cfg, d)

    except Exception as e:
        ledger.log_run(con, "evening", d, started, "error", error=str(e))
        log("evening", f"ERROR: {e}")
        raise
    finally:
        con.close()
