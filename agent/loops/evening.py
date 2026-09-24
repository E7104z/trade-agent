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
from datetime import date, datetime

from .. import ledger
from ..config import Config
from ..delivery import evening_email_html, render_dashboard, send_email
from ..grading import (CURRENT_SIM_VERSION, classify_shadow, grade_day,
                       outcomes_md, simulate_pick)
from ..grading_context import build_grading_context
from ..llm import LLMClient, load_prompt, prompt_version
from ..failures import classify_failure, format_failure
from ..providers import MarketData
from ..util import is_market_day, log, now_et, prev_market_day, today_et

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


def _runner_grader_block(con, d: date) -> str:
    """The full runner candidate list, with the selected rows marked.

    Spec 2.6: the grader is asked whether the SELECTION was defensible at
    decision time, which is unanswerable from the picks alone. A grader that
    cannot see what was passed over has to reconstruct the decision from a
    fragment - and that is the exact condition under which it previously
    invented a narrative around a trade that never validly existed.
    """
    candidates = ledger.runner_candidates_for_date(con, d)
    if not candidates:
        return ""
    outcomes = {o["ticker"]: o for o in ledger.runner_outcomes_for_date(con, d)}
    session = ledger.runner_session(con, d) or {}

    def _n(v, dg=2):
        return "" if v is None else f"{v:.{dg}f}"

    lines = ["| sel | rank | ticker | det ET | price | rvol | vwap% | tc | stop_dist | stop_atr | 200d | R | exit |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    n_sel = 0
    for c in candidates:
        o = outcomes.get(c["ticker"]) or {}
        sel = bool(o.get("selected"))
        n_sel += 1 if sel else 0
        fp = c.get("first_pass_et")
        # A fill that landed on the wrong side of its own stop is not a trade.
        # Mask its R so no lesson is ever extracted from a structurally
        # impossible outcome - the existing fill-validity hard gate, applied
        # here before any runner row can reach lesson extraction.
        invalid = o.get("exit_reason") == "invalid_fill"
        r_txt = "INVALID FILL" if invalid else _n(o.get("r_multiple"))
        lines.append(
            f"| {'YES' if sel else ''} | {o.get('selection_rank') or ''} | {c['ticker']} | "
            f"{fp.strftime('%H:%M') if isinstance(fp, datetime) else ''} | "
            f"{_n(c.get('detection_price'))} | {_n(c.get('rvol'))} | "
            f"{_n(c.get('vwap_pct_above'))} | {_n(c.get('trend_consistency'), 1)} | "
            f"{_n(c.get('stop_dist'))} | {_n(o.get('stop_dist_atr'))} | "
            f"{o.get('trend_side_200d') or ''} | {r_txt} | {o.get('exit_reason') or ''} |")

    skipped = session.get("selection_skipped_reason")
    header = (f"{len(candidates)} candidate(s) detected, {n_sel} selected."
              + (f" Selection note: {skipped}." if skipped else ""))
    body = chr(10).join(lines)
    return ("## RUNNER LANE - FULL CANDIDATE LIST (selected rows marked)" + chr(10) * 2
            + header + chr(10) * 2 + body + chr(10) * 2
            + "Every row was priced by the same simulator, selected or not. Judge "
              "whether the SELECTION was defensible on what was knowable at "
              "decision time - not on which row happened to win.")


def _grade_runner_candidates(con, md: MarketData, d: date, cfg: Config) -> dict:
    """Price EVERY runner candidate, traded or not (RUNNER_PORT_SPEC 1.5).

    This is the free evidence engine. Simulating 40 candidates instead of 10
    costs bar fetches, not tokens, and it is the only way to get a denominator
    for "did selection help?" - you cannot measure a picker against the field
    if the field was never priced.

    Entry is the OPEN OF THE BAR FOLLOWING first_pass_et, never the detection
    bar's close. Entering at the close of the bar that triggered detection is a
    look-ahead fill: the condition check reads that bar's close, so filling
    there buys at a price the rule needed in order to fire.

    Implementation note on how that entry is expressed: simulate_pick with
    entry_type="open" fills at bars[0]["o"], so the minute-bar array is SLICED
    to begin at the first bar strictly after first_pass_et and handed over. The
    persisted columns (triggered/fill/exit/R/MFE/MAE/EOD) are all post-entry
    quantities, so the slice is exactly the right window for them. Session-wide
    indicator fields that simulate_pick also computes would be wrong on a
    sliced array - none of them are read or stored here.
    """
    candidates = ledger.runner_candidates_for_date(con, d)
    summary = {"scored": 0, "triggered": 0, "sum_r": 0.0, "no_bars": 0, "skipped": 0}
    if not candidates:
        return summary
    selection_at = (ledger.runner_session(con, d) or {}).get("selection_at")

    slippage = float(cfg.get("slippage_bps", 0) or 0)
    flatten_at = cfg.get("flatten_at")
    min_bar_volume = float(cfg.get("min_bar_volume", 0) or 0)

    # Indicator layer v2 for the WHOLE runner pool, not just the picks. One
    # batched daily_context fetch (the same the morning grader uses) yields
    # ATR14 and the 200d trend snapshot, so stop_dist_atr and trend_side_200d
    # can be stored per candidate and the weekly miner can test them against
    # selected-vs-unselected the same way it already tests the morning lane.
    atr_map: dict[str, float] = {}
    trend_map: dict[str, dict] = {}
    try:
        ctx = md.daily_context([c["ticker"] for c in candidates])
        atr_map = {t: x["atr14"] for t, x in ctx.items() if x.get("atr14")}
        trend_map = {t: x["trend_snapshot"] for t, x in ctx.items()
                     if x.get("trend_snapshot")}
    except Exception as e:
        log("evening", f"  runner daily_context fetch failed ({e}) - ATR/trend null")

    def _stop_dist_atr(c: dict, atr14: float | None) -> float | None:
        stop_dist = c.get("stop_dist")
        if not stop_dist or not atr14:
            return None
        return round(stop_dist / atr14, 2)

    def _trend_side(snapshot: dict | None) -> str | None:
        if not snapshot:
            return None
        above = snapshot.get("above_ma200")
        return "above" if above is True else ("below" if above is False else None)

    for c in candidates:
        ticker = c["ticker"]
        try:
            bars = md.minute_bars(ticker, d)
        except Exception as e:
            log("evening", f"  runner {ticker}: bar fetch failed ({e})")
            bars = []

        first_pass = c.get("first_pass_et")
        entry_bars = bars
        cutoff = None
        if bars and first_pass is not None:
            fp = first_pass if isinstance(first_pass, datetime) else None
            if fp is not None:
                # Compare on naive ET wall-clock: first_pass_et is stored
                # without a tzinfo, while provider bars carry one.
                #
                # The cutoff is never earlier than the detection BAR'S CLOSE
                # (a first_pass_et of 10:20 labels the 5-minute bar running to
                # 10:25, so the signal is not knowable until 10:25), and never
                # earlier than the scan tick that first saw the candidate or
                # the selection decision -- first_pass_et is walked back up to
                # an hour, and entering there credits survivors with fills
                # nobody could have taken. See runner_scan.honest_entry_time.
                from ..runner_scan import honest_entry_time
                cutoff = honest_entry_time(fp, c.get("scan_pass_et"), selection_at)
                entry_bars = [b for b in bars
                              if b["t"].replace(tzinfo=None) >= cutoff]

        if not entry_bars:
            summary["no_bars"] += 1
            ledger.save_runner_candidate_outcome(con, {
                "session_date": d, "ticker": ticker, "eval_date": d,
                "triggered": False, "exit_reason": "no_data",
                "sim_version": CURRENT_SIM_VERSION,
                "entry_after_et": cutoff,
                "atr14": atr_map.get(ticker),
                "stop_dist_atr": _stop_dist_atr(c, atr_map.get(ticker)),
                "trend_side_200d": _trend_side(trend_map.get(ticker)),
            })
            continue

        shim = {"pick_id": 0, "direction": "long", "entry_type": "open",
                "entry_price": c.get("detection_price"),
                "stop_price": c.get("stop_price"),
                "target_price": c.get("target_price")}
        sim = simulate_pick(shim, entry_bars, None, slippage_bps=slippage,
                            flatten_at=flatten_at, min_bar_volume=min_bar_volume,
                            atr14=atr_map.get(ticker),
                            trend_snapshot=trend_map.get(ticker),
                            session_date=d)

        ledger.save_runner_candidate_outcome(con, {
            "session_date": d, "ticker": ticker, "eval_date": d,
            "triggered": sim.get("triggered"),
            "entry_fill": sim.get("entry_fill"),
            "exit_price": sim.get("exit_price"),
            "exit_reason": sim.get("exit_reason"),
            "r_multiple": sim.get("r_multiple"),
            "mfe_r": sim.get("mfe_r"),
            "mae_r": sim.get("mae_r"),
            "eod_r": sim.get("eod_r"),
            "sim_version": sim.get("sim_version"),
            "entry_after_et": cutoff,
            "atr14": sim.get("atr14"),
            "stop_dist_atr": sim.get("stop_dist_atr"),
            "trend_side_200d": sim.get("trend_side_200d"),
        })
        summary["scored"] += 1
        if sim.get("triggered"):
            summary["triggered"] += 1
            if sim.get("r_multiple") is not None:
                summary["sum_r"] += sim["r_multiple"]

    summary["sum_r"] = round(summary["sum_r"], 2)
    summary["mean_r"] = (round(summary["sum_r"] / summary["triggered"], 3)
                         if summary["triggered"] else None)
    log("evening", f"Runner candidates priced: {summary['scored']} scored, "
                   f"{summary['triggered']} triggered, mean R "
                   f"{summary['mean_r'] if summary['mean_r'] is not None else 'n/a'}")
    return summary


def _grade_rejections(con, md: MarketData, d: date, slippage_bps: float = 0.0,
                      flatten_at: str | None = None, min_bar_volume: float = 0) -> dict:
    """Shadow ledger: simulate every rejected candidate; never touches the equity curve.

    Uses the same friction and exit rules (flatten_at, min_bar_volume) as taken
    picks -- comparisons between taken and rejected are only fair if both sides
    are graded identically. Rejections don't carry a time_stop (not stored on
    the rejections table), which behaves the same as a pick with no time_stop.
    """
    rejections = ledger.ungraded_rejections(con, d)
    summary = {"scored": 0, "net_forgone_r": 0.0, "gap_fade_notes": [],
               "rr_band_notes": []}
    for r in rejections:
        try:
            bars = md.minute_bars(r["ticker"], d)
        except Exception:
            bars = []
        shim = {"pick_id": r["rejection_id"], "direction": r["direction"],
                "entry_type": r["entry_type"], "entry_price": r["entry_price"],
                "stop_price": r["stop_price"], "target_price": r["target_price"]}
        sim = simulate_pick(shim, bars, None, slippage_bps=slippage_bps,
                            flatten_at=flatten_at, min_bar_volume=min_bar_volume,
                            session_date=d)
        eod_outcome, forgone = classify_shadow(sim)
        ledger.save_rejection_outcome(con, r["rejection_id"], eod_outcome, forgone,
                                      sim_version=sim.get("sim_version"))
        summary["scored"] += 1
        summary["net_forgone_r"] += forgone or 0.0
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
                                slippage_bps=slippage_bps, session_date=d)
        open_sim = simulate_pick({"pick_id": 0, "direction": c["direction"],
                                  "entry_type": "open", "entry_price": bars[0]["o"],
                                  "stop_price": c["stop_price"],
                                  "target_price": c["target_price"]}, bars, None,
                                 slippage_bps=slippage_bps, session_date=d)
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

    # Q7 (indicator layer): does participation (RVOL@10:00 vs ADV20) separate
    # winners from chop? One increment per triggered pick with a non-null
    # rvol_at_1000 -- ledger.stats_by_outcome("rvol_bucket") is the deterministic
    # aggregate the weekly miner reads; this just logs the raw events.
    for p in picks:
        o = by_id.get(p["pick_id"], {})
        if o.get("triggered") and o.get("rvol_at_1000") is not None:
            ledger.add_question_progress(
                con, Q_RVOL_SEPARATION, d, 1,
                f"{p['ticker']} RVOL@10:00={o['rvol_at_1000']:.2f}x R={o.get('r_multiple')}")
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
                f"crosses={o.get('vwap_crosses')} R={o.get('r_multiple')}")
            counts["q_increments"] += 1

    # Q9 (indicator layer v2): stop distance sweet spot -- every stopped trade
    # with a non-null stop_dist_atr (the ATR-normalized denominator makes this
    # comparable across tickers, unlike the earlier Q6 raw stop-placement question).
    for p in picks:
        o = by_id.get(p["pick_id"], {})
        if o.get("exit_reason") == "stop" and o.get("stop_dist_atr") is not None:
            ledger.add_question_progress(
                con, Q_STOP_ATR_SWEET_SPOT, d, 1,
                f"{p['ticker']} stop_dist_atr={o['stop_dist_atr']:.2f} R={o.get('r_multiple')}")
            counts["q_increments"] += 1

    # Q10: blue-sky (gap near the 52w high) vs overhead-supply (gap into the
    # 20d range) -- one increment per triggered gap/breakout-class pick.
    for p in picks:
        o = by_id.get(p["pick_id"], {})
        if o.get("triggered") and p.get("setup_type") in ("gap_go", "gap_fade", "breakout"):
            ledger.add_question_progress(
                con, Q_BLUE_SKY_VS_OVERHEAD, d, 1,
                f"{p['ticker']} setup={p.get('setup_type')} R={o.get('r_multiple')}")
            counts["q_increments"] += 1

    # Q11: trend alignment (the 200d question) -- every triggered pick with a
    # non-null trend_side_200d.
    for p in picks:
        o = by_id.get(p["pick_id"], {})
        if o.get("triggered") and o.get("trend_side_200d") is not None:
            ledger.add_question_progress(
                con, Q_TREND_ALIGNMENT_200D, d, 1,
                f"{p['ticker']} {p.get('direction')} trend_side_200d={o['trend_side_200d']} "
                f"R={o.get('r_multiple')}")
            counts["q_increments"] += 1

    # Q12: shortedness -- every triggered squeeze/momentum pick with a
    # non-null short_pct_float.
    for p in picks:
        o = by_id.get(p["pick_id"], {})
        if (o.get("triggered") and o.get("short_pct_float") is not None
                and p.get("setup_type") in ("short_squeeze", "catalyst_momentum")):
            ledger.add_question_progress(
                con, Q_SHORTEDNESS, d, 1,
                f"{p['ticker']} short_pct_float={o['short_pct_float']} R={o.get('r_multiple')}")
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
                f"{p['ticker']} planned R:R={rr:.2f} R={o.get('r_multiple')}")
            counts["q_increments"] += 1

    for ticker, rr, eod_outcome, forgone in shadow.get("rr_band_notes", []):
        ledger.add_question_progress(
            con, Q_RR_TEST_SHADOW, d, 1,
            f"{ticker} planned R:R={rr:.2f} (1.8-2.0 band) {eod_outcome} "
            f"forgone={forgone if forgone is None else round(forgone, 2)}R")
        counts["q_increments"] += 1

    counts["q_increments"] += _q1_gap_fade_mechanics(con, md, d, picks, outcomes, shadow,
                                                     slippage_bps=slippage_bps)
    return counts


