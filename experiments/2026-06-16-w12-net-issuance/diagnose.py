"""Diagnostics: per-rebalance issuance distribution, tag mix, and the extreme short-leg names."""
from __future__ import annotations

import polars as pl

from run_w12 import (
    HOLD_DAYS,
    QUINTILE,
    build_panel_pivot,
    compute_issuance,
    forward_return,
    load_trading_days,
)

BASE = "experiments/2026-06-16-w12-net-issuance"
shares = pl.read_parquet(f"{BASE}/data/shares_long.parquet")
panel = pl.read_parquet(f"{BASE}/data/daily_panel.parquet")
splits_df = pl.read_parquet(f"{BASE}/data/splits.parquet")
splits_map: dict = {}
for r in splits_df.iter_rows(named=True):
    splits_map.setdefault(r["symbol"], []).append((r["ex_date"], float(r["split_ratio"])))
days = load_trading_days()
pivot = build_panel_pivot(panel)

for ri in range(0, len(days) - HOLD_DAYS, HOLD_DAYS):
    t0, t1 = days[ri], days[ri + HOLD_DAYS]
    iss = compute_issuance(shares, splits_map, t0)
    fwd = [forward_return(pivot, r["symbol"], t0, t1) for r in iss.iter_rows(named=True)]
    iss = iss.with_columns(pl.Series("fwd_ret", fwd)).filter(pl.col("fwd_ret").is_finite()).sort("issuance")
    n = iss.height
    k = max(1, int(round(n * QUINTILE)))
    q = iss["issuance"]
    print(f"\n=== {t0} -> {t1}  n={n} ===")
    print(f"  issuance: min={q.min():+.3f} p10={q.quantile(0.1):+.3f} med={q.median():+.3f} p90={q.quantile(0.9):+.3f} max={q.max():+.3f}")
    print(f"  tag mix: {iss.group_by('tag').len().sort('len', descending=True).to_dicts()}")
    short = iss.tail(k).sort("fwd_ret", descending=True)
    print(f"  short-leg (high-issuance) top fwd_ret: {[(r['symbol'], round(r['issuance'],2), round(r['fwd_ret'],2)) for r in short.head(5).iter_rows(named=True)]}")
    print(f"  long-leg mean iss={iss.head(k)['issuance'].mean():+.3f}  short-leg mean iss={iss.tail(k)['issuance'].mean():+.3f}")
