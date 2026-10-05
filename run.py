"""
Trade Agent CLI.

  python run.py init                      create dirs, DB, credentials template
  python run.py morning [--dry-run|--mock] [--offline] [--force] [--date YYYY-MM-DD]
  python run.py evening [--mock] [--offline] [--date YYYY-MM-DD] [--force]
  python run.py weekly  [--mock] [--date YYYY-MM-DD]
  python run.py dashboard                 regenerate the HTML dashboard
  python run.py status                    ledger + performance snapshot
  python run.py regrade --from YYYY-MM-DD [--force]
                                           recompute outcomes under CURRENT sim
                                           semantics (explicit, never automatic)
  python run.py regrade-runner --from YYYY-MM-DD [--force]
                                           re-price runner candidates under the
                                           CURRENT entry rule (explicit, opt-in)
  python run.py regate-runner --from YYYY-MM-DD [--apply]
                                           relabel scan_version 1 runner rows
                                           that pass every v2 gate (dry run
                                           unless --apply)
  python run.py runner-exits [--from YYYY-MM-DD]
                                           runner exit-policy replay report, incl.
                                           the construction arms A/B/C
                                           (read-only; fill it with regrade-runner)
  python run.py runner-evidence           runner continuation after entry (drift
                                           in ATR by bucket) + why each session is
                                           or is not contested (read-only)
  python run.py research [--full]         research-integrity report: selection
                                           ablation + population baseline
                                           (zero API cost, read-only)
"""
from __future__ import annotations

import argparse
import shutil
import sys

from agent import ledger
from agent.config import (BASE_DIR, CREDENTIALS_PATH, DB_PATH, ensure_dirs, load_config)
from agent.util import log, parse_date, today_et


def cmd_init(cfg, args) -> None:
    ensure_dirs()
    example = BASE_DIR / "credentials.json.example"
    if not CREDENTIALS_PATH.exists() and example.exists():
        shutil.copy2(example, CREDENTIALS_PATH)
        log("init", f"Created {CREDENTIALS_PATH.name} — add your Anthropic API key")
    con = ledger.connect()
    con.close()
    log("init", f"Ledger ready at {DB_PATH}")
    log("init", "Next: put your Anthropic key in credentials.json, then run:  python run.py morning")


def cmd_research(cfg, args) -> None:
    """Research-integrity report: selection ablation + population baseline,
    computed entirely from data already in the ledger (zero API cost). --full
    also renders the weekly miner's full research block (FDR-corrected looks,
    quarantine states, paired-arm comparison).
    """
    from agent import ablation, integrity, population
    from agent import ledger as ledger_mod

    con = ledger.connect()
    try:
        print(ablation.ablation_md(ablation.random_pick_ablation(con)))
        print()
        print(population.population_md(population.population_stats(con)))
        if args.full:
            print()
            print(integrity.weekly_block(con, cfg, ledger_mod, today_et())["markdown"])
    finally:
        con.close()


def cmd_status(cfg, args) -> None:
    con = ledger.connect()
    overall = ledger.overall_stats(con)
    streak = ledger.current_streak(con)
    curve = ledger.equity_curve(con, cfg.get("account_size"), cfg.get("dollars_per_r"))
    lessons = ledger.active_lessons(con, 100)
    runs = ledger.recent_runs(con, 5)
    from agent.memory import read_playbook
    _, pb_version = read_playbook(con)

    print("\n=== TRADE AGENT STATUS ===")
    print(f"Playbook version: v{pb_version}")
    print(f"Graded picks: {overall['picks_graded']}  (triggered: {overall['triggered']})")
    wr = f"{overall['win_rate']:.0%}" if overall["win_rate"] is not None else "n/a"
    print(f"Win rate: {wr}   Avg R: {overall['avg_r']}   Total weighted R: {overall['total_weighted_r']}")
    equity = curve[-1]["equity"] if curve else cfg.get("account_size")
    print(f"Paper equity: ${equity:,.0f}  (start ${cfg.get('account_size'):,})")
    if streak["kind"] != "none":
        print(f"Current streak: {streak['length']} {streak['kind']}")
    print(f"Active lessons: {len(lessons)}")
    if runs:
        print("\nRecent runs:")
        for r in runs:
            print(f"  {r['run_date']} {r['loop']:8s} {r['status']:8s} "
                  f"${r['cost_usd']:.3f} {r['model']}")
    try:
        from agent import runner_view
        rs = runner_view.session_summary(con, today_et())
        if rs["session"] or rs["rows"]:
            print("")
            print(f"Runner today: {rs['n_candidates']} candidates, "
                  f"{rs['n_selected']} selected, {len(rs['orders'])} live orders"
                  + ("  [DEGRADED]" if rs["session"].get("provider_degraded") else ""))
            print("  full detail: run.py runner-status")
    except Exception as e:
        print("")
        print(f"Runner today: unavailable ({e})")

    total_cost = sum(r["cost_usd"] or 0 for r in ledger.recent_runs(con, 1000))
    print(f"\nAll-time API spend: ~${total_cost:.2f}")
    con.close()


def cmd_dashboard(cfg, args) -> None:
    from agent.delivery import render_dashboard
    con = ledger.connect()
    row = con.execute("SELECT max(plan_date) FROM daily_plans").fetchone()
    latest = row[0] if row and row[0] else None
    path = render_dashboard(con, cfg, latest)
    con.close()
    print(f"Dashboard: {path}")


