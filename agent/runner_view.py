"""
Read-only views of the Runner lane, for the CLI and the dashboard.

WHY THIS IS SEPARATE FROM THE WEB DASHBOARD

The hosted dashboard at /api/runner/* reads the production Postgres. The runner
lane runs from the local machine (neither GitHub Actions nor Vercel Hobby can
drive a five-minute cron on this account), so its evidence lands in the LOCAL
DuckDB. Pointing a browser at the hosted dashboard therefore shows nothing,
which looks exactly like a lane that isn't working.

These renderers read whatever ledger connection they are handed, so the same
code serves both: `run.py runner-status` against the local DB, and the hosted
dashboard against Postgres once the lane runs there too. `run.py dashboard`
also uses render_html() to build a local static HTML file (agent/delivery.py),
so nothing here requires a Postgres/Neon account to see visually.

Nothing here computes a number. Every value is read back from the tables the
scan and the selector already wrote, so the view cannot disagree with the
evidence.
"""
from __future__ import annotations

import json
from datetime import date

from . import ledger
from .buckets import index_tape

__all__ = ["session_summary", "render_text", "render_html", "biggest_moves",
           "outcome_summary", "universe_line", "construction_summary"]


def _f(v, digits=2, dash="-"):
    if v is None:
        return dash
    try:
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return str(v)


def _hhmm(ts) -> str:
    return ts.strftime("%H:%M") if hasattr(ts, "strftime") else ""


def _realized(o: dict):
    """Planned-risk R (r_planned) -- the R every runner statistic reads. A
    legacy row graded before r_planned existed falls back to r_multiple."""
    v = o.get("r_planned")
    return v if v is not None else o.get("r_multiple")


def session_summary(con, d: date) -> dict:
    """Everything known about one runner session, straight from the tables."""
    sess = ledger.runner_session(con, d) or {}
    cands = ledger.runner_candidates_for_date(con, d)
    outcomes = {o["ticker"]: o for o in ledger.runner_outcomes_for_date(con, d)}

    picks = con.execute(
        """SELECT pick_id, ticker, conviction, size_r, entry_price, stop_price,
                  target_price, thesis
           FROM picks WHERE plan_date = ? AND setup_type = 'dto_runner'
           ORDER BY pick_id""", [d]).fetchall()
    pick_by_ticker = {p[1]: p for p in picks}

    try:
        orders = con.execute(
            """SELECT ticker, qty, status, entry_fill_price
               FROM live_orders WHERE session_date = ? ORDER BY ticker""",
            [d]).fetchall()
    except Exception:
        orders = []

    gates = {}
    if sess.get("gates_unavailable"):
        try:
            gates = json.loads(sess["gates_unavailable"])
        except (TypeError, ValueError):
            gates = {}

    universe = {}
    if sess.get("universe_stats"):
        try:
            universe = json.loads(sess["universe_stats"]) or {}
        except (TypeError, ValueError):
            universe = {}

    rows = []
    for c in cands:
        o = outcomes.get(c["ticker"]) or {}
        p = pick_by_ticker.get(c["ticker"])
        rows.append({
            "ticker": c["ticker"],
            "selected": bool(o.get("selected")),
            "rank": o.get("selection_rank"),
            # When the VWAP/EMA conditions first held (walked back) and when
            # the scan saw it and formed the plan -- two different moments.
            "detection_et": _hhmm(c.get("first_pass_et")),
            "seen_et": _hhmm(c.get("scan_pass_et")),
            "industry": c.get("industry"),
            "price": c.get("detection_price"),
            "stop": c.get("stop_price"),
            "target": c.get("target_price"),
            "stop_dist": c.get("stop_dist"),
            "stop_dist_atr": o.get("stop_dist_atr"),
            "trend_side_200d": o.get("trend_side_200d"),
            "rvol": c.get("rvol"),
            "vwap_pct": c.get("vwap_pct_above"),
            "tc": c.get("trend_consistency"),
            "change_pct": c.get("change_pct"),
            "float_m": c.get("float_shares"),
            "conviction": p[2] if p else None,
            "thesis": (p[7] if p else "") or "",
            # `r` is realized R in PLANNED-risk units. r_multiple (fill-to-stop
            # risk) is kept off the page: a fill a few cents above a stale stop
            # printed +5..7R "wins" on 2026-10-01 that were ~2R on the plan.
            "r": _realized(o) if o.get("triggered") else None,
            "post_move_atr": o.get("post_move_atr"),
            # At entry, and after it (schema v28): how stale the plan was, how
            # extended above VWAP, the index tape it was bought into, and
            # whether price kept going up from the first enterable price.
            "staleness_min": o.get("staleness_min"),
            "ext_vwap_atr": o.get("ext_vwap_atr"),
            "index_tape": (index_tape(o.get("spy_vwap_pct"), o.get("qqq_vwap_pct"))
                           if o.get("entry_px") is not None else None),
            "drift_atr": o.get("drift_atr"),
            "exit_reason": o.get("exit_reason"),
            "invalid_reason": o.get("invalid_reason"),
            "triggered": o.get("triggered"),
            "scan_version": c.get("scan_version"),
            "sim_version": o.get("sim_version"),
        })

    return {
        "date": d,
        "session": sess,
        "gates": gates,
        "universe": universe,
        "rows": rows,
        "n_candidates": len(rows),
        "n_selected": sum(1 for r in rows if r["selected"]),
        "n_picks": len(picks),
        "orders": [{"ticker": o[0], "qty": o[1], "status": o[2], "fill": o[3]}
                   for o in orders],
        "graded": [r for r in rows if r["r"] is not None],
    }


