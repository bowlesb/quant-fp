"""
HF02 — qimb at longer horizons (5/10/15/30 min) + lower-turnover overlays.
Pre-registered experiment. Read-only data; writes only to /app/experiments/hf02.

Core question: does extending horizon + trading on PERSISTENCE ever clear the
turnover-compounded cost gate at measured spread, OOS?

Overlays tested:
  - HOLD: enter on non-zero qimb, hold full h buckets, flip only after h buckets
  - PERSISTENCE(K, thresh): enter only when qimb has held same sign for K consecutive
    buckets AND |qimb| > thresh; hold for h buckets once entered.

Uses hf_metrics_fixed.py (verified demean + OOS, no all-NaN bug).
"""
import sys
import numpy as np
import polars as pl
from pathlib import Path

# Ensure hf_metrics_fixed is importable from hf01 dir
sys.path.insert(0, "/app/experiments/2026-06-16-hf01-quote-imbalance")
from hf_metrics_fixed import (  # noqa: E402
    spearman_ic,
    day_clustered_tstat,
    demean_within_symbol,
    per_symbol_day_ics,
    compute_demean_ic,
    compute_oos_ic,
)

# ── Config ───────────────────────────────────────────────────────────────────
# Extended symbol panel: ~12 megacaps with ≥21 quote-days
SYMBOLS = [
    "MSFT", "AAPL", "TSLA", "AVGO", "AMD",   # HF01 panel (deepest)
    "NVDA", "SPY", "AMZN", "META", "GOOGL",  # additional megacaps
    "QQQ", "NFLX",
]
HORIZONS_MIN = [5, 10, 15, 30]    # HF02 extended grid
WINDOWS_SEC = [120, 300]           # 2min + 5min trailing windows
BUCKET_SEC = 10

# RTH = UTC minutes [810, 1190) => 13:30–19:50 UTC = 09:30–15:50 ET
RTH_MIN_START = 810
RTH_MIN_END = 1190

QUOTE_ROOT = Path("/store/raw/quotes")
EXP_DIR = Path("/app/experiments/2026-06-16-hf02-qimb-lowturnover")

TRAIN_FRAC = 0.50
N_CANARY_SEEDS = 10
MIN_DAYS = 21   # only include symbols with ≥21 quote-days

# Persistence sweep
PERSISTENCE_K = [2, 3, 5]          # consecutive buckets same sign
PERSISTENCE_THRESH = [0.05, 0.10, 0.15]  # |qimb| threshold


# ── Helpers ──────────────────────────────────────────────────────────────────

def utc_min_expr(ts_col: pl.Expr) -> pl.Expr:
    return ts_col.dt.hour().cast(pl.Int32) * 60 + ts_col.dt.minute().cast(pl.Int32)


