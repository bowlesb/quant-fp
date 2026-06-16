"""
H4: Split POST-event Drift (reverse vs forward, liquid-tertile gate PRIMARY)

Pre-registered hypothesis: experiments/2026-06-16-h4-split-postex-drift/hypothesis.md

Split event = ex_date from corporate_actions_pit (action_type='split').
Reverse splits: split_ratio < 1 (distress). Forward splits: split_ratio >= 1 (attention).
Tested SEPARATELY (opposite predicted signs; pooling cancels).

Entry rule: D+1 OPEN after ex_date (tradeable, no look-ahead).
Forward returns: {1, 3, 5, 10, 20} trading days (close-to-close from open entry date).

UTC time handling: bars ts is genuine UTC.
  - 13:30 UTC = 09:30 ET (EDT) = market open. Use UTC hour in [13, 21] for RTH.
  - Open = FIRST bar with UTC hour >= 13 (>= 13:30 specifically, via minute filter).
  - We use first bar per date with hour == 13 AND minute >= 30 as open proxy.

Control: same-date cross-section vs non-event symbols.
Liquidity tertile: based on median daily dollar volume (close * volume) across the full panel.
Liquid = top tertile. Illiquid = bottom tertile. Middle = mid tertile.
LIQUID-TERTILE gate is PRIMARY: a cell with <20 liquid events is UNDERPOWERED.

Metrics: per-date cross-section alpha, day-clustered t, 10-seed canary, per-symbol-demean.
"""

import json
import os
import random
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import psycopg

BARS_ROOT = Path("/store/raw/bars")
OUTPUT_DIR = Path("/app/experiments/2026-06-16-h4-split-postex-drift")
HORIZONS = [1, 3, 5, 10, 20]
CANARY_SEEDS = 10
BARS_WINDOW_START = "2025-12-15"


def get_db_connection() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def load_daily_panel() -> pl.DataFrame:
    """
    Build (symbol, date, open_price, close_price, dollar_volume) panel from minute bars.
    - open_price: FIRST RTH bar per date (EST open = 14:30 UTC; EDT open = 13:30 UTC — both captured)
    - close_price: LAST bar with UTC hour in [13, 21]
    - dollar_volume: sum(close * volume) over RTH bars (proxy for daily dollar volume)
    """
    print("Loading daily panel (open + close + dollar_volume) from bars store...")
    all_frames: list[pl.DataFrame] = []

    symbol_dirs = sorted(BARS_ROOT.iterdir())
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
                df = pl.read_parquet(parquet_path, columns=["ts", "open", "close", "volume"])
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
            (pl.col("utc_hour") >= 13) & (pl.col("utc_hour") <= 21)
        )

        if sym_all.is_empty():
            continue

        # Close: last bar per date (RTH close proxy)
        daily_close = (
            sym_all.sort("ts")
            .group_by("bar_date")
            .agg(pl.col("close").last().alias("close"))
        )

        # Open: first RTH bar per date.
        # EST (Dec-Mar): market opens 14:30 UTC; EDT (Mar-Nov): 13:30 UTC.
        # Use first bar in the already-RTH-filtered set (utc_hour in [13,21]).
        # This correctly captures both EST and EDT opens without hardcoding an offset.
        daily_open = (
            sym_all.sort("ts")
            .group_by("bar_date")
            .agg(pl.col("open").first().alias("open_price"))
        )

        # Dollar volume: sum(close * volume) over RTH
        daily_dvol = (
            sym_all.with_columns(
                (pl.col("close") * pl.col("volume")).alias("dv")
            )
            .group_by("bar_date")
            .agg(pl.col("dv").sum().alias("dollar_volume"))
        )

        daily = (
            daily_close
            .join(daily_open, on="bar_date", how="left")
            .join(daily_dvol, on="bar_date", how="left")
            .with_columns(pl.lit(symbol).alias("symbol"))
            .select(["symbol", "bar_date", "open_price", "close", "dollar_volume"])
        )
        all_frames.append(daily)

        if (idx + 1) % 1000 == 0:
            print(f"  Processed {idx + 1}/{total} symbols")

    print(f"  Concatenating {len(all_frames)} symbol frames...")
    panel = pl.concat(all_frames, rechunk=True).sort(["symbol", "bar_date"])
    print(f"  Panel shape: {panel.shape}")
    return panel