def outcome_summary(s: dict) -> dict:
    """Selected vs field for ONE session, in planned-risk R, with the counts
    that say how little one session is.

    `units` are (session, industry) clusters (agent/clusters.py): the rows of
    one industry on one morning are one observation, which is how the weekly
    search counts them. Invalid fills are counted, never averaged in.
    """
    from .clusters import unit_count

    def side(rows: list[dict]) -> dict:
        trig = [r for r in rows if r["r"] is not None]
        vals = [float(r["r"]) for r in trig]
        unit_rows = [{"session_date": s["date"], "ticker": r["ticker"],
                      "industry": r.get("industry")} for r in trig]
        return {"n": len(vals), "units": unit_count(unit_rows),
                "mean_r": (sum(vals) / len(vals)) if vals else None,
                "invalid": sum(1 for r in rows if r.get("exit_reason") == "invalid_fill"),
                "untriggered": sum(1 for r in rows if r.get("triggered") is False
                                   and r.get("exit_reason") != "invalid_fill")}

    sel = side([r for r in s["rows"] if r["selected"]])
    field = side([r for r in s["rows"] if not r["selected"]])
    delta = (sel["mean_r"] - field["mean_r"]
             if sel["mean_r"] is not None and field["mean_r"] is not None else None)
    return {"selected": sel, "field": field, "delta_r": delta}


def construction_summary(con, s: dict) -> dict:
    """ONE session's plan vs the construction arms, and its continuation after
    entry (specs/RUNNER_TRADE_CONSTRUCTION_v1.md). Read back from the replay
    table and the outcome rows; descriptive, never evidence on its own -- the
    arms are tested as per-session paired ΔR across many sessions, weekly."""
    from . import runner_exits
    d = s["date"]
    try:
        rows = [r for r in ledger.runner_exit_replays_since(con, d)
                if r.get("session_date") == d
                and r.get("policy_version") == runner_exits.POLICY_VERSION
                and r.get("r_planned") is not None]
    except Exception:
        rows = []
    questions = {p.name: p.question for p in runner_exits.POLICIES}
    arms = []
    for name in (runner_exits.REFERENCE,) + runner_exits.ARMS:
        vals = [float(r["r_planned"]) for r in rows if r["policy"] == name]
        arms.append({"policy": name, "question": questions.get(name, ""), "n": len(vals),
                     "mean_r": (sum(vals) / len(vals)) if vals else None,
                     "win_rate": (sum(v > 0 for v in vals) / len(vals)) if vals else None})

    drift = [r for r in s["rows"] if r.get("drift_atr") is not None]

    def cell(rs: list[dict]) -> dict:
        v = [float(r["drift_atr"]) for r in rs]
        return {"n": len(v), "up": sum(1 for x in v if x > 0),
                "mean_drift_atr": (sum(v) / len(v)) if v else None}

    by_tape: dict[str, list[dict]] = {}
    for r in drift:
        by_tape.setdefault(r.get("index_tape") or "?", []).append(r)
    return {"arms": arms,
            "policy_version": runner_exits.POLICY_VERSION,
            "continuation": {**cell(drift),
                             "by_tape": [{"tape": k, **cell(v)}
                                         for k, v in sorted(by_tape.items())]}}


