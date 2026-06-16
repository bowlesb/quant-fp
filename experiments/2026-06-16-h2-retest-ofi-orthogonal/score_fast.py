"""Fast VECTORIZED scorer for the H2-RETEST panel (replaces the O(n^2) per-minute-mask loop).

Same definitions as run_h2_retest.py (Spearman rank-IC per cross-section, day-clustered t, 10-seed shuffle
canary, cross-sectional residualize of fwd-ret on vwap_dev, decile L/S cost gate) but computed with polars
group_by over (date, minute) cross-sections — orders of magnitude faster. Writes data/results.json.
"""
import json
from pathlib import Path

import numpy as np
import polars as pl

DATA = Path("/app/experiments/2026-06-16-h2-retest-ofi-orthogonal/data")
N_MIN_CS = 20  # minimum symbols in a cross-section to score it

SIGNALS = ["ofi_15", "ofi_30", "ofi_15_norm", "ofi_30_norm", "sv_15", "sv_30",
           "sv_15_norm", "sv_30_norm", "vwap_dev_15", "vwap_dev_30"]
OFI_SIGS = ["ofi_15", "ofi_15_norm", "ofi_30", "ofi_30_norm", "sv_15", "sv_15_norm"]


def daily_ics(panel: pl.DataFrame, signal: str, target: str) -> pl.DataFrame:
    """Per-(date,minute) Spearman IC = Pearson of within-CS ranks; then mean per date. Returns (date, ic)."""
    sub = panel.select(["date", "minute", signal, target]).drop_nulls()
    # cross-section size filter
    sub = sub.with_columns(pl.len().over(["date", "minute"]).alias("__n")).filter(pl.col("__n") >= N_MIN_CS)
    if sub.height == 0:
        return pl.DataFrame({"date": [], "ic": []})
    ranked = sub.with_columns(
        pl.col(signal).rank().over(["date", "minute"]).alias("__rs"),
        pl.col(target).rank().over(["date", "minute"]).alias("__rt"),
    )
    per_min = ranked.group_by(["date", "minute"]).agg(
        pl.corr("__rs", "__rt").alias("ic")
    ).drop_nulls("ic")
    return per_min.group_by("date").agg(pl.col("ic").mean().alias("ic")).sort("date")


def tstat(daily: pl.DataFrame) -> tuple[float, float, int]:
    arr = daily["ic"].to_numpy()
    n = len(arr)
    if n < 2:
        return (float(arr[0]) if n == 1 else 0.0, 0.0, n)
    mean = float(np.mean(arr))
    se = float(np.std(arr, ddof=1)) / np.sqrt(n)
    return mean, (mean / se if se > 1e-12 else 0.0), n


def shuffle_target(panel: pl.DataFrame, target: str, seed: int) -> pl.DataFrame:
    """Permute the target WITHIN each (date, minute) cross-section (the canary null)."""
    rng = np.random.default_rng(seed)
    return panel.with_columns(
        pl.col(target).shuffle(seed=int(rng.integers(1 << 31))).over(["date", "minute"]).alias(target)
    )


def residualize(panel: pl.DataFrame, target: str, vwap_dev: str) -> pl.DataFrame:
    """Cross-sectional OLS residual of target on vwap_dev per (date, minute): resid = y - (a + b x)."""
    sub = panel.select(["date", "minute", "symbol", target, vwap_dev])
    stats = sub.drop_nulls([target, vwap_dev]).group_by(["date", "minute"]).agg(
        pl.len().alias("__n"),
        pl.col(vwap_dev).mean().alias("__xm"),
        pl.col(target).mean().alias("__ym"),
        ((pl.col(vwap_dev) - pl.col(vwap_dev).mean()) * (pl.col(target) - pl.col(target).mean())).sum().alias("__sxy"),
        ((pl.col(vwap_dev) - pl.col(vwap_dev).mean()) ** 2).sum().alias("__sxx"),
    )
    stats = stats.with_columns(
        pl.when((pl.col("__sxx") > 1e-12) & (pl.col("__n") >= 5))
        .then(pl.col("__sxy") / pl.col("__sxx")).otherwise(0.0).alias("__b")
    ).with_columns((pl.col("__ym") - pl.col("__b") * pl.col("__xm")).alias("__a"))
    out = sub.join(stats.select(["date", "minute", "__a", "__b"]), on=["date", "minute"], how="left")
    return out.with_columns(
        (pl.col(target) - (pl.col("__a") + pl.col("__b") * pl.col(vwap_dev))).alias(f"{target}_resid")
    ).select(["date", "minute", "symbol", f"{target}_resid"])


