"""
Trade Agent — Web App (Vercel Python serverless entrypoint)

Single Flask app serving:
  - a password-gated dashboard (login/session)
  - three buttons that trigger the morning/evening/weekly loops synchronously
    and stream back the same log output the CLI would print
  - a JSON status API and a raw playbook endpoint

Deployed with vercel.json rewriting all paths to this one function. State
(ledger, playbook) lives in Neon Postgres via agent.ledger / agent.pgshim —
this process has no persistent local disk.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import traceback
from datetime import date, datetime
from decimal import Decimal
from functools import wraps

from flask import Flask, Response, redirect, request, session, url_for

# The "agent" package lives at the repo root, one level above /api.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import ledger  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.memory import read_playbook  # noqa: E402
from agent.util import today_et  # noqa: E402

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or "dev-only-insecure-key-set-SECRET_KEY-env-var"

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")


def _json_default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    return str(o)


def json_response(data: dict, status: int = 200) -> Response:
    return Response(json.dumps(data, default=_json_default, indent=2),
                    status=status, mimetype="application/json")


def _is_cron_request() -> bool:
    return bool(CRON_SECRET) and request.headers.get("Authorization") == f"Bearer {CRON_SECRET}"


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if _is_cron_request():
            return f(*args, **kwargs)
        if not APP_PASSWORD:
            # No password configured — fail closed rather than deploy silently open.
            return json_response(
                {"error": "APP_PASSWORD is not configured on the server — set it in Vercel project env vars."},
                500,
            )
        if not session.get("authed"):
            if request.path.startswith("/api/") or request.method == "POST":
                return json_response({"error": "unauthorized"}, 401)
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------- login

_LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Trade Agent — Login</title>
<style>
body{{font-family:-apple-system,Segoe UI,Arial,sans-serif;background:#0d1117;color:#e6edf3;
     display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:32px;width:320px}}
h1{{color:#58a6ff;font-size:20px;margin-top:0}}
input{{width:100%;padding:10px;margin:10px 0;background:#0d1117;border:1px solid #30363d;
       border-radius:6px;color:#e6edf3;box-sizing:border-box}}
button{{width:100%;padding:10px;background:#238636;border:none;border-radius:6px;color:white;
        font-weight:600;cursor:pointer}}
.err{{color:#f85149;font-size:13px}}
</style></head><body>
<div class="card">
  <h1>Trade Agent</h1>
  <form method="post">
    <input type="password" name="password" placeholder="Password" autofocus required>
    <button type="submit">Sign in</button>
  </form>
  {error}
</div></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if APP_PASSWORD and request.form.get("password") == APP_PASSWORD:
            session["authed"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        return _LOGIN_HTML.format(error="<p class='err'>Wrong password.</p>"), 401
    return _LOGIN_HTML.format(error="")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ----------------------------------------------------------------- dashboard

_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Trade Agent</title>
<style>
body{{font-family:-apple-system,Segoe UI,Arial,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:24px}}
h1,h2,h3{{color:#58a6ff}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:18px;margin-bottom:20px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border:1px solid #30363d;padding:6px 10px;text-align:left}}
th{{background:#21262d}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}}
.stat{{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:12px;text-align:center}}
.stat .v{{font-size:22px;font-weight:700}}
button{{padding:10px 18px;background:#238636;border:none;border-radius:6px;color:white;
        font-weight:600;cursor:pointer;margin-right:10px}}
button:disabled{{background:#30363d;cursor:not-allowed}}
button.secondary{{background:#30363d}}
label{{font-size:13px;color:#8b949e;margin-right:14px}}
pre{{white-space:pre-wrap;font-size:12px;background:#0d1117;border:1px solid #30363d;
     border-radius:6px;padding:12px;max-height:400px;overflow:auto}}
a{{color:#58a6ff}}
.top{{display:flex;justify-content:space-between;align-items:center}}
</style></head><body>
<div class="top">
  <h1>Trade Agent — Live Dashboard</h1>
  <a href="/logout">Sign out</a>
</div>
<p style="color:#8b949e">Loading...</p>
<div class="card">
  <h2>Run a loop</h2>
  <label><input type="checkbox" id="mock"> Test mode (mock LLM, no cost)</label>
  <label><input type="checkbox" id="offline"> Offline data (no market data calls)</label>
  <label><input type="checkbox" id="force"> Force (ignore market-day check)</label>
  <div style="margin-top:12px">
    <button onclick="runLoop('morning')">Run Morning</button>
    <button onclick="runLoop('evening')">Run Evening</button>
    <button onclick="runLoop('weekly')">Run Weekly</button>
    <button class="secondary" onclick="loadData()">Refresh</button>
  </div>
  <p id="running" style="display:none;color:#d29922">Running — this can take 1-4 minutes for the morning loop
     (extended thinking + web search). Do not close this tab.</p>
  <pre id="log" style="display:none"></pre>
</div>
<div id="app"></div>

<script>
async function loadData() {{
  const r = await fetch('/api/status');
  const d = await r.json();
  render(d);
}}

function render(d) {{
  const el = document.getElementById('app');
  const overall = d.overall || {{}};
  const plan = d.plan;
  let planHtml = "<p class='neutral'>No plan yet today.</p>";
  if (plan) {{
    if (plan.no_trade) {{
      planHtml = `<p><b>NO TRADE.</b> ${{plan.no_trade_reason || ''}}</p><p>${{plan.market_stance || ''}}</p>`;
    }} else {{
      let rows = (plan.picks || []).map(p => `<tr><td>${{p.ticker}}</td><td>${{p.direction}}</td>
        <td>${{p.setup_type||''}}</td><td>${{p.entry_price}}</td><td>${{p.stop_price}}</td>
        <td>${{p.target_price}}</td><td>${{p.size_r}}R</td><td>${{p.conviction}}</td>
        <td>${{p.thesis_final||''}}</td></tr>`).join('');
      planHtml = `<p>${{plan.market_stance||''}}</p><table><tr><th>Ticker</th><th>Dir</th><th>Setup</th>
        <th>Entry</th><th>Stop</th><th>Target</th><th>Size</th><th>Conv</th><th>Thesis</th></tr>${{rows}}</table>`;
    }}
  }}
  let setupRows = (d.by_setup || []).map(s => `<tr><td>${{s.dim}}</td><td>${{s.n}}</td><td>${{s.triggered}}</td>
    <td>${{s.win_rate!=null ? Math.round(s.win_rate*100)+'%' : '-'}}</td><td>${{s.avg_r ?? '-'}}</td>
    <td>${{s.total_weighted_r}}</td></tr>`).join('');
  let runsRows = (d.runs || []).map(r => `<tr><td>${{r.loop}}</td><td>${{r.run_date}}</td><td>${{r.status}}</td>
    <td>${{r.model||''}}</td><td>$${{(r.cost_usd||0).toFixed(3)}}</td></tr>`).join('');

  el.innerHTML = `
  <div class="card"><h2>Today's Plan — ${{d.today}}</h2>${{planHtml}}</div>
  <div class="card"><h2>Performance</h2>
    <div class="grid">
      <div class="stat"><div class="v">${{overall.picks_graded ?? 0}}</div>Graded trades</div>
      <div class="stat"><div class="v">${{overall.win_rate!=null ? Math.round(overall.win_rate*100)+'%' : '—'}}</div>Win rate</div>
      <div class="stat"><div class="v">${{overall.avg_r ?? '—'}}</div>Avg R</div>
      <div class="stat"><div class="v">${{overall.total_weighted_r ?? 0}}</div>Total weighted R</div>
      <div class="stat"><div class="v">$${{(d.equity||0).toLocaleString()}}</div>Paper equity</div>
      <div class="stat"><div class="v">${{d.streak||'—'}}</div>Streak</div>
    </div></div>
  <div class="card"><h2>By Setup</h2><table><tr><th>Setup</th><th>N</th><th>Triggered</th>
    <th>Win rate</th><th>Avg R</th><th>Total wR</th></tr>${{setupRows}}</table></div>
  <div class="card"><h2>Playbook v${{d.playbook_version}}</h2><pre>${{d.playbook.replace(/</g,'&lt;')}}</pre></div>
  <div class="card"><h2>Recent Runs</h2><table><tr><th>Loop</th><th>Date</th><th>Status</th>
    <th>Model</th><th>Cost</th></tr>${{runsRows}}</table></div>
  `;
}}

async function runLoop(name) {{
  document.getElementById('running').style.display = 'block';
  document.getElementById('log').style.display = 'block';
  document.getElementById('log').textContent = 'Starting ' + name + ' loop...';
  document.querySelectorAll('button').forEach(b => b.disabled = true);
  const params = new URLSearchParams({{
    mock: document.getElementById('mock').checked ? '1' : '0',
    offline: document.getElementById('offline').checked ? '1' : '0',
    force: document.getElementById('force').checked ? '1' : '0',
  }});
  try {{
    const r = await fetch('/run/' + name + '?' + params.toString(), {{method: 'POST'}});
    const d = await r.json();
    document.getElementById('log').textContent = (d.log || '') + (d.error ? '\\n\\nERROR: ' + d.error : '');
  }} catch (e) {{
    document.getElementById('log').textContent = 'Request failed: ' + e;
  }} finally {{
    document.querySelectorAll('button').forEach(b => b.disabled = false);
    document.getElementById('running').style.display = 'none';
    loadData();
  }}
}}

loadData();
</script>
</body></html>"""