def biggest_moves(s: dict, n: int = 10) -> list[dict]:
    """The n candidates that moved most AFTER the system could act, in ATR:
    (session close - plan price) / ATR14, from the evening simulator.

    This replaced a ranking by `change_pct`. That column is the move that had
    ALREADY happened when the scan flagged the name (detection price vs the
    previous close) -- a feature the selector reads, not an outcome -- so the
    list was really the most-extended names at detection, and "not picked"
    beside them implied misses the data did not show (2026-10-01: LITG, a 2x
    ETF, topped it at +16.9% and exited at -0.60R; CNXC at +10.5% stopped
    out). change_pct is still shown beside each row as context.

    Ungraded rows (no close yet) are absent: an after-detection move is an
    outcome. Read-only, one session -- a same-day eyeball check, never a
    training signal; the FDR-corrected comparison across many contested
    sessions is runner_research.selection_edge in the weekly miner.
    """
    ranked = [r for r in s["rows"] if r.get("post_move_atr") is not None]
    return sorted(ranked, key=lambda r: r["post_move_atr"], reverse=True)[:n]


def universe_line(s: dict) -> str:
    """What each lane filter removed from the universe this session."""
    u = s.get("universe") or {}
    if not u:
        return ""
    bits = []
    inst = u.get("dropped_instrument") or {}
    if inst:
        bits.append("instrument filter removed " + ", ".join(
            f"{v} {k}" for k, v in sorted(inst.items(), key=lambda kv: -kv[1])))
    liq = u.get("dropped_liquidity_by_source") or {}
    if liq:
        bits.append(f"liquidity floor removed {sum(liq.values())} "
                    f"({', '.join(f'{k} {v}' for k, v in sorted(liq.items()))})")
    if u.get("instruments_classified") is False:
        bits.append("NOT instrument-classified (asset list unavailable)")
    return "; ".join(bits)


def _health_line(s: dict) -> str:
    """One line that distinguishes a stand-down from an outage.

    This is the whole point of runner_sessions: a zero-candidate day with a
    full universe is the market declining to offer setups, and a zero-candidate
    day with an empty universe or a failed feed is a broken pipeline. They must
    never read the same.
    """
    sess, gates = s["session"], s["gates"]
    if not sess:
        return "NO SCAN RECORDED for this date - the lane never ran."
    uni = sess.get("universe_size") or 0
    if sess.get("provider_degraded"):
        why = []
        if gates.get("bars_failed"):
            why.append("bar fetch failed")
        if uni == 0:
            why.append("empty universe")
        if gates.get("feed_downgraded"):
            why.append(f"feed downgraded to {gates.get('feed')}")
        return "DEGRADED - " + (", ".join(why) or "see gates") + \
               ". Treat a thin candidate list as an outage, not a stand-down."
    # A structural outage check that does NOT depend on provider_degraded
    # having been set. Sessions recorded before that flag existed - or by any
    # future path that forgets to set it - would otherwise be reported as clean
    # stand-downs. If essentially every scanned symbol returned no bars, no
    # condition was ever evaluated, so there is nothing to stand down from.
    no_bars = sess.get("rejected_no_bars") or 0
    if uni and no_bars >= uni * 0.9:
        return (f"DEGRADED - {no_bars} of {uni} symbols returned NO BARS. "
                f"No condition was evaluated; this is a data outage, not a "
                f"stand-down.")
    if s["n_candidates"] == 0:
        evaluated = uni - no_bars - (sess.get("rejected_too_few_bars") or 0)
        return (f"Genuine stand-down: {uni} scanned, {evaluated} had enough "
                f"bars to evaluate, none met the conditions. Not a failure.")
    feed = gates.get("feed", "?")
    note = ""
    if gates.get("feed_downgraded"):
        note = (f"  [feed={feed}: IEX prints are a few percent of consolidated "
                f"volume, so RVOL is recorded null and this session is not "
                f"comparable to SIP sessions]")

    # The scan can be perfectly healthy while SELECTION is dead. On 2026-09-22
    # this line read "Healthy: 4289 symbols scanned, 78 candidates" with zero
    # picks, because the selector had failed on
    #   selection_failed: Anthropic API failed after 3 attempts: ... credit
    #   balance is too low
    # An outage reported as health is the precise failure this function's
    # docstring exists to forbid -- "none were selected" and "the selector
    # never ran" are opposite facts about the day.
    skipped = str(sess.get("selection_skipped_reason") or "")
    if skipped.startswith("selection_failed:"):
        return (f"DEGRADED - scan healthy ({uni} scanned, {s['n_candidates']} "
                f"candidates) but SELECTION FAILED: {skipped[len('selection_failed:'):].strip()} "
                f"No picks were made because the selector errored, not because "
                f"the candidates were rejected.{note}")

    return f"Healthy: {uni} symbols scanned, {s['n_candidates']} candidates.{note}"


