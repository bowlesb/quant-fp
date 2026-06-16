"""
H2-RETEST: OFI Orthogonal to vwap_dev
Full 20-day, 150-250 liquid symbol panel.
Run via: ops/sandbox.sh "python experiments/2026-06-16-h2-retest-ofi-orthogonal/run_h2_retest.py"
"""

import json
import os
import time
from pathlib import Path

import numpy as np
import polars as pl

STORE = Path("/store/raw")
EXPERIMENT_DIR = Path("/app/experiments/2026-06-16-h2-retest-ofi-orthogonal")
DATA_DIR = EXPERIMENT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# RTH filter: 13:30-19:50 UTC
RTH_START_UTC = 13 * 60 + 30  # minutes since midnight
RTH_END_UTC = 19 * 60 + 50    # exclude 15:50 ET = 19:50 UTC

# Completed days only
EXCLUDE_DATE = "2026-06-16"

FORCE_INCLUDE = {"AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "SPY", "QQQ", "GOOG"}

N_TOP = 250
N_MIN_SYMBOLS_CS = 20
N_SHUFFLE_SEEDS = 10


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def get_available_dates(symbol: str, data_type: str) -> list[str]:
    base = STORE / data_type / f"symbol={symbol}"
    if not base.exists():
        return []
    dates = [d.name.replace("date=", "") for d in base.iterdir() if d.is_dir()]
    dates = [d for d in dates if d != EXCLUDE_DATE]
    return sorted(dates)


def get_quote_symbols() -> set[str]:
    quote_base = STORE / "quotes"
    if not quote_base.exists():
        return set()
    return {d.name.replace("symbol=", "") for d in quote_base.iterdir() if d.is_dir()}


def get_trade_symbols() -> set[str]:
    trade_base = STORE / "trades"
    if not trade_base.exists():
        return set()
    return {d.name.replace("symbol=", "") for d in trade_base.iterdir() if d.is_dir()}


def select_liquid_symbols() -> list[str]:
    log("Selecting liquid symbols...")
    quote_syms = get_quote_symbols()
    trade_syms = get_trade_symbols()
    both = quote_syms & trade_syms
    log(f"  Symbols with both quotes+trades: {len(both)}")

    # Sample a subset for dollar-volume ranking to avoid scanning all 7k bars
    # Use bars for the symbols that have quotes (2504 symbols)
    candidate_syms = list(both)

    # Scan bars for dollar volume estimation - read a few days per symbol
    # For speed, read bars for all candidates using glob
    bars_base = STORE / "bars"
    dollar_vols: dict[str, float] = {}

    # Read bars in bulk using polars with hive partitioning
    log(f"  Reading bars for {len(candidate_syms)} candidate symbols...")

    # Build list of bar files for our candidates
    bar_files = []
    for sym in candidate_syms:
        sym_dir = bars_base / f"symbol={sym}"
        if sym_dir.exists():
            files = list(sym_dir.glob("date=*/data.parquet"))
            files = [f for f in files if EXCLUDE_DATE not in str(f)]
            if files:
                # Sample up to 5 recent files for speed
                bar_files.extend(files[-5:])

    log(f"  Reading {len(bar_files)} bar files for dollar volume...")

    if bar_files:
        # Read in chunks
        chunk_size = 500
        all_dv = []
        for i in range(0, len(bar_files), chunk_size):
            chunk = bar_files[i:i+chunk_size]
            try:
                df = pl.read_parquet(chunk, columns=["symbol", "close", "volume"])
                df = df.with_columns(
                    (pl.col("close") * pl.col("volume")).alias("dv")
                )
                dv = df.group_by("symbol").agg(pl.col("dv").mean().alias("mean_dv"))
                all_dv.append(dv)
            except Exception as exc:
                log(f"  Warning: chunk {i} error: {exc}")

        if all_dv:
            combined = pl.concat(all_dv)
            combined = combined.group_by("symbol").agg(pl.col("mean_dv").mean())
            for row in combined.iter_rows():
                dollar_vols[row[0]] = row[1]

    log(f"  Got dollar volumes for {len(dollar_vols)} symbols")

    # Rank by dollar volume
    ranked = sorted(dollar_vols.items(), key=lambda x: x[1], reverse=True)

    # Take top N_TOP that are in both sets
    top_syms = [sym for sym, _ in ranked if sym in both][:N_TOP]

    # Force-include megacaps
    for sym in FORCE_INCLUDE:
        if sym in both and sym not in top_syms:
            top_syms.append(sym)
            log(f"  Force-included: {sym}")

    log(f"  Selected {len(top_syms)} liquid symbols")
    return top_syms


def compute_ofi_for_symbol_day(sym: str, date: str) -> pl.DataFrame | None:
    """Compute per-minute OFI from quotes for a single symbol-day."""
    fpath = STORE / "quotes" / f"symbol={sym}" / f"date={date}" / "data.parquet"
    if not fpath.exists():
        return None

    try:
        df = pl.read_parquet(fpath, columns=["ts", "bid_price", "bid_size", "ask_price", "ask_size"])
    except Exception:
        return None

    if df.is_empty() or len(df) < 2:
        return None

    # Ensure sorted by time
    df = df.sort("ts")

    # Convert ts to UTC minutes-since-midnight for filtering
    # Cast to Int32 first: dt.hour() returns Int8 which overflows at hour*60 (>127)
    df = df.with_columns(
        pl.col("ts").dt.hour().cast(pl.Int32).alias("hour_utc"),
        pl.col("ts").dt.minute().cast(pl.Int32).alias("min_utc"),
    ).with_columns(
        (pl.col("hour_utc") * 60 + pl.col("min_utc")).alias("min_of_day_utc")
    )

    # Filter to RTH
    df = df.filter(
        (pl.col("min_of_day_utc") >= RTH_START_UTC) &
        (pl.col("min_of_day_utc") < RTH_END_UTC)
    )

    if len(df) < 2:
        return None

    # Floor ts to minute
    df = df.with_columns(
        pl.col("ts").dt.truncate("1m").alias("minute")
    )

    # Compute OFI events using shift
    df = df.with_columns([
        pl.col("bid_price").shift(1).alias("prev_bid_price"),
        pl.col("bid_size").shift(1).alias("prev_bid_size"),
        pl.col("ask_price").shift(1).alias("prev_ask_price"),
        pl.col("ask_size").shift(1).alias("prev_ask_size"),
    ]).slice(1)  # drop first row (no prev)

    # bid_e
    df = df.with_columns(
        pl.when(pl.col("bid_price") > pl.col("prev_bid_price"))
          .then(pl.col("bid_size"))
          .when(pl.col("bid_price") == pl.col("prev_bid_price"))
          .then(pl.col("bid_size") - pl.col("prev_bid_size"))
          .otherwise(-pl.col("prev_bid_size"))
          .alias("bid_e")
    )

    # ask_e (sign convention: buy pressure = positive ask_e when ask price decreases)
    df = df.with_columns(
        pl.when(pl.col("ask_price") < pl.col("prev_ask_price"))
          .then(-pl.col("prev_ask_size"))
          .when(pl.col("ask_price") == pl.col("prev_ask_price"))
          .then(pl.col("ask_size") - pl.col("prev_ask_size"))
          .otherwise(pl.col("ask_size"))
          .alias("ask_e")
    )

    df = df.with_columns(
        (pl.col("bid_e") - pl.col("ask_e")).alias("ofi_event"),
        pl.lit(1).alias("quote_count"),
    )

    # Aggregate per minute
    minute_ofi = df.group_by("minute").agg([
        pl.col("ofi_event").sum().alias("ofi_1m"),
        pl.col("quote_count").sum().alias("n_quotes"),
        # Spread for cost estimation
        ((pl.col("ask_price") - pl.col("bid_price")) /
         ((pl.col("ask_price") + pl.col("bid_price")) / 2.0)).mean().alias("rel_spread_mean"),
    ]).sort("minute")

    minute_ofi = minute_ofi.with_columns([
        pl.lit(sym).alias("symbol"),
        pl.lit(date).alias("date"),
    ])

    return minute_ofi


def compute_sv_for_symbol_day(sym: str, date: str) -> pl.DataFrame | None:
    """Compute per-minute signed volume (tick-rule) from trades."""
    fpath = STORE / "trades" / f"symbol={sym}" / f"date={date}" / "data.parquet"
    if not fpath.exists():
        return None

    try:
        df = pl.read_parquet(fpath, columns=["ts", "price", "size"])
    except Exception:
        return None

    if df.is_empty() or len(df) < 2:
        return None

    df = df.sort("ts")

    # RTH filter (cast to Int32 to avoid Int8 overflow at hour*60)
    df = df.with_columns(
        (pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32)).alias("min_of_day_utc")
    ).filter(
        (pl.col("min_of_day_utc") >= RTH_START_UTC) &
        (pl.col("min_of_day_utc") < RTH_END_UTC)
    )

    if len(df) < 2:
        return None

    df = df.with_columns(
        pl.col("ts").dt.truncate("1m").alias("minute")
    )

    # Tick rule: need prev_price for direction
    df = df.with_columns(
        pl.col("price").shift(1).alias("prev_price")
    )

    # Compute raw direction (ignoring tie-carry across minutes for simplicity)
    df = df.with_columns(
        pl.when(pl.col("price") > pl.col("prev_price")).then(pl.lit(1))
          .when(pl.col("price") < pl.col("prev_price")).then(pl.lit(-1))
          .otherwise(pl.lit(0))  # tie: will forward-fill below
          .alias("dir_raw")
    )

    # Forward fill zeros (carry last direction)
    df = df.with_columns(
        pl.col("dir_raw").replace(0, None).forward_fill().fill_null(1).alias("direction")
    )

    df = df.with_columns(
        (pl.col("direction") * pl.col("size")).alias("signed_vol")
    )

    minute_sv = df.group_by("minute").agg([
        pl.col("signed_vol").sum().alias("sv_1m"),
        pl.col("size").sum().alias("total_vol_1m"),
    ]).sort("minute")

    minute_sv = minute_sv.with_columns([
        pl.lit(sym).alias("symbol"),
        pl.lit(date).alias("date"),
    ])

    return minute_sv