def cmd_regrade(cfg, args) -> None:
    """Recompute outcomes for picks on/after --from under CURRENT simulation
    semantics (agent.grading.CURRENT_SIM_VERSION). Explicit, opt-in, never
    invoked automatically by any loop -- existing rows were graded under
    whatever rules were live when they were written, and are not silently
    rewritten just because the code changed underneath them.
    """
    from collections import defaultdict

    from agent.grading import CURRENT_SIM_VERSION, grade_day
    from agent.grading_context import build_grading_context
    from agent.providers import MarketData

    from_date = parse_date(args.from_date)
    con = ledger.connect()
    picks = ledger.graded_picks_since(con, from_date)
    if not picks:
        log("regrade", f"No graded picks on/after {from_date} — nothing to do")
        con.close()
        return

    if len(picks) > 50 and not args.force:
        log("regrade", f"Refusing: this would rewrite {len(picks)} outcome rows (>50). "
                       "Re-run with --force to proceed.")
        con.close()
        sys.exit(1)

    by_date: dict = defaultdict(list)
    for p in picks:
        by_date[p["plan_date"]].append(p)

    md = MarketData(cfg)
    slippage = float(cfg.get("slippage_bps", 5))
    flatten_at = cfg.get("flatten_at")
    min_bar_volume = float(cfg.get("min_bar_volume", 0) or 0)

    n = 0
    for d in sorted(by_date):
        day_picks = by_date[d]
        gctx = build_grading_context(md, con, d, [p["ticker"] for p in day_picks],
                                     cfg, tag="regrade")
        outcomes = grade_day(md, day_picks, d, slippage_bps=slippage,
                             flatten_at=flatten_at, min_bar_volume=min_bar_volume,
                             adv20_map=gctx["adv20_map"], atr_map=gctx["atr_map"],
                             trend_map=gctx["trend_map"],
                             fundamentals_map=gctx["fundamentals_map"],
                             prev_close_map=gctx["prev_close_map"],
                             or_minutes=gctx["or_minutes"],
                             entry_after_map=gctx["entry_after_map"])
        for o in outcomes:
            ledger.save_outcome(con, o)
            n += 1
    log("regrade", f"Recomputed {n} outcome row(s) from {from_date} onward "
                   f"under sim_version={CURRENT_SIM_VERSION}")
    con.close()


def cmd_regrade_runner(cfg, args) -> None:
    """Re-price every runner candidate for sessions on/after --from under the
    CURRENT simulation semantics -- in particular the honest entry time
    (runner_scan.honest_entry_time: never before the scan tick that recorded
    the candidate or the selection decision). Rows graded before sim_version 4
    entered at the walked-back detection bar, up to an hour before anything
    could have acted on it, which flatters every runner R the weekly miner
    reads. Explicit and opt-in, like `regrade`: never run automatically.
    Selection stamps (selected/rank/pick_id) are preserved by the upsert.
    Runner PICKS (setup dto_runner) are morning-style outcome rows -- re-grade
    those with `regrade`.
    """
    from agent.grading import CURRENT_SIM_VERSION
    from agent.loops.evening import _grade_runner_candidates
    from agent.providers import MarketData

    from_date = parse_date(args.from_date)
    con = ledger.connect()
    rows = con.execute(
        """SELECT session_date, COUNT(*) FROM runner_candidates
           WHERE session_date >= ? GROUP BY session_date ORDER BY session_date""",
        [from_date]).fetchall()
    total = sum(int(r[1]) for r in rows)
    if not rows:
        log("regrade", f"No runner candidates on/after {from_date} -- nothing to do")
        con.close()
        return
    if total > 500 and not args.force:
        log("regrade", f"Refusing: this would re-price {total} runner candidate rows "
                       f"across {len(rows)} session(s) (>500). Re-run with --force.")
        con.close()
        sys.exit(1)
    con.close()
    md = MarketData(cfg)
    for d, n in rows:
        # A fresh connection per session, and one retry if it drops: Neon
        # closed one mid-session on 2026-09-26 (568 candidates). Re-grading a
        # session is idempotent -- outcome rows are updated in place and
        # replays are replaced per session -- so the retry is safe.
        for attempt in (1, 2):
            con = ledger.connect()
            try:
                tickers = ledger.runner_candidate_tickers(con, d)
                md.prefetch_session(tickers, d, daily=False)
                summary = _grade_runner_candidates(con, md, d, cfg)
                restored = ledger.restore_runner_selection(con, d)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2 or type(e).__name__ not in ("OperationalError",
                                                              "InterfaceError"):
                    raise
                log("regrade", f"{d}: connection lost ({type(e).__name__}); retrying session")
            finally:
                try:
                    con.close()
                except Exception:  # noqa: BLE001
                    pass
        log("regrade", f"{d}: re-priced {summary['scored']}/{n} candidate(s), "
                       f"mean R {summary.get('mean_r')}"
                       + (f"; restored selection on {', '.join(restored)}" if restored else ""))
    log("regrade", f"Re-priced {total} runner candidate row(s) from {from_date} onward "
                   f"under sim_version={CURRENT_SIM_VERSION}")


