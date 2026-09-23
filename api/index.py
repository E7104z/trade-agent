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
import time
import traceback
from datetime import date, datetime
from decimal import Decimal
from functools import wraps

from flask import Flask, Response, redirect, request, session, url_for

# The "agent" package lives at the repo root, one level above /api.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import ledger  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.mascot import MASCOT_MARKER, mascot_embed  # noqa: E402
from agent.memory import read_playbook  # noqa: E402
from agent.util import parse_date, today_et  # noqa: E402

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
body{font-family:-apple-system,Segoe UI,Arial,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:24px}
h1,h2,h3{color:#58a6ff}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:18px;margin-bottom:20px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border:1px solid #30363d;padding:6px 10px;text-align:left}
th{background:#21262d}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.stat{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:12px;text-align:center}
.stat .v{font-size:22px;font-weight:700}
button{padding:10px 18px;background:#238636;border:none;border-radius:6px;color:white;
        font-weight:600;cursor:pointer;margin-right:10px}
button:disabled{background:#30363d;cursor:not-allowed}
button.secondary{background:#30363d}
label{font-size:13px;color:#8b949e;margin-right:14px}
select{padding:9px 10px;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;margin:4px 8px 4px 0}
button.small{padding:8px 12px;font-size:13px}
.history-controls{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:12px}
.history-controls label{margin-right:0}
.history-meta{color:#8b949e;font-size:13px;margin:8px 0 12px}
.history-table{overflow:auto;max-height:560px}
.history-table table{min-width:760px}
td.err{color:#f85149;font-size:12px;max-width:340px;overflow:hidden;
       text-overflow:ellipsis;white-space:nowrap}
pre{white-space:pre-wrap;font-size:12px;background:#0d1117;border:1px solid #30363d;
     border-radius:6px;padding:12px;max-height:400px;overflow:auto}
a{color:#58a6ff}
.top{display:flex;justify-content:space-between;align-items:center}
</style></head><body>
<div class="top">
  <h1>Trade Agent — Live Dashboard</h1>
  <a href="/logout">Sign out</a>
</div>
<!--ARBY-->
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
    <button onclick="runLoop('runner')">Run Runner Scan</button>
    <button class="secondary" onclick="loadData()">Refresh</button>
  </div>
  <p id="running" style="display:none;color:#d29922">Running — this can take 1-4 minutes for the morning loop
     (extended thinking + web search). Do not close this tab.</p>
  <pre id="log" style="display:none"></pre>
</div>
<div id="app"></div>

<script>
async function loadData() {
  const r = await fetch('/api/status');
  const d = await r.json();
  render(d);
}

function render(d) {
  const el = document.getElementById('app');
  const overall = d.overall || {};
  const plan = d.plan;
  let planHtml = "<p class='neutral'>No plan yet today.</p>";
  if (plan) {
    if (plan.no_trade) {
      planHtml = `<p><b>NO TRADE.</b> ${plan.no_trade_reason || ''}</p><p>${plan.market_stance || ''}</p>`;
    } else {
      // prefer the authoritative picks table rows (flat entry_type, always
      // resolved) over the raw plan JSON, which may only carry a nested
      // entry_plan.type
      const source = (d.picks_today && d.picks_today.length) ? d.picks_today : (plan.picks || []);
      let rows = source.map(p => {
        const entryType = p.entry_type || (p.entry_plan && p.entry_plan.type) || 'open';
        return `<tr><td>${p.ticker}</td><td>${p.direction}</td>
        <td>${p.setup_type||''}</td><td>${entryType} @ ${p.entry_price}</td><td>${p.stop_price}</td>
        <td>${p.target_price}</td><td>${p.size_r}R</td><td>${p.conviction}</td>
        <td>${p.thesis_final||p.thesis||''}</td></tr>`;
      }).join('');
      planHtml = `<p>${plan.market_stance||''}</p><table><tr><th>Ticker</th><th>Dir</th><th>Setup</th>
        <th>Entry (type @ price)</th><th>Stop</th><th>Target</th><th>Size</th><th>Conv</th><th>Thesis</th></tr>${rows}</table>`;
    }
  }
  let setupRows = (d.by_setup || []).map(s => `<tr><td>${s.dim}</td><td>${s.n}</td><td>${s.triggered}</td>
    <td>${s.win_rate!=null ? Math.round(s.win_rate*100)+'%' : '-'}</td><td>${s.avg_r ?? '-'}</td>
    <td>${s.total_weighted_r}</td></tr>`).join('');
  // runs.error has always been in this payload (recent_runs does SELECT *).
  // It was simply never rendered, which is why a three-line billing failure
  // read as an unexplained red row for hours. Escape it: it is raw exception
  // text and routinely contains < and &.
  const esc = t => String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
  let runsRows = (d.runs || []).map(r => {
    const err = r.error ? esc(r.error) : '';
    const short = err.length > 90 ? err.slice(0, 90) + '&hellip;' : err;
    const cell = err
      ? `<td class="err" title="${err}">${short}</td>`
      : '<td></td>';
    return `<tr><td>${r.loop}</td><td>${r.run_date}</td><td>${r.status}</td>
    <td>${r.model||''}</td><td>$${(r.cost_usd||0).toFixed(3)}</td>${cell}</tr>`;
  }).join('');

  const rejections = d.rejections_today || [];
  let shadowHtml = "<p class='neutral'>Nothing rejected today — every candidate that survived analysis made the plan.</p>";
  if (rejections.length) {
    let shadowRows = rejections.map(r => {
      const outcome = r.eod_outcome
        ? `${r.eod_outcome}${r.forgone_r!=null ? ' (' + (r.forgone_r>=0?'+':'') + r.forgone_r + 'R)' : ''}`
        : 'ungraded';
      return `<tr><td>${r.ticker}</td><td>${r.direction||''}</td><td>${r.setup_type||''}</td>
        <td>${r.entry_type||''} @ ${r.entry_price ?? '—'}</td><td>${r.stop_price ?? '—'}</td>
        <td>${r.target_price ?? '—'}</td><td>${r.conviction ?? '—'}</td>
        <td>${r.reason||''}</td><td>${r.thesis||''}</td><td>${outcome}</td></tr>`;
    }).join('');
    shadowHtml = `<table><tr><th>Ticker</th><th>Dir</th><th>Setup</th><th>Entry (type @ price)</th>
      <th>Stop</th><th>Target</th><th>Conv</th><th>Kill reason</th><th>Thesis</th><th>Shadow outcome</th></tr>${shadowRows}</table>`;
  }

  // ── Runner lane ──────────────────────────────────────────────────────
  const rn = d.runner || {};
  let runnerHtml = "";
  if (rn.ran) {
    const bad  = /^DEGRADED|^NO SCAN/.test(rn.health || "");
    const quiet = /stand-down/.test(rn.health || "");
    const col  = bad ? "#f85149" : (quiet ? "#d29922" : "#3fb950");
    const rows = (rn.rows || []).map(r => {
      const sel = r.selected ? `<span style="color:#3fb950;font-weight:700">#${r.rank}</span>` : "";
      const n = (v, dg) => (v==null ? "—" : Number(v).toFixed(dg==null?2:dg));
      const rc = r.r_multiple==null ? "#8b949e" : (r.r_multiple>0 ? "#3fb950" : "#f85149");
      return `<tr><td>${sel}</td><td><b>${r.ticker}</b></td><td>${r.detection_et||""}</td>
        <td>${n(r.price)}</td><td>${n(r.stop)}</td><td>${n(r.target)}</td>
        <td>${n(r.rvol)}</td><td>${n(r.vwap_pct)}</td><td>${n(r.tc,1)}</td>
        <td>${n(r.change_pct)}</td><td style="color:${rc}">${n(r.r_multiple)}</td>
        <td>${r.exit_reason||""}</td></tr>`;
    }).join("");
    const table = rows
      ? `<table style="margin-top:12px"><tr><th></th><th>Ticker</th><th>Det ET</th>
         <th>Price</th><th>Stop</th><th>Target</th><th>RVOL</th><th>VWAP%</th>
         <th>TC</th><th>Chg%</th><th>R</th><th>Exit</th></tr>${rows}</table>`
      : "";

    const movers = rn.movers || [];
    let moversHtml = "";
    if (movers.length) {
      const overlap = movers.filter(m => m.selected).length;
      const mvRows = movers.map(m => {
        const rc = m.r_multiple==null ? "#8b949e" : (m.r_multiple>0 ? "#3fb950" : "#f85149");
        const tag = m.selected
          ? `<span style="color:#3fb950;font-weight:700">#${m.rank} picked</span>`
          : `<span style="color:#8b949e">not picked</span>`;
        return `<tr><td><b>${m.ticker}</b></td>
          <td style="color:${rc}">${m.r_multiple==null?"—":Number(m.r_multiple).toFixed(2)}</td>
          <td>${m.exit_reason||""}</td><td>${tag}</td></tr>`;
      }).join("");
      moversHtml = `<h3 style="margin-top:18px">Top ${movers.length} movers today (by realized R)</h3>
        <p style="color:#8b949e;font-size:12px">
          ${overlap} of ${movers.length} were selected. Same-day eyeball check, not a
          training signal — one day's winners are noise to retune a selector on. The
          FDR-corrected version, across many sessions, feeds the weekly miner instead.
        </p>
        <table><tr><th>Ticker</th><th>R</th><th>Exit</th><th></th></tr>${mvRows}</table>`;
    }

    runnerHtml = `<div class="card"><h2>Runner Lane — ${d.today}</h2>
      <p style="color:${col};font-weight:600">${rn.health||""}</p>
      ${rn.skipped_reason ? `<p style="color:#8b949e">selection: ${rn.skipped_reason}</p>` : ""}
      <div class="grid">
        <div class="stat"><div class="v">${rn.universe||0}</div>scanned</div>
        <div class="stat"><div class="v">${rn.n_candidates||0}</div>candidates</div>
        <div class="stat"><div class="v">${rn.n_selected||0}</div>selected</div>
        <div class="stat"><div class="v">${rn.n_orders||0}</div>live orders</div>
      </div>
      ${moversHtml}
      <h3 style="margin-top:18px">All candidates</h3>${table}
      <p style="color:#8b949e;font-size:12px;margin-top:10px">
        Every candidate is priced by the evening simulator, selected or not —
        that is what makes “did picking help?” answerable. Rows marked #N were chosen.
      </p></div>`;
  } else {
    runnerHtml = `<div class="card"><h2>Runner Lane — ${d.today}</h2>
      <p style="color:#8b949e">${rn.health || "No scan recorded for today."}</p></div>`;
  }

  el.innerHTML = `
  <div class="card"><h2>Today's Plan — ${d.today}</h2>${planHtml}</div>
  <div class="card"><h2>Performance</h2>
    <div class="grid">
      <div class="stat"><div class="v">${overall.picks_graded ?? 0}</div>Graded trades</div>
      <div class="stat"><div class="v">${overall.win_rate!=null ? Math.round(overall.win_rate*100)+'%' : '—'}</div>Win rate</div>
      <div class="stat"><div class="v">${overall.avg_r ?? '—'}</div>Avg R</div>
      <div class="stat"><div class="v">${overall.total_weighted_r ?? 0}</div>Total weighted R</div>
      <div class="stat"><div class="v">$${(d.equity||0).toLocaleString()}</div>Paper equity</div>
      <div class="stat"><div class="v">${d.streak||'—'}</div>Streak</div>
    </div></div>
  <div class="card"><h2>By Setup</h2><table><tr><th>Setup</th><th>N</th><th>Triggered</th>
    <th>Win rate</th><th>Avg R</th><th>Total wR</th></tr>${setupRows}</table></div>
  ${runnerHtml}
  <div class="card"><h2>Shadow Ledger — Rejected Candidates (${d.today})</h2>${shadowHtml}</div>
    <div class="card"><h2>Historical Data</h2>
        <p class="history-meta">Choose a date and data type to inspect everything saved for that session.</p>
        <div class="history-controls">
            <label for="historyDate">Date</label><select id="historyDate"></select>
            <label for="historyTable">Data</label><select id="historyTable">
                <option value="rejections">Shadow ledger</option>
                <option value="picks">Trade picks</option>
                <option value="outcomes">Trade outcomes</option>
                <option value="daily_plans">Daily plans</option>
                <option value="regime_history">Market regimes</option>
                <option value="lessons">Lessons</option>
                <option value="calibration">Calibration</option>
                <option value="rule_confirmations">Rule confirmations</option>
                <option value="open_question_progress">Question progress</option>
                <option value="runs">Run history</option>
            </select>
            <button class="small" onclick="loadHistory()">View</button>
        </div>
        <div id="historyResult" class="history-meta">Loading available dates...</div>
        <div id="historyTableView" class="history-table"></div>
    </div>
  <div class="card"><h2>Playbook v${d.playbook_version}</h2><pre>${d.playbook.replace(/</g,'&lt;')}</pre></div>
  <div class="card"><h2>Recent Runs</h2><table><tr><th>Loop</th><th>Date</th><th>Status</th>
    <th>Model</th><th>Cost</th><th>Error</th></tr>${runsRows}</table></div>
  `;
  // The Historical Data controls above just got (re)created by the innerHTML
  // assignment, so their dropdown needs to be (re)populated every time render()
  // runs -- not just once at page load, or it's left empty after any refresh.
  loadHistoryDates();
}

async function runLoop(name) {
  document.getElementById('running').style.display = 'block';
  document.getElementById('log').style.display = 'block';
  document.getElementById('log').textContent = 'Starting ' + name + ' loop...';
  document.querySelectorAll('button').forEach(b => b.disabled = true);
  const params = new URLSearchParams({
    mock: document.getElementById('mock').checked ? '1' : '0',
    offline: document.getElementById('offline').checked ? '1' : '0',
    force: document.getElementById('force').checked ? '1' : '0',
  });
  try {
    const r = await fetch('/run/' + name + '?' + params.toString(), {method: 'POST'});
    const text = await r.text();
    // A platform-level failure (e.g. serverless function timeout) returns an
    // HTML/plain-text error page instead of our JSON contract -- show that
    // raw instead of crashing on JSON.parse with a cryptic "Unexpected token".
    let d;
    try { d = JSON.parse(text); } catch { d = null; }
    if (d) {
      document.getElementById('log').textContent = (d.log || '') + (d.error ? '\\n\\nERROR: ' + d.error : '');
    } else {
      document.getElementById('log').textContent = `Request failed (HTTP ${r.status}): ` + text.slice(0, 2000);
    }
  } catch (e) {
    document.getElementById('log').textContent = 'Request failed: ' + e;
  } finally {
    document.querySelectorAll('button').forEach(b => b.disabled = false);
    document.getElementById('running').style.display = 'none';
    loadData();
  }
}

loadData();

async function loadHistoryDates() {
    const result = document.getElementById('historyResult');
    try {
        const r = await fetch('/api/history/dates');
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || 'Could not load dates');
        const select = document.getElementById('historyDate');
        select.innerHTML = (d.dates || []).map(day => `<option value="${day}">${day}</option>`).join('');
        if (!d.dates || !d.dates.length) {
            result.textContent = 'No historical sessions have been recorded yet.';
            return;
        }
        await loadHistory();
    } catch (e) {
        result.textContent = 'Could not load history: ' + e.message;
    }
}

async function loadHistory() {
    const date = document.getElementById('historyDate').value;
    const table = document.getElementById('historyTable').value;
    const result = document.getElementById('historyResult');
    const view = document.getElementById('historyTableView');
    if (!date) return;
    result.textContent = 'Loading...';
    view.innerHTML = '';
    try {
        const r = await fetch(`/api/table/${table}?date=${encodeURIComponent(date)}&limit=1000`);
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || 'Could not load records');
        result.textContent = `${d.n} record${d.n === 1 ? '' : 's'} in ${table.replaceAll('_', ' ')} for ${date}`;
        if (!d.n) {
            view.innerHTML = '<p class="history-meta">No records for this date.</p>';
            return;
        }
        const headers = d.columns || [];
        const headerHtml = headers.map(column => `<th>${historyText(column)}</th>`).join('');
        const rowsHtml = d.rows.map(row => '<tr>' + headers.map(column => `<td>${historyText(row[column])}</td>`).join('') + '</tr>').join('');
        view.innerHTML = `<table><tr>${headerHtml}</tr>${rowsHtml}</table>`;
    } catch (e) {
        result.textContent = 'Could not load history: ' + e.message;
    }
}

function historyText(value) {
    if (value === null || value === undefined) return '—';
    return String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
}
</script>
</body></html>"""

# Arby the desk mascot: a self-contained widget injected at the marker. It only
# reads /api/status and never touches the run/loop logic above.
_PAGE = _PAGE.replace(MASCOT_MARKER, mascot_embed())


@app.route("/")
@login_required
def dashboard():
    return _PAGE


# ------------------------------------------------------------------------ API

_PICK_BARS_CACHE: dict[tuple[str, str], tuple[float, list]] = {}


def _pick_bars(picks: list, d) -> dict:
    """Best-effort real intraday bars for the desk monitor chart, keyed by ticker.

    Returns {} on any failure so the mascot falls back to its synthetic trace.
    Cached for a short TTL so the 60s status poll doesn't re-hit the network.
    """
    if not picks:
        return {}
    out: dict = {}
    now = time.monotonic()
    for p in picks[:3]:
        sym = (p or {}).get("ticker")
        if not sym:
            continue
        key = (str(sym).upper(), d.isoformat())
        cached = _PICK_BARS_CACHE.get(key)
        if cached and now - cached[0] < 90:
            out[sym] = cached[1]
            continue
        bars: list = []
        try:
            from agent import providers
            rows = providers.yf_minute_bars(sym, d)
            # "v" rides along so the dashboard widget can overlay a real VWAP
            # line (agent/mascot.py computeVWAP) -- without it the chart would
            # only ever be able to draw candles, never the volume-weighted line.
            bars = [{"o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v")} for b in rows]
        except Exception:
            bars = []
        _PICK_BARS_CACHE[key] = (now, bars)
        out[sym] = bars
    return out


def _runner_payload(con, d) -> dict:
    """Today's runner session for the dashboard, or a marker that it never ran.

    Never raises: a failure in this lane must not take down the whole status
    endpoint the rest of the desk depends on.
    """
    try:
        from agent import runner_view
        s = runner_view.session_summary(con, d)
        return {
            "ran": bool(s["session"] or s["rows"]),
            "health": runner_view._health_line(s),
            "universe": (s["session"] or {}).get("universe_size") or 0,
            "n_candidates": s["n_candidates"],
            "n_selected": s["n_selected"],
            "n_orders": len(s["orders"]),
            "degraded": bool((s["session"] or {}).get("provider_degraded")),
            "skipped_reason": (s["session"] or {}).get("selection_skipped_reason"),
            "rows": sorted(s["rows"],
                           key=lambda x: (not x["selected"], x["rank"] or 99,
                                          x["ticker"])),
            # Same-day eyeball diagnostic, not a retuning signal -- one day's
            # top N out of hundreds of candidates is noise as training data.
            # The FDR-corrected, many-session version already lives in
            # agent/runner_research.selection_edge() and feeds the weekly
            # miner, which is the only thing allowed to write a playbook rule.
            "movers": [
                {"ticker": r["ticker"], "r_multiple": r["r_multiple"],
                 "exit_reason": r["exit_reason"], "selected": r["selected"],
                 "rank": r["rank"]}
                for r in runner_view.top_movers(s, 10)
            ],
        }
    except Exception as e:
        return {"ran": False, "health": f"runner view unavailable ({e})",
                "rows": [], "n_candidates": 0, "n_selected": 0, "n_orders": 0}


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
            # the DEFINITIVE, resolved pick rows (entry_type always a single flat
            # field here, unlike plan.picks[] which may only carry a nested
            # entry_plan.type) -- this is what the evening grader actually acts on.
            "picks_today": ledger.picks_for_date(con, d),
            "rejections_today": ledger.rejections_for_date(con, d),
            "pick_bars": _pick_bars(
                (plan_row["plan"] or {}).get("picks", []) if plan_row else [], d
            ),
            "overall": overall,
            # Runner lane: the whole session, so the dashboard can show what was
            # scanned, what was picked, and - critically - whether a thin
            # candidate list was the market or a broken feed.
            "runner": _runner_payload(con, d),
            "by_setup": by_setup,
            "streak": streak_str,
            "streak_detail": streak,
            "equity": equity,
            "equity_curve": curve[-120:],
            "runs": runs,
            "run_locks": [dict(zip([c[0] for c in con.execute("DESCRIBE run_locks").fetchall()], row))
                         for row in con.execute(
                             "SELECT * FROM run_locks WHERE trading_date = ? ORDER BY run_type", [d]
                         ).fetchall()],
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


@app.route("/api/history/dates")
@login_required
def api_history_dates():
    """Return every session date represented in the historical ledger tables."""
    con = ledger.connect()
    try:
        dates = set()
        date_sources = {
            "picks": "plan_date",
            "rejections": "plan_date",
            "daily_plans": "plan_date",
            "regime_history": "date",
            "lessons": "created_date",
            "calibration": "session_date",
            "rule_confirmations": "session_date",
            "open_question_progress": "session_date",
            "runs": "run_date",
            "run_locks": "trading_date",
            "plan_runs": "trading_date",
        }
        for table, column in date_sources.items():
            try:
                rows = con.execute(
                    f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL"
                ).fetchall()
                dates.update(row[0].isoformat() for row in rows if row[0] is not None)
            except Exception:
                # Historical browsing should remain available if an older hosted
                # database is missing an optional v3 table.
                continue
        return json_response({"dates": sorted(dates, reverse=True)})
    finally:
        con.close()


# --------------------------------------------------------- raw table access
#
# A generic, read-only, whitelisted browser for every ledger table -- so any
# question like "what did today's MARA pick actually store for entry_type"
# can be answered straight from the app (GET request, or paste the URL into
# a browser) instead of needing a local DATABASE_URL + CLI. Table names are
# whitelisted (never interpolate a caller-controlled table name without one --
# see agent/ledger.py stats_by for the same pattern/lesson). Only SELECT is
# ever built here; there is no way to reach this with a write.

# table -> its natural date column (None if it has none / isn't date-filterable)
_QUERYABLE_TABLES = {
    "picks": "plan_date",
    "outcomes": "eval_date",
    "rejections": "plan_date",
    "daily_plans": "plan_date",
    "lessons": "created_date",
    "lesson_confirmations": "session_date",
    "regime_history": "date",
    "runs": "run_date",
    "calibration": "session_date",
    "rule_confirmations": "session_date",
    "open_question_progress": "session_date",
    "runner_candidates": "session_date",
    "runner_candidate_outcomes": "session_date",
    "runner_sessions": "session_date",
}


@app.route("/api/table")
@login_required
def api_table_index():
    """Lists every queryable table + its columns, so you can discover what's
    available without already knowing the schema."""
    con = ledger.connect()
    try:
        out = {}
        for name in sorted(_QUERYABLE_TABLES):
            out[name] = [c[0] for c in con.execute(f"DESCRIBE {name}").fetchall()]
        return json_response({"tables": out,
                              "usage": "/api/table/<name>?date=YYYY-MM-DD&ticker=XXX&since=YYYY-MM-DD&limit=N"})
    finally:
        con.close()


@app.route("/api/table/<name>")
@login_required
def api_table(name: str):
    if name not in _QUERYABLE_TABLES:
        return json_response(
            {"error": f"unknown or non-whitelisted table {name!r}",
             "available": sorted(_QUERYABLE_TABLES)}, 404)
    con = ledger.connect()
    try:
        date_col = _QUERYABLE_TABLES[name]  # trusted constant, not user input
        cols = [c[0] for c in con.execute(f"DESCRIBE {name}").fetchall()]
        where: list[str] = []
        params: list = []
        if date_col and request.args.get("date"):
            where.append(f"{date_col} = ?")
            params.append(parse_date(request.args["date"]))
        if date_col and request.args.get("since"):
            where.append(f"{date_col} >= ?")
            params.append(parse_date(request.args["since"]))
        if "ticker" in cols and request.args.get("ticker"):
            where.append("ticker = ?")
            params.append(request.args["ticker"].strip().upper())
        try:
            limit = max(1, min(int(request.args.get("limit", 200)), 1000))
        except ValueError:
            limit = 200
        sql = f"SELECT * FROM {name}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        if date_col:
            sql += f" ORDER BY {date_col} DESC"
        sql += f" LIMIT {limit}"
        rows = con.execute(sql, params).fetchall()
        return json_response({"table": name, "columns": cols, "n": len(rows),
                              "rows": [dict(zip(cols, r)) for r in rows]})
    except Exception as e:  # noqa: BLE001
        return json_response({"error": str(e), "trace": traceback.format_exc()}, 500)
    finally:
        con.close()


# ------------------------------------------------------------------ run loops

_VALID_LOOPS = {"morning", "evening", "weekly", "runner"}


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
            elif loop_name == "runner":
                # Runs the scan SERVER-SIDE, so its evidence lands in the same
                # Postgres the dashboard reads. Running it locally writes to a
                # local DuckDB the hosted dashboard cannot see -- which is the
                # entire reason the Runner card kept showing "no scan recorded"
                # while the lane was in fact working fine on the local machine.
                from agent.loops import runner as runner_loop
                from agent.util import now_et
                result = runner_loop.tick(cfg, at=now_et())
                print(json.dumps(result, indent=2, default=str))
            else:
                from agent.loops import weekly
                weekly.run(cfg)
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
        buf.write(f"\n\n{traceback.format_exc()}")
    result = {"loop": loop_name, "log": buf.getvalue(), "error": error}
    if not error and "Duplicate trigger suppressed" in result["log"]:
        con = ledger.connect()
        try:
            existing = ledger.plan_for_date(con, today_et()) if loop_name in ("morning", "evening") else None
            result["duplicate_suppressed"] = True
            result["existing_result"] = existing["plan"] if existing else None
        finally:
            con.close()
    return result


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
# Vercel Hobby restricts cron FREQUENCY (at most once per day per job), not the
# NUMBER of jobs -- an earlier comment here claimed a hard max of two, which was
# wrong and cost this project a working intraday trigger for days. Three
# once-per-day crons deploy fine (morning / evening / runner, verified
# 2026-09-18). A "*/5 13-20 * * 1-5" expression IS rejected, with the explicit
# reason that it "would run more than once per day".
#
# There is still no separate weekly cron: the evening cron runs the weekly miner
# after Friday's grading session, which is equivalent (all of the week's data is
# graded by then).
#
# Cron schedules are fixed UTC with no DST awareness, so we sanity-check the
# current ET hour before spending API money. Windows are wide enough to accept
# the one-hour EST/EDT drift (morning fires 5:45 ET in winter, 6:45 in summer).
# runner fires at 14:30 UTC = 10:30 ET (EDT) / 09:30 ET (EST). The window
# spans both so the DST drift does not silently skip the decision tick.
_CRON_WINDOWS = {"morning": (5, 10), "evening": (15, 19), "runner": (9, 12)}


@app.route("/cron/<loop_name>", methods=["GET"])
@login_required
def cron_loop(loop_name: str):
    if loop_name not in ("morning", "evening", "runner"):
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


# GET as well as POST: Vercel Cron issues GET and sends
# "Authorization: Bearer $CRON_SECRET" automatically. POST is retained so
# the GitHub Actions workflow (and any external caller) keeps working.
@app.route("/cron/intraday-check", methods=["GET", "POST"])
def intraday_check():
    if not _is_cron_request():
        return json_response({"error": "unauthorized"}, 401)
    cfg = load_config()
    from agent.live_monitor import check
    # The live position monitor runs FIRST and unguarded. It is the job this
    # endpoint exists for; if it raises, the request should fail loudly.
    monitor = check(cfg)
    # Spec §1.4 nests the monitor result under "monitor". Its existing keys are
    # ALSO kept at the top level: this response is an established contract and
    # R-3 is additive-only, so re-shaping it would be a behavior change smuggled
    # in under a feature flag — and one that only shows up in production, since
    # the GitHub Actions cron discards the body.
    result = dict(monitor)
    result["monitor"] = monitor
    # The runner lane runs second, inside try/except. This is required, not
    # defensive habit: the runner lane must never be able to break the live
    # position monitor, and a monitor that stops running is a real position
    # left unwatched.
    if (cfg.get("runner", {}) or {}).get("enabled"):
        from agent.util import log
        try:
            from agent.loops.runner import tick
            result["runner"] = tick(cfg)
        except Exception as e:
            log("runner", f"WARN runner tick failed; monitor unaffected: {e}")
            result["runner"] = {"error": str(e)}
    return json_response(result)


@app.route("/api/runner/candidates")
@login_required
def api_runner_candidates():
    """Candidates for one session, alphabetical (spec R-1: detection must not
    rank). Defaults to today."""
    con = ledger.connect()
    try:
        d = parse_date(request.args["date"]) if request.args.get("date") else today_et()
        rows = ledger.runner_candidates_for_date(con, d)
        outcomes = {o["ticker"]: o for o in ledger.runner_outcomes_for_date(con, d)}
        for r in rows:
            r["outcome"] = outcomes.get(r["ticker"])
        return json_response({"date": d.isoformat(), "count": len(rows),
                              "session": ledger.runner_session(con, d),
                              "candidates": rows})
    finally:
        con.close()


@app.route("/api/runner/sessions")
@login_required
def api_runner_sessions():
    """Per-session scan telemetry — the stand-down-vs-outage discriminator.

    A zero-candidate day with universe_size 0 or provider_degraded true is a
    pipeline failure; a zero-candidate day with a full universe and a high
    rejected_conditions is a genuine stand-down. Vercel keeps logs about an
    hour, so this table is the durable record.
    """
    con = ledger.connect()
    try:
        try:
            limit = max(1, min(int(request.args.get("limit", 40)), 400))
        except ValueError:
            limit = 40
        return json_response({"sessions": ledger.recent_runner_sessions(con, limit)})
    finally:
        con.close()


# Local dev convenience: `python api/index.py` runs a normal Flask dev server.
if __name__ == "__main__":
    app.run(debug=True, port=5000)