def compute_bars_for_symbol_day(sym: str, date: str) -> pl.DataFrame | None:
    """Compute per-minute bar features: close, vwap, volume."""
    fpath = STORE / "bars" / f"symbol={sym}" / f"date={date}" / "data.parquet"
    if not fpath.exists():
        return None

    try:
        df = pl.read_parquet(fpath, columns=["ts", "close", "vwap", "volume"])
    except Exception:
        return None

    if df.is_empty():
        return None

    df = df.sort("ts")

    # RTH filter (cast to Int32 to avoid Int8 overflow at hour*60)
    df = df.with_columns(
        (pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32)).alias("min_of_day_utc")
    ).filter(
        (pl.col("min_of_day_utc") >= RTH_START_UTC) &
        (pl.col("min_of_day_utc") < RTH_END_UTC)
    )

    if df.is_empty():
        return None

    df = df.with_columns(
        pl.col("ts").dt.truncate("1m").alias("minute"),
        pl.lit(sym).alias("symbol"),
        pl.lit(date).alias("date"),
    ).rename({"ts": "ts_orig"})

    return df.select(["minute", "symbol", "date", "close", "vwap", "volume"])


def process_all_symbols(symbols: list[str]) -> pl.DataFrame:
    """Process all symbols and dates, return merged per-minute panel."""
    log(f"Processing {len(symbols)} symbols...")

    all_frames: list[pl.DataFrame] = []
    total = len(symbols)

    for idx, sym in enumerate(symbols):
        if idx % 25 == 0:
            log(f"  Symbol {idx}/{total}: {sym}")

        # Get available dates for this symbol (need all three data types)
        q_dates = set(get_available_dates(sym, "quotes"))
        t_dates = set(get_available_dates(sym, "trades"))
        b_dates = set(get_available_dates(sym, "bars"))
        dates = sorted(q_dates & t_dates & b_dates)

        if len(dates) < 5:
            continue

        sym_frames: list[pl.DataFrame] = []

        for date in dates:
            ofi_df = compute_ofi_for_symbol_day(sym, date)
            sv_df = compute_sv_for_symbol_day(sym, date)
            bar_df = compute_bars_for_symbol_day(sym, date)

            if ofi_df is None or sv_df is None or bar_df is None:
                continue

            # Merge on minute
            merged = bar_df.join(ofi_df.select(["minute", "ofi_1m", "n_quotes", "rel_spread_mean"]),
                                 on="minute", how="inner")
            merged = merged.join(sv_df.select(["minute", "sv_1m", "total_vol_1m"]),
                                 on="minute", how="inner")

            if not merged.is_empty():
                sym_frames.append(merged)

        if sym_frames:
            sym_df = pl.concat(sym_frames)
            all_frames.append(sym_df)

    if not all_frames:
        raise RuntimeError("No data frames collected!")

    log(f"Concatenating {len(all_frames)} symbol frames...")
    panel = pl.concat(all_frames)
    log(f"Raw panel rows: {len(panel):,}")
    return panel


