"""
HF01 — Quote-imbalance / signed-flow predicting next-1-to-5-min megacap return.
Pre-registered experiment. Read-only data access, writes only to /app/experiments/.

Memory strategy: process one (symbol, date) at a time, accumulate results to parquet
chunks, then concatenate for analysis. Never hold full symbol in memory.
"""
import sys
import numpy as np
import polars as pl
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
SYMBOLS = ["MSFT", "AVGO", "AMD", "TSLA", "AAPL"]
HORIZONS_MIN = [1, 2, 5]
WINDOWS_SEC = [30, 60, 120]
BUCKET_SEC = 10

RTH_MIN_START = 815   # 09:35 ET in UTC minutes
RTH_MIN_END = 1190    # 15:50 ET in UTC minutes

QUOTE_ROOT = Path("/store/raw/quotes")
TRADE_ROOT = Path("/store/raw/trades")
EXP_DIR = Path("/app/experiments/2026-06-16-hf01-quote-imbalance")
CACHE_DIR = EXP_DIR / "cache"

N_CANARY_SEEDS = 10
TRAIN_FRAC = 0.50
NO_TRADE_BANDS = [0.0, 0.02, 0.05, 0.10, 0.15, 0.20]


# ── Helpers ──────────────────────────────────────────────────────────────────

def utc_min_expr(ts_col: pl.Expr) -> pl.Expr:
    return ts_col.dt.hour().cast(pl.Int32) * 60 + ts_col.dt.minute().cast(pl.Int32)


