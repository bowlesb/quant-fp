"""Build the per-(symbol, date) daily panel for W4 overnight/intraday decomposition.

RTH window (UTC-correct, RESEARCH_PITFALLS #1): a bar belongs to RTH if its UTC minute-of-day
(hour*60+minute) is >= 810 (09:30 ET = 13:30 UTC) and < 1190 (16:00 ET = 20:00 UTC -> last bar 15:59 ET).
open  = open  of the FIRST RTH bar (utc_min >= 810)
close = close of the LAST  RTH bar (utc_min  < 1190)

Per (symbol, date): rth_open, rth_close, dollar_vol (sum close*volume), n_rth_bars,
spread_bps = median over RTH bars of (high-low)/close*1e4 (conservative range-based round-trip proxy).

Memory-safe: process symbols in BATCHES (each batch is a separate lazy scan over only that batch's files),
so peak memory is bounded by batch size, not the whole 7,600-symbol tree.
"""

from __future__ import annotations

import os

import polars as pl

BARS_ROOT = "/store/raw/bars"
OUT = "/app/experiments/2026-06-16-w4-overnight-intraday/panel.parquet"

RTH_OPEN_MIN = 810
RTH_CLOSE_MIN = 1190
BATCH = 400


def list_symbols() -> list[str]:
    syms = []
    for name in os.listdir(BARS_ROOT):
        if name.startswith("symbol="):
            syms.append(name.split("=", 1)[1])
    return sorted(syms)


def process_batch(symbols: list[str]) -> pl.DataFrame:
    paths = [f"{BARS_ROOT}/symbol={sym}/**/*.parquet" for sym in symbols]
    lf = pl.scan_parquet(paths, hive_partitioning=True)
    lf = lf.with_columns(
        (pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32)).alias("utc_min")
    )
    rth = lf.filter((pl.col("utc_min") >= RTH_OPEN_MIN) & (pl.col("utc_min") < RTH_CLOSE_MIN))
    agg = (
        rth.sort("utc_min")
        .group_by(["symbol", "date"])
        .agg(
            pl.col("open").first().alias("rth_open"),
            pl.col("close").last().alias("rth_close"),
            (pl.col("close") * pl.col("volume")).sum().alias("dollar_vol"),
            pl.len().alias("n_rth_bars"),
            (((pl.col("high") - pl.col("low")) / pl.col("close")) * 1e4).median().alias("spread_bps"),
        )
    )
    return agg.collect()


def main() -> None:
    symbols = list_symbols()
    print("total symbols", len(symbols))
    frames = []
    for i in range(0, len(symbols), BATCH):
        batch = symbols[i : i + BATCH]
        frames.append(process_batch(batch))
        print(f"batch {i // BATCH} done ({i + len(batch)}/{len(symbols)})", flush=True)
    panel = pl.concat(frames).sort(["symbol", "date"])
    print("panel rows", panel.shape[0])
    print("symbols", panel["symbol"].n_unique(), "dates", panel["date"].n_unique())
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    panel.write_parquet(OUT)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