def cmd_regrade_shadow(cfg, args) -> None:
    """Re-price every shadow-ledger rejection on/after --from with the evening
    loop's own simulator and friction. Dry run unless --apply.

    Written for 2026-09-25: a test run whose environment carried the
    production DATABASE_URL (tests/test_morning_pipeline_repair.py, before
    tests/conftest.py stripped it) inserted two fixture rejections and marked
    EVERY rejection would_have_won +1.0R. That evening's grader then skipped
    the day's real rejections, because they already looked graded.
    --delete-ids removes named fixture rows first; each is printed before it
    goes.

    The evening also turns shadow grading into open-question progress: Q14
    (R:R band, recorded from the 2026-09-17 session on) and Q1 (gap-fade
    mechanics). Re-pricing does not touch that table. A session whose
    evening ran while its rejections already looked graded never got its
    shadow notes -- 2026-09-25 -- and --progress-for names those sessions;
    a note is added only where that ticker has none for that question and
    session. Opt-in per session, because a blanket backfill also writes
    observations for sessions from before a question was being recorded.
    """
    from agent.grading import CURRENT_SIM_VERSION
    from agent.loops.evening import (Q_GAP_FADE_MECHANICS, Q_RR_TEST_SHADOW,
                                     _grade_rejections, _q1_gap_fade_mechanics,
                                     _rr_band_note)
    from agent.providers import MarketData

    from_date = parse_date(args.from_date)
    delete_ids = [int(x) for x in (args.delete_ids or "").split(",") if x.strip()]
    progress_for = {parse_date(x) for x in (args.progress_for or "").split(",") if x.strip()}
    con = ledger.connect()

    for rid in delete_ids:
        row = con.execute(
            "SELECT rejection_id, plan_date, ticker, reason, entry_price, stop_price, "
            "target_price, eod_outcome, forgone_r FROM rejections WHERE rejection_id = ?",
            [rid]).fetchone()
        log("regrade", f"delete rejection {rid}: {row if row else 'NOT FOUND'}")

    keep = (f" AND rejection_id NOT IN ({','.join('?' * len(delete_ids))})"
            if delete_ids else "")
    rows = con.execute(
        f"""SELECT plan_date, COUNT(*),
                   SUM(CASE WHEN eod_outcome = 'would_have_won' AND forgone_r = 1.0
                            THEN 1 ELSE 0 END)
            FROM rejections WHERE plan_date >= ?{keep}
            GROUP BY plan_date ORDER BY plan_date""",
        [from_date, *delete_ids]).fetchall()
    total = sum(int(r[1]) for r in rows)
    for d, n, stamped in rows:
        log("regrade", f"{d}: {n} rejection(s), {int(stamped or 0)} carrying the "
                       f"would_have_won +1.0R stamp")
    if not args.apply:
        log("regrade", f"Dry run: would delete {len(delete_ids)} row(s) and re-price "
                       f"{total} rejection(s) across {len(rows)} session(s). "
                       f"Re-run with --apply to write.")
        con.close()
        return
    if total > 500 and not args.force:
        log("regrade", f"Refusing: this would re-price {total} rejection rows "
                       f"across {len(rows)} session(s) (>500). Re-run with --force.")
        con.close()
        sys.exit(1)

    for rid in delete_ids:
        con.execute("DELETE FROM rejections WHERE rejection_id = ?", [rid])

    def _has_note(qid: int, d, ticker: str) -> bool:
        # Q14 notes read "TICKER planned R:R=...", Q1 notes "TICKER: planned-entry ..."
        return con.execute(
            "SELECT 1 FROM open_question_progress "
            "WHERE question_id = ? AND session_date = ? AND (note LIKE ? OR note LIKE ?)",
            [qid, d, f"{ticker} %", f"{ticker}:%"]).fetchone() is not None

    md = MarketData(cfg)
    slippage = float(cfg.get("slippage_bps", 5))
    flatten_at = cfg.get("flatten_at")
    min_bar_volume = float(cfg.get("min_bar_volume", 0) or 0)
    q_added = 0
    for d, n, _stamped in rows:
        # Reset and re-price one session at a time, so a failure partway
        # leaves every finished session graded and the rerun picks up the rest.
        con.execute("UPDATE rejections SET eod_outcome = NULL, forgone_r = NULL, "
                    "graded_at = NULL, sim_version = NULL WHERE plan_date = ?", [d])
        tickers = [r[0] for r in con.execute(
            "SELECT DISTINCT ticker FROM rejections WHERE plan_date = ?", [d]).fetchall()]
        md.prefetch_session(tickers, d, daily=False)
        shadow = _grade_rejections(con, md, d, slippage_bps=slippage,
                                   flatten_at=flatten_at, min_bar_volume=min_bar_volume)
        log("regrade", f"{d}: re-priced {shadow['scored']}/{n} rejection(s), "
                       f"net forgone {shadow['net_forgone_r']:+.2f}R")
        if d not in progress_for:
            continue
        for ticker, rr, eod_outcome, forgone in shadow["rr_band_notes"]:
            if not _has_note(Q_RR_TEST_SHADOW, d, ticker):
                ledger.add_question_progress(con, Q_RR_TEST_SHADOW, d, 1,
                                             _rr_band_note(ticker, rr, eod_outcome, forgone))
                q_added += 1
        missing_fades = [g for g in shadow["gap_fade_notes"]
                         if not _has_note(Q_GAP_FADE_MECHANICS, d, g[0]["ticker"])]
        q_added += _q1_gap_fade_mechanics(con, md, d, [], [],
                                          {"gap_fade_notes": missing_fades},
                                          slippage_bps=slippage)
    log("regrade", f"Deleted {len(delete_ids)} row(s); re-priced {total} rejection(s) "
                   f"from {from_date} onward under sim_version={CURRENT_SIM_VERSION}; "
                   f"added {q_added} missing open-question note(s)")
    con.close()


