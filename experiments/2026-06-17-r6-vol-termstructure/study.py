"""R6 vol term-structure characterization. Pre-reg in hypothesis.md.

Per symbol-minute: short_vol = realized vol (std of 1m log-returns) over last SHORT m, long_vol over
last LONG m; vol_term = short_vol/long_vol. Characterize distribution, persistence (lag-k autocorr),
liquid vs speculative balance, and the forward-|return| relationship. Bounded memory: per-symbol
aggregates + a capped sample of (vol_term, fwd_absret) pairs for the IC.
"""

import glob
import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import polars as pl
from scipy.stats import spearmanr

BARS = "/store/raw/bars"
OUT = "/app/experiments/2026-06-17-r6-vol-termstructure"
SHORT, LONG = 10, 60
RTH_LO, RTH_HI = 570, 960
FWD = 30
SAMPLE_PER_SYM = 300


def study_symbol(symbol: str) -> dict[str, object] | None:
    files = sorted(glob.glob(f"{BARS}/symbol={symbol}/date=*/data.parquet"))
    if len(files) < 30:
        return None
    vt_all: list[float] = []
    pairs: list[tuple[float, float]] = []  # (vol_term, fwd_absret)
    autocorr_num = 0.0
    autocorr_den = 0.0
    dollar_total = 0.0
    n_days = 0
    for path in files:
        try:
            df = pl.read_parquet(path, columns=["ts", "close", "volume"])
        except (OSError, pl.exceptions.PolarsError):
            continue
        if df.height == 0:
            continue
        et = df["ts"].dt.convert_time_zone("America/New_York")
        etm = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
        df = df.with_columns(etm.alias("m")).filter((pl.col("m") >= RTH_LO) & (pl.col("m") < RTH_HI)).sort("m")
        if df.height < LONG + FWD + 5:
            continue
        close = df["close"].to_numpy().astype(float)
        vol = df["volume"].to_numpy().astype(float)
        dollar_total += float((close * vol).sum())
        n_days += 1
        logret = np.diff(np.log(close), prepend=np.log(close[0]))
        n = len(close)
        # rolling std of 1m log-returns over SHORT and LONG
        short_vol = np.full(n, np.nan)
        long_vol = np.full(n, np.nan)
        for i in range(LONG, n):
            short_vol[i] = np.std(logret[i - SHORT + 1 : i + 1])
            long_vol[i] = np.std(logret[i - LONG + 1 : i + 1])
        vt = np.where(long_vol > 1e-9, short_vol / long_vol, np.nan)
        valid = np.isfinite(vt)
        vt_all.extend(vt[valid].tolist())
        # persistence: lag-5 autocorr accumulation
        a = vt[LONG : n - 5]
        b = vt[LONG + 5 : n]
        m2 = np.isfinite(a) & np.isfinite(b)
        if m2.sum() > 5:
            am, bm = a[m2] - a[m2].mean(), b[m2] - b[m2].mean()
            autocorr_num += float((am * bm).sum())
            autocorr_den += float(np.sqrt((am * am).sum() * (bm * bm).sum()))
        # forward |return| pairs (tradeable: |close[i+1+FWD]/close[i+1] - 1|)
        for i in range(LONG, n - FWD - 1):
            if np.isfinite(vt[i]) and close[i + 1] > 0:
                fwd_absret = abs(close[i + 1 + FWD] / close[i + 1] - 1.0)
                pairs.append((float(vt[i]), float(fwd_absret)))
    if n_days < 30 or len(vt_all) < 200:
        return None
    rng = np.random.default_rng(abs(hash(symbol)) % (2**32))
    if len(pairs) > SAMPLE_PER_SYM:
        idx = rng.choice(len(pairs), SAMPLE_PER_SYM, replace=False)
        pairs = [pairs[j] for j in idx]
    vt_arr = np.array(vt_all)
    return {
        "symbol": symbol,
        "adv": dollar_total / n_days,
        "vt_median": float(np.median(vt_arr)),
        "vt_p10": float(np.percentile(vt_arr, 10)),
        "vt_p90": float(np.percentile(vt_arr, 90)),
        "frac_expanding": float((vt_arr > 1.0).mean()),
        "autocorr_num": autocorr_num,
        "autocorr_den": autocorr_den,
        "pairs": pairs,
    }


def summarize(name: str, results: list[dict[str, object]]) -> None:
    if not results:
        print(f"\n{name}: no symbols")
        return
    med = np.median([r["vt_median"] for r in results])
    p10 = np.median([r["vt_p10"] for r in results])
    p90 = np.median([r["vt_p90"] for r in results])
    frac_exp = np.mean([r["frac_expanding"] for r in results])
    num = sum(r["autocorr_num"] for r in results)
    den = sum(r["autocorr_den"] for r in results)
    ac = num / den if den > 0 else float("nan")
    pairs = [p for r in results for p in r["pairs"]]
    arr = np.array(pairs)
    fwd_ic = float("nan")
    if len(arr) > 1000:
        fwd_ic = float(spearmanr(arr[:, 0], arr[:, 1]).correlation)
    print(f"\n=== {name} ({len(results)} syms) ===")
    print(f"  vol_term distribution: median {med:.3f} | p10 {p10:.3f} | p90 {p90:.3f} | frac expanding(>1) {frac_exp:.0%}")
    print(f"  persistence (lag-5 autocorr, pooled): {ac:+.3f}")
    print(f"  fwd |ret| 30m rank-IC vs vol_term: {fwd_ic:+.4f} (n={len(arr):,})")


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    syms = sorted(d.split("=")[1] for d in os.listdir(BARS) if d.startswith("symbol="))[::2]
    print(f"studying {len(syms)} symbols (parallel)...", flush=True)
    results = []
    done = 0
    with ProcessPoolExecutor(max_workers=8) as ex:
        for res in ex.map(study_symbol, syms, chunksize=20):
            if res is not None:
                results.append(res)
            done += 1
            if done % 1000 == 0:
                print(f"  {done}/{len(syms)} (kept {len(results)})", flush=True)

    results.sort(key=lambda r: -float(r["adv"]))
    liquid = results[:1000]
    spec = results[len(results) * 2 // 3 :]
    summarize("LIQUID (top-1000 by adv$)", liquid)
    summarize("SPECULATIVE (bottom third)", spec)

    well_spread = abs(np.median([r["vt_median"] for r in liquid]) - 1.0) >= 0.0 and (
        np.median([r["vt_p90"] for r in liquid]) - np.median([r["vt_p10"] for r in liquid])
    ) > 0.5
    liq_ac = sum(r["autocorr_num"] for r in liquid) / max(1e-9, sum(r["autocorr_den"] for r in liquid))
    print(
        f"\nFEATURE READ: spread={'WIDE' if well_spread else 'narrow'} "
        f"persistence={'REAL' if liq_ac > 0.05 else 'weak'} -> "
        f"{'SHIP vol_term_structure' if well_spread and liq_ac > 0.05 else 'reconsider'}"
    )


if __name__ == "__main__":
    np.seterr(all="ignore")
    main()
