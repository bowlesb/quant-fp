"""Build per-(symbol,date) daily RTH bars for the W11 CERTIFY run on the 378d (18mo) history.

Same construction as build_daily.py, but ONLY include symbols that have >=378 date partitions
so we never mix depths. Output: certify_daily.parquet.
"""
from __future__ import annotations

import glob
import os

import polars as pl

OUT = "/app/experiments/2026-06-16-w11-overnight-beta/certify_daily.parquet"
RTH_LO = 810  # 13:30 UTC = 09:30 ET
RTH_HI = 959  # 15:59 UTC bar = closes at 16:00 ET
MIN_PARTITIONS = 378  # require the FULL 18mo depth — never mix depths


def deep_symbol_dirs() -> list[str]:
    dirs = sorted(glob.glob("/store/raw/bars/symbol=*"))
    deep: list[str] = []
    for symbol_dir in dirs:
        n_part = len(glob.glob(os.path.join(symbol_dir, "date=*")))
        if n_part >= MIN_PARTITIONS:
            deep.append(symbol_dir)
    return deep


def build_one(symbol_dir: str) -> pl.DataFrame | None:
    files = glob.glob(os.path.join(symbol_dir, "date=*/*.parquet"))
    if not files:
        return None
    lf = pl.scan_parquet(files, hive_partitioning=True)
    lf = lf.with_columns(
        (pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32)).alias("utc_min")
    ).filter((pl.col("utc_min") >= RTH_LO) & (pl.col("utc_min") <= RTH_HI))
    agg = (
        lf.sort("ts")
        .group_by("symbol", "date")
        .agg(
            pl.col("open").first().alias("rth_open"),
            pl.col("close").last().alias("rth_close"),
            (pl.col("close") * pl.col("volume")).sum().alias("dollar_vol"),
            pl.len().alias("n_bars"),
        )
    )
    out = agg.collect()
    out = out.filter(pl.col("n_bars") >= 30)
    return out if out.height > 0 else None


def main() -> None:
    symbol_dirs = deep_symbol_dirs()
    print(f"deep symbols (>= {MIN_PARTITIONS} date partitions): {len(symbol_dirs)}")
    frames: list[pl.DataFrame] = []
    for i, sdir in enumerate(symbol_dirs):
        result = build_one(sdir)
        if result is not None:
            frames.append(result)
        if (i + 1) % 500 == 0:
            print(f"  {i + 1}/{len(symbol_dirs)} done, {len(frames)} non-empty")
    daily = pl.concat(frames)
    daily = daily.filter((pl.col("rth_open") > 0) & (pl.col("rth_close") > 0))
    daily.write_parquet(OUT)
    print(f"wrote {OUT}: {daily.height} rows, {daily['symbol'].n_unique()} symbols, {daily['date'].n_unique()} dates")
    print("date range:", daily["date"].min(), daily["date"].max())


if __name__ == "__main__":
    main()
