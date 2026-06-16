"""
H5: Dividend POST-EX Drift — LIQUID TERTILE as PRIMARY gate

Pre-registration: experiments/2026-06-16-h5-dividend-postex-drift/hypothesis.md

The PRIMARY test is the liquid-tertile OOS demeaned t. Full-universe is secondary context only.
A full-universe signal that dies in the liquid tertile = KILL (the H10/H10b illiquid mirage).

Event definition:
  - ex_date from corporate_actions_pit (cash_dividend type)
  - ex_date is look-ahead-safe (data revealed on ex_date <= ts)
  - Entry: D+1 OPEN after ex_date (tradeable; never the ex-date print)
  - Forward return: close[D+1+h] / open[D+1] - 1 for {1,3,5,10} trading days

UTC time handling:
  - bars ts is genuine UTC: 13:30 UTC = 09:30 ET (EDT summer, UTC-4)
  - Open bar: first bar where (utc_hour==13 and utc_minute>=30) OR utc_hour>13
  - Close bar: last bar where utc_hour in [13, 21]
  - NEVER read .hour() off UTC and assume ET — off-by-240 bug kills results (see RESEARCH_PITFALLS.md)

Architecture: adapted from H10b's run_h10b.py.
  - Per-symbol-demean computed WITHIN each split (no leakage across train/OOS)
  - Liquid tertile = top 1/3 of symbols by median daily dollar-volume
  - 10-seed within-date canary (permute event flag)
  - Day-clustered t-stat

Additional: dividend-yield split (high vs low yield tercile by cash_amount/close_ex_date).
"""

import json
import os
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import psycopg

BARS_ROOT = Path("/store/raw/bars")
OUTPUT_DIR = Path("/app/experiments/2026-06-16-h5-dividend-postex-drift")
HORIZONS = [1, 3, 5, 10]
CANARY_SEEDS = 10
RTH_OPEN_UTC_HOUR = 13
RTH_OPEN_UTC_MINUTE = 30
RTH_CLOSE_UTC_HOUR = 21
ROUND_TRIP_BPS = 6


def get_db_connection() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def load_daily_panel_with_open() -> pl.DataFrame:
    """
    Build (symbol, date, close, open_price, dollar_vol) panel from minute bars.
    - close: last bar with UTC hour in [13, 21]
    - open_price: first bar where (utc_hour==13 and utc_minute>=30) OR utc_hour>13
    - dollar_vol: sum(close * volume) over RTH bars for liquidity sorting
    """
    print("Loading daily panel (close + open + dollar_vol) from bars store...")
    all_frames: list[pl.DataFrame] = []

    symbol_dirs = list(BARS_ROOT.iterdir())
    total = len(symbol_dirs)
    print(f"  Found {total} symbol directories")

    for idx, sym_dir in enumerate(symbol_dirs):
        if not sym_dir.is_dir():
            continue
        symbol = sym_dir.name.split("=", 1)[1]

        sym_frames: list[pl.DataFrame] = []
        for date_dir in sym_dir.iterdir():
            if not date_dir.is_dir():
                continue
            parquet_path = date_dir / "data.parquet"
            if not parquet_path.exists():
                continue
            try:
                df = pl.read_parquet(parquet_path, columns=["ts", "close", "volume"])
                sym_frames.append(df)
            except Exception as exc:  # noqa: BLE001
                print(f"  WARNING: failed to read {parquet_path}: {exc}")
                continue

        if not sym_frames:
            continue

        sym_all = pl.concat(sym_frames, rechunk=True)
        sym_all = sym_all.with_columns(
            pl.col("ts").dt.hour().alias("utc_hour"),
            pl.col("ts").dt.minute().alias("utc_minute"),
            pl.col("ts").dt.date().alias("bar_date"),
        ).filter(
            (pl.col("utc_hour") >= RTH_OPEN_UTC_HOUR)
            & (pl.col("utc_hour") <= RTH_CLOSE_UTC_HOUR)
        )

        if sym_all.is_empty():
            continue

        # Mark open-eligible bars: hour==13 and minute>=30, OR hour>13
        sym_all = sym_all.with_columns(
            (
                ((pl.col("utc_hour") == 13) & (pl.col("utc_minute") >= 30))
                | (pl.col("utc_hour") > 13)
            ).alias("is_open_eligible")
        )

        # Close = last bar per date (RTH)
        daily_close = (
            sym_all.sort("ts")
            .group_by("bar_date")
            .agg(pl.col("close").last().alias("close"))
        )

        # Open = first RTH open-eligible bar per date (13:30 UTC = 09:30 ET EDT)
        daily_open = (
            sym_all.filter(pl.col("is_open_eligible"))
            .sort("ts")
            .group_by("bar_date")
            .agg(pl.col("close").first().alias("open_price"))
        )

        # Dollar vol = sum(close * volume) over RTH
        daily_dvol = (
            sym_all.with_columns(
                (pl.col("close") * pl.col("volume")).alias("dollar_bar")
            )
            .group_by("bar_date")
            .agg(pl.col("dollar_bar").sum().alias("dollar_vol"))
        )

        daily = (
            daily_close.join(daily_open, on="bar_date", how="left")
            .join(daily_dvol, on="bar_date", how="left")
            .with_columns(pl.lit(symbol).alias("symbol"))
            .select(["symbol", "bar_date", "close", "open_price", "dollar_vol"])
        )
        all_frames.append(daily)

        if (idx + 1) % 1000 == 0:
            print(f"  Processed {idx + 1}/{total} symbols")

    print(f"  Concatenating {len(all_frames)} symbol frames...")
    panel = pl.concat(all_frames, rechunk=True).sort(["symbol", "bar_date"])
    print(f"  Panel shape: {panel.shape}")
    return panel


