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
  .bar { display:inline-block; width:70px; height:8px; background:#0f1115; border:1px solid var(--border);
    border-radius:4px; vertical-align:middle; overflow:hidden; }
  .bar > span { display:block; height:100%; background:var(--amber); }
  .closebtn { float:right; cursor:pointer; color:var(--muted); font-size:13px; }
</style></head>
<body>
<header><h1>Feature Coverage &amp; Trust &nbsp;
  <a href="/">&larr; dashboard</a></h1>
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
    <button id="refresh">↻ refresh</button>
  </div>
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
    "<table class='feat'><thead><tr><th>feature (hover=description)</th><th>trust</th>"+
    "<th>trajectory to trusted</th><th class='num'>clean match-rate</th><th>last validated</th><th>layer</th>"+
    "</tr></thead><tbody>"+rows+"</tbody></table></div>";
  document.getElementById("detailhost").scrollIntoView({behavior:"smooth", block:"nearest"});
}
function closeDetail(){ OPENGROUP=null; document.getElementById("detailhost").innerHTML=""; }

document.getElementById("refresh").onclick=()=>loadGrid(true);
document.getElementById("metric").onchange=e=>{ METRIC=e.target.value; renderGrid(); };
document.getElementById("sortby").onchange=e=>{ SORT=e.target.value; renderGrid(); };
document.getElementById("trustfilter").onchange=e=>{ TRUSTFILTER=e.target.value; renderGrid(); if(OPENGROUP) openDetail(OPENGROUP); };
document.getElementById("search").oninput=e=>{ SEARCH=e.target.value; if(OPENGROUP) openDetail(OPENGROUP); };
loadGrid(false);
</script>
</body></html>"""
