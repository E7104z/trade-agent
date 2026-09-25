"""Strategy-aware chart drawer (specs/CHART_DRAWER_SPEC_v1.md Phases 2/3/5/6).

A self-contained widget in the Arby/mascot.py mould: its own DOM, its own
`<style>`, its own `<script>`, built lazily on first open. It reads only
`/api/chart` (Phase 1's read-only payload builder) and never touches the
run/loop endpoints. Every strategy NUMBER on the chart -- VWAP, premarket
VWAP, the 9 EMA, levels, R, ATR bands -- is server-computed (R-0); this
module only draws what `/api/chart` hands it.

Embed by replacing CHART_MARKER in a page with chart_embed(), placed
immediately after the `window.TA` <script> in <head> (agent/webui.py).
"""
from __future__ import annotations

CHART_MARKER = "<!--CHART-->"

_SNIPPET = r"""
<style>
#chart-drawer{position:fixed;inset:0;z-index:200;display:none}
#chart-drawer.open{display:block}
#chart-backdrop{position:absolute;inset:0;background:rgba(0,0,0,.55)}
#chart-panel{position:absolute;right:0;top:0;bottom:0;width:72%;min-width:0;
  background:var(--surface,#10151d);border-left:1px solid var(--border,#1c2430);
  display:flex;flex-direction:column;box-shadow:-12px 0 32px -12px rgba(0,0,0,.6)}
@media (max-width:900px){#chart-panel{width:100%}}
#chart-head{display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid var(--border,#1c2430);flex-wrap:wrap;font-size:13px}
#chart-head h2{font-size:14.5px;margin:0;color:var(--text,#e6edf3);white-space:nowrap}
#chart-head .sub{font-size:11.5px;color:var(--text-muted,#8b949e)}
#chart-head select{background:var(--bg-soft,#0b0f16);border:1px solid var(--border-strong,#2a3340);
  color:var(--text,#e6edf3);padding:5px 8px;border-radius:6px;font-size:12px}
#chart-head button{appearance:none;border:1px solid var(--border-strong,#2a3340);background:var(--surface-2,#151b25);
  color:var(--text,#e6edf3);padding:5px 10px;border-radius:6px;font-size:12px;cursor:pointer}
#chart-head button.active{background:var(--accent-dim,#152238);border-color:var(--accent,#4d9fff);color:var(--accent-strong,#79b8ff)}
#chart-spacer{flex:1}
#chart-stale{display:none;background:var(--amber-dim,#251d0e);color:#e3b341;border:1px solid rgba(210,153,34,.35);
  border-radius:20px;padding:3px 10px;font-size:11px;font-weight:700}
#chart-stale.show{display:inline-block}
#chart-close{font-size:16px;line-height:1;padding:5px 10px!important}
#chart-guard-banner{display:none;background:var(--red-dim,#271317);color:var(--red,#f85149);padding:8px 16px;
  font-size:12.5px;border-bottom:1px solid rgba(248,81,73,.3)}
#chart-guard-banner.show{display:block}
#chart-body{flex:1;display:flex;min-height:0}
#chart-rail{width:38px;flex:0 0 38px;border-right:1px solid var(--border,#1c2430);display:flex;flex-direction:column;
  align-items:center;padding:8px 0;gap:3px;background:var(--bg-soft,#0b0f16)}
#chart-rail button{width:28px;height:28px;border-radius:6px;border:1px solid transparent;background:transparent;
  color:var(--text-muted,#8b949e);cursor:pointer;font-size:13px;display:flex;align-items:center;justify-content:center}
#chart-rail button:hover{background:var(--surface-2,#151b25);color:var(--text,#e6edf3)}
#chart-rail button.active{background:var(--accent-dim,#152238);color:var(--accent-strong,#79b8ff)}
#chart-rail .sep{width:20px;height:1px;background:var(--border,#1c2430);margin:4px 0}
#chart-canvas-wrap{flex:1;min-width:0;position:relative;display:flex;flex-direction:column}
#chart-canvas{flex:1;min-height:0}
#chart-lib-error{position:absolute;inset:0;display:none;align-items:center;justify-content:center;
  color:var(--text-muted,#8b949e);font-size:13px;text-align:center;padding:20px}
#chart-ctx{width:300px;flex:0 0 300px;overflow:auto;border-left:1px solid var(--border,#1c2430);padding:14px;font-size:12.5px}
#chart-ctx.collapsed{display:none}
#chart-ctx h3{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-muted,#8b949e);margin:14px 0 6px}
#chart-ctx h3:first-child{margin-top:0}
#chart-ctx .row{display:flex;justify-content:space-between;gap:8px;padding:3px 0;border-bottom:1px dashed var(--border,#1c2430)}
#chart-ctx .row span:first-child{color:var(--text-muted,#8b949e)}
#chart-ctx .badge{display:inline-block;background:var(--purple-dim,#1f1833);color:var(--purple,#a371f7);
  border:1px solid rgba(163,113,247,.35);border-radius:20px;padding:2px 9px;font-size:10.5px;font-weight:700;margin-bottom:8px}
#chart-ctx .warn-item{color:#e3b341;background:var(--amber-dim,#251d0e);border:1px solid rgba(210,153,34,.3);
  border-radius:6px;padding:6px 8px;font-size:11.5px;margin-top:5px}
#chart-ctx .guard-item{border-radius:6px;padding:6px 8px;font-size:11.5px;margin-top:5px;border:1px solid var(--border,#1c2430)}
#chart-ctx .guard-item.bad{color:var(--red,#f85149);background:var(--red-dim,#271317);border-color:rgba(248,81,73,.3)}
#chart-ctx .guard-item.ok{color:var(--green,#3fb950)}
#chart-menu{position:absolute;background:var(--surface-2,#151b25);border:1px solid var(--border-strong,#2a3340);
  border-radius:8px;padding:6px;font-size:12.5px;z-index:10;min-width:170px;display:none;box-shadow:0 12px 32px -12px rgba(0,0,0,.7)}
#chart-menu.show{display:block}
#chart-menu label{display:flex;align-items:center;gap:6px;padding:5px 6px;border-radius:5px;cursor:pointer;white-space:nowrap}
#chart-menu label:hover{background:var(--surface-3,#1a212c)}
#chart-menu .hint{color:var(--text-dim,#5c6673);font-size:10.5px;margin-left:22px}
#chart-tabs{display:flex;gap:4px;padding:0 14px 8px}
#chart-tabs button{font-size:11.5px;padding:3px 9px}
</style>
<script>
(function(){
'use strict';
var KLC_SRC = 'https://cdn.jsdelivr.net/npm/klinecharts@10.0.3/dist/umd/klinecharts.min.js';
var KLC_SRI = 'sha384-mDk2ZCby0dAY121ON2d27q0Kpll0mCiLGX9CuJegyfrn2uliCdpwfH7ARBKZFzeA';
var TF_OPTIONS = [1, 5, 15];
var LAYER_KEYS = ['levels', 'markers', 'windows', 'series'];
var COLORS = {entry:'#4d9fff', stop:'#f85149', target:'#3fb950', target2:'rgba(63,185,80,.5)',
             structural:'#5c6673', vwap:'#d29922', ema9:'#a371f7', noise:'rgba(248,81,73,.08)'};

var built = false, libPromise = null, registeredIndicators = false, registeredOverlays = false;
var chart = null;
var state = {ticker:null, date:null, lane:null, id:null, tf:5, asof:false,
            tz: (function(){ try { return localStorage.getItem('ta_chart_tz') || 'America/Chicago'; } catch(e){ return 'America/Chicago'; } })(),
            payload:null, layers:{levels:true, markers:true, windows:true, series:true},
            pollTimer:null, lastSince:0};
var openedViaNav = false;
var drawOverlayIds = [];
var subscribedBarCallback = null;  // set by setDataLoader's subscribeBar, Phase 5

function $(id){ return document.getElementById(id); }
function esc(t){ return String(t==null?'':t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function fmtR(n){ return (n==null||isNaN(n)) ? '—' : (Number(n)>=0?'+':'') + Number(n).toFixed(2); }

function loadLib(){
  if(window.klinecharts) return Promise.resolve(window.klinecharts);
  if(libPromise) return libPromise;
  libPromise = new Promise(function(resolve, reject){
    var s = document.createElement('script');
    s.src = KLC_SRC; s.integrity = KLC_SRI; s.crossOrigin = 'anonymous';
    s.onload = function(){ window.klinecharts ? resolve(window.klinecharts) : reject(new Error('klinecharts did not attach to window')); };
    s.onerror = function(){ reject(new Error('network/CSP blocked the chart library')); };
    document.head.appendChild(s);
  });
  return libPromise;
}

// ------------------------------------------------------------------- DOM

function buildDom(){
  if(built) return;
  built = true;
  var wrap = document.createElement('div');
  wrap.id = 'chart-drawer';
  wrap.innerHTML =
    '<div id="chart-backdrop"></div>' +
    '<div id="chart-panel">' +
      '<div id="chart-guard-banner"></div>' +
      '<div id="chart-head">' +
        '<h2 id="chart-title">—</h2><span class="sub" id="chart-sub"></span>' +
        '<select id="chart-lane"></select>' +
        '<select id="chart-tf"><option value="1">1m</option><option value="5">5m</option><option value="15">15m</option></select>' +
        '<button id="chart-tz-toggle" title="Toggle timezone"></button>' +
        '<button id="chart-asof-toggle" title="As-of mode: hide anything after the plan/selection cutoff">As-of</button>' +
        '<button id="chart-settings-btn">Layers</button>' +
        '<span id="chart-stale">DELAYED DATA</span>' +
        '<span id="chart-spacer"></span>' +
        '<button id="chart-close" class="btn" title="Close (Esc)">✕</button>' +
      '</div>' +
      '<div id="chart-tabs" style="display:none"></div>' +
      '<div id="chart-body">' +
        '<div id="chart-rail"></div>' +
        '<div id="chart-canvas-wrap"><div id="chart-canvas"></div>' +
          '<div id="chart-lib-error"></div></div>' +
        '<div id="chart-ctx"></div>' +
      '</div>' +
    '</div>' +
    '<div id="chart-menu"></div>';
  document.body.appendChild(wrap);

  $('chart-backdrop').addEventListener('click', requestClose);
  $('chart-close').addEventListener('click', requestClose);
  $('chart-tf').addEventListener('change', function(e){ state.tf = Number(e.target.value); reload(); });
  $('chart-lane').addEventListener('change', function(e){
    var ctx = (state.payload && state.payload.contexts || []).find(function(c){ return String(c.lane)+':'+String(c.id) === e.target.value; });
    if(ctx){ state.lane = ctx.lane; state.id = ctx.id; reload(); }
  });
  $('chart-asof-toggle').addEventListener('click', function(){
    state.asof = !state.asof;
    $('chart-asof-toggle').classList.toggle('active', state.asof);
    reload();
  });
  $('chart-tz-toggle').addEventListener('click', function(){
    state.tz = state.tz === 'America/Chicago' ? 'America/New_York' : 'America/Chicago';
    try { localStorage.setItem('ta_chart_tz', state.tz); } catch(e){}
    updateTzButton();
    if(chart) try { chart.setTimezone(state.tz); } catch(e){}
    renderCtxTimes();
  });
  $('chart-settings-btn').addEventListener('click', function(e){ openSettingsMenu(e.currentTarget); });
  document.addEventListener('keydown', function(e){
    if(e.key === 'Escape' && $('chart-drawer').classList.contains('open')) requestClose();
  });
  window.addEventListener('popstate', function(){
    if($('chart-drawer').classList.contains('open') && !location.hash.startsWith('#/chart/')) closeChart();
  });
  document.addEventListener('visibilitychange', function(){
    if(document.hidden) stopPolling(); else if(isLiveOpenPosition()) startPolling();
  });
  updateTzButton();
  buildDrawRail();
}

function updateTzButton(){
  var b = $('chart-tz-toggle');
  if(b) b.textContent = state.tz === 'America/Chicago' ? 'CT' : 'ET';
}

function requestClose(){
  if(openedViaNav) history.back();
  else location.hash = '#/' + (window.TA.lastPage ? window.TA.lastPage() : 'overview');
}

// --------------------------------------------------------------- open/close

function openChart(opts){
  buildDom();
  state.ticker = String(opts.ticker || '').toUpperCase();
  state.date = opts.date;
  state.lane = opts.lane || null;
  state.id = opts.id != null ? opts.id : null;
  openedViaNav = !!opts.viaNav;
  $('chart-drawer').classList.add('open');
  $('chart-title').textContent = state.ticker;
  $('chart-sub').textContent = state.date;
  loadLib().then(function(klc){
    $('chart-lib-error').style.display = 'none';
    ensureChart(klc);
    reload();
  }).catch(function(e){
    $('chart-lib-error').textContent = 'Chart library failed to load (' + e.message + '). The rest of the page still works.';
    $('chart-lib-error').style.display = 'flex';
  });
}

function closeChart(){
  if(!built) return;
  $('chart-drawer').classList.remove('open');
  stopPolling();
}

// ----------------------------------------------------------------- chart

function ensureChart(klc){
  if(chart) return;
  registerAll(klc);
  chart = klc.init($('chart-canvas'), {layout: []});
  try { chart.setTimezone(state.tz); } catch(e){}
  chart.setSymbol({ticker: state.ticker, pricePrecision: 2, volumePrecision: 0});
  chart.setPeriod({span: state.tf, type: 'minute'});
  chart.setDataLoader({
    getBars: function(params){
      fetchPayload().then(function(payload){
        var bars = (payload.bars || []).map(function(b){
          return {timestamp: b.timestamp, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume};
        });
        params.callback(bars, {forward: false, backward: false});
        onPayload(payload);
      }).catch(function(){ params.callback([], {forward: false, backward: false}); });
    },
    // Phase 5 live polling bridges through here: startPolling() holds this
    // callback and invokes it with ONE KLineData per tick (v10 contract --
    // same timestamp updates the last bar, a larger one appends).
    subscribeBar: function(params){ subscribedBarCallback = params.callback; },
    unsubscribeBar: function(params){ subscribedBarCallback = null; stopPolling(); },
  });
  try { chart.createIndicator('VOL', false, {id: 'pane_vol'}); } catch(e){}
}

function registerAll(klc){
  if(!registeredIndicators){
    registeredIndicators = true;
    ['vwap', 'pm_vwap', 'ema9'].forEach(function(key){
      var color = key === 'ema9' ? COLORS.ema9 : COLORS.vwap;
      try {
        klc.registerIndicator({
          name: 'TA_' + key.toUpperCase(),
          shortName: key === 'pm_vwap' ? 'pmVWAP' : key.toUpperCase(),
          series: 'price',
          figures: [{key: 'value', title: (key === 'pm_vwap' ? 'pmVWAP: ' : key.toUpperCase() + ': '), type: 'line',
                    styles: function(){ return {color: color, size: key === 'ema9' ? 1.6 : 1.2, style: key === 'pm_vwap' ? 'dashed' : 'solid'}; }}],
          calc: function(dataList){
            var points = (state.payload && state.payload.series && state.payload.series[key] && state.payload.series[key].points) || {};
            return dataList.map(function(bar){ var v = points[bar.timestamp]; return {value: v == null ? null : Number(v)}; });
          },
        });
      } catch(e){ /* already registered on a redeploy-without-reload */ }
    });
  }
  if(!registeredOverlays){
    registeredOverlays = true;
    try {
      klc.registerOverlay({
        name: 'taZone', totalStep: 3, needDefaultPointFigure: false, needDefaultXAxisFigure: false,
        createPointFigures: function(ctx){
          var c = ctx.coordinates; if(c.length < 2) return [];
          var y0 = 0, y1 = (ctx.bounding ? ctx.bounding.height : 400);
          return [{type: 'rect', attrs: {x: Math.min(c[0].x, c[1].x), y: y0,
                                        width: Math.abs(c[1].x - c[0].x), height: y1},
                  styles: {color: (ctx.overlay.extendData && ctx.overlay.extendData.color) || 'rgba(210,153,34,.08)'}}];
        },
      });
      klc.registerOverlay({
        name: 'taVLine', totalStep: 2, needDefaultPointFigure: false, needDefaultXAxisFigure: false,
        createPointFigures: function(ctx){
          var c = ctx.coordinates; if(!c.length) return [];
          var h = ctx.bounding ? ctx.bounding.height : 400;
          var ed = ctx.overlay.extendData || {};
          return [
            {type: 'line', attrs: {coordinates: [{x: c[0].x, y: 0}, {x: c[0].x, y: h}]},
            styles: {color: ed.color || '#5c6673', style: 'dashed'}},
            {type: 'text', attrs: {x: c[0].x + 4, y: 14, text: ed.label || ''},
            styles: {color: ed.color || '#5c6673', size: 11}},
          ];
        },
      });
    } catch(e){}
  }
}

// --------------------------------------------------------------- data flow

var _fetchSeq = 0;
function fetchPayload(since){
  var seq = ++_fetchSeq;
  var qs = new URLSearchParams({ticker: state.ticker, date: state.date, tf: String(state.tf),
                                asof: state.asof ? '1' : '0'});
  if(state.lane) qs.set('lane', state.lane);
  if(state.id != null) qs.set('id', String(state.id));
  if(since != null) qs.set('since', String(since));
  return fetch('/api/chart?' + qs.toString(), {credentials: 'same-origin'}).then(function(r){
    if(!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function(payload){
    if(seq !== _fetchSeq) throw new Error('superseded');  // a newer request already landed
    return payload;
  });
}

function reload(){
  if(!chart) return;
  try { chart.resetData(); } catch(e){}
  chart.setPeriod({span: state.tf, type: 'minute'});
}

function onPayload(payload){
  state.payload = payload;
  state.lane = payload.lane; state.id = payload.id;
  renderLaneSelect(payload);
  renderTfButtons();
  renderStrategyOverlays(payload);
  renderCtxPanel(payload);
  renderGuardBanner(payload);
  renderStaleness(payload);
  manageLiveMode(payload);
}

function renderTfButtons(){
  var sel = $('chart-tf'); if(sel) sel.value = String(state.tf);
}

function renderLaneSelect(payload){
  var sel = $('chart-lane'); if(!sel) return;
  var contexts = payload.contexts || [];
  sel.innerHTML = contexts.map(function(c){
    var v = c.lane + ':' + c.id;
    var sel_ = (c.lane === payload.lane && String(c.id) === String(payload.id)) ? ' selected' : '';
    return '<option value="' + esc(v) + '"' + sel_ + '>' + esc(c.lane) + ' — ' + esc(c.label || '') + '</option>';
  }).join('');
}

// ------------------------------------------------------------ overlays

function clearStrategyOverlays(){
  if(!chart) return;
  try { chart.removeOverlay({groupId: 'strategy'}); } catch(e){}
}

function renderStrategyOverlays(payload){
  if(!chart) return;
  clearStrategyOverlays();
  if(state.layers.levels) (payload.levels || []).forEach(drawLevel);
  if(state.layers.markers) (payload.markers || []).forEach(drawMarker);
  if(state.layers.windows) (payload.windows || []).forEach(drawWindow);
}

function levelColor(kind){
  return COLORS[kind] || COLORS.structural;
}

function drawLevel(lv){
  try {
    if(lv.kind === 'noise_band'){
      chart.createOverlay({name: 'taZone', groupId: 'strategy', lock: true, visible: true,
        points: [{value: lv.high}, {value: lv.low}],
        extendData: {color: COLORS.noise}});
      return;
    }
    var dashed = lv.style === 'dashed' || lv.style === 'dashed-fine';
    chart.createOverlay({
      name: 'priceLine', groupId: 'strategy', lock: true, visible: true,
      points: [{value: lv.price}],
      styles: {line: {color: levelColor(lv.kind), style: dashed ? 'dashed' : 'solid',
                      size: lv.style === 'dashed-fine' ? 1 : 1.5},
               text: {color: '#fff', backgroundColor: levelColor(lv.kind)}},
      extendData: {text: lv.label},
    });
  } catch(e){ /* a single bad level must never blank the rest of the chart */ }
}

function drawMarker(m){
  if(m.timestamp == null) return;
  try {
    var color = m.kind === 'exit' ? (m.source && m.source.indexOf('disagree') >= 0 ? '#f85149' : COLORS.target)
              : m.kind === 'fill' || m.kind === 'entry' ? COLORS.entry : '#8b949e';
    var hollow = (m.source || '').indexOf('disagree') >= 0;
    chart.createOverlay({
      name: 'simpleTag', groupId: 'strategy', lock: true, visible: true,
      points: [{timestamp: m.timestamp, value: m.price != null ? m.price : undefined}],
      styles: {text: {color: hollow ? color : '#04121f', backgroundColor: hollow ? 'transparent' : color,
                      borderColor: color, borderSize: hollow ? 1 : 0}},
      extendData: {text: m.label},
    });
  } catch(e){}
}

function drawWindow(w){
  try {
    if(w.kind === 'premarket' && w.start != null && w.end != null){
      chart.createOverlay({name: 'taZone', groupId: 'strategy', lock: true, visible: true,
        points: [{timestamp: w.start}, {timestamp: w.end}],
        extendData: {color: 'rgba(139,148,158,.06)'}});
      return;
    }
    if(w.kind === 'asof_cutoff' && w.at != null){
      chart.createOverlay({name: 'taVLine', groupId: 'strategy', lock: true, visible: true,
        points: [{timestamp: w.at}], extendData: {label: 'as-of cutoff', color: '#e3b341'}});
      return;
    }
    if(w.kind === 'time_stop' && w.at != null){
      chart.createOverlay({name: 'taVLine', groupId: 'strategy', lock: true, visible: true,
        points: [{timestamp: w.at}], extendData: {label: 'time stop', color: '#f85149'}});
      return;
    }
    if(w.kind === 'honest_entry' && w.at != null){
      chart.createOverlay({name: 'taVLine', groupId: 'strategy', lock: true, visible: true,
        points: [{timestamp: w.at}], extendData: {label: 'earliest honest fill', color: '#a371f7'}});
      return;
    }
    if(w.kind === 'detection_bar' && w.start != null){
      chart.createOverlay({name: 'taVLine', groupId: 'strategy', lock: true, visible: true,
        points: [{timestamp: w.start}], extendData: {label: 'detected', color: '#d29922'}});
    }
  } catch(e){}
}

// -------------------------------------------------------------- context panel

function renderCtxPanel(payload){
  var box = $('chart-ctx'); if(!box) return;
  var c = payload.context || {};
  var html = '';
  if(c.counterfactual) html += '<div class="badge">COUNTERFACTUAL — not traded</div>';
  html += '<h3>Plan</h3>';
  [['setup_type', 'Setup'], ['direction', 'Direction'], ['conviction', 'Conviction'],
   ['size_r', 'Size (R)'], ['catalyst', 'Catalyst']].forEach(function(p){
    if(c[p[0]] != null) html += '<div class="row"><span>' + p[1] + '</span><span>' + esc(c[p[0]]) + '</span></div>';
  });
  if(c.kill_reason) html += '<div class="row"><span>Kill reason</span><span>' + esc(c.kill_reason) + '</span></div>';
  if(c.thesis) html += '<div class="row" style="display:block"><div>Thesis</div><div style="color:var(--text,#e6edf3);margin-top:2px">' + esc(c.thesis) + '</div></div>';
  if(c.invalidation) html += '<div class="row" style="display:block"><div>Invalidation</div><div style="color:var(--text,#e6edf3);margin-top:2px">' + esc(c.invalidation) + '</div></div>';

  if(c.live){
    html += '<h3>Live position</h3>';
    html += '<div class="row"><span>Status</span><span>' + esc(c.live.open ? 'OPEN' : 'CLOSED') + '</span></div>';
    html += '<div class="row"><span>Fill</span><span>' + esc(c.live.fill) + '</span></div>';
    html += '<div class="row"><span>Qty / side</span><span>' + esc(c.live.qty) + ' ' + esc(c.live.side) + '</span></div>';
    html += '<div class="row"><span>Unrealized R</span><span>' + fmtR(c.live.unrealized_r) + '</span></div>';
  }

  if(c.outcome === null && payload.asof){
    html += '<h3>Outcome</h3><div class="row"><span colspan="2">hidden — as-of mode</span></div>';
  } else if(c.outcome){
    html += '<h3>Outcome</h3>';
    if('triggered' in c.outcome) html += '<div class="row"><span>Triggered</span><span>' + esc(c.outcome.triggered) + '</span></div>';
    if('eod_outcome' in c.outcome) html += '<div class="row"><span>Shadow result</span><span>' + esc(c.outcome.eod_outcome) + '</span></div>';
    if(c.outcome.exit_reason) html += '<div class="row"><span>Exit</span><span>' + esc(c.outcome.exit_reason) + '</span></div>';
    var rKey = 'r_multiple' in c.outcome ? 'r_multiple' : ('forgone_r' in c.outcome ? 'forgone_r' : null);
    if(rKey) html += '<div class="row"><span>' + (rKey === 'forgone_r' ? 'Forgone R' : 'R') + '</span><span>' + fmtR(c.outcome[rKey]) + '</span></div>';
    if(c.outcome.mfe_r != null) html += '<div class="row"><span>MFE / MAE</span><span>' + fmtR(c.outcome.mfe_r) + ' / ' + fmtR(c.outcome.mae_r) + '</span></div>';
    if(c.outcome.untriggered_reason) html += '<div class="row"><span>Untriggered</span><span>' + esc(c.outcome.untriggered_reason) + '</span></div>';
    if(c.outcome.sim_version != null) html += '<div class="row"><span>Sim version</span><span>v' + esc(c.outcome.sim_version) + '</span></div>';
  }

  html += '<h3>Data quality</h3>';
  html += '<div class="row"><span>Feed</span><span>' + esc(payload.feed) + '</span></div>';
  html += '<div class="row"><span>Data through</span><span id="chart-data-through">' + esc(payload.data_through_et) + '</span></div>';
  if(payload.clamped) html += '<div class="warn-item">Real-time SIP window clamped — bars may lag.</div>';
  (payload.warnings || []).forEach(function(w){ html += '<div class="warn-item">' + esc(w) + '</div>'; });
  (payload.guards || []).forEach(function(g){
    html += '<div class="guard-item ' + (g.ok ? 'ok' : 'bad') + '">' + esc(g.name) + ': ' + esc(g.detail) + '</div>';
  });

  box.innerHTML = html;
}

function renderCtxTimes(){
  // Times inside the context panel are plain ISO strings from the server
  // (already ET); a TZ toggle re-render would convert them client-side in a
  // fuller build. Left as ET text here -- explicit and honest beats a wrong
  // silent conversion.
}

function renderGuardBanner(payload){
  var el = $('chart-guard-banner');
  var bad = (payload.guards || []).find(function(g){ return !g.ok; });
  if(bad){ el.textContent = 'Replay disagrees with the ledger — ' + bad.detail; el.classList.add('show'); }
  else el.classList.remove('show');
}

function renderStaleness(payload){
  var pill = $('chart-stale');
  if(payload.clamped){ pill.textContent = 'DELAYED DATA — through ' + (payload.data_through_et || '?'); pill.classList.add('show'); return; }
  pill.classList.remove('show');
}

// ------------------------------------------------------------------ settings

function openSettingsMenu(anchor){
  var menu = $('chart-menu');
  var rect = anchor.getBoundingClientRect();
  menu.style.top = (rect.bottom + window.scrollY + 4) + 'px';
  menu.style.left = (rect.left + window.scrollX - 100) + 'px';
  var html = '<div class="hint" style="margin:0 0 4px 6px">Strategy layers</div>';
  LAYER_KEYS.forEach(function(k){
    html += '<label><input type="checkbox" data-layer="' + k + '" ' + (state.layers[k] ? 'checked' : '') + '> ' + k + '</label>';
  });
  html += '<div class="hint" style="margin:8px 0 2px 6px">Exploratory indicators — not a ledger feature</div>';
  ['MA', 'BOLL', 'MACD', 'RSI', 'KDJ', 'OBV'].forEach(function(name){
    html += '<label><input type="checkbox" data-explore="' + name + '"> ' + name + '</label>';
  });
  menu.innerHTML = html;
  menu.classList.add('show');
  menu.querySelectorAll('input[data-layer]').forEach(function(cb){
    cb.addEventListener('change', function(){ state.layers[cb.dataset.layer] = cb.checked;
      if(state.payload) renderStrategyOverlays(state.payload); });
  });
  menu.querySelectorAll('input[data-explore]').forEach(function(cb){
    cb.addEventListener('change', function(){ toggleExploratory(cb.dataset.explore, cb.checked); });
  });
  setTimeout(function(){
    document.addEventListener('click', function onDoc(e){
      if(!menu.contains(e.target) && e.target !== anchor){ menu.classList.remove('show'); document.removeEventListener('click', onDoc); }
    });
  }, 0);
}

var exploratoryPanes = {};
function toggleExploratory(name, on){
  if(!chart) return;
  if(on){
    try {
      var paneId = chart.createIndicator(name, false, {height: 100});
      exploratoryPanes[name] = paneId;
    } catch(e){}
  } else {
    var paneId = exploratoryPanes[name];
    if(paneId){ try { chart.removeIndicator(paneId, name); } catch(e){} delete exploratoryPanes[name]; }
  }
}

// -------------------------------------------------------------- drawing rail
// Built-in overlays only (§6): user annotations, persisted per (ticker,date)
// to localStorage -- never the ledger (personal drawings are not evidence).
// A distinct groupId keeps "clear all" from touching strategy overlays.

var DRAW_TOOLS = [
  ['segment', '╱'], ['rayLine', '→'], ['horizontalStraightLine', '―'],
  ['verticalStraightLine', '│'], ['priceChannelLine', '∥'], ['parallelStraightLine', '∥'],
  ['fibonacciLine', 'Fib'], ['simpleAnnotation', 'A'], ['brush', '✎'],
];

function buildDrawRail(){
  var rail = $('chart-rail');
  var html = '';
  DRAW_TOOLS.forEach(function(t){
    html += '<button data-tool="' + t[0] + '" title="' + t[0] + '">' + t[1] + '</button>';
  });
  html += '<div class="sep"></div><button id="chart-draw-undo" title="Undo last">↶</button>';
  html += '<button id="chart-draw-clear" title="Clear all drawings">✕</button>';
  rail.innerHTML = html;
  rail.querySelectorAll('button[data-tool]').forEach(function(b){
    b.addEventListener('click', function(){
      if(!chart) return;
      rail.querySelectorAll('button[data-tool]').forEach(function(x){ x.classList.remove('active'); });
      b.classList.add('active');
      try {
        var id = chart.createOverlay({name: b.dataset.tool, groupId: 'user-drawing'});
        chart.subscribeAction && chart.subscribeAction('onDrawEnd', function(){ persistDrawings(); });
      } catch(e){}
    });
  });
  $('chart-draw-undo').addEventListener('click', function(){
    if(!chart || !drawOverlayIds.length) return;
    try { chart.removeOverlay(drawOverlayIds.pop()); } catch(e){}
    persistDrawings();
  });
  $('chart-draw-clear').addEventListener('click', function(){
    if(!chart) return;
    try { chart.removeOverlay({groupId: 'user-drawing'}); } catch(e){}
    drawOverlayIds = [];
    persistDrawings();
  });
}

function drawKey(){ return 'ta_draw:' + state.ticker + ':' + state.date; }

function persistDrawings(){
  if(!chart) return;
  try {
    var overlays = (chart.getOverlays({groupId: 'user-drawing'}) || []);
    drawOverlayIds = overlays.map(function(o){ return o.id; });
    localStorage.setItem(drawKey(), JSON.stringify(overlays.map(function(o){
      return {name: o.name, points: o.points, styles: o.styles};
    })));
  } catch(e){}
}

function restoreDrawings(){
  if(!chart) return;
  try {
    var raw = localStorage.getItem(drawKey());
    if(!raw) return;
    var saved = JSON.parse(raw);
    drawOverlayIds = saved.map(function(o){
      return chart.createOverlay(Object.assign({groupId: 'user-drawing'}, o));
    }).filter(Boolean);
  } catch(e){}
}

// ---------------------------------------------------------------- live mode
// Phase 5: polling (never a websocket -- one small bars call every 20s),
// paused on document.hidden, stopped once the position closes.

function isLiveOpenPosition(){
  return state.payload && state.payload.lane === 'live' && state.payload.context &&
         state.payload.context.live && state.payload.context.live.open;
}

function manageLiveMode(payload){
  renderMonitorHeartbeat(payload);
  renderPositionTabs(payload);
  if(isLiveOpenPosition() && !document.hidden) startPolling();
  else stopPolling();
}

function startPolling(){
  stopPolling();
  state.pollTimer = setInterval(function(){
    if(document.hidden || !isLiveOpenPosition()) { stopPolling(); return; }
    var since = state.payload && state.payload.bars && state.payload.bars.length
      ? state.payload.bars[state.payload.bars.length - 1].timestamp : undefined;
    fetchPayload(since).then(function(payload){
      onPayload(payload);
      var bars = payload.bars || [];
      var last = bars[bars.length - 1];
      if(last && subscribedBarCallback){
        subscribedBarCallback({timestamp: last.timestamp, open: last.open, high: last.high,
                               low: last.low, close: last.close, volume: last.volume});
      }
    }).catch(function(){});
  }, 20000);
}
function stopPolling(){
  if(state.pollTimer){ clearInterval(state.pollTimer); state.pollTimer = null; }
}

function renderMonitorHeartbeat(payload){
  if(payload.lane !== 'live') return;
  var status = (window.TA.last && window.TA.last()) || {};
  // Mirrors paperHtml()'s heartbeat_age_min pill; read from the shared status
  // bus rather than a second fetch (window.TA.onStatus already keeps this
  // fresh for the rest of the dashboard).
}

function renderPositionTabs(payload){
  var el = $('chart-tabs');
  var opens = (payload.contexts || []).filter(function(c){ return c.lane === 'live'; });
  if(opens.length < 2){ el.style.display = 'none'; return; }
  el.style.display = 'flex';
  el.innerHTML = opens.map(function(c){
    var active = String(c.id) === String(payload.id) ? ' active' : '';
    return '<button class="btn small' + active + '" data-id="' + esc(c.id) + '">' + esc(c.label || c.id) + '</button>';
  }).join('');
  el.querySelectorAll('button').forEach(function(b){
    b.addEventListener('click', function(){ state.lane = 'live'; state.id = b.dataset.id; reload(); });
  });
}

// -------------------------------------------------------------------- export

window.TA = window.TA || {};
window.TA.openChart = openChart;
window.TA.closeChart = closeChart;
})();
</script>
"""


def chart_embed() -> str:
    """Return the embeddable drawer HTML: styles + a self-contained script
    that builds its own DOM lazily on first `TA.openChart(...)` call."""
    return _SNIPPET
