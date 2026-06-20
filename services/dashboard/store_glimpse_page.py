"""The visual LIVE feature-store GLIMPSE grid — a standalone page on the dashboard.

Vanilla HTML/JS (no build step), same dark theme as ``universe_coverage_page`` / ``sector_coverage_page``.
Fetches ``/api/store-glimpse`` client-side and renders the DATE x FEATURE-GROUP grid: rows are dates
(newest at top), columns are feature groups + a Total column. Each cell is a tiny box with TWO encodings —
DARKNESS = coverage fraction of the captured universe, HUE = trust status (green/amber/red/grey). Clicking a
group column header EXPANDS it to its individual features; clicking a (date x group) cell opens the
TICKER x DATE drill (``/api/store-glimpse/{group}/tickers``). Auto-refreshes so it always reflects the
current store — the immediate glimpse Ben asked for.
"""

from __future__ import annotations

STORE_GLIMPSE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Feature-store glimpse</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg:#0f1115; --panel:#171a21; --border:#262b35; --text:#d7dce2; --muted:#8b949e;
    --accent:#58a6ff; --grid:#1b2230;
    /* trust hues (full-coverage tone); darkness is applied via per-cell opacity over these */
    --trusted:#2ea043; --pending:#d68a1f; --divergent:#da3633; --ungraded:#586069;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  header { background:var(--panel); padding:14px 22px; border-bottom:1px solid var(--border); }
  header h1 { margin:0; font-size:18px; }
  header a { color:var(--accent); text-decoration:none; font-size:13px; font-weight:400; margin-left:10px; }
  .muted { color:var(--muted); }
  .wrap { padding:18px 22px; }
  .controls { display:flex; gap:10px; align-items:center; margin-bottom:14px; flex-wrap:wrap; }
  .controls button, .controls select { background:var(--panel); color:var(--text);
    border:1px solid var(--border); border-radius:6px; padding:5px 10px; font-size:13px; cursor:pointer; }
  .stats { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px; }
  .stat { background:var(--panel); border:1px solid var(--border); border-radius:6px; padding:6px 11px; font-size:12px; }
  .stat b { font-variant-numeric:tabular-nums; font-size:14px; }
  .legend { display:flex; gap:14px; align-items:center; flex-wrap:wrap; font-size:12px; margin-bottom:14px; }
  .legend .sw { display:inline-block; width:13px; height:13px; border-radius:3px; vertical-align:-2px; margin-right:4px; }
  .legend .ramp { display:inline-flex; vertical-align:-2px; }
  .legend .ramp i { width:13px; height:13px; display:inline-block; background:var(--accent); }
  /* the grid */
  .gridwrap { overflow:auto; border:1px solid var(--border); border-radius:8px; max-height:78vh; }
  table.glimpse { border-collapse:separate; border-spacing:0; font-size:11px; }
  table.glimpse th, table.glimpse td { padding:0; }
  table.glimpse thead th { position:sticky; top:0; z-index:3; background:var(--panel);
    border-bottom:1px solid var(--border); border-right:1px solid var(--grid);
    writing-mode:vertical-rl; text-orientation:mixed; transform:rotate(180deg);
    height:120px; white-space:nowrap; padding:4px 2px; cursor:pointer; color:var(--muted); font-weight:600; }
  table.glimpse thead th.corner, table.glimpse thead th.totcol { writing-mode:horizontal-tb; transform:none;
    text-align:center; vertical-align:bottom; cursor:default; }
  table.glimpse thead th.totcol { color:var(--text); cursor:pointer; }
  table.glimpse thead th .gh { display:inline-block; max-height:108px; overflow:hidden; }
  table.glimpse thead th.expanded { color:var(--accent); }
  table.glimpse thead th.featcol { background:#12151c; font-weight:400; height:120px; }
  table.glimpse td.daterow { position:sticky; left:0; z-index:2; background:var(--panel);
    border-right:1px solid var(--border); border-bottom:1px solid var(--grid);
    padding:0 8px; white-space:nowrap; font-variant-numeric:tabular-nums; text-align:right; min-width:86px; }
  table.glimpse td.cell { width:16px; height:16px; border-right:1px solid var(--grid); border-bottom:1px solid var(--grid); }
  table.glimpse td.cell .box { width:16px; height:16px; cursor:pointer; }
  table.glimpse td.totcell { border-left:1px solid var(--border); }
  .hue-trusted { background:var(--trusted); }
  .hue-pending { background:var(--pending); }
  .hue-divergent { background:var(--divergent); }
  .hue-ungraded { background:var(--ungraded); }
  /* drill overlay */
  .overlay { position:fixed; inset:0; background:rgba(0,0,0,0.6); display:none; z-index:10;
    align-items:flex-start; justify-content:center; padding:40px 20px; overflow:auto; }
  .overlay.open { display:flex; }
  .modal { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:16px 18px;
    max-width:96vw; width:1200px; }
  .modal h2 { margin:0 0 4px; font-size:16px; }
  .modal .close { float:right; cursor:pointer; color:var(--muted); font-size:18px; }
  .drillgrid { overflow:auto; max-height:70vh; border:1px solid var(--border); border-radius:6px; margin-top:10px; }
  table.drill { border-collapse:separate; border-spacing:0; font-size:11px; }
  table.drill th { position:sticky; top:0; background:var(--panel); color:var(--muted); font-weight:600;
    writing-mode:vertical-rl; transform:rotate(180deg); height:74px; padding:2px; border-bottom:1px solid var(--border); }
  table.drill th.symh { writing-mode:horizontal-tb; transform:none; text-align:left; padding:2px 8px;
    position:sticky; left:0; z-index:2; }
  table.drill td.sym { position:sticky; left:0; background:var(--panel); padding:1px 8px; white-space:nowrap;
    font-variant-numeric:tabular-nums; border-bottom:1px solid var(--grid); }
  table.drill td.b { width:14px; height:14px; border-right:1px solid var(--grid); border-bottom:1px solid var(--grid); }
  .prov-both { background:#2ea043; } .prov-stream { background:#3b82f6; }
  .prov-backfill { background:#d68a1f; } .prov-absent { background:#12151c; }
  .empty { color:var(--muted); font-style:italic; padding:14px 0; }
</style></head>
<body>
<header><h1>Feature-store glimpse
  <a href="/">&larr; dashboard</a><a href="/feature-grid">feature grid &rarr;</a>
  <a href="/universe-coverage">universe coverage &rarr;</a></h1>
  <div class="muted" id="subhead">loading…</div>
</header>
<div class="wrap">
  <div class="controls">
    <button id="refresh">↻ refresh</button>
    <label class="muted">window
      <select id="days">
        <option value="14">14d</option><option value="30" selected>30d</option>
        <option value="45">45d</option><option value="60">60d</option>
      </select></label>
    <label class="muted"><input type="checkbox" id="auto" checked> auto-refresh (30s)</label>
  </div>
  <div class="stats" id="stats"></div>
  <div class="legend">
    <span><b>darkness</b> = coverage</span>
    <span class="ramp" title="opacity ∝ fraction of universe covered">
      <i style="opacity:.15"></i><i style="opacity:.35"></i><i style="opacity:.6"></i><i style="opacity:.85"></i><i style="opacity:1"></i></span>
    <span style="margin-left:8px;"><b>color</b> = trust:</span>
    <span><span class="sw hue-trusted"></span>trusted</span>
    <span><span class="sw hue-pending"></span>pending</span>
    <span><span class="sw hue-divergent"></span>divergent</span>
    <span><span class="sw hue-ungraded"></span>ungraded</span>
    <span class="muted">· click a group header to expand · click a cell to drill into tickers</span>
  </div>
  <div id="content"><div class="empty">loading…</div></div>
</div>
<div class="overlay" id="overlay"><div class="modal">
  <span class="close" id="closeDrill">✕</span>
  <h2 id="drillTitle"></h2>
  <div class="muted" id="drillSub"></div>
  <div id="drillBody"><div class="empty">loading…</div></div>
</div></div>
<script>
const expanded = new Set();   // group names whose feature columns are revealed
let LAST = null;

function fmtInt(n){ return (n==null?"—":n.toLocaleString()); }
function pct(x){ return (x==null?"—":(100*x).toFixed(1)+"%"); }

// opacity ramp for coverage darkness: a nonzero day floors at 0.18 so it stays visible, full = 1.0.
function opacityFor(cov){ if(!cov||cov<=0) return 0; return 0.18 + 0.82*Math.min(1, cov); }

function boxHtml(cell, title){
  if(!cell) return '<div class="box" style="opacity:0"></div>';
  const op = opacityFor(cell.coverage);
  if(op<=0) return '<div class="box hue-'+cell.hue+'" style="opacity:0" title="'+title+' — absent"></div>';
  return '<div class="box hue-'+cell.hue+'" style="opacity:'+op.toFixed(3)+'" title="'+title+
    ' — '+pct(cell.coverage)+' covered ('+fmtInt(cell.n_symbols)+' syms), trust:'+cell.hue+'"></div>';
}

function render(view){
  LAST = view;
  document.getElementById('subhead').textContent =
    'store '+(view.store_root||'')+' · anchor '+(view.anchor_date||'—')+
    ' · universe '+fmtInt(view.universe_size)+' · generated '+(view.generated_at||'').replace('T',' ').slice(0,19)+'Z';
  const s = view.summary||{};
  const tc = s.trust_counts||{};
  document.getElementById('stats').innerHTML =
    '<div class="stat"><b>'+fmtInt(s.n_groups)+'</b> groups</div>'+
    '<div class="stat"><b>'+fmtInt(s.n_features)+'</b> features</div>'+
    '<div class="stat"><b>'+fmtInt(s.n_dates)+'</b> dates</div>'+
    '<div class="stat" style="color:var(--trusted)"><b>'+fmtInt(tc.trusted)+'</b> trusted ('+(s.trusted_pct||0)+'%)</div>'+
    '<div class="stat" style="color:var(--pending)"><b>'+fmtInt(tc.pending)+'</b> pending</div>'+
    '<div class="stat" style="color:var(--divergent)"><b>'+fmtInt(tc.divergent)+'</b> divergent</div>'+
    '<div class="stat" style="color:var(--ungraded)"><b>'+fmtInt(tc.ungraded)+'</b> ungraded</div>';

  const groups = view.groups||[];
  const dates = view.dates||[];
  const cells = view.cells||{};
  if(view.warming){ document.getElementById('content').innerHTML='<div class="empty">warming… the background worker is precomputing the grid. This page refreshes automatically.</div>'; return; }
  if(!dates.length){ document.getElementById('content').innerHTML='<div class="empty">no captured dates in the store yet.</div>'; return; }

  // header row: corner + each group (with its expanded feature columns inline) + Total
  let head = '<thead><tr><th class="corner">date \\\\ group</th>';
  for(const g of groups){
    const ex = expanded.has(g.group);
    head += '<th class="'+(ex?'expanded':'')+'" data-group="'+g.group+'" title="'+g.group+' · '+
      fmtInt(g.n_features)+' features · v'+g.version+' · trust:'+g.trust_hue+'"><span class="gh">'+
      g.group+' ('+g.n_features+')</span></th>';
    if(ex){
      for(const f of g.features){
        head += '<th class="featcol" title="'+f.feature+' · '+f.lifecycle_state+'"><span class="gh">'+f.feature+'</span></th>';
      }
    }
  }
  head += '<th class="totcol" data-group="__total__" title="whole-store coverage + trust">TOTAL</th></tr></thead>';

  let body = '<tbody>';
  for(const d of dates){
    const row = cells[d]||{};
    body += '<tr><td class="daterow">'+d+'</td>';
    for(const g of groups){
      const cell = row[g.group];
      body += '<td class="cell" data-group="'+g.group+'" data-date="'+d+'">'+boxHtml(cell, d+' · '+g.group)+'</td>';
      if(expanded.has(g.group)){
        for(const f of g.features){
          // per-feature darkness == its group's coverage; hue == the feature's own trust.
          const fc = cell ? {coverage:cell.coverage, n_symbols:cell.n_symbols, hue:f.trust_hue} : null;
          body += '<td class="cell" data-group="'+g.group+'" data-date="'+d+'">'+boxHtml(fc, d+' · '+f.feature)+'</td>';
        }
      }
    }
    const tot = row['__total__'];
    body += '<td class="cell totcell" data-group="__total__" data-date="'+d+'">'+boxHtml(tot, d+' · TOTAL')+'</td>';
    body += '</tr>';
  }
  body += '</tbody>';

  document.getElementById('content').innerHTML =
    '<div class="gridwrap"><table class="glimpse">'+head+body+'</table></div>';

  // wire header clicks (expand/collapse a group)
  document.querySelectorAll('table.glimpse thead th[data-group]').forEach(th=>{
    const g = th.getAttribute('data-group');
    if(g==='__total__') return;
    th.addEventListener('click', ()=>{ if(expanded.has(g)) expanded.delete(g); else expanded.add(g); render(LAST); });
  });
  // wire cell clicks (drill into tickers for that group)
  document.querySelectorAll('table.glimpse td.cell[data-group]').forEach(td=>{
    const g = td.getAttribute('data-group');
    if(g==='__total__') return;
    td.addEventListener('click', ()=> openDrill(g, td.getAttribute('data-date')));
  });
}

function provBox(p, date, sym){
  return '<td class="b prov-'+p+'" title="'+sym+' · '+date+' · '+p+'"></td>';
}

async function openDrill(group, date){
  const ov = document.getElementById('overlay');
  ov.classList.add('open');
  document.getElementById('drillTitle').textContent = group+'  —  ticker × date';
  document.getElementById('drillSub').textContent = 'around '+date+' · loading…';
  document.getElementById('drillBody').innerHTML = '<div class="empty">loading tickers…</div>';
  const days = document.getElementById('days').value;
  const r = await fetch('/api/store-glimpse/'+encodeURIComponent(group)+'/tickers?days='+days);
  if(!r.ok){ document.getElementById('drillBody').innerHTML='<div class="empty">no data ('+r.status+').</div>'; return; }
  const v = await r.json();
  document.getElementById('drillSub').textContent =
    group+' · '+fmtInt(v.n_tickers)+' tickers (showing top '+fmtInt(Math.min(v.limit, v.n_tickers))+
    ' by coverage) · '+(v.dates||[]).length+' dates';
  const dates = v.dates||[]; const tickers = v.tickers||[];
  if(!tickers.length){ document.getElementById('drillBody').innerHTML='<div class="empty">no tickers in window.</div>'; return; }
  let head = '<thead><tr><th class="symh">ticker</th>';
  for(const d of dates) head += '<th>'+d.slice(5)+'</th>';
  head += '</tr></thead>';
  let body = '<tbody>';
  for(const t of tickers){
    body += '<tr><td class="sym">'+t.symbol+' <span class="muted">('+t.n_present+')</span></td>';
    for(const box of t.boxes) body += provBox(box.provenance, box.date, t.symbol);
    body += '</tr>';
  }
  body += '</tbody>';
  document.getElementById('drillBody').innerHTML =
    '<div class="muted" style="margin:6px 0;font-size:11px;">'+
    '<span class="sw prov-both" style="display:inline-block;width:11px;height:11px;border-radius:2px"></span> both '+
    '<span class="sw prov-stream" style="display:inline-block;width:11px;height:11px;border-radius:2px;margin-left:8px"></span> stream '+
    '<span class="sw prov-backfill" style="display:inline-block;width:11px;height:11px;border-radius:2px;margin-left:8px"></span> backfill '+
    '<span class="sw prov-absent" style="display:inline-block;width:11px;height:11px;border-radius:2px;margin-left:8px;border:1px solid var(--border)"></span> absent</div>'+
    '<div class="drillgrid"><table class="drill">'+head+body+'</table></div>';
}

async function load(force){
  const days = document.getElementById('days').value;
  const r = await fetch('/api/store-glimpse?days='+days+(force?'&refresh=true':''));
  if(!r.ok){ document.getElementById('content').innerHTML='<div class="empty">failed to load ('+r.status+').</div>'; return; }
  render(await r.json());
}

document.getElementById('refresh').addEventListener('click', ()=>load(true));
document.getElementById('days').addEventListener('change', ()=>load(false));
document.getElementById('closeDrill').addEventListener('click', ()=>document.getElementById('overlay').classList.remove('open'));
document.getElementById('overlay').addEventListener('click', (e)=>{ if(e.target.id==='overlay') e.target.classList.remove('open'); });

let timer = null;
function tick(){ if(document.getElementById('auto').checked) load(false); }
document.getElementById('auto').addEventListener('change', ()=>{ /* state read each tick */ });
timer = setInterval(tick, 30000);

load(false);
</script>
</body></html>"""
