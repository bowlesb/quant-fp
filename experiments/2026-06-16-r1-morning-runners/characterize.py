"""R1 small-cap morning-runner event-set characterization (Stage 1, bars only).

Runner-day = prev RTH close in [$2,$20] AND early_move (max first-30-min high / prev_close - 1)
>= 0.30 AND first-30-min vol surge (>= 2x trailing-20d median first-30min vol). Characterizes
counts across threshold cells, intraday fade base-rate, multi-day continuation, capacity.

Parallelized with a process pool over symbols; reads only needed columns.
"""

import glob
import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import polars as pl

BARS = "/store/raw/bars"
RTH_LO, RTH_HI = 810, 1190  # 09:30-15:50 ET in UTC minutes
F30_HI = 840  # first 30 min ends 10:00 ET
EARLY_MIN = 0.30
SURGE_MIN = 2.0
OUT = "/app/experiments/2026-06-16-r1-morning-runners"


def scan_symbol(symbol: str) -> list[dict[str, object]]:
    files = sorted(glob.glob(f"{BARS}/symbol={symbol}/date=*/data.parquet"))
    if len(files) < 25:
        return []
    recs: list[tuple[str, float, float, float, float, float]] = []
    for path in files:
        date = os.path.basename(os.path.dirname(path)).split("=")[1]
        try:
            df = pl.read_parquet(path, columns=["ts", "open", "high", "close", "volume"])
        except (OSError, pl.exceptions.PolarsError):
            continue
        if df.height == 0:
            continue
        df = df.with_columns(
            (pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32)).alias("m")
        )
        rth = df.filter((pl.col("m") >= RTH_LO) & (pl.col("m") < RTH_HI))
        if rth.height == 0:
            continue
        f30 = rth.filter(pl.col("m") < F30_HI)
        if f30.height == 0:
            continue
        rth_s = rth.sort("m")
        recs.append(
            (
                date,
                float(f30["high"].max()),
                float(f30["volume"].sum()),
                float(rth_s["close"][-1]),
                float(rth_s["open"][0]),
                float((rth["close"] * rth["volume"]).sum()),
            )
        )
    if len(recs) < 25:
        return []
    rdf = pl.DataFrame(
        recs,
        schema=["date", "f30_high", "f30_vol", "rth_close", "rth_open", "rth_dollar"],
        orient="row",
    ).sort("date")
    rdf = rdf.with_columns(
        [
            pl.col("rth_close").shift(1).alias("prev_close"),
            pl.col("f30_vol").rolling_median(window_size=20, min_samples=10).shift(1).alias("med_f30_vol"),
            pl.col("rth_close").shift(-1).alias("c1"),
            pl.col("rth_close").shift(-3).alias("c3"),
            pl.col("rth_close").shift(-5).alias("c5"),
        ]
    )
    rdf = rdf.with_columns(
        [
            (pl.col("f30_high") / pl.col("prev_close") - 1.0).alias("early_move"),
            (pl.col("rth_open") / pl.col("prev_close") - 1.0).alias("open_gap"),
            (pl.col("f30_vol") / pl.col("med_f30_vol")).alias("vol_surge"),
        ]
    ).filter(
        (pl.col("prev_close") >= 2.0)
        & (pl.col("prev_close") <= 20.0)
        & pl.col("early_move").is_finite()
        & pl.col("vol_surge").is_finite()
    )
    out_rows: list[dict[str, object]] = []
    for row in rdf.iter_rows(named=True):
        if row["early_move"] >= EARLY_MIN and row["vol_surge"] >= SURGE_MIN:
            out_rows.append(
                {
                    "symbol": symbol,
                    **{
                        k: row[k]
                        for k in (
                            "date",
                            "prev_close",
                            "early_move",
                            "open_gap",
                            "vol_surge",
                            "f30_high",
                            "rth_close",
                            "rth_dollar",
                            "c1",
                            "c3",
                            "c5",
                        )
                    },
                }
            )
    return out_rows


def main() -> None:
    syms = [d.split("=")[1] for d in os.listdir(BARS) if d.startswith("symbol=")]
    print(f"scanning {len(syms)} symbols (parallel) ...", flush=True)
    rows: list[dict[str, object]] = []
    done = 0
    with ProcessPoolExecutor(max_workers=8) as ex:
        for result in ex.map(scan_symbol, syms, chunksize=20):
            rows.extend(result)
            done += 1
            if done % 1000 == 0:
                print(f"  {done}/{len(syms)} (events so far: {len(rows)})", flush=True)

    ev = pl.DataFrame(rows)
    print(f"\n=== RUNNER EVENTS (early>=0.30, surge>=2): {ev.height} rows, {ev['symbol'].n_unique()} symbols ===")
    ev.write_parquet(f"{OUT}/runner_events.parquet")

    print("\nCOUNTS (runner-days / unique syms):")
    for early in (0.30, 0.50, 1.00, 2.00):
        for surge in (2, 3, 5):
            cell = ev.filter((pl.col("early_move") >= early) & (pl.col("vol_surge") >= surge))
            print(f"  early>={early:.2f} surge>={surge}: {cell.height:5d} days / {cell['symbol'].n_unique():4d} syms")

    core = ev.filter((pl.col("early_move") >= 0.50) & (pl.col("vol_surge") >= 3))
    print(f"\n=== CORE (early>=0.50, surge>=3): {core.height} days / {core['symbol'].n_unique()} syms ===")
    if core.height:
        print("early_move pctiles 10/50/90:", [round(float(core["early_move"].quantile(q)), 2) for q in (0.1, 0.5, 0.9)])
        print("prev_close pctiles 10/50/90:", [round(float(core["prev_close"].quantile(q)), 2) for q in (0.1, 0.5, 0.9)])
        print("runner-day $vol 10/50/90:", [f"${float(core['rth_dollar'].quantile(q)):,.0f}" for q in (0.1, 0.5, 0.9)])
        close_vs_high = core["rth_close"] / core["f30_high"] - 1.0
        print(
            f"INTRADAY close vs f30-high: median {float(close_vs_high.median()) * 100:.1f}% | "
            f"frac closing >= f30 high: {float((close_vs_high >= 0).mean()) * 100:.0f}% | "
            f"frac fading >10% off high: {float((close_vs_high < -0.10).mean()) * 100:.0f}%"
        )
        for label, col in (("1d", "c1"), ("3d", "c3"), ("5d", "c5")):
            fwd = core[col] / core["rth_close"] - 1.0
            fwd = fwd.filter(fwd.is_finite())
            print(
                f"MULTI-DAY fwd {label}: median {float(fwd.median()) * 100:+.1f}% | "
                f"frac up {float((fwd > 0).mean()) * 100:.0f}% (n={fwd.len()})"
            )
        print("\nSAMPLE runners (top early_move):")
        for row in core.sort("early_move", descending=True).head(15).iter_rows(named=True):
            print(
                f"  {row['symbol']:6} {row['date']} prev=${row['prev_close']:.2f} "
                f"early=+{row['early_move'] * 100:.0f}% surge={row['vol_surge']:.0f}x $vol=${row['rth_dollar']:,.0f}"
            )


if __name__ == "__main__":
    np.seterr(all="ignore")
    main()