def add_rolling_features(panel: pl.DataFrame) -> pl.DataFrame:
    """Add rolling OFI, SV, vwap_dev features per symbol-day."""
    log("Computing rolling features...")

    # Sort for rolling
    panel = panel.sort(["symbol", "date", "minute"])

    # Compute rolling within each symbol-date group
    panel = panel.with_columns([
        pl.col("ofi_1m").rolling_sum(window_size=15).over(["symbol", "date"]).alias("ofi_15"),
        pl.col("ofi_1m").rolling_sum(window_size=30).over(["symbol", "date"]).alias("ofi_30"),
        pl.col("n_quotes").rolling_sum(window_size=15).over(["symbol", "date"]).alias("nq_15"),
        pl.col("n_quotes").rolling_sum(window_size=30).over(["symbol", "date"]).alias("nq_30"),
        pl.col("sv_1m").rolling_sum(window_size=15).over(["symbol", "date"]).alias("sv_15"),
        pl.col("sv_1m").rolling_sum(window_size=30).over(["symbol", "date"]).alias("sv_30"),
        pl.col("total_vol_1m").rolling_sum(window_size=15).over(["symbol", "date"]).alias("tvol_15"),
        pl.col("total_vol_1m").rolling_sum(window_size=30).over(["symbol", "date"]).alias("tvol_30"),
        # Rolling VWAP: sum(vwap*volume, 15) / sum(volume, 15)
        (pl.col("vwap") * pl.col("volume")).rolling_sum(window_size=15).over(["symbol", "date"]).alias("dv_15"),
        (pl.col("vwap") * pl.col("volume")).rolling_sum(window_size=30).over(["symbol", "date"]).alias("dv_30"),
        pl.col("volume").rolling_sum(window_size=15).over(["symbol", "date"]).alias("vol_15"),
        pl.col("volume").rolling_sum(window_size=30).over(["symbol", "date"]).alias("vol_30"),
    ])

    # Normalized OFI
    panel = panel.with_columns([
        (pl.col("ofi_15") / (pl.col("nq_15") + 1e-9)).alias("ofi_15_norm"),
        (pl.col("ofi_30") / (pl.col("nq_30") + 1e-9)).alias("ofi_30_norm"),
        (pl.col("sv_15") / (pl.col("tvol_15") + 1e-9)).alias("sv_15_norm"),
        (pl.col("sv_30") / (pl.col("tvol_30") + 1e-9)).alias("sv_30_norm"),
    ])

    # Trailing VWAP
    panel = panel.with_columns([
        (pl.col("dv_15") / (pl.col("vol_15") + 1e-9)).alias("trail_vwap_15"),
        (pl.col("dv_30") / (pl.col("vol_30") + 1e-9)).alias("trail_vwap_30"),
    ])

    # vwap_dev
    panel = panel.with_columns([
        ((pl.col("close") - pl.col("trail_vwap_15")) / (pl.col("trail_vwap_15") + 1e-9)).alias("vwap_dev_15"),
        ((pl.col("close") - pl.col("trail_vwap_30")) / (pl.col("trail_vwap_30") + 1e-9)).alias("vwap_dev_30"),
    ])

    # Forward returns: shift close back by 15 and 30 minutes
    panel = panel.with_columns([
        pl.col("close").shift(-15).over(["symbol", "date"]).alias("fwd_close_15"),
        pl.col("close").shift(-30).over(["symbol", "date"]).alias("fwd_close_30"),
    ])

    panel = panel.with_columns([
        (pl.col("fwd_close_15") / pl.col("close")).log().alias("fwd_ret_15"),
        (pl.col("fwd_close_30") / pl.col("close")).log().alias("fwd_ret_30"),
    ])

    # Drop rows with null rolling features (first 30 bars of each day) or null fwd returns
    panel = panel.drop_nulls(subset=["ofi_30", "sv_30", "vwap_dev_30", "fwd_ret_15", "fwd_ret_30"])

    log(f"Panel after rolling features + null drop: {len(panel):,} rows")
    return panel


