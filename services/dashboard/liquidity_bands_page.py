"""The visual canonical ADV-rank / liquidity-band surface — a standalone dashboard page.

Vanilla HTML/JS (no build step), same dark theme as the other dashboard surfaces. Fetches
``/api/liquidity-bands`` client-side and renders the canonical liquidity partition every research lane can
reference instead of re-deriving: per-band COMPOSITION (how many symbols, the ADV range / boundaries) and
membership STABILITY (point-in-time band turnover over the window), plus a symbol lookup ("which band is X
in") and a band-members drill-in ("give me B4's universe").
"""

from __future__ import annotations

LIQUIDITY_BANDS_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Liquidity bands (ADV rank)</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg:#0f1115; --panel:#171a21; --border:#262b35; --text:#d7dce2; --muted:#8b949e;
    --accent:#58a6ff; --bar:#3fb950; --grid:#1b2230;
    --b1:#58a6ff; --b2:#3fb950; --b3:#d8a200; --b4:#e0772b; --b5:#cf3b3b;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  header { background:var(--panel); padding:14px 22px; border-bottom:1px solid var(--border); }
  header h1 { margin:0; font-size:18px; }
  header a { color:var(--accent); text-decoration:none; font-size:13px; font-weight:400; margin-left:10px; }
  .muted { color:var(--muted); }
  .wrap { padding:18px 22px; max-width:1200px; }
  .controls { display:flex; gap:10px; align-items:center; margin-bottom:16px; flex-wrap:wrap; }
  .controls select, .controls button, .controls input { background:var(--panel); color:var(--text);
    border:1px solid var(--border); border-radius:6px; padding:5px 10px; font-size:13px; }
  .controls button { cursor:pointer; }
  .controls button:hover { background:#1d212a; }
  .panel { background:var(--panel); border:1px solid var(--border); border-radius:8px;
    padding:14px 16px; margin-bottom:16px; }
  .panel h2 { margin:0 0 10px; font-size:15px; }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  th, td { text-align:left; padding:7px 9px; border-bottom:1px solid var(--border); white-space:nowrap; }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  .chip { display:inline-block; width:11px; height:11px; border-radius:3px; vertical-align:middle;
    margin-right:6px; }
  /* ADV-range bar: a log-scaled horizontal span from band min to max ADV, for the at-a-glance ladder */
  .advbar { position:relative; height:12px; background:#0f1115; border:1px solid var(--border);
    border-radius:3px; min-width:160px; }
  .advbar > span { position:absolute; top:0; bottom:0; border-radius:2px; opacity:0.85; }
  .ret { display:inline-block; height:9px; border-radius:2px; background:var(--bar); vertical-align:middle; }
  .retwrap { display:inline-block; width:90px; height:9px; background:#0f1115; border:1px solid var(--border);
    border-radius:2px; overflow:hidden; vertical-align:middle; margin-right:6px; }
  .lookup { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  .lookup .result { font-size:13px; }
  .members { font-family:ui-monospace,Menlo,monospace; font-size:11px; color:var(--muted); max-height:160px;
    overflow:auto; background:#0f1115; border:1px solid var(--border); border-radius:6px; padding:6px 8px;
    margin-top:8px; line-height:1.7; word-break:break-all; }
  .pill { padding:1px 7px; border-radius:10px; font-size:11px; font-weight:600; color:#0f1115; }
  .empty { color:var(--muted); font-style:italic; padding:8px 0; }
</style></head>
<body>
<header><h1>Liquidity bands <span class="muted" style="font-size:13px">· canonical ADV rank</span>
  <a href="/">&larr; dashboard</a><a href="/feature-grid">feature grid &rarr;</a>
  <a href="/raw-coverage">raw coverage &rarr;</a></h1>
  <div class="muted" id="subhead">loading…</div>
</header>
<div class="wrap">
  <div class="controls">
    <select id="window">
      <option value="85">window: last 85 trading days</option>
      <option value="60">last 60 days</option>
      <option value="120">last 120 days</option>
      <option value="0">full history (slow)</option>
    </select>
    <button id="refresh">↻ refresh</button>
  </div>

  <div class="panel">
    <h2>Band composition <span class="muted" id="compsub"></span></h2>
    <div id="composition"></div>
  </div>

  <div class="panel">
    <h2>Membership stability <span class="muted">· point-in-time band turnover (retained fraction)</span></h2>
    <div id="stability"></div>
  </div>

  <div class="panel">
    <h2>Symbol lookup</h2>
    <div class="lookup">
      <input id="symin" placeholder="ticker e.g. AAPL" size="14">
      <button id="symbtn">which band?</button>
      <span class="result" id="symres"></span>
    </div>
  </div>

  <div class="panel">
    <h2>Band members <span class="muted">· a band's universe, most-liquid first</span></h2>
    <div class="lookup">
      <select id="bandsel"></select>
      <button id="membtn">list members</button>
      <span class="result muted" id="memcount"></span>
    </div>
    <div id="memhost"></div>
  </div>
</div>
<script>
const BANDCOLOR = {B1:"var(--b1)", B2:"var(--b2)", B3:"var(--b3)", B4:"var(--b4)", B5:"var(--b5)"};
let WINDOW=85, SURFACE=null;

function fmtInt(n){ return (n==null?"—":Math.round(n).toLocaleString()); }
function fmtUSD(n){
  if(n==null) return "—";
  if(n>=1e9) return "$"+(n/1e9).toFixed(2)+"B";
  if(n>=1e6) return "$"+(n/1e6).toFixed(2)+"M";
  if(n>=1e3) return "$"+(n/1e3).toFixed(1)+"K";
  return "$"+n.toFixed(0);
}

async function load(force){
  const r = await fetch("/api/liquidity-bands?window_days="+WINDOW+(force?"&refresh=1":""));
  SURFACE = await r.json();
  document.getElementById("subhead").innerHTML =
    "ONE canonical liquidity partition — symbols ranked by trailing-"+SURFACE.adv_window+"d ADV (RTH dollar "+
    "volume), cut into the Lane-C bands. A lane references this instead of re-deriving its own cut. "+
    "Anchor <b>"+(SURFACE.anchor_date||"—")+"</b> · <b>"+fmtInt(SURFACE.n_ranked_symbols)+"</b> ranked "+
    "(≥"+SURFACE.min_days_for_rank+" valid days) over "+SURFACE.n_dates+" dates "+
    (SURFACE.window_first||"—")+"→"+(SURFACE.window_last||"—")+".";
  document.getElementById("compsub").textContent =
    "· ADV = mean trailing-"+SURFACE.adv_window+"d RTH dollar volume; rank 1 = most liquid";
  renderComposition();
  renderStability();
  fillBandSelect();
}

function renderComposition(){
  const bands = SURFACE.bands;
  if(!bands.some(b=>b.n_symbols)){ document.getElementById("composition").innerHTML =
    "<div class='empty'>no ranked symbols in this window</div>"; return; }
  // log-scale the ADV-range bars across all bands so the ladder is legible (ADV spans many orders of mag)
  const advs = bands.flatMap(b=>[b.adv_min, b.adv_max]).filter(v=>v!=null && v>0);
  const lmin = Math.log10(Math.min(...advs)), lmax = Math.log10(Math.max(...advs));
  const pos = v => (v==null||v<=0) ? 0 : 100*(Math.log10(v)-lmin)/Math.max(1e-9,(lmax-lmin));
  let rows = bands.map(b=>{
    const color = BANDCOLOR[b.band]||"var(--accent)";
    let advbar = "<div class='advbar'></div>";
    if(b.adv_min!=null && b.adv_max!=null){
      const left = pos(b.adv_min), right = pos(b.adv_max);
      advbar = "<div class='advbar'><span style='left:"+left+"%;width:"+Math.max(2,right-left)+
        "%;background:"+color+"'></span></div>";
    }
    return "<tr>"+
      "<td><span class='chip' style='background:"+color+"'></span><b>"+b.band+"</b></td>"+
      "<td class='muted'>"+b.label+"</td>"+
      "<td class='num'>rank "+b.rank_lo+"–"+b.rank_hi+"</td>"+
      "<td class='num'>"+fmtInt(b.n_symbols)+"</td>"+
      "<td class='num'>"+fmtUSD(b.adv_min)+"</td>"+
      "<td class='num'>"+fmtUSD(b.adv_median)+"</td>"+
      "<td class='num'>"+fmtUSD(b.adv_max)+"</td>"+
      "<td style='width:180px'>"+advbar+"</td></tr>";
  }).join("");
  document.getElementById("composition").innerHTML =
    "<table><thead><tr><th>band</th><th></th><th class='num'>rank range</th><th class='num'>symbols</th>"+
    "<th class='num'>ADV min</th><th class='num'>ADV median</th><th class='num'>ADV max</th>"+
    "<th>ADV range (log)</th></tr></thead><tbody>"+rows+"</tbody></table>";
}

function renderStability(){
  const st = SURFACE.stability;
  if(!st || !st.length){ document.getElementById("stability").innerHTML =
    "<div class='empty'>not enough history in this window for a turnover lookback</div>"; return; }
  const keys = Object.keys(st[0]).filter(k=>k.startsWith("retained_"));
  let head = "<tr><th>band</th><th class='num'>members today</th>"+
    keys.map(k=>"<th>"+k.replace("retained_","retained ").replace("_pct","")+"</th>").join("")+"</tr>";
  let rows = st.map(s=>{
    const color = BANDCOLOR[s.band]||"var(--accent)";
    const cells = keys.map(k=>{
      const v = s[k];
      if(v==null) return "<td class='muted'>—</td>";
      return "<td><span class='retwrap'><span class='ret' style='width:"+v+"%'></span></span>"+v+"%</td>";
    }).join("");
    return "<tr><td><span class='chip' style='background:"+color+"'></span><b>"+s.band+"</b></td>"+
      "<td class='num'>"+fmtInt(s.n_today)+"</td>"+cells+"</tr>";
  }).join("");
  document.getElementById("stability").innerHTML =
    "<table><thead>"+head+"</thead><tbody>"+rows+"</tbody></table>";
}

function fillBandSelect(){
  const sel = document.getElementById("bandsel");
  if(sel.options.length) return;
  sel.innerHTML = SURFACE.bands.map(b=>"<option value='"+b.band+"'>"+b.band+" — "+b.label+"</option>").join("");
}

async function lookupSymbol(){
  const sym = document.getElementById("symin").value.trim();
  if(!sym) return;
  const r = await fetch("/api/liquidity-bands/symbol/"+encodeURIComponent(sym)+"?window_days="+WINDOW);
  const d = await r.json();
  const el = document.getElementById("symres");
  if(!d.found){ el.innerHTML = "<span class='muted'>"+d.symbol+" — not ranked (below the "+
    SURFACE.min_days_for_rank+"-day floor or absent from the window)</span>"; return; }
  const color = BANDCOLOR[d.band]||"var(--accent)";
  el.innerHTML = "<b>"+d.symbol+"</b> &rarr; <span class='pill' style='background:"+color+"'>"+d.band+
    "</span> rank <b>"+fmtInt(d.rank)+"</b> · stable ADV <b>"+fmtUSD(d.adv)+"</b> · latest "+
    SURFACE.adv_window+"d ADV "+fmtUSD(d.latest_adv20)+" ("+d.n_valid_days+" valid days)";
}

async function listMembers(){
  const band = document.getElementById("bandsel").value;
  const r = await fetch("/api/liquidity-bands/members/"+encodeURIComponent(band)+"?window_days="+WINDOW);
  const d = await r.json();
  document.getElementById("memcount").textContent =
    d.n_symbols+" symbols in "+band+(d.shown<d.n_symbols ? " (showing first "+d.shown+")" : "");
  if(!d.members.length){ document.getElementById("memhost").innerHTML =
    "<div class='empty'>no members</div>"; return; }
  document.getElementById("memhost").innerHTML =
    "<div class='members'>"+d.members.map(m=>m.symbol+"<span style='color:#4b5563'>#"+m.rank+"</span>")
      .join("  ")+"</div>";
}

document.getElementById("window").onchange=e=>{ WINDOW=parseInt(e.target.value,10); load(false); };
document.getElementById("refresh").onclick=()=>load(true);
document.getElementById("symbtn").onclick=lookupSymbol;
document.getElementById("symin").addEventListener("keydown",e=>{ if(e.key==="Enter") lookupSymbol(); });
document.getElementById("membtn").onclick=listMembers;
load(false);
</script>
</body></html>"""