def _prefetch_grading_bars(con, md: MarketData, d: date, picks: list[dict],
                           runner_enabled: bool) -> None:
    """Warm MarketData's bar memo for every symbol this run will grade.

    The graders below each fetch the same session's bars symbol by symbol --
    picks, the shadow ledger, the paired challenger arm and every runner
    candidate -- and on a runner-heavy day that sequential walk alone
    exceeded the 300s serverless budget, killing the run before the grading
    call, the lessons, or the calibration rows were ever written. One
    concurrent prefetch through the SAME fetch functions makes every later
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
        #   shadow +   minute only (simulated with daily=None)
        #   runner
        #   population session daily only (grade_population)
        pick_syms = {str(p["ticker"]).upper() for p in picks}
        paired = _q("""SELECT DISTINCT ticker FROM paired_arm_picks
                       WHERE plan_date = ? AND arm <> 'A' AND graded_at IS NULL""") - pick_syms
        minute_only = {str(r["ticker"]).upper() for r in ledger.ungraded_rejections(con, d)}
        if runner_enabled:
            minute_only |= {str(t).upper() for t in ledger.runner_candidate_tickers(con, d)}
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

    try:
        plan_row = ledger.plan_for_date(con, d)
        # The runner lane produces picks WITHOUT a morning plan: it detects
        # intraday and selects at 10:30, on its own schedule. Bailing here
        # whenever the morning loop did not produce a plan meant that on any
        # such day the runner's candidates were never graded, never reached
        # lesson extraction, and never reached the weekly miner -- the lane
        # accumulated evidence into a pipeline whose back half never ran.
        runner_enabled = bool((cfg.get("runner", {}) or {}).get("enabled"))
        has_runner_work = False
        if runner_enabled:
            try:
                has_runner_work = bool(ledger.runner_candidate_tickers(con, d))
            except Exception as e:
                log("evening", f"WARN could not check for runner candidates: {e}")
        if not plan_row and not has_runner_work:
            log("evening", f"No plan and no runner candidates for {d} — nothing to grade")
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
        if not plan_row:
            log("evening", f"No morning plan for {d}, but {len(ledger.runner_candidate_tickers(con, d))} "
                           f"runner candidate(s) to grade — proceeding on the runner lane alone")

        picks = ledger.picks_for_date(con, d)
        md = MarketData(cfg)
        _prefetch_grading_bars(con, md, d, picks, runner_enabled)
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
            entry_after_map = gctx["entry_after_map"]
            outcomes = grade_day(md, picks, d, slippage_bps=slippage,
                                 flatten_at=flatten_at, min_bar_volume=min_bar_volume,
                                 adv20_map=adv20_map, atr_map=atr_map, trend_map=trend_map,
                                 fundamentals_map=fundamentals_map, prev_close_map=prev_close_map,
                                 or_minutes=gctx["or_minutes"],
                                 entry_after_map=entry_after_map)
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
                stress = integrity.stress_ladder_for_pick(
                    p, bars, daily_bar, simulate_pick, fparams,
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

        # v3: shadow ledger — grade what the desk REJECTED (counterfactual only,
        # never touches the taken-trade equity curve)
        log("evening", "Scoring rejected candidates (shadow ledger)...")
        shadow = _grade_rejections(con, md, d, slippage_bps=slippage,
                                   flatten_at=flatten_at, min_bar_volume=min_bar_volume)

        # v4: population monkeys + challenger arms, graded by the SAME simulator.
        from .. import integrity
        integrity.on_evening(con, cfg, md, d, simulate=simulate_pick)

        # v10: DTO Runner lane — price EVERY candidate the intraday scan found,
        # selected or not. Additive and fully isolated: it runs after all
        # existing grading, and a failure here must not cost the desk its
        # already-computed outcomes, lessons, or email.
        runner_summary = {"scored": 0}
        if (cfg.get("runner", {}) or {}).get("enabled"):
            try:
                runner_summary = _grade_runner_candidates(con, md, d, cfg)
            except Exception as e:
                log("evening", f"WARN runner candidate grading failed; "
                               f"existing grading unaffected: {e}")
                runner_summary = {"scored": 0, "error": str(e)}

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
            # The runner lane grades on days the morning loop never produced a
            # plan. Say so plainly rather than rendering an empty plan block —
            # a grader shown a blank stance would otherwise narrate one.
            + ("(no morning plan was produced for this date — the picks below "
               "come from the intraday runner lane, which selects at 10:30 ET "
               "independently of the morning plan)\n\n" if not plan_row else
               f"Market stance: {plan_row['plan'].get('market_stance','')}\n"
               f"No-trade: {plan_row['no_trade']}"
               + (f" — reason: {plan_row['plan'].get('no_trade_reason','')}"
                  if plan_row["no_trade"] else "")
               + f"\nNotes for evening from the risk manager: "
                 f"{plan_row.get('notes_for_evening','')}\n\n")
            + f"## PICKS + COMPUTED OUTCOMES (mechanically simulated from real bars)\n\n{out_md}\n\n"
            + f"## EXECUTION GROUND TRUTH\n\n"
            f"Live fills recorded for {d}: "
            f"{sum(1 for o in ledger.live_orders_for_date(con, d) if o.get('entry_fill_price') is not None)}. "
            "Build the narrative from fills when present; only call this a stand-down after confirming zero fills.\n\n"
            f"## SHADOW LEDGER TODAY (rejected candidates — context only, do not grade them)\n\n{shadow_md}\n\n"
            + (f"{_runner_grader_block(con, d)}\n\n" if runner_summary.get("scored") else "")
            + f"## MARKET RECAP TODAY\n\n{recap_md or '(recap unavailable)'}\n\n"
            + f"## ACTIVE LESSONS (for confirm/contradict updates — use these IDs)\n\n{lessons_md}\n\n"
            "Grade the day now, following the output contract exactly."
        )

        model = cfg.models["evening"]
        log("evening", f"Grading call ({model})...")
        llm = LLMClient(cfg)
        result = llm.call(system=system, user=user, model=model,
                          mock_name="evening_grader", thinking=False, web_search=False)
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

        # 3. persist grades + lessons
        n_graded = ledger.apply_grades(con, review.get("pick_reviews", []), picks)

        # Observation only: live paper fills are reconciled alongside the
        # simulator after grading. The simulator's r_multiple remains intact.
        from ..live_monitor import reconcile_evening
        live_summary = reconcile_evening(con, cfg, d)

        tainted_day = _all_fills_tainted(outcomes)
        new_lessons = review.get("lessons", []) or []
        lesson_updates = review.get("lesson_updates", []) or []
        if tainted_day and (new_lessons or lesson_updates):
            log("evening", f"Skipping {len(new_lessons)} new lesson(s) and "
                           f"{len(lesson_updates)} lesson update(s) -- every pick with fill "
                           f"activity today was invalid_fill or execution_anomaly; nothing "
                           f"clean today to draw or confirm a thesis lesson from")
            new_lessons, lesson_updates = [], []
        for l in new_lessons:
            dup = ledger.find_duplicate_lesson(con, l.get("lesson", ""))
            if dup:
                log("evening", f"Skipped duplicate lesson (matches L{dup}): "
                               f"{l.get('lesson', '')[:80]}")
                continue
            lid = ledger.insert_lesson(con, l, "evening", d)
            log("evening", f"New lesson L{lid}: {l['lesson']}")
        for upd in lesson_updates:
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
                       f"live reconciled={live_summary['reconciled']}, "
                       f"lessons extracted={len(new_lessons)}, "
                       f"cost ~${result.usage['cost_usd']:.3f}")

        send_email(cfg, f"Trade Agent — Evening Review {d.isoformat()}",
                   evening_email_html(d, review, out_md))
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
        ledger.log_run(con, "evening", d, started, status, error=detail)
        log("evening", f"ERROR: {e}")
        raise
    finally:
        con.close()