def rank_transform(series: np.ndarray) -> np.ndarray:
    """Cross-sectional rank, return [-1, 1] scaled ranks."""
    n = len(series)
    if n == 0:
        return series
    ranks = np.argsort(np.argsort(series)).astype(float)
    return (ranks / (n - 1) * 2 - 1) if n > 1 else ranks


def spearman_ic(signal: np.ndarray, fwd_ret: np.ndarray) -> float:
    """Rank-IC (Spearman) between signal and forward return."""
    mask = np.isfinite(signal) & np.isfinite(fwd_ret)
    if mask.sum() < 5:
        return np.nan
    signal_r = rank_transform(signal[mask])
    fwd_r = rank_transform(fwd_ret[mask])
    if np.std(signal_r) < 1e-10 or np.std(fwd_r) < 1e-10:
        return np.nan
    return float(np.corrcoef(signal_r, fwd_r)[0, 1])


def compute_ic_panel(panel_pd: dict, signal_cols: list[str], fwd_col: str, rng: np.random.Generator | None = None) -> dict[str, list[float]]:
    """Compute per-cross-section ICs for each signal. Returns dict of signal -> list of daily mean ICs."""
    # Group by minute timestamp
    minutes = panel_pd["minute"]
    dates = panel_pd["date"]
    unique_minutes = sorted(set(zip(minutes, dates)))

    # For clustering: group by date
    date_ics: dict[str, dict[str, list[float]]] = {}

    minute_arr = np.array(minutes)
    date_arr = np.array(dates)

    for sig in signal_cols:
        date_ics[sig] = {}

    for (minute, date) in unique_minutes:
        mask = (minute_arr == minute) & (date_arr == date)
        n = mask.sum()
        if n < N_MIN_SYMBOLS_CS:
            continue

        fwd = panel_pd[fwd_col][mask]
        if rng is not None:
            fwd = rng.permutation(fwd)

        for sig in signal_cols:
            sig_vals = panel_pd[sig][mask]
            ic = spearman_ic(sig_vals, fwd)
            if not np.isnan(ic):
                if date not in date_ics[sig]:
                    date_ics[sig][date] = []
                date_ics[sig][date].append(ic)

    # Daily mean ICs
    result: dict[str, list[float]] = {}
    for sig in signal_cols:
        daily_means = [np.mean(ics) for ics in date_ics[sig].values() if ics]
        result[sig] = daily_means

    return result


