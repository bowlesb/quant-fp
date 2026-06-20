"""The SYSTEM PROGRESS scorecard — Ben's six platform axes (A-F) with a sparkline trend each.

Vanilla HTML/JS (no build step), same dark theme as ``raw_coverage_page``. Fetches
``/api/scorecard`` (current values) and ``/api/scorecard/history`` (the persisted snapshot time series) client-
side and renders six collapsible axis cards: each shows the current headline value + sub-stats + a small
sparkline drawn from the snapshot history, so the TRAJECTORY of each axis reads off at a glance — the thing
Ben actually looks at, not reconstructed each time.
"""

from __future__ import annotations

SCORECARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>System progress scorecard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg:#0f1115; --panel:#171a21; --border:#262b35; --text:#d7dce2; --muted:#8b949e;
    --good:#3fb950; --warn:#e08a2b; --bad:#f85149; --accent:#58a6ff; --grid:#1b2230;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  header { background:var(--panel); padding:14px 22px; border-bottom:1px solid var(--border); }
  header h1 { margin:0; font-size:18px; }
  header a { color:var(--accent); text-decoration:none; font-size:13px; font-weight:400; margin-left:10px; }
  .muted { color:var(--muted); }
  .wrap { padding:18px 22px; max-width:1400px; }
  .controls { display:flex; gap:10px; align-items:center; margin-bottom:16px; }
  .controls button { background:var(--panel); color:var(--text);
    border:1px solid var(--border); border-radius:6px; padding:5px 10px; font-size:13px; cursor:pointer; }
  .axes { display:grid; grid-template-columns:repeat(auto-fill,minmax(380px,1fr)); gap:16px; }
  .axis { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:14px 16px; }
  .axis h2 { margin:0 0 2px; font-size:14px; display:flex; align-items:baseline; gap:8px; cursor:pointer; }
  .axis h2 .key { color:var(--accent); font-weight:700; font-variant-numeric:tabular-nums; }
  .axis h2 .caret { margin-left:auto; color:var(--muted); font-size:12px; }
  .axis .sub { font-size:11px; color:var(--muted); margin-bottom:10px; }
  .headline { display:flex; align-items:baseline; gap:8px; margin-bottom:8px; }
  .headline .big { font-size:30px; font-weight:700; font-variant-numeric:tabular-nums; }
  .headline .unit { font-size:13px; color:var(--muted); }
  .headline .delta { font-size:12px; font-variant-numeric:tabular-nums; }
  .delta.up { color:var(--good); } .delta.down { color:var(--bad); } .delta.flat { color:var(--muted); }
  .stats { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:10px; }
  .stat { background:#0f1115; border:1px solid var(--border); border-radius:6px; padding:4px 9px; font-size:12px; }
  .stat b { font-variant-numeric:tabular-nums; }
  .spark { background:#0f1115; border:1px solid var(--border); border-radius:6px; padding:6px 8px; }
  .spark svg { display:block; width:100%; height:46px; }
  .spark .sparklabel { font-size:10px; color:var(--muted); margin-top:3px; display:flex; justify-content:space-between; }
  .detail { margin-top:10px; font-size:12px; color:var(--muted); display:none; }
  .axis.open .detail { display:block; }
  .detail .row { display:flex; justify-content:space-between; padding:2px 0; border-bottom:1px solid var(--grid); }
  .detail .row b { color:var(--text); font-variant-numeric:tabular-nums; }
  .empty { color:var(--muted); font-style:italic; }
</style></head>
<body>
<header><h1>System progress scorecard
  <a href="/">&larr; dashboard</a><a href="/feature-grid">feature grid &rarr;</a>
  <a href="/raw-coverage">raw coverage &rarr;</a></h1>
  <div class="muted" id="subhead">loading…</div>
</header>
<div class="wrap">
  <div class="controls">
    <button id="refresh">↻ refresh</button>
    <span class="muted" id="snapcount"></span>
  </div>
  <div class="axes" id="axes"></div>
</div>
<script>
let HISTORY = [];

function fmtInt(n){ return (n==null?"—":Number(n).toLocaleString()); }
function fmtPct(n){ return (n==null?"—":Number(n).toFixed(1)+"%"); }

// Sparkline over the snapshot history. ``pick`` maps a snapshot -> the scalar this axis trends. Nulls in the
// series are skipped (a gap), so a temporarily-unavailable axis (e.g. latency doc missing) does not zero the line.
function sparkline(pick, color){
  const pts = HISTORY.map(s => pick(s.axes)).map(v => (v==null?null:Number(v)));
  const valid = pts.filter(v => v!=null);
  if(valid.length < 2){
    return "<div class='spark'><div class='empty'>not enough history yet — trend appears after a few snapshots</div></div>";
  }
  const lo = Math.min(...valid), hi = Math.max(...valid);
  const span = (hi-lo) || 1;
  const W=100, H=40;
  const n = pts.length;
  let d="", started=false;
  pts.forEach((v,i) => {
    if(v==null) return;
    const x = (n===1?0:(i/(n-1))*W);
    const y = H - ((v-lo)/span)*H;
    d += (started?" L":"M") + x.toFixed(1) + " " + y.toFixed(1);
    started = true;
  });
  const first = valid[0], last = valid[valid.length-1];
  return "<div class='spark'>"+
    "<svg viewBox='0 0 "+W+" "+H+"' preserveAspectRatio='none'>"+
      "<path d='"+d+"' fill='none' stroke='"+color+"' stroke-width='1.5' "+
        "vector-effect='non-scaling-stroke' stroke-linejoin='round'/>"+
    "</svg>"+
    "<div class='sparklabel'><span>"+fmtInt(first)+"</span><span>min "+fmtInt(lo)+" · max "+fmtInt(hi)+
      "</span><span>"+fmtInt(last)+"</span></div>"+
    "</div>";
}

// Delta of an axis's headline scalar between the first and last snapshot (the net move over the kept history).
function delta(pick){
  const pts = HISTORY.map(s => pick(s.axes)).map(v => (v==null?null:Number(v))).filter(v => v!=null);
  if(pts.length < 2) return "";
  const d = pts[pts.length-1] - pts[0];
  const cls = d>0?"up":(d<0?"down":"flat");
  const sign = d>0?"+":"";
  return "<span class='delta "+cls+"'>"+sign+fmtInt(d)+" over "+pts.length+" snaps</span>";
}

function stat(big, small){ return "<div class='stat'><b>"+big+"</b> <span class='muted'>"+small+"</span></div>"; }
function detailRow(label, val){ return "<div class='row'><span>"+label+"</span><b>"+val+"</b></div>"; }

function axisCard(key, title, sub, headlineBig, headlineUnit, deltaPick, statsHtml, sparkPick, color, detailHtml){
  return "<div class='axis'>"+
    "<h2><span class='key'>"+key+"</span> "+title+"<span class='caret'>▸ detail</span></h2>"+
    "<div class='sub'>"+sub+"</div>"+
    "<div class='headline'><span class='big' style='color:"+color+"'>"+headlineBig+"</span>"+
      "<span class='unit'>"+headlineUnit+"</span>"+delta(deltaPick)+"</div>"+
    "<div class='stats'>"+statsHtml+"</div>"+
    sparkline(sparkPick, color)+
    "<div class='detail'>"+detailHtml+"</div>"+
    "</div>";
}

function render(d){
  const a = d.axes;
  const cards = [];

  // A — features TRUSTED
  cards.push(axisCard("A", "Features trusted",
    "trust_state='TRUSTED' as % of the live catalog",
    fmtInt(a.A_trusted.value), "of "+fmtInt(a.A_trusted.total)+" · "+fmtPct(a.A_trusted.pct),
    x=>x.A_trusted.value,
    stat(fmtPct(a.A_trusted.pct), "trusted")+stat(fmtInt(a.A_trusted.total), "catalog"),
    x=>x.A_trusted.value, "var(--good)",
    detailRow("trusted features", fmtInt(a.A_trusted.value))+
    detailRow("catalog total", fmtInt(a.A_trusted.total))+
    detailRow("trusted %", fmtPct(a.A_trusted.pct))));

  // B — features deployed (live fingerprint set)
  cards.push(axisCard("B", "Features deployed",
    "live bus schema — the deployed fingerprint set",
    fmtInt(a.B_deployed.value), "features · "+fmtInt(a.B_deployed.groups)+" groups",
    x=>x.B_deployed.value,
    stat(fmtInt(a.B_deployed.groups), "groups")+stat(a.B_deployed.fingerprint, "fingerprint"),
    x=>x.B_deployed.value, "var(--accent)",
    detailRow("features (n_features)", fmtInt(a.B_deployed.value))+
    detailRow("feature groups", fmtInt(a.B_deployed.groups))+
    detailRow("bus fingerprint", a.B_deployed.fingerprint)));

  // C — trust-process health
  cards.push(axisCard("C", "Trust-process health",
    "the lifecycle frontier: eligible vs blocked",
    fmtInt(a.C_process_health.eligible), "eligible (1 clean sweep from trusted)",
    x=>x.C_process_health.eligible,
    stat(fmtInt(a.C_process_health.blocked), "blocked")+
    stat(fmtInt(a.C_process_health.open_defects), "open defects")+
    stat(fmtPct(a.C_process_health.projected_trusted_pct), "projected trusted"),
    x=>x.C_process_health.eligible, "var(--warn)",
    detailRow("eligible (no open defect)", fmtInt(a.C_process_health.eligible))+
    detailRow("blocked (open defect)", fmtInt(a.C_process_health.blocked))+
    detailRow("open parity defects", fmtInt(a.C_process_health.open_defects))+
    detailRow("projected trusted %", fmtPct(a.C_process_health.projected_trusted_pct))));

  // D — latency bar->vector
  const L = a.D_latency;
  const latBig = L.available ? fmtInt(L.p99_ms) : "—";
  cards.push(axisCard("D", "Latency bar→vector",
    "end-to-end p50/p99 (documented baseline · budget 100ms)",
    latBig, L.available ? "ms p99 · "+fmtInt(L.p50_ms)+"ms p50" : "no baseline parsed",
    x=>x.D_latency.p99_ms,
    L.available ? stat(fmtInt(L.p50_ms)+"ms", "p50")+stat(fmtInt(L.p99_ms)+"ms", "p99")+stat(fmtInt(L.budget_ms)+"ms", "budget")
                : stat("—","unavailable"),
    x=>x.D_latency.p99_ms, "var(--bad)",
    detailRow("p50 (ms)", L.available?fmtInt(L.p50_ms):"—")+
    detailRow("p99 (ms)", L.available?fmtInt(L.p99_ms):"—")+
    detailRow("budget (ms)", L.available?fmtInt(L.budget_ms):"—")+
    detailRow("source", L.source)));

  // E — raw-coverage depth + breadth
  const E = a.E_raw_coverage;
  const layers = E.layers || {};
  const bars = layers.bars||{}, trades = layers.trades||{}, quotes = layers.quotes||{};
  let eStats = "";
  let eDetail = detailRow("raw span", (E.span_earliest||"—")+" → "+(E.span_latest||"—"));
  ["bars","trades","quotes"].forEach(t=>{
    const Ly = layers[t]||{};
    eStats += stat(fmtInt(Ly.span_days)+"d", t+" span");
    eDetail += detailRow(t+" — span days", fmtInt(Ly.span_days))+
      detailRow(t+" — symbols/day (median)", fmtInt(Ly.median_symbols_per_day))+
      detailRow(t+" — distinct symbols", fmtInt(Ly.n_symbols));
  });
  cards.push(axisCard("E", "Raw-tape coverage",
    "depth (span days) + breadth (symbols/day), per layer",
    fmtInt(bars.span_days), "days bars depth · "+fmtInt(trades.median_symbols_per_day)+" trades sym/day",
    x=>(x.E_raw_coverage.bars||{}).span_days,
    eStats, x=>(x.E_raw_coverage.bars||{}).span_days, "var(--good)", eDetail));

  // F — open issues
  const F = a.F_open_issues;
  cards.push(axisCard("F", "Open issues",
    "open parity defects + open PRs + quarantined features",
    fmtInt(F.open_defects), "open defects · "+(F.open_prs==null?"gh n/a":fmtInt(F.open_prs)+" open PRs"),
    x=>x.F_open_issues.open_defects,
    stat(F.open_prs==null?"—":fmtInt(F.open_prs), "open PRs")+
    stat(fmtInt(F.quarantined), "quarantined"),
    x=>x.F_open_issues.open_defects, "var(--warn)",
    detailRow("open parity defects", fmtInt(F.open_defects))+
    detailRow("open PRs", F.open_prs==null?"gh unavailable":fmtInt(F.open_prs))+
    detailRow("quarantined features", fmtInt(F.quarantined))));

  document.getElementById("axes").innerHTML = cards.join("");
  document.querySelectorAll(".axis h2").forEach(h => h.onclick = () => h.parentElement.classList.toggle("open"));
}

async function load(force){
  const [scR, hiR] = await Promise.all([
    fetch("/api/scorecard"+(force?"?refresh=1":"")),
    fetch("/api/scorecard/history"),
  ]);
  const d = await scR.json();
  HISTORY = await hiR.json();
  document.getElementById("subhead").innerHTML =
    "the six platform axes Ben tracks over time — current value + trend. Generated "+
    (d.generated_at||"").slice(0,19).replace("T"," ")+" UTC.";
  document.getElementById("snapcount").textContent = HISTORY.length+" snapshots in the trend history";
  render(d);
}

document.getElementById("refresh").onclick=()=>load(true);
load(false);
</script>
</body></html>"""
