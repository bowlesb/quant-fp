"""Build the FROZEN intraday-seasonality baseline table for the intraday_seasonality feature.

Per 30-min ET minute-of-day bucket (09:30-16:00), over the trailing settled liquid-tier history:
  - baseline_absret = market-median per-minute |close/open-1| in that bucket (scale-free across names).
  - vol_shape       = (median bucket per-minute volume) / (median ALL-bucket per-minute volume) — a
    unitless time-of-day multiplier (1.0 = a typical minute; 5.2 at the close).
Committed as a static parquet the feature loads; identical in stream and backfill -> parity-true.
"""

import glob
import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import polars as pl

BARS = "/store/raw/bars"
OUT = "/app/quantlib/features/data/intraday_seasonality_v1.parquet"
OPEN_M, CLOSE_M = 570, 960
BUCKET = 30
LIQUID_TOP = 1500


def scan(symbol: str) -> dict[str, object] | None:
    files = sorted(glob.glob(f"{BARS}/symbol={symbol}/date=*/data.parquet"))
    if len(files) < 40:
        return None
    frames = []
    dollar = 0.0
    n = 0
    for path in files:
        try:
            df = pl.read_parquet(path, columns=["ts", "open", "close", "volume"])
        except (OSError, pl.exceptions.PolarsError):
            continue
        if df.height == 0:
            continue
        et = df["ts"].dt.convert_time_zone("America/New_York")
        etm = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
        df = df.with_columns(etm.alias("m")).filter((pl.col("m") >= OPEN_M) & (pl.col("m") < CLOSE_M))
        if df.height < 30:
            continue
        df = df.with_columns(
            (((pl.col("m") - OPEN_M) // BUCKET) * BUCKET + OPEN_M).alias("bucket"),
            (pl.col("close") / pl.col("open") - 1.0).abs().alias("absret"),
        )
        frames.append(df.select(["bucket", "volume", "absret"]))
        dollar += float((df["close"] * df["volume"]).sum())
        n += 1
    if n < 40:
        return None
    # Aggregate IN-WORKER to per-bucket means (1 row per bucket per symbol) — bounded memory.
    per_bucket = (
        pl.concat(frames)
        .group_by("bucket")
        .agg(pl.col("volume").mean().alias("vmean"), pl.col("absret").mean().alias("amean"))
    )
    return {"symbol": symbol, "adv": dollar / n, "per_bucket": per_bucket.to_dicts()}


def main() -> None:
    syms = [d.split("=")[1] for d in os.listdir(BARS) if d.startswith("symbol=")]
    print(f"scanning {len(syms)} ...", flush=True)
    results = []
    done = 0
    with ProcessPoolExecutor(max_workers=8) as ex:
        for res in ex.map(scan, syms, chunksize=20):
            if res is not None:
                results.append(res)
            done += 1
            if done % 2000 == 0:
                print(f"  {done}/{len(syms)}", flush=True)
    results.sort(key=lambda r: -float(r["adv"]))
    rows = []
    for res in results[:LIQUID_TOP]:
        rows.extend(res["per_bucket"])
    panel = pl.DataFrame(rows)  # one (vmean, amean) per (symbol, bucket): ~1500 x 13 rows, tiny
    prof = panel.group_by("bucket").agg(
        pl.col("amean").median().alias("baseline_absret"),
        pl.col("vmean").median().alias("_med_vol"),
    ).sort("bucket")
    overall_med_vol = float(panel["vmean"].median())
    prof = prof.with_columns((pl.col("_med_vol") / overall_med_vol).alias("vol_shape")).select(
        ["bucket", "baseline_absret", "vol_shape"]
    )
    prof.write_parquet(OUT)
    print(f"wrote {OUT}: {prof.height} buckets")
    print(prof)


if __name__ == "__main__":
    np.seterr(all="ignore")
    main()