def floor_bucket_expr(ts_col: pl.Expr) -> pl.Expr:
    bucket_us = BUCKET_SEC * 1_000_000
    return (ts_col.dt.epoch("us") // bucket_us * bucket_us).cast(pl.Datetime("us", "UTC"))


def spearman_ic(x: np.ndarray, y: np.ndarray) -> float:
    mask = np.isfinite(x) & np.isfinite(y)
    n = int(mask.sum())
    if n < 10:
        return np.nan
    xm, ym = x[mask], y[mask]
    rx = np.argsort(np.argsort(xm)).astype(float)
    ry = np.argsort(np.argsort(ym)).astype(float)
    rx -= rx.mean()
    ry -= ry.mean()
    denom = float(np.sqrt((rx ** 2).sum() * (ry ** 2).sum()))
    if denom == 0.0:
        return np.nan
    return float((rx * ry).sum() / denom)


def day_clustered_tstat(daily_ics: list[float]) -> tuple[float, float]:
    arr = np.array([v for v in daily_ics if np.isfinite(v)])
    if len(arr) < 3:
        return np.nan, np.nan
    mean_ic = float(arr.mean())
    return mean_ic, float(mean_ic / (arr.std(ddof=1) / np.sqrt(len(arr))))


def canary_band(signal: np.ndarray, target: np.ndarray, dates: np.ndarray,
                n_seeds: int = N_CANARY_SEEDS) -> tuple[float, float]:
    rng = np.random.default_rng(42)
    shuffle_ics = []
    for _ in range(n_seeds):
        perm = target.copy()
        for date in np.unique(dates):
            mask = dates == date
            perm[mask] = rng.permuted(target[mask])
        ic = spearman_ic(signal, perm)
        if np.isfinite(ic):
            shuffle_ics.append(ic)
    if not shuffle_ics:
        return np.nan, np.nan
    return float(np.percentile(shuffle_ics, 2.5)), float(np.percentile(shuffle_ics, 97.5))


# ── Per-day data pipeline ────────────────────────────────────────────────────

def process_one_day(symbol: str, date_str: str) -> pl.DataFrame | None:
    """
    Process one (symbol, date): load quotes + trades for that day,
    compute 10s grid, forward returns, and trailing signals.
    Returns a small DataFrame (one row per 10s bucket) or None on failure.
    """
    quote_path = QUOTE_ROOT / f"symbol={symbol}" / f"date={date_str}" / "data.parquet"
    trade_path = TRADE_ROOT / f"symbol={symbol}" / f"date={date_str}" / "data.parquet"

    if not quote_path.exists():
        return None

    # ── Load quotes ──────────────────────────────────────────────────────────
    q = pl.read_parquet(quote_path, columns=["ts", "bid_price", "bid_size", "ask_price", "ask_size"])
    q = q.with_columns(utc_min_expr(pl.col("ts")).alias("_um"))
    q = q.filter(
        (pl.col("_um") >= RTH_MIN_START) & (pl.col("_um") < RTH_MIN_END) &
        (pl.col("bid_price") > 0) & (pl.col("ask_price") > 0) &
        (pl.col("bid_size") > 0) & (pl.col("ask_size") > 0) &
        (pl.col("ask_price") >= pl.col("bid_price"))
    ).drop("_um").sort("ts")

    if len(q) < 50:
        return None

    # ── Compute mid and OFI tick features ───────────────────────────────────
    q = q.with_columns([
        ((pl.col("bid_price") + pl.col("ask_price")) / 2.0).alias("mid"),
        ((pl.col("bid_size") - pl.col("ask_size")) / (pl.col("bid_size") + pl.col("ask_size"))).alias("qimb_tick"),
        (pl.col("bid_size") + pl.col("ask_size")).alias("total_sz"),
    ])

    # OFI per tick (CKS)
    q = q.with_columns([
        pl.col("bid_price").shift(1).alias("pb_prev"),
        pl.col("bid_size").shift(1).alias("bs_prev"),
        pl.col("ask_price").shift(1).alias("pa_prev"),
        pl.col("ask_size").shift(1).alias("as_prev"),
    ])
    q = q.filter(pl.col("pb_prev").is_not_null())  # drop first tick (no prev)

    q = q.with_columns([
        pl.when(pl.col("bid_price") > pl.col("pb_prev")).then(pl.col("bid_size"))
          .when(pl.col("bid_price") == pl.col("pb_prev")).then(pl.col("bid_size") - pl.col("bs_prev"))
          .otherwise(-pl.col("bs_prev"))
          .alias("ofi_bid"),
        pl.when(pl.col("ask_price") < pl.col("pa_prev")).then(pl.col("ask_size"))
          .when(pl.col("ask_price") == pl.col("pa_prev")).then(-(pl.col("ask_size") - pl.col("as_prev")))
          .otherwise(-pl.col("as_prev"))
          .alias("ofi_ask"),
    ])
    q = q.with_columns((pl.col("ofi_bid") + pl.col("ofi_ask")).alias("ofi_tick"))

    # ── Resample to 10s grid ─────────────────────────────────────────────────
    q = q.with_columns(floor_bucket_expr(pl.col("ts")).alias("bucket"))
    grid = (
        q.group_by("bucket")
         .agg([
             pl.col("mid").last().alias("mid"),
             pl.col("bid_price").last().alias("bid"),
             pl.col("ask_price").last().alias("ask"),
         ])
         .sort("bucket")
    )

    # ── Forward returns ──────────────────────────────────────────────────────
    mids = grid["mid"].to_numpy()
    n = len(mids)
    buckets_per_min = 60 // BUCKET_SEC
    fwd_cols = {}
    for h_min in HORIZONS_MIN:
        h_b = h_min * buckets_per_min
        fwd = np.full(n, np.nan)
        if h_b < n:
            fwd[:n - h_b] = mids[h_b:] / mids[:n - h_b] - 1.0
        fwd_cols[f"fwd_{h_min}m"] = fwd
    for col_name, arr in fwd_cols.items():
        grid = grid.with_columns(pl.Series(name=col_name, values=arr))

    # ── Rolling signals on raw ticks ─────────────────────────────────────────
    # For each grid bucket t, signal uses events in [t - w - 1bucket, t - 1bucket)
    # We compute rolling over the raw quote series, then join_asof to grid at (t - 1 bucket)
    bucket_us = BUCKET_SEC * 1_000_000

    roll_exprs = []
    for w_sec in WINDOWS_SEC:
        wdur = f"{w_sec}s"
        roll_exprs += [
            pl.col("qimb_tick").rolling_mean_by("ts", window_size=wdur, closed="left").alias(f"qimb_{w_sec}"),
            pl.col("ofi_tick").rolling_sum_by("ts", window_size=wdur, closed="left").alias(f"ofi_sum_{w_sec}"),
            pl.col("total_sz").rolling_sum_by("ts", window_size=wdur, closed="left").alias(f"ofi_norm_{w_sec}"),
        ]

    q_rolled = q.select(["ts", "qimb_tick", "ofi_tick", "total_sz"]).with_columns(roll_exprs)

    for w_sec in WINDOWS_SEC:
        q_rolled = q_rolled.with_columns(
            (pl.col(f"ofi_sum_{w_sec}") / pl.col(f"ofi_norm_{w_sec}")).alias(f"ofi_{w_sec}")
        )

    # lookup_ts = bucket - 1 bucket (1-bucket lag for strict trailing)
    grid_lk = grid.with_columns(
        (pl.col("bucket").dt.epoch("us") - bucket_us).cast(pl.Datetime("us", "UTC")).alias("lookup_ts")
    )

    q_sig_cols = ["ts"] + [f"qimb_{w}" for w in WINDOWS_SEC] + [f"ofi_{w}" for w in WINDOWS_SEC]
    joined = grid_lk.join_asof(
        q_rolled.select(q_sig_cols).sort("ts"),
        left_on="lookup_ts",
        right_on="ts",
        strategy="backward",
    ).drop("lookup_ts")
    grid = joined

    # ── Trade flow signal ─────────────────────────────────────────────────────
    if trade_path.exists():
        t = pl.read_parquet(trade_path, columns=["ts", "price", "size"])
        t = t.with_columns(utc_min_expr(pl.col("ts")).alias("_um"))
        t = t.filter(
            (pl.col("_um") >= RTH_MIN_START) & (pl.col("_um") < RTH_MIN_END) &
            (pl.col("price") > 0) & (pl.col("size") > 0)
        ).drop("_um").sort("ts")

        if len(t) >= 2:
            t = t.with_columns(pl.col("price").shift(1).alias("prev_price"))
            t = t.filter(pl.col("prev_price").is_not_null())
            t = t.with_columns(
                pl.when(pl.col("price") > pl.col("prev_price")).then(1.0)
                  .when(pl.col("price") < pl.col("prev_price")).then(-1.0)
                  .otherwise(None)
                  .alias("tick_sign_raw")
            )
            t = t.with_columns(
                pl.col("tick_sign_raw").forward_fill().fill_null(1.0).alias("tick_sign")
            )
            t = t.with_columns((pl.col("tick_sign") * pl.col("size")).alias("signed_vol"))

            trade_roll_exprs = []
            for w_sec in WINDOWS_SEC:
                wdur = f"{w_sec}s"
                trade_roll_exprs += [
                    pl.col("signed_vol").rolling_sum_by("ts", window_size=wdur, closed="left").alias(f"sv_{w_sec}"),
                    pl.col("size").rolling_sum_by("ts", window_size=wdur, closed="left").alias(f"vol_{w_sec}"),
                ]

            t_rolled = t.select(["ts", "signed_vol", "size"]).with_columns(trade_roll_exprs)
            for w_sec in WINDOWS_SEC:
                t_rolled = t_rolled.with_columns(
                    (pl.col(f"sv_{w_sec}") / pl.col(f"vol_{w_sec}")).alias(f"stflow_{w_sec}")
                )

            grid_lk2 = grid.with_columns(
                (pl.col("bucket").dt.epoch("us") - bucket_us).cast(pl.Datetime("us", "UTC")).alias("lookup_ts")
            )
            stf_cols = ["ts"] + [f"stflow_{w}" for w in WINDOWS_SEC]
            joined2 = grid_lk2.join_asof(
                t_rolled.select(stf_cols).sort("ts"),
                left_on="lookup_ts",
                right_on="ts",
                strategy="backward",
            ).drop("lookup_ts")
            grid = joined2
        else:
            for w_sec in WINDOWS_SEC:
                grid = grid.with_columns(pl.lit(None, dtype=pl.Float64).alias(f"stflow_{w_sec}"))
    else:
        for w_sec in WINDOWS_SEC:
            grid = grid.with_columns(pl.lit(None, dtype=pl.Float64).alias(f"stflow_{w_sec}"))

    # Add metadata
    grid = grid.with_columns([
        pl.lit(symbol).alias("symbol"),
        pl.lit(date_str).alias("date"),
    ])

    # Keep only analysis columns to minimize cache size
    keep_cols = (
        ["symbol", "date", "bucket", "bid", "ask", "mid"]
        + [f"fwd_{h}m" for h in HORIZONS_MIN]
        + [f"qimb_{w}" for w in WINDOWS_SEC]
        + [f"ofi_{w}" for w in WINDOWS_SEC]
        + [f"stflow_{w}" for w in WINDOWS_SEC]
    )
    grid = grid.select([c for c in keep_cols if c in grid.columns])
    return grid


# ── Analysis functions ────────────────────────────────────────────────────────

def compute_ic_table(pooled: pl.DataFrame) -> list[dict]:
    rows = []
    for sig in ["qimb", "ofi", "stflow"]:
        for w in WINDOWS_SEC:
            col = f"{sig}_{w}"
            if col not in pooled.columns:
                continue
            for h_min in HORIZONS_MIN:
                ret_col = f"fwd_{h_min}m"
                sub = pooled.select([col, ret_col, "date"]).drop_nulls()
                if len(sub) < 100:
                    continue
                x = sub[col].to_numpy()
                y = sub[ret_col].to_numpy()
                dates_arr = sub["date"].to_numpy()
                ic_overall = spearman_ic(x, y)

                daily_ics = []
                for date in sub["date"].unique().to_list():
                    mask = sub["date"] == date
                    xd = sub.filter(mask)[col].to_numpy()
                    yd = sub.filter(mask)[ret_col].to_numpy()
                    daily_ics.append(spearman_ic(xd, yd))

                mean_ic, t_stat = day_clustered_tstat(daily_ics)
                c_lo, c_hi = canary_band(x, y, dates_arr)
                canary_ok = np.isfinite(ic_overall) and np.isfinite(c_hi) and (ic_overall > c_hi or ic_overall < c_lo)

                rows.append({
                    "signal": sig, "w": w, "h_min": h_min,
                    "ic_overall": round(ic_overall, 5),
                    "mean_daily_ic": round(mean_ic, 5),
                    "t_stat": round(t_stat, 2),
                    "canary_lo": round(c_lo, 5),
                    "canary_hi": round(c_hi, 5),
                    "canary_pass": canary_ok,
                    "n_obs": len(sub),
                    "n_days": len(daily_ics),
                })
    return rows


def compute_demean_ic(pooled: pl.DataFrame) -> list[dict]:
    rows = []
    for sig in ["qimb", "ofi", "stflow"]:
        for w in WINDOWS_SEC:
            col = f"{sig}_{w}"
            if col not in pooled.columns:
                continue
            for h_min in HORIZONS_MIN:
                ret_col = f"fwd_{h_min}m"
                sub = pooled.select([col, ret_col, "date", "symbol"]).drop_nulls()
                if len(sub) < 100:
                    continue
                sub = sub.with_columns(
                    (pl.col(ret_col) - pl.col(ret_col).mean().over("symbol")).alias("ret_dm")
                )
                x = sub[col].to_numpy()
                y_dm = sub["ret_dm"].to_numpy()
                daily_ics = []
                for date in sub["date"].unique().to_list():
                    mask = sub["date"] == date
                    xd = sub.filter(mask)[col].to_numpy()
                    yd = sub.filter(mask)["ret_dm"].to_numpy()
                    daily_ics.append(spearman_ic(xd, yd))
                mean_dm, t_dm = day_clustered_tstat(daily_ics)
                rows.append({
                    "signal": sig, "w": w, "h_min": h_min,
                    "ic_demeaned": round(spearman_ic(x, y_dm), 5),
                    "mean_daily_ic_dm": round(mean_dm, 5),
                    "t_dm": round(t_dm, 2),
                })
    return rows


def compute_oos(pooled: pl.DataFrame, oos_dates: set) -> list[dict]:
    rows = []
    for sig in ["qimb", "ofi", "stflow"]:
        for w in WINDOWS_SEC:
            col = f"{sig}_{w}"
            if col not in pooled.columns:
                continue
            for h_min in HORIZONS_MIN:
                ret_col = f"fwd_{h_min}m"
                sub = pooled.filter(pl.col("date").is_in(oos_dates)).select(
                    [col, ret_col, "date", "symbol"]
                ).drop_nulls()
                if len(sub) < 50:
                    continue
                sub = sub.with_columns(
                    (pl.col(ret_col) - pl.col(ret_col).mean().over("symbol")).alias("ret_dm")
                )
                x = sub[col].to_numpy()
                y_dm = sub["ret_dm"].to_numpy()
                daily_ics = []
                for date in sub["date"].unique().to_list():
                    mask = sub["date"] == date
                    xd = sub.filter(mask)[col].to_numpy()
                    yd = sub.filter(mask)["ret_dm"].to_numpy()
                    daily_ics.append(spearman_ic(xd, yd))
                mean_oos, t_oos = day_clustered_tstat(daily_ics)
                rows.append({
                    "signal": sig, "w": w, "h_min": h_min,
                    "ic_oos": round(spearman_ic(x, y_dm), 5),
                    "mean_daily_ic_oos": round(mean_oos, 5),
                    "t_oos": round(t_oos, 2),
                    "n_oos": len(sub),
                })
    return rows


def compute_cost_gate(pooled: pl.DataFrame, oos_dates: set, spread_bps: dict[str, float]) -> list[dict]:
    rows = []
    for sig in ["qimb", "ofi", "stflow"]:
        for w in WINDOWS_SEC:
            col = f"{sig}_{w}"
            if col not in pooled.columns:
                continue
            for h_min in HORIZONS_MIN:
                ret_col = f"fwd_{h_min}m"
                sub = pooled.filter(pl.col("date").is_in(oos_dates)).select(
                    [col, ret_col, "date", "symbol"]
                ).drop_nulls(subset=[col, ret_col])
                if len(sub) < 50:
                    continue
                for band in NO_TRADE_BANDS:
                    sym_rows = []
                    for sym in sub["symbol"].unique().to_list():
                        sym_data = sub.filter(pl.col("symbol") == sym).sort("date")
                        sig_arr = sym_data[col].fill_nan(0.0).to_numpy()
                        ret_arr = sym_data[ret_col].fill_nan(0.0).to_numpy()
                        pos = np.where(sig_arr > band, 1.0, np.where(sig_arr < -band, -1.0, 0.0))
                        gross = float((pos * ret_arr).mean())
                        turnover = float(np.abs(np.diff(np.concatenate([[0.0], pos]))).mean())
                        rt_cost = spread_bps.get(sym, 1.0) * 2.0 / 10000.0
                        sym_rows.append({
                            "gross": gross,
                            "turnover": turnover,
                            "net": gross - turnover * rt_cost,
                            "net_2x": gross - 2.0 * turnover * rt_cost,
                            "rt_bps": spread_bps.get(sym, 1.0) * 2.0,
                        })
                    if not sym_rows:
                        continue
                    rows.append({
                        "signal": sig, "w": w, "h_min": h_min, "band": band,
                        "gross_bps": round(float(np.mean([r["gross"] for r in sym_rows])) * 10000, 4),
                        "turnover": round(float(np.mean([r["turnover"] for r in sym_rows])), 4),
                        "net_bps": round(float(np.mean([r["net"] for r in sym_rows])) * 10000, 4),
                        "net_2x_bps": round(float(np.mean([r["net_2x"] for r in sym_rows])) * 10000, 4),
                        "avg_rt_cost_bps": round(float(np.mean([r["rt_bps"] for r in sym_rows])), 3),
                        "n_syms": len(sym_rows),
                    })
    return rows


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    print("=== HF01: Processing (symbol, date) pairs ===", flush=True)

    spread_bps_per_sym: dict[str, float] = {}
    all_sym_frames: list[pl.DataFrame] = []

    for sym in SYMBOLS:
        sym_dir = QUOTE_ROOT / f"symbol={sym}"
        if not sym_dir.exists():
            print(f"  {sym}: no data dir, skip")
            continue

        dates = sorted([d.name.replace("date=", "") for d in sym_dir.iterdir() if d.is_dir()])
        print(f"  {sym}: {len(dates)} dates", flush=True)

        sym_frames: list[pl.DataFrame] = []
        spreads: list[float] = []

        for date_str in dates:
            day_df = process_one_day(sym, date_str)
            if day_df is None or day_df.is_empty():
                continue
            # Collect spread from bid/ask
            if "bid" in day_df.columns and "ask" in day_df.columns and "mid" in day_df.columns:
                hs = float(((day_df["ask"] - day_df["bid"]) / day_df["mid"]).mean() * 10000 / 2.0)
                spreads.append(hs)
            sym_frames.append(day_df)

        if not sym_frames:
            print(f"  {sym}: no valid days")
            continue

        sym_df = pl.concat(sym_frames)
        spread_bps_per_sym[sym] = float(np.mean(spreads)) if spreads else 1.0
        print(f"    {sym}: {sym_df['date'].n_unique()} days, {len(sym_df)} rows, half-spread={spread_bps_per_sym[sym]:.3f} bps")
        all_sym_frames.append(sym_df)

    if not all_sym_frames:
        print("ERROR: no data computed")
        sys.exit(1)

    pooled = pl.concat(all_sym_frames, how="diagonal").sort(["symbol", "date", "bucket"])
    print(f"\nPooled: {len(pooled)} rows, {pooled['date'].n_unique()} unique dates, {pooled['symbol'].n_unique()} symbols")
    print("Columns:", pooled.columns)

    # ── Gates ────────────────────────────────────────────────────────────────
    print("\n=== Gate 1+2: IC + Canary ===", flush=True)
    ic_rows = compute_ic_table(pooled)
    ic_df = pl.DataFrame(ic_rows)
    print(ic_df.sort("t_stat", descending=True).__str__())
    ic_df.write_csv(EXP_DIR / "ic_results.csv")

    print("\n=== Gate 3: Per-symbol demeaned IC ===", flush=True)
    dm_rows = compute_demean_ic(pooled)
    dm_df = pl.DataFrame(dm_rows)
    print(dm_df.sort("t_dm", descending=True).__str__())
    dm_df.write_csv(EXP_DIR / "demean_results.csv")

    all_dates = sorted(pooled["date"].unique().to_list())
    n_train = int(len(all_dates) * TRAIN_FRAC)
    oos_dates = set(all_dates[n_train:])
    print(f"\n=== Gate 4: Walk-forward OOS === Train: {n_train} days, OOS: {len(oos_dates)} days", flush=True)
    oos_rows = compute_oos(pooled, oos_dates)
    oos_df = pl.DataFrame(oos_rows)
    print(oos_df.sort("t_oos", descending=True).__str__())
    oos_df.write_csv(EXP_DIR / "oos_results.csv")

    print("\n=== Gate 5: Turnover-compounded cost gate ===", flush=True)
    print("Per-symbol measured half-spread (bps):")
    for sym, hs in sorted(spread_bps_per_sym.items()):
        print(f"  {sym}: half-spread={hs:.3f} bps, round-trip={hs*2:.3f} bps")

    cost_rows = compute_cost_gate(pooled, oos_dates, spread_bps_per_sym)
    cost_df = pl.DataFrame(cost_rows)
    cost_df.write_csv(EXP_DIR / "cost_results.csv")

    print("\nBest band per (signal×w×h) by net_bps:")
    best = (
        cost_df.sort("net_bps", descending=True)
        .group_by(["signal", "w", "h_min"])
        .first()
        .sort("net_bps", descending=True)
        .head(20)
    )
    print(best.__str__())

    pl.DataFrame({
        "symbol": list(spread_bps_per_sym.keys()),
        "half_spread_bps": list(spread_bps_per_sym.values()),
    }).write_csv(EXP_DIR / "spreads.csv")

    print("\n=== Done. CSVs saved. ===")


if __name__ == "__main__":
    main()
