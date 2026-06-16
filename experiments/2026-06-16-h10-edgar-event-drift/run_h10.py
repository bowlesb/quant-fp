"""
H10: EDGAR 8-K / Form-4 Event Drift
Pre-registered hypothesis: event cohorts show multi-day forward-return
separation vs matched same-date non-event controls.

Time handling: all ts in bars are genuine UTC.
  - 09:30 ET = 13:30 UTC (EST, Nov-Mar)
  - 09:30 ET = 13:30 UTC (EST) / 14:30 UTC (EDT, Mar-Nov)
  - June dates: EDT → 09:30 ET = 13:30 UTC, 16:00 ET = 20:00 UTC
  - BUT we use the actual bar timestamps and pick the last bar of the RTH
    session (14:30-21:00 UTC covers both EDT and EST windows safely).
  - RTH filter: 13:30 <= ts.hour_utc <= 21:00 (covers EST and EDT open)

Entry rule: filing available_at determines event date.
  - We assign the EVENT ENTRY date as the NEXT calendar date after
    available_at.date() (conservative: never same-day entry).
  - Then we find that date's RTH close price (last bar with UTC hour >= 13:30
    and <= 21:00). If no bar exists for that date, skip forward to next
    available date.

Forward return: close-to-close over {1, 3, 5, 10} trading days.
  - We build a daily-close panel from minute bars for each symbol.
  - Return at horizon h = close(entry + h sessions) / close(entry) - 1.

Control: all other symbols in the universe on the same entry date
  (same-date cross-section), after excluding event symbols for that date.

Metrics:
  - Mean event cohort return minus mean control return per date, then
    averaged across dates (cross-sectional alpha).
  - Day-clustered t-statistic (using date as cluster).
  - 10-seed canary: permute event flag within each date, compute same metric.
  - Per-symbol-demean: subtract each symbol's own mean return across all
    dates before computing the cross-sectional alpha.
"""

import os
import json
import random
import statistics
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import psycopg

BARS_ROOT = Path("/store/raw/bars")
OUTPUT_DIR = Path("/app/experiments/2026-06-16-h10-edgar-event-drift")
HORIZONS = [1, 3, 5, 10]
CANARY_SEEDS = 10
RTH_START_UTC_HOUR = 13  # 13:30 UTC covers both EDT (09:30 ET) and EST (08:30 ET); we use >= 13:30
RTH_END_UTC_HOUR = 21    # 21:00 UTC = 17:00 ET (covers extended close)
# RTH last bar: we want the last bar where hour <= 21 and minute <= 0 for 16:00 close
# Actually: 16:00 ET = 20:00 UTC (EDT) or 21:00 UTC (EST). We use last bar with UTC hour in [13,21] inclusive.


def get_db_connection() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def load_daily_close_panel() -> pl.DataFrame:
    """
    Build a (symbol, date, close) panel from minute bars.
    For each (symbol, date), take the LAST bar with UTC hour in [13, 21]
    (i.e., within the RTH + a little after-close buffer).
    This is the close price for that trading session.
    """
    print("Loading daily close panel from bars store...")
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
                df = pl.read_parquet(parquet_path, columns=["ts", "close"])
                sym_frames.append(df)
            except Exception as exc:  # noqa: BLE001
                print(f"  WARNING: failed to read {parquet_path}: {exc}")
                continue

        if not sym_frames:
            continue

        sym_all = pl.concat(sym_frames, rechunk=True)

        # Filter to RTH hours (UTC 13–21 inclusive)
        sym_all = sym_all.with_columns(
            pl.col("ts").dt.hour().alias("utc_hour"),
            pl.col("ts").dt.date().alias("bar_date"),
        ).filter((pl.col("utc_hour") >= 13) & (pl.col("utc_hour") <= 21))

        if sym_all.is_empty():
            continue

        # Last bar per date = close price
        daily = (
            sym_all.sort("ts")
            .group_by("bar_date")
            .agg(pl.col("close").last().alias("close"))
            .with_columns(pl.lit(symbol).alias("symbol"))
            .select(["symbol", "bar_date", "close"])
        )
        all_frames.append(daily)

        if (idx + 1) % 1000 == 0:
            print(f"  Processed {idx + 1}/{total} symbols")

    print(f"  Concatenating {len(all_frames)} symbol frames...")
    panel = pl.concat(all_frames, rechunk=True).sort(["symbol", "bar_date"])
    print(f"  Panel shape: {panel.shape}")
    return panel


