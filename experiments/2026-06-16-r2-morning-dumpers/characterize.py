"""Small-cap morning DUMPERS — the mirror of R1 runners. $2-20 names that DROP -30%+ in the
first 30 min on a volume surge: do they BOUNCE (symmetric reversal) or continue down?
Reuses the R1 machinery. Bars only. Parallel-free (serial; pool deadlocks under the cgroup)."""
import os, glob
import numpy as np
import polars as pl

BARS = "/store/raw/bars"
RTH_LO, RTH_HI = 810, 1190
F30_HI = 840
DROP_MIN = 0.30
SURGE_MIN = 2.0
OUT = "/app/experiments/2026-06-16-r2-morning-dumpers"


def scan_symbol(symbol):
    files = sorted(glob.glob(f"{BARS}/symbol={symbol}/date=*/data.parquet"))
    if len(files) < 25:
        return []
    recs = []
    for path in files:
        date = os.path.basename(os.path.dirname(path)).split("=")[1]
        try:
            df = pl.read_parquet(path, columns=["ts", "open", "low", "high", "close", "volume"])
        except Exception:
            continue
        if df.height == 0:
            continue
        et = df["ts"].dt.convert_time_zone("America/New_York")
        etm = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
        df = df.with_columns(etm.alias("m"))
        rth = df.filter((pl.col("m") >= 570) & (pl.col("m") < 950))
        if rth.height == 0:
            continue
        f30 = rth.filter(pl.col("m") < 600)
        if f30.height == 0:
            continue
        rth_s = rth.sort("m")
        recs.append((date, float(f30["low"].min()), float(f30["volume"].sum()),
                     float(rth_s["close"][-1]), float(rth_s["open"][0]),
                     float((rth["close"] * rth["volume"]).sum())))
    if len(recs) < 25:
        return []
    rdf = pl.DataFrame(recs, schema=["date","f30_low","f30_vol","rth_close","rth_open","rth_dollar"], orient="row").sort("date")
    rdf = rdf.with_columns([
        pl.col("rth_close").shift(1).alias("prev_close"),
        pl.col("f30_vol").rolling_median(window_size=20, min_samples=10).shift(1).alias("med_f30_vol"),
        pl.col("rth_close").shift(-1).alias("c1"),
        pl.col("rth_close").shift(-5).alias("c5"),
    ])
    rdf = rdf.with_columns([
        (1.0 - pl.col("f30_low") / pl.col("prev_close")).alias("early_drop"),
        (pl.col("f30_vol") / pl.col("med_f30_vol")).alias("vol_surge"),
    ]).filter((pl.col("prev_close") >= 2.0) & (pl.col("prev_close") <= 20.0)
              & pl.col("early_drop").is_finite() & pl.col("vol_surge").is_finite())
    out = []
    for row in rdf.iter_rows(named=True):
        if row["early_drop"] >= DROP_MIN and row["vol_surge"] >= SURGE_MIN:
            out.append({"symbol": symbol, **{k: row[k] for k in
                ("date","prev_close","early_drop","vol_surge","f30_low","rth_close","rth_dollar","c1","c5")}})
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    syms = [d.split("=")[1] for d in os.listdir(BARS) if d.startswith("symbol=")]
    print(f"scanning {len(syms)} symbols (serial)...", flush=True)
    rows = []
    for i, s in enumerate(syms):
        rows.extend(scan_symbol(s))
        if (i+1) % 1500 == 0:
            print(f"  {i+1}/{len(syms)} (events {len(rows)})", flush=True)
    ev = pl.DataFrame(rows)
    print(f"\n=== DUMPER EVENTS (drop>=0.30, surge>=2): {ev.height} rows, {ev['symbol'].n_unique()} syms ===")
    ev.write_parquet(f"{OUT}/dumper_events.parquet")
    core = ev.filter((pl.col("early_drop") >= 0.50) & (pl.col("vol_surge") >= 3))
    print(f"=== CORE (drop>=0.50, surge>=3): {core.height} days / {core['symbol'].n_unique()} syms ===")
    if core.height:
        # Does it BOUNCE off the f30 low by EOD? (close vs f30 low)
        bounce = core["rth_close"] / core["f30_low"] - 1.0
        print(f"INTRADAY close vs f30-LOW: median {float(bounce.median())*100:+.1f}% | frac bouncing >10% off low: {float((bounce>0.10).mean())*100:.0f}% | frac closing BELOW f30 low: {float((bounce<0).mean())*100:.0f}%")
        for label, col in (("1d","c1"),("5d","c5")):
            fwd = (core[col]/core["rth_close"]-1.0)
            fwd = fwd.filter(fwd.is_finite())
            print(f"MULTI-DAY fwd {label}: median {float(fwd.median())*100:+.1f}% | frac up {float((fwd>0).mean())*100:.0f}% (n={fwd.len()})")
        print("runner-day $vol 10/50/90:", [f"${float(core['rth_dollar'].quantile(q)):,.0f}" for q in (0.1,0.5,0.9)])


if __name__ == "__main__":
    np.seterr(all="ignore")
    main()
