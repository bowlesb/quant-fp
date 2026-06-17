"""R3 gap-fill vs gap-extend characterization (liquid universe, bars). Pre-reg in hypothesis.md.

For gapped days (|gap_open| >= 2%), measure the EOD gap-fill fraction = (close-open)/(prev_close-open):
1.0 = fully filled back to prev_close, 0 = no fill, <0 = extended past the open away from prev_close.
Split by gap direction + size bucket + liquidity tier. Parallel over symbols.
"""

import glob
import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import polars as pl

BARS = "/store/raw/bars"
OUT = "/app/experiments/2026-06-16-r3-gap-fill"
GAP_MIN = 0.02


def scan_symbol(symbol: str) -> list[dict[str, object]]:
    files = sorted(glob.glob(f"{BARS}/symbol={symbol}/date=*/data.parquet"))
    if len(files) < 25:
        return []
    recs: list[tuple[str, float, float, float]] = []
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
        df = df.with_columns(etm.alias("m"))
        rth = df.filter((pl.col("m") >= 570) & (pl.col("m") < 960)).sort("m")
        if rth.height < 30:
            continue
        recs.append(
            (
                date,
                float(rth["open"][0]),
                float(rth["close"][-1]),
                float((rth["close"] * rth["volume"]).sum()),
            )
        )
    if len(recs) < 25:
        return []
    rdf = pl.DataFrame(recs, schema=["date", "rth_open", "rth_close", "rth_dollar"], orient="row").sort("date")
    rdf = rdf.with_columns(pl.col("rth_close").shift(1).alias("prev_close"))
    rdf = rdf.with_columns(
        (pl.col("rth_open") / pl.col("prev_close") - 1.0).alias("gap"),
        # adv$ proxy = trailing-20d median rth_dollar (the liquidity tier key), lagged (no look-ahead).
        pl.col("rth_dollar").rolling_median(window_size=20, min_samples=10).shift(1).alias("adv_dollar"),
    ).filter(pl.col("gap").is_finite() & (pl.col("gap").abs() >= GAP_MIN) & pl.col("adv_dollar").is_finite())
    out_rows: list[dict[str, object]] = []
    for row in rdf.iter_rows(named=True):
        denom = row["prev_close"] - row["rth_open"]
        if abs(denom) < 1e-9:
            continue
        fill = (row["rth_close"] - row["rth_open"]) / denom  # 1=filled to prev_close, 0=no fill, <0=extend
        out_rows.append(
            {
                "symbol": symbol,
                "date": row["date"],
                "gap": row["gap"],
                "fill_fraction": float(np.clip(fill, -2.0, 2.0)),
                "adv_dollar": row["adv_dollar"],
            }
        )
    return out_rows


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    syms = [d.split("=")[1] for d in os.listdir(BARS) if d.startswith("symbol=")]
    print(f"scanning {len(syms)} symbols (parallel)...", flush=True)
    rows: list[dict[str, object]] = []
    done = 0
    with ProcessPoolExecutor(max_workers=8) as ex:
        for result in ex.map(scan_symbol, syms, chunksize=20):
            rows.extend(result)
            done += 1
            if done % 1500 == 0:
                print(f"  {done}/{len(syms)} (gapped-days {len(rows)})", flush=True)

    ev = pl.DataFrame(rows)
    print(f"\n=== GAPPED DAYS (|gap|>=2%): {ev.height} rows, {ev['symbol'].n_unique()} syms ===")
    ev.write_parquet(f"{OUT}/gap_events.parquet")

    # liquidity tiers by adv_dollar (per-row, pooled): top third = liquid.
    q33, q67 = float(ev["adv_dollar"].quantile(0.33)), float(ev["adv_dollar"].quantile(0.67))
    ev = ev.with_columns(
        pl.when(pl.col("adv_dollar") >= q67).then(pl.lit("liquid"))
        .when(pl.col("adv_dollar") >= q33).then(pl.lit("mid"))
        .otherwise(pl.lit("illiquid")).alias("tier")
    )

    print("\nFILL FRACTION by gap direction x tier (median | frac fully-filled >=1 | n):")
    for direction, cond in (("gap UP", pl.col("gap") > 0), ("gap DOWN", pl.col("gap") < 0)):
        for tier in ("liquid", "mid", "illiquid"):
            cell = ev.filter(cond & (pl.col("tier") == tier))
            if cell.height < 30:
                continue
            ff = cell["fill_fraction"]
            print(f"  {direction:8} {tier:8}: median {float(ff.median()):+.2f} | "
                  f"fully-filled {float((ff >= 1.0).mean()) * 100:.0f}% | extended<0 {float((ff < 0).mean()) * 100:.0f}% | n={cell.height}")

    print("\nFILL FRACTION by gap-SIZE bucket (LIQUID only):")
    liq = ev.filter(pl.col("tier") == "liquid")
    for lo, hi in ((0.02, 0.05), (0.05, 0.10), (0.10, 0.25), (0.25, 5.0)):
        cell = liq.filter((pl.col("gap").abs() >= lo) & (pl.col("gap").abs() < hi))
        if cell.height < 30:
            continue
        ff = cell["fill_fraction"]
        print(f"  |gap| {lo:.2f}-{hi:.2f}: median fill {float(ff.median()):+.2f} | n={cell.height}")


if __name__ == "__main__":
    np.seterr(all="ignore")
    main()