def build_forward_returns(panel: pl.DataFrame, horizons: list[int]) -> pl.DataFrame:
    """
    For each (symbol, date), compute forward returns at each horizon.
    Forward return at horizon h = close[t+h] / close[t] - 1
    where t+h means h trading days later (trading days = dates in the panel
    for that symbol).
    Uses polars shift within each symbol group.
    """
    print("Building forward returns...")

    result = panel.sort(["symbol", "bar_date"])

    for horizon in horizons:
        result = result.with_columns(
            (
                pl.col("close").shift(-horizon).over("symbol") / pl.col("close") - 1
            ).alias(f"fwd_{horizon}d")
        )

    print(f"  Forward return panel shape: {result.shape}")
    return result


def load_filings(form_types: list[str]) -> pl.DataFrame:
    """
    Load filings from timescaledb for the given form types.
    Returns (symbol, form_type, available_at_date) where available_at_date
    is the UTC date of the filing's available_at timestamp.
    """
    print(f"Loading filings for form types: {form_types}...")
    conn = get_db_connection()
    cur = conn.cursor()

    placeholders = ",".join(["%s"] * len(form_types))
    cur.execute(
        f"""
        SELECT symbol, form_type,
               available_at::date AS available_at_date,
               available_at,
               available_at_source
        FROM filings
        WHERE form_type IN ({placeholders})
          AND available_at >= '2025-12-15'
        ORDER BY symbol, available_at
        """,
        form_types,
    )
    rows = cur.fetchall()
    conn.close()

    df = pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "form_type": [r[1] for r in rows],
            "available_at_date": [r[2] for r in rows],
            "available_at": [r[3] for r in rows],
            "available_at_source": [r[4] for r in rows],
        }
    )
    print(f"  Loaded {len(df)} filings ({len(df['symbol'].unique())} symbols)")
    return df


def assign_entry_dates(
    filings: pl.DataFrame, trading_dates: list[date]
) -> pl.DataFrame:
    """
    For each filing, assign the NEXT TRADEABLE ENTRY DATE.
    Conservative rule: entry date = next trading day AFTER available_at.date().
    (We never enter on the same calendar day to avoid look-ahead.)

    available_at is UTC. We take the UTC date as the "event date" and
    find the next date in trading_dates.
    """
    trading_dates_set = set(trading_dates)
    trading_dates_sorted = sorted(trading_dates)

    # Build a lookup: date -> next trading date
    next_td: dict[date, date | None] = {}
    for dt in trading_dates_sorted:
        # Find the next trading date after dt
        candidate = dt + timedelta(days=1)
        while True:
            if candidate in trading_dates_set:
                next_td[dt] = candidate
                break
            candidate += timedelta(days=1)
            if candidate > trading_dates_sorted[-1]:
                next_td[dt] = None
                break

    rows = filings.to_dicts()
    entry_dates = []
    for row in rows:
        avail_date = row["available_at_date"]
        # avail_date is a date object from psycopg
        if isinstance(avail_date, datetime):
            avail_date = avail_date.date()
        entry = next_td.get(avail_date)
        entry_dates.append(entry)

    result = filings.with_columns(
        pl.Series("entry_date", entry_dates, dtype=pl.Date)
    )
    # Drop filings where entry_date is null or not in trading_dates_set
    result = result.filter(pl.col("entry_date").is_not_null())
    print(f"  Filings with valid entry date: {len(result)}")
    return result


