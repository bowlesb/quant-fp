"""
H9: Longer-horizon vwap_dev reversion (H60/H120).

Run from sandbox:
  cd /home/ben/quant-fp && MEM=12g CPUS=8 ops/sandbox.sh "python experiments/2026-06-16-h9-longhorizon-reversion/run_h9.py"

Data path: /store/raw/bars/symbol=<S>/date=<D>/data.parquet
Output: /app/experiments/2026-06-16-h9-longhorizon-reversion/
"""

import os
import json
import random
import polars as pl
import numpy as np
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
STORE = Path("/store/raw/bars")
OUT = Path("/app/experiments/2026-06-16-h9-longhorizon-reversion")
OUT.mkdir(parents=True, exist_ok=True)

# Use most recent ~50 trading days (skip 2026-06-16 = today/empty)
N_DAYS = 50

# RTH filter: bars timestamps are stored as ET (labeled UTC in the file).
# 09:30 ET = minute 570, 15:50 ET = minute 950 (exclude >=950 for scoring).
# 16:00 ET = minute 960 for the load filter (include close of last bar).
RTH_START_UTC = 9 * 60 + 30    # 570 — 09:30 ET stored as "UTC"
RTH_END_UTC   = 15 * 60 + 50   # 950 — exclude >=15:50 ET for scoring

# Universe: top ~300 names by median daily dollar-volume, require full date coverage
UNIVERSE_SIZE = 300
MIN_DATE_COVERAGE = 0.90   # symbol must appear on ≥90% of the selected days

# Signal windows and forward horizons (minutes)
SIGNAL_WINDOWS = [30, 60]
FWD_HORIZONS   = [60, 120]

# Cost assumptions (round-trip bps)
COST_RT_BPS = [4.0, 6.0, 10.0]
CANARY_SEEDS = 10

DECILE = 10   # number of quantile buckets

print("=== H9 Long-Horizon VWAP-Dev Reversion ===")

# ── Step 1: Enumerate available dates ────────────────────────────────────────
print("\n[1] Enumerating dates...")
all_symbols = sorted([d.name.split("=")[1] for d in STORE.iterdir() if d.name.startswith("symbol=")])
# Use AAPL as reference for available dates
aapl_dates = sorted([d.name.split("=")[1] for d in (STORE / "symbol=AAPL").iterdir()])
# Exclude any empty partitions (today's partial)
# Take last N_DAYS
selected_dates = aapl_dates[-N_DAYS:]
print(f"  Selected {len(selected_dates)} dates: {selected_dates[0]} → {selected_dates[-1]}")

# ── Step 2: Build universe via dollar-volume scan ────────────────────────────
print("\n[2] Building liquid universe (sampling ~10 days for dv estimate)...")

