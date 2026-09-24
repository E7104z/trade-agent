"""
Delivery: email (optional) + a static HTML dashboard rewritten after every loop.

The dashboard is a single self-contained HTML file (no build step) showing:
  - today's plan (or no-trade reasoning)
  - the playbook's current version
  - a running equity curve of paper P&L
  - recent runs / cost log
"""
from __future__ import annotations

import json
import os
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import duckdb

from . import dashboard, ledger
from .config import Config, REPORTS_DIR
from .mascot import MASCOT_MARKER, mascot_embed
from .memory import read_playbook
from .util import log


def send_email(cfg: Config, subject: str, html_body: str) -> bool:
    if not cfg.email_configured:
        log("delivery", "Email not configured or disabled — skipping send")
        return False
    s = cfg.smtp
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = s["from_addr"] or s["username"]
    msg["To"] = ", ".join(s["to_addrs"])
    msg.attach(MIMEText(html_body, "html"))
    try:
        with smtplib.SMTP(s["host"], int(s.get("port", 587))) as server:
            server.starttls()
            server.login(s["username"], s["password"])
            server.sendmail(msg["From"], s["to_addrs"], msg.as_string())
        log("delivery", f"Email sent to {s['to_addrs']}")
        return True
    except Exception as e:
        log("delivery", f"Email send failed: {e}")
        return False


def morning_email_html(plan_date: date, plan: dict, regime_md: str) -> str:
    if plan.get("no_trade"):
        picks_html = f"<p><b>NO TRADE TODAY.</b> {plan.get('no_trade_reason','')}</p>"
    else:
        rows = []
        for p in plan.get("picks", []):
            entry_type = (p.get("entry_plan") or {}).get("type") or p.get("entry_type") or "open"
            rows.append(f"""
            <tr>
              <td>{p['ticker']}</td><td>{p['direction']}</td><td>{p.get('setup_type','')}</td>
              <td>{entry_type} @ {p.get('entry_price')}</td><td>{p.get('stop_price')}</td><td>{p.get('target_price')}</td>
              <td>{p.get('size_r')}R</td><td>{p.get('conviction')}</td>
              <td>{p.get('thesis_final','')}</td>
            </tr>""")
        picks_html = f"""
        <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:monospace;font-size:13px">
          <tr style="background:#eee"><th>Ticker</th><th>Dir</th><th>Setup</th><th>Entry (type @ price)</th><th>Stop</th>
          <th>Target</th><th>Size</th><th>Conv</th><th>Thesis</th></tr>
          {''.join(rows)}
        </table>"""
    return f"""
    <html><body style="font-family:sans-serif">
      <h2>Trade Agent — Morning Plan — {plan_date.isoformat()}</h2>
      <p><i>{plan.get('market_stance','')}</i></p>
      {picks_html}
      <h3>Regime</h3>
      <pre style="white-space:pre-wrap;font-family:monospace">{regime_md}</pre>
      <p style="color:#888;font-size:12px">Automated output. Not investment advice. Paper-trade tracked only.</p>
    </body></html>
    """


def evening_email_html(plan_date: date, review: dict, outcomes_summary: str) -> str:
    rows = "".join(
        f"<tr><td>{r['ticker']}</td><td>{r['grade']}</td><td>{r['why']}</td></tr>"
        for r in review.get("pick_reviews", [])
    )
    return f"""
    <html><body style="font-family:sans-serif">
      <h2>Trade Agent — Evening Review — {plan_date.isoformat()}</h2>
      <p><b>Plan grade: {review.get('plan_grade','?')}</b></p>
      <p>{review.get('plan_notes','')}</p>
      <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-size:13px">
        <tr style="background:#eee"><th>Ticker</th><th>Grade</th><th>Why</th></tr>{rows}
      </table>
      <h3>Lessons extracted</h3>
      <ul>{''.join(f"<li>{l['lesson']}</li>" for l in review.get('lessons', []))}</ul>
      <pre style="white-space:pre-wrap;font-family:monospace;font-size:12px">{outcomes_summary}</pre>
    </body></html>
    """