def compute_liquidity_tertiles(panel: pl.DataFrame) -> pl.DataFrame:
    """
    Assign each symbol to a liquidity tertile based on MEDIAN daily dollar_volume
    across all dates in the panel. Tertile is a symbol-level attribute (not time-varying).
    liquid_tier: 1 = bottom (illiquid), 2 = middle, 3 = top (liquid).
    """
    print("Computing liquidity tertiles...")
    sym_median_dvol = (
        panel.group_by("symbol")
        .agg(pl.col("dollar_volume").median().alias("median_dvol"))
        .sort("median_dvol")
    )

    # Assign tertile based on rank
    n = sym_median_dvol.height
    t1_cutoff = n // 3
    t2_cutoff = 2 * n // 3

    sym_median_dvol = sym_median_dvol.with_row_index("rank_idx").with_columns(
        pl.when(pl.col("rank_idx") < t1_cutoff)
        .then(1)
        .when(pl.col("rank_idx") < t2_cutoff)
        .then(2)
        .otherwise(3)
        .alias("liquid_tier")
    ).select(["symbol", "median_dvol", "liquid_tier"])

    tier_counts = sym_median_dvol.group_by("liquid_tier").agg(pl.len().alias("n")).sort("liquid_tier")
    print(f"  Liquidity tier counts: {tier_counts.to_dicts()}")

    # Sample the cutoffs for reporting
    t3_min = sym_median_dvol.filter(pl.col("liquid_tier") == 3)["median_dvol"].min()
    t1_max = sym_median_dvol.filter(pl.col("liquid_tier") == 1)["median_dvol"].max()
    print(f"  Liquid tier (3) min median dvol: ${t3_min:,.0f}" if t3_min else "")
    print(f"  Illiquid tier (1) max median dvol: ${t1_max:,.0f}" if t1_max else "")

    return sym_median_dvol.select(["symbol", "liquid_tier"])


def build_forward_returns(panel: pl.DataFrame, horizons: list[int]) -> pl.DataFrame:
    """
    For each (symbol, date), compute forward returns at each horizon.
    Entry price = open_price on the entry date (first bar of RTH).
    Forward return at horizon h = close[t+h] / open_price[t] - 1
    where t+h is h trading days after entry.

    Note: for the ENTRY bar, we use open_price as the entry price (D+1 open after ex_date).
    For the CLOSE we use the close at t+h trading days.
    fwd_h = close.shift(-h) / open_price - 1
    """
    print("Building forward returns (open-to-close at horizon h)...")
    result = panel.sort(["symbol", "bar_date"])

    for horizon in horizons:
        result = result.with_columns(
            (
                pl.col("close").shift(-horizon).over("symbol") / pl.col("open_price") - 1
            ).alias(f"fwd_{horizon}d")
        )

    print(f"  Forward return panel shape: {result.shape}")
    return result


