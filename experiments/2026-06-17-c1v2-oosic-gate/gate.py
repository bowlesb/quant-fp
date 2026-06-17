"""C1 v2 cluster-map OOS-IC gate (PR #97). Pre-reg in prereg.md.

Computes peer_relative (within-cluster demeaned return) under v1 vs v2 cluster maps and compares their
OOS rank-IC against real forward returns (tradeable next-minute entry). Paired (same observations) so
the IC difference isolates the cluster map. Shuffle canary per map.
"""

import glob
import os

import numpy as np
import polars as pl
from scipy.stats import spearmanr

BARS = "/store/raw/bars"
V1 = "/app/quantlib/features/data/behavioral_clusters_v1.parquet"
V2 = "/app/experiments/2026-06-17-c1v2-oosic-gate/v2_clusters.parquet"
RET_WINDOWS = (5, 30)
FWD_HORIZONS = (5, 30)
RTH_LO, RTH_HI = 570, 960
N_SYMBOLS = 1000
MAX_DAYS = 60  # recent window — bounds the panel; ~1000 syms x 60d x 78 sampled min still ~4.7M obs
MINUTE_STRIDE = 5  # sample every 5th RTH minute for IC (returns/forwards still use full-minute closes)


def load_panel() -> pl.DataFrame:
    """Per (symbol, date, minute) close for a liquid sample, RTH only, over all available days."""
    # rank symbols by total dollar volume on a recent day to pick the liquid sample.
    probe_day = "2026-06-16"
    probe = []
    for path in glob.glob(f"{BARS}/symbol=*/date={probe_day}/data.parquet"):
        sym = path.split("symbol=")[1].split("/")[0]
        try:
            df = pl.read_parquet(path, columns=["close", "volume"])
            probe.append((sym, float((df["close"] * df["volume"]).sum())))
        except (OSError, pl.exceptions.PolarsError):
            continue
    probe.sort(key=lambda kv: -kv[1])
    syms = [s for s, _ in probe[:N_SYMBOLS]]

    frames = []
    for sym in syms:
        for path in sorted(glob.glob(f"{BARS}/symbol={sym}/date=*/data.parquet"))[-MAX_DAYS:]:
            date = os.path.basename(os.path.dirname(path)).split("=")[1]
            try:
                df = pl.read_parquet(path, columns=["ts", "close"])
            except (OSError, pl.exceptions.PolarsError):
                continue
            if df.height == 0:
                continue
            et = df["ts"].dt.convert_time_zone("America/New_York")
            etm = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
            df = df.filter((etm >= RTH_LO) & (etm < RTH_HI)).with_columns(
                pl.lit(sym).alias("symbol"), pl.lit(date).alias("date"), pl.col("ts").alias("minute")
            )
            if df.height:
                frames.append(df.select(["symbol", "date", "minute", "close"]))
    return pl.concat(frames).sort(["symbol", "date", "minute"])


def add_peer_rel(panel: pl.DataFrame, clusters: pl.DataFrame, suffix: str) -> pl.DataFrame:
    """peer_rel_w = ret_w - mean(ret_w) over the symbol's cluster at that minute, per window."""
    out = panel.join(clusters, on="symbol", how="left")
    exprs = []
    for w in RET_WINDOWS:
        ret = (pl.col("close") / pl.col("close").shift(w).over(["symbol", "date"]) - 1.0).alias(f"_ret{w}")
        exprs.append(ret)
    out = out.with_columns(exprs)
    peer_exprs = []
    for w in RET_WINDOWS:
        peer_mean = pl.col(f"_ret{w}").mean().over(["cluster_id", "minute"])
        peer_exprs.append(
            pl.when(pl.col("cluster_id").is_not_null())
            .then(pl.col(f"_ret{w}") - peer_mean)
            .otherwise(None)
            .alias(f"peerrel{w}_{suffix}")
        )
    return out.with_columns(peer_exprs).select(
        ["symbol", "date", "minute", *[f"peerrel{w}_{suffix}" for w in RET_WINDOWS]]
    )


def add_forward(panel: pl.DataFrame) -> pl.DataFrame:
    """fwd_h from the TRADEABLE next-minute close: entry = close.shift(-1); fwd = close.shift(-1-h)/entry-1."""
    exprs = []
    for h in FWD_HORIZONS:
        entry = pl.col("close").shift(-1).over(["symbol", "date"])
        future = pl.col("close").shift(-1 - h).over(["symbol", "date"])
        exprs.append((future / entry - 1.0).alias(f"fwd{h}"))
    return panel.with_columns(exprs)


