"""The visual RAW-TAPE coverage surface — a standalone page on the dashboard.

Vanilla HTML/JS (no build step), same dark theme as the other panels. Fetches ``/api/raw-coverage``
client-side and renders, per raw layer (bars / trades / quotes): a DEPTH header (earliest→latest span, #
dates, # symbols), a BREADTH bar chart (distinct symbols per day over time — the thin-trades / shallow-quotes
gaps read off at a glance), and a date-coverage timeline strip (one tick per acquired date, gaps visible).
"""

from __future__ import annotations

RAW_COVERAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Raw-tape coverage</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg:#0f1115; --panel:#171a21; --border:#262b35; --text:#d7dce2; --muted:#8b949e;
    --bars:#58a6ff; --trades:#e08a2b; --quotes:#3fb950; --grid:#1b2230;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  header { background:var(--panel); padding:14px 22px; border-bottom:1px solid var(--border); }
  header h1 { margin:0; font-size:18px; }
  header a { color:var(--bars); text-decoration:none; font-size:13px; font-weight:400; margin-left:10px; }
  .muted { color:var(--muted); }
  .wrap { padding:18px 22px; max-width:1400px; }
  .controls { display:flex; gap:10px; align-items:center; margin-bottom:16px; flex-wrap:wrap; }
  .controls select, .controls button { background:var(--panel); color:var(--text);
    border:1px solid var(--border); border-radius:6px; padding:5px 10px; font-size:13px; cursor:pointer; }
  .layer { background:var(--panel); border:1px solid var(--border); border-radius:8px;
    padding:14px 16px; margin-bottom:16px; }
  .layer h2 { margin:0 0 4px; font-size:15px; display:flex; align-items:center; gap:8px; }
  .layer h2 .dot { width:11px; height:11px; border-radius:3px; display:inline-block; }
  .layer .depth { font-size:12px; color:var(--muted); margin-bottom:10px; }
  .layer .depth b { color:var(--text); font-variant-numeric:tabular-nums; }
  .stats { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }
  .stat { background:#0f1115; border:1px solid var(--border); border-radius:6px; padding:5px 10px; font-size:12px; }
  .stat b { font-variant-numeric:tabular-nums; }
  /* breadth: per-day symbols-per-day bars (height ∝ distinct symbols with a real tape that day) */
  .breadth { display:flex; align-items:flex-end; gap:1px; height:110px; padding:6px 4px; overflow-x:auto;
    background:#0f1115; border:1px solid var(--border); border-radius:6px; }
  .breadth .bar { min-width:3px; flex:1 0 3px; background:var(--bars); border-radius:1px 1px 0 0;
    opacity:0.85; }
  .breadth .bar.wknd { opacity:0.3; }
  .breadth.trades .bar { background:var(--trades); }
  .breadth.quotes .bar { background:var(--quotes); }
  .axis { display:flex; justify-content:space-between; font-size:10px; color:var(--muted); margin-top:3px; }
  /* date-coverage gap strip: one cell per CALENDAR weekday from earliest→latest; present=on, gap=off */
  .gaps { display:flex; gap:1px; margin-top:10px; flex-wrap:wrap; }
  .gaps .g { width:7px; height:14px; border-radius:1px; background:var(--grid); }
  .gaps .g.on { background:var(--bars); }
  .gaps.trades .g.on { background:var(--trades); }
  .gaps.quotes .g.on { background:var(--quotes); }
  .gaps .g.wknd { opacity:0.35; }
  .gaplabel { font-size:11px; color:var(--muted); margin-top:8px; }
  .empty { color:var(--muted); font-style:italic; padding:8px 0; }
</style></head>
<body>
<header><h1>Raw-tape coverage
  <a href="/">&larr; dashboard</a><a href="/feature-grid">feature grid &rarr;</a></h1>
  <div class="muted" id="subhead">loading…</div>
</header>
<div class="wrap">
  <div class="controls">
    <select id="days">
      <option value="90">timeline: last 90 days</option>
      <option value="30">last 30 days</option>
      <option value="180">last 180 days</option>
      <option value="365">last year</option>
      <option value="0">full history</option>
    </select>
    <button id="refresh">↻ refresh</button>
  </div>
  <div id="layers"></div>
</div>
<script>
const TIERCOLOR = {bars:"var(--bars)", trades:"var(--trades)", quotes:"var(--quotes)"};
let DAYS=90;

function isWeekend(iso){ const wd=new Date(iso+"T00:00:00").getDay(); return wd===0||wd===6; }
function fmtInt(n){ return (n==null?"—":n.toLocaleString()); }

async function load(force){
  const r = await fetch("/api/raw-coverage?days="+DAYS+(force?"&refresh=1":""));
  const d = await r.json();
  const span = d.span_earliest ? (d.span_earliest+" → "+d.span_latest) : "no raw tape";
  document.getElementById("subhead").innerHTML =
    "raw Alpaca tape on disk, read from the manifests — what modellers can invent + backfill on without "+
    "re-downloading. Span <b>"+span+"</b>. Anchor "+(d.anchor_date||"—")+".";
  document.getElementById("layers").innerHTML = d.layers.map(renderLayer).join("");
}

function renderLayer(L){
  const color = TIERCOLOR[L.tier] || "var(--bars)";
  if(!L.earliest){
    return "<div class='layer'><h2><span class='dot' style='background:"+color+"'></span>"+
      L.tier+" <span class='muted'>· "+L.label+"</span></h2>"+
      "<div class='empty'>no raw tape acquired yet</div></div>";
  }
  const depth =
    "<b>"+L.earliest+"</b> → <b>"+L.latest+"</b> · span <b>"+fmtInt(L.span_days)+"</b> days · "+
    "<b>"+fmtInt(L.n_dates)+"</b> dates present";
  const stats =
    stat(fmtInt(L.n_symbols), "distinct symbols")+
    stat(fmtInt(L.mean_symbols_per_day), "mean symbols/day")+
    stat(fmtInt(L.median_symbols_per_day), "median symbols/day")+
    stat(fmtInt(L.newest_symbols_per_day), "newest day symbols")+
    stat(fmtInt(L.n_cells), "symbol-day cells");
  return "<div class='layer'>"+
    "<h2><span class='dot' style='background:"+color+"'></span>"+L.tier+
      " <span class='muted'>· "+L.label+"</span></h2>"+
    "<div class='depth'>"+depth+"</div>"+
    "<div class='stats'>"+stats+"</div>"+
    breadthChart(L)+
    gapStrip(L)+
    "</div>";
}

function stat(big, small){
  return "<div class='stat'><b>"+big+"</b> <span class='muted'>"+small+"</span></div>";
}

function breadthChart(L){
  const dates = L.dates;
  if(!dates.length) return "<div class='empty'>no dates in window</div>";
  const peak = Math.max(1, ...dates.map(c=>c.n_symbols));
  const bars = dates.map(c=>{
    const h = Math.round(100*c.n_symbols/peak);
    const wknd = isWeekend(c.date) ? " wknd" : "";
    const tip = c.date+" · "+fmtInt(c.n_symbols)+" symbols · "+fmtInt(c.rows)+" rows";
    return "<div class='bar"+wknd+"' style='height:"+h+"%' title=\\""+tip+"\\"></div>";
  }).join("");
  const from = dates[0].date, to = dates[dates.length-1].date;
  return "<div class='muted' style='font-size:11px;margin-bottom:4px'>BREADTH — distinct symbols per day "+
      "(shown "+L.n_dates_shown+" dates "+from+"→"+to+", peak "+fmtInt(peak)+")</div>"+
    "<div class='breadth "+L.tier+"'>"+bars+"</div>"+
    "<div class='axis'><span>"+from+"</span><span>"+to+"</span></div>";
}

function gapStrip(L){
  // one cell per CALENDAR weekday from earliest→latest of the WINDOW shown; present=on, gap(no tape)=off.
  const dates = L.dates;
  if(!dates.length) return "";
  const present = new Set(dates.map(c=>c.date));
  const start = new Date(dates[0].date+"T00:00:00");
  const end = new Date(dates[dates.length-1].date+"T00:00:00");
  let cells="", nWeekdays=0, nGaps=0;
  for(let t=new Date(start); t<=end; t.setDate(t.getDate()+1)){
    const iso = t.toISOString().slice(0,10);
    const wd = t.getDay();
    if(wd===0 || wd===6) continue;  // weekends: market closed, not a gap
    nWeekdays++;
    const on = present.has(iso);
    if(!on) nGaps++;
    cells += "<div class='g "+(on?"on":"")+"' title='"+iso+(on?" · present":" · GAP (no tape)")+"'></div>";
  }
  return "<div class='gaplabel'>date coverage — one tick per trading weekday in the shown window; "+
      "<b style='color:var(--text)'>"+nGaps+"</b> of "+nWeekdays+" weekdays missing a tape</div>"+
    "<div class='gaps "+L.tier+"'>"+cells+"</div>";
}

document.getElementById("days").onchange=e=>{ DAYS=parseInt(e.target.value,10); load(false); };
document.getElementById("refresh").onclick=()=>load(true);
load(false);
</script>
</body></html>"""