def day_clustered_tstat(daily_ics: list[float]) -> tuple[float, float, float]:
    """Return (mean_IC, t-stat, n_days)."""
    arr = np.array(daily_ics)
    n = len(arr)
    if n < 2:
        return float(np.mean(arr)) if n == 1 else (0.0, 0.0, 0.0)
    mean_ic = float(np.mean(arr))
    se = float(np.std(arr, ddof=1)) / np.sqrt(n)
    tstat = mean_ic / se if se > 1e-10 else 0.0
    return mean_ic, tstat, float(n)


def residualize_fwd_on_vwap_dev(panel_pd: dict, fwd_col: str, vwap_dev_col: str) -> np.ndarray:
    """Per-minute cross-sectional OLS residual of fwd_ret on vwap_dev."""
    minutes = panel_pd["minute"]
    dates = panel_pd["date"]
    fwd = np.array(panel_pd[fwd_col], dtype=float)
    vdev = np.array(panel_pd[vwap_dev_col], dtype=float)
    resid = fwd.copy()

    minute_arr = np.array(minutes)
    date_arr = np.array(dates)
    unique_minutes = sorted(set(zip(minutes, dates)))

    for (minute, date) in unique_minutes:
        mask = (minute_arr == minute) & (date_arr == date)
        n = mask.sum()
        if n < N_MIN_SYMBOLS_CS:
            continue

        y = fwd[mask]
        x = vdev[mask]
        valid = np.isfinite(y) & np.isfinite(x)
        if valid.sum() < 5:
            continue

        y_v = y[valid]
        x_v = x[valid]
        # OLS: y = a + b*x
        xm = x_v - x_v.mean()
        ym = y_v - y_v.mean()
        if np.dot(xm, xm) < 1e-10:
            continue
        b = np.dot(xm, ym) / np.dot(xm, xm)
        a = y_v.mean() - b * x_v.mean()
        # Residual for the full mask
        indices = np.where(mask)[0]
        valid_full = np.isfinite(fwd[indices]) & np.isfinite(vdev[indices])
        for i_local, i_global in enumerate(indices):
            if valid_full[i_local]:
                resid[i_global] = fwd[i_global] - (a + b * vdev[i_global])

    return resid