def cmd_find_test_rows(cfg, args) -> None:
    """Read-only sweep for rows a test run wrote into this ledger.

    Five test files used to isolate themselves only by pointing
    ledger.DB_PATH at a tmp file, which connect() ignores when DATABASE_URL
    is set (tests/conftest.py now strips it). On 2026-09-25 at least two of
    them ran against production. This lists every fingerprint those files can
    leave, so the damage is known before anything is deleted. Writes nothing.
    """
    from datetime import datetime, time as dtime

    since = parse_date(args.since)
    since_ts = datetime.combine(since, dtime(0, 0))
    con = ledger.connect()

    def show(title: str, sql: str, params: list) -> None:
        rows = con.execute(sql, params).fetchall()
        print(f"\n== {title}: {len(rows)}")
        for r in rows:
            print("  ", tuple(str(v) if v is not None else None for v in r))

    show("fixture rejections (AAAA / PHVS 'no live quote' / MARA 11.5-10.9-12.6)",
         """SELECT rejection_id, plan_date, ticker, reason, entry_price, stop_price,
                   target_price, eod_outcome, forgone_r, graded_at
            FROM rejections
            WHERE ticker = 'AAAA'
               OR (ticker = 'PHVS' AND reason = 'no live quote')
               OR (ticker = 'MARA' AND entry_price = 11.5 AND stop_price = 10.9)
            ORDER BY rejection_id""", [])

    # save_rejections DELETEs a date's rows before inserting, so a fixture
    # written onto a real session wipes that session's real rejections.
    # daily_plans.persisted_rejection_count is what the morning actually wrote.
    plans = con.execute(
        "SELECT plan_date, persisted_rejection_count FROM daily_plans "
        "ORDER BY plan_date").fetchall()
    counts = dict(con.execute(
        "SELECT plan_date, COUNT(*) FROM rejections GROUP BY plan_date").fetchall())
    short = [(d, want, int(counts.get(d, 0))) for d, want in plans
             if want is not None and int(counts.get(d, 0)) != int(want)]
    print(f"\n== sessions whose stored rejections != the morning's persisted count: {len(short)}")
    for d, want, have in short:
        print(f"   {d}: morning persisted {want}, ledger now holds {have}")

    weekend = sorted({d for (d,) in con.execute(
        "SELECT plan_date FROM daily_plans UNION SELECT plan_date FROM rejections").fetchall()
        if d is not None and d.weekday() >= 5})
    print(f"\n== weekend sessions in daily_plans/rejections: {len(weekend)}")
    for d in weekend:
        print("  ", d)

    show("picks with fixture tickers (TEST, AAAA, TA-TD)",
         """SELECT pick_id, plan_date, ticker, entry_price, stop_price, target_price
            FROM picks WHERE ticker IN ('TEST', 'AAAA', 'TA', 'TB', 'TC', 'TD')
            ORDER BY pick_id""", [])

    show("fixture lessons ('test lesson' / 'placeholder')",
         """SELECT lesson_id, created_date, lesson, status, confirmations, contradictions
            FROM lessons WHERE lesson IN ('test lesson', 'placeholder')
            ORDER BY lesson_id""", [])

    show("live orders for 2026-09-04 or with fixture ids (stop-1 / oco-1)",
         """SELECT live_order_id, pick_id, session_date, ticker, status, exit_reason,
                   live_r, oco_order_id
            FROM live_orders
            WHERE session_date = ? OR live_order_id = 'stop-1' OR oco_order_id = 'oco-1'""",
         [parse_date("2026-09-04")])

    show(f"runs started on/after {since} for an earlier session",
         """SELECT run_id, loop, run_date, started_at, status
            FROM runs WHERE started_at >= ? AND run_date < ?
            ORDER BY run_id""", [since_ts, since])

    show(f"outcomes graded on/after {since} for picks from before it",
         """SELECT p.plan_date, p.ticker, o.pick_id, o.exit_reason, o.r_multiple,
                   o.graded_at, o.sim_version
            FROM outcomes o JOIN picks p ON p.pick_id = o.pick_id
            WHERE o.graded_at >= ? AND p.plan_date < ?
            ORDER BY o.graded_at""", [since_ts, since])
    con.close()


_FIXTURE_REJECTIONS_SQL = """
    SELECT rejection_id, plan_date, ticker, reason FROM rejections
    WHERE ticker = 'AAAA'
       OR (ticker = 'PHVS' AND reason = 'no live quote' AND entry_price = 12.4)
       OR (ticker = 'MARA' AND entry_price = 11.5 AND stop_price = 10.9)
    ORDER BY rejection_id"""


