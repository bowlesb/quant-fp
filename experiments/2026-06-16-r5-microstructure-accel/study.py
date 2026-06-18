"""R5 microstructure-acceleration study. Pre-reg in hypothesis.md.

Per symbol-minute: trade_accel = (n_trades last 5m)/(n_trades prior 5m) - 1, from raw trades aggregated
to per-minute. Forward returns fwd_5m/30m/1d booked from the NEXT minute's close (tradeable entry).
Rank-IC + decile spread + shuffle canary, LIQUID vs SPECULATIVE tiers SEPARATELY.
"""

import glob
import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import polars as pl
from scipy.stats import spearmanr

TRADES = "/store/raw/trades"
BARS = "/store/raw/bars"
OUT = "/app/experiments/2026-06-16-r5-microstructure-accel"
RTH_LO, RTH_HI = 570, 960  # ET minutes


def per_minute(symbol: str) -> pl.DataFrame | None:
    """Per-(date, minute) n_trades + close (last trade price) from raw trades, RTH only."""
    files = sorted(glob.glob(f"{TRADES}/symbol={symbol}/date=*/data.parquet"))
    if len(files) < 30:
        return None
    frames = []
    for path in files:
        date = os.path.basename(os.path.dirname(path)).split("=")[1]
        try:
            df = pl.read_parquet(path, columns=["ts", "price"])
        except (OSError, pl.exceptions.PolarsError):
            continue
        if df.height == 0:
            continue
        et = df["ts"].dt.convert_time_zone("America/New_York")
        df = df.with_columns(
            (et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)).alias("m"),
            et.dt.truncate("1m").alias("minute"),
        ).filter((pl.col("m") >= RTH_LO) & (pl.col("m") < RTH_HI))
        if df.height == 0:
            continue
        agg = df.group_by("minute").agg(pl.len().alias("n_trades"), pl.col("price").last().alias("close")).sort("minute")
        frames.append(agg.with_columns(pl.lit(date).alias("date")))
    if not frames:
        return None
    return pl.concat(frames)


def study_symbol(symbol: str) -> dict[str, object] | None:
    pm = per_minute(symbol)
    if pm is None or pm.height < 200:
        return None
    rows = []
    for date, day in pm.group_by("date"):
        day = day.sort("minute")
        if day.height < 40:
            continue
        nt = day["n_trades"].to_numpy().astype(float)
        close = day["close"].to_numpy().astype(float)
        n = len(nt)
        # rolling 5m sums
        cs = np.concatenate([[0.0], np.cumsum(nt)])
        last5 = np.array([cs[i + 1] - cs[max(0, i - 4)] for i in range(n)])
        prior5 = np.array([cs[max(0, i - 4)] - cs[max(0, i - 9)] for i in range(n)])
        accel = np.where(prior5 > 0, last5 / prior5 - 1.0, np.nan)
        for i in range(10, n - 1):  # need warmup + a next-minute entry
            if not np.isfinite(accel[i]) or close[i + 1] <= 0:
                continue
            entry = close[i + 1]  # tradeable: next minute's close
            f5 = close[min(i + 6, n - 1)] / entry - 1.0
            f30 = close[min(i + 31, n - 1)] / entry - 1.0
            rows.append((symbol, str(date[0]), float(accel[i]), float(f5), float(f30), float(close[-1] / entry - 1.0)))
    if len(rows) < 50:
        return None
    # Downsample to bound memory: cap each symbol to 400 random obs (preserves IC power; total
    # ~1500x400 = 600k tuples). Store compact tuples (no symbol/date strings).
    rng = np.random.default_rng(abs(hash(symbol)) % (2**32))
    if len(rows) > 400:
        idx = rng.choice(len(rows), 400, replace=False)
        rows = [rows[j] for j in idx]
    compact = [(r[2], r[3], r[4], r[5]) for r in rows]  # accel, f5, f30, f1d
    return {"symbol": symbol, "rows": compact, "n_trades_total": float(pm["n_trades"].sum())}


def _ic(arr: np.ndarray, fwd: np.ndarray) -> tuple[float, int]:
    mask = np.isfinite(arr) & np.isfinite(fwd)
    if int(mask.sum()) < 100:
        return float("nan"), int(mask.sum())
    return float(spearmanr(arr[mask], fwd[mask]).correlation), int(mask.sum())


def report_tier(name: str, rows: list[tuple]) -> None:
    if len(rows) < 200:
        print(f"\n{name}: too few rows ({len(rows)})")
        return
    accel = np.array([r[0] for r in rows])
    f5 = np.array([r[1] for r in rows])
    f30 = np.array([r[2] for r in rows])
    f1d = np.array([r[3] for r in rows])
    rng = np.random.default_rng(0)
    shuf = accel.copy()
    rng.shuffle(shuf)
    print(f"\n=== {name} ({len(rows):,} obs) ===")
    for label, fwd in (("fwd_5m", f5), ("fwd_30m", f30), ("fwd_1d", f1d)):
        ic, n = _ic(accel, fwd)
        ic_c, _ = _ic(shuf, fwd)
        # decile spread
        valid = np.isfinite(accel) & np.isfinite(fwd)
        a, y = accel[valid], fwd[valid]
        order = np.argsort(a)
        d = len(order) // 10
        spread = float(np.mean(y[order[-d:]]) - np.mean(y[order[:d]])) if d > 0 else float("nan")
        print(f"  {label}: rank-IC {ic:+.4f} (canary {ic_c:+.4f}) | top-bottom-decile spread {spread * 100:+.3f}% | n={n:,}")


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    syms = sorted(d.split("=")[1] for d in os.listdir(TRADES) if d.startswith("symbol="))
    # Sample for tractability: every 2nd symbol (still ~3800, ample for both tiers; tick aggregation
    # is expensive so we don't need all 7671 for a powered IC). Deterministic.
    syms = syms[::2]
    print(f"studying {len(syms)} symbols (parallel)...", flush=True)
    results = []
    done = 0
    with ProcessPoolExecutor(max_workers=8) as ex:
        for res in ex.map(study_symbol, syms, chunksize=10):
            if res is not None:
                results.append(res)
            done += 1
            if done % 1000 == 0:
                print(f"  {done}/{len(syms)} (kept {len(results)})", flush=True)

    results.sort(key=lambda r: -float(r["n_trades_total"]))
    liquid = results[:1500]
    liquid_rows = [row for res in liquid for row in res["rows"]]
    # speculative = lowest-traded third (proxy for the small/speculative cohort)
    spec = results[len(results) * 2 // 3 :]
    spec_rows = [row for res in spec for row in res["rows"]]
    print(f"\nkept {len(results)} symbols | liquid rows {len(liquid_rows):,} | spec rows {len(spec_rows):,}")
    report_tier("LIQUID (top-1500 by trades)", liquid_rows)
    report_tier("SPECULATIVE (bottom third by trades)", spec_rows)


if __name__ == "__main__":
    np.seterr(all="ignore")
    main()