def compute_cohort_stats(
    fwd_panel: pl.DataFrame,
    event_df: pl.DataFrame,
    form_type_label: str,
    horizons: list[int],
    n_canary_seeds: int = 10,
) -> dict[str, Any]:
    """
    Compute cohort-minus-control forward return statistics.

    For each horizon h:
    1. For each event (symbol, entry_date), get fwd_h return.
    2. For each date d with events, compute:
       - event_mean = mean(fwd_h for event symbols on date d)
       - control_mean = mean(fwd_h for non-event symbols on date d with valid fwd_h)
       - alpha_d = event_mean - control_mean
    3. Overall alpha = mean(alpha_d across dates)
    4. Day-clustered t = mean(alpha_d) / (std(alpha_d) / sqrt(n_dates))
    5. Canary: permute event flags within each date, repeat 10 seeds.
    6. Per-symbol-demean: subtract each symbol's own mean return, then repeat.
    """
    print(f"\nComputing cohort stats for {form_type_label}...")

    # Build event set: (symbol, date) -> True
    # Take the FIRST filing per (symbol, entry_date) to avoid double-counting
    event_entries = (
        event_df.select(["symbol", "entry_date"])
        .unique(subset=["symbol", "entry_date"])
    )

    # Join event symbols to fwd_panel
    fwd_with_event = fwd_panel.join(
        event_entries.rename({"entry_date": "bar_date"}).with_columns(
            pl.lit(True).alias("is_event")
        ),
        on=["symbol", "bar_date"],
        how="left",
    ).with_columns(
        pl.col("is_event").fill_null(False)
    )

    results: dict[str, Any] = {"form_type": form_type_label}

    for horizon in horizons:
        fwd_col = f"fwd_{horizon}d"
        panel_h = fwd_with_event.filter(pl.col(fwd_col).is_not_null())

        horizon_results = _compute_horizon_stats(
            panel_h, fwd_col, horizon, n_canary_seeds
        )
        results[f"horizon_{horizon}d"] = horizon_results

    return results