def cmd_repair_test_rows(cfg, args) -> None:
    """Remove what test runs wrote into this ledger, and put back the shadow
    rows they deleted. Dry run unless --apply. `find-test-rows` shows the
    damage; this repairs the part a fixture signature identifies exactly.

    Removes:
      - lessons 'test lesson' / 'placeholder', with their dated
        confirmations and contradictions
      - the AAPL 'stop-1' live order on 2026-09-04, and the live_r the
        monitor wrote onto that session's real pick
      - runs logged on/after --since for an earlier session
      - daily plans CREATED on/after --since for an earlier date (the empty
        January plans, the Saturday 2026-08-29 plan), with anything under them
      - fixture rejections (AAAA / PHVS 'no live quote' / MARA 11.5-10.9-12.6)
    Restores, for each --rebuild date: the session's rejections, rebuilt from
    its stored plan and analyst candidates by the morning's own
    _build_rejections. save_rejections deletes a date's rows before
    inserting, so a fixture written onto a real session wiped that session.
    Refused when the session still holds real rejections, or when the
    morning's persisted count disagrees with the rebuild.

    Re-price the shadow ledger afterwards with `regrade-shadow`.
    """
    import json
    from datetime import datetime, time as dtime

    from agent.loops.morning import _build_rejections

    since = parse_date(args.since)
    since_ts = datetime.combine(since, dtime(0, 0))
    rebuild = [parse_date(x) for x in (args.rebuild or "").split(",") if x.strip()]
    con = ledger.connect()
    steps: list = []   # (description, callable) run in order on --apply

    def listing(title: str, sql: str, params: list) -> list:
        rows = con.execute(sql, params).fetchall()
        print(f"\n== {title}: {len(rows)}")
        for r in rows:
            print("  ", tuple(str(v) if v is not None else None for v in r))
        return rows

    def sql_step(desc: str, sql: str, params: list) -> None:
        steps.append((desc, lambda: con.execute(sql, params)))

    def in_list(ids: list) -> str:
        return ",".join("?" * len(ids))

    lessons = [r[0] for r in listing(
        "fixture lessons",
        "SELECT lesson_id, created_date, lesson, confirmations FROM lessons "
        "WHERE lesson IN ('test lesson', 'placeholder') ORDER BY lesson_id", [])]
    if lessons:
        for tbl in ("lesson_confirmations", "lesson_contradictions"):
            sql_step(f"delete {tbl} rows for fixture lessons",
                     f"DELETE FROM {tbl} WHERE lesson_id IN ({in_list(lessons)})", lessons)
        sql_step(f"delete {len(lessons)} fixture lesson(s)",
                 f"DELETE FROM lessons WHERE lesson_id IN ({in_list(lessons)})", lessons)

    for order_id, pick_id, _d, _t in listing(
            "fixture live orders (AAPL stop-1, 2026-09-04)",
            "SELECT live_order_id, pick_id, session_date, ticker FROM live_orders "
            "WHERE live_order_id = 'stop-1' AND ticker = 'AAPL' AND session_date = ?",
            [parse_date("2026-09-04")]):
        sql_step(f"delete live order {order_id}",
                 "DELETE FROM live_orders WHERE live_order_id = ?", [order_id])
        others = con.execute("SELECT COUNT(*) FROM live_orders WHERE pick_id = ? "
                             "AND live_order_id <> ?", [pick_id, order_id]).fetchone()[0]
        live_r = con.execute("SELECT live_r FROM outcomes WHERE pick_id = ?",
                             [pick_id]).fetchone()
        print(f"   pick {pick_id}: outcomes.live_r now {live_r[0] if live_r else None}; "
              f"other live orders on it: {others}")
        if not others:
            sql_step(f"clear the live_r the fixture wrote onto pick {pick_id}",
                     "UPDATE outcomes SET live_r = NULL WHERE pick_id = ?", [pick_id])

    runs = [r[0] for r in listing(
        f"runs logged on/after {since} for an earlier session",
        "SELECT run_id, loop, run_date, started_at, status FROM runs "
        "WHERE started_at >= ? AND run_date < ? ORDER BY run_id", [since_ts, since])]
    if runs:
        sql_step(f"delete {len(runs)} run row(s)",
                 f"DELETE FROM runs WHERE run_id IN ({in_list(runs)})", runs)

    for (d, _created) in listing(
            f"daily plans created on/after {since} for an earlier date",
            "SELECT plan_date, created_at FROM daily_plans "
            "WHERE created_at >= ? AND plan_date < ? ORDER BY plan_date", [since_ts, since]):
        n_picks, n_rej = (con.execute("SELECT COUNT(*) FROM picks WHERE plan_date = ?",
                                      [d]).fetchone()[0],
                          con.execute("SELECT COUNT(*) FROM rejections WHERE plan_date = ?",
                                      [d]).fetchone()[0])
        print(f"   {d}: {n_picks} pick(s), {n_rej} rejection(s) under it")
        sql_step(f"delete outcomes under {d}",
                 "DELETE FROM outcomes WHERE pick_id IN "
                 "(SELECT pick_id FROM picks WHERE plan_date = ?)", [d])
        for tbl in ("picks", "rejections", "daily_plans"):
            sql_step(f"delete {tbl} for {d}", f"DELETE FROM {tbl} WHERE plan_date = ?", [d])

    fixture_rej = listing("fixture rejections", _FIXTURE_REJECTIONS_SQL, [])
    fixture_ids = [r[0] for r in fixture_rej]
    if fixture_ids:
        sql_step(f"delete {len(fixture_ids)} fixture rejection(s)",
                 f"DELETE FROM rejections WHERE rejection_id IN ({in_list(fixture_ids)})",
                 fixture_ids)

    for d in rebuild:
        print(f"\n== rebuild rejections for {d}")
        real = con.execute(
            "SELECT COUNT(*) FROM rejections WHERE plan_date = ?"
            + (f" AND rejection_id NOT IN ({in_list(fixture_ids)})" if fixture_ids else ""),
            [d, *fixture_ids]).fetchone()[0]
        if real:
            print(f"   REFUSED: the ledger still holds {real} real rejection(s) for it")
            continue
        row = con.execute("SELECT plan_json, analyst_json, persisted_rejection_count "
                          "FROM daily_plans WHERE plan_date = ?", [d]).fetchone()
        if not row:
            print("   REFUSED: no stored plan for this session")
            continue
        rows = _build_rejections(json.loads(row[1] or "{}"), json.loads(row[0] or "{}"))
        if row[2] is not None and len(rows) != int(row[2]):
            print(f"   REFUSED: rebuild gives {len(rows)} row(s), the morning persisted {row[2]}")
            continue
        for r in rows:
            print(f"   {r['ticker']} {r.get('direction')} {r.get('entry_type')} "
                  f"{r.get('entry_price')}/{r.get('stop_price')}/{r.get('target_price')} "
                  f"| {str(r.get('reason'))[:80]}")
        steps.append((f"rebuild {len(rows)} rejection(s) for {d}",
                      lambda d=d, rows=rows: ledger.save_rejections(con, d, rows)))

    if not args.apply:
        print(f"\nDry run: {len(steps)} step(s) would run. Re-run with --apply to write.")
        for desc, _fn in steps:
            print(f"   - {desc}")
        con.close()
        return
    for desc, fn in steps:
        fn()
        log("repair", desc)
    log("repair", f"Done: {len(steps)} step(s). Now re-price the shadow ledger with regrade-shadow.")
    con.close()


def cmd_undo_progress_backfill(cfg, args) -> None:
    """Take back the open-question notes the first `regrade-shadow` over-added
    on 2026-09-25. Dry run unless --apply.

    That version backfilled every session, and misread both questions:
      - Q14 (R:R band, shadow arm) was only recorded from the 2026-09-17
        session on (3b0df74 reached production 2026-09-17 02:44 UTC), so a
        Q14 note for an earlier session is an observation the system never
        recorded -- and it counts toward the R:R threshold test.
      - Q1 notes read "TICKER: ..." and its has-a-note check looked for
        "TICKER ...", so every gap-fade rejection's note was written twice.
    Q14 notes for sessions before --q14-from are deleted. Q1 notes are cut
    back to one per gap_fade row (rejections + picks) for that session and
    ticker, which is what the evening writes; the first ones read are kept.
    """
    from agent.loops.evening import Q_GAP_FADE_MECHANICS, Q_RR_TEST_SHADOW

    q14_from = parse_date(args.q14_from)
    con = ledger.connect()
    steps: list = []

    early = con.execute(
        "SELECT session_date, note FROM open_question_progress "
        "WHERE question_id = ? AND session_date < ? ORDER BY session_date",
        [Q_RR_TEST_SHADOW, q14_from]).fetchall()
    print(f"\n== Q14 notes for sessions before {q14_from}: {len(early)}")
    for d, note in early:
        print(f"   {d}  {note}")
    if early:
        steps.append((f"delete {len(early)} Q14 note(s) before {q14_from}",
                      lambda: con.execute(
                          "DELETE FROM open_question_progress "
                          "WHERE question_id = ? AND session_date < ?",
                          [Q_RR_TEST_SHADOW, q14_from])))

    groups: dict = {}
    for d, note, inc in con.execute(
            "SELECT session_date, note, increment FROM open_question_progress "
            "WHERE question_id = ?", [Q_GAP_FADE_MECHANICS]).fetchall():
        groups.setdefault((d, str(note).split(":", 1)[0]), []).append((note, inc))
    print("\n== Q1 notes beyond one per gap_fade row:")
    extra = 0
    for (d, ticker), rows in sorted(groups.items()):
        expected = sum(con.execute(
            f"SELECT COUNT(*) FROM {tbl} WHERE plan_date = ? AND ticker = ? "
            "AND setup_type = 'gap_fade'", [d, ticker]).fetchone()[0]
            for tbl in ("rejections", "picks"))
        if expected == 0 or len(rows) <= expected:
            continue
        extra += len(rows) - expected
        print(f"   {d} {ticker}: {len(rows)} note(s) for {expected} gap_fade row(s)")
        for note, _inc in rows:
            print(f"      {note}")

        def collapse(d=d, ticker=ticker, keep=rows[:expected]) -> None:
            con.execute("DELETE FROM open_question_progress "
                        "WHERE question_id = ? AND session_date = ? AND note LIKE ?",
                        [Q_GAP_FADE_MECHANICS, d, f"{ticker}:%"])
            for note, inc in keep:
                ledger.add_question_progress(con, Q_GAP_FADE_MECHANICS, d, inc or 1, note)

        steps.append((f"cut {d} {ticker} Q1 notes to {expected}", collapse))
    print(f"   {extra} extra note(s) in total")

    if not args.apply:
        print(f"\nDry run: {len(steps)} step(s) would run. Re-run with --apply to write.")
        con.close()
        return
    for desc, fn in steps:
        fn()
        log("repair", desc)
    log("repair", f"Done: {len(steps)} step(s).")
    con.close()