def _exit_label(r: dict) -> str:
    if r.get("exit_reason") == "invalid_fill":
        return f"invalid ({r.get('invalid_reason') or 'fill'})"
    return r.get("exit_reason") or ""


def render_text(con, d: date) -> str:
    s = session_summary(con, d)
    sess = s["session"]
    out = [f"=== RUNNER LANE - {d} ===", "", _health_line(s)]

    if sess:
        rej = {k.replace("rejected_", ""): sess.get(k) or 0
               for k in sess if k.startswith("rejected_")}
        out.append(
            f"scan passes {sess.get('scan_passes') or 0}  |  "
            + "  ".join(f"{k} {v}" for k, v in rej.items() if v))
        uline = universe_line(s)
        if uline:
            out.append(f"universe: {uline}")
        if sess.get("selection_skipped_reason"):
            out.append(f"selection: {sess['selection_skipped_reason']}")

    if not s["rows"]:
        return "\n".join(out)

    out += ["", f"{s['n_candidates']} candidates, {s['n_selected']} selected, "
                f"{s['n_picks']} picks written, {len(s['orders'])} live orders", ""]
    out.append(f"{'':3}{'TICKER':8}{'HELD':6}{'SEEN':6}{'PRICE':>9}{'STOP':>9}{'TARGET':>9}"
               f"{'RVOL':>7}{'VWAP%':>7}{'TC':>6}{'CHG%':>8}{'SDA':>6}{'R':>7}{'MOVE':>7}  EXIT")
    out.append("-" * 124)
    for r in sorted(s["rows"], key=lambda x: (not x["selected"],
                                              x["rank"] or 99, x["ticker"])):
        mark = f"#{r['rank']}" if r["selected"] else "  "
        out.append(
            f"{mark:3}{r['ticker']:8}{r['detection_et']:6}{r['seen_et']:6}"
            f"{_f(r['price']):>9}{_f(r['stop']):>9}{_f(r['target']):>9}"
            f"{_f(r['rvol']):>7}{_f(r['vwap_pct']):>7}{_f(r['tc'],1):>6}"
            f"{_f(r['change_pct']):>8}{_f(r['stop_dist_atr']):>6}"
            f"{_f(r['r'],2):>7}{_f(r['post_move_atr'],2):>7}  "
            f"{_exit_label(r)}")
    out.append("HELD = conditions first held (walked back); SEEN = the scan tick that "
               "formed the plan. R = realized R in planned-risk units; MOVE = (close - "
               "plan price) / ATR14.")

    sel = [r for r in s["rows"] if r["selected"] and r["thesis"]]
    if sel:
        out += ["", "WHY THESE WERE PICKED:"]
        for r in sorted(sel, key=lambda x: x["rank"] or 99):
            out.append(f"  #{r['rank']} {r['ticker']} (conv {r['conviction']}): "
                       f"{r['thesis'][:110]}")

    if s["graded"]:
        o = outcome_summary(s)
        out += ["", "OUTCOMES (every candidate is priced, traded or not; planned-risk R):"]
        for label, side in (("selected   ", o["selected"]), ("passed over", o["field"])):
            if side["n"]:
                out.append(f"  {label} n={side['n']:3} ({side['units']} industry units)  "
                           f"mean R {side['mean_r']:+.3f}  | invalid fills {side['invalid']}, "
                           f"untriggered {side['untriggered']}")
        if o["delta_r"] is not None:
            out.append(f"  selection edge today: {o['delta_r']:+.3f} R "
                       f"(one session - not evidence on its own)")

        cs = construction_summary(con, s)
        cont = cs["continuation"]
        if cont["n"]:
            out += ["", f"CONTINUATION AFTER ENTRY (close vs first enterable price, ATR): "
                        f"{cont['up']} of {cont['n']} closed above entry, mean drift "
                        f"{_f(cont['mean_drift_atr'], 2)} ATR"]
            for t in cont["by_tape"]:
                out.append(f"  index tape {t['tape']:<11} n={t['n']:4}  up {t['up']:4}  "
                           f"mean drift {_f(t['mean_drift_atr'], 2)} ATR")
        if any(a["n"] for a in cs["arms"]):
            out += ["", f"CONSTRUCTION ARMS (replay family v{cs['policy_version']}, same "
                        "candidates; R in each arm's own risk unit):"]
            for a in cs["arms"]:
                win = "n/a" if a["win_rate"] is None else f"{100 * a['win_rate']:.0f}%"
                out.append(f"  {a['policy']:<23} n={a['n']:4}  mean R {_f(a['mean_r'], 3):>7}"
                           f"  win {win:>4}  {a['question'][:70]}")
            out.append("  (one session; the weekly search tests per-session paired dR, "
                       "never win rate)")

        moves = biggest_moves(s, 10)
        if moves:
            overlap = sum(1 for r in moves if r["selected"])
            out += ["", f"BIGGEST {len(moves)} MOVES AFTER DETECTION (close vs plan "
                        f"price, in ATR) - {overlap} of {len(moves)} were selected:"]
            for r in moves:
                mark = f"#{r['rank']} PICKED" if r["selected"] else "not picked"
                out.append(f"  {r['ticker']:8}{_f(r['post_move_atr'], 2):>7} ATR  "
                           f"R {_f(r['r'], 2):>6}  ({_exit_label(r) or 'no fill'})  "
                           f"chg-at-detection {_f(r['change_pct'], 1)}%  {mark}")
            out.append("  (one day's moves are noise as a training signal --")
            out.append("   this is for eyeballing, not for retuning selection.")
            out.append("   See the weekly miner for the FDR-corrected version.)")
    return "\n".join(out)