def _compute_horizon_stats(
    panel: pl.DataFrame,
    fwd_col: str,
    horizon: int,
    n_canary_seeds: int,
) -> dict[str, Any]:
    """Compute statistics for a single horizon."""

    # Dates with at least one event and at least one control
    dates_with_events = (
        panel.filter(pl.col("is_event"))
        ["bar_date"]
        .unique()
        .to_list()
    )

    alpha_per_date: list[float] = []
    event_counts: list[int] = []
    control_counts: list[int] = []

    for dt in sorted(dates_with_events):
        day_data = panel.filter(pl.col("bar_date") == dt)
        event_returns = day_data.filter(pl.col("is_event"))[fwd_col].to_list()
        ctrl_returns = day_data.filter(~pl.col("is_event"))[fwd_col].to_list()

        if len(event_returns) == 0 or len(ctrl_returns) == 0:
            continue

        alpha = float(np.mean(event_returns)) - float(np.mean(ctrl_returns))
        alpha_per_date.append(alpha)
        event_counts.append(len(event_returns))
        control_counts.append(len(ctrl_returns))

    n_dates = len(alpha_per_date)
    if n_dates < 2:
        return {
            "n_event_obs": sum(event_counts),
            "n_dates": n_dates,
            "alpha_mean": float("nan"),
            "t_stat": float("nan"),
            "canary_mean": float("nan"),
            "canary_p95": float("nan"),
            "alpha_demean": float("nan"),
            "t_stat_demean": float("nan"),
        }

    alpha_arr = np.array(alpha_per_date)
    alpha_mean = float(np.mean(alpha_arr))
    alpha_std = float(np.std(alpha_arr, ddof=1))
    t_stat = alpha_mean / (alpha_std / np.sqrt(n_dates)) if alpha_std > 0 else float("nan")

    # Canary: permute event flags within each date
    canary_alphas: list[float] = []
    rng = random.Random(42)
    for seed in range(n_canary_seeds):
        rng.seed(seed)
        perm_alpha_per_date: list[float] = []
        for dt in sorted(dates_with_events):
            day_data = panel.filter(pl.col("bar_date") == dt)
            n_events_today = int(day_data.filter(pl.col("is_event")).height)
            if n_events_today == 0:
                continue
            all_returns = day_data[fwd_col].to_list()
            if len(all_returns) < 2:
                continue
            shuffled = all_returns.copy()
            rng.shuffle(shuffled)
            perm_event = shuffled[:n_events_today]
            perm_ctrl = shuffled[n_events_today:]
            if not perm_ctrl:
                continue
            perm_alpha_per_date.append(
                float(np.mean(perm_event)) - float(np.mean(perm_ctrl))
            )
        if perm_alpha_per_date:
            canary_alphas.append(float(np.mean(perm_alpha_per_date)))

    canary_mean = float(np.mean(canary_alphas)) if canary_alphas else float("nan")
    canary_p95 = float(np.percentile(canary_alphas, 95)) if canary_alphas else float("nan")
    canary_p5 = float(np.percentile(canary_alphas, 5)) if canary_alphas else float("nan")

    # Per-symbol-demean: subtract each symbol's mean return across all dates
    sym_means = (
        panel.group_by("symbol")
        .agg(pl.col(fwd_col).mean().alias("sym_mean"))
    )
    panel_dm = panel.join(sym_means, on="symbol", how="left").with_columns(
        (pl.col(fwd_col) - pl.col("sym_mean")).alias(f"{fwd_col}_dm")
    )

    dm_alpha_per_date: list[float] = []
    for dt in sorted(dates_with_events):
        day_data = panel_dm.filter(pl.col("bar_date") == dt)
        event_returns_dm = day_data.filter(pl.col("is_event"))[f"{fwd_col}_dm"].to_list()
        ctrl_returns_dm = day_data.filter(~pl.col("is_event"))[f"{fwd_col}_dm"].to_list()
        if not event_returns_dm or not ctrl_returns_dm:
            continue
        dm_alpha_per_date.append(
            float(np.mean(event_returns_dm)) - float(np.mean(ctrl_returns_dm))
        )

    dm_arr = np.array(dm_alpha_per_date) if dm_alpha_per_date else np.array([float("nan")])
    alpha_dm_mean = float(np.mean(dm_arr))
    dm_std = float(np.std(dm_arr, ddof=1)) if len(dm_arr) > 1 else float("nan")
    t_stat_dm = (
        alpha_dm_mean / (dm_std / np.sqrt(len(dm_arr)))
        if dm_std and dm_std > 0 and len(dm_arr) > 1
        else float("nan")
    )

    print(
        f"  horizon={horizon}d: n_event_obs={sum(event_counts)}, n_dates={n_dates}, "
        f"alpha={alpha_mean*100:.3f}%, t={t_stat:.2f}, "
        f"canary_p95={canary_p95*100:.3f}%, alpha_dm={alpha_dm_mean*100:.3f}%, t_dm={t_stat_dm:.2f}"
    )

    return {
        "n_event_obs": sum(event_counts),
        "n_dates": n_dates,
        "mean_event_per_date": float(np.mean(event_counts)),
        "mean_ctrl_per_date": float(np.mean(control_counts)),
        "alpha_mean_pct": alpha_mean * 100,
        "alpha_std_pct": alpha_std * 100,
        "t_stat": t_stat,
        "canary_mean_pct": canary_mean * 100,
        "canary_p5_pct": canary_p5 * 100,
        "canary_p95_pct": canary_p95 * 100,
        "clears_canary": (
            abs(alpha_mean) > abs(canary_p95)
            if alpha_mean > 0
            else alpha_mean < canary_p5
        ),
        "alpha_demean_pct": alpha_dm_mean * 100,
        "t_stat_demean": t_stat_dm,
    }


def verify_utc_timestamps() -> None:
    """Print a few raw bars to verify UTC handling before main run."""
    print("\n=== UTC TIMESTAMP VERIFICATION ===")
    sample_path = next(BARS_ROOT.glob("symbol=*/date=*/data.parquet"))
    df = pl.read_parquet(sample_path, columns=["ts", "close"])
    df_rth = df.with_columns(pl.col("ts").dt.hour().alias("utc_hour")).filter(
        (pl.col("utc_hour") >= 13) & (pl.col("utc_hour") <= 21)
    )
    print(f"Sample path: {sample_path}")
    print("First RTH bar (UTC):", df_rth.head(1))
    print("Last RTH bar (UTC):", df_rth.tail(1))
    print("Note: 13:30 UTC = 09:30 ET (EDT) or 08:30 ET (EST). 21:00 UTC = 17:00 ET (EDT).")
    print("We use UTC hours 13-21 to safely capture the full RTH session in both timezones.")
    print("=== END VERIFICATION ===\n")