@app.route("/")
@login_required
def dashboard():
    return _PAGE


# ------------------------------------------------------------------------ API

@app.route("/api/status")
@login_required
def api_status():
    con = ledger.connect()
    try:
        cfg = load_config()
        d = today_et()
        plan_row = ledger.plan_for_date(con, d)
        overall = ledger.overall_stats(con)
        by_setup = ledger.stats_by(con, "setup_type")
        streak = ledger.current_streak(con)
        curve = ledger.equity_curve(con, cfg.get("account_size", 100000), cfg.get("dollars_per_r", 500))
        runs = ledger.recent_runs(con, 15)
        playbook_text, version = read_playbook(con)
        equity = curve[-1]["equity"] if curve else cfg.get("account_size", 100000)
        streak_str = f"{streak['length']} {streak['kind']}" if streak.get("kind") not in (None, "none") else "—"
        return json_response({
            "today": d.isoformat(),
            "plan": plan_row["plan"] if plan_row else None,
            "overall": overall,
            "by_setup": by_setup,
            "streak": streak_str,
            "equity": equity,
            "runs": runs,
            "playbook": playbook_text,
            "playbook_version": version,
        })
    except Exception as e:  # noqa: BLE001
        return json_response({"error": str(e), "trace": traceback.format_exc()}, 500)
    finally:
        con.close()