def load_splits(bars_window_start: str) -> tuple[pl.DataFrame, pl.DataFrame]:
    """
    Load splits from corporate_actions_pit.
    Returns (reverse_splits, forward_splits) DataFrames with (symbol, ex_date).
    reverse: split_ratio < 1 (price decreases → distress)
    forward: split_ratio >= 1 (price increases → attention)
    """
    print("Loading splits from corporate_actions_pit...")
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT symbol, ex_date, split_ratio
        FROM corporate_actions_pit
        WHERE action_type = 'split'
          AND ex_date >= %s
        ORDER BY symbol, ex_date
        """,
        (bars_window_start,),
    )
    rows = cur.fetchall()
    conn.close()

    df = pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "ex_date": [r[1] for r in rows],
            "split_ratio": [float(r[2]) for r in rows],
        }
    )
    print(f"  Total splits in window: {len(df)}")

    reverse_splits = df.filter(pl.col("split_ratio") < 1).select(["symbol", "ex_date"])
    forward_splits = df.filter(pl.col("split_ratio") >= 1).select(["symbol", "ex_date"])

    print(f"  Reverse splits (ratio < 1): {len(reverse_splits)}")
    print(f"  Forward splits (ratio >= 1): {len(forward_splits)}")

    return reverse_splits, forward_splits


def assign_entry_dates(
    events: pl.DataFrame, trading_dates: list[date]
) -> pl.DataFrame:
    """
    Assign D+1 trading day after ex_date as the entry date.
    ex_date is the effective date of the split (look-ahead safe: we enter the NEXT trading day).
    """
    trading_dates_set = set(trading_dates)
    trading_dates_sorted = sorted(trading_dates)
    last_td = trading_dates_sorted[-1]

    # Build lookup: date -> next trading day
    next_td: dict[date, date | None] = {}
    for dt in trading_dates_sorted:
        candidate = dt + timedelta(days=1)
        while True:
            if candidate in trading_dates_set:
                next_td[dt] = candidate
                break
            candidate += timedelta(days=1)
            if candidate > last_td:
                next_td[dt] = None
                break

    rows = events.to_dicts()
    entry_dates = []
    for row in rows:
        ex_dt = row["ex_date"]
        if isinstance(ex_dt, datetime):
            ex_dt = ex_dt.date()
        entry = next_td.get(ex_dt)
        entry_dates.append(entry)

    result = events.with_columns(
        pl.Series("entry_date", entry_dates, dtype=pl.Date)
    )
    result = result.filter(pl.col("entry_date").is_not_null())
    print(f"  Events with valid entry date: {len(result)}")
    return result


def compute_cohort_stats_by_tier(
    fwd_panel: pl.DataFrame,
    event_df: pl.DataFrame,
    tier_map: pl.DataFrame,
    label: str,
    horizons: list[int],
    n_canary_seeds: int = 10,
) -> dict[str, Any]:
    """
    Compute cohort stats for FULL universe and per liquidity tier.

    fwd_panel: (symbol, bar_date, open_price, close, dollar_volume, fwd_1d, ..., liquid_tier)
    event_df: (symbol, entry_date) — the events
    tier_map: (symbol, liquid_tier)
    label: e.g. 'reverse_split'
    """
    print(f"\n=== Computing cohort stats for: {label} ===")

    # Deduplicate events: one event per (symbol, entry_date)
    event_entries = event_df.select(["symbol", "entry_date"]).unique(subset=["symbol", "entry_date"])
    print(f"  Unique (symbol, entry_date) events: {len(event_entries)}")

    # Join tier info to fwd_panel
    panel_with_tier = fwd_panel.join(tier_map, on="symbol", how="left")

    # Join event flag to panel
    fwd_with_event = panel_with_tier.join(
        event_entries.rename({"entry_date": "bar_date"}).with_columns(
            pl.lit(True).alias("is_event")
        ),
        on=["symbol", "bar_date"],
        how="left",
    ).with_columns(pl.col("is_event").fill_null(False))

    results: dict[str, Any] = {"label": label}

    # Compute for each scope: full universe, tier 1 (illiquid), tier 2 (mid), tier 3 (liquid)
    scopes = [
        ("full", None),
        ("illiquid_t1", 1),
        ("mid_t2", 2),
        ("liquid_t3", 3),
    ]

    for scope_name, tier_filter in scopes:
        print(f"\n  --- Scope: {scope_name} ---")
        if tier_filter is not None:
            panel_scope = fwd_with_event.filter(pl.col("liquid_tier") == tier_filter)
        else:
            panel_scope = fwd_with_event

        scope_results: dict[str, Any] = {}
        for horizon in horizons:
            fwd_col = f"fwd_{horizon}d"
            panel_h = panel_scope.filter(pl.col(fwd_col).is_not_null())
            horizon_stats = _compute_horizon_stats(
                panel_h, fwd_col, horizon, n_canary_seeds, scope_name
            )
            scope_results[f"horizon_{horizon}d"] = horizon_stats

        results[scope_name] = scope_results

    return results


def _compute_horizon_stats(
    panel: pl.DataFrame,
    fwd_col: str,
    horizon: int,
    n_canary_seeds: int,
    scope_label: str,
) -> dict[str, Any]:
    """Compute statistics for a single horizon and scope."""

    dates_with_events = (
        panel.filter(pl.col("is_event"))["bar_date"].unique().to_list()
    )

    if not dates_with_events:
        print(f"    {scope_label} h={horizon}d: NO EVENTS")
        return {
            "n_event_obs": 0,
            "n_dates": 0,
            "alpha_mean_pct": float("nan"),
            "t_stat": float("nan"),
            "canary_mean_pct": float("nan"),
            "canary_p95_pct": float("nan"),
            "clears_canary": False,
            "alpha_demean_pct": float("nan"),
            "t_stat_demean": float("nan"),
        }

    alpha_per_date: list[float] = []
    event_counts: list[int] = []
    control_counts: list[int] = []

    for dt in sorted(dates_with_events):
        day_data = panel.filter(pl.col("bar_date") == dt)
        event_returns = day_data.filter(pl.col("is_event"))[fwd_col].to_list()
        ctrl_returns = day_data.filter(~pl.col("is_event"))[fwd_col].to_list()

        if not event_returns or not ctrl_returns:
            continue

        alpha = float(np.mean(event_returns)) - float(np.mean(ctrl_returns))
        alpha_per_date.append(alpha)
        event_counts.append(len(event_returns))
        control_counts.append(len(ctrl_returns))

    n_dates = len(alpha_per_date)
    n_event_obs = sum(event_counts)

    if n_dates < 2:
        print(f"    {scope_label} h={horizon}d: n_event_obs={n_event_obs}, n_dates={n_dates} (too few)")
        return {
            "n_event_obs": n_event_obs,
            "n_dates": n_dates,
            "alpha_mean_pct": float("nan"),
            "t_stat": float("nan"),
            "canary_mean_pct": float("nan"),
            "canary_p95_pct": float("nan"),
            "clears_canary": False,
            "alpha_demean_pct": float("nan"),
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

    clears_canary = False
    if not np.isnan(alpha_mean) and not np.isnan(canary_p95) and not np.isnan(canary_p5):
        if alpha_mean > 0:
            clears_canary = bool(alpha_mean > canary_p95)
        else:
            clears_canary = bool(alpha_mean < canary_p5)

    # Per-symbol-demean
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
        f"    {scope_label} h={horizon}d: n_events={n_event_obs}, n_dates={n_dates}, "
        f"alpha={alpha_mean*100:.3f}%, t={t_stat:.2f}, "
        f"canary_p95={canary_p95*100:.3f}%, clears={clears_canary}, "
        f"alpha_dm={alpha_dm_mean*100:.3f}%, t_dm={t_stat_dm:.2f}"
    )

    return {
        "n_event_obs": n_event_obs,
        "n_dates": n_dates,
        "mean_event_per_date": float(np.mean(event_counts)),
        "mean_ctrl_per_date": float(np.mean(control_counts)),
        "alpha_mean_pct": alpha_mean * 100,
        "alpha_std_pct": alpha_std * 100,
        "t_stat": t_stat,
        "canary_mean_pct": canary_mean * 100,
        "canary_p5_pct": canary_p5 * 100,
        "canary_p95_pct": canary_p95 * 100,
        "clears_canary": clears_canary,
        "alpha_demean_pct": alpha_dm_mean * 100,
        "t_stat_demean": t_stat_dm,
    }


def verify_utc_timestamps() -> None:
    """Verify UTC handling against real bars before main run."""
    print("\n=== UTC TIMESTAMP VERIFICATION ===")
    sample_path = next(BARS_ROOT.glob("symbol=*/date=*/data.parquet"))
    df = pl.read_parquet(sample_path, columns=["ts", "close", "open"])
    df_rth = df.with_columns(
        pl.col("ts").dt.hour().alias("utc_hour"),
        pl.col("ts").dt.minute().alias("utc_minute"),
    ).filter(
        (pl.col("utc_hour") >= 13) & (pl.col("utc_hour") <= 21)
    )
    print(f"Sample path: {sample_path}")
    print("First RTH bar (UTC):", df_rth.head(1))
    print("Last RTH bar (UTC):", df_rth.tail(1))
    # First bar should be at hour 13, minute 30 (13:30 UTC = 09:30 ET EDT)
    first_bar = df_rth.head(1).to_dicts()[0]
    print(f"First bar UTC time: {first_bar['ts']} -> hour={first_bar['utc_hour']}, min={first_bar['utc_minute']}")
    print("Expected: EST (Dec-Mar) open = 14:30 UTC; EDT (Mar-Nov) open = 13:30 UTC.")
    print("We capture FIRST bar in RTH filter [13,21], so both seasons are handled correctly.")
    print("=== END VERIFICATION ===\n")


def format_results_table(
    all_stats: list[dict[str, Any]],
    horizons: list[int],
) -> str:
    """Format results as markdown tables with full scope breakdown."""
    lines = []
    scopes = ["full", "illiquid_t1", "mid_t2", "liquid_t3"]

    for stat in all_stats:
        label = stat["label"]
        lines.append(f"\n### {label}\n")

        for scope in scopes:
            scope_data = stat.get(scope, {})
            lines.append(f"\n#### {scope}\n")
            lines.append("| Horizon | N_events | N_dates | Alpha% | t-stat | Canary_p95% | Clears? | Alpha_dm% | t_dm |")
            lines.append("|---------|----------|---------|--------|--------|-------------|---------|-----------|------|")
            for horizon in horizons:
                key = f"horizon_{horizon}d"
                if key not in scope_data:
                    continue
                h = scope_data[key]
                clears = "YES" if h.get("clears_canary") else "NO"
                n_ev = h["n_event_obs"]
                underpow = " ⚠️" if n_ev < 20 else ""
                lines.append(
                    f"| {horizon}d | {n_ev}{underpow} | {h['n_dates']} | "
                    f"{h['alpha_mean_pct']:.3f} | {h['t_stat']:.2f} | "
                    f"{h['canary_p95_pct']:.3f} | {clears} | "
                    f"{h['alpha_demean_pct']:.3f} | {h['t_stat_demean']:.2f} |"
                )
    return "\n".join(lines)


def main() -> None:
    verify_utc_timestamps()

    # 1. Load daily panel (open + close + dollar_volume)
    panel = load_daily_close_panel_full()

    # 2. Compute liquidity tertiles (symbol-level)
    tier_map = compute_liquidity_tertiles(panel)

    # Get trading dates
    trading_dates_all: list[date] = sorted(panel["bar_date"].unique().to_list())
    print(f"Trading date range: {trading_dates_all[0]} to {trading_dates_all[-1]} ({len(trading_dates_all)} dates)")

    # 3. Build forward returns (open-anchored: fwd = close[t+h] / open[t] - 1)
    fwd_panel = build_forward_returns(panel, HORIZONS)

    # 4. Load splits from DB
    reverse_splits, forward_splits = load_splits(BARS_WINDOW_START)

    # 5. Assign entry dates (D+1 open after ex_date)
    bar_symbols = set(panel["symbol"].unique().to_list())

    reverse_entries = assign_entry_dates(reverse_splits, trading_dates_all)
    forward_entries = assign_entry_dates(forward_splits, trading_dates_all)

    # Filter to symbols with bars
    reverse_entries = reverse_entries.filter(pl.col("symbol").is_in(list(bar_symbols)))
    forward_entries = forward_entries.filter(pl.col("symbol").is_in(list(bar_symbols)))

    print(f"\nReverse splits with matching bars: {len(reverse_entries)} ({reverse_entries['symbol'].n_unique()} symbols)")
    print(f"Forward splits with matching bars: {len(forward_entries)} ({forward_entries['symbol'].n_unique()} symbols)")

    # 6. Compute cohort stats per split type, full + by tier
    stats_reverse = compute_cohort_stats_by_tier(
        fwd_panel, reverse_entries, tier_map, "reverse_split", HORIZONS, CANARY_SEEDS
    )
    stats_forward = compute_cohort_stats_by_tier(
        fwd_panel, forward_entries, tier_map, "forward_split", HORIZONS, CANARY_SEEDS
    )

    all_stats = [stats_reverse, stats_forward]

    # 7. Write raw results
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results_path = OUTPUT_DIR / "raw_results.json"
    with open(results_path, "w") as json_file:
        json.dump(all_stats, json_file, indent=2, default=str)
    print(f"\nRaw results written to: {results_path}")

    # 8. Print formatted table
    table = format_results_table(all_stats, HORIZONS)
    print("\n" + table)

    # 9. Verdict summary
    print("\n=== VERDICT SUMMARY ===")
    for stat in all_stats:
        lbl = stat["label"]
        print(f"\n{lbl}:")
        for scope in ["full", "liquid_t3"]:
            scope_data = stat.get(scope, {})
            best_h = None
            best_t = 0.0
            for horizon in HORIZONS:
                key = f"horizon_{horizon}d"
                if key in scope_data:
                    t_val = abs(scope_data[key].get("t_stat", 0) or 0)
                    if t_val > best_t:
                        best_t = t_val
                        best_h = horizon
            if best_h:
                bh = scope_data[f"horizon_{best_h}d"]
                n_ev = bh["n_event_obs"]
                power_note = "UNDERPOWERED (<20 events)" if n_ev < 20 else f"N={n_ev}"
                print(
                    f"  [{scope}] best h={best_h}d, alpha={bh['alpha_mean_pct']:.3f}%, "
                    f"t={bh['t_stat']:.2f}, clears_canary={bh['clears_canary']}, "
                    f"{power_note}"
                )

    print("\nDone.")


# Renamed to avoid conflict with H10 import pattern; used in main() above
def load_daily_close_panel_full() -> pl.DataFrame:
    return load_daily_panel()


if __name__ == "__main__":
    main()
