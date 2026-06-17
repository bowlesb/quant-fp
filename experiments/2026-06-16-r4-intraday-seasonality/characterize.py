"""R4 intraday-seasonality characterization (liquid tier, bars). Pre-reg in hypothesis.md.

Quantifies (1) the strength of the intraday volume/|return| seasonality (open/close vs midday) and
(2) its STABILITY across the sample (first half vs second half rank-corr of the tod profile). If
strong + stable, a time-of-day-normalized feature is warranted. Parallel over symbols; aggregates
per (minute-of-day bucket) pooled across the liquid tier.
"""

import glob
import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import polars as pl
from scipy.stats import spearmanr

BARS = "/store/raw/bars"
OUT = "/app/experiments/2026-06-16-r4-intraday-seasonality"
OPEN_M, CLOSE_M = 570, 960  # 09:30, 16:00 ET
BUCKET = 30  # 30-min tod buckets
LIQUID_TOP = 1500


def scan_symbol(symbol: str) -> dict[str, object] | None:
    files = sorted(glob.glob(f"{BARS}/symbol={symbol}/date=*/data.parquet"))
    if len(files) < 40:
        return None
    frames = []
    dollar_total = 0.0
    n_days = 0
    for path in files:
        date = os.path.basename(os.path.dirname(path)).split("=")[1]
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
            pl.lit(date).alias("date"),
        )
        frames.append(df.select(["date", "bucket", "volume", "absret"]))
        dollar_total += float((df["close"] * df["volume"]).sum())
        n_days += 1
    if n_days < 40:
        return None
    allbars = pl.concat(frames)
    # per (date, bucket) sums/means → then the tod profile is the median across days.
    per = allbars.group_by(["date", "bucket"]).agg(
        pl.col("volume").sum().alias("vol"), pl.col("absret").mean().alias("absret")
    )
    return {
        "symbol": symbol,
        "adv_dollar": dollar_total / n_days,
        "per_date_bucket": per.to_dicts(),
    }


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    syms = [d.split("=")[1] for d in os.listdir(BARS) if d.startswith("symbol=")]
    print(f"scanning {len(syms)} symbols (parallel)...", flush=True)
    results = []
    done = 0
    with ProcessPoolExecutor(max_workers=8) as ex:
        for res in ex.map(scan_symbol, syms, chunksize=20):
            if res is not None:
                results.append(res)
            done += 1
            if done % 1500 == 0:
                print(f"  {done}/{len(syms)} (kept {len(results)})", flush=True)

    results.sort(key=lambda r: -float(r["adv_dollar"]))
    liquid = results[:LIQUID_TOP]
    print(f"\n=== LIQUID tier: {len(liquid)} symbols ===")

    rows = []
    for res in liquid:
        for record in res["per_date_bucket"]:
            rows.append({"date": record["date"], "bucket": int(record["bucket"]), "vol": float(record["vol"]), "absret": float(record["absret"])})
    panel = pl.DataFrame(rows)

    # (1) seasonality strength: median per bucket across the whole sample.
    prof = panel.group_by("bucket").agg(
        pl.col("vol").median().alias("med_vol"), pl.col("absret").median().alias("med_absret")
    ).sort("bucket")
    print("\nTOD PROFILE (median per 30-min bucket, pooled liquid):")
    print("  bucket(ET-min)  med_volume      med_|ret|")
    for row in prof.iter_rows(named=True):
        et_h, et_m = divmod(row["bucket"], 60)
        print(f"  {et_h:02d}:{et_m:02d} ({row['bucket']})   {row['med_vol']:>12,.0f}   {row['med_absret'] * 100:.3f}%")

    vols = prof["med_vol"].to_list()
    absrets = prof["med_absret"].to_list()
    midday_idx = len(vols) // 2
    print(f"\nSEASONALITY STRENGTH:")
    print(f"  volume open/midday ratio: {vols[0] / vols[midday_idx]:.2f}x | close/midday: {vols[-1] / vols[midday_idx]:.2f}x")
    print(f"  |ret|  open/midday ratio: {absrets[0] / absrets[midday_idx]:.2f}x | close/midday: {absrets[-1] / absrets[midday_idx]:.2f}x")

    # (2) stability: split sample by date median, compare bucket profiles' rank order.
    dates = sorted(panel["date"].unique().to_list())
    mid_date = dates[len(dates) // 2]
    h1 = panel.filter(pl.col("date") < mid_date).group_by("bucket").agg(pl.col("vol").median().alias("v")).sort("bucket")
    h2 = panel.filter(pl.col("date") >= mid_date).group_by("bucket").agg(pl.col("vol").median().alias("v")).sort("bucket")
    common = h1.join(h2, on="bucket", suffix="_2")
    rho_vol = spearmanr(common["v"].to_numpy(), common["v_2"].to_numpy()).correlation
    h1r = panel.filter(pl.col("date") < mid_date).group_by("bucket").agg(pl.col("absret").median().alias("r")).sort("bucket")
    h2r = panel.filter(pl.col("date") >= mid_date).group_by("bucket").agg(pl.col("absret").median().alias("r")).sort("bucket")
    commonr = h1r.join(h2r, on="bucket", suffix="_2")
    rho_ret = spearmanr(commonr["r"].to_numpy(), commonr["r_2"].to_numpy()).correlation
    print(f"\nSTABILITY (first-half vs second-half tod-profile rank-corr):")
    print(f"  volume profile Spearman rho: {rho_vol:.3f} | |ret| profile rho: {rho_ret:.3f}")
    print(f"\nVERDICT: seasonality {'STRONG' if vols[0] / vols[midday_idx] > 2 else 'weak'} + "
          f"{'STABLE' if min(rho_vol, rho_ret) > 0.8 else 'unstable'} -> "
          f"{'BUILD tod-normalized feature' if vols[0] / vols[midday_idx] > 2 and min(rho_vol, rho_ret) > 0.8 else 'NO feature'}")


if __name__ == "__main__":
    np.seterr(all="ignore")
    main()
