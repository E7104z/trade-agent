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

from . import ledger
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
            rows.append(f"""
            <tr>
              <td>{p['ticker']}</td><td>{p['direction']}</td><td>{p.get('setup_type','')}</td>
              <td>{p.get('entry_price')}</td><td>{p.get('stop_price')}</td><td>{p.get('target_price')}</td>
              <td>{p.get('size_r')}R</td><td>{p.get('conviction')}</td>
              <td>{p.get('thesis_final','')}</td>
            </tr>""")
        picks_html = f"""
        <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:monospace;font-size:13px">
          <tr style="background:#eee"><th>Ticker</th><th>Dir</th><th>Setup</th><th>Entry</th><th>Stop</th>
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


def weekly_email_html(result: dict) -> str:
    findings = "".join(f"<li>{f['finding']} (n={f['n']}, {f['strength']})</li>" for f in result.get("key_findings", []))
    edge = result.get("current_best_edge", "")
    edge_html = f'<h3>Current best edge</h3><p style="font-weight:bold">{edge}</p>' if edge else ""
    return f"""
    <html><body style="font-family:sans-serif">
      <h2>Trade Agent — Weekly Review</h2>
      <p>{result.get('summary_for_email','')}</p>
      {edge_html}
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
  .badge {{ display:inline-block; padding:2px 8px; border-radius:12px; font-size:12px; background:#21262d; }}
  #chart {{ width:100%; height:260px; }}
  pre {{ white-space:pre-wrap; font-size:13px; color:#c9d1d9; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; }}
  .stat {{ background:#0d1117; border:1px solid #30363d; border-radius:8px; padding:12px; text-align:center; }}
  .stat .v {{ font-size:22px; font-weight:700; }}
</style>
</head>
<body>
<h1>Trade Agent — Live Dashboard</h1>
<p class="neutral">Generated {generated_at} ET &middot; Playbook v{playbook_version}</p>
<!--ARBY-->

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
  <h2>Active Playbook (v{playbook_version})</h2>
  <pre>{playbook_text}</pre>
</div>

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


def _plan_html(plan: dict | None) -> str:
    if not plan:
        return "<p class='neutral'>No plan recorded yet.</p>"
    if plan.get("no_trade"):
        return f"<p><b>NO TRADE.</b> {plan.get('no_trade_reason','')}</p><p class='neutral'>{plan.get('market_stance','')}</p>"
    rows = []
    for p in plan.get("picks", []):
        rows.append(
            f"<tr><td>{p['ticker']}</td><td>{p['direction']}</td><td>{p.get('setup_type','')}</td>"
            f"<td>{p.get('entry_price')}</td><td>{p.get('stop_price')}</td><td>{p.get('target_price')}</td>"
            f"<td>{p.get('size_r')}R</td><td>{p.get('conviction')}</td><td>{p.get('thesis_final','')}</td></tr>"
        )
    table = (
        "<table><tr><th>Ticker</th><th>Dir</th><th>Setup</th><th>Entry</th><th>Stop</th>"
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
    best_edge = ""
    candidates = [s for s in setup_stats if (s.get("triggered") or 0) >= 3 and s.get("avg_r") is not None]
    if candidates:
        top = max(candidates, key=lambda s: s["avg_r"])
        best_edge = f"{top['dim']} · avg {top['avg_r']}R over {top['triggered']} triggered"

    setup_rows = "".join(
        f"<tr><td>{s['dim']}</td><td>{s['n']}</td><td>{s['triggered']}</td>"
        f"<td>{f'{s['win_rate']:.0%}' if s['win_rate'] is not None else '-'}</td>"
        f"<td>{s['avg_r'] if s['avg_r'] is not None else '-'}</td>"
        f"<td>{s['total_weighted_r']}</td></tr>"
        for s in setup_stats
    )
    setup_table = ("<table><tr><th>Setup</th><th>N</th><th>Triggered</th><th>Win rate</th>"
                  "<th>Avg R</th><th>Total wR</th></tr>" + setup_rows + "</table>") if setup_stats else "<p class='neutral'>No graded trades yet.</p>"

    runs_rows = "".join(
        f"<tr><td>{r['loop']}</td><td>{r['run_date']}</td><td>{r['status']}</td>"
        f"<td>{r['model']}</td><td>{r['input_tokens']}</td><td>{r['output_tokens']}</td>"
        f"<td>${r['cost_usd']:.3f}</td></tr>"
        for r in runs
    )
    runs_table = ("<table><tr><th>Loop</th><th>Date</th><th>Status</th><th>Model</th>"
                 "<th>In tok</th><th>Out tok</th><th>Cost</th></tr>" + runs_rows + "</table>") if runs else "<p class='neutral'>No runs logged yet.</p>"

    equity_now = curve[-1]["equity"] if curve else cfg.get("account_size", 100000)

    html = _PAGE_TEMPLATE.format(
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
        playbook_text=playbook_text.replace("<", "&lt;").replace(">", "&gt;"),
        runs_table=runs_table,
        equity_json=json.dumps(curve),
    )
    # Arby the desk mascot — injected after .format() so its JS braces stay
    # untouched. The static file can't fetch /api/status, so the data the
    # widget needs is inlined as boot data.
    playbook_bullets = [
        ln.strip().lstrip("-*• ").replace("**", "").strip()
        for ln in playbook_text.splitlines()
        if ln.strip().startswith(("- ", "* ", "• "))
    ][:4]
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