@app.route("/api/playbook")
@login_required
def api_playbook():
    con = ledger.connect()
    try:
        text, version = read_playbook(con)
        return Response(text, mimetype="text/plain",
                        headers={"X-Playbook-Version": version})
    finally:
        con.close()


# ------------------------------------------------------------------ run loops

_VALID_LOOPS = {"morning", "evening", "weekly"}


def _execute_loop(loop_name: str, cfg, force: bool) -> dict:
    buf = io.StringIO()
    error = None
    try:
        with contextlib.redirect_stdout(buf):
            if loop_name == "morning":
                from agent.loops import morning
                morning.run(cfg, force=force)
            elif loop_name == "evening":
                from agent.loops import evening
                evening.run(cfg, force=force)
            else:
                from agent.loops import weekly
                weekly.run(cfg)
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
        buf.write(f"\n\n{traceback.format_exc()}")
    return {"loop": loop_name, "log": buf.getvalue(), "error": error}


@app.route("/run/<loop_name>", methods=["POST"])
@login_required
def run_loop(loop_name: str):
    """Manual button click from the dashboard — mock/offline/force are user-controlled."""
    if loop_name not in _VALID_LOOPS:
        return json_response({"error": f"unknown loop '{loop_name}'"}, 400)
    cfg = load_config()
    cfg.mock = request.args.get("mock") == "1"
    cfg.offline = request.args.get("offline") == "1"
    force = request.args.get("force") == "1"
    return json_response(_execute_loop(loop_name, cfg, force))


# Vercel Cron only sends GET requests, authenticated via the Authorization:
# Bearer <CRON_SECRET> header it attaches automatically. These always run for
# real (no mock/offline) and never force — they respect the market-day check.
#
# Vercel Hobby allows max TWO cron jobs (daily schedules only), so there is no
# separate weekly cron: the evening cron runs the weekly miner after Friday's
# grading session, which is equivalent (all of the week's data is graded by then).
#
# Cron schedules are fixed UTC with no DST awareness, so we sanity-check the
# current ET hour before spending API money. Windows are wide enough to accept
# the one-hour EST/EDT drift (morning fires 5:45 ET in winter, 6:45 in summer).
_CRON_WINDOWS = {"morning": (5, 10), "evening": (15, 19)}


@app.route("/cron/<loop_name>", methods=["GET"])
@login_required
def cron_loop(loop_name: str):
    if loop_name not in ("morning", "evening"):
        return json_response({"error": f"unknown cron loop '{loop_name}'"}, 400)
    if not _is_cron_request():
        return json_response({"error": "this endpoint is for Vercel Cron only"}, 403)

    from agent.util import now_et
    now = now_et()
    lo, hi = _CRON_WINDOWS[loop_name]
    if not (lo <= now.hour <= hi):
        return json_response({
            "loop": loop_name, "skipped": True,
            "reason": f"current ET hour {now.hour} outside expected window {lo}-{hi} "
                      "(likely a DST schedule drift) — refusing to run automatically",
        })

    cfg = load_config()
    result = _execute_loop(loop_name, cfg, force=False)

    # Friday evening doubles as the weekly mining session.
    if loop_name == "evening" and now.weekday() == 4 and not result.get("error"):
        weekly_result = _execute_loop("weekly", load_config(), force=False)
        result["weekly"] = weekly_result

    return json_response(result)


# Local dev convenience: `python api/index.py` runs a normal Flask dev server.
if __name__ == "__main__":
    app.run(debug=True, port=5000)