# Sample 10 evenly spaced dates from the selected window for speed
sample_dates = selected_dates[::max(1, len(selected_dates)//10)][:10]
dv_records = []

for sym in all_symbols:
    sym_path = STORE / f"symbol={sym}"
    daily_dvs = []
    for d in sample_dates:
        fpath = sym_path / f"date={d}" / "data.parquet"
        if not fpath.exists():
            continue
        df = pl.read_parquet(fpath, columns=["close", "volume"])
        # RTH only for dv
        # We'll just use all rows for speed at universe selection stage
        if df.is_empty():
            continue
        dv = (df["close"] * df["volume"]).sum()
        daily_dvs.append(dv)
    if len(daily_dvs) >= len(sample_dates) * 0.8:
        dv_records.append({"symbol": sym, "median_dv": float(np.median(daily_dvs))})

dv_df = pl.DataFrame(dv_records).sort("median_dv", descending=True)
top_symbols = dv_df.head(UNIVERSE_SIZE)["symbol"].to_list()
print(f"  Candidate universe: {len(top_symbols)} symbols by dv. Top 5: {top_symbols[:5]}")

# ── Step 3: Load and assemble RTH bars panel for selected universe + dates ───
print("\n[3] Loading bars panel...")

frames = []
for sym in top_symbols:
    sym_path = STORE / f"symbol={sym}"
    for d in selected_dates:
        fpath = sym_path / f"date={d}" / "data.parquet"
        if not fpath.exists():
            continue
        df = pl.read_parquet(fpath)
        if df.is_empty():
            continue
        df = df.with_columns([
            pl.lit(d).alias("date"),
            # minute-of-day in ET (stored as "UTC" in the parquet files).
            # Cast to Int32 to avoid i8 overflow (9*60 = 540 > i8 max of 127).
            (pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32)).alias("utc_minute"),
        ])
        frames.append(df)

print(f"  Loaded {len(frames)} (symbol, date) partitions")
bars = pl.concat(frames, how="diagonal_relaxed")
print(f"  Raw rows: {bars.shape[0]:,}")

# Load filter: keep 09:30–18:00 ET to allow forward return lookups up to H=120 beyond 15:50.
# The signal scoring filter (exclude signals at >=15:50) is applied per (W,H) in the analysis.
RTH_LOAD_END = 18 * 60  # 18:00 ET = minute 1080, covers T+120 for T<=15:50
bars = bars.filter(
    (pl.col("utc_minute") >= RTH_START_UTC) & (pl.col("utc_minute") < RTH_LOAD_END)
)
print(f"  After RTH filter: {bars.shape[0]:,}")

# Drop rows with zero volume (can't compute vwap contribution)
bars = bars.filter(pl.col("volume") > 0)

# ── Step 4: Filter universe to symbols with sufficient date coverage ─────────
date_counts = (
    bars.group_by("symbol")
    .agg(pl.col("date").n_unique().alias("n_dates"))
    .filter(pl.col("n_dates") >= int(len(selected_dates) * MIN_DATE_COVERAGE))
)
final_symbols = date_counts["symbol"].to_list()
bars = bars.filter(pl.col("symbol").is_in(final_symbols))
print(f"  Final universe after coverage filter: {len(final_symbols)} symbols")
print(f"  Final rows: {bars.shape[0]:,}")

# ── Step 5: Sort and compute trailing vwap_dev (vectorized) ─────────────────
print("\n[4] Computing trailing VWAP deviations...")

# Sort by (symbol, date, utc_minute)
bars = bars.sort(["symbol", "date", "utc_minute"])

# For each signal window W, compute trailing W-min VWAP = sum(close*vol)/sum(vol)
# using rolling_sum over sorted order within each (symbol, date) group.
# Polars rolling requires the data sorted within each group. We use group_by_dynamic or
# just direct rolling after sort.

result_frames = {}

for W in SIGNAL_WINDOWS:
    print(f"  W={W}: computing trailing {W}-min VWAP...")
    # Use rolling sums within (symbol, date) partition
    df_w = (
        bars
        .with_columns([
            (pl.col("close") * pl.col("volume")).alias("cv"),
        ])
        .with_columns([
            pl.col("cv").rolling_sum(window_size=W, min_samples=W).over(["symbol", "date"]).alias("sum_cv_W"),
            pl.col("volume").rolling_sum(window_size=W, min_samples=W).over(["symbol", "date"]).alias("sum_vol_W"),
        ])
        .with_columns([
            (pl.col("sum_cv_W") / pl.col("sum_vol_W")).alias(f"tvwap_{W}"),
        ])
        .with_columns([
            (pl.col("close") / pl.col(f"tvwap_{W}") - 1.0).alias(f"vwap_dev_{W}"),
        ])
        .drop(["cv", "sum_cv_W", "sum_vol_W", f"tvwap_{W}"])
    )
    result_frames[W] = df_w

# Merge the two signal windows
bars_sig = result_frames[SIGNAL_WINDOWS[0]]
for W in SIGNAL_WINDOWS[1:]:
    bars_sig = bars_sig.join(
        result_frames[W].select(["symbol", "date", "utc_minute", f"vwap_dev_{W}"]),
        on=["symbol", "date", "utc_minute"],
        how="left",
    )

# ── Step 6: Compute forward returns at H ────────────────────────────────────
print("\n[5] Computing forward returns...")

# For each horizon H, forward return = close[T+H] / close[T] - 1
# We match on (symbol, date, utc_minute+H)
# Strategy: join the close column shifted by H minutes within each (symbol, date)

for H in FWD_HORIZONS:
    bars_sig = bars_sig.with_columns([
        (pl.col("utc_minute") + H).alias("target_minute"),
    ]).join(
        bars_sig.select(["symbol", "date", "utc_minute", "close"]).rename({"close": f"close_fwd_{H}", "utc_minute": "target_minute"}),
        on=["symbol", "date", "target_minute"],
        how="left",
    ).with_columns([
        (pl.col(f"close_fwd_{H}") / pl.col("close") - 1.0).alias(f"fwd_ret_{H}"),
    ]).drop("target_minute")

print(f"  bars_sig shape: {bars_sig.shape}")

# ── Step 7: Scoring filter — exclude ≥19:50 UTC for scoring ─────────────────
# (signal must be at T, and we need T+H to be reachable within RTH)
# Already excluded T≥19:50 via RTH filter. But forward return requires close[T+H] to exist.
# Rows where fwd_ret is null (T+H outside day) are dropped naturally in the analysis below.

# Save panel to /tmp for inspection
bars_sig.write_parquet("/tmp/h9_panel.parquet")
print(f"  Panel saved to /tmp/h9_panel.parquet")

# ── Step 8: Decile L/S analysis per (W, H) ──────────────────────────────────
print("\n[6] Computing decile L/S results...")

results = {}

for W in SIGNAL_WINDOWS:
    for H in FWD_HORIZONS:
        sig_col = f"vwap_dev_{W}"
        ret_col = f"fwd_ret_{H}"

        # Working panel: only rows with valid signal and forward return
        # Rebalance cadence = every H minutes, so we snap signals to multiples of H
        # RTH start = 13:30 UTC = 810 minutes. Rebalance at minutes: 810, 810+H, 810+2H, ...
        # We define rebalance_slot = floor((utc_minute - RTH_START_UTC) / H)
        # and keep only rows where utc_minute == RTH_START_UTC + slot * H
        panel = (
            bars_sig
            .filter(
                pl.col(sig_col).is_not_null()
                & pl.col(ret_col).is_not_null()
                # Only score signals where T < 15:50 ET (RTH_END_UTC)
                & (pl.col("utc_minute") < RTH_END_UTC)
            )
            .with_columns([
                (((pl.col("utc_minute") - RTH_START_UTC) / H).floor().cast(pl.Int32)).alias("slot"),
            ])
            .filter(
                pl.col("utc_minute") == (RTH_START_UTC + pl.col("slot") * H)
            )
        )

        n_obs = panel.shape[0]
        n_dates_panel = panel["date"].n_unique()
        print(f"\n  W={W}, H={H}: {n_obs:,} obs across {n_dates_panel} dates")

        if n_obs < 100:
            print(f"    Too few observations, skipping.")
            continue

        # Decile rank within each (date, slot) cross-section
        # Reversion: LONG most-negative vwap_dev (rank=1), SHORT most-positive (rank=10)
        panel = panel.with_columns([
            pl.col(sig_col).rank("ordinal").over(["date", "slot"]).alias("rank_raw"),
            pl.col(sig_col).count().over(["date", "slot"]).alias("cs_count"),
        ]).with_columns([
            # decile 1 = most negative (long), 10 = most positive (short)
            ((pl.col("rank_raw") - 1) / pl.col("cs_count") * DECILE).floor().cast(pl.Int32).clip(0, DECILE - 1).alias("decile"),
        ])

        # L/S per (date, slot): long decile 0, short decile 9
        long_ret = (
            panel.filter(pl.col("decile") == 0)
            .group_by(["date", "slot"])
            .agg(pl.col(ret_col).mean().alias("long_ret"))
        )
        short_ret = (
            panel.filter(pl.col("decile") == DECILE - 1)
            .group_by(["date", "slot"])
            .agg(pl.col(ret_col).mean().alias("short_ret"))
        )
        ls = long_ret.join(short_ret, on=["date", "slot"], how="inner").with_columns([
            (pl.col("long_ret") - pl.col("short_ret")).alias("ls_ret"),
        ])

        gross_bps = float(ls["ls_ret"].mean()) * 10000
        n_periods = ls.shape[0]
        print(f"    Gross L/S: {gross_bps:.2f} bps ({n_periods} rebalance periods)")

        # ── Turnover ──────────────────────────────────────────────────────
        # Fraction of names changing leg from slot to slot+1 within each date
        # Leg: 1=long (decile==0), -1=short (decile==9), 0=neutral
        panel_leg = panel.with_columns([
            pl.when(pl.col("decile") == 0).then(pl.lit(1))
            .when(pl.col("decile") == DECILE - 1).then(pl.lit(-1))
            .otherwise(pl.lit(0))
            .alias("leg"),
        ]).select(["symbol", "date", "slot", "leg"])

        # Join t and t+1 slots
        prev_panel = panel_leg.rename({"slot": "prev_slot", "leg": "prev_leg"}).with_columns([
            (pl.col("prev_slot") + 1).alias("slot"),
        ])
        turnover_df = panel_leg.join(prev_panel, on=["symbol", "date", "slot"], how="inner")
        # Turnover = leg changed (and at least one is non-zero, i.e., it was in a leg)
        turnover_df = turnover_df.with_columns([
            (
                (pl.col("leg") != pl.col("prev_leg")) &
                ((pl.col("leg") != 0) | (pl.col("prev_leg") != 0))
            ).cast(pl.Float64).alias("changed"),
            # Only consider names that were in a leg (long or short) in either period
            ((pl.col("leg") != 0) | (pl.col("prev_leg") != 0)).cast(pl.Float64).alias("in_leg"),
        ]).filter(pl.col("in_leg") > 0)

        turnover = float(turnover_df["changed"].mean()) if turnover_df.shape[0] > 0 else float("nan")
        print(f"    Turnover (fraction changing leg): {turnover:.3f}")

        # ── Net = gross - turnover * cost ─────────────────────────────────
        nets = {}
        for cost in COST_RT_BPS:
            net = gross_bps - turnover * cost
            nets[cost] = net
            print(f"    Net @{cost}bps cost: {net:.2f} bps")

        # ── Day-clustered t-stat ──────────────────────────────────────────
        # Cluster by date: mean L/S per day, then t-test over days
        daily_ls = ls.group_by("date").agg(pl.col("ls_ret").mean().alias("daily_ls"))
        daily_arr = daily_ls["daily_ls"].to_numpy()
        n_days_clust = len(daily_arr)
        if n_days_clust > 1:
            tstat = float(np.mean(daily_arr) / (np.std(daily_arr, ddof=1) / np.sqrt(n_days_clust)))
        else:
            tstat = float("nan")
        print(f"    Day-clustered t: {tstat:.2f} (n_days={n_days_clust})")

        # ── Canary (within-CS shuffle) ────────────────────────────────────
        canary_gross = []
        for seed in range(CANARY_SEEDS):
            rng = np.random.default_rng(seed)
            # Shuffle fwd_ret within each (date, slot) cross-section
            shuffled = panel.with_columns([
                pl.Series(
                    ret_col + "_shuf",
                    # per cross-section shuffle
                    panel.with_columns([
                        pl.col(ret_col).alias("_ret"),
                        (pl.col("date") + "_" + pl.col("slot").cast(pl.Utf8)).alias("_cs_key"),
                    ]).select(
                        pl.col("_ret").shuffle(seed=seed).over("_cs_key")
                    ).to_series().to_numpy(),
                )
            ])
            long_s = (
                shuffled.filter(pl.col("decile") == 0)
                .group_by(["date", "slot"])
                .agg(pl.col(ret_col + "_shuf").mean().alias("lr"))
            )
            short_s = (
                shuffled.filter(pl.col("decile") == DECILE - 1)
                .group_by(["date", "slot"])
                .agg(pl.col(ret_col + "_shuf").mean().alias("sr"))
            )
            ls_s = long_s.join(short_s, on=["date", "slot"], how="inner")
            if ls_s.shape[0] > 0:
                canary_gross.append(float((ls_s["lr"] - ls_s["sr"]).mean()) * 10000)

        canary_mean = float(np.mean(canary_gross)) if canary_gross else float("nan")
        canary_std  = float(np.std(canary_gross)) if canary_gross else float("nan")
        canary_95   = canary_mean + 2 * canary_std
        print(f"    Canary band: mean={canary_mean:.2f}, std={canary_std:.2f}, 95th={canary_95:.2f} bps")
        clears_canary = gross_bps > canary_95
        print(f"    Clears canary (gross > canary_95): {clears_canary}")

        results[(W, H)] = {
            "W": W,
            "H": H,
            "n_obs": n_obs,
            "n_periods": n_periods,
            "n_days": n_days_clust,
            "gross_bps": gross_bps,
            "turnover": turnover,
            "net_4bps": nets[4.0],
            "net_6bps": nets[6.0],
            "net_10bps": nets[10.0],
            "tstat_day_clustered": tstat,
            "canary_mean": canary_mean,
            "canary_std": canary_std,
            "canary_95": canary_95,
            "clears_canary": clears_canary,
            "net_positive_at_6bps": nets[6.0] > 0,
            "net_positive_at_4bps": nets[4.0] > 0,
            "net_positive_at_10bps": nets[10.0] > 0,
        }

# ── Save results JSON ────────────────────────────────────────────────────────
results_json = {str(k): v for k, v in results.items()}
with open(OUT / "raw_results.json", "w") as f:
    json.dump(results_json, f, indent=2)
print(f"\n  Results saved to {OUT}/raw_results.json")

# ── Summary table ────────────────────────────────────────────────────────────
print("\n\n=== SUMMARY TABLE ===")
print(f"{'W':>4} {'H':>4} {'Gross':>8} {'Turn':>7} {'Net@4':>8} {'Net@6':>8} {'Net@10':>8} {'t-stat':>8} {'Can95':>8} {'ClrCan':>7}")
print("-" * 80)
for (W, H), r in sorted(results.items()):
    print(f"{W:>4} {H:>4} {r['gross_bps']:>8.2f} {r['turnover']:>7.3f} {r['net_4bps']:>8.2f} {r['net_6bps']:>8.2f} {r['net_10bps']:>8.2f} {r['tstat_day_clustered']:>8.2f} {r['canary_95']:>8.2f} {str(r['clears_canary']):>7}")

print("\nDone.")
