"""The static HTML/JS for the feature-coverage grid page (served at ``/feature-grid``).

Kept as a single string constant so the dashboard image stays a two-file copy (app.py + helpers) with no
template engine or static-asset build. Vanilla JS, zero runtime deps: it fetches ``/api/feature-grid`` and
``/api/feature-grid/{group}`` (the SAME JSON agents consume) and renders the grid client-side, so the page
and the agent API can never drift. Loads in well under 1-2s (one JSON round-trip, then DOM build).

Encodings (also documented in docs/FEATURE_DASHBOARD.md):
  * DATA COVERAGE -> blue fill OPACITY (transparent=0%, solid=100%) + the % number, always white-on-dark
    with a text outline so it is legible at any opacity (improvement #5).
  * TRUST STATE   -> a distinct channel: left border colour + corner badge. UNGRADED=grey, PENDING/
    VALIDATING=amber (with "X% to trusted"), VALIDATED=green (with "trusted Y%"), DIVERGENT=red.
  * STREAM vs BACKFILL -> a split corner triangle per cell (upper=stream, lower=backfill) so both sides
    show at a glance; the combined % is the number. A toggle switches the main number between combined /
    stream / backfill.
Palette is colour-blind-safe (blue fill + amber/green/grey/red borders are distinguishable by the Okabe-Ito
ordering and reinforced by text labels, never colour alone).
"""

from __future__ import annotations

