"""The visual LIQUIDITY-BAND reference surface — a standalone page on the dashboard.

Vanilla HTML/JS (no build step), same dark theme as ``raw_coverage_page``. Fetches
``/api/liquidity-bands`` client-side and renders the canonical ADV-rank bands every research lane otherwise
re-derives ad hoc:
  * BAND SIZES over time — a stacked per-date bar of how many symbols sit in each band (the universe a
    band-conditioned strategy would hold each day),
  * MEMBERSHIP STABILITY — the day-to-day band-cross rate (overall + per band), i.e. the turnover a
    band-conditioned strategy inherits just from names drifting across the cut,
  * an AS-OF SNAPSHOT — each band's current size, ADV range, and a member sample, plus a symbol lookup
    (a symbol's ADV-rank history) and a band lookup (a band's full current membership = the reproducible
    universe export, e.g. band "2000-4000" == Lane C's B4 small-cap niche).
"""

from __future__ import annotations

LIQUIDITY_BANDS_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Liquidity bands</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg:#0f1115; --panel:#171a21; --border:#262b35; --text:#d7dce2; --muted:#8b949e;
    --accent:#58a6ff; --grid:#1b2230;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  header { background:var(--panel); padding:14px 22px; border-bottom:1px solid var(--border); }
  header h1 { margin:0; font-size:18px; }
  header a { color:var(--accent); text-decoration:none; font-size:13px; font-weight:400; margin-left:10px; }
  .muted { color:var(--muted); }
  .wrap { padding:18px 22px; max-width:1400px; }
  .controls { display:flex; gap:10px; align-items:center; margin-bottom:16px; flex-wrap:wrap; }
  .controls select, .controls button, .controls input { background:var(--panel); color:var(--text);
    border:1px solid var(--border); border-radius:6px; padding:5px 10px; font-size:13px; }
  .controls button { cursor:pointer; }
  .controls input { width:170px; }
  .panel { background:var(--panel); border:1px solid var(--border); border-radius:8px;
    padding:14px 16px; margin-bottom:16px; }
  .panel h2 { margin:0 0 4px; font-size:15px; }
  .panel .sub { font-size:12px; color:var(--muted); margin-bottom:10px; }
  .stats { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }
  .stat { background:#0f1115; border:1px solid var(--border); border-radius:6px; padding:5px 10px; font-size:12px; }
  .stat b { font-variant-numeric:tabular-nums; }
  /* stacked band-size-over-time columns */
  .stack { display:flex; align-items:flex-end; gap:1px; height:160px; padding:6px 4px; overflow-x:auto;
    background:#0f1115; border:1px solid var(--border); border-radius:6px; }
  .col { min-width:4px; flex:1 0 4px; display:flex; flex-direction:column-reverse; }
  .col .seg { width:100%; }
  .axis { display:flex; justify-content:space-between; font-size:10px; color:var(--muted); margin-top:3px; }
  .legend { display:flex; gap:12px; flex-wrap:wrap; margin:8px 0 4px; font-size:12px; }
  .legend .it { display:flex; align-items:center; gap:5px; }
  .legend .sw { width:11px; height:11px; border-radius:3px; display:inline-block; }
  table { border-collapse:collapse; width:100%; font-size:12px; }
  th, td { text-align:right; padding:4px 9px; border-bottom:1px solid var(--border); font-variant-numeric:tabular-nums; }
  th:first-child, td:first-child { text-align:left; }
  th { color:var(--muted); font-weight:600; }
  td.band { font-weight:600; }
  .members { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:11px; color:var(--muted);
    word-break:break-word; }
  .lookup { display:flex; gap:16px; flex-wrap:wrap; }
  .lookup .col50 { flex:1 1 420px; min-width:320px; }
  .empty { color:var(--muted); font-style:italic; padding:8px 0; }
  .pill { display:inline-block; padding:1px 7px; border-radius:10px; font-size:11px; font-weight:600; }
</style></head>
<body>
<header><h1>Liquidity bands
  <a href="/">&larr; dashboard</a><a href="/raw-coverage">raw coverage &rarr;</a></h1>
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
    <input id="cuts" placeholder="cuts e.g. 500,1000,2000,4000" title="ADV-rank band cuts (blank = default)">
    <button id="apply">apply</button>
    <button id="refresh">↻ refresh</button>
  </div>
  <div id="content"></div>
  <div class="panel">
    <h2>Lookups</h2>
    <div class="sub">Given a symbol → its ADV-rank history; given a band → its full current membership
      (the reproducible-universe export a lane uses instead of an ad-hoc top-N).</div>
    <div class="lookup">
      <div class="col50">
        <div class="controls">
          <input id="symq" placeholder="symbol e.g. AAPL"><button id="symgo">rank history</button>
        </div>
        <div id="symout"><div class="empty">enter a symbol</div></div>
      </div>
      <div class="col50">
        <div class="controls">
          <select id="bandq"></select><button id="bandgo">members</button>
        </div>
        <div id="bandout"><div class="empty">pick a band</div></div>
      </div>
    </div>
  </div>
</div>
<script>
const PALETTE = ["#58a6ff","#3fb950","#e08a2b","#bc6cff","#f0506e","#39c5cf","#d29922","#8b949e"];
let DAYS=90, CUTS="", LABELS=[], COLOR={};

function fmtInt(n){ return (n==null?"—":Math.round(n).toLocaleString()); }
function fmtAdv(n){ if(n==null) return "—"; if(n>=1e9) return "$"+(n/1e9).toFixed(2)+"B";
  if(n>=1e6) return "$"+(n/1e6).toFixed(2)+"M"; if(n>=1e3) return "$"+(n/1e3).toFixed(1)+"K"; return "$"+Math.round(n); }
function qstr(extra){ let q="?days="+DAYS; if(CUTS) q+="&cuts="+encodeURIComponent(CUTS);
  return q+(extra||""); }

async function load(force){
  const r = await fetch("/api/liquidity-bands"+qstr(force?"&refresh=1":""));
  if(!r.ok){ document.getElementById("content").innerHTML =
    "<div class='panel empty'>"+(await r.text())+"</div>"; return; }
  const d = await r.json();
  LABELS = d.band_labels || [];
  COLOR = {}; LABELS.forEach((lab,i)=>COLOR[lab]=PALETTE[i%PALETTE.length]);
  const span = d.earliest ? (d.earliest+" → "+d.latest) : "no ranked tape";
  document.getElementById("subhead").innerHTML =
    "canonical trailing-"+d.adv_window+"d dollar-volume (ADV) RANK over the raw bars — pick a reproducible "+
    "liquidity universe by BAND instead of an ad-hoc top-N. Cuts <b>["+(d.cuts||[]).join(", ")+"]</b>, "+
    "span <b>"+span+"</b>, <b>"+fmtInt(d.n_ranked_symbols)+"</b> ranked symbols. "+
    "Point-in-time (≥"+d.min_trailing_days+" trailing days to rank).";
  fillBandSelect();
  document.getElementById("content").innerHTML =
    sizesPanel(d) + stabilityPanel(d) + snapshotPanel(d);
}

function legend(){
  return "<div class='legend'>"+LABELS.map(lab=>
    "<div class='it'><span class='sw' style='background:"+COLOR[lab]+"'></span>"+lab+"</div>").join("")+"</div>";
}

function sizesPanel(d){
  const tl = d.timeline||[];
  if(!tl.length) return "<div class='panel'><h2>Band sizes over time</h2><div class='empty'>no ranked dates</div></div>";
  const peak = Math.max(1, ...tl.map(c=>c.total));
  const cols = tl.map(c=>{
    const segs = LABELS.map(lab=>{
      const n = (c.bands&&c.bands[lab])||0;
      const h = 100*n/peak;
      return n? "<div class='seg' style='height:"+h+"%;background:"+COLOR[lab]+"' title=\\""+
        c.date+" · "+lab+" · "+fmtInt(n)+"\\"></div>" : "";
    }).join("");
    return "<div class='col' title='"+c.date+" · total "+fmtInt(c.total)+"'>"+segs+"</div>";
  }).join("");
  const from=tl[0].date, to=tl[tl.length-1].date;
  return "<div class='panel'><h2>Band sizes over time</h2>"+
    "<div class='sub'>symbols per band per day — the universe a band-conditioned strategy holds "+
      "(shown "+tl.length+" dates "+from+"→"+to+", peak total "+fmtInt(peak)+")</div>"+
    legend()+
    "<div class='stack'>"+cols+"</div>"+
    "<div class='axis'><span>"+from+"</span><span>"+to+"</span></div></div>";
}

function stabilityPanel(d){
  const s = d.stability||{per_band:{}};
  const rows = LABELS.map(lab=>{
    const pb = (s.per_band||{})[lab];
    if(!pb) return "";
    const pct = (pb.cross_rate*100).toFixed(1)+"%";
    return "<tr><td class='band'><span class='pill' style='background:"+COLOR[lab]+"22;color:"+COLOR[lab]+
      "'>"+lab+"</span></td><td>"+fmtInt(pb.pairs)+"</td><td>"+fmtInt(pb.crosses)+"</td><td>"+pct+"</td></tr>";
  }).join("");
  return "<div class='panel'><h2>Membership stability</h2>"+
    "<div class='sub'>day-to-day band-cross rate — how often a symbol drifts across a cut "+
      "(the turnover a band-conditioned strategy inherits for free). Overall <b>"+
      ((s.overall_cross_rate||0)*100).toFixed(1)+"%</b> over "+fmtInt(s.n_transitions)+" symbol-day pairs.</div>"+
    "<table><thead><tr><th>band</th><th>day-pairs</th><th>crosses</th><th>cross rate</th></tr></thead>"+
    "<tbody>"+rows+"</tbody></table></div>";
}

function snapshotPanel(d){
  const snap = d.snapshot||{bands:{}};
  const rows = LABELS.map(lab=>{
    const b = (snap.bands||{})[lab]||{n:0};
    if(!b.n) return "<tr><td class='band'>"+lab+"</td><td>0</td><td>—</td><td colspan=3 class='members'>—</td></tr>";
    const range = (b.rank_lo||"?")+"–"+(b.rank_hi||"?");
    const adv = fmtAdv(b.min_adv)+" – "+fmtAdv(b.max_adv)+" (med "+fmtAdv(b.median_adv)+")";
    const samp = (b.members_sample||[]).join(" ");
    return "<tr><td class='band'><span class='pill' style='background:"+COLOR[lab]+"22;color:"+COLOR[lab]+
      "'>"+lab+"</span></td><td>"+fmtInt(b.n)+"</td><td>"+range+"</td><td class='members' colspan=3 "+
      "title='"+adv+"'>"+samp+"</td></tr>";
  }).join("");
  return "<div class='panel'><h2>Band snapshot · "+(snap.date||"—")+"</h2>"+
    "<div class='sub'>each band as of the latest ranked date — size, ADV rank range, and a member sample "+
      "(hover for the ADV range)</div>"+
    "<table><thead><tr><th>band</th><th>n</th><th>rank range</th><th>members (sample)</th></tr></thead>"+
    "<tbody>"+rows+"</tbody></table></div>";
}

function fillBandSelect(){
  const sel=document.getElementById("bandq");
  const cur=sel.value;
  sel.innerHTML = LABELS.map(lab=>"<option value='"+lab+"'>"+lab+"</option>").join("");
  if(LABELS.includes(cur)) sel.value=cur;
}

async function symLookup(){
  const sym=document.getElementById("symq").value.trim().toUpperCase();
  if(!sym) return;
  const r=await fetch("/api/liquidity-bands/symbol/"+encodeURIComponent(sym)+(CUTS?"?cuts="+encodeURIComponent(CUTS):""));
  const d=await r.json();
  const out=document.getElementById("symout");
  if(!d.n_dates){ out.innerHTML="<div class='empty'>"+sym+" — not ranked (insufficient history or no tape)</div>"; return; }
  const recent=d.history.slice(-30);
  const rows=recent.map(h=>"<tr><td>"+h.date+"</td><td>"+fmtAdv(h.adv_20d)+"</td><td>"+fmtInt(h.adv_rank)+
    "</td><td class='band'><span class='pill' style='background:"+(COLOR[h.band]||"#8b949e")+"22;color:"+
    (COLOR[h.band]||"#8b949e")+"'>"+h.band+"</span></td></tr>").join("");
  out.innerHTML="<div class='sub'>"+sym+" — "+d.n_dates+" ranked dates (last 30 shown)</div>"+
    "<table><thead><tr><th>date</th><th>ADV 20d</th><th>rank</th><th>band</th></tr></thead><tbody>"+rows+"</tbody></table>";
}

async function bandLookup(){
  const band=document.getElementById("bandq").value;
  if(!band) return;
  const r=await fetch("/api/liquidity-bands/members/"+encodeURIComponent(band)+(CUTS?"?cuts="+encodeURIComponent(CUTS):""));
  const d=await r.json();
  const out=document.getElementById("bandout");
  if(!d.n_members){ out.innerHTML="<div class='empty'>"+band+" — empty as of "+(d.asof||"—")+"</div>"; return; }
  const head="<div class='sub'>band <b>"+band+"</b> as of "+d.asof+" — <b>"+fmtInt(d.n_members)+
    "</b> members (reproducible universe)</div>";
  const syms=d.members.map(m=>m.symbol).join(" ");
  out.innerHTML=head+"<div class='members'>"+syms+"</div>";
}

document.getElementById("days").onchange=e=>{ DAYS=parseInt(e.target.value,10); load(false); };
document.getElementById("apply").onclick=()=>{ CUTS=document.getElementById("cuts").value.trim(); load(false); };
document.getElementById("refresh").onclick=()=>load(true);
document.getElementById("symgo").onclick=symLookup;
document.getElementById("symq").addEventListener("keydown",e=>{ if(e.key==="Enter") symLookup(); });
document.getElementById("bandgo").onclick=bandLookup;
load(false);
</script>
</body></html>"""
