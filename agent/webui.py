"""Hosted web dashboard UI — a multi-page SPA shell.

Kept separate from api/index.py (already large) so Flask routing and
presentation don't fight. The single ``PAGE`` string here is what ``/`` serves
after the Arby mascot marker is replaced.

Layout model: every "page" is a static ``<section class="page">`` that stays in
the DOM and is toggled via ``.active``. Only the mascot (Arby) requires this: it
is a self-contained Three.js widget whose ``<script>`` only executes when it is
in the initial HTML — innerHTML injection would silently skip it.
"""
from __future__ import annotations

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Trade Agent — Desk</title>
<style>
:root{
  --bg:#080b10;--bg-soft:#0b0f16;--surface:#10151d;--surface-2:#151b25;--surface-3:#1a212c;
  --border:#1c2430;--border-strong:#2a3340;--text:#e6edf3;--text-muted:#8b949e;--text-dim:#5c6673;
  --accent:#4d9fff;--accent-strong:#79b8ff;--accent-dim:#152238;
  --green:#3fb950;--green-dim:#11251a;--red:#f85149;--red-dim:#271317;
  --amber:#d29922;--amber-dim:#251d0e;--purple:#a371f7;--purple-dim:#1f1833;
  --radius:12px;--radius-sm:8px;
  --shadow:0 1px 0 rgba(255,255,255,.02) inset,0 8px 24px -16px rgba(0,0,0,.7);
  --mono:"SF Mono","JetBrains Mono",ui-monospace,Consolas,"Roboto Mono",monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,"Helvetica Neue",Arial,sans-serif;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;font-family:var(--sans);background:var(--bg);color:var(--text);font-size:14px;line-height:1.5;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:var(--accent-strong);text-decoration:none}a:hover{text-decoration:underline}
h1,h2,h3,h4{margin:0;font-weight:600;letter-spacing:-.01em}
code,pre,.num{font-family:var(--mono)}
.app{display:flex;min-height:100vh}
.sidebar{width:248px;flex:0 0 248px;background:linear-gradient(180deg,var(--bg-soft),var(--bg));
  border-right:1px solid var(--border);display:flex;flex-direction:column;position:sticky;top:0;height:100vh;padding:20px 14px}
.brand{display:flex;align-items:center;gap:11px;padding:4px 8px 20px}
.brand-mark{width:34px;height:34px;border-radius:9px;flex:0 0 34px;
  background:radial-gradient(circle at 30% 25%,#3b82f6,#1d4ed8 65%);
  display:flex;align-items:center;justify-content:center;font-weight:700;font-size:15px;color:#fff;
  box-shadow:0 0 0 1px rgba(255,255,255,.06) inset,0 6px 16px -6px rgba(59,130,246,.6)}
.brand-name{font-size:15px;font-weight:650;letter-spacing:-.01em}
.brand-sub{font-size:11px;color:var(--text-dim);font-weight:500;margin-top:1px}
.nav{display:flex;flex-direction:column;gap:2px;margin-top:4px}
.nav-group{font-size:10px;font-weight:700;letter-spacing:.12em;color:var(--text-dim);text-transform:uppercase;padding:14px 10px 6px}
.nav-item{display:flex;align-items:center;gap:11px;padding:8px 10px;border-radius:var(--radius-sm);color:var(--text-muted);
  font-size:13.5px;font-weight:500;text-decoration:none;transition:background .12s ease,color .12s ease;border:1px solid transparent}
.nav-item svg{width:17px;height:17px;flex:0 0 17px;opacity:.85}
.nav-item:hover{background:var(--surface-2);color:var(--text);text-decoration:none}
.nav-item.active{background:var(--accent-dim);color:var(--accent-strong);border-color:rgba(77,159,255,.25)}
.nav-item.active svg{opacity:1}
.sidebar-foot{margin-top:auto;padding-top:16px;border-top:1px solid var(--border)}
.sidebar-foot .nav-item{color:var(--text-dim)}
.shell{flex:1;min-width:0;display:flex;flex-direction:column}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 28px;
  border-bottom:1px solid var(--border);background:rgba(8,11,16,.72);backdrop-filter:blur(8px);position:sticky;top:0;z-index:30}
.topbar-title h1{font-size:17px}
.topbar-title .sub{font-size:12px;color:var(--text-muted);margin-top:1px}
.topbar-actions{display:flex;align-items:center;gap:10px}
.updated{font-size:12px;color:var(--text-dim);font-family:var(--mono)}
#running{display:none;margin:0 28px;border:1px solid rgba(210,153,34,.4);background:var(--amber-dim);
  color:#e3b341;border-radius:var(--radius-sm);padding:9px 14px;font-size:13px;font-weight:500}
.spinner{width:12px;height:12px;border:2px solid rgba(227,179,65,.35);border-top-color:#e3b341;border-radius:50%;
  display:inline-block;margin-right:9px;vertical-align:-1px;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.content{flex:1;padding:26px 28px 60px;max-width:1200px;width:100%}
.page{display:none;animation:fade .18s ease}
.page.active{display:block}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:20px;
  margin-bottom:18px;box-shadow:var(--shadow)}
.card-head{display:flex;align-items:baseline;justify-content:space-between;gap:14px;flex-wrap:wrap;margin-bottom:14px}
.card-head h2{font-size:15px;color:var(--text)}
.card-sub{font-size:12px;color:var(--text-muted)}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:15px 16px;
  box-shadow:var(--shadow);position:relative;overflow:hidden}
.stat::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--accent);opacity:.55}
.stat.ok::before{background:var(--green)}.stat.bad::before{background:var(--red)}.stat.warn::before{background:var(--amber)}
.stat-label{font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--text-muted)}
.stat-value{font-size:23px;font-weight:700;letter-spacing:-.02em;margin:5px 0 1px;font-family:var(--mono)}
.stat-sub{font-size:11.5px;color:var(--text-dim)}
.table-wrap{overflow:auto;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--bg-soft)}
table{border-collapse:collapse;width:100%;font-size:13px}
thead th{position:sticky;top:0;background:var(--surface-2);color:var(--text-muted);font-size:11px;font-weight:600;
  letter-spacing:.05em;text-transform:uppercase;text-align:left;padding:9px 12px;border-bottom:1px solid var(--border);
  white-space:nowrap;z-index:1}
tbody td{padding:9px 12px;border-bottom:1px solid var(--border);vertical-align:top}
tbody tr:last-child td{border-bottom:none}tbody tr:hover{background:var(--surface-2)}
td.num,th.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
td.num{color:var(--text)}td.pos{color:var(--green)}td.neg{color:var(--red)}td.dim{color:var(--text-dim)}
td.wrap{max-width:380px;white-space:normal}
.pill{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:700;letter-spacing:.04em;
  padding:2px 9px;border-radius:20px;border:1px solid var(--border);color:var(--text-muted);white-space:nowrap}
.pill.ok{color:var(--green);background:var(--green-dim);border-color:rgba(63,185,80,.3)}
.pill.bad{color:var(--red);background:var(--red-dim);border-color:rgba(248,81,73,.3)}
.pill.warn{color:var(--amber);background:var(--amber-dim);border-color:rgba(210,153,34,.3)}
.pill.info{color:var(--accent-strong);background:var(--accent-dim);border-color:rgba(77,159,255,.3)}
.pill.neutral{color:var(--text-muted)}
.btn{appearance:none;border:1px solid var(--border-strong);background:var(--surface-2);color:var(--text);
  padding:8px 14px;border-radius:var(--radius-sm);font-size:13px;font-weight:600;cursor:pointer;
  transition:background .12s ease,border-color .12s ease,transform .04s ease}