def weekly_email_html(result: dict, violations: list[dict] | None = None) -> str:
    findings = "".join(f"<li>{f['finding']} (n={f['n']}, {f['strength']})</li>" for f in result.get("key_findings", []))
    edge = result.get("current_best_edge", "")
    edge_html = f'<h3>Current best edge</h3><p style="font-weight:bold">{edge}</p>' if edge else ""
    viol_html = ""
    if violations:
        import html as _html
        blocks = [v for v in violations if v.get("severity") == "block"]
        rejected = any(v.get("action") == "rewrite_rejected" for v in violations)
        head = (f"{len(blocks)} blocking / {len(violations) - len(blocks)} warning finding(s)"
                + (" — REWRITE REJECTED, prior playbook kept" if rejected else
                   " — repaired before the playbook was written"))
        items = "".join(
            f"<li><b>{_html.escape(str(v.get('checker')))}</b> [{v.get('severity')}/"
            f"{v.get('action')}] {_html.escape(str(v.get('message') or '')[:220])}</li>"
            for v in violations[:5])
        viol_html = f"<h3>Write-time integrity</h3><p>{head}</p><ul>{items}</ul>"
    return f"""
    <html><body style="font-family:sans-serif">
      <h2>Trade Agent — Weekly Review</h2>
      <p>{result.get('summary_for_email','')}</p>
      {edge_html}
      {viol_html}
      <h3>Key findings</h3><ul>{findings}</ul>
      <h3>Changelog</h3><pre style="white-space:pre-wrap">{result.get('changelog','')}</pre>
    </body></html>
    """