FEATURE_GRID_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Feature Coverage &amp; Trust</title>
<style>
  :root {
    --bg:#0f1115; --panel:#171a21; --border:#262b35; --text:#d7dce2; --muted:#8b949e;
    --blue:#1f6feb; --amber:#d8a200; --green:#2da44e; --grey:#6e7681; --red:#cf3b3b;
  }
  body { font-family:system-ui,sans-serif; margin:0; background:var(--bg); color:var(--text); }
  header { background:var(--panel); padding:14px 22px; border-bottom:1px solid var(--border); }
  h1 { font-size:18px; margin:0; }
  header a { color:#58a6ff; text-decoration:none; font-size:13px; }
  .muted { color:var(--muted); font-size:12px; }
  .wrap { padding:18px 22px; }
  .summary { display:flex; flex-wrap:wrap; gap:18px; margin-bottom:14px; font-size:13px; }
  .summary .stat { background:var(--panel); border:1px solid var(--border); border-radius:8px;
                   padding:8px 14px; }
  .summary .stat b { font-size:18px; display:block; font-variant-numeric:tabular-nums; }
  .controls { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:12px; font-size:13px; }
  .controls input, .controls select, .controls button {
    background:#0f1115; color:var(--text); border:1px solid var(--border); border-radius:6px;
    padding:6px 9px; font-size:13px; }
  .controls button { cursor:pointer; }
  .controls button:hover { background:#1d212a; }
  .legend { display:flex; flex-wrap:wrap; gap:16px; font-size:12px; color:var(--muted);
            margin-bottom:14px; align-items:center; }
  .legend .swatch { display:inline-block; width:14px; height:14px; border-radius:3px; vertical-align:middle;
                    margin-right:4px; }
  .legend .ramp { display:inline-block; width:90px; height:12px; vertical-align:middle; margin-right:4px;
                  background:linear-gradient(90deg, rgba(31,111,235,0.08), rgba(31,111,235,1));
                  border:1px solid var(--border); border-radius:3px; }
  .gridscroll { overflow:auto; border:1px solid var(--border); border-radius:8px; }
  table.grid { border-collapse:separate; border-spacing:0; font-size:12px; }
  table.grid th, table.grid td { padding:0; }
  table.grid thead th { position:sticky; top:0; background:var(--panel); z-index:2; padding:7px 6px;
    border-bottom:1px solid var(--border); border-left:1px solid var(--border); text-align:center;
    white-space:nowrap; cursor:pointer; }
  table.grid thead th:hover { background:#1d212a; }
  table.grid tbody th { position:sticky; left:0; background:var(--panel); z-index:1; padding:6px 10px;
    border-bottom:1px solid var(--border); text-align:right; white-space:nowrap; font-weight:600; }
  .cell { position:relative; width:62px; height:42px; border-left:1px solid var(--border);
    border-bottom:1px solid var(--border); cursor:pointer; overflow:hidden; }
  .cell .fill { position:absolute; inset:0; background:var(--blue); }
  .cell .pct { position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
    font-variant-numeric:tabular-nums; font-weight:600; color:#fff;
    text-shadow:0 0 2px #000,0 0 2px #000,0 1px 1px #000; }
  .cell.ts-UNGRADED { border-left:3px solid var(--grey); }
  .cell.ts-PENDING  { border-left:3px solid var(--amber); }
  .cell.ts-VALIDATED{ border-left:3px solid var(--green); }
  .cell.ts-DIVERGENT{ border-left:3px solid var(--red); }
  .cell .badge { position:absolute; top:0; right:0; font-size:8px; line-height:1; padding:1px 2px;
    border-bottom-left-radius:3px; color:#0f1115; font-weight:700; }
  .cell.ts-PENDING  .badge { background:var(--amber); }
  .cell.ts-VALIDATED .badge { background:var(--green); }
  .cell.ts-DIVERGENT .badge { background:var(--red); color:#fff; }
  .cell.ts-UNGRADED .badge { display:none; }
  /* stream/backfill split-corner: a small triangle bottom-left, upper=stream lower=backfill presence */
  .cell .split { position:absolute; bottom:1px; left:4px; display:flex; flex-direction:column; gap:1px; }
  .cell .split i { width:10px; height:3px; border-radius:1px; display:block; opacity:0.85; }
  .cell .split i.on { background:#9fd0ff; } .cell .split i.off { background:#33404f; }
  .detail { background:var(--panel); border:1px solid var(--border); border-radius:8px; margin-top:14px;
            padding:14px 16px; }
  .detail h2 { font-size:15px; margin:0 0 4px; }
  table.feat { border-collapse:collapse; width:100%; font-size:12px; margin-top:8px; }
  table.feat th, table.feat td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--border);
    white-space:nowrap; }
  table.feat td.num { text-align:right; font-variant-numeric:tabular-nums; }
  .pill { padding:1px 6px; border-radius:10px; font-size:11px; font-weight:600; }
  .pill.UNGRADED { background:#2a2f3a; color:var(--muted); }
  .pill.PENDING  { background:#3a3210; color:var(--amber); }
  .pill.VALIDATED{ background:#10301c; color:#56d364; }
  .pill.DIVERGENT{ background:#3a1414; color:#ff7b72; }
  .pill.both         { background:#10301c; color:#56d364; }
  .pill.backfill_only{ background:#3a2a10; color:#e0a458; }
  .pill.stream_only  { background:#10243a; color:#58a6ff; }
  .bar { display:inline-block; width:70px; height:8px; background:#0f1115; border:1px solid var(--border);
    border-radius:4px; vertical-align:middle; overflow:hidden; }
  .bar > span { display:block; height:100%; background:var(--amber); }
  .closebtn { float:right; cursor:pointer; color:var(--muted); font-size:13px; }
  .symbtn { cursor:pointer; background:#0f1115; color:#58a6ff; border:1px solid var(--border);
    border-radius:6px; padding:4px 9px; font-size:12px; margin-top:8px; }
  .symbtn:hover { background:#1d212a; }
  .symcov { margin-top:10px; border-top:1px solid var(--border); padding-top:10px; }
  .symcov .stat { display:inline-block; background:#0f1115; border:1px solid var(--border);
    border-radius:6px; padding:4px 9px; margin-right:8px; font-size:12px; }
  .symcov .stat b { font-variant-numeric:tabular-nums; }
  .symlist { font-family:ui-monospace,Menlo,monospace; font-size:11px; color:var(--muted);
    max-height:120px; overflow:auto; background:#0f1115; border:1px solid var(--border);
    border-radius:6px; padding:6px 8px; margin-top:6px; word-break:break-all; line-height:1.6; }
  /* (group x recent-day x source) presence grid: one tiny square per day, split stream(top)/backfill(bottom) */
  table.tl { border-collapse:separate; border-spacing:0; font-size:11px; margin-top:8px; }
  table.tl th, table.tl td { padding:0; }
  table.tl thead th { position:sticky; top:0; background:var(--panel); z-index:2; padding:5px 4px;
    border-bottom:1px solid var(--border); text-align:center; white-space:nowrap; font-weight:600; }
  table.tl thead th.wknd { color:var(--muted); }
  table.tl tbody th { position:sticky; left:0; background:var(--panel); z-index:1; padding:4px 9px 4px 4px;
    border-bottom:1px solid var(--border); text-align:left; white-space:nowrap; font-weight:600; }
  table.tl tbody th .depth { color:var(--muted); font-weight:400; font-size:10px; }
  .tlc { width:22px; height:20px; border-bottom:1px solid var(--border); border-left:1px solid var(--border); }
  .tlc .pp { width:100%; height:100%; display:flex; flex-direction:column; }
  .tlc .pp i { flex:1; display:block; }
  /* upper half = stream, lower half = backfill; absent=dark, present=blue/orange with per-cell
     opacity ∝ that day's symbol count vs the group's own in-window peak (coverage-volume heat) */
  .tlc .pp i.s.on { background:#58a6ff; } .tlc .pp i.s.off { background:#1b2230; }
  .tlc .pp i.b.on { background:#e08a2b; } .tlc .pp i.b.off { background:#241c12; }
  /* order-flow live coverage trend: per-day union-of-symbols bars (height ∝ distinct live symbols) */
  .oft { display:flex; align-items:flex-end; gap:3px; height:120px; margin-top:8px; padding:6px 4px;
    background:#0f1115; border:1px solid var(--border); border-radius:6px; overflow-x:auto; }
  .oft .bar { display:flex; flex-direction:column; align-items:center; min-width:26px; }
  .oft .bar .col { width:18px; background:#1b2230; border-radius:2px 2px 0 0; position:relative; }
  .oft .bar .col .u { position:absolute; bottom:0; left:0; width:100%; background:#58a6ff; border-radius:2px 2px 0 0; }
  .oft .bar .col .x { position:absolute; bottom:0; left:0; width:100%; background:#2f81f7; }
  .oft .bar .lbl { font-size:9px; color:var(--muted); margin-top:3px; white-space:nowrap; }
  .oft .bar .n { font-size:9px; color:var(--text); font-variant-numeric:tabular-nums; }
  .oft .bar.wknd .col { opacity:0.4; }
  .verdict { font-weight:600; }
  .verdict.up { color:var(--green); } .verdict.flat { color:var(--amber); } .verdict.down { color:var(--red); }
  /* trust frontier: one stacked bar (trusted | eligible | blocked) + a per-group breakdown table */
  .tf-bar { display:flex; height:26px; border-radius:6px; overflow:hidden; margin:8px 0 4px;
    border:1px solid var(--border); font-size:11px; font-weight:600; }
  .tf-bar > div { display:flex; align-items:center; justify-content:center; white-space:nowrap;
    overflow:hidden; color:#0f1115; }
  .tf-bar .tf-t { background:#56d364; } .tf-bar .tf-e { background:#e3b341; } .tf-bar .tf-b { background:#ff7b72; }
  .tf-key { font-size:11px; color:var(--muted); }
  .tf-key b.tf-t { color:#56d364; } .tf-key b.tf-e { color:#e3b341; } .tf-key b.tf-b { color:#ff7b72; }
  table.tf { border-collapse:collapse; width:100%; font-size:12px; margin-top:10px; }
  table.tf th, table.tf td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--border);
    white-space:nowrap; }
  table.tf td.num { text-align:right; font-variant-numeric:tabular-nums; }
  table.tf .gbar { display:inline-block; width:90px; height:9px; border-radius:3px; overflow:hidden;
    vertical-align:middle; border:1px solid var(--border); }
  table.tf .gbar i { display:inline-block; height:100%; }
  table.tf .gbar i.tf-t { background:#56d364; } table.tf .gbar i.tf-e { background:#e3b341; }
  table.tf .gbar i.tf-b { background:#ff7b72; }
  table.tf tr.has-blocked td:first-child { color:var(--red); }
  .tf-blocked { font-family:ui-monospace,Menlo,monospace; font-size:10px; color:var(--muted); }
</style></head>
<body>
<header><h1>Feature Coverage &amp; Trust &nbsp;
  <a href="/">&larr; dashboard</a><a href="/raw-coverage">raw-tape coverage &rarr;</a></h1>
  <div class="muted" id="subhead">loading…</div>
</header>
<div class="wrap">
  <div class="summary" id="summary"></div>
  <div class="legend">
    <span><span class="ramp"></span>data coverage % (blue opacity)</span>
    <span><span class="swatch" style="background:var(--grey)"></span>ungraded</span>
    <span><span class="swatch" style="background:var(--amber)"></span>validating (X% to trusted)</span>
    <span><span class="swatch" style="background:var(--green)"></span>trusted</span>
    <span><span class="swatch" style="background:var(--red)"></span>divergent</span>
    <span>split-corner = stream / backfill present</span>
  </div>
  <div class="controls">
    <input id="search" placeholder="search feature…" size="20">
    <select id="trustfilter">
      <option value="">all trust states</option>
      <option value="VALIDATED">trusted only</option>
      <option value="PENDING">validating only</option>
      <option value="UNGRADED">ungraded only</option>
      <option value="DIVERGENT">divergent only</option>
    </select>
    <select id="sortby">
      <option value="group">sort: group name</option>
      <option value="coverage">sort: coverage (all-history)</option>
      <option value="trust">sort: trust %</option>
    </select>
    <select id="metric">
      <option value="coverage_pct">show: combined %</option>
      <option value="stream_pct">show: stream %</option>
      <option value="backfill_pct">show: backfill %</option>
    </select>
    <button id="tfbtn">▸ trust frontier</button>
    <button id="thinbtn">▸ thinnest live tickers</button>
    <button id="tlbtn">▸ depth &amp; recent-day timeline</button>
    <button id="oftbtn">▸ order-flow live coverage trend</button>
    <button id="refresh">↻ refresh</button>
  </div>
  <div id="tfhost"></div>
  <div id="thinhost"></div>
  <div id="tlhost"></div>
  <div id="ofthost"></div>
  <div class="gridscroll"><table class="grid" id="grid"></table></div>
  <div id="detailhost"></div>
</div>
<script>
let GRID=null, METRIC="coverage_pct", SORT="group", TRUSTFILTER="", SEARCH="", OPENGROUP=null;

function pctOpacity(p){ return 0.08 + 0.92*Math.max(0,Math.min(100,p))/100; }

async function loadGrid(force){
  const r = await fetch("/api/feature-grid" + (force?"?refresh=1":""));
  GRID = await r.json();
  document.getElementById("subhead").textContent =
    "anchor " + (GRID.anchor_date||"—") + " · store " + GRID.store_root +
    " · generated " + GRID.generated_at.replace("T"," ").slice(0,19) + "Z";
  renderSummary(); renderGrid();
  if(OPENGROUP) openDetail(OPENGROUP);
}

function renderSummary(){
  const s = GRID.summary;
  document.getElementById("summary").innerHTML =
    stat(s.n_trusted+"/"+s.n_features, "features trusted")+
    stat(s.trusted_pct+"%", "of features trusted")+
    stat(s.mean_coverage_pct+"%", "mean coverage (all-history)")+
    stat(s.fully_validated_groups, "groups fully validated")+
    stat(s.n_groups, "feature groups");
}
function stat(big,small){ return "<div class='stat'><b>"+big+"</b><span class='muted'>"+small+"</span></div>"; }

function cellsByGroup(){
  const map={};
  for(const c of GRID.cells){ (map[c.group]=map[c.group]||{})[c.period]=c; }
  return map;
}

function sortedGroups(byGroup){
  let groups = GRID.groups.slice();
  if(TRUSTFILTER){
    groups = groups.filter(g=>{
      const all = byGroup[g.group] && byGroup[g.group]["all"];
      if(!all) return false;
      if(TRUSTFILTER==="VALIDATED") return all.n_trusted>0;
      if(TRUSTFILTER==="PENDING") return all.n_validating>0;
      if(TRUSTFILTER==="UNGRADED") return all.trust_state==="UNGRADED";
      if(TRUSTFILTER==="DIVERGENT") return all.trust_state==="DIVERGENT";
      return true;
    });
  }
  const allcov = g => (byGroup[g.group] && byGroup[g.group]["all"] ? byGroup[g.group]["all"].coverage_pct : 0);
  const trustpct = g => (byGroup[g.group] && byGroup[g.group]["all"] ? byGroup[g.group]["all"].trust_pct : 0);
  if(SORT==="coverage") groups.sort((a,b)=>allcov(b)-allcov(a));
  else if(SORT==="trust") groups.sort((a,b)=>trustpct(b)-trustpct(a));
  else groups.sort((a,b)=>a.group.localeCompare(b.group));
  return groups;
}

function renderGrid(){
  const byGroup = cellsByGroup();
  const groups = sortedGroups(byGroup);
  let head = "<thead><tr><th style='text-align:left'>period \\ group</th>";
  for(const g of groups){
    head += "<th title='"+g.n_features+" features, v"+g.version+
      "' onclick=\\"openDetail('"+g.group+"')\\">"+g.group+"<br><span class='muted'>"+g.n_features+"f</span></th>";
  }
  head += "</tr></thead>";
  let body = "<tbody>";
  for(const p of GRID.periods){
    body += "<tr><th>"+p.label+"</th>";
    for(const g of groups){
      const c = byGroup[g.group] ? byGroup[g.group][p.key] : null;
      body += cellHtml(g.group, c);
    }
    body += "</tr>";
  }
  body += "</tbody>";
  document.getElementById("grid").innerHTML = head+body;
}

function badgeText(c){
  if(c.trust_state==="VALIDATED") return "✓"+c.trust_pct+"%";
  if(c.trust_state==="PENDING") return "↑";
  if(c.trust_state==="DIVERGENT") return "!";
  return "";
}

function cellHtml(group, c){
  if(!c){ return "<td class='cell'></td>"; }
  const val = c[METRIC];
  const op = pctOpacity(val);
  const streamOn = c.stream_pct>0 ? "on" : "off";
  const backfillOn = c.backfill_pct>0 ? "on" : "off";
  const tip = group+" · cov "+c.coverage_pct+"% (stream "+c.stream_pct+"%, backfill "+c.backfill_pct+
    "%) · "+c.trust_state+" trusted "+c.n_trusted+"/"+c.n_features+
    " · "+c.n_symbols+" symbols, "+c.n_dates+" dates";
  return "<td class='cell ts-"+c.trust_state+"' title=\\""+tip.replace(/"/g,'')+
    "\\" onclick=\\"openDetail('"+group+"')\\">"+
    "<div class='fill' style='opacity:"+op.toFixed(3)+"'></div>"+
    "<div class='split'><i class='"+streamOn+"'></i><i class='"+backfillOn+"'></i></div>"+
    "<div class='badge'>"+badgeText(c)+"</div>"+
    "<div class='pct'>"+val+"</div></td>";
}

async function openDetail(group){
  OPENGROUP = group;
  const r = await fetch("/api/feature-grid/"+encodeURIComponent(group));
  const d = await r.json();
  let rows="";
  let feats = d.features.slice();
  if(SEARCH) feats = feats.filter(f=>f.feature.toLowerCase().includes(SEARCH.toLowerCase()));
  if(TRUSTFILTER) feats = feats.filter(f=>f.trust_state===TRUSTFILTER);
  for(const f of feats){
    const prog = f.trust_state==="VALIDATED" ? "trusted" :
      "<span class='bar'><span style='width:"+f.progress_to_trusted_pct+"%'></span></span> "+
      f.clean_days+"/"+f.days_needed+"d ("+f.progress_to_trusted_pct+"%)";
    const rate = f.clean_value_rate==null ? "—" : (f.clean_value_rate*100).toFixed(3)+"%";
    rows += "<tr><td title='"+(f.description||"").replace(/'/g,"")+"'>"+f.feature+"</td>"+
      "<td><span class='pill "+f.trust_state+"'>"+f.trust_state+"</span></td>"+
      "<td>"+prog+"</td><td class='num'>"+rate+"</td>"+
      "<td>"+(f.last_validated_day||"—")+"</td>"+
      "<td>"+(f.layer||"")+"</td></tr>";
  }
  const gaps = [];
  if(d.backfill_only_dates.length) gaps.push("backfill-only: "+d.backfill_only_dates.join(", "));
  if(d.stream_only_dates.length) gaps.push("stream-only (no backfill → not parity-checkable): "+d.stream_only_dates.join(", "));
  document.getElementById("detailhost").innerHTML =
    "<div class='detail'><span class='closebtn' onclick='closeDetail()'>✕ close</span>"+
    "<h2>"+d.group+" <span class='muted'>v"+d.version+" · "+d.n_features+" features</span></h2>"+
    "<div class='muted'>stream "+(d.stream_first||"—")+"→"+(d.stream_last||"—")+
      " ("+d.stream_dates.length+"d) · backfill "+(d.backfill_first||"—")+"→"+
      (d.backfill_last||"—")+" ("+d.backfill_dates.length+"d)</div>"+
    (gaps.length?"<div class='muted' style='margin-top:4px'>"+gaps.join(" · ")+"</div>":"")+
    "<button class='symbtn' onclick=\\"loadSymbols('"+d.group+"')\\">▸ ticker representation "+
    "(which symbols are live vs backfill-only)</button>"+
    "<button class='symbtn' onclick=\\"loadSymbolDepth('"+d.group+"')\\">▸ coverage depth "+
    "(how far back per ticker, stream vs backfill)</button>"+
    "<div id='symcovhost'></div>"+
    "<div id='symdepthhost'></div>"+
    "<table class='feat'><thead><tr><th>feature (hover=description)</th><th>trust</th>"+
    "<th>trajectory to trusted</th><th class='num'>clean match-rate</th><th>last validated</th><th>layer</th>"+
    "</tr></thead><tbody>"+rows+"</tbody></table></div>";
  document.getElementById("detailhost").scrollIntoView({behavior:"smooth", block:"nearest"});
}

async function loadSymbols(group){
  const host = document.getElementById("symcovhost");
  if(!host) return;
  host.innerHTML = "<div class='symcov muted'>loading symbol coverage…</div>";
  const r = await fetch("/api/feature-grid/"+encodeURIComponent(group)+"/symbols");
  const s = await r.json();
  const list = (arr) => arr.length ? "<div class='symlist'>"+arr.join(" ")+"</div>"
    : "<div class='muted' style='margin-top:4px'>none</div>";
  host.innerHTML =
    "<div class='symcov'>"+
    "<div class='stat'><b>"+s.stream_coverage_pct+"%</b> live stream coverage</div>"+
    "<div class='stat'><b>"+s.n_stream+"</b> stream <span class='muted'>("+(s.stream_date||"—")+")</span></div>"+
    "<div class='stat'><b>"+s.n_backfill+"</b> backfill <span class='muted'>("+(s.backfill_date||"—")+")</span></div>"+
    "<div class='stat'><b>"+s.n_both+"</b> both</div>"+
    "<div class='stat'><b>"+s.n_backfill_only+"</b> under-represented LIVE</div>"+
    "<div class='muted' style='margin-top:8px'>backfill-only — present in the (full-universe) backfill agg "+
      "but NOT captured on the live stream today ("+s.n_backfill_only+"):</div>"+list(s.backfill_only)+
    (s.n_stream_only? "<div class='muted' style='margin-top:8px'>stream-only — live but absent from "+
      "today's backfill ("+s.n_stream_only+"):</div>"+list(s.stream_only) : "")+
    "</div>";
}

async function loadSymbolDepth(group){
  const host = document.getElementById("symdepthhost");
  if(!host) return;
  host.innerHTML = "<div class='symcov muted'>loading coverage depth…</div>";
  const r = await fetch("/api/feature-grid/"+encodeURIComponent(group)+"/symbol-depth");
  const d = await r.json();
  const span = (e,l) => e ? (e+"→"+l) : "—";
  let rows="";
  for(const row of d.symbols){
    const cls = row.provenance;  // both | backfill_only | stream_only
    rows += "<tr><td>"+row.symbol+"</td>"+
      "<td><span class='pill "+cls+"'>"+cls.replace("_"," ")+"</span></td>"+
      "<td>"+span(row.backfill_earliest,row.backfill_latest)+"</td>"+
      "<td class='num'>"+row.backfill_span_days+"</td>"+
      "<td class='num'>"+row.backfill_n_dates+"</td>"+
      "<td>"+span(row.stream_earliest,row.stream_latest)+"</td>"+
      "<td class='num'>"+row.stream_n_dates+"</td></tr>";
  }
  host.innerHTML =
    "<div class='symcov'>"+
    "<div class='stat'><b>"+d.n_symbols+"</b> symbols</div>"+
    "<div class='stat'><b>"+d.n_both+"</b> both</div>"+
    "<div class='stat'><b>"+d.n_backfill_only+"</b> backfill-only</div>"+
    "<div class='stat'><b>"+d.n_stream_only+"</b> stream-only</div>"+
    "<div class='stat'><b>"+span(d.backfill_earliest,d.backfill_latest)+"</b> backfill span ("+d.backfill_n_dates+"d)</div>"+
    "<div class='stat'><b>"+span(d.stream_earliest,d.stream_latest)+"</b> stream span ("+d.stream_n_dates+"d)</div>"+
    "<div class='muted' style='margin-top:8px'>per-ticker depth — how far back each ticker's data goes, "+
      "per source (shown "+d.n_shown+" of "+d.n_symbols+", shallowest backfill first):</div>"+
    "<table class='feat' style='margin-top:6px'><thead><tr><th>ticker</th><th>provenance</th>"+
    "<th>backfill span</th><th class='num'>days</th><th class='num'>#dates</th>"+
    "<th>stream span</th><th class='num'>#dates</th></tr></thead><tbody>"+rows+"</tbody></table>"+
    "</div>";
}

function closeDetail(){ OPENGROUP=null; document.getElementById("detailhost").innerHTML=""; }

let THINOPEN=false;
async function toggleThin(force){
  const host = document.getElementById("thinhost");
  if(THINOPEN && !force){ THINOPEN=false; host.innerHTML=""; return; }
  THINOPEN=true;
  host.innerHTML = "<div class='symcov muted'>loading thinnest-live tickers…</div>";
  const r = await fetch("/api/feature-grid/thin-live-symbols" + (force?"?refresh=1":""));
  const t = await r.json();
  let rows="";
  for(const s of t.symbols){
    rows += "<tr><td>"+s.symbol+"</td><td class='num'>"+s.n_under_groups+"/"+t.n_live_groups+
      "</td><td class='num'>"+s.n_live_groups+"</td>"+
      "<td title='"+s.under_groups.join(", ")+"'>"+s.under_groups.join(" ")+"</td></tr>";
  }
  host.innerHTML =
    "<div class='symcov'>"+
    "<span class='closebtn' onclick='toggleThin(false)'>✕ close</span>"+
    "<div class='muted' style='margin-bottom:6px'>symbols present in the full-universe backfill agg but "+
      "absent from the live STREAM, across the most LIVE groups — the FP_TICK_SYMBOLS coverage gap, "+
      "system-wide. Scored over "+t.n_live_groups+" live of "+t.n_groups+" groups; "+
      t.n_thin_symbols+" thin symbols total (top "+t.limit+").</div>"+
    "<table class='feat'><thead><tr><th>symbol</th><th class='num'>under-rep groups</th>"+
    "<th class='num'>live in</th><th>under-represented in (hover=all)</th></tr></thead>"+
    "<tbody>"+(rows||"<tr><td colspan='4' class='muted'>no under-represented tickers</td></tr>")+
    "</tbody></table></div>";
}

const WEEKDAY=["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
let TLOPEN=false;
function isWeekend(iso){ const wd=new Date(iso+"T00:00:00").getDay(); return wd===0||wd===6; }
// intensity = symbol count scaled against the group's own in-window peak, floored so any
// nonzero day stays visible (present-but-thin reads dimmer than a full day, absent = dark).
function tlIntensity(n, peak){
  if(n<=0 || peak<=0) return 0;
  return Math.max(0.22, n/peak);
}
function tlCellHtml(c, streamPeak, backfillPeak){
  const si = tlIntensity(c.stream, streamPeak), bi = tlIntensity(c.backfill, backfillPeak);
  const son = c.stream>0?"on":"off", bon = c.backfill>0?"on":"off";
  const sstyle = si>0 ? " style='opacity:"+si.toFixed(3)+"'" : "";
  const bstyle = bi>0 ? " style='opacity:"+bi.toFixed(3)+"'" : "";
  const tip = c.date+" · "+c.provenance+" · stream "+c.stream+" / backfill "+c.backfill+
    " (group peak "+streamPeak+" / "+backfillPeak+")";
  return "<td class='tlc' title=\\""+tip+"\\"><div class='pp'>"+
    "<i class='s "+son+"'"+sstyle+"></i><i class='b "+bon+"'"+bstyle+"></i></div></td>";
}
async function toggleTimeline(force){
  const host = document.getElementById("tlhost");
  if(TLOPEN && !force){ TLOPEN=false; host.innerHTML=""; return; }
  TLOPEN=true;
  host.innerHTML = "<div class='symcov muted'>loading depth &amp; timeline…</div>";
  const r = await fetch("/api/feature-grid/timeline" + (force?"?refresh=1":""));
  const t = await r.json();
  let head = "<thead><tr><th>group <span class='muted'>· backfill depth · live horizon</span></th>";
  for(const d of t.dates){
    const wknd = isWeekend(d) ? " wknd" : "";
    head += "<th class='"+wknd.trim()+"' title='"+d+"'>"+WEEKDAY[new Date(d+'T00:00:00').getDay()]+
      "<br><span class='muted'>"+d.slice(5)+"</span></th>";
  }
  head += "</tr></thead>";
  let body = "<tbody>";
  for(const g of t.groups){
    const depth = g.backfill_earliest ?
      (g.backfill_earliest+" → "+g.backfill_latest+" ("+g.backfill_span_days+"d)") : "no backfill";
    body += "<tr><th title='v"+g.version+" · "+g.n_features+"f'>"+g.group+
      "<br><span class='depth'>bf "+depth+" · live horizon "+g.stream_horizon_days+"d</span></th>";
    for(const c of g.days){ body += tlCellHtml(c, g.stream_peak||0, g.backfill_peak||0); }
    body += "</tr>";
  }
  body += "</tbody>";
  host.innerHTML =
    "<div class='symcov'>"+
    "<span class='closebtn' onclick='toggleTimeline(false)'>✕ close</span>"+
    "<div class='muted' style='margin-bottom:6px'>per (group × day) source coverage — upper bar = live "+
      "STREAM, lower = BACKFILL. Cell BRIGHTNESS ∝ that day's distinct-symbol count vs the group's own "+
      "busiest in-window day, so each row reads as a coverage-VOLUME heat sparkline (dim = thin capture, "+
      "bright = full, dark = absent) — thinning/thickening is legible, not just present/absent (hover for "+
      "counts). Group label shows backfill history DEPTH (earliest→latest span) and live HORIZON (recent "+
      "weekdays the stream captured unbroken). Anchor "+(t.anchor_date||"—")+", last "+t.days+" days.</div>"+
    "<div class='gridscroll'><table class='tl'>"+head+body+"</table></div></div>";
}
document.getElementById("tlbtn").onclick=()=>toggleTimeline(false);

let OFTOPEN=false;
async function toggleOrderflowTrend(force){
  const host = document.getElementById("ofthost");
  if(OFTOPEN && !force){ OFTOPEN=false; host.innerHTML=""; return; }
  OFTOPEN=true;
  host.innerHTML = "<div class='symcov muted'>loading order-flow live coverage trend…</div>";
  const r = await fetch("/api/feature-grid/orderflow-trend" + (force?"?refresh=1":""));
  const t = await r.json();
  // trend is most-recent-first; render oldest→newest left-to-right so the trend reads naturally.
  const days = t.trend.slice().reverse();
  const peak = Math.max(1, ...days.map(d=>d.n_union));
  let bars="";
  for(const d of days){
    const wknd = isWeekend(d.date) ? " wknd" : "";
    const uh = Math.round(100*d.n_union/peak), xh = Math.round(100*d.n_intersection/peak);
    const tip = d.date+" · union "+d.n_union+" symbols (≥1 group) · full-coverage "+d.n_intersection+
      " (all "+d.n_live_groups+" capturing groups)";
    bars += "<div class='bar"+wknd+"' title=\\""+tip+"\\"><div class='n'>"+(d.n_union||"")+"</div>"+
      "<div class='col' style='height:100px'>"+
      "<div class='u' style='height:"+uh+"%'></div><div class='x' style='height:"+xh+"%'></div></div>"+
      "<div class='lbl'>"+d.date.slice(5)+"</div></div>";
  }
  const delta = t.union_delta||0;
  const vclass = delta>0?"up":(delta<0?"down":"flat");
  const vword = delta>0?("WIDENING (+"+delta+")"):(delta<0?("SHRINKING ("+delta+")"):"FLAT");
  host.innerHTML =
    "<div class='symcov'>"+
    "<span class='closebtn' onclick='toggleOrderflowTrend(false)'>✕ close</span>"+
    "<div class='muted' style='margin-bottom:6px'>distinct symbols the LIVE stream carried per day across "+
      "the "+t.groups.length+" order-flow groups — bar = UNION (≥1 group, light blue), inner = "+
      "full-coverage core (every capturing group, dark). Tracks the FP_TICK_SYMBOLS widening-vs-stalling "+
      "decision for live order-flow certification. Anchor "+(t.anchor_date||"—")+", last "+t.days+" days. "+
      "Window edge-to-edge: <span class='verdict "+vclass+"'>"+vword+"</span> "+
      "("+t.oldest_captured_union+" → "+t.newest_captured_union+" symbols).</div>"+
    "<div class='oft'>"+(bars||"<div class='muted'>no live order-flow capture in window</div>")+"</div></div>";
}
document.getElementById("oftbtn").onclick=()=>toggleOrderflowTrend(false);

let TFOPEN=false;
function tfseg(n, total, cls, label){
  if(!n) return "";
  const w = (100*n/Math.max(1,total)).toFixed(1);
  // hide the inline count when the segment is too thin to fit the text
  return "<div class='"+cls+"' style='flex:"+n+"' title='"+label+": "+n+"'>"+(w>6?n:"")+"</div>";
}
async function toggleTrustFrontier(force){
  const host = document.getElementById("tfhost");
  if(TFOPEN && !force){ TFOPEN=false; host.innerHTML=""; return; }
  TFOPEN=true;
  host.innerHTML = "<div class='symcov muted'>loading trust frontier…</div>";
  const r = await fetch("/api/feature-grid/trust-frontier" + (force?"?refresh=1":""));
  const f = await r.json();
  const bar = "<div class='tf-bar'>"+
    tfseg(f.n_trusted, f.n_features, "tf-t", "trusted")+
    tfseg(f.n_eligible, f.n_features, "tf-e", "eligible")+
    tfseg(f.n_blocked, f.n_features, "tf-b", "blocked")+"</div>";
  let rows="";
  for(const g of f.groups){
    const tot=g.n_features||1;
    const seg=(n,cls)=> n? "<i class='"+cls+"' style='width:"+(100*n/tot)+"%'></i>" : "";
    const blocked = g.n_blocked? "<div class='tf-blocked'>blocked: "+g.blocked_features.join(" ")+"</div>" : "";
    rows += "<tr class='"+(g.n_blocked?"has-blocked":"")+"'><td>"+g.group+blocked+"</td>"+
      "<td class='num'>"+g.n_features+"</td>"+
      "<td class='num'>"+g.n_trusted+"</td><td class='num'>"+g.n_eligible+"</td><td class='num'>"+g.n_blocked+"</td>"+
      "<td class='num'>"+g.projected_trusted_pct+"%</td>"+
      "<td><span class='gbar'>"+seg(g.n_trusted,"tf-t")+seg(g.n_eligible,"tf-e")+seg(g.n_blocked,"tf-b")+"</span></td></tr>";
  }
  host.innerHTML =
    "<div class='symcov'>"+
    "<span class='closebtn' onclick='toggleTrustFrontier(false)'>✕ close</span>"+
    "<div class='muted' style='margin-bottom:6px'>How close the "+f.n_features+" features are to fully trusted. "+
      "<span class='tf-key'><b class='tf-t'>TRUSTED</b> "+f.n_trusted+" ("+f.trusted_pct+"%) · "+
      "<b class='tf-e'>ELIGIBLE</b> "+f.n_eligible+" ("+f.eligible_pct+"%) — no open defect, earns trust on the "+
      "next clean settled sweep · <b class='tf-b'>BLOCKED</b> "+f.n_blocked+" ("+f.blocked_pct+"%) — "+f.n_open_defects+
      " open parity defect(s), needs a fix.</span> If every eligible feature passes the next sweep, trust → "+
      "<b style='color:#56d364'>"+f.projected_trusted_pct+"%</b>.</div>"+
    bar+
    "<table class='tf'><thead><tr><th>group</th><th class='num'>feats</th><th class='num'>trusted</th>"+
      "<th class='num'>eligible</th><th class='num'>blocked</th><th class='num'>projected</th><th>split</th>"+
      "</tr></thead><tbody>"+rows+"</tbody></table></div>";
}
document.getElementById("tfbtn").onclick=()=>toggleTrustFrontier(false);

document.getElementById("thinbtn").onclick=()=>toggleThin(false);
document.getElementById("refresh").onclick=()=>{ loadGrid(true); if(TFOPEN) toggleTrustFrontier(true); if(THINOPEN) toggleThin(true); if(TLOPEN) toggleTimeline(true); if(OFTOPEN) toggleOrderflowTrend(true); };
document.getElementById("metric").onchange=e=>{ METRIC=e.target.value; renderGrid(); };
document.getElementById("sortby").onchange=e=>{ SORT=e.target.value; renderGrid(); };
document.getElementById("trustfilter").onchange=e=>{ TRUSTFILTER=e.target.value; renderGrid(); if(OPENGROUP) openDetail(OPENGROUP); };
document.getElementById("search").oninput=e=>{ SEARCH=e.target.value; if(OPENGROUP) openDetail(OPENGROUP); };
loadGrid(false);
</script>
</body></html>"""
