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

__all__ = ["session_summary", "render_text", "render_html", "top_movers"]


def _f(v, digits=2, dash="-"):
    if v is None:
        return dash
    try:
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return str(v)


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

    rows = []
    for c in cands:
        o = outcomes.get(c["ticker"]) or {}
        p = pick_by_ticker.get(c["ticker"])
        rows.append({
            "ticker": c["ticker"],
            "selected": bool(o.get("selected")),
            "rank": o.get("selection_rank"),
            "detection_et": (c["first_pass_et"].strftime("%H:%M")
                             if c.get("first_pass_et") else ""),
            "price": c.get("detection_price"),
            "stop": c.get("stop_price"),
            "target": c.get("target_price"),
            "stop_dist": c.get("stop_dist"),
            "rvol": c.get("rvol"),
            "vwap_pct": c.get("vwap_pct_above"),
            "tc": c.get("trend_consistency"),
            "change_pct": c.get("change_pct"),
            "float_m": c.get("float_shares"),
            "conviction": p[2] if p else None,
            "thesis": (p[7] if p else "") or "",
            "r_multiple": o.get("r_multiple"),
            "exit_reason": o.get("exit_reason"),
            "triggered": o.get("triggered"),
        })

    return {
        "date": d,
        "session": sess,
        "gates": gates,
        "rows": rows,
        "n_candidates": len(rows),
        "n_selected": sum(1 for r in rows if r["selected"]),
        "n_picks": len(picks),
        "orders": [{"ticker": o[0], "qty": o[1], "status": o[2], "fill": o[3]}
                   for o in orders],
        "graded": [r for r in rows if r["r_multiple"] is not None],
    }