# --------------------------------------------------------------------- HTML dashboard

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><title>Trade Agent Dashboard</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; background:#0d1117; color:#e6edf3; margin:0; padding:24px; }}
  h1,h2,h3 {{ color:#58a6ff; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:10px; padding:18px; margin-bottom:20px; }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  th,td {{ border:1px solid #30363d; padding:6px 10px; text-align:left; }}
  th {{ background:#21262d; }}
  .win {{ color:#3fb950; }} .loss {{ color:#f85149; }} .neutral {{ color:#8b949e; }}
  .err {{ color:#f85149; font-size:12px; max-width:340px; overflow:hidden;
          text-overflow:ellipsis; white-space:nowrap; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:12px; font-size:12px; background:#21262d; }}
  #chart {{ width:100%; height:260px; }}
  pre {{ white-space:pre-wrap; font-size:13px; color:#c9d1d9; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; }}
  .stat {{ background:#0d1117; border:1px solid #30363d; border-radius:8px; padding:12px; text-align:center; }}
  .stat .v {{ font-size:22px; font-weight:700; }}
  nav.dash-nav {{ position:sticky; top:0; z-index:20; background:#0d1117; border-bottom:1px solid #30363d;
    padding:8px 0; margin:0 0 8px; display:flex; gap:16px; flex-wrap:wrap; }}
  nav.dash-nav a {{ color:#8b949e; text-decoration:none; font-size:13px; font-weight:600; padding:4px 10px; border-radius:6px; }}
  nav.dash-nav a:hover {{ color:#e6edf3; background:#161b22; }}
  .section-banner {{ display:flex; align-items:baseline; gap:12px; margin:28px 0 14px; padding-bottom:8px; border-bottom:2px solid #30363d; }}
  .section-banner .tag {{ background:#238636; color:#fff; font-size:11px; font-weight:700; letter-spacing:.08em; padding:3px 9px; border-radius:12px; }}
  .section-banner h2 {{ margin:0; color:#e6edf3; }}
  .section-banner .sub {{ color:#8b949e; font-size:12px; }}
  .pb-h {{ margin:18px 0 8px; }} .pb-list {{ margin:6px 0 12px 22px; padding:0; }}
  .pb-list li {{ margin:4px 0; }} .pb-p {{ margin:8px 0; }}
  .pb-hr {{ border:none; border-top:1px solid #30363d; margin:16px 0; }}
  .pb-table {{ width:auto; min-width:320px; margin:10px 0; }}
</style>
</head>
<body>
<h1>Trade Agent — Live Dashboard</h1>
<p class="neutral">Generated {generated_at} ET &middot; Playbook v{playbook_version}</p>
<!--ARBY-->
<nav class="dash-nav">
  <a href="#sec-loop">Loop</a>
  <a href="#sec-runner">Runner</a>
  <a href="#sec-conclusions">Conclusions</a>
  <a href="#sec-playbook">Playbook</a>
  <a href="#sec-runs">Runs</a>
</nav>

<div class="section-banner" id="sec-loop"><span class="tag">LOOP</span><h2>The daily loop</h2><span class="sub">morning plan &rarr; evening grade &rarr; weekly mine</span></div>
<div class="card">
  <h2>Today's Plan — {plan_date}</h2>
  {plan_html}
</div>

<div class="card">
  <h2>Performance</h2>
  <div class="grid">
    <div class="stat"><div class="v">{n_trades}</div>Graded trades</div>
    <div class="stat"><div class="v">{win_rate}</div>Win rate</div>
    <div class="stat"><div class="v">{avg_r}</div>Avg R</div>
    <div class="stat"><div class="v">{total_r}</div>Total weighted R</div>
    <div class="stat"><div class="v">${equity}</div>Paper equity</div>
    <div class="stat"><div class="v">{streak}</div>Current streak</div>
  </div>
  <canvas id="chart"></canvas>
</div>

<div class="card">
  <h2>Performance by Setup</h2>
  {setup_table}
</div>

<div class="card">
  <h2>Shadow Ledger — Rejected Candidates ({plan_date})</h2>
  {shadow_html}
</div>

<div class="section-banner" id="sec-runner"><span class="tag">RUNNER</span><h2>The intraday runner lane</h2><span class="sub">scan &rarr; select &rarr; grade against the shadow</span></div>
{runner_html}

<div class="section-banner" id="sec-conclusions"><span class="tag">CONCLUSIONS</span><h2>What the desk has concluded &amp; changed</h2><span class="sub">the numbers that actually drive future playbooks</span></div>
{conclusions_html}

<div class="section-banner" id="sec-playbook"><span class="tag">PLAYBOOK</span><h2>The playbook</h2><span class="sub">v{playbook_version}</span></div>
<div class="card">{playbook_html}</div>

<div class="section-banner" id="sec-runs"><span class="tag">RUNS</span><h2>Loop run history</h2></div>
<div class="card">
  <h2>Recent Runs</h2>
  {runs_table}
</div>

<script>
const points = {equity_json};
const c = document.getElementById('chart');
c.width = c.clientWidth; c.height = 260;
const ctx = c.getContext('2d');
if (points.length > 1) {{
  const xs = points.map((_,i)=>i), ys = points.map(p=>p.equity);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const pad = 20;
  ctx.strokeStyle = '#3fb950'; ctx.lineWidth = 2; ctx.beginPath();
  points.forEach((p,i)=>{{
    const x = pad + (c.width-2*pad) * i/(points.length-1);
    const y = c.height - pad - (c.height-2*pad) * (p.equity-minY)/((maxY-minY)||1);
    i===0 ? ctx.moveTo(x,y) : ctx.lineTo(x,y);
  }});
  ctx.stroke();
}} else {{
  ctx.fillStyle = '#8b949e'; ctx.fillText('Not enough graded days yet for an equity curve.', 10, 20);
}}
</script>
</body></html>
"""


def _error_cell(error: str | None, limit: int = 90) -> str:
    """Render a runs.error value as a table cell.

    Empty/None renders an EMPTY cell -- never the strings "None" or "undefined",
    which is what an unguarded f-string interpolation would produce for the
    overwhelming majority of rows.

    The text is HTML-escaped because it is raw exception text: tracebacks carry
    <module>, generic types like dict<str, int>, and & in URLs, any of which
    would otherwise break the surrounding table or silently swallow the part of
    the message that mattered. The full string goes in title= so hovering shows
    everything without widening the column.
    """
    if not error:
        return ""
    safe = (str(error).replace("&", "&amp;")
                      .replace("<", "&lt;")
                      .replace(">", "&gt;")
                      .replace('"', "&quot;"))
    short = safe if len(safe) <= limit else safe[:limit] + "&hellip;"
    return f'<span class="err" title="{safe}">{short}</span>'


def _esc(value) -> str:
    """HTML-escape any scalar for safe interpolation into the static page. All
    lesson/verdict/thesis text originates from LLM output, so it must be escaped
    the same way the shared playbook renderer escapes markdown before any tags
    are added back."""
    return (str(value).replace("&", "&amp;")
                      .replace("<", "&lt;")
                      .replace(">", "&gt;")
                      .replace('"', "&quot;"))


def _fmt(v, ndigits: int | None = None, dash: str = "—") -> str:
    """Render an optional number as text, using `dash` for None."""
    if v is None:
        return dash
    return f"{v:.{ndigits}f}" if ndigits is not None else str(v)


def _conclusions_html(cc: dict) -> str:
    """Server-rendered "Conclusions & Changes" section, mirroring the hosted
    dashboard's client-side rendering. The data comes from
    dashboard.conclusions_payload, so the two surfaces cannot derive different
    numbers; only the HTML assembly differs (static file vs browser)."""
    best = cc.get("best_edge") or ""
    best_html = (f"<p style='font-size:15px'><b>{_esc(best)}</b></p>" if best
                 else "<p class='neutral'>Not enough triggered trades yet to declare "
                      "a current best edge (needs &ge;3 per setup).</p>")

    lessons = cc.get("lessons") or []
    if lessons:
        lessons_html = ("<table><tr><th>Lesson</th><th>Setup</th><th>Confidence</th>"
                        "<th>+/-</th><th>Status</th></tr>" + "".join(
            f"<tr><td>{_esc(l.get('lesson',''))}</td><td>{_esc(l.get('setup_type') or '')}</td>"
            f"<td>{_esc(l.get('confidence') or '')}</td>"
            f"<td>+{l.get('confirmations') or 0} / -{l.get('contradictions') or 0}</td>"
            f"<td>{_esc(l.get('status') or '')}</td></tr>"
            for l in lessons
        ) + "</table>")
    else:
        lessons_html = "<p class='neutral'>No active lessons yet.</p>"

    questions = cc.get("open_questions") or []
    if questions:
        questions_html = ("<table><tr><th>Question</th><th>Progress</th><th>Events</th>"
                          "<th>Last</th><th>Recent notes</th></tr>" + "".join(
            f"<tr><td>Q{q.get('question_id')}</td><td>{q.get('total')}</td><td>{q.get('events')}</td>"
            f"<td>{q.get('last_event') or ''}</td>"
            f"<td>{_esc(' · '.join(q.get('recent_notes') or []))}</td></tr>"
            for q in questions
        ) + "</table>")
    else:
        questions_html = "<p class='neutral'>No open-question progress recorded yet.</p>"

    calibration = cc.get("calibration") or []
    if calibration:
        cal_rows = []
        for c in calibration:
            if c.get("suppressed"):
                wr = f"<span title=\"suppressed: n &lt; {c.get('min_n')}\">—</span>"
                ar = "—"
            else:
                wr = f"{c['win_rate']:.0%}" if c.get("win_rate") is not None else "—"
                ar = _fmt(c.get("avg_r"))
            cal_rows.append(
                f"<tr><td>{_esc(c.get('conviction') or '')}</td><td>{c.get('n')}</td>"
                f"<td>{wr}</td><td>{ar}</td></tr>")
        calibration_html = ("<table><tr><th>Stated conviction</th><th>N</th>"
                            "<th>Win rate</th><th>Avg R</th></tr>" + "".join(cal_rows) + "</table>")
    else:
        calibration_html = "<p class='neutral'>No calibration data yet.</p>"

    mg = cc.get("management") or {}
    if mg:
        management_html = (
            "<table><tr><th>Metric</th><th>Value</th></tr>"
            f"<tr><td>Triggered trades</td><td>{mg.get('triggered') or 0}</td></tr>"
            f"<tr><td>Avg realized R</td><td>{_fmt(mg.get('avg_r'))} (n={mg.get('avg_r_n') or 0})</td></tr>"
            f"<tr><td>Avg same-name open&rarr;close (baseline)</td>"
            f"<td>{_fmt(mg.get('avg_baseline_r'))} (n={mg.get('avg_baseline_r_n') or 0})</td></tr>"
            f"<tr><td><b>Edge vs baseline (paired)</b></td>"
            f"<td><b>{_fmt(mg.get('edge_vs_baseline_r'))}</b> R (n={mg.get('edge_vs_baseline_n') or 0})</td></tr>"
            f"<tr><td><b>Management edge vs hold-to-close (paired)</b></td>"
            f"<td><b>{_fmt(mg.get('management_edge_r'))}</b> R (n={mg.get('management_edge_n') or 0})</td></tr>"
            f"<tr><td>Stopped</td><td>{mg.get('stopped') or 0}"
            + (f" ({mg['stopped_then_target']} hit target after)" if mg.get("stopped_then_target") else "")
            + "</td></tr>"
            f"<tr><td>Avg EOD R on stopped trades</td><td>{_fmt(mg.get('avg_eod_r_on_stopped'))}</td></tr>"
            f"<tr><td>Untriggered (fussy entries) w/ baseline</td>"
            f"<td>{mg.get('untriggered_with_baseline') or 0} &mdash; walked away from "
            f"{_fmt(mg.get('avg_baseline_r_untriggered'))}R</td></tr></table>")
    else:
        management_html = "<p class='neutral'>No management (counterfactual) data yet.</p>"

    selection_edge = cc.get("selection_edge") or []
    if selection_edge:
        selection_edge_html = ("<table><tr><th>Week ending</th><th>Sessions</th><th>Edge R</th>"
                               "<th>p</th><th>Verdict</th></tr>" + "".join(
            f"<tr><td>{r.get('week_ending') or ''}</td>"
            f"<td>{r.get('sessions') if r.get('sessions') is not None else ''}</td>"
            f"<td>{_fmt(r.get('edge_r'))}</td>"
            f"<td>{_fmt(r.get('p_value'), 4) if r.get('p_value') is not None else '—'}</td>"
            f"<td>{_esc(r.get('verdict') or '')}</td></tr>"
            for r in selection_edge
        ) + "</table>")
    else:
        selection_edge_html = ("<p class='neutral'>No weekly selection-edge history yet "
                               "(the weekly miner writes this).</p>")

    return "".join([
        "<div class='card'><h3>Current best edge</h3>", best_html, "</div>",
        "<div class='card'><h3>Active lessons</h3>", lessons_html, "</div>",
        "<div class='card'><h3>Open questions</h3>", questions_html, "</div>",
        "<div class='card'><h3>Calibration (does stated conviction track realized R?)</h3>",
        calibration_html, "</div>",
        "<div class='card'><h3>Management quality (stop/target machinery vs hold-to-close)</h3>",
        management_html, "</div>",
        "<div class='card'><h3>Runner selection edge (weekly, FDR-corrected)</h3>",
        selection_edge_html, "</div>",
    ])


def _shadow_html(rejections: list[dict]) -> str:
    """Today's rejected candidates and what they would have done — the shadow
    ledger, the counterfactual the whole "did picking help?" question rests on."""
    if not rejections:
        return "<p class='neutral'>Nothing rejected today &mdash; every candidate that "
        "survived analysis made the plan.</p>"
    rows = []
    for r in rejections:
        outcome = r.get("eod_outcome")
        if outcome:
            fr = r.get("forgone_r")
            outcome = f"{outcome} ({'+' if fr is not None and fr >= 0 else ''}{fr}R)" if fr is not None else outcome
        else:
            outcome = "ungraded"
        rows.append(
            f"<tr><td>{_esc(r.get('ticker',''))}</td><td>{_esc(r.get('direction') or '')}</td>"
            f"<td>{_esc(r.get('setup_type') or '')}</td>"
            f"<td>{_esc(r.get('entry_type') or '')} @ {_fmt(r.get('entry_price'))}</td>"
            f"<td>{_fmt(r.get('stop_price'))}</td><td>{_fmt(r.get('target_price'))}</td>"
            f"<td>{_esc(r.get('conviction') or '—')}</td>"
            f"<td>{_esc(r.get('reason') or '')}</td><td>{_esc(r.get('thesis') or '')}</td>"
            f"<td>{_esc(outcome)}</td></tr>")
    return ("<table><tr><th>Ticker</th><th>Dir</th><th>Setup</th><th>Entry (type @ price)</th>"
            "<th>Stop</th><th>Target</th><th>Conv</th><th>Kill reason</th><th>Thesis</th>"
            "<th>Shadow outcome</th></tr>" + "".join(rows) + "</table>")


def _plan_html(plan: dict | None) -> str:
    if not plan:
        return "<p class='neutral'>No plan recorded yet.</p>"
    if plan.get("no_trade"):
        return f"<p><b>NO TRADE.</b> {plan.get('no_trade_reason','')}</p><p class='neutral'>{plan.get('market_stance','')}</p>"
    rows = []
    for p in plan.get("picks", []):
        entry_type = (p.get("entry_plan") or {}).get("type") or p.get("entry_type") or "open"
        rows.append(
            f"<tr><td>{p['ticker']}</td><td>{p['direction']}</td><td>{p.get('setup_type','')}</td>"
            f"<td>{entry_type} @ {p.get('entry_price')}</td><td>{p.get('stop_price')}</td><td>{p.get('target_price')}</td>"
            f"<td>{p.get('size_r')}R</td><td>{p.get('conviction')}</td><td>{p.get('thesis_final','')}</td></tr>"
        )
    table = (
        "<table><tr><th>Ticker</th><th>Dir</th><th>Setup</th><th>Entry (type @ price)</th><th>Stop</th>"
        "<th>Target</th><th>Size</th><th>Conv</th><th>Thesis</th></tr>" + "".join(rows) + "</table>"
    )
    return f"<p class='neutral'>{plan.get('market_stance','')}</p>{table}"


def render_dashboard(con: duckdb.DuckDBPyConnection, cfg: Config, plan_date: date | None) -> str:
    playbook_text, version = read_playbook(con)
    plan_row = ledger.plan_for_date(con, plan_date) if plan_date else None
    overall = ledger.overall_stats(con)
    setup_stats = ledger.stats_by(con, "setup_type")
    streak = ledger.current_streak(con)
    curve = ledger.equity_curve(con, cfg.get("account_size", 100000), cfg.get("dollars_per_r", 500))
    runs = ledger.recent_runs(con, 12)

    # Runner lane card. Deliberately NOT keyed off plan_date: the runner
    # lane runs on its own schedule (a local Task Scheduler job, not the
    # morning loop) and can easily be several days ahead of the last morning
    # run -- pinning it to plan_date silently showed a stale or empty runner
    # card while the actual latest session had real data. Independently look
    # up the most recent runner_sessions row instead. Returns "" when the
    # lane has never run at all, so the dashboard omits the section rather
    # than rendering an empty shell.
    try:
        from . import runner_view
        latest_runner_row = con.execute(
            "SELECT max(session_date) FROM runner_sessions").fetchone()
        latest_runner_date = (latest_runner_row[0] if latest_runner_row
                              and latest_runner_row[0] else date.today())
        runner_html = runner_view.render_html(con, latest_runner_date)
    except Exception:
        runner_html = ""

    # Room dressing: live wires from the ledger so Arby's man cave reflects the
    # desk's actual state (not decorative fixtures). All deterministic.
    regimes = ledger.recent_regimes(con, 5)
    regime = regimes[0] if regimes else None
    drawdown = ledger.trailing_weighted_r(con, int(cfg.get("drawdown_window", 10)))
    shadow = ledger.shadow_summary(con, 20)
    calibration = ledger.calibration_aggregate(con)
    management = ledger.management_quality(con)

    # Deterministic "current best edge": the setup with the best realized avg R
    # over at least 3 triggered trades. The LLM never derives this number.
    best_edge = dashboard.best_edge(setup_stats)

    setup_rows = "".join(
        f"<tr><td>{s['dim']}</td><td>{s['n']}</td><td>{s['triggered']}</td>"
        f"<td>{f'{s['win_rate']:.0%}' if s['win_rate'] is not None else '-'}</td>"
        f"<td>{s['avg_r'] if s['avg_r'] is not None else '-'}</td>"
        f"<td>{s['total_weighted_r']}</td></tr>"
        for s in setup_stats
    )
    setup_table = ("<table><tr><th>Setup</th><th>N</th><th>Triggered</th><th>Win rate</th>"
                  "<th>Avg R</th><th>Total wR</th></tr>" + setup_rows + "</table>") if setup_stats else "<p class='neutral'>No graded trades yet.</p>"

    # Error column: runs.error was already fetched and already dropped on the
    # floor here, exactly as in the hosted renderer. A failed run that shows
    # only a red word is indistinguishable from any other failed run, which is
    # what turned a billing outage into a multi-hour investigation.
    runs_rows = "".join(
        f"<tr><td>{r['loop']}</td><td>{r['run_date']}</td><td>{r['status']}</td>"
        f"<td>{r['model']}</td><td>{r['input_tokens']}</td><td>{r['output_tokens']}</td>"
        f"<td>${r['cost_usd']:.3f}</td><td>{_error_cell(r.get('error'))}</td></tr>"
        for r in runs
    )
    runs_table = ("<table><tr><th>Loop</th><th>Date</th><th>Status</th><th>Model</th>"
                 "<th>In tok</th><th>Out tok</th><th>Cost</th><th>Error</th></tr>"
                 + runs_rows + "</table>") if runs else "<p class='neutral'>No runs logged yet.</p>"

    equity_now = curve[-1]["equity"] if curve else cfg.get("account_size", 100000)

    # Conclusions & Changes section: identical data to the hosted dashboard via
    # dashboard.conclusions_payload, assembled server-side for the static file.
    conclusions = dashboard.conclusions_payload(con)
    conclusions_html = _conclusions_html(conclusions)

    # Shadow ledger card: today's rejected candidates (the runner's counterfactual).
    rejections = ledger.rejections_for_date(con, plan_date) if plan_date else []
    shadow_html = _shadow_html(rejections)

    # Playbook: shared markdown -> HTML renderer (XSS-safe) and shared bullet
    # extractor, so static and hosted show the same list of rules.
    playbook_bullets = dashboard.playbook_bullets(playbook_text)
    playbook_html = dashboard.playbook_html(playbook_text)

    html = _PAGE_TEMPLATE.format(
        runner_html=runner_html,
        generated_at=str(date.today()),
        playbook_version=version,
        plan_date=plan_date.isoformat() if plan_date else "—",
        plan_html=_plan_html(plan_row["plan"] if plan_row else None),
        n_trades=overall["picks_graded"],
        win_rate=f"{overall['win_rate']:.0%}" if overall["win_rate"] is not None else "—",
        avg_r=overall["avg_r"] if overall["avg_r"] is not None else "—",
        total_r=overall["total_weighted_r"],
        equity=f"{equity_now:,.0f}",
        streak=f"{streak['length']} {streak['kind']}" if streak["kind"] != "none" else "—",
        setup_table=setup_table,
        shadow_html=shadow_html,
        conclusions_html=conclusions_html,
        playbook_html=playbook_html,
        runs_table=runs_table,
        equity_json=json.dumps(curve),
    )
    # Arby the desk mascot — injected after .format() so its JS braces stay
    # untouched. The static file can't fetch /api/status, so the data the
    # widget needs is inlined as boot data.
    mascot_boot = {
        "today": plan_date.isoformat() if plan_date else None,
        "plan": plan_row["plan"] if plan_row else None,
        "overall": overall,
        "by_setup": setup_stats,
        "streak_detail": streak,
        "equity": equity_now,
        "equity_curve": curve[-120:],
        "playbook_version": version,
        "playbook_bullets": playbook_bullets,
        "runs": runs[:5],
        "regime": regime,
        "regimes": regimes,
        "drawdown": drawdown,
        "shadow": shadow,
        "calibration": calibration,
        "management": management,
        "best_edge": best_edge,
    }
    html = html.replace(MASCOT_MARKER, mascot_embed(mascot_boot))
    out_path = REPORTS_DIR / "dashboard.html"
    if os.environ.get("DATABASE_URL"):
        # Hosted (Vercel) deployment: filesystem is read-only outside /tmp.
        # The Flask app renders its own dashboard from the ledger directly;
        # skip the static-file write instead of crashing a loop after its
        # (expensive) LLM calls already completed.
        log("delivery", "Hosted mode — skipping static dashboard.html write")
        return html
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    log("delivery", f"Dashboard written to {out_path}")
    return str(out_path)
