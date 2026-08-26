"""
Arby — the Trade Agent desk mascot.

A fully self-contained HTML/CSS/JS widget (Three.js voxel character in a trading
room) that both dashboards embed. Strictly decoupled from app logic:

  - All DOM lives inside #arby-card; the only outside-DOM touches are READ-ONLY
    (observing the #running indicator, scrollIntoView on existing cards).
  - Data flows IN through window.MASCOT_BOOT (inlined by delivery.py for the
    static dashboard) or a polled fetch of /api/status (hosted dashboard).
    It never calls run endpoints or mutates anything.
  - Room props are click targets that open external trading resources
    (Yahoo Finance, Benzinga, TradingView, Finviz, StockTwits, CNBC, econ
    calendar) in new tabs, plus in-page jumps to the Playbook/Performance cards.
  - Degrades gracefully: no WebGL / no CDN -> link chips + mood pill still work.

Embed by replacing MASCOT_MARKER in a page with mascot_embed(boot_or_None).
"""
from __future__ import annotations

import json

MASCOT_MARKER = "<!--ARBY-->"

_SNIPPET = r"""
<style>
  #arby-card canvas{display:block;margin:0 auto;border-radius:8px;}
  #arby-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px;}
  #arby-top h2{margin:0;}
  #arby-sub{color:#8b949e;font-size:13px;font-weight:400;}
  #arby-market{font-size:12px;color:#8b949e;margin-right:12px;font-family:Consolas,monospace;}
  #arby-toggle{padding:4px 12px;margin:0;background:#21262d;border:1px solid #30363d;border-radius:6px;
    color:#e6edf3;font-size:12px;font-weight:600;cursor:pointer;}
  #arby-wrap{position:relative;outline:none;max-width:1000px;margin:0 auto;}
  #arby-wrap.focused canvas{box-shadow:0 0 0 2px #58a6ff;}
  #arby-wrap:fullscreen{max-width:none;width:100%;height:100%;background:#0d1117;
    display:flex;align-items:center;justify-content:center;}
  #arby-wrap:-webkit-full-screen{max-width:none;width:100%;height:100%;background:#0d1117;
    display:flex;align-items:center;justify-content:center;}
  #arby-fullscreen{position:absolute;right:10px;top:10px;z-index:3;
    background:rgba(13,17,23,.75);border:1px solid #30363d;border-radius:6px;
    color:#e6edf3;font-size:14px;line-height:1;padding:6px 9px;cursor:pointer;}
  #arby-fullscreen:hover{border-color:#58a6ff;color:#58a6ff;}
  #arby-bubble{position:absolute;pointer-events:none;opacity:0;transform:translate(-50%,-8px);
    background:#1c2128;border:1px solid #30363d;border-radius:8px;color:#e6edf3;
    padding:6px 10px;font-size:12px;max-width:200px;text-align:center;z-index:3;
    transition:opacity .25s ease,transform .25s ease;}
  #arby-bubble.show{opacity:1;transform:translate(-50%,-16px);}
  #arby-bubble:after{content:"";position:absolute;left:50%;bottom:-6px;transform:translateX(-50%);
    border:6px solid transparent;border-top-color:#1c2128;}
  #arby-tip{position:absolute;pointer-events:none;display:none;background:rgba(13,17,23,.92);
    border:1px solid #58a6ff;border-radius:6px;color:#e6edf3;padding:4px 9px;font-size:11px;
    z-index:4;white-space:nowrap;}
  #arby-hint{position:absolute;left:10px;bottom:10px;font-size:11px;color:#0d1117;
    background:rgba(255,255,255,.85);padding:4px 8px;border-radius:6px;z-index:2;
    transition:opacity .4s ease;pointer-events:none;}
  #arby-hint.hide{opacity:0;}
  #arby-hud{display:flex;justify-content:space-between;align-items:center;max-width:1000px;
    margin:8px auto 0;font-size:12px;color:#8b949e;gap:10px;flex-wrap:wrap;}
  #arby-mood{padding:2px 10px;border-radius:12px;font-weight:700;font-size:11px;white-space:nowrap;}
  #arby-needs{display:flex;align-items:center;gap:8px;}
  .arby-need{display:flex;align-items:center;gap:4px;}
  .arby-need-label{font-size:9px;letter-spacing:.04em;color:#8b949e;}
  .arby-need-bar{width:44px;height:6px;background:#0d1117;border:1px solid #30363d;border-radius:4px;overflow:hidden;}
  .arby-need-fill{height:100%;width:100%;background:#3fb950;transition:width .4s ease,background .4s ease;}
  #arby-links{display:flex;flex-wrap:wrap;gap:6px;max-width:1000px;margin:10px auto 0;}
  #arby-links a{font-size:11px;color:#e6edf3;text-decoration:none;background:#21262d;
    border:1px solid #30363d;border-radius:14px;padding:3px 10px;}
  #arby-links a:hover{border-color:#58a6ff;color:#58a6ff;}
  #arby-fallback{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:24px;
    text-align:center;color:#8b949e;font-size:13px;}
</style>
<div class="card" id="arby-card">
  <div id="arby-top">
    <h2>&#129489;&#8205;&#128187; Arby <span id="arby-sub">&mdash; Head of Paper Trading</span></h2>
    <div>
      <span id="arby-market"></span>
      <button id="arby-toggle" type="button">Hide</button>
    </div>
  </div>
  <div id="arby-body">
    <div id="arby-wrap" tabindex="0">
      <div id="arby-bubble"></div>
      <div id="arby-tip"></div>
      <div id="arby-hint">Click the floor to send him around &middot; click anything in the room to interact &middot; drag to look around, right-drag to pan, scroll/pinch to zoom &middot; dbl-click resets the view</div>
      <button id="arby-fullscreen" type="button" title="Fullscreen">&#9974;</button>
    </div>
    <div id="arby-hud">
      <span id="arby-activity">warming up&hellip;</span>
      <span id="arby-mood">NEUTRAL</span>
      <div id="arby-needs">
        <div class="arby-need"><span class="arby-need-label">ENERGY</span><div class="arby-need-bar"><div id="arby-need-energy" class="arby-need-fill"></div></div></div>
        <div class="arby-need"><span class="arby-need-label">FOCUS</span><div class="arby-need-bar"><div id="arby-need-focus" class="arby-need-fill"></div></div></div>
      </div>
    </div>
    <div id="arby-links"></div>
  </div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>
(function(){
  'use strict';
  // ------------------------------------------------------------- DOM refs
  var card = document.getElementById('arby-card');
  var wrap = document.getElementById('arby-wrap');
  var body = document.getElementById('arby-body');
  var bubble = document.getElementById('arby-bubble');
  var tip = document.getElementById('arby-tip');
  var hint = document.getElementById('arby-hint');
  var hudActivity = document.getElementById('arby-activity');
  var hudMood = document.getElementById('arby-mood');
  var hudMarket = document.getElementById('arby-market');
  var needEnergyEl = document.getElementById('arby-need-energy');
  var needFocusEl = document.getElementById('arby-need-focus');
  var fsBtn = document.getElementById('arby-fullscreen');
  var toggleBtn = document.getElementById('arby-toggle');
  var linksRow = document.getElementById('arby-links');
  if (!card || !wrap) return;

  // ------------------------------------------------------------- shared state
  var state = {
    status: null,             // last /api/status payload (or boot data)
    slotTickers: { main:'SPY', left:'QQQ', right:'IWM' },
    bullets: ['Cut losers fast','Size to conviction','Let winners run','No trade = ok trade'],
    playbookVersion: null,
    lastGraded: null, lastTotalR: null,
    moodScore: 0,
    bySetup: [],
    // Sims-style motives: decay over time, restored by visiting the matching station.
    needs: { energy: 100, focus: 100 },
  };

  function needColor(v){ return v<30 ? '#f85149' : v<60 ? '#d29922' : '#3fb950'; }
  function refreshNeeds(){
    if (!needEnergyEl) return;
    needEnergyEl.style.width = state.needs.energy+'%';
    needEnergyEl.style.background = needColor(state.needs.energy);
    needFocusEl.style.width = state.needs.focus+'%';
    needFocusEl.style.background = needColor(state.needs.focus);
  }
  refreshNeeds();

  function clamp(v,a,b){ return Math.max(a, Math.min(b, v)); }

  // Eastern-time helpers (traders live on ET; so does Arby). Market state is
  // session-aware: pre-market / regular hours / after-hours / closed, with a
  // hardcoded NYSE holiday table so a weekday holiday never reads as OPEN.
  var ET_FMT;
  try { ET_FMT = new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hour12:false,
        weekday:'short',hour:'numeric',minute:'numeric'}); } catch(e){ ET_FMT = null; }
  function etNow(){
    if (!ET_FMT){ var d=new Date(); return {h:d.getHours(), m:d.getMinutes(), wd:'Mon', isWeekday:true}; }
    var parts = ET_FMT.formatToParts(new Date());
    var get = function(t){ var p = parts.find(function(x){return x.type===t;}); return p?p.value:'0'; };
    var wd = get('weekday');
    return { h: parseInt(get('hour'),10)%24, m: parseInt(get('minute'),10), wd: wd,
             isWeekday: wd!=='Sat' && wd!=='Sun' };
  }
  function etDate(){
    try {
      // en-CA yields an ISO-ish YYYY-MM-DD for the requested timezone
      var s = new Date().toLocaleDateString('en-CA', { timeZone:'America/New_York' });
      var p = s.split('-');
      return { year:+p[0], month:+p[1], day:+p[2] };
    } catch(e){
      var d = new Date();
      return { year:d.getFullYear(), month:d.getMonth()+1, day:d.getDate() };
    }
  }
  // NYSE observed holiday dates, month -> [days]. Extend as years roll over;
  // unknown years degrade to "treat like a normal weekday" rather than crash.
  var MARKET_HOLIDAYS = {
    2025:{1:[1,20],2:[17],4:[18],5:[26],6:[19],7:[4],9:[1],11:[27],12:[25]},
    2026:{1:[1,19],2:[16],4:[3],5:[25],6:[19],7:[3],9:[7],11:[26],12:[25]},
    2027:{1:[1],2:[15],3:[26],5:[31],6:[18],7:[5],9:[6],11:[25],12:[24]},
  };
  function isMarketHoliday(et){
    var year = MARKET_HOLIDAYS[et.year];
    if (!year) return false;
    var days = year[et.month];
    return !!(days && days.indexOf(et.day) >= 0);
  }
  function sessionPhase(){
    var t = etNow();
    if (!t.isWeekday || isMarketHoliday(etDate())) return 'closed';
    var mins = t.h*60 + t.m;
    if (mins < 240)  return 'closed';      // before 4:00 AM ET
    if (mins < 570)  return 'premarket';   // 4:00 - 9:30 AM ET
    if (mins < 960)  return 'open';        // 9:30 AM - 4:00 PM ET
    if (mins < 1200) return 'afterhours';  // 4:00 - 8:00 PM ET
    return 'closed';
  }
  function marketOpen(){ return sessionPhase()==='open'; }
  function sleepTime(){
    var t = etNow();
    if (!t.isWeekday) return true;               // weekends: market's closed, so is he
    if (isMarketHoliday(etDate())) return true;  // holidays too
    return t.h>=22 || t.h<5;
  }
  function dayPhase(){ var h=etNow().h;
    return (h<5||h>=21)?'night' : h<9?'dawn' : h<17?'day' : 'dusk'; }
  function etClockStr(){ var t=etNow(); return (t.h%12||12)+':'+String(t.m).padStart(2,'0')+(t.h<12?'a':'p')+' ET'; }

  function updateMarketBadge(){
    var t = etNow(), ph = sessionPhase(), txt, color;
    if (ph==='open'){ txt='\u25CF OPEN'; color='#3fb950'; }
    else if (ph==='premarket'){ txt='\u25CB PRE-MARKET'; color='#d29922'; }
    else if (ph==='afterhours'){ txt='\u25CB AFTER-HOURS'; color='#58a6ff'; }
    else { txt = (!t.isWeekday) ? '\u25CB WEEKEND' : (isMarketHoliday(etDate()) ? '\u25CB HOLIDAY' : '\u25CB CLOSED'); color='#8b949e'; }
    hudMarket.textContent = txt + ' \u00B7 ' + etClockStr();
    hudMarket.style.color = color;
  }
  updateMarketBadge();
  setInterval(updateMarketBadge, 15000);

  // ------------------------------------------------------------- link registry
  function mainTicker(){ return state.slotTickers.main || 'SPY'; }
  var LINKS = {
    yahoo:      { chip:'\uD83D\uDCC8 Yahoo Finance', label:function(){return 'Yahoo Finance \u2014 '+mainTicker()+' \u2197';},
                  url:function(){return 'https://finance.yahoo.com/quote/'+encodeURIComponent(mainTicker());} },
    tradingview:{ chip:'\uD83D\uDCCA TradingView', label:function(){return 'TradingView \u2014 '+mainTicker()+' chart \u2197';},
                  url:function(){return 'https://www.tradingview.com/chart/?symbol='+encodeURIComponent(mainTicker());} },
    benzinga:   { chip:'\uD83D\uDCF0 Benzinga', label:function(){return 'Benzinga news \u2014 '+mainTicker()+' \u2197';},
                  url:function(){return 'https://www.benzinga.com/quote/'+encodeURIComponent(mainTicker());} },
    finviz:     { chip:'\uD83D\uDDFA\uFE0F Finviz map', label:function(){return 'Finviz market map \u2197';},
                  url:function(){return 'https://finviz.com/map.ashx';} },
    stocktwits: { chip:'\uD83D\uDCAC StockTwits', label:function(){return 'StockTwits \u2014 $'+mainTicker()+' \u2197';},
                  url:function(){return 'https://stocktwits.com/symbol/'+encodeURIComponent(mainTicker());} },
    cnbc:       { chip:'\uD83D\uDCFA CNBC', label:function(){return 'CNBC Markets \u2197';},
                  url:function(){return 'https://www.cnbc.com/markets/';} },
    econcal:    { chip:'\uD83D\uDDD3\uFE0F Econ calendar', label:function(){return 'Economic calendar \u2197';},
                  url:function(){return 'https://finance.yahoo.com/calendar/economic';} },
  };
  var CHIP_ORDER = ['yahoo','tradingview','benzinga','finviz','stocktwits','cnbc','econcal'];
  function buildChips(){
    linksRow.innerHTML = '';
    CHIP_ORDER.forEach(function(key){
      var a = document.createElement('a');
      a.target = '_blank'; a.rel = 'noopener';
      a.dataset.arbyLink = key;
      linksRow.appendChild(a);
    });
    refreshChips();
  }
  function refreshChips(){
    linksRow.querySelectorAll('a').forEach(function(a){
      var def = LINKS[a.dataset.arbyLink]; if(!def) return;
      a.textContent = def.chip; a.href = def.url(); a.title = def.label();
    });
  }
  buildChips();

  function scrollToCard(re, failMsg){
    var hs = Array.prototype.slice.call(document.querySelectorAll('h2'));
    var h = hs.find(function(x){ return re.test(x.textContent); });
    var c = h && h.closest ? h.closest('.card') : null;
    if (c) c.scrollIntoView({behavior:'smooth', block:'start'});
    else if (failMsg) say(failMsg);
  }

  // ------------------------------------------------------------- mood model
  var MOODS = [
    { key:'elated',  min:0.55,  color:0x3fb950, label:'ON FIRE',     pill:'#12261a',
      quips:['Nailed it.','We are SO back.','Green looks good on us.','Edge found, edge pressed.','This is why we journal.'] },
    { key:'happy',   min:0.18,  color:0x56d364, label:'GREEN DAY',   pill:'#12261a',
      quips:['Plan\u2019s working.','Steady hands.','Small wins compound.','Discipline pays today.'] },
    { key:'neutral', min:-0.18, color:0x58a6ff, label:'NEUTRAL',     pill:'#132435',
      quips:['Watching the tape.','No edge, no trade.','Patience is a position.','Waiting for my pitch.'] },
    { key:'annoyed', min:-0.55, color:0xd29922, label:'ROUGH SPOT',  pill:'#2b210c',
      quips:['Stopped out. It happens.','Cutting size till it clears.','Back to the checklist.','Respect the stop, always.'] },
    { key:'tilted',  min:-1.01, color:0xf85149, label:'COLD STREAK', pill:'#2b1414',
      quips:['Rough stretch. Sitting on hands.','Risk off. Head down.','The playbook says: step back.','Drawdowns teach. I\u2019m listening.'] },
  ];
  function moodFor(score){ for (var i=0;i<MOODS.length;i++){ if (score>=MOODS[i].min) return MOODS[i]; } return MOODS[2]; }
  function refreshHud(){
    var m = moodFor(state.moodScore);
    hudMood.textContent = m.label;
    hudMood.style.background = m.pill;
    hudMood.style.color = '#'+m.color.toString(16).padStart(6,'0');
  }
  refreshHud();

  function streakInfo(d){
    var s = d && (d.streak_detail || d.streak);
    if (s && typeof s === 'object') return { kind: s.kind||'none', length: s.length||0 };
    if (typeof s === 'string'){
      var mm = s.match(/(\d+)\s*(win|los)/i);
      if (mm) return { kind: /win/i.test(mm[2])?'winning':'losing', length: parseInt(mm[1],10) };
    }
    return { kind:'none', length:0 };
  }
  function computeMood(d){
    var o = (d && d.overall) || {};
    var trig = o.triggered || 0;
    var conf = Math.min(1, trig/5);                       // fresh ledger -> stay neutral
    var net = (o.win_rate!=null) ? (o.win_rate-0.5)*2 : 0;
    var st = streakInfo(d);
    var stk = st.kind==='winning' ? Math.min(1, st.length/4)
            : st.kind==='losing'  ? -Math.min(1, st.length/4) : 0;
    var rs = (o.avg_r!=null) ? clamp(o.avg_r/1.5, -1, 1) : 0;
    return clamp((0.35*net + 0.4*stk + 0.25*rs) * conf, -1, 1);
  }

  // ------------------------------------------------------------- quips
  function setupInsight(){
    // Pick a real, templated line off by_setup stats \u2014 no LLM call, just the numbers
    // already synced from /api/status, e.g. "Breakout is lagging -- 31% over 6 triggered."
    var arr = (state.bySetup||[]).filter(function(s){ return s.triggered>=3 && s.win_rate!=null; });
    if (!arr.length) return null;
    arr = arr.slice().sort(function(a,b){ return a.win_rate-b.win_rate; });
    var worst = arr[0], best = arr[arr.length-1];
    var goWorst = worst.win_rate<0.45 && Math.random()<0.6;
    var pick = goWorst ? worst : best;
    var rTxt = pick.total_weighted_r!=null ? ', '+(pick.total_weighted_r>=0?'+':'')+pick.total_weighted_r+'R' : '';
    if (goWorst){
      var bullet = state.bullets[0] ? ' Playbook: '+state.bullets[0]+'.' : '';
      return (pick.dim||'That setup')+' is lagging \u2014 '+Math.round(pick.win_rate*100)+'% over '+pick.triggered+' triggered'+rTxt+'.'+bullet;
    }
    return (pick.dim||'That setup')+' is the edge right now \u2014 '+Math.round(pick.win_rate*100)+'% over '+pick.triggered+' triggered'+rTxt+'.';
  }
  function factQuips(){
    var d = state.status, out = [];
    if (!d) return ['First sync pending\u2026'];
    var o = d.overall || {};
    if (o.win_rate!=null && o.triggered) out.push('Win rate '+Math.round(o.win_rate*100)+'% across '+o.triggered+' triggered.');
    if (o.total_weighted_r!=null) out.push('Running total: '+(o.total_weighted_r>=0?'+':'')+o.total_weighted_r+'R weighted.');
    if (d.equity) out.push('Paper equity: $'+Math.round(d.equity).toLocaleString()+'.');
    var st = streakInfo(d);
    if (st.kind==='winning' && st.length>1) out.push(st.length+' wins in a row. Stay humble.');
    if (st.kind==='losing' && st.length>1) out.push(st.length+' losses straight. Size down, zoom out.');
    if (state.playbookVersion!=null) out.push('Playbook v'+state.playbookVersion+' in force.');
    var si = setupInsight();
    if (si) out.push(si);
    var plan = d.plan;
    if (plan){
      if (plan.no_trade) out.push('No trade today \u2014 '+String(plan.no_trade_reason||'no edge').slice(0,70));
      else (plan.picks||[]).slice(0,3).forEach(function(p){
        out.push('Eyes on '+p.ticker+' '+(p.direction||'')+' @ '+p.entry_price+' ('+(p.size_r||1)+'R).');
      });
    }
    var runs = d.runs;
    if (runs && runs.length) out.push('Last run: '+runs[0].loop+' \u00B7 '+runs[0].status+' \u00B7 $'+((runs[0].cost_usd||0)).toFixed(2)+'.');
    return out.length ? out : ['Quiet tape. Reading the playbook.'];
  }

  // ------------------------------------------------------------- bubble
  var bubbleTimeout = null;
  var sayAnchored = null; // set by 3D init to project from head; lite mode centers it
  function say(text, dur){
    bubble.textContent = text;
    if (sayAnchored) sayAnchored();
    else { bubble.style.left='50%'; bubble.style.top='30%'; }
    bubble.classList.add('show');
    clearTimeout(bubbleTimeout);
    bubbleTimeout = setTimeout(function(){ bubble.classList.remove('show'); }, dur||2600);
  }

  // ------------------------------------------------------------- hide/show
  var hidden = false;
  try { hidden = localStorage.getItem('arby.hidden')==='1'; } catch(e){}
  function applyHidden(){
    body.style.display = hidden ? 'none' : '';
    toggleBtn.textContent = hidden ? 'Show' : 'Hide';
  }
  toggleBtn.addEventListener('click', function(){
    hidden = !hidden;
    try { localStorage.setItem('arby.hidden', hidden?'1':'0'); } catch(e){}
    applyHidden();
    if (!hidden && typeof sizeCanvas === 'function') sizeCanvas();
  });
  applyHidden();

  // ------------------------------------------------------------- data sync (shared)
  var applyStatus3D = null; // wired by 3D init
  function applyStatus(d){
    if (!d || d.error) return;
    state.status = d;
    state.playbookVersion = d.playbook_version!=null ? d.playbook_version : state.playbookVersion;
    // main/side tickers from picks
    var picks = d.plan && !d.plan.no_trade ? (d.plan.picks||[]) : [];
    var slots = ['main','left','right'];
    picks.slice(0,3).forEach(function(p,i){ if (p.ticker) state.slotTickers[slots[i]] = p.ticker.toUpperCase(); });
    // playbook bullets
    var bl = d.playbook_bullets || extractBullets(d.playbook);
    if (bl && bl.length) state.bullets = bl;
    state.bySetup = d.by_setup || [];
    // mood + reactions to newly graded trades
    var prevGraded = state.lastGraded, prevTotal = state.lastTotalR;
    var o = d.overall || {};
    state.moodScore = computeMood(d);
    refreshHud(); refreshChips();
    if (applyStatus3D) applyStatus3D(d);
    if (prevGraded!=null && o.picks_graded!=null && o.picks_graded > prevGraded){
      var delta = (o.total_weighted_r||0) - (prevTotal||0);
      if (window.Mascot){ if (delta>=0) window.Mascot.registerWin(true); else window.Mascot.registerLoss(true); }
    }
    state.lastGraded = o.picks_graded!=null ? o.picks_graded : state.lastGraded;
    state.lastTotalR = o.total_weighted_r!=null ? o.total_weighted_r : state.lastTotalR;
  }
  function extractBullets(text){
    if (!text) return null;
    return String(text).split('\n')
      .filter(function(l){ return /^\s*[-*\u2022]\s+/.test(l); })
      .map(function(l){ return l.replace(/^\s*[-*\u2022]\s+/,'').replace(/\*\*/g,'').trim(); })
      .filter(function(l){ return l.length>3 && !/^\[/.test(l); })
      .slice(0,4)
      .map(function(l){ return l.length>36 ? l.slice(0,35)+'\u2026' : l; });
  }
  var syncBusy = false;
  function syncLive(){
    if (syncBusy) return;
    syncBusy = true;
    fetch('/api/status', {credentials:'same-origin'})
      .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
      .then(applyStatus)
      .catch(function(){ /* static file / logged out / offline: boot data already applied */ })
      .then(function(){ syncBusy = false; });
  }
  if (window.MASCOT_BOOT) applyStatus(window.MASCOT_BOOT);
  syncLive();
  setInterval(syncLive, 60000);
  document.addEventListener('visibilitychange', function(){ if (!document.hidden) syncLive(); });

  // ------------------------------------------------------------- WebGL guard
  var renderer = null;
  if (window.THREE){
    try { renderer = new THREE.WebGLRenderer({ antialias:true }); } catch(e){ renderer = null; }
  }
  if (!renderer){
    wrap.innerHTML = '<div id="arby-fallback">Arby needs WebGL (or the Three.js CDN) to appear \u2014 ' +
      'the live-data links below still work.</div>';
    var needsEl = document.getElementById('arby-needs');
    if (needsEl) needsEl.style.display = 'none'; // motive bars are tied to the 3D walk-around loop
    if (fsBtn) fsBtn.style.display = 'none'; // nothing to fullscreen without the 3D room
    hudActivity.textContent = 'off duty (no WebGL)';
    // No 3D animation to react with, but the mood pill + bubble still carry a real reaction.
    window.Mascot = window.Arby = {
      registerWin: function(fromData){
        state.moodScore=clamp(state.moodScore+0.25,-1,1); refreshHud();
        if (fromData) say(['BOOKED. +R in the ledger.','That\u2019s the system working.','Graded green. On to the next.'][Math.floor(Math.random()*3)]);
      },
      registerLoss: function(fromData){
        state.moodScore=clamp(state.moodScore-0.25,-1,1); refreshHud();
        if (fromData) say(['Stopped. Loss logged, lesson pending.','Paid tuition. Moving on.','That one\u2019s going in the journal.'][Math.floor(Math.random()*3)]);
      },
      reset: function(){ state.moodScore=0; refreshHud(); },
      setBlendedMood: function(s){ state.moodScore=clamp(s,-1,1); refreshHud(); },
      syncFromStatus: applyStatus, tryLiveSync: syncLive,
      setChartData:function(){}, setChartFromPicks:function(){}, setNoTrade:function(){},
      say: say, goTo: function(){},
    };
    return;
  }

  // ============================================================= 3D WORLD
  var RW = 480, RH = 300;
  var scene = new THREE.Scene();
  scene.background = new THREE.Color(0x161b22); // blend into the dashboard card
  var camera = new THREE.PerspectiveCamera(42, RW/RH, 0.1, 100);
  // Camera dollies toward/away from a fixed look-at point for scroll/pinch zoom;
  // direction + base distance are derived once from the initial framing so the
  // "breathing" idle sway and zoom compose cleanly (see tick()'s render tail).
  var cameraTarget = new THREE.Vector3(0, 0.85, -0.5);
  var camInitPos = new THREE.Vector3(3.25, 2.3, 3.55);
  var camDir = camInitPos.clone().sub(cameraTarget).normalize();
  var camBaseDist = camInitPos.distanceTo(cameraTarget);
  var camDist = camBaseDist;
  var CAM_MIN = camBaseDist * 0.55;
  var CAM_MAX = camBaseDist * 1.7;
  camera.position.copy(camInitPos);
  camera.lookAt(cameraTarget);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio||1, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  wrap.insertBefore(renderer.domElement, bubble);

  function fsElement(){ return document.fullscreenElement || document.webkitFullscreenElement || null; }
  function sizeCanvas(){
    var fs = fsElement() === wrap;
    var w, h;
    if (fs){
      w = wrap.clientWidth || window.innerWidth;
      h = wrap.clientHeight || window.innerHeight;
    } else {
      w = Math.min(wrap.clientWidth || 480, 1000);
      if (w < 50) w = 480;
      h = Math.round(clamp(w*0.6, 300, 560));
    }
    RW = w; RH = h;
    renderer.setSize(w, h);
    camera.aspect = w/h;
    camera.updateProjectionMatrix();
  }
  sizeCanvas();
  if (window.ResizeObserver) new ResizeObserver(sizeCanvas).observe(wrap);
  else window.addEventListener('resize', sizeCanvas);

  // Fullscreen toggle: expands #arby-wrap itself via the Fullscreen API (with
  // the -webkit- prefix for older Safari), so bubble/tip/hint stay correctly
  // positioned since they're already absolute-positioned relative to it.
  if (fsBtn){
    if (!(wrap.requestFullscreen || wrap.webkitRequestFullscreen)) fsBtn.style.display = 'none';
    fsBtn.addEventListener('click', function(e){
      e.stopPropagation();
      if (fsElement() === wrap){
        (document.exitFullscreen || document.webkitExitFullscreen).call(document);
      } else {
        (wrap.requestFullscreen || wrap.webkitRequestFullscreen).call(wrap);
        wrap.focus();
      }
    });
    var syncFsBtn = function(){
      var on = fsElement() === wrap;
      fsBtn.innerHTML = on ? '&#10005;' : '&#9974;';
      fsBtn.title = on ? 'Exit fullscreen' : 'Fullscreen';
      sizeCanvas();
    };
    document.addEventListener('fullscreenchange', syncFsBtn);
    document.addEventListener('webkitfullscreenchange', syncFsBtn);
  }

  // Scroll-wheel zoom (desktop) — multiplicative so both mouse wheels (large,
  // discrete deltaY) and trackpads (small, continuous deltaY) feel proportional.
  wrap.addEventListener('wheel', function(e){
    e.preventDefault();
    camDist = clamp(camDist * Math.exp(e.deltaY * 0.0016), CAM_MIN, CAM_MAX);
  }, { passive:false });

  // Sims-style orbit + pan, layered on the existing dolly zoom above:
  //   - left-drag (or one-finger drag) orbits the camera around cameraTarget
  //   - right-drag pans cameraTarget across the floor (clamped to the room)
  //   - two-finger drag also pans, alongside the existing pinch-to-zoom
  // camDir stays a live unit vector recomputed from yaw/pitch, so the
  // existing tick()-time dolly math (cameraTarget + camDir*camDist) needs no
  // changes at all — orbiting just steers where that vector points.
  var camYaw = Math.atan2(camDir.x, camDir.z);
  var camPitch = Math.atan2(camDir.y, Math.hypot(camDir.x, camDir.z));
  var camYaw0 = camYaw, camPitch0 = camPitch;
  var cameraTarget0 = cameraTarget.clone();
  var PITCH_MIN = 0.12, PITCH_MAX = 1.45;
  function camDirFromAngles(){
    var cp = Math.cos(camPitch);
    camDir.set(Math.sin(camYaw)*cp, Math.sin(camPitch), Math.cos(camYaw)*cp);
  }
  function panCamera(dx, dy){
    var scale = camDist * 0.0018;
    var right = Math.cos(camYaw), rightZ = -Math.sin(camYaw);
    var fwd = Math.sin(camYaw), fwdZ = Math.cos(camYaw);
    cameraTarget.x = clamp(cameraTarget.x - right*dx*scale - fwd*dy*scale, -(ROOM_X-0.4), ROOM_X-0.4);
    cameraTarget.z = clamp(cameraTarget.z - rightZ*dx*scale - fwdZ*dy*scale, -(ROOM_Z-0.4), ROOM_Z+0.6);
  }
  function resetCamera(){
    camDist = camBaseDist; camYaw = camYaw0; camPitch = camPitch0;
    camDirFromAngles(); cameraTarget.copy(cameraTarget0);
  }

  // Pinch-to-zoom (touch): track the two active pointers' on-screen distance.
  // A single active pointer instead drives orbit/pan (see dragButton below).
  var pinchPointers = {}, pinchDist = 0;
  var dragMoved = false, dragButton = 0;
  renderer.domElement.addEventListener('pointerdown', function(e){
    pinchPointers[e.pointerId] = {x:e.clientX, y:e.clientY};
    var ids = Object.keys(pinchPointers);
    if (ids.length===2){
      var a=pinchPointers[ids[0]], b=pinchPointers[ids[1]];
      pinchDist = Math.hypot(b.x-a.x, b.y-a.y);
    } else if (ids.length===1){
      dragMoved = false; dragButton = e.button;
    }
  });
  renderer.domElement.addEventListener('pointermove', function(e){
    if (!pinchPointers[e.pointerId]) return;
    var prev = pinchPointers[e.pointerId];
    var dx = e.clientX - prev.x, dy = e.clientY - prev.y;
    pinchPointers[e.pointerId] = {x:e.clientX, y:e.clientY};
    var ids = Object.keys(pinchPointers);
    if (ids.length===2){
      var a=pinchPointers[ids[0]], b=pinchPointers[ids[1]];
      var d = Math.hypot(b.x-a.x, b.y-a.y);
      if (pinchDist>10) camDist = clamp(camDist*(pinchDist/d), CAM_MIN, CAM_MAX);
      pinchDist = d;
      panCamera(dx, dy); // two-finger drag pans
    } else if (ids.length===1){
      if (Math.abs(dx)>3 || Math.abs(dy)>3) dragMoved = true;
      if (dragButton===2) panCamera(dx, dy);
      else {
        camYaw -= dx*0.0055;
        camPitch = clamp(camPitch - dy*0.0045, PITCH_MIN, PITCH_MAX);
        camDirFromAngles();
      }
    }
  });
  function endPinchPointer(e){ delete pinchPointers[e.pointerId]; }
  renderer.domElement.addEventListener('pointerup', endPinchPointer);
  renderer.domElement.addEventListener('pointercancel', endPinchPointer);
  // Right-drag pans instead of opening the browser context menu.
  renderer.domElement.addEventListener('contextmenu', function(e){ e.preventDefault(); });
  // Double-click/tap resets to the default framing (position, look direction, and pan).
  renderer.domElement.addEventListener('dblclick', resetCamera);

  var hemi = new THREE.HemisphereLight(0xffffff, 0xcfc2a4, 0.85);
  scene.add(hemi);
  var amb = new THREE.AmbientLight(0xffffff, 0.28);
  scene.add(amb);
  var dirLight = new THREE.DirectionalLight(0xfff2e0, 0.75);
  dirLight.position.set(2.5, 4.5, 2.5);
  dirLight.castShadow = true;
  dirLight.shadow.mapSize.set(1024, 1024);
  dirLight.shadow.camera.left=-3; dirLight.shadow.camera.right=3;
  dirLight.shadow.camera.top=3; dirLight.shadow.camera.bottom=-3;
  dirLight.shadow.camera.near=0.5; dirLight.shadow.camera.far=12;
  dirLight.shadow.bias=-0.0015;
  scene.add(dirLight);

  function shadowify(obj){ obj.traverse(function(o){ if(o.isMesh){ o.castShadow=true; o.receiveShadow=true; } }); }

  // ---------------------------------------------------------- canvas helpers
  function mkCanvas(w,h){ var c=document.createElement('canvas'); c.width=w; c.height=h; return c; }
  function seeded(seed){
    var s = seed % 2147483647; if (s<=0) s += 2147483646;
    return function(){ s = (s*16807) % 2147483647; return (s-1)/2147483646; };
  }
  function hashStr(str){ var h=0; for (var i=0;i<str.length;i++){ h=(h*31+str.charCodeAt(i))|0; } return Math.abs(h)||1; }
  function wrapLines(ctx, text, maxW){
    var words = String(text).split(/\s+/), lines=[], cur='';
    words.forEach(function(w){
      var t = cur ? cur+' '+w : w;
      if (ctx.measureText(t).width > maxW && cur){ lines.push(cur); cur=w; } else cur=t;
    });
    if (cur) lines.push(cur);
    return lines;
  }

  function demoBars(ticker, n){
    var rand = seeded(hashStr(ticker||'TICK'));
    var price = 100, bars = [];
    for (var i=0;i<n;i++){
      var o=price, c=price+(rand()-0.48)*3.2;
      var hi=Math.max(o,c)+rand()*1.4, lo=Math.min(o,c)-rand()*1.4;
      bars.push({o:o,h:hi,l:lo,c:c});
      price=c;
    }
    return bars;
  }
  function synthPickBars(pick, n){
    var entry=+pick.entry_price, stop=+pick.stop_price, target=+pick.target_price;
    var lo=Math.min(entry,stop,target), hi=Math.max(entry,stop,target);
    var span=(hi-lo)||Math.abs(entry)*0.02||1;
    var rand = seeded(hashStr((pick.ticker||'T')+String(entry)));
    var p=entry, bars=[];
    var drift = (target>=entry?1:-1)*span*0.006;
    for (var i=0;i<n;i++){
      var o=p, c=clamp(p+(rand()-0.5)*span*0.11+drift, lo-span*0.04, hi+span*0.04);
      var bh=Math.max(o,c)+rand()*span*0.035, bl=Math.min(o,c)-rand()*span*0.035;
      bars.push({o:o,h:bh,l:bl,c:c});
      p=c;
    }
    return bars;
  }

  // ------------------------------------------------ indicator overlays (VWAP/9EMA)
  // Client-side only, mirroring agent/indicators.py's math (no shared code --
  // this is a decorative canvas widget, not a graded computation). Bars
  // without volume (older cached data, some synthetic fallbacks) fall back to
  // an unweighted (v=1) average, which degrades gracefully to a plain
  // typical-price line rather than crashing or omitting the overlay.
  function computeVWAP(rawBars){
    var cumPV=0, cumV=0, out=[];
    rawBars.forEach(function(b){
      var h=b.h!=null?b.h:b.high, l=b.l!=null?b.l:b.low, c=b.c!=null?b.c:b.close;
      var typical=(h+l+c)/3;
      var v=(b.v!=null && b.v>0) ? b.v : 1;
      cumPV += typical*v; cumV += v;
      out.push(cumV>0 ? cumPV/cumV : c);
    });
    return out;
  }
  function computeEMA(closes, period){
    var n=closes.length, out=new Array(n).fill(null);
    if (n<period) return out;
    var seed=0; for (var i=0;i<period;i++) seed+=closes[i]; seed/=period;
    out[period-1]=seed;
    var k=2/(period+1), prev=seed;
    for (var j=period;j<n;j++){ prev = closes[j]*k + prev*(1-k); out[j]=prev; }
    return out;
  }
  function drawIndicatorLine(ctx, values, slotW, Y, color){
    ctx.strokeStyle=color; ctx.lineWidth=1.3; ctx.beginPath();
    var started=false;
    for (var j=0;j<values.length;j++){
      var v=values[j];
      if (v==null) continue;
      var cx = 8 + j*slotW + slotW/2, cy=Y(v);
      if (!started){ ctx.moveTo(cx,cy); started=true; } else { ctx.lineTo(cx,cy); }
    }
    if (started) ctx.stroke();
  }

  function drawChart(ctx, w, h, opts){
    var ticker=opts.ticker||'', bars=opts.bars||[], levels=opts.levels||null;
    ctx.clearRect(0,0,w,h);
    ctx.fillStyle='#0d1117'; ctx.fillRect(0,0,w,h);
    ctx.strokeStyle='#20262e'; ctx.lineWidth=1;
    for (var i=1;i<6;i++){ ctx.beginPath(); ctx.moveTo(0,i*h/6); ctx.lineTo(w,i*h/6); ctx.stroke(); }
    var norm = bars.map(function(b){ return {o:b.o!=null?b.o:b.open, h:b.h!=null?b.h:b.high,
                                             l:b.l!=null?b.l:b.low, c:b.c!=null?b.c:b.close}; });
    var lo=Infinity, hi=-Infinity;
    norm.forEach(function(b){ lo=Math.min(lo,b.l); hi=Math.max(hi,b.h); });
    if (levels){ ['entry','stop','target'].forEach(function(k){
      if (levels[k]!=null){ lo=Math.min(lo,+levels[k]); hi=Math.max(hi,+levels[k]); } }); }
    if (!isFinite(lo)){ lo=0; hi=1; }
    if (hi===lo){ hi+=1; lo-=1; }
    var padT=20, padB=16, ch=h-padT-padB;
    var Y = function(v){ return padT + ch*(1-(v-lo)/(hi-lo)); };
    if (norm.length){
      var slotW = Math.max(4,(w-16)/norm.length), bodyW = Math.max(2, slotW*0.6);
      norm.forEach(function(b,j){
        var cx = 8 + j*slotW + slotW/2;
        var col = b.c>=b.o ? '#3fb950' : '#f85149';
        ctx.strokeStyle=col; ctx.fillStyle=col;
        ctx.beginPath(); ctx.moveTo(cx,Y(b.h)); ctx.lineTo(cx,Y(b.l)); ctx.stroke();
        ctx.fillRect(cx-bodyW/2, Math.min(Y(b.o),Y(b.c)), bodyW, Math.max(1.5, Math.abs(Y(b.c)-Y(b.o))));
      });
      drawIndicatorLine(ctx, computeVWAP(bars), slotW, Y, '#d29922');
      if (norm.length >= 9){
        drawIndicatorLine(ctx, computeEMA(norm.map(function(b){ return b.c; }), 9), slotW, Y, '#a371f7');
      }
    }
    if (levels){
      var hline = function(v,color,tag){
        if (v==null) return;
        var yy=Y(+v);
        ctx.strokeStyle=color; ctx.setLineDash([4,3]);
        ctx.beginPath(); ctx.moveTo(2,yy); ctx.lineTo(w-2,yy); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle=color; ctx.font='9px Consolas,monospace';
        ctx.fillText(tag+' '+(+v), w-64, clamp(yy-3, 10, h-4));
      };
      hline(levels.target,'#3fb950','T');
      hline(levels.entry,'#58a6ff','E');
      hline(levels.stop,'#f85149','S');
    }
    var dirWord = levels && levels.direction==='short' ? ' SHORT' : (levels ? ' LONG' : '');
    ctx.fillStyle = levels ? (levels.direction==='short' ? '#f85149' : '#3fb950') : '#58a6ff';
    ctx.font='bold 12px Consolas,monospace';
    ctx.fillText(ticker+dirWord, 8, 14);
    ctx.font='10px Consolas,monospace';
    if (levels && levels.size_r!=null){
      ctx.fillStyle='#8b949e';
      ctx.fillText(levels.size_r+'R \u00B7 conv '+(levels.conviction!=null?levels.conviction:'?'), 8, h-5);
    } else if (norm.length){
      var first=norm[0].o, last=norm[norm.length-1].c;
      var pct=((last-first)/first*100).toFixed(2);
      ctx.fillStyle = last>=first ? '#3fb950' : '#f85149';
      ctx.fillText((last>=first?'+':'')+pct+'%', 8, h-5);
    }
  }
  function drawNoTrade(ctx, w, h, reason){
    ctx.fillStyle='#0d1117'; ctx.fillRect(0,0,w,h);
    ctx.strokeStyle='#20262e';
    for (var i=1;i<6;i++){ ctx.beginPath(); ctx.moveTo(0,i*h/6); ctx.lineTo(w,i*h/6); ctx.stroke(); }
    ctx.fillStyle='#d29922'; ctx.font='bold 15px Consolas,monospace';
    ctx.fillText('\u26A0 NO TRADE TODAY', 12, 34);
    ctx.fillStyle='#8b949e'; ctx.font='10px sans-serif';
    wrapLines(ctx, reason||'No edge identified.', w-24).slice(0,5).forEach(function(l,j){
      ctx.fillText(l, 12, 56+j*14);
    });
  }
  function drawEquity(ctx, w, h, curve){
    ctx.fillStyle='#0d1117'; ctx.fillRect(0,0,w,h);
    ctx.strokeStyle='#20262e';
    for (var i=1;i<5;i++){ ctx.beginPath(); ctx.moveTo(0,i*h/5); ctx.lineTo(w,i*h/5); ctx.stroke(); }
    ctx.fillStyle='#58a6ff'; ctx.font='bold 12px Consolas,monospace';
    ctx.fillText('EQUITY CURVE', 10, 16);
    if (!curve || curve.length<2){
      ctx.fillStyle='#8b949e'; ctx.font='10px sans-serif';
      ctx.fillText('Not enough graded days yet.', 10, h/2);
      return;
    }
    var ys = curve.map(function(p){ return p.equity; });
    var lo=Math.min.apply(null,ys), hi=Math.max.apply(null,ys);
    if (hi===lo){ hi+=1; lo-=1; }
    var padT=24, padB=20, padX=10, chH=h-padT-padB;
    var up = ys[ys.length-1] >= ys[0];
    var col = up ? '#3fb950' : '#f85149';
    ctx.beginPath();
    ys.forEach(function(v,j){
      var x = padX + (w-2*padX)*j/(ys.length-1);
      var y = padT + chH*(1-(v-lo)/(hi-lo));
      if (j===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
    });
    ctx.strokeStyle=col; ctx.lineWidth=2; ctx.stroke();
    ctx.lineTo(w-padX, h-padB); ctx.lineTo(padX, h-padB); ctx.closePath();
    ctx.fillStyle = up ? 'rgba(63,185,80,.12)' : 'rgba(248,81,73,.12)';
    ctx.fill();
    var last = curve[curve.length-1];
    ctx.fillStyle=col; ctx.font='bold 11px Consolas,monospace';
    ctx.fillText('$'+Math.round(last.equity).toLocaleString()+'  ('+(last.cum_r>=0?'+':'')+last.cum_r+'R)', 10, h-6);
  }

  // ---------------------------------------------------------- room shell
  // The walkable room fills the space out to ROOM_X/ROOM_Z; the cosmetic
  // floor/wall/ceiling meshes below are sized generously larger still (fixed
  // 16x15), so there's always headroom past the camera's max zoom-out
  // distance and no dark "void" ever shows in the corners of the frame.
  var ROOM_X=3.6, ROOM_Z=3.3;
  var WALL_H=5.2;
  var floor = new THREE.Mesh(new THREE.PlaneGeometry(16,15),
    new THREE.MeshStandardMaterial({color:0xdcb98a}));
  floor.rotation.x=-Math.PI/2; floor.receiveShadow=true; scene.add(floor);
  var rug = new THREE.Mesh(new THREE.CircleGeometry(1.2,32),
    new THREE.MeshStandardMaterial({color:0xb7d2e8}));
  rug.rotation.x=-Math.PI/2; rug.position.set(-0.1,0.004,0.7); rug.receiveShadow=true; scene.add(rug);

  var wallMat = new THREE.MeshStandardMaterial({color:0xf3efe4});
  var backWall = new THREE.Mesh(new THREE.PlaneGeometry(ROOM_X*2+9,WALL_H), wallMat);
  backWall.position.set(0,WALL_H/2,-ROOM_Z); backWall.receiveShadow=true; scene.add(backWall);
  var leftWall = new THREE.Mesh(new THREE.PlaneGeometry(ROOM_Z*2+9,WALL_H), wallMat);
  leftWall.rotation.y=Math.PI/2; leftWall.position.set(-ROOM_X,WALL_H/2,0); leftWall.receiveShadow=true; scene.add(leftWall);
  var rightWall = new THREE.Mesh(new THREE.PlaneGeometry(ROOM_Z*2+9,WALL_H), wallMat);
  rightWall.rotation.y=-Math.PI/2; rightWall.position.set(ROOM_X,WALL_H/2,0); rightWall.receiveShadow=true; scene.add(rightWall);
  var ceiling = new THREE.Mesh(new THREE.PlaneGeometry(16,15),
    new THREE.MeshStandardMaterial({color:0xf3efe4, side:THREE.DoubleSide}));
  ceiling.rotation.x=Math.PI/2; ceiling.position.y=WALL_H; scene.add(ceiling);

  var baseMat = new THREE.MeshStandardMaterial({color:0xffffff});
  [[0,0,-ROOM_Z+0.02,0],[-ROOM_X+0.02,0,0,Math.PI/2],[ROOM_X-0.02,0,0,-Math.PI/2]].forEach(function(bp){
    var len = bp[3]===0 ? ROOM_X*2 : ROOM_Z*2;
    var bb = new THREE.Mesh(new THREE.BoxGeometry(len,0.09,0.04), baseMat);
    bb.rotation.y=bp[3]; bb.position.set(bp[0],0.045,bp[2]); scene.add(bb);
  });

  var shadowBlobTex = new THREE.CanvasTexture((function(){
    var c=mkCanvas(128,128), x=c.getContext('2d');
    var g=x.createRadialGradient(64,64,0,64,64,64);
    g.addColorStop(0,'rgba(20,20,25,0.38)'); g.addColorStop(1,'rgba(20,20,25,0)');
    x.fillStyle=g; x.fillRect(0,0,128,128);
    return c;
  })());
  function addBlob(x,z,scale){
    var b=new THREE.Mesh(new THREE.CircleGeometry(0.35,24),
      new THREE.MeshBasicMaterial({map:shadowBlobTex, transparent:true, depthWrite:false}));
    b.rotation.x=-Math.PI/2; b.position.set(x,0.012,z); b.scale.set(scale,scale,1);
    scene.add(b);
    return b;
  }

  // window + dynamic sky
  var windowG = new THREE.Group();
  var windowFrame = new THREE.Mesh(new THREE.BoxGeometry(0.06,1.3,1.3),
    new THREE.MeshStandardMaterial({color:0xffffff}));
  windowFrame.position.set(-ROOM_X+0.03,1.55,-0.5); windowG.add(windowFrame);
  var skyCv = mkCanvas(200,200), skyCtx = skyCv.getContext('2d');
  var skyTex = new THREE.CanvasTexture(skyCv);
  var windowGlow = new THREE.Mesh(new THREE.PlaneGeometry(1.15,1.15),
    new THREE.MeshBasicMaterial({map:skyTex}));
  windowGlow.rotation.y=Math.PI/2; windowGlow.position.set(-ROOM_X+0.07,1.55,-0.5); windowG.add(windowGlow);
  shadowify(windowG); scene.add(windowG);
  var lastPhase = null;
  function drawSky(phase){
    var g = skyCtx.createLinearGradient(0,0,0,200);
    if (phase==='night'){ g.addColorStop(0,'#0b1026'); g.addColorStop(1,'#1a2340'); }
    else if (phase==='dawn'){ g.addColorStop(0,'#3f5f9e'); g.addColorStop(0.6,'#e08f62'); g.addColorStop(1,'#f4c27a'); }
    else if (phase==='dusk'){ g.addColorStop(0,'#4a3b78'); g.addColorStop(0.6,'#c96b52'); g.addColorStop(1,'#f0a35e'); }
    else { g.addColorStop(0,'#8fd0f8'); g.addColorStop(1,'#d9f0ff'); }
    skyCtx.fillStyle=g; skyCtx.fillRect(0,0,200,200);
    if (phase==='night'){
      skyCtx.fillStyle='#f5f3ce';
      skyCtx.beginPath(); skyCtx.arc(140,55,18,0,Math.PI*2); skyCtx.fill();
      skyCtx.fillStyle='#0b1026';
      skyCtx.beginPath(); skyCtx.arc(147,50,16,0,Math.PI*2); skyCtx.fill();
      var rand = seeded(42);
      skyCtx.fillStyle='rgba(255,255,255,.9)';
      for (var i=0;i<26;i++){ skyCtx.fillRect(rand()*200, rand()*160, 1.6, 1.6); }
    } else if (phase==='day'){
      skyCtx.fillStyle='rgba(255,255,255,.85)';
      skyCtx.beginPath(); skyCtx.ellipse(60,60,22,12,0,0,Math.PI*2); skyCtx.fill();
      skyCtx.beginPath(); skyCtx.ellipse(140,90,26,14,0,0,Math.PI*2); skyCtx.fill();
      skyCtx.fillStyle='#ffe07a';
      skyCtx.beginPath(); skyCtx.arc(160,35,14,0,Math.PI*2); skyCtx.fill();
    } else {
      skyCtx.fillStyle='rgba(255,230,180,.8)';
      skyCtx.beginPath(); skyCtx.arc(60,140,12,0,Math.PI*2); skyCtx.fill();
    }
    skyTex.needsUpdate = true;
  }
  function applyPhaseLighting(phase){
    if (phase==='night'){ hemi.intensity=0.45; amb.intensity=0.16; dirLight.intensity=0.25; }
    else if (phase==='dawn'||phase==='dusk'){ hemi.intensity=0.65; amb.intensity=0.22; dirLight.intensity=0.5; }
    else { hemi.intensity=0.85; amb.intensity=0.28; dirLight.intensity=0.75; }
    var lampOn = (phase==='night' || phase==='dusk');
    if (lampLight){ lampLight.intensity = lampOn ? 0.55 : 0; }
    if (lampHeadMat){ lampHeadMat.emissiveIntensity = lampOn ? 0.9 : 0; }
  }

  // whiteboard (live playbook rules)
  var wbCv = mkCanvas(256,180), wbCtx = wbCv.getContext('2d');
  var wbTex = new THREE.CanvasTexture(wbCv);
  function drawWhiteboard(){
    wbCtx.fillStyle='#fbfdff'; wbCtx.fillRect(0,0,256,180);
    wbCtx.strokeStyle='#cfe0f2'; wbCtx.lineWidth=1;
    for (var i=1;i<6;i++){ wbCtx.beginPath(); wbCtx.moveTo(0,i*30); wbCtx.lineTo(256,i*30); wbCtx.stroke(); }
    wbCtx.fillStyle='#1b3a5c'; wbCtx.font='bold 15px sans-serif';
    wbCtx.fillText('PLAYBOOK'+(state.playbookVersion!=null?' v'+state.playbookVersion:''), 12, 24);
    wbCtx.font='11px sans-serif'; wbCtx.fillStyle='#3a536b';
    state.bullets.slice(0,4).forEach(function(s,i){ wbCtx.fillText('\u2022 '+s, 14, 52+i*24); });
    wbTex.needsUpdate = true;
  }
  drawWhiteboard();
  var wbG = new THREE.Group();
  var whiteboard = new THREE.Mesh(new THREE.PlaneGeometry(1.0,0.72), new THREE.MeshBasicMaterial({map:wbTex}));
  whiteboard.position.set(-1.5,1.55,-ROOM_Z+0.03); wbG.add(whiteboard);
  var wbFrame = new THREE.Mesh(new THREE.BoxGeometry(1.08,0.8,0.03), new THREE.MeshStandardMaterial({color:0xffffff}));
  wbFrame.position.set(-1.5,1.55,-ROOM_Z+0.012); wbG.add(wbFrame);
  shadowify(wbG); scene.add(wbG);

  // equity poster (right wall)
  var postCv = mkCanvas(220,150), postCtx = postCv.getContext('2d');
  var postTex = new THREE.CanvasTexture(postCv);
  drawChart(postCtx,220,150,{ticker:'SPY', bars:demoBars('SPY',22)});
  postTex.needsUpdate = true;
  var posterMesh = new THREE.Mesh(new THREE.PlaneGeometry(0.85,0.6), new THREE.MeshBasicMaterial({map:postTex}));
  posterMesh.rotation.y=-Math.PI/2; posterMesh.position.set(ROOM_X-0.03,1.75,0.4); scene.add(posterMesh);

  // "RISK FIRST" poster (right wall, above the equity poster)
  var riskTex = new THREE.CanvasTexture((function(){
    var c=mkCanvas(180,120), x=c.getContext('2d');
    x.fillStyle='#101418'; x.fillRect(0,0,180,120);
    x.strokeStyle='#f85149'; x.lineWidth=4; x.strokeRect(8,8,164,104);
    x.fillStyle='#f85149'; x.font='bold 34px Consolas,monospace'; x.textAlign='center';
    x.fillText('RISK', 90, 52);
    x.fillStyle='#e6edf3'; x.font='bold 30px Consolas,monospace';
    x.fillText('FIRST', 90, 86);
    return c;
  })());
  var riskPoster = new THREE.Mesh(new THREE.PlaneGeometry(0.62,0.42), new THREE.MeshBasicMaterial({map:riskTex}));
  riskPoster.rotation.y=-Math.PI/2; riskPoster.position.set(ROOM_X-0.03,2.35,0.4); scene.add(riskPoster);

  // corkboard -> StockTwits
  var corkTex = new THREE.CanvasTexture((function(){
    var c=mkCanvas(160,160), x=c.getContext('2d');
    x.fillStyle='#d8c9a8'; x.fillRect(0,0,160,160);
    ['#ffe08a','#a8e6a1','#ffb3ab','#a9d3ff'].forEach(function(col,i){
      x.fillStyle=col; x.fillRect((i%2)*70+15, Math.floor(i/2)*70+15, 50, 50);
    });
    x.fillStyle='#4a3b28'; x.font='bold 11px sans-serif'; x.fillText('$SENTIMENT', 38, 84);
    return c;
  })());
  var corkMesh = new THREE.Mesh(new THREE.PlaneGeometry(0.65,0.65), new THREE.MeshBasicMaterial({map:corkTex}));
  corkMesh.rotation.y=-Math.PI/2; corkMesh.position.set(ROOM_X-0.03,0.95,0.4); scene.add(corkMesh);

  // wall calendar (left wall) -> econ calendar
  var calCv = mkCanvas(128,152), calCtx = calCv.getContext('2d');
  var calTex = new THREE.CanvasTexture(calCv);
  (function drawCalendar(){
    calCtx.fillStyle='#ffffff'; calCtx.fillRect(0,0,128,152);
    calCtx.fillStyle='#d6455a'; calCtx.fillRect(0,0,128,26);
    var now = new Date();
    var month = now.toLocaleString('en-US',{month:'short', timeZone:'America/New_York'}).toUpperCase();
    calCtx.fillStyle='#fff'; calCtx.font='bold 14px sans-serif';
    calCtx.fillText(month+' \u00B7 ECON', 22, 18);
    calCtx.strokeStyle='#d8dee6';
    var day = 1, today = parseInt(now.toLocaleString('en-US',{day:'numeric', timeZone:'America/New_York'}),10);
    for (var r=0;r<5;r++) for (var cc=0;cc<7;cc++){
      var x=4+cc*17.5, y=34+r*23;
      calCtx.strokeRect(x,y,16,20);
      if (day<=31){
        if (day===today){ calCtx.fillStyle='#d6455a'; calCtx.beginPath(); calCtx.arc(x+8,y+10,8,0,Math.PI*2); calCtx.fill(); calCtx.fillStyle='#fff'; }
        else calCtx.fillStyle='#57606a';
        calCtx.font='9px sans-serif'; calCtx.fillText(String(day), x+4, y+13);
        day++;
      }
    }
    calTex.needsUpdate=true;
  })();
  var calMesh = new THREE.Mesh(new THREE.PlaneGeometry(0.42,0.5), new THREE.MeshBasicMaterial({map:calTex}));
  calMesh.rotation.y=Math.PI/2; calMesh.position.set(-ROOM_X+0.03,1.5,-0.9); scene.add(calMesh);

  // wall TV — live ticker tape + headlines (back wall, above desk)
  var tvG = new THREE.Group();
  var tvFrame = new THREE.Mesh(new THREE.BoxGeometry(1.9,0.68,0.04), new THREE.MeshStandardMaterial({color:0x14161a}));
  tvFrame.position.set(1.3,2.0,-ROOM_Z+0.03); tvG.add(tvFrame);
  var tvCv = mkCanvas(512,144), tvCtx = tvCv.getContext('2d');
  var tvTex = new THREE.CanvasTexture(tvCv);
  var tvScreen = new THREE.Mesh(new THREE.PlaneGeometry(1.84,0.6), new THREE.MeshBasicMaterial({map:tvTex}));
  tvScreen.position.set(1.3,2.0,-ROOM_Z+0.055); tvG.add(tvScreen);
  shadowify(tvG); scene.add(tvG);
  var tapeSegs = [{t:'ARBY TV ONLINE \u2014 WAITING FOR FIRST SYNC', c:'#8b949e'}];
  var tapeW = 0;
  var TAPE_FONT = 'bold 30px Consolas,monospace';
  var lastTapeSig = null;
  function tapeSignature(d){
    if (!d) return 'boot';
    var o = d.overall||{}, st = streakInfo(d), plan = d.plan;
    var picksSig = plan && !plan.no_trade
      ? (plan.picks||[]).map(function(p){ return p.ticker+p.entry_price+p.target_price+p.direction; }).join(',')
      : (plan && plan.no_trade ? 'NT:'+plan.no_trade_reason : '');
    return [picksSig, o.win_rate, o.total_weighted_r, d.equity, st.kind, st.length, state.playbookVersion].join('|');
  }
  function rebuildTape(){
    var d = state.status;
    var sig = tapeSignature(d);
    if (sig === lastTapeSig) return; // nothing changed since the last sync \u2014 skip the full remeasure
    lastTapeSig = sig;
    var segs = [];
    var sep = { t:'  \u25C6  ', c:'#30363d' };
    function push(t,c){ segs.push({t:t,c:c}); segs.push(sep); }
    if (d){
      var plan = d.plan;
      if (plan && !plan.no_trade && (plan.picks||[]).length){
        plan.picks.forEach(function(p){
          var up = p.direction!=='short';
          push(p.ticker+' '+(up?'\u25B2':'\u25BC')+' '+p.entry_price+' \u2192 '+p.target_price, up?'#3fb950':'#f85149');
        });
      } else if (plan && plan.no_trade) push('NO TRADE TODAY \u2014 DISCIPLINE', '#d29922');
      var o = d.overall||{};
      if (o.win_rate!=null && o.triggered) push('WIN RATE '+Math.round(o.win_rate*100)+'%', o.win_rate>=0.5?'#3fb950':'#f85149');
      if (o.total_weighted_r!=null) push('TOTAL '+(o.total_weighted_r>=0?'+':'')+o.total_weighted_r+'R', o.total_weighted_r>=0?'#3fb950':'#f85149');
      if (d.equity) push('EQUITY $'+Math.round(d.equity).toLocaleString(), '#58a6ff');
      var st = streakInfo(d);
      if (st.kind!=='none') push('STREAK '+st.length+' '+st.kind.toUpperCase(), st.kind==='winning'?'#3fb950':'#f85149');
      if (state.playbookVersion!=null) push('PLAYBOOK v'+state.playbookVersion, '#e6edf3');
      push('PAPER ONLY \u00B7 NOT FINANCIAL ADVICE', '#8b949e');
    } else segs = [{t:'ARBY TV ONLINE \u2014 WAITING FOR FIRST SYNC   ', c:'#8b949e'}];
    tvCtx.font = TAPE_FONT;
    var w = 0;
    segs.forEach(function(s){ s.w = tvCtx.measureText(s.t).width; w += s.w; });
    tapeSegs = segs; tapeW = Math.max(w, 200);
  }
  rebuildTape();
  var tvAcc = 0;
  function drawTV(t){
    tvCtx.fillStyle='#05070c'; tvCtx.fillRect(0,0,512,144);
    tvCtx.fillStyle='#f85149'; tvCtx.beginPath(); tvCtx.arc(14,16,5,0,Math.PI*2); tvCtx.fill();
    tvCtx.fillStyle='#e6edf3'; tvCtx.font='bold 13px Consolas,monospace';
    tvCtx.fillText('LIVE \u00B7 ARBY TV', 26, 21);
    var open = marketOpen();
    tvCtx.fillStyle = open ? '#3fb950' : '#8b949e';
    tvCtx.textAlign='right';
    tvCtx.fillText((open?'MARKET OPEN':'MARKET CLOSED')+' \u00B7 '+etClockStr(), 500, 21);
    tvCtx.textAlign='left';
    tvCtx.strokeStyle='#20262e'; tvCtx.beginPath(); tvCtx.moveTo(0,32); tvCtx.lineTo(512,32); tvCtx.stroke();
    tvCtx.font = TAPE_FONT;
    var scroll = (t*55) % tapeW;
    var x = -scroll;
    for (var pass=0; pass<2; pass++){
      tapeSegs.forEach(function(s){
        if (x < 512 && x+s.w > 0){ tvCtx.fillStyle=s.c; tvCtx.fillText(s.t, x, 88); }
        x += s.w;
      });
    }
    tvCtx.strokeStyle='#20262e'; tvCtx.beginPath(); tvCtx.moveTo(0,112); tvCtx.lineTo(512,112); tvCtx.stroke();
    tvCtx.fillStyle='#8b949e'; tvCtx.font='12px Consolas,monospace';
    tvCtx.fillText(state.status && state.status.today ? 'SESSION '+state.status.today : 'PAPER DESK', 10, 132);
    tvTex.needsUpdate = true;
  }

  // media console + gaming setup under the wall TV
  var consoleG = new THREE.Group(); consoleG.position.set(1.3,0,-2.95);
  var mcTop = new THREE.Mesh(new THREE.BoxGeometry(1.5,0.06,0.46), new THREE.MeshStandardMaterial({color:0x14161a}));
  mcTop.position.y=0.5; consoleG.add(mcTop);
  var mcBase = new THREE.Mesh(new THREE.BoxGeometry(1.42,0.5,0.4), new THREE.MeshStandardMaterial({color:0x22262c}));
  mcBase.position.y=0.25; consoleG.add(mcBase);
  var soundbar = new THREE.Mesh(new THREE.BoxGeometry(1.1,0.07,0.09), new THREE.MeshStandardMaterial({color:0x2b3036}));
  soundbar.position.set(0,0.56,-0.16); consoleG.add(soundbar);
  var gameBox = new THREE.Mesh(new THREE.BoxGeometry(0.4,0.09,0.26), new THREE.MeshStandardMaterial({color:0x3a4048}));
  gameBox.position.set(-0.35,0.575,0.08); consoleG.add(gameBox);
  var gamePad = new THREE.Mesh(new THREE.BoxGeometry(0.16,0.035,0.09), new THREE.MeshStandardMaterial({color:0x58a6ff}));
  gamePad.position.set(0.22,0.56,0.14); consoleG.add(gamePad);
  var bowlPop = new THREE.Mesh(new THREE.CylinderGeometry(0.1,0.07,0.08,12), new THREE.MeshStandardMaterial({color:0xd6455a}));
  bowlPop.position.set(0.5,0.56,-0.1); consoleG.add(bowlPop);
  var ctrlProp = new THREE.Mesh(new THREE.BoxGeometry(0.16,0.035,0.09), new THREE.MeshStandardMaterial({color:0x58a6ff}));
  ctrlProp.castShadow=true; ctrlProp.visible=false; ctrlProp.userData.hideAtHome=true; scene.add(ctrlProp);
  shadowify(consoleG); scene.add(consoleG);
  addBlob(1.3,-2.95,1.0);

  // couch (media lounge) — pulled forward off the back wall so it doesn't
  // crowd the battlestation and actually occupies the room's open center.
  var couchG = new THREE.Group(); couchG.position.set(1.1,0,-0.3);
  var couchMat = new THREE.MeshStandardMaterial({color:0x2f3a46});
  var couchCush = new THREE.MeshStandardMaterial({color:0x3f5165});
  var couchBase = new THREE.Mesh(new THREE.BoxGeometry(1.7,0.3,0.62), couchMat); couchBase.position.y=0.22; couchG.add(couchBase);
  var couchSeat = new THREE.Mesh(new THREE.BoxGeometry(1.62,0.14,0.6), couchCush); couchSeat.position.set(0,0.44,0); couchG.add(couchSeat);
  var couchBack = new THREE.Mesh(new THREE.BoxGeometry(1.7,0.5,0.18), couchMat); couchBack.position.set(0,0.72,0.3); couchG.add(couchBack);
  [-0.83,0.83].forEach(function(ax){
    var arm = new THREE.Mesh(new THREE.BoxGeometry(0.14,0.42,0.62), couchMat); arm.position.set(ax,0.5,0); couchG.add(arm);
  });
  [-0.38,0.38].forEach(function(px){
    var pillow = new THREE.Mesh(new THREE.BoxGeometry(0.42,0.3,0.12), new THREE.MeshStandardMaterial({color:0xd6455a}));
    pillow.position.set(px,0.6,0.36); pillow.rotation.x=-0.2; couchG.add(pillow);
  });
  shadowify(couchG); scene.add(couchG);
  addBlob(1.1,-0.3,1.1);

  // neon sign on the back wall
  var neonTex = new THREE.CanvasTexture((function(){
    var c=mkCanvas(256,96), nctx=c.getContext('2d');
    nctx.clearRect(0,0,256,96);
    nctx.font='bold 62px Consolas,monospace'; nctx.textAlign='left'; nctx.textBaseline='middle';
    nctx.strokeStyle='#58a6ff'; nctx.lineWidth=9; nctx.lineJoin='round';
    nctx.strokeText('ARBY\u2019S', 12, 52);
    nctx.fillStyle='#bfe0ff';
    nctx.fillText('ARBY\u2019S', 12, 52);
    return c;
  })());
  var neonSign = new THREE.Mesh(new THREE.PlaneGeometry(0.66,0.25), new THREE.MeshBasicMaterial({map:neonTex, transparent:true, side:THREE.DoubleSide}));
  neonSign.position.set(-0.1,2.5,-ROOM_Z+0.035); scene.add(neonSign);
  var neonLight = new THREE.PointLight(0x58a6ff, 0.35, 1.6);
  neonLight.position.set(-0.1,2.5,-ROOM_Z+0.25); scene.add(neonLight);

  // wall clock (real ET time)
  var clockG = new THREE.Group();
  clockG.position.set(-0.45,2.15,-ROOM_Z+0.03);
  var clockFace = new THREE.Mesh(new THREE.CylinderGeometry(0.16,0.16,0.02,24),
    new THREE.MeshStandardMaterial({color:0xffffff}));
  clockFace.rotation.x=Math.PI/2; clockG.add(clockFace);
  var clockRim = new THREE.Mesh(new THREE.TorusGeometry(0.16,0.015,8,24),
    new THREE.MeshStandardMaterial({color:0x22262c}));
  clockG.add(clockRim);
  function mkHand(len, w){
    var g = new THREE.Group();
    var m = new THREE.Mesh(new THREE.BoxGeometry(w,len,0.008), new THREE.MeshBasicMaterial({color:0x22262c}));
    m.position.y = len/2; g.add(m); g.position.z = 0.018; clockG.add(g);
    return g;
  }
  var handMin = mkHand(0.12, 0.012), handHour = mkHand(0.08, 0.018);
  scene.add(clockG);

  // ---------------------------------------------------------- battlestation
  var chartScreens = {};
  var deskG = new THREE.Group(); deskG.position.set(0.15,0,-1.85);
  var deskMat = new THREE.MeshStandardMaterial({color:0x22262c});
  var dTop = new THREE.Mesh(new THREE.BoxGeometry(1.75,0.06,0.62), deskMat); dTop.position.y=0.64; deskG.add(dTop);
  [[-0.78,-0.24],[0.78,-0.24],[-0.78,0.24],[0.78,0.24]].forEach(function(lp){
    var leg=new THREE.Mesh(new THREE.BoxGeometry(0.06,0.64,0.06), deskMat);
    leg.position.set(lp[0],0.32,lp[1]); deskG.add(leg);
  });
  var monitorRoots = {};
  function buildMonitor(x, yawDeg, ticker, w, h, slot){
    var g = new THREE.Group(); g.position.set(x,0.66,-0.1);
    g.rotation.y = THREE.MathUtils.degToRad(yawDeg);
    var stand = new THREE.Mesh(new THREE.BoxGeometry(0.05,0.16,0.05), new THREE.MeshStandardMaterial({color:0x40464f}));
    stand.position.y=0.08; g.add(stand);
    var base = new THREE.Mesh(new THREE.BoxGeometry(0.18,0.02,0.12), new THREE.MeshStandardMaterial({color:0x40464f}));
    base.position.y=-0.01; g.add(base);
    var frame = new THREE.Mesh(new THREE.BoxGeometry(w,h,0.03), new THREE.MeshStandardMaterial({color:0x14161a}));
    frame.position.y=0.16+h/2; g.add(frame);
    var cv = mkCanvas(220,150), cctx = cv.getContext('2d');
    var tex = new THREE.CanvasTexture(cv);
    drawChart(cctx,220,150,{ticker:ticker, bars:demoBars(ticker,22)});
    tex.needsUpdate = true;
    chartScreens[slot] = { cv:cv, ctx:cctx, tex:tex, ticker:ticker };
    var screen = new THREE.Mesh(new THREE.PlaneGeometry(w-0.04,h-0.04), new THREE.MeshBasicMaterial({map:tex}));
    screen.position.set(0,0.16+h/2,0.017); g.add(screen);
    deskG.add(g);
    shadowify(g);
    monitorRoots[slot] = g;
    return g;
  }
  buildMonitor(0, 0, 'SPY', 0.62, 0.38, 'main');
  buildMonitor(-0.58, 22, 'QQQ', 0.42, 0.28, 'left');
  buildMonitor(0.58, -22, 'IWM', 0.42, 0.28, 'right');

  var ledStrip = new THREE.Mesh(new THREE.BoxGeometry(1.7,0.015,0.015), new THREE.MeshBasicMaterial({color:0x3fb950}));
  ledStrip.position.set(0,0.605,0.305); deskG.add(ledStrip);
  var ledLight = new THREE.PointLight(0x3fb950, 0.6, 2.2);
  ledLight.position.set(0.15,0.75,-1.55); scene.add(ledLight);

  var kb = new THREE.Mesh(new THREE.BoxGeometry(0.36,0.02,0.14), new THREE.MeshStandardMaterial({color:0x1c1f24}));
  kb.position.set(-0.05,0.675,0.14); deskG.add(kb);
  var mousepad = new THREE.Mesh(new THREE.BoxGeometry(0.26,0.005,0.18), new THREE.MeshStandardMaterial({color:0x2b2f36}));
  mousepad.position.set(0.32,0.665,0.16); deskG.add(mousepad);
  var mouseProp = new THREE.Mesh(new THREE.BoxGeometry(0.05,0.03,0.08), new THREE.MeshStandardMaterial({color:0x1c1f24}));
  mouseProp.position.set(0.34,0.685,0.16); deskG.add(mouseProp);
  var spkG = new THREE.Group();
  [-0.7,0.7].forEach(function(x){
    var spk = new THREE.Mesh(new THREE.BoxGeometry(0.08,0.16,0.08), new THREE.MeshStandardMaterial({color:0x1c1f24}));
    spk.position.set(x,0.72,-0.2); spkG.add(spk);
  });
  deskG.add(spkG);
  var deskCan = new THREE.Mesh(new THREE.CylinderGeometry(0.03,0.03,0.11,10), new THREE.MeshStandardMaterial({color:0xd6455a}));
  deskCan.position.set(0.75,0.705,0.22); deskG.add(deskCan);

  // desk lamp — dark/off by day, glows at dusk/night (see applyPhaseLighting below)
  var lampG = new THREE.Group(); lampG.position.set(-0.85,0.66,0.22);
  var lampMat = new THREE.MeshStandardMaterial({color:0x40464f});
  var lampBase = new THREE.Mesh(new THREE.CylinderGeometry(0.045,0.05,0.015,10), lampMat);
  lampG.add(lampBase);
  var lampArm = new THREE.Mesh(new THREE.CylinderGeometry(0.007,0.007,0.19,6), lampMat);
  lampArm.position.y=0.1; lampG.add(lampArm);
  var lampHeadMat = new THREE.MeshStandardMaterial({color:0x2b2f36, emissive:0xffdca8, emissiveIntensity:0});
  var lampHead = new THREE.Mesh(new THREE.ConeGeometry(0.05,0.08,10), lampHeadMat);
  lampHead.rotation.x=Math.PI; lampHead.position.set(0.02,0.2,0); lampG.add(lampHead);
  deskG.add(lampG);
  var lampLight = new THREE.PointLight(0xffdca8, 0, 1.1);
  lampLight.position.set(-0.7,0.86,-1.63); scene.add(lampLight);

  shadowify(deskG);
  scene.add(deskG);
  addBlob(0.15,-1.55,1.5);

  // gaming chair
  var chairMat = new THREE.MeshStandardMaterial({color:0x1b1e23});
  var accentMat = new THREE.MeshStandardMaterial({color:0xd6455a});
  var chairG = new THREE.Group(); chairG.position.set(0.15,0,-1.05);
  var seat = new THREE.Mesh(new THREE.BoxGeometry(0.44,0.06,0.42), chairMat); seat.position.y=0.5; chairG.add(seat);
  var backRest = new THREE.Mesh(new THREE.BoxGeometry(0.44,0.55,0.06), chairMat); backRest.position.set(0,0.82,0.19); chairG.add(backRest);
  var backAccent = new THREE.Mesh(new THREE.BoxGeometry(0.08,0.5,0.02), accentMat); backAccent.position.set(0,0.82,0.155); chairG.add(backAccent);
  var headrest = new THREE.Mesh(new THREE.BoxGeometry(0.3,0.16,0.07), chairMat); headrest.position.set(0,1.14,0.2); chairG.add(headrest);
  [-0.26,0.26].forEach(function(x){
    var armR1=new THREE.Mesh(new THREE.BoxGeometry(0.06,0.05,0.3), chairMat); armR1.position.set(x,0.58,0.03); chairG.add(armR1);
    var armPost=new THREE.Mesh(new THREE.BoxGeometry(0.05,0.12,0.05), chairMat); armPost.position.set(x,0.5,0.03); chairG.add(armPost);
  });
  var chairPole = new THREE.Mesh(new THREE.CylinderGeometry(0.04,0.04,0.42,8), new THREE.MeshStandardMaterial({color:0x40464f}));
  chairPole.position.y=0.27; chairG.add(chairPole);
  var chairBase = new THREE.Mesh(new THREE.CylinderGeometry(0.22,0.22,0.03,5), new THREE.MeshStandardMaterial({color:0x40464f}));
  chairBase.position.y=0.06; chairG.add(chairBase);
  shadowify(chairG); scene.add(chairG);
  addBlob(0.15,-1.05,1.0);

  // coffee corner + Benzinga newspaper
  var coffeeG = new THREE.Group(); coffeeG.position.set(1.85,0,1.3);
  var ctTop = new THREE.Mesh(new THREE.CylinderGeometry(0.28,0.28,0.05,16), new THREE.MeshStandardMaterial({color:0xc79a6b}));
  ctTop.position.y=0.42; coffeeG.add(ctTop);
  var ctLeg = new THREE.Mesh(new THREE.CylinderGeometry(0.05,0.05,0.42,8), new THREE.MeshStandardMaterial({color:0x8a5a3b}));
  ctLeg.position.y=0.21; coffeeG.add(ctLeg);
  var mug = new THREE.Mesh(new THREE.CylinderGeometry(0.05,0.05,0.08,10), new THREE.MeshStandardMaterial({color:0xffffff}));
  mug.position.set(-0.12,0.49,0.02); coffeeG.add(mug);
  var snackBowl = new THREE.Mesh(new THREE.CylinderGeometry(0.07,0.05,0.05,12), new THREE.MeshStandardMaterial({color:0xe8e4da}));
  snackBowl.position.set(0.1,0.49,0.1); coffeeG.add(snackBowl);
  var chipsBag = new THREE.Mesh(new THREE.BoxGeometry(0.1,0.14,0.05), new THREE.MeshStandardMaterial({color:0xd6455a}));
  chipsBag.position.set(-0.16,0.47,-0.08); coffeeG.add(chipsBag);
  shadowify(coffeeG); scene.add(coffeeG);
  addBlob(1.85,1.3,1.0);

  var newsG = new THREE.Group(); newsG.position.set(1.92,0.455,1.22);
  var paper = new THREE.Mesh(new THREE.BoxGeometry(0.24,0.012,0.17), new THREE.MeshStandardMaterial({color:0xf5f2ea}));
  newsG.add(paper);
  var mastTex = new THREE.CanvasTexture((function(){
    var c=mkCanvas(128,90), x=c.getContext('2d');
    x.fillStyle='#f5f2ea'; x.fillRect(0,0,128,90);
    x.fillStyle='#101418'; x.font='bold 16px Georgia,serif'; x.fillText('BENZINGA', 12, 22);
    x.strokeStyle='#7a746a';
    for (var i=0;i<4;i++){ x.beginPath(); x.moveTo(12,36+i*12); x.lineTo(116,36+i*12); x.stroke(); }
    return c;
  })());
  var mast = new THREE.Mesh(new THREE.PlaneGeometry(0.22,0.15), new THREE.MeshBasicMaterial({map:mastTex}));
  mast.rotation.x=-Math.PI/2; mast.rotation.z=0.4; mast.position.y=0.008; newsG.add(mast);
  paper.rotation.y=0.4;
  shadowify(newsG); scene.add(newsG);

  // ---- bed (sleep corner, front-left, headboard against the left wall) ----
  var bedFrameMat = new THREE.MeshStandardMaterial({color:0x3c2f24});
  var bedG = new THREE.Group(); bedG.position.set(-2.35,0,0.85);
  var bedFrame = new THREE.Mesh(new THREE.BoxGeometry(1.7,0.18,1.4), bedFrameMat); bedFrame.position.y=0.09; bedG.add(bedFrame);
  var mattress = new THREE.Mesh(new THREE.BoxGeometry(1.62,0.14,1.32), new THREE.MeshStandardMaterial({color:0xf0ead8}));
  mattress.position.y=0.25; bedG.add(mattress);
  var headboard = new THREE.Mesh(new THREE.BoxGeometry(0.08,0.55,1.4), bedFrameMat);
  headboard.position.set(-0.83,0.44,0); bedG.add(headboard);
  var blanket = new THREE.Mesh(new THREE.BoxGeometry(0.95,0.07,1.34), new THREE.MeshStandardMaterial({color:0x8fb9e8}));
  blanket.position.set(0.28,0.34,0); bedG.add(blanket);
  [-0.38,0.38].forEach(function(pz){
    var pillow = new THREE.Mesh(new THREE.BoxGeometry(0.3,0.1,0.34), new THREE.MeshStandardMaterial({color:0xf5f0e6}));
    pillow.position.set(-0.62,0.36,pz); bedG.add(pillow);
  });
  shadowify(bedG); scene.add(bedG);
  addBlob(-2.35,0.85,1.4);

  // nightstand + reading lamp + paperback (beside the bed)
  var nightG = new THREE.Group(); nightG.position.set(-1.62,0,1.45);
  var ntTop = new THREE.Mesh(new THREE.BoxGeometry(0.42,0.04,0.36), new THREE.MeshStandardMaterial({color:0xc79a6b}));
  ntTop.position.y=0.44; nightG.add(ntTop);
  var ntLeg = new THREE.Mesh(new THREE.BoxGeometry(0.34,0.44,0.28), new THREE.MeshStandardMaterial({color:0x8a5a3b}));
  ntLeg.position.y=0.22; nightG.add(ntLeg);
  var ntLamp = new THREE.Mesh(new THREE.CylinderGeometry(0.02,0.03,0.16,10), new THREE.MeshStandardMaterial({color:0x22262c}));
  ntLamp.position.set(-0.1,0.54,0); nightG.add(ntLamp);
  var ntLampGlow = new THREE.Mesh(new THREE.SphereGeometry(0.045,10,8), new THREE.MeshBasicMaterial({color:0xffdca8}));
  ntLampGlow.position.set(-0.1,0.64,0); nightG.add(ntLampGlow);
  var ntBook = new THREE.Mesh(new THREE.BoxGeometry(0.22,0.02,0.16), new THREE.MeshStandardMaterial({color:0xd6455a}));
  ntBook.position.set(0.08,0.47,0.04); nightG.add(ntBook);
  shadowify(nightG); scene.add(nightG);

  // acoustic guitar on a stand (leisure corner)
  var guitarG = new THREE.Group(); guitarG.position.set(-1.1,0,2.35);
  var gtrStand = new THREE.Mesh(new THREE.CylinderGeometry(0.03,0.09,0.55,8), new THREE.MeshStandardMaterial({color:0x40464f}));
  gtrStand.position.y=0.27; guitarG.add(gtrStand);
  var gtrBody = new THREE.Mesh(new THREE.BoxGeometry(0.3,0.34,0.12), new THREE.MeshStandardMaterial({color:0xc98a4b}));
  gtrBody.position.y=0.82; guitarG.add(gtrBody);
  var gtrHole = new THREE.Mesh(new THREE.CircleGeometry(0.07,16), new THREE.MeshBasicMaterial({color:0x24160a}));
  gtrHole.position.set(0,0.88,0.062); guitarG.add(gtrHole);
  var gtrNeck = new THREE.Mesh(new THREE.BoxGeometry(0.06,0.5,0.05), new THREE.MeshStandardMaterial({color:0x5a3a24}));
  gtrNeck.position.set(0.18,1.2,0); gtrNeck.rotation.z=-0.55; guitarG.add(gtrNeck);
  var gtrHead = new THREE.Mesh(new THREE.BoxGeometry(0.08,0.14,0.05), new THREE.MeshStandardMaterial({color:0x3a2817}));
  gtrHead.position.set(0.35,1.46,0); gtrHead.rotation.z=-0.55; guitarG.add(gtrHead);
  shadowify(guitarG); scene.add(guitarG);

  // mini basketball hoop (right-wall gym corner) + ball
  var hoopG = new THREE.Group(); hoopG.position.set(3.0,0,-0.85);
  var hoopPole = new THREE.Mesh(new THREE.CylinderGeometry(0.03,0.045,1.7,10), new THREE.MeshStandardMaterial({color:0x40464f}));
  hoopPole.position.y=0.85; hoopG.add(hoopPole);
  var hoopBack = new THREE.Mesh(new THREE.BoxGeometry(0.04,0.42,0.42), new THREE.MeshStandardMaterial({color:0xffffff}));
  hoopBack.position.set(-0.18,1.45,0); hoopG.add(hoopBack);
  var hoopRim = new THREE.Mesh(new THREE.TorusGeometry(0.17,0.014,8,20), new THREE.MeshStandardMaterial({color:0xe8751a}));
  hoopRim.rotation.x=Math.PI/2; hoopRim.position.set(-0.19,1.38,0); hoopG.add(hoopRim);
  shadowify(hoopG); scene.add(hoopG);

  var basketBall = new THREE.Mesh(new THREE.SphereGeometry(0.08,16,12), new THREE.MeshStandardMaterial({color:0xe8751a}));
  basketBall.castShadow=true; basketBall.visible=false; scene.add(basketBall);

  // weight rack + two dumbbells (right-wall gym corner)
  var weightsG = new THREE.Group(); weightsG.position.set(3.15,0,-1.7);
  var wrFrame = new THREE.Mesh(new THREE.BoxGeometry(0.5,0.55,0.28), new THREE.MeshStandardMaterial({color:0x22262c}));
  wrFrame.position.y=0.28; weightsG.add(wrFrame);
  var wrShelf = new THREE.Mesh(new THREE.BoxGeometry(0.44,0.03,0.24), new THREE.MeshStandardMaterial({color:0x40464f}));
  wrShelf.position.set(0,0.2,0); weightsG.add(wrShelf);
  function addDumbbellVisual(zc, color){
    var g = new THREE.Group(); g.position.set(0,0.52,zc);
    var bar = new THREE.Mesh(new THREE.CylinderGeometry(0.016,0.016,0.2,8), new THREE.MeshStandardMaterial({color:0x8b949e}));
    bar.rotation.z=Math.PI/2; g.add(bar);
    [-0.1,0.1].forEach(function(px){
      var pl=new THREE.Mesh(new THREE.SphereGeometry(0.05,10,8), new THREE.MeshStandardMaterial({color:color}));
      pl.position.x=px; g.add(pl);
    });
    return g;
  }
  weightsG.add(addDumbbellVisual(-0.06, 0x3fb950));
  weightsG.add(addDumbbellVisual(0.06, 0xd6455a));
  shadowify(weightsG); scene.add(weightsG);

  // handheld dumbbell prop (picked up for a lift)
  var dumbbell = new THREE.Group();
  var dbBar = new THREE.Mesh(new THREE.CylinderGeometry(0.016,0.016,0.18,8), new THREE.MeshStandardMaterial({color:0x8b949e}));
  dbBar.rotation.z=Math.PI/2; dumbbell.add(dbBar);
  [-0.09,0.09].forEach(function(x){
    var dp=new THREE.Mesh(new THREE.SphereGeometry(0.045,10,8), new THREE.MeshStandardMaterial({color:0x3fb950}));
    dp.position.x=x; dumbbell.add(dp);
  });
  shadowify(dumbbell); dumbbell.visible=false; dumbbell.userData.hideAtHome=true;
  scene.add(dumbbell);

  // plant + mini fridge (flavor props)
  var plantG = new THREE.Group(); plantG.position.set(-2.2,0,-1.72);
  var pot = new THREE.Mesh(new THREE.CylinderGeometry(0.09,0.11,0.16,10), new THREE.MeshStandardMaterial({color:0xa9563c}));
  pot.position.y=0.08; plantG.add(pot);
  [[0,0.3,0,0.16],[0.05,0.42,0.04,0.13],[-0.06,0.4,-0.03,0.12],[0,0.52,0,0.1]].forEach(function(fp){
    var leaf = new THREE.Mesh(new THREE.BoxGeometry(fp[3],fp[3]*1.4,fp[3]),
      new THREE.MeshStandardMaterial({color:0x2f9e44}));
    leaf.position.set(fp[0],fp[1],fp[2]); leaf.rotation.y=fp[1]*3; plantG.add(leaf);
  });
  shadowify(plantG); scene.add(plantG);

  var fridgeG = new THREE.Group(); fridgeG.position.set(1.9,0,-1.88);
  var fridgeBody = new THREE.Mesh(new THREE.BoxGeometry(0.38,0.52,0.38), new THREE.MeshStandardMaterial({color:0x2b3036}));
  fridgeBody.position.y=0.26; fridgeG.add(fridgeBody);
  var fridgeHandle = new THREE.Mesh(new THREE.BoxGeometry(0.02,0.16,0.03), new THREE.MeshStandardMaterial({color:0x8b949e}));
  fridgeHandle.position.set(-0.14,0.32,0.2); fridgeG.add(fridgeHandle);
  var fridgeCan = new THREE.Mesh(new THREE.CylinderGeometry(0.03,0.03,0.1,10), new THREE.MeshStandardMaterial({color:0x3fb950}));
  fridgeCan.position.set(0,0.57,0); fridgeG.add(fridgeCan);
  shadowify(fridgeG); scene.add(fridgeG);

  // stats shelf (back wall, right end) — a live ledger plaque, redrawn on every sync
  var shelfCv = mkCanvas(200,140), shelfCtx = shelfCv.getContext('2d');
  var shelfTex = new THREE.CanvasTexture(shelfCv);
  function drawShelf(){
    var o = (state.status && state.status.overall) || {};
    var st = streakInfo(state.status);
    shelfCtx.fillStyle='#2b2f36'; shelfCtx.fillRect(0,0,200,140);
    shelfCtx.strokeStyle='#454b54'; shelfCtx.lineWidth=2;
    [46,90].forEach(function(y){ shelfCtx.beginPath(); shelfCtx.moveTo(6,y); shelfCtx.lineTo(194,y); shelfCtx.stroke(); });
    shelfCtx.fillStyle='#e6edf3'; shelfCtx.font='bold 13px sans-serif';
    shelfCtx.fillText('LEDGER STATS', 10, 20);
    shelfCtx.font='11px Consolas,monospace'; shelfCtx.fillStyle='#8b949e';
    shelfCtx.fillText('GRADED  '+(o.picks_graded||0), 10, 55);
    shelfCtx.fillText('STREAK  '+(st.kind!=='none' ? st.length+' '+st.kind.toUpperCase() : '—'), 10, 99);
    shelfCtx.fillStyle = (o.total_weighted_r||0)>=0 ? '#3fb950' : '#f85149';
    shelfCtx.font='bold 12px Consolas,monospace';
    shelfCtx.fillText('TOTAL   '+((o.total_weighted_r||0)>=0?'+':'')+(o.total_weighted_r||0)+'R', 10, 133);
    shelfTex.needsUpdate = true;
  }
  drawShelf();
  function shelfQuip(){
    var o = (state.status && state.status.overall) || {};
    var st = streakInfo(state.status);
    if (!o.picks_graded) return 'Empty case so far — first graded trade goes up here.';
    var streakTxt = st.kind!=='none' ? st.length+' '+st.kind+' in a row' : 'no active streak';
    return o.picks_graded+' graded, '+streakTxt+', '+((o.total_weighted_r||0)>=0?'+':'')+(o.total_weighted_r||0)+'R total.';
  }
  var shelfG = new THREE.Group();
  var shelfBoard = new THREE.Mesh(new THREE.PlaneGeometry(0.72,0.5), new THREE.MeshBasicMaterial({map:shelfTex}));
  shelfBoard.position.set(2.9,1.4,-ROOM_Z+0.03); shelfG.add(shelfBoard);
  var shelfFrame = new THREE.Mesh(new THREE.BoxGeometry(0.8,0.58,0.03), new THREE.MeshStandardMaterial({color:0x14161a}));
  shelfFrame.position.set(2.9,1.4,-ROOM_Z+0.012); shelfG.add(shelfFrame);
  shadowify(shelfG); scene.add(shelfG);

  // dartboard (back wall, left end)
  var dartTex = new THREE.CanvasTexture((function(){
    var c=mkCanvas(120,120), x=c.getContext('2d');
    x.fillStyle='#e8dcc3'; x.fillRect(0,0,120,120);
    [[54,'#1c1f24'],[42,'#e6edf3'],[30,'#1c1f24'],[18,'#e6edf3'],[8,'#d6455a']].forEach(function(r){
      x.fillStyle=r[1]; x.beginPath(); x.arc(60,60,r[0],0,Math.PI*2); x.fill();
    });
    return c;
  })());
  var dartboardG = new THREE.Group();
  var dartboardMesh = new THREE.Mesh(new THREE.CircleGeometry(0.28,24), new THREE.MeshBasicMaterial({map:dartTex}));
  dartboardMesh.position.set(-2.9,1.35,-ROOM_Z+0.03); dartboardG.add(dartboardMesh);
  var dartRim = new THREE.Mesh(new THREE.TorusGeometry(0.29,0.02,8,24), new THREE.MeshStandardMaterial({color:0x5a3a24}));
  dartRim.position.set(-2.9,1.35,-ROOM_Z+0.025); dartboardG.add(dartRim);
  scene.add(dartboardG);

  // bookshelf (right wall, front) — trading library + knickknacks
  var shelfWood = new THREE.MeshStandardMaterial({color:0x6b4a33});
  var bookshelfG = new THREE.Group(); bookshelfG.position.set(3.45,0,1.5);
  // (no separate interior back-panel: bsCase is a solid box, so a panel
  // flush with its own front face just z-fights with it — this IS the back.)
  var bsCase = new THREE.Mesh(new THREE.BoxGeometry(0.26,1.7,1.0), shelfWood); bsCase.position.y=0.85; bookshelfG.add(bsCase);
  var BOOK_COLORS = [0xd6455a,0x3fb950,0x58a6ff,0xd29922,0xe6edf3,0x8b949e];
  var bsRand = seeded(77);
  [0.5,0.95,1.4].forEach(function(sy){
    var board = new THREE.Mesh(new THREE.BoxGeometry(0.3,0.04,0.98), shelfWood);
    board.position.set(0,sy,0); bookshelfG.add(board);
    var xpos = -0.4;
    while (xpos < 0.42){
      var bw = 0.1+bsRand()*0.08;
      if (bsRand()>0.3){
        var book = new THREE.Mesh(new THREE.BoxGeometry(0.12,0.3,bw),
          new THREE.MeshStandardMaterial({color: BOOK_COLORS[Math.floor(bsRand()*BOOK_COLORS.length)]}));
        book.position.set(0, sy+0.04+0.15, xpos);
        book.rotation.x = (bsRand()-0.5)*0.05;
        bookshelfG.add(book);
      }
      xpos += bw+0.02;
    }
  });
  var bsTrophy = new THREE.Mesh(new THREE.CylinderGeometry(0.03,0.05,0.12,12), new THREE.MeshStandardMaterial({color:0xd29922}));
  bsTrophy.position.set(0,1.72,0.28); bookshelfG.add(bsTrophy);
  shadowify(bookshelfG); scene.add(bookshelfG);
  addBlob(3.45,1.5,0.7);

  // treadmill (new front-left floor space) — an alternate energy-restore station
  var treadmillG = new THREE.Group(); treadmillG.position.set(-2.9,0,2.2);
  var tmDeckMat = new THREE.MeshStandardMaterial({color:0x21262d});
  var tmDeck = new THREE.Mesh(new THREE.BoxGeometry(0.5,0.08,1.1), tmDeckMat); tmDeck.position.y=0.08; treadmillG.add(tmDeck);
  var tmBelt = new THREE.Mesh(new THREE.BoxGeometry(0.4,0.01,1.0), new THREE.MeshStandardMaterial({color:0x14161a}));
  tmBelt.position.y=0.125; treadmillG.add(tmBelt);
  var tmConsoleMat = new THREE.MeshStandardMaterial({color:0x3a4048});
  var tmConsole = new THREE.Mesh(new THREE.BoxGeometry(0.45,0.35,0.06), tmConsoleMat); tmConsole.position.set(0,0.55,-0.5); treadmillG.add(tmConsole);
  [-0.2,0.2].forEach(function(x){
    var rail = new THREE.Mesh(new THREE.CylinderGeometry(0.015,0.015,0.55,6), tmConsoleMat);
    rail.position.set(x,0.35,-0.45); treadmillG.add(rail);
  });
  shadowify(treadmillG); scene.add(treadmillG);
  addBlob(-2.9,2.2,0.9);

  // interactive toys: paper-toss bin, paper ball, pocket phone, whiteboard marker, walk marker
  var binG = new THREE.Group(); binG.position.set(0.98,0,-1.42);
  var binWall = new THREE.Mesh(new THREE.CylinderGeometry(0.09,0.07,0.18,12,1,true),
    new THREE.MeshStandardMaterial({color:0x3a4048, side:THREE.DoubleSide}));
  binWall.position.y = 0.09; binG.add(binWall);
  var binBase = new THREE.Mesh(new THREE.CircleGeometry(0.07,12), new THREE.MeshStandardMaterial({color:0x2b3036}));
  binBase.rotation.x = -Math.PI/2; binBase.position.y = 0.006; binG.add(binBase);
  shadowify(binG); scene.add(binG);

  var paperBall = new THREE.Mesh(new THREE.DodecahedronGeometry(0.035,0),
    new THREE.MeshStandardMaterial({color:0xf0ede4}));
  paperBall.castShadow = true; paperBall.visible = false; scene.add(paperBall);

  var phoneProp = new THREE.Group();
  var phBody = new THREE.Mesh(new THREE.BoxGeometry(0.055,0.1,0.012), new THREE.MeshStandardMaterial({color:0x14161a}));
  phoneProp.add(phBody);
  var phScreen = new THREE.Mesh(new THREE.PlaneGeometry(0.045,0.085), new THREE.MeshBasicMaterial({color:0x8fc7ff}));
  phScreen.position.z = 0.008; // faces +Z, back toward his eyes (he faces -Z)
  phoneProp.add(phScreen);
  phoneProp.visible = false; phoneProp.userData.hideAtHome = true;
  scene.add(phoneProp);

  var markerProp = new THREE.Mesh(new THREE.CylinderGeometry(0.011,0.011,0.09,8),
    new THREE.MeshStandardMaterial({color:0xd6455a}));
  markerProp.visible = false; markerProp.userData.hideAtHome = true;
  scene.add(markerProp);

  var walkMarker = new THREE.Mesh(new THREE.RingGeometry(0.1,0.16,24),
    new THREE.MeshBasicMaterial({color:0x58a6ff, transparent:true, opacity:0, depthWrite:false, side:THREE.DoubleSide}));
  walkMarker.rotation.x = -Math.PI/2; walkMarker.position.y = 0.02;
  scene.add(walkMarker);

  // ---------------------------------------------------------- character (Arby)
  var SKIN = 0x6b4a33;
  var shirtMat = new THREE.MeshStandardMaterial({color:0x58a6ff});
  var pantsMat = new THREE.MeshStandardMaterial({color:0x21262d});
  var shoeMat = new THREE.MeshStandardMaterial({color:0x14161a});

  function segmentGroup(w,h,d,mat){
    var g=new THREE.Group();
    var mesh=new THREE.Mesh(new THREE.BoxGeometry(w,h,d),mat);
    mesh.position.y=-h/2; g.add(mesh);
    return g;
  }
  var char3 = new THREE.Group();
  var HIP_Y=0.52, UPPER_LEN=0.26, LOWER_LEN=0.26;
  function buildLeg(x){
    var hip=new THREE.Group(); hip.position.set(x,HIP_Y,0);
    var upper=segmentGroup(0.16,UPPER_LEN,0.16,pantsMat); hip.add(upper);
    var knee=new THREE.Group(); knee.position.y=-UPPER_LEN; upper.add(knee);
    var lower=segmentGroup(0.15,LOWER_LEN,0.15,pantsMat); knee.add(lower);
    var foot=new THREE.Mesh(new THREE.BoxGeometry(0.15,0.06,0.2), shoeMat);
    foot.position.set(0,-LOWER_LEN+0.03,-0.03); lower.add(foot);
    char3.add(hip);
    return {hip:hip, knee:knee};
  }
  var legL=buildLeg(-0.13), legR=buildLeg(0.13);

  var torso = new THREE.Mesh(new THREE.BoxGeometry(0.46,0.5,0.26), shirtMat);
  torso.position.y = HIP_Y+0.25; char3.add(torso);

  var SHOULDER_Y = HIP_Y+0.5;
  function buildArm(x){
    var shoulder=new THREE.Group(); shoulder.position.set(x,SHOULDER_Y,0);
    var armSeg=segmentGroup(0.13,0.42,0.13,shirtMat); shoulder.add(armSeg);
    var hand=new THREE.Mesh(new THREE.BoxGeometry(0.11,0.09,0.11), new THREE.MeshStandardMaterial({color:SKIN}));
    hand.position.y=-0.44; shoulder.add(hand);
    var grip=new THREE.Group(); grip.position.set(0,-0.47,0); shoulder.add(grip); // held-item anchor
    char3.add(shoulder);
    return {shoulder:shoulder, grip:grip};
  }
  var armLParts=buildArm(-0.30), armRParts=buildArm(0.30);
  var armL=armLParts.shoulder, armR=armRParts.shoulder;
  var handL=armLParts.grip, handR=armRParts.grip;

  // head with an expressive canvas face (front = -Z)
  var headGroup = new THREE.Group(); headGroup.position.set(0,SHOULDER_Y,0);
  var faceCv = mkCanvas(128,128), faceCtx = faceCv.getContext('2d');
  var faceTex = new THREE.CanvasTexture(faceCv);
  faceTex.magFilter = THREE.NearestFilter;
  var blinkClosed = false;
  function drawFace(){
    var m = state.moodScore;
    var x = faceCtx;
    x.fillStyle='#'+SKIN.toString(16); x.fillRect(0,0,128,128);
    // beard + sideburns
    x.fillStyle='#17110b';
    x.fillRect(0,92,128,36);
    x.fillRect(0,58,12,40); x.fillRect(116,58,12,40);
    x.fillRect(30,84,68,10); // mustache
    // brows
    var tilt = m<-0.5 ? 0.35 : m<-0.15 ? -0.2 : m>0.5 ? -0.12 : 0;
    [[40,1],[88,-1]].forEach(function(bp){
      x.save(); x.translate(bp[0],34); x.rotate(tilt*bp[1]);
      x.fillStyle='#17110b'; x.fillRect(-13,-4,26,7);
      x.restore();
    });
    // eyes
    if (blinkClosed){
      x.strokeStyle='#17110b'; x.lineWidth=3;
      [[40],[88]].forEach(function(ep){ x.beginPath(); x.moveTo(ep[0]-9,52); x.lineTo(ep[0]+9,52); x.stroke(); });
    } else if (m>0.55){
      x.strokeStyle='#17110b'; x.lineWidth=4;
      [[40],[88]].forEach(function(ep){ x.beginPath(); x.arc(ep[0],54,9,Math.PI*1.05,Math.PI*1.95); x.stroke(); });
    } else {
      [[40],[88]].forEach(function(ep){
        x.fillStyle='#f6f8fa'; x.fillRect(ep[0]-9,45,18,14);
        x.fillStyle='#0d1117'; x.fillRect(ep[0]-3,48,7,9);
      });
    }
    // mouth (drawn on skin just above the beard band)
    x.strokeStyle='#17110b'; x.lineWidth=3; x.beginPath();
    var curve = clamp(m,-1,1)*8;
    x.moveTo(48,80-curve*0.2);
    x.quadraticCurveTo(64,80+curve,80,80-curve*0.2);
    x.stroke();
    faceTex.needsUpdate = true;
  }
  drawFace();
  var skinMat = new THREE.MeshStandardMaterial({color:SKIN});
  var faceMat = new THREE.MeshStandardMaterial({map:faceTex});
  var head = new THREE.Mesh(new THREE.BoxGeometry(0.32,0.32,0.32),
    [skinMat, skinMat, skinMat, skinMat, skinMat, faceMat]); // -Z face is the front
  head.position.y=0.18; headGroup.add(head);
  // headset
  var hsMat = new THREE.MeshStandardMaterial({color:0x14161a});
  [-0.185,0.185].forEach(function(x){
    var ear=new THREE.Mesh(new THREE.BoxGeometry(0.05,0.1,0.1), hsMat);
    ear.position.set(x,0.18,0); headGroup.add(ear);
  });
  var band=new THREE.Mesh(new THREE.BoxGeometry(0.38,0.03,0.08), hsMat);
  band.position.y=0.355; headGroup.add(band);

  // plumbob — a little Sims nod, floating over his head and tinted with his mood color
  var plumbobMat = new THREE.MeshStandardMaterial({color:0x58a6ff, emissive:0x58a6ff, emissiveIntensity:0.7});
  var plumbob = new THREE.Mesh(new THREE.OctahedronGeometry(0.065,0), plumbobMat);
  plumbob.position.y=0.62; headGroup.add(plumbob);

  char3.add(headGroup);
  char3.position.set(0.15,0,-1.05);
  shadowify(char3);
  scene.add(char3);
  var charBlob = addBlob(0.15,-1.05,0.85);

  // ---------------------------------------------------------- particles
  var particleTex = {};
  function textSprite(txt, color){
    var c=mkCanvas(64,64), x=c.getContext('2d');
    x.font='bold 30px Consolas,monospace'; x.textAlign='center'; x.textBaseline='middle';
    x.fillStyle=color; x.fillText(txt,32,34);
    return new THREE.CanvasTexture(c);
  }
  particleTex.win = textSprite('+R','#3fb950');
  particleTex.loss = textSprite('-R','#f85149');
  particleTex.zzz = textSprite('z','#8b949e');
  particleTex.spark = textSprite('\u2726','#ffd75e');
  particleTex.note = textSprite('\u266A','#79c0ff');
  var particles = [];
  for (var pi=0; pi<16; pi++){
    var sm = new THREE.SpriteMaterial({map:particleTex.win, transparent:true, depthWrite:false});
    var sp = new THREE.Sprite(sm);
    sp.scale.set(0.22,0.22,1); sp.visible=false;
    scene.add(sp);
    particles.push({s:sp, vx:0, vy:0, vz:0, life:0, max:1});
  }
  function spawnBurst(kind, n, at){
    var origin;
    if (at){ origin = new THREE.Vector3(at.x, at.y, at.z); }
    else {
      origin = new THREE.Vector3();
      headGroup.getWorldPosition(origin);
      origin.y += 0.35;
    }
    var count = 0;
    for (var i=0;i<particles.length && count<n;i++){
      var p = particles[i];
      if (p.life>0) continue;
      p.s.material.map = particleTex[kind] || particleTex.spark;
      p.s.material.needsUpdate = true;
      p.s.position.set(origin.x+(Math.random()-0.5)*0.3, origin.y+Math.random()*0.1, origin.z+(Math.random()-0.5)*0.3);
      p.vx=(Math.random()-0.5)*0.5;
      p.vy=0.7+Math.random()*0.6;
      p.vz=(Math.random()-0.5)*0.5;
      if (kind==='zzz'){ p.vx*=0.2; p.vy=0.25; p.vz*=0.2; }
      p.max = p.life = kind==='zzz' ? 2.4 : 1.2;
      p.s.visible = true;
      count++;
    }
  }
  function updateParticles(dt){
    particles.forEach(function(p){
      if (p.life<=0) return;
      p.life -= dt;
      if (p.life<=0){ p.s.visible=false; return; }
      p.s.position.x += p.vx*dt; p.s.position.y += p.vy*dt; p.s.position.z += p.vz*dt;
      p.s.material.opacity = p.life/p.max;
    });
  }

  // ---------------------------------------------------------- stations & routing
  var STATIONS = [
    { name:'desk',      label:'trading at the battlestation', pos:new THREE.Vector3(0.15,0,-1.05), yaw:0, sit:true,  minT:240, maxT:480, w:2.2,
      hint:'Charts up. Click a monitor to open it for real.' },
    { name:'whiteboard',label:'reviewing the playbook', pos:new THREE.Vector3(-1.5,0,-2.6), yaw:0, sit:false, minT:45, maxT:80, w:1,
      hint:'Playbook rules. Click the board to jump to the full text.' },
    { name:'tv',        label:'watching the big-screen TV', pos:new THREE.Vector3(0.75,0,-0.3), yaw:0, sit:true, minT:120, maxT:240, w:0.9,
      hint:'Tape\u2019s rolling. Click the TV for CNBC.' },
    { name:'chartwall', label:'studying the equity curve', pos:new THREE.Vector3(3.05,0,0.35), yaw:-Math.PI/2, sit:false, minT:40, maxT:75, w:1,
      hint:'The curve tells the truth. Click it to jump to Performance.' },
    { name:'coffee',    label:'coffee + Benzinga break', pos:new THREE.Vector3(1.45,0,1.05), yaw:-2.13, sit:false, minT:50, maxT:90, w:1,
      hint:'Benzinga\u2019s on the table \u2014 tap the paper.' },
    { name:'window',    label:'staring at the market outside', pos:new THREE.Vector3(-3.1,0,-0.5), yaw:Math.PI/2, sit:false, minT:30, maxT:55, w:0.9,
      hint:'Big-picture time. Click the window for the Finviz map.' },
    { name:'calendar',  label:'checking the econ calendar', pos:new THREE.Vector3(-3.12,0,-0.9), yaw:Math.PI/2, sit:false, minT:25, maxT:40, w:0.7,
      hint:'What\u2019s on deck this week? Click the calendar.' },
    { name:'bed',       label:'crashing in the sleep corner', pos:new THREE.Vector3(-2.35,0,0.35), yaw:0, sit:true, minT:300, maxT:600, w:0.9,
      hint:'Rest is a position.' },
    { name:'weights',   label:'a quick pump', pos:new THREE.Vector3(2.78,0,-1.5), yaw:-Math.PI/2, sit:false, minT:45, maxT:80, w:0.6,
      hint:'Strong body, steady hands.' },
    { name:'console',   label:'gaming on the console', pos:new THREE.Vector3(1.45,0,-0.3), yaw:0, sit:true, minT:120, maxT:240, w:0.7,
      hint:'One more run. Click the console.' },
    { name:'phone',     label:'scrolling StockTwits', pos:new THREE.Vector3(-0.2,0,1.6), yaw:Math.PI, sit:false, minT:40, maxT:70, w:0.8,
      hint:'Sentiment check \u2014 click the corkboard for StockTwits.' },
    { name:'stretch',   label:'stretch break', pos:new THREE.Vector3(0.6,0,0.5), yaw:Math.PI, sit:false, minT:20, maxT:35, w:0.7,
      hint:'Posture is risk management.' },
    { name:'fridge',    label:'grabbing a cold one (sparkling water)', pos:new THREE.Vector3(1.9,0,-1.35), yaw:0, sit:false, minT:15, maxT:25, w:0.6,
      hint:'Hydration is alpha.' },
    { name:'treadmill', label:'a quick cardio reset', pos:new THREE.Vector3(-2.9,0,1.9), yaw:0, sit:false, minT:70, maxT:130, w:0.7,
      hint:'Cardio between candles. Click for a word of encouragement.' },
    { name:'shelf',     label:'checking the stats shelf', pos:new THREE.Vector3(2.9,0,-2.7), yaw:0, sit:false, minT:20, maxT:35, w:0.5,
      hint:'The ledger, in one glance. Click it for the summary.' },
    { name:'dartboard', label:'throwing darts', pos:new THREE.Vector3(-2.9,0,-2.7), yaw:0, sit:false, minT:30, maxT:55, w:0.5,
      hint:'Aim small, miss small.' },
    { name:'bookshelf', label:'browsing the trading library', pos:new THREE.Vector3(3.05,0,1.5), yaw:-Math.PI/2, sit:false, minT:45, maxT:80, w:0.7,
      hint:'A shelf of market wisdom. Click it for a tip.' },
  ];
  var OBSTACLES = [
    {x:0.15,z:-1.75,r:0.6}, {x:1.85,z:1.3,r:0.45}, {x:-2.35,z:0.85,r:0.9},
    {x:-1.62,z:1.45,r:0.26}, {x:-1.1,z:2.35,r:0.3},
    {x:1.9,z:-1.82,r:0.35}, {x:-2.2,z:-1.72,r:0.3}, {x:-2.9,z:2.2,r:0.5},
    {x:1.3,z:-2.95,r:0.55}, {x:3.0,z:-0.85,r:0.35}, {x:3.15,z:-1.7,r:0.35},
    {x:1.1,z:-0.3,r:0.65}, {x:3.45,z:1.5,r:0.5},
  ];
  var HUB = {x:0, z:0.35};
  function clampToRoom(pos){
    pos.x = clamp(pos.x, -(ROOM_X-0.25), ROOM_X-0.25);
    pos.z = clamp(pos.z, -(ROOM_Z-0.25), ROOM_Z-0.2);
    OBSTACLES.forEach(function(o){
      var dx=pos.x-o.x, dz=pos.z-o.z, d=Math.hypot(dx,dz);
      if (d<o.r && d>0.0001){ var push=o.r-d; pos.x+=dx/d*push; pos.z+=dz/d*push; }
    });
  }
  function segBlocked(ax,az,bx,bz){
    for (var i=0;i<OBSTACLES.length;i++){
      var o=OBSTACLES[i];
      for (var tI=0.15; tI<=0.85; tI+=0.1){
        var px=ax+(bx-ax)*tI, pz=az+(bz-az)*tI;
        if (Math.hypot(px-o.x,pz-o.z) < o.r+0.22) return true;
      }
    }
    return false;
  }
  function routeTo(target){
    var fx=char3.position.x, fz=char3.position.z;
    if (!segBlocked(fx,fz,target.x,target.z)) return [{x:target.x,z:target.z}];
    return [{x:HUB.x,z:HUB.z},{x:target.x,z:target.z}];
  }
  function stationIdx(name){ for (var i=0;i<STATIONS.length;i++){ if (STATIONS[i].name===name) return i; } return 0; }
  // pseudo-station for user click-to-walk destinations
  var FREE_STATION = { name:'free', label:'posted up where you sent him', yaw:null, sit:false, minT:4, maxT:7, hint:null, w:0 };
  function curStation(){ return auto.free ? FREE_STATION : STATIONS[auto.stationIdx]; }
  function pickNextStation(excludeIdx){
    var focusOn = focusMode;
    var sleeping = sleepActive();
    var weights = STATIONS.map(function(st,i){
      if (i===excludeIdx) return 0.0001;
      if (sleeping) return st.name==='bed' ? 100 : 0.0001;
      if (focusOn) return st.name==='desk' ? 100 : 0.0001;
      var w = st.w||1;
      if (marketOpen()){ if (st.name==='desk') w*=3.2; if (st.name==='tv') w*=1.5; }
      else { if (st.name==='bed'||st.name==='coffee') w*=1.6; }
      var m = state.moodScore;
      if (m<-0.2 && (st.name==='coffee'||st.name==='window'||st.name==='whiteboard')) w*=1.8;
      if (m>0.4 && (st.name==='desk'||st.name==='chartwall')) w*=1.5;
      // low motives pull him toward the station that restores them, Sims-style
      if (state.needs.energy<35 && (st.name==='bed'||st.name==='treadmill'||st.name==='weights')) w*=3;
      if (state.needs.focus<35 && st.name==='coffee') w*=3;
      return w;
    });
    var total = weights.reduce(function(a,b){return a+b;},0);
    var roll = Math.random()*total;
    for (var i=0;i<weights.length;i++){ roll-=weights[i]; if (roll<=0) return i; }
    return 0;
  }

  // ---------------------------------------------------------- animation state
  var clock = new THREE.Clock();
  var mode = 'auto'; // auto | user
  var auto = { phase:'idling', stationIdx:0, timer:0, targetDur:10, path:[] };
  var sitAmount = 1;      // start seated at desk
  var walkPhase = 0;
  var userIdleTimer = 0;
  var gesture = null;
  var focusMode = false;
  var wakeUntilT = 0;
  var displayColor = new THREE.Color(0x58a6ff);
  var lastBlinkT = 0, nextBlinkGap = 3, lastMoodBand = 'neutral';
  var zzzAcc = 0;
  var deskSubPose = 'typing';
  var deskSubTimer = 0;
  var SPEED = 1.15;
  var needsUiAcc = 0;
  var poseSaveAcc = 0;

  function sleepActive(){ return sleepTime() && clock.getElapsedTime() > wakeUntilT; }
  function wake(){ wakeUntilT = clock.getElapsedTime() + 90; }
  // Character front is -Z: positive hip X-rotation swings the thigh FORWARD.
  function setLegPose(leg, tt){ leg.hip.rotation.x = Math.PI/2*tt; leg.knee.rotation.x = -Math.PI/2*tt; }
  function resetPose(){
    headGroup.rotation.set(0,0,0);
    armL.rotation.set(0,0,0);
    armR.rotation.set(0,0,0);
    torso.rotation.x = 0;
  }

  // ---------------------------------------------------------- held props & toys
  // Reparent a prop into a hand grip; restore it to its home spot on release.
  var held = { R:null, L:null };
  function holdProp(side, obj, px, py, pz, rx, ry, rz){
    if (held[side]===obj) return;
    releaseProp(side);
    if (!obj.userData.home){
      obj.userData.home = { parent: obj.parent, pos: obj.position.clone(), rot: obj.rotation.clone() };
    }
    (side==='R'?handR:handL).add(obj);
    obj.position.set(px,py,pz);
    obj.rotation.set(rx,ry,rz);
    obj.visible = true;
    held[side] = obj;
  }
  function releaseProp(side){
    var obj = held[side];
    if (!obj) return;
    var home = obj.userData.home;
    if (home && home.parent){
      home.parent.add(obj);
      obj.position.copy(home.pos);
      obj.rotation.copy(home.rot);
    }
    if (obj.userData.hideAtHome) obj.visible = false;
    held[side] = null;
  }
  function releaseAllProps(){ releaseProp('R'); releaseProp('L'); }

  // speakers -> one-song dance break
  var danceUntil = 0, danceNoteAcc = 0;
  function startDance(){
    wake();
    danceUntil = clock.getElapsedTime() + 6.5;
    say(['Volume up.','Market\u2019s closed, floor\u2019s open.','One-song break.'][Math.floor(Math.random()*3)], 2000);
  }

  // guitar -> a quick strum
  function strumGuitar(){
    wake();
    spawnBurst('note', 5);
    var t0 = clock.getElapsedTime();
    gesture = { start:t0, dur:0.9, apply:function(p){ armR.rotation.x=0.8+Math.sin(p*Math.PI*5)*0.35; armR.rotation.z=-0.1; } };
    say(['Still got the chords.','Acoustic alpha.'][Math.floor(Math.random()*2)], 2000);
  }

  // bin -> paper-toss minigame; make-rate rides his mood
  var paperFlight = null;
  function shootPaper(){
    if (paperFlight) return;
    wake();
    var t0 = clock.getElapsedTime();
    var hitChance = clamp(0.7 + 0.25*state.moodScore, 0.35, 0.95);
    var willHit = Math.random() < hitChance;
    gesture = { start:t0, dur:0.5, apply:function(p){ armR.rotation.x = 0.6+p*2.0; armR.rotation.z = -0.15*p; } };
    setTimeout(function(){
      var from = new THREE.Vector3(); handR.getWorldPosition(from);
      var to = new THREE.Vector3(binG.position.x, 0.2, binG.position.z);
      if (!willHit){ to.x += 0.17; to.z += 0.1; }
      var mid = from.clone().add(to).multiplyScalar(0.5);
      mid.y = Math.max(from.y, 0.3) + 0.55;
      paperFlight = { t0: clock.getElapsedTime(), dur:0.75, from:from, mid:mid, to:to, hit:willHit, bounced:false };
      paperBall.visible = true;
    }, 380);
  }
  function updatePaperFlight(t, dt){
    if (!paperFlight) return;
    var pf = paperFlight;
    var pp = (t-pf.t0)/pf.dur;
    if (pp>=1){
      if (pf.hit){
        paperBall.visible = false; paperFlight = null;
        spawnBurst('spark', 4, {x:pf.to.x, y:pf.to.y+0.1, z:pf.to.z});
        say(['Buckets.','Count it.','Three points \u2014 like my R-multiples.'][Math.floor(Math.random()*3)], 2000);
      } else if (!pf.bounced){
        pf.bounced = true; pf.t0 = t; pf.dur = 0.4;
        pf.from = pf.to.clone();
        pf.to = new THREE.Vector3(pf.to.x+0.22, 0.035, pf.to.z+0.2);
        pf.mid = pf.from.clone().lerp(pf.to, 0.5); pf.mid.y = 0.22;
      } else {
        paperBall.visible = false; paperFlight = null;
        say(['Rimmed out.','Even paper trades miss sometimes.'][Math.floor(Math.random()*2)], 2000);
      }
      return;
    }
    var q1 = pf.from.clone().lerp(pf.mid, pp), q2 = pf.mid.clone().lerp(pf.to, pp);
    paperBall.position.copy(q1.lerp(q2, pp));
    paperBall.rotation.x += dt*9; paperBall.rotation.z += dt*7;
  }

  // mini hoop -> basketball shot; make-rate rides his mood
  var hoopFlight = null;
  function hoopRimPoint(){ return new THREE.Vector3(hoopG.position.x-0.19, 1.38, hoopG.position.z); }
  function shootHoop(){
    if (hoopFlight) return;
    wake();
    var t0 = clock.getElapsedTime();
    var hitChance = clamp(0.72 + 0.20*state.moodScore, 0.4, 0.92);
    var willHit = Math.random() < hitChance;
    gesture = { start:t0, dur:0.5, apply:function(p){ armR.rotation.x = 0.4+p*2.1; armR.rotation.z = -0.12*p; } };
    setTimeout(function(){
      var from = new THREE.Vector3(); handR.getWorldPosition(from);
      var to = hoopRimPoint();
      if (!willHit){ to.x += 0.13; to.z += 0.1; to.y += 0.05; }
      var mid = from.clone().add(to).multiplyScalar(0.5);
      mid.y = Math.max(from.y, 1.1) + 0.7;
      hoopFlight = { t0: clock.getElapsedTime(), dur:0.8, from:from, mid:mid, to:to, hit:willHit, bounced:false };
      basketBall.visible = true;
    }, 380);
  }
  function updateHoopFlight(t, dt){
    if (!hoopFlight) return;
    var hf = hoopFlight;
    var pp = (t-hf.t0)/hf.dur;
    if (pp>=1){
      if (hf.hit){
        basketBall.visible = false; hoopFlight = null;
        spawnBurst('spark', 4, {x:hf.to.x, y:hf.to.y+0.12, z:hf.to.z});
        say(['Bucket.','Nothing but net.','From downtown.'][Math.floor(Math.random()*3)], 2000);
      } else if (!hf.bounced){
        hf.bounced = true; hf.t0 = t; hf.dur = 0.45;
        hf.from = hf.to.clone();
        hf.to = new THREE.Vector3(hf.to.x+0.2, 0.08, hf.to.z+0.25);
        hf.mid = hf.from.clone().lerp(hf.to, 0.5); hf.mid.y = hf.from.y;
      } else {
        basketBall.visible = false; hoopFlight = null;
        say(['Rimmed out.','No good.'][Math.floor(Math.random()*2)], 2000);
      }
      return;
    }
    var q1 = hf.from.clone().lerp(hf.mid, pp), q2 = hf.mid.clone().lerp(hf.to, pp);
    basketBall.position.copy(q1.lerp(q2, pp));
    basketBall.rotation.x += dt*9; basketBall.rotation.z += dt*7;
  }

  // floor click -> walk order, with a fading target ring
  var walkMarkerT = -1;
  function showWalkMarker(x, z){
    walkMarker.position.x = x; walkMarker.position.z = z;
    walkMarker.scale.set(1,1,1);
    walkMarkerT = clock.getElapsedTime();
  }
  function updateWalkMarker(t){
    if (walkMarkerT<0) return;
    var age = t - walkMarkerT;
    if (age>1.3){ walkMarker.material.opacity = 0; walkMarkerT = -1; return; }
    walkMarker.material.opacity = Math.max(0, 0.85 - age*0.65);
    walkMarker.scale.setScalar(1 + age*0.7);
  }
  function walkToPoint(p){
    releaseAllProps();
    wake();
    mode = 'auto';
    var v = new THREE.Vector3(p.x, 0, p.z);
    clampToRoom(v);
    auto = { phase:'walking', free:true, stationIdx:auto.stationIdx, timer:0, targetDur:0, path:routeTo(v) };
    showWalkMarker(v.x, v.z);
    say(['On my way.','Roger.','Moving.'][Math.floor(Math.random()*3)], 1400);
  }

  function goTo(name){
    mode = 'auto';
    releaseAllProps();
    var idx = stationIdx(name);
    var stn = STATIONS[idx];
    var gdx = char3.position.x - stn.pos.x, gdz = char3.position.z - stn.pos.z;
    if (auto.stationIdx===idx && auto.phase==='idling' && Math.hypot(gdx,gdz)<0.2){
      auto.timer = 0; // already there — just refresh the dwell timer
      return;
    }
    auto = { phase:'walking', stationIdx:idx, timer:0, targetDur:0, path:routeTo(stn.pos) };
  }
  function arriveAtStation(){
    var st = curStation();
    auto.phase='idling'; auto.timer=0;
    auto.targetDur = st.minT + Math.random()*(st.maxT-st.minT);
    // pick up the props that belong to this activity (returned on leave)
    if (st.name==='coffee'){
      holdProp('R', mug, 0,0.02,-0.03, 0,0,0);
      holdProp('L', newsG, 0,0.05,-0.08, 0.85,0,0.15);
    } else if (st.name==='fridge'){
      holdProp('R', fridgeCan, 0,0.03,-0.02, 0,0,0);
    } else if (st.name==='phone'){
      holdProp('R', phoneProp, 0,0.02,-0.04, 0.85,0,0);
    } else if (st.name==='whiteboard'){
      holdProp('R', markerProp, 0,-0.01,-0.05, -Math.PI/2,0,0);
    } else if (st.name==='console'){
      holdProp('R', ctrlProp, 0,0.02,-0.04, 1.1,0,0);
    } else if (st.name==='weights'){
      holdProp('R', dumbbell, 0,0.02,-0.03, 0,0,0);
    }
    if (st.hint && Math.random()<0.4 && !sleepActive()) say(st.hint, 3200);
  }

  function poseForStation(name, t){
    if (name==='desk'){
      if (deskSubPose==='lean'){
        armL.rotation.x=-2.6; armL.rotation.z=0.5;   // hands behind head (up + back)
        armR.rotation.x=-2.6; armR.rotation.z=-0.5;
        headGroup.rotation.x=0.12;
      } else if (deskSubPose==='sip'){
        armR.rotation.x=2.2; armR.rotation.z=-0.25;  // can to mouth (up + forward)
        armL.rotation.x=1.15;
        headGroup.rotation.x=0.1;
      } else if (deskSubPose==='glance'){
        armL.rotation.x=1.15; armR.rotation.x=1.15;
        headGroup.rotation.y=Math.sin(t*1.6)*0.5;
      } else {
        var speedUp = focusMode ? 14 : 9;
        var bob = Math.sin(t*speedUp)*0.12;
        armL.rotation.x=1.15+bob; armR.rotation.x=1.15-bob; // reach forward to keyboard
      }
    } else if (name==='whiteboard'){
      armR.rotation.x=1.3+Math.sin(t*4)*0.25; armR.rotation.z=0.15; armL.rotation.x=0.3;
    } else if (name==='chartwall'){
      armL.rotation.x=0.25; armR.rotation.x=0.25; armL.rotation.z=0.3; armR.rotation.z=-0.3;
      headGroup.rotation.y=Math.sin(t*1.2)*0.35;
    } else if (name==='tv'){
      armL.rotation.z=0.25; armR.rotation.z=-0.25;
      headGroup.rotation.x=0.28;                     // looking UP at the wall TV
    } else if (name==='coffee'){
      var cyc=(Math.sin(t*1.1)+1)/2;
      armR.rotation.x=0.3+cyc*1.6; armR.rotation.z=0.1;  // mug in hand, sipping
      armL.rotation.x=1.25; armL.rotation.z=0.1;         // Benzinga paper held up
      headGroup.rotation.x=-0.12;                        // reading it
    } else if (name==='window'){
      armL.rotation.x=0.15; armR.rotation.x=0.15; armL.rotation.z=0.35; armR.rotation.z=-0.35;
      headGroup.rotation.y=Math.sin(t*0.7)*0.15;
    } else if (name==='calendar'){
      armR.rotation.x=1.5; armR.rotation.z=0.1;      // pointing at the calendar
      headGroup.rotation.x=0.1;
    } else if (name==='bed'){
      if (sleepActive()){ headGroup.rotation.x=0.5; armL.rotation.x=0.05; armR.rotation.x=0.05; }
      else { armL.rotation.z=0.15; armR.rotation.z=-0.15; headGroup.rotation.x=0.14+Math.sin(t*0.8)*0.03; }
    } else if (name==='weights'){
      var curl=(Math.sin(t*4)+1)/2;
      armR.rotation.x=0.3+curl*1.3; armL.rotation.x=0.3+curl*1.1;
    } else if (name==='console'){
      armR.rotation.x=1.0; armR.rotation.z=0.1; armL.rotation.x=1.0; armL.rotation.z=-0.1;
      headGroup.rotation.x=0.12;
    } else if (name==='phone'){
      armR.rotation.x=1.4; armR.rotation.z=0.15;     // phone held up in front
      headGroup.rotation.x=-0.25;                    // eyes down at the phone
    } else if (name==='stretch'){
      var s=(Math.sin(t*1.5)+1)/2;
      armL.rotation.x=-2.6*s; armR.rotation.x=-2.6*s; armL.rotation.z=0.2; armR.rotation.z=-0.2;
    } else if (name==='fridge'){
      armR.rotation.x=1.1; headGroup.rotation.x=-0.2; // reaching in, looking down
    } else if (name==='free'){
      armL.rotation.x=Math.sin(t*1.3)*0.07; armR.rotation.x=-Math.sin(t*1.3)*0.07;
      headGroup.rotation.y=Math.sin(t*0.6)*0.25;      // idle look-around at the user's spot
    } else if (name==='treadmill'){
      var jog = Math.sin(t*8)*0.5;
      legL.hip.rotation.x=jog; legR.hip.rotation.x=-jog;
      armL.rotation.x=-jog*0.8; armR.rotation.x=jog*0.8;
    } else if (name==='shelf'){
      headGroup.rotation.x=0.08; armL.rotation.x=0.15; armR.rotation.x=0.15;
    } else if (name==='dartboard'){
      armR.rotation.x=0.9+Math.sin(t*3)*0.3; armR.rotation.z=-0.1;
    } else if (name==='bookshelf'){
      armR.rotation.x=1.1+Math.sin(t*2.2)*0.15; armR.rotation.z=0.1; armL.rotation.x=0.2;
      headGroup.rotation.x=0.1; headGroup.rotation.y=Math.sin(t*0.8)*0.12;
    }
  }

  // ---------------------------------------------------------- interactivity
  var raycaster = new THREE.Raycaster();
  var pointerNDC = new THREE.Vector2();
  var targets = [];
  function registerTarget(root, def){
    root.userData.arbyT = def;
    targets.push(root);
  }
  function stationQuip(){
    var st = curStation();
    var pool;
    if (mode==='auto' && auto.phase==='idling' && st){
      if (st.name==='desk') pool = ['Charting '+mainTicker()+'.', 'Screens are hot.', 'Logging everything. Always.'];
      else if (st.name==='whiteboard') pool = ['Playbook v'+(state.playbookVersion!=null?state.playbookVersion:'?')+'. Rule one: '+(state.bullets[0]||'survive').toLowerCase()+'.'];
      else if (st.name==='coffee') pool = ['Refuel, then re-attack.','Coffee first. Entries second.'];
      else if (st.name==='bed') pool = sleepActive() ? ['zzz\u2026'] : ['Rest is a position.','Recovery is part of the system.'];
      else if (st.name==='weights') pool = ['One more set.','Strong body, steady hands.'];
      else if (st.name==='console') pool = ['One more run.','High score, then back to the tape.'];
      else if (st.name==='phone') pool = ['Scrolling $'+mainTicker()+' chatter.','Sentiment is a contrarian indicator.'];
      else if (st.name==='fridge') pool = ['Hydration is alpha.','Zero-sugar, zero-slippage.'];
      else if (st.name==='stretch') pool = ['Posture is risk management.'];
      else if (st.name==='bookshelf') pool = ['Rereading my margin notes.','Fewer trades, fewer pages.'];
    }
    return pool && pool.length ? pool[Math.floor(Math.random()*pool.length)] : null;
  }
  function charQuip(){
    var roll = Math.random();
    var sq = stationQuip();
    if (sq && roll<0.35) return sq;
    if (roll<0.7){ var f = factQuips(); return f[Math.floor(Math.random()*f.length)]; }
    var m = moodFor(state.moodScore);
    return m.quips[Math.floor(Math.random()*m.quips.length)];
  }
  var pokeCount = 0, lastPokeT = 0;
  registerTarget(char3, { label:function(){ return 'Talk to Arby'; },
    act:function(){
      var tn = clock.getElapsedTime();
      pokeCount = (tn-lastPokeT<1.8) ? pokeCount+1 : 1;
      lastPokeT = tn;
      if (pokeCount>=4){
        pokeCount = 0;
        say(['Okay, okay \u2014 I\u2019m working here.','Poke the ledger, not me.','Careful \u2014 I bite spreads.'][Math.floor(Math.random()*3)], 2400);
        gesture = { start:tn, dur:0.9, apply:function(p){ headGroup.rotation.y = Math.sin(p*Math.PI*4)*0.4; } };
      } else {
        say(charQuip());
        gesture = { start:tn, dur:0.8, apply:function(p){
          var s = Math.sin(p*Math.PI);
          armL.rotation.x = 2.4*s; armL.rotation.z = 0.3*Math.sin(p*Math.PI*3);
        }};
      }
    } });
  registerTarget(monitorRoots.main, { label:function(){ return 'TradingView \u2014 '+state.slotTickers.main+' chart \u2197'; },
    act:function(){ window.open('https://www.tradingview.com/chart/?symbol='+encodeURIComponent(state.slotTickers.main),'_blank','noopener'); } });
  registerTarget(monitorRoots.left, { label:function(){ return 'Yahoo Finance \u2014 '+state.slotTickers.left+' \u2197'; },
    act:function(){ window.open('https://finance.yahoo.com/quote/'+encodeURIComponent(state.slotTickers.left),'_blank','noopener'); } });
  registerTarget(monitorRoots.right, { label:function(){ return 'Yahoo Finance \u2014 '+state.slotTickers.right+' \u2197'; },
    act:function(){ window.open('https://finance.yahoo.com/quote/'+encodeURIComponent(state.slotTickers.right),'_blank','noopener'); } });
  registerTarget(tvG, { label:function(){ return LINKS.cnbc.label(); },
    act:function(){ window.open(LINKS.cnbc.url(),'_blank','noopener'); } });
  registerTarget(newsG, { label:function(){ return LINKS.benzinga.label(); },
    act:function(){ window.open(LINKS.benzinga.url(),'_blank','noopener'); } });
  registerTarget(windowG, { label:function(){ return LINKS.finviz.label(); },
    act:function(){ window.open(LINKS.finviz.url(),'_blank','noopener'); } });
  registerTarget(corkMesh, { label:function(){ return LINKS.stocktwits.label(); },
    act:function(){ window.open(LINKS.stocktwits.url(),'_blank','noopener'); } });
  registerTarget(calMesh, { label:function(){ return LINKS.econcal.label(); },
    act:function(){ window.open(LINKS.econcal.url(),'_blank','noopener'); } });
  registerTarget(posterMesh, { label:function(){ return 'Equity curve \u2014 jump to Performance'; },
    act:function(){ scrollToCard(/performance/i, 'Performance card is on this page \u2014 scroll down.'); } });
  registerTarget(wbG, { label:function(){ return 'Playbook v'+(state.playbookVersion!=null?state.playbookVersion:'?')+' \u2014 read it below'; },
    act:function(){ scrollToCard(/playbook/i, 'Playbook is on this page \u2014 scroll down.'); } });
  registerTarget(fridgeG, { label:function(){ return 'Mini fridge'; },
    act:function(){ say(['Hydration is alpha.','Emergency electrolytes.'][Math.floor(Math.random()*2)]); } });
  registerTarget(plantG, { label:function(){ return 'Desk plant'; },
    act:function(){ say(['Photosynthesis > FOMO.','My only long-term hold.'][Math.floor(Math.random()*2)]); } });
  registerTarget(lampG, { label:function(){ return 'Desk lamp'; },
    act:function(){ say((dayPhase()==='night'||dayPhase()==='dusk') ? 'Burning the midnight oil.' : 'Don’t need it yet — plenty of daylight.'); } });
  registerTarget(clockG, { label:function(){ return 'Desk clock'; },
    act:function(){ say('It\u2019s '+etClockStr()+' \u2014 market\u2019s '+(marketOpen()?'open. Lock in.':'closed. Prep time.')); } });
  registerTarget(mug, { label:function(){ return held.R===mug ? 'He\u2019s got his coffee' : 'Coffee run \u2014 send him'; },
    act:function(){ goTo('coffee'); say(['Coffee run.','Caffeine protocol engaged.'][Math.floor(Math.random()*2)], 1600); } });
  registerTarget(binG, { label:function(){ return 'Paper toss \u2014 bet he makes it'; },
    act:function(){ shootPaper(); } });
  registerTarget(spkG, { label:function(){ return 'Speakers \u2014 drop a beat'; },
    act:function(){ startDance(); } });
  registerTarget(treadmillG, { label:function(){ return 'Treadmill \u2014 send him for cardio'; },
    act:function(){ goTo('treadmill'); say(['Cardio between candles.','Sweat now, size later.'][Math.floor(Math.random()*2)], 1600); } });
  registerTarget(shelfG, { label:function(){ return 'Stats shelf'; },
    act:function(){ say(shelfQuip()); } });
  registerTarget(dartboardG, { label:function(){ return 'Dartboard \u2014 Shadow Ledger, rejected candidates \u2197'; },
    act:function(){ scrollToCard(/shadow ledger/i, 'Shadow Ledger card is on this page \u2014 scroll down.'); } });
  registerTarget(bedG, { label:function(){ return 'Bed \u2014 send him to rest'; },
    act:function(){ goTo('bed'); say(['Time to rack out.','Sleep corner calling.'][Math.floor(Math.random()*2)], 1600); } });
  registerTarget(nightG, { label:function(){ return 'Nightstand lamp'; },
    act:function(){ say('Reading lamp. The only book I read is the playbook.'); } });
  registerTarget(guitarG, { label:function(){ return 'Guitar \u2014 he\u2019ll strum'; },
    act:function(){ strumGuitar(); } });
  registerTarget(hoopG, { label:function(){ return 'Mini hoop \u2014 shoot'; },
    act:function(){ shootHoop(); } });
  registerTarget(weightsG, { label:function(){ return 'Weight rack \u2014 send him to lift'; },
    act:function(){ goTo('weights'); say(['Pump it.','Lightweight, heavy conviction.'][Math.floor(Math.random()*2)], 1600); } });
  registerTarget(consoleG, { label:function(){ return 'Game console \u2014 fire it up'; },
    act:function(){ goTo('console'); say(['GG.','One more run, then back to the tape.'][Math.floor(Math.random()*2)], 1600); } });
  registerTarget(soundbar, { label:function(){ return 'Soundbar \u2014 drop a beat'; },
    act:function(){ startDance(); } });
  registerTarget(neonSign, { label:function(){ return 'Neon \u2014 the vibe'; },
    act:function(){ say(['This room has range.','Man cave, maximum gains.'][Math.floor(Math.random()*2)]); } });
  registerTarget(deskG, { label:function(){ return 'Battlestation \u2014 back to the screens'; },
    act:function(){ goTo('desk'); say(['Deep in the tape.','Monitors up.','One more scan pass.'][Math.floor(Math.random()*3)], 1600); } });
  registerTarget(chairG, { label:function(){ return 'Gaming chair \u2014 take a seat'; },
    act:function(){ goTo('desk'); say(['Best seat in the cave.','Lumbar support is alpha.'][Math.floor(Math.random()*2)], 1600); } });
  registerTarget(coffeeG, { label:function(){ return 'Coffee table \u2014 fuel up'; },
    act:function(){ goTo('coffee'); say(['Coffee + Benzinga.','Refuel, re-attack.'][Math.floor(Math.random()*2)], 1600); } });
  registerTarget(couchG, { label:function(){ return 'Couch \u2014 kick back & watch the game'; },
    act:function(){ goTo('tv'); say(['Movie night is market research.','Kick back. The tape can wait a minute.'][Math.floor(Math.random()*2)], 1600); } });
  registerTarget(bookshelfG, { label:function(){ return 'Bookshelf \u2014 trading reads'; },
    act:function(){ say(['Shelf full of market classics.','Best returns come from the pages first.'][Math.floor(Math.random()*2)]); } });
  registerTarget(riskPoster, { label:function(){ return 'RISK FIRST \u2014 house rule'; },
    act:function(){ say(['Rule one: don\u2019t blow up.','Risk first. Returns second.'][Math.floor(Math.random()*2)]); } });
  registerTarget(snackBowl, { label:function(){ return 'Snack bowl \u2014 grab a chip'; },
    act:function(){ say(['Carbs before candles.','Snack position: full.'][Math.floor(Math.random()*2)]); } });

  function pickTarget(e){
    var rect = renderer.domElement.getBoundingClientRect();
    pointerNDC.x = ((e.clientX-rect.left)/rect.width)*2-1;
    pointerNDC.y = -((e.clientY-rect.top)/rect.height)*2+1;
    raycaster.setFromCamera(pointerNDC, camera);
    var hits = raycaster.intersectObjects(targets, true);
    if (!hits.length) return null;
    var obj = hits[0].object;
    while (obj){
      if (obj.userData && obj.userData.arbyT) return obj.userData.arbyT;
      obj = obj.parent;
    }
    return null;
  }
  function pickFloorPoint(e){
    var rect = renderer.domElement.getBoundingClientRect();
    pointerNDC.x = ((e.clientX-rect.left)/rect.width)*2-1;
    pointerNDC.y = -((e.clientY-rect.top)/rect.height)*2+1;
    raycaster.setFromCamera(pointerNDC, camera);
    var hits = raycaster.intersectObjects([floor, rug]);
    return hits.length ? hits[0].point : null;
  }
  renderer.domElement.addEventListener('pointermove', function(e){
    if (Object.keys(pinchPointers).length>0){ tip.style.display='none'; return; } // mid-drag: no hover tooltip
    var tgt = pickTarget(e);
    var rect, lx, ly;
    if (tgt){
      renderer.domElement.style.cursor='pointer';
      tip.textContent = tgt.label();
      tip.style.display='block';
      rect = wrap.getBoundingClientRect();
      lx = clamp(e.clientX-rect.left+14, 0, rect.width-160);
      ly = clamp(e.clientY-rect.top+10, 0, rect.height-30);
      tip.style.left = lx+'px'; tip.style.top = ly+'px';
    } else if (pickFloorPoint(e)){
      renderer.domElement.style.cursor='crosshair';
      tip.textContent = 'Send Arby here';
      tip.style.display='block';
      rect = wrap.getBoundingClientRect();
      lx = clamp(e.clientX-rect.left+14, 0, rect.width-160);
      ly = clamp(e.clientY-rect.top+10, 0, rect.height-30);
      tip.style.left = lx+'px'; tip.style.top = ly+'px';
    } else {
      renderer.domElement.style.cursor='default';
      tip.style.display='none';
    }
  });
  renderer.domElement.addEventListener('pointerleave', function(){ tip.style.display='none'; });
  renderer.domElement.addEventListener('click', function(e){
    if (dragMoved){ dragMoved = false; return; } // that was a camera drag, not a click
    wrap.focus();
    wake();
    hint.classList.add('hide');
    var tgt = pickTarget(e);
    if (tgt){ tgt.act(); return; }
    var fp = pickFloorPoint(e);
    if (fp) walkToPoint(fp);
  });

  function anchorBubbleToHead(){
    var headPos = new THREE.Vector3();
    headGroup.getWorldPosition(headPos);
    headPos.y += 0.4;
    headPos.project(camera);
    bubble.style.left = clamp((headPos.x*0.5+0.5)*RW, 60, RW-60)+'px';
    bubble.style.top = clamp((-headPos.y*0.5+0.5)*RH, 30, RH-20)+'px';
  }
  sayAnchored = anchorBubbleToHead;

  // keyboard driving
  var keys = {};
  var DRIVE_KEYS = ['arrowup','arrowdown','arrowleft','arrowright','w','a','s','d'];
  wrap.addEventListener('keydown', function(e){
    var k = e.key.toLowerCase();
    if (DRIVE_KEYS.indexOf(k)>=0){
      e.preventDefault();
      if (mode!=='user') releaseAllProps();
      keys[k]=true; mode='user'; userIdleTimer=0;
      wake(); hint.classList.add('hide');
    }
  });
  wrap.addEventListener('keyup', function(e){ keys[e.key.toLowerCase()]=false; });
  wrap.addEventListener('focus', function(){ wrap.classList.add('focused'); });
  wrap.addEventListener('blur', function(){
    wrap.classList.remove('focused');
    Object.keys(keys).forEach(function(k){ keys[k]=false; });
  });
  setTimeout(function(){ hint.classList.add('hide'); }, 9000);

  // run-loop observer: purely passive read of the app's own "Running" indicator.
  // Registered on DOM-ready because this script executes mid-parse, before the
  // #running element further down the page exists.
  function initRunObserver(){
    var runningEl = document.getElementById('running');
    if (!runningEl || !window.MutationObserver) return;
    var mo = new MutationObserver(function(){
      var vis = runningEl.style.display === 'block';
      if (vis && !focusMode){
        focusMode = true;
        goTo('desk');
        say('A loop is running \u2014 on it.', 3000);
      } else if (!vis && focusMode){
        focusMode = false;
        say('Run finished. Updating my read\u2026', 2500);
        setTimeout(syncLive, 1500);
      }
    });
    mo.observe(runningEl, {attributes:true, attributeFilter:['style']});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initRunObserver);
  else initRunObserver();

  // ---------------------------------------------------------- charts API
  function setChartData(slot, bars, ticker, levels){
    var sc = chartScreens[slot];
    if (!sc) return;
    if (ticker){ sc.ticker = ticker; state.slotTickers[slot] = String(ticker).toUpperCase(); }
    drawChart(sc.ctx, sc.cv.width, sc.cv.height, {ticker:sc.ticker, bars:bars, levels:levels||null});
    sc.tex.needsUpdate = true;
  }
  function setChartFromPicks(picks, d){
    var slots = ['main','left','right'];
    var real = (d && d.pick_bars) || {};
    (picks||[]).slice(0,3).forEach(function(pick,i){
      var slot = slots[i];
      if (!chartScreens[slot]) return;
      if (pick.entry_price==null || pick.stop_price==null || pick.target_price==null) return;
      // Prefer real intraday bars when the API supplied them; otherwise fall
      // back to a deterministic synthetic trace so the chart is never blank.
      var live = real[pick.ticker] && real[pick.ticker].length;
      var bars = live ? real[pick.ticker] : synthPickBars(pick, 26);
      setChartData(slot, bars, pick.ticker, {
        entry:+pick.entry_price, stop:+pick.stop_price, target:+pick.target_price,
        direction:pick.direction, size_r:pick.size_r, conviction:pick.conviction,
      });
    });
  }
  function setNoTrade(reason){
    var sc = chartScreens.main;
    if (!sc) return;
    drawNoTrade(sc.ctx, sc.cv.width, sc.cv.height, reason);
    sc.tex.needsUpdate = true;
  }

  applyStatus3D = function(d){
    var plan = d.plan;
    if (plan){
      if (plan.no_trade) setNoTrade(plan.no_trade_reason);
      else if ((plan.picks||[]).length) setChartFromPicks(plan.picks, d);
    }
    if (d.equity_curve && d.equity_curve.length>1){
      drawEquity(postCtx, 220, 150, d.equity_curve);
      postTex.needsUpdate = true;
    }
    drawWhiteboard();
    drawShelf();
    rebuildTape();
    drawFace();
  };
  // re-apply boot data to the now-live 3D surfaces
  if (state.status) applyStatus3D(state.status);

  // ---------------------------------------------------------- gestures & fx
  function winFx(fromData){
    var t = clock.getElapsedTime();
    gesture = { start:t, dur:1.0, apply:function(p){
      armR.rotation.x = Math.PI*0.9*Math.sin(p*Math.PI);
    }};
    spawnBurst('win', 6);
    if (fromData) say(['BOOKED. +R in the ledger.','That\u2019s the system working.','Graded green. On to the next.'][Math.floor(Math.random()*3)]);
  }
  function lossFx(fromData){
    var t = clock.getElapsedTime();
    gesture = { start:t, dur:1.4, apply:function(p){
      var s = Math.sin(p*Math.PI);
      armR.rotation.x = 2.5*s; armR.rotation.z = -0.35*s;
      headGroup.rotation.x = -0.3*s;
    }};
    spawnBurst('loss', 4);
    if (fromData) say(['Stopped. Loss logged, lesson pending.','Paid tuition. Moving on.','That one\u2019s going in the journal.'][Math.floor(Math.random()*3)]);
  }

  // ---------------------------------------------------------- main loop
  // All per-frame logic lives in tick(dt, t) so it can be driven manually
  // (tests, debugging) as well as by requestAnimationFrame.
  function tick(dt, t){

    // environment
    var phase = dayPhase();
    if (phase!==lastPhase){ drawSky(phase); applyPhaseLighting(phase); lastPhase=phase; }
    var et = etNow();
    handMin.rotation.z = -(et.m/60)*Math.PI*2;
    handHour.rotation.z = -(((et.h%12)+et.m/60)/12)*Math.PI*2;
    tvAcc += dt;
    if (tvAcc>0.08){ drawTV(t); tvAcc=0; }

    // Sims-style motives: drain steadily, only autonomous station visits refill them.
    state.needs.energy = clamp(state.needs.energy - dt*(focusMode?1.1:0.55), 0, 100);
    state.needs.focus = clamp(state.needs.focus - dt*(marketOpen()?1.2:0.6), 0, 100);
    needsUiAcc += dt;
    if (needsUiAcc>0.25){ refreshNeeds(); needsUiAcc=0; }
    poseSaveAcc += dt;
    if (poseSaveAcc>4){ savePose(); poseSaveAcc=0; }

    // mood visuals
    var m = moodFor(state.moodScore);
    displayColor.lerp(new THREE.Color(m.color), dt*2);
    shirtMat.color.copy(displayColor);
    ledStrip.material.color.copy(displayColor);
    ledLight.color.copy(displayColor);
    ledLight.intensity = 0.55 + (focusMode ? Math.abs(Math.sin(t*7))*0.4 : Math.sin(t*2.2)*0.08);
    if (m.key!==lastMoodBand){ lastMoodBand=m.key; drawFace(); }
    plumbobMat.color.copy(displayColor);
    plumbobMat.emissive.copy(displayColor);
    plumbob.rotation.y += dt*1.4;
    plumbob.position.y = 0.62 + Math.sin(t*1.6)*0.02;

    // blink
    if (t-lastBlinkT > nextBlinkGap){
      blinkClosed = true; drawFace();
      lastBlinkT = t; nextBlinkGap = 2.4+Math.random()*3.5;
    } else if (blinkClosed && t-lastBlinkT > 0.13){
      blinkClosed = false; drawFace();
    }

    resetPose();
    charBlob.position.x = char3.position.x;
    charBlob.position.z = char3.position.z;

    var st = curStation();

    if (mode==='user'){
      var vx=0, vz=0;
      if (keys['arrowup']||keys['w']) vz-=1;
      if (keys['arrowdown']||keys['s']) vz+=1;
      if (keys['arrowleft']||keys['a']) vx-=1;
      if (keys['arrowright']||keys['d']) vx+=1;
      var moving = vx!==0||vz!==0;
      sitAmount = Math.max(0, sitAmount-dt*4);
      setLegPose(legL, sitAmount); setLegPose(legR, sitAmount);
      if (moving){
        var len=Math.hypot(vx,vz); vx/=len; vz/=len;
        char3.position.x += vx*1.5*dt; char3.position.z += vz*1.5*dt;
        clampToRoom(char3.position);
        var targetYaw = Math.atan2(-vx,-vz);
        var dy = targetYaw - char3.rotation.y;
        while (dy>Math.PI) dy-=Math.PI*2; while (dy<-Math.PI) dy+=Math.PI*2;
        char3.rotation.y += dy*0.25;
        walkPhase += dt*10;
        var swing = Math.sin(walkPhase)*0.55;
        legL.hip.rotation.x=swing; legR.hip.rotation.x=-swing;
        armL.rotation.x=-swing*0.7; armR.rotation.x=swing*0.7;
        hudActivity.textContent = 'You\u2019re driving him';
        userIdleTimer = 0;
      } else {
        armL.rotation.x=-0.05; armR.rotation.x=-0.05;
        hudActivity.textContent = 'Standing by (move him or wait)';
        userIdleTimer += dt;
        if (userIdleTimer>3){
          mode='auto';
          releaseAllProps();
          var idx = pickNextStation(auto.stationIdx);
          auto = { phase:'walking', stationIdx:idx, timer:0, targetDur:0, path:routeTo(STATIONS[idx].pos) };
        }
      }
    } else {
      auto.timer += dt;
      if (auto.phase==='walking'){
        if (sitAmount>0.02){
          sitAmount = Math.max(0, sitAmount-dt*3.5);
          setLegPose(legL, sitAmount); setLegPose(legR, sitAmount);
          hudActivity.textContent = 'Getting up\u2026';
        } else {
          setLegPose(legL, 0); setLegPose(legR, 0);
          var wp = auto.path[0];
          if (!wp){ arriveAtStation(); }
          else {
            var dx=wp.x-char3.position.x, dz=wp.z-char3.position.z;
            var dist=Math.hypot(dx,dz), step=SPEED*dt;
            if (dist<=step){
              char3.position.x=wp.x; char3.position.z=wp.z;
              auto.path.shift();
              if (!auto.path.length) arriveAtStation();
            } else {
              char3.position.x += dx/dist*step;
              char3.position.z += dz/dist*step;
              var wyaw = Math.atan2(-dx/dist,-dz/dist);
              var wd = wyaw - char3.rotation.y;
              while (wd>Math.PI) wd-=Math.PI*2; while (wd<-Math.PI) wd+=Math.PI*2;
              char3.rotation.y += wd*0.2;
              walkPhase += dt*10;
              var wsw = Math.sin(walkPhase)*0.55;
              legL.hip.rotation.x=wsw; legR.hip.rotation.x=-wsw;
              armL.rotation.x=-wsw*0.7; armR.rotation.x=wsw*0.7;
            }
            hudActivity.textContent = focusMode ? 'Priority: desk'
              : 'Auto \u00B7 walking to '+(auto.free?'your spot':st.name);
          }
        }
      } else { // idling
        // settle yaw toward the station facing (free spots keep their arrival yaw)
        if (st.yaw!=null){
          var sy = st.yaw - char3.rotation.y;
          while (sy>Math.PI) sy-=Math.PI*2; while (sy<-Math.PI) sy+=Math.PI*2;
          char3.rotation.y += sy*0.12;
        }
        if (st.sit) sitAmount = Math.min(1, sitAmount+dt*3.5);
        else sitAmount = Math.max(0, sitAmount-dt*3.5);
        setLegPose(legL, sitAmount); setLegPose(legR, sitAmount);
        // desk sub-pose cycling (the energy can physically comes along for 'sip')
        if (st.name==='desk'){
          deskSubTimer -= dt;
          if (deskSubTimer<=0){
            var poses = focusMode ? ['typing'] : ['typing','typing','typing','glance','sip','lean'];
            deskSubPose = poses[Math.floor(Math.random()*poses.length)];
            deskSubTimer = 5+Math.random()*6;
            if (deskSubPose==='sip') holdProp('R', deskCan, 0,0.03,-0.02, 0,0,0);
            else if (held.R===deskCan) releaseProp('R');
          }
        }
        poseForStation(st.name, t);
        if (st.name==='bed') state.needs.energy = Math.min(100, state.needs.energy + dt*(sleepActive()?26:14));
        if (st.name==='coffee') state.needs.focus = Math.min(100, state.needs.focus + dt*18);
        if (st.name==='treadmill') state.needs.energy = Math.min(100, state.needs.energy + dt*16);
        if (st.name==='weights') state.needs.energy = Math.min(100, state.needs.energy + dt*14);
        if (sleepActive() && st.name==='bed'){
          hudActivity.textContent = 'Asleep \u2014 market\u2019s closed, so is he';
          zzzAcc += dt;
          if (zzzAcc>3.5){ spawnBurst('zzz',1); zzzAcc=0; }
        } else {
          hudActivity.textContent = (focusMode?'Focused \u00B7 ':'Auto \u00B7 ')+st.label;
          if (auto.timer>auto.targetDur && !focusMode){
            releaseAllProps();
            var nidx = pickNextStation(auto.stationIdx);
            auto = { phase:'walking', stationIdx:nidx, timer:0, targetDur:0, path:routeTo(STATIONS[nidx].pos) };
          } else if (focusMode && st.name!=='desk' && auto.timer>1){
            goTo('desk');
          }
        }
        // occasional celebration spin when on fire
        if (st.name==='desk' && state.moodScore>0.6 && !gesture && Math.random()<0.0015){
          var baseYaw = char3.rotation.y;
          gesture = { start:t, dur:1.1, apply:function(p){ char3.rotation.y = baseYaw + p*Math.PI*2; } };
          spawnBurst('spark', 4);
        }
      }
    }

    // one-song dance break (overrides arms/head; skipped mid-walk so pathing stays clean)
    if (t < danceUntil && !(mode==='auto' && auto.phase==='walking')){
      var db = t*7;
      armL.rotation.x = 1.2+Math.sin(db)*1.2; armR.rotation.x = 1.2+Math.sin(db+Math.PI)*1.2;
      armL.rotation.z = 0.4; armR.rotation.z = -0.4;
      headGroup.rotation.y = Math.sin(db*0.5)*0.3;
      headGroup.rotation.x = 0.08*Math.sin(db);
      char3.position.y = sitAmount>0.5 ? 0 : Math.abs(Math.sin(db))*0.045;
      danceNoteAcc += dt;
      if (danceNoteAcc>0.5){ spawnBurst('note',1); danceNoteAcc=0; }
      hudActivity.textContent = 'One-song dance break';
    } else {
      char3.position.y = 0;
    }

    // posture slump on cold streaks (applied over whatever pose is active):
    // negative X pitch hunches the torso and hangs the head forward
    var slump = state.moodScore<-0.2 ? Math.min(1,(-state.moodScore-0.2)/0.8) : 0;
    torso.rotation.x += -0.16*slump;
    headGroup.rotation.x += -0.1*slump;

    if (gesture){
      var gp = (t-gesture.start)/gesture.dur;
      if (gp<=1) gesture.apply(gp);
      else gesture=null;
    }

    updateParticles(dt);
    updatePaperFlight(t, dt);
    updateHoopFlight(t, dt);
    updateWalkMarker(t);

    // camera: dolly along the fixed view direction for zoom, plus a subtle
    // idle sway (scaled with distance so it stays proportionally subtle at
    // any zoom level)
    var breatheScale = camDist/camBaseDist;
    var camBase = cameraTarget.clone().addScaledVector(camDir, camDist);
    camera.position.set(
      camBase.x + Math.sin(t*0.11)*0.05*breatheScale,
      camBase.y + Math.sin(t*0.07)*0.03*breatheScale,
      camBase.z
    );
    camera.lookAt(cameraTarget);

    renderer.render(scene, camera);
  }

  function animate(){
    requestAnimationFrame(animate);
    if (hidden) return;
    tick(Math.min(0.05, clock.getDelta()), clock.getElapsedTime());
  }

  // ---------------------------------------------------------- session pose persistence
  // Session-scoped (sessionStorage, not localStorage) so a reload picks up where he left
  // off mid-visit, but a brand-new tab/day doesn't inherit a stale position.
  var POSE_KEY = 'arby.pose.v1';
  (function restorePose(){
    try {
      var raw = sessionStorage.getItem(POSE_KEY);
      if (!raw) return;
      var p = JSON.parse(raw);
      if (typeof p.x!=='number' || typeof p.z!=='number') return;
      char3.position.x = p.x; char3.position.z = p.z;
      clampToRoom(char3.position);
      char3.rotation.y = typeof p.yaw==='number' ? p.yaw : 0;
      sitAmount = 0; // resume standing, not mid-sit-down
      auto = { phase:'idling', stationIdx:0, timer:0, targetDur:4+Math.random()*3, path:[] };
    } catch(e){}
  })();
  function savePose(){
    try { sessionStorage.setItem(POSE_KEY, JSON.stringify({x:char3.position.x, z:char3.position.z, yaw:char3.rotation.y})); } catch(e){}
  }
  window.addEventListener('pagehide', savePose);
  document.addEventListener('visibilitychange', function(){ if (document.hidden) savePose(); });

  animate();

  // ---------------------------------------------------------- public API
  window.Mascot = window.Arby = {
    registerWin: function(fromData){ state.moodScore=clamp(state.moodScore+0.25,-1,1); state.needs.focus=clamp(state.needs.focus+8,0,100); refreshHud(); refreshNeeds(); drawFace(); winFx(!!fromData); },
    registerLoss: function(fromData){ state.moodScore=clamp(state.moodScore-0.25,-1,1); state.needs.energy=clamp(state.needs.energy-8,0,100); refreshHud(); refreshNeeds(); drawFace(); lossFx(!!fromData); },
    reset: function(){ state.moodScore=0; refreshHud(); drawFace(); },
    setBlendedMood: function(s){ state.moodScore=clamp(s,-1,1); refreshHud(); drawFace(); },
    syncFromStatus: applyStatus,
    tryLiveSync: syncLive,
    setChartData: setChartData,
    setChartFromPicks: setChartFromPicks,
    setNoTrade: setNoTrade,
    say: say,
    goTo: goTo,
    walkTo: function(x,z){ walkToPoint({x:x, z:z}); },
    shootPaper: shootPaper,
    dance: startDance,
    fetchCoffee: function(){ goTo('coffee'); },
    zoomIn: function(){ camDist = clamp(camDist*0.85, CAM_MIN, CAM_MAX); },
    zoomOut: function(){ camDist = clamp(camDist*1.18, CAM_MIN, CAM_MAX); },
    resetView: resetCamera,
    _tick: tick, // manual frame driver (testing/debugging)
    _snap: function(){
      return {
        pos: [char3.position.x, char3.position.y, char3.position.z],
        yaw: char3.rotation.y,
        station: curStation().name, phase: auto.phase, free: !!auto.free,
        sitAmount: sitAmount, focus: focusMode,
        heldR: held.R===mug?'mug':held.R===deskCan?'deskCan':held.R===fridgeCan?'fridgeCan'
              :held.R===phoneProp?'phone':held.R===markerProp?'marker':held.R===ctrlProp?'controller'
              :held.R===dumbbell?'dumbbell':held.R?'other':null,
        heldL: held.L===newsG?'news':held.L?'other':null,
        paperFlying: !!paperFlight, dancing: clock.getElapsedTime()<danceUntil,
        mugWorld: (function(){ var v=new THREE.Vector3(); mug.getWorldPosition(v); return [v.x,v.y,v.z]; })(),
        camDist: camDist, camBaseDist: camBaseDist, camMin: CAM_MIN, camMax: CAM_MAX,
        camPos: [camera.position.x, camera.position.y, camera.position.z],
      };
    },
  };
})();
</script>
"""


def mascot_embed(boot: dict | None = None) -> str:
    """Return the embeddable widget HTML, optionally preceded by inlined boot
    data (used by the static dashboard, where fetch('/api/status') can't work)."""
    boot_tag = ""
    if boot is not None:
        payload = json.dumps(boot, default=str).replace("</", "<\\/")
        boot_tag = f"<script>window.MASCOT_BOOT = {payload};</script>\n"
    return boot_tag + _SNIPPET
