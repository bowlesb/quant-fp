"""The visual UNIVERSE coverage surface — a standalone page on the dashboard.

Vanilla HTML/JS (no build step), same dark theme as ``sector_coverage_page``. Fetches ``/api/universe-coverage``
client-side and renders: a headline captured-vs-available split (the % of the available filtered set actually
being captured), the latest-day gap (uncaptured names left on the table), and a per-day timeline so a re-cap /
breadth restore shows up as a step in the ratio. The whole-universe complement of the #223 per-group DROP
detector — a future silent re-cap is instantly visible here.
"""

from __future__ import annotations

UNIVERSE_COVERAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Universe coverage</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg:#0f1115; --panel:#171a21; --border:#262b35; --text:#d7dce2; --muted:#8b949e;
    --accent:#58a6ff; --captured:#3fb950; --uncaptured:#e08a2b; --capped:#f85149; --grid:#1b2230;
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
  .badge { display:inline-block; border-radius:5px; padding:2px 9px; font-size:12px; font-weight:600; }
  .badge.full { background:var(--captured); color:#0f1115; }
  .badge.thinned { background:var(--uncaptured); color:#0f1115; }
  .badge.capped { background:var(--capped); color:#0f1115; }
  /* headline split: one horizontal bar, captured (green) vs uncaptured (orange/red) */
  .split { display:flex; height:30px; border-radius:6px; overflow:hidden; border:1px solid var(--border); }
  .split .seg { display:flex; align-items:center; justify-content:center; font-size:12px; color:#0f1115;
    font-weight:600; white-space:nowrap; overflow:hidden; }
  .split .seg.captured { background:var(--captured); }
  .split .seg.uncaptured { background:var(--uncaptured); }
  .splitlabel { font-size:12px; color:var(--muted); margin-top:8px; }
  .splitlabel b { color:var(--text); font-variant-numeric:tabular-nums; }
  /* per-day timeline table with inline ratio bars */
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--grid); }
  th { color:var(--muted); font-weight:600; font-size:11px; text-transform:uppercase; }
  td.num { text-align:right; font-variant-numeric:tabular-nums; }
  .barcell { width:40%; }
  .barwrap { background:#0f1115; border:1px solid var(--border); border-radius:4px; height:16px; overflow:hidden;
    position:relative; }
  .barwrap .fill { height:100%; border-radius:3px 0 0 3px; }
  .barwrap .fill.full { background:var(--captured); }
  .barwrap .fill.thinned { background:var(--uncaptured); }
  .barwrap .fill.capped { background:var(--capped); }
  .flag { color:var(--uncaptured); font-size:11px; }
  .empty { color:var(--muted); font-style:italic; padding:8px 0; }
</style></head>
<body>
<header><h1>Universe coverage
  <a href="/">&larr; dashboard</a><a href="/feature-grid">feature grid &rarr;</a>
  <a href="/sector-coverage">sector coverage &rarr;</a></h1>
  <div class="muted" id="subhead">loading…</div>
</header>
<div class="wrap">
  <div class="controls"><button id="refresh">↻ refresh</button></div>
  <div id="content"></div>
</div>
<script>
function fmtInt(n){ return (n==null?"—":n.toLocaleString()); }

async function load(force){
  const r = await fetch("/api/universe-coverage"+(force?"?refresh=1":""));
  const d = await r.json();
  document.getElementById("subhead").innerHTML =
    "Captured universe (<b>universe_membership</b> in_universe count per session) vs the AVAILABLE filtered "+
    "set (<b>"+fmtInt(d.available)+"</b> tradable primary-venue common stocks surviving the seed's "+
    "exchange + ETF/fund screen). The gap = breadth that COULD be captured but isn't — the whole-universe "+
    "complement of the per-group DROP detector.";
  document.getElementById("content").innerHTML =
    headlinePanel(d) + timelinePanel(d);
}

function stat(big, small){
  return "<div class='stat'><b>"+big+"</b> <span class='muted'>"+small+"</span></div>";
}

function headlinePanel(d){
  const l = d.latest;
  if(!l) return "<div class='panel'><h2>Captured vs available</h2>"+
    "<div class='empty'>no captured universe days found</div></div>";
  const cap = l.ratio_pct, capW = Math.max(0, Math.min(100, cap)), uncapW = Math.max(0, 100-capW);
  const split =
    "<div class='split'>"+
      "<div class='seg captured' style='width:"+capW+"%' title='captured "+cap+"%'>"+
        (capW>=14 ? "captured "+cap+"%" : "")+"</div>"+
      "<div class='seg uncaptured' style='width:"+uncapW+"%' title='uncaptured "+(Math.round((100-cap)*10)/10)+"%'>"+
        (uncapW>=14 ? "uncaptured "+(Math.round((100-cap)*10)/10)+"%" : "")+"</div>"+
    "</div>";
  const stats =
    stat("<span class='badge "+l.status+"'>"+l.status+"</span>", "latest "+(l.date||"—"))+
    stat(fmtInt(l.captured), "captured")+
    stat(fmtInt(d.available), "available")+
    stat(l.ratio_pct+"%", "captured of available")+
    stat(fmtInt(l.uncaptured), "uncaptured (left on the table)");
  let label =
    "On <b>"+(l.date||"—")+"</b>, <b>"+fmtInt(l.captured)+"</b> of <b>"+fmtInt(d.available)+"</b> available "+
    "symbols are in the captured universe (<b>"+l.ratio_pct+"%</b>) — <b>"+fmtInt(l.uncaptured)+"</b> "+
    "filtered common stocks are NOT being captured. Bands: full &ge;"+
    Math.round(d.ratio_thresholds.ok*100)+"% / thinned &ge;"+Math.round(d.ratio_thresholds.thin*100)+
    "% / capped below.";
  if(l.over_available) label += " <span class='flag'>(this day's seed predated the current ETF screen, so its "+
    "captured count exceeds today's available set — ratio shown as 100%, informational only.)</span>";
  return "<div class='panel'><h2>Captured vs available "+
    "<span class='muted' style='font-weight:400;font-size:12px'>· latest session</span></h2>"+
    "<div class='stats'>"+stats+"</div>"+split+
    "<div class='splitlabel'>"+label+"</div></div>";
}

function timelinePanel(d){
  const tl = d.timeline || [];
  if(!tl.length) return "";
  const rows = tl.map(t=>{
    const w = Math.max(0, Math.min(100, t.ratio_pct));
    const flag = t.over_available ? " <span class='flag' title='pre-screen seed'>&#9650;</span>" : "";
    return "<tr><td>"+t.date+flag+"</td>"+
      "<td class='num'>"+fmtInt(t.captured)+"</td>"+
      "<td class='num'>"+fmtInt(t.uncaptured)+"</td>"+
      "<td class='num'>"+t.ratio_pct+"%</td>"+
      "<td class='barcell'><div class='barwrap'><div class='fill "+t.status+"' style='width:"+w+"%'></div>"+
        "</div></td></tr>";
  }).join("");
  return "<div class='panel'><h2>Per-day timeline "+
      "<span class='muted' style='font-weight:400;font-size:12px'>· captured / available ratio over recent "+
      "sessions (newest first) — a re-cap or breadth restore shows as a step</span></h2>"+
    "<table><thead><tr><th>date</th><th class='num'>captured</th><th class='num'>uncaptured</th>"+
      "<th class='num'>ratio</th><th class='barcell'></th></tr></thead><tbody>"+rows+"</tbody></table></div>";
}

document.getElementById("refresh").onclick=()=>load(true);
load(false);
</script>
</body></html>"""