def floor_bucket_expr(ts_col: pl.Expr) -> pl.Expr:
    bucket_us = BUCKET_SEC * 1_000_000
    return (ts_col.dt.epoch("us") // bucket_us * bucket_us).cast(pl.Datetime("us", "UTC"))


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
    """Load quotes for one (symbol, date), compute 10s grid + qimb signals + forward returns."""
    quote_path = QUOTE_ROOT / f"symbol={symbol}" / f"date={date_str}" / "data.parquet"
    if not quote_path.exists():
        return None

    q = pl.read_parquet(
        quote_path,
        columns=["ts", "bid_price", "bid_size", "ask_price", "ask_size"],
    )
    q = q.with_columns(utc_min_expr(pl.col("ts")).alias("_um"))
    q = q.filter(
        (pl.col("_um") >= RTH_MIN_START) & (pl.col("_um") < RTH_MIN_END)
        & (pl.col("bid_price") > 0) & (pl.col("ask_price") > 0)
        & (pl.col("bid_size") > 0) & (pl.col("ask_size") > 0)
        & (pl.col("ask_price") >= pl.col("bid_price"))
    ).drop("_um").sort("ts")

    if len(q) < 50:
        return None

    # mid + per-tick qimb
    q = q.with_columns([
        ((pl.col("bid_price") + pl.col("ask_price")) / 2.0).alias("mid"),
        ((pl.col("bid_size") - pl.col("ask_size")) / (pl.col("bid_size") + pl.col("ask_size"))).alias("qimb_tick"),
    ])

    # 10s grid
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

    mids = grid["mid"].to_numpy()
    nbuckets = len(mids)
    buckets_per_min = 60 // BUCKET_SEC  # = 6

    # Forward returns for all horizons.
    # Use polars null (not np.nan float) for the trailing buckets where no forward
    # return exists — so that drop_nulls() in hf_metrics_fixed correctly removes them
    # and group means don't collapse to NaN due to a handful of end-of-day floats.
    for h_min in HORIZONS_MIN:
        h_b = h_min * buckets_per_min
        fwd_list: list[float | None] = [None] * nbuckets
        if h_b < nbuckets:
            valid = (mids[h_b:] / mids[:nbuckets - h_b] - 1.0).tolist()
            for idx_v, val in enumerate(valid):
                fwd_list[idx_v] = float(val)
        grid = grid.with_columns(
            pl.Series(name=f"fwd_{h_min}m", values=fwd_list, dtype=pl.Float64)
        )

    # Rolling qimb signals — trailing, strict 1-bucket lag via join_asof
    bucket_us = BUCKET_SEC * 1_000_000
    roll_exprs = []
    for w_sec in WINDOWS_SEC:
        roll_exprs.append(
            pl.col("qimb_tick")
              .rolling_mean_by("ts", window_size=f"{w_sec}s", closed="left")
              .alias(f"qimb_{w_sec}")
        )
    q_rolled = q.select(["ts", "qimb_tick"]).with_columns(roll_exprs)

    grid_lk = grid.with_columns(
        (pl.col("bucket").dt.epoch("us") - bucket_us).cast(pl.Datetime("us", "UTC")).alias("lookup_ts")
    )
    sig_cols = ["ts"] + [f"qimb_{w}" for w in WINDOWS_SEC]
    grid = grid_lk.join_asof(
        q_rolled.select(sig_cols).sort("ts"),
        left_on="lookup_ts",
        right_on="ts",
        strategy="backward",
    ).drop("lookup_ts")

    grid = grid.with_columns([
        pl.lit(symbol).alias("symbol"),
        pl.lit(date_str).alias("date"),
    ])

    keep_cols = (
        ["symbol", "date", "bucket", "bid", "ask", "mid"]
        + [f"fwd_{h}m" for h in HORIZONS_MIN]
        + [f"qimb_{w}" for w in WINDOWS_SEC]
    )
    return grid.select([c for c in keep_cols if c in grid.columns])


# ── Overlay simulation ────────────────────────────────────────────────────────

def hold_overlay_positions(sig_arr: np.ndarray, h_buckets: int) -> np.ndarray:
    """HOLD overlay: enter on non-zero signed qimb, hold h_buckets, then re-evaluate.
    Turnover ≈ 2/h_buckets (one entry + one exit per hold period).
    Position is the sign of the entry signal, held for exactly h_buckets."""
    n = len(sig_arr)
    pos = np.zeros(n)
    idx = 0
    while idx < n:
        s = sig_arr[idx]
        if s > 0:
            end = min(idx + h_buckets, n)
            pos[idx:end] = 1.0
            idx = end
        elif s < 0:
            end = min(idx + h_buckets, n)
            pos[idx:end] = -1.0
            idx = end
        else:
            idx += 1
    return pos


def persistence_overlay_positions(sig_arr: np.ndarray, k_consec: int, thresh: float,
                                  h_buckets: int) -> np.ndarray:
    """PERSISTENCE overlay: enter only when same sign held K consecutive buckets AND |sig|>thresh.
    Once entered, hold for h_buckets. Then re-evaluate."""
    n = len(sig_arr)
    pos = np.zeros(n)
    idx = 0
    while idx < n:
        # Check persistence at current bucket
        if idx < k_consec:
            idx += 1
            continue
        window = sig_arr[idx - k_consec:idx]
        # All must share the same sign and current |sig| > thresh
        if not np.all(np.isfinite(window)):
            idx += 1
            continue
        current_sig = sig_arr[idx - 1]  # strictly trailing: last of the K buckets
        if abs(current_sig) <= thresh:
            idx += 1
            continue
        all_pos = np.all(window > 0)
        all_neg = np.all(window < 0)
        if all_pos:
            end = min(idx + h_buckets, n)
            pos[idx:end] = 1.0
            idx = end
        elif all_neg:
            end = min(idx + h_buckets, n)
            pos[idx:end] = -1.0
            idx = end
        else:
            idx += 1
    return pos


def extract_trades(pos: np.ndarray, ret_arr: np.ndarray, h_buckets: int,
                   rt_frac: float) -> list[float]:
    """Identify each non-overlapping ENTRY in a HOLD/PERSISTENCE position array and return its
    realized NET-OF-COST return (in fractional units).

    A held block enters at the first bucket where the position becomes non-zero after a zero (or after
    a sign flip). The h-min forward return at the entry bucket IS the realized move over the hold (the
    overlays hold for exactly h_buckets and jump past the block), so one trade == one entry bucket's
    signed forward return minus the round-trip cost. Returns the list of per-trade net returns so the
    +net headline can get a per-TRADE bootstrap, not just an IC t."""
    trades: list[float] = []
    n = len(pos)
    idx = 0
    while idx < n:
        if pos[idx] != 0.0:
            sign = pos[idx]
            realized = sign * ret_arr[idx]  # forward return over the hold, at entry
            trades.append(float(realized - rt_frac))  # one round-trip cost per trade
            # advance past this held block (same sign, contiguous)
            jdx = idx + 1
            while jdx < n and pos[jdx] == sign:
                jdx += 1
            idx = jdx
        else:
            idx += 1
    return trades


def bootstrap_trade_stats(trades: list[float], n_boot: int = 10000,
                          seed: int = 7) -> dict[str, float]:
    """Per-trade net-PnL diagnostics with a bootstrap 95% CI of the MEAN.

    Resamples the realized per-round-trip net-of-cost returns with replacement n_boot times. If the
    95% CI of the mean includes zero, the headline net is NOT significant (a handful of lucky fills),
    not a low-turnover edge. Returns mean/median/win-rate (bps) + CI bounds (bps) + analytic t."""
    arr = np.array([t for t in trades if np.isfinite(t)])
    n = len(arr)
    if n < 5:
        return {
            "n_trades": float(n), "mean_bps": np.nan, "median_bps": np.nan,
            "win_rate": np.nan, "ci_lo_bps": np.nan, "ci_hi_bps": np.nan, "t": np.nan,
        }
    mean = float(arr.mean())
    se = float(arr.std(ddof=1) / np.sqrt(n))
    t_stat = mean / se if se > 0 else np.nan
    rng = np.random.default_rng(seed)
    boot_means = rng.choice(arr, size=(n_boot, n), replace=True).mean(axis=1)
    ci_lo = float(np.percentile(boot_means, 2.5))
    ci_hi = float(np.percentile(boot_means, 97.5))
    return {
        "n_trades": float(n),
        "mean_bps": mean * 10000.0,
        "median_bps": float(np.median(arr)) * 10000.0,
        "win_rate": float((arr > 0).mean()),
        "ci_lo_bps": ci_lo * 10000.0,
        "ci_hi_bps": ci_hi * 10000.0,
        "t": t_stat,
    }


def compute_overlay_cost_table(
    pooled: pl.DataFrame,
    oos_dates: set,
    spread_bps: dict[str, float],
) -> list[dict]:
    """For each (w×h×overlay_type×params) combo, compute OOS gross/turnover/net at 1× and 2× spread."""
    rows: list[dict] = []
    oos = pooled.filter(pl.col("date").is_in(list(oos_dates)))

    for w_sec in WINDOWS_SEC:
        col = f"qimb_{w_sec}"
        if col not in oos.columns:
            continue
        for h_min in HORIZONS_MIN:
            ret_col = f"fwd_{h_min}m"
            h_buckets = h_min * (60 // BUCKET_SEC)

            # --- HOLD overlay ---
            sym_rows_hold: list[dict] = []
            hold_trades_1x: list[float] = []
            hold_trades_2x: list[float] = []
            for sym in oos["symbol"].unique().to_list():
                # Process per (symbol, date) so a hold can't straddle the overnight gap
                sig_arr = oos.filter(pl.col("symbol") == sym).sort(["date", "bucket"])[col].fill_null(0.0).fill_nan(0.0).to_numpy()
                ret_arr = oos.filter(pl.col("symbol") == sym).sort(["date", "bucket"])[ret_col].fill_null(0.0).fill_nan(0.0).to_numpy()
                pos = hold_overlay_positions(sig_arr, h_buckets)
                gross = float((pos * ret_arr).mean())
                turnover = float(np.abs(np.diff(np.concatenate([[0.0], pos]))).mean())
                rt = spread_bps.get(sym, 1.0) * 2.0 / 10000.0
                sym_rows_hold.append({"gross": gross, "turnover": turnover, "rt": rt})
                hold_trades_1x.extend(extract_trades(pos, ret_arr, h_buckets, rt))
                hold_trades_2x.extend(extract_trades(pos, ret_arr, h_buckets, 2.0 * rt))

            if sym_rows_hold:
                bs1 = bootstrap_trade_stats(hold_trades_1x)
                bs2 = bootstrap_trade_stats(hold_trades_2x)
                rows.append({
                    "w": w_sec, "h_min": h_min,
                    "overlay": "HOLD", "k": 0, "thresh": 0.0,
                    "n_trades": int(bs1["n_trades"]),
                    "trade_mean_bps_1x": round(bs1["mean_bps"], 4) if np.isfinite(bs1["mean_bps"]) else None,
                    "trade_median_bps_1x": round(bs1["median_bps"], 4) if np.isfinite(bs1["median_bps"]) else None,
                    "win_rate": round(bs1["win_rate"], 4) if np.isfinite(bs1["win_rate"]) else None,
                    "ci_lo_1x": round(bs1["ci_lo_bps"], 4) if np.isfinite(bs1["ci_lo_bps"]) else None,
                    "ci_hi_1x": round(bs1["ci_hi_bps"], 4) if np.isfinite(bs1["ci_hi_bps"]) else None,
                    "ci_lo_2x": round(bs2["ci_lo_bps"], 4) if np.isfinite(bs2["ci_lo_bps"]) else None,
                    "ci_hi_2x": round(bs2["ci_hi_bps"], 4) if np.isfinite(bs2["ci_hi_bps"]) else None,
                    "trade_t_1x": round(bs1["t"], 2) if np.isfinite(bs1["t"]) else None,
                    "gross_bps": round(float(np.mean([r["gross"] for r in sym_rows_hold])) * 10000, 4),
                    "turnover": round(float(np.mean([r["turnover"] for r in sym_rows_hold])), 5),
                    "net_bps": round(float(np.mean(
                        [r["gross"] - r["turnover"] * r["rt"] for r in sym_rows_hold]
                    )) * 10000, 4),
                    "net_2x_bps": round(float(np.mean(
                        [r["gross"] - 2.0 * r["turnover"] * r["rt"] for r in sym_rows_hold]
                    )) * 10000, 4),
                    "avg_rt_bps": round(float(np.mean([r["rt"] for r in sym_rows_hold])) * 10000, 3),
                    "n_syms": len(sym_rows_hold),
                })

            # --- PERSISTENCE overlays ---
            for k_consec in PERSISTENCE_K:
                for thresh in PERSISTENCE_THRESH:
                    sym_rows_p: list[dict] = []
                    p_trades_1x: list[float] = []
                    p_trades_2x: list[float] = []
                    for sym in oos["symbol"].unique().to_list():
                        sym_data = oos.filter(pl.col("symbol") == sym).sort(["date", "bucket"])
                        sig_arr = sym_data[col].fill_null(0.0).fill_nan(0.0).to_numpy()
                        ret_arr = sym_data[ret_col].fill_null(0.0).fill_nan(0.0).to_numpy()
                        pos = persistence_overlay_positions(sig_arr, k_consec, thresh, h_buckets)
                        gross = float((pos * ret_arr).mean())
                        turnover = float(np.abs(np.diff(np.concatenate([[0.0], pos]))).mean())
                        rt = spread_bps.get(sym, 1.0) * 2.0 / 10000.0
                        sym_rows_p.append({"gross": gross, "turnover": turnover, "rt": rt})
                        p_trades_1x.extend(extract_trades(pos, ret_arr, h_buckets, rt))
                        p_trades_2x.extend(extract_trades(pos, ret_arr, h_buckets, 2.0 * rt))

                    if sym_rows_p:
                        bs1 = bootstrap_trade_stats(p_trades_1x)
                        bs2 = bootstrap_trade_stats(p_trades_2x)
                        rows.append({
                            "w": w_sec, "h_min": h_min,
                            "overlay": "PERSISTENCE", "k": k_consec, "thresh": thresh,
                            "n_trades": int(bs1["n_trades"]),
                            "trade_mean_bps_1x": round(bs1["mean_bps"], 4) if np.isfinite(bs1["mean_bps"]) else None,
                            "trade_median_bps_1x": round(bs1["median_bps"], 4) if np.isfinite(bs1["median_bps"]) else None,
                            "win_rate": round(bs1["win_rate"], 4) if np.isfinite(bs1["win_rate"]) else None,
                            "ci_lo_1x": round(bs1["ci_lo_bps"], 4) if np.isfinite(bs1["ci_lo_bps"]) else None,
                            "ci_hi_1x": round(bs1["ci_hi_bps"], 4) if np.isfinite(bs1["ci_hi_bps"]) else None,
                            "ci_lo_2x": round(bs2["ci_lo_bps"], 4) if np.isfinite(bs2["ci_lo_bps"]) else None,
                            "ci_hi_2x": round(bs2["ci_hi_bps"], 4) if np.isfinite(bs2["ci_hi_bps"]) else None,
                            "trade_t_1x": round(bs1["t"], 2) if np.isfinite(bs1["t"]) else None,
                            "gross_bps": round(float(np.mean([r["gross"] for r in sym_rows_p])) * 10000, 4),
                            "turnover": round(float(np.mean([r["turnover"] for r in sym_rows_p])), 5),
                            "net_bps": round(float(np.mean(
                                [r["gross"] - r["turnover"] * r["rt"] for r in sym_rows_p]
                            )) * 10000, 4),
                            "net_2x_bps": round(float(np.mean(
                                [r["gross"] - 2.0 * r["turnover"] * r["rt"] for r in sym_rows_p]
                            )) * 10000, 4),
                            "avg_rt_bps": round(float(np.mean([r["rt"] for r in sym_rows_p])) * 10000, 3),
                            "n_syms": len(sym_rows_p),
                        })

    return rows


def compute_ic_table_hf02(pooled: pl.DataFrame) -> list[dict]:
    """Canary + raw IC for qimb at all (w×h) cells."""
    rows = []
    for w_sec in WINDOWS_SEC:
        col = f"qimb_{w_sec}"
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
                mask = (sub["date"] == date).to_numpy()
                daily_ics.append(spearman_ic(x[mask], y[mask]))

            mean_ic, t_stat = day_clustered_tstat(daily_ics)
            c_lo, c_hi = canary_band(x, y, dates_arr)
            canary_ok = (
                np.isfinite(ic_overall) and np.isfinite(c_hi)
                and (ic_overall > c_hi or ic_overall < c_lo)
            )
            rows.append({
                "w": w_sec, "h_min": h_min,
                "ic_overall": round(ic_overall, 5),
                "mean_daily_ic": round(mean_ic, 5) if np.isfinite(mean_ic) else None,
                "t_stat": round(t_stat, 2) if np.isfinite(t_stat) else None,
                "canary_lo": round(c_lo, 5),
                "canary_hi": round(c_hi, 5),
                "canary_pass": canary_ok,
                "n_obs": len(sub),
                "n_days": len([v for v in daily_ics if np.isfinite(v)]),
            })
    return rows


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    EXP_DIR.mkdir(parents=True, exist_ok=True)

    print("=== HF02: Loading (symbol, date) pairs ===", flush=True)

    spread_bps_per_sym: dict[str, float] = {}
    all_frames: list[pl.DataFrame] = []

    for sym in SYMBOLS:
        sym_dir = QUOTE_ROOT / f"symbol={sym}"
        if not sym_dir.exists():
            print(f"  {sym}: no data dir, skip", flush=True)
            continue

        dates = sorted([d.name.replace("date=", "") for d in sym_dir.iterdir() if d.is_dir()])
        print(f"  {sym}: {len(dates)} dates found", flush=True)

        if len(dates) < MIN_DAYS:
            print(f"  {sym}: only {len(dates)} days < {MIN_DAYS}, skip", flush=True)
            continue

        sym_frames: list[pl.DataFrame] = []
        spreads: list[float] = []

        for date_str in dates:
            day_df = process_one_day(sym, date_str)
            if day_df is None or day_df.is_empty():
                continue
            # Measure half-spread from bid/ask/mid
            if "bid" in day_df.columns and "ask" in day_df.columns and "mid" in day_df.columns:
                hs = float(((day_df["ask"] - day_df["bid"]) / day_df["mid"]).mean() * 10000 / 2.0)
                spreads.append(hs)
            sym_frames.append(day_df)

        if not sym_frames:
            print(f"  {sym}: no valid days after processing, skip", flush=True)
            continue

        sym_df = pl.concat(sym_frames)
        n_valid_days = sym_df["date"].n_unique()
        if n_valid_days < MIN_DAYS:
            print(f"  {sym}: only {n_valid_days} valid days < {MIN_DAYS}, skip", flush=True)
            continue

        spread_bps_per_sym[sym] = float(np.mean(spreads)) if spreads else 1.0
        print(
            f"    {sym}: {n_valid_days} days, {len(sym_df)} rows, "
            f"half-spread={spread_bps_per_sym[sym]:.3f} bps, "
            f"round-trip={spread_bps_per_sym[sym]*2:.3f} bps",
            flush=True,
        )
        all_frames.append(sym_df)

    if not all_frames:
        print("ERROR: no data", flush=True)
        sys.exit(1)

    pooled = pl.concat(all_frames, how="diagonal").sort(["symbol", "date", "bucket"])
    print(
        f"\nPooled: {len(pooled)} rows, {pooled['date'].n_unique()} unique dates, "
        f"{pooled['symbol'].n_unique()} symbols",
        flush=True,
    )

    # ── Train/OOS split ──────────────────────────────────────────────────────
    all_dates = sorted(pooled["date"].unique().to_list())
    n_train = int(len(all_dates) * TRAIN_FRAC)
    oos_dates = set(all_dates[n_train:])
    print(f"Train: {n_train} days, OOS: {len(oos_dates)} days", flush=True)

    signals = ["qimb"]
    windows = WINDOWS_SEC
    horizons = HORIZONS_MIN

    # ── Gate 1: Canary + raw IC ──────────────────────────────────────────────
    print("\n=== Gate 1: IC + Canary ===", flush=True)
    ic_rows = compute_ic_table_hf02(pooled)
    ic_df = pl.DataFrame(ic_rows)
    print(ic_df.sort("h_min").__str__(), flush=True)
    ic_df.write_csv(EXP_DIR / "ic_results.csv")

    # ── Gate 2: Fixed per-symbol demean IC ───────────────────────────────────
    print("\n=== Gate 2: Fixed per-symbol demean IC ===", flush=True)
    dm_rows = compute_demean_ic(pooled, signals, windows, horizons)
    dm_df = pl.DataFrame(dm_rows)
    print(dm_df.sort("h_min").__str__(), flush=True)

    # ASSERT not all-NaN
    dm_ic_vals = [r["mean_ic_dm"] for r in dm_rows if r["mean_ic_dm"] is not None]
    assert len(dm_ic_vals) > 0, "SANITY FAIL: ALL demean IC are NaN — bug in metrics"
    print(f"  Demean sanity: {len(dm_ic_vals)}/{len(dm_rows)} cells have non-NaN IC — OK", flush=True)
    dm_df.write_csv(EXP_DIR / "demean_results.csv")

    # ── Gate 3: Walk-forward OOS fixed IC ───────────────────────────────────
    print("\n=== Gate 3: Walk-forward OOS IC (fixed) ===", flush=True)
    oos_rows = compute_oos_ic(pooled, oos_dates, signals, windows, horizons)
    oos_df = pl.DataFrame(oos_rows)
    print(oos_df.sort("h_min").__str__(), flush=True)

    oos_ic_vals = [r["mean_ic_oos"] for r in oos_rows if r["mean_ic_oos"] is not None]
    assert len(oos_ic_vals) > 0, "SANITY FAIL: ALL OOS IC are NaN — bug in metrics"
    print(f"  OOS sanity: {len(oos_ic_vals)}/{len(oos_rows)} cells have non-NaN IC — OK", flush=True)
    oos_df.write_csv(EXP_DIR / "oos_results.csv")

    # ── Gate 4: Overlay turnover-compounded cost gate (OOS) ─────────────────
    print("\n=== Gate 4: Overlay cost table (OOS) ===", flush=True)
    print("Per-symbol measured spreads:", flush=True)
    for sym, hs in sorted(spread_bps_per_sym.items()):
        print(f"  {sym}: half={hs:.3f} bps, round-trip={hs*2:.3f} bps", flush=True)

    cost_rows = compute_overlay_cost_table(pooled, oos_dates, spread_bps_per_sym)
    cost_df = pl.DataFrame(cost_rows)
    cost_df.write_csv(EXP_DIR / "cost_results.csv")

    print("\nAll overlay cost results (sorted by net_bps desc):", flush=True)
    print(cost_df.sort("net_bps", descending=True).__str__(), flush=True)

    print("\nTop-10 by net_bps:", flush=True)
    print(cost_df.sort("net_bps", descending=True).head(10).__str__(), flush=True)

    print("\nTop-10 by net_2x_bps:", flush=True)
    print(cost_df.sort("net_2x_bps", descending=True).head(10).__str__(), flush=True)

    # Per-name spread table
    pl.DataFrame({
        "symbol": list(spread_bps_per_sym.keys()),
        "half_spread_bps": list(spread_bps_per_sym.values()),
        "round_trip_bps": [v * 2 for v in spread_bps_per_sym.values()],
    }).write_csv(EXP_DIR / "spreads.csv")

    # Summary: best net cell
    best_row = cost_df.sort("net_bps", descending=True).row(0, named=True)
    print(f"\n=== BEST NET CELL ===", flush=True)
    print(f"  w={best_row['w']}s, h={best_row['h_min']}m, overlay={best_row['overlay']}, "
          f"k={best_row['k']}, thresh={best_row['thresh']}", flush=True)
    print(f"  gross={best_row['gross_bps']} bps, turnover={best_row['turnover']:.5f}, "
          f"net@1x={best_row['net_bps']} bps, net@2x={best_row['net_2x_bps']} bps", flush=True)
    print(f"  TRADE-LEVEL: n_trades={best_row['n_trades']}, "
          f"mean={best_row['trade_mean_bps_1x']} bps, median={best_row['trade_median_bps_1x']} bps, "
          f"win_rate={best_row['win_rate']}, trade_t@1x={best_row['trade_t_1x']}", flush=True)
    print(f"  BOOTSTRAP 95% CI of mean per-trade net: "
          f"1x=[{best_row['ci_lo_1x']}, {best_row['ci_hi_1x']}] bps, "
          f"2x=[{best_row['ci_lo_2x']}, {best_row['ci_hi_2x']}] bps", flush=True)
    excl_1x = best_row['ci_lo_1x'] is not None and best_row['ci_lo_1x'] > 0
    excl_2x = best_row['ci_lo_2x'] is not None and best_row['ci_lo_2x'] > 0
    print(f"  CI excludes zero @1x: {excl_1x}; @2x: {excl_2x}", flush=True)

    any_positive_net = any(r["net_bps"] is not None and r["net_bps"] > 0 for r in cost_rows)
    any_positive_2x = any(r["net_2x_bps"] is not None and r["net_2x_bps"] > 0 for r in cost_rows)
    print(f"\nAny cell net >0 @ 1x spread: {any_positive_net}", flush=True)
    print(f"Any cell net >0 @ 2x spread: {any_positive_2x}", flush=True)

    # Best OOS demean IC cell (now DAY-clustered: n_days, not n_cells)
    best_oos = max(oos_rows, key=lambda r: r["mean_ic_oos"] if r["mean_ic_oos"] is not None else -999)
    print(f"\nBest OOS demean IC (DAY-clustered): w={best_oos['w']}s h={best_oos['h_min']}m "
          f"IC={best_oos['mean_ic_oos']} t={best_oos['t_oos']} (n_days={best_oos['n_days']})", flush=True)

    print("\n=== Done. CSVs saved to", EXP_DIR, "===", flush=True)


if __name__ == "__main__":
    main()
