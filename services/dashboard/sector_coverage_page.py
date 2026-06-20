"""The visual SECTOR coverage surface — a standalone page on the dashboard.

Vanilla HTML/JS (no build step), same dark theme as ``raw_coverage_page``. Fetches ``/api/sector-coverage``
client-side and renders: a headline classified-vs-unknown split (the % of the live universe carrying a sector
label), a per-sector bar/table (the 11 FMP-GICS sectors, ranked, so under-represented sectors read off at a
glance), and a sample of unclassified tickers (the mostly ETF/warrant/preferred tail, surfaced so the honest
partial coverage is visible).
"""

from __future__ import annotations

SECTOR_COVERAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Sector coverage</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg:#0f1115; --panel:#171a21; --border:#262b35; --text:#d7dce2; --muted:#8b949e;
    --accent:#58a6ff; --classified:#3fb950; --unknown:#e08a2b; --grid:#1b2230;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  header { background:var(--panel); padding:14px 22px; border-bottom:1px solid var(--border); }
  header h1 { margin:0; font-size:18px; }
  header a { color:var(--accent); text-decoration:none; font-size:13px; font-weight:400; margin-left:10px; }
  .muted { color:var(--muted); }
  .wrap { padding:18px 22px; max-width:1100px; }
  .controls { display:flex; gap:10px; align-items:center; margin-bottom:16px; }
  .controls button { background:var(--panel); color:var(--text);
    border:1px solid var(--border); border-radius:6px; padding:5px 10px; font-size:13px; cursor:pointer; }
  .panel { background:var(--panel); border:1px solid var(--border); border-radius:8px;
    padding:14px 16px; margin-bottom:16px; }
  .panel h2 { margin:0 0 10px; font-size:15px; }
  .stats { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }
  .stat { background:#0f1115; border:1px solid var(--border); border-radius:6px; padding:6px 11px; font-size:12px; }
  .stat b { font-variant-numeric:tabular-nums; font-size:14px; }
  /* headline split: one horizontal bar, classified (green) vs unknown (orange) */
  .split { display:flex; height:30px; border-radius:6px; overflow:hidden; border:1px solid var(--border); }
  .split .seg { display:flex; align-items:center; justify-content:center; font-size:12px; color:#0f1115;
    font-weight:600; white-space:nowrap; overflow:hidden; }
  .split .seg.classified { background:var(--classified); }
  .split .seg.unknown { background:var(--unknown); }
  .splitlabel { font-size:12px; color:var(--muted); margin-top:8px; }
  .splitlabel b { color:var(--text); font-variant-numeric:tabular-nums; }
  /* per-sector table with inline proportion bars */
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--grid); }
  th { color:var(--muted); font-weight:600; font-size:11px; text-transform:uppercase; }
  td.num { text-align:right; font-variant-numeric:tabular-nums; }
  .barcell { width:45%; }
  .barwrap { background:#0f1115; border:1px solid var(--border); border-radius:4px; height:16px; overflow:hidden; }
  .barwrap .fill { background:var(--accent); height:100%; border-radius:3px 0 0 3px; }
  /* unclassified sample chips */
  .chips { display:flex; flex-wrap:wrap; gap:5px; }
  .chip { background:#0f1115; border:1px solid var(--border); border-radius:4px; padding:2px 7px;
    font-size:11px; font-variant-numeric:tabular-nums; color:var(--muted); }
  .empty { color:var(--muted); font-style:italic; padding:8px 0; }
</style></head>
<body>
<header><h1>Sector coverage
  <a href="/">&larr; dashboard</a><a href="/feature-grid">feature grid &rarr;</a>
  <a href="/raw-coverage">raw-tape coverage &rarr;</a></h1>
  <div class="muted" id="subhead">loading…</div>
</header>
<div class="wrap">
  <div class="controls"><button id="refresh">↻ refresh</button></div>
  <div id="content"></div>
</div>
<script>
function fmtInt(n){ return (n==null?"—":n.toLocaleString()); }

async function load(force){
  const r = await fetch("/api/sector-coverage"+(force?"?refresh=1":""));
  const d = await r.json();
  document.getElementById("subhead").innerHTML =
    "FMP GICS-aligned sector labels joined onto the live universe ("+fmtInt(d.universe_size)+" symbols, "+
    "membership "+(d.universe_date||"—")+"). Unmapped names (mostly ETFs / warrants / preferred / units) "+
    "are bucketed as <b>sector_is_unknown</b> by design, never dropped.";
  document.getElementById("content").innerHTML =
    splitPanel(d) + sectorPanel(d) + unclassifiedPanel(d);
}

function splitPanel(d){
  const cls = d.classified_pct, unk = Math.round((100-cls)*10)/10;
  const clsW = Math.max(cls, 0), unkW = Math.max(100-cls, 0);
  const split =
    "<div class='split'>"+
      "<div class='seg classified' style='width:"+clsW+"%' title='classified "+cls+"%'>"+
        (clsW>=12 ? "classified "+cls+"%" : "")+"</div>"+
      "<div class='seg unknown' style='width:"+unkW+"%' title='unknown "+unk+"%'>"+
        (unkW>=12 ? "unknown "+unk+"%" : "")+"</div>"+
    "</div>";
  const stats =
    stat(fmtInt(d.universe_size), "live universe")+
    stat(fmtInt(d.n_classified), "classified")+
    stat(fmtInt(d.n_unknown), "unknown")+
    stat(d.classified_pct+"%", "classified of universe")+
    stat(fmtInt(d.n_distinct_sectors), "sectors present");
  const label =
    "Unknown breaks down into <b>"+fmtInt(d.n_blank_sector)+"</b> with a blank-sector map row + "+
    "<b>"+fmtInt(d.n_no_row)+"</b> with no sector_map row yet. Whole sector_map table: "+
    "<b>"+fmtInt(d.sector_map.n_rows)+"</b> rows, <b>"+fmtInt(d.sector_map.n_classified)+"</b> classified, "+
    "<b>"+fmtInt(d.sector_map.n_distinct_sectors)+"</b> distinct sectors.";
  return "<div class='panel'><h2>Classified vs unknown</h2>"+
    "<div class='stats'>"+stats+"</div>"+split+
    "<div class='splitlabel'>"+label+"</div></div>";
}

function stat(big, small){
  return "<div class='stat'><b>"+big+"</b> <span class='muted'>"+small+"</span></div>";
}

function sectorPanel(d){
  if(!d.sectors.length) return "<div class='panel'><h2>Per sector</h2>"+
    "<div class='empty'>no classified symbols in the live universe</div></div>";
  const peak = Math.max(1, ...d.sectors.map(s=>s.n_symbols));
  const rows = d.sectors.map(s=>{
    const w = Math.round(100*s.n_symbols/peak);
    return "<tr><td>"+s.sector+"</td>"+
      "<td class='num'>"+fmtInt(s.n_symbols)+"</td>"+
      "<td class='num'>"+s.pct_of_universe+"%</td>"+
      "<td class='barcell'><div class='barwrap'><div class='fill' style='width:"+w+"%'></div></div></td></tr>";
  }).join("");
  return "<div class='panel'><h2>Per sector <span class='muted' style='font-weight:400;font-size:12px'>"+
      "· "+d.sectors.length+" sectors, ranked by symbol count (% of the live universe)</span></h2>"+
    "<table><thead><tr><th>sector</th><th class='num'>symbols</th><th class='num'>% univ</th>"+
      "<th class='barcell'></th></tr></thead><tbody>"+rows+"</tbody></table></div>";
}

function unclassifiedPanel(d){
  const sample = d.unclassified_sample || [];
  if(!sample.length) return "";
  const chips = sample.map(s=>"<span class='chip'>"+s+"</span>").join("");
  return "<div class='panel'><h2>Unclassified sample "+
      "<span class='muted' style='font-weight:400;font-size:12px'>· "+sample.length+" of "+
      fmtInt(d.n_unknown)+" — mostly ETFs / warrants (W/WS) / preferred (.PR) / units</span></h2>"+
    "<div class='chips'>"+chips+"</div></div>";
}

document.getElementById("refresh").onclick=()=>load(true);
load(false);
</script>
</body></html>"""