def render_html(con, d: date) -> str:
    """A dashboard card. Returns '' when the lane never ran, so the dashboard
    simply omits the section rather than showing an empty shell."""
    s = session_summary(con, d)
    if not s["session"] and not s["rows"]:
        return ""

    health = _health_line(s)
    bad = health.startswith(("DEGRADED", "NO SCAN"))
    colour = "#f85149" if bad else ("#d29922" if "stand-down" in health else "#3fb950")

    rows_html = []
    for r in sorted(s["rows"], key=lambda x: (not x["selected"],
                                              x["rank"] or 99, x["ticker"])):
        sel = ("<span style='color:#3fb950;font-weight:700'>"
               f"#{r['rank']}</span>" if r["selected"] else "")
        rv = r["r"]
        rcol = "#8b949e" if rv is None else ("#3fb950" if rv > 0 else "#f85149")
        rows_html.append(
            f"<tr><td>{sel}</td><td><b>{r['ticker']}</b></td>"
            f"<td>{r['industry'] or ''}</td>"
            f"<td>{r['detection_et']}</td><td>{r['seen_et']}</td><td>{_f(r['price'])}</td>"
            f"<td>{_f(r['stop'])}</td><td>{_f(r['target'])}</td>"
            f"<td>{_f(r['rvol'])}</td><td>{_f(r['vwap_pct'])}</td>"
            f"<td>{_f(r['tc'],1)}</td><td>{_f(r['change_pct'])}</td>"
            f"<td>{_f(r['stop_dist_atr'])}</td>"
            f"<td style='color:{rcol}'>{_f(rv)}</td><td>{_f(r['post_move_atr'])}</td>"
            f"<td>{_exit_label(r)}</td></tr>")

    moves = biggest_moves(s, 10)
    moves_html = ""
    if moves:
        overlap = sum(1 for r in moves if r["selected"])
        mv_rows = []
        for r in moves:
            tag = (f"<span style='color:#3fb950;font-weight:700'>"
                   f"#{r['rank']} picked</span>" if r["selected"]
                   else "<span style='color:#8b949e'>not picked</span>")
            mv = r["post_move_atr"]
            ccol = "#3fb950" if (mv or 0) >= 0 else "#f85149"
            mv_rows.append(
                f"<tr><td><b>{r['ticker']}</b></td>"
                f"<td style='color:{ccol}'>{_f(mv, 2)}</td><td>{_f(r['r'])}</td>"
                f"<td>{_exit_label(r) or 'no fill'}</td><td>{_f(r['change_pct'], 1)}%</td>"
                f"<td>{tag}</td></tr>")
        moves_html = f"""
  <h3 style="margin-top:18px">Biggest {len(moves)} moves after detection</h3>
  <p style="color:#8b949e;font-size:12px">
    Ranked by (session close − plan price) ÷ ATR14: the move after the system
    could act. {overlap} of {len(moves)} were selected. Chg% is the move that had
    already happened at detection — context, not outcome. A same-day eyeball
    check, not a training signal; the FDR-corrected comparison across many
    sessions feeds the weekly miner instead.
  </p>
  <table><tr><th>Ticker</th><th>Move (ATR)</th><th>R</th><th>Exit</th><th>Chg% at detection</th><th></th></tr>
    {''.join(mv_rows)}
  </table>"""

    summary_html = ""
    if s["graded"]:
        o = outcome_summary(s)
        bits = []
        for label, side in (("Selected", o["selected"]), ("Field", o["field"])):
            if side["n"]:
                bits.append(f"{label}: {side['mean_r']:+.2f}R over {side['n']} fills "
                            f"({side['units']} industry units; {side['invalid']} invalid "
                            f"fills not counted)")
        if bits:
            summary_html = ("<p style='font-size:12px'>" + " · ".join(bits)
                            + " — planned-risk R, one session.</p>")

    uline = universe_line(s)
    universe_html = (f'<p style="color:#8b949e;font-size:12px">Universe: {uline}</p>'
                     if uline else "")
    return f"""
<div class="card">
  <h2>Runner lane — {d}</h2>
  <p style="color:{colour};font-weight:600">{health}</p>
  {universe_html}
  <div class="grid">
    <div class="stat"><div class="v">{s['session'].get('universe_size') or 0}</div>scanned</div>
    <div class="stat"><div class="v">{s['n_candidates']}</div>candidates</div>
    <div class="stat"><div class="v">{s['n_selected']}</div>selected</div>
    <div class="stat"><div class="v">{len(s['orders'])}</div>live orders</div>
  </div>
  {summary_html}
  {moves_html}
  <h3 style="margin-top:18px">All candidates</h3>
  <table style="margin-top:6px">
    <tr><th></th><th>Ticker</th><th>Industry</th><th>Held</th><th>Seen</th><th>Price</th>
        <th>Stop</th><th>Target</th><th>RVOL</th><th>VWAP%</th><th>TC</th><th>Chg%</th>
        <th>StopΔATR</th><th>R</th><th>Move (ATR)</th><th>Exit</th></tr>
    {''.join(rows_html)}
  </table>
  <p style="color:#8b949e;font-size:12px;margin-top:10px">
    Every candidate is priced by the evening simulator, selected or not — that
    is what makes "did picking help?" answerable. Held = when the conditions
    first held; Seen = the scan tick that formed the plan (price, stop, target)
    and the earliest an order could go in. R is realized R in planned-risk
    units. Rows marked #N were chosen.
  </p>
</div>"""