def verify_utc_timestamps(panel: pl.DataFrame) -> None:
    """Print a sample to verify UTC handling."""
    print("\n=== UTC TIMESTAMP VERIFICATION ===")
    sample_path = next(BARS_ROOT.glob("symbol=*/date=*/data.parquet"))
    df = pl.read_parquet(sample_path, columns=["ts", "close"])
    df_rth = df.with_columns(
        pl.col("ts").dt.hour().alias("utc_hour"),
        pl.col("ts").dt.minute().alias("utc_minute"),
    ).filter((pl.col("utc_hour") >= 13) & (pl.col("utc_hour") <= 21))
    print(f"Sample path: {sample_path}")
    print("First RTH bar (UTC):", df_rth.head(1).select(["ts", "utc_hour", "utc_minute"]))
    print("Last RTH bar (UTC):", df_rth.tail(1).select(["ts", "utc_hour", "utc_minute"]))
    print("Expected: first bar utc_hour=13, utc_minute>=30 (09:30 ET EDT = 13:30 UTC)")
    print("Panel date range:", panel["bar_date"].min(), "to", panel["bar_date"].max())
    print("=== END VERIFICATION ===\n")


def build_forward_returns(panel: pl.DataFrame, horizons: list[int]) -> pl.DataFrame:
    """
    For each (symbol, date), compute forward returns at each horizon.
    Uses D+1 open as entry (tradeable), exit at close[D+1+h].
    open_fwd_h = close[entry_date + h sessions] / open_price[entry_date] - 1

    The panel is sorted by (symbol, bar_date). The open_price column is the first
    bar's price on that date. We shift close by -h to get the exit price.
    """
    print("Building forward returns (open entry)...")
    result = panel.sort(["symbol", "bar_date"])

    for horizon in horizons:
        # Exit price = close at horizon sessions ahead
        result = result.with_columns(
            (
                pl.col("close").shift(-horizon).over("symbol")
                / pl.col("open_price")
                - 1
            ).alias(f"open_fwd_{horizon}d")
        )

    print(f"  Forward return panel shape: {result.shape}")
    return result