def cmd_regate_runner(cfg, args) -> None:
    """Relabel scan_version 1 runner candidates whose STORED inputs pass every
    scan_version 2 gate (agent/runner_regate.py). Dry run unless --apply.

    Pair it with `regrade-runner` so the relabelled rows are also priced under
    the current sim_version -- the weekly search mines only rows that match
    BOTH current versions.
    """
    from agent import runner_regate
    from agent.runner_scan import SCAN_VERSION

    from_date = parse_date(args.from_date)
    require_rvol = bool((cfg.get("runner", {}) or {}).get("require_rvol", True))
    if SCAN_VERSION in runner_regate.UNREACHABLE:
        log("regate", f"NOTE: the running scan_version is {SCAN_VERSION}, which no stored "
                      f"row can be relabelled into ({runner_regate.UNREACHABLE[SCAN_VERSION]}). "
                      f"This only relabels v{runner_regate.REGATE_FROM} -> "
                      f"v{runner_regate.REGATE_TO} for the record; the weekly search "
                      f"mines v{SCAN_VERSION} rows only.")
    con = ledger.connect()
    try:
        res = runner_regate.plan(con, from_date, require_rvol=require_rvol)
        log("regate", f"Checked {res['checked']} scan_version {runner_regate.REGATE_FROM} "
                      f"row(s) on/after {from_date}: {len(res['admit'])} pass every "
                      f"scan_version {runner_regate.REGATE_TO} gate across "
                      f"{len(res['sessions'])} session(s)")
        for why, n in sorted(res["rejected_by_reason"].items()):
            log("regate", f"  stays v{runner_regate.REGATE_FROM}: {n} -- {why}")
        if not args.apply:
            log("regate", "Dry run -- nothing written. Re-run with --apply to relabel.")
            return
        n = runner_regate.apply(con, res["admit"])
        log("regate", f"Relabelled {n} row(s) scan_version {runner_regate.REGATE_TO} "
                      f"(regated_from={runner_regate.REGATE_FROM})")
    finally:
        con.close()


def cmd_traders_log(cfg, args, run_date) -> None:
    """Show (--show) or regenerate a session's trader's log
    (specs/TRADERS_LOG_SPEC_v1.md). Regeneration refuses a session whose
    evening grading has not completed: the log is written from what the
    evening stored, and a half-graded day would be narrated as a whole one."""
    from agent import traders_log
    d = run_date or today_et()
    con = ledger.connect()
    try:
        if args.show:
            row = traders_log.load(con, d)
            if not row:
                print(f"No trader's log stored for {d}.")
                return
            print(f"{d}  status={row['status']}  persona={row['persona_version']}  "
                  f"model={row['model']}  cost=${float(row['cost_usd'] or 0):.4f}")
            if row["violations_json"]:
                print(f"validator: {row['violations_json']}")
            print()
            print(row["rendered_text"] or "(nothing rendered)")
            return
        graded = con.execute(
            "SELECT COUNT(*) FROM runs WHERE loop = 'evening' AND run_date = ? "
            "AND status IN ('ok', 'degraded')", [d]).fetchone()[0]
        if not graded and not cfg.mock:
            log("traders-log", f"Refusing: the evening grading for {d} has not completed "
                               "(no ok/degraded evening run). Run the evening first.")
            sys.exit(1)
        row = traders_log.generate(con, cfg, d)
        print(f"{d}  status={row['status']}  cost=${float(row['cost_usd'] or 0):.4f}"
              + ("  (mock: not stored)" if cfg.mock else ""))
        if row.get("violations_json"):
            print(f"validator: {row['violations_json']}")
        print()
        print(row.get("rendered_text") or "(nothing rendered)")
    finally:
        con.close()