.btn:hover{background:var(--surface-3);border-color:var(--accent)}
.btn:active{transform:translateY(1px)}.btn:disabled{opacity:.5;cursor:not-allowed}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#04121f}
.btn.primary:hover{background:var(--accent-strong);border-color:var(--accent-strong)}
.btn.ghost{background:transparent}.btn.small{padding:5px 11px;font-size:12px}
.run-buttons{display:flex;gap:10px;flex-wrap:wrap}
.run-controls{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:16px}
.check{display:flex;flex-direction:column;gap:2px;cursor:pointer;font-size:13px;font-weight:500}
.check input{margin-right:6px;accent-color:var(--accent)}
.check small{font-size:11px;color:var(--text-dim);font-weight:400;margin-left:22px}
.log{margin-top:16px;background:#05070a;border:1px solid var(--border);border-radius:var(--radius-sm);padding:14px;
  font-size:12px;line-height:1.6;white-space:pre-wrap;max-height:420px;overflow:auto;font-family:var(--mono);color:#c9d1d9}
.history-controls{display:flex;align-items:end;gap:14px;flex-wrap:wrap;margin-bottom:14px}
.history-controls label{display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--text-muted);font-weight:500}
.history-meta{color:var(--text-muted);font-size:12.5px;margin:4px 0 12px}
select{appearance:none;background:var(--bg-soft);border:1px solid var(--border-strong);color:var(--text);
  padding:8px 30px 8px 11px;border-radius:var(--radius-sm);font-size:13px;font-family:var(--sans);cursor:pointer}
