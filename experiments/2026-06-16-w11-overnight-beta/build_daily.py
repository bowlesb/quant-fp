"""Build per-(symbol,date) daily RTH bars for the W11 overnight-beta test.

Output: daily.parquet with columns symbol, date, rth_open, rth_close, dollar_vol.
- RTH = UTC minute in [810, 959] (13:30-15:59 ET = the regular session minute bars).
- rth_open  = open  of the FIRST RTH bar of the day (the 09:30 ET print).
- rth_close = close of the LAST  RTH bar of the day (the 15:59 ET bar = 16:00 ET close).
- dollar_vol = sum over RTH bars of (close * volume) — for the liquidity sort.
NOTE on the tradeable-entry trap (MEMORY): the open here IS the 09:30 print. For a beta-quintile
overnight bet that is acceptable as a directional research realization, but we FLAG the MOO/MOC auction
caveat in cost (the bet is buy-at-close / sell-at-open = the MOO auction price).
"""
from __future__ import annotations

import glob
import os

import polars as pl

OUT = "/app/experiments/2026-06-16-w11-overnight-beta/daily.parquet"
RTH_LO = 810  # 13:30 UTC = 09:30 ET
RTH_HI = 959  # 15:59 UTC bar = closes at 16:00 ET


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
    # require a reasonable number of RTH minute bars so half-days / illiquid stubs don't poison the open
    out = out.filter(pl.col("n_bars") >= 30)
    return out if out.height > 0 else None


def main() -> None:
    symbol_dirs = sorted(glob.glob("/store/raw/bars/symbol=*"))
    print(f"scanning {len(symbol_dirs)} symbols")
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