def cmd_runner(cfg, args) -> None:
    """One intraday Runner tick (or a loop of them) from this machine.

    The hosted path for this lane is POST /cron/intraday-check, driven by a
    scheduler. On this account neither available scheduler can do it: GitHub
    Actions returns startup_failure for every run (an account-level block -- a
    trivial `echo hello` workflow fails identically), and Vercel Cron on the
    Hobby plan rejects any schedule that fires more than once a day.

    So this runs the same `agent.loops.runner.tick()` locally, against the
    local ledger, using the credentials already in credentials.json. Orders go
    to the SAME Alpaca paper account either way -- the only difference is which
    database records the evidence.
    """
    import time as _time
    from datetime import datetime, time as dtime

    from agent.loops import runner as runner_loop
    from agent.providers import MarketData
    from agent.util import ET, now_et

    if args.no_orders:
        cfg.raw.setdefault("runner", {})["live_orders_enabled"] = False
        log("runner", "--no-orders: order placement disabled for this run")

    rcfg = cfg.get("runner", {}) or {}
    if not rcfg.get("enabled"):
        log("runner", "runner.enabled is false in config.json -- nothing to do")
        return

    md = MarketData(cfg)
    con = ledger.connect()

    def _at_override():
        if not args.at:
            return None
        hh, mm = (int(x) for x in args.at.split(":"))
        return datetime.combine(now_et().date(), dtime(hh, mm), tzinfo=ET)

    try:
        while True:
            at = _at_override() or now_et()
            started = _time.monotonic()
            try:
                res = runner_loop.tick(cfg, at=at, md=md, con=con)
            except Exception as e:
                # A tick must never kill the loop: a transient provider error
                # at 10:35 should not cost the rest of the session.
                log("runner", f"WARN tick failed ({type(e).__name__}: {e}) -- continuing")
                res = {"error": str(e)}

            if res.get("skipped"):
                log("runner", f"skipped: {res['skipped']}")
            elif "error" not in res:
                log("runner",
                    f"universe={res.get('universe_size')} "
                    f"new={res.get('new_candidates')} "
                    f"total={res.get('candidates')} "
                    f"{res.get('elapsed_ms')}ms"
                    + (" [DEGRADED]" if res.get("provider_degraded") else ""))
                sel = res.get("selection") or {}
                if sel.get("selected"):
                    log("runner", f"SELECTED {len(sel['selected'])}: "
                                  + ", ".join(sel["selected"]))
                    orders = sel.get("orders") or {}
                    log("runner", f"orders placed: {orders.get('placed', 0)}"
                                  + (f" | skipped: {orders['skipped']}"
                                     if orders.get("skipped") else ""))
                elif sel.get("selection_skipped_reason"):
                    log("runner", f"selection: {sel['selection_skipped_reason']}")

            if not args.loop:
                break

            # Stop once the window has closed for the day rather than spinning
            # until the process is killed.
            end = str(rcfg.get("scan_end_et", "15:00"))
            eh, em = (int(x) for x in end.split(":"))
            now_t = now_et().timetz().replace(tzinfo=None)
            if now_t > dtime(eh, em):
                log("runner", f"scan window closed ({end} ET) -- exiting loop")
                break

            sleep_for = max(5.0, args.every - (_time.monotonic() - started))
            _time.sleep(sleep_for)
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="trade-agent", description="AI trading analyst with a track record")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p, dates=True):
        p.add_argument("--mock", action="store_true", help="use canned LLM responses (no API cost)")
        p.add_argument("--offline", action="store_true", help="use canned/synthetic market data (no network)")
        p.add_argument("--force", action="store_true", help="run even on non-market days")
        p.add_argument("--no-email", action="store_true", help="skip email delivery")
        if dates:
            p.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (default: today ET)")

    p_morning = sub.add_parser("morning", help="generate today's trade plan")
    add_common(p_morning)
    p_morning.add_argument("--dry-run", action="store_true",
                           help="build and save the full prompt without calling the API")

    p_evening = sub.add_parser("evening", help="grade today's picks and extract lessons")
    add_common(p_evening)

    p_weekly = sub.add_parser("weekly", help="mine the ledger and rewrite the playbook")
    add_common(p_weekly)

    sub.add_parser("dashboard", help="regenerate the HTML dashboard")
    sub.add_parser("status", help="print ledger and performance snapshot")
    sub.add_parser("init", help="initialize directories, DB, credentials template")

    p_regrade = sub.add_parser("regrade", help="recompute outcomes under current sim semantics (explicit, opt-in)")
    p_regrade.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD -- recompute picks on/after this date")
    p_regrade.add_argument("--force", action="store_true", help="required if this would rewrite more than 50 rows")

    p_rregrade = sub.add_parser(
        "regrade-runner",
        help="re-price runner candidates under the current (honest) entry rule (explicit, opt-in)")
    p_rregrade.add_argument("--from", dest="from_date", required=True,
                            help="YYYY-MM-DD -- re-price sessions on/after this date")
    p_rregrade.add_argument("--force", action="store_true",
                            help="required if this would re-price more than 500 rows")

    p_sregrade = sub.add_parser(
        "regrade-shadow",
        help="re-price shadow-ledger rejections with the evening simulator (dry run unless --apply)")
    p_sregrade.add_argument("--from", dest="from_date", required=True,
                            help="YYYY-MM-DD -- re-price sessions on/after this date")
    p_sregrade.add_argument("--delete-ids", dest="delete_ids", default=None,
                            help="comma-separated rejection_ids to delete first (e.g. test fixture rows)")
    p_sregrade.add_argument("--progress-for", dest="progress_for", default=None,
                            help="comma-separated sessions whose missing Q14/Q1 shadow notes to add "
                                 "(default: none)")
    p_sregrade.add_argument("--apply", action="store_true",
                            help="write the changes (default is a dry run)")
    p_sregrade.add_argument("--force", action="store_true",
                            help="required if this would re-price more than 500 rows")

    p_ftr = sub.add_parser(
        "find-test-rows",
        help="read-only: list rows a test run may have written into this ledger")
    p_ftr.add_argument("--since", default="2026-09-24",
                       help="YYYY-MM-DD -- flag runs/grades written on/after this date for earlier sessions")

    p_rtr = sub.add_parser(
        "repair-test-rows",
        help="remove rows test runs wrote, rebuild rejections they deleted (dry run unless --apply)")
    p_rtr.add_argument("--since", default="2026-09-24",
                       help="YYYY-MM-DD -- plans/runs written on/after this date for earlier sessions are fixtures")
    p_rtr.add_argument("--rebuild", default=None,
                       help="comma-separated YYYY-MM-DD sessions whose rejections to rebuild from the stored plan")
    p_rtr.add_argument("--apply", action="store_true",
                       help="write the changes (default is a dry run)")

    p_upb = sub.add_parser(
        "undo-progress-backfill",
        help="remove the Q14/Q1 notes the first regrade-shadow over-added (dry run unless --apply)")
    p_upb.add_argument("--q14-from", dest="q14_from", default="2026-09-17",
                       help="YYYY-MM-DD -- first session whose evening recorded Q14 notes")
    p_upb.add_argument("--apply", action="store_true",
                       help="write the changes (default is a dry run)")

    p_regate = sub.add_parser(
        "regate-runner",
        help="relabel scan_version 1 runner rows that pass every v2 gate (dry run unless --apply)")
    p_regate.add_argument("--from", dest="from_date", required=True,
                          help="YYYY-MM-DD -- check sessions on/after this date")
    p_regate.add_argument("--apply", action="store_true",
                          help="write the relabel (default is a dry run)")

    p_runner = sub.add_parser(
        "runner",
        help="run one intraday Runner scan tick (scan -> select -> place)")
    add_common(p_runner)
    p_runner.add_argument("--at", type=str, default=None,
                          help="override the tick time, HH:MM ET (testing)")
    p_runner.add_argument("--loop", action="store_true",
                          help="keep ticking every --every seconds until the "
                               "scan window closes, then exit")
    p_runner.add_argument("--every", type=int, default=300,
                          help="seconds between ticks in --loop mode (default 300)")
    p_runner.add_argument("--no-orders", action="store_true",
                          help="scan and select but never place an order")

    p_rstatus = sub.add_parser(
        "runner-status",
        help="show today's Runner scan: candidates, picks, outcomes")
    p_rstatus.add_argument("--date", type=str, default=None,
                           help="YYYY-MM-DD (default: today ET)")

    p_rexits = sub.add_parser(
        "runner-exits",
        help="runner exit-policy replay report (read-only, zero API cost)")
    p_rexits.add_argument("--from", dest="from_date", type=str, default=None,
                          help="YYYY-MM-DD (default: last 120 days)")

    sub.add_parser(
        "runner-evidence",
        help="runner continuation after entry (drift in ATR by bucket) and selection "
             "coverage per session (read-only, zero API cost)")

    p_tlog = sub.add_parser(
        "traders-log",
        help="show (--show) or regenerate a session's trader's log (refuses an ungraded session)")
    p_tlog.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (default: today ET)")
    p_tlog.add_argument("--show", action="store_true", help="print the stored log, change nothing")
    p_tlog.add_argument("--mock", action="store_true",
                        help="use the canned draft (mocks/traders_log.json); stores nothing")

    p_pmon = sub.add_parser(
        "persona-monitor",
        help="persona on/off sessions: counts and distributions only (read-only, zero API cost)")
    p_pmon.add_argument("--sessions", type=int, default=10,
                        help="most recent N sessions per group (0 = all; default 10)")

    p_hyp = sub.add_parser(
        "hypotheses",
        help="the trader's hypothesis notebook: every idea, its count and verdict (read-only)")
    p_hyp.add_argument("--show", type=str, default=None,
                       help="one idea (H-...): its pre-registration, verdict and tagged trades")

    p_research = sub.add_parser("research", help="research integrity report (selection ablation + population baseline, zero API cost)")
    p_research.add_argument("--full", action="store_true", help="also render the full weekly research block (FDR looks, quarantine, paired arms)")

    args = parser.parse_args()
    cfg = load_config()
    cfg.mock = getattr(args, "mock", False)
    cfg.offline = getattr(args, "offline", False)
    cfg.dry_run = getattr(args, "dry_run", False)
    cfg.no_email = getattr(args, "no_email", False)

    run_date = None
    if getattr(args, "date", None):
        run_date = parse_date(args.date)

    if args.cmd == "init":
        cmd_init(cfg, args)
    elif args.cmd == "status":
        cmd_status(cfg, args)
    elif args.cmd == "dashboard":
        cmd_dashboard(cfg, args)
    elif args.cmd == "regrade":
        cmd_regrade(cfg, args)
    elif args.cmd == "regrade-runner":
        cmd_regrade_runner(cfg, args)
    elif args.cmd == "regrade-shadow":
        cmd_regrade_shadow(cfg, args)
    elif args.cmd == "find-test-rows":
        cmd_find_test_rows(cfg, args)
    elif args.cmd == "repair-test-rows":
        cmd_repair_test_rows(cfg, args)
    elif args.cmd == "undo-progress-backfill":
        cmd_undo_progress_backfill(cfg, args)
    elif args.cmd == "regate-runner":
        cmd_regate_runner(cfg, args)
    elif args.cmd == "research":
        cmd_research(cfg, args)
    elif args.cmd == "morning":
        from agent.loops import morning
        morning.run(cfg, run_date, force=args.force)
    elif args.cmd == "evening":
        from agent.loops import evening
        evening.run(cfg, run_date, force=args.force)
    elif args.cmd == "weekly":
        from agent.loops import weekly
        weekly.run(cfg, run_date)
    elif args.cmd == "runner":
        cmd_runner(cfg, args)
    elif args.cmd == "runner-exits":
        from datetime import timedelta
        from agent import runner_exits
        con = ledger.connect()
        try:
            since = (parse_date(args.from_date) if args.from_date
                     else today_et() - timedelta(days=120))
            rows = [r for r in ledger.runner_exit_replays_since(con, since)
                    if r.get("policy_version") == runner_exits.POLICY_VERSION
                    and r.get("r_planned") is not None]
            print(runner_exits.report_text(rows))
        finally:
            con.close()
    elif args.cmd == "runner-evidence":
        from agent import runner_research
        con = ledger.connect()
        try:
            print(runner_research.evidence_report_text(
                runner_research.current_drift_rows(con),
                runner_research.selection_coverage(con)))
        finally:
            con.close()
    elif args.cmd == "traders-log":
        cmd_traders_log(cfg, args, run_date)
    elif args.cmd == "persona-monitor":
        from agent import persona_monitor
        con = ledger.connect()
        try:
            n = args.sessions or None
            print(persona_monitor.render_text(persona_monitor.collect(con, n), n))
        finally:
            con.close()
    elif args.cmd == "hypotheses":
        from agent import hypotheses
        con = ledger.connect()
        try:
            print(hypotheses.render_text(con, run_date or today_et(), args.show))
        finally:
            con.close()
    elif args.cmd == "runner-status":
        from agent import runner_view
        con = ledger.connect()
        try:
            print(runner_view.render_text(con, run_date or today_et()))
        finally:
            con.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