def format_results_table(all_stats: list[dict[str, Any]]) -> str:
    """Format results as a markdown table."""
    lines = []
    for stat in all_stats:
        ft = stat["form_type"]
        lines.append(f"\n### {ft} Forward Return: Cohort minus Control\n")
        lines.append("| Horizon | N_obs | N_dates | Alpha% | t-stat | Canary_p95% | Clears? | Alpha_dm% | t_dm |")
        lines.append("|---------|-------|---------|--------|--------|-------------|---------|-----------|------|")
        for horizon in HORIZONS:
            key = f"horizon_{horizon}d"
            if key not in stat:
                continue
            h = stat[key]
            clears = "YES" if h.get("clears_canary") else "NO"
            lines.append(
                f"| {horizon}d | {h['n_event_obs']} | {h['n_dates']} | "
                f"{h['alpha_mean_pct']:.3f} | {h['t_stat']:.2f} | "
                f"{h['canary_p95_pct']:.3f} | {clears} | "
                f"{h['alpha_demean_pct']:.3f} | {h['t_stat_demean']:.2f} |"
            )
    return "\n".join(lines)


def main() -> None:
    verify_utc_timestamps()

    # 1. Load daily close panel
    panel = load_daily_close_panel()

    # Get sorted list of all trading dates in the universe
    trading_dates_all: list[date] = sorted(panel["bar_date"].unique().to_list())
    print(f"Trading date range: {trading_dates_all[0]} to {trading_dates_all[-1]} ({len(trading_dates_all)} dates)")

    # 2. Build forward returns
    fwd_panel = build_forward_returns(panel, HORIZONS)

    # 3. Load filings
    filings_8k = load_filings(["8-K"])
    filings_f4 = load_filings(["4"])

    # 4. Assign entry dates
    filings_8k_entries = assign_entry_dates(filings_8k, trading_dates_all)
    filings_f4_entries = assign_entry_dates(filings_f4, trading_dates_all)

    # Filter to symbols that exist in the bars panel
    bar_symbols = set(panel["symbol"].unique().to_list())
    filings_8k_entries = filings_8k_entries.filter(pl.col("symbol").is_in(list(bar_symbols)))
    filings_f4_entries = filings_f4_entries.filter(pl.col("symbol").is_in(list(bar_symbols)))

    print(f"\n8-K filings with matching bars: {len(filings_8k_entries)} ({filings_8k_entries['symbol'].n_unique()} symbols)")
    print(f"Form-4 filings with matching bars: {len(filings_f4_entries)} ({filings_f4_entries['symbol'].n_unique()} symbols)")

    # Print available_at_source breakdown
    for ft_label, ft_df in [("8-K", filings_8k_entries), ("Form-4", filings_f4_entries)]:
        src_counts = ft_df.group_by("available_at_source").agg(pl.len().alias("count"))
        print(f"{ft_label} available_at_source: {src_counts.to_dicts()}")

    # 5. Compute cohort stats
    stats_8k = compute_cohort_stats(
        fwd_panel, filings_8k_entries, "8-K", HORIZONS, CANARY_SEEDS
    )
    stats_f4 = compute_cohort_stats(
        fwd_panel, filings_f4_entries, "Form-4", HORIZONS, CANARY_SEEDS
    )

    all_stats = [stats_8k, stats_f4]

    # 6. Write results
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results_path = OUTPUT_DIR / "raw_results.json"
    with open(results_path, "w") as json_file:
        json.dump(all_stats, json_file, indent=2, default=str)
    print(f"\nRaw results written to: {results_path}")

    # Print summary table
    table = format_results_table(all_stats)
    print("\n" + table)

    # Verdict
    print("\n=== VERDICT SUMMARY ===")
    for stat in all_stats:
        ft = stat["form_type"]
        best_horizon = None
        best_t = 0.0
        for horizon in HORIZONS:
            key = f"horizon_{horizon}d"
            if key in stat:
                t = abs(stat[key].get("t_stat", 0) or 0)
                if t > best_t:
                    best_t = t
                    best_horizon = horizon
        if best_horizon:
            bh = stat[f"horizon_{best_horizon}d"]
            clears = bh.get("clears_canary", False)
            survived_dm = abs(bh.get("t_stat_demean", 0) or 0) > 1.5
            print(
                f"{ft}: best horizon={best_horizon}d, "
                f"alpha={bh['alpha_mean_pct']:.3f}%, t={bh['t_stat']:.2f}, "
                f"clears_canary={clears}, survived_demean={survived_dm}"
            )

    print("\nDone.")


if __name__ == "__main__":
    main()