def ic(a: np.ndarray, b: np.ndarray) -> tuple[float, int]:
    mask = np.isfinite(a) & np.isfinite(b)
    if int(mask.sum()) < 500:
        return float("nan"), int(mask.sum())
    return float(spearmanr(a[mask], b[mask]).correlation), int(mask.sum())


def main() -> None:
    print("loading panel...", flush=True)
    panel = load_panel()
    print(f"panel: {panel['symbol'].n_unique()} syms, {panel.height:,} rows, "
          f"{panel['date'].n_unique()} days", flush=True)

    v1 = pl.read_parquet(V1).select(pl.col("symbol").cast(pl.String), pl.col("cluster_id").cast(pl.Int32))
    v2 = pl.read_parquet(V2).select(pl.col("symbol").cast(pl.String), pl.col("cluster_id").cast(pl.Int32))
    # shuffled v1 (canary): permute cluster_id across symbols
    rng = np.random.default_rng(0)
    shuf_ids = v1["cluster_id"].to_numpy().copy()
    rng.shuffle(shuf_ids)
    vshuf = v1.with_columns(pl.Series("cluster_id", shuf_ids))

    base = add_forward(panel)
    pr1 = add_peer_rel(panel, v1, "v1")
    pr2 = add_peer_rel(panel, v2, "v2")
    prs = add_peer_rel(panel, vshuf, "shuf")
    merged = base.join(pr1, on=["symbol", "date", "minute"]).join(pr2, on=["symbol", "date", "minute"]).join(
        prs, on=["symbol", "date", "minute"]
    )

    dates = sorted(merged["date"].unique().to_list())
    cut = dates[int(len(dates) * 0.70)]
    test = merged.filter(pl.col("date") >= cut)
    print(f"\nwalk-forward: train < {cut} | TEST >= {cut} ({test.height:,} rows, {test['date'].n_unique()} days)")

    test_dates = sorted(test["date"].unique().to_list())
    print("\n=== OOS rank-IC vs forward returns (TEST set, paired) ===")
    print(f"{'signal':>10} {'fwd':>5} | {'v1 IC':>9} {'v2 IC':>9} {'v2-v1':>9} {'shuf IC':>9} | {'days v2>v1':>10}   winner")
    v2_wins = 0
    v2_losses = 0
    cells = 0
    for w in RET_WINDOWS:
        for h in FWD_HORIZONS:
            fwd = test[f"fwd{h}"].to_numpy()
            ic1, n1 = ic(test[f"peerrel{w}_v1"].to_numpy(), fwd)
            ic2, _ = ic(test[f"peerrel{w}_v2"].to_numpy(), fwd)
            ics, _ = ic(test[f"peerrel{w}_shuf"].to_numpy(), fwd)
            diff = abs(ic2) - abs(ic1)
            # per-day robustness: fraction of test days where v2 |IC| > v1 |IC| (paired by day).
            day_v2_better = 0
            day_total = 0
            for d in test_dates:
                sub = test.filter(pl.col("date") == d)
                f = sub[f"fwd{h}"].to_numpy()
                d1, nn = ic(sub[f"peerrel{w}_v1"].to_numpy(), f)
                d2, _ = ic(sub[f"peerrel{w}_v2"].to_numpy(), f)
                if nn >= 500 and np.isfinite(d1) and np.isfinite(d2):
                    day_total += 1
                    if abs(d2) > abs(d1):
                        day_v2_better += 1
            day_frac = day_v2_better / day_total if day_total else float("nan")
            winner = "v2" if diff > 0 else "v1"
            cells += 1
            if diff > 0:
                v2_wins += 1
            else:
                v2_losses += 1
            print(
                f"  peerrel{w:>2}m {h:>4}m | {ic1:+.5f} {ic2:+.5f} {diff:+.5f} {ics:+.5f} | "
                f"{day_v2_better:>4}/{day_total:<4} ({day_frac:.0%})   {winner}  (n={n1:,})"
            )

    print(f"\nv2 |IC| > v1 |IC| in {v2_wins}/{cells} cells (pooled).")
    verdict = "v2 WINS — recommend merge #97" if v2_wins > v2_losses and v2_wins >= 3 else (
        "v2 does NOT win — keep v1 (cohesion gain does not translate)"
    )
    print(f"VERDICT: {verdict}")


if __name__ == "__main__":
    np.seterr(all="ignore")
    main()