def load_dividends() -> pl.DataFrame:
    """
    Load cash dividends from corporate_actions_pit.
    Returns (symbol, ex_date, cash_amount) — all in-window events.
    ex_date is a DATE column (not timestamp); look-ahead-safe (revealed on ex_date).
    Entry is D+1 after ex_date (we never trade the ex_date itself).
    """
    print("Loading cash dividends from corporate_actions_pit...")
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT symbol, ex_date, cash_amount
        FROM corporate_actions_pit
        WHERE action_type = 'cash_dividend'
          AND ex_date >= '2025-12-14'
          AND ex_date <= '2026-06-15'
        ORDER BY symbol, ex_date
        """
    )
    rows = cur.fetchall()
    conn.close()

    df = pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "ex_date": [r[1] for r in rows],
            "cash_amount": [float(r[2]) if r[2] is not None else float("nan") for r in rows],
        },
        schema={
            "symbol": pl.Utf8,
            "ex_date": pl.Date,
            "cash_amount": pl.Float64,
        },
    )
    print(f"  Loaded {len(df)} cash dividends ({df['symbol'].n_unique()} symbols)")
    return df


def assign_entry_dates(
    dividends: pl.DataFrame, trading_dates: list[date]
) -> pl.DataFrame:
    """
    For each ex_date, find the NEXT trading day as entry date.
    Entry = D+1 after ex_date = the first trading day AFTER the ex_date.
    This ensures we only trade based on information revealed by the ex_date.

    ex_date is a DATE (not a timestamp) — no UTC conversion needed.
    We look for the first trading day AFTER ex_date in our universe.
    """
    trading_dates_set = set(trading_dates)
    trading_dates_sorted = sorted(trading_dates)

    # Build lookup: date -> next trading date
    # For dates that ARE trading days, next means the following trading day.
    # For non-trading dates (weekends/holidays), next is the first following trading day.
    all_dates_to_check: set[date] = set()
    for row in dividends.to_dicts():
        ex_d = row["ex_date"]
        if isinstance(ex_d, datetime):
            ex_d = ex_d.date()
        all_dates_to_check.add(ex_d)

    next_td: dict[date, date | None] = {}
    for ex_d in all_dates_to_check:
        candidate = ex_d + timedelta(days=1)
        found = None
        while True:
            if candidate in trading_dates_set:
                found = candidate
                break
            candidate += timedelta(days=1)
            if candidate > trading_dates_sorted[-1]:
                break
        next_td[ex_d] = found

    rows = dividends.to_dicts()
    entry_dates = []
    for row in rows:
        ex_d = row["ex_date"]
        if isinstance(ex_d, datetime):
            ex_d = ex_d.date()
        entry_dates.append(next_td.get(ex_d))

    result = dividends.with_columns(
        pl.Series("entry_date", entry_dates, dtype=pl.Date)
    )
    result = result.filter(pl.col("entry_date").is_not_null())
    print(f"  Dividends with valid entry date: {len(result)}")
    return result


def compute_liquid_tertile_mask(panel: pl.DataFrame) -> set[str]:
    """
    Return set of symbols in the top 1/3 by median daily dollar-volume.
    This is the PRIMARY filter for H5 — must hold here to be KEEP.
    """
    sym_dvol = (
        panel.group_by("symbol")
        .agg(pl.col("dollar_vol").median().alias("median_dvol"))
        .sort("median_dvol", descending=True)
    )
    n = len(sym_dvol)
    top_n = n // 3
    top_symbols = set(sym_dvol["symbol"].head(top_n).to_list())
    print(f"  Liquid tertile: {len(top_symbols)} / {n} symbols (threshold n={top_n})")
    return top_symbols


def compute_horizon_stats(
    panel: pl.DataFrame,
    fwd_col: str,
    n_canary_seeds: int,
    cost_bps: float = 0.0,
) -> dict[str, Any]:
    """
    Compute day-clustered alpha + t-stat for a single horizon.
    Per-symbol demean computed WITHIN this panel slice (no cross-split leakage).
    Returns raw and demeaned alpha, t-stat, canary p95.
    """
    cost = cost_bps / 10000.0

    dates_with_events = (
        panel.filter(pl.col("is_event"))["bar_date"].unique().to_list()
    )
    dates_with_events_sorted = sorted(dates_with_events)

    # Per-symbol demean WITHIN this slice
    sym_means = panel.group_by("symbol").agg(
        pl.col(fwd_col).mean().alias("sym_mean")
    )
    panel_dm = panel.join(sym_means, on="symbol", how="left").with_columns(
        (pl.col(fwd_col) - pl.col("sym_mean")).alias(f"{fwd_col}_dm")
    )

    alpha_per_date: list[float] = []
    dm_alpha_per_date: list[float] = []
    event_counts: list[int] = []
    control_counts: list[int] = []

    for dt in dates_with_events_sorted:
        day_data = panel.filter(pl.col("bar_date") == dt).filter(
            pl.col(fwd_col).is_not_null()
        )
        day_data_dm = panel_dm.filter(pl.col("bar_date") == dt).filter(
            pl.col(fwd_col).is_not_null()
        )

        event_ret = day_data.filter(pl.col("is_event"))[fwd_col].to_list()
        ctrl_ret = day_data.filter(~pl.col("is_event"))[fwd_col].to_list()
        event_ret_dm = day_data_dm.filter(pl.col("is_event"))[f"{fwd_col}_dm"].to_list()
        ctrl_ret_dm = day_data_dm.filter(~pl.col("is_event"))[f"{fwd_col}_dm"].to_list()

        if not event_ret or not ctrl_ret:
            continue

        # Net of round-trip cost on event side only
        event_ret_net = [r - cost for r in event_ret]
        alpha_per_date.append(float(np.mean(event_ret_net)) - float(np.mean(ctrl_ret)))
        event_counts.append(len(event_ret))
        control_counts.append(len(ctrl_ret))

        if event_ret_dm and ctrl_ret_dm:
            event_dm_net = [r - cost for r in event_ret_dm]
            dm_alpha_per_date.append(
                float(np.mean(event_dm_net)) - float(np.mean(ctrl_ret_dm))
            )

    n_dates = len(alpha_per_date)
    nan = float("nan")

    if n_dates < 5:
        return {
            "n_event_obs": sum(event_counts),
            "n_control_obs": sum(control_counts),
            "n_dates": n_dates,
            "alpha_mean_pct": nan,
            "t_stat": nan,
            "alpha_demean_pct": nan,
            "t_stat_demean": nan,
            "canary_mean_pct": nan,
            "canary_p95_pct": nan,
        }

    alpha_arr = np.array(alpha_per_date)
    alpha_mean = float(np.mean(alpha_arr))
    alpha_std = float(np.std(alpha_arr, ddof=1))
    t_stat = (
        alpha_mean / (alpha_std / np.sqrt(n_dates)) if alpha_std > 0 else nan
    )

    dm_arr = (
        np.array(dm_alpha_per_date) if dm_alpha_per_date else np.array([nan])
    )
    alpha_dm = float(np.mean(dm_arr))
    dm_std = float(np.std(dm_arr, ddof=1)) if len(dm_arr) > 1 else nan
    t_dm = (
        alpha_dm / (dm_std / np.sqrt(len(dm_arr)))
        if (dm_std and dm_std > 0 and len(dm_arr) > 1)
        else nan
    )

    # Canary: permute event flags within each date
    canary_alphas: list[float] = []
    rng = random.Random(42)
    for seed in range(n_canary_seeds):
        rng.seed(seed)
        perm_per_date: list[float] = []
        for dt in dates_with_events_sorted:
            day_data = panel.filter(pl.col("bar_date") == dt).filter(
                pl.col(fwd_col).is_not_null()
            )
            n_ev = int(day_data.filter(pl.col("is_event")).height)
            if n_ev == 0:
                continue
            all_ret = day_data[fwd_col].to_list()
            if len(all_ret) < 2:
                continue
            shuffled = all_ret.copy()
            rng.shuffle(shuffled)
            perm_ev = shuffled[:n_ev]
            perm_ctrl_vals = shuffled[n_ev:]
            if not perm_ctrl_vals:
                continue
            perm_per_date.append(
                float(np.mean(perm_ev)) - float(np.mean(perm_ctrl_vals))
            )
        if perm_per_date:
            canary_alphas.append(float(np.mean(perm_per_date)))

    canary_mean = float(np.mean(canary_alphas)) if canary_alphas else nan
    canary_p95 = float(np.percentile(canary_alphas, 95)) if canary_alphas else nan

    return {
        "n_event_obs": sum(event_counts),
        "n_control_obs": sum(control_counts),
        "n_dates": n_dates,
        "alpha_mean_pct": alpha_mean * 100,
        "t_stat": t_stat,
        "alpha_demean_pct": alpha_dm * 100,
        "t_stat_demean": t_dm,
        "canary_mean_pct": canary_mean * 100,
        "canary_p95_pct": canary_p95 * 100,
    }


def run_analysis(
    label: str,
    fwd_panel: pl.DataFrame,
    event_df: pl.DataFrame,
    horizons: list[int],
    split_dates: tuple[list[date], list[date]] | None = None,
    liquid_symbols: set[str] | None = None,
    cost_bps: float = 0.0,
) -> dict[str, Any]:
    """
    Run cohort analysis for a given subset/configuration.
    split_dates: (train_dates, oos_dates) for walk-forward; None for full period.
    liquid_symbols: restrict universe to this set if provided.
    """
    print(f"\n--- {label} ---")

    # Build event join: (symbol, entry_date) → mark as event on that date
    event_entries = event_df.select(["symbol", "entry_date"]).unique(
        subset=["symbol", "entry_date"]
    )
    fwd_with_event = fwd_panel.join(
        event_entries.rename({"entry_date": "bar_date"}).with_columns(
            pl.lit(True).alias("is_event")
        ),
        on=["symbol", "bar_date"],
        how="left",
    ).with_columns(pl.col("is_event").fill_null(False))

    if liquid_symbols is not None:
        fwd_with_event = fwd_with_event.filter(
            pl.col("symbol").is_in(list(liquid_symbols))
        )

    results: dict[str, Any] = {"label": label}

    for horizon in horizons:
        fwd_col = f"open_fwd_{horizon}d"
        panel_h = fwd_with_event.filter(pl.col(fwd_col).is_not_null())

        if split_dates is not None:
            train_dates, oos_dates = split_dates
            train_set = set(train_dates)
            oos_set = set(oos_dates)

            train_panel = panel_h.filter(pl.col("bar_date").is_in(list(train_set)))
            oos_panel = panel_h.filter(pl.col("bar_date").is_in(list(oos_set)))

            train_stats = compute_horizon_stats(
                train_panel, fwd_col, CANARY_SEEDS, cost_bps
            )
            oos_stats = compute_horizon_stats(
                oos_panel, fwd_col, CANARY_SEEDS, cost_bps
            )

            print(
                f"  h={horizon}d TRAIN: n={train_stats['n_event_obs']}, "
                f"alpha_dm={train_stats['alpha_demean_pct']:.3f}%, "
                f"t_dm={train_stats['t_stat_demean']:.2f}"
            )
            print(
                f"  h={horizon}d OOS:   n={oos_stats['n_event_obs']}, "
                f"alpha_dm={oos_stats['alpha_demean_pct']:.3f}%, "
                f"t_dm={oos_stats['t_stat_demean']:.2f}"
            )

            results[f"horizon_{horizon}d"] = {
                "train": train_stats,
                "oos": oos_stats,
            }
        else:
            full_stats = compute_horizon_stats(
                panel_h, fwd_col, CANARY_SEEDS, cost_bps
            )
            print(
                f"  h={horizon}d: n={full_stats['n_event_obs']}, "
                f"alpha_dm={full_stats['alpha_demean_pct']:.3f}%, "
                f"t_dm={full_stats['t_stat_demean']:.2f}"
            )
            results[f"horizon_{horizon}d"] = full_stats

    return results


def compute_yield_split(
    dividends: pl.DataFrame, panel: pl.DataFrame
) -> pl.DataFrame:
    """
    For each dividend event, compute approximate yield = cash_amount / close_on_ex_date.
    Then split into terciles. Returns dividends with yield tercile label.
    """
    # Get close price on ex_date for yield computation
    close_on_ex = panel.select(["symbol", "bar_date", "close"]).rename(
        {"bar_date": "ex_date", "close": "ex_close"}
    )
    divs_with_yield = dividends.join(close_on_ex, on=["symbol", "ex_date"], how="left")
    divs_with_yield = divs_with_yield.with_columns(
        (pl.col("cash_amount") / pl.col("ex_close")).alias("div_yield")
    ).filter(pl.col("div_yield").is_not_null() & pl.col("div_yield").is_finite())

    # Compute tercile thresholds
    yield_vals = divs_with_yield["div_yield"].drop_nulls()
    p33 = float(yield_vals.quantile(0.333))
    p67 = float(yield_vals.quantile(0.667))

    divs_with_yield = divs_with_yield.with_columns(
        pl.when(pl.col("div_yield") < p33)
        .then(pl.lit("low_yield"))
        .when(pl.col("div_yield") < p67)
        .then(pl.lit("mid_yield"))
        .otherwise(pl.lit("high_yield"))
        .alias("yield_tercile")
    )

    n_total = len(divs_with_yield)
    for tercile in ["low_yield", "mid_yield", "high_yield"]:
        cnt = divs_with_yield.filter(pl.col("yield_tercile") == tercile).height
        print(f"  Yield tercile {tercile}: {cnt} / {n_total} events")

    return divs_with_yield


def main() -> None:
    print("=== H5: Dividend POST-EX Drift (LIQUID TERTILE as PRIMARY gate) ===\n")

    # 1. Load panel
    panel = load_daily_panel_with_open()
    verify_utc_timestamps(panel)

    trading_dates_all: list[date] = sorted(panel["bar_date"].unique().to_list())
    n_dates = len(trading_dates_all)
    print(
        f"Trading date range: {trading_dates_all[0]} to {trading_dates_all[-1]} "
        f"({n_dates} dates)"
    )

    # 2. Build forward returns (open entry)
    fwd_panel = build_forward_returns(panel, HORIZONS)

    # 3. Load dividends and assign entry dates
    dividends = load_dividends()
    dividends = assign_entry_dates(dividends, trading_dates_all)
    bar_symbols = set(panel["symbol"].unique().to_list())
    dividends = dividends.filter(pl.col("symbol").is_in(list(bar_symbols)))
    print(f"Dividends with matching bar symbols: {len(dividends)} ({dividends['symbol'].n_unique()} symbols)")

    # 4. Train/OOS split — split_idx at ~63 days each
    split_idx = n_dates // 2
    train_dates = trading_dates_all[:split_idx]
    oos_dates = trading_dates_all[split_idx:]
    print(f"\nSplit: TRAIN={train_dates[0]} to {train_dates[-1]} ({len(train_dates)} days)")
    print(f"       OOS={oos_dates[0]} to {oos_dates[-1]} ({len(oos_dates)} days)")

    # 5. Liquid tertile (PRIMARY gate)
    liquid_symbols = compute_liquid_tertile_mask(panel)

    # 6. Run all analyses
    all_results: dict[str, Any] = {}

    # 6a. PRIMARY: Liquid-tertile walk-forward OOS (THE headline number)
    all_results["PRIMARY_liquid_wf"] = run_analysis(
        "PRIMARY: Liquid-tertile WF OOS (top 1/3 by dollar-vol)",
        fwd_panel,
        dividends,
        HORIZONS,
        split_dates=(train_dates, oos_dates),
        liquid_symbols=liquid_symbols,
        cost_bps=ROUND_TRIP_BPS,
    )

    # 6b. Full-universe walk-forward OOS (secondary/context)
    all_results["full_universe_wf"] = run_analysis(
        "Full-universe WF OOS (context only)",
        fwd_panel,
        dividends,
        HORIZONS,
        split_dates=(train_dates, oos_dates),
        cost_bps=ROUND_TRIP_BPS,
    )

    # 6c. Full-universe full-period (no split — context/benchmark)
    all_results["full_universe_full"] = run_analysis(
        "Full-universe full-period (no split, context only)",
        fwd_panel,
        dividends,
        HORIZONS,
        split_dates=None,
        cost_bps=0.0,  # no cost for context view
    )

    # 6d. Dividend yield split — is the drift concentrated in high-yield events?
    print("\n--- Dividend yield split ---")
    divs_with_yield = compute_yield_split(dividends, panel)

    for tercile_label in ["high_yield", "mid_yield", "low_yield"]:
        tercile_divs = divs_with_yield.filter(pl.col("yield_tercile") == tercile_label)
        if len(tercile_divs) < 20:
            print(f"  Skipping {tercile_label} (too few events: {len(tercile_divs)})")
            continue

        # Liquid-tertile OOS for this yield tercile
        key_liquid = f"yield_{tercile_label}_liquid_oos"
        all_results[key_liquid] = run_analysis(
            f"Yield {tercile_label} / liquid-tertile WF OOS",
            fwd_panel,
            tercile_divs,
            HORIZONS,
            split_dates=(train_dates, oos_dates),
            liquid_symbols=liquid_symbols,
            cost_bps=ROUND_TRIP_BPS,
        )

        # Full-universe OOS for this yield tercile (context)
        key_full = f"yield_{tercile_label}_full_oos"
        all_results[key_full] = run_analysis(
            f"Yield {tercile_label} / full-universe WF OOS",
            fwd_panel,
            tercile_divs,
            HORIZONS,
            split_dates=(train_dates, oos_dates),
            cost_bps=ROUND_TRIP_BPS,
        )

    # 7. Save raw results
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results_path = OUTPUT_DIR / "raw_results_h5.json"
    with open(results_path, "w") as json_file:
        json.dump(all_results, json_file, indent=2, default=str)
    print(f"\nRaw results saved to {results_path}")

    # 8. Print summary
    print("\n" + "=" * 70)
    print("PRIMARY RESULT: Liquid-tertile OOS (THE headline for H5 verdict)")
    print("=" * 70)
    primary = all_results["PRIMARY_liquid_wf"]
    for horizon in HORIZONS:
        key = f"horizon_{horizon}d"
        if key not in primary:
            continue
        oos_s = primary[key]["oos"]
        train_s = primary[key]["train"]
        print(
            f"  {horizon}d | TRAIN: n={train_s['n_event_obs']:5d}, "
            f"dm%={train_s['alpha_demean_pct']:+.3f}, t_dm={train_s['t_stat_demean']:+.2f} | "
            f"OOS: n={oos_s['n_event_obs']:5d}, "
            f"dm%={oos_s['alpha_demean_pct']:+.3f}, t_dm={oos_s['t_stat_demean']:+.2f}"
        )

    print("\nFULL-UNIVERSE OOS (secondary context):")
    full_wf = all_results["full_universe_wf"]
    for horizon in HORIZONS:
        key = f"horizon_{horizon}d"
        if key not in full_wf:
            continue
        oos_s = full_wf[key]["oos"]
        print(
            f"  {horizon}d | OOS: n={oos_s['n_event_obs']:5d}, "
            f"dm%={oos_s['alpha_demean_pct']:+.3f}, t_dm={oos_s['t_stat_demean']:+.2f}"
        )

    # Verdict hint
    liquid_oos_ts = []
    for horizon in HORIZONS:
        key = f"horizon_{horizon}d"
        if key in primary and "oos" in primary[key]:
            t_dm = primary[key]["oos"].get("t_stat_demean", float("nan"))
            liquid_oos_ts.append((horizon, t_dm))

    best_liquid_t = max(
        (abs(t) for _, t in liquid_oos_ts if not np.isnan(t)), default=float("nan")
    )
    liquid_clears = any(t >= 2.0 for _, t in liquid_oos_ts if not np.isnan(t))

    print(f"\nLIQUID-TERTILE OOS demeaned t >= 2.0: {'YES' if liquid_clears else 'NO'} (best |t|={best_liquid_t:.2f})")
    print(f"Pre-reg verdict criterion: KEEP needs liquid t>=2 at >=1 horizon net 6bps OOS")
    print(f"Preliminary verdict: {'KEEP-AS-LEAD' if liquid_clears else 'KILL (liquid t<2.0 — same illiquid mirage as H10)'}")

    print("\nDone.")


if __name__ == "__main__":
    main()
