"""Measure per-name round-trip spread (bps) for the W1 liquid universe.

For the top-500 liquid symbols (by median daily dollar-volume from the close panel),
sample available quote dates and compute the median RTH relative spread
((ask-bid)/mid) over quotes in the RTH window, then express round-trip (cross-the-spread
both sides => the full quoted spread is the round-trip cost). Writes spreads.csv.
"""
from __future__ import annotations

import glob
import os

import numpy as np
import polars as pl

EXP_DIR = "/app/experiments/2026-06-16-w1-factor-momentum"
PANEL = os.path.join(EXP_DIR, "close_panel.parquet")
QUOTES = "/store/raw/quotes/symbol={sym}/date=*/data.parquet"
RTH_LO = 810
RTH_HI = 1190
N_KEEP = 500
MAX_QUOTE_DATES = 5  # sample up to N quote dates per symbol


def liquid_symbols(n_keep: int) -> list[str]:
    panel = pl.read_parquet(PANEL)
    n_dates = panel["date"].n_unique()
    agg = (
        panel.group_by("symbol")
        .agg(
            pl.col("dollar_vol").median().alias("med_dvol"),
            pl.col("close").count().alias("n_obs"),
        )
        .filter(pl.col("n_obs") >= 0.95 * n_dates)
        .sort("med_dvol", descending=True)
        .head(n_keep)
    )
    return agg["symbol"].to_list()


def measure_symbol(sym: str) -> float | None:
    files = sorted(glob.glob(QUOTES.format(sym=sym)))
    if not files:
        return None
    files = files[:: max(1, len(files) // MAX_QUOTE_DATES)][:MAX_QUOTE_DATES]
    spreads: list[float] = []
    for path in files:
        lf = pl.scan_parquet(path)
        minute = pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32)
        q = (
            lf.with_columns(minute.alias("utc_min"))
            .filter((pl.col("utc_min") >= RTH_LO) & (pl.col("utc_min") < RTH_HI))
            .filter((pl.col("bid_price") > 0) & (pl.col("ask_price") > pl.col("bid_price")))
            .with_columns(
                ((pl.col("ask_price") - pl.col("bid_price"))
                 / ((pl.col("ask_price") + pl.col("bid_price")) / 2.0) * 1e4).alias("rel_bps")
            )
            .select("rel_bps")
            .collect()
        )
        if q.height > 0:
            spreads.append(float(q["rel_bps"].median()))
    if not spreads:
        return None
    return float(np.median(spreads))


def main() -> None:
    syms = liquid_symbols(N_KEEP)
    print(f"liquid-{N_KEEP} symbols: {len(syms)}", flush=True)
    rows: list[dict] = []
    for i, sym in enumerate(syms):
        sp = measure_symbol(sym)
        if sp is not None:
            rows.append({"symbol": sym, "rt_spread_bps": round(sp, 3)})
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(syms)} measured={len(rows)}", flush=True)
    out = pl.DataFrame(rows)
    out.write_csv(os.path.join(EXP_DIR, "spreads.csv"))
    measured = out["rt_spread_bps"].to_numpy()
    print(f"measured {len(rows)}/{len(syms)} symbols")
    print(f"spread bps: median={np.median(measured):.2f} "
          f"p25={np.percentile(measured,25):.2f} p75={np.percentile(measured,75):.2f} "
          f"min={measured.min():.2f} max={measured.max():.2f}")


if __name__ == "__main__":
    main()
