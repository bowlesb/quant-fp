"""Local-network dashboard for the quant trading system.

Serves a single status page on the LAN showing build progress (rendered from
STATE.md / JOURNAL.md) and live system health queried directly from TimescaleDB.
Read-only: no secrets, no controls. Ben checks it in a browser; Claude reads
/status.json or the DB directly in-session.
"""
import os
from pathlib import Path
from typing import Any

import markdown
import psycopg
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Quant Dashboard")

DOCS_DIR = Path("/docs")

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}

# Tables whose row counts and latest-timestamp we surface as health signals.
TRACKED_TABLES = [
    "bars_1m",
    "quote_agg_1m",
    "trade_agg_1m",
    "trades_raw",
    "news",
    "feature_vectors",
    "predictions",
    "orders_log",
    "fills_log",
]
TABLES_WITH_TS = {
    "bars_1m",
    "quote_agg_1m",
    "trade_agg_1m",
    "trades_raw",
    "news",
    "feature_vectors",
    "predictions",
}


def collect_metrics() -> dict[str, Any]:
    """Query live system health from the database."""
    metrics: dict[str, Any] = {"db_ok": False, "tables": {}}
    try:
        with psycopg.connect(**DB_KWARGS, connect_timeout=5) as conn:
            metrics["db_ok"] = True
            with conn.cursor() as cur:
                for table in TRACKED_TABLES:
                    row: dict[str, Any] = {}
                    cur.execute(f"SELECT count(*) FROM {table}")
                    count_row = cur.fetchone()
                    row["rows"] = count_row[0] if count_row else 0
                    if table in TABLES_WITH_TS:
                        ts_col = "created_at" if table == "news" else "ts"
                        cur.execute(f"SELECT max({ts_col}) FROM {table}")
                        ts_row = cur.fetchone()
                        row["latest"] = ts_row[0].isoformat() if ts_row and ts_row[0] else None
                    metrics["tables"][table] = row

                cur.execute(
                    "SELECT ok, ts FROM reconciliation_log ORDER BY ts DESC LIMIT 1"
                )
                recon = cur.fetchone()
                metrics["last_reconciliation"] = (
                    {"ok": recon[0], "ts": recon[1].isoformat()} if recon else None
                )

                cur.execute(
                    """
                    SELECT trade_date, symbol, received_minutes, expected_minutes
                    FROM data_quality_daily
                    WHERE trade_date = (SELECT max(trade_date) FROM data_quality_daily)
                    ORDER BY symbol
                    """
                )
                metrics["coverage"] = [
                    {
                        "date": r[0].isoformat(),
                        "symbol": r[1],
                        "received": r[2],
                        "expected": r[3],
                        "pct": round(100.0 * r[2] / r[3], 1) if r[3] else 0.0,
                    }
                    for r in cur.fetchall()
                ]
    except psycopg.Error as exc:
        metrics["error"] = str(exc)
    return metrics


def render_doc(name: str) -> str:
    path = DOCS_DIR / name
    if not path.exists():
        return f"<p><em>{name} not mounted.</em></p>"
    return markdown.markdown(
        path.read_text(encoding="utf-8"), extensions=["tables", "fenced_code"]
    )


@app.get("/status.json")
def status_json() -> JSONResponse:
    return JSONResponse(collect_metrics())


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    metrics = collect_metrics()
    db_badge = (
        "<span class='ok'>● connected</span>"
        if metrics["db_ok"]
        else "<span class='bad'>● down</span>"
    )

    rows_html = ""
    for table, info in metrics.get("tables", {}).items():
        latest = info.get("latest") or "—"
        rows_html += (
            f"<tr><td>{table}</td><td class='num'>{info['rows']:,}</td>"
            f"<td>{latest}</td></tr>"
        )

    recon = metrics.get("last_reconciliation")
    recon_html = "no reconciliation yet" if not recon else (
        f"{'OK' if recon['ok'] else 'MISMATCH'} @ {recon['ts']}"
    )

    coverage = metrics.get("coverage", [])
    if coverage:
        cov_rows = "".join(
            f"<tr><td>{c['symbol']}</td><td class='num'>{c['received']}/{c['expected']}</td>"
            f"<td class='num'>{c['pct']}%</td></tr>"
            for c in coverage
        )
        cov_html = (
            f"<h2 style='margin-top:0;font-size:15px;'>Coverage ({coverage[0]['date']})</h2>"
            f"<table><thead><tr><th>symbol</th><th class='num'>bars</th>"
            f"<th class='num'>coverage</th></tr></thead><tbody>{cov_rows}</tbody></table>"
        )
    else:
        cov_html = "<h2 style='margin-top:0;font-size:15px;'>Coverage</h2><p class='muted'>no coverage data yet</p>"

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Quant Dashboard</title>
<meta http-equiv="refresh" content="30">
<style>
  body {{ font-family: system-ui, sans-serif; margin: 0; background:#0f1115; color:#d7dce2; }}
  header {{ background:#171a21; padding:16px 24px; border-bottom:1px solid #262b35; }}
  h1 {{ font-size:18px; margin:0; }}
  .wrap {{ padding:24px; max-width:1100px; margin:0 auto; }}
  .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:24px; }}
  .card {{ background:#171a21; border:1px solid #262b35; border-radius:8px; padding:16px 20px; }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  th,td {{ text-align:left; padding:6px 8px; border-bottom:1px solid #262b35; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .ok {{ color:#3fb950; }} .bad {{ color:#f85149; }}
  .doc {{ font-size:14px; line-height:1.5; }}
  .doc h2 {{ font-size:15px; border-bottom:1px solid #262b35; padding-bottom:4px; }}
  .doc code {{ background:#0f1115; padding:1px 4px; border-radius:3px; }}
  .doc pre {{ background:#0f1115; padding:12px; border-radius:6px; overflow:auto; }}
  .muted {{ color:#8b949e; font-size:12px; }}
</style></head>
<body>
<header><h1>Quant Trading System &nbsp; {db_badge}</h1>
<div class="muted">auto-refreshes every 30s &middot; reconciliation: {recon_html}</div></header>
<div class="wrap">
  <div class="grid" style="margin-bottom:24px;">
    <div class="card">
      <h2 style="margin-top:0;font-size:15px;">Data health</h2>
      <table><thead><tr><th>table</th><th class="num">rows</th><th>latest</th></tr></thead>
      <tbody>{rows_html}</tbody></table>
    </div>
    <div class="card">{cov_html}</div>
  </div>
  <div class="grid">
    <div class="card doc">{render_doc("STATE.md")}</div>
    <div class="card doc">{render_doc("JOURNAL.md")}</div>
  </div>
</div>
</body></html>"""