.prose{font-size:13.5px;color:#c9d1d9}
.prose h1,.prose h2,.prose h3,.prose h4{color:var(--accent-strong);margin:20px 0 8px}
.prose h2{font-size:16px}.prose h3{font-size:14px}
.prose p{margin:8px 0}.prose ul,.prose ol{margin:6px 0 12px 22px;padding:0}
.prose li{margin:4px 0}.prose hr{border:none;border-top:1px solid var(--border);margin:18px 0}
.prose code{background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:1px 5px;font-size:12px}
.prose table{width:auto;min-width:340px;margin:12px 0;background:transparent}
.prose thead th{position:static;background:var(--surface-2)}
.prose td,.prose th{border:1px solid var(--border);padding:6px 10px}
.equity-chart{position:relative}
.equity-chart svg{width:100%;height:auto;display:block}
.eq-grid line{stroke:var(--border);stroke-width:1}
.eq-grid text{font-size:10px;fill:var(--text-dim);font-family:var(--mono)}
.eq-dots circle{cursor:default}
.eq-meta{display:flex;justify-content:space-between;align-items:center;font-size:11.5px;color:var(--text-dim);
  margin-top:8px;font-family:var(--mono)}
.eq-last{font-weight:700;font-size:13px}
.toast{position:fixed;right:22px;bottom:22px;z-index:100;max-width:340px;background:var(--surface-3);
  border:1px solid var(--border-strong);border-left:3px solid var(--accent);color:var(--text);border-radius:var(--radius-sm);
  padding:12px 15px;font-size:13px;box-shadow:0 12px 32px -12px rgba(0,0,0,.8);opacity:0;transform:translateY(8px);
  transition:opacity .2s ease,transform .2s ease}
.toast.show{opacity:1;transform:none}.toast.error{border-left-color:var(--red)}.toast.success{border-left-color:var(--green)}
.empty{color:var(--text-dim);font-size:13px;padding:14px 2px}
.kpi-line{display:flex;align-items:center;gap:10px;font-size:13px}
.muted-note{color:var(--text-muted);font-size:12.5px;margin:10px 0}
.card h3{font-size:14px;margin:18px 0 8px;color:var(--accent-strong)}
td.err{color:var(--red);max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.best-edge p{font-size:14px;margin:0;color:var(--green)}.best-edge{border-color:rgba(63,185,80,.28)}
.best-edge.none p{color:#e3b341}.best-edge.none{border-color:rgba(210,153,34,.3)}
.pos{color:var(--green)}.neg{color:var(--red)}.dim{color:var(--text-dim)}
/* learning-loop health */
.nav-dot{width:8px;height:8px;border-radius:50%;margin-left:auto;background:var(--text-dim);flex:0 0 8px}
.nav-dot.ok{background:var(--green);box-shadow:0 0 0 3px rgba(63,185,80,.15)}
.nav-dot.warn{background:var(--amber);box-shadow:0 0 0 3px rgba(210,153,34,.15)}
.nav-dot.bad{background:var(--red);box-shadow:0 0 0 3px rgba(248,81,73,.18)}
.lh{border-left:3px solid var(--border-strong)}
.lh.ok{border-left-color:var(--green)}.lh.warn{border-left-color:var(--amber)}.lh.bad{border-left-color:var(--red)}
.lh-flow{display:flex;align-items:stretch;gap:8px;flex-wrap:wrap}
.lh-stage{flex:1 1 180px;display:flex;gap:10px;background:var(--bg-soft);border:1px solid var(--border);
  border-radius:var(--radius-sm);padding:11px 12px;min-width:0}
.lh-dot{width:9px;height:9px;border-radius:50%;margin-top:5px;flex:0 0 9px;background:var(--text-dim)}
.lh-stage.ok .lh-dot{background:var(--green)}.lh-stage.warn .lh-dot{background:var(--amber)}.lh-stage.bad .lh-dot{background:var(--red)}
.lh-stage.bad{border-color:rgba(248,81,73,.35)}.lh-stage.warn{border-color:rgba(210,153,34,.3)}
.lh-name{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--text-muted)}
.lh-head{font-size:12.5px;color:var(--text);margin-top:3px;line-height:1.4}
.lh-detail{font-size:11.5px;color:var(--text-dim);margin-top:4px;line-height:1.4;word-break:break-word}
.lh-arrow{align-self:center;color:var(--text-dim);font-size:15px}
.lh-signals{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:14px;font-size:12px;color:var(--text-muted)}
.lh-signals b{color:var(--text);font-family:var(--mono);font-weight:600}
.lh-warn{margin:12px 0 0;padding:0;list-style:none}
.lh-warn li{font-size:12.5px;color:#e3b341;background:var(--amber-dim);border:1px solid rgba(210,153,34,.3);
  border-radius:var(--radius-sm);padding:8px 11px;margin-top:6px}
.lanes{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.lane{background:var(--bg-soft);border:1px solid var(--border);border-radius:var(--radius-sm);padding:14px 15px}
.lane h3{margin:0 0 10px!important;font-size:13px!important;color:var(--text)!important;display:flex;justify-content:space-between;align-items:center}
.lane-row{display:flex;justify-content:space-between;font-size:12.5px;padding:4px 0;border-bottom:1px dashed var(--border)}
.lane-row:last-child{border-bottom:none}.lane-row span:first-child{color:var(--text-muted)}
.lane-row span:last-child{font-family:var(--mono)}
.guard-bar{height:6px;background:var(--surface-3);border-radius:4px;overflow:hidden;margin:6px 0 2px}
.guard-fill{height:100%;background:var(--green)}.guard-fill.warn{background:var(--amber)}.guard-fill.bad{background:var(--red)}
.filter-row{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:6px 0 10px}
.filter-row input[type=text]{background:var(--bg-soft);border:1px solid var(--border-strong);color:var(--text);
  padding:7px 10px;border-radius:var(--radius-sm);font-size:13px;min-width:180px}
.filter-row label{font-size:12.5px;color:var(--text-muted);display:flex;align-items:center;gap:6px;cursor:pointer}
@media (max-width:820px){
  .app{flex-direction:column}
  .sidebar{width:100%;flex:auto;position:static;height:auto;flex-direction:column;padding:12px}
  .brand{padding-bottom:10px}
  .nav{flex-direction:row;flex-wrap:wrap;gap:4px}.nav-group{display:none}
  .sidebar-foot{border-top:none;padding-top:8px}
  .topbar{padding:12px 16px;flex-wrap:wrap}
  .content{padding:18px 16px 48px}
  #running{margin:0 16px}
  .lh-arrow{display:none}
}
</style>
<script>
// Shared status bus. Defined in <head> because widget scripts (Arby) execute
// mid-parse, before the dashboard script at the end of <body>. The dashboard
// publishes every /api/status payload here; widgets subscribe instead of
// polling the heaviest endpoint on their own.
window.TA = (function(){
  var subs = [], last = null;
  return {
    onStatus: function(fn){ subs.push(fn); if (last){ try { fn(last); } catch(e){} } },
    publish: function(d){ last = d; subs.slice().forEach(function(fn){ try { fn(d); } catch(e){} }); },
    last: function(){ return last; },
    refresh: function(){},            // replaced by the dashboard script
    navigate: function(p){ location.hash = '#/' + p; },
  };
})();
</script>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-mark">TA</div>
      <div>
        <div class="brand-name">Trade Agent</div>
        <div class="brand-sub">Paper desk · research tool</div>
      </div>
    </div>
    <nav class="nav">
      <div class="nav-group">Desk</div>
      <a class="nav-item active" data-page="overview" href="#/overview"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg><span>Overview</span><span class="nav-dot" id="health-dot" title="Learning-loop health"></span></a>
      <a class="nav-item" data-page="today" href="#/today"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg><span>Today's Plan</span></a>
      <a class="nav-item" data-page="runner" href="#/runner"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg><span>Runner</span></a>
      <a class="nav-item" data-page="playbook" href="#/playbook"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg><span>Playbook</span></a>
      <a class="nav-item" data-page="research" href="#/research"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M21.21 15.89A10 10 0 1 1 8 2.83"/><path d="M22 12A10 10 0 0 0 12 2v10z"/></svg><span>Research</span></a>
      <div class="nav-group">System</div>
      <a class="nav-item" data-page="history" href="#/history"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3"/></svg><span>History</span></a>
      <a class="nav-item" data-page="runs" href="#/runs"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg><span>Runs</span></a>
    </nav>
    <div class="sidebar-foot">
      <a class="nav-item" href="/logout"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg><span>Sign out</span></a>
    </div>
  </aside>
  <div class="shell">
    <header class="topbar">
      <div class="topbar-title">
        <h1 id="page-title">Overview</h1>
        <div class="sub" id="page-sub">Equity, edge, and today at a glance</div>
      </div>
      <div class="topbar-actions">
        <span class="updated" id="updated">—</span>
        <button class="btn ghost" onclick="loadData()">Refresh</button>
      </div>
    </header>
    <div id="running"><span class="spinner"></span>A loop is running — this can take 1–4 minutes (extended thinking + web search). Do not close this tab.</div>
    <main class="content">
      <section class="page active" data-page="overview">
        <!--ARBY-->
        <div id="overview-body"><div class="empty">Loading…</div></div>
      </section>
      <section class="page" data-page="today"><div id="today-body"><div class="empty">Loading…</div></div></section>
      <section class="page" data-page="runner"><div id="runner-body"><div class="empty">Loading…</div></div></section>
      <section class="page" data-page="playbook"><div id="playbook-body"><div class="empty">Loading…</div></div></section>
      <section class="page" data-page="research"><div id="research-body"><div class="empty">Loading…</div></div></section>
      <section class="page" data-page="history">
        <div class="card">
          <div class="card-head"><h2>Raw ledger inspector</h2><span class="card-sub">every whitelisted table, one session at a time</span></div>
          <div class="history-controls">
            <label>Date<select id="historyDate"></select></label>
            <label>Table<select id="historyTable">
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
              <option value="runner_sessions">Runner sessions</option>
              <option value="runner_candidates">Runner candidates</option>
              <option value="runner_candidate_outcomes">Runner outcomes</option>
              <option value="live_orders">Live paper orders</option>
              <option value="playbook_versions">Playbook versions (latest, any date)</option>
              <option value="llm_archive">LLM response archive (latest, any date)</option>
            </select></label>
            <button class="btn" onclick="loadHistory()">View</button>
          </div>
          <div id="historyResult" class="history-meta"></div>
          <div id="historyTableView" class="table-wrap"></div>
        </div>
      </section>
      <section class="page" data-page="runs">
        <div class="card">
          <div class="card-head"><h2>Run a loop</h2><span class="card-sub">trigger the desk's scheduled jobs on demand</span></div>
          <div class="run-controls">
            <label class="check"><span><input type="checkbox" id="mock"> Test mode</span><small>mock LLM, no cost</small></label>
            <label class="check"><span><input type="checkbox" id="offline"> Offline data</span><small>skip market data calls</small></label>
            <label class="check"><span><input type="checkbox" id="force"> Force</span><small>ignore market-day check</small></label>
          </div>
          <div class="run-buttons">
            <button class="btn primary" onclick="runLoop('morning')">Run Morning</button>
            <button class="btn primary" onclick="runLoop('evening')">Run Evening</button>
            <button class="btn primary" onclick="runLoop('weekly')">Run Weekly</button>
            <button class="btn" onclick="runLoop('runner')">Run Runner Scan</button>
          </div>
          <pre id="log" class="log" style="display:none"></pre>
        </div>
        <div class="card">
          <div class="card-head"><h2>Loop run history</h2></div>
          <div id="runs-table"></div>
        </div>
      </section>
    </main>
  </div>
</div>
<script>
'use strict';
const $ = id => document.getElementById(id);
const esc = t => String(t ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const fmtMoney = n => '$' + Number(n||0).toLocaleString(undefined,{maximumFractionDigits:0});
const fmtR = n => (n==null || isNaN(n)) ? '—' : (Number(n)>=0?'+':'') + Number(n).toFixed(2);
const rCls = n => n==null ? 'dim' : (n>0?'pos':'neg');
// Display precision for prices: stored levels carry float noise (27.40220444444445).
const px = v => (v==null || v==='' || isNaN(v)) ? '—' : Number(v).toFixed(Math.abs(Number(v)) < 1 ? 4 : 2);
const empty = m => `<div class="empty">${m}</div>`;
const kpi = (label, value, sub, cls) => `<div class="stat ${cls||''}"><div class="stat-label">${label}</div><div class="stat-value">${value}</div><div class="stat-sub">${sub||''}</div></div>`;

let state = null;

const PAGES = {
  overview:{t:'Overview', s:'Equity, edge, and today at a glance'},
  today:{t:"Today's Plan", s:'The paper account, the morning call, and the shadow ledger'},
  runner:{t:'Runner', s:'The intraday scan, select, grade lane'},
  playbook:{t:'Playbook', s:"The desk's accumulated rules"},
  research:{t:'Research', s:'What the desk has concluded & changed'},
  history:{t:'History', s:'Raw ledger inspector, one session at a time'},
  runs:{t:'Runs', s:'Trigger a loop and review run history'},
};

function currentPage(){ return (location.hash||'#/overview').replace('#/','') || 'overview'; }
function renderRoute(){
  const p = PAGES[currentPage()] ? currentPage() : 'overview';
  document.querySelectorAll('.page').forEach(s => s.classList.toggle('active', s.dataset.page===p));
  document.querySelectorAll('.nav-item[data-page]').forEach(a => a.classList.toggle('active', a.dataset.page===p));
  $('page-title').textContent = PAGES[p].t;
  $('page-sub').textContent = PAGES[p].s;
  if(p === 'history' && !historyDatesLoaded) loadHistoryDates();
  if(p === 'today') loadPaper();
}
let historyDatesLoaded = false;
window.addEventListener('hashchange', renderRoute);

// One status request at a time, shared by the page and every widget on it.
// A failed refresh keeps the last good payload on screen rather than blanking
// the desk -- a transient 500 should read as "stale", not "empty".
let inflight = null, lastLoaded = 0, busy = false;
const REFRESH_MS = 60000;

function loadData(){
  if(inflight) return inflight;
  inflight = (async () => {
    try {
      const r = await fetch('/api/status', {credentials:'same-origin'});
      if(r.status === 401){ location.href = '/login'; return; }
      if(!r.ok){
        let msg = 'HTTP ' + r.status;
        try { const j = await r.json(); if(j && j.error) msg += ' — ' + j.error; } catch(_) {}
        throw new Error(msg);
      }
      state = await r.json();
      lastLoaded = Date.now();
      window.TA.publish(state);
      renderAll();
      tickUpdated();
    } catch(e) {
      toast('Could not load data: ' + e.message, 'error');
      $('updated').textContent = state ? 'refresh failed — showing last data' : 'load failed';
      if(!state) renderAll();
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}
window.TA.refresh = loadData;

function tickUpdated(){
  if(!lastLoaded) return;
  const s = Math.round((Date.now() - lastLoaded) / 1000);
  $('updated').textContent = 'Updated ' + (s < 5 ? 'just now' : s < 60 ? s + 's ago' : Math.round(s/60) + 'm ago');
}

function renderAll(){
  renderOverview(); renderToday(); renderRunner(); renderPlaybook(); renderResearch(); renderRuns();
  renderHealthDot();
}

function renderHealthDot(){
  const h = (state && state.loop_health) || {};
  const dot = $('health-dot');
  if(!dot) return;
  dot.className = 'nav-dot ' + ({ok:'ok', warn:'warn', bad:'bad'}[h.overall] || '');
  dot.title = 'Learning loop: ' + ({ok:'healthy', warn:'needs attention', bad:'a stage is failing'}[h.overall] || 'unknown');
}

function toast(msg, kind){
  const t = document.createElement('div');
  t.className = 'toast ' + (kind||'info');
  t.textContent = msg;
  document.body.appendChild(t);
  requestAnimationFrame(() => t.classList.add('show'));
  setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 250); }, 4200);
}

// Only the desk's own controls -- never Arby's widget buttons.
function setBusy(on){
  busy = on;
  document.querySelectorAll('.run-buttons button, .topbar button, .history-controls button')
    .forEach(b => b.disabled = on);
}

function equityChart(curve){
  if(!curve || curve.length < 2) return empty('Not enough graded history yet — the equity curve appears after the first few triggered trades.');
  const W=760, H=230, PL=56, PR=16, PT=16, PB=32;
  const vals = curve.map(p => Number(p.equity)||0);
  const lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals), span = (hi-lo)||1;
  const X = i => PL + (curve.length===1 ? 0 : i*(W-PL-PR)/(curve.length-1));
  const Y = v => PT + (1-(v-lo)/span)*(H-PT-PB);
  let line = '', dots = '';
  curve.forEach((p,i) => {
    const cx = X(i).toFixed(1), cy = Y(p.equity).toFixed(1);
    line += (i ? ' L' : 'M') + cx + ' ' + cy;
    dots += `<circle cx="${cx}" cy="${cy}" r="2.6"><title>${esc(p.date)} — ${fmtMoney(p.equity)} (${fmtR(p.day_r)}R)</title></circle>`;
  });
  const area = 'M' + X(0).toFixed(1) + ' ' + (H-PB) + ' L' + line.slice(1) + ' L' + X(curve.length-1).toFixed(1) + ' ' + (H-PB) + ' Z';
  const up = vals[vals.length-1] >= vals[0];
  const c = up ? '#3fb950' : '#f85149';
  let grid = '';
  for(let g=0; g<=3; g++){
    const v = lo + (hi-lo)*g/3, gy = Y(v).toFixed(1);
    grid += `<line x1="${PL}" y1="${gy}" x2="${W-PR}" y2="${gy}"/>`;
    grid += `<text x="${PL-8}" y="${(+gy+4).toFixed(1)}" text-anchor="end">$${Math.round(v).toLocaleString()}</text>`;
  }
  const first = curve[0], last = curve[curve.length-1];
  return `<div class="equity-chart">
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Equity curve">
      <g class="eq-grid">${grid}</g>
      <path class="eq-area" d="${area}" fill="${c}" opacity="0.12"/>
      <path class="eq-line" d="${line}" fill="none" stroke="${c}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      <g class="eq-dots" fill="${c}">${dots}</g>
    </svg>
    <div class="eq-meta"><span>${esc(first.date)}</span><span class="eq-last" style="color:${c}">${fmtMoney(last.equity)}</span><span>${esc(last.date)}</span></div>
  </div>`;
}
const NO_EDGE = /^No positive edge/;
const HEALTH_LABEL = {ok:'HEALTHY', warn:'NEEDS ATTENTION', bad:'STAGE FAILING', unknown:'UNKNOWN'};
const HEALTH_PILL = {ok:'ok', warn:'warn', bad:'bad'};

function healthCard(){
  const h = state.loop_health || {};
  const stages = h.stages || [];
  if(!stages.length && !(h.warnings||[]).length) return '';
  const flow = stages.map(s => `<div class="lh-stage ${esc(s.status)}"><div class="lh-dot"></div><div style="min-width:0">
      <div class="lh-name">${esc(s.label)}</div><div class="lh-head">${esc(s.headline)}</div>
      ${s.detail ? `<div class="lh-detail">${esc(s.detail)}</div>` : ''}</div></div>`).join('<div class="lh-arrow">→</div>');
  const sg = h.signals || {};
  const sig = sg.window_days ? `<div class="lh-signals"><span>Last ${sg.window_days} days:</span>
      <span><b>${sg.outcomes_graded ?? 0}</b> trades graded</span>
      <span><b>${sg.runner_candidates_priced ?? 0}</b> runner candidates priced</span>
      <span><b>${sg.rejections_scored ?? 0}</b> rejections scored</span>
      <span><b>${sg.lessons_created ?? 0}</b> lessons created</span>
      <span><b>${sg.lesson_confirmations ?? 0}</b> lesson confirmations</span>
      <span><b>${sg.rule_confirmations ?? 0}</b> rule confirmations</span>
      <span><b>${sg.active_lessons ?? 0}</b> lessons in the bank</span></div>` : '';
  const warns = (h.warnings||[]).length
    ? `<ul class="lh-warn">${h.warnings.map(w => `<li>${esc(w)}</li>`).join('')}</ul>` : '';
  const pill = HEALTH_PILL[h.overall] || 'neutral';
  return `<div class="card lh ${esc(h.overall||'')}"><div class="card-head"><h2>Learning loop</h2>
      <span class="pill ${pill}">${HEALTH_LABEL[h.overall] || 'UNKNOWN'}</span></div>
      <div class="lh-flow">${flow}</div>${sig}${warns}</div>`;
}

// The morning lane's streak clamp and drawdown breaker read exactly these
// lane-scoped numbers (the runner lane is shown for comparison; it has no
// breaker of its own).
function laneCard(key, title, sub){
  const L = (state.lanes || {})[key];
  if(!L) return '';
  const o = L.overall || {}, dd = L.drawdown || {};
  const pause = Number(state.drawdown_pause_r ?? -4);
  const sum = Number(dd.sum_weighted_r || 0);
  let guard = '';
  if(key === 'morning'){
    const used = pause < 0 ? Math.max(0, Math.min(1, -sum / -pause)) : 0;
    const armed = (dd.n||0) >= 5;
    const tripped = armed && sum <= pause;
    const close = armed && !tripped && sum <= pause + 1.5;
    const cls = tripped ? 'bad' : close ? 'warn' : '';
    guard = `<div class="lane-row"><span>Drawdown breaker</span><span class="${tripped?'neg':''}">${fmtR(sum)}R of ${pause.toFixed(1)}R</span></div>
      <div class="guard-bar" title="Last ${dd.n||0} triggered trades, size-weighted"><div class="guard-fill ${cls}" style="width:${(sum < 0 ? used*100 : 0).toFixed(0)}%"></div></div>
      <div class="stat-sub">${tripped ? 'CAPITAL PRESERVATION MODE is active' : armed ? `last ${dd.n} triggered trades` : `arms at 5 triggered trades (${dd.n||0} so far)`}</div>`;
  }
  return `<div class="lane"><h3>${esc(title)}<span class="card-sub">${esc(sub)}</span></h3>
    <div class="lane-row"><span>Graded / triggered</span><span>${o.picks_graded ?? 0} / ${o.triggered ?? 0}</span></div>
    <div class="lane-row"><span>Win rate</span><span>${o.win_rate!=null ? Math.round(o.win_rate*100)+'%' : '—'}</span></div>
    <div class="lane-row"><span>Avg R</span><span class="${rCls(o.avg_r)}">${fmtR(o.avg_r)}</span></div>
    <div class="lane-row"><span>Total weighted R</span><span>${fmtR(o.total_weighted_r)}</span></div>
    <div class="lane-row"><span>Streak</span><span>${esc(L.streak || '—')}</span></div>
    ${guard}</div>`;
}

function renderOverview(){
  const b = $('overview-body');
  if(!state){ b.innerHTML = empty('Loading…'); return; }
  const o = state.overall || {};
  const best = (state.conclusions||{}).best_edge || '';
  const plan = state.plan;
  let planMini;
  if(plan && plan.no_trade) planMini = `<div class="kpi-line"><span class="pill warn">NO TRADE</span><span>${esc(plan.no_trade_reason||'')}</span></div>`;
  else if(plan && (plan.picks||[]).length) planMini = `<div class="kpi-line"><span class="pill ok">${plan.picks.length} PICKS</span><span>${plan.picks.map(p=>esc(p.ticker)).join(', ')}</span></div>`;
  else if(plan) planMini = `<div class="kpi-line"><span class="pill info">PLAN READY</span><span>${esc(plan.market_stance||'')}</span></div>`;
  else planMini = `<div class="kpi-line"><span class="pill neutral">NO PLAN</span><span>The morning call hasn't run for today.</span></div>`;

  const lanes = state.lanes ? `<div class="card"><div class="card-head"><h2>By lane</h2>
      <span class="card-sub">two strategies, never pooled — each lane's risk rules read only its own record</span></div>
      <div class="lanes">${laneCard('morning', 'Morning lane', 'premarket analyst + risk manager')}${laneCard('runner', 'Runner lane', 'intraday scan + selector')}</div></div>` : '';
  b.innerHTML = `
    ${healthCard()}
    <div class="stat-grid">
      ${kpi('Graded trades', o.picks_graded ?? 0, 'all lanes')}
      ${kpi('Win rate', o.win_rate!=null ? Math.round(o.win_rate*100)+'%' : '—', (o.triggered??0)+' triggered')}
      ${kpi('Avg R', fmtR(o.avg_r), 'per triggered trade', o.avg_r>0?'ok':(o.avg_r<0?'bad':''))}
      ${kpi('Total weighted R', fmtR(o.total_weighted_r), 'size-adjusted', o.total_weighted_r>0?'ok':(o.total_weighted_r<0?'bad':''))}
      ${kpi('Paper equity', fmtMoney(state.equity), 'each lane at its own $/R')}
      ${kpi('Streak', state.streak || '—', 'all lanes')}
    </div>
    <div class="card"><div class="card-head"><h2>Equity curve</h2><span class="card-sub">paper P&L, size-adjusted, each lane at its own $/R</span></div>${equityChart(state.equity_curve||[])}</div>
    ${lanes}
    ${best ? `<div class="card best-edge ${NO_EDGE.test(best) ? 'none' : ''}"><div class="card-head"><h2>Current best edge</h2></div><p>${esc(best)}</p></div>` : ''}
    <div class="card"><div class="card-head"><h2>Performance by setup</h2><span class="card-sub">realized edge per playbook setup</span></div>${setupTable()}</div>
    <div class="card"><div class="card-head"><h2>Today at a glance</h2><span class="card-sub">${esc(state.today)}</span></div>${planMini}</div>
  `;
}

function setupTable(){
  const rows = state.by_setup || [];
  if(!rows.length) return empty('No setup performance yet.');
  return `<div class="table-wrap"><table>
    <thead><tr><th>Setup</th><th class="num">N</th><th class="num">Triggered</th><th class="num">Win rate</th><th class="num">Avg R</th><th class="num">Total wR</th></tr></thead>
    <tbody>${rows.map(s=>`<tr><td>${esc(s.dim)}</td><td class="num">${s.n}</td><td class="num">${s.triggered}</td>
      <td class="num">${s.win_rate!=null?Math.round(s.win_rate*100)+'%':'—'}</td>
      <td class="num ${rCls(s.avg_r)}">${fmtR(s.avg_r)}</td>
      <td class="num ${rCls(s.total_weighted_r)}">${fmtR(s.total_weighted_r)}</td></tr>`).join('')}</tbody></table></div>`;
}
function planHtml(){
  const plan = state.plan;
  if(!plan) return '<div class="empty">No plan for today yet.</div>';
  if(plan.no_trade){
    return `<div class="kpi-line" style="margin-bottom:8px"><span class="pill warn">NO TRADE</span><span>${esc(plan.no_trade_reason||'')}</span></div><p>${esc(plan.market_stance||'')}</p>`;
  }
  const source = (state.picks_today && state.picks_today.length) ? state.picks_today : (plan.picks||[]);
  if(!source.length) return `<p>${esc(plan.market_stance||'')}</p><div class="empty">Plan has no picks yet.</div>`;
  const rows = source.map(p=>{
    const entryType = p.entry_type || (p.entry_plan && p.entry_plan.type) || 'open';
    return `<tr><td>${esc(p.ticker)}</td><td>${esc(p.direction)}</td><td>${esc(p.setup_type||'')}</td>
      <td>${esc(entryType)} @ ${px(p.entry_price)}</td><td>${px(p.stop_price)}</td>
      <td>${px(p.target_price)}</td><td>${esc(p.size_r ?? '—')}R</td><td>${esc(p.conviction ?? '—')}</td>
      <td class="wrap">${esc(p.thesis_final||p.thesis||'')}</td></tr>`;
  }).join('');
  return `<p>${esc(plan.market_stance||'')}</p>
    <div class="table-wrap"><table><thead><tr><th>Ticker</th><th>Dir</th><th>Setup</th><th>Entry (type @ price)</th><th>Stop</th><th>Target</th><th>Size</th><th>Conv</th><th>Thesis</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function shadowHtml(){
  const rejections = state.rejections_today || [];
  if(!rejections.length) return '<div class="empty">Nothing rejected today — every candidate that survived analysis made the plan.</div>';
  const rows = rejections.map(r=>{
    const outcome = r.eod_outcome
      ? `${esc(r.eod_outcome)}${r.forgone_r!=null ? ' (' + fmtR(r.forgone_r) + 'R)' : ''}`
      : 'ungraded';
    return `<tr><td>${esc(r.ticker)}</td><td>${esc(r.direction||'')}</td><td>${esc(r.setup_type||'')}</td>
      <td>${esc(r.entry_type||'')} @ ${px(r.entry_price)}</td><td>${px(r.stop_price)}</td>
      <td>${px(r.target_price)}</td><td>${esc(r.conviction ?? '—')}</td>
      <td class="wrap">${esc(r.reason||'')}</td><td class="wrap">${esc(r.thesis||'')}</td><td>${outcome}</td></tr>`;
  }).join('');
  return `<div class="table-wrap"><table><thead><tr><th>Ticker</th><th>Dir</th><th>Setup</th><th>Entry (type @ price)</th><th>Stop</th><th>Target</th><th>Conv</th><th>Kill reason</th><th>Thesis</th><th>Shadow outcome</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

// ---------------------------------------------------------- paper account
// Loaded only while the Today page is open (it calls the broker), refreshed
// with the page's regular refresh.
let paperState = null, paperLoading = null;

function loadPaper(){
  if(paperLoading) return paperLoading;
  paperLoading = (async () => {
    try {
      const r = await fetch('/api/paper', {credentials:'same-origin'});
      if(!r.ok) throw new Error('HTTP ' + r.status);
      paperState = await r.json();
    } catch(e) {
      paperState = {error: e.message};
    } finally {
      paperLoading = null;
    }
    const box = $('paper-body');
    if(box) box.innerHTML = paperHtml();
  })();
  return paperLoading;
}

const etTime = iso => {
  if(!iso) return '—';
  const t = new Date(iso);
  return isNaN(t) ? esc(iso) : t.toLocaleString('en-US', {timeZone:'America/New_York', month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}) + ' ET';
};
const STATUS_PILL = s => ({filled:'ok', partially_filled:'info', new:'info', accepted:'info', held:'info',
  pending_new:'info', canceled:'neutral', expired:'neutral', rejected:'bad', replaced:'neutral'}[String(s||'').toLowerCase()] || 'neutral');

function paperHtml(){
  const p = paperState;
  if(!p) return empty('Loading the paper account…');
  if(p.enabled === false) return empty('Live paper trading is off (config.json → live_paper.enabled).');
  const a = p.account || {};
  const dayPl = (a.equity!=null && a.last_equity!=null) ? a.equity - a.last_equity : null;
  const age = p.heartbeat_age_min;
  const mon = (age == null)
    ? `<span class="pill bad">MONITOR NEVER RAN</span><span>Nothing is enforcing time stops or the 15:55 flatten on live positions.</span>`
    : (age <= 15
        ? `<span class="pill ok">MONITOR LIVE</span><span>last intraday tick ${age < 1 ? 'under a minute' : Math.round(age) + ' min'} ago</span>`
        : `<span class="pill warn">MONITOR IDLE</span><span>last intraday tick ${age < 120 ? Math.round(age) + ' min' : (age/60).toFixed(1) + ' h'} ago — expected every 5 min during the session</span>`);
  const pos = p.positions || [];
  const posHtml = pos.length ? `<div class="table-wrap"><table><thead><tr><th>Symbol</th><th>Side</th><th class="num">Qty</th><th class="num">Avg entry</th><th class="num">Last</th><th class="num">Unrealized</th></tr></thead><tbody>${
      pos.map(x => `<tr><td><b>${esc(x.symbol)}</b></td><td>${esc(x.side)}</td><td class="num">${esc(x.qty)}</td>
        <td class="num">${px(x.avg_entry_price)}</td><td class="num">${px(x.current_price)}</td>
        <td class="num ${rCls(x.unrealized_pl)}">${x.unrealized_pl==null?'—':fmtMoney(x.unrealized_pl).replace('$-','-$')} ${x.unrealized_plpc==null?'':'('+(x.unrealized_plpc*100).toFixed(2)+'%)'}</td></tr>`).join('')
    }</tbody></table></div>` : empty('No open positions.');
  const ords = p.orders || [];
  const ordHtml = ords.length ? `<div class="table-wrap" style="max-height:420px"><table><thead><tr><th>Submitted</th><th>Symbol</th><th>Side</th><th class="num">Qty</th><th>Type</th><th>Status</th><th class="num">Fill</th><th>Exit legs</th></tr></thead><tbody>${
      ords.map(o => `<tr><td>${etTime(o.submitted_at)}</td><td><b>${esc(o.symbol)}</b></td><td>${esc(o.side)}</td><td class="num">${esc(o.qty)}</td>
        <td>${esc(o.type)}${o.order_class && o.order_class!=='simple' ? ' · '+esc(o.order_class) : ''}${o.limit_price!=null?' @ '+px(o.limit_price):''}</td>
        <td><span class="pill ${STATUS_PILL(o.status)}">${esc(o.status)}</span></td>
        <td class="num">${px(o.filled_avg_price)}</td>
        <td>${(o.legs||[]).map(l => `${esc(l.type)}: ${esc(l.status)}${l.filled_avg_price!=null?' @ '+px(l.filled_avg_price):''}`).join('<br>') || '—'}</td></tr>`).join('')
    }</tbody></table></div>` : empty('No orders on the paper account yet.');
  return `
    ${p.error ? `<ul class="lh-warn"><li>Broker: ${esc(p.error)}</li></ul>` : ''}
    <div class="stat-grid">
      ${kpi('Paper equity', a.equity!=null ? fmtMoney(a.equity) : '—', 'Alpaca paper account')}
      ${kpi('Today', dayPl==null ? '—' : (dayPl>=0?'+':'-') + fmtMoney(Math.abs(dayPl)), 'vs. prior close', dayPl>0?'ok':(dayPl<0?'bad':''))}
      ${kpi('Open positions', pos.length, pos.length ? pos.map(x => x.symbol).join(', ') : 'flat')}
      ${kpi('Buying power', a.buying_power!=null ? fmtMoney(a.buying_power) : '—', a.status || '')}
    </div>
    <div class="kpi-line" style="margin-bottom:12px">${mon}</div>
    ${p.runner_note ? `<p class="muted-note">Runner orders today: ${esc(p.runner_note)}</p>` : ''}
    <h3>Open positions</h3>${posHtml}
    <h3>Recent broker orders</h3>${ordHtml}
    <p class="muted-note">Straight from the broker. Morning picks go in as bracket orders at the open; runner picks at the 10:30 selection. The intraday monitor cancels entries still unfilled at their time stop and flattens every position at 15:55 ET.</p>`;
}

function renderToday(){
  const b = $('today-body');
  if(!state){ b.innerHTML = empty('Loading…'); return; }
  b.innerHTML = `
    <div class="card"><div class="card-head"><h2>Paper account</h2><span class="card-sub">live Alpaca paper trading — what the broker actually holds</span></div><div id="paper-body">${paperHtml()}</div></div>
    <div class="card"><div class="card-head"><h2>Today's plan — ${esc(state.today)}</h2>${state.plan?`<span class="pill neutral">playbook v${esc(state.playbook_version||'')}</span>`:''}</div>${planHtml()}</div>
    <div class="card"><div class="card-head"><h2>Shadow ledger</h2><span class="card-sub">candidates the desk killed, and what they did anyway</span></div>${shadowHtml()}</div>
  `;
}
function renderRunner(){
  const b = $('runner-body');
  if(!state){ b.innerHTML = empty('Loading…'); return; }
  const rn = state.runner || {};
  if(!rn.ran){
    b.innerHTML = `<div class="card"><div class="card-head"><h2>Runner Lane — ${esc(state.today)}</h2></div>${empty(rn.health || 'No scan recorded for today.')}</div>`;
    return;
  }
  const bad = /^DEGRADED|^NO SCAN/.test(rn.health || '');
  const quiet = /stand-down/.test(rn.health || '');
  // An auto-refresh rebuilds this page; keep the filter box's focus and caret.
  const focused = document.activeElement;
  const hadFocus = !!(focused && focused.id === 'rn-q');
  const caret = hadFocus ? focused.selectionStart : 0;
  const table = (rn.rows||[]).length
    ? `<div class="filter-row"><input type="text" id="rn-q" placeholder="Filter ticker…" value="${esc(runnerFilter.q)}" autocomplete="off">
        <label><input type="checkbox" id="rn-sel" ${runnerFilter.sel?'checked':''}> selected only</label>
        <label><input type="checkbox" id="rn-trig" ${runnerFilter.trig?'checked':''}> triggered only</label>
        <span class="card-sub" id="rn-count"></span></div><div id="rn-table"></div>`
    : '';
  let moversHtml = '';
  const movers = rn.movers || [];
  if(movers.length){
    const overlap = movers.filter(m=>m.selected).length;
    const mvRows = movers.map(m=>{
      const rc = m.r_multiple==null ? '#8b949e' : (m.r_multiple>0 ? '#3fb950' : '#f85149');
      const tag = m.selected ? `<span style="color:#3fb950;font-weight:700">#${m.rank} picked</span>` : `<span style="color:#8b949e">not picked</span>`;
      return `<tr><td><b>${esc(m.ticker)}</b></td><td class="num" style="color:${rc}">${m.r_multiple==null?'—':Number(m.r_multiple).toFixed(2)}</td><td>${esc(m.exit_reason||'')}</td><td>${tag}</td></tr>`;
    }).join('');
    moversHtml = `<h3>Top ${movers.length} movers today (by realized R)</h3>
      <p class="muted-note">${overlap} of ${movers.length} were selected. Same-day eyeball check, not a training signal — one day's winners are noise to retune a selector on. The FDR-corrected version, across many sessions, feeds the weekly miner instead.</p>
      <div class="table-wrap"><table><thead><tr><th>Ticker</th><th class="num">R</th><th>Exit</th><th></th></tr></thead><tbody>${mvRows}</tbody></table></div>`;
  }
  let picksHtml = '';
  if((rn.picks||[]).length){
    const pRows = rn.picks.map(p=>`<tr><td><b>${esc(p.ticker)}</b></td><td>${esc(p.direction)}</td><td>${esc(p.conviction||'')}</td>
      <td class="num">${esc(p.size_r ?? '—')}R</td><td class="num">${px(p.entry_price)}</td><td class="num">${px(p.stop_price)}</td>
      <td class="num">${px(p.target_price)}</td><td class="wrap">${esc(p.thesis||'')}</td></tr>`).join('');
    picksHtml = `<h3>Selected picks (${rn.picks.length})</h3>
      <div class="table-wrap"><table><thead><tr><th>Ticker</th><th>Dir</th><th>Conviction</th><th>Size</th><th>Entry</th><th>Stop</th><th>Target</th><th>Thesis</th></tr></thead><tbody>${pRows}</tbody></table></div>`;
  }
  b.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>Runner Lane — ${esc(state.today)}</h2><span class="pill ${bad?'bad':quiet?'warn':'ok'}">${esc(rn.health||'')}</span></div>
      ${rn.skipped_reason ? `<p class="muted-note">selection: ${esc(rn.skipped_reason)}</p>` : ''}
      <div class="stat-grid">
        ${kpi('Scanned', rn.universe||0, '')}
        ${kpi('Candidates', rn.n_candidates||0, '')}
        ${kpi('Selected', rn.n_selected||0, '')}
        ${kpi('Live orders', rn.n_orders||0, '')}
      </div>
      ${picksHtml}
      ${moversHtml}
      <h3>All candidates</h3>${table || '<div class="empty">No candidates.</div>'}
      <p class="muted-note">Every candidate is priced by the evening simulator, selected or not — that is what makes "did picking help?" answerable. Each is entered no earlier than the scan tick that found it and the selection decision, so picks and field are priced from the same moment. Rows marked #N were chosen.</p>
    </div>`;
  if(table){
    if(hadFocus){
      const inp = $('rn-q'); inp.focus();
      try { inp.setSelectionRange(caret, caret); } catch(_) {}
    }
    $('rn-q').addEventListener('input', e => { runnerFilter.q = e.target.value; renderRunnerTable(); });
    $('rn-sel').addEventListener('change', e => { runnerFilter.sel = e.target.checked; renderRunnerTable(); });
    $('rn-trig').addEventListener('change', e => { runnerFilter.trig = e.target.checked; renderRunnerTable(); });
    renderRunnerTable();
  }
}

// Kept across refreshes so the 60s auto-refresh never resets what you typed.
const runnerFilter = {q:'', sel:false, trig:false};

function renderRunnerTable(){
  const box = $('rn-table');
  if(!box || !state) return;
  const all = ((state.runner||{}).rows) || [];
  const q = runnerFilter.q.trim().toUpperCase();
  const rows = all.filter(r => (!q || String(r.ticker||'').toUpperCase().includes(q))
    && (!runnerFilter.sel || r.selected) && (!runnerFilter.trig || r.triggered));
  $('rn-count').textContent = rows.length === all.length ? `${all.length} candidates` : `${rows.length} of ${all.length}`;
  if(!rows.length){ box.innerHTML = '<div class="empty">No candidates match.</div>'; return; }
  const n = (v, dg) => (v==null ? '—' : Number(v).toFixed(dg==null?2:dg));
  const body = rows.map(r=>{
    const sel = r.selected ? `<span class="pos" style="font-weight:700">#${esc(r.rank)}</span>` : '';
    return `<tr><td>${sel}</td><td><b>${esc(r.ticker)}</b></td><td>${esc(r.detection_et||'')}</td>
      <td class="num">${n(r.price)}</td><td class="num">${n(r.stop)}</td><td class="num">${n(r.target)}</td>
      <td class="num">${n(r.rvol)}</td><td class="num">${n(r.vwap_pct)}</td><td class="num">${n(r.tc,1)}</td>
      <td class="num">${n(r.change_pct)}</td><td class="num ${rCls(r.r_multiple)}">${n(r.r_multiple)}</td>
      <td>${esc(r.exit_reason||'')}</td></tr>`;
  }).join('');
  box.innerHTML = `<div class="table-wrap" style="max-height:620px"><table><thead><tr><th></th><th>Ticker</th><th>Det ET</th><th class="num">Price</th><th class="num">Stop</th><th class="num">Target</th><th class="num">RVOL</th><th class="num">VWAP%</th><th class="num">TC</th><th class="num">Chg%</th><th class="num">R</th><th>Exit</th></tr></thead><tbody>${body}</tbody></table></div>`;
}
function renderPlaybook(){
  const b = $('playbook-body');
  if(!state){ b.innerHTML = empty('Loading…'); return; }
  const html = state.playbook_html ? state.playbook_html : `<pre>${esc(state.playbook||'')}</pre>`;
  b.innerHTML = `<div class="card"><div class="card-head"><h2>The playbook</h2><span class="card-sub">v${esc(state.playbook_version||'—')}</span></div><div class="prose">${html}</div></div>`;
}

function renderResearch(){
  const b = $('research-body');
  if(!state){ b.innerHTML = empty('Loading…'); return; }
  const cc = state.conclusions || {};

  let bestEdgeHtml = '<div class="empty">Not enough triggered trades yet to declare a current best edge (needs ≥3 per setup).</div>';
  if(cc.best_edge) bestEdgeHtml = `<p>${esc(cc.best_edge)}</p>`;

  let lessonsHtml = '<div class="empty">No active lessons yet.</div>';
  if((cc.lessons||[]).length){
    lessonsHtml = `<div class="table-wrap"><table><thead><tr><th>Lesson</th><th>Setup</th><th>Confidence</th><th>+/-</th><th>Status</th></tr></thead><tbody>` +
      cc.lessons.map(l=>`<tr><td>${esc(l.lesson)}</td><td>${esc(l.setup_type||'')}</td><td>${esc(l.confidence||'')}</td>
        <td style="white-space:nowrap">+${l.confirmations??0} / -${l.contradictions??0}</td><td>${esc(l.status||'')}</td></tr>`).join('') + '</tbody></table></div>';
  }

  let questionsHtml = '<div class="empty">No open-question progress recorded yet.</div>';
  if((cc.open_questions||[]).length){
    questionsHtml = `<div class="table-wrap"><table><thead><tr><th>Question</th><th>Progress</th><th>Events</th><th>Last</th><th>Recent notes</th></tr></thead><tbody>` +
      cc.open_questions.map(q=>`<tr><td>Q${q.question_id}</td><td>${q.total}</td><td>${q.events}</td>
        <td>${esc(q.last_event||'')}</td><td>${esc((q.recent_notes||[]).join(' · '))}</td></tr>`).join('') + '</tbody></table></div>';
  }

  let calHtml = '<div class="empty">No calibration data yet.</div>';
  if((cc.calibration||[]).length){
    calHtml = `<div class="table-wrap"><table><thead><tr><th>Stated conviction</th><th>N</th><th>Win rate</th><th>Avg R</th></tr></thead><tbody>` +
      cc.calibration.map(c=>{
        const wr = c.suppressed ? `<span title="suppressed: n < ${c.min_n}">—</span>` : (c.win_rate!=null ? Math.round(c.win_rate*100)+'%' : '—');
        const ar = c.suppressed ? '—' : (c.avg_r ?? '—');
        return `<tr><td>${esc(c.conviction||'')}</td><td>${c.n}</td><td>${wr}</td><td>${ar}</td></tr>`;
      }).join('') + '</tbody></table></div>';
  }

  const mg = cc.management || {};
  let mgHtml = '<div class="empty">No management (counterfactual) data yet.</div>';
  if(Object.keys(mg).length){
    mgHtml = `<div class="table-wrap"><table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>
      <tr><td>Triggered trades</td><td>${mg.triggered??0}</td></tr>
      <tr><td>Avg realized R</td><td>${mg.avg_r ?? '—'} (n=${mg.avg_r_n??0})</td></tr>
      <tr><td>Avg same-name open→close (baseline)</td><td>${mg.avg_baseline_r ?? '—'} (n=${mg.avg_baseline_r_n??0})</td></tr>
      <tr><td><b>Edge vs baseline (paired)</b></td><td><b>${mg.edge_vs_baseline_r ?? '—'}</b> R (n=${mg.edge_vs_baseline_n??0})</td></tr>
      <tr><td><b>Management edge vs hold-to-close (paired)</b></td><td><b>${mg.management_edge_r ?? '—'}</b> R (n=${mg.management_edge_n??0})</td></tr>
      <tr><td>Stopped</td><td>${mg.stopped??0}${mg.stopped_then_target ? ` (${mg.stopped_then_target} hit target after)` : ''}</td></tr>
      <tr><td>Avg EOD R on stopped trades</td><td>${mg.avg_eod_r_on_stopped ?? '—'}</td></tr>
      <tr><td>Untriggered (fussy entries) w/ baseline</td><td>${mg.untriggered_with_baseline??0} — walked away from ${mg.avg_baseline_r_untriggered ?? '—'}R</td></tr></tbody></table></div>`;
  }

  let seHtml = '<div class="empty">No weekly selection-edge history yet (the weekly miner writes this).</div>';
  if((cc.selection_edge||[]).length){
    seHtml = `<div class="table-wrap"><table><thead><tr><th>Week ending</th><th>Sessions</th><th>Edge R</th><th>p</th><th>Verdict</th></tr></thead><tbody>` +
      cc.selection_edge.map(r=>`<tr><td>${esc(r.week_ending||'')}</td><td>${r.sessions??''}</td>
        <td>${r.edge_r ?? '—'}</td><td>${r.p_value!=null ? Number(r.p_value).toFixed(4) : '—'}</td>
        <td>${esc(r.verdict||'')}</td></tr>`).join('') + '</tbody></table></div>';
  }

  b.innerHTML = `
    <div class="card best-edge ${NO_EDGE.test(cc.best_edge || '') ? 'none' : ''}"><div class="card-head"><h2>Current best edge</h2><span class="card-sub">the setup the desk is most profitable in right now</span></div>${bestEdgeHtml}</div>
    <div class="card"><div class="card-head"><h2>Active lessons</h2></div>${lessonsHtml}</div>
    <div class="card"><div class="card-head"><h2>Open questions</h2></div>${questionsHtml}</div>
    <div class="card"><div class="card-head"><h2>Calibration</h2><span class="card-sub">does stated conviction track realized R?</span></div>${calHtml}</div>
    <div class="card"><div class="card-head"><h2>Management quality</h2><span class="card-sub">stop/target machinery vs hold-to-close</span></div>${mgHtml}</div>
    <div class="card"><div class="card-head"><h2>Runner selection edge</h2><span class="card-sub">weekly, FDR-corrected</span></div>${seHtml}</div>
  `;
}
function renderRuns(){
  const b = $('runs-table');
  if(!state){ b.innerHTML = empty('Loading…'); return; }
  const runs = state.runs || [];
  if(!runs.length){ b.innerHTML = empty('No loop runs recorded yet.'); return; }
  const rows = runs.map(r=>{
    const err = r.error ? esc(r.error) : '';
    const short = err.length > 90 ? err.slice(0,90) + '&hellip;' : err;
    const cell = err ? `<td class="err" title="${err}">${short}</td>` : '<td></td>';
    const st = (r.status||'').toLowerCase();
    const stCls = (st==='ok' || st==='success') ? 'ok' : ((st==='error'||st==='failed') ? 'bad' : 'neutral');
    return `<tr><td>${esc(r.loop)}</td><td>${esc(r.run_date)}</td><td><span class="pill ${stCls}">${esc(r.status)}</span></td>
      <td>${esc(r.model||'')}</td><td class="num">$${(r.cost_usd||0).toFixed(3)}</td>${cell}</tr>`;
  }).join('');
  b.innerHTML = `<div class="table-wrap"><table><thead><tr><th>Loop</th><th>Date</th><th>Status</th><th>Model</th><th class="num">Cost</th><th>Error</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

async function runLoop(name){
  $('running').style.display = 'block';
  $('log').style.display = 'block';
  $('log').textContent = 'Starting ' + name + ' loop...';
  setBusy(true);
  const params = new URLSearchParams({
    mock: $('mock').checked ? '1' : '0',
    offline: $('offline').checked ? '1' : '0',
    force: $('force').checked ? '1' : '0',
  });
  try {
    const r = await fetch('/run/' + name + '?' + params.toString(), {method:'POST'});
    const text = await r.text();
    let d;
    try { d = JSON.parse(text); } catch { d = null; }
    if(d){
      $('log').textContent = (d.log || '') + (d.error ? '\n\nERROR: ' + d.error : '');
    } else {
      $('log').textContent = 'Request failed (HTTP ' + r.status + '): ' + text.slice(0, 2000);
    }
  } catch(e) {
    $('log').textContent = 'Request failed: ' + e;
  } finally {
    setBusy(false);
    $('running').style.display = 'none';
    loadData();
  }
}

async function loadHistoryDates(){
  const result = $('historyResult');
  try {
    const r = await fetch('/api/history/dates');
    const d = await r.json();
    if(!r.ok) throw new Error(d.error || 'Could not load dates');
    const select = $('historyDate');
    select.innerHTML = (d.dates || []).map(day => `<option value="${esc(day)}">${esc(day)}</option>`).join('');
    historyDatesLoaded = true;
    if(!d.dates || !d.dates.length){
      result.textContent = 'No historical sessions have been recorded yet.';
      return;
    }
    await loadHistory();
  } catch(e) {
    result.textContent = 'Could not load history: ' + e.message;
  }
}

async function loadHistory(){
  const date = $('historyDate').value;
  const table = $('historyTable').value;
  const result = $('historyResult');
  const view = $('historyTableView');
  if(!date) return;
  result.textContent = 'Loading...';
  view.innerHTML = '';
  try {
    // Archive tables are not date-filtered server-side and carry large text
    // bodies: fetch only the latest few.
    const undated = /^(llm_archive|playbook_versions)$/.test(table);
    const r = await fetch(`/api/table/${table}?date=${encodeURIComponent(date)}&limit=${undated ? 20 : 1000}`);
    const d = await r.json();
    if(!r.ok) throw new Error(d.error || 'Could not load records');
    result.textContent = `${d.n} record${d.n === 1 ? '' : 's'} in ${table.replaceAll('_',' ')}` +
      (undated ? ' (latest first, not filtered by date)' : ` for ${date}`);
    if(!d.n){
      view.innerHTML = '<div class="empty">No records for this date.</div>';
      return;
    }
    const headers = d.columns || [];
    const headerHtml = headers.map(column => `<th>${historyText(column)}</th>`).join('');
    const rowsHtml = d.rows.map(row => '<tr>' + headers.map(column => `<td>${historyText(row[column])}</td>`).join('') + '</tr>').join('');
    view.innerHTML = `<table><thead><tr>${headerHtml}</tr></thead><tbody>${rowsHtml}</tbody></table>`;
  } catch(e) {
    result.textContent = 'Could not load history: ' + e.message;
  }
}

function historyText(value){
  if(value === null || value === undefined) return '—';
  return String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
}

loadData();
renderRoute();
// Live desk: refresh every minute while the tab is visible and no loop is
// running (a run refreshes on completion anyway), and immediately on return
// to a tab that has gone stale.
setInterval(() => {
  if(document.hidden || busy) return;
  loadData();
  if(currentPage() === 'today') loadPaper();
}, REFRESH_MS);
setInterval(tickUpdated, 5000);
document.addEventListener('visibilitychange', () => {
  if(!document.hidden && !busy && Date.now() - lastLoaded > 20000) loadData();
});
</script>
</body>
</html>
"""