def compute_cost_gate(panel_pd: dict, signal_col: str, fwd_col: str) -> dict:
    """Compute decile L-S gross bps and median spread for cost gate."""
    spread = np.array(panel_pd["rel_spread_mean"], dtype=float)
    median_spread_bps = float(np.nanmedian(spread) * 10000)
    one_way_cost_bps = median_spread_bps / 2.0
    roundtrip_cost_bps = one_way_cost_bps * 2.0

    minutes = panel_pd["minute"]
    dates = panel_pd["date"]
    sig = np.array(panel_pd[signal_col], dtype=float)
    fwd = np.array(panel_pd[fwd_col], dtype=float)
    minute_arr = np.array(minutes)
    date_arr = np.array(dates)
    unique_minutes = sorted(set(zip(minutes, dates)))

    top_rets = []
    bot_rets = []

    for (minute, date) in unique_minutes:
        mask = (minute_arr == minute) & (date_arr == date)
        n = mask.sum()
        if n < N_MIN_SYMBOLS_CS:
            continue

        sig_cs = sig[mask]
        fwd_cs = fwd[mask]
        valid = np.isfinite(sig_cs) & np.isfinite(fwd_cs)
        if valid.sum() < N_MIN_SYMBOLS_CS:
            continue

        sig_v = sig_cs[valid]
        fwd_v = fwd_cs[valid]

        n_decile = max(1, len(sig_v) // 10)
        sorted_idx = np.argsort(sig_v)
        bot_ret = float(np.mean(fwd_v[sorted_idx[:n_decile]]))
        top_ret = float(np.mean(fwd_v[sorted_idx[-n_decile:]]))
        top_rets.append(top_ret)
        bot_rets.append(bot_ret)

    gross_ret = float(np.mean(top_rets) - np.mean(bot_rets)) * 10000  # bps
    clears_cost = gross_ret > roundtrip_cost_bps

    return {
        "median_spread_bps": median_spread_bps,
        "one_way_cost_bps": one_way_cost_bps,
        "roundtrip_cost_bps": roundtrip_cost_bps,
        "gross_ls_bps": gross_ret,
        "clears_cost": clears_cost,
    }


def main() -> None:
    t0 = time.time()
    log("=== H2-RETEST: OFI Orthogonal to vwap_dev ===")

    # Step 1: Select symbols
    symbols = select_liquid_symbols()
    log(f"Using {len(symbols)} symbols: {symbols[:10]}...")

    # Save symbol list
    with open(DATA_DIR / "symbols.json", "w") as fp:
        json.dump(symbols, fp)

    # Step 2: Load and process data
    panel = process_all_symbols(symbols)
    panel = add_rolling_features(panel)

    # Save panel for debugging
    panel_path = DATA_DIR / "panel.parquet"
    panel.write_parquet(panel_path)
    log(f"Panel saved to {panel_path}")

    # Convert to numpy dict for IC computation
    log("Converting to numpy for IC computation...")
    panel_pd: dict = {}
    for col in panel.columns:
        panel_pd[col] = panel[col].to_numpy()

    # Report panel stats
    n_syms = len(set(panel_pd["symbol"]))
    n_days = len(set(panel_pd["date"]))
    n_rows = len(panel_pd["minute"])
    log(f"Panel: {n_rows:,} rows, {n_syms} symbols, {n_days} days")

    # Step 3: Compute standalone rank-ICs
    log("Computing standalone rank-ICs...")

    signal_cols = ["ofi_15", "ofi_30", "ofi_15_norm", "ofi_30_norm", "sv_15", "sv_30",
                   "sv_15_norm", "sv_30_norm", "vwap_dev_15", "vwap_dev_30"]

    results: dict = {}

    for horizon, fwd_col in [("H15", "fwd_ret_15"), ("H30", "fwd_ret_30")]:
        log(f"  Horizon {horizon}...")
        daily_ics = compute_ic_panel(panel_pd, signal_cols, fwd_col)
        horizon_results: dict = {}
        for sig in signal_cols:
            mean_ic, tstat, n_days_sig = day_clustered_tstat(daily_ics[sig])
            horizon_results[sig] = {"mean_ic": mean_ic, "tstat": tstat, "n_days": n_days_sig}
            log(f"    {sig}: IC={mean_ic:.4f}, t={tstat:.2f}, n_days={n_days_sig:.0f}")
        results[horizon] = horizon_results

    # Step 4: Canary test (shuffle)
    log("Computing canary shuffle test (10 seeds)...")
    rng_seeds = range(10)
    canary_ics: dict[str, list[float]] = {sig: [] for sig in signal_cols}

    for seed in rng_seeds:
        rng = np.random.default_rng(seed)
        for horizon, fwd_col in [("H15", "fwd_ret_15")]:
            shuf_daily = compute_ic_panel(panel_pd, signal_cols, fwd_col, rng=rng)
            for sig in signal_cols:
                mean_ic_shuf, _, _ = day_clustered_tstat(shuf_daily[sig])
                canary_ics[sig].append(mean_ic_shuf)

    canary_bands: dict[str, tuple[float, float]] = {}
    for sig in signal_cols:
        if canary_ics[sig]:
            lo = float(np.percentile(canary_ics[sig], 2.5))
            hi = float(np.percentile(canary_ics[sig], 97.5))
            canary_bands[sig] = (lo, hi)
            log(f"  Canary {sig}: [{lo:.4f}, {hi:.4f}]")

    results["canary_bands"] = {k: list(v) for k, v in canary_bands.items()}

    # Step 5: Residualized IC (orthogonalized)
    log("Computing residualized ICs...")
    residual_results: dict = {}

    for horizon, fwd_col, vwap_dev_col in [
        ("H15", "fwd_ret_15", "vwap_dev_15"),
        ("H30", "fwd_ret_30", "vwap_dev_30"),
    ]:
        log(f"  Residualizing {fwd_col} on {vwap_dev_col}...")
        resid = residualize_fwd_on_vwap_dev(panel_pd, fwd_col, vwap_dev_col)
        resid_key = f"fwd_ret_{horizon[1:]}_resid"
        panel_pd[resid_key] = resid

        ofi_sigs = ["ofi_15", "ofi_15_norm", "ofi_30", "ofi_30_norm", "sv_15", "sv_15_norm"]
        daily_ics_resid = compute_ic_panel(panel_pd, ofi_sigs, resid_key)
        horizon_resid: dict = {}
        for sig in ofi_sigs:
            mean_ic, tstat, n_days_sig = day_clustered_tstat(daily_ics_resid[sig])
            horizon_resid[sig] = {"mean_ic": mean_ic, "tstat": tstat, "n_days": n_days_sig}
            log(f"    {sig} vs resid@{horizon}: IC={mean_ic:.4f}, t={tstat:.2f}")
        residual_results[horizon] = horizon_resid

    results["residual"] = residual_results

    # Step 6: Cost gate
    log("Computing cost gate...")
    cost_results: dict = {}
    for sig_col, fwd_col, label in [
        ("ofi_15_norm", "fwd_ret_15", "ofi_15_norm_H15"),
        ("ofi_30_norm", "fwd_ret_30", "ofi_30_norm_H30"),
        ("ofi_15", "fwd_ret_15", "ofi_15_H15"),
    ]:
        gate = compute_cost_gate(panel_pd, sig_col, fwd_col)
        cost_results[label] = gate
        log(f"  {label}: gross={gate['gross_ls_bps']:.2f}bps, "
            f"rt_cost={gate['roundtrip_cost_bps']:.2f}bps, "
            f"spread={gate['median_spread_bps']:.2f}bps, "
            f"clears={gate['clears_cost']}")

    results["cost_gate"] = cost_results

    # Save full results
    results_path = DATA_DIR / "results.json"
    with open(results_path, "w") as fp:
        json.dump(results, fp, indent=2, default=float)
    log(f"Results saved to {results_path}")

    elapsed = time.time() - t0
    log(f"=== DONE in {elapsed:.1f}s ===")

    # Print summary
    print("\n=== SUMMARY ===")
    print(f"Panel: {n_rows:,} rows, {n_syms} symbols, {n_days} days")
    print("\n--- Standalone IC ---")
    for horizon in ["H15", "H30"]:
        print(f"\n{horizon}:")
        for sig in ["ofi_15", "ofi_15_norm", "ofi_30_norm", "sv_15", "vwap_dev_15", "vwap_dev_30"]:
            if sig in results[horizon]:
                r = results[horizon][sig]
                cb = canary_bands.get(sig, (None, None))
                clears_canary = "✓" if cb[0] is not None and (r["mean_ic"] > cb[1] or r["mean_ic"] < cb[0]) else "✗"
                print(f"  {sig:20s}: IC={r['mean_ic']:+.4f}, t={r['tstat']:+.2f} {clears_canary}")

    print("\n--- Residualized (marginal over vwap_dev) ---")
    for horizon in ["H15", "H30"]:
        print(f"\n{horizon}:")
        for sig in ["ofi_15", "ofi_15_norm", "ofi_30_norm", "sv_15", "sv_15_norm"]:
            if sig in results["residual"].get(horizon, {}):
                r = results["residual"][horizon][sig]
                print(f"  {sig:20s}: marginal IC={r['mean_ic']:+.4f}, t={r['tstat']:+.2f}")

    print("\n--- Cost Gate ---")
    for label, gate in cost_results.items():
        clears = "CLEARS" if gate["clears_cost"] else "FAILS"
        print(f"  {label}: gross={gate['gross_ls_bps']:.2f}bps vs rt_cost={gate['roundtrip_cost_bps']:.2f}bps [{clears}]")
        print(f"    median_spread={gate['median_spread_bps']:.2f}bps")


if __name__ == "__main__":
    main()
