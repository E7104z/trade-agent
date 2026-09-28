"""Strategy-aware chart drawer (specs/CHART_DRAWER_SPEC_v1.md Phases 2/3/5/6).

A self-contained widget in the Arby/mascot.py mould: its own DOM, its own
`<style>`, its own `<script>`, built lazily on first open. It reads only
`/api/chart` (Phase 1's read-only payload builder) and never touches the
run/loop endpoints. Every strategy NUMBER on the chart -- VWAP, the 9 EMA,
levels, risk, reward R, result R -- is server-computed (R-0); this module
only formats and draws what `/api/chart` hands it. Server series are mapped
onto the displayed bars by bar-close time (a value is shown on a bar only
once the bar it came from has closed), never recomputed.

Built against KLineChart v10's published index.d.ts -- not v9 docs: in v10
createIndicator takes the pane in the create object, removeIndicator takes a
filter object, and overlay lifecycle hooks (onDrawEnd, onRightClick) live on
the overlay itself, not on chart.subscribeAction.

Embed by replacing CHART_MARKER in a page with chart_embed(), placed
immediately after the `window.TA` <script> in <head> (agent/webui.py).
"""
from __future__ import annotations

CHART_MARKER = "<!--CHART-->"

_SNIPPET = r"""
<style>
#chart-drawer{position:fixed;inset:0;z-index:200;display:none;font-family:var(--sans,system-ui,sans-serif)}
#chart-drawer.open{display:block}
#chart-backdrop{position:absolute;inset:0;background:rgba(2,4,8,.62)}
#chart-panel{position:absolute;right:0;top:0;bottom:0;width:min(1560px,88vw);background:var(--surface,#10151d);
  border-left:1px solid var(--border,#1c2430);display:flex;flex-direction:column;box-shadow:-24px 0 48px -24px rgba(0,0,0,.85)}
@media (max-width:1000px){#chart-panel{width:100%}}
#chart-drawer button{font-family:inherit}
#chart-guard-banner{display:none;background:var(--red-dim,#271317);color:#ff7b72;padding:8px 16px;font-size:12.5px;
  border-bottom:1px solid rgba(248,81,73,.3)}
#chart-guard-banner.show{display:block}

/* header */
#chart-head{display:flex;align-items:center;gap:10px;padding:12px 14px 10px 18px;flex-wrap:wrap}
.ch-ticker{font-size:22px;font-weight:800;letter-spacing:-.01em;color:var(--text,#e6edf3);line-height:1}
.ch-date{font-size:13px;color:var(--text-muted,#8b949e);white-space:nowrap}
.ch-badge{font-size:10.5px;font-weight:800;letter-spacing:.07em;padding:4px 8px;border-radius:5px;text-transform:uppercase;white-space:nowrap;line-height:1}
.ch-badge.long{background:rgba(63,185,80,.14);color:#56d364}
.ch-badge.short{background:rgba(248,81,73,.14);color:#ff7b72}
.ch-badge.cf{background:var(--purple-dim,#1f1833);color:#c297ff}
.ch-badge.live{background:rgba(63,185,80,.14);color:#56d364}
.ch-badge.live::before{content:'';display:inline-block;width:6px;height:6px;border-radius:50%;background:#56d364;margin-right:6px;vertical-align:1px;animation:chPulse 1.6s infinite}
@keyframes chPulse{50%{opacity:.25}}
.ch-badge.warn{background:var(--amber-dim,#251d0e);color:#e3b341}
#chart-lane{appearance:none;background:var(--bg-soft,#0b0f16) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%238b949e' stroke-width='1.5' fill='none'/%3E%3C/svg%3E") no-repeat right 10px center;
  border:1px solid var(--border-strong,#2a3340);color:var(--text,#e6edf3);height:30px;padding:0 30px 0 10px;border-radius:7px;font-size:12.5px;max-width:340px;cursor:pointer}
#chart-lane:disabled{opacity:.6;cursor:default}
.ch-spacer{flex:1}
#chart-close{width:34px;height:34px;border-radius:8px;border:1px solid var(--border-strong,#2a3340);background:var(--surface-2,#151b25);
  color:var(--text-muted,#8b949e);font-size:15px;cursor:pointer;display:flex;align-items:center;justify-content:center}
#chart-close:hover{color:var(--text,#e6edf3);border-color:#3a4556}

/* key numbers */
#chart-stats{display:flex;align-items:stretch;border-top:1px solid var(--border,#1c2430);border-bottom:1px solid var(--border,#1c2430);
  background:var(--bg-soft,#0b0f16);min-height:66px}
.ch-stats-main{display:flex;flex:1;flex-wrap:wrap;min-width:0}
.ch-stat{padding:10px 18px 9px;border-right:1px solid var(--border,#1c2430);min-width:104px}
.ch-stat .k{font-size:10.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--text-dim,#5c6673);
  display:flex;align-items:center;gap:6px;white-space:nowrap}
.ch-stat .k i{display:inline-block;width:10px;height:3px;border-radius:2px;background:currentColor}
.ch-stat .v{font-family:var(--mono,monospace);font-size:19px;font-weight:700;color:var(--text,#e6edf3);margin-top:3px;
  font-variant-numeric:tabular-nums;white-space:nowrap;line-height:1.2}
.ch-stat .s{font-size:11.5px;color:var(--text-muted,#8b949e);margin-top:2px;white-space:nowrap}
.ch-stat.k-entry .k{color:#79b8ff}.ch-stat.k-entry .v{color:#a5cdff}
.ch-stat.k-stop .k{color:#ff7b72}.ch-stat.k-stop .v{color:#ffa198}
.ch-stat.k-target .k{color:#56d364}.ch-stat.k-target .v{color:#7ee787}
.ch-result{padding:9px 20px 8px;text-align:right;min-width:180px;border-left:1px solid var(--border,#1c2430);flex:0 0 auto}
.ch-result .k{font-size:10.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--text-dim,#5c6673)}
.ch-result .v{font-family:var(--mono,monospace);font-size:28px;font-weight:800;line-height:1.1;font-variant-numeric:tabular-nums;color:var(--text,#e6edf3)}
.ch-result .s{font-size:12px;color:var(--text-muted,#8b949e);margin-top:1px}
.ch-result.pos .v{color:#56d364}.ch-result.neg .v{color:#ff7b72}.ch-result.none .v{color:var(--text-dim,#5c6673)}

/* toolbar */
#chart-toolbar{display:flex;align-items:center;gap:6px;padding:8px 12px;border-bottom:1px solid var(--border,#1c2430);flex-wrap:wrap}
.ch-seg{display:inline-flex;border:1px solid var(--border-strong,#2a3340);border-radius:7px;overflow:hidden}
.ch-seg button{border:0;background:transparent;color:var(--text-muted,#8b949e);height:28px;padding:0 11px;font-size:12.5px;font-weight:600;cursor:pointer}
.ch-seg button+button{border-left:1px solid var(--border-strong,#2a3340)}
.ch-seg button:hover{color:var(--text,#e6edf3)}
.ch-seg button.on{background:var(--accent-dim,#152238);color:var(--accent-strong,#79b8ff)}
.ch-chip{height:30px;padding:0 10px;border-radius:7px;border:1px solid var(--border-strong,#2a3340);background:transparent;
  color:var(--text-dim,#5c6673);font-size:12.5px;font-weight:600;display:inline-flex;align-items:center;gap:7px;cursor:pointer;white-space:nowrap}
.ch-chip:hover{color:var(--text,#e6edf3)}
.ch-chip.on{color:var(--text,#e6edf3);background:var(--surface-2,#151b25);border-color:#3a4556}
.ch-chip .sw{width:12px;height:3px;border-radius:2px;background:var(--text-dim,#5c6673)}
.ch-chip.on .sw{background:var(--sw)}
.ch-chip.warn.on{background:var(--amber-dim,#251d0e);border-color:rgba(210,153,34,.5);color:#e3b341}
.ch-chip[hidden]{display:none}
.ch-sep{width:1px;height:20px;background:var(--border-strong,#2a3340);margin:0 4px}
.ch-count{font-size:10.5px;background:var(--accent-dim,#152238);color:var(--accent-strong,#79b8ff);border-radius:9px;padding:1px 6px}

/* body */
#chart-body{flex:1;display:flex;min-height:0}
#chart-rail{width:44px;flex:0 0 44px;border-right:1px solid var(--border,#1c2430);display:flex;flex-direction:column;
  align-items:center;padding:8px 0;gap:2px;background:var(--bg-soft,#0b0f16);overflow-y:auto}
#chart-rail button{width:32px;height:32px;border-radius:7px;border:1px solid transparent;background:transparent;
  color:var(--text-muted,#8b949e);cursor:pointer;display:flex;align-items:center;justify-content:center;flex:0 0 auto}
#chart-rail button svg{width:17px;height:17px;fill:none;stroke:currentColor;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
#chart-rail button:hover{background:var(--surface-2,#151b25);color:var(--text,#e6edf3)}
#chart-rail button.on{background:var(--accent-dim,#152238);color:var(--accent-strong,#79b8ff);border-color:rgba(77,159,255,.35)}
#chart-rail .sep{width:22px;height:1px;background:var(--border-strong,#2a3340);margin:6px 0;flex:0 0 auto}
@media (max-width:700px){#chart-rail{display:none}}
#chart-canvas-wrap{flex:1;min-width:0;position:relative}
#chart-canvas{position:absolute;inset:0}
#chart-status{position:absolute;inset:0;display:none;align-items:center;justify-content:center;pointer-events:none;z-index:5}
#chart-status.show{display:flex}
#chart-status .box{background:rgba(16,21,29,.92);border:1px solid var(--border-strong,#2a3340);padding:10px 16px;border-radius:8px;
  color:var(--text-muted,#8b949e);font-size:13px;max-width:70%;text-align:center}
#chart-status.err .box{color:#ff7b72;border-color:rgba(248,81,73,.4);pointer-events:auto}
#chart-retry{margin-left:10px;height:26px;padding:0 10px;border-radius:6px;border:1px solid rgba(248,81,73,.5);background:transparent;color:#ff7b72;cursor:pointer;font-size:12px}
#chart-hint{position:absolute;left:50%;top:10px;transform:translateX(-50%);z-index:6;display:none;pointer-events:none;
  background:var(--accent-dim,#152238);color:var(--accent-strong,#79b8ff);border:1px solid rgba(77,159,255,.35);
  font-size:12px;padding:5px 12px;border-radius:14px;white-space:nowrap}
#chart-hint.show{display:block}

/* details panel */
#chart-ctx{width:310px;flex:0 0 310px;overflow:auto;border-left:1px solid var(--border,#1c2430);padding:14px 16px 24px;font-size:13px;
  color:var(--text,#e6edf3)}
#chart-ctx.collapsed{display:none}
@media (max-width:1100px){#chart-ctx{position:absolute;right:0;top:0;bottom:0;z-index:7;background:var(--surface,#10151d);box-shadow:-12px 0 24px -12px rgba(0,0,0,.8)}}
#chart-ctx h3{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text-dim,#5c6673);margin:18px 0 6px}
#chart-ctx h3:first-child{margin-top:2px}
#chart-ctx .row{display:flex;justify-content:space-between;gap:12px;padding:5px 0;border-bottom:1px solid var(--border,#1c2430)}
#chart-ctx .row span:first-child{color:var(--text-muted,#8b949e)}
#chart-ctx .row span:last-child{text-align:right;font-family:var(--mono,monospace);font-size:12.5px}
#chart-ctx .txt{color:var(--text,#e6edf3);line-height:1.5;margin:0}
#chart-ctx .callout{border-left:3px solid #e3b341;background:var(--amber-dim,#251d0e);padding:9px 11px;border-radius:0 7px 7px 0}
#chart-ctx .callout .h{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#e3b341;margin-bottom:3px}
#chart-ctx .warn-item{color:#e3b341;background:var(--amber-dim,#251d0e);border:1px solid rgba(210,153,34,.3);border-radius:6px;padding:6px 8px;font-size:12px;margin-top:6px}
#chart-ctx .guard-item{border-radius:6px;padding:6px 8px;font-size:12px;margin-top:6px;border:1px solid var(--border,#1c2430)}
#chart-ctx .guard-item.bad{color:#ff7b72;background:var(--red-dim,#271317);border-color:rgba(248,81,73,.3)}
#chart-ctx .guard-item.ok{color:var(--text-muted,#8b949e)}
#chart-ctx .basis{display:flex;gap:8px;align-items:baseline;font-size:12px;color:var(--text-muted,#8b949e);padding:3px 0}
#chart-ctx .basis i{flex:0 0 12px;height:3px;border-radius:2px;transform:translateY(-3px)}

/* menus + position tabs */
#chart-menu{position:fixed;background:var(--surface-2,#151b25);border:1px solid var(--border-strong,#2a3340);border-radius:9px;padding:6px;
  font-size:12.5px;z-index:210;min-width:230px;display:none;box-shadow:0 16px 40px -12px rgba(0,0,0,.8);color:var(--text,#e6edf3)}
#chart-menu.show{display:block}
#chart-menu label{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:6px;cursor:pointer;white-space:nowrap}
#chart-menu label:hover{background:var(--surface-3,#1a212c)}
#chart-menu input{accent-color:#4d9fff}
#chart-menu .hint{color:var(--text-dim,#5c6673);font-size:11px;padding:4px 8px 6px;white-space:normal;max-width:230px}
#chart-tabs{display:none;gap:6px;padding:8px 12px;border-bottom:1px solid var(--border,#1c2430)}
/* phones: last so it wins over the base rules above */
@media (max-width:700px){
  #chart-head{padding:10px 12px 8px;gap:8px}
  #chart-lane{max-width:100%}
  #chart-stats{flex-direction:column;min-height:0}
  .ch-stats-main{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))}
  .ch-stat{padding:6px 10px;min-width:0;border-bottom:1px solid var(--border,#1c2430)}
  .ch-stat .v{font-size:15px}
  .ch-stat .s{font-size:10.5px;white-space:normal}
  .ch-result{display:flex;align-items:baseline;gap:10px;text-align:left;border-left:0;padding:6px 12px;min-width:0}
  .ch-result .v{font-size:20px;order:-1}
  #chart-toolbar{flex-wrap:nowrap;overflow-x:auto;padding:6px 10px}
  #chart-toolbar>*{flex-shrink:0}
  .ch-chip{height:28px;padding:0 8px}
}
</style>
<script>
(function(){
'use strict';
var KLC_SRC = 'https://cdn.jsdelivr.net/npm/klinecharts@10.0.3/dist/umd/klinecharts.min.js';
var KLC_SRI = 'sha384-mDk2ZCby0dAY121ON2d27q0Kpll0mCiLGX9CuJegyfrn2uliCdpwfH7ARBKZFzeA';
var PANE = 'candle_pane', VOL_PANE = 'ta_vol_pane';
var MONO = '"SF Mono","JetBrains Mono",ui-monospace,Consolas,"Roboto Mono",monospace';
var SANS = '-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,"Helvetica Neue",Arial,sans-serif';
var C = {up:'#26a69a', down:'#ef5350', text:'#e6edf3', muted:'#8b949e', dim:'#5c6673',
         grid:'rgba(139,148,158,.075)', axis:'#232c38', ink:'#06090f', panelA:'rgba(11,15,22,.92)',
         entry:'#4d9fff', stop:'#f85149', target:'#3fb950', structural:'#8b949e',
         vwap:'#f0b429', ema9:'#c297ff', noise:'rgba(248,81,73,.07)', amber:'#e3b341', purple:'#a371f7',
         draw:'#79b8ff'};
// Server series the drawer can show. VWAP/EMA values come from /api/chart only (R-0).
var SERIES = [{key:'vwap', label:'VWAP', color:C.vwap}, {key:'ema9', label:'EMA 9', color:C.ema9}];
var STUDIES = [
  {name:'MA', label:'Moving averages', overlay:true}, {name:'BOLL', label:'Bollinger bands', overlay:true},
  {name:'MACD', label:'MACD'}, {name:'RSI', label:'RSI'}, {name:'KDJ', label:'KDJ'}, {name:'OBV', label:'OBV'}];
var LEVEL_STYLE = {
  entry: {color:C.entry, strong:true, size:1.5}, stop: {color:C.stop, strong:true, size:1.5},
  target: {color:C.target, strong:true, size:1.5}, target2: {color:C.target, dashed:true, size:1},
  structural: {color:C.structural, dashed:true, size:1}};
var KEY_LEVELS = {entry:1, stop:1, target:1};
var PREFS_KEY = 'ta_chart_prefs';
var DEFAULT_LAYERS = {vwap:true, ema9:true, levels:true, fills:true, events:true};
var ICON = {
  segment:'<path d="M3 13L13 3"/><circle cx="3" cy="13" r="1.6"/><circle cx="13" cy="3" r="1.6"/>',
  rayLine:'<path d="M3.5 12.5L15 1"/><circle cx="3" cy="13" r="1.6"/>',
  horizontalStraightLine:'<path d="M1 8h14"/>',
  horizontalRayLine:'<path d="M4.6 8H15"/><circle cx="3" cy="8" r="1.6"/>',
  verticalStraightLine:'<path d="M8 1v14"/>',
  parallelStraightLine:'<path d="M1.5 10.5L10.5 1.5M5.5 14.5l9-9"/>',
  fibonacciLine:'<path d="M1.5 2.5h13M1.5 6h13M1.5 8.5h13M1.5 13.5h13"/>',
  priceLine:'<path d="M1 9.5h8"/><rect x="9.5" y="6.5" width="5.5" height="6" rx="1.2"/>',
  simpleAnnotation:'<path d="M2.5 2.5h11v8h-5l-3 3v-3h-3z"/>',
  brush:'<path d="M1.5 13c2.5-.5 3-4.5 5.2-4.5 2 0 1 3.4 3.3 3.4 2.3 0 3.3-4.9 4.5-9.4"/>',
  undo:'<path d="M5 5.5h5.5a3.5 3.5 0 010 7H6"/><path d="M7.5 3L5 5.5 7.5 8"/>',
  trash:'<path d="M2.5 4.5h11M6.5 4.5V2.5h3v2M4 4.5l.8 9.5h6.4l.8-9.5"/>'};
var DRAW_TOOLS = [
  ['segment', 'Trend line'], ['rayLine', 'Ray'], ['horizontalStraightLine', 'Horizontal line'],
  ['horizontalRayLine', 'Horizontal ray'], ['verticalStraightLine', 'Vertical line'],
  ['parallelStraightLine', 'Parallel channel'], ['fibonacciLine', 'Fib retracement'],
  ['priceLine', 'Price label'], ['simpleAnnotation', 'Note'], ['brush', 'Brush']];

var built = false, libPromise = null, registered = false;
var chart = null, klcRef = null;
var prefs = loadPrefs();
var state = {ticker:null, date:null, lane:null, id:null,
             tf: [1, 5, 15].indexOf(prefs.tf) >= 0 ? prefs.tf : 5, asof:false,
             tz: prefs.tz || legacyTz() || 'America/Chicago',
             layers: Object.assign({}, DEFAULT_LAYERS, prefs.layers || {}),
             details: prefs.details != null ? !!prefs.details : window.innerWidth > 1180,
             payload:null, series:{}, legend:{}, loading:false, pollTimer:null};
var openedViaNav = false;
var loadSeq = 0;
var subscribedBar = null;          // v10 subscribeBar callback, fed by live polling
var studyPanes = {};               // study name -> paneId it was created in
var drawingId = null, selectedId = null, drawnKey = null;

function $(id){ return document.getElementById(id); }
function esc(t){ return String(t==null?'':t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function num(v){ return v == null || v === '' || isNaN(Number(v)) ? null : Number(v); }
function px(v){ v = num(v); return v == null ? '—' : v.toFixed(2); }
function fmtR(v){ v = num(v); return v == null ? '—' : (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v).toFixed(2) + 'R'; }
function bigNum(v){
  v = Number(v); if(!isFinite(v)) return '—';
  var a = Math.abs(v);
  return a >= 1e9 ? (v / 1e9).toFixed(2) + 'B' : a >= 1e6 ? (v / 1e6).toFixed(2) + 'M' : a >= 1e3 ? (v / 1e3).toFixed(1).replace(/\.0$/, '') + 'K' : String(Math.round(v));
}
function tzShort(){ return state.tz === 'America/New_York' ? 'ET' : 'CT'; }
function clock(ts){
  if(ts == null) return '';
  try { return new Intl.DateTimeFormat('en-US', {timeZone: state.tz, hour:'2-digit', minute:'2-digit', hourCycle:'h23'})
                   .format(new Date(ts)) + ' ' + tzShort(); } catch(e){ return ''; }
}
function loadPrefs(){ try { return JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') || {}; } catch(e){ return {}; } }
function legacyTz(){ try { return localStorage.getItem('ta_chart_tz'); } catch(e){ return null; } }
function savePrefs(){
  try { localStorage.setItem(PREFS_KEY, JSON.stringify({tf: state.tf, tz: state.tz, layers: state.layers, details: state.details})); } catch(e){}
}

function loadLib(){
  if(window.klinecharts) return Promise.resolve(window.klinecharts);
  if(libPromise) return libPromise;
  libPromise = new Promise(function(resolve, reject){
    var s = document.createElement('script');
    s.src = KLC_SRC; s.integrity = KLC_SRI; s.crossOrigin = 'anonymous';
    s.onload = function(){ window.klinecharts ? resolve(window.klinecharts) : reject(new Error('klinecharts did not attach to window')); };
    s.onerror = function(){ libPromise = null; reject(new Error('network/CSP blocked the chart library')); };
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
    '<div id="chart-panel" role="dialog" aria-modal="true" aria-labelledby="chart-title">' +
      '<div id="chart-guard-banner"></div>' +
      '<div id="chart-head">' +
        '<span class="ch-ticker" id="chart-title">—</span>' +
        '<span id="chart-badges"></span>' +
        '<span class="ch-date" id="chart-sub"></span>' +
        '<select id="chart-lane" title="Which trade on this ticker/date to chart"></select>' +
        '<span class="ch-badge warn" id="chart-stale" hidden>Delayed data</span>' +
        '<span class="ch-spacer"></span>' +
        '<button id="chart-close" title="Close (Esc)" aria-label="Close chart">✕</button>' +
      '</div>' +
      '<div id="chart-stats"></div>' +
      '<div id="chart-toolbar">' +
        '<div class="ch-seg" id="chart-tf" role="group" aria-label="Timeframe">' +
          '<button data-tf="1">1m</button><button data-tf="5">5m</button><button data-tf="15">15m</button></div>' +
        '<span class="ch-sep"></span>' +
        SERIES.map(function(s){
          return '<button class="ch-chip" data-layer="' + s.key + '" style="--sw:' + s.color + '" title="Show ' + s.label +
                 ' (server-computed)"><span class="sw"></span>' + s.label + '</button>';
        }).join('') +
        '<button class="ch-chip" data-layer="levels" style="--sw:' + C.entry + '" title="Entry / stop / target and structural levels"><span class="sw"></span>Levels</button>' +
        '<button class="ch-chip" data-layer="fills" style="--sw:' + C.target + '" title="Fill and exit markers"><span class="sw"></span>Fills</button>' +
        '<button class="ch-chip" data-layer="events" style="--sw:' + C.amber + '" title="Time stop, detection, as-of cutoff"><span class="sw"></span>Events</button>' +
        '<button class="ch-chip" id="chart-studies-btn" title="Exploratory indicators (not ledger features)">Studies <span id="chart-studies-n"></span> ▾</button>' +
        '<span class="ch-spacer"></span>' +
        '<button class="ch-chip warn" id="chart-asof-toggle" title="As-of mode: hide everything after the plan/selection cutoff">As-of</button>' +
        '<button class="ch-chip on" id="chart-tz-toggle" title="Toggle Central / Eastern time"></button>' +
        '<button class="ch-chip on" id="chart-fit" title="Fit the whole session (double-click chart also works)">Fit</button>' +
        '<button class="ch-chip" id="chart-ctx-toggle" title="Show / hide the plan details panel">Details</button>' +
      '</div>' +
      '<div id="chart-tabs"></div>' +
      '<div id="chart-body">' +
        '<div id="chart-rail"></div>' +
        '<div id="chart-canvas-wrap"><div id="chart-canvas"></div>' +
          '<div id="chart-status"><div class="box" id="chart-status-text"></div></div>' +
          '<div id="chart-hint"></div></div>' +
        '<div id="chart-ctx"></div>' +
      '</div>' +
    '</div>' +
    '<div id="chart-menu"></div>';
  document.body.appendChild(wrap);

  $('chart-backdrop').addEventListener('click', requestClose);
  $('chart-close').addEventListener('click', requestClose);
  $('chart-tf').addEventListener('click', function(e){
    var b = e.target.closest('button[data-tf]'); if(!b) return;
    var tf = Number(b.dataset.tf); if(tf === state.tf) return;
    state.tf = tf; savePrefs(); renderToolbar(); loadData();
  });
  $('chart-toolbar').addEventListener('click', function(e){
    var b = e.target.closest('button[data-layer]'); if(!b) return;
    toggleLayer(b.dataset.layer);
  });
  $('chart-lane').addEventListener('change', function(e){
    var ctx = ((state.payload && state.payload.contexts) || []).find(function(c){ return String(c.lane)+':'+String(c.id) === e.target.value; });
    if(ctx){ state.lane = ctx.lane; state.id = ctx.id; syncHash(); loadData(); }
  });
  $('chart-asof-toggle').addEventListener('click', function(){ state.asof = !state.asof; renderToolbar(); loadData(); });
  $('chart-tz-toggle').addEventListener('click', function(){
    state.tz = state.tz === 'America/Chicago' ? 'America/New_York' : 'America/Chicago';
    savePrefs(); renderToolbar();
    if(chart) try { chart.setTimezone(state.tz); } catch(e){}
    renderStats(); renderCtx();
  });
  $('chart-fit').addEventListener('click', fitView);
  $('chart-ctx-toggle').addEventListener('click', function(){ state.details = !state.details; savePrefs(); renderToolbar(); });
  $('chart-studies-btn').addEventListener('click', function(e){ e.stopPropagation(); toggleStudiesMenu(e.currentTarget); });
  $('chart-canvas').addEventListener('dblclick', function(e){
    if(!drawingId && !selectedId) fitView();
  });
  document.addEventListener('keydown', onKey);
  document.addEventListener('click', function(e){
    var menu = $('chart-menu');
    if(menu.classList.contains('show') && !menu.contains(e.target)) menu.classList.remove('show');
  });
  window.addEventListener('popstate', function(){
    if(isOpen() && !location.hash.startsWith('#/chart/')) closeChart();
  });
  document.addEventListener('visibilitychange', function(){
    if(document.hidden) stopPolling(); else if(isLiveOpenPosition()) startPolling();
  });
  if(window.ResizeObserver){
    new ResizeObserver(function(){ if(chart && isOpen()) chart.resize(); }).observe($('chart-canvas'));
  } else {
    window.addEventListener('resize', function(){ if(chart && isOpen()) chart.resize(); });
  }
  buildDrawRail();
  renderToolbar();
}

function isOpen(){ return built && $('chart-drawer').classList.contains('open'); }

function onKey(e){
  if(!isOpen()) return;
  var typing = /^(INPUT|SELECT|TEXTAREA)$/.test((e.target && e.target.tagName) || '');
  if(e.key === 'Escape'){
    var menu = $('chart-menu');
    if(menu.classList.contains('show')) menu.classList.remove('show');
    else if(drawingId) cancelDrawing();
    else requestClose();
    return;
  }
  if((e.key === 'Delete' || e.key === 'Backspace') && selectedId && !typing && chart){
    try { chart.removeOverlay({id: selectedId}); } catch(err){}
    selectedId = null; persistDrawings(); e.preventDefault();
  }
}

function requestClose(){
  if(openedViaNav) history.back();
  else location.hash = '#/' + (window.TA.lastPage ? window.TA.lastPage() : 'overview');
}

function syncHash(){
  // keep the deep link pointing at what is on screen, without a router round-trip
  var qs = [];
  if(state.lane) qs.push('lane=' + encodeURIComponent(state.lane));
  if(state.id != null) qs.push('id=' + encodeURIComponent(state.id));
  var h = '#/chart/' + state.ticker + '/' + encodeURIComponent(state.date || '') + (qs.length ? '?' + qs.join('&') : '');
  try { history.replaceState(history.state, '', h); } catch(e){}
}

// --------------------------------------------------------------- open/close

function openChart(opts){
  buildDom();
  var ticker = String(opts.ticker || '').toUpperCase();
  if(state.payload && state.payload.ticker !== ticker){ state.payload = null; }
  state.ticker = ticker;
  state.date = opts.date;
  state.lane = opts.lane || null;
  state.id = opts.id != null ? opts.id : null;
  openedViaNav = !!opts.viaNav;
  $('chart-drawer').classList.add('open');
  document.documentElement.style.overflow = 'hidden';
  renderHeader(); renderStats(); renderCtx();
  setStatus('Loading ' + ticker + '…');
  loadLib().then(function(klc){
    var fresh = !chart;
    ensureChart(klc);
    if(!fresh) loadData();   // a fresh chart already issued its first load in ensureChart
  }).catch(function(e){
    setStatus('Chart library failed to load (' + e.message + '). The rest of the page still works.', true);
  });
}

function closeChart(){
  if(!built) return;
  $('chart-drawer').classList.remove('open');
  $('chart-menu').classList.remove('show');
  document.documentElement.style.overflow = '';
  cancelDrawing();
  stopPolling();
}

function setStatus(msg, isErr){
  var el = $('chart-status'); if(!el) return;
  $('chart-status-text').innerHTML = esc(msg || '') + (isErr && chart ? ' <button id="chart-retry">Retry</button>' : '');
  var retry = $('chart-retry'); if(retry) retry.addEventListener('click', loadData);
  el.classList.toggle('show', !!msg);
  el.classList.toggle('err', !!isErr);
}

// ----------------------------------------------------------------- chart

function chartStyles(){
  var text = function(o){ return Object.assign({family: MONO, size: 11.5}, o); };
  return {
    grid: {show: true, horizontal: {show: true, size: 1, color: C.grid, style: 'solid'}, vertical: {show: false}},
    candle: {
      type: 'candle_solid',
      bar: {compareRule: 'current_open', upColor: C.up, downColor: C.down, noChangeColor: C.muted,
            upBorderColor: C.up, downBorderColor: C.down, noChangeBorderColor: C.muted,
            upWickColor: C.up, downWickColor: C.down, noChangeWickColor: C.muted},
      priceMark: {
        show: true,
        high: {show: true, color: C.muted, textSize: 11, textFamily: MONO},
        low: {show: true, color: C.muted, textSize: 11, textFamily: MONO},
        last: {show: true, upColor: C.up, downColor: C.down, noChangeColor: C.muted,
               line: {show: true, style: 'dashed', dashedValue: [2, 3], size: 1},
               text: text({show: true, color: '#ffffff', weight: 700, size: 12, paddingLeft: 5, paddingRight: 5,
                           paddingTop: 3, paddingBottom: 3, borderRadius: 3})}},
      tooltip: {
        showRule: 'always', showType: 'standard', offsetLeft: 10, offsetTop: 8, offsetRight: 10,
        title: {show: false},
        legend: {size: 12.5, family: MONO, color: C.muted, marginLeft: 0, marginTop: 2, marginRight: 12, marginBottom: 4,
                 defaultValue: '—', template: candleLegend}}},
    indicator: {
      ohlc: {upColor: 'rgba(38,166,154,.7)', downColor: 'rgba(239,83,80,.7)', noChangeColor: C.muted},
      bars: [{style: 'fill', borderSize: 0, upColor: 'rgba(38,166,154,.45)', downColor: 'rgba(239,83,80,.45)', noChangeColor: 'rgba(139,148,158,.4)'}],
      // study colours kept clear of VWAP amber / EMA purple
      lines: ['#6ec1ff', '#ff7eb6', '#7ee787', '#c9d1d9', '#ffa657'].map(function(c){
        return {style: 'solid', smooth: false, size: 1.2, dashedValue: [2, 2], color: c}; }),
      lastValueMark: {show: false},
      tooltip: {
        showRule: 'always', showType: 'standard', offsetLeft: 10, offsetTop: 4, offsetRight: 10,
        title: {show: true, showName: true, showParams: true, size: 12, family: MONO, color: C.dim, marginLeft: 0, marginRight: 8},
        legend: {size: 12, family: MONO, color: C.muted, marginLeft: 0, marginRight: 12, defaultValue: '—'}}},
    xAxis: {axisLine: {show: true, color: C.axis}, tickLine: {show: false}, tickText: text({color: C.muted})},
    yAxis: {axisLine: {show: true, color: C.axis}, tickLine: {show: false}, tickText: text({color: C.muted})},
    separator: {size: 1, color: C.axis, fill: true, activeBackgroundColor: 'rgba(77,159,255,.12)'},
    crosshair: {
      horizontal: {line: {style: 'dashed', dashedValue: [3, 3], color: '#5c6673', size: 1},
                   text: text({color: C.text, backgroundColor: '#2a3340', borderColor: '#2a3340', borderRadius: 3})},
      vertical: {line: {style: 'dashed', dashedValue: [3, 3], color: '#5c6673', size: 1},
                 text: text({color: C.text, backgroundColor: '#2a3340', borderColor: '#2a3340', borderRadius: 3})}},
    overlay: {
      point: {color: C.draw, borderColor: 'rgba(121,184,255,.35)', borderSize: 1, radius: 4,
              activeColor: C.draw, activeBorderColor: 'rgba(121,184,255,.35)', activeBorderSize: 3, activeRadius: 5},
      line: {color: C.draw, size: 1.5, style: 'solid'},
      rect: {color: 'rgba(121,184,255,.12)', borderColor: C.draw, borderSize: 1},
      polygon: {color: 'rgba(121,184,255,.12)', borderColor: C.draw},
      circle: {color: 'rgba(121,184,255,.12)', borderColor: C.draw},
      arc: {color: C.draw},
      text: {color: C.draw, size: 11.5, family: MONO, backgroundColor: 'transparent', borderSize: 0,
             paddingLeft: 2, paddingRight: 2, paddingTop: 2, paddingBottom: 2}}
  };
}

function ensureChart(klc){
  if(chart) return;
  klcRef = klc;
  registerAll(klc);
  chart = klc.init($('chart-canvas'), {
    locale: 'en-US', timezone: state.tz, styles: chartStyles(),
    formatter: {formatDate: function(p){ return klc.utils.formatDate(p.dateTimeFormat, p.timestamp, 'HH:mm'); },
                formatBigNumber: bigNum},
  });
  // Volume, without the default 5/10/20 MAs that cluttered the pane.
  chart.createIndicator({name: 'VOL', paneId: VOL_PANE, calcParams: [],
    createTooltipDataSource: function(){ return {name: 'Volume', calcParamsText: '', features: [], legends: []}; }}, false);
  try { chart.setPaneOptions({id: VOL_PANE, height: 84, minHeight: 48}); } catch(e){}
  SERIES.forEach(function(s){ if(state.layers[s.key]) setSeriesIndicator(s.key, true); });
  // Keep entry/stop/target on screen: widen the candle pane's auto range to include them.
  chart.overrideYAxis({paneId: PANE, createRange: candleRange});
  // Round-clock time labels (09:00, 10:00 ...) instead of every-N-bars.
  chart.overrideXAxis({createTicks: timeTicks});
  chart.setOffsetRightDistance(36);
  chart.setSymbol({ticker: state.ticker, pricePrecision: 2, volumePrecision: 0});
  chart.setPeriod({span: state.tf, type: 'minute'});
  // Setting the loader last issues exactly one initial load.
  chart.setDataLoader({
    getBars: getBars,
    subscribeBar: function(p){ subscribedBar = p.callback; },
    unsubscribeBar: function(){ subscribedBar = null; stopPolling(); },
  });
}

// Any change of ticker/date/lane/tf/as-of comes through here: exactly one
// reload, and a stale response can never overwrite a newer one (loadSeq).
function loadData(){
  if(!chart) return;
  clearStrategyOverlays();
  setStatus('Loading ' + state.ticker + '…');
  var sym = chart.getSymbol(), per = chart.getPeriod();
  var symChanged = !sym || sym.ticker !== state.ticker;
  var perChanged = !per || per.span !== state.tf;
  if(symChanged){ clearUserDrawings(); chart.setSymbol({ticker: state.ticker, pricePrecision: 2, volumePrecision: 0}); }
  if(perChanged) chart.setPeriod({span: state.tf, type: 'minute'});
  if(!symChanged && !perChanged) chart.resetData();
}

function fetchPayload(){
  var qs = new URLSearchParams({ticker: state.ticker, date: state.date || '', tf: String(state.tf),
                                asof: state.asof ? '1' : '0'});
  if(state.lane) qs.set('lane', state.lane);
  if(state.id != null) qs.set('id', String(state.id));
  return fetch('/api/chart?' + qs.toString(), {credentials: 'same-origin'}).then(function(r){
    if(!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  });
}

function toKLine(b){ return {timestamp: b.timestamp, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume}; }

function getBars(params){
  if(params.type !== 'init'){ params.callback([], false); return; }
  var seq = ++loadSeq;
  state.loading = true;
  fetchPayload().then(function(payload){
    if(seq !== loadSeq) return;     // superseded -- the newer load owns the chart
    state.loading = false;
    state.payload = payload;
    state.lane = payload.lane; state.id = payload.id;
    prepareSeries(payload);          // before the callback: indicator calc reads it
    var bars = (payload.bars || []).map(toKLine);
    params.callback(bars, false);
    setStatus(bars.length ? '' : 'No bars for ' + state.ticker + ' on ' + state.date + '.');
    onPayload(payload);
    fitView();
    restoreDrawings();
  }).catch(function(e){
    if(seq !== loadSeq) return;
    state.loading = false;
    // never leave the previous trade's numbers up next to an empty chart
    state.payload = null;
    clearStrategyOverlays();
    params.callback([], false);
    renderHeader(); renderStats(); renderCtx(); stopPolling();
    setStatus('Couldn’t load chart data (' + e.message + ').', true);
  });
}

function onPayload(payload){
  renderHeader();
  renderStats();
  renderToolbar();
  renderStrategyOverlays();
  renderCtx();
  renderGuardBanner(payload);
  renderPositionTabs(payload);
  manageLiveMode();
}

// Fit the session to the width. A partial session (as-of cut, or live
// mid-day) keeps the full day's time scale with the future left blank, so
// bars don't balloon or huddle against the axis.
function fitView(){
  if(!chart) return;
  var n = chart.getDataList().length; if(!n) return;
  var size = chart.getSize(PANE, 'main');
  var w = ((size && size.width) || $('chart-canvas').clientWidth - 70) - 40;
  var tf = (state.payload && state.payload.tf) || state.tf;
  var slots = Math.max(n, Math.ceil(390 / tf));
  var bs = Math.max(1.5, Math.min(24, w / (slots + 1)));
  chart.setBarSpace(bs);
  chart.setOffsetRightDistance(Math.max(36, w - n * bs));
  chart.scrollToRealTime();
}

// ------------------------------------------------------------ server series
// Values are the server's (R-0). A display bar shows the latest server value
// whose own bar had CLOSED by the display bar's close -- so a 5m EMA steps on
// a 1m chart instead of leaking the 5m close early, and a 1m VWAP on a 5m
// chart shows its value at the bucket's close, not its open.

function prepareSeries(payload){
  state.series = {};
  var src = payload.series || {};
  SERIES.forEach(function(def){
    var s = src[def.key]; if(!s || !s.points) return;
    var ts = Object.keys(s.points).map(Number).filter(isFinite).sort(function(a, b){ return a - b; });
    if(!ts.length) return;
    state.series[def.key] = {ts: ts, v: ts.map(function(t){ return num(s.points[t]); }),
                             bm: num(s.bar_minutes) || payload.tf || state.tf};
  });
}

function seriesLabel(def){
  var s = state.series[def.key], tf = (state.payload && state.payload.tf) || state.tf;
  return s && s.bm > tf ? def.label + ' (' + s.bm + 'm)' : def.label;
}

function seriesValues(key, dataList){
  var s = state.series[key], out = new Array(dataList.length);
  var tf = (state.payload && state.payload.tf) || state.tf;
  var j = -1, lag = s ? (tf - s.bm) * 60000 : 0;
  for(var i = 0; i < dataList.length; i++){
    if(!s){ out[i] = null; continue; }
    var cut = dataList[i].timestamp + lag;
    while(j + 1 < s.ts.length && s.ts[j + 1] <= cut) j++;
    out[i] = j >= 0 ? s.v[j] : null;
  }
  return out;
}

function setSeriesIndicator(key, on){
  if(!chart) return;
  var name = 'TA_' + key.toUpperCase();
  try {
    if(on){ if(!chart.getIndicators({paneId: PANE, name: name}).length) chart.createIndicator({name: name, paneId: PANE}, true); }
    else chart.removeIndicator({paneId: PANE, name: name});
  } catch(e){}
}

function idxOf(ts){
  var list = chart ? chart.getDataList() : [], lo = 0, hi = list.length - 1;
  while(lo <= hi){ var m = (lo + hi) >> 1, t = list[m].timestamp; if(t === ts) return m; if(t < ts) lo = m + 1; else hi = m - 1; }
  return -1;
}

// One legend row: time, OHLC (coloured by the bar), change, volume, then the
// strategy series values in their line colours.
function candleLegend(data){
  var cur = data.current; if(!cur) return [];
  var col = cur.close >= cur.open ? C.up : C.down;
  var L = function(t, v, c, tc){ return {title: {text: t, color: tc || C.dim}, value: {text: v, color: c}}; };
  var prev = data.prev ? data.prev.close : cur.open, chg = prev ? (cur.close - prev) / prev * 100 : 0;
  var out = [L('', '{time}', C.text), L('O ', '{open}', col), L('H ', '{high}', col), L('L ', '{low}', col),
             L('C ', '{close}', col), L('', (chg > 0 ? '+' : chg < 0 ? '−' : '') + Math.abs(chg).toFixed(2) + '%', chg >= 0 ? C.up : C.down),
             L('Vol ', '{volume}', C.muted)];
  var i = idxOf(cur.timestamp);
  SERIES.forEach(function(s){
    if(!state.layers[s.key]) return;
    var vals = state.legend[s.key], v = vals && i >= 0 ? vals[i] : null;
    if(v != null) out.push(L(seriesLabel(s) + ' ', v.toFixed(2), s.color, s.color));
  });
  return out;
}

// --------------------------------------------------------------- axes

function candleRange(p){
  var r = p.defaultRange, lo = r.from, hi = r.to, span = hi - lo;
  if(state.layers.levels && state.payload && span > 0){
    (state.payload.levels || []).forEach(function(lv){
      var v = num(lv.price);
      if(!KEY_LEVELS[lv.kind] || v == null) return;
      // pull a key level into view if it is within one more day-range; further
      // out it gets an edge-pinned label instead of crushing the candles
      if(v < lo && v >= r.from - span) lo = v;
      if(v > hi && v <= r.to + span) hi = v;
    });
  }
  var d = hi - lo;
  return {from: lo, to: hi, range: d, realFrom: lo, realTo: hi, realRange: d, displayFrom: lo, displayTo: hi, displayRange: d};
}

var tzCache = {key: null, off: 0};
function tzOffsetMin(ts){
  var key = state.tz + ':' + Math.floor(ts / 86400000);
  if(tzCache.key === key) return tzCache.off;
  var off = 0;
  try {
    var parts = new Intl.DateTimeFormat('en-US', {timeZone: state.tz, hourCycle: 'h23', year: 'numeric', month: '2-digit',
                                                  day: '2-digit', hour: '2-digit', minute: '2-digit'}).formatToParts(new Date(ts));
    var g = function(t){ return Number((parts.find(function(x){ return x.type === t; }) || {}).value); };
    off = Math.round((Date.UTC(g('year'), g('month') - 1, g('day'), g('hour') % 24, g('minute')) - Math.floor(ts / 60000) * 60000) / 60000);
  } catch(e){}
  tzCache = {key: key, off: off};
  return off;
}
function minuteOfDay(ts){ var m = Math.floor(ts / 60000) + tzOffsetMin(ts); return ((m % 1440) + 1440) % 1440; }
function hhmm(m){ var h = Math.floor(m / 60), mm = m % 60; return (h < 10 ? '0' : '') + h + ':' + (mm < 10 ? '0' : '') + mm; }

function timeTicks(p){
  var list = chart ? chart.getDataList() : [];
  if(!list.length) return p.defaultTicks;
  var from = Math.max(0, Math.ceil(p.range.realFrom)), to = Math.min(list.length - 1, Math.floor(p.range.realTo));
  var bs = chart.getBarSpace().bar, tf = (state.payload && state.payload.tf) || state.tf;
  var steps = [5, 10, 15, 30, 60, 120, 240], step = 240;
  for(var k = 0; k < steps.length; k++){ if(steps[k] >= tf && steps[k] / tf * bs >= 70){ step = steps[k]; break; } }
  var ticks = [];
  for(var i = from; i <= to; i++){
    var m = minuteOfDay(list[i].timestamp);
    if(m % step !== 0) continue;
    var c = chart.convertToPixel({dataIndex: i}, {paneId: PANE});
    // skip labels that would be clipped at either edge
    if(c && isFinite(c.x) && c.x > 20 && c.x < p.bounding.width - 20) ticks.push({coord: c.x, value: list[i].timestamp, text: hhmm(m)});
  }
  return ticks.length ? ticks : p.defaultTicks;
}

// ------------------------------------------------------------ registration

function pill(color, hollow, size){
  return {style: 'fill', color: hollow ? color : C.ink, size: size || 11, family: SANS, weight: 700,
          paddingLeft: 6, paddingRight: 6, paddingTop: 3, paddingBottom: 3, borderRadius: 4,
          borderStyle: 'solid', borderSize: 1, borderColor: color, borderDashedValue: [2, 2],
          backgroundColor: hollow ? C.panelA : color};
}

function registerAll(klc){
  if(registered) return;
  registered = true;
  SERIES.forEach(function(s){
    try {
      klc.registerIndicator({
        name: 'TA_' + s.key.toUpperCase(), shortName: s.label, series: 'price', precision: 2,
        figures: [{key: 'v', title: s.label + ' ', type: 'line',
                   styles: function(){ return {color: s.color, size: 1.75, style: 'solid'}; }}],
        calc: function(dataList){
          var vals = seriesValues(s.key, dataList);
          state.legend[s.key] = vals;
          return vals.map(function(v){ return {v: v}; });
        },
        // values are shown in the candle legend row, not a row per line
        createTooltipDataSource: function(){ return {name: '', calcParamsText: '', features: [], legends: []}; },
      });
    } catch(e){ /* already registered on a redeploy-without-reload */ }
  });
  try {
    // Horizontal strategy level: full-width line, left label, price tag on the
    // axis. A level outside the visible range gets an edge-pinned label.
    klc.registerOverlay({
      name: 'taLevel', totalStep: 2, needDefaultPointFigure: false, needDefaultXAxisFigure: false, needDefaultYAxisFigure: false,
      createPointFigures: function(p){
        var ed = p.overlay.extendData || {}, c = p.coordinates[0];
        if(!c || !isFinite(c.y)) return [];
        var w = p.bounding.width, h = p.bounding.height, y = c.y;
        if(y < 0 || y > h){
          var top = y < 0;
          return [{type: 'text', ignoreEvent: true,
                   attrs: {x: w - 8, y: top ? 8 : h - 8, text: (top ? '▲ ' : '▼ ') + ed.label, align: 'right', baseline: top ? 'top' : 'bottom'},
                   styles: pill(ed.color, true)}];
        }
        var figs = [{type: 'line', ignoreEvent: true, attrs: {coordinates: [{x: 0, y: y}, {x: w, y: y}]},
                     styles: {color: ed.color, size: ed.size || 1, style: ed.dashed ? 'dashed' : 'solid', dashedValue: [5, 4]}}];
        if(ed.label) figs.push({type: 'text', ignoreEvent: true, attrs: {x: w - 6, y: y, text: ed.label, align: 'right', baseline: 'middle'},
                                styles: pill(ed.color, !ed.strong)});
        return figs;
      },
      createYAxisFigures: function(p){
        var ed = p.overlay.extendData || {}, c = p.coordinates[0];
        if(!ed.axis || !c || !isFinite(c.y) || c.y < 0 || c.y > p.bounding.height) return [];
        return [{type: 'text', ignoreEvent: true, attrs: {x: 0, y: c.y, text: ed.axis, align: 'left', baseline: 'middle'},
                 styles: Object.assign(pill(ed.color, !ed.strong, 11.5), {family: MONO, borderRadius: 2, paddingLeft: 4, paddingRight: 4})}];
      },
    });
    // Price band (the entry noise band): full-width tint between two prices.
    klc.registerOverlay({
      name: 'taBand', totalStep: 3, needDefaultPointFigure: false, needDefaultXAxisFigure: false, needDefaultYAxisFigure: false,
      createPointFigures: function(p){
        var c = p.coordinates, ed = p.overlay.extendData || {};
        if(c.length < 2 || !isFinite(c[0].y) || !isFinite(c[1].y)) return [];
        var y0 = Math.min(c[0].y, c[1].y), y1 = Math.max(c[0].y, c[1].y);
        var figs = [{type: 'rect', ignoreEvent: true, attrs: {x: 0, y: y0, width: p.bounding.width, height: Math.max(1, y1 - y0)},
                     styles: {style: 'fill', color: ed.color || C.noise}}];
        if(ed.label) figs.push({type: 'text', ignoreEvent: true, attrs: {x: p.bounding.width - 8, y: y0 + 3, text: ed.label, align: 'right', baseline: 'top'},
                                styles: {color: 'rgba(255,123,114,.75)', size: 10.5, family: SANS, backgroundColor: 'transparent', borderSize: 0}});
        return figs;
      },
    });
    // Fill / exit marker: arrow at the exact price, stem, labelled pill.
    klc.registerOverlay({
      name: 'taMarker', totalStep: 2, needDefaultPointFigure: false, needDefaultXAxisFigure: false, needDefaultYAxisFigure: false,
      createPointFigures: function(p){
        var ed = p.overlay.extendData || {}, c = p.coordinates[0];
        if(!c || !isFinite(c.x) || !isFinite(c.y)) return [];
        var d = ed.below ? 1 : -1, tip = c.y + d * 2, base = c.y + d * 10, end = c.y + d * 26;
        var st = pill(ed.color, ed.hollow);
        var half = (klcRef.utils.calcTextWidth(ed.label, st.size, st.weight, st.family) + st.paddingLeft + st.paddingRight) / 2;
        var lx = Math.max(half + 2, Math.min(p.bounding.width - half - 2, c.x));
        return [
          {type: 'polygon', ignoreEvent: true, attrs: {coordinates: [{x: c.x, y: tip}, {x: c.x - 5.5, y: base}, {x: c.x + 5.5, y: base}]},
           styles: {style: 'fill', color: ed.color}},
          {type: 'line', ignoreEvent: true, attrs: {coordinates: [{x: c.x, y: base}, {x: c.x, y: end}]}, styles: {color: ed.color, size: 1}},
          {type: 'text', ignoreEvent: true, attrs: {x: lx, y: end, text: ed.label, align: 'center', baseline: ed.below ? 'top' : 'bottom'},
           styles: st}];
      },
    });
    // Vertical event line (time stop, detection, as-of cutoff) with a label
    // near the bottom so it never collides with the legend.
    klc.registerOverlay({
      name: 'taVLine', totalStep: 2, needDefaultPointFigure: false, needDefaultXAxisFigure: false, needDefaultYAxisFigure: false,
      createPointFigures: function(p){
        var c = p.coordinates[0], ed = p.overlay.extendData || {};
        if(!c || !isFinite(c.x)) return [];
        var h = p.bounding.height, w = p.bounding.width, right = c.x > w - 170;
        return [
          {type: 'line', ignoreEvent: true, attrs: {coordinates: [{x: c.x, y: 0}, {x: c.x, y: h}]},
           styles: {color: ed.color, style: 'dashed', dashedValue: [4, 4], size: 1}},
          {type: 'text', ignoreEvent: true,
           attrs: {x: right ? c.x - 5 : c.x + 5, y: h - 8 - (ed.row || 0) * 24, text: ed.label, align: right ? 'right' : 'left', baseline: 'bottom'},
           styles: pill(ed.color, true, 10.5)}];
      },
    });
  } catch(e){}
}

// ------------------------------------------------------------ overlays

function clearStrategyOverlays(){
  if(!chart) return;
  try { chart.removeOverlay({groupId: 'strategy'}); } catch(e){}
}

function levelOverlay(lv, summary){
  var st = LEVEL_STYLE[lv.kind] || LEVEL_STYLE.structural;
  if(lv.kind === 'noise_band'){
    if(num(lv.high) == null || num(lv.low) == null) return null;
    return {name: 'taBand', points: [{value: num(lv.high)}, {value: num(lv.low)}],
            extendData: {color: C.noise, label: 'noise band'}};
  }
  var price = num(lv.price); if(price == null) return null;
  // the price sits in the axis tag right beside the pill, so the pill carries
  // only the name ("Entry breakout", "Stop (9 EMA)", "PDH") and stays short
  var label = String(lv.label || lv.kind).replace(/\s*@?\s*-?\d+(\.\d+)?\s*$/, '') || lv.kind;
  if(lv.kind === 'target' && summary && summary.reward_r != null && lv.source !== 'broker') label += '  ·  ' + fmtR(summary.reward_r);
  if(lv.source === 'broker') label += ' (broker' + (lv.status ? ', ' + lv.status : '') + ')';
  return {name: 'taLevel', points: [{value: price}],
          extendData: {label: label, axis: price.toFixed(2), color: st.color, strong: !!st.strong, dashed: !!st.dashed, size: st.size}};
}

function markerOverlay(m, dir){
  var price = num(m.price);
  if(m.timestamp == null || price == null || (m.kind !== 'fill' && m.kind !== 'exit')) return null;
  var long = dir !== 'short', hollow = String(m.source || '').indexOf('disagree') >= 0;
  if(m.kind === 'fill'){
    return {name: 'taMarker', points: [{timestamp: m.timestamp, value: price}],
            extendData: {label: (long ? 'BUY ' : 'SELL ') + price.toFixed(2), color: C.entry, below: long, hollow: hollow}};
  }
  var reason = String(m.reason || ''), color = reason === 'target' ? C.target : reason === 'stop' ? C.stop : C.amber;
  var word = reason === 'target' ? 'TARGET ' : reason === 'stop' ? 'STOPPED ' : 'EXIT ';
  return {name: 'taMarker', points: [{timestamp: m.timestamp, value: price}],
          extendData: {label: word + price.toFixed(2), color: color, below: !long, hollow: hollow}};
}

// Windows + price-less markers, merged per timestamp so the runner lane's
// "earliest honest fill" (present as both) is drawn once.
function collectEvents(p){
  var byTs = {}, order = [];
  function add(ts, label, color){
    if(ts == null || !label) return;
    var k = String(ts);
    if(!byTs[k]){ byTs[k] = {ts: ts, labels: [], color: color}; order.push(k); }
    if(!byTs[k].labels.some(function(x){ return x.toLowerCase() === label.toLowerCase(); })) byTs[k].labels.push(label);
  }
  (p.windows || []).forEach(function(w){
    if(w.kind === 'time_stop') add(w.at, 'Time stop', C.stop);
    else if(w.kind === 'asof_cutoff') add(w.at, 'As-of cutoff', C.amber);
    else if(w.kind === 'honest_entry') add(w.at, 'Earliest honest fill', C.purple);
    else if(w.kind === 'detection_bar') add(w.start, 'Detected', C.vwap);
  });
  (p.markers || []).forEach(function(m){
    if(num(m.price) != null) return;
    if(m.kind === 'detection') add(m.timestamp, 'Detected', C.vwap);
    else if(m.kind === 'detection_close') add(m.timestamp, 'Detection bar close', C.vwap);
    else if(m.kind === 'honest_entry') add(m.timestamp, 'Earliest honest fill', C.purple);
  });
  return order.map(function(k){ return byTs[k]; }).sort(function(a, b){ return a.ts - b.ts; });
}

function renderStrategyOverlays(){
  if(!chart || !state.payload) return;
  clearStrategyOverlays();
  var p = state.payload, s = p.summary || {}, list = [];
  if(state.layers.levels) (p.levels || []).forEach(function(lv){ var o = levelOverlay(lv, s); if(o) list.push(o); });
  if(state.layers.fills) (p.markers || []).forEach(function(m){ var o = markerOverlay(m, s.direction); if(o) list.push(o); });
  if(state.layers.events) collectEvents(p).forEach(function(ev, i){
    list.push({name: 'taVLine', points: [{timestamp: ev.ts}], extendData: {label: ev.labels.join(' · '), color: ev.color, row: i % 3}});
  });
  list.forEach(function(o){
    // one bad overlay must never blank the rest of the chart
    try { chart.createOverlay(Object.assign({groupId: 'strategy', lock: true}, o)); } catch(e){}
  });
}

// ----------------------------------------------------------------- header / stats

function renderHeader(){
  $('chart-title').textContent = state.ticker || '—';
  var d = state.date ? new Date(state.date + 'T12:00:00Z') : null;
  $('chart-sub').textContent = d && !isNaN(d) ? d.toLocaleDateString('en-US', {weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC'}) : (state.date || '');
  var p = state.payload, s = (p && p.summary) || {}, c = (p && p.context) || {}, b = '';
  if(s.direction) b += '<span class="ch-badge ' + (s.direction === 'short' ? 'short' : 'long') + '">' + esc(s.direction) + '</span> ';
  if(c.counterfactual) b += '<span class="ch-badge cf" title="Rejected at plan time: this chart shows what would have happened">Not traded</span> ';
  if(isLiveOpenPosition()) b += '<span class="ch-badge live">Live</span> ';
  $('chart-badges').innerHTML = b;
  var sel = $('chart-lane'), contexts = (p && p.contexts) || [];
  sel.innerHTML = contexts.length ? contexts.map(function(x){
    var v = x.lane + ':' + x.id, on = (x.lane === p.lane && String(x.id) === String(p.id)) ? ' selected' : '';
    var lane = String(x.lane || ''); lane = lane.charAt(0).toUpperCase() + lane.slice(1);
    return '<option value="' + esc(v) + '"' + on + '>' + esc(lane) + ' · ' + esc(String(x.label || '').replace(/_/g, ' ')) + '</option>';
  }).join('') : '<option>No plan / trade on this date</option>';
  sel.disabled = contexts.length < 2;
  var stale = $('chart-stale');
  stale.hidden = !(p && p.clamped);
  if(p && p.clamped) stale.textContent = 'Delayed data' + (p.data_through_et ? ' · through ' + p.data_through_et.slice(11, 16) + ' ET' : '');
}

function stat(k, v, s, cls){
  return '<div class="ch-stat ' + (cls || '') + '"><div class="k">' + (cls ? '<i></i>' : '') + esc(k) + '</div><div class="v">' + v + '</div>' +
         '<div class="s">' + (s || '&nbsp;') + '</div></div>';
}

function renderStats(){
  var el = $('chart-stats'), p = state.payload;
  if(!p){ el.innerHTML = '<div class="ch-stats-main"></div>'; return; }
  var s = p.summary || {}, cells = [];
  var risk = s.risk_per_share != null ? 'risk ' + px(s.risk_per_share) + (s.risk_pct != null ? ' · ' + Number(s.risk_pct).toFixed(2) + '%' : '') : '';
  cells.push(stat(s.entry_label || 'Entry', px(s.entry), '', 'k-entry'));
  cells.push(stat('Stop', px(s.stop), esc(risk), 'k-stop'));
  cells.push(stat('Target', px(s.target), s.reward_r != null ? esc(Number(s.reward_r).toFixed(2) + 'R reward') : '', 'k-target'));
  if(s.fill) cells.push(stat('Fill', px(s.fill.price), esc(clock(s.fill.timestamp))));
  if(s.exit) cells.push(stat('Exit', px(s.exit.price), esc([s.exit.reason, clock(s.exit.timestamp)].filter(Boolean).join(' · '))));
  var res = s.result, r = res ? num(res.r) : null;
  var cls = r == null ? 'none' : r > 0 ? 'pos' : r < 0 ? 'neg' : '';
  el.innerHTML = '<div class="ch-stats-main">' + cells.join('') + '</div>' +
    '<div class="ch-result ' + cls + '"><div class="k">' + esc(res ? res.label : 'Outcome') + '</div>' +
    '<div class="v">' + esc(r == null ? '—' : fmtR(r)) + '</div><div class="s">' + esc(s.status || '') + '</div></div>';
}

function renderToolbar(){
  if(!built) return;
  document.querySelectorAll('#chart-tf button').forEach(function(b){ b.classList.toggle('on', Number(b.dataset.tf) === state.tf); });
  var avail = (state.payload && state.payload.series) || null;
  document.querySelectorAll('#chart-toolbar button[data-layer]').forEach(function(b){
    var k = b.dataset.layer;
    b.classList.toggle('on', !!state.layers[k]);
    b.setAttribute('aria-pressed', state.layers[k] ? 'true' : 'false');
    var def = SERIES.find(function(s){ return s.key === k; });
    if(def && avail){
      b.hidden = !avail[k];
      b.lastChild.textContent = seriesLabel(def);
    }
  });
  $('chart-asof-toggle').classList.toggle('on', state.asof);
  $('chart-tz-toggle').textContent = tzShort();
  $('chart-ctx-toggle').classList.toggle('on', state.details);
  $('chart-ctx').classList.toggle('collapsed', !state.details);
  var n = Object.keys(studyPanes).length;
  $('chart-studies-n').innerHTML = n ? '<span class="ch-count">' + n + '</span>' : '';
}

function toggleLayer(key){
  state.layers[key] = !state.layers[key];
  savePrefs(); renderToolbar();
  if(!chart) return;
  if(SERIES.some(function(s){ return s.key === key; })) setSeriesIndicator(key, state.layers[key]);
  else {
    renderStrategyOverlays();
    // re-run the candle range so levels stop (or start) widening it
    if(key === 'levels') chart.overrideYAxis({paneId: PANE, createRange: candleRange});
  }
}

// -------------------------------------------------------------- details panel

function rows(pairs){
  return pairs.filter(function(p){ return p[1] != null && p[1] !== ''; }).map(function(p){
    return '<div class="row"><span>' + esc(p[0]) + '</span><span>' + esc(p[1]) + '</span></div>';
  }).join('');
}

function renderCtx(){
  var box = $('chart-ctx'); if(!box) return;
  var p = state.payload;
  if(!p){ box.innerHTML = ''; return; }
  var c = p.context || {}, h = '';
  if(c.kill_reason) h += '<div class="callout"><div class="h">Rejected because</div><p class="txt">' + esc(c.kill_reason) + '</p></div>';
  var plan = rows([['Setup', c.setup_type ? String(c.setup_type).replace(/_/g, ' ') : null], ['Direction', c.direction],
                   ['Conviction', c.conviction], ['Size', c.size_r != null ? c.size_r + 'R' : null],
                   ['Catalyst', c.catalyst], ['Selection rank', c.selection_rank]]);
  if(plan) h += '<h3>Plan</h3>' + plan;
  if(c.thesis) h += '<h3>Thesis</h3><p class="txt">' + esc(c.thesis) + '</p>';
  if(c.invalidation) h += '<h3>Invalidation</h3><p class="txt">' + esc(c.invalidation) + '</p>';
  if(c.live){
    h += '<h3>Live position</h3>' + rows([['Status', c.live.open ? 'Open' : 'Closed'], ['Fill', px(c.live.fill)],
      ['Qty', (c.live.qty != null ? c.live.qty : '') + ' ' + (c.live.side || '')], ['Unrealized', fmtR(c.live.unrealized_r)],
      ['Broker status', c.live.status]]);
  }
  var o = c.outcome;
  var orows = o ? rows([
      ['Triggered', o.triggered == null ? null : (o.triggered ? 'yes' : 'no')], ['Exit', o.exit_reason],
      ['MFE / MAE', o.mfe_r != null ? fmtR(o.mfe_r) + ' / ' + fmtR(o.mae_r) : null],
      ['Untriggered', o.untriggered_reason], ['Shadow grade', o.eod_outcome ? String(o.eod_outcome).replace(/_/g, ' ') : null],
      ['Sim version', o.sim_version != null ? 'v' + o.sim_version : null]]) : '';
  if(orows) h += '<h3>Outcome detail</h3>' + orows;
  var series = p.series || {}, basis = '';
  SERIES.forEach(function(s){
    if(series[s.key] && series[s.key].basis) basis += '<div class="basis"><i style="background:' + s.color + '"></i><span><b>' + esc(s.label) + '</b> — ' + esc(series[s.key].basis) + '</span></div>';
  });
  h += '<h3>Data</h3>' + rows([['Feed', p.feed], ['Data through', p.data_through_et ? p.data_through_et.slice(11, 16) + ' ET' : '—'],
                               ['Bars', (p.bars || []).length + ' × ' + p.tf + 'm']]) + basis;
  if(p.clamped) h += '<div class="warn-item">Real-time SIP window clamped — the last bars may lag.</div>';
  (p.warnings || []).forEach(function(w){ h += '<div class="warn-item">' + esc(w) + '</div>'; });
  (p.guards || []).forEach(function(g){ h += '<div class="guard-item ' + (g.ok ? 'ok' : 'bad') + '">' + esc(g.ok ? '✓ ' : '✕ ') + esc(g.name) + ': ' + esc(g.detail) + '</div>'; });
  box.innerHTML = h;
}

function renderGuardBanner(payload){
  var el = $('chart-guard-banner');
  var bad = (payload.guards || []).find(function(g){ return !g.ok; });
  if(bad){ el.textContent = 'Replay disagrees with the ledger — ' + bad.detail; el.classList.add('show'); }
  else el.classList.remove('show');
}

// ------------------------------------------------------------------ studies
// Exploratory, library-computed indicators -- explicitly NOT ledger features.

function toggleStudiesMenu(anchor){
  var menu = $('chart-menu');
  if(menu.classList.contains('show')){ menu.classList.remove('show'); return; }
  var html = '<div class="hint">Exploratory studies, computed in the browser by the chart library — not ledger features.</div>';
  STUDIES.forEach(function(st){
    html += '<label><input type="checkbox" data-study="' + st.name + '"' + (studyPanes[st.name] ? ' checked' : '') + '> ' + esc(st.label) + '</label>';
  });
  menu.innerHTML = html;
  var r = anchor.getBoundingClientRect();
  menu.style.top = (r.bottom + 6) + 'px';
  menu.style.left = Math.max(8, Math.min(window.innerWidth - 250, r.left)) + 'px';
  menu.classList.add('show');
  menu.querySelectorAll('input[data-study]').forEach(function(cb){
    cb.addEventListener('change', function(){ toggleStudy(cb.dataset.study, cb.checked); });
  });
}

function toggleStudy(name, on){
  if(!chart) return;
  var st = STUDIES.find(function(s){ return s.name === name; }) || {};
  try {
    if(on && !studyPanes[name]){
      var paneId = st.overlay ? PANE : 'ta_study_' + name;
      var id = chart.createIndicator({name: name, paneId: paneId}, !!st.overlay);
      if(id){
        studyPanes[name] = paneId;
        if(!st.overlay) try { chart.setPaneOptions({id: paneId, height: 90, minHeight: 50}); } catch(e){}
      }
    } else if(!on && studyPanes[name]){
      chart.removeIndicator({paneId: studyPanes[name], name: name});
      delete studyPanes[name];
    }
  } catch(e){}
  renderToolbar();
}

// -------------------------------------------------------------- drawing rail
// Built-in overlays only (§6): personal annotations, persisted per
// (ticker, date) to localStorage -- never the ledger (drawings aren't
// evidence). A distinct groupId keeps "delete all" off strategy overlays.

function buildDrawRail(){
  var svg = function(k){ return '<svg viewBox="0 0 16 16" aria-hidden="true">' + ICON[k] + '</svg>'; };
  var html = DRAW_TOOLS.map(function(t){
    return '<button data-tool="' + t[0] + '" title="' + esc(t[1]) + '" aria-label="' + esc(t[1]) + '">' + svg(t[0]) + '</button>';
  }).join('');
  html += '<div class="sep"></div>' +
          '<button id="chart-draw-undo" title="Undo last drawing" aria-label="Undo last drawing">' + svg('undo') + '</button>' +
          '<button id="chart-draw-clear" title="Delete all drawings on this chart" aria-label="Delete all drawings">' + svg('trash') + '</button>';
  var rail = $('chart-rail');
  rail.innerHTML = html;
  rail.querySelectorAll('button[data-tool]').forEach(function(b){
    b.addEventListener('click', function(){
      if(b.classList.contains('on')){ cancelDrawing(); return; }
      startDrawing(b.dataset.tool, DRAW_TOOLS.find(function(t){ return t[0] === b.dataset.tool; })[1]);
    });
  });
  $('chart-draw-undo').addEventListener('click', function(){
    if(!chart) return;
    var mine = chart.getOverlays({groupId: 'user-drawing'});
    if(!mine.length) return;
    try { chart.removeOverlay({id: mine[mine.length - 1].id}); } catch(e){}
    persistDrawings();
  });
  $('chart-draw-clear').addEventListener('click', function(){
    if(!chart || !chart.getOverlays({groupId: 'user-drawing'}).length) return;
    if(!window.confirm('Delete all your drawings on ' + state.ticker + ' ' + state.date + '?')) return;
    clearUserDrawings(); persistDrawings();
  });
}

function drawHandlers(){
  return {
    onDrawEnd: function(){ drawingId = null; setActiveTool(null); setTimeout(persistDrawings, 0); },
    onPressedMoveEnd: function(){ setTimeout(persistDrawings, 0); },
    onRightClick: function(){ setTimeout(persistDrawings, 0); },   // library default removes it; save after
    onSelected: function(e){ selectedId = e && e.overlay ? e.overlay.id : null; },
    onDeselected: function(){ selectedId = null; },
  };
}

function startDrawing(tool, title){
  if(!chart) return;
  cancelDrawing();
  var extend;
  if(tool === 'simpleAnnotation'){
    extend = window.prompt('Note text');
    if(!extend) return;
  }
  try {
    drawingId = chart.createOverlay(Object.assign({name: tool, groupId: 'user-drawing', extendData: extend}, drawHandlers()));
  } catch(e){ drawingId = null; }
  if(drawingId){
    setActiveTool(tool);
    showHint(title + ': click on the chart to place it · Esc cancels · right-click a drawing to delete it');
  }
}

function cancelDrawing(){
  if(drawingId && chart){ try { chart.removeOverlay({id: drawingId}); } catch(e){} }
  drawingId = null;
  setActiveTool(null);
}

function setActiveTool(tool){
  if(!built) return;
  $('chart-rail').querySelectorAll('button[data-tool]').forEach(function(b){ b.classList.toggle('on', b.dataset.tool === tool); });
  if(!tool) showHint('');
}

function showHint(msg){
  var el = $('chart-hint'); if(!el) return;
  el.textContent = msg; el.classList.toggle('show', !!msg);
}

function drawKey(){ return 'ta_draw:' + state.ticker + ':' + state.date; }

function clearUserDrawings(){
  cancelDrawing();
  if(chart) try { chart.removeOverlay({groupId: 'user-drawing'}); } catch(e){}
  selectedId = null;
}

function persistDrawings(){
  if(!chart) return;
  try {
    var saved = chart.getOverlays({groupId: 'user-drawing'}).filter(function(o){ return o.id !== drawingId; }).map(function(o){
      return {name: o.name, extendData: o.extendData,
              points: (o.points || []).map(function(pt){ return {timestamp: pt.timestamp, value: pt.value}; })};
    });
    if(saved.length) localStorage.setItem(drawKey(), JSON.stringify(saved));
    else localStorage.removeItem(drawKey());
  } catch(e){}
}

// Re-created from saved (timestamp, value) points after every full load, so a
// timeframe switch re-anchors them to the new bars.
function restoreDrawings(){
  if(!chart) return;
  cancelDrawing();
  try { chart.removeOverlay({groupId: 'user-drawing'}); } catch(e){}
  var saved = [];
  try { saved = JSON.parse(localStorage.getItem(drawKey()) || '[]') || []; } catch(e){}
  saved.forEach(function(o){
    try { chart.createOverlay(Object.assign({name: o.name, groupId: 'user-drawing', points: o.points, extendData: o.extendData}, drawHandlers())); } catch(e){}
  });
}

// ---------------------------------------------------------------- live mode
// Phase 5: polling (never a websocket -- one small bars call every 20s),
// paused on document.hidden, stopped once the position closes.

function isLiveOpenPosition(){
  var p = state.payload;
  return !!(p && p.lane === 'live' && p.context && p.context.live && p.context.live.open);
}

function manageLiveMode(){
  if(isLiveOpenPosition() && !document.hidden){ if(!state.pollTimer) startPolling(); }
  else stopPolling();
}

function startPolling(){
  stopPolling();
  state.pollTimer = setInterval(pollOnce, 20000);
}

function stopPolling(){
  if(state.pollTimer){ clearInterval(state.pollTimer); state.pollTimer = null; }
}

function pollOnce(){
  if(document.hidden || !isLiveOpenPosition()){ stopPolling(); return; }
  if(state.loading || !chart) return;
  var seq = loadSeq;
  fetchPayload().then(function(payload){
    if(seq !== loadSeq || !subscribedBar) return;   // a full reload started meanwhile
    var list = chart.getDataList(), lastTs = list.length ? list[list.length - 1].timestamp : -Infinity;
    state.payload = payload;
    prepareSeries(payload);
    (payload.bars || []).forEach(function(b){ if(b.timestamp >= lastTs) subscribedBar(toKLine(b)); });
    onPayload(payload);
  }).catch(function(){});
}

function renderPositionTabs(payload){
  var el = $('chart-tabs');
  var opens = (payload.contexts || []).filter(function(c){ return c.lane === 'live'; });
  if(opens.length < 2){ el.style.display = 'none'; return; }
  el.style.display = 'flex';
  el.innerHTML = opens.map(function(c){
    var on = String(c.id) === String(payload.id) ? ' on' : '';
    return '<button class="ch-chip' + on + '" data-id="' + esc(c.id) + '">' + esc(c.label || c.id) + '</button>';
  }).join('');
  el.querySelectorAll('button').forEach(function(b){
    b.addEventListener('click', function(){ state.lane = 'live'; state.id = b.dataset.id; syncHash(); loadData(); });
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