def cost_gate(panel: pl.DataFrame, signal: str, target: str) -> dict:
    median_spread_bps = float(panel["rel_spread_mean"].drop_nulls().median() * 1e4)
    roundtrip = median_spread_bps  # one-way = spread/2, round-trip = spread
    sub = panel.select(["date", "minute", signal, target]).drop_nulls()
    sub = sub.with_columns(pl.len().over(["date", "minute"]).alias("__n")).filter(pl.col("__n") >= N_MIN_CS)
    ranked = sub.with_columns(
        (pl.col(signal).rank().over(["date", "minute"]) / pl.col("__n")).alias("__pct")
    )
    legs = ranked.group_by(["date", "minute"]).agg(
        pl.col(target).filter(pl.col("__pct") >= 0.9).mean().alias("__top"),
        pl.col(target).filter(pl.col("__pct") <= 0.1).mean().alias("__bot"),
    ).drop_nulls()
    gross = float((legs["__top"] - legs["__bot"]).mean() * 1e4)
    return {"median_spread_bps": median_spread_bps, "roundtrip_cost_bps": roundtrip,
            "gross_ls_bps": gross, "clears_cost": gross > roundtrip}


def main() -> None:
    panel = pl.read_parquet(DATA / "panel.parquet")
    results: dict = {"panel": {"n_rows": panel.height, "n_symbols": panel["symbol"].n_unique(),
                               "n_days": panel["date"].n_unique()}}

    for horizon, tgt in [("H15", "fwd_ret_15"), ("H30", "fwd_ret_30")]:
        results[horizon] = {}
        for sig in SIGNALS:
            mean, t, n = tstat(daily_ics(panel, sig, tgt))
            results[horizon][sig] = {"mean_ic": mean, "tstat": t, "n_days": n}
            print(f"{horizon} {sig:14s}: IC={mean:+.4f} t={t:+.2f} nd={n}", flush=True)

    print("canary (10 seeds, H15)...", flush=True)
    canary: dict[str, list[float]] = {sig: [] for sig in SIGNALS}
    for seed in range(10):
        shuf = shuffle_target(panel, "fwd_ret_15", seed)
        for sig in SIGNALS:
            mean, _, _ = tstat(daily_ics(shuf, sig, "fwd_ret_15"))
            canary[sig].append(mean)
    results["canary_bands"] = {sig: [float(np.percentile(canary[sig], 2.5)),
                                     float(np.percentile(canary[sig], 97.5))] for sig in SIGNALS}

    print("residualized marginal-over-vwap_dev...", flush=True)
    results["residual"] = {}
    for horizon, tgt, vdev in [("H15", "fwd_ret_15", "vwap_dev_15"), ("H30", "fwd_ret_30", "vwap_dev_30")]:
        resid = residualize(panel, tgt, vdev)
        merged = panel.join(resid, on=["date", "minute", "symbol"], how="left")
        results["residual"][horizon] = {}
        for sig in OFI_SIGS:
            mean, t, n = tstat(daily_ics(merged, sig, f"{tgt}_resid"))
            results["residual"][horizon][sig] = {"mean_ic": mean, "tstat": t, "n_days": n}
            print(f"  resid {horizon} {sig:14s}: IC={mean:+.4f} t={t:+.2f}", flush=True)

    print("cost gate...", flush=True)
    results["cost_gate"] = {}
    for sig, tgt, label in [("ofi_15_norm", "fwd_ret_15", "ofi_15_norm_H15"),
                            ("ofi_30_norm", "fwd_ret_30", "ofi_30_norm_H30"),
                            ("ofi_15", "fwd_ret_15", "ofi_15_H15")]:
        g = cost_gate(panel, sig, tgt)
        results["cost_gate"][label] = g
        print(f"  {label}: gross={g['gross_ls_bps']:+.2f}bps rt_cost={g['roundtrip_cost_bps']:.2f}bps "
              f"clears={g['clears_cost']}", flush=True)

    with open(DATA / "results.json", "w") as fp:
        json.dump(results, fp, indent=2, default=float)
    print("saved results.json", flush=True)


if __name__ == "__main__":
    main()