def top_movers(s: dict, n: int = 10) -> list[dict]:
    """The n best-performing GRADED candidates today, by realized R,
    regardless of whether the selector picked them.

    This is a read-only diagnostic, not a training signal. One day's top
    movers are ~n out of hundreds of candidates -- far too few to safely
    re-derive a selection rule from without fitting noise (see the mismatch
    on 2026-09-17: the actual top 10 movers and the 10 selected candidates
    had ZERO overlap, and mean R for "selected" was actually LOWER than the
    field average -- a real, single-session result, not evidence of a broken
    selector). That comparison, done properly across many CONTESTED sessions
    with FDR correction, already lives in
    agent/runner_research.selection_edge() and feeds the weekly miner, which
    is the only thing allowed to turn a pattern into a playbook rule.

    This function exists so a person can eyeball "did we even scan/see what
    won today" without waiting for that -- nothing here writes back into the
    selector prompt or any rule table.
    """
    graded = [r for r in s["rows"] if r["r_multiple"] is not None]
    return sorted(graded, key=lambda r: r["r_multiple"], reverse=True)[:n]


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
    return f"Healthy: {uni} symbols scanned, {s['n_candidates']} candidates.{note}"


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
        if sess.get("selection_skipped_reason"):
            out.append(f"selection: {sess['selection_skipped_reason']}")

    if not s["rows"]:
        return "\n".join(out)

    out += ["", f"{s['n_candidates']} candidates, {s['n_selected']} selected, "
                f"{s['n_picks']} picks written, {len(s['orders'])} live orders", ""]
    out.append(f"{'':3}{'TICKER':8}{'DET':6}{'PRICE':>9}{'STOP':>9}{'TARGET':>9}"
               f"{'RVOL':>7}{'VWAP%':>7}{'TC':>6}{'CHG%':>8}{'R':>7}  EXIT")
    out.append("-" * 96)
    for r in sorted(s["rows"], key=lambda x: (not x["selected"],
                                              x["rank"] or 99, x["ticker"])):
        mark = f"#{r['rank']}" if r["selected"] else "  "
        out.append(
            f"{mark:3}{r['ticker']:8}{r['detection_et']:6}"
            f"{_f(r['price']):>9}{_f(r['stop']):>9}{_f(r['target']):>9}"
            f"{_f(r['rvol']):>7}{_f(r['vwap_pct']):>7}{_f(r['tc'],1):>6}"
            f"{_f(r['change_pct']):>8}{_f(r['r_multiple'],2):>7}  "
            f"{r['exit_reason'] or ''}")

    sel = [r for r in s["rows"] if r["selected"] and r["thesis"]]
    if sel:
        out += ["", "WHY THESE WERE PICKED:"]
        for r in sorted(sel, key=lambda x: x["rank"] or 99):
            out.append(f"  #{r['rank']} {r['ticker']} (conv {r['conviction']}): "
                       f"{r['thesis'][:110]}")

    graded = s["graded"]
    if graded:
        picked = [r["r_multiple"] for r in graded if r["selected"]]
        passed = [r["r_multiple"] for r in graded if not r["selected"]]
        out += ["", "OUTCOMES (every candidate is priced, traded or not):"]
        if picked:
            out.append(f"  selected   n={len(picked):3}  mean R "
                       f"{sum(picked)/len(picked):+.3f}")
        if passed:
            out.append(f"  passed over n={len(passed):3}  mean R "
                       f"{sum(passed)/len(passed):+.3f}")
        if picked and passed:
            edge = sum(picked) / len(picked) - sum(passed) / len(passed)
            out.append(f"  selection edge today: {edge:+.3f} R "
                       f"(one session - not evidence on its own)")

        movers = top_movers(s, 10)
        if movers:
            overlap = sum(1 for r in movers if r["selected"])
            out += ["", f"TOP {len(movers)} MOVERS TODAY (by realized R, "
                        f"regardless of selection) - {overlap} of "
                        f"{len(movers)} were selected:"]
            for r in movers:
                mark = f"#{r['rank']} PICKED" if r["selected"] else "not picked"
                out.append(f"  {r['ticker']:8}{_f(r['r_multiple'], 2):>7}R  "
                           f"({r['exit_reason'] or ''})  {mark}")
            out.append("  (one day's winners are noise as a training signal --")
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
        rmul = r["r_multiple"]
        rcol = "#8b949e" if rmul is None else ("#3fb950" if rmul > 0 else "#f85149")
        rows_html.append(
            f"<tr><td>{sel}</td><td><b>{r['ticker']}</b></td>"
            f"<td>{r['detection_et']}</td><td>{_f(r['price'])}</td>"
            f"<td>{_f(r['stop'])}</td><td>{_f(r['target'])}</td>"
            f"<td>{_f(r['rvol'])}</td><td>{_f(r['vwap_pct'])}</td>"
            f"<td>{_f(r['tc'],1)}</td><td>{_f(r['change_pct'])}</td>"
            f"<td style='color:{rcol}'>{_f(rmul)}</td>"
            f"<td>{r['exit_reason'] or ''}</td></tr>")

    movers = top_movers(s, 10)
    movers_html = ""
    if movers:
        overlap = sum(1 for r in movers if r["selected"])
        mv_rows = []
        for r in movers:
            tag = (f"<span style='color:#3fb950;font-weight:700'>"
                   f"#{r['rank']} picked</span>" if r["selected"]
                   else "<span style='color:#8b949e'>not picked</span>")
            rmul = r["r_multiple"]
            rcol = "#3fb950" if rmul > 0 else "#f85149"
            mv_rows.append(
                f"<tr><td><b>{r['ticker']}</b></td>"
                f"<td style='color:{rcol}'>{_f(rmul)}</td>"
                f"<td>{r['exit_reason'] or ''}</td><td>{tag}</td></tr>")
        movers_html = f"""
  <h3 style="margin-top:18px">Top {len(movers)} movers today (by realized R)</h3>
  <p style="color:#8b949e;font-size:12px">
    {overlap} of {len(movers)} were selected. This is a same-day eyeball check,
    not a training signal — one day's winners are far too few (~{len(movers)}
    out of {s['n_candidates']}) to safely re-derive a selection rule from.
    The FDR-corrected version of this comparison, across many sessions, feeds
    the weekly miner instead.
  </p>
  <table><tr><th>Ticker</th><th>R</th><th>Exit</th><th></th></tr>
    {''.join(mv_rows)}
  </table>"""

    return f"""
<div class="card">
  <h2>Runner lane — {d}</h2>
  <p style="color:{colour};font-weight:600">{health}</p>
  <div class="grid">
    <div class="stat"><div class="v">{s['session'].get('universe_size') or 0}</div>scanned</div>
    <div class="stat"><div class="v">{s['n_candidates']}</div>candidates</div>
    <div class="stat"><div class="v">{s['n_selected']}</div>selected</div>
    <div class="stat"><div class="v">{len(s['orders'])}</div>live orders</div>
  </div>
  {movers_html}
  <h3 style="margin-top:18px">All candidates</h3>
  <table style="margin-top:6px">
    <tr><th></th><th>Ticker</th><th>Det ET</th><th>Price</th><th>Stop</th>
        <th>Target</th><th>RVOL</th><th>VWAP%</th><th>TC</th><th>Chg%</th>
        <th>R</th><th>Exit</th></tr>
    {''.join(rows_html)}
  </table>
  <p style="color:#8b949e;font-size:12px;margin-top:10px">
    Every candidate is priced by the evening simulator, selected or not — that
    is what makes "did picking help?" answerable. Rows marked #N were chosen.
  </p>
</div>"""
