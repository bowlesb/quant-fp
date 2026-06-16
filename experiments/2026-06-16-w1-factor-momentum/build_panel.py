"""Build the daily-close panel for W1 cross-sectional momentum.

Reads /store/raw/bars for ALL symbols, extracts the daily CLOSE = last RTH bar
(UTC minute in [810, 1190)) per (symbol, date), and writes a wide-ish long panel
parquet to the experiment dir. Read-only on /store; writes ONLY to the experiment dir.
"""
from __future__ import annotations

import glob
import os

import polars as pl

EXP_DIR = "/app/experiments/2026-06-16-w1-factor-momentum"
BARS_GLOB = "/store/raw/bars/symbol=*"
RTH_LO = 810   # 13:30 UTC = 09:30 ET
RTH_HI = 1190  # 19:50 UTC (exclusive) per spec
EXCLUDE_DATES = {"2026-06-16"}  # exclude possibly-empty final day per spec


def daily_close_for_symbol(sym_dir: str) -> pl.DataFrame | None:
    symbol = sym_dir.split("symbol=")[-1]
    files = glob.glob(os.path.join(sym_dir, "date=*", "data.parquet"))
    if not files:
        return None
    lf = pl.scan_parquet(files)
    minute = pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32)
    lf = (
        lf.with_columns(
            minute.alias("utc_min"),
            pl.col("ts").dt.date().alias("date"),
        )
        .filter((pl.col("utc_min") >= RTH_LO) & (pl.col("utc_min") < RTH_HI))
    )
    # last RTH bar per date = max ts in window
    out = (
        lf.sort("ts")
        .group_by("date")
        .agg(
            pl.col("close").last().alias("close"),
            # dollar volume across the RTH window for liquidity ranking
            (pl.col("close") * pl.col("volume")).sum().alias("dollar_vol"),
        )
        .with_columns(pl.lit(symbol).alias("symbol"))
        .collect()
    )
    if out.height == 0:
        return None
    return out


def main() -> None:
    sym_dirs = sorted(glob.glob(BARS_GLOB))
    print(f"n symbol dirs: {len(sym_dirs)}")
    frames: list[pl.DataFrame] = []
    for i, sym_dir in enumerate(sym_dirs):
        df = daily_close_for_symbol(sym_dir)
        if df is not None:
            frames.append(df)
        if (i + 1) % 500 == 0:
            print(f"  processed {i+1}/{len(sym_dirs)}; frames={len(frames)}", flush=True)
    panel = pl.concat(frames, how="vertical")
    panel = panel.filter(~pl.col("date").cast(pl.Utf8).is_in(list(EXCLUDE_DATES)))
    panel = panel.filter(pl.col("close") > 0)
    panel = panel.sort(["symbol", "date"])
    out_path = os.path.join(EXP_DIR, "close_panel.parquet")
    panel.write_parquet(out_path)
    n_dates = panel["date"].n_unique()
    n_syms = panel["symbol"].n_unique()
    print(f"PANEL: rows={panel.height} symbols={n_syms} dates={n_dates}")
    print(f"date range: {panel['date'].min()} .. {panel['date'].max()}")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
